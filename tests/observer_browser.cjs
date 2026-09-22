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
// Each window's cache pair adds up to its prompt_tokens, the identity the
// provider's own counters hold. The cards split those the same way the
// per-session block below does, so a window that reported no cache at all
// would drop the column instead of printing a zero.
const data = { enabled: true, scope: "all", usage_summary: {since:"2026-09-18T00:00:00Z", windows:{
  hour:{total_tokens:1234,prompt_tokens:1200,completion_tokens:34,
        prompt_cache_hit_tokens:900,prompt_cache_miss_tokens:300},
  day:{total_tokens:56789,prompt_tokens:56000,completion_tokens:789,
       prompt_cache_hit_tokens:42000,prompt_cache_miss_tokens:14000},
  week:{total_tokens:1234567,prompt_tokens:1234000,completion_tokens:567,
        prompt_cache_hit_tokens:925500,prompt_cache_miss_tokens:308500},
}}, sessions: [
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
  // A row written before the meter exists carries only the last call, and this
  // one's provider never reported a cache at all: the panel shows it as a
  // last-call figure and folds the cache into the input rather than printing
  // the raw response.
  { name: "s2", status: "idle", running: true, meshes: ["team-b"], summary: "병합 완료", events: [],
    // Pinned, so the two boxes have something to disagree about: s1 is out of
    // the scope and s2 is in it, and a box drawn from the wrong session would
    // read the same on both.
    observe_pin: true,
    usage: { prompt_tokens: 900, completion_tokens: 60 } },
] };
const server = http.createServer((req, res) => {
  let body = "";
  req.on("data", chunk => body += chunk);
  req.on("end", () => {
    if (req.url.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");
      // This fixture serves individual reads. Use the client's supported
      // fallback instead of claiming a batch succeeded without any answers.
      if(req.url === "/api/batch") {res.statusCode=404;return res.end('{}');}
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
      if(req.url==="/api/observer/settings") {
        const patch=JSON.parse(body);
        data.enabled=patch.enabled;
        if(patch.scope!==undefined) data.scope=patch.scope;
        sent.push({url:req.url, body:patch});
        return res.end(JSON.stringify({enabled:data.enabled, scope:data.scope||"all"}));
      }
      // One-shot refresh: the pass ran and added two events, which is what the
      // card reports back. A pass with nothing new answers called:false.
      if(req.url.endsWith("/refresh")) { sent.push({url:req.url,body:JSON.parse(body||"{}")}); return res.end('{"called":true,"events":2}'); }
      if (req.url.endsWith("/events/e1")) return res.end('{"content":"Which environment?"}');
      if(req.url.includes("/transcript?")) return res.end(JSON.stringify({records:[{seq:1,role:"user",blocks:[{type:"text",text:"Transcript retained"}]}],has_more:false,cursor:1}));
      // The observe pin is one session field read by two polls: the rail's
      // session list and the observer's own snapshot. The fixture keeps the
      // one value both read, so a write through either box is visible to the
      // other on its next poll — which is what the page claims and what a
      // fixture serving two independent copies would silently not test.
      if(req.url.endsWith("/pin")) {
        const name = decodeURIComponent(req.url.split("/")[3]);
        const on = JSON.parse(body||"{}").pinned === true;
        const row = data.sessions.find(s => s.name === name);
        if(!row) { res.statusCode=404; return res.end('{"error":"세션을 찾을 수 없습니다."}'); }
        row.observe_pin = on;
        sent.push({url:req.url, body:JSON.parse(body||"{}")});
        return res.end(JSON.stringify({name, pinned:on}));
      }
      if(req.method === "GET") {
        if(req.url === "/api/daemon") return res.end('{"version":"test","boot_id":"test"}');
        if(req.url === "/api/sessions" || req.url.startsWith("/api/sessions?")) {
          return res.end(JSON.stringify(data.sessions.map(s => ({
            name:s.name, status:s.status, observe_pin:!!s.observe_pin,
          }))));
        }
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
    const tokenToggle=page.locator("#observer-token-toggle");
    assert.equal(await tokenToggle.getAttribute("aria-expanded"),"false");
    assert.equal(await page.locator("#observer-token-detail").isVisible(),false);
    assert.match(await tokenToggle.innerText(),/1h 1.2K · 24h 56.8K · 7d 1.2M/);
    assert(await tokenToggle.evaluate(e=>e.offsetHeight<=40 && e.scrollWidth<=e.clientWidth));
    await tokenToggle.click();
    await page.setViewportSize({width:320,height:844});
    assert(await tokenToggle.evaluate(e=>e.offsetHeight<=40 && e.scrollWidth<=e.clientWidth));
    await page.setViewportSize({width:390,height:844});
    assert.equal(await tokenToggle.getAttribute("aria-expanded"),"true");
    assert.deepEqual(await page.locator(".observer-token-window strong").allTextContents(),["1,234","56,789","1,234,567"]);
    // 입력 is the uncached prefill and 캐시 the part served from cache, which is
    // the same reading the per-session block below gives the same word.
    assert.deepEqual(await page.locator(".observer-token-window small").allTextContents(),
      ["입력 300 · 캐시 900 · 출력 34",
       "입력 14,000 · 캐시 42,000 · 출력 789",
       "입력 308,500 · 캐시 925,500 · 출력 567"]);
    assert.match(await page.locator("#observer-token-note").innerText(),/시작 이전 사용량 제외/);
    await page.waitForTimeout(250);
    assert.equal(await tokenToggle.getAttribute("aria-expanded"),"true","refresh preserves expansion");
    await tokenToggle.click();
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
    // Three figures rather than four overlapping ones: the cached side is
    // reported separately, so the input is the prompt that missed the cache.
    assert.match(meterText, /누적\s+호출 3회 · 입력 1,800 · 캐시 1,200 · 출력 210/);
    assert.match(meterText, /2026-09-17\s+호출 2회 · 입력 1,200 · 캐시 800 · 출력 140/);
    assert.match(meterText, /2026-09-16\s+호출 1회 · 입력 600 · 캐시 400 · 출력 70/);
    await page.locator("#observer-usage summary").click();
    const boardUsage = await page.locator("#observer-usage-body").innerText();
    // The fleet figure keeps the lifetime total apart from the sessions the
    // meter never reached, so a last call is never added in as if cumulative.
    assert.match(boardUsage, /누적\s+호출 3회 · 입력 1,800 · 캐시 1,200 · 출력 210/);
    assert.match(boardUsage, /2026-09-17\s+호출 2회/);
    const lastCallLine = boardUsage.split("\n").find(line => line.startsWith("마지막 관찰"));
    assert.match(lastCallLine, /입력 900 · 출력 60/);
    assert.doesNotMatch(lastCallLine, /캐시/, "a provider that reported no cache gets no cache column");
    await page.locator("#observer-usage summary").click();
    await page.click("#observer-mobile-monitor");
    await page.waitForFunction(()=>document.getElementById("observer-mobile-monitor").textContent==="관찰 시작");
    assert(sent.some(r=>r.url==="/api/observer/settings" && r.body.enabled===false));
    await page.click("#observer-mobile-monitor");
    await page.waitForFunction(()=>document.getElementById("observer-mobile-monitor").textContent==="관찰 끄기");
    await page.setViewportSize({width:1280,height:900});
    assert.equal(await tokenToggle.isVisible(),false);
    assert.equal(await page.locator("#observer-token-detail").isVisible(),true);
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
    assert.match(await tokenToggle.innerText(),/1h 1.2K · 24h 56.8K · 7d 1.2M/,"scope filters do not change global totals");
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
    // The card's two per-session actions share its action row with the three
    // links: as bars under the card they took the column's full width and the
    // 44px touch target, which made them the largest thing on the card.
    const actions = page.locator('.observer-post[data-session="s1"] .observer-links button');
    assert.deepEqual(await actions.allTextContents(), ["이 세션에 입력","지금 갱신"]);
    const action = await actions.first().evaluate(e=>{
      const card=e.closest(".observer-card");
      return {width:e.offsetWidth,height:e.offsetHeight,card:card.offsetWidth};
    });
    assert(action.width < action.card/2, `the action is not a full-width bar (${action.width} of ${action.card}px)`);
    assert(action.height <= 32, `the action sits in the row (${action.height}px)`);
    // One press, one pass, and the card says what the pass did — including
    // when it spent no API call because there was nothing new to read.
    await actions.nth(1).click();
    const note = page.locator(".observer-post[data-session=s1] .observer-action-note");
    await page.waitForFunction(()=>document.querySelector(".observer-post[data-session=s1] .observer-action-note")?.textContent==="갱신됨 · 새 항목 2개");
    assert.equal(sent.filter(r=>r.url.endsWith("/refresh")).length, 1);
    assert.equal(sent.at(-1).url, "/api/observer/s1/refresh");
    assert.equal(await note.innerText(), "갱신됨 · 새 항목 2개");
    await page.uncheck("#observer-actions-only");
    // The observe pin on the card, drawn from the session's own flag: the two
    // cards disagree (s1 out of the scope, s2 in it), so a box reading the
    // wrong row or one shared value would show the same state twice.
    const cardBox = s => page.locator(`.observer-card[data-session="${s}"] .observer-pin input`);
    assert.equal(await cardBox("s1").isChecked(), false);
    assert.equal(await cardBox("s2").isChecked(), true);
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
    // The fold is the reader's, not the phone's: desktop keeps the composer
    // closed until it is asked for, and asking opens it with the caret in it.
    assert.equal(await isFolded(), true);
    assert.equal(await page.locator("#observer-prompt").isVisible(), false);
    assert.equal(await page.locator("#observer-composer-toggle").isVisible(), true);
    await page.click("#observer-composer-toggle");
    assert.equal(await isFolded(), false);
    assert.equal(await page.locator("#observer-prompt").isVisible(), true);
    assert.equal(await page.locator("#observer-prompt").evaluate(e=>document.activeElement===e), true);
    await page.click("#observer-composer-toggle");
    // The band above the board is chrome, so the board has to be what the
    // window is mostly made of: the composer's 104px and the trimmed hint and
    // control rows are what the columns got instead (316px before this round).
    assert(await page.locator("#observer-composer").evaluate(e=>e.classList.contains("folded")));
    const desktop=await page.evaluate(()=>{
      const content=document.querySelector("#observer-view .observer-content");
      const cards=document.querySelector("#observer-cards");
      const band=[...content.children].filter(e=>e!==cards)
        .reduce((n,e)=>n+e.getBoundingClientRect().height,0);
      const usage=document.querySelector("#observer-token-summary").getBoundingClientRect().height;
      return {cards:cards.getBoundingClientRect().height,band,usage};
    });
    // The requested top-level monitor shares the old board's vertical budget.
    assert(desktop.cards + desktop.usage >= 468, `board and token monitor should retain their vertical budget (${Math.round(desktop.cards)}px)`);
    assert(desktop.usage <= 180, `the token monitor should stay compact (${Math.round(desktop.usage)}px)`);
    assert(desktop.band <= 210, `the band above the board should stay trim (${Math.round(desktop.band)}px)`);
    // The scope switch sits beside the monitor button it modifies — one
    // decision, whether the observer runs and over what — and is drawn from
    // the snapshot rather than from the click.
    const scopeBox = page.locator("#observer-scope-pinned");
    assert.equal(await scopeBox.isVisible(), true);
    assert.equal(await scopeBox.isChecked(), false);
    await scopeBox.check();
    await page.waitForFunction(()=>document.getElementById("observer-scope-pinned").checked);
    assert.deepEqual(sent.filter(r=>r.url==="/api/observer/settings").at(-1).body, {enabled:true,scope:"pinned"});
    // The monitor button posts `enabled` alone; a route that reset the scope
    // on every switch would silently widen what the operator is paying for.
    await page.click("#observer-monitor");
    await page.waitForFunction(()=>document.getElementById("observer-monitor").textContent==="관찰 시작");
    assert.equal(await scopeBox.isChecked(), true, "the monitor switch leaves the mode alone");
    await page.click("#observer-monitor");
    await page.waitForFunction(()=>document.getElementById("observer-monitor").textContent==="관찰 끄기");
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
    data.sessions.push({name:"s3",running:true,status:"busy",meshes:[],events:[],state:"done",
      usage:{prompt_tokens:500,completion_tokens:40,prompt_cache_hit_tokens:120,prompt_cache_miss_tokens:380}});
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
    // The grid limits each session's own posts, after filters, and persists its
    // size: the filtered session list is what the reader asked for, so the cut
    // falls inside each session rather than dropping whole sessions off the end.
    // The board/timeline remains available with all original controls.
    assert.equal(await page.locator('.observer-view-controls').isVisible(),false);
    await page.setViewportSize({width:1280,height:900});
    const gridSessions = () => page.locator('.observer-grid .observer-card')
      .evaluateAll(es=>[...new Set(es.map(e=>e.dataset.session))].sort());
    await page.locator('label:has(#observer-layout-grid)').click();
    assert.equal(await page.locator('#observer-limit-control').isVisible(),true);
    assert.equal(await page.locator('#observer-limit').inputValue(),'5');
    await page.locator('#observer-limit').fill('1');
    assert.deepEqual(await gridSessions(),['s1','s2','s3']);
    assert.equal(await page.locator('.observer-grid .observer-card').count(),3,
      'one post each, and no session dropped by the limit');
    assert.equal(await page.locator('.observer-grid .event').getAttribute('data-event'),'mobile-new');
    if(process.env.CLAUNCH_GRID_SCREENSHOT) await page.screenshot({path:process.env.CLAUNCH_GRID_SCREENSHOT,fullPage:true});
    await page.reload();
    await page.waitForSelector('.observer-grid .observer-card');
    assert.equal(await page.locator('#observer-layout-grid').isChecked(),true);
    assert.equal(await page.locator('#observer-limit').inputValue(),'1');
    assert.equal(await page.locator('.observer-card').count(),3);
    await page.setViewportSize({width:1280,height:900});
    assert.equal(await page.locator('.observer-grid').isVisible(),true);
    await page.check('#observer-actions-only');
    await page.getByRole('button',{name:'staging',exact:true}).waitFor();
    assert.equal(await page.locator('.observer-card').count(),1,'gate remains reachable after filtering');
    await page.uncheck('#observer-actions-only');
    data.sessions[1].events=Array.from({length:12},(_,i)=>({id:`grid-${i}`,kind:'result',text:`Grid post ${i}`,source:'transcript:2',at:new Date(Date.now()+(i+1)*60000).toISOString()}));
    await page.locator('#observer-limit').fill('10');
    await page.getByText('Grid post 11',{exact:true}).waitFor();
    // Twelve posts on s2 and the limit is ten: the two oldest are cut and the
    // other sessions keep theirs.
    assert.deepEqual(await page.locator('.observer-grid .observer-card[data-session="s2"] .event').evaluateAll(es=>es.map(e=>e.dataset.event)),Array.from({length:10},(_,i)=>`grid-${11-i}`));
    assert.deepEqual(await gridSessions(),['s1','s2','s3']);
    // A session the meter never reached reports its last call, and the fleet
    // line adds those on their own row rather than into the lifetime total.
    const lastCallBlock = page.locator('.observer-grid .observer-card[data-session="s3"] details', { hasText: "관찰 API 사용량 (마지막 관찰)" });
    assert.equal(await lastCallBlock.count(),1);
    await lastCallBlock.locator("summary").click();
    assert.match(await lastCallBlock.locator("pre").innerText(), /마지막 관찰\s+입력 380 · 캐시 120 · 출력 40/);
    const noCacheBlock = page.locator('.observer-grid .observer-card[data-session="s2"] details', { hasText: "관찰 API 사용량 (마지막 관찰)" });
    await noCacheBlock.locator("summary").click();
    const noCacheText = await noCacheBlock.locator("pre").innerText();
    assert.match(noCacheText, /마지막 관찰\s+입력 900 · 출력 60/);
    assert.doesNotMatch(noCacheText, /캐시/);
    await page.locator('#observer-usage summary').click();
    assert.match(await page.locator('#observer-usage-body').innerText(), /마지막 관찰\s+입력 1,280 · 캐시 120 · 출력 100/);
    await page.locator('#observer-usage summary').click();
    await page.locator('.observer-grid .observer-card').last().scrollIntoViewIfNeeded();
    assert.equal(await page.locator('.observer-grid .observer-card').last().isVisible(),true);
    await page.selectOption('#observer-activity','working');
    assert.equal(await page.locator('.observer-card').count(),1);
    assert.equal(await page.locator('.observer-card').getAttribute('data-session'),'s3');
    await page.selectOption('#observer-activity','all');
    await page.locator('label:has(#observer-layout-board)').click();
    assert.equal(await page.locator('.observer-column').count(),3);
    assert.equal(await page.locator('#observer-limit-control').isVisible(),false);
    await page.setViewportSize({width:320,height:740});
    await page.waitForSelector('.observer-timeline .observer-card');
    assert.equal(await page.locator('.observer-timeline .observer-card').count(),16);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    // Desktop preferences survive mobile's mandatory full timeline, including reloads.
    await page.setViewportSize({width:1280,height:900});
    await page.locator('label:has(#observer-layout-timeline)').click();
    assert.equal(await page.locator('.observer-timeline .observer-card').count(),16);
    assert.equal(await page.locator('#observer-sort').isVisible(),false);
    assert.equal(await page.locator('#observer-limit-control').isVisible(),false);
    await page.locator('.observer-timeline .observer-card').last().scrollIntoViewIfNeeded();
    assert.equal(await page.locator('.observer-timeline').evaluate(e=>e.scrollTop>0),true);
    await page.reload();
    await page.waitForSelector('.observer-timeline .observer-card');
    assert.equal(await page.locator('#observer-layout-timeline').isChecked(),true);
    await page.locator('label:has(#observer-layout-grid)').click();
    await page.setViewportSize({width:390,height:844});
    await page.waitForSelector('.observer-timeline .observer-card');
    assert.equal(await page.locator('.observer-timeline .observer-card').count(),16);
    assert.equal(await page.locator('.observer-view-controls').isVisible(),false);
    await page.reload();
    await page.waitForSelector('.observer-timeline .observer-card');
    assert.equal(await page.locator('.observer-timeline .observer-card').count(),16);
    await page.setViewportSize({width:1280,height:900});
    await page.waitForSelector('.observer-grid .observer-card');
    assert.equal(await page.locator('#observer-layout-grid').isChecked(),true);
    assert.equal(await page.locator('#observer-limit').inputValue(),'10');
    // Corrupt and unavailable storage must not prevent rendering.
    await page.evaluate(()=>localStorage.setItem('claunch-observer-grid-limit','999'));
    await page.reload();
    await page.waitForSelector('.observer-card');
    await page.locator('label:has(#observer-layout-grid)').click();
    assert.equal(await page.locator('#observer-limit').inputValue(),'5');
    await page.addInitScript(()=>{
      const get=Storage.prototype.getItem,set=Storage.prototype.setItem;
      Storage.prototype.getItem=function(key){if(key.startsWith('claunch-observer-'))throw Error('storage blocked');return get.call(this,key);};
      Storage.prototype.setItem=function(key,value){if(key.startsWith('claunch-observer-'))throw Error('storage blocked');return set.call(this,key,value);};
    });
    await page.reload();
    await page.waitForSelector('.observer-card');
    await page.locator('label:has(#observer-layout-grid)').click();
    await page.locator('#observer-limit').fill('1');
    assert.deepEqual(await page.locator('.observer-card').evaluateAll(es=>[...new Set(es.map(e=>e.dataset.session))].sort()),['s1','s2','s3']);
    const previousEvents=data.sessions[0].events;
    // Mechanical events join both timeline and grid by timestamp. They show
    // their source and changes without querying transcript evidence.
    data.sessions[0].events = [
      {id:"m-old",origin:"daemon",kind:"borrow",text:"세션 인증 프로파일 변경",at:"2030-01-01T00:00:00Z",details:{previous:"p1",current:"p2"}},
      {id:"m-new",origin:"daemon",kind:"worktree",text:"세션 작업 디렉터리 이동",at:"2030-01-01T02:00:00Z",details:{previous:"C:/old",current:"C:/new/<script>"}},
      {id:"m-middle",kind:"test",text:"모델 관찰 결과",source:"transcript:2",at:"2030-01-01T10:00:00+09:00"}
    ];
    await page.selectOption('#observer-scope','session');
    await page.selectOption('#observer-selection','s1');
    await page.locator('label:has(#observer-layout-timeline)').click();
    await page.waitForSelector('[data-event="m-new"]');
    assert.deepEqual(await page.locator('#observer-view .event').evaluateAll(es=>es.map(e=>e.dataset.event)),['m-new','m-middle','m-old']);
    assert.match(await page.locator('[data-event="m-new"]').innerText(),/세션 이벤트 · worktree/);
    assert.match(await page.locator('[data-event="m-old"]').innerText(),/p1 → p2/);
    assert.match(await page.locator('[data-event="m-new"]').innerText(),/C:\/old → C:\/new\/<script>/);
    assert.equal(await page.locator('[data-event="m-new"] script').count(),0);
    assert.equal(await page.locator('[data-event="m-new"] button').count(),0);
    await page.locator('label:has(#observer-layout-grid)').click();
    await page.locator('#observer-limit').fill('10');
    assert.deepEqual(await page.locator('#observer-view .event').evaluateAll(es=>es.map(e=>e.dataset.event)),['m-new','m-middle','m-old']);
    data.sessions[0].events=previousEvents;
    await page.selectOption("#observer-scope","global");
    {
    // Transcript session details reuse observer data, independent of its filters.
    await page.evaluate(()=>location.hash="#/log/s1");
    await page.getByText("Transcript retained",{exact:true}).waitFor();
    await page.click("#log-info-tab");
    await page.waitForSelector('#log-info-pane .observer-card[data-session="s1"]');
    assert.equal(await page.locator('#log-info-pane .observer-card').count(),1);
    assert.match(await page.locator('#log-info-pane').innerText(),/테스트 12개 통과/);
    assert.equal(await page.locator('#term-log-pane').isVisible(),false);
    assert.equal(await page.locator('#log-info-tab').getAttribute('aria-selected'),'true');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.locator('#log-info-tab').press('ArrowLeft');
    assert.equal(await page.getByText("Transcript retained",{exact:true}).isVisible(),true);
    await page.waitForTimeout(200);
    const transcriptStoppedReads=observerReads;
    await page.waitForTimeout(300);
    assert.equal(observerReads,transcriptStoppedReads,'info poll stops on transcript tab');
    await page.click('#log-info-tab');
    data.sessions[0].summary='Updated session summary';
    await page.getByText('Updated session summary',{exact:true}).waitFor();
    await page.evaluate(()=>location.hash="#/log/s2");
    await page.waitForFunction(()=>document.getElementById('log-title').textContent==='s2');
    assert.equal(await page.locator('#term-log-pane').isVisible(),true);
    await page.click('#log-info-tab');
    await page.waitForSelector('#log-info-pane .observer-card[data-session="s2"]');
    assert.equal(await page.locator('#log-info-pane .observer-card[data-session="s1"]').count(),0);
    await page.evaluate(()=>location.hash="#/log/missing");
    await page.waitForFunction(()=>document.getElementById('log-title').textContent==='missing');
    await page.click('#log-info-tab');
    await page.getByText('아직 이 세션의 관찰 정보가 없습니다.',{exact:true}).waitFor();
    await page.evaluate(()=>location.hash="#/observer");
    await page.waitForSelector('#observer-view:not(.hidden)');
    // The rail's observe-pin box, beside the 📌 pin. It is checked last and on
    // its own load because a rail tab costs vertical space the board's own
    // budget is measured against — a session pinned from the start would move
    // that measurement rather than test this control.
    await page.evaluate(()=>localStorage.setItem("claunch_session_pins:/", JSON.stringify(["s1"])));
    await page.reload();
    await page.waitForSelector(".observer-card");
    const railBox = page.locator(".session-tab-observe");
    await railBox.waitFor();
    assert.equal(await railBox.isChecked(), false, "the rail box reads s1's own flag");
    assert.equal(await page.locator(".session-tab").count(), 1, "one tab, so one box");
    // Writing through the rail reaches the card on the observer's own poll:
    // both read the session's one field rather than keeping a copy, and the
    // rail's poll is the slower of the two, so this is the direction whose
    // convergence the two cadences could actually disagree on.
    await railBox.check();
    await page.waitForFunction(()=>document.querySelector('.observer-card[data-session="s1"] .observer-pin input')?.checked);
    assert.equal(sent.filter(r=>r.url.endsWith("/pin")).at(-1).url, "/api/observer/s1/pin");
    assert.deepEqual(sent.filter(r=>r.url.endsWith("/pin")).at(-1).body, {pinned:true});
    await railBox.uncheck();
    await page.waitForFunction(()=>!document.querySelector('.observer-card[data-session="s1"] .observer-pin input')?.checked);
    }
    assert.deepEqual(errors, []);
    console.log("PASS: mobile layout, composer fold, usage as input/cache/output over calls and days, fleet total split from last-call rows, elapsed-since-update, scope/action filters, evidence, Escape, target input, per-session drafts, shared auth, deep links, polling lifecycle, legacy redirect, direct screenshot/answer, activity filters, cflow approval/selection, per-session latest-N grid, 1/10 limits, filter-before-limit, storage persistence/fallback, responsive view switching");
  } finally { await browser.close(); server.close(); server.closeAllConnections(); }
})().catch(error => { console.error(error); server.close(); server.closeAllConnections(); process.exitCode = 1; });
