/* All threads page (threads-history.js, claunch-8k02k): the request it makes
   for a page and its filters, what it draws for each thread, and the pager;
   and its view modes (claunch-zny1o): tree (sessions > threads list panel and
   the picked thread), newest (the thread list and only the newest thread),
   feed (the cards). And the composer under the thread shown in tree and
   newest (claunch-1qj0b): text and images posted the way the session's
   threads pane posts them. */
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
    this.classList = { add() {}, remove() {} };
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
  Option, URLSearchParams, Date, Math, Number, String, JSON, Object, Array,
  api: async url => { calls.push(url); return { ok: true, status: 200, json: async () => reply(url) }; },
});
ctx.globalThis = ctx;
// The dashboard loads threads-pane.js first; the page borrows its helpers.
vm.runInContext(fs.readFileSync(
  path.join(__dirname, '../../src/claude_launcher/web/static/threads-pane.js'), 'utf8'), ctx);
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
    thread('th-a', 's1', 'open', [['user', 'hello'], ['agent', 'hi back'], ['guest', 'try B'],
                                  ['system', 'guest s3 ended']], 4),
    thread('th-b', 's2', 'closed', []),
  ], total: 5, page: 1, per_page: 2, pages: 3, sessions });
  page.open('feed');
  assert.ok(view.textContent.includes('Loading'));
  await tick();
  assert.deepStrictEqual(calls, ['/api/threads/history?page=1']);
  const cards = view.byClass('thh-thread');
  assert.deepStrictEqual(cards.map(c => c.dataset.thread), ['th-a', 'th-b']);
  const msgs = cards[0].byClass('thh-msg');
  assert.deepStrictEqual(msgs.map(m => m.className),
                         ['thh-msg thh-user', 'thh-msg thh-agent', 'thh-msg thh-guest', 'thh-msg thh-system']);
  assert.ok(msgs[0].textContent.includes('hello') && msgs[1].textContent.includes('s1'));
  assert.ok(msgs[2].textContent.includes('(guest)') && !msgs[2].textContent.includes('s1'));  // claunch-p9wes
  // claunch-p9wes.1: a daemon notice says so instead of passing as the session's.
  assert.ok(msgs[3].textContent.includes('daemon') && msgs[3].textContent.includes('guest s3 ended'));
  assert.ok(!msgs[3].textContent.includes('s1'));
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
  assert.strictEqual(view.byClass('thh-compose').length, 0);   // feed stays read-only

  // Images the user attached stay on the record (claunch-shl2r): an images-only
  // message draws its pictures, each linked to the full one, and no empty body.
  const pic = thread('th-p', 's1', 'open', [['user', '']]);
  pic.messages[0].images = [{ name: 'ab.png', type: 'image/png' }];
  reply = () => ({ threads: [pic], total: 1, page: 1, per_page: 20, pages: 1, sessions });
  page.open('feed');
  await settle();
  const pmsg = view.byClass('thh-msg')[0];
  assert.strictEqual(pmsg.byClass('thh-body').length, 0);
  const links = pmsg.byClass('th-image');
  assert.strictEqual(links.length, 1);
  assert.strictEqual(links[0].href, 'api/threads/th-p/images/ab.png');
  assert.strictEqual(links[0].children[0].src, links[0].href);

  // An agent's ask (claunch-etw0s): the read-only pages show the answer
  // state as a chip -- waiting, then the user's answer with its note.
  const askThread = (answer) => ({
    id: 'th-ask', session: 's1', title: 'Landing?', status: 'open',
    updated_at: '2026-10-14T10:00:00+00:00', omitted: 0,
    messages: [{ seq: 1, thread: 'th-ask', author: 'agent', kind: 'ask', body: '이대로 진행?',
                 at: '2026-10-14T10:00:00+00:00',
                 data: { type: 'approve', choices: [], answer } }],
  });
  reply = () => ({ threads: [askThread(null)], total: 1, page: 1, per_page: 20, pages: 1, sessions });
  page.open('feed');
  await settle();
  const waiting = view.byClass('thh-msg')[0];
  assert.ok(waiting.byClass('thh-ask-waiting').length === 1
            && waiting.textContent.includes('답변 대기'), 'an unanswered ask says 답변 대기');
  assert.strictEqual(waiting.byClass('thh-answer').length, 0);
  reply = () => ({ threads: [askThread({ decision: 'approve', text: 'go', at: '2026-10-14T10:01:00+00:00' })],
    total: 1, page: 1, per_page: 20, pages: 1, sessions });
  page.open('feed');
  await settle();
  const answered = view.byClass('thh-msg')[0];
  assert.ok(answered.byClass('thh-ask-answered').length === 1
            && answered.textContent.includes('사용자 답변 · 승인'), 'the answered ask shows the decision');
  assert.ok(answered.textContent.includes('go') && answered.byClass('thh-answer').length === 1,
            'the answer note lands under the question');

  // ---- the composer (claunch-1qj0b) ----
  const posts = [];
  let postReply = () => ({ status: 200, data: { delivered: true } });
  let staged = 0;
  ctx.api = async (url, opts = {}) => {
    calls.push(url);
    if (opts.method === 'POST') {
      posts.push({ url, type: opts.headers['Content-Type'], body: opts.body });
      if (url === '/api/threads/images') return { ok: true, status: 200, json: async () => ({ id: `img-${++staged}` }) };
      const r = postReply();
      return { ok: r.status < 400, status: r.status, json: async () => r.data };
    }
    const m = url.match(/^\/api\/threads\/(th-[^?]+)$/);
    const data = url.startsWith('/api/threads/index') ? indexFor(url) : m ? conversation(m[1]) : { error: 'unexpected ' + url };
    return { ok: !data.error, status: data.error ? 404 : 200, json: async () => data };
  };
  page.open('tree', 'th-1');
  await settle();
  const composer = () => view.byClass('thh-compose')[0];
  const textArea = () => composer().byClass('th-text')[0];
  const sendBtn = () => composer().byClass('th-send')[0];
  assert.strictEqual(composer().dataset.thread, 'th-1');
  assert.strictEqual(textArea().placeholder, 's1에게 보내기');

  // Enter sends the text; the composer empties and the thread is read again.
  textArea().value = '  hello there ';
  textArea().listeners.input();
  let prevented = false;
  calls.length = 0;
  textArea().listeners.keydown({ key: 'Enter', shiftKey: false, preventDefault: () => { prevented = true; } });
  assert.ok(prevented);
  assert.ok(sendBtn().disabled && sendBtn().textContent === '전송 중…');
  await settle();
  assert.deepStrictEqual(posts.map(p => [p.url, p.type]), [['/api/threads/th-1/messages', 'application/json']]);
  assert.deepStrictEqual(JSON.parse(posts[0].body), { text: 'hello there' });
  assert.deepStrictEqual(calls, ['/api/threads/th-1/messages', '/api/threads/th-1']);
  assert.strictEqual(textArea().value, '');
  assert.ok(!sendBtn().disabled);
  // Shift+Enter is a new line: nothing is sent.
  textArea().listeners.keydown({ key: 'Enter', shiftKey: true, preventDefault: () => assert.fail('sent') });
  // An empty composer sends nothing.
  posts.length = 0;
  sendBtn().listeners.click();
  await settle();
  assert.strictEqual(posts.length, 0);

  // A pasted screenshot waits under the box; a picked non-image is left out.
  const png = { name: 'shot.png', type: 'image/png' };
  let pasted = false;
  textArea().listeners.paste({ clipboardData: { items: [{ kind: 'file', type: 'image/png', getAsFile: () => png }] },
    preventDefault: () => { pasted = true; } });
  assert.ok(pasted);
  assert.strictEqual(composer().byClass('th-attach-item').length, 1);
  const input = composer().all(c => c.tagName === 'INPUT')[0];
  input.files = [{ name: 'notes.txt', type: 'text/plain' }, { name: 'b.webp', type: '' }];
  input.listeners.change();
  assert.strictEqual(composer().byClass('th-attach-item').length, 2);
  assert.ok(view.byClass('thh-notice')[0].textContent.includes('이미지가 아닌 파일 1개'));
  // × takes one back out.
  composer().byClass('th-attach-drop')[1].listeners.click();
  assert.strictEqual(composer().byClass('th-attach-item').length, 1);

  // A failed post keeps the text and the image for a retry and says why.
  textArea().value = 'see this';
  textArea().listeners.input();
  postReply = () => ({ status: 409, data: { error: 'thread is an operator thread' } });
  sendBtn().listeners.click();
  await settle();
  assert.deepStrictEqual(posts.map(p => [p.url, p.type]),
    [['/api/threads/images', 'image/png'], ['/api/threads/th-1/messages', 'application/json']]);
  assert.strictEqual(posts[0].body, png);
  assert.deepStrictEqual(JSON.parse(posts[1].body), { text: 'see this', images: ['img-1'] });
  assert.strictEqual(textArea().value, 'see this');
  assert.strictEqual(composer().byClass('th-attach-item').length, 1);
  assert.ok(view.byClass('thh-notice')[0].textContent.includes('operator thread'));

  // The retry goes through; a held delivery is said with the thread's session.
  posts.length = 0;
  postReply = () => ({ status: 200, data: { delivered: true, hold: 'draft' } });
  sendBtn().listeners.click();
  await settle();
  assert.deepStrictEqual(JSON.parse(posts[1].body), { text: 'see this', images: ['img-2'] });
  assert.strictEqual(textArea().value, '');
  assert.strictEqual(composer().byClass('th-attach-item').length, 0);
  assert.ok(view.byClass('thh-notice')[0].textContent.includes('s1 터미널 입력창'));

  // An images-only message is a message; the notice belongs to its thread.
  posts.length = 0;
  postReply = () => ({ status: 200, data: { delivered: true } });
  composer().listeners.drop({ dataTransfer: { items: [], files: [png] }, preventDefault() {} });
  sendBtn().listeners.click();
  // A drop while the post is out is not taken (the post sends what it had).
  composer().listeners.drop({ dataTransfer: { items: [], files: [png] }, preventDefault() {} });
  await settle();
  assert.strictEqual(composer().byClass('th-attach-item').length, 0);
  assert.deepStrictEqual(JSON.parse(posts[1].body), { text: '', images: ['img-3'] });
  // newest shows the composer too, for the newest thread.
  page.open('newest');
  await settle();
  assert.strictEqual(composer().dataset.thread, 'th-4');
  assert.strictEqual(textArea().placeholder, 's1에게 보내기');

  // An unknown mode falls back to tree.
  page.open('nope');
  assert.strictEqual(page.state.view, 'tree');
  page.stop();
  console.log('ok');
})().catch(e => { console.error(e); process.exit(1); });
