/* The restart-gate card, run against a stub daemon.

   A managed session's `claunch daemon restart` no longer restarts on the
   spot: it opens a gate in the daemon, and this card is where a person
   settles it (daemon/restart_gate.py). The card's job is to hold a single
   request — one card, a live countdown, a decision that is a deliberate
   button press rather than a stray click — and to hand the outcome to the
   same recovery the web button's restart uses.

   So what is checked here is not "does a card appear" but the things that
   make the card safe to have: a pending request draws exactly one card and
   redraws in place as the poll answers; the countdown moves with the clock
   and clamps at zero; the buttons POST to approve/reject and only a
   successful POST settles the card (a refused one re-enables the button);
   approving hands the gap to the same "restarting the daemon" notice the
   web button shows; a settled or gone request retires the card; and a
   stray click anywhere on the card decides nothing. Time and the network
   belong to the harness, so none of it is waited for. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web",
                         "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

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

/* ---- stub world ------------------------------------------------------- */
function build(opts) {
  const o = opts || {};

  function node(cls, text) {
    const n = {
      className: cls || "", textContent: text === undefined ? "" : text,
      title: "", children: [], parentNode: null, on: {},
      disabled: false,
      classList: {
        contains: (c) => n.className.split(" ").includes(c),
        toggle: (c, on) => {
          const has = n.classList.contains(c);
          if (on === undefined ? has : !on) {
            n.className = n.className.split(" ").filter((x) => x !== c).join(" ");
          } else if (!has) { n.className = `${n.className} ${c}`.trim(); }
        },
        remove: (c) => {
          n.className = n.className.split(" ").filter((x) => x !== c).join(" ");
        },
      },
      appendChild: (c) => { c.parentNode = n; n.children.push(c); return c; },
      removeChild: (c) => {
        const i = n.children.indexOf(c);
        if (i >= 0) n.children.splice(i, 1);
        c.parentNode = null;
      },
      addEventListener: (ev, fn) => { n.on[ev] = fn; },
      // The handler receives a real-ish event; a decision that swallows
      // stopPropagation must not care. click() on a node with no listener
      // is a no-op, which is itself a thing tested below.
      click: (ev) => n.on.click && n.on.click(ev || { stopPropagation() {} }),
      // The card's countdown is updated by re-finding its sub-line; the
      // stub knows only class selectors.
      querySelector: (sel) => {
        if (sel.startsWith(".")) {
          const cls = sel.slice(1);
          return n.children.find((c) => c.classList.contains(cls)) || null;
        }
        return null;
      },
      // what a card is, read back as one string
      text: () => [n.textContent, ...n.children.map((c) => c.textContent)].join(" "),
    };
    return n;
  }

  const nodes = {
    notices: node(),
    "daemon-info": node(),
    "auth-overlay": node("hidden"),
  };

  const timers = [];
  let ticket = 0;
  const setTimeoutStub = (fn, ms) => { timers.push({ id: ++ticket, fn, ms }); return ticket; };
  const clearTimeoutStub = (id) => {
    const i = timers.findIndex((t) => t.id === id);
    if (i >= 0) timers.splice(i, 1);
  };

  const clock = { at: 1700000000000 };
  class FakeDate {
    constructor(ms) { this.ms = ms === undefined ? clock.at : ms; }
    toLocaleTimeString() { return `T${this.ms}`; }
    // Gate deadlines arrive as ISO strings; Date.parse is real node, so a
    // fixed string is a fixed number, and the countdown then compares it
    // against FakeDate.now (the harness clock).
    getTime() { return typeof this.ms === "number" ? this.ms : Date.parse(this.ms); }
    // isNaN() is how app.js tells an unparseable instant apart; a string it
    // cannot parse is still "a time it was given".
    valueOf() { return typeof this.ms === "number" ? this.ms : 1; }
  }
  FakeDate.now = () => clock.at;

  const daemon = { up: o.up !== false, boot: "b1", version: "9.9", uptime: 42 };
  const daemonHealth = async () =>
    (daemon.up ? { status: "ok", version: daemon.version, boot_id: daemon.boot,
                   started_at: "t-start-1" } : null);
  const apiCalls = [];
  // What the stub daemon would answer on the gate endpoints. The test sets
  // `rec` to a pending/settled record or null, and the failure flags to
  // make a POST refuse.
  const gate = { rec: null, approveFail: false, rejectFail: false };
  const apiStub = async (p) => {
    apiCalls.push(p);
    if (!daemon.up) throw new Error("down");
    if (p === "/api/daemon/restart-request") {
      return { json: async () => ({ request: gate.rec ? JSON.parse(JSON.stringify(gate.rec)) : null }) };
    }
    if (p === "/api/daemon/restart-request/approve") {
      if (gate.approveFail) throw new Error("refused");
      return { json: async () => ({ ok: true, restarting: true }) };
    }
    if (p === "/api/daemon/restart-request/reject") {
      if (gate.rejectFail) throw new Error("refused");
      return { json: async () => ({ ok: true, rejected: true }) };
    }
    return { json: async () => ({ version: daemon.version, uptime: daemon.uptime, relay: null }) };
  };

  const code = slice("/* notices — the page's own voice",
                     "pollTimer = setInterval(pollTick, 2000);");
  const api = new Function(
    "$", "el", "setTimeout", "clearTimeout", "Date", "api", "daemonHealth",
    "renderRelayBadge", "refreshProfiles", "refreshHarnesses", "refreshRoles",
    "refreshWorkspaces", "refreshSessions", "refreshMeshList", "refreshCflow",
    "refreshTermQueued", "reconnectNow", "route",
    code +
    "\nreturn {notify, dismissNotice, noticeClock, setDaemonOnline, " +
    "refreshRestartGate, pollOnce, boot," +
    " get cards() { return [...notices.keys()]; }," +
    " get gateKey() { return NOTICE_GATE; }," +
    " get online() { return daemonOnline; }};"
  )(
    (id) => nodes[id],
    (tag, cls, text) => node(cls, text),
    setTimeoutStub, clearTimeoutStub, FakeDate, apiStub, daemonHealth,
    () => {}, () => {}, () => {}, () => {}, () => {}, () => {}, () => {},
    () => {}, () => {}, () => {}, () => {},
  );

  return { api, nodes, clock, gate, apiCalls,
           strip: () => nodes.notices.children,
           said: () => nodes.notices.children.map((c) => c.text()).join(" | ") };
}

function pending(rec) {
  return { id: "g1", session: "s9",
           requested_at: new Date(clockBase()).toISOString(),
           deadline: new Date(clockBase() + 300000).toISOString(),
           status: "pending", ...(rec || {}) };
}
const clockBase = () => 1700000000000;

/* --- one pending request draws exactly one card ------------------------ */
{
  const w = build();
  w.gate.rec = pending();
  (async () => {
    await w.api.refreshRestartGate();
    check("a pending request draws one card", w.strip().length === 1, w.said());
    check("titled as a restart waiting on a person",
          /restart requested/.test(w.said()), w.said());
    check("naming the session that asked", w.said().includes("s9"), w.said());
    check("with a countdown to the deadline", /auto-approves in 5:00/.test(w.said()), w.said());
    const card = w.strip()[0];
    const actions = card.children.find((c) => c.className.includes("gate-actions"));
    check("a card with an action row", !!actions, card.children.length);
    check("an Approve button", actions.children.some((b) => b.textContent === "Approve"),
          actions.children.map((b) => b.textContent));
    check("a Reject button", actions.children.some((b) => b.textContent === "Reject"),
          actions.children.map((b) => b.textContent));

    await w.api.refreshRestartGate();
    check("the next poll does not stack a second card", w.strip().length === 1, w.said());

    w.clock.at += 61000;
    await w.api.refreshRestartGate();
    check("the countdown moves with the clock", /auto-approves in 3:59/.test(w.said()), w.said());

    w.clock.at += 3600000;                 // past the deadline
    await w.api.refreshRestartGate();
    check("and clamps at zero instead of counting negative",
          /auto-approves in 0:00/.test(w.said()), w.said());
  })();
}

/* --- a stray click decides nothing ------------------------------------- */
{
  const w = build();
  w.gate.rec = pending();
  (async () => {
    await w.api.refreshRestartGate();
    w.strip()[0].click();
    check("clicking the card itself decides nothing",
          w.strip().length === 1 && w.apiCalls.every((p) => !p.includes("approve") && !p.includes("reject")),
          w.said());
  })();
}

/* --- reject: settle the gate, restart nothing --------------------------- */
{
  const w = build();
  w.gate.rec = pending();
  (async () => {
    await w.api.refreshRestartGate();
    const actions = w.strip()[0].children.find((c) => c.className.includes("gate-actions"));
    const reject = actions.children.find((b) => b.textContent === "Reject");
    await reject.click();
    check("Reject posts the rejection", w.apiCalls.includes("/api/daemon/restart-request/reject"),
          w.apiCalls);
    check("and retires the card", w.strip().length === 0, w.said());

    w.gate.rec = pending({ status: "rejected" });
    await w.api.refreshRestartGate();
    check("a settled request never comes back", w.strip().length === 0, w.said());
  })();
}

/* --- approve: the daemon's own door, labelled like the button's --------- */
{
  const w = build();
  w.gate.rec = pending();
  (async () => {
    await w.api.refreshRestartGate();
    const actions = w.strip()[0].children.find((c) => c.className.includes("gate-actions"));
    const approve = actions.children.find((b) => b.textContent === "Approve");
    await approve.click();
    check("Approve posts the approval", w.apiCalls.includes("/api/daemon/restart-request/approve"),
          w.apiCalls);
    check("the gate card is gone", w.strip().length === 1, w.said());
    check("replaced by the restarting announcement the web button uses",
          /restarting the daemon/.test(w.said()), w.said());
    check("and the badge says what is happening",
          w.nodes["daemon-info"].textContent === "restarting…",
          w.nodes["daemon-info"].textContent);
  })();
}

/* --- a refused POST settles nothing -------------------------------------- */
{
  const w = build();
  w.gate.rec = pending();
  w.gate.rejectFail = true;
  (async () => {
    await w.api.refreshRestartGate();
    const actions = w.strip()[0].children.find((c) => c.className.includes("gate-actions"));
    const reject = actions.children.find((b) => b.textContent === "Reject");
    await reject.click();
    check("a refused decision leaves the card standing",
          w.strip().length === 1, w.said());
    check("and re-enables the button for a second try",
          reject.disabled === false, reject.disabled);
  })();
}

/* --- no request: nothing to show ---------------------------------------- */
{
  const w = build();
  (async () => {
    await w.api.refreshRestartGate();
    check("a daemon with no pending request shows nothing",
          w.strip().length === 0, w.said());
  })();
}

/* --- the markup and the stylesheet the code reaches for ----------------- */
check("pollOnce feeds the gate card", /refreshRestartGate\(\);/.test(src));
check("the card is a notice with buttons, not a click-to-dismiss toast",
      /"notice warn gate"/.test(src) && /gate-actions/.test(src));
check("a stray click is not the cursor of a decision",
      /\.notice\.gate\s*\{[^}]*cursor:\s*default/.test(css));
check("the buttons sit in their own row under the card text",
      /\.gate-actions\s*\{[^}]*display:\s*flex/.test(css));

process.on("exit", (code) => {
  if (failures) { console.log(`${failures} check(s) failed`); process.exitCode = 1; }
  else if (!code) console.log("all restart-gate checks passed");
});
