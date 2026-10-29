/* A top-level rail row whose parent is gone is drawn as an orphan
   (claunch-ikiys).

   When a parent's record is removed and no session above it survives, the
   daemon clears the child's `parent` and keeps the lost name in `orphan_of`.
   A child whose parent is on record but not on the rail (archived, filtered
   out) is drawn at the top level too. Both used to look exactly like a root a
   person started. This drives the shipped UI against a fixture daemon and
   asserts what the row builder in refreshSessions draws: the orphan rows carry
   the `orphan` class, `data-orphan-of` and a "고아" tag naming the lost parent
   in its title; a root and a child shown under its parent carry none of it. */
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
  { ...base, name: "kid", orphan_of: "gone" },
  { ...base, name: "stray", parent: "archived-one" },
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
    });
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.locator('#session-list li.sess-card[data-name="solo"]').waitFor();
    const rows = await page.evaluate(() =>
      Object.fromEntries([...document.querySelectorAll("#session-list li.sess-card")].map((li) => {
        const tags = li.querySelectorAll(".rail-orphan");
        return [li.dataset.name, {
          orphan: li.classList.contains("orphan"),
          child: li.classList.contains("child"),
          orphanOf: li.dataset.orphanOf || null,
          tags: tags.length,
          tagText: tags[0] ? tags[0].textContent : null,
          tagTitle: tags[0] ? tags[0].title : null,
        }];
      })));
    assert.deepEqual(Object.keys(rows).sort(), sessions.map((s) => s.name).sort());

    assert.deepEqual(rows.kid, {
      orphan: true, child: false, orphanOf: "gone", tags: 1, tagText: "고아",
      tagTitle: "orphan: parent gone was removed",
    }, "the daemon's orphan record is drawn as an orphan");
    assert.deepEqual(rows.stray, {
      orphan: true, child: false, orphanOf: "archived-one", tags: 1, tagText: "고아",
      tagTitle: "orphan: parent archived-one is not on this list",
    }, "a parent missing from the rail leaves its child drawn as an orphan");
    for (const name of ["lead", "solo", "w1"]) {
      assert.equal(rows[name].orphan, false, `${name} is not marked as an orphan`);
      assert.equal(rows[name].orphanOf, null, `${name} carries no data-orphan-of`);
      assert.equal(rows[name].tags, 0, `${name} has no orphan tag`);
    }
    assert.equal(rows.w1.child, true, "a child under its parent stays a child row");
    assert.deepEqual(errors, []);
    console.log("railorphan_browser: ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
