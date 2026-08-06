# Sprint 2 re-entry projection conflict — Planner reconciliation request

Three episode-1 Reviewer decisions now prescribe incompatible projections.

Durable state already created from decision #151 (REV3):

- tasks #329–#331 were added to spec document #108;
- unit 13 (`code`) binds #329–#330 to DEV6/REV1, wave 1;
- unit 13 was released as message #153 / wake #165 and accepted by DEV6, so it cannot be replanned;
- unit 14 (`report_only`) binds #331 to DEV4/REV2, wave 2, depends on unit 13, and remains unreleased.

Decision #152 (REV1) instead requests an immediate dependency-free DEV4/REV3 acceptance lane, a parallel DEV3/REV2 documentation lane, and a conditional DEV6/REV1 injection lane only if acceptance is negative.

Decision #154 (REV2) instead requests a broader DEV3/REV2 skill/help correction lane, then DEV4/REV3 acceptance depending on that lane.

The spec permits one supplied-state rst-c acceptance session and one final reset. Creating a second report lane or removing unit 14's dependency without a unified decision would risk duplicate VM mutation and would silently override another Reviewer's projection.

Please return one exact durable reconciliation decision that names:

1. which incremental documentation/help tasks must be added beyond #329–#331;
2. whether planned unit 14 must remain, be cancelled, or be replanned, including exact reviewer and full dependency set;
3. whether unit 13 should simply finish as released or whether any later corrective unit is required after it;
4. confirmation that only one acceptance rerun may be released.

Until that decision arrives, unit 13 may continue safely, unit 14 stays blocked, and no duplicate acceptance or additional code lane will be created.
