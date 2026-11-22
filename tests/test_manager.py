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


def checkout(root, name, port=8765, runtime="host", git_options=()):
    repo = root / name
    api = repo / ".super-coder/api"
    api.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", *git_options, str(repo)], check=True)
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
def gui(repo, port, root_status=200, *, identity_port=None, location=None):
    requests = []

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            data = {"ok": True, "port": identity_port or port, "repo": repo.name, "repo_root": str(repo)}
            health = self.path == "/api/health"
            signin = self.path.startswith("/signin.html") and (root_status in (301, 302, 303, 307, 308) or location)
            redirect = self.path == "/" and (root_status in (301, 302, 303, 307, 308) or location)
            self.send_response(200 if health or signin else 302 if location and self.path == "/" else root_status if self.path == "/" else 404)
            self.send_header("Content-Type", "application/json" if health else "text/html")
            if redirect:
                self.send_header("Location", location or "/signin.html")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode() if health else b"<!doctype html><title>Review</title>")

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", port), Health) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield requests
        finally:
            server.shutdown()
            thread.join()


def test_discovery_and_independent_observations(tmp_path, modules, monkeypatch):
    inv, _ = modules
    port = free_port()
    repos = tmp_path / "repos"
    running = checkout(repos, "running", port, "host", ("--separate-git-dir", str(tmp_path / "gitdir")))
    assert (running / ".git").is_file()
    stranger = checkout(repos, "wrong-target", port)
    stopped = checkout(repos, "stopped", free_port())
    offline = checkout(repos, "docker-offline", free_port(), "sandbox")
    unknown = checkout(repos, "ambiguous", free_port(), None)
    damaged = checkout(repos, "damaged")
    (damaged / ".super-coder/instance.json").write_bytes(b'{"repo": "\xff"}')
    subprocess.run(["git", "-C", str(running), "add", ".super-coder", "sc"], check=True)
    subprocess.run(["git", "-C", str(running), "-c", "user.name=Fixture", "-c", "user.email=test@localhost",
                    "commit", "-qm", "fixture"], check=True)
    linked = repos / "linked"
    subprocess.run(["git", "-C", str(running), "worktree", "add", "-q", "--detach", str(linked)], check=True)
    assert (linked / ".git").is_file() and (linked / ".super-coder/instance.json").is_file()
    alias = repos / "alias"
    alias.symlink_to(running, target_is_directory=True)
    (repos / "launcher-only").mkdir()
    shutil.copy2(ROOT / "sc", repos / "launcher-only/sc")
    listing = tmp_path / "publisher/urls.json"
    listing.parent.mkdir()

    real_command = inv.command

    def supervisor(*args):
        if args[0] == "git":
            return real_command(*args)
        if args[0] == "docker":
            if args[-1] == "sc-running":
                return subprocess.CompletedProcess(args, 0, f"{running}\n{running}\nrunning\n", "")
            if args[-1] == "sc-docker-offline":
                return subprocess.CompletedProcess(args, 1, "", "dial unix /run/docker.sock: no such file or directory")
            return subprocess.CompletedProcess(args, 1, "", f"Error: No such container: {args[-1]}")
        assert args[0] == "systemctl"
        return subprocess.CompletedProcess(args, 4, "LoadState=not-found\nActiveState=inactive", "")

    monkeypatch.setattr(inv, "command", supervisor)
    with gui(running, port):
        rows = {row["name"]: row for row in inv.inventory(repos, listing)}
        assert set(rows) == {r.name for r in (running, stranger, stopped, offline, unknown, damaged)}
        assert rows[damaged.name]["health"] == "invalid port"
        assert rows[damaged.name]["local_url"] is None
        assert rows[running.name]["configured_runtime"] == "host"
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
        assert inv.configured_runtime(unknown, {"port": rows_port(unknown)}) == "docker"

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


def test_inventory_without_docker_cli(tmp_path, modules, monkeypatch):
    inv, _ = modules
    root = tmp_path / "repos"
    checkout(root, "host", free_port())
    checkout(root, "sandbox", free_port(), "sandbox")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(shutil.which("git"))
    systemctl = bin_dir / "systemctl"
    systemctl.write_text("#!/bin/sh\necho LoadState=not-found\nexit 4\n")
    systemctl.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir))

    def observations():
        return {row["name"]: (row["runtime"], row["state"], row["health"])
                for row in inv.inventory(root, tmp_path / "publisher/urls.json")}

    assert observations() == {"host": ("host", "stopped", "no listener"),
                              "sandbox": ("docker", "unknown", "no listener")}

    docker = bin_dir / "docker"
    docker.write_text("#!/bin/sh\necho 'Cannot connect to the Docker daemon' >&2\nexit 1\n")
    docker.chmod(0o700)
    assert observations()["host"] == ("host", "unknown", "no listener")


def test_supervisor_attributes_live_backends_to_checkout(tmp_path, modules, monkeypatch):
    inv, _ = modules
    repo = checkout(tmp_path, "service", runtime="sandbox")
    other = checkout(tmp_path / "elsewhere", "service")
    server = repo / ".super-coder/api/server.py"
    server.write_text("import time\nprint('ready', flush=True)\ntime.sleep(60)\n")
    with subprocess.Popen([sys.executable, str(server)], stdout=subprocess.PIPE, text=True) as process:
        try:
            # Wait for exec to finish before inventory reads /proc/<pid>/cmdline.
            assert process.stdout.readline() == "ready\n"
            source = other

            def show(*args):
                if args[0] == "systemctl":
                    fields = f"LoadState=loaded\nActiveState=active\nMainPID={process.pid}"
                else:
                    assert args[0] == "docker"
                    fields = f"{source}\n{repo}\nrunning"
                return subprocess.CompletedProcess(args, 0, fields, "")

            monkeypatch.setattr(inv, "command", show)
            assert inv.supervisor(repo, "docker") == ("host", "running")
            assert inv.host_state(other) == "unknown"
            assert inv.docker_state(repo) == "unknown"
            source = repo
            assert inv.supervisor(repo, "docker") == ("unknown", "unknown")
        finally:
            process.terminate()


@pytest.mark.parametrize(("local_status", "public_status"), [
    (200, 404), (404, 200), (404, 404), (302, 200), (200, 302), (302, 302), (302, 404),
])
def test_open_checks_gui_root_at_each_authority(tmp_path, modules, monkeypatch, local_status, public_status):
    inv, _ = modules
    port = free_port()
    root = tmp_path / "repos"
    repo = checkout(root, "sample", port)
    dns = "host.test.ts.net"
    public_url = f"https://{dns}:{port}"
    listing = tmp_path / "urls.json"
    listing.write_text(json.dumps([{"repo": repo.name, "url": public_url}]))
    monkeypatch.setattr(inv, "tailnet_name", lambda: dns)
    monkeypatch.setattr(inv, "supervisor", lambda *_: ("host", "unknown"))
    with gui(repo, port, local_status):
        public_port = free_port()

        def published_connection(name, target_port, timeout):
            assert (name, target_port) == (dns, port)
            return http.client.HTTPConnection("127.0.0.1", public_port, timeout=timeout)

        monkeypatch.setattr(http.client, "HTTPSConnection", published_connection)
        with gui(repo, public_port, public_status, identity_port=port):
            row, = inv.inventory(root, listing)
    assert row["health"] == ("healthy" if local_status in (200, 302) else "unavailable")
    assert row["local_url"] == (f"http://127.0.0.1:{port}" if local_status in (200, 302) else None)
    assert row["publication"] == ("verified" if public_status in (200, 302) else "unavailable")
    assert row["open_url"] == (public_url if public_status in (200, 302) else None)


@pytest.mark.parametrize("absolute", [False, True])
def test_gui_health_accepts_only_the_same_origin_signin_flow(tmp_path, modules, absolute):
    inv, _ = modules
    port = free_port()
    repo = checkout(tmp_path, "authenticated", port)
    cfg = {"port": port}
    location = f"http://127.0.0.1:{port}/signin.html?next=/" if absolute else "/signin.html"
    with gui(repo, port, location=location) as requests:
        assert inv.gui_health(f"http://127.0.0.1:{port}", repo, cfg) == "healthy"
    assert requests == ["/api/health", "/", "/signin.html" + ("?next=/" if absolute else "")]


@pytest.mark.parametrize("location", [
    "https://unexpected.test/signin.html", "//unexpected.test/signin.html", "/account.html",
])
def test_gui_health_rejects_other_redirects_without_following(tmp_path, modules, location):
    inv, _ = modules
    port = free_port()
    repo = checkout(tmp_path, "redirected", port)
    with gui(repo, port, location=location) as requests:
        assert inv.gui_health(f"http://127.0.0.1:{port}", repo, {"port": port}) == "unavailable"
    assert requests == ["/api/health", "/"]


def stage_package(repo):
    package = repo / ".super-coder/manager"
    shutil.copytree(PACKAGE, package)
    scripts = package.parent / "scripts"
    scripts.mkdir()
    shutil.copy2(PACKAGE.parent / "scripts/instance_state.py", scripts)
    shutil.copy2(PACKAGE.parent / "api/auth_sessions.py", package / "auth_sessions.py")
    shutil.copy2(PACKAGE.parent / "api/auth_sessions.py", package.parent / "api/auth_sessions.py")
    return package


def fixture_analytics(package):
    shutil.copy2(PACKAGE.parent / "api/auth_sessions.py", package / "auth_sessions.py")
    module = package / "token_analytics"
    assets = module / "static"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "index.html").write_text('<title>Token Analytics fixture</title><script src="/analytics/app.js"></script>')
    (assets / "app.js").write_text('document.title = "Token Analytics fixture"')
    (module / "__init__.py").write_text("""
from pathlib import Path

class Analytics:
    def __init__(self, state_dir, repositories, *, engine_repo=None):
        self.state = state_dir
        self.repos = repositories
        self.engine = engine_repo

    def start(self):
        (self.state / 'started').touch()

    def close(self):
        (self.state / 'closed').touch()

    def read(self, name, query):
        import instance_state
        return dict(name=name, query=query, repos=[str(p) for p in self.repos()],
                    engine=str(self.engine), resolver=instance_state.__file__)

    def refresh(self):
        (self.state / 'refreshed').touch()
        return {'ok': True}
""")


def test_password_setup_and_reset(tmp_path, modules, monkeypatch, capsys):
    _, manage = modules
    from operator_password import matches

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    manager = manage.Manager()
    with manager.locked():
        manager.save({})
    argv = [sys.executable, str(PACKAGE / "manage.py"), "password"]
    batch = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           start_new_session=True, check=False)
    assert batch.returncode != 0 and batch.stdout == ""
    assert "interactive host terminal" in batch.stderr
    assert manager.config() == {}

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    answers = iter(["fixture-only", "mismatch", "fixture-only", "fixture-only", "new-fixture", "new-fixture"])
    monkeypatch.setattr(manage.getpass, "getpass", lambda _: next(answers))
    with pytest.raises(SystemExit):
        manage.main(["password"])
    assert manager.config() == {}

    credential = "Basic " + base64.b64encode(b"operator:fixture-only").decode()
    assert manage.main(["password"]) == 0
    assert matches(credential, manager.config()["operator_verifier"])
    assert manage.main(["password"]) == 0
    assert not matches(credential, manager.config()["operator_verifier"])
    assert "password" not in manager.config()
    output = capsys.readouterr()
    for supplied in ("fixture-only", "new-fixture"):
        assert supplied not in output.out + output.err + manager.settings.read_text()


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
        monkeypatch.setattr(manage, "SOURCE", stage_package(first))
        manage.main(["install", "--root", str(root), "--port", str(port)])
        cfg = manager.config()
        assert manager.settings.stat().st_mode & 0o077 == 0
        first_release = (manager.data / "current").resolve()
        # The running service remains usable when its source disappears.
        shutil.rmtree(first / ".super-coder/manager")
        secret = base64.b64encode(b"operator:fixture-login").decode()
        signed = {"Authorization": "Basic " + secret}
        assert request(port, "/")[0] == 302
        assert request(port, "/", signed)[0] == 401
        assert request(port, "/signin.html")[0] == 200
        cfg["operator_verifier"] = manage.make_verifier("fixture-login")
        manager.save(cfg)
        assert request(port, "/api/inventory", signed | {"Host": "other.test"})[0] == 403
        assert request(port, "/api/inventory", signed | {"Origin": "https://other.test"})[0] == 403
        assert request(port, "/api/inventory", signed, "POST")[0] == 403
        assert request(port, "/api/unknown", signed | {"Origin": f"http://127.0.0.1:{port}"}, "POST")[0] == 404
        assert b"Installations" in request(port, "/", signed)[1]
        for path in ("/manager.js", "/manager.css", "/theme.js"):
            assert request(port, path, signed)[0] == 200
        status, body = request(port, "/api/inventory", signed)
        assert status == 200
        assert {r["name"] for r in json.loads(body)["installations"]} == {"first", "second"}
        source = stage_package(second)
        fixture_analytics(source)
        monkeypatch.setattr(manage, "SOURCE", source)
        manage.main(["install", "--port", str(free_port())])
        assert len(starts) == 1
        assert manager.config() == cfg
        # A preview release stored its generated credential in the settings.
        # Update retains that login and removes the recoverable secret.
        legacy = dict(cfg, password="fixture-login")
        del legacy["operator_verifier"]
        manager.save(legacy)
        manage.main(["update"])
        assert len(starts) == 2 and not first_release.exists()
        assert "password" not in manager.config() and "fixture-login" not in manager.settings.read_text()
        current = (manager.data / "current").resolve()
        assert (manager.state / "started").exists()
        # Remove the supplier copy and add a launcher-only neighbor. The
        # collector still sees the two primary installs and its own resolver.
        shutil.rmtree(source)
        (root / "launcher-only").mkdir()
        (root / "launcher-only/sc").touch()
        for name in ("status", "filters", "sessions", "usage", "activity", "engine-updates"):
            status, body = request(port, f"/api/analytics/{name}?project=one&project=two", signed)
            assert status == 200
            assert json.loads(body) == {"name": name, "query": {"project": ["one", "two"]},
                                        "repos": [str(first), str(second)], "engine": str(second),
                                        "resolver": str(current / "instance_state.py")}
        assert request(port, "/analytics/app.js")[0] == 401
        assert request(port, "/analytics/app.js", signed)[0] == 200
        assert request(port, "/api/analytics/refresh", signed)[0] == 404
        assert request(port, "/api/analytics/refresh", signed, "POST")[0] == 403
        assert not (manager.state / "refreshed").exists()
        assert request(port, "/api/analytics/refresh", signed | {"Origin": f"http://127.0.0.1:{port}"}, "POST")[0] == 200
        assert (manager.state / "refreshed").exists()
        manage.main(["remove"])
        assert (manager.state / "closed").exists()
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
    systemctl = fixture_bin / "systemctl"
    systemctl.write_text("#!/bin/sh\necho LoadState=not-found\nexit 4\n")
    systemctl.chmod(0o700)
    monkeypatch.setenv("PATH", f"{fixture_bin}:{os.environ['PATH']}")
    port = free_port()
    root = tmp_path / "repos"
    gui_port = free_port()
    repo = checkout(root, "sample", gui_port)
    stopped = checkout(root, "offline", free_port())
    docker = fixture_bin / "docker"
    docker.write_text(f"""#!/bin/sh
for name do :; done
if [ "$name" = sc-sample ]; then
    printf '%s\\n' {shlex.quote(str(repo))} {shlex.quote(str(repo))} running
else
    echo "No such container: $name" >&2
    exit 1
fi
""")
    docker.chmod(0o700)
    static = tmp_path / "package"
    shutil.copytree(PACKAGE, static)
    fixture_analytics(static)
    settings = tmp_path / "settings.json"
    cfg = {"root": str(root), "port": port, "listing": str(tmp_path / "listing/urls.json"),
           "operator_verifier": manage.make_verifier("disposable"), "service_id": "fixture"}
    settings.write_text(json.dumps(cfg))
    script = """
    const {chromium} = require(process.argv[1]);
    (async () => {
      const browser = await chromium.launch({executablePath:process.argv[2], headless:true});
      try {
        const page = await browser.newPage();
        const paired = await page.request.post(process.argv[3]+"/api/auth/pair", {
          headers:{Origin:process.argv[3]},
          data:{username:"operator",password:"disposable",device:"Chromium fixture"}
        });
        if (!paired.ok()) throw Error("pairing failed: "+paired.status()+" "+await paired.text());
        await page.goto(process.argv[3]);
        const active = page.locator('tr[data-repo="sample"]');
        const offline = page.locator('tr[data-repo="offline"]');
        await active.getByRole('link', {name:"sample",exact:true}).waitFor();
        if (await active.getByRole('link').getAttribute('href') !== process.argv[4]) throw Error("wrong link");
        const runtime = await active.getByRole("cell").nth(1).innerText();
        if (!runtime.startsWith("Docker") || !runtime.includes("Configured for Host"))
          throw Error("runtime switch lost: " + runtime);
        if (!(await offline.innerText()).includes("Stopped") || await offline.getByRole('link').count())
          throw Error("stopped row lost supervisor state");
        const trigger=active.getByRole("button",{name:"Actions for sample"});
        await trigger.focus();
        await page.keyboard.press("ArrowDown");
        await active.getByRole("menuitem",{name:"Update Subfloor"}).waitFor();
        await page.keyboard.press("Escape");
        if(await trigger.getAttribute("aria-expanded") !== "false") throw Error("menu did not close");
        await page.getByRole("button",{name:"Light theme"}).click();
        if(await page.locator("html").getAttribute("data-theme") !== "light") throw Error("light theme failed");
        await page.getByRole("button",{name:"Dark theme"}).click();
        if(await page.locator("html").getAttribute("data-theme") !== "dark") throw Error("dark theme failed");
        await page.getByRole("button",{name:"System theme"}).click();
        if(await page.locator("html").getAttribute("data-theme-choice") !== "system") throw Error("system theme failed");
        if(await page.getByRole("button",{name:"System theme"}).getAttribute("aria-pressed") !== "true") throw Error("theme state failed");
        await page.getByRole("button",{name:"Dark theme"}).click();
        await page.setViewportSize({width:390,height:844});
        if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)) throw Error("mobile overflow");
        await trigger.click();
        const menuBox=await active.getByRole("menu").boundingBox();
        if(menuBox.x<0 || menuBox.x+menuBox.width>390) throw Error("mobile menu outside viewport");
        await page.keyboard.press("Escape");
        await page.setViewportSize({width:1440,height:1000});
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
