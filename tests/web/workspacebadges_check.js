/* The Workspaces page's row badges (claunch-gax44), run against the real
   wsGitBadges / wsRow / refreshWsGitSummary from app.js.

   What has to hold: a workspace row shows its branch, ↑ahead ↓behind when
   either is non-zero, and "N changed" when tracked files are modified; a
   branch level with its upstream and a clean tree shows the branch alone;
   a detached HEAD says so; a directory git cannot read says it is not a git
   checkout; a missing workspace and one the summary has not answered for
   yet get no git badge at all; and an older daemon that 404s the summary
   route leaves the rows as they were. */
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

let renders = 0;
let answer = null;
const ctx = {
  el,
  wsOpen: true,
  wsSyncOpen: new Set(),
  wsSessionsIn: () => [],
  wsVscodeControls: () => el("span", "vscode"),
  wsSyncToggle: () => {},
  wsRemove: () => {},
  renderWorkspaces: () => { renders++; },
  api: async (url) => {
    assert.equal(url, "/api/reposync/workspaces/summary");
    return answer;
  },
};
vm.createContext(ctx);
vm.runInContext(
  "let wsGitSummary = {};\n" +
  [slice("refreshWsGitSummary"), slice("wsGitBadges"), slice("wsRow")].join("\n") +
  "\nthis.refresh = refreshWsGitSummary; this.badges = wsGitBadges; this.row = wsRow;" +
  "\nthis.summary = () => wsGitSummary;",
  ctx
);

// Spread first: an array made inside the vm context has that realm's
// Array.prototype, which deepStrictEqual tells apart from this one's.
const texts = (nodes) => [...nodes].map((n) => n.text);

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
  const all = row.children.map((c) => c.text);
  assert.ok(all.includes("master") && all.includes("↓2"), all.join("|"));
  // A missing workspace gets no git badge even if a stale entry is there.
  const gone = ctx.row({ name: "proj", path: "/r", exists: false });
  assert.ok(!gone.children.some((c) => c.text === "master"));

  // An older daemon: 404 leaves the summary and the rows as they were.
  answer = { ok: false, status: 404, json: async () => ({}) };
  await ctx.refresh();
  assert.equal(renders, 1);
  assert.equal(ctx.summary().proj.behind, 2);

  console.log("ok");
})().catch((err) => { console.error(err); process.exit(1); });
