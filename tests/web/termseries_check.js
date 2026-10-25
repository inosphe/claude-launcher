/* The token timeline at the foot of the overlay (claunch-0af4e), run against
   the real functions from app.js.

   What has to hold:
     drawn      -- one stacked area and line per part (cached input, write,
                   input), one value per request on an axis of request order
                   (claunch-0af4e.1.2), a dashed mark per compaction on the
                   step down it causes, a solid diamond-headed mark per
                   profile swap (claunch-0af4e.1.1), and the zoomed chart adds
                   the height axis' maximum, the axis (request numbers with
                   their times), legend and readout.
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
  '"use strict";\n' + consts + ["termSeriesRead", "termSeriesData", "termSeriesTotal", "termSeriesIndexAt", "termSeriesScale",
    "termSeriesTicks", "termSeriesTick", "termSeriesChart",
    "termSeriesPointerCheck", "termSeriesRelease", "termSeriesMaker"].map(slice).join("\n") + `
exports.read = termSeriesRead;
exports.data = termSeriesData;
exports.chart = termSeriesChart;
exports.scale = termSeriesScale;
exports.indexAt = termSeriesIndexAt;
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
/* Four requests whose contexts are 13, 33, 10 and 44: the compaction at
   start+150 comes between the second and the third, which is the fall. */
const CONTEXTS = [[1, 10, 2], [1, 30, 2], [1, 5, 4], [2, 40, 2]];
const series = (win, start = 1000) => ({
  available: true, window: win, start, until: start + 400, requests: 4, count: 4,
  points: CONTEXTS.map(([input, cache_read, cache_write], i) =>
    ({ t: start + i * 100, n: i + 1, count: 1, input, cache_read, cache_write })),
  compactions: [start + 150],
  swaps: [{ at: start + 250, from: "opencode", to: "opencode2" }],
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
  check("the compaction sits between request 2 and 3 (a step of 40px each on a 120px chart)",
        svg.kids[6].attrs.x1, "60.0");
  const swap = svg.kids[7];
  check("the swap sits between request 3 and 4, as a stem, a diamond head and a name",
        [swap.kids.map((k) => k.tag), swap.kids[0].attrs.x1, swap.kids[0].attrs.y2],
        [["line", "polygon", "title"], "100.0", "26"]);
  check("the swap mark is solid where the compaction mark is dashed (own class, not the compaction's)",
        [swap.attrs.class, swap.kids[0].attrs.class], ["term-series-swap", undefined]);
  check("the swap names the profiles and when", swap.kids[2].textContent,
        "profile swap opencode \u2192 opencode2, 2m ago");
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
        ymax.map((k) => k.textContent), ["44 max"]);
  const rule = zsvg.kids.filter((k) => k.attrs.class === "term-series-ymax-rule");
  check("the maximum has a rule level with the largest context, among the axis' own rules",
        [rule.some((k) => k.attrs.y1 === "2.0"), rule.every((k) => k.attrs.y1 === k.attrs.y2)], [true, true]);
  /* Two lines: which request, then what it sent (claunch-k5udr.1). One line
     of both ran out of the box and across the terminal under it. */
  const readoutLines = (chart) => chart.kids[3].kids.map((k) => k.textContent);
  check("the readout has two lines", big.kids[3].kids.map((k) => k.className),
        ["term-series-readout-line", "term-series-readout-line"]);
  big.kids[0].handlers.mousemove({ clientX: 100 });
  check("the readout names the request under the pointer, with its time and context",
        readoutLines(big), ["#4 · 1m ago", "44 = in 2 · cached 40 · write 2"]);
  big.kids[0].handlers.mousemove({ clientX: 0 });
  check("the first request is under the left edge", readoutLines(big), ["#1 · 6m ago", "13 = in 1 · cached 10 · write 2"]);
  const grouped = ctx.chart({ ...series("full"), points: series("full").points.map((p) => ({ ...p, count: 5 })) }, true);
  grouped.kids[0].handlers.mousemove({ clientX: 100 });
  check("a point that stands for several requests says so on its own line",
        readoutLines(grouped), ["#4 · 1m ago · max of 5", "44 = in 2 · cached 40 · write 2"]);
  big.kids[0].handlers.mouseleave();
  check("the readout is blank on both lines once the pointer leaves",
        readoutLines(big).map((t) => t.trim()), ["", ""]);

  // The height axis (claunch-nw9ca): a log axis whose middle is 1/(1+sqrt(B))
  // of the top, so 300k of a 1M window sits at half height; ticked at round
  // values near even steps; a line at the compact threshold.
  const big1 = (n) => [{ input: 2, cache_read: n - 2, cache_write: 0 }];
  const sc = (n, limits) => {
    const r = ctx.scale(big1(n), limits);
    return [r.top, r.fixed];
  };
  const ticks = (n, limits) => ctx.scale(big1(n), limits).ticks;
  check("top is the largest request without a window", sc(367e3, null), [367e3, false]);
  check("top is the window, not the largest request", sc(367e3, { top: 1e6 }), [1e6, true]);
  check("a request over the window pushes the top up to it", sc(2e6, { top: 1e6 }), [2e6, false]);
  check("a 1M window is ticked 100k, 150k, 300k, 500k, 700k",
        ticks(1e3, { top: 1e6 }), [100e3, 150e3, 300e3, 500e3, 700e3]);
  check("a 200k window is ticked 15k, 30k, 60k, 100k, 150k",
        ticks(1e3, { top: 2e5 }), [15e3, 30e3, 60e3, 100e3, 150e3]);
  check("a session of a few dozen tokens is ticked in round whole tokens",
        ctx.scale(big1(44), null).ticks, [3, 7.5, 13, 21, 31]);
  const ratioOf = (n, v, limits) => ctx.scale(big1(n), limits).ratio(v);
  check("the middle of a 1M axis is 300k, where a linear axis put 500k",
        ratioOf(5e3, 300e3, { top: 1e6 }).toFixed(4), "0.4999");
  check("500k sits above the middle of a 1M axis",
        ratioOf(5e3, 500e3, { top: 1e6 }).toFixed(3), "0.690");
  check("the axis has its top at the ceiling and zero on the baseline",
        [ratioOf(5e3, 0, { top: 1e6 }), ratioOf(5e3, 1e6, { top: 1e6 })], [0, 1]);
  check("a value over the top is clamped to it", ratioOf(5e3, 1.5e6, { top: 1e6 }), 1);
  check("the largest request changing does not move a position when the window is named",
        [ratioOf(5e3, 3e4, { top: 1e6 }), ratioOf(9e5, 3e4, { top: 1e6 })].map((r) => r.toFixed(6)).every((r, _, a) => r === a[0]), true);
  check("a 44k session sits well above the baseline of a 1M axis",
        ratioOf(44e3, 44e3, { top: 1e6 }).toFixed(3), "0.105");
  const tiny = ctx.scale([{ input: 200, cache_read: 0, cache_write: 0 }, { input: 500, cache_read: 0, cache_write: 0 }], { top: 1e6 });
  check("a session of a few hundred tokens is lifted off the baseline too",
        [tiny.top, tiny.ratio(200).toFixed(4), tiny.ratio(500).toFixed(4)], [1e6, "0.0005", "0.0013"]);
  const sameSize = ctx.scale([{ input: 700, cache_read: 0, cache_write: 0 }], { top: 1e6 });
  check("one request of one size keeps a non-zero axis", [sameSize.top, sameSize.ratio(700).toFixed(4)], [1e6, "0.0018"]);
  // 1.4 is what the request named, and it is worth holding here: on this axis
  // the base is the only knob, and 1.4 is the value that leaves the chart
  // looking linear, which is why the shipped axis uses 5.44 instead.
  const biased = (v, top, b) => Math.log(1 + (b - 1) * (v / top)) / Math.log(b);
  check("the shipped axis puts 300k at half height of a 1M window",
        biased(300e3, 1e6, 5.44).toFixed(4), "0.4999");
  check("base 1.4 would leave 458k near the middle, a linear-looking chart",
        [biased(458e3, 1e6, 1.4).toFixed(3), biased(500e3, 1e6, 1.4).toFixed(3)], ["0.500", "0.542"]);
  check("the middle of the axis is top*(sqrt(B)-1)/(B-1) for the base in use",
        Math.round(1e6 * (Math.sqrt(5.44) - 1) / 4.44), 300086);
  const side = (label, window, compact) => ({ label, window, compact });
  const lines = (limits, n = 367e3) => ctx.scale(big1(n), limits).compact.map((l) => [l.at, l.labels]);
  check("one threshold, one line", lines({ top: 1e6, sides: [side("a", 1e6, 9e5)] }), [[9e5, ["a"]]]);
  check("equal thresholds of the two sides of a borrowed session are one line",
        lines({ top: 1e6, sides: [side("a", 1e6, 9e5), side("b", 1e6, 9e5)] }), [[9e5, ["a", "b"]]]);
  check("different thresholds are two lines, each knowing its side",
        lines({ top: 1e6, sides: [side("a", 1e6, 9e5), side("b", 2e5, 1.5e5)] }), [[9e5, ["a"]], [1.5e5, ["b"]]]);
  check("no threshold, no line", lines({ top: 1e6, sides: [side("a", 1e6, null)] }), []);
  check("no limits at all, no line", lines(null), []);
  const lineAt = (limits, n = 367e3, zoom = false) => {
    const pts = [0, 1].map((i) => ({ t: 1000 + i * 100, n: i + 1, count: 1, input: 2, cache_read: n - 2, cache_write: 0 }));
    return ctx.chart({ ...series("cap"), points: pts, compactions: [], swaps: [], limits }, zoom).kids[0].kids
      .filter((k) => k.attrs.class === "term-series-compact-line");
  };
  const lim = { top: 1e6, sides: [side("a", 1e6, 9e5)] };
  const one = lineAt(lim);
  check("the threshold line is horizontal across the chart at its height",
        [one.length, one[0].attrs.x1, one[0].attrs.x2, one[0].attrs.y1 === one[0].attrs.y2,
         one[0].attrs.y1], [1, "0", "120", true, "3.2"]);
  check("two sides with the same threshold draw one line", lineAt({ ...lim, sides: [side("a", 1e6, 9e5), side("b", 1e6, 9e5)] }).length, 1);
  check("two sides with different thresholds draw two", lineAt({ ...lim, sides: [side("a", 1e6, 9e5), side("b", 2e5, 1.5e5)] }).length, 2);
  check("a session without a threshold draws no line", lineAt({ top: 1e6, sides: [side("a", 1e6, null)] }).length, 0);
  check("the threshold line stays where it was when the largest request grows",
        lineAt(lim, 5e3)[0].attrs.y1, lineAt(lim, 9e5)[0].attrs.y1);
  const zl = ctx.chart({ ...series("full"), points: [0, 1].map((i) => ({ t: 1000 + i * 100, n: i + 1, count: 1, input: 2, cache_read: 4e5, cache_write: 0 })),
    compactions: [], swaps: [], limits: { top: 1e6, sides: [side("work", 1e6, 9e5), side("lender", 5e5, 4.5e5)] } }, true).kids[0].kids;
  check("the zoomed label names the window as the top",
        zl.filter((k) => k.attrs.class === "term-series-ymax").map((k) => k.textContent), ["1M max, context window"]);
  check("each threshold line is labelled with its side when there are two",
        zl.filter((k) => k.attrs.class === "term-series-ytick" && k.textContent.startsWith("compact")).map((k) => k.textContent),
        ["compact 900000 (work)", "compact 450000 (lender)"]);

  // Request order: one value per request, so a compaction is a fall.
  const edge = (svgEl, part) => svgEl.kids.filter((k) => k.attrs.class === `term-series-line ${part}`)[0]
    .attrs.points.split(" ").map((pt) => Number(pt.split(",")[1]));
  const tops = (svgEl) => edge(svgEl, "cache_read");
  const ys = tops(svg);
  check("the top edge has one point per request", ys.length, 4);
  check("the context falls after the compaction (larger y is lower) and then rises again",
        [ys[2] > ys[1], ys[3] < ys[2]], [true, true]);
  check("the largest context is at the chart's top edge", ys[3] === 2 && ys.every((y) => y >= 2), true);
  // The log height axis: write at the bottom, input above it, cached input at
  // the top, each at its own boundary on the axis of the 44-token request.
  const logY = (v, h = 26, top = 44) =>
    Number((h - 1 - (Math.log(1 + 4.44 * (v / top)) / Math.log(5.44)) * (h - 3)).toFixed(1));
  check("the stack's boundaries are on the log axis, last request: write 2, +input 4, +cached 44",
        [edge(svg, "cache_write"), edge(svg, "input"), edge(svg, "cache_read")].map((e) => e[e.length - 1]),
        [logY(2), logY(4), logY(44)]);
  check("the smallest boundary sits above the baseline, the largest context at the top",
        [logY(2), logY(44)], [22.5, 2.0]);
  const huge = ctx.chart({ ...series("full"), points: [0, 1, 2, 3].map((i) => ({
    t: 1000 + i * 100, n: i + 1, count: 1, input: 1e5 * (i + 1), cache_read: 2e8 * (i + 1), cache_write: 3e3 })) }, true);
  check("a large axis is ticked at round values, none above the maximum",
        huge.kids[0].kids.filter((k) => k.attrs.class === "term-series-ytick").map((k) => k.textContent),
        ["60M", "150M", "250M", "400M", "600M"]);
  check("a small axis is ticked in whole tokens too",
        big.kids[0].kids.filter((k) => k.attrs.class === "term-series-ytick").map((k) => k.textContent),
        ["3", "7.5", "13", "21", "31"]);
  check("the axis row names the first, the middle and the last request with their times",
        big.kids[1].kids.map((k) => k.textContent), ["#1 · 6m ago", "#3 · 3m ago", "#4 · 1m ago"]);
  check("the tooltip counts the requests, names the last and largest context and the swap",
        [small.title.startsWith("context of 4 requests over 6m; last 44, largest 44"),
         small.title.includes("1 profile swap"), small.title.includes("1 compaction")],
        [true, true, true]);
  const lone = ctx.chart({ ...series("cap"), points: [series("cap").points[0]], compactions: [], swaps: [] }, true);
  check("a single request is drawn across the chart, its axis names it once",
        [lone.kids[0].kids[1].attrs.points.split(" ").length, lone.kids[1].kids.map((k) => k.textContent)],
        [2, ["#1 · 6m ago"]]);
  const ps = series("cap").points;
  check("a time maps to a place between the requests around it",
        [ctx.indexAt(ps, 500), ctx.indexAt(ps, 1000), ctx.indexAt(ps, 1150), ctx.indexAt(ps, 1250), ctx.indexAt(ps, 9999)],
        [0, 0, 1.5, 2.5, 3]);
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
