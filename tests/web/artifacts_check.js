/* Artifacts page: size/age formatting, the ordering, and what counts as
   unmeasured (artifacts-page.js). */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const src = fs.readFileSync(
  path.join(__dirname, '../../src/claude_launcher/web/static/artifacts-page.js'), 'utf8');
const ctx = vm.createContext({ globalThis: {}, document: {}, Date, Number, String, Math, Set });
ctx.globalThis = ctx;
vm.runInContext(src, ctx);
const page = ctx.ArtifactsPage;

assert.strictEqual(page.formatBytes(0), '0 B');
assert.strictEqual(page.formatBytes(1023), '1023 B');
assert.strictEqual(page.formatBytes(1024), '1.0 KB');
assert.strictEqual(page.formatBytes(1536), '1.5 KB');
assert.strictEqual(page.formatBytes(10 * 1024), '10 KB');
assert.strictEqual(page.formatBytes(1024 ** 3), '1.0 GB');
// A directory nobody has walked yet says so; a cache miss is not a zero.
assert.strictEqual(page.formatBytes(undefined), '미측정');
assert.strictEqual(page.formatBytes(null), '미측정');

const now = Date.parse('2026-10-23T12:00:00Z');
assert.strictEqual(page.age('2026-10-23T11:59:30Z', now), '방금 전');
assert.strictEqual(page.age('2026-10-23T11:58:00Z', now), '2분 전');
assert.strictEqual(page.age('2026-10-23T10:00:00Z', now), '2시간 전');
assert.strictEqual(page.age('2026-10-20T12:00:00Z', now), '3일 전');
assert.strictEqual(page.age('', now), '');
assert.strictEqual(page.age('garbage', now), '');

const sessions = [
  { name: 's995', live: false, category: 'archived' },
  { name: 's99', live: true, category: 'running' },
  { name: 's10', live: false, category: 'killed' },
];
// A live session is the one a cleanup can act on, so it comes first; the rest
// sort by the number in the name, not by its string.
assert.deepStrictEqual(Array.from(page.sortedSessions(sessions), s => s.name),
  ['s99', 's10', 's995']);
assert.strictEqual(page.sortedSessions(undefined).length, 0);

const withItems = [
  { name: 's1', live: true, cwd: '/work/one', category: 'running',
    items: [{ path: '/t/s1w', kind: 'sweep' }] },
  { name: 's2', live: false, cwd: '/work/two', category: 'killed',
    items: [{ path: '/t/s2c1012000000', kind: 'run' }] },
];
assert.deepStrictEqual(Array.from(page.visibleSessions({ sessions: withItems }, ''), s => s.name),
  ['s1', 's2']);
assert.deepStrictEqual(Array.from(page.visibleSessions({ sessions: withItems }, 'SWEEP'), s => s.name),
  ['s1']);
assert.deepStrictEqual(Array.from(page.visibleSessions({ sessions: withItems }, '/work/two'), s => s.name),
  ['s2']);
assert.strictEqual(page.visibleSessions({ sessions: withItems }, 'nothing').length, 0);

// A session with an empty scratch and no cleanup record is hidden by
// default; an item, a non-empty scratch or a cleanup record brings it back.
const empties = [
  { name: 'idle', live: false, scratch: { path: '/t/e', exists: true, entries: 0 } },
  { name: 'has-entries', live: false, scratch: { path: '/t/h', entries: 3 } },
  { name: 'has-items', live: false, scratch: { path: '/t/i', entries: 0 },
    items: [{ path: '/t/i/x', kind: 'pytest' }] },
  { name: 'has-schedule', live: false, scratch: { path: '/t/s', entries: 0 },
    cleanup: { scheduled: { attempts: 1 }, receipts: [] } },
  { name: 'has-receipt', live: false, scratch: { path: '/t/r', exists: false },
    cleanup: { scheduled: null, receipts: [{ plan_id: 'p' }] } },
];
assert.deepStrictEqual(Array.from(page.visibleSessions({ sessions: empties }, ''), s => s.name),
  ['has-entries', 'has-items', 'has-receipt', 'has-schedule']);
assert.strictEqual(page.visibleSessions({ sessions: empties }, '', true).length, 5);

// Each category has its own switch (the reader asked for two, not one), and
// both are off by default so the page opens on the list it always showed.
const kinds = [
  { name: 'live-one', live: true, category: 'running',
    items: [{ path: '/t/a', kind: 'sweep' }] },
  { name: 'dead-one', live: false, category: 'killed',
    items: [{ path: '/t/b', kind: 'sweep' }] },
  { name: 'old-one', live: false, category: 'archived',
    items: [{ path: '/t/c', kind: 'sweep' }] },
];
const all = ['live-one', 'dead-one', 'old-one'];
const names = (options) => Array.from(
  page.visibleSessions({ sessions: kinds }, '', false, options), s => s.name);
assert.deepStrictEqual(names({}), all);
assert.deepStrictEqual(names({ excludeKilled: true }), ['live-one', 'old-one']);
assert.deepStrictEqual(names({ excludeArchived: true }), ['live-one', 'dead-one']);
assert.deepStrictEqual(names({ excludeKilled: true, excludeArchived: true }), ['live-one']);
assert.strictEqual(page.hiddenByCategory({ sessions: kinds }, {}), 0);
assert.strictEqual(page.hiddenByCategory({ sessions: kinds },
  { excludeKilled: true, excludeArchived: true }), 2);

// The bar reads the walk's own accounting: cached sizes cannot answer it,
// because a forced walk leaves the old number in place until it re-walks.
// The vm context has its own Object.prototype, so each field is compared.
const bars = (job) => {
  const state = page.progress(job === undefined ? {} : { job });
  return [state.total, state.done, state.started, state.finished];
};
assert.deepStrictEqual(bars({ total: 4, done: 1, started: 'x' }), [4, 1, true, false]);
// A done count past the total (a stale job the page kept) reads as full.
assert.deepStrictEqual(bars({ total: 0, done: 9, started: 'x' }), [0, 0, true, false]);
assert.deepStrictEqual(bars(undefined), [0, 0, false, false]);
assert.deepStrictEqual(bars({ total: 2, done: 2, finished: 'y' }), [2, 2, false, true]);

const data = {
  measurable: ['/t/scratch', '/t/s1w', '/t/orphan'],
  sessions: [{
    name: 's1',
    scratch: { path: '/t/scratch', bytes: 100 },
    items: [
      { path: '/t/scratch/pytest-1', area: 'scratch' },   // inside the scratch row
      { path: '/t/s1w', bytes: 200 },
    ],
  }],
  orphans: [{ path: '/t/orphan', error: 'permission denied' }],
};
// Only paths the daemon would walk count; a scratch entry is not one, and a
// row that already has a size or a recorded error is not asked for again.
assert.deepStrictEqual(Array.from(page.unmeasured(data)), []);
assert.deepStrictEqual(Array.from(page.measurable(data)), ['/t/scratch', '/t/s1w', '/t/orphan']);
assert.deepStrictEqual(
  Array.from(page.unmeasured({ ...data, orphans: [{ path: '/t/orphan' }] })), ['/t/orphan']);
assert.deepStrictEqual(Array.from(page.unmeasured(null)), []);
console.log('ok');
