/* Real packs-pane.js (claunch-6esft.1): the three sections, the preview a
   "view" press fetches, send and drop bodies behind a confirm, the held mark,
   and that daemon text goes in as text. */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.dataset = {}; this.textContent = ''; this.parent = null; }
  append(...items) { for (const i of items) { if (typeof i === 'object') i.parent = this; this.children.push(i); } }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  setAttribute(name, value) { this.attributes[name] = value; }
  contains(other) { for (let n = other; n; n = n.parent) if (n === this) return true; return false; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null; }
}

function walk(n, out = []) { out.push(n); for (const c of n.children || []) if (typeof c === 'object') walk(c, out); return out; }
function text(n) { return walk(n).map(e => e.textContent).join(' '); }
function rowOf(host, key) { return walk(host).find(e => e.tag === 'li' && e.dataset.pack === key); }
function button(row, label) { return walk(row).find(e => e.tag === 'button' && e.textContent === label); }
const flush = () => new Promise(r => setTimeout(r, 0));

const calls = [];
let held = {};
const sent = [];

async function api(raw, opts = {}) {
  const url = new URL(raw, 'http://test');
  const method = opts.method || 'GET', body = opts.body ? JSON.parse(opts.body) : null;
  calls.push({path: url.pathname, pack: url.searchParams.get('pack'), method, body});
  let result = {};
  if (url.pathname === '/api/sessions/w1/rebrief' && method === 'GET') result = {block: 'brief\nline two'};
  else if (url.pathname === '/api/sessions/w1/rebrief') result = {delivered: true};
  else if (url.pathname === '/api/sessions/w1/packs' && method === 'GET' && url.searchParams.get('pack')) {
    result = {block: `# claunch prompt pack: ${url.searchParams.get('pack')}\n<b>body</b>`};
  } else if (url.pathname === '/api/sessions/w1/packs' && method === 'GET') {
    result = {
      session: 'w1',
      given: [{id: 'beads', text_id: 'aaa111', source: 'package', lines: 40, ticked: true, title: 'Beads', changed: true}],
      available: [
        {id: 'mcp-qa', kind: 'mcp', source: 'package', lines: 30, text_id: 'bbb222', title: 'QA',
         ...(held['mcp-qa'] ? {held: held['mcp-qa']} : {})},
        {id: 'retrospective', kind: 'on-demand', source: 'package', lines: 20, text_id: 'ccc333', title: '<img onerror=bad()>'},
      ],
      sent: [...sent].reverse(),
    };
  } else if (url.pathname === '/api/sessions/w1/packs' && method === 'POST') {
    if (body.pack === 'mcp-qa') held['mcp-qa'] = {text_id: 'bbb222', how: 'mcp', at: '2026-10-20T01:02:03Z'};
    const row = {pack: body.pack, how: body.pack === 'mcp-qa' ? 'mcp' : 'on-demand', text_id: 'x', at: '2026-10-20T01:02:03Z', delivered: true};
    sent.push(row);
    result = {ok: true, ...row};
  } else if (url.pathname === '/api/sessions/w1/packs' && method === 'DELETE') {
    const had = !!held[url.searchParams.get('pack')];
    delete held[url.searchParams.get('pack')];
    result = {ok: true, dropped: had};
  } else return {ok: false, status: 404, json: async () => ({error: 'no route'})};
  return {ok: true, json: async () => result};
}

const confirms = [];
let confirmAnswer = true;
const ctx = vm.createContext({
  document: {createElement: tag => new Element(tag)},
  api, URL, console, confirm: (m) => { confirms.push(m); return confirmAnswer; },
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/packs-pane.js'), 'utf8'), ctx);

(async () => {
  const target = new Element('div');
  ctx.PacksPane.mount(target, 'w1');
  await flush(); await flush();
  const host = target.children[0];
  assert.strictEqual(host.className, 'packs-host');
  const headings = walk(host).filter(e => e.tag === 'h4').map(e => e.textContent);
  assert.deepStrictEqual(headings, ['Briefing', 'In the opening', 'Not in the opening', 'Sent']);
  assert.ok(text(rowOf(host, 'briefing')).includes('2 lines'));
  const beads = text(rowOf(host, 'beads'));
  assert.ok(beads.includes('ticked at creation') && beads.includes('id aaa111') && beads.includes('file edited since'), beads);
  assert.ok(button(rowOf(host, 'beads'), 'send again'));
  assert.ok(text(rowOf(host, 'mcp-qa')).includes('MCP tool guide'));
  assert.ok(!button(rowOf(host, 'mcp-qa'), 'drop'), 'nothing held yet');
  // Daemon text is a text node, never markup.
  assert.ok(walk(host).some(e => e.textContent === '<img onerror=bad()>'));
  assert.ok(!('innerHTML' in host));
  assert.ok(text(host).includes('nothing sent from here yet'));

  // view: fetches the block the send would type and shows it; hide closes it.
  button(rowOf(host, 'mcp-qa'), 'view').onclick();
  await flush(); await flush();
  const pre = walk(rowOf(host, 'mcp-qa')).find(e => e.tag === 'pre');
  assert.ok(pre && pre.textContent.includes('<b>body</b>'), 'preview is the daemon block, as text');
  assert.ok(calls.some(c => c.method === 'GET' && c.pack === 'mcp-qa'));
  button(rowOf(host, 'mcp-qa'), 'hide').onclick();
  assert.ok(!walk(rowOf(host, 'mcp-qa')).some(e => e.tag === 'pre'));
  // The briefing preview needs no fetch: it is the block already loaded.
  const before = calls.length;
  button(rowOf(host, 'briefing'), 'view').onclick();
  assert.strictEqual(calls.length, before);
  assert.strictEqual(walk(rowOf(host, 'briefing')).find(e => e.tag === 'pre').textContent, 'brief\nline two');

  // A refused confirm sends nothing.
  confirmAnswer = false;
  await button(rowOf(host, 'mcp-qa'), 'send').onclick();
  assert.ok(!calls.some(c => c.method === 'POST'));
  confirmAnswer = true;

  // send: POST {pack}, then the row is held and offers drop; the log fills.
  await button(rowOf(host, 'mcp-qa'), 'send').onclick();
  await flush();
  const post = calls.find(c => c.method === 'POST');
  assert.deepStrictEqual(JSON.parse(JSON.stringify(post.body)), {pack: 'mcp-qa'});
  assert.ok(confirms.at(-1).includes('prompt pack mcp-qa') && confirms.at(-1).includes('w1'));
  assert.ok(text(host).includes('prompt pack mcp-qa sent'));
  assert.ok(text(rowOf(host, 'mcp-qa')).includes('held · id bbb222'));
  assert.ok(button(rowOf(host, 'mcp-qa'), 'send again') && button(rowOf(host, 'mcp-qa'), 'drop'));
  assert.ok(text(host).includes('2026-10-20 01:02:03 · mcp-qa · mcp'));

  // The briefing goes to the rebrief route, without a body.
  await button(rowOf(host, 'briefing'), 'send').onclick();
  assert.ok(calls.some(c => c.method === 'POST' && c.path === '/api/sessions/w1/rebrief' && c.body === null));

  // drop: DELETE ?pack=, then the held mark and the button go.
  await button(rowOf(host, 'mcp-qa'), 'drop').onclick();
  await flush();
  assert.ok(calls.some(c => c.method === 'DELETE' && c.pack === 'mcp-qa'));
  assert.ok(text(host).includes('prompt pack mcp-qa dropped'));
  assert.ok(!button(rowOf(host, 'mcp-qa'), 'drop'));

  // A failing route shows its error as an alert.
  const errTarget = new Element('div');
  ctx.PacksPane.mount(errTarget, 'ghost');
  await flush(); await flush();
  const alert = walk(errTarget).find(e => e.attributes.role === 'alert');
  assert.ok(alert && alert.textContent === 'no route');

  // unmount removes the host.
  ctx.PacksPane.unmount();
  assert.strictEqual(errTarget.children.length, 0);
  console.log('packspane_check ok');
})().catch(e => { console.error(e); process.exit(1); });
