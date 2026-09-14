/* Joining a mesh from the session detail panel.

   The panel's Meshes section used to be a list and nothing else: it said
   which rooms a session was in, and putting it into another one meant
   leaving for the mesh page and finding the session again in a dropdown of
   every live session on the machine. `sessMeshJoin` is the write that
   section was missing, and what is worth holding here is not the POST — the
   mesh page has always sent it — but the three ways it can be wrong when the
   session is the thing already decided:

     - the rooms offered are the ones it is NOT in. A mesh it already belongs
       to in the list and in the picker at once is an offer the daemon
       refuses, and the reader cannot tell which of the two lists is stale;
     - the role comes from the SELECTED mesh, because a mesh may carry its
       own vocabulary. Until that fetch answers, the row must not post a word
       the mesh may not know — a fallback shown while waiting is a
       placeholder, not an answer;
     - a remote mesh answers 202: the request was pended for its owner to
       approve and no membership exists yet. Reported as "joined", the chips
       above stay empty and the reader reads that as a bug.

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
    tag, kids: [], text: "", classes: new Set(), title: "", value: "",
    placeholder: "", disabled: false, selected: false, dataset: {},
    handlers: {},
    // A real <select> reports its first option until one is picked, and the
    // row reads that value to decide which mesh it is asking about.
    appendChild(c) {
      this.kids.push(c);
      if (this.tag === "select" && !this.value && c.value) this.value = c.value;
      return c;
    },
    append(...cs) { cs.forEach((c) => this.kids.push(c)); },
    addEventListener(ev, fn) { this.handlers[ev] = fn; },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) {
      this.classes = new Set(String(v).split(/\s+/).filter(Boolean));
    },
    get innerHTML() { return ""; },
    set innerHTML(v) { if (!v) this.kids = []; },
  };
  return n;
}
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}
const document = { createElement: (tag) => node(tag) };
/* Everything the box put on the page, flattened. */
function textOf(n) {
  return [n.text, ...n.kids.map(textOf)].filter(Boolean).join(" ");
}
function find(n, cls) {
  if (n.classes.has(cls)) return n;
  for (const k of n.kids) {
    const hit = find(k, cls);
    if (hit) return hit;
  }
  return null;
}
/* Two hops of microtasks plus a macrotask: the role fetch awaits the
   response and then its json(). */
const tick = () => new Promise((r) => setTimeout(r, 0)).then(() => {});
const settle = async () => { for (let i = 0; i < 5; i++) await tick(); };

/* ---- stub daemon ------------------------------------------------------- */
let calls = [];        // every api() the row made, in order
let rolesReply = {     // what GET /api/mesh/<m>/roles answers
  ok: true, status: 200,
  doc: { default: "worker", roles: [{ name: "leader" }, { name: "worker" },
                                    { name: "reviewer" }] },
};
let joinReply = { ok: true, status: 201, doc: { handle: "s9" } };
let refreshed = { mesh: 0, session: 0 };

function api(url, opts) {
  calls.push({ url, opts });
  const reply = /\/roles$/.test(url) ? rolesReply : joinReply;
  if (reply.throws) return Promise.reject(new Error("offline"));
  return Promise.resolve({
    ok: reply.ok,
    status: reply.status,
    json: () => Promise.resolve(reply.doc),
  });
}

function load(meshes) {
  calls = [];
  refreshed = { mesh: 0, session: 0 };
  const fn = new Function(
    "el", "document", "api", "meshCache", "refreshMeshList", "refreshSession",
    `let sessJoinBox = null;
     ${slice("sessMeshJoin")}
     return sessMeshJoin;`
  );
  return fn(
    el, document, api, meshes,
    () => { refreshed.mesh++; }, () => { refreshed.session++; }
  );
}

const MESHES = [{ name: "mesh-a" }, { name: "mesh-b" }, { name: "mesh-c" }];
const live = (extra) => Object.assign(
  { session: { name: "s9", status: "running" }, meshes: [] }, extra || {}
);

/* ---- an exited session is not enrolled --------------------------------- */
{
  const box = load(MESHES)(live({ session: { name: "s9", status: "exited" } }));
  const t = textOf(box);
  assert.ok(/exited/.test(t), t);
  assert.strictEqual(find(box, "sess-mesh-pick"), null,
    "an exited session must not be offered a join row");
}

/* ---- nothing left to join --------------------------------------------- */
{
  const all = MESHES.map((m) => ({ mesh: m.name, handle: "s9", role: "worker" }));
  const box = load(MESHES)(live({ meshes: all }));
  assert.strictEqual(find(box, "sess-mesh-pick"), null);
  assert.ok(/already in every one/.test(textOf(box)), textOf(box));

  // And the empty machine says something different: there is no room to join
  // at all, which is fixed somewhere else entirely.
  const none = load([])(live());
  assert.ok(/no mesh exists/.test(textOf(none)), textOf(none));
}

/* ---- the rooms offered are the ones it is not in ----------------------- */
{
  const box = load(MESHES)(live({
    meshes: [{ mesh: "mesh-b", handle: "reviewer3", role: "reviewer" }],
  }));
  const pick = find(box, "sess-mesh-pick");
  assert.ok(pick, "a session with a room left to join gets the picker");
  assert.deepStrictEqual(pick.kids.map((o) => o.value), ["mesh-a", "mesh-c"]);

  // The handle defaults to the session name, and the row says so rather than
  // leaving the reader to guess what an empty box sends.
  const handle = find(box, "sess-mesh-handle");
  assert.ok(handle.placeholder.includes("s9"), handle.placeholder);
}

/* ---- the role list belongs to the selected mesh ------------------------ */
(async () => {
  const join = load(MESHES);
  const box = join(live());
  const pick = find(box, "sess-mesh-pick");
  const role = find(box, "sess-mesh-role-pick");
  assert.strictEqual(pick.value, "mesh-a");
  await settle();
  assert.deepStrictEqual(role.kids.map((o) => o.value),
    ["leader", "worker", "reviewer"]);
  assert.ok(role.kids.find((o) => o.value === "worker").selected,
    "the mesh's own default role is the one preselected");
  assert.ok(calls.some((c) => c.url === "/api/mesh/mesh-a/roles"),
    JSON.stringify(calls.map((c) => c.url)));

  /* ---- the join itself ------------------------------------------------- */
  role.value = "reviewer";
  find(box, "sess-mesh-handle").value = "  helper  ";
  calls = [];
  await find(box, "wf-btn").handlers.click();
  const post = calls.find((c) => c.opts && c.opts.method === "POST");
  assert.ok(post, "the button posts an enrolment");
  assert.strictEqual(post.url, "/api/mesh/mesh-a/members");
  const body = JSON.parse(post.opts.body);
  assert.strictEqual(body.session, "s9");
  assert.strictEqual(body.handle, "helper");   // trimmed
  assert.strictEqual(body.role, "reviewer");
  // Both lists the new membership belongs to are re-read: the panel's chips
  // and the sidebar's mesh list.
  assert.strictEqual(refreshed.session, 1);
  assert.strictEqual(refreshed.mesh, 1);
})().then(async () => {

  /* ---- a role the mesh never named is not posted ----------------------- */
  rolesReply = { ok: false, status: 404, doc: {} };
  const join = load(MESHES);
  const box = join(live());
  await settle();
  const role = find(box, "sess-mesh-role-pick");
  assert.ok(role.kids.length, "the row still offers something to join as");
  calls = [];
  await find(box, "wf-btn").handlers.click();
  const body = JSON.parse(calls.find((c) => c.opts.method === "POST").opts.body);
  assert.strictEqual(body.role, "",
    "an unanswered role fetch posts no role — the daemon picks the default");
  rolesReply = {
    ok: true, status: 200,
    doc: { default: "worker", roles: [{ name: "worker" }] },
  };

  /* ---- a pended remote join is not reported as a membership ------------ */
  joinReply = { ok: true, status: 202, doc: { pending: true } };
  const remote = load(MESHES)(live());
  await settle();
  await find(remote, "wf-btn").handlers.click();
  const pended = textOf(remote);
  assert.ok(/asked mesh-a to admit/.test(pended), pended);
  assert.ok(/approve/.test(pended), pended);
  assert.ok(!/joined mesh-a/.test(pended), pended);

  /* ---- a refusal is shown, and the typed handle survives it ------------ */
  joinReply = { ok: false, status: 409, doc: { error: "handle 'helper' is taken" } };
  const bad = load(MESHES)(live());
  await settle();
  const handle = find(bad, "sess-mesh-handle");
  handle.value = "helper";
  const btn = find(bad, "wf-btn");
  await btn.handlers.click();
  assert.ok(/handle 'helper' is taken/.test(textOf(bad)), textOf(bad));
  assert.strictEqual(handle.value, "helper",
    "a refused join must not throw away what was typed");
  assert.strictEqual(btn.disabled, false, "the button is usable again");
  assert.strictEqual(refreshed.session, 0, "nothing changed, nothing re-read");

  /* ---- the daemon being unreachable says so, and claims nothing -------- */
  joinReply = { throws: true };
  const off = load(MESHES)(live());
  await settle();
  await find(off, "wf-btn").handlers.click();
  assert.ok(/nothing was joined/.test(textOf(off)), textOf(off));

  console.log("meshjoin_check ok");
}).catch((err) => {
  console.error(err && err.stack || err);
  process.exit(1);
});
