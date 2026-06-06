---
rendered_by: super-coder
source: db
edit: changes here are overwritten — author via the shell or localhost GUI
---

# redline_review

Review PNG redlines from the shared scratch dir — find the image by filename match, describe what is seen, interpret intent, propose an implementation, then hold for approval before writing code. Use when the FnB says "redlines".

**Category:** craft

---

# redline_review — read a redline before you build it

A redline is a marked-up screenshot the FnB drops in the repo's shared scratch
dir (`<repo>/shared/redlines/`) to communicate a change visually. This skill is
the discipline for turning that image into an approved plan **before** any code.

**Trigger:** the FnB says "redlines" (with or without specific context).

## Steps

1. **Find the image**
   - List `shared/redlines/`.
   - Match a filename to the prompt context (fuzzy / keyword).
   - One file present and no strong mismatch → use it. Multiple → pick the best
     filename match; if it's genuinely ambiguous, surface that rather than guess.

2. **Read the image** — use the Read tool to load the PNG visually.

3. **Report in three parts — skip none:**
   - **What I see:** literal description — layout, labels, UI elements,
     annotations, the markup itself.
   - **What I understand:** the interpreted intent — the change or requirement
     this redline is communicating.
   - **What I propose:** a concrete implementation plan — files, components,
     approach.

4. **Hold** — do not write or execute any code until the FnB explicitly approves
   the proposal.

5. **After resolution is confirmed** — once the FnB confirms the redline is
   resolved, delete the source `.png` from `shared/redlines/`. Delete only on
   explicit confirmation, never on assumed completion.
