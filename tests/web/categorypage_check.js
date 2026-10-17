/* Real category-page.js: the tree, selection -> extract/merge/move bodies,
   proposal review, the session pane's assign press, and stale reads. */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.textContent = ''; this.value = ''; this.parent = null; }
  append(...items) { for (const i of items) { if (typeof i === 'object') i.parent = this; this.children.push(i); } }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  setAttribute(name, value) { this.attributes[name] = value; }
  get childElementCount() { return this.children.length; }
  querySelector(tag) { return this.children.find(c => c.tag === tag) || null; }
  contains(other) { for (let n = other; n; n = n.parent) if (n === this) return true; return false; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null; }
}

const root = new Element('div');
const calls = [];
let tree = {
  nodes: [
    {id: 'n1', parent_id: null, label: 'web', aliases: ['dashboard'], path: ['web']},
    {id: 'n2', parent_id: 'n1', label: 'tabs', aliases: [], path: ['web', 'tabs']},
    {id: 'n3', parent_id: null, label: '<img src=x onerror=bad()>', aliases: [], path: ['<img src=x onerror=bad()>']},
  ],
  items: [
    {kind: 'session', ref: 's1', node_id: 'n1'},
    {kind: 'session', ref: 's2', node_id: 'n2'},
    {kind: 'issue', ref: 'i-1', node_id: 'n3'},
  ],
  unassigned: [{kind: 'session', ref: 's9'}],
  proposals: [{id: 'p1', parent_id: 'n1', label: 'panes', aliases: ['split'], description: '', reason: 'new UI',
               items: [{kind: 'session', ref: 's1', waiting_in: 'n1'}], proposed_by: 's1-cat1', created_at: '2026-10-18T00:00:00'}],
  details: {'session:s1': {status: 'idle', note: 'tab work'}, 'issue:i-1': {title: 'An issue', status: 'open'}},
};
let sessionView = {workspace: 'alpha', session: 's1', mode: 'fork', job: null,
  places: [{kind: 'session', ref: 's1', node: {path: ['web']}, proposals: []}]};
let delay = null;

async function api(raw, opts = {}) {
  const url = new URL(raw, 'http://test');
  const method = opts.method || 'GET', body = opts.body ? JSON.parse(opts.body) : null;
  calls.push({url, method, body});
  let result = {};
  if (url.pathname === '/api/workspaces') result = {workspaces: [{name: 'alpha'}, {name: 'beta'}]};
  else if (url.pathname === '/api/category' && method === 'GET') {
    result = {workspace: url.searchParams.get('workspace'), ...tree};
    if (delay) { const d = delay; delay = null; await d; }
  } else if (url.pathname === '/api/sessions/s1/category') result = sessionView;
  else if (url.pathname === '/api/sessions/s1/category/assign') {
    sessionView = {...sessionView, job: {state: 'running', fork: 's1-cat1', started_at: '2026-10-18T01:02:03', note: ''}};
    result = {mode: 'fork', fork: 's1-cat1'};
  }
  return {ok: true, json: async () => result};
}

const timers = [];
const ctx = vm.createContext({
  document: {createElement: tag => new Element(tag), getElementById: () => root},
  api, URLSearchParams, URL, console, confirm: () => true,
  setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout: () => {},
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/category-page.js'), 'utf8'), ctx);

// The route: #/category and #/category/<workspace> (app.js parseHash).
const appSrc = fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/app.js'), 'utf8');
function slice(name) {
  const start = appSrc.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  let depth = 0;
  for (let j = appSrc.indexOf('{', start); j < appSrc.length; j++) {
    if (appSrc[j] === '{') depth++;
    else if (appSrc[j] === '}') { depth--; if (!depth) return appSrc.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}
const parseHash = new Function(`${slice('parseHash')}; return parseHash;`)();
assert.deepStrictEqual({...parseHash('#/category')}, {page: 'category', workspace: ''});
assert.deepStrictEqual({...parseHash('#/category/my%20ws')}, {page: 'category', workspace: 'my ws'});

const all = (node = root) => [node, ...node.children.flatMap(c => typeof c === 'object' ? all(c) : [])];
const button = (label, under = root) => all(under).find(n => n.tag === 'button' && n.textContent === label);
const box = (label) => all().find(n => n.tag === 'input' && n.attributes['aria-label'] === label);
const pick = (label) => { const b = box(label); b.checked = true; b.onchange(); };
const formWith = (text) => all().find(n => n.tag === 'form' && n.children.some(c => c.textContent === text));
async function settle() { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); }
const last = () => calls[calls.length - 1];

(async () => {
  // The lowest-common-ancestor rule the page applies before asking.
  const byId = new Map(tree.nodes.map(n => [n.id, n]));
  assert.strictEqual(ctx.categoryLca(byId, ['n2', 'n1']), 'n1');
  assert.strictEqual(ctx.categoryLca(byId, ['n2', 'n3']), null);
  assert.strictEqual(ctx.categoryLca(byId, ['n2', 'n2']), 'n2');

  await ctx.CategoryPage.open('alpha'); await settle();
  assert(calls.some(c => c.url.pathname === '/api/category' && c.url.searchParams.get('workspace') === 'alpha'));
  // Labels are text, never markup.
  assert(all().some(n => n.textContent === '<img src=x onerror=bad()>'));
  assert(!all().some(n => n.tag === 'img'));
  assert(all().some(n => n.textContent === '트리에 없는 세션 1개'));
  assert(all().some(n => n.tag === 'a' && n.textContent === 's1' && n.href === '#/s/s1'));
  assert(all().some(n => n.tag === 'a' && n.textContent === 'i-1' && n.href === '#/beads/i-1'));

  // Two sessions inside "web": extract goes under web.
  pick('session s1 선택'); pick('session s2 선택');
  assert(!button('Extract…').disabled && !button('Merge…').disabled);
  button('Extract…').onclick();
  assert(all().some(n => n.textContent === '새 카테고리 위치: web'));
  const f = formWith('만들기');
  const [label, aliases] = f.children.filter(c => c.tag === 'input');
  label.value = 'routing'; label.oninput(); aliases.value = 'route, 라우팅'; aliases.oninput();
  f.onsubmit({preventDefault() {}}); await settle();
  assert.deepStrictEqual(last().url.pathname, '/api/category');  // reloaded after the write
  const extract = calls.find(c => c.url.pathname === '/api/category/extract');
  assert.strictEqual(extract.body.node_id, 'n1');
  assert.deepStrictEqual(extract.body.items, [{kind: 'session', ref: 's1'}, {kind: 'session', ref: 's2'}]);
  assert.deepStrictEqual(extract.body.aliases, ['route', '라우팅']);
  assert.strictEqual(extract.body.label, 'routing');
  // The selection is cleared by a write.
  assert(!box('session s1 선택').checked);

  // Items in different top-level categories: no extract, merge lands at the top level.
  pick('session s2 선택'); pick('issue i-1 선택');
  assert(button('Extract…').disabled);
  button('Merge…').onclick();
  assert(all().some(n => n.textContent === '새 카테고리 위치: (top level)'));
  let mf = formWith('만들기');
  mf.children.find(c => c.tag === 'input').value = 'nav';
  mf.onsubmit({preventDefault() {}}); await settle();
  const merge = calls.find(c => c.url.pathname === '/api/category/merge');
  assert.deepStrictEqual(merge.body.items.map(i => i.ref), ['s2', 'i-1']);
  assert.deepStrictEqual(merge.body.nodes, []);
  assert.strictEqual(merge.body.label, 'nav');

  // Whole categories merge too; an unassigned session counts as the root.
  pick('카테고리 tabs 선택'); pick('session s9 선택');
  assert(button('Extract…').disabled && !button('Merge…').disabled);
  button('Merge…').onclick();
  mf = formWith('만들기'); mf.children.find(c => c.tag === 'input').value = 'ui';
  mf.onsubmit({preventDefault() {}}); await settle();
  const merge2 = calls.filter(c => c.url.pathname === '/api/category/merge')[1];
  assert.deepStrictEqual(merge2.body.nodes, ['n2']);
  assert.deepStrictEqual(merge2.body.items, [{kind: 'session', ref: 's9'}]);

  // Move to an existing category.
  pick('session s9 선택');
  button('Move to…').onclick();
  const picker = all().find(n => n.tag === 'select' && n.attributes['aria-label'] === '카테고리 선택');
  picker.value = 'n2'; picker.onchange();
  button('옮기기').onclick(); await settle();
  const moved = calls.find(c => c.url.pathname === '/api/category/assign');
  assert.strictEqual(moved.body.node_id, 'n2');
  assert.deepStrictEqual(moved.body.items, [{kind: 'session', ref: 's9'}]);

  // Proposals: accept with an edited name.
  button('제안 검토 (1)').onclick();
  const name = all().find(n => n.attributes['aria-label'] === '수락할 이름');
  name.value = 'split panes'; name.oninput();
  button('수락').onclick(); await settle();
  const review = calls.find(c => c.url.pathname === '/api/category/proposals/p1/review');
  assert.strictEqual(review.body.decision, 'accept');
  assert.strictEqual(review.body.label, 'split panes');
  assert.deepStrictEqual(review.body.aliases, ['split']);

  // Workspace switch reads the other tree.
  const ws = all().find(n => n.tag === 'select' && n.attributes['aria-label'] === 'Workspace');
  ws.value = 'beta'; ws.onchange(); await settle();
  assert.strictEqual(last().url.searchParams.get('workspace'), 'beta');

  // Leaving the route invalidates an outstanding read.
  let release; delay = new Promise(resolve => { release = resolve; });
  button('새로고침').onclick(); ctx.CategoryPage.stop();
  const before = root.children;
  release(); await settle(); assert.strictEqual(root.children, before);

  // The session pane: its place, the press, the running job.
  const host = new Element('div');
  ctx.CategoryPane.mount(host, 's1'); await settle();
  assert(all(host).some(n => n.textContent === 'web'));
  assert(all(host).some(n => n.tag === 'a' && n.href === '#/category/alpha'));
  assert(all(host).some(n => String(n.textContent).includes('quick-fork')));
  button('Assign to tree', host).onclick(); await settle();
  assert(calls.some(c => c.url.pathname === '/api/sessions/s1/category/assign' && c.method === 'POST'));
  const pressed = button('배정 진행 중…', host);
  assert(pressed && pressed.disabled);
  assert(all(host).some(n => n.textContent === '진행 중'));
  ctx.CategoryPane.unmount();
  assert.strictEqual(host.children.length, 0);

  // An in-place job the session has not acted on can be requested again.
  sessionView = {...sessionView, mode: 'inplace', job: {mode: 'inplace', state: 'running', fork: null, started_at: '2026-10-18T01:02:03', note: ''}};
  const host2 = new Element('div');
  ctx.CategoryPane.mount(host2, 's1'); await settle();
  const again = button('다시 요청', host2);
  assert(again && !again.disabled);
  ctx.CategoryPane.unmount();
  console.log('ok');
})().catch(error => { console.error(error); process.exit(1); });
