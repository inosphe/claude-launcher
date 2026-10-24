/* The rail's window has to survive a quiet poll, and its height model has to
   stay put (claunch-thuwx.1).

   The window landed in claunch-thuwx (commit 31bde0d0). On 2026-10-25 the
   operator reported two things about it: "스크롤시에 아무것도 안그려지는 경우가
   너무 빈번하네. 이 위로도 아무것도 안그려지고있어", and "스크롤 알고리즘이
   결정론적이지도 않고, 수렴하지도 않고 발산하는 느낌". Both are held here,
   against the real `refreshSessions`, the real window helpers and a stub DOM
   that has a viewport and measurable rows.

   * a pass that drew nothing (a quiet poll, a press-held pass) leaves the
     list's window alone. Clearing it there is what killed the scroll
     handler: `railWireWindowScroll` bails on a list with no `_railRows`, so
     every poll quietly turned scrolling off until some session changed;
   * a scroll after such a poll draws the region it scrolled to -- the whole
     path end to end, from the scroll event to the rebuilt slice;
   * the height a row is assumed to have is a property of the rail, not of
     where the reader has been. It is sampled once from the rows this window
     has seen and then frozen, so measuring a tall region does not move the
     plan under a reader who is somewhere else;
   * a model that drifts under the reader (rows above them measured taller)
     is corrected for, by the anchor row's own drift, so the row the reader
     was looking at stays where it was -- and two passes over an unchanged
     model at the same position are the same pass (no correction loop). */
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const src = fs.readFileSync(
  process.env.RAILWINDOW_APP_JS || path.join(STATIC, "app.js"), "utf8");

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
/* The window is under repair (claunch-thuwx.1), so this check has to load
   against the code as it stands AND against the code being written: a piece
   the fix adds is optional here and its absence is a named failure below,
   never a crash that hides which behaviour broke. Once the fix has landed the
   fallbacks are dead weight and should go with it. */
function optionalSlice(name) {
  try { return slice(name); } catch { return ""; }
}
/* Read out of app.js rather than retyped, so a rename here fails loudly
   instead of quietly testing a copy that no longer matches. */
function constant(name) {
  const m = src.match(new RegExp(`^const ${name} = [^\\n]+$`, "m"));
  if (!m) throw new Error("missing const " + name);
  return m[0];
}
function optionalConstant(name, fallback) {
  try { return constant(name); } catch { return fallback; }
}
function state(name) {
  const m = src.match(new RegExp(`^(?:const|let) ${name} = [^\\n]+$`, "m"));
  if (!m) throw new Error("missing state " + name);
  return m[0];
}
function optionalState(name, fallback) {
  try { return state(name); } catch { return fallback; }
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

/* ---- stub DOM -------------------------------------------------------- */
/* Row heights are by name, so a test can say which rows are tall. Anything
   with a `style.height` (a spacer) is that height; a heading is its own; a
   container is the sum of what it holds. */
const heights = new Map();
function nodeHeight(n) {
  if (!n) return 0;
  if (n.style && n.style.height) return parseFloat(n.style.height) || 0;
  if (n.dataset && n.dataset.name) {
    return heights.has(n.dataset.name) ? heights.get(n.dataset.name) : 72;
  }
  if (!n.kids || !n.kids.length) return 34;
  return n.kids.reduce((sum, kid) => sum + nodeHeight(kid), 0);
}

/* Where a node sits on screen: the list's own top is 0, and everything in it
   is laid out top to bottom and moved up by the list's scrollTop -- what
   getBoundingClientRect reports in a page, for the anchor to read. */
function nodeTop(n) {
  if (!n || n === list || !n.parentNode) return 0;
  const parent = n.parentNode;
  let at = parent === list ? -(list.scrollTop || 0) : nodeTop(parent);
  for (const kid of parent.kids) {
    if (kid === n) break;
    at += nodeHeight(kid);
  }
  return at;
}

let scrollListeners = [];
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), dataset: {}, style: {},
    title: "", type: "", parentNode: null, scrollTop: 0,
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
    addEventListener(type, fn) {
      if (type === "scroll" && n === list) scrollListeners.push(fn);
    },
    setAttribute(k, v) { n[k] = v; },
    getAttribute(k) { return n[k]; },
    querySelector() { return null; },
    querySelectorAll(sel) {
      const want = matchSelector(sel);
      return walk(n).filter(want);
    },
    getBoundingClientRect() { return { top: nodeTop(n), height: nodeHeight(n) }; },
    get firstElementChild() { return n.kids.find((k) => k.tag) || null; },
    get childNodes() { return n.kids; },
    get scrollHeight() { return nodeHeight(n); },
    get clientHeight() { return n === list ? listViewport : nodeHeight(n); },
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
    remove: (...cs) => cs.forEach((c) => n.classes.delete(c)),
  };
  return n;
}
/* The three selectors the window reads: a drawn row, a heading, a group. */
function matchSelector(sel) {
  if (sel === "li[data-name]") return (n) => n.tag === "li" && !!n.dataset.name;
  if (sel === ".session-group-heading") {
    return (n) => n.classes.has("session-group-heading");
  }
  if (sel === "li.session-group") {
    return (n) => n.tag === "li" && n.classes.has("session-group");
  }
  return () => false;
}
function walk(n, out = []) {
  for (const k of n.kids) { out.push(k); walk(k, out); }
  return out;
}
const list = node("ul");
let listViewport = 900;
const document = { createElement: (tag) => node(tag) };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}

let served = { sessions: [] };
const api = async () => ({ ok: true, json: async () => served });

/* Everything refreshSessions leans on that is not the row itself, stubbed as
   railslice_check stubs it. Line/decoration builders are inert: this check is
   about where the window sits, not what a row paints. */
const stubs = `
let sessionFilter = ["running", "hold", "delayed", "paused"];
let sessionsCache = [], currentName = null, currentPage = "home";
let attachedPid = null, linkState = "down", sessName = null, snapshotName = null;
let keptTerms = new Map();
let groupOrder = [];
let sessionView = "list";
let railHeldOn = false;
function dropKept() {}
function railHeld() { return railHeldOn; }
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
function railFocusedCardName() { return null; }
`;

const ctx = {};
new Function(
  "exports", "document", "el", "api", "list", "meshCache", "sessionGroupByMesh",
  stubs + capLine[0] + "\n"
  + [
      constant("RAIL_WINDOW_MIN_ROWS"), constant("RAIL_WINDOW_OVERSCAN_PX"),
      constant("RAIL_ROW_EST_PX"), constant("RAIL_WINDOW_SCROLL_MS"),
      optionalConstant("RAIL_WINDOW_SLACK_PX", ""),
      optionalConstant("RAIL_ESTIMATE_SAMPLE", "const RAIL_ESTIMATE_SAMPLE = 32;"),
    ].join("\n")
  + "\n"
  + [state("railRowPx"), state("railSeen"),
     optionalState("railSeenKeys", "const railSeenKeys = new Set();"),
     state("railWindowScrollTimer")].join("\n")
  + "\n"
  + slice("sessionCategory") + slice("sessionMatchesFilter")
  + slice("byLineage") + slice("sessionMeshGroup") + slice("sessMeshes")
  + slice("railMeshTags") + slice("sessHandles") + slice("handleTag")
  + slice("profileHarnessLabel") + slice("railMetaText")
  + slice("railEstimatePx") + slice("railRowKey") + slice("railRowPxOf")
  + optionalSlice("railOffsets") + slice("railWindowPlan") + slice("railSpacerRow")
  + slice("railMeasureRows") + slice("railWindowForPass")
  + optionalSlice("railSliceCovers")
  + slice("railWireWindowScroll") + optionalSlice("railClearWindow")
  + optionalSlice("railTakeAnchor") + optionalSlice("railPlanAnchor")
  + optionalSlice("railRestoreAnchor")
  + slice("refreshSessions")
  + `
const __api = {
  refresh: refreshSessions,
  railRowPx, railSeen, railSeenKeys, railEstimatePx, railRowPxOf, railRowKey,
  railWindowPlan, railWindowForPass, railMeasureRows,
  held(on) { railHeldOn = on; },
  view(name) { sessionView = name; },
};
// A piece the fix adds is exported only when it is there (optionalSlice).
if (typeof railOffsets === "function") __api.railOffsets = railOffsets;
if (typeof railClearWindow === "function") __api.railClearWindow = railClearWindow;
Object.assign(exports, __api);`)(ctx, document, el, api, list, [], false);

const A = ctx;
const { refresh, railRowPx, railSeen, railSeenKeys, railEstimatePx,
        railWindowPlan, railWindowForPass, railMeasureRows,
        railClearWindow } = A;
/* Before the fix there is no separate walk of the model; the plan's own
   offsets are the same numbers (offset 0 is the model's start). */
const railOffsets = A.railOffsets || ((rows) => {
  const plan = railWindowPlan(rows, 0, 0);
  return { offsets: plan.offsets, total: plan.total };
});

const drawn = () => walk(list).filter((r) => r.classes.has("sess-card"))
  .map((r) => r.dataset.name);
const fleet = (prefix, n) => Array.from({ length: n }, (_, i) => ({
  name: `${prefix}${i}`, status: "idle", profile: "nc", parent: null,
}));
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const resetWindow = () => {
  list._railRows = null;
  list._railWindowPlan = null;
  list._railWindowSpan = null;
  list._sessionsSignature = null;
  list._sessionsSigSeq = null;
  list.scrollTop = 0;
  list.kids = [];
};

(async () => {
  /* ---- 1. a quiet poll leaves the window on the list -------------------
     The reported symptom. A poll that changes nothing draws nothing
     (`rows` is empty for it), so the pass has no window of its own -- but it
     has also not touched the list, and the window it found there is the only
     thing the scroll handler reads. */
  resetWindow();
  served = { sessions: fleet("f", 800) };
  await refresh();
  const built = list._railWindowPlan;
  assert.ok(built, "the first pass over a fleet-sized rail is windowed");
  assert.strictEqual(list._railRows.length, 800, "and the model is on the list");

  await refresh();                        // same fleet: nothing changed
  check("a poll that changed nothing leaves the window on the list",
        !!list._railWindowPlan, true);
  check("...and the model with it, which is the scroll handler's input",
        Array.isArray(list._railRows) ? list._railRows.length : null, 800);

  /* ---- 2. ...so scrolling after it draws the region scrolled to --------
     End to end: the scroll event, the deferred handler, the forced pass, the
     slice that comes out. With the window cleared above, the handler bails on
     its first line and the rail below the last drawn slice stays blank. */
  check("the list has a scroll handler", scrollListeners.length > 0, true);
  const model = list._railRows;
  const modelPx = railOffsets(model);
  const lastTop = Math.max(0, modelPx.total - listViewport);
  // Middle, then the end, then back up past where the fleet was first drawn,
  // then the top: the operator's report was that scrolling in either
  // direction drew nothing.
  const stops = [
    { at: Math.round(lastTop / 2), what: "the middle" },
    { at: lastTop, what: "the end" },
    { at: 5000, what: "back up" },
    { at: 0, what: "the top" },
  ];
  const nameNear = (i) => {
    for (let k = i; k < Math.min(model.length, i + 5); k++) {
      if (model[k] && model[k].session) return model[k].session.name;
    }
    for (let k = i - 1; k >= Math.max(0, i - 5); k--) {
      if (model[k] && model[k].session) return model[k].session.name;
    }
    return null;
  };
  for (const stop of stops) {
    list.scrollTop = stop.at;
    scrollListeners[0]();
    await wait(250);                      // past the 80 ms debounce and a pass
    const after = list._railWindowPlan;
    check(`a scroll to ${stop.what} after a quiet poll redraws a slice`,
          !!after, true);
    if (!after) continue;
    let row = 0;
    while (row < model.length - 1 && after.offsets[row + 1] <= stop.at) row += 1;
    check(`${stop.what}: the slice starts at or above the scroll position`,
          after.offsets[after.first] <= stop.at, true);
    check(`${stop.what}: and covers the viewport below it`,
          after.offsets[after.last] >= Math.min(stop.at + listViewport, after.total),
          true);
    check(`${stop.what}: it draws a slice of the fleet, not all of it`,
          drawn().length > 0 && drawn().length < model.length, true);
    check(`${stop.what}: the row under the reader is one of the drawn ones`,
          drawn().includes(nameNear(row)), true);
  }

  /* ---- 3. a forced rebuild a press held off is still owed --------------
     The scroll asks for one rebuild. If a press holds the rows when it
     arrives, that pass cannot draw -- and the flag that asked for it is the
     only reason the next pass would. Spending it here loses the scroll. */
  A.held(true);
  refresh._forceRebuild = true;
  await refresh();
  check("a forced rebuild a press held off asks again when the press ends",
        refresh._forceRebuild, true);
  A.held(false);
  refresh._forceRebuild = false;

  /* ---- 4. the height a row is assumed to have does not follow the reader
     The plan's total and every unmeasured row's offset come from this one
     figure. Built as a running mean of every measurement, it re-weights
     itself by whichever region the reader scrolled into, so the same model
     and the same position plan differently before and after a scroll to a
     tall region -- a rail that never settles. */
  railRowPx.clear();
  railSeenKeys.clear();
  railSeen.rows = 0;
  railSeen.px = 0;
  railSeen.closed = false;
  const measured = (names, px) => {
    const host = node("ul");
    for (const name of names) {
      heights.set(name, px);
      const li = node("li");
      li.classes.add("sess-card");
      li.dataset.name = name;
      host.appendChild(li);
    }
    railMeasureRows(host);
    return host;
  };
  measured(Array.from({ length: 40 }, (_, i) => `a${i}`), 100);
  const first = railEstimatePx();
  assert.strictEqual(first, 100, "the estimate is the mean of what was measured");
  measured(Array.from({ length: 40 }, (_, i) => `b${i}`), 300);
  check("measuring a tall region elsewhere does not move it",
        railEstimatePx(), first);

  /* ---- 5. the plan is a pure function of the model ---------------------
     Same model, same position, same slice -- including every offset. */
  const rows = fleet("p", 400);
  const one = railWindowPlan(rows, 9000, 900);
  const two = railWindowPlan(rows, 9000, 900);
  check("the same model at the same position plans the same slice",
        [one.first, one.last, one.top, one.total], [two.first, two.last, two.top, two.total]);
  check("...and the same offsets", one.offsets, two.offsets);

  /* ---- 6. the row the reader looks at stays where it is ---------------
     claunch-gpxur: "스크롤바의 위치가 안정되지 못하네. 계속 의도와 다른곳으로
     스크롤 위치가 튀고있어". A rail whose rows below the top are taller than
     the estimate the first slice froze (a region of rows with a second line).
     A scroll into that region draws it, measures it, and every offset in it
     moves; the reader must still land on the row the scrollbar pointed at,
     and the polls that follow must leave that row where it is on screen. */
  resetWindow();
  heights.clear();
  railRowPx.clear();
  railSeenKeys.clear();
  railSeen.rows = 0;
  railSeen.px = 0;
  railSeen.closed = false;
  const tallFleet = fleet("q", 800);
  tallFleet.forEach((s, i) => { if (i >= 100) heights.set(s.name, 136); });
  served = { sessions: tallFleet };
  await refresh();
  assert.ok(list._railWindowPlan, "the tall fleet is windowed");
  assert.strictEqual(railEstimatePx(), 72, "its estimate comes from the short rows at the top");

  /* What the reader sees: the first drawn card on screen and how far below
     the top edge it sits. */
  const onScreen = () => {
    for (const li of walk(list)) {
      if (!(li.tag === "li" && li.dataset.name)) continue;
      const top = nodeTop(li), h = nodeHeight(li);
      if (h > 0 && top + h > 0) return { name: li.dataset.name, at: Math.round(top) };
    }
    return null;
  };
  // Scroll to where the model says row q400 starts.
  const target = 400;
  const planned = railOffsets(list._railRows).offsets[target];
  list.scrollTop = planned;
  scrollListeners[0]();
  await wait(250);
  check("a scroll into an unmeasured region lands on the row the scrollbar pointed at",
        onScreen(), { name: `q${target}`, at: 0 });

  // Polls that change a session rebuild the slice at the same position.
  const settledView = onScreen();
  const settledTop = list.scrollTop;
  for (let k = 0; k < 3; k++) {
    list._sessionsSignature = null;
    await refresh();
    check(`poll ${k + 1} after the scroll leaves the reader's row where it was`,
          onScreen(), settledView);
  }
  check("...and the scroll position settled after the first measuring pass",
        list.scrollTop, settledTop);

  /* ---- 7. a pass's own restore does not ask for another pass -------------
     The restore moves scrollTop and the browser fires a scroll event for it.
     Planned afresh against the just-measured model, the slice's ends moved by
     a row, so the handler asked for a rebuild -- and that pass moved the reader
     again. Inside the drawn slice, the scroll handler must not ask. */
  const planBefore = list._railWindowPlan;
  scrollListeners[0]();
  await wait(250);
  check("a scroll event at the settled position draws nothing new",
        list._railWindowPlan === planBefore, true);
  // A small move inside the slice is not a reason either.
  list.scrollTop = settledTop + 120;
  scrollListeners[0]();
  await wait(250);
  check("a scroll of 120px inside the slice draws nothing new",
        list._railWindowPlan === planBefore, true);
  list.scrollTop = settledTop;

  /* ---- 7b. the drawn rows are laid out for real ------------------------
     A row `content-visibility: auto` has not rendered measures as its 64 px
     placeholder; the window's own rows must not be skipped. */
  check("a windowed list carries the class that lays its rows out",
        list.classList.contains("rail-windowed"), true);
  const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");
  check("...and the stylesheet turns the skip off under it",
        /#session-list\.rail-windowed li\[data-name\]\s*\{[^}]*content-visibility:\s*visible/
          .test(css), true);

  /* ---- 8. a whole draw still takes the window off ---------------------
     The other half: the window must not outlive the rail it describes. A
     filter that cuts the rail below the window's floor draws it whole, and
     the scroll handler and the reveal path must not be left reading a model
     the pass no longer draws. */
  resetWindow();
  served = { sessions: fleet("s", 800) };
  await refresh();
  assert.ok(list._railWindowPlan, "the fleet-sized rail is windowed");
  list._sessionsSignature = null;
  served = { sessions: fleet("t", 40) };
  await refresh();
  assert.strictEqual(list._railWindowPlan, null,
    "a rail drawn whole leaves no window behind");
  assert.strictEqual(list._railRows, null, "and no model either");
  check("and the windowed class comes off with it",
        list.classList.contains("rail-windowed"), false);

  /* ---- 9. the helpers the page keeps are the ones under test ---------- */
  assert.strictEqual(typeof railClearWindow, "function",
    "the pass takes the window off through one named decision");

  if (failures) {
    console.error(`railwindow_regress_check: ${failures} failure(s)`);
    process.exit(1);
  }
  console.log("railwindow_regress_check ok");
})().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
