import ast
from collections import Counter
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / ".super-coder/assets/deepseek/dsh-shell-authority-contract.json"
NODE_PROBE = ROOT / "tests/fixtures/deepseek_dsh_shell_env_probe.mjs"
SOURCE_ROOTS = (ROOT / ".super-coder/scripts", ROOT / ".super-coder/api")


def load_contract():
    return json.loads(CONTRACT_PATH.read_text())


def dispatcher_patterns(source):
    marker = '\ncase "$cmd" in\n'
    start = source.rfind(marker)
    if start < 0:
        raise AssertionError("final dispatcher case not found")
    return set(re.findall(r"^  ([A-Za-z0-9*?_|-]+)\)", source[start:], re.MULTILINE))


def literal_subparser_counter(path):
    tree = ast.parse(path.read_text())
    values = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "add_parser":
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if isinstance(node.args[0].value, str):
            values.append(node.args[0].value)
    return Counter(values)


def compared_command_literals(path, names):
    tree = ast.parse(path.read_text())
    values = set()

    def is_subject(node):
        if isinstance(node, ast.Name):
            return node.id in names
        return (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id in names
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == 0
        )

    def strings(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return {
                item.value for item in node.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            }
        return set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or not is_subject(node.left):
            continue
        for comparator in node.comparators:
            values.update(strings(comparator))
    return values - {"-h", "--help", "help"}


def dynamic_for_subcommands(path, target_name):
    tree = ast.parse(path.read_text())
    values = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.For) or not isinstance(node.target, ast.Tuple):
            continue
        if not node.target.elts or not isinstance(node.target.elts[0], ast.Name):
            continue
        if node.target.elts[0].id != target_name or not isinstance(node.iter, ast.Tuple):
            continue
        for row in node.iter.elts:
            if (
                isinstance(row, ast.Tuple)
                and row.elts
                and isinstance(row.elts[0], ast.Constant)
                and isinstance(row.elts[0].value, str)
            ):
                values.add(row.elts[0].value)
    return values


def shell_map_subcommands(source):
    start = source.index('  map)          case "${1:-}" in')
    end = source.index("                esac", start)
    labels = set(re.findall(r"^\s+([?A-Za-z0-9_|-]+)\)", source[start:end], re.MULTILINE))
    return labels - {"map", "-h|--help", "?*"}


def shell_enter_subcommands(source):
    start = source.index("  enter)\n")
    end = source.index("  enter-*)", start)
    return {
        value
        for value in re.findall(r'= "([A-Za-z0-9_-]+)" \]; then', source[start:end])
        if not value.startswith("-")
    }


def source_files():
    for root in SOURCE_ROOTS:
        for path in root.rglob("*"):
            if path.suffix in {".py", ".sh"}:
                yield path
    yield ROOT / "sc"


def paths_matching(pattern):
    matcher = re.compile(pattern)
    return sorted(
        str(path.relative_to(ROOT))
        for path in source_files()
        if matcher.search(path.read_text())
    )


def resolve_policy(
    contract, *, provenance, environment, command_class,
    admin_artifact_discoverable=False,
):
    bridge_names = {
        key for key in environment
        if key == "DSH_SHELL" or key.startswith("DSH_SC_")
    }
    probes = {name: 0 for name in contract["effect_probes"]}
    if provenance not in {"managed", "native"}:
        return "refused", probes
    if provenance == "native":
        return ("refused", probes) if bridge_names else ("native", probes)
    if command_class == "identity_neutral_read_only":
        return "neutral", probes
    if command_class == "refused":
        return "refused", probes
    aliases = set(contract["aliases"])
    if set(environment) & aliases != aliases:
        return "refused", probes
    if environment.get("DSH_SHELL") != "1":
        return "refused", probes
    return "authorized", probes


class DeepSeekDshPreparationContractTests(unittest.TestCase):
    def setUp(self):
        self.contract = load_contract()

    def test_release_alias_health_and_currency_contract_is_exact(self):
        adapter = json.loads(
            (ROOT / ".super-coder/adapters/deepseek/adapter.json").read_text()
        )
        release = self.contract["dsh_release"]
        self.assertEqual(adapter["official_runtime"]["version"], release["version"])
        self.assertEqual(adapter["official_runtime"]["tag"], release["tag"])
        self.assertEqual(adapter["official_runtime"]["commit"], release["commit"])
        self.assertEqual(
            adapter["official_runtime"]["shell_authority_contract"],
            "assets/deepseek/dsh-shell-authority-contract.json",
        )
        self.assertEqual(len(self.contract["supported_seams"]), 8)
        self.assertEqual(
            self.contract["aliases"],
            [
                "DSH_SC_SHELL_ID",
                "DSH_SC_SHELL_SHORTNAME",
                "DSH_SC_SHELL_WORKTREE",
                "DSH_SC_API_BASE",
                "DSH_SC_MEM_CREDENTIAL_FILE",
                "DSH_SC_BINDING_GENERATION",
                "DSH_SC_PLUGIN_HEALTH_GENERATION",
            ],
        )
        generation_inputs = self.contract["plugin_contract_generation_inputs"]
        self.assertEqual(
            generation_inputs,
            [
                "canonical_fork_id",
                "dedicated_profile_id",
                "plugin_bundle_digest",
                "declared_variable_schema_digest",
                "canonical_registry_path_identity",
                "host_boot_generation",
                "plugin_load_hmr_generation",
            ],
        )
        self.assertNotIn("registry_snapshot_generation", generation_inputs)
        self.assertNotIn("binding_record_generation", generation_inputs)
        self.assertEqual(
            set(self.contract["registry_currency"]),
            {"registry_snapshot_generation", "binding_record_generation"},
        )
        self.assertEqual(
            self.contract["containment_baseline"]["production_state"],
            "serialized",
        )
        self.assertIn(
            "server-minted exact-ref capability",
            self.contract["containment_baseline"]["proof_authority"],
        )
        self.assertNotIn(
            "registry_snapshot_generation",
            self.contract["containment_baseline"]["global_identity_lease"],
        )

    def test_every_public_dispatch_pattern_has_one_classification(self):
        policy = self.contract["public_command_policy"]
        groups = [
            policy["identity_neutral_read_only"],
            policy["dsh_shell_authorized"],
            policy["refused"],
        ]
        flat = [item for group in groups for item in group]
        self.assertEqual(len(flat), len(set(flat)), "a dispatcher route has two policies")
        expected = set(flat) | set(policy["selector_policies"]) | {"*"}
        observed = dispatcher_patterns(
            (ROOT / ".super-coder/scripts/dispatch.sh").read_text()
        )
        self.assertEqual(observed, expected)

        mutated = (
            (ROOT / ".super-coder/scripts/dispatch.sh").read_text()
            + '\ncase "$cmd" in\n  unclassified-new-command) : ;;\nesac\n'
        )
        self.assertNotEqual(dispatcher_patterns(mutated), expected)

    def test_literal_subcommands_are_drift_detected(self):
        for relative, expected in self.contract["literal_subparser_counters"].items():
            self.assertEqual(
                literal_subparser_counter(ROOT / relative),
                Counter(expected),
                relative,
            )

        custom = {
            "feature": compared_command_literals(
                ROOT / ".super-coder/scripts/feature.py", {"cmd"}
            ),
            "models": compared_command_literals(
                ROOT / ".super-coder/scripts/models.py", {"args", "command"}
            ),
            "skill": compared_command_literals(
                ROOT / ".super-coder/scripts/skill.py", {"cmd"}
            ),
            "analytics": compared_command_literals(
                ROOT / ".super-coder/scripts/analytics.py", {"argv"}
            ),
            "artifact-mode": compared_command_literals(
                ROOT / ".super-coder/scripts/artifact_policy.py", {"command"}
            ),
            "map-extractor": compared_command_literals(
                ROOT / ".super-coder/scripts/map_extractor_install.py", {"argv"}
            ),
            "map": shell_map_subcommands(
                (ROOT / ".super-coder/scripts/dispatch.sh").read_text()
            ),
            "enter": shell_enter_subcommands(
                (ROOT / ".super-coder/scripts/dispatch.sh").read_text()
            ),
            "vm-mcp-relay": compared_command_literals(
                ROOT / ".super-coder/scripts/vm_mcp_relay.py", {"mode"}
            ),
            "vm.mcp": dynamic_for_subcommands(
                ROOT / ".super-coder/scripts/vm.py", "action"
            ),
            "mem.dynamic": dynamic_for_subcommands(
                ROOT / ".super-coder/scripts/mem.py", "k"
            ),
        }
        self.assertEqual(
            custom,
            {
                surface: set(subcommands)
                for surface, subcommands in self.contract["custom_subcommands"].items()
            },
        )

    def test_every_direct_identity_consumer_is_inventoried(self):
        patterns = {
            "SC_API_TOKEN": r"\bSC_API_TOKEN\b",
            "SC_API_BASE": r"\bSC_API_BASE\b",
            "SC_MEM_CREDENTIAL_FILE": r"\bSC_MEM_CREDENTIAL_FILE\b",
            "SC_SHELL_ID": r"\bSC_SHELL_ID\b",
            "SC_SHELL_SHORTNAME": r"\bSC_SHELL_SHORTNAME\b",
            "SC_SHELL_WORKTREE": r"\bSC_SHELL_WORKTREE\b",
            "SC_ADMIN": r"\bSC_ADMIN\b",
            "credential_discovery": r"\b_?discover_runtime_credential\b",
        }
        for name, pattern in patterns.items():
            self.assertEqual(
                paths_matching(pattern),
                self.contract["direct_identity_signal_inventory"][name],
                name,
            )
        self.assertNotIn(
            ".super-coder/scripts/new_identity_consumer.py",
            self.contract["direct_identity_signal_inventory"]["SC_API_TOKEN"],
        )

    def test_direct_effect_consumers_are_drift_detected(self):
        patterns = {
            "direct_db": r"sqlite3\.connect|exec sqlite3",
            "credential_artifact": (
                r"mem_credentials|runtime_credential|credential_file|"
                r"SC_MEM_CREDENTIAL_FILE"
            ),
            "api_effect": r"urlopen\(|urllib\.request|requests\.|_api\(|/_sc/|/api/",
            "filesystem_effect": (
                r"\.write_text\(|\.write_bytes\(|\.unlink\(|os\.replace\(|"
                r"os\.remove\(|shutil\.(?:copy|move|rmtree)\(|"
                r"open\([^\n]*,[^\n]*[\"'](?:w|a|x)"
            ),
            "process_effect": (
                r"subprocess\.(?:run|Popen|call|check_call|check_output)\(|"
                r"os\.exec|exec \"?\$|docker |curl "
            ),
        }
        for name, pattern in patterns.items():
            self.assertEqual(
                paths_matching(pattern),
                self.contract["direct_effect_signal_inventory"][name],
                name,
            )

    def test_execution_provenance_survives_marker_and_alias_mutation(self):
        aliases = {name: f"value-{index}" for index, name in enumerate(self.contract["aliases"])}
        exact = {"DSH_SHELL": "1", **aliases}
        cases = [
            ("managed", {}, "dsh_shell_authorized", "refused"),
            ("managed", {"DSH_SHELL": "0"}, "dsh_shell_authorized", "refused"),
            ("managed", exact, "dsh_shell_authorized", "authorized"),
            ("managed", {}, "identity_neutral_read_only", "neutral"),
            ("managed", exact, "refused", "refused"),
            ("native", {"DSH_SHELL": "1"}, "dsh_shell_authorized", "refused"),
            ("native", aliases, "dsh_shell_authorized", "refused"),
            ("native", {}, "dsh_shell_authorized", "native"),
            ("unknown", {}, "dsh_shell_authorized", "refused"),
        ]
        for provenance, environment, command_class, expected in cases:
            with self.subTest(provenance=provenance, environment=environment):
                decision, probes = resolve_policy(
                    self.contract,
                    provenance=provenance,
                    environment=environment,
                    command_class=command_class,
                )
                self.assertEqual(decision, expected)
                if decision in {"refused", "neutral"}:
                    self.assertEqual(set(probes.values()), {0})

        decision, probes = resolve_policy(
            self.contract,
            provenance="managed",
            environment={},
            command_class="dsh_shell_authorized",
            admin_artifact_discoverable=True,
        )
        self.assertEqual(decision, "refused")
        self.assertEqual(probes["credential_discovery"], 0)
        self.assertEqual(probes["api_read"], 0)
        self.assertEqual(probes["db_read"], 0)
        self.assertEqual(probes["filesystem_read"], 0)
        self.assertEqual(probes["process_start"], 0)
        self.assertEqual(probes["message_write"], 0)
        self.assertEqual(probes["wake_write"], 0)
        self.assertEqual(probes["denied_marker_write"], 0)

    def test_real_pinned_dsh_components_reproduce_clean_room_fixture(self):
        dsh = shutil.which("dsh")
        self.assertIsNotNone(dsh, "pinned dsh is required for the executable fixture")
        package_root = Path(dsh).resolve().parents[1]
        completed = subprocess.run(
            ["node", str(NODE_PROBE), str(package_root)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            env=os.environ.copy(),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        receipt = json.loads(completed.stdout)
        self.assertEqual(receipt["contract"], "dsh-shell-env-clean-room-fixture-v1")
        self.assertEqual(set(receipt["versions"].values()), {"0.1.1-rc.2"})
        self.assertEqual(receipt["powershellParity"], "source-contract-passed")


if __name__ == "__main__":
    unittest.main()
