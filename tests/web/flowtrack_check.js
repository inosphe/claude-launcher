/* The flow view's track builder, run against the real functions in app.js.

   The track is a workflow's state machine squeezed onto a line, and the whole
   claim of the view is that it is the SAME machine the run page draws — same
   order, same loops, same end. That claim is checkable, so it is checked
   here: flowOrder against the order wfDiagramSvg actually emits, and the rest
   against the states a run can be in (mid-run, blocked, finished, and pointed
   at a step the snapshot has never heard of). */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

/* `to` is searched from `from` onwards: an end marker only has to be unique
   *after* the start, which lets the cheap ones (a section rule) be used. */
function slice(from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to, a + 1);
  if (a < 0 || b < 0 || b <= a) throw new Error(`cannot slice ${from} .. ${to}`);
  return src.slice(a, b);
}

const RULE = "/* ------------------------------------------------------------------ */";

const code = [
  slice("function escXml(", RULE),                       // + wfDiagramSvg
  slice("const FLOW = {", "let flowMesh"),
  slice("function flowMetrics(", "/* The steps of a workflow"),
  slice("function flowOrder(", "/* Blocked on a HUMAN"), // + flowTrack
  slice("function flowNeedsHuman(", "const FLOW_WORDS"), // + flowState
].join("\n");

const ctx = {};
new Function(
  "exports",
  code + "\nObject.assign(exports, {wfDiagramSvg, flowMetrics, flowOrder," +
  " flowTrack, flowNeedsHuman, flowState});"
)(ctx);
const { wfDiagramSvg, flowMetrics, flowOrder, flowTrack, flowNeedsHuman,
        flowState } = ctx;

let failures = 0;
function check(what, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${what}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

/* A workflow with everything the track has vocabulary for: a gate, a verify,
   a user branch, a loop back to an earlier step, and a termination. */
const WF = {
  name: "review", start: "plan",
  steps: [
    { id: "plan", next: "build" },
    { id: "build", gate: "ready?", verify: "pytest -q", next: "judge" },
    { id: "judge", select: { prompt: "good?", chooser: "user", options: [
      { name: "ship", next: "ship" }, { name: "again", next: "build" },
    ] } },
    { id: "ship" },
  ],
};

/* --- the claim that makes the strip readable -------------------------- */
{
  const drawn = [...wfDiagramSvg(WF, {}, null).matchAll(/data-step="([^"]+)"/g)]
    .map((m) => m[1]).filter((id) => id !== "end");
  check("the strip numbers steps exactly as the run page stacks them",
        flowOrder(WF).join() === drawn.join(), { strip: flowOrder(WF), rows: drawn });
}

/* an unreachable step is shown, at the tail, rather than quietly dropped */
{
  const orphaned = { start: "a", steps: [{ id: "a" }, { id: "lost" }] };
  check("an orphan step still appears", flowOrder(orphaned).join() === "a,lost",
        flowOrder(orphaned));
}

/* --- shape ------------------------------------------------------------ */
{
  const t = flowTrack(WF, {});
  check("one pip per step, plus the end it can terminate at",
        t.pips.map((p) => p.id).join() === "plan,build,judge,ship,end",
        t.pips.map((p) => p.id));
  check("a select reads as a branch", t.pips[2].kind === "select", t.pips[2]);
  check("gate and verify hang off the step that has them",
        t.pips[1].gate === true && t.pips[1].verify === true, t.pips[1]);
  check("a step with neither claims neither",
        !t.pips[0].gate && !t.pips[0].verify, t.pips[0]);
  // Only edges the rail does not already imply: judge -> build is the loop.
  check("the loop is the only arc drawn",
        JSON.stringify(t.arcs) === JSON.stringify([{ from: 2, to: 1, back: true }]),
        t.arcs);
}

/* a workflow that never terminates grows no end pip — it would be a lie */
{
  const looping = {
    start: "a",
    steps: [{ id: "a", next: "b" }, { id: "b", next: "a" }],
  };
  const t = flowTrack(looping, {});
  check("no end pip without a termination",
        t.pips.map((p) => p.id).join() === "a,b", t.pips.map((p) => p.id));
  check("the way back is an arc", t.arcs.length === 1 && t.arcs[0].back === true,
        t.arcs);
}

/* a `next` naming a step that is not there costs an edge, not the picture */
{
  const dangling = { start: "a", steps: [{ id: "a", next: "nowhere" }] };
  const t = flowTrack(dangling, {});
  check("a dangling next draws no edge and no end",
        t.pips.length === 1 && t.arcs.length === 0, t);
}

/* --- where the run is ------------------------------------------------- */
{
  const t = flowTrack(WF, {
    status: "waiting_approval", step_id: "build", visits: { plan: 1, build: 1 },
  });
  check("behind is visited", t.pips[0].state === "visited", t.pips[0]);
  check("here is current", t.pips[1].state === "current", t.pips[1]);
  check("ahead is untouched", t.pips[2].state === "ahead" &&
        t.pips[3].state === "ahead", t.pips.map((p) => p.state));
  check("the end of an unfinished run is not lit",
        t.pips[4].state === "ahead", t.pips[4]);
  check("current is where the run is", t.current === 1, t.current);
  check("the graph is the run's own", t.offGraph === false);
}

{
  const t = flowTrack(WF, {
    status: "done", step_id: "ship",
    visits: { plan: 1, build: 2, judge: 2, ship: 1 },
  });
  check("a finished run sits on the end, not on its last step",
        t.pips.map((p) => p.state).join() ===
          "visited,visited,visited,visited,current",
        t.pips.map((p) => p.state));
  check("a revisited step carries its count", t.pips[1].visits === 2, t.pips[1]);
}

{
  const t = flowTrack(WF, {
    status: "aborted", step_id: "build", visits: { plan: 1, build: 1 },
  });
  check("an aborted run lights nothing as current",
        t.current === -1 && !t.pips.some((p) => p.state === "current"),
        t.pips.map((p) => p.state));
}

/* The graphs are shared per workflow@cwd, so a re-run over an edited YAML can
   land on a step this snapshot has never heard of. Saying so beats drawing a
   track with nothing lit, which reads as "not started". */
{
  const t = flowTrack(WF, { status: "step", step_id: "ghost", visits: {} });
  check("a step outside the snapshot is called out",
        t.offGraph === true && t.current === -1, t);
}

/* --- who is actually waiting on a person ------------------------------ */
{
  const cases = [
    [{ status: "waiting_approval" }, true, "blocked"],
    [{ status: "waiting_selection" }, true, "blocked"],
    [{ status: "select", chooser: "user" }, true, "blocked"],
    // the agent's own branch: it must not read as a queue for the operator
    [{ status: "select", chooser: "agent" }, false, "deciding"],
    [{ status: "step" }, false, "running"],
    [{ status: "done" }, false, "done"],
    [{ status: "error" }, false, "error"],
    [{ status: "idle" }, false, "none"],
    [{ status: "no_session" }, false, "none"],
    [{ remote: true, status: "waiting_approval" }, false, "unknown"],
    [null, false, "unknown"],
    // A run whose session exited keeps its position — the card draws the
    // track — but the position is where the agent LEFT it. It must not read
    // as progress, and its gate is not something a human can clear into
    // motion, so 'stopped' outranks both.
    [{ status: "step", stopped: true }, false, "stopped"],
    [{ status: "waiting_approval", stopped: true }, false, "stopped"],
    [{ status: "idle", stopped: true }, false, "stopped"],
    // ...but a finished run is finished, however its session ended
    [{ status: "done", stopped: true }, false, "done"],
    [{ status: "error", stopped: true }, false, "error"],
  ];
  for (const [f, human, state] of cases) {
    check(`needs-a-human: ${JSON.stringify(f)}`, flowNeedsHuman(f) === human);
    check(`state: ${JSON.stringify(f)}`, flowState(f) === state, flowState(f));
  }
}

/* --- geometry: every card the same, however long the longest track ---- */
{
  const small = flowMetrics(3);
  check("a short workflow does not shrink the card below the minimum",
        small.cardW === 158 && small.gap === 22, small);
  const one = flowMetrics(1);
  check("a single-step workflow still gets a card", one.cardW === 158, one);
  const long = flowMetrics(30);
  check("a long one is capped in width, not in steps",
        long.cardW === 320 && long.gap < 22, long);
  // The last pip must still land inside the card, or the track runs out of it.
  const span = long.gap * 29;
  check("the compressed track fits the card it is drawn in",
        span <= long.cardW - 2 * 18 + 0.001, { span, cardW: long.cardW });
  check("the row is taller than the card, so cards never touch",
        long.rowH > long.cardH && long.colW > long.cardW, long);
}

if (failures) {
  console.log(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("flowtrack_check: ok");
