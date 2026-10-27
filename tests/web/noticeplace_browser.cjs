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
   that is already up (the ResizeObserver path), and on a phone (the mobile
   bar, the header down). */
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

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
    let data = {};
    if (url.pathname === "/api/daemon") data = { version: "fixture", boot_id: "test" };
    else if (url.pathname === "/api/sessions") data = { sessions, llm_configured: false };
    else if (url.pathname.endsWith("/capture")) data = { lines: ["Session snapshot"] };
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
    // and down everywhere else, and the phone's bar is the other way round.
    const measure = () => page.evaluate(() => {
      const box = (sel) => {
        const el = document.querySelector(sel);
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { top: r.top, bottom: r.bottom, height: r.height,
                 up: el.getClientRects().length > 0 && getComputedStyle(el).display !== "none" };
      };
      const bars = ["#session-tabs", "#term-header", "#mobile-top"]
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
