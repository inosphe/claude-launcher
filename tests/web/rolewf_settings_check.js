/* Settings ▸ Role workflows, run against the real functions from app.js.

   One row per role, each a select whose first option names the workflow the
   role would get with no override (its workflows' default_role), then "-"
   (no workflow), then what the role's filter admits. A saved override is
   shown selected -- also one the daemon directory does not list -- and a
   change PUTs {role, workflow} to /api/role-workflows and forgets the
   new-session form's list memo so that form re-reads the overrides. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("node:assert/strict");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8");

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  const head = src.slice(start - 6, start) === "async " ? start - 6 : start;
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}
function sliceConst(name) {
  const start = src.indexOf(`const ${name} =`);
  if (start < 0) throw new Error(`cannot locate ${name}`);
  return src.slice(start, src.indexOf(";", start) + 1);
}

function el(tag, cls, text) {
  return {
    tag, cls: cls || "", text: text === undefined ? "" : String(text),
    children: [], handlers: {},
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { this.children.push(...cs); },
    addEventListener(ev, fn) { (this.handlers[ev] = this.handlers[ev] || []).push(fn); },
  };
}
function select() {
  const s = el("select");
  s.options = [];
  s.appendChild = (o) => { s.options.push(o); return o; };
  return s;
}

const puts = [];
const ANSWERS = {
  "/api/roles": { roles: [{ name: "worker" }, { name: "reviewer" }] },
  "/api/cflow/workflows": { workflows: [
    { name: "improv-worker", default_role: "worker",
      filter_roles: { type: "whitelist", roles: ["worker"] } },
    { name: "improv-worker-v2", version: 2,
      filter_roles: { type: "whitelist", roles: ["worker"] } },
    { name: "plain" },
  ] },
  "/api/role-workflows": { role_workflows: { reviewer: "gone-flow" } },
};
let renders = 0;
const ctx = {
  el, Option: function (label, value) { return { label, value }; },
  document: { createElement: () => select() },
  wsOpen: true, workflowsFor: "F:/repo",
  renderWorkspaces() { renders++; },
  api: async (p, opts) => {
    if (opts && opts.method === "PUT") {
      const body = JSON.parse(opts.body);
      puts.push(body);
      return { ok: true, json: async () => ({ role_workflows: { [body.role]: body.workflow } }) };
    }
    return { ok: true, json: async () => ANSWERS[p] };
  },
};
vm.createContext(ctx);
vm.runInContext([
  sliceConst("roleWfSettings"),
  slice("spawnWorkflowEntry"), slice("spawnWorkflowAdmits"), slice("spawnRankWorkflows"),
  slice("refreshRoleWorkflowSettings"), slice("roleWorkflowsCard"),
  "globalThis.st = roleWfSettings;",
].join("\n"), ctx);

const selects = (card) => card.children.filter((c) => c.cls === "sess-spawn-row")
  .map((r) => r.children[1]);

(async () => {
  let card = ctx.roleWorkflowsCard();
  assert.ok(card.children.some((c) => c.text === "Loading…"), "loading until fetched");

  await ctx.refreshRoleWorkflowSettings();
  assert.equal(renders, 1, "a fetch redraws the page");
  card = ctx.roleWorkflowsCard();
  const [worker, reviewer] = selects(card);
  assert.deepEqual(worker.options.map((o) => o.value),
    ["", "-", "improv-worker", "improv-worker-v2", "plain"]);
  assert.equal(worker.options[0].label, "(default: improv-worker)");
  assert.equal(worker.value, "", "no override: the default row");
  // reviewer: no workflow volunteers, the worker-only ones are not offered,
  // and a saved override the directory does not list is still shown.
  assert.equal(reviewer.options[0].label, "(default: none)");
  assert.deepEqual(reviewer.options.map((o) => o.value), ["", "-", "plain", "gone-flow"]);
  assert.equal(reviewer.value, "gone-flow");

  worker.value = "improv-worker-v2";
  await worker.handlers.change[0]();
  assert.deepEqual(puts, [{ role: "worker", workflow: "improv-worker-v2" }]);
  assert.deepEqual(ctx.st.overrides, { worker: "improv-worker-v2" });
  assert.equal(ctx.workflowsFor, null, "the new-session form re-reads its list");
  assert.equal(ctx.st.error, "");
  console.log("rolewf_settings_check ok");
})().catch((err) => { console.error(err); process.exit(1); });
