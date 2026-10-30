/* The dashboard tick must not die (claunch-8vl1s.1).

   A person reported a `leader stopped` card still on screen after the session
   was back, while /api/leader-faults listed nothing. The card is sticky and
   the poll is what takes it down, so the poll had stopped running: `pollTick`
   refused every tick while `polling` was true, and `polling` came down only
   in the tick's own `finally`. A read that never settles therefore stopped
   the dashboard for good, and it is the sticky cards -- the ones with no
   timeout of their own -- that stay up as the evidence.

   The tick now keeps `visiblePoll`'s escape (POLL_STUCK_MS, claunch-02w8s):
   a tick that has been out longer than that no longer holds the poll, and a
   token keeps the late one from clearing the flag the newer tick holds.

   Pinned here against the real declarations and `pollTick` sliced out of the
   shipped app.js, with time, the log and the read all belonging to the
   harness:
     - a read inside POLL_STUCK_MS still holds the poll (no stacking);
     - a read out past it no longer does, and the re-arm is logged;
     - the tick that comes after it holds the poll again;
     - the late read finishing does not open the poll the newer tick holds,
       and once that one settles the next tick runs. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8"
);

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

// Registered before anything is awaited: the tick this harness starts last is
// deliberately never settled. A run that stops at an `await` is a failure and
// not a pass, so `finished` is what says the checks below actually ran -- the
// first version of this file printed its pass line while hung on exactly that.
let finished = false;
process.on("exit", (code) => {
  if (failures) { console.log(`${failures} check(s) failed`); process.exitCode = 1; }
  else if (!finished) { console.log("the harness stopped before its last check"); process.exitCode = 1; }
  else if (!code) console.log("all poll-stuck checks passed");
});

/* The tick's state is declared beside it, and sliced rather than retyped: a
   harness that writes `let polling = false` itself would keep agreeing with
   app.js after the real one stopped being false. */
const start = src.indexOf("const DASHBOARD_POLL_MS = 5000;");
if (start < 0) throw new Error("cannot locate DASHBOARD_POLL_MS");
const fn = src.indexOf("async function pollTick()", start);
if (fn < 0) throw new Error("cannot locate pollTick");
let depth = 0;
let end = -1;
for (let j = src.indexOf("{", fn); j < src.length; j++) {
  if (src[j] === "{") depth += 1;
  else if (src[j] === "}") { depth -= 1; if (!depth) { end = j + 1; break; } }
}
if (end < 0) throw new Error("cannot find the end of pollTick");
const block = src.slice(start, end);
for (const name of ["polling", "pollSince", "pollToken", "hiddenPolledAt"]) {
  if (!block.includes(name)) throw new Error(`sliced pollTick without ${name}`);
}

/* The escape's length is read off the shipped constant, so the harness moves
   its clock to whatever that value is. */
const stuck = src.match(/const POLL_STUCK_MS = (\d+);/);
if (!stuck) throw new Error("cannot locate POLL_STUCK_MS");
const POLL_STUCK_MS = Number(stuck[1]);

function world() {
  const clock = { now: 1000000 };
  const log = [];
  const reads = [];
  const ctx = {
    document: { hidden: false },
    Date: { now: () => clock.now },
    POLL_STUCK_MS,
    authOpen: () => false,
    linkLog: (kind, detail) => log.push({ kind, detail }),
    // Every read stays out until the harness settles that one.
    pollOnce: () => new Promise((resolve) => { reads.push(resolve); }),
  };
  vm.createContext(ctx);
  vm.runInContext(`${block}\nthis.pollTick = pollTick;`, ctx);
  const settle = (i) => { const r = reads[i]; if (r) reads[i] = null; if (r) r(); };
  return { ctx, clock, log, reads, settle };
}

(async () => {
  const w = world();
  const first = w.ctx.pollTick();
  check("a tick runs the read", w.reads.length === 1, w.reads.length);

  await w.ctx.pollTick();
  check("a tick while the read is out starts nothing", w.reads.length === 1, w.reads.length);
  check("nothing is logged while the read is young", w.log.length === 0, w.log);

  w.clock.now += POLL_STUCK_MS - 1;
  await w.ctx.pollTick();
  check("a read just inside POLL_STUCK_MS keeps the poll closed", w.reads.length === 1, w.reads.length);

  w.clock.now += 1;
  const second = w.ctx.pollTick();
  check("a read out past POLL_STUCK_MS no longer holds the poll", w.reads.length === 2, w.reads.length);
  check("the re-arm is written to the link log",
        w.log.length === 1 && w.log[0].kind === "poll.stuck", w.log);
  check("the log carries how long the tick was out",
        w.log.length === 1 && w.log[0].detail && w.log[0].detail.ms === POLL_STUCK_MS, w.log);

  await w.ctx.pollTick();
  check("the newer tick holds the poll again", w.reads.length === 2, w.reads.length);

  // The tick that outlived its escape settles now. It must not clear a flag
  // it no longer holds: that is the stacking the token exists to prevent.
  w.settle(0);
  await first;
  await w.ctx.pollTick();
  check("a late read finishing does not open the poll it no longer holds",
        w.reads.length === 2, w.reads.length);

  w.settle(1);
  await second;
  const third = w.ctx.pollTick();
  check("once the read settles the next tick runs", w.reads.length === 3, w.reads.length);
  // `third` stays out on purpose: the harness is done, and settling it would
  // only exercise the case above.
  third.catch(() => {});
  finished = true;
})().catch((err) => {
  console.log(`FAIL the harness threw — ${err && err.stack ? err.stack : err}`);
  process.exitCode = 1;
});

/* Why this tick is the one that matters for that card: the removal is inside
   the batch this tick awaits. If the card's refresh ever moves off it, the
   escape above stops covering the thing it was written for, and this says so
   instead of the harness going on checking a tick nobody's cards ride on. */
{
  const once = src.indexOf("async function pollOnce()");
  const batch = once < 0 ? "" : src.slice(once, src.indexOf("await Promise.all(refreshes)", once));
  check("the leader-fault refresh is one of the tick's batches",
        /refreshLeaderFaults\(\)/.test(batch), batch.length);
  check("the tick is what runs pollOnce", /await pollOnce\(\)/.test(block));
}
