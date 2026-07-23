#!/usr/bin/env python3
"""Print the browser sign-in operator token (spec doc #30 req 23).

`./sc token` (exact alias `make dos-token`) prints the current Admin runtime
credential — the token a browser operator pastes into the sign-in prompt — and
ONLY that token, on stdout. It never rotates the credential, never puts it in
command arguments, and never writes it to a log.

The source of truth is the owner-only runtime artifact the supervised API
provisions at every boot (`.super-coder/run/mem/<shortname>.json`, dir 0700,
file 0600 — see scripts/mem_credentials.py). Reading is artifact-only: env
wiring (SC_API_TOKEN) never substitutes, so the refusal contract below always
applies. Selection matches `sc mem` discovery — the unique artifact, or
SC_MEM_AS=<shortname> when several Admin identities exist.

A missing, unreadable, or insecurely permissioned artifact refuses on stderr
with the supported service action (`./sc restart` / `make dos-r`, which
re-provisions it at boot). The trust-boundary check itself lives in
mem.py:_discover_runtime_credential — this command reuses it rather than
duplicating security logic. (Named operator_token.py, not token.py: the
stdlib `token` module would shadow an `import token`.)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mem  # noqa: E402

_USAGE = ("usage: ./sc token — print the browser sign-in operator token (an "
          "operator capability: the Admin runtime credential from the "
          "owner-only artifact .super-coder/run/mem/<shortname>.json) to "
          "stdout, and nothing else. The value itself never appears in help, "
          "logs, or command arguments.")


def main(argv: "list[str] | None" = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in ("-h", "--help"):
        print(_USAGE)
        return 0
    # Refusals name this command, not `sc mem`.
    mem._PROG = "sc token"
    if not mem._discover_runtime_credential():
        mem.die(f"no Admin runtime credential in {mem._CRED_DIR} — the "
                "supervised service provisions one per Admin shell at boot "
                "(`./sc restart` / `make dos-r`).")
    # Printing the operator token to stdout IS this command's spec'd function
    # (doc #30 req 23): stdout is the paste channel for the browser sign-in
    # prompt, never a log; nothing else is written anywhere.
    print(mem.SC_API_TOKEN)  # codeql[py/clear-text-logging-sensitive-data]
    return 0


if __name__ == "__main__":
    sys.exit(main())
