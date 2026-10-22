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
                     reset time (all when none has either). */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const src = fs.readFileSync(
  path.join(__dirname, '../../src/claude_launcher/web/static/profile-usage.js'), 'utf8');

const calls = [];
let answer = null;
const ctx = vm.createContext({
  globalThis: {}, Date, Number, String, Math, Set, Map, Promise, Error,
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

  console.log('profileusage_check: ok');
})().catch((err) => { console.error(err); process.exit(1); });
