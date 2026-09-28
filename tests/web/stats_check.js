/* The Stats page (#/stats[/<session>]): one session's usage over hour, day
   or week buckets and its input sorted by origin. Slice the shipped
   functions out of app.js so this check covers the page the browser runs:
   the route, the endpoint it asks, the unit switch, the poll that speeds up
   while the transcript is still being read, and what the page draws. */
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
  return src.slice(start, src.indexOf("];\n", start) + 3);
}

function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), handlers: {},
    href: "", title: "", type: "", value: "", selected: false,
    style: {}, dataset: {},
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
let currentName = "s1";
let sessionsCache = [{ name: "s1" }, { name: "s2" }];
const location = { hash: "" };
function $() { return view; }
function showView(name) { shown.push(name); }
async function api(url) {
  fetched.push(url);
  const a = answer;
  return { ok: a.ok, status: a.status, json: async () => a.body };
}
function setTimeout(fn, ms) { const t = { fn, ms }; timers.push(t); return t; }
function clearTimeout(t) { const i = timers.indexOf(t); if (i >= 0) timers.splice(i, 1); }
function setAnswer(next) { answer = next; }
function state() {
  return { cache: statsCache, error: statsError, open: statsPageOpen,
           session: statsSession, unit: statsUnit, hash: location.hash };
}
`;

const ctx = {};
new Function(
  "exports", "el", "view", "fetched", "shown", "timers", "answer",
  stubs
  + constant("USAGE_PARTS") + constant("STATS_UNITS")
  + constant("STATS_CATEGORIES") + constant("STATS_METHOD")
  + slice("usageShort")
  + ["openStatsPage", "stopStatsPoll", "statsDefaultSession", "statsSchedule",
     "refreshStats", "statsPct", "statsBucketLabel", "statsCard", "statsChart",
     "statsBucketTable", "statsFigures", "statsSourceTable", "statsSenderTable",
     "statsScroll", "statsSection", "statsControls", "renderStats", "parseHash"]
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
const withClass = (cls) => view.all().filter((n) => n.classes.has(cls));
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

  ctx.setAnswer({ ok: true, status: 200, body: { ...reading, partial: true } });
  ctx.open("");
  await tick();
  check("with no name the page opens on the session on screen",
        ctx.state().session === "s1" && shown.at(-1) === "stats", ctx.state());
  check("it asks the session's stats endpoint for the chosen unit",
        fetched.at(-1) === "/api/sessions/s1/stats?unit=day", fetched);
  check("a partial reading polls again soon",
        timers.length === 1 && timers[0].ms === 2000, timers.map((t) => t.ms));
  check("a partial reading says so",
        withClass("stats-partial").length === 1, view.words());

  ctx.setAnswer({ ok: true, status: 200, body: reading });
  await ctx.refresh();
  check("a finished reading polls slowly",
        timers.length === 1 && timers[0].ms === 30000, timers.map((t) => t.ms));
  check("one column per bucket, empty ones included",
        withClass("stats-col").length === 3, withClass("stats-col").length);
  check("each column is as tall as its total against the busiest",
        withClass("stats-bar").map((b) => b.style.height).join() ===
          "81.82%,0.00%,100.00%",
        withClass("stats-bar").map((b) => b.style.height));
  const words = view.words();
  check("the notice + mesh share adds both categories",
        words.includes("Notices + mesh") && words.includes("70.0%") &&
        words.includes("60.0%") && words.includes("50.0%"), words);
  check("every origin and each notice kind is listed",
        ["human", "claunch notices", "mesh messages", "opening task", "harness",
         "session: reminder", "cflow nudge"].every((w) => words.includes(w)), words);
  check("a kind row leaves the share columns empty",
        withClass("stats-kind").every((r) => r.kids.length === 9 &&
          [2, 4, 6, 8].every((i) => r.kids[i].text === "")),
        withClass("stats-kind").map((r) => r.kids.map((k) => k.text)));
  const links = view.all().filter((n) => n.tag === "a");
  check("a live sender links to its own statistics, a gone one is text",
        links.some((a) => a.href === "#/stats/s2") &&
        !links.some((a) => a.href === "#/stats/gone") && words.includes("gone"),
        links.map((a) => a.href));
  check("the bucket table leaves out empty periods",
        view.all().filter((n) => n.tag === "tr" && n.kids[0] &&
          n.kids[0].text === "09-27").length === 0);
  check("the method notes are shown",
        withClass("stats-method")[0].kids.length === 5,
        withClass("stats-method").map((n) => n.kids.length));

  const week = withClass("wf-btn").find((b) => b.dataset.unit === "week");
  week.handlers.click[0]();
  await tick();
  check("switching the unit asks again with that unit",
        ctx.state().unit === "week" &&
        fetched.at(-1) === "/api/sessions/s1/stats?unit=week", fetched.at(-1));

  const pick = withClass("stats-session")[0];
  pick.value = "s2";
  pick.handlers.change[0]();
  check("choosing a session moves to its link", ctx.state().hash === "#/stats/s2",
        ctx.state().hash);

  check("labels: the hour, the date, the week's Monday",
        ctx.label("2026-09-28T13:00:00+09:00", "hour") === "09-28 13h" &&
        ctx.label("2026-09-28T00:00:00+09:00", "day") === "09-28" &&
        ctx.label("2026-09-28T00:00:00+09:00", "week") === "wk 09-28");

  ctx.setAnswer({ ok: true, status: 200,
                  body: { session: "s1", available: false,
                          reason: "no transcript found for this session's conversation" } });
  await ctx.refresh();
  check("an unavailable reading gives its reason",
        view.words().includes("no transcript found"), view.words());

  ctx.setAnswer({ ok: false, status: 400, body: { error: "unit must be one of: hour" } });
  await ctx.refresh();
  check("an API error is stated", ctx.state().error === "unit must be one of: hour",
        ctx.state());

  ctx.setAnswer({ ok: false, status: 404, body: {} });
  await ctx.refresh();
  check("a daemon without the endpoint is named as outdated",
        ctx.state().error.includes("predates the Stats page"), ctx.state());

  const before = fetched.length;
  ctx.stop();
  await ctx.refresh();
  check("leaving stops both the timer and the reads",
        timers.length === 0 && fetched.length === before && !ctx.state().open,
        { timers: timers.length, fetched: fetched.length });

  if (failures) process.exit(1);
  console.log("stats_check: ok");
})();
