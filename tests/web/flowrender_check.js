/* Run the real renderFlowTopo against a stub DOM and inspect what it built.

   flowtrack_check covers the maths of one track; this covers the page those
   tracks live on — that every member gets a card inside its cluster, that the
   spawn edges and the cuts still get drawn (this view is a mesh diagram
   first), and above all that an agent blocked on a human turns up BOTH on the
   canvas and in the strip of buttons above it. That doubling is the point of
   the view; a regression that quietly dropped one half would look fine. */
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
  slice("function escXml(", RULE),                            // + wfDiagramSvg
  slice("const RING = {", "/* The send/add forms must survive the 2s poll"),
  slice("const FLOW = {", "/* boot  "),
].join("\n");

/* ---- stub DOM --------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, attrs: {}, kids: [], text: "", html: "", classes: new Set(), handlers: {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    appendChild(c) { this.kids.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.kids.push(c)); },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    closest() { return null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get innerHTML() { return this.html; },
    set innerHTML(v) { this.html = String(v); this.kids = []; },
  };
  n.classList = {
    add: (...cs) => cs.forEach((c) => n.classes.add(c)),
    toggle: (c, on) => (on ? n.classes.add(c) : n.classes.delete(c)),
    contains: (c) => n.classes.has(c),
  };
  return n;
}
const document = {
  createElementNS: (_ns, tag) => node(tag),
  createElement: (tag) => node(tag),
};
const window = { addEventListener() {} };
const location = { hash: "" };

function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}
const views = {};
const $ = (id) => (views[id] ||= node("div"));
const api = async () => ({ ok: true, json: async () => ({}) });
const confirm = () => true;
const alert = () => {};
const cflowAction = async () => {};
const refreshMeshView = () => {};
const refreshMeshList = () => {};
const refreshFlowView = async () => {};
const showView = () => {};
function meshDotClass(r) {
  if (r === "idle") return "idle";
  if (r === "busy" || r === "starting") return "busy";
  if (r === "remote-connected") return "starting";
  return "exited";
}

const ctx = {};
new Function(
  "exports", "document", "window", "location", "el", "$", "api", "confirm",
  "alert", "cflowAction", "refreshMeshView", "refreshMeshList",
  "refreshFlowView", "showView", "meshDotClass",
  code +
  "\nObject.assign(exports, {renderFlowTopo, pick: (v) => { flowPick = v; }});"
)(ctx, document, window, location, el, $, api, confirm, alert, cflowAction,
  refreshMeshView, refreshMeshList, refreshFlowView, showView, meshDotClass);

/* ---- walk helpers ------------------------------------------------------ */
function all(root, pred, out = []) {
  if (pred(root)) out.push(root);
  for (const k of root.kids) all(k, pred, out);
  return out;
}
const hasClass = (n, c) =>
  n.classes.has(c) || String(n.attrs.class || "").split(/\s+/).includes(c);
const withClass = (root, c) => all(root, (n) => hasClass(n, c));

let failures = 0;
function check(what, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${what}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

const WF = {
  name: "review", start: "plan",
  steps: [
    { id: "plan", next: "build" },
    { id: "build", gate: "ready?", next: "judge" },
    { id: "judge", select: { prompt: "good?", chooser: "user", options: [
      { name: "ship", next: "ship" }, { name: "again", next: "build" },
    ] } },
    { id: "ship" },
  ],
};

const member = (handle, parent, extra) => ({
  handle, parent: parent || null, machine: "", role: "worker",
  session: handle, reachability: "idle", ...extra,
});

const INFO = {
  name: "team", self: "work-pc", primary: null, authority: "work-pc",
  peers: [],
  members: [member("lead"), member("w1", "lead"), member("w2", "lead")],
  member_links: [{ a: "w1", b: "w2", enabled: false }],
  links: [],
};
const DATA = {
  flows: {
    lead: { session: "lead", cwd: "/p", scope: "lead", status: "step",
            workflow: "review", step_id: "plan", visits: { plan: 1 },
            key: "review@/p" },
    w1: { session: "w1", cwd: "/p", scope: "w1", status: "waiting_approval",
          workflow: "review", step_id: "build", gate: "ready?",
          visits: { plan: 1, build: 1 }, key: "review@/p" },
    w2: { session: "w2", cwd: "/p", scope: "w2", status: "idle" },
  },
  workflows: { "review@/p": WF },
};

/* --- the canvas -------------------------------------------------------- */
{
  ctx.pick(null);
  ctx.renderFlowTopo(INFO, DATA);
  const view = $("flow-view");
  const cards = withClass(view, "flow-card");
  check("one card per member", cards.length === 3, cards.length);
  check("every card is placed",
        cards.every((c) => /^translate\(/.test(c.attrs.transform || "")),
        cards.map((c) => c.attrs.transform));
  const canvas = all(view, (n) => n.tag === "svg")[0];
  check("the canvas is fitted to what it holds",
        !!canvas && /^-?[\d.]+ -?[\d.]+ [\d.]+ [\d.]+$/.test(canvas.attrs.viewBox),
        canvas && canvas.attrs.viewBox);
  check("the cluster is drawn behind the cards",
        withClass(view, "mesh-cluster").length === 1);
  check("the spawn tree is still drawn",
        withClass(view, "mesh-spawn").length === 2,
        withClass(view, "mesh-spawn").length);
  check("a cut between two members is still drawn",
        withClass(view, "mesh-mcut").length === 1);

  // The track: one pip per step plus the end, for each member that has a run.
  const pips = withClass(cards.find((c) => hasClass(c, "running")) || cards[0],
                         "flow-pip");
  check("a running agent's whole machine is on its card", pips.length === 5,
        pips.length);
  const idle = cards.find((c) => hasClass(c, "none"));
  check("the agent with no run gets no track",
        !!idle && withClass(idle, "flow-pip").length === 0);
}

/* --- the thing the view exists for ------------------------------------- */
{
  ctx.pick(null);
  ctx.renderFlowTopo(INFO, DATA);
  const view = $("flow-view");
  const blocked = withClass(view, "flow-card").filter((c) => hasClass(c, "blocked"));
  check("the blocked agent is loud on the canvas", blocked.length === 1,
        blocked.length);
  const rows = withClass(view, "flow-waiting-row");
  check("...and repeated above it with the button that clears it",
        rows.length === 1 && all(rows[0], (n) => n.tag === "button").length === 1,
        rows.length);
  check("the gate's own words are what the row shows",
        all(rows[0], (n) => n.text === "ready?").length === 1);
}

/* A mesh where nothing is blocked still shows the strip — an empty one is an
   answer ("nothing needs you"), an absent one is a question. */
{
  ctx.pick(null);
  const calm = { ...DATA, flows: { ...DATA.flows, w1: { ...DATA.flows.w1,
    status: "step", gate: undefined } } };
  ctx.renderFlowTopo(INFO, calm);
  const view = $("flow-view");
  check("the strip is there with nothing in it",
        withClass(view, "flow-waiting").length === 1 &&
        withClass(view, "flow-waiting-row").length === 0);
}

/* --- the card as an index into the full machine ------------------------ */
{
  ctx.pick("w1");
  ctx.renderFlowTopo(INFO, DATA);
  const view = $("flow-view");
  const detail = withClass(view, "flow-detail")[0];
  check("picking a card opens its detail", !!detail);
  const dia = detail && withClass(detail, "wf-diagram")[0];
  check("the detail holds the run page's own diagram, not a second drawing",
        !!dia && dia.html.includes('data-step="judge"'), dia && dia.html.slice(0, 60));
  check("the picked card is marked on the canvas",
        withClass(view, "flow-card").filter((c) => hasClass(c, "picked")).length === 1);
}

/* A pick that outlives the member it named must not leave a panel claiming to
   show somebody who has left. */
{
  ctx.pick("ghost");
  ctx.renderFlowTopo(INFO, DATA);
  check("a pick pointing at nobody is dropped",
        withClass($("flow-view"), "flow-detail").length === 0);
}

/* An empty mesh draws nothing and says so, rather than dividing by zero in
   the ring maths. */
{
  ctx.pick(null);
  ctx.renderFlowTopo({ ...INFO, members: [], member_links: [] },
                     { flows: {}, workflows: {} });
  const view = $("flow-view");
  check("an empty mesh is a sentence, not a canvas",
        all(view, (n) => n.tag === "svg").length === 0);
}

if (failures) {
  console.log(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("flowrender_check: ok");
