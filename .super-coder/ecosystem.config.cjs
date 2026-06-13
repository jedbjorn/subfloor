// pm2 process definition for super-coder's review layer.
//
// One stdlib server (api/server.py) serves the JSON API + the static UI on this
// fork's derived port (scripts/ports.py → .super-coder/instance.json). The pm2
// process name is unique PER FORK ("sc-<repo>") so several forks — and the host
// repo's own pm2 apps — coexist in the one shared pm2 daemon without clashing.
//
// `make up` runs `ports.py ensure` first, so instance.json exists before this
// file is read.

const path = require("path");
const { execSync } = require("child_process");

const ENGINE = __dirname;                       // .super-coder/
const REPO_ROOT = path.join(ENGINE, "..");
const py = process.env.SC_PYTHON || "python3";

function ports() {
  try {
    return JSON.parse(execSync(`${py} ${path.join(ENGINE, "scripts/ports.py")} show`).toString());
  } catch {
    return { repo: path.basename(REPO_ROOT), port: 8800 };
  }
}
const cfg = ports();

module.exports = {
  apps: [
    {
      name: `sc-${cfg.repo}`,
      script: path.join(ENGINE, "api/server.py"),
      interpreter: py,
      args: `--port ${cfg.port}`,
      cwd: REPO_ROOT,
      autorestart: true,
      max_restarts: 5,
      env: { PYTHONUNBUFFERED: "1" },
    },
    {
      // Hourly repo remap — keeps the map (.sc-state/map.db) live between the
      // git-hook remaps (which only fire on pull / checkout / rebase). The belt
      // to the hooks' suspenders: it catches uncommitted local restructuring the
      // hooks can't see. One-shot per tick — autorestart:false so pm2 runs
      // map_repo, lets it exit, and cron_restart relaunches it on schedule. The
      // cartographer owns the map; this keeps it fresh unattended while the stack
      // is up (the hooks still cover forks that don't run pm2).
      name: `sc-map-${cfg.repo}`,
      script: path.join(ENGINE, "scripts/map_repo.py"),
      interpreter: py,
      cwd: REPO_ROOT,
      autorestart: false,
      cron_restart: "7 * * * *",
      env: { PYTHONUNBUFFERED: "1" },
    },
  ],
};
