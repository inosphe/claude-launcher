/* Real page module: workspace switching, review, CRUD, references and stale reads. */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.textContent = ''; this.value = ''; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(name, value) { this.attributes[name] = value; }
}
const root = new Element('div');
const calls = [];
let terms = [], proposals = [{id: 'p1', term: '<script>bad()</script>', definition: 'Meaning', aliases: [], source: 'context', proposed_by: 's94', status: 'pending', created_at: 'today'}];
let delay = null;
async function api(raw, opts = {}) {
  const url = new URL(raw, 'http://test');
  const method = opts.method || 'GET', body = opts.body ? JSON.parse(opts.body) : null;
  calls.push({url, method, body});
  let result;
  if (url.pathname === '/api/workspaces') result = {workspaces: [{name: 'alpha'}, {name: 'beta'}]};
  else if (url.pathname.endsWith('/review')) { terms = [{...proposals[0], id: 't1', reference: '[[term:#t1]]'}]; proposals = []; result = {}; }
  else if (url.pathname.endsWith('/resolve')) result = {text: body.text, references: [{status: 'resolved'}]};
  else if (method === 'DELETE') { terms = []; result = {ok: true}; }
  else if (method === 'POST' || method === 'PUT') { terms = [{...body, id: 't1', reference: '[[term:#t1]]'}]; result = {}; }
  else if (url.pathname.endsWith('/suggestions')) result = {suggestions: proposals};
  else { result = {terms}; if (delay) { const d = delay; delay = null; await d; } }
  return {ok: true, json: async () => result};
}
const ctx = vm.createContext({document: {createElement: tag => new Element(tag), getElementById: () => root}, api,
  URLSearchParams, confirm: () => true, console});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/glossary-page.js'), 'utf8'), ctx);
const all = (node = root) => [node, ...node.children.flatMap(c => typeof c === 'object' ? all(c) : [])];
const button = label => all().find(n => n.tag === 'button' && n.textContent === label);
const field = name => all().find(n => n.name === name);
async function settle() { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); }
(async () => {
  await ctx.GlossaryPage.open();
  button('제안 검토').onclick();
  assert(all().some(n => n.tag === 'h4' && n.textContent === '<script>bad()</script>'));
  assert(!all().some(n => n.tag === 'script'));
  assert(calls.some(c => c.url.searchParams.get('workspace') === 'alpha'));
  button('수락').onclick(); await settle();
  assert(calls.some(c => c.method === 'POST' && c.body.decision === 'accept'));
  button('등록된 용어').onclick();
  assert(button('편집') && button('삭제'));
  button('편집').onclick();
  field('definition').value = 'Edited meaning'; field('definition').oninput();
  all().find(n => n.tag === 'form' && n.children.some(c => c.textContent === '저장')).onsubmit({preventDefault() {}});
  await settle();
  assert(calls.some(c => c.method === 'PUT' && c.body.definition === 'Edited meaning'));
  button('삭제').onclick(); await settle();
  assert(calls.some(c => c.method === 'DELETE'));
  button('용어 추가').onclick();
  for (const [name, value] of [['term', 'new term'], ['definition', 'new meaning'], ['aliases', 'alias one\nalias two']]) {
    field(name).value = value; field(name).oninput();
  }
  all().find(n => n.tag === 'form' && n.children.some(c => c.textContent === '저장')).onsubmit({preventDefault() {}});
  await settle();
  assert(calls.some(c => c.method === 'POST' && c.body.term === 'new term' && c.body.aliases.length === 2));
  button('참조 확인').onclick();
  const ref = all().find(n => n.attributes['aria-label'] === '용어 참조 원문');
  ref.value = '[[term:#t1]]'; ref.oninput();
  all().find(n => n.tag === 'form' && n.children.some(c => c.textContent === '참조 확인')).onsubmit({preventDefault() {}});
  await settle();
  assert(all().some(n => n.textContent === '정의 확인됨'));
  const picker = all().find(n => n.attributes['aria-label'] === 'Workspace');
  picker.value = 'beta'; picker.onchange(); await settle();
  assert(calls.slice(-2).every(c => c.url.searchParams.get('workspace') === 'beta'));
  assert(!all().some(n => n.tag === 'pre'));
  // Leaving the route invalidates an outstanding read.
  let release; delay = new Promise(resolve => { release = resolve; });
  button('새로고침').onclick(); ctx.GlossaryPage.stop();
  const before = root.children;
  release(); await settle(); assert.strictEqual(root.children, before);
  console.log('ok');
})().catch(error => { console.error(error); process.exit(1); });
