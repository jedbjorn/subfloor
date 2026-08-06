# rst-c downstream acceptance re-run, episode 2

Deliver task #336 as a report-only lane after the code unit merges. Use exactly one supplied-state `rst-c` session and exactly one final `reset --off`.

The first evidence line must pin the integrated engine revision. Launch through the engine after that merge and reconcile `rst-c` to the pinned head. Before any other acceptance step, prove that `SC_HARNESS` is set and `./sc vm status --json` reports the adapter as supported. If either check fails, stop and report the run invalid; do not submit it as gate evidence.

If the preflight passes, perform the complete sequence: status; start only if off; open the absent GUI application through the injected Windows MCP `App` tool and confirm it by observation; push; complex exec through an explicit PowerShell command file; capture and confirm a non-black viewable artifact; `mcp up` with endpoint ready; at least one GUI observation and one GUI action using Snapshot then Click/Type and Screenshot verification; `mcp down`; then the sole final `reset --off` with powered-off confirmation.

No opening or mid-session reset is permitted. Record first-turn tool evidence and every structured error. Claim no unconfirmed step. Produce the durable acceptance report only; make no code changes and create no second VM-touching or retry lane.
