/* The rail's page nav folds to one row on a desktop (claunch-cka36).

   Serves the real index.html against a fixture whose API answers nothing,
   and measures the nav in a real browser:
     1. on a desktop rail the nav shows one row: the dock is one row tall and
        only the first row's links are visible, the rest clipped away;
     2. hovering the nav opens every row over the session list, the dock
        keeps its height (the list does not move), and the first row stays
        where it was under the pointer;
     3. moving the pointer away folds it again;
     4. keyboard focus on a link opens it too, and the focus a click leaves
        on a link does not (3 runs right after a click);
     5. on a phone width the whole nav shows, as before. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");

const TYPES = { ".js": "application/javascript", ".css": "text/css", ".html": "text/html" };
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.statusCode = 404;
    res.setHeader("Content-Type", "application/json");
    return res.end("{}");
  }
  const rel = url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/static\//, "");
  const file = path.join(root, rel);
  try {
    const body = fs.readFileSync(file);
    res.setHeader("Content-Type", TYPES[path.extname(file)] || "application/octet-stream");
    res.end(body);
  } catch { res.statusCode = 404; res.end(); }
});

// Which links a person can see: inside the nav's clipped box and the viewport.
function measure() {
  const nav = document.getElementById("rail-nav");
  const dock = document.getElementById("rail-nav-dock");
  const box = nav.getBoundingClientRect();
  const links = Array.from(nav.querySelectorAll("a"));
  const visible = links.filter((a) => {
    const r = a.getBoundingClientRect();
    return r.height > 0 && r.top >= box.top - 0.5 && r.bottom <= box.bottom + 0.5
      && r.bottom <= window.innerHeight + 0.5;
  }).map((a) => a.dataset.page);
  const first = links[0].getBoundingClientRect();
  return {
    total: links.length, visible,
    dockHeight: dock.getBoundingClientRect().height,
    navHeight: box.height,
    firstTop: first.top,
    columns: getComputedStyle(nav).gridTemplateColumns.split(" ").length,
  };
}

async function settle(page) {
  await page.waitForTimeout(250);  // past the 120ms max-height transition
}

(async () => {
  await safeListen(server);
  const base = `http://127.0.0.1:${server.address().port}/`;
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1200, height: 800 } });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto(base);
    await page.mouse.move(1100, 400);
    await settle(page);

    // 1. one row
    const folded = await page.evaluate(measure);
    assert.ok(folded.total > folded.columns, `the nav has more than one row: ${JSON.stringify(folded)}`);
    assert.ok(Math.abs(folded.dockHeight - 47) < 1, `the dock is one row: ${folded.dockHeight}`);
    assert.equal(folded.visible.length, folded.columns, `one row of links shows: ${folded.visible}`);
    assert.equal(folded.visible[0], "observer", "the first row is the nav's first links");

    // 2. hover opens every row above, the dock and the first row stay put
    await page.hover("#rail-nav a[data-page=observer]");
    await settle(page);
    const open = await page.evaluate(measure);
    assert.equal(open.visible.length, open.total, `every link shows on hover: ${open.visible}`);
    assert.ok(open.navHeight > folded.navHeight * 2, "the nav grew");
    assert.equal(open.dockHeight, folded.dockHeight, "the dock (and so the list above) did not move");
    assert.ok(Math.abs(open.firstTop - folded.firstTop) < 1,
      `the first row stays under the pointer: ${folded.firstTop} -> ${open.firstTop}`);
    await page.click("#rail-nav a[data-page=settings]");
    assert.match(page.url(), /#\/settings$/, "a link in an opened row is clickable");

    // 3. leaving folds it
    await page.mouse.move(1100, 400);
    await settle(page);
    assert.equal((await page.evaluate(measure)).visible.length, folded.columns, "folds when the pointer leaves");

    // 4. keyboard focus opens it (the click in 2 left focus on a link and
    //    did not keep it open: only keyboard focus, :focus-visible, does)
    await page.focus("#rail-nav a[data-page=worktrees]");
    await page.keyboard.press("Tab");
    assert.equal(await page.evaluate(() => document.activeElement.dataset.page), "glossary");
    await settle(page);
    assert.equal((await page.evaluate(measure)).visible.length, folded.total, "focus opens it");
    await page.evaluate(() => document.activeElement.blur());

    // 5. phone: the whole nav
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(base);
    await settle(page);
    const phone = await page.evaluate(() => {
      const nav = document.getElementById("rail-nav");
      return { transform: getComputedStyle(nav).transform, overflow: getComputedStyle(nav).overflow,
               position: getComputedStyle(nav).position };
    });
    assert.deepEqual(phone, { transform: "none", overflow: "visible", position: "static" },
      "a phone keeps the plain nav");

    assert.deepEqual(errors, []);
    console.log("railnav_browser: ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
