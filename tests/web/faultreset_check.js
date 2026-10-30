/* The usage-limit reset poll (claunch-ijot0), run against the real functions.

   The daemon lists the sessions it paused on a usage limit whose stated reset
   has passed (GET /api/fault-resets); this poll draws them. What has to hold:
   a row becomes one card that names the session, can be put away with x and
   opens the session; polling again raises nothing new; a new reset (another
   until) raises again; the card comes down when the row goes (resumed,
   killed, archived); a card put away in another browser raises nothing and
   adds no history row; with the history panel open nothing is recorded. */
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

function row(session, until) {
  return {
    session, paused_at: "2026-10-30T04:00:00+00:00",
    fault: {
      kind: "usage_limit", label: "usage limit", harness: "claude",
      until: until || "2026-11-02T00:00:00+00:00", until_stated: true,
    },
  };
}

function harness() {
  let rows = [];
  let panel = false;
  const remote = new Map();
  const raised = [];
  const dismissed = [];
  const ctx = {
    FAULT_RESET_PREFIX: "fault-reset-",
    faultResetRaised: new Map(),
    noticeSync: { remote },
    noticeSourceShown: () => true,
    noticePanelOpen: () => panel,
    notify: (title, sub, opts) => { raised.push({ title, sub, opts }); return {}; },
    dismissNotice: (key) => { dismissed.push(key); },
    api: async () => ({ ok: true, json: async () => ({ resets: rows, count: rows.length }) }),
  };
  vm.createContext(ctx);
  vm.runInContext(slice("faultResetSub"), ctx);
  vm.runInContext(slice("refreshFaultResets"), ctx);
  return {
    ctx, raised, dismissed, remote,
    set(r) { rows = r; },
    panel(v) { panel = v; },
    run: () => ctx.refreshFaultResets(),
  };
}

(async () => {
  let h = harness();
  h.set([row("s29")]);
  await h.run();
  await h.run();
  is(h.raised.length === 1, `one row raises one card once, raised ${h.raised.length}`);
  const card = h.raised[0] || {};
  const o = card.opts || {};
  is(/usage limit reset: s29/.test(card.title || ""), `title: ${card.title}`);
  is(/resume/.test(card.sub || ""), `sub says the resume is the person's: ${card.sub}`);
  is(o.dismissable !== false, "the card can be put away");
  is(o.sticky === true, "the card does not time out");
  is(o.session === "s29" && o.source === "fault", "opens the session, source fault");
  is(o.key === "fault-reset-s29", `key: ${o.key}`);

  h.set([row("s29", "2026-11-09T00:00:00+00:00")]);
  await h.run();
  is(h.raised.length === 2, "a new reset raises again");
  h.set([]);
  await h.run();
  is(h.dismissed.includes("fault-reset-s29"), "a row that goes takes its card down");

  h = harness();
  h.remote.set("fault-reset:s29:2026-11-02T00:00:00+00:00", { dismissed: true });
  h.set([row("s29")]);
  await h.run();
  await h.run();
  is(h.raised.length === 0, "a card put away elsewhere is not offered to notify");

  h = harness();
  h.panel(true);
  h.set([row("s29")]);
  await h.run();
  is(h.ctx.faultResetRaised.size === 0, "an open panel records nothing");
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
