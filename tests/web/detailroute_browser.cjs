/* The session detail rail is the address's `?detail=<name>`, in the shipped UI
   against a local fixture daemon (no real session is controlled).

   The regression: the rail lived only in memory, so a link to another page —
   the Operator or Observer tab — left some session's rail docked beside that
   page, and a reload of the very same address showed no rail at all. The URL
   and the screen disagreed in both directions. Checked here: every nav tab
   closes it, Back reopens it, a reload of an address that carries it shows it,
   an open rail follows a terminal switch, and on a phone the detail is a page
   that the address alone brings back.

   Requires Playwright (NODE_PATH may point to an external install).
   Run: node tests/web/detailroute_browser.cjs */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");
const sessions = ["s1", "s2", "s3"].map((name) => ({
  name, status: "exited", paused_at: "2026-09-18T00:00:00Z", cwd: "/repo", harness: "codex",
}));
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
    let data = {};
    if (url.pathname === "/api/daemon") data = { version: "fixture", boot_id: "test" };
    else if (url.pathname === "/api/sessions") data = { sessions, llm_configured: false };
    else if (url.pathname.endsWith("/capture")) data = { lines: ["Session snapshot"] };
    else if (/^\/api\/sessions\/[^/]+\/meta$/.test(url.pathname)) {
      const name = decodeURIComponent(url.pathname.split("/")[3]);
      data = { session: sessions.find((s) => s.name === name) || { name } };
    }
    else if (url.pathname === "/api/observer") data = { sessions: [], enabled: false };
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
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const browser = await chromium.launch({ headless: true });
  let checks = 0;
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(`${e} @ ${page.url()}\n${e.stack}`));
    await page.addInitScript(() => localStorage.setItem("claunch_token:/", "fixture-token"));
    const origin = `http://127.0.0.1:${server.address().port}`;
    const hash = () => page.evaluate(() => location.hash);
    const railUp = () => page.evaluate(() => {
      const v = document.getElementById("sess-view");
      return !v.classList.contains("hidden") && v.getBoundingClientRect().width > 0;
    });
    const until = (fn, arg) => page.waitForFunction(fn, arg, { timeout: 5000 });
    const railIs = (up) => until((up) => {
      const v = document.getElementById("sess-view");
      return (!v.classList.contains("hidden") && v.getBoundingClientRect().width > 0) === up;
    }, up);

    await page.goto(`${origin}/#/s/s1`);
    await page.locator('#session-list li .sess-info').first().waitFor();
    await until(() => typeof currentName !== "undefined" && currentName === "s1");
    await railIs(false);

    // ---- wide: every page link closes it, Back brings it back ----
    for (const nav of ["#/operator", "#/observer", "#/beads", "#/flows", "#/mesh", "#/"]) {
      // Back on s1's terminal with the rail closed, the way a person gets
      // there: the header's `details` is the toggle.
      if (await railUp()) await page.locator("#term-details").click();
      await until(() => location.hash === "#/s/s1");
      await railIs(false);
      await page.locator("#term-details").click();
      await until(() => location.hash === "#/s/s1?detail=s1");
      await railIs(true); checks++;
      await page.locator(`#nav a[href="${nav}"], a[data-page][href="${nav}"]`).first().click();
      await until((nav) => location.hash === nav, nav);
      await railIs(false); checks++;
      await page.goBack();
      await until(() => location.hash === "#/s/s1?detail=s1");
      await railIs(true); checks++;
    }

    // ---- the close button and Back past the ⓘ both close it on its terminal ----
    await page.locator("#term-details").click();
    await until(() => location.hash === "#/s/s1");
    await railIs(false); checks++;
    await page.locator("#term-details").click();
    await railIs(true);
    await page.goBack();
    await until(() => location.hash === "#/s/s1");
    await railIs(false); checks++;

    // ---- an open rail follows a terminal switch, in the address too ----
    await page.locator("#term-details").click();
    await railIs(true);
    await page.evaluate(() => { location.hash = "#/s/s2"; });
    await until(() => location.hash === "#/s/s2?detail=s2");
    await railIs(true); checks++;
    assert.equal(await page.locator('#session-list .sess-info.on').getAttribute("data-name"), "s2");
    checks++;

    // ---- a reload shows exactly what the address says ----
    await page.goto(`${origin}/#/operator?detail=s3`);
    await railIs(true); checks++;
    await page.reload();
    await railIs(true); checks++;
    await page.goto(`${origin}/#/operator`);
    await page.reload();
    await railIs(false); checks++;

    // ---- a phone: the detail is the page, and the address carries it ----
    await page.setViewportSize({ width: 390, height: 800 });
    await page.goto(`${origin}/#/?detail=s1`);
    await until(() => document.getElementById("sess-view").parentNode.id === "main");
    await railIs(true); checks++;
    await page.goBack().catch(() => {});
    await page.evaluate(() => { location.hash = "#/"; });
    await until(() => location.hash === "#/");
    await railIs(false); checks++;
    // narrowing with a rail open closes it and takes it out of the address
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`${origin}/#/operator?detail=s1`);
    await railIs(true);
    await page.setViewportSize({ width: 390, height: 800 });
    await until(() => location.hash === "#/operator");
    await railIs(false); checks++;

    assert(!(await railUp()));
    assert.deepEqual(errors, []);
    console.log(`detailroute browser checks passed (${checks})`);
  } finally {
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
})().catch((e) => { console.error(e); process.exitCode = 1; server.close(); });
