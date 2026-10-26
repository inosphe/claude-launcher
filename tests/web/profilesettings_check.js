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
  // Workspaces left the strip for a page of its own (#/workspaces).
  assert.equal(tabs.children.length, 5);
  assert.deepEqual(tabs.children.map((t) => t.text),
                   ["General", "Profiles", "Providers", "Install", "VS Code"]);
  assert.deepEqual(tabs.children.map((t) => t.href),
                   ["#/settings", "#/settings/profiles",
                    "#/settings/providers",
                    "#/settings/install", "#/settings/vscode"]);
  // The section that is up is the one that is not a link away from itself.
  assert.equal(tabs.children[0].cls.includes("on"), true);
  assert.equal(tabs.children[1].cls.includes("on"), false);
  context.wsSection = "profiles";
  tabs = context.settingsTabs();
  assert.equal(tabs.children[1].cls.includes("on"), true);
  context.wsSection = "vscode";
  tabs = context.settingsTabs();
  assert.deepEqual(tabs.children.map((t) => t.cls.includes("on")), [false, false, false, false, true]);

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
    "Profile", "Harness", "Provider", "Effort", "Permission mode", "Rules",
    "Pi TUI mode", "Directory",
  ]);
  assert.equal(rows[0].children[0].text, "work");
  assert.equal(rows[0].children[2].text, "deepseek");
  // `auto` in the file, `plan` being converged: shown as the move, not as a
  // value -- this is the state `claunch apply` closes.
  assert.equal(rows[0].children[4].text, "auto → plan");
  assert.equal(rows[0].children[4].cls.includes("profile-pending"), true);
  // ...and the rules column sits between it and the path, settled.
  assert.equal(rows[0].children[5].text, "guard ok");
  assert.equal(rows[0].children[5].cls.includes("profile-ok"), true);
  // Not a pi profile: the pi TUI mode column is a dash, then the path.
  assert.equal(rows[0].children[6].text, "—");
  assert.equal(rows[0].children[6].cls.includes("profile-muted"), true);
  assert.equal(rows[0].children[7].text, "C:\\profiles\\work");

  // Another harness gets a dash in both Claude Code columns: these are keys
  // it never reads, so an empty cell is the honest answer, not a zero.
  assert.equal(rows[1].children[4].text, "—");
  assert.equal(rows[1].children[4].cls.includes("profile-muted"), true);
  assert.equal(rows[1].children[5].text, "—");
  assert.equal(rows[1].children[5].cls.includes("profile-muted"), true);

  // A profile the daemon could not describe is reported, not dropped.
  assert.equal(rows[2].children[0].text, "broken");
  assert.match(rows[2].children[1].text, /unknown harness/);

  // --- the effort column --------------------------------------------------
  // No layer sets one in this fixture: the cell names the harness default
  // rather than showing a blank that reads like zero.
  assert.equal(rows[0].children[3].text, "harness default");
  assert.equal(rows[0].children[3].cls.includes("profile-muted"), true);
  assert.equal(rows[1].children[3].text, "harness default");

  // Three states told apart: inherited (resolved but not declared), declared,
  // and a launch the harness will refuse.
  const effortRow = { ...details()[0], reasoning_effort: "medium",
    reasoning_effort_declared: null, reasoning_effort_problem: null };
  assert.equal(context.reasoningEffortLabel(effortRow).text, "medium (inherited)");
  effortRow.reasoning_effort_declared = "medium";
  assert.equal(context.reasoningEffortLabel(effortRow).text, "medium");
  effortRow.reasoning_effort = null;
  assert.equal(context.reasoningEffortLabel(effortRow).text, "harness default");
  effortRow.reasoning_effort = "high";
  effortRow.reasoning_effort_problem =
    "harness 'kimi' cannot translate configured provider field(s): reasoning_effort";
  const refused = context.reasoningEffortLabel(effortRow);
  assert.match(refused.text, /launch refused/);
  assert.equal(refused.cls, "profile-pending");
  assert.match(refused.title, /cannot translate/);

  // Since claunch-2pthl a Pi provider that declares no openai_reasoning_format
  // launches without the level instead of being refused. The cell says which of
  // the two it is, and the message is the provider field to fill in -- a person
  // reading "launch refused" would go looking for a profile value to clear.
  effortRow.reasoning_effort_problem =
    "provider 'opengateway' declares reasoning_effort 'high' but no " +
    "openai_reasoning_format, so a launch runs without a reasoning level.";
  effortRow.reasoning_effort_refused = false;
  const omitted = context.reasoningEffortLabel(effortRow);
  assert.match(omitted.text, /omitted at launch/);
  assert.doesNotMatch(omitted.text, /refused/);
  assert.match(omitted.title, /openai_reasoning_format/);
  const omittedEditor = context.reasoningEffortEditor(effortRow);
  const omittedNote = all(omittedEditor).find((n) => /openai_reasoning_format/.test(n.text || ""));
  assert.equal(omittedNote.cls, "wf-warning");
  effortRow.reasoning_effort_refused = true;
  const refusedEditor = context.reasoningEffortEditor(effortRow);
  assert.equal(all(refusedEditor).find((n) => /openai_reasoning_format/.test(n.text || "")).cls,
               "wf-error");
  delete effortRow.reasoning_effort_refused;

  // The selector edits the profile's own key: the levels are the ones the
  // daemon published, and "harness default" is the undeclaration.
  response = { profiles: [], profile_details: details(),
    reasoning_effort_choices: ["low", "medium", "high"] };
  await context.refreshProfileSettings();
  const editorRow = { ...details()[0], reasoning_effort: null,
    reasoning_effort_declared: null, reasoning_effort_problem: null };
  let effortEditor = context.reasoningEffortEditor(editorRow);
  let effortSelect = find(effortEditor, (n) => n.tag === "select");
  assert.deepEqual(effortSelect.children.map((o) => o.value),
                   ["", "low", "medium", "high"]);
  assert.equal(effortSelect.children[0].text, "(harness default)");
  assert.equal(effortSelect.value, "");
  assert.match(textOf(effortEditor), /Applies to new sessions/);

  requests.length = 0;
  writeResponse = { profile: "work", declared: "medium",
    reasoning_effort: "medium", reasoning_effort_problem: null };
  effortSelect.value = "medium";
  await find(effortEditor, (n) => n.tag === "form").handlers.submit({ preventDefault() {} });
  assert.equal(requests[0][0], "/api/profiles/reasoning-effort");
  assert.equal(requests[0][1].method, "POST");
  assert.deepEqual(JSON.parse(requests[0][1].body), { profile: "work", effort: "medium" });
  assert.match(textOf(context.reasoningEffortEditor(editorRow)),
               /launch at medium effort/);

  // Undeclaring sends null, which removes the key instead of writing "none".
  requests.length = 0;
  writeResponse = { profile: "work", declared: null,
    reasoning_effort: null, reasoning_effort_problem: null };
  await context.reasoningEffortApply("work", "");
  assert.deepEqual(JSON.parse(requests[0][1].body), { profile: "work", effort: null });
  assert.match(textOf(context.reasoningEffortEditor(editorRow)),
               /use the harness default/);

  // A value written by hand outside the published levels stays selectable,
  // so the next Save cannot silently rewrite it.
  const handWritten = { ...details()[0], reasoning_effort: "xhigh",
    reasoning_effort_declared: "xhigh", reasoning_effort_problem: null };
  effortEditor = context.reasoningEffortEditor(handWritten);
  effortSelect = find(effortEditor, (n) => n.tag === "select");
  assert.equal(effortSelect.value, "xhigh");
  assert.equal(effortSelect.children.at(-1).value, "xhigh");

  // --- converged reads as settled ----------------------------------------
  response = { profiles: [], profile_details: details({ declared: "auto", target: "auto", first: "auto" }) };
  await context.refreshProfileSettings();
  const settled = all(find(context.profilesPanel(), (n) => n.tag === "table"))
    .filter((n) => n.tag === "tr")[1];
  assert.equal(settled.children[4].text, "auto");
  assert.equal(settled.children[4].cls.includes("profile-ok"), true);

  // --- a profile with no value at all ------------------------------------
  response = { profiles: [], profile_details: details({ declared: null, target: "auto", first: null }) };
  await context.refreshProfileSettings();
  const asking = all(find(context.profilesPanel(), (n) => n.tag === "table"))
    .filter((n) => n.tag === "tr")[1];
  assert.equal(asking.children[4].text, "asks (no value)");

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
  assert.equal(shortRows[0].children[5].text, "deny: 4 missing");
  assert.equal(shortRows[0].children[5].cls.includes("profile-pending"), true);
  assert.equal(shortRows[1].children[5].text, "allow: 1 missing · deny: 4 missing");

  // --- no Claude profile at all ------------------------------------------
  response = { profiles: [], profile_details: [details()[2], details()[3]] };
  await context.refreshProfileSettings();
  assert.match(textOf(context.profileModeCard()), /No Claude Code profile yet/);
  assert.match(textOf(context.permissionRulesCard()), /No Claude Code profile yet/);

  // --- pi's TUI mode ------------------------------------------------------
  // Only a pi profile's row carries `pi_tui_mode`; the cell says what the next
  // session gets and why, and the selector edits the profile's own declaration.
  const piTui = (over = {}) => ({ key: "tuiMode", declared: null, effective: null,
    file_value: null, next: "fullscreen", source: "pi-default",
    default: "fullscreen", modes: ["regular", "fullscreen"], error: null, ...over });
  const piRow = { name: "glm", profile: "glm", harness: "pi", explicit: false,
    directory: "C:\\profiles\\glm", harness_policy: { provider: "fireworks" },
    permission_mode: null, permission_rules: null, pi_tui_mode: piTui() };
  response = { profile_details: [piRow] };
  await context.refreshProfileSettings();
  let piCells = all(find(context.profilesPanel(), (n) => n.tag === "table"))
    .filter((n) => n.tag === "tr")[1].children;
  assert.equal(piCells[4].text, "—");
  assert.match(piCells[6].text, /^fullscreen \(pi's default\)$/);
  let tuiEditor = context.piTuiModeEditor(piRow);
  const tuiSelect = find(tuiEditor, (n) => n.tag === "select");
  assert.deepEqual(tuiSelect.children.map((o) => o.value), ["", "regular", "fullscreen"]);
  assert.equal(tuiSelect.children[0].text, "Default (fullscreen)");
  assert.equal(tuiSelect.value, "");
  assert.match(textOf(tuiEditor), /Applies to new sessions/);

  requests.length = 0;
  writeResponse = { profile: "glm", declared: "regular",
    pi_tui_mode: piTui({ declared: "regular", effective: "regular", next: "regular", source: "profile" }) };
  tuiSelect.value = "regular";
  await find(tuiEditor, (n) => n.tag === "form").handlers.submit({ preventDefault() {} });
  assert.equal(requests[0][0], "/api/profiles/pi-tui-mode");
  assert.equal(requests[0][1].method, "POST");
  assert.deepEqual(JSON.parse(requests[0][1].body), { profile: "glm", mode: "regular" });
  assert.match(textOf(context.piTuiModeEditor(piRow)), /new sessions of glm start in regular mode/);

  // Back to the default sends null, the same undeclare the mode card uses.
  requests.length = 0;
  writeResponse = { profile: "glm", declared: null, pi_tui_mode: piTui() };
  await context.piTuiModeApply("glm", "");
  assert.deepEqual(JSON.parse(requests[0][1].body), { profile: "glm", mode: null });

  // A hand-written file value the declaration will overwrite is said so.
  const overwritten = { ...piRow, pi_tui_mode: piTui({ declared: "fullscreen",
    effective: "fullscreen", file_value: "regular", next: "fullscreen", source: "profile" }) };
  tuiEditor = context.piTuiModeEditor(overwritten);
  assert.equal(find(tuiEditor, (n) => n.tag === "select").value, "fullscreen");
  assert.match(textOf(tuiEditor),
    /settings.json holds regular; the declared fullscreen overwrites it at the next launch/);
  response = { profile_details: [{ ...piRow, pi_tui_mode: piTui({
    file_value: "regular", next: "regular", source: "file" }) }] };
  await context.refreshProfileSettings();
  piCells = all(find(context.profilesPanel(), (n) => n.tag === "table"))
    .filter((n) => n.tag === "tr")[1].children;
  assert.equal(piCells[6].text, "regular (hand-written in settings.json)");

  ok = false;
  writeResponse = { error: "TUI mode is only available for profiles that can run pi" };
  await context.piTuiModeApply("glm", "regular");
  assert.match(textOf(context.piTuiModeEditor(piRow)), /only available for profiles that can run pi/);
  ok = true;

  console.log("profilesettings_check: ok");
})();
