/* Real sim-page.js: list, create-for-a-session, detail actions, answers,
   map and settings, drafts that survive a poll redraw, stale reads. */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.textContent = ''; this.value = ''; this.className = ''; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(name, value) { this.attributes[name] = value; }
  contains(n) { return all(this).includes(n); }
  remove() {}
}
const root = new Element('div');
const calls = [];
const sim = {id: 'sim-abc123', title: '<b>door</b>', goal: 'find gaps', owner: 's1', state: 'idle', state_reason: 'created',
  project: 'p', root: '/repo', sources: ['spec.md'], counts: {open: 1, question: 1}, model_stats: {machines: 1, rules: 2},
  last_round: {round: 1, coverage: {reached: 2, cells: 4}}, runner: {session: 'sim-abc123-run1', started_at: 't0'},
  from: 'sim-old999', stale_sources: [{section: 'rules', id: 'r.lock'}],
  settings: {interpreter_profile: 'ds4', judge_profile: 'glm', interpreter_reasoning: 'low', judge_reasoning: 'low',
    seed: 1, budget: 2000, horizon: 12, max_rounds: 20}};
const findings = [
  {key: 'unhandled|door|open|open', kind: 'unhandled', status: 'open', detail: 'no rule', count: 3, first_round: 1, last_round: 1,
    witness: {steps: [{event: 'open'}, {event: 'open'}]}, assumed: [], provisional: [],
    reopened: {why: 'x'}, prev_question: {text: 'old q'}, triage: {verdict: 'spec', why: 'nothing about it'}},
  {key: 'unhandled|door|closed|close', kind: 'unhandled', status: 'question', detail: 'no rule', count: 1, first_round: 1, last_round: 1,
    witness: {steps: [{event: 'close'}]}, question: {text: 'what then?', options: ['nothing', 'error'], by: 's1'}},
];
let delay = null;
async function api(raw, opts = {}) {
  const url = new URL(raw, 'http://test');
  const method = opts.method || 'GET', body = opts.body ? JSON.parse(opts.body) : null;
  calls.push({url, method, body});
  let result, ok = true;
  const p = url.pathname;
  if (p === '/api/sessions') result = {sessions: [{name: 's1', project: 'p', status: 'idle'}, {name: 's2', project: 'p', status: 'busy'}]};
  else if (p === '/api/sims/profiles') result = {profiles: ['ds4', 'glm', 'oauth'], judges: ['ds4', 'glm'],
    models: {ds4: ['flash', 'glm-flash'], glm: ['glm-big']}, defaults: {interpreter_profile: 'ds4', judge_profile: ''}};
  else if (p === '/api/sims' && method === 'GET') { result = {sims: [sim]}; if (delay) { const d = delay; delay = null; await d; } }
  else if (p === '/api/sims' && method === 'POST') result = {sim: {...sim, id: 'sim-new111', owner: body.owner}, nudged: true};
  else if (p.endsWith('/findings')) result = {total: findings.length, findings};
  else if (p.endsWith('/replay')) result = url.searchParams.get('key') === 'unhandled|door|open|open'
    ? {stale: false, reproduces: true, checkpoint: null, start: {sm: {door: 'closed'}, vars: {}}, raised_at: 2,
       steps: [{n: 1, event: 'open', assume: {}, fired: ['r.open'], findings: [], changed: {door: ['closed', 'open']}, sm: {door: 'open'}, vars: {}},
               {n: 2, event: 'open', assume: {'r.x': false}, fired: [], findings: ['unhandled|door|open|open'], changed: {}, sm: {door: 'open'}, vars: {}}]}
    : {stale: true, why: 'rule r.gone is no longer in the model'};
  else if (p.endsWith('/model')) result = {model: {machines: [{id: 'door', states: ['closed', 'open'], initial: 'closed'}], vars: [], events: [{name: 'open'}],
    rules: [{id: 'r.open', on: {event: 'open'}, do: [], source: [{ref: 'spec.md', quote: 'opens'}]}], invariants: [], checkpoints: {}}};
  else if (p.endsWith('/coverage')) result = coverage;
  else if (p === '/api/sims/sim-abc123' && method === 'GET') result = {sim, rounds: [{round: 1, at: 'x', by: 's1', new: 2, open: 2}], journal: [], owner_live: true};
  else if (p.endsWith('/start')) result = {result: {round: 2}, sim, notified: false, runner: 'sim-abc123-run1'};
  else if (p.endsWith('/nudge')) result = {nudged: true, to: 's1'};
  else if (p.endsWith('/answer')) result = {ok: true, nudged: true};
  else if (p.endsWith('/transfer')) result = {sim};
  else if (p.endsWith('/settings')) result = {sim};
  else if (p.endsWith('/proposals')) result = {proposals: [
    {id: 'e-1234abcd', kind: 'clarify', file: 'spec.md', old: 'opens', new: 'opens once', reason: 'twice is open', status: 'proposed',
     findings: ['unhandled|door|open|open'], check: {breaks: ['r.open']}},
    {id: 'e-feedbeef', kind: 'complete', file: 'spec.md', old: '', new: 'close is ignored', reason: 'r', status: 'rejected',
     findings: ['unhandled|door|closed|close'], check: {breaks: []}, decision: {by: 'user', note: 'not now'}}]};
  else if (p.includes('/proposals/') && p.endsWith('/decide')) result = {proposal: {}, nudged: true};
  else { result = {error: 'unexpected ' + method + ' ' + p}; ok = false; }
  return {ok, status: ok ? 200 : 500, json: async () => result};
}
const loc = {hash: ''};
let focused = null;
const ctx = vm.createContext({document: {createElement: tag => new Element(tag), getElementById: () => root,
  get activeElement() { return focused; }}, api, location: loc, URLSearchParams, setInterval: () => 1, clearInterval: () => {},
  confirm: () => true, console, encodeURIComponent});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/sim-page.js'), 'utf8'), ctx);
// The map keeps static rule text and the last round's observation apart.
const K = (st, e) => `door\u0000${st}\u0000${e}`;
const cell = (rule, available, observed, finding = null) => ({rule, available, observed, finding});
let coverage = {grid: [{machine: 'door', states: ['closed', 'open'], events: ['open', 'close', 'lock']}],
  handled: [K('closed', 'open')], gaps: {[K('open', 'open')]: 'open', [K('closed', 'close')]: 'question'},
  cells: {[K('closed', 'open')]: cell('yes', 'yes', {handled: 3, unhandled: 0}),
          [K('open', 'open')]: cell('no', 'yes', {handled: 0, unhandled: 2}, 'open'),
          [K('closed', 'close')]: cell('no', 'maybe', {handled: 0, unhandled: 0}, 'question'),
          [K('open', 'close')]: cell('maybe', 'yes', {handled: 1, unhandled: 1}),
          [K('closed', 'lock')]: cell('no', 'no', {handled: 0, unhandled: 0}),
          [K('open', 'lock')]: cell('no', 'yes', {handled: 0, unhandled: 0})},
  observed_from: {round: 1, at: 't', model_hash: 'abc', current: true}, counts: {cells: 6, candidates: 5, observed: 3}};
function all(node = root) { return [node, ...node.children.flatMap(c => typeof c === 'object' ? all(c) : [])]; }
const button = label => all().find(n => n.tag === 'button' && n.textContent === label);
const labelled = label => all().find(n => n.attributes['aria-label'] === label);
async function settle() { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); }
(async () => {
  ctx.SimPage.open(); await settle();
  // The title is text, never markup.
  assert(all().some(n => n.tag === 'strong' && n.textContent === '<b>door</b>'));
  assert(!all().some(n => n.tag === 'b'));
  // Create for a session: owner and model choices go in the body.
  button('새 시뮬레이션').onclick();
  const title = all().find(n => n.tag === 'input' && n.required); title.value = 'login'; title.oninput();
  labelled('맡길 세션').value = 's2'; labelled('맡길 세션').onchange();
  // An OAuth-only profile can interpret but is not offered as a judge.
  assert(all(labelled('해석(B) 프로필')).some(n => n.tag === 'option' && n.value === 'oauth'));
  assert(!all(labelled('판정(C) 프로필')).some(n => n.tag === 'option' && n.value === 'oauth'));
  // Same profile, same model: the page warns; another model of it clears the warning.
  assert(all().some(n => n.className === 'sim-warn'));
  labelled('판정(C) 모델').value = 'glm-flash'; labelled('판정(C) 모델').onchange();
  assert(!all().some(n => n.className === 'sim-warn'));
  labelled('판정(C) 프로필').value = 'glm'; labelled('판정(C) 프로필').onchange();
  assert(all(labelled('판정(C) 모델')).some(n => n.tag === 'option' && n.value === 'glm-big'));
  // A poll while typing does not redraw over the draft.
  focused = all().find(n => n.tag === 'textarea');
  await ctx.SimPage.open(); await settle();
  focused = null;
  labelled('이어 받을 시뮬레이션').value = 'sim-abc123'; labelled('이어 받을 시뮬레이션').onchange();
  all().find(n => n.tag === 'form').onsubmit({preventDefault() {}}); await settle();
  const made = calls.find(c => c.method === 'POST' && c.url.pathname === '/api/sims');
  assert.strictEqual(made.body.from, 'sim-abc123');
  assert.strictEqual(made.body.owner, 's2');
  assert.strictEqual(made.body.title, 'login');
  assert.strictEqual(made.body.settings.judge_profile, 'glm');
  assert.strictEqual(made.body.settings.interpreter_profile, 'ds4');
  assert.strictEqual(made.body.settings.judge_model, '');  // a new judge profile resets the model choice
  assert.strictEqual(loc.hash, '#/sims/sim-new111');
  // Detail.
  ctx.SimPage.open('sim-abc123'); await settle();
  assert(button('실행') && button('소유권 해제'));
  assert(all().some(n => n.className === 'sim-triage' && n.textContent.includes('사람이 결정') && n.textContent.includes('nothing about it')));
  // Carry: where it came from, rules that lost their quote, a reopened question.
  assert(all().some(n => n.tag === 'a' && n.href === '#/sims/sim-old999'));
  assert(all().some(n => n.className === 'sim-warn' && n.textContent.includes('r.lock')));
  assert(all().some(n => n.className === 'sim-warn' && n.textContent.includes('old q')));
  // The attached run session links to its terminal.
  assert(all().some(n => n.tag === 'a' && n.href === '#/s/sim-abc123-run1'));
  button('실행').onclick(); await settle();
  assert(calls.some(c => c.method === 'POST' && c.url.pathname.endsWith('/start')));
  const say = all().find(n => n.tag === 'input' && /소유 세션에 전할 말/.test(n.placeholder || ''));
  say.value = 'check the lock'; say.oninput();
  button('소유 세션에 요청').onclick(); await settle();
  assert(calls.some(c => c.url.pathname.endsWith('/nudge') && c.body.text === 'check the lock'));
  labelled('이관할 세션').value = 's2'; labelled('이관할 세션').onchange();
  button('소유권 이관').onclick(); await settle();
  assert(calls.some(c => c.url.pathname.endsWith('/transfer') && c.body.to === 's2'));
  // Answer a question with an option.
  button('nothing').onclick();
  button('답하기').onclick(); await settle();
  assert(calls.some(c => c.url.pathname.endsWith('/answer') && c.body.text === 'nothing' && c.body.key === 'unhandled|door|closed|close'));
  // Replay a witness: start state, one row per step, the raising step marked.
  ctx.SimPage.open('sim-abc123'); await settle();
  button('재생').onclick(); await settle();
  assert(calls.some(c => c.url.pathname.endsWith('/replay') && c.url.searchParams.get('key') === 'unhandled|door|open|open'));
  assert(all().some(n => n.tag === 'li' && n.className === 'sim-step sim-step-raised'));
  assert(all().some(n => n.textContent === ' door: closed → open'));
  assert(all().some(n => n.textContent === '가정: r.x=거짓'));
  assert(button('재생 닫기'));
  // A stale witness says why instead of showing steps.
  all().filter(n => n.tag === 'button' && n.textContent === '재생')[0].onclick(); await settle();
  assert(all().some(n => n.className === 'sim-warn' && n.textContent.includes('r.gone')));
  button('재생 닫기').onclick(); await settle();
  // Map and model tabs.
  ctx.SimPage.open('sim-abc123', 'map'); await settle();
  const td = () => all().filter(n => n.tag === 'td');
  const at = (i) => td()[i];   // row-major: closed×open, closed×close, closed×lock, open×open, ...
  assert.strictEqual(at(0).className, 'sim-cell-ok sim-rule-yes');
  assert.strictEqual(at(1).className, 'sim-cell-ask');
  assert.strictEqual(at(2).className, 'sim-cell-na');
  assert.strictEqual(at(2).textContent, '×');
  assert.strictEqual(at(3).className, 'sim-cell-gap');
  assert.strictEqual(at(3).textContent, '!');
  assert.strictEqual(at(4).className, 'sim-cell-mixed sim-rule-maybe');
  assert.strictEqual(at(5).className, 'sim-cell-unseen');
  assert(at(0).title.includes('규칙 있음') && at(0).title.includes('처리 3'));
  assert(at(1).title.includes('사건 조건부'));
  assert(all().some(n => n.textContent === '전체 6칸 · 가용 후보(조건부 포함) 5칸 · 마지막 라운드 관측 3칸'));
  assert(all().some(n => n.textContent.includes('전체 실행에서 방문하지 않았다는 뜻은 아님')));
  assert(!all().some(n => n.textContent.includes('아직 도달 안 함')));
  // An observation made on an older model is not shown as evidence.
  const stale = (current) => {
    coverage = {...coverage, observed_from: current === null ? null : {...coverage.observed_from, current},
      cells: Object.fromEntries(Object.entries(coverage.cells).map(([k, v]) => [k, {...v, observed: null}]))};
  };
  stale(false); ctx.SimPage.open('sim-abc123', 'overview'); await settle(); ctx.SimPage.open('sim-abc123', 'map'); await settle();
  assert(all().some(n => n.className === 'sim-warn' && n.textContent.includes('모델이 바뀌기 전')));
  assert.strictEqual(at(0).className, 'sim-cell-nodata sim-rule-yes');
  assert(at(0).title.includes('관측 자료 없음'));
  stale(null); ctx.SimPage.open('sim-abc123', 'overview'); await settle(); ctx.SimPage.open('sim-abc123', 'map'); await settle();
  assert(all().some(n => n.className === 'sim-warn' && n.textContent.startsWith('관측 자료 없음')));
  assert.strictEqual(at(3).className, 'sim-cell-gap');   // the ledger still marks a raised finding
  ctx.SimPage.open('sim-abc123', 'model'); await settle();
  assert(all().some(n => n.className === 'sim-source' && n.textContent.includes('spec.md')));
  // Edit proposals: a diff, the quotes it would break, decide only while proposed.
  sim.proposals = {proposed: 1, accepted: 0, rejected: 1};
  ctx.SimPage.open('sim-abc123', 'proposals'); await settle();
  assert(button('편집 제안 (1)'));
  assert(all().some(n => n.className === 'sim-del' && n.textContent === 'opens'));
  assert(all().some(n => n.className === 'sim-add' && n.textContent === 'opens once'));
  assert(all().some(n => n.className === 'sim-warn' && n.textContent.includes('r.open')));
  assert(all().some(n => n.textContent === '파일 끝에 추가'));
  assert(all().some(n => n.textContent === '메모: not now'));
  assert.strictEqual(all().filter(n => n.tag === 'button' && n.textContent === '수락').length, 1);
  const memo = all().find(n => n.tag === 'input' && n.placeholder === '메모 (선택)'); memo.value = 'ok'; memo.oninput();
  button('수락').onclick(); await settle();
  const decided = calls.find(c => c.url.pathname.endsWith('/proposals/e-1234abcd/decide'));
  assert.deepStrictEqual(decided.body, {accept: true, note: 'ok'});
  delete sim.proposals;
  // Settings save the chosen profiles.
  ctx.SimPage.open('sim-abc123', 'settings'); await settle();
  labelled('해석(B) reasoning').value = 'high'; labelled('해석(B) reasoning').onchange();
  labelled('판정자 분류').checked = false; labelled('판정자 분류').onchange();
  assert.strictEqual(labelled('언어').value, '한국어');
  all().find(n => n.tag === 'form').onsubmit({preventDefault() {}}); await settle();
  const saved = calls.find(c => c.url.pathname.endsWith('/settings'));
  assert.strictEqual(saved.body.settings.interpreter_reasoning, 'high');
  assert.strictEqual(saved.body.settings.triage, false);
  assert.strictEqual(saved.body.settings.max_edits, 0);
  assert.strictEqual(saved.body.settings.language, '한국어');
  assert.deepStrictEqual(saved.body.sources, ['spec.md']);
  // The terminal's pane: one session's simulations, moving without the hash.
  ctx.SimPage.stop();
  const pane = new Element('div');
  loc.hash = '#/s/s1';
  ctx.SimPane.mount(pane, 's1'); await settle();
  const inPane = () => all(pane);
  assert(calls.some(c => c.url.pathname === '/api/sims' && c.url.searchParams.get('owner') === 's1'));
  assert(inPane().some(n => n.tag === 'h2' && n.textContent === 's1의 시뮬레이션'));
  const row = inPane().find(n => n.tag === 'li' && n.children.some(k => k.textContent === '<b>door</b>'));
  row.onclick(); await settle();
  assert.strictEqual(loc.hash, '#/s/s1');
  assert(inPane().some(n => n.tag === 'a' && n.className === 'sim-full' && n.href === '#/sims/sim-abc123/findings'));
  const tabBtn = inPane().find(n => n.tag === 'button' && n.textContent.startsWith('편집 제안'));
  tabBtn.onclick(); await settle();
  assert.strictEqual(loc.hash, '#/s/s1');
  assert(inPane().some(n => n.className === 'sim-add'));
  inPane().find(n => n.tag === 'a' && n.className === 'sim-back').onclick({preventDefault() {}}); await settle();
  assert(inPane().some(n => n.tag === 'h2' && n.textContent === 's1의 시뮬레이션'));
  // A new simulation from the pane is owned by that session, without a picker.
  inPane().find(n => n.tag === 'button' && n.textContent === '새 시뮬레이션').onclick();
  assert(!inPane().some(n => n.attributes['aria-label'] === '맡길 세션'));
  const t2 = inPane().find(n => n.tag === 'input' && n.required); t2.value = 'pane sim'; t2.oninput();
  inPane().find(n => n.tag === 'form').onsubmit({preventDefault() {}}); await settle();
  const made2 = calls.filter(c => c.url.pathname === '/api/sims' && c.method === 'POST').pop();
  assert.strictEqual(made2.body.owner, 's1');
  assert.strictEqual(loc.hash, '#/s/s1');
  ctx.SimPane.unmount();
  // Leaving the route invalidates an outstanding read.
  ctx.SimPage.open(); await settle();
  let release; delay = new Promise(resolve => { release = resolve; });
  button('새로고침').onclick(); ctx.SimPage.stop();
  const before = root.children;
  release(); await settle(); assert.strictEqual(root.children, before);
  console.log('ok');
})().catch(error => { console.error(error); process.exit(1); });
