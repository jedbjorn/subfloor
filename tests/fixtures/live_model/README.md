# live_model fixtures — provenance

Every file here is a **real transcript written by a real run of the harness it
belongs to**, captured on 2026-07-25 for spec doc 44 (feature 17), sprint 45
unit 44-U1. Nothing is hand-authored, trimmed, or reshaped: what a harness
wrote is what is committed.

That is a requirement, not a preference. Decision #55 and spec doc 44's
acceptance section both bind it — "a test asserting against data that cannot
occur manufactures confidence". If a fixture ever needs to change, **re-capture
it**; do not edit it.

## How these were produced

Throwaway sessions in throwaway working directories, each driven headlessly
with trivial prompts (`Reply with exactly one word: ALPHA` …) so the captured
content carries no real work. The model switch is a genuine per-invocation
model selection, resumed into the same session — the same path an operator's
in-harness `/model` switch takes through the writer.

```
claude    claude -p "…" --model haiku      then  claude -p -c "…" --model sonnet
kimi      kimi -m kimi-code/k3 -p "…"      then  kimi -c -m kimi-code/kimi-for-coding -p "…"
opencode  opencode run -m ollama-cloud/gpt-oss:120b "…"
          then opencode run -c -m ollama-cloud/qwen3.5:397b "…"
```

## What each fixture is for

| Path | Worktree recorded inside | Case | Model sequence |
|---|---|---|---|
| `claude/projects/-tmp-lm-capture-claude-single/` | `/tmp/lm-capture/claude-single` | single-model | haiku, haiku |
| `claude/projects/-tmp-lm-capture-claude-ab/` | `/tmp/lm-capture/claude-ab` | **mid-session switch** | haiku → sonnet |
| `claude/projects/-tmp-lm-capture-claude/` | `/tmp/lm-capture/claude` | **switch-back** | haiku → sonnet → haiku |
| `claude/projects/-tmp-lm-capture-claude-sub/` | `/tmp/lm-capture/claude-sub` | subagent noise | main sonnet, subagent haiku |
| `kimi/sessions/wd_kimi-single_*/` | `/tmp/lm-capture/kimi-single` | single-model | k3, k3 |
| `kimi/sessions/wd_kimi-ab_*/` | `/tmp/lm-capture/kimi-ab` | **mid-session switch** | k3 → kimi-for-coding |
| `kimi/sessions/wd_kimi_*/` | `/tmp/lm-capture/kimi` | **switch-back** | k3 → kimi-for-coding → k3 |
| `kimi/sessions/wd_kimi-sub_*/` | `/tmp/lm-capture/kimi-sub` | subagent noise | main ends kimi-for-coding, `agent-0` k3 |
| `opencode/opencode.db` | four `session.directory` values | all four cases | see below |

`opencode/opencode.db` is a single **real** opencode database written by an
isolated `XDG_DATA_HOME`, holding one session per case — `oc-single`,
`oc-ab` (gpt-oss → qwen3.5), `oc-back` (gpt-oss → qwen3.5 → gpt-oss) and
`oc-sub` (parent ends qwen3.5, child session on gpt-oss). One file rather than
three keeps it to 400 KB and exercises "select the session for THIS directory"
the way the real DB does.

### The two wild claude specimens

```
claude/projects/-home-j3d1-dos-arch--sc-worktrees-pln1/ea436171-….jsonl
claude/projects/-home-j3d1-dos-arch--sc-worktrees-dev2/b0fce1e9-….jsonl
```

These are **not** capture runs — they are untouched wild sessions, kept because
they contain the one shape a capture run cannot be made to produce on demand:
an assistant record whose `message.model` is the literal `<synthetic>`. Claude
writes it on locally-generated records ("No response requested.", API-error
stubs); there are 50 in the scout's corpus and a naive last-record probe
reports it as the live model. PLN1 ruling (c) of sprint 45 put them in scope
and pointed at these specimens.

- `ea436171` — real `claude-sonnet-5` records, then `<synthetic>` **last**: the
  probe must skip it and report `claude-sonnet-5`.
- `b0fce1e9` — 8 lines, its only assistant record is `<synthetic>`: the probe
  must report nothing (`none`), not a placeholder.

## Why these paths look absolute

The probe's input is the shell's worktree, and each format records that path
itself (claude per-record `cwd`, kimi `state.json.workDir`, opencode
`session.directory`). The tests pass the same paths the captures ran in, so the
fixtures must keep their original directory names — including claude's lossy
`-`-encoded project-dir names, whose prefix collisions
(`-tmp-lm-capture-claude` is a prefix of three others) are themselves under
test.

## codex and vibe — `unsupported`

- **vibe** records the model once per session (`meta.json` `config.active_model`)
  and never per message — it cannot express a switch. No fixture.
- **codex** records `turn_context.payload.model` per turn, which would be
  enough, but nothing in its rollouts distinguishes a subagent turn from a main
  one (651 rollouts inspected, zero agent-attribution keys), and the account
  was out of credits, so no capture run could settle it. Per spec doc 44's own
  rule a harness that cannot make the distinction is `unsupported` rather than
  sometimes-wrong. Tracked as flag #174 for a revisit after the billing window.

```
codex/sessions/2026/07/25/rollout-2026-07-25T11-18-45-019f98ff-….jsonl
```

This one IS kept, at PLN1's direction. It is the rollout codex wrote during the
U1 capture attempt that died on the usage limit: real bytes, `session_meta` +
one `turn_context` carrying the explicit model `gpt-5.5`, and no assistant
output — a genuine "no answer yet" specimen in codex's shape.

It is not decoration. `test_codex_stays_unsupported_even_with_a_real_rollout_present`
uses it to pin the thing most likely to go wrong at the revisit: codex has a
perfectly readable model id sitting right there, and `unsupported` must hold
anyway, because the verdict is a claim about main-vs-subagent attribution and
not about whether a model id can be found.
