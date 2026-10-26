/* The Stats page (#/stats) in a real browser: the fleet overview, the detail
   panel and the controls that change what the overview asks for.

   Serves the real index.html against a fixture: /api/sessions answers a rail,
   /api/stats/overview answers a window payload whose rows depend on the
   window and on whether killed/archived sessions were asked for, and
   /api/sessions/<name>/stats answers one session's transcript reading. It
   checks:
     1. the default page is the fleet, with one row per metered session;
     2. killed/archived rows appear only when the box is ticked, and the box
        reaches the request (?killed=1 / ?archived=1);
     3. the window tabs ask for today / week / month;
     4. the provider strip narrows the table to that provider's sessions;
     5. clicking a row opens the detail panel, which asks that session's
        transcript reading and shows the window's provider change;
     6. closing the panel goes back to the fleet. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");

const now = Date.now();
const iso = (h) => new Date(now - h * 3600000).toISOString();

const SESSIONS = [
  { name: "alpha", status: "running", profile: "ds4:claude", harness: "claude",
    created_at: iso(3), project: "default", cwd: "/w/repo1" },
  { name: "beta", status: "running", profile: "kimi:claude", harness: "claude",
    created_at: iso(5), project: "default", cwd: "/w/repo1" },
  { name: "gone", status: "exited", exit_code: 143, profile: "nc:claude",
    harness: "claude", created_at: iso(9), exited_at: iso(2),
    project: "default", cwd: "/w/repo2" },
];

const provider = (name, total, requests) => ({
  provider: name, input: Math.round(total * 0.1), cache_read: Math.round(total * 0.9),
  cache_write: 0, output: 0, total, requests,
});

function row(name, providerName, total, requests, extra) {
  return {
    session: name, status: "running", category: "running", harness: "claude",
    profile: `${providerName}:claude`, borrow: "", issue: "", project: "default",
    source: "ledger", provider: providerName, model: "m-1",
    providers: [provider(providerName, total, requests)],
    input: Math.round(total * 0.1), cache_read: Math.round(total * 0.9),
    cache_write: 0, output: 0, total, requests,
    first_at: iso(3), last_at: iso(0.2), days: {}, switches: [],
    declared_provider: providerName, ended: false,
    ...extra,
  };
}

const ALPHA = row("alpha", "fireworks", 3000, 6, {
  switches: [{ at: iso(2), kind: "provider", from: "kimi", to: "fireworks",
               from_model: "k3", to_model: "m-1" }],
});
const BETA = row("beta", "kimi", 1000, 2, { borrow: "ds4" });
const GONE = row("gone", "fireworks", 500, 1, {
  status: "exited", category: "killed", ended: true,
});
const OLD = row("old", "kimi", 200, 1, {
  status: "exited", category: "archived", ended: true,
});

/* The killed and archived rows answer their own flag, so a page that asks for
   one of them gets exactly that row. */
const overview = (url) => {
  const win = url.searchParams.get("window") || "today";
  const killed = url.searchParams.get("killed") === "1";
  const archived = url.searchParams.get("archived") === "1";
  const ended = killed || archived;
  const factor = win === "today" ? 1 : win === "week" ? 2 : 3;
  const scale = (r) => ({ ...r, total: r.total * factor,
    requests: r.requests * factor,
    providers: r.providers.map((p) => ({ ...p, total: p.total * factor,
      requests: p.requests * factor })) });
  const rows = [ALPHA, BETA].map(scale);
  if (killed) rows.push(scale(GONE));
  if (archived) rows.push(scale(OLD));
  const total = rows.reduce((n, r) => n + r.total, 0);
  const byProvider = {};
  for (const r of rows) {
    for (const p of r.providers) {
      byProvider[p.provider] ||= { provider: p.provider, sessions: 0, total: 0, requests: 0 };
      byProvider[p.provider].sessions++;
      byProvider[p.provider].total += p.total;
      byProvider[p.provider].requests += p.requests;
    }
  }
  return {
    window: win, unit: "day", days: [],
    since: new Date(now).toISOString(), until: new Date(now).toISOString(),
    zone: "+0000", include_ended: ended,
    include_killed: killed, include_archived: archived,
    totals: { input: 0, cache_read: 0, cache_write: 0, output: 0, total,
              requests: rows.reduce((n, r) => n + r.requests, 0) },
    cache_share: 0.9,
    providers: Object.values(byProvider).sort((a, b) => b.total - a.total),
    sessions: rows,
    counts: { sessions: rows.length, metered: rows.length, unmetered: 0,
              killed_excluded: killed ? 0 : 1, archived_excluded: archived ? 0 : 1 },
    ledger_since: null, partial: false,
  };
};

const reading = {
  session: "alpha", available: true, harness: "claude", unit: "day",
  utc_offset: "+0000", since: iso(5),
  totals: { input: 10, cache_read: 90, cache_write: 0, output: 10, total: 110,
            requests: 2, subagent_requests: 0 },
  buckets: [
    { start: new Date(now - 86400000).toISOString(), input: 5, cache_read: 45,
      cache_write: 0, output: 5, total: 55, requests: 1,
      inputs: { human: 1, daemon: 0, mesh: 0, opening: 0, harness: 0 } },
    { start: new Date(now).toISOString(), input: 5, cache_read: 45,
      cache_write: 0, output: 5, total: 55, requests: 1,
      inputs: { human: 0, daemon: 1, mesh: 0, opening: 0, harness: 0 } },
  ],
  sources: [
    { category: "human", messages: 1, tokens: 10, carry: 0, requests: 1,
      triggered: { total: 55 }, share: { messages: 0.5, tokens: 0.5,
        triggered: 0.5, carry: 0.5 }, kinds: [] },
  ],
  senders: [],
  unattributed: { requests: 0, triggered: { total: 0 } },
  compactions: 0,
};

const asked = [];
const TYPES = { ".js": "application/javascript", ".css": "text/css", ".html": "text/html" };
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    asked.push(url.pathname + url.search);
    if (url.pathname === "/api/sessions") {
      return res.end(JSON.stringify({ sessions: SESSIONS }));
    }
    if (url.pathname === "/api/stats/overview") {
      return res.end(JSON.stringify(overview(url)));
    }
    if (/^\/api\/sessions\/[^/]+\/stats$/.test(url.pathname)) {
      return res.end(JSON.stringify({ ...reading,
        session: decodeURIComponent(url.pathname.split("/")[3]) }));
    }
    res.statusCode = 404;
    return res.end("{}");
  }
  const rel = url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/static\//, "");
  const file = path.join(root, rel);
  try {
    const body = fs.readFileSync(file);
    res.setHeader("Content-Type", TYPES[path.extname(file)] || "application/octet-stream");
    res.end(body);
  } catch { res.statusCode = 404; res.end(); }
});

const names = (page) => page.$$eval("#stats-view .stats-row",
  (rows) => rows.map((r) => r.dataset.session));

(async () => {
  const port = await safeListen(server);
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1400, height: 950 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto(`http://127.0.0.1:${port}/#/stats`);
    await page.waitForFunction(() =>
      document.querySelectorAll("#stats-view .stats-row").length >= 2, null, { timeout: 15000 });

    // 1. The default page is the fleet, not one session.
    assert.deepEqual(await names(page), ["alpha", "beta"]);
    assert.ok(asked.some((u) => u.startsWith("/api/stats/overview?window=today&killed=0&archived=0")),
      asked.join(" | "));
    assert.ok(!asked.some((u) => u.includes("/stats?unit=")),
      "no per-session reading is fetched for the fleet view");
    const bars = await page.$$eval("#stats-view .stats-rowbar-fill",
      (els) => els.map((e) => e.style.width));
    assert.deepEqual(bars.map((b) => parseFloat(b)).map((n) => Math.round(n * 100) / 100),
      [100, 33.33]);
    const borrow = await page.$eval("#stats-view .stats-row[data-session='beta'] .stats-borrow",
      (e) => e.textContent);
    assert.equal(borrow, "borrow ds4");

    // 2. The killed session is held out until its own box is ticked.
    await page.check("#stats-view .stats-ended input[data-ended='killed']");
    await page.waitForFunction(() =>
      Array.from(document.querySelectorAll("#stats-view .stats-row"))
        .some((r) => r.dataset.session === "gone"));
    assert.deepEqual(await names(page), ["alpha", "beta", "gone"]);
    assert.ok(asked.some((u) => u.startsWith("/api/stats/overview?window=today&killed=1&archived=0")),
      asked.join(" | "));
    const ended = await page.$eval("#stats-view .stats-row[data-session='gone'] .stats-status-cell",
      (e) => ({ text: e.textContent, ended: e.classList.contains("ended") }));
    assert.deepEqual(ended, { text: "killed", ended: true });

    // 2b. Archived sessions have their own box, separate from killed ones.
    await page.check("#stats-view .stats-ended input[data-ended='archived']");
    await page.waitForFunction(() =>
      Array.from(document.querySelectorAll("#stats-view .stats-row"))
        .some((r) => r.dataset.session === "old"));
    assert.deepEqual(await names(page), ["alpha", "beta", "gone", "old"]);
    assert.ok(asked.some((u) => u.startsWith("/api/stats/overview?window=today&killed=1&archived=1")),
      asked.join(" | "));
    await page.uncheck("#stats-view .stats-ended input[data-ended='archived']");
    await page.waitForFunction(() => {
      const rows = Array.from(document.querySelectorAll("#stats-view .stats-row"));
      return rows.length === 3 && !rows.some((r) => r.dataset.session === "old");
    });
    assert.deepEqual(await names(page), ["alpha", "beta", "gone"]);

    // 3. The window tabs ask for the window.
    await page.click("#stats-view .stats-window button[data-window='week']");
    await page.waitForFunction(() =>
      Array.from(document.querySelectorAll("#stats-view .stats-window button"))
        .some((b) => b.dataset.window === "week" && b.classList.contains("active")));
    await page.waitForFunction(() => {
      const el = document.querySelector("#stats-view .stats-row[data-session='alpha'] .stats-rowvalue");
      return el && /^6(\.00)?k$/i.test(el.textContent.trim());
    });
    assert.ok(asked.some((u) => u.startsWith("/api/stats/overview?window=week&killed=1&archived=0")),
      asked.join(" | "));

    // 4. The provider strip narrows the table.
    await page.click("#stats-view .stats-provider[data-provider='kimi']");
    await page.waitForFunction(() =>
      document.querySelectorAll("#stats-view .stats-row").length === 1);
    assert.deepEqual(await names(page), ["beta"]);
    const pressed = await page.$eval("#stats-view .stats-provider[data-provider='kimi']",
      (e) => e.classList.contains("active"));
    assert.equal(pressed, true);
    await page.click("#stats-view .stats-provider[data-provider='kimi']");
    await page.waitForFunction(() =>
      document.querySelectorAll("#stats-view .stats-row").length === 3);

    // 5. A row opens the detail panel, which reads that session.
    await page.click("#stats-view .stats-row[data-session='alpha']");
    await page.waitForFunction(() => {
      const panel = document.querySelector("#stats-view .stats-detail");
      return panel && panel.querySelectorAll(".stats-area").length >= 4;
    }, null, { timeout: 15000 });
    assert.ok(asked.some((u) => u === "/api/sessions/alpha/stats?unit=day"),
      asked.join(" | "));
    const panel = await page.$eval("#stats-view .stats-detail", (e) => ({
      head: e.querySelector("h3").textContent,
      switchText: (e.querySelector(".stats-switch") || {}).textContent || "",
      showsChart: e.querySelectorAll(".stats-area").length,
      open: (e.querySelector(".stats-detail-open") || {}).getAttribute?.("href"),
    }));
    assert.equal(panel.head, "alpha");
    assert.match(panel.switchText, /provider/);
    assert.match(panel.switchText, /kimi → fireworks/);
    assert.ok(panel.showsChart >= 4, "the transcript reading is drawn in the panel");
    assert.equal(new URL(panel.open, "http://x").hash, "#/s/alpha");
    assert.match(page.url(), /#\/stats\/alpha$/);
    if (process.env.STATS_DETAIL_SHOT) {
      await page.screenshot({ path: process.env.STATS_DETAIL_SHOT, fullPage: true });
    }

    // 6. Closing the panel goes back to the fleet.
    await page.click("#stats-view .stats-detail-head button");
    await page.waitForFunction(() =>
      document.querySelectorAll("#stats-view .stats-detail").length === 0);
    assert.match(page.url(), /#\/stats$/);
    assert.ok((await names(page)).length === 3);

    // 7. On a window too small for the page, the page scrolls down inside
    //    its own view and the table scrolls sideways inside its own box:
    //    the view had no rule of its own, so it neither scrolled nor kept
    //    its padding, and #main cut off both edges (claunch-8m2hy).
    await page.setViewportSize({ width: 480, height: 420 });
    await page.waitForFunction(() =>
      document.getElementById("stats-view").getBoundingClientRect().bottom <= window.innerHeight);
    const box = await page.evaluate(() => {
      const view = document.getElementById("stats-view");
      const scroll = view.querySelector(".stats-main .stats-scroll");
      const head = view.querySelector(".wf-head");
      view.scrollTop = 1e6;
      scroll.scrollLeft = 1e6;
      const v = view.getBoundingClientRect();
      const s = scroll.getBoundingClientRect();
      const ths = scroll.querySelectorAll("th");
      const last = ths[ths.length - 1].getBoundingClientRect();
      return {
        overflowY: getComputedStyle(view).overflowY,
        scrolledDown: view.scrollTop,
        viewBottom: v.bottom, windowHeight: window.innerHeight,
        viewWide: view.scrollWidth - view.clientWidth,
        pageWide: document.documentElement.scrollWidth - window.innerWidth,
        tableWide: scroll.scrollWidth - scroll.clientWidth,
        lastRight: last.right, boxRight: s.right,
        headInset: head.getBoundingClientRect().left - v.left,
      };
    });
    assert.ok(["auto", "scroll"].includes(box.overflowY), JSON.stringify(box));
    assert.ok(box.scrolledDown > 0, "the view scrolls down: " + JSON.stringify(box));
    assert.ok(box.viewBottom <= box.windowHeight, "the view ends inside the window: " + JSON.stringify(box));
    assert.ok(box.viewWide <= 0 && box.pageWide <= 0, "nothing widens the page: " + JSON.stringify(box));
    assert.ok(box.tableWide > 0, "the table scrolls inside its box: " + JSON.stringify(box));
    assert.ok(box.lastRight <= box.boxRight + 1, "the last column is reachable: " + JSON.stringify(box));
    assert.ok(box.headInset > 0, "the view keeps its padding: " + JSON.stringify(box));
    await page.setViewportSize({ width: 1400, height: 950 });

    assert.deepEqual(errors, [], "page errors: " + errors.join(" | "));
    if (process.env.STATS_SHOT) {
      await page.screenshot({ path: process.env.STATS_SHOT, fullPage: true });
    }
    console.log("stats_browser: all checks passed");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
