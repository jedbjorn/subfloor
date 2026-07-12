---
name: test_authoring
description: Principles for stringent pytest tests — tests a realistic bug turns red. Pair with a granted stack-infra testing skill (test_authoring_sqlite / test_authoring_pg / a fork-local one) if the shell has one.
category: craft
common: false
---

# test_authoring — stringent pytest tests

Apply when writing a test or reviewing a diff that touches `tests/`.
Pass condition for any test: a realistic bug turns it red. A test no bug
can fail reads as coverage while guarding nothing — sharpen or cut it.

Stack infra (fixture setup, callers, DB access pattern) lives in the granted
stack skill — `test_authoring_sqlite` / `test_authoring_pg` / a fork-local
skill that supersedes this one. Load it alongside. None granted -> this skill
stands alone; do NOT hunt for one the fork doesn't ship.

## Rules (the floor)

1. **Count + content + negative.** After a count assertion (`written == 1`),
   assert the content (the right row: fields + FKs) + the negative (the row
   that must NOT exist). Wrong body / wrong participant / stray contact must
   turn the test red. `>= 1` is banned where the exact count is knowable.

2. **No config-mirror tautologies.** NEVER assert output equals a constant
   the code under test imports in-process
   (`assert resp == list(THE_SAME_CONSTANT)`) — it catches hardcoding only,
   never a wrong value. Pin the literal expectation in the test, or derive it
   from independent behavior (e.g. the error classes a real
   `classify_error()` emits across sample failures).

3. **Round-trips assert the negative space.** Insert `new` -> assert `new`
   present + prior value gone + sibling fields untouched.
   `assert get() == put_value` alone passes against a stub that echoes input.

4. **Every error / edge branch gets its own case.** Failure path / reject
   path / NULL path / empty-input path -> one test each. `is not None` /
   bare truthiness banned where the exact value is knowable.

5. **Negative tests assert the effect is absent.** Denied / rejected / gated
   path -> assert the underlying action did not happen (no row written,
   resource still unreachable, no egress call) — a 4xx or a
   `permission_denied` string alone does not pass.

6. **Schema changes: test behavior, not `PRAGMA`.** To prove a column
   nullable, insert a NULL row -> assert accepted. The catalog flag can be
   right while a CHECK or trigger still rejects.

7. **Idempotency / migration tests run on a dirty fixture.** Seed the exact
   state the migration cleans (the rows it removes still present) -> run once
   and twice -> assert convergence. Idempotency-on-clean proves almost
   nothing.

8. **Reject silent-empty.** Bad filter / typo'd enum value -> assert 422
   explicitly, never a 200 reading as "nothing found."

## Review lens (tests/ diff)

- Read the assertions, not the test name.
- Per `assert`: name a one-line code change that would still pass it. That
  change is a real bug -> the assertion is too weak; demand the fix.
- Count-only / substring-only / `is not None` -> demand the exact value.
- Output compared to a constant the code imports -> flag rule 2.
- Only the success branch tested -> name the missing edge + require it.

## Mechanizable subset (enforce in CI)

Grep-able; wire into a `.github` workflow that fails the build so the floor
holds when this skill isn't loaded. Point the CI failure message back at
this skill.

- `assert .* (==|!=) (list|set)\(<KNOWN_CONSTANT>\)` — config-mirror shape.
- `assert .* >= 1` / bare `assert .* is not None` in a new test diff —
  demand an exact value.
- Count assertion with no content assertion in the following N lines.

## Never

- Mock the function under test, then assert the mock returned what you set.
- Assert a key exists without asserting its value.
- Let a count or status code stand in for "the right thing happened."
- Test only the happy path of code that has error branches.
- Ship a test whose assertions no realistic bug could violate.
