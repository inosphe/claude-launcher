/* Chart page: the model behind #/chart (chart-page.js) -- which sessions it
   keeps, where their bars start and stop, and how project, group and range
   narrow them. */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const src = fs.readFileSync(
  path.join(__dirname, '../../src/claude_launcher/web/static/chart-page.js'), 'utf8');
const store = {};
const ctx = vm.createContext({
  globalThis: {}, document: {}, Date,
  localStorage: { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; } },
  setInterval: () => 0, clearInterval: () => {},
});
ctx.globalThis = ctx;
vm.runInContext(src, ctx);
const C = ctx.ChartPage;

const H = 3600000;
const now = Date.UTC(2026, 9, 13, 12, 0, 0);
const iso = (t) => new Date(t).toISOString();

// Categories match the rail's (app.js sessionCategory).
assert.strictEqual(C.category({ status: 'running' }), 'running');
assert.strictEqual(C.category({ status: 'exited', held_at: 'x' }), 'hold');
assert.strictEqual(C.category({ status: 'exited', paused_at: 'x' }), 'paused');
assert.strictEqual(C.category({ status: 'exited' }), 'killed');
assert.strictEqual(C.category({ status: 'exited', archived_at: 'x' }), 'archived');
assert.strictEqual(C.projectOf({}), 'default');
assert.strictEqual(C.projectOf({ project: ' gds ' }), 'gds');

// Spans: a running or held session lives to now; an exited one to its exit,
// an archived one without an exit to its archiving; no creation, no bar.
let sp = C.spanOf({ status: 'running', created_at: iso(now - 2 * H) }, now);
assert.deepStrictEqual([sp.start, sp.end, sp.alive], [now - 2 * H, now, true]);
sp = C.spanOf({ status: 'exited', held_at: 'x', created_at: iso(now - H) }, now);
assert.strictEqual(sp.alive, true);
assert.strictEqual(sp.end, now);
sp = C.spanOf({ status: 'exited', created_at: iso(now - 5 * H), exited_at: iso(now - 3 * H) }, now);
assert.deepStrictEqual([sp.end, sp.alive], [now - 3 * H, false]);
sp = C.spanOf({ status: 'exited', created_at: iso(now - 5 * H), archived_at: iso(now - 4 * H) }, now);
assert.strictEqual(sp.end, now - 4 * H);
sp = C.spanOf({ status: 'exited', created_at: iso(now - H), exited_at: iso(now - 2 * H) }, now);
assert.strictEqual(sp.end, sp.start, 'an exit before creation is clamped');
assert.strictEqual(C.spanOf({ status: 'running' }, now), null);

// Durations: two largest units.
assert.strictEqual(C.formatDuration(30000), '<1m');
assert.strictEqual(C.formatDuration(42 * 60000), '42m');
assert.strictEqual(C.formatDuration(2 * H), '2h');
assert.strictEqual(C.formatDuration(2 * H + 5 * 60000), '2h 5m');
assert.strictEqual(C.formatDuration(27 * H), '1d 3h');
assert.strictEqual(C.formatDuration(48 * H), '2d');
assert.strictEqual(C.formatDuration(NaN), '—');
assert.strictEqual(C.median([3, 1, 2]), 2);
assert.strictEqual(C.median([4, 1, 2, 3]), 2.5);
assert.strictEqual(C.median([]), null);
assert.strictEqual(C.ticks(0, 60, 6).length, 7);
assert.strictEqual(C.ticks(5, 5).length, 0);

const sessions = [
  { name: 'a', status: 'running', created_at: iso(now - 2 * H), project: 'p1', mesh: 'm1' },
  { name: 'b', status: 'exited', created_at: iso(now - 30 * H), exited_at: iso(now - 26 * H), project: 'p1', mesh: 'm2' },
  { name: 'c', status: 'exited', paused_at: 'x', created_at: iso(now - 10 * 24 * H), exited_at: iso(now - 9 * 24 * H), mesh: 'm1' },
  { name: 'd', status: 'exited', created_at: iso(now - 3 * H), exited_at: iso(now - H), archived_at: iso(now - H), project: 'p1', mesh: 'm1' },
  { name: 'e', status: 'running', created_at: iso(now - 40 * 24 * H), project: 'p1', mesh: 'm2' },
];
const groupValue = (g, s) => (g === 'mesh' ? s.mesh : 'ws');
const flows = { a: { version: 'v2', state: 'yours', label: 'improv · gate' },
                e: { version: 'v1', state: 'running', label: 'improv · step' } };
const names = (m) => Array.from(m.rows, (r) => r.name);
const build = (o) => C.buildModel(sessions, Object.assign({ now, groupValue, cflowOf: (n) => flows[n] || null }, o));

// Range: a bar is kept when it overlaps the window; archived only on request.
assert.deepStrictEqual(names(build({ range: '24h' })), ['a', 'e']);
assert.deepStrictEqual(names(build({ range: '7d' })), ['a', 'b', 'e']);
assert.deepStrictEqual(names(build({ range: '7d', archived: true })), ['a', 'd', 'b', 'e']);
assert.deepStrictEqual(names(build({ range: 'all' })), ['a', 'b', 'c', 'e']);
assert.strictEqual(build({ range: 'all' }).t0, now - 40 * 24 * H, 'the "all" axis starts at the earliest creation');
assert.strictEqual(build({ range: '24h' }).t0, now - 24 * H);
// An unknown range falls back to 7 days.
assert.deepStrictEqual(names(build({ range: 'bogus' })), ['a', 'b', 'e']);

// Project: the rail's rule, an unnamed project is "default".
assert.deepStrictEqual(names(build({ range: 'all', project: 'default' })), ['c']);
assert.deepStrictEqual(names(build({ range: 'all', project: 'p1' })), ['a', 'b', 'e']);

// Sort by lifetime, longest first.
assert.deepStrictEqual(names(build({ range: 'all', sort: 'lifetime' })), ['e', 'c', 'b', 'a']);

// Grouping: groups are counted before the pick narrows; rows run group by group.
let m = build({ range: 'all', group: 'mesh' });
assert.deepStrictEqual(Array.from(m.groups, (g) => [g.value, g.count]), [['m1', 2], ['m2', 2]]);
assert.deepStrictEqual(names(m), ['a', 'c', 'b', 'e']);
m = build({ range: 'all', group: 'mesh', groupPick: 'm2' });
assert.deepStrictEqual(names(m), ['b', 'e']);
assert.strictEqual(m.groupPick, 'm2');
assert.strictEqual(m.groups.length, 2, 'the picker still offers every group');
// A pick that names no group of this view shows them all.
m = build({ range: 'all', group: 'mesh', groupPick: 'gone' });
assert.strictEqual(m.groupPick, '');
assert.strictEqual(m.rows.length, 4);
// Without a grouping there are no groups and the pick is ignored.
m = build({ range: 'all', groupPick: 'm2' });
assert.strictEqual(m.groups.length, 0);
assert.strictEqual(m.rows.length, 4);

// Summary: per-category counts, alive count, cflow tallies, median lifetime.
m = build({ range: 'all', archived: true });
assert.strictEqual(m.summary.total, 5);
assert.deepStrictEqual(Object.assign({}, m.summary.counts),
  { running: 2, hold: 0, paused: 1, killed: 1, archived: 1 });
assert.strictEqual(m.summary.alive, 2);
assert.deepStrictEqual(Object.assign({}, m.summary.flow), { running: 1, yours: 1, other: 0, none: 3 });
assert.strictEqual(m.summary.medianLifetime, 4 * H);
assert.strictEqual(m.rows.find((r) => r.name === 'a').cflow.label, 'improv · gate');

// Bar geometry: proportional, clipped on the left, never invisible.
let g = C.barGeometry({ start: 25, end: 75 }, 0, 100);
assert.deepStrictEqual([g.left, g.width, g.clipped], [25, 50, false]);
g = C.barGeometry({ start: -50, end: 10 }, 0, 100);
assert.deepStrictEqual([g.left, g.width, g.clipped], [0, 10, true]);
g = C.barGeometry({ start: 100, end: 100 }, 0, 100);
assert.ok(g.width > 0 && g.left + g.width <= 100);
console.log('ok');
