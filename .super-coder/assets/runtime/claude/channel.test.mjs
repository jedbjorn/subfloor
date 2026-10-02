import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { assetCall, CONTRACT, MAX_FRAME_BYTES } from './asset-client.mjs';

async function controller(handler) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'sc-f89-channel-test-'));
  const endpoint = path.join(directory, 'owner.sock');
  const peers = new Set();
  const server = net.createServer(socket => {
    peers.add(socket);
    socket.on('close', () => peers.delete(socket));
    let input = '';
    socket.on('data', async chunk => {
      input += chunk.toString('utf8');
      const end = input.indexOf('\n');
      if (end < 0) return;
      const frame = JSON.parse(input.slice(0, end));
      input = '';
      const reply = await handler(frame);
      if (reply !== null) socket.end(JSON.stringify(reply) + '\n');
    });
  });
  await new Promise(resolve => server.listen(endpoint, resolve));
  await fs.chmod(endpoint, 0o600);
  return { endpoint, close: async () => {
    for (const peer of peers) peer.destroy();
    await new Promise(resolve => server.close(resolve));
    await fs.rm(directory, { recursive: true });
  } };
}

async function eventually(condition, timeout = 2000) {
  const deadline = Date.now() + timeout;
  while (!condition() && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(condition(), 'bounded observation did not arrive');
}

test('asset client uses frozen private frame and bounds refusals', async () => {
  const frames = [];
  const seat = await controller(frame => {
    frames.push(frame);
    return frame.payload.refuse ? { ok: false, error: 'REFUSED', detail: 'private detail' }
      : { ok: true, result: { accepted: true } };
  });
  try {
    const options = { endpoint: seat.endpoint, generation: 'generation', timeout: 0.2 };
    assert.deepEqual(await assetCall({ kind: 'hook' }, options), { accepted: true });
    assert.deepEqual(frames[0], { generation: 'generation', contract: CONTRACT, op: 'asset',
      payload: { kind: 'hook' }, timeout: 0.2 });
    await assert.rejects(assetCall({ refuse: true }, options), /controller rejected/);
    await assert.rejects(assetCall({ text: 'x'.repeat(MAX_FRAME_BYTES) }, options), /exceeds bound/);
    assert.equal(frames.length, 2);
  } finally { await seat.close(); }
});

test('asset client times out without raw output or fallback', async () => {
  const seat = await controller(() => null);
  try {
    await assert.rejects(assetCall({ kind: 'channel.pull' }, {
      endpoint: seat.endpoint, generation: 'generation', timeout: 0.03,
    }), /asset deadline/);
  } finally { await seat.close(); }
});

test('real pinned MCP stdio channel waits initialization, delivers once, and attributes reply', async () => {
  const calls = [], notifications = [];
  let dispatch = false;
  const seat = await controller(frame => {
    calls.push(frame);
    assert.equal(frame.generation, 'test-generation');
    assert.equal(frame.contract, CONTRACT);
    if (frame.payload.kind === 'channel.pull') return { ok: true, result: { notifications:
      dispatch && frame.payload.after < 1 ? [{ sequence: 1, content: 'finite user input',
        meta: { request_id: 'request', payload_digest: 'sha' } }] : [] } };
    return { ok: true, result: { accepted: true } };
  });
  const transport = new StdioClientTransport({ command: process.execPath,
    args: [fileURLToPath(new URL('./channel.mjs', import.meta.url))],
    env: { PATH: process.env.PATH, SC_F89_CONTROLLER_ENDPOINT: seat.endpoint,
      SC_F89_GENERATION_ID: 'test-generation' }, stderr: 'pipe' });
  const client = new Client({ name: 'source-test', version: '1' });
  client.fallbackNotificationHandler = notification => { notifications.push(notification); };
  let stderr = '';
  transport.stderr?.on('data', chunk => { stderr += chunk.toString(); });
  try {
    await client.connect(transport);
    await eventually(() => calls.some(frame => frame.payload.kind === 'channel.ready'));
    const listing = await client.listTools();
    assert.equal(listing.tools[0].name, 'reply');
    dispatch = true;
    await eventually(() => notifications.length === 1);
    assert.equal(notifications[0].method, 'notifications/claude/channel');
    assert.deepEqual(notifications[0].params, { content: 'finite user input',
      meta: { request_id: 'request', payload_digest: 'sha' } });
    await eventually(() => calls.some(frame => frame.payload.kind === 'channel.sent'));
    await client.callTool({ name: 'reply', arguments: { request_id: 'request', text: 'answer' } });
    assert.ok(calls.some(frame => frame.payload.kind === 'channel.reply' && frame.payload.request_id === 'request'));
    await new Promise(resolve => setTimeout(resolve, 250));
    assert.equal(notifications.length, 1);
    assert.equal(stderr, '');
  } finally {
    await client.close();
    await seat.close();
  }
});
