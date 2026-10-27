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
    {kind: 'session', ref: 's10', node_id: 'n2'},
    {kind: 'session', ref: 's11', node_id: 'n2'},
    {kind: 'issue', ref: 'i-1', node_id: 'n3'},
    {kind: 'issue', ref: 'i-2', node_id: 'n1'},
  ],
  unassigned: [{kind: 'session', ref: 's9'}],
  proposals: [{id: 'p1', parent_id: 'n1', label: 'panes', aliases: ['split'], description: '', reason: 'new UI',
               items: [{kind: 'session', ref: 's1', waiting_in: 'n1'}], proposed_by: 's1-cat1', created_at: '2026-10-18T00:00:00'}],
  details: {'session:s1': {status: 'idle', life: 'running', note: 'tab work', issue: 'i-2'},
            'session:s2': {status: 'busy', life: 'running', note: ''},
            'session:s10': {status: 'exited', life: 'killed', note: ''},
            'session:s11': {status: 'exited', life: 'archived', note: ''},
            'issue:i-1': {title: 'An issue', status: 'open'},
            'issue:i-2': {title: 's1 work', status: 'in_progress', assignee: 's1'}},
};
let sessionView = {workspace: 'alpha', session: 's1', mode: 'fork', job: null,
  places: [{kind: 'session', ref: 's1', node: {path: ['web']}, proposals: []}]};
let killedView = {workspace: 'alpha', session: 's2', mode: 'fork', job: null, life: 'killed',
  places: [{kind: 'session', ref: 's2', node: null, proposals: []}]};
let delay = null;
const confirms = [];
let confirmAnswer = true;

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
  } else if (url.pathname === '/api/sessions/s2/category') result = killedView;
  else if (url.pathname === '/api/sessions/s2/category/assign') {
    if (!body?.wake) return {ok: false, status: 409, json: async () => ({error: "session 's2' has ended (killed); wake it to assign it", needs_wake: true, life: 'killed'})};
    killedView = {...killedView, life: 'running', job: {state: 'running', fork: 's2-cat1', started_at: '2026-10-18T01:02:03', note: ''}};
    result = {mode: 'fork', fork: 's2-cat1'};
  }
  return {ok: true, json: async () => result};
}

const timers = [];
const ctx = vm.createContext({
  document: {createElement: tag => new Element(tag), getElementById: () => root},
  api, URLSearchParams, URL, console, confirm: (m) => { confirms.push(m); return confirmAnswer; },
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

  // A session's issues go under it: its own issue first, else the assignee;
  // an issue whose session is not in the same list stays a row of the node.
  const S = (ref) => ({kind: 'session', ref}), I = (ref) => ({kind: 'issue', ref});
  const nest = (items, details) => JSON.parse(JSON.stringify(ctx.categoryNest(items, details)))
    .map(r => [r.item.ref, r.under.map(u => u.ref)]);
  assert.deepStrictEqual(nest([I('a'), S('x'), I('b'), I('c'), S('y'), I('d')], {
    'session:x': {issue: 'a'}, 'issue:a': {assignee: 'y'},   // own issue wins over assignee
    'issue:b': {assignee: 'y'}, 'issue:c': {assignee: 'gone'}, 'issue:d': {}}),
    [['x', ['a']], ['c', []], ['y', ['b']], ['d', []]]);
  assert.deepStrictEqual(nest([I('a')], {'issue:a': {assignee: 'x'}}), [['a', []]]);

  await ctx.CategoryPage.open('alpha'); await settle();
  const group = all().find(n => n.className === 'category-session-group');
  assert(group, 'a session with an issue renders as a group');
  assert.strictEqual(group.children[0].tag, 'div');
  assert(all(group.children[0]).some(n => n.tag === 'a' && n.textContent === 's1'));
  const sub = group.children[1];
  assert.strictEqual(sub.className, 'category-children category-session-children');
  assert(all(sub).some(n => n.tag === 'a' && n.textContent === 'i-2'));
  assert(!all(sub).some(n => n.tag === 'a' && n.textContent === 's1'));
  assert(calls.some(c => c.url.pathname === '/api/category' && c.url.searchParams.get('workspace') === 'alpha'));
  // Labels are text, never markup.
  assert(all().some(n => n.textContent === '<img src=x onerror=bad()>'));
  assert(!all().some(n => n.tag === 'img'));
  assert(all().some(n => n.textContent === '트리에 없는 세션 1개'));
  assert(all().some(n => n.tag === 'a' && n.textContent === 's1' && n.href === '#/s/s1'));
  assert(all().some(n => n.tag === 'a' && n.textContent === 'i-1' && n.href === '#/beads/i-1'));

  // Session state comes from the daemon's partition: a live one shows what the
  // screen reports, and the finished partitions stay hidden until asked.
  const shown = (ref) => all().some(n => n.tag === 'a' && n.textContent === ref);
  assert(all().some(n => n.textContent === 'running · idle'));
  assert(all().some(n => n.textContent === 'running · busy'));
  assert(!shown('s10') && !shown('s11'));
  const filter = (key) => all().find(n => n.tag === 'input' && n.id === 'category-show-' + key);
  const killBox = filter('killed'), archBox = filter('archived');
  assert(killBox && archBox && !killBox.checked && !archBox.checked);
  killBox.checked = true; killBox.onchange();
  assert(shown('s10') && !shown('s11') && all().some(n => n.textContent === 'killed'));
  archBox.checked = true; archBox.onchange();
  assert(shown('s11') && all().some(n => n.textContent === 'archived'));
  killBox.checked = false; killBox.onchange();
  archBox.checked = false; archBox.onchange();
  assert(!shown('s10') && !shown('s11'));

  // Two sessions inside "web": extract goes under web.
  pick('session s1 선택'); pick('session s2 선택');
  assert(!button('Extract…').disabled && !button('Merge…').disabled);
  // Split needs two items and one parent; the button is offered here but not
  // for a selection spread over two top-level categories (checked below).
  assert(!button('Split…').disabled);
  button('Split…').onclick();
  assert(all().some(n => n.textContent === '새 카테고리 위치: web'));
  const sf = formWith('분할');
  const lines = sf.children.find(c => c.tag === 'textarea');
  // The box is empty -- the person writes the lines -- and the picked items are
  // offered as a placeholder in the `kind:ref` spelling the route accepts.
  // Prefilled on one line they would read as a part labelled "session".
  assert.strictEqual(lines.value, '');
  assert.strictEqual(lines.attributes.placeholder, '새 이름: session:s1, session:s2\n나머지');
  lines.value = 'beads page: session:s1\nsims page: session:s2'; lines.oninput();
  sf.onsubmit({preventDefault() {}}); await settle();
  const split = calls.find(c => c.url.pathname === '/api/category/split');
  assert.strictEqual(split.body.parent_id, 'n1');
  assert.deepStrictEqual(split.body.parts, [
    {label: 'beads page', items: ['session:s1']},
    {label: 'sims page', items: ['session:s2']}]);
  // The selection the form told the person to choose from rides along as
  // `items`, in {kind, ref}: without it the server would draw candidates from
  // the whole parent subtree and the catch-all line would move unpicked items.
  assert.deepStrictEqual(split.body.items,
    [{kind: 'session', ref: 's1'}, {kind: 'session', ref: 's2'}]);
  // A line with no refs is the catch-all: the label goes alone and the server
  // gives that part whatever the earlier lines left.
  pick('session s1 선택'); pick('session s2 선택');
  button('Split…').onclick();
  const sf2 = formWith('분할');
  const lines2 = sf2.children.find(c => c.tag === 'textarea');
  lines2.value = 'beads page: session:s1\nrest'; lines2.oninput();
  sf2.onsubmit({preventDefault() {}}); await settle();
  const split2 = calls.filter(c => c.url.pathname === '/api/category/split')[1];
  assert.deepStrictEqual(split2.body.parts,
    [{label: 'beads page', items: ['session:s1']}, {label: 'rest'}]);
  // An empty form is refused before any request goes out.
  pick('session s1 선택'); pick('session s2 선택');
  button('Split…').onclick();
  const sf3 = formWith('분할');
  const callsBefore = calls.length;
  const blank = sf3.children.find(c => c.tag === 'textarea');
  blank.value = '\n\n'; blank.oninput();
  sf3.onsubmit({preventDefault() {}}); await settle();
  assert.strictEqual(calls.length, callsBefore, 'an empty split sends nothing');
  assert(all().some(n => n.textContent.includes('이름: 항목')));

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
  // "제안으로 등록" starts off: the press creates a new category at once.
  const propBox = all(host).find(n => n.tag === 'input' && n.id === 'category-proposal-opt');
  assert(propBox && !propBox.checked);
  assert(all(host).some(n => String(n.textContent).includes('제안으로 등록')));
  button('Assign to tree', host).onclick(); await settle();
  const presses = () => calls.filter(c => c.url.pathname === '/api/sessions/s1/category/assign' && c.method === 'POST');
  assert.strictEqual(presses().length, 1);
  assert.deepStrictEqual(presses()[0].body, {proposal: false});
  const pressed = button('배정 진행 중…', host);
  assert(pressed && pressed.disabled);
  assert(all(host).some(n => n.textContent === '진행 중'));
  ctx.CategoryPane.unmount();
  assert.strictEqual(host.children.length, 0);

  // Ticked: the new category is filed for review instead (a fresh pane starts
  // with the box off, so this is the box being turned on).
  sessionView = {...sessionView, job: null};
  const host4 = new Element('div');
  ctx.CategoryPane.mount(host4, 's1'); await settle();
  const prop4 = all(host4).find(n => n.tag === 'input' && n.id === 'category-proposal-opt');
  assert(prop4 && !prop4.checked);
  prop4.checked = true; prop4.onchange();
  button('Assign to tree', host4).onclick(); await settle();
  assert.deepStrictEqual(presses()[1].body, {proposal: true});
  ctx.CategoryPane.unmount();

  // An in-place job the session has not acted on can be requested again.
  sessionView = {...sessionView, mode: 'inplace', job: {mode: 'inplace', state: 'running', fork: null, started_at: '2026-10-18T01:02:03', note: ''}};
  const host2 = new Element('div');
  ctx.CategoryPane.mount(host2, 's1'); await settle();
  const again = button('다시 요청', host2);
  assert(again && !again.disabled);
  ctx.CategoryPane.unmount();

  // A killed session: the press asks before waking it; "no" sends nothing more,
  // "yes" sends the request again with wake.
  const host3 = new Element('div');
  ctx.CategoryPane.mount(host3, 's2'); await settle();
  assert(all(host3).some(n => String(n.textContent).includes('종료된(killed) 세션입니다')));
  const s2posts = () => calls.filter(c => c.url.pathname === '/api/sessions/s2/category/assign');
  confirmAnswer = false; confirms.length = 0;
  button('Assign to tree', host3).onclick(); await settle();
  assert.strictEqual(confirms.length, 1);
  assert(confirms[0].includes('killed') && confirms[0].includes('깨워서'));
  assert.strictEqual(s2posts().length, 1);
  assert(!all(host3).some(n => n.attributes && n.attributes.role === 'alert'));
  confirmAnswer = true;
  button('Assign to tree', host3).onclick(); await settle();
  assert.strictEqual(s2posts().length, 3);
  assert.deepStrictEqual(s2posts()[2].body, {wake: true, proposal: false});
  assert(button('배정 진행 중…', host3));
  ctx.CategoryPane.unmount();
  console.log('ok');
})().catch(error => { console.error(error); process.exit(1); });
