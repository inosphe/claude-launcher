/* One directory holds one cflow run per session, so the run page's head has
   to name the session as well as the workflow: without it, twenty sessions
   driving 'improv-worker' in the same tree draw twenty identical heads. The
   owning session used to sit down in .wf-meta, which lives in the column
   that scrolls — the head and the button bar are the only strip that holds
   still (style.css: `#term-wf .wf-head { position: sticky }`), so an
   identity kept below them is gone the moment the reader moves.

   This slices the real head-building code out of app.js and checks four
   things: the chip is in the head, it is the link to that session, it is NOT
   left behind in the meta line as a duplicate, and 'default' — the unscoped
   slot, which belongs to no session — gets no chip at all. The phone's top
   bar names the run the same way, so it is checked from the same source. */
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
  const head = src.lastIndexOf("async ", start) === start - 6 ? start - 6 : start;
  // from the brace that opens the BODY — a default in the parameter list
  // (`opts = {}`) would otherwise close the function on the spot
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, attrs: {}, kids: [], text: "", classes: new Set(), handlers: {},
    dataset: {}, disabled: false, href: "", title: "",
    setAttribute(k, v) { this.attrs[k] = String(v); },
    appendChild(c) { this.kids.push(c); return c; },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    querySelector() { return null; },
    querySelectorAll() { return []; },
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
const has = (n, cls) => walk(n).some((k) => k.classes.has(cls));
const find = (n, cls) => walk(n).filter((k) => k.classes.has(cls));

const document = {
  createElement: (tag) => node(tag),
  createElementNS: (_ns, tag) => node(tag),
};
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}

/* Everything renderWfInto leans on that this file is not testing: the
   diagram, the reports, the buttons and the boxes below them. Stubbed to
   their shapes so the head is what is left to check. */
const stubs = `
let wfSelectedStep = null, wfCwd = null, wfScope = "default";
let meshName = "", flowMesh = "", transcriptName = "", traceSession = "";
let sessName = "", currentName = "", currentPage = "home";
function wfDiagramSvg() { return ""; }
function wfPacedNote() { return null; }
// The timing diagram under the graph (s157) — another of the pictures this
// file is not testing, stubbed to the same shape as its neighbour above.
function wfTimelinePanel() { return null; }
function wfReports() { return el("div", "wf-reports"); }
function wfActions() { return el("div", "wf-actions"); }
function reminderControl() { return el("div", "wf-reminder"); }
function pendingBanner() { return null; }
function renderWfIdle(view) { view.appendChild(el("div", "wf-start")); }
function cflowAction() {}
function confirm() { return true; }
// The diagram/reports drag split (s469) — another thing this file is not
// testing, stubbed to its shape so renderWfInto's references resolve.
let wfColDragHost = null;
const MOBILE_MQ = { matches: false };
function wfSplitBar() { return el("div", "wf-split"); }
function loadWfDiaW() { return null; }
function setWfColW() {}
`;

const ctx = {};
new Function(
  "exports", "document", "el",
  stubs + "\n" +
  [slice("answerFellToUs"), slice("wfDotClass"), slice("shortenPath"),
   slice("captureWfScrolls"), slice("restoreWfScrolls"),
   slice("wfOwnerChip"), slice("renderWfInto"), slice("mobileTitle")].join("\n") +
  `
Object.assign(exports, {
  renderWfInto, wfOwnerChip, mobileTitle,
  setPage: (p, cwd, scope) => { currentPage = p; wfCwd = cwd; wfScope = scope; },
});`
)(ctx, document, el);

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) return;
  failures++;
  console.log(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
};

const UI = {
  host: "page",
  getStep: () => null,
  putStep: () => {},
  select() {},
  refresh: () => {},
  stillHere: () => true,
  fullLink: false,
  actions: {},
};

const WF = {
  name: "improv-worker",
  steps: [{ id: "intake", next: "work" }, { id: "work" }],
};
const RUN = {
  run: "run-6407f733", status: "step", step_id: "work",
  started_at: "2026-08-26T08:29:27", steps_completed: 1,
};
const data = (over) => ({
  cwd: "F:\\works\\claude-launcher\\.claude\\worktrees\\s127-s152",
  scope: "s152", sessions: ["s152"], run: RUN, workflow: WF,
  reports: [], journal: [], ...over,
});

/* ---- the head names the session, not only the workflow ---------------- */
{
  const view = node("div");
  ctx.renderWfInto(view, data(), UI);
  const head = find(view, "wf-head")[0];
  check("the run page has a head", !!head);
  const kids = head ? head.kids : [];
  check("the workflow's name is still the head's h2",
        kids[0] && kids[0].tag === "h2" && kids[0].text === "improv-worker",
        kids[0] && kids[0].text);
  const chip = kids.find((k) => k.classes.has("cflow-scope"));
  check("the owning session is IN the head", !!chip);
  check("...naming that session", chip && chip.text === "session s152",
        chip && chip.text);
  check("...beside the name, ahead of the run's state badge",
        !!chip && kids.indexOf(chip) === 1, kids.map((k) => k.tag));
  check("...as the link to it", chip && chip.tag === "a" &&
        chip.href === "#/s/s152", chip && chip.href);
  check("the state badge is still there",
        kids.some((k) => k.classes.has("badge") && k.classes.has("wf-running")),
        kids.map((k) => [...k.classes].join(".")));
  /* The point of the move: one chip, in the strip that holds still. Left in
     .wf-meta as well it would be a second, contradictory-looking link the
     moment a poll rebuilt one and not the other. */
  const meta = find(view, "wf-meta")[0];
  check("the meta line kept the rest of the run's facts",
        meta && meta.kids.some((k) => k.text.startsWith("run run-6407f733")),
        meta && meta.kids.map((k) => k.text));
  check("...and does not repeat the session chip",
        meta && !meta.kids.some((k) => k.classes.has("cflow-scope")));
  check("so the page carries exactly one", find(view, "cflow-scope").length === 1,
        find(view, "cflow-scope").length);
}

/* ---- a session that is not running is still the run's owner ----------- */
{
  const view = node("div");
  ctx.renderWfInto(view, data({ sessions: [] }), UI);
  const chip = find(view, "cflow-scope")[0];
  check("an exited session still names its run", !!chip &&
        chip.text === "session s152");
  check("...and the chip says what opening it does", !!chip &&
        /not running/.test(chip.title), chip && chip.title);
}
{
  const view = node("div");
  ctx.renderWfInto(view, data(), UI);
  const chip = find(view, "cflow-scope")[0];
  check("a live session's chip attaches instead", !!chip &&
        chip.title === "attach this run's session", chip && chip.title);
}

/* ---- the unscoped slot belongs to no session, so it gets no chip ------ */
{
  const view = node("div");
  ctx.renderWfInto(view, data({ scope: "default", sessions: [] }), UI);
  check("the default slot gets no session chip",
        find(view, "cflow-scope").length === 0);
  check("...but still has its head", has(view, "wf-head"));
  check("wfOwnerChip agrees on its own",
        ctx.wfOwnerChip({ scope: "default" }) === null &&
        ctx.wfOwnerChip({}) === null);
}

/* ---- the phone's top bar names the same run the same way -------------- */
{
  ctx.setPage("wf", "F:\\works\\claude-launcher\\.claude\\worktrees\\s127-s152",
              "s152");
  const title = ctx.mobileTitle();
  // Spelled out rather than a substring test: the worktree path below holds
  // "s152" too, so `includes` would pass on the old title that never named
  // the session at all.
  check("the mobile bar names the session ahead of the path",
        title.startsWith("workflow · s152 · "), title);
  ctx.setPage("wf", "F:\\works\\claude-launcher", "default");
  check("...and says nothing extra for the unscoped slot",
        !/s152/.test(ctx.mobileTitle()) &&
        ctx.mobileTitle().startsWith("workflow · "), ctx.mobileTitle());
}

if (failures) { console.log(`${failures} failure(s)`); process.exit(1); }
console.log("wfhead_check: ok");
