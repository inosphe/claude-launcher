/* The rail draws only the rows its state filter admits (claunch-7hlnj).

   It used to build a full card for every unarchived record and hide the ones
   the filter turned away with `session-filtered` (display:none). On a
   working machine that was 623 of 771 rows -- 47k of the page's 57k
   elements -- rebuilt, signed and decorated on every poll, while the user
   saw 148. What has to hold, run against the real refreshSessions and
   setSessionFilter from app.js:

   * a record the filter turns away has no row at all;
   * a child whose parent the filter turns away is drawn as a root, not
     indented under a row that is not there;
   * the filter is part of the rail's rebuild signature, so a filter change
     with the records unchanged redraws -- from the records in hand
     (`cached`), with no request -- and the rows it now admits appear;
   * setSessionFilter asks for that cached redraw before its poll, so a
     widened filter does not wait for the daemon to show its rows. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  const head = src.lastIndexOf("async ", start) === start - 6 ? start - 6 : start;
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error("unbalanced " + name);
}
const capLine = src.match(/^const RAIL_MESH_TAGS = .+$/m);
if (!capLine) throw new Error("cannot locate RAIL_MESH_TAGS in app.js");

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* ---- stub DOM (raillayout_check's) -------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), dataset: {}, style: {},
    title: "", type: "", parent: null,
    appendChild(c) { c.parent = this; this.kids.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.appendChild(c)); },
    addEventListener() {},
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
    querySelectorAll(sel) {
      const cls = sel.replace(/^\./, "");
      return walk(this).filter((k) => k.classes.has(cls));
    },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) {
      this.classes = new Set(String(v).split(/\s+/).filter(Boolean));
    },
    get innerHTML() { return ""; },
    set innerHTML(v) { this.kids = []; },
  };
  n.classList = {
    add: (...cs) => cs.forEach((c) => n.classes.add(c)),
    contains: (c) => n.classes.has(c),
  };
  return n;
}
function walk(n, out = []) {
  for (const k of n.kids) { out.push(k); walk(k, out); }
  return out;
}
const document = { createElement: (tag) => node(tag) };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}

const list = node("ul");
let served = { sessions: [] };
let requests = 0;
const api = async () => { requests++; return { ok: true, json: async () => served }; };

/* Everything refreshSessions leans on that is not the row itself, stubbed as
   raillayout_check stubs it. The filter itself is REAL (sliced below). */
const stubs = `
let sessionFilter = ["running", "hold", "delayed", "paused"];
let sessionsCache = [], currentName = null, currentPage = "home";
let attachedPid = null, linkState = "down", sessName = null, snapshotName = null;
let keptTerms = new Map();
function dropKept() {}
function railHeld() { return false; }
let railRedrawPending = false;
function forgetDeadSessions() {}
function refreshResumeChoices() {}
function renderHome() {}
function syncBulkActions() {}
function syncMobileBars() {}
function renderTermHandle() {}
function applyCflowBadges() {}
function applyGotoFlash() {}
function applyRailQuiet() {}
function applyBriefingCards() {}
function terminalOnScreen() { return false; }
function attach() {}
function setStatusBadge() {}
function $(id) { return list; }
function refreshParentChoices() {}
function ctxNoteOnRow() {}
function ctxRailLine(s) { return el("span", "rail-ctx-line unknown"); }
function railCwdLine(s) { return el("span", "rail-cwd"); }
function railSeenLine(s) { return el("span", "rail-seen"); }
function decorateNoteRow(li, s) {}
function decorateProgressRow(li, s) {}
function decorateProcsRow(li, s) {}
function decorateBriefingRow(li, s) {}
`;

const ctx = {};
new Function(
  "exports", "document", "el", "api", "list", "meshCache", "sessionGroupByMesh",
  stubs + capLine[0] + "\n"
  + slice("sessionCategory") + slice("sessionMatchesFilter")
  + slice("byLineage") + slice("sessionMeshGroup") + slice("sessMeshes") + slice("railMeshTags") + slice("sessHandles") + slice("handleTag")
  + slice("profileHarnessLabel") + slice("railMetaText")
  + slice("refreshSessions")
  + `
Object.assign(exports, {
  refresh: refreshSessions,
  setFilter: (f) => { sessionFilter = f; },
});`)(ctx, document, el, api, list, [], false);

// The row itself: its ⓘ and + carry the same data-name.
const drawn = () => walk(list).filter((r) => r.classes.has("sess-card"))
  .map((r) => [r.dataset.name, r.classes.has("child")]);

/* lead (live) spawned w1 (live) and w2 (killed); old (killed) spawned w3
   (live); keep (paused) stands alone. */
served = { sessions: [
  { name: "lead", status: "idle", profile: "nc", parent: null },
  { name: "w1", status: "busy", profile: "nc", parent: "lead" },
  { name: "w2", status: "exited", profile: "nc", parent: "lead" },
  { name: "old", status: "exited", profile: "nc", parent: null },
  { name: "w3", status: "idle", profile: "nc", parent: "old" },
  { name: "keep", status: "exited", paused_at: "2026-10-22T06:00:00Z",
    profile: "nc", parent: null },
] };

(async () => {
  await ctx.refresh();
  check("running + paused: the killed records have no row; the orphaned child is a root",
        drawn(),
        [["lead", false], ["w1", true], ["w3", false], ["keep", false]]);
  check("no row the filter turned away is left in the DOM to hide",
        walk(list).filter((r) => r.classes.has("session-filtered")).length, 0);

  // The records stay as they are; only the filter widens.
  const before = requests;
  ctx.setFilter(["running", "hold", "delayed", "failed", "killed", "paused"]);
  await ctx.refresh({ cached: true });
  check("a cached redraw asks the daemon for nothing", requests - before, 0);
  check("a widened filter redraws from the records in hand, lineage restored",
        drawn(),
        [["lead", false], ["w1", true], ["w2", true], ["old", false], ["w3", true],
         ["keep", false]]);

  ctx.setFilter(["killed"]);
  await ctx.refresh({ cached: true });
  check("narrowed again: only what the filter admits", drawn(),
        [["w2", false], ["old", false]]);

  /* setSessionFilter: the cached redraw goes before the poll. */
  const calls = [];
  const sctx = new Function(
    "calls",
    "const SESSION_FILTERS = ['current', 'running-paused', 'running', 'hold', 'delayed', 'failed', 'killed', 'paused', 'archived'];\n"
    + "const SESSION_FILTER_KEY = 'k';\n"
    + "const localStorage = { setItem() {} };\n"
    + "let sessionFilter = ['running'];\n"
    + "let sessionsCache = [];\n"
    + "function syncSessionFilters() { calls.push('sync'); }\n"
    + "function sessionFilterRefreshState() { return 'current'; }\n"
    + "function refreshSessions(o) { calls.push(o && o.cached ? 'cached' : 'poll:' + o.state); }\n"
    + slice("setSessionFilter")
    + "\nreturn { setSessionFilter };",
  )(calls);
  sctx.setSessionFilter("killed");
  check("setSessionFilter redraws from the cache, then polls", calls,
        ["sync", "cached", "poll:current"]);

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("railfilterdraw_check: ok");
})().catch((e) => { console.error(e); process.exit(1); });
