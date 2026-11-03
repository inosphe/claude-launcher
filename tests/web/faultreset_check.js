/* The usage-limit pause poll (claunch-ijot0, claunch-9djny.1), run against
   the real functions.

   The daemon lists the sessions it paused on a usage limit (GET
   /api/fault-resets): `pending` while the stated reset is still ahead,
   `resets` once it has passed. This poll draws them. What has to hold: a row
   becomes one card that names the session, can be put away with x and opens
   the session; polling again raises nothing new; the same card is redrawn --
   not duplicated -- with the "decide" wording when its reset passes; a new
   reset (another until) raises again; the card comes down when the row goes
   (resumed, killed, archived); a card put away in another browser raises
   nothing and adds no history row; with the history panel open nothing is
   recorded. */
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

const UNTIL = "2026-11-02T00:00:00+00:00";

function row(session, until) {
  return {
    session, paused_at: "2026-10-30T04:00:00+00:00",
    fault: {
      kind: "usage_limit", label: "usage limit", harness: "claude",
      until: until || UNTIL, until_stated: true,
    },
  };
}

function harness() {
  let pending = [];
  let due = [];
  let panel = false;
  const remote = new Map();
  const raised = [];
  const dismissed = [];
  const ctx = {
    FAULT_RESET_PREFIX: "fault-reset-",
    faultResetDrawn: new Map(),
    noticeSync: { remote },
    noticeSourceShown: () => true,
    noticePanelOpen: () => panel,
    notify: (title, sub, opts) => { raised.push({ title, sub, opts }); return {}; },
    dismissNotice: (key) => { dismissed.push(key); },
    api: async () => ({
      ok: true,
      json: async () => ({
        resets: due, pending, count: due.length, pending_count: pending.length,
      }),
    }),
  };
  vm.createContext(ctx);
  vm.runInContext(slice("faultResetSub"), ctx);
  vm.runInContext(slice("refreshFaultResets"), ctx);
  return {
    ctx, raised, dismissed, remote,
    pending(r) { pending = r; },
    due(r) { due = r; },
    panel(v) { panel = v; },
    run: () => ctx.refreshFaultResets(),
  };
}

(async () => {
  // --- the reset is still ahead: the card says so, once -------------------
  let h = harness();
  h.pending([row("s29")]);
  await h.run();
  await h.run();
  is(h.raised.length === 1, `one pending row raises one card once, raised ${h.raised.length}`);
  let card = h.raised[0] || {};
  let o = card.opts || {};
  is(/usage limit pause: s29/.test(card.title || ""), `pending title: ${card.title}`);
  is(/resume/.test(card.sub || ""), `pending sub says the resume is the person's: ${card.sub}`);
  is(o.dismissable !== false, "the card can be put away");
  is(o.sticky === true, "the card does not time out");
  is(o.session === "s29" && o.source === "fault", "opens the session, source fault");
  is(o.key === "fault-reset-s29", `key: ${o.key}`);

  // --- the reset passes: the same card, new wording, never two ------------
  h.pending([]);
  h.due([row("s29")]);
  await h.run();
  await h.run();
  is(h.raised.length === 2, `the due phase redraws the card once, raised ${h.raised.length}`);
  card = h.raised[1] || {};
  o = card.opts || {};
  is(/usage limit reset: s29/.test(card.title || ""), `due title: ${card.title}`);
  is(/reset passed/.test(card.sub || ""), `due sub asks for the decision: ${card.sub}`);
  is(o.key === "fault-reset-s29", `one card per session across the phase: ${o.key}`);

  // --- a later reset (another until) is a new card ------------------------
  h.due([row("s29", "2026-11-09T00:00:00+00:00")]);
  await h.run();
  is(h.raised.length === 3, "a new reset raises again");

  // --- the row goes: the card comes down ----------------------------------
  h.due([]);
  await h.run();
  is(h.dismissed.includes("fault-reset-s29"), "a row that goes takes its card down");

  // --- put away in another browser ----------------------------------------
  h = harness();
  h.remote.set(`fault-reset:s29:${UNTIL}`, { dismissed: true });
  h.pending([row("s29")]);
  await h.run();
  await h.run();
  is(h.raised.length === 0, "a card put away elsewhere is not offered to notify");
  h.pending([]);
  h.due([row("s29")]);
  await h.run();
  is(h.raised.length === 0, "nor is it offered again when its reset passes");

  // --- the history panel is open ------------------------------------------
  h = harness();
  h.panel(true);
  h.pending([row("s29")]);
  await h.run();
  is(h.ctx.faultResetDrawn.size === 0, "an open panel records nothing");
  h.panel(false);
  await h.run();
  is(h.raised.length === 1, "the card stands once the panel closes");

  if (FAIL.length) {
    console.error("FAIL");
    for (const line of FAIL) console.error(`  - ${line}`);
    process.exit(1);
  }
  console.log("ok");
})();
