/* The notification panel's repaint cost (claunch-n39r2).

   The panel behind the rail's bell is drawn by noticePanelPaint(), and the
   gate feed used to call it once per request whose history row it opened —
   plus once per feed, twice a poll (refreshCflow and refreshCflow2Rail both
   feed it). On the DevTools trace in the issue (Trace-20261027T230354, 486 s
   against a ~1,100-session daemon) that path was 34.7% of the sampled main
   thread and both multi-second stalls were one of those loops: the panel was
   torn down and rebuilt N+2 times for a poll that opened N rows.

   What is checked here, against the real sliced app.js:
   - one feed that opens N requests rebuilds the body once, whatever N is;
   - a second feed with the same answer rebuilds nothing, and a third that
     changed one request rebuilds once;
   - a batch of writes (what the panel's open/close pass does for every
     waiting request) flushes exactly one rebuild, and it shows the last
     state;
   - the countdown in a row moves on the page's one-second tick
     (noticeClocksPaint) with no rebuild at all — the rebuild is no longer
     what keeps the clock right;
   - opening the panel always draws, even when the state matches what the
     body already held from the previous open.

   The counter is the stub body's innerHTML setter: every rebuild of the
   panel starts with it, so counting it counts the rebuilds, and counting
   created nodes alongside it says what one rebuild costs. */

const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web",
                         "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");

function slice(from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to, a + 1);
  if (a < 0 || b < 0 || b <= a) throw new Error(`cannot slice ${from} .. ${to}`);
  return src.slice(a, b);
}

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

const BASE = 1700000000000;

function memoryStorage() {
  const data = new Map();
  return {
    data,
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => { data.set(k, String(v)); },
    removeItem: (k) => { data.delete(k); },
  };
}

/* ---- stub world ------------------------------------------------------- */
function build() {
  const seen = { rebuilds: 0, created: 0 };

  function node(cls, text) {
    const n = {
      className: cls || "", textContent: text === undefined ? "" : text,
      title: "", children: [], parentNode: null, on: {},
      disabled: false, type: "",
      classList: {
        contains: (c) => n.className.split(" ").includes(c),
        toggle: (c, on) => {
          const has = n.classList.contains(c);
          if (on === undefined ? has : !on) {
            n.className = n.className.split(" ").filter((x) => x !== c).join(" ");
          } else if (!has) { n.className = `${n.className} ${c}`.trim(); }
        },
        add: (c) => n.classList.toggle(c, true),
        remove: (c) => n.classList.toggle(c, false),
      },
      appendChild: (c) => { c.parentNode = n; n.children.push(c); return c; },
      insertBefore: (c, ref) => {
        const i = n.children.indexOf(ref);
        c.parentNode = n;
        if (i < 0) n.children.push(c); else n.children.splice(i, 0, c);
        return c;
      },
      get firstChild() { return n.children[0] || null; },
      removeChild: (c) => {
        const i = n.children.indexOf(c);
        if (i >= 0) n.children.splice(i, 1);
        c.parentNode = null;
      },
      append: (...cs) => { for (const c of cs) n.appendChild(c); },
      addEventListener: (ev, fn) => { n.on[ev] = fn; },
      click: (ev) => n.on.click && n.on.click(ev || { stopPropagation() {} }),
      querySelector: (sel) => {
        const cls = sel.slice(1);
        const walk = (x) => {
          for (const c of x.children) {
            if (c.classList.contains(cls)) return c;
            const deep = walk(c);
            if (deep) return deep;
          }
          return null;
        };
        return walk(n);
      },
      querySelectorAll: (sel) => { const one = n.querySelector(sel); return one ? [one] : []; },
      isConnected: true,
      text: () => [n.textContent, ...n.children.map((c) => c.text())].join(" "),
    };
    Object.defineProperty(n, "innerHTML", {
      get: () => "",
      configurable: true,
      set: () => {
        n.children.forEach((c) => { c.parentNode = null; });
        n.children = [];
      },
    });
    return n;
  }

  const bell = node("notice-bell");
  bell.appendChild(node("notice-bell-count"));
  const nodes = {
    notices: node(),
    "daemon-info": node(),
    "notice-bell": bell,
    "m-notice-bell": bell,
    "notice-panel": node(""),            // open
    "notice-panel-body": node(),
    "notice-settings": node("hidden"),
    "notice-settings-body": node(),
  };
  // Every rebuild of the panel starts by clearing the body; count those, and
  // count the nodes one costs.
  const body = nodes["notice-panel-body"];
  const innerHTML = Object.getOwnPropertyDescriptor(body, "innerHTML");
  Object.defineProperty(body, "innerHTML", {
    get: innerHTML.get,
    set: (v) => { seen.rebuilds += 1; innerHTML.set(v); },
  });

  const clock = { at: BASE };
  class FakeDate extends Date {
    constructor(...a) { super(...(a.length ? a : [clock.at])); }
    static now() { return clock.at; }
  }

  const gate = { restart: null };
  const api = async (p) => {
    const answer = (body) => ({ ok: true, json: async () => JSON.parse(JSON.stringify(body)) });
    if (p === "/api/daemon/restart-request") return answer({ request: gate.restart });
    return answer({});
  };
  const storage = memoryStorage();
  const loc = { hash: "" };
  const noop = () => {};
  const listeners = { document: {}, window: {} };
  const target = (on) => ({ addEventListener: (ev, fn) => { on[ev] = fn; } });
  const __doc = target(listeners.document);
  const __win = target(listeners.window);
  __doc.createElement = (tag) => { seen.created += 1; return node("", ""); };
  __win.addEventListener = (ev, fn) => { listeners.window[ev] = fn; };

  const code = slice("/* notices — the page's own voice",
                     "pollTimer = setInterval(pollTick, DASHBOARD_POLL_MS);");
  const sandbox = new Function(
    "$", "el", "setTimeout", "clearTimeout", "Date", "api", "location",
    "localStorage", "daemonHealth",
    "renderRelayBadge", "refreshProfiles", "refreshHarnesses", "refreshRoles",
    "refreshWorkspaces", "refreshSessions", "refreshMeshList", "refreshCflow",
    "refreshTermQueued", "reconnectNow", "route", "refreshNewWorktree",
    "refreshProjects", "ensureControlSocket", "refreshCflow2Rail",
    "refreshTermWindow", "refreshTermTimers",
    "sessLayoutFor", "setSessLayout", "talkShows", "BroadcastChannel",
    "confirm", "prompt", "document", "window",
    code +
    "\nreturn {noticePanelToggle, runGateSync, noticePanelBatch, noticePanelPaint," +
    " noticeClocksPaint, noticeHistory, gatePending, noticeBellPaint," +
    " refreshRestartGate};"
  )(
    (id) => nodes[id] || null,
    (tag, cls, text) => node(cls, text),
    noop, noop, FakeDate, api, loc, storage, null,
    noop, noop, noop, noop,
    noop, noop, noop, noop,
    noop, noop, noop, noop,
    noop, noop, noop,
    noop, noop, noop, noop, noop,
    noop, noop, noop,
    noop, noop, noop, undefined, undefined,
    __doc, __win
  );

  return {
    api: sandbox,
    nodes,
    clock,
    gate,
    seen,
    panel: () => nodes["notice-panel-body"].text(),
    rebuilds: () => seen.rebuilds,
    reset: () => { seen.rebuilds = 0; seen.created = 0; },
  };
}

/* One waiting ask of a v2 run, the shape /api/cflow2/rail answers with. */
function ask(i, extra) {
  return Object.assign({
    id: `ask2-${i}`, session: `s${i}`, cwd: `/w/s${i}`, scope: `s${i}`,
    version: 2, kind: "ask", label: "landing-review", prompt: `land ${i}?`,
    deadline: new Date(BASE + 300000).toISOString(),
    presses: [{
      label: "request", path: "/api/cflow2/approve",
      body: { cwd: `/w/s${i}`, scope: `s${i}`, node: `i${i}`, decision: "request" },
    }],
  }, extra || {});
}

function asks(n) {
  const out = [];
  for (let i = 1; i <= n; i += 1) out.push(ask(i));
  return out;
}

(async () => {
/* --- one feed, one rebuild, whatever it opened -------------------------- */
{
  for (const n of [2, 12]) {
    const w = build();
    w.api.noticePanelToggle(true);
    w.reset();
    w.api.runGateSync(asks(n), []);
    check(`a feed opening ${n} requests rebuilds the panel once`,
          w.rebuilds() === 1, { rebuilds: w.rebuilds(), n });
    check(`and lists all ${n} of them`,
          (w.panel().match(/landing-review/g) || []).length >= n, w.panel().slice(0, 200));
  }
}

/* --- an answer that did not move costs nothing -------------------------- */
{
  const w = build();
  w.api.noticePanelToggle(true);
  const recs = asks(6);
  w.api.runGateSync(recs, []);
  w.reset();
  w.api.runGateSync(asks(6), []);
  check("the same answer twice rebuilds nothing the second time",
        w.rebuilds() === 0, w.rebuilds());
  check("and creates no nodes", w.seen.created === 0, w.seen.created);

  const changed = asks(6);
  changed[2].prompt = "land 3 again, with a different question?";
  w.api.runGateSync(changed, []);
  check("a changed answer rebuilds once", w.rebuilds() === 1, w.rebuilds());
  check("and the new question is on screen", /different question/.test(w.panel()),
        w.panel().slice(0, 300));

  w.reset();
  w.api.runGateSync(asks(6), []);
  check("a request leaving the pending set rebuilds once", w.rebuilds() === 1,
        w.rebuilds());
}

/* --- a batch of writes is one rebuild, and it shows the last state ------ */
{
  const w = build();
  w.api.noticePanelToggle(true);
  w.reset();
  w.api.noticePanelBatch(() => {
    w.api.runGateSync(asks(3), []);
    w.api.runGateSync(asks(5), []);
    // A nested batch (the panel's open pass calls one inside another) still
    // flushes only when the outermost ends.
    w.api.noticePanelBatch(() => { w.api.runGateSync(asks(7), []); });
  });
  check("a batch of three feeds rebuilds once", w.rebuilds() === 1, w.rebuilds());
  check("and the panel shows the last of them",
        (w.panel().match(/landing-review/g) || []).length >= 7,
        w.panel().slice(0, 200));
}

/* --- the countdown moves without a rebuild ------------------------------ */
{
  const w = build();
  w.api.noticePanelToggle(true);
  w.gate.restart = {
    id: "r1", session: "s9", reason: "serve master 1234abcd",
    requested_at: new Date(BASE).toISOString(),
    deadline: new Date(BASE + 300000).toISOString(),
    status: "pending", extensions: 0,
  };
  await w.api.refreshRestartGate();
  check("the row carries its countdown", /auto-approves in 5:00/.test(w.panel()),
        w.panel());
  w.clock.at += 61000;
  w.reset();
  w.api.noticeClocksPaint();
  check("the one-second tick moves the countdown", /auto-approves in 3:59/.test(w.panel()),
        w.panel());
  check("and rebuilds nothing", w.rebuilds() === 0, w.rebuilds());
}

/* --- opening always draws ---------------------------------------------- */
{
  const w = build();
  w.api.runGateSync(asks(3), []);
  w.api.noticePanelToggle(true);
  const first = w.panel();
  w.api.noticePanelToggle(false);
  w.reset();
  w.api.noticePanelToggle(true);
  check("closing and opening again draws the panel", w.rebuilds() === 1, w.rebuilds());
  check("and it comes back with its rows", w.panel() === first, [w.panel(), first]);
}

})().then(() => {
  if (failures) { console.log(`${failures} check(s) failed`); process.exitCode = 1; }
  else console.log("noticepaint_check: ok");
});
