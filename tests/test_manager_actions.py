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
    monkeypatch.setattr(mod, "service_state", lambda *_: ("stopped", "no listener"))
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


@pytest.mark.parametrize("changed", ["checkout", "instance"])
def test_removal_worker_refuses_replaced_checkout(operations, monkeypatch, changed):
    mod = sys.modules["repository_actions"]
    monkeypatch.setattr(mod, "service_state", lambda *_: ("stopped", "no listener"))
    repo = installation(operations.root, "selected")
    original = operations.root / "original"
    execute = operations.execute

    def swap_then_execute(*args):
        if changed == "checkout":
            repo.rename(original)
            installation(operations.root, "selected")
        else:
            config = repo / ".super-coder/instance.json"
            config.write_text(json.dumps({**json.loads(config.read_text()), "instance_id": "replacement"}))
        execute(*args)

    monkeypatch.setattr(operations, "execute", swap_then_execute)
    preview = operations.preview({"name": repo.name, "action": "remove"})
    record = operations.apply({**preview, "confirmation": preview["phrase"]})
    assert wait_result(operations, record["id"])["status"] == "failed"
    assert "confirmed checkout changed" in operations.output(record["id"])
    assert not (original / "calls").exists() and not (repo / "calls").exists()


def test_interrupted_removal_retains_output_and_requires_a_new_preview(operations, monkeypatch):
    repo = installation(operations.root, "selected")
    launcher = repo / "sc"
    launcher.write_text(launcher.read_text().replace(
        "  [ ! -e partial ]", "  kill -TERM $$\n  [ ! -e partial ]"))
    monkeypatch.setattr(sys.modules["repository_actions"], "service_state", lambda *_: ("stopped", "no listener"))
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
    monkeypatch.setattr(mod, "service_state", lambda *_: ("running", "healthy") if (repo / "up").exists() else ("stopped", "no listener"))
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


@pytest.mark.parametrize("initial_state", ["running", "stopped"])
@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
def test_update_preserves_running_intent_and_retains_recovery(operations, monkeypatch, initial_state, outcome):
    repo = installation(operations.root, "target")
    if initial_state == "running":
        (repo / "up").touch()
    if outcome == "failed":
        (repo / "fail-update").touch()
    monkeypatch.setattr(sys.modules["repository_actions"], "service_state",
                        lambda *_: ("running", "healthy") if (repo / "up").exists() else ("stopped", "no listener"))
    executable(repo / "sc", """
if [ "$1" = update ]; then
  echo update >> calls
  echo "migrate: backed up existing DB -> $PWD/backup/preupdate.db"
  echo "  A bad update? ./sc rollback restores the DB + engine together."
  [ ! -e fail-update ] || { echo 'recover: ./sc rollback'; exit 9; }
fi
""")
    preview = operations.preview({"name": repo.name, "action": "update"})
    assert "./sc update" in preview["detail"]
    assert ("leaves it stopped" in preview["detail"]) == (initial_state == "stopped")
    operations.apply(preview)
    record = wait_result(operations, preview["id"])
    assert record["status"] == outcome, operations.output(record["id"])
    assert (repo / "up").exists() == (initial_state == "running")
    assert (repo / "calls").read_text() == "update\n"
    assert f"migrate: backed up existing DB -> {repo}/backup/preupdate.db" in record["recovery"]
    assert "recover: ./sc rollback" in record["recovery"] if outcome == "failed" else True
    output = operations.output(record["id"])
    assert "backed up existing DB" in output and "rollback" in output


def test_update_preview_revalidates_running_intent(operations, monkeypatch):
    repo = installation(operations.root, "target")
    (repo / "up").touch()
    monkeypatch.setattr(sys.modules["repository_actions"], "service_state",
                        lambda *_: ("running", "healthy") if (repo / "up").exists() else ("stopped", "no listener"))
    preview = operations.preview({"name": repo.name, "action": "update"})
    (repo / "up").unlink()
    with pytest.raises(ValueError, match="plan changed"):
        operations.apply(preview)
    assert not (repo / "calls").exists()


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
  mkdir -p .super-coder/api .super-coder/scripts
  for dir in shared shared/redlines; do
    if [ ! -d "$dir" ]; then mkdir "$dir"; touch "$dir/.gitkeep"; fi
  done
  touch .super-coder/api/server.py
  echo sc >> .gitignore
  echo "{\\"port\\":17833,\\"runtime\\":\\"$3\\",\\"installed_at\\":\\"now\\",\\"instance_id\\":\\"fixture\\"}" > .super-coder/instance.json
  echo install >> calls ;;
 launch)
  if [ -e retain ]; then touch up; exit 7; fi
  test ! -e deny || exit 7
  touch up; echo launch >> calls ;;
 restart) test -e up; rm -f retain ;;
esac
""")
    git(source, "add", "sc")
    git(source, "commit", "-m", "fixture launcher")
    monkeypatch.setattr(bootstrap, "SOURCE", source.as_uri())
    monkeypatch.setattr(mod, "SOURCE", source.as_uri())
    def state(repo):
        return "running" if (repo / "up").exists() else "absent"

    monkeypatch.setattr(mod, "host_state", lambda repo: state(repo) if mod.install_config(repo.parent, repo)["runtime"] == "host" else "absent")
    monkeypatch.setattr(mod, "docker_state", lambda repo, optional: "absent" if optional else state(repo))
    monkeypatch.setattr(mod, "gui_health", lambda url, repo, cfg:
                        "healthy" if (repo / "up").exists() and not (repo / "retain").exists() else "no listener")
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


@pytest.mark.parametrize("edit", ["index", "worktree", "both", "history", "none", "already_committed"])
def test_retry_after_interrupted_bootstrap_commit(operations, installer, monkeypatch, edit):
    mod = sys.modules["repository_actions"]
    repo = operations.root / "project"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    ignore = repo / ".gitignore"
    ignore.write_text("base\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "base")
    head = git(repo, "rev-parse", "HEAD")
    lock = repo / ".git/index.lock"
    provision = mod.provision

    def interrupt_commit(repo, state, save, run, launch):
        def locked(stage, *argv):
            if stage == "Commit bootstrap files" and edit != "already_committed":
                lock.touch()
            run(stage, *argv)
            if stage == "Commit bootstrap files":
                raise OSError("interrupted after commit")
        provision(repo, state, save, locked, launch)

    monkeypatch.setattr(mod, "provision", interrupt_commit)
    request = {"action": "install", "runtime": "host", "name": repo.name, "username": "Ada"}

    def submit():
        receipt = operations.apply(operations.preview(request))
        return wait_result(operations, receipt["id"])

    assert submit()["status"] == "failed"
    if edit != "already_committed":
        lock.unlink()
    monkeypatch.setattr(mod, "provision", provision)
    installed = ignore.read_bytes()
    if edit in ("index", "both", "history"):
        ignore.write_bytes(installed + b"staged project work\n")
        git(repo, "add", ".gitignore")
        if edit == "history":
            git(repo, "commit", "-m", "project change", "--only", "--", ".gitignore")
            head = git(repo, "rev-parse", "HEAD")
        ignore.write_bytes(installed)
    if edit in ("worktree", "both"):
        ignore.write_bytes(installed + b"unstaged project work\n")
    index = git(repo, "ls-files", "--stage")
    worktree = ignore.read_bytes()
    result = submit()
    if edit in ("none", "already_committed"):
        assert result["status"] == "succeeded", result
        assert git(repo, "show", "HEAD:.gitignore") == installed.decode().strip()
        assert git(repo, "rev-list", "--count", "HEAD") == "2"
    else:
        assert result["status"] == "failed", result
        assert git(repo, "rev-parse", "HEAD") == head
        assert git(repo, "ls-files", "--stage") == index
        assert ignore.read_bytes() == worktree


def test_retry_after_lost_install_checkpoint_preserves_unstaged_work(operations, installer, monkeypatch):
    mod = sys.modules["repository_actions"]
    repo = operations.root / "project"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    ignore = repo / ".gitignore"
    ignore.write_text("base\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "base")
    provision = mod.provision

    def lose_commit_checkpoint(repo, state, save, run, launch):
        def persist(current):
            if current["stage"] == "commit":
                raise OSError("commit checkpoint lost")
            save(current)
        provision(repo, state, persist, run, launch)

    monkeypatch.setattr(mod, "provision", lose_commit_checkpoint)
    request = {"action": "install", "runtime": "host", "name": repo.name, "username": "Ada"}

    def submit():
        receipt = operations.apply(operations.preview(request))
        return wait_result(operations, receipt["id"])

    assert submit()["status"] == "failed"
    monkeypatch.setattr(mod, "provision", provision)
    checkpoint = json.loads(operations.checkpoint(repo).read_text())
    assert checkpoint["stage"] == "install"
    assert "output" not in checkpoint and "base" not in checkpoint
    ignore.write_bytes(ignore.read_bytes() + b"unstaged project work\n")
    head = git(repo, "rev-parse", "HEAD")
    index = git(repo, "ls-files", "--stage")
    worktree = ignore.read_bytes()
    result = submit()
    assert result["status"] == "failed"
    assert "interrupted checkpoint" in result["detail"]
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "ls-files", "--stage") == index
    assert ignore.read_bytes() == worktree


@pytest.mark.parametrize("name", ["shared/.gitkeep", "shared/redlines/.gitkeep", "shared/redlines", "shared"])
def test_provision_refuses_indexed_scaffold_missing_from_worktree(operations, name):
    repo = operations.root / "project"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("staged project content")
    git(repo, "add", name)
    path.unlink()
    if path.parent != repo:
        path.parent.rmdir()
    with pytest.raises(ValueError, match="Resolve project work"):
        operations.preview({"action": "install", "name": repo.name, "runtime": "host", "username": "Ada"})
    assert git(repo, "show", ":" + name) == "staged project content"
    assert not path.exists() and (path.parent == repo or not path.parent.exists())
    assert not git(repo, "remote") and not operations.history()


def test_retry_requires_a_healthy_gui_after_failed_launch(operations, installer):
    repo = operations.root / "project"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "retain").touch()
    request = {"action": "install", "name": repo.name, "runtime": "host", "username": "Ada"}

    def submit(data):
        preview = operations.preview(data)
        operations.apply(preview)
        return wait_result(operations, preview["id"])

    failed = submit(request)
    assert failed["status"] == "failed" and "status 7" in failed["detail"]
    retry = submit(request)
    assert retry["status"] == "failed" and retry["stage"] == "Launch"
    assert "restart" in retry["detail"] and "no listener" in retry["detail"]
    assert json.loads(operations.checkpoint(repo).read_text())["stage"] == "launch"
    assert submit({"action": "restart", "name": repo.name})["status"] == "succeeded"
    assert submit(request)["status"] == "succeeded"
    assert json.loads(operations.checkpoint(repo).read_text())["stage"] == "done"
    assert (repo / "calls").read_text().splitlines() == ["install"]


def test_legacy_removal_with_project_placeholder_requires_update(operations):
    repo = installation(operations.root, "legacy")
    keep = repo / "shared/.gitkeep"
    keep.parent.mkdir()
    keep.touch()
    with pytest.raises(ValueError, match="Update this checkout"):
        operations.preview({"action": "remove", "name": repo.name})
    assert keep.exists() and not operations.history()


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
                        lambda repo, cfg: ("running", "healthy") if (repo / "up").exists() else ("stopped", "no listener"))
    with ThreadingHTTPServer(("127.0.0.1", 0), service.Handler) as server:
        server.settings = tmp_path / "settings.json"
        server.settings.write_text(json.dumps({"root": str(operations.root), "listing": str(tmp_path / "urls"),
                                               "operator_verifier": sys.modules["manage"].make_verifier("disposable")}))
        server.analytics = None
        server.browser_auth = service.BrowserAuth(server.settings, "fixture")
        server.actions = operations
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        js = """
        const {chromium}=require(process.argv[1]);
        (async()=>{
          const browser=await chromium.launch({executablePath:process.argv[2],headless:true});
          try {
            const page=await browser.newPage();
            const paired=await page.request.post(process.argv[3]+"/api/auth/pair",{
              headers:{Origin:process.argv[3]},data:{username:"operator",password:"disposable",device:"Chromium fixture"}
            });
            if(!paired.ok()) throw Error("pairing failed: "+paired.status()+" "+await paired.text());
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
            const selectedRow=page.locator('tr[data-repo="selected"]');
            const remove=async()=>{
              await selectedRow.getByRole("button",{name:"Actions for selected"}).click();
              await selectedRow.getByRole("menuitem",{name:/Remove Subfloor/}).click();
            };
            await remove();
            await page.getByText(/fixture plan/).waitFor();
            await page.getByRole("dialog").getByRole("button",{name:"Cancel"}).click();
            await remove();
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


def test_browser_refreshes_after_inflight_load_and_delayed_publication():
    chromium = shutil.which("chromium")
    playwright = ROOT / ".super-coder/browser/node_modules/playwright"
    if not (chromium and playwright.is_dir() and shutil.which("node")):
        pytest.skip("optional local Chromium seat")
    subprocess.run(["node", str(ROOT / "tests/manager_refresh.cjs"), str(playwright), chromium,
                    str(PACKAGE / "static")], timeout=40, check=True)
