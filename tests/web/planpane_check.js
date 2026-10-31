/* Real plan-pane.js (claunch-7jzp8): the step tree and its decision cards
   (effect, risk, recommendation), the closure section and the closable line,
   the decide bodies naming the node's rev and the note, a 409 reloading the
   reworded node, and the feedback box. The joint view (claunch-7jzp8.1):
   the toggle, the related sessions' lines, and a nested pane deciding on
   the other session's plan. The plan is drawn as a tree in #/plan's shape
   (claunch-7jzp8.5.2): step branches with a status mark, a decision forking
   into option branches (recommended, then chosen and unchosen), its own
   steps under it, the closure items as branches of their heading. */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.textContent = ''; this.value = ''; this.parent = null; }
  append(...items) { for (const i of items) { if (typeof i === 'object') i.parent = this; this.children.push(i); } }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  setAttribute(name, value) { this.attributes[name] = value; }
  contains(other) { for (let n = other; n; n = n.parent) if (n === this) return true; return false; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null; }
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

async function request(raw, method = 'GET', body) {
  calls.push({path: raw, method, body});
  const url = new URL(raw, 'http://test');
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

const ctx = vm.createContext({
  document: {createElement: tag => new Element(tag)},
  api: async () => { throw new Error('the pane must use the injected request'); },
  URL, console, JSON, Object,
  setTimeout: () => 0, clearTimeout: () => {},
});
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
  assert.deepStrictEqual(d1.children[0].children.map(c => c.textContent), ['◆', 'storage', '결정 필요']);
  const optionClasses = () => all(host).find(n => n.tag === 'li' && n.className.includes('plan-decision'))
    .children.find(c => c.className === 'ptree plan-options-tree').children.map(c => c.className);
  assert.deepStrictEqual(optionClasses(), ['ptree-node ptree-option ptree-recommended', 'ptree-node ptree-option']);
  const sub = d1.children.find(c => c.className === 'ptree ptree-steps');
  assert.strictEqual(sub.children[0].className, 'ptree-node ptree-step ptree-st-doing plan-item plan-step');
  assert.deepStrictEqual(sub.children[0].children[0].children.map(c => c.textContent), ['◐', 'write store', '진행 중']);
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

  // Closable, and a session without a plan.
  plan.summary = {...plan.summary, closable: true, blockers: [], waiting: 0};
  ctx.PlanPane._view().open(); await settle();
  assert(text(host).includes('닫아도 됨'));
  plan = null;
  ctx.PlanPane._view().open(); await settle();
  assert(text(host).includes('아직 계획을 쓰지 않았습니다'));
  ctx.PlanPane.unmount();
  assert.strictEqual(host.children.length, 0);
  console.log('ok');
})().catch(error => { console.error(error); process.exit(1); });
