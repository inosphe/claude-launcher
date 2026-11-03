/* PR page: the model behind #/pr (pr-page.js) -- which rows it keeps, how
   they group into projects and days, the one status word a row shows, and
   the routing that opens it. */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const staticDir = path.join(__dirname, '../../src/claude_launcher/web/static');
const src = fs.readFileSync(path.join(staticDir, 'pr-page.js'), 'utf8');

// The page runs under two base paths -- "/" served directly and "/t/<name>/"
// through a relay tunnel (app.js: BASE + url()). The stub below is the tunnel,
// so a request that skips url() still records a path and fails the assertion
// after it, instead of vanishing into a 404 at runtime (claunch-ijkge.3).
const BASE = '/t/shared-main/';
const fetched = [];
const ctx = vm.createContext({
  globalThis: {}, Date,
  setInterval: () => 0, clearInterval: () => {},
  document: { getElementById: () => null, createElement: () => ({}) },
  url: (p) => BASE + String(p).replace(/^\/+/, ''),
  fetch: (u) => {
    fetched.push(u);
    return Promise.resolve({ ok: true, status: 200, json: async () => ({ prs: [], projects: [] }) });
  },
});
ctx.globalThis = ctx;
vm.runInContext(src, ctx);
const P = ctx.PrPage;
// arrays the module builds belong to the vm realm; compare their values
const plain = (v) => JSON.parse(JSON.stringify(v));

// -- wired into the app, not just present on disk -------------------------
const html = fs.readFileSync(path.join(staticDir, 'index.html'), 'utf8');
assert.ok(html.includes('id="pr-view"'), 'the page has a view container');
assert.ok(/<a href="#\/pr" data-page="pr">/.test(html), 'the nav link names the page it routes to');
assert.ok(html.includes('static/pr-page.js'), 'the page script is loaded');
const app = fs.readFileSync(path.join(staticDir, 'app.js'), 'utf8');
assert.ok(/pr: "pr-view"/.test(app), 'the view is registered');
assert.ok(app.includes('parts[0] === "pr"'), '#/pr parses as the page');
assert.ok(app.includes('globalThis.PrPage.open()'), 'routing opens it');
assert.ok(app.includes('globalThis.PrPage?.stop()'), 'leaving the page stops its poll');

// -- the status word: recorded values only, never an invented "open" ------
assert.strictEqual(P.prState({ draft: true, status: 'open' }), 'draft');
assert.strictEqual(P.prState({ status: 'open' }), 'open');
assert.strictEqual(P.prState({ status: 'OPENED' }), 'open');
assert.strictEqual(P.prState({ status: 'merged' }), 'merged');
assert.strictEqual(P.prState({ status: 'closed' }), 'closed');
assert.strictEqual(P.prState({ status: 'declined' }), 'closed');
assert.strictEqual(P.prState({ status: '' }), 'recorded');
assert.strictEqual(P.prState({}), 'recorded');

// -- the day a row is filed under, and the undated bucket -----------------
assert.strictEqual(P.dayOf({ at: '2026-11-02T10:00:00+00:00' }), '2026-11-02');
assert.strictEqual(P.dayOf({ at: '' }), '');
assert.strictEqual(P.dayOf({}), '');
assert.strictEqual(P.dayOf(null), '');

const rows = [
  { url: 'u1', number: 1, project: 'alpha', at: '2026-11-02T10:00:00+00:00' },
  { url: 'u2', number: 2, project: 'alpha', at: '2026-11-02T09:00:00+00:00' },
  { url: 'u3', number: 3, project: 'alpha', at: '2026-11-01T10:00:00+00:00' },
  { url: 'u4', number: 4, project: 'beta', at: '' },
  { url: 'u5', number: 5, project: 'beta', at: '2026-11-02T11:00:00+00:00' },
];

// -- grouping: projects by name, days newest first, undated last ----------
let model = plain(P.buildModel({ prs: rows }, {}));
assert.deepStrictEqual(model.projects.map((p) => p.project), ['alpha', 'beta']);
const alpha = model.projects[0];
assert.deepStrictEqual(alpha.dates.map((d) => d.date), ['2026-11-02', '2026-11-01']);
assert.deepStrictEqual(alpha.dates[0].prs.map((r) => r.number), [1, 2],
  'the order the daemon sent is kept inside a day');
assert.strictEqual(alpha.count, 3);
const beta = model.projects[1];
assert.deepStrictEqual(beta.dates.map((d) => d.date), ['2026-11-02', ''],
  'the undated bucket is last, not first');
assert.strictEqual(beta.dates[1].label, '날짜 미상');
assert.strictEqual(model.total, 5);

// -- filters: project and status -----------------------------------------
model = plain(P.buildModel({ prs: rows }, { project: 'beta' }));
assert.deepStrictEqual(model.projects.map((p) => p.project), ['beta']);
assert.strictEqual(model.total, 2);
model = plain(P.buildModel({ prs: rows }, { status: 'recorded' }));
assert.deepStrictEqual(model.projects.map((p) => p.project), ['alpha', 'beta'],
  'rows with no recorded status are the "recorded" bucket');
assert.strictEqual(model.total, 5);
model = plain(P.buildModel({ prs: rows }, { status: 'merged' }));
assert.strictEqual(model.total, 0, 'a filter the records do not carry empties the list');

// -- an empty or malformed answer is an empty model, not a crash ----------
assert.deepStrictEqual(plain(P.buildModel(null, {})), { projects: [], total: 0 });
assert.deepStrictEqual(plain(P.buildModel({}, {})), { projects: [], total: 0 });
assert.deepStrictEqual(plain(P.buildModel({ prs: 'nope' }, {})), { projects: [], total: 0 });
// a session with no project is shown under the daemon's own word for it
assert.strictEqual(plain(P.buildModel({ prs: [{ url: 'u', number: 1 }] }, {})).projects[0].project,
  'default');

// -- the path one PR lives at, and the four segments the daemon routes -----
// `<host>/<owner>/<name>` is a slug in the record and four segments in the
// route: aiohttp routes on the decoded path, so an encoded slash would be a
// separator by the time the match ran.
assert.deepStrictEqual(plain(P.splitRepo('github.ncsoft.com/RnIAgentDev/claunch')),
  { host: 'github.ncsoft.com', owner: 'RnIAgentDev', name: 'claunch' });
assert.strictEqual(P.splitRepo('only-two/parts'), null);
assert.strictEqual(P.splitRepo('a/b/c/d'), null);
assert.strictEqual(P.splitRepo('h/o/n ame'), null, 'a space is not a path segment');
assert.strictEqual(P.splitRepo(''), null);
assert.strictEqual(P.splitRepo(null), null);

assert.strictEqual(P.endpoint('github.ncsoft.com/RnIAgentDev/claunch', 805),
  '/api/prs/github.ncsoft.com/RnIAgentDev/claunch/805');
assert.strictEqual(P.endpoint('github.ncsoft.com/RnIAgentDev/claunch', 805, 'merge'),
  '/api/prs/github.ncsoft.com/RnIAgentDev/claunch/805/merge');
assert.strictEqual(P.endpoint('github.ncsoft.com/RnIAgentDev/claunch', 805, 'rebase-request'),
  '/api/prs/github.ncsoft.com/RnIAgentDev/claunch/805/rebase-request');
assert.strictEqual(P.endpoint('bad', 805), '', 'an unparseable repo makes no path');
assert.strictEqual(P.endpoint('h/o/n', 0), '', 'a PR with no number makes no path');
assert.strictEqual(P.endpoint('h/o/n', -3), '');

// -- the merge button's one input -----------------------------------------
assert.strictEqual(P.canMerge({ verdict: { can_merge: true } }), true);
assert.strictEqual(P.canMerge({ verdict: { can_merge: false } }), false);
assert.strictEqual(P.canMerge({}), false);
assert.strictEqual(P.canMerge(null), false);

// -- no request escapes the page's base path (the claunch-ijkge.3 defect) --
assert.ok(!/fetch\(\s*["']\//.test(src),
  'a fetch with an absolute path leaves the relay tunnel; wrap it in url()');

// -- and the two reads actually go through it ------------------------------
(async () => {
  fetched.length = 0;
  await P.load();
  assert.deepStrictEqual(fetched, ['/t/shared-main/api/prs'],
    'the list request keeps the tunnel prefix');

  fetched.length = 0;
  P.openDetail({ repo: 'github.ncsoft.com/RnIAgentDev/claunch', number: 805, url: 'u' });
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.deepStrictEqual(fetched, ['/t/shared-main/api/prs/github.ncsoft.com/RnIAgentDev/claunch/805'],
    'the detail request keeps the tunnel prefix');

  // a record with no repo cannot ask the daemon anything -- it must not send
  // a request built from a half-parsed path
  fetched.length = 0;
  P.openDetail({ repo: '', number: 7, url: 'u' });
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.deepStrictEqual(fetched, [], 'no repo, no request');
  P.closeDetail();
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
