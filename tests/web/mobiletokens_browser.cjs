/* The phone's touch and type floor (claunch-0a5nk.4): at 600px and below the
   tokens --tap-min 44px, --fs-body 13px and --fs-meta 12px are defined, and
   the four pages the design report measured use them.

   Before this, at 390px with the same kind of fixture, the controls under
   44px were home 33/38, session detail 44/52, new 43/51 and beads 33/38,
   and 36 of 66 text runs on home were under 12px (the filter counts at 9px).
   What is held here, in a real browser with the shipped UI:

   - the tokens resolve at 390px and are absent at 820px;
   - on home, detail, new and beads, at most two visible controls are under
     44px on their short side (checkboxes and radios are left out: the label
     around them is what a finger hits), at most one text run is under 12px,
     and the page does not scroll sideways;
   - a chip row that no longer fits at 44px a chip stays on one line and
     scrolls inside itself (the session filters, the beads status row). */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");

const names = ["s101", "s102-api", "s103", "s104-ui", "s105", "s106-rev", "s107", "s108"];
const sessions = names.map((name, i) => ({
  name, status: i % 3 === 2 ? "exited" : "running", cwd: "/repo/claunch",
  harness: "claude", profile: "work", role: i === 5 ? "reviewer" : "worker",
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

const PAGES = [["home", "#/"], ["detail", "#/s/s101?detail=s101"], ["new", "#/new"], ["beads", "#/beads"]];

function measure() {
  const vis = (el) => {
    const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== "hidden" && cs.display !== "none"
      && r.bottom > 0 && r.top < innerHeight * 3;
  };
  const name = (el) => (el.id ? "#" + el.id : el.tagName.toLowerCase()
    + (typeof el.className === "string" && el.className.trim() ? "." + el.className.trim().split(/\s+/)[0] : ""));
  const taps = [...document.querySelectorAll(
    "button, a[href], input:not([type=hidden]):not([type=checkbox]):not([type=radio]), select, textarea, [role=button], summary",
  )].filter(vis);
  const small = taps.filter((el) => {
    const r = el.getBoundingClientRect(); return Math.min(r.width, r.height) < 44;
  }).map((el) => { const r = el.getBoundingClientRect(); return `${name(el)} ${Math.round(r.width)}x${Math.round(r.height)}`; });
  const texts = [...document.querySelectorAll("body *")].filter(vis)
    .filter((el) => [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim()));
  const tinyText = texts.filter((el) => parseFloat(getComputedStyle(el).fontSize) < 12)
    .map((el) => `${name(el)} ${getComputedStyle(el).fontSize}`);
  return {
    page: document.body.dataset.page, taps: taps.length, small, texts: texts.length, tinyText,
    scrollW: document.documentElement.scrollWidth, vw: innerWidth,
  };
}

const oneLine = (page, sel) => page.evaluate((s) => {
  const row = document.querySelector(s);
  if (!row) return null;
  const kids = [...row.children].filter((k) => k.getBoundingClientRect().width > 0);
  const tops = new Set(kids.map((k) => Math.round(k.getBoundingClientRect().top)));
  return { kids: kids.length, lines: tops.size, scroll: row.scrollWidth, client: row.clientWidth,
           overflowX: getComputedStyle(row).overflowX };
}, sel);

(async () => {
  await safeListen(server);
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({
      viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true,
    });
    await page.addInitScript(() => localStorage.setItem("claunch_token:/", "fixture-token"));
    const origin = `http://127.0.0.1:${server.address().port}`;
    await page.goto(origin + "/#/");
    await page.waitForFunction(() => document.querySelectorAll("li.sess-card").length >= 6, null, { timeout: 30000 });

    const tokens = () => page.evaluate(() => {
      const cs = getComputedStyle(document.documentElement);
      return ["--tap-min", "--fs-body", "--fs-meta"].map((t) => cs.getPropertyValue(t).trim());
    });
    assert.deepEqual(await tokens(), ["44px", "13px", "12px"], "the tokens at 390px");

    for (const [label, hash] of PAGES) {
      await page.evaluate((h) => { location.hash = h; }, hash);
      await page.waitForTimeout(700);
      const m = await page.evaluate(measure);
      assert.ok(m.taps >= 10, `${label}: only ${m.taps} controls rendered`);
      assert.ok(m.small.length <= 2, `${label}: ${m.small.length}/${m.taps} controls under 44px: ${m.small.join(", ")}`);
      assert.ok(m.tinyText.length <= 1, `${label}: ${m.tinyText.length}/${m.texts} text runs under 12px: ${m.tinyText.join(", ")}`);
      assert.ok(m.scrollW <= m.vw, `${label}: the page scrolls sideways ${m.scrollW} > ${m.vw}`);
    }

    // The session filters: one line, scrolling inside the row.
    await page.evaluate(() => { location.hash = "#/"; });
    await page.waitForTimeout(500);
    const filters = await oneLine(page, ".session-filter-buttons");
    assert.ok(filters && filters.kids >= 6, `session filters rendered: ${JSON.stringify(filters)}`);
    assert.equal(filters.lines, 1, `session filters on one line: ${JSON.stringify(filters)}`);
    assert.ok(filters.scroll > filters.client && filters.overflowX === "auto",
      `session filters scroll inside the row: ${JSON.stringify(filters)}`);

    // The beads status row: one line, scrolling inside the row.
    await page.evaluate(() => { location.hash = "#/beads"; });
    await page.waitForTimeout(700);
    const states = await oneLine(page, ".beads-status-filters");
    assert.ok(states && states.kids >= 4, `beads status row rendered: ${JSON.stringify(states)}`);
    assert.equal(states.lines, 1, `beads status row on one line: ${JSON.stringify(states)}`);
    assert.ok(states.scroll > states.client && states.overflowX === "auto",
      `beads status row scrolls inside the row: ${JSON.stringify(states)}`);

    // Above the phone step the tokens are not defined.
    await page.setViewportSize({ width: 820, height: 1000 });
    await page.waitForTimeout(300);
    assert.deepEqual(await tokens(), ["", "", ""], "no tokens at 820px");

    console.log("mobiletokens_browser: ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
