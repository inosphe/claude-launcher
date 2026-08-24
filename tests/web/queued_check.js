/* The queued-deliveries banner: what the daemon is holding for the attached
   session, and WHY it has not been typed in. Four things it must get right —
   it exists only while there is a backlog and a terminal on screen, it names
   the reader's OWN typing as the hold when their keystrokes are the recent
   ones (and someone else's keyboard when they are not), it refits the
   terminal exactly when its shape changes (never on a same-shape repaint),
   and the panel twin only claims "your typing" when the panel describes the
   session actually on screen. Slice the real functions out of app.js and
   drive them against a stub DOM. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  const head = src.lastIndexOf("async ", start) === start - 6 ? start - 6 : start;
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), handlers: {},
    dataset: {}, title: "", type: "",
    appendChild(c) { this.kids.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.appendChild(c)); },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    fire(k, ev) { return Promise.all((this.handlers[k] || []).map((fn) => fn(ev))); },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) {
      this.classes = new Set(String(v).split(/\s+/).filter(Boolean));
    },
    set innerHTML(v) { if (v === "") this.kids = []; },
    classList: {
      add: (...cs) => cs.forEach((c) => n.classes.add(c)),
      remove: (...cs) => cs.forEach((c) => n.classes.delete(c)),
      contains: (c) => n.classes.has(c),
      toggle: (c, on) => {
        const want = on === undefined ? !n.classes.has(c) : !!on;
        if (want) n.classes.add(c); else n.classes.delete(c);
        return want;
      },
    },
  };
  return n;
}
function walk(n, out = []) {
  for (const k of n.kids) { out.push(k); walk(k, out); }
  return out;
}
const texts = (n) => walk(n).map((k) => k.text).join(" | ");
const byClass = (n, cls) => walk(n).find((k) => k.classes.has(cls));

const banner = node("div");
banner.classes.add("hidden");
const $ = (id) => {
  if (id !== "term-queued") throw new Error("unexpected $: " + id);
  return banner;
};
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}

/* app.js's module-level state, declared here the way the real module
   declares it. tqNote must be among them: renderTermQueued READS it, and a
   free read throws — the only reason it survived before being that the
   hidden branch assigns it first, which is an accident of call order, not a
   declaration. */
const stubs = `
let currentPage = "terminal";
let currentName = "s16";
let tqOpen = false;
let tqNote = "";
let lastLocalKey = 0;
let refits = 0;
function refitSoon() { refits++; }
let posted = [];
let flushReply = { flushed: 1, queued: { messages: [], reason: null } };
async function api(url, opts) {
  posted.push([url, (opts || {}).method || "GET"]);
  return { ok: true, json: async () => flushReply };
}
function refreshTermQueued() {}
function refreshSession() {}
`;

const ctx = {};
new Function(
  "exports", "$", "el",
  stubs
  + slice("fmtAge") + slice("localTyping") + slice("queuedReason")
  + slice("flushQueued")
  + slice("queuedMsgRow") + slice("renderTermQueued") + slice("sessQueued") + `
Object.assign(exports, {
  renderTermQueued, sessQueued,
  refits: () => refits,
  typeNow: () => { lastLocalKey = Date.now(); },
  typeLongAgo: () => { lastLocalKey = Date.now() - 60000; },
  setPage: (p) => { currentPage = p; },
  posted: () => posted,
  note: () => tqNote,
  setFlushReply: (r) => { flushReply = r; },
});`
)(ctx, $, el);

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) return;
  failures++;
  console.log(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
};

const Q = (reason, msgs, status) => ({
  reason,
  status: status || (reason === "busy" ? "busy" : "idle"),
  keyboard_busy: reason === "keyboard",
  busy_hold: 120,
  messages: msgs,
});
const MSG = {
  mesh: "team", handle: "worker", from: "operator", type: "ask",
  id: "msg-1", body: "rebase onto master", held_for: 90,
};

/* ---- nothing pending: the banner does not exist ---- */
ctx.renderTermQueued(Q(null, []));
check("empty backlog leaves it hidden", banner.classes.has("hidden"));
ctx.renderTermQueued(null);
check("no payload (old daemon, page swap) leaves it hidden",
      banner.classes.has("hidden"));

/* ---- a backlog: counted, explained, folded shut ---- */
const before = ctx.refits();
ctx.renderTermQueued(Q("busy", [MSG, { ...MSG, id: "msg-2", body: "and push" }]));
check("a backlog shows the strip", !banner.classes.has("hidden"));
check("appearing refits the terminal under it", ctx.refits() === before + 1);
check("the count is the backlog's", texts(banner).includes("2 queued messages"),
      texts(banner));
check("busy names the turn as the hold",
      texts(banner).includes("mid-turn"), texts(banner));
check("shut by default: no message rows yet", !byClass(banner, "tq-msg"));

/* same shape again: the 2s poll must not jiggle the grid */
ctx.renderTermQueued(Q("busy", [MSG, { ...MSG, id: "msg-2" }]));
check("a same-shape repaint does not refit", ctx.refits() === before + 1);

/* ---- fold it open: the actual messages, and another refit ---- */
const head = byClass(banner, "tq-head");
check("the strip is a button", head && head.tag === "button");
head.fire("click");
const rows = walk(banner).filter((k) => k.classes.has("tq-msg"));
check("open shows one row per message", rows.length === 2, rows.length);
check("a row says who, to which handle, through which mesh, how long",
      texts(rows[0]).includes("operator → worker · via team") &&
      texts(rows[0]).includes("ask") && texts(rows[0]).includes("1m"),
      texts(rows[0]));
check("and the body itself", texts(rows[0]).includes("rebase onto master"));
check("folding refits too", ctx.refits() === before + 2);

/* ---- the keyboard hold: whose keyboard? ---- */
ctx.typeLongAgo();
ctx.renderTermQueued(Q("keyboard", [MSG]));
check("a stale local keystroke blames another viewer",
      texts(banner).includes("another viewer"), texts(banner));
check("...and is not dressed as the reader's fault",
      !banner.classes.has("focus-hold"));
ctx.typeNow();
ctx.renderTermQueued(Q("keyboard", [MSG]));
check("a recent local keystroke names YOUR typing",
      texts(banner).includes("YOUR typing"), texts(banner));
check("...and turns the strip loud", banner.classes.has("focus-hold"));

/* ---- other reasons say themselves ---- */
ctx.renderTermQueued(Q("exited", [MSG], "exited"));
check("exited says respawn", texts(banner).includes("respawned"), texts(banner));
ctx.renderTermQueued(Q("busy", [MSG], "starting"));
check("starting is not called mid-turn",
      texts(banner).includes("starting"), texts(banner));
ctx.renderTermQueued(Q("settling", [MSG]));
check("nothing holding it reads as delivering",
      texts(banner).includes("delivering"), texts(banner));

/* ---- off the terminal page it does not exist ---- */
ctx.setPage("home");
ctx.renderTermQueued(Q("busy", [MSG]));
check("a backlog on another page shows nothing", banner.classes.has("hidden"));
ctx.setPage("terminal");

/* ---- the panel twin ---- */
check("an empty backlog renders no panel box",
      ctx.sessQueued({ session: { name: "s16" }, queued: Q(null, []) }) === null);
check("a missing payload (old daemon) renders no panel box",
      ctx.sessQueued({ session: { name: "s16" } }) === null);
ctx.typeNow();
let box = ctx.sessQueued({ session: { name: "s16" }, queued: Q("keyboard", [MSG]) });
check("the panel box is titled with the count",
      texts(box).includes("Queued deliveries (1)"), texts(box));
check("on the session on screen, the hold is YOUR typing",
      texts(box).includes("YOUR typing"), texts(box));
check("and the box wears the hold", box.classes.has("held"));
check("...as a warning, not a note", !!byClass(box, "wf-warning"));
box = ctx.sessQueued({ session: { name: "other" }, queued: Q("keyboard", [MSG]) });
check("another session's panel never blames this tab's typing",
      !texts(box).includes("YOUR typing") && texts(box).includes("keyboard"),
      texts(box));
box = ctx.sessQueued({ session: { name: "s16" }, queued: Q("busy", [MSG]) });
check("a busy hold is a note, not a warning",
      !!byClass(box, "wf-note") && !box.classes.has("held"));

/* ---- "deliver now": the operator ending the wait ----
   The strip used to be one button, so the fold and the delivery would have
   been the same tap. They are separate now, and separate is the property
   worth pinning: a button inside a button is invalid HTML, and the tap that
   meant "let me look" must never type a message into a running turn. */
/* The click handlers are async, so this tail is too — plain CommonJS here,
   no top-level await. */
(async () => {
  ctx.setPage("terminal");
  ctx.renderTermQueued(Q("busy", [MSG]));
  const flush = byClass(banner, "tq-flush");
  check("the strip offers a way to deliver now",
        !!flush && flush.tag === "button");
  check("it is not nested inside the fold toggle",
        !walk(byClass(banner, "tq-head")).some((k) => k.classes.has("tq-flush")));
  check("it says what it does", (flush.text || "").includes("deliver now"));

  await flush.fire("click");
  check("clicking it posts to this session's flush route",
        ctx.posted().some(([u, m]) =>
          u === "/api/sessions/s16/queued/flush" && m === "POST"),
        ctx.posted());
  check("a flush that delivered says nothing extra", ctx.note() === "");

  /* A flush that moved nothing must say so where the reason line was — a
     button that silently does nothing is the bug this whole strip exists to
     prevent. */
  ctx.setFlushReply({ flushed: 0, queued: { messages: [MSG], reason: "exited" } });
  await flush.fire("click");
  check("a flush that delivered nothing explains itself",
        ctx.note().includes("exited"), ctx.note());
  ctx.renderTermQueued(Q("busy", [MSG]));
  check("...in place of the standard reason line",
        texts(banner).includes("exited") && !texts(banner).includes("mid-turn"),
        texts(banner));
  /* and it must not outlive the backlog it was about */
  ctx.renderTermQueued(Q(null, []));
  check("the note dies with the backlog", ctx.note() === "");

  /* the panel twin flushes the session it names, not the one on screen */
  ctx.setFlushReply({ flushed: 1, queued: { messages: [], reason: null } });
  const pbox = ctx.sessQueued({
    session: { name: "other" }, queued: Q("busy", [MSG]),
  });
  const panelFlush = walk(pbox).find((k) => (k.text || "").includes("deliver to"));
  check("the panel names whose terminal the paste lands in",
        !!panelFlush && panelFlush.text.includes("other"),
        panelFlush && panelFlush.text);
  await panelFlush.fire("click");
  check("and posts to that session, not the attached one",
        ctx.posted().some(([u, m]) =>
          u === "/api/sessions/other/queued/flush" && m === "POST"),
        ctx.posted());

  if (failures) { console.log(`${failures} failure(s)`); process.exit(1); }
  console.log("queued_check ok");
})();
