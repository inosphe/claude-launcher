/* Run the real renderTrace against a stub DOM and inspect what it drew.

   Complements seq_check.js: that one checks the event list, this one checks
   that the drawing code turns it into the picture the page claims. The claims
   worth holding it to are the ones a reader acts on — an arrow that has not
   landed must not look like one that has, a conversation between other people
   must not look like this session's, and an unanswered question must be
   findable without following every arrow to its end. */
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
  slice("const TRACE_POLL_MS", "function stopMsgPoll"),
  slice("function traceMs(", "/* ---- the page"),
  slice("function traceHead(", "/* boot  "),
].join("\n");

/* ---- stub DOM --------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, attrs: {}, kids: [], text: "", html: "", classes: new Set(), handlers: {},
    scrollTop: 0, scrollLeft: 0, scrollHeight: 0, clientHeight: 0,
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
const document = { createElementNS: (_ns, t) => node(t), createElement: (t) => node(t) };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}
const view = node("div");
const $ = () => view;
const go = () => {};
const fmtAge = (s) => `${Math.floor(s / 60)}m`;
const renderMeshOwed = () => el("div", "mesh-owed", "unanswered box");
const location = { hash: "" };
const traceSession = "coder3";
let traceLast = null;
const traceOpen = new Set();

const ctx = {};
new Function(
  "exports", "document", "$", "el", "svg", "go", "fmtAge", "renderMeshOwed",
  "location", "traceSession", "traceLast", "traceOpen",
  // svg() itself is the shared helper the rest of the app draws with; it is
  // three lines and taking it from source keeps this honest.
  slice("function svg(tag, attrs, text)", "/* Cut state is per EDGE") + "\n" +
  code +
  "\nObject.assign(exports, {renderTrace, seqRow, msgEvents, msgLanes, seqW});"
)(ctx, document, $, el, undefined, go, fmtAge, renderMeshOwed, location,
  traceSession, traceLast, traceOpen);
const { renderTrace, seqRow, msgEvents, msgLanes, seqW } = ctx;

/* ---- walk helpers ----------------------------------------------------- */
function all(root, pred, out = []) {
  if (pred(root)) out.push(root);
  for (const k of root.kids) all(k, pred, out);
  return out;
}
const hasClass = (n, c) =>
  n.classes.has(c) || String(n.attrs.class || "").split(/\s+/).includes(c);
const withClass = (root, c) => all(root, (n) => hasClass(n, c));
const byTag = (root, t) => all(root, (n) => n.tag === t);
const texts = (root) => all(root, () => true).map((n) => n.text).filter(Boolean);

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

/* ---- an afternoon in a three-agent mesh -------------------------------- */
const T = (m) => `2026-08-12T10:${String(m).padStart(2, "0")}:00Z`;
const DATA = {
  meta: { session: { name: "coder3", cwd: "F:\\works\\ShelterZero" } },
  meshes: [
    { mesh: "coder3", handle: "coder3", role: "worker", members: 3 },
    { mesh: "ops", handle: "hand", role: "worker", members: 2 },
  ],
  mesh: { mesh: "coder3", handle: "coder3", role: "worker", members: 3 },
  info: {
    name: "coder3", primary: null,
    members: [
      { handle: "lead", role: "leader", joined_at: T(0) },
      { handle: "coder3", role: "worker", joined_at: T(1), parent: "lead" },
      { handle: "reviewer", role: "reviewer", joined_at: T(2) },
    ],
  },
  history: [
    { id: "a", ts: T(3), from: "operator", to: "lead", type: "ask", epoch: 0,
      seq: 0, body: "ship the ECS change", recipients: ["lead"],
      delivered: ["lead"], remote: [] },
    // between the others: context, not this session's traffic
    { id: "b", ts: T(4), from: "lead", to: "reviewer", type: "fyi", epoch: 0,
      seq: 1, body: "heads up", recipients: ["reviewer"],
      delivered: ["reviewer"], remote: [] },
    { id: "c", ts: T(5), from: "lead", to: "coder3", type: "ask", epoch: 0,
      seq: 2, recipients: ["coder3"], delivered: ["coder3"], remote: [],
      body: "take the ECS change through review and tell me what the task "
        + "definition ends up looking like, including "
        + "F:\\works\\ShelterZero\\infra\\environments\\production\\services\\"
        + "gateway\\ecs\\task\\service-definition-production-canary.tf" },
    // sent, not yet typed in: the arrow must say so
    { id: "d", ts: T(20), from: "coder3", to: "reviewer", type: "ask", epoch: 0,
      seq: 3, body: "review please", recipients: ["reviewer"],
      delivered: [], remote: [] },
  ],
  owed: { owed: 1, members: [
    { handle: "reviewer", owed: 1, messages: [{ id: "d", age: 240 }] },
  ] },
  flow: { journal: [
    { at: T(6), event: "step_completed", step: "implement" },
    { at: T(7), event: "lock_taken" },
  ] },
};

renderTrace(DATA);

/* --- the page's furniture ---------------------------------------------- */
{
  check("both memberships are offered as tabs",
        withClass(view, "seq-tab").length === 2, withClass(view, "seq-tab").length);
  const on = withClass(view, "seq-tab").filter((n) => hasClass(n, "on"));
  check("the mesh on screen is the one marked", on.length === 1 &&
        on[0].text.startsWith("coder3"), on.map((n) => n.text));
  check("the unanswered ledger is on the page, where its buttons are",
        withClass(view, "mesh-owed").length === 1);
  // ...and only while there is something in it: the picture already says
  // "nothing is owed" by carrying no chips, and this page is the picture.
  renderTrace({ ...DATA, owed: { owed: 0, members: [{ handle: "lead", owed: 0 }] } });
  check("a mesh with nothing owed spends no room saying so",
        withClass(view, "mesh-owed").length === 0);
  renderTrace(DATA);
  check("the lane heads are pinned in their own block",
        withClass(view, "seq-head").length === 1);
}

/* --- the lanes ---------------------------------------------------------- */
{
  const names = withClass(view, "seq-lane-name").map((n) => n.text);
  check("a lane per party, the outsider first",
        names.join() === "operator,lead,coder3,reviewer", names);
  const self = withClass(view, "seq-lane-head").filter((n) => hasClass(n, "self"));
  check("only the session the page is about is marked as itself",
        self.length === 1, self.length);
  const rows = withClass(view, "seq-row-svg");
  check("every row carries the full set of lane lines",
        rows.every((r) => withClass(r, "seq-lane").length === 4),
        rows.map((r) => withClass(r, "seq-lane").length));
}

/* --- the arrows --------------------------------------------------------- */
{
  const msgs = withClass(view, "seq-msg");
  check("one group per message", msgs.length === 4, msgs.length);
  // operator→lead and lead→reviewer both happened beside this session, not to
  // it. Faded is exactly right for them: they are why the next message
  // arrived, and reading them as this session's own traffic is the mistake.
  const faint = msgs.filter((n) => hasClass(n, "faint"));
  check("what happened between other parties is kept, and faded",
        faint.length === 2, faint.map((n) => texts(n)));
  const mine = msgs.filter((n) => !hasClass(n, "faint"));
  check("...and what this session sent or was sent is not",
        mine.length === 2, mine.map((n) => texts(n)));
  const waiting = withClass(view, "seq-arrow").filter((n) => hasClass(n, "waiting"));
  check("the undelivered ask is the one drawn as still in flight",
        waiting.length === 1, waiting.length);
  const open = withClass(view, "seq-arrowhead").filter((n) => hasClass(n, "open"));
  check("...and it is the one with a hollow head", open.length === 1);
  const out = withClass(view, "seq-drop").filter((n) => hasClass(n, "out"));
  check("its recipient's mark says 'queued', not 'typed in'", out.length === 1);
  check("delivered recipients are marked as arrived",
        withClass(view, "seq-drop").length === 4 && out.length === 1);
}

/* --- what a reader is looking for --------------------------------------- */
{
  const owed = withClass(view, "seq-owed");
  check("the unanswered ask is chipped in the margin", owed.length === 1, owed.length);
  check("...with how long it has been waiting",
        texts(owed[0]).some((t) => t.includes("4m") && t.includes("unanswered")),
        texts(owed[0]));
  const gap = withClass(view, "seq-gap");
  check("the quarter-hour of silence is folded into one marker",
        gap.length === 1 && texts(gap[0]).some((t) => t.includes("quiet")),
        gap.map((g) => texts(g)));
  const flow = withClass(view, "seq-flow-label");
  check("the run's step lands on this session's lane, bookkeeping does not",
        flow.length === 1 && flow[0].text === "done: implement",
        flow.map((n) => n.text));
  check("every member's arrival opens its lane",
        withClass(view, "seq-join").length === 3);
}

/* --- a long body is offered, not dumped --------------------------------- */
{
  const labels = withClass(view, "seq-label").map((n) => n.text);
  check("a long message is clipped to the width it has",
        labels.some((t) => t.endsWith("…")), labels);
  check("nothing is expanded until asked",
        withClass(view, "seq-body").length === 0);
  const openable = withClass(view, "seq-msg").filter((n) => hasClass(n, "openable"));
  check("only the message too long to read on its line invites a click",
        openable.length === 1, openable.length);
  // What the click does: the row folds open with the body wrapped into it.
  traceOpen.add("c");
  renderTrace(DATA);
  const lines = withClass(view, "seq-body");
  check("the opened message shows its whole body, wrapped",
        lines.length > 1 && lines.map((n) => n.text).join(" ").includes("review"),
        lines.map((n) => n.text));
  // A path is one word with nowhere to break, and is what gets pasted into
  // these messages. It must be cut to the row, not run off the side of it.
  const longest = Math.max(...lines.map((n) => n.text.length));
  check("a word too long to wrap is cut to the row it is drawn in",
        longest <= 100, { longest, lines: lines.map((n) => n.text) });
  check("...and none of it is lost on the way",
        lines.map((n) => n.text).join("").includes("service-definition-production"),
        lines.map((n) => n.text));
  traceOpen.clear();
}

/* --- degraded, and honest about it -------------------------------------- */
{
  renderTrace({ ...DATA, history: [
    // a daemon too old to say who a '*' reached
    { id: "u", ts: T(3), from: "lead", to: "*", epoch: 0, seq: 0, body: "hi" },
  ], owed: null, flow: null });
  const nowhere = withClass(view, "seq-nowhere");
  check("an unresolved broadcast says the daemon did not report, not 'nobody'",
        nowhere.length === 1 && nowhere[0].text.includes("not reported"),
        nowhere.map((n) => n.text));

  renderTrace({ ...DATA, history: [
    // resolved, and the answer was nobody: an edge cut since it was sent
    { id: "x", ts: T(3), from: "lead", to: "*", epoch: 0, seq: 0, body: "hi",
      recipients: [], delivered: [], remote: [] },
  ], owed: null, flow: null });
  const cut = withClass(view, "seq-nowhere");
  check("a message that now reaches nobody says exactly that",
        cut.length === 1 && cut[0].text.includes("reaches nobody"),
        cut.map((n) => n.text));
}

/* --- a session in no mesh has nothing to draw --------------------------- */
{
  renderTrace({ meta: DATA.meta, meshes: [], mesh: null });
  check("no mesh, no diagram — and a reason instead",
        withClass(view, "seq-row-svg").length === 0 &&
        withClass(view, "wf-note").length === 1,
        texts(view));
}

/* --- geometry: the picture is as wide as the room ----------------------- */
{
  check("width grows with the lanes", seqW(4) > seqW(2) && seqW(0) > 0,
        [seqW(0), seqW(2), seqW(4)]);
}

if (failures) {
  console.log(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("seqrender_check: ok");
