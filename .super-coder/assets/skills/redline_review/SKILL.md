---
name: redline_review
description: Review PNG redlines from the shared scratch dir — find by filename match, describe what is seen, interpret intent, propose implementation, hold for approval before any code. Fires when the FnB says "redlines".
category: craft
common: false
---

# redline_review — read a redline before you build it

Redline = a marked-up screenshot the FnB drops in `<repo>/shared/redlines/` to
communicate a change visually. Turn the image into an approved plan BEFORE any
code.

Trigger: the FnB says "redlines" (with or without specific context).

## Steps

1. **Find the image**
   - List `shared/redlines/`. Dir missing (fork installed before the engine
     created it) -> `mkdir -p <repo>/shared/redlines` + check `shared/` root —
     earlier drops land there.
   - Match a filename to the prompt context (fuzzy/keyword). One file + no
     strong mismatch -> use it. Multiple -> best filename match; genuinely
     ambiguous -> surface the candidates, do not guess.

2. **Read the image** — Read tool, load the PNG visually.

3. **Report in three parts — skip none:**
   - **What I see:** literal description — layout, labels, UI elements,
     annotations, the markup itself.
   - **What I understand:** interpreted intent — the change or requirement the
     redline is communicating.
   - **What I propose:** concrete implementation plan — files, components,
     approach.

4. **Hold** — write/execute NO code until the FnB explicitly approves the
   proposal.

5. **After resolution** — FnB confirms the redline resolved -> delete the
   source `.png` from `shared/redlines/`. Delete only on explicit
   confirmation, never on assumed completion.
