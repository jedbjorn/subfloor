# Tool-Calling Discipline (opencode)

Keep tool calls well-formed. These rules matter most for models that emit tool
calls as text (Qwen, Hermes-style): one malformed call otherwise compounds into
repeated failures as the model copies the harness's own error format back.

- Call tools **only** through the harness's native tool mechanism. Never write a
  tool call as prose or markup in message text — no `<function=...>`, no
  `<parameter=...>`, no fenced tool blocks.
- There is **no tool named `invalid`, `unknown`, or `function`**. They are not
  real tools; never call them. The only callable tools are the ones the harness
  advertises (bash, edit, glob, grep, read, write, task, todowrite, webfetch,
  skill, question, …).
- If a tool result reports an invalid call or an unavailable tool, do **not**
  echo that error shape as a new call. Treat it as a signal to reissue the
  *intended* action with a real tool name and correct arguments.
- One logical action per tool call. Prefer a single well-formed call over
  several speculative ones.
