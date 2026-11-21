"""Per-user manager HTTP service. All application handlers share the access gate."""
import base64
import hmac
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from inventory import inventory

STATIC = Path(__file__).resolve().parent / "static"
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/analytics/": ("analytics/index.html", "text/html; charset=utf-8"),
          "/manager.css": ("manager.css", "text/css"),
          "/manager.js": ("manager.js", "text/javascript")}


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

    def dispatch(self):
        config = json.loads(self.server.settings.read_text())
        if (self.command == "GET" and self.path == "/health" and
                self.headers.get_all("Host") == [f"127.0.0.1:{self.server.server_port}"]):
            self.reply(200, config["service_id"].encode(), "text/plain")
            return
        if not self.authorized(config):
            return
        path = urlsplit(self.path).path
        if self.command == "GET" and path in ASSETS:
            filename, kind = ASSETS[path]
            file = STATIC / filename
            if not file.is_file():
                self.reply(404, b'{"error":"Page unavailable in this manager revision"}')
                return
            self.reply(200, file.read_bytes(), kind)
        elif self.command == "GET" and path == "/api/inventory":
            try:
                rows = inventory(Path(config["root"]), Path(config["listing"]),
                                 bool(config.get("publication") or config.get("publisher_configured")))
            except OSError as exc:
                self.reply(503, json.dumps({"error": str(exc)}).encode())
                return
            response = {"root": config["root"], "installations": rows,
                        "analytics_available": (STATIC / ASSETS["/analytics/"][0]).is_file()}
            self.reply(200, json.dumps(response).encode())
        else:
            self.reply(404, b'{"error":"Unknown manager route"}')

    do_GET = do_POST = dispatch


def serve(settings):
    config = json.loads(settings.read_text())
    with ThreadingHTTPServer(("127.0.0.1", config["port"]), Handler) as server:
        server.settings = settings
        server.serve_forever()


if __name__ == "__main__":
    serve(Path(sys.argv[1]))
