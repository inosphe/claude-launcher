/* One form, two tabs — the session modal, run against the real functions.

   Creating a session and spawning a child were two forms: the `#/new` page's
   `#new-session` and a wizard `buildSpawnForm` assembled in JavaScript. They
   asked the same questions, so every row existed twice and the two copies
   drifted. There is one form now — the shipped markup — and the modal
   BORROWS it: the node is moved into `#modal-body` on open and put back in
   `#new-view` on close.

   That arrangement has failure modes of its own, and they are what this
   harness holds:

   - The borrow itself. A form left in the modal body is a form the next
     dialog deletes (`body.innerText = ""`), and a form never moved back is
     a `#/new` page with nothing on it. Open and close are checked as a
     round trip, including the spawn box's remembered size, which is the one
     piece of state that has to be read off the box BEFORE the class that
     sizes it is dropped.
   - The tab. It is not a view: it sets the single answer the two paths
     differ on — whether a parent is named — and nothing else. So the values
     an operator typed must survive it, the parent picker and the strip must
     never disagree about which tab that makes it, and the title and the
     submit button have to follow.
   - The rows that only a child has. Three of them moved onto the shared
     form with this change and had no checks before it: the mesh row's
     refusal ("-"), the Connect list the spawn wizard owned, and the run
     refusal a cleared Workflow row travels as. Each is a key the daemon
     reads, so each is checked at the payload.
   - The seed. The leader's quick job fills three pickers and a task, and
     the option sets arrive afterwards — a seeded answer set before the
     fills is one those fills can drop, which is why it is applied twice.

   The entry points and the success path are checked against the source
   text: they are single call sites, and what can break about them is that
   somebody rewires one of the three buttons to something else. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

let failures = 0;
function check(label, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g === w) return;
  failures++;
  console.error(`FAIL ${label}\n  got  ${g}\n  want ${w}`);
}

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  const head = src.slice(start - 6, start) === "async " ? start - 6 : start;
  let depth = 0;
  // From the BODY's brace, not the first one: a default argument (`opts =
  // {}`) puts a pair in the signature, and counting from there closes the
  // function at the wrong place.
  for (let j = src.indexOf(") {", start) + 2; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

/* ---- the markup the modal borrows ------------------------------------- */
const formStart = html.indexOf('<form id="new-session">');
const formEnd = html.indexOf("</form>", formStart);
const formHtml = html.slice(formStart, formEnd);
check("the form still sleeps inside the page's own view",
      html.lastIndexOf('<div id="new-view"', formStart) >= 0, true);
check("the tab strip is inside the form, so the borrow carries it",
      formHtml.includes('id="new-tabs"'), true);
check("both tabs are declared, with the New session tab selected",
      [...formHtml.matchAll(/id="(new-tab-\w+)"[\s\S]*?aria-selected="(\w+)"/g)]
        .map((m) => [m[1], m[2]]),
      [["new-tab-new", "true"], ["new-tab-spawn", "false"]]);
check("the parent row has an id, since the New session tab folds it away",
      formHtml.includes('<label id="new-parent-row">Parent'), true);
check("the connect row is a frame the mesh poll fills",
      /<div id="new-connect-row" class="hidden"><\/div>/.test(formHtml), true);
check("the page head is addressable, since the box carries the title",
      formHtml.includes('id="new-page-head"'), true);
check("the strip is styled on its own classes, not on a modal selector",
      /\.sess-modal-tabs \{/.test(css) &&
      /\.sess-modal-tab\[aria-selected="true"\] \{/.test(css), true);
check("the form drops its page column width inside the box",
      /#modal-overlay\.spawn-open #new-session \{ max-width: none; \}/.test(css),
      true);

/* ---- the three ways in, and the one way to #/new ---------------------- */
check("the rail's + and the detail panel's Spawn button both open it",
      (src.match(/openSpawnModal\((?:target|s)\.name\)/g) || []).length, 3);
check("the leader's quick job opens it with its pickers as the seed",
      /openSpawnModal\(s\.name, \{ seed: \{[\s\S]{0,200}quick: true/.test(src),
      true);
check("openSpawnModal is the spawn tab with the opener pinned",
      /openSessionModal\(\{ tab: "spawn", parent: parentName,/.test(src), true);
check("the #/new route keeps its name and its mesh argument",
      /case "new": showView\("new"\); openNewSession\(r\.mesh\);/.test(src),
      true);
check("...and that opener is the New session tab",
      /openSessionModal\(\{ tab: "new", mesh: pendingNewMesh \}\)/.test(src),
      true);
check("the PR wizard gives the borrowed form back before clearing the body",
      /async function openPrModal\(sessionName\) \{[\s\S]{0,400}if \(sessionModal\) sessionModalClose\(\);/
        .test(src), true);

/* ---- the success path ------------------------------------------------- */
const submit = src.slice(src.indexOf('$("new-session").addEventListener("submit"'));
check("a created session closes the box it was created in",
      /if \(sessionModal\) sessionModalClose\(\{ route: false \}\);/.test(submit),
      true);
check("...and the rail is awaited before the route to the new session",
      submit.indexOf("await refreshSessions();") <
        submit.indexOf('location.hash = "#/s/"'), true);
check("...and a spawn from the open session refreshes its children list",
      /if \(parent && sessName === parent\.name\) refreshSessKids\(\);/
        .test(submit), true);
check("a blank worktree name is the daemon's to fill, spelt per endpoint",
      /body\.worktree = typed \|\| \(parent \? true : ""\);/.test(submit),
      true);
check("the picks the next opening starts from are written down",
      /saveSpawnRecall\(\{\s*parent: parent \? parent\.name : "",/.test(submit),
      true);

/* ---- the stub DOM ----------------------------------------------------- */
function classList(node) {
  return {
    add: (c) => node.classes.add(c),
    remove: (c) => node.classes.delete(c),
    contains: (c) => node.classes.has(c),
    toggle: (c, on) => (on === undefined
      ? (node.classes.has(c) ? node.classes.delete(c) : node.classes.add(c))
      : (on ? node.classes.add(c) : node.classes.delete(c))),
  };
}
function node(tag = "div", id = "") {
  const n = {
    tag, id, kids: [], textContent: "", title: "", value: "", type: "",
    checked: false, disabled: false, className: "", classes: new Set(),
    attrs: {}, handlers: {},
    appendChild(k) {
      // A move, like the real appendChild: the borrow depends on the node
      // leaving its old home, and a stub that copies it would pass a check
      // the browser fails.
      if (k && k.parent) k.parent.kids = k.parent.kids.filter((x) => x !== k);
      n.kids.push(k);
      if (k) k.parent = n;
      return k;
    },
    append(...ks) { ks.forEach((k) => n.appendChild(k)); },
    removeChild(k) { n.kids = n.kids.filter((x) => x !== k); return k; },
    setAttribute(k, v) { n.attrs[k] = v; },
    getAttribute(k) { return n.attrs[k]; },
    addEventListener(name, fn) { (n.handlers[name] ||= []).push(fn); },
    fire(name) { (n.handlers[name] || []).forEach((fn) => fn({})); },
    querySelector(sel) {
      return sel === "button[type=submit]" ? n._submit
        : sel === ".modal-box" ? n._boxChild : null;
    },
    getBoundingClientRect() { return { width: n._w || 0, height: n._h || 0 }; },
    style: {},
    get innerHTML() { return ""; },
    set innerHTML(v) { if (v === "") n.kids.length = 0; },
    get innerText() { return ""; },
    set innerText(v) { if (v === "") n.kids.length = 0; },
  };
  n.classList = classList(n);
  return n;
}
function picker(pairs = []) {
  const sel = node("select");
  sel.options = pairs.map(([l, v]) => Object.assign(node("option"),
                                                    { textContent: l, value: v }));
  sel.appendChild = (o) => { sel.options.push(o); return o; };
  Object.defineProperty(sel, "innerHTML", {
    get: () => "", set: (v) => { if (v === "") sel.options.length = 0; },
  });
  return sel;
}

const ids = {};
function $(id) { return ids[id] || null; }
for (const id of ["modal-overlay", "modal-body", "modal-actions", "modal-title",
                  "new-view", "new-page-head", "new-parent-row",
                  "new-handle-row", "new-connect-row", "new-tab-new",
                  "new-tab-spawn", "create-status", "create-error"]) {
  ids[id] = node("div", id);
}
ids["modal-overlay"].classes.add("hidden");
const modalBox = node("div", "modal-box");
ids["modal-overlay"]._boxChild = modalBox;
ids["modal-overlay"].appendChild(modalBox);

const form = node("form", "new-session");
form.parent = picker([["(none — a session of its own)", ""],
                      ["lead — running", "lead"]]);
form.mesh = picker();
form.handle = node("input");
form.role = picker([["(no role)", ""], ["worker", "worker"]]);
form.workflow = picker([["(none)", ""], ["improv-worker", "improv-worker"]]);
form.context = node("input");
form.task = node("textarea");
form.name = node("input");
form.args = node("input");
form.profile = picker([["work", "work"], ["home", "home"]]);
form.null_token = node("input");
form.worktree_mode = { value: "" };
form.cwd = picker([["(daemon cwd)", ""],
                   ["launcher — F:/works/claude-launcher",
                    "F:/works/claude-launcher"]]);
form.beads = { value: "new" };
form.issue = picker([["(pick an issue)", ""]]);
form._submit = Object.assign(node("button"), { textContent: "Create" });
form._submit.attrs.type = "submit";
ids["new-session"] = form;
ids["new-view"].appendChild(form);

let parentNow = null;
const calls = [];
/* The bag the sliced module closed over. Mutated in place rather than
   rebuilt, so a check can change what the functions see. */
const state = {
  meshCache: [], workflowsCache: [], calls, recall: {},
  parent: () => parentNow,
  candidate: (m) => !!m && m.live !== false,
  workspaces: [{ name: "launcher", path: "F:/works/claude-launcher" }],
};
const stored = {};
const localStorage = {
  getItem: (k) => (k in stored ? stored[k] : null),
  setItem: (k, v) => { stored[k] = String(v); },
};

const ctx = {};
new Function(
  "exports", "$", "document", "window", "localStorage", "location", "api",
  "state",
  `let meshCache = state.meshCache, workflowsCache = state.workflowsCache;
let newWfPicked = false, pendingNewMesh = "";
let newWorktreeFor = "held", issuesFor = "held", issuesRead = true,
    issueFilter = "held";
let sessName = "lead";
let sessionModal = null;
let sessionConnectHandles = [], sessionConnectPicked = [];
let sessionConnectFor = null, sessionParentMeta = {};
const BASE = "b";
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
}
function spawnParent() { return state.parent(); }
function spawnRecall() { return state.recall; }
function connectCandidate(m) { return state.candidate(m); }
function renderRoleStance() {}
function syncNewWorktree() { state.calls.push("worktree-sync"); }
function syncSpawnMode() { state.calls.push("spawn-mode"); }
function refreshSpawnPolicy() { state.calls.push("policy"); }
function refreshNewWorktree() { state.calls.push("worktree"); }
function refreshWorkflowChoices() { state.calls.push("workflows"); }
function refreshWorkspaces() { state.calls.push("workspaces"); }
function refreshRoles(mesh) { state.calls.push("roles:" + mesh); }
function refreshIssueChoices() { state.calls.push("issues"); }
function beadsMode() { return "new"; }
function refreshParentChoices() { state.calls.push("parents"); }
function syncOnboardPickers() { state.calls.push("pickers"); }
function go(hash) { state.calls.push("go:" + hash); }
function applySessionCwdChange() { state.calls.push("cwd-change"); }
function syncBeadsRow() { state.calls.push("beads-row"); }
function refreshIssueChoices() { state.calls.push("issues"); }
let pendingSeedIssue = "";
let workspacesCache = state.workspaces;
function sessionModalKey() {}
function Option(label, value) {
  return Object.assign(document.createElement("option"),
                       { textContent: label, value });
}
` + slice("clampSpawnSize") + slice("spawnSizeRecall") + slice("spawnSizeApply")
  + slice("spawnSizeRemember")
  + slice("readParentMeshes")
  + slice("ensureOption") + slice("applySessionModalSeed")
  + slice("applySessionModalRecall") + slice("sessionMeshNow")
  + slice("renderSessionConnect") + slice("refreshSessionConnect")
  + slice("sessionConnectFields") + slice("setSessionModalTab")
  + slice("syncSessionModalTabStrip") + slice("syncSessionModalChrome")
  + slice("applySessionParentChange") + slice("openSessionModal")
  + slice("sessionModalClose") + slice("openSpawnModal") + `
const SPAWN_SIZE_KEY = \`claunch_spawnsize:\${BASE}\`;
const SPAWN_W_MIN = 420, SPAWN_H_MIN = 240;
const spawnWMax = () => 2000, spawnHMax = () => 1200;
Object.assign(exports, {
  open: openSessionModal, close: sessionModalClose, spawn: openSpawnModal,
  tab: setSessionModalTab, seed: applySessionModalSeed,
  recall: applySessionModalRecall, connect: refreshSessionConnect,
  connectFields: sessionConnectFields, meshNow: sessionMeshNow,
  heldIssue: () => pendingSeedIssue,
  modal: () => sessionModal, picked: (v) => { sessionConnectPicked = v; },
  handles: () => sessionConnectHandles, wfPicked: () => newWfPicked,
  pending: () => pendingNewMesh,
});`
)(
  ctx, $, { createElement: node, addEventListener() {}, removeEventListener() {} },
  { innerWidth: 1600, innerHeight: 1000 }, localStorage,
  { hash: "#/" },
  async (url) => { calls.push("api:" + url); return apiAnswer(url); },
  state
);

let meshDoc = { members: [] };
function apiAnswer(url) {
  if (url.startsWith("/api/sessions/") && url.endsWith("/meta")) {
    return { ok: true, json: async () => ({ meshes: [{ mesh: "m1", handle: "lead" }] }) };
  }
  if (url.startsWith("/api/mesh/")) {
    return { ok: true, json: async () => meshDoc };
  }
  return { ok: false, json: async () => ({}) };
}

/* ---- the borrow: open, and the round trip ----------------------------- */
async function main() {
  await ctx.open({ tab: "new" });
  check("opening moves the form into the modal body",
        ids["modal-body"].kids.map((k) => k.id), ["new-session"]);
  check("...and takes the page's own heading down",
        ids["new-page-head"].classes.has("hidden"), true);
  check("...and the overlay is up, wearing the class that widens the box",
        [ids["modal-overlay"].classes.has("hidden"),
         ids["modal-overlay"].classes.has("spawn-open")], [false, true]);
  check("...with exactly one action beside the form's own submit",
        ids["modal-actions"].kids.map((k) => k.textContent), ["Cancel"]);
  check("the New session tab hides the parent row it has answered",
        ids["new-parent-row"].classes.has("hidden"), true);
  check("...and the strip says which tab that is",
        [ids["new-tab-new"].attrs["aria-selected"],
         ids["new-tab-spawn"].attrs["aria-selected"]], ["true", "false"]);
  check("...and the box's title and the button read as a create",
        [ids["modal-title"].textContent, form._submit.textContent],
        ["New session", "Create"]);

  // Something typed, so the tab switch can be checked for keeping it.
  form.task.value = "read the board";
  form.name.value = "kid";
  parentNow = { name: "lead", cwd: "F:/repo" };
  ctx.modal().parent = "lead";
  ctx.tab("spawn");
  check("the spawn tab pins the opener as the parent",
        form.parent.value, "lead");
  check("...and shows the parent row, since another parent is a valid change",
        ids["new-parent-row"].classes.has("hidden"), false);
  check("...and the title and the button follow the parent",
        [ids["modal-title"].textContent, form._submit.textContent],
        ["Spawn a child of lead", "Spawn child"]);
  check("...and nothing the operator typed was cleared",
        [form.task.value, form.name.value], ["read the board", "kid"]);
  check("...and the parent change re-asks everything that depends on it",
        ["spawn-mode", "policy", "worktree", "pickers"]
          .every((c) => calls.includes(c)), true);

  parentNow = null;
  ctx.tab("new");
  check("back on the New session tab the parent is cleared, not remembered",
        form.parent.value, "");
  check("...and the typed values are still there",
        [form.task.value, form.name.value], ["read the board", "kid"]);

  // The box was dragged; closing writes that size down and strips the inline
  // pair, which is the only reason close runs before the class is dropped.
  modalBox._w = 820; modalBox._h = 640;
  modalBox.style.width = "820px";
  ctx.close();
  check("closing gives the form back to the page",
        ids["new-view"].kids.map((k) => k.id), ["new-session"]);
  check("...leaving the modal body empty for the next dialog",
        ids["modal-body"].kids.length, 0);
  check("...and the page heading comes back with it",
        ids["new-page-head"].classes.has("hidden"), false);
  check("...and the overlay is down, without the widening class",
        [ids["modal-overlay"].classes.has("hidden"),
         ids["modal-overlay"].classes.has("spawn-open")], [true, false]);
  check("...and the dragged size is remembered under the spawn box's key",
        JSON.parse(stored["claunch_spawnsize:b"] || "null"),
        { w: 820, h: 640 });
  check("...with the inline pair stripped, so a confirm dialog is prose-wide",
        [modalBox.style.width, modalBox.style.height], ["", ""]);
  check("closing twice is not a second close",
        ctx.modal(), null);

  /* ---- the seed --------------------------------------------------------- */
  form.task.value = ""; form.name.value = "";
  parentNow = { name: "lead", cwd: "F:/repo" };
  await ctx.spawn("lead", { seed: { quick: true, role: "worker",
                                    workflow: "improv-worker", worktree: true,
                                    task: "take the next issue" } });
  check("the quick job's pickers and task land on the shared form",
        [form.role.value, form.workflow.value, form.task.value,
         form.worktree_mode.value],
        ["worker", "improv-worker", "take the next issue", "new"]);
  check("...and a seeded workflow counts as picked, so no auto-pick overrides it",
        ctx.wfPicked(), true);
  check("...and the seed is applied again after the option sets land",
        calls.filter((c) => c === "pickers").length >= 2, true);
  ctx.close();

  /* ---- a seed for a value the list does not hold ------------------------ */
  form.role = picker([["(no role)", ""]]);
  ctx.seed(form, { role: "reviewer" });
  check("a seeded answer the fill did not offer is added rather than dropped",
        [form.role.value, form.role.options.map((o) => o.value)],
        ["reviewer", ["", "reviewer"]]);

  /* ---- a seed that already knows the directory and the issue ----------- */
  ctx.seed(form, { workspace: "launcher", issue: "claunch-lxuy" });
  check("a seeded workspace NAME lands as the path the picker speaks",
        form.cwd.value, "F:/works/claude-launcher");
  check("...and the rows that follow the directory are re-asked",
        calls.includes("cwd-change"), true);
  check("a seeded issue opens the board row on the answer that picks one",
        form.beads.value, "existing");
  check("...and the id is HELD, since the picker is filled from a fetch",
        [ctx.heldIssue(), calls.includes("issues")], ["claunch-lxuy", true]);
  check("the held id is applied where every other fill applies its preset",
        /if \(pendingSeedIssue\) \{/.test(src) &&
        /pendingSeedIssue = "";/.test(src), true);
  check("...and an id the board does not offer is still offered, labelled",
        /not among this board's open candidates/.test(src), true);

  /* ---- what the modal remembers ---------------------------------------- */
  form.role = picker([["(no role)", ""], ["worker", "worker"]]);
  form.profile.value = "";
  ctx.recall(form);
  check("nothing remembered, nothing applied", [form.role.value, form.profile.value],
        ["", ""]);
  // A remembered pair is applied to rows that are open and empty...
  state.recall = { role: "worker", profile: "home:claude" };
  ctx.recall(form);
  check("a remembered role and profile are applied to open, empty rows",
        [form.role.value, form.profile.value], ["worker", "home"]);
  // ...and never onto a row the policy shut, nor over an answer given.
  form.role = picker([["(no role)", ""], ["worker", "worker"]]);
  form.role.disabled = true;
  form.profile.value = "work";
  ctx.recall(form);
  check("a greyed row and a filled one are both left alone",
        [form.role.value, form.profile.value], ["", "work"]);

  /* ---- the rows only a child has: mesh, connect, the run refusal -------- */
  form.mesh.value = "";
  meshDoc = { members: [
    { handle: "lead", live: true }, { handle: "worker_a", live: true },
    { handle: "gone", live: false },
  ] };
  await ctx.connect(true);
  check("the peers on offer are the parent's mesh, minus the parent itself",
        ctx.handles(), ["worker_a"]);
  check("...and the row is drawn with a box per peer",
        ids["new-connect-row"].kids.map((k) => k.tag),
        ["span", "label"]);
  check("...and it is not hidden once there is somebody to offer",
        ids["new-connect-row"].classes.has("hidden"), false);

  const body = {};
  ctx.picked(["worker_a"]);
  ctx.connectFields(form, body);
  check("a ticked peer travels as the connect list", body.connect, ["worker_a"]);

  const refused = {};
  form.mesh.value = "-";
  ctx.connectFields(form, refused);
  check("a child that refuses the mesh sends no wiring", refused.connect,
        undefined);
  check("...and the effective mesh is nothing, not the parent's",
        ctx.meshNow({ meshes: ["m1"] }), "");
  form.mesh.value = "";
  check("no pick inherits the parent's single mesh",
        ctx.meshNow({ meshes: ["m1"] }), "m1");
  check("...and a parent in several has no inheritance to name",
        ctx.meshNow({ meshes: ["m1", "m2"] }), "");
  form.mesh.value = "m9";
  check("an explicit pick outranks the inheritance",
        ctx.meshNow({ meshes: ["m1"] }), "m9");

  const hidden = {};
  form.mesh.value = "";
  ids["new-connect-row"].classes.add("hidden");
  ctx.picked(["worker_a"]);
  ctx.connectFields(form, hidden);
  check("a tick standing on a folded row is not an answer", hidden.connect,
        undefined);

  console.log("sessionmodal_check: " + (failures ? `${failures} failing` : "ok"));
  process.exitCode = failures ? 1 : 0;
}

main().catch((e) => { console.error(e); process.exitCode = 1; });
