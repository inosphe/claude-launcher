/* The leader-fault poll (claunch-9djny), run against the real functions.

   The daemon decides which sessions count (a leader role, a category of
   running or hold, an active fault) and this poll only draws them. What has
   to hold here: an alert becomes one fatal card that names the session and
   cannot be dismissed while it is up; polling again raises nothing new; a
   card is re-raised only when what it says changed; the card comes down when
   the daemon stops listing the row (the fault cleared, or the session was
   paused, killed or archived); a kind the reader hid raises nothing; and a
   failed request leaves the cards that are up alone rather than taking them
   down. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(name) {
  let start = src.indexOf(`async function ${name}(`);
  // The cut must keep `async` with the body: an `await` inside a bare
  // `function` is a syntax error, not a slow call.
  if (start < 0) start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

const FAIL = [];
function is(ok, what) {
  if (!ok) FAIL.push(what);
}

function alert(session, over) {
  return Object.assign({
    session, handle: session, meshes: ["claunch"], category: "running",
    fault: {
      kind: "usage_limit", label: "usage limit", harness: "claude",
      message: "You've hit your weekly limit",
      until: "2026-10-28T00:00:00+00:00", resolved_by: "account",
    },
  }, over || {});
}

function harness() {
  let rows = [];
  let hidden = false;
  let failing = false;
  const raised = [];
  const dismissed = [];
  const ctx = {
    LEADER_FAULT_PREFIX: "leader-fault-",
    leaderFaultRaised: new Map(),
    noticeSourceShown: () => !hidden,
    notify: (title, sub, opts) => { raised.push({ title, sub, opts }); return {}; },
    dismissNotice: (key) => { dismissed.push(key); },
    api: async () => {
      if (failing) throw new Error("no daemon");
      return { ok: true, json: async () => ({ alerts: rows, count: rows.length }) };
    },
  };
  vm.createContext(ctx);
  vm.runInContext(slice("leaderFaultSub"), ctx);
  vm.runInContext(slice("refreshLeaderFaults"), ctx);
  return {
    ctx,
    raised, dismissed,
    set(rowsIn) { rows = rowsIn; },
    hide(v) { hidden = v; },
    fail(v) { failing = v; },
    run: () => ctx.refreshLeaderFaults(),
  };
}

async function cards() {
  const h = harness();
  h.set([alert("s29")]);
  await h.run();
  is(h.raised.length === 1, `one alert should raise one card, raised ${h.raised.length}`);
  const card = h.raised[0] || {};
  is(/leader stopped/.test(card.title || ""), `title names the alert: ${card.title}`);
  is(/s29/.test(card.title || ""), `title names the session: ${card.title}`);
  is(/weekly limit/.test(card.sub || ""), `sub carries the fault message: ${card.sub}`);
  is(/usage limit/.test(card.sub || ""), `sub carries the kind: ${card.sub}`);
  is((card.opts || {}).kind === "fatal", `kind is fatal: ${(card.opts || {}).kind}`);
  is((card.opts || {}).sticky === true, "a fatal card never times out");
  is((card.opts || {}).dismissable === false, "a fatal card has no x");
  is((card.opts || {}).session === "s29", "the card opens the session");
  is((card.opts || {}).key === "leader-fault-s29", `card key: ${(card.opts || {}).key}`);
  is((card.opts || {}).source === "fault", `card source: ${(card.opts || {}).source}`);
}

async function steady() {
  const h = harness();
  h.set([alert("s29")]);
  await h.run();
  await h.run();
  await h.run();
  is(h.raised.length === 1,
     `re-polling an unchanged alert must not raise again, raised ${h.raised.length}`);
  is(h.dismissed.length === 0, "nothing is taken down while the alert stands");
}

async function changed() {
  const h = harness();
  h.set([alert("s29")]);
  await h.run();
  h.set([alert("s29", { fault: Object.assign({}, alert("s29").fault, {
    message: "You've hit your weekly limit · resets Oct 28, 9am",
  }) })]);
  await h.run();
  is(h.raised.length === 2, `a changed message is re-raised, raised ${h.raised.length}`);
  h.set([alert("s29", { category: "hold" })]);
  await h.run();
  is(h.raised.length === 3, `a changed category is re-raised, raised ${h.raised.length}`);
}

async function cleared() {
  const h = harness();
  h.set([alert("s29")]);
  await h.run();
  h.set([]);
  await h.run();
  is(h.dismissed.length === 1, `a cleared alert takes its card down, dismissed ${h.dismissed}`);
  is(h.dismissed[0] === "leader-fault-s29", `dismissed the right key: ${h.dismissed[0]}`);
  is(h.ctx.leaderFaultRaised.size === 0, "the raised map forgets a cleared alert");
  h.set([alert("s29")]);
  await h.run();
  is(h.raised.length === 2, "the same alert coming back raises again");
}

async function many() {
  const h = harness();
  h.set([alert("s29"), alert("s77", { handle: "lead" })]);
  await h.run();
  is(h.raised.length === 2, `two leaders raise two cards, raised ${h.raised.length}`);
  h.set([alert("s77", { handle: "lead" })]);
  await h.run();
  is(h.dismissed.length === 1 && h.dismissed[0] === "leader-fault-s29",
     `only the row that went away is taken down: ${h.dismissed}`);
}

async function hidden() {
  const h = harness();
  h.hide(true);
  h.set([alert("s29")]);
  await h.run();
  is(h.raised.length === 0, "a hidden kind raises no card");
  h.hide(false);
  await h.run();
  is(h.raised.length === 1, "unhiding raises the alert on the next poll");
}

async function refused() {
  const h = harness();
  h.set([alert("s29")]);
  await h.run();
  h.fail(true);
  await h.run();
  await h.run();
  is(h.dismissed.length === 0, "a failed request leaves the card up");
  is(h.raised.length === 1, "a failed request raises nothing either");
}

(async () => {
  await cards();
  await steady();
  await changed();
  await cleared();
  await many();
  await hidden();
  await refused();
  if (FAIL.length) {
    console.error("FAIL");
    for (const line of FAIL) console.error(`  - ${line}`);
    process.exit(1);
  }
  console.log("ok");
})();
