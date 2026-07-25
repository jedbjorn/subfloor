#!/usr/bin/env python3
"""Mutation driver for spec #43 U7 (composer actions).

Acceptance for U7 names one mutation explicitly — "make the identity_mismatch
branch fall through to the start POST -> the fail-closed test goes red" — but a
single round trip only pins a single branch, and the unit's value is that the
+Chat chain is fail-closed on EVERY recovery-shaped outcome. So each mutation
below breaks one property in the real source, runs the suites, and demands red;
the driver then reverts and demands green again. A mutation that stays GREEN is
a finding about the test, not a success.

This lives in the repo on purpose. U2's acceptance evidence was run from an
untracked scratch directory and died with the worktree (REV1's Low L3 on
PR #596), so the round trips were unreproducible the moment the session ended.

Usage:
    python3 tests/mutations/u7_composer_actions.py          # all mutations
    python3 tests/mutations/u7_composer_actions.py --list

Exit 0 = every mutation reproduced red->revert->green.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / ".super-coder" / "ui" / "app.js"
CSS = ROOT / ".super-coder" / "ui" / "style.css"

SUITES = [
    "tests/test_interface_composer_actions.py",
    "tests/test_interface_composer_contract.py",
    "tests/test_interface_ui_contract.py",
    # The rendered suite is in the gate because two of U7's properties have no
    # other witness: that the launch triple is READ from the session payload,
    # and that the hint's CSS hook matches the markup. It SKIPS when Playwright
    # or its browser is absent (flag #169 — the sandbox image ships a browser
    # layout this Playwright cannot find), and a skip is silently green, so run
    # it with a working browser before trusting those two round trips.
    "tests/test_interface_ui_rendered.py",
]


@dataclass
class Mutation:
    name: str
    property: str
    path: Path
    old: str
    new: str


MUTATIONS = [
    # ── The spec's named mutation ────────────────────────────────────────────
    Mutation(
        name="identity-mismatch-falls-through",
        property="fail-closed: identity_mismatch must not reach the start POST",
        path=APP,
        old="""  if (!r.terminated && r.reason === "identity_mismatch") {
    if (root) await renderInterface(root);
    return { kind: "recovery" };
  }""",
        new="""  if (!r.terminated && r.reason === "identity_mismatch") {
    if (root) await renderInterface(root);
    return { kind: "terminated" };
  }""",
    ),
    # ── The other fail-closed legs, same property, different branch ──────────
    Mutation(
        name="thrown-recovery-409-falls-through",
        property="fail-closed: not_occupied / identity_unverified / not_running",
        path=APP,
        old="""      ifDetach();
      if (root) await renderInterface(root);
      return { kind: "recovery" };""",
        new="""      ifDetach();
      if (root) await renderInterface(root);
      return { kind: "terminated" };""",
    ),
    Mutation(
        name="declined-force-falls-through",
        property="fail-closed: a declined force kill starts nothing",
        path=APP,
        old="""      return { kind: "declined",
               note: "graceful stop timed out — End again to retry, or confirm the force kill." };""",
        new="""      return { kind: "terminated" };""",
    ),
    Mutation(
        name="start-gated-on-absence-of-error",
        property=(
            "fail-closed is an EQUALITY test on terminated, not "
            '"nothing went wrong so far"'
        ),
        path=APP,
        old='  if (out.kind !== "terminated") {\n    if (out.kind !== "recovery") a.st.note = out.note;',
        new='  if (out.kind === "failed") {\n    a.st.note = out.note;',
    ),
    # ── The launch triple, reused verbatim ───────────────────────────────────
    Mutation(
        name="effort-dropped-from-relaunch",
        property="the effort leg of the launch triple survives +Chat",
        path=APP,
        old="  if (effort) body.effort = effort;",
        new="  if (false) body.effort = effort;",
    ),
    Mutation(
        name="model-dropped-from-relaunch",
        property="the model-route leg of the launch triple survives +Chat",
        path=APP,
        old="  if (model) body.model = model;",
        new="  if (false) body.model = model;",
    ),
    Mutation(
        name="null-effort-sent-as-null",
        property='a NULL leg is OMITTED, not sent as null ("harness default")',
        path=APP,
        old="  if (effort) body.effort = effort;",
        new="  body.effort = effort ?? null;",
    ),
    # Only the RENDERED suite witnesses this one: the node-driven chain tests
    # set st.launchEffort themselves, so they pin what the chain does with the
    # triple but not that it is read from the session payload at all. This
    # mutation was GREEN until the rendered relaunch test was added.
    Mutation(
        name="launch-effort-not-read-from-session",
        property="+Chat reads effort from the session payload, not from nowhere",
        path=APP,
        old="    launchEffort: sess.launch_effort ?? null,",
        new="    launchEffort: null,",
    ),
    # ── The bounded retry ────────────────────────────────────────────────────
    # There is deliberately NO "retry is unbounded" mutation here. The first
    # draft of the start leg was a retry loop, and widening its guard made this
    # driver HANG rather than go red — a suite that spins forever demonstrates
    # nothing. The loop was replaced with two straight-line attempts, which
    # makes unbounded retry unexpressible instead of merely untested. The
    # timeout in run_suites() remains as the backstop for that whole class.
    Mutation(
        name="occupancy-retry-absent",
        property="the occupancy race IS retried once (the race is real)",
        path=APP,
        old='  if (failure && failure.status === 409 && failure.code === "shell_occupied") {',
        new="  if (false) {",
    ),
    Mutation(
        name="422-retried-like-a-race",
        property="only shell_occupied is retried; a 422 route error is not",
        path=APP,
        old='  if (failure && failure.status === 409 && failure.code === "shell_occupied") {',
        new="  if (failure) {",
    ),
    # Required by the planner's ratification of the 422 judgement call (task
    # #1800): the unavailable-route leg must not fall through to a
    # default-route start. This is the silent-substitution failure — the launch
    # triple is the unit's invariant, so relaunching on a DIFFERENT route is
    # worse than refusing, and a test that only checked "did not crash" would
    # miss it entirely.
    Mutation(
        name="422-falls-through-to-default-route-start",
        property="an unavailable route is refused, never downgraded and started",
        path=APP,
        old="  if (failure)\n"
            '    toast("new chat not started — " +\n'
            '          (failure.code ? failure.code + ": " : "") + failure.message);',
        new="  if (failure) {\n"
            "    delete body.model;\n"
            "    delete body.effort;\n"
            "    failure = await ifStartSession(body);\n"
            "  }",
    ),
    Mutation(
        name="retry-wait-dropped",
        property="the retry waits 2s rather than hammering the occupancy flip",
        path=APP,
        old="    await new Promise((done) => setTimeout(done, 2000));",
        new="    await new Promise((done) => setTimeout(done, 0));",
    ),
    Mutation(
        name="server-message-swallowed",
        property="an exhausted start surfaces the server's own words",
        path=APP,
        old='    toast("new chat not started — " +\n'
            '          (failure.code ? failure.code + ": " : "") + failure.message);',
        new='    toast("new chat not started");',
    ),
    Mutation(
        name="start-failure-reported-as-success",
        property="a failed start is never silently treated as a new chat",
        path=APP,
        old="async function ifStartSession(body) {\n"
            "  try {\n"
            '    await apiIf("/interface/sessions", "POST", body);\n'
            "    return null;\n"
            "  } catch (e) { return e; }\n"
            "}",
        new="async function ifStartSession(body) {\n"
            '  try { await apiIf("/interface/sessions", "POST", body); } catch { /* ignored */ }\n'
            "  return null;\n"
            "}",
    ),
    # ── The composer's action row ─────────────────────────────────────────────
    Mutation(
        name="send-button-restored",
        property="Enter is the SOLE submit affordance",
        path=APP,
        old='  const actions = el("div", { className: "if-composer-actions" }, end, fresh);',
        new='  const actions = el("div", { className: "if-composer-actions" },\n'
            '    el("button", { className: "act primary", type: "button",'
            ' textContent: "Send" }), end, fresh);',
    ),
    Mutation(
        name="hint-absent",
        property="the static 'enter to send' hint renders",
        path=APP,
        old='  a.composerEl.append(input, actions, hint, note);',
        new='  a.composerEl.append(input, actions, note);',
    ),
    Mutation(
        name="hint-tracks-gate-state",
        property=(
            "the hint is STATIC — U2's note is the single state surface, so a "
            "hint that moves with gate state can contradict it"
        ),
        path=APP,
        old="  const row = ifComposerGate(a);\n  let text = \"\";",
        new="  const row = ifComposerGate(a);\n"
            "  a.composerEl.children[2].textContent = row.outcome === \"BLOCKED\"\n"
            "    ? \"cannot send\" : \"enter to send\";\n  let text = \"\";",
    ),
    Mutation(
        name="end-label-reverted",
        property='End reads "End", not "End chat"',
        path=APP,
        old='className: "act bad if-end-chat", type: "button", textContent: "End",',
        new='className: "act bad if-end-chat", type: "button", textContent: "End chat",',
    ),
    Mutation(
        name="end-leaves-the-red-family",
        property="End keeps the red act-bad styling family",
        path=APP,
        old='className: "act bad if-end-chat", type: "button", textContent: "End",',
        new='className: "act", type: "button", textContent: "End",',
    ),
    Mutation(
        name="new-chat-button-absent",
        property="+Chat exists on the occupied composer",
        path=APP,
        old="  const actions = el(\"div\", { className: \"if-composer-actions\" }, end, fresh);",
        new="  const actions = el(\"div\", { className: \"if-composer-actions\" }, end);",
    ),
    # ── Double activation ────────────────────────────────────────────────────
    Mutation(
        name="actions-not-disabled-during-chain",
        property="both actions are held for the chain's whole duration",
        path=APP,
        old="  a.composerActionsBusy = true;\n  a.paint();",
        new="  a.paint();",
    ),
    # ── End's own contract, which U7 must not disturb ────────────────────────
    Mutation(
        name="end-swallows-unrelated-failure",
        property="End reports a non-recovery failure instead of going quiet",
        path=APP,
        old='    else return { kind: "failed", note: "end chat failed: " + e.message };',
        new='    else return { kind: "recovery" };',
    ),
    # ── The hint's CSS hook, which the rendered suite reads ──────────────────
    Mutation(
        name="hint-css-hook-renamed",
        property="the hint's class is the one the rendered suite locates",
        path=CSS,
        old=".if-composer .if-composer-hint {",
        new=".if-composer .if-composer-hint-x {",
    ),
]


# The suites run in ~2s. The timeout exists because a mutation can HANG rather
# than fail — widening the old retry loop's guard spun node forever instead of
# reddening — and an un-timed driver then hangs with the source still mutated.
SUITE_TIMEOUT_S = 120


def run_suites() -> tuple[bool, bool]:
    """(passed, timed_out). A timeout is a failure: a suite that cannot finish
    has not demonstrated the property."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *SUITES],
            cwd=ROOT, capture_output=True, text=True, check=False,
            timeout=SUITE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, True
    return result.returncode == 0, False


def apply(mutation: Mutation) -> str:
    original = mutation.path.read_text()
    count = original.count(mutation.old)
    if count != 1:
        raise SystemExit(
            f"{mutation.name}: anchor matched {count} times in "
            f"{mutation.path.name}, expected exactly 1 — the driver is stale")
    mutation.path.write_text(original.replace(mutation.old, mutation.new))
    return original


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--only", default=None,
                        help="run one mutation by name")
    args = parser.parse_args()

    selected = [m for m in MUTATIONS
                if args.only is None or m.name == args.only]
    if args.list:
        for m in selected:
            print(f"{m.name:38s} {m.property}")
        return 0
    if not selected:
        raise SystemExit(f"no mutation named {args.only!r}")

    print("baseline: ", end="", flush=True)
    passed, timed_out = run_suites()
    if not passed:
        print("TIMED OUT" if timed_out else "RED", "— fix the tree before mutating")
        return 1
    print("green")

    failures = []
    for m in selected:
        print(f"{m.name:38s} ", end="", flush=True)
        original = apply(m)
        try:
            passed, timed_out = run_suites()
            red = not passed
            print("hung " if timed_out else ("red " if red else "GREEN "),
                  end="", flush=True)
        finally:
            m.path.write_text(original)     # restored even on Ctrl-C / SIGTERM
        green, _ = run_suites()
        print("-> revert -> " + ("green" if green else "STILL RED"))
        # A hang is not an acceptable red: the suite never demonstrated the
        # property, it merely failed to answer.
        if not (red and green) or timed_out:
            failures.append((m, "hung" if timed_out else "did not round-trip"))

    print()
    if failures:
        print(f"{len(failures)} of {len(selected)} mutations FAILED:")
        for m, why in failures:
            print(f"  - {m.name} ({why}): {m.property}")
        return 1
    print(f"{len(selected)}/{len(selected)} mutations red -> revert -> green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
