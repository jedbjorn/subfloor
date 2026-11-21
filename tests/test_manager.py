"""Optional host manager, exercised against owner-local disposable checkouts."""
import base64
import contextlib
import http.client
import importlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / ".super-coder/manager"


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(PACKAGE))
    return importlib.import_module("inventory"), importlib.import_module("manage")


def checkout(root, name, port=8765, runtime="host"):
    repo = root / name
    api = repo / ".super-coder/api"
    api.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (api / "server.py").write_text("")
    shutil.copy2(ROOT / "sc", repo / "sc")
    config = {"port": port, "repo": name}
    if runtime:
        config["runtime"] = runtime
    (api.parent / "instance.json").write_text(json.dumps(config))
    return repo


def request(port, path, headers=None, method="GET"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def gui(repo, port):
    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            data = {"ok": True, "port": port, "repo": repo.name, "repo_root": str(repo)}
            self.send_response(200 if self.path == "/api/health" else 404)
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", port), Health) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            thread.join()


def test_discovery_and_independent_observations(tmp_path, modules, monkeypatch):
    inv, _ = modules
    port = free_port()
    repos = tmp_path / "repos"
    running = checkout(repos, "running", port, None)
    stranger = checkout(repos, "wrong-target", port)
    stopped = checkout(repos, "stopped", free_port())
    offline = checkout(repos, "docker-offline", free_port(), "sandbox")
    unknown = checkout(repos, "ambiguous", free_port(), None)
    linked = checkout(repos, "linked")
    shutil.rmtree(linked / ".git")
    (linked / ".git").write_text("gitdir: elsewhere")
    alias = repos / "alias"
    alias.symlink_to(running, target_is_directory=True)
    (repos / "launcher-only").mkdir()
    shutil.copy2(ROOT / "sc", repos / "launcher-only/sc")
    listing = tmp_path / "publisher/urls.json"
    listing.parent.mkdir()

    def supervisor(*args):
        if args[0] == "docker":
            if args[-1] == "sc-running":
                return subprocess.CompletedProcess(args, 0, f"{running}\nrunning\n", "")
            if args[-1] == "sc-docker-offline":
                return subprocess.CompletedProcess(args, 1, "", "dial unix /run/docker.sock: no such file or directory")
            return subprocess.CompletedProcess(args, 1, "", f"Error: No such container: {args[-1]}")
        assert args[0] == "systemctl"
        return subprocess.CompletedProcess(args, 4, "LoadState=not-found\nActiveState=inactive", "")

    monkeypatch.setattr(inv, "command", supervisor)
    with gui(running, port):
        rows = {row["name"]: row for row in inv.inventory(repos, listing)}
        assert set(rows) == {r.name for r in (running, stranger, stopped, offline, unknown)}
        assert rows[running.name]["runtime"] == "docker"
        assert rows[running.name]["state"] == "running"
        assert rows[running.name]["local_url"] == f"http://127.0.0.1:{port}"
        assert rows[stranger.name]["health"] == "identity mismatch"
        assert rows[stranger.name]["local_url"] is None
        assert rows[stopped.name]["state"] == "stopped"
        assert rows[stopped.name]["publication"] == "list unavailable"
        assert rows[offline.name]["state"] == "unknown"
        assert rows[unknown.name]["runtime"] == "unknown"
        (unknown / ".super-coder/scripts").mkdir()
        (unknown / ".super-coder/scripts/runtime.py").touch()
        assert inv.supervisor(unknown, {}) == ("docker", "absent")

        monkeypatch.setattr(inv, "tailnet_name", lambda: "host.test.ts.net")
        health = inv.gui_health

        def published_probe(url, repo, cfg):
            if url.startswith("https://"):
                assert url == f"https://host.test.ts.net:{cfg['port']}"
                return "healthy" if repo == running else "unavailable"
            return health(url, repo, cfg)

        monkeypatch.setattr(inv, "gui_health", published_probe)
        listing.write_text(json.dumps([
            {"repo": running.name, "url": f"https://host.test.ts.net:{port}"},
            {"repo": stopped.name, "url": f"https://host.test.ts.net:{rows_port(stopped)}"},
            {"repo": stranger.name, "url": "https://unexpected.test"},
        ]))
        rows = {row["name"]: row for row in inv.inventory(repos, listing)}
        assert rows[running.name]["open_url"] == f"https://host.test.ts.net:{port}"
        assert rows[stranger.name]["published_url"] is None
        assert rows[stopped.name]["state"] == "stopped"
        assert rows[stopped.name]["publication"] == "unavailable"
        assert rows[stopped.name]["open_url"] is None


def rows_port(repo):
    return json.loads((repo / ".super-coder/instance.json").read_text())["port"]


def test_enable_update_reuse_and_remove_from_disposable_user(tmp_path, modules, monkeypatch):
    _, manage = modules
    for name in ("STATE", "DATA", "CONFIG"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    port = free_port()
    root = tmp_path / "my repos"
    first = checkout(root, "first", free_port())
    second = checkout(root, "second", free_port())
    manager = manage.Manager()
    process = None
    starts = []

    def stop():
        nonlocal process
        if process is not None:
            process.terminate()
            process.wait(timeout=10)
            process = None

    # The fixture supervisor runs the actual installed unit's ExecStart.
    def systemctl(*args):
        nonlocal process
        if args[0] == "restart" or (args[:2] == ("enable", "--now") and process is None):
            stop()
            line = next(line[10:] for line in manager.unit.read_text().splitlines()
                        if line.startswith("ExecStart="))
            starts.append(line)
            process = subprocess.Popen(shlex.split(line))
        elif args[0] == "disable":
            stop()

    monkeypatch.setattr(manage, "systemctl", systemctl)
    try:
        shutil.copytree(PACKAGE, first / ".super-coder/manager")
        monkeypatch.setattr(manage, "SOURCE", first / ".super-coder/manager")
        manage.main(["install", "--root", str(root), "--port", str(port)])
        cfg = manager.config()
        assert manager.settings.stat().st_mode & 0o077 == 0
        first_release = (manager.data / "current").resolve()
        # The running service remains usable when its source disappears.
        shutil.rmtree(first / ".super-coder/manager")
        secret = base64.b64encode(("operator:" + cfg["password"]).encode()).decode()
        signed = {"Authorization": "Basic " + secret}
        assert request(port, "/")[0] == 401
        assert request(port, "/api/inventory", signed | {"Host": "other.test"})[0] == 403
        assert request(port, "/api/inventory", signed | {"Origin": "https://other.test"})[0] == 403
        assert request(port, "/api/inventory", signed, "POST")[0] == 403
        assert request(port, "/api/unknown", signed | {"Origin": f"http://127.0.0.1:{port}"}, "POST")[0] == 404
        assert b"Installations" in request(port, "/", signed)[1]
        for path in ("/manager.js", "/manager.css"):
            assert request(port, path, signed)[0] == 200
        status, body = request(port, "/api/inventory", signed)
        assert status == 200
        assert {r["name"] for r in json.loads(body)["installations"]} == {"first", "second"}
        shutil.copytree(PACKAGE, second / ".super-coder/manager")
        monkeypatch.setattr(manage, "SOURCE", second / ".super-coder/manager")
        manage.main(["install", "--port", str(free_port())])
        assert len(starts) == 1
        assert manager.config() == cfg
        manage.main(["update"])
        assert len(starts) == 2 and not first_release.exists()
        assert manager.config() == cfg
        manage.main(["remove"])
        assert not manager.unit.exists() and not manager.data.exists()
        assert not manager.settings.exists()
        for repo in (first, second):
            assert (repo / ".git").is_dir() and (repo / ".super-coder/instance.json").is_file()
    finally:
        stop()


def test_publish_changes_only_its_exact_mapping(tmp_path, modules, monkeypatch):
    _, manage = modules
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    manager = manage.Manager()
    manager.state.mkdir(mode=0o700)
    repos = tmp_path / "repos"
    checkout(repos, "reserved", 8008)
    cfg = {"root": str(repos), "port": 18798}
    host = "halo.test.ts.net"
    other = {f"{host}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:18799/_subfloor/8804"}}}}
    config = {"TCP": {"443": {"HTTPS": True}}, "Web": dict(other)}
    calls = []

    def tailscale(*args):
        if args == ("tailscale", "serve", "status", "--json"):
            return json.dumps(config)
        calls.append(args)
        tls = next(a.split("=")[1] for a in args if a.startswith("--https="))
        authority = f"{host}:{tls}"
        if args[-1] == "off":
            del config["TCP"][tls], config["Web"][authority]
        else:
            config["TCP"][tls] = {"HTTPS": True}
            config["Web"][authority] = {"Handlers": {"/": {"Proxy": args[-1]}}}
        return ""

    monkeypatch.setattr(manage, "run", tailscale)
    monkeypatch.setattr(manage, "tailnet_name", lambda: host)
    config["AllowFunnel"] = {f"{host}:9443": True}
    with pytest.raises(ValueError, match="already in use"):
        manager.publish(cfg, 9443)
    with pytest.raises(ValueError, match="assigned to"):
        manager.publish(cfg, 8008)
    assert not calls
    manager.publish(cfg, 18798)
    manager.publish(cfg, 18798)
    assert len(calls) == 1 and cfg["publication"] == f"{host}:18798"
    authority = cfg["publication"]
    original = config["Web"][authority]
    config["Web"][authority] = {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8989"}}}
    with pytest.raises(ValueError, match="changed"):
        manager.unpublish(cfg)
    assert len(calls) == 1
    config["Web"][authority] = original
    manager.unpublish(cfg)
    assert config["Web"] == other and config["TCP"] == {"443": {"HTTPS": True}}
    assert "publication" not in manager.config()


def test_manager_browser_refresh_and_navigation(tmp_path, modules, monkeypatch):
    """Run real manager assets, HTTP auth and inventory in a clean browser profile."""
    _, manage = modules
    playwright = ROOT / ".super-coder/browser/node_modules/playwright"
    chromium = shutil.which("chromium")
    if not (playwright.is_dir() and chromium and shutil.which("node")):
        pytest.skip("optional local Chromium seat")
    fixture_bin = tmp_path / "bin"
    fixture_bin.mkdir()
    for name in ("systemctl", "docker"):
        cmd = fixture_bin / name
        cmd.write_text("#!/bin/sh\necho LoadState=not-found\nexit 4\n")
        cmd.chmod(0o700)
    monkeypatch.setenv("PATH", f"{fixture_bin}:{os.environ['PATH']}")
    port = free_port()
    root = tmp_path / "repos"
    gui_port = free_port()
    repo = checkout(root, "sample", gui_port)
    stopped = checkout(root, "offline", free_port())
    static = tmp_path / "package"
    shutil.copytree(PACKAGE, static)
    page = static / "static/analytics/index.html"
    page.parent.mkdir()
    page.write_text("<title>Token Analytics fixture</title>")
    settings = tmp_path / "settings.json"
    cfg = {"root": str(root), "port": port, "listing": str(tmp_path / "listing/urls.json"),
           "password": "disposable", "service_id": "fixture"}
    settings.write_text(json.dumps(cfg))
    script = """
    const {chromium} = require(process.argv[1]);
    (async () => {
      const browser = await chromium.launch({executablePath:process.argv[2], headless:true});
      try {
        const page = await browser.newPage({httpCredentials:{username:"operator",password:"disposable"}});
        await page.goto(process.argv[3]);
        const active = page.locator('tr[data-repo="sample"]');
        const offline = page.locator('tr[data-repo="offline"]');
        await active.getByRole('link', {name:"Open"}).waitFor();
        if (await active.getByRole('link').getAttribute('href') !== process.argv[4]) throw Error("wrong link");
        if (!(await offline.innerText()).includes("Stopped") || await offline.getByRole('link').count())
          throw Error("stopped row lost supervisor state");
        await page.getByRole("button", {name:"Refresh"}).click();
        await page.waitForFunction(() => !document.querySelector("#refresh").disabled);
        await page.getByRole("link", {name:"Token Analytics"}).click();
        if (await page.title() !== "Token Analytics fixture") throw Error("navigation failed");
      } finally { await browser.close(); }
    })().catch(e=>{console.error(e);process.exitCode=1});
    """
    with gui(repo, gui_port), subprocess.Popen([sys.executable, str(static / "server.py"), str(settings)]) as proc:
        try:
            manage.Manager().ready(cfg)
            result = subprocess.run(["node", "-e", script, str(playwright), chromium,
                                     f"http://127.0.0.1:{port}", f"http://127.0.0.1:{gui_port}"],
                                    capture_output=True, text=True, timeout=45, check=False)
            assert result.returncode == 0, result.stderr
        finally:
            proc.terminate()
    assert stopped.is_dir()
