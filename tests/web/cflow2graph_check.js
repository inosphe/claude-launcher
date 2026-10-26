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

const CONSTS = ["CF2G_NODE_W", "CF2G_NODE_H", "CF2G_GAP", "CF2G_LANE", "CF2G_PAD",
                "CF2G_HEAD", "CF2G_CHIP", "CF2G_RAIL", "CF2G_EDGE", "CF2G_MARGIN",
                "CF2G_GUTTER", "CF2G_ATOMIC", "CF2G_BRANCHES", "cf2ChartModes"];

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
  [slice("cflow2GraphClip"), slice("cflow2GraphLoopText"), slice("cflow2GraphChip"),
   slice("cflow2GraphSig"), slice("cflow2GraphSite"), slice("cflow2GraphProcs"), slice("cflow2GraphOpen"),
   slice("cflow2GraphLayout"), slice("cflow2GraphSvg"), slice("cflow2NodeBits"),
   slice("escXml"), slice("el"), slice("cflow2ChartMode"), slice("cflow2ChartModeTabs")].join("\n") +
  "\nreturn { cflow2GraphLayout, cflow2GraphProcs, cflow2GraphOpen, cflow2GraphSvg," +
  " cflow2GraphSig, cflow2GraphSite, cflow2NodeBits, cflow2ChartMode, cflow2ChartModeTabs," +
  " CF2G_ATOMIC, CF2G_BRANCHES, CF2G_PAD };"
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
