/* The rail keeps its scroll position across a redraw — run against the real
   refreshSessions from app.js and a stub DOM that models a scroll container.

   `#session-list` is not a list inside a scrolling panel; it IS the scrolling
   element (`overflow-y: auto` in style.css). A poll that finds the fleet
   changed rebuilds it from scratch with `list.innerHTML = ""`, and emptying a
   scroll container drops its content height to zero: the browser clamps
   scrollTop to 0 along with it, and re-appending the rows does not bring the
   position back. Every session-state change on the machine therefore threw a
   reader who had scrolled down the rail back to the top, which is what was
   reported — the rail going back up on its own, every so often.

   What has to hold: a rebuild leaves the reader where they were; a rebuild
   that drops rows lands at the new bottom rather than refusing to move; a
   poll that changes nothing, and a poll held by a press, leave the position
   alone; a rail already at the top is not nudged off it; and a reduced DOM
   with no scroll geometry at all still draws its rows. */
const fs = require("fs");
const path = require("path");
/* The env override is how this file is shown to FAIL: point it at a copy of
   app.js with the save/restore removed and the behavioural checks below go
   red, which is the only way to know they test the fix rather than agree
   with it. */
const src = fs.readFileSync(
  process.env.RAILSCROLL_APP_JS || path.join(
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
const holdMs = src.match(/^const RAIL_HOLD_MS = .+$/m);
const heldUntil = src.match(/^let railHeldUntil = .+$/m);
const pending = src.match(/^let railRedrawPending = .+$/m);
if (!holdMs || !heldUntil || !pending) {
  throw new Error("cannot locate the rail hold's state in app.js");
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), dataset: {}, style: {},
    title: "", type: "",
    appendChild(c) { n.kids.push(c); return c; },
    append(...cs) { cs.forEach((c) => n.appendChild(c)); },
    addEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    get textContent() { return n.text; },
    set textContent(v) { n.text = String(v); },
    get className() { return [...n.classes].join(" "); },
    set className(v) { n.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get innerHTML() { return ""; },
    set innerHTML(v) { n.kids = []; },
  };
  n.classList = {
    add: (...cs) => cs.forEach((c) => n.classes.add(c)),
    remove: (...cs) => cs.forEach((c) => n.classes.delete(c)),
    contains: (c) => n.classes.has(c),
    toggle: (c, on) => (on ? n.classes.add(c) : n.classes.delete(c)),
  };
  return n;
}
const document = { createElement: node };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

/* A scroll container, modelled the way a browser behaves rather than the way
   the fix would like it to: the position is stored, but it is REPORTED
   clamped to what the content allows, and emptying the element destroys it.
   That last line is the bug — without it this harness could not fail. */
const ROW_H = 24, VIEW_H = 120;
function scroller() {
  const n = node("ul");
  let top = 0;
  const maxTop = () => Math.max(0, n.scrollHeight - n.clientHeight);
  Object.defineProperties(n, {
    clientHeight: { value: VIEW_H, writable: true },
    scrollHeight: { get: () => n.kids.length * ROW_H },
    scrollTop: {
      get: () => Math.min(top, maxTop()),
      set: (v) => { top = Math.max(0, Math.min(Number(v) || 0, maxTop())); },
    },
    innerHTML: {
      get: () => "",
      set: () => { n.kids = []; top = 0; },
    },
  });
  return n;
}

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
function renderTermHandle() {}
function applyCflowBadges() {}
function applyGotoFlash() {}
function applyRailQuiet() {}
function applyBriefingCards() {}
/* The reader's own note line, stubbed for the same reason as the briefing
   decoration below: its wording and its stylesheet contract are
   railnote_check's subject, not this harness's. */
function decorateNoteRow(li, s) {}
function decorateBriefingRow() {}
function terminalOnScreen() { return false; }
function attach() {}
function setStatusBadge() {}
function ctxNoteOnRow() {}
function ctxRailLine(s) { return el("span", "rail-ctx-line unknown"); }
function railCwdLine(s) { return el("span", "rail-cwd"); }
function railSeenLine(s) { return el("span", "rail-seen"); }
function railMeshTags(name) { return []; }
function handleTag(name) { return null; }
function sessMeshes(name) { return []; }
function openSpawnModal() {}
function openDetail() {}
function $(id) { return list; }
`;

/* One module instance per list, so the scroll-less DOM below gets a page of
   its own instead of inheriting this one's caches. */
function build(list, api) {
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
  expire: () => { railHeldUntil = Date.now() - 1; },
  owed: () => railRedrawPending,
});`)(ctx, document, el, api, list, setTimeout, [], false);
  return ctx;
}

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const fleet = (n, status) => Array.from({ length: n }, (_, i) => (
  { name: `s${i + 1}`, status: i === 0 ? (status || "idle") : "idle",
    harness: "claude", cwd: "" }));

const list = scroller();
let served = { sessions: fleet(8), llm_configured: true };
const ctx = build(list, async () => ({ ok: true, json: async () => served }));
const names = () => list.kids.filter((li) => li.dataset.name).map((li) => li.dataset.name);

(async () => {
  await ctx.refresh({ state: "current" });
  check("a poll draws the rows", names().length, 8);
  /* 8 rows of 24 in a 120-high viewport: 72 of scroll, so 60 is a real
     position partway down and not a clamped 0. */
  check("...enough of them to scroll", list.scrollHeight - list.clientHeight, 72);
  list.scrollTop = 60;
  check("the reader scrolls down the rail", list.scrollTop, 60);

  /* ---- a rebuild leaves the reader where they were -------------------- */
  served = { sessions: fleet(8, "busy"), llm_configured: true };
  await ctx.refresh({ state: "current" });
  check("a session changing state redraws the rail", names().length, 8);
  check("...and the rail stays where the reader left it", list.scrollTop, 60);

  /* ---- a poll that changes nothing does not move it either ------------ */
  await ctx.refresh({ state: "current" });
  check("an unchanged poll leaves the position alone", list.scrollTop, 60);

  /* ---- nor does one held by a press ----------------------------------- */
  ctx.hold();
  served = { sessions: fleet(9, "busy"), llm_configured: true };
  await ctx.refresh({ state: "current" });
  check("a poll mid-press draws no new row", names().length, 8);
  check("...and leaves the position alone", list.scrollTop, 60);
  ctx.expire();

  /* ---- a rebuild that drops rows lands at the new bottom -------------- */
  served = { sessions: fleet(6), llm_configured: true };
  await ctx.refresh({ state: "current" });
  check("a shorter fleet redraws", names().length, 6);
  /* 6 rows leave 24 of scroll. Restoring 60 past that is not an error: the
     browser clamps it, and the rail sits at its new bottom. */
  check("...and the rail sits at the new bottom, not back at the top",
        list.scrollTop, 24);

  /* ---- a rail at the top is not nudged off it ------------------------- */
  list.scrollTop = 0;
  served = { sessions: fleet(7), llm_configured: true };
  await ctx.refresh({ state: "current" });
  check("a rail at the top redraws", names().length, 7);
  check("...and is still at the top", list.scrollTop, 0);

  /* ---- a DOM with no scroll geometry still draws ---------------------- */
  const plain = node("ul");
  let plainServed = { sessions: fleet(3), llm_configured: true };
  const plainCtx = build(plain, async () => ({ ok: true, json: async () => plainServed }));
  let threw = null;
  try {
    await plainCtx.refresh({ state: "current" });
    plainServed = { sessions: fleet(4), llm_configured: true };
    await plainCtx.refresh({ state: "current" });
  } catch (e) { threw = String((e && e.message) || e); }
  check("a rail with no scroll geometry does not throw", threw, null);
  check("...and still draws its rows",
        plain.kids.filter((li) => li.dataset.name).length, 4);

  process.exit(failures ? 1 : 0);
})();
