/* The Workspaces page, run against the real functions from app.js.

   The directory registry, the projects that group sessions and each
   workspace's beads board used to sit among the machine settings on
   General, then on a Settings tab; they are a page of their own now
   (#/workspaces, claunch-tqm2e). What has to hold is that each of them is
   drawn on exactly that page, that Settings no longer carries them nor a tab
   for them, that #/workspaces (every "manage" link beside a Project or
   Directory field) and the old #/settings/workspaces bookmark both open the
   page, that each workspace's Sync panel is drawn under its row when open,
   and that opening a section fetches that section's data and not another's. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("node:assert/strict");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

function el(tag, cls, text) {
  const node = {
    tag, cls: cls || "", text: text === undefined ? "" : String(text),
    children: [], href: "", handlers: {},
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(ev, fn) { (this.handlers[ev] = this.handlers[ev] || []).push(fn); },
    set innerHTML(v) { this.children = []; },
  };
  return node;
}

/* ---- routes ------------------------------------------------------------ */
const parseHash = new Function(`${slice("parseHash")}; return parseHash;`)();
assert.deepEqual(parseHash("#/settings"), { page: "settings", section: "" });
// A bookmark from when the registry was a Settings tab.
assert.deepEqual(parseHash("#/settings/workspaces"), { page: "workspaces" });
assert.deepEqual(parseHash("#/settings/profiles"), { page: "settings", section: "profiles" });
// The link every "manage" beside Project and Directory still spells.
assert.deepEqual(parseHash("#/workspaces"), { page: "workspaces" });

/* ---- which cards each section draws ------------------------------------ */
const CARDS = [
  "keyHelpCard", "scoreGoalSettingsCard", "roleWorkflowsCard", "railStaleCard", "modelChoicesCard",
  "llmSettingsCard", "modelPricesCard", "simDefaultsCard", "qaNudgeCard", "megaThreadCard", "faqCard",
  "promptPresetCard", "prompterSettingsCard",
  "statusCheckCard", "ragCard", "ghCard", "relaySettingsCard", "wslRegStubCard", "ptyHostSettingCard",
  "testOutputDirCard",
  "delegateSettingCard", "modelTiersCard", "providerAffinityCard",
  "wsAddCard", "projectsCard", "beadsBoardsCard",
];
const WORKSPACE_CARDS = ["wsAddCard", "projectsCard", "beadsBoardsCard"];
const settingsView = el("div", "ws-view");
const pageView = el("div", "workspaces-view");
let view = settingsView;
const ctx = {
  el,
  $: (id) => (id === "ws-view" ? settingsView : id === "workspaces-view" ? pageView : null),
  document: { activeElement: null },
  workspacesCache: [{ name: "a", exists: true }, { name: "b", exists: true }],
  wsSection: "",
  wsRow: (w) => el("div", "ws-row", w.name),
  wsSyncOpen: new Set(),
  wsSyncPanel: (w) => el("div", "ws-sync", w.name),
  wsTreesOpen: new Set(),
  wsWorktreeList: (w) => el("div", "ws-worktrees", w.name),
  wsRefreshAllBar: () => el("div", "ws-refresh-all", "bar"),
  installPanel: () => el("div", "card", "installPanel"),
  profilesPanel: () => el("div", "card", "profilesPanel"),
  globalThis: {},
};
for (const name of CARDS) ctx[name] = () => el("section", "card", name);
vm.createContext(ctx);
vm.runInContext(
  ["settingsTabs", "renderWorkspaces", "workspacesPanel", "refocusSettingsField"]
    .map(slice).join("\n"),
  ctx);

function drawn(section) {
  ctx.wsSection = section;
  view = section === "workspaces" ? pageView : settingsView;
  ctx.renderWorkspaces();
  return view.children.filter((c) => c.cls === "card").map((c) => c.text);
}
const general = drawn("");
const workspaces = drawn("workspaces");
for (const name of WORKSPACE_CARDS) {
  assert.equal(general.includes(name), false, `${name} left General`);
  assert.equal(workspaces.includes(name), true, `${name} is on Workspaces`);
}
// Nothing else moved: General keeps every machine setting, and the
// Workspaces tab carries only the registry's cards.
assert.deepEqual(general, CARDS.filter((c) => !WORKSPACE_CARDS.includes(c)));
assert.deepEqual(workspaces, ["wsAddCard", "projectsCard", "beadsBoardsCard"]);
// The registry list itself went with them, and is drawn into the page's own
// view: Settings' view is left as it was.
drawn("workspaces");
const list = pageView.children.find((c) => c.cls === "ws-list");
assert.ok(list, "the Registered list is on the Workspaces page");
assert.equal(list.children[0].text, "Registered (2)");
assert.equal(pageView.children[0].children[0].text, "Workspaces", "the page's own head");
assert.equal(pageView.children.some((c) => c.cls.includes("settings-tabs")), false,
             "the Workspaces page carries no Settings tab strip");
drawn("");
assert.equal(settingsView.children.some((c) => c.cls === "ws-list"), false,
             "the Registered list left General");
// Settings' tab strip no longer offers a Workspaces tab.
const tabs = settingsView.children.find((c) => c.cls.includes("settings-tabs"));
assert.deepEqual(tabs.children.map((t) => t.text), ["General", "Profiles", "Install", "VS Code"]);
assert.deepEqual(tabs.children.filter((t) => t.cls.includes("on")).map((t) => t.text),
                 ["General"]);
// An open Sync panel is drawn right under its workspace's row, and only that one.
ctx.wsSyncOpen.add("b");
drawn("workspaces");
const rows = pageView.children.find((c) => c.cls === "ws-list").children.map((c) => `${c.cls}:${c.text}`);
assert.deepEqual(rows, [":Registered (2)", "ws-refresh-all:bar", "ws-row:a", "ws-row:b", "ws-sync:b"]);
// An open worktree list sits between its row and that row's Sync panel
// (claunch-kjjta).
ctx.wsTreesOpen.add("b");
drawn("workspaces");
assert.deepEqual(
  pageView.children.find((c) => c.cls === "ws-list").children.map((c) => `${c.cls}:${c.text}`),
  [":Registered (2)", "ws-refresh-all:bar", "ws-row:a", "ws-row:b", "ws-worktrees:b", "ws-sync:b"]);
ctx.wsTreesOpen.clear();
ctx.wsSyncOpen.clear();

/* ---- which view each section shows ------------------------------------- */
const shown = [];
const vctx = {
  wsOpen: false, wsSection: "",
  showView(name) { shown.push(name); },
  renderWorkspaces() {}, refreshWorkspaces() {},
};
vm.createContext(vctx);
vm.runInContext(slice("openWorkspaces"), vctx);
vctx.openWorkspaces("workspaces");
vctx.openWorkspaces("profiles");
vctx.openWorkspaces("");
assert.deepEqual(shown, ["workspaces", "settings", "settings"]);

/* ---- which data each section fetches ------------------------------------ */
const called = [];
const REFRESHERS = [
  "refreshHarnesses", "refreshLlmSettings", "refreshModelPrices", "refreshSimDefaults", "refreshQaNudge",
  "refreshMegaThread", "refreshFaq", "refreshPromptPresets",
  "refreshPrompterSettings", "refreshStatusChecks", "refreshRagStatus",
  "refreshGhStatus", "refreshRelaySettings", "refreshBeadsBoards",
  "refreshProjects", "refreshProfileSettings", "refreshInstallOverview",
  "refreshVscode", "refreshWsGitSummary", "refreshWsUsage",
  "refreshRoleWorkflowSettings", "refreshWslRegStub", "refreshPtyHostSetting",
  "refreshTestOutputDir",
  "refreshDelegateSetting", "refreshProviderAffinity", "refreshModelTiers",
];
const octx = {
  wsSection: "", wsOpen: true,
  openWorkspaces(section) { octx.wsSection = section || ""; },
  renderWorkspaces() {},
};
for (const name of REFRESHERS) {
  octx[name] = () => { called.push(name); return Promise.resolve(); };
}
vm.createContext(octx);
vm.runInContext(slice("openSettings"), octx);

called.length = 0;
octx.openSettings("workspaces");
// ...the VS Code state its rows' buttons are drawn from, and the git
// summary its row badges are (claunch-gax44), and the cached disk usage
// (claunch-2oait).
assert.deepEqual([...called].sort(),
  ["refreshBeadsBoards", "refreshProjects", "refreshVscode", "refreshWsGitSummary",
   "refreshWsUsage"]);

called.length = 0;
octx.openSettings("vscode");
assert.deepEqual(called, ["refreshVscode"], "the VS Code tab fetches its own state only");

called.length = 0;
octx.openSettings("");
assert.equal(called.includes("refreshBeadsBoards"), false,
             "General no longer fetches the board listing it does not draw");
assert.equal(called.includes("refreshLlmSettings"), true);
assert.equal(called.includes("refreshModelPrices"), true,
             "General fetches the Model prices card's table");
assert.equal(called.includes("refreshSimDefaults"), true,
             "General fetches the Simulation models card's data");
assert.equal(called.includes("refreshRoleWorkflowSettings"), true,
             "General fetches the Role workflows card's data");
assert.equal(called.includes("refreshPtyHostSetting"), true,
             "General fetches the PTY host card's data (claunch-xnnct.5)");
assert.equal(called.includes("refreshTestOutputDir"), true,
             "General fetches the test output dir card's data (claunch-pyv3d.1)");

console.log("settingsworkspaces_check ok");
