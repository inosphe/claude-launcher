const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');
const source = fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/app.js'), 'utf8');
function element() {
  return { children: [], value: 'before AFTER', selectionStart: 7, selectionEnd: 12,
    disabled: false, style: {}, classList: { add() {}, remove() {} },
    setAttribute() {}, appendChild(child) { this.children.push(child); },
    replaceChildren() { this.children = []; }, focus() {},
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; },
    addEventListener(event, callback) { this[event] = callback; } };
}
const nodes = {};
const calls = [];
let response = { items: [{id: '1', text: '<script>한글</script>\nnext', copied_at: new Date().toISOString()}] };
const ctx = vm.createContext({
  $: id => nodes[id] || (nodes[id] = element()),
  el: (tag, css, text) => Object.assign(element(), { textContent: text, tag }),
  currentName: 's1', sessionEnded: false,
  autogrowTermInput() {},
  api: async (url, opts) => { calls.push([url, opts]); return { ok: true, json: async () => response }; },
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
  ctx.currentName = 's2';
  nodes['term-input-field'].value = 'new session';
  row.children[2].click();
  assert.equal(nodes['term-input-field'].value, 'new session');
  await ctx.deleteHostClipboard('1');
  assert.equal(calls[1][0], '/api/clipboard/1');
  assert.equal(calls[1][1].method, 'DELETE');
  console.log('clipboard: preview, caret replacement, no auto-send, session guard, delete passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
