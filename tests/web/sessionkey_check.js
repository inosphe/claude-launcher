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

const railKeyChip = new Function(
  "keyPools", "document", "fetchKeyPool", "refreshSessions", "openKeyPicker",
  keyPoolTtlTable() + "\n" + slice("railKeyChip") + "; return railKeyChip;"
)(keyPools, document, fetchKeyPool, poolEnv.refreshSessions,
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

// A stale cache re-reads on a slow clock, and keeps drawing from what it had.
const stale = { at: Date.now() - 120000, data: two };
keyPools.set("work:claude", stale);
fetches.length = 0;
const staleChip = railKeyChip(session());
check("a stale pool still draws", !!staleChip);
check("a stale pool re-reads once", fetches.length === 1
  && fetches[0] === "/api/profiles/work%3Aclaude/keys", fetches[0]);

/* ---- the picker ---- */
let posted = null;
let dialog = null;
const pickEnv = {
  keyPools,
  fetchKeyPool: () => Promise.resolve(two),
  modalInfo: (title) => { pickEnv.info = title; },
  showModal: (spec) => { dialog = spec; return Promise.resolve(pickEnv.answer); },
  api: (path, opts) => {
    posted = { path, body: JSON.parse(opts.body) };
    return Promise.resolve({ ok: true, status: 200, json: async () => ({ key_id: "x" }) });
  },
  sessionsCache: [{ name: "s1", key_id: "k-bbbbbb" }],
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

(async () => {
  // Cancel: nothing is sent.
  pickEnv.answer = null;
  await openKeyPicker("s1", "work:claude");
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
    !JSON.stringify(dialog).includes("sk-AAAA1111BBBB"));

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
