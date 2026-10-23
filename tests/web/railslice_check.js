/* The rail is drawn in slices, not in one task (claunch-rw7tw).

   The operator's trace (2026-10-23) shows the renderer main thread held for
   8,251 ms in a single task while the rail was drawn, 100% CPU from 6.9 s to
   15.2 s, nothing painted and no input handled in between. The work is the
   same either way -- what the slice buys is that the browser can paint and
   answer between parts of it.

   Held here, against the real `refreshSessions` and a stub DOM:

   * a pass over a fleet larger than the slice threshold is handed back to the
     event loop partway through: when the promise it returns settles the WHOLE
     fleet is drawn, but it was not drawn in one turn;
   * a pass that is superseded while suspended stops writing -- what is left
     in the list is the newer pass's fleet, with none of the older one's rows
     still in it;
   * a rail small enough to be one short task is still drawn in the turn that
     asks for it, which is the contract the other node harnesses were written
     against.

   The budget is forced to zero (`refreshSessions._sliceMs = 0`) so the slice
   boundary does not depend on how fast the machine running this is. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const src = fs.readFileSync(
  process.env.RAILSLICE_APP_JS || path.join(STATIC, "app.js"), "utf8");

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

/* ---- stub DOM, the same one railfilterdraw_check builds ---- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), dataset: {}, style: {},
    title: "", type: "", parentNode: null,
    appendChild(c) { n.kids.push(c); c.parentNode = n; return c; },
    append(...cs) { cs.forEach((c) => n.appendChild(c)); },
    insertBefore(c, at) {
      const i = at && n.kids.indexOf(at);
      if (i === undefined || i < 0) n.kids.push(c); else n.kids.splice(i, 0, c);
      c.parentNode = n;
      return c;
    },
    replaceChild(c, old) {
      const i = n.kids.indexOf(old);
      if (i >= 0) n.kids[i] = c; else n.kids.push(c);
      c.parentNode = n;
      old.parentNode = null;
      return old;
    },
    removeChild(c) {
      const i = n.kids.indexOf(c);
      if (i >= 0) n.kids.splice(i, 1);
      c.parentNode = null;
      return c;
    },
    addEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    get firstElementChild() { return n.kids.find((k) => k.tag) || null; },
    get childNodes() { return n.kids; },
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
const api = async () => ({ ok: true, json: async () => served });

/* Everything refreshSessions leans on that is not the row itself, stubbed as
   railfilterdraw_check stubs it. Line/decoration builders are inert: this
   check is about when the pass runs, not what it paints. */
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
  + slice("byLineage") + slice("sessionMeshGroup") + slice("sessMeshes")
  + slice("railMeshTags") + slice("sessHandles") + slice("handleTag")
  + slice("profileHarnessLabel") + slice("railMetaText")
  + slice("refreshSessions")
  + `
Object.assign(exports, {
  refresh: refreshSessions,
});`)(ctx, document, el, api, list, [], false);

const drawn = () => walk(list).filter((r) => r.classes.has("sess-card"))
  .map((r) => r.dataset.name);

const fleet = (prefix, n) => Array.from({ length: n }, (_, i) => ({
  name: `${prefix}${i}`, status: "idle", profile: "nc", parent: null,
}));

/* A macrotask turn: what the pass's own yield schedules. */
const turn = () => new Promise((resolve) => setTimeout(resolve, 0));

(async () => {
  // The budget is forced to zero, so every slice hands back to the event loop
  // after its first row whatever this machine's speed is.
  ctx.refresh._sliceMs = 0;

  /* ---- a fleet past the threshold is drawn across turns ---- */
  const big = fleet("b", 200);
  served = { sessions: big };
  const pass = ctx.refresh();
  await turn();
  const midway = drawn().length;
  check("a pass over a large fleet hands back to the event loop partway",
        midway > 0 && midway < big.length, true);
  await pass;
  check("...and the whole fleet is drawn once the pass settles",
        drawn().length, big.length);
  check("...in the order the daemon sent it", drawn()[0], "b0");

  /* ---- a superseded pass stops writing ---- */
  const first = fleet("a", 200);
  const second = fleet("c", 200);
  served = { sessions: first };
  const stale = ctx.refresh();
  await turn();                    // it is suspended partway through `first`
  served = { sessions: second };
  const fresh = ctx.refresh();     // a newer pass takes the sequence number
  await Promise.all([stale, fresh]);
  const left = drawn();
  check("a pass superseded while suspended leaves none of its own rows behind",
        left.filter((n) => n.startsWith("a")).length, 0);
  check("...and the newer pass drew its fleet whole", left.length, second.length);

  /* ---- a rail small enough to be one short task is still drawn in the turn
     that asks for it, which is what the other harnesses rely on ---- */
  const small = fleet("s", 40);
  served = { sessions: small };
  let after = -1;
  const sp = ctx.refresh().then(() => { after = drawn().length; });
  await sp;
  check("a small rail is drawn by the pass that was asked for it",
        after, small.length);
  check("...without waiting for a later turn", drawn().length, small.length);

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("railslice_check: ok");
})().catch((e) => {
  console.error("railslice_check: threw", e);
  process.exit(1);
});
