/* The detail panel's two opening buttons: Re-inject and Copy.

   A session's opening is typed into its terminal once, at creation, and never
   replayed. When that one delivery does not land the agent sits with no idea
   what it exists for, and these buttons are the person's door back into it:
   Re-inject asks the daemon to type the recorded block again
   (POST /api/sessions/<name>/opening), Copy puts the same text on the
   clipboard so it can be pasted by hand.

   What has to hold, and what each check below is for:

     one text   -- both buttons hand over the SAME text. Re-inject types what
                   the daemon recorded; Copy reads it with GET. A panel that
                   assembled its own would drift from what the session was
                   given, and the copy is what a person debugs with.
     the source -- the daemon says whether the text is `recorded` (the bytes
                   handed over at creation) or `recomposed` (older session:
                   rebuilt from its definition). The outcome line has to say
                   which, because "sent the recorded opening" and "sent a
                   reconstruction" are different claims about what the
                   session now knows.
     the refusal -- a POST that does not land (exited, timeout, not reading)
                   says why, in the panel, not only in the daemon log: the
                   person pressed the button and is watching this line.
     the empty  -- a session with no opening at all says so instead of
                   sending an empty block or a blank success.
     the in-flight -- the panel is rebuilt every 2s; a rebuild mid-request
                   must not hand back a live button while the first press is
                   still in flight (a second press would type it twice).
     no name    -- a session with no name draws no buttons: the routes are
                   keyed by name, and a button that cannot address anything
                   is a button that fails when pressed.

   The stub DOM and the slice() helper follow sesstask_check.js; the daemon's
   own half is checked in tests/test_session_opening.py. */
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
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), title: "", listeners: {},
    disabled: false,
    appendChild(c) { this.kids.push(c); return c; },
    append(...cs) { this.kids.push(...cs); return this; },
    addEventListener(t, fn) { this.listeners[t] = fn; },
    click() { if (this.listeners.click) return this.listeners.click(); },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    all() {
      const out = [this];
      for (const k of this.kids) out.push(...k.all());
      return out;
    },
    find(cls) { return this.all().filter((k) => k.classes.has(cls)); },
    words() { return this.all().map((k) => k.text).join(" "); },
  };
  return n;
}
const document = { createElement: (t) => node(t) };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

/* ---- the stub daemon --------------------------------------------------- */
/* One switchboard for both methods, so a check can assert on the exact call
   the panel made (method, path) and answer with a canned document. */
const calls = [];
let answers = { GET: null, POST: null };
function api(url, opts) {
  const method = (opts && opts.method) || "GET";
  calls.push({ method, url });
  const body = answers[method];
  return Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve(body),
  });
}
const copied = [];
const navigator = { clipboard: { writeText: (text) => { copied.push(text); return Promise.resolve(); } } };
/* `confirm` is the browser's; the panel asks it before typing a second copy
   of an opening that rode the command line. The harness answers from here. */
let confirmed = [];
let confirmAnswer = true;
const confirm = (text) => { confirmed.push(text); return confirmAnswer; };

const sessOpeningNote = new Map();
const sessOpeningBusy = new Set();
const sessTaskOpen = new Set();
const ctx = {};
new Function(
  "exports", "document", "el", "api", "navigator", "confirm", "sessOpeningNote",
  "sessOpeningBusy", "sessTaskOpen", "refreshSession",
  slice("openingSourceWord") + slice("sessOpeningActions") +
  slice("taskIsLong") + slice("sessTask") + `
Object.assign(exports, { actions: sessOpeningActions, word: openingSourceWord,
                         task: sessTask });`
)(ctx, document, el, api, navigator, confirm, sessOpeningNote, sessOpeningBusy,
  sessTaskOpen, () => {});

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const tick = () => new Promise((resolve) => setImmediate(resolve));
const settle = async () => { for (let i = 0; i < 8; i++) await tick(); };
const statusOf = (box) => box.find("sess-opening-status")[0];
const buttons = (box) => ({
  inject: box.find("sess-opening-inject")[0],
  copy: box.find("sess-opening-copy")[0],
});

const RECORDED = {
  session: "s9", source: "recorded", text: "the whole opening", chars: 17,
  images: 2, recorded_at: "2026-10-23T09:00:00+00:00", ok: true, delivered: true,
};
const RECOMPOSED = {
  session: "old", source: "recomposed", text: "rebuilt text", chars: 12,
  images: 0, ok: true, delivered: true,
};

async function main() {
  /* The three words a receipt can end with. An unknown answer is not dressed
     up as a record: it says the record is not there. */
  check("the source words", [ctx.word("recorded"), ctx.word("recomposed"),
                             ctx.word("none"), ctx.word("something else")],
        ["recorded at creation",
         "rebuilt from the session record, not the original",
         "no opening recorded", "no opening recorded"]);

  /* ---- drawn, with a name ---------------------------------------------- */
  let box = ctx.actions({ name: "s9" });
  let { inject, copy } = buttons(box);
  check("both buttons are drawn", [!!inject, !!copy], [true, true]);
  check("re-inject says what it does, and what it does not do",
        /never typed over/.test(inject.title) && /not re-sent/.test(inject.title), true);
  check("copy says it is the same text",
        /same text/.test(copy.title), true);
  /* Before anything is pressed the line explains the pair rather than
     leaving a blank paragraph that reads as a failed load. */
  check("the idle line explains the buttons",
        /Re-inject types the opening/.test(statusOf(box).text), true);

  /* ---- re-inject: read, then POST, and the receipt is the answer ------- */
  answers = { GET: { ...RECORDED, text: "the whole opening", transport: "typed" },
              POST: RECORDED };
  calls.length = 0;
  let pending = inject.click();
  /* Mid-flight: a rebuilt panel must not hand back a live button. */
  check("the press is in flight", sessOpeningBusy.has("s9"), true);
  const rebuilt = ctx.actions({ name: "s9" });
  check("a rebuilt button is disabled while the first press runs",
        buttons(rebuilt).inject.disabled, true);
  await pending;
  await settle();
  check("it reads the opening, then POSTs it",
        calls.map((c) => `${c.method} ${c.url}`),
        ["GET /api/sessions/s9/opening", "POST /api/sessions/s9/opening"]);
  check("the receipt names the text and the source",
        statusOf(ctx.actions({ name: "s9" })).text,
        "handed over 17 characters (recorded at creation) · 2 image(s) not re-sent");
  check("the outcome survives the 2s rebuild",
        sessOpeningNote.get("s9").text.includes("recorded at creation"), true);
  check("the busy mark is cleared", sessOpeningBusy.has("s9"), false);

  /* ---- an opening that rode the command line asks first --------------- */
  /* `transport: "argv"` means the harness took the block as an argument:
     nothing was typed, so nothing was lost, and a re-inject is a second
     copy. A person has to say yes. */
  sessOpeningNote.clear();
  calls.length = 0;
  confirmed = [];
  confirmAnswer = false;
  answers = { GET: { ...RECORDED, transport: "argv" }, POST: RECORDED };
  await buttons(ctx.actions({ name: "s9" })).inject.click();
  await settle();
  check("it asks before typing a second copy", confirmed.length, 1);
  check("...saying why", /command line/.test(confirmed[0]), true);
  check("a refused confirm sends nothing", calls.map((c) => c.method), ["GET"]);
  check("...and says so", statusOf(ctx.actions({ name: "s9" })).text,
        "cancelled — nothing was sent");
  /* Said yes: the send goes through. */
  confirmAnswer = true;
  calls.length = 0;
  answers = { GET: { ...RECORDED, transport: "argv" }, POST: RECORDED };
  await buttons(ctx.actions({ name: "s9" })).inject.click();
  await settle();
  check("a confirmed argv hand-over is sent",
        calls.map((c) => c.method), ["GET", "POST"]);

  /* ---- re-inject: a delivery that did not land says why --------------- */
  sessOpeningNote.clear();
  answers = { GET: { ...RECORDED, transport: "typed" },
              POST: { ...RECORDED, delivered: false,
                      reason: "the terminal took nothing within 90s" } };
  box = ctx.actions({ name: "s9" });
  await buttons(box).inject.click();
  await settle();
  check("an undelivered press is a warning, not a success",
        statusOf(ctx.actions({ name: "s9" })).classes.has("wf-warning"), true);
  check("...carrying the daemon's reason",
        statusOf(ctx.actions({ name: "s9" })).text,
        "not delivered: the terminal took nothing within 90s");

  /* ---- a session with no opening at all ------------------------------- */
  sessOpeningNote.clear();
  calls.length = 0;
  answers = { GET: { session: "s9", source: "none", text: "", chars: 0,
                     images: 0, transport: "typed" }, POST: null };
  await buttons(ctx.actions({ name: "s9" })).inject.click();
  await settle();
  check("no opening means no POST", calls.map((c) => c.method), ["GET"]);
  check("...and the line says so", statusOf(ctx.actions({ name: "s9" })).text,
        "this session has no opening to hand over");

  /* ---- a recomposed answer is labelled as one ------------------------- */
  sessOpeningNote.clear();
  answers = { GET: { ...RECOMPOSED, transport: "typed" }, POST: RECOMPOSED };
  await buttons(ctx.actions({ name: "old" })).inject.click();
  await settle();
  check("a reconstruction is not passed off as the original",
        statusOf(ctx.actions({ name: "old" })).text,
        "handed over 12 characters (rebuilt from the session record, not the original)");

  /* ---- copy: the GET text, verbatim, and nothing else ----------------- */
  sessOpeningNote.clear();
  copied.length = 0;
  calls.length = 0;
  answers = { GET: RECORDED, POST: null };
  await buttons(ctx.actions({ name: "s9" })).copy.click();
  await settle();
  check("copy reads the same route", calls.map((c) => `${c.method} ${c.url}`),
        ["GET /api/sessions/s9/opening"]);
  check("and puts the daemon's text on the clipboard, not the panel's", copied,
        ["the whole opening"]);
  check("the receipt says how much went to the clipboard",
        statusOf(ctx.actions({ name: "s9" })).text,
        "copied 17 characters (recorded at creation) · 2 image(s) not in the text");

  /* ---- copy of an empty opening is not a blank success ---------------- */
  sessOpeningNote.clear();
  copied.length = 0;
  answers = { GET: { session: "s9", source: "none", text: "", chars: 0, images: 0 }, POST: null };
  await buttons(ctx.actions({ name: "s9" })).copy.click();
  await settle();
  check("an empty opening copies nothing", copied, []);
  check("...and says so", statusOf(ctx.actions({ name: "s9" })).text,
        "this session has no opening to copy");

  /* ---- no name, no buttons -------------------------------------------- */
  /* The buttons address the daemon by session name; a nameless row (a harness
     fixture, a half-built payload) cannot, so the section that draws them
     keeps them out rather than building a route from an empty string. */
  check("a session with no name draws no button row",
        ctx.task({ task: "a task but no name" }).find("sess-opening-inject").length, 0);
  check("a named session's task section does draw them",
        ctx.task({ name: "s9", task: "a task" }).find("sess-opening-inject").length, 1);

  /* ---- where the buttons sit ------------------------------------------ */
  /* Source order, for the reason sesstask_check gives: the one harness that
     draws renderSession stubs this section out, so nothing else pins that
     the buttons are drawn by the task section at all -- and that is the
     placement the request asked for ("the detail panel's opening task"). */
  const body = slice("sessTask");
  check("the Opening task section draws them", body.includes("sessOpeningActions(s)"), true);
  check("...after the task text it hands over",
        body.indexOf("sess-task-text") < body.indexOf("sessOpeningActions(s)"), true);
  /* An opening is more than its task: a session with packs and no task text
     still has something to hand over, so the buttons are drawn even where the
     section says there is no task. */
  check("...even for a session with no task text",
        ctx.task({ name: "packsonly" }).find("sess-opening-inject").length, 1);

  if (failures) process.exit(1);
  console.log("openinginject_check ok");
}

main().catch((err) => { console.error(err); process.exit(1); });
