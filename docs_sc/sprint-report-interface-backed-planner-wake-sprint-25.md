---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
feature: 
roadmap_status: 
frozen: false
---

# SPRINT REPORT: Interface-backed planner wake (Sprint 25, feature #14)

Spec: doc #20. Sprint doc: #25. Closed 2026-07-23. Planner: PLN1.
Final main: **@10d1bdd, green.**

## Verdict

**Conforms-with-deviations; shipped; real-fork operation proven; full cross-harness gate deferred with eyes open.**

Eleven sequence units (seq 1–11) plus three seq-11 fork-hardening fix units all merged to a green main. The pre-freeze conformance pass (two sharded review shells judging spec #20 against main) returned **0 Major** across both shards — every safety-critical clause (the decision-#15 submit gate, the parking invariant, the generation fence, hook-rejection auditing, no-auto-replay) holds. Four conformance Mediums: one (M2 byte-fidelity) closed by the part-B matrices; three (M1 plaintext lease_token in snapshot, F1 no engine tmux.conf, F3 wall-clock quiet debounce) deferred to post-sprint hardening, each low-risk under the decision-#30 trust boundary.

The seq-11 gate ran a real-fork acceptance on a materialized fork (**ami**): the Interface vertical (durable tmux-3.5a chat, shadow grid, API-owned broker) was **proven to run end-to-end** (dev1 chat live). That gate surfaced three genuine fork-deploy defects the source-run sandbox structurally could not see — all fixed and merged before freeze: the shadow-sidecar **materialization gap** (#514), three **real-tmux runtime defects** (#515), and the **Interface worktree-provision** gap (#526).

**Deferred with eyes open (post-sprint, per decision #35):** the full real cross-harness task→red→green→review→merge sprint on Claude+Codex+Kimi (the top verification tier), the Interface QA hardening pass on ami (Codex/Sol reviewer investigating at freeze), and the three conformance Mediums. These are tracked below, not lost.

## Units Shipped

| Seq | Task | Unit | PR | Merge |
|---|---|---|---|---|
| 1 | SC-010 | `sc mem task edit` verb | #492 | c5abaa1 |
| 2 | SC-008 | serialize doc writes headlessly | #493 | 6b8f93c |
| 3 | #79 | interface stream + input-broker feasibility spike (HARD GATE) | #496 | 81756b1 |
| 4 | #80 | session schema + state machines | #500 | 6a2b8ec |
| 5 | #81 | one-shell Interface vertical slice | #505 | 7e39ce0 |
| 6 | #82 | CLI parity + full Interface workflow | #507 | 2be13d8 |
| 7 | #83 | cross-harness lifecycle adapters | #508 | ab7dd5a |
| 8 | #84 | transactional brokered planner wake (feature payload) | #511 | 057fd84 |
| 9 | #85 | watched-PR polling + daemon cutover | #503 | 6a989f2 |
| 10 | #86 | operator wake surfaces + sprint workflow | #513 | 13f5405 |
| 11 | #87 | conformance pass (docs #27/#28) + real-tmux integration matrices | #515 | 3c5f998 |
| 11-fix | flag #59 | materialize `.super-coder/shadow/` to forks | #514 | a113168 |
| 11-fix | flag #61 | provision shell worktree at reserve, never assume it | #526 | 10d1bdd |

Planned order held; the seq-11 fix units were inserted front-of-chain under still-active authority (why the conformance pass runs before freeze). W0→W1→W2→converge windows ran as planned; seq 8 recovered from a mid-build worker death (WIP preserved as a checkpoint, completed post-restart).

## Judgements Made

- **Reviewer harness switch (decision #21):** all devs and reviewers on Kimi K3 — Codex/Sol's content filter refused Interface ws/PTY/auth/broker *code* review. Held all sprint.
- **Ambiguity rulings, all ratified:** seq-4 (#23), seq-5 (4 calls), seq-8 (#32 — incl. probe-fail-open, verified TOTAL by REV2), seq-10 (#33 — route shapes, retry-resolves-alerts-with-re-arm, close-on-both-triggers), part-B (4 calls).
- **Hard requirements #49/#50/#51 (decisions #28/#31):** seq-7 review findings landed in seq 8 with tests — start-ready off real provider readiness, hook commit serialized in-txn, every rejection path audited. Verified fixed in the seq-8 re-review.
- **Trust boundary (decision #30, supersedes #26):** personal-machine tool; the guarded boundary is web-origin / network / credential-exposure, not other local processes. The shipped seq-5 operator-cap bootstrap STANDS for this sprint; automatic same-origin bootstrap is a post-sprint relax (PLN2 authoring). Reviewers judged the remaining sprint against this boundary.
- **Intentional deviation — frozen-CANCEL:** a frozen sprint doc cancels queued wake like a close (stronger than the spec minimum). Marked deviated-intentionally in conformance.
- **Finish-scope (decision #35):** finish now; defer the full cross-harness gate + ami QA hardening to a post-sprint ami re-try.
- **Conformance-Medium rulings (decision #36):** M2 closed by #515; M1/F1/F3 deferred-with-note.

## Spec Accuracy

Conformance docs: **#27** (REV1, boundary/API/auth) and **#28** (REV2, wake/broker/gate). Both **0 Major**.

- Safety-critical clauses **as-specced**: submit gate (all conditions atomic), parking invariant (delivery_unknown never resubmitted), generation-fenced input order, hook rejection audit, no-auto-replay, actor scoping, idempotency/CSRF/ticket/Host-allowlist auth stack.
- **deviated-intentionally:** frozen-CANCEL; operator-cap bootstrap (decision #26 direction-superseded by #30 — recorded as such, NOT a defect; absence of auto-bootstrap not flagged).
- **deviated-silently → Medium (deferred):** M1 plaintext lease_token reaches the git-tracked snapshot (spec says hashes-only durable).
- **Medium (deferred):** F1 no engine-shipped tmux.conf; F3 wall-clock quiet debounce.
- **Medium (closed):** M2 byte-fidelity matrix — now covered by the part-B real-tmux matrices (#515).
- Unit reports' `deviations:` cross-checked against conformance: seq-10 declared the minimal-REST-route-shapes deviation (spec-debt, below); part-B declared runtime defects fixed in-unit (disclosed in PR body). No dev `deviations: none` collided with a silent-deviation finding.

## Issues Encountered

- **The real-fork gate as a hardening campaign.** Standing up the Interface on ami exposed three fork-deploy defects invisible to the source-run sandbox: (1) `.super-coder/shadow/` never materialized to forks → sidecar missing (#514, + a recurrence-guard test so a new engine subdir can't silently miss the manifest again); (2) three real-tmux-only runtime defects — pump FIFO false-EOF, dropped un-awaited pane-death coroutine, tmux-3.5a capture-pane trailing-newline blanking reattach (#515); (3) the Interface computed but never provisioned a shell's worktree (#526). Each found → diagnosed → fixed → re-materialized → retested on ami.
- **Infra:** the pinned Interface stack (tmux 3.5a from source, websockets 16.1.1, @xterm 6.0.0, Node 22) was baked into the sandbox image by the outside team (flag #52 closed); the container restarted onto it mid-sprint with a clean stopping point (seq-8 WIP checkpoint preserved + pushed).
- **Engine foot-guns surfaced:** render-check resolves to the main checkout from a worktree (flags #32/#47); `update.py` ignores `--help` and runs the reconcile (benign, noted); a mid-build worker death drains its task row before dying (L&S — re-send before re-boot); memory data-loss when un-snapshotted writes meet a rebuild (flag #39 — snapshot discipline enforced throughout).
- **Review cycles:** seq 8 (2 rounds: 1 Major + 2 Med → clean), seq 10 (2 rounds: 1 Major + 1 Med → clean), part-B (1 round clean), the 3 fix units (1 round each, clean). No CI-red fights beyond expected rebases.

## Deferred & Follow-ups (post-sprint backlog)

1. **Full real cross-harness sprint gate** — task→red→green→review→merge on Claude+Codex+Kimi, on a real fork (ami). The top verification tier; deferred per decision #35. This is the ami re-try.
2. **Interface QA hardening on ami** — Codex/Sol reviewer investigating at freeze ("working, but tedious/buggy"); its findings + the retry seed the polish backlog.
3. **Conformance Mediums:** M1 hash-or-snapshot-exclude the lease_token; F1 ship an engine tmux.conf / exec `tmux -f`; F3 monotonic-clock the quiet debounce.
4. **flag #60** — shadow NODE_PATH robustness (engine should accept the npm global root as a candidate, or the Dockerfile installs to /opt/sc-shadow — the main sandbox bake diverged; ami built correctly).
5. **Part-B Lows (REV2 L1–L4):** _on_pump_exit future unobserved (silent chain exceptions); _pipe_pane timeout fd/shadow leak; **tmux gate has no CI coverage** (evidence sandbox-local; 810/808 collection delta to reconcile); late .pipeup marker on deadline raise.
6. **Seq-10 Lows (L1–L5):** resolve_batch internal-commit convention; malformed retry-path ValueError; park.reason cross-alert; idempotent replay re-fires notify_binding; stale park-alert on crash mid-retry.
7. **Worktree-fix Lows:** race-loser 500-not-409; only SystemExit curated on provision failure; is_dir-vs-exists edge.
8. **Engine hygiene:** render-check worktree-ROOT resolution (#32/#47); zombie-session reap tooling (#38 → roadmap #22); `update.py --help`.
9. **PLN2:** the automatic same-origin bootstrap relax spec (decision #26→#30).
10. **Housekeeping PRs (FnB gate, independent of this sprint):** #512 (pin tmux from source — closes the flag-#52 residual), #510 (soft-vocab skills posture), #506 (faulted-worker doc).

## Spec Debt

- **API Resources** did not enumerate the operator wake-status / alerts / retry routes — seq 10 chose minimal REST shapes under the existing shell-scope prefix (decision #33). Write the chosen shapes back into the spec.
- **Security And Privacy** bootstrap: the spec's automatic same-origin bootstrap was not what shipped (the interim operator-cap exchange did); decision #30 records the target, PLN2 authors it. Reconcile the spec body to interim-vs-target.
- The three deferred Mediums (M1/F1/F3) are spec-clarification inputs: name the lease_token snapshot exclusion, the engine tmux.conf, and the monotonic-clock quiet baseline explicitly.
- **Fork-deploy assumptions** the gate exposed (shadow materialization, worktree provisioning) should be stated as spec invariants: "the Interface provisions everything a shell needs to exec; it assumes nothing about prior CLI boot."

## Metrics

- 11 sequence units + 3 seq-11 gate fix units = 14 merged PRs on feature #14 (plus the 3 open housekeeping PRs, FnB-gated).
- Conformance: 2 shards, 0 Major, 4 Medium (1 closed / 3 deferred), 18 Low.
- Review cycles: seq 8 = 2, seq 10 = 2, all others = 1.
- Real-fork gate: 3 fork-deploy defects found → fixed → merged.
- Zero scheduled polls by any shell — the whole sprint ran event-driven (task/result/pr_event rows + inbox watcher).
