# Work unit 9 — PR 1034 @ `b51e0ca6` — changes_requested

Spec #108 @ `4b1b2cf7` (bound == current, no mid-Sprint edits); tasks 321, 322.
Six checks green at this exact head (tests, verify, render-check, CodeQL,
Analyze python/actions). PR body carries only the work-unit id and spec
reference; zero PR comments, zero PR reviews. Four files touched (`vm.py` plus
three test files) — in lane, no Sprint artifacts committed. Base is current
`main` (`387a8c0`). 117 VM tests pass at this head from a clean detached
worktree; the full local suite runs 1787 tests with no code failures (9 loader
errors are `import pytest` in my environment only — CI's `tests` job is green
at this head).

Blocking: 2 Major.

---

## Major 1 — every `exec` failure collapses to one opaque code and discards stdout/stderr

**Violated:** spec Tool Contracts — "On validation, connection, timeout,
malformed-response, or broker failure, the CLI returns a structured error";
objective 8 — "receive a clear structured error and report it to the user";
and the `exec` result contract (`exit_code`, `stdout`, `stderr`).

**Location:** `vm.py:1429-1447` (`_broker_failure` exec branch), reached from
`vm.py:1547` `if not response.get("ok")`. `do_exec` sets `ok = (returncode ==
0)` at `vm.py:580`, so *any* non-zero guest exit takes this path.

**Consequence, executed at head** — four unrelated conditions produce
byte-identical output; every diagnostic string is dropped:

| broker response (verbatim `do_exec` output) | client error |
|---|---|
| config missing, guest never ran — `exit -1`, stderr `missing required field(s): ssh_key_path` | `exec_failed` · "the guest command failed" · `exit_code:-1` |
| `ssh` absent on host — `exit 127`, stderr `command not found: ssh …` | `exec_failed` · same message · `exit_code:127` |
| guest exec timed out — `exit 124`, stderr `timed out (>120s)` | `exec_failed` · same message · `exit_code:124` |
| ordinary `Get-Item C:\nope` — `exit 1`, real stderr | `exec_failed` · same message · `exit_code:1` |

Human render for all four: `✗ exec [exec_failed]: the guest command failed`.
Details carry only `stdout_bytes` / `stderr_bytes`.

Follow-on: `result.exit_code` is provably always `0` — the success branch
(`vm.py:1584`) is reachable only when the broker said `ok:true`, i.e.
`returncode == 0`. A documented result field and the `_human_result` branch at
`vm.py:1658` are dead for every other value. Skill-workflow steps 4 ("open the
application only if absent") and 6 ("a test failure does not skip cleanup")
both need a failing command's output; the shell gets none.

The redaction is also self-defeating: on `exit 0` that same `stdout` is
printed verbatim and returned in full. The spec forbids *"guest command
text"*, not guest command **output**, so
`test_failed_guest_command_does_not_copy_output_into_error_details:166` pins a
rule the spec does not state while buying no confidentiality.

**Fix boundary:** keep the four conditions distinguishable and preserve the
diagnostic. Straightforward shape: a guest command that *ran* is a successful
`exec` operation whose result carries `exit_code`/`stdout`/`stderr` (the
process exit code may still be non-zero); reserve error codes for the
conditions the spec enumerates — config/validation, timeout, transport —
carrying `stderr` as the message, the way the merged `start`/`reset` paths
already carry `response["output"]`. No new machinery.

---

## Major 2 — `capture --output` can atomically overwrite live engine state

**Violated:** spec — "An explicit output path must remain inside an allowed
local artifact area"; §Beyond Intention — a shell "cannot … write captures
outside allowed artifact paths".

**Location:** `vm.py:1290-1313` `_capture_target`. The allowed root is
`LOCAL_ARTIFACT_ROOT = <repo>/.sc-state/local` (`vm.py:400`); the guard at
`vm.py:1308` rejects only `target == root` and paths outside it. Anything else
under that root is then written by `os.replace` (`vm.py:1381`).

`.sc-state/local` is not an artifact area — it is the engine's own ignored
state directory. Live in this install: `.sc-state/local/content.sql`
(60,288 B — the per-instance memory snapshot), `.sc-state/local/map/map.db`
(442,368 B — the repo map DB), `map/content.sql`, `renders/`, and update
markers.

**Consequence, executed at head** against a real AF_UNIX broker with real
files planted at those paths:

```
--output .sc-state/local/content.sql
  -> ok:true  path:…/.sc-state/local/content.sql  bytes:23  format:ppm
  before: '-- local memory snapshot\nINSERT INTO she…'
  after : b'P6\n2 2\n255\n\x00\x01\x02…'

--output .sc-state/local/map/map.db
  -> ok:true   before: b'SQLite format 3\x00'   after: b'P6\n2 2\n255\n…'
```

`ok:true` — the shell gets no signal that it just destroyed the memory
snapshot or the map DB. `--output /tmp/evil.ppm` is correctly refused, so the
guard works; its root is one directory too high.

**Fix boundary:** make the allowed root the capture directory the spec names
(`.sc-state/local/vm-captures/`), keeping the existing resolve-then-contain
check. One constant, its `--help` string (`vm.py:1760`), and the matching test
expectations.

---

## Low — report notes, not blocking

- **L1** `run_operation` pre-validates with `_capture_target(output, "ppm")`
  (`vm.py:1479`); the format argument is inert when `output` is explicit, so a
  reader must prove the hard-coded `"ppm"` cannot reach a filename.
- **L2** `MAX_CAPTURE_BYTES` = 16 MiB against an uncompressed PPM: 1920×1080 ≈
  6.2 MB is comfortable, 2560×1600 ≈ 12.3 MB is close, 3840×2160 ≈ 24.9 MB
  always fails `capture_too_large` with no override.
- **L3** `push` hard-requires the new `source`/`destination` keys
  (`vm.py:1600-1604`), so a pre-merge `vm-broker` reports
  `broker_response_invalid` for a push that actually staged the file.
  `sc restart` does bounce `vm-broker`, so the skew window is update-without-
  restart only; task #327 (unit 11) owns legacy-broker compatibility.

---

## Verified good

- **#1024, end-to-end through the real CLI and a real unix socket** (not
  mocks): multiline PowerShell with double and single quotes, `$env:`/`$p`
  variables, `C:\Program Files\Demo App\` paths, pipes, backticks and
  `München 東京` reach `POST /exec` byte-identical. `--command-file` preserves
  content and trailing newline in either `--json` position; both forms
  together, neither form, and a whitespace-only command are refused before
  broker contact.
- **#1025 artifact half:** the default artifact is mode `0600` under
  `.sc-state/local/vm-captures/`, written via `mkstemp` + `os.replace` with no
  leftover `.tmp`; byte-count mismatch, invalid base64, and oversized metadata
  each refuse to create a target or damage an existing one.
- `push` returns structured `source`/`destination`; `do_capture` now correctly
  reports `ok:false` when `domain` is unset.
