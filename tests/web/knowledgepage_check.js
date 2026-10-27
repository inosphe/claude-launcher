/* Real page module: listing, search, kind filter, CRUD of db records, read-only file records,
   workspace switching and stale reads (claunch-269q1); markdown bodies, the version history
   panel and reviving a superseded record (claunch-qgdfc). */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.textContent = ''; this.value = ''; this.className = '';
    this.classList = {add: name => { this.className = (this.className + ' ' + name).trim(); }}; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(name, value) { this.attributes[name] = value; }
}
const root = new Element('div');
const calls = [];
const fileRow = {id: 'md:v', kind: 'vision', title: '<script>bad()</script>', body: '방향', tags: ['t'], refs: [],
  origin: 'file', path: '.claunch/knowledge/v.md', author: '', status: 'active', updated_at: '2026-10-26'};
let records = [fileRow, {id: 'r1', kind: 'decision', title: '결정', body: '이유', tags: [], refs: ['claunch-1'],
  origin: 'db', author: 's1', status: 'active', updated_at: '2026-10-26T00:00', version: 1},
  {id: 'r2', kind: 'need', title: '대체된 니즈', body: '옛 배경', tags: [], refs: [],
  origin: 'db', author: 's1', status: 'superseded', updated_at: '2026-10-20', version: 2}];
let delay = null;
async function api(raw, opts = {}) {
  const url = new URL(raw, 'http://test');
  const method = opts.method || 'GET', body = opts.body ? JSON.parse(opts.body) : null;
  calls.push({url, method, body});
  let result;
  if (url.pathname === '/api/workspaces') result = {workspaces: [{name: 'alpha'}, {name: 'beta'}]};
  else if (url.pathname.endsWith('/history')) result = {versions: [
    {id: 'r1', version: 1, title: '결정', body: '옛 이유', tags: [], refs: [], status: 'active',
      author: 's1', updated_at: '2026-10-25', archived_by: 's2', archived_at: '2026-10-26', current: false},
    {id: 'r1', version: 2, title: '결정', body: '이유', tags: [], refs: [], status: 'active',
      author: 's1', updated_at: '2026-10-26', current: true}]};
  else if (method === 'DELETE') { records = records.filter(r => !url.pathname.endsWith(r.id)); result = {ok: true}; }
  else if (method === 'POST' || method === 'PUT') result = {record: body};
  else { result = {records}; if (delay) { const d = delay; delay = null; await d; } }
  return {ok: true, json: async () => result};
}
const mdInto = (node, text) => { node.md = text; return node; };
const ctx = vm.createContext({document: {createElement: tag => new Element(tag), getElementById: () => root}, api,
  URLSearchParams, confirm: () => true, console, mdInto});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/knowledge-page.js'), 'utf8'), ctx);
const all = (node = root) => [node, ...node.children.flatMap(c => typeof c === 'object' ? all(c) : [])];
const buttons = label => all().filter(n => n.tag === 'button' && n.textContent === label);
const field = name => all().find(n => n.name === name);
const labelled = label => all().find(n => n.attributes['aria-label'] === label);
const saveForm = () => all().find(n => n.tag === 'form' && n.children.some(c => c.textContent === '저장'));
async function settle() { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); }
(async () => {
  await ctx.KnowledgePage.open();
  assert(calls.some(c => c.url.pathname === '/api/knowledge' && c.url.searchParams.get('workspace') === 'alpha'));
  // Text stays text; the file record offers no edit or delete.
  assert(all().some(n => n.textContent === ' <script>bad()</script>'));
  assert(!all().some(n => n.tag === 'script'));
  assert.strictEqual(buttons('편집').length, 2);
  assert(all().some(n => typeof n.textContent === 'string' && n.textContent.includes('.claunch/knowledge/v.md')));
  // A body goes through the page's markdown renderer (mdInto) instead of textContent.
  assert(all().some(n => n.md === '방향'));
  assert(all().some(n => n.md === '이유' && n.className.includes('md')));
  // The 이력 button reads the record's versions and draws the past text too.
  buttons('이력')[0].onclick(); await settle();
  assert(calls.some(c => c.method === 'GET' && c.url.pathname === '/api/knowledge/r1/history'));
  assert(all().some(n => n.md === '옛 이유'));
  assert(all().some(n => typeof n.textContent === 'string' && n.textContent.includes('v1')));
  // A superseded record can be revived: the PUT sets its status back to active.
  buttons('되살리기')[0].onclick(); await settle();
  assert(calls.some(c => c.method === 'PUT' && c.url.pathname === '/api/knowledge/r2' && c.body.status === 'active'));
  // Search and kind filter go to the API.
  const search = labelled('기록 검색'); search.value = '착지'; search.oninput();
  all().find(n => n.tag === 'form' && n.children.some(c => c.textContent === '검색')).onsubmit({preventDefault() {}});
  await settle();
  assert.strictEqual(calls[calls.length - 1].url.searchParams.get('q'), '착지');
  const kind = labelled('종류'); kind.value = 'decision'; kind.onchange(); await settle();
  assert.strictEqual(calls[calls.length - 1].url.searchParams.get('kind'), 'decision');
  // Edit a db record.
  buttons('편집')[0].onclick();
  field('body').value = '고친 이유'; field('body').oninput();
  saveForm().onsubmit({preventDefault() {}}); await settle();
  assert(calls.some(c => c.method === 'PUT' && c.url.pathname === '/api/knowledge/r1' && c.body.body === '고친 이유'));
  // Create one with tags and refs split by line.
  buttons('기록 추가')[0].onclick();
  for (const [name, value] of [['title', '새 니즈'], ['body', '배경'], ['tags', 'a\nb'], ['refs', 'claunch-2']]) {
    field(name).value = value; field(name).oninput();
  }
  const kindPick = labelled('기록 종류'); kindPick.value = 'need'; kindPick.onchange();
  saveForm().onsubmit({preventDefault() {}}); await settle();
  assert(calls.some(c => c.method === 'POST' && c.body.kind === 'need' && c.body.tags.length === 2
    && c.body.refs[0] === 'claunch-2'));
  buttons('삭제')[0].onclick(); await settle();
  assert(calls.some(c => c.method === 'DELETE' && c.url.pathname === '/api/knowledge/r1'));
  const exportLink = all().find(n => n.tag === 'a' && n.textContent === 'Markdown 내보내기');
  assert(exportLink.href.startsWith('/api/knowledge/export?workspace=alpha'));
  const picker = labelled('Workspace'); picker.value = 'beta'; picker.onchange(); await settle();
  assert.strictEqual(calls[calls.length - 1].url.searchParams.get('workspace'), 'beta');
  // Leaving the route invalidates an outstanding read.
  let release; delay = new Promise(resolve => { release = resolve; });
  kind.value = ''; labelled('종류').onchange(); ctx.KnowledgePage.stop();
  const before = root.children;
  release(); await settle(); assert.strictEqual(root.children, before);
  console.log('ok');
})().catch(error => { console.error(error); process.exit(1); });
