/* The rail holds still while a pointer is down on it — run against the real
   refreshSessions from app.js and a stub DOM.

   The rail is rebuilt from scratch on every poll (`list.innerHTML = ""`,
   from setInterval(pollTick, 2000)), and a press is not an instant:
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

const list = node("ul");
let served = { sessions: [], llm_configured: true };
let ok = true;
const api = async () => ({ ok, json: async () => served });

/* Everything refreshSessions leans on that is not the hold. decorateBriefingRow
   is the one that is not a no-op: it hangs a marker on the row standing in for
   the ⟳ the real one builds, so "the row survived" can be checked as "the node
   the finger was on survived" rather than only as a row count. */
const stubs = `
let sessionsCache = [], currentName = null, currentPage = "home";
let attachedPid = null, linkState = "down", sessName = null, briefingLLM = true;
function forgetDeadSessions() {}
function refreshResumeChoices() {}
function refreshParentChoices() {}
function renderHome() {}
function syncBulkActions() {}
function syncMobileBars() {}
function applyCflowBadges() {}
function applyGotoFlash() {}
function applyBriefingCards() {}
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
function openSpawnModal() {}
function openDetail() {}
function $(id) { return list; }
`;

const ctx = {};
new Function(
  "exports", "document", "el", "api", "list", "setTimeout",
  stubs
  + holdMs[0] + "\n" + heldUntil[0] + "\n" + pending[0] + "\n"
  + slice("railHeld") + slice("holdRail") + slice("releaseRail")
  + slice("byLineage") + slice("refreshSessions")
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
});`)(ctx, document, el, api, list, setTimeout);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}
const names = () => list.kids.map((li) => li.dataset.name);
const glyph = (li) => li.kids.find((k) => k.classes.has("sess-brief-rowref"));
const tick = () => new Promise((r) => setTimeout(r, 0));

const TWO = [
  { name: "s1", status: "idle", harness: "claude", cwd: "" },
  { name: "s2", status: "exited", exit_code: 2, harness: "claude", cwd: "" },
];

(async () => {
  served = { sessions: TWO, llm_configured: true };

  /* ---- the ordinary poll still rebuilds ------------------------------- */
  await ctx.refresh();
  check("a poll draws the rows", names(), ["s1", "s2"]);
  const first = [...list.kids];
  check("...each carrying the row's own glyph",
        list.kids.map((li) => !!glyph(li)), [true, true]);
  await ctx.refresh();
  check("an unheld poll rebuilds — none of the rows is the same object",
        list.kids.some((li, i) => li === first[i]), false);

  /* ---- a press freezes the teardown, and only the teardown ------------ */
  const before = [...list.kids];
  const pressed = glyph(list.kids[1]);
  ctx.hold();
  check("a pointer down on the rail holds it", ctx.held(), true);
  /* The list changes underneath — a session appears — and the poll still
     takes the new data; it is the DOM that waits. */
  served = {
    sessions: [...TWO, { name: "s3", status: "busy", harness: "claude", cwd: "" }],
    llm_configured: true,
  };
  await ctx.refresh();
  check("a poll mid-press leaves the rows exactly where they were",
        list.kids.map((li, i) => li === before[i]), [true, true]);
  check("...the very node under the finger included",
        glyph(list.kids[1]) === pressed, true);
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
  await ctx.refresh();
  check("...so the next poll draws normally again", names(), ["s1"]);

  /* ---- a failed poll leaves the page as it was ------------------------ */
  const kept = [...list.kids];
  const cached = ctx.cache().map((s) => s.name);
  ok = false;
  served = { error: "no" };
  await ctx.refresh();
  check("an error response is not 'this daemon has no sessions'",
        list.kids.map((li, i) => li === kept[i]), [true]);
  check("...and the cache forgetDeadSessions judges by is untouched",
        ctx.cache().map((s) => s.name), cached);
  ok = true;

  process.exit(failures ? 1 : 0);
})();
