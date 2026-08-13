/* The message trace's event list, run against the real functions in app.js.

   Everything the trace page draws is a drawing of what `msgEvents` returns,
   so this is where the claims live: that the order is the mesh's own
   ((epoch, seq), not a clock several machines disagree about), that what the
   focus session is not part of survives as context rather than being dropped,
   that a silence is a marker rather than a gap in the scrollbar, and that
   what the daemon says about arrival — who a '*' reached, who has actually
   had it typed in — is carried through to the row instead of being guessed
   at here. `msgLanes` is checked alongside it: the columns are derived from
   the same list, and an operator is not a member. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to, a + 1);
  if (a < 0 || b < 0 || b <= a) throw new Error(`cannot slice ${from} .. ${to}`);
  return src.slice(a, b);
}

const code = [
  slice("const TRACE_POLL_MS", "function stopMsgPoll"),
  slice("function traceMs(", "/* ---- geometry"),
].join("\n");

const ctx = {};
new Function(
  "exports",
  code + "\nObject.assign(exports, {msgEvents, msgLanes, traceCmp, traceFlowLabel});"
)(ctx);
const { msgEvents, msgLanes, traceCmp, traceFlowLabel } = ctx;

let failures = 0;
function check(what, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${what}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

const T = (s) => `2026-08-12T10:${String(s).padStart(2, "0")}:00Z`;
const msg = (over) => ({
  id: "m" + (over.seq === undefined ? 0 : over.seq),
  ts: T(0), from: "lead", to: "coder3", type: "say", epoch: 0, seq: 0,
  body: "hello", recipients: ["coder3"], delivered: [], remote: [], ...over,
});
const MEMBERS = [
  { handle: "lead", role: "leader", joined_at: T(0) },
  { handle: "coder3", role: "worker", joined_at: T(1), parent: "lead" },
  { handle: "reviewer", role: "reviewer", joined_at: T(2) },
];
const kinds = (evs) => evs.map((e) => e.kind).join(",");

/* --- order: the mesh's, not the clock's ------------------------------- */
{
  // ts deliberately disagrees with seq: a message written by another daemon
  // whose clock is behind must not jump the queue it was sequenced into.
  const out = msgEvents({
    handle: "coder3", members: [], gapMs: 0,
    messages: [
      msg({ id: "b", seq: 2, ts: T(1) }),
      msg({ id: "a", seq: 1, ts: T(9) }),
    ],
  });
  check("(epoch, seq) decides, not the timestamp",
        out.map((e) => e.msg.id).join() === "a,b", out.map((e) => e.msg.id));
}

{
  // An authority handover bumps the epoch; the old authority's late traffic
  // cannot interleave with the new one's, whatever its seq says.
  const out = msgEvents({
    handle: "coder3", members: [], gapMs: 0,
    messages: [
      msg({ id: "new", epoch: 1, seq: 0 }),
      msg({ id: "old", epoch: 0, seq: 99 }),
    ],
  });
  check("a later epoch outranks a higher seq",
        out.map((e) => e.msg.id).join() === "old,new", out.map((e) => e.msg.id));
  check("a message with no seq at all still lands",
        msgEvents({
          handle: "x", members: [], gapMs: 0,
          messages: [msg({ id: "p", seq: undefined, ts: T(5) })],
        }).length === 1);
}

/* --- the other events, merged in by the only clock they have ---------- */
{
  const out = msgEvents({
    handle: "coder3", members: MEMBERS, gapMs: 0,
    messages: [msg({ seq: 0, ts: T(3) }), msg({ id: "m2", seq: 1, ts: T(8) })],
    journal: [
      { at: T(5), event: "step_completed", step: "implement" },
      { at: T(6), event: "lock_taken" },        // bookkeeping: not a mark
    ],
  });
  check("joins open the room, then the traffic",
        kinds(out) === "join,join,join,msg,flow,msg", kinds(out));
  const flow = out.find((e) => e.kind === "flow");
  check("a workflow mark lands between the messages it sits between",
        out.indexOf(flow) === 4, out.indexOf(flow));
  check("it is drawn on the focus session's lane, the only run we fetched",
        flow.handle === "coder3", flow);
  check("bookkeeping the reader cannot act on is left out",
        out.filter((e) => e.kind === "flow").length === 1);
  check("a join carries what spawned it",
        out[1].handle === "coder3" && out[1].parent === "lead", out[1]);
}

/* --- silence, as a thing rather than as an absence -------------------- */
{
  const out = msgEvents({
    handle: "coder3", members: [],
    messages: [msg({ seq: 0, ts: T(0) }), msg({ id: "m2", seq: 1, ts: T(12) })],
  });
  check("a long quiet is folded into one marker",
        kinds(out) === "msg,gap,msg", kinds(out));
  check("and says how long it was",
        out[1].ms === 12 * 60 * 1000, out[1]);
  const busy = msgEvents({
    handle: "coder3", members: [],
    messages: [msg({ seq: 0, ts: T(0) }), msg({ id: "m2", seq: 1, ts: T(2) })],
  });
  check("a working exchange is not broken up", kinds(busy) === "msg,msg", kinds(busy));
}

/* --- whose message is it --------------------------------------------- */
{
  const out = msgEvents({
    handle: "coder3", members: MEMBERS, gapMs: 0,
    messages: [
      msg({ seq: 0, from: "lead", recipients: ["coder3"] }),
      msg({ id: "x", seq: 1, from: "lead", to: "reviewer", recipients: ["reviewer"] }),
      msg({ id: "o", seq: 2, from: "operator", to: "coder3", recipients: ["coder3"] }),
    ],
  }).filter((e) => e.kind === "msg");
  check("addressed to us: ours", out[0].mine === true, out[0]);
  check("between the others: kept, but not ours",
        out[1].mine === false, out[1]);
  check("the operator is not a member, and is marked as speaking from outside",
        out[2].external === true && out[2].mine === true, out[2]);
  check("a member's own message is not marked external",
        out[0].external === false, out[0]);
}

/* --- arrival: the daemon's answer, carried through -------------------- */
{
  const out = msgEvents({
    handle: "coder3", members: MEMBERS, gapMs: 0,
    messages: [msg({
      seq: 0, from: "lead", to: "*",
      recipients: ["coder3", "reviewer", "far"],
      delivered: ["coder3"], remote: ["far"],
    })],
  }).filter((e) => e.kind === "msg");
  check("'*' is drawn to the members the daemon resolved it to",
        out[0].to.join() === "coder3,reviewer,far", out[0].to);
  check("who has had it typed in survives", out[0].delivered.join() === "coder3");
  check("and who we cannot answer for is kept apart",
        out[0].remote.join() === "far", out[0].remote);
}

{
  // A daemon too old to annotate: '*' resolves to nobody we can name, and
  // that is drawn as "reaches nobody" rather than invented here.
  const out = msgEvents({
    handle: "coder3", members: MEMBERS, gapMs: 0,
    messages: [{ id: "u", ts: T(0), from: "lead", to: "*", seq: 0, body: "hi" }],
  }).filter((e) => e.kind === "msg");
  check("an unannotated broadcast claims no recipients",
        out[0].to.length === 0 && out[0].mine === false, out[0]);
  check("...and is flagged unresolved, so the row can say which silence it is",
        out[0].resolved === false, out[0]);
  const direct = msgEvents({
    handle: "coder3", members: MEMBERS, gapMs: 0,
    messages: [{ id: "d", ts: T(0), from: "lead", to: "coder3", seq: 0 }],
  }).filter((e) => e.kind === "msg");
  check("an unannotated direct message still reads off its address",
        direct[0].to.join() === "coder3" && direct[0].mine === true, direct[0]);
  const cut = msgEvents({
    handle: "coder3", members: MEMBERS, gapMs: 0,
    messages: [msg({ seq: 0, from: "lead", to: "*", recipients: [] })],
  }).filter((e) => e.kind === "msg");
  check("a resolved-to-nobody message is a different fact from an unasked one",
        cut[0].resolved === true && cut[0].to.length === 0, cut[0]);
}

/* --- unanswered, per message ------------------------------------------ */
{
  const out = msgEvents({
    handle: "coder3", members: MEMBERS, gapMs: 0,
    messages: [
      msg({ id: "asked", seq: 0, type: "ask", recipients: ["coder3", "reviewer"] }),
      msg({ id: "said", seq: 1 }),
    ],
    owed: { members: [
      { handle: "coder3", messages: [{ id: "asked", age: 240 }] },
      { handle: "reviewer", messages: [{ id: "asked", age: 240 }] },
    ] },
  }).filter((e) => e.kind === "msg");
  check("a debt is attached to the message that incurred it",
        out[0].debts.length === 2, out[0].debts);
  check("each debtor is named, with its age",
        out[0].debts[0].handle === "coder3" && out[0].debts[0].age === 240,
        out[0].debts);
  check("a message nobody owes on carries none", out[1].debts.length === 0);
}

/* --- the columns ------------------------------------------------------ */
{
  const events = msgEvents({
    handle: "coder3", members: MEMBERS, gapMs: 0,
    messages: [
      msg({ seq: 0, from: "operator", to: "lead", recipients: ["lead"] }),
      msg({ id: "m2", seq: 1, from: "lead", recipients: ["coder3"] }),
    ],
  });
  const lanes = msgLanes(events, "coder3");
  check("the outsider takes the first column, beside the mesh rather than in it",
        lanes[0].key === "operator" && lanes[0].outside === true, lanes[0]);
  check("then the members, in the order the room grew",
        lanes.map((l) => l.key).join() === "operator,lead,coder3,reviewer",
        lanes.map((l) => l.key));
  check("the session the page is about is marked, and only it",
        lanes.filter((l) => l.self).map((l) => l.key).join() === "coder3");
  check("a member carries its role into its head",
        lanes[1].role === "leader", lanes[1]);
}

{
  // Nothing said, nothing joined: the page is still about a session, and a
  // trace with no lane for it would be a page about nobody.
  const lanes = msgLanes([], "coder3");
  check("the focus session always has a lane",
        lanes.length === 1 && lanes[0].key === "coder3" && lanes[0].self === true,
        lanes);
}

/* --- the vocabulary of a run ------------------------------------------ */
{
  const cases = [
    [{ event: "step_completed", step: "build" }, "done: build"],
    [{ event: "step_report", summary: "all green" }, "report: all green"],
    [{ event: "done" }, "run finished"],
    [{ event: "aborted" }, "run aborted"],
    [{ event: "gate_wait", step: "ship" }, "gate: ship"],
    [{ event: "lock_taken" }, ""],
    [{ event: "request_superseded" }, ""],
  ];
  for (const [entry, label] of cases) {
    check(`journal: ${entry.event}`, traceFlowLabel(entry) === label,
          traceFlowLabel(entry));
  }
}

/* --- the comparator on its own ---------------------------------------- */
{
  check("equal position compares equal",
        traceCmp({ epoch: 0, seq: 3, ts: T(0) }, { epoch: 0, seq: 3, ts: T(0) }) === 0);
}

if (failures) {
  console.log(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("seq_check: ok");
