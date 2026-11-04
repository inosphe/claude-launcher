/* Real retrospection-page.js (claunch-lmnlv): listing newest first with the
   workspace / session / text filters sent as query parameters, a record opened
   whole through /api/retrospection/<id> (markdown body, text-only title), the
   empty state, and a stale read losing to a newer one. Read only: no write
   request ever leaves the page. */
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
const listeners = {};
const rows = [
  {id: 'rt-2', workspace: 'alpha', session: 's2', trigger: 'manual', run: '', workflow: '', issue: '',
   title: '<b>s2 회고</b>', excerpt: '수동', chars: 2, problems: 0, proposals: 0, tags: [], auto: true,
   draft: 'wd-2', created_at: '2026-11-02T07:00:00+00:00'},
  {id: 'rt-1', workspace: 'alpha', session: 's1', trigger: 'run', run: 'run-1', workflow: 'improv-worker-v2',
   issue: 'claunch-x1', title: 's1 회고', excerpt: '## A', chars: 40, problems: 2, proposals: 1, tags: [],
   auto: false, draft: 'wd-1', created_at: '2026-11-02T06:00:00+00:00'},
];
let records = rows, delay = null;
async function api(raw, opts = {}) {
  const url = new URL(raw, 'http://test');
  calls.push({url, method: opts.method || 'GET'});
  let result;
  if (url.pathname === '/api/retrospection/facets') result = {workspaces: [{name: 'alpha', count: 2}, {name: 'beta', count: 0}],
    sessions: [{name: 's2', workspace: 'alpha', count: 1}, {name: 's1', workspace: 'alpha', count: 1}, {name: 's9', workspace: 'beta', count: 1}]};
  else if (url.pathname === '/api/retrospection') {
    result = {records}; if (delay) { const d = delay; delay = null; await d; }
  } else {
    const id = decodeURIComponent(url.pathname.split('/').pop());
    const row = rows.find(r => r.id === id);
    if (!row) return {ok: false, status: 404, json: async () => ({error: 'retrospection record not found'})};
    result = {record: {...row, body: '## A. 세션 회고\n- 목표'}};
  }
  return {ok: true, json: async () => result};
}
const mdInto = (n, text) => { n.md = text; return n; };
const ctx = vm.createContext({document: {createElement: tag => new Element(tag), getElementById: () => root,
  addEventListener: (name, fn) => { (listeners[name] = listeners[name] || []).push(fn); }}, api,
  URLSearchParams, console, mdInto, Promise});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/retrospection-page.js'), 'utf8'), ctx);
const all = (n = root) => [n, ...n.children.flatMap(c => typeof c === 'object' ? all(c) : [])];
const text = () => all().map(n => n.textContent).join('\n');
const buttons = label => all().filter(n => n.tag === 'button' && n.textContent === label);
const labelled = label => all().find(n => n.attributes['aria-label'] === label);
const lists = () => calls.filter(c => c.url.pathname === '/api/retrospection');
async function settle() { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); }
(async () => {
  await ctx.RetrospectionPage.open(); await settle();
  assert.strictEqual(lists()[0].url.searchParams.get('workspace'), null, 'all workspaces by default');
  assert.strictEqual(lists()[0].url.searchParams.get('limit'), '100');
  // Titles stay text; the list is in the order the daemon gave (newest first).
  assert(all().some(n => n.textContent === '<b>s2 회고</b>'));
  assert(!all().some(n => n.tag === 'b'));
  const titles = all().filter(n => n.tag === 'h4').map(h => h.textContent);
  assert.deepStrictEqual(titles, ['<b>s2 회고</b>', 's1 회고']);
  assert(text().includes('런 회고 · s1 · alpha · improv-worker-v2 · claunch-x1 · 2026-11-02 06:00 · 사용자 확정'));
  assert(text().includes('수동 회고 · s2 · alpha · 2026-11-02 07:00 · 자동 기록'));
  assert(text().includes('문제 2 · 제안 1'));

  // Filters go to the daemon as query parameters; the session list follows the workspace.
  labelled('Workspace').value = 'alpha'; labelled('Workspace').onchange(); await settle();
  assert.strictEqual(lists().slice(-1)[0].url.searchParams.get('workspace'), 'alpha');
  const sessionOptions = labelled('세션').children.map(o => o.value);
  assert.deepStrictEqual(sessionOptions, ['', 's2', 's1']);
  labelled('세션').value = 's1'; labelled('세션').onchange(); await settle();
  assert.strictEqual(lists().slice(-1)[0].url.searchParams.get('session'), 's1');
  const search = labelled('회고 검색'); search.value = '정의'; search.oninput();
  all().find(n => n.tag === 'form').onsubmit({preventDefault() {}}); await settle();
  assert.strictEqual(lists().slice(-1)[0].url.searchParams.get('q'), '정의');

  // While any filter is set the toolbar offers one button that clears them all;
  // it goes away again once the reload comes back unfiltered.
  const reset = all().find(n => n.tag === 'button' && n.textContent === '필터 초기화');
  assert(reset, 'reset button appears while a filter is set');
  reset.onclick(); await settle();
  const cleared = lists().slice(-1)[0].url.searchParams;
  assert.deepStrictEqual([cleared.get('workspace'), cleared.get('session'), cleared.get('q')], [null, null, null]);
  assert(!all().some(n => n.textContent === '필터 초기화'), 'reset button goes away once nothing is filtered');

  // Opening one by id (the router's #/retrospection/<id>) reads it whole.
  await ctx.RetrospectionPage.open('rt-1'); await settle();
  assert(calls.some(c => c.url.pathname === '/api/retrospection/rt-1'));
  assert(all().some(n => n.md === '## A. 세션 회고\n- 목표' && n.className.includes('md')));
  assert(text().includes('런 run-1'));
  // The card standing for the open record is marked, and Escape closes it the
  // way its 닫기 button does.
  assert(all().some(n => n.tag === 'article' && n.attributes['aria-current'] === 'true'));
  assert.strictEqual((listeners.keydown || []).length, 1, 'one Escape listener on the page');
  listeners.keydown[0]({key: 'Escape', preventDefault() {}}); await settle();
  assert(!text().includes('런 run-1'), 'Escape closes the open record');
  assert(!all().some(n => n.attributes['aria-current'] === 'true'));
  // An id that is gone says so.
  await ctx.RetrospectionPage.open('rt-none'); await settle();
  assert(text().includes('retrospection record not found'));

  // A stale list loses to the newer one.
  let release; delay = new Promise(r => { release = r; });
  records = rows;
  const first = ctx.RetrospectionPage.open();
  records = [];
  const second = ctx.RetrospectionPage.open();
  release(); await first; await second; await settle();
  assert(text().includes('조건에 맞는 회고가 없습니다') || text().includes('아직 회고 기록이 없습니다'));

  // Read only.
  assert(calls.every(c => c.method === 'GET'));
  console.log('ok');
})().catch(error => { console.error(error); process.exit(1); });
