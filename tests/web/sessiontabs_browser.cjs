/* Full shipped UI with a local fixture daemon; no real session is controlled. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");
const writes = [];
const sessions = Array.from({ length: 9 }, (_, i) => ({
  name: `s${i + 1}`, status: i === 7 ? "busy" : "exited",
  paused_at: i === 7 ? null : "2026-09-18T00:00:00Z", cwd: "/repo", harness: "codex",
}));
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
    if (req.method !== "GET") writes.push(req.url);
    let data = {};
    if (url.pathname === "/api/daemon") data = { version: "fixture", boot_id: "test" };
    else if (url.pathname === "/api/sessions") data = { sessions, llm_configured: false };
    else if (url.pathname.endsWith("/capture")) data = { lines: ["Session snapshot"] };
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
      if (!localStorage.getItem("tabs-seeded")) {
        localStorage.setItem("claunch_session_pins:/", '["s9"]');
        localStorage.setItem("tabs-seeded", "true");
      }
    });
    const origin = `http://127.0.0.1:${server.address().port}`;
    await page.goto(origin);
    await page.locator('.session-tab[data-name="s9"]').waitFor();
    const names = () => page.locator(".session-tab").evaluateAll(els => els.map(el => el.dataset.name));
    const tab = name => page.locator(`.session-tab[data-name="${name}"]`);
    async function visit(name) {
      await page.evaluate(name => { location.hash = `#/s/${name}`; }, name);
      await page.waitForFunction(name => document.querySelector(`.session-tab[data-name="${name}"] .session-tab-open`)?.getAttribute("aria-current") === "page", name);
    }
    assert.deepEqual(await names(), ["s9"], "legacy pin migrates");
    assert.equal(await page.locator(".session-pinbar").count(), 0);
    for (const name of ["s1", "s2", "s3", "s4", "s5"]) await visit(name);
    await tab("s1").locator("a").click();
    await page.waitForFunction(() => location.hash === "#/s/s1" && document.querySelector('.session-tab[data-name="s1"]').classList.contains("active"));
    assert.deepEqual(await names(), ["s9", "s1", "s2", "s3", "s4", "s5"]);
    await visit("s6");
    assert.deepEqual(await names(), ["s9", "s1", "s3", "s4", "s5", "s6"]);
    await tab("s1").locator(".session-tab-pin").click();
    await visit("s7");
    assert.deepEqual(await names(), ["s9", "s1", "s3", "s4", "s5", "s6", "s7"]);
    // Closing a pinned tab must not temporarily unpin it into the full LRU
    // set and evict an unrelated recent tab.
    await tab("s1").locator(".session-tab-close").click();
    assert.deepEqual(await names(), ["s9", "s3", "s4", "s5", "s6", "s7"]);
    await tab("s7").locator(".session-tab-close").click();
    await page.waitForFunction(() => location.hash === "#/s/s6");
    assert(!await tab("s1").count());
    const before = await names();
    await page.reload();
    await tab("s6").locator("a[aria-current='page']").waitFor();
    assert.deepEqual(await names(), before, "reload preserves tabs and closed pin stays closed");
    await page.locator("#term-pin").click();
    await page.waitForFunction(() => document.querySelector('.session-tab[data-name="s6"]').classList.contains("pinned"));
    await tab("s6").locator(".session-tab-pin").click();
    assert.equal(await page.locator("#term-pin").getAttribute("aria-pressed"), "false");
    await visit("s8");
    await visit("s6");
    await visit("s8"); // parked live terminal restore also updates the active tab
    assert.equal(await tab("s8").locator("a").getAttribute("aria-current"), "page");
    assert((await tab("s8").locator(".dot").boundingBox()).width > 0, "session status dot is visible");
    if (process.env.CLAUNCH_TAB_SCREENSHOT) await page.screenshot({ path: process.env.CLAUNCH_TAB_SCREENSHOT });
    for (const width of [1440, 1000, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await page.waitForTimeout(100);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `no overflow at ${width}`);
      const bounds = await page.locator("#session-tabs").boundingBox();
      assert(bounds && bounds.width <= width, "tabs remain visible");
      if (width > 820) {
        const details = await page.locator("#term-details").boundingBox();
        assert(details.y >= bounds.y + bounds.height, "details button is below tabs");
      }
    }
    await page.setViewportSize({ width: 1440, height: 900 });
    for (const name of await names()) {
      await tab(name).locator(".session-tab-close").click();
      await page.waitForFunction(name => !document.querySelector(`.session-tab[data-name="${name}"]`), name);
    }
    await page.waitForFunction(() => location.hash === "#/");
    assert.equal(await page.locator("#session-tabs").isVisible(), false);
    await page.reload();
    await page.waitForFunction(() => document.getElementById("daemon-info").textContent === "vfixture");
    assert.deepEqual(await names(), [], "closing all tabs stays empty after reload");
    assert(!writes.some(url => /\/(kill|pause|archive|input|terminate)/.test(url)), "closing tabs does not control sessions");
    assert.deepEqual(errors, []);
    console.log("sessiontabs browser checks passed");
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(e => { console.error(e); process.exitCode = 1; server.close(); });
