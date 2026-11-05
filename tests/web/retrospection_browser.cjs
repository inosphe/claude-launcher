/* The Retrospection page (#/retrospection, retrospection-page.js) in a real
   browser: the real index.html, app.js and stylesheet against API fixtures
   (claunch-u0wyo).

   The page drew taller than the window with nothing to scroll: `html, body`
   are `overflow: hidden`, `#main` is a flex column, and `#retrospection-view`
   was left out of the rule that makes the glossary-shaped pages their own
   scroll container. Checks:
     1. the view is the scrolling element — `overflow-y: auto`, its content is
        taller than its box, a wheel over the list moves its own `scrollTop`
        and leaves the document at 0;
     2. the toolbar fits the page: it does not overflow the view sideways, and
        the search box keeps a usable width beside the two filters;
     3. 열기 opens the record in the aside with its whole body, the selected
        card is marked, and Escape closes it again;
     4. a record body taller than the window scrolls inside the sticky aside
        rather than running past its bottom edge;
     5. at 600px the two columns stack, the aside stops being sticky, and the
        view still scrolls;
     6. no page errors. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");

const now = Date.now();
const ago = (h) => new Date(now - h * 3600000).toISOString();
const RECORDS = Array.from({ length: 24 }, (_, i) => ({
  id: `rt-${i + 1}`,
  workspace: i % 3 === 0 ? "claunch" : "gds6",
  session: `s${1000 + i}`,
  trigger: i % 4 === 0 ? "manual" : "run",
  run: i % 4 === 0 ? "" : `2026110${i % 9}-1200`,
  workflow: i % 4 === 0 ? "" : "improv-worker-v2",
  issue: `claunch-${1000 + i}`,
  title: `회고 ${i + 1} — 스크롤과 사용성 ${i + 1}`,
  excerpt: `요약 ${i + 1}. `.repeat(12).trim(),
  chars: 400,
  problems: i % 3,
  proposals: i % 2,
  tags: [],
  auto: i % 2 === 0,
  draft: `wd-${i + 1}`,
  created_at: ago(i + 1),
}));
const BODY = ["## 목표", "", ...Array.from({ length: 60 }, (_, i) => `- 항목 ${i + 1}: 본문이 길면 상세 패널 안에서 스크롤되어야 한다.`),
  "", "## 결과", "", "본문 끝."].join("\n");
const TYPES = { ".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8", ".json": "application/json" };

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/sessions") return res.end("{\"sessions\":[]}");
    if (url.pathname === "/api/mesh") return res.end("{\"meshes\":[]}");
    if (url.pathname === "/api/retrospection/facets") {
      return res.end(JSON.stringify({
        workspaces: [{ name: "gds6", count: 16 }, { name: "claunch", count: 8 }],
        sessions: RECORDS.map((r) => ({ name: r.session, workspace: r.workspace, count: 1 })),
      }));
    }
    if (url.pathname === "/api/retrospection") return res.end(JSON.stringify({ records: RECORDS }));
    const one = url.pathname.match(/^\/api\/retrospection\/([^/]+)$/);
    if (one) {
      const row = RECORDS.find((r) => r.id === decodeURIComponent(one[1]));
      if (!row) { res.statusCode = 404; return res.end("{\"error\":\"retrospection record not found\"}"); }
      return res.end(JSON.stringify({ record: { ...row, body: BODY } }));
    }
    res.statusCode = 404;
    return res.end("{}");
  }
  const rel = url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/static\//, "");
  try {
    const body = fs.readFileSync(path.join(root, rel));
    res.setHeader("Content-Type", TYPES[path.extname(rel)] || "application/octet-stream");
    res.end(body);
  } catch { res.statusCode = 404; res.end(); }
});

const viewMetrics = (page) => page.$eval("#retrospection-view", (el) => ({
  overflowY: getComputedStyle(el).overflowY,
  scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight,
  scrollWidth: el.scrollWidth, clientWidth: el.clientWidth,
  docScrollTop: document.scrollingElement.scrollTop,
}));

(async () => {
  const port = await safeListen(server);
  const browser = await chromium.launch({ headless: true,
    ...(process.env.CLAUNCH_CHROMIUM ? { executablePath: process.env.CLAUNCH_CHROMIUM } : {}) });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto(`http://127.0.0.1:${port}/#/retrospection`);
    await page.waitForSelector("#retrospection-view .retrospection-card", { timeout: 20000 });

    // 1. The view is the scroll container.
    const before = await viewMetrics(page);
    assert.equal(before.overflowY, "auto", "the view scrolls, not the page");
    assert.ok(before.scrollHeight > before.clientHeight + 200,
      `content is taller than the box (${before.scrollHeight} vs ${before.clientHeight})`);
    assert.equal(before.docScrollTop, 0);
    await page.mouse.move(420, 420);
    await page.mouse.wheel(0, 900);
    await page.waitForFunction(() => document.getElementById("retrospection-view").scrollTop > 0, null,
      { timeout: 4000 });
    const after = await viewMetrics(page);
    assert.equal(after.docScrollTop, 0, "the document itself never scrolls");

    // Back to the top for the rest of the checks.
    await page.$eval("#retrospection-view", (el) => { el.scrollTop = 0; });

    // 2. The toolbar fits: no sideways overflow, and the search box keeps a
    //    usable width next to the workspace and session pickers.
    assert.ok(before.scrollWidth <= before.clientWidth + 1,
      `the page does not overflow sideways (${before.scrollWidth} vs ${before.clientWidth})`);
    const search = await page.$eval("#retrospection-view .glossary-search input", (el) => el.getBoundingClientRect().width);
    assert.ok(search >= 200, `the search box keeps a usable width (${search}px)`);

    // 3. Opening a record fills the aside, marks the card, and Escape closes it.
    const firstTitle = await page.$eval("#retrospection-view .retrospection-card h4", (el) => el.textContent);
    await page.$eval("#retrospection-view .retrospection-card .glossary-actions button", (el) => el.click());
    await page.waitForSelector("#retrospection-view .retrospection-detail", { timeout: 4000 });
    const detail = await page.$eval("#retrospection-view .retrospection-detail", (el) => ({
      title: el.querySelector("h3").textContent,
      bodyLines: el.querySelectorAll(".retrospection-body .md-list").length,
      top: el.getBoundingClientRect().top,
      overflowY: getComputedStyle(el).overflowY,
      scrollHeight: el.scrollHeight, clientHeight: el.clientHeight,
    }));
    assert.equal(detail.title, firstTitle, "the aside names the record that was opened");
    assert.ok(detail.bodyLines > 0, "the whole body is drawn");
    assert.ok(detail.top >= 0 && detail.top < 800, `the detail starts inside the window (${detail.top})`);
    assert.equal(await page.$eval("#retrospection-view .retrospection-card", (el) => el.getAttribute("aria-current")), "true");
    await page.keyboard.press("Escape");
    await page.waitForSelector("#retrospection-view .retrospection-detail", { state: "detached", timeout: 4000 });

    // 4. A body taller than the window scrolls inside the sticky aside.
    await page.$eval("#retrospection-view .retrospection-card .glossary-actions button", (el) => el.click());
    await page.waitForSelector("#retrospection-view .retrospection-detail", { timeout: 4000 });
    const pane = await page.$eval("#retrospection-view .retrospection-detail", (el) => ({
      overflowY: getComputedStyle(el).overflowY, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight,
      bottom: el.getBoundingClientRect().bottom,
    }));
    assert.equal(pane.overflowY, "auto", "a long record body scrolls inside its pane");
    assert.ok(pane.scrollHeight > pane.clientHeight, "the body is taller than the pane");
    assert.ok(pane.bottom <= 800 + 1, `the pane stays inside the window (${pane.bottom})`);
    await page.keyboard.press("Escape");

    // 5. Narrow: the columns stack and the view still scrolls.
    await page.setViewportSize({ width: 600, height: 900 });
    const narrow = await page.$eval("#retrospection-view", (el) => ({
      overflowY: getComputedStyle(el).overflowY,
      scrollHeight: el.scrollHeight, clientHeight: el.clientHeight,
      columns: getComputedStyle(el.querySelector(".glossary-layout")).gridTemplateColumns,
    }));
    assert.equal(narrow.overflowY, "auto");
    assert.ok(narrow.scrollHeight > narrow.clientHeight, "the narrow view scrolls too");
    assert.ok(!narrow.columns.includes(" "), `the two columns stack at 600px (${narrow.columns})`);

    assert.deepEqual(errors, [], "no page errors");
    console.log("ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((error) => { console.error(error); process.exit(1); });
