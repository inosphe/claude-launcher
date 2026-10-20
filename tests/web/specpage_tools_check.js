/* Real page module (spec-page.js), the editing tools: a pause in typing
   checks by itself, each error with a known line gets a button that selects
   that line in the editor, the editor survives redraws (its caret with it),
   a valid spec previews its sessions and work before any leader is chosen,
   the template button fills the editor, and an answer about old text is
   dropped. */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.textContent = ''; this.value = ''; this.disabled = false; this.selection = null; this.focused = false; }
  appendChild(child) { this.children.push(child); return child; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(name, value) { this.attributes[name] = value; }
  focus() { this.focused = true; }
  setSelectionRange(start, end) { this.selection = [start, end]; }
}
const root = new Element('div');
const calls = [];
let answers = {}, delay = null, confirmed = true;
async function api(raw, opts = {}) {
  const verb = raw.split('/').pop();
  const body = opts.body ? JSON.parse(opts.body) : null;
  calls.push({verb, body});
  if (delay) { const d = delay; delay = null; await d; }
  const [status, result] = answers[verb](body);
  return {ok: status < 300, status, json: async () => result};
}
// Timers the test fires by hand: the page's typing pause.
const timers = [];
const setTimeout = (fn, ms) => { timers.push({fn, ms, live: true}); return timers.length; };
const clearTimeout = id => { if (timers[id - 1]) timers[id - 1].live = false; };
const fire = () => { const due = timers.filter(t => t.live); due.forEach(t => { t.live = false; t.fn(); }); return due.length; };
const ctx = vm.createContext({document: {createElement: tag => new Element(tag), getElementById: () => root},
  api, confirm: () => confirmed, console, setTimeout, clearTimeout});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/spec-page.js'), 'utf8'), ctx);
const all = (node = root) => [node, ...node.children.flatMap(c => typeof c === 'object' ? all(c) : [])];
const button = label => all().find(n => n.tag === 'button' && n.textContent === label);
const labelled = label => all().find(n => n.attributes['aria-label'] === label);
const texts = tag => all().filter(n => n.tag === tag).map(n => n.textContent);
async function settle() { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); }
const type = value => { const area = labelled('명세 YAML'); area.value = value; area.oninput(); };
const SPEC = {name: 'demo', title: '', mesh: 'spec-demo', mesh_mode: 'new', leader_mode: 'ask',
  candidates: [], workspace: 'ws', agents: 2, issues: 2, board_dir: '/w', board_error: '', workspaces: ['ws']};
const PREVIEW = {
  sessions: [
    {id: 'leader', kind: 'leader', depth: 0, parent: null, role: 'leader', subroles: [], workflow: 'improv-leader', mode: 'ask', queue: [], reviews: [], connect: []},
    {id: 'lead', kind: 'sub-lead', depth: 1, parent: 'leader', role: 'worker', subroles: [], workflow: 'improv-worker', queue: ['epic'], reviews: [], connect: []},
    {id: 'w1', kind: 'worker', depth: 2, parent: 'lead', role: 'worker', subroles: ['shader'], workflow: 'improv-worker', queue: ['b'], reviews: [], connect: ['lead']},
    {id: 'rev', kind: 'reviewer', depth: 1, parent: 'leader', role: 'reviewer', subroles: [], workflow: '', queue: [], reviews: ['b'], connect: []},
  ],
  tasks: [
    {key: 'b', type: 'task', title: 'second', priority: 2, parent: 'epic', blocked_by: ['epic'], assignee: 'w1', review: 'rev', size: '', wave: 1, gated_by: ['auth']},
    {key: 'epic', type: 'epic', title: 'first', priority: 1, parent: null, blocked_by: [], assignee: 'lead', review: '', size: '', wave: 0, gated_by: []},
  ],
  contracts: [{id: 'c1', kind: 'seam', title: 'shape', parties: ['w1']}],
  decisions: [{key: 'auth', question: 'auth?', recommended: 'token', gates: ['b']}],
};
(async () => {
  ctx.SpecPage.open();
  const editor = labelled('명세 YAML');
  assert(labelled('입력 중 자동 검증').checked, 'auto-check is on by default');
  // Typing: no request per keystroke, one check after the pause.
  answers.check = () => [400, {error: '2 problem(s)', errors: ['agents.w1.kind: one of sub-lead, worker, reviewer', 'name: lowercase slug [a-z0-9-] required'], lines: [3, null]}];
  type('spec: 1\n'); type('spec: 1\nagents:\n  - kind: wrkr\n');
  assert.strictEqual(calls.length, 0, 'typing alone sends nothing');
  assert.strictEqual(fire(), 1, 'the second keystroke replaced the first pause');
  await settle();
  assert.strictEqual(calls.at(-1).verb, 'check');
  assert.strictEqual(labelled('명세 YAML'), editor, 'the editor is kept across redraws');
  // An error with a line: a button that selects the line.
  const go = labelled('줄 3로 이동');
  assert(go && go.textContent === '줄 3');
  go.onclick();
  assert(editor.focused);
  assert.deepStrictEqual(editor.selection, [16, 30], 'line 3 is "  - kind: wrkr"');
  // An error without one stays plain text.
  assert(texts('li').includes('name: lowercase slug [a-z0-9-] required'));
  assert(texts('span').includes('오류 2건'));
  // A valid spec: the sessions and the work, before any leader is chosen.
  answers.check = () => [200, {errors: [], spec: SPEC, preview: PREVIEW}];
  type('spec: 1\nname: demo\n'); fire(); await settle();
  assert(texts('span').includes('형식 오류 없음'));
  assert(texts('h3').includes('세션 미리보기 — 4개'));
  const cells = texts('td');
  assert(cells.includes('leader (ask)'));
  assert(cells.includes('  └ w1'), 'depth 2 is indented under its sub-lead');
  assert(cells.includes('worker + shader'));
  assert(texts('h3').includes('작업 미리보기 — 이슈 2개'));
  assert(cells.indexOf('epic') < cells.indexOf('b'), 'wave 0 before wave 1');
  assert(cells.includes('auth'), 'the gating decision is shown');
  assert(texts('h3').includes('계약 — doc 이슈 1개') && texts('h3').includes('미결 결정 1건'));
  assert.strictEqual(ctx.SpecPage.leaderChoice(), '', 'no leader was chosen for the preview');
  // Auto-check off: typing schedules nothing; the button still checks.
  const toggle = labelled('입력 중 자동 검증');
  toggle.checked = false; toggle.onchange();
  type('spec: 1\nname: demo2\n');
  assert.strictEqual(fire(), 0);
  assert(!texts('h3').some(t => t.startsWith('세션 미리보기')), 'edited text drops the old preview');
  toggle.checked = true; toggle.onchange();
  // An answer about text that was edited since is dropped.
  let release; delay = new Promise(resolve => { release = resolve; });
  answers.check = () => [200, {errors: [], spec: SPEC, preview: PREVIEW}];
  fire();
  type('spec: 1\nname: demo3\n');
  release(); await settle();
  assert(!texts('h3').some(t => t.startsWith('세션 미리보기')), 'the late answer described old text');
  // The template: fills the editor (after a confirmation over own text).
  answers.template = () => [200, {yaml: 'spec: 1\nname: my-feature\n'}];
  confirmed = false; button('예시 템플릿').onclick(); await settle();
  assert.strictEqual(editor.value, 'spec: 1\nname: demo3\n', 'declined: the text stays');
  confirmed = true; button('예시 템플릿').onclick(); await settle();
  assert.strictEqual(editor.value, 'spec: 1\nname: my-feature\n');
  assert.strictEqual(calls.at(-1).verb, 'template');
  // An import in flight is not cancelled by typing: the editor is read-only,
  // the buttons stay off until it answers, and its log is drawn.
  answers.check = () => [200, {errors: [], spec: SPEC, preview: PREVIEW}];
  answers.plan = () => [200, {actions: [{do: 'issue'}], lines: ['issue epic']}];
  answers.import = () => [200, {log: [{do: 'issue', what: 'epic', result: 'created', detail: 'claunch-x1'}]}];
  type('spec: 1\nname: demo4\n'); fire(); await settle();
  button('plan 미리보기').onclick(); await settle();
  let finish; delay = new Promise(resolve => { finish = resolve; });
  confirmed = true; button('import 실행').onclick(); await settle();
  assert(editor.readOnly, 'the editor is read-only while the import runs');
  type('spec: 1\nname: edited\n');
  assert.strictEqual(editor.value, 'spec: 1\nname: demo4\n', 'an edit during the import is refused');
  assert.strictEqual(fire(), 0, 'and schedules no check');
  assert(button('처리 중…').disabled && button('import 실행').disabled, 'no second import while the first runs');
  assert(button('예시 템플릿').disabled);
  finish(); await settle();
  assert(texts('h3').includes('import 결과') && texts('td').includes('claunch-x1'), 'the import log is drawn');
  assert(!editor.readOnly && !button('검증').disabled);
  assert.strictEqual(calls.filter(c => c.verb === 'import').length, 1);
  // Leaving the route cancels the pending pause.
  ctx.SpecPage.stop();
  assert.strictEqual(fire(), 0);
  console.log('ok');
})().catch(error => { console.error(error); process.exit(1); });
