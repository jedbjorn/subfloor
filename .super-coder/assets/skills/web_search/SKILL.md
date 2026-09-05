---
name: web_search
description: Search the web through the engine (`sc search`, Tavily). Use when a task needs current facts, docs, release notes, or error text you cannot find in the repo or your own knowledge. The key lives on the host; you only need your shell token.
category: substrate
command: sc search
common: true
---

# web_search — look it up through the engine

`sc search` is the one web search verb every shell has, on every harness.
It posts your query to the engine API with your own bearer token; the host
calls Tavily with the instance's API key and returns the results. The key never
reaches a shell, and a sandboxed shell needs no network egress.

## When to search

- A fact that changes: a library's current API, a release note, a CLI flag, a
  version's known bug, an error message you cannot explain from the code.
- Before guessing at an external service's protocol or a package's behaviour.
- Never for anything the repo catalogue or your own memory already answers —
  map first, memory second, search third.

## The verb

```bash
sc search "<query>"                      # 5 results + a short synthesized answer
sc search "<query>" --max 10             # 1..20 results
sc search "<query>" --depth advanced     # deeper crawl, slower
sc search "<query>" --json               # raw payload: answer, results[] (title, url, snippet, score)
```

Output is a numbered list: title, URL, snippet. Treat snippets as leads, not
proof — open the URL that matters (`curl -sL <url>` or your harness's fetch
tool) before you rely on it, and cite the URL in what you write.

## When it fails

Every failure names its cause and carries no secret:

| Message starts with | Meaning | Do |
|---|---|---|
| `web search is not configured` | no key on this instance | Tell the FnB: set it in the GUI → **Scripts → Web Search**. Do not work around it with a key of your own. |
| `Tavily rejected the API key` | the stored key is invalid or revoked | Tell the FnB to rotate it in the same GUI card. |
| `Tavily plan usage limit` / `rate limit` | quota exhausted | Stop searching; say so; continue from what you have. |
| `Tavily unreachable` | host network failure | Retry once later; then surface it. |
| `the engine API is required` | your shell is not API-wired | Boot via the launcher with the server up. |

Search results are not persisted anywhere by the engine. What you learn goes
where any other finding goes: the narrative, a decision, or the work itself.
