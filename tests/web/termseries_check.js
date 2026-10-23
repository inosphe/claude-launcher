/* The token timeline at the foot of the overlay (claunch-0af4e), run against
   the real functions from app.js.

   What has to hold:
     drawn      -- one stacked area and line per part (cached input, write,
                   input), a dashed mark per compaction at its time, a solid
                   diamond-headed mark per profile swap (claunch-0af4e.1.1),
                   and the zoomed chart adds the height axis' maximum, the
                   axis, legend and readout.
     read       -- /api/sessions/<name>/token-series is asked out of the batch,
                   once per TTL and window; the hover view asks for window=full
                   and draws the capped series until it arrives.
     nothing    -- an unavailable answer or one without points draws nothing.
     overlay    -- one chart for the whole overlay (the same session series
                   under both blocks drew it twice), hovering it grows it to
                   the zoomed view and leaving restores the small one.
     release    -- the hover also ends on a pointer move outside every chart,
                   so a chart replaced by a repaint (no mouseleave) or moved
                   out from under the pointer by the zoom cannot keep the
                   zoomed view on (claunch-a27zc). */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8");

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}
const constStart = src.indexOf("const TERM_SERIES_TTL_MS");
const constEnd = src.indexOf("function termSeriesRead(");
if (constStart < 0 || constEnd < 0) throw new Error("cannot locate the series tables");
const consts = src.slice(constStart, constEnd);

function node(tag, cls, text) {
  const n = {
    tag, className: cls || "", textContent: text ?? "", title: "", kids: [], attrs: {}, handlers: {},
    style: { setProperty() {} },
    setAttribute(k, v) { n.attrs[k] = String(v); },
    addEventListener(ev, fn) { n.handlers[ev] = fn; },
    getBoundingClientRect() { return { left: 0, width: 100 }; },
    append(...cs) { n.kids.push(...cs); },
    appendChild(c) { n.kids.push(c); return c; },
    replaceChildren(...cs) { n.kids = cs; },
    classList: {
      toggle(c, on) {
        const has = n.className.split(/\s+/).filter(Boolean);
        const i = has.indexOf(c);
        if (on === undefined) on = i < 0;
        if (on && i < 0) has.push(c);
        if (!on && i >= 0) has.splice(i, 1);
        n.className = has.join(" ");
      },
      contains(c) { return n.className.split(/\s+/).includes(c); },
    },
  };
  return n;
}
const docListeners = [];
const document = {
  /* An SVG element's className is a read-only SVGAnimatedString: assigning
     it throws under "use strict", as app.js runs (claunch-a27zc.1). */
  createElementNS: (_ns, tag) => {
    const n = node(tag);
    delete n.className;
    Object.defineProperty(n, "className", { get: () => ({ baseVal: n.attrs.class || "" }) });
    return n;
  },
  addEventListener(ev, fn, capture) { docListeners.push([ev, fn, capture]); },
  removeEventListener(ev, fn, capture) {
    const i = docListeners.findIndex(([e, f, c]) => e === ev && f === fn && c === capture);
    if (i >= 0) docListeners.splice(i, 1);
  },
};
/* What lies under the pointer is a hit test; the event's own target is the
   overlay, as a real browser aims the move right after a repaint. */
let underPointer = null;
document.elementFromPoint = () => underPointer;
const overlayNode = { closest: () => null };
const moveOn = (inChart) => {
  underPointer = { closest: (sel) => (inChart && sel === ".term-series" ? {} : null) };
  for (const [ev, fn] of docListeners.slice()) {
    if (ev === "mousemove") fn({ target: overlayNode, clientX: 5, clientY: 5 });
  }
};
const calls = [];
let reply = null;
const api = async (p, opts) => { calls.push([p, opts]); return reply; };
let renders = 0;
const renderTermTps = () => { renders++; };

const ctx = {};
new Function(
  "exports", "el", "document", "api", "renderTermTps", "fmtAge", "usageShort",
  '"use strict";\n' + consts + ["termSeriesRead", "termSeriesData", "termSeriesTotal", "termSeriesCumulative", "termSeriesChart",
    "termSeriesPointerCheck", "termSeriesRelease", "termSeriesMaker"].map(slice).join("\n") + `
exports.read = termSeriesRead;
exports.data = termSeriesData;
exports.chart = termSeriesChart;
exports.cumulative = termSeriesCumulative;
exports.maker = termSeriesMaker;
exports.reads = termSeriesReads;
exports.hover = (v) => { termSeriesHover = v; };
exports.hovered = () => termSeriesHover;
`)(ctx, node, document, api, renderTermTps,
  (s) => `${Math.floor(s / 60)}m`, (n) => String(n));

let failures = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  if (a !== e) { failures++; console.error(`FAIL ${label}\n  got      ${a}\n  expected ${e}`); }
}
const settle = () => new Promise((r) => setTimeout(r, 0));
const answer = (data, ok = true) => ({ ok, status: ok ? 200 : 400, json: async () => data });
const series = (win, start = 1000) => ({
  available: true, window: win, start, until: start + 400, bucket_s: 100, requests: 3,
  points: [0, 1, 2, 3].map((i) => ({ t: start + i * 100, input: 1, cache_read: 10 * (i + 1), cache_write: 2 })),
  compactions: [start + 200],
  swaps: [{ at: start + 300, from: "opencode", to: "opencode2" }],
});

(async () => {
  // The drawing.
  const small = ctx.chart(series("cap"), false);
  const svg = small.kids[0];
  check("small chart: an area and a line per part, then one compaction mark",
        svg.kids.map((k) => k.attrs.class),
        ["term-series-area cache_write", "term-series-line cache_write",
         "term-series-area input", "term-series-line input",
         "term-series-area cache_read", "term-series-line cache_read", "term-series-compaction",
         "term-series-swap"]);
  check("the compaction sits halfway along a 120px chart", svg.kids[6].attrs.x1, "60.0");
  const swap = svg.kids[7];
  check("the swap sits three quarters along, as a stem, a diamond head and a name",
        [swap.kids.map((k) => k.tag), swap.kids[0].attrs.x1, swap.kids[0].attrs.y2],
        [["line", "polygon", "title"], "90.0", "26"]);
  check("the swap mark is solid where the compaction mark is dashed (own class, not the compaction's)",
        [swap.attrs.class, swap.kids[0].attrs.class], ["term-series-swap", undefined]);
  check("the swap names the profiles and when", swap.kids[2].textContent,
        "profile swap opencode \u2192 opencode2, 1m ago");
  check("the small chart draws no height label", svg.kids.some((k) => k.attrs.class === "term-series-ymax"), false);
  check("the small chart has no axis or legend", small.kids.length, 1);
  const big = ctx.chart(series("full"), true);
  check("zoomed chart: svg, axis, legend, readout",
        big.kids.map((k) => k.attrs.class || k.className),
        ["term-series-chart", "term-series-axis", "term-series-legend", "term-series-readout"]);
  check("the legend names the parts, the compaction and the swap",
        big.kids[2].kids.map((k) => k.textContent),
        ["cached input", "input", "write", "compaction", "profile swap"]);
  const zsvg = big.kids[0];
  const ymax = zsvg.kids.filter((k) => k.attrs.class === "term-series-ymax");
  check("the zoomed chart labels its height axis with the maximum",
        ymax.map((k) => k.textContent), ["112 (log)"]);
  const rule = zsvg.kids.filter((k) => k.attrs.class === "term-series-ymax-rule");
  check("the maximum has a rule at the top of the plot, level with the stack's top",
        [rule.length, rule[0].attrs.y1], [1, "2.0"]);
  big.kids[0].handlers.mousemove({ clientX: 100 });
  check("the readout names the bucket under the pointer",
        big.kids[3].textContent, "1m ago: in 4 · cached 100 · write 8 (+43 in 1m)");

  // Cumulative: running totals per part, so the stack only ever rises.
  const run = ctx.cumulative(series("cap").points);
  check("each part is a running total from the first bucket",
        run.map((p) => [p.input, p.cache_read, p.cache_write]),
        [[1, 10, 2], [2, 30, 4], [3, 60, 6], [4, 100, 8]]);
  check("the stack's top never falls",
        run.every((p, i) => i === 0 || (p.input + p.cache_read + p.cache_write) >=
                            (run[i - 1].input + run[i - 1].cache_read + run[i - 1].cache_write)), true);
  const edge = (svgEl, part) => svgEl.kids.filter((k) => k.attrs.class === `term-series-line ${part}`)[0]
    .attrs.points.split(" ").map((pt) => Number(pt.split(",")[1]));
  const tops = (svgEl) => edge(svgEl, "cache_read");
  const ys = tops(svg);
  check("the drawn top edge rises left to right (smaller y is higher)",
        ys.every((y, i) => i === 0 || y <= ys[i - 1]), true);
  check("the last point is the window's whole sum at the chart's top edge",
        ys[ys.length - 1] < ys[0] && ys[ys.length - 1] === 2, true);
  // Log height axis: write at the bottom, input above it, cached input on top.
  const logY = (v, h = 26, peak = 112) => Number((h - 1 - (v > 1 ? Math.log(v) / Math.log(peak) : 0) * (h - 3)).toFixed(1));
  check("the stack's boundaries are on a log axis, last bucket: write 8, +input 12, +cached 112",
        [edge(svg, "cache_write"), edge(svg, "input"), edge(svg, "cache_read")].map((e) => e[e.length - 1]),
        [logY(8), logY(12), logY(112)]);
  check("write is lowest, input above it, cached input highest",
        logY(8) > logY(12) && logY(12) > logY(112), true);
  const huge = ctx.chart({ ...series("full"), points: [0, 1, 2, 3].map((i) => ({
    t: 1000 + i * 100, input: 1e5 * (i + 1), cache_read: 2e8 * (i + 1), cache_write: 3e3 })) }, true);
  check("the zoomed log axis has a rule and a label at 1k and 1M, none at 1B above the maximum",
        huge.kids[0].kids.filter((k) => k.attrs.class === "term-series-ytick").map((k) => k.textContent),
        ["1k", "1M"]);
  check("a small axis has no decade rules (112 is under 1k)",
        big.kids[0].kids.filter((k) => k.attrs.class === "term-series-ytick").length, 0);
  check("the axis row has the span start and now only; the height label is in the chart",
        big.kids[1].kids.map((k) => k.textContent), ["6m ago", "now"]);
  check("the tooltip counts the whole window and the swap",
        [small.title.startsWith("112 tokens sent over"), small.title.includes("1 profile swap")],
        [true, true]);
  const bare = ctx.chart({ ...series("cap"), swaps: undefined, compactions: [] }, false);
  check("a series without swap records draws no swap mark",
        bare.kids[0].kids.some((k) => k.attrs.class === "term-series-swap"), false);

  // Reading.
  reply = answer(series("cap"));
  check("nothing before the first answer", ctx.data("s1", false), null);
  check("asked out of the batch with the capped window",
        calls.map(([p, o]) => [p, o.noBatch]), [["/api/sessions/s1/token-series?window=cap", true]]);
  ctx.data("s1", false);
  check("a read in flight is not asked again", calls.length, 1);
  await settle(); await settle();
  check("the capped series is drawn once it arrived", ctx.data("s1", false).window, "cap");
  check("the answer repaints the overlay", renders >= 1, true);
  reply = answer(series("full"));
  check("hovering draws the capped series until the full one arrives", ctx.data("s1", true).window, "cap");
  check("and asks for the whole conversation",
        calls.map(([p]) => p).slice(-1), ["/api/sessions/s1/token-series?window=full"]);
  await settle(); await settle();
  check("then draws the full one", ctx.data("s1", true).window, "full");
  ctx.reads.full.at -= 60000;
  ctx.data("s1", true);
  check("a stale full read is asked again", calls.length, 3);

  // Another session starts afresh; an unavailable answer draws nothing.
  reply = answer({ available: false, reason: "no transcript" });
  ctx.data("s2", false);
  await settle(); await settle();
  check("an unavailable answer draws nothing", ctx.data("s2", false), null);

  // Hover: the chart grows under the pointer.
  ctx.hover(false);
  const make = ctx.maker(series("cap"), series("full"), false);
  const b = make("series");
  check("unhovered, the chart is small and capped", b.className, "term-series");
  const before = renders;
  b.handlers.mouseenter();
  check("entering the chart names it as the hovered one and repaints",
        [ctx.hovered(), renders - before], ["series", 1]);
  b.handlers.mouseenter();
  check("a second enter on it changes nothing", renders - before, 1);
  check("the hover watches pointer moves on the document",
        docListeners.map(([ev, , c]) => [ev, c]), [["mousemove", true]]);
  const repaint = ctx.maker(series("cap"), series("full"), "series");
  const grown = repaint("series");
  check("the repaint grows the hovered chart, with the whole conversation",
        [grown.className, grown.kids.length], ["term-series zoom", 4]);
  b.handlers.mouseleave();
  check("leaving switches it off and repaints", [ctx.hovered(), renders - before], [false, 2]);
  check("and stops watching", docListeners.length, 0);

  // The chart the pointer entered is replaced by a repaint: the new one never
  // got a mouseenter, so its mouseleave cannot be relied on.
  const c = make("series");
  c.handlers.mouseenter();
  const zoomed = ctx.maker(series("cap"), series("full"), "series")("series");
  const mid = renders;
  moveOn(true);
  check("a move over a chart keeps the zoomed view", [ctx.hovered(), renders - mid], ["series", 0]);
  moveOn(false);
  check("a move outside every chart ends it and repaints",
        [ctx.hovered(), renders - mid, docListeners.length], [false, 1, 0]);
  c.handlers.mouseleave();
  zoomed.handlers.mouseleave();
  check("a late mouseleave after that changes nothing", renders - mid, 1);

  if (failures) { console.error(`${failures} failure(s)`); process.exit(1); }
  console.log("termseries_check: ok");
})();
