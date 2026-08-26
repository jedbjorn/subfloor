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
from contextlib import contextmanager
from pathlib import Path
from typing import Self

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / ".super-coder"
SCRIPTS = ENGINE / "scripts"
PLUGIN = ENGINE / "assets" / "deepseek" / "sc-shell-env-plugin.mjs"
PLUGIN_PROBE = ROOT / "tests" / "fixtures" / "deepseek_dsh_identity_plugin_probe.mjs"
sys.path.insert(0, str(SCRIPTS))

import deepseek_execution_domain
import deepseek_host
import deepseek_one_shot
import deepseek_web
import dsh_execution_provenance
from deepseek_identity_registry import (
    ALIASES,
    HEALTH_CONTRACT,
    DeepSeekIdentityError,
    DeepSeekIdentityRegistry,
    SimulatedRegistryCrash,
    plugin_contract_generation,
    process_start_ticks,
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
    host_pid: int | None = None,
) -> str:
    selected_host_pid = os.getpid() if host_pid is None else host_pid
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
    registry.observe_host(
        host_boot_generation=host_boot_generation,
        host_pid=selected_host_pid,
    )
    owner_json(
        registry.layout.health,
        {
            "contract": HEALTH_CONTRACT,
            "loaded": True,
            "fork_id": registry.layout.fork_id,
            "profile_id": registry.layout.profile_id,
            "registry_path": str(registry.layout.registry.resolve()),
            "host_boot_generation": host_boot_generation,
            "host_pid": selected_host_pid,
            "host_start_ticks": process_start_ticks(selected_host_pid),
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
        "hostIdentityPath": str(registry.layout.host_identity.resolve()),
        "executionLauncherPath": str(
            (SCRIPTS / "deepseek_execution_domain.py").resolve()
        ),
        "executionLauncherDigest": registry.execution_launcher_digest,
        "cgroupRoot": "/sys/fs/cgroup",
        "descriptorFd": 198,
        "descriptorTtlSeconds": 86400,
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
        assert self.process.pid is not None
        self.registry.observe_host(
            host_boot_generation=self.boot_generation,
            host_pid=self.process.pid,
        )
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

    def spawn(
        self,
        session_id: str,
        argv: list[str],
        *,
        environment: dict[str, str] | None = None,
    ) -> dict[str, object]:
        request: dict[str, object] = {
            "session_id": session_id,
            "spawn_argv": argv,
        }
        if environment is not None:
            request["spawn_env"] = environment
        return self.request(request)

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
    lifecycle_epoch: int = 1,
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
        lifecycle_epoch=lifecycle_epoch,
        shell_id=shell_id,
        shell_shortname=shortname,
        shell_worktree=worktree,
        api_base="http://127.0.0.1:8837",
        token=token,
        plugin_contract_generation=health["plugin_contract_generation"],
        crash_at=crash_at,
    )


def terminalize_binding(
    registry: DeepSeekIdentityRegistry,
    *,
    session_id: str,
) -> None:
    snapshot = registry.read_snapshot()
    record = snapshot["records"][session_id]
    closing = registry.begin_close(
        expected_snapshot_generation=snapshot["snapshot_generation"],
        root_session_id=session_id,
        expected_record_generation=record["record_generation"],
    )
    registry.retire_binding(
        expected_snapshot_generation=closing.snapshot_generation,
        root_session_id=session_id,
        expected_record_generation=closing.record_generation,
        quiesced=True,
    )


def wait_for(predicate, *, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition did not become true before timeout")


@contextmanager
def sleeping_process():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


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
            process = subprocess.Popen(
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
            registry.observe_host(
                host_boot_generation=boot,
                host_pid=process.pid,
            )
            return process

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
            first.kill()
            first.wait(timeout=5)
            assert json.loads(registry.layout.health.read_text())["loaded"] is True
            with pytest.raises(DeepSeekIdentityError) as abruptly_dead:
                registry.read_live_health(expected_host_boot_generation="host-boot-one")
            assert abruptly_dead.value.code == "HARNESS_PLUGIN_HEALTH_UNAVAILABLE"

        finally:
            if first.poll() is None:
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


def test_bash_and_pwsh_tool_executions_use_the_fixed_domain_launcher() -> None:
    with tempfile.TemporaryDirectory() as raw:
        registry, worktree = registry_fixture(Path(raw))
        with PluginProbe(registry) as plugin:
            create_binding(
                registry,
                worktree,
                session_id="wrapped-session",
                conversation_id="wrapped-conversation",
                shell_id=37,
                shortname="WRAPPED",
                token="wrapped-secret",
            )
            requests = (
                ["bash", "-c", "printf bash"],
                ["pwsh", "-NoProfile", "-Command", "Write-Output pwsh"],
            )
            domain_ids = set()
            for requested in requests:
                wrapped = plugin.spawn("wrapped-session", requested)["argv"]
                assert wrapped[:7] == [
                    "/usr/bin/systemd-run",
                    "--user",
                    "--scope",
                    "--quiet",
                    "--collect",
                    "--property=Delegate=yes",
                    "--",
                ]
                assert wrapped[7] == str(SCRIPTS / "deepseek_execution_domain.py")
                assert wrapped[-len(requested) :] == requested
                assert wrapped[wrapped.index("--descriptor-fd") + 1] == "198"
                assert wrapped[wrapped.index("--registry") + 1] == str(
                    registry.layout.registry.resolve()
                )
                domain_id = wrapped[wrapped.index("--domain-id") + 1]
                assert len(domain_id) == 32
                domain_ids.add(domain_id)
            assert len(domain_ids) == 2
            assert plugin.spawn(
                "wrapped-session",
                ["native-tool", "argument"],
                environment={"PATH": os.environ["PATH"]},
            ) == {"argv": ["native-tool", "argument"]}
            partial = plugin.spawn(
                "wrapped-session",
                ["bash", "-c", "false"],
                environment={
                    "DSH_SESSION_ID": "wrapped-session",
                    "DSH_SC_SHELL_ID": "37",
                },
            )
            assert partial == {
                "error": "sc-shell-identity: refusing partial ToolExecution identity"
            }


def test_admitted_tool_snapshot_survives_close_while_new_root_refuses() -> None:
    with tempfile.TemporaryDirectory() as raw:
        registry, worktree = registry_fixture(Path(raw))
        contract_generation = synthetic_health(registry)
        create_binding(
            registry,
            worktree,
            session_id="immutable-tool-root",
            conversation_id="immutable-tool-conversation",
            shell_id=38,
            shortname="IMMUTABLE",
            token="immutable-secret",
        )
        record = registry.resolve_record("immutable-tool-root")
        environment = {
            "DSH_SESSION_ID": "immutable-tool-root",
            "DSH_SC_SHELL_ID": "38",
            "DSH_SC_SHELL_SHORTNAME": "IMMUTABLE",
            "DSH_SC_SHELL_WORKTREE": str(worktree),
            "DSH_SC_API_BASE": "http://127.0.0.1:8837",
            "DSH_SC_MEM_CREDENTIAL_FILE": record["credential_file"],
            "DSH_SC_BINDING_GENERATION": "1",
            "DSH_SC_PLUGIN_HEALTH_GENERATION": contract_generation,
        }
        admitted = deepseek_execution_domain._binding_snapshot(
            registry_path=registry.layout.registry,
            fork_id=registry.layout.fork_id,
            profile_id=registry.layout.profile_id,
            environment=environment,
        )
        descriptor = os.memfd_create(
            "immutable-tool-snapshot", os.MFD_ALLOW_SEALING
        )
        with os.fdopen(descriptor, "rb", closefd=True) as descriptor_file:
            metadata = os.fstat(descriptor_file.fileno())
            frozen = {
                "contract": deepseek_execution_domain.CONTRACT,
                "cgroup": "/proof/sc-dsh/" + "a" * 32 + ".scope",
                "domain_id": "a" * 32,
                "descriptor_device": metadata.st_dev,
                "descriptor_inode": metadata.st_ino,
                "expires_monotonic_ns": time.monotonic_ns() + 60_000_000_000,
                "non_delegated": True,
                "cgroup_device": 1,
                "cgroup_inode": 2,
                "cgroup_owner_uid": os.geteuid(),
                "issuer_pid": os.getpid(),
                "issuer_start_ticks": process_start_ticks(os.getpid()),
                "host_pid": os.getpid(),
                "host_start_ticks": process_start_ticks(os.getpid()),
                "root_pid": os.getpid(),
                "root_start_ticks": process_start_ticks(os.getpid()),
                "fork_id": registry.layout.fork_id,
                "profile_id": registry.layout.profile_id,
                **admitted,
            }
            deepseek_execution_domain._seal_descriptor(
                descriptor_file.fileno(), frozen
            )
            snapshot = registry.read_snapshot()
            closed = registry.begin_close(
                expected_snapshot_generation=snapshot["snapshot_generation"],
                root_session_id="immutable-tool-root",
                expected_record_generation=record["record_generation"],
            )
            assert closed.state == "closing"

            with pytest.raises(
                deepseek_execution_domain.ExecutionDomainError
            ) as new_root:
                deepseek_execution_domain._binding_snapshot(
                    registry_path=registry.layout.registry,
                    fork_id=registry.layout.fork_id,
                    profile_id=registry.layout.profile_id,
                    environment=environment,
                )
            assert str(new_root.value) == "ToolExecution binding is not active"

            retained = dsh_execution_provenance._sealed_descriptor(
                descriptor_file.fileno()
            )
            assert retained == frozen
            assert retained["root_session_id"] == "immutable-tool-root"
            assert retained["binding_record_generation"] == 1
            assert retained["plugin_contract_generation"] == contract_generation


@pytest.mark.parametrize("tool", ["bash", "pwsh"])
def test_real_admitted_tool_execution_survives_close_while_new_root_refuses(
    tool: str,
) -> None:
    cgroup_root = Path("/sys/fs/cgroup")
    scope_launcher = Path("/usr/bin/systemd-run")
    if not scope_launcher.is_file():
        pytest.skip("the supported Linux delegated-scope launcher is unavailable")
    executable = shutil.which(tool)
    if executable is None:
        pytest.skip(f"{tool} is unavailable on this Linux seat")

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        registry, worktree = registry_fixture(root)
        contract_generation = synthetic_health(registry)
        session_id = f"immutable-{tool}-root"
        create_binding(
            registry,
            worktree,
            session_id=session_id,
            conversation_id=f"immutable-{tool}-conversation",
            shell_id=39 if tool == "bash" else 40,
            shortname=f"IMMUTABLE_{tool.upper()}",
            token=f"immutable-{tool}-secret",
        )
        record = registry.resolve_record(session_id)
        environment = {
            **os.environ,
            "DSH_SESSION_ID": session_id,
            "DSH_SC_SHELL_ID": str(record["shell_id"]),
            "DSH_SC_SHELL_SHORTNAME": str(record["shell_shortname"]),
            "DSH_SC_SHELL_WORKTREE": str(record["shell_worktree"]),
            "DSH_SC_API_BASE": str(record["api_base"]),
            "DSH_SC_MEM_CREDENTIAL_FILE": str(record["credential_file"]),
            "DSH_SC_BINDING_GENERATION": str(record["record_generation"]),
            "DSH_SC_PLUGIN_HEALTH_GENERATION": contract_generation,
        }
        ready = root / f"{tool}-ready"
        release = root / f"{tool}-release"
        effect = root / f"{tool}-effect"
        refused_effect = root / f"{tool}-refused-effect"
        environment.update(
            {
                "SC_TEST_READY": str(ready),
                "SC_TEST_RELEASE": str(release),
                "SC_TEST_EFFECT": str(effect),
            }
        )
        if tool == "bash":
            command = [
                executable,
                "-c",
                (
                    'printf ready > "$SC_TEST_READY"; '
                    'while [ ! -e "$SC_TEST_RELEASE" ]; do sleep 0.01; done; '
                    'printf bash > "$SC_TEST_EFFECT"'
                ),
            ]
        else:
            command = [
                executable,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "[IO.File]::WriteAllText($env:SC_TEST_READY, 'ready'); "
                    "while (-not [IO.File]::Exists($env:SC_TEST_RELEASE)) { "
                    "Start-Sleep -Milliseconds 10 }; "
                    "[IO.File]::WriteAllText($env:SC_TEST_EFFECT, 'pwsh')"
                ),
            ]

        launcher = [
            str(scope_launcher),
            "--user",
            "--scope",
            "--quiet",
            "--collect",
            "--property=Delegate=yes",
            "--",
            str(SCRIPTS / "deepseek_execution_domain.py"),
            "--fork-id",
            registry.layout.fork_id,
            "--profile-id",
            registry.layout.profile_id,
            "--registry",
            str(registry.layout.registry),
            "--host-identity",
            str(registry.layout.host_identity),
            "--cgroup-root",
            str(cgroup_root),
            "--domain-id",
            uuid.uuid4().hex,
            "--descriptor-fd",
            "198",
            "--ttl-seconds",
            "60",
            "--",
            *command,
        ]
        with subprocess.Popen(
            launcher,
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as admitted:
            try:
                wait_for(ready.exists)
                assert ready.read_text() == "ready"
                snapshot = registry.read_snapshot()
                closed = registry.begin_close(
                    expected_snapshot_generation=snapshot["snapshot_generation"],
                    root_session_id=session_id,
                    expected_record_generation=record["record_generation"],
                )
                assert closed.state == "closing"
                time.sleep(0.1)
                release.touch()
                stdout, stderr = admitted.communicate(timeout=10)
                assert admitted.returncode == 0, stderr
                assert stdout == ""
                assert effect.read_text() == tool
            finally:
                release.touch(exist_ok=True)
                if admitted.poll() is None:
                    admitted.terminate()
                    admitted.wait(timeout=5)

        refused_command = [
            executable,
            "-c" if tool == "bash" else "-Command",
            (
                f"printf refused > {refused_effect}"
                if tool == "bash"
                else f"[IO.File]::WriteAllText('{refused_effect}', 'refused')"
            ),
        ]
        command_boundary = len(launcher) - len(command)
        refused = subprocess.run(
            [*launcher[:command_boundary], *refused_command],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert refused.returncode == 126
        assert "ToolExecution binding is not active" in refused.stderr
        assert refused_effect.exists() is False


def test_execution_domain_uses_linux_seal_uapi_when_python_omits_names() -> None:
    program = f"""
import fcntl
import os
import runpy
for name in (
    'F_ADD_SEALS', 'F_GET_SEALS', 'F_SEAL_SEAL', 'F_SEAL_SHRINK',
    'F_SEAL_GROW', 'F_SEAL_WRITE',
):
    if hasattr(fcntl, name):
        delattr(fcntl, name)
module = runpy.run_path({str(SCRIPTS / 'deepseek_execution_domain.py')!r})
descriptor = os.memfd_create('seal-fallback', os.MFD_ALLOW_SEALING)
module['_seal_descriptor'](descriptor, {{'probe': 'python-without-seal-names'}})
assert fcntl.fcntl(descriptor, module['F_GET_SEALS']) & module['REQUIRED_SEALS'] == 15
"""
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_provenance_uses_linux_seal_uapi_when_python_omits_names() -> None:
    program = f"""
import fcntl
import json
import os
import runpy
for name in (
    'F_GET_SEALS', 'F_SEAL_SEAL', 'F_SEAL_SHRINK', 'F_SEAL_GROW',
    'F_SEAL_WRITE',
):
    if hasattr(fcntl, name):
        delattr(fcntl, name)
module = runpy.run_path({str(SCRIPTS / 'dsh_execution_provenance.py')!r})
descriptor = os.memfd_create('provenance-seal-fallback', os.MFD_ALLOW_SEALING)
value = {{
    'contract': module['CONTRACT'],
    'cgroup': None,
    'domain_id': None,
    'binding_generation': None,
    'expires_monotonic_ns': None,
    'non_delegated': None,
    'issuer_key_id': None,
    'root_pid': None,
    'root_start_ticks': None,
    'cgroup_device': None,
    'cgroup_inode': None,
    'cgroup_owner_uid': None,
    'signature': None,
}}
os.write(descriptor, json.dumps(value).encode())
try:
    module['_sealed_descriptor'](descriptor)
except ValueError as exc:
    assert str(exc) == 'execution descriptor is not immutably sealed'
else:
    raise AssertionError('unsealed descriptor was accepted')
fcntl.fcntl(descriptor, 1033, module['REQUIRED_SEALS'])
assert module['_sealed_descriptor'](descriptor) == value
"""
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_production_domain_issue_verify_marker_deletion_and_teardown() -> None:
    cgroup_root = Path("/sys/fs/cgroup")
    membership_rows = [
        fields[2]
        for row in Path("/proc/self/cgroup").read_text().splitlines()
        if len(fields := row.split(":", 2)) == 3
        and fields[0] == "0"
        and fields[1] == ""
    ]
    if len(membership_rows) != 1:
        pytest.skip("unified cgroup-v2 membership is unavailable")
    probe = (
        cgroup_root
        / membership_rows[0].lstrip("/")
        / f"sc-dsh-test-probe-{os.getpid()}"
    )
    try:
        probe.mkdir()
        probe.rmdir()
    except OSError as exc:
        pytest.skip(f"current Linux seat has no delegated cgroup subtree: {exc}")

    with tempfile.TemporaryDirectory() as raw:
        registry, worktree = registry_fixture(Path(raw))
        synthetic_health(registry)
        create_binding(
            registry,
            worktree,
            session_id="production-domain",
            conversation_id="production-conversation",
            shell_id=73,
            shortname="DOMAIN",
            token="domain-secret",
        )
        record = registry.resolve_record("production-domain")
        environment = os.environ.copy()
        environment.update(
            {
                "DSH_SESSION_ID": "production-domain",
                "DSH_SC_SHELL_ID": str(record["shell_id"]),
                "DSH_SC_SHELL_SHORTNAME": record["shell_shortname"],
                "DSH_SC_SHELL_WORKTREE": record["shell_worktree"],
                "DSH_SC_API_BASE": record["api_base"],
                "DSH_SC_MEM_CREDENTIAL_FILE": record["credential_file"],
                "DSH_SC_BINDING_GENERATION": str(record["record_generation"]),
                "DSH_SC_PLUGIN_HEALTH_GENERATION": record[
                    "plugin_contract_generation"
                ],
            }
        )
        child_program = f"""
import fcntl
import json
import os
import tempfile
from pathlib import Path
import sys
sys.path.insert(0, {str(SCRIPTS)!r})
from dsh_execution_provenance import REQUIRED_SEALS, resolve_linux

issuer = Path({str(registry.layout.host_identity)!r})
managed = resolve_linux(descriptor_fd=198, issuer_identity=issuer)
assert managed.context is not None
for name in list(os.environ):
    if name == 'DSH_SHELL' or name.startswith('DSH_SC_'):
        os.environ.pop(name)
without_markers = resolve_linux(descriptor_fd=198, issuer_identity=issuer)
payload = os.pread(198, 65537, 0)
copied_fd = os.memfd_create('copied-domain', os.MFD_ALLOW_SEALING)
os.write(copied_fd, payload)
fcntl.fcntl(copied_fd, getattr(fcntl, 'F_ADD_SEALS', 1033), REQUIRED_SEALS)
copied = resolve_linux(descriptor_fd=copied_fd, issuer_identity=issuer)
forged_value = json.loads(payload)
forged_fd = os.memfd_create('forged-domain', os.MFD_ALLOW_SEALING)
forged_stat = os.fstat(forged_fd)
forged_value['descriptor_device'] = forged_stat.st_dev
forged_value['descriptor_inode'] = forged_stat.st_ino
forged_value['binding_record_generation'] += 1
os.write(forged_fd, json.dumps(forged_value, sort_keys=True, separators=(',', ':')).encode())
fcntl.fcntl(forged_fd, getattr(fcntl, 'F_ADD_SEALS', 1033), REQUIRED_SEALS)
forged = resolve_linux(descriptor_fd=forged_fd, issuer_identity=issuer)
stale = resolve_linux(
    descriptor_fd=198,
    issuer_identity=issuer,
    now_monotonic_ns=10**30,
)
with tempfile.TemporaryDirectory() as raw:
    fake = Path(raw) / 'cgroup'
    fake.write_text('0::/native.slice\\n')
    wrong_domain = resolve_linux(
        proc_cgroup=fake,
        descriptor_fd=198,
        issuer_identity=issuer,
    )
with tempfile.TemporaryDirectory() as raw:
    dead_issuer = resolve_linux(
        descriptor_fd=198,
        issuer_identity=issuer,
        process_root=Path(raw),
    )
domain_path = Path('/sys/fs/cgroup') / managed.context.cgroup.lstrip('/')
print(json.dumps({{
    'cgroup': managed.context.cgroup,
    'domain_id': managed.context.domain_id,
    'root_session_id': managed.context.root_session_id,
    'shell_id': managed.context.shell_id,
    'binding_record_generation': managed.context.binding_record_generation,
    'managed': managed.provenance,
    'without_markers': without_markers.provenance,
    'copied': copied.provenance,
    'copied_reason': copied.reason,
    'forged': forged.provenance,
    'forged_reason': forged.reason,
    'stale': stale.provenance,
    'wrong_domain': wrong_domain.provenance,
    'dead_issuer': dead_issuer.provenance,
    'domain_mode': domain_path.stat().st_mode & 0o777,
    'cgroup_procs_mode': (domain_path / 'cgroup.procs').stat().st_mode & 0o777,
}}))
"""
        domain_id = uuid.uuid4().hex
        completed = subprocess.run(
            [
                str(SCRIPTS / "deepseek_execution_domain.py"),
                "--fork-id",
                registry.layout.fork_id,
                "--profile-id",
                registry.layout.profile_id,
                "--registry",
                str(registry.layout.registry),
                "--host-identity",
                str(registry.layout.host_identity),
                "--cgroup-root",
                str(cgroup_root),
                "--domain-id",
                domain_id,
                "--descriptor-fd",
                "198",
                "--ttl-seconds",
                "60",
                "--",
                sys.executable,
                "-c",
                child_program,
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        receipt = json.loads(completed.stdout)
        assert receipt == {
            "cgroup": receipt["cgroup"],
            "domain_id": domain_id,
            "root_session_id": "production-domain",
            "shell_id": 73,
            "binding_record_generation": 1,
            "managed": "managed",
            "without_markers": "managed",
            "copied": "unknown",
            "copied_reason": "execution descriptor file identity mismatches",
            "forged": "unknown",
            "forged_reason": "execution issuer does not hold this descriptor",
            "stale": "unknown",
            "wrong_domain": "unknown",
            "dead_issuer": "unknown",
            "domain_mode": 0o555,
            "cgroup_procs_mode": 0o444,
        }
        assert f"/sc-dsh/{domain_id}.scope" in receipt["cgroup"]
        assert not (cgroup_root / receipt["cgroup"].lstrip("/")).exists()


def test_execution_launcher_refuses_non_host_parent_and_stale_lineage() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        registry, worktree = registry_fixture(root)
        marker = root / "protected-effect"

        def environment_for(session_id: str, record: dict[str, object]):
            return {
                **os.environ,
                "DSH_SESSION_ID": session_id,
                "DSH_SC_SHELL_ID": str(record["shell_id"]),
                "DSH_SC_SHELL_SHORTNAME": str(record["shell_shortname"]),
                "DSH_SC_SHELL_WORKTREE": str(record["shell_worktree"]),
                "DSH_SC_API_BASE": str(record["api_base"]),
                "DSH_SC_MEM_CREDENTIAL_FILE": str(record["credential_file"]),
                "DSH_SC_BINDING_GENERATION": str(record["record_generation"]),
                "DSH_SC_PLUGIN_HEALTH_GENERATION": str(
                    record["plugin_contract_generation"]
                ),
            }

        def run_launcher(session_id: str, record: dict[str, object], domain_id: str):
            return subprocess.run(
                [
                    str(SCRIPTS / "deepseek_execution_domain.py"),
                    "--fork-id",
                    registry.layout.fork_id,
                    "--profile-id",
                    registry.layout.profile_id,
                    "--registry",
                    str(registry.layout.registry),
                    "--host-identity",
                    str(registry.layout.host_identity),
                    "--domain-id",
                    domain_id,
                    "--",
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).write_text('effect')",
                ],
                cwd=ROOT,
                env=environment_for(session_id, record),
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

        with sleeping_process() as unrelated_host:
            synthetic_health(registry, host_pid=unrelated_host.pid)
            create_binding(
                registry,
                worktree,
                session_id="non-host-parent",
                conversation_id="non-host-conversation",
                shell_id=81,
                shortname="NONHOST",
                token="non-host-secret",
            )
            refused = run_launcher(
                "non-host-parent",
                registry.resolve_record("non-host-parent"),
                uuid.uuid4().hex,
            )
            assert refused.returncode == 126
            assert "not a direct child of the live Host" in refused.stderr
            assert marker.exists() is False

        synthetic_health(registry)
        create_binding(
            registry,
            worktree,
            session_id="lineage-root",
            conversation_id="lineage-conversation",
            shell_id=82,
            shortname="LINEAGE",
            token="lineage-secret",
        )
        snapshot = registry.read_snapshot()
        root_record = snapshot["records"]["lineage-root"]
        registry.register_lineage(
            expected_snapshot_generation=snapshot["snapshot_generation"],
            root_session_id="lineage-root",
            child_session_id="lineage-child",
            expected_record_generation=root_record["record_generation"],
        )
        current = registry.read_snapshot()
        registry.rotate_binding(
            expected_snapshot_generation=current["snapshot_generation"],
            root_session_id="lineage-root",
            expected_record_generation=root_record["record_generation"],
            token="rotated-lineage-secret",
            plugin_contract_generation=root_record["plugin_contract_generation"],
        )
        stale_record = registry.resolve_record("lineage-root")
        stale = run_launcher("lineage-child", stale_record, uuid.uuid4().hex)
        assert stale.returncode == 126
        assert "ToolExecution lineage is stale" in stale.stderr
        assert marker.exists() is False


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


def test_terminal_tombstone_reopens_same_conversation_at_newer_epoch() -> None:
    with tempfile.TemporaryDirectory() as raw:
        registry, worktree = registry_fixture(Path(raw))
        generation = synthetic_health(registry)
        create_binding(
            registry,
            worktree,
            session_id="session-reopen",
            conversation_id="conversation-reopen",
            shell_id=405,
            shortname="SHELL_REOPEN",
            token="secret-epoch-one",
        )
        first_credential = registry.read_snapshot()["records"]["session-reopen"][
            "credential_file"
        ]
        terminalize_binding(registry, session_id="session-reopen")
        terminal = registry.read_snapshot()["records"]["session-reopen"]
        assert terminal["state"] == "terminal"
        assert terminal["record_generation"] == 3
        assert Path(first_credential).exists() is False

        reopened = registry.reopen_binding(
            expected_snapshot_generation=3,
            root_session_id="session-reopen",
            expected_record_generation=3,
            conversation_id="conversation-reopen",
            lifecycle_epoch=2,
            shell_id=405,
            shell_shortname="SHELL_REOPEN",
            shell_worktree=worktree,
            api_base="http://127.0.0.1:9948",
            token="secret-epoch-two",
            plugin_contract_generation=generation,
        )
        assert (
            reopened.operation,
            reopened.snapshot_generation,
            reopened.record_generation,
            reopened.lifecycle_epoch,
            reopened.state,
        ) == ("reopen", 4, 4, 2, "active")
        snapshot = registry.read_snapshot()
        record = snapshot["records"]["session-reopen"]
        assert record["credential_file"] != first_credential
        assert record["api_base"] == "http://127.0.0.1:9948"
        assert record["closed_at"] is None
        assert record["reopened_at"] == record["created_at"]
        assert len(record["tombstone_history"]) == 1
        tombstone = record["tombstone_history"][0]
        assert {
            key: value
            for key, value in tombstone.items()
            if key not in {"created_at", "closed_at"}
        } == {
            "state": "terminal",
            "conversation_id": "conversation-reopen",
            "lifecycle_epoch": 1,
            "record_generation": 3,
            "shell_id": 405,
            "shell_shortname": "SHELL_REOPEN",
            "shell_worktree": str(worktree.resolve()),
            "api_base": "http://127.0.0.1:8837",
            "plugin_contract_generation": generation,
            "reopened_at": None,
            "recovered_at": None,
        }
        assert isinstance(tombstone["created_at"], str)
        assert isinstance(tombstone["closed_at"], str)
        credential = json.loads(Path(record["credential_file"]).read_text())
        assert credential["token"] == "secret-epoch-two"
        assert credential["lifecycle_epoch"] == 2
        assert credential["binding_generation"] == 4
        assert registry.resolve_record("session-reopen")["lifecycle_epoch"] == 2

        committed = json.loads(json.dumps(snapshot))
        artifacts = sorted(registry.layout.credentials.glob("binding-*.json"))
        with pytest.raises(DeepSeekIdentityError) as stale:
            registry.reopen_binding(
                expected_snapshot_generation=3,
                root_session_id="session-reopen",
                expected_record_generation=3,
                conversation_id="conversation-reopen",
                lifecycle_epoch=3,
                shell_id=405,
                shell_shortname="SHELL_REOPEN",
                shell_worktree=worktree,
                api_base="http://127.0.0.1:8837",
                token="stale-secret",
                plugin_contract_generation=generation,
            )
        assert stale.value.code == "HARNESS_REGISTRY_STALE_WRITER"
        assert registry.read_snapshot() == committed
        assert sorted(registry.layout.credentials.glob("binding-*.json")) == artifacts


@pytest.mark.parametrize(
    "case",
    [
        "equal-epoch",
        "older-epoch",
        "other-conversation",
        "other-shell-id",
        "other-shortname",
        "other-worktree",
    ],
)
def test_terminal_reopen_refuses_changed_owner_or_non_newer_epoch(case: str) -> None:
    with tempfile.TemporaryDirectory() as raw:
        registry, worktree = registry_fixture(Path(raw))
        generation = synthetic_health(registry)
        initial_epoch = 2 if case == "older-epoch" else 1
        create_binding(
            registry,
            worktree,
            session_id="session-reopen-refused",
            conversation_id="conversation-reopen-refused",
            shell_id=406,
            shortname="SHELL_REFUSED",
            token="secret-before-refusal",
            lifecycle_epoch=initial_epoch,
        )
        terminalize_binding(registry, session_id="session-reopen-refused")
        other_worktree = registry.repo_root / ".sc-worktrees" / "other"
        other_worktree.mkdir()
        arguments = {
            "conversation_id": "conversation-reopen-refused",
            "lifecycle_epoch": 2,
            "shell_id": 406,
            "shell_shortname": "SHELL_REFUSED",
            "shell_worktree": worktree,
        }
        if case in {"equal-epoch", "older-epoch"}:
            arguments["lifecycle_epoch"] = 1
        elif case == "other-conversation":
            arguments["conversation_id"] = "other-conversation"
        elif case == "other-shell-id":
            arguments["shell_id"] = 999
        elif case == "other-shortname":
            arguments["shell_shortname"] = "OTHER_SHELL"
        elif case == "other-worktree":
            arguments["shell_worktree"] = other_worktree
        before = registry.read_snapshot()
        with pytest.raises(DeepSeekIdentityError) as refused:
            registry.reopen_binding(
                expected_snapshot_generation=3,
                root_session_id="session-reopen-refused",
                expected_record_generation=3,
                api_base="http://127.0.0.1:8837",
                token="secret-refused",
                plugin_contract_generation=generation,
                **arguments,
            )
        assert refused.value.code == "HARNESS_BINDING_REOPEN_REFUSED"
        assert registry.read_snapshot() == before
        assert list(registry.layout.credentials.glob("binding-*.json")) == []


def test_reopen_refuses_live_closing_and_child_lineage_records() -> None:
    with tempfile.TemporaryDirectory() as raw:
        registry, worktree = registry_fixture(Path(raw))
        generation = synthetic_health(registry)
        create_binding(
            registry,
            worktree,
            session_id="live-root",
            conversation_id="conversation-live",
            shell_id=407,
            shortname="SHELL_LIVE",
            token="secret-live",
        )

        def reopen(session_id: str, snapshot_generation: int, record_generation: int):
            return registry.reopen_binding(
                expected_snapshot_generation=snapshot_generation,
                root_session_id=session_id,
                expected_record_generation=record_generation,
                conversation_id="conversation-live",
                lifecycle_epoch=2,
                shell_id=407,
                shell_shortname="SHELL_LIVE",
                shell_worktree=worktree,
                api_base="http://127.0.0.1:8837",
                token="secret-reopen",
                plugin_contract_generation=generation,
            )

        active = registry.read_snapshot()
        with pytest.raises(DeepSeekIdentityError) as live:
            reopen("live-root", 1, 1)
        assert live.value.code == "HARNESS_BINDING_REOPEN_REFUSED"
        assert registry.read_snapshot() == active
        closing = registry.begin_close(
            expected_snapshot_generation=1,
            root_session_id="live-root",
            expected_record_generation=1,
        )
        closing_snapshot = registry.read_snapshot()
        with pytest.raises(DeepSeekIdentityError) as closing_refused:
            reopen("live-root", closing.snapshot_generation, closing.record_generation)
        assert closing_refused.value.code == "HARNESS_BINDING_REOPEN_REFUSED"
        assert registry.read_snapshot() == closing_snapshot

        registry.retire_binding(
            expected_snapshot_generation=2,
            root_session_id="live-root",
            expected_record_generation=2,
            quiesced=True,
        )
        create_binding(
            registry,
            worktree,
            session_id="lineage-root",
            conversation_id="conversation-lineage-root",
            shell_id=408,
            shortname="SHELL_LINEAGE_ROOT",
            token="secret-lineage-root",
        )
        registry.register_lineage(
            expected_snapshot_generation=4,
            root_session_id="lineage-root",
            child_session_id="lineage-child",
            expected_record_generation=1,
        )
        lineage_snapshot = registry.read_snapshot()
        with pytest.raises(DeepSeekIdentityError) as child:
            reopen("lineage-child", 5, 1)
        assert child.value.code == "HARNESS_BINDING_REOPEN_REFUSED"
        assert registry.read_snapshot() == lineage_snapshot


@pytest.mark.parametrize(
    ("crash_at", "committed", "orphan_count"),
    [
        ("before_artifact_fsync", False, 0),
        ("after_artifact_fsync", False, 1),
        ("before_registry_replace", False, 1),
        ("after_registry_replace", True, 0),
    ],
)
def test_reopen_crash_boundaries_preserve_one_authoritative_epoch(
    crash_at: str, committed: bool, orphan_count: int
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        registry, worktree = registry_fixture(Path(raw))
        generation = synthetic_health(registry)
        create_binding(
            registry,
            worktree,
            session_id="session-reopen-crash",
            conversation_id="conversation-reopen-crash",
            shell_id=409,
            shortname="SHELL_REOPEN_CRASH",
            token="secret-epoch-one",
        )
        terminalize_binding(registry, session_id="session-reopen-crash")
        with pytest.raises(SimulatedRegistryCrash) as crash:
            registry.reopen_binding(
                expected_snapshot_generation=3,
                root_session_id="session-reopen-crash",
                expected_record_generation=3,
                conversation_id="conversation-reopen-crash",
                lifecycle_epoch=2,
                shell_id=409,
                shell_shortname="SHELL_REOPEN_CRASH",
                shell_worktree=worktree,
                api_base="http://127.0.0.1:8837",
                token="secret-epoch-two",
                plugin_contract_generation=generation,
                crash_at=crash_at,
            )
        assert str(crash.value) == crash_at
        snapshot = registry.read_snapshot()
        record = snapshot["records"]["session-reopen-crash"]
        assert snapshot["snapshot_generation"] == (4 if committed else 3)
        assert record["state"] == ("active" if committed else "terminal")
        assert record["lifecycle_epoch"] == (2 if committed else 1)
        assert record["record_generation"] == (4 if committed else 3)
        recovery = registry.recover_artifacts()
        assert recovery["removed_orphans"] == orphan_count
        assert len(list(registry.layout.credentials.glob("binding-*.json"))) == (
            1 if committed else 0
        )


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
            registry.register_lineage(
                expected_snapshot_generation=1,
                root_session_id="restart-session",
                child_session_id="restart-child",
                expected_record_generation=1,
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
                expected_snapshot_generation=2,
                root_session_id="restart-session",
                expected_record_generation=1,
                token="secret-two",
                plugin_contract_generation=generation_two,
                recovery=True,
            )
            assert (recovered.snapshot_generation, recovered.record_generation) == (
                3,
                2,
            )
            aliases = second.collect("restart-session")["aliases"]
            assert aliases["DSH_SC_PLUGIN_HEALTH_GENERATION"] == generation_two
            assert aliases["DSH_SC_BINDING_GENERATION"] == "2"
            child_aliases = second.collect("restart-child")["aliases"]
            assert child_aliases["DSH_SC_BINDING_GENERATION"] == "2"
            assert child_aliases["DSH_SC_SHELL_SHORTNAME"] == "SHELL_RESTART"
            snapshot = registry.read_snapshot()
            assert snapshot["records"]["restart-session"]["recovered_at"] is not None
            assert "secret-one" not in registry.layout.registry.read_text()
            assert "secret-two" not in registry.layout.registry.read_text()


def test_abrupt_host_death_blocks_mutation_until_new_host_recovery() -> None:
    with tempfile.TemporaryDirectory() as raw:
        registry, worktree = registry_fixture(Path(raw))
        with PluginProbe(registry, boot_generation="abrupt-host-one") as first:
            generation_one = registry.read_live_health()["plugin_contract_generation"]
            create_binding(
                registry,
                worktree,
                session_id="abrupt-session",
                conversation_id="abrupt-conversation",
                shell_id=607,
                shortname="SHELL_ABRUPT",
                token="secret-abrupt-one",
            )
            before = registry.read_snapshot()
            artifacts_before = sorted(
                registry.layout.credentials.glob("binding-*.json")
            )
            assert first.process is not None
            first.process.kill()
            first.process.wait(timeout=5)
            assert json.loads(registry.layout.health.read_text())["loaded"] is True

            with pytest.raises(DeepSeekIdentityError) as unavailable:
                registry.read_live_health()
            assert unavailable.value.code == "HARNESS_PLUGIN_HEALTH_UNAVAILABLE"
            with pytest.raises(DeepSeekIdentityError) as create_refused:
                registry.create_binding(
                    expected_snapshot_generation=1,
                    root_session_id="dead-host-create",
                    conversation_id="dead-host-conversation",
                    lifecycle_epoch=1,
                    shell_id=608,
                    shell_shortname="SHELL_DEAD_CREATE",
                    shell_worktree=worktree,
                    api_base="http://127.0.0.1:8837",
                    token="secret-dead-create",
                    plugin_contract_generation=generation_one,
                )
            assert create_refused.value.code == "HARNESS_PLUGIN_HEALTH_UNAVAILABLE"
            with pytest.raises(DeepSeekIdentityError) as recovery_refused:
                registry.rotate_binding(
                    expected_snapshot_generation=1,
                    root_session_id="abrupt-session",
                    expected_record_generation=1,
                    token="secret-dead-recovery",
                    plugin_contract_generation=generation_one,
                    recovery=True,
                )
            assert recovery_refused.value.code == "HARNESS_PLUGIN_HEALTH_UNAVAILABLE"
            assert registry.read_snapshot() == before
            assert (
                sorted(registry.layout.credentials.glob("binding-*.json"))
                == artifacts_before
            )

        with PluginProbe(registry, boot_generation="abrupt-host-two") as second:
            generation_two = registry.read_live_health()["plugin_contract_generation"]
            assert generation_two != generation_one
            recovered = registry.rotate_binding(
                expected_snapshot_generation=1,
                root_session_id="abrupt-session",
                expected_record_generation=1,
                token="secret-abrupt-two",
                plugin_contract_generation=generation_two,
                recovery=True,
            )
            assert (
                recovered.operation,
                recovered.snapshot_generation,
                recovered.record_generation,
            ) == ("recover", 2, 2)
            current = registry.read_snapshot()
            current_artifacts = sorted(
                registry.layout.credentials.glob("binding-*.json")
            )
            with pytest.raises(DeepSeekIdentityError) as stale_generation:
                registry.rotate_binding(
                    expected_snapshot_generation=2,
                    root_session_id="abrupt-session",
                    expected_record_generation=2,
                    token="secret-stale-replay",
                    plugin_contract_generation=generation_one,
                    recovery=True,
                )
            assert stale_generation.value.code == "HARNESS_PLUGIN_HEALTH_MISMATCH"
            assert registry.read_snapshot() == current
            assert (
                sorted(registry.layout.credentials.glob("binding-*.json"))
                == current_artifacts
            )
            assert (
                second.collect("abrupt-session")["aliases"][
                    "DSH_SC_PLUGIN_HEALTH_GENERATION"
                ]
                == generation_two
            )


@pytest.mark.parametrize("identity_case", ["dead-pid", "wrong-start-ticks"])
def test_host_pid_identity_mismatch_refuses_without_durable_effect(
    identity_case: str,
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        registry, worktree = registry_fixture(Path(raw))
        with PluginProbe(registry, boot_generation="pid-proof-host"):
            health = json.loads(registry.layout.health.read_text())
            identity = json.loads(registry.layout.host_identity.read_text())
            if identity_case == "dead-pid":
                invalid_pid = 2_000_000_000
                health["host_pid"] = invalid_pid
                identity["host_pid"] = invalid_pid
            else:
                health["host_start_ticks"] += 1
                identity["host_start_ticks"] += 1
            owner_json(registry.layout.health, health)
            owner_json(registry.layout.host_identity, identity)
            with pytest.raises(DeepSeekIdentityError) as unavailable:
                registry.read_live_health()
            assert unavailable.value.code == "HARNESS_PLUGIN_HEALTH_UNAVAILABLE"
            with pytest.raises(DeepSeekIdentityError) as refused:
                registry.create_binding(
                    expected_snapshot_generation=0,
                    root_session_id="pid-refused-session",
                    conversation_id="pid-refused-conversation",
                    lifecycle_epoch=1,
                    shell_id=609,
                    shell_shortname="SHELL_PID_REFUSED",
                    shell_worktree=worktree,
                    api_base="http://127.0.0.1:8837",
                    token="secret-pid-refused",
                    plugin_contract_generation=health["plugin_contract_generation"],
                )
            assert refused.value.code == "HARNESS_PLUGIN_HEALTH_UNAVAILABLE"
            assert registry.read_snapshot()["snapshot_generation"] == 0
            assert list(registry.layout.credentials.glob("binding-*.json")) == []


def test_concurrent_host_restart_rejects_old_plugin_health_replay() -> None:
    with tempfile.TemporaryDirectory() as raw:
        registry, worktree = registry_fixture(Path(raw))
        with PluginProbe(registry, boot_generation="concurrent-host-one") as first:
            generation_one = registry.read_live_health()["plugin_contract_generation"]
            create_binding(
                registry,
                worktree,
                session_id="concurrent-existing-session",
                conversation_id="concurrent-existing-conversation",
                shell_id=610,
                shortname="SHELL_CONCURRENT_EXISTING",
                token="secret-concurrent-existing",
            )
            with PluginProbe(registry, boot_generation="concurrent-host-two") as second:
                generation_two = registry.read_live_health()[
                    "plugin_contract_generation"
                ]
                assert generation_two != generation_one
                health_two = json.loads(registry.layout.health.read_text())
                old_aliases = first.collect("concurrent-existing-session")["aliases"]
                assert old_aliases["DSH_SC_PLUGIN_HEALTH_GENERATION"] == generation_one
                with pytest.raises(DeepSeekIdentityError) as stale_health:
                    registry.read_live_health()
                assert stale_health.value.code == "HARNESS_PLUGIN_HEALTH_MISMATCH"
                before = registry.read_snapshot()
                artifacts = sorted(registry.layout.credentials.glob("binding-*.json"))
                with pytest.raises(DeepSeekIdentityError) as refused:
                    registry.create_binding(
                        expected_snapshot_generation=1,
                        root_session_id="concurrent-refused-session",
                        conversation_id="concurrent-refused-conversation",
                        lifecycle_epoch=1,
                        shell_id=611,
                        shell_shortname="SHELL_CONCURRENT_REFUSED",
                        shell_worktree=worktree,
                        api_base="http://127.0.0.1:8837",
                        token="secret-concurrent-refused",
                        plugin_contract_generation=generation_one,
                    )
                assert refused.value.code == "HARNESS_PLUGIN_HEALTH_MISMATCH"
                assert registry.read_snapshot() == before
                assert (
                    sorted(registry.layout.credentials.glob("binding-*.json"))
                    == artifacts
                )

                assert second.process is not None and second.process.poll() is None
                owner_json(registry.layout.health, health_two)
                restored = registry.read_live_health()
                assert restored["plugin_contract_generation"] == generation_two


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
            assert stat.S_IMODE(registry.layout.host_identity.stat().st_mode) == 0o600
            assert stat.S_IMODE(registry.layout.credentials.stat().st_mode) == 0o700
            assert credential.is_symlink() is False
            credential_payload = json.loads(credential.read_text())
            assert credential_payload["token"] == "permission-secret"
            assert credential_payload["binding_generation"] == 1
            assert credential_payload["shell_id"] == 808
            assert "permission-secret" not in registry.layout.registry.read_text()


def test_runtime_binding_tracks_exact_lifecycle_key_rotation_and_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        registry, worktree = registry_fixture(Path(raw))
        monkeypatch.setattr(deepseek_web, "_identity_registry", lambda _env: registry)
        monkeypatch.setattr(deepseek_web, "_verify_shell_identity", lambda _env: None)
        with PluginProbe(registry) as plugin:
            env = {
                "SC_API_TOKEN": "lifecycle-token-one",
                "SC_API_BASE": "http://127.0.0.1:8837",
                "SC_SHELL_ID": "909",
                "SC_SHELL_SHORTNAME": "LIFECYCLE",
            }
            first = deepseek_web.bind_session_identity(
                env=env,
                root_session_id="lifecycle-root",
                conversation_id="lifecycle-conversation",
                lifecycle_epoch=1,
                worktree=worktree,
            )
            assert first["record_generation"] == 1
            assert plugin.collect("lifecycle-root")["aliases"][
                "DSH_SC_BINDING_GENERATION"
            ] == "1"

            unchanged = deepseek_web.bind_session_identity(
                env=env,
                root_session_id="lifecycle-root",
                conversation_id="lifecycle-conversation",
                lifecycle_epoch=1,
                worktree=worktree,
            )
            assert unchanged == first
            assert registry.read_snapshot()["snapshot_generation"] == 1

            rotated_env = {**env, "SC_API_TOKEN": "lifecycle-token-two"}
            rotated = deepseek_web.bind_session_identity(
                env=rotated_env,
                root_session_id="lifecycle-root",
                conversation_id="lifecycle-conversation",
                lifecycle_epoch=1,
                worktree=worktree,
            )
            assert rotated["record_generation"] == 2
            rotated_credential = json.loads(
                Path(registry.resolve_record("lifecycle-root")["credential_file"])
                .read_text()
            )
            assert rotated_credential["token"] == "lifecycle-token-two"
            assert "lifecycle-token-one" not in registry.layout.registry.read_text()

            advanced = deepseek_web.bind_session_identity(
                env=rotated_env,
                root_session_id="lifecycle-root",
                conversation_id="lifecycle-conversation",
                lifecycle_epoch=2,
                worktree=worktree,
            )
            assert advanced["lifecycle_epoch"] == 2
            record = registry.resolve_record("lifecycle-root")
            assert record["state"] == "active"
            assert record["lifecycle_epoch"] == 2
            assert len(record["tombstone_history"]) == 1
            assert record["tombstone_history"][0]["lifecycle_epoch"] == 1

            terminal = deepseek_web.retire_session_identity(
                env=rotated_env,
                root_session_id="lifecycle-root",
                quiesced=True,
            )
            assert terminal["state"] == "terminal"
            snapshot = registry.read_snapshot()
            assert snapshot["records"]["lifecycle-root"]["state"] == "terminal"
            assert snapshot["records"]["lifecycle-root"]["credential_file"] is None
            assert list(registry.layout.credentials.glob("binding-*.json")) == []
            assert plugin.collect("lifecycle-root") == {"aliases": {}}


def test_one_shot_unknown_cancellation_closes_binding_until_terminal_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        registry, worktree = registry_fixture(root)
        synthetic_health(registry)
        create_binding(
            registry,
            worktree,
            session_id="unrelated-root",
            conversation_id="unrelated-conversation",
            shell_id=910,
            shortname="UNRELATED",
            token="unrelated-token",
        )
        unrelated_before = dict(registry.resolve_record("unrelated-root"))
        state_path = root / "deepseek-web-state.json"
        env = {
            "SC_API_TOKEN": "one-shot-token",
            "SC_API_BASE": "http://127.0.0.1:8837",
            "SC_SHELL_ID": "909",
            "SC_SHELL_SHORTNAME": "ONE-SHOT",
            "SC_SHELL_WORKTREE": str(worktree),
            "SC_DEEPSEEK_WEB_STATE": str(state_path),
        }
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(deepseek_web, "_identity_registry", lambda _env: registry)
        monkeypatch.setattr(deepseek_web, "_verify_shell_identity", lambda _env: None)
        monkeypatch.setattr(deepseek_web, "ensure", lambda *_args, **_kwargs: {})
        session_refs: list[str] = []

        class UnknownCancellation:
            def call(self, method: str, payload: object) -> object:
                assert method == "session.cancel"
                return {"accepted": False}

        def run_unknown(*_args: object, session_ref: str, **_kwargs: object) -> int:
            session_refs.append(session_ref)
            deepseek_one_shot._finalize_unknown(UnknownCancellation(), session_ref)
            raise AssertionError("unknown cancellation must refuse")

        monkeypatch.setattr(deepseek_one_shot, "_run", run_unknown)

        with pytest.raises(deepseek_host.DeepSeekHostError) as refused:
            deepseek_one_shot.run("acme-dynamic/model-7", "high", "prompt")

        assert refused.value.code == "HARNESS_ONE_SHOT_BUSY"
        assert len(session_refs) == 1
        session_ref = session_refs[0]
        marker = state_path.with_name("deepseek-shell-identity-unproven.json")
        assert not marker.exists()
        closing = registry.read_snapshot()
        record = closing["records"][session_ref]
        credential = Path(record["credential_file"])
        assert record["state"] == "closing"
        assert record["record_generation"] == 2
        assert credential.exists()
        assert registry.resolve_record("unrelated-root") == unrelated_before

        with pytest.raises(deepseek_web.DeepSeekWebError) as denied:
            deepseek_web.bind_session_identity(
                env=env,
                root_session_id=session_ref,
                conversation_id=f"one-shot:{session_ref}",
                lifecycle_epoch=1,
                worktree=worktree,
            )
        assert denied.value.code == "HARNESS_BINDING_NOT_LIVE"
        assert registry.read_snapshot() == closing

        terminal = deepseek_web.retire_session_identity(
            env=env, root_session_id=session_ref, quiesced=True
        )
        assert terminal == {
            "root_session_id": session_ref,
            "state": "terminal",
            "lifecycle_epoch": 1,
            "record_generation": 3,
        }
        terminal_record = registry.read_snapshot()["records"][session_ref]
        assert terminal_record["state"] == "terminal"
        assert terminal_record["credential_file"] is None
        assert not credential.exists()
        assert not marker.exists()
        assert registry.resolve_record("unrelated-root") == unrelated_before


def test_runtime_binding_rejects_foreign_or_stale_lifecycle_without_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        registry, worktree = registry_fixture(Path(raw))
        synthetic_health(registry)
        monkeypatch.setattr(deepseek_web, "_identity_registry", lambda _env: registry)
        monkeypatch.setattr(deepseek_web, "_verify_shell_identity", lambda _env: None)
        env = {
            "SC_API_TOKEN": "owner-token",
            "SC_API_BASE": "http://127.0.0.1:8837",
            "SC_SHELL_ID": "910",
            "SC_SHELL_SHORTNAME": "OWNER",
        }
        deepseek_web.bind_session_identity(
            env=env,
            root_session_id="owned-root",
            conversation_id="owned-conversation",
            lifecycle_epoch=2,
            worktree=worktree,
        )
        before = registry.read_snapshot()
        artifacts = sorted(registry.layout.credentials.glob("binding-*.json"))
        foreign = {
            **env,
            "SC_API_TOKEN": "foreign-token",
            "SC_SHELL_ID": "911",
            "SC_SHELL_SHORTNAME": "FOREIGN",
        }
        with pytest.raises(deepseek_web.DeepSeekWebError) as refused_owner:
            deepseek_web.bind_session_identity(
                env=foreign,
                root_session_id="owned-root",
                conversation_id="owned-conversation",
                lifecycle_epoch=2,
                worktree=worktree,
            )
        assert refused_owner.value.code == "HARNESS_BINDING_REUSE_REFUSED"
        with pytest.raises(deepseek_web.DeepSeekWebError) as refused_epoch:
            deepseek_web.bind_session_identity(
                env=env,
                root_session_id="owned-root",
                conversation_id="owned-conversation",
                lifecycle_epoch=1,
                worktree=worktree,
            )
        assert refused_epoch.value.code == "HARNESS_LIFECYCLE_STALE"
        assert registry.read_snapshot() == before
        assert sorted(registry.layout.credentials.glob("binding-*.json")) == artifacts
        credential = json.loads(Path(before["records"]["owned-root"]["credential_file"]).read_text())
        assert credential["token"] == "owner-token"
        assert "foreign-token" not in json.dumps(before)
