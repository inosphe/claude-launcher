/* The Worktree row's Reuse picker, driven against the real functions from
   app.js.

   The list is the daemon's answer about ONE directory, and the row does not
   name it — so four things can go wrong without anything on screen saying
   so, and all four have:

   - **The source.** `newSessionCwd()` is "where this session will stand":
     the directory the operator picked, or the parent's own while the row is
     unanswered. A directory of "" reaches the daemon as `resolve_cwd(None)`,
     which is the daemon's OWN process directory — so a fallback taken for
     the wrong reason silently lists another repository's checkouts.
   - **When it is read.** The spawn policy report is what settles the
     Directory row, and it arrives after the modal is on screen. A list
     fetched before it landed was fetched from wherever the row pointed while
     the answer was in flight, and nothing re-read it afterwards.
   - **The narrowing.** A filter written against one repository's checkouts
     means nothing against another's, and a pick the filter hides must not
     stay selected behind it — a hidden <option> is still what
     `select.value` holds, and Reuse would then cut a checkout the operator
     cannot see they chose.
   - **The branch the rebase aims at.** `master` and `main` are both in the
     wild; the button has to name the one THIS repository has, and offer
     nothing where it has neither.

   The hint line is pinned with them, because it is the only part of the row
   that says which directory the names came from. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  const head = src.slice(start - 6, start) === "async " ? start - 6 : start;
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}
function sliceLet(name) {
  const start = src.indexOf(`let ${name} =`);
  if (start < 0) throw new Error(`cannot locate let ${name} in app.js`);
  return src.slice(start, src.indexOf(";", start) + 1);
}

/* ---- stub DOM ---- */
function control(value = "") {
  return { value, checked: false, disabled: false, options: [] };
}
function option(label, value) {
  return { label, value, textContent: label, disabled: false };
}
function picker(pairs = []) {
  const sel = {
    value: "", disabled: false,
    options: pairs.map(([l, v]) => option(l, v)),
    appendChild(o) { sel.options.push(o); return o; },
    get innerHTML() { return ""; },
    set innerHTML(v) { if (v === "") sel.options.length = 0; },
  };
  return sel;
}
const box_ = {};
function box(id) {
  return {
    id, textContent: "", title: "", disabled: false,
    classes: new Set(), attrs: {},
    setAttribute(k, v) { box_[id].attrs[k] = String(v); },
    getAttribute(k) { return box_[id].attrs[k]; },
    classList: {
      add: (c) => box_[id].classes.add(c),
      remove: (c) => box_[id].classes.delete(c),
      contains: (c) => box_[id].classes.has(c),
      toggle: (c, on) => (on ? box_[id].classes.add(c) : box_[id].classes.delete(c)),
    },
  };
}
for (const id of ["new-worktree", "new-worktree-name-row",
                  "new-worktree-existing-row", "new-worktree-rebase-row",
                  "worktree-hint", "worktree-rebase-trunk", "cwd-hint"]) {
  box_[id] = box(id);
}

const form = {
  // The picker's values are PATHS — that is what the create form sends.
  cwd: picker([["(daemon cwd)", ""], ["repo — F:/repo", "F:/repo"],
               ["other — F:/other", "F:/other"]]),
  parent: picker([["(none — a session of its own)", ""], ["lead — idle", "lead"]]),
  worktree_mode: Object.assign([control(""), control(""), control("")],
                              { value: "" }),
  worktree_name: control(""),
  worktree_filter: control(""),
  worktree_existing: picker(),
  worktree_rebase: control(""),
};

let sessions = [];
let registry = [];
const asked = [];
/* The daemon's answer per directory. Three repositories are enough to tell
   the sources apart: the picked one, the parent's, and the daemon's own. */
const REPOS = {
  "F:/repo": { repo: true, root: "F:/repo", branch: "master",
               branches: ["master", "feature"],
               worktrees: ["w-alpha", "w-beta"] },
  "F:/other": { repo: true, root: "F:/other", branch: "main",
                branches: ["main"], worktrees: ["o-one"] },
};
function answer(cwd) {
  asked.push(cwd);
  return REPOS[cwd] || { repo: false, root: "", branch: "", branches: [],
                         worktrees: [] };
}

const ctx = {};
new Function(
  "exports", "$", "document", "Option", "sessionsCache", "api",
  "workspacesCache", "renderWorkspaces", "renderHome", "syncSpawnMode",
  "refreshWorkflowChoices", "beadsMode", "refreshIssueChoices",
  [`let newSpawnReport = null, newSpawnReportFor = null;`,
   sliceLet("newWorktreeFor"), sliceLet("newWorktreeGit"),
   sliceLet("newWorktreeFilter"),
   slice("cwdSplit"), slice("shortenPath"), slice("cwdShort"),
   slice("spawnParent"), slice("newSessionCwd"), slice("newWorktreeMode"),
   slice("worktreeTrunkBranch"), slice("syncWorktreeTrunkButton"),
   slice("renderWorktreeOptions"), slice("syncNewWorktree"),
   slice("refreshNewWorktree"),
   // The registry poll, which rebuilds the Directory row behind the form's
   // back. Everything it closes over that is a function arrives as a
   // parameter; the module state it reads is declared here.
   sliceLet("workspacesRendered"),
   `let wsOpen = false, currentPage = "home";`,
   slice("refreshWorkspaces"),
   slice("applySessionCwdChange"),
   `exports.refresh = refreshNewWorktree;
    exports.render = renderWorktreeOptions;
    exports.sync = syncNewWorktree;
    exports.trunk = worktreeTrunkBranch;
    exports.trunkSync = syncWorktreeTrunkButton;
    exports.setSessions = (s) => { sessionsCache = s; };
    exports.filter = (q) => { newWorktreeFilter = q; };
    // The daemon's answer, applied the way refreshSpawnPolicy applies it —
    // the row is only usable once THIS parent's report says so.
    exports.policy = (name, report) => {
      newSpawnReportFor = name; newSpawnReport = report;
    };
    exports.pollRegistry = refreshWorkspaces;
    exports.setRegistry = (list) => { registry = list; };`].join("\n")
)(ctx,
   (id) => (id === "new-session" ? form : box_[id] || null),
   // refreshWorkspaces reaches the row the way the page does, by selector.
   { querySelector: (sel) => (sel.includes("name=cwd") ? form.cwd : null),
     createElement: () => option("", "") },
   function Option(label, value) { return option(label, value); },
   sessions,
   async (url) => {
     if (String(url) === "/api/workspaces") return { ok: true, json: async () => ({ workspaces: registry }) };
     const cwd = decodeURIComponent(String(url).replace(/^.*cwd=/, ""));
     return { ok: true, json: async () => answer(cwd) };
   },
   registry,
   // renderWorkspaces and renderHome belong to other pages this check does
   // not open; syncSpawnMode is the form's own gating.
   () => {}, () => {}, () => {},
   // applySessionCwdChange is sliced for real, so its own dependencies have
   // to exist — what it does to the worktree list is the point of the case.
   () => {}, () => "", () => {});

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}
const names = () => form.worktree_existing.options
  .map((o) => o.value).filter((v) => v !== "");
/* The poll reaches the pickers through applySessionCwdChange, which calls
   refreshNewWorktree without awaiting it — one macrotask turn lets the fetch
   it started settle, the way it would on a live page before the next frame. */
const settle = () => new Promise((r) => setTimeout(r, 0));

async function main() {
  ctx.setSessions([{ name: "lead", status: "idle", cwd: "F:/repo" }]);

  /* -- the source is the directory the row points at -------------------- */
  form.parent.value = "";
  form.cwd.value = "F:/other";
  await ctx.refresh();
  check("the list is read from the directory the row points at",
        asked[asked.length - 1], "F:/other");
  check("...and it is that directory's checkouts that are offered",
        names(), ["o-one"]);

  /* -- an unanswered Directory row means the parent's own directory ------ */
  form.parent.value = "lead";
  form.cwd.value = "";                    // "(inherit the parent's directory)"
  await ctx.refresh();
  check("an unanswered row is the parent's directory, not the daemon's",
        asked[asked.length - 1], "F:/repo");
  check("...and the parent's checkouts are what it offers",
        names(), ["w-alpha", "w-beta"]);

  /* -- the list is re-read when the source moves ------------------------- */
  form.cwd.value = "F:/other";
  await ctx.refresh();
  check("moving the directory re-reads the list",
        asked[asked.length - 1], "F:/other");

  /* -- the hint names the directory the names came from ------------------ */
  ctx.policy("lead", { may_choose: ["worktree"] });
  form.worktree_mode.value = "existing";
  ctx.sync();
  const hint = box_["worktree-hint"];
  check("the hint names the directory the list was read from",
        [hint.textContent, hint.classes.has("hidden"), hint.classes.has("info")],
        ["checkouts of F:/other", false, true]);

  /* -- a filter written against one repository does not outlive it ------- */
  ctx.filter("o-one");
  form.worktree_filter.value = "o-one";
  form.worktree_existing.value = "o-one";
  form.cwd.value = "F:/repo";
  await ctx.refresh();
  check("a narrowing is dropped when the directory moves, box and all",
        [form.worktree_filter.value, names()], ["", ["w-alpha", "w-beta"]]);

  /* -- the narrowing narrows, and does not leave a hidden pick ---------- */
  form.worktree_mode.value = "existing";
  form.worktree_existing.value = "w-alpha";
  ctx.filter("beta");
  form.worktree_filter.value = "beta";
  ctx.render();
  check("the box narrows the same list", names(), ["w-beta"]);
  check("a pick the narrowing hides does not stay selected",
        form.worktree_existing.value, "");

  ctx.filter("");
  ctx.render();
  check("an empty narrowing is the whole list",
        names(), ["w-alpha", "w-beta"]);
  form.worktree_existing.value = "w-beta";
  ctx.filter("w-b");
  ctx.render();
  check("a pick that survives the narrowing is kept",
        [names(), form.worktree_existing.value], [["w-beta"], "w-beta"]);
  ctx.filter("");
  ctx.render();

  /* -- the trunk button names THIS repository's trunk -------------------- */
  form.worktree_rebase.disabled = false;
  form.worktree_rebase.value = "";
  ctx.sync();
  check("the button names the trunk this repository has, not one assumed",
        box_["worktree-rebase-trunk"].textContent, "master");

  form.cwd.value = "F:/other";
  await ctx.refresh();
  ctx.sync();
  check("...and it follows the branch list when the directory moves",
        box_["worktree-rebase-trunk"].textContent, "main");

  /* A repository with neither is offered no button at all, rather than a
     button naming a branch that is not there. */
  const saved = REPOS["F:/other"];
  REPOS["F:/other"] = { ...saved, branch: "trunk", branches: ["trunk"] };
  // Away and back, so the memo does not answer for a directory whose branch
  // list has since been re-read.
  form.cwd.value = "F:/repo";
  await ctx.refresh();
  form.cwd.value = "F:/other";
  await ctx.refresh();
  ctx.sync();
  const btn = box_["worktree-rebase-trunk"];
  check("a repository with no master or main gets no trunk to aim at",
        [btn.textContent, btn.disabled], ["trunk", true]);
  REPOS["F:/other"] = saved;

  /* -- and one press of it fills or clears the box ----------------------- */
  form.cwd.value = "F:/repo";
  await ctx.refresh();
  form.worktree_rebase.value = "";
  ctx.sync();
  check("the box is not pressed while it holds something else",
        btn.getAttribute("aria-pressed"), "false");
  form.worktree_rebase.value = "master";
  ctx.trunkSync();
  check("a box already holding the trunk reads as pressed",
        btn.getAttribute("aria-pressed"), "true");

  /* -- the registry poll can move the row without a change event -------- */
  // A workspace whose directory went missing is dropped from the registry,
  // and the poll puts the Directory row back on "(daemon cwd)" from inside
  // itself. A programmatic `.value` fires no `change` event, so unless the
  // poll re-reads them the pickers below go on showing the repository the
  // row has already stopped naming — and a name taken from that list would
  // cut a checkout of the NEW repository.
  form.parent.value = "";
  ctx.policy("", null);
  registry = [{ name: "repo", path: "F:/repo", exists: true },
              { name: "other", path: "F:/other", exists: true }];
  await ctx.pollRegistry();
  form.cwd.value = "F:/repo";
  await ctx.refresh();
  check("with the workspace registered, the row and the list agree",
        [form.cwd.value, names()], ["F:/repo", ["w-alpha", "w-beta"]]);

  registry = [{ name: "other", path: "F:/other", exists: true }];
  await ctx.pollRegistry();
  await settle();
  check("a workspace that went missing drops the row to the daemon's cwd",
        form.cwd.value, "");
  check("...and the worktree list is re-read from where the row now points",
        asked[asked.length - 1], "");
  check("...so the old repository's checkouts are off the picker",
        names(), []);

  /* -- a locked row says so instead of captioning ------------------------ */
  // The same parent, with spawn.allow_worktree shut: the row is not merely
  // unusable, it is unusable for a reason the operator can act on.
  form.parent.value = "lead";
  form.cwd.value = "F:/repo";
  await ctx.refresh();
  ctx.policy("lead", { may_choose: [] });
  ctx.sync();
  const locked = box_["worktree-hint"];
  check("a locked row keeps its warning rather than a caption",
        [locked.textContent, locked.classes.has("info")],
        ["worktree selection is locked by spawn.allow_worktree", false]);

  if (failures) process.exit(1);
  console.log("worktreepicker_check: ok");
}
main().catch((e) => { console.error(e); process.exit(1); });
