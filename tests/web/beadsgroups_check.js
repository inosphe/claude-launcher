/* Independent status pages and selected-workspace totals. */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const src = fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/app.js'), 'utf8');
function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  assert(start >= 0, name);
  const head = src.slice(start - 6, start) === 'async ' ? start - 6 : start;
  let depth = 0;
  for (let n = src.indexOf(') {', start) + 2; n < src.length; n++) {
    if (src[n] === '{') depth++;
    if (src[n] === '}' && !--depth) return src.slice(head, n + 1);
  }
  throw new Error(name);
}
function el(tag, cls, text) {
  return { tag, cls, text, children: [], handlers: {},
    appendChild(n) { this.children.push(n); },
    addEventListener(k, fn) { this.handlers[k] = fn; },
    setAttribute(k, v) { this[k] = v; },
  };
}
const ctx = vm.createContext({ URLSearchParams, el, requests: [] });
vm.runInContext(`
let beadsOpen = true, beadsSection = 'board', beadsLoading = false, beadsMore = false;
let beadsPage = 0, beadsTotal = null, beadsStreamVersion = 0, beadsPri = null;
let beadsCache = null, beadsError = '', beadsSort = 'updated_at', beadsDirection = 'desc';
let beadsFilter = 'active', beadsLayout = 'board', beadsSession = '', beadsWorkspace = '/a';
let beadsLanePages = {};
const BEADS_PAGE_SIZE = 48, BEADS_LANE_SIZE = 12;
const BEADS_STATUSES = ['open', 'in_ready', 'in_progress', 'in_review', 'blocked', 'closed'];
const BEADS_ACTIVE = new Set(BEADS_STATUSES.slice(0, -1));
function renderBeads() {}
async function refreshBeadsDetail() {}
function refreshBeadsRelated() {}
function api(url) { return new Promise(resolve => requests.push({ url, resolve })); }
` + ['loadBeadsPage', 'beadsReadPage', 'beadsMergeLanes', 'beadsLanes',
     'beadsGoToLanePage', 'restartBeadsStream', 'beadsStatusQuery', 'beadsPageCount',
     'beadsStatusName', 'beadsStatusNote', 'beadsLanePager'].map(slice).join('\n'), ctx);
const run = code => vm.runInContext(code, ctx);
const flush = () => new Promise(resolve => setImmediate(resolve));
function respond(request, total = 37) {
  const q = new URL(request.url, 'http://localhost').searchParams;
  const status = q.get('status'), offset = Number(q.get('offset')), limit = Number(q.get('limit'));
  const count = Math.min(limit, Math.max(0, total - offset));
  request.resolve({ ok: true, json: async () => ({
    total: total + 500, has_more: true,
    boards: [
      { root: '/a', total, has_more: offset + count < total,
        issues: Array.from({ length: count }, (_, n) => ({ id: `${status}-${offset + n}`, status })),
        deps: [{ from: 'child', to: 'parent', type: 'parent-child' }] },
      { root: '/b', total: 500, has_more: true, issues: [], deps: [] },
    ],
  }) });
}
(async () => {
  run('restartBeadsStream()');
  assert.equal(ctx.requests.length, 5);
  for (const r of ctx.requests) {
    const q = new URL(r.url, 'http://localhost').searchParams;
    assert.equal(q.getAll('status').length, 1);
    assert.equal(q.get('limit'), '12');
    respond(r);
  }
  await flush();
  assert.equal(run('beadsCache.boards[0].issues.length'), 60);
  assert.equal(run('beadsCache.boards[0].lanes.in_progress.total'), 37);
  assert.equal(run('beadsCache.boards[0].deps.length'), 1);
  assert.equal(run('beadsCache.boards[1].lanes.open.total'), 500);
  run("beadsGoToLanePage('open', 1)");
  assert.equal(ctx.requests.length, 10);
  for (const r of ctx.requests.slice(5)) {
    const q = new URL(r.url, 'http://localhost').searchParams;
    assert.equal(q.get('offset'), q.get('status') === 'open' ? '12' : '0');
    respond(r);
  }
  await flush();
  assert.equal(run('beadsCache.boards[0].lanes.open.page'), 1);
  assert.equal(run('beadsCache.boards[0].lanes.in_progress.page'), 0);
  const pager = run("beadsLanePager('open', 12, beadsCache.boards[0].lanes.open)");
  assert.equal(pager.children[0].text, '13–24 of 37');
  assert(!pager.children[1].disabled);
  assert.equal(run("beadsLanePager('closed', 0, {page: 0, total: 0, more: false}).children[0].text"), 'No issues');
  assert(!src.includes('const BEADS_GROUPS'));
  run('loadBeadsPage()');
  for (const r of ctx.requests.slice(10, 15)) respond(r, 2);
  await flush();
  assert.equal(ctx.requests.length, 16);
  assert(ctx.requests[15].url.includes('offset=0'));
  respond(ctx.requests[15], 2);
  await flush();
  assert.equal(run('beadsCache.boards[0].lanes.open.page'), 0);
  run('loadBeadsPage()');
  const stale = ctx.requests.slice(16);
  run("beadsSession = 's810'; beadsFilter = 'closed'; restartBeadsStream()");
  const current = ctx.requests.at(-1);
  assert(current.url.includes('assignee=s810'));
  assert(current.url.includes('status=closed'));
  assert(current.url.includes('offset=0'));
  stale.forEach(r => respond(r, 999));
  await flush();
  assert.equal(run('beadsCache'), null);
  respond(current, 1);
  await flush();
  assert.equal(run('beadsCache.boards[0].issues.length'), 1);
  assert.equal(run('beadsCache.boards[0].issues[0].status'), 'closed');
  run("beadsLayout = 'tree'; restartBeadsStream()");
  respond(ctx.requests.at(-1), 1);
  await flush();
  assert.equal(run('beadsTotal'), 1);
  assert.equal(run('beadsMore'), false);
  assert.equal(run('beadsPageCount()'), 1);
  console.log('beadsgroups_check ok: independent pages and workspace totals');
})().catch(err => { console.error(err); process.exitCode = 1; });
