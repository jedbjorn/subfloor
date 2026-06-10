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
    // Bun shell: run the guard at the worktree root; .nothrow() so we read the
    // exit code, .quiet() so its output doesn't leak into the TUI.
    const res = await $`bash ${worktree}/.super-coder/scripts/branch-guard.sh`
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
