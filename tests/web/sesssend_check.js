/* The session panel can write to the session it names. That box has to hold
   four lines at once — it must address the RIGHT handle (a session is called
   something different in every mesh it joined), it must speak as the operator
   rather than impersonate a member, it must not lose a half-typed message to
   the 2s poll, and it must not swallow a refusal. Slice the real function out
   of app.js, drive it against a stub DOM, and check all four. */
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
    tag, attrs: {}, kids: [], text: "", classes: new Set(), handlers: {},
    dataset: {}, value: undefined, disabled: false, placeholder: "", title: "",
    appendChild(c) {
      this.kids.push(c);
      // a browser's <select> takes the first option's value as its own
      if (this.tag === "select" && c.tag === "option" && this.value === undefined) {
        this.value = c.value;
      }
      return c;
    },
    append(...cs) { cs.forEach((c) => this.appendChild(c)); },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    fire(k, ev) { return Promise.all((this.handlers[k] || []).map((fn) => fn(ev))); },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) {
      this.classes = new Set(String(v).split(/\s+/).filter(Boolean));
    },
  };
  return n;
}
function walk(n, out = []) {
  for (const k of n.kids) { out.push(k); walk(k, out); }
  return out;
}
const has = (n, cls) => walk(n).some((k) => k.classes.has(cls));
const texts = (n) => walk(n).map((k) => k.text).join(" | ");
const tags = (n, tag) => walk(n).filter((k) => k.tag === tag);

const document = { createElement: (tag) => node(tag) };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}

/* the daemon: every call recorded, every answer scripted by the test */
let sent = [];
let reply = { ok: true, doc: { id: "m-1" } };
const api = async (p, opts) => {
  sent.push({ path: p, body: JSON.parse(opts.body), method: opts.method });
  if (reply.throw) throw new Error("offline");
  return { ok: reply.ok, status: reply.status || 200, json: async () => reply.doc };
};

const stubs = `
let sessSendBox = null;
let refreshed = 0;
function refreshSession() { refreshed++; }
`;

const ctx = {};
new Function(
  "exports", "document", "el", "api",
  stubs + slice("sessSend") + `
Object.assign(exports, {
  sessSend,
  box: () => sessSendBox,
  drop: () => { sessSendBox = null; },
  refreshed: () => refreshed,
});`
)(ctx, document, el, api);

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) return;
  failures++;
  console.log(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
};

/* The same session, under two names, because that is the whole point of
   picking the mesh: 'coder4' is 'builder' in one room and 'reviewer' in the
   other, and a message addressed to the session name reaches nobody. */
const DATA = {
  session: { name: "coder4", status: "idle" },
  meshes: [
    { mesh: "ship", handle: "builder", role: "coder", members: 3 },
    { mesh: "review", handle: "reviewer", role: "reviewer", members: 2 },
  ],
};
const parts = (box) => {
  const sel = tags(box, "select");
  return {
    mesh: sel[0], intent: sel[1],
    text: tags(box, "textarea")[0],
    btn: walk(box).find((k) => k.tag === "button"),
    status: walk(box).find((k) => k.classes.has("wf-note") && k.tag === "p"),
  };
};

/* let every pending await in the code under test run to the end */
const settle = () => new Promise((r) => setImmediate(r));

async function main() {
  /* ---- a session in no mesh has nothing to send through ---- */
  ctx.drop();
  const none = ctx.sessSend({ session: { name: "solo" }, meshes: [] });
  check("no mesh, no form", !tags(none, "textarea").length);
  check("...and it says why rather than sitting there dead",
        texts(none).includes("in none"), texts(none));
  check("it holds no box open across that", ctx.box() === null);

  /* ---- the controls ---- */
  ctx.drop();
  const box = ctx.sessSend(DATA);
  const p = parts(box);
  check("the box is titled like its neighbours", texts(box).includes("Send message"));
  check("every membership is offerable",
        tags(p.mesh, "option").map((o) => o.value).join(",") === "ship,review",
        tags(p.mesh, "option").map((o) => o.value));
  check("the intents are the mesh's own vocabulary",
        tags(p.intent, "option").map((o) => o.value).join(",") === "say,ask,fyi,ack",
        tags(p.intent, "option").map((o) => o.value));
  check("the placeholder names who is about to be written to",
        p.text.placeholder.includes("builder"), p.text.placeholder);

  /* ---- send: as the operator, to the handle of the mesh in hand ---- */
  p.text.value = "  rebase onto master  ";
  await p.btn.fire("click");
  check("one message, posted to the chosen mesh", sent.length === 1 &&
        sent[0].path === "/api/mesh/ship/messages" && sent[0].method === "POST",
        sent);
  check("addressed to the handle, not the session name",
        sent[0].body.to === "builder", sent[0].body);
  check("spoken as the operator, who is nobody's member",
        sent[0].body.from === "operator" && sent[0].body.external === true,
        sent[0].body);
  check("the body is trimmed", sent[0].body.body === "rebase onto master",
        sent[0].body.body);
  check("and carries the intent", sent[0].body.type === "say");
  check("the box is emptied for the next one", p.text.value === "");
  check("it reports where the message went",
        p.status.text.includes("sent to builder"), p.status.text);
  check("...quietly, as a note", p.status.classes.has("wf-note") &&
        !p.status.classes.has("wf-warning"), [...p.status.classes]);
  check("and the panel is re-read: an 'ask' is a debt from now on",
        ctx.refreshed() === 1);

  /* ---- the other room, the other name ---- */
  sent = [];
  p.mesh.value = "review";
  await p.mesh.fire("change");
  check("switching mesh re-aims the placeholder",
        p.text.placeholder.includes("reviewer"), p.text.placeholder);
  p.intent.value = "ask";
  p.text.value = "ready for you";
  await p.btn.fire("click");
  check("the second mesh's handle is the one addressed",
        sent[0].path === "/api/mesh/review/messages" && sent[0].body.to === "reviewer",
        sent);
  check("...with the intent that expects an answer", sent[0].body.type === "ask");

  /* ---- an empty message is not a message ---- */
  sent = [];
  p.text.value = "   ";
  await p.btn.fire("click");
  check("whitespace sends nothing", sent.length === 0, sent);

  /* ---- Ctrl+Enter is the same send; a bare Enter is a newline ---- */
  sent = [];
  p.text.value = "now";
  let prevented = 0;
  await p.text.fire("keydown", { key: "Enter", preventDefault: () => prevented++ });
  check("Enter alone types a newline", sent.length === 0, sent);
  await p.text.fire("keydown", {
    key: "Enter", ctrlKey: true, preventDefault: () => prevented++,
  });
  check("Ctrl+Enter sends", sent.length === 1, sent);
  check("...and does not also break the line", prevented === 1, prevented);
  // The key handler fires the send without returning it, so the send is still
  // in flight here — let it land before the next check writes over its state.
  await settle();

  /* ---- a refusal is shown, and the words are kept ---- */
  sent = [];
  reply = { ok: false, status: 403, doc: { error: "no link from operator to reviewer" } };
  p.text.value = "let me in";
  await p.btn.fire("click");
  check("the daemon's own words are shown",
        p.status.text.includes("no link from operator"), p.status.text);
  check("...as a warning, not a note", p.status.classes.has("wf-warning"));
  check("and the message is not thrown away", p.text.value === "let me in");

  /* A mirror whose primary is unreachable took the message durably but has
     not delivered it — that is not a plain 'sent'. */
  reply = { ok: true, doc: { id: "m-9", queued: true } };
  await p.btn.fire("click");
  check("a queued send says queued", p.status.text.includes("queued m-9"),
        p.status.text);
  check("...and warns, because nobody has read it yet",
        p.status.classes.has("wf-warning"));

  /* An unreachable daemon must not look like a delivery. */
  reply = { throw: true };
  p.text.value = "hello?";
  await p.btn.fire("click");
  check("a dead daemon is reported as such",
        p.status.text.includes("nothing was sent"), p.status.text);
  check("the message survives that too", p.text.value === "hello?");
  check("and the button is usable again", p.btn.disabled === false);
  reply = { ok: true, doc: { id: "m-2" } };

  /* ---- the 2s poll must not wipe what is being typed ---- */
  p.text.value = "half a thou";
  const again = ctx.sessSend(DATA);
  check("a rebuild re-uses the very same node, typing and all",
        parts(again).text === p.text && parts(again).text.value === "half a thou");
  const busier = {
    session: DATA.session,
    meshes: [
      { ...DATA.meshes[0], members: 9 },   // someone else joined
      DATA.meshes[1],
    ],
  };
  check("someone else joining the mesh is no reason to lose it",
        parts(ctx.sessSend(busier)).text === p.text);

  /* ...but the box IS about one session in one set of rooms. */
  const left = { session: DATA.session, meshes: [DATA.meshes[1]] };
  check("leaving a mesh rebuilds it", parts(ctx.sessSend(left)).text !== p.text);
  ctx.drop();
  const other = ctx.sessSend({ session: { name: "coder5" }, meshes: DATA.meshes });
  sent = [];
  parts(other).text.value = "x";
  await parts(other).btn.fire("click");
  check("another session's box writes to another session's handle",
        sent[0].body.to === "builder" && parts(other).text !== p.text, sent);

  console.log(failures ? `\n${failures} failure(s)` : "all sess-send checks passed");
  process.exit(failures ? 1 : 0);
}

main();
