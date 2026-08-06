# Unit 15 expected output

Deliver a code change on top of current `main` that completes tasks #332 and
#333 only:

- revise `windows_devkit` to name `cmd.exe` as the guest default SSH shell and
  show explicit `powershell -NoProfile -Command` invocation;
- document that guest console stdout may transliterate non-ASCII and that
  byte-exact output should be base64-encoded guest-side;
- mirror both revised Windows skill bodies byte-identically into one new
  trailing reseed migration;
- add focused assertions in `tests/test_windows_vm_skills.py` for both
  statements.

Preserve all behavior merged by earlier Sprint 2 units. Do not expand into the
broader help or GUI application-open follow-ups excluded by reconciliation
decision #158.
