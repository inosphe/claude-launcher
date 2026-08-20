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
function refreshSessKids() { kidsRefreshed++; }
function refreshSessions() { railRefreshed++; }
function stopSessKids() { if (sessKidsTimer) sessKidsTimer = null; }
`;

const ctx = {};
new Function(
  "exports", "document", "el", "api",
  stubs
  + sliceConst("const QUICKJOB_FALLBACK = {")
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
    name: inputs.find((i) => i.type !== "checkbox"),
    task: tags(box, "textarea")[0],
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

  /* ---- quick job: defaults in, one worker out ----------------------------- */
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

  /* a taskless worker is refused before the daemon is asked */
  sent = [];
  await p.spawn.fire("click");
  await settle();
  check("no task, no spawn", !sent.some((s) => s.method === "POST"));

  routes["POST /api/sessions/lead1/children"] = {
    status: 201,
    doc: { session: { name: "job-77" }, mesh: { ok: true, mesh: "m0" } },
  };
  p.task.value = "fix the flaky test";
  sent = [];
  await p.spawn.fire("click");
  await settle();
  const post = sent.find((s) => s.method === "POST");
  check("spawn posts to the leader's children", post &&
    post.path === "/api/sessions/lead1/children", post && post.path);
  check("the body carries the picked role", post && post.body.role === "worker");
  check("...and the picked workflow", post && post.body.workflow === "improv-worker");
  check("...and the typed task", post && post.body.task === "fix the flaky test");
  check("the worktree is stamped after the prefix", post &&
    /^job-\d{8}-\d{6}$/.test(post.body.worktree || ""), post && post.body.worktree);
  check("success names the child and its mesh",
    texts(box).includes("spawned 'job-77'") && texts(box).includes("m0"),
    texts(box).slice(-120));
  const counted = ctx.counters();
  check("a spawn refreshes the roster and the rail",
    counted.kids >= 1 && counted.rail >= 1, counted);

  /* a refusal is shown, not swallowed */
  routes["POST /api/sessions/lead1/children"] = {
    ok: false, status: 403, doc: { error: "child limit reached (4/4)" },
  };
  p.task.value = "one more";
  await p.spawn.fire("click");
  await settle();
  check("the policy's refusal is quoted", texts(box).includes("child limit reached"));

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

  /* a leader that may not spawn is told so before typing anything */
  ctx.drop();
  routes = QJ_ROUTES();
  routes["GET /api/sessions/lead1/children"] = {
    doc: { can_spawn: false, blocked_by: ["depth limit reached (3/3)"] },
  };
  const blocked = ctx.sessQuickJob(data);
  await settle();
  check("a blocked spawn keeps the button dead",
    qjParts(blocked).spawn.disabled === true);
  check("...and quotes the policy", texts(blocked).includes("depth limit reached"));

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
  new Function("exports", slice("escXml") + slice("wfDiagramSvg")
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
