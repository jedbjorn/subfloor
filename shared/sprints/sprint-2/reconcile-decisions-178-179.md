# Sprint 2 episode-2 reconciliation required: decisions #178 and #179 conflict

No new Planner state change has been made for #179.

Decision #178 from REV2 arrived first and was already executed before #179 was delivered:

- task #331 corrected to `done`;
- tasks #334–#336 created against document 108;
- unit 16: `Windows GUI app-open and help corrections`, code, wave 1, tasks #334/#335, DEV3 (shell 5) → REV2 (shell 8);
- unit 17: `rst-c downstream acceptance re-run, episode 2`, report-only, wave 2, task #336, DEV4 (shell 6) → REV3 (shell 13), depends on `[16]`;
- Sprint 2 was already armed; `arm` was rejected twice with `requires prepared; found armed`, recorded as flag SC-472; dispatch then succeeded.

Decision #179 asks for a different projection: a dependency-free report-only lane on DEV6→REV1 using a fresh Codex subprocess, plus a parallel tests-only code lane on DEV3→REV2. Cutting that now would duplicate the acceptance gate, violate #178's single dependency-held rerun, and collide with the already-created DEV3 lane. Released/completed units also cannot be silently rewritten.

Please reconcile jointly and return one durable superseding decision that explicitly states:

1. whether units 16 and 17 stand exactly as created;
2. whether either still-planned unit should be replanned or cancelled, naming the unit id and complete replacement fields;
3. whether #179's fresh-process method and interactive-argv tests should be folded into the existing units, deferred, or replace them;
4. confirmation that exactly one further VM-touching acceptance lane remains authorized.

Until that decision arrives, the Planner will create no duplicate tasks, units, Codex-recipe lane, or VM-touching lane. Existing safe work may continue under durable state.
