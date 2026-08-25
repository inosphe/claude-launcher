/* Time-based gating, as the DRAWINGS have to say it.

   A select option may carry `interval`: the run may take that branch at most
   once per interval, and a choice made inside one is HELD until the window
   opens — the daemon's clock releases it, not a person. Until this check the
   pacing existed only in prose: the run page says it while a choice is held
   and says nothing the rest of the time, and the two pictures — the workflow
   graph and the flow card — did not know the concept at all. The card's was
   the worse half: `waiting_window` fell through flowState's ladder to
   "running", so a leader parked on a five-minute window read as a leader
   working.

   So this pins three things a regression would quietly undo:
     - a paced branch is dashed and carries its cadence WHENEVER it is
       declared, not only while it bites (nothing else tells a reader the
       branch is paced before it is held),
     - exactly one edge goes `held`, and it is the one the run named,
     - `held` is its own flow state, worded as agreed with the rail
       (s106: colour = session, shape = run state; the word is "held" and the
       releaser is the daemon) — and it is NOT in flowNeedsHuman, because a
       person cannot clear it and that list is the queue that calls them. */
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
  // fmtOpensAt lives with the rail's badge, but the held edge label and the
  // card corner both need it. s106 hit exactly this hole from the other side
  // — a rail check that first walked the held path died on the missing
  // function — so it is named here rather than left to luck.
  slice("/* A paced option's opening moment", "/* One line under each rail row"),
  slice("function escXml(", RULE),                  // fmtPace + wfDiagramSvg
  slice("function answerFellToUs(", "function shortenPath("),
  slice("function flowNeedsHuman(", "/* ---- drawing ---"), // + flowState(Word)
].join("\n");

const ctx = {};
new Function(
  "exports",
  code + "\nObject.assign(exports, {fmtPace, wfDiagramSvg, flowNeedsHuman," +
  " flowState, flowStateWord, FLOW_WORDS});"
)(ctx);
const { fmtPace, wfDiagramSvg, flowNeedsHuman, flowState, flowStateWord,
        FLOW_WORDS } = ctx;

const fail = [];
function ok(cond, what) { if (!cond) fail.push(what); }

/* ---- the workflow under test ------------------------------------------ */
/* Shaped like improv-leader's standby, which is where this feature actually
   runs: one agent-chosen select whose merge branch is paced and whose other
   branches are not. */
const WF = {
  name: "improv-leader",
  start: "standby",
  steps: [
    {
      id: "standby",
      title: "관제 대기",
      select: {
        prompt: "what now?",
        chooser: "agent",
        options: [
          { name: "integrate", next: "sweep", interval: 300 },
          { name: "hold", next: "standby" },
        ],
      },
    },
    { id: "sweep", title: "전체 스윕", verify: "npm test", next: null },
  ],
};

/* ---- fmtPace ----------------------------------------------------------- */
ok(fmtPace(300) === "5m", `300s reads as 5m, got ${fmtPace(300)}`);
ok(fmtPace(45) === "45s", `under a minute stays in seconds, got ${fmtPace(45)}`);
// A cadence that is not whole minutes still climbs — "1.5m" and "90s" cost a
// reader the same four characters, and staying in one unit per range is what
// makes a column of them comparable at a glance.
ok(fmtPace(90) === "1.5m", `90s reads as 1.5m, got ${fmtPace(90)}`);
ok(fmtPace(5400) === "1.5h", `5400s reads as 1.5h, got ${fmtPace(5400)}`);
// A missing cadence must not draw as "0s" or "NaNm" — it must draw as nothing.
ok(fmtPace(null) === "", "no cadence formats to nothing");
ok(fmtPace(0) === "", "zero formats to nothing");

/* ---- declared, but nothing held --------------------------------------- */
const idle = wfDiagramSvg(WF, { step_id: "standby", status: "step", visits: {} }, null);

ok(/class="wfd-edge paced"/.test(idle),
   "the paced branch is dashed while merely declared");
ok(idle.includes("every 5m"),
   "...and carries its cadence, which is the only way to learn it");
ok(!/wfd-edge[^"]*held/.test(idle),
   "nothing is held, so no edge claims to be");
ok(!idle.includes("holding"), "...and no step box claims to be either");
// The unpaced sibling must stay plain, or "dashed" stops meaning anything.
ok((idle.match(/class="wfd-edge paced"/g) || []).length === 1,
   "exactly one branch is paced — the other is left plain");
ok(idle.includes("select:agent · paced"),
   "the step's flag line says the property exists");

/* ---- held: the run is parked on that one branch ------------------------ */
const HELD = {
  step_id: "standby",
  status: "waiting_window",
  option: "integrate",
  interval: 300,
  opens_at: "2026-08-25T10:52:00+00:00",
  visits: { standby: 2 },
};
const held = wfDiagramSvg(WF, HELD, null);

ok(/class="wfd-edge paced held"/.test(held),
   "the held branch is drawn held");
ok((held.match(/wfd-edge paced held/g) || []).length === 1,
   "exactly one edge is held — not every paced one");
ok(/wfd-epace held/.test(held), "...and its label is marked with it");
ok(held.includes("held →"),
   "the held label uses the agreed word 'held' and points at the opening time");
ok(!held.includes("every 5m"),
   "a held branch says when it opens, not merely how often it may run");
ok(/class="wfd-node current holding"/.test(held),
   "the step box says the run is parked here, not working here");

/* A run standing on the step but NOT holding must not borrow the marks: this
   is the difference between "deciding" and "decided, and waiting on a clock". */
const deciding = wfDiagramSvg(
  WF, { step_id: "standby", status: "select", option: "integrate", visits: {} }, null
);
ok(!deciding.includes("holding"),
   "a run merely deciding at the step is not drawn as held");
ok(!/wfd-edge[^"]*held/.test(deciding), "...and neither is its branch");

/* The hold belongs to one step: the same option name on another step must not
   light up, which is what a name-only match would do. */
const TWO = {
  name: "two", start: "a",
  steps: [
    { id: "a", select: { prompt: "?", chooser: "agent",
        options: [{ name: "integrate", next: "b", interval: 300 }] } },
    { id: "b", select: { prompt: "?", chooser: "agent",
        options: [{ name: "integrate", next: null, interval: 300 }] } },
  ],
};
const two = wfDiagramSvg(TWO, { ...HELD, step_id: "b" }, null);
ok((two.match(/wfd-edge paced held/g) || []).length === 1,
   "the hold marks the branch of the step the run is on, not every same-named one");

/* ---- a workflow with no pacing is untouched --------------------------- */
const PLAIN = {
  name: "plain", start: "a",
  steps: [{ id: "a", next: "b" }, { id: "b", next: null }],
};
const plain = wfDiagramSvg(PLAIN, { step_id: "a", status: "step", visits: {} }, null);
ok(!plain.includes("paced"), "an unpaced workflow gains no pacing marks");
ok(!plain.includes("wfd-epace"), "...and no cadence labels");

/* ---- the flow card's state word --------------------------------------- */
ok(flowState(HELD) === "held",
   `waiting_window is its own state, got '${flowState(HELD)}'`);
// With a real responder on it: an ask that reached nobody is the operator's,
// which is a different branch of the same ladder and not what is under test.
ok(flowState({ status: "waiting_answer", ask: { asked: [{ handle: "s45" }] } })
     === "delegated",
   "a peer holding it is still a different state");
// The whole point: it used to fall through to "running".
ok(flowState(HELD) !== "running",
   "a run on the clock is not reported as a run that is going");
ok(flowNeedsHuman(HELD) === false,
   "held is NOT the operator's queue — the daemon's clock clears it");
ok(FLOW_WORDS.held === "held for its window",
   `the long word is the agreed one, got '${FLOW_WORDS.held}'`);
ok(/^held → /.test(flowStateWord("held", HELD)),
   `the card corner shortens to 'held → <time>', got '${flowStateWord("held", HELD)}'`);
// Without an opening time there is nothing to point at, and the long word
// must survive rather than degrade to "held → undefined".
ok(flowStateWord("held", { status: "waiting_window" }) === "held for its window",
   "with no opens_at the card falls back to the long word");
ok(flowStateWord("running", { status: "step" }) === "running",
   "every other state keeps the word it had");

/* ---- an exited session outranks the clock ----------------------------- */
// A run held for a window whose session has gone is not counting down to
// anything a person will see acted on; `stopped` is the fact that matters.
ok(flowState({ ...HELD, stopped: true }) === "stopped",
   "a stopped session outranks the hold, as it does every other live state");

if (fail.length) {
  console.error("wfdpace_check FAILED:\n  " + fail.join("\n  "));
  process.exit(1);
}
console.log("wfdpace_check ok");
