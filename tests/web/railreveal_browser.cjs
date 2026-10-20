/* `⇱ card` lands on the row it names, on a rail long enough to scroll
   (claunch-ayaig).

   The rail skips layout for rows scrolled out of view (claunch-oo8au.1), so a
   row below the fold answers with contain-intrinsic-size's fallback -- 64px,
   against rows that measure 136px here -- until it renders. A scroll started
   at that moment is aimed with the guesses: the press put the row 5,928px
   below the rail's top (ten rail-heights, out of view) on a 200-row rail, and
   a second press was still 2,142px off, because the smooth scroll commits to
   the position it measured before the rows it passes have rendered and
   changed height. The press now lays the rail out for one turn first
   (layRailOut), and this is that claim measured in a browser: the row ends up
   centred, and the skip is back afterwards.

   A stub DOM cannot carry this check -- gotocard_check.js stubs
   scrollIntoView, so it sees the call and not where the browser lands. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require(process.env.CLAUNCH_PLAYWRIGHT || "playwright");
const root = path.join(__dirname, "../../src/claude_launcher/web/static");

const ROWS = 200;
const TARGET = `s${ROWS - 20}`;            // twenty rows above the foot

const sessions = [{ name: "attached", status: "busy", cwd: "/repo", harness: "claude",
                    model: "opus", conversation_id: "a" }];
for (let i = 0; i < ROWS; i++) {
  sessions.push({ name: `s${String(i).padStart(3, "0")}`, status: "idle", cwd: "/repo",
                  harness: "claude", model: "opus" });
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://fixture");
  if (url.pathname.startsWith("/api/")) {
    res.setHeader("Content-Type", "application/json");
    if (url.pathname === "/api/batch") { res.statusCode = 404; return res.end("{}"); }
    let data = {};
    const meta = url.pathname.match(/^\/api\/sessions\/([^/]+)\/meta$/);
    if (url.pathname === "/api/daemon") data = { version: "fixture", boot_id: "test" };
    else if (url.pathname === "/api/sessions") data = { sessions, llm_configured: true };
    else if (meta) data = { session: sessions.find((s) => s.name === decodeURIComponent(meta[1])) || {} };
    else if (url.pathname.endsWith("/capture")) data = { lines: [] };
    else if (/^\/api\/(profiles|workspaces|mesh|cflow|harnesses|roles)$/.test(url.pathname)) data = [];
    res.end(JSON.stringify(data));
    return;
  }
  const file = path.join(root, url.pathname === "/" ? "index.html"
    : url.pathname.replace(/^\/static\//, ""));
  try {
    res.setHeader("Content-Type", file.endsWith(".js") ? "application/javascript"
      : file.endsWith(".css") ? "text/css" : "text/html");
    res.end(fs.readFileSync(file));
  } catch { res.statusCode = 404; res.end(); }
});

/* Where the row sits in the rail, in the rail's own coordinates: `error` is
   how far its centre is from the rail's centre (0 = what the press asks
   for), `inView` is whether a reader can see it at all. */
const landing = (page, name) => page.evaluate((name) => {
  const list = document.querySelector("#session-list");
  const row = list.querySelector(`li[data-name="${CSS.escape(name)}"]`);
  if (!row) return { found: false };
  const lr = list.getBoundingClientRect();
  const rr = row.getBoundingClientRect();
  return {
    found: true,
    error: Math.round((rr.top + rr.height / 2) - (lr.top + lr.height / 2)),
    inView: rr.top >= lr.top - 1 && rr.bottom <= lr.bottom + 1,
    railHeight: Math.round(lr.height),
    rowHeight: Math.round(rr.height),
    scrollHeight: list.scrollHeight,
  };
}, name);

(async () => {
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.addInitScript(() => {
      localStorage.setItem("claunch_token:/", "fixture-token");
      // The poll stays out of the measurement: one boot poll, then nothing
      // moves but the press.
      window.setInterval = () => 0;
    });
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.locator(`#session-list li.sess-card[data-name="${TARGET}"]`).waitFor();

    // A rail that has to scroll, and a target below the fold: otherwise the
    // press proves nothing.
    const rail = await page.evaluate(() => {
      const list = document.querySelector("#session-list");
      const rows = [...list.querySelectorAll("li[data-name]")];
      return { count: rows.length, scrollHeight: list.scrollHeight,
               clientHeight: list.clientHeight };
    });
    assert.ok(rail.scrollHeight > rail.clientHeight * 3,
      `the rail has to scroll for this check to mean anything (${rail.scrollHeight} vs ${rail.clientHeight})`);

    // The press, then let the smooth scroll finish.
    const answered = await page.evaluate(async (name) => {
      const ok = revealSessionCard(name);
      await new Promise((r) => setTimeout(r, 2500));
      return ok;
    }, TARGET);
    assert.equal(answered, true, "the press answers that it found the row");

    const landed = await landing(page, TARGET);
    assert.equal(landed.found, true, "the row exists after the press");
    assert.ok(landed.inView, `the row is on screen (error ${landed.error}px, rail ${landed.railHeight}px)`);
    // Centred within half a rail: a row brought to the very edge is on screen
    // and still reads as not found.
    assert.ok(Math.abs(landed.error) < landed.railHeight / 2,
      `the row is centred, not merely in view (error ${landed.error}px, rail ${landed.railHeight}px)`);

    // The skip the press borrowed is given back, so the poll keeps the
    // rendering win it was bought for (claunch-oo8au.1).
    const handedBack = await page.evaluate(async () => {
      await new Promise((r) => setTimeout(r, 800));
      const list = document.querySelector("#session-list");
      const row = list.querySelector("li[data-name]");
      return { classed: list.classList.contains("rail-laid-out"),
               visibility: getComputedStyle(row).contentVisibility };
    });
    assert.equal(handedBack.classed, false, "the rail is not left laid out");
    assert.equal(handedBack.visibility, "auto", "the rows skip their layout again");

    // And it stays where it landed once the skip is back.
    const after = await landing(page, TARGET);
    assert.ok(after.inView, `the row is still on screen after the rail is handed back (error ${after.error}px)`);

    assert.deepEqual(errors, [], "no page errors");

    // The regression itself, on a page where no row has been laid out yet:
    // the bare scroll the press used to do, aimed with the rail's estimates.
    // Without this the check above could pass on a rail that had simply been
    // scrolled once before.
    const bare = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    await bare.addInitScript(() => {
      localStorage.setItem("claunch_token:/", "fixture-token");
      window.setInterval = () => 0;
    });
    await bare.goto(`http://127.0.0.1:${server.address().port}`);
    await bare.locator(`#session-list li.sess-card[data-name="${TARGET}"]`).waitFor();
    const estimated = await bare.evaluate(async (name) => {
      const list = document.querySelector("#session-list");
      const row = list.querySelector(`li[data-name="${CSS.escape(name)}"]`);
      const before = list.scrollHeight;
      const truth = () => [...list.querySelectorAll("li[data-name]")]
        .reduce((n, x) => n + x.offsetHeight, 0);
      // The shipped scroll: the smooth one. An instant scroll re-resolves as
      // the layout settles and lands correctly on this page; the smooth one
      // commits to the position it measured, which is the bug.
      row.scrollIntoView({ block: "center", behavior: "smooth" });
      await new Promise((r) => setTimeout(r, 2500));
      const lr = list.getBoundingClientRect();
      const rr = row.getBoundingClientRect();
      return { before, after: list.scrollHeight, truth: truth(),
               error: Math.round((rr.top + rr.height / 2) - (lr.top + lr.height / 2)),
               railHeight: Math.round(lr.height) };
    }, TARGET);
    await bare.close();
    assert.ok(Math.abs(estimated.error) > estimated.railHeight,
      `without the laid-out rail the same scroll is off by more than a rail: ${estimated.error}px`
      + ` (rail ${estimated.railHeight}px, height estimated ${estimated.before}`
      + ` against ${estimated.truth})`);
  } finally {
    await browser.close();
    server.close();
  }
  console.log("railreveal_browser: ok");
})().catch((err) => { console.error(err); process.exit(1); });
