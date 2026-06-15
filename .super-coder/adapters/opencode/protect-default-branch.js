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
    // Resolve the installed engine the same env-independent way `sc` does: a
    // fork gitignores .super-coder/, so it is absent from the worktree — walk to
    // the MAIN worktree root via git's common dir (its parent owns the engine).
    // $SC_ENGINE_DIR (exported by run.py) is an optional fast-path override; we
    // do NOT depend on it, so a manual `opencode` launch still guards.
    let eng = process.env.SC_ENGINE_DIR;
    if (!eng) {
      const r = await $`bash -c 'cd "$(git rev-parse --git-common-dir 2>/dev/null)/.." 2>/dev/null && pwd'`
        .cwd(worktree).quiet().nothrow();
      const root = (r.stdout?.toString() || "").trim();
      eng = root ? `${root}/.super-coder` : `${worktree}/.super-coder`;
    }
    const guard = `${eng}/scripts/branch-guard.sh`;
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
