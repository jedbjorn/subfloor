import ast
import ctypes
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import urllib.request
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / ".super-coder/assets/deepseek/dsh-shell-authority-contract.json"
NODE_PROBE = ROOT / "tests/fixtures/deepseek_dsh_shell_env_probe.mjs"
INVENTORY_PROBE = ROOT / ".super-coder/scripts/dsh_preparation_inventory.py"
PROVENANCE_PROBE = ROOT / ".super-coder/scripts/dsh_execution_provenance.py"
WINDOWS_PROBE = ROOT / "tests/fixtures/deepseek_dsh_job_object_probe.ps1"
EFFECT_DRIVER = ROOT / "tests/fixtures/deepseek_dsh_effect_driver.py"
SOURCE_ROOTS = (ROOT / ".super-coder/scripts", ROOT / ".super-coder/api")
REQUIRED_SEALS = (
    fcntl.F_SEAL_SEAL
    | fcntl.F_SEAL_SHRINK
    | fcntl.F_SEAL_GROW
    | fcntl.F_SEAL_WRITE
)


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


def generated_inventory(root=ROOT):
    completed = subprocess.run(
        ["python3", str(INVENTORY_PROBE), "--root", str(root)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def linux_domain_fixture(kind):
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        proc = root / "proc-cgroup"
        cgroup_root = root / "cgroup"
        cgroup_root.mkdir()
        now = 1_000_000
        descriptor_fd = None
        domain_id = "a" * 32
        membership = f"/sc-dsh/{domain_id}.scope"
        if kind == "missing":
            pass
        elif kind == "unreadable":
            proc.mkdir()
        elif kind == "ambiguous":
            proc.write_text("0::/\n0::/other\n")
        elif kind == "native":
            proc.write_text("0::/native.slice\n")
        else:
            proc.write_text(f"0::{membership}\n")
            domain = cgroup_root / membership.lstrip("/")
            domain.mkdir(parents=True, mode=0o700)
            (domain / "cgroup.type").write_text("domain\n")
            (domain / "cgroup.subtree_control").write_text("\n")
            if kind != "no-descriptor":
                descriptor_fd = os.memfd_create(
                    "sc-dsh-domain",
                    os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
                )
                descriptor = {
                    "contract": "sc-dsh-linux-cgroup-v2-v1",
                    "cgroup": membership,
                    "domain_id": domain_id,
                    "binding_generation": 7,
                    "expires_monotonic_ns": now if kind == "stale" else now + 1,
                    "non_delegated": kind != "delegated",
                }
                os.write(descriptor_fd, json.dumps(descriptor).encode())
                fcntl.fcntl(descriptor_fd, fcntl.F_ADD_SEALS, REQUIRED_SEALS)
        try:
            yield {
                "proc": proc,
                "cgroup_root": cgroup_root,
                "descriptor_fd": descriptor_fd,
                "now": now,
            }
        finally:
            if descriptor_fd is not None:
                os.close(descriptor_fd)


def clean_probe_environment(extra=None):
    environment = {
        name: value for name, value in os.environ.items()
        if not name.startswith("DSH_") and not name.startswith("SC_")
    }
    if extra:
        environment.update(extra)
    return environment


def run_provenance_probe(fixture, *, environment=None, extra_args=()):
    command = [
        "python3",
        str(PROVENANCE_PROBE),
        "--proc-cgroup",
        str(fixture["proc"]),
        "--cgroup-root",
        str(fixture["cgroup_root"]),
        "--now-monotonic-ns",
        str(fixture["now"]),
    ]
    pass_fds = ()
    if fixture["descriptor_fd"] is not None:
        command.extend(["--descriptor-fd", str(fixture["descriptor_fd"])])
        pass_fds = (fixture["descriptor_fd"],)
    command.extend(extra_args)
    return subprocess.run(
        command,
        cwd=ROOT,
        env=clean_probe_environment(environment),
        pass_fds=pass_fds,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


@contextmanager
def access_watch(*paths):
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "inotify_init1")
    try:
        for path in paths:
            watch = libc.inotify_add_watch(
                fd,
                os.fsencode(path),
                0x00000001 | 0x00000020,
            )
            if watch < 0:
                raise OSError(ctypes.get_errno(), f"inotify_add_watch {path}")
        yield fd
    finally:
        os.close(fd)


def watched_events(fd):
    try:
        return os.read(fd, 65_536)
    except BlockingIOError:
        return b""


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
        self.assertEqual(self.contract["schema_version"], 2)
        self.assertEqual(
            {
                seam["name"]: (
                    seam["source"], seam["source_sha256"], seam["contract"]
                )
                for seam in self.contract["supported_seams"]
            },
            {
                "profile-composition": (
                    "apps/cli/src/profile-boot.ts",
                    "4a89a793d0a793e7573d7b275d9682459fa2e211edc6814d23b90c75588fa663",
                    "bundle layers, dedicated profile patch, home patch, explicit overlays, then telemetry; production must use an engine-owned DSH_HOME so no shared home patch participates",
                ),
                "per-execution-shell-env": (
                    "packages/shell/shell-env/src/index.ts",
                    "a9347895a868d3a8928042833f253165c02e7f187e39593259cf164e2bae88c1",
                    "ShellEnvRegistry.register declares owned DSH_* names and collect(execution) resolves a fresh immutable snapshot",
                ),
                "ambient-scrub": (
                    "packages/subprocess/subprocess/src/index.ts",
                    "0498a406106a51e34ac24e2e752dde67bb3c5c623333599e2af1805a30ca5499",
                    "scrubbedParentEnv removes every ambient DSH_* and credential-shaped KEY/PASSWORD/SECRET/TOKEN name",
                ),
                "subprocess-merge": (
                    "packages/subprocess/subprocess-local/src/spawn.ts",
                    "3038096134defaf03f767b94fdcbb65c7103f60448f5973201732c93def72d71",
                    "explicit caller environment is merged after scrubbedParentEnv for foreground, background, and terminal children",
                ),
                "bash-collection": (
                    "packages/shell/tool-bash/src/index.ts",
                    "e0302d4cc1d835ca4118434afc9913c4236773bbce92349ae7644d540d40c482",
                    "ctx.shellEnv.collect(exec) is called once per foreground or background ToolExecution",
                ),
                "bash-dispatch": (
                    "packages/shell/bash-local/src/index.ts",
                    "c501ca1e9164642f1291c0e40b3a7226f6079af847aa8c22b4fda52fbf692d39",
                    "spec.dshEnv is merged last into the explicit spawn environment for run and start",
                ),
                "powershell-collection": (
                    "packages/shell/tool-pwsh/src/index.ts",
                    "189024974fdb0d15605a96973aad2b8a4bdaf8c5fdaf4bbe7c2465794cf65e67",
                    "ctx.shellEnv.collect(exec) supplies the same snapshot for PowerShell foreground and background ToolExecution",
                ),
                "powershell-dispatch": (
                    "packages/shell/pwsh-local/src/index.ts",
                    "a9f00ecb6b4394549792952f7f9ef344352236a4e225d28e2945331422bb864b",
                    "spec.dshEnv is merged last into the PowerShell spawn environment",
                ),
            },
        )
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
        provenance = self.contract["execution_provenance"]
        self.assertEqual(
            provenance["managed_rule"],
            "domain membership selects DSH policy even when DSH_SHELL and every DSH_SC_* alias were deleted or falsified",
        )
        self.assertEqual(
            provenance["native_rule"],
            "no domain plus no DSH_* bridge names selects the unchanged native path",
        )
        self.assertEqual(
            provenance["spoof_rule"],
            "no domain plus any DSH_SHELL or DSH_SC_* name exits 77 before effect",
        )
        self.assertEqual(
            provenance["unknown_rule"],
            "missing, ambiguous, stale, breakaway, or unreadable domain evidence exits 77 before effect",
        )
        self.assertEqual(
            provenance["admin_artifact_rule"],
            "managed, spoofed, and unknown decisions never inspect runtime credential artifacts or attempt Admin discovery",
        )
        self.assertEqual(
            provenance["linux_contributor"],
            {
                "path": ".super-coder/scripts/dsh_execution_provenance.py",
                "sha256": sha256(PROVENANCE_PROBE),
                "contract": "sc-dsh-linux-cgroup-v2-v1",
            },
        )
        self.assertEqual(
            provenance["windows_contributor"]["sha256"],
            sha256(WINDOWS_PROBE),
        )
        self.assertEqual(
            provenance["bootstrap_order"][3],
            "under managed membership refuse SC_DISPATCH, SC_CALLER_ROOT, "
            "SC_MEM_AS, and every ambient SC_* before target or credential inspection",
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

    def test_complete_source_sc_signal_and_effect_inventory_is_exact(self):
        observed = generated_inventory()
        for name in (
            "source_sha256_inventory",
            "direct_sc_signal_inventory",
            "ambient_sc_policy",
            "literal_subparser_counters",
            "effect_call_vocabulary",
            "direct_effect_signal_inventory",
            "risky_call_inventory",
            "effect_detector_vocabulary",
        ):
            self.assertEqual(observed[name], self.contract[name], name)

        policy = self.contract["ambient_sc_policy"]
        groups = [
            set(policy[name])
            for name in (
                "pre_provenance_refused",
                "credential_selection_refused",
                "identity_selection_refused",
                "effect_configuration_refused",
            )
        ]
        self.assertEqual(sum(map(len, groups)), len(set().union(*groups)))
        self.assertEqual(
            set().union(*groups),
            set(self.contract["direct_sc_signal_inventory"]),
        )
        self.assertIn(
            "sc",
            self.contract["direct_sc_signal_inventory"]["SC_DISPATCH"],
        )
        self.assertIn(
            ".super-coder/scripts/mem.py",
            self.contract["direct_sc_signal_inventory"]["SC_MEM_AS"],
        )
        effects = self.contract["direct_effect_signal_inventory"]
        self.assertIn(
            ".super-coder/scripts/map_setup.py",
            effects["filesystem_write"],
        )
        self.assertIn("os.chmod", self.contract["effect_call_vocabulary"]["filesystem_write"])
        detector = self.contract["effect_detector_vocabulary"]
        self.assertTrue({"chmod", "mkdir", "rename"} <= set(detector["filesystem_write_methods"]))
        self.assertTrue(
            {"os.posix_spawn", "asyncio.create_subprocess_exec"}
            <= set(detector["process_calls"])
        )

    def test_inventory_detector_reacts_to_new_authority_and_effect_forms(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            scripts = root / ".super-coder/scripts"
            api = root / ".super-coder/api"
            scripts.mkdir(parents=True)
            api.mkdir(parents=True)
            (root / "sc").write_text("#!/bin/sh\nexec sh dispatcher\n")
            sample = scripts / "sample.py"
            sample.write_text(
                "import os\n"
                "SC_NEW_AUTHORITY = 'SC_NEW_AUTHORITY'\n"
                "os.chmod('target', 0o700)\n"
            )
            first = generated_inventory(root)
            self.assertEqual(
                first["direct_sc_signal_inventory"]["SC_NEW_AUTHORITY"],
                [".super-coder/scripts/sample.py"],
            )
            self.assertEqual(
                first["direct_effect_signal_inventory"]["filesystem_write"],
                [".super-coder/scripts/sample.py"],
            )
            sample.write_text(
                "import os\n"
                "os.posix_spawn('/bin/true', ['/bin/true'], {})\n"
            )
            second = generated_inventory(root)
            self.assertNotEqual(
                first["source_sha256_inventory"],
                second["source_sha256_inventory"],
            )
            self.assertEqual(
                second["direct_effect_signal_inventory"]["process_effect"],
                [".super-coder/scripts/sample.py", "sc"],
            )

    def test_linux_resolver_rejects_missing_ambiguous_stale_and_delegated_evidence(self):
        for kind in (
            "missing",
            "unreadable",
            "ambiguous",
            "no-descriptor",
            "stale",
            "delegated",
        ):
            with self.subTest(kind=kind), linux_domain_fixture(kind) as fixture:
                completed = run_provenance_probe(fixture)
                self.assertEqual(completed.returncode, 77, completed.stderr)
                self.assertEqual(json.loads(completed.stdout)["provenance"], "unknown")

    def test_managed_and_spoofed_selectors_refuse_before_external_effects(self):
        aliases = {
            name: f"value-{index}"
            for index, name in enumerate(self.contract["aliases"])
        }
        exact = {"DSH_SHELL": "1", **aliases}
        cases = [
            ("managed", {}, "marker-and-aliases-deleted"),
            ("managed", {"DSH_SHELL": "0", **aliases}, "marker-falsified"),
            ("managed", {**exact, "SC_DISPATCH": "TARGET"}, "dispatch-override"),
            ("managed", {**exact, "SC_MEM_AS": "Admin"}, "admin-selector"),
            ("native", {"DSH_SHELL": "1", "SC_DISPATCH": "TARGET"}, "native-spoof"),
            ("ambiguous", {"SC_DISPATCH": "TARGET"}, "unknown-membership"),
        ]
        with tempfile.TemporaryDirectory() as raw:
            fixture_root = Path(raw)
            admin_dir = fixture_root / "credentials"
            event_dir = fixture_root / "effects"
            admin_dir.mkdir()
            event_dir.mkdir()
            admin = admin_dir / "Admin.json"
            admin.write_text(json.dumps({
                "token": "discoverable-admin-token",
                "api_base": "http://127.0.0.1:8837",
            }))
            for kind, environment, label in cases:
                with self.subTest(label=label), linux_domain_fixture(kind) as fixture:
                    actual_environment = {
                        **environment,
                        "DSH_EFFECT_DIR": str(event_dir),
                        "DSH_ADMIN_CREDENTIAL": str(admin),
                        "DSH_EFFECT_API": "http://127.0.0.1:9/denied",
                    }
                    if actual_environment.get("SC_DISPATCH") == "TARGET":
                        actual_environment["SC_DISPATCH"] = str(EFFECT_DRIVER)
                    with access_watch(EFFECT_DRIVER, admin_dir, admin) as watcher:
                        completed = run_provenance_probe(
                            fixture,
                            environment=actual_environment,
                            extra_args=(
                                "--native-credential-dir",
                                str(admin_dir),
                                "--default-dispatch",
                                str(EFFECT_DRIVER),
                                *(item for pair in (
                                    ("--alias", name)
                                    for name in self.contract["aliases"]
                                ) for item in pair),
                            ),
                        )
                        self.assertEqual(completed.returncode, 77, completed.stderr)
                        self.assertEqual(json.loads(completed.stdout)["decision"], "refused")
                        self.assertEqual(watched_events(watcher), b"")
                    self.assertEqual(list(event_dir.iterdir()), [])

    def test_effect_driver_trips_every_external_sentinel(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            event_dir = root / "effects"
            admin = root / "Admin.json"
            admin.write_text('{"token":"discoverable"}\n')
            completed = subprocess.run(
                [str(EFFECT_DRIVER)],
                env=clean_probe_environment({
                    "DSH_EFFECT_DIR": str(event_dir),
                    "DSH_ADMIN_CREDENTIAL": str(admin),
                    "DSH_EFFECT_API": "http://127.0.0.1:9/expected-refusal",
                }),
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                {path.name for path in event_dir.iterdir()},
                {
                    "api_effect",
                    "credential_discovery",
                    "db_write",
                    "denied_marker_write",
                    "effect.db",
                    "filesystem_write",
                    "message_write",
                    "process_start",
                    "wake_write",
                },
            )

    def test_exact_managed_aliases_authorize_without_admin_discovery(self):
        aliases = {
            name: f"value-{index}"
            for index, name in enumerate(self.contract["aliases"])
        }
        with tempfile.TemporaryDirectory() as raw:
            admin_dir = Path(raw) / "credentials"
            admin_dir.mkdir()
            admin = admin_dir / "Admin.json"
            admin.write_text('{"token":"discoverable"}\n')
            with (
                linux_domain_fixture("managed") as fixture,
                access_watch(admin_dir, admin) as watcher,
            ):
                completed = run_provenance_probe(
                    fixture,
                    environment={"DSH_SHELL": "1", **aliases},
                    extra_args=tuple(
                        item for name in self.contract["aliases"]
                        for item in ("--alias", name)
                    ) + ("--native-credential-dir", str(admin_dir)),
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(json.loads(completed.stdout)["decision"], "authorized")
                self.assertEqual(watched_events(watcher), b"")

    def test_native_control_reaches_native_credential_discovery(self):
        with tempfile.TemporaryDirectory() as raw:
            admin_dir = Path(raw) / "credentials"
            admin_dir.mkdir()
            admin = admin_dir / "Admin.json"
            admin.write_text('{"token":"discoverable"}\n')
            with (
                linux_domain_fixture("native") as fixture,
                access_watch(admin_dir, admin) as watcher,
            ):
                completed = run_provenance_probe(
                    fixture,
                    extra_args=("--native-credential-dir", str(admin_dir)),
                )
                receipt = json.loads(completed.stdout)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(receipt["decision"], "native")
                self.assertEqual(receipt["credential_count"], 1)
                self.assertNotEqual(watched_events(watcher), b"")

    def test_every_source_hash_matches_the_exact_upstream_commit(self):
        commit = self.contract["dsh_release"]["commit"]
        for seam in self.contract["supported_seams"]:
            with self.subTest(seam=seam["name"]):
                url = (
                    "https://raw.githubusercontent.com/deepseek-ai/"
                    f"deepseek-harness/{commit}/{seam['source']}"
                )
                with urllib.request.urlopen(url, timeout=20) as response:
                    source = response.read()
                self.assertEqual(
                    hashlib.sha256(source).hexdigest(),
                    seam["source_sha256"],
                )

    def test_windows_job_object_contributor_is_exact_and_non_breakaway(self):
        source = WINDOWS_PROBE.read_text()
        self.assertEqual(
            sha256(WINDOWS_PROBE),
            "05a087c9653e3215a34a7f87f65895f459c15896df25c45a3455ae51e7f2a4bd",
        )
        for marker in (
            "IsProcessInJob",
            "QueryInformationJobObject",
            "JOB_OBJECT_LIMIT_BREAKAWAY_OK",
            "JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK",
            "GetFileType($descriptor)",
            'contract = "sc-dsh-windows-job-object-v1"',
        ):
            self.assertIn(marker, source)

    def test_real_pinned_dsh_components_reproduce_clean_room_fixture(self):
        dsh = shutil.which("dsh")
        self.assertIsNotNone(dsh, "pinned dsh is required for the executable fixture")
        package_root = Path(dsh).resolve().parents[1]
        completed = subprocess.run(
            ["node", str(NODE_PROBE), str(package_root), str(CONTRACT_PATH)],
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
        self.assertEqual(
            set(receipt["runtimeHashes"]),
            {seam["name"] for seam in self.contract["supported_seams"]},
        )
        self.assertEqual(receipt["powershellParity"], "source-contract-passed")


if __name__ == "__main__":
    unittest.main()
