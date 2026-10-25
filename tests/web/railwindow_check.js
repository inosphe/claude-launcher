/* The rail draws the visible slice, not the whole fleet (claunch-thuwx).

   `content-visibility: auto` (style.css) already skips the layout of rows off
   screen, and the payload already arrives as changed rows only -- yet a pass
   over 792 rows still built, signed and decorated every row: longest task
   51-67 ms with the polls stopped, of it style 29 ms. That cost is per row,
   and no transport diff can take it out, so the pass now draws the rows
   around the viewport and stands two spacer rows in for the rest.

   Held here:

   * the plan over a fleet-sized model draws a slice bounded by the viewport,
     keeps every row's offset (the reveal path scrolls by index), and its
     total equals the model's height;
   * heights learned from earlier passes move the offsets (a measured row is
     not the estimate), so the spacers converge instead of drifting;
   * the shipped pass skips out-of-window rows while still counting them into
     their group, records the model on the list for the reveal and scroll
     paths, draws exactly two spacers, and the scroll handler asks for one
     rebuild -- and only when the slice no longer covers the reader;
   * the spacer keeps no padding or border of its own (a CSS stub would add
     them to the height it was given). */
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

/* ---- the window's constants and functions, sliced out ------------------ */
const constSrc = ["RAIL_WINDOW_MIN_ROWS", "RAIL_WINDOW_OVERSCAN_PX",
                  "RAIL_ROW_EST_PX"]
  .map((n) => {
    const m = src.match(new RegExp(`const ${n} = [^\\n]+`));
    if (!m) throw new Error("missing const " + n);
    return m[0];
  })
  .join("\n");

const ctx = {};
new Function("exports", "document",
  constSrc + "\n" + [
    "const railRowPx = new Map();",
    "const railSeen = { rows: 0, px: 0, closed: false };",
    "const railSeenKeys = new Set();",
    "const RAIL_ESTIMATE_SAMPLE = 32;",
    // The rail's densities: the height model samples each one apart, so the
    // estimate a row is planned with depends on which card it is drawn as.
    "const railSeenByMode = new Map();",
    slice("railSeenFor"),
    slice("railRowMode"),
    slice("railEstimatePx"),
    slice("railRowKey"),
    slice("railRowPxOf"),
    slice("railOffsets"),
    slice("railWindowPlan"),
    slice("railSpacerRow"),
    slice("railMeasureRows"),
  ].join("\n") + "\nexports.api = { railWindowPlan, railSpacerRow, railRowKey,"
  + " railRowPxOf, railRowPx, railSeen, railSeenKeys, railEstimatePx,"
  + " railSeenFor, railSeenByMode,"
  + " railOffsets, railMeasureRows, RAIL_WINDOW_MIN_ROWS,"
  + " RAIL_ROW_EST_PX, RAIL_WINDOW_OVERSCAN_PX, RAIL_ESTIMATE_SAMPLE };")(
  ctx,
  // Just enough element for a spacer.
  { createElement: (tag) => ({
      tag, className: "", style: {},
      setAttribute(k, v) { this[k] = v; },
    }) }
);
const { railWindowPlan, railSpacerRow, railRowKey, railRowPxOf, railRowPx,
        railSeen, railSeenKeys, railEstimatePx, railOffsets, railMeasureRows,
        RAIL_WINDOW_MIN_ROWS, RAIL_ROW_EST_PX, RAIL_WINDOW_OVERSCAN_PX,
        RAIL_ESTIMATE_SAMPLE } = ctx.api;

const session = (name) => ({ type: "session", session: { name } });
const fleet = (n) => Array.from({ length: n }, (_, i) => session(`s${i}`));

/* ---- 1. the plan within the viewport ----------------------------------- */
{
  const rows = fleet(800);
  const viewport = 900;
  const scrollTop = 4000;
  const plan = railWindowPlan(rows, scrollTop, viewport);
  const drawn = plan.last - plan.first;

  assert.ok(drawn > 0, "something is drawn");
  assert.ok(drawn < rows.length / 4,
    `a viewport's slice is a fraction of the fleet (drew ${drawn} of ${rows.length})`);
  assert.ok(plan.top <= scrollTop,
    "the slice starts above the reader's position");
  const drawnHeight = plan.offsets[plan.last] - plan.offsets[plan.first];
  assert.ok(plan.offsets[plan.first] <= scrollTop
    && plan.offsets[plan.last] >= scrollTop + viewport,
    "the slice covers the viewport");
  assert.ok(plan.total === rows.length * RAIL_ROW_EST_PX,
    `total height is every row's estimate (${plan.total})`);
  assert.ok(drawnHeight > 0);

  // Every row has an offset and they never go backwards.
  for (let i = 1; i <= rows.length; i++) {
    assert.ok(plan.offsets[i] >= plan.offsets[i - 1], "offsets are monotone");
  }

  // The end of the list is reachable: a scroll to the bottom draws the last
  // rows instead of an empty window past the model.
  const end = railWindowPlan(rows, plan.total - viewport, viewport);
  assert.ok(end.last === rows.length, `the bottom slice ends at the model (${end.last})`);
  assert.ok(end.offsets[end.last] === plan.total, "and stops at the total");

  // Scrolling to the very top draws from the first row.
  const start = railWindowPlan(rows, 0, viewport);
  assert.strictEqual(start.first, 0, "the top slice starts at row 0");
  assert.strictEqual(start.top, 0, "with nothing above it");
}

/* ---- 2. measured heights move the offsets ------------------------------ */
{
  const rows = fleet(300);
  const before = railWindowPlan(rows, 6000, 900);
  railRowPx.set(railRowKey(rows[0]), 200);   // one row that formats tall
  const after = railWindowPlan(rows, 6000, 900);
  assert.strictEqual(after.total, before.total + (200 - RAIL_ROW_EST_PX),
    "a measured row changes the model's height by the difference");
  assert.strictEqual(after.offsets[1], 200,
    "and its offset is the measured height, not the estimate");
  assert.strictEqual(railRowPxOf(rows[0]), 200);
  assert.strictEqual(railRowPxOf(rows[1]), RAIL_ROW_EST_PX,
    "an unseen row keeps the estimate");
  // A group row is not a zero-height row: its heading is drawn too.
  assert.strictEqual(railRowPxOf({ type: "group", level: 0, group: "mesh", value: "m" }),
    RAIL_ROW_EST_PX);
  // `ungroup` carries no node of its own.
  assert.strictEqual(railRowPxOf({ type: "ungroup" }), 0);
  railRowPx.clear();

  // A row never seen is assumed to be what the window has measured so far:
  // on a rail of rich rows the 64 px figure understates every one of them and
  // the plan's total grows at each pass (a scrollbar that keeps lengthening).
  assert.strictEqual(railEstimatePx(), RAIL_ROW_EST_PX,
    "with almost nothing measured the estimate stays the stylesheet's figure");
  railSeen.rows = 10;
  railSeen.px = 1500;
  assert.strictEqual(railEstimatePx(), 150,
    "once rows have been measured it is their mean");
  assert.strictEqual(railRowPxOf({ type: "session", session: { name: "unseen" } }), 150);
  railSeen.rows = 0;
  railSeen.px = 0;
  railSeen.closed = false;
}

/* ---- 2b. what feeds the estimate, and when it stops moving -------------
   claunch-thuwx.1: summed over every measurement, the mean re-weighted itself
   by whichever rows were on screen, so the same model at the same scrollTop
   planned differently before and after a scroll to a tall region. */
{
  const host = (names, px) => ({
    querySelectorAll: (sel) => (sel === "li[data-name]"
      ? names.map((name) => ({
          dataset: { name },
          getBoundingClientRect: () => ({ height: px }),
        }))
      : []),
  });
  railRowPx.clear();
  railSeenKeys.clear();
  railSeen.rows = 0;
  railSeen.px = 0;
  railSeen.closed = false;

  railMeasureRows(host(["m0", "m1"], 100));
  railMeasureRows(host(["m0", "m1"], 500));   // the same rows, measured taller
  assert.strictEqual(railSeen.rows, 2,
    "a row feeds the estimate once, however often it is measured");
  assert.strictEqual(railSeen.px, 200, "at the height it was first measured");

  // One pass that draws more rows than the sample holds fills it to the cap.
  railMeasureRows(host(Array.from({ length: RAIL_ESTIMATE_SAMPLE + 10 },
    (_, i) => `k${i}`), 100));
  assert.strictEqual(railSeen.rows, RAIL_ESTIMATE_SAMPLE,
    "the sample is full and no further row joins it");
  assert.strictEqual(railSeen.closed, true, "and it is closed");
  const frozen = railEstimatePx();
  railMeasureRows(host(["z0"], 900));
  assert.strictEqual(railEstimatePx(), frozen,
    "so a tall region measured elsewhere cannot move it");
  assert.strictEqual(railRowPxOf({ type: "session", session: { name: "z0" } }), 900,
    "while that row keeps its own measured height");

  railRowPx.clear();
  railSeenKeys.clear();
  railSeen.rows = 0;
  railSeen.px = 0;
  railSeen.closed = false;

  // claunch-gpxur: the slice at the top of a rail draws about two dozen rows,
  // fewer than the sample holds. The rows of the next slice joining it moved
  // every unmeasured offset at once, so the sample closes with the first pass
  // that measured enough rows for the estimate to be used at all.
  railMeasureRows(host(Array.from({ length: 24 }, (_, i) => `t${i}`), 72));
  assert.strictEqual(railEstimatePx(), 72, "the first pass's rows set the estimate");
  railMeasureRows(host(Array.from({ length: 24 }, (_, i) => `u${i}`), 136));
  assert.strictEqual(railEstimatePx(), 72,
    "and the next pass's taller rows do not move it, though the sample is not full");
  railRowPx.clear();
  railSeenKeys.clear();
  railSeen.rows = 0;
  railSeen.px = 0;
  railSeen.closed = false;
}

/* ---- 3. the spacer carries no chrome ----------------------------------- */
{
  const li = railSpacerRow(1234);
  assert.strictEqual(li.className, "rail-gap");
  assert.strictEqual(li.style.height, "1234px");
  assert.strictEqual(li["aria-hidden"], "true");
  assert.strictEqual(li.dataset, undefined);
  assert.ok(/#session-list li\.rail-gap\s*\{[^}]*padding: 0/.test(css),
    "the shipped stylesheet strips the row padding off a spacer");
  assert.ok(/#session-list li\.rail-gap\s*\{[^}]*border: 0/.test(css),
    "and its border");
}

/* ---- 4. the shipped pass holds the slice ------------------------------- */
{
  const start = src.indexOf("async function refreshSessions(");
  assert.ok(start > 0, "refreshSessions is in app.js");
  let depth = 0, end = -1;
  for (let j = src.indexOf(") {", start) + 2; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) { end = j + 1; break; } }
  }
  const body = src.slice(start, end);

  assert.ok(/const planRows = typeof railWindowForPass === "function"\s*\n?\s*\? rows\.filter\(drawnByFilter\)/
    .test(body), "the plan is over the rows the filter admits");
  assert.ok(/railWindowForPass\(list, planRows, keptScrollTop, railAnchorTaken\)/.test(body),
    "the pass asks the window helper for its slice, at the position it took before the teardown");
  // claunch-gpxur: the row the reader looks at is read off the page before the
  // pass moves anything, and put back once everything that adds height has run.
  assert.ok(body.indexOf("railTakeAnchor(list)") < body.indexOf("const inPlace = rebuild"),
    "the anchor is taken before the list is touched");
  assert.ok(body.indexOf("applyBriefingCards();") < body.indexOf("railMeasureRows(list);")
    && body.indexOf("railMeasureRows(list);") < body.indexOf("railRestoreAnchor(list, railAnchor)"),
    "the rows are measured after the painters, and the anchor restored last");
  const chooser = slice("railWindowForPass");
  assert.ok(/rows\.length < RAIL_WINDOW_MIN_ROWS/.test(chooser),
    "a short rail is drawn whole");
  assert.ok(/clientHeight/.test(chooser), "and so is a stub with no viewport");
  assert.ok(/railFocusedCardName/.test(chooser),
    "the focused card is kept inside the slice");
  assert.ok(/planAt < railWin\.first \|\| planAt >= railWin\.last/.test(body),
    "rows outside the slice are not built");
  assert.ok(/planAt \+= 1;/.test(body),
    "the plan's index advances over the rows the filter admits, not over every row");
  // A group's size is counted from the model: a windowed pass draws a
  // fraction of the group it heads.
  assert.ok(/const groupSizes = new Map\(\)/.test(body),
    "the group sizes are counted from the model");
  assert.ok(/box\.textContent = String\(size\.n\)/.test(body),
    "a drawn heading shows its group's whole size");
  // The groups that enclose the slice are drawn at its top, with their height
  // taken out of the top spacer, so the rows under them keep their container.
  assert.ok(/const hoistedGroups = \[\]/.test(body),
    "the enclosing groups are found");
  assert.ok(/drawGroupRow\(row\)/.test(body) && /const drawGroupRow = \(row\) =>/.test(body),
    "a heading is drawn by the same code in the slice and above it");
  assert.ok(/railWin\.top - hoistedPx/.test(body),
    "the hoisted headings' height comes out of the top spacer");
  assert.ok(/railSpacerRow\(railWin\.top\)/.test(body) === false,
    "the top spacer is no longer the slice's whole offset");
  assert.ok(/railWin\.total - \(railWin\.offsets\[railWin\.last\] \|\| 0\)/.test(body),
    "and so do the rows below it");
  assert.ok(/list\._railRows = planRows;/.test(body),
    "the plan's model is kept on the list for the reveal path");
  assert.ok(/railMeasureRows\(list\)/.test(body),
    "what was drawn is measured for the next plan");

  // The spacers are squared against the list as it finally stands, after the
  // in-place pass has taken the last pass's tail out of it: a total that
  // misses high leaves the last rows past the end of the scroll.
  assert.ok(/const total = railOffsets\(planRows\)\.total;/.test(body)
    && /Math\.round\(total - list\.scrollHeight\)/.test(body),
    "the spacers are squared against the list's height");
  assert.ok(/if \(railTailFix\) railTailFix\(\);/.test(body),
    "and that happens once the pass has finished");
  assert.ok(body.indexOf("box.removeChild(box.childNodes[used])")
    < body.indexOf("if (railTailFix) railTailFix();"),
    "after the in-place cleanup, not before it");

  // The reveal path can scroll to a row that is not drawn yet.
  const reveal = slice("revealSessionListRow");
  assert.ok(/list\._railRows/.test(reveal),
    "reveal falls back to the model's offsets");
  assert.ok(/railWindowPlan\(model, 0, 0\)\.offsets/.test(reveal),
    "read off the model as it stands, not off the last pass's stored plan");
  assert.ok(/refreshSessions\._forceRebuild = true/.test(reveal),
    "and asks for the pass that will draw it");
  assert.ok(/const again = \(\) =>/.test(reveal) && /rounds > 2/.test(reveal),
    "aiming again once the list is at the model's real height, a bounded number of times");

  // The scroll handler redraws only when the slice stops covering the view,
  // and it asks for a rebuild rather than running a pass itself.
  const wire = slice("railWireWindowScroll");
  assert.ok(/if \(railSliceCovers\(box\)\) return;/.test(wire),
    "a scroll inside the drawn slice draws nothing");
  assert.ok(/refreshSessions\._forceRebuild = true/.test(wire),
    "a scroll past it asks for one rebuild");
  // The pass runs from the deferred timer, never from the scroll event
  // itself: a scroll fires dozens of times a second.
  assert.ok(wire.indexOf("setTimeout") < wire.indexOf("refreshSessions({ cached: true })"),
    "the handler does not run a pass from the scroll event itself");
  // And it draws what is in hand: a wheel flick must not queue a request.
  assert.ok(/refreshSessions\(\{ cached: true \}\)/.test(wire),
    "the scroll rebuild reads the cache instead of polling");

  // The flag is what turns the rebuild on, and it is spent once.
  assert.ok(/const changed = windowForced \|\| signature !== list\._sessionsSignature;/
    .test(body), "the forced rebuild opens the same gate a changed fleet does");
  assert.ok(/refreshSessions\._forceRebuild = false;/.test(body),
    "and is cleared in the pass that uses it");
}

console.log("railwindow_check ok");
