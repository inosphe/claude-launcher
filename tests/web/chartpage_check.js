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
// Killed exclusion (claunch-xu9ti): the option drops killed records from the
// model and zeroes their summary count, like the archived switch beside it.
assert.deepStrictEqual(names(build({ range: 'all', excludeKilled: true })), ['a', 'c', 'e']);
const noKilled = build({ range: 'all', excludeKilled: true });
assert.strictEqual(noKilled.summary.total, 3);
assert.deepStrictEqual(Object.assign({}, noKilled.summary.counts),
  { running: 2, hold: 0, paused: 1, killed: 0, archived: 0 });
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
assert.deepStrictEqual(Object.assign({}, m.summary.flow),
  { running: 1, yours: 1, other: 0, none: 3, stalled: 0 });
assert.strictEqual(m.summary.medianLifetime, 4 * H);
assert.strictEqual(m.rows.find((r) => r.name === 'a').cflow.label, 'improv · gate');

// The cflow clock the rail row carries: `updated` (the run's last write) or,
// without it, the oldest active task's `entered_at` in epoch seconds.
assert.strictEqual(C.cflowChangedAt({ updated: iso(now - 5 * 60000) }), now - 5 * 60000);
assert.strictEqual(C.cflowChangedAt({ entered_at: (now - 9 * 60000) / 1000 }), now - 9 * 60000);
assert.strictEqual(C.cflowChangedAt({ updated: iso(now - 5 * 60000), entered_at: 0 }),
  now - 5 * 60000, 'updated wins over entered_at');
assert.strictEqual(C.cflowChangedAt({}), null);
assert.strictEqual(C.cflowChangedAt(null), null);
assert.strictEqual(C.cflowChangedAt({ updated: 'not a time', entered_at: 'x' }), null);

// A run in the same task for STALL_MS is marked; one a person owes never is
// (it is waiting on the reader, not stuck), and clock skew reads as "just now".
assert.strictEqual(C.STALL_MS, 30 * 60000);
let age = C.cflowAge({ state: 'running', changedAt: now - C.STALL_MS }, now);
assert.deepStrictEqual([age.elapsed, age.stalled], [C.STALL_MS, true]);
age = C.cflowAge({ state: 'running', changedAt: now - C.STALL_MS + 1 }, now);
assert.deepStrictEqual([age.elapsed, age.stalled], [C.STALL_MS - 1, false]);
age = C.cflowAge({ state: 'yours', changedAt: now - 5 * C.STALL_MS }, now);
assert.deepStrictEqual([age.elapsed, age.stalled], [5 * C.STALL_MS, false]);
age = C.cflowAge({ state: 'done', changedAt: now - 5 * C.STALL_MS }, now);
assert.strictEqual(age.stalled, false);
age = C.cflowAge({ state: 'running', changedAt: now + 60000 }, now);
assert.deepStrictEqual([age.elapsed, age.stalled], [0, false]);
const none = C.cflowAge(null, now);
assert.deepStrictEqual([none.elapsed, none.stalled], [null, false]);

// The summary counts only the running rows whose clock is old.
m = C.buildModel(sessions, { now, range: 'all', cflowOf: (n) => ({
  a: { version: 'v2', state: 'running', label: 'old', changedAt: now - 45 * 60000 },
  e: { version: 'v2', state: 'running', label: 'fresh', changedAt: now - 60000 },
})[n] || null });
assert.strictEqual(m.summary.flow.stalled, 1);
m = C.buildModel(sessions, { now, range: 'all', cflowOf: (n) => n === 'a'
  ? { version: 'v2', state: 'yours', label: 'owed', changedAt: now - 2 * C.STALL_MS } : null });
assert.strictEqual(m.summary.flow.stalled, 0, 'a run waiting on a person is not stalled');

// Axis scale: the default log axis keeps the whole window on one screen and
// places a time by its logarithmically compressed distance from now; the
// linear axis caps one screen at AXIS_CAP_MS and widens the grid for the rest.
assert.strictEqual(C.AXIS_CAP_MS, 7 * 24 * H);
assert.strictEqual(C.LOG_UNIT_MS, H);
const ALL = { axisStart: now - 40 * 24 * H, t1: now };
let ax = build({ range: 'all' });
assert.deepStrictEqual([ax.scale, ax.axisStart, ax.axisWidthPct], ['log', now - 40 * 24 * H, 100]);
assert.strictEqual(ax.t0, now - 40 * 24 * H, 'the window itself does not shrink');
ax = build({ range: 'all', scale: 'linear' });
assert.deepStrictEqual([ax.scale, ax.axisStart], ['linear', now - 40 * 24 * H]);
assert.ok(Math.abs(ax.axisWidthPct - (40 * 24 * H / C.AXIS_CAP_MS) * 100) < 1e-9);

// The widened grid keeps bars AND ticks in the window's coordinate system:
// the cap sets the scale (one screen = AXIS_CAP_MS), not a new axis start.
// claunch-c2emv review: an axisStart of `now - CAP` put every session older
// than the cap on the left edge under a "7일 전" tick.
assert.ok(Math.abs(C.positionOf(now - 40 * 24 * H, ax) - 0) < 1e-9, 'the oldest start is at the left edge');
assert.ok(Math.abs(C.positionOf(now - 20 * 24 * H, ax) - 50) < 1e-9, 'half the window is half the grid');
assert.ok(Math.abs(C.positionOf(now, ax) - 100) < 1e-9);
assert.ok(C.axisTicks(ax).length > C.axisTicks(build({ range: '7d', scale: 'linear' })).length,
  'a wider grid carries more ticks');
// The same bar inside the widened grid: a 20-day session spans half of it,
// and both its ends are on the axis rather than pinned to the left edge.
const mid = C.barGeometry({ start: now - 20 * 24 * H, end: now }, ax);
assert.ok(Math.abs(mid.left - 50) < 1e-9 && Math.abs(mid.width - 50) < 1e-9, JSON.stringify(mid));
assert.strictEqual(mid.clipped, false);
const oldest = C.barGeometry({ start: now - 40 * 24 * H, end: now }, ax);
assert.deepStrictEqual([oldest.left, Math.round(oldest.width), oldest.clipped], [0, 100, false]);
ax = build({ range: '7d', scale: 'linear' });
assert.deepStrictEqual([ax.axisStart, ax.axisWidthPct], [ax.t0, 100]);
ax = build({ range: '24h' });
assert.deepStrictEqual([ax.axisStart, ax.axisWidthPct], [ax.t0, 100], 'a short window needs no trick');

// Position: ends of the axis, monotone in time.
assert.ok(Math.abs(C.positionOf(now, ALL) - 100) < 1e-9);
assert.ok(Math.abs(C.positionOf(now - 40 * 24 * H, ALL) - 0) < 1e-9);
assert.ok(C.positionOf(now - H, ALL) > C.positionOf(now - 24 * H, ALL));
assert.ok(Math.abs(C.positionOf(25, { axisStart: 0, t1: 100 }) - 25) < 1e-9);

// The measured reason for the log default: on a 40-day axis a session made
// 14 minutes ago is a third of a percent wide linearly, and 3.1% on the log
// axis (see the table on claunch-c2emv).
const young = { start: now - 14 * 60000, end: now };
const logGeo = C.barGeometry(young, Object.assign({ scale: 'log' }, ALL));
assert.ok(logGeo.width > 2.5 && logGeo.width < 3.5, `log width ${logGeo.width}`);
assert.strictEqual(logGeo.clipped, false);
const linGeo = C.barGeometry(young, Object.assign({ scale: 'linear' }, ALL));
assert.ok(linGeo.width <= 0.25, `linear width ${linGeo.width} (the minimum sliver)`);

// The log axis labels round ages, not clock times.
const logTicks = C.axisTicks({ axisStart: now - 40 * 24 * H, t1: now, scale: 'log', axisWidthPct: 100 });
assert.ok(logTicks.length >= 4 && logTicks.length <= 8, `${logTicks.length} ticks`);
assert.ok(logTicks.every((t) => t < now && t >= now - 40 * 24 * H));

// Tick spacing is a question about pixels, not about a percentage of the
// axis: the split view's timeline column is a fraction of the page, and a
// label wider than the gap it sits in reads as one with its neighbour
// (claunch-c2emv review -- measured 6.4px and 7.8px overlaps at 200px).
const WEEK = { axisStart: now - 7 * 86400000, t1: now, scale: 'log', axisWidthPct: 100 };
const labelPx = (t) => `${C.formatDuration(now - t)} 전`.length * 6.2;
const keptOn = (trackPx) => C.axisTicks(WEEK, { trackPx });
const wide7 = keptOn(800);
const narrow7 = keptOn(200);
assert.ok(narrow7.length < wide7.length, `200px keeps ${narrow7.length}, 800px ${wide7.length}`);
assert.ok(narrow7.length >= 2, `${narrow7.length} labels on a 200px column`);
for (const [track, kept] of [[200, narrow7], [800, wide7]]) {
  for (let i = 1; i < kept.length; i++) {
    const gap = (C.positionOf(kept[i], WEEK) - C.positionOf(kept[i - 1], WEEK)) * (track / 100);
    const need = (labelPx(kept[i]) + labelPx(kept[i - 1])) / 2 + 6;
    assert.ok(gap >= need - 1e-9, `track ${track}: ${gap.toFixed(1)}px between labels needing ${need.toFixed(1)}px`);
  }
}

// Bar geometry: proportional, clipped on the left, never invisible.
const LIN = { axisStart: 0, t1: 100, scale: 'linear' };
let g = C.barGeometry({ start: 25, end: 75 }, LIN);
assert.deepStrictEqual([g.left, g.width, g.clipped], [25, 50, false]);
g = C.barGeometry({ start: -50, end: 10 }, LIN);
assert.deepStrictEqual([g.left, g.width, g.clipped], [0, 10, true]);
g = C.barGeometry({ start: 100, end: 100 }, LIN);
assert.ok(g.width > 0 && g.left + g.width <= 100);

// Role: the session's own field, then the role it holds in the grouped mesh,
// then its first membership, else "free-role" (the rail's spelling).
const mems = [{ mesh: 'm1', handle: 'h1', label: 'worker' },
              { mesh: 'm2', handle: 'h2', label: 'leader+merger' }];
assert.strictEqual(C.roleOf({ role: 'reviewer' }, mems, 'm2'), 'reviewer');
assert.strictEqual(C.roleOf({}, mems, 'm2'), 'leader+merger');
assert.strictEqual(C.roleOf({}, mems, ''), 'worker');
assert.strictEqual(C.roleOf({}, mems, 'gone'), 'worker');
assert.strictEqual(C.roleOf({}, [], ''), 'free-role');
m = C.buildModel([{ name: 'r1', status: 'running', created_at: iso(now - H) }],
  { now, range: 'all', meshesOf: (n) => (n === 'r1' ? mems : []) });
assert.strictEqual(m.rows[0].role, 'worker');
assert.strictEqual(m.rows[0].meshes.length, 2);
m = C.buildModel([{ name: 'r1', status: 'running', created_at: iso(now - H) }], { now, range: 'all' });
assert.strictEqual(m.rows[0].role, 'free-role');

// The split's share is clamped; junk falls back to the default.
assert.strictEqual(C.clampSplit(10), 30);
assert.strictEqual(C.clampSplit(95), 82);
assert.strictEqual(C.clampSplit(50), 50);
assert.strictEqual(C.clampSplit('x'), 64);

// ---- token series (claunch-x15q5) -----------------------------------------
// The payload carries SUMS per bucket; the drawing model turns them into a
// rate (tokens/s over the bucket) so that column width x height is tokens
// and the area under the curve is the window's total.
const payload = {
  window: '24h', bucket_s: 300, start: now - 2 * H, until: now,
  points: [
    { t: now - 2 * H, input: 100, cache_read: 2000, cache_write: 0, output: 0, total: 2100, requests: 2 },
    { t: now - H, input: 0, cache_read: 0, cache_write: 0, output: 0, total: 0, requests: 0 },
    { t: now - 300000, input: 10, cache_read: 20, cache_write: 30, output: 40, total: 100, requests: 1 },
  ],
  totals: { input: 110, cache_read: 2020, cache_write: 30, output: 40, total: 2200, requests: 3 },
  cache_share: 2020 / 2200, sessions: 2, unmetered: 1, ledger_since: '2026-10-01',
};
let sm = C.buildSeriesModel(payload);
assert.strictEqual(sm.width, 300);
assert.strictEqual(sm.widthMs, 300000);
assert.strictEqual(sm.points.length, 3);
assert.strictEqual(sm.points[0].rate, 2100 / 300);
assert.strictEqual(sm.points[1].rate, 0, 'a padded bucket is a gap, not dropped');
assert.strictEqual(sm.peak, 2100 / 300);
assert.strictEqual(sm.totals.total, 2200);
assert.strictEqual(sm.totals.requests, 3);
assert.strictEqual(sm.sessions, 2);
assert.strictEqual(sm.unmetered, 1);
assert.ok(Math.abs(sm.cacheShare - 2020 / 2200) < 1e-12);

// The rate is what makes the area the total: each column's area on the
// timeline is (bucket's span on the axis) x (its rate), and summed over a
// linear axis that is the tokens spent -- the property the ticket asked for.
const lin = { axisStart: now - 2 * H, t1: now, scale: 'linear' };
let area = 0;
for (const p of sm.points) {
  const span = C.seriesSpan(p, sm, lin);
  area += ((span.right - span.left) / 100) * ((sm.until - sm.start) / 1000) * p.rate;

}
assert.ok(Math.abs(area - sm.totals.total) < 1, `area ${area} is the total ${sm.totals.total}`);

// The span sits on the same axis the session bars use.
const p0 = C.seriesSpan(sm.points[0], sm, lin);
assert.ok(Math.abs(p0.left - 0) < 1e-9);
assert.ok(Math.abs(p0.right - (300 / (2 * 3600)) * 100) < 1e-9);
const p2 = C.seriesSpan(sm.points[2], sm, lin);
assert.ok(Math.abs(p2.left - C.positionOf(now - 300000, lin)) < 1e-9);
// The last bucket is cut at `until`, not drawn a whole width past now.
const clipped = { axisStart: now - 2 * H, t1: now, scale: 'linear' };
assert.ok(C.seriesSpan(sm.points[2], sm, clipped).right <= 100);

// Junk and absence are null, never a chart of zeros.
assert.strictEqual(C.buildSeriesModel(null), null);
assert.strictEqual(C.buildSeriesModel({}), null);
assert.strictEqual(C.buildSeriesModel({ points: [] }), null);
assert.strictEqual(C.buildSeriesModel({ points: [{ t: now, total: 5 }] }), null, 'no bucket width, no model');

// Token shorthand matches app.js usageShort's three-significant-figure rule.
assert.strictEqual(C.formatTokens(999), '999');
assert.strictEqual(C.formatTokens(1234), '1.23k', 'under 10k keeps two decimals, as usageShort does');
assert.strictEqual(C.formatTokens(45000), '45.0k');
assert.strictEqual(C.formatTokens(1200000), '1.20M');
assert.strictEqual(C.formatTokens(3660000000), '3.66B');
assert.strictEqual(C.formatRate(0), '0 tok/s');
assert.strictEqual(C.formatRate(12.5), '13 tok/s', 'whole tokens, as usageShort rounds them');
assert.deepStrictEqual(Array.from(C.SERIES_PARTS, (p) => p[0]),
  ['input', 'cache_read', 'cache_write', 'output']);


// The chart's own four parts in the order the Stats page stacks them, and the
// bar geometry the axes feed (a chart that asked seriesSpan for a bucket
// width in milliseconds must not get one in seconds).
assert.deepStrictEqual(Array.from(C.SERIES_PARTS, (p) => p[1]),
  ['fresh input', 'cache read', 'cache write', 'output']);
const half = C.buildSeriesModel({ bucket_s: 60, points: [{ t: 0, total: 60 }],
  until: 60000, start: 0 });
assert.strictEqual(half.points[0].rate, 1);
assert.strictEqual(half.widthMs, 60000);
assert.ok(Math.abs(C.seriesSpan(half.points[0], half,
  { axisStart: 0, t1: 60000, scale: 'linear' }).right - 100) < 1e-9,
  'a 60s bucket of a 60s axis spans all of it');

// ---- per-session tokens on the rows (claunch-mlrga) -----------------------
// The rows print the figure the Stats page's table prints for the session: the
// same payload carries it, so the column and the chart above it are one read.
const rowsPayload = {
  window: '24h', bucket_s: 300, start: now - 2 * H, until: now,
  points: [{ t: now - 2 * H, total: 0 }],
  totals: { total: 0, requests: 0 },
  by_session: {
    a: { input: 10, cache_read: 20, cache_write: 0, output: 5, total: 35, requests: 2 },
    b: { input: 0, cache_read: 0, cache_write: 0, output: 0, total: 0, requests: 0 },
    junk: null,
  },
};
const rm = C.buildSeriesModel(rowsPayload);
assert.strictEqual(rm.bySession.get('a').total, 35);
assert.strictEqual(rm.bySession.get('a').requests, 2);
assert.strictEqual(rm.bySession.get('b').total, 0,
  'a measured session with nothing in the window is a zero');
assert.ok(!rm.bySession.has('junk'), 'a malformed entry is dropped, not half-read');
assert.ok(!rm.bySession.has('oauth'), 'a session the ledger never saw is absent, not zero');
// The total falls back to the four parts when the payload omits it.
const partsOnly = C.buildSeriesModel({ bucket_s: 60, points: [{ t: 0, total: 60 }],
  by_session: { c: { input: 1, cache_read: 2, output: 3 } } });
assert.strictEqual(partsOnly.bySession.get('c').total, 6);

// buildModel carries each row's figure, counts what it could not read apart
// from what was never measured, and sums only the rows it is showing.
const tokenMap = { a: { total: 35, requests: 2 }, b: { total: 0, requests: 0 } };
const tm = C.buildModel(sessions, { now, range: 'all',
  tokensOf: (n) => (n in tokenMap ? tokenMap[n] : null) });
const byName = (m, n) => m.rows.find((r) => r.name === n);
assert.strictEqual(byName(tm, 'a').tokens.total, 35);
assert.deepStrictEqual(Object.assign({}, tm.summary.tokens),
  { total: 35, requests: 2, metered: 2, unmetered: 2, unread: 0 });
// The sort a reader uses to find the biggest spender.
assert.deepStrictEqual(Array.from(C.buildModel(sessions, { now, range: 'all',
  sort: 'tokens', tokensOf: (n) => (n in tokenMap ? tokenMap[n] : null) }).rows,
  (r) => r.name), ['a', 'b', 'c', 'e']);
// A tie falls back to the name, so the order is stable between polls.
assert.deepStrictEqual(Array.from(C.buildModel(sessions, { now, range: 'all',
  sort: 'tokens', tokensOf: () => ({ total: 5 }) }).rows, (r) => r.name),
  ['a', 'b', 'c', 'e']);
// No series read at all is "not read yet" (blank cells), not "unmetered".
const unread = C.buildModel(sessions, { now, range: 'all' });
assert.strictEqual(unread.rows[0].tokens, undefined);
assert.deepStrictEqual(Object.assign({}, unread.summary.tokens),
  { total: 0, requests: 0, metered: 0, unmetered: 0, unread: unread.rows.length });

console.log('ok');
