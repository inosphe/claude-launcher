/* The list card's two densities (claunch-g4owg).

   Before: the name, its tags, the state text and the row's buttons shared
   one line on a rail wider than 420px, and the tags were the only part that
   could shrink -- so a paused row, whose state text reads
   "codex/codex · paused", had its role cut to "fre…" while the running row
   under it showed "free-role" whole. How much of the role survived depended
   on the session's state.

   Expanded (the default): the name and its tags take the first line on
   their own, at both the default 260px rail and a wide one, and no tag is
   clipped -- the handle, the role and the room. Compact: one line per
   session -- the name, the role, the state text and the ⓘ share a line,
   the detail lines, the handle and the room are not drawn, and the role is
   still whole. The choice is remembered across a reload. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");
const sessions = [
  { name: "s812", status: "exited", paused_at: "2026-10-01T00:00:00Z", exit_code: 0,
    cwd: "/repo/.claude/worktrees/wt-20260929-012232", harness: "codex", profile: "codex",
    role: "free-role" },
  { name: "s930", status: "idle", cwd: "/repo/.claude/worktrees/wt-20261009-145501",
    harness: "claude", profile: "nc", role: "specialist-reviewer" },
  { name: "s954-with-a-longer-name", status: "busy", cwd: "/repo", harness: "claude",
    profile: "nc", role: "worker", parent: "s930" },
];
// Every row is a mesh member under a handle of its own, so each carries the
// three pills the name line can hold: the handle, the role and the room.
// Most sessions on a working daemon look like this.
const meshes = {
  meshes: [{
    name: "mesh-0826",
    members: [
      { session: "s812", local: true, handle: "s812-planner", role: "free-role" },
      { session: "s930", local: true, handle: "s930-x", role: "reviewer" },
      { session: "s954-with-a-longer-name", local: true, handle: "worker-r13", role: "worker" },
    ],
  }],
};
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
    let data = {};
    if (url.pathname === "/api/daemon") data = { version: "fixture", boot_id: "test" };
    else if (url.pathname === "/api/sessions") data = { sessions, llm_configured: true };
    else if (url.pathname === "/api/mesh") data = meshes;
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

const measure = (page) => page.evaluate(() => {
  const mid = (r) => (r.top + r.bottom) / 2;
  const list = document.getElementById("session-list");
  return {
    rail: document.getElementById("sidebar").getBoundingClientRect().width,
    density: list.classList.contains("density-compact") ? "compact"
      : list.classList.contains("density-expanded") ? "expanded" : "",
    rows: [...list.querySelectorAll("li.sess-card")].map((li) => {
      const box = (n) => n.getBoundingClientRect();
      const name = li.querySelector(".rail-name");
      const role = li.querySelector(".mesh-role");
      const head = li.querySelector(".rail-head");
      // Cut by its own ellipsis, or by the name group's overflow around it --
      // the second is how a rigid pill gets cut.
      const clipped = (n) => n.scrollWidth > n.clientWidth + 1
        || box(n).right > box(head).right + 1;
      // Boxes, not the display value: a pill inside a hidden group still
      // reads its own display.
      const shown = (n) => n.getClientRects().length > 0;
      const pills = [...head.querySelectorAll(".rail-handle, .mesh-role, .rail-mesh")];
      const meta = li.querySelector(".meta");
      const info = li.querySelector(".sess-info");
      const cwd = li.querySelector(".rail-cwd");
      return {
        name: li.dataset.name,
        roleText: role.textContent,
        roleClipped: clipped(role),
        handleShown: !!head.querySelector(".rail-handle") && shown(head.querySelector(".rail-handle")),
        meshShown: !!head.querySelector(".rail-mesh") && shown(head.querySelector(".rail-mesh")),
        clippedPills: pills.filter((n) => shown(n) && clipped(n)).map((n) => n.textContent),
        nameMid: mid(box(name)), nameBottom: box(name).bottom,
        roleMid: mid(box(role)),
        metaMid: mid(box(meta)), metaTop: box(meta).top,
        metaShown: getComputedStyle(meta).display !== "none",
        infoMid: mid(box(info)),
        cwdShown: !!cwd && getComputedStyle(cwd).display !== "none",
        overflow: li.scrollWidth > li.clientWidth + 1,
      };
    }),
  };
});

(async () => {
  await safeListen(server);
  const browser = await chromium.launch({ headless: true });
  try {
    const errors = [];
    const open = async (railWidth) => {
      const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
      page.on("pageerror", (e) => errors.push(String(e)));
      await page.addInitScript((w) => {
        localStorage.setItem("claunch_token:/", "fixture-token");
        if (w) localStorage.setItem("claunch_railw:/", String(w));
      }, railWidth);
      await page.goto(`http://127.0.0.1:${server.address().port}`);
      // The handles arrive with the mesh poll, after the first rows.
      await page.locator('#session-list li.sess-card[data-name="s954-with-a-longer-name"] .rail-handle').waitFor();
      return page;
    };

    for (const railWidth of [null, 560]) {
      const page = await open(railWidth);
      const label = railWidth ? `${railWidth}px rail` : "default rail";

      const exp = await measure(page);
      if (railWidth) assert.ok(exp.rail > 420, `${label} is wide (${exp.rail}px)`);
      assert.equal(exp.density, "expanded", `${label}: expanded is the default`);
      assert.equal(exp.rows.length, sessions.length);
      for (const r of exp.rows) {
        const where = `${label}, expanded, ${r.name}`;
        assert.ok(r.handleShown && r.meshShown, `${where}: the handle and the room are drawn`);
        assert.deepEqual(r.clippedPills, [], `${where}: no pill on the name line is cut`);
        assert.ok(Math.abs(r.roleMid - r.nameMid) <= 4 || r.roleMid > r.nameMid,
          `${where}: the role sits with the name`);
        assert.ok(r.metaTop >= r.nameBottom - 1,
          `${where}: the state text is on a line under the name (${r.metaTop} vs ${r.nameBottom})`);
        assert.ok(r.cwdShown, `${where}: the directory line is drawn`);
        assert.ok(!r.overflow, `${where}: the row does not overflow the rail`);
      }

      await page.locator('#session-density label:has(input[value="compact"])').click();
      const cmp = await measure(page);
      assert.equal(cmp.density, "compact");
      for (const r of cmp.rows) {
        const where = `${label}, compact, ${r.name}`;
        assert.ok(!r.roleClipped, `${where}: the role "${r.roleText}" is drawn whole`);
        // The handle and the room are the expanded card's and the ⓘ panel's:
        // beside a whole role they were what cut it on the 260px line.
        assert.ok(!r.handleShown && !r.meshShown, `${where}: the handle and the room are not drawn`);
        // The state text is on the line where there is room for it, and off
        // the narrow rail's compact line, which is all name and role.
        assert.equal(r.metaShown, !!railWidth, `${where}: the state text is drawn only on the wide rail`);
        const line = [["role", r.roleMid], ["ⓘ", r.infoMid]];
        if (r.metaShown) line.push(["state text", r.metaMid]);
        for (const [what, at] of line) {
          assert.ok(Math.abs(at - r.nameMid) <= 4, `${where}: the ${what} shares the name's line`);
        }
        assert.ok(!r.cwdShown, `${where}: the directory line is not drawn`);
        assert.ok(!r.overflow, `${where}: the row does not overflow the rail`);
      }

      // Remembered per browser.
      await page.reload();
      await page.locator('#session-list li.sess-card[data-name="s954-with-a-longer-name"]').waitFor();
      assert.equal((await measure(page)).density, "compact", `${label}: compact survives a reload`);
      assert.ok(await page.locator('#session-density input[value="compact"]').isChecked());
      await page.locator('#session-density label:has(input[value="expanded"])').click();
      assert.equal((await measure(page)).density, "expanded");

      // The grid draws its own cells, so the density toggle leaves with the list.
      await page.locator('#session-view label:has(input[value="grid"])').click();
      assert.ok(await page.locator("#session-density").isHidden(), `${label}: hidden in the grid`);
      await page.locator('#session-view label:has(input[value="list"])').click();
      assert.ok(await page.locator("#session-density").isVisible());
      await page.close();
    }
    assert.deepEqual(errors, []);
    console.log("raildensity_browser: ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
