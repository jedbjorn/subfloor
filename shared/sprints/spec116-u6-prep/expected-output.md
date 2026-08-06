A reviewed and merged source-only subfloor maintainer canary outside
`ENGINE_PATHS`, with no distributed `sc` verb. The implementation must include
hermetic coverage for preflight, exact-ref materialization, orchestration,
deadlines, receipt redaction, partial-failure cleanup, and idempotent cleanup.
The post-merge live receipt and dos-app Sprint 5 recovery remain task #353 and
are not claimed by this editing lane.
