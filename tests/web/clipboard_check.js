const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');
const source = fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/app.js'), 'utf8');
function element() {
  return { children: [], value: 'before AFTER', selectionStart: 7, selectionEnd: 12,
    disabled: false, style: {}, classList: { add() {}, remove() {} },
    setAttribute() {}, appendChild(child) { this.children.push(child); child.parent = this; },
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); },
    select() { this.selectionStart = 0; this.selectionEnd = this.value.length; },
    replaceChildren() { this.children = []; }, focus() {},
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; },
    addEventListener(event, callback) { this[event] = callback; } };
}
const nodes = {};
const calls = [];
const written = [];
const commands = [];
let response = { items: [{id: '1', text: '<script>한글</script>\nnext', copied_at: new Date().toISOString()}] };
const ctx = vm.createContext({
  $: id => nodes[id] || (nodes[id] = element()),
  el: (tag, css, text) => Object.assign(element(), { textContent: text, tag }),
  currentName: 's1', sessionEnded: false,
  autogrowTermInput() {},
  api: async (url, opts) => { calls.push([url, opts]); return { ok: true, json: async () => response }; },
  navigator: { clipboard: { writeText: async text => { written.push(text); } } },
  document: {
    activeElement: null,
    createElement: tag => Object.assign(element(), { tag, value: '' }),
    execCommand(name) {
      const area = nodes['term-clipboard-items'].children[0].children.find(c => c.tag === 'textarea');
      commands.push([name, area && area.value.slice(area.selectionStart, area.selectionEnd)]);
      return true;
    },
  },
});
vm.runInContext(source.slice(source.indexOf('function insertPromptPreset('), source.indexOf('function sendPromptPreset(')), ctx);
vm.runInContext(source.slice(source.indexOf('let hostClipboardRequest ='), source.indexOf('$("term-clipboard-toggle")?.addEventListener')), ctx);
(async () => {
  vm.runInContext('hostClipboardSession = currentName', ctx);
  await ctx.loadHostClipboard();
  const row = nodes['term-clipboard-items'].children[0];
  assert.equal(row.children[1].textContent, response.items[0].text);
  row.children[2].click();
  assert.equal(nodes['term-input-field'].value, 'before ' + response.items[0].text);
  assert.equal(calls.length, 1, 'Paste does not send terminal input');
  // Copy writes the entry to the browser machine's clipboard and nowhere else.
  const copy = row.children[3];
  assert.equal(copy.textContent, 'Copy');
  await copy.click();
  assert.deepEqual(written, [response.items[0].text]);
  assert.equal(calls.length, 1, 'Copy does not send terminal input');
  assert.match(nodes['term-clipboard-status'].textContent, /^Copied/);
  // Plain http from another machine has no navigator.clipboard: the selected
  // textarea route copies the same text and leaves nothing behind in the row.
  ctx.navigator.clipboard = undefined;
  const before = row.children.length;
  await copy.click();
  assert.deepEqual(commands, [['copy', response.items[0].text]]);
  assert.equal(row.children.length, before, 'the fallback textarea is removed');
  assert.match(nodes['term-clipboard-status'].textContent, /^Copied/);
  // A browser that refuses both routes says so instead of claiming a copy.
  ctx.navigator.clipboard = { writeText: async () => { throw new Error('denied'); } };
  ctx.document.execCommand = () => false;
  await copy.click();
  assert.match(nodes['term-clipboard-status'].textContent, /would not allow/);
  assert.equal(row.children[4].textContent, 'Delete');
  ctx.currentName = 's2';
  nodes['term-input-field'].value = 'new session';
  row.children[2].click();
  assert.equal(nodes['term-input-field'].value, 'new session');
  await ctx.deleteHostClipboard('1');
  assert.equal(calls[1][0], '/api/clipboard/1');
  assert.equal(calls[1][1].method, 'DELETE');
  console.log('clipboard: preview, caret replacement, no auto-send, copy to browser (both routes, refusal), session guard, delete passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
