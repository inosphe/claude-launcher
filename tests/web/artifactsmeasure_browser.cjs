/* The Artifacts page's measure flow (#/artifacts, artifacts-page.js) in a
   real browser: what the reader sees between pressing 측정 and the sizes
   arriving.

   Serves the real index.html/app.js/artifacts-page.js against a two-session
   fixture, answers POST /api/artifacts/measure after a short delay (so the
   in-flight window is observable), and lets GET /api/artifacts change shape
   between polls. It checks:
     1. before any press the session button reads 측정 and each row says
        미측정 -- the state the reader starts from;
     2. a press shows it landed immediately: that session's button becomes
        측정 중… and disabled, the page-wide button does too, and the request
        went once;
     3. a press on another session while that request is in flight is
        dropped -- one POST in flight is the whole page's job, and the second
        card does not falsely claim to be measuring;
     4. a poll that reports the walk running marks those rows 측정 중 and the
        session head counts them (측정 중 2/2), without rebuilding the list;
     5. the final poll returns the button to 측정, marks the rows 완료, and
        reports the walk in the notice;
     6. a path whose walk failed shows a 실패 chip with the reason as its
        title -- a failure is an answer, not a missing measurement;
     7. a refused measure request (500) is reported and the button is usable
        again -- a dead button after a failure is the other half of the bug;
     8. no page errors. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const root = path.join(__dirname, "../../src/claude_launcher/web/static");
const TYPES = { ".js": "text/javascript", ".css": "text/css", ".html": "text/html" };

const SESSION_1 = {
  name: "s1", live: true, category: "running", cwd: "/repo",
  scratch: { path: "/t/s1/scratch", exists: true, entries: 1 },
  items: [{ path: "/t/s1w", area: "basetemp", kind: "sweep",
            modified: "2026-11-01T00:00:00Z" }],
  totals: { bytes: 0, unmeasured: 2 },
  cleanup: { scheduled: null, receipts: [] },
};
const SESSION_2 = {
  name: "s2", live: false, category: "exited", cwd: "/repo",
  scratch: { path: "/t/s2/scratch", exists: false },
  items: [{ path: "/t/s2w", area: "basetemp", kind: "run" }],
  totals: { bytes: 0, unmeasured: 1 },
  cleanup: { scheduled: null, receipts: [] },
};

const base = (over = {}) => ({
  root: { path: "/t", exists: true },
  sessions: [SESSION_1, SESSION_2],
  orphans: [],
  measurable: ["/t/s1/scratch", "/t/s1w", "/t/s2w"],
  totals: { bytes: 0, unmeasured: 3 },
  running: [],
  job: { total: 0, done: 0, started: null, finished: null },
  ...over,
});

(async () => {
  const browser = await chromium.launch({ headless: true });
  const errors = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    page.on("pageerror", (e) => errors.push(String(e)));

    let current = base();
    let measureCalls = 0;
    let measureReply = null;   // {status, doc} to refuse the next press
    const MEASURE_DELAY_MS = 350;

    await page.route("http://pr.test/**", async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname === "/api/artifacts") return route.fulfill({ json: current });
      // The page batches read-only GETs into one POST.
      if (url.pathname === "/api/batch") {
        const wanted = JSON.parse(route.request().postData() || "{}").paths || [];
        const answers = {};
        if (wanted.includes("/api/artifacts")) answers["/api/artifacts"] = current;
        return route.fulfill({ json: { answers } });
      }
      if (url.pathname === "/api/artifacts/measure") {
        measureCalls += 1;
        await new Promise((resolve) => setTimeout(resolve, MEASURE_DELAY_MS));
        if (measureReply) {
          const { status, doc } = measureReply;
          measureReply = null;
          return route.fulfill({ status, json: doc });
        }
        return route.fulfill({ json: { paths: 2 } });
      }
      if (url.pathname.startsWith("/api/")) return route.fulfill({ json: {} });
      const file = url.pathname === "/" ? "index.html" : url.pathname.replace("/static/", "");
      const body = fs.readFileSync(path.join(root, file));
      return route.fulfill({ body, contentType: TYPES[path.extname(file)] || "application/octet-stream" });
    });

    const until = async (ready, why, ms = 8000) => {
      const started = Date.now();
      while (Date.now() - started < ms) {
        if (await ready()) return;
        await new Promise((resolve) => setTimeout(resolve, 40));
      }
      throw new Error(`timed out: ${why}`);
    };
    // An element may not exist yet (the notice only after the walk ends), and
    // locator.textContent() would block on it instead of reporting "".
    const text = async (loc) => (await loc.count()) ? ((await loc.first().textContent()) || "") : "";
    const card = (name) => page.locator("#artifacts-view .ar-session",
      { has: page.locator(`a.ar-name:text-is("${name}")`) });
    const measureButton = (name) => card(name).locator("button.ar-measure");
    const row = (name, p) => card(name).locator(`.ar-row[data-path="${p}"]`);
    const statusOf = (name, p) => row(name, p).locator(".ar-status");
    const notice = () => page.locator("#artifacts-view .wt-notice");
    const allButton = () => page.locator("#artifacts-view .ar-measure-all");

    await page.goto("http://pr.test/");
    await page.evaluate(() => { location.hash = "#/artifacts"; });
    await until(async () => (await page.locator("#artifacts-view .ar-session").count()) === 2,
      "the two session cards to render");
    // The page routes once more when boot's first refresh batch lands, and
    // that re-opens the page -- which drops a notice and any press still
    // pending (the state this harness is about). Wait for the list to stop
    // being rebuilt before pressing anything.
    for (let attempt = 0; attempt < 25; attempt++) {
      const handle = await page.evaluateHandle(
        () => document.querySelector("#artifacts-view .ar-session"));
      await page.waitForTimeout(400);
      const same = await page.evaluate((el) => !!el && el.isConnected &&
        document.querySelector("#artifacts-view .ar-session") === el, handle);
      if (same) break;
      if (attempt === 24) throw new Error("the artifacts list never settled");
    }

    // 1. Nothing has been pressed: the page says so per row.
    assert.equal(await text(measureButton("s1")), "측정");
    assert.equal(await measureButton("s1").isEnabled(), true);
    assert.equal(await text(statusOf("s1", "/t/s1/scratch")), "미측정");
    assert.equal(await text(statusOf("s1", "/t/s1w")), "미측정");
    assert.equal(await text(allButton()), "전체 측정");

    // 2. The press is visible before the daemon has answered: the button is
    //    the job's state for the reader.
    await measureButton("s1").click();
    await until(async () => (await text(measureButton("s1"))) === "측정 중…",
      "the pressed button to show 측정 중…");
    assert.equal(await measureButton("s1").isDisabled(), true);
    assert.ok((await measureButton("s1").getAttribute("class")).includes("is-busy"));
    assert.equal(await text(allButton()), "측정 중…");
    assert.equal(await allButton().isDisabled(), true);
    assert.equal(measureCalls, 1);

    // 3. Another session's press while that request is in flight is dropped:
    //    one walk at a time, and the second card must not claim otherwise.
    assert.equal(await text(measureButton("s2")), "측정");
    assert.equal(await measureButton("s2").isEnabled(), true);
    await measureButton("s2").click();
    await page.waitForTimeout(120);
    assert.equal(measureCalls, 1, "a second press must not start a second walk");
    assert.equal(await text(measureButton("s2")), "측정");
    // The daemon's answer names what it will walk.
    await until(async () => (await text(notice())).includes("측정 시작 · 2개 경로"),
      "the walk to be reported as started");

    // 4. A poll reporting the walk running marks those rows, and the session
    //    head counts them.
    current = base({
      running: ["/t/s1/scratch", "/t/s1w"],
      job: { total: 2, done: 1, started: "2026-11-04T06:00:00Z", finished: null },
      sessions: [
        { ...SESSION_1,
          scratch: { path: "/t/s1/scratch", exists: true, entries: 1, running: true },
          items: [{ path: "/t/s1w", area: "basetemp", kind: "sweep", running: true }] },
        SESSION_2,
      ],
    });
    await until(async () => (await text(statusOf("s1", "/t/s1w"))) === "측정 중",
      "the running rows to be marked 측정 중");
    assert.equal(await text(statusOf("s1", "/t/s1/scratch")), "측정 중");
    assert.equal(await text(card("s1").locator(".ar-measure-state")), "측정 중 2/2");
    assert.equal(await card("s1").locator(".ar-measure-state").isVisible(), true);
    // The poll repainted the chips; the button still holds the walk.
    assert.equal(await text(measureButton("s1")), "측정 중…");
    // The daemon walks one job at a time, so a second session's press is not
    // offered while one is running -- it would replace the counter on screen.
    assert.equal(await measureButton("s2").isEnabled(), false);
    assert.equal(await text(measureButton("s2")), "측정");

    // 5. The walk ends: the button comes back and the rows carry their sizes.
    current = base({
      job: { total: 2, done: 2, started: "2026-11-04T06:00:00Z", finished: "2026-11-04T06:00:05Z" },
      totals: { bytes: 300, unmeasured: 1 },
      sessions: [
        { ...SESSION_1,
          scratch: { path: "/t/s1/scratch", exists: true, entries: 1, bytes: 100,
                     at: "2026-11-04T06:00:00Z" },
          items: [{ path: "/t/s1w", area: "basetemp", kind: "sweep", bytes: 200,
                    at: "2026-11-04T06:00:00Z" }],
          totals: { bytes: 300, unmeasured: 0 } },
        SESSION_2,
      ],
    });
    await until(async () => (await text(notice())).includes("측정 완료"),
      "the walk to be reported as finished");
    assert.match(await text(notice()), /측정 완료 · 2개 경로/);
    assert.equal(await text(measureButton("s1")), "측정");
    assert.equal(await measureButton("s1").isEnabled(), true);
    assert.equal(await text(allButton()), "전체 측정");
    assert.equal(await text(statusOf("s1", "/t/s1w")), "완료");
    assert.match(await text(row("s1", "/t/s1w").locator(".ar-size")), /200 B/);
    assert.equal(await card("s1").locator(".ar-measure-state").isVisible(), false);

    // 6. A failed walk is an answer with a reason, shown on its own row.
    current = base({
      sessions: [
        SESSION_1,
        { ...SESSION_2,
          items: [{ path: "/t/s2w", area: "basetemp", kind: "run",
                    error: "Permission denied: /t/s2w" }] },
      ],
    });
    await page.locator("#artifacts-view .wf-head button", { hasText: "새로 고침" }).click();
    await until(async () => (await text(statusOf("s2", "/t/s2w"))) === "실패",
      "the failed row to be marked 실패");
    assert.equal(await statusOf("s2", "/t/s2w").getAttribute("title"),
      "Permission denied: /t/s2w");
    assert.ok((await statusOf("s2", "/t/s2w").getAttribute("class")).includes("ar-status-failed"));
    assert.equal(await statusOf("s2", "/t/s2/scratch").getAttribute("title"),
      "경로가 없다 — 지울 것도 잴 것도 없다");

    // 7. A refused press is reported and does not leave the button dead.
    measureReply = { status: 500, doc: { error: "window unavailable" } };
    await measureButton("s2").click();
    await until(async () => (await text(notice())).includes("측정 실패"),
      "the refused press to be reported");
    assert.match(await text(notice()), /측정 실패: window unavailable/);
    assert.equal(await text(measureButton("s2")), "측정");
    assert.equal(await measureButton("s2").isEnabled(), true);
    assert.equal(await text(allButton()), "전체 측정");

    // 8. No page errors.
    assert.deepEqual(errors, []);
    console.log("ok: artifacts measure progress");
  } finally {
    await browser.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
