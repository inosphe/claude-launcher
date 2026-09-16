/* Browser integration: CLAUNCH_PLAYWRIGHT may name an installed playwright-core.
   Run: node tests/observer_browser.cjs. No live daemon or agent receives input. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require(process.env.CLAUNCH_PLAYWRIGHT || "playwright");
const root = path.join(__dirname, "../src/claude_launcher/web/static");
const sent = [];
let observerReads = 0, needsLogin = true;
const data = { enabled: true, sessions: [
  { name: "s1", status: "busy", running: true, meshes: ["team-a"], summary: "테스트 12개 통과. 결정을 기다립니다.",
    generated_at: new Date().toISOString(), events: [{ id: "e1", kind: "action", text: "배포 환경을 선택하십시오.",
      needs_action: true, source: "transcript:1", at: new Date().toISOString(), acknowledged: false }] },
  { name: "s2", status: "idle", running: true, meshes: ["team-b"], summary: "병합 완료", events: [] },
] };
const server = http.createServer((req, res) => {
  let body = "";
  req.on("data", chunk => body += chunk);
  req.on("end", () => {
    if (req.url.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");
      if (req.url === "/api/observer") {
        observerReads++;
        if(needsLogin) { needsLogin=false; res.statusCode=401; return res.end('{}'); }
        return res.end(JSON.stringify(data));
      }
      if (req.url.endsWith("/events/e1")) return res.end('{"content":"Which environment?"}');
      if(req.method === "GET") {
        if(req.url === "/api/daemon") return res.end('{"version":"test","boot_id":"test"}');
        if(/^\/api\/(profiles|sessions|workspaces|mesh|cflow|harnesses|roles)/.test(req.url)) return res.end('[]');
        return res.end('{}');
      }
      sent.push({ url: req.url, body: JSON.parse(body || "{}") });
      return res.end('{"ok":true}');
    }
    const file = path.join(root, (req.url === "/" ? "index.html" : req.url.replace("/static/", "")));
    try {
      res.setHeader("Content-Type", file.endsWith(".js") ? "application/javascript" : file.endsWith(".css") ? "text/css" : "text/html");
      res.end(fs.readFileSync(file));
    } catch { res.statusCode = 404; res.end(); }
  });
});
(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CLAUNCH_CHROMIUM || undefined });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    await page.addInitScript(() => {
      localStorage.setItem("claunch_token:/", "fixture-token");
      const interval=window.setInterval;
      window.setInterval=(fn,ms,...args)=>interval(fn,ms===10000?100:ms,...args);
    });
    const errors = []; page.on("pageerror", error => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/#/observer`);
    await page.waitForSelector(".observer-card");
    assert.equal(await page.locator(".observer-card").count(), 2);
    assert(sent.some(r=>r.url==="/api/auth/session" && r.body.token==="fixture-token"));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.selectOption("#observer-scope", "mesh"); await page.selectOption("#observer-selection", "team-b");
    assert.equal(await page.locator(".observer-card").count(), 1);
    assert.match(await page.locator(".observer-card").innerText(), /s2/);
    await page.selectOption("#observer-scope", "session"); await page.selectOption("#observer-selection", "s1");
    assert.equal(await page.locator(".observer-card").count(), 1);
    await page.selectOption("#observer-scope", "global"); await page.check("#observer-actions-only");
    assert.equal(await page.locator(".observer-card").count(), 1);
    await page.getByText("근거 · transcript:1", { exact: true }).click();
    await page.getByText('"Which environment?"', { exact: false }).waitFor();
    await page.getByText("이 세션에 입력", { exact: true }).click();
    await page.fill("#observer-prompt", "새 테스트를 실행하십시오");
    await page.click("#observer-interrupt");
    await page.waitForFunction(() => document.getElementById("observer-input-status").textContent.includes("Esc 전송됨"));
    assert.deepEqual(sent.at(-1).body, { keys: ["Escape"] });
    await page.click("#observer-send");
    await page.waitForFunction(() => document.getElementById("observer-input-status").textContent.includes("지시 전송됨"));
    assert.equal(sent.at(-1).url, "/api/sessions/s1/keys");
    assert.deepEqual(sent.at(-1).body.keys, ["새 테스트를 실행하십시오", "Enter"]);
    assert.equal(await page.inputValue("#observer-prompt"), "");
    await page.fill("#observer-prompt", "session one draft");
    await page.selectOption("#observer-target", "s2"); await page.fill("#observer-prompt", "session two draft");
    await page.selectOption("#observer-target", "s1");
    assert.equal(await page.inputValue("#observer-prompt"), "session one draft");
    if (process.env.CLAUNCH_SCREENSHOT) await page.screenshot({ path: process.env.CLAUNCH_SCREENSHOT, fullPage: true });
    await page.evaluate(() => {location.hash="#/observer/session/s2";});
    await page.waitForFunction(() => document.getElementById("observer-target").value === "s2");
    await page.uncheck("#observer-actions-only");
    assert.equal(await page.locator(".observer-card").count(), 1);
    assert.match(await page.locator(".observer-card").innerText(), /s2/);
    await page.reload();
    await page.waitForSelector(".observer-card");
    assert.equal(await page.locator(".observer-card").count(), 1);
    assert.equal(await page.inputValue("#observer-target"), "s2");
    assert.equal(await page.locator("#rail-nav a[data-page=observer]").getAttribute("class"), "active");
    await page.evaluate(() => {location.hash="#/observer";});
    await page.waitForFunction(() => document.querySelectorAll(".observer-card").length === 2);
    await page.evaluate(() => {location.hash="#/";});
    await page.waitForFunction(() => document.body.dataset.page === "home");
    await page.waitForTimeout(150);
    const stoppedReads=observerReads;
    await page.waitForTimeout(300);
    assert.equal(observerReads, stoppedReads, "observer polling stops off-page");
    await page.evaluate(() => {location.hash="#/observer";});
    await page.waitForFunction(() => document.body.dataset.page === "observer");
    await page.waitForTimeout(150);
    assert(observerReads > stoppedReads, "observer polling resumes on return");
    await page.setViewportSize({width:1280,height:900});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.goto(`http://127.0.0.1:${server.address().port}/static/observer.html`);
    await page.waitForURL("**/#/observer");
    await page.waitForSelector(".observer-card");
    assert.deepEqual(errors, []);
    console.log("PASS: mobile layout, scope/action filters, evidence, Escape, target input, per-session drafts, shared auth, deep links, polling lifecycle, legacy redirect");
  } finally { await browser.close(); server.close(); server.closeAllConnections(); }
})().catch(error => { console.error(error); server.close(); server.closeAllConnections(); process.exitCode = 1; });
