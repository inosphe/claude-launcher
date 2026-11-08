/* The Artifacts page's cleanup flow (#/artifacts, artifacts-page.js) in a
   real browser: the plan-then-apply path the page now offers next to 측정.

   Serves the real index.html and app.js against a fixture of four sessions
   and answers POST /api/artifact-cleanup as the daemon does, then checks:
     1. a session with cleanup candidates has a 정리 button and one without
        has it disabled;
     2. pressing 정리 sends only {session} -- no plan_id, no path -- and the
        plan it answers with is drawn as a list: the ready item, the kept
        item and its reason, and the counts;
     3. 적용 submits that plan_id, and the answer's removed/cleanup_pending
        counts are reported before the listing is read again;
     4. a plan answered with `scheduled` (a cleanup is already waiting)
        reports that and leaves 적용 disabled -- the daemon's own rule is
        not to submit such a plan;
     5. an apply refused because the window is busy answers `scheduled`, and
        the page reports it as a result instead of an error;
     6. a refused plan (400) is shown as an error and nothing is submitted;
     7. a refused apply (400) is reported and the plan is dropped;
     8. a two-hundred `cleanup_pending` (window refused, retry record could
        not be written) is reported as "not started" with its reason, the
        plan stays on screen and 적용 stays enabled -- it is not a deletion;
     9. a ready row has a 지우기 button that submits its position in the plan
        (`rows`) and never a path; the answer is the plan minus that row, so
        the rows left alone stay on screen under the same plan_id, and a
        cleanup_pending row has no such button;
    10. a row apply refused for a busy window says "not started" and keeps
        the plan -- the page never reports it as scheduled, because a
        schedule would replan the whole set;
    11. no page errors. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const root = path.join(__dirname, "../../src/claude_launcher/web/static");
const TYPES = { ".js": "text/javascript", ".css": "text/css", ".html": "text/html" };

const INVENTORY = {
  root: { path: "/t", exists: true },
  sessions: [
    { name: "s1", live: true, category: "running", cwd: "/repo",
      scratch: { path: "/t/s1/scratch", exists: true, entries: 1, bytes: 100 },
      items: [{ path: "/t/s1w", area: "basetemp", kind: "sweep", bytes: 5000000,
                modified: "2026-11-01T00:00:00Z" }],
      totals: { bytes: 5000100 }, cleanup: { scheduled: null, receipts: [] } },
    { name: "s2", live: false, category: "exited", cwd: "/repo",
      scratch: { path: "/t/s2/scratch", exists: false },
      items: [], totals: { bytes: 0 },
      cleanup: { scheduled: null, receipts: [
        { plan_id: "old", created_at: "2026-11-01T00:00:00Z", removed: 2, bytes: 1000 }] } },
    { name: "s3", live: false, category: "exited", cwd: "/repo",
      scratch: { path: "/t/s3/scratch", exists: false },
      items: [{ path: "/t/s3w", area: "basetemp", kind: "run", bytes: 2000 }],
      totals: { bytes: 2000 }, cleanup: { scheduled: { attempts: 1 }, receipts: [] } },
    { name: "s4", live: false, category: "exited", cwd: "/repo",
      scratch: { path: "/t/s4/scratch", exists: false },
      items: [{ path: "/t/s4w", area: "basetemp", kind: "run", bytes: 3000 }],
      totals: { bytes: 3000 }, cleanup: { scheduled: null, receipts: [] } },
  ],
  orphans: [],
  measurable: [],
  totals: { bytes: 5005100, unmeasured: 0 },
  running: [],
};

(async () => {
  const browser = await chromium.launch({ headless: true });
  const errors = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    page.on("pageerror", (e) => errors.push(String(e)));
    const calls = [];       // every POST /api/artifact-cleanup body, in order
    let listings = 0;       // GET /api/artifacts, to see the reload after an apply
    let planReply = null;   // per session: what the plan request answers
    let applyReply = null;

    await page.route("http://pr.test/**", async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname === "/api/artifacts") {
        listings++;
        return route.fulfill({ json: INVENTORY });
      }
      // Read-only GETs are batched by the page into one POST. The artifacts
      // listing is the only one this harness needs answered.
      if (url.pathname === "/api/batch") {
        const wanted = JSON.parse(route.request().postData() || "{}").paths || [];
        const answers = {};
        if (wanted.includes("/api/artifacts")) {
          listings++;
          answers["/api/artifacts"] = INVENTORY;
        }
        return route.fulfill({ json: { answers } });
      }
      if (url.pathname === "/api/artifact-cleanup") {
        const body = JSON.parse(route.request().postData() || "{}");
        calls.push(body);
        if (!body.plan_id) {
          const reply = planReply(body.session);
          if (reply.status) {
            return route.fulfill({ status: reply.status, json: reply.doc });
          }
          return route.fulfill({ json: reply.doc });
        }
        const reply = applyReply(body.session);
        if (reply.status) return route.fulfill({ status: reply.status, json: reply.doc });
        return route.fulfill({ json: reply.doc });
      }
      if (url.pathname.startsWith("/api/")) return route.fulfill({ json: {} });
      const file = url.pathname === "/" ? "index.html" : url.pathname.replace("/static/", "");
      const body = fs.readFileSync(path.join(root, file));
      return route.fulfill({ body, contentType: TYPES[path.extname(file)] || "application/octet-stream" });
    });

    const until = async (ready, why, ms = 5000) => {
      const started = Date.now();
      while (Date.now() - started < ms) {
        if (ready()) return;
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
      throw new Error(`timed out: ${why}`);
    };
    const card = (name) => page.locator("#artifacts-view .ar-session", { has: page.locator(`a.ar-name:text-is("${name}")`) });
    const notice = () => page.locator("#artifacts-view .wt-notice");
    const plans = [
      { plan_id: "tok1", session: "s1", created_at: 1, items: [
        { path: "/t/s1w", status: "ready", bytes: 5000000, kind: "test", fingerprint: "f1" },
        { path: "/t/s1/scratch", status: "cleanup_pending", bytes: 100,
          reason: "test run receipt is not a regular file" }] },
      { plan_id: "tok2", session: "s4", created_at: 1, items: [
        { path: "/t/s4w", status: "ready", bytes: 3000, kind: "test", fingerprint: "f4" }] },
    ];
    planReply = (session) => {
      if (session === "s3") {
        return { doc: { plan_id: "tok3", session: "s3", created_at: 1, items: [
          { path: "/t/s3w", status: "ready", bytes: 2000 }],
          scheduled: { attempts: 1, last_reason: "scheduled cleanup: s3" } } };
      }
      const plan = plans.find((p) => p.session === session);
      return { doc: plan || { plan_id: "empty", session, created_at: 1, items: [] } };
    };
    let applies = 0;
    applyReply = (session) => {
      applies++;
      if (session === "s4") {
        return { doc: { status: "scheduled", plan_id: "tok2", reason: "measurement window unavailable" } };
      }
      return { doc: { plan_id: "tok1", session, removed: 1, cleanup_pending: 1, items: [] } };
    };

    await page.goto("http://pr.test/#/artifacts");
    await page.waitForSelector("#artifacts-view .ar-session", { timeout: 15000 });

    // 1. The action exists where there is something to clean and is disabled
    //    where there is not.
    assert.equal(await card("s1").locator("button:text-is('정리')").count(), 1);
    assert.equal(await card("s2").locator("button:text-is('정리')").isDisabled(), true);

    // 2. Plan request: session only; the plan is drawn, ready and kept apart.
    await card("s1").locator("button:text-is('정리')").click();
    await page.waitForFunction(() => document.querySelectorAll("#artifacts-view .ar-plan-row").length === 2,
      null, { timeout: 15000 });
    assert.deepEqual(calls[0], { session: "s1" }, "a plan request names a session and nothing else");
    assert.equal(await card("s1").locator(".ar-plan-row").count(), 2);
    assert.equal(await card("s1").locator(".ar-plan-row .ar-plan-reason").textContent(),
      "test run receipt is not a regular file");
    assert.match(await card("s1").locator(".ar-plan .ar-cleanup-line").first().textContent(), /준비 1개 · 보류 1개/);
    assert.equal(await card("s1").locator("button:text-is('적용')").isEnabled(), true);
    // The button cannot open a second plan while this one is on screen.
    assert.equal(await card("s1").locator("button:text-is('정리')").isDisabled(), true);

    // 3. Apply submits the plan_id; the counts are reported, then the listing
    //    is read again so the receipts and sizes are current.
    const listingsBefore = listings;
    await card("s1").locator("button:text-is('적용')").click();
    await page.waitForFunction(() => {
      const n = document.querySelector("#artifacts-view .wt-notice");
      return n && n.textContent.includes("삭제 1개");
    }, null, { timeout: 15000 });
    assert.deepEqual(calls[1], { session: "s1", plan_id: "tok1" });
    assert.match(await notice().textContent(), /s1: 정리 완료 · 삭제 1개 · 보류 1개/);
    // The reload is a batched read one tick later, so give it a moment.
    await until(() => listings > listingsBefore, "the listing is read again after an apply");
    assert.equal(await card("s1").locator(".ar-plan-row").count(), 0, "the spent plan leaves the card");

    // 4. A standing schedule: the plan is reported, not submitted.
    await card("s3").locator("button:text-is('정리')").click();
    await page.waitForFunction(() => document.querySelectorAll("#artifacts-view .ar-session .wt-chip.ar-scheduled").length >= 2,
      null, { timeout: 15000 });
    assert.equal(await card("s3").locator("button:text-is('적용')").isDisabled(), true);
    assert.match(await card("s3").locator(".ar-plan .ar-cleanup-line").first().textContent(), /정리 예약됨/);

    // 5. An apply refused for a busy window is an answer, not a failure.
    await card("s4").locator("button:text-is('정리')").click();
    await card("s4").locator("button:text-is('적용')").click();
    await page.waitForFunction(() => {
      const n = document.querySelector("#artifacts-view .wt-notice");
      return n && n.textContent.includes("예약했다");
    }, null, { timeout: 15000 });
    assert.match(await notice().textContent(), /s4: 측정 창이 바쁘다 — 정리를 예약했다 \(measurement window unavailable\)/);

    // 6. A refusal (400) is shown as an error and nothing is applied.
    planReply = (session) => ({ status: 400, doc: { error: "a managed session is required", status: "cleanup_pending" } });
    await card("s1").locator("button:text-is('정리')").click();
    await page.waitForFunction(() => {
      const node = [...document.querySelectorAll("#artifacts-view .ar-session")]
        .find((n) => n.querySelector("a.ar-name")
          && n.querySelector("a.ar-name").textContent === "s1");
      const plan = node && node.querySelector(".ar-plan");
      return plan && plan.textContent.includes("계획 실패");
    }, null, { timeout: 15000 });
    assert.match(await card("s1").locator(".ar-plan").textContent(), /계획 실패: a managed session is required/);
    assert.equal(applies, 2, "the refused plan submitted nothing");

    // 7. A refused apply is reported and the plan is dropped: the daemon no
    //    longer holds it, so the reader plans again rather than pressing a
    //    button that can only fail the same way. (The wait is scoped to the
    //    card: another session's plan block stays open above it.)
    planReply = (session) => ({ doc: plans.find((p) => p.session === session) });
    applyReply = () => ({ status: 400, doc: {
      error: "unknown, expired or foreign plan; request a new plan", status: "cleanup_pending" } });
    await card("s4").locator("button:text-is('정리')").click();
    await card("s4").locator("button:text-is('적용')").click();
    await page.waitForFunction(() => {
      const node = [...document.querySelectorAll("#artifacts-view .ar-session")]
        .find((n) => n.querySelector("a.ar-name")
          && n.querySelector("a.ar-name").textContent === "s4");
      const plan = node && node.querySelector(".ar-plan");
      return plan && plan.textContent.includes("적용 실패");
    }, null, { timeout: 15000 });
    assert.match(await card("s4").locator(".ar-plan").textContent(),
      /적용 실패: unknown, expired or foreign plan; request a new plan/);
    assert.equal(await card("s4").locator(".ar-plan-row").count(), 0);
    assert.equal(await card("s4").locator("button:text-is('정리')").isEnabled(), true);

    // 8. Two hundred with `cleanup_pending` is not a deletion: the daemon
    //    refused the window and could not even write its retry record, so
    //    apply() never ran. The page must say that and keep the plan (the
    //    daemon still holds it) instead of reporting "정리 완료 · 삭제 0개".
    planReply = (session) => ({ doc: plans.find((p) => p.session === session) });
    applyReply = (session) => ({ doc: { status: "cleanup_pending", plan_id: "tok1",
      session, reason: "measurement window unavailable (could not schedule: disk full)" } });
    await card("s1").locator("button:text-is('정리')").click();
    await card("s1").locator("button:text-is('적용')").click();
    await page.waitForFunction(() => {
      const n = document.querySelector("#artifacts-view .wt-notice");
      return n && n.textContent.includes("시작하지 못했다");
    }, null, { timeout: 15000 });
    assert.match(await notice().textContent(),
      /s1: 정리를 시작하지 못했다 — measurement window unavailable \(could not schedule: disk full\)/);
    assert.doesNotMatch(await notice().textContent(), /정리 완료/);
    assert.equal(await card("s1").locator(".ar-plan-row").count(), 2, "the unapplied plan stays on screen");
    assert.equal(await card("s1").locator("button:text-is('적용')").isEnabled(), true);

    // 9. One row at a time: the button submits the row's position in the plan
    //    (no path), and the daemon answers with the plan minus that row.
    const readyRow = card("s1").locator(".ar-plan-row").first();
    assert.equal(await readyRow.locator("button:text-is('지우기')").count(), 1);
    assert.equal(await card("s1").locator(".ar-plan-row").nth(1)
      .locator("button:text-is('지우기')").count(), 0, "a kept row cannot be deleted");
    applyReply = () => ({ doc: { plan_id: "tok1", session: "s1", created_at: 1,
      removed: 1, cleanup_pending: 0, items: [
        { path: "/t/s1w", status: "removed", bytes: 5000000, kind: "test", fingerprint: "f1" },
        { path: "/t/s1/scratch", status: "cleanup_pending", bytes: 100,
          reason: "test run receipt is not a regular file" }] } });
    await readyRow.locator("button:text-is('지우기')").click();
    await page.waitForFunction(() => {
      const n = document.querySelector("#artifacts-view .wt-notice");
      return n && n.textContent.includes("삭제 1개");
    }, null, { timeout: 15000 });
    assert.deepEqual(calls[calls.length - 1], { session: "s1", plan_id: "tok1", rows: [0] },
      "a row apply names a position in the plan, never a path");
    assert.equal(await card("s1").locator(".ar-plan-row").count(), 2,
      "the rows left alone stay on screen under the same plan");
    assert.match(await card("s1").locator(".ar-plan-row").first().textContent(), /삭제됨/);
    assert.equal(await card("s1").locator("button:text-is('적용')").isDisabled(), true,
      "nothing ready is left in this plan");

    // 10. A row apply with the window busy is "not started", not scheduled:
    //     keeping the plan is what lets the reader press again.
    planReply = (session) => ({ doc: plans.find((p) => p.session === session) });
    applyReply = (session) => ({ doc: { status: "cleanup_pending", plan_id: "tok2",
      session, reason: "measurement window unavailable" } });
    await card("s4").locator("button:text-is('정리')").click();
    await card("s4").locator(".ar-plan-row button:text-is('지우기')").click();
    await page.waitForFunction(() => {
      const n = document.querySelector("#artifacts-view .wt-notice");
      return n && n.textContent.includes("s4: 정리를 시작하지 못했다");
    }, null, { timeout: 15000 });
    assert.deepEqual(calls[calls.length - 1], { session: "s4", plan_id: "tok2", rows: [0] });
    assert.match(await notice().textContent(), /s4: 정리를 시작하지 못했다 — measurement window unavailable/);
    assert.doesNotMatch(await notice().textContent(), /예약했다/);
    assert.equal(await card("s4").locator(".ar-plan-row").count(), 1, "the plan stays for another press");
    assert.equal(await card("s4").locator(".ar-plan-row button:text-is('지우기')").count(), 1);

    assert.deepEqual(errors, [], `page errors: ${errors.join("; ")}`);
    console.log("Artifacts cleanup flow: plan, apply, scheduled and refused paths passed");
  } finally {
    await browser.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
