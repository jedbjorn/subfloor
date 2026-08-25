#!/usr/bin/env node

import assert from "node:assert/strict";
import { createHash, createPublicKey, verify } from "node:crypto";
import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const FIELDS = [
  "binding_generation",
  "contract",
  "domain_id",
  "expires_unix_ms",
  "issued_unix_ms",
  "job_handle",
  "process_id",
];

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

function validateDescriptor(vector, fixture, facts) {
  if (!facts.readable) throw new Error("descriptor unreadable");
  if (facts.handleType !== "pipe") throw new Error("descriptor is not a pipe");
  if (facts.descriptorWritable) throw new Error("descriptor handle is writable");
  if (!facts.jobMember) throw new Error("process is not in expected Job");
  if (facts.breakaway) throw new Error("Job permits breakaway");

  const payload = decodeBase64(vector.payload_base64);
  const signature = decodeBase64(vector.signature_base64);
  const publicKey = createPublicKey({
    key: decodeBase64(fixture.public_key_spki_base64),
    format: "der",
    type: "spki",
  });
  if (!verify("RSA-SHA256", payload, publicKey, signature)) {
    throw new Error("descriptor signature mismatch");
  }

  const text = new TextDecoder("utf-8", { fatal: true }).decode(payload);
  const parsed = JSON.parse(text);
  assert.deepEqual(Object.keys(parsed), FIELDS, "descriptor schema");
  assert.equal(JSON.stringify(parsed), text, "descriptor canonical JSON");
  assert.equal(parsed.contract, fixture.contract);
  assert.match(parsed.domain_id, /^[a-f0-9]{32}$/);
  assert.equal(parsed.domain_id, fixture.expected_domain_id);
  assert.equal(parsed.job_handle, fixture.expected_job_handle);
  assert.equal(parsed.process_id, fixture.expected_process_id);
  assert.equal(Number.isSafeInteger(parsed.binding_generation), true);
  assert.equal(parsed.binding_generation > 0, true);
  assert.equal(Number.isSafeInteger(parsed.issued_unix_ms), true);
  assert.equal(Number.isSafeInteger(parsed.expires_unix_ms), true);
  assert.equal(parsed.issued_unix_ms <= fixture.now_unix_ms, true);
  assert.equal(fixture.now_unix_ms < parsed.expires_unix_ms, true);
  assert.equal(parsed.expires_unix_ms - parsed.issued_unix_ms <= 30000, true);
  return parsed;
}

export async function runWindowsDescriptorVectors(vectorPath, powershellPath) {
  const vectorSource = await readFile(vectorPath);
  const fixture = JSON.parse(vectorSource);
  const powershellSource = await readFile(powershellPath, "utf8");
  const publicKeyMatch = powershellSource.match(/\$TrustedPublicKey = "([^"]+)"/);
  assert.equal(publicKeyMatch?.[1], fixture.public_key_spki_base64);
  assert.match(powershellSource, /NtQueryObject/);
  assert.match(powershellSource, /FILE_TYPE_PIPE/);
  assert.match(powershellSource, /FORBIDDEN_WRITE_ACCESS/);
  assert.match(powershellSource, /ImportSubjectPublicKeyInfo/);
  assert.match(powershellSource, /VerifyData/);
  assert.match(powershellSource, /expires_unix_ms/);
  assert.match(powershellSource, /binding_generation/);

  const validFacts = {
    readable: true,
    handleType: "pipe",
    descriptorWritable: false,
    jobMember: true,
    breakaway: false,
  };
  const cases = [
    ["valid", "valid", validFacts, true],
    ["arbitrary-file", "valid", { ...validFacts, handleType: "disk" }, false],
    ["mutable-pipe", "valid", { ...validFacts, descriptorWritable: true }, false],
    ["unreadable", "valid", { ...validFacts, readable: false }, false],
    ["wrong-job", "wrong_job", validFacts, false],
    ["wrong-process", "wrong_process", validFacts, false],
    ["stale", "stale", validFacts, false],
    ["extra-field", "extra_field", validFacts, false],
    ["zero-generation", "zero_generation", validFacts, false],
    ["breakaway", "valid", { ...validFacts, breakaway: true }, false],
    ["not-job-member", "valid", { ...validFacts, jobMember: false }, false],
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
    try {
      validateDescriptor(
        { ...fixture.vectors[vectorName], ...mutation },
        fixture,
        facts,
      );
      actual = true;
    } catch {
      actual = false;
    }
    assert.equal(actual, expected, label);
    (actual ? accepted : refused).push(label);
  }
  return {
    contract: fixture.contract,
    accepted,
    refused,
    publicKeySha256: sha256(decodeBase64(fixture.public_key_spki_base64)),
    powershellSha256: sha256(powershellSource),
    vectorsSha256: sha256(vectorSource),
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  if (!process.argv[2] || !process.argv[3]) {
    throw new Error("usage: deepseek_dsh_windows_descriptor_probe.mjs <vectors> <powershell>");
  }
  process.stdout.write(`${JSON.stringify(
    await runWindowsDescriptorVectors(process.argv[2], process.argv[3]),
    null,
    2,
  )}\n`);
}
