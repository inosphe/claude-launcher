/* The run's journal in the TIME DOMAIN.

   The run page had two readings of a cflow run and neither carried a clock:
   the state graph (what this workflow can do) and a fold of two hundred
   journal lines (what it did, in the order it did it). Neither answers the
   question an operator actually arrives with — where did the wall clock go —
   and the fold answers it worst of all, because a step that took twenty
   minutes of work and a step that took twenty minutes because nobody
   answered its gate print as exactly the same two lines.

   So the journal is laid on an axis: one lane per step, one bar per visit,
   and inside each bar the stretches spent at a DOOR (a choice presented and
   not confirmed, a gate, a question with a peer, a paced option held for its
   window) drawn apart from the stretches spent working.

   What a regression would quietly undo, and what this therefore pins:

     - the interval model. `step_delivered` opens a bar and `step_completed`
       closes it, but the engine has two ways of leaving a step without ever
       completing it (`state_forced`, and a resumed run being handed the next
       step), so DELIVERY closes the standing bar too. Miss that and one
       bar runs across every lane below it.
     - the shape the engine ACTUALLY writes, which is not the shape a
       hand-made fixture has. A door is journalled between two steps, after
       the previous one completed and before the next is delivered, and the
       `step` on it names the step being ENTERED. A step that is nothing but
       a choice (a leader's `standby`) is never delivered at all — so the
       door is the only thing that ever puts the run there, and a model that
       waits for a delivery draws twenty real minutes as "never entered".
       Both fixtures for this are transcribed from runs in this repository.
     - the right edge. A live run owns the axis out to `now` and its open bar
       grows with the poll; a finished one must stop where the record stops,
       or every archived run reads as though it is still being worked.
     - doors are counted INTO the step's total and drawn APART from it. The
       whole point of the picture is that those two facts coexist.
     - a door left open by the record closes with its bar, not at the end of
       the axis — an ask that was never answered belongs to the step it was
       asked in.
     - the lanes are the graph's rows. The two pictures share `wfStepOrder`
       precisely so a step is on the same line in both; a lane order of its
       own is a lane the reader has to hunt for.
     - every declared step gets a lane, entered or not (a timing diagram that
       hides its idle signals is not one), and a step the workflow no longer
       declares still gets one, because it still happened.
     - the drawing stays wire-able and safe: every lane group carries
       `data-step` (that attribute IS the click binding in wfTimelinePanel),
       a step id with markup in it cannot break out of the SVG, and no
       coordinate is ever NaN. */
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

const code = [
  slice("function escXml(", "/* A cadence in the largest unit"),
  // the model and the drawing, stopping short of wfTimelinePanel — that one
  // needs a document, and everything worth pinning is below it
  slice("const WFT_WAIT = {", "function wfTimelinePanel("),
].join("\n");

const ctx = {};
new Function(
  "exports",
  code + "\nObject.assign(exports, {wfStepOrder, wfTimeline, wfTimelineSvg," +
  " wftTickStep, wftDur, WFT});"
)(ctx);
const { wfStepOrder, wfTimeline, wfTimelineSvg, wftTickStep, wftDur, WFT } = ctx;

const fail = [];
function ok(cond, what) { if (!cond) fail.push(what); }

/* ---- the workflow under test ------------------------------------------ */
/* improv-worker's shape, cut down: a straight run into a delegated choice
   that can send the run back round, which is where repeat visits (and the
   only interesting doors) come from. */
const WF = {
  name: "improv-worker",
  start: "intake",
  steps: [
    { id: "intake", title: "목표 수립", next: "work" },
    { id: "work", title: "작업 실행", next: "review" },
    {
      id: "review",
      select: {
        chooser: "parent",
        prompt: "land it?",
        options: [
          { name: "request", next: "landing" },
          { name: "rework", next: "work" },
        ],
      },
    },
    { id: "landing", next: "wrapup" },
    { id: "wrapup", next: null },
    { id: "orphan", next: null },   // declared, unreachable, never entered
  ],
};

const T = (s) => new Date(Date.UTC(2026, 7, 26, 9, 0, 0) + s * 1000).toISOString();
const MS = (s) => Date.UTC(2026, 7, 26, 9, 0, 0) + s * 1000;

/* A run that: works intake, is sent back round from review once, waits on a
   delegated ask the second time, has one verify fail and one pass, and is
   still standing in `landing` when the page is drawn. */
const JOURNAL = [
  { at: T(0), event: "started", run: "r1", workflow: "improv-worker" },
  { at: T(0), event: "step_delivered", run: "r1", step: "intake", visit: 1 },
  { at: T(60), event: "step_report", run: "r1", step: "intake", visit: 1,
    summary: "goal fixed" },
  { at: T(60), event: "step_completed", run: "r1", step: "intake", visit: 1 },
  { at: T(60), event: "step_delivered", run: "r1", step: "work", visit: 1 },
  { at: T(600), event: "step_report", run: "r1", step: "work", visit: 1,
    summary: "wrote it" },
  { at: T(600), event: "step_completed", run: "r1", step: "work", visit: 1 },
  { at: T(600), event: "step_delivered", run: "r1", step: "review", visit: 1 },
  { at: T(660), event: "verify_failed", run: "r1", step: "review",
    output: "2 failed" },
  { at: T(700), event: "select_presented", run: "r1", step: "review" },
  { at: T(760), event: "select_confirmed", run: "r1", step: "review",
    option: "rework" },
  { at: T(760), event: "step_completed", run: "r1", step: "review", visit: 1 },
  // second lap
  { at: T(760), event: "step_delivered", run: "r1", step: "work", visit: 2 },
  { at: T(900), event: "step_completed", run: "r1", step: "work", visit: 2 },
  { at: T(900), event: "step_delivered", run: "r1", step: "review", visit: 2 },
  { at: T(920), event: "verify_passed", run: "r1", step: "review" },
  { at: T(930), event: "ask_opened", run: "r1", step: "review",
    asked: ["s127"] },
  { at: T(1230), event: "ask_answered", run: "r1", step: "review",
    by: "s127", decision: "request" },
  { at: T(1230), event: "select_confirmed", run: "r1", step: "review",
    option: "request" },
  { at: T(1230), event: "step_completed", run: "r1", step: "review", visit: 2 },
  // ...and still standing here when the page draws
  { at: T(1230), event: "step_delivered", run: "r1", step: "landing", visit: 1 },
];
const RUN = { run: "r1", status: "step", step_id: "landing",
              started_at: T(0), workflow: "improv-worker" };
const NOW = MS(1800);

const m = wfTimeline(WF, RUN, JOURNAL, NOW);
const laneOf = (id) => m.lanes.find((l) => l.id === id);

/* ---- the axis --------------------------------------------------------- */
ok(m.t0 === MS(0), "the axis starts at the first thing the journal recorded");
ok(m.live === true, "a run that is neither done nor aborted is live");
ok(m.t1 === NOW, "a live run's axis runs out to now, not to its last write");
ok(m.span === 1800 * 1000, "span is the axis, not the recorded stretch");

/* ---- bars: one per visit, closed by the record ------------------------- */
const work = laneOf("work");
ok(work.bars.length === 2, "the two visits to `work` are two bars, not one");
ok(work.bars[0].from === MS(60) && work.bars[0].to === MS(600),
   "a bar spans delivery -> completion");
ok(work.bars[0].visit === 1 && work.bars[1].visit === 2,
   "each bar keeps the visit number the journal gave it");
ok(work.busy === (600 - 60 + 900 - 760) * 1000,
   "a lane's total is the sum of its bars");
ok(laneOf("intake").bars.length === 1, "intake was entered once");

/* No bar may overlap another: the run stands in exactly one step at a time,
   and a bar that outlives its step is the failure this whole model exists to
   avoid. */
const all = m.bars.slice().sort((a, b) => a.from - b.from);
let overlap = null;
for (let i = 1; i < all.length; i++) {
  if (all[i].from < all[i - 1].to) overlap = `${all[i - 1].step}/${all[i].step}`;
}
ok(!overlap, `no two bars overlap (got ${overlap})`);

/* ---- the open bar ----------------------------------------------------- */
const landing = laneOf("landing");
ok(landing.bars.length === 1 && landing.bars[0].live === true,
   "the step the run is standing in is marked live");
ok(landing.bars[0].to === NOW,
   "the live bar runs to now, so it grows on every poll");

/* Delivery closes what is standing, even with no completion in the record —
   the `state_forced` and resumed-run paths. */
const FORCED = [
  { at: T(0), event: "step_delivered", run: "r1", step: "work", visit: 1 },
  { at: T(300), event: "state_forced", run: "r1", step: "wrapup",
    was: "work" },
  { at: T(300), event: "step_delivered", run: "r1", step: "wrapup", visit: 1 },
  { at: T(400), event: "step_completed", run: "r1", step: "wrapup", visit: 1 },
  { at: T(400), event: "done", run: "r1" },
];
const fm = wfTimeline(WF, { status: "done", started_at: T(0) }, FORCED, MS(9999));
const fw = fm.lanes.find((l) => l.id === "work").bars[0];
ok(fw.to === MS(300), "a step left without completing closes when the next is delivered");
ok(fm.t1 === MS(400),
   "a finished run's axis stops where the record does, not at now");
ok(fm.lanes.find((l) => l.id === "wrapup").bars[0].live === false,
   "nothing on a finished run is live");
ok(fm.runMarks.some((x) => x.kind === "done"), "the finish is kept as a run mark");

/* ---- doors: counted in, drawn apart ----------------------------------- */
const review = laneOf("review");
const r1 = review.bars[0], r2 = review.bars[1];
ok(r1.waits.length === 1 && r1.waits[0].kind === "select",
   "select_presented opens a `select` door");
ok(r1.waits[0].from === MS(700) && r1.waits[0].to === MS(760),
   "...closed by select_confirmed, not by the bar");
ok(r2.waits.length === 1 && r2.waits[0].kind === "ask",
   "ask_opened opens an `ask` door");
ok(r2.waits[0].to === MS(1230), "...closed by ask_answered");
ok(review.waited === (760 - 700 + 1230 - 930) * 1000,
   "waiting time is summed per lane");
ok(review.busy === (760 - 600 + 1230 - 900) * 1000,
   "and it is INSIDE the lane's total, not beside it: a door is time at the step");

/* A door the record never closes belongs to its step, not to the axis. */
const HANG = [
  { at: T(0), event: "step_delivered", run: "r1", step: "review", visit: 1 },
  { at: T(10), event: "ask_opened", run: "r1", step: "review" },
  { at: T(100), event: "step_completed", run: "r1", step: "review", visit: 1 },
  { at: T(100), event: "step_delivered", run: "r1", step: "landing", visit: 1 },
];
const hm = wfTimeline(WF, { status: "step", started_at: T(0) }, HANG, MS(5000));
const hw = hm.lanes.find((l) => l.id === "review").bars[0].waits[0];
ok(hw.to === MS(100),
   "an unanswered ask ends with its step, not at the end of the axis");

/* Two doors at once — a paced option held while its selection is still open.
   They are tracked independently, so neither swallows the other. */
const HELD = [
  { at: T(0), event: "step_delivered", run: "r1", step: "review", visit: 1 },
  { at: T(10), event: "select_presented", run: "r1", step: "review" },
  { at: T(20), event: "select_held", run: "r1", step: "review",
    option: "request" },
  { at: T(80), event: "select_confirmed", run: "r1", step: "review",
    option: "request" },
];
const hb = wfTimeline(WF, { status: "step", started_at: T(0) }, HELD, MS(100))
  .lanes.find((l) => l.id === "review").bars[0];
ok(hb.waits.length === 2, "a step can be behind two doors at once");
ok(hb.waits.map((w) => w.kind).join(",") === "select,window",
   "...and each keeps its own kind");
ok(hb.waits[0].to === MS(80) && hb.waits[1].to === MS(80),
   "one confirmation closes both");

/* ---- the shape the real journals actually have ------------------------ */
/* Everything above was written against a hand-made record, and a hand-made
   record puts a door INSIDE the step it belongs to. The engine does not: it
   writes the door an instant AFTER the previous step completed and BEFORE the
   next is delivered, and the `step` on the door names the step being entered.
   Worse, a step that is nothing but a choice — a leader's `standby` — is
   never delivered at all, so a run can sit in one for twenty minutes with no
   `step_delivered` anywhere in its journal. Read literally the way the first
   draft of this model read it, that step is drawn as never entered and the
   twenty minutes vanish off the clock entirely.

   Both fixtures below are transcribed from real runs in this repository
   (.cflow/runs/s127 and .cflow/runs/s150), timestamps included. */

const STANDBY = [
  { at: T(0), event: "started", run: "r", workflow: "improv-leader" },
  { at: T(0), event: "step_delivered", run: "r", step: "intake", visit: 1 },
  { at: T(1777), event: "step_completed", run: "r", step: "intake", visit: 1 },
  // no step_delivered for `standby` — ever
  { at: T(1777), event: "select_presented", run: "r", step: "standby" },
  { at: T(2163), event: "select_confirmed", run: "r", step: "standby",
    option: "integrate" },
  { at: T(2163), event: "step_delivered", run: "r", step: "preflight", visit: 1 },
  { at: T(2309), event: "step_completed", run: "r", step: "preflight", visit: 1 },
  // ...and a door's outcome arriving just AHEAD of the step it belongs to
  { at: T(2309), event: "ask_unanswered_proceeded", run: "r", step: "integrate" },
  { at: T(2309), event: "step_delivered", run: "r", step: "integrate", visit: 1 },
  { at: T(2573), event: "step_completed", run: "r", step: "integrate", visit: 1 },
  { at: T(2573), event: "done", run: "r" },
];
const LEAD = { start: "intake", steps: [
  { id: "intake", next: "standby" },
  { id: "standby", select: { chooser: "agent", prompt: "?", options: [
    { name: "integrate", next: "preflight" }, { name: "wait", next: "standby" }] } },
  { id: "preflight", next: "integrate" },
  { id: "integrate", next: null },
] };
const sm = wfTimeline(LEAD, { status: "done", started_at: T(0) }, STANDBY, MS(9999));
const sb = sm.lanes.find((l) => l.id === "standby");
ok(sb.bars.length === 1,
   "a step that is only a choice is entered by its DOOR — nothing delivers it");
ok(sb.bars[0].from === MS(1777) && sb.bars[0].to === MS(2163),
   "...and it holds the step from the choice being put to the choice being made");
ok(sb.waited === 386 * 1000 && sb.busy === 386 * 1000,
   "...all of which was waiting, which is the whole reason to draw it");
ok(sb.bars[0].visit === 1, "a bar opened by a door still numbers its visit");
const integ = sm.lanes.find((l) => l.id === "integrate").bars[0];
ok(integ.marks.length === 1 && integ.marks[0].glyph === "⋯",
   "an outcome written just ahead of its step lands on that step, not nowhere");
ok(integ.marks[0].at >= integ.from && integ.marks[0].at <= integ.to,
   "...clamped into the bar rather than dangling before it");
ok(sm.lanes.find((l) => l.id === "intake").bars[0].to === MS(1777),
   "and the step before it still ends where it completed");

/* s150: the ask that decides a landing, opened after `commit` completed and
   answered while the run is still standing in `landing`. */
const LAND = [
  { at: T(0), event: "step_delivered", run: "r", step: "commit", visit: 1 },
  { at: T(240), event: "step_completed", run: "r", step: "commit", visit: 1 },
  { at: T(240), event: "ask_opened", run: "r", step: "landing",
    ask: "ask-9e0715" },
  { at: T(289), event: "ask_answered", run: "r", step: "landing",
    by: "s127", decision: "request" },
];
const lm = wfTimeline({ start: "commit", steps: [
  { id: "commit", next: "landing" }, { id: "landing", next: null }] },
  { status: "step", step_id: "landing", started_at: T(0) }, LAND, MS(900));
const lb = lm.lanes.find((l) => l.id === "landing").bars[0];
ok(lb && lb.from === MS(240) && lb.live === true,
   "an ask opened between two steps puts the run in the one being entered");
ok(lb.to === MS(900), "...and it is still standing there now");
ok(lb.waits.length === 1 && lb.waits[0].to === MS(289),
   "the door closes when it was answered, not when the step ends");
ok(Math.round(lm.lanes.find((l) => l.id === "landing").waited / 1000) === 49,
   "so 49s of a 11m step reads as waiting and the rest as work");

/* ---- marks ------------------------------------------------------------ */
const glyphs = (b) => b.marks.map((x) => x.glyph).join("");
ok(glyphs(laneOf("intake").bars[0]) === "◆", "a report is one mark on its bar");
ok(glyphs(r1) === "✗●", "a failed verify and the option taken, in that order");
ok(glyphs(r2) === "✓●", "the second lap passed verify");
ok(r1.marks[0].label.indexOf("2 failed") >= 0,
   "the verify's own output rides in the mark, for the tooltip");
ok(r1.marks.every((x) => x.at >= r1.from && x.at <= r1.to),
   "a mark lands inside the bar it belongs to");
ok(sm.lanes.find((l) => l.id === "standby").bars[0].marks
     .map((x) => x.glyph).join("") === "●",
   "the option taken is a mark on the step it was chosen at");

/* ---- lanes ------------------------------------------------------------ */
ok(m.lanes.map((l) => l.id).join(",") ===
   wfStepOrder(WF).join(","),
   "the lanes are the graph's rows, in the graph's order");
ok(laneOf("orphan") && laneOf("orphan").bars.length === 0,
   "a declared step nobody entered still gets a lane");
ok(laneOf("orphan").busy === 0, "...with nothing on the clock");

/* A run outlives the file it was cut from. */
const GONE = JOURNAL.concat([
  { at: T(1300), event: "step_delivered", run: "r1", step: "sunset", visit: 1 },
  { at: T(1400), event: "step_completed", run: "r1", step: "sunset", visit: 1 },
]);
const gm = wfTimeline(WF, RUN, GONE, NOW);
const gone = gm.lanes.find((l) => l.id === "sunset");
ok(gone && gone.declared === false,
   "a step the workflow no longer declares still gets a lane, flagged");
ok(gm.lanes[gm.lanes.length - 1].id === "sunset",
   "...appended after the declared ones rather than interleaved");

/* ---- nothing to draw --------------------------------------------------- */
ok(wfTimeline(WF, RUN, [], NOW).bars.length === 0,
   "an empty journal yields no bars (the panel then draws nothing at all)");
ok(wfTimeline(WF, {}, [], NOW) === null,
   "...and with no start time either there is no model to draw");

/* ---- durations --------------------------------------------------------- */
ok(wftDur(45 * 1000) === "45s", "seconds under a minute");
ok(wftDur(520 * 1000) === "8m40s", "two units, never a decimal");
ok(wftDur(600 * 1000) === "10m", "and the second unit dropped when it is zero");
ok(wftDur(3600 * 1000) === "1h", "an exact hour");
ok(wftDur(7199 * 1000) === "1h59m", "truncated, so 1h60m can never print");
ok(wftDur(-5) === "0s", "a negative span is nonsense, not a minus sign");
ok(wftTickStep(1800 * 1000, 5) % 1000 === 0 &&
   [1, 5, 15, 30, 60, 300, 900, 1800, 3600].indexOf(
     wftTickStep(1800 * 1000, 5) / 1000) >= 0,
   "ticks land on a unit a person says out loud");

/* ---- the drawing -------------------------------------------------------- */
const svg = wfTimelineSvg(m, "work", RUN);
ok(/^<svg /.test(svg) && svg.trim().endsWith("</svg>"), "it is one SVG");
ok(!/NaN|Infinity|undefined/.test(svg), "no coordinate is NaN/undefined");
for (const l of m.lanes) {
  ok(svg.indexOf(`data-step="${l.id}"`) >= 0,
     `lane ${l.id} carries data-step — that attribute IS the click binding`);
}
ok((svg.match(/class="wft-lane/g) || []).length === m.lanes.length,
   "one lane group per lane, idle ones included");
ok((svg.match(/class="wft-run"/g) || []).length === m.bars.length,
   "one bar rect per bar");
ok(svg.indexOf('class="wft-wait ask"') >= 0 &&
   svg.indexOf('class="wft-wait select"') >= 0,
   "each door is drawn in its own kind, so the palette can tell them apart");
ok(/class="wft-lane[^"]*\bselected\b/.test(svg),
   "the selected step's lane says so, the way its box in the graph does");
ok(/class="wft-lane[^"]*\bcurrent\b/.test(svg),
   "and the step the run is standing in does too");
ok(/class="wft-lane[^"]*\bidle\b/.test(svg), "an unentered lane is marked idle");
ok(svg.indexOf('class="wft-bar live"') >= 0, "the open bar is marked live");
ok(svg.indexOf("elapsed") >= 0 && svg.indexOf(">0</text>") >= 0,
   "the axis says it is elapsed time and labels its origin");
ok(svg.indexOf("now · ") >= 0, "a live run says its right edge is now");
ok(wfTimelineSvg(fm, null, { status: "done" }).indexOf("now · ") < 0,
   "...and a finished one does not");

/* An instant step still has to be visible: zero width draws nothing. */
const INSTANT = [
  { at: T(0), event: "step_delivered", run: "r1", step: "intake", visit: 1 },
  { at: T(0), event: "step_completed", run: "r1", step: "intake", visit: 1 },
  { at: T(0), event: "step_delivered", run: "r1", step: "work", visit: 1 },
  { at: T(600), event: "step_completed", run: "r1", step: "work", visit: 1 },
  { at: T(600), event: "done", run: "r1" },
];
const im = wfTimeline(WF, { status: "done", started_at: T(0) }, INSTANT, MS(600));
const iw = wfTimelineSvg(im, null, { status: "done" })
  .match(/class="wft-run" x="[\d.]+" y="[\d.]+" width="([\d.]+)"/g) || [];
ok(iw.length === 2 && iw.every((s) => parseFloat(/width="([\d.]+)"/.exec(s)[1]) >= 2),
   "a step that took no measurable time is still drawn");

/* Nothing in a journal is ours to trust as markup. */
const EVIL = [
  { at: T(0), event: "step_delivered", run: "r1",
    step: '</svg><script>x</script>&', visit: 1 },
  { at: T(60), event: "step_completed", run: "r1",
    step: '</svg><script>x</script>&', visit: 1 },
];
const em = wfTimeline({ start: "a", steps: [] }, { status: "done", started_at: T(0) },
                      EVIL, MS(60));
const esvg = wfTimelineSvg(em, null, {});
ok(esvg.indexOf("<script>") < 0 && esvg.indexOf("&amp;") >= 0,
   "a step id is escaped into the SVG, never spliced into it");
ok((esvg.match(/<\/svg>/g) || []).length === 1,
   "...and cannot close the drawing early");

/* The geometry the stylesheet is written against. */
ok(WFT.w === 480, "the same width as the state graph above it, so they align");
ok(WFT.name + WFT.right < WFT.w, "the plot has room left between the gutters");

if (fail.length) {
  console.error("wftime_check FAILED:\n  " + fail.join("\n  "));
  process.exit(1);
}
console.log("wftime_check ok");
