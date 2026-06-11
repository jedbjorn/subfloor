---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# test_authoring

Standards for writing and reviewing stringent pytest tests — tests that can actually fail. Use when authoring or reviewing any test under `tests/`.

**Category:** craft

---

# test_authoring — stringent pytest tests

Use this when writing a new test, or reviewing a diff that touches `tests/`.
The goal of a test is to **fail when the code is wrong**. A test that passes
no matter what the code does is worse than no test — it reads as coverage while
guarding nothing.

## The foundation you build on

`tests/conftest.py` builds a throwaway DB from the **real** `schema.sql` + the
post-059 migrations, seeds two tenants (Alice / Bob) + a shared system shell,
and drives the **real** app through `TestClient` with real auth (session cookie
or shell bearer key). Use it:

- Hit real endpoints through the `alice` / `bob` / `admin` / `anon` / `shell_a`
  / `shell_b` callers — do not mock the router or the DB layer.
- Assert against **real rows** in the test DB, not against the payload you just
  sent back to yourself.
- Mock only the true external boundary — outbound IMAP / HTTP / broker egress.
  Never mock the function under test or the logic you're claiming to verify.

## The rules (the floor)

1. **Count + content + negative.** A count assertion (`written == 1`) must be
   followed by a content assertion (the *right* row, with the right fields and
   FKs) **and** a negative assertion (the row that must *not* exist). A bug that
   writes the wrong body, wrong participant, or a stray contact must turn the
   test red. `>= 1` is banned where an exact count is knowable.

2. **No config-mirror tautologies.** Never assert that code output equals a
   constant the code-under-test imports in-process
   (`assert resp == list(THE_SAME_CONSTANT)`). It can only catch hardcoding, not
   a wrong value. Instead: pin the literal expectation in the test, or derive it
   from independent behavior (e.g. the error classes a real `classify_error()`
   actually emits across sample failures).

3. **Round-trips assert the negative space too.** Insert `new`; assert `new` is
   present **and** the prior value is gone **and** sibling fields are untouched.
   `assert get() == put_value` alone passes against a stub that echoes input.

4. **Every error / edge branch gets a test.** If the code has a failure path, a
   reject path, a NULL path, or an empty-input path, each gets its own case.
   Happy-path-only is the most common way a test is "written to pass."
   `is not None` / truthiness is banned where the exact value is knowable.

5. **Negative tests assert the action did not happen, not just the message.**
   For a denied / rejected / gated path, assert the underlying effect is absent
   (no row written, resource still unreachable, no egress call) — not only that
   a 4xx or a `permission_denied` string came back.

6. **Schema changes are tested by behavior, not by `PRAGMA`.** To prove a column
   is nullable, insert a NULL row and assert it's accepted — don't read the
   catalog flag. The pragma can be right while a CHECK or trigger still rejects.

7. **Idempotency / migration tests run on a *dirty* fixture.** Seed the exact
   state the migration is meant to clean (the rows it removes still present),
   then run it once and twice, asserting convergence. Idempotency-on-clean is
   nearly free to pass and proves almost nothing.

8. **Reject silent-empty.** A bad filter / typo'd enum value must 422, never a
   200 reading as "nothing found." Assert the rejection explicitly.

## Review lens (use when reviewing a tests/ diff)

- Read the assertions, not the test name. Does any realistic bug survive them?
- For each `assert`: name a one-line code change that would still pass it. If
  that change is a real bug, the assertion is too weak.
- Count-only? Substring-only? `is not None`? — demand the exact value.
- Does the test compare output to a constant the code imports? — flag rule 2.
- Is only the success branch tested? — name the missing edge and require it.

## Mechanizable subset (enforce in CI, not just here)

These are grep-able and belong in a `.github` workflow that fails the build, so
the floor holds even when this skill isn't loaded:

- `assert .* (==|!=) (list|set)\(<KNOWN_CONSTANT>\)` — config-mirror shape.
- `assert .* >= 1` / bare `assert .* is not None` in a new test diff — demand an
  exact value.
- a count assertion with no content assertion in the following N lines.

A skill teaches the judgment; CI enforces the floor. Wire the CI failure message
to point back at this skill.

## Never

- Mock the function under test, then assert the mock returned what you set.
- Assert a key exists without asserting its value.
- Let a count or status code stand in for "the right thing happened."
- Test only the happy path for code that has error branches.
- Ship a test whose assertions no realistic bug could violate.
