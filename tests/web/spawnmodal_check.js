/* The spawn modal: the wizard as a dialog. A browser is the only honest judge
   of how it looks, but the RULES are where the mistakes live, and those are
   driven here against a stub DOM. Three things must hold: the brain ports the
   CLI wizard's gating (a row the policy locks greys with the key that opens
   it, and a value standing on a greyed row is not an answer the user gave),
   the payload is spelt in the CLI's own keys and reads through the disables,
   and the three ways a spawn starts — the rail's +, the detail Spawn button,
   the leader's quick job — all land in the same modal with the opener pinned
   as the parent. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"), "utf8");

/* ---- slice helpers, the same shape the other harnesses use -------------- */
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
function sliceStmt(decl) {
  const start = src.indexOf(decl);
  if (start < 0) throw new Error("missing " + decl);
  const end = src.indexOf(";", start);
  return src.slice(start, end + 1);
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), handlers: {}, dataset: {},
    value: undefined, checked: false, disabled: false, placeholder: "", rows: 0,
    title: "", onclick: null, isConnected: true, _checked: [],
    classList: {
      add(c) { n.classes.add(c); },
      remove(c) { n.classes.delete(c); },
      contains(c) { return n.classes.has(c); },
    },
    appendChild(c) { this.kids.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.appendChild(c)); },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    fire(k, ev) { return Promise.all((this.handlers[k] || []).map((fn) => fn(ev))); },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) {
      this.classes = new Set(String(v).split(/\s+/).filter(Boolean));
    },
    get innerText() { return this.text; },
    set innerText(v) { this.kids = []; this.text = String(v); },
    get innerHTML() { return ""; },
    set innerHTML(v) { this.kids = []; },
    get options() { return this.kids.filter((k) => k.tag === "option"); },
    querySelector(sel) {
      let pred = null;
      if (sel === "input") pred = (k) => k.tag === "input";
      else if (sel === ".sess-spawn-note") pred = (k) => k.tag === "span" &&
        k.classes.has("sess-spawn-note");
      return pred ? this._find(pred) : null;
    },
    _find(pred) {
      for (const k of this.kids) {
        if (pred(k)) return k;
        if (k._find) { const r = k._find(pred); if (r) return r; }
      }
      return null;
    },
  };
  if (tag === "input" || tag === "textarea") n.value = "";
  return n;
}
const walk = (n, out = []) => {
  if (!(n && n.kids && Array.isArray(n.kids))) {
    console.log("walk hit non-node:", n && n.tag, typeof n, n && n.kids);
    return out;
  }
  for (const k of n.kids) { out.push(k); walk(k, out); }
  return out;
};
const texts = (n) => walk(n).map((k) => k.text).join(" | ");
const tags = (n, tag) => walk(n).filter((k) => k.tag === tag);
const buttons = (n) => tags(n, "button");

const modalEls = {
  "modal-overlay": node("div"), "modal-title": node("h2"),
  "modal-body": node("div"), "modal-actions": node("div"),
};
const document = {
  createElement: (tag) => node(tag),
  getElementById: (id) => modalEls[id] || null,
  listeners: {},
  addEventListener(k, fn) { (this.listeners[k] ||= []).push(fn); },
  removeEventListener(k, fn) {
    this.listeners[k] = (this.listeners[k] || []).filter((f) => f !== fn);
  },
};
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}
const $ = (id) => document.getElementById(id);

/* ---- the daemon: every call recorded, every answer scripted ------------ */
let sent = [];
let routes = {};
const api = async (p, opts) => {
  const method = (opts && opts.method) || "GET";
  sent.push({ path: p, method, body: opts && opts.body && JSON.parse(opts.body) });
  // The longest prefix wins: "GET /api/mesh" must not swallow
  // "GET /api/mesh/m0", or the members answer would answer for the list.
  const key = Object.keys(routes)
    .filter((k) => {
      const [m, prefix] = k.split(" ");
      return m === method && p.startsWith(prefix);
    })
    .sort((a, b) => b.length - a.length)[0];
  const r = key ? routes[key] : { ok: false, status: 404, doc: {} };
  if (r.throw) throw new Error("offline");
  return { ok: r.ok !== false, status: r.status || 200, json: async () => r.doc || {} };
};

const store = {};
const localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
};

const stubs = `
let sessName = null;
let railRefreshed = 0, kidsRefreshed = 0, gotoHash = "";
let sessionsCache = [];
let spawnModal = null;
let BASE = "/";
function refreshSessions() { railRefreshed++; }
/* The box's remembered size is a contract of its own — spawnsize_check drives
   the real pair. Here they are stubs: this harness is about the form's RULES,
   and a stub DOM has no box to measure. */
function spawnSizeApply() {}
function spawnSizeRemember() {}
function refreshSessKids() { kidsRefreshed++; }
function go(h) { gotoHash = h; }
`;

const ctx = {};
new Function(
  "exports", "document", "el", "api", "localStorage", "$",
  stubs
  + sliceStmt("const SPAWN_RECALL_FIELDS =")
  + sliceStmt("const SPAWN_RECALL_KEY =")
  + slice("spawnRecall") + slice("saveSpawnRecall")
  + slice("spawnMeshNow") + slice("spawnAutoWorktree")
  + slice("spawnWorkflowEntry") + slice("spawnWorkflowAdmits") + slice("spawnRankWorkflows")
  + slice("syncSpawnGates") + slice("spawnPayload")
  + slice("spawnReport") + slice("spawnPreflightNote")
  + slice("spawnHardBlocks") + slice("postSpawn")
  + slice("spawnMissingSources") + slice("spawnSourceNote")
  + slice("qjStamp") + slice("fillSpawnSelect") + slice("spawnRow") + slice("spawnCheckRow")
  + slice("refillSpawnWorkflows") + slice("spawnConnectNow")
  + slice("buildSpawnForm")
  + slice("spawnModalKey") + slice("spawnModalClose") + slice("openSpawnModal")
  + slice("spawnModalLoad") + slice("refreshSpawnConnect") + slice("spawnModalGo")
  + `
Object.assign(exports, {
  spawnPayload, syncSpawnGates, spawnRankWorkflows, spawnWorkflowAdmits,
  spawnWorkflowEntry, spawnAutoWorktree, spawnMeshNow,
  spawnRecall, saveSpawnRecall, buildSpawnForm, openSpawnModal, spawnModalClose,
  refillSpawnWorkflows, spawnConnectNow,
  spawnMissingSources, spawnSourceNote,
  setSessions: (a) => { sessionsCache = a; },
  setSess: (n) => { sessName = n; },
  isOpen: () => spawnModal !== null,
  counters: () => ({ rail: railRefreshed, kids: kidsRefreshed, goto: gotoHash }),
});`
)(ctx, document, el, api, localStorage, $);

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) return;
  failures++;
  console.log(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
};
const settle = () => new Promise((r) => setImmediate(r));

/* ---- plain-object control stub for the rules ----------------------------- */
const ctl = (extra = {}) => Object.assign({
  value: "", checked: false, disabled: false, hidden: false, textContent: "",
}, extra);

function uiStub(over = {}) {
  return Object.assign({
    parent: { value: "lead1" }, parentSess: {}, parentMesh: "", report: {},
    git: { repo: true, worktrees: [] }, stamp: "20260824-210000",
    name: ctl(), role: ctl(), workflow: ctl(), context: ctl(), contextRow: ctl(),
    mesh: ctl(), handle: ctl(), task: ctl(), args: ctl(),
    harness: ctl(), profile: ctl(), borrow: ctl(),
    nullTok: ctl(), fork: ctl(), over: ctl(), overRow: ctl(),
    harnessNote: ctl(), profileNote: ctl(), borrowNote: ctl(),
    nullNote: ctl(), forkNote: ctl(), argsNote: ctl(),
    workspace: ctl(), workspaceNote: ctl(),
    worktree: ctl(), worktreeNote: ctl(), wtName: ctl(),
    update: ctl(), rebase: ctl(), wtRow: ctl(), wtNameRow: ctl(),
    updateRow: ctl(), rebaseRow: ctl(),
    handleRow: ctl(), connectRow: ctl(), connectHandles: [],
    meshNote: ctl(), parentMeshes: [],
    connect: () => [],
  }, over);
}

async function main() {
  /* ---- the brain: ranking ---------------------------------------------- */
  const WL = { type: "whitelist", roles: ["worker", "qa"] };
  check("whitelist admits its own role",
    ctx.spawnWorkflowAdmits({ filter_roles: WL }, "worker") === true);
  check("...and turns an outsider away",
    ctx.spawnWorkflowAdmits({ filter_roles: WL }, "leader") === false);
  check("no filter and no role admit everything",
    ctx.spawnWorkflowAdmits({}, "") === true);
  check("an unknown filter type volunteers nothing",
    ctx.spawnWorkflowAdmits({ filter_roles: { type: "all", roles: ["worker"] } }, "worker") === false);

  const ranked = ctx.spawnRankWorkflows([
    { name: "review", default_role: "qa", priority: 1 },
    { name: "improv-worker", default_role: "worker", priority: 5 },
    { name: "secret", default_role: "", priority: 9, filter_roles: { type: "blacklist", roles: ["worker"] } },
    "bare",
  ], "worker");
  check("the role's own default ranks first",
    ranked.options[0].name === "improv-worker", ranked.options[0]);
  check("...and is the auto-pick", ranked.auto === "improv-worker");
  check("a refused workflow is volunteered last",
    ranked.options[ranked.options.length - 1].name === "secret");
  check("ranking marks the refusal in the option detail",
    ranked.options.some((o) => /filter_roles turns 'worker' away/.test(o.detail)), ranked.options);

  const auto = ctx.spawnAutoWorktree(uiStub({ name: ctl({ value: "w7" }) }));
  check("the auto name is name-stamped", auto === "w7-20260824-210000", auto);
  const autoParent = ctx.spawnAutoWorktree(uiStub());
  check("...falling back to the parent's name",
    autoParent === "lead1-20260824-210000", autoParent);
  check("mesh picks the explicit mesh over the parent's",
    ctx.spawnMeshNow(uiStub({ mesh: ctl({ value: "m1" }), parentMesh: "m0" })) === "m1");
  check("'-' means no mesh at all",
    ctx.spawnMeshNow(uiStub({ mesh: ctl({ value: "-" }), parentMesh: "m0" })) === "");
  check("no pick inherits the parent's mesh",
    ctx.spawnMeshNow(uiStub({ parentMesh: "m0" })) === "m0");

  /* ---- the brain: payload reads THROUGH the disables -------------------- */
  const full = uiStub({
    report: { may_choose: ["profile", "args", "worktree", "fork", "borrow"] },
    name: ctl({ value: "  c7 " }),
    role: ctl({ value: "worker" }), workflow: ctl({ value: "improv-worker" }),
    mesh: ctl({ value: "m0" }), handle: ctl({ value: "c7" }),
    connect: () => ["w2", ""],
    harness: ctl({ value: "claude" }), profile: ctl({ value: "p1" }),
    borrow: ctl({ value: "p2" }), nullTok: ctl({ checked: true }),
    args: ctl({ value: "--verbose --json" }),
    workspace: ctl({ value: "ws" }),
    worktree: ctl({ value: "@named" }), wtName: ctl({ value: "my-wt" }),
    wtRow: ctl({ hidden: false }), rebaseRow: ctl({ hidden: true }),
    update: ctl(), rebase: ctl(),
    overRow: ctl({ hidden: true }), over: ctl({ checked: true }),
  });
  const body = ctx.spawnPayload(full);
  check("payload trims the name", body.name === "c7", body);
  check("payload spells the CLI keys", body.role === "worker" && body.workflow === "improv-worker");
  check("payload sends mesh and handle", body.mesh === "m0" && body.handle === "c7");
  check("payload sends the connect list", body.connect && body.connect.join(",") === "w2", body.connect);
  check("payload splits args", body.args.join(" ") === "--verbose --json", body.args);
  check("@named sends the given worktree name", body.worktree === "my-wt", body.worktree);
  check("a hidden over-limit row is not asked",
    body.over_limit === undefined, body);

  // A value standing on a greyed row is not an answer the user gave.
  const greyed = uiStub({
    report: { may_choose: [] },
    harness: ctl({ value: "pi", disabled: true }),
    profile: ctl({ value: "p1", disabled: true }),
    borrow: ctl({ value: "p2", disabled: true }),
    nullTok: ctl({ checked: true, disabled: true }),
    args: ctl({ value: "-x", disabled: true }),
    workspace: ctl({ value: "ws", disabled: true }),
    fork: ctl({ checked: true, disabled: true }),
    worktree: ctl({ value: "@auto", disabled: true }), wtRow: ctl({ hidden: false }),
    overRow: ctl({ hidden: true }), over: ctl({ checked: true }),
    mesh: ctl({ value: "-" }), role: ctl({ value: "" }),
  });
  const greyBody = ctx.spawnPayload(greyed);
  check("a greyed harness is not sent", greyBody.harness === undefined, greyBody);
  check("a greyed profile is not sent", greyBody.profile === undefined);
  check("a greyed --null is not sent", greyBody.null_token === undefined);
  check("a greyed fork is not sent", greyBody.fork === undefined);
  check("a greyed worktree is not sent", greyBody.worktree === undefined);
  check("'-' mesh travels as none, without a handle",
    greyBody.mesh === "-" && greyBody.handle === undefined, greyBody);
  check("only the '-' mesh travels from an all-greyed form",
    Object.keys(greyBody).join(",") === "mesh", Object.keys(greyBody));

  /* ---- the brain: gates -------------------------------------------------- */
  const g = uiStub({
    report: { may_choose: [], spawnable_harnesses: [], workspaces: null },
    harness: ctl({ value: "" }), parentSess: { harness: "claude" },
    git: { repo: true }, over: ctl(), update: ctl(),
  });
  ctx.syncSpawnGates(g);
  check("harness is read-only because the profile owns it",
    g.harness.disabled === true && /profile owns/.test(g.harnessNote.textContent),
    g.harnessNote.textContent);
  check("locked profile names its key",
    g.profile.disabled === true && /spawn\.allow_profile/.test(g.profileNote.textContent));
  check("no workspaces list locks the directory row",
    g.workspace.disabled === true && /spawn\.allow_workspace/.test(g.workspaceNote.textContent));
  check("worktree not in may_choose locks the row",
    g.worktree.disabled === true && /spawn\.allow_worktree/.test(g.worktreeNote.textContent));

  const g2 = uiStub({
    report: { may_choose: ["worktree", "profile", "fork"], workspaces: [] },
    harness: ctl({ value: "claude" }), parentSess: { harness: "claude" },
    git: { repo: true, worktrees: ["tan"] }, worktree: ctl({ value: "tan" }),
    update: ctl({ checked: true }), fork: ctl(),
  });
  ctx.syncSpawnGates(g2);
  check("a choosable worktree stays open", g2.worktree.disabled === false);
  const gRepo = uiStub({
    report: { may_choose: ["worktree"] }, git: { repo: false },
    harness: ctl({ value: "claude" }), parentSess: { harness: "claude" },
    worktree: ctl(), update: ctl(), over: ctl(),
  });
  ctx.syncSpawnGates(gRepo);
  check("a non-repo directory takes the worktree rows away", gRepo.wtRow.hidden === true);
  check("a reused checkout offers rebase", g2.updateRow.hidden === false && g2.rebaseRow.hidden === false);
  check("a worktree of its own denies the fork",
    g2.fork.disabled === true && /worktree of its own/.test(g2.forkNote.textContent),
    g2.forkNote.textContent);

  const g3 = uiStub({
    report: { may_choose: ["fork"] }, git: {},
    harness: ctl({ value: "" }), profile: ctl({ value: "pi-profile" }),
    profileDetails: { "pi-profile": { harness: "pi" } },
    parentSess: { harness: "claude" },
    nullTok: ctl(), borrow: ctl(), worktree: ctl({ value: "" }),
    over: ctl(), update: ctl(),
    mesh: ctl({ value: "-" }), connectHandles: ["a"],
  });
  ctx.syncSpawnGates(g3);
  check("a non-claude child cannot --null",
    g3.nullTok.disabled === true && /claude harness only/.test(g3.nullNote.textContent));
  check("...nor borrow a token",
    g3.borrow.disabled === true && /claude harness only/.test(g3.borrowNote.textContent));
  check("mesh '-' takes handle and connect away",
    g3.handleRow.hidden === true && g3.connectRow.hidden === true);

  /* ---- inheriting a mesh: when it resolves, and when it cannot ---------- */
  /* Sitting on "(inherit the parent's mesh)" is an answer the DAEMON settles
     (daemon/onboard.py inherit_mesh), and it settles three ways: one mesh is
     the answer, none opens a fresh one holding just the pair, and several is
     REFUSED by name — guessing there does not fail, it broadcasts the child
     into a room of strangers. The form must not guess where the daemon will
     not, so the ambiguity is said on the row before Spawn is pressed. */
  const meshCase = (over) => {
    const u = uiStub(Object.assign({
      report: { may_choose: [] }, git: {},
      parentSess: { harness: "claude" }, over: ctl(), update: ctl(),
    }, over));
    ctx.syncSpawnGates(u);
    return u;
  };

  const oneMesh = meshCase({ parentMeshes: ["m0"], parentMesh: "m0", mesh: ctl({ value: "" }) });
  check("one mesh: inheriting resolves, so the row says nothing",
    oneMesh.meshNote.hidden === true, oneMesh.meshNote.textContent);
  check("...and spawnMeshNow answers with it",
    ctx.spawnMeshNow(oneMesh) === "m0", ctx.spawnMeshNow(oneMesh));

  /* No mesh is NOT the same as no answer: the daemon opens one for the pair,
     so the child still gets a handle in it. */
  const noneMesh = meshCase({ parentMeshes: [], parentMesh: "", mesh: ctl({ value: "" }) });
  check("no mesh: inheriting is still unambiguous",
    noneMesh.meshNote.hidden === true, noneMesh.meshNote.textContent);
  check("...and the handle row stays, because a fresh mesh still needs one",
    noneMesh.handleRow.hidden === false);

  const many = meshCase({
    parentMeshes: ["m0", "m9"], parentMesh: "", mesh: ctl({ value: "" }),
  });
  check("several meshes: the row says the daemon will refuse",
    many.meshNote.hidden === false && /m0/.test(many.meshNote.textContent)
      && /m9/.test(many.meshNote.textContent), many.meshNote.textContent);
  check("...and the picker stays live, because naming one is the fix",
    many.mesh.disabled !== true);
  check("...and nothing is guessed for the connect offers",
    ctx.spawnMeshNow(many) === "", ctx.spawnMeshNow(many));

  const manyNamed = meshCase({
    parentMeshes: ["m0", "m9"], parentMesh: "", mesh: ctl({ value: "m9" }),
  });
  check("several meshes, one named: the ambiguity is gone",
    manyNamed.meshNote.hidden === true, manyNamed.meshNote.textContent);
  check("...and that name is what travels",
    ctx.spawnMeshNow(manyNamed) === "m9");

  /* Declining outright is an answer too — not an unresolved inherit. */
  const manyNone = meshCase({
    parentMeshes: ["m0", "m9"], parentMesh: "", mesh: ctl({ value: "-" }),
  });
  check("several meshes, '-' picked: not ambiguous, just off the air",
    manyNone.meshNote.hidden === true && manyNone.handleRow.hidden === true,
    manyNone.meshNote.textContent);

  /* ---- recall: remembered between spawns, and only the fields ------------ */
  ctx.saveSpawnRecall({ parent: "lead1", role: "worker", profile: "p1",
                        borrow: "p2", null_token: true, stray: "x" });
  const rec = ctx.spawnRecall();
  check("recall round-trips the remembered fields",
    rec.role === "worker" && rec.profile === "p1" && rec.borrow === "p2" && rec.null_token === true, rec);
  check("recall drops fields it does not remember", rec.stray === undefined, rec);

  /* ---- the form: every control the brain reads is built ------------------ */
  const built = ctx.buildSpawnForm("lead1", { quick: true, task: "fix the tab", name: "w7" });
  const bui = built.ui;
  for (const k of ["name", "role", "workflow", "context", "mesh", "handle", "task",
                   "harness", "profile", "borrow", "args", "workspace", "worktree",
                   "wtName", "update", "rebase", "fork", "over"]) {
    check(`form builds ${k}`, bui[k] && typeof bui[k] === "object", k);
  }
  check("parent is pinned to the opener", bui.parent.value === "lead1", bui.parent);
  check("the seed task lands in the field", bui.task.value === "fix the tab", bui.task.value);
  check("the seed name lands in the field", bui.name.value === "w7");
  check("the stub stamp is a stamped name",
    /^\d{8}-\d{6}$/.test(bui.stamp), bui.stamp);
  check("the parent line names the opener",
    texts(built.box).includes("child of lead1"));

  /* ---- the route: the quick job opening lands in the same modal ---------- */
  Object.keys(store).forEach((k) => delete store[k]);
  const m0Members = { members: [{ handle: "lead1", role: "leader" },
                                { handle: "w2", role: "worker" }] };
  routes = {
    "GET /api/sessions/lead1/meta": { doc: {
      session: { name: "lead1", cwd: "C:/repo", harness: "claude" },
      meshes: [{ mesh: "m0", handle: "lead1", role: "leader" }],
    } },
    "GET /api/sessions/lead1/children": { doc: {
      can_spawn: true, children_remaining: 3,
      may_choose: ["profile", "args", "worktree", "fork", "borrow"],
      spawnable_harnesses: ["claude"], workspaces: null,
    } },
    "GET /api/roles": { doc: { roles: [{ name: "leader" }, { name: "worker" }] } },
    "GET /api/profiles": { doc: { profiles: ["p1", "p2"] } },
    "GET /api/mesh": { doc: { meshes: [{ name: "m0" }] } },
    "GET /api/git?cwd=C%3A%2Frepo": { doc: { repo: true, worktrees: [] } },
    "GET /api/cflow/workflows?cwd=C%3A%2Frepo": { doc: {
      workflows: [{ name: "improv-worker", default_role: "worker", priority: 5 },
                  "review"],
    } },
    "GET /api/mesh/m0": { doc: m0Members },
    "POST /api/sessions/lead1/children": { status: 201, doc: {
      session: { name: "job-1" }, mesh: { ok: true, mesh: "m0" },
    } },
  };
  ctx.setSessions([{ name: "lead1", cwd: "C:/repo", harness: "claude", status: "idle" }]);
  sent = [];
  await ctx.openSpawnModal("lead1", { seed: {
    quick: true, role: "worker", workflow: "improv-worker", worktree: true,
    task: "fix the tab",
  } });
  await settle();
  await settle();

  check("the modal is open", ctx.isOpen() === true);
  check("the title names the parent", modalEls["modal-title"].text === "Spawn a child of lead1",
    modalEls["modal-title"].text);
  const acts = buttons(modalEls["modal-actions"]);
  const spawn = acts.find((b) => b.text.startsWith("Spawn"));
  check("the quick job's seed arms the button", spawn && spawn.disabled === false);
  check("the preflight reports the slots",
    texts(modalEls["modal-body"]).includes("3 child slot(s) left"),
    texts(modalEls["modal-body"]).slice(-120));
  const mSel = nodeSel(modalEls["modal-body"], "select") || [];
  // Inherit is the default, not merely an option — the row opens the way
  // Harness, Profile and Directory do. Naming the parent's mesh outright is
  // the same answer only while the parent is in ONE mesh, and spelling it into
  // the payload takes the rule away from daemon/onboard.py inherit_mesh.
  const meshSel = mSel.find((s) => (s.options || [])
    .some((o) => o.text === "(inherit the parent's mesh)"));
  check("the mesh picker opens on inherit", meshSel && meshSel.value === "",
    meshSel && meshSel.value);
  check("...with the parent's own mesh still on offer to name outright",
    meshSel && (meshSel.options || []).some((o) => o.value === "m0"),
    meshSel && (meshSel.options || []).map((o) => o.value));
  check("the role seed lands", mSel.some((s) => s.value === "worker"),
    mSel.map((s) => s.value));
  check("the workflow seed lands",
    mSel.some((s) => s.value === "improv-worker"), mSel.map((s) => s.value));
  check("the task seed lands in the task box",
    tags(modalEls["modal-body"], "textarea")[0].value === "fix the tab",
    tags(modalEls["modal-body"], "textarea")[0].value);

  // The default mesh's own members, fetched for the connect offers.
  await settle();
  const connRow = walk(modalEls["modal-body"])
    .find((k) => k.classes && k.classes.has("sess-spawn-connect"));
  check("the connect row offers the other member only",
    connRow && tags(connRow, "input").length === 1 &&
      texts(connRow).includes("w2") && !texts(connRow).includes("lead1"),
    connRow && texts(connRow));

  // Tick the offered peer, so the payload below proves that inheriting the
  // mesh does not cost the connect offers: they are resolved against the
  // EFFECTIVE mesh, which is the parent's while the picker says inherit.
  const peerBox = connRow && tags(connRow, "input")[0];
  if (peerBox) { peerBox.checked = true; await peerBox.fire("change"); }

  // The leader's own panel is the opener, so a success refreshes the roster.
  ctx.setSess("lead1");
  sent = [];
  await spawn.fire("click");
  await settle();
  const post = sent.find((s) => s.method === "POST");
  check("spawn posts to the opener's children", post &&
    post.path === "/api/sessions/lead1/children", post && post.path);
  check("the quick-job seed travels: role", post && post.body.role === "worker");
  check("...workflow", post && post.body.workflow === "improv-worker");
  check("...and task", post && post.body.task === "fix the tab");
  // Omitted, not spelt: "" is how the payload says inherit, and `put` drops
  // an empty value rather than sending it.
  check("the mesh is left for the daemon to inherit",
    post && !("mesh" in post.body), post && post.body.mesh);
  // ...and inheriting must not cost the connect offers. They are resolved
  // against the EFFECTIVE mesh, so a child sitting on inherit still travels
  // with the peers that were ticked.
  check("the ticked peer still travels while inheriting",
    post && JSON.stringify(post.body.connect || []) === JSON.stringify(["w2"]),
    post && post.body.connect);
  check("the worktree is a stamped @auto under the parent",
    post && /^lead1-\d{8}-\d{6}$/.test(post.body.worktree || ""), post && post.body);
  check("success closes the modal", modalEls["modal-overlay"].classList.contains("hidden"));
  const counted = ctx.counters();
  check("success refreshes the roster and the rail",
    counted.kids >= 1 && counted.rail >= 1, counted);

  /* ---- a picker emptied by a failed fetch says so ------------------------
     The bug this pins: every option source degrades to null, so a daemon
     that answers /children but not /roles leaves the Role picker holding
     nothing but its placeholder — indistinguishable, on screen, from a
     daemon that declares no roles. The wizard must name the sources that
     did not arrive, in the warning colour, instead of standing there armed
     over blank pickers. */
  check("no missing sources means no note", ctx.spawnSourceNote([]) === "");
  check("one missing source is named, singular",
    /^could not load roles — that picker is empty/.test(
      ctx.spawnSourceNote(["roles"])), ctx.spawnSourceNote(["roles"]));
  check("several are named, plural",
    /^could not load roles, workflows — those pickers are empty/.test(
      ctx.spawnSourceNote(["roles", "workflows"])));
  check("a source is missing only when its doc is falsy",
    JSON.stringify(ctx.spawnMissingSources({
      roles: { roles: [] }, workflows: null, git: undefined, meshes: { a: 1 },
    })) === JSON.stringify(["workflows", "git"]),
    ctx.spawnMissingSources({
      roles: { roles: [] }, workflows: null, git: undefined, meshes: { a: 1 } }));
  // An EMPTY list is an answer and must NOT be reported as a failed fetch.
  check("an empty-but-present list is not 'missing'",
    ctx.spawnMissingSources({ roles: { roles: [] } }).length === 0);

  const goodRoles = routes["GET /api/roles"];
  const goodWfs = routes["GET /api/cflow/workflows?cwd=C%3A%2Frepo"];
  routes["GET /api/roles"] = { throw: true };
  routes["GET /api/cflow/workflows?cwd=C%3A%2Frepo"] = { ok: false, status: 500 };
  await ctx.openSpawnModal("lead1", {});
  await settle();
  await settle();
  const warned = texts(modalEls["modal-body"]);
  check("the modal names both dead sources",
    warned.includes("could not load roles, workflows"), warned.slice(-220));
  check("...and says an empty picker is not an empty offering",
    warned.includes("not because there is nothing to offer"));
  check("...still reporting the slots it did learn",
    warned.includes("3 child slot(s) left"), warned.slice(-220));
  routes["GET /api/roles"] = goodRoles;
  routes["GET /api/cflow/workflows?cwd=C%3A%2Frepo"] = goodWfs;
  // ...and a healthy load says nothing about sources.
  await ctx.openSpawnModal("lead1", {});
  await settle();
  await settle();
  check("a healthy load raises no source warning",
    !texts(modalEls["modal-body"]).includes("could not load"));

  /* ---- the child cap is a crossing, not a dead end ----------------------
     spawn.py folds the SOFT cap into `blocked_by` as well, so `can_spawn` is
     false at the limit — and reading that alone used to stop the load dead:
     no Workflow picker, no Over-limit row, a modal that said "child limit
     reached" over an empty dropdown. The cap is exactly what a busy fleet
     runs into, which is how "no workflows in the spawn modal" kept coming
     back. */
  const CAP = "child limit reached (4/4)";
  routes["GET /api/sessions/lead1/children"] = { doc: {
    can_spawn: false, blocked_by: [CAP], soft_blocked_by: [CAP],
    children_used: 4, children_remaining: 0,
    may_choose: ["worktree"], spawnable_harnesses: [],
  } };
  sent = [];
  await ctx.openSpawnModal("lead1", { seed: { role: "worker" } });
  await settle();
  await settle();
  const capSel = nodeSel(modalEls["modal-body"], "select") || [];
  const capWf = capSel.find((sel) => (sel.options || [])
    .some((o) => o.text === "(no workflow)"));
  check("at the cap the workflow picker is still filled",
    capWf && (capWf.options || []).some((o) => o.value === "improv-worker"),
    capWf && (capWf.options || []).map((o) => o.value));
  check("...and the role's own default is still auto-picked",
    capWf && capWf.value === "improv-worker", capWf && capWf.value);
  const capOverRow = walk(modalEls["modal-body"]).find((n) =>
    n.tag === "div" && n.classes.has("sess-spawn-row") &&
    texts(n).includes("spawn over the child limit"));
  check("the over-limit row is on offer at the cap",
    capOverRow && capOverRow.hidden === false, capOverRow && capOverRow.hidden);
  check("the note quotes the cap and names the crossing",
    texts(modalEls["modal-body"]).includes(CAP) &&
      texts(modalEls["modal-body"]).includes("tick 'spawn over the child limit'"),
    texts(modalEls["modal-body"]).slice(-200));
  const capActs = buttons(modalEls["modal-actions"]);
  const capBtn = capActs.find((b) => b.text.startsWith("Spawn"));
  check("the button stays dead until the cap is crossed",
    capBtn && capBtn.disabled === true);
  const capBox = capOverRow && capOverRow._find((k) => k.tag === "input");
  if (capBox) { capBox.checked = true; await capBox.fire("change"); }
  check("...and the crossing arms it", capBtn && capBtn.disabled === false);
  await capBtn.fire("click");
  await settle();
  const capPost = sent.find((x) => x.method === "POST");
  check("the crossing travels as over_limit",
    capPost && capPost.body.over_limit === true, capPost && capPost.body);

  /* ---- a parent whose own record never arrived is not guessed at ---------
     The workflow and git questions are both about where the CHILD will
     stand, and the daemon resolves an absent cwd to its OWN directory
     (api.py h_cflow_workflows) — so asking with "" does not fail, it
     succeeds about the wrong directory and fills the pickers with workflows
     the child will never see. */
  routes["GET /api/sessions/lead1/meta"] = { throw: true };
  routes["GET /api/sessions/lead1/children"] = { doc: {
    can_spawn: true, children_remaining: 2, may_choose: [],
    spawnable_harnesses: [],
  } };
  ctx.setSessions([]);        // ...and the rail's cache has no row either
  sent = [];
  await ctx.openSpawnModal("lead1", {});
  await settle();
  await settle();
  check("an unknown parent is not asked about on the daemon's behalf",
    !sent.some((x) => /\/api\/(cflow\/workflows|git)\?cwd=$/.test(x.path)),
    sent.map((x) => x.path));
  check("...and the empty pickers say why",
    texts(modalEls["modal-body"]).includes("could not load"),
    texts(modalEls["modal-body"]).slice(-220));
  ctx.setSessions([{ name: "lead1", cwd: "C:/repo", harness: "claude", status: "idle" }]);
  routes["GET /api/sessions/lead1/meta"] = { doc: {
    session: { name: "lead1", cwd: "C:/repo", harness: "claude" },
    meshes: [{ mesh: "m0", handle: "lead1", role: "leader" }],
  } };

  /* ---- a denied parent is told before the button can be pressed ---------- */
  routes["GET /api/sessions/lead1/children"] = { doc: {
    can_spawn: false, blocked_by: ["depth limit reached (3/3)"],
  } };
  sent = [];
  await ctx.openSpawnModal("lead1", {});
  await settle();
  const acts2 = buttons(modalEls["modal-actions"]);
  const spawn2 = acts2.find((b) => b.text.startsWith("Spawn"));
  check("a blocked parent keeps the button dead", spawn2 && spawn2.disabled === true);
  check("...and quotes the policy", texts(modalEls["modal-body"]).includes("depth limit reached"));

  console.log("\nspawnmodal_check: " + (failures ? failures + " failing" : "all ok"));
  process.exitCode = failures ? 1 : 0;
}

/* the checker's own selector: the stub nodes have no live querySelectorAll,
   so the modal's selects are walked with the same walk() the harness drives. */
function nodeSel(root, tag) { return tags(root, tag); }

main();
