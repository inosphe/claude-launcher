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
     it gets no mouseleave, and before the fix the overlay stayed zoomed;
   - the readout under the pointer survives a repaint with no pointer move,
     and is empty again once the pointer is off the plot (claunch-v5z9y);
   - the readout is two lines whose widest realistic text fits the plot, and
     the row they are laid out in keeps its reserved height whether or not
     they have text, so neither a longer readout nor an empty one moves the
     chart (claunch-k5udr, claunch-k5udr.1);
   - on the light page the overlay's text and marks are the colours the dark
     page gives them: theme.js rewrites every colour in the overlay's own
     rules for a light page, which put dark text on the overlay's dark wash
     (claunch-v5z9y). */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const STATIC = path.join(__dirname, "../../src/claude_launcher/web/static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");
const theme = fs.readFileSync(path.join(STATIC, "theme.js"), "utf8");

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
#terminal { height: 100%; flex: none; }  /* the real panel is a flex child of the page */
</style><script>${theme}</script></head><body><div id="term"><div id="terminal"><div id="term-tps-overlay"></div></div></div><script>
"use strict";  // as app.js runs: an SVG className write throws only then (claunch-a27zc.1)
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}
const fmtAge = (s) => Math.round(s / 60) + "m", usageShort = (n) => String(n);
${src.slice(constStart, constEnd)}
${["termSeriesTotal", "termSeriesIndexAt", "termSeriesScale", "termSeriesTicks", "termSeriesTick",
    "termSeriesTickLabelY", "termSeriesChart", "termSeriesPointerCheck", "termSeriesRelease",
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
  lastBox.appendChild(el("span", "term-tps-small", "last call 3s ago"));
  const usageBox = el("div", "term-usage");
  const usageRow = el("div", "term-usage-row");
  usageRow.append(el("span", "term-usage-who", "claude"), el("span", "term-usage-item low", "5h 12%"),
    el("span", "term-usage-item mid", "7d 60%"), el("span", "term-usage-item high", "7d 95%"));
  usageBox.appendChild(usageRow);
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
          assert.equal(drawnMarks.max, "1M max, context window",
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
          /* The height axis' own labels are drawn, not merely computed: this
             is the geometry the fix of claunch-nw9ca.1 is about, and only a
             browser can measure the box a font actually produces. */
          const axisLabels = await p.evaluate(() => {
            const boxes = [...document.querySelectorAll(".term-series.zoom > .term-series-chart > .term-series-ytick")]
              .filter((n) => !n.textContent.startsWith("compact"))
              .map((n) => { const b = n.getBBox(); return { text: n.textContent, y: b.y, h: b.height }; });
            let closest = null, clear = Infinity;
            for (let i = 1; i < boxes.length; i++) {
              // ticks ascend by value, so boxes[i-1] is drawn lower than boxes[i]
              const gap = boxes[i - 1].y - (boxes[i].y + boxes[i].h);
              if (gap < clear) { clear = gap; closest = [boxes[i].text, boxes[i - 1].text]; }
            }
            return { count: boxes.length, labels: boxes.map((b) => b.text), clear, closest,
                     boxHeight: boxes.length ? boxes[0].h : null };
          });
          assert.ok(axisLabels.count >= 4,
            `the zoomed chart draws several height rules, got ${axisLabels.labels.join(",")}`);
          // A positive clearance is the labels not touching: the gap is the
          // bottom of the upper box to the top of the lower one.
          assert.ok(axisLabels.clear > 0,
            `no two height labels overlap: the closest pair ${JSON.stringify(axisLabels.closest)} ` +
            `clears ${axisLabels.clear.toFixed(2)}px, against a ${axisLabels.boxHeight}px box`);
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
    /* The readout under the pointer outlives a repaint (claunch-v5z9y). */
    {
      const p = await browser.newPage({ viewport: { width: 900, height: 700 } });
      await p.setContent(page);
      const small = await (await p.$("#term-tps-overlay .term-series")).boundingBox();
      await p.mouse.move(10, 600);
      await p.mouse.move(small.x + small.width / 2, small.y + small.height / 2, { steps: 5 });
      await p.waitForTimeout(100);
      const plot = await (await p.$(".term-series.zoom .term-series-chart")).boundingBox();
      await p.mouse.move(plot.x + plot.width * 0.5, plot.y + plot.height / 2, { steps: 4 });
      /* Two lines: which request, then what it sent (claunch-k5udr.1). */
      const readout = () => p.evaluate(() =>
        [...document.querySelectorAll(".term-series-readout-line")].map((n) => n.textContent));
      const shown = await readout();
      assert.equal(shown.length, 2, "the readout has two lines");
      assert.ok(/^#\d+ · /.test(shown[0]), `the first line names the request under the pointer, got ${JSON.stringify(shown)}`);
      assert.ok(/ = in .* · cached .* · write /.test(shown[1]),
        `the second line gives what it sent, got ${JSON.stringify(shown)}`);
      const before = await p.evaluate(() => window.renders);
      await p.evaluate(() => renderTermTps());
      assert.equal(await p.evaluate(() => window.renders) - before, 1, "one repaint, with no pointer move");
      assert.deepEqual(await readout(), shown, "the readout is the same after the repaint");
      await p.evaluate(() => { renderTermTps(); renderTermTps(); });
      assert.deepEqual(await readout(), shown, "and after more repaints");
      /* The chart does not move under the readout (claunch-k5udr). The
         overlay is right-anchored, so a wider readout used to widen the box
         the plot is laid out in and shift the plot left, out from under a
         pointer that had not moved; that reach cleared the readout, shrank
         the box and shifted the plot back. A longer readout must leave the
         plot's box alone, and crossing the whole plot must keep the readout
         filled at every step. */
      const box = () => p.evaluate(() => {
        const r = document.querySelector(".term-series.zoom .term-series-chart").getBoundingClientRect();
        return [r.x, r.y, r.width, r.height].join();
      });
      const readoutBox = () => p.evaluate(() => {
        const r = document.querySelector(".term-series-readout").getBoundingClientRect();
        const lines = [...document.querySelectorAll(".term-series-readout-line")]
          .map((n) => ({ shown: n.getBoundingClientRect().width, text: n.scrollWidth }));
        return { box: [r.x, r.y, r.width, r.height].join(), lines };
      });
      const still = await box();
      const sized = await readoutBox();
      assert.equal(sized.lines.length, 2, "two line boxes are laid out");
      assert.ok(sized.lines.every((l) => l.text <= plot.width),
        `each line is no wider than the plot, got ${JSON.stringify(sized.lines)}`);
      assert.equal(sized.box.split(",")[2], String(plot.width),
        `the readout is exactly as wide as the plot, got ${sized.box} against ${plot.width}`);
      const widened = await p.evaluate(() => {
        const line = document.querySelectorAll(".term-series-readout-line")[1];
        line.textContent = `${line.textContent} · largest of 12345 requests with 1.00M cached input`;
        const r = document.querySelector(".term-series.zoom .term-series-chart").getBoundingClientRect();
        const box = document.querySelector(".term-series-readout").getBoundingClientRect();
        return { plot: [r.x, r.y, r.width, r.height].join(), readout: [box.x, box.width].join() };
      });
      assert.equal(widened.plot, still, "a longer readout leaves the plot exactly where it was");
      assert.equal(widened.readout, [plot.x, plot.width].join(),
        `and the readout box does not grow with it, got ${widened.readout}`);
      await p.evaluate(() => renderTermTps());
      const sweep = [];
      for (const frac of [0.05, 0.25, 0.5, 0.75, 0.95]) {
        await p.mouse.move(plot.x + plot.width * frac, plot.y + plot.height / 2, { steps: 2 });
        await p.waitForTimeout(30);
        sweep.push([await readout(), await box(), await readoutBox()]);
      }
      assert.ok(sweep.every(([text]) => text[0].trim().startsWith("#") && /\d/.test(text[1])),
        `both lines stay filled across the whole plot, got ${JSON.stringify(sweep.map(([t]) => t))}`);
      assert.ok(sweep.every(([, , r]) => r.lines.every((l) => l.text <= 320)),
        `no line reaches past the plot at any step, got ${JSON.stringify(sweep.map(([, , r]) => r.lines))}`);
      assert.equal(new Set(sweep.map(([, b]) => b)).size, 1,
        `the plot does not move as the pointer crosses it, got ${JSON.stringify(sweep.map(([, b]) => b))}`);
      assert.equal(new Set(sweep.map(([, , r]) => r.box)).size, 1,
        `and neither does the readout's reserved row, got ${JSON.stringify(sweep.map(([, , r]) => r.box))}`);
      /* Wheel the numbers up to the widest a 1M window can produce: the
         second line still has to fit the plot, not be clipped by it. */
      const wide = await p.evaluate(() => {
        const line = document.querySelectorAll(".term-series-readout-line")[1];
        line.textContent = "999.9k = in 999.9k · cached 999.9k · write 999.9k";
        const first = document.querySelectorAll(".term-series-readout-line")[0];
        first.textContent = "#1234 · 11h38m ago · max of 12";
        return [...document.querySelectorAll(".term-series-readout-line")].map((n) => n.scrollWidth);
      });
      assert.ok(wide.every((w) => w <= 320), `the widest realistic line fits the plot, got ${JSON.stringify(wide)}`);
      /* Off the plot but inside the chart's box: the readout is cleared, and
         a repaint does not bring it back. */
      const legend = await (await p.$(".term-series.zoom .term-series-legend")).boundingBox();
      await p.mouse.move(legend.x + 4, legend.y + legend.height / 2, { steps: 4 });
      assert.deepEqual((await readout()).map((t) => t.trim()), ["", ""], "the readout is empty off the plot");
      const cleared = await readoutBox();
      assert.equal(cleared.box, sized.box, "the reserved row keeps its height with no text in it");
      await p.evaluate(() => renderTermTps());
      assert.deepEqual((await readout()).map((t) => t.trim()), ["", ""], "and stays empty after a repaint");
      /* Back on the plot, then out of the chart: the state ends with it. */
      await p.mouse.move(plot.x + plot.width * 0.25, plot.y + plot.height / 2, { steps: 4 });
      assert.notEqual((await readout())[0].trim(), "", "the readout is back on the plot");
      await p.mouse.move(20, 690, { steps: 10 });
      await p.waitForTimeout(100);
      assert.deepEqual(await p.evaluate(() => [termSeriesHover, termSeriesPointerAt]), [false, null],
        "leaving the chart clears the pointer position with the hover");
      await p.close();
    }
    /* The light page: the overlay reads as it does on the dark one. */
    {
      const p = await browser.newPage({ viewport: { width: 900, height: 700 } });
      await p.setContent(page);
      const small = await (await p.$("#term-tps-overlay .term-series")).boundingBox();
      await p.mouse.move(10, 600);
      await p.mouse.move(small.x + small.width / 2, small.y + small.height / 2, { steps: 5 });
      await p.waitForTimeout(100);
      const plot = await (await p.$(".term-series.zoom .term-series-chart")).boundingBox();
      await p.mouse.move(plot.x + plot.width * 0.5, plot.y + plot.height / 2, { steps: 4 });
      const TEXT = [
        ["#term-tps-overlay", "color"], [".term-tps-big", "color"], [".term-tps-small", "color"],
        [".term-usage-who", "color"], [".term-usage-item.low", "color"], [".term-usage-item.mid", "color"],
        [".term-usage-item.high", "color"], [".term-series-ymax", "fill"], [".term-series-ytick", "fill"],
        [".term-series-axis", "color"], [".term-series-legend", "color"], [".term-series-readout", "color"],
      ];
      const MARKS = [
        ["#term-tps-overlay", "borderTopColor"], [".term-series", "borderTopColor"],
        [".term-series-ymax", "stroke"], [".term-series-ytick", "stroke"],
        [".term-series-ymax-rule", "stroke"], [".term-series-compaction", "stroke"],
        [".term-series-swap line", "stroke"], [".term-series-swap polygon", "fill"],
        [".term-series-swap polygon", "stroke"],
      ];
      const STATES = ["", "idle", "stale", "live", "err"];
      const read = (theme) => p.evaluate(([theme, text, marks, states]) => {
        window.claunchTheme.set(theme);
        const out = {};
        for (const state of states) {
          const overlay = document.getElementById("term-tps-overlay");
          overlay.className = state;
          for (const [sel, prop] of [...text, ...marks, ["#term-tps-overlay", "borderTopColor"]]) {
            const n = sel === "#term-tps-overlay" ? overlay : overlay.querySelector(sel);
            out[`${state || "base"} ${sel} ${prop}`] = n ? getComputedStyle(n)[prop] : null;
          }
        }
        return out;
      }, [theme, TEXT, MARKS, STATES]);
      const dark = await read("dark");
      const light = await read("light");
      assert.ok(Object.values(dark).filter((v) => v === null).length === 0, "every probed element is drawn");
      const differs = Object.keys(dark).filter((k) => dark[k] !== light[k])
        .map((k) => `${k}: dark ${dark[k]}, light ${light[k]}`);
      assert.deepEqual(differs, [], "the overlay's colours are the same on the light page");
      /* Text on the overlay's wash (the dark page's colour at 78%, over the
         black behind it here) keeps a contrast a reader can use. */
      const lum = (css) => {
        const [r, g, b] = css.match(/[\d.]+/g).slice(0, 3).map(Number).map((v) => {
          v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
        });
        return 0.2126 * r + 0.7152 * g + 0.0722 * b;
      };
      const wash = lum("rgb(16, 17, 20)");
      const low = TEXT.map(([sel, prop]) => [`base ${sel} ${prop}`, light[`base ${sel} ${prop}`]])
        .map(([k, v]) => [k, (lum(v) + 0.05) / (wash + 0.05)]).filter(([, ratio]) => ratio < 4.5);
      assert.deepEqual(low, [], "every text colour has a contrast of 4.5 or more against the wash");
      await p.close();
    }
  } finally {
    await browser.close();
  }
  console.log("termseries_browser: ok");
})().catch((err) => { console.error(err); process.exit(1); });
