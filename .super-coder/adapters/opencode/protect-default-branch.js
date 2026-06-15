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
  // opencode puts the tool ARGS in the second param (output.args), not input —
  // input carries only {tool, sessionID, callID}.
  "tool.execute.before": async (input, output) => {
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
    // Pass the edited file as $1 so the guard checks the TARGET file's branch —
    // blocking an edit aimed at the stale main root (or any protected-branch
    // checkout), not just one whose cwd is on a protected branch. write/edit/
    // patch carry the path under one of these keys; if none is a string we pass
    // nothing and the guard falls back to its cwd-branch check.
    const a = (output && output.args) || {};
    const target = [a.filePath, a.file_path, a.path, a.filename].find(
      (v) => typeof v === "string" && v.length > 0,
    );
    // Bun shell: run the guard at the worktree root; .nothrow() so we read the
    // exit code, .quiet() so its output doesn't leak into the TUI.
    const res = await (target
      ? $`bash ${guard} ${target}`.cwd(worktree).quiet().nothrow()
      : $`bash ${guard}`.cwd(worktree).quiet().nothrow());
    if (res.exitCode !== 0) {
      const msg = (res.stderr?.toString() || "").trim();
      throw new Error(
        msg || "Blocked: HEAD is on a protected default branch — create a feature branch first.",
      );
    }
  },
});
