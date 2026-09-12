## FEATURES, SPECS, AND DOCS

A feature is a `roadmap` row, alive from `brainstorm` onward. Specs
(`documents`, kind `spec`) hang off the feature, ordered by `seq`; a feature
may hold several unfrozen specs at once, and freezing one never gates the
others. A spec is **shipped** when frozen, **active** when unfrozen with rows
in `spec_tasks`, otherwise **backlog**. The **doc** (kind `doc`) is the
feature's readable face, written when its first spec ships. Bodies live in
the DB; never author a loose `.md` as the canonical copy.

Every feature belongs to a work-stream (`projects`). On every feature create,
spec author, or spec update, assess it in the same act: `sc mem get projects`;
a new feature -> `sc mem roadmap add "<title>" --project <shortname>`;
ungrouped -> `sc mem roadmap project <feature_id> <shortname>`; no fit ->
`sc mem project add <shortname> "<title>" --purpose "…"` then assign. Ask the
FnB only when several streams fit. Quick fixes need no feature.

## POSTURE, THEN CHALLENGE

Before writing: `sc mem get documents`, `sc mem get decisions`, and the repo's
own docs (`sc map-sql "SELECT path FROM dr_filepath WHERE role='doc'"`). A
spec that touches a recorded decision honors it or supersedes it explicitly
with `sc mem decision "…" --parent <id>`; never silently re-decide.
Documentation is the preferred account of intended posture; when it is absent,
ambiguous, or plausibly stale, verify the narrow code path and name that
fallback in the spec. Documentation and code disagree -> surface both to the
FnB; do not pick one as truth.

Walk the proposed workflow end to end. Challenge contradictions, missing
boundaries, hidden assumptions, audience mismatches, partial failure,
concurrent use, permissions, and the unhappy path. Ask the FnB to resolve
anything that could change implementation or acceptance; a deferral belongs in
Out of Scope with the boundary it leaves behind. The conversation is input;
the spec is the resolved contract: settled FnB decisions sit beside the clause
they govern, and an unconfirmed suggestion is never promoted to a requirement.

## ACCEPTANCE FEASIBILITY

Before committing acceptance criteria or handing a spec to implementation,
walk each criterion through these questions in order. Apply this to new specs,
substantive revisions, and existing specs entering a Sprint.

1. **Can we meet it with available resources?** Name the environment, tools,
   access, data, people, and time needed, and establish what is available now.
   A hoped-for capability is an unresolved prerequisite, not a test plan.
2. **What evidence is worth obtaining?** Separate directly proven success
   from inferred success. Weigh the value of direct proof against its cost;
   assess how accurately success can be inferred from available tests and
   observations, naming assumptions, blind spots, and residual uncertainty.
   Choose and justify the evidence level with the FnB when it changes the
   acceptance promise. Never label inference as proof or demand direct proof
   merely because it sounds stronger. Evidence choices do not waive the
   correctness, authorization, or tenancy requirements themselves.
3. **Is post-implementation verification practical in Dev?** Describe the
   setup, action, observable result, and cleanup on the actual available seat.
   Bound the effort and state what that environment can and cannot establish;
   mocks or a Dev pass do not prove behavior beyond their demonstrated reach.
4. **Can the FnB supply the missing proof?** Consider concrete privileged
   console commands run by the FnB in cooperation with shells during or after
   the Sprint. Specify the target, commands, expected evidence, shell role,
   cleanup, and timing; confirm FnB participation before depending on it.
   Distinguish Sprint completion from any pending post-Sprint acceptance, and
   keep unperformed checks explicitly pending.
5. **Is a privileged process still justified?** Only if direct proof remains
   required and the preceding paths are insufficient, consider implementing a
   narrowly scoped privileged process. State why it is worth building, its
   exact operations and authority, lifetime, cleanup, and verification path.
   Resolve that scope with the FnB before adding it to implementation; a
   verification gap is not authority to build general privileged tooling.

Record the result in `## Acceptance Criteria`: for each criterion, give the
claim, required evidence level and rationale, verification method and seat,
owner and timing, prerequisites, and observable pass condition. Keep this
proportional to the work; a compact table is sufficient. Unavailable required
proof -> resolve the prerequisite, agree a narrower claim or justified
inference, or defer the criterion explicitly before releasing dependent work.
If feasibility breaks during implementation, return to the FnB and the normal
spec revision/Sprint rebind procedure before expanding verification scope.
Do not keep adding code to chase an acceptance claim the resources cannot prove.

## THE SPEC CONTRACT

Every new spec and every substantive revision of an unfrozen spec carries:

| section | holds |
|---|---|
| `## Current Posture` | related systems and behavior before the change; documents and decisions consulted; code paths read because documentation was missing, ambiguous, or stale |
| `## Scope` → `### In Scope` / `### Out of Scope` | what this delivery adds, changes, or removes; what it deliberately excludes and the boundary retained ("not in this delivery", never "never") |
| design sections | synthesized FnB decisions and rationale beside the requirement they constrain |
| `## Acceptance Criteria` | per-criterion evidence choice, feasibility, verification method, owner, timing, prerequisites, and observable pass condition from ACCEPTANCE FEASIBILITY |
| `## Anticipated User Activity` | `### Vocabulary`, `### Expected Activity`, `### Reach`, `### Audience and Assurance`, `### Data Tenancy`, `### Beyond Intention`; about 60 lines at most |

Vocabulary roster: Valid Privileged User, Valid User, Visitor, Future
Potential User, System, Shell, Unexpected Participant. Audience postures:
Unknown, Authenticated, Operational, Technical, Administrator. Separate process
curation (how much explanation a surface needs) from safety hardening
(correctness, validation, authorization, tenancy, safe failure — never waived
by expertise). Soft vocabulary, hard invariants: write anticipated activity,
Unexpected Participant, Beyond Intention, Reach, and tenancy; never threat
model, attack, adversary, exploit, abuse case, vulnerability, breach,
privilege escalation, exfiltration, or malicious. Internal-only features still
carry the section.

Author with
`sc mem doc add "<title>" --kind spec --feature <id> --body-file ./draft.md --render-path specs_sc/<slug>.md`;
`--seq` auto-advances. Body format: the `themed_markdown` skill.

## REVISE, FREEZE, DOCUMENT

Unfrozen -> edit in place: `sc mem doc edit <document_id> --body-file ./draft.md`
(also `--title`, `--render-path`); no new row, no seq bump. Frozen -> title
and body are refused; open a new spec under the same feature; only
`--render-path` still moves. When a new era makes a feature's history
misleading: create the fresh feature with its work-stream, run
`sc mem doc move <document_id> --feature <target>` (atomic across the spec,
its tasks, and document-linked decisions; refuses frozen, doc-kind, terminal,
or Sprint-bound), re-read under the target, then retitle the old feature and
set its truthful terminal status.

On the dev's docs-pending flag: `sc mem doc freeze <document_id>`; read the
shipped code, not the spec, and write
`sc mem doc add "<feature> — how it works" --kind doc --feature <id> --body-file ./draft.md --render-path docs_sc/<slug>.md`;
then `sc mem flag close <flag_id> --notes "Spec frozen; doc <id> written → docs_sc/<slug>.md"`.
Until that close, shipped + open flag is the truthful interim state.
