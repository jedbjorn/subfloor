"""Per-user manager HTTP service. All application handlers share the access gate."""
import base64
import hmac
import json
import mimetypes
import os
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from inventory import installations, inventory

STATIC = Path(__file__).resolve().parent / "static"
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/manager.css": ("manager.css", "text/css"),
          "/manager.js": ("manager.js", "text/javascript")}

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
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def authorized(self, config):
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
        expected = "Basic " + base64.b64encode(("operator:" + config["password"]).encode()).decode()
        values = self.headers.get_all("Authorization", [])
        if len(values) != 1 or not hmac.compare_digest(values[0].encode(), expected.encode()):
            self.reply(401, b'{"error":"Operator sign-in required"}',
                       headers=[("WWW-Authenticate", 'Basic realm="Subfloor Manager"')])
            return False
        return True

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

    def dispatch(self):
        config = json.loads(self.server.settings.read_text())
        if (self.command == "GET" and self.path == "/health" and
                self.headers.get_all("Host") == [f"127.0.0.1:{self.server.server_port}"]):
            self.reply(200, config["service_id"].encode(), "text/plain")
            return
        if not self.authorized(config):
            return
        url = urlsplit(self.path)
        path = url.path
        if path.startswith(("/analytics/", "/api/analytics/")):
            return self.analytics_route(url)
        if self.command == "GET" and path in ASSETS:
            filename, kind = ASSETS[path]
            self.reply(200, (STATIC / filename).read_bytes(), kind)
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
