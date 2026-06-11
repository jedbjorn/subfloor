#!/usr/bin/env python3
"""Live-preview every dev shell's worktree UI through one front port.

The problem: dev shells each own a git worktree (`.sc-worktrees/<shortname>/`),
but the fork's dev server runs from the *main* checkout — so a shell's UI edits,
made in its worktree, never reach the live dev server. Pointing the one dev
server at a single worktree would just clobber the isolation the worktrees exist
to provide.

The fix: one router on the fork's `dev_port` that fans out to a per-worktree
vite, routed by **subdomain**. Each worktree's vite runs at root on a private
internal port; the router reads the `Host` header and proxies
`http://<shortname>.localhost:<dev_port>/...` to that worktree's vite —
HTTP and the HMR websocket alike. `*.localhost` resolves to 127.0.0.1 on modern
systems, so no hosts-file or DNS setup is needed.

Subdomain (not path-prefix) routing is deliberate: each subdomain is a distinct
origin, so the SvelteKit app serves from root unchanged — no `kit.paths.base`,
no rewrite of the same-origin `/api/*` server-route seam, native HMR. A
path-prefix scheme would force all three.

    http://dev1.localhost:8842/        dev1's worktree UI, live
    http://dev2.localhost:8842/        dev2's worktree UI, live
    http://localhost:8842/             index of available shells

Routing is per-connection: a browser keeps one TCP connection per origin, so the
first `Host` seen on a connection is the route for its whole life. That lets the
proxy splice raw bytes after the header block — HTTP keep-alive and websocket
upgrades flow through untouched, no per-request reparsing.

Usage:
    python3 .super-coder/scripts/preview.py        # run in foreground (Ctrl-C to stop)

Bind host honours $SC_BIND (0.0.0.0 in the sandbox so the published port is
reachable; 127.0.0.1 on the host). Front port is $SC_DEV_PORT if set (the
sandbox publishes it) else the repo-derived dev_port from ports.py.
"""
from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE.parent
SCRIPTS = ENGINE / "scripts"
WORKTREES = REPO_ROOT / ".sc-worktrees"
SIDECAR = ".sc-preview.vite.config.js"  # generated per worktree-ui (under .sc-worktrees/, gitignored)

sys.path.insert(0, str(SCRIPTS))
import ports  # noqa: E402  — reuse the one port-derivation source


# ── worktree / ui discovery ──────────────────────────────────────────────────

def _find_ui_dir(root: Path) -> Path | None:
    """Shallowest dir under `root` holding a vite config (skip node_modules)."""
    best: Path | None = None
    best_depth = 99
    for pat in ("vite.config.js", "vite.config.ts", "vite.config.mjs"):
        for cfg in root.glob(f"**/{pat}"):
            if "node_modules" in cfg.parts or ".svelte-kit" in cfg.parts:
                continue
            depth = len(cfg.relative_to(root).parts)
            if depth < best_depth:
                best, best_depth = cfg.parent, depth
    return best


def discover() -> dict[str, Path]:
    """Map each worktree's shortname (dir name) -> its UI dir, where one exists."""
    out: dict[str, Path] = {}
    if not WORKTREES.is_dir():
        return out
    for wt in sorted(WORKTREES.iterdir()):
        if not wt.is_dir():
            continue
        ui = _find_ui_dir(wt)
        if ui is not None:
            out[wt.name] = ui
    return out


# ── per-worktree vite preparation ────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ensure_node_modules(ui: Path) -> bool:
    """Symlink the main checkout's node_modules into the worktree UI if missing.
    Same repo + lockfile, so main's install is valid. Returns False if neither
    the worktree nor main has node_modules (caller should skip/warn)."""
    if (ui / "node_modules").exists():
        return True
    # worktree UI lives at .sc-worktrees/<name>/<rel>; main UI is REPO_ROOT/<rel>
    try:
        sub = ui.relative_to(WORKTREES)          # <name>/<rel...>
        main_ui = REPO_ROOT.joinpath(*sub.parts[1:])
    except ValueError:
        main_ui = None
    if main_ui and (main_ui / "node_modules").is_dir():
        (ui / "node_modules").symlink_to(main_ui / "node_modules")
        return True
    return False


def _ensure_submodules(wt_root: Path) -> None:
    """Best-effort submodule init (e.g. vendor/md-converter) — never blocks."""
    if (wt_root / ".gitmodules").exists():
        try:
            import subprocess
            subprocess.run(["git", "-C", str(wt_root), "submodule", "update",
                            "--init", "--recursive"],
                           capture_output=True, timeout=120)
        except Exception:
            pass


def _write_sidecar(ui: Path, dev_port: int) -> Path:
    """Generate a vite config that extends the worktree's own, overriding only
    what subdomain-behind-a-shared-port needs: allow *.localhost hosts, and point
    the HMR client back at the front port (else it dials the private port)."""
    sidecar = ui / SIDECAR
    sidecar.write_text(
        "// GENERATED by ./sc preview — do not edit, do not commit.\n"
        "// Extends this worktree's vite.config with the overrides the preview\n"
        "// router needs: accept *.localhost, route HMR through the front port.\n"
        "import { mergeConfig } from 'vite';\n"
        "import base from './vite.config.js';\n"
        "const cfg = typeof base === 'function'\n"
        "  ? await base({ command: 'serve', mode: 'development' })\n"
        "  : base;\n"
        "export default mergeConfig(cfg, {\n"
        "  server: {\n"
        "    host: '127.0.0.1',\n"
        "    strictPort: true,\n"
        "    allowedHosts: ['.localhost'],\n"
        f"    hmr: {{ clientPort: {dev_port}, protocol: 'ws' }},\n"
        "  },\n"
        "});\n"
    )
    return sidecar


async def _log_pump(name: str, stream: asyncio.StreamReader) -> None:
    while True:
        line = await stream.readline()
        if not line:
            break
        sys.stdout.write(f"  [{name}] {line.decode(errors='replace').rstrip()}\n")
        sys.stdout.flush()


async def start_vite(name: str, ui: Path, dev_port: int) -> tuple[int, asyncio.subprocess.Process] | None:
    """Prepare and launch one worktree's vite; return (internal_port, proc)."""
    if not _ensure_node_modules(ui):
        print(f"  · {name}: no node_modules in worktree or main — run an install "
              f"in {ui}; skipping")
        return None
    _ensure_submodules(WORKTREES / name)  # worktree root = .sc-worktrees/<name>
    _write_sidecar(ui, dev_port)
    port = _free_port()
    vite = ui / "node_modules" / ".bin" / "vite"
    if not vite.exists():
        print(f"  · {name}: vite not found at {vite}; skipping")
        return None
    proc = await asyncio.create_subprocess_exec(
        str(vite), "dev", "--config", SIDECAR,
        "--port", str(port), "--host", "127.0.0.1", "--strictPort",
        cwd=str(ui),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    asyncio.ensure_future(_log_pump(name, proc.stdout))
    return port, proc


# ── the router ───────────────────────────────────────────────────────────────

class Router:
    def __init__(self, dev_port: int, bind: str):
        self.dev_port = dev_port
        self.bind = bind
        self.routes: dict[str, int] = {}             # shortname -> internal port
        self.procs: dict[str, asyncio.subprocess.Process] = {}
        self.uis: dict[str, Path] = {}

    def _label(self, host: str) -> str | None:
        host = host.split(":")[0].strip().lower()
        if host.endswith(".localhost"):
            return host[: -len(".localhost")].split(".")[-1]
        return None  # bare localhost / 127.0.0.1 / unknown → the index

    async def reconcile(self) -> None:
        """Bring running vites in line with the worktrees on disk (dynamic:
        new shells appear, deleted ones are reaped) — no restart needed."""
        found = discover()
        for name, ui in found.items():
            if name in self.routes:
                continue
            started = await start_vite(name, ui, self.dev_port)
            if started:
                port, proc = started
                self.routes[name] = port
                self.procs[name] = proc
                self.uis[name] = ui
                print(f"  → {name}: http://{name}.localhost:{self.dev_port}/  (vite :{port})")
        for name in [n for n in self.routes if n not in found]:
            self._kill(name)
            print(f"  ← {name}: worktree gone — preview stopped")

    def _kill(self, name: str) -> None:
        proc = self.procs.pop(name, None)
        self.routes.pop(name, None)
        self.uis.pop(name, None)
        if proc and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

    def shutdown(self) -> None:
        for name in list(self.procs):
            self._kill(name)

    async def reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await self.reconcile()
            except Exception as e:  # never let a scan kill the router
                print(f"  ! reconcile error: {e}")

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = await reader.read(65536)
                if not chunk:
                    writer.close()
                    return
                buf += chunk
                if len(buf) > 1 << 18:  # 256 KiB of headers → bail
                    break
            host = _header(buf, b"host") or ""
            label = self._label(host)
            target = self.routes.get(label) if label else None
            if target is None:
                await self._serve_index(writer, label, host)
                return
            try:
                br, bw = await asyncio.open_connection("127.0.0.1", target)
            except OSError:
                await _respond(writer, 502, f"{label}: backend not ready")
                return
            bw.write(buf)
            await bw.drain()
            await asyncio.gather(_pipe(reader, bw), _pipe(br, writer),
                                 return_exceptions=True)
        except Exception:
            try:
                writer.close()
            except Exception:
                pass

    async def _serve_index(self, writer: asyncio.StreamWriter, label, host) -> None:
        if label and label not in self.routes:
            await _respond(writer, 404,
                           f"no preview for '{label}'. Open shells: "
                           f"{', '.join(self.routes) or '(none)'}")
            return
        rows = "".join(
            f'<li><a href="http://{n}.localhost:{self.dev_port}/">'
            f'{n}.localhost:{self.dev_port}</a></li>'
            for n in sorted(self.routes)
        ) or "<li><em>no dev-shell worktree UIs found</em></li>"
        body = (f"<!doctype html><meta charset=utf-8>"
                f"<title>sc preview</title>"
                f"<h1>super-coder · live worktree previews</h1><ul>{rows}</ul>"
                f"<p>Each link is a dev shell's worktree, live with HMR.</p>")
        await _respond(writer, 200, body, content_type="text/html; charset=utf-8")

    async def serve(self) -> None:
        # Bind first, before spawning any vite — a port clash should fail fast
        # and cleanly, not after starting N dev servers we'd have to reap.
        try:
            server = await asyncio.start_server(self.handle, self.bind, self.dev_port)
        except OSError as e:
            print(f"sc preview: cannot bind {self.bind}:{self.dev_port} "
                  f"({e.strerror}).")
            print(f"  dev_port {self.dev_port} is already in use — most often the "
                  f"sandbox already publishes it, or a preview is already running.")
            print(f"  Run preview inside the sandbox (dev_port is free in there), "
                  f"stop the other listener, or set SC_DEV_PORT to a free port.")
            return
        print(f"sc preview · front http://{self.bind}:{self.dev_port}  "
              f"(index at http://localhost:{self.dev_port}/)")
        await self.reconcile()
        if not self.routes:
            print("  (no worktree UIs yet — will pick them up as shells appear)")
        asyncio.ensure_future(self.reconcile_loop())
        async with server:
            await server.serve_forever()


# ── tiny HTTP/byte helpers ───────────────────────────────────────────────────

def _header(buf: bytes, name: bytes) -> str | None:
    head = buf.split(b"\r\n\r\n", 1)[0]
    for line in head.split(b"\r\n")[1:]:
        k, _, v = line.partition(b":")
        if k.strip().lower() == name:
            return v.strip().decode(errors="replace")
    return None


async def _pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await r.read(65536)
            if not data:
                break
            w.write(data)
            await w.drain()
    except Exception:
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


async def _respond(writer: asyncio.StreamWriter, status: int, body: str,
                   content_type: str = "text/plain; charset=utf-8") -> None:
    reason = {200: "OK", 404: "Not Found", 502: "Bad Gateway"}.get(status, "OK")
    payload = body.encode()
    head = (f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(payload)}\r\n"
            f"Connection: close\r\n\r\n").encode()
    try:
        writer.write(head + payload)
        await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def main() -> int:
    bind = os.environ.get("SC_BIND", "127.0.0.1")
    dev_port = int(os.environ.get("SC_DEV_PORT") or ports.resolve()["dev_port"])
    router = Router(dev_port, bind)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: (router.shutdown(), loop.stop()))
        except NotImplementedError:  # pragma: no cover — non-unix
            pass
    try:
        loop.run_until_complete(router.serve())
    except (KeyboardInterrupt, RuntimeError):
        pass
    finally:
        router.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
