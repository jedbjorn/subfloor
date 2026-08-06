# Unit 13 expected-output correction

Unit 13's durable `expected_output` field was populated in error from the
contents of a stale pre-Sprint `.super-coder/scripts/vm.py`. It is not a
deliverable specification and must not be copied, restored, or treated as the
target file content.

The authorized deliverable is exactly tasks #329 and #330 applied on top of
current `main`, preserving every behavior merged by Sprint 2:

- distinguish an unset/unknown `SC_HARNESS` state from a declared-unsupported
  adapter, with the specified recovery guidance and regressions;
- gate managed MCP injection on a linked VM, with linked/unlinked coverage for
  Claude, Codex, and OpenCode.

Do not reduce or revert merged VM behavior. REV1 is the assigned Reviewer and
will review against current integrated `main` plus tasks #329/#330, not against
the corrupt `expected_output` blob.
