/* New-session's Borrow row follows the harness encoded in PROFILE:HARNESS;
   there is no second Harness row. This is intentionally separate from spawn
   checks: no parent/policy exists here to explain an accidentally grey row. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  const head = src.slice(start - 6, start) === "async " ? start - 6 : start;
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let i = body; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (!depth) return src.slice(head, i + 1); }
  }
  throw new Error("unbalanced " + name);
}

const ctl = (over = {}) => Object.assign({ value: "", disabled: false, checked: false }, over);
const form = {
  profile: ctl({ value: "work:pi" }), harness: ctl({ value: "pi", disabled: true }),
  fork: ctl(), role: ctl(), resume: ctl(), null_token: ctl(), borrow: ctl({ value: "ds4" }),
};
const details = {
  "work:pi": { harness: "pi", borrow_allowed: true, borrow_mode: "token" },
  "work:kimi": { harness: "kimi", borrow_allowed: false, borrow_mode: "none" },
  "work:claude": { harness: "claude", borrow_allowed: true, borrow_mode: "provider-token" },
};
const ctx = {};
new Function(
  "exports", "$", "profileDetails",
  `function spawnParent() { return null; }
function renderRoleStance() {}
function syncSpawnMode() {}
function renderRuntimeSummary() {}
function renderProfileHint() {}
` + slice("profileHarnessLabel") + slice("profileBorrowCapability") +
slice("profileHarnessName") +
slice("syncForkAvailability") + `
exports.sync = syncForkAvailability;
exports.label = profileHarnessLabel;`
)(ctx, (id) => id === "new-session" ? form : null, details);

let failures = 0;
function check(label, condition, extra) {
  if (condition) return;
  failures++;
  console.error(`FAIL ${label}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
}

ctx.sync();
check("Pi can borrow the shared token", form.borrow.disabled === false);
check("Pi still cannot use Claude --null", form.null_token.disabled === true);
check("the lender survives the Pi sync", form.borrow.value === "ds4", form.borrow.value);

form.profile.value = "work:kimi";
form.harness.value = "kimi";
form.borrow.value = "ds4";
ctx.sync();
check("OAuth harness locks Borrow", form.borrow.disabled === true);
check("and clears a stale lender", form.borrow.value === "");

form.profile.value = "work:claude";
form.harness.value = "claude";
form.borrow.value = "ds4";
form.null_token.checked = true;
ctx.sync();
check("Claude --null locks Borrow", form.borrow.disabled === true);
check("and clears the contradictory lender", form.borrow.value === "");
check("a canonical selector is displayed once",
  ctx.label("ds4:claude", "claude") === "ds4/claude");
check("a bare legacy selector gets the same display",
  ctx.label("ds4", "claude") === "ds4/claude");
check("a mismatched stored pair remains visible",
  ctx.label("ds4:pi", "claude") === "ds4:pi/claude");

/* The main Web create form consumes the labelled, policy-filtered option
   objects. Keep a denied compatibility selector in profile_selectors to
   prove that profile_options is authoritative when the daemon publishes it. */
async function checkProfilePicker() {
  const profileSelect = {
    kids: [], value: "",
    set innerHTML(value) { this.kids = []; },
    get innerHTML() { return ""; },
    appendChild(child) { this.kids.push(child); return child; },
    get options() { return this.kids; },
  };
  const pickerDoc = {
    createElement: () => ({ value: "", textContent: "", title: "", disabled: false }),
    querySelector: (selector) => selector.includes("name=profile") ? profileSelect : null,
  };
  const pickerApi = async () => ({
    ok: true, status: 200,
    json: async () => ({
      profile_selectors: ["codex:codex", "codex:claude", "work:claude"],
      profile_options: [
        { value: "codex:codex", label: "codex/codex", harness: "codex" },
        { value: "work:claude", label: "work/claude", harness: "claude" },
      ],
      profile_details: [
        { name: "codex", harness: "codex" },
        { name: "codex:claude", harness: "claude", harness_allowed: false },
      ],
    }),
  });
  const picker = {};
  new Function(
    "exports", "api", "document", "syncNewBorrowOptions", "syncForkAvailability",
    `let profileDetails = {};` + slice("refreshProfiles") + `
exports.refresh = refreshProfiles;
exports.details = () => profileDetails;`
  )(
    picker, pickerApi, pickerDoc, async () => {}, () => {}
  );
  await picker.refresh();
  check("the create picker displays the canonical default once",
    profileSelect.options.some((o) =>
      o.value === "codex:codex" && o.textContent === "codex/codex"));
  check("the create picker omits codex:claude",
    !profileSelect.options.some((o) => o.value === "codex:claude"),
    profileSelect.options.map((o) => o.value));
}

async function checkBorrowAuthModes() {
  const borrow = {
    kids: [], value: "", disabled: false, title: "",
    set innerHTML(value) { this.kids = []; },
    get innerHTML() { return ""; },
    appendChild(child) { this.kids.push(child); return child; },
    get options() { return this.kids; },
  };
  const authForm = { profile: { value: "work:claude" }, borrow };
  const authDocument = {
    createElement: () => ({ value: "", textContent: "", title: "", disabled: false }),
  };
  const authDetails = {
    "work:claude": { harness: "claude", borrow_allowed: true,
                       borrow_mode: "provider-token" },
    "codex:codex": { harness: "codex", borrow_allowed: false,
                       borrow_mode: "none" },
  };
  const authHarnesses = {
    claude: { auth: "claude" }, codex: { auth: "oauth" },
  };
  let parent = null;
  const authApi = async (url) => ({
    ok: true, status: 200,
    json: async () => url.includes("codex%3Acodex")
      ? { capability: { allowed: false, mode: "none" }, options: [] }
      : { options: [
          { name: "work", label: "work", selectable: true, message: "ready" },
          { name: "ds4", label: "ds4", selectable: true, message: "ready" },
        ] },
  });
  const auth = {};
  new Function(
    "exports", "api", "document", "form", "parentNow", "details", "harnesses",
    `let newBorrowFor = null, newBorrowSeq = 0;
let profileDetails = details;
let harnessDetails = harnesses;
function $(id) { return id === "new-session" ? form : null; }
function spawnParent() { return parentNow(); }
function syncSpawnMode() {}
function syncForkAvailability() {}
` + slice("baseProfileName") + slice("profileBorrowCapability")
    + slice("profileOwnAuthLabel") + slice("profileHarnessName")
    + slice("readBorrowOptions")
    + slice("fillValidatedBorrow") + slice("syncNewBorrowOptions") + `
exports.sync = syncNewBorrowOptions;`
  )(
    auth, authApi, authDocument, authForm, () => parent,
    authDetails, authHarnesses
  );

  await auth.sync(true);
  check("new-session folds its base profile into the own-token choice",
    borrow.options.map((o) => o.value).join(",") === ",ds4",
    borrow.options.map((o) => o.value));

  parent = { name: "lead", profile: "other:claude" };
  await auth.sync(true);
  const values = borrow.options.map((o) => o.value);
  check("a parented form separates inherit and the profile's own token",
    values.join(",") === ",work,ds4", values);
  const own = borrow.options.find((o) => o.value === "work");
  check("the profile's own token is a head answer, not a lender row",
    own !== undefined && own.textContent === "work's own token" && !own.disabled,
    own && [own.textContent, own.disabled]);
  check("the duplicate lender row is folded into that head answer",
    borrow.options.filter((o) => o.value === "work").length === 1, values);

  authForm.profile.value = "codex:codex";
  await auth.sync(true);
  check("an OAuth profile replaces the parent-auth head with its own login",
    borrow.options.length === 1 && borrow.options[0].value === "" &&
      borrow.options[0].textContent ===
        "(codex/codex profile's own OAuth login)",
    borrow.options.map((o) => [o.value, o.textContent]));
}

async function checkSpawnModalBorrowHead() {
  const mkBorrow = () => ({
    kids: [], value: "", disabled: false, title: "",
    set innerHTML(value) { this.kids = []; },
    get innerHTML() { return ""; },
    appendChild(child) { this.kids.push(child); return child; },
    get options() { return this.kids; },
  });
  const mkUi = () => ({
    profile: { value: "" },           // inherit the parent's profile
    parentSess: { profile: "work:claude", harness: "claude" },
    profileDetails: {
      "work:claude": { harness: "claude", borrow_allowed: true,
                         borrow_mode: "provider-token" },
    },
    borrow: mkBorrow(),
  });
  let baseSelectable = true;
  const spawnApi = async () => ({
    ok: true, status: 200,
    json: async () => ({ options: [
      { name: "work", label: "work", selectable: baseSelectable,
        message: baseSelectable ? "ready" : "no usable token" },
      { name: "ds4", label: "ds4", selectable: true, message: "ready" },
    ] }),
  });
  const spawn = {};
  new Function(
    "exports", "api", "document",
    `let spawnModal = null;
function syncSpawnGates() {}
function spawnModalClose() {}
` + slice("baseProfileName") + slice("profileBorrowCapability")
    + slice("profileOwnAuthLabel") + slice("fillValidatedBorrow")
    + slice("readBorrowOptions") + slice("refreshSpawnBorrowOptions") + `
exports.refresh = refreshSpawnBorrowOptions;
exports._setModal = (s) => { spawnModal = s; };`
  )(spawn, spawnApi,
    { createElement: () => ({ value: "", textContent: "", title: "", disabled: false }) });

  const st = { ui: mkUi(), parent: "lead" };
  spawn._setModal(st);
  await spawn.refresh(st, true);
  const values = borrow2values(st.ui.borrow);
  check("the spawn modal heads with inherit, then the profile's own token",
    values.join(",") === ",work,ds4", values);
  const own = st.ui.borrow.options.find((o) => o.value === "work");
  check("the spawn modal's own answer names the base profile",
    own !== undefined && own.textContent === "work's own token" && !own.disabled,
    own && [own.textContent, own.disabled]);
  check("the spawn modal folds the duplicate lender row",
    st.ui.borrow.options.filter((o) => o.value === "work").length === 1, values);

  baseSelectable = false;
  const st2 = { ui: mkUi(), parent: "lead" };
  spawn._setModal(st2);
  await spawn.refresh(st2, true);
  const own2 = st2.ui.borrow.options.find((o) => o.value === "work");
  check("an unusable own token is greyed with the lender's reason",
    own2 !== undefined && own2.disabled && /no usable token/.test(own2.title || ""),
    own2 && [own2.disabled, own2.title]);
}

function borrow2values(select) {
  return select.options.map((o) => o.value);
}

Promise.all([
  checkProfilePicker(), checkBorrowAuthModes(), checkSpawnModalBorrowHead(),
]).then(() => {
  console.log("borrowform_check: " + (failures ? `${failures} failing` : "ok"));
  process.exitCode = failures ? 1 : 0;
}).catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
