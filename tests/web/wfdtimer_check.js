/* A step's timed wait, as THE DRAWING has to say it.

   A step may declare `timer: {every, max, then, after}`: the run sits at
   the step and the daemon moves it — `every` seconds per fire, `max` fires
   per round. Until this check the schedule existed only in the engine's
   payload prose: the run page says a timed wait is waiting and when the
   next fire is, and says nothing at all the rest of the time, and the
   workflow graph did not know the concept at all — a `poll` loop drew its
   `wait` step as an ordinary box to an ordinary edge, and a reader looking
   at the picture could not tell the poll cadence from a run stalled
   forever. (Paced select options were already drawn — `wfdpace_check.js`
   pins those. A timer is a different mechanism: a paced branch is taken at
   most once per interval, a timed step is LEFT by the daemon's clock.)

   So this pins three things a regression would quietly undo:
     - a timed step's box carries the schedule whenever it is declared:
       the cadence and the fires-per-round budget, not only while the run
       happens to be parked on it,
     - while the run IS parked on it (`waiting_timer`), the next fire's
       local time rides at the end of the same line,
     - a timed step draws no dashed branch — pacing's visual mark is for
       paced options only, and borrowing it would make two different
       mechanisms read as one. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to, a + 1);
  if (a < 0 || b < 0 || b <= a) throw new Error(`cannot slice ${from} .. ${to}`);
  return src.slice(a, b);
}

const RULE = "/* ------------------------------------------------------------------ */";

const code = [
  // fmtOpensAt lives with the rail's badge; the timed step's "next <time>"
  // label and the held edge label both need it (same reason wfdpace names it).
  slice("/* A paced option's opening moment", "/* One line under each rail row"),
  // wfStepOrder + fmtPace + wfDiagramSvg (the row order is shared with the
  // timing diagram under the graph — see wfdpace_check.js for the same slice).
  slice("function escXml(", RULE),
].join("\n");

const ctx = {};
new Function(
  "exports",
  code + "\nObject.assign(exports, {fmtPace, fmtOpensAt, wfDiagramSvg});"
)(ctx);
const { fmtPace, fmtOpensAt, wfDiagramSvg } = ctx;

const fail = [];
function ok(cond, what) { if (!cond) fail.push(what); }

/* ---- the workflow under test ------------------------------------------ */
/* Shaped like brief's (and poll's) loop: an ordinary poll step, then the
   timed wait that hands the run back to it by fire and to end by budget. */
const WF = {
  name: "brief",
  start: "poll",
  steps: [
    { id: "poll", title: "브리핑", next: "wait" },
    {
      id: "wait",
      title: "Timed wait",
      timer: { every: 300, max: 22, then: "poll", after: "end" },
      next: "end",
    },
  ],
};

/* ---- declared, run not parked on it ----------------------------------- */
const idle = wfDiagramSvg(WF, { step_id: "poll", status: "step", visits: {} }, null);

ok(idle.includes("timed · every 5m · max 22"),
   "the timed step's box carries its schedule — cadence and fires budget");
ok(!/· next /.test(idle),
   "without a live wait in progress nothing claims a next fire");
ok(!idle.includes("wfd-edge paced"),
   "a timed step draws no dashed branch — pacing's mark belongs to paced options");
ok(!idle.includes("timed ·"), "the plain poll step gains no timer marks");

/* ---- parked on it: waiting_timer -------------------------------------- */
const LIVE = {
  step_id: "wait",
  status: "waiting_timer",
  fires: 3,
  max: 22,
  opens_at: "2026-08-28T12:15:00+00:00",
  visits: { wait: 2 },
};
const live = wfDiagramSvg(WF, LIVE, null);

ok(/· next \d{1,2}:\d{2}/.test(live),
   "while the run waits on the timer the next fire's local time is on the box");
ok(live.includes("timed · every 5m · max 22"),
   "...and the declared schedule still reads alongside it");
ok(/class="wfd-node current/.test(live),
   "the run standing on a timed step is drawn as current, not as working");
ok(!live.includes("holding"),
   "a timed wait borrows none of the paced hold's marks");

/* The schedule is a fact about the workflow, so any standing run sees it —
   a run that moved past the timed step still draws the box with its clock. */
const past = wfDiagramSvg(
  WF, { step_id: "poll", status: "step", visits: { wait: 1 } }, null
);
ok(past.includes("timed · every 5m · max 22"),
   "a step the run already left keeps its schedule on the box");

/* A cadence in hours still formats into the same slot. */
const HOURS = {
  name: "hourly", start: "a",
  steps: [
    { id: "a", timer: { every: 5400, max: 3, then: "a", after: "end" }, next: "end" },
  ],
};
const hourly = wfDiagramSvg(HOURS, { step_id: "a", status: "step", visits: {} }, null);
ok(hourly.includes("timed · every 1.5h · max 3"),
   `a 5400s timer formats as 1.5h in the box, got a different word`);

/* ---- the timed step stays a normal, selectable node -------------------- */
ok(/<g class="[^"]*" data-step="wait">/.test(idle),
   "the timed step keeps its node, clickable like any other step");

if (fail.length) {
  console.error(fail.join("\n"));
  process.exit(1);
}
console.log("wfdtimer_check.js: ok — no timed-step drawing regression");
