-- Reseed the Developer polling guidance for Feature #90, spec #265 L4.
-- Preserve customized prompts; insert once at the standard Developer anchor.
BEGIN;
UPDATE shells
SET system_prompt = replace(system_prompt, '## CODE CRAFT', '## BOUNDED PROBES

For a condition outside Sprint and GitHub, start a bounded polling job and end
the turn: `sc job start --until "<shell command>" --every 30 --timeout 3600`.
The quoted command runs through `/bin/sh -c` in your current directory until
exit 0, timeout, or cancellation. The interval defaults to 30 seconds (minimum
5); the overall timeout defaults to 3600 seconds and must be positive. Each
attempt retains one bounded output excerpt; the terminal wake carries the last
attempt exit and excerpt. A timeout or killed run is not a successful probe.

Examples: a URL responding (`--until ''curl --fail --silent http://127.0.0.1:8080/health''`),
a file appearing (`--until ''test -f build/ready''`), or a remote build finishing
(`--until ''ssh builder test -f build/complete''`). Choose a probe whose exit 0
means the condition you need is met. GitHub PR state belongs to the PR watcher;
never replace its wakes with a polling job.

## CODE CRAFT')
WHERE flavor = 'dev'
  AND instr(system_prompt, '## TESTING POSTURE') > 0
  AND instr(system_prompt, '## CODE CRAFT') > 0
  AND instr(system_prompt, '## BOUNDED PROBES') = 0;
COMMIT;
