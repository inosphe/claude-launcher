/* The Hive page (#/hive, hive-page.js) in a real browser.

   Serves the real index.html against a fixture: six sessions in two
   projects (a leader with a worker, the worker's two reviewers, a held
   session and one killed two hours ago), one mesh, and a board whose queues
   give the worker one issue and leave two unassigned. Checks:
     1. the nav carries Hive right after Chart;
     2. one project cell per project, the mesh cell inside, the worker's
        chip nested under the leader's with its issue beside it;
     3. the two reviewers are one deck, and clicking it opens the deck and
        draws both chips;
     4. clicking a chip fills the detail pane with the session and a link to
        open it; clicking it again clears the pane;
     5. the ended-window picker set to 숨김 drops the killed chip, and the
        choice survives a reload;
     6. the search box marks the matching chip;
     7. the zoom buttons change the scale, and fit brings the whole stage
        into the viewport;
     8. the unassigned pool is a deck under the board's cell;
     9. no page errors. */
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
  { name: "lead", status: "busy", created_at: ago(5), project: "claunch", cwd: "/w/claunch" },
  { name: "wk", status: "idle", parent: "lead", created_at: ago(4), project: "claunch", cwd: "/w/claunch", issue: "c-1" },
  { name: "wk-rev", status: "idle", parent: "wk", created_at: ago(1), project: "claunch", cwd: "/w/claunch" },
  { name: "wk-rev2", status: "busy", parent: "wk", created_at: ago(1), project: "claunch", cwd: "/w/claunch" },
  { name: "parked", status: "exited", held_at: ago(3), created_at: ago(9), project: "gds", cwd: "/w/gds" },
  { name: "dead", status: "exited", exited_at: ago(2), created_at: ago(9), project: "gds", cwd: "/w/gds" },
];
const MESH = {
  meshes: [{
    name: "claunch", members: [
      { session: "lead", handle: "lead", role: "leader", roles: ["leader"], local: true },
      { session: "wk", handle: "wk", role: "worker", roles: ["worker"], local: true },
      { session: "wk-rev", handle: "wk-rev", role: "reviewer", roles: ["reviewer"], local: true },
      { session: "wk-rev2", handle: "wk-rev2", role: "reviewer", roles: ["reviewer"], local: true },
    ],
  }],
};
const QUEUES = {
  available: true, statuses: ["open", "in_ready", "in_progress", "in_review", "blocked"],
  boards: [{
    root: "/w/claunch", workspace: "claunch", error: null,
    lanes: [
      { session: "lead", known: true, issues: [], created: [] },
      { session: "wk", known: true, issues: [{ id: "c-1", title: "the worker's issue", status: "in_progress", priority: 2 }], created: [] },
    ],
    unassigned: [
      { id: "c-7", title: "pool one", status: "open", priority: 3 },
      { id: "c-8", title: "pool two", status: "open", priority: 3 },
    ],
  }],
};

const TYPES = { ".js": "application/javascript", ".css": "text/css", ".html": "text/html" };
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/sessions") {
      return res.end(JSON.stringify({ sessions: url.searchParams.get("state") === "archived" ? [] : SESSIONS }));
    }
    if (url.pathname === "/api/mesh") return res.end(JSON.stringify(MESH));
    if (url.pathname === "/api/beads/queues") return res.end(JSON.stringify(QUEUES));
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

const chipNames = (page) => page.$$eval("#hive-view .hive-chip[data-kind=session]",
  (els) => els.map((e) => e.dataset.id).sort());

(async () => {
  const port = await safeListen(server);
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto(`http://127.0.0.1:${port}/#/hive`);
    // The worker's chip sits under the leader's once the mesh poll has
    // answered, and its issue once the queues have.
    await page.waitForFunction(() =>
      document.querySelector('#hive-view .hive-mesh .hive-kids .hive-chip[data-id="wk"]')
      && document.querySelector('#hive-view .hive-attach .hive-chip[data-id="c-1"]'), null, { timeout: 15000 });

    // 1. Hive sits right after Chart in the nav.
    const pages = await page.$$eval("#rail-nav a", (as) => as.map((a) => a.dataset.page));
    assert.equal(pages[pages.indexOf("chart") + 1], "hive");

    // 2. Project cells, the mesh inside, the tree nested.
    const projects = await page.$$eval("#hive-view .hive-project > .hive-cell-head h3", (hs) => hs.map((h) => h.textContent));
    assert.deepEqual(projects, ["claunch", "gds"]);
    const meshName = await page.$eval("#hive-view .hive-project .hive-mesh h4", (h) => h.textContent);
    assert.equal(meshName, "claunch");
    const nested = await page.$eval('#hive-view .hive-chip[data-id="lead"]', (c) =>
      !!c.closest(".hive-tree").querySelector('.hive-kids .hive-chip[data-id="wk"]'));
    assert.ok(nested, "the worker's chip is under the leader's");

    // 3. The reviewers are one deck until it is opened.
    assert.deepEqual(await chipNames(page), ["dead", "lead", "parked", "wk"]);
    const face = page.locator('#hive-view .hive-deck-face[data-deck="wk/minor"]');
    assert.match(await face.textContent(), /보조 세션 ×2/);
    await face.click();
    await page.waitForSelector('#hive-view .hive-deck.open .hive-chip[data-id="wk-rev2"]');
    assert.deepEqual(await chipNames(page), ["dead", "lead", "parked", "wk", "wk-rev", "wk-rev2"]);

    // 4. A chip fills the detail pane; a second click clears it.
    await page.click('#hive-view .hive-chip[data-id="wk"]');
    await page.waitForSelector("#hive-view .hive-detail:not(.hidden) h3");
    assert.equal(await page.$eval("#hive-view .hive-detail h3", (h) => h.textContent), "wk");
    assert.equal(await page.$eval("#hive-view .hive-detail a.hive-btn", (a) => a.getAttribute("href")), "#/s/wk");
    await page.click('#hive-view .hive-chip[data-id="wk"]');
    await page.waitForSelector("#hive-view .hive-detail.hidden", { state: "attached" });

    // 5. Hiding ended sessions drops the killed chip, and stays so.
    await page.selectOption("#hive-view .hive-toolbar label.hive-tool select", "0");
    await page.waitForFunction(() => !document.querySelector('#hive-view .hive-chip[data-id="dead"]'));
    await page.reload();
    await page.waitForSelector('#hive-view .hive-chip[data-id="lead"]');
    assert.ok(!(await page.$('#hive-view .hive-chip[data-id="dead"]')), "the choice survives a reload");
    assert.match(await page.textContent("#hive-view .hive-status"), /끝난 세션 1 숨김/);

    // 6. Search marks the match.
    await page.fill("#hive-view .hive-search", "parked");
    await page.waitForSelector('#hive-view .hive-port.searching .hive-chip.hit[data-id="parked"]');
    assert.equal(await page.$$eval("#hive-view .hive-chip.hit", (e) => e.length), 1);
    await page.fill("#hive-view .hive-search", "");

    // 7. Zoom and fit.
    const label = () => page.textContent("#hive-view .hive-zoom-label");
    const before = await label();
    await page.click('#hive-view .hive-zoom button[title^="확대"]');
    assert.notEqual(await label(), before);
    await page.click('#hive-view .hive-zoom button[title^="전체"]');
    const fits = await page.evaluate(() => {
      const port = document.querySelector("#hive-view .hive-port").getBoundingClientRect();
      const stage = document.querySelector("#hive-view .hive-stage").getBoundingClientRect();
      return stage.left >= port.left - 1 && stage.right <= port.right + 1 && stage.top >= port.top - 1;
    });
    assert.ok(fits, "fit brings the stage inside the viewport");

    // 8. The board's unassigned work is a deck in the pool cell.
    const pool = await page.$eval("#hive-view .hive-pool", (p) => p.textContent);
    assert.match(pool, /claunch 보드/);
    assert.match(pool, /미배정 P3 ×2/);

    // 9.
    assert.deepEqual(errors, []);
    console.log("hive browser ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((err) => { console.error(err); process.exit(1); });
