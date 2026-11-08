/* The Chart page's detail column in a real browser (claunch-h82vq): its two
   views, the session's lifecycle presses and its run's gates.

   Serves the real index.html against a fixture with two sessions -- alpha
   running with a v2 run that waits on a person's choice, beta paused -- and
   checks:
     1. picking a row shows the 요약 | Detail radio with 요약 lit, the
        summary list under it, and the column's action row: a live session
        offers 터미널 · kill · pause · hold · archive, a paused one
        터미널 · resume · kill · archive;
     2. the run's owed choice is drawn with one button per option, and a
        press (confirmed) posts /api/cflow2/select with the run's slot and
        the option;
     3. kill posts /api/sessions/<name>/kill;
     4. the Detail tab moves the dashboard's own session detail (#sess-view,
        its head and its Details | Workflow | Beads tabs) into the column,
        the docked rail stays shut, and the choice survives the page's
        redraw and a reload;
     5. the detail's own × hands the panel back: the column shows 요약
        again and #sess-view is back out of the column and hidden;
     6. leaving the page hands it back too.
   4b (claunch-h82vq.1): the column's top -- name, presses, gates, radio --
   is fixed and only the body under it scrolls. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");

const now = Date.now();
const ago = (h) => new Date(now - h * 3600000).toISOString();
const SESSIONS = [
  { name: "alpha", status: "running", created_at: ago(2), project: "default", cwd: "F:/w/repo1", role: "worker" },
  { name: "beta", status: "exited", created_at: ago(30), exited_at: ago(20), paused_at: ago(20),
    project: "default", cwd: "F:/w/repo2" },
];
const RAIL = {
  alpha: {
    workflow: "improv-worker-v2", status: "running", round: 1, current: ["consult-check"],
    cwd: "F:/w/repo1", scope: "alpha@main", run: "r1",
    gated: true, blocking: true, owed: ["consult-check"], urgent_asks: [],
    answers: [{ kind: "choose", instance: "round/consult-check@21", label: "consult-check",
                prompt: "작업으로 갈지 협의할지", options: [{ name: "work" }, { name: "consult" }] }],
    updated: ago(0.01), entered_at: (now - 60000) / 1000,
  },
};
const posts = [];
const metaAsked = [];

const TYPES = { ".js": "application/javascript", ".css": "text/css", ".html": "text/html" };
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    // No /api/batch here: the client falls back to one request per read.
    if (req.method === "POST" && url.pathname !== "/api/batch") {
      let body = "";
      req.on("data", (c) => { body += c; });
      req.on("end", () => {
        posts.push({ path: url.pathname, body: body ? JSON.parse(body) : null });
        res.end(JSON.stringify({ ok: true, name: "alpha", status: "running" }));
      });
      return;
    }
    if (url.pathname === "/api/sessions") {
      const archived = url.searchParams.get("state") === "archived";
      return res.end(JSON.stringify({ sessions: archived ? [] : SESSIONS }));
    }
    if (url.pathname === "/api/cflow2/rail") return res.end(JSON.stringify({ rows: RAIL }));
    const meta = url.pathname.match(/^\/api\/sessions\/([^/]+)\/meta$/);
    if (meta) {
      const name = decodeURIComponent(meta[1]);
      metaAsked.push(name);
      const s = SESSIONS.find((x) => x.name === name) || { name };
      return res.end(JSON.stringify({ session: { ...s, cols: 80, rows: 24 } }));
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

(async () => {
  const port = await safeListen(server);
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1500, height: 900 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    // Every confirm the presses raise is answered yes.
    page.on("dialog", (d) => d.accept());
    await page.goto(`http://127.0.0.1:${port}/#/chart`);
    await page.waitForFunction(() =>
      document.querySelectorAll("#chart-view .chart-row[data-name]").length >= 2, null, { timeout: 15000 });

    const col = "#chart-view .chart-detail";
    const tabs = () => page.$$eval(`${col} .chart-detail-tabs .seq-tab`,
      (bs) => bs.map((b) => `${b.textContent}${b.classList.contains("on") ? "*" : ""}`));
    const acts = () => page.$$eval(`${col} .chart-detail-actions .chart-act`, (bs) => bs.map((b) => b.textContent));

    // 1. Pick alpha: the radio, the summary, the live session's presses.
    await page.click("#chart-view .chart-row[data-name='alpha'] .chart-life");
    await page.waitForSelector(`${col} .chart-detail-tabs`);
    assert.deepEqual(await tabs(), ["요약*", "Detail"]);
    assert.ok(await page.$(`${col} .chart-detail-list`), "요약 draws the summary list");
    assert.deepEqual(await acts(), ["터미널", "kill", "pause", "hold", "archive"]);
    assert.equal(await page.$eval(`${col} .chart-detail-actions a`, (a) => a.getAttribute("href")), "#/s/alpha");

    // 2. The owed choice, one button per option; a press posts the select.
    await page.waitForSelector(`${col} .chart-detail-gates .chart-gate`);
    const gate = await page.$eval(`${col} .chart-gate`, (g) => ({
      head: g.querySelector(".chart-gate-head").textContent,
      href: g.querySelector(".chart-gate-head").getAttribute("href"),
      question: g.querySelector(".chart-gate-question").textContent,
      buttons: [...g.querySelectorAll(".tcf-btn")].map((b) => b.textContent),
    }));
    assert.match(gate.head, /improv-worker-v2 · v2 · consult-check — choose/);
    assert.equal(gate.href, "#/wf/" + encodeURIComponent("alpha@main|F:/w/repo1"));
    assert.equal(gate.question, "작업으로 갈지 협의할지");
    assert.deepEqual(gate.buttons, ["work", "consult"]);
    await page.click(`${col} .chart-gate .tcf-btn:text-is("work")`);
    await page.waitForFunction(() => true);
    for (let i = 0; i < 50 && !posts.some((p) => p.path === "/api/cflow2/select"); i++) await page.waitForTimeout(100);
    const sel = posts.find((p) => p.path === "/api/cflow2/select");
    assert.ok(sel, "the press posts the select: " + JSON.stringify(posts));
    assert.deepEqual(sel.body, { cwd: "F:/w/repo1", scope: "alpha@main", node: "round/consult-check@21",
                                 option: "work", reason: "" });

    // 3. kill posts the kill route.
    await page.click(`${col} .chart-detail-actions button[data-act="kill"]`);
    for (let i = 0; i < 50 && !posts.some((p) => p.path === "/api/sessions/alpha/kill"); i++) await page.waitForTimeout(100);
    assert.ok(posts.some((p) => p.path === "/api/sessions/alpha/kill"), JSON.stringify(posts));

    // A paused session offers resume and keeps kill (file it as killed).
    await page.click("#chart-view .chart-row[data-name='beta'] .chart-life");
    await page.waitForFunction(() =>
      document.querySelector("#chart-view .chart-detail .chart-detail-name")?.textContent === "beta");
    assert.deepEqual(await acts(), ["터미널", "resume", "kill", "archive"]);
    assert.equal(await page.$(`${col} .chart-detail-gates`), null, "no run, no gates");

    // 4. The Detail tab: the dashboard's own panel, inside the column.
    await page.click("#chart-view .chart-row[data-name='alpha'] .chart-life");
    await page.waitForFunction(() =>
      document.querySelector("#chart-view .chart-detail .chart-detail-name")?.textContent === "alpha");
    await page.click(`${col} .chart-detail-tabs .seq-tab[data-pane="detail"]`);
    await page.waitForSelector(`${col} .chart-detail-host #sess-view .sess-head`);
    const hosted = async () => page.evaluate(() => {
      const v = document.getElementById("sess-view");
      return {
        inColumn: !!v.closest("#chart-view .chart-detail-host"),
        docked: v.classList.contains("docked"),
        hidden: v.classList.contains("hidden"),
        head: (v.querySelector(".sess-head h2") || {}).textContent || "",
        tabs: [...v.querySelectorAll(".sess-tabs .seq-tab")].map((b) => b.textContent),
        address: location.hash,
      };
    });
    let h = await hosted();
    assert.equal(h.inColumn, true);
    assert.equal(h.docked, false);
    assert.equal(h.hidden, false);
    assert.equal(h.head, "alpha");
    assert.deepEqual(h.tabs, ["Details", "Workflow", "Beads"]);
    assert.ok(!/detail=/.test(h.address), "the address names no rail: " + h.address);
    assert.ok(metaAsked.includes("alpha"), "the panel reads the session's meta");
    assert.deepEqual(await tabs(), ["요약", "Detail*"]);
    // The action row stays above the tabs in this view too; the kill pressed
    // in 3 reads as pending (killUiState) until a poll sees the exit.
    assert.deepEqual(await acts(), ["터미널", "ending…", "pause", "hold", "archive"]);
    // 4b. Only the body scrolls (claunch-h82vq.1): on a short window the
    // detail overflows, and scrolling it to the end leaves the top -- name,
    // presses, gates, the radio -- where it was, in view.
    await page.setViewportSize({ width: 1500, height: 650 });
    const pinned = () => page.evaluate((c) => {
      const col = document.querySelector(c);
      const top = col.querySelector(".chart-detail-top");
      const body = col.querySelector(".chart-detail-body");
      const box = col.getBoundingClientRect();
      const r = (sel) => (top.querySelector(sel) || { getBoundingClientRect: () => null }).getBoundingClientRect();
      const acts = r(".chart-detail-actions");
      const tabs = r(".chart-detail-tabs");
      return {
        inTop: !!top.querySelector(".chart-detail-actions") && !!top.querySelector(".chart-detail-gates")
          && !!top.querySelector(".chart-detail-tabs") && !!top.querySelector(".chart-detail-name"),
        hostInBody: !!body.querySelector(".chart-detail-host #sess-view"),
        overflow: body.scrollHeight - body.clientHeight,
        bodyTop: body.scrollTop,
        colTop: col.scrollTop,
        actsTop: acts && acts.top,
        tabsTop: tabs && tabs.top,
        tabsBottom: tabs && tabs.bottom,
        colBottom: box.bottom,
      };
    }, col);
    const before = await pinned();
    assert.equal(before.inTop, true, "the name, presses, gates and radio sit in the fixed top");
    assert.equal(before.hostInBody, true, "the borrowed panel sits in the scrolling body");
    assert.ok(before.overflow > 0, "the body overflows on a short window: " + JSON.stringify(before));
    // Find and scroll the body in one task: $eval resolves its handle first,
    // and a redraw in between (the page's timer) leaves it on a detached node.
    await page.evaluate((c) => {
      const b = document.querySelector(`${c} .chart-detail-body`);
      b.scrollTop = b.scrollHeight;
    }, col);
    const after = await pinned();
    assert.ok(after.bodyTop > 0, "the body scrolled: " + JSON.stringify({ before, after }));
    assert.equal(after.colTop, 0, "the column itself does not scroll");
    assert.equal(after.actsTop, before.actsTop, "the presses stay put");
    assert.equal(after.tabsTop, before.tabsTop, "the radio stays put");
    assert.ok(after.tabsBottom <= after.colBottom, "and stays in view: " + JSON.stringify(after));
    // The summary view scrolls the same way.
    await page.click(`${col} .chart-detail-tabs .seq-tab[data-pane="summary"]`);
    await page.waitForSelector(`${col} .chart-detail-body .chart-detail-list`);
    assert.ok(await page.$(`${col} .chart-detail-top .chart-detail-actions`), "summary: presses in the top");
    await page.click(`${col} .chart-detail-tabs .seq-tab[data-pane="detail"]`);
    await page.waitForSelector(`${col} .chart-detail-host #sess-view .sess-head`);
    await page.setViewportSize({ width: 1500, height: 900 });
    // The page's own redraw keeps the panel where it is.
    await page.waitForTimeout(5600);
    h = await hosted();
    assert.equal(h.inColumn, true, "survives the redraw");
    assert.equal(h.head, "alpha");
    // Another row: the same panel, aimed at it.
    await page.click("#chart-view .chart-row[data-name='beta'] .chart-life");
    await page.waitForFunction(() =>
      document.querySelector("#chart-view .chart-detail-host #sess-view .sess-head h2")?.textContent === "beta",
      null, { timeout: 10000 });
    // And a reload comes back on the Detail tab.
    await page.reload();
    await page.waitForSelector(`${col} .chart-detail-host #sess-view .sess-head`, { timeout: 15000 });
    assert.deepEqual(await tabs(), ["요약", "Detail*"]);

    // 5. The detail's own × closes the borrowed panel, not the row.
    await page.click(`${col} #sess-view .sess-close`);
    await page.waitForFunction(() =>
      document.querySelector("#chart-view .chart-detail .chart-detail-list"), null, { timeout: 5000 });
    assert.deepEqual(await tabs(), ["요약*", "Detail"]);
    h = await hosted();
    assert.equal(h.inColumn, false, "handed back");
    assert.equal(h.hidden, true, "and shut: the address names no detail");

    // 6. Leaving the page hands a borrowed panel back as well.
    await page.click(`${col} .chart-detail-tabs .seq-tab[data-pane="detail"]`);
    await page.waitForSelector(`${col} .chart-detail-host #sess-view .sess-head`);
    await page.evaluate(() => { location.hash = "#/stats"; });
    await page.waitForFunction(() => !document.getElementById("sess-view").closest("#chart-view"),
      null, { timeout: 5000 });
    h = await hosted();
    assert.equal(h.hidden, true);

    if (process.env.CHARTDETAIL_SHOT) {
      await page.evaluate(() => { location.hash = "#/chart"; });
      await page.waitForSelector(`${col} .chart-detail-host #sess-view .sess-head`, { timeout: 15000 });
      await page.screenshot({ path: process.env.CHARTDETAIL_SHOT, fullPage: false });
    }
    assert.deepEqual(errors, []);
    console.log("ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
