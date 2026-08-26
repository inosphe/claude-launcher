/* New-session's Borrow row follows the selected harness contract while the
   Harness row stays read-only. This is intentionally separate from the spawn
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
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let i = body; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (!depth) return src.slice(start, i + 1); }
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
` + slice("profileBorrowCapability") + slice("syncForkAvailability") + `
exports.sync = syncForkAvailability;`
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

console.log("borrowform_check: " + (failures ? `${failures} failing` : "ok"));
process.exitCode = failures ? 1 : 0;
