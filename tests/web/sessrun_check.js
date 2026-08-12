/* The session panel can open the run itself, instead of sending you to the
   run page and taking the terminal away. That fold has to hold three lines at
   once — it must cost nothing while it is shut, it must not outlive the run it
   reads, and what it shows has to be the run page's material and not a
   lookalike. Slice the real functions out of app.js, drive them against a stub
   DOM, and check all three. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  // keep the `async` if there is one: the slice has to stay compilable
  const head = src.lastIndexOf("async ", start) === start - 6 ? start - 6 : start;
  // From the brace that opens the BODY — `opts = {}` in a parameter list is a
  // pair of braces that would otherwise close the function on the spot.
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error("unbalanced " + name);
}
/* The caps come from the source rather than being restated here, and the
   fixtures are built from them: a raised cap must not quietly turn the
   truncation checks below into no-ops. */
function sliceLine(decl) {
  const start = src.indexOf(decl);
  if (start < 0) throw new Error("missing " + decl);
  return src.slice(start, src.indexOf("\n", start) + 1);
}
const CAPS = {};
new Function("exports", sliceLine("const SESS_RUN_REPORTS") +
             sliceLine("const SESS_RUN_JOURNAL") +
             "Object.assign(exports, { SESS_RUN_REPORTS, SESS_RUN_JOURNAL });")(CAPS);

function sliceConst(decl) {
  const start = src.indexOf(decl);
  if (start < 0) throw new Error("missing " + decl);
  const end = src.indexOf("\n};", start);
  return src.slice(start, end + 3);
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, attrs: {}, kids: [], text: "", classes: new Set(), handlers: {},
    dataset: {}, open: false,
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    appendChild(c) { this.kids.push(c); return c; },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    fire(k) { (this.handlers[k] || []).forEach((fn) => fn()); },
    querySelector(sel) { return walk(this).find((k) => k.classes.has(sel.slice(1))) || null; },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get innerHTML() { return ""; },
    set innerHTML(v) { if (!v) this.kids = []; },
  };
  n.classList = {
    add: (...cs) => cs.forEach((c) => n.classes.add(c)),
    toggle: (c, on) => (on ? n.classes.add(c) : n.classes.delete(c)),
    contains: (c) => n.classes.has(c),
  };
  return n;
}
function walk(n, out = []) {
  for (const k of n.kids) { out.push(k); walk(k, out); }
  return out;
}
// An SVG node gets its class through setAttribute, an HTML one through el();
// both are "what class is this" to the checks below.
const classOf = (n) => new Set([
  ...n.classes,
  ...String(n.attrs.class || "").split(/\s+/).filter(Boolean),
]);
const has = (n, cls) => walk(n).some((k) => classOf(k).has(cls));
const texts = (n) => walk(n).map((k) => k.text).join(" | ");

const document = {
  createElementNS: (_ns, tag) => node(tag),
  createElement: (tag) => node(tag),
};
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}

let fetched = [];
const api = async (path) => {
  fetched.push(path);
  return { ok: true, json: async () => apiDoc };
};
let apiDoc = {};
let timers = 0;
const setInterval = () => { timers++; return timers; };
const clearInterval = () => { timers--; };

/* Everything the sliced code leans on and this file is not testing. */
const stubs = `
let sessRunFold = null, sessRunTimer = null;
let posted = [], nudged = [];
function cflowAction(path, body, after) { posted.push({ path, body, after }); }
function nudgeRun(cwd, scope) { nudged.push({ cwd, scope }); return Promise.resolve(); }
function confirm() { return true; }
function alert() {}
`;

const ctx = {};
new Function(
  "exports", "document", "el", "api", "setInterval", "clearInterval",
  stubs +
  sliceConst("const FLOW = {") + "\n" +
  sliceLine("const SESS_RUN_REPORTS") + sliceLine("const SESS_RUN_JOURNAL") +
  [slice("flowOrder"), slice("flowTrack"), slice("flowNeedsHuman"),
   slice("flowState"), slice("flowMetrics"), slice("flowPipShape"),
   slice("flowTrackSvg"), slice("svg"), slice("wfActions"),
   slice("sessRunFoldFor"), slice("stopSessRun"), slice("refreshSessRun"),
   slice("renderSessRun"), slice("sessRunTrack")].join("\n") +
  `
Object.assign(exports, {
  sessRunFoldFor, refreshSessRun, renderSessRun, sessRunTrack, wfActions,
  fold: () => sessRunFold, timer: () => sessRunTimer,
  drop: () => { sessRunFold = null; },
  posted: () => posted,
});`
)(ctx, document, el, api, setInterval, clearInterval);

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) return;
  failures++;
  console.log(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
};

/* ---- a run to read ---- */
const WF = {
  name: "ecs-change", start: "survey",
  steps: [
    { id: "survey", next: "plan" },
    { id: "plan", gate: "human sign-off", next: "build" },
    { id: "build", verify: "pytest", next: "check" },
    {
      id: "check",
      select: { chooser: "user", prompt: "ship it?", options: [
        { name: "ship", next: null }, { name: "again", next: "build" },
      ] },
    },
  ],
};
const RUN = {
  run: "r-7", status: "waiting_approval", step_id: "plan",
  gate: "human sign-off", started_at: "2026-08-12T09:00:00",
  steps_completed: 3, visits: { survey: 1, plan: 2, build: 1 },
};
const NREP = CAPS.SESS_RUN_REPORTS + 4;   // enough to be cut
const NJNL = CAPS.SESS_RUN_JOURNAL + 20;
const reports = Array.from({ length: NREP }, (_, i) => ({
  step: "build", visit: i + 1, at: `2026-08-12T10:${String(i).padStart(2, "0")}:00`,
  summary: `pass ${i}`, details: i % 2 ? "stack\ntrace" : "",
}));
const journal = Array.from({ length: NJNL }, (_, i) => ({
  at: "2026-08-12T10:00:00", event: "step", step: `s${i}`,
}));
const DATA = {
  cwd: "F:\\works\\ShelterZero", scope: "coder3", sessions: ["coder3"],
  run: RUN, workflow: WF, reports, journal,
};

/* ---- shut, it costs nothing ---- */
const flow = { cwd: DATA.cwd, scope: "coder3", status: "waiting_approval" };
const fold = ctx.sessRunFoldFor(flow);
check("the fold starts shut", fold.open === false);
check("and asks the daemon for nothing until it is opened", fetched.length === 0, fetched);
check("it names the slot it belongs to", fold.dataset.slot === "coder3|" + DATA.cwd,
      fold.dataset.slot);
check("a rebuild of the panel re-uses the very same node",
      ctx.sessRunFoldFor(flow) === fold);
check("another session's run does not", ctx.sessRunFoldFor(
  { cwd: DATA.cwd, scope: "s7", status: "running" }) !== fold);

/* ---- open: it fetches, and it polls only while open ---- */
ctx.drop();
const f2 = ctx.sessRunFoldFor(flow);
apiDoc = DATA;
f2.open = true;
f2.fire("toggle");
check("opening asks for this slot's run",
      fetched.length === 1 && fetched[0].startsWith("/api/cflow/run?"), fetched);
check("...keyed by the pair a run is keyed by",
      fetched[0].includes(`cwd=${encodeURIComponent(DATA.cwd)}`) &&
      fetched[0].includes("scope=coder3"), fetched[0]);
check("and starts a poll", timers === 1, timers);
f2.open = false;
f2.fire("toggle");
check("closing stops it", timers === 0, timers);

/* ---- what it shows is the run page's material ---- */
const body = node("div");
ctx.renderSessRun(body, DATA);
check("the state machine is drawn", has(body, "sess-run-track"));
check("...and coloured by what the run is doing", walk(body).some((k) => {
  const c = classOf(k);
  return c.has("sess-run-track") && c.has("blocked");
}));
check("it says which step it is on", texts(body).includes("here: plan"), texts(body));
check("the gate is quoted", texts(body).includes("human sign-off"));
check("with the button that clears it",
      walk(body).some((k) => k.classes.has("approve")));
check("the run is identified", texts(body).includes("run r-7"));

/* The one control the rail must NOT offer: aborting a run is a decision to
   make on the page that shows the whole of it. */
check("no archive button in the rail", !has(body, "archive"));
const page = ctx.wfActions(DATA);
check("...but the run page still has one", has(page, "archive"));

/* ---- long runs are cut, and say so ---- */
const shown = walk(body).filter((k) => k.classes.has("sess-run-report"));
check("reports are capped", shown.length === CAPS.SESS_RUN_REPORTS, shown.length);
check("and the cut is stated, not silent",
      texts(body).includes(`newest ${CAPS.SESS_RUN_REPORTS} of ${NREP}`), texts(body));
check("the newest report leads", texts(body).includes(`pass ${NREP - 1}`));
const jnl = walk(body).filter((k) => k.classes.has("wf-journal-line"));
check("the journal is capped too", jnl.length === CAPS.SESS_RUN_JOURNAL, jnl.length);

/* ---- the run ends ---- */
const gone = node("div");
ctx.renderSessRun(gone, { status: "idle", cwd: DATA.cwd, scope: "coder3" });
check("an emptied slot says so rather than drawing an empty track",
      texts(gone).includes("no run any more") && !has(gone, "sess-run-track"),
      texts(gone));

console.log(failures ? `\n${failures} failure(s)` : "all sess-run checks passed");
process.exit(failures ? 1 : 0);
