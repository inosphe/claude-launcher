// The Install section of the Settings page (#/settings/install): one row per
// install command with its dry-run counts, Preview asking for a plan, Run
// posting the target only after the person confirms. Drives the real
// functions out of app.js with a stubbed `api`, the way
// profilesettings_check.js does.

const assert = require("assert/strict");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const src = fs.readFileSync(
  path.join(__dirname, "../../src/claude_launcher/web/static/app.js"), "utf8");

function el(tag, cls, text = "") {
  return { tag, cls, text, children: [], handlers: {}, attributes: {},
    appendChild(child) { this.children.push(child); return child; },
    addEventListener(name, fn) { this.handlers[name] = fn; },
    setAttribute(name, value) { this.attributes[name] = value; } };
}
function all(node) { return [node, ...(node.children || []).flatMap(all)]; }
const find = (root, predicate) => all(root).find(predicate);
const textOf = (root) => all(root).map((n) => n.text || "").join(" ");

const overview = {
  global_workflows_dir: "C:\\h\\workflows",
  user_config_dir: "C:\\h\\.claude",
  targets: [
    { id: "install:global", label: "Global", command: "claunch install --global",
      summary: { create: 2, update: 1, unchanged: 5 }, pending: [] },
    { id: "cflow-update", label: "Workflows", command: "claunch cflow update",
      summary: { create: 0, update: 0, delete: 1, unchanged: 9 }, pending: [] },
    { id: "install:profile:bad", label: "Profile: bad", command: "x",
      error: "ProfileError: nope" },
  ],
};
const plan = {
  dry_run: true,
  target: { id: "install:global", label: "Global", command: "claunch install --global" },
  summary: { create: 1, update: 1, unchanged: 0 },
  effects: { mcp: "Registers the 'claunch' MCP server" },
  changes: [
    { path: "C:\\h\\.claude.json", kind: "update", category: "mcp",
      before: "{\n}", after: "{\n  \"mcpServers\": {}\n}" },
    { path: "C:\\h\\skills\\cflow\\SKILL.md", kind: "create", category: "skill",
      before: null, after: "skill text" },
  ],
  lines: ["mcp server 'claunch' -> C:\\h\\.claude.json"],
};

const requests = [];
let confirmAnswer = false;
const context = vm.createContext({
  el,
  document: { createTextNode: (text) => el("text", null, text) },
  wsOpen: true,
  wsSection: "install",
  renderWorkspaces() {},
  confirm: () => confirmAnswer,
  encodeURIComponent,
  api: async (url, options) => {
    requests.push([url, options]);
    const body = url === "/api/install" ? overview
      : url.startsWith("/api/install/plan") ? plan
      : { ...plan, dry_run: false, lines: ["installed"] };
    return { ok: true, status: 200, json: async () => body };
  },
});

vm.runInContext(
  src.slice(src.indexOf("let installOverview = null"),
            src.indexOf("function renderWorkspaces() {")),
  context);

(async () => {
  // --- before the first answer: a waiting line, not an empty table ----------
  let panel = context.installPanel();
  assert.match(textOf(panel), /Reading the install state/);

  // --- the overview ----------------------------------------------------------
  await context.refreshInstallOverview();
  assert.deepEqual(requests.map((r) => r[0]), ["/api/install"]);
  panel = context.installPanel();
  const table = find(panel, (n) => n.tag === "table");
  const rows = table.children.slice(1);
  assert.equal(rows.length, 3);
  assert.deepEqual(rows[0].children.slice(0, 6).map((c) => c.text),
    ["Global", "claunch install --global", "2", "1", "0", "5"]);
  // A count that means "Run would write something" is marked.
  assert.equal(rows[0].children[2].cls, "install-pending");
  assert.equal(rows[1].children[2].cls, null);
  // A retired workflow the update would remove counts as a pending change.
  assert.deepEqual(rows[1].children.slice(2, 6).map((c) => c.text), ["0", "0", "1", "9"]);
  assert.equal(rows[1].children[4].cls, "install-pending");
  // A target that could not be planned says why and cannot be run.
  assert.match(textOf(rows[2]), /ProfileError: nope/);
  const badRun = find(rows[2], (n) => n.tag === "button" && n.text === "Run");
  assert.equal(badRun.disabled, true);

  // --- Preview asks for that target's plan and shows its effect and files ----
  const preview = find(rows[0], (n) => n.tag === "button" && n.text === "Preview");
  await preview.handlers.click();
  assert.equal(requests.at(-1)[0], "/api/install/plan?target=install%3Aglobal");
  panel = context.installPanel();
  const detail = find(panel, (n) => n.cls === "ws-add install-detail");
  assert.ok(detail);
  const text = textOf(detail);
  assert.match(text, /Dry run: Global/);
  assert.match(text, /Registers the 'claunch' MCP server/);
  assert.match(text, /\+   "mcpServers": \{\}/);
  assert.match(text, /skill text/);

  // --- Run posts nothing unless the person confirms --------------------------
  const run = find(rows[0], (n) => n.tag === "button" && n.text === "Run");
  const before = requests.length;
  confirmAnswer = false;
  await run.handlers.click();
  assert.equal(requests.length, before);

  confirmAnswer = true;
  await run.handlers.click();
  const post = requests.find((r) => r[0] === "/api/install/apply");
  assert.ok(post);
  assert.equal(post[1].method, "POST");
  assert.deepEqual(JSON.parse(post[1].body), { target: "install:global" });
  // ...and the overview is read again, so the counts show the new state.
  assert.equal(requests.at(-1)[0], "/api/install");
  panel = context.installPanel();
  assert.match(textOf(panel), /Ran: Global/);
  assert.match(textOf(panel), /installed/);
  console.log("installsettings_check: ok");
})().catch((err) => { console.error(err); process.exit(1); });
