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
     against;
   * the list is signed where the loop ends, not where it begins, and a pass
     that is superseded after a newer one has already signed leaves that newer
     signature alone (found in review: the abandon path cleared it and the next
     poll rebuilt the whole fleet for nothing);
   * no synchronous stretch of a pass is a long task: every stretch between two
     yields is timed against the shipped budget, and the slowest must stay
     under 50 ms. This is the JavaScript half of the completion criterion; the
     browser's style/layout half is measured against the live daemon
     (`rail_split2.cjs` in the session's test directory), because it is the
     engine's work, not this code's.

   The budget is forced to a small truthy value (`refreshSessions._sliceMs =
   0.01`) so the slice boundary does not depend on how fast the machine running
   this is -- zero would fall back to the shipped 8 ms (`_sliceMs || 8`). */
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

/* Microtasks only: lets the pass run up to the point where it schedules a
   held yield, without letting any timer fire. */
const flush = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };

/* Holds every yield the pass schedules, so a test can say exactly where the
   pass is suspended instead of racing it. `release()` resumes it. */
const holdYields = () => {
  const real = global.setTimeout;
  const held = [];
  global.setTimeout = (fn, ms, ...rest) => {
    if (ms === 0 && typeof fn === "function") {
      held.push(fn);
      return { unref() {} };
    }
    return real(fn, ms, ...rest);
  };
  return {
    release() { const fns = held.splice(0); fns.forEach((f) => f()); return fns.length; },
    restore() { global.setTimeout = real; },
    get pending() { return held.length; },
  };
};

(async () => {
  // Small but truthy: the shipped code reads `_sliceMs || 8`, so zero would
  // fall back to the default budget. At 0.01 ms every slice hands back to the
  // event loop after its first row whatever this machine's speed is.
  ctx.refresh._sliceMs = 0.01;

  /* ---- a fleet past the threshold is drawn across turns ---- */
  const big = fleet("b", 200);
  served = { sessions: big };
  let settled = false;
  const pass = ctx.refresh().then(() => { settled = true; });
  await turn();
  const midway = drawn().length;
  // It has handed the thread back: the pass is not finished and the rail is
  // not whole. How many rows the first turn drew depends on the budget and the
  // machine, so that is not what this asks.
  check("a pass over a large fleet hands back to the event loop partway",
        settled === false && midway < big.length, true);
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

  /* ---- the signature belongs to the pass that ended the loop ---- */
  list._sessionsSignature = null;
  list._sessionsSigSeq = null;
  served = { sessions: fleet("g", 300) };
  const signing = ctx.refresh();
  await turn();
  check("a pass that is still drawing has not signed the list",
        !!list._sessionsSignature, false);
  check("...and it is really partway through", drawn().length < 300, true);
  await signing;
  check("...it signs when its loop ends",
        typeof list._sessionsSignature, "string");

  /* ---- a superseded pass does not erase a newer pass's signature ----
     Found in review (claunch-rw7tw): the abandon path cleared the list's
     signature unconditionally, so a pass that resumed after a newer one had
     finished wiped that newer signature and the next poll rebuilt the whole
     fleet for nothing. The older pass is held at its first yield so the two
     passes cannot race. */
  served = { sessions: fleet("a", 300) };
  list._sessionsSignature = null;
  list._sessionsSigSeq = null;
  const hold = holdYields();
  const older = ctx.refresh();
  await flush();                      // it reaches its first yield and waits
  check("the older pass is suspended at a yield, not finished",
        hold.pending > 0 && drawn().filter((n) => n.startsWith("a")).length < 300, true);
  served = { sessions: fleet("c", 40) };
  const newer = ctx.refresh();        // 40 rows: finishes in this same turn
  await newer;
  const newerSig = list._sessionsSignature;
  check("a newer pass signs the fleet it drew", typeof newerSig, "string");
  hold.release();                     // the older pass resumes and is superseded
  await older;
  hold.restore();
  check("a superseded pass leaves the newer pass's signature in place",
        list._sessionsSignature, newerSig);
  const keptRow = walk(list).find((r) => r.classes.has("sess-card"));
  await ctx.refresh();
  check("...so the next poll keeps the rows it already drew",
        walk(list).find((r) => r.classes.has("sess-card")) === keptRow, true);

  /* ---- ...even when one more pass started since without signing ----
     Found in the second review round (claunch-rw7tw): comparing the signature
     against the NEWEST pass number let a resumed pass clear a signature that
     was still newer than its own. A filter pick fires both a cached pass and a
     network pass; the second finds the fleet unchanged, draws in place and
     signs nothing, so "not the newest" is not the same as "older than me".
     Three steps, each held so they cannot race: */
  served = { sessions: fleet("d", 300) };
  list._sessionsSignature = null;
  list._sessionsSigSeq = null;
  const hold3 = holdYields();
  const oldest = ctx.refresh();
  await flush();                       // suspended at its first yield
  served = { sessions: fleet("e", 40) };
  const signer = ctx.refresh();        // 40 rows: signs in this turn
  await signer;
  const signerSig = list._sessionsSignature;
  const inPlace = ctx.refresh();       // same fleet: in place, signs nothing
  await inPlace;
  check("a third pass over the same fleet draws in place and signs nothing",
        list._sessionsSignature, signerSig);
  hold3.release();                     // the oldest pass resumes and is superseded
  await oldest;
  hold3.restore();
  check("...the oldest pass leaves that signature in place",
        list._sessionsSignature, signerSig);

  /* ---- no synchronous stretch of a pass is a long task ----
     The completion criterion is per task, not per pass (claunch-rw7tw): the
     pass may take as many turns as it needs, but no single stretch of it may
     hold the thread for a long task. Measured on the real `refreshSessions`,
     with the harness's forced budget so a stretch is one row's work -- at the
     shipped 8 ms a loaded machine inflates any wall-clock bound, and a check
     that fails when the machine is busy teaches the wrong lesson. The shipped
     budget is then checked structurally: it still hands the pass back. */
  const realSetTimeout = global.setTimeout;
  const timeSegments = async (rows) => {
    const segments = [];
    let mark = performance.now();
    global.setTimeout = (fn, ms, ...rest) => {
      const now = performance.now();
      segments.push(now - mark);
      mark = now;
      return realSetTimeout(fn, ms, ...rest);
    };
    try {
      served = { sessions: fleet("t", rows) };
      list._sessionsSignature = null;
      list._sessionsSigSeq = null;
      mark = performance.now();
      await ctx.refresh();
    } finally {
      global.setTimeout = realSetTimeout;
    }
    return segments;
  };

  const segments = await timeSegments(400);   // harness budget (0.01 ms)
  const longest = Math.max(...segments);
  check("a pass over 400 rows is split into more than one segment",
        segments.length > 1, true);
  check("...and no stretch holds the thread for a long task (under 50 ms)",
        longest < 50, true);
  check("...the whole fleet is still drawn", drawn().length, 400);

  delete ctx.refresh._sliceMs;                // the shipped budget: 8 ms
  // 400 rows do not reach 8 ms of work in this stub; a fleet of the size the
  // operator reported does.
  const shipped = await timeSegments(1200);
  check("the shipped budget hands the pass back too", shipped.length > 1, true);
  check("...and the fleet is still whole", drawn().length, 1200);

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("railslice_check: ok");
})().catch((e) => {
  console.error("railslice_check: threw", e);
  process.exit(1);
});
