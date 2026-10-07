/* Prompt packs in the browser, run against the real functions from app.js.

   Two places: the create form's Prompt packs row (installed packs and then
   claunch's own copies as chips in a General and a Roles group, the role
   packs not in play folded, each with a view that shows its text, any of
   them tickable with no role or mesh, the defaults ticked only where installed, a pack the role or a
   ticked subrole requires locked on with the role named, and one that is
   required but exists nowhere said out loud), and the Settings ▸ Install card (one cell per
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
// One check at a time, an async one finished before the next starts: the
// checks share the form's state, and a preview still reading must not see
// the next check's offer.
let queue = Promise.resolve();
function check(name, fn) {
  queue = queue.then(async () => {
    try { await fn(); } catch (err) { failures++; console.error(`FAIL ${name}`, err); }
  });
}

/* ---- the create form's row ---------------------------------------------- */
const row = node("div", "new-check-row");
const form = { role: { value: "" }, mesh: { value: "team" } };
const fetched = [];
const answers = {};   // url prefix -> [status, body]
const fakeApi = async (url) => {
  fetched.push(url);
  const key = Object.keys(answers).find((k) => url.startsWith(k));
  const [status, body] = key ? answers[key] : [404, { error: "nope" }];
  return { ok: status < 400, status, json: async () => body };
};
const env = new Function(
  "$", "el", "document", "rolesByName", "spawnParent", "api", "mdInto", "newSessionCwd",
  [`let newSubroleTicks = new Set();`,
   `let rolesDefault = "";`,
   `let newPackOffer = null, newPackOfferFor = null, newPackTicks = new Map();`,
   `let newDaemonPromptTick = null, newPackRolesOpen = false, newPackPreview = null;`,
   slice("newDaemonPromptWanted"),
   slice("newSubrolesMeshless"), slice("newSubroles"), slice("newPrimaryRole"),
   slice("newRequiredPacks"), slice("newPackTicked"),
   slice("renderPromptPackRow"), slice("newPromptPacks"),
   slice("togglePackPreview"), slice("packPreviewOrigin"), slice("packPreviewPanel"),
   `return {
      set offer(v) { newPackOffer = v; },
      set roleDefault(v) { rolesDefault = v; },
      ticks: () => newPackTicks, subTicks: () => newSubroleTicks,
      render: renderPromptPackRow, packs: newPromptPacks,
      daemonWanted: newDaemonPromptWanted,
      resetDaemonTick: () => { newDaemonPromptTick = null; },
      rolesOpen: () => newPackRolesOpen,
      closeRoles: () => { newPackRolesOpen = false; },
      preview: () => newPackPreview,
      origin: packPreviewOrigin,
      required: newRequiredPacks, primary: newPrimaryRole,
    };`].join("\n")
)(
  (id) => (id === "new-session" ? form : id === "new-packs-row" ? row : null),
  el,
  { createElement: (tag) => node(tag), createTextNode: (t) => node("#text", "", t) },
  { worker: { aliases: ["coder"] }, unreal: {}, leader: {}, "free-role": {} },
  () => null,
  fakeApi,
  (n, t) => { n.appendChild(node("md", "", t)); return n; },
  () => "F:/proj/sub",
);
// The summary is text nodes and chips; read it the way a browser's
// textContent would, every descendant in order.
const deep = (n) => n.textContent + (n.children || []).map(deep).join("");
const summary = () =>
  all(row, (n) => n.className === "wf-note new-packs-summary").map(deep)[0] || "";
const chips = () => all(row, (n) => /(^| )pack-chip( |$)/.test(n.className));
const groups = () => all(row, (n) => n.className === "pack-group")
  .map((g) => [g.children[0].textContent,
    all(g, (n) => n.tag === "input").map((b) => b.name)]);
const button = (label) => all(row, (n) => n.tag === "button" && n.textContent === label);
const flush = () => new Promise((r) => setTimeout(r, 0));

const OFFER = {
  workspace: { name: "proj", path: "F:/proj" },
  packs: [
    { id: "beads", title: "Beads board" },
    { id: "common", title: "Common" },
  ],
  default_packs: ["common"],
  role_packs: { unreal: ["unreal-operator"], leader: ["beads"] },
  shipped: [
    { id: "beads", title: "Beads board" },
    { id: "common", title: "Common" },
    { id: "unreal-operator", title: "Unreal — operator" },
    { id: "role-worker", title: "Role — worker" },
    { id: "role-leader", title: "Role — leader" },
  ],
};
const boxes = () => all(row, (n) => n.tag === "input");

check("no offer, no row", () => {
  env.render();
  assert.equal(row.classList.hidden, true);
  assert.deepEqual(env.packs(form), []);
});

const GENERAL = ["prompt_pack_beads", "prompt_pack_common", "prompt_pack_unreal-operator"];

check("installed packs, then claunch's copies, are chips; the default ticked", () => {
  env.offer = OFFER;
  env.render();
  assert.equal(row.classList.hidden, false);
  // Two groups; the role packs nobody asked for are folded behind a button.
  assert.deepEqual(groups(), [["General", GENERAL], ["Roles", []]]);
  assert.equal(button("+2 more").length, 1);
  const b = boxes();
  assert.deepEqual(b.map((x) => x.checked), [false, true, false]);
  assert.deepEqual(b.map((x) => x.disabled), [false, false, false]);
  // A lit chip for the ticked one, and each chip says where its text is from.
  assert.deepEqual(chips().map((c) => c.className.includes(" on")), [false, true, false]);
  assert.match(text(row), /Unreal — operator claunch view/);
  const src = all(row, (n) => n.className === "pack-chip-badge src-package")[0];
  assert.equal(src.title, "claunch's copy, not installed in proj");
  assert.match(text(row), /Common installed view/);
  assert.deepEqual(env.packs(form), ["common"]);
});

check("the folded role packs open and close, and a ticked one stays out", () => {
  button("+2 more")[0].handlers.click[0]();
  assert.equal(env.rolesOpen(), true);
  assert.deepEqual(groups()[1], ["Roles", ["prompt_pack_role-worker", "prompt_pack_role-leader"]]);
  assert.equal(button("fewer").length, 1);
  newTick("role-worker", true);
  button("fewer")[0].handlers.click[0]();
  // Folded again, but the ticked one is in play and stays shown.
  assert.deepEqual(groups()[1], ["Roles", ["prompt_pack_role-worker"]]);
  assert.equal(button("+1 more").length, 1);
  assert.deepEqual(env.packs(form), ["common", "role-worker"]);
  newTick("role-worker", false);
  assert.deepEqual(groups()[1], ["Roles", []]);
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
  assert.ok(chips()[0].className.includes("locked"));
  assert.match(text(row), /Beads board 🔒 leader installed/);
  const lock = all(row, (n) => n.className === "pack-chip-badge lock")[0];
  assert.equal(lock.title, "required by leader");
  assert.deepEqual(env.packs(form), ["beads"]);
  form.role.value = "";
});

check("a ticked subrole's uninstalled pack is shown as claunch's copy", () => {
  env.subTicks().add("unreal");
  env.render();
  assert.deepEqual(env.required(form), { "unreal-operator": ["unreal"] });
  const b = boxes();
  assert.deepEqual(b.map((x) => x.name), GENERAL);
  assert.equal(b[2].checked, true);
  assert.equal(b[2].disabled, true);
  assert.match(text(row), /Unreal — operator 🔒 unreal claunch/);
  assert.doesNotMatch(text(row), /refused/);
  assert.equal(summary(),
    "The opening will include: unreal-operator (role unreal, claunch's copy).");
  // Not sent as a tick: the daemon adds required packs itself.
  assert.deepEqual(env.packs(form), []);
});

check("the summary names every pack and why it is there, one chip each", () => {
  newTick("common", true);
  env.render();
  assert.equal(summary(),
    "The opening will include: common (ticked) · unreal-operator (role unreal, claunch's copy).");
  assert.deepEqual(all(row, (n) => n.className === "pack-chip-mini").map((n) => n.textContent),
    ["common (ticked)", "unreal-operator (role unreal, claunch's copy)"]);
  newTick("common", false);
});

check("a blank Role row is inferred from the handle, alias included", () => {
  env.subTicks().clear();
  env.offer = { ...OFFER, role_packs: { ...OFFER.role_packs, worker: ["role-worker"] } };
  form.handle = { value: "coder2" };
  assert.deepEqual(env.primary(form), { role: "worker", inferred: true });
  env.render();
  assert.equal(summary(),
    "The opening will include: role-worker (role worker, claunch's copy)." +
    " Role worker is inferred from the handle (no Role picked).");
  // The required role pack is in play, so it is shown though folded.
  assert.deepEqual(groups()[1], ["Roles", ["prompt_pack_role-worker"]]);
  // A handle naming no role falls to the vocabulary's default.
  form.handle.value = "zed";
  env.roleDefault = "free-role";
  assert.deepEqual(env.primary(form), { role: "free-role", inferred: true });
  // A picked role is not inferred, and no mesh means no role at all.
  form.role.value = "leader";
  assert.deepEqual(env.primary(form), { role: "leader", inferred: false });
  form.role.value = "";
  form.mesh.value = "";
  assert.deepEqual(env.primary(form), { role: "", inferred: false });
  form.mesh.value = "team";
  delete form.handle;
  env.roleDefault = "";
  env.offer = OFFER;
});

check("a required pack that exists nowhere refuses the create", () => {
  env.offer = { ...OFFER, role_packs: { leader: ["ghost"] } };
  form.role.value = "leader";
  env.render();
  assert.match(text(row),
    /ghost is required by leader but is neither installed in proj nor shipped with claunch/);
  form.role.value = "";
  env.offer = OFFER;
});

check("subroles do not count without a mesh", () => {
  form.mesh.value = "";
  assert.deepEqual(env.required(form), {});
  env.render();
  assert.doesNotMatch(text(row), /🔒/);
  assert.equal(boxes().every((b) => !b.disabled), true);
  form.mesh.value = "team";
  env.subTicks().clear();
});

check("with no mesh and nothing installed, any of claunch's packs can be ticked", () => {
  // The user's case: no mesh, so no role and nothing required, and a
  // workspace that installed no pack. The row still offers every pack.
  env.offer = { ...OFFER, workspace: null, packs: [] };
  form.mesh.value = "";
  form.role.value = "worker";
  env.render();
  assert.equal(row.classList.hidden, false);
  button("+2 more")[0].handlers.click[0]();
  assert.deepEqual(boxes().map((x) => x.name),
    [...GENERAL, "prompt_pack_role-worker", "prompt_pack_role-leader"]);
  // Nobody touched the row: the default does not tick an uninstalled copy.
  assert.deepEqual(boxes().map((x) => x.checked), [false, false, false, false, false]);
  assert.deepEqual(env.packs(form), []);
  assert.equal(summary(), "The opening will include no prompt pack.");
  newTick("role-worker", true);
  newTick("beads", true);
  assert.deepEqual(env.packs(form), ["beads", "role-worker"]);
  assert.equal(summary(),
    "The opening will include: beads (ticked, claunch's copy) · " +
    "role-worker (ticked, claunch's copy).");
  newTick("role-worker", false);
  newTick("beads", false);
  env.closeRoles();
  form.role.value = "";
  form.mesh.value = "team";
  env.offer = OFFER;
});

check("the daemon's prompt is the first chip, ticked, and can be unticked", () => {
  env.offer = { ...OFFER, daemon_prompt: { id: "daemon", title: "Daemon prompt (default)" } };
  env.render();
  const b = boxes();
  assert.deepEqual(b.map((x) => x.name), ["daemon_prompt", ...GENERAL]);
  assert.equal(groups()[0][1][0], "daemon_prompt");
  assert.equal(b[0].checked, true);
  assert.equal(b[0].disabled, false);
  assert.equal(env.daemonWanted(), true);
  assert.match(text(row), /Daemon prompt \(default\) daemon view/);
  const before = env.packs(form);
  assert.ok(summary().startsWith(
    "The opening will include: daemon prompt (this daemon's, every session)"));
  // It is not a pack id the form sends; its own switch carries the answer.
  assert.ok(!before.includes("daemon"));
  b[0].checked = false;
  b[0].handlers.change[0]();
  assert.equal(env.daemonWanted(), false);
  assert.equal(boxes()[0].checked, false);
  assert.ok(!summary().includes("daemon prompt"));
  assert.deepEqual(env.packs(form), before);
  // A daemon with no prompt offers no chip, and nothing is wanted.
  env.offer = { ...OFFER, packs: [], shipped: [], daemon_prompt: null };
  env.resetDaemonTick();
  env.render();
  assert.equal(env.daemonWanted(), false);
  assert.equal(row.classList.hidden, true);
  // The daemon's prompt alone is enough to show the row.
  env.offer = { ...OFFER, packs: [], shipped: [], role_packs: {},
    daemon_prompt: { id: "daemon", title: "Daemon prompt (default)" } };
  env.render();
  assert.equal(row.classList.hidden, false);
  assert.deepEqual(boxes().map((x) => x.name), ["daemon_prompt"]);
  env.resetDaemonTick();
  env.offer = OFFER;
});

check("view shows a pack's text below the chips, and a second click closes it", async () => {
  env.offer = OFFER;
  env.render();
  answers["/api/prompt-packs/text"] = [200, {
    id: "common", title: "Common", body: "# Rules\nbe kind", source: "workspace",
    status: "edited", lines: 2,
  }];
  const viewCommon = () => chips()[1].children[1];
  assert.equal(viewCommon().textContent, "view");
  viewCommon().handlers.click[0]();
  // Asked for this form's directory, and a placeholder while it reads.
  assert.equal(fetched.at(-1), "/api/prompt-packs/text?id=common&cwd=F%3A%2Fproj%2Fsub");
  assert.match(text(row), /Reading…/);
  await flush();
  assert.ok(chips()[1].className.includes("previewing"));
  const panel = all(row, (n) => n.className === "pack-preview")[0];
  assert.match(text(panel), /Common edited in proj · 2 lines/);
  assert.equal(all(panel, (n) => n.tag === "md")[0].textContent, "# Rules\nbe kind");
  // The panel sits between the chips and the summary.
  const order = row.children.map((n) => n.className);
  assert.ok(order.indexOf("pack-preview") < order.indexOf("wf-note new-packs-summary"));
  viewCommon().handlers.click[0]();
  assert.equal(env.preview(), null);
  assert.equal(all(row, (n) => n.className === "pack-preview").length, 0);
});

check("the daemon's prompt previews from its own route; a failure is said", async () => {
  env.offer = { ...OFFER, daemon_prompt: { id: "daemon", title: "Daemon prompt (default)" } };
  env.render();
  answers["/api/daemon-prompt"] = [200, { instance: "default", text: "cite files\nbe brief\nok" }];
  chips()[0].children[1].handlers.click[0]();
  assert.equal(fetched.at(-1), "/api/daemon-prompt");
  await flush();
  assert.match(text(row), /Daemon prompt \(default\) this daemon's prompt, every session · 3 lines/);
  all(row, (n) => n.tag === "button" && n.textContent === "close")[0].handlers.click[0]();
  assert.equal(env.preview(), null);
  // An id the daemon does not know: the panel says so instead of hanging.
  delete answers["/api/prompt-packs/text"];
  chips()[1].children[1].handlers.click[0]();
  await flush();
  assert.match(text(row), /nope/);
  chips()[1].children[1].handlers.click[0]();
  env.offer = OFFER;
});

check("each origin is named the way the chips name it", () => {
  assert.equal(env.origin({ source: "package" }, "proj"), "claunch's copy, not installed in proj");
  assert.equal(env.origin({ source: "workspace", status: "current" }, "proj"), "installed in proj");
  assert.equal(env.origin({ source: "workspace", status: "stale" }, "proj"),
    "installed in proj, older than claunch's");
  assert.equal(env.origin({ source: "workspace", status: "local" }, "proj"), "proj's own pack");
  assert.equal(env.origin({ source: "daemon" }, "proj"), "this daemon's prompt, every session");
});

function newTick(id, on) {
  const box = boxes().find((b) => b.value === id);
  box.checked = on;
  box.handlers.change[0]();
}

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

queue.then(() => setTimeout(() => {
  if (failures) process.exit(1);
  console.log("prompt_packs_check: ok");
}, 10));
