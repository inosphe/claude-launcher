/* What one repaint of the session header costs, in forced layout.

   The header re-fits on every mutation it cannot prove irrelevant, and the
   reminder chip's countdown is a mutation every second — so on an idle page
   this ran 1 Hz forever. The trace behind claunch-hwbuc caught it there:
   `fits` was 55 ms of the main thread in a ten-second recording, and a tick
   spent 57 rect reads at a wide header and 176 at one where six controls had
   gone into the More menu (each call re-solved the header's style and
   re-read every visible item, and each read forced a fresh layout because
   the call before it had just written a class).

   The countdown cannot move a width: it is digits in a `tabular-nums` run, at
   the same length. So the property this holds is twofold — that tick costs no
   measurement at all, AND the row still re-fits when the text can matter (a
   digit count changes, or a character that is not a digit). A guard that only
   ever answered "no refit" would pass the first half and break the header; the
   second half is why both are here.

   The zero is claunch-xy9s0: with the proof in hand `sameWidths` used to read
   one rect anyway, to compare the touched item against the width the last fit
   recorded, and that read forced a style and a layout of the header on every
   tick (1 read/tick in a 2026-10-23 trace of the dashboard, once per second
   for as long as a session is on screen). The proof says the width did not
   move, so there is nothing left to read. The row is checked in both states
   that reach that code — settled (squeezed at 1920px, iconified and
   overflowing at 820px: the fixture's own header),
   and a row that fits — because the fit-to-a-share case is the one where a
   text change can matter without the item's own width moving.

   Numbers rather than a stopwatch: counts are deterministic for a given DOM,
   where milliseconds on a loaded runner are not. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
/* STATIC_ROOT is how the before/after for claunch-hwbuc was taken: point it
   at a checkout of an earlier toolbar.js and the same check reports what that
   one cost (it fails the first assertion, which is the point). */
const root = process.env.STATIC_ROOT
  || path.join(__dirname, "../../src/claude_launcher/web/static");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const header = html.slice(html.indexOf('<div id="term-header"'), html.indexOf('    <!-- Deliveries'));

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1920, height: 800 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.setContent(`<div id="layout">${header}</div>`);
    await page.addStyleTag({ path: path.join(root, "style.css") });
    await page.addStyleTag({ content: "#layout { display:block; position:relative; width:100%; }" });
    await page.evaluate(() => {
      document.getElementById("term-header").classList.remove("hidden");
      for (const [id, text] of Object.entries({
        "term-title": "s1305", "term-status": "busy",
        "term-timer": "Ⅱ role reminder in 7:12",
        "term-hold": "delivery: would queue — busy",
        "term-zoom-level": "13px", "term-timer-skip": "⏭",
      })) {
        const el = document.getElementById(id);
        el.textContent = text;
        el.classList.remove("hidden");
      }
    });
    // quietdom runs before toolbar.js in the page; its same-value guards are
    // part of what a repaint writes here (see toolbar_browser).
    await page.addScriptTag({ path: path.join(root, "quietdom.js") });
    await page.addScriptTag({ path: path.join(root, "toolbar.js") });

    const settle = () => page.evaluate(() => new Promise((done) => {
      let n = 3;
      const step = () => (--n ? requestAnimationFrame(step) : done());
      requestAnimationFrame(step);
    }));

    /* How a tick reaches the row: paintTermTimer writes the chip's text (which
       a MutationObserver reports as a childList swap) and nothing else. */
    async function repaint(width, texts) {
      await page.locator("#layout").evaluate((el, w) => { el.style.width = `${w}px`; }, width);
      await settle();
      const state = await page.evaluate(() => {
        const h = document.getElementById("term-header");
        return {
          cls: h.className,
          overflow: [...h.querySelectorAll(".toolbar-overflow")].map((el) => el.id || el.className),
        };
      });
      const reads = await page.evaluate(async (texts) => {
        const proto = Element.prototype;
        const orig = proto.getBoundingClientRect;
        let rects = 0;
        proto.getBoundingClientRect = function (...a) { rects++; return orig.apply(this, a); };
        const frame = () => new Promise((done) => {
          let n = 3;
          const step = () => (--n ? requestAnimationFrame(step) : done());
          requestAnimationFrame(step);
        });
        const box = document.getElementById("term-timer");
        const out = [];
        try {
          for (const text of texts) {
            rects = 0;
            box.textContent = text;
            await frame();
            out.push(rects);
          }
        } finally { proto.getBoundingClientRect = orig; }
        return out;
      }, texts);
      const after = await page.evaluate(() => {
        const h = document.getElementById("term-header");
        return {
          cls: h.className,
          overflow: [...h.querySelectorAll(".toolbar-overflow")].map((el) => el.id || el.className),
        };
      });
      return { ...state, reads, after };
    }

    /* A countdown, second by second — digits, same length. */
    const COUNTDOWN = ["Ⅱ role reminder in 7:11", "Ⅱ role reminder in 7:10",
                       "Ⅱ role reminder in 7:09", "Ⅱ role reminder in 7:08"];

    for (const width of [1920, 820]) {
      const run = await repaint(width, COUNTDOWN);
      assert.equal(errors.length, 0, `page errors: ${errors.join(" | ")}`);
      const worst = Math.max(...run.reads.slice(1));
      assert.equal(worst, 0,
        `a countdown tick must not measure the row at ${width}px ` +
        `(${run.reads.join(",")} rect reads)`);
      assert.deepEqual(run.after, { cls: run.cls, overflow: run.overflow },
        `a countdown cannot change what fits at ${width}px`);
    }

    /* The two ways the text CAN matter, and both must still re-fit. */
    const grew = await repaint(1280, ["Ⅱ role reminder in 9:59", "Ⅱ role reminder in 10:00"]);
    assert.ok(grew.reads[1] > 10,
      `a countdown that gains a digit must re-fit (${grew.reads.join(",")} rect reads)`);
    const relabelled = await page.evaluate(async () => {
      const proto = Element.prototype;
      const orig = proto.getBoundingClientRect;
      let rects = 0;
      proto.getBoundingClientRect = function (...a) { rects++; return orig.apply(this, a); };
      const frame = () => new Promise((done) => {
        let n = 3;
        const step = () => (--n ? requestAnimationFrame(step) : done());
        requestAnimationFrame(step);
      });
      const box = document.getElementById("term-timer");
      box.textContent = "Ⅱ role reminder at 10:00";
      await frame();
      proto.getBoundingClientRect = orig;
      return rects;
    });
    assert.ok(relabelled > 10,
      `a text change that is not a digit swap must re-fit (${relabelled} rect reads)`);

    /* To the other state that reaches sameWidths: a row that fits, where no
       control has gone into the More menu, none is a glyph and the spacing
       is not squeezed, so an item's width is its content's. The countdown is covered there too;
       a relabel, which can move that width, still has to re-fit. */
    await page.locator("#layout").evaluate((el) => { el.style.width = "1920px"; });
    await settle();
    await page.evaluate(() => {
      const h = document.getElementById("term-header");
      for (const el of [...h.children]) {
        if (el.id === "term-more") continue;
        if (el.id === "term-title" || (el.querySelector && el.querySelector("#term-timer"))) continue;
        el.classList.add("hidden");
      }
    });
    await settle();
    const fitting = await page.evaluate(() => {
      const h = document.getElementById("term-header");
      return { squeezed: parseFloat(getComputedStyle(h).columnGap) < 10,
               icons: h.querySelectorAll(".toolbar-icon").length,
               overflow: h.querySelectorAll(".toolbar-overflow").length };
    });
    assert.deepEqual(fitting, { squeezed: false, icons: 0, overflow: 0 },
      "the fitting-row case needs a full header, not one squeezed, iconified or overflowing");
    const fitted = await repaint(1920, COUNTDOWN);
    assert.equal(Math.max(...fitted.reads.slice(1)), 0,
      `a countdown tick must not measure a row that fits ` +
      `(${fitted.reads.join(",")} rect reads)`);
    assert.deepEqual(fitted.after, { cls: fitted.cls, overflow: fitted.overflow },
      "a countdown cannot change what fits on a row that fits");
    const refitted = await repaint(1920, ["Ⅱ role reminder in 7:10", "Ⅱ role reminder at 7:10"]);
    assert.ok(refitted.reads[1] > 10,
      `a relabel on a row that fits must re-fit (${refitted.reads.join(",")} rect reads)`);

    console.log(`toolbarrefit_browser: ok (${COUNTDOWN.length} countdown ticks, ` +
                `0 rect reads each on settled and full rows; a longer countdown ` +
                `re-fits with ${grew.reads[1]}, a relabel with ${relabelled})`);
  } finally {
    await browser.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
