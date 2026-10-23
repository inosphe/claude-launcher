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
     4b. "killed 제외" drops the killed rows and the state survives a reload;
     5. grouping by workspace draws a heading per group, and picking one
        group leaves only its rows;
     6. every row names a role (its own, else "free-role");
     7. clicking a row highlights it and fills the detail column on the
        right, and clicking it again clears both;
     8. dragging the divider resizes the two columns, and the new split
        survives a reload;
     9. the token usage block shows the window's figures and a series row whose
        columns sit on the bars' axis (claunch-x15q5);
    10. each row prints its own window figure in the token column -- one grid
        column with the header and the series row, a dash where the ledger has
        no record -- and the detail column lists the total with its four
        parts (claunch-mlrga). */
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
const seriesAsked = [];

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
    if (url.pathname === "/api/stats/series") {
      seriesAsked.push(Object.fromEntries(url.searchParams));
      // Two buckets of a five-minute series: busy two hours ago, quiet now.
      const width = 300, start = now - 2 * 3600000;
      const points = [];
      for (let t = start; t <= now; t += width * 1000) {
        const busy = t === start;
        points.push({ t, input: busy ? 100 : 0, cache_read: busy ? 2000 : 0,
                      cache_write: 0, output: busy ? 50 : 0,
                      total: busy ? 2150 : 0, requests: busy ? 2 : 0 });
      }
      return res.end(JSON.stringify({
        window: url.searchParams.get("window"), bucket_s: width,
        start, until: now, points,
        totals: { input: 100, cache_read: 2000, cache_write: 0, output: 50,
                  total: 2150, requests: 2 },
        cache_share: 2000 / 2150, sessions: 3, unmetered: 1,
        // The same pass carries each row's own figure: alpha spent the busy
        // bucket, gamma is a session the ledger never saw.
        by_session: {
          alpha: { input: 100, cache_read: 2000, cache_write: 0, output: 50,
                   total: 2150, requests: 2 },
          beta: { input: 10000, cache_read: 0, cache_write: 0, output: 0,
                  total: 12500, requests: 9 },
        },
        ledger_since: "2026-10-01",
      }));
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

const rowNames = (page) => page.$$eval("#chart-view .chart-row:not(.chart-axis):not(.chart-series-row) .chart-name-text",
  (els) => els.map((e) => e.textContent).sort());

/* The axis labels a viewer can read, left to right, with the column's edges. */
const visibleTicks = (page) => page.$$eval("#chart-view .chart-axis .chart-track", (tracks) => {
  const t = tracks[0].getBoundingClientRect();
  return Array.from(tracks[0].querySelectorAll(".chart-tick"), (e) => {
    const b = e.getBoundingClientRect();
    return { text: e.textContent, left: b.left, right: b.right,
             visible: getComputedStyle(e).visibility !== "hidden",
             track: { left: t.left, right: t.right } };
  }).filter((x) => x.visible).sort((a, b) => a.left - b.left);
});

/* The labels must not touch each other, and must stay inside the column. */
function assertTicksApart(ticks, where) {
  assert.ok(ticks.length >= 2, `${where}: visible ticks: ${ticks.map((t) => t.text).join(" | ")}`);
  for (let i = 1; i < ticks.length; i++) {
    assert.ok(ticks[i].left >= ticks[i - 1].right,
      `${where}: "${ticks[i - 1].text}" [${ticks[i - 1].left}, ${ticks[i - 1].right}] touches ` +
      `"${ticks[i].text}" [${ticks[i].left}, ${ticks[i].right}]`);
  }
  for (const t of ticks) {
    assert.ok(t.left >= t.track.left - 0.5 && t.right <= t.track.right + 0.5,
      `${where}: "${t.text}" is cut by the column`);
  }
}

(async () => {
  const port = await safeListen(server);
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto(`http://127.0.0.1:${port}/#/chart`);
    await page.waitForFunction(() =>
      document.querySelectorAll("#chart-view .chart-row:not(.chart-axis):not(.chart-series-row)").length >= 3, null, { timeout: 15000 });

    // 1. Chart sits right after Stats in the nav.
    const pages = await page.$$eval("#rail-nav a", (as) => as.map((a) => a.dataset.page));
    assert.equal(pages[pages.indexOf("stats") + 1], "chart");

    // 2. One row per current session, a bar inside its track, a lifetime.
    assert.deepEqual(await rowNames(page), ["alpha", "beta", "gamma"]);
    const bars = await page.$$eval("#chart-view .chart-row:not(.chart-axis):not(.chart-series-row)", (rows) => rows.map((r) => {
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
    const flowRows = await page.$$eval("#chart-view .chart-row:not(.chart-axis):not(.chart-series-row)", (rows) => rows.map((r) => {
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
    assert.equal(alpha.age, "⚠ 45m", "a stalled row carries the mark with its age");
    assert.equal(alpha.stalled, true, "a run in one task for 45m is marked");
    assert.equal(gamma.age, "1m");
    assert.equal(gamma.stalled, false, "a run that moved a minute ago is not marked");
    assert.equal(flowRows.find((r) => r.name === "beta").age, "", "no run, no age");
    const stat = await page.$$eval("#chart-view .chart-stat", (els) => els.map((e) => e.textContent));
    assert.ok(stat.includes("1정지 의심"), stat.join(" | "));

    // 2c. No two visible axis labels touch, and none is cut by the column.
    // At this viewport the timeline column is narrow (the detail pane takes
    // the rest), so labels that fit their gap as a percentage of the page do
    // not fit the column: "1d 전" and "12h 전" read as one before this check.
    assertTicksApart(await visibleTicks(page), "log axis");
    // The same holds for the linear axis, whose labels are clock times.
    await page.click("#chart-view .chart-seg-btn:text-is('선형')");
    assertTicksApart(await visibleTicks(page), "linear axis");
    await page.click("#chart-view .chart-seg-btn:text-is('로그')");

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
      document.querySelectorAll("#chart-view .chart-row:not(.chart-series-row) .chart-name-text")).some((e) => e.textContent === "delta"));
    assert.equal(archivedAsked, 1);

    // 4b. "killed 제외" folds the killed rows away and remembers the pick.
    // beta is the fixture's killed session (exited, not held/paused/archived).
    const killedRow = () => page.$$eval(
      "#chart-view .chart-row:not(.chart-series-row) .chart-name-text",
      (es) => es.map((e) => e.textContent));
    const killedBoxes = await page.$$("#chart-view .chart-check input");
    assert.equal(killedBoxes.length, 2, "archived 포함 and killed 제외 sit side by side");
    await page.check("#chart-view .chart-check:has-text('killed 제외') input");
    await page.waitForFunction(() => !Array.from(
      document.querySelectorAll("#chart-view .chart-row:not(.chart-series-row) .chart-name-text")).some((e) => e.textContent === "beta"));
    assert.deepEqual(await killedRow(), ["alpha", "gamma", "delta"]);
    await page.reload();
    await page.waitForSelector("#chart-view .chart-main");
    assert.ok(await page.$eval("#chart-view .chart-check:has-text('killed 제외') input",
      (b) => b.checked), "killed 제외 is still checked after a reload");
    await page.waitForFunction(() => !Array.from(
      document.querySelectorAll("#chart-view .chart-row:not(.chart-series-row) .chart-name-text")).some((e) => e.textContent === "beta"));
    await page.waitForFunction(() => Array.from(
      document.querySelectorAll("#chart-view .chart-row:not(.chart-series-row) .chart-name-text")).some((e) => e.textContent === "delta"));
    assert.deepEqual(await killedRow(), ["alpha", "gamma", "delta"],
      "killed 제외 survives a reload");
    await page.uncheck("#chart-view .chart-check:has-text('killed 제외') input");
    await page.waitForFunction(() => Array.from(
      document.querySelectorAll("#chart-view .chart-row:not(.chart-series-row) .chart-name-text")).some((e) => e.textContent === "beta"));
    assert.deepEqual(await killedRow(), ["alpha", "gamma", "delta", "beta"]);

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
    const roles = await page.$$eval("#chart-view .chart-row:not(.chart-axis):not(.chart-series-row)", (rows) =>
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

    // 9. The token usage block: the window's figures, the four parts named,
    // a series row drawn on the timeline's own axis, and the query the
    // page's picks make.
    await page.waitForSelector("#chart-view .chart-usage");
    await page.waitForFunction(() =>
      document.querySelectorAll("#chart-view .chart-series-row rect.chart-series-area").length > 0,
      null, { timeout: 15000 });
    const usage = await page.$eval("#chart-view .chart-usage", (b) => ({
      text: b.textContent,
      stats: Array.from(b.querySelectorAll(".chart-usage-stat"),
        (s) => s.querySelector(".chart-usage-label").textContent + "=" +
               s.querySelector(".chart-usage-value").textContent),
      legend: Array.from(b.querySelectorAll(".chart-usage-legend-item"), (e) => e.textContent),
    }));
    assert.ok(usage.stats.includes("총 토큰=2.15k"), usage.stats.join(" | "));
    assert.ok(usage.stats.includes("요청=2"), usage.stats.join(" | "));
    assert.ok(usage.stats.some((s) => s.startsWith("캐시 비중=93.0%")), usage.stats.join(" | "));
    assert.ok(usage.legend.some((l) => l.startsWith("cache read 2.00k")), usage.legend.join(" | "));
    assert.ok(usage.legend.some((l) => l.startsWith("output 50")), usage.legend.join(" | "));
    assert.match(usage.text, /버킷/);
    // The series row's columns land on the same instants the bars do: the
    // busy bucket, two hours back, sits where a session created then starts.
    const geom = await page.evaluate(() => {
      const track = document.querySelector("#chart-view .chart-series-row .chart-track")
        .getBoundingClientRect();
      const rects = Array.from(
        document.querySelectorAll("#chart-view .chart-series-row rect.chart-series-area"),
        (r) => { const b = r.getBoundingClientRect();
                 return { left: b.left, width: b.width, height: b.height }; });
      const alpha = document.querySelector("#chart-view .chart-row[data-name='alpha'] .chart-bar")
        .getBoundingClientRect();
      return { track, rects, alpha };
    });
    assert.ok(geom.rects.some((r) => r.height > 4), "the busy bucket has a height");
    assert.ok(geom.rects.some((r) => r.height < 1 || r.width < 1) ||
      geom.rects.length < 24, "quiet buckets draw nothing");
    for (const r of geom.rects) {
      assert.ok(r.left >= geom.track.left - 2 && r.left + r.width <= geom.track.right + 2,
        "a column stays inside the track");
    }
    const asked = seriesAsked[seriesAsked.length - 1];
    assert.equal(asked.window, "7d", "the page's range is the query's window");
    // The query follows the page's own two boxes, whatever state the earlier
    // steps left them in (an assertion on a hardcoded pair tested the order
    // of the steps, not the page).
    const boxes = await page.$$eval("#chart-view .chart-check input",
      (els) => els.map((e) => e.checked));
    assert.equal(asked.archived, boxes[0] ? "1" : "0", "archived box");
    assert.equal(asked.killed, boxes[1] ? "0" : "1", "killed box");

    // 10. Each row prints its own window figure in the token column -- the
    // number the Stats page's table prints for that session (claunch-mlrga) --
    // and a session the ledger never saw is a dash, not a zero.
    // The archived rows are fetched on their own after a reload, so wait for
    // theirs as well as alpha's before reading the column.
    await page.waitForFunction(() =>
      document.querySelector("#chart-view .chart-row[data-name='alpha'] .chart-tokens")
        ?.textContent.trim() === "2.15k" &&
      document.querySelector("#chart-view .chart-row[data-name='delta'] .chart-tokens"),
      null, { timeout: 15000 });
    const cells = await page.$$eval("#chart-view .chart-row:not(.chart-axis):not(.chart-series-row)",
      (rows) => Object.fromEntries(rows.map((r) => [
        r.dataset.name, r.querySelector(".chart-tokens")?.textContent.trim()])));
    assert.equal(cells.alpha, "2.15k");
    assert.equal(cells.beta, "12.5k", "the row sums are the row's own figure");
    assert.equal(cells.gamma, "—", "a session the ledger has no record of is a dash");
    assert.equal(cells.delta, "—", "archived and unmetered is a dash too");
    const head = await page.$$eval("#chart-view .chart-axis .chart-tokens",
      (els) => els.map((e) => e.textContent.trim()));
    assert.deepEqual(head, ["토큰"], "the column names itself");
    const seriesCell = await page.$eval("#chart-view .chart-series-row .chart-tokens",
      (e) => e.textContent.trim());
    assert.equal(seriesCell, "2.15k", "the series row prints the fleet total it draws");
    // The new column is one grid column: its header, a session row and the
    // series row share the same left and right edges, and it sits between
    // the lifetime and the cflow columns rather than under either of them.
    const cols = await page.evaluate(() => {
      const box = (sel) => {
        const e = document.querySelector(sel);
        const b = e.getBoundingClientRect();
        return { left: b.left, right: b.right };
      };
      return {
        head: box("#chart-view .chart-axis .chart-tokens"),
        row: box("#chart-view .chart-row[data-name='alpha'] .chart-tokens"),
        series: box("#chart-view .chart-series-row .chart-tokens"),
        life: box("#chart-view .chart-row[data-name='alpha'] .chart-life"),
        flow: box("#chart-view .chart-row[data-name='alpha'] .chart-flow"),
      };
    });
    for (const key of ["row", "series"]) {
      assert.ok(Math.abs(cols.head.left - cols[key].left) <= 1 &&
        Math.abs(cols.head.right - cols[key].right) <= 1,
        `the token column's header and ${key} cell share edges: ${JSON.stringify(cols)}`);
    }
    assert.ok(cols.life.right <= cols.head.left + 1 && cols.head.right <= cols.flow.left + 1,
      `the token column sits between lifetime and cflow: ${JSON.stringify(cols)}`);
    // The detail column repeats the figure with its four parts, the way the
    // Stats page's own panel lists them.
    await page.click("#chart-view .chart-row[data-name='alpha'] .chart-name");
    await page.waitForSelector("#chart-view .chart-detail .chart-detail-tokens");
    const detail = await page.$eval("#chart-view .chart-detail", (e) => e.textContent);
    assert.ok(detail.includes("2.15k"), "the detail carries the total: " + detail.slice(0, 200));
    assert.ok(detail.includes("cache read 2.00k"), "and the parts: " + detail.slice(0, 200));
    await page.click("#chart-view .chart-row[data-name='alpha'] .chart-name");

    // A screenshot on request, for looking at the page by hand: the harness
    // otherwise only asserts numbers (CHART_SHOT=<path>). Taken with alpha
    // picked, so the detail column is in the frame too.
    if (process.env.CHART_SHOT) {
      await page.click("#chart-view .chart-row[data-name='alpha'] .chart-name");
      await page.waitForSelector("#chart-view .chart-detail .chart-detail-tokens");
      await page.screenshot({ path: process.env.CHART_SHOT, fullPage: false });
      await page.click("#chart-view .chart-row[data-name='alpha'] .chart-name");
    }
    assert.deepEqual(errors, []);
    console.log("ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
