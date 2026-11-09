/* The phone's top bar (#mobile-top) fits a 390px screen on every page, and
   the moves it no longer carries are one tap away under ⋯ (claunch-0a5nk.2).

   Before this the bar carried the size knob, pause, kill, archive and the
   link-panel chip on every page, and on 24 of 25 pages its controls ran
   past the right edge of a 390px viewport (measured with a fixture daemon,
   claunch-0a5nk design report). What is held here, in a real browser with
   the shipped UI and a fixture daemon:

   - on each page, every control on the bar ends inside the viewport and the
     title keeps at least 40px;
   - the size knob is up on the terminal page only;
   - ⋯ opens a menu that holds kill, archive and the connection chip, and the
     menu closes on a pick, on a tap outside, on Escape and on a route
     change. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");

const sessions = ["s101", "s102-api", "s103"].map((name, i) => ({
  name, status: i === 2 ? "exited" : "running", cwd: "/repo/claunch",
  harness: "claude", profile: "work", role: "worker",
  created_at: "2026-11-06T08:00:00Z", task: "fixture task " + name,
}));

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
    let data = {};
    if (url.pathname === "/api/daemon") data = { version: "fixture", boot_id: "test" };
    else if (url.pathname === "/api/sessions") data = { sessions, llm_configured: false };
    else if (url.pathname.endsWith("/capture")) data = { lines: ["Session snapshot"] };
    else if (/^\/api\/sessions\/[^/]+\/meta$/.test(url.pathname)) {
      const name = decodeURIComponent(url.pathname.split("/")[3]);
      data = { session: sessions.find((s) => s.name === name) || { name } };
    }
    else if (url.pathname === "/api/observer") data = { sessions: [], enabled: false };
    else if (/^\/api\/(profiles|workspaces|mesh|cflow|harnesses|roles)$/.test(url.pathname)) data = [];
    return res.end(JSON.stringify(data));
  }
  const file = path.join(root, url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/static\//, ""));
  try {
    res.setHeader("Content-Type", file.endsWith(".js") ? "application/javascript"
      : file.endsWith(".css") ? "text/css" : "text/html");
    res.end(fs.readFileSync(file));
  } catch { res.statusCode = 404; res.end(); }
});

// The pages the design report measured, with the session open first so the
// bar carries the session's controls wherever it can.
const PAGES = [
  "#/s/s101", "#/s/s101?detail=s101", "#/new", "#/beads", "#/chart", "#/threads",
  "#/observer", "#/chat", "#/mesh", "#/flows", "#/window", "#/stats", "#/worktrees",
  "#/artifacts", "#/pr", "#/hive", "#/plan", "#/settings", "#/glossary",
  "#/knowledge", "#/fleet", "#/spec", "#/sims", "#/vm",
];

const barBoxes = (page) => page.evaluate(() => {
  const bar = document.getElementById("mobile-top");
  const shown = [...bar.children].filter((el) => {
    const b = el.getBoundingClientRect();
    return b.width > 0 && el.id !== "m-more-menu";
  });
  return {
    page: document.body.dataset.page,
    viewport: window.innerWidth,
    barScroll: bar.scrollWidth, barClient: bar.clientWidth,
    title: document.getElementById("m-title").getBoundingClientRect().width,
    zoom: document.getElementById("m-zoom").getBoundingClientRect().width > 0,
    right: shown.map((el) => [el.id, Math.round(el.getBoundingClientRect().right)]),
  };
});

(async () => {
  await safeListen(server);
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({
      viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true,
    });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.addInitScript(() => localStorage.setItem("claunch_token:/", "fixture-token"));
    const origin = `http://127.0.0.1:${server.address().port}`;
    await page.goto(origin + "/#/");
    await page.waitForTimeout(800);

    for (const hash of PAGES) {
      await page.evaluate((h) => { location.hash = h; }, hash);
      await page.waitForTimeout(400);
      const m = await barBoxes(page);
      for (const [id, right] of m.right) {
        assert.ok(right <= m.viewport, `${hash}: #${id} ends at ${right}px, past ${m.viewport}px`);
      }
      assert.ok(m.barScroll <= m.barClient, `${hash}: the bar scrolls ${m.barScroll} > ${m.barClient}`);
      assert.ok(m.title >= 40, `${hash}: the title is squeezed to ${m.title}px`);
      assert.equal(m.zoom, m.page === "terminal", `${hash}: size knob up=${m.zoom} on page ${m.page}`);
    }

    // ⋯ opens the menu; the session's rare moves and the chip are in it.
    await page.evaluate(() => { location.hash = "#/s/s101"; });
    await page.waitForTimeout(400);
    const more = page.locator("#m-more");
    assert.equal(await more.getAttribute("aria-expanded"), "false");
    await more.click();
    assert.equal(await more.getAttribute("aria-expanded"), "true");
    for (const id of ["m-kill", "m-archive", "m-conn"]) {
      assert.equal(await page.locator(`#${id}`).isVisible(), true, `#${id} in the open menu`);
    }
    const menu = await page.evaluate(() => {
      const b = document.getElementById("m-more-menu").getBoundingClientRect();
      return { left: b.left, right: b.right, top: b.top,
               barBottom: document.getElementById("mobile-top").getBoundingClientRect().bottom };
    });
    assert.ok(menu.left >= 0 && menu.right <= 390, `the menu sits on screen: ${JSON.stringify(menu)}`);
    assert.ok(menu.top >= menu.barBottom - 1, `the menu hangs under the bar: ${JSON.stringify(menu)}`);

    // A tap outside closes it.
    await page.mouse.click(195, 600);
    assert.equal(await page.locator("#m-more-menu").isVisible(), false, "outside tap closes the menu");
    // Escape closes it.
    await more.click();
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#m-more-menu").isVisible(), false, "Escape closes the menu");
    // A route change closes it.
    await more.click();
    await page.evaluate(() => { location.hash = "#/beads"; });
    await page.waitForTimeout(300);
    assert.equal(await page.locator("#m-more-menu").isVisible(), false, "a route change closes the menu");
    // A pick closes it, and the pick reaches the header's control it mirrors.
    await page.evaluate(() => { location.hash = "#/s/s101"; });
    await page.waitForTimeout(300);
    await more.click();
    await page.evaluate(() => {
      window.__archived = 0;
      document.getElementById("term-archive").addEventListener("click", (e) => {
        e.stopImmediatePropagation(); window.__archived += 1;
      }, { capture: true });
    });
    await page.click("#m-archive");
    assert.equal(await page.evaluate(() => window.__archived), 1, "archive reaches #term-archive");
    assert.equal(await page.locator("#m-more-menu").isVisible(), false, "a pick closes the menu");

    assert.deepEqual(errors.filter((e) => /m-more|mobile-top|setMobileMore/.test(e)), []);
    console.log("mobiletop_browser: ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
