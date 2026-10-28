/* The session header gives up space in three ordered stages (claunch-xbhbg).

   `toolbar.js` used to keep every label while moving controls into the `⋯`
   menu, and once more than half had left it turned the whole row into glyphs
   at once. That flip was hundreds of pixels wide, so one pixel of column
   changed the whole bar, and a put-back pass then brought narrow controls
   back out of the menu, so which controls were where did not follow the
   width in any order (claunch-8ntfx's rule, replaced at the user's request).

   The rule held here, on the real header markup, style.css and toolbar.js:

     1. squeeze: the gap and the buttons' side padding shrink first, by the
        missing width;
     2. iconify: then one control at a time, in MINIMIZE_ORDER, becomes a glyph;
     3. overflow: only once no control is left to iconify do controls go into
        the menu, in the same order.

   So as the column narrows a pixel at a time, no control ever gets wider,
   the glyph set and the menu set only grow, the menu is empty while any
   control could still turn into a glyph, and a step changes at most one
   control's state (the step that brings up `⋯` itself excepted: the button
   takes its own width). And a lit control that went into the menu lights the
   menu's button, since it cannot be seen where it is. */
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
    const errors = [];
    const open = async () => {
      const page = await browser.newPage({ viewport: { width: 2600, height: 900 } });
      page.on("pageerror", e => errors.push(String(e)));
      await page.setContent(`<div id="layout">${header}</div>`);
      await page.addStyleTag({ path: path.join(root, "style.css") });
      await page.addStyleTag({ content: "#layout { display:block; position:relative; width:100%; }" });
      await page.evaluate(() => {
        document.getElementById("term-header").classList.remove("hidden");
        for (const [id, text] of Object.entries({
          "term-title": "s5860", "term-status": "busy",
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
      return page;
    };

    // One sweep inside a page: set the width, wait for the fitting frame (two
    // frames: the resize is observed after layout, the fit runs on the next
    // frame), record every control. ~2200 widths at two frames each is over a
    // minute on one page, so four pages sweep a quarter each at once; each
    // starts one pixel above its quarter so the seams are compared too.
    const sweep = (page, from, to) => page.evaluate(async ({ from, to }) => {
      const layout = document.getElementById("layout");
      const h = document.getElementById("term-header");
      const frame = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
      const key = el => el.id || [...el.classList].find(c => c.startsWith("term-")) || "";
      const out = [];
      for (let w = from; w >= to; w--) {
        layout.style.width = `${w}px`;
        await frame();
        const items = [...h.children].flatMap(el => el.classList.contains("term-actions") ? [...el.children] : [el])
          .filter(el => el.id !== "term-more" && el.id !== "term-title"
            && (el.classList.contains("toolbar-overflow") || getComputedStyle(el).display !== "none"));
        const details = document.getElementById("term-details");
        const row = {};
        for (const el of [...items, details]) {
          const state = el.classList.contains("toolbar-overflow") ? "menu"
            : el.hasAttribute("data-toolbar-glyph") ? "icon" : "label";
          row[key(el)] = { state, width: state === "menu" ? 0 : el.getBoundingClientRect().width };
        }
        out.push({
          at: w, row,
          gap: parseFloat(getComputedStyle(h).columnGap),
          more: !document.getElementById("term-more").classList.contains("hidden"),
          overflowsBox: h.scrollWidth > h.clientWidth + 0.5,
        });
      }
      return out;
    }, { from, to });

    const [FROM, TO, PARTS] = [2400, 160, 4];
    const cut = Math.ceil((FROM - TO + 1) / PARTS);
    const pages = await Promise.all(Array.from({ length: PARTS }, open));
    const page = pages[0];
    const parts = await Promise.all(pages.map((p, i) =>
      sweep(p, FROM - i * cut + (i ? 1 : 0), Math.max(TO, FROM - (i + 1) * cut + 1))));
    const rows = parts.flatMap((part, i) => (i ? part.slice(1) : part));
    for (let i = 1; i < rows.length; i++) assert.equal(rows[i].at, rows[i - 1].at - 1, "the quarters join up");
    const at = w => rows.find(r => r.at === w);
    const states = r => Object.values(r.row).map(c => c.state);
    const full = rows[0];
    assert(states(full).every(s => s === "label") && full.gap === 10 && !full.more,
      `the widest column is the full row: ${JSON.stringify(full)}`);

    const problems = [];
    let squeezed = false;
    for (let i = 1; i < rows.length; i++) {
      const a = rows[i - 1];
      const b = rows[i];
      if (b.overflowsBox) problems.push(`${b.at}px: the row overflows its box`);
      if (b.gap < a.gap - 2) problems.push(`${b.at}px: the gap jumped ${a.gap} -> ${b.gap}`);
      if (b.gap > a.gap + 0.01) problems.push(`${b.at}px: the gap grew ${a.gap} -> ${b.gap}`);
      if (b.gap < 10) squeezed = true;
      let changed = 0;
      for (const [k, cb] of Object.entries(b.row)) {
        const ca = a.row[k];
        if (!ca) continue;
        if (cb.width > ca.width + 0.5) problems.push(`${b.at}px: ${k} grew ${ca.width.toFixed(1)} -> ${cb.width.toFixed(1)}`);
        const rank = { label: 0, icon: 1, menu: 2 };
        if (rank[cb.state] < rank[ca.state]) problems.push(`${b.at}px: ${k} went back ${ca.state} -> ${cb.state}`);
        if (cb.state !== ca.state) changed++;
        // Glyphs only once the spacing is as tight as it goes.
        if (cb.state === "icon" && b.gap > 4.01) problems.push(`${b.at}px: ${k} is a glyph with gap ${b.gap}`);
      }
      if (changed > 1 && !(b.more && !a.more))
        problems.push(`${b.at}px: ${changed} controls changed state in one pixel`);
    }
    assert(squeezed, "the spacing squeezes before anything else");
    // The menu is empty while any control could still become a glyph: the
    // controls that are labels at the first width with a menu are exactly the
    // ones that are labels at the last width without it (their glyph is no
    // narrower than their label, so they never iconify).
    const firstMenu = rows.findIndex(r => r.more);
    assert(firstMenu > 0, "the narrowest column needs the menu");
    const lastRow = rows[firstMenu - 1];
    const labelled = r => Object.entries(r.row).filter(([, c]) => c.state === "label").map(([k]) => k).sort();
    assert.deepEqual(labelled(rows[firstMenu]).filter(k => rows[firstMenu].row[k].state !== "menu"),
      labelled(lastRow).filter(k => rows[firstMenu].row[k].state !== "menu"),
      "the menu opens only once nothing left on the row can still turn into a glyph");
    assert.deepEqual(problems.slice(0, 10), [], `${problems.length} problems in the 1px sweep`);

    // Nothing here may feed itself: a held column must hold still, on both
    // sides of the first glyph and of the first menu item.
    const firstIcon = rows.find(r => states(r).includes("icon")).at;
    for (const w of [firstIcon, firstIcon + 1, rows[firstMenu].at, rows[firstMenu].at + 1]) {
      await page.locator("#layout").evaluate((el, px) => { el.style.width = `${px}px`; }, w);
      const seq = await page.evaluate(() => new Promise(done => {
        const h = document.getElementById("term-header");
        const sig = () => `${h.style.getPropertyValue("--tb-gap")}|${h.querySelectorAll("[data-toolbar-glyph]").length}|`
          + h.querySelectorAll(".toolbar-overflow").length;
        const out = []; let n = 60;
        const step = () => { out.push(sig()); (--n ? requestAnimationFrame(step) : done(out.slice(5))); };
        requestAnimationFrame(step);
      }));
      assert.equal(new Set(seq).size, 1, `the row holds still at ${w}px: ${[...new Set(seq)].join(" / ")}`);
    }

    // app.js rewrites the whole class list of the status, tps, hold and link
    // chips on every poll. The glyph state is an attribute so that write
    // cannot take it off: the chip is still a glyph after the write.
    const glyphHold = rows.find(r => r.row["term-hold"] && r.row["term-hold"].state === "icon").at;
    await page.locator("#layout").evaluate((el, px) => { el.style.width = `${px}px`; }, glyphHold);
    const kept = await page.evaluate(() => new Promise(done => {
      const hold = document.getElementById("term-hold");
      requestAnimationFrame(() => requestAnimationFrame(() => {
        const before = hold.hasAttribute("data-toolbar-glyph");
        hold.className = "term-btn hold-chip";
        const right = hold.hasAttribute("data-toolbar-glyph");
        requestAnimationFrame(() => requestAnimationFrame(() =>
          done({ before, right, after: hold.hasAttribute("data-toolbar-glyph") })));
      }));
    }));
    assert.deepEqual(kept, { before: true, right: true, after: true },
      `a whole-class write keeps the hold chip a glyph at ${glyphHold}px`);

    // A lit control in the menu lights `⋯`; on the row it does not.
    const lit = async (w, id, cls) => {
      await page.locator("#layout").evaluate((el, px) => { el.style.width = `${px}px`; }, w);
      await page.evaluate(({ id, cls }) => {
        for (const el of document.querySelectorAll(".has-resource, .has-news")) el.classList.remove("has-resource", "has-news");
        if (id) document.getElementById(id).classList.add(cls);
      }, { id, cls });
      await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => requestAnimationFrame(r)))));
      return page.evaluate((id) => {
        const more = document.getElementById("term-more");
        return {
          inMenu: id ? document.getElementById(id).classList.contains("toolbar-overflow") : null,
          more: ["has-resource", "has-news"].filter(c => more.classList.contains(c)),
        };
      }, id);
    };
    assert.deepEqual(await lit(180, "term-qabtn", "has-resource"), { inMenu: true, more: ["has-resource"] },
      "a lit QA button in the menu lights ⋯");
    await page.locator("#term-more").click();
    assert.equal(await page.locator("#term-overflow-menu .term-btn.has-resource").count(), 1,
      "the QA button's proxy in the menu is lit too");
    await page.keyboard.press("Escape");
    assert.deepEqual(await lit(180, "term-threadsbtn", "has-news"), { inMenu: true, more: ["has-news"] },
      "thread news in the menu lights ⋯ with the news colour");
    assert.deepEqual(await lit(180, null), { inMenu: null, more: [] }, "nothing lit, ⋯ unlit");
    // At the widest column with a menu, the QA button is still on the row.
    const roomy = rows[firstMenu].at;
    const onRow = await lit(roomy, "term-qabtn", "has-resource");
    assert.equal(onRow.inMenu, false, `QA is on the row at ${roomy}px`);
    assert.deepEqual(onRow.more, [], "a lit control on the row does not light ⋯");
    assert.deepEqual(errors, []);

    const glyphs = Object.values(at(rows[firstMenu].at - 0 + 0).row).filter(c => c.state === "icon").length;
    console.log(`toolbaroverflow: squeeze from ${rows.find(r => r.gap < 10).at}px, first glyph at ${firstIcon}px,`
      + ` menu from ${rows[firstMenu].at}px (${glyphs} glyphs on the row then), 1px sweep 2400..160 monotonic`);
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
