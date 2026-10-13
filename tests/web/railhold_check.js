/* The rail holds still while a pointer is down on it — run against the real
   refreshSessions from app.js and a stub DOM.

   A row is rebuilt when its session's data changes (the whole rail, in the
   reduced harness below that cannot move nodes), and a press is not an
   instant:
   pointerdown, pointerup, and only then the click a handler is waiting for.
   A rebuild landing between the first two takes the node the press started
   on out of the document, and the browser has no common ancestor left to
   dispatch the click to — so the press produces nothing at all, with no sign
   it was ever taken. It reads as "the button does nothing", intermittently,
   and it lands hardest on the row's small glyphs (▸ briefing, ⟳ refresh, ⓘ
   details, + spawn).

   What has to hold: a poll arriving mid-press leaves the rows exactly where
   they were — the same objects, not equal ones, because identity is the
   whole point; it still takes everything else the poll brought; it remembers
   that it owes a redraw and pays it on release, but not before the click
   that press is still owed; the hold expires on its own so a lost pointerup
   cannot freeze the rail for the rest of the session; and a failed poll
   leaves the page as it was instead of reading an error body as "this daemon
   has no sessions". */
const fs = require("fs");
const path = require("path");
/* The env override is how this file is shown to FAIL: point it at a copy of
   app.js with the hold neutered and every behavioural check below goes red,
   which is the only way to know they are testing the hold and not merely
   agreeing with it. */
const src = fs.readFileSync(
  process.env.RAILHOLD_APP_JS || path.join(
    __dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  const head = src.lastIndexOf("async ", start) === start - 6 ? start - 6 : start;
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}
/* The hold's state lives at module level, so it is lifted by line rather than
   by function — and pinned here, because a hold with no deadline is half of
   the bug this file exists to keep out. */
const holdMs = src.match(/^const RAIL_HOLD_MS = .+$/m);
const heldUntil = src.match(/^let railHeldUntil = .+$/m);
const pending = src.match(/^let railRedrawPending = .+$/m);
if (!holdMs || !heldUntil || !pending) {
  throw new Error("cannot locate the rail hold's state in app.js");
}

/* ---- stub DOM ---------------------------------------------------------- */
/* Nodes move the way the browser moves them: appending or inserting a node
   that already has a parent takes it out of that parent first. The in-place
   redraw (the second half of this file) depends on exactly that. */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), dataset: {}, style: {},
    title: "", type: "", parentNode: null,
    get childNodes() { return n.kids; },
    appendChild(c) { return n.insertBefore(c, null); },
    insertBefore(c, ref) {
      if (c.parentNode) c.parentNode.removeChild(c);
      const at = ref ? n.kids.indexOf(ref) : -1;
      if (at < 0) n.kids.push(c); else n.kids.splice(at, 0, c);
      c.parentNode = n;
      return c;
    },
    removeChild(c) {
      const at = n.kids.indexOf(c);
      if (at >= 0) n.kids.splice(at, 1);
      c.parentNode = null;
      return c;
    },
    remove() { if (n.parentNode) n.parentNode.removeChild(n); },
    replaceWith(m) {
      const p = n.parentNode;
      if (!p) return;
      p.insertBefore(m, n);
      p.removeChild(n);
    },
    get firstElementChild() { return n.kids[0] || null; },
    append(...cs) { cs.forEach((c) => n.appendChild(c)); },
    addEventListener() {},
    /* Only the two shapes refreshRailSeen asks: ".class" for the first
       descendant wearing it, "li[data-name]" for every row. */
    querySelector(sel) {
      const cls = sel.startsWith(".") ? sel.slice(1) : null;
      for (const k of n.kids) {
        if (cls && k.classes && k.classes.has(cls)) return k;
        const deep = k.querySelector ? k.querySelector(sel) : null;
        if (deep) return deep;
      }
      return null;
    },
    querySelectorAll(sel) {
      if (sel !== "li[data-name]") return [];
      const out = [];
      for (const k of n.kids) {
        if (k.dataset && k.dataset.name) out.push(k);
        if (k.querySelectorAll) out.push(...k.querySelectorAll(sel));
      }
      return out;
    },
    get textContent() { return n.text; },
    set textContent(v) { n.text = String(v); },
    get className() { return [...n.classes].join(" "); },
    set className(v) { n.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get innerHTML() { return ""; },
    set innerHTML(v) { n.kids.slice().forEach((c) => n.removeChild(c)); },
  };
  n.classList = {
    add: (...cs) => cs.forEach((c) => n.classes.add(c)),
    remove: (...cs) => cs.forEach((c) => n.classes.delete(c)),
    contains: (c) => n.classes.has(c),
    toggle: (c, on) => (on ? n.classes.add(c) : n.classes.delete(c)),
  };
  return n;
}
/* The first harness is the reduced one: no createDocumentFragment, so
   refreshSessions keeps its teardown and the hold is all that keeps a row. */
const document = { createElement: node };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const list = node("ul");
let served = { sessions: [], llm_configured: true };
let ok = true;
const api = async () => ({ ok, json: async () => served });

/* Everything refreshSessions leans on that is not the hold. decorateBriefingRow
   is the one that is not a no-op: it hangs a marker on the row standing in for
   the ⟳ the real one builds, so "the row survived" can be checked as "the node
   the finger was on survived" rather than only as a row count. */
const stubs = `
function sessionMatchesFilter() { return true; }
let sessionsCache = [], currentName = null, currentPage = "home";
let attachedPid = null, linkState = "down", sessName = null, briefingLLM = true, snapshotName = null;
function forgetDeadSessions() {}
function refreshResumeChoices() {}
function refreshParentChoices() {}
function renderHome() {}
function syncBulkActions() {}
function syncMobileBars() {}
/* The terminal header's mesh-handle chip, which the session poll
   repaints (sesshandle_check's subject); here it is only a call that
   has to resolve — this harness draws no header. */
function renderTermHandle() {}
function applyCflowBadges() {}
function applyGotoFlash() {}
function applyRailQuiet() {}
function applyBriefingCards() {}
/* The reader's own note line, stubbed for the same reason as the briefing
   decoration below: its wording and its stylesheet contract are
   railnote_check's subject, not this harness's. */
function decorateNoteRow(li, s) {}
function decorateProgressRow(li, s) {}
function decorateBriefingRow(li, s) {
  li.appendChild(el("button", "sess-brief-rowref", "R"));
}
function terminalOnScreen() { return false; }
function attach() {}
function setStatusBadge() {}
function ctxNoteOnRow() {}
function ctxRailLine(s) { return el("span", "rail-ctx-line unknown"); }
function railCwdLine(s) { return el("span", "rail-cwd"); }
/* ...and the attention line beside them, stubbed for the same reason: this
   harness is about what a press does to a rebuild, not about what any of
   the row's lines say (railseen_check holds that one). */
function railSeenLine(s) { return el("span", "rail-seen"); }
function railMeshTags(name) { return []; }
/* The mesh handle a row/head wears when it differs from the session name
   (sesshandle_check's subject); here it is only a call that has to
   resolve. */
function handleTag(name) { return null; }
/* The mesh memberships a row's role chip consults (meshrole_check's
   subject); here it is only a call that has to resolve. */
function sessMeshes(name) { return []; }
function openSpawnModal() {}
function openDetail() {}
function $(id) { return list; }
`;

const ctx = {};
new Function(
  "exports", "document", "el", "api", "list", "setTimeout", "meshCache", "sessionGroupByMesh",
  stubs
  + holdMs[0] + "\n" + heldUntil[0] + "\n" + pending[0] + "\n"
  + slice("railHeld") + slice("holdRail") + slice("releaseRail")
  + slice("byLineage") + slice("sessionMeshGroup") + slice("profileHarnessLabel")
  + slice("railMetaText")
  + slice("refreshSessions")
  + `
Object.assign(exports, {
  refresh: refreshSessions,
  hold: holdRail,
  release: releaseRail,
  held: () => railHeld(),
  owed: () => railRedrawPending,
  cache: () => sessionsCache,
  /* A pointerup that never came: the deadline is all that is left, so wind it
     back rather than sleeping RAIL_HOLD_MS in a test. */
  expire: () => { railHeldUntil = Date.now() - 1; },
  deadline: RAIL_HOLD_MS,
});`)(ctx, document, el, api, list, setTimeout, [], true);

/* The second harness is the shipped page's shape: a document that can make
   fragments, so refreshSessions updates the rail in place. Ungrouped, so the
   rows are the list's own children. */
const placeList = node("ul");
const placeCtx = {};
let placeServed = { sessions: [], llm_configured: true };
new Function(
  "exports", "document", "el", "api", "list", "setTimeout", "meshCache", "sessionGroupByMesh",
  stubs
  + holdMs[0] + "\n" + heldUntil[0] + "\n" + pending[0] + "\n"
  + slice("railHeld") + slice("holdRail") + slice("releaseRail")
  + slice("byLineage") + slice("sessionMeshGroup") + slice("profileHarnessLabel")
  + slice("railMetaText")
  + slice("refreshSessions")
  + `
Object.assign(exports, { refresh: refreshSessions, hold: holdRail });`)(
  placeCtx, { createElement: node, createDocumentFragment: () => node("#fragment") },
  el, async () => ({ ok: true, json: async () => placeServed }), placeList,
  setTimeout, [], false);

/* The third is the same page grouped by mesh: group containers are kept
   across a redraw too, and a row whose mesh changed moves under its new
   heading. Later declarations replace the shared stubs' sessMeshes. */
const groupList = node("ul");
const groupCtx = {};
let groupServed = { sessions: [], llm_configured: true };
const meshOf = {};
new Function(
  "exports", "document", "el", "api", "list", "setTimeout", "meshCache", "sessionGroupByMesh",
  "meshOf",
  stubs + `
function sessMeshes(name) { return meshOf[name] ? [{ mesh: meshOf[name], label: "worker" }] : []; }
function isSessionGroupCollapsed() { return false; }
function setSessionGroupCollapsed() {}
`
  + holdMs[0] + "\n" + heldUntil[0] + "\n" + pending[0] + "\n"
  + slice("railHeld") + slice("holdRail") + slice("releaseRail")
  + slice("byLineage") + slice("sessionMeshGroup") + slice("profileHarnessLabel")
  + slice("railMetaText")
  + slice("refreshSessions")
  + `
Object.assign(exports, { refresh: refreshSessions });`)(
  groupCtx, { createElement: node, createDocumentFragment: () => node("#fragment") },
  el, async () => ({ ok: true, json: async () => groupServed }), groupList,
  /* meshOf is also the meshCache the page signature reads, as the mesh poll's
     answer is on the real page: a membership change is a change to draw. */
  setTimeout, meshOf, true, meshOf);

/* The fourth is the in-place page with the real tps line and the real
   refreshRailSeen: a row kept across polls must still show how old its tps
   reading is (s88's T1 on claunch-v87). Date.now is the clock it reads. */
const tpsList = node("ul");
const tpsCtx = {};
let tpsServed = { sessions: [], llm_configured: true };
new Function(
  "exports", "document", "el", "api", "list", "setTimeout", "meshCache", "sessionGroupByMesh",
  stubs + "function railSeenLine(s) { return el('span', 'rail-seen'); }\n"
  + holdMs[0] + "\n" + heldUntil[0] + "\n" + pending[0] + "\n"
  + slice("railHeld") + slice("holdRail") + slice("releaseRail")
  + slice("byLineage") + slice("sessionMeshGroup") + slice("profileHarnessLabel")
  + slice("railMetaText") + slice("fmtAge")
  + slice("tpsFmt") + slice("ttftFmt") + slice("tpsLatencyText") + slice("tpsAgeSecs")
  + slice("tpsSecs") + slice("tpsLiveText")
  + slice("tpsText") + slice("tpsTooltip") + slice("tpsStaleClass") + slice("tpsRailLine")
  + slice("refreshRailSeen")
  + slice("refreshSessions")
  + `
Object.assign(exports, { refresh: refreshSessions });`)(
  tpsCtx, { createElement: node, createDocumentFragment: () => node("#fragment") },
  el, async () => ({ ok: true, json: async () => tpsServed }), tpsList,
  setTimeout, [], false);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}
const rows = () => list.kids.filter((li) => li.dataset.name);
const names = () => rows().map((li) => li.dataset.name);
const glyph = (li) => li.kids.find((k) => k.classes.has("sess-brief-rowref"));
const tick = () => new Promise((r) => setTimeout(r, 0));

const TWO = [
  { name: "s1", status: "idle", harness: "claude", cwd: "" },
  { name: "s2", status: "exited", exit_code: 2, harness: "claude", cwd: "" },
];

(async () => {
  served = { sessions: TWO, llm_configured: true };

  /* ---- an unchanged ordinary poll preserves the rows ------------------ */
  await ctx.refresh({ state: "current" });
  check("a poll draws the rows", names(), ["s1", "s2"]);
  const first = [...rows()];
  check("...each carrying the row's own glyph",
        rows().map((li) => !!glyph(li)), [true, true]);
  await ctx.refresh({ state: "current" });
  check("an unchanged unheld poll keeps every row object",
        rows().map((li, i) => li === first[i]), [true, true]);

  /* ---- a press freezes the teardown, and only the teardown ------------ */
  const before = [...rows()];
  const pressed = glyph(rows()[1]);
  ctx.hold();
  check("a pointer down on the rail holds it", ctx.held(), true);
  /* The list changes underneath — a session appears — and the poll still
     takes the new data; it is the DOM that waits. */
  served = {
    sessions: [...TWO, { name: "s3", status: "busy", harness: "claude", cwd: "" }],
    llm_configured: true,
  };
  await ctx.refresh({ state: "current" });
  check("a poll mid-press leaves the rows exactly where they were",
        rows().map((li, i) => li === before[i]), [true, true]);
  check("...the very node under the finger included",
        glyph(rows()[1]) === pressed, true);
  check("...so the rail has not yet grown the new row", names(), ["s1", "s2"]);
  check("...but the poll's data was taken all the same",
        ctx.cache().map((s) => s.name), ["s1", "s2", "s3"]);
  check("...and the redraw it skipped is remembered", ctx.owed(), true);

  /* ---- release pays the redraw back ----------------------------------- */
  ctx.release();
  check("releasing lets go of the hold", ctx.held(), false);
  /* Deferred by a task on purpose: the click is dispatched after the
     pointerup handler returns, so the redraw must not run before it. */
  check("...but not in the same task as the pointerup",
        names(), ["s1", "s2"]);
  await tick();
  await tick();
  check("...and then the rail catches up", names(), ["s1", "s2", "s3"]);
  check("...with nothing still owed", ctx.owed(), false);

  /* ---- a lost pointerup cannot freeze the rail ------------------------ */
  ctx.hold();
  check("the hold carries a deadline at all", ctx.deadline > 0, true);
  ctx.expire();
  check("an expired hold is no hold", ctx.held(), false);
  served = { sessions: [TWO[0]], llm_configured: true };
  await ctx.refresh({ state: "current" });
  check("...so the next poll draws normally again", names(), ["s1"]);

  /* ---- a failed poll leaves the page as it was ------------------------ */
  const kept = [...rows()];
  const cached = ctx.cache().map((s) => s.name);
  ok = false;
  served = { error: "no" };
  await ctx.refresh({ state: "current" });
  check("an error response is not 'this daemon has no sessions'",
        rows().map((li, i) => li === kept[i]), [true]);
  check("...and the cache forgetDeadSessions judges by is untouched",
        ctx.cache().map((s) => s.name), cached);
  ok = true;

  /* ---- no press: the rail updates in place (claunch-v87) -------------- */
  const placeRows = () => placeList.kids.filter((li) => li.dataset.name);
  const placeNames = () => placeRows().map((li) => li.dataset.name);
  placeServed = { sessions: TWO, llm_configured: true };
  await placeCtx.refresh({ state: "current" });
  check("in place: a poll draws the rows", placeNames(), ["s1", "s2"]);
  const kept0 = [...placeRows()];
  placeServed = {
    sessions: [...TWO, { name: "s3", status: "busy", harness: "claude", cwd: "" }],
    llm_configured: true,
  };
  await placeCtx.refresh({ state: "current" });
  check("in place: a session appearing adds its row", placeNames(), ["s1", "s2", "s3"]);
  check("...and every other row stays the same object, with no press holding it",
        placeRows().slice(0, 2).map((li, i) => li === kept0[i]), [true, true]);
  const kept1 = [...placeRows()];
  placeServed = {
    sessions: [TWO[0], { ...TWO[1], status: "idle", exit_code: null },
               { name: "s3", status: "busy", harness: "claude", cwd: "" }],
    llm_configured: true,
  };
  await placeCtx.refresh({ state: "current" });
  check("in place: a session that changed gets a fresh row",
        placeRows()[1] === kept1[1], false);
  check("...drawn from its new record",
        placeRows()[1].kids.find((k) => k.classes.has("meta")).text, "claude");
  check("...while the rows that did not change are kept",
        [placeRows()[0] === kept1[0], placeRows()[2] === kept1[2]], [true, true]);
  const kept2 = [...placeRows()];
  placeServed = { sessions: [TWO[0], { name: "s3", status: "busy", harness: "claude", cwd: "" }],
                  llm_configured: true };
  await placeCtx.refresh({ state: "current" });
  check("in place: a session that left loses its row", placeNames(), ["s1", "s3"]);
  check("...and the rest are the same objects",
        [placeRows()[0] === kept2[0], placeRows()[1] === kept2[2]], [true, true]);
  check("...with nothing left behind in the list", placeList.kids.length, 2);
  check("...and no node claiming a parent it is not in",
        kept2[1].parentNode, null);

  /* ---- grouped: containers are kept, a row moves between them --------- */
  const groups = () => groupList.kids.filter((g) => g.dataset.group);
  const bodyOf = (g) => g.kids.find((k) => k.classes.has("session-group-body"));
  const countOf = (g) => g.kids[0].kids.find((k) => k.classes.has("session-group-count")).text;
  const rowsIn = (g) => bodyOf(g).kids.map((li) => li.dataset.name);
  Object.assign(meshOf, { s1: "a", s2: "a", s3: "b" });
  groupServed = {
    sessions: [...TWO, { name: "s3", status: "busy", harness: "claude", cwd: "" }],
    llm_configured: true,
  };
  await groupCtx.refresh({ state: "current" });
  check("grouped: two mesh groups", groups().map((g) => g.dataset.value), ["a", "b"]);
  check("...holding their rows", groups().map(rowsIn), [["s1", "s2"], ["s3"]]);
  const [ga, gb] = groups();
  const s1row = bodyOf(ga).kids[0];
  meshOf.s2 = "b";
  await groupCtx.refresh({ state: "current" });
  check("grouped: the group containers are the same objects",
        [groups()[0] === ga, groups()[1] === gb], [true, true]);
  check("...the row whose mesh changed sits under its new heading",
        groups().map(rowsIn), [["s1"], ["s2", "s3"]]);
  check("...each heading counts what it now holds",
        groups().map(countOf), ["1", "2"]);
  check("...and the untouched row is the same object", bodyOf(ga).kids[0] === s1row, true);
  meshOf.s1 = "b";
  await groupCtx.refresh({ state: "current" });
  check("grouped: a group left empty is removed",
        groups().map((g) => g.dataset.value), ["b"]);
  check("...and its container is detached", ga.parentNode === null, true);

  /* ---- a kept row's tps age moves on (claunch-v87, s88 T1) ------------ */
  const realNow = Date.now;
  const T = Date.parse("2026-10-07T00:00:00Z");
  const tpsRec = { ts: new Date(T).toISOString(), tps: 42.5, max_age_s: 600 };
  const tpsRow = () => tpsList.kids.find((li) => li.dataset.name === "s1");
  const tpsLine = () => tpsRow().querySelector(".rail-tps-line");
  try {
    Date.now = () => T + 5000;
    tpsServed = { sessions: [{ ...TWO[0], tps: tpsRec }, TWO[1]], llm_configured: true };
    await tpsCtx.refresh({ state: "current" });
    const kept = tpsRow();
    check("tps: the first poll draws a fresh reading",
          [tpsLine().className, tpsLine().querySelector(".rail-tps-age").text],
          ["rail-tps-line", "5s ago"]);
    Date.now = () => T + 15 * 60 * 1000;
    tpsServed = { sessions: [{ ...TWO[0], tps: tpsRec },
                             { ...TWO[1], status: "idle", exit_code: null }],
                  llm_configured: true };
    await tpsCtx.refresh({ state: "current" });
    check("tps: another session's change keeps s1's row", tpsRow() === kept, true);
    check("...and its tps line shows the reading's age now, stale past max_age_s",
          [tpsLine().className, tpsLine().querySelector(".rail-tps-age").text],
          ["rail-tps-line stale", "15m ago"]);
    Date.now = () => T + 30 * 60 * 1000;
    await tpsCtx.refresh({ state: "current" });
    check("...and on a poll that changes nothing",
          tpsLine().querySelector(".rail-tps-age").text, "30m ago");
  } finally {
    Date.now = realNow;
  }

  process.exit(failures ? 1 : 0);
})();
