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
    await page.addScriptTag({ path: path.join(root, "toolbar.js") });
    async function check(width) {
      // Desktop columns can be narrow even while the viewport stays wide;
      // phones already use the separate mobile controls.
      await page.locator("#layout").evaluate((el, width) => { el.style.width = `${width}px`; }, width);
      await page.waitForTimeout(80);
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
      const ordered = geometry.rects.filter(r => r.right > r.left).sort((a, b) => a.left - b.left);
      for (let i = 1; i < ordered.length; i++) assert(ordered[i].left >= ordered[i - 1].right - 1, `overlap at ${width}`);
    }
    for (const width of [1920, 1193, 1000, 800, 600, 480, 360, 240, 180]) {
      await check(width);
      assert(await page.locator("#term-title").evaluate(el => el.clientWidth >= el.scrollWidth),
        `session name stays fully visible at ${width}`);
    }
    await page.locator("#term-more").click();
    await page.locator("#term-overflow-menu button").filter({ hasText: "⬒ run" }).click();
    assert.equal(await page.evaluate(() => window.clicks), 1, "overflow forwards original action");
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
    await page.evaluate(() => { document.getElementById("term-title").textContent = "s581"; });
    await check(1920);
    assert.equal(await page.locator("#term-header").evaluate(el => el.classList.contains("term-compact")), false);
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
        await page.waitForTimeout(80);
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
