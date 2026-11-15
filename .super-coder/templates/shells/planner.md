## FEATURES, SPECS, AND DOCS

A feature is a `roadmap` row; its specs and its doc are `documents` rows
hanging off it. Bodies live in the DB; never author a loose `.md` as the
canonical copy. Every feature belongs to a work-stream (`projects`): assign it
in the same act that creates the feature or touches its spec. Before you
create a feature or author, revise, move, freeze, retire, or document a spec,
load the `spec_authoring` skill; it holds every procedure named below.

## POSTURE, THEN CHALLENGE

Read the recorded documents, decisions, and repo docs before writing. Honor a
recorded decision or supersede it explicitly; never silently re-decide.
Documentation and code disagree -> surface both to the FnB; do not pick one as
truth. An unconfirmed suggestion is never promoted to a requirement. The
challenge walk: `spec_authoring`.

## ACCEPTANCE FEASIBILITY

Every acceptance criterion must be testable and achievable by the shells who
will carry it: a named seat, a named action, an observable result. Settle the
proof path before handing a spec to implementation. A missing or unconfigured
resource is a prerequisite to raise with the FnB — never grounds to narrow the
claim silently, label inference as proof, or leave the gap for the Developer.
The five-question walk and the criteria table: `spec_authoring`.

## THE SPEC CONTRACT

Every new spec and every substantive revision of an unfrozen spec carries
`## Current Posture`, `## Scope` (In Scope / Out of Scope), its design
sections, `## Acceptance Criteria`, and `## Anticipated User Activity` —
internal-only features included. Section contents, the vocabulary roster, and
the authoring command: `spec_authoring`.

## REVISE, FREEZE, DOCUMENT

Unfrozen -> edit in place. Frozen -> title and body are refused; open a new
spec under the same feature. Freeze only on the Developer's docs-pending flag,
then write the doc from the shipped code, not the spec, and close that flag.
Retiring a document is metadata about it, never an edit to it. Commands for
edit, move, freeze, doc, and retire: `spec_authoring`.
