/* The Unanswered box, and the two things it lets an operator do about a row.

   The buttons are the whole point of the panel now, and which of them appears
   is a decision the daemon makes (`can_nudge` / `can_dismiss` on each row) —
   a member counted on another machine can be nudged through its own daemon
   but not dismissed here, and on a mirror neither is true. That mapping, and
   the request each button actually sends, are what this checks: getting the
   URL wrong would fail silently in a browser and loudly nowhere. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

const a = src.indexOf("function fmtAge(secs) {");
const b = src.indexOf("/* Nudge-policy editor");
if (a < 0 || b <= a) throw new Error("cannot locate the unanswered section");
const code = src.slice(a, b);

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, attrs: {}, kids: [], text: "", classes: new Set(), handlers: {},
    disabled: false, title: "",
    setAttribute(k, v) { this.attrs[k] = String(v); },
    appendChild(c) { this.kids.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.kids.push(c)); },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    click() { (this.handlers.click || []).forEach((fn) => fn()); },
  };
  n.classList = {
    add: (...cs) => cs.forEach((c) => n.classes.add(c)),
    contains: (c) => n.classes.has(c),
  };
  return n;
}
const document = { createElement: (tag) => node(tag) };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}
const location = { hash: "" };
const meshDotClass = () => "idle";
const confirm = () => true;
const alert = (m) => { alerted.push(m); };
let alerted = [];
let refreshed = 0;
const refreshMeshView = () => { refreshed += 1; };
const refreshMeshList = () => {};

/* Every request the panel makes, in order, plus what the daemon replied. */
let calls = [];
let nextResponse = { ok: true, body: {} };
const api = async (p, opts = {}) => {
  calls.push({ path: p, method: (opts || {}).method || "GET", body: (opts || {}).body });
  return { ok: nextResponse.ok, status: nextResponse.ok ? 200 : 400,
           json: async () => nextResponse.body };
};

const ctx = {};
new Function(
  "exports", "document", "el", "location", "meshDotClass", "confirm", "alert",
  "api", "refreshMeshView", "refreshMeshList",
  code + "\nObject.assign(exports, {renderMeshOwed, fmtAge});"
)(ctx, document, el, location, meshDotClass, confirm, alert, api,
  refreshMeshView, refreshMeshList);

/* ---- helpers ----------------------------------------------------------- */
function all(root, pred, out = []) {
  if (pred(root)) out.push(root);
  for (const k of root.kids) all(k, pred, out);
  return out;
}
const withClass = (root, c) => all(root, (n) => n.classes.has(c));
const texts = (root, c) => withClass(root, c).map((n) => n.text);

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}
const flush = () => new Promise((r) => setTimeout(r, 0));

const info = { name: "team", primary: null };
const localRow = {
  handle: "w-api", role: "worker", machine: "work-pc", session: "s1",
  local: true, reachability: "idle", source: "log", owed: 2, pending: 0,
  oldest_age: 900, stale: false, can_nudge: true, can_dismiss: true,
  messages: [
    { id: "msg-aaa", from: "lead", type: "ask", age: 900, body: "status?" },
    { id: "msg-bbb", from: "lead", type: "ask", age: 120, body: "and this?" },
  ],
};
const remoteRow = {
  handle: "scout", role: "worker", machine: "laptop", session: "s3",
  local: false, reachability: "remote-connected", source: "reported", owed: 1,
  pending: 0, oldest_age: null, stale: false, can_nudge: true,
  can_dismiss: false, messages: [],
};
const report = (rows, extra = {}) => ({
  mesh: "team", members: rows, owed: rows.reduce((n, r) => n + (r.owed || 0), 0),
  owing: rows.filter((r) => r.owed).length, pending: 0, engine: "work-pc",
  heartbeat: { enabled: true }, ...extra,
});

/* ---- a local member owing two answers ---------------------------------- */
(async () => {
  {
    calls = [];
    const box = ctx.renderMeshOwed(info, report([localRow]));
    const btns = withClass(box, "mesh-owed-btn");
    check("both row buttons", btns.length === 2, texts(box, "mesh-owed-btn"));
    check("one × per message", withClass(box, "mesh-owed-x").length === 2);

    withClass(box, "mesh-owed-x")[0].click();
    await flush();
    check("the × dismisses that one message", calls.length === 1 && (
      calls[0].path === "/api/mesh/team/members/w-api/owed/msg-aaa" &&
      calls[0].method === "DELETE"
    ), calls);
    check("and redraws from the daemon", refreshed > 0);

    calls = [];
    btns.find((n) => n.text === "dismiss all").click();
    await flush();
    check("dismiss all takes the whole row", calls.length === 1 && (
      calls[0].path === "/api/mesh/team/members/w-api/owed" &&
      calls[0].method === "DELETE"
    ), calls);

    calls = [];
    btns.find((n) => n.text === "nudge").click();
    await flush();
    check("nudge posts to the member", calls.length === 1 && (
      calls[0].path === "/api/mesh/team/members/w-api/nudge" &&
      calls[0].method === "POST"
    ), calls);
  }

  /* ---- a refusal is shown, not swallowed -------------------------------- */
  {
    calls = [];
    alerted = [];
    nextResponse = { ok: false, body: { error: "its terminal did not take it" } };
    const box = ctx.renderMeshOwed(info, report([localRow]));
    const nudge = withClass(box, "mesh-owed-btn").find((n) => n.text === "nudge");
    nudge.click();
    await flush();
    check("the daemon's reason reaches the operator",
          alerted.length === 1 && /did not take it/.test(alerted[0]), alerted);
    check("and the button comes back", nudge.disabled === false);
    nextResponse = { ok: true, body: {} };
  }

  /* ---- a member counted on another daemon -------------------------------- */
  {
    const box = ctx.renderMeshOwed(info, report([remoteRow]));
    check("nudge only, on a remote row",
          texts(box, "mesh-owed-btn").join() === "nudge",
          texts(box, "mesh-owed-btn"));
    check("no × without messages to dismiss",
          withClass(box, "mesh-owed-x").length === 0);
  }

  /* ---- a mirror, which can do neither ------------------------------------ */
  {
    const box = ctx.renderMeshOwed(
      { name: "team", primary: "work-pc" },
      report([{ ...remoteRow, can_nudge: false }])
    );
    check("no buttons a mirror cannot honour",
          withClass(box, "mesh-owed-btn").length === 0,
          texts(box, "mesh-owed-btn"));
  }

  /* ---- nothing owed, and no report at all -------------------------------- */
  {
    const quiet = ctx.renderMeshOwed(info, report([{ ...localRow, owed: 0, messages: [] }]));
    check("a settled mesh offers nothing to press",
          withClass(quiet, "mesh-owed-btn").length === 0);
    const none = ctx.renderMeshOwed(info, null);
    check("an older daemon's silence does not throw", none !== undefined);
  }

  console.log(failures ? `\n${failures} failure(s)` : "all owed checks passed");
  process.exit(failures ? 1 : 0);
})();
