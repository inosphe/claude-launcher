/* The rail's key chips ask for one redraw, not one per row (claunch-rw7tw.1).

   Every row's key chip starts its profile's pool read while that row is being
   built, and asks for a redraw when the read lands. Asked per row, a rail of
   214 rows left one whole pass open per row -- the live trace shows 387 at
   once -- and all of them awaited the same batched /api/sessions read. When
   that read settled, every one re-parsed the same body and re-merged the
   fleet in a single task measured at 6,089 ms (the arms and their numbers are
   in the round's report; the poll itself was not stopped for the measurement).

   Held here, against the shipped functions:

   * a fresh read of a profile whose read is already in flight joins that
     read instead of issuing a second one -- the rail asks once per row, and
     the answer is the same for every row of that profile;
   * many redraw requests in one turn start one pass, and a request that
     arrives after that pass has run starts the next one, so a rail is never
     left un-redrawn by the collapse;
   * the chip's callbacks reach the redraw through `requestSessionsRefresh`,
     not `refreshSessions` directly -- a per-row path back to a whole pass is
     what this file exists to keep out. */
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

// One pool read whose answer the check controls, and the count of reads the
// page actually issued.
function buildPools() {
  const issued = [];
  const settle = [];
  const api = (u) => {
    issued.push(u);
    return new Promise((resolve) => settle.push((payload, ok = true) => resolve({
      ok, status: ok ? 200 : 500,
      json: async () => payload,
      text: async () => JSON.stringify(payload),
    })));
  };
  const code = [
    "const keyPools = new Map();",
    "const keyPoolPending = new Map();",
    slice("fetchKeyPool"),
    "return { fetchKeyPool, keyPools, keyPoolPending };",
  ].join("\n");
  const scope = new Function("api", code)(api);
  return { ...scope, issued, settle };
}

// A zero-delay timer the check runs by hand, so "in one turn" is a fact the
// harness decides rather than a race.
function buildRefresh() {
  const timers = [];
  const code = [
    "let sessionsRefreshQueued = false;",
    slice("requestSessionsRefresh"),
    "return { requestSessionsRefresh };",
  ].join("\n");
  const scope = new Function("setTimeout", "refreshSessions", code)(
    (fn) => timers.push(fn),
    () => { scope.passes += 1; }
  );
  scope.passes = 0;
  const flush = () => { const t = timers.splice(0); t.forEach((fn) => fn()); };
  return { request: scope.requestSessionsRefresh, flush, timers,
           passes: () => scope.passes };
}

/* ---- 1. a stale pool read is joined, not repeated ---------------------- */
async function aFreshReadJoinsTheOneInFlight() {
  const pools = buildPools();
  const first = pools.fetchKeyPool("alpha", { fresh: true });
  const second = pools.fetchKeyPool("alpha", { fresh: true });
  const third = pools.fetchKeyPool("alpha");
  assert.strictEqual(pools.issued.length, 1,
    "three callers, one profile: one read goes out");
  assert.strictEqual(first, second, "a fresh caller joins the read in flight");
  assert.strictEqual(first, third, "and so does a cached caller");

  pools.settle.shift()({ pooled: true, keys: [{ id: "k1" }] });
  await first;
  assert.deepStrictEqual(pools.keyPools.get("alpha").data.pooled, true);

  // Once that read is done, a later fresh ask is a new read: the clock it is
  // checking has to be able to move.
  pools.fetchKeyPool("alpha", { fresh: true });
  assert.strictEqual(pools.issued.length, 2, "the next fresh ask reads again");

  // A different profile is its own question.
  pools.fetchKeyPool("beta");
  assert.strictEqual(pools.issued.length, 3, "another profile is another read");
}

/* ---- 2. one turn, one pass --------------------------------------------- */
async function manyRequestsInOneTurnStartOnePass() {
  const app = buildRefresh();
  for (let i = 0; i < 214; i++) app.request();
  assert.strictEqual(app.timers.length, 1, "214 rows, one deferred pass");
  assert.strictEqual(app.passes(), 0, "nothing is drawn in the asking turn");

  app.flush();
  assert.strictEqual(app.passes(), 1, "the turn's requests became one pass");

  // The collapse must not swallow the next turn's request: a read that lands
  // after the pass ran still owes a redraw.
  app.request();
  assert.strictEqual(app.timers.length, 1, "the next turn asks again");
  app.flush();
  assert.strictEqual(app.passes(), 2, "and gets its own pass");
}

/* ---- 3. the row asks through the collapser ----------------------------- */
function theChipDoesNotCallThePassDirectly() {
  const start = src.indexOf("function railKeyChip(");
  assert.ok(start > 0, "railKeyChip is in app.js");
  let depth = 0, end = -1;
  for (let j = src.indexOf(") {", start) + 2; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) { end = j + 1; break; } }
  }
  assert.ok(end > 0, "railKeyChip is balanced");
  const body = src.slice(start, end);
  assert.ok(!/refreshSessions\s*\(/.test(body),
    "the row's chip must not start a whole pass per row");
  assert.ok(/requestSessionsRefresh\s*\(/.test(body),
    "its read callbacks ask through requestSessionsRefresh");
}

(async () => {
  await aFreshReadJoinsTheOneInFlight();
  await manyRequestsInOneTurnStartOnePass();
  theChipDoesNotCallThePassDirectly();
  console.log("railkeypool_check ok");
})().catch((err) => { console.error(err); process.exit(1); });
