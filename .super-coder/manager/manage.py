"""Install, update and remove the optional manager for the current Linux user."""
import argparse
import contextlib
import fcntl
import http.client
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from inventory import install_config, tailnet_name

UNIT = "subfloor-manager.service"
SOURCE = Path(__file__).resolve().parent
DEFAULT_PORT = 18798


def run(*args):
    result = subprocess.run(args, check=False, text=True, capture_output=True, timeout=30,
                            stdin=subprocess.DEVNULL)
    if result.returncode:
        raise ValueError(f"{' '.join(args[:3])}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def systemctl(*args):
    return run("systemctl", "--user", *args)


def checked_port(raw):
    number = int(raw)
    if not 1024 <= number <= 65535:
        raise ValueError("choose a port between 1024 and 65535")
    return number


def unit_arg(value):
    value = str(value)
    if any(ord(c) < 32 for c in value):
        raise ValueError("unsupported control character in service path")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def atomic(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        temp = Path(f.name)
        try:
            f.write(data.encode())
            f.flush()
            os.fsync(f.fileno())
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def check_instance_ports(root, port, keys):
    for repo in root.iterdir():
        cfg = install_config(root, repo)
        if cfg and any(cfg.get(key) == port for key in keys):
            raise ValueError(f"port {port} is assigned to {repo}; choose a free manager port")


def serve_mapping(authority, port):
    config = json.loads(run("tailscale", "serve", "status", "--json"))
    tls = authority.rsplit(":", 1)[1]
    target = f"http://127.0.0.1:{port}"
    web = config.get("Web") or {}
    tcp = config.get("TCP") or {}
    funnels = config.get("AllowFunnel") or {}
    authorities = {a for a in web if a.endswith(":" + tls)}
    if any(on and a.endswith(":" + tls) for a, on in funnels.items()):
        return "occupied"
    if (authorities == {authority} and tcp.get(tls) == {"HTTPS": True}
            and web[authority] == {"Handlers": {"/": {"Proxy": target}}}):
        return "manager"
    if authorities or tls in tcp:
        return "occupied"
    return "free"


class Manager:
    def __init__(self):
        def xdg(key, fallback):
            value = Path(os.environ.get(key) or Path.home() / fallback)
            if not value.is_absolute():
                raise ValueError(f"{key} must be absolute")
            return value

        self.state = xdg("XDG_STATE_HOME", ".local/state") / "subfloor-manager"
        self.data = xdg("XDG_DATA_HOME", ".local/share") / "subfloor-manager"
        self.unit = xdg("XDG_CONFIG_HOME", ".config") / "systemd/user" / UNIT
        self.settings = self.state / "settings.json"
        self.listing = Path.home() / ".local/state/subfloor-tailnet/urls.json"

    @contextlib.contextmanager
    def locked(self):
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (self.state.is_symlink() or self.state.stat().st_uid != os.getuid()
                or self.state.stat().st_mode & 0o077):
            raise ValueError(f"manager state must be private to its owner: {self.state}")
        with (self.state / "operation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def save(self, cfg):
        atomic(self.settings, json.dumps(cfg) + "\n")

    def config(self):
        if not self.settings.exists():
            raise ValueError("manager is not installed; run ./sc manager install")
        return json.loads(self.settings.read_text())

    def ready(self, cfg):
        # Never send the operator password to an unverified listener.
        for _ in range(30):
            client = http.client.HTTPConnection("127.0.0.1", cfg["port"], timeout=0.5)
            try:
                client.request("GET", "/health")
                response = client.getresponse()
                if response.status == 200 and response.read(64).decode() == cfg["service_id"]:
                    return
            except (OSError, UnicodeError, http.client.HTTPException):
                pass
            finally:
                client.close()
            time.sleep(0.1)
        raise ValueError(f"manager did not answer on port {cfg['port']}; check journalctl --user -u {UNIT}")

    def install(self, args):
        if self.settings.exists():
            cfg = self.config()
            if (self.data / "current/server.py").is_file() and self.unit.exists():
                systemctl("enable", "--now", UNIT)
                self.ready(cfg)
                return cfg
        else:
            if self.unit.exists() or self.data.exists():
                raise ValueError(f"existing manager files without settings; inspect {self.unit} and {self.data}")
            root = (args.root or Path.home() / "Repos").expanduser().resolve()
            if not root.is_dir():
                raise ValueError(f"repository root is not a directory: {root}")
            port = args.port or DEFAULT_PORT
            check_instance_ports(root, port, ("port", "dev_port", "browser_port", "browser_proxy_port",
                                              "engine_ops_port", "vm_mcp_port"))
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
            cfg = {"root": str(root), "port": port, "service_id": secrets.token_hex(16),
                   "listing": str((args.urls or self.listing).expanduser().resolve()),
                   "publisher_configured": bool(args.urls), "password": secrets.token_urlsafe(32)}
            self.save(cfg)
        self.deploy(cfg)
        return cfg

    def deploy(self, cfg):
        self.data.mkdir(parents=True, mode=0o700, exist_ok=True)
        current = self.data / "current"
        release = Path(tempfile.mkdtemp(prefix="release-", dir=self.data))
        # Only the distributed package is copied. Persistent data lives in state.
        shutil.copytree(SOURCE, release, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        link = self.data / "current.new"
        link.unlink(missing_ok=True)
        link.symlink_to(release.name)
        link.replace(current)
        self.unit.parent.mkdir(parents=True, exist_ok=True)
        argv = [Path(sys.executable).resolve(), "-E", "-s", current / "server.py", self.settings]
        atomic(self.unit, "[Unit]\nDescription=Subfloor Manager\n\n[Service]\nType=exec\nExecStart=" +
               " ".join(map(unit_arg, argv)) + "\nRestart=on-failure\nRestartSec=2\nUMask=0077\n"
               "\n[Install]\nWantedBy=default.target\n")
        systemctl("daemon-reload")
        systemctl("enable", UNIT)
        systemctl("restart", UNIT)
        self.ready(cfg)
        for old in self.data.glob("release-*"):
            if old != release and old.is_dir() and not old.is_symlink():
                shutil.rmtree(old)

    def publish(self, cfg, port):
        name = tailnet_name()
        if not name:
            raise ValueError("Tailscale must be running with a private DNS name")
        authority = f"{name}:{port}"
        previous = cfg.get("publication")
        if previous and previous != authority:
            raise ValueError("unpublish the previous manager mapping first")
        check_instance_ports(Path(cfg["root"]), port, ("port",))
        status = serve_mapping(authority, cfg["port"])
        if status == "occupied" or (status == "manager" and not previous):
            raise ValueError(f"Serve port {port} is already in use; choose a free private HTTPS port")
        # Save intent before Serve so retry and unpublish recover an interrupted operation.
        cfg["publication"] = authority
        self.save(cfg)
        if status == "free":
            run("tailscale", "serve", "--bg", f"--https={port}", "--yes",
                f"http://127.0.0.1:{cfg['port']}")
        if serve_mapping(authority, cfg["port"]) != "manager":
            raise ValueError("manager mapping was not confirmed; inspect tailscale serve status")

    def unpublish(self, cfg):
        authority = cfg.get("publication")
        if not authority:
            return
        if tailnet_name() != authority.rsplit(":", 1)[0]:
            raise ValueError("cannot verify the original Serve hostname; inspect the mapping")
        status = serve_mapping(authority, cfg["port"])
        if status == "occupied":
            raise ValueError("the recorded Serve mapping has changed; inspect it before removal")
        if status == "manager":
            run("tailscale", "serve", "--https=" + authority.rsplit(":", 1)[1], "off")
            if serve_mapping(authority, cfg["port"]) != "free":
                raise ValueError("Serve did not confirm removal of the manager mapping")
        del cfg["publication"]
        self.save(cfg)

    def remove(self, cfg):
        self.unpublish(cfg)
        if self.unit.exists():
            systemctl("disable", "--now", UNIT)
            self.unit.unlink()
        systemctl("daemon-reload")
        if self.data.exists():
            shutil.rmtree(self.data)
        self.settings.unlink()
        print(f"Manager removed. Retained manager history: {self.state}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    install = subs.add_parser("install", help="enable once, or show the existing manager")
    install.add_argument("--root", type=Path, help="directory of primary checkouts (default ~/Repos)")
    install.add_argument("--port", type=checked_port, help=f"loopback port (default {DEFAULT_PORT})")
    install.add_argument("--urls", type=Path, help="existing publisher's urls.json")
    subs.add_parser("update", help="install the manager payload from this engine revision")
    subs.add_parser("url", help="print the local and configured private URLs")
    subs.add_parser("password", help="show this host user's manager password")
    publish = subs.add_parser("publish", help="add a dedicated private Tailscale Serve mapping")
    publish.add_argument("--https-port", type=checked_port, required=True)
    subs.add_parser("unpublish", help="remove the manager's exact Serve mapping")
    subs.add_parser("remove", help="stop and uninstall this user's manager")
    args = parser.parse_args(argv)
    try:
        manager = Manager()
        with manager.locked():
            cfg = manager.install(args) if args.action == "install" else manager.config()
            if args.action == "update":
                manager.deploy(cfg)
            elif args.action == "publish":
                manager.publish(cfg, args.https_port)
            elif args.action == "unpublish":
                manager.unpublish(cfg)
            elif args.action == "remove":
                manager.remove(cfg)
                return 0
            elif args.action == "password":
                print(cfg["password"])
                return 0
            print(f"Subfloor Manager: http://127.0.0.1:{cfg['port']}/")
            if cfg.get("publication"):
                print(f"Private manager: https://{cfg['publication']}/")
            print("Sign in as operator. Show the password with ./sc manager password")
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"manager {args.action}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
