/* Browser integration: CLAUNCH_PLAYWRIGHT may name an installed playwright-core.
   Run: node tests/observer_browser.cjs. No live daemon or agent receives input. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require(process.env.CLAUNCH_PLAYWRIGHT || "playwright");
const root = path.join(__dirname, "../src/claude_launcher/web/static");
const sent = [];
let gates=[];
let observerReads = 0, needsLogin = true;
// The meter's days are fixed strings, not "today": the panel labels them as
// written, so a fixture that used the clock would only assert the clock.
const data = { enabled: true, sessions: [
  { name: "s1", status: "busy", running: true, meshes: ["team-a"], summary: "테스트 12개 통과. 결정을 기다립니다.",
    generated_at: new Date().toISOString(), last_activity_at: new Date(Date.now()-300000).toISOString(),
    usage_totals: { calls: 3, prompt_tokens: 3000, completion_tokens: 210,
                    prompt_cache_hit_tokens: 1200, prompt_cache_miss_tokens: 1800 },
    usage_daily: {
      "2026-09-17": { calls: 2, prompt_tokens: 2000, completion_tokens: 140,
                      prompt_cache_hit_tokens: 800, prompt_cache_miss_tokens: 1200 },
      "2026-09-16": { calls: 1, prompt_tokens: 1000, completion_tokens: 70,
                      prompt_cache_hit_tokens: 400, prompt_cache_miss_tokens: 600 } },
    events: [{ id: "e1", kind: "action", text: "배포 환경을 선택하십시오.",
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
      if(req.method==="GET" && req.url.startsWith("/api/cflow/run?")) {
        const run=gates[0];
        return res.end(JSON.stringify({cwd:"/repo",scope:"s1",sessions:["s1"],run:run||{status:"step"}}));
      }
      if(req.method==="GET" && req.url.startsWith("/api/cflow?")) return res.end(JSON.stringify({runs:gates}));
      if(req.url.endsWith("/images/image")) {res.setHeader("Content-Type","image/png");return res.end(Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=","base64"));}
      if(req.url.endsWith("/reports/question/answer")) {
        const e=data.sessions[0].events.find(e=>e.id==="question");
        e.answer={text:JSON.parse(body).text};e.needs_action=false;e.acknowledged=true;e.delivery="sent";
        sent.push({url:req.url,body:JSON.parse(body)});return res.end(JSON.stringify(e));
      }
      if(req.url==="/api/cflow/approve") gates=[];
      if(req.url==="/api/observer/settings") data.enabled=JSON.parse(body).enabled;
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
    assert.equal(await page.locator("#observer-view > header").isVisible(), false);
    assert.equal(await page.locator("#observer-mobile-monitor").isVisible(), true);
    assert.equal(await page.locator(".observer-card").first().evaluate(e=>getComputedStyle(e).fontSize), "13px");
    // The composer is most of this screen, so it arrives folded with its own
    // header standing in for it, and the header alone opens it back up.
    assert.equal(await page.locator("#observer-composer-toggle").isVisible(), true);
    assert.equal(await page.locator("#observer-composer").evaluate(e=>e.classList.contains("folded")), true);
    assert.equal(await page.locator("#observer-prompt").isVisible(), false);
    assert.equal(await page.locator("#observer-target").isVisible(), false);
    await page.click("#observer-composer-toggle");
    assert.equal(await page.locator("#observer-prompt").isVisible(), true);
    assert.equal(await page.locator("#observer-composer").evaluate(e=>e.classList.contains("folded")), false);
    await page.click("#observer-composer-toggle");
    assert.equal(await page.locator("#observer-prompt").isVisible(), false);
    // The observation meter: per session on its own post, and once for the
    // whole board. Both name a day rather than only a lifetime figure.
    const s1Post = page.locator('.observer-post[data-session="s1"]');
    assert.equal(await s1Post.count(), 1);
    assert.match(await s1Post.locator(".observer-updated").innerText(), /업데이트 방금/);
    assert.match(await s1Post.locator(".observer-updated").innerText(), /활동 5분 전/);
    const meter = s1Post.locator("details", { hasText: "관찰 API 사용량 (누적)" });
    await meter.locator("summary").click();
    const meterText = await meter.locator("pre").innerText();
    assert.match(meterText, /누적\s+호출 3회 · 입력 3,000 · 출력 210 · 캐시적중 1,200 · 미스 1,800/);
    assert.match(meterText, /2026-09-17\s+호출 2회/);
    assert.match(meterText, /2026-09-16\s+호출 1회/);
    await page.locator("#observer-usage summary").click();
    const boardUsage = await page.locator("#observer-usage-body").innerText();
    assert.match(boardUsage, /누적\s+호출 3회 · 입력 3,000 · 출력 210 · 캐시적중 1,200 · 미스 1,800/);
    assert.match(boardUsage, /2026-09-17\s+호출 2회/);
    await page.locator("#observer-usage summary").click();
    await page.click("#observer-mobile-monitor");
    await page.waitForFunction(()=>document.getElementById("observer-mobile-monitor").textContent==="관찰 시작");
    assert(sent.some(r=>r.url==="/api/observer/settings" && r.body.enabled===false));
    await page.click("#observer-mobile-monitor");
    await page.waitForFunction(()=>document.getElementById("observer-mobile-monitor").textContent==="관찰 끄기");
    await page.setViewportSize({width:1280,height:900});
    assert.equal(await page.locator("#observer-view > header").isVisible(), true);
    assert.equal(await page.locator("#observer-mobile-monitor").isVisible(), false);
    await page.setViewportSize({width:390,height:844});
    assert(sent.some(r=>r.url==="/api/auth/session" && r.body.token==="fixture-token"));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.selectOption("#observer-scope", "mesh"); await page.selectOption("#observer-selection", "team-b");
    await page.waitForFunction(()=>document.querySelector("#observer-target option[value=s2]")&&!document.querySelector("#observer-target option[value=s1]"));
    assert.deepEqual(await page.locator("#observer-target option").evaluateAll(es=>es.map(e=>e.value)),["","s2"]);
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
    assert.deepEqual(await page.locator("#observer-target option").evaluateAll(es=>es.map(e=>e.value)),["","s1"]);
    await page.uncheck("#observer-actions-only");
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
    assert.equal(await page.locator("#observer-mobile-monitor").isVisible(),false);
    await page.waitForTimeout(150);
    const stoppedReads=observerReads;
    await page.waitForTimeout(300);
    assert.equal(observerReads, stoppedReads, "observer polling stops off-page");
    await page.evaluate(() => {location.hash="#/observer";});
    await page.waitForFunction(() => document.body.dataset.page === "observer");
    await page.waitForTimeout(150);
    assert(observerReads > stoppedReads, "observer polling resumes on return");
    // Folded on the phone, and desktop ignores that state rather than
    // inheriting it: there the whole footer is laid out and the header is gone.
    const isFolded = () => page.locator("#observer-composer").evaluate(e=>e.classList.contains("folded"));
    if (!await isFolded()) await page.click("#observer-composer-toggle");
    assert.equal(await isFolded(), true);
    assert.equal(await page.locator("#observer-prompt").isVisible(), false);
    await page.setViewportSize({width:1280,height:900});
    await page.waitForSelector(".observer-board");
    assert.equal(await page.locator("#observer-composer").evaluate(e=>e.classList.contains("folded")), false);
    assert.equal(await page.locator("#observer-prompt").isVisible(), true);
    assert.equal(await page.locator("#observer-composer-toggle").isVisible(), false);
    assert((await page.locator('.observer-column[data-session="s1"] details', { hasText: "관찰 API 사용량 (누적)" }).count()) >= 1,
      "the desktop column keeps its own meter");
    const beforeOrder=await page.locator(".observer-column").evaluateAll(es=>es.map(e=>e.dataset.session));
    data.sessions[1].last_activity_at=new Date(Date.now()+60000).toISOString();
    data.sessions[1].summary="latest activity for s2";
    await page.getByText("latest activity for s2",{exact:true}).waitFor();
    assert.deepEqual(await page.locator(".observer-column").evaluateAll(es=>es.map(e=>e.dataset.session)),beforeOrder,"automatic refresh preserves PC column order");
    await page.click("#observer-sort");
    assert.equal(await page.locator(".observer-column").first().getAttribute("data-session"),"s2");
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.goto(`http://127.0.0.1:${server.address().port}/static/observer.html`);
    await page.waitForURL("**/#/observer");
    await page.waitForSelector(".observer-card");
    await page.route("**/api/observer", async route => {
      await new Promise(resolve=>setTimeout(resolve,200));
      await route.continue();
    });
    await page.reload();
    await page.waitForFunction(() => document.body.dataset.page === "observer");
    await page.evaluate(() => {location.hash="#/observer/session/s1";});
    await page.waitForFunction(() => document.getElementById("observer-target").value === "s1");
    assert.equal(await page.locator(".observer-card").count(), 1);
    await page.unroute("**/api/observer");
    data.sessions[0].status="idle";
    data.sessions[0].last_activity_at=new Date(Date.now()-3600000).toISOString();
    data.sessions[0].events.push({id:"question",origin:"agent",kind:"action",source:"agent:s1",question:true,
      text:"Review screenshot and choose",choices:["Accept","Revise"],attachments:[{id:"image"}],needs_action:true,at:new Date().toISOString()});
    data.sessions[1].state="done";
    data.sessions.push({name:"s3",running:true,status:"busy",meshes:[],events:[],state:"done"});
    gates=[{run:"r1",scope:"s1",cwd:"/repo",status:"waiting_approval",step_id:"review",gate:"Approve deployment",sessions:["s1"]}];
    await page.evaluate(()=>{location.hash="#/observer";});
    await page.reload();
    await page.getByRole("button",{name:"Approve gate",exact:true}).waitFor();
    assert.equal(await page.locator(".state.waiting").innerText(),"장기 대기");
    assert.equal(await page.locator(".state.done").innerText(),"완료 후 유휴");
    assert.equal(await page.locator(".state.working").innerText(),"동작 중");
    await page.selectOption("#observer-activity","working");
    assert.deepEqual(await page.locator("#observer-target option").evaluateAll(es=>es.map(e=>e.value)),["","s3"]);
    assert.equal(await page.inputValue("#observer-target"),"");
    assert.equal(await page.locator(".observer-card").count(),1);
    assert.match(await page.locator(".observer-card").innerText(),/s3/);
    await page.selectOption("#observer-activity","all");
    await page.locator(".observer-image img").waitFor();
    assert.equal(await page.locator(".observer-image img").evaluate(img=>img.complete&&img.naturalWidth>0),true);
    await page.locator(".observer-answer").getByRole("button",{name:"Revise",exact:true}).click();
    await page.locator(".observer-answer textarea").fill("Revise the mobile spacing");
    data.sessions[1].summary="new summary while user is typing";
    await page.waitForTimeout(250);
    assert.equal(await page.locator(".observer-answer textarea").inputValue(),"Revise the mobile spacing");
    await page.locator(".observer-answer").getByRole("button",{name:"답변 전송",exact:true}).click();
    await page.getByText("사용자 답변: Revise the mobile spacing",{exact:true}).waitFor();
    assert(sent.some(r=>r.url.endsWith("/reports/question/answer")&&r.body.text==="Revise the mobile spacing"));
    page.on("dialog",dialog=>dialog.accept());
    await page.getByRole("button",{name:"Approve gate",exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector(".observer-gate"));
    assert(sent.some(r=>r.url==="/api/cflow/approve"&&r.body.scope==="s1"&&r.body.cwd==="/repo"));
    gates=[{run:"r1",scope:"s1",cwd:"/repo",status:"waiting_selection",step_id:"environment",chooser:"user",prompt:"Choose target",options:[{name:"staging",description:"Test environment"},{name:"production",description:"Live environment"}],sessions:["s1"]}];
    await page.getByRole("button",{name:"staging",exact:true}).waitFor();
    await page.getByRole("button",{name:"staging",exact:true}).click();
    await page.waitForTimeout(150);
    assert(sent.some(r=>r.url==="/api/cflow/select"&&r.body.option==="staging"&&r.body.scope==="s1"));
    if(process.env.CLAUNCH_BOARD_SCREENSHOT) await page.screenshot({path:process.env.CLAUNCH_BOARD_SCREENSHOT,fullPage:true});
    await page.setViewportSize({width:390,height:844});
    data.sessions[1].events=[{id:"mobile-new",kind:"result",text:"newest timeline event",source:"transcript:2",at:new Date(Date.now()+120000).toISOString()}];
    await page.getByText("newest timeline event",{exact:true}).waitFor();
    assert.equal(await page.locator(".observer-post .event").first().getAttribute("data-event"),"mobile-new");
    assert.equal(await page.locator("#observer-sort").isVisible(),false);
    await page.locator(".observer-content").evaluate(e=>e.scrollTop=0);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    if(process.env.CLAUNCH_SCREENSHOT) await page.screenshot({path:process.env.CLAUNCH_SCREENSHOT,fullPage:true});
    assert.deepEqual(errors, []);
    console.log("PASS: mobile layout, composer fold, usage meter over calls and days, elapsed-since-update, scope/action filters, evidence, Escape, target input, per-session drafts, shared auth, deep links, polling lifecycle, legacy redirect, direct screenshot/answer, activity filters, cflow approval/selection");
  } finally { await browser.close(); server.close(); server.closeAllConnections(); }
})().catch(error => { console.error(error); server.close(); server.closeAllConnections(); process.exitCode = 1; });
