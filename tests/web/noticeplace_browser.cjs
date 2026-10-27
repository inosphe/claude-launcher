/* Where the corner notice strip lands, against the full shipped UI with a
   fixture daemon (claunch-lolft).

   The strip is fixed to the top-right corner, and that corner is where the
   page's own bars end: the session tabs wrap across the full width, the
   session header carries run/sims/category/qa/threads and the ⓘ details
   button to the right edge, and a phone puts its status line there instead.
   Pinned 12px from the top the first card sat on top of them, which is the
   one row a person keeps reaching for — so the report is real and the
   controls are blocked.

   What is held here is the invariant, measured in a real browser rather
   than read off the stylesheet: a card never starts above the bottom of any
   bar that is stacked against the top of the page. It is checked on a
   session page (tabs + header), with the tab row wrapping under a card
   that is already up (the ResizeObserver path), with the rows under the
   header — the timer line and the cflow answer line — filled in
   (claunch-i5lk4), and on a phone (the mobile bar, the header down). */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { safeListen } = require("./safe_listen.cjs");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");
// Eight is past a row's worth at 1440px: the tab bar wraps, which is the
// layout where a typed constant would go stale.
const sessions = Array.from({ length: 8 }, (_, i) => ({
  name: `s${i + 1}`, status: i === 7 ? "busy" : "exited",
  paused_at: i === 7 ? null : "2026-10-18T00:00:00Z", cwd: "/repo", harness: "codex",
}));

// The two rows the session screen stacks under its header, for the scenario
// that needs them (claunch-i5lk4): the session's timer line and the cflow
// answer line. Both are polled, so they are served from here rather than
// injected into the page — an injected strip would be wiped by the next poll.
const fixture = { timers: [], rail: {} };

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
    let data = {};
    if (url.pathname === "/api/daemon") data = { version: "fixture", boot_id: "test" };
    else if (url.pathname === "/api/sessions") data = { sessions, llm_configured: false };
    else if (url.pathname.endsWith("/capture")) data = { lines: ["Session snapshot"] };
    else if (/^\/api\/sessions\/[^/]+\/timers$/.test(url.pathname))
      data = { session: decodeURIComponent(url.pathname.split("/")[3]),
               timers: fixture.timers };
    else if (url.pathname === "/api/cflow2/rail") data = { rows: fixture.rail };
    else if (/^\/api\/(profiles|workspaces|mesh|cflow|harnesses|roles)$/.test(url.pathname)) data = [];
    res.end(JSON.stringify(data));
    return;
  }
  const file = path.join(root, url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/static\//, ""));
  try {
    res.setHeader("Content-Type", file.endsWith(".js") ? "application/javascript"
      : file.endsWith(".css") ? "text/css" : "text/html");
    res.end(fs.readFileSync(file));
  } catch { res.statusCode = 404; res.end(); }
});

(async () => {
  await safeListen(server);
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on("pageerror", e => errors.push(String(e)));
    await page.addInitScript(() => localStorage.setItem("claunch_token:/", "fixture-token"));
    const origin = `http://127.0.0.1:${server.address().port}`;
    await page.goto(origin);
    await page.locator(".session-row, #session-list li, #session-list a").first().waitFor()
      .catch(() => {});

    // Bars that own the top of the page, and the strip under them. A bar
    // counts only while it has a box: #term-header is up on a session page
    // and down everywhere else, the phone's bar is the other way round, and
    // the rows under the header (queued, window, timers, procs, cflow) are
    // each hidden until they have something to report.
    const measure = () => page.evaluate(() => {
      const box = (sel) => {
        const el = document.querySelector(sel);
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { top: r.top, bottom: r.bottom, height: r.height,
                 up: el.getClientRects().length > 0 && getComputedStyle(el).display !== "none" };
      };
      const bars = ["#session-tabs", "#term-header", "#mobile-top",
        "#term-queued", "#term-window", "#term-timers", "#term-procs", "#term-cflow"]
        .map(sel => ({ sel, ...box(sel) }))
        .filter(b => b.up && b.height > 0);
      const notices = box("#notices");
      const card = box("#notices .notice");
      return {
        bars, notices, card,
        cards: document.querySelectorAll("#notices .notice").length,
        cssTop: getComputedStyle(document.getElementById("notices")).top,
        details: box("#term-details"),
        tabsWrapped: (() => { const t = box("#session-tabs"); return !!t && t.height > 60; })(),
      };
    });
    const settle = () => page.evaluate(() => new Promise(done => {
      let n = 3; const step = () => (--n ? requestAnimationFrame(step) : done());
      requestAnimationFrame(step);
    }));

    // Routed to a session, on either layout: a phone hides the tab bar and
    // the session header, so the wait is on the route and the header's own
    // title, which both layouts fill in.
    const visit = async (name) => {
      await page.evaluate(n => { location.hash = `#/s/${n}`; }, name);
      await page.waitForFunction(n => location.hash === `#/s/${n}`
        && document.getElementById("term-title").textContent.includes(n), name);
      await settle();
    };

    // A sticky card is one that does not time out under the measurement.
    const showCard = async (title) => {
      await page.evaluate(t => notify(t, "measurement card", { sticky: true, key: "place-" + t }), title);
      await settle();
    };

    // The invariant of this harness, in one place: no card may start above
    // the bottom of any bar that is stacked against the top of the page.
    const assertClear = async (where) => {
      const m = await measure();
      assert(m.cards > 0, `${where}: a notice card is on screen`);
      assert(m.bars.length > 0, `${where}: at least one top bar is up`);
      for (const bar of m.bars) {
        assert(m.notices.top >= bar.bottom - 0.5,
          `${where}: the strip starts below ${bar.sel} `
          + `(strip ${m.notices.top}, bar bottom ${bar.bottom})`);
        assert(m.card.top >= bar.bottom - 0.5,
          `${where}: the card is drawn below ${bar.sel} `
          + `(card ${m.card.top}, bar bottom ${bar.bottom})`);
        assert(m.card.top < bar.bottom + 600,
          `${where}: the card still belongs to the top of the page`);
      }
      // The ⓘ details button floats in the header's last lane at right:14px
      // — the very corner the strip is pinned to. It is the control the
      // complaint named, so it is asserted on its own.
      if (m.details && m.details.up) {
        assert(m.card.top >= m.details.bottom - 0.5,
          `${where}: the card does not cover the details button`);
      }
      return m;
    };

    // --- a session page: tabs and header are both up ----------------------
    await visit("s1");
    let m = await measure();
    assert(m.bars.some(b => b.sel === "#term-header"), "the session header is up");
    assert(m.bars.some(b => b.sel === "#session-tabs"), "the tab bar is up");
    await showCard("first");
    m = await assertClear("session page");
    assert(m.notices.top >= m.bars.find(b => b.sel === "#term-header").bottom,
      "the strip sits under the header, not in its corner");
    assert.notEqual(m.cssTop, "12px", "the strip's top is measured, not the old constant");
    // The picture the complaint was made with: a card, the header under it.
    if (process.env.CLAUNCH_NOTICE_SCREENSHOT)
      await page.screenshot({ path: process.env.CLAUNCH_NOTICE_SCREENSHOT });

    // --- the rows the header stacks under itself --------------------------
    // The queued banner, the window row, the timer line and the cflow answer
    // line all sit on the header's bottom edge and carry presses of their
    // own, so a card placed just under the header lands on them
    // (claunch-i5lk4). The timer line and the cflow answer line are the two
    // the person reads there; the other rows use the same slot.
    const nowS = Math.round(Date.now() / 1000);
    fixture.timers = [{ id: "t1", source: "cflow2", kind: "wait", label: "await landing",
      state: "waiting", due: nowS + 600, since: nowS, scope: "s1@main" }];
    fixture.rail = { s1: {
      cwd: "/repo", scope: "default", run: "r-fixture", workflow: "improv-worker-v2",
      status: "waiting", current: ["round/item/work"], owed: ["ask: work"],
      gated: true, urgent_asks: [], inputs: {},
      answers: [{ kind: "ask", instance: "round/item/work", label: "work",
        prompt: "may the run land this branch?",
        options: [{ name: "approve" }, { name: "decline" }] }],
    } };
    await page.evaluate(() => Promise.all([refreshTermTimers(), refreshCflow2Rail()]));
    await settle();
    m = await measure();
    for (const sel of ["#term-timers", "#term-cflow"])
      assert(m.bars.some(b => b.sel === sel), `${sel} is up under the header`);
    await showCard("rows");
    const under = await assertClear("rows under the header");
    const lowest = under.bars.reduce((a, b) => (b.bottom > a.bottom ? b : a));
    assert.equal(lowest.sel, "#term-cflow", "the cflow answer line is the lowest row");
    assert(under.notices.top >= lowest.bottom,
      "the strip clears the cflow answer line, not just the header "
      + `(strip ${under.notices.top}, cflow bottom ${lowest.bottom})`);
    console.log(`rows under the header: strip ${under.notices.top}, `
      + `header ${under.bars.find(b => b.sel === "#term-header").bottom}, `
      + `timers ${under.bars.find(b => b.sel === "#term-timers").bottom}, `
      + `cflow ${lowest.bottom}`);
    // Back to an empty stack for the scenarios that follow: the rows are
    // polled, so they have to be cleared at the source.
    fixture.timers = []; fixture.rail = {};
    await page.evaluate(() => Promise.all([refreshTermTimers(), refreshCflow2Rail()]));
    await settle();
    m = await measure();
    assert.equal(m.bars.some(b => b.sel === "#term-cflow"), false,
      "the cflow line goes back down with no run waiting");
    // The other direction of the same mechanism (claunch-i5lk4.1). Going
    // down is proven above; coming back up is not the same witness. The
    // placement is re-measured because watchNoticeStrip observes every
    // named row, so a row that goes display: none — a reported size of 0 —
    // re-places a card that is already on screen. Without that observer the
    // card keeps the top it was given while the rows were up, and nothing
    // else in this harness would notice.
    const backUp = m.notices.top;
    assert(backUp < under.notices.top,
      "the strip comes back up when the rows go away "
      + `(strip ${backUp}, was ${under.notices.top} with the rows up)`);
    const headerBottom = m.bars.find(b => b.sel === "#term-header").bottom;
    // Back in the header's neighbourhood, not still hanging where the cflow
    // line was. A window rather than the exact gap: the register the strip
    // keeps under the last bar is app.js's to choose, and a test that pins
    // the number fails on a legitimate change to it.
    assert(backUp <= headerBottom + 24,
      "with the rows gone the strip sits back under the header "
      + `(strip ${backUp}, header bottom ${headerBottom})`);
    console.log(`rows gone again: strip ${backUp} (was ${under.notices.top}), `
      + `header ${headerBottom}`);

    // --- the session detail docked on the right (claunch-chwt1) ----------
    // On a wide screen the detail is the right-hand column of the layout,
    // and the strip is fixed to the viewport's right: with the rail up the
    // cards were drawn over its head. The strip moves to the rail's left
    // edge, follows the rail when its bar is dragged, and goes back to the
    // corner when the rail closes — all on a card that is already up.
    const side = () => page.evaluate(() => {
      const r = (sel) => {
        const el = document.querySelector(sel);
        if (!el || !el.getClientRects().length) return null;
        const b = el.getBoundingClientRect();
        return b.width > 0 ? { left: b.left, right: b.right } : null;
      };
      return { card: r("#notices .notice"), strip: r("#notices"),
               split: r("#detail-split"), rail: r("#sess-view.docked"),
               width: window.innerWidth };
    });
    const cornerRight = (await side()).strip.right;
    await page.evaluate(() => openDetail("s1"));
    await page.waitForFunction(() => {
      const v = document.getElementById("sess-view");
      return v.classList.contains("docked") && v.getClientRects().length > 0;
    });
    await settle();
    let d = await side();
    assert(d.split && d.rail, "the detail rail is docked on the right");
    assert(d.card.right <= d.split.left + 0.5,
      "the card stays left of the docked detail rail "
      + `(card right ${d.card.right}, rail starts ${d.split.left})`);
    assert(d.card.right >= d.split.left - 40,
      "the card sits against the rail, not somewhere in the middle "
      + `(card right ${d.card.right}, rail starts ${d.split.left})`);
    console.log(`detail docked: card right ${d.card.right}, rail starts ${d.split.left}`);
    // The drag bar writes --detail-w; a wider rail pushes the card along.
    const narrowLeft = d.split.left;
    await page.evaluate(() => document.getElementById("layout").style.setProperty("--detail-w", "520px"));
    await settle();
    d = await side();
    assert(d.split.left < narrowLeft - 100, "the rail widened under the card");
    assert(d.card.right <= d.split.left + 0.5,
      "the card follows a widened rail "
      + `(card right ${d.card.right}, rail starts ${d.split.left})`);
    await page.evaluate(() => document.getElementById("layout").style.removeProperty("--detail-w"));
    await page.evaluate(() => closeDetail());
    await page.waitForFunction(() =>
      document.getElementById("sess-view").getClientRects().length === 0);
    await settle();
    d = await side();
    assert(Math.abs(d.strip.right - cornerRight) <= 0.5,
      "with the rail closed the strip goes back to its corner "
      + `(strip right ${d.strip.right}, corner ${cornerRight})`);

    // --- the tab row wraps under a card that is already up ----------------
    // The bars move after the card is drawn, which is why the placement is
    // watched rather than computed once at show time: a narrower column
    // wraps the tabs while the card stays where it was put.
    await page.setViewportSize({ width: 850, height: 900 });
    for (const name of ["s2", "s3", "s4", "s5", "s6", "s7"]) await visit(name);
    m = await measure();
    const tabCount = await page.locator(".session-tab").count();
    assert(m.tabsWrapped, "the tab bar wrapped to a second row "
      + `(tabs ${tabCount}, bar height ${m.bars.find(b => b.sel === "#session-tabs")?.height})`);
    m = await assertClear("wrapped tabs");
    assert(m.notices.top >= m.bars.find(b => b.sel === "#session-tabs").bottom,
      "the strip follows the tab bar down when it wraps");

    // --- a page with no header: the strip keeps its corner ----------------
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.evaluate(() => { location.hash = "#/"; });
    await page.waitForFunction(() =>
      document.getElementById("term-header").getClientRects().length === 0);
    await showCard("home");
    const home = await assertClear("home page");
    assert.equal(home.bars.some(b => b.sel === "#term-header"), false,
      "the header is down on the home page");

    // --- a phone: the status line owns the top, the header is down --------
    await page.setViewportSize({ width: 390, height: 844 });
    await visit("s8");
    await showCard("phone");
    const phone = await assertClear("phone");
    assert(phone.bars.some(b => b.sel === "#mobile-top"), "the phone's status line is up");
    assert.equal(phone.bars.some(b => b.sel === "#term-header"), false,
      "the session header is down on a phone");

    // --- the package's own drawing stays put ------------------------------
    // The panel is hidden while the strip is up (claunch-k2khb) and this
    // change does not touch that, but a card that never came back would
    // pass every assertion above with an empty strip.
    assert(phone.cards > 0, "cards stay up through a viewport change");
    assert.deepEqual(errors, []);
    console.log("noticeplace browser checks passed");
  } finally {
    await browser.close();
    server.close();
  }
})().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
