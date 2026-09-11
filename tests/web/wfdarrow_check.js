/* Which way the workflow graph's arrowheads point.

   The graph draws two kinds of route. A TREE route is the canonical way into
   a step: it drops straight down from the box above, and its head has always
   pointed at the box it lands on. Every other route is a REFERENCE — a merge
   from a second branch, a loop back to an earlier step, the run finishing at
   `end` — and it is bent out through a side rail so the arcs do not lie on
   top of each other.

   A cubic's head is oriented by its last segment: the tangent at the landing
   runs from the second control point to the end point. For a reference route
   that control point sits ON the rail, so the head points into the box only
   when the landing is between the box's centre and the rail. It was landing
   on the far side instead, which reversed the head of every reference route
   in every workflow — 3 of them in improv-leader, 13 in improv-worker — and
   the two routes into `end` came out pointing away from the pill in opposite
   directions, which is what a reader reported (s526, 2026-09-11).

   Two more facts this pins:
     - `end` is a 90-wide pill, not a 210-wide step box, so a route into it
       lands 45 from its centre. On the step half-width the head stood 58px
       out in open space beside the pill, attached to nothing.
     - a loop-back re-enters the BOTTOM of its target, so its head points UP.
       That needs its last control point below the landing; on the rail, the
       head lay flat against the underside pointing sideways. */
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
  slice("/* A paced option's opening moment", "/* One line under each rail row"),
  slice("function escXml(", RULE),
].join("\n");
const ctx = {};
new Function("exports", code + "\nObject.assign(exports, {wfDiagramSvg});")(ctx);
const { wfDiagramSvg } = ctx;

const fail = [];
function ok(cond, what) { if (!cond) fail.push(what); }

/* ---- the workflow under test ------------------------------------------ */
/* improv-leader's shape, cut down to the three kinds of reference route:
   a forward merge (preflight -> sweep, sweep's canonical parent being the
   standby that reaches it first), a loop back (wrapup -> standby) and two
   routes into `end` leaving from opposite sides of the drawing. */
const WF = {
  name: "arrows",
  start: "intake",
  steps: [
    { id: "intake", title: "intake", next: "standby" },
    {
      id: "standby",
      title: "standby",
      select: {
        prompt: "what now?",
        chooser: "agent",
        options: [
          { name: "integrate", next: "preflight" },
          { name: "skip", next: "sweep" },
          { name: "wind-down", next: "wrapup" },
        ],
      },
    },
    { id: "preflight", title: "preflight", next: "sweep" },
    { id: "sweep", title: "sweep", next: null },
    {
      id: "wrapup",
      title: "wrapup",
      select: {
        prompt: "done?",
        chooser: "agent",
        options: [
          { name: "done", next: null },
          { name: "decline", next: "standby" },
        ],
      },
    },
  ],
};

const svg = wfDiagramSvg(WF, null, null);

/* Every box on the page, by step id — `end` included, which is why this
   reads the rendered rect rather than recomputing the layout. */
const boxes = {};
const BOX = /<g class="[^"]*" data-step="([^"]+)">(?:<title>[^<]*<\/title>)?<rect x="([\d.-]+)" y="([\d.-]+)" width="([\d.]+)" height="([\d.]+)"/g;
for (const m of svg.matchAll(BOX)) {
  boxes[m[1]] = { x: +m[2], y: +m[3], w: +m[4], h: +m[5] };
}
ok(Object.keys(boxes).length === 6,
   `six boxes including end, got ${Object.keys(boxes).length}`);
ok(boxes.end && boxes.end.w === 90, "end is the 90-wide pill this check assumes");

const PATH = /<path class="[^"]*" d="M [\d.-]+ [\d.-]+ C [\d.-]+ [\d.-]+, ([\d.-]+) ([\d.-]+), ([\d.-]+) ([\d.-]+)" marker-end="[^"]*" data-ref="([^"]*)"/g;
const refs = [];
for (const m of svg.matchAll(PATH)) {
  const ref = m[5].replace(/&gt;/g, ">");
  if (!ref) continue;                       // a tree route carries no data-ref
  refs.push({
    to: ref.split(">")[1],
    ref,
    cx: +m[1], cy: +m[2], x: +m[3], y: +m[4],
  });
}
// preflight->sweep, wrapup->standby, sweep->end, wrapup->end.
ok(refs.length === 4, `four reference routes, got ${refs.length}`);

for (const r of refs) {
  const b = boxes[r.to];
  if (!b) { fail.push(`${r.ref} lands on a box that is not drawn`); continue; }
  // On the box, to within the 2px the router insets its landings by.
  const on = r.x >= b.x - 2 && r.x <= b.x + b.w + 2
    && r.y >= b.y - 2 && r.y <= b.y + b.h + 2;
  ok(on, `${r.ref} lands on its target box, not beside it`);
  const dx = r.x - r.cx, dy = r.y - r.cy;
  // Into the box: horizontally, the head must travel towards the centre
  // column; vertically (a loop-back), towards the centre row.
  const towards = Math.abs(dx) > Math.abs(dy)
    ? (dx > 0 ? r.x < b.x + b.w / 2 : r.x > b.x + b.w / 2)
    : (dy > 0 ? r.y < b.y + b.h / 2 : r.y > b.y + b.h / 2);
  ok(towards, `${r.ref} points INTO its target, not away from it`);
}

/* The loop back is the one route that arrives from below, and the only one
   whose head is vertical. */
const loop = refs.find((r) => r.ref === "wrapup>standby");
ok(loop && Math.abs(loop.y - loop.cy) > Math.abs(loop.x - loop.cx),
   "the loop back arrives vertically");
ok(loop && loop.y < loop.cy, "...pointing up, into the box it re-enters");

/* Both routes into `end` reach the pill itself, from either side. */
const ends = refs.filter((r) => r.to === "end");
ok(ends.length === 2, `two routes finish the run, got ${ends.length}`);
for (const e of ends) {
  ok(Math.abs(e.x - (boxes.end.x + boxes.end.w / 2)) <= 45,
     `${e.ref} lands within the pill's own half-width`);
}

if (fail.length) {
  console.error("FAIL:\n  " + fail.join("\n  "));
  process.exit(1);
}
console.log("ok — every reference arrowhead points at what it arrives at");
