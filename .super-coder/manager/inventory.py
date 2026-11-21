"""Read-only observations of primary checkouts, supervisors and GUI endpoints."""
import http.client
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit


def command(*args):
    try:
        return subprocess.run(args, check=False, capture_output=True, text=True, timeout=3,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None


def tailnet_name():
    result = command("tailscale", "status", "--json")
    if result and not result.returncode:
        try:
            data = json.loads(result.stdout)
            name = data["Self"]["DNSName"].lower().rstrip(".")
            if data["BackendState"] == "Running" and re.fullmatch(
                    r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ts\.net", name):
                return name
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
    return None


def install_config(root, repo):
    """Validate a direct, owner-held primary checkout each time it is selected."""
    engine = repo / ".super-coder"
    config = engine / "instance.json"
    try:
        if (repo.parent != root or repo.resolve() != repo or repo.stat().st_uid != os.getuid()
                or not (repo / ".git").is_dir() or (repo / ".git").is_symlink()):
            return None
        for file in (config, engine / "api/server.py", repo / "sc"):
            if not file.is_file() or file.resolve() != file or file.stat().st_uid != os.getuid():
                return None
        raw = config.read_text()
    except (OSError, RuntimeError):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def host_state(repo):
    result = command("systemctl", "--user", "show", "--property=LoadState,ActiveState",
                     "--", f"sc-review-{repo.name}.service")
    if not result:
        return "unknown"
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if fields.get("LoadState") == "not-found":
        return "absent"
    if result.returncode or fields.get("LoadState") != "loaded":
        return "unknown"
    return {"active": "running", "inactive": "stopped", "failed": "failed",
            "activating": "starting", "deactivating": "stopping"}.get(fields.get("ActiveState"), "unknown")


def docker_state(repo):
    result = command("docker", "container", "inspect", "--format",
                     "{{.Config.WorkingDir}}\n{{.State.Status}}", f"sc-{repo.name}")
    if not result:
        return "unknown"
    if result.returncode:
        error = result.stderr.strip().lower()
        missing = (f"no such container: sc-{repo.name}".lower(),
                   f"no such object: sc-{repo.name}".lower())
        return "absent" if error.endswith(missing) else "unknown"
    parts = result.stdout.strip().splitlines()
    if len(parts) != 2 or parts[0] != str(repo):
        return "unknown"
    return {"running": "running", "created": "stopped", "exited": "stopped",
            "dead": "failed", "paused": "paused", "restarting": "starting"}.get(parts[1], "unknown")


def supervisor(repo, config):
    mode = config.get("runtime")
    if mode == "host":
        return "host", host_state(repo)
    if mode == "sandbox":
        return "docker", docker_state(repo)
    if mode is not None:
        return "unknown", "unknown"
    host, docker = host_state(repo), docker_state(repo)
    observed = [(runtime, state) for runtime, state in (("host", host), ("docker", docker))
                if state not in ("absent", "unknown")]
    if len(observed) == 1:
        return observed[0]
    # Engines with the runtime selector default to sandbox when the key is absent.
    if not observed and (repo / ".super-coder/scripts/runtime.py").is_file():
        return "docker", docker
    return "unknown", "unknown"


def gui_health(url, repo, config):
    """Probe only the named authority, without a proxy, redirects or credentials."""
    target = urlsplit(url)
    cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    connection = cls(target.hostname, target.port, timeout=2)
    try:
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        if response.status != 200:
            return "unavailable"
        data = json.loads(response.read(65537))
        if not isinstance(data, dict) or data.get("ok") is not True:
            return "unavailable"
        expected = config.get("repo", repo.name)
        if (data.get("repo") != expected or data.get("repo_root") != str(repo)
                or data.get("port") != config.get("port")):
            return "identity mismatch"
        return "healthy"
    except ConnectionRefusedError:
        return "no listener"
    except (OSError, ValueError, http.client.HTTPException):
        return "unavailable"
    finally:
        connection.close()


def published_routes(listing, enabled):
    if not enabled and not listing.parent.exists():
        return {}, "not configured", None
    try:
        entries = json.loads(listing.read_text())
        if not isinstance(entries, list):
            return {}, "list unavailable", None
        routes = {item["repo"]: item["url"] for item in entries
                  if isinstance(item, dict) and isinstance(item.get("repo"), str)
                  and isinstance(item.get("url"), str)}
    except (ValueError, OSError):
        return {}, "list unavailable", None
    return routes, "pending", tailnet_name() if routes else None


def inventory(root, listing, published=False):
    routes, default_publication, dns = published_routes(listing, published)
    installs = [(repo, cfg) for repo in sorted(root.iterdir())
                if (cfg := install_config(root, repo)) is not None]

    def observe(item):
        repo, cfg = item
        runtime, state = supervisor(repo, cfg)
        port = cfg.get("port")
        valid = type(port) is int and 1024 <= port <= 65535
        local = f"http://127.0.0.1:{port}" if valid else None
        health = gui_health(local, repo, cfg) if local else "invalid port"
        if state == "absent":
            state = "stopped" if health == "no listener" else "unknown"
        if state == "stopped" and health != "no listener":
            state = "unknown"
        url = routes.get(repo.name)
        publication = default_publication
        if url:
            if valid and dns and url == f"https://{dns}:{port}":
                status = gui_health(url, repo, cfg)
                publication = "verified" if status == "healthy" else status
            else:
                url, publication = None, "unverified"
        return {"name": repo.name, "path": str(repo), "runtime": runtime, "state": state,
                "health": health, "publication": publication, "published_url": url,
                "local_url": local if health == "healthy" else None,
                "open_url": url if publication == "verified" else None}

    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(observe, installs))
