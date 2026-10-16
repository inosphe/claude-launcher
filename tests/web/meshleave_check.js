/* The Details panel's Mesh sub-tab: leave, pending join requests, join
   (claunch-8x20z).

   `sessMeshesSection` draws every membership of one session with a Leave on
   its own row, the join requests that session still has waiting on another
   machine's owner with a Withdraw, and the join form under them. What is
   worth holding here:

     - Leave sends the DELETE for THAT row's mesh and handle, and only after
       the reader confirms — a dismissed confirm sends nothing;
     - a refusal from the daemon is shown as a warning, and the line saying
       what happened survives the panel's rebuild;
     - the pending list is this session's join requests only: another
       session's request and a daemon attach are not on it;
     - Withdraw sends the outgoing DELETE for that request id;
     - the join form is the existing sessMeshJoin, still drawn.

   Slice the real function out of app.js and drive it against a stub DOM. */
const assert = require("assert");
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
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), title: "", href: "",
    type: "", disabled: false, handlers: {},
    appendChild(c) { this.kids.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.kids.push(c)); },
    addEventListener(ev, fn) { this.handlers[ev] = fn; },
  };
  return n;
}
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}
function all(n, cls, out = []) {
  if (n.classes.has(cls)) out.push(n);
  for (const k of n.kids) all(k, cls, out);
  return out;
}
function textOf(n) {
  return [n.text, ...n.kids.map(textOf)].filter(Boolean).join(" ");
}
const settle = async () => {
  for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0));
};

/* ---- stub daemon ------------------------------------------------------- */
let calls = [];
let reply = { ok: true, status: 200, doc: { ok: true } };
let answer = true;           // what confirm() says
let asked = [];
let refreshed = { mesh: 0, session: 0 };

function api(url, opts) {
  calls.push({ url, method: (opts || {}).method || "GET" });
  if (reply.throws) return Promise.reject(new Error("offline"));
  return Promise.resolve({
    ok: reply.ok, status: reply.status, json: () => Promise.resolve(reply.doc),
  });
}

const ctx = {};
new Function(
  "exports", "el", "api", "confirm", "refreshMeshList", "refreshSession",
  "outgoing",
  `let meshOutgoing = outgoing;
   function memberRoleLabel(m) { return m.role; }
   function memberAutoRoles() { return []; }
   function localStamp(x) { return String(x || ""); }
   function sessRoleEditor() { return el("div", "sess-role-edit"); }
   function sessMeshJoin() { return el("div", "sess-mesh-join"); }
   function go() {}
   let sessMeshNote = null;
   ${slice("sessMeshesSection")}
   exports.draw = sessMeshesSection;
   exports.setOutgoing = (o) => { meshOutgoing = o; };`
)(ctx, el, api, (msg) => { asked.push(msg); return answer; },
  () => refreshed.mesh++, () => refreshed.session++, []);

const data = {
  session: { name: "s9", status: "running" },
  meshes: [
    { mesh: "alpha", handle: "s9", role: "worker", members: 3 },
    { mesh: "beta", handle: "b-9", role: "reviewer", members: 2 },
  ],
};

(async () => {
  /* ---- the memberships ------------------------------------------------- */
  let box = ctx.draw(data);
  const leaves = all(box, "sess-mesh-leave");
  assert.strictEqual(leaves.length, 2, "one Leave per membership");
  assert.ok(/Meshes \(2\)/.test(textOf(box)), "the heading counts them");
  assert.strictEqual(all(box, "sess-mesh-join").length, 1, "the join form is drawn");
  assert.strictEqual(all(box, "sess-role-edit").length, 2, "one role editor each");

  // A dismissed confirm sends nothing.
  answer = false;
  await leaves[1].handlers.click();
  assert.deepStrictEqual(calls, [], "no DELETE without the reader's yes");
  assert.ok(/beta/.test(asked[0]) && /b-9/.test(asked[0]),
            "the confirm names the room and the handle");

  // Confirmed: the DELETE for that row's mesh and handle.
  answer = true;
  await leaves[1].handlers.click();
  await settle();
  assert.deepStrictEqual(calls, [
    { url: "/api/mesh/beta/members/b-9", method: "DELETE" },
  ]);
  assert.ok(refreshed.mesh >= 1 && refreshed.session >= 1,
            "the list and the panel are refreshed");
  box = ctx.draw(data);
  assert.ok(/left beta/.test(textOf(box)), "the outcome survives the rebuild");

  // A refusal is a warning, in the daemon's words.
  calls = [];
  reply = { ok: false, status: 409, doc: { error: "no such member 's9'" } };
  await all(ctx.draw(data), "sess-mesh-leave")[0].handlers.click();
  await settle();
  assert.strictEqual(calls[0].url, "/api/mesh/alpha/members/s9");
  box = ctx.draw(data);
  const warn = all(box, "wf-warning");
  assert.strictEqual(warn.length, 1, "the refusal is drawn as a warning");
  assert.ok(/no such member/.test(warn[0].text));

  // Another session's panel does not carry this one's outcome line.
  const other = ctx.draw({ session: { name: "s10" }, meshes: [] });
  assert.strictEqual(all(other, "wf-warning").length, 0);
  assert.ok(/not a member of any mesh/.test(textOf(other)));

  /* ---- pending join requests ------------------------------------------- */
  ctx.setOutgoing([
    { request_id: "r1", mesh: "gamma", primary: "box2", session: "s9",
      handle: "s9", role: "worker" },
    { request_id: "r2", mesh: "delta", primary: "box2", session: "s10",
      handle: "s10" },
    { request_id: "r3", mesh: "eps", primary: "box3", attach: true },
  ]);
  box = ctx.draw(data);
  const pend = all(box, "sess-mesh-req");
  assert.strictEqual(pend.length, 1, "only this session's own join request");
  assert.ok(/gamma@box2/.test(pend[0].text));
  assert.ok(/awaiting approval \(1\)/.test(textOf(box)));

  calls = [];
  reply = { ok: true, status: 200, doc: { cancelled: true } };
  await all(box, "sess-mesh-withdraw")[0].handlers.click();
  await settle();
  assert.deepStrictEqual(calls, [{ url: "/api/mesh/outgoing/r1", method: "DELETE" }]);
  assert.ok(/withdrew the request to join gamma@box2/.test(textOf(ctx.draw(data))));

  ctx.setOutgoing([]);
  assert.strictEqual(all(ctx.draw(data), "sess-mesh-pending").length, 0,
                     "no pending block when nothing waits");

  console.log("meshleave_check ok");
})().catch((e) => { console.error(e); process.exit(1); });
