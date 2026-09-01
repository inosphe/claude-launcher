/* Role-specific panels: the session rail grows verbs by ROLE — a leader gets
   a dispatch form and a children roster, everybody else gets neither. Four
   things must hold: the registry answers to either place a role is declared
   (definition or mesh membership) and never hands a panel out twice; the
   quick-job form spawns exactly what its pickers say (defaults from
   /api/quickjob, worktree stamped after the prefix) and refuses a taskless
   worker; the reaping nudge names the idle children and only ever TYPES a
   request into the leader (POST /deliver) — it kills nothing itself; and the
   workflow diagram is pinned at its natural size so a wider column cannot
   zoom it. Slice the real functions out of app.js and drive them against a
   stub DOM. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);
const css = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "style.css"),
  "utf8"
);

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
function sliceConst(decl) {
  const start = src.indexOf(decl);
  if (start < 0) throw new Error("missing " + decl);
  const end = src.indexOf("\n};", start);
  return src.slice(start, end + 3);
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, attrs: {}, kids: [], text: "", classes: new Set(), handlers: {},
    dataset: {}, value: undefined, disabled: false, placeholder: "", title: "",
    checked: false, href: "", rows: 0, isConnected: true,
    appendChild(c) {
      this.kids.push(c);
      if (this.tag === "select" && c.tag === "option" && this.value === undefined) {
        this.value = c.value;
      }
      return c;
    },
    append(...cs) { cs.forEach((c) => this.appendChild(c)); },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    fire(k, ev) { return Promise.all((this.handlers[k] || []).map((fn) => fn(ev))); },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) {
      this.classes = new Set(String(v).split(/\s+/).filter(Boolean));
    },
    get innerHTML() { return ""; },
    set innerHTML(v) { this.kids = []; },
  };
  // A real input/textarea reads "" before anybody types; only <select> starts
  // undefined, which is what lets the first <option> claim it above.
  if (tag === "input" || tag === "textarea") n.value = "";
  return n;
}
function walk(n, out = []) {
  for (const k of n.kids) { out.push(k); walk(k, out); }
  return out;
}
const texts = (n) => walk(n).map((k) => k.text).join(" | ");
const tags = (n, tag) => walk(n).filter((k) => k.tag === tag);
const buttons = (n) => tags(n, "button");

const document = { createElement: (tag) => node(tag) };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}

/* The daemon: every call recorded, every answer scripted. Routes are keyed
   "METHOD path-prefix" so a GET and a POST to the same path can answer
   differently — which is exactly what /children does. */
let sent = [];
let routes = {};
const api = async (p, opts) => {
  const method = (opts && opts.method) || "GET";
  sent.push({ path: p, method, body: opts && opts.body && JSON.parse(opts.body) });
  const key = Object.keys(routes).find((k) => {
    const [m, prefix] = k.split(" ");
    return m === method && p.startsWith(prefix);
  });
  const r = key ? routes[key] : { ok: false, status: 404, doc: {} };
  if (r.throw) throw new Error("offline");
  return { ok: r.ok !== false, status: r.status || 200, json: async () => r.doc || {} };
};

const stubs = `
let sessQuickJobBox = null;
let sessKidsBox = null;
let sessKidsTimer = null;
let sessName = null;
let kidsRefreshed = 0, railRefreshed = 0;
let spawnModalOpens = [];
function refreshSessKids() { kidsRefreshed++; }
function refreshSessions() { railRefreshed++; }
function stopSessKids() { if (sessKidsTimer) sessKidsTimer = null; }
function openSpawnModal(parent, opts) { spawnModalOpens.push({ parent, opts }); }
`;

const ctx = {};
new Function(
  "exports", "document", "el", "api",
  stubs
  + sliceConst("const QUICKJOB_FALLBACK = {")
  + slice("spawnReport") + slice("spawnPreflightNote") + slice("spawnHardBlocks")
  + slice("postSpawn")
  + slice("spawnMissingSources") + slice("spawnSourceNote")
  + slice("qjStamp") + slice("idleNudgeText")
  + slice("sessQuickJob") + slice("renderSessKids")
  + slice("sessRoleNames") + slice("rolePanels") + slice("sessChildren")
  + sliceConst("const ROLE_PANELS = {")
  + `
Object.assign(exports, {
  sessRoleNames, rolePanels, sessQuickJob, renderSessKids, idleNudgeText,
  qjStamp, ROLE_PANELS,
  qjBox: () => sessQuickJobBox,
  drop: () => { sessQuickJobBox = null; sessKidsBox = null; },
  opens: () => spawnModalOpens,
  resetOpens: () => { spawnModalOpens = []; },
  counters: () => ({ kids: kidsRefreshed, rail: railRefreshed }),
});`
)(ctx, document, el, api);

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) return;
  failures++;
  console.log(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
};
const settle = () => new Promise((r) => setImmediate(r));

const QJ_ROUTES = () => ({
  "GET /api/quickjob": { doc: { quick_job: {
    role: "worker", workflow: "improv-worker", worktree: true,
    name_prefix: "job", task: "",
  } } },
  "GET /api/roles": { doc: { roles: [{ name: "leader" }, { name: "worker" }] } },
  "GET /api/cflow/workflows": { doc: { workflows: ["improv-worker", "review"] } },
  "GET /api/sessions/lead1/children":
    { doc: { can_spawn: true, children_remaining: 3 } },
});

const qjParts = (box) => {
  const sel = tags(box, "select");
  const inputs = tags(box, "input");
  return {
    role: sel[0], wf: sel[1],
    wt: inputs.find((i) => i.type === "checkbox"),
    spawn: buttons(box).find((b) => b.text.startsWith("Spawn")),
    save: buttons(box).find((b) => b.text.startsWith("Save")),
  };
};

async function main() {
  /* ---- the registry: role in, panels out --------------------------------- */
  check("leader by definition gets both panels",
    ctx.rolePanels({ session: { role: "leader" } }).length === 2);
  check("leader by mesh membership alone is enough",
    ctx.rolePanels({ session: {}, meshes: [{ mesh: "m", role: "leader" }] })
      .length === 2);
  check("leader twice over still gets each panel once",
    ctx.rolePanels({
      session: { role: "leader" },
      role: { name: "leader" },
      meshes: [{ mesh: "m", role: "LEADER" }],
    }).length === 2);
  check("a worker gets no role panels",
    ctx.rolePanels({ session: { role: "worker" },
      meshes: [{ mesh: "m", role: "worker" }] }).length === 0);
  check("the resolved role object wins over the raw name",
    ctx.rolePanels({ session: { role: "" }, role: { name: "leader" } })
      .length === 2);

  /* ---- quick job: defaults in, one wizard out ---------------------------- */
  ctx.drop();
  sent = [];
  routes = QJ_ROUTES();
  const data = { session: { name: "lead1", role: "leader", cwd: "C:/repo" } };
  const box = ctx.sessQuickJob(data);
  await settle();
  const p = qjParts(box);
  check("role picker lands on the yaml default", p.role.value === "worker",
    p.role && p.role.value);
  check("workflow picker lands on the yaml default",
    p.wf.value === "improv-worker", p.wf && p.wf.value);
  check("worktree default is read from the yaml", p.wt && p.wt.checked === true);
  check("capability line reports the slots", texts(box).includes("3 child slot(s) left"));
  check("spawn is armed once the sets are in", p.spawn.disabled === false);

  /* the poll must not rebuild the form under a half-typed task */
  const again = ctx.sessQuickJob(data);
  check("same session, same live node", ctx.qjBox() &&
    walk(again).includes(ctx.qjBox()));

  /* The worker dispatch is the wizard's now: the pickers seed it, the leader
     is pinned as the parent, and the wizard does the POST (its own harness
     checks the body). */
  ctx.resetOpens();
  await p.spawn.fire("click");
  await settle();
  const open = ctx.opens()[0];
  check("the quick-job button opens the wizard", !!open, open);
  check("...with this leader as the parent", open && open.parent === "lead1",
    open && open.parent);
  check("...and the pickers as the seed",
    open && open.opts && open.opts.seed &&
      open.opts.seed.role === "worker" &&
      open.opts.seed.workflow === "improv-worker" &&
      open.opts.seed.worktree === true,
    open && open.opts && open.opts.seed);

  /* save writes the three defaults back, and says where they went */
  routes["PUT /api/quickjob"] = { doc: { quick_job: { role: "worker" } } };
  sent = [];
  await p.save.fire("click");
  await settle();
  const put = sent.find((s) => s.method === "PUT");
  check("save PUTs the quickjob block", put && put.path === "/api/quickjob");
  check("save sends role/workflow/worktree and nothing else", put &&
    JSON.stringify(Object.keys(put.body).sort()) ===
    JSON.stringify(["role", "workflow", "worktree"]), put && put.body);
  check("save names the yaml it wrote", texts(box).includes("~/.claunch.yaml"));

  /* a leader that may not spawn is told so before anything is dispatched */
  ctx.drop();
  routes = QJ_ROUTES();
  routes["GET /api/sessions/lead1/children"] = {
    doc: { can_spawn: false, blocked_by: ["depth limit reached (3/3)"] },
  };
  ctx.resetOpens();
  const blocked = ctx.sessQuickJob(data);
  await settle();
  check("a blocked spawn keeps the button dead",
    qjParts(blocked).spawn.disabled === true);
  check("...and quotes the policy", texts(blocked).includes("depth limit reached"));
  await qjParts(blocked).spawn.fire("click");
  await settle();
  check("a blocked spawn opens no wizard", ctx.opens().length === 0,
    ctx.opens().length);

  /* ---- the child cap is a warning here, not a wall ----------------------
     spawn.py reports the SOFT cap in soft_blocked_by alone now: can_spawn
     stays true and blocked_by is empty, because the cap warns and lets the
     spawn through. The panel has to keep the button AND say the cap was
     reached -- a leader at 4/4 that loses the button loses the one press
     that reaches the wizard, and a leader that loses the sentence spawns a
     fifth worker without being told it did. */
  ctx.drop();
  routes = QJ_ROUTES();
  routes["GET /api/sessions/lead1/children"] = {
    doc: {
      can_spawn: true,
      blocked_by: [],
      soft_blocked_by: [
        "child limit reached (4 running/4) — spawning anyway is allowed " +
        "and the daemon counts it against you",
      ],
      children_remaining: 0,
    },
  };
  ctx.resetOpens();
  const capped = ctx.sessQuickJob(data);
  await settle();
  check("the child cap keeps the button alive",
    qjParts(capped).spawn.disabled === false);
  check("...and says which cap was reached",
    texts(capped).includes("child limit reached (4 running/4)"),
    texts(capped).slice(-260));
  check("...and says the spawn goes through anyway",
    texts(capped).includes("spawning anyway is allowed"),
    texts(capped).slice(-260));
  await qjParts(capped).spawn.fire("click");
  await settle();
  check("a capped leader still reaches the wizard", ctx.opens().length === 1,
    ctx.opens().length);

  /* A daemon old enough to fold the cap into blocked_by as well: the panel
     subtracts it (spawnHardBlocks) rather than reading a wall into it. */
  ctx.drop();
  routes = QJ_ROUTES();
  routes["GET /api/sessions/lead1/children"] = {
    doc: {
      can_spawn: false,
      blocked_by: ["child limit reached (4/4)"],
      soft_blocked_by: ["child limit reached (4/4)"],
      children_remaining: 0,
    },
  };
  ctx.resetOpens();
  const folded = ctx.sessQuickJob(data);
  await settle();
  check("an old daemon's folded cap is still not a wall",
    qjParts(folded).spawn.disabled === false);
  await qjParts(folded).spawn.fire("click");
  await settle();
  check("...and still reaches the wizard", ctx.opens().length === 1,
    ctx.opens().length);

  /* A hard block sitting UNDER the soft one still closes the panel: nothing
     about the child cap undoes the depth ceiling. */
  ctx.drop();
  routes = QJ_ROUTES();
  routes["GET /api/sessions/lead1/children"] = {
    doc: {
      can_spawn: false,
      blocked_by: ["depth limit reached (3/3)"],
      soft_blocked_by: ["child limit reached (4/4)"],
      children_remaining: 0,
    },
  };
  ctx.resetOpens();
  const both = ctx.sessQuickJob(data);
  await settle();
  check("a hard block outranks the soft cap",
    qjParts(both).spawn.disabled === true);
  check("...and the note quotes the HARD one",
    texts(both).includes("depth limit reached"), texts(both).slice(-200));
  check("...and the soft cap alone opens no wizard",
    ctx.opens().length === 0, ctx.opens().length);

  /* ---- a picker emptied by a failed fetch says so, and refuses to save ---
     The panel's two pickers are also what "Save as defaults" WRITES to
     ~/.claunch.yaml. So a dead /api/roles is worse here than in the wizard:
     the role list degrades to empty, the picker holds the placeholder, and
     one press of Save would put that empty string over the user's real
     default. The warning has to be visible and the save has to be shut. */
  ctx.drop();
  routes = QJ_ROUTES();
  routes["GET /api/roles"] = { throw: true };
  ctx.resetOpens();
  sent = [];
  const dead = ctx.sessQuickJob(data);
  await settle();
  const dp = qjParts(dead);
  check("a dead source is named", texts(dead).includes("could not load roles"),
    texts(dead).slice(-200));
  check("...as a failed fetch, not an empty offering",
    texts(dead).includes("not because there is nothing to offer"));
  check("...and Save is shut so it cannot overwrite the yaml",
    dp.save.disabled === true);
  // The slot count must not paint over the warning.
  check("...the warning outranks the slot count",
    !texts(dead).includes("3 child slot(s) left"), texts(dead).slice(-200));
  // Spawning is still allowed: the wizard is where the pick is finally made,
  // and it raises its own warning. Only the silent WRITE is forbidden.
  check("...but dispatch still works", dp.spawn.disabled === false);

  /* the healthy panel says nothing about sources and saves as before */
  ctx.drop();
  routes = QJ_ROUTES();
  const live = ctx.sessQuickJob(data);
  await settle();
  check("a healthy panel raises no source warning",
    !texts(live).includes("could not load"));
  check("...and Save is available", qjParts(live).save.disabled === false);

  /* ---- the reaping nudge: reported idleness, leader's judgement ---------- */
  const KIDS = { children: [
    { name: "w1", status: "idle" },
    { name: "w2", status: "busy",
      cflow: { workflow: "improv-worker", step: "work" } },
    { name: "w3", status: "idle" },
    { name: "w4", status: "exited" },
  ] };
  const bodyEl = el("div", "sess-kids-body");
  bodyEl.dataset.slot = "lead1";
  ctx.renderSessKids(bodyEl, KIDS);
  check("every child is a row", tags(bodyEl, "a").length === 4);
  check("a child's run travels with it",
    texts(bodyEl).includes("improv-worker · work"));
  const nudge = buttons(bodyEl)[0];
  check("the nudge counts the idle", nudge.text.includes("(2)"), nudge.text);
  check("the nudge is armed", nudge.disabled === false);

  sent = [];
  routes = { "POST /api/sessions/lead1/deliver":
    { doc: { ok: true, delivered: true } } };
  await nudge.fire("click");
  await settle();
  const njPost = sent.find((s) => s.method === "POST");
  check("the nudge is a delivery into the leader itself", njPost &&
    njPost.path === "/api/sessions/lead1/deliver", njPost && njPost.path);
  check("it names the idle children, and only them", njPost &&
    njPost.body.text.includes("w1, w3") && !njPost.body.text.includes("w2"),
    njPost && njPost.body.text);
  check("it asks for judgement, not obedience",
    njPost && njPost.body.text.includes("Your call"));
  check("the outcome is reported", texts(bodyEl).includes("typed into"));

  /* nothing idle: the button is offered but dead, and says why */
  const calm = el("div");
  calm.dataset.slot = "lead1";
  ctx.renderSessKids(calm, { children: [{ name: "w2", status: "busy" }] });
  const calmBtn = buttons(calm)[0];
  check("no idle children, no live button", calmBtn.disabled === true);
  check("...with the reason on the tooltip",
    calmBtn.title.includes("no idle children"));

  /* an empty roster points at the form above rather than offering a button
     there is nothing to press */
  const bare = el("div");
  bare.dataset.slot = "lead1";
  ctx.renderSessKids(bare, { children: [] });
  check("no children reads as an invitation", texts(bare).includes("no children"));
  check("...and offers no nudge", buttons(bare).length === 0);

  /* a delivery the daemon accepted but could not type yet must not read as
     "done" — the leader is mid-turn, and the request is still queued */
  sent = [];
  routes = { "POST /api/sessions/lead1/deliver":
    { doc: { ok: true, delivered: false } } };
  ctx.renderSessKids(bodyEl, KIDS);
  await buttons(bodyEl)[0].fire("click");
  await settle();
  check("a held delivery says queued, not typed",
    texts(bodyEl).includes("queued"), texts(bodyEl).slice(-100));

  /* ---- the diagram holds its size --------------------------------------- */
  const diaCtx = {};
  new Function("exports",
    // wfStepOrder is the graph's row order, lifted out to be shared with the
    // timing diagram under it (s157) — named here because this harness
    // slices by function name rather than by span.
    // wfdTextW/wfdFit cut a step title to its box; wfDiagramSvg calls them on
    // every node, so a slice without them crashes rather than draws.
    slice("escXml") + slice("wfStepOrder") + slice("wfTreeLayout")
    + slice("wfdTextW") + slice("wfdFit")
    + slice("wfDiagramSvg")
    + "Object.assign(exports, { wfDiagramSvg });")(diaCtx);
  const svg = diaCtx.wfDiagramSvg(
    { start: "a", steps: [{ id: "a", title: "A" }] }, { visits: {} }, null
  );
  check("the svg pins its natural width", svg.includes('width="480"'), svg.slice(0, 160));
  check("...and its natural height", /height="\d+"/.test(svg));
  check("the stylesheet only ever shrinks it",
    /\.wfd \{[^}]*max-width: 100%/.test(css) && !/\.wfd \{[^}]*[^-]width: 100%/.test(css));

  console.log(failures ? `${failures} failure(s)` : "rolepanel_check OK");
  process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.log("CRASH", e); process.exit(1); });
