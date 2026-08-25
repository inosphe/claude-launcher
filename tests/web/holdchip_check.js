/* The delivery chip (#term-hold) in the terminal header: would a message
   arriving RIGHT NOW be typed in or queued — and, when it is queued, by
   what — plus the click that pins delivery shut and lets it go again.

   Two properties make it worth a check of its own rather than folding into
   queued_check: it exists while the backlog is EMPTY (the banner does not,
   so "nothing queued because I pinned this" and "nothing queued, come on
   in" are the same empty list everywhere else), and it is the only control
   that WRITES the hold (the banner's "deliver now" drops one gate for one
   pass and never the standing hold).

   What it must get right: it names the hold a person set ahead of the
   timing holds (busy/typing), it dresses a person-set hold differently from
   an automatic one (pinned is a decision, not a guess that will lapse), the
   click posts to the hold route — never the flush route — and one click is
   one flip however fast it is tapped. Slice the real functions out of
   app.js and drive them against a stub DOM. */
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
    dataset: {}, title: "", type: "", disabled: false,
    appendChild(c) { this.kids.push(c); return c; },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    fire(k, ev) { return Promise.all((this.handlers[k] || []).map((fn) => fn(ev))); },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) {
      this.classes = new Set(String(v).split(/\s+/).filter(Boolean));
    },
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

const chip = node("button");
chip.classes.add("hidden");
const $ = (id) => {
  if (id !== "term-hold") throw new Error("unexpected $: " + id);
  return chip;
};
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}

/* app.js's module-level state, re-declared the way the real module does.
   The toggle calls renderTermQueued + renderHoldChip from the polled
   payload, so both are stubbed here; the async click path is awaited below,
   so `holdBusy` being module state (not a closure) is what the double-tap
   check leans on. */
const stubs = `
let currentPage = "terminal";
let currentName = "s16";
let holdBusy = false;
let posted = [];
let holdReply = { ok: true, doc: { hold: true, queued: { state: "hold", messages: [] } } };
async function api(url, opts) {
  posted.push([url, (opts || {}).method || "GET"]);
  return { ok: holdReply.ok, json: async () => holdReply.doc };
}
let renderTermQueuedCalls = [];
function renderTermQueued(q) { renderTermQueuedCalls.push(q); }
function refreshTermQueued() {}
`;

const ctx = {};
new Function(
  "exports", "$", "el",
  stubs
  + slice("holdChipText") + slice("renderHoldChip") + slice("chipTitle")
  + slice("toggleHold") + `
Object.assign(exports, {
  holdChipText, renderHoldChip, chipTitle, toggleHold,
  setPage: (p) => { currentPage = p; },
  posted: () => posted,
  setHoldReply: (r) => { holdReply = r; },
  rendered: () => renderTermQueuedCalls,
});`
)(ctx, $, el);

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) return;
  failures++;
  console.log(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
};

/* A payload the daemon would send. `state` is always filled; the backlog
   count is the messages the banner would fold open. */
const Q = (state, msgs) => ({
  state,
  messages: msgs || [],
  draft_open: false,
});

/* ---- while the backlog is empty, the chip still answers ---- */
ctx.renderHoldChip(Q("settling"));
check("a free session says delivery: live",
      chip.text.includes("delivery: live"), chip.text);
check("...and wears neither a guess nor a pin",
      !chip.classes.has("hold-blocked") && !chip.classes.has("hold-pinned"));
check("...and is not hidden while a terminal is on screen",
      !chip.classes.has("hidden"));

ctx.renderHoldChip(Q("hold"));
check("an empty backlog under a hold still says held — pinned",
      chip.text.includes("held") && chip.text.includes("pinned"), chip.text);
check("...and wears the pin, not the automatic amber",
      chip.classes.has("hold-pinned") && !chip.classes.has("hold-blocked"));

ctx.renderHoldChip(Q("busy"));
check("a busy turn with nothing yet queued says WOULD queue",
      chip.text.includes("would queue") && chip.text.includes("busy"), chip.text);
check("...and wears the automatic amber, not the pin",
      chip.classes.has("hold-blocked") && !chip.classes.has("hold-pinned"));

/* ---- a backlog folds the count in ---- */
const MSG = { mesh: "team", handle: "worker", from: "op", type: "ask", id: "m1", body: "x", held_for: 1 };
ctx.renderHoldChip(Q("hold", [MSG]));
check("a pinned backlog counts the waiting messages",
      chip.text.includes("pinned") && chip.text.includes("1 queued"), chip.text);

/* ---- the keyboard hold is told apart from the busy one ---- */
ctx.renderHoldChip({ ...Q("keyboard"), draft_open: true });
check("an unsent line is named, because its fix is not 'wait'",
      chip.text.includes("unsent line"), chip.text);
ctx.renderHoldChip(Q("keyboard"));
check("a bare keystroke is just typing",
      chip.text.includes("typing") && !chip.text.includes("unsent line"),
      chip.text);

/* ---- hold is told ahead of the timing holds, never behind ---- */
check("pinned outranks busy: the chip says hold, not busy",
      ctx.holdChipText(Q("hold"), 2).includes("pinned"));
check("exited is the one hold with no button",
      ctx.holdChipText(Q("exited"), 0).includes("exited"));

/* ---- off the terminal page, or no payload: the chip does not exist ---- */
ctx.setPage("home");
ctx.renderHoldChip(Q("settling"));
check("another page leaves it hidden", chip.classes.has("hidden"));
ctx.setPage("terminal");
ctx.renderHoldChip(null);
check("an old daemon (no payload) leaves it hidden",
      chip.classes.has("hidden"));

/* ---- the tooltip says what the click will do ---- */
ctx.renderHoldChip(Q("hold"));
check("a pinned chip's tooltip offers RESUME",
      chip.title.includes("RESUME"), chip.title);
ctx.renderHoldChip(Q("settling"));
check("a live chip's tooltip offers HOLD, for the hands-off case",
      chip.title.includes("HOLD") && chip.title.includes("reading"),
      chip.title);

/* ---- the click toggles the hold, via the hold route, once per flip ---- */
(async () => {
  await ctx.toggleHold();
  check("the click posts to the hold route",
        ctx.posted().some(([u, m]) =>
          u === "/api/sessions/s16/queued/hold" && m === "POST"),
        ctx.posted());
  check("...never to the flush route",
        !ctx.posted().some(([u]) => u.includes("queued/flush")));
  check("...and re-renders from the polled payload",
        ctx.rendered().length >= 1);

  /* a double-tap is one flip, not two racing writes */
  const before = ctx.posted().length;
  const first = ctx.toggleHold();
  const second = ctx.toggleHold();
  await first; await second;
  check("a double-tap still posts once",
        ctx.posted().filter(([u]) => u.includes("queued/hold")).length ===
          ctx.posted().slice(0, before).filter(([u]) => u.includes("queued/hold")).length + 1,
        ctx.posted());

  /* An old daemon without the route: the write is declined, so the chip
     must not show the new state it wished for. `holdBusy` is released — the
     *call* is over — but the chip stays disabled in the stub, because the
     real re-render comes from the next poll (refreshTermQueued, a no-op
     here). What matters is what it SHOWS, and that is the last truth. */
  const before_failed = chip.text;
  ctx.setHoldReply({ ok: false, doc: {} });
  await ctx.toggleHold();
  check("a declined toggle does not paint the state it wished for",
        chip.text === before_failed, chip.text);
  ctx.renderHoldChip(Q("settling"));
  check("the next poll lifts the disabled state again",
        !chip.disabled);

  if (failures) { console.log(`${failures} failure(s)`); process.exit(1); }
  console.log("holdchip_check ok");
})();
