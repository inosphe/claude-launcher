/* The session modal does not wait for /api/git.

   Opening the form fills four option sets, and they used to be awaited
   together (`Promise.all`). Three of them answer in single-digit
   milliseconds; the fourth, the worktree list, runs four git processes over
   the parent's checkout. Measured on this machine (s586, 2026-09-22, a
   repository carrying 814 branches and 484 worktrees):

     /api/git               496-547 ms    (existing 214, branches 126,
                                           current_branch 32, repo_root 32)
     /api/profiles           49- 93 ms
     /api/cflow/workflows    2.5- 8.6 ms
     /api/workspaces         1.5- 5.0 ms

   A `Promise.all` is as slow as its slowest member, so the form was drawn
   half a second after it could have been and every field -- the Profile
   select among them -- looked slow. The worktree fetch is fired without
   being waited for, and it paints its own rows when it lands.

   Two rules hold that arrangement, and this is what breaks if either goes:

   - The fetch is not inside the awaited group. Putting it back restores the
     half second.
   - The answer is discarded when the directory it was asked about is no
     longer the one the form is on. Half a second is long enough for the
     modal to be closed and reopened on another parent, and painting the
     first answer anyway puts another repository's checkouts on screen --
     the failure the "checkouts of ..." hint exists to name.

   See claunch-web-session-modal-waits-on-git-w75gv. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");

let failures = 0;
function check(label, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g === w) return;
  failures++;
  console.error(`FAIL ${label}\n  got  ${g}\n  want ${w}`);
}

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  const head = src.slice(start - 6, start) === "async " ? start - 6 : start;
  let depth = 0;
  for (let j = src.indexOf(") {", start) + 2; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

const open = slice("openSessionModal");
const refresh = slice("refreshNewWorktree");

/* ---- the fetch is outside the awaited group ---------------------------- */

// The group is the one `await Promise.all([...])` in the opener.
const at = open.indexOf("await Promise.all(");
check("the opener still awaits a group of fills", at >= 0, true);
const groupEnd = open.indexOf("]);", at);
const group = open.slice(at, groupEnd);

check("the worktree fill is not awaited with the others",
      group.includes("refreshNewWorktree"), false);
check("the three that are cheap are still awaited together",
      ["refreshWorkspaces", "refreshWorkflowChoices", "refreshRoles"]
        .every((fn) => group.includes(fn)), true);

// It is still started -- not waited for is not the same as not called.
check("the worktree fill is still started on open",
      open.includes("refreshNewWorktree"), true);
check("it is started before the group it no longer belongs to",
      open.indexOf("refreshNewWorktree") < at, true);
check("a failure in it degrades its own field only",
      /refreshNewWorktree\(\)\)\.catch\(/.test(open), true);

/* ---- a late answer is not painted onto another directory --------------- */

// What it asked about, and what the form is on now, are compared after the
// await and before anything is drawn.
const awaitAt = refresh.indexOf("await api(");
const guardAt = refresh.indexOf("if (cwd !== newWorktreeFor) return;");
const paintAt = refresh.indexOf("renderWorktreeOptions()");
check("the stale answer is dropped", guardAt >= 0, true);
check("the check happens after the fetch", guardAt > awaitAt, true);
check("and before anything is drawn", guardAt < paintAt, true);

// The memo the rows are read from is only written past that check, so a
// dropped answer leaves the previous directory's list standing rather than
// half-replacing it.
const assignAt = refresh.indexOf("newWorktreeGit = git;");
check("the payload is stored only past the check",
      assignAt > guardAt && assignAt < paintAt, true);

// The failure path assigns to the local, not to the memo: a fetch that threw
// must not blank the rows of a directory it was not asked about.
const body = refresh.slice(awaitAt);
check("a failed fetch does not write the memo directly",
      /catch \{ newWorktreeGit =/.test(body), false);

/* ---- what the rows do when the answer finally lands -------------------- */

// Late is only the same picture if the arrival keeps a selection the
// operator (or a quick job's seed) already made.
const render = slice("renderWorktreeOptions");
check("the arrival keeps the selection it finds",
      /const kept = sel\.value/.test(render)
        && /sel\.value = .*kept/.test(render), true);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("modalgitwait_check: ok");
