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
    slice("budgeted"),
    slice("batchResponse"),
    slice("batchable"),
    slice("batchQueue"),
    slice("batchFlush"),
    "const BATCH_MAX = 24;",
    "const BATCH_WINDOW_MS = 0;",   // the harness drives the flush by hand
    "const HTTP_BUDGET = 4;",
    "let httpInFlight = 0;",
    "const httpWaiting = [];",
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

/* ---- 3. a read that ends up alone still costs one connection ----------- */
/* The window is what gathers callers whose timers are merely out of phase;
   a read that nobody joined is sent as a batch of one. It costs the same
   single connection either way, and keeping one path here means there is one
   place where a read becomes a request. */
async function aReadThatEndsUpAloneIsStillOneRequest() {
  const app = build(async (u, opts) => {
    if (u === "/api/batch") {
      const answers = {};
      for (const p of JSON.parse(opts.body).paths) answers[p] = { path: p };
      return jsonResponse({ answers, errors: {} });
    }
    return jsonResponse({ path: u });
  });
  const resp = await app.api("/api/workspaces");
  assert.strictEqual(app.calls.length, 1, "one read, one request");
  assert.strictEqual(app.calls[0].url, "/api/batch");
  assert.deepStrictEqual(batchOf(app.calls[0]), ["/api/workspaces"]);
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

/* ---- 6. the page keeps room for the terminal --------------------------- */
/* Batching reduces how often the ceiling is approached; the budget is what
   makes the headroom a guarantee. A request past it waits in the page rather
   than in the browser's connection queue, where a WebSocket handshake would
   be stuck behind it with nothing to report. */
async function requestsPastTheBudgetWaitInThePage() {
  const release = [];
  const app = build(() => new Promise((resolve) => release.push(
    () => resolve(jsonResponse({ ok: true }))
  )));

  // Writes, so nothing is batched and each one is a request of its own.
  const sent = [];
  for (let i = 0; i < 7; i++) {
    sent.push(app.api(`/api/thing/${i}`, { method: "POST", body: "{}" }));
  }
  for (let i = 0; i < 5; i++) await new Promise((resolve) => setTimeout(resolve, 0));
  assert.strictEqual(app.calls.length, 4, "four in flight, three waiting");

  // One answers; the connection it held goes to the next in line, and no
  // more than that.
  release.shift()();
  for (let i = 0; i < 5; i++) await new Promise((resolve) => setTimeout(resolve, 0));
  assert.strictEqual(app.calls.length, 5, "one finished, one let through");

  // Draining takes several rounds: each one that answers lets a waiter start
  // a request of its own, which is the property being shown.
  for (let round = 0; round < 12 && release.length; round++) {
    while (release.length) release.shift()();
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  await Promise.all(sent);
  assert.strictEqual(app.calls.length, 7, "and every one of them was sent");
}

(async () => {
  await cadenceStaysWithTheCaller();
  await requestsPastTheBudgetWaitInThePage();
  await writesAndProbesGoOutAlone();
  await aReadThatEndsUpAloneIsStillOneRequest();
  await aFailedReadIsAloneInFailing();
  await anOlderDaemonStillServesThePage();
  console.log("batchpoll_check ok");
})().catch((err) => { console.error(err); process.exit(1); });
