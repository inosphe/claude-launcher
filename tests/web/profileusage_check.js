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
     dollars      -- samples with no 5h/7d window but dollar fields (a
                     Fireworks account, claunch-f5w4x.5) chart in dollars:
                     credit left and month spend, unclamped; a dollar window
                     is shown even at 0%; the axis tops out at a 1-2-5 step.
     disk         -- sizes read as B / KB / MB.
     api keys     -- an account's month split by API key (claunch-uqnek):
                     each key's label and share of the account total, and a
                     note when the account does not break spend down. */
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
  // A reading with no percentage and no money carries a line instead, and is
  // shown rather than dropped (opengateway-local, claunch-firtc).
  const local = { name: 'today', utilization: 0, resets_at: null, detail: '2 req · in 1.4k' };
  assert.deepStrictEqual(T.shownWindows([idle, local]).map((w) => w.name), ['today']);

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
  // OpenCode Go: 5h, calendar week and plan month on the same percentage axis.
  const go = T.chartSeries([{ at: 0, windows: [
    { name: 'five_hour', utilization: 41 }, { name: 'weekly', utilization: 17 },
    { name: 'monthly', utilization: 8 }] }], 900);
  same(go.map((s) => s.label), ['5h', 'week', 'month']);
  same(go[2].runs, [[[0, 8]]]);

  // Dollar charts.
  const fw = (at, spend, left) => ({ at, windows: [
    { name: 'monthly_spend', utilization: 0, used_dollars: spend },
    { name: 'credit', utilization: 10, used_dollars: 50, limit_dollars: 500, remaining_dollars: left }] });
  let plan = T.chartPlan([fw(0, 240, 260), fw(900, 247.84, 252.16)], 900);
  assert.strictEqual(plan.unit, 'usd');
  same(plan.series.map((s) => s.label), ['credit left', 'month spend']);
  same(plan.series[0].runs, [[[0, 260], [900, 252.16]]]);
  same(plan.series[1].runs, [[[0, 240], [900, 247.84]]]);
  assert.strictEqual(T.chartPlan([sample(0, 2, 50)], 900).unit, 'pct');
  // Without a credit entry only the spend line is drawn; null is no point.
  plan = T.chartPlan([{ at: 0, windows: [
    { name: 'monthly_spend', utilization: 0, used_dollars: 3, remaining_dollars: null }] }], 900);
  same(plan.series.map((s) => s.name), ['monthly_spend']);
  assert.deepStrictEqual([T.niceMax(247.84), T.niceMax(1000), T.niceMax(0.3), T.niceMax(0)],
    [500, 1000, 0.5, 1]);
  const spend = { name: 'monthly_spend', utilization: 0, resets_at: null, used_dollars: 0 };
  assert.deepStrictEqual(T.shownWindows([used, spend]).map((w) => w.name),
    ['five_hour', 'monthly_spend']);

  // The crosshair's viewBox x under the cursor (claunch-4mi9b): a box of the
  // drawing's own ratio maps linearly; a box wider than the drawing (the svg
  // capped by max-height) centres it, so its edges land on 0 and W.
  assert.strictEqual(T.viewX(110, { left: 10, width: 640, height: 180 }, 640, 180), 100);
  const wide = { left: 10, width: 1000, height: 220 };
  const scale = 220 / 180, pad = (1000 - 640 * scale) / 2;
  assert.ok(Math.abs(T.viewX(10 + pad, wide, 640, 180)) < 1e-9);
  assert.ok(Math.abs(T.viewX(10 + pad + 640 * scale, wide, 640, 180) - 640) < 1e-9);
  assert.ok(Math.abs(T.viewX(10 + pad + 36 * scale, wide, 640, 180) - 36) < 1e-9);

  // The chart's own hover path puts the crosshair on the sample under the
  // cursor in a box wider than the drawing: the right end of the plot is
  // the last sample, a quarter in is the sample nearest that time. The
  // width-ratio mapping this replaced picked an earlier sample at the right
  // end and missed the quarter point (claunch-4mi9b).
  const made = [];
  const element = (tag) => {
    const el = {
      tag, attrs: {}, children: [], listeners: {}, style: {}, textContent: '',
      classList: { add() {}, remove() {} },
      setAttribute(k, v) { this.attrs[k] = String(v); },
      append(...kids) { this.children.push(...kids); },
      addEventListener(type, fn) { this.listeners[type] = fn; },
    };
    made.push(el);
    return el;
  };
  ctx.document = { getElementById: () => null, createElement: element,
    createElementNS: (ns, tag) => element(tag), createTextNode: (text) => ({ text }) };
  const hourly = Array.from({ length: 9 }, (_, i) => fw(i * 3600, 200 + i, 300 - i));
  T.state.period = 900;
  T.chart(hourly);
  const plot = made.find((el) => el.tag === 'svg');
  const cross = made.find((el) => el.attrs.class === 'pu-cross');
  const hit = made.find((el) => el.tag === 'rect');
  // The same 1000x220 box as above: the drawing is scaled and centred.
  plot.getBoundingClientRect = () => wide;
  const L = 36, R = 34, W = 640;
  const hover = (vx) => {
    hit.listeners.mousemove({ clientX: wide.left + pad + vx * scale });
    return Number(cross.attrs.x1);
  };
  assert.ok(Math.abs(hover(W - R - 0.5) - (W - R)) < 1e-6, 'right end: last sample');
  const quarter = L + (W - L - R) / 4;
  assert.ok(Math.abs(hover(quarter) - quarter) < 1e-6, 'a quarter in: the sample at 2h');

  // API keys of one account (claunch-uqnek): a Fireworks account several
  // keys share reports one total whichever key asks, so the card splits it.
  // The label is the local profile that holds the key when the daemon can
  // tell, the console's own name next, the key id last; the share is against
  // the account's month, and an answer with no breakdown says so instead of
  // drawing nothing.
  const split = T.keyRows({ data: { source: 'fireworks-billing', windows: [
    { name: 'monthly_spend', utilization: 0, used_dollars: 5.97 }],
    api_keys: [
      { key_id: 'key_b', label: 'ds4-kdj', used_dollars: 3.44 },
      { key_id: 'key_a', label: 'ds4', used_dollars: 2.53 },
      { key_id: 'key_c', used_dollars: 0 }] } });
  const rows = split.children.slice(1);
  same(rows.map((el) => el.children[0].textContent), ['ds4-kdj', 'ds4', 'key_c']);
  same(rows.map((el) => el.children.find((c) => c.tag === 'small').textContent),
    ["58% of the account's month", "42% of the account's month",
     "0% of the account's month"]);
  assert.strictEqual(rows[2].children[1].textContent, '$0.00');
  const bare = T.keyRows({ data: { windows: [], api_keys: [] } });
  assert.strictEqual(bare.children.length, 1);
  assert.ok(bare.children[0].textContent.includes('did not break its spend down by API key'));
  // The daemon words the reason when it has one (claunch-uqnek.1): an empty
  // month, unreadable keys and an account that does not rate keys are three
  // answers, and the card shows the daemon's sentence rather than one of its
  // own. The sentence above is the fallback for an older daemon.
  const emptyMonth = T.keyRows({ data: { source: 'fireworks-billing', windows: [], api_keys: [],
    api_keys_note: 'No spend this month yet, so there is nothing to break down by API key.' } });
  assert.strictEqual(emptyMonth.children[0].textContent,
    'No spend this month yet, so there is nothing to break down by API key.');
  const unread = T.keyRows({ data: { source: 'fireworks-billing', windows: [], api_keys: [],
    api_keys_note: "The account's key-by-key spend could not be read; only the account total is shown." } });
  assert.ok(unread.children[0].textContent.includes('could not be read'));

  // Sizes.
  assert.deepStrictEqual([T.sizeText(512), T.sizeText(2048), T.sizeText(3 * 1024 ** 2)],
    ['512 B', '2.0 KB', '3.0 MB']);

  console.log('profileusage_check: ok');
})().catch((err) => { console.error(err); process.exit(1); });
