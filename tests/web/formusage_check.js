/* The create form's usage readout (claunch-yx2bd), run against the real
   functions from app.js.

   The Runs-as section decides whose token a session spends; this block says
   what that token has left. What has to hold:

   - nothing is asked for while no profile is selected, and the block stays
     hidden until an answer lands (a daemon that cannot serve the route draws
     nothing rather than an error);
   - the profile's own windows are drawn from the answer, in the same
     vocabulary the usage page uses (5h/7d/…, percent, reset countdown);
   - a pool is listed one row per key, and the row the NEXT LAUNCH takes is
     marked from the server's `next` — not guessed here;
   - the strategy select is offered only for a pool and enabled only when
     there are two or more keys to choose between, because that is the only
     case the choice changes anything;
   - a usage strategy (usage-min/usage-max) is not previewed by the server, so
     the row its rule would take on the numbers shown is marked as "lowest
     now"/"highest now" — arithmetic on the last read, not a promise;
   - an answer for a profile the operator has already left never paints over
     the one they picked next;
   - a refused read (409, unreadable pool) is shown as the sentence the daemon
     sent. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");

let failures = 0;
function check(label, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g === w) return;
  failures++;
  console.error(`FAIL ${label}\n  got  ${g}\n  want ${w}`);
}

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  const head = src.slice(start - 6, start) === "async " ? start - 6 : start;
  let depth = 0;
  for (let j = src.indexOf(") {", start) + 2; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

/* The const block (the TTL, the two label tables and the state object) is not
   a function, so it needs its own cut: everything between the first const and
   the first helper below it. */
function constBlock() {
  const start = src.indexOf("const NEW_USAGE_TTL_MS");
  const end = src.indexOf("function newUsageAge(");
  if (start < 0 || end < start) throw new Error("cannot locate the usage consts");
  return src.slice(start, end);
}

/* ---- stub DOM ---- */
function makeNode(tag) {
  const classes = new Set();
  const node = {
    tag, style: {}, children: [], listeners: {}, dataset: {},
    title: "", textContent: "", disabled: false, value: "",
  };
  Object.defineProperty(node, "className", {
    get: () => [...classes].join(" "),
    set: (value) => {
      classes.clear();
      String(value || "").split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
    },
  });
  Object.defineProperty(node, "options", {
    get: () => node.children.filter((child) => child.tag === "option"),
  });
  Object.defineProperty(node, "innerHTML", {
    get: () => "",
    set: (value) => { if (!value) node.children.length = 0; },
  });
  node.classList = {
    add: (c) => classes.add(c),
    remove: (c) => classes.delete(c),
    contains: (c) => classes.has(c),
    toggle: (c, on) => {
      const want = on === undefined ? !classes.has(c) : !!on;
      if (want) classes.add(c); else classes.delete(c);
      return want;
    },
  };
  node.appendChild = (child) => { node.children.push(child); return child; };
  node.append = (...items) => { node.children.push(...items); };
  return node;
}

const IDS = [
  "new-usage", "new-usage-who", "new-usage-when", "new-usage-refresh",
  "new-usage-windows", "new-usage-keys", "new-strategy-row",
  "new-key-strategy", "new-usage-note",
];
const nodes = {};
for (const id of IDS) nodes[id] = makeNode("div");
const document = { createElement: makeNode };
const $ = (id) => (id === "new-session" ? { profile: {} } : nodes[id] || null);

/* ---- stub daemon ---- */
const fetches = [];
const calls = [];
const pending = [];
function api(url, opts) {
  fetches.push(url);
  calls.push({ url, opts: opts || null });
  return new Promise((resolve) => pending.push({ url, resolve }));
}
function settle(index, spec) {
  pending[index].resolve({
    ok: spec.ok !== false,
    status: spec.status || 200,
    json: async () => spec.body,
  });
}
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

let selectorNow = "";
// The form is the only host of this block; the page's own predicate is
// stubbed here so the "off screen costs nothing" rule can be driven.
let formOnScreen = true;
const env = {
  $, document, api,
  newProfileSelector: () => selectorNow,
  sessionFormHeld: () => formOnScreen,
  Date, Math, Number, String, JSON, encodeURIComponent, Promise,
};
const build = new Function(
  ...Object.keys(env),
  constBlock() +
  slice("newUsageAge") + slice("newUsageWindowLabel") + slice("newUsagePeak") +
  slice("newUsageLevel") + slice("newUsageMoney") + slice("newUsageReset") +
  slice("newUsageWindowText") + slice("newUsageWindowLine") +
  slice("newUsageKeySummary") + slice("newUsageFillStrategies") +
  slice("renderNewUsage") + slice("startNewUsageRead") +
  slice("setNewKeyStrategy") +
  "; return { renderNewUsage, startNewUsageRead, setNewKeyStrategy, state: newUsageState };",
);
const usage = build(...Object.values(env));

const box = nodes["new-usage"];
const windowsBox = nodes["new-usage-windows"];
const keysBox = nodes["new-usage-keys"];
const select = nodes["new-key-strategy"];
const strategyRow = nodes["new-strategy-row"];
const note = nodes["new-usage-note"];
const who = nodes["new-usage-who"];

function windowLine(name, utilization) {
  return { name, utilization, resets_at: null, status: "ok" };
}

function doc(over = {}) {
  return {
    profile: "work", owner: null, pooled: false, strategy: null,
    strategies: ["rotation", "usage-min", "usage-max", "random"],
    next: null, next_unknown: null, unmeasurable: null, keys: [],
    usage: {
      source: "oauth-usage", cached: true, updated_at: Date.now() / 1000,
      windows: [windowLine("five_hour", 12)],
    },
    usage_error: null,
    ...over,
  };
}

async function main() {
  /* ---- no profile: silent, and nothing asked ---- */
  selectorNow = "";
  usage.renderNewUsage();
  check("no profile hides the block", box.classList.contains("hidden"), true);
  check("no profile asks nothing", fetches.length, 0);

  /* ---- off screen: silent, and nothing asked ---- */
  selectorNow = "work:claude";
  formOnScreen = false;
  usage.renderNewUsage();
  check("a form off screen asks nothing", fetches.length, 0);
  check("...and keeps the block hidden", box.classList.contains("hidden"), true);
  formOnScreen = true;

  /* ---- a single token: its own windows, no key list, the pool hint ---- */
  usage.renderNewUsage();
  check("a read starts for the selected profile", fetches[0],
    "/api/usage/keys?profile=work%3Aclaude&refresh=auto");
  check("nothing is drawn before the answer", box.classList.contains("hidden"), true);
  settle(0, { body: doc() });
  await flush();
  check("the block opens when the answer lands", box.classList.contains("hidden"), false);
  check("it names the profile", who.textContent, "Usage · work");
  check("the windows are drawn", windowsBox.children.length, 1);
  check("a window reads like the usage page's",
    windowsBox.children[0].children[0].textContent, "5h 12%");
  check("a single token has no key list", keysBox.classList.contains("hidden"), true);
  check("a single token has no strategy row", strategyRow.classList.contains("hidden"), true);
  check("the note names how to get a pool", /one token/.test(note.textContent), true);
  check("the note stays visible", note.classList.contains("hidden"), false);
  check("no window error is shown", note.textContent.includes("usage unavailable"), false);

  /* ---- a pool: every key, and the next launch's key marked ---- */
  selectorNow = "work:claude";
  usage.state.at = 0;  // the TTL has run out; a fresh read
  usage.renderNewUsage();
  settle(pending.length - 1, {
    body: doc({
      pooled: true, owner: "work", strategy: "rotation",
      next: { key_id: "k-2", strategy: "rotation", fallback: null },
      keys: [
        { id: "k-1", label: "main", masked: "sk-A…1111", current: true,
          next: false, usage: { windows: [windowLine("five_hour", 12)], error: null } },
        { id: "k-2", label: "spare", masked: "sk-B…2222", current: false,
          next: true, usage: { windows: [windowLine("five_hour", 80)], error: null } },
      ],
    }),
  });
  await flush();
  check("every key is listed", keysBox.children.length, 2);
  check("the key list is shown for a pool", keysBox.classList.contains("hidden"), false);
  check("the key in the token file says so",
    keysBox.children[0].children[0].textContent, "in use");
  check("the next launch's key is marked",
    keysBox.children[1].children[0].textContent, "next");
  check("a next row is lit", keysBox.children[1].className.includes("next"), true);
  check("a key row carries its own numbers",
    keysBox.children[1].children[2].textContent, "5h 80%");
  check("the note names the next key and its strategy",
    note.textContent.includes("next launch takes spare (rotation)"), true);
  check("the strategy row is offered for a pool",
    strategyRow.classList.contains("hidden"), false);
  check("the strategy select holds every strategy",
    select.children.map((o) => o.value), ["rotation", "usage-min", "usage-max", "random"]);
  check("the strategy select shows the pool's answer", select.value, "rotation");
  check("a pool with two keys may set the strategy", select.disabled, false);

  /* ---- a usage strategy is arithmetic here, not a preview ---- */
  selectorNow = "work:claude";
  usage.state.at = 0;
  usage.renderNewUsage();
  settle(pending.length - 1, {
    body: doc({
      pooled: true, owner: "work", strategy: "usage-min",
      next: null,
      next_unknown: "usage-min reads every key's usage at launch and takes the lowest",
      keys: [
        { id: "k-1", label: "main", masked: "sk-A…1111", current: false,
          next: false, usage: { windows: [windowLine("five_hour", 12)], error: null } },
        { id: "k-2", label: "spare", masked: "sk-B…2222", current: false,
          next: false, usage: { windows: [windowLine("five_hour", 80)], error: null } },
      ],
    }),
  });
  await flush();
  const first = keysBox.children[0];
  const second = keysBox.children[1];
  check("no row claims to be the next launch's",
    [first.children[0].textContent, second.children[0].textContent], ["", ""]);
  check("the lowest row is marked as arithmetic on the numbers",
    first.children[3] && first.children[3].textContent, "lowest now");
  check("the higher row is not marked", second.children.length, 3);
  check("the note says the pick happens at launch",
    note.textContent.includes("usage-min reads every key's usage"), true);

  /* ---- one key in the pool: the row is offered, the choice is not ---- */
  selectorNow = "work:claude";
  usage.state.at = 0;
  usage.renderNewUsage();
  settle(pending.length - 1, {
    body: doc({
      pooled: true, owner: "work", strategy: "rotation",
      next: { key_id: "k-1", strategy: "rotation", fallback: null },
      keys: [{ id: "k-1", label: "only", masked: "sk-A…1111", current: true,
        next: true, usage: { windows: [windowLine("five_hour", 12)], error: null } }],
    }),
  });
  await flush();
  check("one key is not a list", keysBox.classList.contains("hidden"), true);
  check("one key may not pick a strategy", select.disabled, true);
  check("the note says why", note.textContent.includes("two or more"), true);

  /* ---- a refused read is shown as the daemon's sentence ---- */
  selectorNow = "other:claude";
  usage.state.at = 0;
  usage.renderNewUsage();
  settle(pending.length - 1, { ok: false, status: 409, body: { error: "cannot read key pool" } });
  await flush();
  check("a refusal keeps the block visible", box.classList.contains("hidden"), false);
  check("the refusal is shown as sent", note.textContent, "cannot read key pool");

  /* ---- a late answer for a profile the operator left is dropped ---- */
  selectorNow = "late:claude";
  usage.state.at = 0;
  usage.renderNewUsage();
  const lateRead = pending.length - 1;
  selectorNow = "next:claude";
  usage.renderNewUsage();
  const currentRead = pending.length - 1;
  settle(currentRead, { body: doc({ profile: "next" }) });
  await flush();
  check("the answer for the selected profile is drawn", who.textContent, "Usage · next");
  settle(lateRead, { body: doc({ profile: "late" }) });
  await flush();
  check("a late answer does not paint over it", who.textContent, "Usage · next");

  /* ---- the strategy write goes to the pool's own profile ---- */
  // The row is on screen for this profile, so the form's selector is the one
  // the write is about (an acknowledgment for another profile is dropped by
  // renderNewUsage, which the last section already holds).
  selectorNow = "work:claude";
  calls.length = 0;
  usage.setNewKeyStrategy("work", "work:claude", "usage-min");
  check("the strategy is saved with a POST", calls[0] && calls[0].url,
    "/api/profiles/work/keys/strategy");
  check("...carrying the strategy that was picked",
    calls[0] && JSON.parse(calls[0].opts.body).strategy, "usage-min");
  settle(pending.length - 1, {
    body: { profile: "work", owner: "work", strategy: "usage-min", keys: 2 },
  });
  await flush();
  check("an accepted write is acknowledged", usage.state.flash,
    "strategy saved: usage-min");

  calls.length = 0;
  usage.setNewKeyStrategy("work", "work:claude", "weighted");
  settle(pending.length - 1, {
    ok: false, status: 400, body: { error: "unknown strategy 'weighted'" },
  });
  await flush();
  check("a refused write says what the daemon said", usage.state.flash,
    "unknown strategy 'weighted'");

  /* ---- refresh forces the account reads ---- */
  // Not awaited: the request is issued before the first await inside, which
  // is the whole of what this checks. Awaiting would wait on a promise this
  // stub never settles, and node exits quietly on one of those.
  usage.startNewUsageRead("work", true);
  check("refresh asks the daemon for a manual read",
    fetches[fetches.length - 1], "/api/usage/keys?profile=work&refresh=manual");

  console.log("formusage_check: " + (failures ? `${failures} failing` : "ok"));
  process.exitCode = failures ? 1 : 0;
}

main().catch((e) => { console.error(e); process.exitCode = 1; });
