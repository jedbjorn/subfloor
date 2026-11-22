"""Per-user manager HTTP service. All application handlers share the access gate."""
import json
import mimetypes
import os
import signal
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from browser_auth import AuthError, BrowserAuth
from inventory import installations, inventory
from operator_password import matches
from repository_actions import Actions, Conflict

STATIC = Path(__file__).resolve().parent / "static"
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/manager.css": ("manager.css", "text/css"),
          "/manager.js": ("manager.js", "text/javascript"),
          "/theme.js": ("theme.js", "text/javascript"),
          "/auth.js": ("auth.js", "text/javascript"),
          "/signin.html": ("signin.html", "text/html; charset=utf-8"),
          "/signin.js": ("signin.js", "text/javascript"),
          "/signin.css": ("signin.css", "text/css")}
OPEN_ASSETS = {"/signin.html", "/signin.js", "/signin.css", "/manager.css", "/theme.js"}

ANALYTICS = STATIC.parent / "token_analytics"
ANALYTICS_READS = {"status", "filters", "sessions", "usage", "activity", "engine-updates"}


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_args):
        pass

    def reply(self, status, body, kind="application/json", headers=()):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'")
        if self.renew_cookie:
            self.send_header("Set-Cookie", self.renew_cookie)
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def origin_allowed(self, config):
        host = self.headers.get_all("Host", [])
        local = f"127.0.0.1:{self.server.server_port}"
        origins = {local: f"http://{local}"}
        if public := config.get("publication"):
            origins[public] = f"https://{public}"
        origin = self.headers.get_all("Origin", [])
        fetch = self.headers.get_all("Sec-Fetch-Site", [])
        if (len(host) != 1 or host[0] not in origins
                or (origin and origin != [origins[host[0]]])
                or (self.command != "GET" and not origin)
                or (fetch and fetch not in (["same-origin"], ["none"]) and
                    (self.command != "GET" or self.path.startswith("/api/")))):
            self.reply(403, b'{"error":"Unexpected request origin"}')
            return False
        return True

    def identify(self, config):
        if not self.origin_allowed(config):
            return False
        values = self.headers.get_all("Authorization", [])
        if values:
            if len(values) == 1 and matches(values[0], config.get("operator_verifier")):
                self.basic_auth = True
                return True
            self.reply(401, b'{"error":"Operator sign-in required","code":"sign_in_required"}')
            return False
        cookies = self.headers.get_all("Cookie", [])
        if len(cookies) > 1:
            self.reply(401, b'{"error":"Operator sign-in required","code":"sign_in_required"}')
            return False
        import auth_sessions
        tokens = auth_sessions.presented_tokens(cookies[0] if cookies else None,
                                               self.server.browser_auth.cookie_name)
        for token in tokens:
            try:
                session = self.server.browser_auth.session(token)
            except (OSError, ValueError):
                self.reply(503, b'{"error":"Paired-browser state is unavailable"}')
                return False
            if session is not None:
                self.operator_token = token
                self.operator_session = session
                break
        return True

    def authenticated(self):
        return self.basic_auth or self.operator_session is not None

    def require_operator(self):
        if self.authenticated():
            return True
        url = urlsplit(self.path)
        if self.command == "GET" and url.path in ("/", "/analytics/") and not self.headers.get("Authorization"):
            self.reply(302, b"", headers=[("Location", "/signin.html?next=" + quote(url.path, safe="/"))])
        else:
            self.reply(401, b'{"error":"Operator sign-in required","code":"sign_in_required"}')
        return False

    def renew(self, config):
        if self.operator_token and urlsplit(self.path).path in ("/", "/analytics/", "/api/auth/status"):
            secure = self.headers.get("Host") == config.get("publication")
            self.renew_cookie = self.server.browser_auth.cookie(self.operator_token, secure)

    def json_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 8192 or self.headers.get_content_type() != "application/json":
            raise ValueError("A bounded JSON request is required.")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise TypeError("Expected a JSON object.")
        return data

    def auth_error(self, exc):
        headers = [("Retry-After", str(exc.retry_after))] if exc.retry_after else []
        self.reply(exc.status, json.dumps({"error": {"code": exc.code, "message": exc.message}}).encode(),
                   headers=headers)

    def pair(self, config):
        if not self.origin_allowed(config):
            return
        try:
            data = self.json_body()
            if (not isinstance(data.get("username"), str) or not isinstance(data.get("password"), str)
                    or not isinstance(data.get("device", ""), str)):
                raise TypeError("Username, password, and device name must be text.")
            token, result = self.server.browser_auth.pair(
                data["username"], data["password"], data.get("device"), self.headers.get("User-Agent"))
        except AuthError as exc:
            return self.auth_error(exc)
        except (ValueError, TypeError, OSError, json.JSONDecodeError) as exc:
            return self.reply(400, json.dumps({"error": str(exc)}).encode())
        secure = self.headers.get("Host") == config.get("publication")
        self.reply(200, json.dumps(result).encode(),
                   headers=[("Set-Cookie", self.server.browser_auth.cookie(token, secure))])

    def auth_status(self, config):
        if self.basic_auth:
            result = {"enrolled": bool(config.get("operator_verifier")), "signed_in": True,
                      "user": {"username": "operator"}, "session": None, "retry_after": 0}
        else:
            result = self.server.browser_auth.status(self.operator_token)
            result["enrolled"] = bool(config.get("operator_verifier"))
        self.renew(config)
        self.reply(200, json.dumps(result).encode())

    def session_route(self, config):
        if not self.operator_token:
            return self.reply(401, b'{"error":"A paired browser is required","code":"sign_in_required"}')
        auth = self.server.browser_auth
        secure = self.headers.get("Host") == config.get("publication")
        try:
            if self.command == "GET" and self.path == "/api/auth/sessions":
                result = {"sessions": auth.sessions(self.operator_token)}
                return self.reply(200, json.dumps(result).encode())
            if self.command == "POST" and self.path == "/api/auth/sign-out":
                auth.sign_out(self.operator_token)
                return self.reply(200, b'{"signed_in":false}',
                                  headers=[("Set-Cookie", auth.clear_cookie(secure))])
            if self.command == "POST" and self.path == "/api/auth/sessions/revoke":
                data = self.json_body()
                result = auth.revoke(self.operator_token, data.get("id"))
                headers = [("Set-Cookie", auth.clear_cookie(secure))] if result["current"] else []
                return self.reply(200, json.dumps(result).encode(), headers=headers)
        except AuthError as exc:
            return self.auth_error(exc)
        except (ValueError, TypeError, OSError, json.JSONDecodeError) as exc:
            return self.reply(400, json.dumps({"error": str(exc)}).encode())
        return self.reply(404, b'{"error":"Unknown authentication route"}')

    def analytics_route(self, url):
        service = self.server.analytics
        if service is None:
            return self.reply(404, b'{"error":"Token Analytics is unavailable"}')
        if self.command == "GET" and url.path in self.server.analytics_assets:
            asset = self.server.analytics_assets[url.path]
            kind = mimetypes.guess_file_type(asset)[0] or "application/octet-stream"
            return self.reply(200, asset.read_bytes(), kind)
        if url.path.startswith("/api/analytics/"):
            name = url.path.removeprefix("/api/analytics/")
            if self.command == "GET" and name in ANALYTICS_READS:
                try:
                    data = service.read(name, parse_qs(url.query))
                except (ValueError, KeyError) as exc:
                    return self.reply(400, json.dumps({"error": str(exc)}).encode())
                return self.reply(200, json.dumps(data).encode())
            if self.command == "POST" and name == "refresh":
                return self.reply(200, json.dumps(service.refresh()).encode())
        return self.reply(404, b'{"error":"Unknown analytics route"}')

    def action_route(self, path):
        actions = self.server.actions
        try:
            if self.command == "GET" and path == "/api/actions":
                result = actions.history()
            elif self.command == "GET" and path.startswith("/api/actions/output/"):
                result = {"output": actions.output(path.rsplit("/", 1)[1])}
            elif self.command == "POST" and path in ("/api/actions/preview", "/api/actions/apply"):
                data = self.json_body()
                result = actions.preview(data) if path.endswith("/preview") else actions.apply(data)
            else:
                return self.reply(404, b'{"error":"Unknown action route"}')
        except Conflict as exc:
            return self.reply(409, json.dumps({"error": str(exc)}).encode())
        except (ValueError, TypeError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            return self.reply(400, json.dumps({"error": str(exc)}).encode())
        return self.reply(200, json.dumps(result).encode())

    def serve_asset(self, path):
        filename, kind = ASSETS[path]
        self.reply(200, (STATIC / filename).read_bytes(), kind)

    def dispatch(self):
        self.basic_auth = False
        self.operator_token = None
        self.operator_session = None
        self.renew_cookie = None
        config = json.loads(self.server.settings.read_text())
        if (self.command == "GET" and self.path == "/health" and
                self.headers.get_all("Host") == [f"127.0.0.1:{self.server.server_port}"]):
            self.reply(200, config["service_id"].encode(), "text/plain")
            return
        url = urlsplit(self.path)
        path = url.path
        if self.command == "GET" and path in OPEN_ASSETS:
            if self.origin_allowed(config):
                self.serve_asset(path)
            return
        if self.command == "GET" and path == "/api/auth/status":
            if self.identify(config):
                self.auth_status(config)
            return
        if self.command == "POST" and path == "/api/auth/pair":
            self.pair(config)
            return
        if not self.identify(config) or not self.require_operator():
            return
        self.renew(config)
        if path.startswith("/api/auth/"):
            return self.session_route(config)
        if path == "/api/actions" or path.startswith("/api/actions/"):
            return self.action_route(path)
        if path.startswith(("/analytics/", "/api/analytics/")):
            return self.analytics_route(url)
        if self.command == "GET" and path in ASSETS:
            self.serve_asset(path)
        elif self.command == "GET" and path == "/api/inventory":
            try:
                rows = inventory(Path(config["root"]), Path(config["listing"]),
                                 bool(config.get("publication") or config.get("publisher_configured")))
            except OSError as exc:
                self.reply(503, json.dumps({"error": str(exc)}).encode())
                return
            response = {"root": config["root"], "installations": rows,
                        "analytics_available": self.server.analytics is not None}
            self.reply(200, json.dumps(response).encode())
        else:
            self.reply(404, b'{"error":"Unknown manager route"}')

    do_GET = do_POST = dispatch


def serve(settings, engine_repo=None):
    config = json.loads(settings.read_text())
    with ThreadingHTTPServer(("127.0.0.1", config["port"]), Handler) as server:
        server.settings = settings
        server.browser_auth = BrowserAuth(settings, config["service_id"])
        server.actions = Actions(Path(config["root"]), settings.parent)
        server.analytics = None
        if ANALYTICS.is_dir():
            # The user-service environment may differ from the installer's.
            # settings is under XDG_STATE_HOME/subfloor-manager.
            os.environ["XDG_STATE_HOME"] = str(settings.parent.parent)
            root = Path(config["root"])
            static = ANALYTICS / "static"
            server.analytics_assets = {
                "/analytics/" + asset.relative_to(static).as_posix(): asset
                for asset in static.rglob("*") if asset.is_file() and asset.resolve() == asset
            }
            server.analytics_assets["/analytics/"] = server.analytics_assets["/analytics/index.html"]

            def repositories():
                return [repo for repo, _ in installations(root)]

            server.analytics = import_module("token_analytics").Analytics(
                settings.parent, repositories, engine_repo=engine_repo)
        try:
            if server.analytics is not None:
                server.analytics.start()
            server.serve_forever()
        finally:
            if server.analytics is not None:
                server.analytics.close()


if __name__ == "__main__":
    # systemd stops and updates the user service with SIGTERM. Unwind the
    # listener so the scheduler joins before the installed release is retired.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    serve(Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) > 2 else None)
