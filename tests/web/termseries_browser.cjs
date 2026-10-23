/* The overlay's token timeline charts under a real pointer (claunch-a27zc).

   termseries_check.js runs the hover logic on stub nodes; what it cannot show
   is what a browser does with a chart that is rebuilt and resized under the
   pointer. This runs the real termSeries* functions from app.js and the real
   style.css in Chromium, in an overlay laid out as renderTermTps lays it out
   (throughput block, its chart, usage block, its chart), and drives it with
   real mouse input. For each of the two charts:

   - entering it grows that chart alone, and the zoomed chart lies under the
     pointer (growing both moved the lower chart out from under it);
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
for (const slot of ["tps", "usage"]) {
  assert.ok(render.includes(`makeChart("${slot}")`), `renderTermTps builds the ${slot} chart by its slot`);
}
assert.ok(/replaceChildren\(\.\.\.\[lastBox, tpsChart, usageBox, usageChart\]/.test(render),
  "renderTermTps stacks the charts under their blocks");

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
${["termSeriesTotal", "termSeriesCumulative", "termSeriesChart", "termSeriesPointerCheck", "termSeriesRelease",
    "termSeriesMaker"].map(slice).join("\n")}
const series = (win) => ({
  available: true, window: win, start: 0, until: 3600, bucket_s: 120, requests: 30,
  compactions: [1800],
  points: Array.from({ length: 30 }, (_, i) => ({ t: i * 120, input: 5, cache_read: 50 + i, cache_write: 8 })),
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
  overlay.replaceChildren(lastBox, makeChart("tps"), usageBox, makeChart("usage"));
}
renderTermTps();
</script></body></html>`;

(async () => {
  const browser = await chromium.launch();
  try {
    for (const [index, slot] of [[0, "tps"], [1, "usage"]]) {
      for (const wiggle of [true, false]) {
        const p = await browser.newPage({ viewport: { width: 900, height: 700 } });
        const errors = [];
        p.on("pageerror", (err) => errors.push(err.message));
        await p.setContent(page);
        const drawn = await p.$$("#term-tps-overlay .term-series");
        assert.deepEqual([drawn.length, errors], [2, []],
          "the overlay draws both charts without an error (a throw here froze the overlay)");
        const box = await drawn[index].boundingBox();
        const x = box.x + box.width / 2, y = box.y + box.height / 2;
        await p.mouse.move(10, 600);
        await p.mouse.move(x, y, { steps: 5 });
        await p.waitForTimeout(100);
        assert.equal(await p.evaluate(() => termSeriesHover), slot, `entering the ${slot} chart hovers it`);
        assert.deepEqual(
          await p.evaluate(() => [...document.querySelectorAll(".term-series")].map((n) => n.classList.contains("zoom"))),
          slot === "tps" ? [true, false] : [false, true], `only the ${slot} chart grows`);
        assert.ok(await p.evaluate(([px, py]) => !!document.elementFromPoint(px, py).closest(".term-series.zoom"), [x, y]),
          `the zoomed ${slot} chart lies under the pointer`);
        if (wiggle) {
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
          "and draws both charts small again");
        await p.close();
      }
    }
  } finally {
    await browser.close();
  }
  console.log("termseries_browser: ok");
})().catch((err) => { console.error(err); process.exit(1); });
