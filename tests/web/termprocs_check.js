/* The session's process line and card chips (claunch-ihwel): what the
   daemon's restart command left running for a session, from
   GET /api/sessions/<name>/procs and the `procs` field of the session list.
   What it must get right — the line and its header button exist only while
   there is a process (and a terminal is on screen), the button folds the
   line without hiding the button, a read for the previous session is
   ignored, each line's `kill` and each card chip's `X` ask first and then
   POST the kill route for that pid, the card's X does not open the session,
   and a card with no process loses its chips. Slice the real functions out
   of app.js and drive them against a stub DOM. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");

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
    tag, kids: [], text: "", classes: new Set(), handlers: {}, attrs: {},
    dataset: {}, title: "", type: "", removed: false,
    get textContent() { return n.text; },
    set textContent(v) { n.text = String(v); },
    appendChild(c) { this.kids.push(c); return c; },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    fire(k, ev) { return Promise.all((this.handlers[k] || []).map((fn) => fn(ev))); },
    setAttribute(k, v) { n.attrs[k] = String(v); },
    querySelector(sel) { return walk(n).find((k) => !k.removed && k.classes.has(sel.slice(1))) || null; },
    remove() { n.removed = true; },
    set innerHTML(v) { if (v === "") this.kids = []; },
    classList: {
      add: (...cs) => cs.forEach((c) => n.classes.add(c)),
      remove: (...cs) => cs.forEach((c) => n.classes.delete(c)),
      contains: (c) => n.classes.has(c),
      toggle: (c, on) => { if (on) n.classes.add(c); else n.classes.delete(c); },
    },
  };
  return n;
}
function walk(n, out = []) {
  for (const k of n.kids) { out.push(k); walk(k, out); }
  return out;
}
const texts = (n) => walk(n).map((k) => k.text).join(" | ");
const byClass = (n, cls) => walk(n).filter((k) => k.classes.has(cls));

const box = node("div");
box.classes.add("hidden");
const button = node("button");
button.classes.add("hidden");
const $ = (id) => {
  if (id === "term-procs") return box;
  if (id === "term-procsbtn") return button;
  throw new Error("unexpected $: " + id);
};
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}

const stubs = `
let currentPage = "terminal";
let currentName = "s16";
let refits = 0;
function refitSoon() { refits++; }
let termProcsOpen = true;
let termProcsView = null;
const localStorage = { setItem() {} };
const calls = [];
let confirmAnswer = true;
async function modalConfirm(title, body, label) { calls.push(["confirm", title, label]); return confirmAnswer; }
async function modalInfo(title, body) { calls.push(["info", title, body]); }
async function api(url, opts) {
  calls.push(["api", url, (opts || {}).method || "GET"]);
  return { ok: true, status: 200, json: async () => ({ session: "s16", killed: [41], procs: [] }) };
}
async function refreshSessions() { calls.push(["refresh"]); }
function railNotePlace(li, node) { li.appendChild(node); }
`;

const ctx = {};
new Function(
  "exports", "$", "el",
  stubs
  + slice("fmtAge") + slice("fmtSpan")
  + slice("procShort") + slice("procTitle") + slice("killSessionProc")
  + slice("setTermProcsOpen") + slice("renderTermProcs") + slice("decorateProcsRow") + `
Object.assign(exports, {
  renderTermProcs, decorateProcsRow, setTermProcsOpen, procShort,
  refits: () => refits,
  calls,
  setPage: (p) => { currentPage = p; },
  setName: (n) => { currentName = n; },
  setConfirm: (v) => { confirmAnswer = v; },
});`
)(ctx, $, el);

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) return;
  failures++;
  console.log(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
};

const NOW = Date.now();
Date.now = () => NOW;
const T = NOW / 1000;
const DEV = { pid: 41, cmd: "/usr/bin/npm run dev --workspace server", label: "restart:reflect",
              since: T - 125, children: 2 };
const VIEW = (procs, over) => ({ session: "s16", procs, ...over });
const settle = () => new Promise((r) => setTimeout(r, 0));

(async () => {
  /* ---- nothing running: no line, no button ---- */
  ctx.renderTermProcs(VIEW([]));
  check("no process, no line", box.classes.has("hidden"));
  check("no process, no button", button.classes.has("hidden"));
  ctx.renderTermProcs(null);
  check("no payload (old daemon) leaves both hidden",
        box.classes.has("hidden") && button.classes.has("hidden"));

  /* ---- the dev server ---- */
  const before = ctx.refits();
  ctx.renderTermProcs(VIEW([DEV]));
  check("a process shows the line", !box.classes.has("hidden"));
  check("and the header button, with the count", !button.classes.has("hidden")
        && button.text.startsWith("procs 1"), button.text);
  check("the button says the line is open", button.attrs["aria-pressed"] === "true");
  check("appearing refits the terminal", ctx.refits() === before + 1);
  let t = texts(box);
  check("one line names pid, starter and command",
        t.includes("41") && t.includes("restart:reflect") && t.includes("npm run dev"), t);
  check("what runs under it is counted", t.includes("+2"), t);
  const line = byClass(box, "tp-line")[0];
  check("the hover says how long it has run", line.title.includes("running for 2m"), line.title);
  ctx.renderTermProcs(VIEW([DEV]));
  check("the same rows do not refit again", ctx.refits() === before + 1);

  /* ---- fold and unfold ---- */
  ctx.setTermProcsOpen(false);
  check("folded: the line goes", box.classes.has("hidden"));
  check("folded: the button stays to bring it back", !button.classes.has("hidden")
        && button.attrs["aria-pressed"] === "false", button.attrs);
  ctx.setTermProcsOpen(true);
  check("unfolded: the line is back", !box.classes.has("hidden") && texts(box).includes("npm"));

  /* ---- a read for the session we left ---- */
  ctx.renderTermProcs(VIEW([DEV], { session: "s99" }));
  check("another session's read is ignored", box.classes.has("hidden") && button.classes.has("hidden"));
  ctx.renderTermProcs(VIEW([DEV]));
  ctx.setPage("transcript");
  ctx.renderTermProcs(VIEW([DEV]));
  check("no line off the terminal page", box.classes.has("hidden"));
  ctx.setPage("terminal");
  ctx.renderTermProcs(VIEW([DEV]));

  /* ---- kill from the line ---- */
  ctx.calls.length = 0;
  ctx.setConfirm(false);
  await byClass(box, "tp-kill")[0].fire("click");
  check("a refused confirmation sends nothing",
        !ctx.calls.some((c) => c[0] === "api"), ctx.calls);
  ctx.setConfirm(true);
  ctx.calls.length = 0;
  await byClass(box, "tp-kill")[0].fire("click");
  await settle();
  check("kill asks, then posts the pid's route",
        ctx.calls[0][0] === "confirm" && ctx.calls.some((c) => c[0] === "api"
          && c[1] === "/api/sessions/s16/procs/41/kill" && c[2] === "POST"), ctx.calls);
  check("the answer redraws the line (nothing left)", box.classes.has("hidden"));
  check("and the rail is refreshed", ctx.calls.some((c) => c[0] === "refresh"));

  /* ---- the session card ---- */
  const li = node("li");
  ctx.decorateProcsRow(li, { name: "s16", procs: [DEV] });
  const chips = byClass(li, "rail-proc");
  check("one chip per process", chips.length === 1);
  check("the chip is the short command", texts(li).includes("npm run dev --workspace server")
        && !texts(li).includes("/usr/bin/"), texts(li));
  const x = byClass(li, "rail-proc-x")[0];
  check("the chip's button is an X", x && x.text === "X");
  ctx.calls.length = 0;
  let stopped = false;
  await x.fire("click", { stopPropagation() { stopped = true; } });
  await settle();
  check("X does not open the session", stopped);
  check("X posts the same kill route", ctx.calls.some((c) => c[0] === "api"
        && c[1] === "/api/sessions/s16/procs/41/kill"), ctx.calls);
  ctx.decorateProcsRow(li, { name: "s16", procs: [DEV, { ...DEV, pid: 42, cmd: "node x.js", children: 0 }] });
  check("a redecorate replaces the chips in place",
        byClass(li, "rail-proc").filter((c) => !c.removed).length === 2
        && byClass(li, "rail-procs").length === 1);
  ctx.decorateProcsRow(li, { name: "s16" });
  check("no process: the chips go", li.querySelector(".rail-procs") === null);

  check("a long command is cut", ctx.procShort({ cmd: "x".repeat(80) }, 10).length === 10);

  if (failures) {
    console.log(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("termprocs_check: ok");
})();
