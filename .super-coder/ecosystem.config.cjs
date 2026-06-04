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
  ],
};
