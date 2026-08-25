import { createHash, randomUUID } from "node:crypto";
import {
  closeSync,
  constants,
  fchmodSync,
  fstatSync,
  fsyncSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { dirname, isAbsolute, join } from "node:path";

export const name = "sc-shell-identity";
export const inject = ["shellEnv"];

const HEALTH_CONTRACT = "sc-dsh-plugin-health-v1";
const REGISTRY_CONTRACT = "sc-dsh-identity-registry-v1";
const ALIASES = Object.freeze({
  DSH_SC_SHELL_ID: "Canonical positive engine shell ID.",
  DSH_SC_SHELL_SHORTNAME: "Canonical shortname for the same engine shell.",
  DSH_SC_SHELL_WORKTREE: "Canonical worktree bound to the root DSH session.",
  DSH_SC_API_BASE: "Loopback-only engine API base.",
  DSH_SC_MEM_CREDENTIAL_FILE: "Unique owner-only credential artifact for this binding generation.",
  DSH_SC_BINDING_GENERATION: "Current root binding record generation.",
  DSH_SC_PLUGIN_HEALTH_GENERATION: "Live dedicated profile and plugin contract generation.",
});

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function ownerJson(path) {
  const beforePath = lstatSync(path);
  if (!beforePath.isFile() || beforePath.isSymbolicLink() || (beforePath.mode & 0o777) !== 0o600) {
    throw new Error("owner-only artifact is unsafe");
  }
  if (typeof process.getuid === "function" && beforePath.uid !== process.getuid()) {
    throw new Error("owner-only artifact has another owner");
  }
  const noFollow = constants.O_NOFOLLOW ?? 0;
  const descriptor = openSync(path, constants.O_RDONLY | noFollow);
  try {
    const before = fstatSync(descriptor);
    if (beforePath.dev !== before.dev || beforePath.ino !== before.ino) {
      throw new Error("owner-only artifact identity changed before reading");
    }
    const value = JSON.parse(readFileSync(descriptor, "utf8"));
    const after = fstatSync(descriptor);
    if (before.dev !== after.dev || before.ino !== after.ino || before.size !== after.size) {
      throw new Error("owner-only artifact changed while reading");
    }
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new Error("owner-only artifact is not an object");
    }
    return value;
  } finally {
    closeSync(descriptor);
  }
}

function atomicOwnerJson(path, value) {
  const parent = dirname(path);
  mkdirSync(parent, { recursive: true, mode: 0o700 });
  const temporary = join(parent, `.${path.split("/").at(-1)}.${randomUUID()}.tmp`);
  const descriptor = openSync(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
  try {
    fchmodSync(descriptor, 0o600);
    writeFileSync(descriptor, `${canonicalJson(value)}\n`, "utf8");
    fsyncSync(descriptor);
  } finally {
    closeSync(descriptor);
  }
  try {
    renameSync(temporary, path);
    const directory = openSync(parent, constants.O_RDONLY | (constants.O_DIRECTORY ?? 0));
    try {
      fsyncSync(directory);
    } finally {
      closeSync(directory);
    }
  } catch (error) {
    try { unlinkSync(temporary); } catch {}
    throw error;
  }
}

function requiredString(config, key) {
  const value = config?.[key];
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`sc-shell-identity: missing ${key}`);
  }
  return value;
}

function positiveInteger(value, field) {
  if (!Number.isSafeInteger(value) || value <= 0) throw new Error(`invalid ${field}`);
  return value;
}

function safeCredential(path) {
  if (typeof path !== "string" || !isAbsolute(path)) throw new Error("credential path is invalid");
  const metadata = lstatSync(path);
  if (!metadata.isFile() || metadata.isSymbolicLink() || (metadata.mode & 0o777) !== 0o600) {
    throw new Error("credential artifact is unsafe");
  }
  if (typeof process.getuid === "function" && metadata.uid !== process.getuid()) {
    throw new Error("credential artifact has another owner");
  }
  return path;
}

function linuxStartTicks(pid) {
  const raw = readFileSync(`/proc/${pid}/stat`, "utf8");
  const commandEnd = raw.lastIndexOf(")");
  if (commandEnd < 0) throw new Error("Host process identity is unavailable");
  const fields = raw.slice(commandEnd + 1).trim().split(/\s+/);
  const value = Number(fields[19]);
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new Error("Host process identity is invalid");
  }
  return value;
}

export function apply(ctx, config) {
  if (process.platform !== "linux") {
    throw new Error("sc-shell-identity: supported on Linux only");
  }
  const forkId = requiredString(config, "forkId");
  const profileId = requiredString(config, "profileId");
  const pluginBundleDigest = requiredString(config, "pluginBundleDigest");
  const declaredVariableSchemaDigest = requiredString(config, "declaredVariableSchemaDigest");
  const registryPath = requiredString(config, "registryPath");
  const registryPathIdentity = requiredString(config, "registryPathIdentity");
  const healthPath = requiredString(config, "healthPath");
  const hostBootGeneration = requiredString(process.env, "SC_DSH_HOST_BOOT_GENERATION");
  const hostPid = process.pid;
  const hostStartTicks = linuxStartTicks(hostPid);
  const pluginLoadHmrGeneration = randomUUID().replaceAll("-", "");
  const generationInputs = Object.freeze({
    canonical_fork_id: forkId,
    dedicated_profile_id: profileId,
    plugin_bundle_digest: pluginBundleDigest,
    declared_variable_schema_digest: declaredVariableSchemaDigest,
    canonical_registry_path_identity: registryPathIdentity,
    host_boot_generation: hostBootGeneration,
    plugin_load_hmr_generation: pluginLoadHmrGeneration,
  });
  const contractGeneration = sha256(canonicalJson(generationInputs));

  const publishHealth = ({ loaded = true, snapshot = null, record = null } = {}) => {
    atomicOwnerJson(healthPath, {
      contract: HEALTH_CONTRACT,
      loaded,
      fork_id: forkId,
      profile_id: profileId,
      registry_path: registryPath,
      host_boot_generation: hostBootGeneration,
      host_pid: hostPid,
      host_start_ticks: hostStartTicks,
      plugin_load_hmr_generation: pluginLoadHmrGeneration,
      plugin_contract_generation: contractGeneration,
      registry_snapshot_generation: snapshot?.snapshot_generation ?? null,
      binding_record_generation: record?.record_generation ?? null,
      observed_at: new Date().toISOString(),
    });
  };

  const resolve = (execution) => {
    const sessionId = execution?.agent?.session?.header?.id;
    if (typeof sessionId !== "string" || sessionId.length === 0) return {};
    const snapshot = ownerJson(registryPath);
    if (
      snapshot.contract !== REGISTRY_CONTRACT
      || snapshot.schema_version !== 1
      || snapshot.fork_id !== forkId
      || snapshot.profile_id !== profileId
      || snapshot.registry_path !== registryPath
      || !Number.isSafeInteger(snapshot.snapshot_generation)
      || snapshot.snapshot_generation < 0
      || !snapshot.records || typeof snapshot.records !== "object"
      || !snapshot.lineage || typeof snapshot.lineage !== "object"
    ) {
      throw new Error("sc-shell-identity: registry identity or schema mismatch");
    }
    const lineage = snapshot.lineage[sessionId];
    const rootSessionId = lineage?.root_session_id ?? sessionId;
    const record = snapshot.records[rootSessionId];
    if (!record || record.state !== "active" || record.root_session_id !== rootSessionId) return {};
    positiveInteger(record.record_generation, "binding record generation");
    positiveInteger(record.lifecycle_epoch, "lifecycle epoch");
    positiveInteger(record.shell_id, "shell ID");
    if (lineage && (
      lineage.lifecycle_epoch !== record.lifecycle_epoch
      || lineage.record_generation !== record.record_generation
    )) {
      throw new Error("sc-shell-identity: stale child lineage");
    }
    if (record.plugin_contract_generation !== contractGeneration) return {};
    if (typeof record.shell_shortname !== "string" || record.shell_shortname.length === 0) return {};
    if (typeof record.shell_worktree !== "string" || !isAbsolute(record.shell_worktree)) return {};
    if (typeof record.api_base !== "string" || !/^http:\/\/(?:127\.0\.0\.1|localhost)(?::\d+)?(?:\/.*)?$/.test(record.api_base)) return {};
    const credential = safeCredential(record.credential_file);
    publishHealth({ snapshot, record });
    return {
      DSH_SC_SHELL_ID: String(record.shell_id),
      DSH_SC_SHELL_SHORTNAME: record.shell_shortname,
      DSH_SC_SHELL_WORKTREE: record.shell_worktree,
      DSH_SC_API_BASE: record.api_base,
      DSH_SC_MEM_CREDENTIAL_FILE: credential,
      DSH_SC_BINDING_GENERATION: String(record.record_generation),
      DSH_SC_PLUGIN_HEALTH_GENERATION: contractGeneration,
    };
  };

  publishHealth();
  ctx.shellEnv.register({
    name: "super-coder",
    variables: Object.fromEntries(Object.entries(ALIASES).map(([key, description]) => [key, { description }])),
    resolve,
  });
  ctx.effect(() => () => publishHealth({ loaded: false }), "sc-shell-identity: health disposal");
}
