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
   to leave. It also holds the menu to what the row cannot show: once the
   glyphs are on, a control that had left only to save the labels comes back
   if it fits, so nothing sits in the menu that the row has room for (review
   of claunch-8ntfx, s1565-rev). */
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

    // Within a row the menu takes more controls as the column narrows, but
    // that is a trend, not a step function, and the assertion samples it. The
    // peel goes from the right, where the least-used controls sit, so when a
    // wide group has to leave it frees room for several narrow controls
    // behind it and the count steps down: measured at 1px, 1734px holds 6
    // (zoom, split, sims, category, qa, threads) and 1679px holds 4 (life out;
    // zoom, split and sims back). Both states are minimal -- the check below
    // holds every item in the menu to "the row cannot show it" -- so the menu
    // holds what the priority order gives up, not a fixed count. The row kind
    // itself never goes back: once the glyphs are on, no narrower column
    // returns to the labelled row.
    const glyphs = rows.filter(r => r.compact);
    assert(glyphs.length > 0, `the glyph row still takes over once most of the row has left: ${JSON.stringify(rows)}`);
    assert.equal(rows.findIndex(r => r.compact), rows.length - glyphs.length,
      `the glyph row does not go back to labels as the column narrows: ${JSON.stringify(rows)}`);
    for (const band of [rows.filter(r => !r.compact), glyphs]) {
      for (let i = 1; i < band.length; i++) {
        assert(band[i].overflow >= band[i - 1].overflow,
          `the menu takes more controls as the column narrows (sampled widths): ${JSON.stringify(rows)}`);
      }
    }
    for (const r of rows) assert.equal(r.overflowsBox, false, `the row still fits its box at ${r.at}px`);

    // Minimality: no control sits in the menu that the row could show. Every
    // one of them is put back, alone, and the row must then overflow -- that
    // is the fitter's own criterion, measured synchronously so the refit does
    // not undo the temporary class.
    const leftover = [];
    for (const w of [1100, 800, 600, 400]) {
      await width(w);
      await settle();
      const check = await page.evaluate(() => {
        const h = document.getElementById("term-header");
        const more = document.getElementById("term-more");
        const style = getComputedStyle(h);
        const avail = h.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
        const gap = parseFloat(style.columnGap);
        const items = [...h.children].flatMap(el => el.classList.contains("term-actions") ? [...el.children] : [el])
          .filter(el => el !== more && el.id !== "term-details"
            && (el.classList.contains("toolbar-overflow") || getComputedStyle(el).display !== "none"));
        const needed = () => {
          const visible = items.filter(el => !el.classList.contains("toolbar-overflow"));
          if (!more.classList.contains("hidden")) visible.push(more);
          return visible.reduce((sum, el) => sum + el.getBoundingClientRect().width, 0)
            + Math.max(0, visible.length - 1) * gap;
        };
        const back = [];
        for (const item of items.filter(el => el.classList.contains("toolbar-overflow"))) {
          item.classList.remove("toolbar-overflow");
          if (needed() <= avail + 0.5) back.push(item.id || item.className);
          item.classList.add("toolbar-overflow");
        }
        return { compact: h.classList.contains("term-compact"), avail, back };
      });
      if (check.back.length) leftover.push({ at: w, ...check });
    }
    assert.deepEqual(leftover, [],
      `no control sits in the menu that the row could show: ${JSON.stringify(leftover)}`);

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

    console.log(`toolbaroverflow: labels kept to ⋯${Math.max(...labelled.map(r => r.overflow))} of ${edge.of},`
      + ` glyphs take over at ${Math.max(...glyphs.map(r => r.at))}px`);
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
