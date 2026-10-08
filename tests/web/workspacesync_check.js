/* The Workspaces page's Sync panel (claunch-tqm2e), run against the real
   functions from app.js.

   What has to hold: the panel draws the plan and offers the button that
   matches its action (none when up to date); pressing it sends exactly the
   two tips the panel showed; a 409 "stale" answer replaces the plan on
   screen and runs nothing further; a peer that could not be read is a note
   under the peers, and the plan is drawn anyway.

   And the approval (s469's decline of the first version): the panel's button
   never approves a push. A push answers 202 with an approval request; the
   panel shows that it waits, reads the card until a person answers it, and
   sends the request again with the id only once the card is approved. A
   rejected or expired card ends with nothing sent. A fast-forward pushes
   nothing, so it asks here instead and needs no card. */
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
const timers = [];
let reply = null;
let confirmAnswer = true;
let gateRefreshed = 0;
let badgesRefreshed = 0;   // the row badges re-read after a sync that ran (claunch-gax44)
const ctx = {
  el, renderWorkspaces() {}, wsOpen: true,
  Option: function (text, value) { return { tag: "option", text, value, children: [] }; },
  document: { createTextNode: (t) => ({ tag: "#text", text: t, children: [] }) },
  confirm: (msg) => { calls.push({ confirm: msg }); return confirmAnswer; },
  setTimeout: (fn) => { timers.push(fn); },
  refreshReposyncGate: () => { gateRefreshed++; },
  refreshWsGitSummary: () => { badgesRefreshed++; },
  encodeURIComponent,
  JSON, Promise, String, Set, Map, Object,
  api: async (url, opts) => {
    calls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
    const r = reply(url, opts);
    return { ok: r.status < 400, status: r.status, json: async () => r.body };
  },
};
vm.createContext(ctx);
vm.runInContext(
  [constant("wsSyncOpen"), constant("wsSyncState"), constant("WS_SYNC_LABEL"),
   constant("WS_SYNC_PATH"), constant("WS_SYNC_POLL_MS"),
   ...["wsSyncSt", "wsSyncRefresh", "wsSyncSend", "wsSyncWait", "wsSyncRun",
       "wsSyncBranch", "wsSyncPlanLine", "wsSyncPeers", "wsSyncResult",
       "wsSyncPanel"].map(slice),
   "globalThis.__st = wsSyncSt;"].join("\n"),
  ctx);

const W = { name: "proj", path: "/r", exists: true };
const posts = () => calls.filter((c) => c.url && c.body);
async function tick() {
  const fn = timers.shift();
  assert.ok(fn, "a poll was scheduled");
  await fn();
}

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

  /* ---- fast-forward: asks here, declining sends nothing ----------------- */
  st.doc.plan = { ...PLAN, action: "ff", ahead: 0, affected: [] };
  calls.length = 0;
  confirmAnswer = false;
  await ctx.wsSyncRun("proj");
  assert.equal(calls.filter((c) => c.confirm).length, 1);
  assert.equal(posts().length, 0);
  confirmAnswer = true;
  st.doc.plan = PLAN;

  /* ---- a push: no confirm; the answer is a card to wait on -------------- */
  calls.length = 0;
  let card = "pending";
  reply = (url) => {
    if (url === "/api/reposync/requests/r1") return { status: 200, body: { request: { id: "r1", status: card } } };
    if (url.endsWith("/sync")) {
      return { status: 202, body: { status: "waiting_approval", request: "r1",
                                    deadline: "2026-10-08T06:30:00+00:00", plan: PLAN } };
    }
    return { status: 200, body: DOC };
  };
  await ctx.wsSyncRun("proj");
  assert.equal(calls.filter((c) => c.confirm).length, 0, "a push does not ask in the panel");
  assert.deepEqual(posts()[0].body, {
    remote: "origin", branch: "master", local: PLAN.local, upstream: PLAN.upstream });
  assert.equal(st.waiting.request, "r1");
  assert.equal(gateRefreshed, 1, "the card is fetched at once");
  nodes = walk(ctx.wsSyncPanel(W)).map((n) => n.text);
  assert.ok(nodes.includes("Waiting for approval…"), "the button says it waits");
  assert.ok(nodes.some((t) => t.startsWith("Waiting for approval: answer the")));
  // the card is still pending: read again later, send nothing
  await tick();
  assert.equal(posts().length, 1);
  assert.equal(timers.length, 1, "still waiting");

  /* ---- approved: the same body again, with the id ----------------------- */
  card = "approved";
  reply = (url) => {
    if (url === "/api/reposync/requests/r1") return { status: 200, body: { request: { id: "r1", status: "approved" } } };
    if (url.endsWith("/sync")) {
      return { status: 200, body: { ok: true, plan: PLAN, new: "d".repeat(40), pushed: true,
                                    restack: [{ branch: "s1-x", worktree: "", command: "git rebase --onto d a s1-x" }],
                                    board: [] } };
    }
    return { status: 200, body: { ...DOC, plan: { ...PLAN, action: "up-to-date" } } };
  };
  await tick();
  const sent = posts();
  assert.equal(sent.length, 2);
  assert.deepEqual(sent[1].body, {
    remote: "origin", branch: "master", local: PLAN.local, upstream: PLAN.upstream, request: "r1" });
  assert.equal(st.waiting, null);
  nodes = walk(ctx.wsSyncResult(st.result)).map((n) => n.text);
  assert.ok(nodes.includes("done: master = dddddddd (pushed)"));
  assert.ok(nodes.includes("restack s1-x: git rebase --onto d a s1-x"));
  assert.equal(calls[calls.length - 1].url, "/api/reposync/workspaces/proj/status?fetch=0",
               "the status is read again after a sync");
  assert.equal(badgesRefreshed, 1, "the row badges are read again after a sync that ran");

  /* ---- rejected: nothing sent again, and the panel says so -------------- */
  st.doc.plan = PLAN;
  calls.length = 0;
  reply = (url) => url === "/api/reposync/requests/r2"
    ? { status: 200, body: { request: { id: "r2", status: "rejected" } } }
    : { status: 202, body: { status: "waiting_approval", request: "r2", deadline: "" } };
  await ctx.wsSyncRun("proj");
  await tick();
  assert.equal(posts().length, 1, "a rejected card is never followed by a second send");
  assert.equal(st.waiting, null);
  assert.equal(st.busy, "");
  assert.match(st.error, /was rejected; nothing was pushed/);

  /* ---- stale: the fresh plan replaces the old, nothing else runs -------- */
  calls.length = 0;
  const fresh = { ...PLAN, upstream: "c".repeat(40), behind: 2 };
  reply = () => ({ status: 409, body: { error: "moved", stale: true, plan: fresh } });
  await ctx.wsSyncRun("proj");
  assert.equal(posts().length, 1);
  assert.equal(st.doc.plan.upstream, "c".repeat(40), "the screen now shows the fresh plan");
  assert.match(st.error, /moved since this plan was shown/);
  assert.equal(st.result, null);
  assert.equal(timers.length, 0);
  assert.equal(badgesRefreshed, 1, "a refused sync moved nothing, so the badges stay");

  /* ---- branch push waits on its card too -------------------------------- */
  calls.length = 0;
  st.pushBranch = "s1-x";
  st.force = true;
  reply = (url) => url.endsWith("/branch-push")
    ? { status: 202, body: { status: "waiting_approval", request: "r3", deadline: "" } }
    : { status: 200, body: { request: { id: "r3", status: "approved" } } };
  await ctx.wsSyncBranch("proj", "push");
  assert.deepEqual(posts()[0].body, { branch: "s1-x", force_with_lease: true });
  reply = (url) => url.endsWith("/branch-push")
    ? { status: 200, body: { ok: true, remote: "origin", ref: "pc-a/s1-x", sha: "e".repeat(40) } }
    : { status: 200, body: { request: { id: "r3", status: "approved" } } };
  await tick();
  assert.deepEqual(posts()[1].body, { branch: "s1-x", force_with_lease: true, request: "r3" });
  assert.equal(st.result.kind, "push");

  /* ---- branch pull needs both fields, and no card ----------------------- */
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
  assert.deepEqual(posts()[0].body, { machine: "pc-b", branch: "s9-y" });

  console.log("workspacesync_check ok");
})().catch((err) => { console.error(err); process.exit(1); });
