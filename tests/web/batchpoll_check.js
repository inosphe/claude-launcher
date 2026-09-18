/* The reads that happen together go out on one connection.

   A browser holds a small number of connections to one server — Firefox's
   default is six — and a request in flight owns one for its whole life. The
   dashboard's tick asked for nine paths at once, which took all six, and a
   WebSocket needs a connection of its own: its handshake sat in the queue,
   was never sent, and the terminal never came up. The daemon saw nothing to
   refuse, which is why every record it kept read as calm
   (claunch-restart-disconnect-banner-12p2).

   What is held here is the part that must not become a scheduler. Batching
   is a matter of transport: it notices that several callers asked in the
   same turn and gives them one request. It never decides when anything is
   read, so a caller on a slower period is simply absent from the batches it
   skips — that property is the first check below.

   The rest are the ways this could quietly cost a reader something it had
   before: a write must never be folded in, one read must not pay for a batch
   it does not need, a failed path must not take the others down, and a
   daemon too old to know the route must still be usable. */
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  const head = src.lastIndexOf("async ", start) === start - 6 ? start - 6 : start;
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

/* ---- the harness ------------------------------------------------------- */
/* `api` is sliced in whole, so the batching decision under test is the one
   the page actually makes. fetch is a stub that records every request. */
function build(handler) {
  const calls = [];
  const scope = {
    calls,
    setTimeout,
    url: (p) => p,
    relogin: async () => false,
    showAuth: () => { scope.authShown = true; },
    fetch: async (u, opts = {}) => {
      calls.push({ url: u, method: (opts.method || "GET").toUpperCase(), opts });
      return handler(u, opts, calls.length);
    },
  };
  const code = [
    slice("api"),
    slice("batchResponse"),
    slice("batchable"),
    slice("batchQueue"),
    slice("batchFlush"),
    "const BATCH_MAX = 24;",
    "let batchPending = null;",
    "return { api, batchable, batchFlush };",
  ].join("\n");
  const make = new Function(
    "url", "fetch", "relogin", "showAuth", "setTimeout", "scope", code
  );
  Object.assign(scope, make(
    scope.url, scope.fetch, scope.relogin, scope.showAuth, scope.setTimeout, scope
  ));
  return scope;
}

function jsonResponse(payload, ok = true, status = 200) {
  return { ok, status, json: async () => payload, text: async () => JSON.stringify(payload) };
}

function batchOf(call) {
  return JSON.parse(call.opts.body).paths;
}

/* ---- 1. the caller keeps its own cadence ------------------------------- */
/* Two reads asked for together travel together; a read that is not due is
   not in the batch, because nothing here knows or decides its period. */
async function cadenceStaysWithTheCaller() {
  const app = build(async (u, opts) => {
    if (u === "/api/batch") {
      const answers = {};
      for (const p of JSON.parse(opts.body).paths) answers[p] = { path: p };
      return jsonResponse({ answers, errors: {} });
    }
    return jsonResponse({ path: u });
  });

  const tick = await Promise.all([
    app.api("/api/sessions"),
    app.api("/api/mesh?view=rail"),
    app.api("/api/cflow"),
  ]);
  assert.strictEqual(app.calls.length, 1, "three reads, one request");
  assert.strictEqual(app.calls[0].url, "/api/batch");
  assert.deepStrictEqual(
    batchOf(app.calls[0]).sort(),
    ["/api/cflow", "/api/mesh?view=rail", "/api/sessions"]
  );
  // Each caller gets its own answer back, under the path it asked for.
  assert.deepStrictEqual(await tick[0].json(), { path: "/api/sessions" });
  assert.deepStrictEqual(await tick[1].json(), { path: "/api/mesh?view=rail" });

  // The next tick: one part skipped its turn, so it is simply not there.
  app.calls.length = 0;
  await Promise.all([app.api("/api/sessions"), app.api("/api/cflow")]);
  assert.deepStrictEqual(
    batchOf(app.calls[0]).sort(), ["/api/cflow", "/api/sessions"]
  );
}

/* ---- 2. what must never be folded in ----------------------------------- */
async function writesAndProbesGoOutAlone() {
  const app = build(async () => jsonResponse({ ok: true }));
  assert.ok(app.batchable("/api/sessions", {}), "a read is batchable");
  assert.ok(!app.batchable("/api/sessions", { method: "POST" }), "a write is not");
  assert.ok(
    !app.batchable("/api/sessions", { method: "GET", body: "x" }),
    "a GET carrying a body is not"
  );
  // The liveness probe has to answer while everything else is refused, and a
  // batch cannot carry a batch.
  assert.ok(!app.batchable("/api/health", {}), "the health probe is not");
  assert.ok(!app.batchable("/api/batch", {}), "the batch route is not");
  // Not this daemon's own API, so not this daemon's batch.
  assert.ok(!app.batchable("https://example.com/api/sessions", {}));
  // The fallback flag, so a retry after a failed batch cannot re-queue.
  assert.ok(!app.batchable("/api/sessions", { noBatch: true }));

  await app.api("/api/sessions", { method: "POST", body: "{}" });
  assert.strictEqual(app.calls.length, 1);
  assert.strictEqual(app.calls[0].url, "/api/sessions");
  assert.strictEqual(app.calls[0].method, "POST");
}

/* ---- 3. one read does not pay for a batch ------------------------------ */
async function aLoneReadGoesStraightOut() {
  const app = build(async (u) => jsonResponse({ path: u }));
  const resp = await app.api("/api/workspaces");
  assert.strictEqual(app.calls.length, 1);
  assert.strictEqual(app.calls[0].url, "/api/workspaces", "no batch for one read");
  assert.deepStrictEqual(await resp.json(), { path: "/api/workspaces" });
}

/* ---- 4. one bad path does not take the others down --------------------- */
async function aFailedReadIsAloneInFailing() {
  const app = build(async (u, opts) => {
    if (u === "/api/batch") {
      const paths = JSON.parse(opts.body).paths;
      const answers = {};
      for (const p of paths) if (p !== "/api/broken") answers[p] = { path: p };
      return jsonResponse({ answers, errors: { "/api/broken": "404 Not Found" } });
    }
    return jsonResponse({ path: u });
  });

  const [good, bad, alsoGood] = await Promise.all([
    app.api("/api/sessions"), app.api("/api/broken"), app.api("/api/cflow"),
  ]);
  assert.ok(good.ok && alsoGood.ok, "the reads that worked still answer");
  assert.deepStrictEqual(await good.json(), { path: "/api/sessions" });
  assert.ok(!bad.ok, "the one that did not is not pretended to have");
  assert.strictEqual(bad.status, 502);
}

/* ---- 5. a daemon that has never heard of the route --------------------- */
/* New assets are served by an old daemon after an upgrade until it restarts;
   the page must keep working, one connection per read as before. */
async function anOlderDaemonStillServesThePage() {
  const app = build(async (u) => {
    if (u === "/api/batch") return jsonResponse({ error: "not found" }, false, 404);
    return jsonResponse({ path: u });
  });

  const [a, b] = await Promise.all([
    app.api("/api/sessions"), app.api("/api/cflow"),
  ]);
  assert.deepStrictEqual(await a.json(), { path: "/api/sessions" });
  assert.deepStrictEqual(await b.json(), { path: "/api/cflow" });
  const tried = app.calls.map((c) => c.url);
  assert.ok(tried.includes("/api/batch"), tried.join(","));
  assert.ok(tried.includes("/api/sessions") && tried.includes("/api/cflow"), tried.join(","));
}

(async () => {
  await cadenceStaysWithTheCaller();
  await writesAndProbesGoOutAlone();
  await aLoneReadGoesStraightOut();
  await aFailedReadIsAloneInFailing();
  await anOlderDaemonStillServesThePage();
  console.log("batchpoll_check ok");
})().catch((err) => { console.error(err); process.exit(1); });
