// The profile manager section of the Settings page: its tab strip, the
// permission-mode card, and the per-profile table. Drives the real card
// functions out of app.js with a stubbed `api`, the way
// relaysettings_check.js does — the page has no DOM test harness, and the
// thing worth pinning here is what the card *asks the daemon for* and what
// it does with the answer, not how the browser paints it.

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

// One Claude profile holding `auto`, one whose convergence has not run, and
// one on another harness -- the three states the table has to tell apart.
function details({ declared = "auto", target = "auto", first = "auto" } = {}) {
  return [
    { name: "work", profile: "work", harness: "claude", explicit: false,
      directory: "C:\\profiles\\work",
      harness_policy: { provider: "deepseek" },
      permission_mode: { key: "permissions.defaultMode", value: first,
        target, declared, source: declared === null ? "claunch-default" : "declared",
        converged: first === target,
        modes: ["default", "manual", "acceptEdits", "plan", "auto", "bypassPermissions"] } },
    { name: "work:codex", profile: "work", harness: "codex", explicit: true,
      permission_mode: null },
    { name: "cx", profile: "cx", harness: "codex", explicit: false,
      directory: "C:\\profiles\\cx",
      harness_policy: { provider: "" }, permission_mode: null },
    { name: "broken", error: "profile 'broken' selects unknown harness 'nope'" },
  ];
}

// Two payloads, because the card reads and writes different endpoints and a
// single shared stub would answer the listing with the write's reply.
let response = {};
let writeResponse = {};
let ok = true;
const requests = [];
const context = vm.createContext({
  el,
  document: { createTextNode: (text) => el("text", null, text) },
  wsOpen: true,
  wsSection: "",
  renderWorkspaces() {},
  api: async (url, options) => {
    requests.push([url, options]);
    return {
      ok,
      json: async () => (url === "/api/profiles" ? response : writeResponse),
    };
  },
});

vm.runInContext(
  src.slice(src.indexOf("let profilesCache = []"),
            src.indexOf("function renderWorkspaces() {")),
  context);

(async () => {
  // --- the tab strip -----------------------------------------------------
  context.wsSection = "";
  let tabs = context.settingsTabs();
  assert.equal(tabs.children.length, 2);
  assert.deepEqual(tabs.children.map((t) => t.text), ["General", "Profiles"]);
  assert.deepEqual(tabs.children.map((t) => t.href),
                   ["#/settings", "#/settings/profiles"]);
  // The section that is up is the one that is not a link away from itself.
  assert.equal(tabs.children[0].cls.includes("on"), true);
  assert.equal(tabs.children[1].cls.includes("on"), false);
  context.wsSection = "profiles";
  tabs = context.settingsTabs();
  assert.equal(tabs.children[1].cls.includes("on"), true);

  // --- the card, declared ------------------------------------------------
  response = { profiles: [], profile_details: details() };
  await context.refreshProfiles();
  assert.equal(requests.at(-1)[0], "/api/profiles");

  let card = context.profileModeCard();
  const select = find(card, (n) => n.id === "profile-mode-select");
  assert.equal(select.value, "auto");
  // Every mode the daemon accepts is offered, plus the "no declaration" row.
  assert.deepEqual(select.children.map((o) => o.value),
                   ["", "default", "manual", "acceptEdits", "plan", "auto", "bypassPermissions"]);
  // A declared value says it is declared; the packaged default is named too,
  // so the page never shows a blank where a decision should be.
  assert.match(textOf(card), /Declared: auto/);
  assert.match(textOf(card), /converges to auto/);

  // --- the card, nothing declared ----------------------------------------
  response = { profiles: [], profile_details: details({ declared: null }) };
  await context.refreshProfiles();
  card = context.profileModeCard();
  assert.match(textOf(card), /Declared: nothing \(claunch's default\)/);
  assert.equal(find(card, (n) => n.id === "profile-mode-select").value, "");

  // --- applying it -------------------------------------------------------
  requests.length = 0;
  writeResponse = { key: "permissions.defaultMode", declared: "plan", target: "plan",
                    converged: ["work"], unchanged: ["cx"], failed: [] };
  await context.profileModeApply("plan");
  const [url, options] = requests[0];
  assert.equal(url, "/api/profiles/permission-mode");
  assert.equal(options.method, "POST");
  assert.equal(JSON.parse(options.body).mode, "plan");
  // ...and the result is reported as counts, because "wrote it nowhere" and
  // "wrote it everywhere" are both HTTP 200. Read off the rendered card: the
  // notice is a `let` in the script's own scope, not a property of the
  // context object, so the card is the only door to it from out here.
  assert.match(textOf(context.profileModeCard()), /declared plan/);
  assert.match(textOf(context.profileModeCard()), /wrote it into 1 profile\(s\)/);
  assert.match(textOf(context.profileModeCard()), /1 already had it/);

  // --- undeclaring sends null, not the empty string ----------------------
  requests.length = 0;
  writeResponse = { declared: null, target: "auto", converged: ["work"],
                    unchanged: [], failed: [] };
  await context.profileModeApply("");
  assert.equal(JSON.parse(requests[0][1].body).mode, null);
  assert.match(textOf(context.profileModeCard()), /back on claunch's default \(auto\)/);

  // --- a failure is surfaced, not swallowed ------------------------------
  ok = false;
  writeResponse = { error: "unknown permission mode 'nope'" };
  await context.profileModeApply("nope");
  assert.match(textOf(context.profileModeCard()), /unknown permission mode/);
  ok = true;

  // --- the table ---------------------------------------------------------
  response = { profiles: [], profile_details: details({ declared: "plan", target: "plan" }) };
  await context.refreshProfiles();
  const panel = context.profilesPanel();
  const table = find(panel, (n) => n.tag === "table");
  const rows = all(table).filter((n) => n.tag === "tr").slice(1);  // drop the header

  // One row per profile: the per-harness selector row is not a profile.
  assert.equal(rows.length, 3);
  assert.equal(rows[0].children[0].text, "work");
  assert.equal(rows[0].children[2].text, "deepseek");
  // `auto` in the file, `plan` being converged: shown as the move, not as a
  // value -- this is the state `claunch apply` closes.
  assert.equal(rows[0].children[3].text, "auto → plan");
  assert.equal(rows[0].children[3].cls.includes("profile-pending"), true);
  assert.equal(rows[0].children[4].text, "C:\\profiles\\work");

  // Another harness gets a dash: this is a Claude Code key it never reads.
  assert.equal(rows[1].children[3].text, "—");
  assert.equal(rows[1].children[3].cls.includes("profile-muted"), true);

  // A profile the daemon could not describe is reported, not dropped.
  assert.equal(rows[2].children[0].text, "broken");
  assert.match(rows[2].children[1].text, /unknown harness/);

  // --- converged reads as settled ----------------------------------------
  response = { profiles: [], profile_details: details({ declared: "auto", target: "auto", first: "auto" }) };
  await context.refreshProfiles();
  const settled = all(find(context.profilesPanel(), (n) => n.tag === "table"))
    .filter((n) => n.tag === "tr")[1];
  assert.equal(settled.children[3].text, "auto");
  assert.equal(settled.children[3].cls.includes("profile-ok"), true);

  // --- a profile with no value at all ------------------------------------
  response = { profiles: [], profile_details: details({ declared: null, target: "auto", first: null }) };
  await context.refreshProfiles();
  const asking = all(find(context.profilesPanel(), (n) => n.tag === "table"))
    .filter((n) => n.tag === "tr")[1];
  assert.equal(asking.children[3].text, "asks (no value)");

  // --- no Claude profile at all ------------------------------------------
  response = { profiles: [], profile_details: [details()[2], details()[3]] };
  await context.refreshProfiles();
  assert.match(textOf(context.profileModeCard()), /No Claude Code profile yet/);

  console.log("profilesettings_check: ok");
})();
