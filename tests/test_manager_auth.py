"""Long-lived Manager browser pairing on disposable state and browser profiles."""
import base64
import http.client
import importlib
import json
import shutil
import subprocess
import sys
import threading
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / ".super-coder/manager"


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(PACKAGE))
    browser_auth = importlib.import_module("browser_auth")
    manage = importlib.import_module("manage")
    spec = importlib.util.spec_from_file_location("manager_http_auth", PACKAGE / "server.py")
    server_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server_module)
    root = tmp_path / "repos"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    settings = state / "settings.json"
    config = {"root": str(root), "port": 0, "listing": str(tmp_path / "urls.json"),
              "operator_verifier": manage.make_verifier("fixture-password"), "service_id": "auth-fixture"}
    settings.write_text(json.dumps(config))
    with ThreadingHTTPServer(("127.0.0.1", 0), server_module.Handler) as httpd:
        httpd.settings = settings
        httpd.browser_auth = browser_auth.BrowserAuth(settings, config["service_id"])
        httpd.analytics = None
        config["port"] = httpd.server_port
        settings.write_text(json.dumps(config))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_port}"

        def request(path, *, method="GET", token=None, basic=False, payload=None,
                    host=None, origin=True):
            headers = {}
            if token:
                headers["Cookie"] = token
            if basic:
                headers["Authorization"] = "Basic " + base64.b64encode(b"operator:fixture-password").decode()
            if payload is not None:
                headers["Content-Type"] = "application/json"
                if origin:
                    headers["Origin"] = origin if isinstance(origin, str) else base
                payload = json.dumps(payload)
            if host:
                headers["Host"] = host
            connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=10)
            try:
                connection.request(method, path, body=payload, headers=headers)
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()

        def pair(device, *, password="fixture-password", host=None, origin=None):
            authority = host or f"127.0.0.1:{httpd.server_port}"
            status, headers, body = request(
                "/api/auth/pair", method="POST", host=host,
                origin=origin or (f"https://{authority}" if host else base),
                payload={"username": "operator", "password": password, "device": device})
            return status, headers, body

        try:
            yield SimpleNamespace(module=browser_auth, manage=manage, settings=settings, config=config,
                                  auth=httpd.browser_auth, base=base, request=request, pair=pair)
        finally:
            httpd.shutdown()
            thread.join()


def cookie_token(headers):
    cookie = SimpleCookie()
    cookie.load(headers["Set-Cookie"])
    name, morsel = next(iter(cookie.items()))
    return f"{name}={morsel.value}", morsel.value, headers["Set-Cookie"]


def test_pair_multiple_browsers_renew_revoke_and_sign_out(service):
    status, headers, _ = service.request("/")
    assert status == 302 and headers["Location"] == "/signin.html?next=/"
    assert "WWW-Authenticate" not in headers
    assert service.request("/signin.html")[0] == 200
    assert service.request("/api/inventory")[0] == 401
    assert service.request("/api/inventory", basic=True)[0] == 200
    assert service.pair("Wrong password", password="incorrect")[0] == 401
    assert service.pair("Cross origin", origin="https://unexpected.test")[0] == 403
    assert service.request("/api/auth/pair", method="POST", origin=False,
                           payload={"username": "operator", "password": "fixture-password"})[0] == 403

    status, headers, body = service.pair("Work laptop")
    assert status == 200 and json.loads(body)["signed_in"]
    first, first_secret, first_cookie = cookie_token(headers)
    assert "Max-Age=34560000" in first_cookie
    assert "HttpOnly" in first_cookie and "SameSite=Strict" in first_cookie
    assert "Secure" not in first_cookie
    status, headers, _ = service.pair("Phone")
    assert status == 200
    second, second_secret, _ = cookie_token(headers)
    assert first_secret != second_secret
    for token in (first, second):
        assert service.request("/api/inventory", token=token)[0] == 200
    private = service.auth.private.read_text()
    public = (service.auth.public / "sessions.json").read_text()
    for secret in (first_secret, second_secret):
        assert secret not in private and secret not in public
    assert "Work laptop" in private and "Phone" in private

    status, headers, body = service.request("/api/auth/status", token=first)
    assert status == 200 and json.loads(body)["session"]["device"] == "Work laptop"
    assert "Max-Age=34560000" in headers["Set-Cookie"]
    sessions = json.loads(service.request("/api/auth/sessions", token=second)[2])["sessions"]
    assert {row["device"] for row in sessions} == {"Work laptop", "Phone"}
    first_id = next(row["id"] for row in sessions if row["device"] == "Work laptop")
    status, _, body = service.request("/api/auth/sessions/revoke", method="POST", token=second,
                                      payload={"id": first_id})
    assert status == 200 and not json.loads(body)["current"]
    assert service.request("/api/inventory", token=first)[0] == 401
    assert service.request("/api/inventory", token=second)[0] == 200
    status, headers, _ = service.request("/api/auth/sign-out", method="POST", token=second, payload={})
    assert status == 200 and "Max-Age=0" in headers["Set-Cookie"]
    assert service.request("/api/inventory", token=second)[0] == 401


def test_secure_published_cookie(service):
    host = "halo.example.ts.net:18798"
    service.config["publication"] = host
    service.settings.write_text(json.dumps(service.config))
    status, headers, _ = service.pair("Tailnet browser", host=host)
    assert status == 200
    token, _, cookie = cookie_token(headers)
    assert "; Secure" in cookie
    assert service.request("/api/auth/status", token=token, host=host,
                           origin="https://" + host)[1]["Set-Cookie"].endswith("; Secure")


def test_sessions_survive_restart_and_password_reset_revokes_all(tmp_path, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(PACKAGE))
    browser_auth = importlib.import_module("browser_auth")
    manage = importlib.import_module("manage")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    manager = manage.Manager()
    manager.state.mkdir(mode=0o700, parents=True)
    config = {"service_id": "restart-fixture", "operator_verifier": manage.make_verifier("old-password")}
    manager.save(config)
    auth = browser_auth.BrowserAuth(manager.settings, config["service_id"])
    first, _ = auth.pair("operator", "old-password", "First", "")
    second, _ = auth.pair("operator", "old-password", "Second", "")
    restarted = browser_auth.BrowserAuth(manager.settings, config["service_id"])
    assert restarted.session(first)["device"] == "First"
    assert restarted.session(second)["device"] == "Second"
    first_id = restarted.session(first)["id"]
    assert restarted.revoke_id(first_id) == 1
    assert restarted.session(first) is None and restarted.session(second) is not None

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    answers = iter(["new-password", "new-password"])
    monkeypatch.setattr(manage.getpass, "getpass", lambda _: next(answers))
    manager.set_password(config)
    assert restarted.session(second) is None
    assert not restarted.all_sessions()
    third, _ = restarted.pair("operator", "new-password", "Replacement", "")
    assert restarted.session(third)["device"] == "Replacement"
    assert manage.main(["sessions"]) == 0
    assert "Replacement" in capsys.readouterr().out
    assert manage.main(["revoke", restarted.session(third)["id"]]) == 0
    assert restarted.session(third) is None


def test_missing_publication_ends_sessions(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(PACKAGE))
    browser_auth = importlib.import_module("browser_auth")
    manage = importlib.import_module("manage")
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"service_id": "missing-publication",
                                    "operator_verifier": manage.make_verifier("fixture-password")}))
    auth = browser_auth.BrowserAuth(settings, "missing-publication")
    token, _ = auth.pair("operator", "fixture-password", "Browser", "")
    (auth.public / "sessions.json").unlink()
    restarted = browser_auth.BrowserAuth(settings, "missing-publication")
    assert restarted.session(token) is None
    assert restarted.all_sessions() == []


def test_two_chromium_profiles_pair_persist_and_revoke_independently(service):
    playwright = ROOT / ".super-coder/browser/node_modules/playwright"
    chromium = shutil.which("chromium")
    if not (playwright.is_dir() and chromium and shutil.which("node")):
        pytest.skip("optional local Chromium seat")
    script = """
    const assert = require('node:assert/strict');
    const {chromium} = require(process.argv[1]);
    (async()=>{
      const browser=await chromium.launch({executablePath:process.argv[2],headless:true});
      try {
        const pair=async(device)=>{
          const context=await browser.newContext({viewport:{width:390,height:844}});
          const page=await context.newPage();
          await page.goto(process.argv[3]+'/');
          await page.getByRole('heading',{name:'Pair this browser'}).waitFor();
          assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
          await page.getByLabel('Username').fill('operator');
          await page.getByLabel('Manager password').fill('fixture-password');
          await page.getByLabel('Device name').fill(device);
          await page.getByRole('button',{name:'Pair browser'}).click();
          await page.getByRole('heading',{name:'Installations',exact:true}).waitFor();
          const cookies=await context.cookies(process.argv[3]);
          const session=cookies.find(cookie=>cookie.name.startsWith('sc_session_'));
          assert(session && session.httpOnly && session.sameSite==='Strict');
          assert(session.expires*1000-Date.now()>300*86400000);
          return {context,page,session};
        };
        const first=await pair('First Chromium');
        const second=await pair('Second Chromium');
        assert.notEqual(first.session.value,second.session.value);
        await first.page.reload();
        await first.page.getByRole('heading',{name:'Installations',exact:true}).waitFor();
        await second.page.getByRole('button',{name:'Paired browsers'}).waitFor();
        await second.page.getByRole('button',{name:'Paired browsers'}).click();
        const dialog=second.page.getByRole('dialog');
        await dialog.locator('article').filter({hasText:'First Chromium'}).waitFor();
        await dialog.locator('article').filter({hasText:'Second Chromium'}).waitFor();
        const firstCard=dialog.locator('article').filter({hasText:'First Chromium'});
        await firstCard.getByRole('button',{name:'Revoke'}).click();
        await firstCard.getByRole('button',{name:'Confirm revoke'}).click();
        await firstCard.waitFor({state:'detached'});
        await first.page.reload();
        await first.page.getByRole('heading',{name:'Pair this browser'}).waitFor();
        await second.page.getByRole('button',{name:'Done'}).click();
        await second.page.reload();
        await second.page.getByRole('heading',{name:'Installations',exact:true}).waitFor();
        await second.page.getByRole('button',{name:'Paired browsers'}).click();
        await second.page.getByRole('button',{name:'Sign out this browser'}).click();
        await second.page.getByRole('heading',{name:'Pair this browser'}).waitFor();
      } finally {await browser.close();}
    })().catch(error=>{console.error(error);process.exitCode=1});
    """
    result = subprocess.run(["node", "-e", script, str(playwright), chromium, service.base],
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
