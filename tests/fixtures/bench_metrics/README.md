# Benchmark metric fixture provenance

The Codex rollout is the unmodified file written by Codex on 2026-09-07
(session `01a07d6d-0032-74d3-9df9-53729ee9a4be`). A trivial read-only capture
ran from `/tmp`, with user config ignored, requesting one shell command:
`printf METRIC_CAPTURE`, followed by `DONE`. Model: `gpt-5.6-sol`.
The original rollout records the CLI version, timestamps, tool call, and
cumulative usage. Its exec wrapper contains one actual shell execution.
No events were authored, trimmed, or rewritten for these tests.

The tests also reuse the real Claude and OpenCode captures in `live_model/`;
see that directory's README for provenance. Claude's PLN1 specimen proves
multi-block deduplication and two Bash calls. Its subagent specimen proves
parent-session attribution. OpenCode's oc-sub session proves that a non-shell
tool counts as a call but not a shell execution, and excludes the child's
separate session. The quota-failed Codex specimen proves absence of usage
and latency is null, while a captured absence of tool calls is measured zero.

Tests corrupt copies and remove a table to exercise failure/format drift;
the committed captures themselves are never modified.
