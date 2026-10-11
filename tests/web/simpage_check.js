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
}
const root = new Element('div');
const calls = [];
const sim = {id: 'sim-abc123', title: '<b>door</b>', goal: 'find gaps', owner: 's1', state: 'idle', state_reason: 'created',
  project: 'p', root: '/repo', sources: ['spec.md'], counts: {open: 1, question: 1}, model_stats: {machines: 1, rules: 2},
  last_round: {round: 1, coverage: {reached: 2, cells: 4}},
  settings: {interpreter_profile: 'ds4', judge_profile: 'glm', interpreter_reasoning: 'low', judge_reasoning: 'low',
    seed: 1, budget: 2000, horizon: 12, max_rounds: 20}};
const findings = [
  {key: 'unhandled|door|open|open', kind: 'unhandled', status: 'open', detail: 'no rule', count: 3, first_round: 1, last_round: 1,
    witness: {steps: [{event: 'open'}, {event: 'open'}]}, assumed: [], provisional: []},
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
  else if (p === '/api/sims/profiles') result = {profiles: ['ds4', 'glm'], defaults: {interpreter_profile: 'ds4', judge_profile: ''}};
  else if (p === '/api/sims' && method === 'GET') { result = {sims: [sim]}; if (delay) { const d = delay; delay = null; await d; } }
  else if (p === '/api/sims' && method === 'POST') result = {sim: {...sim, id: 'sim-new111', owner: body.owner}, nudged: true};
  else if (p.endsWith('/findings')) result = {total: findings.length, findings};
  else if (p.endsWith('/model')) result = {model: {machines: [{id: 'door', states: ['closed', 'open'], initial: 'closed'}], vars: [], events: [{name: 'open'}],
    rules: [{id: 'r.open', on: {event: 'open'}, do: [], source: [{ref: 'spec.md', quote: 'opens'}]}], invariants: [], checkpoints: {}}};
  else if (p.endsWith('/coverage')) result = {grid: [{machine: 'door', states: ['closed', 'open'], events: ['open', 'close']}],
    handled: ['door\u0000closed\u0000open'], gaps: {'door\u0000open\u0000open': 'open', 'door\u0000closed\u0000close': 'question'}};
  else if (p === '/api/sims/sim-abc123' && method === 'GET') result = {sim, rounds: [{round: 1, at: 'x', by: 's1', new: 2, open: 2}], journal: [], owner_live: true};
  else if (p.endsWith('/start')) result = {result: {round: 2}, sim, notified: false};
  else if (p.endsWith('/nudge')) result = {nudged: true, to: 's1'};
  else if (p.endsWith('/answer')) result = {ok: true, nudged: true};
  else if (p.endsWith('/transfer')) result = {sim};
  else if (p.endsWith('/settings')) result = {sim};
  else { result = {error: 'unexpected ' + method + ' ' + p}; ok = false; }
  return {ok, status: ok ? 200 : 500, json: async () => result};
}
const loc = {hash: ''};
let focused = null;
const ctx = vm.createContext({document: {createElement: tag => new Element(tag), getElementById: () => root,
  get activeElement() { return focused; }}, api, location: loc, URLSearchParams, setInterval: () => 1, clearInterval: () => {},
  confirm: () => true, console, encodeURIComponent});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/sim-page.js'), 'utf8'), ctx);
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
  labelled('판정(C) 프로필').value = 'glm'; labelled('판정(C) 프로필').onchange();
  // A poll while typing does not redraw over the draft.
  focused = all().find(n => n.tag === 'textarea');
  await ctx.SimPage.open(); await settle();
  focused = null;
  all().find(n => n.tag === 'form').onsubmit({preventDefault() {}}); await settle();
  const made = calls.find(c => c.method === 'POST' && c.url.pathname === '/api/sims');
  assert.strictEqual(made.body.owner, 's2');
  assert.strictEqual(made.body.title, 'login');
  assert.strictEqual(made.body.settings.judge_profile, 'glm');
  assert.strictEqual(made.body.settings.interpreter_profile, 'ds4');
  assert.strictEqual(loc.hash, '#/sims/sim-new111');
  // Detail.
  ctx.SimPage.open('sim-abc123'); await settle();
  assert(button('실행') && button('소유권 해제'));
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
  // Map and model tabs.
  ctx.SimPage.open('sim-abc123', 'map'); await settle();
  assert(all().some(n => n.className === 'sim-cell-gap' && n.textContent === '!'));
  assert(all().some(n => n.className === 'sim-cell-ok'));
  assert(all().some(n => n.className === 'sim-cell-ask'));
  ctx.SimPage.open('sim-abc123', 'model'); await settle();
  assert(all().some(n => n.className === 'sim-source' && n.textContent.includes('spec.md')));
  // Settings save the chosen profiles.
  ctx.SimPage.open('sim-abc123', 'settings'); await settle();
  labelled('해석(B) reasoning').value = 'high'; labelled('해석(B) reasoning').onchange();
  all().find(n => n.tag === 'form').onsubmit({preventDefault() {}}); await settle();
  const saved = calls.find(c => c.url.pathname.endsWith('/settings'));
  assert.strictEqual(saved.body.settings.interpreter_reasoning, 'high');
  assert.deepStrictEqual(saved.body.sources, ['spec.md']);
  // Leaving the route invalidates an outstanding read.
  ctx.SimPage.open(); await settle();
  let release; delay = new Promise(resolve => { release = resolve; });
  button('새로고침').onclick(); ctx.SimPage.stop();
  const before = root.children;
  release(); await settle(); assert.strictEqual(root.children, before);
  console.log('ok');
})().catch(error => { console.error(error); process.exit(1); });
