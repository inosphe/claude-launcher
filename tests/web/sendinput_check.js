/* The send-keys input under the terminal. A native field where xterm's
   composer is a terminal — the field the reader types a prompt into,
   Enter handing the line to the session through the same send-keys
   passthrough `claunch send-keys` uses, Ctrl+J putting a newline in it
   instead.

   The box has to hold the contract the whole raw-keystroke path lives under:
   the text and its Enter go in ONE /keys call (a client that splits them
   re-spreads the submit/enter split across call sites — the split belongs to
   Session.send_keys alone), an empty or unaddressed line sends nothing, an
   operator feedback choice rides that same call as one point (none by
   default, reset once the send lands), a refusal's words are shown and the
   half-typed line kept, a dead daemon does not look like a delivery, and a
   session that has ended has the box closed with the reason shown, Ctrl+J
   inserts a newline at the caret rather than sending — taken in the capture
   phase at the window, because the Firefox family claims that chord for its
   downloads panel — and a line that carries a newline goes as ONE paste (the
   keys path would write a raw LF, which every harness reads as a submit, so
   the block would arrive a line at a time). Slice the real functions out of
   app.js, drive them against a stub DOM, and check all of it. */
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
    tag, classes: new Set(), _text: "", title: "",
    value: "", disabled: false,
    selectionStart: 0, selectionEnd: 0,
    style: {}, scrollHeight: 0,
    setSelectionRange(a, b) { n.selectionStart = a; n.selectionEnd = b; },
    focus() {},
    classList: {
      add(c) { n.classes.add(c); },
      remove(c) { n.classes.delete(c); },
      toggle(c, on) {
        if (on === undefined) {
          if (n.classes.has(c)) n.classes.delete(c); else n.classes.add(c);
        } else if (on) n.classes.add(c); else n.classes.delete(c);
      },
      contains(c) { return n.classes.has(c); },
    },
  };
  Object.defineProperty(n, "textContent", {
    get() { return n._text; },
    set(v) { n._text = String(v); },
  });
  return n;
}

/* the daemon: every call recorded, every answer scripted by the test */
let sent = [];
let reply = { ok: true, doc: {} };
let inFlight = 0;
let maxInFlight = 0;
const api = async (p, opts) => {
  inFlight++;
  maxInFlight = Math.max(maxInFlight, inFlight);
  try { await new Promise((r) => setImmediate(r)); } finally { inFlight--; }
  const type = opts.headers["Content-Type"];
  // A key line posts JSON; an image posts the blob itself, so only the
  // former is parsed — parsing the latter would be the test inventing a
  // shape the page never sends.
  const body = String(type).startsWith("image/") ? opts.body : JSON.parse(opts.body);
  sent.push({ path: p, method: opts.method, contentType: type, body });
  if (reply.throw) throw new Error("offline");
  if (reply.html) {
    // what fetch hands back after following the relay's redirect to its
    // login page: a page, not the route's JSON
    return { ok: true, status: 200, redirected: true,
             json: async () => { throw new SyntaxError("Unexpected token <"); } };
  }
  const doc = typeof reply.doc === "function" ? reply.doc(sent.length) : reply.doc;
  return { ok: reply.ok, status: reply.status || 200, json: async () => doc };
};

const FIELD = node("textarea");
const BTN = node("button");
const NOTE = node("span");
/* the browser's clipboard, scripted by each test: an image, nothing, or a
   refusal (no permission, an insecure origin, a browser without read()) */
let clipboard = { items: [] };
const navigator = {
  clipboard: {
    read: async () => {
      if (clipboard.refuse) throw new Error("NotAllowedError");
      if (clipboard.absent) return [];
      return clipboard.items;
    },
  },
};
const imageItem = (type, blob) => ({
  types: [type],
  getType: async () => blob,
});

/* the page's element lookup, over the elements this strip owns */
const SCORE_BOX = node("span");
SCORE_BOX.classes.add("hidden");
/* the feedback radios, one stub per kind: the browser would uncheck the
   rest of the group on its own, this DOM leaves that to the code under
   test — which is exactly what the contract checks below read */
const SCORE_NONE = node("input");
SCORE_NONE.checked = true;
const SCORE_REWARD = node("input");
const SCORE_PENALTY = node("input");
const SCORE_COUNTS = node("span");
const PICKER = node("input");
PICKER.files = [];
PICKER.clicked = 0;
PICKER.click = () => { PICKER.clicked++; };
const $ = (id) => ({
  "term-image-file": PICKER,
  "term-input-field": FIELD,
  "term-input-send": BTN,
  "term-input-note": NOTE,
  "term-score-goal": SCORE_BOX,
  "term-score-feedback-none": SCORE_NONE,
  "term-score-feedback-reward": SCORE_REWARD,
  "term-score-feedback-penalty": SCORE_PENALTY,
  "term-score-counts": SCORE_COUNTS,
}[id] || null);

/* which element the page would call focused. The window hook reads it to
   decide whether this Ctrl+J is the composer's or the browser's. */
let ACTIVE = FIELD;
const document = { get activeElement() { return ACTIVE; } };

const ctx = {};
/* the kinds array is data, not a function: read the declaration out of the
   source instead of copying its values here, so the two cannot drift */
const kindsDecl = src.match(/const SCORE_FEEDBACK_KINDS = \[[^\]]*\];/);
if (!kindsDecl) throw new Error("missing SCORE_FEEDBACK_KINDS");
const imageKindsDecl = src.match(/const PASTE_IMAGE_KINDS = \{[^}]*\};/);
if (!imageKindsDecl) throw new Error("missing PASTE_IMAGE_KINDS");
new Function(
  "exports", "api", "$", "navigator", "document",
  `let currentName = null;
let sessionEnded = false;
let sessionsCache = [];
let harnessDetails = {};
let sessJournalBox = null;
function sessInputJournalFill() {}
${kindsDecl[0]}
${imageKindsDecl[0]}
` + slice("termInputNote") + `
` + slice("sendKeyLine") + `
` + slice("currentScoreFeedback") + `
` + slice("renderScoreGoal") + `
` + slice("setScoreFeedbackChoice") + `
` + slice("setScoreFeedbackDisabled") + `
` + slice("termInputQueueNote") + `
` + slice("autogrowTermInput") + `
` + slice("insertTermInputText") + `
` + slice("imageKindOf") + `
` + slice("isAltV") + `
` + slice("uploadImage") + `
` + slice("uploadPastedImage") + `
` + slice("sendImageFiles") + `
` + slice("clipboardImage") + `
` + slice("clipboardRefusal") + `
` + slice("pasteClipboardImage") + `
` + slice("pastedImageFiles") + `
` + slice("onTermInputPaste") + `
` + slice("onTerminalPaste") + `
` + slice("sessionTakesImages") + `
` + slice("onTermKeyEvent") + `
` + slice("openImagePicker") + `
` + slice("onImagePickerChange") + `
` + slice("isCtrlJ") + `
` + slice("onTermInputKeydown") + `
` + slice("onWindowCtrlJ") + `
Object.assign(exports, {
  sendKeyLine,
  termInputQueueNote,
  termInputNote,
  renderScoreGoal,
  currentScoreFeedback,
  onTermInputKeydown,
  onWindowCtrlJ,
  onTermInputPaste,
  onTerminalPaste,
  onTermKeyEvent,
  openImagePicker,
  onImagePickerChange,
  sendImageFiles,
  pasteClipboardImage,
  setHarnesses: (h) => { harnessDetails = h; },
  setSession: (name, ended) => { currentName = name; sessionEnded = !!ended; },
  setSessions: (rows) => { sessionsCache = rows; },
});`
)(ctx, api, $, navigator, document);

const box = () => ({ field: FIELD, btn: BTN, note: NOTE });

/* a keydown as the browser delivers it, with the two things the handler
   answers with recorded */
function press(key, mods = {}) {
  const ev = {
    key, ctrlKey: false, shiftKey: false, altKey: false, metaKey: false,
    isComposing: false, keyCode: 0, currentTarget: FIELD, target: FIELD,
    prevented: false, preventDefault() { ev.prevented = true; },
    ...mods,
  };
  ctx.onTermInputKeydown(ev);
  return ev;
}

/* the same keydown, delivered the way the window's capture listener gets it
   — before any element handler, and with the propagation stop recorded */
function capture(key, mods = {}) {
  const ev = {
    key, ctrlKey: false, shiftKey: false, altKey: false, metaKey: false,
    isComposing: false, keyCode: 0, currentTarget: null, target: ACTIVE,
    prevented: false, preventDefault() { ev.prevented = true; },
    stopped: false, stopPropagation() { ev.stopped = true; },
    ...mods,
  };
  ctx.onWindowCtrlJ(ev);
  return ev;
}

/* let every pending await in the code under test run to the end */
const settle = async () => {
  for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r));
};

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) return;
  failures++;
  console.log(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
};

async function main() {
  /* ---- an empty box sends nothing ---- */
  ctx.setSession("coder4", false);
  const b = box();
  const none = await ctx.sendKeyLine(b.field, b.btn, b.note);
  check("whitespace sends nothing", sent.length === 0 && none === false, sent);

  /* ---- no session to address, no send ---- */
  sent = [];
  ctx.setSession(null, false);
  b.field.value = "  hi  ";
  await ctx.sendKeyLine(b.field, b.btn, b.note);
  check("no attached session sends nothing", sent.length === 0, sent);

  /* ---- an ended session queues the line instead of refusing it ---- */
  const live = ctx.termInputQueueNote(false);
  const dead = ctx.termInputQueueNote(true);
  check("a live session has no queue note", live === "");
  check("an ended session says lines are queued", /queued/.test(dead), dead);
  sent = [];
  reply = { ok: true, status: 202,
            doc: { ok: true, queued: true, position: 2, input_id: "x" } };
  ctx.setSession("coder4", true);
  b.field.value = "anything";
  const queued = await ctx.sendKeyLine(b.field, b.btn, b.note);
  check("an ended session still sends — the daemon queues it",
        sent.length === 1 && queued === true, sent);
  check("...with the durable input id the queue is keyed on",
        typeof sent[0].body.input_id === "string" && sent[0].body.input_id,
        sent[0].body);
  check("...and the field empties", b.field.value === "");
  check("...and the note says it is queued, not typed",
        /queued \(2 waiting\)/.test(NOTE.textContent), NOTE.textContent);
  reply = { ok: true, doc: {} };

  /* ---- the live send: ONE call, text and Enter together ---- */
  sent = [];
  reply = { ok: true, doc: {} };
  ctx.setSession("coder4", false);
  b.field.value = "  rebase onto master  ";
  const ok = await ctx.sendKeyLine(b.field, b.btn, b.note);
  check("a live send returns true", ok === true);
  check("exactly one request — text and Enter never split",
        sent.length === 1, sent);
  check("...to the attached session's keys route",
        sent[0].path === "/api/sessions/coder4/keys", sent[0].path);
  check("...as a POST", sent[0].method === "POST");
  check("...with the JSON content type the daemon parses",
        sent[0].contentType === "application/json");
  check("both the line and its Enter ride in one keys list",
        Array.isArray(sent[0].body.keys) &&
        sent[0].body.keys.length === 2 &&
        sent[0].body.keys[0] === "rebase onto master" &&
        sent[0].body.keys[1] === "Enter",
        sent[0].body);
  check("the field is emptied for the next line", b.field.value === "");
  check("no pitfall keys field", sent[0].body.paste === undefined, sent[0].body);
  check("no feedback chosen rides as none", sent[0].body.feedback === "none",
        sent[0].body);

  /* ---- operator feedback rides the send when the feature is on ----
     The control is only shown for an opted-in session; a chosen point goes
     with the next input, the daemon's answer refreshes the counts, and the
     choice resets so a point is never spent twice. */
  sent = [];
  reply = { ok: true,
            doc: { score_goal: { enabled: true, reward: 1, penalty: 1, active: true } } };
  ctx.setSession("coder4", false);
  ctx.setSessions([
    { name: "coder4", status: "idle", score_goal: true, user_reward: 1, user_penalty: 0 },
  ]);
  SCORE_BOX.classes.delete("hidden");
  SCORE_NONE.checked = false;
  SCORE_PENALTY.checked = true;
  b.field.value = "fix the lint";
  const fb = await ctx.sendKeyLine(b.field, b.btn, b.note);
  check("a send carrying feedback returns true", fb === true);
  check("the chosen point rides the one send",
        sent.length === 1 && sent[0].body.feedback === "penalty",
        sent[0] && sent[0].body);
  check("the choice resets for the next input",
        SCORE_NONE.checked === true && SCORE_PENALTY.checked === false &&
        SCORE_REWARD.checked === false,
        { none: SCORE_NONE.checked, reward: SCORE_REWARD.checked, penalty: SCORE_PENALTY.checked });
  check("the cache and the counts refresh from the daemon's answer",
        SCORE_COUNTS.textContent === "R1 · P1", SCORE_COUNTS.textContent);
  sent = [];
  reply = { ok: true, doc: {} };
  b.field.value = "and the tests";
  await ctx.sendKeyLine(b.field, b.btn, b.note);
  check("the send after the reset carries none again",
        sent.length === 1 && sent[0].body.feedback === "none",
        sent[0] && sent[0].body);
  /* a point checked behind a hidden control does not ride: the early return
     in currentScoreFeedback is the only thing keeping it out of the body */
  sent = [];
  SCORE_NONE.checked = false;
  SCORE_REWARD.checked = true;
  SCORE_BOX.classes.add("hidden");
  b.field.value = "quiet note";
  await ctx.sendKeyLine(b.field, b.btn, b.note);
  check("a checked point behind a hidden control does not ride",
        sent.length === 1 && sent[0].body.feedback === "none",
        sent[0] && sent[0].body);
  SCORE_NONE.checked = true;
  SCORE_REWARD.checked = false;
  /* an ended session greys the radios out and shows none, whatever was
     checked — a disabled point is not a choice the operator can still make */
  SCORE_BOX.classes.delete("hidden");
  SCORE_NONE.checked = false;
  SCORE_PENALTY.checked = true;
  ctx.setSessions([
    { name: "coder4", status: "exited", score_goal: true, user_reward: 1, user_penalty: 1 },
  ]);
  ctx.setSession("coder4", true);
  ctx.renderScoreGoal();
  check("an ended session disables every radio",
        SCORE_NONE.disabled && SCORE_REWARD.disabled && SCORE_PENALTY.disabled,
        { none: SCORE_NONE.disabled, reward: SCORE_REWARD.disabled, penalty: SCORE_PENALTY.disabled });
  check("...and the choice reads none",
        SCORE_NONE.checked === true && SCORE_PENALTY.checked === false,
        { none: SCORE_NONE.checked, penalty: SCORE_PENALTY.checked });
  /* a checked-but-disabled radio reads none: the guard is what stands when
     the control was greyed out without clearing the mark (sendKeyLine itself
     refuses an ended session before this point, so this is read directly) */
  SCORE_PENALTY.checked = true;
  SCORE_NONE.checked = false;
  check("a checked-but-disabled point reads none",
        ctx.currentScoreFeedback() === "none",
        ctx.currentScoreFeedback());
  SCORE_PENALTY.checked = false;
  SCORE_NONE.checked = true;
  SCORE_NONE.disabled = SCORE_REWARD.disabled = SCORE_PENALTY.disabled = false;
  SCORE_NONE.checked = true;
  SCORE_BOX.classes.add("hidden");
  ctx.setSessions([]);
  ctx.setSession("coder4", false);
  reply = { ok: true, doc: {} };

  /* ---- a refusal keeps the words and the line ---- */
  sent = [];
  reply = { ok: false, status: 409,
            doc: { error: "session 'coder4': someone is typing there right now — nothing was sent." } };
  b.field.value = "let me in";
  const refused = await ctx.sendKeyLine(b.field, b.btn, b.note);
  check("a refusal is not a success", refused === false);
  check("the daemon's own words are shown",
        NOTE.textContent.includes("someone is typing there"), NOTE.textContent);
  check("...as a warning, not a note", NOTE.classes.has("wf-warning"));
  check("the half-typed line is not thrown away", b.field.value === "let me in");
  check("the button is usable again", b.btn.disabled === false);

  /* ---- an unreachable daemon must not look like a delivery ---- */
  sent = [];
  reply = { throw: true };
  b.field.value = "hello?";
  const lost = await ctx.sendKeyLine(b.field, b.btn, b.note);
  check("a dead daemon is reported as such", lost === false &&
        NOTE.textContent.includes("nothing was sent"), NOTE.textContent);
  check("the line survives that too", b.field.value === "hello?");
  check("and the button is usable again", b.btn.disabled === false);

  /* ---- Ctrl+J is a newline at the caret, not a send ---- */
  sent = [];
  reply = { ok: true, doc: {} };
  ctx.setSession("coder4", false);
  FIELD.disabled = false;
  FIELD.value = "first";
  FIELD.selectionStart = FIELD.selectionEnd = 5;
  const cj = press("j", { ctrlKey: true });
  check("Ctrl+J is taken from the browser", cj.prevented === true);
  check("Ctrl+J puts a newline in the box", FIELD.value === "first\n",
        FIELD.value);
  check("...with the caret after it", FIELD.selectionStart === 6,
        FIELD.selectionStart);
  check("...and sends nothing", sent.length === 0, sent);

  /* ---- it inserts where the caret is, over a selection ---- */
  FIELD.value = "abcd";
  FIELD.selectionStart = 1; FIELD.selectionEnd = 3;
  press("j", { ctrlKey: true });
  check("Ctrl+J replaces the selection", FIELD.value === "a\nd", FIELD.value);

  /* ---- the window hook takes Ctrl+J before the browser does ----
     Firefox opens its downloads panel on this chord, and a listener that
     only sees the event on the way back up loses the race. The capture
     listener is what the page actually relies on, so it is driven here the
     way the window would deliver it. */
  sent = [];
  FIELD.value = "first";
  FIELD.selectionStart = FIELD.selectionEnd = 5;
  ACTIVE = FIELD;
  const wcj = capture("j", { ctrlKey: true });
  check("the window hook takes the chord", wcj.prevented === true);
  check("...and stops it reaching anything else", wcj.stopped === true);
  check("...putting the newline in the box", FIELD.value === "first\n",
        FIELD.value);
  check("...and sending nothing", sent.length === 0, sent);

  /* a layout that labels the key differently still reports keyCode 74 */
  FIELD.value = "first";
  FIELD.selectionStart = FIELD.selectionEnd = 5;
  const wkc = capture("Unidentified", { ctrlKey: true, keyCode: 74 });
  check("keyCode 74 counts as the same chord",
        wkc.prevented === true && FIELD.value === "first\n", FIELD.value);

  /* elsewhere on the page the chord belongs to the browser */
  FIELD.value = "kept";
  ACTIVE = null;
  const wout = capture("j", { ctrlKey: true });
  check("Ctrl+J outside the composer is left to the browser",
        wout.prevented === false && FIELD.value === "kept", FIELD.value);
  ACTIVE = FIELD;

  /* a disabled composer does not claim it either */
  FIELD.disabled = true;
  const wdis = capture("j", { ctrlKey: true });
  check("a disabled composer does not claim the chord",
        wdis.prevented === false, wdis);
  FIELD.disabled = false;

  /* ---- Enter sends, and an IME committing a syllable does not ---- */
  sent = [];
  FIELD.value = "send me";
  const ime = press("Enter", { isComposing: true });
  check("an Enter that commits an IME syllable sends nothing",
        sent.length === 0 && ime.prevented === false, sent);
  const ent = press("Enter");
  await settle();
  check("Enter sends the line",
        sent.length === 1 && ent.prevented === true, sent);
  check("...and the line it sent is the one in the box",
        sent[0].body.keys[0] === "send me", sent[0].body);

  /* ---- a line with a newline in it goes as ONE paste ---- */
  sent = [];
  reply = { ok: true, doc: {} };
  FIELD.value = "line one\nline two";
  const multi = await ctx.sendKeyLine(FIELD, BTN, NOTE);
  check("a multi-line send returns true", multi === true);
  check("exactly one request for the block", sent.length === 1, sent);
  check("it is a paste, carrying both lines",
        sent[0].body.paste === "line one\nline two", sent[0].body);
  check("...submitted by the daemon's own separate Enter",
        sent[0].body.enter === true, sent[0].body);
  check("...never as keys — a raw LF there is a submit per line",
        sent[0].body.keys === undefined, sent[0].body);
  check("...with the operator's force, like the one-line path",
        sent[0].body.force === true, sent[0].body);
  check("...and the same duplicate-suppression id",
        typeof sent[0].body.input_id === "string" && sent[0].body.input_id,
        sent[0].body);
  check("the box is emptied for the next block", FIELD.value === "");

  /* ---- Alt+V hands the clipboard image to the session ---- */
  sent = [];
  reply = { ok: true, doc: { ok: true, path: "C:/state/sessions/coder4/pastes/x.png",
                             bytes: 12, delivered: true, keys: ["M-v"], reason: "" } };
  ctx.setSession("coder4", false);
  FIELD.disabled = false;
  FIELD.value = "look at ";
  FIELD.selectionStart = FIELD.selectionEnd = 8;
  const blob = { type: "image/png", size: 12 };
  clipboard = { items: [imageItem("image/png", blob)] };
  const altv = press("v", { altKey: true });
  await settle();
  check("Alt+V is taken from the browser", altv.prevented === true);
  check("exactly one upload", sent.length === 1, sent);
  check("...to the session's paste-image route",
        sent[0].path === "/api/sessions/coder4/paste-image", sent[0].path);
  check("...carrying the blob itself, typed as the image it is",
        sent[0].body === blob && sent[0].contentType === "image/png", sent[0]);
  check("what was already typed is left alone — no path goes in the line",
        FIELD.value === "look at ", FIELD.value);
  check("...and the line says the image reached the session",
        NOTE.textContent.includes("sent to the session"), NOTE.textContent);
  check("...with no keystroke sent from the page: the daemon sends it, on the "
        + "machine whose clipboard now holds the image",
        !sent.some((r) => r.path.endsWith("/keys")), sent);

  /* ---- an empty clipboard says so and uploads nothing ---- */
  sent = [];
  FIELD.value = "";
  clipboard = { absent: true };
  await ctx.pasteClipboardImage(NOTE);
  check("an empty clipboard uploads nothing", sent.length === 0, sent);
  check("...and says what was wrong",
        NOTE.textContent.includes("no image"), NOTE.textContent);
  check("...as a warning", NOTE.classes.has("wf-warning"));

  /* ---- a refused clipboard points at the way that still works ---- */
  sent = [];
  clipboard = { refuse: true };
  await ctx.pasteClipboardImage(NOTE);
  check("a refused clipboard uploads nothing", sent.length === 0, sent);
  check("...and names Ctrl+V as the way through",
        NOTE.textContent.includes("Ctrl+V"), NOTE.textContent);

  /* ---- an ordinary Ctrl+V carrying an image takes the same path ---- */
  sent = [];
  FIELD.value = "";
  const pasted = { type: "image/png", size: 9 };
  const ev = {
    currentTarget: FIELD, target: FIELD, prevented: false,
    preventDefault() { ev.prevented = true; },
    clipboardData: { items: [
      { kind: "string", type: "text/plain" },
      { kind: "file", type: "image/png", getAsFile: () => pasted },
    ] },
  };
  ctx.onTermInputPaste(ev);
  await settle();
  check("a pasted image file is uploaded too", sent.length === 1, sent);
  check("...and the browser's own paste is taken", ev.prevented === true);
  check("...and the box is left empty here too", FIELD.value === "", FIELD.value);

  /* ---- a paste with no image is left to the browser ---- */
  sent = [];
  const textEv = {
    currentTarget: FIELD, target: FIELD, prevented: false,
    preventDefault() { textEv.prevented = true; },
    clipboardData: { items: [{ kind: "string", type: "text/plain" }] },
  };
  ctx.onTermInputPaste(textEv);
  await settle();
  check("a text paste is not intercepted",
        sent.length === 0 && textEv.prevented === false, sent);

  /* ---- a refused upload keeps the daemon's words and types nothing ---- */
  sent = [];
  FIELD.value = "keep me";
  clipboard = { items: [imageItem("image/png", blob)] };
  reply = { ok: false, status: 413, doc: { error: "the image is larger than 24 MiB" } };
  await ctx.pasteClipboardImage(NOTE);
  check("a refused upload leaves the line alone", FIELD.value === "keep me", FIELD.value);
  check("...and shows the daemon's reason",
        NOTE.textContent.includes("larger than"), NOTE.textContent);

  /* ---- stored but not handed over is its own answer ----
     The store can succeed while the hand-over does not: a harness with no
     declared image key, a clipboard tool that is not installed. The reason
     names the thing that has to change, so it is shown as it came. */
  sent = [];
  FIELD.value = "keep me";
  reply = { ok: true, doc: { ok: true, path: "C:/state/sessions/coder4/pastes/y.png",
                             bytes: 12, delivered: false, keys: [],
                             reason: "harness 'py' has no image paste key declared" } };
  await ctx.pasteClipboardImage(NOTE);
  check("an undelivered image leaves the line alone",
        FIELD.value === "keep me", FIELD.value);
  check("...and the daemon's reason is shown, not a rewrite of it",
        NOTE.textContent.includes("no image paste key declared"), NOTE.textContent);
  check("...as a warning", NOTE.classes.has("wf-warning"));

  /* ==== the same result wherever the browser is ==========================
     Same PC or another PC, direct or through the relay: the image always
     leaves THIS browser as bytes, and the harness is never left to read a
     clipboard on its own — that would be the daemon PC's clipboard. */
  const OK_DOC = { ok: true, path: "C:/state/sessions/coder4/pastes/z.png",
                   bytes: 12, delivered: true, keys: ["M-v"], reason: "" };

  /* ---- Alt+V by the physical key: a Hangul IME reports another letter ---- */
  sent = [];
  reply = { ok: true, doc: OK_DOC };
  clipboard = { items: [imageItem("image/png", blob)] };
  const hangul = press("ㅍ", { altKey: true, code: "KeyV" });
  await settle();
  check("Alt+V with a Hangul IME on is still Alt+V",
        hangul.prevented === true && sent.length === 1, sent);

  /* ---- a page that cannot read the clipboard is not told "no image" ---- */
  const realClip = navigator.clipboard;
  navigator.clipboard = undefined;
  globalThis.isSecureContext = false;
  sent = [];
  await ctx.pasteClipboardImage(NOTE);
  check("no clipboard reader uploads nothing", sent.length === 0, sent);
  check("...and does not claim the clipboard holds no image",
        !NOTE.textContent.includes("no image"), NOTE.textContent);
  check("...it names the insecure origin (http:// from another PC)",
        NOTE.textContent.includes("secure origin"), NOTE.textContent);
  check("...and the two ways that still work",
        NOTE.textContent.includes("Ctrl+V") && NOTE.textContent.includes("image button"),
        NOTE.textContent);
  globalThis.isSecureContext = true;
  await ctx.pasteClipboardImage(NOTE);
  check("a secure page without a reader says the browser has none",
        NOTE.textContent.includes("no clipboard reader"), NOTE.textContent);
  delete globalThis.isSecureContext;
  navigator.clipboard = realClip;

  /* ---- xterm's key hook: Alt+V never reaches the PTY as ESC v ---- */
  ctx.setSessions([{ name: "coder4", harness: "claude" }]);
  ctx.setHarnesses({ claude: { name: "claude", image_paste_keys: ["M-v"] },
                     sh: { name: "sh", image_paste_keys: [] } });
  sent = [];
  clipboard = { items: [imageItem("image/png", blob)] };
  const termKey = (type, mods) => {
    const ev = { type, key: "v", code: "KeyV", altKey: false, ctrlKey: false,
                 metaKey: false, prevented: false,
                 preventDefault() { ev.prevented = true; }, ...mods };
    return { ev, pass: ctx.onTermKeyEvent(ev) };
  };
  const down = termKey("keydown", { altKey: true });
  const press2 = termKey("keypress", { altKey: true });
  const up = termKey("keyup", { altKey: true });
  await settle();
  check("the terminal keeps Alt+V from xterm on every phase",
        down.pass === false && press2.pass === false && up.pass === false);
  check("...and uploads the browser's image once, on keydown",
        sent.length === 1 && sent[0].path === "/api/sessions/coder4/paste-image", sent);
  check("...with the browser's own default taken", down.ev.prevented === true);
  const plainV = termKey("keydown", {});
  check("a plain v goes to the terminal", plainV.pass === true);
  const ctrlAltV = termKey("keydown", { altKey: true, ctrlKey: true });
  check("Ctrl+Alt+V goes to the terminal", ctrlAltV.pass === true);
  ctx.setSessions([{ name: "coder4", harness: "sh" }]);
  sent = [];
  const shell = termKey("keydown", { altKey: true });
  await settle();
  check("a harness with no image key keeps its Alt+V (Meta-v)",
        shell.pass === true && sent.length === 0, sent);
  ctx.setSessions([{ name: "coder4", harness: "unknown-to-the-page" }]);
  sent = [];
  const unknown = termKey("keydown", { altKey: true });
  await settle();
  check("a harness the page has no record of goes through the route, "
        + "and the daemon answers for it",
        unknown.pass === false && sent.length === 1, sent);
  ctx.setSessions([{ name: "coder4", harness: "claude" }]);

  /* ---- an image pasted onto the terminal is taken ahead of xterm ---- */
  sent = [];
  const shots = [{ type: "image/png", size: 3, name: "a.png" },
                 { type: "image/jpeg", size: 4, name: "b.jpg" }];
  const tev = {
    prevented: false, stopped: false,
    preventDefault() { tev.prevented = true; },
    stopImmediatePropagation() { tev.stopped = true; },
    clipboardData: { items: shots.map((f) => ({ kind: "file", type: f.type,
                                                getAsFile: () => f })) },
  };
  ctx.onTerminalPaste(tev);
  await settle();
  check("images pasted on the terminal are uploaded, all of them",
        sent.length === 2 && sent[0].body === shots[0] && sent[1].body === shots[1], sent);
  check("...and xterm never sees that paste", tev.prevented && tev.stopped);
  const textOnTerm = {
    prevented: false, preventDefault() { textOnTerm.prevented = true; },
    clipboardData: { items: [{ kind: "string", type: "text/plain" }] },
  };
  sent = [];
  ctx.onTerminalPaste(textOnTerm);
  check("a text paste on the terminal is xterm's", !textOnTerm.prevented && sent.length === 0);

  /* ---- files copied in a file manager arrive on `files`, not `items` ---- */
  sent = [];
  const copied = { type: "", size: 5, name: "shot.PNG" };
  const fev = {
    currentTarget: FIELD, target: FIELD, prevented: false,
    preventDefault() { fev.prevented = true; },
    clipboardData: { items: [], files: [copied] },
  };
  ctx.onTermInputPaste(fev);
  await settle();
  check("a copied image file is taken from `files`",
        sent.length === 1 && fev.prevented, sent);
  check("...typed from its extension when the browser gave it no type",
        sent[0] && sent[0].contentType === "image/png", sent[0]);

  /* ---- the image button: several files, one after another ---- */
  sent = [];
  maxInFlight = 0;
  reply = { ok: true, doc: OK_DOC };
  ctx.openImagePicker();
  check("the image button opens the file picker", PICKER.clicked === 1);
  const picked = [
    { type: "image/png", size: 1, name: "one.png" },
    { type: "text/plain", size: 1, name: "notes.txt" },
    { type: "image/webp", size: 1, name: "two.webp" },
    { type: "", size: 1, name: "three.jpeg" },
  ];
  PICKER.files = picked;
  PICKER.value = "C:\\fakepath\\one.png";
  ctx.onImagePickerChange({ currentTarget: PICKER });
  await settle();
  check("every picked image is uploaded, in the order picked",
        sent.length === 3 && sent[0].body === picked[0]
        && sent[1].body === picked[2] && sent[2].body === picked[3], sent);
  check("...one at a time", maxInFlight === 1, maxInFlight);
  check("...each typed as the image it is",
        sent.map((r) => r.contentType).join() === "image/png,image/webp,image/jpeg",
        sent.map((r) => r.contentType));
  check("...the file that is not an image is left out and said so",
        NOTE.textContent.includes("3 of 3") && NOTE.textContent.includes("not an image"),
        NOTE.textContent);
  check("...and the picker is cleared so the same file can be picked again",
        PICKER.value === "", PICKER.value);

  /* ---- one failure in a batch does not stop the rest, and is named ---- */
  sent = [];
  reply = { ok: true, doc: (n) => n === 2
    ? { ok: true, path: "p", delivered: false, reason: "powershell.exe: clipboard busy" }
    : OK_DOC };
  await ctx.sendImageFiles([picked[0], picked[2], picked[3]], NOTE);
  check("a batch goes on past a failed image", sent.length === 3, sent);
  check("...and reports the count and the first failure by name",
        NOTE.textContent.includes("2 of 3")
        && NOTE.textContent.includes("two.webp: powershell.exe: clipboard busy"),
        NOTE.textContent);
  check("...as a warning", NOTE.classes.has("wf-warning"));

  /* ---- nothing but non-images ---- */
  sent = [];
  await ctx.sendImageFiles([picked[1]], NOTE);
  check("no image among the files uploads nothing and says so",
        sent.length === 0 && NOTE.textContent.includes("none of those files is an image"),
        NOTE.textContent);

  /* ---- the relay's login page answering the upload ---- */
  sent = [];
  reply = { html: true };
  const viaLogin = await ctx.sendImageFiles([picked[0]], NOTE);
  check("a relay login page is not taken for a stored image",
        viaLogin === 0 && NOTE.textContent.includes("login"), NOTE.textContent);

  console.log(failures ? `\n${failures} failure(s)` : "all send-input checks passed");
  process.exit(failures ? 1 : 0);
}

main();
