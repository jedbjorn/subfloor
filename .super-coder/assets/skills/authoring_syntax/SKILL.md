---
name: authoring_syntax
description: House syntax for text authored for AI consumption — skill bodies, shell focus/mandate text, agent prompts. Directives with success conditions, specific prohibitions, light operators, need-to-know. Fires when authoring or editing one, or on "syntax check X".
category: craft
common: false
---

# authoring_syntax — write for the shell that loads it

House syntax for text authored for AI consumption — skill bodies, shell
focus/mandate text, agent prompts. Governs how the text reads; lifecycle
mechanics (create / seed / grant / persist) stay in `local_skill_management`.

Retrofit = touch-time: an existing doc converts when next edited. No mass
pass. Engine skill bodies are upstream-owned — a syntax fix to one is an
upstream PR (`issue_reporting` boundary), never a fork edit.

## Fires when

- authoring / editing any AI-consumed doc
- "syntax check X" -> run Self-test against X, return findings ranked, no
  rewrite

## Rules

- directive + success condition. every instruction = action + observable
  pass. "bounce the api -> `make health` returns ok" not "make sure the api
  is running".
- prohibitions are specific. bar the exact action: "NEVER `echo $SECRET`"
  not "be careful with secrets". no compliance test = cut or sharpen.
- operators, light: `->` then / leads-to. `=` is / defined-as. `/` or. `+`
  and. use only where compression loses nothing; else prose.
- need-to-know. cut the why unless it changes the action taken. NEVER name a
  tool / path / failure mode the reader would not otherwise encounter.
- CAPS = emphasis. rare — caps on every rule = caps on none.
- imperative voice. "validate input" not "you should validate input".
- format: prose = reasoning. tables / short bullets = structured data.
  `path:line` = code.
- one source of truth. state a constraint once, where it's used; restate
  only adjacent to a distant use.

## Self-test

- per line: could the reader comply and still fail? -> add the success
  condition.
- per line: delete it — does behavior change? no -> stays deleted.
- per doc: anything explaining rather than directing? -> cut or convert.

This skill obeys itself. Every edit to it passes the Self-test before write.
