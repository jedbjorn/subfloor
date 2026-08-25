"""Transactional per-fork DeepSeek identity registry and stock plugin tests."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Self

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCRIPTS = ENGINE / "scripts"
PLUGIN = ENGINE / "assets" / "deepseek" / "sc-shell-env-plugin.mjs"
PLUGIN_PROBE = ROOT / "tests" / "fixtures" / "deepseek_dsh_identity_plugin_probe.mjs"
sys.path.insert(0, str(SCRIPTS))

from deepseek_identity_registry import (
    ALIASES,
    HEALTH_CONTRACT,
    DeepSeekIdentityError,
    DeepSeekIdentityRegistry,
    SimulatedRegistryCrash,
    plugin_contract_generation,
)


def owner_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n")
    path.chmod(0o600)


def fake_repository(parent: Path, name: str) -> tuple[Path, Path]:
    repo = parent / name
    worktree = repo / ".sc-worktrees" / "dev"
    worktree.mkdir(parents=True)
    return repo, worktree


def registry_fixture(
    parent: Path, name: str = "fork"
) -> tuple[DeepSeekIdentityRegistry, Path]:
    repo, worktree = fake_repository(parent, name)
    registry = DeepSeekIdentityRegistry(
        repo_root=repo,
        runtime_root=parent / "identity-roots",
    )
    registry.materialize_profile()
    return registry, worktree


def synthetic_health(
    registry: DeepSeekIdentityRegistry,
    *,
    host_boot_generation: str = "host-boot-1",
    plugin_load_hmr_generation: str = "plugin-load-1",
) -> str:
    inputs = {
        "canonical_fork_id": registry.layout.fork_id,
        "dedicated_profile_id": registry.layout.profile_id,
        "plugin_bundle_digest": registry.plugin_digest,
        "declared_variable_schema_digest": registry.schema_digest,
        "canonical_registry_path_identity": registry.registry_path_identity,
        "host_boot_generation": host_boot_generation,
        "plugin_load_hmr_generation": plugin_load_hmr_generation,
    }
    generation = plugin_contract_generation(inputs)
    owner_json(
        registry.layout.health,
        {
            "contract": HEALTH_CONTRACT,
            "loaded": True,
            "fork_id": registry.layout.fork_id,
            "profile_id": registry.layout.profile_id,
            "registry_path": str(registry.layout.registry.resolve()),
            "host_boot_generation": host_boot_generation,
            "plugin_load_hmr_generation": plugin_load_hmr_generation,
            "plugin_contract_generation": generation,
            "registry_snapshot_generation": None,
            "binding_record_generation": None,
        },
    )
    return generation


def plugin_config(
    registry: DeepSeekIdentityRegistry,
    *,
    registry_path: Path | None = None,
    health_path: Path | None = None,
) -> dict[str, str]:
    return {
        "forkId": registry.layout.fork_id,
        "profileId": registry.layout.profile_id,
        "pluginBundleDigest": registry.plugin_digest,
        "declaredVariableSchemaDigest": registry.schema_digest,
        "registryPath": str((registry_path or registry.layout.registry).resolve()),
        "registryPathIdentity": registry.registry_path_identity,
        "healthPath": str((health_path or registry.layout.health).resolve()),
    }


class PluginProbe:
    def __init__(
        self,
        registry: DeepSeekIdentityRegistry,
        *,
        config: dict[str, str] | None = None,
        boot_generation: str | None = None,
    ) -> None:
        self.registry = registry
        self.config = config or plugin_config(registry)
        self.boot_generation = boot_generation or f"boot-{uuid.uuid4().hex}"
        self.process: subprocess.Popen[str] | None = None

    def __enter__(self) -> Self:
        self.process = subprocess.Popen(
            [
                "node",
                str(PLUGIN_PROBE),
                str(PLUGIN),
                json.dumps(self.config, separators=(",", ":")),
                self.boot_generation,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        ready = self._read()
        if ready != {"ready": True}:
            raise AssertionError(f"plugin probe did not become ready: {ready}")
        return self

    def _read(self) -> dict[str, object]:
        assert self.process is not None and self.process.stdout is not None
        line = self.process.stdout.readline()
        if not line:
            assert self.process.stderr is not None
            raise AssertionError(self.process.stderr.read())
        return json.loads(line)

    def request(self, value: dict[str, object]) -> dict[str, object]:
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()
        return self._read()

    def collect(self, session_id: str) -> dict[str, object]:
        return self.request({"session_id": session_id})

    def __exit__(self, *_exc: object) -> None:
        assert self.process is not None
        try:
            if self.process.poll() is None:
                assert self.request({"dispose": True}) == {"disposed": True}
        finally:
            if self.process.poll() is None:
                self.process.terminate()
            self.process.wait(timeout=5)


def create_binding(
    registry: DeepSeekIdentityRegistry,
    worktree: Path,
    *,
    session_id: str,
    conversation_id: str,
    shell_id: int,
    shortname: str,
    token: str,
    expected_snapshot_generation: int | None = None,
    crash_at: str | None = None,
):
    snapshot = registry.read_snapshot()
    expected = (
        snapshot["snapshot_generation"]
        if expected_snapshot_generation is None
        else expected_snapshot_generation
    )
    health = registry.read_live_health()
    return registry.create_binding(
        expected_snapshot_generation=expected,
        root_session_id=session_id,
        conversation_id=conversation_id,
        lifecycle_epoch=1,
        shell_id=shell_id,
        shell_shortname=shortname,
        shell_worktree=worktree,
        api_base="http://127.0.0.1:8837",
        token=token,
        plugin_contract_generation=health["plugin_contract_generation"],
        crash_at=crash_at,
    )


def wait_for(predicate, *, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition did not become true before timeout")


def test_profile_is_dedicated_per_fork_and_stock_composition_resolves() -> None:
    if shutil.which("dsh") is None:
        pytest.skip("pinned stock dsh is unavailable")
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        left, _ = registry_fixture(root, "fork-left")
        right, _ = registry_fixture(root, "fork-right")
        adapter = json.loads(
            (ENGINE / "adapters" / "deepseek" / "adapter.json").read_text()
        )
        assert adapter["official_runtime"]["profile_home"] == (
            "engine-owned per-fork DSH_HOME"
        )
        assert adapter["official_runtime"]["profile_contract"] == "sc-dsh-profile-v1"
        assert adapter["official_runtime"]["shell_identity_plugin"] == (
            "assets/deepseek/sc-shell-env-plugin.mjs"
        )
        assert adapter["official_runtime"]["registry_contract"] == (
            "sc-dsh-identity-registry-v1"
        )
        assert left.layout.fork_id != right.layout.fork_id
        assert left.layout.profile_id != right.layout.profile_id
        assert left.layout.registry != right.layout.registry
        assert left.layout.dsh_home != right.layout.dsh_home
        assert (left.layout.dsh_home / "cordis.patch.yml").read_text() == "[]\n"
        manifest = json.loads((left.layout.profile_dir / "package.json").read_text())
        assert manifest["dsh"]["profile"]["bundles"] == [
            "@deepseek-ai/dsh-base",
            "@deepseek-ai/dsh-web-app",
        ]
        env = os.environ.copy()
        env.update(left.host_environment())
        result = subprocess.run(
            ["dsh", "--profile", left.layout.profile_id, "--dump-config"],
            cwd=left.repo_root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0
        assert result.stderr == ""
        assert result.stdout.count("- id: sc-shell-identity") == 1
        assert PLUGIN.as_uri() in result.stdout
        assert str(Path.home() / ".dsh") not in result.stdout


def test_stock_host_plugin_health_hmr_removal_wrong_registry_and_restart() -> None:
    if shutil.which("dsh") is None:
        pytest.skip("pinned stock dsh is unavailable")
    with tempfile.TemporaryDirectory() as raw:
        registry, _worktree = registry_fixture(Path(raw))

        def start(boot: str, *, materialize: bool = True) -> subprocess.Popen[str]:
            env = os.environ.copy()
            if materialize:
                env.update(registry.materialize_profile())
            else:
                env["DSH_HOME"] = str(registry.layout.dsh_home)
            env["SC_DSH_HOST_BOOT_GENERATION"] = boot
            return subprocess.Popen(
                [
                    "dsh",
                    "--profile",
                    registry.layout.profile_id,
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--no-open",
                ],
                cwd=registry.repo_root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

        first = start("host-boot-one")
        try:
            wait_for(registry.layout.health.exists)
            initial = registry.read_live_health(
                expected_host_boot_generation="host-boot-one"
            )
            initial_generation = initial["plugin_contract_generation"]
            assert initial["loaded"] is True
            assert initial["registry_snapshot_generation"] is None

            (registry.layout.profile_dir / "cordis.patch.yml").write_text("[]\n")
            (registry.layout.profile_dir / "cordis.patch.yml").chmod(0o600)
            wait_for(
                lambda: (
                    json.loads(registry.layout.health.read_text()).get("loaded")
                    is False
                )
            )
            assert first.poll() is None

            registry.materialize_profile()
            wait_for(
                lambda: (
                    json.loads(registry.layout.health.read_text()).get("loaded") is True
                    and json.loads(registry.layout.health.read_text()).get(
                        "plugin_contract_generation"
                    )
                    != initial_generation
                )
            )
            reloaded = registry.read_live_health(
                expected_host_boot_generation="host-boot-one"
            )
            assert reloaded["plugin_contract_generation"] != initial_generation

        finally:
            first.terminate()
            first.wait(timeout=5)

        registry.materialize_profile()
        wrong_registry = registry.layout.root / "wrong-registry.json"
        owner_json(wrong_registry, registry.read_snapshot())
        patch = (registry.layout.profile_dir / "cordis.patch.yml").read_text()
        patch = patch.replace(
            str(registry.layout.registry.resolve()), str(wrong_registry.resolve())
        )
        (registry.layout.profile_dir / "cordis.patch.yml").write_text(patch)
        (registry.layout.profile_dir / "cordis.patch.yml").chmod(0o600)
        wrong = start("host-boot-wrong", materialize=False)
        try:
            wait_for(
                lambda: (
                    registry.layout.health.exists()
                    and json.loads(registry.layout.health.read_text()).get(
                        "host_boot_generation"
                    )
                    == "host-boot-wrong"
                )
            )
            with pytest.raises(DeepSeekIdentityError) as mismatch:
                registry.read_live_health(
                    expected_host_boot_generation="host-boot-wrong"
                )
            assert mismatch.value.code == "HARNESS_PLUGIN_HEALTH_MISMATCH"
            assert wrong.poll() is None
        finally:
            wrong.terminate()
            wrong.wait(timeout=5)

        second = start("host-boot-two")
        try:
            wait_for(
                lambda: (
                    registry.layout.health.exists()
                    and json.loads(registry.layout.health.read_text()).get(
                        "host_boot_generation"
                    )
                    == "host-boot-two"
                )
            )
            restarted = registry.read_live_health(
                expected_host_boot_generation="host-boot-two"
            )
            assert restarted["loaded"] is True
            assert restarted["plugin_contract_generation"] != initial_generation
        finally:
            second.terminate()
            second.wait(timeout=5)


def test_unaffected_live_binding_survives_other_binding_mutations() -> None:
    with tempfile.TemporaryDirectory() as raw:
        registry, worktree = registry_fixture(Path(raw))
        with PluginProbe(registry) as plugin:
            health = registry.read_live_health()
            contract_generation = health["plugin_contract_generation"]
            a = create_binding(
                registry,
                worktree,
                session_id="session-A",
                conversation_id="conversation-A",
                shell_id=101,
                shortname="SHELL_A",
                token="secret-token-A",
            )
            assert a.snapshot_generation == 1
            aliases_a = plugin.collect("session-A")["aliases"]
            assert aliases_a == {
                "DSH_SC_SHELL_ID": "101",
                "DSH_SC_SHELL_SHORTNAME": "SHELL_A",
                "DSH_SC_SHELL_WORKTREE": str(worktree.resolve()),
                "DSH_SC_API_BASE": "http://127.0.0.1:8837",
                "DSH_SC_MEM_CREDENTIAL_FILE": registry.read_snapshot()["records"][
                    "session-A"
                ]["credential_file"],
                "DSH_SC_BINDING_GENERATION": "1",
                "DSH_SC_PLUGIN_HEALTH_GENERATION": contract_generation,
            }

            b = create_binding(
                registry,
                worktree,
                session_id="session-B",
                conversation_id="conversation-B",
                shell_id=202,
                shortname="SHELL_B",
                token="secret-token-B",
            )
            assert b.snapshot_generation == 2
            assert plugin.collect("session-A")["aliases"] == aliases_a

            rotated = registry.rotate_binding(
                expected_snapshot_generation=2,
                root_session_id="session-B",
                expected_record_generation=1,
                token="secret-token-B-rotated",
                plugin_contract_generation=contract_generation,
            )
            assert (rotated.snapshot_generation, rotated.record_generation) == (3, 2)
            assert plugin.collect("session-A")["aliases"] == aliases_a
            cleaned = registry.cleanup_retired_artifacts(
                expected_snapshot_generation=3,
                root_session_id="session-B",
                expected_record_generation=2,
                quiesced=True,
            )
            assert (cleaned.snapshot_generation, cleaned.record_generation) == (4, 3)
            assert (
                plugin.collect("session-B")["aliases"]["DSH_SC_BINDING_GENERATION"]
                == "3"
            )
            assert len(list(registry.layout.credentials.glob("binding-*.json"))) == 2
            assert plugin.collect("session-A")["aliases"] == aliases_a

            closing = registry.begin_close(
                expected_snapshot_generation=4,
                root_session_id="session-B",
                expected_record_generation=3,
            )
            assert (closing.snapshot_generation, closing.state) == (5, "closing")
            assert plugin.collect("session-B") == {"aliases": {}}
            assert plugin.collect("session-A")["aliases"] == aliases_a

            terminal = registry.retire_binding(
                expected_snapshot_generation=5,
                root_session_id="session-B",
                expected_record_generation=4,
                quiesced=True,
            )
            assert (terminal.snapshot_generation, terminal.state) == (6, "terminal")
            assert plugin.collect("session-A")["aliases"] == aliases_a
            snapshot = registry.read_snapshot()
            assert snapshot["records"]["session-A"]["record_generation"] == 1
            assert snapshot["records"]["session-B"]["credential_file"] is None
            assert snapshot["records"]["session-B"]["record_generation"] == 5
            raw_registry = registry.layout.registry.read_text()
            assert "secret-token-A" not in raw_registry
            assert "secret-token-B" not in raw_registry
            diagnostics = json.dumps(registry.diagnostics(), sort_keys=True)
            assert "secret-token" not in diagnostics
            assert len(list(registry.layout.credentials.glob("binding-*.json"))) == 1


def test_concurrent_writers_refuse_stale_snapshot_without_lost_update() -> None:
    with tempfile.TemporaryDirectory() as raw:
        registry, worktree = registry_fixture(Path(raw))
        synthetic_health(registry)
        barrier = threading.Barrier(2)

        def writer(suffix: str) -> tuple[str, str]:
            barrier.wait(timeout=5)
            try:
                create_binding(
                    registry,
                    worktree,
                    session_id=f"session-{suffix}",
                    conversation_id=f"conversation-{suffix}",
                    shell_id=100 + ord(suffix),
                    shortname=f"SHELL_{suffix}",
                    token=f"secret-{suffix}",
                    expected_snapshot_generation=0,
                )
                return suffix, "committed"
            except DeepSeekIdentityError as exc:
                return suffix, exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(writer, ("A", "B")))
        assert sorted(value for _suffix, value in outcomes) == [
            "HARNESS_REGISTRY_STALE_WRITER",
            "committed",
        ]
        snapshot = registry.read_snapshot()
        assert snapshot["snapshot_generation"] == 1
        assert len(snapshot["records"]) == 1
        committed = next(suffix for suffix, value in outcomes if value == "committed")
        refused = next(suffix for suffix, value in outcomes if value != "committed")
        assert f"session-{committed}" in snapshot["records"]
        assert f"session-{refused}" not in snapshot["records"]
        assert len(list(registry.layout.credentials.glob("binding-*.json"))) == 1


@pytest.mark.parametrize(
    ("crash_at", "committed", "orphan_count"),
    [
        ("before_artifact_fsync", False, 0),
        ("after_artifact_fsync", False, 1),
        ("before_registry_replace", False, 1),
        ("after_registry_replace", True, 0),
    ],
)
def test_create_crash_boundaries_recover_one_authoritative_snapshot(
    crash_at: str, committed: bool, orphan_count: int
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        registry, worktree = registry_fixture(Path(raw))
        synthetic_health(registry)
        with pytest.raises(SimulatedRegistryCrash) as crash:
            create_binding(
                registry,
                worktree,
                session_id="session-crash",
                conversation_id="conversation-crash",
                shell_id=303,
                shortname="SHELL_CRASH",
                token="secret-crash",
                crash_at=crash_at,
            )
        assert str(crash.value) == crash_at
        snapshot = registry.read_snapshot()
        assert snapshot["snapshot_generation"] == (1 if committed else 0)
        assert ("session-crash" in snapshot["records"]) is committed
        recovery = registry.recover_artifacts()
        assert recovery["removed_orphans"] == orphan_count
        artifacts = list(registry.layout.credentials.glob("binding-*.json"))
        assert len(artifacts) == (1 if committed else 0)
        if committed:
            assert snapshot["records"]["session-crash"]["credential_file"] == str(
                artifacts[0]
            )


def test_closing_tombstone_crash_cleanup_and_reuse_refusal() -> None:
    with tempfile.TemporaryDirectory() as raw:
        registry, worktree = registry_fixture(Path(raw))
        synthetic_health(registry)
        create_binding(
            registry,
            worktree,
            session_id="session-close",
            conversation_id="conversation-close",
            shell_id=404,
            shortname="SHELL_CLOSE",
            token="secret-close",
        )
        with pytest.raises(SimulatedRegistryCrash):
            registry.begin_close(
                expected_snapshot_generation=1,
                root_session_id="session-close",
                expected_record_generation=1,
                crash_at="after_closing_update",
            )
        assert registry.read_snapshot()["records"]["session-close"]["state"] == "active"
        closing = registry.begin_close(
            expected_snapshot_generation=1,
            root_session_id="session-close",
            expected_record_generation=1,
        )
        assert closing.record_generation == 2
        with pytest.raises(DeepSeekIdentityError) as unknown:
            registry.retire_binding(
                expected_snapshot_generation=2,
                root_session_id="session-close",
                expected_record_generation=2,
                quiesced=False,
            )
        assert unknown.value.code == "HARNESS_BINDING_QUIESCENCE_UNKNOWN"
        assert len(list(registry.layout.credentials.glob("binding-*.json"))) == 1

        with pytest.raises(SimulatedRegistryCrash) as committed_crash:
            registry.retire_binding(
                expected_snapshot_generation=2,
                root_session_id="session-close",
                expected_record_generation=2,
                quiesced=True,
                crash_at="after_registry_replace",
            )
        assert str(committed_crash.value) == "after_registry_replace"
        terminal = registry.read_snapshot()["records"]["session-close"]
        assert terminal["state"] == "terminal"
        assert terminal["credential_file"] is None
        assert terminal["record_generation"] == 3
        assert registry.recover_artifacts() == {
            "snapshot_generation": 3,
            "removed_orphans": 1,
        }
        assert list(registry.layout.credentials.glob("binding-*.json")) == []
        with pytest.raises(DeepSeekIdentityError) as reused:
            create_binding(
                registry,
                worktree,
                session_id="session-close",
                conversation_id="conversation-close",
                shell_id=404,
                shortname="SHELL_CLOSE",
                token="new-secret",
            )
        assert reused.value.code == "HARNESS_BINDING_REUSE_REFUSED"
        assert registry.read_snapshot()["snapshot_generation"] == 3


def test_lineage_is_current_only_and_stale_children_refuse() -> None:
    with tempfile.TemporaryDirectory() as raw:
        registry, worktree = registry_fixture(Path(raw))
        with PluginProbe(registry) as plugin:
            generation = registry.read_live_health()["plugin_contract_generation"]
            create_binding(
                registry,
                worktree,
                session_id="root-session",
                conversation_id="conversation-lineage",
                shell_id=505,
                shortname="SHELL_LINEAGE",
                token="secret-lineage",
            )
            lineage = registry.register_lineage(
                expected_snapshot_generation=1,
                root_session_id="root-session",
                child_session_id="child-session",
                expected_record_generation=1,
            )
            assert (lineage.snapshot_generation, lineage.record_generation) == (2, 1)
            child = plugin.collect("child-session")["aliases"]
            assert child["DSH_SC_SHELL_SHORTNAME"] == "SHELL_LINEAGE"
            assert child["DSH_SC_BINDING_GENERATION"] == "1"
            registry.rotate_binding(
                expected_snapshot_generation=2,
                root_session_id="root-session",
                expected_record_generation=1,
                token="secret-lineage-rotated",
                plugin_contract_generation=generation,
            )
            refused = plugin.collect("child-session")
            assert refused == {"error": "sc-shell-identity: stale child lineage"}
            with pytest.raises(DeepSeekIdentityError) as stale:
                registry.resolve_record("child-session")
            assert stale.value.code == "HARNESS_LINEAGE_STALE"
            root = plugin.collect("root-session")["aliases"]
            assert root["DSH_SC_BINDING_GENERATION"] == "2"


def test_host_restart_blocks_old_contract_until_conditional_recovery() -> None:
    with tempfile.TemporaryDirectory() as raw:
        registry, worktree = registry_fixture(Path(raw))
        with PluginProbe(registry, boot_generation="host-one") as first:
            generation_one = registry.read_live_health()["plugin_contract_generation"]
            create_binding(
                registry,
                worktree,
                session_id="restart-session",
                conversation_id="restart-conversation",
                shell_id=606,
                shortname="SHELL_RESTART",
                token="secret-one",
            )
            assert (
                first.collect("restart-session")["aliases"][
                    "DSH_SC_PLUGIN_HEALTH_GENERATION"
                ]
                == generation_one
            )

        assert json.loads(registry.layout.health.read_text())["loaded"] is False
        with PluginProbe(registry, boot_generation="host-two") as second:
            generation_two = registry.read_live_health()["plugin_contract_generation"]
            assert generation_two != generation_one
            assert second.collect("restart-session") == {"aliases": {}}
            recovered = registry.rotate_binding(
                expected_snapshot_generation=1,
                root_session_id="restart-session",
                expected_record_generation=1,
                token="secret-two",
                plugin_contract_generation=generation_two,
                recovery=True,
            )
            assert (recovered.snapshot_generation, recovered.record_generation) == (
                2,
                2,
            )
            aliases = second.collect("restart-session")["aliases"]
            assert aliases["DSH_SC_PLUGIN_HEALTH_GENERATION"] == generation_two
            assert aliases["DSH_SC_BINDING_GENERATION"] == "2"
            snapshot = registry.read_snapshot()
            assert snapshot["records"]["restart-session"]["recovered_at"] is not None
            assert "secret-one" not in registry.layout.registry.read_text()
            assert "secret-two" not in registry.layout.registry.read_text()


def test_two_forks_same_user_isolate_profiles_registries_and_credentials() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        left, left_worktree = registry_fixture(root, "left")
        right, right_worktree = registry_fixture(root, "right")
        with PluginProbe(left) as left_plugin, PluginProbe(right) as right_plugin:
            create_binding(
                left,
                left_worktree,
                session_id="same-session",
                conversation_id="left-conversation",
                shell_id=701,
                shortname="LEFT",
                token="left-secret",
            )
            create_binding(
                right,
                right_worktree,
                session_id="same-session",
                conversation_id="right-conversation",
                shell_id=702,
                shortname="RIGHT",
                token="right-secret",
            )
            left_aliases = left_plugin.collect("same-session")["aliases"]
            right_aliases = right_plugin.collect("same-session")["aliases"]
            assert left_aliases["DSH_SC_SHELL_SHORTNAME"] == "LEFT"
            assert right_aliases["DSH_SC_SHELL_SHORTNAME"] == "RIGHT"
            assert (
                left_aliases["DSH_SC_MEM_CREDENTIAL_FILE"]
                != right_aliases["DSH_SC_MEM_CREDENTIAL_FILE"]
            )
            assert Path(left_aliases["DSH_SC_MEM_CREDENTIAL_FILE"]).is_relative_to(
                left.layout.root
            )
            assert Path(right_aliases["DSH_SC_MEM_CREDENTIAL_FILE"]).is_relative_to(
                right.layout.root
            )

        wrong_health = left.layout.root / "wrong-health.json"
        wrong_config = plugin_config(
            left,
            registry_path=right.layout.registry,
            health_path=wrong_health,
        )
        with PluginProbe(left, config=wrong_config) as wrong:
            refused = wrong.collect("same-session")
            assert refused == {
                "error": "sc-shell-identity: registry identity or schema mismatch"
            }
            health = json.loads(wrong_health.read_text())
            assert health["registry_path"] == str(right.layout.registry.resolve())
            assert "left-secret" not in json.dumps(health)
            assert "right-secret" not in json.dumps(health)


def test_artifact_permissions_and_alias_schema_are_exact() -> None:
    with tempfile.TemporaryDirectory() as raw:
        registry, worktree = registry_fixture(Path(raw))
        with PluginProbe(registry) as plugin:
            create_binding(
                registry,
                worktree,
                session_id="permission-session",
                conversation_id="permission-conversation",
                shell_id=808,
                shortname="PERMISSIONS",
                token="permission-secret",
            )
            aliases = plugin.collect("permission-session")["aliases"]
            assert tuple(aliases) == ALIASES
            credential = Path(aliases["DSH_SC_MEM_CREDENTIAL_FILE"])
            assert stat.S_IMODE(credential.stat().st_mode) == 0o600
            assert stat.S_IMODE(registry.layout.registry.stat().st_mode) == 0o600
            assert stat.S_IMODE(registry.layout.lock.stat().st_mode) == 0o600
            assert stat.S_IMODE(registry.layout.credentials.stat().st_mode) == 0o700
            assert credential.is_symlink() is False
            credential_payload = json.loads(credential.read_text())
            assert credential_payload["token"] == "permission-secret"
            assert credential_payload["binding_generation"] == 1
            assert credential_payload["shell_id"] == 808
            assert "permission-secret" not in registry.layout.registry.read_text()
