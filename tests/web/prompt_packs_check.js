/* Prompt packs in the browser, run against the real functions from app.js.

   Two places: the create form's Prompt packs row (installed packs as
   checkboxes, the defaults ticked, a pack the role or a ticked subrole
   requires locked on with the role named, and one that is required but not
   installed said out loud), and the Settings ▸ Install card (one cell per
   workspace × pack whose buttons follow the copy's status, force only after
   a confirm for an edited or local copy). */
const fs = require("fs");
const path = require("path");
const assert = require("node:assert/strict");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(name) {
  let start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  if (src.slice(start - 6, start) === "async ") start -= 6;
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

function node(tag, cls, text) {
  return {
    tag, className: cls || "", textContent: text === undefined ? "" : String(text),
    children: [], handlers: {}, checked: false, disabled: false, title: "",
    classList: {
      hidden: false,
      toggle(name, on) { if (name === "hidden") this.hidden = !!on; },
    },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(ev, fn) { (this.handlers[ev] = this.handlers[ev] || []).push(fn); },
    set innerHTML(v) { this.children = []; },
  };
}
const el = (tag, cls, text) => node(tag, cls, text);
function all(n, pred, out = []) {
  if (pred(n)) out.push(n);
  for (const c of n.children || []) all(c, pred, out);
  return out;
}
const text = (n) => all(n, () => true).map((x) => x.textContent).join(" ");

let failures = 0;
function check(name, fn) {
  const fail = (err) => { failures++; console.error(`FAIL ${name}`, err); };
  try {
    const out = fn();
    if (out && out.then) out.catch(fail);
  } catch (err) { fail(err); }
}

/* ---- the create form's row ---------------------------------------------- */
const row = node("div", "new-check-row");
const form = { role: { value: "" }, mesh: { value: "team" } };
const env = new Function(
  "$", "el", "document", "rolesByName", "spawnParent",
  [`let newSubroleTicks = new Set();`,
   `let newPackOffer = null, newPackOfferFor = null, newPackTicks = new Map();`,
   slice("newSubrolesMeshless"), slice("newSubroles"),
   slice("newRequiredPacks"), slice("newPackTicked"),
   slice("renderPromptPackRow"), slice("newPromptPacks"),
   `return {
      set offer(v) { newPackOffer = v; },
      ticks: () => newPackTicks, subTicks: () => newSubroleTicks,
      render: renderPromptPackRow, packs: newPromptPacks,
      required: newRequiredPacks,
    };`].join("\n")
)(
  (id) => (id === "new-session" ? form : id === "new-packs-row" ? row : null),
  el,
  { createElement: (tag) => node(tag), createTextNode: (t) => node("#text", "", t) },
  { worker: {}, unreal: {}, leader: {} },
  () => null,
);

const OFFER = {
  workspace: { name: "proj", path: "F:/proj" },
  packs: [
    { id: "beads", title: "Beads board" },
    { id: "common", title: "Common" },
  ],
  default_packs: ["common"],
  role_packs: { unreal: ["unreal-operator"], leader: ["beads"] },
};
const boxes = () => all(row, (n) => n.tag === "input");

check("no offer, no row", () => {
  env.render();
  assert.equal(row.classList.hidden, true);
  assert.deepEqual(env.packs(form), []);
});

check("installed packs are boxes, the default ticked", () => {
  env.offer = OFFER;
  env.render();
  assert.equal(row.classList.hidden, false);
  const b = boxes();
  assert.deepEqual(b.map((x) => x.name), ["prompt_pack_beads", "prompt_pack_common"]);
  assert.deepEqual(b.map((x) => x.checked), [false, true]);
  assert.deepEqual(env.packs(form), ["common"]);
});

check("an operator's untick survives a redraw", () => {
  const common = boxes()[1];
  common.checked = false;
  common.handlers.change[0]();
  env.render();
  assert.equal(boxes()[1].checked, false);
  assert.deepEqual(env.packs(form), []);
});

check("the role locks its pack on and names itself", () => {
  form.role.value = "leader";
  env.render();
  const beads = boxes()[0];
  assert.equal(beads.checked, true);
  assert.equal(beads.disabled, true);
  assert.match(text(row), /required by leader/);
  assert.deepEqual(env.packs(form), ["beads"]);
  form.role.value = "";
});

check("a ticked subrole requiring a missing pack is said out loud", () => {
  env.subTicks().add("unreal");
  env.render();
  assert.deepEqual(env.required(form), { "unreal-operator": ["unreal"] });
  assert.match(text(row), /unreal-operator is required by unreal but is not installed in proj/);
  // Not sent as a tick: the daemon adds required packs itself, and names
  // the missing one in its refusal.
  assert.deepEqual(env.packs(form), []);
});

check("subroles do not count without a mesh", () => {
  form.mesh.value = "";
  assert.deepEqual(env.required(form), {});
  env.render();
  assert.doesNotMatch(text(row), /not installed/);
  form.mesh.value = "team";
  env.subTicks().clear();
});

/* ---- the Settings card -------------------------------------------------- */
const calls = [];
let confirmAnswer = true;
const card = new Function(
  "el", "document", "api", "confirm", "renderWorkspaces", "Option",
  [`let promptPacksDoc = null, promptPacksError = "", promptPacksBusy = "", promptPacksDraft = null;`,
   `let wsOpen = false, newPackOfferFor = "x";`,
   `const PROMPT_PACK_STATUS = ${JSON.stringify({
     missing: "not installed", current: "installed", stale: "update available",
     edited: "edited here", local: "local pack" })};`,
   slice("promptPackCall"), slice("promptPackAction"),
   slice("promptPacksCard"), slice("promptPackSettings"),
   `return {
      set doc(v) { promptPacksDoc = v; },
      draft: () => promptPacksDraft,
      card: promptPacksCard, act: promptPackAction,
    };`].join("\n")
)(
  el,
  { createElement: (tag) => node(tag), createTextNode: (t) => node("#text", "", t) },
  async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return { ok: true, json: async () => ({}) };
  },
  () => confirmAnswer,
  () => {},
  function Option(label, value) { return node("option", "", label); },
);

const DOC = {
  catalog: [
    { id: "beads", title: "Beads board", description: "board rules" },
    { id: "common", title: "Common", description: "shared", default: true },
  ],
  workspaces: [
    { name: "proj", path: "F:/proj", packs: [
      { id: "beads", status: "missing" }, { id: "common", status: "edited" },
      { id: "house", status: "local" },
    ] },
    { name: "other", path: "F:/other", packs: [
      { id: "beads", status: "stale" }, { id: "common", status: "current" },
    ] },
  ],
  role_packs: { unreal: ["unreal-operator"] },
  default_packs: ["common"],
  roles: ["leader", "unreal", "worker"],
};

check("buttons follow the status of each copy", () => {
  card.doc = DOC;
  const c = card.card();
  const table = all(c, (n) => n.className === "md-table prompt-pack-table")[0];
  const rows = table.children.slice(1);
  const labels = (tr) => tr.children.slice(1).map((td) =>
    all(td, (n) => n.tag === "button").map((b) => b.textContent).join("+"));
  // Columns: shipped beads, common, then the local pack "house".
  assert.deepEqual(labels(rows[0]), ["Install", "Overwrite+Remove", "Remove"]);
  assert.deepEqual(labels(rows[1]), ["Update+Remove", "Remove", ""]);
});

check("force only after a confirm, and a declined confirm sends nothing", async () => {
  const ws = DOC.workspaces[0];
  confirmAnswer = false;
  card.act(ws, { id: "common", status: "edited" }, "install");
  assert.equal(calls.length, 0);
  confirmAnswer = true;
  card.act(ws, { id: "common", status: "edited" }, "install");
  card.act(ws, { id: "beads", status: "missing" }, "install");
  await new Promise((r) => setTimeout(r, 0));
  assert.deepEqual(calls.map((c) => [c.url, c.body.pack, c.body.force]), [
    ["/api/prompt-packs/install", "common", true],
    ["/api/prompt-packs/install", "beads", false],
  ]);
});

check("the settings draft starts from the saved mapping", () => {
  card.doc = DOC;
  card.card();
  assert.deepEqual(card.draft(), {
    role_packs: { unreal: ["unreal-operator"] }, default_packs: ["common"],
  });
});

setTimeout(() => {
  if (failures) process.exit(1);
  console.log("prompt_packs_check: ok");
}, 10);
