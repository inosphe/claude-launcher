/* The Chart page (#/chart, chart-page.js) in a real browser.

   Serves the real index.html against a fixture whose /api/sessions answers
   four sessions in two projects (one archived, asked for only when the
   viewer turns archived on) and whose other endpoints answer nothing, and
   checks:
     1. the nav carries Chart right after Stats;
     2. the page draws one row per current session of every project, each
        with a bar inside its track and a lifetime;
     3. the project picker narrows the rows and moves the shared project
        (the address's ?project=);
     4. "archived 포함" fetches the archived records and adds their rows;
     5. grouping by workspace draws a heading per group, and picking one
        group leaves only its rows. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");

const now = Date.now();
const ago = (h) => new Date(now - h * 3600000).toISOString();
const CURRENT = [
  { name: "alpha", status: "running", created_at: ago(2), project: "default", cwd: "F:/w/repo1", role: "worker" },
  { name: "beta", status: "exited", created_at: ago(30), exited_at: ago(20), project: "default", cwd: "F:/w/repo2" },
  { name: "gamma", status: "running", created_at: ago(5), project: "other", cwd: "F:/w/repo1/.claude/worktrees/x" },
];
const ARCHIVED = [
  { name: "delta", status: "exited", created_at: ago(10), exited_at: ago(8), archived_at: ago(7), project: "default", cwd: "F:/w/repo2" },
];
let archivedAsked = 0;

const TYPES = { ".js": "application/javascript", ".css": "text/css", ".html": "text/html" };
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/sessions") {
      const archived = url.searchParams.get("state") === "archived";
      if (archived) archivedAsked++;
      return res.end(JSON.stringify({ sessions: archived ? ARCHIVED : CURRENT }));
    }
    res.statusCode = 404;
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

const rowNames = (page) => page.$$eval("#chart-view .chart-row:not(.chart-axis) .chart-name-text",
  (els) => els.map((e) => e.textContent).sort());

(async () => {
  const port = await safeListen(server);
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto(`http://127.0.0.1:${port}/#/chart`);
    await page.waitForFunction(() =>
      document.querySelectorAll("#chart-view .chart-row:not(.chart-axis)").length >= 3, null, { timeout: 15000 });

    // 1. Chart sits right after Stats in the nav.
    const pages = await page.$$eval("#rail-nav a", (as) => as.map((a) => a.dataset.page));
    assert.equal(pages[pages.indexOf("stats") + 1], "chart");

    // 2. One row per current session, a bar inside its track, a lifetime.
    assert.deepEqual(await rowNames(page), ["alpha", "beta", "gamma"]);
    const bars = await page.$$eval("#chart-view .chart-row:not(.chart-axis)", (rows) => rows.map((r) => {
      const t = r.querySelector(".chart-track").getBoundingClientRect();
      const b = r.querySelector(".chart-bar").getBoundingClientRect();
      return { inside: b.left >= t.left - 0.5 && b.right <= t.right + 0.5 && b.width > 0,
               life: r.querySelector(".chart-life").textContent };
    }));
    for (const b of bars) {
      assert.ok(b.inside, "bar stays inside its track");
      assert.match(b.life, /^(<1m|\d+[dhm]( \d+[hm])?)$/);
    }
    if (process.env.CHART_SHOT) await page.screenshot({ path: process.env.CHART_SHOT, fullPage: true });

    // 3. The project picker narrows and moves the shared project.
    await page.selectOption("#chart-view .chart-select select", "other");
    await page.waitForFunction(() => location.hash.includes("project=other"));
    assert.deepEqual(await rowNames(page), ["gamma"]);
    await page.selectOption("#chart-view .chart-select select", "");
    assert.deepEqual(await rowNames(page), ["alpha", "beta", "gamma"]);

    // 4. Archived records are fetched on request and drawn.
    assert.equal(archivedAsked, 0);
    await page.check("#chart-view .chart-check input");
    await page.waitForFunction(() => Array.from(
      document.querySelectorAll("#chart-view .chart-name-text")).some((e) => e.textContent === "delta"));
    assert.equal(archivedAsked, 1);

    // 5. Group by workspace: headings, then one group's rows.
    await page.click("#chart-view .chart-seg-btn:text-is('Workspace')");
    const heads = await page.$$eval("#chart-view .chart-group-head", (hs) => hs.map((h) => h.textContent));
    assert.equal(heads.length, 2, heads.join(" | "));
    const selects = await page.$$("#chart-view .chart-select select");
    assert.equal(selects.length, 2);
    const options = await selects[1].$$eval("option", (os) => os.map((o) => o.value));
    await selects[1].selectOption(options.find((v) => v.endsWith("repo1")));
    assert.deepEqual(await rowNames(page), ["alpha", "gamma"]);
    assert.equal(await page.$$eval("#chart-view .chart-group-head", (hs) => hs.length), 0);

    assert.deepEqual(errors, []);
    console.log("ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
