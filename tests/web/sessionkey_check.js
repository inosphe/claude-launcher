/* The rail's key chip and the key picker (claunch-wllke.1), run against the
   real functions from app.js.

   A profile may hold several launcher keys; a session uses one at a time and
   the rail offers a click straight into the picker. What has to hold here:
   - the chip is drawn ONLY for a session whose profile holds a pool (a
     single-token profile has no choice to offer, and a chip on every row
     would say the opposite), and it reads the pool from the cache rather
     than asking the daemon once per row;
   - an uncached profile starts one read and draws nothing this poll — that is
     how the row builder's late helpers behave, and a second call must not
     start a second read;
   - the label is the key's own (its label, else its mask), and the chip is
     lit only while the session is PINNED to a key rather than following the
     pool;
   - the picker's options carry the key ids and preselect nothing on the
     destructive side: Cancel sends no request at all;
   - a pick POSTs that id to /api/sessions/<name>/key and drops the cached
     pool, so the chips re-read it.
   - each key option carries a usage line (claunch-z881u): it opens as
     "reading…", is filled from /api/usage/keys (asked with the session's
     harness, refresh=auto) once that answers, and says why when it cannot.
   The stylesheet is held too: the chip is a short pill on the row's buttons
   line, not an open-ended label that can push the buttons onto another line. */
const fs = require("fs");
const path = require("path");
const staticDir = path.join(__dirname, "..", "..", "src", "claude_launcher",
                            "web", "static");
const src = fs.readFileSync(path.join(staticDir, "app.js"), "utf8");
const css = fs.readFileSync(path.join(staticDir, "style.css"), "utf8");

/* The body starts at the `) {` that closes the parameter list — not at the
   first `{`, which a destructured parameter (`{ fresh = false }`) would
   claim. */
function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  const head = src.slice(start - 6, start) === "async " ? start - 6 : start;
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

/* ---- stub DOM ---- */
function el(tag) {
  const node = {
    tag, className: "", title: "", type: "", textContent: "", dataset: {},
    children: [], listeners: {}, attrs: {},
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { for (const c of cs) this.children.push(c); },
    addEventListener(t, fn) { this.listeners[t] = fn; },
    setAttribute(k, v) { this.attrs[k] = v; },
  };
  return node;
}
const document = { createElement: el };

const failures = [];
function check(name, cond, detail) {
  if (!cond) failures.push(`${name}${detail ? " — " + detail : ""}`);
}

/* ---- the chip ---- */
const keyPools = new Map();
const keyPoolPending = new Map();
const fetches = [];
let redraws = 0;
const poolEnv = {
  keyPools, document, encodeURIComponent,
  api: (path) => {
    fetches.push(path);
    return Promise.resolve({
      ok: true, status: 200,
      json: async () => ({ pooled: false, keys: [] }),
    });
  },
  refreshSessions: () => { redraws++; },
  openKeyPicker: (name, profile) => { poolEnv.opened = [name, profile]; },
};
const fetchKeyPool = new Function(
  "keyPools", "keyPoolPending", "api", "encodeURIComponent",
  slice("fetchKeyPool") + "; return fetchKeyPool;"
)(keyPools, keyPoolPending, poolEnv.api, encodeURIComponent);
/* KEY_POOL_TTL_MS is a const, not a function, so it needs its own cut. */
function keyPoolTtlTable() {
  const start = src.indexOf("const KEY_POOL_TTL_MS =");
  if (start < 0) throw new Error("cannot locate KEY_POOL_TTL_MS in app.js");
  return src.slice(start, src.indexOf(";", start) + 1);
}

/* The chip asks for a redraw through the collapser, not through the pass
   itself (claunch-rw7tw.1): one row asking is one row asking, and a whole
   pass per row is the storm that trace found. */
const requestSessionsRefresh = new Function(
  "setTimeout", "refreshSessions", "sessionsRefreshQueued",
  slice("requestSessionsRefresh") + "; return requestSessionsRefresh;"
)(setTimeout, poolEnv.refreshSessions, false);

const railKeyChip = new Function(
  "keyPools", "document", "fetchKeyPool", "requestSessionsRefresh",
  "openKeyPicker",
  keyPoolTtlTable() + "\n" + slice("railKeyChip") + "; return railKeyChip;"
)(keyPools, document, fetchKeyPool, requestSessionsRefresh,
  poolEnv.openKeyPicker);

const session = (over = {}) =>
  Object.assign({ name: "s1", profile: "work:claude" }, over);

// Nothing cached: no chip this poll, but exactly one read started.
check("uncached row draws nothing", railKeyChip(session()) === null);
check("uncached row starts a read", fetches.length === 1
  && fetches[0] === "/api/profiles/work%3Aclaude/keys", fetches[0]);
railKeyChip(session());
check("a pending read is not restarted", fetches.length === 1);

// A single-token profile: still no chip.
keyPools.set("work:claude", { at: Date.now(), data: { pooled: false, keys: [] } });
check("single-token profile draws no chip", railKeyChip(session()) === null);

// A pool: the chip names the key the next launch would use.
const two = {
  pooled: true, owner: "work", strategy: "rotation",
  keys: [
    { id: "k-aaaaaa", label: "", masked: "sk-A…BBBB", current: false, use_count: 2 },
    { id: "k-bbbbbb", label: "second", masked: "sk-C…DDDD", current: true, use_count: 1 },
  ],
};
keyPools.set("work:claude", { at: Date.now(), data: two });
const chip = railKeyChip(session());
check("pooled profile draws a chip", !!chip);
check("chip is a button", chip && chip.tag === "button");
check("chip carries a short label", chip && chip.children[0]
  && chip.children[0].textContent === "second",
  chip && JSON.stringify(chip.children[0] && chip.children[0].textContent));
check("an unpinned session is not lit", chip && chip.className === "sess-key");
check("chip names the session", chip && chip.dataset.name === "s1");
let stopped = false;
chip.listeners.click({ stopPropagation() { stopped = true; } });
check("click does not also attach the session", stopped);
check("click opens the picker", poolEnv.opened
  && poolEnv.opened[0] === "s1" && poolEnv.opened[1] === "work:claude");

// Pinned: the chip shows THAT key, not the pool's current one, and is lit.
const pinnedChip = railKeyChip(session({ key_id: "k-aaaaaa" }));
check("a pinned session shows its key", pinnedChip
  && pinnedChip.children[0].textContent === "sk-A…BBBB");
check("a pinned session is lit", pinnedChip && pinnedChip.className.includes("on"));

/* The read the uncached row started settles long before the next poll, so
   its chain is allowed to run first: the stale case below is a cache ON a
   settled read, not a read still in flight (the two are different questions
   -- a fresh ask joins a read in flight, which railkeypool_check holds). */
async function stalePoolReReads() {
  for (let i = 0; i < 20 && keyPoolPending.size; i++) await Promise.resolve();
  // A stale cache re-reads on a slow clock, and keeps drawing from what it had.
  const stale = { at: Date.now() - 120000, data: two };
  keyPools.set("work:claude", stale);
  fetches.length = 0;
  const staleChip = railKeyChip(session());
  check("a stale pool still draws", !!staleChip);
  check("a stale pool re-reads once", fetches.length === 1
    && fetches[0] === "/api/profiles/work%3Aclaude/keys", fetches[0]);
}

/* ---- the picker ---- */
let posted = null;
let dialog = null;
/* The usage read the picker starts, and what it answers. The helpers that
   turn a reading into text are the create form's own, cut from app.js. */
const usageAsked = [];
let usageAnswer = null;
const usageApi = (path) => {
  usageAsked.push(path);
  if (usageAnswer instanceof Error) return Promise.reject(usageAnswer);
  return Promise.resolve({
    ok: !usageAnswer.error, status: usageAnswer.error ? 409 : 200,
    json: async () => usageAnswer,
  });
};
const usageHelpers = ["newUsageWindowLabel", "newUsagePeak", "newUsageLevel",
  "newUsageMoney", "newUsageReset", "newUsageWindowText", "newUsageKeySummary"]
  .map(slice).join("\n");
function usageLabelTable() {
  const start = src.indexOf("const NEW_USAGE_WINDOW_LABELS =");
  if (start < 0) throw new Error("cannot locate NEW_USAGE_WINDOW_LABELS in app.js");
  return src.slice(start, src.indexOf("};", start) + 2);
}
const keyPickerUsage = new Function("api", "encodeURIComponent",
  slice("keyPickerUsage") + "; return keyPickerUsage;")(usageApi, encodeURIComponent);
const keyPickerFillUsage = new Function(
  usageLabelTable() + "\n" + usageHelpers + "\n" + slice("keyPickerFillUsage")
  + "; return keyPickerFillUsage;")();
const keyPickerSelector = new Function(
  slice("keyPickerSelector") + "; return keyPickerSelector;")();
const pickEnv = {
  keyPools,
  fetchKeyPool: () => Promise.resolve(two),
  modalInfo: (title) => { pickEnv.info = title; },
  // The real modal hands each option's detail node back on the option.
  showModal: (spec) => {
    dialog = spec;
    for (const opt of (spec.choices && spec.choices.options) || []) {
      if (opt.detail !== undefined) {
        opt.detailEl = el("small");
        opt.detailEl.textContent = opt.detail;
      }
    }
    return Promise.resolve(pickEnv.answer);
  },
  keyPickerUsage, keyPickerSelector, keyPickerFillUsage,
  api: (path, opts) => {
    posted = { path, body: JSON.parse(opts.body) };
    return Promise.resolve({ ok: true, status: 200, json: async () => ({ key_id: "x" }) });
  },
  sessionsCache: [{ name: "s1", key_id: "k-bbbbbb" },
                  { name: "s2", harness: "opencode" }],
  currentName: "other",
  refreshSessions: () => { pickEnv.refreshed = true; },
  detach: () => {},
  attach: () => {},
  encodeURIComponent,
};
const openKeyPicker = new Function(
  ...Object.keys(pickEnv),
  slice("openKeyPicker") + "; return openKeyPicker;"
)(...Object.values(pickEnv));

const settle = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

(async () => {
  await stalePoolReReads();
  // Cancel: nothing is sent.
  pickEnv.answer = null;
  usageAnswer = {
    keys: [
      { id: "k-aaaaaa", usage: { key_id: "k-aaaaaa", error: null, windows: [
        { name: "five_hour", utilization: 95.2 },
        { name: "seven_day", utilization: 40 }] } },
      { id: "k-bbbbbb", usage: { key_id: "k-bbbbbb", error: "HTTP 401", windows: [] } },
    ],
    unmeasurable: null,
  };
  await openKeyPicker("s1", "work:claude");
  await settle();
  check("a picker opened", !!dialog);
  check("cancel sends nothing", posted === null);
  const opts = (dialog.choices && dialog.choices.options) || [];
  check("every key is offered", opts.length === 3, `got ${opts.length}`);
  check("options carry key ids", opts[0].value.key_id === "k-aaaaaa");
  check("the unpin option carries null",
    opts[opts.length - 1].value.key_id === null);
  check("the pinned key is marked", /pinned/.test(opts[1].hint || ""),
    opts[1].hint);
  check("tokens only ever appear masked",
    !JSON.stringify(dialog, (k, v) => (k === "detailEl" ? undefined : v))
      .includes("sk-AAAA1111BBBB"));

  /* ---- each key's usage (claunch-z881u) ---- */
  check("usage is read once, from the cache", usageAsked.length === 1
    && usageAsked[0] === "/api/usage/keys?profile=work%3Aclaude&refresh=auto",
    usageAsked[0]);
  check("a key option opens with a usage line", opts[0].detail === "usage: reading…",
    opts[0].detail);
  check("the reading fills the line", opts[0].detailEl
    && opts[0].detailEl.textContent === "usage: 5h 95%  ·  7d 40%",
    opts[0].detailEl && opts[0].detailEl.textContent);
  check("a key near its limit is marked", opts[0].detailEl
    && /\bhigh\b/.test(opts[0].detailEl.className), opts[0].detailEl.className);
  check("a key whose read failed says why", opts[1].detailEl
    && opts[1].detailEl.textContent === "usage: HTTP 401",
    opts[1].detailEl && opts[1].detailEl.textContent);
  check("the unpin option has no usage line", !opts[2].detailEl);

  // The session's harness picks the reader; an unreadable answer says so.
  usageAsked.length = 0;
  usageAnswer = { error: "profile 'oc' has no usage reader" };
  await openKeyPicker("s2", "oc");
  await settle();
  const ocOpts = dialog.choices.options;
  check("usage is asked with the session's harness",
    usageAsked[0] === "/api/usage/keys?profile=oc%3Aopencode&refresh=auto", usageAsked[0]);
  check("a refused read is shown, not left reading", ocOpts[0].detailEl
    && ocOpts[0].detailEl.textContent === "usage: unavailable: profile 'oc' has no usage reader",
    ocOpts[0].detailEl && ocOpts[0].detailEl.textContent);

  // A pool whose harness cannot tell keys apart names that instead.
  usageAnswer = { keys: [{ id: "k-aaaaaa", usage: null }], unmeasurable: "login usage" };
  await openKeyPicker("s1", "work:claude");
  await settle();
  check("an unmeasurable pool says so", dialog.choices.options[0].detailEl.textContent
    === "usage: cannot be read: login usage", dialog.choices.options[0].detailEl.textContent);

  // The daemon unreachable: the picker still works.
  usageAnswer = new Error("offline");
  await openKeyPicker("s1", "work:claude");
  await settle();
  check("an unreachable daemon is shown", /^usage: unavailable: could not reach the daemon/
    .test(dialog.choices.options[0].detailEl.textContent),
    dialog.choices.options[0].detailEl.textContent);

  // A pick: POST that id, and drop the cached pool so the chips re-read it.
  pickEnv.answer = { go: true, key_id: "k-aaaaaa" };
  await openKeyPicker("s1", "work:claude");
  check("a pick POSTs to the session's key route",
    posted && posted.path === "/api/sessions/s1/key", posted && posted.path);
  check("a pick sends the key id", posted && posted.body.key_id === "k-aaaaaa");
  check("the pool cache is dropped after a switch", !keyPools.has("work:claude"));
  check("the rail is refreshed", pickEnv.refreshed === true);

  /* ---- the stylesheet holds the chip to a pill ---- */
  check("the chip has a rule", /#session-list \.sess-key \{/.test(css));
  const rule = (css.match(/#session-list \.sess-key \{[^}]*\}/) || [""])[0];
  check("the chip is capped", /max-width: \d+px/.test(rule), rule);
  check("the chip's label ellipsises", /\.sess-key-label[^{]*\{[^}]*text-overflow: ellipsis/.test(css));

  if (failures.length) {
    console.error(failures.map((f) => "FAIL " + f).join("\n"));
    process.exit(1);
  }
  console.log("sessionkey: ok");
})();
