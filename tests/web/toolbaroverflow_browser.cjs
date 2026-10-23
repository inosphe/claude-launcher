/* The session header gives up its controls one at a time, keeping the labels.

   `toolbar.js` used to shrink the whole row to fixed-width glyphs the moment
   the labelled row was a pixel too wide, and only then start moving controls
   into the `⋯` menu. The two states differ by hundreds of pixels, so a 1px
   change flipped the entire bar between "every label" and "every glyph", and
   the menu never appeared while any label did. Measured on the shipped
   markup, 21 controls: 2016.83px expanded against 962.72px compact, with the
   first `⋯` item appearing only at 1026px (claunch-8ntfx).

   This drives the real header markup, style.css and toolbar.js in a browser
   and holds the order the other way: the labels stay while controls are left
   to move, the menu grows one item at a time as the column narrows, and the
   glyph row appears only once more than half of the controls would have had
   to leave. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const header = html.slice(html.indexOf('<div id="term-header"'), html.indexOf('    <!-- Deliveries'));

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 2600, height: 900 } });
    await page.setContent(`<div id="layout">${header}</div>`);
    await page.addStyleTag({ path: path.join(root, "style.css") });
    await page.addStyleTag({ content: "#layout { display:block; position:relative; width:100%; }" });
    await page.evaluate(() => {
      document.getElementById("term-header").classList.remove("hidden");
      for (const [id, text] of Object.entries({
        "term-title": "s1565", "term-status": "busy",
        "term-timer": "Ⅱ role reminder in 7:12", "term-timer-skip": "⏭",
        "term-hold": "delivery: would queue — busy", "term-zoom-level": "13px",
      })) {
        const el = document.getElementById(id);
        if (!el) continue;
        el.textContent = text;
        el.classList.remove("hidden");
      }
    });
    // index.html loads quietdom.js before every other script; its same-value
    // guards are part of what the fitter sees on each repaint.
    await page.addScriptTag({ path: path.join(root, "quietdom.js") });
    await page.addScriptTag({ path: path.join(root, "toolbar.js") });
    const settle = (frames = 4) => page.evaluate(n => new Promise(done => {
      const step = () => (--n ? requestAnimationFrame(step) : done());
      requestAnimationFrame(step);
    }), frames);
    const width = w => page.locator("#layout").evaluate((el, px) => { el.style.width = `${px}px`; }, w);
    const read = () => page.evaluate(() => {
      const h = document.getElementById("term-header");
      // `.toolbar-overflow` is `display:none`, so a control that has left the
      // row is only in this list by its class, not by its computed display.
      const shown = [...h.children].flatMap(el => el.classList.contains("term-actions") ? [...el.children] : [el])
        .filter(el => el.id !== "term-more" && el.id !== "term-details"
          && (el.classList.contains("toolbar-overflow") || getComputedStyle(el).display !== "none"));
      return {
        compact: h.classList.contains("term-compact"),
        overflow: shown.filter(el => el.classList.contains("toolbar-overflow")).length,
        of: shown.length,
        overflowsBox: h.scrollWidth > h.clientWidth,
      };
    });

    // A coarse sweep down the column: what leaves the row, and in what state.
    const rows = [];
    for (const w of [2200, 2000, 1700, 1400, 1200, 1150, 1000, 800, 600, 400, 300]) {
      await width(w);
      await settle();
      rows.push({ at: w, ...await read() });
    }

    assert.equal(rows[0].compact, false, `the widest column keeps its labels: ${JSON.stringify(rows[0])}`);
    assert.equal(rows[0].overflow, 0, `the widest column needs no menu: ${JSON.stringify(rows[0])}`);

    const labelled = rows.filter(r => !r.compact && r.overflow > 0);
    assert(labelled.length >= 2,
      `a column too narrow for its labels moves controls into the menu, keeping the labels: ${JSON.stringify(rows)}`);

    for (let i = 1; i < rows.length; i++) {
      assert(rows[i].overflow >= rows[i - 1].overflow,
        `the menu only grows as the column narrows: ${JSON.stringify(rows)}`);
    }

    const glyphs = rows.filter(r => r.compact);
    assert(glyphs.length > 0, `the glyph row still takes over when the menu would hold most of the row: ${JSON.stringify(rows)}`);
    assert(glyphs.every(r => r.overflow >= Math.floor(r.of / 2)),
      `the labels are given up only once more than half of the controls would leave: ${JSON.stringify(rows)}`);
    for (const r of glyphs) assert.equal(r.overflowsBox, false, `the row still fits its box at ${r.at}px`);

    // The regression pin: one pixel past the point where the labelled row
    // fits costs one control, not every label on the row. The old fitter
    // turned all of them into glyphs here.
    let edge = null;
    for (let w = 2200; w >= 1900; w--) {
      await width(w);
      await settle(2);
      const r = await read();
      if (r.compact || r.overflow > 0) { edge = { at: w, ...r }; break; }
    }
    assert(edge, "the labelled row stops fitting somewhere in 1900..2200px, found none");
    assert.equal(edge.compact, false, `one pixel over the limit keeps the labels: ${JSON.stringify(edge)}`);
    assert.equal(edge.overflow, 1, `one pixel over the limit costs exactly one control: ${JSON.stringify(edge)}`);

    // Nothing here may feed itself: a held column must hold still.
    for (const w of [edge.at, edge.at + 1]) {
      await width(w);
      await settle();
      const seq = await page.evaluate(() => new Promise(done => {
        const h = document.getElementById("term-header");
        const sig = () => (h.classList.contains("term-compact") ? "C" : "E")
          + h.querySelectorAll(".toolbar-overflow").length;
        const out = [sig()]; let n = 60;
        const step = () => { out.push(sig()); (--n ? requestAnimationFrame(step) : done(out)); };
        requestAnimationFrame(step);
      }));
      assert.equal(new Set(seq).size, 1, `the row holds still at ${w}px: ${[...new Set(seq)].join("|")}`);
    }

    console.log(`toolbaroverflow: labels kept to ⋯${Math.max(...labelled.map(r => r.overflow))} of ${edge.of}, glyphs from ${Math.min(...glyphs.map(r => r.at))}px`);
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
