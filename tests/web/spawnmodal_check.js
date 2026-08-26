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
  + slice("spawnMeshNow") + slice("spawnWtFragment")
  + slice("spawnAutoWorktree") + slice("spawnAutoWorktreeHint")
  + slice("spawnWorkflowEntry") + slice("spawnWorkflowAdmits") + slice("spawnRankWorkflows")
  + slice("profileBorrowCapability") + slice("syncSpawnGates") + slice("syncSpawnBeads")
  + slice("spawnPayload")
  + slice("spawnReport") + slice("spawnPreflightNote")
  + slice("spawnHardBlocks") + slice("postSpawn")
  + slice("spawnMissingSources") + slice("spawnSourceNote")
  + slice("qjStamp") + slice("fillSpawnSelect") + slice("spawnRow") + slice("spawnSubRow")
  + slice("spawnCheckRow") + slice("spawnRadioGroup")
  + slice("refillSpawnWorkflows") + slice("spawnConnectNow")
  + slice("buildSpawnForm")
  + slice("spawnModalKey") + slice("spawnModalClose") + slice("openSpawnModal")
  + slice("spawnModalLoad") + slice("refreshSpawnConnect") + slice("spawnModalGo")
  + slice("refreshSpawnBeads") + slice("fillSpawnIssueOptions")
  + `
Object.assign(exports, {
  spawnPayload, syncSpawnGates, syncSpawnBeads, spawnRankWorkflows, spawnWorkflowAdmits,
  spawnWorkflowEntry, spawnAutoWorktree, spawnAutoWorktreeHint,
  spawnMeshNow, spawnRadioGroup,
  spawnRecall, saveSpawnRecall, buildSpawnForm, openSpawnModal, spawnModalClose,
  refillSpawnWorkflows, spawnConnectNow,
  spawnMissingSources, spawnSourceNote,
  refreshSpawnBeads, fillSpawnIssueOptions,
  setSessions: (a) => { sessionsCache = a; },
  setSess: (n) => { sessName = n; },
  isOpen: () => spawnModal !== null,
  spawnUi: () => (spawnModal ? spawnModal.ui : null),
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

/* The worktree row is the one control the brain does not drive as a plain
   value: it is a radio group, so the harness builds the REAL one (against the
   stub DOM) rather than a ctl() that would agree with anything. */
let wtGroupN = 0;
const wtGroup = (value = "", off = false) => {
  const g = ctx.spawnRadioGroup(`wt${wtGroupN++}`, [
    ["", "no worktree"], ["new", "new worktree"], ["existing", "existing worktree"],
  ]);
  g.value = value;
  if (off) g.disabled = true;
  return g;
};

/* The board row's radios, the same real group the worktree row uses — a
   ctl() would agree with any payload, and the three answers are exactly
   what the gates fold and unfold. */
let beadsGroupN = 0;
const beadsGroup = (value = "new") => {
  const g = ctx.spawnRadioGroup(`bd${beadsGroupN++}`, [
    ["new", "new issue"], ["existing", "an existing issue"], ["none", "no issue"],
  ]);
  g.value = value;
  return g;
};

function uiStub(over = {}) {
  return Object.assign({
    parent: { value: "lead1" }, parentSess: {}, parentMesh: "", report: {},
    git: { repo: true, worktrees: [] }, stamp: "20260824-210000",
    name: ctl(), role: ctl(), workflow: ctl(), context: ctl(), contextRow: ctl(),
    mesh: ctl(), handle: ctl(), task: ctl(), args: ctl(),
    profile: ctl(), borrow: ctl(),
    nullTok: ctl(), fork: ctl(), over: ctl(), overRow: ctl(),
    profileNote: ctl(), borrowNote: ctl(),
    nullNote: ctl(), forkNote: ctl(), argsNote: ctl(),
    workspace: ctl(), workspaceNote: ctl(),
    wtMode: wtGroup(), worktreeNote: ctl(), wtName: ctl(),
    wtPick: ctl(), wtPickRow: ctl(),
    update: ctl(), rebase: ctl(), wtRow: ctl(), wtNameRow: ctl(),
    updateRow: ctl(), rebaseRow: ctl(),
    handleRow: ctl(), connectRow: ctl(), connectHandles: [],
    meshNote: ctl(), parentMeshes: [],
    beads: beadsGroup(), issueText: ctl(), issueTextRow: ctl({ hidden: true }),
    issuePick: ctl(), issueRow: ctl({ hidden: true }), issueHint: ctl({ hidden: true }),
    _issues: [], _issuesFor: null, _issuesError: "", _issuesRead: false,
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

  /* ---- the pair: a child's run comes from its PARENT, not its role ------
     The role still ranks the list; what is preselected is the pair the
     parent's own run declares (`child_cflow`). Reading the child's run off
     its role is what handed a worker-role child the worker flow under a
     parent driving something else entirely. */
  const WFS = [
    { name: "improv-worker", default_role: "worker", priority: 5 },
    { name: "worker-flow" },
  ];
  const pairUi = uiStub({
    workflow: node("select"), _wfs: WFS,
    report: { child_cflow: "worker-flow" },
  });
  ctx.refillSpawnWorkflows(pairUi, "worker", "");
  check("the parent's pair is preselected, not the role's default",
    pairUi.workflow.value === "worker-flow", pairUi.workflow.value);
  check("...over a list the role still ranks",
    pairUi.workflow.options[1].value === "improv-worker",
    pairUi.workflow.options.map((o) => o.value));

  const loneUi = uiStub({ workflow: node("select"), _wfs: WFS, report: {} });
  ctx.refillSpawnWorkflows(loneUi, "worker", "");
  check("a parent that pairs with nothing preselects nothing",
    loneUi.workflow.value === "", loneUi.workflow.value);

  const pickedUi = uiStub({
    workflow: node("select"), _wfs: WFS,
    report: { child_cflow: "worker-flow" },
  });
  ctx.refillSpawnWorkflows(pickedUi, "worker", "");
  pickedUi.workflow.value = "improv-worker";          // the operator picks
  ctx.refillSpawnWorkflows(pickedUi, "leader", "worker-flow");
  check("a pick of the operator's survives a role change",
    pickedUi.workflow.value === "improv-worker", pickedUi.workflow.value);

  /* An emptied row is not silence: the daemon reads an absent workflow as
     "give the child my pair", so a cleared row has to say no out loud. */
  const cleared = uiStub({
    report: { child_cflow: "worker-flow", may_choose: [] },
    wtMode: wtGroup(), wtRow: ctl({ hidden: true }),
    rebaseRow: ctl({ hidden: true }), overRow: ctl({ hidden: true }),
  });
  check("clearing the row over a pair travels as a refusal",
    ctx.spawnPayload(cleared).workflow === "-", ctx.spawnPayload(cleared));
  const clearedNoPair = uiStub({
    report: { may_choose: [] },
    wtMode: wtGroup(), wtRow: ctl({ hidden: true }),
    rebaseRow: ctl({ hidden: true }), overRow: ctl({ hidden: true }),
  });
  check("...but with no pair there is nothing to refuse",
    ctx.spawnPayload(clearedNoPair).workflow === undefined,
    ctx.spawnPayload(clearedNoPair));

  /* The auto name carries BOTH sessions. It used to be `<child or parent>-
     <stamp>`, so an unnamed child -- the quick job's every child -- was named
     after its PARENT, and a fleet of them left a repository full of
     `lead1-<stamp>` checkouts that no longer said whose each one was. */
  const auto = ctx.spawnAutoWorktree(uiStub({ name: ctl({ value: "w7" }) }));
  check("the auto name is parent-child-stamped",
    auto === "lead1-w7-20260824-210000", auto);
  const autoParent = ctx.spawnAutoWorktree(uiStub());
  check("...and is EMPTY when the child has no name yet — only the daemon " +
    "can finish it", autoParent === "", autoParent);
  check("...whose hint names the hole instead of promising a string",
    ctx.spawnAutoWorktreeHint(uiStub()) ===
      "lead1-<the child's name>-<time cut>",
    ctx.spawnAutoWorktreeHint(uiStub()));
  const odd = ctx.spawnAutoWorktree(uiStub({
    name: ctl({ value: "w 1" }), parent: ctl({ value: "lead/x" }),
  }));
  check("...and a session name git would refuse is reduced to one it takes",
    odd === "lead-x-w-1-20260824-210000", odd);
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
    wtMode: wtGroup("new"), wtName: ctl({ value: "my-wt" }),
    wtPick: ctl(), wtRow: ctl({ hidden: false }), rebaseRow: ctl({ hidden: true }),
    update: ctl(), rebase: ctl(),
    overRow: ctl({ hidden: true }), over: ctl({ checked: true }),
  });
  const body = ctx.spawnPayload(full);
  check("payload trims the name", body.name === "c7", body);
  check("payload spells the CLI keys", body.role === "worker" && body.workflow === "improv-worker");
  check("payload sends mesh and handle", body.mesh === "m0" && body.handle === "c7");
  check("payload sends the connect list", body.connect && body.connect.join(",") === "w2", body.connect);
  check("payload splits args", body.args.join(" ") === "--verbose --json", body.args);
  check("a named new worktree sends that name", body.worktree === "my-wt", body.worktree);
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
    wtMode: wtGroup("new", true), wtRow: ctl({ hidden: false }),
    overRow: ctl({ hidden: true }), over: ctl({ checked: true }),
    mesh: ctl({ value: "-" }), role: ctl({ value: "" }),
  });
  const greyBody = ctx.spawnPayload(greyed);
  check("a greyed profile is not sent", greyBody.profile === undefined);
  check("a greyed --null is not sent", greyBody.null_token === undefined);
  check("a greyed fork is not sent", greyBody.fork === undefined);
  check("a greyed worktree is not sent", greyBody.worktree === undefined);
  check("'-' mesh travels as none, without a handle",
    greyBody.mesh === "-" && greyBody.handle === undefined, greyBody);
  check("only the '-' mesh travels from an all-greyed form",
    Object.keys(greyBody).join(",") === "mesh", Object.keys(greyBody));

  /* ---- the board row: three answers, one key each in the payload ---------
     The same contract as the create form's #new-beads: "new" with the box
     empty sends nothing (the daemon mints from the task), "existing"
     adopts by id, "none" is an explicit refusal, and the keys never mix —
     issue_text beside "existing" or "none" is a contradiction the daemon
     refuses (beads.check_request). */
  const bdNew = uiStub({
    beads: beadsGroup("new"), issueText: ctl({ value: "  find the bug  " }),
  });
  const bdNewBody = ctx.spawnPayload(bdNew);
  check("'new' with a filled box writes the issue text",
    bdNewBody.issue_text === "find the bug", bdNewBody);
  check("...and nothing else board-shaped",
    bdNewBody.issue === undefined && !("beads" in bdNewBody), bdNewBody);
  const bdEmpty = uiStub({ beads: beadsGroup("new") });
  check("'new' with an empty box sends nothing — the task mints",
    ctx.spawnPayload(bdEmpty).issue_text === undefined, ctx.spawnPayload(bdEmpty));
  const bdPick = uiStub({
    beads: beadsGroup("existing"), issuePick: ctl({ value: "claunch-2lb" }),
  });
  const bdPickBody = ctx.spawnPayload(bdPick);
  check("'existing' with a pick adopts it by id",
    bdPickBody.issue === "claunch-2lb", bdPickBody);
  check("...and never carries a minting text",
    bdPickBody.issue_text === undefined, bdPickBody);
  const bdNone = uiStub({ beads: beadsGroup("none") });
  check("'none' travels as an explicit refusal",
    ctx.spawnPayload(bdNone).beads === false, ctx.spawnPayload(bdNone));
  const bdUnpicked = uiStub({ beads: beadsGroup("existing") });
  check("'existing' with nothing picked sends nothing",
    ctx.spawnPayload(bdUnpicked).issue === undefined, ctx.spawnPayload(bdUnpicked));

  /* ---- the board row's two detail rows follow the picked answer ---------- */
  const sg = uiStub({});
  ctx.syncSpawnBeads(sg);
  check("'new' shows the box and folds the picker",
    sg.issueTextRow.hidden === false && sg.issueRow.hidden === true);
  sg.beads.value = "existing";
  ctx.syncSpawnBeads(sg);
  check("'existing' shows the picker and folds the box",
    sg.issueTextRow.hidden === true && sg.issueRow.hidden === false);
  sg.beads.value = "none";
  ctx.syncSpawnBeads(sg);
  check("'none' folds both",
    sg.issueTextRow.hidden === true && sg.issueRow.hidden === true);
  sg.issueText.value = "the spec";
  sg.beads.value = "new";
  ctx.syncSpawnBeads(sg);
  check("the box keeps its words across the other answers",
    sg.issueText.value === "the spec", sg.issueText.value);

  /* The hint speaks only the consequence the row cannot show. */
  const held = uiStub({
    beads: beadsGroup("existing"), issuePick: ctl({ value: "k1" }),
    _issues: [{ id: "k1", status: "in_progress", held_by: "w9" }],
    _issuesRead: true,
  });
  ctx.syncSpawnBeads(held);
  check("a held issue is spelled out as a JOIN",
    held.issueHint.hidden === false && /JOINS it/.test(held.issueHint.textContent),
    held.issueHint.textContent);
  const vacant = uiStub({
    beads: beadsGroup("existing"), _issues: [], _issuesRead: true,
  });
  ctx.syncSpawnBeads(vacant);
  check("an answered-but-empty board says so",
    vacant.issueHint.hidden === false &&
      /no open issue on this directory/.test(vacant.issueHint.textContent),
    vacant.issueHint.textContent);
  const awaiting = uiStub({
    beads: beadsGroup("existing"), _issues: [], _issuesRead: false,
  });
  ctx.syncSpawnBeads(awaiting);
  check("an unanswered board stays quiet — in flight is not 'nothing'",
    awaiting.issueHint.hidden === true, awaiting.issueHint.textContent);
  const errBoard = uiStub({
    beads: beadsGroup("existing"), _issuesRead: true,
    _issuesError: "no board in this directory",
  });
  ctx.syncSpawnBeads(errBoard);
  check("the board's own error is the message, not the empty list",
    errBoard.issueHint.hidden === false &&
      errBoard.issueHint.textContent === "no board in this directory",
    errBoard.issueHint.textContent);
  const noRow = uiStub({});
  delete noRow.beads;
  ctx.syncSpawnGates(noRow);
  ctx.syncSpawnBeads(noRow);
  check("a bag without the row passes the gates untouched", true);

  /* ---- the brain: gates -------------------------------------------------- */
  const g = uiStub({
    report: { may_choose: [], spawnable_harnesses: [], workspaces: null },
    harness: ctl({ value: "" }), parentSess: { harness: "claude" },
    git: { repo: true }, over: ctl(), update: ctl(),
  });
  ctx.syncSpawnGates(g);
  check("locked profile names its key",
    g.profile.disabled === true && /spawn\.allow_profile/.test(g.profileNote.textContent));
  check("no workspaces list locks the directory row",
    g.workspace.disabled === true && /spawn\.allow_workspace/.test(g.workspaceNote.textContent));
  check("worktree not in may_choose locks every mode",
    g.wtMode.disabled === true &&
      Object.values(g.wtMode.inputs).every((i) => i.disabled === true) &&
      /spawn\.allow_worktree/.test(g.worktreeNote.textContent),
    g.worktreeNote.textContent);

  const g2 = uiStub({
    report: { may_choose: ["worktree", "profile", "fork"], workspaces: [] },
    harness: ctl({ value: "claude" }), parentSess: { harness: "claude" },
    git: { repo: true, worktrees: ["tan"] }, wtMode: wtGroup("existing"),
    wtPick: ctl({ value: "tan" }), update: ctl({ checked: true }), fork: ctl(),
  });
  ctx.syncSpawnGates(g2);
  check("a choosable worktree stays open", g2.wtMode.disabled === false);
  check("reuse folds out its picker and hides the name field",
    g2.wtPickRow.hidden === false && g2.wtNameRow.hidden === true,
    [g2.wtPickRow.hidden, g2.wtNameRow.hidden]);
  const gRepo = uiStub({
    report: { may_choose: ["worktree"] }, git: { repo: false },
    harness: ctl({ value: "claude" }), parentSess: { harness: "claude" },
    update: ctl(), over: ctl(),
  });
  ctx.syncSpawnGates(gRepo);
  check("a non-repo directory takes the worktree rows away", gRepo.wtRow.hidden === true);
  check("a reused checkout offers rebase", g2.updateRow.hidden === false && g2.rebaseRow.hidden === false);
  check("a worktree of its own denies the fork",
    g2.fork.disabled === true && /worktree of its own/.test(g2.forkNote.textContent),
    g2.forkNote.textContent);

  /* ---- the worktree row: three modes, each with its own detail -----------
     The bug this pins: one <select> used to hold "(no worktree)", "@auto",
     "@named" and every checkout the repository has — four kinds of answer in
     one list, so the mode and the name were the same question. The modes are
     radios now, and each one's detail is folded under it; what must hold is
     that only the picked mode's rows are on screen and only its answer is in
     the payload. */
  const wtCase = (over = {}) => {
    const u = uiStub(Object.assign({
      report: { may_choose: ["worktree"], workspaces: [] },
      harness: ctl({ value: "claude" }), parentSess: { harness: "claude" },
      git: { repo: true, worktrees: ["tan", "oak"] },
      wtRow: ctl({ hidden: false }), over: ctl(), update: ctl(),
    }, over));
    ctx.syncSpawnGates(u);
    return u;
  };

  const none = wtCase({ wtMode: wtGroup("") });
  check("no worktree folds every detail away",
    none.wtNameRow.hidden === true && none.wtPickRow.hidden === true &&
      none.updateRow.hidden === true && none.rebaseRow.hidden === true,
    [none.wtNameRow.hidden, none.wtPickRow.hidden, none.updateRow.hidden]);
  check("...and sends no worktree at all",
    ctx.spawnPayload(none).worktree === undefined);

  const fresh = wtCase({ wtMode: wtGroup("new"), name: ctl({ value: "w7" }) });
  check("new worktree asks for a name and nothing else",
    fresh.wtNameRow.hidden === false && fresh.wtPickRow.hidden === true &&
      fresh.updateRow.hidden === true,
    [fresh.wtNameRow.hidden, fresh.wtPickRow.hidden]);
  // The generated name is READ on the form, not discovered by pressing Spawn
  // -- and it can be, because THIS child is named on this form.
  check("...spelling the generated name into the blank field's placeholder",
    fresh.wtName.placeholder === "blank = lead1-w7-20260824-210000",
    fresh.wtName.placeholder);
  check("...and a blank name sends that generated one",
    ctx.spawnPayload(fresh).worktree === "lead1-w7-20260824-210000",
    ctx.spawnPayload(fresh).worktree);

  /* The quick job's own case: a task is typed and nothing else, so the child
     has no name here and the name cannot be finished on this side. It travels
     as `true` -- "one of its own, you name it" -- and the daemon, which picks
     the child's `sN`, completes it. Sending a guess instead is what named
     every quick job's checkout after the leader that dispatched it. */
  const unnamed = wtCase({ wtMode: wtGroup("new") });
  check("an unnamed child hands the naming to the daemon",
    ctx.spawnPayload(unnamed).worktree === true,
    ctx.spawnPayload(unnamed).worktree);
  check("...and never as the string 'true', which would cut a worktree " +
    "called True", typeof ctx.spawnPayload(unnamed).worktree === "boolean");
  check("...with a placeholder that says so rather than naming a checkout " +
    "nobody will find",
    unnamed.wtName.placeholder ===
      "blank = lead1-<the child's name>-<time cut>",
    unnamed.wtName.placeholder);
  const typed = wtCase({
    wtMode: wtGroup("new"), wtName: ctl({ value: "my-own" }),
  });
  check("a typed name is still the whole name",
    ctx.spawnPayload(typed).worktree === "my-own",
    ctx.spawnPayload(typed).worktree);

  const reuse = wtCase({
    wtMode: wtGroup("existing"), wtPick: ctl({ value: "oak" }),
    wtName: ctl({ value: "typed-then-abandoned" }),
    update: ctl({ checked: true }), rebase: ctl({ value: "master" }),
  });
  ctx.syncSpawnGates(reuse);   // the update tick opens the rebase row
  check("existing sends the checkout it names",
    ctx.spawnPayload(reuse).worktree === "oak", ctx.spawnPayload(reuse).worktree);
  check("...carrying the rebase the catch-up opened",
    ctx.spawnPayload(reuse).rebase_onto === "master");
  // A name typed under 'new' and then abandoned is not an answer either: the
  // mode decides which field is read, which is the point of splitting them.
  check("...and not the name left in the other mode's field",
    ctx.spawnPayload(reuse).worktree !== "typed-then-abandoned");

  // Nothing to reuse: the mode is greyed rather than offered over an empty
  // picker, and a greyed mode cannot stay the answer.
  const bare = wtCase({
    wtMode: wtGroup("existing"), git: { repo: true, worktrees: [] },
  });
  check("a repository with no checkouts greys the reuse mode",
    bare.wtMode.inputs.existing.disabled === true &&
      bare.wtMode.inputs.new.disabled === false,
    [bare.wtMode.inputs.existing.disabled, bare.wtMode.inputs.new.disabled]);
  check("...and drops it as the answer rather than leaving it stuck",
    bare.wtMode.value === "" && ctx.spawnPayload(bare).worktree === undefined,
    bare.wtMode.value);
  const grown = wtCase({
    wtMode: wtGroup(""), git: { repo: true, worktrees: ["tan"] },
  });
  check("...and it comes back live once there is one",
    grown.wtMode.inputs.existing.disabled === false);

  // Reuse with nothing picked is not a silent fall back to a fresh checkout.
  const unpicked = wtCase({ wtMode: wtGroup("existing"), wtPick: ctl({ value: "" }) });
  check("reuse with no checkout picked sends nothing",
    ctx.spawnPayload(unpicked).worktree === undefined,
    ctx.spawnPayload(unpicked));

  // The group answers to `.value` and `.disabled` the way the <select> did,
  // so the gates and the payload never learn which widget they are driving.
  const grp = wtGroup("");
  check("the group opens on the harmless first answer", grp.value === "");
  grp.value = "existing";
  check("setting the value checks that one and unchecks the rest",
    grp.inputs.existing.checked === true && grp.inputs.new.checked === false);
  grp.value = "nonsense";
  check("an unknown answer lands on the harmless first one", grp.value === "");
  let heard = 0;
  grp.listen(() => { heard++; });
  // The browser unchecks the siblings itself (they share a name); the group's
  // own handler repeats it, so a stub DOM — and a node that drifted out of the
  // group — read the same. One handler covers all three buttons.
  grp.inputs.new.checked = true;
  await grp.inputs.new.fire("change");
  check("a click is the group's value, with the siblings unchecked",
    grp.value === "new" && grp.inputs[""].checked === false && heard === 1,
    [grp.value, grp.inputs[""].checked, heard]);

  const g3 = uiStub({
    report: { may_choose: ["fork", "borrow"] }, git: {},
    harness: ctl({ value: "" }), profile: ctl({ value: "pi-profile" }),
    profileDetails: { "pi-profile": { harness: "pi", borrow_allowed: true,
                                       borrow_mode: "token" } },
    parentSess: { harness: "claude" },
    nullTok: ctl(), borrow: ctl(), wtMode: wtGroup(""),
    over: ctl(), update: ctl(),
    mesh: ctl({ value: "-" }), connectHandles: ["a"],
  });
  ctx.syncSpawnGates(g3);
  check("a non-claude child cannot --null",
    g3.nullTok.disabled === true && /claude harness only/.test(g3.nullNote.textContent));
  check("...but its declared API-key route can borrow a token",
    g3.borrow.disabled === false, g3.borrowNote.textContent);
  check("mesh '-' takes handle and connect away",
    g3.handleRow.hidden === true && g3.connectRow.hidden === true);

  const g4 = uiStub({
    report: { may_choose: ["borrow"] }, git: {},
    harness: ctl({ value: "" }), profile: ctl({ value: "kimi-profile" }),
    profileDetails: { "kimi-profile": { harness: "kimi", borrow_allowed: false,
                                         borrow_mode: "none" } },
    parentSess: { harness: "claude" }, nullTok: ctl(), borrow: ctl({ value: "p2" }),
    wtMode: wtGroup(""), over: ctl(), update: ctl(), mesh: ctl({ value: "-" }),
  });
  ctx.syncSpawnGates(g4);
  check("an OAuth child clears and locks an inherited borrow",
    g4.borrow.disabled === true && g4.borrow.value === "" &&
      /own profile storage/.test(g4.borrowNote.textContent),
    [g4.borrow.value, g4.borrowNote.textContent]);

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
                   "profile", "borrow", "args", "workspace", "wtMode",
                   "wtPick", "wtName", "update", "rebase", "fork", "over",
                   "beads", "issueText", "issueTextRow", "issuePick", "issueRow",
                   "issueHint"]) {
    check(`form builds ${k}`, bui[k] && typeof bui[k] === "object", k);
  }
  check("the qualified Profile : Harness picker has no duplicate harness control",
    bui.harness === undefined, bui.harness);
  check("the board row opens on 'new'", bui.beads.value === "new", bui.beads.value);
  check("...with its two detail rows folded until the gates run",
    bui.issueTextRow.hidden === true && bui.issueRow.hidden === true,
    [bui.issueTextRow.hidden, bui.issueRow.hidden]);
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
    "GET /api/profiles": { doc: {
      profiles: ["p1", "p2"],
      profile_selectors: ["p1:claude", "p1:pi", "p2:claude", "p2:pi"],
      profile_details: [
        { name: "p1:claude", harness: "claude", harness_available: true,
          borrow_allowed: true, borrow_mode: "provider-token" },
        { name: "p1:pi", harness: "pi", harness_available: true,
          borrow_allowed: true, borrow_mode: "token" },
        { name: "p2:claude", harness: "claude", harness_available: true,
          borrow_allowed: true, borrow_mode: "provider-token" },
        { name: "p2:pi", harness: "pi", harness_available: true,
          borrow_allowed: true, borrow_mode: "token" },
      ],
    } },
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
  // Under the cap the gate is not merely empty, it is gone: an action bar
  // carrying a folded warning block is a bar that has grown a dead region.
  const openGate = walk(modalEls["modal-actions"]).find((n) =>
    n.classes && n.classes.has("sess-spawn-cap"));
  check("under the cap the gate is folded",
    openGate && openGate.hidden === true, openGate && openGate.hidden);
  const mSel = nodeSel(modalEls["modal-body"], "select") || [];
  // Inherit is the default, not merely an option — the row opens the way
  // Harness, Profile and Directory do. Naming the parent's mesh outright is
  // the same answer only while the parent is in ONE mesh, and spelling it into
  // the payload takes the rule away from daemon/onboard.py inherit_mesh.
  const meshSel = mSel.find((s) => (s.options || [])
    .some((o) => o.text === "(inherit the parent's mesh)"));
  const profileSel = mSel.find((s) => (s.options || [])
    .some((o) => o.value === "p1:pi"));
  const borrowSel = mSel.find((s) => (s.options || [])
    .some((o) => o.value === "p1") && !(s.options || [])
      .some((o) => o.value === "p1:pi"));
  check("profile choices are qualified execution selectors",
    profileSel && (profileSel.options || []).some((o) => o.value === "p2:pi"));
  check("borrow choices remain base profiles sharing the one token",
    borrowSel && (borrowSel.options || []).some((o) => o.value === "p2"));
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
  /* The quick job types a task and nothing else, so nothing on this side
     knows which session gets the checkout. It used to send `lead1-<stamp>`
     -- the LEADER's name -- and a fleet of quick jobs then sat in a rail of
     `lead1-...` worktrees that no longer said whose each one was. It now
     asks the daemon, which knows the child's `sN` by the time it cuts. */
  check("the quick job hands the worktree's name to the daemon",
    post && post.body.worktree === true, post && post.body);
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

  /* ---- the seed still speaks the API's language, the form speaks modes ---
     `worktree: true` and `worktree: "<name>"` are what the quick job and the
     saved defaults carry, and the daemon takes the same. The modal is where
     that becomes a MODE, and the rule is the repository's own list: a name it
     has is a reuse, a name it does not have is a new checkout carrying that
     name. Getting this backwards would open the wizard on "new" over a name
     that already exists — and cut a second checkout of it on Spawn. */
  const goodGit = routes["GET /api/git?cwd=C%3A%2Frepo"];
  routes["GET /api/git?cwd=C%3A%2Frepo"] =
    { doc: { repo: true, worktrees: ["tan", "oak"] } };

  await ctx.openSpawnModal("lead1", { seed: { worktree: "oak" } });
  await settle();
  await settle();
  const wui = ctx.spawnUi();
  // Read the group through the bag, not the DOM: the board row added a
  // second triplet of radios, so a page-wide count would no longer isolate
  // the worktree row.
  const wtRadios = Object.keys(wui.wtMode.inputs).map((k) => wui.wtMode.inputs[k].value);
  check("the row is three radios, not a list of four kinds of answer",
    wtRadios.length === 3 &&
      JSON.stringify(wtRadios) === JSON.stringify(["", "new", "existing"]),
    wtRadios);
  check("a seeded name the repository has opens on reuse",
    wui.wtMode.value === "existing" && wui.wtPick.value === "oak",
    [wui.wtMode.value, wui.wtPick.value]);
  check("...with the reuse rows out and the name row folded away",
    wui.wtPickRow.hidden === false && wui.updateRow.hidden === false &&
      wui.wtNameRow.hidden === true,
    [wui.wtPickRow.hidden, wui.updateRow.hidden, wui.wtNameRow.hidden]);
  // Switching mode on the form re-folds the detail without a reload.
  wui.wtMode.inputs.new.checked = true;
  await wui.wtMode.inputs.new.fire("change");
  check("picking new folds the reuse rows away and asks for a name",
    wui.wtNameRow.hidden === false && wui.wtPickRow.hidden === true &&
      wui.updateRow.hidden === true && wui.rebaseRow.hidden === true,
    [wui.wtNameRow.hidden, wui.wtPickRow.hidden, wui.updateRow.hidden]);
  const acts3 = buttons(modalEls["modal-actions"]);
  const spawn3 = acts3.find((b) => b.text.startsWith("Spawn"));
  sent = [];
  await spawn3.fire("click");
  await settle();
  const newPost = sent.find((x) => x.method === "POST");
  check("...and Spawn cuts a generated name, not the checkout it left",
    newPost && newPost.body.worktree === true,
    newPost && newPost.body.worktree);

  await ctx.openSpawnModal("lead1", { seed: { worktree: "s45-fresh" } });
  await settle();
  await settle();
  const wui2 = ctx.spawnUi();
  check("a seeded name the repository does NOT have opens on new",
    wui2.wtMode.value === "new" && wui2.wtName.value === "s45-fresh",
    [wui2.wtMode.value, wui2.wtName.value]);

  await ctx.openSpawnModal("lead1", { seed: { worktree: true } });
  await settle();
  await settle();
  check("`true` opens on new with the name left blank",
    ctx.spawnUi().wtMode.value === "new" && ctx.spawnUi().wtName.value === "",
    ctx.spawnUi().wtName.value);

  await ctx.openSpawnModal("lead1", {});
  await settle();
  await settle();
  check("no seed opens on no worktree at all",
    ctx.spawnUi().wtMode.value === "" && ctx.spawnUi().wtPickRow.hidden === true);
  routes["GET /api/git?cwd=C%3A%2Frepo"] = goodGit;

  /* ---- the board row: the daemon's verdicts fill the picker --------------
     "existing" is the one answer that reads the board, and only then — a
     board read costs the daemon a `br` fork, so opening the modal on the
     default 'new' must cost nothing. The picker rows carry the daemon's
     adoption verdict, the same promise the create form makes. */
  routes["GET /api/beads/candidates"] = { doc: {
    issues: [
      { id: "claunch-aaa", title: "fix it", status: "open", held_by: null },
      { id: "claunch-bbb", title: "do it", status: "in_progress", held_by: "w2" },
    ],
  } };
  sent = [];
  await ctx.openSpawnModal("lead1", {});
  await settle();
  await settle();
  const bui2 = ctx.spawnUi();
  check("the board row opens on 'new' with the box out",
    bui2.beads.value === "new" && bui2.issueTextRow.hidden === false &&
      bui2.issueRow.hidden === true,
    [bui2.beads.value, bui2.issueTextRow.hidden, bui2.issueRow.hidden]);
  check("...and nobody pays for a board read the answer never looks at",
    !sent.some((s) => s.path.startsWith("/api/beads")),
    sent.filter((s) => s.path.startsWith("/api/beads")));
  bui2.beads.inputs.existing.checked = true;
  await bui2.beads.inputs.existing.fire("change");
  await settle();
  const fetchB = sent.find((s) => s.path.startsWith("/api/beads/candidates"));
  check("'existing' asks the board of the parent's directory",
    fetchB && fetchB.path === "/api/beads/candidates?parent=lead1",
    fetchB && fetchB.path);
  check("...and opens the picker over the daemon's verdicts",
    bui2.issueRow.hidden === false &&
      (bui2.issuePick.options || []).some((o) => o.value === "claunch-aaa") &&
      (bui2.issuePick.options || []).some((o) => /held by w2/.test(o.text)),
    (bui2.issuePick.options || []).map((o) => o.text));
  bui2.issuePick.value = "claunch-bbb";
  await bui2.issuePick.fire("change");
  check("picking the held issue says 'JOINS'",
    bui2.issueHint.hidden === false && /JOINS it/.test(bui2.issueHint.textContent),
    bui2.issueHint.textContent);
  const actsB = buttons(modalEls["modal-actions"]);
  const spawnB = actsB.find((b) => b.text.startsWith("Spawn"));
  sent = [];
  await spawnB.fire("click");
  await settle();
  const bdPost = sent.find((s) => s.method === "POST");
  check("a picked issue travels as the adopt key",
    bdPost && bdPost.body.issue === "claunch-bbb", bdPost && bdPost.body);

  /* The minting box and the refusal ride the same three modes. */
  await ctx.openSpawnModal("lead1", { seed: { task: "fix the tab" } });
  await settle();
  await settle();
  const bui3 = ctx.spawnUi();
  bui3.issueText.value = "the spec lives here";
  const actsC = buttons(modalEls["modal-actions"]);
  const spawnC = actsC.find((b) => b.text.startsWith("Spawn"));
  sent = [];
  await spawnC.fire("click");
  await settle();
  const textPost = sent.find((s) => s.method === "POST");
  check("'new' with a filled box writes a separate issue",
    textPost && textPost.body.issue_text === "the spec lives here" &&
      textPost.body.task === "fix the tab", textPost && textPost.body);
  check("'new' never also adopts",
    textPost && textPost.body.issue === undefined, textPost && textPost.body);

  await ctx.openSpawnModal("lead1", {});
  await settle();
  await settle();
  const bui4 = ctx.spawnUi();
  bui4.beads.inputs.none.checked = true;
  await bui4.beads.inputs.none.fire("change");
  const actsD = buttons(modalEls["modal-actions"]);
  const spawnD = actsD.find((b) => b.text.startsWith("Spawn"));
  sent = [];
  await spawnD.fire("click");
  await settle();
  const nonePost = sent.find((s) => s.method === "POST");
  check("'none' travels as an explicit refusal",
    nonePost && nonePost.body.beads === false &&
      nonePost.body.issue === undefined && nonePost.body.issue_text === undefined,
    nonePost && nonePost.body);

  /* A workspace the operator aimed the child at moves the board question
     with it — the bear-trap of asking the parent's board in a modal set to
     open on it. */
  routes["GET /api/sessions/lead1/children"] = { doc: {
    can_spawn: true, children_remaining: 3,
    may_choose: ["workspace"], spawnable_harnesses: [],
    workspaces: [{ name: "wsx", path: "C:/other", exists: true }],
  } };
  sent = [];
  await ctx.openSpawnModal("lead1", {});
  await settle();
  await settle();
  const wui3 = ctx.spawnUi();
  wui3.workspace.value = "wsx";
  await wui3.workspace.fire("change");
  wui3.beads.inputs.existing.checked = true;
  await wui3.beads.inputs.existing.fire("change");
  await settle();
  const wsFetch = sent.find((s) => s.path.startsWith("/api/beads/candidates"));
  check("a workspace pick asks that board directly",
    wsFetch && wsFetch.path === "/api/beads/candidates?cwd=C%3A%2Fother",
    wsFetch && wsFetch.path);
  sent = [];
  const actsE = buttons(modalEls["modal-actions"]);
  const spawnE = actsE.find((b) => b.text.startsWith("Spawn"));
  await spawnE.fire("click");
  await settle();
  const wsPost = sent.find((s) => s.method === "POST");
  check("the aimed workspace travels with the child",
    wsPost && wsPost.body.workspace === "wsx", wsPost && wsPost.body);
  routes["GET /api/sessions/lead1/children"] = { doc: {
    can_spawn: true, children_remaining: 3,
    may_choose: ["profile", "args", "worktree", "fork", "borrow"],
    spawnable_harnesses: ["claude"], workspaces: null,
  } };

  /* ---- the child cap is a warning, not a dead end -----------------------
     The cap does not refuse any more: spawn.py names it in `soft_blocked_by`
     alone, `can_spawn` stays true and `blocked_by` is empty. What is checked
     here is that the modal still SAYS it — a fifth child that appears with
     no sentence about the cap is the other way to get this wrong — and that
     the load is not stopped by it. Reading the verdict alone used to stop
     the load dead: no Workflow picker, no Over-limit row, a modal that said
     "child limit reached" over an empty dropdown. The cap is exactly what a
     busy fleet runs into, which is how "no workflows in the spawn modal"
     kept coming back. */
  const CAP = "child limit reached (4 running/4)";
  routes["GET /api/sessions/lead1/children"] = { doc: {
    can_spawn: true, blocked_by: [], soft_blocked_by: [CAP],
    children_used: 4, children_remaining: 0,
    may_choose: ["worktree"], spawnable_harnesses: [],
    child_cflow: "improv-worker",
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
  check("...and the parent's pair is still auto-picked",
    capWf && capWf.value === "improv-worker", capWf && capWf.value);
  /* The layout of the cap, which is the half of it that kept failing in
     practice. The reason used to be printed at the top of the form, the
     crossing was the form's last row, and the button they explain lives in
     the action bar outside the form's scroller — three places, twenty rows
     apart. All three are now one glance: the gate holds reason AND crossing
     and hangs in the bar, above the button. */
  const capGate = walk(modalEls["modal-actions"]).find((n) =>
    n.classes && n.classes.has("sess-spawn-cap"));
  check("the cap gate is in the action bar, beside the button",
    capGate && capGate.hidden === false, capGate && capGate.hidden);
  check("...and holds the reason and the crossing together",
    capGate && texts(capGate).includes(CAP) &&
      texts(capGate).includes("spawn over the child limit"),
    capGate && texts(capGate));
  const capOverRow = capGate && walk(capGate).find((n) =>
    n.tag === "div" && n.classes.has("sess-spawn-row") &&
    texts(n).includes("spawn over the child limit"));
  check("the over-limit row is on offer at the cap",
    capOverRow && capOverRow.hidden === false, capOverRow && capOverRow.hidden);
  check("...with the cost of crossing under it, not inside its label",
    capOverRow && texts(capOverRow).includes("the daemon counts it against you"),
    capOverRow && texts(capOverRow));
  check("the form's own note no longer repeats the cap",
    !texts(modalEls["modal-body"]).includes(CAP),
    texts(modalEls["modal-body"]).slice(-200));
  const capActs = buttons(modalEls["modal-actions"]);
  const capBtn = capActs.find((b) => b.text.startsWith("Spawn"));
  check("the cap leaves the button alive", capBtn && capBtn.disabled === false,
    capBtn && capBtn.disabled);
  check("...with nothing to explain away on it",
    capBtn && capBtn.title === "", capBtn && capBtn.title);
  const capBox = capOverRow && capOverRow._find((k) => k.tag === "input");
  check("the crossing is pre-answered yes", capBox && capBox.checked === true,
    capBox && capBox.checked);
  await capBtn.fire("click");
  await settle();
  const capPost = sent.find((x) => x.method === "POST");
  check("the crossing travels as over_limit",
    capPost && capPost.body.over_limit === true, capPost && capPost.body);

  /* Unticking it is how the strict reading is asked for, and that answer has
     to travel as a `false` — swallowed as falsy it would read as "did not
     say", which is the crossing, and the operator would get the child they
     just said no to. */
  sent = [];
  await ctx.openSpawnModal("lead1", { seed: { role: "worker" } });
  await settle();
  await settle();
  const strictGate = walk(modalEls["modal-actions"]).find((n) =>
    n.classes && n.classes.has("sess-spawn-cap"));
  const strictBox = strictGate && walk(strictGate).find((n) => n.tag === "input");
  if (strictBox) { strictBox.checked = false; await strictBox.fire("change"); }
  const strictBtn = buttons(modalEls["modal-actions"])
    .find((b) => b.text.startsWith("Spawn"));
  check("unticking leaves the button alive (the daemon answers, not the form)",
    strictBtn && strictBtn.disabled === false, strictBtn && strictBtn.disabled);
  await strictBtn.fire("click");
  await settle();
  const strictPost = sent.find((x) => x.method === "POST");
  check("...and the untick travels as a false",
    strictPost && strictPost.body.over_limit === false,
    strictPost && strictPost.body);

  /* A soft refusal that names nothing to cross. The gate still has to open —
     it is the only place the modal says why the button is dead now that the
     reason has left the form's top note — but it opens on the reason alone,
     with no box under it pretending there is a way through. */
  routes["GET /api/sessions/lead1/children"] = { doc: {
    can_spawn: false, blocked_by: [], soft_blocked_by: [],
    may_choose: ["worktree"], spawnable_harnesses: [],
  } };
  await ctx.openSpawnModal("lead1", {});
  await settle();
  await settle();
  const bareGate = walk(modalEls["modal-actions"]).find((n) =>
    n.classes && n.classes.has("sess-spawn-cap"));
  check("a crossing-less refusal still opens the gate on its reason",
    bareGate && bareGate.hidden === false &&
      texts(bareGate).includes("this session may not spawn"),
    bareGate && [bareGate.hidden, texts(bareGate)]);
  const bareRow = bareGate && walk(bareGate).find((n) =>
    n.tag === "div" && n.classes.has("sess-spawn-row"));
  check("...and offers no box there is no crossing for",
    bareRow && bareRow.hidden === true, bareRow && bareRow.hidden);

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
