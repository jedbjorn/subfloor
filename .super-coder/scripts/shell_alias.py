#!/usr/bin/env python3
"""./sc alias — install the `subfloor` operator command for bash and fish.

The operator surface is `subfloor <verb> [args]`, which is exactly
`./sc <verb> [args]` run from the enclosing checkout. It replaces the retired
`make dos-*` aliases: no Makefile, no include line in the fork's own Makefile,
no `make` on the host. The command is a shell function, written once per host
user and shared by every Subfloor checkout that user owns:

    bash — a sentinel-delimited block appended to ~/.bashrc
    fish — an autoloaded function at ~/.config/fish/functions/subfloor.fish
           plus a completion file at ~/.config/fish/completions/subfloor.fish

The function walks up from the current directory to the nearest directory
holding `sc` next to `.sc-state/` (or `.super-coder/`) and execs that `sc`,
so it works from a fork root, any subdirectory, and any linked shell worktree,
and it picks the right install when one host user owns several.

`./sc install` installs it; every `./sc update` refreshes it (the bridge in
update_compat.py runs from the newly materialized engine, so an existing fork
adopts the command on its first update to this floor). Re-run `./sc alias`
after a shell-config reset; `./sc alias --remove` drops it; `--status` reports
without writing; `--print bash|fish` emits the function for inspection.

Usage:
    ./sc alias                 # install or refresh (bash + fish)
    ./sc alias --status        # report what is installed, exit 0 when current
    ./sc alias --remove        # remove the managed block and fish files
    ./sc alias --print bash    # print the bash function
    ./sc alias --print fish    # print the fish function
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

COMMAND = "subfloor"
VERSION = 1

BASH_BEGIN = f"# >>> {COMMAND} (managed by ./sc alias v{VERSION}) >>>"
BASH_END = f"# <<< {COMMAND} <<<"
_BASH_BLOCK_RE = re.compile(
    rf"^# >>> {COMMAND} \(managed by \./sc alias v\d+\) >>>\n.*?^# <<< {COMMAND} <<<\n?",
    re.M | re.S,
)

# The verbs an operator reaches for daily, offered as completions. Anything
# else still runs — the function forwards every argument verbatim.
COMPLETION_VERBS = (
    "enter admin launch restart down update test url help alias make-cleanup "
    "install doctor rollback update-harnesses harness-status feature runtime "
    "remove eject persist mem map map-sql map-schema sql skill models job pr "
    "sprint token verify render render-check snapshot rebuild migrate migration "
    "build logs serve health ports preview run boot deps lint typecheck"
)

_RESOLVE_COMMENT = (
    "# Walk up to the nearest Subfloor checkout (sc next to .sc-state/ or\n"
    "# .super-coder/) and run its ./sc — every argument forwarded verbatim."
)

BASH_FUNCTION = f"""{BASH_BEGIN}
{_RESOLVE_COMMENT}
{COMMAND}() {{
  local dir
  dir=$(pwd -P) || return 1
  while :; do
    if [ -f "$dir/sc" ] && {{ [ -d "$dir/.sc-state" ] || [ -d "$dir/.super-coder" ]; }}; then
      "$dir/sc" "$@"
      return
    fi
    [ "$dir" = / ] && break
    dir=${{dir%/*}}
    [ -n "$dir" ] || dir=/
  done
  printf '%s\\n' "✗ {COMMAND}: no Subfloor checkout at or above $PWD (looked for ./sc next to .sc-state/)" >&2
  return 1
}}
complete -W "{COMPLETION_VERBS}" {COMMAND}
{BASH_END}
"""

FISH_FUNCTION = f"""# {COMMAND} — managed by ./sc alias v{VERSION}; re-run ./sc alias to refresh, --remove to drop
{_RESOLVE_COMMENT}
function {COMMAND} --description 'Subfloor operator command: runs the enclosing checkout'"'"'s ./sc'
    set -l dir (pwd -P)
    while true
        if test -f "$dir/sc"; and begin; test -d "$dir/.sc-state"; or test -d "$dir/.super-coder"; end
            "$dir/sc" $argv
            return
        end
        if test "$dir" = /
            break
        end
        set dir (dirname "$dir")
    end
    echo "✗ {COMMAND}: no Subfloor checkout at or above $PWD (looked for ./sc next to .sc-state/)" >&2
    return 1
end
"""

FISH_COMPLETION = f"""# {COMMAND} — managed by ./sc alias v{VERSION}
complete -c {COMMAND} -f
complete -c {COMMAND} -n '__fish_use_subcommand' -a '{COMPLETION_VERBS}'
"""

FISH_MANAGED_MARK = f"managed by ./sc alias v"


class AliasError(RuntimeError):
    """Shell configuration is unsafe to edit."""


def _home(environ: Mapping[str, str]) -> Path:
    raw = environ.get("HOME")
    if not raw:
        raise AliasError("HOME is unset; cannot locate the shell configuration to edit")
    return Path(raw).expanduser()


def bashrc_path(environ: Mapping[str, str] | None = None) -> Path:
    return _home(environ or os.environ) / ".bashrc"


def fish_config_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = environ or os.environ
    raw = env.get("XDG_CONFIG_HOME")
    base = Path(raw).expanduser() if raw else _home(env) / ".config"
    return base / "fish"


def fish_function_path(environ: Mapping[str, str] | None = None) -> Path:
    return fish_config_dir(environ) / "functions" / f"{COMMAND}.fish"


def fish_completion_path(environ: Mapping[str, str] | None = None) -> Path:
    return fish_config_dir(environ) / "completions" / f"{COMMAND}.fish"


def _refuse_unsafe(path: Path, what: str) -> None:
    if path.is_symlink():
        raise AliasError(f"{what} is a symlink; refusing to edit it: {path}")
    if path.exists() and not path.is_file():
        raise AliasError(f"{what} is not a regular file; refusing to edit it: {path}")


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise AliasError(f"cannot read {path}: {exc}") from exc


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.sc-tmp")
    try:
        tmp.write_text(content)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise AliasError(f"cannot write {path}: {exc}") from exc


# ── bash ─────────────────────────────────────────────────────────────────────

def bash_state(environ: Mapping[str, str] | None = None) -> str:
    """'current' · 'stale' (an older managed block) · 'absent'."""
    path = bashrc_path(environ)
    _refuse_unsafe(path, "~/.bashrc")
    text = _read(path)
    if BASH_FUNCTION in text:
        return "current"
    if _BASH_BLOCK_RE.search(text):
        return "stale"
    return "absent"


def install_bash(environ: Mapping[str, str] | None = None) -> str:
    path = bashrc_path(environ)
    state = bash_state(environ)
    if state == "current":
        return f"bash: {path} already current — left as-is"
    text = _read(path)
    if state == "stale":
        text = _BASH_BLOCK_RE.sub("", text)
    if text and not text.endswith("\n"):
        text += "\n"
    if text and not text.endswith("\n\n"):
        text += "\n"
    _write(path, text + BASH_FUNCTION)
    verb = "refreshed" if state == "stale" else "wrote"
    return f"bash: {verb} the {COMMAND} function in {path}"


def remove_bash(environ: Mapping[str, str] | None = None) -> str:
    path = bashrc_path(environ)
    if bash_state(environ) == "absent":
        return f"bash: no managed block in {path}"
    text = _BASH_BLOCK_RE.sub("", _read(path))
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.rstrip("\n") + "\n" if text.strip() else ""
    _write(path, text)
    return f"bash: removed the {COMMAND} function from {path}"


# ── fish ─────────────────────────────────────────────────────────────────────

def _fish_file_state(path: Path, expected: str, what: str) -> str:
    _refuse_unsafe(path, what)
    text = _read(path)
    if not text:
        return "absent"
    if text == expected:
        return "current"
    if FISH_MANAGED_MARK in text:
        return "stale"
    return "foreign"


def fish_state(environ: Mapping[str, str] | None = None) -> str:
    """'current' · 'stale' · 'absent' · 'foreign' (an unmanaged file in the way)."""
    fn = _fish_file_state(fish_function_path(environ), FISH_FUNCTION, "fish function file")
    comp = _fish_file_state(fish_completion_path(environ), FISH_COMPLETION, "fish completion file")
    if "foreign" in (fn, comp):
        return "foreign"
    if fn == "current" and comp == "current":
        return "current"
    if fn == "absent" and comp == "absent":
        return "absent"
    return "stale"


def install_fish(environ: Mapping[str, str] | None = None) -> str:
    state = fish_state(environ)
    fn_path = fish_function_path(environ)
    if state == "foreign":
        raise AliasError(
            f"an unmanaged fish file already defines {COMMAND}: {fn_path} "
            f"(or its completion). Move it aside, then re-run ./sc alias"
        )
    if state == "current":
        return f"fish: {fn_path} already current — left as-is"
    _write(fn_path, FISH_FUNCTION)
    _write(fish_completion_path(environ), FISH_COMPLETION)
    verb = "refreshed" if state == "stale" else "wrote"
    return f"fish: {verb} the {COMMAND} function at {fn_path}"


def remove_fish(environ: Mapping[str, str] | None = None) -> str:
    state = fish_state(environ)
    if state == "absent":
        return f"fish: no managed function at {fish_function_path(environ)}"
    if state == "foreign":
        return f"fish: {fish_function_path(environ)} is not managed by ./sc alias — left alone"
    removed = []
    for path in (fish_function_path(environ), fish_completion_path(environ)):
        try:
            path.unlink()
            removed.append(str(path))
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise AliasError(f"cannot remove {path}: {exc}") from exc
    return f"fish: removed {', '.join(removed)}"


# ── whole-host operations ────────────────────────────────────────────────────

def install(environ: Mapping[str, str] | None = None) -> list[str]:
    """Install or refresh the command for bash and fish. Returns report lines."""
    return [install_bash(environ), install_fish(environ)]


def remove(environ: Mapping[str, str] | None = None) -> list[str]:
    return [remove_bash(environ), remove_fish(environ)]


def status(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    return {"bash": bash_state(environ), "fish": fish_state(environ)}


def is_current(environ: Mapping[str, str] | None = None) -> bool:
    return all(state == "current" for state in status(environ).values())


def activation_hint() -> str:
    """What the operator types so the just-written function is defined NOW."""
    return (
        f"open a new terminal, or `source ~/.bashrc` (fish picks it up "
        f"immediately), then `{COMMAND} help`"
    )


def render(shell: str) -> str:
    if shell == "bash":
        return BASH_FUNCTION
    if shell == "fish":
        return FISH_FUNCTION
    raise AliasError(f"unsupported shell {shell!r} — bash or fish")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sc alias",
        description=f"install the `{COMMAND}` operator command for bash and fish",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="report without writing")
    group.add_argument("--remove", action="store_true", help="remove the managed function")
    group.add_argument("--print", dest="print_shell", choices=("bash", "fish"),
                       metavar="bash|fish", help="print the function for one shell")
    args = parser.parse_args(argv)
    try:
        if args.print_shell:
            sys.stdout.write(render(args.print_shell))
            return 0
        if args.status:
            report = status()
            print(f"bash: {report['bash']}  ({bashrc_path()})")
            print(f"fish: {report['fish']}  ({fish_function_path()})")
            return 0 if is_current() else 1
        lines = remove() if args.remove else install()
        for line in lines:
            print(f"  {line}")
        if not args.remove:
            print(f"  {activation_hint()}")
        return 0
    except AliasError as exc:
        print(f"✗ sc alias: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    from cli_entry import run_cli

    raise SystemExit(run_cli(main, sys.argv[1:]))
