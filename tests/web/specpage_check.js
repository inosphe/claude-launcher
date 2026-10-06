/* Real page module (spec-page.js): paste → check errors, check → leader
   choice → plan table, import confirm and result, a stop that resumes, and
   an answer that arrives after the route was left. */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.textContent = ''; this.value = ''; this.disabled = false; }
  appendChild(child) { this.children.push(child); return child; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(name, value) { this.attributes[name] = value; }
}
const root = new Element('div');
const calls = [];
let answers = {}, delay = null, confirmed = true;
async function api(raw, opts = {}) {
  const verb = raw.split('/').pop();
  const body = JSON.parse(opts.body);
  calls.push({verb, body});
  if (delay) { const d = delay; delay = null; await d; }
  const [status, result] = answers[verb](body);
  return {ok: status < 300, status, json: async () => result};
}
const ctx = vm.createContext({document: {createElement: tag => new Element(tag), getElementById: () => root},
  api, confirm: () => confirmed, console});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/spec-page.js'), 'utf8'), ctx);
const all = (node = root) => [node, ...node.children.flatMap(c => typeof c === 'object' ? all(c) : [])];
const button = label => all().find(n => n.tag === 'button' && n.textContent === label);
const labelled = label => all().find(n => n.attributes['aria-label'] === label);
const texts = tag => all().filter(n => n.tag === tag).map(n => n.textContent);
async function settle() { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); }
const SPEC = {name: 'night-sense', title: '', mesh: 'spec-night-sense', mesh_mode: 'new',
  leader_mode: 'ask', candidates: ['s29'], workspace: '', agents: 4, issues: 4};
(async () => {
  ctx.SpecPage.open();
  assert(button('검증').disabled, 'nothing to check yet');
  // Paste a broken spec: the validator's lines are shown, nothing else.
  answers.check = () => [400, {error: '2 problem(s) in the spec', errors: ['agents[1].kind: must be a string', '<b>x</b>']}];
  const area = labelled('명세 YAML');
  area.value = 'spec: 1\n'; area.oninput();
  button('검증').onclick(); await settle();
  assert.deepStrictEqual(texts('li'), ['agents[1].kind: must be a string', '<b>x</b>']);
  assert(!all().some(n => n.tag === 'b'), 'error text is text, not markup');
  assert(!button('plan 미리보기'));
  // A valid spec: summary, leader choice (ask → nothing chosen), plan.
  answers.check = () => [200, {errors: [], spec: SPEC}];
  labelled('명세 YAML').value = 'spec: 1\nname: night-sense\n'; labelled('명세 YAML').oninput();
  button('검증').onclick(); await settle();
  assert(all().some(n => n.tag === 'p' && n.textContent.startsWith('night-sense · 메시 spec-night-sense')));
  assert.strictEqual(ctx.SpecPage.leaderChoice(), '');
  assert(button('import 실행').disabled, 'import waits for a plan');
  answers.plan = body => body.leader ? [200, {actions: [{do: 'mesh-create'}, {do: 'mesh-join'}],
    lines: ['mesh-create  mesh=spec-night-sense', 'mesh-join    session=s29']}]
    : [400, {error: 'this spec leaves the leader to the import: pass own or shared:<session>', leader_error: true}];
  button('plan 미리보기').onclick(); await settle();
  assert(texts('p').some(t => t.includes('pass own or shared')), 'the leader error sits by the choice');
  labelled('리더').value = 'shared'; labelled('리더').onchange();
  assert.strictEqual(labelled('리더 세션').value, 's29', 'the single candidate is prefilled');
  assert.strictEqual(ctx.SpecPage.leaderChoice(), 'shared:s29');
  button('plan 미리보기').onclick(); await settle();
  assert.strictEqual(calls.at(-1).body.leader, 'shared:s29');
  assert(texts('td').includes('mesh-join    session=s29'));
  assert(texts('h3').includes('plan — action 2개'));
  // own: a profile field, sent with the import.
  labelled('리더').value = 'own'; labelled('리더').onchange();
  assert(!labelled('리더 세션') && labelled('프로필'));
  labelled('프로필').value = 'nc'; labelled('프로필').oninput();
  button('plan 미리보기').onclick(); await settle();
  // Declined confirmation: no request.
  confirmed = false; const before = calls.length;
  button('import 실행').onclick(); await settle();
  assert.strictEqual(calls.length, before);
  // A stop: the log is shown and the page says a rerun continues.
  confirmed = true;
  answers.import = () => [409, {error: 'br create failed', resumable: true,
    log: [{do: 'issue', what: 'frame', result: 'created', detail: 't-1'}]}];
  button('import 실행').onclick(); await settle();
  assert.deepStrictEqual(calls.at(-1).body, {yaml: 'spec: 1\nname: night-sense\n', leader: 'own', profile: 'nc'});
  assert(texts('p').includes('br create failed'));
  assert(texts('p').some(t => t.includes('이어서 진행')));
  assert(texts('td').includes('t-1'));
  answers.import = () => [200, {ok: true, log: [{do: 'spawn', what: 'ue1', result: 'created', detail: 's-ue1'}]}];
  button('import 실행').onclick(); await settle();
  assert(texts('p').includes('import 완료 — 1개 action'));
  // Editing the text drops everything that described the old text.
  labelled('명세 YAML').value = 'spec: 1\nname: other\n'; labelled('명세 YAML').oninput();
  assert(!button('plan 미리보기') && !texts('td').length);
  // Leaving the route invalidates an outstanding answer.
  let release; delay = new Promise(resolve => { release = resolve; });
  button('검증').onclick(); ctx.SpecPage.stop();
  const frozen = root.children;
  release(); await settle(); assert.strictEqual(root.children, frozen);
  console.log('ok');
})().catch(error => { console.error(error); process.exit(1); });
