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
   nothing, so it asks here instead and needs no card.

   And the shared branches (claunch-tamel): the branches other machines
   published as <machine>/<branch> are listed with a Pull each, which sends
   the branch pull with the planned remote and needs no card; one already
   level or with local commits of its own offers nothing to press, and a
   prefix that is not a relay peer is kept apart under a fold.

   And the branches waiting to land (claunch-6p7ri): each local branch the
   remote's master does not hold is a row with its ahead count, its time and
   the session stamped on its tip, linked to that session's page; an
   unstamped one says so, and an empty list says every branch is on the
   remote's master.

   And the peer table (claunch-gjjdp): one row per peer with its master tip
   and how that tip stands against ours, worded per state; a peer that did
   not answer is a row marked unreadable. */
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
   constant("WS_SYNC_PATH"), constant("WS_SYNC_POLL_MS"), constant("WS_SHARED_STATE"),
   constant("WS_HISTORY_KIND"), constant("WS_SYNC_MODE_NOTE"), constant("WS_UNMERGED_SHOWN"),
   ...["wsSyncSt", "wsSyncRefresh", "wsSyncSend", "wsSyncWait", "wsSyncRun",
       "wsSyncBranch", "wsSyncPlanLine", "wsSyncPeers", "wsSyncResult",
       "wsSyncRemoteComparisons", "wsSyncPanel", "wsSyncPullBody", "wsSyncShared", "wsSyncUnmerged",
       "wsPeerDistanceText", "wsSyncHistory", "wsHistoryApproval", "wsAgo", "wsSyncUnmergedRow",
       "wsSyncWhyDisabled", "wsSyncButtonRow"].map(slice),
   ...["wsSyncAgentBody", "wsSyncAgentBlocked", "wsSyncAgentControls", "wsSyncParentLabel", "wsSyncPromptText",
       "wsSyncPromptBody", "wsSyncPreviewPrompt", "wsSyncOperationPreview", "wsLocalSyncControls"].map(slice),
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
  assert.ok(nodes.some((n) => n.text === "Nothing to sync: master already equals origin/master."),
            "and says why next to the button");
  // delegated, the label no longer carries the reason: the note still does
  st.agentMode = "agent";
  nodes = walk(ctx.wsSyncPanel(W));
  assert.ok(nodes.find((n) => n.text === "Delegate sync").disabled);
  assert.ok(nodes.some((n) => n.text === "Nothing to sync: master already equals origin/master."));
  assert.ok(nodes.some((n) => n.text === "Choose a parent session above to delegate."),
            "the push says what it waits for");
  st.agentMode = "commands";
  assert.equal(ctx.wsSyncWhyDisabled({ ...st, doc: { ...st.doc, plan: PLAN } }, "sync"), "",
               "a plan to carry out: nothing to explain");

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
  assert.deepEqual(posts()[0].body, { branch: "s1-x", force_with_lease: true, remote: "origin" });
  reply = (url) => url.endsWith("/branch-push")
    ? { status: 200, body: { ok: true, remote: "origin", ref: "pc-a/s1-x", sha: "e".repeat(40) } }
    : { status: 200, body: { request: { id: "r3", status: "approved" } } };
  await tick();
  assert.deepEqual(posts()[1].body, { branch: "s1-x", force_with_lease: true, remote: "origin", request: "r3" });
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
  assert.deepEqual(posts()[0].body, { machine: "pc-b", branch: "s9-y", remote: "origin" },
                   "the pull names the remote the panel planned on");

  /* ---- shared branches: listed with a Pull each ------------------------- */
  const row = (ref, state, peer) => ({
    machine: ref.split("/")[0], branch: ref.split("/").slice(1).join("/"), ref,
    sha: "f".repeat(40), date: "2026-10-08T10:00:00+09:00", subject: "work",
    peer, local: state === "new" ? null : "a".repeat(40), state });
  st.doc = { ...DOC, plan: PLAN, shared: [
    row("pc-b/s2-y", "new", true), row("pc-b/old", "behind", true),
    row("pc-b/same", "same", true), row("pc-b/div", "diverged", true),
    row("feature/z", "new", false)] };
  st.busy = "";
  nodes = walk(ctx.wsSyncPanel(W));
  const shared = nodes.find((n) => n.cls === "ws-sync-shared");
  assert.ok(shared, "the list sits in the panel");
  assert.ok(walk(shared).some((n) => n.text === "Published by other machines on origin (4) · as last fetched"));
  const rowOf = (ref) => walk(shared).find((n) => n.cls === "ws-sync-shared-row" && n.children[0].text === ref);
  const pullOf = (ref) => rowOf(ref).children.find((n) => n.tag === "button");
  assert.equal(pullOf("pc-b/s2-y").disabled, false);
  assert.equal(pullOf("pc-b/old").disabled, false);
  assert.equal(pullOf("pc-b/same").disabled, true, "level: nothing to pull");
  assert.equal(pullOf("pc-b/div").disabled, true, "diverged: a pull would refuse");
  assert.match(pullOf("pc-b/div").title, /would refuse/);
  const fold = walk(shared).find((n) => n.cls === "ws-sync-shared-other");
  assert.ok(fold && walk(fold).some((n) => n.children && n.children[0] && n.children[0].text === "feature/z"),
            "a prefix that is not a relay peer is under the fold");
  calls.length = 0;
  const cardsBefore = gateRefreshed;
  await pullOf("pc-b/old").handlers.click[0]();
  assert.equal(posts().length, 1);
  assert.equal(posts()[0].url, "/api/reposync/workspaces/proj/branch-pull");
  assert.deepEqual(posts()[0].body, { machine: "pc-b", branch: "old", remote: "origin" });
  assert.equal(gateRefreshed, cardsBefore, "a pull files no card");

  /* ---- no peer has published: a note, no rows --------------------------- */
  st.doc = { ...DOC, plan: PLAN, fetched: true, shared: [] };
  nodes = walk(ctx.wsSyncPanel(W));
  assert.ok(nodes.some((n) => n.text === "Published by other machines on origin (0)"));
  assert.ok(!nodes.some((n) => n.cls === "ws-sync-shared-row"));

  /* ---- branches waiting to land ----------------------------------------- */
  st.doc = { ...DOC, plan: PLAN, unmerged: [
    { branch: "s1-open", sha: "1".repeat(40), date: "2026-10-08T17:01:02+09:00",
      subject: "open work", ahead: 3, session: "s43" },
    { branch: "plain", sha: "2".repeat(40), date: "2026-10-07T09:00:00+09:00",
      subject: "add plain.txt", ahead: 1, session: null }] };
  nodes = walk(ctx.wsSyncPanel(W));
  const land = nodes.find((n) => (n.cls || "").includes("ws-sync-unmerged"));
  assert.ok(land, "the section sits in the panel");
  const landTexts = walk(land).map((n) => n.text);
  assert.ok(landTexts.includes("Waiting to land (2)"));
  const landRows = walk(land).filter((n) => n.cls === "ws-sync-unmerged-row");
  assert.equal(landRows.length, 2);
  assert.deepEqual(landRows[0].children.map((n) => n.text),
                   ["s1-open", "+3", ctx.wsAgo("2026-10-08T17:01:02+09:00"), "s43"]);
  assert.equal(landRows[0].children[2].title, "2026-10-08 17:01", "the full time on hover");
  assert.ok(landTexts.some((t) => /1 made by a session, 1 unstamped/.test(t)), "one line of counts");
  const link = landRows[0].children[3];
  assert.equal(link.tag, "a");
  assert.equal(link.href, "#/s/s43");
  assert.match(landRows[0].children[1].title, /not on origin\/master/);
  assert.equal(landRows[1].children[3].text, "unstamped");
  // many: the newest WS_UNMERGED_SHOWN in view, the rest under a fold
  const many = Array.from({ length: 8 }, (_, i) => ({
    branch: `b${i}`, sha: String(i).repeat(40), date: "2026-10-08T10:00:00+00:00",
    subject: "", ahead: 1, session: i % 2 ? `s${i}` : null }));
  st.doc = { ...DOC, plan: PLAN, unmerged: many };
  const big = walk(ctx.wsSyncPanel(W)).find((n) => (n.cls || "").includes("ws-sync-unmerged"));
  const landFold = walk(big).find((n) => n.cls === "ws-sync-unmerged-more");
  assert.ok(landFold, "the rest is folded");
  assert.equal(landFold.children[0].text, "Show 3 more");
  assert.equal(walk(big).filter((n) => n.cls === "ws-sync-unmerged-row").length, 8, "every row is still there");
  assert.equal(walk(landFold).filter((n) => n.cls === "ws-sync-unmerged-row").length, 3);
  // ages
  const now = Date.parse("2026-10-08T12:00:00Z");
  assert.equal(ctx.wsAgo("2026-10-08T11:59:30Z", now), "now");
  assert.equal(ctx.wsAgo("2026-10-08T11:48:00Z", now), "12m");
  assert.equal(ctx.wsAgo("2026-10-08T09:00:00Z", now), "3h");
  assert.equal(ctx.wsAgo("2026-10-04T12:00:00Z", now), "4d");
  assert.equal(ctx.wsAgo("", now), "");
  st.doc = { ...DOC, plan: PLAN, unmerged: [] };
  nodes = walk(ctx.wsSyncPanel(W)).map((n) => n.text);
  assert.ok(nodes.includes("Waiting to land (0)"));
  assert.ok(nodes.includes("Every local branch is already on origin/master."));

  /* ---- the peer table ---------------------------------------------------- */
  st.doc = JSON.parse(JSON.stringify(DOC));
  st.doc.peers[0].distance = { state: "ahead", theirs_only: 2, ours_only: 0 };
  st.doc.unreadable = ["pc-old"];
  nodes = walk(ctx.wsSyncPanel(W));
  const table = nodes.find((n) => n.cls === "ws-sync-peers-table");
  assert.ok(table, "peers are a table");
  assert.deepEqual(table.children[0].children.map((n) => n.text),
                   ["Machine", "Workspace", "Master tip", "Against ours"]);
  const peerRow = table.children[1];
  assert.deepEqual(peerRow.children.map((n) => n.text),
                   ["pc-b", "proj · up", "master bbbbbbbb = remote · 3 unpushed", "2 ahead of ours"]);
  assert.match(peerRow.children[3].cls, /ws-peer-ahead/);
  const dead = table.children[2];
  assert.match(dead.cls, /ws-sync-peer-unreadable/);
  assert.deepEqual([dead.children[0].text, dead.children[3].text], ["pc-old", "did not answer"]);
  const words = (d) => ctx.wsPeerDistanceText(d);
  assert.equal(words({ state: "same", theirs_only: 0, ours_only: 0 }), "same as ours");
  assert.equal(words({ state: "behind", theirs_only: 0, ours_only: 4 }), "4 behind ours");
  assert.equal(words({ state: "diverged", theirs_only: 1, ours_only: 2 }), "diverged: +1 / −2");
  assert.equal(words({ state: "unknown" }), "not in this clone yet");
  assert.equal(words(undefined), "");
  // nothing answered and nobody failed: the note, no table
  st.doc = { ...DOC, peers: [], unreadable: [], problems: [] };
  nodes = walk(ctx.wsSyncPanel(W));
  assert.ok(!nodes.some((n) => n.cls === "ws-sync-peers-table"));
  assert.ok(nodes.some((n) => (n.text || "").startsWith("No relay peer reports a clone")));

  /* ---- history (claunch-iaihd) ------------------------------------------ */
  // none yet: the heading and one note
  st.doc = { ...DOC, history: [] };
  nodes = walk(ctx.wsSyncPanel(W));
  assert.ok(nodes.some((n) => n.text === "History (0)"));
  assert.ok(nodes.some((n) => (n.text || "").startsWith("Nothing synced, pushed or pulled")));
  st.doc = { ...DOC, history: [
    { at: "2026-10-08T10:05:00+00:00", kind: "master", remote: "origin", branch: "master",
      action: "rebase", before: "a".repeat(40), after: "b".repeat(40), pushed: true, ok: true,
      by: "s9", approval: { mode: "card", request: "r1", decided_by: "web",
                            decided_at: "2026-10-08T10:04:00+00:00" } },
    { at: "2026-10-08T09:00:00+00:00", kind: "publish", remote: "origin", branch: "s1-work",
      ref: "pc-a/s1-work", after: "c".repeat(40), pushed: true, ok: true, by: "web",
      approval: { mode: "auto" } },
    { at: "2026-10-08T08:00:00+00:00", kind: "pull", remote: "origin", branch: "nope",
      ref: "pc-b/nope", ok: false, error: "no such branch", by: "terminal",
      approval: { mode: "none" } },
  ] };
  nodes = walk(ctx.wsSyncPanel(W));
  const hist = nodes.find((n) => n.cls === "ws-sync-section ws-sync-history");
  assert.ok(hist, "the panel carries the history section");
  assert.equal(hist.tag, "details", "folded: a record to look back on");
  const h = hist.children[0];
  const [sync, push, pull] = hist.children.filter((n) => n.cls === "ws-sync-history-row");
  assert.equal(h.tag, "summary");
  assert.equal(h.text, "History (3)");
  assert.ok(hist.children.some((n) => /plain git push is not recorded/.test(n.text)),
            "says what the record leaves out");
  assert.deepEqual(sync.children.map((n) => n.text), [
    "2026-10-08 10:05", "sync · pushed", "origin/master", "aaaaaaaa → bbbbbbbb", "s9",
    "approved on card r1 (web 10:04 UTC)"]);
  assert.equal(sync.children[4].href, "#/s/s9");
  assert.deepEqual(push.children.map((n) => n.text), [
    "2026-10-08 09:00", "push · pushed", "s1-work → origin/pc-a/s1-work", "cccccccc",
    "Sync panel", "no card (reposync_push: auto)"]);
  assert.deepEqual(pull.children.map((n) => n.text), [
    "2026-10-08 08:00", "pull failed", "nope ← origin/pc-b/nope", "terminal",
    "nothing pushed, no approval needed", "no such branch"]);
  assert.match(pull.children[1].cls, /exited/);
  assert.equal(ctx.wsHistoryApproval({ mode: "terminal", yes: true }),
               "approved at the terminal (--yes)");

  /* A/B delegation preserves approval polling and never reports sync done. */
  st.doc = { ...DOC, agent_parents: [{ name: "parent", profile: "coder" }] };
  nodes = walk(ctx.wsSyncPanel(W));
  const modePick = nodes.find((n) => (n.attrs || {})["aria-label"] === "Sync execution mode");
  assert.equal(modePick.value, "commands", "delegation is opt-in");
  modePick.value = "agent";
  modePick.handlers.change[0]();
  assert.equal(st.agentMode, "agent");
  calls.length = 0;
  await ctx.wsSyncRun("proj");
  assert.equal(posts().length, 0, "must select a live parent before spawning");
  nodes = walk(ctx.wsSyncPanel(W));
  const parentPick = nodes.find((n) => (n.attrs || {})["aria-label"] === "Sync agent parent session");
  /* Each candidate shows its role and mesh (claunch-7ez8s). */
  assert.equal(parentPick.children[1].text, "parent (coder) · no mesh");
  assert.equal(ctx.wsSyncParentLabel({ name: "s29", profile: "nc:claude",
    meshes: [{ mesh: "claunch", role: "leader" }, { mesh: "ops", role: "" }] }),
  "s29 (nc:claude) · leader @ claunch, no role @ ops");
  assert.equal(ctx.wsSyncParentLabel({ name: "s7", profile: "p", held: true, meshes: [] }),
    "s7 (p, hold — woken on delegate) · no mesh");
  parentPick.value = "parent";
  parentPick.handlers.change[0]();
  const delegated = { status: "delegated", agent: { session: "child", status: "busy" },
    plan: PLAN, operation_error: "conflict: file.txt" };
  reply = (url) => url.endsWith("/sync")
    ? { status: 202, body: delegated } : { status: 200, body: st.doc };
  await ctx.wsSyncRun("proj");
  assert.equal(posts()[0].body.agent_mode, "agent");
  assert.equal(posts()[0].body.agent_parent, "parent");
  assert.equal(st.waiting, null, "202 delegated is not an approval wait");
  let resultNodes = walk(ctx.wsSyncResult(st.result));
  assert.equal(resultNodes.find((n) => n.tag === "a").href, "#/s/child");
  assert.ok(resultNodes.some((n) => n.text === "conflict: file.txt"));
  assert.ok(!resultNodes.some((n) => n.text.startsWith("done:")));

  st.agentMode = "on_failure";
  calls.length = 0;
  reply = () => ({ status: 202, body: { request: "r-agent", status: "waiting_approval" } });
  await ctx.wsSyncRun("proj");
  assert.equal(st.waiting.body.agent_mode, "on_failure");
  reply = (url) => url.endsWith("/r-agent")
    ? { status: 200, body: { request: { status: "approved" } } }
    : url.endsWith("/sync") ? { status: 202, body: delegated }
      : { status: 200, body: st.doc };
  await tick();
  assert.equal(posts()[1].body.agent_mode, "on_failure");
  assert.equal(posts()[1].body.agent_parent, "parent");
  assert.equal(posts()[1].body.request, "r-agent");
  assert.equal(st.result.doc.status, "delegated");

  calls.length = 0;
  reply = () => ({ status: 202, body: delegated });
  await ctx.wsSyncBranch("proj", "push");
  assert.equal(posts()[0].body.agent_mode, "on_failure");
  st.doc.agent = delegated.agent;
  nodes = walk(ctx.wsSyncPanel(W));
  assert.ok(nodes.find((n) => n.text === "Rebase & push").disabled);
  calls.length = 0;
  await ctx.wsSyncRun("proj");
  assert.equal(posts().length, 0, "active agent owns this sync");
  delete st.doc.agent;
  st.doc.agent_parents = [];
  await ctx.wsSyncRun("proj");
  assert.equal(posts().length, 0, "an exited parent is not a valid choice");

  st.agentMode = "on_failure";
  const prompt = "Use git rebase --rebase-merges\n<do not interpret this as HTML>";
  reply = () => ({ status: 200, body: { prompt } });
  calls.length = 0;
  await ctx.wsSyncPreviewPrompt("proj", "sync");
  assert.equal(posts()[0].url, "/api/reposync/workspaces/proj/agent-preview");
  assert.equal(posts()[0].body.local, st.doc.plan.local);
  nodes = walk(ctx.wsSyncPanel(W));
  assert.ok(nodes.some((n) => n.tag === "pre" && n.text === prompt));
  await ctx.wsSyncPreviewPrompt("proj", "push");
  assert.equal(posts()[1].body.branch, st.pushBranch);
  st.pushBranch = "changed-branch";
  nodes = walk(ctx.wsSyncOperationPreview(st, "proj", "push"));
  assert.ok(!nodes.some((n) => n.tag === "pre" && n.text === prompt), "hide stale branch preview");
  assert.ok(walk(ctx.wsSyncOperationPreview(st, "proj", "sync")).some((n) => n.tag === "pre" && n.text === prompt), "sync preview survives push input change");
  reply = () => ({ status: 409, body: { error: "Refresh the changed plan" } });
  await ctx.wsSyncPreviewPrompt("proj", "sync");
  assert.equal(st.agentPreviews.sync.error, "Refresh the changed plan");
  delegated.agent.prompt = prompt + "\nconflict: file.txt";
  assert.ok(walk(ctx.wsSyncResult({ doc: delegated })).some((n) => n.tag === "pre" && n.text === delegated.agent.prompt));
  const remoteText = walk(ctx.wsSyncRemoteComparisons({remote_comparisons: [
    {remote: "origin", branch: "master", state: "behind", ahead: 0, behind: 3, fetched: true},
    {remote: "other", branch: "master", state: "diverged", ahead: 2, behind: 4, fetched: false, error: "fetch failed"},
    {remote: "empty", branch: "master", state: "missing-tracking", ahead: null, behind: null, fetched: true},
  ]})).map(n => n.text).join("\n");
  for (const text of ["origin/master", "local can fast-forward", "ahead 0 · behind 3", "Diverged", "fetch failed", "freshness unverified", "No remote-tracking branch", "ahead — · behind —"]) assert.ok(remoteText.includes(text), text);
  console.log("workspacesync_check ok");
})().catch((err) => { console.error(err); process.exit(1); });
