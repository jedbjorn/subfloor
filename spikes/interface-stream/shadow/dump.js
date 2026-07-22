'use strict';
// Test helper: replay a byte stream into a FRESH @xterm/headless terminal
// and print the semantic grid dump (JSON) on stdout.
// usage: node dump.js <cols> <rows> <file-containing-escape-stream>

const fs = require('fs');
const { Terminal } = require('@xterm/headless');
const { trackModes, dumpGrid } = require('./grid');

const [, , cols, rows, file] = process.argv;
const term = new Terminal({ cols: Number(cols), rows: Number(rows), allowProposedApi: true, scrollback: 0 });
const st = trackModes(term);
const data = fs.readFileSync(file, 'utf8');
term.write(data, () => {
  process.stdout.write(JSON.stringify(dumpGrid(term, st)) + '\n');
});
