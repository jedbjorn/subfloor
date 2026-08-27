"""Sprint 28 WU121: freeze DSH removal inputs before destructive work."""
from __future__ import annotations

import gzip
import importlib.util
import io
import json
import sqlite3
import tarfile
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests/fixtures/dsh_removal"
MANIFEST_PATH = ROOT / ".super-coder/assets/dsh-removal/removal-manifest-v1.json"
PRE_BRIDGE_PATH = FIXTURE_DIR / "pre-bridge.json"
COMPATIBILITY_PATH = FIXTURE_DIR / "compatibility-floor.json"
PAYLOAD_PATH = FIXTURE_DIR / "tracked-artifacts.tar.gz"
GENERATOR_PATH = FIXTURE_DIR / "build_fixtures.py"

SPEC = importlib.util.spec_from_file_location("dsh_removal_fixtures", GENERATOR_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load DSH removal fixture generator")
FIXTURES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURES)


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def replay_database(fixture: dict) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    try:
        con.executescript((ROOT / ".super-coder/schema.sql").read_text())
        ledger = load(MANIFEST_PATH)["immutable_migration_ledger"]
        self_floor = fixture["migration_floor"]
        selected = [row for row in ledger if row["filename"] <= self_floor["last"]]
        if len(selected) != self_floor["count"]:
            raise AssertionError("fixture migration count disagrees with frozen ledger")
        for row in selected:
            migration = ROOT / row["path"]
            if FIXTURES.sha256_file(migration) != row["sha256"]:
                raise AssertionError(f"immutable migration changed: {row['filename']}")
            con.executescript(migration.read_text())

        trigger_sql = [
            (row[0], row[1])
            for row in con.execute(
                "SELECT name,sql FROM sqlite_master "
                "WHERE type='trigger' AND sql IS NOT NULL ORDER BY name"
            )
        ]
        con.execute("PRAGMA foreign_keys=OFF")
        for name, _ in trigger_sql:
            con.execute(f'DROP TRIGGER "{name}"')
        for table, rows in fixture["database_rows"].items():
            for row in rows:
                columns = list(row)
                placeholders = ",".join("?" for _ in columns)
                names = ",".join(f'"{name}"' for name in columns)
                con.execute(
                    f'INSERT OR REPLACE INTO "{table}" ({names}) VALUES ({placeholders})',
                    [row[name] for name in columns],
                )
        for _, sql in trigger_sql:
            con.execute(sql)
        con.commit()
        con.execute("PRAGMA foreign_keys=ON")
        return con
    except Exception:
        con.close()
        raise


def extract_payload(root: Path) -> None:
    tar_bytes = gzip.decompress(PAYLOAD_PATH.read_bytes())
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
        for member in archive.getmembers():
            target = (root / member.name).resolve()
            if root.resolve() not in target.parents:
                raise AssertionError(f"fixture payload escapes root: {member.name}")
            if not member.isfile():
                raise AssertionError(f"fixture payload has non-file member: {member.name}")
        archive.extractall(root, filter="data")


def materialize_fixture(root: Path, fixture: dict) -> None:
    extract_payload(root)
    engine = root / ".super-coder"
    state = root / ".sc-state"
    (state / "local/dsh-removal").mkdir(parents=True)
    (engine / "run").mkdir(parents=True, exist_ok=True)
    (engine / "instance.json").write_text(
        json.dumps(fixture["instance"], sort_keys=True) + "\n"
    )
    (engine / "run/deepseek-web.json").write_text(
        json.dumps(fixture["runtime_state"], sort_keys=True) + "\n"
    )
    (state / "engine.ref").write_text(fixture["installed_engine_ref"] + "\n")
    control = fixture["compatibility_control"]
    if control["marker_present"]:
        marker = state / "local/dsh-removal/compatibility-floor.json"
        marker.write_text(
            json.dumps(
                {
                    "contract": "sc-dsh-compatibility-floor-v1",
                    "engine_ref": control["minimum_floor_ref"],
                    "pre_materialization_hook": control["pre_materialization_hook"],
                    "fresh_process_cleanup_hook": control["fresh_process_cleanup_hook"],
                },
                sort_keys=True,
            )
            + "\n"
        )


class DshRemovalPreparationTest(unittest.TestCase):
    def test_committed_inventory_and_fixtures_are_internally_exact(self) -> None:
        self.assertEqual([], FIXTURES.frozen_validation_errors())

    def test_manifest_freezes_disjoint_pre_removal_reference_partitions(self) -> None:
        manifest = load(MANIFEST_PATH)
        partitions = [
            manifest["tracked_artifacts"],
            manifest["shared_sources"],
            manifest["verification_sources"],
            manifest["immutable_reference_migrations"],
        ]
        paths = [row["path"] for group in partitions for row in group]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(26, len(manifest["tracked_artifacts"]))
        self.assertIn(
            ".super-coder/scripts/deepseek_web.py",
            {row["path"] for row in manifest["tracked_artifacts"]},
        )
        self.assertIn(
            ".super-coder/api/model_catalog.py",
            {row["path"] for row in manifest["shared_sources"]},
        )
        self.assertIn(
            ".dockerignore",
            {row["path"] for row in manifest["shared_sources"]},
        )
        self.assertEqual(
            [".dockerignore", ".gitignore"], manifest["scan"]["roots"][:2]
        )
        self.assertEqual(
            "committed-artifact-internal-consistency-only",
            manifest["freeze_policy"]["ci_validation"],
        )
        self.assertIn("--refresh", manifest["freeze_policy"]["refresh_command"])
        self.assertIn("forbidden", manifest["freeze_policy"]["refresh_boundary"])
        self.assertNotIn(
            ".super-coder/migrations/0227_deepseek_controlled_route_binding.sql",
            {row["path"] for row in manifest["tracked_artifacts"]},
        )

    def test_payload_contains_only_exact_digest_owned_artifacts(self) -> None:
        manifest = load(MANIFEST_PATH)
        expected = {row["path"]: row["sha256"] for row in manifest["tracked_artifacts"]}
        tar_bytes = gzip.decompress(PAYLOAD_PATH.read_bytes())
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
            members = archive.getmembers()
            actual = {
                member.name: FIXTURES.sha256_bytes(archive.extractfile(member).read())
                for member in members
            }
        self.assertEqual(expected, actual)
        self.assertEqual([], [member.name for member in members if not member.isfile()])
        self.assertEqual(
            FIXTURES.sha256_bytes(PAYLOAD_PATH.read_bytes()),
            load(PRE_BRIDGE_PATH)["tracked_payload"]["sha256"],
        )

    def test_refresh_is_rejected_after_frozen_inputs_change(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            frozen = root / "frozen.py"
            frozen.write_text("old\n")
            manifest = {
                "tracked_artifacts": [
                    {
                        "path": "frozen.py",
                        "sha256": FIXTURES.sha256_file(frozen),
                    }
                ],
                "shared_sources": [],
                "verification_sources": [],
                "immutable_reference_migrations": [],
                "immutable_migration_ledger": [],
            }
            self.assertEqual([], FIXTURES.refresh_floor_errors(manifest, root=root))
            frozen.write_text("new\n")
            self.assertEqual(
                ["changed frozen input: frozen.py"],
                FIXTURES.refresh_floor_errors(manifest, root=root),
            )

    def test_migration_floor_pins_every_existing_file_and_dsh_history(self) -> None:
        manifest = load(MANIFEST_PATH)
        ledger = manifest["immutable_migration_ledger"]
        self.assertEqual(184, len(ledger))
        self.assertEqual("0001_seed_skills.sql", ledger[0]["filename"])
        self.assertEqual("0236_live_native_conversation_routes.sql", ledger[-1]["filename"])
        self.assertEqual(
            {
                "0227_deepseek_controlled_route_binding.sql",
                "0230_deepseek_stock_host_route_binding.sql",
                "0235_live_native_route_binding_v3.sql",
                "0236_live_native_conversation_routes.sql",
            },
            {
                Path(row["path"]).name
                for row in manifest["immutable_reference_migrations"]
                if row["disposition"] == "preserve-dsh-history"
            },
        )
        self.assertEqual(
            [],
            [
                row["filename"]
                for row in ledger
                if FIXTURES.sha256_file(ROOT / row["path"]) != row["sha256"]
            ],
        )

    def test_fresh_replay_and_both_installed_floors_have_dirty_exact_state(self) -> None:
        pre_bridge = load(PRE_BRIDGE_PATH)
        compatibility = load(COMPATIBILITY_PATH)
        self.assertEqual(pre_bridge["database_rows"], compatibility["database_rows"])
        for fixture in (pre_bridge, compatibility):
            with closing(replay_database(fixture)) as con:
                self.assertEqual([], con.execute("PRAGMA foreign_key_check").fetchall())
                self.assertEqual(
                    [
                        ("deepseek", "deepseek-official/deepseek-chat"),
                        ("opencode", "ollama-cloud/deepseek-v4-pro"),
                    ],
                    [
                        tuple(row)
                        for row in con.execute(
                            "SELECT harness,selector FROM model_routes "
                            "WHERE harness IN ('deepseek','opencode') ORDER BY harness"
                        )
                    ],
                )
                self.assertEqual(
                    ("running", 4242, 31337, "preserve this normalized DSH prompt"),
                    tuple(
                        con.execute(
                            "SELECT r.state,r.process_pid,r.process_start_ticks,m.body "
                            "FROM conversation_runs r JOIN conversation_messages m "
                            "ON m.message_id=r.trigger_message_id WHERE r.run_id=9001"
                        ).fetchone()
                    ),
                )
                self.assertEqual(
                    ("deepseek", 9001, "deepseek"),
                    tuple(
                        con.execute(
                            "SELECT p.harness,p.active_route_binding_id,b.harness "
                            "FROM sprint_participants p "
                            "JOIN sprint_participant_route_bindings b "
                            "ON b.binding_id=p.active_route_binding_id "
                            "WHERE p.participant_id=9001"
                        ).fetchone()
                    ),
                )
                self.assertEqual(
                    "retain OpenCode model access",
                    con.execute(
                        "SELECT body FROM conversation_messages WHERE message_id=9002"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    0,
                    con.execute(
                        "SELECT COUNT(*) FROM model_routes WHERE harness='codex' "
                        "AND selector='deepseek-official/deepseek-chat'"
                    ).fetchone()[0],
                )

    def test_installed_floors_pin_expected_control_and_identity_differences(self) -> None:
        pre_bridge = load(PRE_BRIDGE_PATH)
        compatibility = load(COMPATIBILITY_PATH)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pre_root = root / "pre"
            compat_root = root / "compat"
            pre_root.mkdir()
            compat_root.mkdir()
            materialize_fixture(pre_root, pre_bridge)
            materialize_fixture(compat_root, compatibility)
            marker = Path(".sc-state/local/dsh-removal/compatibility-floor.json")
            installed_manifest = Path(
                ".super-coder/assets/dsh-removal/removal-manifest-v1.json"
            )
            self.assertFalse((pre_root / marker).exists())
            self.assertFalse((pre_root / installed_manifest).exists())
            self.assertFalse((compat_root / installed_manifest).exists())
            self.assertNotEqual(
                pre_bridge["installed_engine_ref"],
                compatibility["installed_engine_ref"],
            )
            self.assertEqual(
                {
                    "contract": "sc-dsh-compatibility-floor-v1",
                    "engine_ref": "2" * 40,
                    "fresh_process_cleanup_hook": True,
                    "pre_materialization_hook": True,
                },
                load(compat_root / marker),
            )
            manifest = load(MANIFEST_PATH)
            for artifact in manifest["tracked_artifacts"]:
                relative = artifact["path"]
                self.assertEqual(
                    artifact["sha256"], FIXTURES.sha256_file(pre_root / relative)
                )
                self.assertEqual(
                    artifact["sha256"], FIXTURES.sha256_file(compat_root / relative)
                )
            target = manifest["tracked_artifacts"][0]
            changed = compat_root / target["path"]
            changed.write_bytes(changed.read_bytes() + b"tampered")
            self.assertNotEqual(target["sha256"], FIXTURES.sha256_file(changed))
            self.assertEqual(target["sha256"], FIXTURES.sha256_file(pre_root / target["path"]))

    def test_runtime_inventory_names_every_owner_and_preserves_controls(self) -> None:
        manifest = load(MANIFEST_PATH)
        self.assertEqual(
            {"stock-dsh-host", "super-coder-dsh-relay"},
            {row["name"] for row in manifest["processes"]},
        )
        self.assertEqual(
            {"public", "private-host-bare-metal", "private-relay-sandbox"},
            {row["name"] for row in manifest["ports"]},
        )
        self.assertEqual(
            {
                "flavor_defaults",
                "model_routes",
                "model_catalog_generations",
                "analytics_parse_cache",
                "conversations",
                "conversation_runs",
                "conversation_outbox",
                "active_shell_chats",
                "sprint_participants",
                "sprint_participant_route_bindings",
            },
            {row["table"] for row in manifest["active_state_owners"]},
        )
        generated = {row["path"] for row in manifest["generated_artifacts"]}
        self.assertIn(".super-coder/run/deepseek-identity/<fork-id>", generated)
        self.assertIn(".super-coder/run/deepseek-web.json", generated)
        self.assertIn(".super-coder/logs/deepseek-web.log", generated)
        self.assertIn("~/.dsh", manifest["external_preserve"])
        self.assertIn(
            "OpenCode-owned DeepSeek-family selectors and native-option identity",
            manifest["retained_controls"],
        )


if __name__ == "__main__":
    unittest.main()
