"""Mutation round trips for spec #48 / sprint 45 U11 — per-request /vendor
resolution and the honest failure report.

Acceptance evidence, committed rather than run once in a session that dies
with it (the U5/U7 convention). Each entry breaks ONE property in the real
source, asserts the test claiming to pin it actually goes RED, restores the
file, and asserts it goes green again. A property whose mutation stays green
is not pinned, whatever the suite's colour says.

    python3 tests/mutations/u11_vendor_resolution.py [-v]

Two harness conditions, both from flag #182 — a same-size mutation reverted
inside one second leaves a VALID `.pyc` behind (invalidation is source
mtime-to-the-second plus size), so the interpreter can keep running mutated
bytecode after the revert and hand back a reading that is simply false:

  * every child runs with PYTHONDONTWRITEBYTECODE=1, and
  * every `__pycache__` under the repo is cleared before each run.

The revert is proven, never assumed: the baseline must come back green after
each round trip, and a failure there is reported as a dirty revert rather than
folded into the mutation's own result.

Not named test_*.py on purpose: pytest must not collect it — it edits tracked
source. Every file is restored in a `finally`, so an interrupted run still
leaves the tree clean; `git diff` after a run is the check.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / ".super-coder" / "api" / "server.py"
APP = ROOT / ".super-coder" / "ui" / "app.js"
SERVER_SUITE = "tests/test_vendor_assets.py"
UI_SUITE = "tests/test_ui_vendor_probe.py"

FROZEN_TABLE = """def _resolve_vendor(rel: str) -> tuple:"""

# (label, suite, -k selector, file, old, new)
MUTATIONS = [
    (
        "M1 the vendor table is frozen at import again — the defect itself, "
        "restored by hand",
        SERVER_SUITE, "vendored_after_the_server_started", SERVER,
        '        candidate = (root / rel).resolve()',
        '        if rel not in _FROZEN_AT_IMPORT:\n'
        '            return None, "no such file"\n'
        '        candidate = (root / rel).resolve()',
    ),
    (
        "M2 containment is dropped — every traversal spelling reaches the "
        "file, and the test has to see the BYTES, not just a status",
        SERVER_SUITE, "traversal or symlink or absolute_path", SERVER,
        '        if not candidate.is_relative_to(root):\n'
        '            return None, "outside the vendor root"\n',
        '',
    ),
    (
        "M3 the decode goes entirely (the half of decode-then-contain a "
        "traversal battery cannot see — every `..` spelling still 404s, just "
        "at a different gate, so only an encoded NAME shows it)",
        SERVER_SUITE, "percent_encoded", SERVER,
        'unquote(path[len("/vendor/"):])',
        'path[len("/vendor/"):]',
    ),
    (
        "M4 the suffix allowlist is dropped and the type is guessed from the "
        "name — anything under vendor/ becomes readable",
        SERVER_SUITE, "allowlisted or source_maps", SERVER,
        '        ctype = _VENDOR_TYPES.get(Path(rel).suffix.lower())\n'
        '        if ctype is None:\n'
        '            return None, "suffix not allowlisted"',
        '        ctype = _VENDOR_TYPES.get(Path(rel).suffix.lower(),\n'
        '                                  "text/plain; charset=utf-8")',
    ),
    (
        "M5 .map is quietly allowlisted (the ruling this round was to leave "
        "it off)",
        SERVER_SUITE, "source_maps", SERVER,
        '    ".css": "text/css; charset=utf-8",',
        '    ".css": "text/css; charset=utf-8",\n    ".map": "application/json",',
    ),
    (
        "M6 the regular-file gate goes, so a directory answers 200",
        SERVER_SUITE, "directories or directory_named", SERVER,
        '        if not candidate.is_file():\n'
        '            return None, "no such file"',
        '        if not candidate.exists():\n'
        '            return None, "no such file"',
    ),
    (
        "M7 the sender is str-only again — a vendored font stops round "
        "tripping",
        SERVER_SUITE, "binary", SERVER,
        '        if isinstance(payload, (bytes, bytearray)):\n'
        '            body = bytes(payload)\n'
        '        else:\n'
        '            body = (json.dumps(payload, default=_json_default)\n'
        '                    if ctype.startswith("application/json")\n'
        '                    else payload).encode()',
        '        body = (json.dumps(payload, default=_json_default)\n'
        '                if ctype.startswith("application/json")\n'
        '                else payload).encode()',
    ),
    (
        "M8 every rejection answers with one generic reason, so the 404 stops "
        "naming the gate",
        SERVER_SUITE, "names_the_gate", SERVER,
        '            return self._send(404, detail, "text/plain")   # detail = the gate',
        '            return self._send(404, "not found", "text/plain")',
    ),
    (
        "M9 the unresolvable-path guard goes, so a NUL byte raises into a 500 "
        "instead of missing",
        SERVER_SUITE, "unresolvable", SERVER,
        '    except (OSError, ValueError):',
        '    except NotADirectoryError:',
    ),
    (
        "M10 HEAD stops answering for vendored assets — the probe reads 405 "
        "for every healthy script",
        SERVER_SUITE, "head", SERVER,
        '        if not path.startswith("/vendor/"):',
        '        if True:',
    ),
    (
        "M11 HEAD writes the body it says it is not sending",
        SERVER_SUITE, "head_with_headers", SERVER,
        '        if self.command != "HEAD":\n            self.wfile.write(body)',
        '        self.wfile.write(body)',
    ),
    (
        "M12 the shell files become dynamic too — the spec's deliberate limit "
        "widens filesystem tenancy silently",
        SERVER_SUITE, "frozen", SERVER,
        '        if path in _STATIC:\n'
        '            fname, ctype = _STATIC[path]',
        '        if path in _STATIC or (UI_DIR / path.lstrip("/")).is_file():\n'
        '            fname, ctype = _STATIC.get(\n'
        '                path, (path.lstrip("/"), "application/javascript"))',
    ),
    # -- the app shell ------------------------------------------------------
    (
        "M13 the old assertion comes back: a 404 is reported as a load race "
        "with a refresh remedy",
        UI_SUITE, "named_with_its_status or old_remedy", APP,
        '    a.st.note = "terminal library not ready — checking the vendored scripts…";\n'
        '    a.paint();\n'
        '    ifDiagnoseVendor(a, ticket, attempt, missing);\n'
        '    return;',
        '    a.st.note = "terminal library still loading — refresh the page";\n'
        '    a.paint();\n'
        '    return;',
    ),
    (
        "M14 the probe reports only the first failed script",
        UI_SUITE, "every_failed_script", APP,
        '      failed.map((p) => p.src + " returned " + p.status).join(", ") +',
        '      failed[0].src + " returned " + failed[0].status +',
    ),
    (
        "M15 the probe reads the cache instead of the server, so a stale 200 "
        "can mask the miss",
        UI_SUITE, "rather_than_the_cache", APP,
        '{ method: "HEAD", cache: "no-store" }',
        '{ method: "HEAD" }',
    ),
    (
        "M16 the retry bound is removed — a corrupt vendor file becomes a "
        "permanent spinner (the same lie in another costume)",
        UI_SUITE, "terminates_within_the_bound", APP,
        '  if (attempt + 1 >= IF_VENDOR_RETRY_MAX)',
        '  if (false)',
    ),
    (
        "M17 the retry itself goes, so the genuine defer race is reported as "
        "a fault the operator has to act on",
        UI_SUITE, "resolves_itself", APP,
        '  a.vendorRetry = setTimeout(() => {\n'
        '    a.vendorRetry = null;\n'
        '    if (ifAttach !== a) return;\n'
        '    ifOpenStream(a, ticket, attempt + 1);\n'
        '  }, IF_VENDOR_RETRY_MS);',
        '  ifVendorNote(a, "terminal library still loading");',
    ),
    (
        "M18 the generation check goes, so a probe that lands after the "
        "operator moved on repaints a pane they already left",
        UI_SUITE, "throws_after_the_operator", APP,
        'function ifVendorNote(a, note) {\n  if (ifAttach !== a) return;',
        'function ifVendorNote(a, note) {',
    ),
    (
        "M19 the post-await generation check goes, so a dead attach still "
        "schedules its next attempt",
        UI_SUITE, "moved_on", APP,
        '  if (ifAttach !== a) return;\n  const failed = probes.filter',
        '  const failed = probes.filter',
    ),
    (
        "M22 a queued attempt survives the attachment it belongs to, and "
        "opens a stream against a pane the operator already left",
        UI_SUITE, "queued_attempt", APP,
        '    a.vendorRetry = null;\n    if (ifAttach !== a) return;\n',
        '    a.vendorRetry = null;\n',
    ),
    (
        "M20 a probe that threw is reported as a diagnosis rather than as an "
        "unknown",
        UI_SUITE, "throws", APP,
        '    return ifVendorNote(a, "terminal unavailable — could not check the " +\n'
        '      "vendored scripts: " + e.message);',
        '    return ifVendorNote(a, "terminal unavailable — the vendored "\n'
        '      + "scripts returned an error; the engine floor needs a restart");',
    ),
    (
        "M21 the enumeration is replaced by a hardcoded list — it agrees with "
        "index.html today and cannot be made to disagree by the page",
        UI_SUITE, "rather_than_the_cache", APP,
        '  const srcs = Array.from(document.querySelectorAll(\'script[src^="/vendor/"]\'))\n'
        '    .map((s) => s.getAttribute("src"));',
        '  const srcs = ["/vendor/xterm/xterm.js", "/vendor/xterm/addon-fit.js"];',
    ),
]

# M1 needs a name to exist for the frozen-table mutation to be syntactically
# real; injected alongside it and removed with it.
FROZEN_SHIM = ('_FROZEN_AT_IMPORT = {"xterm/xterm.js"}\n\n\n' + FROZEN_TABLE)


def clear_caches() -> None:
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def run(suite: str, selector: str) -> bool:
    """True when the selected tests pass, run on source rather than bytecode."""
    clear_caches()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", suite, "-q", "-k", selector],
        cwd=ROOT, capture_output=True, text=True, env=env)
    return proc.returncode == 0


def main() -> int:
    verbose = "-v" in sys.argv
    failures = []
    for label, suite, selector, path, old, new in MUTATIONS:
        original = path.read_text()
        if original.count(old) != 1:
            failures.append(f"{label}: anchor found {original.count(old)}x, "
                            "expected exactly 1 — the driver has drifted from "
                            "the source")
            print(f"[FAIL] {label}\n       anchor is not unique")
            continue
        try:
            mutated = original.replace(old, new, 1)
            if label.startswith("M1 "):
                mutated = mutated.replace(FROZEN_TABLE, FROZEN_SHIM, 1)
            path.write_text(mutated)
            red = not run(suite, selector)
        finally:
            path.write_text(original)
        green = run(suite, selector)
        ok = red and green
        if not ok:
            failures.append(
                f"{label}: mutated={'RED' if red else 'GREEN (NOT PINNED)'} "
                f"restored={'green' if green else 'RED (dirty revert)'}")
        print(f"[{'ok' if ok else 'FAIL'}] {label}\n       {suite} "
              f"-k {selector}: {'red' if red else 'STAYED GREEN'} -> "
              f"{'green' if green else 'STILL RED'}")
        if verbose and not ok:
            print("       ^ this property is not actually pinned")
    print(f"\n{len(MUTATIONS) - len(failures)}/{len(MUTATIONS)} round trips "
          "behaved (red under mutation, green on revert)")
    for line in failures:
        print("  FAIL " + line)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
