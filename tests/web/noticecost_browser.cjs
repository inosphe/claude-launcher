/* What the notification channel costs a page, against a fixture daemon
   (claunch-l9znm).

   The question this answers is the one the issue was opened with: after
   every cflow gate that waits on a person was raised into the notice panel
   (claunch-whm4e.1), how much does that channel actually spend? Not "does
   it look expensive" — a number for localStorage writes and bytes, a number
   for DOM nodes added and removed, and the card ages held still.

   The comment on the issue carries the measurement this harness was made
   from (s5822, 2026-10-27). That measurement lived only in a session
   scratch directory, so a reviewer reproducing it had to copy the script
   out of a board comment; this file is the same measurement where the
   repository can run it.

   Three things are held:

   1. **A settled feed does not keep rewriting history.** Once the panel has
      drawn the pending gates, further polls of the same set write nothing
      to `claunch.noticeHistory`. Before claunch-qnq1c a pending row trimmed
      at the 100-row cap was re-recorded on the next poll, and with 133 asks
      waiting that was 33+ whole-history writes per feed.
   2. **A card's age does not restart.** The age a card shows is the run's
      own `opened` stamp, not when this browser first saw it, so it counts
      up across polls instead of falling back to 0s. Held as "the age is at
      least what the fixture's stamp implies", which is the shape the bug
      broke.
   3. **The steady state is bounded.** Over a window of polls the history
      stays at the pending set's size — `NOTICE_HISTORY_MAX` rows, plus the
      rows of requests still waiting that noticeTrim keeps past the cap —
      and does not climb with the poll count. The cards under #notices stay
      equal to the pending set's size for the same window.

   None of these three is first held here. `noticehistory_check.js` holds
   the same three against a sliced `app.js` and a fake DOM (the cap and the
   quiet feed at "more waiting requests than the cap", the opened stamp at
   "a gate's age is when it opened"), and those checks run without a
   browser. What this file adds is the layer: the real `app.js`, booted by
   the real page, polling on its own timer, writing the real localStorage,
   drawing into the real `#notices` — so the numbers below are what a
   browser actually spends rather than what the sliced functions do. A
   change that kept the unit checks green while the assembled page looped
   would land here.

   The fixture serves 133 pending gates on purpose: past the 100-row cap, so
   a trim-and-re-record loop has something to loop on. A smaller fixture
   would pass whether or not the loop is back. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");

const root = path.join(__dirname, "../../src/claude_launcher/web/static");

// Past NOTICE_HISTORY_MAX (100) by a third, so the cap actually bites.
const N = 133;
// How long the feed is polled after it has settled. The page polls on its
// own timer; this window only has to be longer than a couple of intervals.
const WINDOW_MS = 6000;

const nowSec = Date.now() / 1000;
// Each gate opened at a distinct age, oldest first, so a card whose age
// restarts is distinguishable from one that keeps counting.
const rows = {};
for (let i = 0; i < N; i += 1) {
  const s = `sess-${i}`;
  rows[s] = {
    cwd: `/w/${s}`, scope: s, run: `run-${i}`, workflow: "improv-worker-v2",
    status: "waiting_approval", gated: true, owed: ["ask: landing-review"],
    answers: [{
      kind: "ask", instance: "landing@3", ask: `ask2-${i}`, urgent: false,
      label: "landing-review", prompt: "land it?",
      // 30 minutes and older, on the daemon's clock
      opened_at: nowSec - (i + 1) * 60,
      options: [{ name: "approve", description: "" }, { name: "decline", description: "" }],
    }],
    urgent_asks: [],
  };
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    // No /api/batch: the page falls back to the individual endpoints, which
    // is the path this harness counts.
    if (url.pathname === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
    let data = {};
    if (url.pathname === "/api/daemon") data = { version: "fixture", boot_id: "noticecost" };
    else if (url.pathname === "/api/sessions") {
      data = {
        sessions: Object.keys(rows).map((n) => ({
          name: n, status: "busy", cwd: rows[n].cwd, harness: "claude",
        })),
        llm_configured: false,
      };
    } else if (url.pathname === "/api/cflow2/rail") data = { rows, now: Date.now() / 1000 };
    else if (url.pathname.endsWith("/capture")) data = { lines: [] };
    else if (/^\/api\//.test(url.pathname)) data = [];
    res.end(JSON.stringify(data));
    return;
  }
  const file = path.join(
    root, url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/static\//, "")
  );
  try {
    res.setHeader("Content-Type", file.endsWith(".js") ? "application/javascript"
      : file.endsWith(".css") ? "text/css" : "text/html");
    res.end(fs.readFileSync(file));
  } catch { res.statusCode = 404; res.end(); }
});

/* Seconds out of an age line: "<stamp> · <n>s ago" / "<n>m ago" / "<n>h<n>m ago". */
function ageSeconds(text) {
  const m = /·\s*(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?\s*ago/.exec(text || "");
  if (!m) return null;
  const [, h, min, s] = m;
  return (Number(h || 0) * 3600) + (Number(min || 0) * 60) + Number(s || 0);
}

(async () => {
  await safeListen(server);
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));

    // Count every write to the history key, and every mutation under the
    // two hosts the channel draws into.
    await page.addInitScript(() => {
      localStorage.setItem("claunch_token:/", "fixture-token");
      window.__cost = { writes: 0, bytes: 0, added: 0, removed: 0, records: 0 };
      const si = Storage.prototype.setItem;
      Storage.prototype.setItem = function (k, v) {
        if (k === "claunch.noticeHistory") {
          window.__cost.writes += 1;
          window.__cost.bytes += String(v).length;
        }
        return si.call(this, k, v);
      };
      // The panel's own repaint counter would be nice, but the contract this
      // file holds is about storage and nodes, which are observable without
      // reaching into the page's internals.
      document.addEventListener("DOMContentLoaded", () => {
        const mo = new MutationObserver((rs) => {
          for (const r of rs) {
            window.__cost.records += 1;
            window.__cost.added += r.addedNodes.length;
            window.__cost.removed += r.removedNodes.length;
          }
        });
        for (const sel of ["#notices", "#notice-panel-body"]) {
          const host = document.querySelector(sel);
          if (host) {
            mo.observe(host, {
              subtree: true, childList: true, attributes: true, characterData: true,
            });
          }
        }
      });
    });

    await page.goto(`http://127.0.0.1:${server.address().port}`, { waitUntil: "load" });

    // Wait for the feed to settle: cards drawn, and one poll's worth of
    // writes finished. Then zero the counters — from here on, anything
    // written is the loop this file is looking for.
    await page.waitForFunction(
      () => document.querySelectorAll("#notices .notice").length > 0,
      null, { timeout: 15000 }
    );
    await page.waitForTimeout(1500);

    const settled = await page.evaluate(() => ({
      cards: document.querySelectorAll("#notices .notice").length,
      ages: [...document.querySelectorAll("#notices .notice-when")]
        .slice(0, 5).map((n) => n.textContent.trim()),
      writes: window.__cost.writes,
      bytes: window.__cost.bytes,
    }));

    await page.evaluate(() => {
      window.__cost.writes = 0;
      window.__cost.bytes = 0;
      window.__cost.added = 0;
      window.__cost.removed = 0;
      window.__cost.records = 0;
    });

    // Let the page poll on its own timer, untouched, for the window.
    await page.waitForTimeout(WINDOW_MS);

    const after = await page.evaluate(() => ({
      writes: window.__cost.writes,
      bytes: window.__cost.bytes,
      added: window.__cost.added,
      removed: window.__cost.removed,
      records: window.__cost.records,
      cards: document.querySelectorAll("#notices .notice").length,
      ages: [...document.querySelectorAll("#notices .notice-when")]
        .slice(0, 5).map((n) => n.textContent.trim()),
      historyLen: (() => {
        try {
          const raw = localStorage.getItem("claunch.noticeHistory");
          const parsed = raw ? JSON.parse(raw) : [];
          return Array.isArray(parsed) ? parsed.length : -1;
        } catch { return -2; }
      })(),
    }));

    const report = {
      fixture_gates: N,
      settled_cards: settled.cards,
      settled_history_writes: settled.writes,
      settled_history_bytes: settled.bytes,
      window_ms: WINDOW_MS,
      window_history_writes: after.writes,
      window_history_bytes: after.bytes,
      window_dom_records: after.records,
      window_dom_added: after.added,
      window_dom_removed: after.removed,
      window_cards: after.cards,
      window_history_rows: after.historyLen,
      settled_ages: settled.ages,
      window_ages: after.ages,
    };
    console.log(JSON.stringify(report, null, 1));

    assert.deepEqual(errors, [], `page errors: ${errors.join(" | ")}`);

    // 1. A settled feed is quiet: no history write for the same pending set.
    assert.equal(
      after.writes, 0,
      `a settled feed rewrote claunch.noticeHistory ${after.writes} time(s) over `
      + `${WINDOW_MS}ms (${after.bytes} bytes) — the trim-and-re-record loop is back `
      + `(claunch-qnq1c). Cards: ${after.cards}`
    );

    // 2. The history stays bounded while the feed keeps polling.
    //
    //    The bound is the pending set plus the one row noticeTrim allows past
    //    the cap, and NOT the cap itself: noticeTrim deliberately keeps the
    //    row of a request that is still waiting (the other half of
    //    claunch-qnq1c). So 133 pending gates is 133 rows, and asserting 101
    //    here would be asserting away the fix this file guards. What must not
    //    happen is the row count climbing with the poll count.
    const bound = Math.max(N, 100) + 1;
    assert.ok(
      after.historyLen <= bound,
      `history grew to ${after.historyLen} rows, past ${bound} (the ${N} pending `
      + `gates plus the one row noticeTrim allows past its cap)`
    );
    assert.ok(
      after.historyLen > 0,
      `history is empty (${after.historyLen}) — nothing was recorded, so the quiet `
      + `feed above proves nothing`
    );

    // 3. A card's age is the run's stamp, and it keeps counting. The fixture
    //    opened these at least 60s ago and at most 133*60s ago; a card that
    //    restarted at 0s reads under 60.
    const ages = after.ages.map(ageSeconds).filter((v) => v !== null);
    assert.ok(ages.length > 0, `no age line could be read: ${JSON.stringify(after.ages)}`);
    for (const secs of ages) {
      assert.ok(
        secs >= 60,
        `a card reads "${secs}s ago" while every fixture gate opened at least 60s ago `
        + `— its age restarted instead of following the run's opened stamp `
        + `(claunch-l9znm). Ages: ${JSON.stringify(after.ages)}`
      );
    }

    // 4. The card count is the pending set's size, not the poll count.
    assert.equal(
      after.cards, settled.cards,
      `cards went ${settled.cards} -> ${after.cards} while the pending set stayed `
      + `${N} — something is adding a card per poll`
    );
    assert.equal(
      after.cards, N,
      `expected ${N} cards for ${N} pending gates, drew ${after.cards}`
    );

    console.log("notice cost: ok");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((err) => { console.error(err); process.exit(1); });
