/* The Settings page's Workspaces section, run against the real functions
   from app.js.

   The directory registry, the projects that group sessions and each
   workspace's beads board used to sit among the machine settings on
   General. What has to hold now is that each of them is on exactly one
   tab, that General no longer carries them, that the older #/workspaces
   link (every "manage" link beside a Project or Directory field) opens the
   section those links mean, and that opening a section fetches that
   section's data and not the other one's. */
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
assert.deepEqual(parseHash("#/settings/workspaces"), { page: "settings", section: "workspaces" });
assert.deepEqual(parseHash("#/settings/profiles"), { page: "settings", section: "profiles" });
// The link every "manage" beside Project and Directory still spells.
assert.deepEqual(parseHash("#/workspaces"), { page: "settings", section: "workspaces" });

/* ---- which cards each section draws ------------------------------------ */
const CARDS = [
  "keyHelpCard", "scoreGoalSettingsCard", "railStaleCard", "modelChoicesCard",
  "llmSettingsCard", "faqCard", "promptPresetCard", "prompterSettingsCard",
  "statusCheckCard", "ragCard", "ghCard", "relaySettingsCard",
  "wsAddCard", "projectsCard", "beadsBoardsCard",
];
const WORKSPACE_CARDS = ["wsAddCard", "projectsCard", "beadsBoardsCard"];
const view = el("div", "ws-view");
const ctx = {
  el, view,
  $: (id) => (id === "ws-view" ? view : null),
  document: { activeElement: null },
  workspacesCache: [{ name: "a" }, { name: "b" }],
  wsSection: "",
  wsRow: (w) => el("div", "ws-row", w.name),
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
// The registry list itself went with them.
ctx.wsSection = "workspaces";
ctx.renderWorkspaces();
const list = view.children.find((c) => c.cls === "ws-list");
assert.ok(list, "the Registered list is on Workspaces");
assert.equal(list.children[0].text, "Registered (2)");
ctx.wsSection = "";
ctx.renderWorkspaces();
assert.equal(view.children.some((c) => c.cls === "ws-list"), false,
             "the Registered list left General");
// The tab strip marks the section that is up.
ctx.wsSection = "workspaces";
ctx.renderWorkspaces();
const tabs = view.children.find((c) => c.cls.includes("settings-tabs"));
assert.deepEqual(tabs.children.filter((t) => t.cls.includes("on")).map((t) => t.text),
                 ["Workspaces"]);

/* ---- which data each section fetches ------------------------------------ */
const called = [];
const REFRESHERS = [
  "refreshHarnesses", "refreshLlmSettings", "refreshFaq", "refreshPromptPresets",
  "refreshPrompterSettings", "refreshStatusChecks", "refreshRagStatus",
  "refreshGhStatus", "refreshRelaySettings", "refreshBeadsBoards",
  "refreshProjects", "refreshProfileSettings", "refreshInstallOverview",
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
assert.deepEqual([...called].sort(), ["refreshBeadsBoards", "refreshProjects"]);

called.length = 0;
octx.openSettings("");
assert.equal(called.includes("refreshBeadsBoards"), false,
             "General no longer fetches the board listing it does not draw");
assert.equal(called.includes("refreshLlmSettings"), true);

console.log("settingsworkspaces_check ok");
