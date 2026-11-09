/* Real page module (fleet-page.js) on a fake DOM: the preset list loads,
   a project fills its workspace and mesh, the opening task expands the
   tree through the daemon, per-level and per-member profiles are sent,
   launch pins the previewed name and prefix, "Spec에서 열기" hands the
   spec to SpecPage, the editor adds a group, checks it and saves the
   document, and an answer that arrives after the route was left is
   dropped. */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.textContent = ''; this.value = ''; this.disabled = false; this.style = {}; }
  appendChild(child) { this.children.push(child); return child; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name]; }
}
const root = new Element('div');
const calls = [];
let delay = null, confirmed = true, timers = [];
const PROFILES = ['sr', 'ds4', 'glm-flash'];
const GROUPS = [
  {id: 'coord', label: '코디네이터', kind: 'coordinator', depth: 0, count: 1},
  {id: 'lead', label: '리드', kind: 'sub-lead', depth: 1, count: 2},
  {id: 'w', label: '워커', kind: 'worker', depth: 2, count: 4}];
const row = (handle, kind, depth, extra = {}) => ({handle, id: handle, kind, depth, workflow: 'improv-worker-v2', issue: 'pending', ...extra});
const SHAPE = [row('coord', 'coordinator', 0, {workflow: 'improv-coordinator-v2', issue: 'lane'}),
  row('lead1', 'sub-lead', 1, {parent: 'coord', relation: 'independent', issue: 'lane'}),
  row('lead1-w1', 'worker', 2, {parent: 'lead1', relation: 'stacked'})];
const PRESET = {name: 'coordinator-leads-workers', title: '코디네이터 · 리드 · 워커', source: 'builtin', errors: [],
  params: {leads: {label: '리드 수', default: 2, min: 1, max: 6}, workers: {label: '리드당 워커 수', default: 2, min: 0, max: 6}},
  groups: GROUPS, shape: SHAPE, yaml: 'fleet: 1\n'};
const DOC = {fleet: 1, name: PRESET.name, title: PRESET.title, params: PRESET.params,
  root: {id: 'coord', kind: 'coordinator', children: [{id: 'lead', kind: 'sub-lead', count: 'leads', issue: 'lane', children: []}]}};
const answers = {
  'GET /api/fleet/presets': () => [200, {presets: [PRESET], default: PRESET.name, kinds: ['coordinator', 'sub-lead', 'worker', 'reviewer'],
    issue_modes: ['lane', 'pending', 'none'], max_count: 12, tiers: ['economy'], dir: '/h/fleet-presets'}],
  'GET /api/profiles': () => [200, {profiles: PROFILES}],
  'GET /api/projects': () => [200, {projects: [{name: 'claunch', default_workspace: 'claunch-ws', default_mesh: 'claunch'}]}],
  'GET /api/workspaces': () => [200, {workspaces: [{name: 'claunch-ws'}, {name: 'other'}]}],
  'GET /api/mesh?view=rail': () => [200, {meshes: [{name: 'claunch'}]}],
  'POST /api/fleet/check': body => [200, {doc: body.doc || DOC, yaml: 'fleet: 1\nname: x\n', errors: [], groups: GROUPS, shape: SHAPE}],
  'POST /api/fleet/expand': body => [200, {yaml: 'spec: 1\n', name: 'fleet-coordinator-leads-workers-11052100', prefix: 'f11052100',
    mesh: body.launch.mesh, mesh_mode: 'attach', board_dir: '/w/claunch', board_error: '',
    agents: SHAPE.map(r => ({...r, handle: `f11052100-${r.handle}`, profile: body.launch.profiles[r.id] || 'sr'})),
    preview: {tasks: [{key: 'root', type: 'epic'}]}, lines: ['spawn coord', 'spawn lead1']}],
  'POST /api/fleet/launch': () => [200, {ok: true, log: [{do: 'spawn', what: 'coord', result: 'created', detail: 's-1'}], mesh: 'claunch'}],
};
async function api(raw, opts = {}) {
  const key = `${opts.method || 'GET'} ${raw}`;
  const body = opts.body ? JSON.parse(opts.body) : undefined;
  calls.push({key, body});
  if (delay) { const d = delay; delay = null; await d; }
  const answer = answers[key] || (key.startsWith('PUT /api/fleet/presets/') ? () => [200, {ok: true, preset: {}}] : null);
  assert(answer, `unexpected call ${key}`);
  const [status, result] = answer(body);
  return {ok: status < 300, status, json: async () => result};
}
const handed = [];
const ctx = vm.createContext({
  document: {createElement: tag => new Element(tag), getElementById: () => root},
  api, confirm: () => confirmed, console,
  setTimeout: fn => { timers.push(fn); return timers.length; }, clearTimeout: () => {},
  SpecPage: {load: (text, opts) => handed.push({text, opts})}, location: {hash: '#/fleet'},
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/fleet-page.js'), 'utf8'), ctx);
const all = (node = root) => [node, ...node.children.flatMap(c => typeof c === 'object' ? all(c) : [])];
const button = label => all().find(n => n.tag === 'button' && n.textContent === label);
const labelled = label => all().find(n => n.attributes['aria-label'] === label);
const texts = tag => all().filter(n => n.tag === tag).map(n => n.textContent);
async function settle() { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); }
async function tick() { const due = timers; timers = []; due.forEach(fn => fn()); await settle(); }
(async () => {
  ctx.FleetPage.open();
  await settle();
  assert(labelled('프리셋 coordinator-leads-workers'), 'the built-in is a card');
  const {L} = ctx.FleetPage._state();
  assert.strictEqual(L.preset, 'coordinator-leads-workers', 'the default preset is picked');
  assert.deepStrictEqual({...L.params}, {leads: 2, workers: 2}, 'its params start at their defaults');
  // The preset's shape is drawn before there is a task; no launch yet.
  assert(texts('strong').includes('lead1-w1'));
  assert(button('Fleet 시작').disabled && button('Spec에서 열기').disabled);
  await tick();
  assert.strictEqual(calls.at(-1).key, 'POST /api/fleet/check', 'no task: the counts are checked, not expanded');
  // A project fills its default workspace and mesh; the mesh is known: attach.
  labelled('project').value = 'claunch'; labelled('project').onchange();
  assert.strictEqual(labelled('workspace').value, 'claunch-ws');
  assert.strictEqual(labelled('mesh').value, 'claunch');
  assert(texts('span').some(t => t.startsWith('기존 메시에 합류')));
  // Profiles: the root's default, one level, and -- per member -- one card.
  labelled('기본 프로필').value = 'sr'; labelled('기본 프로필').onchange();
  labelled('lead 레벨 프로필').value = 'ds4'; labelled('lead 레벨 프로필').onchange();
  labelled('리드 수').value = '1'; labelled('리드 수').oninput();
  labelled('오프닝 태스크').value = '로그인 개편\n세부'; labelled('오프닝 태스크').oninput();
  await tick();
  const expand = calls.filter(c => c.key === 'POST /api/fleet/expand').at(-1);
  assert(expand, 'a task expands through the daemon');
  assert.deepStrictEqual(JSON.parse(JSON.stringify(expand.body.launch)), {task: '로그인 개편\n세부', project: 'claunch', workspace: 'claunch-ws',
    mesh: 'claunch', params: {leads: 1, workers: 2}, profiles: {lead: 'ds4'}, default_profile: 'sr'});
  assert(texts('strong').includes('f11052100-lead1'), 'the expansion draws the prefixed handles');
  assert(texts('div').includes('메시 claunch (합류) · 핸들 접두사 f11052100'));
  assert(!button('Fleet 시작').disabled);
  labelled('멤버별 프로필').checked = true; labelled('멤버별 프로필').onchange();
  const pick = labelled('f11052100-lead1-w1 프로필');
  assert(pick, 'per-member profile pickers appear on the cards');
  pick.value = 'glm-flash'; pick.onchange(); await tick();
  assert.strictEqual(calls.filter(c => c.key === 'POST /api/fleet/expand').at(-1).body.launch.profiles['lead1-w1'], 'glm-flash');
  // Declined confirmation: nothing is sent.
  confirmed = false; let before = calls.length;
  button('Fleet 시작').onclick(); await settle();
  assert.strictEqual(calls.length, before);
  // Launch pins the previewed name and prefix.
  confirmed = true;
  button('Fleet 시작').onclick(); await settle();
  const launched = calls.at(-1);
  assert.strictEqual(launched.key, 'POST /api/fleet/launch');
  assert.strictEqual(launched.body.launch.name, 'fleet-coordinator-leads-workers-11052100');
  assert.strictEqual(launched.body.launch.prefix, 'f11052100');
  assert(texts('p').some(t => t.startsWith('fleet 시작됨 — action 1개')));
  assert(texts('td').includes('s-1'));
  // A stop mid-way keeps the log and says a rerun continues.
  answers['POST /api/fleet/launch'] = () => [409, {error: 'br create failed', resumable: true, log: [], name: 'n1', prefix: 'p1'}];
  button('Fleet 시작').onclick(); await settle();
  assert(texts('pre').includes('br create failed'));
  assert(texts('p').some(t => t.includes('이어서 진행')));
  assert.strictEqual(L.prefix, 'p1');
  // The expansion opens in the Spec page.
  button('Spec에서 열기').onclick();
  assert.deepStrictEqual(JSON.parse(JSON.stringify(handed)), [{text: 'spec: 1\n', opts: {workspace: 'claunch-ws', profile: 'sr'}}]);
  assert.strictEqual(ctx.location.hash, '#/spec');
  // The editor: add a group under the lead, check, save the document.
  button('Presets').onclick();
  await settle();
  assert(labelled('그룹 coord') && labelled('그룹 lead'));
  labelled('그룹 lead').onclick();
  assert.strictEqual(labelled('그룹 id').value, 'lead');
  button('+ 하위 그룹').onclick();
  const {E} = ctx.FleetPage._state();
  assert.strictEqual(E.selected, 'w1', 'the new group is selected');
  assert(E.dirty && texts('h3').includes('coordinator-leads-workers · 저장 안 됨'));
  await tick();
  const checked = calls.at(-1);
  assert.strictEqual(checked.key, 'POST /api/fleet/check');
  assert.strictEqual(checked.body.doc.root.children[0].children[0].kind, 'worker');
  assert(!button('저장').disabled);
  button('저장').onclick(); await settle();
  const saved = calls.find(c => c.key.startsWith('PUT '));
  assert.strictEqual(saved.key, 'PUT /api/fleet/presets/coordinator-leads-workers');
  assert.strictEqual(saved.body.doc.root.children[0].children[0].id, 'w1');
  assert(texts('p').some(t => t.startsWith('저장됨 — coordinator-leads-workers')));
  // Errors from the check are listed as text and block the save.
  answers['POST /api/fleet/check'] = body => [200, {doc: body.doc, yaml: '', errors: ['root: <b>x</b>']}];
  labelled('프리셋 제목').value = '새 제목'; labelled('프리셋 제목').oninput(); await tick();
  assert(texts('li').includes('root: <b>x</b>') && !all().some(n => n.tag === 'b'));
  assert(button('저장').disabled);
  // Leaving the route invalidates an outstanding answer.
  let release; delay = new Promise(resolve => { release = resolve; });
  labelled('프리셋 제목').oninput(); timers.forEach(fn => fn()); timers = [];
  ctx.FleetPage.stop();
  const frozen = root.children;
  release(); await settle(); assert.strictEqual(root.children, frozen);
  console.log('ok');
})().catch(error => { console.error(error); process.exit(1); });
