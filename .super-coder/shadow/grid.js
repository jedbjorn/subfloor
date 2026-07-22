'use strict';
// Semantic grid extraction + escape-sequence redraw reconstruction for
// @xterm/headless 6.0.0. Shared by sidecar.js (broker shadow) and dump.js
// (test replay helper).
//
// Cell attribute getters in headless 6.0.0 return raw bitmasks; nonzero is
// truthy. Color modes: 0 default, 16777216 palette-16, 33554432 palette-256,
// 50331648 RGB.

const CM_RGB = 50331648;

// Modes the public `term.modes` API does not report are tracked via parser
// hooks: DECRST ?25 (cursor visibility) and ?1006 (SGR extended mouse).
function trackModes(term) {
  const st = { cursorVisible: true, sgrMouse: false };
  const onMode = (params, set) => {
    for (const p of params) {
      if (p === 25) st.cursorVisible = set;
      else if (p === 1006) st.sgrMouse = set;
    }
    return false; // fall through to default handlers
  };
  term.parser.registerCsiHandler({ prefix: '?', final: 'h' }, (p) => onMode(p, true));
  term.parser.registerCsiHandler({ prefix: '?', final: 'l' }, (p) => onMode(p, false));
  return st;
}

function colorKey(mode, value) {
  if (!mode) return 'def';
  if (mode === CM_RGB) return 'rgb:' + value;
  return 'pal:' + value;
}

// Semantic key for one cell; null for zero-width continuation cells.
function cellKey(cell) {
  if (!cell || cell.getWidth() === 0) return null;
  let flags = '';
  if (cell.isBold()) flags += 'b';
  if (cell.isDim()) flags += 'd';
  if (cell.isItalic()) flags += 'i';
  if (cell.isUnderline()) flags += 'u';
  if (cell.isBlink()) flags += 'l';
  if (cell.isInverse()) flags += 'v';
  if (cell.isInvisible()) flags += 'n';
  if (cell.isStrikethrough()) flags += 's';
  return colorKey(cell.getFgColorMode(), cell.getFgColor()) + '|' +
    colorKey(cell.getBgColorMode(), cell.getBgColor()) + '|' + flags;
}

const DEFAULT_KEY = 'def|def|';

// SGR sequence (from reset) reproducing a semantic key.
function sgrFor(key) {
  const [fg, bg, flags] = key.split('|');
  const p = ['0'];
  if (flags.includes('b')) p.push('1');
  if (flags.includes('d')) p.push('2');
  if (flags.includes('i')) p.push('3');
  if (flags.includes('u')) p.push('4');
  if (flags.includes('l')) p.push('5');
  if (flags.includes('v')) p.push('7');
  if (flags.includes('n')) p.push('8');
  if (flags.includes('s')) p.push('9');
  const emit = (spec, base8, baseBright, ext) => {
    if (spec === 'def') return;
    if (spec.startsWith('pal:')) {
      const n = Number(spec.slice(4));
      if (n < 8) p.push(String(base8 + n));
      else if (n < 16) p.push(String(baseBright + n - 8));
      else p.push(ext, '5', String(n));
    } else {
      const v = Number(spec.slice(4));
      p.push(ext, '2', String((v >> 16) & 255), String((v >> 8) & 255), String(v & 255));
    }
  };
  emit(fg, 30, 90, '38');
  emit(bg, 40, 100, '48');
  return '\x1b[' + p.join(';') + 'm';
}

function encodeRow(line) {
  // last cell that is not a default-attributed blank
  let last = -1;
  for (let x = 0; x < line.length; x++) {
    const cell = line.getCell(x);
    if (!cell || cell.getWidth() === 0) continue;
    const key = cellKey(cell);
    const chars = cell.getChars() || ' ';
    if (key !== DEFAULT_KEY || chars.trim() !== '') last = x;
  }
  if (last < 0) return '';
  let out = '', cur = null;
  for (let x = 0; x <= last; x++) {
    const cell = line.getCell(x);
    if (!cell || cell.getWidth() === 0) continue;
    const key = cellKey(cell);
    if (key !== cur) { out += sgrFor(key); cur = key; }
    out += cell.getChars() || ' ';
  }
  return out;
}

// Escape-sequence reconstruction a fresh xterm.js can write() to reproduce
// the terminal's current state: reset, non-default modes, alt-screen entry,
// full visible grid with SGR, cursor position + visibility.
function buildRedraw(term, st) {
  const b = term.buffer.active;
  let s = '\x1bc';
  const m = term.modes;
  if (m.applicationCursorKeysMode) s += '\x1b[?1h';
  if (m.applicationKeypadMode) s += '\x1b=';
  if (m.bracketedPasteMode) s += '\x1b[?2004h';
  if (m.sendFocusMode) s += '\x1b[?1004h';
  const mm = m.mouseTrackingMode;
  if (mm === 'x10') s += '\x1b[?9h';
  else if (mm === 'vt200') s += '\x1b[?1000h';
  else if (mm === 'drag') s += '\x1b[?1002h';
  else if (mm === 'any') s += '\x1b[?1003h';
  if (st.sgrMouse) s += '\x1b[?1006h';
  if (!m.wraparoundMode) s += '\x1b[?7l';
  if (b.type === 'alternate') s += '\x1b[?1049h';
  for (let y = 0; y < term.rows; y++) {
    const line = b.getLine(y);
    if (!line) continue;
    s += `\x1b[${y + 1};1H` + encodeRow(line);
  }
  s += `\x1b[${b.cursorY + 1};${b.cursorX + 1}H`;
  s += st.cursorVisible ? '\x1b[?25h' : '\x1b[?25l';
  return s;
}

// Semantic dump used by tests for cell-by-cell comparison of two replays.
function dumpGrid(term, st) {
  const b = term.buffer.active;
  const m = term.modes;
  const grid = [];
  for (let y = 0; y < term.rows; y++) {
    const line = b.getLine(y);
    const row = [];
    for (let x = 0; x < term.cols; x++) {
      const cell = line && line.getCell(x);
      if (!cell || cell.getWidth() === 0) row.push([null, '']);
      else row.push([cellKey(cell), cell.getChars() || ' ']);
    }
    grid.push(row);
  }
  return {
    cols: term.cols, rows: term.rows, alt: b.type === 'alternate',
    cursor: [b.cursorX, b.cursorY],
    modes: {
      bp: m.bracketedPasteMode, acm: m.applicationCursorKeysMode,
      apk: m.applicationKeypadMode, mouse: m.mouseTrackingMode,
      sgrMouse: st.sgrMouse, cursorVisible: st.cursorVisible,
    },
    grid,
  };
}

module.exports = { trackModes, buildRedraw, dumpGrid };
