---
name: sprint_canary
description: Run the exact-ref, disposable dos-app Sprint promotion canary for Spec #116.
---

# sprint_canary — exact-ref dos-app promotion gate

Use for a request to run or validate the Spec #116 exact-ref dos-app Sprint
canary. Do not use this for ordinary unit-suite guidance.

The source harness is `maintainer/dos_app_sprint_canary.py` in the subfloor
source checkout. Orchestrate that harness; do not reimplement its preflight,
disposable-install, Sprint, receipt, or cleanup behavior.

## Preconditions

- Obtain the requested full 40-character subfloor commit, the subfloor source
  checkout, the foreign dos-app checkout, and an explicit receipt path outside
  temporary/disposable state.
- The FnB authorizes the live canary. A green canary does not authorize
  promotion, recovery, or a mutation of `dos-app` main.
- Keep a failed receipt. It is the atomic, redacted cleanup record.

## Procedure

1. Pin the candidate before testing. From the source checkout, fetch
   `origin/main`, resolve the requested ref to one full commit SHA, and require
   it to be contained in `origin/main`.

   ```bash
   git -C "$SOURCE_REPO" fetch origin main
   CANDIDATE_SHA="$(git -C "$SOURCE_REPO" rev-parse --verify "$REQUESTED_REF^{commit}")"
   git -C "$SOURCE_REPO" merge-base --is-ancestor "$CANDIDATE_SHA" origin/main
   ```

   Success = `CANDIDATE_SHA` is a 40-character SHA and the containment check
   exits 0. Stop on any failure; do not substitute a branch name or a nearby
   commit.

2. Run the focused hermetic test before the live canary.

   ```bash
   cd "$SOURCE_REPO"
   python3 -m pytest -q tests/test_dos_app_sprint_canary.py
   ```

   Success = pytest passes. A test failure blocks the live canary.

3. Run the harness against the foreign install only with engine API credentials
   removed. The harness creates disposable resources and refuses a dirty
   dos-app checkout; never work around either guard and never run it from, or
   directly mutate, `dos-app` main.

   ```bash
   env -u SC_API_TOKEN -u SC_API_URL \
     python3 "$SOURCE_REPO/maintainer/dos_app_sprint_canary.py" run \
       --engine-ref "$CANDIDATE_SHA" \
       --source-repo "$SOURCE_REPO" \
       --dos-app-repo "$DOS_APP_REPO" \
       --dos-app-ref origin/main \
       --receipt "$RECEIPT_PATH" \
       --run-id "$RUN_ID"
   ```

   Success = the command exits 0 and writes one receipt. Use explicit timeout
   options only when the FnB specifies them.

4. Retain and inspect the receipt without printing sensitive material. A green
   gate requires all of: `candidate_sha` equals `CANDIDATE_SHA`; `status` is
   `passed`; `sprint.lifecycle` is `completed`; and `cleanup.complete` is true.
   Report only those fields plus the receipt path and bounded timeline facts.

5. On failure, report the stable `CanaryError` code, its stage, and bounded
   redacted evidence from the receipt. Run guarded, idempotent cleanup against
   the same receipt; do not delete or rewrite that receipt.

   ```bash
   env -u SC_API_TOKEN -u SC_API_URL \
     python3 "$SOURCE_REPO/maintainer/dos_app_sprint_canary.py" cleanup \
       --receipt "$RECEIPT_PATH"
   ```

   Success = `cleanup.complete` is true. If cleanup fails, report its code and
   failed actions; do not begin another run.

6. After a green receipt, stop. Proceed to real dos-app promotion or Sprint
   recovery only on explicit FnB direction.

## Never

- NEVER expose `SC_API_TOKEN`, `SC_API_URL`, prompts, transcripts, or the full
  receipt in a report.
- NEVER run the live canary before the focused test passes.
- NEVER treat a green receipt as Spec #116 acceptance or authority to promote.
