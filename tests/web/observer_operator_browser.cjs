/* Browser integration for Operator v2 on the Observer page (claunch-iw5i8.4):
   tabs, creation with a project multi-check or all projects, scope and watch
   edits, the conversation (send from the page composer, answers to asks,
   older entries opened in place), 「operator에게 묻기」 on a session card, the
   composer's height cap, an empty target the reader picked staying empty,
   and the page staying the one scroller.
   CLAUNCH_PLAYWRIGHT may name an installed playwright-core.
   Run: node tests/web/observer_operator_browser.cjs. No live daemon. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require(process.env.CLAUNCH_PLAYWRIGHT || "playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");
const sent = [];
const at = n => new Date(Date.UTC(2026, 9, 17, 9, 0, n)).toISOString();
const observer = { enabled: false, scope: "all", sessions: [
  { name: "s1", status: "busy", running: true, meshes: ["team-a"], summary: "작업 중", events: [] },
  { name: "s2", status: "idle", running: true, meshes: ["team-b"], summary: "대기", events: [] },
] };
// The app's session list is where a session's project comes from.
const projectOf = { s1: "alpha", s2: "beta" };
/* 25 entries, so the conversation draws its newest 20 and folds 5: every
   kind the feed has, and two open asks the tab counts as unseen. */
const feed = [];
for (let i = 0; i < 19; i++) feed.push({ id: `p${i}`, at: at(i), kind: "post", role: "bot", text: `관찰 보고 ${i}`, level: "info", refs: [] });
feed.push({ id: "u1", at: at(19), kind: "user", role: "user", text: "s1은 무엇을 하고 있습니까?" });
feed.push({ id: "p19", at: at(20), kind: "post", role: "bot", text: "s1은 테스트를 돌리고 있습니다.", level: "attention", refs: ["s1"], parent: "p18" });
feed.push({ id: "d1", at: at(21), kind: "dispatch", role: "bot", target: "s1", text: "테스트 결과를 보고하십시오", on_behalf_of: "u1", delivery: "sent" });
feed.push({ id: "y1", at: at(22), kind: "system", role: "system", text: "설정 변경 — 감시: 켜짐" });
feed.push({ id: "a1", at: at(23), kind: "ask", role: "bot", type: "approve", text: "s1에 재시작을 지시할까요?", choices: [], level: "urgent", refs: ["s1"], answer: null });
feed.push({ id: "a2", at: at(24), kind: "ask", role: "bot", type: "choice", text: "어느 브랜치를 볼까요?", choices: ["main", "dev"], level: "attention", refs: [], answer: null });
const ops = {
  op1: { id: "op1", name: "ops-a", all: false, projects: ["alpha"], profile: null, model: null, effort: null,
         watch: true, created_at: at(0), session: null, feed, seen: [] },
};
let serial = 1;
function summary(o) {
  const open = o.feed.filter(e => e.kind === "ask" && !e.answer).map(e => e.id);
  return { id: o.id, name: o.name, all: o.all, projects: o.projects, profile: o.profile, model: o.model,
    effort: o.effort, watch: o.watch, created_at: o.created_at,
    operator: o.session ? { name: o.session, running: true, status: "idle" } : null,
    pending: open.length, unseen: open.filter(id => !o.seen.includes(id)).length };
}
function operatorsApi(req, res, body) {
  const url = req.url.split("?")[0], parts = url.split("/").slice(3).map(decodeURIComponent);
  const json = (value, status = 200) => { res.statusCode = status; return res.end(JSON.stringify(value)); };
  const data = body ? JSON.parse(body) : {};
  if (req.method !== "GET") sent.push({ method: req.method, url, body: data });
  if (!parts.length) {
    if (req.method === "GET") return json({ operators: Object.values(ops).map(summary).sort((a, b) => a.name.localeCompare(b.name)) });
    // The daemon raises a name clash as a 409 with a plain-text body.
    if (Object.values(ops).some(o => o.name === data.name)) { res.statusCode = 409; return res.end(`an operator named '${data.name}' already exists`); }
    if (!data.all && !(data.projects || []).length) return json({ error: "projects required" }, 400);
    const id = `op${++serial}`;
    ops[id] = { id, name: data.name, all: !!data.all, projects: data.all ? [] : data.projects, profile: data.profile || null,
      model: null, effort: null, watch: data.watch !== false, created_at: at(30), session: null, feed: [], seen: [] };
    return json(summary(ops[id]), 201);
  }
  const o = ops[parts[0]];
  if (!o) return json({ error: "no such operator" }, 404);
  const rest = parts.slice(1).join("/");
  if (!rest && req.method === "GET") return json({ ...summary(o), feed: o.feed.slice(-200), sessions: [], gates: [] });
  if (!rest && req.method === "PATCH") {
    for (const key of ["name", "all", "projects", "profile", "watch"]) if (key in data) o[key] = data[key];
    if (o.all) o.projects = [];
    return json(summary(o));
  }
  if (!rest && req.method === "DELETE") { delete ops[o.id]; return json({ deleted: o.id }); }
  if (rest === "seen") { o.seen = o.feed.filter(e => e.kind === "ask" && !e.answer).map(e => e.id); return json(summary(o)); }
  if (rest === "message") {
    const entry = { id: `m${o.feed.length}`, at: new Date().toISOString(), kind: "user", role: "user", text: data.text };
    o.feed.push(entry);
    return json({ ...entry, nudge: o.session ? "sent" : "no-operator" });
  }
  const answer = rest.match(/^asks\/([^/]+)\/answer$/);
  if (answer) {
    const e = o.feed.find(x => x.id === answer[1]);
    e.answer = { decision: data.decision ?? null, text: data.text || null, at: new Date().toISOString() };
    return json({ ...e, nudge: "no-operator" });
  }
  if (rest === "start") { o.session = "s900"; return json({ name: "s900" }, 201); }
  if (rest === "stop") { o.session = null; return json({ id: o.id, stopped: true }); }
  return json({ error: "unhandled" }, 400);
}
const server = http.createServer((req, res) => {
  let body = "";
  req.on("data", chunk => body += chunk);
  req.on("end", () => {
    if (req.url.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");
      if (req.url === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
      if (req.url.split("?")[0] === "/api/observer") return res.end(JSON.stringify(observer));
      if (req.url.startsWith("/api/operators")) return operatorsApi(req, res, body);
      if (req.url === "/api/projects") return res.end(JSON.stringify({ projects: [{ name: "alpha" }, { name: "beta" }] }));
      if (req.url === "/api/profiles") return res.end(JSON.stringify({ profile_options: [{ value: "p1:claude", label: "p1 · claude" }] }));
      if (req.url === "/api/sessions" || req.url.startsWith("/api/sessions?")) {
        return res.end(JSON.stringify({ sessions: observer.sessions.map(s => ({
          name: s.name, status: s.status, running: s.running, meshes: s.meshes, project: projectOf[s.name] })) }));
      }
      if (req.method === "GET") {
        if (req.url === "/api/daemon") return res.end('{"version":"test","boot_id":"test"}');
        if (/^\/api\/(profiles|sessions|workspaces|mesh|cflow|harnesses|roles)/.test(req.url)) return res.end("[]");
        return res.end("{}");
      }
      sent.push({ method: req.method, url: req.url, body: JSON.parse(body || "{}") });
      return res.end('{"ok":true}');
    }
    const file = path.join(root, (req.url === "/" ? "index.html" : req.url.replace("/static/", "")));
    try {
      res.setHeader("Content-Type", file.endsWith(".js") ? "application/javascript" : file.endsWith(".css") ? "text/css" : "text/html");
      res.end(fs.readFileSync(file));
    } catch { res.statusCode = 404; res.end(); }
  });
});
/* Elements under #observer-view that scroll inside themselves, and any
   sideways overflow (the same measure observer_browser.cjs uses). */
async function innerScrollers(page) {
  return page.evaluate(() => {
    const view = document.getElementById("observer-view"), out = [];
    for (const e of view.querySelectorAll("*")) {
      const cs = getComputedStyle(e);
      const y = /auto|scroll/.test(cs.overflowY) && e.scrollHeight > e.clientHeight + 1;
      const x = /auto|scroll/.test(cs.overflowX) && e.scrollWidth > e.clientWidth + 1;
      if (y || x) out.push(`${e.tagName.toLowerCase()}${e.id ? "#" + e.id : "." + e.classList[0]}`);
    }
    if (document.documentElement.scrollWidth > innerWidth) out.push("document: sideways overflow");
    if (view.scrollWidth > view.clientWidth + 1) out.push("#observer-view: sideways overflow");
    return out;
  });
}
const lastSent = (method, suffix) => sent.filter(r => r.method === method && r.url.endsWith(suffix)).at(-1);

(async () => {
  await safeListen(server);
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CLAUNCH_CHROMIUM || undefined });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    await page.addInitScript(() => {
      localStorage.setItem("claunch_token:/", "fixture-token");
      const interval = window.setInterval;
      window.setInterval = (fn, ms, ...args) => interval(fn, ms === 10000 ? 200 : ms, ...args);
    });
    const errors = []; page.on("pageerror", error => errors.push(error.message));
    page.on("dialog", dialog => dialog.accept());
    await page.goto(`http://127.0.0.1:${server.address().port}/#/observer`);
    await page.waitForSelector(".observer-card");
    const tab = name => page.locator("#observer-operators .op-tab", { hasText: name });
    // The Operator dock starts closed: no operator section over the cards.
    const stored = () => page.evaluate(() => localStorage.getItem("claunch-observer-operator-dock"));
    assert.equal(await page.isVisible("#observer-dock"), false);
    assert.equal(await stored(), null);
    assert.equal(await page.getAttribute("#observer-operator-toggle", "aria-pressed"), "false");
    await page.click("#observer-operator-toggle");
    assert.equal(await stored(), "open");
    assert.equal(await page.getAttribute("#observer-operator-toggle", "aria-pressed"), "true");
    await tab("ops-a").waitFor();
    // Two columns: the dock is right of the cards and as tall as the page's
    // viewport, and it stays put while the page scrolls under it.
    const columns = await page.evaluate(() => {
      const view = document.getElementById("observer-view"), dock = document.getElementById("observer-dock").getBoundingClientRect();
      return {dockLeft: dock.left, cardsRight: document.getElementById("observer-cards").getBoundingClientRect().right, dockHeight: dock.height, viewHeight: view.clientHeight};
    });
    assert(columns.dockLeft >= columns.cardsRight, `dock right of the cards (${columns.dockLeft} < ${columns.cardsRight})`);
    assert(Math.abs(columns.dockHeight - columns.viewHeight) <= 1, `dock as tall as the viewport (${columns.dockHeight} vs ${columns.viewHeight})`);
    assert.equal(await tab("ops-a").getAttribute("aria-selected"), "true", "the only operator is selected");

    // Viewing the operator marks its open asks seen.
    await page.waitForFunction(() => document.querySelectorAll("#observer-operators .op-entry").length > 0);
    await page.waitForFunction(() => !document.querySelector("#observer-operators .op-badge.unseen"));
    assert(lastSent("POST", "/api/operators/op1/seen"), "the open operator is marked seen");
    assert.equal(await page.locator("#observer-operators .op-badge").innerText(), "2", "two asks still wait");
    assert.match(await page.locator("#observer-operators .op-scope").innerText(), /범위: alpha · 정지됨/);

    // The newest 20 entries, the rest opened in place.
    assert.equal(await page.locator("#observer-operators .op-entry").count(), 20);
    await page.click("text=이전 대화 5건 펼치기");
    assert.equal(await page.locator("#observer-operators .op-entry").count(), 25);
    assert.deepEqual(await innerScrollers(page), ["div.op-area"], "the expanded conversation scrolls inside the dock, nothing else does");
    assert.match(await page.locator('.op-entry[data-entry="d1"]').innerText(), /→ s1: 테스트 결과를 보고하십시오/);
    assert.equal(await page.locator('.op-entry[data-entry="p19"]').evaluate(e => e.classList.contains("op-reply")), true);
    assert.equal(await page.locator('.op-entry[data-entry="p19"] a.op-ref').getAttribute("href"), "#/observer/session/s1");

    // Asks: approve/deny and a choice, each one press.
    await page.locator('.op-entry[data-entry="a1"] button', { hasText: "승인" }).click();
    await page.waitForFunction(() => /사용자 답변 · 승인/.test(document.querySelector('.op-entry[data-entry="a1"]')?.textContent || ""));
    assert.deepEqual(lastSent("POST", "/asks/a1/answer").body, { decision: "approve" });
    await page.locator('.op-entry[data-entry="a2"] button', { hasText: "dev" }).click();
    await page.waitForFunction(() => /사용자 답변 · dev/.test(document.querySelector('.op-entry[data-entry="a2"]')?.textContent || ""));
    assert.deepEqual(lastSent("POST", "/asks/a2/answer").body, { decision: "dev" });

    if (process.env.CLAUNCH_SCREENSHOT) await page.screenshot({ path: process.env.CLAUNCH_SCREENSHOT });
    // The page composer talks to the operator: it is offered as a target and,
    // with nothing picked, is the default one.
    await page.click("#observer-composer-toggle");
    assert.deepEqual(await page.locator("#observer-target option").evaluateAll(es => es.map(e => e.value)), ["", "s1", "s2", "operator:op1"]);
    assert.equal(await page.inputValue("#observer-target"), "operator:op1");
    assert.equal(await page.locator("#observer-interrupt").isDisabled(), true, "nothing to interrupt in an operator");
    await page.fill("#observer-prompt", "지금 막힌 세션이 있습니까?");
    await page.click("#observer-send");
    await page.waitForFunction(() => /operator ops-a: 메시지 저장됨/.test(document.getElementById("observer-input-status").textContent));
    assert.deepEqual(lastSent("POST", "/api/operators/op1/message").body, { text: "지금 막힌 세션이 있습니까?" });
    assert.equal(await page.inputValue("#observer-prompt"), "");
    await page.waitForFunction(() => [...document.querySelectorAll("#observer-operators .op-user")].some(e => e.textContent.includes("지금 막힌 세션이 있습니까?")));
    assert.equal(sent.filter(r => r.url.endsWith("/keys")).length, 0, "no keys went to any session");
    // The dock's own input row: Enter sends to the selected operator.
    await page.fill("#observer-operators .op-composer textarea", "dock에서 묻습니다");
    await page.press("#observer-operators .op-composer textarea", "Enter");
    await page.waitForFunction(() => /operator ops-a:/.test(document.querySelector("#observer-operators .op-composer-status")?.textContent || ""));
    assert.deepEqual(lastSent("POST", "/api/operators/op1/message").body, { text: "dock에서 묻습니다" });
    assert.equal(await page.inputValue("#observer-operators .op-composer textarea"), "");
    // Kept per browser: a reload comes back with the dock open.
    await page.reload();
    await tab("ops-a").waitFor();
    assert.equal(await page.evaluate(() => document.getElementById("observer-view").classList.contains("dock-open")), true);
    await page.waitForSelector(".observer-card");
    await page.click("#observer-composer-toggle"); // a reload folds the composer again
    // Closed, the section goes and the cards take the width; a card's
    // 「operator에게 묻기」 opens it again, since the answer shows there.
    await page.click("#observer-operator-toggle");
    assert.equal(await stored(), "closed");
    assert.equal(await page.isVisible("#observer-operators"), false);
    const wide = await page.evaluate(() => document.getElementById("observer-cards").getBoundingClientRect().right / document.getElementById("observer-view").clientWidth);
    assert(wide > 0.95, `cards take the width when closed (${wide})`);

    // 「operator에게 묻기」 on a card: s1 (alpha) is in scope, s2 (beta) is not.
    const askOn = name => page.locator(`.observer-card[data-session="${name}"] .observer-ask-operator`);
    assert.equal(await askOn("s1").isDisabled(), false);
    assert.equal(await askOn("s2").isDisabled(), true);
    assert.match(await askOn("s2").getAttribute("title"), /s2\(beta\)을 범위에 둔 operator가 없습니다/);
    await page.selectOption("#observer-target", "s1");
    await askOn("s1").click();
    assert.equal(await page.isVisible("#observer-operators"), true, "asking opens the dock");
    assert.equal(await stored(), "open");
    assert.equal(await page.inputValue("#observer-target"), "operator:op1");
    assert.equal(await page.inputValue("#observer-prompt"), "s1에 대해: ");
    assert.equal(await page.locator("#observer-prompt").evaluate(e => document.activeElement === e), true);
    await page.fill("#observer-prompt", "");
    // Picking 「세션 선택」 keeps it: later polls do not put the operator back
    // (claunch-iw5i8.5).
    await page.selectOption("#observer-target", "");
    // A tab press redraws the page, which re-syncs the composer's targets.
    await tab("ops-a").click();
    await page.waitForTimeout(400);
    assert.equal(await page.inputValue("#observer-target"), "", "an empty target the reader picked stays empty");
    // The floating Operator's segment is hidden here: the operators are on the page.
    assert.equal(await page.isVisible(".operator-fab"), false);

    // Create: all projects.
    await page.click("text=+ 새 operator");
    await page.waitForSelector("#observer-operators .op-projects input[value=beta]");
    await page.fill("#observer-op-name", "all-ops");
    await page.check("#observer-op-all");
    assert.equal(await page.locator("#observer-operators .op-projects input[value=alpha]").isDisabled(), true);
    await page.click("#observer-operators .op-form button[type=submit]");
    await tab("all-ops").waitFor();
    assert.deepEqual(lastSent("POST", "/api/operators").body, { name: "all-ops", all: true, projects: [], profile: null, watch: true });
    await page.waitForFunction(() => document.querySelector('#observer-operators .op-tab[aria-selected="true"]')?.textContent.includes("all-ops"));
    assert.match(await page.locator("#observer-operators .op-scope").innerText(), /범위: 전체 프로젝트/);
    // An all-projects operator covers s2 as well.
    await page.waitForFunction(() => !document.querySelector('.observer-card[data-session="s2"] .observer-ask-operator')?.disabled);

    // Create: two projects by check box, with a profile; then a name clash.
    await page.click("text=+ 새 operator");
    await page.waitForSelector("#observer-operators .op-projects input[value=beta]");
    await page.fill("#observer-op-name", "two");
    await page.check("#observer-operators .op-projects input[value=alpha]");
    await page.check("#observer-operators .op-projects input[value=beta]");
    await page.selectOption("#observer-op-profile", "p1:claude");
    await page.click("#observer-operators .op-form button[type=submit]");
    await tab("two").waitFor();
    assert.deepEqual(lastSent("POST", "/api/operators").body, { name: "two", all: false, projects: ["alpha", "beta"], profile: "p1:claude", watch: true });
    await page.click("text=+ 새 operator");
    await page.waitForSelector("#observer-operators .op-projects input[value=beta]");
    // A name but no scope is refused before anything is sent.
    await page.fill("#observer-op-name", "three");
    const creates = sent.filter(r => r.method === "POST" && r.url === "/api/operators").length;
    await page.click("#observer-operators .op-form button[type=submit]");
    assert.equal(sent.filter(r => r.method === "POST" && r.url === "/api/operators").length, creates);
    assert.match(await page.locator("#observer-operators .op-form .op-error").innerText(), /프로젝트를 하나 이상/);
    await page.fill("#observer-op-name", "two");
    await page.check("#observer-operators .op-projects input[value=alpha]");
    await page.click("#observer-operators .op-form button[type=submit]");
    await page.waitForFunction(() => /already exists/.test(document.querySelector("#observer-operators .op-form .op-error")?.textContent || ""));
    await page.click("#observer-operators .op-form button:has-text('취소')");

    // Edit the scope of ops-a and switch its watch off.
    await tab("ops-a").click();
    await page.waitForFunction(() => /범위: alpha/.test(document.querySelector("#observer-operators .op-scope")?.textContent || ""));
    await page.click("#observer-operators .op-actions button:has-text('설정')");
    await page.waitForSelector("#observer-operators .op-projects input[value=beta]");
    assert.equal(await page.locator("#observer-operators .op-projects input[value=alpha]").isChecked(), true);
    await page.check("#observer-operators .op-projects input[value=beta]");
    await page.click("#observer-operators .op-form button[type=submit]");
    await page.waitForFunction(() => /범위: alpha, beta/.test(document.querySelector("#observer-operators .op-scope")?.textContent || ""));
    assert.deepEqual(lastSent("PATCH", "/api/operators/op1").body, { name: "ops-a", all: false, projects: ["alpha", "beta"], profile: null, watch: true });
    await page.uncheck("#observer-op-watch");
    await page.waitForFunction(() => document.getElementById("observer-op-watch")?.checked === false && !document.getElementById("observer-op-watch").disabled);
    assert.deepEqual(lastSent("PATCH", "/api/operators/op1").body, { watch: false });

    // Start needs a profile: without one the form opens and says so.
    const starts = sent.filter(r => r.url.endsWith("/start")).length;
    await page.click("#observer-operators .op-actions button:has-text('시작')");
    await page.waitForFunction(() => /프로필을 고르십시오/.test(document.querySelector("#observer-operators .op-form .op-error")?.textContent || ""));
    assert.equal(sent.filter(r => r.url.endsWith("/start")).length, starts);
    await page.click("#observer-operators .op-form button:has-text('취소')");
    // With one, start and stop are one press each, and a running operator
    // cannot be deleted.
    await tab("two").click();
    await page.waitForFunction(() => /범위: alpha, beta/.test(document.querySelector("#observer-operators .op-scope")?.textContent || "") && document.querySelector('#observer-operators .op-tab[aria-selected="true"]')?.textContent.includes("two"));
    await page.click("#observer-operators .op-actions button:has-text('시작')");
    await page.waitForFunction(() => /실행 중 · s900/.test(document.querySelector("#observer-operators .op-scope")?.textContent || ""));
    assert.equal(await page.locator("#observer-operators .op-actions button:has-text('삭제')").isDisabled(), true);
    await page.click("#observer-operators .op-actions button:has-text('정지')");
    await page.waitForFunction(() => /정지됨/.test(document.querySelector("#observer-operators .op-scope")?.textContent || ""));
    await page.click("#observer-operators .op-actions button:has-text('삭제')");
    await page.waitForFunction(() => ![...document.querySelectorAll("#observer-operators .op-tab")].some(t => t.textContent.includes("two")));
    assert(lastSent("DELETE", "/api/operators/op3"));

    // The composer's draft grows to 40% of the window and scrolls inside
    // itself past that; beside it the dock's conversation scrolls inside the
    // dock (the reader's choice for that column). Nothing else scrolls.
    await page.fill("#observer-prompt", Array.from({ length: 120 }, (_, i) => `줄 ${i}`).join("\n"));
    const box = await page.locator("#observer-prompt").evaluate(e => ({ h: e.getBoundingClientRect().height, cap: innerHeight * 0.4 }));
    assert(box.h <= box.cap + 1, `the draft stops at 40vh (${box.h} > ${box.cap})`);
    const scrollers = await innerScrollers(page);
    assert(scrollers.includes("textarea#observer-prompt"), scrollers.join());
    assert.deepEqual(scrollers.filter(s => !["textarea#observer-prompt", "div.op-area"].includes(s)), [], "no other inner scroller");
    await page.fill("#observer-prompt", "");
    // The dock stays on screen while the page scrolls.
    const dockTop = async () => page.evaluate(() => document.getElementById("observer-dock").getBoundingClientRect().top);
    await page.setViewportSize({ width: 1280, height: 500 });
    await page.waitForTimeout(200);
    const before = await dockTop();
    const scrolled = await page.evaluate(() => { const v = document.getElementById("observer-view"); v.scrollTop = 300; return v.scrollTop; });
    assert(scrolled > 0, "the page scrolls at 500px high");
    assert.equal(await dockTop(), before, "the dock is held on screen");
    await page.setViewportSize({ width: 1280, height: 900 });

    // On a phone the dock sits above the page in its flow, fits the width and
    // adds no scroller; the top bar carries the toggle.
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(300);
    assert.deepEqual(await innerScrollers(page), []);
    assert.equal(await page.isVisible("#observer-mobile-operator"), true);
    assert.equal(await page.getAttribute("#observer-mobile-operator", "aria-pressed"), "true");
    assert.equal(await page.locator("#observer-operators .op-tab").first().isVisible(), true);

    assert.deepEqual(errors, []);
    console.log("PASS: operator tabs, seen badge, feed folding and kinds, approve/choice answers, composer target and send, card ask-about scope, picked empty target kept, fab hidden, create all/projects, validation and 409, scope edit, watch toggle, start/stop/delete, 40vh draft cap, dock closed by default, toggle kept in localStorage across reload, two columns with a held dock and its own input row, ask opens the dock, inner scrollers only the draft and the dock conversation on desktop and none on mobile");
  } finally { await browser.close(); server.close(); server.closeAllConnections(); }
})().catch(error => { console.error(error); server.close(); server.closeAllConnections(); process.exitCode = 1; });
