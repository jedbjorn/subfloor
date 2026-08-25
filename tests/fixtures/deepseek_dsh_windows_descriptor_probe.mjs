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

export async function runWindowsDescriptorVectors(vectorPath, policyPath, powershellPath) {
  const vectorSource = await readFile(vectorPath);
  const fixture = JSON.parse(vectorSource);
  const policySource = await readFile(policyPath);
  const policy = JSON.parse(policySource);
  const powershellSource = await readFile(powershellPath, "utf8");
  if (policy.contract !== fixture.contract) throw new Error("vector policy contract mismatch");
  if (policy.public_key_spki_base64 !== fixture.public_key_spki_base64) {
    throw new Error("vector policy key mismatch");
  }
  const policyHash = sha256(policySource);
  for (const marker of [
    "NtQueryObject", "FILE_TYPE_PIPE", "FORBIDDEN_WRITE_ACCESS",
    "ImportSubjectPublicKeyInfo", "VerifyData", "foreach ($rule in $Policy.rules)",
    `\$PolicySha256 = "${policyHash}"`,
    '$rule.stage -eq "native" -and',
    '$rule.stage -eq "descriptor" -and',
    "-not (Test-PolicyRule $rule $state)",
    "expected_domain_id = $ExpectedDomainId",
    "job_member = $inExpectedJob",
    "breakaway = $permitsBreakaway",
    "descriptor_writable = $descriptorWritable",
    "signature_valid = $signatureValid",
  ]) {
    if (!powershellSource.includes(marker)) throw new Error(`PowerShell adapter missing ${marker}`);
  }
  assert.equal(
    powershellSource.split("-not (Test-PolicyRule $rule $state)").length - 1,
    2,
    "PowerShell must enforce both shared-policy stages",
  );

  const validFacts = {
    readable: true,
    handle_type: "pipe",
    descriptor_writable: false,
    job_member: true,
    breakaway: false,
  };
  const cases = [
    ["valid", "valid", validFacts, true],
    ["arbitrary-file", "valid", { ...validFacts, handle_type: "disk" }, false],
    ["mutable-pipe", "valid", { ...validFacts, descriptor_writable: true }, false],
    ["unreadable", "valid", { ...validFacts, readable: false }, false],
    ["wrong-job", "wrong_job", validFacts, false],
    ["wrong-process", "wrong_process", validFacts, false],
    ["stale", "stale", validFacts, false],
    ["extra-field", "extra_field", validFacts, false],
    ["zero-generation", "zero_generation", validFacts, false],
    ["breakaway", "valid", { ...validFacts, breakaway: true }, false],
    ["not-job-member", "valid", { ...validFacts, job_member: false }, false],
    [
      "bad-signature",
      "valid",
      validFacts,
      false,
      { signature_base64: fixture.vectors.wrong_job.signature_base64 },
    ],
    [
      "malformed",
      "valid",
      validFacts,
      false,
      { payload_base64: Buffer.from("{").toString("base64") },
    ],
  ];
  const accepted = [];
  const refused = [];
  for (const [label, vectorName, facts, expected, mutation = {}] of cases) {
    let actual = false;
    let failure = null;
    try {
      validateDescriptor(
        { ...fixture.vectors[vectorName], ...mutation },
        fixture,
        facts,
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
    powershellSha256: sha256(powershellSource),
    vectorsSha256: sha256(vectorSource),
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  if (!process.argv[2] || !process.argv[3] || !process.argv[4]) {
    throw new Error("usage: deepseek_dsh_windows_descriptor_probe.mjs <vectors> <policy> <powershell>");
  }
  process.stdout.write(`${JSON.stringify(
    await runWindowsDescriptorVectors(process.argv[2], process.argv[3], process.argv[4]),
    null,
    2,
  )}\n`);
}
