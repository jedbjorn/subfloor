# Windows GUI app-open and help corrections

Deliver tasks #334 and #335 on top of current integrated `main`.

- In both authoritative Windows skills, state that `./sc vm exec` runs in the SSH session context and cannot open a GUI application on the interactive desktop. Route an absent GUI application through the injected Windows MCP `App` tool and require visual confirmation before proceeding.
- Add `vm push`, `vm exec`, and `vm capture` to the top-level `./sc help` catalogue.
- Replace delivered `claude mcp add` relay guidance in `dispatch.sh` and `vm_mcp_relay.py` with managed adapter injection plus `./sc vm mcp up`.
- Add the `cmd.exe` default-shell and lossy-stdout/base64 guidance to `vm exec --help`.
- Carry both revised skill bodies in one new trailing, idempotent reseed migration and add focused regressions, including `tests/test_windows_vm_skills.py`.
- Preserve all previously merged VM lifecycle, structured-result, adapter-gating, and skill behavior. Do not broaden into the report-only acceptance lane.

Verification must cover focused help/skill/migration tests, freshness/convergence, render-check, and the repository CI gate. The reviewed PR head must contain no unresolved Critical, Major, or Medium finding before merge authorization.
