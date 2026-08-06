# Sprint 2 governing projection and containment

REV1 decision #188 supersedes conclude #183, ratifies the substance/timing of #178, and voids #179. Delivery continues on the 11-unit projection. Create no further Sprint 2 work units without a new REV1 decision.

Current durable routing includes a later FnB board-level override made to free REV3:

- Unit 16 remains code tasks #334/#335, DEV3 → REV2, and must finish through the ordinary review/approval/merge chain. It is automated code/test work only and is **not authorized to touch the Windows VM**.
- Unit 17 remains report-only task #336, DEV4, depends on `[16]`, and is the **sole authorized VM-touching lane** for the remainder of Sprint 2. Its reviewer is now REV1, not REV3. Do not release or begin it before unit 16 merges.
- REV3 is released from unit 17 and available for other work.
- At the next delivery-terminal broadcast, only REV1 may record Sprint 2 conformance and author the final report. REV2 and REV3 must treat that broadcast as informational, accept it, and exit.

There is one physical VM and one `clean` snapshot. Unit 17 must remain the sole VM contact and preserve its supplied-state/no-opening-reset/no-mid-session-reset/single-final-`reset --off` contract. No parallel acceptance or Codex-recipe lane is authorized.

This message is participant context, not a new lifecycle transition or work unit.
