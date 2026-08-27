from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".super-coder" / "scripts"
POLICY = ROOT / ".super-coder" / "assets" / "deepseek" / "dsh-shell-authority-contract.json"
sys.path.insert(0, str(SCRIPTS))

import dsh_execution_provenance as admission  # noqa: E402
import models as models_cli  # noqa: E402
import skill as skill_cli  # noqa: E402


class JsonResponse:
    def __init__(self, value: dict[str, object]) -> None:
        self.status = 200
        self.payload = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int) -> bytes:
        return self.payload


def owner_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n")
    path.chmod(0o600)


def run_tty(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float = 10,
) -> tuple[int, bytes]:
    """Run one command on a real PTY and return its exact terminal bytes."""
    master, slave = pty.openpty()
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait(timeout=5)
                raise TimeoutError(f"TTY command did not exit: {argv!r}")
            ready, _, _ = select.select([master], [], [], min(remaining, 0.1))
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b""
                if chunk:
                    output.extend(chunk)
            if process.poll() is not None:
                while True:
                    ready, _, _ = select.select([master], [], [], 0)
                    if not ready:
                        break
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                return process.returncode, bytes(output)
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def execution_context(root: Path) -> admission.ExecutionDomainContext:
    return admission.ExecutionDomainContext(
        cgroup="/fixture/sc-dsh/0123456789abcdef0123456789abcdef.scope",
        domain_id="0123456789abcdef0123456789abcdef",
        fork_id="fork-1",
        profile_id="profile-1",
        registry_snapshot_generation=9,
        execution_session_id="session-1",
        root_session_id="session-1",
        conversation_id="conversation-1",
        lifecycle_epoch=4,
        shell_id=11,
        shell_shortname="DEV5",
        shell_worktree=str(root),
        api_base="http://127.0.0.1:8837",
        credential_file=str(root / "credential.json"),
        binding_record_generation=7,
        plugin_contract_generation="plugin-contract-9",
        lineage_record_generation=None,
    )


def aliases(context: admission.ExecutionDomainContext) -> dict[str, str]:
    return {
        "DSH_SC_SHELL_ID": str(context.shell_id),
        "DSH_SC_SHELL_SHORTNAME": context.shell_shortname,
        "DSH_SC_SHELL_WORKTREE": context.shell_worktree,
        "DSH_SC_API_BASE": context.api_base,
        "DSH_SC_MEM_CREDENTIAL_FILE": context.credential_file,
        "DSH_SC_BINDING_GENERATION": str(context.binding_record_generation),
        "DSH_SC_PLUGIN_HEALTH_GENERATION": context.plugin_contract_generation,
    }


class DshCommandAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = json.loads(POLICY.read_text())

    def test_public_and_selector_policy_is_closed_at_the_exact_base(self):
        cases = {
            (): "identity_neutral_read_only",
            ("help",): "identity_neutral_read_only",
            ("mem", "which"): "dsh_shell_authorized",
            ("mem", "seed"): "dsh_shell_authorized",
            ("mem", "future-command"): "refused",
            ("feature",): "dsh_shell_authorized",
            ("feature", "list"): "dsh_shell_authorized",
            ("feature", "enable"): "refused",
            ("job", "_supervise"): "refused",
            ("job", "status"): "dsh_shell_authorized",
            ("vm", "mcp", "status"): "dsh_shell_authorized",
            ("vm", "mcp", "future-command"): "refused",
            ("vm-mcp-relay", "fg"): "refused",
            ("token",): "refused",
            ("future-route",): "refused",
        }
        observed = {
            route: admission._route_class(route, self.policy) for route in cases
        }
        self.assertEqual(observed, cases)

        adapter = json.loads(
            (ROOT / ".super-coder" / "adapters" / "deepseek" / "adapter.json").read_text()
        )
        self.assertEqual(adapter["official_runtime"]["command_admission"], {
            "bootstrap": "../sc",
            "verifier": "scripts/dsh_execution_provenance.py",
            "policy": "assets/deepseek/dsh-shell-authority-contract.json",
            "floor": "assets/deepseek/dsh-command-admission-v1.json",
            "contract": "sc-dsh-command-admission-v1",
        })

    def test_every_shell_authorized_public_row_reaches_one_closed_policy_path(self):
        selector = {
            "job": ["status"],
            "mem": ["which"],
            "pr": ["subscribe"],
            "sprint": ["inbox"],
            "visual-qa": ["run"],
            "vm": ["status"],
            "models": ["list"],
            "skill": ["list"],
            "map-extractor": ["install"],
            "vm-mcp-relay": ["status"],
        }
        routes = self.policy["public_command_policy"]["dsh_shell_authorized"]
        self.assertEqual(len(routes), 20)
        observed = {
            route: admission._route_class(
                [route, *selector.get(route, [])], self.policy
            )
            for route in routes
        }
        self.assertEqual(
            observed,
            {route: "dsh_shell_authorized" for route in routes},
        )
        self.assertEqual(
            admission._route_class(["feature"], self.policy),
            "dsh_shell_authorized",
        )

    def test_neutral_and_refused_managed_routes_have_no_identity_or_dispatch_effect(self):
        managed = admission.Resolution("managed", "fixture", None)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with (
                mock.patch.object(admission, "resolve_linux", return_value=managed),
                mock.patch.object(
                    admission,
                    "resolve_execution_identity",
                    side_effect=AssertionError("identity read"),
                ) as identity,
                mock.patch.object(
                    admission,
                    "_exec_dispatch",
                    side_effect=AssertionError("dispatch effect"),
                ) as dispatch,
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                self.assertEqual(
                    admission.admit_sc(
                        caller_root=root,
                        live_root=root,
                        policy_path=POLICY,
                        arguments=["help"],
                    ),
                    0,
                )
                self.assertEqual(
                    admission.admit_sc(
                        caller_root=root,
                        live_root=root,
                        policy_path=POLICY,
                        arguments=["token"],
                    ),
                    77,
                )
            identity.assert_not_called()
            dispatch.assert_not_called()

    def test_every_refused_public_row_has_zero_identity_and_dispatch_effects(self):
        refused = []
        for route in self.policy["public_command_policy"]["refused"]:
            refused.append([route.replace("*", "fixture")])
        refused.extend([
            ["feature", "enable"],
            ["feature", "disable"],
            ["job", "_supervise"],
            ["vm-mcp-relay", "fg"],
            ["mem", "future-command"],
            ["future-route"],
        ])
        self.assertEqual(len(refused), 77)
        managed = admission.Resolution("managed", "fixture", None)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with (
                mock.patch.object(admission, "resolve_linux", return_value=managed),
                mock.patch.object(
                    admission,
                    "resolve_execution_identity",
                    side_effect=AssertionError("identity read"),
                ) as identity,
                mock.patch.object(
                    admission,
                    "_exec_dispatch",
                    side_effect=AssertionError("dispatch effect"),
                ) as dispatch,
                mock.patch.dict(os.environ, {}, clear=True),
                contextlib.redirect_stderr(io.StringIO()) as errors,
            ):
                outcomes = [
                    admission.admit_sc(
                        caller_root=root,
                        live_root=root,
                        policy_path=POLICY,
                        arguments=route,
                    )
                    for route in refused
                ]
            self.assertEqual(outcomes, [77] * 77)
            self.assertEqual(errors.getvalue().count("refused before effect"), 77)
            identity.assert_not_called()
            dispatch.assert_not_called()

    def test_ambient_sc_selector_refuses_before_policy_identity_and_dispatch(self):
        managed = admission.Resolution("managed", "fixture", None)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with (
                mock.patch.object(admission, "resolve_linux", return_value=managed),
                mock.patch.object(
                    admission, "_load_policy", side_effect=AssertionError("policy read")
                ) as policy,
                mock.patch.object(
                    admission,
                    "resolve_execution_identity",
                    side_effect=AssertionError("identity read"),
                ) as identity,
                mock.patch.object(
                    admission,
                    "_exec_dispatch",
                    side_effect=AssertionError("dispatch effect"),
                ) as dispatch,
                mock.patch.dict(
                    os.environ,
                    {"SC_DISPATCH": "/protected/target", "DSH_SHELL": "1"},
                    clear=True,
                ),
            ):
                result = admission.admit_sc(
                    caller_root=root,
                    live_root=root,
                    policy_path=POLICY,
                    arguments=["mem", "which"],
                )
            self.assertEqual(result, 77)
            policy.assert_not_called()
            identity.assert_not_called()
            dispatch.assert_not_called()

    def test_authorized_dispatch_projects_only_the_verified_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            engine = root / ".super-coder" / "scripts"
            engine.mkdir(parents=True)
            dispatch_path = engine / "dispatch.sh"
            dispatch_path.write_text("#!/bin/sh\nexit 0\n")
            context = execution_context(root)
            identity = admission.IdentityContext(
                shell_id=11,
                shell_shortname="DEV5",
                shell_worktree=str(root),
                api_base="http://127.0.0.1:8837",
                credential_file=context.credential_file,
                binding_generation=7,
                plugin_health_generation="plugin-contract-9",
                token="secret-not-projected",
            )
            captured: list[tuple[Path, list[str], dict[str, str]]] = []

            def capture(path, arguments, environment):
                captured.append((path, list(arguments), dict(environment)))

            with (
                mock.patch.object(
                    admission,
                    "resolve_linux",
                    return_value=admission.Resolution("managed", "fixture", context),
                ),
                mock.patch.object(
                    admission, "resolve_execution_identity", return_value=identity
                ) as resolve_identity,
                mock.patch.object(admission, "_exec_dispatch", side_effect=capture),
                mock.patch.dict(os.environ, aliases(context), clear=True),
            ):
                with self.assertRaisesRegex(AssertionError, "unreachable"):
                    admission.admit_sc(
                        caller_root=root,
                        live_root=root,
                        policy_path=POLICY,
                        arguments=["mem", "which"],
                    )
            resolve_identity.assert_called_once_with(
                context=context,
                environment=mock.ANY,
            )
            self.assertEqual(len(captured), 1)
            path, arguments, environment = captured[0]
            self.assertEqual(path, dispatch_path)
            self.assertEqual(arguments, ["mem", "which"])
            self.assertEqual(environment["SC_CALLER_ROOT"], str(root))
            self.assertEqual(
                {name: environment[name] for name in admission.ALIASES},
                aliases(context),
            )
            self.assertNotIn("SC_API_TOKEN", environment)
            self.assertNotIn(identity.token, environment.values())

    def test_production_identity_binds_credential_lifecycle_and_whoami(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            context = execution_context(root)
            credential = Path(context.credential_file)
            owner_json(credential, {
                "contract": "sc-dsh-binding-credential-v1",
                "token": "fixture-token",
                "api_base": context.api_base,
                "shell_id": context.shell_id,
                "shell_shortname": context.shell_shortname,
                "root_session_id": context.root_session_id,
                "conversation_id": context.conversation_id,
                "lifecycle_epoch": context.lifecycle_epoch,
                "binding_generation": context.binding_record_generation,
                "plugin_contract_generation": context.plugin_contract_generation,
            })
            environment = aliases(context)
            with mock.patch.object(
                admission.urllib.request,
                "urlopen",
                return_value=JsonResponse({
                    "shell_id": 11,
                    "shortname": "DEV5",
                    "display_name": "Code-03",
                }),
            ) as whoami:
                identity = admission.resolve_execution_identity(
                    context=context,
                    environment=environment,
                )
            self.assertEqual(identity.shell_id, 11)
            self.assertEqual(identity.shell_shortname, "DEV5")
            self.assertEqual(identity.token, "fixture-token")
            self.assertEqual(whoami.call_count, 1)

            changed = json.loads(credential.read_text())
            changed["lifecycle_epoch"] = 5
            owner_json(credential, changed)
            with mock.patch.object(
                admission.urllib.request,
                "urlopen",
                side_effect=AssertionError("network effect"),
            ) as refused_whoami:
                with self.assertRaisesRegex(
                    ValueError, "credential artifact mismatches execution identity"
                ):
                    admission.resolve_execution_identity(
                        context=context,
                        environment=environment,
                    )
            refused_whoami.assert_not_called()

    def test_real_bootstrap_native_spoof_cannot_execute_dispatch_override(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            scripts = root / ".super-coder" / "scripts"
            assets = root / ".super-coder" / "assets" / "deepseek"
            scripts.mkdir(parents=True)
            assets.mkdir(parents=True)
            shutil.copy2(ROOT / "sc", root / "sc")
            shutil.copy2(SCRIPTS / "dsh_execution_provenance.py", scripts)
            shutil.copy2(SCRIPTS / "cli_entry.py", scripts)
            shutil.copy2(POLICY, assets)
            shutil.copy2(
                ROOT / ".super-coder" / "assets" / "deepseek"
                / "dsh-command-admission-v1.json",
                assets,
            )
            effect = root / "dispatch-effect"
            override = root / "override.sh"
            override.write_text(f"#!/bin/sh\nprintf reached > {effect}\n")
            override.chmod(0o700)
            environment = {
                name: value
                for name, value in os.environ.items()
                if not name.startswith("DSH_") and name != "SC_DISPATCH"
            }
            native_environment = dict(environment)
            native_environment["SC_DISPATCH"] = str(override)
            native = subprocess.run(
                [str(root / "sc"), "mem", "which"],
                cwd=root,
                env=native_environment,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(native.returncode, 0, native.stderr)
            self.assertEqual(effect.read_text(), "reached")
            effect.unlink()

            environment.update({
                "DSH_SHELL": "spoofed",
                "SC_DISPATCH": str(override),
            })
            completed = subprocess.run(
                [str(root / "sc"), "mem", "which"],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 77, completed.stderr)
            self.assertIn("refused before effect", completed.stderr)
            self.assertFalse(effect.exists())

            (assets / "dsh-command-admission-v1.json").unlink()
            environment["DSH_SHELL"] = ""
            fallback = subprocess.run(
                [str(root / "sc"), "mem", "which"],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(fallback.returncode, 77, fallback.stderr)
            self.assertIn("provenance contributor unavailable", fallback.stderr)
            self.assertFalse(effect.exists())

    def prepare_tty_bootstrap(self, raw: str) -> tuple[Path, dict[str, str], Path, Path]:
        root = Path(raw)
        scripts = root / ".super-coder" / "scripts"
        assets = root / ".super-coder" / "assets" / "deepseek"
        scripts.mkdir(parents=True)
        assets.mkdir(parents=True)
        shutil.copy2(ROOT / "sc", root / "sc")
        shutil.copy2(SCRIPTS / "dsh_execution_provenance.py", scripts)
        shutil.copy2(SCRIPTS / "cli_entry.py", scripts)
        shutil.copy2(POLICY, assets)
        shutil.copy2(
            ROOT / ".super-coder" / "assets" / "deepseek"
            / "dsh-command-admission-v1.json",
            assets,
        )
        dispatch_log = root / "dispatch.log"
        startup_marker = root / "bare-python-started"
        startup = root / "python-startup.py"
        startup.write_text(
            "from pathlib import Path\n"
            f"Path({str(startup_marker)!r}).write_text('bare python executed\\n')\n"
        )
        override = root / "dispatch.sh"
        override.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$DISPATCH_LOG\"\n"
            "printf 'dispatch:%s\\n' \"$*\"\n"
        )
        override.chmod(0o700)
        environment = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith("DSH_") and name != "SC_DISPATCH"
        }
        environment.update({
            "DISPATCH_LOG": str(dispatch_log),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONSTARTUP": str(startup),
            "SC_DISPATCH": str(override),
        })
        return root, environment, dispatch_log, startup_marker

    def test_real_bootstrap_tty_locator_never_executes_bare_python(self):
        with tempfile.TemporaryDirectory() as raw:
            root, environment, dispatch_log, startup_marker = (
                self.prepare_tty_bootstrap(raw)
            )
            observed = []
            for route in ("help", "health"):
                non_tty = subprocess.run(
                    [str(root / "sc"), route],
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                tty_rc, tty_bytes = run_tty(
                    [str(root / "sc"), route],
                    cwd=root,
                    env=environment,
                )
                tty_text = tty_bytes.decode().replace("\r\n", "\n")
                self.assertEqual(non_tty.returncode, 0, non_tty.stderr.decode())
                self.assertEqual(tty_rc, 0, tty_text)
                self.assertEqual(tty_text.strip(), non_tty.stdout.decode().strip())
                self.assertEqual(
                    [byte for byte in tty_bytes if byte < 32 and byte not in b"\r\n\t"],
                    [],
                )
                observed.extend((route, route))

            self.assertFalse(startup_marker.exists())
            self.assertEqual(dispatch_log.read_text().splitlines(), observed)

    @unittest.skipUnless(shutil.which("make"), "GNU Make is not installed")
    def test_real_bootstrap_tty_make_routes_dispatch_once(self):
        with tempfile.TemporaryDirectory() as raw:
            root, environment, dispatch_log, startup_marker = (
                self.prepare_tty_bootstrap(raw)
            )
            shutil.copy2(ROOT / ".super-coder" / "aliases.mk", root / ".super-coder")
            (root / "Makefile").write_text("include .super-coder/aliases.mk\n")

            expected = {
                "dos-l": "launch",
                "dos-health": "health",
                "dos-e": "enter",
            }
            for target, route in expected.items():
                rc, terminal = run_tty(
                    ["make", "--no-print-directory", target],
                    cwd=root,
                    env=environment,
                )
                text = terminal.decode().replace("\r\n", "\n")
                self.assertEqual(rc, 0, text)
                self.assertIn(f"dispatch:{route}", text)
                self.assertEqual(
                    [byte for byte in terminal if byte < 32 and byte not in b"\r\n\t"],
                    [],
                )

            self.assertFalse(startup_marker.exists())
            self.assertEqual(
                dispatch_log.read_text().splitlines(),
                ["launch", "health", "enter"],
            )

    def test_mem_adopts_the_admitted_dsh_credential_without_admin_discovery(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            credential = root / "credential.json"
            owner_json(credential, {
                "contract": "sc-dsh-binding-credential-v1",
                "token": "dsh-token",
                "api_base": "http://127.0.0.1:8837",
                "shell_id": 11,
                "shell_shortname": "DEV5",
                "root_session_id": "session-1",
                "conversation_id": "conversation-1",
                "lifecycle_epoch": 4,
                "binding_generation": 7,
                "plugin_contract_generation": "plugin-contract-9",
            })
            spec = importlib.util.spec_from_file_location(
                "mem_dsh_admission_test", SCRIPTS / "mem.py"
            )
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            with mock.patch.dict(
                os.environ,
                {"DSH_SC_MEM_CREDENTIAL_FILE": str(credential)},
                clear=True,
            ):
                spec.loader.exec_module(module)
                with mock.patch.object(
                    module,
                    "_discover_runtime_credential",
                    side_effect=AssertionError("Admin discovery"),
                ) as discovery:
                    module._require_api()
            self.assertEqual(module.SC_API_TOKEN, "dsh-token")
            self.assertEqual(module.SC_API_BASE, "http://127.0.0.1:8837")
            self.assertEqual(module._DISCOVERED_FROM, credential)
            discovery.assert_not_called()

    def test_bounded_catalogue_consumers_select_api_from_dsh_credential(self):
        for module, program in ((models_cli, "models"), (skill_cli, "skill")):
            with self.subTest(program=program):
                with (
                    mock.patch.object(module.mem, "SC_API_TOKEN", ""),
                    mock.patch.object(module.mem, "_require_api") as require_api,
                    mock.patch.dict(
                        os.environ,
                        {"DSH_SC_MEM_CREDENTIAL_FILE": "/admitted/credential.json"},
                        clear=True,
                    ),
                ):
                    self.assertIs(module._shell_api_enabled(), True)
                    self.assertEqual(module.mem._PROG, program)
                    require_api.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
