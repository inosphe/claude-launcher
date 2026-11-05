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

   Three things are held, and three premises that make them mean anything:

   0. **The window held polls.** The window ends when the page has received
      two rail answers, and the fixture's own count of what it served is
      reported beside it. Without this the quiet feed below is true of a
      stopped timer and of a lengthened poll interval alike — an empty
      window writes nothing no matter what the page does (s404-rev,
      s1262-rev).
   1. **A settled feed does not keep rewriting history.** Once the panel has
      drawn the pending gates, further polls of the same set write nothing
      to `claunch.noticeHistory`. Before claunch-qnq1c a pending row trimmed
      at the 100-row cap was re-recorded on the next poll, and with 133 asks
      waiting that was 33+ whole-history writes per feed.
   2. **And it leaves the DOM alone.** No node is added or removed under the
      two hosts the feed draws into. The counts were reported before without
      being asserted, so a page that detached and re-attached all 133 cards
      on every poll printed `added 133 / removed 133` and still exited 0
      (s490-rev).
   3. **The steady state is bounded — the history side.** Over the window
      the history stays at the pending set's size — `NOTICE_HISTORY_MAX`
      rows, plus the rows of requests still waiting that noticeTrim keeps
      past the cap — and does not climb with the poll count.
   4. **A card's age does not restart.** The age a card shows is the run's
      own `opened` stamp, not when this browser first saw it, so it counts
      up across polls instead of falling back to 0s. Held as "the age is at
      least what the fixture's stamp implies", which is the shape the bug
      broke.
   5. **The steady state is bounded — the card side.** The cards under
      #notices stay equal to the pending set's size, so nothing adds a card
      per poll.

   The first, third and fourth are not first held here. `noticehistory_check.js`
   holds them against a sliced `app.js` and a fake DOM (the cap and the
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
// The fewest polls a window can hold and still describe a working feed.
const MIN_WINDOW_POLLS = 2;
// How long the harness waits for those polls.
//
// The window ends when the page has received MIN_WINDOW_POLLS rail answers,
// not after a fixed duration. Both halves of that matter: a window spanning
// no tick writes nothing whatever the page does, so it proves nothing, and
// a window padded long enough to span two ticks spends its whole length
// every run — 20s of the 60s `test_browser_harness` allows each harness —
// even when the feed is healthy. Waiting for the answers costs two poll
// intervals (10s at the `DASHBOARD_POLL_MS` of 5000 in app.js today), and a
// page whose interval grows past this budget fails here rather than
// quietly describing a shorter window than it claims.
const POLL_WAIT_MS = 30000;

// Every /api/cflow2/rail answer the fixture serves, so the window can be
// described in polls rather than assumed to have had any.
let railRequests = 0;

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
      // One minute to N minutes old, on the daemon's clock. Every one of
      // them is past the 60s floor the age assertion below uses.
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
    } else if (url.pathname === "/api/cflow2/rail") {
      railRequests += 1;
      data = { rows, now: Date.now() / 1000 };
    }
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
      window.__cost = { writes: 0, bytes: 0, added: 0, removed: 0, records: 0, polls: 0 };
      // Count the rail answers the page receives. The window ends on this
      // counter, so a page that stopped polling never reaches it: the wait
      // times out and says what it saw instead of describing an empty
      // window as a quiet one.
      const realFetch = window.fetch;
      window.fetch = async function (...args) {
        const resp = await realFetch.apply(window, args);
        try {
          const first = args[0];
          const target = new URL(typeof first === "string" ? first : first.url, location.href);
          if (target.pathname === "/api/cflow2/rail") window.__cost.polls += 1;
        } catch { /* not a request this file reads */ }
        return resp;
      };
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
      window.__cost.polls = 0;
    });
    // Counted on the server, so it cannot be reset from inside the page —
    // record where the counter stood when the window opened and subtract.
    // This is the fixture's count; the window itself ends on the page's, and
    // the two are reported side by side.
    const railAtWindowStart = railRequests;

    // Let the page poll on its own timer, untouched, until it has received
    // the answers the window needs. The wait is the premise: when it fails,
    // nothing below has been measured, so it says so instead of reporting
    // the quiet feed of a page that stopped polling.
    const windowStarted = Date.now();
    try {
      await page.waitForFunction(
        (n) => window.__cost.polls >= n, MIN_WINDOW_POLLS, { timeout: POLL_WAIT_MS },
      );
    } catch {
      throw new Error(
        `the page received fewer than ${MIN_WINDOW_POLLS} rail answer(s) within `
        + `${POLL_WAIT_MS}ms (the fixture served ${railRequests - railAtWindowStart}) `
        + `— the feed stopped polling, so nothing below would have been measured `
        + `over a working feed. The page polls every DASHBOARD_POLL_MS; if that `
        + `interval grew past ${POLL_WAIT_MS / MIN_WINDOW_POLLS}ms, raise POLL_WAIT_MS`
      );
    }
    // One paint after the last answer: the poll that ends the window has to
    // have drawn, or a page that rebuilds the list on every poll could
    // change the DOM after this harness stopped looking.
    await page.evaluate(
      () => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))
    );

    const windowMs = Date.now() - windowStarted;
    const windowPolls = railRequests - railAtWindowStart;
    const after = await page.evaluate(() => ({
      polls: window.__cost.polls,
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
      window_wait_ms: windowMs,
      window_polls: after.polls,
      window_fixture_polls: windowPolls,
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

    // 0. The window actually held polls. The wait above ends on the page's
    //    own count; this reads that count where every other number below is
    //    read, so a window with no poll in it cannot be described as a quiet
    //    one. A window with no poll in it writes nothing whatever the page
    //    does, so "no writes" would be true of a broken page, a stopped
    //    timer and a changed interval alike.
    assert.ok(
      after.polls >= MIN_WINDOW_POLLS,
      `the window held ${after.polls} rail answer(s), fewer than the `
      + `${MIN_WINDOW_POLLS} this file needs — nothing below was measured over `
      + `a working feed. The page polls every DASHBOARD_POLL_MS; if that `
      + `interval grew past ${POLL_WAIT_MS / MIN_WINDOW_POLLS}ms, raise POLL_WAIT_MS`
    );

    // 1. A settled feed is quiet: no history write for the same pending set.
    assert.equal(
      after.writes, 0,
      `a settled feed rewrote claunch.noticeHistory ${after.writes} time(s) over `
      + `${windowMs}ms / ${after.polls} poll(s) (${after.bytes} bytes) — the `
      + `trim-and-re-record loop is back (claunch-qnq1c). Cards: ${after.cards}`
    );

    // 2. And it leaves the DOM alone. The counts above are reported either
    //    way, so without this they would look like evidence while asserting
    //    nothing: a page that detached and re-attached all 133 cards on every
    //    poll prints added 133 / removed 133 and still exits 0 (s490-rev).
    assert.equal(
      after.added, 0,
      `${after.added} node(s) were added under #notices/#notice-panel-body over `
      + `${after.polls} poll(s) of an unchanged pending set — the feed is `
      + `rebuilding what did not change (${after.records} mutation record(s))`
    );
    assert.equal(
      after.removed, 0,
      `${after.removed} node(s) were removed under #notices/#notice-panel-body `
      + `over ${after.polls} poll(s) of an unchanged pending set — the feed is `
      + `tearing down and redrawing (${after.records} mutation record(s))`
    );

    // 3. The history stays bounded while the feed keeps polling.
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

    // 4. A card's age is the run's stamp, and it keeps counting. The fixture
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

    // 5. The card count is the pending set's size, not the poll count.
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
