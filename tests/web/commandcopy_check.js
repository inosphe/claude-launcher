const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/app.js'), 'utf8');
function element() {
  return { children: [], events: {}, style: {}, offsetHeight: 24, offsetWidth: 100,
    appendChild(child) { child.parent = this; this.children.push(child); },
    remove() { this.parent.children = this.parent.children.filter(c => c !== this); },
    contains(target) { return target === this; },
    addEventListener(type, fn) { this.events[type] = fn; },
    removeEventListener(type) { delete this.events[type]; },
    getBoundingClientRect() { return { left: 10, right: 610, top: 20, bottom: 220, height: 200 }; },
    setAttribute() {}, select() {}, setSelectionRange() {}, focus() {},
  };
}
let lines = [];
const host = element();
host.querySelector = () => element();
const written = [];
const notices = [];
const ctx = vm.createContext({ document: { createElement: element, activeElement: null },
  navigator: { clipboard: { writeText: async text => written.push(text) } },
  notify: (...args) => notices.push(args),
});
vm.runInContext(source.slice(source.indexOf('async function copyToBrowserClipboard('), source.indexOf('/* OSC 52')), ctx);
vm.runInContext(source.slice(source.indexOf('function terminalCommandAt('), source.indexOf('async function deleteHostClipboard(')), ctx);
const events = {};
let disposed = 0;
const subscribe = key => fn => { events[key] = fn; return { dispose() { disposed++; } }; };
const t = { element: host, rows: 10, buffer: { active: {
  viewportY: 0, get length() { return lines.length; }, getLine: row => lines[row],
} }, onScroll: subscribe('scroll'), onResize: subscribe('resize'),
  onWriteParsed: subscribe('write'), loadAddon(addon) { this.addon = addon; addon.activate(this); } };
function setLines(...rows) {
  lines = rows.map(row => { const [text, wrapped = false] = Array.isArray(row) ? row : [row];
    return { isWrapped: wrapped, length: text.length,
      getCell(col) { return { getCode: () => text.charCodeAt(col), getWidth: () => 1 }; },
      translateToString(trim, start = 0, end = text.length) {
        const part = text.slice(start, end); return trim ? part.trimEnd() : part;
      } }; });
}
function hover(row = 0) {
  host.events.mousemove({ target: host, clientX: 30, clientY: 20 + row * 20 + 10 });
}
function click(button) {
  let stopped = 0;
  return button.events.click({ preventDefault() { stopped++; }, stopPropagation() { stopped++; } })
    .then(() => assert.equal(stopped, 2));
}
(async () => {
  setLines('  ! claunch cflow2 approve  ');
  assert.equal(ctx.terminalCommandAt(t, 0), '! claunch cflow2 approve');
  for (const text of ['ordinary output', 'Use ! claunch approve', '!', '!!', '  ', '$ echo ! hi']) {
    setLines(text); assert.equal(ctx.terminalCommandAt(t, 0), null, text);
  }
  setLines('  - `! echo "한국어"` ');
  assert.equal(ctx.terminalCommandAt(t, 0), '! echo "한국어"');
  setLines('! echo "two  ', ['spaces"', true], 'following output');
  assert.equal(ctx.terminalCommandAt(t, 1), '! echo "two  spaces"', 'soft wraps preserve spaces');
  assert.equal(ctx.terminalCommandAt(t, 2), null, 'hard newline terminates command');
  setLines(['! truncated', true]);
  assert.equal(ctx.terminalCommandAt(t, 0), null, 'do not copy partial scrollback');
  setLines('! echo "<script>한글</script>"');
  ctx.installTerminalCommandCopy(t);
  const button = host.children[0];
  hover();
  assert.equal(button.hidden, false);
  assert.equal(written.length, 0, 'hover must not copy');
  await click(button);
  assert.deepEqual(written, ['! echo "<script>한글</script>"']);
  assert.equal(button.textContent, 'Copied');
  assert.equal(host.children.length, 1, 'command is never inserted as HTML');
  for (const key of ['scroll', 'resize', 'write']) {
    hover(); events[key](); assert.equal(button.hidden, true, key);
  }
  hover(); setLines('! changed'); await click(button);
  assert.equal(written.length, 1, 'stale command never copied');
  assert.equal(button.hidden, true);
  // HTTP clients use the same existing fallback, with no daemon-side copy.
  ctx.navigator.clipboard = undefined;
  ctx.document.execCommand = name => {
    assert.equal(name, 'copy'); written.push(host.children.find(c => c.value).value); return true;
  };
  hover(); await click(button);
  assert.equal(written.at(-1), '! changed');
  assert.equal(host.children.length, 1, 'fallback textarea removed');
  ctx.document.execCommand = () => false;
  await click(button);
  assert.equal(button.textContent, 'Copy failed');
  assert.equal(notices.length, 1);
  setLines('plain', '! scrolled'); t.buffer.active.viewportY = 1; hover();
  assert.equal(button.hidden, false, 'viewport offset is included');
  host.events.mouseleave(); assert.equal(button.hidden, true);
  t.addon.dispose();
  assert.equal(disposed, 3);
  assert.equal(host.children.length, 0);
  assert.deepEqual(Object.keys(host.events), []);
  assert.match(source, /installTerminalCommandCopy\(cliTerm\)/);
  assert.match(source, /installTerminalCommandCopy\(term\)/);
  console.log('command copy: detection, wraps, hover, browser/HTTP clipboard, stale output, lifecycle passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
