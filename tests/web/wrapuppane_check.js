/* Real wrapup-pane.js (claunch-ar8jn): the tabs and their counts, the form
   <-> payload round trip, save / confirm / delete bodies with the revision
   read, a confirm with unsaved edits saving first, a 409 replacing the
   form with the current draft, confirm-all, and the ended-session warning.
   A retrospection draft (claunch-lmnlv) edits title and body only and keeps
   what it looked back on; an auto-confirmed one is a settled row marked
   자동 기록 linking to its Retrospection record. */
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
let drafts = [
  {id: 'wd-g1', kind: 'glossary', status: 'draft', revision: 1, reason: 'used in the round', edited_by_user: false,
   payload: {term: 'draft', definition: '임시본', aliases: ['임시'], source: ''}},
  {id: 'wd-k1', kind: 'knowledge', status: 'failed', revision: 3, reason: '', error: 'title taken', edited_by_user: false,
   payload: {kind: 'lesson', title: 'L', body: 'B', tags: [], refs: [], source: ''}},
  {id: 'wd-c1', kind: 'category', status: 'draft', revision: 1, reason: '',
   payload: {new: {label: 'wrap-up', aliases: [], description: ''}, items: [{kind: 'session', ref: 's1'}]}},
  {id: 'wd-c2', kind: 'category', status: 'confirmed', revision: 3, reason: '',
   payload: {node_id: 'n1'}, applied: {path: ['web', 'panes']}},
];
let alive = true;
let conflictOn = null;

function listing() {
  const open = drafts.filter(d => ['draft', 'failed'].includes(d.status));
  const counts = {glossary: 0, category: 0, knowledge: 0, retrospection: 0};
  for (const d of open) counts[d.kind]++;
  return {session: 's1', drafts, unresolved: open.length, counts, alive};
}

async function request(raw, method = 'GET', body) {
  calls.push({path: raw, method, body});
  const url = new URL(raw, 'http://test');
  const m = url.pathname.match(/^\/api\/wrapup\/([^/]+)(?:\/(confirm|discard))?$/);
  if (url.pathname === '/api/wrapup') return listing();
  if (url.pathname === '/api/wrapup/confirm-all') {
    let n = 0;
    for (const d of drafts) if (d.kind === body.kind && ['draft', 'failed'].includes(d.status)) { d.status = 'confirmed'; d.revision += 2; n++; }
    return {confirmed: n, failed: 0, drafts: []};
  }
  if (m) {
    const d = drafts.find(x => x.id === m[1]);
    if (conflictOn === d.id) {
      conflictOn = null;
      d.revision += 1; d.payload = {...d.payload, term: 'agent rewrote it'};
      throw Object.assign(Error(`draft ${d.id} is at revision ${d.revision}`), {status: 409, body: {current: d}});
    }
    assert.strictEqual(body.revision, d.revision, `stale revision for ${d.id}`);
    if (!m[2]) { d.payload = body.payload; d.revision += 1; d.status = 'draft'; return {draft: d}; }
    d.status = m[2] === 'confirm' ? 'confirmed' : 'discarded'; d.revision += 1;
    return {draft: d};
  }
  throw new Error('unexpected ' + raw);
}

const ctx = vm.createContext({
  document: {createElement: tag => new Element(tag)},
  api: async () => { throw new Error('the pane must use the injected request'); },
  URL, console, JSON,
  setTimeout: () => 0, clearTimeout: () => {},
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/wrapup-pane.js'), 'utf8'), ctx);

const all = (node) => [node, ...node.children.flatMap(c => typeof c === 'object' ? all(c) : [])];
const text = (host) => all(host).map(n => n.textContent).join('\n');
const buttons = (host, label) => all(host).filter(n => n.tag === 'button' && n.textContent === label);
const tab = (host, prefix) => all(host).find(n => n.tag === 'button' && n.attributes.role === 'tab' && n.textContent.startsWith(prefix));
const input = (host, label) => all(host).find(n => n.attributes['aria-label'] === label);
async function settle() { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); }
const posts = () => JSON.parse(JSON.stringify(calls.filter(c => c.method === 'POST')));

(async () => {
  // The form round trip keeps what the form does not show.
  const cat = drafts[2];
  const f = ctx.wrapupFormOf('category', cat.payload);
  assert.strictEqual(f.op, 'new');
  f.aliases = 'a, b';
  assert.deepStrictEqual(JSON.parse(JSON.stringify(ctx.wrapupPayloadOf('category', f, cat.payload))),
    {new: {label: 'wrap-up', aliases: ['a', 'b'], description: ''}, items: [{kind: 'session', ref: 's1'}]});
  const fold = ctx.wrapupFormOf('category', {fold: {label: 'x', nodes: ['n1']}});
  assert.strictEqual(fold.op, 'fold');
  fold.json = '{oops';
  assert.throws(() => ctx.wrapupPayloadOf('category', fold, {}), /JSON/);
  assert.strictEqual(ctx.wrapupSummary(drafts[3]), '기존 노드 n1에 배치');

  const host = new Element('div');
  ctx.WrapupPane.mount(host, 's1', {request}); await settle();
  // Tabs carry the unresolved counts; the first tab with drafts is shown.
  assert(tab(host, 'Glossary (1)') && tab(host, 'Category (1)') && tab(host, 'Knowledge (1)'));
  assert.strictEqual(tab(host, 'Glossary').attributes['aria-selected'], 'true');
  assert(text(host).includes('draft') && text(host).includes('이유: used in the round'));

  // An edited draft confirmed: the edit is saved first, then confirmed with
  // the revision the save returned.
  const def = input(host, '정의');
  def.value = '확정 전 기록'; def.oninput();
  buttons(host, '확정')[0].onclick(); await settle();
  const [save, confirm] = posts().slice(-2);
  assert.strictEqual(save.path, '/api/wrapup/wd-g1');
  assert.deepStrictEqual(save.body, {revision: 1, payload: {term: 'draft', definition: '확정 전 기록', aliases: ['임시'], source: ''}});
  assert.deepStrictEqual(confirm, {path: '/api/wrapup/wd-g1/confirm', method: 'POST', body: {revision: 2}});
  assert(text(host).includes('확정을 기다리는 임시본이 없습니다'));
  assert(text(host).includes('처리된 항목 1개'));

  // A failed knowledge draft shows its store's refusal.
  tab(host, 'Knowledge').onclick();
  assert(text(host).includes('title taken'));
  buttons(host, '삭제')[0].onclick(); await settle();
  assert.deepStrictEqual(posts().slice(-1)[0], {path: '/api/wrapup/wd-k1/discard', method: 'POST', body: {revision: 3}});

  // A 409: the agent rewrote the draft; the form shows the current text.
  drafts.push({id: 'wd-g2', kind: 'glossary', status: 'draft', revision: 1, reason: '',
               payload: {term: 'old', definition: 'd', aliases: [], source: ''}});
  ctx.WrapupPane._view().open(); await settle();
  tab(host, 'Glossary').onclick();
  const term = input(host, '용어');
  term.value = 'mine'; term.oninput();
  conflictOn = 'wd-g2';
  buttons(host, '저장')[0].onclick(); await settle();
  assert(text(host).includes('revision 2'));
  assert.strictEqual(input(host, '용어').value, 'agent rewrote it');

  // Confirm-all for the tab; refused while a form has unsaved edits.
  tab(host, 'Category').onclick();
  const label = input(host, '새 카테고리 이름');
  label.value = 'wrapup'; label.oninput();
  const before = posts().length;
  buttons(host, '이 탭 모두 확정 (1)')[0].onclick(); await settle();
  assert.strictEqual(posts().length, before);
  assert(text(host).includes('저장하지 않은 편집'));
  buttons(host, '저장')[0].onclick(); await settle();
  buttons(host, '이 탭 모두 확정 (1)')[0].onclick(); await settle();
  assert.deepStrictEqual(posts().slice(-1)[0], {path: '/api/wrapup/confirm-all', method: 'POST', body: {target: 's1', kind: 'category'}});

  // Retrospection: the user edits title and body; run, issue and counts stay.
  const retro = {title: 'R', body: '## A', trigger: 'run', run: 'run-1', workflow: 'improv-worker-v2',
                 issue: 'claunch-x1', problems: 2, proposals: 1};
  drafts.push({id: 'wd-r1', kind: 'retrospection', status: 'draft', revision: 1, reason: 'run-1 회고', payload: retro},
              {id: 'wd-r2', kind: 'retrospection', status: 'confirmed', revision: 3, reason: '', settled_by: 'auto',
               payload: {...retro, title: 'auto one'}, applied: {id: 'rt-abc', title: 'auto one'}});
  ctx.WrapupPane._view().open(); await settle();
  assert(tab(host, 'Retrospection (1)'));
  tab(host, 'Retrospection').onclick();
  assert(text(host).includes('런 회고 · run-1 · improv-worker-v2 · claunch-x1 · 문제 2 · 제안 1'));
  const rtitle = input(host, '제목');
  rtitle.value = 'R2'; rtitle.oninput();
  buttons(host, '저장')[0].onclick(); await settle();
  assert.deepStrictEqual(posts().slice(-1)[0].body, {revision: 1, payload: {...retro, title: 'R2'}});
  const autoRow = all(host).find(n => n.tag === 'li' && all(n).some(c => c.textContent === 'auto one'));
  assert(autoRow && all(autoRow).some(c => c.textContent === '자동 기록'));
  assert(all(autoRow).some(c => c.tag === 'a' && c.href === '#/retrospection/rt-abc'));
  drafts = drafts.filter(d => d.kind !== 'retrospection');

  // An ended session with drafts left says so.
  alive = false;
  ctx.WrapupPane._view().open(); await settle();
  assert(text(host).includes('이 세션은 종료되었습니다. 남은 임시본 1건'));
  ctx.WrapupPane.unmount();
  assert.strictEqual(host.children.length, 0);

  // The dashboard card for a session that ended with drafts waiting
  // (app.js wrapupNoticeSync): none on a browser's first poll, one per end,
  // and it goes down once the drafts are settled.
  const appSrc = fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/app.js'), 'utf8');
  const fn = (name) => {
    const start = appSrc.indexOf(`function ${name}(`);
    let depth = 0;
    for (let j = appSrc.indexOf('{', start); j < appSrc.length; j++) {
      if (appSrc[j] === '{') depth++;
      else if (appSrc[j] === '}') { depth--; if (!depth) return appSrc.slice(start, j + 1); }
    }
    throw new Error(name);
  };
  const saved = {}, cards = [], dismissed = [];
  const n = vm.createContext({
    noticeLoad: (k, d) => (k in saved ? saved[k] : d), noticeSave: (k, v) => { saved[k] = v; },
    notify: (title, sub, opts) => cards.push({title, sub, opts}), dismissNotice: (k) => dismissed.push(k),
    setSessLayout() {}, location: {hash: ''},
  });
  vm.runInContext(`const WRAPUP_NOTICED_KEY = "k"; let wrapupNoticed = null;
    ${fn('wrapupNoticeOpen')} ${fn('wrapupNoticeSync')} this.sync = wrapupNoticeSync;`, n);
  const ended = {s1: {alive: false, ended_at: 't1', unresolved: 2, counts: {glossary: 1, category: 0, knowledge: 1}},
                 s2: {alive: true, ended_at: null, unresolved: 3, counts: {}}};
  n.sync(ended);
  assert.strictEqual(cards.length, 0, 'the first poll announces nothing');
  n.sync({...ended, s3: {alive: false, ended_at: 't2', unresolved: 1, counts: {category: 1}}});
  assert.strictEqual(cards.length, 1);
  assert(cards[0].title.includes('s3 ended with 1 unconfirmed') && cards[0].opts.source === 'wrapup' && cards[0].opts.sticky);
  n.sync({...ended, s3: {alive: false, ended_at: 't2', unresolved: 1, counts: {category: 1}}});
  assert.strictEqual(cards.length, 1, 'announced once');
  n.sync(ended);
  assert.deepStrictEqual(dismissed, ['wrapup-s3']);
  console.log('ok');
})().catch(error => { console.error(error); process.exit(1); });
