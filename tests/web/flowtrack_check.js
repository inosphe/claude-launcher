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
  slice("function answerFellToUs(", "function shortenPath("),
  slice("function flowNeedsHuman(", "const FLOW_WORDS"), // + flowState
].join("\n");

const ctx = {};
new Function(
  "exports",
  code + "\nObject.assign(exports, {wfDiagramSvg, wfStepOrder, wfTreeLayout, wfdTextW, wfdFit, flowMetrics," +
  " flowOrder, flowTrack, flowNeedsHuman, flowState});"
)(ctx);
const { wfDiagramSvg, wfStepOrder, wfTreeLayout, wfdTextW, wfdFit, flowMetrics,
        flowOrder, flowTrack, flowNeedsHuman, flowState } = ctx;

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

/* A fork occupies sibling columns.  The join is still one node: its first
   forward arrival is the tree parent and the other arrival is a dashed
   reference edge, rather than a second copy of `join`. */
{
  const fork = {
    name: "fork", start: "root",
    steps: [
      { id: "root", select: { options: [
        { name: "left", next: "left" }, { name: "right", next: "right" },
      ] } },
      { id: "left", next: "join" }, { id: "right", next: "join" },
      { id: "join" },
    ],
  };
  const svg = wfDiagramSvg(fork, {}, null);
  const nodeX = (id) => {
    const m = svg.match(new RegExp(
      `data-step="${id}"[^>]*><rect x="([\\d.]+)" y="([\\d.]+)"`));
    return m ? { x: +m[1], y: +m[2] } : null;
  };
  const root = nodeX("root"), left = nodeX("left"), right = nodeX("right");
  check("fork children occupy separate sibling columns",
        !!root && !!left && !!right && left.x < root.x && root.x < right.x,
        { root, left, right });
  check("a joined step is rendered once",
        (svg.match(/data-step="join"/g) || []).length === 1, svg);
  check("the second arrival to a join is a reference edge",
        svg.includes('class="wfd-edge ref"') && svg.includes('data-ref="right&gt;join"'),
        svg);
  const routes = [
    { from: "root", to: "left" }, { from: "root", to: "right" },
    { from: "left", to: "join" }, { from: "right", to: "join" },
  ];
  const layout = wfTreeLayout(wfStepOrder(fork), routes, fork.start);
  check("the first forward arrival is the join's tree parent",
        layout.parent.get("join") === "left", [...layout.parent]);
}

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

/* ---- waiting_answer: delegated, unless it reached nobody -------------- */
/* The card gets one word, so this is where the lie was loudest: "waiting on
   a peer" for a run no peer has ever heard of. */
const PEER = { status: "waiting_answer", ask: { asked: [{ handle: "lead" }] } };
const NOBODY = { status: "waiting_answer", ask: { asked: [] } };
const NOASK = { status: "waiting_answer" };

check("an ask with a holder is the peer's",
      ctx.flowState(PEER) === "delegated", ctx.flowState(PEER));
check("...and is not counted as the reader's move",
      ctx.flowNeedsHuman(PEER) === false, ctx.flowNeedsHuman(PEER));
check("an ask that reached nobody is the reader's",
      ctx.flowNeedsHuman(NOBODY) === true, ctx.flowNeedsHuman(NOBODY));
check("...so the card says so rather than naming a peer",
      ctx.flowState(NOBODY) === "blocked", ctx.flowState(NOBODY));
check("no ask at all reads the same way (a forced goto leaves this)",
      ctx.flowState(NOASK) === "blocked", ctx.flowState(NOASK));
/* A stopped session outranks it: the run is where the agent left it, and
   there is nobody in front of it to press anything. */
check("a stopped session still outranks it",
      ctx.flowState({ ...NOBODY, stopped: true }) === "stopped",
      ctx.flowState({ ...NOBODY, stopped: true }));

/* --- the rows are layers, not a breadth-first walk --------------------- */
/* The shape is improv-worker's: one main chain with two loops back into it
   and a shortcut to the finish. A BFS order rows a join by its SHORTEST
   path, so wrapup (reachable straight from landing) sat above landed (only
   reachable via the long chain) and the closing chain — landed -> wrapup ->
   end — ran BACKWARD up the rail. The order must instead be the deepest
   path, so every real edge runs downward and the only edges pointing up
   are the two loop arcs. */
const WORKER = {
  name: "improv-worker", start: "intake",
  steps: [
    { id: "intake", next: "work" }, { id: "work", next: "review" },
    { id: "review", next: "commit" }, { id: "commit", next: "landing" },
    { id: "landing", select: { prompt: "p", chooser: "agent", options: [
      { name: "request", next: "rebase" }, { name: "hold", next: "wrapup" } ] } },
    { id: "rebase", next: "peer-review" },
    { id: "peer-review", select: { prompt: "p", chooser: "agent", options: [
      { name: "pass", next: "integration-request" },
      { name: "changes", next: "work" } ] } },
    { id: "integration-request", next: "await-landing" },
    { id: "await-landing", select: { prompt: "p", chooser: "agent", options: [
      { name: "landed", next: "landed" }, { name: "rebase", next: "rebase" } ] } },
    { id: "landed", next: "wrapup" },
    { id: "wrapup" },   // no `next`: the run terminates here, drawing the end node
  ],
};
{
  const order = wfStepOrder(WORKER);
  const at = (id) => order.indexOf(id);
  const FORWARD = [
    ["intake", "work"], ["work", "review"], ["review", "commit"],
    ["commit", "landing"], ["landing", "rebase"], ["landing", "wrapup"],
    ["rebase", "peer-review"], ["peer-review", "integration-request"],
    ["integration-request", "await-landing"], ["await-landing", "landed"],
    ["landed", "wrapup"],
  ];
  const LOOPS = [["peer-review", "work"], ["await-landing", "rebase"]];
  check("the main chain and the shortcut all run downward",
        FORWARD.every(([u, v]) => at(v) > at(u)), order);
  check("the two loop arcs are the only edges pointing up",
        LOOPS.every(([u, v]) => at(v) < at(u)), order);
  check("the join the shortcut shares sits below its long way round",
        at("wrapup") > at("landed"), order);
  check("...and right below it, so the closing chain is straight",
        at("wrapup") === at("landed") + 1 && at("landed") > at("await-landing"),
        order);
  const drawn = [...wfDiagramSvg(WORKER, {}, null).matchAll(/data-step="([^"]+)"/g)]
    .map((m) => m[1]).filter((id) => id !== "end");
  check("the strip numbers the branching graph exactly as the run page stacks it",
        flowOrder(WORKER).join() === drawn.join(),
        { strip: flowOrder(WORKER), rows: drawn });
  // end hangs off the bottom, below every step it terminates
  const svg = wfDiagramSvg(WORKER, {}, null);
  const yOf = (id) => {
    const m = svg.match(
      new RegExp(`class="[^"]*wfd-node[^"]*" data-step="${id}"[^>]*>` +
                 `(?:<title>[^<]*</title>)?<rect x="[^"]+" y="(\\d+)"`));
    return m ? +m[1] : NaN;
  };
  check("end is the bottom row of the picture",
        order.every((id) => Number.isFinite(yOf(id)) && yOf("end") > yOf(id)),
        { endY: yOf("end"), steps: order.map((id) => yOf(id)) });
}

/* A delegated ask has two graph exits: approval follows `next`, while a
   refusal follows `on_decline`. The refusal target must stay in the same
   component so end-hold is drawn beside the ending gate rather than as an
   orphaned island. */
{
  const ASK_END = {
    name: "ask-end", start: "landed",
    steps: [
      { id: "landed", next: "end-gate" },
      { id: "end-gate", ask: { on_decline: "end-hold" }, next: "end" },
      { id: "end-hold" },
    ],
  };
  const order = wfStepOrder(ASK_END);
  check("an ask refusal target is connected to the graph",
        order.indexOf("end-hold") > order.indexOf("end-gate"), order);
  const svg = wfDiagramSvg(ASK_END, {}, null);
  check("the graph draws the refusal edge",
        svg.includes(">decline</text>") &&
          (svg.match(/class="wfd-edge/g) || []).length === 4, svg);
}

/* --- one route, one arc, one label to a coordinate --------------------- */
/* The real improv-worker has two things the fixture above leaves out, and
   both of them broke the drawing rather than the order.

   `await-landing` leaves for `rebase` under TWO names (a rebase re-request
   and a re-measure request). Those are one way back, not two: drawn as two
   arcs they put their labels on the identical coordinate — measured at
   x=127,y=649, one word on top of the other — and they spent two upward arcs
   on one route. Two is also the floor here rather than a target to beat: a
   directed cycle drawn with every arc pointing down does not exist, so each
   of this graph's two loops owes exactly one upward arc. Rowing `rebase`
   under `await-landing` does not buy the loop back — it only moves which arc
   points up, and every other cut of that cycle floats a downstream step to
   row 0, because `rebase` is the only step in it with an edge coming in from
   above.

   And the titles are sentences. A 45-character title in a 210px box did not
   stop at the box: it ran out over the right rail and printed across the
   edge labels drawn there. */
const WORKER_FULL = {
  name: "improv-worker", start: "intake",
  steps: [
    { id: "intake", title: "목표 수립", next: "work" },
    { id: "work", title: "작업 실행", next: "review" },
    { id: "review", title: "테스트 (표적)", next: "commit" },
    { id: "commit", title: "커밋·보고", next: "landing" },
    { id: "landing", title: "착지 결정 (깨끗하면 스스로 요청한다)",
      select: { prompt: "p", chooser: "agent", options: [
        { name: "request", next: "rebase" },
        { name: "escalate", next: "landing-review" } ] } },
    { id: "landing-review", title: "착지 결정 (상위 세션에 묻는다)",
      select: { prompt: "p", chooser: "delegate", options: [
        { name: "request", next: "rebase" },
        { name: "hold", next: "wrapup" } ] } },
    { id: "rebase", title: "정렬 (요청 전 — rebase, 또는 프리뷰 머지 위 재측정)",
      next: "peer-review" },
    { id: "peer-review", title: "동료 리뷰 (리베이스 후, 요청 직전)",
      select: { prompt: "p", chooser: "delegate", options: [
        { name: "pass", next: "integration-request" },
        { name: "changes", next: "work" } ] } },
    { id: "integration-request", title: "상위 통합 요청 (머지는 상위 세션 전권)",
      next: "await-landing" },
    { id: "await-landing", title: "착지 대기 (요청은 착지가 아니다)",
      select: { prompt: "p", chooser: "agent", options: [
        { name: "landed", next: "landed" },
        { name: "rebase", next: "rebase" },
        { name: "remeasure", next: "rebase" } ] } },
    { id: "landed", title: "착지 확인 (기계가 본다)", next: "wrapup" },
    { id: "wrapup", title: "회차 마감·정리" },
  ],
};
{
  const svg = wfDiagramSvg(WORKER_FULL, {}, null);

  /* Every arc as (first y, last y): a straight `M x y1 L x y2` and a bend
     `M x y1 C bx y1, bx y2, x2 y2` both finish on the last number pair. */
  const arcs = [...svg.matchAll(/<path class="wfd-edge[^"]*" d="([^"]+)"/g)]
    .map((m) => {
      const n = m[1].match(/-?[\d.]+/g).map(Number);
      return {
        d: m[1], y1: n[1], y2: n[n.length - 1],
        bend: m[1].includes("C") ? n[2] : null,
      };
    });
  const up = arcs.filter((a) => a.y2 < a.y1);
  check("one upward arc per loop, and this graph has two loops",
        up.length === 2, arcs.map((a) => `${a.y1}->${a.y2}`));
  check("...and the shared way back is drawn once, not once per option",
        arcs.length === 16, arcs.length);

  /* Merging the arc must not eat a branch name: both options that take the
     shared way back are still written on it. */
  const labels = [...svg.matchAll(
    /<text class="wfd-e(?:label|pace)[^"]*" x="([-\d.]+)" y="([-\d.]+)"[^>]*>([^<]*)</g)]
    .map((m) => ({ x: +m[1], y: +m[2], text: m[3] }));
  const named = labels.map((l) => l.text);
  for (const name of ["request", "escalate", "hold", "pass", "changes",
                      "landed", "rebase", "remeasure"]) {
    check(`the branch '${name}' is still named on its arc`,
          named.includes(name), named);
  }
  const at = labels.map((l) => `${l.x},${l.y}`);
  check("no two edge labels share a coordinate", new Set(at).size === at.length, at);

  /* Two arcs that cross the same rows may not share a gutter column. The
     option index used to choose it, so `changes` (rows 1..7) came out in the
     same column as the loop back to rebase (rows 6..9) and crossed it. */
  const NXPX = 135;
  const left = arcs.filter((a) => a.bend !== null && a.bend < NXPX);
  const span = (a) => [Math.min(a.y1, a.y2), Math.max(a.y1, a.y2)];
  for (const a of left) {
    for (const b of left) {
      if (a === b) continue;
      const [al, ah] = span(a), [bl, bh] = span(b);
      if (al <= bh && bl <= ah) {
        check("arcs crossing the same rows get their own column",
              a.bend !== b.bend, { a: a.d, b: b.d });
      }
    }
  }

  /* A step with one straight way out draws it on the centre line. `landing`
     sends its first option down the right rail and its second straight down,
     so counting the option's place in the menu offset the only straight
     arrow it has as though it had a twin. */
  const CENTRE = 480 / 2;
  // Every step in this graph has at most one straight way out, so all of them
  // belong on the line. A step with two would legitimately fan off it.
  const centres = [...svg.matchAll(/<path class="wfd-edge[^"]*" d="M ([-\d.]+) [-\d.]+ L/g)]
    .map((m) => +m[1]);
  check("a step with one straight way out draws it on the centre line",
        centres.filter((x) => x !== CENTRE).length === 0, centres);

  /* The title stops at the box, and the whole of it stays reachable. */
  const BOX = 210 - 24;
  const titles = [...svg.matchAll(/<text class="wfd-title"[^>]*>([^<]*)</g)]
    .map((m) => m[1]);
  check("no title draws wider than its box",
        titles.every((t) => wfdTextW(t, 13) <= BOX),
        titles.map((t) => [t, Math.round(wfdTextW(t, 13))]));
  check("a title that had to be cut says so",
        titles.some((t) => t.endsWith("…")), titles);
  check("...and hangs its whole text on the node for a hover",
        svg.includes(
          "<title>integration-request — 상위 통합 요청 (머지는 상위 세션 전권)</title>"),
        svg.slice(svg.indexOf("<title>"), svg.indexOf("<title>") + 90));
  check("a title that fits is left alone", titles.includes("intake — 목표 수립"),
        titles);

  /* A Hangul glyph is a full em and Latin is not. Measuring both the same way
     is what let a 45-character title claim it fitted. */
  check("the width estimate is per-glyph, not per-character",
        wfdTextW("가나다", 13) === 39 && wfdTextW("abc", 13) < 39,
        [wfdTextW("가나다", 13), wfdTextW("abc", 13)]);
  check("a cut string never draws wider than the budget it was given",
        wfdTextW(wfdFit("상위 통합 요청 (머지는 상위 세션 전권)", 13, 80), 13) <= 80,
        wfdFit("상위 통합 요청 (머지는 상위 세션 전권)", 13, 80));

  /* Where paths come back together. Two forward arcs land on `rebase` (from
     landing and landing-review) and two on `wrapup` (from landing-review and
     landed); nothing else in this graph is arrived at twice. Before this,
     every arc ended in the same head and the reader had to trace all sixteen
     to find that out. */
  const heads = [...svg.matchAll(
    /<path class="wfd-edge[^"]*" d="([^"]+)" marker-end="url\(#([^)]+)\)"/g)]
    .map((m) => {
      const n = m[1].match(/-?[\d.]+/g).map(Number);
      return { y1: n[1], y2: n[n.length - 1], head: m[2] };
    });
  check("an arc arriving where two paths meet gets the merge head",
        heads.filter((h) => h.head === "arrow-merge").length === 4,
        heads.map((h) => `${h.y1}->${h.y2} ${h.head}`));
  check("...and every other arc keeps the plain one",
        heads.filter((h) => h.head === "arrow").length === heads.length - 4,
        heads.length);
  /* A loop back is a retry, not a convergence — one path returning to itself.
     Counting it would make every retry target a merge, and `rebase` would
     claim three ways in when a reader can only arrive from two. */
  check("a loop back is never drawn as a merge",
        heads.every((h) => !(h.head === "arrow-merge" && h.y2 < h.y1)),
        heads.filter((h) => h.y2 < h.y1).map((h) => h.head));

  /* The same two facts as numbers, on the box the reader is already on. */
  const flagLines = [...svg.matchAll(
    /<text class="wfd-flags"[^>]*>(?:<title>([^<]*)<\/title>)?([^<]*)</g)]
    .map((m) => ({ full: m[1], shown: m[2] }));
  const flagText = flagLines.map((f) => f.shown);
  check("the two steps two paths meet at say so, and no others do",
        flagText.filter((t) => t.includes("merge:")).length === 2
          && flagText.filter((t) => t.includes("merge:2")).length === 2,
        flagText);
  /* Forks count DESTINATIONS, not options: await-landing offers three
     (landed / rebase / remeasure) that lead to two places, so `select:agent`
     alone tells the reader the wrong number. */
  check("a fork counts where the run can go, not how many options say it",
        flagText.filter((t) => t.includes("fork:2")).length === 4
          && !flagText.some((t) => t.includes("fork:3")),
        flagText);

  /* This line was never cut to the box before `fork:`/`merge:` were added to
     it. No shipped workflow overflows it today — which is exactly the state
     the titles were in right before one did. */
  check("no flags line draws wider than its box",
        flagText.every((t) => wfdTextW(t, 10) <= BOX),
        flagText.map((t) => [t, Math.round(wfdTextW(t, 10))]));
  check("a flags line that has to be cut keeps its whole text on a hover",
        flagLines.every((f) => (f.shown.endsWith("…")) === (f.full != null)),
        flagLines);
  const LONG = "gate · verify · select:agent · paced · fork:3 · merge:2";
  check("...and that budget actually bites on a line long enough to need it",
        wfdTextW(LONG, 10) > BOX && wfdTextW(wfdFit(LONG, 10, BOX), 10) <= BOX,
        [Math.round(wfdTextW(LONG, 10)), wfdFit(LONG, 10, BOX)]);

  /* The picture must not have grown a lane or an arc to say any of this. */
  check("marking merges costs no arcs and no upward arcs",
        heads.length === 16 && heads.filter((h) => h.y2 < h.y1).length === 2,
        [heads.length, heads.filter((h) => h.y2 < h.y1).length]);
}

if (failures) {
  console.log(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("flowtrack_check: ok");
