/* On a phone the bottom tab bar replaces the rail's page grid, and #/menu
   reaches every page the grid did (claunch-0a5nk.3).

   Before this, home on a 390x844 screen gave the bottom ~500px to the 29-link
   grid (#rail-nav) and showed two session rows (claunch-0a5nk design report,
   problem M1). What is held here, in a real browser with the shipped UI and a
   fixture daemon:

   - at 390px home shows the grid's replacement: five tabs pinned to the
     bottom edge, inside the viewport, and every fixture session row above
     them;
   - the tabs are down on the terminal page, where the input line and the
     keyboard take the bottom;
   - the lit tab follows the page, and a page without a tab lights 더보기;
   - #/menu lists exactly the links #rail-nav carries, its search narrows the
     list, and a pick opens that page;
   - at 820px (the tablet end of the mobile shell) the grid is kept and the
     tabs stay down. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");

const sessions = ["s101", "s102-api", "s103", "s104-ui", "s105", "s106-rev", "s107", "s108"]
  .map((name, i) => ({
    name, status: i % 4 === 2 ? "exited" : "running", cwd: "/repo/claunch",
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

const shown = (page, sel) => page.evaluate((s) => {
  const e = document.querySelector(s);
  return !!e && e.getBoundingClientRect().height > 0;
}, sel);
const litTab = (page) => page.evaluate(() =>
  [...document.querySelectorAll("#mobile-tabs a.active")].map((a) => a.dataset.page));
const goto = async (page, hash) => {
  await page.evaluate((h) => { location.hash = h; }, hash);
  await page.waitForTimeout(350);
};

(async () => {
  await safeListen(server);
  const browser = await chromium.launch({ headless: true });
  const origin = `http://127.0.0.1:${server.address().port}`;
  try {
    const page = await browser.newPage({
      viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true,
    });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.addInitScript(() => localStorage.setItem("claunch_token:/", "fixture-token"));
    await page.goto(origin + "/#/");
    // The rail's default filter lists the six running fixture sessions.
    await page.waitForFunction(() => document.querySelectorAll("#session-list li.sess-card").length >= 6);
    await page.waitForTimeout(300);

    // Home: the tabs are at the bottom edge, the grid is gone, the list is up.
    const home = await page.evaluate(() => {
      const tabs = document.getElementById("mobile-tabs").getBoundingClientRect();
      const rows = [...document.querySelectorAll("#session-list li.sess-card")]
        .map((li) => li.getBoundingClientRect());
      return {
        vh: window.innerHeight, vw: window.innerWidth,
        tabs: { top: tabs.top, bottom: tabs.bottom, left: tabs.left, right: tabs.right },
        tabCount: document.querySelectorAll("#mobile-tabs a").length,
        tabWidths: [...document.querySelectorAll("#mobile-tabs a")].map((a) => a.getBoundingClientRect().width),
        rowsAbove: rows.filter((r) => r.height > 0 && r.bottom <= tabs.top).length,
        rows: rows.length,
      };
    });
    assert.equal(home.tabCount, 5);
    assert.ok(Math.abs(home.tabs.bottom - home.vh) <= 1, `tabs end at ${home.tabs.bottom}, viewport ${home.vh}`);
    assert.ok(home.tabs.left >= 0 && home.tabs.right <= home.vw, `tabs inside: ${JSON.stringify(home.tabs)}`);
    for (const w of home.tabWidths) assert.ok(w >= 44, `a tab is ${w}px wide`);
    assert.equal(await shown(page, "#rail-nav-dock"), false, "the page grid is down on a phone");
    assert.equal(home.rowsAbove, home.rows, `${home.rowsAbove} of ${home.rows} session rows above the tabs`);
    assert.deepEqual(await litTab(page), ["home"]);

    // The terminal keeps its bottom; a page lights its tab, or 더보기.
    await goto(page, "#/s/s101");
    assert.equal(await shown(page, "#mobile-tabs"), false, "tabs are down on the terminal");
    await goto(page, "#/beads");
    assert.equal(await shown(page, "#mobile-tabs"), true);
    assert.deepEqual(await litTab(page), ["beads"]);
    await goto(page, "#/flows");
    assert.deepEqual(await litTab(page), ["menu"], "a page with no tab lights 더보기");

    // 더보기 opens #/menu, which carries exactly the rail's links.
    await page.click('#mobile-tabs a[data-page="menu"]');
    await page.waitForTimeout(350);
    assert.equal(await page.evaluate(() => document.body.dataset.page), "menu");
    assert.equal(await page.evaluate(() => document.getElementById("m-title").textContent), "더보기");
    const links = await page.evaluate(() => ({
      rail: [...document.querySelectorAll("#rail-nav a")].map((a) => a.getAttribute("href")),
      menu: [...document.querySelectorAll("#menu-list a")].map((a) => a.getAttribute("href")),
      short: [...document.querySelectorAll("#menu-list a")]
        .filter((a) => a.getBoundingClientRect().height < 44).length,
      right: Math.max(...[...document.querySelectorAll("#menu-list a")]
        .map((a) => a.getBoundingClientRect().right)),
    }));
    assert.ok(links.rail.length >= 29, `the rail has ${links.rail.length} links`);
    assert.deepEqual(links.menu, links.rail, "#/menu lists the rail's pages, in its order");
    assert.equal(links.short, 0, "every menu row is at least 44px tall");
    assert.ok(links.right <= 390, `menu rows end at ${links.right}px`);

    // Search narrows the list; a pick opens the page.
    await page.fill("#menu-search", "gloss");
    const left = await page.evaluate(() =>
      [...document.querySelectorAll("#menu-list a")].filter((a) => !a.hidden).map((a) => a.dataset.page));
    assert.deepEqual(left, ["glossary"]);
    await page.click('#menu-list a[data-page="glossary"]');
    await page.waitForTimeout(350);
    assert.equal(await page.evaluate(() => document.body.dataset.page), "glossary");
    assert.deepEqual(await litTab(page), ["menu"]);
    // Coming back, the list is whole again.
    await goto(page, "#/menu");
    assert.equal(await page.evaluate(() =>
      [...document.querySelectorAll("#menu-list a")].filter((a) => a.hidden).length), 0);

    // The 세션 tab is the rail.
    await page.click('#mobile-tabs a[data-page="home"]');
    await page.waitForTimeout(350);
    assert.equal(await page.evaluate(() => document.body.classList.contains("rail-open")), true);

    assert.deepEqual(errors.filter((e) => /menu|mobile-tabs|openMenuPage|filterMenuPage/.test(e)), []);
    await page.close();

    // The tablet end of the mobile shell keeps the grid.
    const tab = await browser.newPage({ viewport: { width: 820, height: 1180 }, hasTouch: true, isMobile: true });
    await tab.addInitScript(() => localStorage.setItem("claunch_token:/", "fixture-token"));
    await tab.goto(origin + "/#/");
    await tab.waitForTimeout(800);
    assert.equal(await shown(tab, "#mobile-tabs"), false, "no tabs at 820px");
    assert.equal(await shown(tab, "#rail-nav-dock"), true, "the grid stays at 820px");
    await tab.close();
    console.log("mobiletabs_browser: ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
