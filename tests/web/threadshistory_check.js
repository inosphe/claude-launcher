/* All threads page (threads-history.js, claunch-8k02k): the request it makes
   for a page and its filters, what it draws for each thread, and the pager. */
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
  page.open();
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
  console.log('ok');
})().catch(e => { console.error(e); process.exit(1); });
