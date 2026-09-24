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
// The gate guard's two halves as the daemon reports them: what claunch plants,
// what is missing from it, and what -- if anything -- the shared layer
// declares. Only the two halves' presence changes between cases; the shape
// does not, because the card and the table both read the answer off `missing`.
function rules({ allow = true, deny = true } = {}) {
  const parts = {
    allow: { key: "permissions.allow", expected: ["mcp__claunch"],
      declared: ["mcp__claunch"] },
    deny: { key: "permissions.deny", declared: null,
      expected: ["Bash(claunch cflow approve)", "Bash(claunch cflow approve:*)",
        "PowerShell(claunch cflow approve)",
        "PowerShell(claunch cflow approve:*)"] },
  };
  for (const [half, whole] of [["allow", allow], ["deny", deny]]) {
    const part = parts[half];
    part.value = whole ? part.expected.slice() : [];
    part.missing = whole ? [] : part.expected.slice();
    part.converged = whole;
  }
  return { ...parts, converged: allow && deny };
}

function details({ declared = "auto", target = "auto", first = "auto" } = {}) {
  return [
    { name: "work", profile: "work", harness: "claude", explicit: false,
      directory: "C:\\profiles\\work",
      harness_policy: { provider: "deepseek" },
      permission_mode: { key: "permissions.defaultMode", value: first,
        target, declared, source: declared === null ? "claunch-default" : "declared",
        converged: first === target,
        modes: ["default", "manual", "acceptEdits", "plan", "auto", "bypassPermissions"] },
      permission_rules: rules() },
    { name: "work:codex", profile: "work", harness: "codex", explicit: true,
      permission_mode: null, permission_rules: null },
    { name: "cx", profile: "cx", harness: "codex", explicit: false,
      directory: "C:\\profiles\\cx",
      harness_policy: { provider: "" }, permission_mode: null,
      permission_rules: null },
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
  assert.equal(tabs.children.length, 4);
  assert.deepEqual(tabs.children.map((t) => t.text),
                   ["General", "Workspaces", "Profiles", "Install"]);
  assert.deepEqual(tabs.children.map((t) => t.href),
                   ["#/settings", "#/settings/workspaces", "#/settings/profiles",
                    "#/settings/install"]);
  // The section that is up is the one that is not a link away from itself.
  assert.equal(tabs.children[0].cls.includes("on"), true);
  assert.equal(tabs.children[2].cls.includes("on"), false);
  context.wsSection = "profiles";
  tabs = context.settingsTabs();
  assert.equal(tabs.children[2].cls.includes("on"), true);
  context.wsSection = "workspaces";
  tabs = context.settingsTabs();
  assert.deepEqual(tabs.children.map((t) => t.cls.includes("on")), [false, true, false, false]);

  // --- the card, declared ------------------------------------------------
  response = { profiles: [], profile_details: details() };
  await context.refreshProfileSettings();
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

  // --- the rules card ----------------------------------------------------
  // Both halves are named with the counts behind them, because "has some
  // rules" and "has the guard" are the two facts this card exists to tell
  // apart. Read-only: putting the rules there is `claunch install`'s job.
  let rulesCard = context.permissionRulesCard();
  assert.match(textOf(rulesCard), /1 allow rule\(s\), 4 deny rule\(s\)/);
  assert.match(textOf(rulesCard), /allow: mcp__claunch/);
  assert.match(textOf(rulesCard), /deny: nothing declared/);
  assert.match(textOf(rulesCard), /All 1 Claude Code profile\(s\) carry the allow half\./);
  assert.match(textOf(rulesCard), /All 1 Claude Code profile\(s\) carry the deny half\./);
  assert.equal(all(rulesCard).filter((n) => n.tag === "button").length, 0);
  assert.equal(all(rulesCard).filter((n) => n.tag === "form").length, 0);

  // --- the card, nothing declared ----------------------------------------
  response = { profiles: [], profile_details: details({ declared: null }) };
  await context.refreshProfileSettings();
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
  await context.refreshProfileSettings();
  const panel = context.profilesPanel();
  const table = find(panel, (n) => n.tag === "table");
  const rows = all(table).filter((n) => n.tag === "tr").slice(1);  // drop the header

  // One row per profile: the per-harness selector row is not a profile.
  assert.equal(rows.length, 3);
  assert.deepEqual(all(table).filter((n) => n.tag === "th").map((n) => n.text), [
    "Profile", "Harness", "Provider", "Permission mode", "Rules", "Directory",
  ]);
  assert.equal(rows[0].children[0].text, "work");
  assert.equal(rows[0].children[2].text, "deepseek");
  // `auto` in the file, `plan` being converged: shown as the move, not as a
  // value -- this is the state `claunch apply` closes.
  assert.equal(rows[0].children[3].text, "auto → plan");
  assert.equal(rows[0].children[3].cls.includes("profile-pending"), true);
  // ...and the rules column sits between it and the path, settled.
  assert.equal(rows[0].children[4].text, "guard ok");
  assert.equal(rows[0].children[4].cls.includes("profile-ok"), true);
  assert.equal(rows[0].children[5].text, "C:\\profiles\\work");

  // Another harness gets a dash in both Claude Code columns: these are keys
  // it never reads, so an empty cell is the honest answer, not a zero.
  assert.equal(rows[1].children[3].text, "—");
  assert.equal(rows[1].children[3].cls.includes("profile-muted"), true);
  assert.equal(rows[1].children[4].text, "—");
  assert.equal(rows[1].children[4].cls.includes("profile-muted"), true);

  // A profile the daemon could not describe is reported, not dropped.
  assert.equal(rows[2].children[0].text, "broken");
  assert.match(rows[2].children[1].text, /unknown harness/);

  // --- converged reads as settled ----------------------------------------
  response = { profiles: [], profile_details: details({ declared: "auto", target: "auto", first: "auto" }) };
  await context.refreshProfileSettings();
  const settled = all(find(context.profilesPanel(), (n) => n.tag === "table"))
    .filter((n) => n.tag === "tr")[1];
  assert.equal(settled.children[3].text, "auto");
  assert.equal(settled.children[3].cls.includes("profile-ok"), true);

  // --- a profile with no value at all ------------------------------------
  response = { profiles: [], profile_details: details({ declared: null, target: "auto", first: null }) };
  await context.refreshProfileSettings();
  const asking = all(find(context.profilesPanel(), (n) => n.tag === "table"))
    .filter((n) => n.tag === "tr")[1];
  assert.equal(asking.children[3].text, "asks (no value)");

  // A profile override does not become the shared card's selected value.
  const scoped = details({ declared: "plan", target: "plan", first: "plan" });
  Object.assign(scoped[0].permission_mode, {
    override: "plan", source: "profile", shared_declared: "acceptEdits",
    shared_target: "acceptEdits", packaged_default: "auto",
  });
  scoped.push({ ...scoped[0], profile: "second", name: "second",
    permission_mode: { ...scoped[0].permission_mode, override: null } });
  response = { profile_details: scoped };
  await context.refreshProfileSettings();
  card = context.profileModeCard();
  assert.equal(find(card, (n) => n.id === "profile-mode-select").value, "acceptEdits");
  assert.match(textOf(card), /claunch default \(auto\)/);
  assert.match(textOf(card), /Profile overrides below are preserved/);
  let editor = context.profileModeEditor(scoped[0]);
  assert.equal(find(editor, (n) => n.tag === "select").value, "plan");
  assert.match(textOf(editor), /Use shared default \(acceptEdits\)/);
  assert.match(textOf(editor), /Apply to profile/);

  // Submit the actual per-profile form, including its target and result scope.
  requests.length = 0;
  writeResponse = { profile: "work", declared: "auto", target: "auto",
    converged: ["work"], unchanged: [], failed: [] };
  find(editor, (n) => n.tag === "select").value = "auto";
  await find(editor, (n) => n.tag === "form").handlers.submit({ preventDefault() {} });
  assert.deepEqual(JSON.parse(requests[0][1].body), { profile: "work", mode: "auto" });
  assert.match(textOf(context.profileModeEditor(scoped[0])), /declared auto/);
  assert.doesNotMatch(textOf(context.profileModeEditor(scoped.at(-1))), /declared auto/);
  assert.doesNotMatch(textOf(context.profileModeCard()), /wrote it into/);

  requests.length = 0;
  writeResponse = { profile: "work", declared: null, target: "acceptEdits",
    converged: ["work"], unchanged: [], failed: [] };
  await context.profileModeApply("", "work");
  assert.deepEqual(JSON.parse(requests[0][1].body), { profile: "work", mode: null });
  assert.match(textOf(context.profileModeEditor(scoped[0])), /using shared default \(acceptEdits\)/);
  assert.equal(all(context.profilesPanel()).filter((n) => n.tag === "button" && n.text === "Apply to profile").length, 2);

  writeResponse = { profile: "work", declared: "plan", target: "plan",
    converged: [], unchanged: [], failed: [{ profile: "work", reason: "write failed" }] };
  await context.profileModeApply("plan", "work");
  assert.match(textOf(context.profileModeEditor(scoped[0])), /FAILED on work: write failed/);

  // --- a gap is named, not just flagged ----------------------------------
  // The boundary the card exists for: one profile short of the guard, one
  // short of both halves. Which profiles, by name -- a count alone does not
  // say which settings.json to go and look at.
  const short = [details()[0]];
  short[0].permission_rules = rules({ deny: false });
  short.push({ ...short[0], profile: "bare", name: "bare",
               permission_rules: rules({ allow: false, deny: false }) });
  response = { profile_details: short };
  await context.refreshProfileSettings();
  rulesCard = context.permissionRulesCard();
  assert.match(textOf(rulesCard),
    /1 of 2 profile\(s\): the claunch MCP server is not allowed — bare/);
  assert.match(textOf(rulesCard),
    /2 of 2 profile\(s\): the gate guard is incomplete — work, bare/);
  assert.doesNotMatch(textOf(rulesCard), /carry the allow half/);

  // The same two states in the table, told apart by which rules are absent
  // rather than by a boolean -- a half-planted guard is the case this shape
  // is for.
  const shortRows = all(find(context.profilesPanel(), (n) => n.tag === "table"))
    .filter((n) => n.tag === "tr").slice(1);
  assert.equal(shortRows[0].children[4].text, "deny: 4 missing");
  assert.equal(shortRows[0].children[4].cls.includes("profile-pending"), true);
  assert.equal(shortRows[1].children[4].text, "allow: 1 missing · deny: 4 missing");

  // --- no Claude profile at all ------------------------------------------
  response = { profiles: [], profile_details: [details()[2], details()[3]] };
  await context.refreshProfileSettings();
  assert.match(textOf(context.profileModeCard()), /No Claude Code profile yet/);
  assert.match(textOf(context.permissionRulesCard()), /No Claude Code profile yet/);

  console.log("profilesettings_check: ok");
})();
