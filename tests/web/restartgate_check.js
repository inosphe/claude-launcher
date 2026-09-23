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
   web button shows; Extend moves the deadline without settling anything and
   retires itself once the budget is spent; a settled or gone request retires
   the card; and a stray click anywhere on the card decides nothing. Time and the network
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
      // `isConnected` is what decides whether a poll REUSES the card or
      // builds a new one. Without it every poll rebuilt the card, so the
      // buttons a previous poll handed out went stale -- and a check holding
      // one of them read the state of a node the page had already dropped.
      // A card is connected while something still holds it.
      get isConnected() { return n.parentNode !== null; },
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
      // The card is re-found by class each poll: its sub-line to repaint the
      // countdown, its Extend button to repaint the budget. The real
      // querySelector searches DESCENDANTS, and the button lives one level
      // down inside the action row -- a stub that looked only at direct
      // children would report it missing and pass code that never runs in a
      // browser. Depth-first, first match, like the real one.
      querySelector: (sel) => {
        if (!sel.startsWith(".")) return null;
        const cls = sel.slice(1);
        for (const c of n.children) {
          if (c.classList.contains(cls)) return c;
          const deeper = c.querySelector(sel);
          if (deeper) return deeper;
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
  const gate = { rec: null, approveFail: false, rejectFail: false,
                 extendFail: false };
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
    if (p === "/api/daemon/restart-request/extend") {
      if (gate.extendFail) throw new Error("refused");
      // The daemon moves the deadline and leaves the request pending; the
      // next poll is what the card repaints from, exactly as in the browser.
      gate.rec = { ...gate.rec,
                   extensions: (gate.rec.extensions || 0) + 1,
                   deadline: new Date(clockBase() + 600000).toISOString() };
      return { json: async () => ({ ok: true, extended: true, request: gate.rec }) };
    }
    return { json: async () => ({ version: daemon.version, uptime: daemon.uptime, relay: null }) };
  };

  const code = slice("/* notices — the page's own voice",
                     "pollTimer = setInterval(pollTick, DASHBOARD_POLL_MS);");
  const api = new Function(
    "$", "el", "setTimeout", "clearTimeout", "Date", "api", "daemonHealth",
    "renderRelayBadge", "refreshProfiles", "refreshHarnesses", "refreshRoles",
    "refreshWorkspaces", "refreshSessions", "refreshMeshList", "refreshCflow",
    "refreshTermQueued", "reconnectNow", "route",
    // The drain check below runs boot() and pollOnce() whole; these are
    // other checks' subjects (notice_check lists the same), here they only
    // have to exist.
    "refreshNewWorktree", "refreshProjects", "ensureControlSocket",
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
    () => {}, () => {}, () => {},
  );

  return { api, nodes, clock, gate, apiCalls, daemon,
           strip: () => nodes.notices.children,
           said: () => nodes.notices.children.map((c) => c.text()).join(" | ") };
}

function pending(rec) {
  return { id: "g1", session: "s9",
           requested_at: new Date(clockBase()).toISOString(),
           deadline: new Date(clockBase() + 300000).toISOString(),
           status: "pending", extensions: 0, max_extensions: 2,
           ...(rec || {}) };
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
    check("an Extend button", actions.children.some((b) => b.textContent === "+5 min"),
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

/* --- approve, then the drain: the old daemon still answers for a while --
   The approval POST returns before the old process stops: it finishes the
   reply, drains its sessions (beads wind-down included), and only then
   lets go of the port. Polls in that window reach the SAME boot id. They
   used to read as "the link came back" and posted "daemon back", and the
   real stop that followed posted "daemon offline" — two cards, in reverse
   order, about an outage the person had just asked for. The only event in
   a requested restart is the successor's boot id. */
{
  const w = build();
  (async () => {
    await w.api.pollOnce();               // first poll seeds the page: booted, boot b1
    w.gate.rec = pending();
    await w.api.refreshRestartGate();
    const actions = w.strip()[0].children.find((c) => c.className.includes("gate-actions"));
    await actions.children.find((b) => b.textContent === "Approve").click();

    w.gate.rec = pending({ status: "approved" });
    await w.api.pollOnce();               // old daemon, still draining
    check("a poll during the drain does not announce the daemon back",
          !/daemon back/.test(w.said()), w.said());
    check("the restarting card stays up through the drain",
          /restarting the daemon/.test(w.said()), w.said());
    check("and so does the badge",
          w.nodes["daemon-info"].textContent === "restarting…",
          w.nodes["daemon-info"].textContent);

    w.daemon.up = false;                  // the old process lets go
    await w.api.pollOnce();
    check("the requested stop is not reported as an outage",
          !/daemon offline/.test(w.said()), w.said());
    check("the restarting card is still what the strip says",
          /restarting the daemon/.test(w.said()), w.said());

    w.daemon.up = true;                   // the successor answers
    w.daemon.boot = "b2";
    await w.api.pollOnce();
    check("the successor is announced as a restart",
          /daemon restarted/.test(w.said()), w.said());
    check("and the restarting card is gone with the gap it labelled",
          !/restarting the daemon/.test(w.said()), w.said());
    check("with no back/offline card left over from the drain",
          !/daemon back|daemon offline/.test(w.said()), w.said());

    // After the restart the page is an ordinary page again: a real outage
    // of the successor is an outage and is said as one.
    w.daemon.up = false;
    await w.api.pollOnce();
    check("a later outage is reported again",
          /daemon offline/.test(w.said()), w.said());
  })();
}

/* --- approve, and the old daemon never goes: the wait is bounded --------- */
{
  const w = build();
  (async () => {
    await w.api.pollOnce();
    w.gate.rec = pending();
    await w.api.refreshRestartGate();
    const actions = w.strip()[0].children.find((c) => c.className.includes("gate-actions"));
    await actions.children.find((b) => b.textContent === "Approve").click();
    w.gate.rec = null;
    await w.api.pollOnce();
    check("inside the drain window the same daemon is still the drain",
          /restarting the daemon/.test(w.said()), w.said());
    w.clock.at += 5 * 60 * 1000;          // the drain bound has passed
    await w.api.pollOnce();
    check("past the bound the same daemon is said to be back, unrestarted",
          /daemon back/.test(w.said()) && /never\s+restarted/.test(w.said()), w.said());
    check("and the page is online again", w.api.online === true, w.api.online);
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

/* --- extend: buys time, decides nothing --------------------------------- */
{
  const w = build();
  w.gate.rec = pending();
  (async () => {
    await w.api.refreshRestartGate();
    const actions = w.strip()[0].children.find((c) => c.className.includes("gate-actions"));
    const extend = actions.children.find((b) => b.textContent === "+5 min");
    await extend.click();
    check("Extend posts the extension", w.apiCalls.includes("/api/daemon/restart-request/extend"),
          w.apiCalls);
    check("and settles nothing — the card stays up", w.strip().length === 1, w.said());
    check("nothing was approved or rejected by it",
          w.apiCalls.every((p) => !p.includes("approve") && !p.includes("reject")),
          w.apiCalls);

    await w.api.refreshRestartGate();
    check("the countdown repaints from the moved deadline",
          /auto-approves in 10:00/.test(w.said()), w.said());
    check("and the card says how much of the budget is spent",
          /extended 1\/2/.test(w.said()), w.said());
    check("the button comes back for the second press",
          extend.disabled === false, extend.disabled);
  })();
}

/* --- extend: the last press retires the button -------------------------- */
{
  const w = build();
  w.gate.rec = pending({ extensions: 2 });
  (async () => {
    await w.api.refreshRestartGate();
    const actions = w.strip()[0].children.find((c) => c.className.includes("gate-actions"));
    const extend = actions.children.find((b) => b.className.includes("gate-extend"));
    check("a spent budget disables the button", extend.disabled === true, extend.disabled);
    check("and says so rather than offering more time",
          extend.textContent === "extended", extend.textContent);
    check("while Approve and Reject stay live",
          actions.children.filter((b) => b.disabled).length === 1,
          actions.children.map((b) => [b.textContent, b.disabled]));
  })();
}

/* --- a refused extension leaves the deadline alone ---------------------- */
{
  const w = build();
  w.gate.rec = pending();
  w.gate.extendFail = true;
  (async () => {
    await w.api.refreshRestartGate();
    const actions = w.strip()[0].children.find((c) => c.className.includes("gate-actions"));
    const extend = actions.children.find((b) => b.className.includes("gate-extend"));
    await extend.click();
    check("a refused extension leaves the card standing", w.strip().length === 1, w.said());
    check("and the countdown where it was", /auto-approves in 5:00/.test(w.said()), w.said());
    await w.api.refreshRestartGate();
    check("with the button available again", extend.disabled === false, extend.disabled);
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
