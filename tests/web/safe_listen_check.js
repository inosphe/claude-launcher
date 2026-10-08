/* Unit check for safe_listen.cjs: an unsafe port from the OS is closed and the
   server re-listens. No browser needed. Run: node tests/web/safe_listen_check.js */
const assert = require("node:assert/strict");
const http = require("node:http");
const { isUnsafePort, safeListen } = require("./safe_listen.cjs");

for (const port of [0, 6000, 6566, 6665, 6668, 6669, 10080]) {
  assert.equal(isUnsafePort(port), true, `${port} is on Chromium's list`);
}
for (const port of [80, 443, 3000, 8080, 6670, 49152]) {
  assert.equal(isUnsafePort(port), false, `${port} is allowed`);
}

// A stand-in server whose first listens land on unsafe ports.
function scripted(ports) {
  const calls = { listen: 0, close: 0 };
  let current = null, handlers = {};
  return {
    calls,
    once(ev, fn) { handlers[ev] = fn; },
    removeListener(ev) { delete handlers[ev]; },
    listen(p, host, cb) { assert.equal(p, 0); calls.listen++; current = ports.shift(); setImmediate(cb); },
    address() { return { port: current }; },
    close(cb) { calls.close++; setImmediate(cb); },
  };
}

(async () => {
  const s = scripted([6566, 6668, 41234]);
  assert.equal(await safeListen(s), 41234);
  assert.deepEqual(s.calls, { listen: 3, close: 2 }, "reopened once per unsafe port, kept the safe one open");

  const only = scripted([49999]);
  assert.equal(await safeListen(only), 49999);
  assert.deepEqual(only.calls, { listen: 1, close: 0 });

  await assert.rejects(safeListen(scripted(Array(5).fill(6000)), "127.0.0.1", 5), /no Chromium-safe port/);

  // A real server ends up on a safe port and is reachable.
  const real = http.createServer((req, res) => res.end("ok"));
  const port = await safeListen(real);
  assert.equal(isUnsafePort(port), false);
  assert.equal(real.address().port, port);
  real.close();
  console.log("safe_listen checks passed");
})().catch((e) => { console.error(e); process.exit(1); });
