/* Real geometry and control checks. NODE_PATH may point to external Playwright. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const header = html.slice(html.indexOf('<div id="term-header"'), html.indexOf('    <!-- Deliveries'));
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1920, height: 800 } });
    // The toolbar re-fits on the frame after a resize or a mutation
    // (ResizeObserver / MutationObserver -> requestAnimationFrame -> fit).
    // Wait for those frames rather than for a fixed 80ms: under a loaded
    // runner the timer can expire before the fitting frame (claunch-gqp8c).
    const settle = () => page.evaluate(() => new Promise(done => {
      let n = 3; const step = () => (--n ? requestAnimationFrame(step) : done()); requestAnimationFrame(step);
    }));
    const errors = [];
    page.on("pageerror", e => errors.push(String(e)));
    await page.setContent(`<div id="layout">${header}</div>`);
    await page.addStyleTag({ path: path.join(root, "style.css") });
    await page.addStyleTag({ content: "#layout { display:block; position:relative; width:100%; }" });
    await page.evaluate(() => {
      document.getElementById("term-header").classList.remove("hidden");
      for (const [id, text] of Object.entries({
        "term-title": "s581", "term-status": "busy", "term-timer": "Ⅱ role reminder in 7:12",
        "term-hold": "delivery: would queue — busy", "term-zoom-level": "13px",
        "term-timer-skip": "⏭",
      })) {
        const el = document.getElementById(id);
        el.textContent = text;
        el.classList.remove("hidden");
      }
      window.clicks = 0;
      document.getElementById("term-splitbtn").addEventListener("click", () => window.clicks++);
    });
    // The page loads quietdom.js before every other script (index.html); its
    // same-value guards are part of what the toolbar sees on each repaint.
    await page.addScriptTag({ path: path.join(root, "quietdom.js") });
    await page.addScriptTag({ path: path.join(root, "toolbar.js") });
    async function check(width) {
      // Desktop columns can be narrow even while the viewport stays wide;
      // phones already use the separate mobile controls.
      await page.locator("#layout").evaluate((el, width) => { el.style.width = `${width}px`; }, width);
      await settle();
      const geometry = await page.evaluate(() => {
        const h = document.getElementById("term-header");
        const rects = [...h.querySelectorAll("button, #term-title, .badge, .term-handle")]
          .filter(el => el.getClientRects().length).map(el => {
            const r = el.getBoundingClientRect(); return { id: el.id, left: r.left, right: r.right, y: r.top + r.height / 2 };
          });
        return { rects, width: h.clientWidth, scroll: h.scrollWidth, pageWidth: document.documentElement.scrollWidth };
      });
      assert(geometry.scroll <= geometry.width, `header overflow at ${width}`);
      assert(geometry.pageWidth <= 1920, `page overflow at ${width}`);
      for (const r of geometry.rects) assert(r.left >= 0 && r.right <= width, `${r.id} outside at ${width}`);
      const centers = geometry.rects.map(r => r.y);
      assert(Math.max(...centers) - Math.min(...centers) < 4, `wrapped at ${width}`);
      // `details` floats outside the flex row (absolute), so the row's
      // align-items does not centre it; it has to land on the same line by
      // itself. Pinned 8px from the top it sat 3.8px above its neighbours.
      const details = geometry.rects.find(r => r.id === "term-details");
      for (const r of geometry.rects.filter(r => r.id && r.id !== "term-details" && /^term-(?!title)/.test(r.id)))
        assert(Math.abs(r.y - details.y) <= 1, `details centred with ${r.id} at ${width}: ${details.y} vs ${r.y}`);
      const ordered = geometry.rects.filter(r => r.right > r.left).sort((a, b) => a.left - b.left);
      for (let i = 1; i < ordered.length; i++) assert(ordered[i].left >= ordered[i - 1].right - 1, `overlap at ${width}`);
    }
    for (const width of [1920, 1193, 1000, 800, 600, 480, 360, 240, 180]) {
      await check(width);
      assert(await page.locator("#term-title").evaluate(el => el.clientWidth >= el.scrollWidth),
        `session name stays fully visible at ${width}`);
    }
    // The reminder controls share one frame (claunch-0jjv3): at full width
    // the chip and the skip sit inside it, and when the row is tight they
    // leave for the overflow menu together.
    await check(1920);
    const group = await page.evaluate(() => {
      const g = document.getElementById("term-reminder").getBoundingClientRect();
      const inside = id => {
        const r = document.getElementById(id).getBoundingClientRect();
        return r.left >= g.left && r.right <= g.right && r.top >= g.top && r.bottom <= g.bottom;
      };
      return { shown: g.width > 0, timer: inside("term-timer"), skip: inside("term-timer-skip") };
    });
    assert.deepEqual(group, { shown: true, timer: true, skip: true }, "reminder group frames both controls");
    // The countdown rewrites its text every second (claunch-i3es6): a rewrite
    // that leaves the chip as wide as it was re-describes the controls
    // without the measuring pass, and one that widens it still re-fits.
    // `fits` counts the pass by its first query, the one that clears the
    // overflowed and iconified items. The skip holds only on a full row (no
    // squeeze, no glyph, no menu), so the column is made wide enough for
    // every control first.
    await page.locator("#layout").evaluate(el => { el.style.width = "4000px"; });
    await settle();
    await page.evaluate(() => {
      const h = document.getElementById("term-header");
      const query = h.querySelectorAll.bind(h);
      window.fits = 0;
      h.querySelectorAll = (sel) => { if (sel.startsWith(".toolbar-overflow")) window.fits++; return query(sel); };
    });
    const refits = async (text) => {
      await page.evaluate(text => {
        window.fits = 0;
        // As paintTermTimer repaints each second: the class and the title
        // are written again with what they already read, then the text.
        const box = document.querySelector("#term-timer");
        box.className = box.className;
        box.title = box.title;
        box.textContent = text;
      }, text);
      await settle();
      return page.evaluate(() => window.fits);
    };
    assert.equal(await page.locator("#term-header").evaluate(el => el.querySelectorAll("[data-toolbar-glyph]").length), 0);
    assert.equal(await refits("Ⅱ role reminder in 7:12"), 0, "a same-width countdown text does not re-fit the row");
    assert.equal(await refits("Ⅱ role reminder in 7:12 and a good deal longer than it was"), 1,
      "a countdown text that widens the chip re-fits the row");
    await refits("Ⅱ role reminder in 7:12");
    await check(1920);
    // Only the frame draws a border (claunch-0jjv3.2): the chip has none on
    // any side, the skip keeps just the divider on its left, and a due
    // state colours the frame rather than the controls.
    const borders = await page.evaluate(() => {
      const visible = s => s.borderStyle !== "none" && parseFloat(s.borderWidth) > 0 && s.borderColor !== "rgba(0, 0, 0, 0)";
      const sides = id => {
        const s = getComputedStyle(document.getElementById(id));
        return ["Top", "Right", "Bottom", "Left"].filter(side => visible({
          borderStyle: s[`border${side}Style`], borderWidth: s[`border${side}Width`], borderColor: s[`border${side}Color`],
        }));
      };
      const frame = () => getComputedStyle(document.getElementById("term-reminder")).borderTopColor;
      const rest = frame();
      for (const id of ["term-timer", "term-timer-skip"]) document.getElementById(id).classList.add("due");
      const out = { timer: sides("term-timer"), skip: sides("term-timer-skip"), changed: frame() !== rest, due: frame() };
      for (const id of ["term-timer", "term-timer-skip"]) document.getElementById(id).classList.remove("due");
      return out;
    });
    assert.deepEqual(borders, { timer: [], skip: ["Left"], changed: true, due: "rgb(77, 56, 0)" },
      `one border around the reminder controls: ${JSON.stringify(borders)}`);
    await check(240);
    assert.equal(await page.locator("#term-reminder").evaluate(el => el.classList.contains("toolbar-overflow")), true,
      "the reminder group overflows as one item");
    await page.locator("#term-more").click();
    const menuText = await page.locator("#term-overflow-menu button").allTextContents();
    const at = menuText.findIndex(t => t.includes("role reminder"));
    assert(at >= 0 && menuText[at + 1] === "⏭", `menu keeps the reminder pair together: ${JSON.stringify(menuText)}`);
    await page.keyboard.press("Escape");
    // A hidden member of the group (nothing to skip) is not offered there.
    // Without the skip the group is narrower and holds the row a little
    // longer (it is late in the minimize order), so the column is narrower.
    await page.evaluate(() => document.getElementById("term-timer-skip").classList.add("hidden"));
    await check(180);
    assert.equal(await page.locator("#term-reminder").evaluate(el => el.classList.contains("toolbar-overflow")), true,
      "the reminder group without its skip is in the menu at 180px");
    await page.locator("#term-more").click();
    const lone = await page.locator("#term-overflow-menu button").allTextContents();
    assert(lone.some(t => t.includes("role reminder")) && !lone.includes("⏭"),
      `a hidden skip stays out of the menu: ${JSON.stringify(lone)}`);
    await page.keyboard.press("Escape");
    await page.evaluate(() => {
      for (const id of ["term-timer", "term-timer-skip"]) document.getElementById(id).classList.add("hidden");
    });
    await check(1920);
    assert.equal(await page.locator("#term-reminder").evaluate(el => getComputedStyle(el).display), "none",
      "an empty reminder group leaves the row");
    await page.evaluate(() => {
      for (const id of ["term-timer", "term-timer-skip"]) document.getElementById(id).classList.remove("hidden");
    });
    await check(1920);
    await page.locator("#layout").evaluate(el => { el.style.width = "180px"; });
    await settle();
    await page.locator("#term-more").click();
    await page.locator("#term-overflow-menu button").filter({ hasText: "⬒ run" }).click();
    assert.equal(await page.evaluate(() => window.clicks), 1, "overflow forwards original action");
    // The view switch folds in as one button per radio, pressed on the view
    // on screen, and pressing one selects that radio.
    await page.locator("#term-more").click();
    const views = await page.locator("#term-overflow-menu button").evaluateAll(
      bs => bs.filter(b => ["CLI", "HTML"].includes(b.textContent)).map(b => [b.textContent, b.getAttribute("aria-pressed")]));
    assert.deepEqual(views, [["CLI", "true"], ["HTML", "false"]], `view radios in the menu: ${JSON.stringify(views)}`);
    await page.locator("#term-overflow-menu button").filter({ hasText: /^HTML$/ }).click();
    assert.equal(await page.evaluate(() => document.querySelector('input[name="term-view"][value="html"]').checked), true,
      "the HTML proxy selects the HTML radio");
    // The lifecycle group (claunch-ndz47) folds in the same way: one button
    // per segment, pressed on the state the session is in, and a segment the
    // state does not allow stays in the menu disabled.
    await page.evaluate(() => {
      document.getElementById("term-kill").setAttribute("aria-checked", "true");
      for (const id of ["term-kill", "term-resume"]) document.getElementById(id).disabled = true;
    });
    await settle();
    await page.locator("#term-more").click();
    const life = await page.locator("#term-overflow-menu button").evaluateAll(
      bs => bs.filter(b => ["resume", "pause", "kill", "archive"].includes(b.textContent))
        .map(b => [b.textContent, b.getAttribute("aria-pressed"), b.disabled]));
    assert.deepEqual(life, [["resume", "false", true], ["pause", "false", false],
      ["kill", "true", true], ["archive", "false", false]],
      `lifecycle radios in the menu: ${JSON.stringify(life)}`);
    await page.keyboard.press("Escape");
    await page.locator("#term-more").click();
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#term-more").getAttribute("aria-expanded"), "false");
    await page.evaluate(() => {
      document.getElementById("term-title").textContent = "very-long-session-name-".repeat(8);
      const hold = document.getElementById("term-hold");
      hold.textContent = "delivery: queued — unsent line (999)";
      hold.title = "Resume delivery";
      document.getElementById("term-link").classList.remove("hidden");
      document.getElementById("term-link").textContent = "reconnecting (10)";
    });
    for (const width of [360, 800, 1193]) await check(width);
    assert.match(await page.locator("#term-hold").getAttribute("title"), /queued.*999.*Resume delivery/);
    // Back to the ordinary row: a short name and the socket up again. The
    // header turns its glyphs back into labels once the row fits — what this
    // asserts is that the glyph state is undone, not how many pixels the stress step
    // above had to spare (the lifecycle group keeps all four of its
    // segments on the row, claunch-ndz47).
    await page.evaluate(() => {
      document.getElementById("term-title").textContent = "s581";
      document.getElementById("term-link").classList.add("hidden");
    });
    await check(1920);
    assert.equal(await page.locator("#term-header").evaluate(el => el.querySelectorAll("[data-toolbar-glyph]").length), 0);
    // The lifecycle group on the row is one Tab stop walked by the arrow keys
    // (claunch-468ec). Kill is checked but disabled here (and resume is
    // disabled), so the stop is the first segment that can be pressed, and
    // the arrows skip the disabled ones. An arrow only moves focus: nothing
    // is pressed by walking the group.
    //
    // The segments are read off the group rather than listed here: hold
    // joined the lifecycle pill in claunch-p6me5, and a harness that keeps
    // its own copy of the list silently measures a group the page no longer
    // has. The order is the group's DOM order.
    const LIFE = await page.evaluate(() =>
      [...document.querySelectorAll("#term-life [role=radio]")].map((b) => b.id));
    assert.deepEqual(LIFE, ["term-resume", "term-idle-hold", "term-pause", "term-kill", "term-archive"],
      `the lifecycle group's segments: ${JSON.stringify(LIFE)}`);
    const stops = () => page.evaluate((ids) => ids.map(id => document.getElementById(id).getAttribute("tabindex")), LIFE);
    const focused = () => page.evaluate(() => document.activeElement && document.activeElement.id);
    await page.evaluate((ids) => {
      window.lifePresses = [];
      for (const id of ids) document.getElementById(id).addEventListener("click", () => window.lifePresses.push(id));
    }, LIFE);
    assert.deepEqual(await stops(), ["-1", "0", "-1", "-1", "-1"], "the stop is the first pressable segment");
    await page.locator("#term-pause").focus();
    const walk = [];
    for (const key of ["ArrowRight", "ArrowRight", "ArrowLeft", "Home", "End", "ArrowDown", "ArrowUp"]) {
      await page.keyboard.press(key);
      walk.push(await focused());
    }
    assert.deepEqual(walk, ["term-archive", "term-idle-hold", "term-archive", "term-idle-hold", "term-archive",
      "term-idle-hold", "term-archive"], `arrow walk: ${JSON.stringify(walk)}`);
    assert.deepEqual(await stops(), ["-1", "-1", "-1", "-1", "0"], "the stop follows focus");
    assert.deepEqual(await page.evaluate(() => window.lifePresses), [], "walking the group presses nothing");
    await page.keyboard.press("Tab");
    assert(!LIFE.includes(await focused()), "Tab leaves the group from its stop");
    await page.keyboard.press("Shift+Tab");
    assert.equal(await focused(), "term-archive", "Shift+Tab comes back to the stop");
    // When the state changes with focus elsewhere, the stop goes to the
    // checked segment where it can be pressed (a paused record: kill).
    await page.evaluate(() => {
      document.activeElement.blur();
      document.getElementById("term-kill").disabled = false;
    });
    await settle();
    assert.deepEqual(await stops(), ["-1", "-1", "-1", "0", "-1"], "the stop moves to the checked segment");
    await page.locator("#term-kill").focus();
    await page.keyboard.press("Enter");
    assert.deepEqual(await page.evaluate(() => window.lifePresses), ["term-kill"], "Enter presses the focused segment");
    // Exercise the real column hierarchy: wrapped tabs above the header,
    // with a resizable detail panel beside the terminal column.
    await page.evaluate(() => {
      const layout = document.getElementById("layout");
      layout.style.display = "flex";
      const main = document.createElement("main");
      main.id = "main";
      const tabs = document.createElement("div");
      tabs.id = "session-tabs";
      tabs.style.height = "130px";
      main.append(tabs, document.getElementById("term-header"));
      const panel = document.createElement("aside");
      panel.id = "sess-view";
      panel.className = "docked";
      layout.replaceChildren(main, panel);
    });
    for (const width of [1200, 1000]) {
      for (const panelWidth of [0, 300, 600]) {
        await page.evaluate(({ width, panelWidth }) => {
          document.getElementById("layout").style.width = `${width}px`;
          const panel = document.getElementById("sess-view");
          panel.classList.toggle("hidden", !panelWidth);
          panel.style.setProperty("--detail-w", `${panelWidth}px`);
        }, { width, panelWidth });
        await settle();
        const headerBox = await page.locator("#term-header").boundingBox();
        const details = await page.locator("#term-details").boundingBox();
        assert(details.x >= headerBox.x && details.x + details.width <= headerBox.x + headerBox.width,
          `details stays within terminal column at ${width}/${panelWidth}`);
        assert(details.y >= headerBox.y && details.y + details.height <= headerBox.y + headerBox.height,
          "details stays within header below wrapped tabs");
        if (panelWidth) {
          const panelBox = await page.locator("#sess-view").boundingBox();
          assert(details.x + details.width <= panelBox.x, "details does not overlap detail panel");
        }
      }
    }
    assert.deepEqual(errors, []);
    console.log("toolbar browser checks passed");
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
