#!/usr/bin/env node

import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";

const pluginPath = process.argv[2];
const config = JSON.parse(process.argv[3] ?? "null");
const hostBootGeneration = process.argv[4];
if (!pluginPath || !config || !hostBootGeneration) {
  throw new Error("usage: deepseek_dsh_identity_plugin_probe.mjs <plugin> <config-json> <host-boot-generation>");
}
process.env.SC_DSH_HOST_BOOT_GENERATION = hostBootGeneration;

const plugin = await import(pathToFileURL(pluginPath));
let contributor;
const cleanup = [];
const ctx = {
  shellEnv: {
    register(value) {
      contributor = value;
      return () => { contributor = undefined; };
    },
  },
  effect(factory) {
    const disposer = factory();
    if (typeof disposer === "function") cleanup.push(disposer);
    return disposer;
  },
};
plugin.apply(ctx, config);
process.stdout.write(`${JSON.stringify({ ready: true })}\n`);

const input = createInterface({ input: process.stdin, terminal: false });
for await (const line of input) {
  const request = JSON.parse(line);
  if (request.dispose === true) {
    for (const dispose of cleanup.reverse()) await dispose();
    process.stdout.write(`${JSON.stringify({ disposed: true })}\n`);
    process.exit(0);
  }
  try {
    const aliases = contributor.resolve({
      agent: { session: { header: { id: request.session_id } } },
    });
    process.stdout.write(`${JSON.stringify({ aliases })}\n`);
  } catch (error) {
    process.stdout.write(`${JSON.stringify({ error: String(error?.message ?? error) })}\n`);
  }
}
