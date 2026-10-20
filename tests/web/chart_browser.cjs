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
        group leaves only its rows;
     6. every row names a role (its own, else "free-role");
     7. clicking a row highlights it and fills the detail column on the
        right, and clicking it again clears both;
     8. dragging the divider resizes the two columns, and the new split
        survives a reload. */
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
// Two v2 runs: alpha's clock is old enough to be marked, gamma's is not.
const RAIL = {
  alpha: { workflow: "improv-worker-v2", status: "running", round: 2, current: ["작업 실행"],
           gated: false, owed: [], answers: [], urgent_asks: [], updated: ago(0.75),
           entered_at: (now - 45 * 60000) / 1000 },
  gamma: { workflow: "improv-worker-v2", status: "running", round: 1, current: ["intake"],
           gated: false, owed: [], answers: [], urgent_asks: [], updated: ago(1 / 60),
           entered_at: (now - 60000) / 1000 },
};
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
    if (url.pathname === "/api/cflow2/rail") {
      return res.end(JSON.stringify({ rows: RAIL }));
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
    // 2b. The cflow column carries the v2 run's state and how long since it
    // moved; a run in one task past the stall window is marked, a fresh one
    // is not, and a session without a run shows no age.
    await page.waitForFunction(() =>
      document.querySelectorAll("#chart-view .chart-flow-age").length >= 2, null, { timeout: 15000 });
    const flowRows = await page.$$eval("#chart-view .chart-row:not(.chart-axis)", (rows) => rows.map((r) => {
      const f = r.querySelector(".chart-flow");
      return {
        name: r.querySelector(".chart-name-text").textContent,
        text: f ? f.textContent : "",
        stalled: !!(f && f.classList.contains("stalled")),
        age: (r.querySelector(".chart-flow-age") || {}).textContent || "",
      };
    }));
    const alpha = flowRows.find((r) => r.name === "alpha");
    const gamma = flowRows.find((r) => r.name === "gamma");
    assert.match(alpha.text, /improv-worker-v2 · running · round 2 · 작업 실행/);
    assert.equal(alpha.age, "45m");
    assert.equal(alpha.stalled, true, "a run in one task for 45m is marked");
    assert.equal(gamma.age, "1m");
    assert.equal(gamma.stalled, false, "a run that moved a minute ago is not marked");
    assert.equal(flowRows.find((r) => r.name === "beta").age, "", "no run, no age");
    const stat = await page.$$eval("#chart-view .chart-stat", (els) => els.map((e) => e.textContent));
    assert.ok(stat.includes("1정지 의심"), stat.join(" | "));

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

    // 6. Every row names a role.
    await (await page.$$("#chart-view .chart-select select"))[1].selectOption("");
    const roles = await page.$$eval("#chart-view .chart-row:not(.chart-axis)", (rows) =>
      Object.fromEntries(rows.map((r) => [r.querySelector(".chart-name-text").textContent,
                                          r.querySelector(".chart-role").textContent])));
    assert.equal(roles.alpha, "worker");
    assert.equal(roles.beta, "free-role");

    // 7. A click highlights the row and shows its detail on the right.
    const detailText = () => page.$eval("#chart-view .chart-detail", (d) => d.textContent);
    assert.match(await detailText(), /클릭하면/);
    await page.click("#chart-view .chart-row[data-name='alpha'] .chart-life");
    await page.waitForSelector("#chart-view .chart-row.selected[data-name='alpha']");
    assert.equal(await page.$$eval("#chart-view .chart-row.selected", (rs) => rs.length), 1);
    const side = await page.$eval("#chart-view .chart-detail", (d) => ({
      name: d.querySelector(".chart-detail-name").textContent,
      text: d.textContent,
      href: d.querySelector(".chart-detail-open").getAttribute("href"),
      right: d.getBoundingClientRect().left,
      mainRight: document.querySelector("#chart-view .chart-main").getBoundingClientRect().right,
    }));
    assert.equal(side.name, "alpha");
    assert.match(side.text, /worker/);
    assert.match(side.text, /F:\/w\/repo1/);
    assert.equal(side.href, "#/s/alpha");
    assert.ok(side.right >= side.mainRight, "detail sits right of the timeline");
    // The pick survives the page's own redraw.
    await page.waitForTimeout(5600);
    assert.equal(await page.$eval("#chart-view .chart-row.selected", (r) => r.dataset.name), "alpha");
    await page.click("#chart-view .chart-row[data-name='alpha'] .chart-life");
    assert.equal(await page.$$eval("#chart-view .chart-row.selected", (rs) => rs.length), 0);
    assert.match(await detailText(), /클릭하면/);

    // 8. Dragging the divider resizes the columns, and the split is kept.
    const width = () => page.$eval("#chart-view .chart-main", (m) => m.getBoundingClientRect().width);
    const before = await width();
    const d = await page.$eval("#chart-view .chart-divider", (e) => {
      const b = e.getBoundingClientRect();
      const s = document.querySelector("#chart-view .chart-split").getBoundingClientRect();
      return { x: b.left + b.width / 2, y: b.top + 40, left: s.left, w: s.width };
    });
    await page.mouse.move(d.x, d.y);
    await page.mouse.down();
    await page.mouse.move(d.left + d.w * 0.45, d.y, { steps: 5 });
    await page.mouse.up();
    const after = await width();
    assert.ok(Math.abs(after - d.w * 0.45) < 12, `main is ${after}px of ${d.w}px`);
    assert.ok(after < before - 50);
    await page.reload();
    await page.waitForSelector("#chart-view .chart-main");
    assert.ok(Math.abs((await width()) - after) < 12, "split kept across a reload");

    assert.deepEqual(errors, []);
    console.log("ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
