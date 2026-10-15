/* All threads page (threads-history.js, claunch-8k02k): the request it makes
   for a page and its filters, what it draws for each thread, and the pager;
   and its view modes (claunch-zny1o): tree (sessions > threads list panel and
   the picked thread), newest (the thread list and only the newest thread),
   feed (the cards). */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

class Node {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.className = '';
    this.dataset = {};
    this.listeners = {};
    this._text = '';
    this.value = '';
    this.disabled = false;
  }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(n) { this.children.push(n); return n; }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  all(pred, out = []) {
    for (const c of this.children) { if (pred(c)) out.push(c); c.all(pred, out); }
    return out;
  }
  byClass(cls) { return this.all(c => c.className.split(' ').includes(cls)); }
}
class Option extends Node {
  constructor(text, value) { super('option'); this._text = text; this.value = value; }
}

const view = new Node('div');
const calls = [];
let reply = null;
const ctx = vm.createContext({
  document: { createElement: tag => new Node(tag), getElementById: id => id === 'threads-view' ? view : null },
  Option, URLSearchParams, Date, Math, Number, String,
  api: async url => { calls.push(url); return { ok: true, status: 200, json: async () => reply(url) }; },
});
ctx.globalThis = ctx;
vm.runInContext(fs.readFileSync(
  path.join(__dirname, '../../src/claude_launcher/web/static/threads-history.js'), 'utf8'), ctx);
const page = ctx.ThreadsHistoryPage;
const tick = () => new Promise(r => setImmediate(r));

const thread = (id, session, status, messages, omitted = 0) => ({
  id, session, title: `title ${id}`, status, updated_at: '2026-10-14T10:00:00+00:00', omitted,
  messages: messages.map(([author, body], i) => ({ seq: i + 1, thread: id, author, body, at: '2026-10-14T10:00:00+00:00' })),
});
const sessions = [{ session: 's1', threads: 2 }, { session: 's2', threads: 1 }];

(async () => {
  // First page: two threads, three pages in all.
  reply = () => ({ threads: [
    thread('th-a', 's1', 'open', [['user', 'hello'], ['agent', 'hi back']], 4),
    thread('th-b', 's2', 'closed', []),
  ], total: 5, page: 1, per_page: 2, pages: 3, sessions });
  page.open('feed');
  assert.ok(view.textContent.includes('Loading'));
  await tick();
  assert.deepStrictEqual(calls, ['/api/threads/history?page=1']);
  const cards = view.byClass('thh-thread');
  assert.deepStrictEqual(cards.map(c => c.dataset.thread), ['th-a', 'th-b']);
  const msgs = cards[0].byClass('thh-msg');
  assert.deepStrictEqual(msgs.map(m => m.className), ['thh-msg thh-user', 'thh-msg thh-agent']);
  assert.ok(msgs[0].textContent.includes('hello') && msgs[1].textContent.includes('s1'));
  assert.ok(cards[0].textContent.includes('4 older message(s) not shown'));
  assert.ok(cards[1].textContent.includes('no messages yet'));
  assert.strictEqual(cards[0].byClass('thh-sess')[0].href, '#/s/s1');
  const pagers = view.byClass('thh-pager');
  assert.strictEqual(pagers.length, 2);                       // above and below the list
  const [newer, , older] = pagers[0].children;
  assert.ok(newer.disabled && !older.disabled);
  assert.ok(pagers[0].textContent.includes('page 1 / 3 · 5 threads'));

  // Older page.
  reply = () => ({ threads: [thread('th-c', 's1', 'open', [['user', 'old']])],
    total: 5, page: 2, per_page: 2, pages: 3, sessions });
  older.listeners.click();
  await tick();
  assert.strictEqual(calls.at(-1), '/api/threads/history?page=2');
  assert.deepStrictEqual(view.byClass('thh-thread').map(c => c.dataset.thread), ['th-c']);

  // A session filter goes back to page 1 and is sent along; so is status.
  const select = view.byClass('thh-session')[0];
  assert.deepStrictEqual(select.children.map(o => o.value), ['', 's1', 's2']);
  reply = () => ({ threads: [], total: 0, page: 1, per_page: 20, pages: 1, sessions });
  select.value = 's2';
  select.listeners.change();
  await tick();
  assert.strictEqual(calls.at(-1), '/api/threads/history?page=1&session=s2');
  assert.ok(view.textContent.includes('No threads.'));
  const status = view.byClass('thh-status')[0];
  assert.strictEqual(status.value, '');
  status.value = 'closed';
  status.listeners.change();
  await tick();
  assert.strictEqual(calls.at(-1), '/api/threads/history?page=1&session=s2&status=closed');

  // Past the last page (the result shrank under it): the last page is asked for.
  page.state.page = 4;
  let n = 0;
  reply = url => (++n === 1
    ? { threads: [], total: 3, page: 4, per_page: 2, pages: 2, sessions }
    : { threads: [thread('th-z', 's1', 'open', [])], total: 3, page: 2, per_page: 2, pages: 2, sessions });
  page.load();
  await tick(); await tick();
  assert.match(calls.at(-1), /page=2/);
  assert.deepStrictEqual(view.byClass('thh-thread').map(c => c.dataset.thread), ['th-z']);

  // An error is shown, and a closed page does not redraw.
  reply = () => ({ error: 'boom' });
  ctx.api = async url => { calls.push(url); return { ok: false, status: 500, json: async () => reply(url) }; };
  page.load();
  await tick();
  assert.ok(view.byClass('wf-warning')[0].textContent.includes('boom'));
  page.stop();
  const before = view.children;
  page.load();
  await tick();
  assert.strictEqual(view.children, before);

  // ---- view modes (claunch-zny1o) ----
  const row = (id, session, status, updated, excerpt = '') =>
    ({ id, session, title: `title ${id}`, status, updated_at: updated, messages: excerpt ? 1 : 0, excerpt });
  const index = {
    threads: [
      row('th-3', 's2', 'open', '2026-10-15T10:03:00+00:00', 'latest'),
      row('th-2', 's1', 'closed', '2026-10-15T10:02:00+00:00', 'two'),
      row('th-1', 's1', 'open', '2026-10-15T10:01:00+00:00', 'one'),
    ],
    sessions: [
      { session: 's2', threads: 1, open: 1, updated_at: '2026-10-15T10:03:00+00:00' },
      { session: 's1', threads: 2, open: 1, updated_at: '2026-10-15T10:02:00+00:00' },
    ],
    all_sessions: [{ session: 's2', threads: 1 }, { session: 's1', threads: 2 }],
    total: 3, truncated: false,
  };
  // Like the store: a session filter narrows threads and their groups, never all_sessions.
  const indexFor = url => {
    const only = new URLSearchParams(url.split('?')[1] || '').get('session');
    if (!only) return index;
    return { ...index, threads: index.threads.filter(t => t.session === only),
      sessions: index.sessions.filter(g => g.session === only) };
  };
  const conversation = id => ({
    thread: { ...index.threads.find(t => t.id === id) },
    messages: [{ seq: 1, thread: id, author: 'user', body: `body of ${id}`, at: '2026-10-15T10:00:00+00:00' }],
    more: false,
  });
  ctx.api = async url => {
    calls.push(url);
    const m = url.match(/^\/api\/threads\/(th-[^?]+)$/);
    const data = url.startsWith('/api/threads/index') ? indexFor(url) : m ? conversation(m[1]) : { error: 'unexpected ' + url };
    return { ok: !data.error, status: data.error ? 404 : 200, json: async () => data };
  };
  page.state.session = ''; page.state.status = '';
  const settle = async () => { await tick(); await tick(); await tick(); };
  const modeTabs = () => view.byClass('thh-mode');

  // The modes are told apart: one tab each, the current one marked, and its own route.
  assert.strictEqual(page.route('tree', 'th-1'), '#/threads/tree/th-1');
  assert.strictEqual(page.route('newest'), '#/threads/newest');

  // tree (the default): sessions with their threads under them, nothing picked yet.
  calls.length = 0;
  page.open();
  await settle();
  assert.strictEqual(page.state.view, 'tree');
  assert.deepStrictEqual(calls, ['/api/threads/index']);
  assert.deepStrictEqual(modeTabs().map(t => [t.dataset.view, t.className.includes('active')]),
    [['tree', true], ['newest', false], ['feed', false]]);
  assert.strictEqual(view.byClass('thh-tree').length, 1);
  const groups = view.byClass('thh-group');
  assert.deepStrictEqual(groups.map(g => g.dataset.session), ['s2', 's1']);
  assert.deepStrictEqual(groups[1].byClass('thh-trow').map(r => r.dataset.thread), ['th-2', 'th-1']);
  assert.ok(groups[1].byClass('thh-srow')[0].textContent.includes('2 thread(s) · 1 open'));
  assert.ok(view.byClass('thh-detail')[0].textContent.includes('Pick a thread'));

  // Picking a thread reads only that one; the list is not fetched again.
  calls.length = 0;
  groups[1].byClass('thh-trow')[1].listeners.click();
  await settle();
  assert.deepStrictEqual(calls, ['/api/threads/th-1']);
  const shown = view.byClass('thh-detail')[0];
  assert.strictEqual(shown.dataset.thread, 'th-1');
  assert.ok(shown.textContent.includes('body of th-1'));
  assert.ok(view.byClass('thh-trow').find(r => r.dataset.thread === 'th-1').className.includes('active'));

  // A session row folds its threads away and back.
  view.byClass('thh-group')[1].byClass('thh-srow')[0].listeners.click();
  assert.strictEqual(view.byClass('thh-group')[1].byClass('thh-trow').length, 0);
  view.byClass('thh-group')[1].byClass('thh-srow')[0].listeners.click();
  assert.strictEqual(view.byClass('thh-group')[1].byClass('thh-trow').length, 2);

  // newest: the whole thread list stays (flat, with sessions), and only the newest shows.
  calls.length = 0;
  modeTabs()[1].listeners.click();
  await settle();
  assert.strictEqual(page.state.view, 'newest');
  assert.deepStrictEqual(calls, ['/api/threads/th-3']);
  assert.strictEqual(view.byClass('thh-tree').length, 0);
  const flat = view.byClass('thh-flat')[0];
  assert.deepStrictEqual(flat.byClass('thh-trow').map(r => r.dataset.thread), ['th-3', 'th-2', 'th-1']);
  assert.strictEqual(flat.byClass('thh-badge').length, 1);
  assert.ok(flat.byClass('thh-trow')[0].textContent.includes('newest'));
  assert.strictEqual(view.byClass('thh-detail')[0].dataset.thread, 'th-3');
  assert.strictEqual(view.byClass('thh-msg').length, 1);         // one conversation, not every thread's

  // Refresh in newest follows whichever thread is newest now.
  index.threads.unshift(row('th-4', 's1', 'open', '2026-10-15T10:04:00+00:00', 'newer'));
  calls.length = 0;
  view.byClass('thh-controls')[0].children.find(c => c.textContent === 'Refresh').listeners.click();
  await settle();
  assert.deepStrictEqual(calls, ['/api/threads/index', '/api/threads/th-4']);
  assert.strictEqual(view.byClass('thh-detail')[0].dataset.thread, 'th-4');

  // A row in newest opens that thread in tree; the filters are sent to the index.
  view.byClass('thh-trow').find(r => r.dataset.thread === 'th-2').listeners.click();
  await settle();
  assert.strictEqual(page.state.view, 'tree');
  assert.strictEqual(view.byClass('thh-detail')[0].dataset.thread, 'th-2');
  calls.length = 0;
  const sel = view.byClass('thh-session')[0];
  assert.deepStrictEqual(sel.children.map(o => o.value), ['', 's2', 's1']);
  sel.value = 's1';
  sel.listeners.change();
  await settle();
  assert.deepStrictEqual(calls, ['/api/threads/index?session=s1', '/api/threads/th-2']);
  // The other sessions stay pickable after the filter narrowed the list.
  assert.deepStrictEqual(view.byClass('thh-group').map(g => g.dataset.session), ['s1']);
  assert.deepStrictEqual(view.byClass('thh-session')[0].children.map(o => o.value), ['', 's2', 's1']);
  assert.strictEqual(view.byClass('thh-session')[0].value, 's1');

  // feed is the card list again, from the history route.
  page.state.session = '';
  calls.length = 0;
  reply = () => ({ threads: [thread('th-a', 's1', 'open', [['user', 'hello']])], total: 1, page: 1, per_page: 20, pages: 1, sessions });
  ctx.api = async url => { calls.push(url); return { ok: true, status: 200, json: async () => reply(url) }; };
  modeTabs()[2].listeners.click();
  await settle();
  assert.deepStrictEqual(calls, ['/api/threads/history?page=1']);
  assert.strictEqual(view.byClass('thh-split').length, 0);
  assert.deepStrictEqual(view.byClass('thh-thread').map(c => c.dataset.thread), ['th-a']);

  // An unknown mode falls back to tree.
  page.open('nope');
  assert.strictEqual(page.state.view, 'tree');
  page.stop();
  console.log('ok');
})().catch(e => { console.error(e); process.exit(1); });
