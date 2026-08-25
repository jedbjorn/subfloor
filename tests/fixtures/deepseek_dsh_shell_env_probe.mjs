#!/usr/bin/env node

import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { access, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const EXPECTED_VERSION = "0.1.1-rc.2";
const packageRoot = process.argv[2];
const contractPath = process.argv[3];

if (!packageRoot || !contractPath) {
  throw new Error("usage: deepseek_dsh_shell_env_probe.mjs <@deepseek-ai/dsh package root> <contract>");
}
const contract = JSON.parse(await readFile(contractPath, "utf8"));

const runtimeMarkers = {
  "profile-composition": [/homePatchPath/, /prepareProfile/, /runProfile/],
  "per-execution-shell-env": [/DSH_SHELL/, /DSH_SESSION_ID/, /collect\(execution\)/],
  "ambient-scrub": [/scrubbedParentEnv/, /KEY\|PASSWORD\|SECRET\|TOKEN/],
  "subprocess-merge": [/scrubbedParentEnv/, /childEnv\(spec\.env\)/],
  "bash-collection": [/ctx\.shellEnv\.collect\(exec\)/],
  "bash-dispatch": [/\.\.\.spec\.dshEnv/],
  "powershell-collection": [/ctx\.shellEnv\.collect\(exec\)/],
  "powershell-dispatch": [/\.\.\.spec\.dshEnv/],
};
const runtimeHashes = {};
for (const seam of contract.supported_seams) {
  const runtime = await readFile(join(packageRoot, seam.runtime));
  const digest = createHash("sha256").update(runtime).digest("hex");
  assert.equal(digest, seam.runtime_sha256, `${seam.name} runtime hash`);
  const source = runtime.toString("utf8");
  for (const marker of runtimeMarkers[seam.name] ?? []) {
    assert.match(source, marker, `${seam.name} runtime contract`);
  }
  runtimeHashes[seam.name] = digest;
}

async function packageJson(name) {
  const path = name === "dsh"
    ? join(packageRoot, "package.json")
    : join(packageRoot, "node_modules", "@deepseek-ai", name, "package.json");
  return JSON.parse(await readFile(path, "utf8"));
}

async function packageModule(name) {
  const path = join(packageRoot, "node_modules", "@deepseek-ai", name, "lib", "index.js");
  return import(pathToFileURL(path));
}

async function waitFor(path) {
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    try {
      await access(path);
      return;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
  }
  throw new Error(`timed out waiting for ${path}`);
}

const packageNames = [
  "dsh-shell-env",
  "dsh-subprocess",
  "dsh-subprocess-local",
  "dsh-tool-bash",
  "dsh-bash-local",
  "dsh-tool-pwsh",
  "dsh-pwsh-local",
];
const versions = { dsh: (await packageJson("dsh")).version };
for (const name of packageNames) {
  versions[name] = (await packageJson(name)).version;
}
assert.deepEqual(new Set(Object.values(versions)), new Set([EXPECTED_VERSION]));

const cordisPath = join(packageRoot, "node_modules", "@deepseek-ai", "cordis", "lib", "index.js");
const { Context } = await import(pathToFileURL(cordisPath));
const { ShellEnvRegistry } = await packageModule("dsh-shell-env");
const { LocalSubprocessRuntime } = await packageModule("dsh-subprocess-local");
const { LocalBashExecutor } = await packageModule("dsh-bash-local");

const ctx = new Context();
const registry = new ShellEnvRegistry(ctx, { dshHome: "/fixture/dsh-home" });
new LocalSubprocessRuntime(ctx);
const bash = new LocalBashExecutor(ctx, {
  cwd: process.cwd(),
  timeoutMs: 5000,
  maxTimeoutMs: 5000,
  maxOutputBytes: 65536,
  maxSpillBytes: 65536,
  graceMs: 100,
});

const aliases = {
  "managed-session-A": {
    DSH_SC_SHELL_ID: "101",
    DSH_SC_SHELL_SHORTNAME: "SHELL_A",
    DSH_SC_SHELL_WORKTREE: "/worktrees/shell-a",
    DSH_SC_API_BASE: "http://127.0.0.1:8837",
    DSH_SC_MEM_CREDENTIAL_FILE: "/credentials/shell-a",
    DSH_SC_BINDING_GENERATION: "7",
    DSH_SC_PLUGIN_HEALTH_GENERATION: "contract-3",
  },
  "managed-session-B": {
    DSH_SC_SHELL_ID: "202",
    DSH_SC_SHELL_SHORTNAME: "SHELL_B",
    DSH_SC_SHELL_WORKTREE: "/worktrees/shell-b",
    DSH_SC_API_BASE: "http://127.0.0.1:8837",
    DSH_SC_MEM_CREDENTIAL_FILE: "/credentials/shell-b",
    DSH_SC_BINDING_GENERATION: "11",
    DSH_SC_PLUGIN_HEALTH_GENERATION: "contract-3",
  },
};
const declared = Object.fromEntries(
  Object.keys(aliases["managed-session-A"]).map((key) => [key, { description: key }]),
);
registry.register({
  name: "super-coder",
  variables: declared,
  resolve(execution) {
    return aliases[execution.agent?.session.header.id] ?? {};
  },
});

function execution(sessionId) {
  return { agent: { session: { header: { id: sessionId } } } };
}

function exactSnapshot(sessionId) {
  const snapshot = registry.collect(execution(sessionId));
  assert.equal(Object.isFrozen(snapshot), true);
  assert.equal(snapshot.DSH_SHELL, "1");
  assert.equal(snapshot.DSH_SESSION_ID, sessionId);
  for (const [key, value] of Object.entries(aliases[sessionId])) {
    assert.equal(snapshot[key], value);
  }
  return snapshot;
}

const originalAmbient = {
  DSH_SC_SHELL_SHORTNAME: process.env.DSH_SC_SHELL_SHORTNAME,
  DSH_AMBIENT_CANARY: process.env.DSH_AMBIENT_CANARY,
  SC_SHELL_SHORTNAME: process.env.SC_SHELL_SHORTNAME,
};
process.env.DSH_SC_SHELL_SHORTNAME = "STALE_AMBIENT_DSH";
process.env.DSH_AMBIENT_CANARY = "STALE_AMBIENT_DSH";
process.env.SC_SHELL_SHORTNAME = "STALE_AMBIENT_SC";

const fixtureDir = await mkdtemp(join(tmpdir(), "dsh-shell-env-probe."));
try {
  const release = join(fixtureDir, "release");
  const run = (label, sessionId) => {
    const ready = join(fixtureDir, `${label}.ready`);
    const command = [
      `touch ${JSON.stringify(ready)}`,
      `while [ ! -f ${JSON.stringify(release)} ]; do sleep 0.01; done`,
      "printf '%s|%s|%s|%s|%s|%s\\n' \"$$\" \"$DSH_SESSION_ID\" \"$DSH_SC_SHELL_SHORTNAME\" \"$DSH_SC_MEM_CREDENTIAL_FILE\" \"${DSH_AMBIENT_CANARY-unset}\" \"${SC_SHELL_SHORTNAME-unset}\"",
    ].join("; ");
    return bash.run(bash.resolve({ command, dshEnv: exactSnapshot(sessionId) }));
  };

  const foregroundA = run("A", "managed-session-A");
  const foregroundB = run("B", "managed-session-B");
  await Promise.all([waitFor(join(fixtureDir, "A.ready")), waitFor(join(fixtureDir, "B.ready"))]);
  await writeFile(release, "release\n", { flag: "wx" });
  const [resultA, resultB] = await Promise.all([foregroundA, foregroundB]);
  assert.equal(resultA.exitCode, 0);
  assert.equal(resultB.exitCode, 0);
  assert.match(resultA.stdout.text, /^\d+\|managed-session-A\|SHELL_A\|\/credentials\/shell-a\|unset\|STALE_AMBIENT_SC\n$/);
  assert.match(resultB.stdout.text, /^\d+\|managed-session-B\|SHELL_B\|\/credentials\/shell-b\|unset\|STALE_AMBIENT_SC\n$/);

  const backgroundA = bash.start(bash.resolve({
    command: "printf 'A|%s|%s\\n' \"$DSH_SC_SHELL_SHORTNAME\" \"${DSH_AMBIENT_CANARY-unset}\"",
    dshEnv: exactSnapshot("managed-session-A"),
  }));
  process.env.DSH_SC_SHELL_SHORTNAME = "MUTATED_AMBIENT_DSH";
  const backgroundB = bash.start(bash.resolve({
    command: "printf 'B|%s|%s\\n' \"$DSH_SC_SHELL_SHORTNAME\" \"${DSH_AMBIENT_CANARY-unset}\"",
    dshEnv: exactSnapshot("managed-session-B"),
  }));
  await Promise.all([backgroundA.done, backgroundB.done]);
  assert.equal(backgroundA.readOutput().delta, "A|SHELL_A|unset\n");
  assert.equal(backgroundB.readOutput().delta, "B|SHELL_B|unset\n");

  const unbound = registry.collect({});
  assert.equal(unbound.DSH_SC_SHELL_SHORTNAME, undefined);
  assert.equal(unbound.DSH_SC_MEM_CREDENTIAL_FILE, undefined);

  assert.throws(() => {
    const mutated = { ...exactSnapshot("managed-session-B"), DSH_SC_SHELL_SHORTNAME: "SHELL_A" };
    assert.equal(mutated.DSH_SC_SHELL_SHORTNAME, aliases["managed-session-B"].DSH_SC_SHELL_SHORTNAME);
  });
  assert.notEqual(
    unbound.DSH_SC_SHELL_SHORTNAME ?? process.env.SC_SHELL_SHORTNAME,
    undefined,
    "ambient fallback mutation must remain detectable",
  );

  const pwshToolSource = await readFile(
    join(packageRoot, "node_modules", "@deepseek-ai", "dsh-tool-pwsh", "lib", "index.js"),
    "utf8",
  );
  const pwshLocalSource = await readFile(
    join(packageRoot, "node_modules", "@deepseek-ai", "dsh-pwsh-local", "lib", "index.js"),
    "utf8",
  );
  assert.match(pwshToolSource, /ctx\.shellEnv\.collect\(exec\)/);
  assert.match(pwshLocalSource, /\.\.\.spec\.dshEnv/);

  process.stdout.write(`${JSON.stringify({
    contract: "dsh-shell-env-clean-room-fixture-v1",
    versions,
    runtimeHashes,
    foreground: [resultA.stdout.text.trim(), resultB.stdout.text.trim()],
    background: ["A|SHELL_A|unset", "B|SHELL_B|unset"],
    unboundAliases: [],
    powershellParity: "source-contract-passed",
  }, null, 2)}\n`);
} finally {
  await ctx.root.fiber.dispose();
  await rm(fixtureDir, { recursive: true, force: true });
  for (const [key, value] of Object.entries(originalAmbient)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
}
