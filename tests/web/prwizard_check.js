/* The PR wizard: the detail head's "Open PR" button, its modal, its rules.
   A browser judges how it looks; the RULES are what break, and they are
   driven here against a stub DOM the way spawnmodal_check does. What must
   hold: the preview fills the form's defaults and a blocker keeps the button
   dead; the monitor checkbox lives only under a ticked report checkbox AND
   a daemon that offers a monitor (a tick on a greyed box is dropped); the
   uncommitted checkbox greys on a clean tree; the payload is spelt in the
   route's keys and reads through the disables; the press posts to
   /api/sessions/<name>/pr and swaps the form for the step list. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"), "utf8");

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

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), handlers: {}, attributes: {},
    value: undefined, checked: false, disabled: false, hidden: false, rows: 0,
    title: "", href: "", onclick: null,
    classList: {
      add(c) { n.classes.add(c); },
      remove(c) { n.classes.delete(c); },
      contains(c) { return n.classes.has(c); },
    },
    appendChild(c) { this.kids.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.appendChild(c)); },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    setAttribute(k, v) { this.attributes[k] = String(v); },
    fire(k, ev) { return Promise.all((this.handlers[k] || []).map((fn) => fn(ev))); },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get innerText() { return this.text; },
    set innerText(v) { this.kids = []; this.text = String(v); },
    get innerHTML() { return ""; },
    set innerHTML(v) { this.kids = []; },
    get options() { return this.kids.filter((k) => k.tag === "option"); },
    querySelector(sel) {
      const pred = sel === "input" ? (k) => k.tag === "input" : null;
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
  if (tag === "input" || tag === "textarea" || tag === "select") n.value = "";
  return n;
}
const walk = (n, out = []) => { for (const k of n.kids) { out.push(k); walk(k, out); } return out; };
const texts = (n) => walk(n).map((k) => k.text).join(" | ");
const buttons = (n) => walk(n).filter((k) => k.tag === "button");

const modalEls = {
  "modal-overlay": node("div"), "modal-title": node("h2"),
  "modal-body": node("div"), "modal-actions": node("div"),
};
const document = {
  createElement: (tag) => node(tag),
  getElementById: (id) => modalEls[id] || null,
  listeners: {},
  addEventListener(k, fn) { (this.listeners[k] ||= []).push(fn); },
  removeEventListener(k, fn) { this.listeners[k] = (this.listeners[k] || []).filter((f) => f !== fn); },
};
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}
const $ = (id) => document.getElementById(id);

/* ---- the daemon, scripted --------------------------------------------- */
let sent = [];
let routes = {};
const api = async (p, opts) => {
  const method = (opts && opts.method) || "GET";
  sent.push({ path: p, method, body: opts && opts.body && JSON.parse(opts.body) });
  const key = Object.keys(routes)
    .filter((k) => { const [m, prefix] = k.split(" "); return m === method && p.startsWith(prefix); })
    .sort((a, b) => b.length - a.length)[0];
  const r = key ? routes[key] : { ok: false, status: 404, doc: {} };
  if (r.throw) throw new Error("offline");
  return { ok: r.ok !== false, status: r.status || 200, json: async () => r.doc || {} };
};

const stubs = `
let prModal = null;
let spawnModal = null;
let currentName = null;
function terminalOnScreen() { return false; }
function handleTag() { return null; }
function cwdLine() { return el("div", "sess-cwd"); }
function closeDetail() {}
function go() {}
function openSpawnModal() {}
const MOBILE_MQ = { matches: false };
`;

const ctx = {};
new Function(
  "exports", "document", "el", "api", "$",
  stubs
  + slice("spawnRow") + slice("spawnSubRow") + slice("spawnCheckRow")
  + slice("spawnGroup") + slice("fillSpawnSelect") + slice("setActionPending")
  + slice("prCheck") + slice("buildPrForm") + slice("syncPrGates") + slice("prPayload")
  + slice("prApplyPreview") + slice("prModalKey") + slice("prModalClose")
  + slice("openPrModal") + slice("prModalLoad") + slice("prResultView") + slice("prModalGo")
  + slice("sessHead")
  + `
Object.assign(exports, {
  buildPrForm, syncPrGates, prPayload, prApplyPreview, openPrModal, prModalClose,
  prResultView, sessHead,
  isOpen: () => prModal !== null,
  state: () => prModal,
});`
)(ctx, document, el, api, $);

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) return;
  failures++;
  console.log(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
};
const settle = () => new Promise((r) => setImmediate(r));

const PREVIEW = {
  repo: true, branch: "s1-work", head: "abcdef0123456789", head_short: "abcdef01",
  head_subject: "fix the thing", dirty: { tracked: 2, untracked: 1 }, worktree: "s1-work",
  remotes: [
    { remote: "origin", host: "ghe.example.com", slug: "team/proj", configured: true },
    { remote: "mirror", host: null, slug: null, configured: false },
  ],
  remote: "origin", base: "develop", branch_default: "s1-pr-20260914-2041",
  gh: { installed: true }, auth: {}, blockers: [],
  monitor_workflow: "improv-worker-pr-monitor", monitor_available: true,
};

async function main() {
  /* ---- the head carries the button, and it opens THIS session's wizard ---- */
  const head = ctx.sessHead({ name: "s1", status: "idle", cwd: "C:/repo" });
  const prBtn = buttons(head).find((b) => b.text === "Open PR");
  check("the detail head has an Open PR button", !!prBtn);
  check("...between Spawn and Open terminal", buttons(head).map((b) => b.text).join(",")
    === "Spawn,Open PR,Open terminal,×", buttons(head).map((b) => b.text));
  const exitedHead = ctx.sessHead({ name: "s1", status: "exited", cwd: "C:/repo" });
  const exitedPr = buttons(exitedHead).find((b) => b.text === "Open PR");
  check("an exited session still has a directory to push", exitedPr && !exitedPr.disabled);

  /* ---- the form's rules, before any daemon ---- */
  const { ui } = ctx.buildPrForm("s1");
  check("report is on by default", ui.report.checked === true);
  check("monitor is greyed while the daemon offers none", ui.monitor.disabled === true);
  ui.monitorAvailable = true;
  ctx.syncPrGates(ui);
  check("monitor opens under a ticked report once offered", ui.monitor.disabled === false);
  ui.monitor.checked = true;
  ui.report.checked = false;
  ctx.syncPrGates(ui);
  check("unticking report greys monitor", ui.monitor.disabled === true);
  check("...and drops its tick", ui.monitor.checked === false);
  ui.report.checked = true;
  ctx.syncPrGates(ui);
  ui.monitor.checked = true;
  let p = ctx.prPayload(ui);
  check("payload spells the route's keys",
    ["remote", "base", "branch", "title", "body", "draft", "include_uncommitted", "force", "report", "monitor"]
      .every((k) => k in p), Object.keys(p));
  check("payload carries both switches when both stand", p.report === true && p.monitor === true);
  ui.monitorAvailable = false;
  ctx.syncPrGates(ui);
  p = ctx.prPayload(ui);
  check("a greyed monitor is not asked for", p.monitor === false);

  /* ---- the preview lands: defaults, facts, and the clean-tree rule ---- */
  const ready = ctx.prApplyPreview(ui, PREVIEW);
  check("a preview without blockers is ready", ready === true);
  check("the preview offers the monitor", ui.monitorAvailable === true && ui.monitor.disabled === false);
  const noMon = ctx.buildPrForm("s1").ui;
  ctx.prApplyPreview(noMon, { ...PREVIEW, monitor_available: false });
  check("...and withholds it where the workflow is not declared",
    noMon.monitorAvailable === false && noMon.monitor.disabled === true && noMon.monitorRow.title.includes("cflow update"));
  check("the branch default is the daemon's", ui.branch.value === "s1-pr-20260914-2041");
  check("the base default is the daemon's", ui.base.value === "develop");
  check("the title is session-first", ui.title.value === "s1: fix the thing");
  check("the remote select holds every remote", ui.remote.options.length === 2, ui.remote.options.length);
  check("...and the daemon's pick is selected", ui.remote.value === "origin", ui.remote.value);
  check("the facts line says what stands and how dirty",
    ui.facts.text.includes("s1-work @ abcdef01") && ui.facts.text.includes("2 modified, 1 untracked"),
    ui.facts.text);
  check("uncommitted stays live on a dirty tree", ui.uncommitted.disabled === false && ui.uncommitted.checked === true);
  const clean = Object.assign({}, PREVIEW, { dirty: { tracked: 0, untracked: 0 } });
  ui.branch.value = "";
  ctx.prApplyPreview(ui, clean);
  check("uncommitted greys on a clean tree", ui.uncommitted.disabled === true && ui.uncommitted.checked === false);
  check("...and the payload does not ask for it", ctx.prPayload(ui).include_uncommitted === false);
  check("a preview with blockers is not ready",
    ctx.prApplyPreview(ui, Object.assign({}, PREVIEW, { blockers: ["gh is not installed"] })) === false);

  /* ---- the modal: load, blockers, the press ---- */
  routes["GET /api/sessions/s1/pr/preview"] = { doc: PREVIEW };
  sent = [];
  await ctx.openPrModal("s1");
  await settle();
  check("the modal is open", ctx.isOpen());
  check("the title names the session", modalEls["modal-title"].text === "Open a pull request from s1");
  check("the preview was asked of this session", sent.some((x) => x.path === "/api/sessions/s1/pr/preview"), sent);
  let acts = buttons(modalEls["modal-actions"]);
  let goBtn = acts.find((b) => b.text === "Push & open PR");
  check("the button is live on a ready preview", goBtn && goBtn.disabled === false);

  routes["POST /api/sessions/s1/pr"] = { doc: {
    ok: true, remote: "origin", branch: "s1-pr-20260914-2041", tip: "0123456789abcdef",
    steps: [{ id: "inspect", ok: true, detail: "x" }, { id: "snapshot", ok: true, detail: "y" },
            { id: "push", ok: true, detail: "z" }, { id: "pr", ok: true, detail: "opened u" }],
    pr: { url: "https://ghe.example.com/team/proj/pull/7", number: 7 },
    monitor: { session: "s9", workflow: "improv-worker-pr-monitor", run_started: true },
    delivered: true, warnings: ["note: a warning"],
  } };
  const st = ctx.state();
  st.ui.branch.value = "s1-pr-custom";
  st.ui.monitor.checked = true;
  sent = [];
  await goBtn.fire("click");
  await settle();
  const post = sent.find((x) => x.method === "POST");
  check("the press posts to the session's pr route", post && post.path === "/api/sessions/s1/pr", sent);
  check("...with the form as typed", post && post.body.branch === "s1-pr-custom" && post.body.remote === "origin"
    && post.body.report === true && post.body.monitor === true, post && post.body);
  const bodyText = texts(modalEls["modal-body"]);
  check("the form gave way to the step list", bodyText.includes("✓ push: z") && bodyText.includes("✓ pr: opened u"), bodyText);
  check("...with the PR link", walk(modalEls["modal-body"]).some((k) => k.tag === "a" && k.href.endsWith("/pull/7")));
  check("...the delivery", bodyText.includes("reported into the session's terminal"));
  check("...the monitor child by name", bodyText.includes("child session s9 watches the PR"));
  check("...and the daemon's warnings", bodyText.includes("note: a warning"));
  acts = buttons(modalEls["modal-actions"]);
  check("only Close remains", acts.length === 1 && acts[0].text === "Close", acts.map((b) => b.text));
  await acts[0].fire("click");
  check("Close closes", !ctx.isOpen());

  /* ---- a blocked preview keeps the button dead and says why ---- */
  routes["GET /api/sessions/s1/pr/preview"] = { doc: Object.assign({}, PREVIEW,
    { blockers: ["gh is not signed in to ghe.example.com (gh auth login --hostname ghe.example.com)"] }) };
  await ctx.openPrModal("s1");
  await settle();
  acts = buttons(modalEls["modal-actions"]);
  goBtn = acts.find((b) => b.text === "Push & open PR");
  check("a blocker keeps the button dead", goBtn && goBtn.disabled === true);
  check("...and is quoted", texts(modalEls["modal-body"]).includes("not signed in"), texts(modalEls["modal-body"]).slice(0, 200));
  ctx.prModalClose();

  /* ---- a failed step is shown as such ---- */
  routes["GET /api/sessions/s1/pr/preview"] = { doc: PREVIEW };
  routes["POST /api/sessions/s1/pr"] = { doc: {
    ok: false, failed: "push", error: "git push: rejected",
    steps: [{ id: "inspect", ok: true, detail: "" }, { id: "snapshot", ok: true, detail: "" },
            { id: "push", ok: false, detail: "git push: rejected" }],
    delivered: null, warnings: [],
  } };
  await ctx.openPrModal("s1");
  await settle();
  goBtn = buttons(modalEls["modal-actions"]).find((b) => b.text === "Push & open PR");
  await goBtn.fire("click");
  await settle();
  const failText = texts(modalEls["modal-body"]);
  check("a refused push is a red row and a sentence",
    failText.includes("✗ push: git push: rejected") && failText.includes("failed at push"), failText);
  ctx.prModalClose();

  /* ---- a daemon error is a note, and the form stays ---- */
  routes["POST /api/sessions/s1/pr"] = { ok: false, status: 500, doc: { error: "boom" } };
  await ctx.openPrModal("s1");
  await settle();
  goBtn = buttons(modalEls["modal-actions"]).find((b) => b.text === "Push & open PR");
  await goBtn.fire("click");
  await settle();
  check("an HTTP error is shown and the form is kept", texts(modalEls["modal-body"]).includes("boom")
    && buttons(modalEls["modal-actions"]).some((b) => b.text === "Push & open PR"));
  check("...with the button back", goBtn.disabled === false);

  console.log("\nprwizard_check: " + (failures ? failures + " failing" : "all ok"));
  process.exitCode = failures ? 1 : 0;
}

main();
