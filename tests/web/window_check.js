/* The measurement Window page: the arbiter's current holders and the FIFO
   queue behind them. Slice the shipped functions out of app.js so this check
   covers the page the browser runs, including its poll lifecycle and the
   distinction between a session owner and a raw pid owner. */
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

function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), handlers: {},
    href: "", title: "", type: "",
    appendChild(c) { this.kids.push(c); return c; },
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

const document = { createElement: (tag) => node(tag) };
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
let windowCache = null;
let windowError = "";
let windowTimer = null;
let windowPageOpen = false;
let windowCancelBusy = new Set();
let sessionsCache = [{ name: "s1", status: "busy" }];
function $() { return view; }
function showView(name) { shown.push(name); }
async function api(url) {
  fetched.push(url);
  return { ok: answer.ok, status: answer.status,
           json: async () => answer.body };
}
function setInterval(fn, ms) { const t = { fn, ms }; timers.push(t); return t; }
function clearInterval(t) { const i = timers.indexOf(t); if (i >= 0) timers.splice(i, 1); }
function fmtAge(secs) {
  secs = Math.floor(secs);
  if (secs < 60) return secs + "s";
  if (secs < 3600) return Math.floor(secs / 60) + "m";
  return Math.floor(secs / 3600) + "h";
}
function plural(n, one, many) { return n + " " + (n === 1 ? one : many || one + "s"); }
function setAnswer(next) { answer = next; }
function setCache(next) { windowCache = next; windowError = ""; }
function state() {
  return { cache: windowCache, error: windowError, open: windowPageOpen };
}
`;

const ctx = {};
new Function(
  "exports", "document", "el", "view", "fetched", "shown", "timers",
  "answer",
  stubs
  + slice("openWindowPage") + slice("stopWindowPoll")
  + slice("refreshWindow") + slice("windowOwner") + slice("windowAge")
  + slice("windowEntry") + slice("windowSummary") + slice("renderWindow")
  + slice("parseHash")
  + `
Object.assign(exports, {
  open: openWindowPage, stop: stopWindowPoll, refresh: refreshWindow,
  owner: windowOwner, age: windowAge, entry: windowEntry, render: renderWindow,
  parseHash, setAnswer, setCache, state,
});`
)(ctx, document, el, view, fetched, shown, timers, answer);

let failures = 0;
function check(name, condition, extra) {
  if (condition) return;
  failures++;
  console.error(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
}
const withClass = (cls) => view.all().filter((n) => n.classes.has(cls));

const snapshot = {
  holders: [
    { grant_id: "g1", cls: "targeted", session: "s1", pid: 11,
      label: "pytest tests/test_one.py", acquired_at: "2026-08-31T06:00:00Z" },
    { grant_id: "g2", cls: "sweep", session: null, pid: 44,
      label: "full suite", acquired_at: "2026-08-31T06:01:00Z" },
  ],
  queue: [
    { grant_id: "q1", cls: "targeted", session: "s2", pid: 22,
      label: "changed tests", enqueued_at: "2026-08-31T06:02:00Z" },
    { grant_id: "q2", cls: "sweep", session: null, pid: 55,
      label: "release sweep", enqueued_at: "2026-08-31T06:03:00Z" },
  ],
  caps: { targeted: 5, sweep: 1 }, max_wait: 1800, reminder_interval: 180,
  cores: 32, advisory_n_now: 6,
};

(async () => {
  check("the rail has a Window tab",
        html.includes('href="#/window" data-page="window"') &&
        html.includes('id="window-view"'));
  check("the hash resolves to the Window page",
        ctx.parseHash("#/window").page === "window", ctx.parseHash("#/window"));

  ctx.setAnswer({ ok: true, status: 200, body: snapshot });
  ctx.open();
  await new Promise((resolve) => setImmediate(resolve));
  check("opening shows the page", shown.at(-1) === "window", shown);
  check("opening reads the arbiter endpoint", fetched.includes("/api/window"), fetched);
  check("the active page polls every two seconds",
        timers.length === 1 && timers[0].ms === 2000, timers.map((t) => t.ms));

  check("one row is drawn for every holder and waiter",
        withClass("window-row").length === 4, withClass("window-row").length);
  check("queued requests have cancellation controls",
        withClass("window-cancel").length === 2, withClass("window-cancel").length);
  check("holder and FIFO positions are explicit",
        view.words().includes("held") && view.words().includes("#1") &&
        view.words().includes("#2"), view.words());
  check("capacity, queue depth and recommended workers are visible",
        view.words().includes("1 / 5") && view.words().includes("1 waiting") &&
        view.words().includes("Recommended workers") && view.words().includes("6"),
        view.words());
  check("the 30-minute queue limit and release reminder are visible",
        view.words().includes("Maximum queue wait") && view.words().includes("30m") &&
        view.words().includes("Release reminder") && view.words().includes("every 3m"),
        view.words());
  check("a live session owner links to its session",
        withClass("window-owner").some((n) => n.tag === "a" && n.href === "#/s/s1"));
  check("a raw pid owner remains visible",
        withClass("window-owner").some((n) => n.text === "pid 44"));
  check("labels explain what each grant is running",
        view.words().includes("pytest tests/test_one.py") &&
        view.words().includes("release sweep"), view.words());

  check("age uses the acquisition or enqueue timestamp",
        ctx.age({ acquired_at: "2026-08-31T06:00:00Z" },
                Date.parse("2026-08-31T06:02:05Z")) === "2m ago");
  check("a missing owner and timestamp are stated",
        ctx.owner({}) === "unknown owner" && ctx.age({}) === "age unknown");

  const beforeStop = fetched.length;
  ctx.stop();
  await ctx.refresh();
  check("leaving stops both polling and reads",
        timers.length === 0 && fetched.length === beforeStop && !ctx.state().open,
        { timers: timers.length, fetched: fetched.length, state: ctx.state() });

  ctx.setCache(snapshot);
  ctx.setAnswer({ ok: false, status: 503, body: { error: "window unavailable" } });
  ctx.open();
  await ctx.refresh();
  check("an API failure is stated without erasing the last snapshot",
        ctx.state().error === "window unavailable" &&
        withClass("window-row").length === 4,
        { state: ctx.state(), rows: withClass("window-row").length });
  ctx.stop();

  ctx.setCache({ holders: [], queue: [], caps: { targeted: 5, sweep: 1 },
                 max_wait: 1800, reminder_interval: 180, cores: 8, advisory_n_now: 8 });
  ctx.render();
  check("an idle arbiter states both empty sections",
        view.words().includes("no grants are held") &&
        view.words().includes("nothing is waiting"), view.words());

  if (failures) process.exit(1);
  console.log("window_check ok");
})();
