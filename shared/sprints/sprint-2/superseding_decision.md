# Sprint 2 — superseding Reviewer decision (supersedes #183; ratifies #178; voids #179)

`decision`: **re-enter**. Delivery continues. Conclusion does **not** govern.
This single decision replaces my #183 and answers #184.

## 1. #184's conclusion was right; its stated premise is wrong

You were correct to refuse #183. But there is **no terminal-unit lifecycle gate
on `complete`**. `server.py:2506-2527` records the final report, then calls
`SprintLifecycleStore.transition(sprint_id,'completed',...)`;
`sprint_domain.py:309-342` checks only the lifecycle edge, actor authority, and
`terminal_outcome`. It never queries `sprint_work_units`. The all-terminal count
exists only in `_queue_delivery_terminal` (2419-2434), which gates a
*notification*.

So `sc sprint complete` would have **succeeded**, closing Sprint 2 as
`completed` with unit 16 active (PR 1049 green, unmerged) and unit 17 planned.
Your judgment, not an engine gate, prevented that. Flag **#210**.

## 2. Authority: the conflict is an engine defect, not contested judgment

`_queue_delivery_terminal` (`sprint_domain.py:2443-2475`) loops **every**
`role='reviewer'` participant and sends each the identical mandate "send the
Planner either a re-enter decision or your conclude decision". At ev279
(13:29:57) REV1, REV2 and REV3 all received it. Three reviewers then acted in
good faith on one broadcast. `record_conformance` (`sprint_close.py:56-78`)
gates only on `_require_reviewer` + idempotency key — no exclusivity, no
supersede path. Flags **#207**, **#208**.

I am the single governing conformance Reviewer for Sprint 2: I signed the
pre-declaration QAQC for document 108 (approval_id 7, reviewer_shell_id 7),
I authored the only conformance report (#3, ev280), and FnB has assigned me
Reviewer for this recovery — a board-level authority record under decision #46.
REV2 and REV3 acted under the defect, not out of turn. No blame attaches.

## 3. Ordering correction

#184 states units 16/17 pre-date #183. True. But they also **post-date my
conformance report**: ev280 `conformance.recorded` 13:39:59 → ev281 unit 16
13:42:26 → ev283 unit 17 13:42:32. My delivery-terminal wake was **not** stale
(9/9 terminal at ev279). Report #3 was validly recorded against the 9-unit
projection, then invalidated in scope three minutes later by a plan reopening
the engine should have refused. Flag **#209**.

## 4. Legal disposition — units 16/17

**Unit 16** (`active`, `code`, tasks 334/335, DEV3→REV2, PR 1049 green
13:57:24, review not yet requested) has **exactly one** legal terminal path:

- `cancel-unit` → **ILLEGAL**: "only planned work units may be cancelled"
  (`sprint_domain.py:2305`).
- `complete-unit` → **ILLEGAL**: "code work units complete only through the
  merge judgment chain" (`sprint_domain.py:2235`).
- `replan-unit` → **ILLEGAL**: "only planned work units may be replanned"
  (`sprint_domain.py:2137`).
- **LEGAL**: `request-review` → `record-review approved` → `authorize-merge` →
  merge observed → `completed`.

This is why conclusion cannot govern: unit 16 has no legal disposition that
permits an honest close now. Any conclude would either fabricate a disposition
or exploit the missing gate in §1.

**Unit 17** (`planned`, `report_only`, task 336, DEV4→REV3, depends `[16]`):
`cancel-unit` and `replan-unit` are both legal. I decline both. Task 336 is the
only work that can close follow-ups #6 and #7; cancelling it would leave spec
108's §Acceptance live-session gate permanently unproven. **Keep as planned.**

## 5. Tasks #334-#336 — ratified, not new scope

Bound revision `4b1b2cf7…` equals current; `mid_sprint_edits` is empty, so no
spec drift. The three tasks land exactly on my own recorded follow-ups:

- 334 "Route GUI application opening through the Windows MCP App tool" → #8
- 335 "Complete the `./sc help` Windows VM catalogue" → spec 108 catalogue
- 336 "Complete the §Acceptance downstream gate in one session" → #6, #7

334/335 are already `done`; 336 is `pending`. REV2's #178 and my report #3 agree
on **substance** and differ only on **timing** — it judged in-Sprint patching,
I judged post-Sprint follow-up. Since the patch is already durable, coded and
green, and it is legally unrecallable (§4), **#178's timing judgment governs.**
I ratify it and withdraw mine.

## 6. Single governing projection

**The 11-unit projection created by #178 is the sole governing projection.**
Decision **#179 is void** — do not execute it, now or later. If any part of
#179 named work not in the 11-unit projection, it must be cut post-Sprint, not
absorbed. Create **no** further work units for Sprint 2 without a new decision
from me.

## 7. Exactly one authorized VM-touching lane: **unit 17**

There is one physical VM (domain `W10C_DOS-ARCH_Testing`, one `clean`
snapshot). Units 12 and 14 each ended with a sole `./sc vm reset --off`; two
concurrent lanes would race that reset and mutually destroy evidence.

- **Unit 17 — AUTHORIZED.** Sole VM contact for the remainder of Sprint 2.
- **Unit 16 — NOT authorized.** Code plus automated regressions only; zero VM
  contact, even to smoke-test the MCP App routing it implements.

Unit 17's dependency on `[16]` already serialises this. Do not relax it.

## 8. Report #3 and the next conformance boundary

Report #3 stands as append-only evidence of the 9-unit projection; it is
**scope-superseded, not withdrawn**. Follow-ups #6-#9 remain pending FnB
disposition. #6/#7/#8 are now expected to be *answered* by units 16/17; #9
(unbounded `expected_output`) is unrelated and stays post-Sprint.

When 16 and 17 reach terminal, `terminal_count` becomes 11 and a **new**
delivery-terminal wake will broadcast to all three reviewers again, re-running
this exact race. Containment: **only REV1 (shell 7) may record conformance for
Sprint 2.** REV2 and REV3 must treat that notification as informational —
`accept` it and exit. I will then record a second conformance report under a new
idempotency key and author the final report.

## 9. Action for PLN1

1. Execute nothing from #183 or #179.
2. Resume normal delivery on the 11-unit projection: unit 16 through the merge
   chain, then unit 17 on release.
3. Relay §7 (one VM lane) and §8 (single conformance Reviewer) to DEV3, DEV4,
   REV2, REV3 before unit 17 releases.
4. Surface flags #207-#210 to FnB — four Sprints v2 engine defects; #210 is the
   host-safety one.
5. No corrected final report is provided: authoring one now would assert a close
   that is illegal under §4. It follows the 11-unit conformance pass.

Evidence: `shared/sprints/sprint-2/evidence.json`, `/_sc/sprint/2/timeline`
(events 278-290), report #3, follow-ups #6-#9, flags #207-#210.
