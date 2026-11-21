"""Operator-confirmed operations on direct children of the configured root."""
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path

from inventory import (
    configured_runtime,
    docker_state,
    gui_health,
    host_state,
    install_config,
    installations,
)
from manage import atomic
from provision import COMMIT, SCAFFOLDS, SOURCE, identity, prepared, primary, provision

COMMANDS = {"start": ["launch"], "stop": ["down"], "restart": ["restart", "--yes"]}
PROVISION = ("install", "create")


class Conflict(ValueError):
    pass


def target(root, name):
    if (not isinstance(name, str) or not name or name in (".", "..") or Path(name).name != name
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)):
        raise ValueError("Choose a repository name directly under the configured root.")
    repo = root / name
    if repo.resolve() != repo or (repo.exists() and repo.stat().st_uid != os.getuid()):
        raise ValueError(f"Unsafe repository path: {repo}")
    return repo


def service_state(repo, cfg):
    runtime = configured_runtime(repo, cfg)
    port = cfg.get("port")
    if runtime == "unknown" or type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Cannot verify the configured runtime and port.")
    host = host_state(repo)
    docker = docker_state(repo, optional=runtime == "host")
    own, other = (host, docker) if runtime == "host" else (docker, host)
    if other not in ("absent", "stopped") or own not in ("absent", "stopped", "running", "failed"):
        raise Conflict("Cannot attribute an idle or running supervisor to this checkout. Refresh inventory.")
    health = gui_health(f"http://127.0.0.1:{port}", repo, cfg)
    if health == "no listener" and own in ("absent", "stopped", "failed"):
        return "stopped", health
    if own == "running" and health in ("healthy", "no listener", "unavailable"):
        return "running", health
    raise Conflict("The GUI listener and supervisor do not identify the selected installation.")


def verify_state(repo, expected, instance):
    # A supervisor may return before its child has exec'd or its unit has
    # been collected. Retry only the observation.
    deadline = time.monotonic() + 8
    while True:
        cfg = install_config(repo.parent, repo)
        if not cfg or cfg.get("instance_id") != instance:
            raise ValueError("The selected installation changed during the operation.")
        try:
            state, health = service_state(repo, cfg)
        except Conflict as exc:
            failure = str(exc)
        else:
            if state == expected and (expected == "stopped" or health == "healthy"):
                return
            failure = f"Command returned without a confirmed {expected} service."
            if expected == "running" and state == "running":
                failure = (f"Supervisor is running but its GUI reports {health}. "
                           "Inspect command output, restart the installation, then retry.")
        if time.monotonic() >= deadline:
            raise ValueError(failure)
        time.sleep(.25)


def command_env():
    return {**os.environ, "SC_PYTHON": sys.executable, "PYTHONUNBUFFERED": "1", "GIT_TERMINAL_PROMPT": "0"}


def installation_key(repo, cfg):
    return {"primary": primary(repo), "engine": identity(repo / ".super-coder"), "sc": identity(repo / "sc"),
            "instance": cfg.get("instance_id"), "installed_at": cfg.get("installed_at"),
            "runtime": cfg.get("runtime"), "port": cfg.get("port")}


def exec_confirmed_removal(repo, expected):
    # The worker starts this child in the selected directory. Check both
    # that held directory and the named installation, then exec relative
    # to the same cwd so a replacement cannot redirect the launch.
    cfg = install_config(repo.parent, repo)
    if (not cfg or Path.cwd() != repo or identity(Path(".")) != expected["primary"][:2]
            or installation_key(repo, cfg) != expected):
        raise ValueError("The confirmed checkout changed before removal. Preview it again.")
    os.execl("./sc", "./sc", "remove", "--yes")


def dry_run(repo):
    script = repo / ".super-coder/scripts/remove.py"
    if not script.is_file() or script.resolve() != script:
        raise ValueError("Removal is unavailable in this installed engine.")
    help_result = subprocess.run(["./sc", "remove", "--help"], cwd=repo, stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, errors="replace", timeout=10,
                                 check=False, env=command_env() | {"PYTHONDONTWRITEBYTECODE": "1"})
    if help_result.returncode or not all(opt in help_result.stdout for opt in ("--dry-run", "--yes")):
        raise ValueError("This installed engine lacks supported removal preview and confirmation.")
    result = subprocess.run(["./sc", "remove", "--dry-run"], cwd=repo, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, errors="replace", timeout=90, check=False, env=command_env() | {"PYTHONDONTWRITEBYTECODE": "1"})
    output = result.stdout + result.stderr
    if result.returncode:
        raise Conflict(output.strip() or "Engine removal preflight refused.")
    plan = {line.strip() for line in output.splitlines()}
    if f"repository : {repo}" not in plan:
        raise Conflict("The engine did not confirm the canonical removal target in its dry-run.")
    if ("scaffolds  : remove only unchanged installer-owned placeholders" not in plan
            and any(os.path.lexists(repo / name) for name in SCAFFOLDS)):
        raise Conflict("Update this checkout’s engine before removal so it can preserve project placeholders.")
    return output


class Actions:
    def __init__(self, root, state):
        self.root = root
        self.directory = state / "repository-actions"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.guard = threading.Lock()
        self.provision_lock = threading.Lock()
        self.locks = {}
        self.previews = {}
        for path in self.directory.glob("*.result"):
            record = json.loads(path.read_text())
            if record["status"] == "running":
                record["status"] = "interrupted"
                self.finish(record)

    def save(self, record):
        atomic(self.directory / (record["id"] + ".result"), json.dumps(record))

    def finish(self, record):
        record["finished"] = time.time()
        log = self.directory / (record["id"] + ".log")
        if record["action"] == "remove" and log.is_file():
            # Keep engine-reported destinations even if long output rolls out
            # of the viewer or interruption precedes the final manifest line.
            markers = ("→ backup destination ready:", "→ verified WAL-safe DB backup:",
                       "preserved backup:", "backup manifest:", "remove interrupted; backup manifest:")
            with log.open(errors="replace") as stream:
                record["recovery"] = [line.strip() for line in stream if line.strip().startswith(markers)]
        self.save(record)

    def history(self):
        records = [json.loads(path.read_text()) for path in self.directory.glob("*.result")]
        # Keep recovery evidence visible after a target disappears from inventory.
        return sorted(records, key=lambda r: r["created"], reverse=True)

    def lock(self, repo):
        with self.guard:
            return self.locks.setdefault(repo, threading.Lock())

    def checkpoint(self, repo):
        return self.directory / (hashlib.sha256(os.fsencode(repo)).hexdigest() + ".install")

    def plan(self, repo, action, data, *, allow_existing=False):
        if action in PROVISION:
            checkpoint = self.checkpoint(repo)
            stored = json.loads(checkpoint.read_text()) if checkpoint.is_file() else None
            if allow_existing and (not stored or stored["stage"] == "done") and install_config(self.root, repo):
                return None, ""
            state = prepared(repo, action, stored)
            runtime, username = data.get("runtime"), data.get("username")
            if runtime not in ("host", "sandbox") or not isinstance(username, str) or not username.strip():
                raise ValueError("Choose a runtime and enter the Subfloor operator username.")
            if "runtime" in state and (runtime, username) != (state["runtime"], state["username"]):
                raise Conflict("Resume with the originally selected runtime and operator username.")
            state.update(runtime=runtime, username=username)
            summary = (f"Target: {repo}\nRuntime: {runtime} · Operator: {username}\n"
                       f"Engine: {SOURCE} (main)\nStart at: {state['stage']}\n"
                       "Initialize empty Git history if needed. Materialize .super-coder/, install and launch.\n"
                       f'Bootstrap commit "{COMMIT}":\n' + "\n".join(state["files"]))
            return state, summary
        cfg = install_config(self.root, repo)
        if not cfg or not os.access(repo / "sc", os.X_OK):
            raise ValueError("The target is no longer a validated executable installation.")
        instance = cfg.get("instance_id")
        if instance and any(other != repo and config.get("instance_id") == instance
                            for other, config in installations(self.root)):
            raise Conflict("Another checkout uses this installation identity. Resolve the duplicate first.")
        key = installation_key(repo, cfg)
        if action == "remove":
            return key, dry_run(repo)
        state, _ = service_state(repo, cfg)
        required = "stopped" if action == "start" else "running"
        if state != required:
            raise Conflict(f"This action requires a {required} installation.")
        effect = "Active sessions will be interrupted." if action != "start" else "Starts the selected installation."
        return key, f"{repo}\n./sc {' '.join(COMMANDS[action])}\n{effect}"

    def preview(self, data):
        repo = target(self.root, data.get("name"))
        lock = self.lock(repo)
        if not lock.acquire(blocking=False):
            raise Conflict(f"An operation is running for {repo}.")
        try:
            action = data.get("action")
            if action not in (*PROVISION, *COMMANDS, "remove"):
                raise ValueError("Choose a repository operation.")
            key, detail = self.plan(repo, action, data, allow_existing=True)
            if key is None:
                return {"existing": repo.name}
            receipt = {"id": secrets.token_hex(16), "name": repo.name, "action": action,
                       "detail": detail, "phrase": f"REMOVE {repo.name}" if action == "remove" else ""}
            self.previews[repo] = (receipt, data, key, time.monotonic())
            return receipt
        finally:
            lock.release()

    def apply(self, data):
        repo = target(self.root, data.get("name"))
        with ExitStack() as leases:
            lock = self.lock(repo)
            if not lock.acquire(blocking=False):
                raise Conflict(f"An operation is running for {repo}.")
            leases.callback(lock.release)
            pending = self.previews.get(repo)
            if not pending or pending[0]["id"] != data.get("id") or time.monotonic() - pending[3] > 600:
                raise Conflict("This preview has expired or was already used. Refresh the target.")
            receipt, request, key, _ = pending
            if receipt["phrase"] and data.get("confirmation") != receipt["phrase"]:
                raise ValueError("The confirmation must match the displayed removal phrase exactly.")
            if receipt["action"] in PROVISION:
                # The installer allocates from the ports of sibling repositories.
                if not self.provision_lock.acquire(blocking=False):
                    raise Conflict("Another repository is being provisioned. Retry after it finishes.")
                leases.callback(self.provision_lock.release)
            del self.previews[repo]
            current, detail = self.plan(repo, receipt["action"], request)
            if current != key or detail != receipt["detail"]:
                raise Conflict("The target or plan changed. Preview it again.")
            if receipt["action"] == "remove":
                cfg = install_config(self.root, repo)
                if not cfg:
                    raise Conflict("The selected installation changed before removal.")
                service_state(repo, cfg)
            record = {"id": receipt["id"], "action": receipt["action"], "path": str(repo),
                      "status": "running", "stage": "Confirmed", "created": time.time(), "detail": ""}
            self.save(record)
            worker_leases = leases.pop_all()
            try:
                threading.Thread(target=self.execute, args=(repo, current, record.copy(), worker_leases),
                                 daemon=True).start()
            except RuntimeError as exc:
                worker_leases.close()
                record.update(status="failed", detail=str(exc))
                self.save(record)
                raise ValueError("Could not start the repository operation.") from exc
            return record

    def execute(self, repo, key, record, leases):
        log = self.directory / (record["id"] + ".log")

        def run(stage, *argv):
            record.update(stage=stage)
            self.save(record)
            with log.open("ab") as stream:
                process = subprocess.run(argv, cwd=repo, stdin=subprocess.DEVNULL, stdout=stream,
                                         stderr=subprocess.STDOUT, check=False,
                                         env=command_env())
            if process.returncode:
                raise ValueError(f"{stage} exited with status {process.returncode}.")

        def launch(cfg):
            record.update(stage="Launch")
            self.save(record)
            state, _ = service_state(repo, cfg)
            if state == "stopped":
                run("Launch", "./sc", "launch")
            verify_state(repo, "running", cfg.get("instance_id"))

        try:
            action = record["action"]
            if action in PROVISION:
                checkpoint = self.checkpoint(repo)

                def save(state):
                    atomic(checkpoint, json.dumps(state))

                save(key)
                provision(repo, key, save, run, launch)
            elif action == "remove":
                run("Remove Subfloor", sys.executable, "-E", "-s", "-B", __file__, str(repo), json.dumps(key))
                self.checkpoint(repo).unlink(missing_ok=True)
            else:
                run(action.capitalize(), "./sc", *COMMANDS[action])
                expected = "stopped" if action == "stop" else "running"
                verify_state(repo, expected, key["instance"])
            record["status"] = "succeeded"
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            record.update(status="failed", detail=str(exc))
        finally:
            try:
                self.finish(record)
            finally:
                leases.close()

    def output(self, token):
        if len(token) != 32 or any(ch not in "0123456789abcdef" for ch in token):
            raise ValueError("Invalid operation ID.")
        if not (self.directory / (token + ".result")).is_file():
            raise ValueError("Unknown operation.")
        path = self.directory / (token + ".log")
        if not path.exists():
            return ""
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 65536))
            return stream.read().decode(errors="replace")


if __name__ == "__main__":
    try:
        exec_confirmed_removal(Path(sys.argv[1]), json.loads(sys.argv[2]))
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))
