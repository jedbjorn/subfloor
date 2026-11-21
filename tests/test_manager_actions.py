"""Manager confirmations, recovery and Git isolation on disposable primary checkouts."""
import importlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / ".super-coder/manager"


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


@pytest.fixture
def operations(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(PACKAGE))
    mod = importlib.import_module("repository_actions")
    root = tmp_path / "repos"
    root.mkdir()
    state = tmp_path / "manager"
    state.mkdir()
    for name in list(os.environ):
        if name.startswith(("SC_", "GIT_", "XDG_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / ".local/state"))
    monkeypatch.setenv("SC_PYTHON", sys.executable)
    for prefix in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv("GIT_" + prefix + "_NAME", "Fixture")
        monkeypatch.setenv("GIT_" + prefix + "_EMAIL", "fixture@localhost")
    return mod.Actions(root, state)


def executable(path, body):
    path.write_text("#!/bin/sh\nset -eu\n" + body)
    path.chmod(0o755)


def installation(root, name):
    repo = root / name
    repo.mkdir()
    git(repo, "init", "-b", "main")
    engine = repo / ".super-coder"
    (engine / "scripts").mkdir(parents=True)
    (engine / "scripts/remove.py").touch()
    (engine / "api").mkdir()
    (engine / "api/server.py").touch()
    (engine / "instance.json").write_text(json.dumps({"runtime": "host", "port": 17833, "instance_id": name}))
    executable(repo / "sc", """
if [ "$2" = --help ]; then
  echo "Usage: ./sc remove [--dry-run] [--yes]"
elif [ "$2" = --dry-run ]; then
  [ ! -e refuse ] || { echo 'dirty managed worktree' >&2; exit 1; }
  echo "  repository : $PWD"
  cat plan
else
  echo invoked >> calls
  echo "backup manifest: $PWD/backup/manifest.json"
  [ ! -e partial ] || { rm .super-coder/instance.json; exit 6; }
fi
""")
    (repo / "plan").write_text("fixture plan")
    return repo


def wait_result(actions, token):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        record = next(r for r in actions.history() if r["id"] == token)
        if record["status"] != "running":
            return record
        time.sleep(.05)
    pytest.fail(f"Operation did not settle: {actions.history()}")


def test_removal_preview_revalidation_and_recovery(operations, monkeypatch):
    mod = sys.modules["repository_actions"]
    monkeypatch.setattr(mod, "service_state", lambda *_: "stopped")
    repo = installation(operations.root, "selected")
    neighbor = installation(operations.root, "neighbor")
    request = {"name": repo.name, "action": "remove"}
    preview = operations.preview(request)
    assert str(repo) in preview["detail"] and not operations.history()
    with pytest.raises(ValueError, match="confirmation"):
        operations.apply({**preview, "confirmation": "REMOVE neighbor"})
    (repo / "plan").write_text("changed plan")
    with pytest.raises(ValueError, match="plan changed"):
        operations.apply({**preview, "confirmation": "REMOVE selected"})
    (repo / "refuse").touch()
    with pytest.raises(ValueError, match="dirty managed worktree"):
        operations.preview(request)
    (repo / "refuse").unlink()
    assert not (repo / "calls").exists()
    (repo / "partial").touch()
    preview = operations.preview(request)
    operations.apply({**preview, "confirmation": preview["phrase"]})
    record = wait_result(operations, preview["id"])
    assert record["status"] == "failed"
    assert record["recovery"] == [f"backup manifest: {repo}/backup/manifest.json"]
    assert str(repo / "backup/manifest.json") in operations.output(record["id"])
    assert operations.history()[0]["path"] == str(repo)
    with pytest.raises(ValueError, match="already used"):
        operations.apply({**preview, "confirmation": preview["phrase"]})
    assert (repo / "calls").read_text() == "invoked\n"
    assert not (neighbor / "calls").exists()


def test_interrupted_removal_retains_output_and_requires_a_new_preview(operations, monkeypatch):
    repo = installation(operations.root, "selected")
    launcher = repo / "sc"
    launcher.write_text(launcher.read_text().replace(
        "  [ ! -e partial ]", "  kill -TERM $$\n  [ ! -e partial ]"))
    monkeypatch.setattr(sys.modules["repository_actions"], "service_state", lambda *_: "stopped")
    preview = operations.preview({"name": repo.name, "action": "remove"})
    apply = {**preview, "confirmation": preview["phrase"]}
    operations.apply(apply)
    record = wait_result(operations, preview["id"])
    assert record["status"] == "failed" and "-15" in record["detail"]
    assert record["recovery"] == [f"backup manifest: {repo}/backup/manifest.json"]
    with pytest.raises(ValueError, match="already used"):
        operations.apply(apply)
    assert (repo / "calls").read_text() == "invoked\n"


def test_refuses_shared_instance_identity_before_dry_run(operations):
    selected = installation(operations.root, "selected")
    other = installation(operations.root, "other")
    config = ".super-coder/instance.json"
    (other / config).write_bytes((selected / config).read_bytes())
    with pytest.raises(ValueError, match="duplicate"):
        operations.preview({"name": selected.name, "action": "remove"})


def test_occupied_names_and_ports_leave_checkout_untouched(operations, monkeypatch):
    mod = sys.modules["repository_actions"]
    repo = installation(operations.root, "selected")
    monkeypatch.setattr(mod, "host_state", lambda repo: "absent")
    monkeypatch.setattr(mod, "docker_state", lambda repo, optional: "absent")
    monkeypatch.setattr(mod, "gui_health", lambda *_: "identity mismatch")
    with pytest.raises(ValueError, match="listener"):
        operations.preview({"action": "start", "name": repo.name})
    other = operations.root / "occupied"
    other.mkdir()
    git(other, "init", "-b", "main")
    with pytest.raises(ValueError, match="occupied"):
        operations.preview({"action": "create", "name": other.name, "runtime": "host", "username": "Ada"})
    assert not operations.history() and not (repo / "calls").exists()


def test_lifecycle_serializes_and_recovers_interrupted_record(operations, monkeypatch):
    repo = installation(operations.root, "target")
    mod = sys.modules["repository_actions"]
    monkeypatch.setattr(mod, "service_state", lambda *_: "running" if (repo / "up").exists() else "stopped")
    executable(repo / "sc", """
case "$1" in
 launch) while [ ! -e release ]; do sleep .05; done; touch up ;;
 restart) test "$2" = --yes; test -e up ;;
 down) rm up ;;
esac
""")
    request = {"name": repo.name, "action": "start"}
    preview = operations.preview(request)
    operations.apply(preview)
    try:
        with pytest.raises(ValueError, match="operation is running"):
            operations.preview(request)
    finally:
        (repo / "release").touch()
    assert wait_result(operations, preview["id"])["status"] == "succeeded"
    for action in ("restart", "stop"):
        preview = operations.preview({**request, "action": action})
        assert "Active sessions" in preview["detail"]
        operations.apply(preview)
        assert wait_result(operations, preview["id"])["status"] == "succeeded"
    record = operations.history()[0]
    record["status"] = "running"
    operations.save(record)
    restored = mod.Actions(operations.root, operations.directory.parent)
    assert restored.history()[0]["status"] == "interrupted"


@pytest.mark.parametrize("name", ["..", "../other", "/tmp/other", "a/b", ""])
def test_rejects_targets_outside_root(operations, name):
    with pytest.raises(ValueError):
        operations.preview({"action": "create", "name": name, "runtime": "host", "username": "Ada"})


@pytest.fixture
def installer(tmp_path, monkeypatch):
    mod = sys.modules["repository_actions"]
    bootstrap = sys.modules["provision"]
    source = tmp_path / "subfloor"
    source.mkdir()
    git(source, "init", "-b", "main")
    executable(source / "sc", """
case "$1" in
 install)
  mkdir -p .super-coder/api .super-coder/scripts shared/redlines
  touch .super-coder/api/server.py shared/.gitkeep shared/redlines/.gitkeep
  echo sc >> .gitignore
  echo "{\\"runtime\\":\\"$3\\",\\"installed_at\\":\\"now\\",\\"instance_id\\":\\"fixture\\"}" > .super-coder/instance.json
  echo install >> calls ;;
 launch)
  test ! -e deny || exit 7
  touch up; echo launch >> calls ;;
esac
""")
    git(source, "add", "sc")
    git(source, "commit", "-m", "fixture launcher")
    monkeypatch.setattr(bootstrap, "SOURCE", source.as_uri())
    monkeypatch.setattr(mod, "SOURCE", source.as_uri())
    monkeypatch.setattr(mod, "service_state", lambda repo, _: "running" if (repo / "up").exists() else "stopped")
    return source


def test_provision_resumes_and_commits_only_installation(operations, installer):
    for action, runtime in (("create", "sandbox"), ("install", "host")):
        repo = operations.root / action
        if action == "install":
            repo.mkdir()
            git(repo, "init", "-b", "main")
            (repo / "project").write_text("base")
            git(repo, "add", "project")
            git(repo, "commit", "-m", "project")
            (repo / "project").write_text("staged")
            git(repo, "add", "project")
            (repo / "project").write_text("unstaged")
            (repo / "deny").touch()
        request = {"action": action, "runtime": runtime, "name": repo.name, "username": "Ada"}
        preview = operations.preview(request)
        assert all(item in preview["detail"] for item in (str(repo), ".gitignore", "engine.ref", runtime))
        operations.apply(preview)
        record = wait_result(operations, preview["id"])
        if action == "install":
            assert record["status"] == "failed" and record["stage"] == "Launch", operations.output(record["id"])
            (repo / "deny").unlink()
            preview = operations.preview(request)
            assert "Start at: launch" in preview["detail"]
            operations.apply(preview)
            record = wait_result(operations, preview["id"])
            assert git(repo, "show", "HEAD:project") == "base"
            assert git(repo, "show", ":project") == "staged"
            assert (repo / "project").read_text() == "unstaged"
        assert record["status"] == "succeeded", record
        assert (repo / "calls").read_text().splitlines() == ["install", "launch"]
        assert git(repo, "rev-list", "--count", "HEAD") == "2"
        assert operations.preview(request) == {"existing": repo.name}


def test_browser_provisions_and_confirms_selected_removal(operations, installer, tmp_path, monkeypatch):
    chromium = shutil.which("chromium")
    playwright = ROOT / ".super-coder/browser/node_modules/playwright"
    if not (chromium and playwright.is_dir() and shutil.which("node")):
        pytest.skip("optional local Chromium seat")
    spec = importlib.util.spec_from_file_location("manager_http", PACKAGE / "server.py")
    service = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(service)
    selected = installation(operations.root, "selected")
    (selected / "partial").touch()
    monkeypatch.setattr(sys.modules["repository_actions"], "service_state",
                        lambda repo, cfg: "running" if (repo / "up").exists() else "stopped")
    with ThreadingHTTPServer(("127.0.0.1", 0), service.Handler) as server:
        server.settings = tmp_path / "settings.json"
        server.settings.write_text(json.dumps({"root": str(operations.root), "listing": str(tmp_path / "urls"),
                                               "operator_verifier": sys.modules["manage"].make_verifier("disposable")}))
        server.analytics = None
        server.actions = operations
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        js = """
        const {chromium}=require(process.argv[1]);
        (async()=>{
          const browser=await chromium.launch({executablePath:process.argv[2],headless:true});
          try {
            const page=await browser.newPage({httpCredentials:{username:"operator",password:"disposable"}});
            await page.goto(process.argv[3]);
            await page.getByRole("button",{name:"Add repository"}).click();
            await page.getByLabel("Destination").selectOption("create");
            await page.getByLabel("Repository name").fill("new-project");
            await page.getByLabel("Subfloor operator username").fill("Ada");
            await page.getByRole("button",{name:"Preview installation"}).click();
            await page.getByText(/Bootstrap commit.*engine.ref/s).waitFor();
            await page.getByRole("button",{name:"Confirm",exact:true}).click();
            await page.getByText("Create repository · succeeded",{exact:true}).waitFor();
            await page.locator('tr[data-repo="new-project"]').waitFor();
            const remove=page.locator('tr[data-repo="selected"]').getByRole("button",{name:/Remove Subfloor/});
            await remove.click();
            await page.getByText(/fixture plan/).waitFor();
            await page.getByRole("dialog").getByRole("button",{name:"Cancel"}).click();
            await remove.click();
            const confirm=page.getByRole("button",{name:"Confirm",exact:true});
            await page.getByLabel("Type REMOVE selected to confirm removal").fill("REMOVE other");
            if(await confirm.isEnabled()) throw Error("wrong confirmation accepted");
            await page.getByLabel("Type REMOVE selected to confirm removal").fill("REMOVE selected");
            await confirm.click();
            const failed=page.locator("article").filter({hasText:"Remove Subfloor from this repo · failed"});
            await failed.getByRole("button",{name:"View output"}).click();
            await page.getByRole("dialog").getByText(/backup manifest:.*selected/).waitFor();
            await page.getByRole("button",{name:"Close",exact:true}).click();
            await page.reload();
            await failed.waitFor();
            if(await page.locator('tr[data-repo="selected"]').count()) throw Error("partial target still installed");
          } finally {await browser.close();}
        })().catch(e=>{console.error(e);process.exitCode=1});
        """
        try:
            result = subprocess.run(["node", "-e", js, str(playwright), chromium,
                                     f"http://127.0.0.1:{server.server_port}"], text=True,
                                    capture_output=True, timeout=60, check=False)
            assert result.returncode == 0, result.stderr
        finally:
            server.shutdown()
            thread.join()
    assert (selected / "calls").read_text() == "invoked\n"
