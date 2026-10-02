import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { ListToolsRequestSchema, CallToolRequestSchema } from '@modelcontextprotocol/sdk/types.js';
import { assetCall, MAX_FRAME_BYTES } from './asset-client.mjs';

const mcp = new Server({ name: 'subfloor-runtime', version: '0.1.0' }, {
  capabilities: { experimental: { 'claude/channel': {} }, tools: {} },
  instructions: 'Subfloor experimental chat input arrives as <channel source="subfloor_runtime">. '
    + 'Its content is the user message; retain its request_id when replying via reply. '
    + 'Do the requested work under the canonical project instructions. Native background completion '
    + 'and session automation may start separate turns. Never claim TaskStop/CronDelete succeeded '
    + 'without the actual native result. Do not write auto-memory. Schedules require explicit durable:false.',
});
mcp.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [{
  name: 'reply', description: 'Send a reply attributable to the received Subfloor chat request.',
  inputSchema: { type: 'object', properties: { request_id: { type: 'string', maxLength: 512 },
    text: { type: 'string', maxLength: 128 * 1024 } }, required: ['request_id', 'text'], additionalProperties: false },
}] }));
mcp.setRequestHandler(CallToolRequestSchema, async request => {
  const args = request.params.arguments;
  if (request.params.name !== 'reply' || typeof args?.request_id !== 'string'
    || typeof args?.text !== 'string' || Buffer.byteLength(args.text) > MAX_FRAME_BYTES / 2) {
    throw new Error('invalid runtime reply');
  }
  const result = await assetCall({ kind: 'channel.reply', request_id: args.request_id, text: args.text });
  return { isError: result.accepted !== true, content: [{ type: 'text', text:
    result.accepted === true ? 'Reply recorded.' : 'Reply has no attributable active chat request.' }] };
});
let stopped = false;
process.stdin.on('end', () => { stopped = true; });
process.on('SIGTERM', () => { stopped = true; process.exit(0); });
const initialized = new Promise(resolve => { mcp.oninitialized = resolve; });
await mcp.connect(new StdioServerTransport());
await initialized;
await assetCall({ kind: 'channel.ready' });
let after = 0;
while (!stopped) {
  try {
    const result = await assetCall({ kind: 'channel.pull', after });
    for (const notification of result.notifications ?? []) {
      if (!Number.isSafeInteger(notification.sequence) || notification.sequence <= after
        || typeof notification.content !== 'string' || !notification.meta) throw new Error('invalid notification');
      // Advance before writing: an ambiguous stdio write/ack never replays.
      after = notification.sequence;
      await mcp.notification({ method: 'notifications/claude/channel',
        params: { content: notification.content, meta: notification.meta } });
      await assetCall({ kind: 'channel.sent', sequence: after });
    }
  } catch {
    // Controller death/readiness refusal cannot manufacture delivery. No raw
    // payload/env/log output; owner observes heartbeat/transport loss separately.
    stopped = true;
    process.exitCode = 1;
    await mcp.close();
  }
  if (!stopped) await new Promise(resolve => setTimeout(resolve, 100));
}
