/* The rail's Chart view (claunch-h0wi6) in a real browser.

   The session rail switches between three views of the same sessions: List,
   Grid and Chart. Chart draws the Chart page's timeline (chart-page.js) in
   the rail and narrows by that timeline's own toolbar, so the rail's state
   filters and its grouping are hidden there. The project stays one pick for
   the whole page: the tabs above the rail narrow the rail's chart, and the
   rail's chart toolbar carries no project picker of its own.

   Serves the real index.html against a fixture whose /api/sessions answers
   three sessions in two projects and whose /api/projects names both, and
   checks:
     1. the view switch offers List, Grid and Chart, in that order;
     2. picking Chart shows the rail chart with one row per session and hides
        the list, the grid, the state filters and the Group by control, while
        the project tabs stay;
     3. the rail chart's toolbar has the timeline's filters (Range) and no
        Project picker, and the rail draws no detail column and no page head;
     4. a project tab narrows the rail chart's rows;
     5. the chart toolbar's own filter (Group by) acts on the rail chart;
     6. clicking a rail row opens its session (#/s/<name>) and marks it;
     7. the view survives a reload;
     8. picking List again brings back the list and the filters and empties
        the rail chart;
     9. the #/chart page still draws its own Project picker while the rail
        is on its Chart view;
    10. rows run parent-before-child: a child sits right under its parent,
        stepped in with a branch mark, in the rail and on the page, and the
        toolbar's 위계 box turns that off and on (claunch-h0wi6.2). */
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
  { name: "gamma", status: "running", created_at: ago(5), project: "other", cwd: "F:/w/repo3" },
  { name: "delta", parent: "alpha", status: "running", created_at: ago(1), project: "default", cwd: "F:/w/repo1" },
];
const PROJECTS = [{ name: "default" }, { name: "other" }];

const TYPES = { ".js": "application/javascript", ".css": "text/css", ".html": "text/html" };
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/sessions") {
      const archived = url.searchParams.get("state") === "archived";
      return res.end(JSON.stringify({ sessions: archived ? [] : CURRENT }));
    }
    if (url.pathname === "/api/projects") {
      return res.end(JSON.stringify({ projects: PROJECTS }));
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

const ROWS = "#session-chart .chart-row:not(.chart-axis):not(.chart-series-row)";
const railRows = (page) => page.$$eval(`${ROWS} .chart-name-text`,
  (els) => els.map((e) => e.textContent).sort());
const shown = (page, sel) => page.$eval(sel, (el) => {
  const r = el.getBoundingClientRect();
  return getComputedStyle(el).display !== "none" && r.width > 0 && r.height > 0;
});

(async () => {
  const port = await safeListen(server);
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto(`http://127.0.0.1:${port}/#/`);
    await page.waitForFunction(() =>
      document.querySelectorAll("#session-list li[data-name]").length >= 1, null, { timeout: 15000 });

    // 1. Three views, in order.
    const views = await page.$$eval('input[name="session-view"]', (els) => els.map((e) => e.value));
    assert.deepEqual(views, ["list", "grid", "chart"]);

    // 2. Chart: the rail chart replaces the list; the filters go, the tabs stay.
    await page.click('#session-view input[value="chart"] + span');
    await page.waitForFunction((sel) => document.querySelectorAll(sel).length >= 4, ROWS, { timeout: 15000 });
    assert.deepEqual(await railRows(page), ["alpha", "beta", "delta", "gamma"]);
    assert.equal(await shown(page, "#session-chart"), true, "the rail chart is shown");
    assert.equal(await shown(page, "#session-list"), false, "the list is hidden");
    assert.equal(await shown(page, "#session-grid"), false, "the grid is hidden");
    assert.equal(await shown(page, "#session-filters"), false, "the state filters are hidden");
    assert.equal(await shown(page, "#session-group-mode"), false, "Group by is hidden");
    assert.equal(await shown(page, "#project-tabs"), true, "the project tabs stay");

    // 3. The timeline's filters, no project picker, no detail, no page head.
    const labels = await page.$$eval("#session-chart .chart-toolbar .chart-seg-label",
      (els) => els.map((e) => e.textContent));
    assert.ok(labels.includes("Range"), labels.join(" | "));
    assert.ok(labels.includes("Group by"), labels.join(" | "));
    assert.ok(!labels.includes("Project"), "no Project picker in the rail: " + labels.join(" | "));
    assert.equal(await page.$("#session-chart .chart-detail"), null);
    assert.equal(await page.$("#session-chart .wf-head"), null);
    // The bars are drawn inside the rail's width, not off its edge.
    const fits = await page.$$eval(ROWS, (rows) => rows.map((r) => {
      const t = r.querySelector(".chart-track").getBoundingClientRect();
      const box = document.getElementById("session-chart").getBoundingClientRect();
      return t.width > 20 && t.left >= box.left - 0.5 && t.right <= box.right + 0.5;
    }));
    assert.ok(fits.every(Boolean), "every track is inside the rail: " + fits.join(","));

    // 4. A project tab narrows the rail chart.
    await page.waitForSelector('#project-tabs .project-tab[data-project="other"]');
    await page.click('#project-tabs .project-tab[data-project="other"]');
    await page.waitForFunction((sel) =>
      [...document.querySelectorAll(sel + " .chart-name-text")].map((e) => e.textContent).join() === "gamma",
    ROWS, { timeout: 5000 });
    await page.click('#project-tabs .project-tab[data-project=""]');
    await page.waitForFunction((sel) => document.querySelectorAll(sel).length === 4, ROWS, { timeout: 5000 });

    // 10. Parent before child: delta (the newest) sits right under alpha,
    // stepped in; the 위계 box gives the flat newest-first order back.
    const order = (root) => page.$$eval(`${root} .chart-row:not(.chart-axis):not(.chart-series-row)`,
      (rows) => rows.map((r) => {
        const n = r.querySelector(".chart-name");
        return { name: r.dataset.name, child: n.classList.contains("chart-child"),
                 branch: !!n.querySelector(".chart-branch"),
                 pad: parseFloat(getComputedStyle(n).paddingLeft) };
      }));
    let rows = await order("#session-chart");
    assert.deepEqual(rows.map((r) => r.name), ["alpha", "delta", "gamma", "beta"], JSON.stringify(rows));
    const child = rows.find((r) => r.name === "delta");
    const root = rows.find((r) => r.name === "alpha");
    assert.ok(child.child && child.branch && child.pad > root.pad, JSON.stringify(rows));
    assert.ok(rows.filter((r) => r.name !== "delta").every((r) => !r.child));
    const treeBox = "#session-chart .chart-tree input";
    assert.equal(await page.$eval(treeBox, (e) => e.checked), true, "위계 is on by default");
    await page.click(treeBox);
    await page.waitForFunction(() => !document.querySelector("#session-chart .chart-name.chart-child"),
      null, { timeout: 5000 });
    rows = await order("#session-chart");
    assert.deepEqual(rows.map((r) => r.name), ["delta", "alpha", "gamma", "beta"], JSON.stringify(rows));
    await page.click(treeBox);
    await page.waitForSelector('#session-chart .chart-row[data-name="delta"] .chart-name.chart-child',
      { timeout: 5000 });

    // 5. The toolbar's own grouping acts on the rail chart.
    await page.click('#session-chart .chart-seg[aria-label="Group by"] .chart-seg-btn:has-text("Workspace")');
    await page.waitForSelector("#session-chart .chart-group-head", { timeout: 5000 });
    await page.click('#session-chart .chart-seg[aria-label="Group by"] .chart-seg-btn:has-text("Off")');
    await page.waitForFunction(() => !document.querySelector("#session-chart .chart-group-head"),
      null, { timeout: 5000 });

    // 6. A row opens its session.
    await page.click(`${ROWS}[data-name="gamma"] .chart-name`);
    await page.waitForFunction(() => location.hash === "#/s/gamma", null, { timeout: 5000 });
    await page.waitForSelector(`${ROWS}.selected[data-name="gamma"]`, { timeout: 8000 });

    // 7. The view survives a reload.
    await page.goto(`http://127.0.0.1:${port}/#/`);
    await page.waitForFunction((sel) => document.querySelectorAll(sel).length >= 4, ROWS, { timeout: 15000 });
    assert.equal(await page.$eval('input[name="session-view"][value="chart"]', (e) => e.checked), true);
    assert.equal(await shown(page, "#session-filters"), false);

    // 9. The page keeps its Project picker while the rail is on Chart.
    await page.goto(`http://127.0.0.1:${port}/#/chart`);
    await page.waitForSelector("#chart-view .chart-toolbar");
    const pageLabels = await page.$$eval("#chart-view .chart-toolbar .chart-seg-label",
      (els) => els.map((e) => e.textContent));
    assert.ok(pageLabels.includes("Project"), pageLabels.join(" | "));
    assert.ok(await page.$("#chart-view .wf-head"), "the page keeps its head");
    // 10, on the page: the same order and the child stepped in.
    await page.waitForSelector('#chart-view .chart-row[data-name="delta"] .chart-name.chart-child',
      { timeout: 5000 });
    const pageRows = await order("#chart-view");
    assert.deepEqual(pageRows.map((r) => r.name), ["alpha", "delta", "gamma", "beta"]);

    // 8. Back to List: the list and the filters return, the rail chart empties.
    await page.click('#session-view input[value="list"] + span');
    await page.waitForFunction(() => !document.querySelector("#session-chart .chart-row"), null, { timeout: 5000 });
    assert.equal(await shown(page, "#session-list"), true);
    assert.equal(await shown(page, "#session-filters"), true);
    assert.equal(await shown(page, "#session-chart"), false);
    // The page's chart is untouched by the rail leaving its Chart view.
    assert.ok(await page.$("#chart-view .chart-row:not(.chart-axis)"), "the page still draws");

    if (process.env.RAILCHART_SHOT) {
      await page.click('#session-view input[value="chart"] + span');
      await page.goto(`http://127.0.0.1:${port}/#/`);
      await page.waitForFunction((sel) => document.querySelectorAll(sel).length >= 3, ROWS, { timeout: 15000 });
      await page.screenshot({ path: process.env.RAILCHART_SHOT, fullPage: false });
    }
    assert.deepEqual(errors, []);
    console.log("ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
