/* The Workspaces page's row badges (claunch-gax44), run against the real
   wsGitBadges / wsRow / refreshWsGitSummary from app.js.

   What has to hold: a workspace row shows its branch, ↑ahead ↓behind when
   either is non-zero, and "N changed" when tracked files are modified; a
   branch level with its upstream and a clean tree shows the branch alone;
   a detached HEAD says so; a directory git cannot read says it is not a git
   checkout; a missing workspace and one the summary has not answered for
   yet get no git badge at all; and an older daemon that 404s the summary
   route leaves the rows as they were.

   And the worktrees (claunch-kjjta): a row whose summary lists linked
   worktrees carries an "N worktrees" toggle; opened, each worktree is a
   line with its path, its branch (or detached HEAD) and the sessions whose
   directory it is -- matched whatever the slashes or the case -- linked to
   their pages, and "no session" when nobody stands there. A workspace
   registered at a linked worktree lists the main checkout too, marked
   "main checkout" (claunch-k9ddh).

   And the start button (claunch-dbik7): a present workspace's row has a
   "New session" button that opens the one session modal on its "new" tab
   seeded with that workspace's name; a missing workspace has none, and so
   does a page with no modal to open. */
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
  const start = src.search(new RegExp(`(async )?function ${name}\\(`));
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

function el(tag, cls, text) {
  return {
    tag, cls: cls || "", text: text === undefined ? "" : String(text),
    children: [], handlers: {}, title: "",
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(ev, fn) { (this.handlers[ev] = this.handlers[ev] || []).push(fn); },
  };
}

// The row is a head and an action bar (claunch-mhz4g.1): read it as one
// flat list of everything drawn in it.
function flat(n, out = []) {
  for (const c of n.children || []) { out.push(c); flat(c, out); }
  return out;
}

let renders = 0;
let answer = null;
const opened = [];
const ctx = {
  el,
  openSessionModal: (opts) => { opened.push(opts); },
  wsOpen: true,
  wsSyncOpen: new Set(),
  wsSessionsIn: () => [],
  wsVscodeControls: () => el("span", "vscode"),
  wsSyncToggle: () => {},
  wsRemove: () => {},
  wsRefreshAllBadge: () => null,
  wsUsageButton: () => el("button", "ws-usage", "size"),
  sessionsCache: [
    { name: "s7", cwd: "C:\\r\\wt-feature\\" },
    { name: "s8", cwd: "c:/R/wt-feature" },
    { name: "s9", cwd: "C:/elsewhere" },
  ],
  renderWorkspaces: () => { renders++; },
  api: async (url) => {
    assert.equal(url, "/api/reposync/workspaces/summary");
    return answer;
  },
};
vm.createContext(ctx);
vm.runInContext(
  "let wsGitSummary = {};\nconst wsTreesOpen = new Set();\n" +
  [slice("refreshWsGitSummary"), slice("wsGitBadges"), slice("wsRow"),
   slice("wsPathKey"), slice("wsWorktreeList"), slice("commitRef"), slice("commitText")].join("\n") +
  "\nthis.refresh = refreshWsGitSummary; this.badges = wsGitBadges; this.row = wsRow;" +
  "\nthis.trees = wsWorktreeList; this.treesOpen = wsTreesOpen;" +
  "\nthis.summary = () => wsGitSummary;",
  ctx
);

// Spread first: an array made inside the vm context has that realm's
// Array.prototype, which deepStrictEqual tells apart from this one's.
// A node's whole text: a detached head is a link inside its badge (claunch-tb145).
const deepText = (n) => n.text + (n.children || []).map(deepText).join("");
const texts = (nodes) => [...nodes].map(deepText);

// Ahead, behind and changed together.
let b = ctx.badges({ branch: "master", detached: false, upstream: "origin/master",
                     ahead: 2, behind: 1, changed: 3 });
assert.deepEqual(texts(b), ["master", "↑2 ↓1", "3 changed"]);
assert.match(b[0].title, /tracks origin\/master/);
assert.match(b[1].cls, /ws-git-behind/);
assert.match(b[1].title, /last fetch/);
assert.match(b[2].title, /untracked files are not counted/);

// Level and clean: the branch alone.
b = ctx.badges({ branch: "s1-x", detached: false, upstream: "origin/s1-x",
                 ahead: 0, behind: 0, changed: 0 });
assert.deepEqual(texts(b), ["s1-x"]);

// Ahead only is not drawn as "behind".
b = ctx.badges({ branch: "s1-x", upstream: "origin/s1-x", ahead: 4, behind: 0, changed: 0 });
assert.deepEqual(texts(b), ["s1-x", "↑4"]);
assert.doesNotMatch(b[1].cls, /ws-git-behind/);

// No upstream, detached, unreadable, not answered yet.
b = ctx.badges({ branch: "local", upstream: null, ahead: null, behind: null, changed: 0 });
assert.deepEqual(texts(b), ["local"]);
assert.equal(b[0].title, "no upstream set");
assert.deepEqual(texts(ctx.badges({ detached: true, branch: null, changed: 0 })), ["detached HEAD"]);
b = ctx.badges({ error: "git status: not a git repository" });
assert.deepEqual(texts(b), ["not a git checkout"]);
assert.match(b[0].title, /not a git repository/);
assert.equal(ctx.badges(undefined).length, 0);

(async () => {
  // The summary lands in the rows after one fetch and one render.
  answer = { ok: true, json: async () => ({ workspaces: {
    proj: { branch: "master", upstream: "origin/master", ahead: 0, behind: 2, changed: 0 },
  } }) };
  await ctx.refresh();
  assert.equal(renders, 1);
  const row = ctx.row({ name: "proj", path: "/r", exists: true });
  const all = flat(row).map((c) => c.text);
  assert.ok(all.includes("master") && all.includes("↓2"), all.join("|"));
  // A missing workspace gets no git badge even if a stale entry is there.
  const gone = ctx.row({ name: "proj", path: "/r", exists: false });
  assert.ok(!flat(gone).some((c) => c.text === "master"));

  // An older daemon: 404 leaves the summary and the rows as they were.
  answer = { ok: false, status: 404, json: async () => ({}) };
  await ctx.refresh();
  assert.equal(renders, 1);
  assert.equal(ctx.summary().proj.behind, 2);

  /* ---- worktrees ------------------------------------------------------- */
  answer = { ok: true, json: async () => ({ workspaces: {
    proj: { branch: "master", upstream: "origin/master", ahead: 0, behind: 0, changed: 0,
            worktrees: [
              { path: "C:/r/wt-feature", branch: "s7-feature", detached: false,
                head: "a".repeat(40), prunable: false },
              { path: "C:/r/wt-old", branch: null, detached: true,
                head: "b".repeat(40), prunable: true },
            ] },
  } }) };
  await ctx.refresh();
  let r = ctx.row({ name: "proj", path: "/r", exists: true });
  const toggle = flat(r).find((c) => c.tag === "button" && /worktree/.test(c.text));
  assert.ok(toggle, "a row with worktrees carries the toggle");
  assert.equal(toggle.text, "2 worktrees ▾");
  const before = renders;
  toggle.handlers.click[0]();
  assert.ok(ctx.treesOpen.has("proj"));
  assert.equal(renders, before + 1);
  r = ctx.row({ name: "proj", path: "/r", exists: true });
  assert.equal(flat(r).find((c) => c.tag === "button" && /worktree/.test(c.text)).text,
               "2 worktrees ▴");
  const list = ctx.trees({ name: "proj" });
  assert.equal(list.children.length, 2);
  const [feat, old] = list.children;
  assert.deepEqual(texts(feat.children), ["C:/r/wt-feature", "s7-feature", "s7", "s8"]);
  const links = feat.children.filter((c) => c.tag === "a");
  assert.deepEqual(links.map((a) => a.href), ["#/s/s7", "#/s/s8"]);
  assert.deepEqual(texts(old.children),
                   ["C:/r/wt-old", "detached bbbbbbbb", "directory gone", "no session"]);
  // registered at a linked worktree, the main checkout is listed and says so
  // (claunch-k9ddh)
  answer = { ok: true, json: async () => ({ workspaces: {
    proj: { branch: "s7-feature", ahead: 0, behind: 0, changed: 0, worktrees: [
      { path: "C:/r", branch: "master", detached: false, head: "c".repeat(40),
        prunable: false, main: true },
    ] } } }) };
  await ctx.refresh();
  const [mainLine] = ctx.trees({ name: "proj" }).children;
  assert.deepEqual(texts(mainLine.children), ["C:/r", "master", "main checkout", "no session"]);
  assert.ok(!texts(feat.children).includes("main checkout"));

  // no worktrees: no toggle at all
  answer = { ok: true, json: async () => ({ workspaces: {
    proj: { branch: "master", ahead: 0, behind: 0, changed: 0, worktrees: [] } } }) };
  await ctx.refresh();
  r = ctx.row({ name: "proj", path: "/r", exists: true });
  assert.ok(!flat(r).some((c) => c.tag === "button" && /worktree/.test(c.text)));

  /* ---- start a session here ------------------------------------------- */
  const startOf = (row) => flat(row).find((c) => c.tag === "button" && c.text === "New session");
  r = ctx.row({ name: "proj", path: "C:/r", exists: true });
  const start = startOf(r);
  assert.ok(start, "a present workspace's row carries the start button");
  assert.match(start.title, /C:\/r/);
  start.handlers.click[0]();
  assert.equal(opened.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(opened[0])),
                   { tab: "new", seed: { workspace: "proj" } });
  // a missing workspace: nowhere to start one
  assert.equal(startOf(ctx.row({ name: "proj", path: "C:/r", exists: false })), undefined);
  // a page without the modal: no button that would do nothing
  const keep = ctx.openSessionModal;
  delete ctx.openSessionModal;
  assert.equal(startOf(ctx.row({ name: "proj", path: "C:/r", exists: true })), undefined);
  ctx.openSessionModal = keep;

  /* ---- the row's two lines (claunch-mhz4g.1) ---------------------------- */
  r = ctx.row({ name: "proj", path: "C:/r", exists: true });
  assert.deepEqual(r.children.map((c) => c.cls), ["ws-row-head", "ws-row-actions"]);
  const groups = r.children[1].children.map((g) => g.cls.replace("ws-action-group ", ""));
  assert.deepEqual(groups, ["ws-group-start", "ws-group-open", "ws-group-panels", "ws-group-end"],
                   "start, open, panels, then unregister set apart");
  const buttons = flat(r.children[1]).filter((c) => c.tag === "button");
  assert.deepEqual(buttons.filter((b) => /approve/.test(b.cls)).map((b) => b.text), ["New session"],
                   "one primary button");
  assert.equal(r.children[1].children[3].children[0].text, "Unregister");
  assert.ok(flat(r.children[0]).some((c) => c.text === "master"), "status badges sit in the head");

  console.log("ok");
})().catch((err) => { console.error(err); process.exit(1); });
