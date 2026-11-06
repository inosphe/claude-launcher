/* Real subprocs.js (claunch-8t5r0.3-.5): the session card line and the
   header chip drawn from the rail row's `subprocs`, a fallback shown as
   such; the picker listing every procedure with the unavailable ones
   disabled and their reason as the tooltip, the "available only" filter,
   the call body, and the pack role editor's body. Daemon text goes in as
   text. */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

class ClassList {
  constructor(el) { this.el = el; }
  _set() { return new Set(String(this.el.className || '').split(/\s+/).filter(Boolean)); }
  contains(c) { return this._set().has(c); }
  toggle(c, on) {
    const s = this._set();
    const want = on === undefined ? !s.has(c) : !!on;
    if (want) s.add(c); else s.delete(c);
    this.el.className = [...s].join(' ');
    return want;
  }
}

class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attributes = {}; this.dataset = {};
    this.textContent = ''; this.parent = null; this.className = ''; this.title = '';
    this.listeners = {}; this.classList = new ClassList(this); this.disabled = false; this.value = '';
  }
  append(...items) { for (const i of items) { if (typeof i === 'object') { if (i.parent) i.remove(); i.parent = this; } this.children.push(i); } }
  appendChild(i) { this.append(i); return i; }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  setAttribute(name, value) { this.attributes[name] = value; }
  contains(other) { for (let n = other; n; n = n.parent) if (n === this) return true; return false; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null; }
  get nextSibling() { if (!this.parent) return null; const k = this.parent.children; return k[k.indexOf(this) + 1] || null; }
  after(n) { const p = this.parent; if (n.parent) n.remove(); n.parent = p; p.children.splice(p.children.indexOf(this) + 1, 0, n); }
  addEventListener(kind, fn) { (this.listeners[kind] ||= []).push(fn); }
  fire(kind, ev = {}) { for (const fn of this.listeners[kind] || []) fn({ preventDefault() {}, stopPropagation() {}, ...ev }); }
  querySelector(sel) { const cls = sel.replace(/^\./, ''); return walk(this).slice(1).find(e => e.classList && e.classList.contains(cls)) || null; }
}

function walk(n, out = []) { out.push(n); for (const c of n.children || []) if (typeof c === 'object') walk(c, out); return out; }
function text(n) { return walk(n).map(e => e.textContent).join(' '); }
function itemOf(host, name) {
  return walk(host).find(e => e.tag === 'li' && walk(e).some(c => c.className === 'subproc-name' && c.textContent === name));
}
function button(row, label) { return walk(row).find(e => e.tag === 'button' && e.textContent === label); }
const flush = () => new Promise(r => setTimeout(r, 0));

const calls = [];
const PROCS = {
  run: 'r1', scope: 's1@main', task: 'n3', task_label: 'round/work', fork_calls: true,
  roles: ['worker'], refusal: '',
  procedures: [
    { name: 'sim-run', source: 'calls', available: true, why: '', mode: 'inline', declared: true,
      params: { question: { type: 'str', required: true }, purpose: { type: 'str', default: 'spec' } } },
    { name: 'unreal:editor-handoff', source: 'pack', pack: 'unreal', available: false, mode: 'fork',
      forkable: true, pack_roles: { type: 'whitelist', roles: ['unreal'] },
      why: 'pack unreal admits only unreal; the calling session holds worker', params: {} },
  ],
};
const PACKS = {
  cwd: '/w', packs: [
    { id: 'unreal', layer: 'project', installed: true, title: '<b>Unreal</b>', roles: { type: 'whitelist', roles: ['unreal'] },
      procedures: ['unreal:build-check', 'unreal:editor-handoff'] },
    { id: 'other', layer: 'package', installed: false, roles: null, procedures: ['other:x'] },
  ],
};

async function api(raw, opts = {}) {
  const url = new URL(raw, 'http://test');
  const method = opts.method || 'GET', body = opts.body ? JSON.parse(opts.body) : null;
  calls.push({ path: url.pathname, query: Object.fromEntries(url.searchParams), method, body });
  let result;
  if (url.pathname === '/api/cflow2/procedures') result = PROCS;
  else if (url.pathname === '/api/cflow2/packs' && method === 'GET') result = PACKS;
  else if (url.pathname === '/api/cflow2/call') result = { called: { call: 'c1', mode: 'inline' } };
  else if (url.pathname.startsWith('/api/cflow2/packs/')) result = PACKS;
  else return { ok: false, status: 404, json: async () => ({ error: 'no route' }) };
  return { ok: true, json: async () => result };
}

const chip = new Element('span');
chip.className = 'term-subproc hidden';
let toggled = [];
const ctx = vm.createContext({
  document: { createElement: t => new Element(t), createTextNode: s => { const n = new Element('#text'); n.textContent = s; return n; },
    getElementById: id => (id === 'term-subproc' ? chip : null) },
  api, URL, console, JSON, confirm: () => true, queueMicrotask,
  localStorage: { getItem: () => null, setItem: () => {} },
  toggleSplitPane: k => toggled.push(k),
});
vm.runInContext('var cflow2RailCache = {}; var currentName = "s1";', ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/subprocs.js'), 'utf8'), ctx);

(async () => {
  // -- card and header -------------------------------------------------------
  const li = new Element('li');
  const v2 = new Element('a'); v2.className = 'sess-cflow2'; li.append(v2, new Element('span'));
  ctx.SubprocUi.card(li, { subprocs: [] });
  assert.strictEqual(li.querySelector('.sess-subproc'), null);
  const sub = { id: 'c1', procedure: 'unreal:editor-handoff', label: 'round/work', mode: 'fork', by: 'agent',
    fork: { status: 'running', session: 's1-qf1' } };
  ctx.SubprocUi.card(li, { subprocs: [sub] });
  const line = li.querySelector('.sess-subproc');
  assert.ok(line && line.textContent === '⤷ sub unreal:editor-handoff · fork', line && line.textContent);
  assert.strictEqual(v2.nextSibling, line, 'the line sits under the v2 line');
  assert.ok(line.title.includes('fork session: s1-qf1'));
  assert.ok(!line.classList.contains('fallback'));
  const fell = { ...sub, mode: 'inline', fork: { status: 'fallback' }, fallback: { cause: 'harness has no conversation' } };
  ctx.SubprocUi.card(li, { subprocs: [fell] });
  assert.ok(line.classList.contains('fallback') && line.textContent.includes('fork→inline'));
  assert.ok(line.title.includes('fork failed, running inline: harness has no conversation'));
  ctx.SubprocUi.card(li, null);
  assert.strictEqual(li.querySelector('.sess-subproc'), null, 'gone when the call is');

  ctx.SubprocUi.header();
  assert.ok(chip.classList.contains('hidden'));
  vm.runInContext('cflow2RailCache = {s1: ' + JSON.stringify({ cwd: '/w', scope: 's1@main', subprocs: [fell] }) + '};', ctx);
  ctx.SubprocUi.header();
  assert.ok(!chip.classList.contains('hidden') && chip.classList.contains('fallback'));
  assert.ok(chip.textContent.includes('fork→inline') && chip.textContent.endsWith('⚠'), chip.textContent);
  chip.fire('click');
  assert.deepStrictEqual(toggled, ['subprocs']);

  // -- the picker ---------------------------------------------------------------
  const target = new Element('div');
  ctx.SubprocPane.mount(target, 's1');
  await flush(); await flush();
  const host = target.children[0];
  assert.strictEqual(host.className, 'subproc-host');
  assert.deepStrictEqual(calls.slice(0, 2).map(c => [c.path, c.query.scope || null]),
    [['/api/cflow2/procedures', 's1@main'], ['/api/cflow2/packs', null]]);
  assert.ok(text(host).includes('Sub procedures (1/2 available)'));
  const blocked = itemOf(host, 'unreal:editor-handoff');
  assert.ok(blocked.className.includes('disabled'));
  assert.strictEqual(blocked.title, PROCS.procedures[1].why, 'the reason is the tooltip');
  const bcall = button(blocked, 'call…');
  assert.ok(bcall.disabled && bcall.title === PROCS.procedures[1].why);
  assert.ok(!button(itemOf(host, 'sim-run'), 'call…').disabled);
  // Daemon text is text.
  assert.ok(walk(host).some(e => e.textContent.includes('<b>Unreal</b>')));
  assert.ok(!('innerHTML' in host));

  // the filter keeps only what can run
  const box = walk(host).find(e => e.tag === 'input' && e.type === 'checkbox');
  box.checked = true; box.fire('change');
  const host2 = target.children[0];
  assert.ok(itemOf(host2, 'sim-run') && !itemOf(host2, 'unreal:editor-handoff'));
  box.checked = false;
  const box2 = walk(host2).find(e => e.tag === 'input' && e.type === 'checkbox');
  box2.checked = false; box2.fire('change');

  // a call: the form prefilled from the params, then POSTed as the user's
  let h = target.children[0];
  button(itemOf(h, 'sim-run'), 'call…').fire('click');
  h = target.children[0];
  const form = walk(itemOf(h, 'sim-run')).find(e => e.className === 'subproc-form');
  const ta = walk(form).find(e => e.tag === 'textarea');
  assert.deepStrictEqual(JSON.parse(ta.value), { question: '', purpose: 'spec' });
  ta.value = JSON.stringify({ question: 'q?', purpose: 'spec' });
  button(form, 'call').fire('click');
  await flush(); await flush(); await flush();
  const post = calls.find(c => c.path === '/api/cflow2/call');
  assert.deepStrictEqual(JSON.parse(JSON.stringify(post.body)), {
    cwd: '/w', scope: 's1@main', procedure: 'sim-run', node: 'n3',
    with: { question: 'q?', purpose: 'spec' }, mode: 'auto' });

  // the role filter editor writes {type, roles}; a catalog pack has it disabled
  h = target.children[0];
  const unreal = itemOf(h, 'unreal');
  const save = button(unreal, 'save roles');
  assert.ok(!save.disabled);
  const input = walk(unreal).find(e => e.tag === 'input' && e.type === 'text');
  input.value = 'unreal, Engine ';
  save.fire('click');
  await flush();
  const roles = calls.find(c => c.path === '/api/cflow2/packs/roles');
  assert.deepStrictEqual(JSON.parse(JSON.stringify(roles.body)),
    { cwd: '/w', pack: 'unreal', roles: { type: 'whitelist', roles: ['unreal', 'Engine'] } });
  await flush(); await flush();
  h = target.children[0];
  assert.ok(button(itemOf(h, 'other'), 'save roles').disabled);
  button(itemOf(h, 'other'), 'install').fire('click');
  await flush();
  assert.ok(calls.some(c => c.path === '/api/cflow2/packs/install' && c.body.pack === 'other'));

  // a session with no v2 run
  const none = new Element('div');
  ctx.SubprocPane.mount(none, 'ghost');
  await flush();
  assert.ok(text(none).includes('no cflow v2 run'));
  ctx.SubprocPane.unmount();
  assert.strictEqual(none.children.length, 0);
  console.log('subprocs_check ok');
})().catch((e) => { console.error(e); process.exit(1); });
