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
    children: [], listeners: {}, attrs: {}, options: [], value: "",
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { for (const c of cs) this.children.push(c); },
    addEventListener(t, fn) { this.listeners[t] = fn; },
    setAttribute(k, v) { this.attrs[k] = v; },
    // Just the word-level behaviour the picker uses: `toggle("hidden", on)`
    // on the roll button, and `contains` for the assertions below.
    classList: {
      add(c) {
        if (!node.className.split(/\s+/).includes(c)) {
          node.className = (node.className + " " + c).trim();
        }
      },
      remove(c) {
        node.className = node.className.split(/\s+/)
          .filter((x) => x && x !== c).join(" ");
      },
      contains(c) { return node.className.split(/\s+/).includes(c); },
      toggle(c, on) {
        const want = on === undefined ? !this.contains(c) : !!on;
        if (want) this.add(c); else this.remove(c);
        return want;
      },
    },
  };
  return node;
}
const document = {
  createElement: el,
  // The picker's auto row writes its label as a text node beside the box,
  // the way the page's other inline checkboxes do (claunch-d2t79).
  createTextNode: (text) => {
    const node = el("#text");
    node.textContent = String(text);
    return node;
  },
};

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
  keyPoolTtlTable() + "\n" + slice("keyPoolProfile") + "\n" + slice("railKeyChip")
  + "; return railKeyChip;"
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

// A borrow runs on the lender's token, so the chip is the lender's pool and
// the picker opens on it (claunch-e48es).
keyPools.set("lend", { at: Date.now(), data: Object.assign({}, two, { owner: "lend" }) });
const borrowChip = railKeyChip(session({ borrow: "lend" }));
check("a borrowing session draws the lender's chip", !!borrowChip);
check("...and says it is borrowed", borrowChip && borrowChip.title.includes("borrowed from lend"),
  borrowChip && borrowChip.title);
borrowChip.listeners.click({ stopPropagation() {} });
check("...and opens the picker on the lender's pool", poolEnv.opened
  && poolEnv.opened[0] === "s1" && poolEnv.opened[1] === "lend");
check("a --null session reads no pool", railKeyChip(session({ null_token: true })) === null);

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
check("a session's own pool is read with its harness",
  keyPickerSelector("work", { harness: "claude" }), "work:claude");
check("a borrowed pool is read under the lender's own name",
  keyPickerSelector("lend", { harness: "claude", borrow: "lend" }), "lend");
/* The strategy labels the picker's select is built from, the write helper and
   the row builder itself (claunch-i985h). All three are cut whole from app.js
   so the harness draws the row the page draws. */
function strategyLabelTable() {
  const start = src.indexOf("const NEW_USAGE_STRATEGY_LABELS =");
  if (start < 0) throw new Error("cannot locate NEW_USAGE_STRATEGY_LABELS in app.js");
  return src.slice(start, src.indexOf("};", start) + 2);
}
const writeApi = (path, opts) => {
  posted = { path, body: JSON.parse((opts && opts.body) || "{}") };
  return Promise.resolve({
    ok: true, status: 200,
    json: async () => pickEnv.writeAnswer || { key_id: "x" },
  });
};
const keyPickerWrite = new Function("api", "encodeURIComponent",
  slice("keyPickerWrite") + "; return keyPickerWrite;")(writeApi, encodeURIComponent);
const keyPickerControls = new Function(
  "el", "document", "keyPickerWrite",
  strategyLabelTable() + "\n" + slice("keyPickerControls")
  + "; return keyPickerControls;")(el, document, keyPickerWrite);
let poolAnswer = two;
const pickEnv = {
  el,
  keyPools,
  // A live answer: the checks below swap the pool the picker opens on.
  fetchKeyPool: () => Promise.resolve(poolAnswer),
  modalInfo: (title) => { pickEnv.info = title; },
  // The real modal hands each option's detail and hint nodes back on the
  // option.
  showModal: (spec) => {
    dialog = spec;
    for (const opt of (spec.choices && spec.choices.options) || []) {
      if (opt.detail !== undefined) {
        opt.detailEl = el("small");
        opt.detailEl.textContent = opt.detail;
      }
      if (opt.hint) {
        opt.hintEl = el("small");
        opt.hintEl.textContent = opt.hint;
      }
    }
    return Promise.resolve(pickEnv.answer);
  },
  keyPickerUsage, keyPickerSelector, keyPickerFillUsage,
  keyPickerControls, keyPickerWrite,
  api: writeApi,
  sessionsCache: [{ name: "s1", key_id: "k-bbbbbb" },
                  { name: "s2", harness: "opencode" }],
  currentName: "other",
  refreshSessions: () => { pickEnv.refreshed = true; },
  // The auto row's write asks for a redraw through the collapser rather than
  // a whole pass (claunch-d2t79), the same way the rail's chip does.
  requestSessionsRefresh: () => { pickEnv.refreshRequested = true; },
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
    next: { key_id: "k-aaaaaa", exact: true },
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

  /* ---- the strategy row, and which key the next launch takes (claunch-i985h) ---- */
  // Its own open, on a pinned session, so the row is read against a known
  // pool rather than whatever fixture the previous case left behind.
  usageAnswer = {
    keys: [],
    next: { key_id: "k-aaaaaa", exact: true },
    unmeasurable: null,
  };
  pickEnv.answer = null;
  await openKeyPicker("s1", "work:claude");
  await settle();
  const opts2 = dialog.choices.options;
  const box = dialog.controls;
  check("the modal carries the pool's strategy row", !!box);
  const row = box && box.children[0];
  const select = row && row.children[0] && row.children[0].children[0];
  const roll = row && row.children[1];
  const nextLine = box && box.children[1];
  const note = box && box.children[2];
  check("the select offers every strategy", select && select.children.length === 5,
    select && String(select.children.length));
  check("the select starts on the pool's strategy", select && select.value === "rotation",
    select && select.value);
  check("the next line names the peek's key", nextLine
    && nextLine.textContent === "next launch takes sk-A…BBBB",
    nextLine && nextLine.textContent);
  check("the row the peek names is marked", opts2[0].hintEl
    && /next launch takes this/.test(opts2[0].hintEl.textContent),
    opts2[0].hintEl && opts2[0].hintEl.textContent);
  check("roll is not offered outside random", roll && roll.className.includes("hidden"),
    roll && roll.className);
  check("an unpinned session's strategy row says the pool decides", note
    && /no pin/i.test(note.textContent) || /No pin/.test(note.textContent),
    note && note.textContent);
  // The radio starts on the session's pin, not on the token file's key.
  check("the radio starts on the session's pinned key",
    dialog.choices.selected(opts2[1]) === true
      && dialog.choices.selected(opts2[0]) !== true);

  /* ---- changing the strategy here writes the pool ---- */
  pickEnv.writeAnswer = { strategy: "manual", pick: "k-bbbbbb", keys: 2 };
  posted = null;
  select.value = "manual";
  select.listeners.change();
  await settle();
  check("a strategy change writes the pool",
    posted && posted.path === "/api/profiles/work/keys/strategy", posted && posted.path);
  check("manual carries the key marked in the list",
    posted && posted.body.strategy === "manual" && posted.body.key_id === "k-bbbbbb",
    posted && JSON.stringify(posted.body));
  check("the pools cache is dropped after the write", !keyPools.has("work:claude"));
  // Under manual the pressed key IS the choice: one press, one write.
  posted = null;
  dialog.choices.onPick(opts2[0]);
  await settle();
  check("pressing a key under manual names that key",
    posted && posted.body.strategy === "manual" && posted.body.key_id === "k-aaaaaa",
    posted && JSON.stringify(posted.body));

  /* ---- random: the roll draws the key the next launch takes ---- */
  poolAnswer = {
    pooled: true, owner: "work", strategy: "random", pick: "k-bbbbbb",
    keys: two.keys,
  };
  usageAnswer = { keys: [], next: { key_id: "k-bbbbbb", exact: true } };
  pickEnv.writeAnswer = { strategy: "random", pick: "k-aaaaaa", key: { id: "k-aaaaaa" } };
  pickEnv.answer = null;
  await openKeyPicker("s1", "work:claude");
  await settle();
  const rbox = dialog.controls;
  const rroll = rbox.children[0].children[1];
  check("random offers the roll", !rroll.className.includes("hidden"), rroll.className);
  check("random's next line is the stored draw", rbox.children[1].textContent
    === "next launch takes second", rbox.children[1].textContent);
  // The re-read after a draw answers with the new pick; the harness hands it
  // the answer the daemon would give.
  usageAnswer = { keys: [], next: { key_id: "k-aaaaaa", exact: true } };
  posted = null;
  rroll.listeners.click();
  await settle();
  check("the roll posts to the roll route",
    posted && posted.path === "/api/profiles/work/keys/roll", posted && posted.path);
  check("the new draw is what the next launch takes",
    rbox.children[1].textContent === "next launch takes sk-A…BBBB",
    rbox.children[1].textContent);

  /* ---- a session with no pin starts on the key its launch was given ---- */
  poolAnswer = two;
  usageAnswer = { keys: [], next: { key_id: "k-aaaaaa", exact: true } };
  pickEnv.sessionsCache.push({ name: "s3", launched_key: "k-aaaaaa" });
  await openKeyPicker("s3", "work:claude");
  await settle();
  const lOpts = dialog.choices.options;
  check("the radio starts on the key the session was launched with",
    dialog.choices.selected(lOpts[0]) === true);
  check("that row says the launch used it",
    /last launch used this/.test(lOpts[0].hint || ""), lOpts[0].hint);

  /* ---- a pick: POST that id, and drop the cached pool so the chips re-read it ---- */
  poolAnswer = two;
  pickEnv.answer = { go: true, key_id: "k-aaaaaa" };
  await openKeyPicker("s1", "work:claude");
  check("a pick POSTs to the session's key route",
    posted && posted.path === "/api/sessions/s1/key", posted && posted.path);
  check("a pick sends the key id", posted && posted.body.key_id === "k-aaaaaa");
  check("the pool cache is dropped after a switch", !keyPools.has("work:claude"));
  check("the rail is refreshed", pickEnv.refreshed === true);

  /* ---- the automatic key switch's answer (claunch-d2t79) ----
     It is the SESSION's answer, not the pool's: the modal writes it to the
     session route, costs no restart, and leaves the key list and the strategy
     exactly where they were. */
  pickEnv.sessionsCache[0].auto_key_switch = true;
  pickEnv.answer = null;
  pickEnv.writeAnswer = { auto_key_switch: false };
  poolAnswer = two;
  usageAnswer = { keys: [], next: { key_id: "k-aaaaaa", exact: true } };
  await openKeyPicker("s1", "work:claude");
  await settle();
  const abox = dialog.controls;
  const autoRow = abox && abox.children[3];
  const autoNote = abox && abox.children[4];
  const autoBox = autoRow && autoRow.children[0] && autoRow.children[0].children[0];
  check("the modal offers the automatic switch", !!autoBox);
  check("...checked because this session already asked for it",
    autoBox && autoBox.checked === true);
  check("...and the note names the rule the key is chosen by",
    autoNote && /lowest usage/.test(autoNote.textContent),
    autoNote && autoNote.textContent);
  posted = null;
  pickEnv.refreshRequested = false;
  autoBox.checked = false;
  autoBox.listeners.change();
  await settle();
  check("unticking writes the SESSION, not the pool",
    posted && posted.path === "/api/sessions/s1/auto-key-switch",
    posted && posted.path);
  check("...with the new answer", posted && posted.body.enabled === false,
    posted && JSON.stringify(posted.body));
  check("...keeps the cached row on what the daemon stored",
    pickEnv.sessionsCache[0].auto_key_switch === false,
    String(pickEnv.sessionsCache[0].auto_key_switch));
  check("...and asks the rail to redraw the card's badge",
    pickEnv.refreshRequested === true);
  delete pickEnv.sessionsCache[0].auto_key_switch;
  pickEnv.writeAnswer = { key_id: "x" };

  /* ---- the rail card's AUTO badge (claunch-d2t79) ----
     Drawn from the session's own field on the poll, not from the pool cache:
     the answer is what the daemon will do on a usage limit even before (or
     without) that row's pool ever being read. */
  function actionBar() {
    const bar = el("div");
    bar.children = [];
    bar.querySelector = (sel) =>
      bar.children.find((c) =>
        String(c.className).split(/\s+/).includes(sel.replace(".", ""))) || null;
    const own = (child) => {
      child.remove = () => {
        const at = bar.children.indexOf(child);
        if (at >= 0) bar.children.splice(at, 1);
      };
      child.replaceWith = (next) => {
        own(next);
        const at = bar.children.indexOf(child);
        if (at >= 0) bar.children.splice(at, 1, next);
      };
      return child;
    };
    bar.appendChild = (child) => { bar.children.push(own(child)); return child; };
    return bar;
  }
  const badgeEnv = { asked: null };
  const decorate = new Function("document", "keyPoolProfile", "openKeyPicker",
    slice("decorateAutoKeyBadge") + "; return decorateAutoKeyBadge;")(
    document,
    new Function(slice("keyPoolProfile") + "; return keyPoolProfile;")(),
    (n, p) => { badgeEnv.asked = [n, p]; });

  const bar = actionBar();
  decorate(bar, { name: "s1", profile: "work:claude" });
  check("a session that did not ask for the switch has no badge",
    bar.children.length === 0);
  decorate(bar, { name: "s1", profile: "work:claude", auto_key_switch: true });
  const badge = bar.children[0];
  check("...and the one that did says AUTO",
    !!badge && badge.textContent === "AUTO", badge && badge.textContent);
  check("...as its own button, not the row",
    badge && badge.type === "button" && badge.dataset.name === "s1");
  decorate(bar, { name: "s1", profile: "work:claude", auto_key_switch: true });
  check("...drawn once however often the poll comes round",
    bar.children.length === 1);
  badge.listeners.click({ stopPropagation() {} });
  check("...opening this session's pool",
    JSON.stringify(badgeEnv.asked) === '["s1","work:claude"]',
    JSON.stringify(badgeEnv.asked));
  decorate(bar, { name: "s1", profile: "work:claude" });
  check("...and taken away when the answer is turned off",
    bar.children.length === 0);

  /* ---- the stylesheet holds the chip to a pill ---- */
  check("the AUTO badge has a rule of its own",
    /#session-list \.sess-auto-key \{/.test(css));
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
