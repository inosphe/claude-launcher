/* The session detail head on a phone (claunch-0a5nk.5): at 600px and below
   the head keeps Spawn and Open terminal on one line and puts Spawn same
   setup, VS Code and Open PR under ⋯.

   Before this, at 390px with a fixture daemon, the head's five verbs took
   three lines above the session's facts (claunch-0a5nk design report, §4).
   What is held here, in a real browser with the shipped UI:

   - at 390px the head's visible verbs are Spawn, Open terminal and ⋯, on
     one line, inside the viewport; the other three are hidden;
   - ⋯ opens a menu holding those three, inside the viewport; the menu stays
     open across a redraw of the panel, and closes on a pick (the pick still
     runs), a tap outside, Escape and a route change;
   - on pages other than the terminal, the terminal's controls and the
     session tab strip are not on screen (design report problem M3);
   - at 820px the head is as before: five verbs, no ⋯. */
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

const DETAIL = "#/s/s101?detail=s101";

// The head's buttons as the reader sees them: label, box, shown or not.
const headButtons = (page) => page.evaluate(() => {
  const head = document.querySelector("#sess-view .sess-head, .sess-head");
  if (!head) return null;
  return {
    page: document.body.dataset.page, vw: innerWidth,
    open: head.classList.contains("more-open"),
    buttons: [...head.querySelectorAll("button.wf-btn")].map((b) => {
      const r = b.getBoundingClientRect();
      return { text: b.textContent.trim(), shown: r.width > 0 && r.height > 0,
               top: Math.round(r.top), left: Math.round(r.left), right: Math.round(r.right),
               expanded: b.getAttribute("aria-expanded") };
    }),
  };
});
const shownLabels = (m) => m.buttons.filter((b) => b.shown).map((b) => b.text);
const MENU = ["Spawn same setup", "VS Code", "Open PR"];

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
    await page.evaluate((h) => { location.hash = h; }, DETAIL);
    await page.waitForSelector(".sess-head .sess-head-more", { timeout: 15000 });
    await page.waitForTimeout(300);

    // One line: Spawn, Open terminal, ⋯.
    let m = await headButtons(page);
    assert.equal(m.page, "session");
    assert.deepEqual(shownLabels(m), ["Spawn", "Open terminal", "⋯"], JSON.stringify(m));
    const line = m.buttons.filter((b) => b.shown);
    assert.equal(new Set(line.map((b) => b.top)).size, 1, `the verbs share a line: ${JSON.stringify(line)}`);
    for (const b of line) assert.ok(b.left >= 0 && b.right <= m.vw, `${b.text} inside the viewport: ${JSON.stringify(b)}`);
    const more = page.locator(".sess-head .sess-head-more");
    assert.equal(await more.getAttribute("aria-expanded"), "false");

    // ⋯ opens the menu with the other three, on screen.
    await more.click();
    m = await headButtons(page);
    assert.equal(m.open, true);
    assert.equal(await more.getAttribute("aria-expanded"), "true");
    for (const label of MENU) {
      const b = m.buttons.find((x) => x.text === label);
      assert.ok(b && b.shown && b.left >= 0 && b.right <= m.vw, `${label} on screen in the menu: ${JSON.stringify(b)}`);
    }

    // A redraw of the panel keeps it open.
    await page.evaluate(() => refreshSession());
    await page.waitForTimeout(400);
    assert.equal((await headButtons(page)).open, true, "the menu survives a redraw");

    // A tap outside closes it.
    await page.mouse.click(195, 600);
    assert.equal((await headButtons(page)).open, false, "outside tap closes the menu");
    // Escape closes it.
    await more.click();
    await page.keyboard.press("Escape");
    assert.equal((await headButtons(page)).open, false, "Escape closes the menu");
    // A pick runs, and closes it.
    await more.click();
    await page.evaluate(() => { window.__pr = []; window.openPrModal = (name) => window.__pr.push(name); });
    await page.locator(".sess-head-extra button", { hasText: "Open PR" }).click();
    assert.deepEqual(await page.evaluate(() => window.__pr), ["s101"], "Open PR reaches its wizard");
    assert.equal((await headButtons(page)).open, false, "a pick closes the menu");
    // A route change closes it.
    await more.click();
    await page.evaluate(() => { location.hash = "#/beads"; });
    await page.waitForTimeout(400);
    await page.evaluate((h) => { location.hash = h; }, DETAIL);
    await page.waitForTimeout(500);
    assert.equal((await headButtons(page)).open, false, "a route change closes the menu");

    // M3: the terminal's controls and the tab strip are on the terminal only.
    const chrome = () => page.evaluate(() => {
      const shown = (id) => { const e = document.getElementById(id); if (!e) return false;
        const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
      return { page: document.body.dataset.page, zoom: shown("m-zoom"), tabs: shown("session-tabs") };
    });
    for (const hash of [DETAIL, "#/beads", "#/new", "#/threads"]) {
      await page.evaluate((h) => { location.hash = h; }, hash);
      await page.waitForTimeout(400);
      const c = await chrome();
      assert.equal(c.zoom, false, `${hash}: the size knob is off the ${c.page} page`);
      assert.equal(c.tabs, false, `${hash}: the session tab strip is off the ${c.page} page`);
    }
    await page.evaluate(() => { location.hash = "#/s/s101"; });
    await page.waitForTimeout(400);
    assert.equal((await chrome()).zoom, true, "the size knob is up on the terminal page");

    // 820px: the head as before.
    await page.setViewportSize({ width: 820, height: 1000 });
    await page.evaluate((h) => { location.hash = h; }, DETAIL);
    await page.waitForTimeout(600);
    m = await headButtons(page);
    assert.deepEqual(shownLabels(m), ["Spawn", "Spawn same setup", "VS Code", "Open PR", "Open terminal"],
      `820px keeps the five verbs: ${JSON.stringify(m)}`);

    assert.deepEqual(errors.filter((e) => /sessHead|SessHeadMore|sess-head/.test(e)), []);
    console.log("mobiledetail_browser: ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
