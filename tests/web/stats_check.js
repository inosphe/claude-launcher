/* The Stats page. Three readings share it:

   * the fleet overview (#/stats) — every session that worked in the chosen
     window (today / 7 days / 30 days), its usage beside the others', killed
     and archived rows held out until the box is ticked, and a provider strip
     that narrows the table;
   * the detail panel (#/stats/<session>) — that session's window rows
     (per-provider figures and the provider/model changes the ledger proves)
     above the transcript reading the page has always drawn;
   * compare mode (#/compare/<a,b,c>) — several sessions' transcript figures
     side by side, with the spread across the set.

   Slice the shipped functions out of app.js so these checks cover what the
   browser runs: the routes, the endpoints each mode asks, the controls, the
   rows, the detail panel, and what the page draws. */
const fs = require("fs");
const path = require("path");
const root = path.join(__dirname, "..", "..");
const src = fs.readFileSync(
  path.join(root, "src", "claude_launcher", "web", "static", "app.js"),
  "utf8"
);
const html = fs.readFileSync(
  path.join(root, "src", "claude_launcher", "web", "static", "index.html"),
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
    else if (src[j] === "}") {
      depth--;
      if (!depth) return src.slice(head, j + 1);
    }
  }
  throw new Error("unbalanced " + name);
}

/* A top-level `const NAME = [...];` block, verbatim. */
function constant(name) {
  const start = src.indexOf(`const ${name} = `);
  if (start < 0) throw new Error("missing " + name);
  // A checkout may carry either line ending (core.autocrlf).
  const end = src.slice(start).search(/\];\r?\n/);
  if (end < 0) throw new Error("unterminated " + name);
  return src.slice(start, start + end + 2);
}

function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), handlers: {},
    href: "", title: "", type: "", value: "", selected: false, checked: false,
    style: {}, dataset: {}, attrs: {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    appendChild(c) { this.kids.push(c); return c; },
    append(...cs) { this.kids.push(...cs); },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) {
      this.classes = new Set(String(v).split(/\s+/).filter(Boolean));
    },
    set innerHTML(v) { if (!v) this.kids = []; },
    all() {
      const out = [this];
      for (const k of this.kids) out.push(...k.all());
      return out;
    },
    words() { return this.all().map((x) => x.text).join(" | "); },
  };
  return n;
}

function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const view = node("div");
const fetched = [];
const shown = [];
const timers = [];
let answer = { ok: true, status: 200, body: {} };

const stubs = `
let statsCache = null;
let statsError = "";
let statsTimer = null;
let statsPageOpen = false;
let statsSession = "";
let statsUnit = "day";
let statsSeq = 0;
let statsDetailSeq = 0;
let statsCompare = null;
let statsCompareCache = null;
let statsLogScale = false;
let statsWindow = "today";
let statsIncludeKilled = false;
let statsIncludeArchived = false;
let statsOverview = null;
let statsOverviewError = "";
let statsProvider = "";
const STATS_COMPARE_MAX = 12;
const document = { createElementNS: (_ns, tag) => el(tag) };
let currentName = "s1";
let sessionsCache = [{ name: "s1" }, { name: "s2" }];
const location = { hash: "" };
function $() { return view; }
function showView(name) { shown.push(name); }
async function api(url) {
  fetched.push(url);
  const a = (typeof answer === "function") ? answer(url) : answer;
  return { ok: a.ok, status: a.status, json: async () => a.body };
}
function setTimeout(fn, ms) { const t = { fn, ms }; timers.push(t); return t; }
function clearTimeout(t) { const i = timers.indexOf(t); if (i >= 0) timers.splice(i, 1); }
function setAnswer(next) { answer = next; }
function state() {
  return { cache: statsCache, error: statsError, open: statsPageOpen,
           session: statsSession, unit: statsUnit, hash: location.hash,
           compare: statsCompare, overview: statsOverview,
           window: statsWindow, killed: statsIncludeKilled,
           archived: statsIncludeArchived,
           provider: statsProvider, overviewError: statsOverviewError };
}
`;

const ctx = {};
new Function(
  "exports", "el", "view", "fetched", "shown", "timers", "answer",
  stubs
  + constant("USAGE_PARTS") + constant("STATS_UNITS")
  + constant("STATS_WINDOWS") + constant("STATS_CATEGORIES")
  + constant("STATS_METHOD") + constant("STATS_WINDOW_METHOD")
  + constant("STATS_PALETTE")
  + slice("usageShort") + slice("profileHarnessLabel")
  + ["openStatsPage", "stopStatsPoll", "statsSchedule",
     "refreshStats", "refreshStatsOverview", "refreshStatsDetail",
     "statsPct", "statsBucketLabel", "statsStamp", "statsCard", "statsChart",
     "statsBucketTable", "statsFigures", "statsSourceTable", "statsSenderTable",
     "statsScroll", "statsSection", "statsWindowTabs", "statsEndedToggle",
     "setStatsProvider", "statsProviderStrip",
     "statsRowTotal", "statsRowRequests", "statsOverviewRows",
     "statsOverviewTable", "statsOverviewRow", "statsSwitchList",
     "statsDetailWindow", "statsReadingSections", "statsDetailPanel",
     "statsControls", "renderStatsOverview", "renderStats", "parseHash",
     "refreshStatsCompare", "statsUnitTabs", "statsCompareHash", "statsValue",
     "statsZ", "themeColor", "statsColor", "statsCompareChoices",
     "statsComparePicker", "statsCompareTable", "statsShareBars",
     "statsTimelineChart", "renderStatsCompare"]
    .map(slice).join("\n")
  + `
Object.assign(exports, {
  open: openStatsPage, stop: stopStatsPoll, refresh: refreshStats,
  label: statsBucketLabel, parseHash, setAnswer, state,
});`
)(ctx, el, view, fetched, shown, timers, answer);

let failures = 0;
function check(name, condition, extra) {
  if (condition) return;
  failures++;
  console.error(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
}
/* A node's classes, whether they were set as a property (HTML elements) or
   through setAttribute (an SVG path's `class` is an attribute, not a
   settable property in the browser either). */
const classesOf = (n) => new Set([
  ...n.classes,
  ...String(n.attrs.class || "").split(/\s+/).filter(Boolean),
]);
const withClass = (cls) => view.all().filter((n) => classesOf(n).has(cls));
const tick = () => new Promise((resolve) => setImmediate(resolve));

const zero = { human: 0, daemon: 0, mesh: 0, opening: 0, harness: 0 };
function row(category, messages, share, extra = {}) {
  return { category, messages, chars: 0, tokens: messages * 100, carry: messages * 1000,
           requests: 1, triggered: { total: messages * 10000 },
           share, kinds: [], ...extra };
}
const reading = {
  session: "s1", available: true, harness: "claude", unit: "day",
  utc_offset: "+0900", since: "2026-09-26T15:00:00+00:00",
  totals: { input: 10, cache_read: 900, cache_write: 50, output: 40, total: 1000,
            requests: 12, subagent_requests: 2 },
  buckets: [
    { start: "2026-09-26T00:00:00+09:00", input: 5, cache_read: 400, cache_write: 25,
      output: 20, total: 450, requests: 5, inputs: { ...zero, human: 1 } },
    { start: "2026-09-27T00:00:00+09:00", input: 0, cache_read: 0, cache_write: 0,
      output: 0, total: 0, requests: 0, inputs: { ...zero } },
    { start: "2026-09-28T00:00:00+09:00", input: 5, cache_read: 500, cache_write: 25,
      output: 20, total: 550, requests: 7, inputs: { ...zero, daemon: 3, mesh: 2 } },
  ],
  sources: [
    row("human", 1, { messages: 0.1, tokens: 0.1, triggered: 0.2, carry: 0.1 }),
    row("daemon", 5, { messages: 0.5, tokens: 0.3, triggered: 0.5, carry: 0.3 }, {
      kinds: [{ kind: "session: reminder", messages: 3, tokens: 300, carry: 0,
                triggered: { total: 0 }, requests: 0 },
              { kind: "cflow nudge", messages: 2, tokens: 30, carry: 0,
                triggered: { total: 50000 }, requests: 4 }] }),
    row("mesh", 2, { messages: 0.2, tokens: 0.2, triggered: 0.1, carry: 0.2 }),
    row("opening", 1, { messages: 0.1, tokens: 0.2, triggered: 0.1, carry: 0.2 }),
    row("harness", 1, { messages: 0.1, tokens: 0.2, triggered: 0.1, carry: 0.2 }),
  ],
  senders: [{ sender: "s2", messages: 2, tokens: 200, carry: 0, requests: 1,
              triggered: { total: 3000 } },
            { sender: "gone", messages: 1, tokens: 10, carry: 0, requests: 0,
              triggered: { total: 0 } }],
  unattributed: { requests: 0, triggered: { total: 0 } },
  compactions: 2,
};

/* The window payload: two metered sessions and one the ledger cannot speak
   for, one of which changed provider mid-window. */
function sessionRow(extra) {
  return {
    session: "s1", status: "busy", category: "running", harness: "claude",
    profile: "ds4:claude", borrow: "", issue: "claunch-x", project: "claunch",
    source: "ledger", provider: "fireworks", model: "m1",
    providers: [{ provider: "fireworks", input: 100, cache_read: 900,
                  cache_write: 0, output: 0, total: 1000, requests: 4 }],
    input: 100, cache_read: 900, cache_write: 0, output: 0, total: 1000,
    requests: 4, first_at: "2026-10-21T09:00:00+09:00",
    last_at: "2026-10-21T20:00:00+09:00", days: { "2026-10-21": 1000 },
    switches: [{ at: "2026-10-21T09:30:00+09:00", kind: "provider",
                 from: "kimi", to: "fireworks", from_model: "k3",
                 to_model: "m1" }],
    declared_provider: "fireworks", ended: false,
    ...extra,
  };
}
const overview = {
  window: "today", unit: "day", days: ["2026-10-21"],
  since: "2026-10-21T00:00:00+09:00", until: "2026-10-21T21:00:00+09:00",
  zone: "+0900", include_ended: false,
  totals: { input: 110, cache_read: 990, cache_write: 0, output: 0, total: 1100,
            requests: 5 },
  cache_share: 0.9,
  providers: [
    { provider: "fireworks", sessions: 1, totals: 0, total: 1000, requests: 4 },
    { provider: "kimi", sessions: 1, total: 100, requests: 1 },
  ],
  sessions: [
    sessionRow({}),
    sessionRow({
      session: "s2", provider: "kimi", borrow: "ds4", switches: [],
      providers: [{ provider: "kimi", input: 10, cache_read: 90, cache_write: 0,
                    output: 0, total: 100, requests: 1 }],
      total: 100, requests: 1, days: { "2026-10-21": 100 },
    }),
    sessionRow({
      session: "oauth", source: "none", provider: "claude", model: "",
      providers: [], total: 0, requests: 0, days: {}, switches: [],
      profile: "nc:claude", issue: "", declared_provider: "claude",
    }),
  ],
  counts: { sessions: 3, metered: 2, unmetered: 1, ended_excluded: 5,
              killed_excluded: 4, archived_excluded: 1 },
  ledger_since: "2026-10-19",
  partial: false,
};

const byUrl = (over) => (url) => url.includes("/api/stats/overview")
  ? { ok: true, status: 200, body: { ...overview, ...(over || {}) } }
  : { ok: true, status: 200, body: reading };

(async () => {
  check("the rail has a Stats tab next to Window",
        html.includes('href="#/stats" data-page="stats"') &&
        html.includes('id="stats-view"') &&
        html.indexOf('data-page="window"') < html.indexOf('data-page="stats"'));
  check("#/stats resolves to the page with no session named",
        JSON.stringify(ctx.parseHash("#/stats")) === '{"page":"stats","name":""}',
        ctx.parseHash("#/stats"));
  check("#/stats/<name> names the session",
        ctx.parseHash("#/stats/s%202").name === "s 2", ctx.parseHash("#/stats/s%202"));

  // ---- the fleet overview ---------------------------------------------
  ctx.setAnswer(byUrl());
  ctx.open("");
  await tick();
  check("with no session the page opens on the fleet, not on one session",
        ctx.state().session === "" && shown.at(-1) === "stats", ctx.state());
  check("it asks the overview endpoint for today, killed and archived held out",
        fetched.at(-1) === "/api/stats/overview?window=today&killed=0&archived=0", fetched);
  check("the overview asks nothing per session",
        !fetched.some((u) => u.includes("/stats?unit=")), fetched);
  check("a finished overview polls slowly",
        timers.length === 1 && timers[0].ms === 30000, timers.map((t) => t.ms));

  check("a window tab per unit of time, Today pressed",
        withClass("stats-window").length === 1 &&
        withClass("wf-btn").filter((b) => b.dataset.window).length === 3 &&
        withClass("wf-btn").find((b) => b.dataset.window === "today")
          .classes.has("active"), view.words());

  const rows = withClass("stats-row");
  check("one row per session in the payload, longest bar first",
        rows.length === 3 &&
        rows.map((r) => r.dataset.session).join() === "s1,s2,oauth",
        rows.map((r) => r.dataset.session));
  check("a row carries the session's tokens, requests and share",
        rows[0].words().includes("1.00k") && rows[0].words().includes("4") &&
        rows[0].words().includes("90.9%"), rows[0].words());
  check("the bar is as wide as the row's share of the busiest",
        withClass("stats-rowbar-fill")[0].style.width === "100.00%" &&
        withClass("stats-rowbar-fill")[1].style.width === "10.00%",
        withClass("stats-rowbar-fill").map((b) => b.style.width));
  check("a session the ledger cannot speak for shows a dash, never a zero",
        rows[2].words().includes("—") &&
        withClass("stats-unmetered").length === 1, rows[2].words());
  check("a borrow is marked on its row",
        rows[1].words().includes("borrow ds4"), rows[1].words());
  check("a session answered by another provider in the window is marked",
        rows[0].words().includes("⇄ provider"), rows[0].words());
  check("the provider strip counts the sessions and their tokens",
        withClass("stats-provider").length === 3 &&
        withClass("stats-provider").map((b) => b.dataset.provider).join() ===
          ",fireworks,kimi", withClass("stats-provider").map((b) => b.dataset.provider));
  check("the summary holds the window total and the held-out count",
        view.words().includes("1.10k") && view.words().includes("Held out") &&
        view.words().includes("5"), view.words());
  check("an unmetered session is explained",
        view.words().includes("no ledger record"), view.words());
  check("the method rows say where the figures come from",
        withClass("stats-method").length === 1 &&
        view.words().includes("metering ledger"),
        withClass("stats-method").length);

  // A provider narrows the table to the rows that used it.
  withClass("stats-provider").find((b) => b.dataset.provider === "kimi")
    .handlers.click[0]();
  check("picking a provider keeps only the sessions that used it",
        ctx.state().provider === "kimi" &&
        withClass("stats-row").map((r) => r.dataset.session).join() === "s2",
        withClass("stats-row").map((r) => r.dataset.session));
  check("the provider card is the pressed one",
        withClass("stats-provider").find((b) => b.dataset.provider === "kimi")
          .classes.has("active"));
  withClass("stats-provider").find((b) => b.dataset.provider === "kimi")
    .handlers.click[0]();
  check("picking it again shows every provider",
        ctx.state().provider === "" && withClass("stats-row").length === 3);

  // The two ways a session ends have a box each, and each asks for its own.
  const boxes = withClass("stats-ended-box");
  check("killed and archived have a box each",
        boxes.length === 2 &&
        boxes[0].words().includes("include killed") &&
        boxes[1].words().includes("include archived"),
        boxes.map((b) => b.words()));
  const archiveBox = boxes[1].kids[0];
  archiveBox.checked = true;
  archiveBox.handlers.change[0]();
  await tick();
  check("ticking the box asks again with killed and archived included",
        ctx.state().killed === false && ctx.state().archived === true &&
        fetched.at(-1) === "/api/stats/overview?window=today&killed=0&archived=1",
        fetched.at(-1));

  // The window switch.
  const week = withClass("wf-btn").find((b) => b.dataset.window === "week");
  week.handlers.click[0]();
  await tick();
  check("switching the window asks for that window",
        ctx.state().window === "week" &&
        fetched.at(-1) === "/api/stats/overview?window=week&killed=0&archived=1", fetched.at(-1));

  // ---- the detail panel ------------------------------------------------
  ctx.setAnswer((url) => url.includes("/api/stats/overview")
    ? { ok: true, status: 200, body: overview }
    : { ok: true, status: 200, body: { ...reading, partial: true } });
  ctx.open("s1");
  await tick();
  check("a session link opens the detail panel for it",
        withClass("stats-detail").length === 1 && ctx.state().session === "s1");
  check("the panel asks that session's transcript reading",
        fetched.some((u) => u === "/api/sessions/s1/stats?unit=day"), fetched);
  const panel = withClass("stats-detail")[0];
  check("the panel names the session and offers its own page",
        panel.words().includes("s1") && panel.all().some((n) =>
          n.tag === "a" && n.href === "#/s/s1" && n.text === "open session"),
        panel.words());
  check("the panel lists the window's provider change",
        withClass("stats-switch").length === 1 &&
        panel.words().includes("kimi → fireworks") &&
        withClass("stats-switch-kind")[0].text === "provider", panel.words());
  check("the panel's window table carries the provider's share",
        withClass("stats-detail-providers")[0].words().includes("100.0%"),
        withClass("stats-detail-providers")[0].words());
  check("the transcript reading is in the panel, labelled as the conversation",
        withClass("stats-area").length >= 4 &&
        panel.words().includes("whole conversation (transcript)"), panel.words());
  check("the chart draws one stacked band per usage part",
        withClass("stats-area").length === 4 &&
        withClass("stats-area").every((a) =>
          (a.attrs.class || "").includes("usage-") &&
          /^M[\d.]+ [\d.]+ L/.test(a.attrs.d || "")),
        withClass("stats-area").map((a) => a.attrs.class));
  check("the stacked bands reach the window's own total",
        withClass("stats-total-line").length === 1 &&
        (withClass("stats-total-line")[0].attrs.d || "").startsWith("M"),
        withClass("stats-total-line").map((a) => a.attrs.d));
  check("each bucket keeps a hover reading of its own",
        withClass("stats-hit").length === 3, withClass("stats-hit").length);
  // A band whose points all share one x is a vertical line of zero width: the
  // path still parses and still starts with "M", so the shape has to be read
  // as coordinates, not matched as a prefix.
  const xsOf = (d) => [...String(d || "").matchAll(/(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)/g)]
    .map((m) => Number(m[1]));
  check("every stacked band has width",
        withClass("stats-area").every((a) => new Set(xsOf(a.attrs.d)).size >= 2),
        withClass("stats-area").map((a) => a.attrs.d));
  check("the total line spans the chart",
        new Set(xsOf(withClass("stats-total-line")[0].attrs.d)).size >= 2,
        withClass("stats-total-line")[0].attrs.d);

  check("a partial reading polls again soon",
        timers.length === 1 && timers[0].ms === 2000, timers.map((t) => t.ms));

  // A session that started today has exactly one day bucket -- the commonest
  // reading the panel shows, and the one a count-based x axis collapses.
  ctx.setAnswer((url) => url.includes("/api/stats/overview")
    ? { ok: true, status: 200, body: overview }
    : { ok: true, status: 200, body: { ...reading, buckets: [reading.buckets[2]] } });
  await ctx.refresh();
  check("a one-bucket reading still draws full-width bands",
        withClass("stats-area").length === 4 &&
        withClass("stats-area").every((a) => new Set(xsOf(a.attrs.d)).size >= 2 &&
          Math.max(...xsOf(a.attrs.d)) - Math.min(...xsOf(a.attrs.d)) > 100),
        withClass("stats-area").map((a) => a.attrs.d));
  check("a one-bucket reading still draws its total line",
        withClass("stats-total-line").length === 1 &&
        /L-?\d/.test(withClass("stats-total-line")[0].attrs.d) &&
        new Set(xsOf(withClass("stats-total-line")[0].attrs.d)).size >= 2,
        withClass("stats-total-line").map((a) => a.attrs.d));

  // Clicking a row is the other way in.
  withClass("stats-row")[1].handlers.click[0]({ preventDefault() {} });
  check("clicking a row moves to that session's panel",
        ctx.state().hash === "#/stats/s2", ctx.state().hash);

  withClass("stats-detail-head")[0].kids.find((k) => k.tag === "button")
    .handlers.click[0]();
  check("closing the panel goes back to the fleet",
        ctx.state().hash === "#/stats", ctx.state().hash);

  // The unit switch moves the transcript reading, not the window.
  const day = withClass("wf-btn").find((b) => b.dataset.unit === "week");
  day.handlers.click[0]();
  await tick();
  check("switching the unit asks the reading again with that unit",
        ctx.state().unit === "week" &&
        fetched.at(-1) === "/api/sessions/s1/stats?unit=week", fetched.at(-1));

  // The detail reading's failures are its own.
  ctx.setAnswer((url) => url.includes("/api/stats/overview")
    ? { ok: true, status: 200, body: overview }
    : { ok: false, status: 400, body: { error: "unit must be one of: hour" } });
  await ctx.refresh();
  check("an API error is stated in the panel",
        ctx.state().error === "unit must be one of: hour", ctx.state());
  ctx.setAnswer((url) => url.includes("/api/stats/overview")
    ? { ok: true, status: 200, body: overview }
    : { ok: false, status: 404, body: {} });
  await ctx.refresh();
  check("a daemon without the reading endpoint is named as outdated",
        ctx.state().error.includes("predates the Stats page"), ctx.state());
  ctx.setAnswer({ ok: false, status: 404, body: {} });
  await ctx.refresh();
  check("a daemon without the overview endpoint is named as outdated",
        ctx.state().overviewError.includes("predates the window overview"),
        ctx.state());

  ctx.setAnswer({ ok: true, status: 200, body: { ...reading, available: false,
    reason: "no transcript found for this session's conversation" } });
  await ctx.refresh();
  check("an unavailable reading gives its reason",
        view.words().includes("no transcript found"), view.words());

  check("labels: the hour, the date, the week's Monday",
        ctx.label("2026-09-28T13:00:00+09:00", "hour") === "09-28 13h" &&
        ctx.label("2026-09-28T00:00:00+09:00", "day") === "09-28" &&
        ctx.label("2026-09-28T00:00:00+09:00", "week") === "wk 09-28");

  // ---- compare mode ------------------------------------------------------
  check("#/compare/<a,b> is the Stats page comparing those sessions",
        JSON.stringify(ctx.parseHash("#/compare/s1,s%202")) ===
          '{"page":"stats","name":"","compare":["s1","s 2"]}',
        ctx.parseHash("#/compare/s1,s%202"));
  const shares = (h, d) => ({ human: { messages: h, tokens: h, triggered: h },
                              daemon: { messages: d, tokens: d, triggered: d },
                              mesh: { messages: 0, tokens: 0, triggered: 0 },
                              opening: { messages: 0, tokens: 0, triggered: 0 },
                              harness: { messages: 0, tokens: 0, triggered: 0 } });
  const compared = {
    unit: "week", compared: ["s1", "s2"], reader: "worker",
    sessions: [{ session: "s1", available: true }, { session: "s2", available: true },
               { session: "old", available: false, reason: "no transcript found" }],
    metrics: [
      { key: "requests", label: "requests", kind: "count", values: [10, 30],
        mean: 20, median: 20, stdev: 14.142, min: 10, max: 30,
        z: [-0.7071, 0.7071], rank: [2, 1] },
      { key: "machine_messages_share", label: "daemon + mesh input share",
        kind: "share", values: [0.25, null], mean: 0.25, median: 0.25, stdev: null,
        min: 0.25, max: 0.25, z: [null, null], rank: [1, null] },
    ],
    shares: [{ session: "s1", ...shares(0.75, 0.25) },
             { session: "s2", ...shares(0.5, 0.5) }],
    timeline: { starts: ["2026-09-14T00:00:00+09:00", "2026-09-21T00:00:00+09:00",
                         "2026-09-28T00:00:00+09:00"],
                series: [{ session: "s1", tokens: [100, 0, 50], requests: [1, 0, 1] },
                         { session: "s2", tokens: [null, 200, 300], requests: [null, 2, 3] }] },
  };
  ctx.setAnswer({ ok: true, status: 200, body: compared });
  ctx.open("", ["s1", "s2"]);
  await tick();
  check("compare mode asks the compare endpoint for the set and the unit",
        fetched.at(-1) === "/api/stats/compare?sessions=s1,s2&unit=week", fetched.at(-1));
  const table = withClass("stats-compare")[0];
  check("the figures table has a row per figure and a column per session plus spread",
        table && table.kids.length === 3 && table.kids[0].kids.length === 1 + 2 + 5,
        table && table.kids.map((r) => r.kids.length));
  const req = table.kids[1].kids;
  check("a value carries its z-score and rank",
        req[1].words().includes("10") && req[1].words().includes("−0.71 · #2") &&
        req[2].words().includes("+0.71 · #1"), [req[1].words(), req[2].words()]);
  check("the spread columns print mean, median, std dev, min, max",
        req.slice(3).map((c) => c.text).join() === "20,20,14,10,30",
        req.slice(3).map((c) => c.text));
  const share = table.kids[2].kids;
  check("a share prints as a percentage, a missing value and an undefined " +
        "deviation as a dash",
        share[1].words().includes("25.0%") && share[2].words().includes("–") &&
        share[5].text === "–", share.map((c) => c.words()));
  check("no z-score is drawn where the spread is undefined",
        withClass("stats-z").length === 2, withClass("stats-z").length);
  const segs = withClass("stats-share-seg");
  check("origin bars: one segment per non-zero origin, as wide as its share",
        segs.length === 8 && segs[0].style.width === "75.00%" &&
        segs[1].style.width === "25.00%", segs.map((x) => x.style.width));
  const lines = view.all().filter((n) => n.tag === "polyline");
  check("the timeline draws a line per session and leaves the unread bucket out",
        lines.length === 2 && lines[0].attrs.points.split(" ").length === 3 &&
        lines[1].attrs.points.split(" ").length === 2,
        lines.map((l) => l.attrs.points));
  const heads = table.kids[0].kids.slice(1, 3).map((th) => th.style.color);
  check("a session keeps one colour in the table and the lines",
        heads[0] === lines[0].attrs.stroke && heads[1] === lines[1].attrs.stroke &&
        heads[0] !== heads[1], [heads, lines.map((l) => l.attrs.stroke)]);
  const flat = lines[0].attrs.points;
  withClass("stats-log")[0].handlers.click[0]();
  const logged = view.all().filter((n) => n.tag === "polyline")[0].attrs.points;
  check("the log scale redraws the lines and says so",
        logged !== flat && view.words().includes("log scale") &&
        withClass("stats-log")[0].classes.has("active"), [flat, logged]);
  withClass("stats-log")[0].handlers.click[0]();
  check("a session with nothing to read is named with its reason",
        view.words().includes("old: not compared — no transcript found"), view.words());
  check("a finished comparison polls slowly",
        timers.length === 1 && timers[0].ms === 30000, timers.map((t) => t.ms));

  const picks = withClass("stats-pick");
  check("the picker lists every session, the compared ones pressed",
        picks.map((b) => b.text + ":" + b.attrs["aria-pressed"]).join() ===
          "s1:true,s2:true", picks.map((b) => b.text));
  picks[0].handlers.click[0]();
  check("unpressing a session drops it from the link",
        ctx.state().hash === "#/compare/s2", ctx.state().hash);
  withClass("wf-btn").find((b) => b.dataset.mode === "one").handlers.click[0]();
  check("Fleet goes back to the overview",
        ctx.state().hash === "#/stats", ctx.state().hash);

  ctx.setAnswer({ ok: true, status: 200, body: { ...compared, partial: true } });
  ctx.open("", ["s1", "s2", "s3"]);
  await tick();
  check("a changed set is asked afresh, and a partial answer polls fast",
        fetched.at(-1) === "/api/stats/compare?sessions=s1,s2,s3&unit=week" &&
        timers.length === 1 && timers[0].ms === 2000,
        [fetched.at(-1), timers.map((t) => t.ms)]);

  const asked = fetched.length;
  ctx.open("", []);
  await tick();
  check("an empty set asks nothing and says to pick",
        fetched.length === asked && view.words().includes("pick sessions above"),
        view.words());
  check("the Compare button is the pressed one in compare mode",
        withClass("wf-btn").find((b) => b.dataset.mode === "compare")
          .classes.has("active"));

  ctx.setAnswer(byUrl());
  ctx.open("");
  await tick();
  check("a one-session link leaves compare mode",
        ctx.state().compare === null &&
        fetched.at(-1) === "/api/stats/overview?window=week&killed=0&archived=1", fetched.at(-1));
  withClass("wf-btn").find((b) => b.dataset.mode === "compare").handlers.click[0]();
  check("Compare starts from the session on screen (none: an empty set)",
        ctx.state().hash === "#/compare/", ctx.state().hash);

  const before = fetched.length;
  ctx.stop();
  await ctx.refresh();
  check("leaving stops both the timer and the reads",
        timers.length === 0 && fetched.length === before && !ctx.state().open,
        { timers: timers.length, fetched: fetched.length });

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("stats_check: all checks passed");
})();
