/* The one-line send-keys input under the terminal. A native <input> where
   xterm's composer is a terminal — the field the reader types a prompt into,
   Enter handing the line to the session through the same send-keys
   passthrough `claunch send-keys` uses.

   The box has to hold the contract the whole raw-keystroke path lives under:
   the text and its Enter go in ONE /keys call (a client that splits them
   re-spreads the submit/enter split across call sites — the split belongs to
   Session.send_keys alone), an empty or unaddressed line sends nothing, a
   refusal's words are shown and the half-typed line kept, a dead daemon does
   not look like a delivery, and a session that has ended has the box closed
   with the reason shown. Slice the real functions out of app.js, drive them
   against a stub DOM, and check all of it. */
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
const api = async (p, opts) => {
  sent.push({ path: p, method: opts.method, contentType: opts.headers["Content-Type"], body: JSON.parse(opts.body) });
  if (reply.throw) throw new Error("offline");
  return { ok: reply.ok, status: reply.status || 200, json: async () => reply.doc };
};

const ctx = {};
new Function(
  "exports", "api",
  `let currentName = null;
let sessionEnded = false;
` + slice("termInputNote") + `
` + slice("sendKeyLine") + `
` + slice("termInputBlock") + `
Object.assign(exports, {
  sendKeyLine,
  termInputBlock,
  termInputNote,
  setSession: (name, ended) => { currentName = name; sessionEnded = !!ended; },
});`
)(ctx, api);

const FIELD = node("input");
const BTN = node("button");
const NOTE = node("span");
const box = () => ({ field: FIELD, btn: BTN, note: NOTE });

/* let every pending await in the code under test run to the end */
const settle = () => new Promise((r) => setImmediate(r));

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

  /* ---- an ended session has the box closed, not a live one ---- */
  const live = ctx.termInputBlock(false);
  const dead = ctx.termInputBlock(true);
  check("a live session is open", live === "");
  check("an ended session has a reason", dead !== "", dead);
  sent = [];
  ctx.setSession("coder4", true);
  b.field.value = "anything";
  const blocked = await ctx.sendKeyLine(b.field, b.btn, b.note);
  check("an ended session blocks the send", sent.length === 0 && blocked === false, sent);
  check("...and shows the reason", NOTE.textContent === dead, NOTE.textContent);

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

  console.log(failures ? `\n${failures} failure(s)` : "all send-input checks passed");
  process.exit(failures ? 1 : 0);
}

main();
