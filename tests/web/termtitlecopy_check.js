// Clicking #term-title copies the session name (claunch-8dz6j).
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');
const static_ = path.join(__dirname, '../../src/claude_launcher/web/static');
const source = fs.readFileSync(path.join(static_, 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(static_, 'index.html'), 'utf8');
const toolbar = fs.readFileSync(path.join(static_, 'toolbar.js'), 'utf8');

// The title is a keyboard-reachable button, wired to the copy.
assert.match(html, /<span id="term-title" role="button" tabindex="0"><\/span>/);
assert.match(source, /\$\("term-title"\)\?\.addEventListener\("click", copyTermTitle\)/);
assert.match(toolbar, /click to copy/, 'the tooltip says what a click does');

const nodes = {};
const notices = [];
const written = [];
const commands = [];
function element() {
  return { children: [], textContent: '', style: {}, value: '',
    setAttribute() {}, appendChild(child) { this.children.push(child); child.parent = this; },
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); },
    select() {}, setSelectionRange() {}, focus() {} };
}
const body = element();
const ctx = vm.createContext({
  $: id => nodes[id],
  notify: (title, sub, opts) => notices.push({ title, sub, key: opts && opts.key }),
  navigator: { clipboard: { writeText: async text => { written.push(text); } } },
  document: {
    body, activeElement: null,
    createElement: tag => Object.assign(element(), { tag }),
    execCommand(name) { commands.push(name); return true; },
  },
});
vm.runInContext(source.slice(source.indexOf('async function copyToBrowserClipboard('),
  source.indexOf('/* OSC 52 (')), ctx);

(async () => {
  nodes['term-title'] = Object.assign(element(), { textContent: '  s459  ' });
  await ctx.copyTermTitle();
  assert.deepEqual(written, ['s459'], 'the trimmed name reaches the clipboard');
  assert.equal(notices.at(-1).title, 'Copied session name');
  assert.equal(notices.at(-1).sub, 's459');

  // Read at click time: a later attach rewrites the title.
  nodes['term-title'].textContent = 's460';
  await ctx.copyTermTitle();
  assert.deepEqual(written, ['s459', 's460']);

  // No session attached yet: nothing is copied and nothing is announced.
  nodes['term-title'].textContent = '';
  const before = notices.length;
  await ctx.copyTermTitle();
  assert.equal(written.length, 2);
  assert.equal(notices.length, before);

  // Plain http has no navigator.clipboard: the textarea route copies.
  ctx.navigator.clipboard = undefined;
  nodes['term-title'].textContent = 's461';
  await ctx.copyTermTitle();
  assert.deepEqual(commands, ['copy']);
  assert.equal(body.children.length, 0, 'the fallback textarea is removed');
  assert.equal(notices.at(-1).title, 'Copied session name');

  // Both routes refused: say so instead of claiming a copy.
  ctx.document.execCommand = () => false;
  await ctx.copyTermTitle();
  assert.equal(notices.at(-1).title, 'Copy failed');
  console.log('termtitlecopy: markup, click copies name, read at click, empty no-op, fallback, refusal passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
