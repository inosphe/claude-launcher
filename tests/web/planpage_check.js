/* Real plan-page.js (claunch-7jzp8.2): the overview grouped by stage, the
   stage and project filters, the joint grouping read only when asked for,
   "ended sessions too" asking with all=1, and the session links. */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.textContent = ''; this.value = ''; this.parent = null; }
  append(...items) { for (const i of items) { if (typeof i === 'object') i.parent = this; this.children.push(i); } }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  setAttribute(name, value) { this.attributes[name] = value; }
}

const calls = [];
const OVERVIEW = {
  counts: {user: 1, blocked: 0, closable: 1, working: 1, idle: 0},
  rows: [
    {session: 's2', stage: 'user', project: 'claunch', status: 'busy', category: 'running',
     plan: {title: 'Child', steps: 3, done: 1, current: 'write', waiting: 2}, run: {workflow: 'improv-worker-v2', round: 1, current: ['work'], owed: ['choose: land?'], status: 'waiting_approval'}},
    {session: 's3', stage: 'closable', project: 'other', status: 'idle', category: 'running',
     plan: {title: 'Done', steps: 2, done: 2, closable: true}, run: null},
    {session: 's1', stage: 'working', project: 'claunch', status: 'busy', category: 'running',
     plan: null, run: {workflow: 'improv-leader-v2', current: ['standby'], owed: [], status: 'running'}},
  ],
};
const JOINT = {groups: [{root: 's1', sessions: 1, waiting: 2, members: [
  {session: 's1', depth: 0, has_plan: false}, {session: 's2', depth: 1, has_plan: true}]}]};

async function request(raw) {
  calls.push(raw);
  const url = new URL(raw, 'http://test');
  if (url.pathname === '/api/plan/overview') return OVERVIEW;
  if (url.pathname === '/api/plan/joint') return JOINT;
  throw new Error('unexpected ' + raw);
}

const host = new Element('div');
const ctx = vm.createContext({
  document: {createElement: tag => new Element(tag), getElementById: id => (id === 'plan-view' ? host : null)},
  api: async () => { throw new Error('the page must use the injected request'); },
  URL, console, JSON, Object, Promise, Set,
  setTimeout: () => 0, clearTimeout: () => {},
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/plan-page.js'), 'utf8'), ctx);

const all = (n) => [n, ...n.children.flatMap(c => typeof c === 'object' ? all(c) : [])];
const text = () => all(host).map(n => n.textContent).join('\n');
const buttons = (label) => all(host).filter(n => n.tag === 'button' && n.textContent.startsWith(label));
const rows = () => all(host).filter(n => n.tag === 'a' && n.className === 'plan-page-session').map(n => n.textContent);
async function settle() { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); }

(async () => {
  ctx.PlanPage.open({request}); await settle();
  assert.deepStrictEqual(calls, ['/api/plan/overview']);
  const t = text();
  for (const want of ['세션 진행 개요', '내 차례 (1)', '닫아도 됨 (1)', '진행 중 (1)',
                      'Child — 단계 1/3 · 지금: write · 내 결정 대기 2건', '런이 기다림: choose: land?',
                      'improv-worker-v2 r1 · work · waiting_approval', 'improv-leader-v2 · standby'])
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
  pick.value = ''; pick.onchange(); await settle();

  // joint grouping: read only now; the plan-less parent's row sits first, s3 falls to the rest
  buttons('연관 세션별')[0].onclick(); await settle();
  assert(calls.includes('/api/plan/joint'));
  assert(text().includes('s1 · 세션 2개 · 내 결정 대기 2건') && text().includes('계획 없는 세션 (1)'));
  assert.deepStrictEqual(rows(), ['s1', 's2', 's3']);

  // ended sessions too
  buttons('끝난 세션 포함')[0].onclick(); await settle();
  assert(calls.includes('/api/plan/overview?all=1'));
  ctx.PlanPage.stop();
  console.log('ok');
})().catch(error => { console.error(error); process.exit(1); });
