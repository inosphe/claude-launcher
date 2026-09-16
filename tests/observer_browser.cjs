/* Browser integration: CLAUNCH_PLAYWRIGHT may name an installed playwright-core.
   Run: node tests/observer_browser.cjs. No live daemon or agent receives input. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require(process.env.CLAUNCH_PLAYWRIGHT || "playwright");
const root = path.join(__dirname, "../src/claude_launcher/web/static");
const sent = [];
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
      if (req.url === "/api/observer") return res.end(JSON.stringify(data));
      if (req.url.endsWith("/events/e1")) return res.end('{"content":"Which environment?"}');
      sent.push({ url: req.url, body: JSON.parse(body || "{}") });
      return res.end('{"ok":true}');
    }
    const file = path.join(root, req.url.replace("/static/", ""));
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
    const errors = []; page.on("pageerror", error => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/static/observer.html`);
    await page.waitForSelector(".card");
    assert.equal(await page.locator(".card").count(), 2);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.selectOption("#scope", "mesh"); await page.selectOption("#selection", "team-b");
    assert.equal(await page.locator(".card").count(), 1);
    assert.match(await page.locator(".card").innerText(), /s2/);
    await page.selectOption("#scope", "session"); await page.selectOption("#selection", "s1");
    assert.equal(await page.locator(".card").count(), 1);
    await page.selectOption("#scope", "global"); await page.check("#actions-only");
    assert.equal(await page.locator(".card").count(), 1);
    await page.getByText("근거 · transcript:1", { exact: true }).click();
    await page.getByText('"Which environment?"', { exact: false }).waitFor();
    await page.getByText("이 세션에 입력", { exact: true }).click();
    await page.fill("#prompt", "새 테스트를 실행하십시오");
    await page.click("#interrupt");
    await page.waitForFunction(() => document.getElementById("input-status").textContent.includes("Esc 전송됨"));
    assert.deepEqual(sent.at(-1).body, { keys: ["Escape"] });
    await page.click("#send");
    await page.waitForFunction(() => document.getElementById("input-status").textContent.includes("지시 전송됨"));
    assert.equal(sent.at(-1).url, "/api/sessions/s1/keys");
    assert.deepEqual(sent.at(-1).body.keys, ["새 테스트를 실행하십시오", "Enter"]);
    assert.equal(await page.inputValue("#prompt"), "");
    await page.fill("#prompt", "session one draft");
    await page.selectOption("#target", "s2"); await page.fill("#prompt", "session two draft");
    await page.selectOption("#target", "s1");
    assert.equal(await page.inputValue("#prompt"), "session one draft");
    if (process.env.CLAUNCH_SCREENSHOT) await page.screenshot({ path: process.env.CLAUNCH_SCREENSHOT, fullPage: true });
    assert.deepEqual(errors, []);
    console.log("PASS: mobile layout, scope/action filters, evidence, Escape, target input, per-session drafts");
  } finally { await browser.close(); server.close(); server.closeAllConnections(); }
})().catch(error => { console.error(error); server.close(); server.closeAllConnections(); process.exitCode = 1; });
