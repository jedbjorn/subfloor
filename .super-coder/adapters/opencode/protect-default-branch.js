// super-coder branch guard — OpenCode plugin (tool.execute.before).
//
// Blocks write/edit/patch while HEAD is a protected default branch, forcing a
// feature branch before work lands. Throwing in tool.execute.before aborts that
// single tool call and surfaces the message to the model (it does not crash the
// session). Registered via opencode.json `plugin` (path relative to repo root).
//
// One branch-decision source: this shells out to the shared branch-guard.sh that
// claude (PreToolUse), codex (.codex/hooks.json) and the git pre-commit hook all
// use — so SC_PROTECTED_BRANCHES and the message stay identical across harnesses.
const WRITE_TOOLS = new Set(["write", "edit", "patch"]);

export const ProtectDefaultBranch = async ({ $, worktree }) => ({
  "tool.execute.before": async (input) => {
    if (!WRITE_TOOLS.has(input.tool)) return;
    // Resolve the guard via $SC_ENGINE_DIR (absolute path to the installed
    // engine, exported by run.py). A fork gitignores .super-coder/, so it is
    // absent from the worktree — a worktree-relative path found nothing. Fall
    // back to the worktree-relative path for a non-engine launch.
    const guard = process.env.SC_ENGINE_DIR
      ? `${process.env.SC_ENGINE_DIR}/scripts/branch-guard.sh`
      : `${worktree}/.super-coder/scripts/branch-guard.sh`;
    // Bun shell: run the guard at the worktree root; .nothrow() so we read the
    // exit code, .quiet() so its output doesn't leak into the TUI.
    const res = await $`bash ${guard}`
      .cwd(worktree)
      .quiet()
      .nothrow();
    if (res.exitCode !== 0) {
      const msg = (res.stderr?.toString() || "").trim();
      throw new Error(
        msg || "Blocked: HEAD is on a protected default branch — create a feature branch first.",
      );
    }
  },
});
