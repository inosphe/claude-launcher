/* The Reports page: every round report on this machine, in one table.

   The page exists for the reader who has neither a session nor an issue in
   hand, and the thing it must not do is quietly become a page about the
   sessions that are still running -- on a real machine those are two rows out
   of twenty-five. So the checks here are mostly about what survives: a round
   whose session the daemon has no record of is drawn, marked and still
   openable, and it is only ever hidden by a filter the reader asked for.

   The rest is the reading: the size said as "19 KB" rather than 19591 B (the
   bytes are what the filesystem knows, not what a person wants), the stamp
   said with its zone, and the order flipping without re-sorting.

   Slice the real functions out of app.js and drive them against a stub DOM. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  const head = src.lastIndexOf("async ", start) === start - 6 ? start - 6 : start;
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

/* The state tabs are a table, not a function, and the labels the page offers
   are half of what is under test -- so take the real one rather than keeping
   a copy here that could drift from it. */
function sliceConst(name) {
  const start = src.indexOf(`const ${name} = [`);
  if (start < 0) throw new Error("missing " + name);
  const end = src.indexOf("\n];", start);
  if (end < 0) throw new Error("unterminated " + name);
  return src.slice(start, end + 3);
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), handlers: {}, dataset: {},
    value: "", disabled: false, title: "", href: "", type: "", target: "",
    rel: "", selected: false,
    appendChild(c) { this.kids.push(c); return c; },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    // The page redraws itself from scratch every poll and every filter click;
    // a stub that kept the old children would let a leak pass unnoticed.
    set innerHTML(v) { if (!v) this.kids = []; },
    get innerHTML() { return ""; },
    get className() { return [...this.classes].join(" "); },
    set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    all() {
      const out = [this];
      for (const k of this.kids) out.push(...k.all());
      return out;
    },
    find(cls) { return this.all().filter((n) => n.classes.has(cls)); },
    // Every scrap of text in the subtree, for the "does it say so at all"
    // checks that do not care which element said it.
    words() { return this.all().map((n) => n.text).join(" "); },
  };
  return n;
}
const document = { createElement: (t) => node(t) };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const view = node("div");
const fetched = [];
let answer = { ok: true, status: 200, body: { reports: [] } };
const timers = [];

const stubs = `
/* The page's own state. Declared here rather than sliced, the way
   beads_check declares the board's: what is under test is the functions that
   read and write it, and a harness that could not reset it between checks
   would be testing the order they run in. */
let reportsCache = null, reportsError = "", reportsTimer = null, reportsOpen = false;
let reportsState = "all", reportsSession = "", reportsIssue = "", reportsOldest = false;
const shown = [];
function showView(n) { shown.push(n); }
function $() { return view; }
async function api(p) {
  fetched.push(p);
  return { ok: answer.ok, status: answer.status, json: async () => answer.body };
}
function setInterval(fn, ms) { timers.push(ms); return { ms }; }
function clearInterval(t) { timers.splice(timers.indexOf(t.ms), 1); }
function setAnswer(a) { answer = a; }
function setFilters(f) {
  reportsState = f.state === undefined ? "all" : f.state;
  reportsSession = f.session || "";
  reportsIssue = f.issue || "";
  reportsOldest = !!f.oldest;
}
function setCache(rows) { reportsCache = rows; reportsError = ""; }
/* The page's base path. Real in app.js (derived from location.pathname);
   settable here because a row's one link has to survive being served under a
   relay tunnel's "/t/<backend>/" prefix as well as from the daemon's root. */
let BASE = "/";
function setBase(b) { BASE = b; }
function state() {
  return { open: reportsOpen, error: reportsError, cache: reportsCache };
}
`;

const ctx = {};
new Function(
  "exports", "document", "el", "view", "fetched", "answer", "timers",
  stubs
  + sliceConst("REPORT_STATES")
  + slice("openReports") + slice("stopReportsPoll") + slice("refreshReports")
  + slice("reportSessionState") + slice("reportsShown")
  + slice("url")
  + slice("fmtReportSize") + slice("reportWhen")
  + slice("reportsFilterBar") + slice("reportsPick") + slice("reportsRow")
  + slice("renderReports")
  + `
Object.assign(exports, {
  open: openReports, stop: stopReportsPoll, refresh: refreshReports,
  sessionState: reportSessionState, shownRows: reportsShown,
  size: fmtReportSize, when: reportWhen, row: reportsRow,
  bar: reportsFilterBar, render: renderReports,
  states: REPORT_STATES, shown, setAnswer, setFilters, setCache, state, setBase,
});`)(ctx, document, el, view, fetched, answer, timers);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* ---- the size, said the way a person reads it -------------------------- */
check("bytes stay bytes while they are readable as bytes", ctx.size(931), "931 B");
check("the real case from the board: 19591 B is 19 KB", ctx.size(19591), "19 KB");
check("a small page keeps one decimal", ctx.size(4200), "4.1 KB");
check("the boundary is 1024, not 1000", [ctx.size(1023), ctx.size(1024)], ["1023 B", "1.0 KB"]);
check("a long round is megabytes", ctx.size(3 * 1024 * 1024), "3.0 MB");
check("nothing known is nothing said, never 'NaN B'",
      [ctx.size(undefined), ctx.size(null), ctx.size(-1), ctx.size("x")],
      ["", "0 B", "", ""]);

/* ---- the stamp --------------------------------------------------------- */
check("the stamp says which zone it is in", ctx.when("2026-08-26T05:13:22Z"),
      "2026-08-26 05:13:22 UTC");
check("a row with no stamp says nothing rather than 'undefined'", ctx.when(null), "");

/* ---- what the daemon still knows about the writer ---------------------- */
check("a running session", ctx.sessionState({ session_status: "busy" }), "live");
check("idle is still live -- the record is there", ctx.sessionState({ session_status: "idle" }), "live");
check("a record that has exited", ctx.sessionState({ session_status: "exited" }), "ended");
check("no record at all -- the ordinary case for this page",
      [ctx.sessionState({ session_status: null }), ctx.sessionState({})], ["gone", "gone"]);

/* ---- the rows ---------------------------------------------------------- */
const ROWS = [
  { file: "20260826T051322Z-claunch-j31.html", session: "s121",
    at: "2026-08-26T05:13:22Z", issue: "claunch-j31", size: 19591,
    session_status: null,
    url: "/api/sessions/s121/reports/20260826T051322Z-claunch-j31.html" },
  { file: "20260826T040000Z-claunch-tak.html", session: "s-live",
    at: "2026-08-26T04:00:00Z", issue: "claunch-tak", size: 4200,
    session_status: "busy",
    url: "/api/sessions/s-live/reports/20260826T040000Z-claunch-tak.html" },
  { file: "20260826T020000Z-claunch-j31.html", session: "s-live",
    at: "2026-08-26T02:00:00Z", issue: "claunch-j31", size: 1536,
    session_status: "busy",
    url: "/api/sessions/s-live/reports/20260826T020000Z-claunch-j31.html" },
  { file: "20260825T090000Z-no-issue.html", session: "s99",
    at: "2026-08-25T09:00:00Z", issue: null, size: 900,
    session_status: "exited",
    url: "/api/sessions/s99/reports/20260825T090000Z-no-issue.html" },
];

const gone = ctx.row(ROWS[0]);
const open = gone.find("reports-open")[0];
check("the row opens the report itself, in its own tab",
      [open.href, open.target, open.rel], [ROWS[0].url, "_blank", "noopener"]);
check("and names the round by the issue it was written for",
      gone.find("reports-issue")[0].text, "claunch-j31");
check("the size is readable, not raw bytes", gone.find("reports-size")[0].text, "19 KB");
check("the stamp rides beside it", gone.find("reports-when")[0].text,
      "2026-08-26 05:13:22 UTC");

/* The point of the page. The daemon has no record of s121 -- the row is here
   anyway, it says so, and it does not pretend there is a session to visit. */
const chip = gone.find("reports-sess")[0];
check("a session with no record is still drawn, marked as such",
      [chip.tag, chip.classes.has("gone"), chip.find("reports-sess-state")[0].text],
      ["span", true, "no record"]);
check("and it is not offered as a link to nowhere", chip.href, "");
check("the title says why the round outlived it",
      chip.title.includes("kept outside the session directory"), true);
check("the report is still openable -- that is the whole point", open.href, ROWS[0].url);

/* Openable from wherever the page is being served, which is the half this
   row skipped. The url the daemon hands back is its own absolute path, and
   the daemon cannot know it was reached through a relay tunnel -- under
   "/t/<backend>/" an unresolved href leaves the tunnel and hits the relay's
   404 rather than the daemon (claunch-krw1: 404 bare, 302-to-login under the
   prefix). Every fetch on this page already resolves against BASE -- all five
   call sites wrap url(), and api() does too -- and this link now does the
   same. Other clickable hrefs are not covered by that: mdLink() sets one
   without url() (claunch-xntk). */
ctx.setBase("/t/box/");
check("through a relay tunnel the row's link keeps the tunnel prefix",
      ctx.row(ROWS[0]).find("reports-open")[0].href,
      "/t/box/api/sessions/s121/reports/20260826T051322Z-claunch-j31.html");
ctx.setBase("/");
check("and from the daemon's own root it is the path the daemon gave",
      ctx.row(ROWS[0]).find("reports-open")[0].href, ROWS[0].url);
check("and the issue is still reachable on the board",
      gone.find("reports-board")[0].href, "#/beads/claunch-j31");

const live = ctx.row(ROWS[1]);
const liveChip = live.find("reports-sess")[0];
check("a session the daemon still runs is a link to it",
      [liveChip.tag, liveChip.href, liveChip.classes.has("live")],
      ["a", "#/s/s-live", true]);
check("the chip says the state the daemon reported",
      [liveChip.find("reports-sess-name")[0].text, liveChip.find("reports-sess-state")[0].text],
      ["s-live", "running"]);

const noIssue = ctx.row(ROWS[3]);
check("a round that named no issue says so rather than showing a blank",
      noIssue.find("reports-issue")[0].text, "no issue");
check("and offers no board link, because there is no issue to open",
      noIssue.find("reports-board").length, 0);
check("an ended session is marked ended, not gone",
      [noIssue.find("reports-sess")[0].classes.has("ended"),
       noIssue.find("reports-sess")[0].tag], [true, "a"]);

/* ---- narrowing and order ----------------------------------------------- */
const only = (f) => { ctx.setFilters(f); return ctx.shownRows(ROWS).map((r) => r.session); };
check("by default every round is shown -- the page hides nothing on its own",
      only({}), ["s121", "s-live", "s-live", "s99"]);
check("live narrows to the sessions still running",
      only({ state: "live" }), ["s-live", "s-live"]);
check("ended narrows to the records that finished", only({ state: "ended" }), ["s99"]);
check("no record narrows to the rounds that outlived their session",
      only({ state: "gone" }), ["s121"]);
check("by session", only({ session: "s99" }), ["s99"]);
check("by issue", only({ issue: "claunch-tak" }), ["s-live"]);
check("a round with no issue is not swept up by an issue filter",
      only({ issue: "claunch-j31" }), ["s121", "s-live"]);
/* Two facts the board actually holds, and the page must not flatten either:
   a session writes more than one round, and two sessions write up one issue. */
check("both rounds a session wrote survive the narrowing",
      only({ session: "s-live" }), ["s-live", "s-live"]);
check("and they are told apart by what the row carries",
      (ctx.setFilters({ session: "s-live" }), ctx.shownRows(ROWS).map((r) => r.issue)),
      ["claunch-tak", "claunch-j31"]);
check("the filters compose", only({ state: "gone", issue: "claunch-tak" }), []);
check("oldest first is the same list read backwards, not a second sort",
      only({ oldest: true }), ["s99", "s-live", "s-live", "s121"]);
ctx.setFilters({});

/* ---- the page ---------------------------------------------------------- */
ctx.setCache(ROWS);
ctx.render();
check("every round is drawn", view.find("reports-row").length, 4);
check("and the count says so plainly", view.find("reports-count")[0].text, "4 reports");
check("the state tabs are offered, plus the two pickers and the order flip",
      [view.find("seq-tab").length, view.find("reports-pick").length,
       view.find("reports-order").length],
      [ctx.states.length, 2, 1]);

/* The pickers offer what the rows carry, not the fleet: a session with no
   report would be an option that leads to an empty page. */
const picks = view.find("reports-pick");
check("the session picker offers each session once, however many it wrote",
      picks[0].kids.map((o) => o.text), ["every session", "s-live", "s121", "s99"]);
check("the issue picker skips the rounds that named none",
      picks[1].kids.map((o) => o.text), ["every issue", "claunch-j31", "claunch-tak"]);

/* Clicking a state tab narrows in place, and the count says what was left
   out -- a filtered table that still read "3 reports" would be lying. */
const goneTab = view.find("seq-tab").find((b) => b.text === "no record");
goneTab.handlers.click[0]();
check("narrowing redraws in place, keeping no stale rows",
      view.find("reports-row").length, 1);
check("and the count says what is hidden", view.find("reports-count")[0].text,
      "1 of 4 reports");
ctx.setFilters({ state: "gone", issue: "claunch-tak" });
ctx.render();
check("a narrowing that matches nothing says so instead of drawing an empty table",
      view.words().includes("nothing matches"), true);
ctx.setFilters({});

/* An empty machine and an old daemon are different answers, and neither is
   "no reports exist" said in the other's voice. */
ctx.setCache([]);
ctx.render();
check("a machine with no round yet says how one gets written",
      view.words().includes("claunch report save"), true);
check("and draws no filters over an empty table", view.find("seq-tab").length, 0);

ctx.setCache(null);
ctx.render();
check("before the first answer the page says it is loading",
      view.words().includes("loading"), true);

/* ---- the fetch and the poll -------------------------------------------- */
ctx.setAnswer({ ok: true, status: 200, body: { reports: ROWS } });
ctx.open();
check("opening the page shows it and asks for the index once",
      [ctx.shown[ctx.shown.length - 1], fetched], ["reports", ["/api/reports"]]);
check("and arms one poll, slow enough for a thing written once a round",
      timers, [30000]);
ctx.open();
check("re-entering asks again -- walking back to a page is a reason to look",
      fetched, ["/api/reports", "/api/reports"]);
check("but arms no second poll", timers, [30000]);
ctx.stop();
check("leaving stops it, and the page stops asking",
      [timers, ctx.state().open], [[], false]);

const asked = fetched.length;
const done = ctx.refresh().then(() => {
  check("a tick that lands after the reader left fetches nothing",
        fetched.length, asked);

  ctx.setAnswer({ ok: true, status: 200, body: { reports: ROWS } });
  ctx.open();
  return new Promise((r) => setTimeout(r, 0));
}).then(() => {
  check("the answer lands in the table", ctx.state().cache.length, 4);
  check("and the reader is not told anything is wrong", ctx.state().error, "");

  ctx.setAnswer({ ok: false, status: 404, body: {} });
  return ctx.refresh();
}).then(() => {
  // A daemon older than this page has no /api/reports. Saying "no reports"
  // would be the one wrong answer: the files are there, this daemon just
  // cannot hand them over.
  check("an older daemon is named as the reason, not read as an empty machine",
        ctx.state().error.includes("daemon restart"), true);
  check("and the rows it already had are kept", ctx.state().cache.length, 4);

  ctx.setAnswer({ ok: false, status: 500, body: { error: "boom" } });
  return ctx.refresh();
}).then(() => {
  check("any other failure is reported in the daemon's own words",
        ctx.state().error, "boom");
  ctx.stop();
  if (failures) process.exit(1);
  console.log("reports_check ok");
});

done.catch((e) => { console.error(e); process.exit(1); });
