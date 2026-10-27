/* The mega thread page (#/megathreads, megathread-page.js, claunch-v5nhp) in
   a real browser against a fixture daemon.

   Serves the real index.html and one cluster of two members, and checks:
     1. the rail has the entry right after Threads;
     2. the page draws one node per member around the user and opens it from
        the address;
     3. the member picker filters the session list, counts the ticks and says
        what the create button will do;
     4. a message that the daemon answers "choose" opens the selection panel
        with both offers and their scores;
     5. ticking both sends the message plus exactly those thread ids;
     6. the delivery result is shown, not swallowed.

   No live daemon is involved: /api/megathreads* answers from the fixture. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");

const root = path.join(__dirname, "../../src/claude_launcher/web/static");
const TYPES = { ".js": "application/javascript", ".css": "text/css", ".html": "text/html" };

const MEGA = {
  id: "mt-1",
  title: "frontend and deploy",
  status: "open",
  created_at: "2026-10-20T10:00:00+00:00",
  updated_at: "2026-10-20T10:00:00+00:00",
  sessions: 2,
  members: [
    { session: "s1", thread: "th-a", thread_title: "log format", status: "idle", unread: 0,
      added_by: "user", added_at: "2026-10-20T10:00:00+00:00" },
    { session: "s2", thread: "th-b", thread_title: "deploy", status: "busy", unread: 1,
      added_by: "user", added_at: "2026-10-20T10:00:00+00:00" },
  ],
};
const CANDIDATES = [
  { session: "s1", thread: "th-a", p_mine: 0.97, p_not_mine: 0.03, logit: 3.48, choice: "mine", confidence: 0.94 },
  { session: "s2", thread: "th-b", p_mine: 0.9, p_not_mine: 0.1, logit: 2.2, choice: "mine", confidence: 0.9 },
];
const posted = [];
let fleetReleased = false;
const fleetHeld = [];
function releaseFleet() {
  fleetReleased = true;
  for (const go of fleetHeld.splice(0)) go();
}
const drafts = [];

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
    // The page's own fleet read is the one with no `state` (every record,
    // archived included). It is held until the test lets it go, so the
    // check below can see the clusters drawn before the fleet has arrived
    // (claunch-wbs0i: on a live daemon that read took 1.5-2.3 s and the
    // whole page waited for it).
    if (url.pathname === "/api/sessions" && !url.searchParams.has("state") && !fleetReleased) {
      fleetHeld.push(() => server.emit("request", req, res));
      return;
    }
    if (url.pathname === "/api/sessions") {
      // The fields the rail poll really sends: branch and issue, no `task`,
      // and the two activity stamps the picker sorts by. `role` is left out
      // on purpose — the real rail sends it empty on every live row
      // (claunch-cn8v6), and the roster below is where it comes from.
      return res.end(JSON.stringify({ sessions: [
        { name: "s1", status: "idle", branch: "s1-log-format", project: "claunch",
          created_at: "2026-01-01T00:00:00Z", last_activity_at: "2026-01-02T00:00:00Z",
          briefing: { one_line: "로그 형식을 정한다", state: "working",
            now: "flow_report로 랜딩 상태를 확인하는 중" } },
        { name: "s2", status: "exited", paused_at: "2026-01-03T00:00:00Z",
          issue: "claunch-cn8v6", branch: "s2-deploy", project: "claunch",
          created_at: "2026-01-02T00:00:00Z", last_activity_at: "2026-01-03T00:00:00Z" },
      ] }));
    }
    // The two joins the picker makes: the mesh roster (role + mesh) and the
    // v2 run each session drives.
    if (url.pathname === "/api/mesh") {
      return res.end(JSON.stringify({ meshes: [{
        name: "claunch", project: "claunch",
        members: [{ session: "s1", role: "worker" }, { session: "s2", role: "reviewer" }],
      }] }));
    }
    if (url.pathname === "/api/cflow2/rail") {
      return res.end(JSON.stringify({ rows: {
        s2: { workflow: "improv-worker-v2", status: "running", current: ["work"], gated: false },
      } }));
    }
    if (url.pathname === "/api/megathreads/draft" && req.method === "POST") {
      let body = "";
      req.on("data", (chunk) => body += chunk);
      req.on("end", () => {
        drafts.push(JSON.parse(body || "{}"));
        return res.end(JSON.stringify({
          threshold: 2.197, model: "jev-latest", tokens: 420, scored: 2,
          members: [
            { session: "s2", thread: "", p_mine: 0.93, logit: 2.6, recommended: true },
            { session: "s1", thread: "", p_mine: 0.12, logit: -1.9, recommended: false },
          ],
          recommended: ["s2"], budget_used_tokens: 420, budget_remaining_tokens: 1999580,
        }));
      });
      return;
    }
    if (url.pathname === "/api/megathreads") {
      return res.end(JSON.stringify({ megathreads: [MEGA], total: 1 }));
    }
    if (url.pathname === "/api/megathreads/mt-1") {
      return res.end(JSON.stringify({ mega: MEGA }));
    }
    if (url.pathname === "/api/megathreads/mt-1/routes") {
      return res.end(JSON.stringify({ routes: [], total: 0 }));
    }
    if (url.pathname === "/api/megathreads/mt-1/route" && req.method === "POST") {
      let body = "";
      req.on("data", (chunk) => body += chunk);
      req.on("end", () => {
        const parsed = JSON.parse(body || "{}");
        posted.push(parsed);
        if (parsed.choose) {
          return res.end(JSON.stringify({
            verdict: "manual",
            delivered: parsed.choose.map((thread) => ({ thread, session: thread === "th-a" ? "s1" : "s2", delivered: true })),
            route: { verdict: "manual", tokens: 0, candidates: [], chosen: parsed.choose },
            budget_used_tokens: 900,
          }));
        }
        return res.end(JSON.stringify({
          verdict: "choose", threshold: 2.197, candidates: CANDIDATES,
          options: CANDIDATES, errors: [], tokens: 900, model: "jev-latest",
          delivered: [], budget_used_tokens: 900, budget_remaining_tokens: 9999100,
        }));
      });
      return;
    }
    res.statusCode = 404;
    return res.end("{}");
  }
  const rel = url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/static\//, "");
  const file = path.join(root, rel);
  try {
    const data = fs.readFileSync(file);
    res.setHeader("Content-Type", TYPES[path.extname(file)] || "application/octet-stream");
    res.end(data);
  } catch { res.statusCode = 404; res.end(); }
});

(async () => {
  const port = await safeListen(server);
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto(`http://127.0.0.1:${port}/#/megathreads`);
    await page.waitForFunction(() => document.querySelectorAll("#megathreads-view .mt-node").length === 2,
      null, { timeout: 15000 });

    // 1. The rail carries the entry right after Threads.
    const pages = await page.$$eval("#rail-nav a", (as) => as.map((a) => a.dataset.page));
    assert.equal(pages[pages.indexOf("threads") + 1], "megathreads");

    // 2. One node per member, labelled, around the user.
    const nodes = await page.$$eval("#megathreads-view .mt-node .mt-node-name", (els) => els.map((e) => e.textContent));
    assert.deepEqual(nodes.sort(), ["s1", "s2"]);
    assert.equal(await page.$$eval("#megathreads-view .mt-me-label", (els) => els.length), 1);
    // The table can change shape: one chip per member, and the sessions that
    // could join are offered.
    const chips = await page.$$eval("#megathreads-view .mt-chip-name", (els) => els.map((e) => e.textContent));
    assert.equal(chips.length, 2);
    // ...all of it before the fleet read has been answered: the clusters do
    // not wait on it. Then the fleet arrives and the page draws it in.
    for (let i = 0; i < 300 && !fleetHeld.length; i++) await new Promise((ok) => setTimeout(ok, 10));
    assert.ok(fleetHeld.length >= 1, "the page asks for the fleet after drawing the clusters");
    assert.deepEqual(await page.$$eval("#megathreads-view .mt-add-session option", (els) => els.length), 0);
    releaseFleet();
    await page.waitForFunction(() => document.querySelectorAll("#megathreads-view .mt-add-session option").length === 2,
      null, { timeout: 15000 });
    const joinable = await page.$$eval("#megathreads-view .mt-add-session option", (els) => els.map((e) => e.value));
    assert.deepEqual(joinable.sort(), ["s1", "s2"]);

    // 2b. The create panel opens over the page, and its picker filters,
    //     sorts, counts the ticks and says what the create button will do
    //     instead of going quiet.
    await page.click("#megathreads-view .mt-new-open");
    await page.waitForSelector("#megathreads-view .mt-create-card");
    const picker = "#megathreads-view .mt-create-card";
    const pickerNames = () => page.$$eval(`${picker} .mt-new-session-row .mt-new-name`,
      (els) => els.map((e) => e.textContent));
    // Newest activity first: s2 (01-03) before s1 (01-02).
    assert.deepEqual(await pickerNames(), ["s2", "s1"], "the default order is the newest activity");
    // The second line comes from fields the daemon really sends (branch,
    // issue): `task` is not in the rail poll, so a fixture that hands it
    // over would pass while every live row stayed blank.
    const pickerLines = await page.$$eval(`${picker} .mt-new-session-row .mt-new-line`,
      (els) => els.map((e) => e.textContent));
    assert.deepEqual(pickerLines, ["claunch-cn8v6 · s2-deploy", "s1-log-format"],
      `each row says what the session is on: ${JSON.stringify(pickerLines)}`);
    // The joins: role and mesh from the roster, the run from the cflow rail,
    // the briefing digest from the record.
    const metas = await page.$$eval(`${picker} .mt-new-session-row .mt-new-meta`,
      (els) => els.map((e) => e.textContent));
    assert.ok(metas[0].includes("reviewer") && metas[0].includes("claunch"), metas[0]);
    assert.ok(metas[0].includes("improv-worker-v2"), `the running cflow is on the row: ${metas[0]}`);
    const briefs = await page.$$eval(`${picker} .mt-new-session-row .mt-new-brief`,
      (els) => els.map((e) => e.textContent));
    assert.deepEqual(briefs, ["로그 형식을 정한다"], "the briefing digest is its own line");

    // The rows keep their own height when they outnumber the list. The list
    // is a fixed-height column flexbox, so a row that may shrink is squeezed
    // down to its 32px min-height and its meta/line/brief lines are drawn
    // over the next row (claunch-g07ra). Two rows do not fill a full-size
    // card, so the card is cut short until the list has to scroll.
    await page.$eval(picker, (card) => { card.style.height = "320px"; });
    const geometry = await page.$$eval(`${picker} .mt-new-session-row`, (rows) => rows.map((row) => ({
      top: row.getBoundingClientRect().top,
      bottom: row.getBoundingClientRect().bottom,
      height: row.clientHeight,
      content: row.scrollHeight,
    })));
    const list = await page.$eval(`${picker} .mt-create-list`, (el) => ({
      height: el.clientHeight, content: el.scrollHeight,
    }));
    assert.ok(list.content > list.height, `the list overflows and scrolls: ${JSON.stringify(list)}`);
    for (const row of geometry) {
      assert.ok(row.content <= row.height, `a row is as tall as what it holds: ${JSON.stringify(geometry)}`);
    }
    assert.ok(geometry[0].bottom <= geometry[1].top, `the rows do not overlap: ${JSON.stringify(geometry)}`);
    await page.$eval(picker, (card) => { card.style.height = ""; });

    // The lifecycle filter: s2 is paused, so only it survives.
    await page.click(`${picker} .mt-create-status[data-category="paused"]`);
    assert.deepEqual(await pickerNames(), ["s2"]);
    await page.click(`${picker} .mt-create-status[data-category="paused"]`);
    assert.deepEqual((await pickerNames()).sort(), ["s1", "s2"]);

    // The sort control: oldest activity first reverses the list.
    await page.selectOption(`${picker} .mt-create-sort`, "activity-asc");
    assert.deepEqual(await pickerNames(), ["s1", "s2"]);
    await page.selectOption(`${picker} .mt-create-sort`, "activity-desc");

    await page.fill(`${picker} .mt-new-filter`, "s2-deploy");
    assert.deepEqual(await pickerNames(), ["s2"], "the branch is searchable");
    await page.fill(`${picker} .mt-new-filter`, "s2");
    assert.deepEqual(await pickerNames(), ["s2"], "the filter narrows the list");
    await page.fill(`${picker} .mt-new-filter`, "no such session");
    assert.equal((await pickerNames()).length, 0);
    assert.ok((await page.textContent(`${picker} .mt-new-sessions`)).includes("거른 결과가 없습니다"));
    await page.fill(`${picker} .mt-new-filter`, "");
    assert.deepEqual((await pickerNames()).sort(), ["s1", "s2"]);

    // The Jev preview: type a topic, ask, and the recommended member comes
    // back ticked and flagged. The user edits from there.
    await page.fill(`${picker} .mt-create-topic-input`, "배포 로그 형식");
    await page.click(`${picker} .mt-create-draft`);
    await page.waitForSelector(`${picker} .mt-new-flag`);
    assert.equal(drafts.length, 1, "one preview request");
    assert.equal(drafts[0].topic, "배포 로그 형식");
    assert.deepEqual(drafts[0].sessions.sort(), ["s1", "s2"], "the preview scores what is on screen");
    assert.equal(await page.textContent(`${picker} .mt-new-count`), "1개 선택",
      "the recommendation arrives ticked");
    // Every scored member carries a flag; only the recommended one carries
    // the `추천` mark, and the rest are drawn weak (megathread-page.js: the
    // recommendation is the strong flag, a scored non-recommendation is
    // `mt-new-flag weak`). What this holds is that exactly one member is
    // recommended, not that only one member is scored.
    const flags = await page.$$eval(`${picker} .mt-new-flag`, (els) => els.map((e) => ({
      text: e.textContent, weak: e.classList.contains("weak"),
    })));
    const strong = flags.filter((f) => !f.weak);
    assert.equal(strong.length, 1, `only the scored recommendation is flagged strongly: ${JSON.stringify(flags)}`);
    assert.ok(strong[0].text.includes("추천"));
    assert.equal(flags.length, 2, `both scored members are flagged: ${JSON.stringify(flags)}`);
    assert.ok(flags.filter((f) => f.weak).every((f) => !f.text.includes("추천")),
      "a weak flag never claims a recommendation");
    assert.ok((await page.textContent(`${picker} .mt-create-draft-note`)).includes("1명을 추천"),
      "the panel says what the preview found");

    await page.check(`${picker} .mt-new-session[value="s1"]`);
    assert.equal(await page.textContent(`${picker} .mt-new-count`), "2개 선택");
    assert.equal(await page.isDisabled(`${picker} .mt-new-clear`), false, "clear is live once a row is ticked");
    await page.fill(`${picker} .mt-create-title`, "two of you");
    assert.equal(await page.textContent(`${picker} .mt-new-hint`), "2명으로 만듭니다");
    assert.equal(await page.isDisabled(`${picker} .mt-new-create`), false);
    await page.click(`${picker} .mt-new-clear`);
    assert.equal(await page.textContent(`${picker} .mt-new-count`), "0개 선택");
    assert.ok((await page.textContent(`${picker} .mt-new-hint`)).includes("빈"),
      "an empty pick is warned about");
    assert.equal(await page.isDisabled(`${picker} .mt-new-clear`), true);
    await page.click(`${picker} .mt-create-close`);
    assert.equal(await page.$$eval("#megathreads-view .mt-create-card", (els) => els.length), 0,
      "the panel closes");

    // 3. An ambiguous answer offers both members with their scores.
    await page.fill("#megathreads-view .mt-compose textarea", "both of you, the log line and the deploy");
    await page.click("#megathreads-view .mt-send");
    await page.waitForSelector("#megathreads-view .mt-options");
    const options = await page.$$eval("#megathreads-view .mt-option", (els) => els.map((e) => e.textContent));
    assert.equal(options.length, 2);
    assert.ok(options[0].includes("s1"));
    assert.ok(options[0].includes("logit"));
    const verdict = await page.textContent("#megathreads-view .mt-verdict");
    assert.ok(verdict.includes("2"), `the verdict says how many were offered: ${verdict}`);

    // 4. Ticking both sends the message and exactly those threads.
    const boxes = await page.$$("#megathreads-view .mt-option input");
    await boxes[0].check();
    await boxes[1].check();
    await page.waitForFunction(() => document.querySelector("#megathreads-view .mt-send").textContent.includes("선택한"));
    await page.click("#megathreads-view .mt-send");
    await page.waitForFunction(() => document.querySelector("#megathreads-view .mt-notice"));
    assert.deepEqual(posted[0], { text: "both of you, the log line and the deploy" });
    assert.deepEqual(posted[1], { text: "both of you, the log line and the deploy", choose: ["th-a", "th-b"] });

    // 5. The delivery result is shown.
    const notice = await page.textContent("#megathreads-view .mt-notice");
    assert.ok(notice.includes("s1") && notice.includes("전달됨"), notice);

    assert.deepEqual(errors, [], "the page raised nothing");
    console.log("megathread_browser ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((err) => { console.error(err); process.exit(1); });
