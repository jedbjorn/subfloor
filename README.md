# super-coder

A **forkable shell substrate for a single code repository.** You fork it into a
project repo; it brings the shell system — DB-backed identity, memory, seed/L&S,
decisions, flags, a roadmap, and spec/doc content — and runs that repo through
whatever coding harness you point at it (Claude Code today, OpenCode next).

The bet: **we build the data layer, we rent the harness.** The agent loop, the
tools, the model API are the harness's job. We own identity + memory + content
and render a boot artifact the harness reads natively.

This repo is also **dogfood**: super-coder maintains super-coder. Its own
`.super-coder/` engine manages the maintainer shell that builds it.

## Layout

```
.super-coder/        the engine (see .super-coder/README.md)
docs_sc/ specs_sc/   rendered, read-only (later phases)
skills_sc/ roadmap_sc.md
CLAUDE.md / AGENTS.md  boot artifact — gitignored, rebuilt at launch
```

## Quick start

```bash
make rebuild     # build .super-coder/shell_db.db from schema + migrations + snapshot
make launch      # username-only auth + pick a shell + render boot + exec harness
```

The live `.super-coder/shell_db.db` is **gitignored and rebuilt** from
git-tracked text. See `.super-coder/README.md` for the full model.

> Spec: the founding design lives in the roadmap (`super-coder` feature row).
> Build plan + log: tracked in superCC `shared/super-coder-impl-plan.md` during
> bring-up.
