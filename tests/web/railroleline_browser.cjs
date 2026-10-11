/* The selected session's role, whole on a line of its own (claunch-g4owg.1).

   On the name line the role shares about 260px with the name, the mesh
   handle and the rooms, and it is the pill that gets cut: "specialist-
   reviewer" reads "spe…". Selecting a session draws its role again on a
   line under the name, whole, and takes it off the name line so it is not
   drawn twice. Every other card is unchanged: role beside the name, no
   extra line. Selecting another card moves the line with it. */
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
    const box = (n) => n.getBoundingClientRect();
    const shown = (n) => !!n && n.getClientRects().length > 0;
    const head = li.querySelector(".rail-head");
    const inline = head.querySelector(".mesh-role");
    const line = li.querySelector(":scope > .rail-role-line");
    const whole = line && line.querySelector(".mesh-role");
    return {
      name: li.dataset.name,
      active: li.classList.contains("active"),
      inlineShown: shown(inline),
      inlineClipped: shown(inline) && inline.scrollWidth > inline.clientWidth + 1,
      lineShown: shown(line),
      lineText: whole ? whole.textContent : "",
      // whole: its text fits its own box, and the box fits the card
      lineClipped: shown(whole) && (whole.scrollWidth > whole.clientWidth + 1
        || box(whole).right > box(li).right + 1),
      lineBelowName: shown(line) && box(line).top >= box(head).bottom - 1,
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
      if (!railWidth) {
        // the reason for the line: beside the name this role does not fit
        await page.evaluate(() => document.querySelector("#session-list li.active").classList.remove("active"));
        const before = (await measure(page)).find((r) => r.name === sel.name);
        assert.ok(before.inlineClipped, `${label}: unselected, the long role is cut on the name line`);
        await page.evaluate(() => document.querySelector('#session-list li[data-name="s812-with-a-longer-name"]').classList.add("active"));
        rows = await measure(page);
        sel = pick("s812-with-a-longer-name");
      }
      assert.ok(sel.lineShown, `${label}: the selected card draws its role line`);
      assert.equal(sel.lineText, "specialist-reviewer");
      assert.ok(!sel.lineClipped, `${label}: the role on its line is whole`);
      assert.ok(sel.lineBelowName, `${label}: the role line sits under the name line`);
      assert.ok(!sel.inlineShown, `${label}: the name line's role steps aside`);
      assert.ok(!sel.overflow, `${label}: the card does not overflow the rail`);
      const other = pick("s930");
      assert.ok(!other.lineShown && other.inlineShown,
        `${label}: an unselected card keeps its role beside the name, no extra line`);

      // selecting another card moves the line
      await page.locator('#session-list li.sess-card[data-name="s930"] .rail-name').click();
      await page.waitForFunction(() =>
        document.querySelector('#session-list li[data-name="s930"]').classList.contains("active"));
      rows = await measure(page);
      assert.ok(pick("s930").lineShown && !pick("s930").inlineShown, `${label}: the line follows the selection`);
      assert.ok(!pick("s812-with-a-longer-name").lineShown
        && pick("s812-with-a-longer-name").inlineShown, `${label}: and leaves the card it was on`);
      await page.close();
    }
    assert.deepEqual(errors, []);
    console.log("railroleline_browser: ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
