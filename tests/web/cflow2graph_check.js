/* The cflow v2 run page's chart, drawn as a graph instead of a callstack
   (claunch-g6uma). The person asked for four things and each is pinned here
   against the real functions sliced out of app.js:

   - a mode the reader can switch, so the callstack is still there;
   - a `loop` as a bounding box around its body;
   - the direction of the flow — down for `seq`, side by side for branches,
     forked above and joined below, so `par` and the aggregating joins read
     as what they are;
   - one shared node per procedure body however many places call it, and the
     `call` itself drawn as a node.

   Geometry is data (cflow2GraphLayout returns boxes), so none of this needs
   a browser: the chart below is the shape /api/cflow2 sends. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}
/* A one-line `const NAME = ...;` declaration, copied with its real value so
   the harness cannot drift from what ships. */
function sliceConst(name) {
  const start = src.indexOf(`const ${name} =`);
  if (start < 0) throw new Error(`cannot locate const ${name} in app.js`);
  const end = src.indexOf("\n", start);
  return src.slice(start, end).replace(/;$/, "") + ";";
}
/* The same, for a declaration that runs over several lines — an array or an
   object reads better one entry per line, and the harness still copies the
   shipped value instead of a copy of it. */
function sliceConstMulti(name) {
  const start = src.indexOf(`const ${name} =`);
  if (start < 0) throw new Error(`cannot locate const ${name} in app.js`);
  let depth = 0;
  for (let j = src.indexOf("=", start); j < src.length; j++) {
    if (src[j] === "[" || src[j] === "{") depth++;
    else if (src[j] === "]" || src[j] === "}") {
      depth--;
      if (!depth) return src.slice(start, src.indexOf(";", j) + 1);
    }
  }
  throw new Error(`unbalanced const ${name}`);
}

const CONSTS = ["CF2G_NODE_W", "CF2G_NODE_H", "CF2G_GAP", "CF2G_LANE", "CF2G_PAD",
                "CF2G_HEAD", "CF2G_CHIP", "CF2G_RAIL", "CF2G_EDGE", "CF2G_MARGIN",
                "CF2G_GUTTER", "CF2G_ATOMIC", "CF2G_BRANCHES", "cf2ChartModes",
                "CF2G_HANDLE", "CF2G_PRESET_DEPTH", "cf2GraphPresets", "cf2GraphFolds"];
/* the presets are written one per line, so the harness copies them whole */
const BLOCK_CONSTS = ["CF2G_PRESETS"];

/* ---- stub DOM, just enough for the mode tabs -------------------------- */
function stubNode(tag) {
  return {
    tag, children: [], attrs: {}, listeners: {}, className: "", textContent: "",
    type: "",
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k]; },
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
    appendChild(c) { this.children.push(c); return c; },
    click() { for (const fn of this.listeners.click || []) fn(); },
  };
}
const store = {};
globalThis.document = { createElement: (t) => stubNode(t) };
globalThis.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
};

const fns = new Function(
  CONSTS.map(sliceConst).join("\n") + "\n" +
  BLOCK_CONSTS.map(sliceConstMulti).join("\n") + "\n" +
  [slice("cflow2GraphClip"), slice("cflow2GraphLoopText"), slice("cflow2GraphChip"),
   slice("cflow2GraphSig"), slice("cflow2GraphSite"), slice("cflow2GraphProcs"), slice("cflow2GraphOpen"),
   slice("cflow2GraphLayout"), slice("cflow2GraphSvg"), slice("cflow2GraphInto"),
   slice("cflow2GraphHidden"),
   slice("cflow2NodeBits"),
   slice("escXml"), slice("el"), slice("cflow2ChartMode"), slice("cflow2ChartModeTabs"),
   slice("cflow2GraphPreset"), slice("cflow2GraphPresetSet"),
   slice("cflow2GraphPresetPick"), slice("cflow2GraphPresetTabs")].join("\n") +
  "\nreturn { cflow2GraphLayout, cflow2GraphProcs, cflow2GraphOpen, cflow2GraphSvg," +
  " cflow2GraphSig, cflow2GraphSite, cflow2NodeBits, cflow2ChartMode, cflow2ChartModeTabs," +
  " cflow2GraphInto, cflow2GraphPreset, cflow2GraphPresetSet, cflow2GraphPresetPick," +
  " cflow2GraphPresetTabs," +
  " cflow2GraphHidden," +
  " CF2G_ATOMIC, CF2G_BRANCHES, CF2G_PAD, CF2G_PRESETS, cf2GraphPresets, cf2GraphFolds };"
)();

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}
function ok(what, cond, detail) {
  if (!cond) { console.error(`FAIL ${what}${detail ? `\n  ${detail}` : ""}`); failures++; }
}

/* ---- the chart /api/cflow2 sends -------------------------------------- */
function fixture() {
  return {
    root: "root",
    nodes: {
      root: { id: "root", type: "seq", children: ["t1", "lp", "pa", "ca", "cb", "z"],
              roles: ["", "", "", "", "", ""] },
      t1: { id: "t1", type: "task", label: "t1", title: "first", children: [], roles: [] },
      lp: { id: "lp", type: "loop", mode: "until", max: 3, guard: "done", children: ["lb"],
            roles: ["do"] },
      lb: { id: "lb", type: "task", label: "body", children: [], roles: [] },
      pa: { id: "pa", type: "par", settle: "all", children: ["a1", "a2"],
            roles: ["branch w1", "branch w2"] },
      a1: { id: "a1", type: "task", label: "a1", children: [], roles: [] },
      a2: { id: "a2", type: "task", label: "a2", children: [], roles: [] },
      ca: { id: "ca", type: "scope", proc: "P", children: ["ba"], roles: [""] },
      ba: { id: "ba", type: "task", label: "step", children: [], roles: [] },
      cb: { id: "cb", type: "scope", proc: "P", children: ["bb"], roles: [""] },
      bb: { id: "bb", type: "task", label: "step", children: [], roles: [] },
      z: { id: "z", type: "task", label: "z", children: [], roles: [] },
    },
  };
}
const ALL = () => new Set(Object.keys(fixture().nodes));
const nodeOf = (g, id) => g.nodes.find((n) => n.id === id);
const boxOf = (g, id) => g.boxes.find((b) => b.id === id);
const edges = (g, kind) => g.edges.filter((e) => e.kind === kind);
const inside = (outer, inner) => inner.x >= outer.x && inner.y >= outer.y &&
  inner.x + inner.w <= outer.x + outer.w && inner.y + inner.h <= outer.y + outer.h;

/* ---- collapsed by default: one box per top-level step ---------------- */
{
  const chart = fixture();
  const g = fns.cflow2GraphLayout(chart, { instances: [] }, { open: new Set(["root"]) });
  check("a collapsed chart draws the root's steps only",
        g.nodes.map((n) => n.id).sort(), ["ca", "cb", "lp", "pa", "t1", "z"]);
  check("a collapsed loop says how many it hides",
        nodeOf(g, "lp").sub, "until ≤ 3 · done · +1 collapsed");
  ok("a collapsed step offers to open", nodeOf(g, "lp").toggle === true);
  ok("a leaf has nothing to open", nodeOf(g, "t1").toggle === false);
  check("no loop frame while it is collapsed", g.boxes.length, 0);
}

/* ---- the sequence runs downward --------------------------------------- */
{
  const chart = fixture();
  const g = fns.cflow2GraphLayout(chart, { instances: [] }, { open: ALL() });
  const t1 = nodeOf(g, "t1"), lp = boxOf(g, "lp"), z = nodeOf(g, "z");
  ok("a task sits above the step after it", t1.y + t1.h <= lp.y, `${t1.y + t1.h} vs ${lp.y}`);
  ok("the last step is below the parallel group", z.y > lp.y);
  const seq = edges(g, "seq").filter((e) => e.from === "t1" && e.to === "lp");
  check("an arrow joins consecutive steps", seq.length, 1);
  ok("the arrow is drawn from the first box's bottom to the next one's top",
     seq[0].d.startsWith(`M${t1.x + t1.w / 2} ${t1.y + t1.h}`), seq[0].d);
}

/* ---- a loop is a bounding box with its retry running back up ---------- */
{
  const chart = fixture();
  const g = fns.cflow2GraphLayout(chart, { instances: [] }, { open: ALL() });
  const frame = boxOf(g, "lp"), body = nodeOf(g, "lb");
  ok("the loop is drawn as a frame", frame && frame.kind === "loop");
  ok("the body is inside the frame", inside(frame, body),
     `frame ${JSON.stringify(frame)} body ${JSON.stringify(body)}`);
  check("the frame is titled with its mode and bound", frame.text, "↻ until ≤ 3 done");
  const back = edges(g, "back");
  check("one retry edge", back.length, 1);
  check("the retry goes from the body back into the body", [back[0].from, back[0].to], ["lb", "lb"]);
  ok("the retry is drawn as its own kind", fns.cflow2GraphSvg(g).includes("cf2g-edge back"));
  const rule = css.slice(css.indexOf(".cf2g-edge.back"), css.indexOf("}", css.indexOf(".cf2g-edge.back")));
  ok("and reads as dashed, not as a fall-through", /stroke-dasharray/.test(rule), rule);
}

/* ---- branches stand side by side, forked above and joined below ------- */
{
  const chart = fixture();
  const g = fns.cflow2GraphLayout(chart, { instances: [] }, { open: ALL() });
  const a1 = nodeOf(g, "a1"), a2 = nodeOf(g, "a2"), pa = boxOf(g, "pa");
  check("the branches share a row", a1.y, a2.y);
  ok("the first branch is left of the second", a1.x + a1.w <= a2.x, `${a1.x + a1.w} vs ${a2.x}`);
  ok("both branches sit inside the group", inside(pa, a1) && inside(pa, a2));
  const forks = edges(g, "fork").filter((e) => e.from === "pa");
  const joins = edges(g, "join").filter((e) => e.to === "pa");
  check("one fork per branch", forks.map((e) => e.to).sort(), ["a1", "a2"]);
  check("one join per branch", joins.map((e) => e.from).sort(), ["a1", "a2"]);
  check("the forks carry the branch names", forks.map((e) => e.label).sort(),
        ["branch w1", "branch w2"]);
  ok("the join lands on the group's bottom edge",
     joins.every((e) => e.d.endsWith(`${pa.x + pa.w / 2} ${pa.y + pa.h}`)), joins[0].d);
  check("the group is captioned with its settle mode",
        g.chips.find((c) => c.id === "pa").text, "∥ par · settle all");
}

/* ---- one shared node for a procedure called twice --------------------- */
{
  const chart = fixture();
  const procs = fns.cflow2GraphProcs(chart);
  check("both call sites are one body", [procs.first.get("ca"), procs.first.get("cb")], ["ca", "ca"]);
  check("the body lists its sites", procs.sites.get("ca"), ["ca", "cb"]);

  const g = fns.cflow2GraphLayout(chart, { instances: [] }, { open: ALL() });
  const frame = boxOf(g, "ca"), again = nodeOf(g, "cb");
  ok("the first call draws the body once", frame && frame.kind === "call");
  check("only the first call has a body box", g.nodes.filter((n) => n.id === "ba").length, 1);
  ok("the first call's frame says it is shared", frame.shared === true);
  check("the frame names the procedure and its use count", frame.text, "call P · shared ×2");
  check("the other call is a reference node", again.kind, "ref");
  check("the reference names the procedure", again.head, "call P");
  check("the reference names the shared body", again.ref, "ca");
  check("the reference keeps its own call site", again.sub, "cb · shared ×2");
  check("the frame keeps the call site that drew it", frame.sub, "ca");
  const ref = edges(g, "ref");
  check("one reference edge", ref.length, 1);
  check("the reference points at the shared node", [ref[0].from, ref[0].to], ["cb", "ca"]);
}

/* A call with different block parameters is a different body: the shape
   travels in the signature, so the two sites must not be merged. */
{
  const chart = fixture();
  chart.nodes.bb = { id: "bb", type: "task", label: "other step", children: [], roles: [] };
  const procs = fns.cflow2GraphProcs(chart);
  check("a different body is not shared", procs.first.get("cb"), "cb");
  check("each site is its own body", (procs.sites.get("cb") || []).length, 1);
  const g = fns.cflow2GraphLayout(chart, { instances: [] }, { open: ALL() });
  check("both calls draw a frame", g.boxes.filter((b) => b.kind === "call").length, 2);
  check("no reference edge", edges(g, "ref").length, 0);
}

/* Each call site keeps its own name, which is the only thing that tells two
   calls of one shared body apart in the picture. */
{
  check("a call site is named by its step",
        fns.cflow2GraphSite("round/resolve/issue-decision.call"), "issue-decision");
  check("a nested site keeps its own path", fns.cflow2GraphSite("settle/settle-stall.call"),
        "settle-stall");
  check("a position alone is not a name",
        fns.cflow2GraphSite("round/land/already-landed/0"), "already-landed/0");
  check("no id, no name", fns.cflow2GraphSite(null), "");
}

/* ---- a single call is still an explicit call node -------------------- */
{
  const chart = fixture();
  delete chart.nodes.cb;
  delete chart.nodes.bb;
  chart.nodes.root.children = ["ca", "z"];
  chart.nodes.root.roles = ["", ""];
  const g = fns.cflow2GraphLayout(chart, { instances: [] }, { open: ALL() });
  check("one call frame", g.boxes.filter((b) => b.kind === "call").length, 1);
  check("an unshared call is titled plainly", boxOf(g, "ca").text, "call P");
}

/* ---- nested: nothing lands on anything else -------------------------- */
{
  const chart = {
    root: "n",
    nodes: {
      n: { id: "n", type: "seq", children: ["p", "t"], roles: ["", ""] },
      p: { id: "p", type: "par", settle: "allSettled", children: ["lp", "ch"],
           roles: ["branch a", "branch b"] },
      lp: { id: "lp", type: "loop", mode: "while", max: 4, children: ["s"], roles: ["do"] },
      s: { id: "s", type: "seq", children: ["s1", "s2"], roles: ["", ""] },
      s1: { id: "s1", type: "task", label: "s1", children: [], roles: [] },
      s2: { id: "s2", type: "task", label: "s2", children: [], roles: [] },
      ch: { id: "ch", type: "choose", chooser: "agent", children: ["o1", "o2"],
            roles: ["option yes", "option no"] },
      o1: { id: "o1", type: "task", label: "o1", children: [], roles: [] },
      o2: { id: "o2", type: "task", label: "o2", children: [], roles: [] },
      t: { id: "t", type: "task", label: "t", children: ["tf"], roles: ["on fail"] },
      tf: { id: "tf", type: "control", op: "raise", children: [], roles: [] },
    },
  };
  const g = fns.cflow2GraphLayout(chart, {}, { open: new Set(Object.keys(chart.nodes)) });
  const leaves = g.nodes.filter((b) => b.kind === "leaf");
  const hits = [];
  for (let i = 0; i < leaves.length; i++) {
    for (let j = i + 1; j < leaves.length; j++) {
      const a = leaves[i], b = leaves[j];
      if (a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h) {
        hits.push([a.id, b.id]);
      }
    }
  }
  check("no two steps are drawn on top of each other", hits, []);
  ok("the loop sits inside the parallel branch",
     inside(boxOf(g, "p"), boxOf(g, "lp")), JSON.stringify(boxOf(g, "lp")));
  ok("both alternatives are inside the choose group", inside(boxOf(g, "ch"), nodeOf(g, "o1")) &&
     inside(boxOf(g, "ch"), nodeOf(g, "o2")));
  ok("the choose's options are on one row and labelled",
     nodeOf(g, "o1").y === nodeOf(g, "o2").y &&
     edges(g, "fork").filter((e) => e.from === "ch").map((e) => e.label).sort()
       .join("|") === "option no|option yes");
  ok("an `on` outcome hangs below the step it answers",
     nodeOf(g, "tf").y >= nodeOf(g, "t").y + nodeOf(g, "t").h &&
     edges(g, "seq").some((e) => e.from === "t" && e.to === "tf" && e.label === "on fail"));
}

/* ---- the run's position is opened, the rest collapsed ---------------- */
{
  const chart = fixture();
  const detail = { instances: [{ instance: "i1", node: "a2", await: "task" }] };
  const open = fns.cflow2GraphOpen(chart, detail, null, null);
  ok("the root is always open", open.has("root"));
  ok("a waiting instance's ancestors are opened", open.has("pa"));
  ok("nothing else is", !open.has("lp"));
  const g = fns.cflow2GraphLayout(chart, detail, { open });
  ok("the waiting step is drawn", !!nodeOf(g, "a2"));
  check("it is marked as the one awaiting", nodeOf(g, "a2").awaiting, "task");
  check("and nothing near it is invented", nodeOf(g, "t1").awaiting, "");

  const withSel = fns.cflow2GraphOpen(chart, { instances: [] }, "lb", null);
  ok("the selection's ancestors are opened", withSel.has("lp") && withSel.has("root"));
  const toggled = fns.cflow2GraphOpen(chart, { instances: [] }, null, { lp: true });
  ok("a person's toggle opens a container", toggled.has("lp"));
  const folded = fns.cflow2GraphOpen(chart, detail, null, { pa: false });
  ok("a person's toggle folds one back", !folded.has("pa"));
}

/* ---- the presets: how much of the definition is unfolded --------------
   (claunch-g6uma.2.1, thread th-e39112) The row answers two questions.
   Simplified is where the run is; the levels are structure, and a level counts
   block bodies — a loop's, a `call`'s and a branch's alike. `deep()` has one
   loop whose body holds a seq and a par, so `lv2` opens the loop and `lv3`
   opens what it holds. */
function deep() {
  return {
    root: "d0",
    nodes: {
      d0: { id: "d0", type: "seq", children: ["a", "d1"], roles: ["", ""] },
      a: { id: "a", type: "task", label: "a", children: [], roles: [] },
      d1: { id: "d1", type: "loop", mode: "while", guard: "more", children: ["d2"],
            roles: ["do"] },
      d2: { id: "d2", type: "seq", children: ["b", "d3"], roles: ["", ""] },
      b: { id: "b", type: "task", label: "b", children: [], roles: [] },
      d3: { id: "d3", type: "par", settle: "all", children: ["c1", "c2"], roles: ["", ""] },
      c1: { id: "c1", type: "task", label: "c1", children: [], roles: [] },
      c2: { id: "c2", type: "task", label: "c2", children: [], roles: [] },
    },
  };
}
/* two loops, one inside the other: each level on this chart opens exactly one
   more block, which is what tells the levels apart at all */
function nest() {
  return {
    root: "n0",
    nodes: {
      n0: { id: "n0", type: "seq", children: ["p", "L1", "q"], roles: ["", "", ""] },
      p: { id: "p", type: "task", label: "p", children: [], roles: [] },
      L1: { id: "L1", type: "loop", mode: "while", guard: "more", children: ["b1"], roles: ["do"] },
      b1: { id: "b1", type: "seq", children: ["s1", "L2"], roles: ["", ""] },
      s1: { id: "s1", type: "task", label: "s1", children: [], roles: [] },
      L2: { id: "L2", type: "loop", mode: "for", items: "xs", children: ["b2"], roles: ["do"] },
      b2: { id: "b2", type: "seq", children: ["s2"], roles: [""] },
      s2: { id: "s2", type: "task", label: "s2", children: [], roles: [] },
      q: { id: "q", type: "task", label: "q", children: [], roles: [] },
    },
  };
}
{
  const chart = deep();
  const opened = (preset) => [...fns.cflow2GraphOpen(chart, {}, null, null, preset)].sort();
  check("the ladder has the presets the page offers", fns.CF2G_PRESETS.length, 5);
  check("simplified shows the root's steps and follows the run", opened("simple"), ["d0"]);
  check("one level opens the root and its own blocks' bodies",
        opened("lv1"), ["d0", "d1"]);
  check("two levels opens the blocks the loop body holds",
        opened("lv2"), ["d0", "d1", "d2", "d3"]);
  check("three levels opens no more — this chart is that shallow",
        opened("lv3"), ["d0", "d1", "d2", "d3"]);
  check("all opens the deepest block too", opened("all"), ["d0", "d1", "d2", "d3"]);
  check("a preset the page does not offer reads as simplified", opened("lv9"), ["d0"]);

  /* the levels are a ladder among themselves. `simple` is deliberately not on
     it: it answers where the run is, not how deep the picture goes, and the
     two are not comparable (claunch-g6uma.2.1). */
  const levels = ["lv1", "lv2", "lv3", "all"];
  const gaps = [];
  for (let i = 1; i < levels.length; i++) {
    const now = new Set(opened(levels[i]));
    for (const id of opened(levels[i - 1])) if (!now.has(id)) gaps.push([levels[i], id]);
  }
  check("each level opens at least the level below it", gaps, []);

  check("a fold laid on a level closes that block again",
        [...fns.cflow2GraphOpen(chart, {}, null, { d1: false }, "all")].sort(),
        ["d0", "d2", "d3"]);
  check("an open laid on simplified opens that block",
        [...fns.cflow2GraphOpen(chart, {}, null, { d1: true }, "simple")].sort(),
        ["d0", "d1"]);
}

/* ---- the root's own type does not shift the ladder --------------------
   A definition wrapped in a `try` and one sitting in a plain `seq` must give
   the same `lv1`: the chart's root is the frame of the whole thing, not a body
   a level opens. A branch body counts a level like a loop's, which is the
   person's rule read on the other kind of block (thread th-e39112). */
{
  const chart = {
    root: "w0",
    nodes: {
      w0: { id: "w0", type: "try", children: ["b1", "k1"], roles: ["body", "catch"] },
      b1: { id: "b1", type: "seq", children: ["s", "T"], roles: ["", ""] },
      s: { id: "s", type: "task", label: "s", children: [], roles: [] },
      T: { id: "T", type: "try", children: ["b2", "k2"], roles: ["body", "catch"] },
      b2: { id: "b2", type: "seq", children: ["u"], roles: [""] },
      k2: { id: "k2", type: "seq", children: [], roles: [""] },
      u: { id: "u", type: "task", label: "u", children: [], roles: [] },
      k1: { id: "k1", type: "task", label: "k1", children: [], roles: [] },
    },
  };
  const openOf = (preset) => [...fns.cflow2GraphOpen(chart, {}, null, null, preset)].sort();
  check("a branch at the root does not spend the first level",
        openOf("lv1"), ["T", "b1", "w0"]);
  check("the body inside that branch is what the second level opens",
        openOf("lv2"), ["T", "b1", "b2", "w0"]);
  const lv1 = fns.cflow2GraphLayout(chart, {}, {
    open: fns.cflow2GraphOpen(chart, {}, null, null, "lv1") });
  check("…and the first level draws it as one line", !!boxOf(lv1, "b2"), false);
}

/* ---- the levels are one ladder, and every block body counts a level -----
   (claunch-g6uma.2.1, thread th-e39112) The person's first report was that the
   levels read as part of Simplified: they folded the run's path away, so a
   level and Simplified were two answers to one question. The answer the person
   then gave is the rule here — a level counts block bodies, and a loop, a
   `call` and a branch are the same unit. The two questions stay apart: the
   levels draw the structure, Simplified follows the run, and the levels report
   a run they leave out instead of bending the structure around it. `nest()` is
   two nested loops, so each level opens exactly one more block. */
{
  const chart = nest();
  const openOf = (preset) => fns.cflow2GraphOpen(chart, {}, null, null, preset);
  const layoutOf = (preset) => fns.cflow2GraphLayout(chart, {}, { open: openOf(preset) });
  const levels = ["lv1", "lv2", "lv3", "all"];

  /* the outline above the fold: the root's own blocks are on the picture at
     every level, one line each while the level is too shallow for them */
  check("every level draws the root's own blocks",
        levels.map((p) => ["p", "L1", "q"].filter((id) => !layoutOf(p).drawn.has(id))),
        [[], [], [], []]);
  check("the outer loop is a frame at every level",
        levels.map((p) => !!boxOf(layoutOf(p), "L1")), [true, true, true, true]);
  check("the loop inside a body is a frame from two levels on",
        levels.map((p) => !!boxOf(layoutOf(p), "L2")), [false, true, true, true]);

  /* a level counts block bodies, and a plain `seq` between two of them is not
     one: `b1` and `L2` both sit one block body inside `L1`, so `lv2` opens
     them together and `lv3` opens the body below them */
  check("each level opens one more block, whatever kind it is",
        levels.map((p) => [...openOf(p)].sort()),
        [["L1", "n0"], ["L1", "L2", "b1", "n0"], ["L1", "L2", "b1", "b2", "n0"],
         ["L1", "L2", "b1", "b2", "n0"]]);
  check("on this chart each level folds one block fewer",
        levels.map((p) => layoutOf(p).folded.length), [1, 1, 0, 0]);
  check("simplified folds the structure away, which is what it is for",
        layoutOf("simple").folded.slice().sort(), ["L1"]);
  check("the folded blocks are blocks, not leaves or call references",
        levels.map((p) => layoutOf(p).folded.filter((id) => !chart.nodes[id].children.length)),
        [[], [], [], []]);

  /* A level draws the structure and does not follow the run: the run's path
     would otherwise be most of the picture on a deep chart, and the levels
     would all become Simplified. What a level leaves out is reported under the
     picture, with a press that goes to Simplified (ask2-1b15e9b5). */
  const atS1 = { instances: [{ instance: "i1", node: "s1", await: "task" }] };
  const drawnAt = (preset) => fns.cflow2GraphLayout(chart, atS1,
    { open: fns.cflow2GraphOpen(chart, atS1, null, null, preset) });
  ok("simplified follows the run down to where it waits",
     !!nodeOf(drawnAt("simple"), "s1"));
  check("the levels reach the run only where their own depth does",
        levels.map((p) => !!nodeOf(drawnAt(p), "s1")), [false, true, true, true]);
  check("…and every level that cannot reach it names it",
        levels.map((p) => fns.cflow2GraphHidden(drawnAt(p), atS1)),
        [["s1"], [], [], []]);
  check("a deeper run is on no level until the level that opens its block",
        ["lv1", "lv2", "lv3", "all"].map((p) => {
          const d = { instances: [{ instance: "i1", node: "s2", await: "task" }] };
          const g = fns.cflow2GraphLayout(chart, d,
            { open: fns.cflow2GraphOpen(chart, d, null, null, p) });
          return [!!nodeOf(g, "s2"), fns.cflow2GraphHidden(g, d).length];
        }),
        [[false, 1], [false, 1], [true, 0], [true, 0]]);

  /* a selection is a person's own focus, so its path is open at every preset —
     the one lift the levels keep */
  const sel = fns.cflow2GraphOpen(chart, {}, "s2", null, "lv1");
  ok("a selected step's path is open at a level too", sel.has("L2") && sel.has("b2"));
}

/* ---- a run that sits deep, and the level that reports it ----------------
   (s2273-rev2, claunch-g6uma.2.1) A level draws the structure, so a deep run
   can be off its picture; the note under the drawing is what keeps that from
   being silent, and Simplified — one press away — is where the run is drawn.
   A manual fold is the other way to lose it, and the same note covers both. */
{
  const chart = deep();
  const detail = { instances: [{ instance: "i1", node: "c1", await: "task" }] };
  const openOf = (preset) => fns.cflow2GraphOpen(chart, detail, null, null, preset);
  const layoutOf = (preset) => fns.cflow2GraphLayout(chart, detail, { open: openOf(preset) });

  check("simplified opens the whole path down to where the run waits",
        [...openOf("simple")].sort(), ["d0", "d1", "d2", "d3"]);
  const sim = layoutOf("simple");
  ok("…and draws the step that is waiting",
     !!nodeOf(sim, "c1") && nodeOf(sim, "c1").awaiting === "task");
  check("a level draws the run only where its own depth reaches",
        ["lv1", "lv2", "lv3", "all"].map((p) => !!nodeOf(layoutOf(p), "c1")),
        [false, true, true, true]);
  check("…and names it everywhere it does not",
        ["lv1", "lv2", "lv3", "all"].map((p) => fns.cflow2GraphHidden(layoutOf(p), detail)),
        [["c1"], [], [], []]);

  check("simplified leaves nothing out", fns.cflow2GraphHidden(sim, detail), []);
  check("so does `all`", fns.cflow2GraphHidden(layoutOf("all"), detail), []);
  check("a chart with nothing running hides nothing",
        fns.cflow2GraphHidden(fns.cflow2GraphLayout(chart, {}, { open: new Set(["d0"]) }), {}), []);
  /* a plain run has no box of its own: its children are placed in its place.
     Counting boxes would call every `seq` on the run's path hidden. A hand
     fold takes the run off the picture the same way a level does, and then the
     note speaks. */
  const nested = fns.cflow2GraphLayout(deep(), detail, { open: new Set(["d0", "d1"]) });
  check("the state is what the walk reached, not what has a box",
        fns.cflow2GraphHidden(nested, detail), ["c1"]);
  ok("the note under the drawing says it, and the way back is one press",
     src.includes("cflow2GraphHidden(layoutNow, data)") &&
     src.includes("1 step of the run is not in this view") &&
     src.includes('"Show the run"') && css.includes(".cf2g-hidden"));
}

/* ---- what unfolding could ever show is the chart's answer --------------
   (s2273-rev2, claunch-g6uma.2) `everything` exists so a step that no fold
   could reveal is not reported as hidden. Settling ownership against the
   picture on screen breaks that: when the site that owns a shared body is
   itself inside a folded block, no open set reaches the body, and the note
   falls silent exactly at the levels that hid the run — speaking only at the
   level deep enough to draw the owner. It is settled against everyOpen in
   its own map now, so it is a function of the chart alone. */
{
  const chart = {
    root: "r",
    nodes: {
      r: { id: "r", type: "seq", children: ["c1"], roles: [""] },
      c1: { id: "c1", type: "seq", children: ["s1"], roles: [""] },
      s1: { id: "s1", type: "scope", proc: "p", children: ["q1"], roles: [""] },
      q1: { id: "q1", type: "scope", proc: "q", children: ["w1"], roles: [""] },
      w1: { id: "w1", type: "task", label: "w1", children: [], roles: [] },
    },
  };
  const detail = { instances: [{ instance: "i1", node: "w1", await: "task" }] };
  const layoutOf = (preset) => fns.cflow2GraphLayout(chart, detail, {
    open: fns.cflow2GraphOpen(chart, detail, null, null, preset) });

  check("the chart's own reach is every step, folds or no folds",
        [...layoutOf("lv1").everything].sort(), ["c1", "q1", "r", "s1", "w1"]);
  const ever = ["simple", "lv1", "lv2", "lv3", "all"]
    .map((p) => [...layoutOf(p).everything].sort().join(","));
  check("and it does not move with the preset", ever.filter((v) => v !== ever[0]), []);
  /* this chart is two `call` bodies deep and holds no loop at all, which is
     the person's rule read back: a call body counts a level exactly like a
     loop, so each level opens one more of them */
  check("each level opens one more call body",
        ["lv1", "lv2", "lv3", "all"].map((p) =>
          [...fns.cflow2GraphOpen(chart, detail, null, null, p)].sort()),
        [["c1", "r", "s1"], ["c1", "q1", "r", "s1"], ["c1", "q1", "r", "s1"],
         ["c1", "q1", "r", "s1"]]);
  check("a level that folds the run's call bodies reports the run",
        ["lv1", "lv2", "lv3", "all"].map((p) => fns.cflow2GraphHidden(layoutOf(p), detail)),
        [["w1"], [], [], []]);
  check("simplified has nothing to report", fns.cflow2GraphHidden(layoutOf("simple"), detail), []);
  check("so does all", fns.cflow2GraphHidden(layoutOf("all"), detail), []);

  /* the other half of the rule: a step under a site that is not its body's
     owner is never drawn by any open set, so it must stay silent rather than
     be counted as folded away. */
  const sharedChart = fixture();
  const sDetail = { instances: [{ instance: "i1", node: "bb", await: "task" }] };
  const sLayout = (preset) => fns.cflow2GraphLayout(sharedChart, sDetail, {
    open: fns.cflow2GraphOpen(sharedChart, sDetail, null, null, preset) });
  ok("the non-owning site really owns nothing",
     !nodeOf(sLayout("all"), "bb") && !!nodeOf(sLayout("all"), "ba"));
  check("a step under the non-owning site is not reported at any preset",
        ["simple", "lv1", "lv2", "lv3", "all"]
          .map((p) => fns.cflow2GraphHidden(sLayout(p), sDetail)),
        [[], [], [], [], []]);
}

/* ---- a group's caption shares its row with the fold control -----------
   (s2273-rev2) A one-branch group is exactly one box wide and the longest
   captions — `try`, `timeout` — are wider than that, so the caption used to
   run under the control. */
{
  const layoutFor = (node) => fns.cflow2GraphLayout({
    root: "r",
    nodes: {
      r: { id: "r", type: "seq", children: ["g"], roles: [""] },
      g: node,
      t: { id: "t", type: "task", label: "t", children: [], roles: [] },
      u: { id: "u", type: "task", label: "u", children: [], roles: [] },
    },
  }, {}, { open: new Set(["r", "g"]) });
  /* 10.5px monospace is about 6.3px a character: the caption ends left of the
     control, which starts 20px short of the frame's right edge */
  const clears = (layout) => {
    const chip = layout.chips[0], fold = layout.folds.find((f) => f.id === "g");
    return chip.x + 2 + chip.text.length * 6.3 <= fold.x;
  };
  const narrow = [
    ["timeout", { id: "g", type: "timeout", seconds: 30, children: ["t"], roles: ["body"] }],
    ["try", { id: "g", type: "try", children: ["t"], roles: ["body"] }],
    ["group", { id: "g", type: "group", name: "rounds", settle: "all", children: ["t"], roles: ["b"] }],
  ];
  check("every one-branch caption stays left of the fold control",
        narrow.filter(([, node]) => !clears(layoutFor(node))).map(([k]) => k), []);
  ok("a cut caption says so", layoutFor(narrow[1][1]).chips[0].text.endsWith("…"));
  check("a wide group keeps its caption whole",
        layoutFor({ id: "g", type: "par", settle: "all", children: ["t", "u"], roles: ["", ""] })
          .chips[0].text, "∥ par · settle all");
}


{
  const chart = fixture();
  const svg = {};
  const dangling = [];
  for (const preset of ["simple", "lv1", "lv2", "lv3", "all"]) {
    const g = fns.cflow2GraphLayout(chart, {},
      { open: fns.cflow2GraphOpen(chart, {}, null, null, preset) });
    svg[preset] = fns.cflow2GraphSvg(g);
    const onScreen = new Set(g.nodes.concat(g.boxes).map((b) => b.id));
    for (const n of g.nodes) if (n.ref && !onScreen.has(n.ref)) dangling.push([preset, n.id]);
  }
  check("no preset leaves a call pointing at a body that is not drawn", dangling, []);
  ok("a preset that draws more says so in the markup",
     svg.simple.length < svg.lv2.length, `${svg.simple.length} vs ${svg.lv2.length}`);

  const open1 = fns.cflow2GraphOpen(chart, {}, null, null, "lv1");
  const open2 = fns.cflow2GraphOpen(chart, {}, null, null, "lv2");
  const lv1 = fns.cflow2GraphLayout(chart, {}, { open: open1 });
  const lv2 = fns.cflow2GraphLayout(chart, {}, { open: open2 });
  /* every block with no other block above it is one level's worth: a loop, a
     par and a `call` are opened alike (thread th-e39112) */
  check("one level already draws the root's blocks as frames",
        [boxOf(lv1, "lp").kind, boxOf(lv1, "pa").kind, boxOf(lv1, "ca").kind],
        ["loop", "group", "call"]);
  check("…and what the loop holds",
        [!!nodeOf(lv1, "lb"), !!nodeOf(lv1, "a1")], [true, true]);
  /* this chart's loop body is one task, so there is no deeper block for a
     second level to open: the levels agree here, which is the point — the
     outline is not what a level changes */
  check("with nothing nested inside a block the levels agree",
        [[...open1].sort(), [...open2].sort()],
        [["ca", "cb", "lp", "pa", "root"], ["ca", "cb", "lp", "pa", "root"]]);
  check("…and fold nothing, so the note stays quiet",
        [lv1.folded.length, lv2.folded.length], [0, 0]);
}

/* ---- a block folds and opens again on the block itself ---------------- */
{
  const chart = deep();
  const all = fns.cflow2GraphOpen(chart, {}, null, null, "all");
  const wide = fns.cflow2GraphLayout(chart, {}, { open: all });
  check("every open block carries a fold control",
        wide.folds.map((f) => f.id).sort(), ["d0", "d1", "d2", "d3"]);
  const wideSvg = fns.cflow2GraphSvg(wide);
  ok("the loop's control is drawn on the loop",
     wideSvg.includes('data-node="d1" data-collapse="1"'));
  ok("a plain run has one too, in its own band",
     wideSvg.includes('data-node="d0" data-collapse="1"'));

  const folded = fns.cflow2GraphOpen(chart, {}, null, { d1: false }, "all");
  const g = fns.cflow2GraphLayout(chart, {}, { open: folded });
  check("folding a block leaves no frame", !!boxOf(g, "d1"), false);
  check("…one box in its place that says what it hides",
        nodeOf(g, "d1").sub, "while ≤ ? · more · +1 collapsed");
  ok("…and that box offers to open it", nodeOf(g, "d1").toggle === true);
  check("nothing inside a folded block is drawn", nodeOf(g, "d2"), undefined);
  check("the folded block has no fold control left",
        g.folds.map((f) => f.id), ["d0"]);
  ok("the box that opens it is the pressable one",
     fns.cflow2GraphSvg(g).includes('data-node="d1" data-toggle="1"'));
  check("opening it again comes back to the same picture",
        [...fns.cflow2GraphOpen(chart, {}, null, { d1: true }, "all")].sort(),
        [...all].sort());

  /* a shared body has one owner: the other sites are calls that point at it,
     so none of them is a block a reader can fold */
  const sharedChart = fixture();
  const sharedG = fns.cflow2GraphLayout(sharedChart, {}, { open: ALL() });
  check("only the site that drew a shared body has a fold control",
        sharedG.folds.filter((f) => f.kind === "call").map((f) => f.id), ["ca"]);
}

/* ---- a press on the drawing goes where it was aimed ------------------ */
{
  const chart = fixture();
  const calls = [];
  const host = stubNode("div");
  const into = fns.cflow2GraphInto(host, chart, { instances: [] }, {
    preset: "lv2", open: {},
    onSelect: (id, collapsed) => calls.push(["select", id, collapsed]),
    onToggle: (id, open) => calls.push(["toggle", id, open]),
  });
  ok("the drawing is put in the host", host.children[0] === into.wrap);
  ok("the fold control is in the markup it drew",
     into.wrap.innerHTML.includes('data-node="lp" data-collapse="1"'));
  const press = (ds) =>
    into.wrap.listeners.click[0]({ target: { closest: () => ({ dataset: ds }) } });
  press({ node: "lp", collapse: "1" });
  press({ node: "lp", toggle: "1" });
  press({ node: "t1" });
  check("a fold control folds its block and selects nothing",
        calls[0], ["toggle", "lp", false]);
  check("a folded block's box asks to open it", calls[1], ["select", "lp", true]);
  check("any other box is a selection", calls[2], ["select", "t1", false]);
  const quiet = fns.cflow2GraphInto(stubNode("div"), chart, {}, { preset: "lv2" });
  quiet.wrap.listeners.click[0]({ target: { closest: () => ({ dataset: { node: "lp", collapse: "1" } }) } });
  ok("a press with nobody listening is harmless", true);
}

/* ---- the markup carries the whole picture ---------------------------- */
{
  const chart = fixture();
  const g = fns.cflow2GraphLayout(chart, { instances: [] }, { open: ALL() });
  const svg = fns.cflow2GraphSvg(g);
  ok("it is one svg", svg.startsWith("<svg class=\"cf2g\"") && svg.endsWith("</svg>"));
  ok("the loop is a box", svg.includes("cf2g-box loop"));
  ok("the call is a frame", svg.includes("cf2g-box call shared"));
  ok("edges are classified", svg.includes("cf2g-edge fork") && svg.includes("cf2g-edge join") &&
     svg.includes("cf2g-edge ref") && svg.includes("cf2g-edge back"));
  ok("every box can be pressed", svg.includes('data-node="t1"') && svg.includes('data-node="cb"'));
  const collapsed = fns.cflow2GraphLayout(chart, { instances: [] }, { open: new Set(["root"]) });
  ok("a collapsed container offers the toggle",
     fns.cflow2GraphSvg(collapsed).includes('data-node="lp" data-toggle="1"'));
  const escaped = fns.cflow2GraphSvg(fns.cflow2GraphLayout({
    root: "r",
    nodes: { r: { id: "r", type: "task", label: "<b>&\"x\"", children: [], roles: [] } },
  }, {}, { open: new Set() }));
  ok("labels are escaped for the svg", escaped.includes("&lt;b&gt;&amp;&quot;x&quot;"));
}

/* A body is drawn where the reader can see it: a site that is not on screen
   cannot be the node the other sites point at (s2273-rev, claunch-g6uma — the
   owner used to be the chart-order first whatever the collapse state, so 24
   of 24 partially expanded sites drew a reference node with no line). */
{
  const chart = {
    root: "root",
    nodes: {
      root: { id: "root", type: "seq", children: ["lp", "cb", "cc"], roles: ["", "", ""] },
      lp: { id: "lp", type: "loop", mode: "until", max: 2, children: ["ca"], roles: ["do"] },
      ca: { id: "ca", type: "scope", proc: "P", children: ["ba"], roles: [""] },
      ba: { id: "ba", type: "task", label: "step", children: [], roles: [] },
      cb: { id: "cb", type: "scope", proc: "P", children: ["bb"], roles: [""] },
      bb: { id: "bb", type: "task", label: "step", children: [], roles: [] },
      cc: { id: "cc", type: "scope", proc: "P", children: ["bc"], roles: [""] },
      bc: { id: "bc", type: "task", label: "step", children: [], roles: [] },
    },
  };
  check("the chart-order first site is still where the group is keyed",
        fns.cflow2GraphProcs(chart).first.get("cc"), "ca");

  const g = fns.cflow2GraphLayout(chart, {}, { open: new Set(["root"]) });
  check("only the sites on screen are drawn",
        [!!nodeOf(g, "ca"), !!nodeOf(g, "cb"), !!nodeOf(g, "cc")], [false, true, true]);
  check("the first site on screen owns the body", nodeOf(g, "cb").ref, "");
  check("the next site on screen points at it", nodeOf(g, "cc").ref, "cb");
  check("one reference edge, between the two drawn sites",
        edges(g, "ref").map((e) => [e.from, e.to]), [["cc", "cb"]]);

  /* opening one site's ancestors and nothing else: no node is left pointing
     at a body that is not drawn */
  const dangling = [];
  for (const site of ["ca", "cb", "cc"]) {
    const opened = fns.cflow2GraphOpen(chart, {}, site, null);
    const gs = fns.cflow2GraphLayout(chart, {}, { open: opened });
    for (const n of gs.nodes) {
      if (n.ref && !gs.nodes.some((m) => m.id === n.ref)) dangling.push([site, n.id]);
    }
  }
  check("no drawn site points at a node that is not drawn", dangling, []);

  const wide = fns.cflow2GraphLayout(chart, {}, { open: new Set(Object.keys(chart.nodes)) });
  check("with everything open the chart-order first draws the body again",
        [boxOf(wide, "ca").kind, nodeOf(wide, "cb").ref, nodeOf(wide, "cc").ref],
        ["call", "ca", "ca"]);
  check("and both other sites reach it", edges(wide, "ref").map((e) => e.to), ["ca", "ca"]);
}

/* ---- the two views are a radio group, and the choice is remembered --- */
{
  check("a host nobody has switched starts on the callstack",
        fns.cflow2ChartMode("fresh"), "callstack");
  store["claunch.cflow2.chartmode.odd"] = "diagram";
  check("a stored value that is neither view falls back to the callstack",
        fns.cflow2ChartMode("odd"), "callstack");

  let draws = 0;
  const group = fns.cflow2ChartModeTabs("host-a", "callstack", () => { draws++; });
  check("it is one radio group of two", [group.attrs.role, group.children.length], ["radiogroup", 2]);
  ok("each button is a radio", group.children.every((b) => b.attrs.role === "radio"));
  check("the callstack is the checked one",
        group.children.map((b) => [b.textContent, b.attrs["aria-checked"]]),
        [["Callstack", "true"], ["Graph", "false"]]);

  group.children[1].click();
  check("pressing Graph switches the mode", fns.cflow2ChartMode("host-a"), "graph");
  check("…remembered for this viewer", store["claunch.cflow2.chartmode.host-a"], "graph");
  check("…and asks for one redraw", draws, 1);

  const back = fns.cflow2ChartModeTabs("host-a", fns.cflow2ChartMode("host-a"), () => { draws++; });
  check("the redrawn tabs check the mode that was picked",
        back.children.map((b) => b.attrs["aria-checked"]), ["false", "true"]);

  ok("the chart section reads the stored mode and draws the tabs from it",
     src.includes("const chartMode = cflow2ChartMode(host);") &&
     src.includes("sec.appendChild(cflow2ChartModeTabs(host, chartMode, redraw));"));
  ok("the graph and the callstack are the branches the mode picks between",
     src.includes('chartMode === "graph"'));
}

/* ---- the presets are a sub radio group, and the choice is remembered -- */
{
  check("a host nobody has switched starts on the simplified view",
        fns.cflow2GraphPreset("fresh"), "simple");
  store["claunch.cflow2.graphpreset.odd"] = "lv9";
  check("a stored value that is no preset falls back to the simplified view",
        fns.cflow2GraphPreset("odd"), "simple");

  let draws = 0;
  const group = fns.cflow2GraphPresetTabs("host-g", "lv2", () => { draws++; });
  check("it is one sub radio group of the presets",
        [group.attrs.role, group.attrs["aria-label"], group.className],
        ["radiogroup", "Graph detail", "cf2-modes sub"]);
  /* Simplified answers a different question from the levels, so the row says
     so: a rule and a caption stand between it and the ladder (ask2-1b15e9b5). */
  check("the row is Simplified, a rule, a caption, then the ladder",
        group.children.map((c) => c.attrs.role || c.className), [
          "radio", "cf2-modes-sep", "cf2-modes-cap",
          "radio", "radio", "radio", "radio",
        ]);
  check("the caption names what the ladder counts", group.children[2].textContent, "block detail");
  check("the rule is decoration, not a control",
        group.children[1].attrs["aria-hidden"], "true");
  const radios = group.children.filter((c) => c.attrs.role === "radio");
  check("one button per preset", radios.length, fns.CF2G_PRESETS.length);
  ok("each button is a radio", radios.every((b) => b.attrs.role === "radio"));
  check("the preset in hand is the checked one",
        radios.map((b) => [b.textContent, b.attrs["aria-checked"]]),
        [["Simplified", "false"], ["1 level", "false"], ["2 levels", "true"],
         ["3 levels", "false"], ["All", "false"]]);

  radios[3].click();
  check("pressing a preset switches to it", fns.cflow2GraphPreset("host-g"), "lv3");
  check("…remembered for this viewer", store["claunch.cflow2.graphpreset.host-g"], "lv3");
  check("…and asks for one redraw", draws, 1);

  /* a preset starts the chart over from itself: the folds a person laid on
     the last one are not carried onto this one (s2273-rev2) */
  fns.cf2GraphFolds["host-g"] = { d1: false, d3: true };
  radios[0].click();
  check("picking a preset drops the person's own folds",
        [fns.cflow2GraphPreset("host-g"), fns.cf2GraphFolds["host-g"]], ["simple", {}]);
  check("…and asks for the redraw that shows it", draws, 2);

  fns.cflow2GraphPresetSet("host-g", "all");
  check("the current-state button can set it without a press",
        [fns.cflow2GraphPreset("host-g"), store["claunch.cflow2.graphpreset.host-g"]],
        ["all", "all"]);
  fns.cf2GraphFolds["host-g"] = { d2: false };
  fns.cflow2GraphPresetPick("host-g", "simple");
  check("the way back to the run clears them the same way",
        [fns.cflow2GraphPreset("host-g"), fns.cf2GraphFolds["host-g"]], ["simple", {}]);

  ok("the chart section draws the sub group from the stored preset, in graph mode only",
     src.includes("if (chartMode === \"graph\") {") &&
     src.includes("sec.appendChild(cflow2GraphPresetTabs(host, cflow2GraphPreset(host), redraw));"));
  ok("the graph keeps its own folds, apart from the callstack's",
     src.includes("cf2GraphFolds[host], preset)") &&
     src.includes("cf2GraphFolds[host] || {}"));
  ok("a redraw is skipped unless the preset or the folds changed",
     src.includes("sel, preset, [...opened].sort()"));
  ok("Expand all and Collapse stay with the callstack, which has no presets",
     src.includes('if (chartMode !== "graph") tools.append(expand, fold);'));
  ok("the current-state button reaches the run's position by picking Simplified",
     src.includes('cflow2GraphPresetPick(host, "simple")'));
  ok("the CSS sets the sub group apart from the view tabs", css.includes(".cf2-modes.sub"));
  ok("the CSS draws the rule and the caption between the two questions",
     css.includes(".cf2-modes-sep") && css.includes(".cf2-modes-cap"));
  ok("the CSS gives the fold control a pressable look", css.includes(".cf2g-fold:hover"));
  ok("a level says how much detail it folded, and opens the next level",
     src.includes("blocks are folded") &&
     src.includes('"Show more detail"') &&
     src.includes("CF2G_PRESETS[CF2G_PRESETS.findIndex((p) => p[0] === preset) + 1]"));
  ok("the legend separates the two questions the row answers",
     src.includes("Simplified follows the run; a level draws the structure instead"));
}

/* ---- nothing to draw ------------------------------------------------- */
{
  const g = fns.cflow2GraphLayout(null, null, { open: new Set() });
  check("an absent chart draws nothing", [g.nodes.length, g.edges.length], [0, 0]);
  ok("and stays a drawable size", g.w >= 1 && g.h >= 1);
}

if (failures) {
  console.error(`${failures} failure(s)`);
  process.exit(1);
}
console.log("cflow2graph: ok");
