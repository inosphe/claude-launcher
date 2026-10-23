/* The overlay's token timeline chart under a real pointer (claunch-a27zc).

   termseries_check.js runs the hover logic on stub nodes; what it cannot show
   is what a browser does with a chart that is rebuilt and resized under the
   pointer. This runs the real termSeries* functions from app.js and the real
   style.css in Chromium, in an overlay laid out as renderTermTps lays it out
   (throughput block, usage block, the one chart), and drives it with real
   mouse input:

   - entering it grows it, and the zoomed chart lies under the pointer;
   - the zoomed chart labels its height axis with the largest context, and a
     profile swap is a solid mark where a compaction is dashed
     (claunch-0af4e.1.1);
   - one value per request: the compaction mark stands on the fall of the
     context between two requests (claunch-0af4e.1.2);
   - small moves inside it repaint nothing;
   - the overlay builds at all: app.js runs under "use strict", where a write
     to an SVG element's read-only className throws, and that throw aborted
     every rebuild and froze the throughput numbers (claunch-a27zc.1);
   - entering and leaving in one jump, with no move inside the zoomed chart,
     still ends the hover. The pointer never entered the rebuilt chart, so
     it gets no mouseleave, and before the fix the overlay stayed zoomed. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const STATIC = path.join(__dirname, "../../src/claude_launcher/web/static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

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

// The page below stands in for renderTermTps; hold it to the same order.
const render = slice("renderTermTps");
assert.equal(render.split("makeChart(").length - 1, 1, "renderTermTps builds one chart, not one per block");
assert.ok(render.includes('makeChart("series")'), "renderTermTps builds the chart by its slot");
assert.ok(/replaceChildren\(\.\.\.\[lastBox, usageBox, chart\]/.test(render),
  "renderTermTps puts the chart under the last block");

const page = `<!doctype html><html><head><style>${css}
body { margin: 0; background: #000; }
#term { position: relative; width: 900px; height: 700px; }
</style></head><body><div id="term"><div id="term-tps-overlay"></div></div><script>
"use strict";  // as app.js runs: an SVG className write throws only then (claunch-a27zc.1)
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}
const fmtAge = (s) => Math.round(s / 60) + "m", usageShort = (n) => String(n);
${src.slice(constStart, constEnd)}
${["termSeriesTotal", "termSeriesIndexAt", "termSeriesScale", "termSeriesChart", "termSeriesPointerCheck", "termSeriesRelease",
    "termSeriesMaker"].map(slice).join("\n")}
/* 30 requests, one every 120 s; the context grows to request 15, is
   compacted at 1800 s (the summary is the next request's context) and grows
   again. */
const series = (win) => ({
  available: true, window: win, start: 0, until: 3600, requests: 30, count: 30,
  compactions: [1800],
  swaps: [{ at: 2400, from: "opencode", to: "opencode2" }],
  points: Array.from({ length: 30 }, (_, i) => ({
    t: i * 120, n: i + 1, count: 1, input: 5, cache_read: (i < 15 ? 50 + i * 10 : 10 + i) * 1000, cache_write: 8000 })),
  /* A 1M window that compacts at 900k on the profile's own account and at
     450k on the lender's. */
  limits: { top: 1e6, sides: [
    { label: "work", window: 1e6, compact: 9e5 }, { label: "lender", window: 1e6, compact: 4.5e5 }] },
});
window.renders = 0;
function renderTermTps() {
  window.renders++;
  const overlay = document.getElementById("term-tps-overlay");
  const makeChart = termSeriesMaker(series("cap"), series("full"), termSeriesHover);
  const lastBox = el("div", "term-tps-last");
  lastBox.appendChild(el("span", "term-tps-big", "42 tok/s"));
  const usageBox = el("div", "term-usage");
  usageBox.appendChild(el("div", "term-usage-row", "claude 5h 12% 7d 30%"));
  overlay.replaceChildren(lastBox, usageBox, makeChart("series"));
}
renderTermTps();
</script></body></html>`;

(async () => {
  const browser = await chromium.launch();
  try {
    for (const [index, slot] of [[0, "series"]]) {
      for (const wiggle of [true, false]) {
        const p = await browser.newPage({ viewport: { width: 900, height: 700 } });
        const errors = [];
        p.on("pageerror", (err) => errors.push(err.message));
        await p.setContent(page);
        const drawn = await p.$$("#term-tps-overlay .term-series");
        assert.deepEqual([drawn.length, errors], [1, []],
          "the overlay draws the chart without an error (a throw here froze the overlay)");
        const box = await drawn[index].boundingBox();
        const x = box.x + box.width / 2, y = box.y + box.height / 2;
        await p.mouse.move(10, 600);
        await p.mouse.move(x, y, { steps: 5 });
        await p.waitForTimeout(100);
        assert.equal(await p.evaluate(() => termSeriesHover), slot, `entering the ${slot} chart hovers it`);
        assert.deepEqual(
          await p.evaluate(() => [...document.querySelectorAll(".term-series")].map((n) => n.classList.contains("zoom"))),
          [true], `the ${slot} chart grows`);
        assert.ok(await p.evaluate(([px, py]) => !!document.elementFromPoint(px, py).closest(".term-series.zoom"), [x, y]),
          `the zoomed ${slot} chart lies under the pointer`);
        if (wiggle) {
          const drawnMarks = await p.evaluate(() => {
            const dash = (sel) => getComputedStyle(document.querySelector(sel)).strokeDasharray;
            const label = document.querySelector(".term-series.zoom .term-series-ymax");
            return {
              max: label && label.textContent, labelInPlot: !!label && label.getBoundingClientRect().width > 0,
              swapDash: dash(".term-series-swap line"), compactionDash: dash(".term-series-compaction"),
              swapStroke: getComputedStyle(document.querySelector(".term-series-swap line")).stroke,
              compactionStroke: getComputedStyle(document.querySelector(".term-series-compaction")).stroke,
            };
          });
          assert.equal(drawnMarks.max, "1000000 max (log, context window)",
            "the zoomed chart's height axis is labelled with the window it tops out at");
          const compact = await p.evaluate(() => {
            const plot = document.querySelector(".term-series.zoom .term-series-chart").getBoundingClientRect();
            const rule = (n) => n.getBoundingClientRect();
            return {
              lines: [...document.querySelectorAll(".term-series.zoom .term-series-compact-line")].map((n) => ({
                y: (rule(n).top + rule(n).bottom) / 2 - plot.top, wide: rule(n).width,
                dash: getComputedStyle(n).strokeDasharray })),
              labels: [...document.querySelectorAll(".term-series.zoom .term-series-ytick")]
                .map((n) => n.textContent).filter((t) => t.startsWith("compact")),
              plot: plot.height,
            };
          });
          assert.equal(compact.lines.length, 2, "a threshold line for each side");
          assert.ok(compact.lines.every((l) => l.wide >= 300 && l.dash === "none" && l.y > 0 && l.y < compact.plot),
            "each is solid, spans the plot and lies inside it");
          assert.ok(compact.lines[0].y < compact.lines[1].y, "the higher threshold is the higher line");
          assert.deepEqual(compact.labels, ["compact 900000 (work)", "compact 450000 (lender)"]);
          assert.ok(drawnMarks.labelInPlot, "and the label is laid out");
          const fall = await p.evaluate(() => {
            const top = document.querySelector(".term-series.zoom .term-series-line.cache_read")
              .getAttribute("points").split(" ").map((pt) => pt.split(",").map(Number));
            const mark = Number(document.querySelector(".term-series.zoom .term-series-compaction").getAttribute("x1"));
            return { before: top[14], after: top[15], mark };
          });
          assert.ok(fall.after[1] > fall.before[1], "the context falls at the compaction (larger y is lower)");
          assert.ok(fall.before[0] < fall.mark && fall.mark < fall.after[0],
            "and the compaction mark stands on that fall, between the two requests");
          assert.equal(drawnMarks.swapDash, "none", "a swap mark is solid");
          assert.notEqual(drawnMarks.compactionDash, "none", "a compaction mark is dashed");
          assert.notEqual(drawnMarks.swapStroke, drawnMarks.compactionStroke, "and the two differ in colour");
          const before = await p.evaluate(() => window.renders);
          for (let i = 0; i < 10; i++) { await p.mouse.move(x + (i % 2 ? 2 : -2), y); await p.waitForTimeout(15); }
          assert.equal(await p.evaluate(() => window.renders) - before, 0,
            `small moves inside the ${slot} chart repaint nothing`);
          assert.equal(await p.evaluate(() => termSeriesHover), slot, "and keep it hovered");
        }
        await p.mouse.move(x, 690);
        await p.mouse.move(20, 690, { steps: 10 });
        await p.waitForTimeout(100);
        assert.equal(await p.evaluate(() => termSeriesHover), false,
          `leaving the ${slot} chart${wiggle ? "" : " in one jump"} ends the hover`);
        assert.equal(await p.evaluate(() => document.querySelectorAll(".term-series.zoom").length), 0,
          "and draws the chart small again");
        await p.close();
      }
    }
  } finally {
    await browser.close();
  }
  console.log("termseries_browser: ok");
})().catch((err) => { console.error(err); process.exit(1); });
