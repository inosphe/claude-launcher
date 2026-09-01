/* The goto-gate card, run against a stub daemon.

   A leader session's request to move a descendant's cflow run waits on a
   person (daemon/goto_gate.py), and this card is where that person settles
   it. Unlike the restart gate there can be several requests at once — one
   per run — so what is checked here is the set discipline: one card per
   pending request, redrawn in place rather than stacked; the card names who
   asked, which run, from where to where, and prints the reason in full (it
   is the whole basis for the click); the countdown moves with the clock and
   clamps at zero; the buttons POST to approve/deny and only a successful
   POST settles the card; a settled or gone request retires its own card
   while a still-pending neighbour keeps its; and a stray click anywhere on
   the card decides nothing. Time and the network belong to the harness, so
   none of it is waited for. */
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

/* ---- stub world ------------------------------------------------------- */
function build() {
  function node(cls, text) {
    const n = {
      className: cls || "", textContent: text === undefined ? "" : text,
      title: "", children: [], parentNode: null, on: {},
      disabled: false,
      classList: {
        contains: (c) => n.className.split(" ").includes(c),
      },
      appendChild: (c) => { c.parentNode = n; n.children.push(c); return c; },
      removeChild: (c) => {
        const i = n.children.indexOf(c);
        if (i >= 0) n.children.splice(i, 1);
        c.parentNode = null;
      },
      addEventListener: (ev, fn) => { n.on[ev] = fn; },
      click: (ev) => n.on.click && n.on.click(ev || { stopPropagation() {} }),
      querySelector: (sel) => {
        if (sel.startsWith(".")) {
          const cls = sel.slice(1);
          return n.children.find((c) => c.classList.contains(cls)) || null;
        }
        return null;
      },
      isConnected: true,
      text: () => [n.textContent, ...n.children.map((c) => c.text())].join(" "),
    };
    return n;
  }

  const nodes = { notices: node() };

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
    getTime() { return typeof this.ms === "number" ? this.ms : Date.parse(this.ms); }
    valueOf() { return typeof this.ms === "number" ? this.ms : 1; }
  }
  FakeDate.now = () => clock.at;

  const apiCalls = [];
  // What the stub daemon answers on the goto-gate endpoints: `recs` is the
  // list the GET returns; the failure flags make a POST refuse.
  const gate = { recs: [], approveFail: false, denyFail: false };
  const apiStub = async (p) => {
    apiCalls.push(p);
    if (p === "/api/cflow/goto-requests") {
      return { json: async () => ({ requests: JSON.parse(JSON.stringify(gate.recs)) }) };
    }
    const m = p.match(/^\/api\/cflow\/goto-requests\/([^/]+)\/(approve|deny)$/);
    if (m) {
      if (m[2] === "approve" && gate.approveFail) throw new Error("refused");
      if (m[2] === "deny" && gate.denyFail) throw new Error("refused");
      return { json: async () => ({ ok: true }) };
    }
    throw new Error(`unexpected api call ${p}`);
  };

  const code = slice("/* notices — the page's own voice",
                     "pollTimer = setInterval(pollTick, DASHBOARD_POLL_MS);");
  const api = new Function(
    "$", "el", "setTimeout", "clearTimeout", "Date", "api",
    code + "\nreturn { refreshGotoGate," +
    " get cards() { return [...notices.keys()]; } };"
  )(
    (id) => nodes[id],
    (tag, cls, text) => node(cls, text),
    setTimeoutStub, clearTimeoutStub, FakeDate, apiStub,
  );

  return { api, nodes, clock, gate, apiCalls,
           strip: () => nodes.notices.children,
           said: () => nodes.notices.children.map((c) => c.text()).join(" | ") };
}

function pending(rec) {
  return { id: "g1", session: "lead", target_session: "w1",
           from: "landed", step: "wrapup",
           reason: "branch verified landed by git",
           requested_at: new Date(1700000000000).toISOString(),
           deadline: new Date(1700000000000 + 300000).toISOString(),
           status: "pending", ...(rec || {}) };
}

/* --- one pending request draws exactly one card ------------------------- */
{
  const w = build();
  w.gate.recs = [pending()];
  (async () => {
    await w.api.refreshGotoGate();
    check("a pending request draws one card", w.strip().length === 1, w.said());
    check("titled with the run being moved",
          /run move requested: w1/.test(w.said()), w.said());
    check("naming who asked and the move", /lead asks to move w1's run landed → wrapup/.test(w.said()),
          w.said());
    check("printing the reason in full", /branch verified landed by git/.test(w.said()), w.said());
    check("with a countdown to the deadline", /auto-approves in 5:00/.test(w.said()), w.said());
    const card = w.strip()[0];
    const actions = card.children.find((c) => c.className.includes("gate-actions"));
    check("an Approve button", actions.children.some((b) => b.textContent === "Approve"),
          actions.children.map((b) => b.textContent));
    check("a Deny button", actions.children.some((b) => b.textContent === "Deny"),
          actions.children.map((b) => b.textContent));

    await w.api.refreshGotoGate();
    check("the next poll does not stack a second card", w.strip().length === 1, w.said());

    w.clock.at += 61000;
    await w.api.refreshGotoGate();
    check("the countdown moves with the clock", /auto-approves in 3:59/.test(w.said()), w.said());

    w.clock.at += 3600000;                 // past the deadline
    await w.api.refreshGotoGate();
    check("and clamps at zero instead of counting negative",
          /auto-approves in 0:00/.test(w.said()), w.said());
  })();
}

/* --- two runs gated at once: one card each, retired apart --------------- */
{
  const w = build();
  w.gate.recs = [pending(), pending({ id: "g2", target_session: "w2" })];
  (async () => {
    await w.api.refreshGotoGate();
    check("two pending requests draw two cards", w.strip().length === 2, w.said());
    await w.api.refreshGotoGate();
    check("and a redraw keeps them at two", w.strip().length === 2, w.said());

    w.gate.recs = [pending({ status: "approved" }), pending({ id: "g2", target_session: "w2" })];
    await w.api.refreshGotoGate();
    check("a settled request retires its own card",
          w.strip().length === 1, w.said());
    check("while the still-pending one keeps its card",
          /w2/.test(w.said()), w.said());
  })();
}

/* --- a stray click decides nothing -------------------------------------- */
{
  const w = build();
  w.gate.recs = [pending()];
  (async () => {
    await w.api.refreshGotoGate();
    w.strip()[0].click();
    check("clicking the card itself decides nothing",
          w.strip().length === 1 &&
          w.apiCalls.every((p) => !p.includes("approve") && !p.includes("deny")),
          w.said());
  })();
}

/* --- approve: POST and retire; a refused POST keeps the card ------------- */
{
  const w = build();
  w.gate.recs = [pending()];
  (async () => {
    await w.api.refreshGotoGate();
    const actions = w.strip()[0].children.find((c) => c.className.includes("gate-actions"));
    const approve = actions.children.find((b) => b.textContent === "Approve");

    w.gate.approveFail = true;
    await approve.click();
    check("a refused approval keeps the card", w.strip().length === 1, w.said());
    check("and re-enables the button", approve.disabled === false);

    w.gate.approveFail = false;
    await approve.click();
    check("Approve posts the approval",
          w.apiCalls.includes("/api/cflow/goto-requests/g1/approve"), w.apiCalls);
    check("and retires the card", w.strip().length === 0, w.said());
  })();
}

/* --- deny: POST and retire ------------------------------------------------ */
{
  const w = build();
  w.gate.recs = [pending()];
  (async () => {
    await w.api.refreshGotoGate();
    const actions = w.strip()[0].children.find((c) => c.className.includes("gate-actions"));
    const deny = actions.children.find((b) => b.textContent === "Deny");
    await deny.click();
    check("Deny posts the denial",
          w.apiCalls.includes("/api/cflow/goto-requests/g1/deny"), w.apiCalls);
    check("and retires the card", w.strip().length === 0, w.said());

    w.gate.recs = [pending({ status: "denied" })];
    await w.api.refreshGotoGate();
    check("a settled request never comes back", w.strip().length === 0, w.said());
  })();
}

if (failures) {
  console.log(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("gotogate_check: ok");
