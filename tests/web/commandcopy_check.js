const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/app.js'), 'utf8');
const css = fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/style.css'), 'utf8');
function listeners() {
  return { events: {},
    addEventListener(type, fn, capture) { this.events[type + (capture ? '!' : '')] = fn; },
    removeEventListener(type, fn, capture) { delete this.events[type + (capture ? '!' : '')]; } };
}
function element() {
  const classes = new Set();
  return { ...listeners(), children: [], style: {}, offsetHeight: 16, offsetWidth: 50,
    classList: { add: c => classes.add(c), remove: c => classes.delete(c),
      toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)), contains: c => classes.has(c) },
    appendChild(child) { child.parent = this; this.children.push(child); },
    replaceChildren(...kids) { this.children = kids; },
    remove() { this.parent.children = this.parent.children.filter(c => c !== this); },
    getBoundingClientRect() { return { left: 10, right: 610, top: 20, bottom: 220, width: 600, height: 200 }; },
    setAttribute() {}, select() {}, setSelectionRange() {}, focus() {},
  };
}
let lines = [];
const host = element();
host.querySelector = () => element();
const win = listeners();
const written = [];
const notices = [];
const timers = [];
const ctx = vm.createContext({ document: { createElement: element, activeElement: null }, window: win,
  navigator: { clipboard: { writeText: async text => written.push(text) } },
  notify: (...args) => notices.push(args),
  setTimeout: fn => { timers.push(fn); return timers.length; }, clearTimeout() {},
});
vm.runInContext(source.slice(source.indexOf('async function copyToBrowserClipboard('), source.indexOf('/* OSC 52')), ctx);
vm.runInContext(source.slice(source.indexOf('function terminalCommandAt('), source.indexOf('async function deleteHostClipboard(')), ctx);
const events = {};
let disposed = 0;
let selection = '';
const subscribe = key => fn => { events[key] = fn; return { dispose() { disposed++; } }; };
// 10 rows x 30 cols over a 600x200 screen: a cell is 20px wide and 20px tall.
const t = { element: host, rows: 10, cols: 30, modes: { mouseTrackingMode: 'none' },
  hasSelection: () => !!selection, getSelection: () => selection,
  buffer: { active: { viewportY: 0, get length() { return lines.length; }, getLine: row => lines[row] } },
  onScroll: subscribe('scroll'), onResize: subscribe('resize'),
  onWriteParsed: subscribe('write'), loadAddon(addon) { this.addon = addon; addon.activate(this); } };
function setLines(...rows) {
  lines = rows.map(row => { const [text, wrapped = false] = Array.isArray(row) ? row : [row];
    return { isWrapped: wrapped, length: text.length,
      getCell(col) { return { getCode: () => text.charCodeAt(col), getWidth: () => 1, getChars: () => text[col] ?? '' }; },
      translateToString(trim, start = 0, end = text.length) {
        const part = text.slice(start, end); return trim ? part.trimEnd() : part;
      } }; });
}
const at = (row, col) => ({ target: host, button: 0, clientX: 10 + col * 20 + 10, clientY: 20 + row * 20 + 10,
  shiftKey: false, prevented: 0, preventDefault() { this.prevented++; }, stopPropagation() { this.prevented++; } });
const plain = value => JSON.parse(JSON.stringify(value));
const hover = (row = 0, col = 3) => host.events.mousemove(at(row, col));
const layer = () => host.children.find(c => c.className === 'term-command-link');
const badge = () => host.children.find(c => c.className === 'term-command-copied');
const hot = () => host.classList.contains('term-command-hot');
async function click(row = 0, col = 3, moveTo = null) {
  const down = at(row, col);
  hover(row, col);
  host.events['mousedown!'](down);
  const up = moveTo ? at(...moveTo) : at(row, col);
  if (moveTo) host.events.mousemove(up);
  await win.events['mouseup!'](up);
  return { down, up };
}
(async () => {
  setLines('  ! claunch cflow2 approve  ');
  assert.equal(ctx.terminalCommandAt(t, 0), '! claunch cflow2 approve');
  for (const text of ['ordinary output', 'Use ! claunch approve', '!', '!!', '  ', '$ echo ! hi']) {
    setLines(text); assert.equal(ctx.terminalCommandAt(t, 0), null, text);
  }
  setLines('  - `! echo "한국어"` ');
  assert.equal(ctx.terminalCommandAt(t, 0), '! echo "한국어"');
  assert.deepEqual(plain(ctx.terminalCommandCells(t, 0).segments), [{ row: 0, from: 5, to: 17 }],
    'underline covers the command, not the Markdown bullet or backticks');
  setLines('! echo "two  ', ['spaces"', true], 'following output');
  assert.equal(ctx.terminalCommandAt(t, 1), '! echo "two  spaces"', 'soft wraps preserve spaces');
  assert.equal(ctx.terminalCommandAt(t, 2), null, 'hard newline terminates command');
  assert.deepEqual(plain(ctx.terminalCommandCells(t, 1).segments),
    [{ row: 0, from: 0, to: 13 }, { row: 1, from: 0, to: 7 }], 'one segment per wrapped row');
  setLines(['! truncated', true]);
  assert.equal(ctx.terminalCommandAt(t, 0), null, 'do not copy partial scrollback');

  setLines('  ! echo "<script>한글</script>"');
  ctx.installTerminalCommandCopy(t);
  assert.equal(host.children.length, 2, 'link layer and copied badge only; no button');
  assert.equal(layer().hidden, true);
  hover(0, 0);
  assert.equal(layer().hidden, true, 'leading blanks are not the link');
  assert.equal(hot(), false);
  hover(0, 3);
  assert.equal(layer().hidden, false, 'hover over the text underlines it');
  assert.equal(hot(), true, 'pointer cursor while over the text');
  assert.equal(layer().children.length, 1);
  const width = '! echo "<script>한글</script>"'.length * 20;
  assert.deepEqual([layer().children[0].style.left, layer().children[0].style.width], ['40px', `${width}px`],
    'underline starts at "!" and spans the command cells');
  assert.equal(written.length, 0, 'hover must not copy');
  hover(5, 3);
  assert.equal(layer().hidden, true, 'off the text the underline goes away');
  assert.equal(hot(), false);

  let { down } = await click(0, 3);
  assert.deepEqual(written, ['! echo "<script>한글</script>"']);
  assert.equal(down.prevented, 0, 'the press still reaches xterm (selection keeps working)');
  assert.equal(badge().hidden, false);
  assert.equal(badge().textContent, 'Copied');
  assert.equal(host.children.length, 2, 'command is never inserted as HTML');

  await click(0, 3, [0, 10]);
  assert.equal(written.length, 1, 'a drag selects; it never copies');
  selection = 'echo';
  await click(0, 3);
  assert.equal(written.length, 1, 'an existing selection is left alone');
  selection = '';
  host.events['mousedown!'](at(5, 3)); await win.events['mouseup!'](at(5, 3));
  assert.equal(written.length, 1, 'a click outside the text does nothing');

  // Mouse-tracking TUIs would receive the click as input: keep it from them.
  t.modes.mouseTrackingMode = 'vt200';
  ({ down } = await click(0, 3));
  assert.equal(down.prevented, 2, 'tracking mode: press kept from the PTY');
  assert.equal(written.length, 2);
  t.modes.mouseTrackingMode = 'none';

  for (const key of ['scroll', 'write']) {
    hover(); events[key](); assert.equal(layer().hidden, false, `${key} re-measures under a still pointer`);
  }
  hover(); events.resize(); assert.equal(layer().hidden, true, 'resize clears the link');
  setLines('plain output');
  hover(); events.write(); assert.equal(layer().hidden, true, 'repaint without a command clears the link');

  setLines('! old');
  hover(0, 1); host.events['mousedown!'](at(0, 1)); setLines('! changed'); await win.events['mouseup!'](at(0, 1));
  assert.equal(written.length, 2, 'stale command never copied');

  // HTTP clients use the same existing fallback, with no daemon-side copy.
  ctx.navigator.clipboard = undefined;
  ctx.document.execCommand = name => {
    assert.equal(name, 'copy'); written.push(host.children.find(c => c.value).value); return true;
  };
  await click(0, 1);
  assert.equal(written.at(-1), '! changed');
  assert.equal(host.children.length, 2, 'fallback textarea removed');
  ctx.document.execCommand = () => false;
  await click(0, 1);
  assert.equal(badge().textContent, 'Copy failed');
  assert.equal(badge().classList.contains('term-command-copied-failed'), true);
  assert.equal(notices.length, 1);
  timers.at(-1)(); assert.equal(badge().hidden, true, 'badge fades after a moment');

  setLines('plain', '! scrolled'); t.buffer.active.viewportY = 1; hover(0, 1);
  assert.equal(layer().hidden, false, 'viewport offset is included');
  assert.equal(layer().children[0].style.top, '0px', 'drawn on the screen row, not the buffer row');
  host.events.mouseleave(); assert.equal(layer().hidden, true);
  t.addon.dispose();
  assert.equal(disposed, 3);
  assert.equal(host.children.length, 0);
  assert.deepEqual(Object.keys(host.events), []);
  assert.deepEqual(Object.keys(win.events), []);
  assert.equal(hot(), false);
  assert.match(source, /installTerminalCommandCopy\(cliTerm\)/);
  assert.match(source, /installTerminalCommandCopy\(term\)/);
  assert.doesNotMatch(source + css, /term-command-copy\b/, 'the Copy command button is gone');
  assert.match(css, /\.term-command-link, \.term-command-copied \{ pointer-events: none; \}/);
  console.log('command copy: detection, cell ranges, hover underline, click vs drag, tracking mode, clipboard, stale output, lifecycle passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
