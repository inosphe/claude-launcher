/* Real plan-pane.js (claunch-7jzp8): the step tree and its decision cards
   (effect, risk, recommendation), the closure section and the closable line,
   the decide bodies naming the node's rev and the note, a 409 reloading the
   reworded node, and the feedback box. The joint view (claunch-7jzp8.1):
   the toggle, the related sessions' lines, and a nested pane deciding on
   the other session's plan. The plan is drawn as a tree in #/plan's shape
   (claunch-7jzp8.5.2): step branches with a status mark, a decision forking
   into option branches (recommended, then chosen and unchosen), its own
   steps under it, the closure items as branches of their heading. The 5 s
   poll (claunch-nerp0): a field being typed in is not redrawn, a control
   the user was on gets the focus back by its data-plan-key, and the pane's
   scroll position comes back. Rounds (claunch-r3j1o.1): the round row,
   a sealed round drawn read-only, the carried item's round chip. */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

/* Just enough of the DOM to hold a focus: focus()/blur() move
   document.activeElement, a redraw detaches what was in it (and with it the
   focus, as the browser does), and focusout bubbles up to the pane root,
   which is where plan-pane.js listens for it. */
class Element {
  constructor(tag, doc) {
    this.tag = tag; this.doc = doc || null;
    this.children = []; this.attributes = {}; this.textContent = ''; this.value = '';
    this.parent = null; this.scrollTop = 0; this.listeners = {};
  }
  get tagName() { return this.tag.toUpperCase(); }
  get parentNode() { return this.parent; }
  append(...items) { for (const i of items) { if (typeof i === 'object') i.parent = this; this.children.push(i); } }
  replaceChildren(...items) {
    const hadFocus = this.doc && this.doc.activeElement && this.contains(this.doc.activeElement);
    this.children = []; this.scrollTop = 0;
    if (hadFocus) this.doc.activeElement = null;
    this.append(...items);
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }
  contains(other) { for (let n = other; n; n = n.parent) if (n === this) return true; return false; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  focus() { if (this.doc) this.doc.activeElement = this; }
  blur() {
    if (this.doc && this.doc.activeElement === this) this.doc.activeElement = null;
    this.dispatch('focusout');
  }
  dispatch(type) { for (let n = this; n; n = n.parent) (n.listeners[type] || []).forEach(fn => fn({type, target: this})); }
}

const calls = [];
const OPTS = [{label: 'A', effect: 'fast', risk: 'untested', recommended: true}, {label: 'B', effect: 'safe', risk: 'slow'}];
let plan = {
  session: 's1', revision: 3, title: 'Ship X', goal: 'X in one round', issue: 'claunch-x',
  steps: [
    {id: 's1', kind: 'step', title: 'read code', detail: '', status: 'done', rev: 1, result: null},
    {id: 'd1', kind: 'decision', title: 'storage', detail: 'where to keep it', options: OPTS, rev: 2, result: null,
     children: [{id: 's2', kind: 'step', title: 'write store', detail: '', status: 'doing', rev: 1, result: null, children: []}]},
  ],
  closure: [
    {id: 'v1', kind: 'verify', title: 'open the pane by hand', detail: 'press ☰ plan', rev: 1, result: null},
    {id: 'f1', kind: 'followup', title: 'joint tree', detail: '', status: 'open', rev: 1, result: null},
  ],
  summary: {steps: 2, done: 1, current: 'write store', closable: false, waiting: 3,
            blockers: [{id: 'd1', title: 'storage', why: 'decision not taken', who: 'user'},
                       {id: 's2', title: 'write store', why: 'step doing', who: 'session'}]},
};
let conflictOn = null;
let jointGroup = null;
const plan2 = {
  session: 's2', revision: 1, title: 'Child Y', goal: '', issue: 'claunch-x.1',
  steps: [{id: 'd9', kind: 'decision', title: 'cache?', detail: '', options: OPTS, rev: 4, result: null, children: []}],
  closure: [], summary: {steps: 0, done: 0, current: null, closable: false, waiting: 1,
                         blockers: [{id: 'd9', title: 'cache?', why: 'decision not taken', who: 'user'}]},
};
let changes = 0;
let roundsDoc = null;
const sealedPlan = {
  session: 's1', revision: 7, title: 'Round one', goal: '', issue: 'claunch-x', run: 'r1', round: 1, sealed: true,
  closed_at: '2026-11-03T00:00:00+00:00', key: 'r1/1',
  steps: [{id: 'd1', kind: 'decision', title: 'storage', detail: '', options: OPTS, rev: 1,
           result: {choice: 'B', note: '', at: 'x'}, children: []}],
  closure: [{id: 'v1', kind: 'verify', title: 'old check', detail: '', rev: 1, result: null}],
  summary: {steps: 0, done: 0, current: null, closable: false, waiting: 1,
            blockers: [{id: 'v1', title: 'old check', why: 'not checked yet', who: 'user'}]},
};

async function request(raw, method = 'GET', body) {
  calls.push({path: raw, method, body});
  const url = new URL(raw, 'http://test');
  if (url.pathname === '/api/plan/rounds') {
    if (!roundsDoc) throw Object.assign(Error('no rounds'), {status: 404});
    return roundsDoc;
  }
  if (url.pathname === '/api/plan' && url.searchParams.get('round')) {
    return {session: 's1', round: url.searchParams.get('round'),
            plan: url.searchParams.get('round') === 'r1/1' ? sealedPlan : null};
  }
  if (url.pathname === '/api/plan') {
    const who = url.searchParams.get('session');
    return {session: who, plan: who === 's2' ? plan2 : plan};
  }
  if (url.pathname === '/api/plan/joint') {
    if (!jointGroup) throw Object.assign(Error('no joint'), {status: 404});
    return {session: url.searchParams.get('session'), group: jointGroup};
  }
  if (url.pathname === '/api/plan/feedback') return {delivered: true};
  const m = url.pathname.match(/^\/api\/plan\/node\/([^/]+)\/decide$/);
  if (m) {
    const doc = body.session === 's2' ? plan2 : plan;
    const node = [...doc.steps, ...doc.closure].find(n => n.id === m[1]);
    if (conflictOn === node.id) {
      conflictOn = null; node.rev += 1; node.title = 'storage, reworded';
      throw Object.assign(Error(`node ${node.id} is at rev ${node.rev}`), {status: 409, body: {current: plan}});
    }
    assert.strictEqual(body.rev, node.rev, `stale rev for ${node.id}`);
    node.result = {choice: body.choice, verdict: body.verdict, action: body.action, note: body.note};
    return {node};
  }
  throw new Error('unexpected ' + raw);
}

const doc = {createElement: tag => new Element(tag, doc), activeElement: null};
const timers = [];
const ctx = vm.createContext({
  document: doc,
  api: async () => { throw new Error('the pane must use the injected request'); },
  URL, console, JSON, Object,
  setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout: () => {},
});
const runTimers = () => { for (const fn of timers.splice(0)) fn(); };
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/plan-pane.js'), 'utf8'), ctx);

const all = (node) => [node, ...node.children.flatMap(c => typeof c === 'object' ? all(c) : [])];
const text = (host) => all(host).map(n => n.textContent).join('\n');
const buttons = (host, label) => all(host).filter(n => n.tag === 'button' && n.textContent === label);
const input = (host, label) => all(host).filter(n => n.attributes['aria-label'] === label);
async function settle() { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); }
const posts = () => JSON.parse(JSON.stringify(calls.filter(c => c.method === 'POST')));

(async () => {
  const host = new Element('div');
  ctx.PlanPane.mount(host, 's1', {request, onChange: () => { changes++; }}); await settle();
  const t = text(host);
  for (const want of ['Ship X', 'X in one round', '단계 1/2 완료 · 지금: write store', '닫을 수 없음: 내 결정 대기 1건 · 세션 작업 남음 1건',
                      '결정 필요', '기대 효과: fast', '리스크: untested', '권장', '진행 중', '사람 확인', '후속 후보', '종료 조건'])
    assert(t.includes(want), `missing ${want}`);
  // the nested step is drawn under the decision
  const d1 = all(host).find(n => n.tag === 'li' && n.className.includes('plan-decision'));
  assert(text(d1).includes('write store'));
  // the tree: the steps hang from the plan, the decision forks into its options, then its own steps
  assert.strictEqual(d1.className, 'ptree-node ptree-decision ptree-undecided plan-item plan-decision');
  assert.strictEqual(d1.parent.className, 'ptree ptree-steps');
  // every item names its node id (th-437508), between the mark and the title
  assert.deepStrictEqual(d1.children[0].children.map(c => c.textContent), ['◆', 'd1', 'storage', '결정 필요']);
  const optionClasses = () => all(host).find(n => n.tag === 'li' && n.className.includes('plan-decision'))
    .children.find(c => c.className === 'ptree plan-options-tree').children.map(c => c.className);
  assert.deepStrictEqual(optionClasses(), ['ptree-node ptree-option ptree-recommended', 'ptree-node ptree-option']);
  const sub = d1.children.find(c => c.className === 'ptree ptree-steps');
  assert.strictEqual(sub.children[0].className, 'ptree-node ptree-step ptree-st-doing plan-item plan-step');
  assert.deepStrictEqual(sub.children[0].children[0].children.map(c => c.textContent), ['◐', 's2', 'write store', '진행 중']);
  const closures = all(host).find(n => n.className === 'ptree ptree-closures');
  assert.deepStrictEqual(closures.children.map(c => c.className),
    ['ptree-node ptree-closure ptree-st-todo plan-item plan-verify', 'ptree-node ptree-closure ptree-st-todo plan-item plan-followup']);

  // Pick B with a note: the body names the node's rev and the note.
  const note = input(host, '선택에 붙일 메모 (선택 사항)')[0];
  note.value = 'safer'; note.oninput();
  buttons(host, '이 안 선택')[1].onclick(); await settle();
  assert.deepStrictEqual(posts().slice(-1)[0],
    {path: '/api/plan/node/d1/decide', method: 'POST', body: {session: 's1', note: 'safer', rev: 2, choice: 'B'}});
  assert.strictEqual(changes, 1);
  assert(text(host).includes('선택: B — safer'));
  assert.deepStrictEqual(optionClasses(), ['ptree-node ptree-option ptree-unchosen ptree-recommended',
                                           'ptree-node ptree-option ptree-chosen']);
  assert(text(host).includes('선택됨'));

  // A 409: the session reworded the node; the pane reloads and says so.
  plan.steps[1].result = null;
  ctx.PlanPane._view().open(); await settle();
  conflictOn = 'd1';
  buttons(host, '이 안 선택')[0].onclick(); await settle();
  assert(text(host).includes('세션이 이 항목을 고쳤습니다'));
  assert(text(host).includes('storage, reworded'));

  // A verify item and a followup.
  buttons(host, '통과')[0].onclick(); await settle();
  assert.deepStrictEqual(posts().slice(-1)[0].body, {session: 's1', note: '', rev: 1, verdict: 'passed'});
  buttons(host, '이슈로 넘김')[0].onclick(); await settle();
  assert.deepStrictEqual(posts().slice(-1)[0].body, {session: 's1', note: '', rev: 1, action: 'issue'});
  assert(text(host).includes('확인: 통과') && text(host).includes('처분: 이슈로 넘김'));

  // Feedback goes out; an empty box sends nothing.
  const before = posts().length;
  buttons(host, '의견 보내기')[0].onclick(); await settle();
  assert.strictEqual(posts().length, before);
  const fb = input(host, '계획에 대한 의견')[0];
  fb.value = 'split d1'; fb.oninput();
  buttons(host, '의견 보내기')[0].onclick(); await settle();
  assert.deepStrictEqual(posts().slice(-1)[0], {path: '/api/plan/feedback', method: 'POST', body: {session: 's1', text: 'split d1'}});

  // Joint view: a toggle, the related sessions' lines, a nested pane on s2's plan.
  assert(!text(host).includes('연관 세션 함께 보기'));
  jointGroup = {root: 's1', sessions: 2, waiting: 4, closable: false, members: [
    {session: 's1', parent: null, via: null, depth: 0, has_plan: true, title: 'Ship X', steps: 2, done: 1, current: 'write store', waiting: 3, closable: false},
    {session: 's2', parent: 's1', via: 'spawn', depth: 1, has_plan: true, title: 'Child Y', steps: 0, done: 0, waiting: 1, closable: false},
  ]};
  ctx.PlanPane._view().open(); await settle();
  assert(text(host).includes('연관 세션 2개 · 내 결정 대기 4건'));
  buttons(host, '연관 세션 함께 보기')[0].onclick(); await settle();
  let jt = text(host);
  for (const want of ['자식 세션', 'Child Y', '단계 0/0 완료 · 내 결정 대기 1건 · 닫을 수 없음', '이 세션만 보기'])
    assert(jt.includes(want), `joint missing ${want}`);
  assert.strictEqual(buttons(host, '계획 펼치기').length, 1);          // not for the session itself
  buttons(host, '계획 펼치기')[0].onclick(); await settle();
  assert(text(host).includes('cache?'));
  assert(calls.some(c => c.path === '/api/plan?session=s2'));
  assert(!calls.some(c => c.path === '/api/plan/joint?session=s2'));   // a nested pane reads no joint tree
  buttons(host, '이 안 선택')[0].onclick(); await settle();
  assert.deepStrictEqual(posts().slice(-1)[0],
    {path: '/api/plan/node/d9/decide', method: 'POST', body: {session: 's2', note: '', rev: 4, choice: 'A'}});
  buttons(host, '접기')[0].onclick(); await settle();
  assert(!text(host).includes('cache?'));
  buttons(host, '이 세션만 보기')[0].onclick(); await settle();
  assert(text(host).includes('종료 조건'));
  jointGroup = null;

  // The 5 s poll does not take the focus (claunch-nerp0).
  plan = {
    session: 's1', revision: 1, title: 'Ship X', goal: '', issue: 'claunch-x',
    steps: [{id: 'd1', kind: 'decision', title: 'storage', detail: '', options: OPTS, rev: 1, result: null, children: []}],
    closure: [{id: 'f1', kind: 'followup', title: 'follow it up', detail: '', status: 'open', rev: 1, result: null}],
    summary: {steps: 0, done: 0, current: null, closable: false, waiting: 1, blockers: []},
  };
  ctx.PlanPane._view().open(); await settle();
  const noteLabel = '선택에 붙일 메모 (선택 사항)';
  const typed = input(host, noteLabel)[0];
  typed.focus();
  assert.strictEqual(doc.activeElement, typed, 'the fake DOM holds the focus');
  typed.value = 'half typed'; typed.oninput();
  plan = {...plan, title: 'Ship X, retitled'};       // the session moved on
  ctx.PlanPane._view().open(); await settle();
  assert.strictEqual(input(host, noteLabel)[0], typed, 'a field being typed in is not redrawn');
  assert.strictEqual(doc.activeElement, typed, 'and it keeps the focus');
  assert(!text(host).includes('Ship X, retitled'), 'the poll waited for the field to be left');
  assert.strictEqual(ctx.PlanPane._view()._state().notes.d1, 'half typed');

  // Leaving the field draws what the poll held back, and the note stays.
  timers.length = 0;
  typed.blur(); runTimers();
  assert(text(host).includes('Ship X, retitled'), 'the held-back poll draws on blur');
  assert.strictEqual(input(host, noteLabel)[0].value, 'half typed', 'the typed note survives the redraw');

  // A control keeps the focus across a redraw: the element is new, the focus
  // is on it because the redraw found it by its data-plan-key.
  const choose = buttons(host, '이 안 선택')[0];
  assert.strictEqual(choose.getAttribute('data-plan-key'), 'opt:d1:A');
  choose.focus();
  plan = {...plan, revision: 2};
  ctx.PlanPane._view().open(); await settle();
  const newChoose = buttons(host, '이 안 선택')[0];
  assert.notStrictEqual(newChoose, choose, 'the poll redrew the button');
  assert.strictEqual(doc.activeElement, newChoose, 'the redrawn control has the focus');

  // The pane's scroll position comes back too.
  doc.activeElement = null;
  host.scrollTop = 240;
  plan = {...plan, revision: 3};
  ctx.PlanPane._view().open(); await settle();
  assert.strictEqual(host.scrollTop, 240, 'the scroll position is put back');

  // A poll with the same plan leaves the DOM alone (th-437508): a text
  // selection being dragged spans these very nodes.
  const kept = buttons(host, '이 안 선택')[0];
  ctx.PlanPane._view().open(); await settle();
  assert.strictEqual(buttons(host, '이 안 선택')[0], kept, 'an unchanged poll does not redraw');
  // ...and a changed one waits while the user holds a selection in the pane.
  doc.getSelection = () => ({isCollapsed: false, rangeCount: 1, anchorNode: kept});
  plan = {...plan, title: 'Ship X, while selecting'};
  ctx.PlanPane._view().open(); await settle();
  assert(!text(host).includes('Ship X, while selecting'), 'a selection holds the redraw back');
  doc.getSelection = () => ({isCollapsed: true, rangeCount: 0, anchorNode: null});
  timers.length = 0;
  kept.dispatch('mouseup'); runTimers();
  assert(text(host).includes('Ship X, while selecting'), 'letting the selection go draws it');
  delete doc.getSelection;

  // 보류 (claunch-jw1v2 D), a reopened check, a followup the session closed
  // itself and a note (th-62379d).
  plan = {...plan, revision: 9, closure: [
    {id: 'v2', kind: 'verify', title: 'live run', detail: '', rev: 3, result: null,
     previous: {verdict: 'passed'}, reopened: {reason: 'the pane code changed'}},
    {id: 'f2', kind: 'followup', title: 'filed as claunch-y', detail: '', status: 'done', rev: 1, result: null},
    {id: 'n1', kind: 'note', title: 'could also cache X', detail: 'later', rev: 1, result: null},
  ]};
  ctx.PlanPane._view().open(); await settle();
  let ct = text(host);
  assert(ct.includes('세션이 다시 확인을 요청했습니다: the pane code changed'));
  assert(ct.includes('세션이 처리함'));
  assert(ct.includes('참고 (최종 확인용 · 게이트 없음)') && ct.includes('could also cache X'));
  const notes = all(host).find(n => n.className === 'ptree ptree-notes');
  const gated = all(host).find(n => n.className === 'ptree ptree-closures');
  assert.strictEqual(all(gated).filter(n => n.tag === 'button' && n.textContent === '여기서 진행').length, 0,
                     'a followup the session closed has nothing to decide');
  assert.strictEqual(notes.children.length, 1);
  // claunch-i0whx: a note offers the two optional picks, never 버림.
  const noteButtons = all(notes).filter(n => n.tag === 'button').map(n => n.textContent);
  assert.deepStrictEqual(noteButtons, ['여기서 진행', '이슈로 넘김'], 'a note offers here / issue only');
  all(notes).find(n => n.tag === 'button' && n.textContent === '이슈로 넘김').onclick(); await settle();
  assert.deepStrictEqual(posts().slice(-1)[0].body, {session: 's1', note: '', rev: 1, action: 'issue'});
  assert(text(host).includes('처분: 이슈로 넘김'), 'the note shows the pick');
  buttons(host, '보류')[0].onclick(); await settle();
  assert.deepStrictEqual(posts().slice(-1)[0].body, {session: 's1', note: '', rev: 3, verdict: 'deferred'});
  assert(text(host).includes('확인: 보류'));

  // claunch-vmcg7: the session's default pick, a ruled-out option, and the
  // nodes under an option (the chosen one open, the other faded).
  plan = {...plan, revision: 10, closure: [], steps: [
    {id: 'h1', kind: 'decision', title: 'layout', detail: '', rev: 1,
     options: [{label: 'A', effect: 'fast', risk: 'untested', recommended: true, default: true},
               {label: 'B', effect: 'safe', risk: 'slow'},
               {label: 'C', effect: 'cheap', risk: 'brittle', rejected: 'no backup path'}],
     result: {choice: 'A', by: 'agent', note: ''},
     children: [
       {id: 'h2', kind: 'decision', title: 'index on A', under: 'A', detail: '', rev: 1, result: null,
        options: OPTS, children: []},
       {id: 'h3', kind: 'decision', title: 'migrate on B', under: 'B', detail: '', rev: 1, result: null,
        options: OPTS, children: []},
       {id: 'h4', kind: 'step', title: 'write it', status: 'todo', detail: '', rev: 1, children: []},
     ]}]};
  ctx.PlanPane._view().open(); await settle();
  const h1 = all(host).find(n => n.tag === 'li' && n.className.includes('plan-decision'));
  assert.deepStrictEqual(h1.children[0].children.map(c => c.textContent), ['◆', 'h1', 'layout', '자동 선택됨']);
  const hopts = h1.children.find(c => c.className === 'ptree plan-options-tree').children;
  assert.deepStrictEqual(hopts.map(c => c.className), ['ptree-node ptree-option ptree-chosen ptree-recommended',
    'ptree-node ptree-option ptree-unchosen', 'ptree-node ptree-option ptree-unchosen ptree-rejected']);
  assert(text(hopts[2]).includes('배제 이유: no backup path') && text(hopts[2]).includes('배제'));
  assert.strictEqual(buttons(hopts[2], '이 안 선택').length, 0, 'a ruled-out option cannot be picked');
  assert.strictEqual(buttons(hopts[1], '이 안 선택').length, 1, 'the user may still change the default');
  assert(text(hopts[0]).includes('index on A') && !text(hopts[0]).includes('migrate on B'));
  assert(text(hopts[1]).includes('migrate on B') && text(hopts[1]).includes('선택되지 않은 안의 하위 항목'));
  const h3 = all(hopts[1]).find(n => n.tag === 'li' && n.className.includes('plan-decision'));
  assert(h3.className.includes('ptree-inactive'));
  assert(text(h3).includes('대기 (상위 안 미선택)'));
  assert.strictEqual(buttons(h3, '이 안 선택').length, 0, 'an inactive branch takes no choice yet');
  assert.strictEqual(input(h3, '선택에 붙일 메모 (선택 사항)').length, 0);
  // the common child hangs from the decision itself, outside every option
  assert(!hopts.some(o => text(o).includes('write it')) && text(h1).includes('write it'));
  assert(text(host).includes('자동 선택됨 · 다른 안을 고르면 세션에 전달됩니다'));

  // Closable, and a session without a plan.
  plan.summary = {...plan.summary, closable: true, blockers: [], waiting: 0};
  ctx.PlanPane._view().open(); await settle();
  assert(text(host).includes('닫아도 됨'));
  // Rounds: no row while nothing is sealed; then a row, a carried chip, and a
  // sealed round drawn without any control.
  assert.strictEqual(buttons(host, '1회차').length, 0);
  roundsDoc = {session: 's1', current: {run: 'r1', round: 2, key: 'r1/2'},
               rounds: [{run: 'r1', round: 1, key: 'r1/1', title: 'Round one'}]};
  plan.closure = [...plan.closure, {id: 'v0', kind: 'verify', title: 'carried check', detail: '', rev: 1,
                                    result: null, from: {run: 'r1', round: 1}}];
  ctx.PlanPane._view().open(); await settle();
  assert.strictEqual(buttons(host, '현재 (2회차)').length, 1);
  assert(text(host).includes('1회차에서 넘어옴'));
  const postsBefore = posts().length;
  buttons(host, '1회차')[0].onclick(); await settle();
  const sealedText = text(host);
  assert(sealedText.includes('Round one') && sealedText.includes('읽기 전용') && sealedText.includes('선택: B'),
         'the sealed round is drawn');
  for (const label of ['이 안 선택', '통과', '실패', '의견 보내기'])
    assert.strictEqual(buttons(host, label).length, 0, `a sealed round has no ${label} button`);
  assert.strictEqual(input(host, '계획에 대한 의견').length, 0);
  assert(calls.some(c => c.path.includes('round=r1%2F1')), 'the pane read the sealed round');
  assert.strictEqual(posts().length, postsBefore);
  buttons(host, '현재 (2회차)')[0].onclick(); await settle();
  assert(!text(host).includes('읽기 전용') && buttons(host, '통과').length > 0, 'back on the current plan');
  roundsDoc = null;

  plan = null;
  ctx.PlanPane._view().open(); await settle();
  assert(text(host).includes('아직 계획을 쓰지 않았습니다'));
  ctx.PlanPane.unmount();
  assert.strictEqual(host.children.length, 0);
  console.log('ok');
})().catch(error => { console.error(error); process.exit(1); });
