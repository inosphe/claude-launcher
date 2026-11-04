/* Real plan-page.js (claunch-7jzp8.2): the tree by default (claunch-7jzp8.5)
   -- session cards nested by their joint parent, each plan's steps under its
   card, a decision forking into its options with the taken one marked, a
   card's steps folded and unfolded, a filtered-out member dimmed, every
   tree rooted at its project and a session joined to nothing drawn as a
   branch of its project (claunch-7jzp8.5.2) -- then the
   overview grouped by stage, the stage and project filters,
   "ended sessions too" asking with all=1, and the session links; with a
   project picked, its aggregation session's pane (kept across redraws) or
   the start button (claunch-7jzp8.3). The 10 s poll (claunch-nerp0.1): a
   field being typed in is not redrawn, a page control the user was on gets
   the focus back by its data-plan-key, and the page's scroll position comes
   back. */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

class Element {
  constructor(tag, doc) {
    this.tag = tag; this.doc = doc || null;
    this.children = []; this.attributes = {}; this.textContent = ''; this.value = '';
    this.parent = null; this.scrollTop = 0; this.listeners = {};
  }
  get tagName() { return this.tag.toUpperCase(); }
  append(...items) { for (const i of items) { if (typeof i === 'object') i.parent = this; this.children.push(i); } }
  /* A redraw detaches what was in it -- and with it the focus, as the browser
     does -- and clears the container's scroll position. */
  replaceChildren(...items) {
    const hadFocus = this.doc && this.doc.activeElement && this.contains(this.doc.activeElement);
    this.children = []; this.scrollTop = 0;
    if (hadFocus) this.doc.activeElement = null;
    this.append(...items);
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }
  contains(other) { for (let n = other; n; n = n.parent) if (n === this) return true; return false; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter(f => f !== fn); }
  focus() { if (this.doc) this.doc.activeElement = this; }
  blur() {
    if (this.doc && this.doc.activeElement === this) this.doc.activeElement = null;
    this.dispatch('focusout');
  }
  dispatch(type) { for (let n = this; n; n = n.parent) (n.listeners[type] || []).forEach(fn => fn({type, target: this})); }
}

const calls = [];
const OVERVIEW = {
  counts: {user: 1, blocked: 0, closable: 1, working: 1, idle: 0},
  rows: [
    {session: 's2', stage: 'user', project: 'claunch', status: 'busy', category: 'running',
     plan: {title: 'Child', steps: 3, done: 1, current: 'write', waiting: 2}, run: {workflow: 'improv-worker-v2', round: 1, round_now: 3, current: ['work'], owed: ['choose: land?'], status: 'waiting_approval'}},
    {session: 's3', stage: 'closable', project: 'other', status: 'idle', category: 'running',
     plan: {title: 'Done', steps: 2, done: 2, closable: true}, run: null},
    {session: 's1', stage: 'working', project: 'claunch', status: 'busy', category: 'running',
     plan: null, run: {workflow: 'improv-leader-v2', current: ['standby'], owed: [], status: 'running'}},
  ],
};
OVERVIEW.aggregators = [{project: 'other', session: 'agg', running: true}];
const TREE = [
  {id: 'a', kind: 'step', title: 'write', status: 'doing', children: []},
  {id: 'd', kind: 'decision', title: 'storage', choice: 'B', detail: 'where the plan lives',
   options: [{label: 'A', effect: 'fast', risk: 'untested', recommended: true}, {label: 'B', effect: 'safe', risk: 'slow'}],
   children: [{id: 'c', kind: 'step', title: 'migrate', status: 'todo', children: []}]},
];
const JOINT = {groups: [{root: 's1', sessions: 1, waiting: 2, members: [
  {session: 's1', depth: 0, has_plan: false, parent: null, via: null},
  {session: 's2', depth: 1, has_plan: true, parent: 's1', via: 'spawn', tree: TREE}]}]};

const posts = [];
async function request(raw, method, body) {
  if (method === 'POST') { posts.push([raw, body]); return {name: 'agg2'}; }
  calls.push(raw);
  const url = new URL(raw, 'http://test');
  if (url.pathname === '/api/profiles') return {profile_options: [{value: 'p1'}, {value: 'p2'}]};
  if (url.pathname === '/api/plan/overview') return OVERVIEW;
  if (url.pathname === '/api/plan/joint') return JOINT;
  throw new Error('unexpected ' + raw);
}

const doc = {createElement: tag => new Element(tag, doc), getElementById: id => (id === 'plan-view' ? host : null),
             activeElement: null};
const host = new Element('div', doc);
const panes = [];
const timers = [];
const runTimers = () => { for (const fn of timers.splice(0)) fn(); };
const ctx = vm.createContext({
  document: doc,
  api: async () => { throw new Error('the page must use the injected request'); },
  URL, console, JSON, Object, Promise, Set,
  makePlanPane: (opts) => { panes.push({opts, stopped: false}); const me = panes[panes.length - 1];
    return {open: () => { me.opened = true; }, stop: () => { me.stopped = true; }}; },
  setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout: () => {},
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/plan-page.js'), 'utf8'), ctx);

const all = (n) => [n, ...n.children.flatMap(c => typeof c === 'object' ? all(c) : [])];
const text = () => all(host).map(n => n.textContent).join('\n');
const buttons = (label) => all(host).filter(n => n.tag === 'button' && n.textContent.startsWith(label));
const rows = () => all(host).filter(n => n.tag === 'a' && n.className === 'plan-page-session').map(n => n.textContent);
async function settle() { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); }

(async () => {
  ctx.PlanPage.open({request}); await settle();
  assert.deepStrictEqual(calls, ['/api/plan/overview', '/api/plan/joint?steps=1']);
  const liOf = (session) => all(host).find(n => n.tag === 'li' && n.className === 'ptree-node ptree-session'
    && all(n.children[0]).some(c => c.className === 'plan-page-session' && c.textContent === session));
  const byTitle = (title) => all(host).find(n => n.tag === 'li' && n.children[0] && n.children[0].children
    && n.children[0].children.some(c => c.textContent === title || (c.children || []).some(d => d.textContent === title)));
  let t = text();
  for (const want of ['세션 2개 · 내 결정 대기 2건', '연관 세션 2개 · 내 결정 대기 2건', '세션 1개 · 내 결정 대기 0건',
                      '계획 없는 세션 (1)', '자식 세션', '결정됨',
                      'Child — 단계 1/3 · 지금: write · 내 결정 대기 2건', '런이 기다림: choose: land?',
                      '기대효과: fast', '리스크: untested', '기대효과: safe', '리스크: slow'])
    assert(t.includes(want), `tree: missing ${want}`);
  assert.deepStrictEqual(rows(), ['s1', 's2', 's3']);
  // one tree per project, the project at its root, in name order
  const projectOf = (li) => { let n = li; while (n && !String(n.className).includes('ptree-project')) n = n.parent; return n; };
  const projectName = (li) => projectOf(li).children[0].children[1].textContent;
  const roots = host.children[0].children.filter(n => n.className === 'ptree ptree-root');
  assert.deepStrictEqual(roots.map(r => r.children[0].children[0].children[1].textContent), ['claunch', 'other']);
  assert.strictEqual(roots[0].children[0].className, 'ptree-node ptree-project');
  // s1, the group's root, is a branch of its project; s3 has no plan in a joint tree: under its project's plan-less branch
  assert.strictEqual(liOf('s1').parent.className, 'ptree ptree-sessions');
  assert.strictEqual(liOf('s1').parent.parent, roots[0].children[0]);
  assert.strictEqual(projectName(liOf('s3')), 'other');
  assert.strictEqual(liOf('s3').parent.parent.className, 'ptree-node ptree-noplan');
  // s2 hangs under s1's card; its steps under its own
  const s2 = liOf('s2');
  assert.strictEqual(s2.parent.className, 'ptree ptree-sessions');
  assert.strictEqual(s2.parent.parent, liOf('s1'));
  assert.strictEqual(s2.children[1].className, 'ptree ptree-steps');
  const decision = byTitle('storage');
  assert.strictEqual(decision.className, 'ptree-node ptree-decision ptree-decided');
  assert.strictEqual(decision.children[0].attributes.title, 'where the plan lives');
  const branches = decision.children[1].children.map(c => c.className);
  assert.deepStrictEqual(branches, ['ptree-node ptree-option ptree-unchosen ptree-recommended',
                                    'ptree-node ptree-option ptree-chosen', 'ptree-node ptree-step ptree-st-todo']);
  assert.strictEqual(byTitle('write').className, 'ptree-node ptree-step ptree-st-doing');
  // fold one card's steps, then unfold everything
  buttons('단계 접기')[0].onclick(); await settle();
  assert(!text().includes('기대효과: fast') && text().includes('단계 펼치기 (2)'));
  assert.strictEqual(liOf('s2').children.length, 1);
  buttons('모두 펼치기')[0].onclick(); await settle();
  assert(text().includes('기대효과: fast'));
  buttons('모두 접기')[0].onclick(); await settle();
  assert(!text().includes('기대효과: fast'));
  buttons('모두 펼치기')[0].onclick(); await settle();
  const savedTree = JOINT.groups[0].members[1].tree, savedCalls = calls.length;
  // claunch-vmcg7: the default pick reads 자동 선택됨, a ruled-out option is
  // marked with its reason, a node under an option hangs inside that option.
  JOINT.groups[0].members[1].tree = [
    {id: 'h1', kind: 'decision', title: 'layout', choice: 'A', auto: true,
     options: [{label: 'A', effect: 'fast', risk: 'untested', recommended: true, default: true},
               {label: 'B', effect: 'safe', risk: 'slow'},
               {label: 'C', effect: 'cheap', risk: 'brittle', rejected: 'no backup path'}],
     children: [{id: 'h2', kind: 'decision', title: 'migrate on B', under: 'B', options: [{label: 'X', effect: 'x', risk: 'x'}], children: []},
                {id: 'h3', kind: 'step', title: 'common step', status: 'todo', children: []}]},
  ];
  ctx.PlanPage._view().open(); await settle();
  const h1 = byTitle('layout');
  assert.strictEqual(h1.className, 'ptree-node ptree-decision ptree-decided');
  assert(h1.children[0].children.some(c => c.textContent === '자동 선택됨'));
  const hb = h1.children[1].children;
  assert.deepStrictEqual(hb.map(c => c.className), ['ptree-node ptree-option ptree-chosen ptree-recommended',
    'ptree-node ptree-option ptree-unchosen', 'ptree-node ptree-option ptree-unchosen ptree-rejected',
    'ptree-node ptree-step ptree-st-todo']);
  const hText = (n) => all(n).map(c => c.textContent).join('\n');
  assert(hText(hb[2]).includes('배제 이유: no backup path'));
  assert(hText(hb[1]).includes('migrate on B') && hText(hb[1]).includes('선택되지 않은 안의 하위 항목'));
  assert(all(hb[1]).some(n => n.tag === 'li' && String(n.className).includes('ptree-inactive')));
  assert(!hText(hb[0]).includes('migrate on B'));

  JOINT.groups[0].members[1].tree = savedTree;
  ctx.PlanPage._view().open(); await settle();
  calls.length = savedCalls;                                     // the reads above are this case's own

  // a filter that keeps s2 keeps its group whole: s1 drawn, dimmed
  buttons('내 차례')[0].onclick(); await settle();
  assert.deepStrictEqual(rows(), ['s1', 's2']);
  const card = (session) => liOf(session).children[0].className;
  assert(card('s1').includes('ptree-dim') && !card('s2').includes('ptree-dim') && card('s2').includes('plan-stage-user'));
  buttons('내 차례')[0].onclick(); await settle();
  buttons('닫아도 됨')[0].onclick(); await settle();
  assert.deepStrictEqual(rows(), ['s3']);                       // no member of s1's group left: not drawn
  buttons('닫아도 됨')[0].onclick(); await settle();

  // the stage grouping
  buttons('단계별')[0].onclick(); await settle();
  assert.strictEqual(calls.length, 2);                           // no read: the rows are already here
  t = text();
  for (const want of ['세션 진행 개요', '내 차례 (1)', '닫아도 됨 (1)', '진행 중 (1)',
                      'Child — 단계 1/3 · 지금: write · 내 결정 대기 2건', '런이 기다림: choose: land?',
                      'improv-worker-v2 r3 · work · waiting_approval', 'improv-leader-v2 · standby'])
    assert(t.includes(want), `missing ${want}`);
  assert.deepStrictEqual(rows(), ['s2', 's3', 's1']);
  assert.strictEqual(all(host).find(n => n.tag === 'a').href, '#/s/s2');

  // stage filter, then off again
  buttons('닫아도 됨')[0].onclick(); await settle();
  assert.deepStrictEqual(rows(), ['s3']);
  buttons('닫아도 됨')[0].onclick(); await settle();
  assert.deepStrictEqual(rows(), ['s2', 's3', 's1']);

  // project filter
  const pick = all(host).find(n => n.tag === 'select');
  pick.value = 'claunch'; pick.onchange(); await settle();
  assert.deepStrictEqual(rows(), ['s2', 's1']);

  // aggregation: listed with no project; a running one's pane, kept across redraws
  const projectPick = () => all(host).find(n => n.tag === 'select' && n.attributes['aria-label'] === '프로젝트');
  projectPick().value = 'other'; projectPick().onchange(); await settle();
  assert.strictEqual(panes.length, 1);
  assert.deepStrictEqual([panes[0].opts.session, panes[0].opts.nested, panes[0].opened], ['agg', true, true]);
  assert(all(host).some(n => n.className === 'plan-page-agg-session' && n.href === '#/s/agg'));
  buttons('닫아도 됨')[0].onclick(); await settle();
  buttons('닫아도 됨')[0].onclick(); await settle();
  assert.strictEqual(panes.length, 1);                           // the same pane, not a new one each redraw
  // none running: the profiles are read once and the start posts the picked one
  projectPick().value = 'claunch'; projectPick().onchange(); await settle();
  assert.strictEqual(panes[0].stopped, true);
  assert(text().includes('claunch에는 실행 중인 집계 세션이 없습니다.'));
  const prof = all(host).find(n => n.tag === 'select' && n.attributes['aria-label'] === '집계 세션 프로필');
  assert.strictEqual(prof.children.map(o => o.value).join(), 'p1,p2');
  prof.value = 'p2'; prof.onchange();
  buttons('집계 세션 시작')[0].onclick(); await settle();
  assert.strictEqual(JSON.stringify(posts), JSON.stringify([['/api/plan/aggregators/claunch/start', {profile: 'p2'}]]));
  assert.strictEqual(calls.filter(c => c === '/api/profiles').length, 1);
  projectPick().value = ''; projectPick().onchange(); await settle();
  assert(text().includes('other: agg'));

  // back to the tree: the joint trees are read again
  const before = calls.filter(c => c === '/api/plan/joint?steps=1').length;
  buttons('트리')[0].onclick(); await settle();
  assert.strictEqual(calls.filter(c => c === '/api/plan/joint?steps=1').length, before + 1);
  assert.deepStrictEqual(rows(), ['s1', 's2', 's3']);

  // a session with a plan that no spawn or issue joins to another: its own
  // group, drawn as a branch of its project beside the others
  JOINT.groups.push({root: 's3', sessions: 1, waiting: 0, members: [
    {session: 's3', depth: 0, has_plan: true, parent: null, via: null, tree: [{id: 'z', kind: 'step', title: 'wrap', status: 'done', children: []}]}]});
  buttons('트리')[0].onclick(); await settle();
  assert.strictEqual(liOf('s3').parent.className, 'ptree ptree-sessions');
  assert.strictEqual(liOf('s3').parent.parent.className, 'ptree-node ptree-project');
  assert.strictEqual(projectName(liOf('s3')), 'other');
  assert(!text().includes('계획 없는 세션') && text().includes('wrap'));

  // ended sessions too
  buttons('끝난 세션 포함')[0].onclick(); await settle();
  assert(calls.includes('/api/plan/overview?all=1'));

  // The 10 s poll does not take the focus (claunch-nerp0.1). A field inside
  // the aggregation pane: the poll draws nothing while it is focused, and the
  // held-back draw happens once the field is left.
  projectPick().value = 'other'; projectPick().onchange(); await settle();
  const aggHost = all(host).find(n => n.className === 'plan-host');
  assert(aggHost, 'the aggregation pane is mounted');
  const field = new Element('textarea', doc);
  field.value = 'half typed';
  aggHost.append(field);
  field.focus();
  assert.strictEqual(doc.activeElement, field, 'the fake DOM holds the focus');
  OVERVIEW.rows.push({session: 's9', stage: 'working', project: 'other', status: 'busy', category: 'running', plan: null, run: null});
  OVERVIEW.counts.working += 1;
  ctx.PlanPage._view().open(); await settle();
  assert.strictEqual(doc.activeElement, field, 'a field being typed in keeps the focus');
  assert(!text().includes('s9'), 'the poll waited for the field to be left');
  timers.length = 0;                                  // drop the pending polls; keep only the settle
  field.blur(); runTimers(); await settle();
  assert(text().includes('s9'), 'the held-back poll draws once the field is left');
  assert.strictEqual(field.value, 'half typed', 'the field keeps what was typed in it');

  // A control keeps the focus across a redraw: the element is new, and the
  // focus is on it because the redraw found it by its data-plan-key.
  const stageToggle = buttons('단계별')[0];
  assert.strictEqual(stageToggle.getAttribute('data-plan-key'), 'by:stage');
  stageToggle.focus();
  OVERVIEW.counts.working += 1;
  ctx.PlanPage._view().open(); await settle();
  const redrawn = buttons('단계별')[0];
  assert.notStrictEqual(redrawn, stageToggle, 'the poll redrew the button');
  assert.strictEqual(doc.activeElement, redrawn, 'the redrawn control has the focus');

  // The page's scroll position comes back too: the redraw clears it.
  doc.activeElement = null;
  host.scrollTop = 180;
  OVERVIEW.counts.working += 1;
  ctx.PlanPage._view().open(); await settle();
  assert.strictEqual(host.scrollTop, 180, 'the scroll position is put back');

  ctx.PlanPage.stop();
  console.log('ok');
})().catch(error => { console.error(error); process.exit(1); });
