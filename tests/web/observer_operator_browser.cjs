/* Browser integration for Operator v2 on the Observer page (claunch-iw5i8.4):
   tabs, creation with a project multi-check or all projects, scope and watch
   edits, the conversation (send from the page composer, answers to asks,
   older entries opened in place), 「operator에게 묻기」 on a session card, the
   composer's height cap, an empty target the reader picked staying empty,
   and the page staying the one scroller. The conversation is threads
   (claunch-iw5i8.12.1): the list and the selected thread, one at a time in
   the dock's narrow column, a row press and 「← 목록」 moving between them, a
   message going to the thread on screen, 「+ 새 thread」 opening one with a
   title and an attached image, and the dispatch card kept in its thread.
   CLAUNCH_PLAYWRIGHT may name an installed playwright-core.
   Run: node tests/web/observer_operator_browser.cjs. No live daemon. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require(process.env.CLAUNCH_PLAYWRIGHT || "playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");
const sent = [], reads = [];
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
/* One report written the way an operator writes them: markdown, with a line
   of raw HTML that must stay text. */
feed[17].text = "## 진행 요약\n\n- **s1**: `pytest` 실행 중\n- s2: 대기\n\n```\nFAILED tests/test_a.py::test_b - AssertionError: a very long line that would otherwise run past the narrow dock column\n```\n\n<img src=x onerror=\"window.__pwned=1\">";
feed.push({ id: "u1", at: at(19), kind: "user", role: "user", text: "s1은 무엇을 하고 있습니까?" });
feed.push({ id: "p19", at: at(20), kind: "post", role: "bot", text: "s1은 테스트를 돌리고 있습니다.", level: "attention", refs: ["s1"], parent: "p18" });
feed.push({ id: "d1", at: at(21), kind: "dispatch", role: "bot", target: "s1", text: "테스트 결과를 보고하십시오", on_behalf_of: "u1", delivery: "sent" });
feed.push({ id: "y1", at: at(22), kind: "system", role: "system", text: "설정 변경 — 감시: 켜짐" });
feed.push({ id: "a1", at: at(23), kind: "ask", role: "bot", type: "approve", text: "s1에 재시작을 지시할까요?", choices: [], level: "urgent", refs: ["s1"], answer: null });
feed.push({ id: "a2", at: at(24), kind: "ask", role: "bot", type: "choice", text: "어느 브랜치를 볼까요?", choices: ["main", "dev"], level: "attention", refs: [], answer: null });
for (const e of feed) e.thread = "th-a";
// A second, older thread with one report in it.
feed.unshift({ id: "b1", at: at(0), kind: "post", role: "bot", text: "배포 thread 보고", level: "info", refs: [], thread: "th-b" });
const threadTitles = { "th-a": "이전 대화", "th-b": "배포" };
const staged = [];
const ops = {
  op1: { id: "op1", name: "ops-a", all: false, projects: ["alpha"], profile: null, model: null, effort: null,
         watch: true, created_at: at(0), session: null, feed, seen: [], titles: threadTitles },
};
function threadsOf(o) {
  const ids = Object.keys(o.titles || {});
  const rows = ids.map(id => {
    const own = o.feed.filter(e => e.thread === id);
    const last = own.at(-1) || null;
    return { id, title: o.titles[id], status: "open", updated_at: last?.at || at(0), messages: own.length,
      pending: own.filter(e => e.kind === "ask" && !e.answer).length, last };
  });
  return rows.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
}
let serial = 1;
function summary(o) {
  const open = o.feed.filter(e => e.kind === "ask" && !e.answer).map(e => e.id);
  return { id: o.id, name: o.name, all: o.all, projects: o.projects, profile: o.profile, model: o.model,
    effort: o.effort, watch: o.watch, created_at: o.created_at,
    workflow: o.workflow || "operator-v2", qa: !!o.qa, gds6_url: o.gds6_url || null,
    operator: o.session ? { name: o.session, running: true, status: o.status || "idle" } : null,
    pending: open.length, unseen: open.filter(id => !o.seen.includes(id)).length };
}
function operatorsApi(req, res, body) {
  const url = req.url.split("?")[0], parts = url.split("/").slice(3).map(decodeURIComponent);
  const json = (value, status = 200) => { res.statusCode = status; return res.end(JSON.stringify(value)); };
  const data = body ? JSON.parse(body) : {};
  if (req.method !== "GET") sent.push({ method: req.method, url, body: data });
  else reads.push(req.url);
  if (!parts.length) {
    if (req.method === "GET") return json({ operators: Object.values(ops).map(summary).sort((a, b) => a.name.localeCompare(b.name)) });
    // The daemon raises a name clash as a 409 with a plain-text body.
    if (Object.values(ops).some(o => o.name === data.name)) { res.statusCode = 409; return res.end(`an operator named '${data.name}' already exists`); }
    if (!data.all && !(data.projects || []).length) return json({ error: "projects required" }, 400);
    const id = `op${++serial}`;
    ops[id] = { id, name: data.name, all: !!data.all, projects: data.all ? [] : data.projects, profile: data.profile || null,
      model: null, effort: null, watch: data.watch !== false, created_at: at(30), session: null, feed: [], seen: [],
      workflow: data.workflow || "operator-v2", qa: !!data.qa, gds6_url: data.gds6_url || null };
    return json(summary(ops[id]), 201);
  }
  const o = ops[parts[0]];
  if (!o) return json({ error: "no such operator" }, 404);
  const rest = parts.slice(1).join("/");
  if (!rest && req.method === "GET") {
    const threads = threadsOf(o);
    const asked = new URL(req.url, "http://x").searchParams.get("thread");
    if (asked && !threads.some(t => t.id === asked)) return json({ error: "no such thread" }, 404);
    const thread = asked || threads[0]?.id || null;
    return json({ ...summary(o), threads, thread, feed: o.feed.filter(e => e.thread === thread).slice(-200), sessions: [], gates: [] });
  }
  if (!rest && req.method === "PATCH") {
    for (const key of ["name", "all", "projects", "profile", "watch", "workflow", "qa", "gds6_url"]) if (key in data) o[key] = data[key];
    if (o.all) o.projects = [];
    return json(summary(o));
  }
  if (!rest && req.method === "DELETE") { delete ops[o.id]; return json({ deleted: o.id }); }
  if (rest === "seen") { o.seen = o.feed.filter(e => e.kind === "ask" && !e.answer).map(e => e.id); return json(summary(o)); }
  if (rest === "message") {
    const thread = data.thread || threadsOf(o)[0]?.id || "th-new";
    o.titles = o.titles || {}; if (!o.titles[thread]) o.titles[thread] = "Thread #1";
    const entry = { id: `m${o.feed.length}`, at: new Date().toISOString(), kind: "user", role: "user", text: data.text, thread,
      images: (data.images || []).map(name => ({ name, type: "image/png", bytes: 3 })) };
    o.feed.push(entry);
    return json({ ...entry, nudge: o.session ? "sent" : "no-operator" });
  }
  if (rest === "threads") {
    const id = `th-n${Object.keys(o.titles || {}).length}`;
    o.titles = o.titles || {}; o.titles[id] = data.title || "Thread #9";
    if (!data.text && !(data.images || []).length) return json({ thread: { id, title: o.titles[id] } });
    const entry = { id: `m${o.feed.length}`, at: new Date().toISOString(), kind: "user", role: "user", text: data.text, thread: id,
      images: (data.images || []).map(name => ({ name, type: "image/png", bytes: 3 })) };
    o.feed.push(entry);
    return json({ thread: { id, title: o.titles[id] }, entry, nudge: "no-operator" });
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
      if (req.url === "/api/threads/images" && req.method === "POST") {
        const id = `${String(staged.length).padStart(20, "0")}.png`;
        staged.push({ id, type: req.headers["content-type"], bytes: body.length });
        return res.end(JSON.stringify({ id }));
      }
      if (req.url.startsWith("/api/threads/") && req.url.includes("/images/")) {
        res.setHeader("Content-Type", "image/png");
        return res.end(Buffer.from("89504e470d0a1a0a", "hex"));
      }
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
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, permissions: ["clipboard-read", "clipboard-write"] });
    await page.addInitScript(() => {
      localStorage.setItem("claunch_token:/", "fixture-token");
      // The observer polls on a 10s interval (observer.js); waiting ten
      // seconds for the page to notice a change the harness just made would
      // make this harness minutes long, so it is sped up.
      //
      // 200ms was too fast: each tick calls operators().refresh() -> render(),
      // which rebuilds the operator section's DOM (including an open form).
      // A form being filled at that rate is replaced under the pointer, and
      // Playwright's "visible, enabled and stable" action check never settles
      // on an element that is replaced five times a second -- that was the
      // flake at `waiting for .op-tab two` and at the form's error line, 1
      // run in ~8, with the element present and visible whenever it was
      // measured. A second is still far faster than the wait, and slow enough
      // that a press lands on the node it was aimed at.
      const interval = window.setInterval;
      window.setInterval = (fn, ms, ...args) => interval(fn, ms === 10000 ? 1000 : ms, ...args);
      // Chromium will not settle an action on an element whose box moves
      // between paints, so the page's own animations go off as well.
      window.addEventListener("DOMContentLoaded", () => {
        const style = document.createElement("style");
        style.textContent = "*, *::before, *::after { animation: none !important;"
          + " transition: none !important; scroll-behavior: auto !important; }";
        document.head.appendChild(style);
      });
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
    // The session line above the conversation (claunch-iw5i8.13): outside the
    // dock's scroller, naming the operator; it has no session yet.
    const sessionLine = () => page.evaluate(() => {
      const bar = document.querySelector("#observer-operators > .op-sessionbar");
      const link = bar?.querySelector("a.op-bar-open");
      return bar && { hidden: bar.hidden, who: bar.querySelector(".op-bar-who")?.textContent, session: bar.querySelector(".op-bar-session")?.textContent,
        href: link ? link.getAttribute("href") : null, outside: !bar.closest(".op-area") };
    });
    assert.deepEqual(await sessionLine(), { hidden: false, who: "ops-a", session: "세션 없음", href: null, outside: true });

    // The newest 20 entries, the rest opened in place.
    assert.equal(await page.locator("#observer-operators .op-entry").count(), 20);
    await page.click("text=이전 대화 5건 펼치기");
    assert.equal(await page.locator("#observer-operators .op-entry").count(), 25);
    assert.deepEqual(await innerScrollers(page), ["div.op-area"], "the expanded conversation scrolls inside the dock, nothing else does");
    assert.match(await page.locator('.op-entry[data-entry="d1"]').innerText(), /→ s1: 테스트 결과를 보고하십시오/);
    assert.equal(await page.locator('.op-entry[data-entry="p19"]').evaluate(e => e.classList.contains("op-reply")), true);
    assert.equal(await page.locator('.op-entry[data-entry="p19"] a.op-ref').getAttribute("href"), "#/observer/session/s1");

    // Bodies are markdown: structure from the renderer, raw HTML left as text,
    // and a code block wraps in the column instead of scrolling sideways.
    const md = await page.locator('.op-entry[data-entry="p17"] .op-text').evaluate(e => ({
      md: e.classList.contains("md"), h: e.querySelectorAll(".md-h").length, li: e.querySelectorAll("li").length,
      strong: e.querySelectorAll("strong, .md-strong").length, code: e.querySelectorAll("code").length,
      pre: e.querySelectorAll("pre").length, img: e.querySelectorAll("img").length, raw: /<img src=x/.test(e.textContent),
      pwned: !!window.__pwned, preFits: [...e.querySelectorAll("pre")].every(p => p.scrollWidth <= p.clientWidth + 1) }));
    assert.deepEqual(md, { md: true, h: 1, li: 2, strong: 1, code: 2, pre: 1, img: 0, raw: true, pwned: false, preFits: true });
    // A code block carries a copy button (claunch-fokj3) that puts the
    // block's own text on the clipboard, without the button's label.
    const copy = page.locator('.op-entry[data-entry="p17"] pre .op-copy');
    assert.equal(await copy.count(), 1, "one copy button on the one code block");
    await copy.click();
    await page.waitForFunction(() => document.querySelector('.op-entry[data-entry="p17"] pre .op-copy')?.textContent === "복사됨");
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()),
      "FAILED tests/test_a.py::test_b - AssertionError: a very long line that would otherwise run past the narrow dock column");
    // The conversation reads leave the session panel out (claunch-fokj3):
    // the daemon spends seconds on it and nothing here draws it.
    const viewReads = reads.filter(u => /^\/api\/operators\/op1(\?|$)/.test(u));
    assert(viewReads.length > 0 && viewReads.every(u => new URL(u, "http://x").searchParams.get("panel") === "0"),
      `every conversation read asks for panel=0: ${JSON.stringify(viewReads)}`);

    // Text selected in the conversation survives until it is copied
    // (claunch-fokj3). Every poll used to rebuild the section, and the
    // release that ended a drag-select redrew it too, so the selection was
    // gone before Ctrl+C.
    {
      await page.evaluate(() => getSelection().removeAllRanges());
      // Whichever entry body is on screen now (the steps above moved
      // between threads).
      const text = await page.evaluate(() => {
        const body = [...document.querySelectorAll("#observer-operators .op-entry .op-text")].reverse()
          .find(e => e.getClientRects().length && e.textContent.trim().length > 4);
        body.dataset.probe = "1";
        return body.textContent.trim();
      });
      const entry = '#observer-operators .op-text[data-probe="1"]';
      await page.locator(entry).scrollIntoViewIfNeeded();
      const box = await page.locator(entry).boundingBox();
      const selected = () => page.evaluate(() => getSelection().toString());
      const connected = () => page.evaluate(() => window.__sel?.isConnected === true);
      await page.evaluate(sel => { window.__sel = document.querySelector(sel); }, entry);
      // A real drag across the entry's text: the release must not redraw.
      await page.mouse.move(box.x + 2, box.y + box.height / 2);
      await page.mouse.down();
      await page.mouse.move(box.x + box.width - 2, box.y + box.height / 2, { steps: 5 });
      await page.mouse.up();
      await page.evaluate(() => new Promise(r => setTimeout(r, 50)));
      assert(text.includes((await selected()).trim()) && (await selected()).trim().length > 2, `the drag selected the entry's text: ${await selected()}`);
      assert.equal(await connected(), true, "the release after a drag-select redraws nothing");
      // An unchanged poll draws nothing; a poll with news waits while the
      // selection stands.
      await page.evaluate(() => globalThis.ObserverOperators.refresh());
      const probe = { id: "z2", at: at(41), kind: "system", role: "system", text: "probe", thread: "th-a" };
      ops.op1.feed.push(probe);
      await page.evaluate(() => globalThis.ObserverOperators.refresh());
      assert((await selected()).trim().length > 2, "the selection outlives a poll that read news");
      assert.equal(await connected(), true, "the held poll did not redraw");
      // Letting go of the selection lets the held redraw through.
      await page.evaluate(() => getSelection().removeAllRanges());
      await page.waitForFunction(() => !!document.querySelector('#observer-operators .op-entry[data-entry="z2"]'), null, { timeout: 5000 });
      assert.equal(await connected(), false);
      ops.op1.feed.splice(ops.op1.feed.indexOf(probe), 1);
      await page.evaluate(() => globalThis.ObserverOperators.refresh());
      await page.waitForFunction(() => !document.querySelector('#observer-operators .op-entry[data-entry="z2"]'));
    }

    // Asks: approve/deny and a choice, each one press.
    await page.locator('.op-entry[data-entry="a1"] button', { hasText: "승인" }).click();
    await page.waitForFunction(() => /사용자 답변 · 승인/.test(document.querySelector('.op-entry[data-entry="a1"]')?.textContent || ""));
    assert.deepEqual(lastSent("POST", "/asks/a1/answer").body, { decision: "approve" });
    await page.fill('.op-entry[data-entry="a2"] textarea', "**dev**로 갑니다");
    await page.locator('.op-entry[data-entry="a2"] button', { hasText: "dev" }).click();
    await page.waitForFunction(() => /사용자 답변 · dev/.test(document.querySelector('.op-entry[data-entry="a2"]')?.textContent || ""));
    assert.deepEqual(lastSent("POST", "/asks/a2/answer").body, { decision: "dev", text: "**dev**로 갑니다" });
    // The answer's own words are markdown too, under the decision line.
    assert.equal(await page.locator('.op-entry[data-entry="a2"] .op-answer-text.md strong, .op-entry[data-entry="a2"] .op-answer-text.md .md-strong').first().innerText(), "dev");

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
    assert.deepEqual(lastSent("POST", "/api/operators/op1/message").body, { text: "지금 막힌 세션이 있습니까?", thread: "th-a" });
    assert.equal(await page.inputValue("#observer-prompt"), "");
    await page.waitForFunction(() => [...document.querySelectorAll("#observer-operators .op-user")].some(e => e.textContent.includes("지금 막힌 세션이 있습니까?")));
    assert.equal(sent.filter(r => r.url.endsWith("/keys")).length, 0, "no keys went to any session");
    // The dock's own input row: Enter sends to the selected operator.
    await page.fill("#observer-operators .op-composer textarea", "dock에서 묻습니다");
    await page.press("#observer-operators .op-composer textarea", "Enter");
    await page.waitForFunction(() => /operator ops-a:/.test(document.querySelector("#observer-operators .op-composer-status")?.textContent || ""));
    assert.deepEqual(lastSent("POST", "/api/operators/op1/message").body, { text: "dock에서 묻습니다", thread: "th-a" });
    assert.equal(await page.inputValue("#observer-operators .op-composer textarea"), "");
    // Ctrl+J breaks the line at the caret and does not send (claunch-aos8w),
    // the chord the page's own composer takes; Enter still sends.
    await page.fill("#observer-operators .op-composer textarea", "한줄");
    await page.evaluate(() => {
      const t = document.querySelector("#observer-operators .op-composer textarea");
      t.focus();
      t.selectionStart = t.selectionEnd = 1;
    });
    const postsBefore = sent.filter(r => r.url.endsWith("/api/operators/op1/message")).length;
    await page.press("#observer-operators .op-composer textarea", "Control+J");
    assert.equal(await page.inputValue("#observer-operators .op-composer textarea"), "한\n줄", "Ctrl+J breaks the line at the caret");
    assert.equal(sent.filter(r => r.url.endsWith("/api/operators/op1/message")).length, postsBefore, "Ctrl+J does not send");
    await page.press("#observer-operators .op-composer textarea", "Enter");
    // The field clears only after sendTo resolves, so an empty field means
    // the POST the stub logged is this message.
    await page.waitForFunction(() => document.querySelector("#observer-operators .op-composer textarea").value === "");
    assert.deepEqual(lastSent("POST", "/api/operators/op1/message").body, { text: "한\n줄", thread: "th-a" });
    assert.equal(await page.inputValue("#observer-operators .op-composer textarea"), "");

    // Threads: the dock's column is narrow, so the selected thread shows and
    // the list waits behind 「← 목록」.
    const visible = sel => page.locator(`#observer-operators ${sel}`).first().isVisible();
    assert.equal(await visible(".op-thread-list"), false);
    assert.equal(await page.locator("#observer-operators .op-thread-title").innerText(), "이전 대화");
    // The thread's head holds to the top of the conversation as it scrolls
    // (claunch-1ruqi): 「← 목록」 stays where the reader needs it.
    const pinnedHead = await page.evaluate(() => {
      const area = document.querySelector("#observer-operators .op-area");
      area.scrollTop = area.scrollHeight;
      const box = area.getBoundingClientRect();
      const headBox = document.querySelector("#observer-operators .op-thread-head");
      const head = headBox.getBoundingClientRect();
      const back = document.querySelector("#observer-operators .op-thread-back").getBoundingClientRect();
      // The row covers the scroller's top padding too: a point just under the
      // scroller's own top edge lands in the head, not in an entry.
      const probe = document.elementFromPoint(box.left + box.width / 2, box.top + 2);
      return { scrolled: area.scrollTop > 0, offset: Math.round(head.top - box.top), back: back.width > 0 && back.top >= box.top - 1 && back.bottom <= box.bottom, covers: !!probe && headBox.contains(probe) };
    });
    assert.deepEqual(pinnedHead, { scrolled: true, offset: 0, back: true, covers: true }, "the head covers the top of the scroller, padding included, and keeps 「← 목록」");
    await page.click("#observer-operators .op-thread-back");
    assert.equal(await visible(".op-thread-list"), true);
    assert.equal(await visible(".op-thread-view"), false);
    assert.deepEqual(await page.locator("#observer-operators .op-thread-row .op-thread-name").allInnerTexts(), ["이전 대화", "배포"]);
    assert.equal(await page.locator('#observer-operators .op-thread-row[data-thread="th-a"] .op-thread-pending').count(), 0, "th-a's asks were answered above");
    assert.equal(await page.getAttribute('#observer-operators .op-thread-row[data-thread="th-a"]', "aria-current"), "true");
    await page.click('#observer-operators .op-thread-row[data-thread="th-b"]');
    await page.waitForFunction(() => document.querySelector("#observer-operators .op-thread-title")?.textContent === "배포"
      && document.querySelector('#observer-operators .op-entry[data-entry="b1"]'));
    assert.equal(await page.locator("#observer-operators .op-entry").count(), 1);
    assert.equal(await visible(".op-thread-list"), false, "a row press shows its thread");
    await page.fill("#observer-operators .op-composer textarea", "배포 thread에 묻습니다");
    await page.press("#observer-operators .op-composer textarea", "Enter");
    await page.waitForFunction(() => [...document.querySelectorAll("#observer-operators .op-user")].some(e => e.textContent.includes("배포 thread에 묻습니다")));
    assert.deepEqual(lastSent("POST", "/api/operators/op1/message").body, { text: "배포 thread에 묻습니다", thread: "th-b" });
    // A new thread: a title, a message and an image; the image is staged
    // first and the thread opens with both.
    await page.click("#observer-operators .op-thread-back");
    await page.click("#observer-operators .op-thread-new");
    assert.equal(await visible(".op-thread-title-input"), true);
    await page.fill("#observer-operators .op-thread-title-input", "새 장애");
    await page.setInputFiles("#observer-operators .op-attach-input", { name: "shot.png", mimeType: "image/png", buffer: Buffer.from("89504e470d0a1a0a", "hex") });
    assert.equal(await page.locator("#observer-operators .op-attach img").count(), 1, "the image waits under the input row");
    await page.fill("#observer-operators .op-composer textarea", "이 화면을 보십시오");
    await page.press("#observer-operators .op-composer textarea", "Enter");
    await page.waitForFunction(() => document.querySelector("#observer-operators .op-thread-title")?.textContent === "새 장애");
    assert.deepEqual(staged.map(x => x.type), ["image/png"]);
    assert.deepEqual(lastSent("POST", "/api/operators/op1/threads").body, { title: "새 장애", text: "이 화면을 보십시오", images: [staged[0].id] });
    await page.waitForSelector("#observer-operators .op-user .op-image img");
    assert.match(await page.getAttribute("#observer-operators .op-user .op-image img", "src"), /api\/threads\/th-n2\/images\/0+\.png$/);
    assert.equal(await page.locator("#observer-operators .op-attach img").count(), 0, "sent, the strip is empty");
    // Back to the first thread for the rest.
    await page.click("#observer-operators .op-thread-back");
    await page.click('#observer-operators .op-thread-row[data-thread="th-a"]');
    await page.waitForFunction(() => document.querySelector("#observer-operators .op-thread-title")?.textContent === "이전 대화");
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
    assert.deepEqual(lastSent("POST", "/api/operators").body, { name: "all-ops", all: true, projects: [], profile: null, watch: true,
      workflow: "operator-v2", qa: false, gds6_url: null });
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
    assert.deepEqual(lastSent("POST", "/api/operators").body, { name: "two", all: false, projects: ["alpha", "beta"], profile: "p1:claude", watch: true,
      workflow: "operator-v2", qa: false, gds6_url: null });
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
    assert.deepEqual(lastSent("PATCH", "/api/operators/op1").body, { name: "ops-a", all: false, projects: ["alpha", "beta"], profile: null, watch: true,
      workflow: "operator-v2", qa: false, gds6_url: null });
    await page.uncheck("#observer-op-watch");
    await page.waitForFunction(() => document.getElementById("observer-op-watch")?.checked === false && !document.getElementById("observer-op-watch").disabled);
    assert.deepEqual(lastSent("PATCH", "/api/operators/op1").body, { watch: false });

    // The gds6 workflow: its address field appears, session QA is switched on
    // with it (a gds6 operator asks some questions in the QA pane), the tab
    // says which kind it is, and the record is sent with all three.
    await page.click("#observer-operators .op-actions button:has-text('설정')");
    await page.waitForSelector("#observer-op-workflow");
    assert.equal(await page.locator("#observer-operators .op-gds6").isVisible(), false);
    assert.equal(await page.isChecked("#observer-op-qa"), false);
    await page.selectOption("#observer-op-workflow", "gds-operator-v2");
    assert.equal(await page.locator("#observer-operators .op-gds6").isVisible(), true);
    assert.equal(await page.isChecked("#observer-op-qa"), true);
    await page.fill("#observer-op-gds6-url", "http://gds:8888");
    await page.click("#observer-operators .op-form button[type=submit]");
    await page.waitForFunction(() => document.querySelector("#observer-operators .op-tab[aria-selected='true'] .op-chip")?.textContent === "gds6");
    assert.deepEqual(lastSent("PATCH", "/api/operators/op1").body, { name: "ops-a", all: false, projects: ["alpha", "beta"], profile: null, watch: false,
      workflow: "gds-operator-v2", qa: true, gds6_url: "http://gds:8888" });

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
    // Started, the session line names its session and links to its page.
    assert.deepEqual(await sessionLine(), { hidden: false, who: "two", session: "s900 · 실행 중 (idle)", href: "#/s/s900", outside: true });
    assert.equal(await page.locator("#observer-operators .op-actions button:has-text('삭제')").isDisabled(), true);
    // The dot draws the session's state the way the rail's dot does
    // (claunch-zko8f): green idle, amber busy, on the tab and the session line.
    const dots = () => page.evaluate(() => [
      document.querySelector('#observer-operators .op-tab[aria-selected="true"] .op-dot'),
      document.querySelector("#observer-operators > .op-sessionbar .op-dot"),
    ].map(d => d && { cls: d.className, bg: getComputedStyle(d).backgroundColor }));
    assert.deepEqual(await dots(), Array(2).fill({ cls: "op-dot on idle", bg: "rgb(63, 185, 80)" }));
    ops.op3.status = "busy";
    await tab("two").click();
    await page.waitForFunction(() => /실행 중 \(busy\)/.test(document.querySelector("#observer-operators > .op-sessionbar")?.textContent || ""));
    assert.deepEqual(await dots(), Array(2).fill({ cls: "op-dot on busy", bg: "rgb(210, 153, 34)" }));
    delete ops.op3.status;
    await page.click("#observer-operators .op-actions button:has-text('정지')");
    await page.waitForFunction(() => /정지됨/.test(document.querySelector("#observer-operators .op-scope")?.textContent || ""));
    assert.equal((await sessionLine()).session, "세션 없음");
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
    // The thread's head holds to the top of the page once the conversation
    // reaches it — on a phone the page is the one scroller (claunch-1ruqi).
    // The long thread of ops-a first: a short one scrolls away with the section.
    await tab("ops-a").click();
    await page.waitForSelector('#observer-operators .op-thread-row[data-thread="th-a"]', { state: "attached" });
    await page.click("#observer-operators .op-thread-back");
    await page.click('#observer-operators .op-thread-row[data-thread="th-a"]');
    await page.waitForFunction(() => document.querySelector("#observer-operators .op-thread-title")?.textContent === "이전 대화"
      && document.querySelector("#observer-operators .op-entry"));
    const pageHead = await page.evaluate(() => {
      const view = document.getElementById("observer-view");
      const head = () => document.querySelector("#observer-operators .op-thread-head").getBoundingClientRect();
      const bar = document.querySelector("#observer-operators > .op-sessionbar");
      const box = view.getBoundingClientRect();
      const natural = view.scrollTop + (head().top - box.top);
      view.scrollTop = natural + 60;
      const h = head(), b = bar.getBoundingClientRect();
      // The session line keeps the top band and the head holds right under it:
      // neither covers the other (claunch-1ruqi rework — head z 2 over bar z 1
      // hid the session line whenever a long thread was scrolled).
      const probeBar = document.elementFromPoint(b.left + 8, b.top + b.height / 2);
      const back = document.querySelector("#observer-operators .op-thread-back").getBoundingClientRect();
      return { scrolled: view.scrollTop > 0, barPinned: Math.round(b.top - box.top) === 0,
        headUnderBar: Math.abs(h.top - b.bottom) <= 1, barOwnsItsRow: !!probeBar && bar.contains(probeBar),
        back: back.width > 0 && back.top >= h.top - 1 && back.bottom <= h.bottom + 1 };
    });
    assert.deepEqual(pageHead, { scrolled: true, barPinned: true, headUnderBar: true, barOwnsItsRow: true, back: true }, "the head holds under the pinned session line on the stepped page, both rows on screen");
    await page.evaluate(() => { document.getElementById("observer-view").scrollTop = 0; });
    // On the phone the list and the conversation take turns as well.
    await page.click("#observer-operators .op-thread-back");
    assert.equal(await page.locator("#observer-operators .op-thread-list").isVisible(), true);
    assert.deepEqual(await innerScrollers(page), []);
    if (process.env.CLAUNCH_SCREENSHOT_PHONE) await page.screenshot({ path: process.env.CLAUNCH_SCREENSHOT_PHONE, fullPage: true });

    // A poll's redraw waits while a pointer is down: render() replaces the
    // whole section, and a button replaced between a press's down and up
    // never receives its click. This is the shipped behaviour measured
    // against a poll pinned to land inside a press -- held down with the
    // pointer for the length of one, then released.
    {
      await page.setViewportSize({ width: 1280, height: 900 });
      await tab("ops-a").click();
      await page.waitForFunction(() => document.querySelector('#observer-operators .op-tab[aria-selected="true"]')?.textContent.includes("ops-a"));
      await page.click("#observer-operators .op-actions button:has-text('설정')");
      await page.waitForSelector("#observer-operators .op-form");
      // The box is read in the page: Playwright's own action check wants the
      // node to stay put, and the poll is exactly what moves it.
      await page.evaluate(() => document.querySelector("#observer-operators .op-form").scrollIntoView({ block: "center" }));
      const saveBox = await page.evaluate(() => {
        const r = document.querySelector("#observer-operators .op-form button[type=submit]").getBoundingClientRect();
        return { x: r.left, y: r.top, width: r.width, height: r.height };
      });
      const saves = sent.filter(r => r.method === "PATCH" && r.url === "/api/operators/op1").length;
      // Down on the button, one poll's redraw in between, then up. The click
      // the browser dispatches after the up has to reach the form.
      await page.mouse.move(saveBox.x + saveBox.width / 2, saveBox.y + saveBox.height / 2);
      await page.mouse.down();
      await page.evaluate(() => globalThis.ObserverOperators.refresh());
      await page.mouse.up();
      // The save closes the form; the press that never landed leaves it open.
      await page.waitForFunction(() => !document.querySelector("#observer-operators .op-form"), null, { timeout: 5000 });
      assert.equal(sent.filter(r => r.method === "PATCH" && r.url === "/api/operators/op1").length, saves + 1,
        "the press lands although a poll ran between its down and its up");
    }

    // A press that ends PAST the view's edge still releases it. The flag that
    // holds a redraw back while a pointer is down is set on a down inside the
    // view; if its only release listener sat on the view's own root, a
    // pointerup over the page outside would never reach it and the flag
    // would stay set -- every later poll would go on waiting for a press
    // that had already ended, and the section would stop redrawing for good.
    // Measured before the window listener: five polls after such a release
    // drew nothing.
    {
      await tab("ops-a").click();
      await page.waitForFunction(() => document.querySelector('#observer-operators .op-tab[aria-selected="true"]')?.textContent.includes("ops-a"));
      // A redraw is visible as the section's nodes being replaced, so the
      // probe holds the node that is on screen now and asks whether it is
      // still the one connected once the polls have run.
      const spot = await page.evaluate(() => {
        window.__before = document.querySelector("#observer-operators .op-tabs");
        const r = document.querySelector("#observer-operators .op-tab").getBoundingClientRect();
        // A point genuinely OUTSIDE the dock: on desktop the dock is the
        // right column, so the bottom-right corner of the window is still
        // inside it -- a release there bubbles through the dock and the
        // probe would pass without the window listener doing anything. The
        // dock's left edge is the boundary to cross.
        const dock = document.querySelector("#observer-operators").getBoundingClientRect();
        return { x: r.left + r.width / 2, y: r.top + r.height / 2,
                 bx: Math.max(1, dock.left - 40), by: Math.round(r.top + r.height / 2) };
      });
      await page.mouse.move(spot.x, spot.y);
      await page.mouse.down();
      await page.mouse.move(spot.bx, spot.by);   // well outside the dock
      await page.mouse.up();
      // A poll that reads what is drawn redraws nothing (claunch-fokj3), so
      // the probe's poll reads something new.
      const probe = { id: "z1", at: at(40), kind: "system", role: "system", text: "probe", thread: "th-a" };
      ops.op1.feed.push(probe);
      await page.evaluate(() => globalThis.ObserverOperators.refresh());
      await page.evaluate(() => globalThis.ObserverOperators.refresh());
      await page.waitForFunction(() => window.__before && !window.__before.isConnected,
        null, { timeout: 5000 });
      ops.op1.feed.splice(ops.op1.feed.indexOf(probe), 1);
      await page.evaluate(() => globalThis.ObserverOperators.refresh());
      await page.waitForFunction(() => !document.querySelector('#observer-operators .op-entry[data-entry="z1"]'));
    }

    // A list read that FAILED says nothing about which operators exist, so it
    // must not touch the selection. It used to: `choose("")` ran whenever the
    // list did not hold the selected id, and a list left empty by a failed
    // read holds nothing -- so a read cut short wrote the empty id over the
    // reader's, and the next visit opened on the first operator in the list
    // instead. That is what a reload does to the read in flight, which is how
    // the floating dialog's harness hit it in 2 runs of 12.
    //
    // The first read of a page is the one that matters: it is the only read
    // that meets an empty list. Refusing that read is the regression. The
    // selection has to outlive a visit, so the operator it is on is one the
    // run has not deleted: all-ops, made above.
    await page.evaluate(() => { localStorage.setItem("claunch-observer-operator-dock", "open"); });
    await tab("all-ops").click();
    await page.waitForFunction(() => document.querySelector('#observer-operators .op-tab[aria-selected="true"]')?.textContent.includes("all-ops"));
    const key = () => page.evaluate(() => localStorage.getItem("claunch-observer-operator"));
    const kept = Object.values(ops).find(o => o.name === "all-ops").id;
    assert.equal(await key(), kept, "the pressed operator is the one kept");
    await page.route("**/api/operators", route => route.fulfill({ status: 500, body: '{"error":"boom"}' }));
    await page.reload();
    await page.waitForFunction(() => /operator 목록을 읽지 못했습니다/.test(document.querySelector("#observer-operators")?.textContent || ""));
    assert.equal(await key(), kept, "a refused list read leaves the reader's operator alone");
    await page.unroute("**/api/operators");
    await page.reload();
    await tab("all-ops").waitFor();
    assert.equal(await page.evaluate(() => document.querySelector('#observer-operators .op-tab[aria-selected="true"]')?.textContent),
      "all-ops", "and the next visit opens on it");

    assert.deepEqual(errors, []);
    console.log("PASS: threads list and conversation taking turns in the dock and on a phone, a row press, a message into the thread on screen, a new thread with a title and a staged image, operator tabs, seen badge, feed folding and kinds, markdown bodies and answers with raw HTML kept as text, approve/choice answers, composer target and send, card ask-about scope, picked empty target kept, fab hidden, session line outside the scroller with the session's state and link, create all/projects, validation and 409, scope edit, watch toggle, start/stop/delete, 40vh draft cap, dock closed by default, toggle kept in localStorage across reload, two columns with a held dock and its own input row, Ctrl+J newline and Enter send in the dock composer, ask opens the dock, inner scrollers only the draft and the dock conversation on desktop and none on mobile");
  } finally { await browser.close(); server.close(); server.closeAllConnections(); }
})().catch(error => { console.error(error); server.close(); server.closeAllConnections(); process.exitCode = 1; });
