import net from 'node:net';

export const MAX_FRAME_BYTES = 256 * 1024;
export const CONTRACT = 'f89-native-runtime-v1';

export function assetCall(payload, { endpoint = process.env.SC_F89_CONTROLLER_ENDPOINT,
  generation = process.env.SC_F89_GENERATION_ID, timeout = 1 } = {}) {
  timeout = Math.min(Math.max(timeout, 0.01), 5);
  if (!endpoint || !generation) return Promise.reject(new Error('controller context missing'));
  const frame = Buffer.from(JSON.stringify({ generation, contract: CONTRACT, op: 'asset', payload, timeout }) + '\n');
  if (frame.length > MAX_FRAME_BYTES) return Promise.reject(new Error('asset frame exceeds bound'));
  return new Promise((resolve, reject) => {
    const client = net.createConnection({ path: endpoint });
    let received = Buffer.alloc(0), settled = false;
    const finish = (error, result) => {
      if (settled) return;
      settled = true;
      client.destroy();
      if (error) reject(error); else resolve(result);
    };
    client.setTimeout(timeout * 1000, () => finish(new Error('asset deadline')));
    client.on('error', () => finish(new Error('asset transport unavailable')));
    client.on('connect', () => client.write(frame));
    client.on('data', chunk => {
      received = Buffer.concat([received, chunk]);
      if (received.length > MAX_FRAME_BYTES) return finish(new Error('asset response exceeds bound'));
      const newline = received.indexOf(10);
      if (newline < 0) return;
      try {
        const response = JSON.parse(received.subarray(0, newline).toString('utf8'));
        if (response.ok !== true || !response.result || typeof response.result !== 'object') {
          return finish(new Error('controller rejected asset'));
        }
        finish(null, response.result);
      } catch { finish(new Error('invalid asset response')); }
    });
    client.on('end', () => finish(new Error('incomplete asset response')));
  });
}
