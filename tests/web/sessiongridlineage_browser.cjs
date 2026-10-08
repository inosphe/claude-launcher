/* Selecting a grid cell draws its session's lineage as coloured arrows.

   A click on a cell selects the session; the grid then lays an SVG over its
   cells with one arrow from the parent's cell to the selected one and one from
   the selected one to each child's cell, the parent link and the child links
   in two colours, and rings the cells at the far ends (claunch-86740). This
   drives the shipped UI against a fixture daemon and checks what the page
   actually drew: which arrows, in which colour, ending on which cell's border,
   scrolling with the cells, and not taking the clicks meant for the cells
   under them. Set SG_LINEAGE_SHOT to a .png path to keep a screenshot. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");
const base = { status: "idle", cwd: "/repo", harness: "claude", model: "opus" };
const sessions = [
  { ...base, name: "lead" },
  { ...base, name: "w1", parent: "lead" },
  { ...base, name: "w2", parent: "lead" },
  { ...base, name: "g1", parent: "w1" },
  { ...base, name: "g2", parent: "w1" },
  { ...base, name: "solo" },
];
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
    let data = {};
    if (url.pathname === "/api/daemon") data = { version: "fixture", boot_id: "test" };
    else if (url.pathname === "/api/sessions") data = { sessions, llm_configured: true };
    else if (url.pathname.endsWith("/capture")) data = { lines: [] };
    else if (/^\/api\/(profiles|workspaces|mesh|cflow|harnesses|roles)$/.test(url.pathname)) data = [];
    res.end(JSON.stringify(data));
    return;
  }
  const file = path.join(root, url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/static\//, ""));
  try {
    res.setHeader("Content-Type", file.endsWith(".js") ? "application/javascript" : file.endsWith(".css") ? "text/css" : "text/html");
    res.end(fs.readFileSync(file));
  } catch { res.statusCode = 404; res.end(); }
});
(async () => {
  await safeListen(server);
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on("pageerror", e => errors.push(String(e)));
    await page.addInitScript(() => {
      localStorage.setItem("claunch_token:/", "fixture-token");
      localStorage.setItem("claunch_session_view:/", "grid");
      // Three cells a line, so the six sessions take two lines and some
      // arrows have to cross from one line to the next.
      localStorage.setItem("claunch_session_grid_per_line:/", "3");
    });
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    const cell = (name) => page.locator(`#session-grid .sg-cell[data-name="${name}"]`);
    await cell("solo").waitFor();
    assert.equal(await page.locator("#session-grid .sg-lineage").count(), 0,
      "nothing is selected yet, so nothing is drawn");

    // What the page drew: each arrow's class, ends and colour, and each end
    // measured against the cell box it should touch.
    const drawn = () => page.evaluate(() => {
      const host = document.getElementById("session-grid");
      const svg = host.querySelector(".sg-lineage");
      if (!svg) return null;
      const s = svg.getBoundingClientRect();
      const rect = (name) => {
        const r = host.querySelector(`.sg-cell[data-name="${name}"]`).getBoundingClientRect();
        return { left: r.left - s.left, right: r.right - s.left, top: r.top - s.top, bottom: r.bottom - s.top };
      };
      return {
        pointerEvents: getComputedStyle(svg).pointerEvents,
        arrows: [...svg.querySelectorAll(".sg-lineage-arrow")].map((p) => {
          const len = p.getTotalLength();
          const a = p.getPointAtLength(0), b = p.getPointAtLength(len);
          return {
            kind: p.classList.contains("parent") ? "parent" : "child",
            from: p.dataset.from, to: p.dataset.to,
            stroke: getComputedStyle(p).stroke,
            marker: p.getAttribute("marker-end"),
            length: len,
            start: { x: a.x, y: a.y }, end: { x: b.x, y: b.y },
            fromBox: rect(p.dataset.from), toBox: rect(p.dataset.to),
          };
        }),
        rings: [...host.querySelectorAll(".sg-cell.lineage-parent, .sg-cell.lineage-child")]
          .map((el) => [el.dataset.name, el.classList.contains("lineage-parent") ? "parent" : "child"]),
      };
    });
    const onBorder = (box, p) => {
      const inX = p.x >= box.left - 1.5 && p.x <= box.right + 1.5;
      const inY = p.y >= box.top - 1.5 && p.y <= box.bottom + 1.5;
      const edge = Math.min(Math.abs(p.x - box.left), Math.abs(p.x - box.right),
                            Math.abs(p.y - box.top), Math.abs(p.y - box.bottom));
      return inX && inY && edge < 1.5;
    };
    const inside = (box, p) => p.x > box.left && p.x < box.right && p.y > box.top && p.y < box.bottom;

    await cell("w1").click();
    let got = await drawn();
    assert.ok(got, "selecting w1 draws the overlay");
    assert.deepEqual(got.arrows.map((a) => [a.kind, a.from, a.to]),
      [["parent", "lead", "w1"], ["child", "w1", "g1"], ["child", "w1", "g2"]]);
    assert.deepEqual(got.rings.sort(), [["g1", "child"], ["g2", "child"], ["lead", "parent"]]);
    const colours = new Set(got.arrows.map((a) => `${a.kind}:${a.stroke}`));
    assert.equal(colours.size, 2, `one colour per kind: ${[...colours]}`);
    assert.notEqual(got.arrows[0].stroke, got.arrows[1].stroke, "parent and child links differ in colour");
    for (const a of got.arrows) {
      assert.ok(inside(a.fromBox, a.start), `${a.from}->${a.to} starts inside ${a.from}: ${JSON.stringify(a)}`);
      assert.ok(onBorder(a.toBox, a.end), `${a.from}->${a.to} ends on ${a.to}'s border: ${JSON.stringify(a)}`);
      assert.equal(a.marker, `url(#sg-lineage-head-${a.kind})`);
      // Neighbouring cells are 3px apart; the arrow must still be a line.
      assert.ok(a.length > 20, `${a.from}->${a.to} is ${a.length}px long`);
    }
    assert.equal(got.pointerEvents, "none", "the overlay takes no pointer events");
    if (process.env.SG_LINEAGE_SHOT) {
      await page.locator("#session-grid").screenshot({ path: process.env.SG_LINEAGE_SHOT });
    }

    // A press on a cell that an arrow crosses still reaches the cell: the
    // middle of lead->w1 is over some cell or gap, never the overlay.
    const under = await page.evaluate(() => {
      const p = document.querySelector("#session-grid .sg-lineage-arrow.parent");
      const m = p.getPointAtLength(p.getTotalLength() / 2);
      const s = p.ownerSVGElement.getBoundingClientRect();
      const hit = document.elementFromPoint(s.left + m.x, s.top + m.y);
      return hit && hit.closest("svg") ? "svg" : "page";
    });
    assert.equal(under, "page");

    // Another selection replaces the drawing: a root has only child links.
    await cell("lead").click();
    got = await drawn();
    assert.deepEqual(got.arrows.map((a) => [a.kind, a.from, a.to]),
      [["child", "lead", "w1"], ["child", "lead", "w2"]]);
    assert.deepEqual(got.rings.sort(), [["w1", "child"], ["w2", "child"]]);
    assert.equal(await page.locator("#session-grid .sg-lineage").count(), 1, "one overlay at a time");

    // A session with no relatives in the grid draws nothing and clears the last.
    await cell("solo").click();
    assert.equal(await drawn(), null);
    assert.equal(await page.locator("#session-grid .lineage-parent, #session-grid .lineage-child").count(), 0);

    // A rail of another width reflows the cells; the arrows follow.
    await cell("w1").click();
    await page.evaluate(() => { document.getElementById("session-grid").style.width = "260px"; });
    await page.waitForFunction(() => {
      const host = document.getElementById("session-grid");
      const p = host.querySelector(".sg-lineage-arrow.child");
      const c = host.querySelector('.sg-cell[data-name="g1"]');
      if (!p || !c) return false;
      const s = p.ownerSVGElement.getBoundingClientRect(), r = c.getBoundingClientRect();
      const e = p.getPointAtLength(p.getTotalLength());
      return e.x + s.left >= r.left - 2 && e.x + s.left <= r.right + 2
        && e.y + s.top >= r.top - 2 && e.y + s.top <= r.bottom + 2;
    }, null, { timeout: 5000 });

    // The list view has no overlay; coming back to the grid draws it again.
    await page.locator('input[name="session-view"][value="list"]').check({ force: true });
    assert.equal(await page.locator("#session-grid .sg-lineage").count(), 0);
    await page.locator('input[name="session-view"][value="grid"]').check({ force: true });
    await page.locator("#session-grid .sg-lineage").waitFor({ state: "attached" });

    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
    server.close();
  }
  console.log("sessiongridlineage_browser: ok");
})().catch((e) => { console.error(e); process.exit(1); });
