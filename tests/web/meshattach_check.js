/* Daemon attach on the web (docs/mesh-design.md "Daemon attach").

   Four pieces of the page carry it, and each has one way to be wrong that
   the daemon's own tests cannot see:

     - remoteMeshChoices: the session form's remote rows. An offered mesh is
       an attach (pre-approved) and must carry the attach prefix; one that
       needs approval is a session join request by address; a mesh already
       attached is local and must not be offered twice.
     - renderRemoteMeshes: the sidebar list. Only an available row gets an
       Attach button, and it posts to the address's attach route.
     - renderPublishPanel: the owner's visibility picker and offer rows.
       The picker shows the mesh's current value, and an offer is withdrawn
       with a DELETE on that machine.
     - renderOutgoingJoins: a pending attach has no handle to show.

   Slice the real functions out of app.js and drive them against a stub DOM. */
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
    placeholder: "", disabled: false, dataset: {}, handlers: {},
    appendChild(c) { this.kids.push(c); return c; },
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
    get classList() {
      const self = this;
      return {
        add: (...cs) => cs.forEach((c) => self.classes.add(c)),
        remove: (...cs) => cs.forEach((c) => self.classes.delete(c)),
        toggle: (c, on) => {
          const want = on === undefined ? !self.classes.has(c) : !!on;
          if (want) self.classes.add(c); else self.classes.delete(c);
          return want;
        },
        contains: (c) => self.classes.has(c),
      };
    },
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
function textOf(n) {
  return [n.text, ...n.kids.map(textOf)].filter(Boolean).join(" ");
}
function all(n, pred, out = []) {
  if (pred(n)) out.push(n);
  n.kids.forEach((k) => all(k, pred, out));
  return out;
}
const tick = () => new Promise((r) => setTimeout(r, 0));
const settle = async () => { for (let i = 0; i < 5; i++) await tick(); };

/* ---- stub daemon ------------------------------------------------------- */
let calls = [];
let reply = { ok: true, status: 201, doc: {} };
function api(url, opts) {
  calls.push({ url, opts });
  return Promise.resolve({
    ok: reply.ok, status: reply.status, json: () => Promise.resolve(reply.doc),
  });
}

const DISCOVERED = {
  meshes: [
    { mesh: "dev", machine: "pcA", state: "available", access: "offer",
      members: 3, sources: ["offer"] },
    { mesh: "ops", machine: "pcA", state: "available", access: "approval",
      members: 1, sources: ["public"] },
    { mesh: "old", machine: "pcB", state: "attached", access: "approval",
      members: 2, sources: ["public"], key: "old@pcB" },
    { mesh: "web", machine: "pcC", state: "attached", access: "approval",
      members: 0, sources: ["public"], key: "web@pcC" },
    { mesh: "q", machine: "pcC", state: "pending", access: "approval",
      members: 0, sources: ["public"] },
  ],
  errors: { pcD: "peer backend is unreachable" },
};

/* ---- remoteMeshChoices -------------------------------------------------- */
{
  const choices = new Function(
    "remoteMeshes", "ATTACH_PREFIX",
    `${slice("remoteMeshChoices")}; return remoteMeshChoices;`
  )(DISCOVERED, "attach:")();
  assert.deepStrictEqual(choices.map((c) => c.value),
    ["attach:dev@pcA", "ops@pcA", "q@pcC"]);
  // attached rows are local meshes already: not offered again
  assert.ok(/offered/.test(choices[0].label), choices[0].label);
  assert.ok(/approval/.test(choices[1].label), choices[1].label);
  assert.ok(/admit this session/.test(choices[1].note), choices[1].note);
  // no discovery answer yet: nothing to offer, and no throw
  const none = new Function(
    "remoteMeshes", "ATTACH_PREFIX",
    `${slice("remoteMeshChoices")}; return remoteMeshChoices;`
  )(null, "attach:")();
  assert.deepStrictEqual(none, []);
}

/* ---- renderRemoteMeshes ------------------------------------------------- */
(async () => {
  const nodes = { "mesh-remote-list": node("ul"), "mesh-remote-note": node("p") };
  const $ = (id) => nodes[id];
  let meshListRefreshed = 0;
  let reloaded = 0;
  const location = { hash: "" };
  const render = new Function(
    "$", "el", "document", "api", "remoteMeshes", "refreshMeshList",
    "loadRemoteMeshes", "location", "currentProject",
    `${slice("renderRemoteMeshes")}; return renderRemoteMeshes;`
  )($, el, document, api, DISCOVERED, () => { meshListRefreshed++; },
    () => { reloaded++; }, location, "gds6");
  render();
  const list = nodes["mesh-remote-list"];
  assert.strictEqual(list.kids.length, 5);
  const buttons = all(list, (n) => n.tag === "button");
  assert.deepStrictEqual(buttons.map((b) => b.text), ["Attach", "Attach"],
    "only the two available rows are attachable");
  assert.ok(/offered/.test(textOf(list.kids[0])), textOf(list.kids[0]));
  assert.ok(/needs approval/.test(textOf(list.kids[1])), textOf(list.kids[1]));
  // an attached mesh opens under its address, beside any local namesake
  list.kids[3].handlers.click();
  assert.strictEqual(location.hash, "#/mesh/web%40pcC");
  // a relay peer that failed is reported, not hidden
  const note = nodes["mesh-remote-note"];
  assert.ok(/pcD: peer backend is unreachable/.test(note.text), note.text);
  assert.ok(note.classes.has("error"));

  calls = [];
  reply = { ok: true, status: 201, doc: { attached: true, mesh: "dev@pcA" } };
  await buttons[0].handlers.click();
  await settle();
  assert.strictEqual(calls[0].url, "/api/mesh/dev%40pcA/attach");
  assert.strictEqual(calls[0].opts.method, "POST");
  // the mirror is filed under the project the rail shows, so it is listed
  assert.deepStrictEqual(JSON.parse(calls[0].opts.body), { project: "gds6" });
  assert.strictEqual(meshListRefreshed, 1);
  assert.strictEqual(reloaded, 1);
  assert.strictEqual(location.hash, "#/mesh/dev%40pcA");

  // a refusal is shown and the button comes back
  reply = { ok: false, status: 400, doc: { error: "bad offer" } };
  await buttons[1].handlers.click();
  await settle();
  assert.ok(/bad offer/.test(note.text), note.text);
  assert.strictEqual(buttons[1].disabled, false);

  /* ---- renderPublishPanel ---------------------------------------------- */
  const panels = {};
  let viewRefreshed = 0;
  const publish = new Function(
    "el", "document", "api", "refreshMeshView", "confirm", "meshPublishPanels",
    `${slice("renderPublishPanel")}; return renderPublishPanel;`
  )(el, document, api, () => { viewRefreshed++; }, () => true, panels);
  const info = {
    name: "dev", visibility: "invited", offers: ["pcB"],
    peers: [{ machine: "pcA", self: true }, { machine: "pcC" }],
  };
  const fed = node("div");
  publish(info, fed);
  const selects = all(fed, (n) => n.tag === "select");
  assert.strictEqual(selects[0].value, "invited",
    "the picker shows the mesh's own visibility");
  assert.deepStrictEqual(selects[0].kids.map((o) => o.value),
    ["private", "public", "invited"]);
  assert.ok(/pcB/.test(textOf(fed)) && /offered/.test(textOf(fed)), textOf(fed));
  const offerBtn = all(fed, (n) => n.tag === "button" && n.text === "Offer")[0];
  assert.strictEqual(offerBtn.disabled, true, "no daemon picked yet");

  // withdrawing an offer is a DELETE on that machine
  calls = [];
  reply = { ok: true, status: 200, doc: {} };
  const kick = all(fed, (n) => n.classes.has("mesh-kick"))[0];
  await kick.handlers.click();
  await settle();
  assert.strictEqual(calls[0].url, "/api/mesh/dev/offers/pcB");
  assert.strictEqual(calls[0].opts.method, "DELETE");

  // the daemon list excludes linked peers and daemons already offered
  panels.dev.peers = ["pcA", "pcB", "pcC", "pcD"];
  const fed2 = node("div");
  publish(info, fed2);
  const pick = all(fed2, (n) => n.tag === "select")[1];
  assert.deepStrictEqual(pick.kids.map((o) => o.value), ["", "pcD"]);

  // changing visibility PUTs the new value
  calls = [];
  const vis = all(fed2, (n) => n.tag === "select")[0];
  vis.value = "public";
  await vis.handlers.change();
  await settle();
  assert.strictEqual(calls[0].url, "/api/mesh/dev/visibility");
  assert.strictEqual(calls[0].opts.method, "PUT");
  assert.deepStrictEqual(JSON.parse(calls[0].opts.body), { visibility: "public" });

  // offering posts the picked machine
  pick.value = "pcD";
  pick.handlers.change();
  const fed3 = node("div");
  publish(info, fed3);
  const go = all(fed3, (n) => n.tag === "button" && n.text === "Offer")[0];
  assert.strictEqual(go.disabled, false);
  calls = [];
  await go.handlers.click();
  await settle();
  assert.strictEqual(calls[0].url, "/api/mesh/dev/offers");
  assert.deepStrictEqual(JSON.parse(calls[0].opts.body), { machine: "pcD" });
  assert.ok(viewRefreshed > 0);

  /* ---- renderOutgoingJoins --------------------------------------------- */
  const box = node("ul");
  const outgoing = new Function(
    "$", "el", "document", "api", "refreshMeshList",
    `${slice("renderOutgoingJoins")}; return renderOutgoingJoins;`
  )(() => box, el, document, api, () => {});
  outgoing([
    { request_id: "req-1", mesh: "ops", primary: "pcA", handle: "", attach: true },
    { request_id: "req-2", mesh: "m", primary: "pcA", handle: "bob" },
  ]);
  assert.ok(/attach awaiting approval/.test(textOf(box.kids[0])), textOf(box.kids[0]));
  assert.ok(/as 'bob'/.test(textOf(box.kids[1])), textOf(box.kids[1]));

  /* ---- renamePeerButton ------------------------------------------------ */
  {
    const loc = { hash: "" };
    let answer = "pcA2";
    let listed = 0;
    const make = new Function(
      "el", "api", "prompt", "alert", "refreshMeshList", "refreshMeshView",
      "location",
      `${slice("renamePeerButton")}; return renamePeerButton;`
    )(el, api, () => answer, (m) => { throw new Error("alert: " + m); },
      async () => { listed++; }, () => {}, loc);
    const btn = make({ name: "m@pcA" }, "pcA");
    calls = [];
    reply = { ok: true, status: 200, doc: {
      old: "pcA", new: "pcA2", meshes: ["m@pcA"],
      rekeyed: [{ from: "m@pcA", to: "m@pcA2" }],
    } };
    await btn.handlers.click();
    await settle();
    assert.strictEqual(calls[0].url, "/api/relay/peers/pcA/rename");
    assert.deepStrictEqual(JSON.parse(calls[0].opts.body), { new: "pcA2" });
    assert.strictEqual(listed, 1);
    // the mesh on screen moved: follow it to its new key
    assert.strictEqual(loc.hash, "#/mesh/m%40pcA2");
    // an unchanged answer sends nothing
    calls = [];
    answer = "pcA";
    await btn.handlers.click();
    assert.strictEqual(calls.length, 0);
  }

  /* ---- meshProjectPicker ---------------------------------------------- */
  {
    function Option(label, value) {
      const o = node("option"); o.text = label; o.value = value; return o;
    }
    const selectNode = () => {
      const n = node("select");
      Object.defineProperty(n, "options", { get() { return this.kids; } });
      return n;
    };
    const doc = { createElement: (tag) => (tag === "select" ? selectNode() : node(tag)) };
    let listed = 0;
    let redrawn = 0;
    const make = (projects) => new Function(
      "document", "Option", "api", "alert", "projectsCache",
      "refreshMeshList", "refreshMeshView",
      `${slice("meshProjectPicker")}; return meshProjectPicker;`
    )(doc, Option, api, (m) => { throw new Error("alert: " + m); }, projects,
      async () => { listed++; }, () => { redrawn++; });
    const picker = make([{ name: "default" }, { name: "gds6" }]);
    // a mirror attached before it had a project sits in the default one
    const info = { name: "gds6@shared", project: "default" };
    const sel = picker(info);
    assert.deepStrictEqual(sel.kids.map((o) => o.value), ["default", "gds6"]);
    assert.strictEqual(sel.value, "default");
    calls = [];
    reply = { ok: true, status: 200, doc: { mesh: "gds6@shared", project: "gds6" } };
    sel.value = "gds6";
    await sel.handlers.change();
    assert.strictEqual(calls[0].url, "/api/mesh/gds6%40shared/project");
    assert.strictEqual(calls[0].opts.method, "PUT");
    assert.deepStrictEqual(JSON.parse(calls[0].opts.body), { project: "gds6" });
    assert.strictEqual(info.project, "gds6");
    assert.strictEqual(listed, 1);
    assert.strictEqual(redrawn, 1);
    // a daemon without projects draws no picker
    assert.strictEqual(make([])(info), null);
  }

  console.log("meshattach_check: ok");
})().catch((err) => { console.error(err); process.exit(1); });
