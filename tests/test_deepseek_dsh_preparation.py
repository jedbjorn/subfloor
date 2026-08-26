import ast
import ctypes
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / ".super-coder/assets/deepseek/dsh-shell-authority-contract.json"
NODE_PROBE = ROOT / "tests/fixtures/deepseek_dsh_shell_env_probe.mjs"
INVENTORY_PROBE = ROOT / ".super-coder/scripts/dsh_preparation_inventory.py"
PROVENANCE_PROBE = ROOT / ".super-coder/scripts/dsh_execution_provenance.py"
EXECUTION_LAUNCHER = ROOT / ".super-coder/scripts/deepseek_execution_domain.py"
EFFECT_DRIVER = ROOT / "tests/fixtures/deepseek_dsh_effect_driver.py"
PROTOTYPE_ISSUER_PATH = ROOT / "tests/fixtures/deepseek_dsh_prototype_issuer.py"
SOURCE_ROOTS = (ROOT / ".super-coder/scripts", ROOT / ".super-coder/api")
REQUIRED_SEALS = (
    fcntl.F_SEAL_SEAL
    | fcntl.F_SEAL_SHRINK
    | fcntl.F_SEAL_GROW
    | fcntl.F_SEAL_WRITE
)

ISSUER_SPEC = importlib.util.spec_from_file_location(
    "deepseek_dsh_prototype_issuer",
    PROTOTYPE_ISSUER_PATH,
)
if ISSUER_SPEC is None or ISSUER_SPEC.loader is None:
    raise RuntimeError("cannot load prototype issuer fixture")
PROTOTYPE_ISSUER = importlib.util.module_from_spec(ISSUER_SPEC)
ISSUER_SPEC.loader.exec_module(PROTOTYPE_ISSUER)


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


def generated_inventory(root=ROOT, policy_contract=None):
    command = ["python3", str(INVENTORY_PROBE), "--root", str(root)]
    selected_policy = policy_contract or (CONTRACT_PATH if root == ROOT else None)
    if selected_policy is not None:
        command.extend(["--policy-contract", str(selected_policy)])
    completed = subprocess.run(
        command,
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


def process_start_ticks(pid):
    text = (Path("/proc") / str(pid) / "stat").read_text()
    fields = text[text.rfind(")") + 2:].split()
    return int(fields[19])


@contextmanager
def linux_domain_fixture(kind):
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        proc = root / "proc-cgroup"
        cgroup_root = root / "cgroup"
        cgroup_root.mkdir()
        issuer_key = root / "prototype-issuer-public.json"
        PROTOTYPE_ISSUER.write_public_key(issuer_key)
        now = 1_000_000
        descriptor_fd = None
        lineage_process = None
        domain_id = "a" * 32
        membership = f"/sc-dsh/{domain_id}.scope"
        if kind == "missing":
            pass
        elif kind == "unreadable":
            proc.mkdir()
        elif kind == "ambiguous":
            proc.write_text("0::/\n0::/other\n")
        elif kind in {"native", "copied-descriptor"}:
            proc.write_text("0::/native.slice\n")
        else:
            visible_membership = membership
            if kind == "cross-domain":
                visible_membership = f"/sc-dsh/{'b' * 32}.scope"
            proc.write_text(f"0::{visible_membership}\n")
            domain = cgroup_root / membership.lstrip("/")
            domain.mkdir(parents=True, mode=0o700)
            (domain / "cgroup.type").write_text("domain\n")
            (domain / "cgroup.subtree_control").write_text(
                "cpu\n" if kind == "delegated" else "\n"
            )
            cgroup_procs = domain / "cgroup.procs"
            cgroup_procs.write_text(f"{os.getpid()}\n")
            cgroup_procs.chmod(0o444)
            if kind != "attacker-owned-domain":
                domain.chmod(0o555)
            if kind != "no-descriptor":
                descriptor_fd = os.memfd_create(
                    "sc-dsh-domain",
                    os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
                )
                root_pid = os.getpid()
                if kind == "unauthorized-self-entry":
                    lineage_process = subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(60)"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    root_pid = lineage_process.pid
                metadata = domain.stat()
                descriptor = {
                    "contract": "sc-dsh-linux-cgroup-v2-v2",
                    "cgroup": membership,
                    "domain_id": domain_id,
                    "binding_generation": 7,
                    "expires_monotonic_ns": now if kind == "stale" else now + 1,
                    "non_delegated": kind != "delegated",
                    "issuer_key_id": PROTOTYPE_ISSUER.KEY_ID,
                    "root_pid": root_pid,
                    "root_start_ticks": process_start_ticks(root_pid),
                    "cgroup_device": metadata.st_dev,
                    "cgroup_inode": metadata.st_ino,
                    "cgroup_owner_uid": metadata.st_uid,
                }
                signed = PROTOTYPE_ISSUER.sign_descriptor(descriptor)
                if kind == "forged-descriptor":
                    signed["binding_generation"] = 8
                os.write(descriptor_fd, json.dumps(signed).encode())
                fcntl.fcntl(descriptor_fd, fcntl.F_ADD_SEALS, REQUIRED_SEALS)
        try:
            yield {
                "proc": proc,
                "cgroup_root": cgroup_root,
                "descriptor_fd": descriptor_fd,
                "issuer_key": issuer_key,
                "now": now,
            }
        finally:
            if descriptor_fd is not None:
                os.close(descriptor_fd)
            if lineage_process is not None:
                lineage_process.terminate()
                lineage_process.wait(timeout=5)


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
    if fixture.get("issuer_key") is not None:
        command.extend(["--prototype-issuer-key", str(fixture["issuer_key"])])
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


@contextmanager
def whoami_server(*, shortname="DEV5", before_reply=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def _reply(self):
            requests.append({
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "method": self.command,
            })
            if before_reply is not None:
                before_reply()
            payload = json.dumps({
                "shell_id": 11,
                "shell_shortname": shortname,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = _reply
        do_POST = _reply

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def write_owner_json(path, data):
    path.write_text(json.dumps(data, sort_keys=True))
    path.chmod(0o600)


def identity_material(root, api_base):
    worktree = root / "worktree"
    worktree.mkdir()
    credential = root / "credential.json"
    record = root / "identity-record.json"
    aliases = {
        "DSH_SC_SHELL_ID": "11",
        "DSH_SC_SHELL_SHORTNAME": "DEV5",
        "DSH_SC_SHELL_WORKTREE": str(worktree),
        "DSH_SC_API_BASE": api_base,
        "DSH_SC_MEM_CREDENTIAL_FILE": str(credential),
        "DSH_SC_BINDING_GENERATION": "7",
        "DSH_SC_PLUGIN_HEALTH_GENERATION": "plugin-contract-9",
    }
    write_owner_json(credential, {
        "contract": "sc-dsh-prototype-credential-v1",
        "token": "fixture-token",
        "api_base": api_base,
        "shell_id": 11,
        "shell_shortname": "DEV5",
        "binding_generation": 7,
        "plugin_health_generation": "plugin-contract-9",
    })
    write_owner_json(record, {
        "contract": "sc-dsh-prototype-current-identity-v1",
        "current": True,
        "shell_id": 11,
        "shell_shortname": "DEV5",
        "shell_worktree": str(worktree),
        "api_base": api_base,
        "credential_file": str(credential),
        "binding_generation": 7,
        "plugin_health_generation": "plugin-contract-9",
    })
    return aliases, record, credential


def authorized_probe_args(contract, record, effect):
    return tuple(
        item for name in contract["aliases"] for item in ("--alias", name)
    ) + (
        "--policy-route", "mem",
        "--identity-record", str(record),
        "--protected-effect", str(effect),
    )


class DeepSeekDshPreparationContractTests(unittest.TestCase):
    def setUp(self):
        self.contract = load_contract()

    def test_release_alias_health_and_currency_contract_is_exact(self):
        adapter = json.loads(
            (ROOT / ".super-coder/adapters/deepseek/adapter.json").read_text()
        )
        release = self.contract["dsh_release"]
        self.assertEqual(
            self.contract["governing_revision"],
            {
                "document_id": 174,
                "current_sha256": "06bb2bc31856575b88983d522fd881ad8e9b68c75714a804ff6cdb5bbd98aeb8",
                "sprint_bound_sha256": "84056c2fc7206b83f2d3beb71150545326d5e33f557cb5f2329f55321eab0bdf",
                "scope_decision_id": 261,
                "reviewer_disposition_message_id": 1385,
                "drift_disposition": "retain Sprint 25 WU104 Doc 174 v4 as the immutable Sprint binding; Doc 174 v5, Decision 261, and Reviewer disposition message 1385 are active reliability narrowing authority and do not edit that binding",
                "task_id": 651,
                "sprint_id": 25,
                "work_unit_id": 104,
                "preparation_work_unit": {
                    "task_id": 648,
                    "work_unit_id": 101,
                },
                "replaced_binding": {
                    "sprint_id": 24,
                    "work_unit_id": 95,
                    "sprint_bound_sha256": "a305eba5c73988d202e3f3f9d392d623645b3f799dfe4ab4945687026ae5a969",
                },
                "implementation_base": "21a05f3eae09b70c4c42bae69a6d04e89bb1d8f7",
            },
        )
        self.assertEqual(
            self.contract["retirement_package"],
            {
                "contract": "sc-dsh-containment-retirement-v1",
                "sprint_id": 25,
                "work_unit_id": 106,
                "task_id": 653,
                "historical_sprint_binding_sha256": "84056c2fc7206b83f2d3beb71150545326d5e33f557cb5f2329f55321eab0bdf",
                "active_reliability_sha256": "06bb2bc31856575b88983d522fd881ad8e9b68c75714a804ff6cdb5bbd98aeb8",
                "scope_decision_id": 261,
                "candidate_acceptance_decision_id": 262,
                "candidate_ref": "8a4551100ca14f0777f175719b577cb11b733565",
                "candidate_receipt_sha256": "77791c5d4e031cf9b16e7ddee3932f581bbafc5094f86e26fd8a2b1504c5ae69",
                "mutation_driver": "tests/mutations/u106_deepseek_retirement.py",
                "receipt_contract": "sc-dsh-containment-retirement-receipt-v1",
                "receipt_authority": (
                    "the retirement_ref and retirement_tree are read from the clean "
                    "exact worktree; every mutation must turn its focused promoted-path "
                    "proof red and the restored source green"
                ),
                "protected_boundaries": [
                    "global ShellIdentityLease and managed identity wait queue are absent",
                    "unknown one-shot terminality closes only its exact transactional binding",
                    "failed, stale, and partial promoted ratchets revoke and fence only enumerated disposable roots",
                    "unrelated bindings and already-admitted immutable ToolExecution snapshots remain available",
                    "model discovery and identity-neutral native inference survive managed-authority refusal",
                    "Browser, Sprint, and one-shot preserve surface-specific bounded readiness behavior",
                ],
            },
        )
        self.assertEqual(adapter["official_runtime"]["version"], release["version"])
        self.assertEqual(adapter["official_runtime"]["tag"], release["tag"])
        self.assertEqual(adapter["official_runtime"]["commit"], release["commit"])
        self.assertEqual(
            adapter["official_runtime"]["shell_authority_contract"],
            "assets/deepseek/dsh-shell-authority-contract.json",
        )
        self.assertEqual(self.contract["schema_version"], 2)
        self.assertEqual(
            self.contract["verification_package"],
            {
                "contract": "sc-dsh-reliability-verification-v1",
                "sprint_id": 25,
                "work_unit_id": 105,
                "task_id": 652,
                "historical_sprint_binding_sha256": "84056c2fc7206b83f2d3beb71150545326d5e33f557cb5f2329f55321eab0bdf",
                "active_reliability_sha256": "06bb2bc31856575b88983d522fd881ad8e9b68c75714a804ff6cdb5bbd98aeb8",
                "scope_decision_id": 261,
                "reviewer_disposition_message_id": 1385,
                "mutation_driver": "tests/mutations/u105_deepseek_reliability.py",
                "receipt_contract": "sc-dsh-reliability-verification-receipt-v1",
                "receipt_authority": (
                    "the candidate_ref and candidate_tree are read from the clean "
                    "owned worktree; every mutation must turn its focused test red "
                    "and the exact restored source green"
                ),
                "protected_boundaries": [
                    "bounded transient readiness with immediate invariant refusal",
                    "Browser chat-only, Sprint turn-only, and one-shot invocation-only exhaustion",
                    "model and native inference continuity after managed-authority refusal",
                    "binding create, rotate, reopen, and protected-root revalidation",
                    "fresh Bash and pwsh Linux execution domain per ToolExecution root",
                    "already-admitted immutable ToolExecution snapshot with new-root refusal",
                    "candidate ratchet revocation and enumerated-root-only fencing",
                ],
                "retirement_authority": {
                    "work_unit_id": 106,
                    "task_id": 653,
                    "advanced_by_this_package": False,
                },
            },
        )
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
        promoted = self.contract["promoted_runtime"]
        self.assertEqual(promoted["production_state"], "promoted")
        self.assertEqual(
            promoted["retired_containment"],
            [
                "DeepSeekAdapter ShellIdentityLease",
                "managed identity wait queue",
                "global unproven Host-credential marker",
            ],
        )
        self.assertIn(
            "server-minted exact-ref capability",
            promoted["proof_authority"],
        )
        self.assertIn("scrubs ambient SC credentials", promoted["ambient_boundary"])
        self.assertIn("no promoted failure restores them", promoted["ambient_boundary"])
        self.assertIn("unrelated bindings", promoted["failure_boundary"])
        self.assertNotIn("containment_baseline", self.contract)
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
                "contract": "sc-dsh-linux-cgroup-v2-v2",
            },
        )
        self.assertEqual(
            provenance["production_linux_contributor"],
            {
                "path": ".super-coder/scripts/deepseek_execution_domain.py",
                "sha256": sha256(EXECUTION_LAUNCHER),
                "contract": "sc-dsh-linux-cgroup-v2-v3",
                "descriptor_fd": 198,
                "delegation": "/usr/bin/systemd-run --user --scope --property=Delegate=yes is the fixed supported-Linux scope launcher; the execution issuer creates roots only below its own delegated membership",
                "entry": "Wrap only a complete registry-current ToolExecution identity. Refuse partial, stale, copied, wrong-domain, dead-issuer, self-created, and self-entered evidence before user code.",
                "teardown": "Drain or cgroup.kill the one execution domain within two seconds, remove it, and surface incomplete cleanup as failure.",
            },
        )
        self.assertIn("Production MUST use", provenance["preparation_boundary"])
        self.assertIn("MUST NOT wire the prototype", provenance["preparation_boundary"])
        self.assertEqual(
            provenance["prototype_issuer_fixture"],
            {
                "path": "tests/fixtures/deepseek_dsh_prototype_issuer.py",
                "sha256": sha256(PROTOTYPE_ISSUER_PATH),
                "authority": "deterministic test-only signer; never a product authority source",
            },
        )
        harness = provenance["preparation_policy_harness"]
        self.assertEqual(
            harness["effect_driver"],
            {
                "path": "tests/fixtures/deepseek_dsh_effect_driver.py",
                "sha256": sha256(EFFECT_DRIVER),
            },
        )
        self.assertIn("stable sc admission boundary", harness["neutral_route"])
        self.assertIn("authenticated whoami equality", harness["authorized_route"])
        self.assertIn("absent before replying", harness["effect_order"])
        self.assertIn("task 650 wires the stable sc bootstrap", harness["production_wiring"])
        self.assertNotIn("windows", provenance)
        self.assertNotIn("windows_contributor", provenance)
        self.assertIn("Arch and Ubuntu Linux only", provenance["platform_boundary"])
        self.assertIn("pwsh", provenance["executor_rule"])
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
        literal_policy = self.contract["literal_subcommand_policy"]
        self.assertEqual(
            set(literal_policy),
            set(self.contract["literal_subparser_counters"]),
        )
        for relative, counters in self.contract["literal_subparser_counters"].items():
            self.assertEqual(set(literal_policy[relative]), set(counters), relative)
            self.assertTrue(
                set(literal_policy[relative].values())
                <= {"dsh_shell_authorized", "refused"},
                relative,
            )
        self.assertEqual(
            literal_policy[".super-coder/scripts/job.py"]["_supervise"],
            "refused",
        )
        self.assertEqual(
            literal_policy[".super-coder/scripts/mem.py"]["which"],
            "dsh_shell_authorized",
        )
        self.assertEqual(
            literal_policy[".super-coder/scripts/migration.py"]["new"],
            "refused",
        )
        authorized_literal_paths = {
            ".super-coder/scripts/job.py",
            ".super-coder/scripts/mem.py",
            ".super-coder/scripts/pr_cli.py",
            ".super-coder/scripts/sprint_cli.py",
            ".super-coder/scripts/visual_qa.py",
            ".super-coder/scripts/vm.py",
        }
        for relative, policies in literal_policy.items():
            for subcommand, outcome in policies.items():
                expected = (
                    "refused"
                    if relative not in authorized_literal_paths
                    or (relative.endswith("/job.py") and subcommand == "_supervise")
                    else "dsh_shell_authorized"
                )
                self.assertEqual(outcome, expected, f"{relative}:{subcommand}")

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
        custom_policy = self.contract["custom_subcommand_policy"]
        self.assertEqual(set(custom_policy), set(custom))
        for surface, subcommands in custom.items():
            self.assertEqual(set(custom_policy[surface]), subcommands, surface)
            self.assertTrue(
                set(custom_policy[surface].values())
                <= {"dsh_shell_authorized", "refused"},
                surface,
            )
        authorized_custom = {
            "feature": {"list"},
            "models": {"refresh", "list", "resolve"},
            "skill": {"list", "put", "grant", "revoke", "rm", "retire", "unretire"},
            "map-extractor": {"install"},
            "vm-mcp-relay": {"up", "down", "status"},
            "vm.mcp": {"status", "up", "down"},
            "mem.dynamic": {"seed", "lns"},
        }
        for surface, policies in custom_policy.items():
            for subcommand, outcome in policies.items():
                expected = (
                    "dsh_shell_authorized"
                    if subcommand in authorized_custom.get(surface, set())
                    else "refused"
                )
                self.assertEqual(outcome, expected, f"{surface}:{subcommand}")

    def test_exhaustive_sc_signal_and_bounded_effect_inventory_is_exact(self):
        observed = generated_inventory()
        for name in (
            "bounded_audit_scope",
            "source_sha256_inventory",
            "direct_sc_signal_inventory",
            "ambient_sc_policy",
            "literal_subparser_counters",
            "literal_subcommand_policy",
            "effect_call_vocabulary",
            "direct_effect_signal_inventory",
            "risky_call_inventory",
            "risky_call_classification",
            "route_entrypoint_inventory",
            "bounded_direct_consumer_inventory",
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
        scope = self.contract["bounded_audit_scope"]
        expected_scope = {
            "sc",
            ".super-coder/scripts/dispatch.sh",
            ".super-coder/scripts/job.py",
            ".super-coder/scripts/map_finalize.py",
            ".super-coder/scripts/mem.py",
            ".super-coder/scripts/models.py",
            ".super-coder/scripts/pr_cli.py",
            ".super-coder/scripts/skill.py",
        }
        self.assertEqual(set(scope["paths"]), expected_scope)
        self.assertEqual(set(self.contract["source_sha256_inventory"]), expected_scope)
        self.assertIn("no route-to-callsite", scope["rule"])
        self.assertIn("whole-program", scope["rule"])
        for excluded in (
            "risky_callsite_inventory",
            "effect_callsite_inventory",
            "effect_callsite_id_inventory",
            "route_source_reachability",
            "direct_consumer_policy",
            "route_effect_binding_inventory",
            "effect_callsite_policy_rule",
        ):
            self.assertNotIn(excluded, self.contract)
        effects = self.contract["direct_effect_signal_inventory"]
        self.assertIn(
            ".super-coder/scripts/map_finalize.py",
            effects["direct_db_write"],
        )
        self.assertIn("os.open", self.contract["effect_call_vocabulary"]["filesystem_write"])
        for path in (
            ".super-coder/scripts/map_finalize.py",
            ".super-coder/scripts/models.py",
            ".super-coder/scripts/skill.py",
        ):
            self.assertTrue(
                any(
                    path in effects.get(category, [])
                    for category in ("direct_db", "direct_db_read", "direct_db_write")
                ),
                path,
            )
        db_vocabulary = self.contract["effect_call_vocabulary"]
        self.assertIn("db_driver.connect", db_vocabulary["direct_db"])
        self.assertIn("db_connection.execute.read", db_vocabulary["direct_db_read"])
        self.assertIn("db_connection.execute.write", db_vocabulary["direct_db_write"])
        self.assertIn("db_connection.commit", db_vocabulary["direct_db_write"])
        detector = self.contract["effect_detector_vocabulary"]
        self.assertTrue({"chmod", "mkdir", "rename"} <= set(detector["filesystem_write_methods"]))
        self.assertTrue(
            {"os.posix_spawn", "asyncio.create_subprocess_exec"}
            <= set(detector["process_calls"])
        )
        risky = self.contract["risky_call_inventory"]
        classifications = self.contract["risky_call_classification"]
        self.assertEqual(set(risky), set(classifications))
        self.assertEqual(classifications["db_driver.connect"], "direct_db")
        self.assertEqual(classifications["os.environ.get"], "credential_discovery")
        self.assertEqual(classifications["os.kill"], "process_effect")
        self.assertEqual(classifications["os.killpg"], "process_effect")
        self.assertEqual(classifications["os.open"], "filesystem_write")
        self.assertEqual(classifications["os.read"], "filesystem_read")
        self.assertEqual(classifications["subprocess.run"], "process_effect")
        consumers = self.contract["bounded_direct_consumer_inventory"]
        self.assertEqual(set(consumers), set(scope["paths"]))
        for path, row in consumers.items():
            self.assertEqual(row["sha256"], sha256(ROOT / path))
            self.assertEqual(
                set(row["sc_signals"]),
                {
                    name
                    for name, paths in self.contract["direct_sc_signal_inventory"].items()
                    if path in paths
                },
            )
            self.assertEqual(
                set(row["effect_classes"]),
                {
                    name
                    for name, paths in effects.items()
                    if path in paths
                },
            )

    def test_native_windows_assets_and_ci_are_excluded(self):
        excluded = (
            "tests/fixtures/deepseek_dsh_job_object_probe.ps1",
            "tests/fixtures/deepseek_dsh_windows_descriptor_probe.mjs",
            "tests/fixtures/deepseek_dsh_windows_descriptor_vectors.json",
            "tests/fixtures/deepseek_dsh_windows_native_adapter.ps1",
            "tests/fixtures/deepseek_dsh_windows_provenance_policy.json",
            "tests/fixtures/test_deepseek_dsh_windows_native_adapter.ps1",
        )
        self.assertEqual(
            [relative for relative in excluded if (ROOT / relative).exists()],
            [],
        )
        workflow = (ROOT / ".github/workflows/tests.yml").read_text()
        self.assertNotIn("windows-latest", workflow)
        self.assertNotIn("Windows DSH native provenance contract", workflow)

    def test_inventory_detector_reacts_to_new_authority_and_effect_forms(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            scripts = root / ".super-coder/scripts"
            api = root / ".super-coder/api"
            scripts.mkdir(parents=True)
            api.mkdir(parents=True)
            (root / "sc").write_text("#!/bin/sh\nexec sh dispatcher\n")
            sample = scripts / "mem.py"
            sample.write_text(
                "import asyncio\n"
                "import http.client\n"
                "import db_driver as driver\n"
                "from db_driver import connect as open_engine_db\n"
                "import os\n"
                "import shutil as files\n"
                "from os import kill as signal_process\n"
                "def injected(path, connect=driver.connect):\n"
                "    return connect(path)\n"
                "def wrapped(path):\n"
                "    return injected(path)\n"
                "SC_NEW_AUTHORITY = 'SC_NEW_AUTHORITY'\n"
                "asyncio.open_connection('127.0.0.1', 9)\n"
                "asyncio.start_server(lambda: None, '127.0.0.1', 9)\n"
                "http.client.HTTPConnection('127.0.0.1')\n"
                "signal_process(1, 0)\n"
                "os.killpg(1, 0)\n"
                "os.write(1, b'x')\n"
                "os.fsync(1)\n"
                "files.copytree('source', 'target')\n"
                "con = driver.connect('engine.db')\n"
                "connection = open_engine_db('engine-alias.db')\n"
                "injected_connection = wrapped('engine-wrapped.db')\n"
                "connection.execute('SELECT 2')\n"
                "injected_connection.execute('SELECT 3')\n"
                "con.execute('SELECT 1')\n"
                "con.execute('UPDATE shells SET current_state=NULL')\n"
                "con.commit()\n"
                "con.rollback()\n"
                "text = 'ordinary'.replace('o', 'x')\n"
                "items = ['ordinary']; items.remove('ordinary')\n"
                "stream = object(); stream.open()\n"
            )
            first = generated_inventory(root)
            self.assertEqual(
                first["direct_sc_signal_inventory"]["SC_NEW_AUTHORITY"],
                [".super-coder/scripts/mem.py"],
            )
            expected = {
                "asyncio.open_connection": "api_effect",
                "asyncio.start_server": "api_effect",
                "http.client.HTTPConnection": "api_effect",
                "os.fsync": "filesystem_write",
                "os.kill": "process_effect",
                "os.killpg": "process_effect",
                "os.write": "filesystem_write",
                "shutil.copytree": "filesystem_write",
                "db_driver.connect": "direct_db",
            }
            self.assertEqual(first["risky_call_classification"], expected)
            self.assertEqual(
                set(first["risky_call_inventory"]),
                set(first["risky_call_classification"]),
            )
            db_calls = first["effect_call_vocabulary"]
            self.assertIn("db_connection.execute.read", db_calls["direct_db_read"])
            self.assertIn("db_connection.execute.write", db_calls["direct_db_write"])
            self.assertIn("db_connection.commit", db_calls["direct_db_write"])
            self.assertIn("db_connection.rollback", db_calls["direct_db_write"])
            self.assertNotIn("effect_callsite_inventory", first)
            self.assertNotIn("route_source_reachability", first)
            self.assertNotIn("route_effect_binding_inventory", first)
            self.assertEqual(
                set(first["bounded_direct_consumer_inventory"]),
                {".super-coder/scripts/mem.py", "sc"},
            )
            sample.write_text(
                "import socket\n"
                "socket.future_effect('target')\n"
            )
            with self.assertRaisesRegex(
                AssertionError,
                r"(?s)unclassified risky calls:.*socket\.future_effect",
            ):
                generated_inventory(root)

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

    def test_linux_resolver_accepts_only_issuer_attested_protected_lineage(self):
        with linux_domain_fixture("managed") as fixture:
            completed = run_provenance_probe(
                fixture,
                environment={"DSH_SHELL": "1"},
                extra_args=("--command-class", "identity_neutral_read_only"),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                json.loads(completed.stdout),
                {
                    "decision": "neutral",
                    "provenance": "managed",
                    "credential_count": None,
                },
            )

    def test_linux_resolver_rejects_forged_sealed_descriptor(self):
        with linux_domain_fixture("forged-descriptor") as fixture:
            completed = run_provenance_probe(fixture)
            self.assertEqual(completed.returncode, 77, completed.stderr)
            self.assertIn("signature is invalid", json.loads(completed.stdout)["reason"])

    def test_linux_resolver_rejects_owner_writable_self_created_domain(self):
        with linux_domain_fixture("attacker-owned-domain") as fixture:
            completed = run_provenance_probe(fixture)
            self.assertEqual(completed.returncode, 77, completed.stderr)
            self.assertIn("admission is writable", json.loads(completed.stdout)["reason"])

    def test_linux_resolver_rejects_cross_domain_descriptor(self):
        with linux_domain_fixture("cross-domain") as fixture:
            completed = run_provenance_probe(fixture)
            self.assertEqual(completed.returncode, 77, completed.stderr)
            self.assertIn("names another cgroup", json.loads(completed.stdout)["reason"])

    def test_linux_resolver_rejects_unauthorized_self_entry(self):
        with linux_domain_fixture("unauthorized-self-entry") as fixture:
            completed = run_provenance_probe(fixture)
            self.assertEqual(completed.returncode, 77, completed.stderr)
            self.assertIn(
                "outside the issued execution lineage",
                json.loads(completed.stdout)["reason"],
            )

    def test_linux_resolver_rejects_descriptor_copied_outside_domain(self):
        with linux_domain_fixture("managed") as fixture:
            fixture["proc"].write_text("0::/native.slice\n")
            completed = run_provenance_probe(fixture)
            self.assertEqual(completed.returncode, 77, completed.stderr)
            self.assertIn("descriptor is outside", json.loads(completed.stdout)["reason"])

    def test_managed_and_spoofed_selectors_refuse_before_external_effects(self):
        aliases = {
            name: f"value-{index}"
            for index, name in enumerate(self.contract["aliases"])
        }
        exact = {"DSH_SHELL": "1", **aliases}
        cases = [
            ("managed", {}, "marker-and-aliases-deleted"),
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
                    "api_read",
                    "api_write",
                    "credential_discovery",
                    "credential_read",
                    "credential_write",
                    "db_read",
                    "db_write",
                    "denied_marker_write",
                    "effect.db",
                    "filesystem_write",
                    "message_write",
                    "process_start",
                    "wake_write",
                },
            )

    def test_neutral_help_harness_has_zero_external_effects_or_authority_output(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            admin_dir = root / "credentials"
            event_dir = root / "effects"
            admin_dir.mkdir()
            event_dir.mkdir()
            admin = admin_dir / "Admin.json"
            admin.write_text('{"token":"discoverable"}\n')
            db = root / "neutral.db"
            db.write_text("sentinel\n")
            identity_record = root / "identity-record.json"
            write_owner_json(identity_record, {"must": "not be read"})
            protected_effect = root / "protected-effect"
            with whoami_server() as (api_base, requests):
                with (
                    linux_domain_fixture("managed") as fixture,
                    access_watch(
                        EFFECT_DRIVER,
                        admin_dir,
                        admin,
                        db,
                        identity_record,
                    ) as watcher,
                ):
                    completed = run_provenance_probe(
                        fixture,
                        environment={
                            "DSH_EFFECT_DIR": str(event_dir),
                            "DSH_ADMIN_CREDENTIAL": str(admin),
                            "DSH_EFFECT_API": api_base,
                        },
                        extra_args=(
                            "--policy-route", "help",
                            "--identity-record", str(identity_record),
                            "--protected-effect", str(protected_effect),
                            "--default-dispatch", str(EFFECT_DRIVER),
                        ),
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertEqual(
                        json.loads(completed.stdout),
                        {
                            "decision": "neutral",
                            "provenance": "managed",
                            "credential_count": None,
                            "route": "help",
                        },
                    )
                    self.assertEqual(watched_events(watcher), b"")
                self.assertEqual(requests, [])
            self.assertEqual(list(event_dir.iterdir()), [])
            self.assertFalse(protected_effect.exists())

    def test_exact_authenticated_identity_ignores_marker_before_first_effect(self):
        marker_cases = (
            ("observed", {"DSH_SHELL": "1"}),
            ("absent", {}),
            ("zero", {"DSH_SHELL": "0"}),
            ("falsified", {"DSH_SHELL": "caller-selected"}),
        )
        for label, marker_environment in marker_cases:
            with self.subTest(marker=label), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                effect = root / "protected-effect.json"
                effect_absent_at_whoami = []

                def observe_before_reply(
                    effect_path=effect,
                    observations=effect_absent_at_whoami,
                ):
                    observations.append(not effect_path.exists())

                with whoami_server(before_reply=observe_before_reply) as (
                    api_base,
                    requests,
                ):
                    aliases, record, _credential = identity_material(root, api_base)
                    with linux_domain_fixture("managed") as fixture:
                        completed = run_provenance_probe(
                            fixture,
                            environment={**marker_environment, **aliases},
                            extra_args=authorized_probe_args(
                                self.contract,
                                record,
                                effect,
                            ),
                        )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(
                    json.loads(completed.stdout)["decision"],
                    "authorized",
                )
                self.assertEqual(
                    requests,
                    [{
                        "path": "/_sc/mem/whoami",
                        "authorization": "Bearer fixture-token",
                        "method": "GET",
                    }],
                )
                self.assertEqual(effect_absent_at_whoami, [True])
                self.assertEqual(
                    json.loads(effect.read_text()),
                    {
                        "binding_generation": 7,
                        "plugin_health_generation": "plugin-contract-9",
                        "shell_id": 11,
                        "shell_shortname": "DEV5",
                    },
                )

    def _assert_identity_refusal(
        self,
        mutate,
        *,
        whoami_shortname="DEV5",
        before_reply=None,
    ):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            effect = root / "protected-effect.json"
            with whoami_server(
                shortname=whoami_shortname,
                before_reply=before_reply,
            ) as (api_base, requests):
                aliases, record, credential = identity_material(root, api_base)
                mutate(aliases, record, credential)
                with linux_domain_fixture("managed") as fixture:
                    completed = run_provenance_probe(
                        fixture,
                        environment={"DSH_SHELL": "1", **aliases},
                        extra_args=authorized_probe_args(self.contract, record, effect),
                    )
            self.assertEqual(completed.returncode, 77, completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["decision"], "refused")
            self.assertFalse(effect.exists())
            return requests

    def test_authorized_harness_rejects_partial_aliases_before_effect(self):
        requests = self._assert_identity_refusal(
            lambda aliases, _record, _credential: aliases.pop("DSH_SC_SHELL_ID")
        )
        self.assertEqual(requests, [])

    def test_authorized_harness_rejects_unknown_aliases_before_effect(self):
        requests = self._assert_identity_refusal(
            lambda aliases, _record, _credential: aliases.__setitem__(
                "DSH_SC_UNDECLARED", "unexpected"
            )
        )
        self.assertEqual(requests, [])

    def test_authorized_harness_rejects_malformed_aliases_before_effect(self):
        requests = self._assert_identity_refusal(
            lambda aliases, _record, _credential: aliases.__setitem__(
                "DSH_SC_SHELL_ID", "not-an-id"
            )
        )
        self.assertEqual(requests, [])

    def test_authorized_harness_rejects_stale_current_record_before_effect(self):
        def stale(_aliases, record, _credential):
            data = json.loads(record.read_text())
            data["current"] = False
            write_owner_json(record, data)

        requests = self._assert_identity_refusal(stale)
        self.assertEqual(requests, [])

    def test_authorized_harness_rejects_cross_shell_record_before_effect(self):
        def cross_shell(_aliases, record, _credential):
            data = json.loads(record.read_text())
            data["shell_id"] = 12
            write_owner_json(record, data)

        requests = self._assert_identity_refusal(cross_shell)
        self.assertEqual(requests, [])

    def test_authorized_harness_rejects_unsafe_credential_before_effect(self):
        requests = self._assert_identity_refusal(
            lambda _aliases, _record, credential: credential.chmod(0o644)
        )
        self.assertEqual(requests, [])

    def test_authorized_harness_rejects_symlink_credential_before_effect(self):
        def symlink(aliases, record, credential):
            link = credential.with_name("credential-link.json")
            link.symlink_to(credential)
            aliases["DSH_SC_MEM_CREDENTIAL_FILE"] = str(link)
            data = json.loads(record.read_text())
            data["credential_file"] = str(link)
            write_owner_json(record, data)

        requests = self._assert_identity_refusal(symlink)
        self.assertEqual(requests, [])

    def test_authorized_harness_rejects_nonloopback_api_before_effect(self):
        requests = self._assert_identity_refusal(
            lambda aliases, _record, _credential: aliases.__setitem__(
                "DSH_SC_API_BASE", "http://192.0.2.1:8837"
            )
        )
        self.assertEqual(requests, [])

    def test_authorized_harness_rejects_whoami_mismatch_before_effect(self):
        requests = self._assert_identity_refusal(
            lambda _aliases, _record, _credential: None,
            whoami_shortname="OTHER",
        )
        self.assertEqual(
            requests,
            [{
                "path": "/_sc/mem/whoami",
                "authorization": "Bearer fixture-token",
                "method": "GET",
            }],
        )

    def test_authorized_harness_rejects_changed_credential_before_effect(self):
        changed = {"path": None}

        def mutate(_aliases, _record, credential):
            changed["path"] = credential

        def change_during_whoami():
            path = changed["path"]
            data = json.loads(path.read_text())
            data["token"] = "changed-token-after-request"
            write_owner_json(path, data)

        requests = self._assert_identity_refusal(
            mutate,
            before_reply=change_during_whoami,
        )
        self.assertEqual(len(requests), 1)

    def test_authorized_harness_rejects_stale_generations_before_effect(self):
        requests = self._assert_identity_refusal(
            lambda aliases, _record, _credential: aliases.__setitem__(
                "DSH_SC_BINDING_GENERATION", "8"
            )
        )
        self.assertEqual(requests, [])

    def test_authorized_harness_rejects_stale_plugin_generation_before_effect(self):
        requests = self._assert_identity_refusal(
            lambda aliases, _record, _credential: aliases.__setitem__(
                "DSH_SC_PLUGIN_HEALTH_GENERATION", "plugin-contract-stale"
            )
        )
        self.assertEqual(requests, [])

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
        self.assertEqual(
            receipt["powershellToolExecutionSeam"],
            "supported-linux-only",
        )


if __name__ == "__main__":
    unittest.main()
