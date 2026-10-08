/* The Workspaces page's Sync panel (claunch-tqm2e), run against the real
   functions from app.js.

   What has to hold: the panel draws the plan and offers the button that
   matches its action (none when up to date); pressing it sends exactly the
   two tips the person was shown; a 409 "stale" answer replaces the plan on
   screen and runs nothing further; a peer that could not be read is a note
   under the peers, and the plan is drawn anyway. */
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

function constant(name) {
  const m = src.match(new RegExp(`const ${name} = [^;]*;`));
  if (!m) throw new Error(`cannot locate const ${name}`);
  return m[0];
}

function el(tag, cls, text) {
  return {
    tag, cls: cls || "", text: text === undefined ? "" : String(text),
    children: [], handlers: {}, disabled: false, value: "", attrs: {},
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(ev, fn) { (this.handlers[ev] = this.handlers[ev] || []).push(fn); },
    setAttribute(k, v) { this.attrs[k] = v; },
  };
}

function walk(node, out = []) {
  out.push(node);
  for (const c of node.children || []) walk(c, out);
  return out;
}

const PLAN = {
  repo: "/r", workspace: "proj", remote: "origin", remote_key: "git.example/o/p",
  branch: "master", action: "rebase", ahead: 2, behind: 1,
  local: "a".repeat(40), upstream: "b".repeat(40), merges: [], affected: ["s1-x"],
  summary: "rebase 2 unpushed commit(s) (0 merge(s)) onto origin/master (+1) and push; 1 local branch(es) left on the old commits",
};
const DOC = {
  workspace: "proj", repo: "/r", machine: "pc-a", fetched: false,
  remotes: { "git.example/o/p": "origin" },
  branches: ["master", "s1-x"],
  peers: [{
    machine: "pc-b", workspace: "proj", key: "git.example/o/p",
    local_remote: "origin", peer_remote: "up",
    branches: { master: { sha: "b".repeat(40), tracking: [
      { key: "git.example/o/p", ahead: 3, behind: 0 }] } },
  }],
  problems: ["pc-old: cannot read its repositories (404)"],
  plan: PLAN,
};

const calls = [];
let reply = null;
let confirmAnswer = true;
const ctx = {
  el, renderWorkspaces() {}, wsOpen: true,
  Option: function (text, value) { return { tag: "option", text, value, children: [] }; },
  document: { createTextNode: (t) => ({ tag: "#text", text: t, children: [] }) },
  confirm: (msg) => { calls.push({ confirm: msg }); return confirmAnswer; },
  encodeURIComponent,
  JSON, Promise, String, Set, Map, Object,
  api: async (url, opts) => {
    calls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
    const r = reply(url);
    return { ok: r.status < 400, status: r.status, json: async () => r.body };
  },
};
vm.createContext(ctx);
vm.runInContext(
  [constant("wsSyncOpen"), constant("wsSyncState"), constant("WS_SYNC_LABEL"),
   ...["wsSyncSt", "wsSyncRefresh", "wsSyncRun", "wsSyncBranch", "wsSyncPlanLine",
       "wsSyncPeers", "wsSyncResult", "wsSyncPanel"].map(slice),
   "globalThis.__st = wsSyncSt;"].join("\n"),
  ctx);

const W = { name: "proj", path: "/r", exists: true };

(async () => {
  /* ---- status: the plan and peers are drawn; problems are notes -------- */
  reply = () => ({ status: 200, body: JSON.parse(JSON.stringify(DOC)) });
  await ctx.wsSyncRefresh("proj", false);
  assert.equal(calls[0].url, "/api/reposync/workspaces/proj/status?fetch=0");
  const st = ctx.__st("proj");
  assert.equal(st.pushBranch, "s1-x", "the push picker defaults to a work branch");
  let nodes = walk(ctx.wsSyncPanel(W));
  const texts = nodes.map((n) => n.text);
  assert.ok(texts.includes("Rebase & push"), "the button names the plan's action");
  assert.ok(texts.includes("rebase"), "the action badge");
  assert.ok(texts.some((t) => t.startsWith("left on the old commits: s1-x")));
  assert.ok(texts.includes("pc-old: cannot read its repositories (404)"));
  assert.ok(texts.some((t) => t.includes("master bbbbbbbb = remote · 3 unpushed")),
            "a peer's master against the remote, with its unpushed count");

  /* ---- up to date: no button to press ---------------------------------- */
  st.doc.plan = { ...PLAN, action: "up-to-date", ahead: 0, behind: 0, affected: [] };
  nodes = walk(ctx.wsSyncPanel(W));
  const upToDate = nodes.find((n) => n.text === "Up to date");
  assert.ok(upToDate && upToDate.disabled, "an up-to-date plan offers nothing to press");
  st.doc.plan = PLAN;

  /* ---- declining the confirm sends nothing ------------------------------ */
  calls.length = 0;
  confirmAnswer = false;
  await ctx.wsSyncRun("proj");
  assert.equal(calls.filter((c) => c.url).length, 0);
  confirmAnswer = true;

  /* ---- stale: the fresh plan replaces the old, nothing else runs -------- */
  calls.length = 0;
  const fresh = { ...PLAN, upstream: "c".repeat(40), behind: 2 };
  reply = () => ({ status: 409, body: { error: "moved", stale: true, plan: fresh } });
  await ctx.wsSyncRun("proj");
  const posted = calls.filter((c) => c.url);
  assert.equal(posted.length, 1);
  assert.equal(posted[0].url, "/api/reposync/workspaces/proj/sync");
  assert.deepEqual(posted[0].body, {
    remote: "origin", branch: "master", local: PLAN.local, upstream: PLAN.upstream });
  assert.equal(st.doc.plan.upstream, "c".repeat(40), "the screen now shows the fresh plan");
  assert.match(st.error, /moved since this plan was shown/);
  assert.equal(st.result, null);

  /* ---- success: the result is kept and the status is read again --------- */
  calls.length = 0;
  reply = (url) => url.endsWith("/sync")
    ? { status: 200, body: { ok: true, plan: fresh, new: "d".repeat(40), pushed: true,
                             restack: [{ branch: "s1-x", worktree: "", command: "git rebase --onto d a s1-x" }],
                             board: [] } }
    : { status: 200, body: { ...DOC, plan: { ...fresh, action: "up-to-date" } } };
  await ctx.wsSyncRun("proj");
  assert.deepEqual(calls.filter((c) => c.url).map((c) => c.url), [
    "/api/reposync/workspaces/proj/sync", "/api/reposync/workspaces/proj/status?fetch=0"]);
  assert.equal(calls.find((c) => c.url && c.url.endsWith("/sync")).body.upstream, "c".repeat(40));
  nodes = walk(ctx.wsSyncResult(st.result)).map((n) => n.text);
  assert.ok(nodes.includes("done: master = dddddddd (pushed)"));
  assert.ok(nodes.includes("restack s1-x: git rebase --onto d a s1-x"));

  /* ---- branch pull needs both fields ------------------------------------ */
  calls.length = 0;
  st.pullMachine = "pc-b";
  st.pullBranch = "";
  await ctx.wsSyncBranch("proj", "pull");
  assert.equal(calls.length, 0, "no machine+branch, no request");
  st.pullBranch = "s9-y";
  reply = (url) => url.endsWith("/branch-pull")
    ? { status: 200, body: { ok: true, remote: "origin", branch: "pc-b/s9-y", sha: "e".repeat(40) } }
    : { status: 200, body: DOC };
  await ctx.wsSyncBranch("proj", "pull");
  assert.deepEqual(calls[0].body, { machine: "pc-b", branch: "s9-y" });

  console.log("workspacesync_check ok");
})().catch((err) => { console.error(err); process.exit(1); });
