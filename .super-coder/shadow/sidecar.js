'use strict';
// Shadow terminal sidecar. ONE Node process per service, multiplexing
// generations over JSON-lines stdio. Volatile memory only.
//
// stdin ops:  {id?, op, gen, ...}
//   create   {gen, rows, cols}
//   feed     {gen, data: base64}        (synchronous-ordered per gen)
//   resize   {gen, rows, cols}
//   snapshot {gen}  -> {id, ok, gen, redraw: base64}
//   dispose  {gen}
// Ops without id are fire-and-forget (no reply).

const readline = require('readline');
const { Terminal } = require('@xterm/headless');
const { trackModes, buildRedraw } = require('./grid');

const gens = new Map();   // gen -> {term, st}
const chains = new Map(); // gen -> Promise (per-gen op ordering)

function reply(obj) { process.stdout.write(JSON.stringify(obj) + '\n'); }

// Serialize ops per generation: fn runs after all prior ops for gen settle.
function run(gen, fn) {
  const prev = chains.get(gen) || Promise.resolve();
  const next = prev.then(fn).catch((e) => {
    process.stderr.write(`shadow op error gen=${gen}: ${e && e.stack || e}\n`);
  });
  chains.set(gen, next);
  return next;
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on('line', (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  const { id, op, gen } = msg;
  switch (op) {
    case 'create':
      run(gen, async () => {
        const old = gens.get(gen);
        if (old) old.term.dispose();
        const term = new Terminal({ cols: msg.cols, rows: msg.rows, allowProposedApi: true, scrollback: 0 });
        gens.set(gen, { term, st: trackModes(term) });
      }).then(() => { if (id) reply({ id, ok: true, gen }); });
      break;
    case 'feed': {
      const data = Buffer.from(msg.data, 'base64');
      run(gen, () => new Promise((resolve) => {
        const g = gens.get(gen);
        if (!g) return resolve();
        g.term.write(data, resolve);
      })).then(() => { if (id) reply({ id, ok: true, gen }); });
      break;
    }
    case 'resize':
      run(gen, async () => {
        const g = gens.get(gen);
        if (g) g.term.resize(msg.cols, msg.rows);
      }).then(() => { if (id) reply({ id, ok: true, gen }); });
      break;
    case 'snapshot':
      run(gen, async () => {
        const g = gens.get(gen);
        if (!g) throw new Error('no such generation');
        return Buffer.from(buildRedraw(g.term, g.st), 'utf8').toString('base64');
      }).then(
        (redraw) => reply({ id, ok: true, gen, redraw }),
        (e) => reply({ id, ok: false, gen, error: String(e) }),
      );
      break;
    case 'dispose':
      run(gen, async () => {
        const g = gens.get(gen);
        if (g) { g.term.dispose(); gens.delete(gen); }
      }).then(() => { if (id) reply({ id, ok: true, gen }); });
      break;
    default:
      if (id) reply({ id, ok: false, gen, error: 'unknown op' });
  }
});
