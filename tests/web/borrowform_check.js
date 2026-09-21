/* The session form's Borrow row follows the Profile + Harness pair, and the
   two controls recombine to PROFILE:HARNESS for the daemon. Both sides of
   the form are driven here, because one function answers for both now: with
   no parent the base profile folds into the own-token choice, and with one
   the inherited arrangement and the profile's own token are two separate
   head answers. The spawn wizard used to own a second copy of this rule
   (refreshSpawnBorrowOptions) and it was checked separately; there is one
   form and one rule now, so there is one set of checks. */
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
  profile: ctl({ value: "work" }), harness: ctl({ value: "pi" }),
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
  `let newProfileOptions = [];
function spawnParent() { return null; }
function renderRoleStance() {}
function syncSpawnMode() {}
function renderNewClaudeRuntime() {}
function renderNewCodexRuntime() {}
function renderNewPiRuntime() {}
function renderRuntimeSummary() {}
function renderProfileHint() {}
function syncNewModelOptions() {}
` + slice("baseProfileName") + slice("spawnProfileSelector") +
slice("newProfileUi") + slice("newProfileSelector") +
slice("newProfileDetail") + slice("newProfileHarnessName") +
slice("profileHarnessLabel") + slice("profileBorrowCapability") +
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

form.harness.value = "kimi";
form.borrow.value = "ds4";
ctx.sync();
check("OAuth harness locks Borrow", form.borrow.disabled === true);
check("and clears a stale lender", form.borrow.value === "");

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
  const makeSelect = () => ({
    kids: [], value: "", disabled: false,
    set innerHTML(value) { this.kids = []; },
    get innerHTML() { return ""; },
    appendChild(child) { this.kids.push(child); return child; },
    get options() { return this.kids; },
  });
  const profileSelect = makeSelect();
  const harnessSelect = makeSelect();
  const pickerForm = { profile: profileSelect, harness: harnessSelect };
  const pickerDoc = {
    createElement: () => ({ value: "", textContent: "", title: "", disabled: false }),
  };
  const pickerApi = async () => ({
    ok: true, status: 200,
    json: async () => ({
      profile_selectors: ["codex:codex", "codex:claude", "work:claude"],
      profile_options: [
        { value: "codex:codex", profile: "codex", harness: "codex",
          default: true },
        { value: "work:claude", profile: "work", harness: "claude",
          default: true },
        { value: "work:pi", profile: "work", harness: "pi", default: false },
      ],
      profile_details: [
        { name: "codex", harness: "codex" },
        { name: "codex:claude", harness: "claude", harness_allowed: false },
      ],
    }),
  });
  const picker = {};
  new Function(
    "exports", "api", "document", "form", "syncNewBorrowOptions",
    "syncForkAvailability",
    `let profileDetails = {}, newProfileOptions = [], newHarnessFor = null;
function $(id) { return id === "new-session" ? form : null; }
function spawnParent() { return null; }
` + slice("baseProfileName") + slice("normalizeSpawnProfileOptions") +
slice("spawnProfileSelector") + slice("newProfileUi") +
slice("newProfileSelector") + slice("refillSpawnHarnesses") +
slice("refillNewHarnessOptions") + slice("fillSpawnSelect") +
slice("refreshProfiles") + `
exports.refresh = refreshProfiles;
exports.refill = () => refillNewHarnessOptions(form, undefined, true);
exports.selector = () => newProfileSelector(form);
exports.details = () => profileDetails;`
  )(
    picker, pickerApi, pickerDoc, pickerForm, async () => {}, () => {}
  );
  await picker.refresh();
  check("the create Profile picker lists each base profile once",
    profileSelect.options.map((o) => o.value).join(",") === "codex,work",
    profileSelect.options.map((o) => o.value));
  check("the first profile starts on its policy-filtered default harness",
    profileSelect.value === "codex" && harnessSelect.value === "codex",
    [profileSelect.value, harnessSelect.value]);
  profileSelect.value = "work";
  picker.refill();
  check("changing Profile rebuilds its Harness choices",
    harnessSelect.options.map((o) => o.value).join(",") === "claude,pi" &&
      harnessSelect.value === "claude",
    [harnessSelect.value, harnessSelect.options.map((o) => o.value)]);
  harnessSelect.value = "pi";
  check("the split controls recombine to the canonical selector",
    picker.selector() === "work:pi", picker.selector());
}

async function checkBorrowAuthModes() {
  const borrow = {
    kids: [], value: "", disabled: false, title: "",
    set innerHTML(value) { this.kids = []; },
    get innerHTML() { return ""; },
    appendChild(child) { this.kids.push(child); return child; },
    get options() { return this.kids; },
  };
  const authForm = {
    profile: { value: "work" }, harness: { value: "claude" }, borrow,
  };
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
  // The base profile's own credential state, flipped by the last check: an
  // own-token head answer the daemon says is unusable must be offered
  // GREYED with its reason, never quietly dropped.
  let baseSelectable = true;
  const authApi = async (url) => ({
    ok: true, status: 200,
    json: async () => url.includes("codex%3Acodex")
      ? { capability: { allowed: false, mode: "none" }, options: [] }
      : { options: [
          { name: "work", label: "work", selectable: baseSelectable,
            message: baseSelectable ? "ready" : "no usable token" },
          { name: "ds4", label: "ds4", selectable: true, message: "ready" },
        ] },
  });
  const auth = {};
  new Function(
    "exports", "api", "document", "form", "parentNow", "details", "harnesses",
    `let newBorrowFor = null, newBorrowSeq = 0, newProfileOptions = [];
let profileDetails = details;
let harnessDetails = harnesses;
function $(id) { return id === "new-session" ? form : null; }
function spawnParent() { return parentNow(); }
function syncSpawnMode() {}
function syncForkAvailability() {}
` + slice("baseProfileName") + slice("spawnProfileSelector")
    + slice("newProfileUi") + slice("newProfileSelector")
    + slice("newProfileDetail") + slice("newProfileHarnessName")
    + slice("profileBorrowCapability") + slice("profileOwnAuthLabel")
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
  /* Which of the two heads the row STARTS on. The Profile row here names a
     profile of its own, so the child runs on THAT profile's token: the empty
     answer carries the parent's whole arrangement, its own borrow included,
     and that is the parent's answer to a question this form has just been
     given a separate answer to. */
  check("an overridden profile starts the row on its own token",
    borrow.value === "work", borrow.value);

  /* Back on "(inherit the parent's profile)" — the profile select's empty
     value — inheriting is what was asked for, auth included. */
  authForm.profile.value = "";
  await auth.sync(true);
  check("an inherited profile keeps the parent's arrangement",
    borrow.value === "", borrow.value);
  authForm.profile.value = "work";

  /* A lender the operator picked is theirs: the row stops following the
     Profile row above it once it has been answered. */
  await auth.sync(true);
  borrow.value = "ds4";
  borrow._borrowTouched = true;
  await auth.sync(true);
  check("an operator's own pick survives a refill",
    borrow.value === "ds4", borrow.value);
  borrow._borrowTouched = false;
  await auth.sync(true);

  baseSelectable = false;
  await auth.sync(true);
  const own2 = borrow.options.find((o) => o.value === "work");
  check("an unusable own token is greyed with the lender's reason",
    own2 !== undefined && own2.disabled && /no usable token/.test(own2.title || ""),
    own2 && [own2.disabled, own2.title]);
  baseSelectable = true;

  authForm.profile.value = "codex";
  authForm.harness.value = "codex";
  await auth.sync(true);
  check("an OAuth profile replaces the parent-auth head with its own login",
    borrow.options.length === 1 && borrow.options[0].value === "" &&
      borrow.options[0].textContent ===
        "(codex/codex profile's own OAuth login)",
    borrow.options.map((o) => [o.value, o.textContent]));
}

Promise.all([
  checkProfilePicker(), checkBorrowAuthModes(),
]).then(() => {
  console.log("borrowform_check: " + (failures ? `${failures} failing` : "ok"));
  process.exitCode = failures ? 1 : 0;
}).catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
