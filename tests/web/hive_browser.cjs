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
     5. the killed chip is hidden by default; the ended-window picker set
        to 6시간 brings it back, and the choice survives a reload; the
        archive button counts killed and failed records;
     6. the search box marks the matching chip;
     7. the zoom buttons change the scale, and fit brings the whole stage
        into the viewport;
     8. the unassigned pool is a deck under the board's cell;
     9. a drag pans from a chip and from header text without picking or
        selecting anything, and a press inside the slop is still a click;
    10. a chip filed in the category tree carries its node as a tag (an
        item in two nodes says so), the detail pane lists the full paths,
        search finds a chip by its category, and the toolbar switch hides
        the tags;
    11. relations (claunch-yzgqb.2): the worker's issue marks that the
        worker holds it and was started on it and that a reviewer joined
        it; the leader carries a reference chip for the pool issue it
        filed; picking the issue lights its sessions, fades the rest and
        draws a line to each drawn one; the detail pane lists them by kind;
        a legend button hides one kind and the toolbar switch hides all;
    12. briefing overlay (claunch-yzgqb.2.1): resting the pointer on the
        worker's chip opens an overlay with its briefing rows in the
        session tab's order, a session with no briefing says so, leaving
        the chip closes it, focusing a chip opens it, the chip carries no
        native title under it, and search finds a chip by its briefing;
    13. opening task and cflow (claunch-z6cfr): the overlay carries the
        worker's opening task (read from /api/sessions/<name>, its first
        lines and how many more) and the leader's cflow run (workflow,
        status, round, current nodes, what it waits on); the detail pane a
        click opens draws the whole task, the briefing and the cflow rows
        with a link to the run page, and a session with no run says so;
    14. no page errors. */
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
  { name: "wk", status: "idle", parent: "lead", created_at: ago(4), project: "claunch", cwd: "/w/claunch", issue: "c-1",
    briefing: { one_line: "wiring the relation chips", state: "fresh", goal: "show session-issue ties",
                now: "writing the browser step", progress: "model done, view half" } },
  { name: "wk-rev", status: "idle", parent: "wk", created_at: ago(1), project: "claunch", cwd: "/w/claunch" },
  { name: "wk-rev2", status: "busy", parent: "wk", created_at: ago(1), project: "claunch", cwd: "/w/claunch", issue: "c-1" },
  { name: "parked", status: "exited", held_at: ago(3), created_at: ago(9), project: "gds", cwd: "/w/gds" },
  { name: "dead", status: "exited", exited_at: ago(2), created_at: ago(9), project: "gds", cwd: "/w/gds" },
];
const WK_TASK = Array.from({ length: 12 }, (_, i) => `task line ${i + 1}`).join("\n");
const CFLOW2 = {
  lead: { cwd: "/w/claunch", scope: "lead", workflow: "improv-leader-v2", status: "running", round: 3,
          current: ["integrate"], owed: ["select: land"], gated: true },
};
const taskAsked = [];
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
      { id: "c-8", title: "pool two", status: "open", priority: 3, created_by: "lead" },
    ],
  }],
};

// The board's workspace tree: wk under web/hive, c-1 in two nodes (the
// reading claunch-2gb7l may bring), c-7 under a node of its own.
const CATEGORY = {
  workspace: "claunch",
  nodes: [
    { id: "n1", parent_id: null, label: "web", path: ["web"] },
    { id: "n2", parent_id: "n1", label: "hive", path: ["web", "hive"] },
    { id: "n3", parent_id: null, label: "beads", path: ["beads"] },
  ],
  items: [
    { kind: "session", ref: "wk", node_id: "n2" },
    { kind: "issue", ref: "c-1", node_id: "n3" },
    { kind: "issue", ref: "c-1", node_id: "n2" },
    { kind: "issue", ref: "c-7", node_id: "n3" },
  ],
  proposals: [], unassigned: [], details: {},
};
const categoryAsked = [];

const TYPES = { ".js": "application/javascript", ".css": "text/css", ".html": "text/html" };
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/sessions") {
      return res.end(JSON.stringify({ sessions: url.searchParams.get("state") === "archived" ? [] : SESSIONS }));
    }
    if (url.pathname === "/api/mesh") return res.end(JSON.stringify(MESH));
    if (url.pathname === "/api/cflow2/rail") return res.end(JSON.stringify({ rows: CFLOW2 }));
    const one = url.pathname.match(/^\/api\/sessions\/([^/]+)$/);
    if (one) {
      const name = decodeURIComponent(one[1]);
      const s = SESSIONS.find((x) => x.name === name);
      taskAsked.push(name);
      if (!s) { res.statusCode = 404; return res.end(JSON.stringify({ error: "no such session" })); }
      return res.end(JSON.stringify({ ...s, task: name === "wk" ? WK_TASK : "" }));
    }
    if (url.pathname === "/api/beads/queues") return res.end(JSON.stringify(QUEUES));
    if (url.pathname === "/api/category") {
      categoryAsked.push(url.searchParams.get("workspace"));
      return res.end(JSON.stringify(CATEGORY));
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
    assert.deepEqual(await chipNames(page), ["lead", "parked", "wk"]);
    const face = page.locator('#hive-view .hive-deck-face[data-deck="wk/minor"]');
    assert.match(await face.textContent(), /보조 세션 ×2/);
    await face.click();
    await page.waitForSelector('#hive-view .hive-deck.open .hive-chip[data-id="wk-rev2"]');
    assert.deepEqual(await chipNames(page), ["lead", "parked", "wk", "wk-rev", "wk-rev2"]);

    // 4. A chip fills the detail pane; a second click clears it.
    await page.click('#hive-view .hive-chip[data-id="wk"]');
    await page.waitForSelector("#hive-view .hive-detail:not(.hidden) h3");
    assert.equal(await page.$eval("#hive-view .hive-detail h3", (h) => h.textContent), "wk");
    assert.equal(await page.$eval("#hive-view .hive-detail a.hive-btn", (a) => a.getAttribute("href")), "#/s/wk");
    await page.click('#hive-view .hive-chip[data-id="wk"]');
    await page.waitForSelector("#hive-view .hive-detail.hidden", { state: "attached" });

    // 5. Ended sessions are hidden by default; a window brings them back,
    // and stays so.
    assert.match(await page.textContent("#hive-view .hive-status"), /끝난 세션 1 숨김/);
    assert.equal(await page.textContent("#hive-view .hive-archive"), "killed·failed 1개 보관");
    await page.selectOption("#hive-view .hive-toolbar label.hive-tool select", "6h");
    await page.waitForSelector('#hive-view .hive-chip[data-id="dead"]');
    await page.reload();
    await page.waitForSelector('#hive-view .hive-chip[data-id="lead"]');
    assert.ok(await page.$('#hive-view .hive-chip[data-id="dead"]'), "the choice survives a reload");

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

    // 9. A drag pans from wherever it starts: on a chip it moves the stage
    // and picks nothing, on header text it selects nothing, and a press
    // that moves less than the slop is still a click.
    const shift = () => page.$eval("#hive-view .hive-stage", (s) => {
      const m = /translate\(([-\d.]+)px, ([-\d.]+)px\)/.exec(s.style.transform);
      return [Number(m[1]), Number(m[2])];
    });
    const drag = async (sel, dx, dy) => {
      const box = await page.locator(sel).boundingBox();
      const x = box.x + Math.min(20, box.width / 2), y = box.y + box.height / 2;
      await page.mouse.move(x, y);
      await page.mouse.down();
      await page.mouse.move(x + dx, y + dy, { steps: 6 });
      await page.mouse.up();
    };
    const detailHidden = () => page.$eval("#hive-view .hive-detail", (d) => d.classList.contains("hidden"));
    assert.ok(await detailHidden());
    let [x0, y0] = await shift();
    await drag('#hive-view .hive-chip[data-id="wk"]', 60, 40);
    let [x1, y1] = await shift();
    assert.equal(Math.round(x1 - x0), 60, "a drag on a chip pans");
    assert.equal(Math.round(y1 - y0), 40);
    assert.ok(await detailHidden(), "the click that ends a pan picks nothing");
    await drag("#hive-view .hive-project > .hive-cell-head h3 >> nth=0", -50, 30);
    const [x2, y2] = await shift();
    assert.equal(Math.round(x2 - x1), -50, "a drag on header text pans");
    assert.equal(Math.round(y2 - y1), 30);
    assert.equal(await page.evaluate(() => String(getSelection())), "", "no text was selected");
    await drag('#hive-view .hive-chip[data-id="wk"]', 2, 1);
    await page.waitForSelector("#hive-view .hive-detail:not(.hidden) h3");
    assert.deepEqual(await shift(), [x2, y2], "a press inside the slop does not pan");
    await page.click('#hive-view .hive-chip[data-id="wk"]');
    await page.waitForSelector("#hive-view .hive-detail.hidden", { state: "attached" });

    // 10. Category tags, from the tree of the board's workspace.
    await page.waitForSelector('#hive-view .hive-chip[data-id="wk"] .hive-cat');
    assert.deepEqual([...new Set(categoryAsked)], ["claunch"], "the board's workspace tree is read");
    const tagOf = (kind, id) => page.textContent(`#hive-view .hive-chip[data-kind="${kind}"][data-id="${id}"] .hive-cat`);
    assert.equal(await tagOf("session", "wk"), "# hive");
    assert.equal(await tagOf("issue", "c-1"), "# beads +1", "an item in two nodes");
    assert.equal(await page.$('#hive-view .hive-chip[data-id="lead"] .hive-cat'), null, "lead is filed nowhere");
    await page.click('#hive-view .hive-chip[data-kind="issue"][data-id="c-1"]');
    await page.waitForSelector("#hive-view .hive-detail:not(.hidden) h3");
    const catRow = await page.$$eval("#hive-view .hive-detail dt", (dts) => {
      const dt = dts.find((d) => d.textContent === "카테고리");
      return dt ? dt.nextElementSibling.textContent : null;
    });
    assert.equal(catRow, "beads\nweb / hive");
    await page.click('#hive-view .hive-chip[data-kind="issue"][data-id="c-1"]');
    await page.waitForSelector("#hive-view .hive-detail.hidden", { state: "attached" });
    await page.fill("#hive-view .hive-search", "web / hive");
    await page.waitForSelector('#hive-view .hive-port.searching .hive-chip.hit[data-id="wk"]');
    assert.deepEqual(await page.$$eval("#hive-view .hive-chip.hit", (e) => e.map((c) => c.dataset.id).sort()), ["c-1", "wk"]);
    await page.fill("#hive-view .hive-search", "");
    const catSwitch = page.locator("#hive-view .hive-toolbar label.hive-toggle", { hasText: "카테고리" }).locator("input");
    await catSwitch.uncheck();
    await page.waitForSelector("#hive-view .hive-cat", { state: "detached" });
    await catSwitch.check();
    await page.waitForSelector('#hive-view .hive-chip[data-id="wk"] .hive-cat');

    // 11. Relations between sessions and issues.
    const marks = (sel) => page.$$eval(`${sel} .hive-rels .hive-rel`, (els) => els.map((e) => e.textContent));
    const c1 = '#hive-view .hive-attach .hive-chip[data-kind="issue"][data-id="c-1"]';
    assert.deepEqual(await marks(c1), ["담당", "시작", "참가 wk-rev2"]);
    const ref = '#hive-view .hive-chip[data-id="lead"] ~ .hive-refs .hive-ref[data-id="c-8"]';
    await page.waitForSelector(ref);
    assert.deepEqual(await marks(ref), ["생성", "미배정"]);
    assert.equal(await page.$eval(ref, (c) => c.dataset.ref), "lead");
    // Open the reviewers' deck so both sessions tied to c-1 are drawn.
    await page.click('#hive-view .hive-deck-face[data-deck="wk/minor"]');
    await page.waitForSelector('#hive-view .hive-deck.open .hive-chip[data-id="wk-rev2"]');
    const rref = '#hive-view .hive-chip[data-id="wk-rev2"] ~ .hive-refs .hive-ref[data-id="c-1"]';
    assert.deepEqual(await marks(rref), ["참가", "담당 wk"]);
    await page.click(c1);
    await page.waitForSelector("#hive-view .hive-port.relating .hive-links");
    const lit = await page.$$eval("#hive-view .hive-chip.rel[data-kind=session]", (els) => els.map((e) => e.dataset.id).sort());
    assert.deepEqual(lit, ["wk", "wk-rev2"]);
    const lines = await page.$$eval("#hive-view .hive-links path", (ps) => ps.map((p) => [p.dataset.to, p.getAttribute("class")]).sort());
    assert.deepEqual(lines, [["session:wk", "r-assignee"], ["session:wk-rev2", "r-join"]]);
    assert.equal(await page.$eval('#hive-view .hive-chip[data-id="lead"]', (c) => getComputedStyle(c).opacity), "0.3");
    const dd = (label) => page.$$eval("#hive-view .hive-detail dt", (dts, l) => {
      const dt = dts.find((d) => d.textContent === l);
      return dt ? dt.nextElementSibling.textContent : null;
    }, label);
    assert.equal(await dd("담당 세션"), "wk");
    assert.equal(await dd("시작 세션"), "wk");
    assert.equal(await dd("참가 세션"), "wk-rev2");
    // A session pick lists its ties by kind and lights its reference chip.
    await page.click('#hive-view .hive-chip[data-kind="session"][data-id="lead"]');
    await page.waitForFunction(() => document.querySelector("#hive-view .hive-detail h3")?.textContent === "lead");
    assert.equal(await dd("생성"), "c-8");
    assert.ok(await page.$(`${ref}.rel`), "the reference chip is lit");
    await page.click('#hive-view .hive-chip[data-kind="session"][data-id="lead"]');
    await page.waitForSelector("#hive-view .hive-detail.hidden", { state: "attached" });
    assert.equal(await page.$("#hive-view .hive-port.relating"), null);
    // The legend hides one kind; the toolbar switch hides them all.
    await page.click('#hive-view .hive-legend button[data-rel="join"]');
    await page.waitForFunction((sel) => !document.querySelector(sel), rref);
    assert.deepEqual(await marks(c1), ["담당", "시작"]);
    await page.click('#hive-view .hive-legend button[data-rel="join"]');
    await page.waitForSelector(rref);
    const relSwitch = page.locator("#hive-view .hive-toolbar label.hive-toggle", { hasText: "관계" }).locator("input");
    await relSwitch.uncheck();
    await page.waitForSelector("#hive-view .hive-rels", { state: "detached" });
    assert.equal(await page.$("#hive-view .hive-refs"), null);
    await relSwitch.check();
    await page.waitForSelector(ref);

    // 12. The briefing overlay.
    const brief = "#hive-view .hive-brief";
    const wkChip = '#hive-view .hive-chip[data-kind="session"][data-id="wk"]';
    assert.equal(await page.getAttribute(wkChip, "title"), null, "no native tooltip under the overlay");
    await page.hover(wkChip);
    await page.waitForSelector(`${brief}:not(.hidden)[data-session="wk"]`);
    assert.deepEqual(await page.$$eval(`${brief} .hive-brief-row`, (rs) => rs.map((r) => r.textContent)),
      ["wiring the relation chips", "goalshow session-issue ties", "nowwriting the browser step",
       "progressmodel done, view half"]);
    assert.equal(await page.textContent(`${brief} .hive-brief-head.h-briefing`), "briefing · fresh");
    assert.match(await page.textContent(`${brief} .hive-brief-name`), /^wk — idle/);
    const inPort = await page.$eval(brief, (b) => {
      const p = b.parentElement.getBoundingClientRect(), r = b.getBoundingClientRect();
      return r.left >= p.left && r.right <= p.right && r.top >= p.top && r.bottom <= p.bottom;
    });
    assert.ok(inPort, "the overlay stays inside the canvas");
    await page.hover('#hive-view .hive-chip[data-kind="session"][data-id="lead"]');
    await page.waitForSelector(`${brief}:not(.hidden)[data-session="lead"]`);
    assert.equal(await page.textContent(`${brief} .hive-brief-head.h-briefing`), "briefing 없음");
    await page.hover("#hive-view .hive-status");
    await page.waitForSelector(brief, { state: "hidden" });
    await page.focus(wkChip);
    await page.waitForSelector(`${brief}:not(.hidden)[data-session="wk"]`);
    await page.focus("#hive-view .hive-search");
    await page.waitForSelector(brief, { state: "hidden" });
    await page.fill("#hive-view .hive-search", "browser step");
    await page.waitForSelector('#hive-view .hive-chip.hit[data-id="wk"]');
    await page.fill("#hive-view .hive-search", "");

    // 13. Opening task and cflow, in the overlay and the detail pane.
    await page.hover(wkChip);
    await page.waitForSelector(`${brief}:not(.hidden)[data-session="wk"] .hive-task-text`);
    assert.equal(await page.textContent(`${brief} .hive-task-text`),
      WK_TASK.split("\n").slice(0, 8).join("\n"), "the overlay shows the task's first lines");
    assert.match(await page.textContent(brief), /… 4줄 더/);
    const wkReads = () => taskAsked.filter((n) => n === "wk").length;
    const readsBefore = wkReads();
    assert.equal(await page.textContent(`${brief} .hive-brief-head.h-cflow`), "cflow 런 없음");
    const heads = await page.$$eval(`${brief} .hive-brief-head`, (hs) => hs.map((h) => h.className.split(" ").pop()));
    assert.deepEqual(heads, ["h-task", "h-briefing", "h-cflow"]);
    const leadChip = '#hive-view .hive-chip[data-kind="session"][data-id="lead"]';
    await page.hover(leadChip);
    await page.waitForSelector(`${brief}:not(.hidden)[data-session="lead"] .hive-cflow-rows`);
    assert.match(await page.textContent(`${brief} .hive-brief-head.h-cflow`), /^cflow · improv-leader-v2/);
    assert.deepEqual(await page.$$eval(`${brief} .hive-cflow-rows .hive-brief-row`, (rs) => rs.map((r) => r.textContent)),
      ["workflowimprov-leader-v2", "statusrunning · 사람 대기", "round3", "현재integrate", "대기select: land"]);
    await page.waitForFunction((b) => /기록된 opening task가 없다/.test(document.querySelector(b).textContent), brief);
    assert.doesNotMatch(await page.textContent(`${brief} .hive-brief-meta`), /cflow:/, "the cflow line is its own section");
    await page.hover("#hive-view .hive-status");
    await page.waitForSelector(brief, { state: "hidden" });
    // The detail pane: the whole task, the briefing, the cflow rows.
    await page.click(wkChip);
    await page.waitForSelector("#hive-view .hive-detail:not(.hidden) .s-task .hive-task-text.full");
    assert.equal(await page.textContent("#hive-view .hive-detail .hive-task-text.full"), WK_TASK);
    assert.equal(wkReads(), readsBefore, "the task read for the overlay is kept for the pane");
    assert.deepEqual(await page.$$eval("#hive-view .hive-detail .s-briefing .hive-brief-row", (rs) => rs.map((r) => r.textContent)),
      ["wiring the relation chips", "goalshow session-issue ties", "nowwriting the browser step",
       "progressmodel done, view half"]);
    assert.equal(await page.textContent("#hive-view .hive-detail .s-cflow .hive-detail-head"), "cflow 런 없음");
    await page.click(leadChip);
    await page.waitForFunction(() => document.querySelector("#hive-view .hive-detail h3")?.textContent === "lead");
    assert.deepEqual(await page.$$eval("#hive-view .hive-detail .s-cflow .hive-brief-row", (rs) => rs.map((r) => r.textContent)),
      ["workflowimprov-leader-v2", "statusrunning · 사람 대기", "round3", "현재integrate", "대기select: land"]);
    assert.deepEqual(await page.$$eval("#hive-view .hive-detail a.hive-btn", (as) => as.map((a) => a.getAttribute("href"))),
      ["#/s/lead", "#/wf/" + encodeURIComponent("lead|/w/claunch")]);
    await page.waitForSelector("#hive-view .hive-detail .s-task .hive-muted");
    assert.equal(await page.textContent("#hive-view .hive-detail .s-task .hive-muted"), "기록된 opening task가 없다.");
    await page.click(leadChip);
    await page.waitForSelector("#hive-view .hive-detail.hidden", { state: "attached" });

    // 14.
    assert.deepEqual(errors, []);
    console.log("hive browser ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((err) => { console.error(err); process.exit(1); });
