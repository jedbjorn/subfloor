---
title: super-coder — Harnesses & models
tags: [substrate, shells, agentic-coding, harness-agnostic, sqlite]
date: 2026-07-20
project: super-coder
purpose: Plans over API keys, flavor model defaults, the sprint interview
---

# Harnesses & models

## Harnesses & models

> [!class2]
> **UI** Shells (flavor model defaults) · **Shells** all five flavors

### Prefer a subscription plan over a raw API key

Agentic coding burns **huge** token volume — multi-step loops, large context,
constant re-reads. Metered per-token API billing scales with every one of those
tokens and gets expensive fast. A flat **subscription plan** is generally far
cheaper *and* predictable for this workload, so we recommend running each harness
against its plan rather than its pay-as-you-go API:

| Harness | Provider | Recommended plan |
|---|---|---|
| **Claude Code** | Anthropic | [Claude Pro / Max](https://claude.com/pricing) |
| **Codex** | OpenAI | [ChatGPT Plus / Pro](https://openai.com/chatgpt/pricing/) |
| **Vibe** | Mistral | [Mistral plans](https://mistral.ai/pricing) |
| **Kimi Code** | Moonshot AI | [Kimi memberships (Moderato / Allegretto / …)](https://www.kimi.com/help/membership/membership-pricing) |
| **OpenCode** → open-weights | Ollama | [Ollama Cloud (or run local, free)](https://ollama.com/) |

Codex exists for exactly this reason — a ChatGPT account bills **flat, with no
per-token metering**. OpenCode with a raw API key stays the **metered catch-all**:
reach for it when you need a model no plan covers, accepting per-token cost. Ollama
goes one further — open-weights models you can run **locally for free** on your own
hardware, or on Ollama Cloud's plan.

### Why each role defaults to the model it does

Every shell has a **flavor** (its role); each flavor ships an advisory model
default per harness (the `flavor_defaults` table — the picker pre-selects it;
`--harness` / `-m` / the picker override). The doctrine:

| Flavor | Job | Codex | Claude | OpenCode (open-weights) |
|---|---|---|---|---|
| **planner** | architecture, plans | `gpt-5.5` | `fable` ★ | `deepseek-v4-pro` |
| **reviewer** | adversarial review | `gpt-5.5` | `fable` ★ | `glm-5.2` |
| **dev** | write the code | `gpt-5.6-sol` ★ | `opus` | `qwen3-coder-next` |
| **cartographer** | map the repo | `gpt-5.6-terra` ★ | `sonnet` | `glm-5.2` |
| **admin** | own the substrate, maintain `main` | `gpt-5.5` | `opus` ★ | `deepseek-v4-pro` |

★ = the harness the picker pre-selects for that flavor.

The logic — defaults are set from **sprint success telemetry** (which
model/flavor pairings actually land reviewed, merged work across the fleet),
re-fit as the telemetry moves, plus three standing rules:

- **Bookends premium.** Planner and reviewer are *low-volume, high-leverage
  reasoning* — one good plan or one sharp review pays for the premium model
  (`fable` on both). Dev and cartographer are the volume roles; telemetry
  currently favors the `gpt-5.6` line there (`sol` writing code, `terra`
  mapping), which also keeps the bulk volume on the flat-billed ChatGPT plan.
- **Reviewer runs a different lineage than the code it reviews**, so it isn't
  blind to the same mistakes the authoring model made — adversarial
  *diversity*, not a second opinion from the same brain. With devs on GPT and
  review on Claude, the current fit preserves this.
- **Three lineages, always.** Every flavor offers Codex (OpenAI), Claude
  (Anthropic), and OpenCode (open-weights via Ollama Cloud) — pick any provider for
  any role at launch. The OpenCode column is constrained to **MIT- or
  Apache-licensed** weights only (e.g. DeepSeek V4, GLM-5.2, Qwen3-Coder, gpt-oss);
  Modified-MIT / unresolved-license models (Kimi, MiniMax) are excluded even when
  available on the provider.
- **Admin decisions carry real risk** (a wrong rollback is data loss), so the
  one shell that maintains `main` (see [*Shells & worktrees*](shells-and-worktrees.md))
  defaults premium — currently `opus` on Claude.

> [!class2]
> **Vibe and Kimi Code sit outside this matrix.** Neither takes a model from the launch seam. Vibe selects its own via `active_model` in `~/.vibe/config.toml` (`vibe --setup`) or `VIBE_ACTIVE_MODEL`, and takes no headless boot. Kimi Code selects via `default_model` in `~/.kimi-code/config.toml` (its `-m` wants a user-local alias, not a portable model id) — it *does* boot headless (`kimi -p`), on that configured default (`./sc run` covers claude · codex · opencode · kimi).

### The sprint interview — models per role, per sprint

`flavor_defaults` + the picker cover interactive boots. Sprints boot workers
**headlessly** (`./sc run` — no picker), so the model seam moves to the sprint
declaration: the planner asks the operator exactly **two questions** — which
harness and model for **devs** (one answer, every dev runs it), and which for
**reviewers** (one answer, every reviewer runs it). The answers land in the
sprint doc's header —

```
models: devs=<harness>/<model> · reviewers=<harness>/<model>
```

— and parameterize every `./sc run` the planner issues for that sprint. No
answer → `flavor_defaults`, unchanged. One answer per flavor is deliberate:
shells of a flavor are interchangeable workers, and reviewers stay a
*different lineage* from the code they gate — the doctrine above, chosen per
sprint instead of per boot.

The planner itself is not interviewed — it is already booted. **Strong
recommendation, not a gate: run the planner on Claude.** The planner is the
low-volume, high-leverage reasoning seat, the one long-lived context in the
loop, and the only role the inbox watcher (`./sc watch inbox`, claude-only)
fully serves. Any harness *works* in the planner seat — wake latency and
ergonomics degrade, correctness doesn't.
