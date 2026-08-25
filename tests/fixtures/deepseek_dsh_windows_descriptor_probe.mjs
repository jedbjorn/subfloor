#!/usr/bin/env node

import assert from "node:assert/strict";
import { createHash, createPublicKey, verify } from "node:crypto";
import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function decodeBase64(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9+/]+={0,2}$/.test(value)) {
    throw new Error("invalid base64");
  }
  const decoded = Buffer.from(value, "base64");
  if (decoded.toString("base64") !== value) throw new Error("non-canonical base64");
  return decoded;
}

function resolve(reference, state) {
  if (typeof reference !== "string") return reference;
  if (Object.hasOwn(state, reference)) return state[reference];
  if (!reference.includes(".")) return reference;
  let current = state;
  for (const part of reference.split(".")) current = current?.[part];
  return current;
}

function rulePasses(rule, state) {
  const left = resolve(rule.left, state);
  const right = resolve(rule.right, state);
  switch (rule.operator) {
    case "equals": return left === right;
    case "matches": return typeof left === "string" && new RegExp(right).test(left);
    case "exact_fields":
      return JSON.stringify(Object.keys(left).sort()) === JSON.stringify([...right].sort());
    case "canonical_json": return JSON.stringify(left) === right;
    case "integer_min": return Number.isSafeInteger(left) && left >= right;
    case "integer_lte": return Number.isSafeInteger(left) && left <= right;
    case "integer_gt": return Number.isSafeInteger(left) && left > right;
    case "integer_difference_lte":
      return Number.isSafeInteger(left) && Number.isSafeInteger(right)
        && left - right <= rule.limit;
    default: throw new Error(`unknown policy operator: ${rule.operator}`);
  }
}

function mapNativeFacts(policy, native) {
  const adapter = policy.native_adapter;
  const breakawayMask = adapter.job_object_limit_breakaway_ok
    | adapter.job_object_limit_silent_breakaway_ok;
  return {
    job_member: native.membership_query_succeeded && native.job_member,
    breakaway: (native.limit_flags & breakawayMask) !== 0,
    handle_type: native.file_type === adapter.file_type_pipe ? "pipe" : "other",
    readable: (native.granted_access & adapter.file_read_data) !== 0,
    descriptor_writable:
      (native.granted_access & adapter.forbidden_write_access) !== 0,
  };
}

function validateDescriptor(vector, fixture, facts, policy) {
  const payload = decodeBase64(vector.payload_base64);
  const signature = decodeBase64(vector.signature_base64);
  const publicKey = createPublicKey({
    key: decodeBase64(policy.public_key_spki_base64),
    format: "der",
    type: "spki",
  });
  const text = new TextDecoder("utf-8", { fatal: true }).decode(payload);
  const parsed = JSON.parse(text);
  const state = {
    policy,
    payload: parsed,
    facts: {
      ...facts,
      signature_valid: verify("RSA-SHA256", payload, publicKey, signature),
    },
    context: {
      payload_text: text,
      expected_domain_id: fixture.expected_domain_id,
      expected_job_handle: fixture.expected_job_handle,
      expected_process_id: fixture.expected_process_id,
      now_unix_ms: fixture.now_unix_ms,
    },
  };
  for (const rule of policy.rules) {
    if (!rulePasses(rule, state)) throw new Error(`policy refused: ${rule.id}`);
  }
  return parsed;
}

export async function runWindowsDescriptorVectors(
  vectorPath,
  policyPath,
  powershellPath,
  adapterPath,
) {
  const vectorSource = await readFile(vectorPath);
  const fixture = JSON.parse(vectorSource);
  const policySource = await readFile(policyPath);
  const policy = JSON.parse(policySource);
  const powershellSource = await readFile(powershellPath, "utf8");
  const adapterSource = await readFile(adapterPath, "utf8");
  if (policy.contract !== fixture.contract) throw new Error("vector policy contract mismatch");
  if (policy.public_key_spki_base64 !== fixture.public_key_spki_base64) {
    throw new Error("vector policy key mismatch");
  }
  const policyHash = sha256(policySource);
  for (const marker of [
    "NtQueryObject", "FILE_TYPE_PIPE", "FORBIDDEN_WRITE_ACCESS",
    "ImportSubjectPublicKeyInfo", "VerifyData", "foreach ($rule in $Policy.rules)",
    `\$PolicySha256 = "${policyHash}"`,
    `\$AdapterSha256 = "${sha256(adapterSource)}"`,
    "ConvertTo-ScDshNativeFacts @nativeArguments",
    "Read-ScDshFramedDescriptor @readArguments",
    '$rule.stage -eq "native" -and',
    '$rule.stage -eq "descriptor" -and',
    "-not (Test-PolicyRule $rule $state)",
    "expected_domain_id = $ExpectedDomainId",
    "signature_valid = $signatureValid",
  ]) {
    if (!powershellSource.includes(marker)) throw new Error(`PowerShell adapter missing ${marker}`);
  }
  assert.equal(
    powershellSource.split("-not (Test-PolicyRule $rule $state)").length - 1,
    2,
    "PowerShell must enforce both shared-policy stages",
  );
  for (const marker of [
    "$MembershipQuerySucceeded -and $JobMember",
    "$LimitFlags -band $breakawayMask",
    "$FileType -eq [UInt32]$native.file_type_pipe",
    "$GrantedAccess -band [UInt32]$native.file_read_data",
    "$GrantedAccess -band [UInt32]$native.forbidden_write_access",
    "PeekNamedPipe",
    "[ScDshPipeProbe]::AvailableBytes($Handle)",
    "$watch.ElapsedMilliseconds -ge $TimeoutMs",
    'throw [System.TimeoutException]::new("descriptor read timed out")',
  ]) {
    if (!adapterSource.includes(marker)) throw new Error(`PowerShell adapter missing ${marker}`);
  }

  const validNative = {
    membership_query_succeeded: true,
    job_member: true,
    limit_flags: 0,
    file_type: policy.native_adapter.file_type_pipe,
    granted_access: policy.native_adapter.file_read_data,
  };
  const cases = [
    ["valid", "valid", validNative, true],
    ["arbitrary-file", "valid", { ...validNative, file_type: 1 }, false],
    ["mutable-pipe", "valid", {
      ...validNative,
      granted_access: policy.native_adapter.file_read_data
        | policy.native_adapter.forbidden_write_access,
    }, false],
    ["unreadable", "valid", { ...validNative, granted_access: 0 }, false],
    ["wrong-job", "wrong_job", validNative, false],
    ["wrong-process", "wrong_process", validNative, false],
    ["stale", "stale", validNative, false],
    ["extra-field", "extra_field", validNative, false],
    ["zero-generation", "zero_generation", validNative, false],
    ["breakaway", "valid", {
      ...validNative,
      limit_flags: policy.native_adapter.job_object_limit_breakaway_ok,
    }, false],
    ["not-job-member", "valid", { ...validNative, job_member: false }, false],
    ["membership-api-failed", "valid", {
      ...validNative,
      membership_query_succeeded: false,
    }, false],
    [
      "bad-signature",
      "valid",
      validNative,
      false,
      { signature_base64: fixture.vectors.wrong_job.signature_base64 },
    ],
    [
      "malformed",
      "valid",
      validNative,
      false,
      { payload_base64: Buffer.from("{").toString("base64") },
    ],
  ];
  const accepted = [];
  const refused = [];
  for (const [label, vectorName, native, expected, mutation = {}] of cases) {
    let actual = false;
    let failure = null;
    try {
      validateDescriptor(
        { ...fixture.vectors[vectorName], ...mutation },
        fixture,
        mapNativeFacts(policy, native),
        policy,
      );
      actual = true;
    } catch (error) {
      actual = false;
      failure = error;
    }
    assert.equal(actual, expected, `${label}: ${failure?.message ?? "accepted"}`);
    (actual ? accepted : refused).push(label);
  }
  return {
    contract: fixture.contract,
    accepted,
    refused,
    publicKeySha256: sha256(decodeBase64(fixture.public_key_spki_base64)),
    policySha256: policyHash,
    nativeAdapterSha256: sha256(adapterSource),
    powershellSha256: sha256(powershellSource),
    vectorsSha256: sha256(vectorSource),
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  if (!process.argv[2] || !process.argv[3] || !process.argv[4] || !process.argv[5]) {
    throw new Error("usage: deepseek_dsh_windows_descriptor_probe.mjs <vectors> <policy> <powershell> <native-adapter>");
  }
  process.stdout.write(`${JSON.stringify(
    await runWindowsDescriptorVectors(
      process.argv[2], process.argv[3], process.argv[4], process.argv[5],
    ),
    null,
    2,
  )}\n`);
}
