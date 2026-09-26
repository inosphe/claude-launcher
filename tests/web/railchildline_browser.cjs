/* A child row on the narrow rail keeps its └ tick and its status dot on the
   name's line.

   Under 420px the rail's @container rule stacks each row: the dot, the tick
   and the name group are ordered first and the name group is given the rest
   of the line. That "rest" used to be a calc() over assumed pixel widths of
   the tick and the dot (100% - 24px on a child row), and the └ glyph comes
   from whatever fallback font the platform has -- wider than assumed on a
   Korean Windows box, where it resolves to a CJK face -- so the name group
   no longer fit and wrapped, leaving the tick and the dot alone on a line
   above the name (claunch-gge34). This drives the shipped UI against a
   fixture daemon at the default 260px rail, with the tick forced to a full
   em so the check does not depend on the machine's fonts, and asserts the
   tick, the dot and the name share one line on the parent and on children
   at depth 1 and 4.

   The same rows also keep the ▸ toggle on the buttons' line. The meta text
   there was based on its own width, and a flex line breaks before anything
   shrinks, so once a child row's indent grew at depth 2 the ▸ took a line of
   its own (claunch-gge34.1). */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");
const sessions = [
  { name: "p0", status: "idle", cwd: "/repo", harness: "claude", model: "opus" },
  { name: "c1", status: "busy", cwd: "/repo/.claude/worktrees/p0-c1", harness: "claude", model: "opus", parent: "p0" },
  { name: "c2", status: "idle", cwd: "/repo", harness: "claude", model: "opus", parent: "c1" },
  { name: "c3", status: "idle", cwd: "/repo", harness: "claude", model: "opus", parent: "c2" },
  { name: "c4-with-a-rather-long-session-name", status: "idle", cwd: "/repo", harness: "claude", model: "opus", parent: "c3" },
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
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on("pageerror", e => errors.push(String(e)));
    await page.addInitScript(() => {
      localStorage.setItem("claunch_token:/", "fixture-token");
    });
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.locator('#session-list li.sess-card[data-name="c4-with-a-rather-long-session-name"]').waitFor();
    // With `wideTick` the tick is drawn a full em wide -- what a CJK fallback
    // face gives box-drawing characters -- so the check does not depend on
    // which fonts this machine has.
    const rows = (wideTick) => page.evaluate((wideTick) => {
      const probe = document.getElementById("rail-tick-probe") || document.createElement("style");
      probe.id = "rail-tick-probe";
      probe.textContent = wideTick
        ? "#session-list li.child::before { display: inline-block; width: 1em; }" : "";
      document.head.appendChild(probe);
      const mid = (r) => (r.top + r.bottom) / 2;
      return {
        rail: document.getElementById("sidebar").getBoundingClientRect().width,
        rows: [...document.querySelectorAll("#session-list li.sess-card")].map((li) => {
          const name = li.querySelector(".rail-name").getBoundingClientRect();
          const dot = li.querySelector(".dot").getBoundingClientRect();
          const info = li.querySelector(".sess-info").getBoundingClientRect();
          const toggle = li.querySelector(".sess-brief-toggle").getBoundingClientRect();
          const tick = getComputedStyle(li, "::before");
          const liBox = li.getBoundingClientRect();
          const padTop = parseFloat(getComputedStyle(li).paddingTop);
          return {
            name: li.dataset.name, child: li.classList.contains("child"),
            nameMid: mid(name), dotMid: mid(dot), nameWidth: name.width,
            infoMid: mid(info), toggleMid: mid(toggle),
            // The pseudo-element has no box of its own to measure; the name
            // leaving the row's first line is how its wrap shows.
            firstLine: liBox.top + padTop, nameTop: name.top,
            tickContent: tick.content,
          };
        }),
      };
    }, wideTick);
    for (const wideTick of [false, true]) {
      const got = await rows(wideTick);
      assert.ok(got.rail <= 420, `the rail is narrow enough for the stacked layout (${got.rail}px)`);
      assert.equal(got.rows.length, sessions.length);
      for (const r of got.rows) {
        const where = `${r.name} (wide tick: ${wideTick})`;
        if (r.child) assert.equal(r.tickContent, '"└"', `${where} draws the tick`);
        assert.ok(Math.abs(r.dotMid - r.nameMid) <= 4,
          `${where}: the dot shares the name's line (dot ${r.dotMid}, name ${r.nameMid})`);
        assert.ok(r.nameTop - r.firstLine <= 8,
          `${where}: the name sits on the row's first line (${r.nameTop - r.firstLine}px below it)`);
        assert.ok(r.nameWidth >= 20, `${where}: the name keeps some width (${r.nameWidth}px)`);
        assert.ok(Math.abs(r.toggleMid - r.infoMid) <= 4,
          `${where}: the ▸ shares the buttons' line (▸ ${r.toggleMid}, ⓘ ${r.infoMid})`);
      }
    }
    assert.deepEqual(errors, []);
    console.log("railchildline_browser: ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
