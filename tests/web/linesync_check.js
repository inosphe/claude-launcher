/* The session line's remote-sync mode (daemon/line_sync.py, the "session
   line sync" block of app.js).

   The contract: local-only sends nothing on an edit; a `line_sync` frame
   that turns the session to remote moves this page's local text to the
   read-only keep box and puts the shared text in the field; every edit then
   goes out as one socket frame (or over HTTP with no socket); frames from
   others are applied with the caret kept in place, an older echo of this
   page's own write is skipped, nothing is applied mid-composition, a stale
   answer is dropped unless forced; a send clears the shared line only on
   the condition that it still holds what was sent; switching back to local
   brings the kept text back. Slice the real functions out of app.js, drive
   them against a stub DOM and a stub socket, and check all of it. */
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

function node(tag, id) {
  const n = {
    tag, id, classes: new Set(), _text: "", title: "", placeholder: "",
    value: "", disabled: false, attrs: {},
    selectionStart: 0, selectionEnd: 0, style: {}, scrollHeight: 0,
    setSelectionRange(a, b) { n.selectionStart = a; n.selectionEnd = b; },
    setAttribute(k, v) { n.attrs[k] = String(v); },
    focus() {},
    classList: {
      add(c) { n.classes.add(c); },
      remove(c) { n.classes.delete(c); },
      toggle(c, on) {
        if (on === undefined) on = !n.classes.has(c);
        if (on) n.classes.add(c); else n.classes.delete(c);
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

const FIELD = node("textarea", "term-input-field");
const KEEP = node("textarea", "term-local-keep");
KEEP.classes.add("hidden");
const TOGGLE = node("button", "term-sync-toggle");
const BTN = node("button", "term-input-send");
const NOTE = node("span", "term-input-note");
const els = {
  "term-input-field": FIELD, "term-local-keep": KEEP,
  "term-sync-toggle": TOGGLE, "term-input-send": BTN, "term-input-note": NOTE,
};
const $ = (id) => els[id] || null;
const document = { activeElement: FIELD };

let calls = [];
let replies = [];
const api = async (p, opts = {}) => {
  const body = opts.body ? JSON.parse(opts.body) : null;
  calls.push({ path: p, method: opts.method || "GET", body });
  await new Promise((r) => setImmediate(r));
  const reply = replies.shift() || { ok: true, doc: {} };
  const doc = typeof reply.doc === "function" ? reply.doc(body) : reply.doc;
  return { ok: reply.ok !== false, status: reply.status || 200, json: async () => doc };
};
const socket = { readyState: 1, frames: [], send(s) { this.frames.push(JSON.parse(s)); } };

const clientDecl = src.match(/const LINE_SYNC_CLIENT = [^;]*;/);
if (!clientDecl) throw new Error("missing LINE_SYNC_CLIENT");
const lineDecl = src.match(/let lineSync = \{[^}]*\};/);
if (!lineDecl) throw new Error("missing lineSync");

const exportsObj = {};
new Function(
  "exports", "api", "$", "document", "socket",
  `let currentName = null;
let ws = socket;
const WebSocket = { OPEN: 1 };
let sessionsCache = [];
let sessJournalBox = null;
function currentScoreFeedback() { return "none"; }
function setScoreFeedbackChoice() {}
function renderScoreGoal() {}
${clientDecl[0]}
${lineDecl[0]}
let lineSyncComposing = false;
let sessionEnded = false;
let snapshotName = null;
let attachedPid = null;
` + [
    "autogrowTermInput", "termInputNote", "sendKeyLine", "lineSyncShared",
    "resetLineSync", "loadLineSync", "setLineText", "applyLineSync",
    "termInputQueueNote", "termInputEnded", "renderTermInputField",
    "renderLineSync", "pushLineSync", "clearSentLine", "toggleLineSync",
  ].map(slice).join("\n") + `
exports.LINE_SYNC_CLIENT = LINE_SYNC_CLIENT;
exports.set = (name) => { currentName = name; };
exports.setWs = (s) => { ws = s; };
exports.compose = (on) => { lineSyncComposing = on; };
exports.state = () => lineSync;
Object.assign(exports, { sendKeyLine, lineSyncShared, resetLineSync,
  applyLineSync, pushLineSync, clearSentLine, toggleLineSync });
`
)(exportsObj, api, $, document, socket);
const L = exportsObj;
const ME = L.LINE_SYNC_CLIENT;
const flush = () => new Promise((r) => setTimeout(r, 10));
let failures = 0;
function check(cond, what) {
  if (!cond) { failures++; console.error("FAIL:", what); }
}
const frame = (o) => ({ type: "line_sync", mode: "remote", text: "", rev: 1, at: "", client: "other", ...o });

(async () => {
  // ---- attach: local until told otherwise, and the state is read ----
  replies.push({ doc: frame({ mode: "local", rev: 0 }) });
  L.set("s1");
  L.resetLineSync("s1");
  await flush();
  check(calls.length === 1 && calls[0].path === "/api/sessions/s1/line-sync" &&
        calls[0].method === "GET", "attach reads the line-sync state");
  check(!L.lineSyncShared(), "an untouched session is local");
  check(TOGGLE.textContent === "Sync off" && TOGGLE.attrs["aria-pressed"] === "false",
        "the toggle says local-only");

  // ---- local-only: an edit costs nothing on the wire ----
  FIELD.value = "my local draft";
  L.pushLineSync();
  check(socket.frames.length === 0, "local-only sends no frame on an edit");

  // ---- the session is switched to remote ----
  L.applyLineSync(frame({ text: "shared start", rev: 2 }));
  check(L.lineSyncShared(), "a remote frame makes the line shared");
  check(KEEP.value === "my local draft", "the local text moves to the keep box");
  check(!KEEP.classes.has("hidden"), "the keep box is shown while it holds text");
  check(FIELD.value === "shared start", "the field holds the shared text");
  check(FIELD.classes.has("line-shared"), "the field is marked shared");
  check(TOGGLE.textContent === "Sync on" && TOGGLE.attrs["aria-pressed"] === "true",
        "the toggle says remote-sync");

  // ---- an edit goes out as one socket frame ----
  FIELD.value = "shared start!";
  L.pushLineSync();
  check(socket.frames.length === 1, "one frame per edit");
  check(socket.frames[0].type === "line_sync" && socket.frames[0].text === "shared start!" &&
        socket.frames[0].client === ME, "the frame carries the text and this page's id");

  // ---- this page's own older echo is skipped while a newer write is out ----
  FIELD.value = "shared start!!";
  L.pushLineSync();
  L.applyLineSync(frame({ text: "shared start!", rev: 3, client: ME }));
  check(FIELD.value === "shared start!!", "an older own echo does not take keys back");
  L.applyLineSync(frame({ text: "shared start!!", rev: 4, client: ME }));
  check(FIELD.value === "shared start!!", "the latest own echo changes nothing");

  // ---- another page's edit is applied, caret kept relative to the text ----
  FIELD.value = "hello world";
  FIELD.selectionStart = FIELD.selectionEnd = 11;
  L.applyLineSync(frame({ text: ">> hello world", rev: 5 }));
  check(FIELD.value === ">> hello world", "another page's edit is applied");
  check(FIELD.selectionStart === 14, "the caret moves with an insertion before it");
  FIELD.selectionStart = FIELD.selectionEnd = 2;
  L.applyLineSync(frame({ text: ">> hello world, again", rev: 6 }));
  check(FIELD.selectionStart === 2, "an edit after the caret leaves it alone");

  // ---- an older answer is dropped, a forced one is taken ----
  L.applyLineSync(frame({ text: "stale", rev: 3 }));
  check(FIELD.value === ">> hello world, again", "a frame older than the last one is dropped");

  // ---- nothing lands mid-composition ----
  L.compose(true);
  L.applyLineSync(frame({ text: "while composing", rev: 7 }));
  check(FIELD.value === ">> hello world, again", "a composing IME is not interrupted");
  L.compose(false);

  // ---- send: the shared line is cleared on condition ----
  socket.frames = [];
  calls = [];
  FIELD.value = "run the tests";
  replies.push({ doc: {} });
  const sentOk = await L.sendKeyLine(FIELD, BTN, NOTE);
  check(sentOk && calls[0].path === "/api/sessions/s1/keys", "the line is sent as before");
  check(FIELD.value === "", "the sent line leaves the field");
  const clear = socket.frames[socket.frames.length - 1];
  check(clear && clear.text === "" && clear.if_text === "run the tests",
        "the clear is conditional on the line that was sent");

  // A line another page started while the send was in flight survives.
  socket.frames = [];
  FIELD.value = "someone else's next line";
  L.clearSentLine(FIELD, "the old line");
  check(FIELD.value === "someone else's next line", "a newer line is not wiped locally");
  check(socket.frames[0].if_text === "the old line", "and the daemon is asked on the same condition");

  // ---- no socket: the HTTP door ----
  L.setWs(null);
  calls = [];
  FIELD.value = "over http";
  replies.push({ doc: frame({ text: "over http", rev: 20, client: ME }) });
  L.pushLineSync();
  await flush();
  check(calls.length === 1 && calls[0].method === "POST" &&
        calls[0].path === "/api/sessions/s1/line-sync" &&
        calls[0].body.text === "over http" && calls[0].body.client === ME,
        "without a socket the edit goes over HTTP");
  check(L.state().rev === 20, "the HTTP answer is applied");
  L.setWs(socket);

  // ---- the toggle asks the daemon to switch the session ----
  calls = [];
  replies.push({ doc: frame({ mode: "local", text: "over http", rev: 21 }) });
  await L.toggleLineSync();
  check(calls[0].method === "POST" && calls[0].body.mode === "local",
        "the toggle on a shared line asks for local");
  check(!L.lineSyncShared(), "the answer switches this page back");
  check(FIELD.value === "my local draft", "the kept local text comes back into the field");
  check(KEEP.value === "" && KEEP.classes.has("hidden"), "the keep box empties and hides");
  check(!FIELD.classes.has("line-shared"), "the field is no longer marked shared");

  calls = [];
  replies.push({ doc: frame({ text: "over http", rev: 22 }) });
  await L.toggleLineSync();
  check(calls[0].body.mode === "remote", "the toggle on a local line asks for remote");
  check(FIELD.value === "over http", "the shared line kept by the daemon comes back");

  // A refused switch is said under the box.
  replies.push({ ok: false, status: 404, doc: { error: "no such session" } });
  await L.toggleLineSync();
  check(NOTE.textContent === "no such session", "a refused switch is shown");

  // ---- another session: nothing carries over ----
  replies.push({ doc: frame({ mode: "local", rev: 0 }) });
  L.set("s2");
  L.resetLineSync("s2");
  await flush();
  check(!L.lineSyncShared() && KEEP.value === "", "a different session starts local and empty");

  if (failures) {
    console.error(`${failures} line-sync check(s) failed`);
    process.exit(1);
  }
  console.log("all line-sync checks passed");
})().catch((err) => { console.error(err); process.exit(1); });
