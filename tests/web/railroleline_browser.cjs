/* The selected session's role stays on the name line (claunch-hknpm).

   The rail used to move a selected card's role onto a line of its own,
   because the name line shared about 260px with the name, the mesh handle,
   the rooms and the action buttons, and the role was the pill that got cut.
   The buttons moved to their own line (claunch-ku033), so that line is no
   longer crowded and the extra role line was removed (claunch-g4owg.1 is
   gone). This harness holds the removal: selecting a card must not add a
   line, must not hide the role beside the name, and must not overflow the
   rail. It replaced the earlier harness that measured the removed line. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");
const sessions = [
  { name: "s812-with-a-longer-name", status: "exited", paused_at: "2026-10-01T00:00:00Z",
    exit_code: 0, cwd: "/repo", harness: "codex", profile: "codex",
    role: "specialist-reviewer" },
  { name: "s930", status: "idle", cwd: "/repo", harness: "claude", profile: "nc",
    role: "free-role" },
];
const meshes = {
  meshes: [{
    name: "mesh-0826",
    members: [
      { session: "s812-with-a-longer-name", local: true, handle: "s812-planner",
        role: "specialist-reviewer" },
      { session: "s930", local: true, handle: "s930-x", role: "free-role" },
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
  const list = document.getElementById("session-list");
  return [...list.querySelectorAll("li.sess-card")].map((li) => {
    const shown = (n) => !!n && n.getClientRects().length > 0;
    const head = li.querySelector(".rail-head");
    const inline = head.querySelector(".mesh-role");
    return {
      name: li.dataset.name,
      active: li.classList.contains("active"),
      // the name line is the row's first line: dot, name group, profile meta
      inlineShown: shown(inline),
      inlineText: inline ? inline.textContent : "",
      // no role line of its own on any card, selected or not
      extraRoleLine: [...li.querySelectorAll(":scope > span")]
        .filter((n) => n !== head && n.querySelector(":scope > .mesh-role")).length,
      overflow: li.scrollWidth > li.clientWidth + 1,
    };
  });
});

(async () => {
  await safeListen(server);
  const browser = await chromium.launch({ headless: true });
  try {
    const errors = [];
    for (const railWidth of [null, 560]) {
      const label = railWidth ? `${railWidth}px rail` : "default rail";
      const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
      page.on("pageerror", (e) => errors.push(String(e)));
      await page.addInitScript((w) => {
        localStorage.setItem("claunch_token:/", "fixture-token");
        if (w) localStorage.setItem("claunch_railw:/", String(w));
      }, railWidth);
      const base = `http://127.0.0.1:${server.address().port}/`;
      await page.goto(base + "#/s/s812-with-a-longer-name");
      await page.locator('#session-list li.sess-card[data-name="s930"] .rail-handle').waitFor();
      await page.locator('#session-list li.sess-card.active').waitFor();

      let rows = await measure(page);
      const pick = (n) => rows.find((r) => r.name === n);
      let sel = pick("s812-with-a-longer-name");
      assert.ok(sel.active, `${label}: the hash selects the card`);
      assert.ok(sel.inlineShown, `${label}: the selected card keeps its role beside the name`);
      assert.equal(sel.inlineText, "specialist-reviewer");
      assert.equal(sel.extraRoleLine, 0, `${label}: the selected card draws no extra role line`);
      assert.ok(!sel.overflow, `${label}: the card does not overflow the rail`);
      const other = pick("s930");
      assert.ok(other.inlineShown, `${label}: an unselected card keeps its role beside the name`);
      assert.equal(other.extraRoleLine, 0, `${label}: no card draws an extra role line`);

      // selecting another card changes nothing about where the role is drawn
      await page.locator('#session-list li.sess-card[data-name="s930"] .rail-name').click();
      await page.waitForFunction(() =>
        document.querySelector('#session-list li[data-name="s930"]').classList.contains("active"));
      rows = await measure(page);
      assert.ok(pick("s930").inlineShown
        && pick("s812-with-a-longer-name").inlineShown,
        `${label}: both cards keep the role beside the name after the selection moves`);
      assert.equal(pick("s930").extraRoleLine + pick("s812-with-a-longer-name").extraRoleLine, 0,
        `${label}: the selection moves no role line with it`);
      await page.close();
    }
    assert.deepEqual(errors, []);
    console.log("railroleline_browser: ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
