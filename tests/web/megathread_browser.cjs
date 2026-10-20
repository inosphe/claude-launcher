/* The mega thread page (#/megathreads, megathread-page.js, claunch-v5nhp) in
   a real browser against a fixture daemon.

   Serves the real index.html and one cluster of two members, and checks:
     1. the rail has the entry right after Threads;
     2. the page draws one node per member around the user and opens it from
        the address;
     3. a message that the daemon answers "choose" opens the selection panel
        with both offers and their scores;
     4. ticking both sends the message plus exactly those thread ids;
     5. the delivery result is shown, not swallowed.

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

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
    if (url.pathname === "/api/sessions") {
      return res.end(JSON.stringify({ sessions: [{ name: "s1" }, { name: "s2" }] }));
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
