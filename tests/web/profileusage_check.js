/* Profile usage page (profile-usage.js): a usage read renders whatever way
   the answer came back, and its values read the way `claunch usage` prints
   them (claunch-f5w4x.1).

   What has to hold:
     no headers   -- an answer from /api/batch has no `headers`; the read
                     takes the cache facts from the body and does not throw
                     ("Cannot read properties of undefined (reading 'get')").
     out of batch -- the read asks api() not to batch it.
     headers      -- an answer with headers and no body facts still reads.
     format       -- reset as "resets in 2h16m", only windows with use or a
                     reset time (all when none has either).
     next read    -- a read sets the key's next periodic read from the body's
                     next_at; the countdown reads "next read in 14:03" and
                     "due" once passed; a key past it is read again,
                     automatically, on the next tick (claunch-f5w4x.3).
     chart        -- the 5h and 7d lines from the samples, clamped to 0-100,
                     split where two samples are more than two periods apart;
                     a window the table does not name draws nothing.
     disk         -- sizes read as B / KB / MB. */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const src = fs.readFileSync(
  path.join(__dirname, '../../src/claude_launcher/web/static/profile-usage.js'), 'utf8');

const calls = [];
let answer = null;
const ctx = vm.createContext({
  globalThis: {}, Date, Number, String, Math, Set, Map, Promise, Error, Object,
  setInterval: () => 0, clearInterval: () => {},
  document: { getElementById: () => null },
  api: async (p, opts) => { calls.push([p, opts]); return answer; },
});
ctx.globalThis = ctx;
vm.runInContext(src, ctx);
const T = ctx.ProfileUsagePage._test;

const row = { key: 'nc/anthropic/claude', profile: 'nc' };
const body = {
  key: 'nc/anthropic/claude', profile: 'nc', provider: 'anthropic', harness: 'claude',
  source: 'ratelimit-headers', windows: [], cached: true, updated_at: 1792650000,
};

(async () => {
  // The shape app.js batchResponse() hands back: no headers.
  answer = { ok: true, status: 200, batched: true, json: async () => body };
  await T.readUsage(row, false);
  let got = T.state.values.get(row.key);
  assert.strictEqual(got.error, '', `batched read failed: ${got.error}`);
  assert.strictEqual(got.cached, true);
  assert.strictEqual(got.updatedAt, 1792650000);
  assert.strictEqual(calls[0][0], '/api/usage?profile=nc&refresh=auto');
  assert.strictEqual(calls[0][1].noBatch, true);

  // A direct fetch from an older daemon: the facts only in the headers.
  const headers = { 'X-Usage-Cached': 'false', 'X-Usage-Updated-At': '1792650100' };
  const facts = T.readFacts({ headers: { get: (k) => headers[k] ?? null } }, {});
  assert.deepStrictEqual([facts.cached, facts.updatedAt], [false, 1792650100]);

  // Reset text as cli._fmt_reset writes it.
  const now = Date.UTC(2026, 9, 22, 7, 0, 0);
  const at = (m) => new Date(now + m * 60000 + 30000);
  assert.strictEqual(T.resetText(at(136), now), 'resets in 2h16m');
  assert.strictEqual(T.resetText(at(5336), now), 'resets in 88h56m');
  assert.strictEqual(T.resetText(at(42), now), 'resets in 42m');
  assert.strictEqual(T.resetText(new Date(now - 1000), now), 'resetting');

  // Windows as cli._print_usage picks them.
  const used = { name: 'five_hour', utilization: 2, resets_at: null };
  const reset = { name: 'seven_day', utilization: 0, resets_at: '2030-01-01T00:00:00Z' };
  const idle = { name: 'seven_day_opus', utilization: 0, resets_at: null };
  assert.deepStrictEqual(T.shownWindows([used, reset, idle]).map((w) => w.name),
    ['five_hour', 'seven_day']);
  assert.deepStrictEqual(T.shownWindows([idle]).map((w) => w.name), ['seven_day_opus']);

  // The next periodic read rides in the body.
  answer = { ok: true, status: 200, json: async () => ({ ...body, next_at: 1792650900 }) };
  await T.readUsage(row, false);
  assert.strictEqual(T.state.nextAt.get(row.key), 1792650900);
  // A failed read waits one period.
  answer = { ok: false, status: 400, json: async () => ({ error: 'no login' }) };
  const before = Date.now() / 1000;
  await T.readUsage(row, true);
  const waited = T.state.nextAt.get(row.key) - before;
  assert.ok(waited >= T.state.period - 1 && waited <= T.state.period + 1, `waited ${waited}`);

  // Countdown text.
  assert.strictEqual(T.countdownText(1000 + 843, 1000 * 1000), 'next read in 14:03');
  assert.strictEqual(T.countdownText(1000 + 5, 1000 * 1000), 'next read in 0:05');
  assert.strictEqual(T.countdownText(999, 1000 * 1000), 'next read: due');
  assert.strictEqual(T.countdownText(undefined, 1000 * 1000), 'next read: due');

  // A key past its next read is read again (automatic) on the tick.
  T.state.open = true;
  T.state.targets = [{ ...row, supported: true }];
  T.state.nextAt.set(row.key, Date.now() / 1000 - 1);
  calls.length = 0;
  answer = { ok: true, status: 200, json: async () => ({ ...body, next_at: Date.now() / 1000 + 900 }) };
  T.tick();
  await new Promise((r) => setTimeout(r, 0));
  assert.deepStrictEqual(calls.map((c) => c[0]), ['/api/usage?profile=nc&refresh=auto']);
  T.tick();
  assert.strictEqual(calls.length, 1, 'a key inside its period is not read again');
  T.state.open = false;

  // Arrays built inside the vm context are another realm's: compare as JSON.
  const same = (a, b) => assert.strictEqual(JSON.stringify(a), JSON.stringify(b));
  // Chart series.
  const sample = (at, five, seven) => ({ at, windows: [
    { name: 'five_hour', utilization: five }, { name: 'seven_day', utilization: seven },
    { name: 'seven_day_opus', utilization: 1 }] });
  const series = T.chartSeries([sample(0, 2, 50), sample(900, 120, 51), sample(900 * 5, 3, 52)], 900);
  same(series.map((s) => s.label), ['5h', '7d']);
  same(series[0].runs, [[[0, 2], [900, 100]], [[4500, 3]]]);
  assert.strictEqual(series[1].runs.length, 2);
  same(T.chartSeries([{ at: 0, windows: [{ name: 'primary', utilization: 4 }] }], 900), []);

  // Sizes.
  assert.deepStrictEqual([T.sizeText(512), T.sizeText(2048), T.sizeText(3 * 1024 ** 2)],
    ['512 B', '2.0 KB', '3.0 MB']);

  console.log('profileusage_check: ok');
})().catch((err) => { console.error(err); process.exit(1); });
