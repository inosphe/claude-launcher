/* The Workspaces page's Refresh all (claunch-87eqx), run against the real
   wsRefreshAllRun / wsRefreshAllBadge / wsRefreshAllBar from app.js.

   What has to hold: one press refreshes every present workspace's Sync
   status with a fetch first (wsSyncRefresh(name, true)) and skips a missing
   one; no more than WS_REFRESH_ALL_CAP refreshes run at once; every row
   says where it is -- queued, fetching…, refreshed, or refresh failed with
   the error as its tooltip; the bar counts the finished ones and the
   failures; a second press while a run goes starts nothing; and once all
   are through, the row badges are read again (refreshWsGitSummary). */
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

function consts() {
  const start = src.indexOf("const WS_REFRESH_ALL_CAP");
  const end = src.indexOf("async function wsRefreshAllRun(");
  if (start < 0 || end < start) throw new Error("cannot locate the Refresh all constants");
  return src.slice(start, end);
}

function el(tag, cls, text) {
  return {
    tag, cls: cls || "", text: text === undefined ? "" : String(text),
    children: [], handlers: {}, title: "", disabled: false,
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(ev, fn) { (this.handlers[ev] = this.handlers[ev] || []).push(fn); },
  };
}

// Each refresh waits until the check lets it go, so the lanes can be counted.
const pending = new Map();   // name -> resolve
const started = [];
let running = 0;
let peak = 0;
const sync = new Map();      // name -> {error}
let summaries = 0;
let renders = 0;
const ctx = {
  el,
  wsOpen: true,
  workspacesCache: [
    { name: "a", exists: true }, { name: "b", exists: true },
    { name: "gone", exists: false },
    { name: "c", exists: true }, { name: "d", exists: true },
  ],
  renderWorkspaces: () => { renders++; },
  wsSyncSt: (name) => {
    if (!sync.has(name)) sync.set(name, { error: "" });
    return sync.get(name);
  },
  wsSyncRefresh: (name, fetchFirst) => {
    assert.equal(fetchFirst, true, "Refresh all fetches first");
    started.push(name);
    running++;
    peak = Math.max(peak, running);
    return new Promise((resolve) => {
      pending.set(name, (error) => {
        ctx.wsSyncSt(name).error = error || "";
        running--;
        pending.delete(name);
        resolve();
      });
    });
  },
  refreshWsGitSummary: async () => { summaries++; },
};
vm.createContext(ctx);
vm.runInContext(
  consts() +
  [slice("wsRefreshAllRun"), slice("wsRefreshAllBadge"), slice("wsRefreshAllBar")].join("\n") +
  "\nthis.run = wsRefreshAllRun; this.badge = wsRefreshAllBadge; this.bar = wsRefreshAllBar;" +
  "\nthis.cap = WS_REFRESH_ALL_CAP; this.all = wsRefreshAll;",
  ctx
);

const tick = () => new Promise((r) => setImmediate(r));
const badgeText = (name) => { const b = ctx.badge(name); return b ? b.text : null; };
const barOf = () => {
  const bar = ctx.bar();
  const btn = bar.children.find((c) => c.tag === "button");
  const note = bar.children.find((c) => c.cls === "wf-note");
  return { btn, note: note ? note.text : null };
};

(async () => {
  assert.equal(ctx.cap, 2);
  // Before any run: no badge on any row, the button is live, no count.
  assert.equal(ctx.badge("a"), null);
  let bar = barOf();
  assert.equal(bar.btn.text, "Refresh all (fetch)");
  assert.equal(bar.btn.disabled, false);
  assert.match(bar.btn.title, /2 at a time/);
  assert.equal(bar.note, null);

  // Press: the first two lanes start, the rest are queued, the missing one is not in it.
  bar.btn.handlers.click[0]();
  await tick();
  assert.deepEqual([...started], ["a", "b"]);
  assert.equal(running, 2);
  assert.deepEqual(["a", "b", "c", "d"].map(badgeText),
                   ["fetching…", "fetching…", "queued", "queued"]);
  assert.equal(badgeText("gone"), null, "a missing workspace is not refreshed");
  bar = barOf();
  assert.equal(bar.btn.text, "Refreshing…");
  assert.equal(bar.btn.disabled, true);
  assert.equal(bar.note, "0/4 refreshed");

  // A second press while it goes starts nothing.
  await ctx.run();
  assert.deepEqual([...started], ["a", "b"]);

  // a fails, its lane takes c; the cap holds.
  pending.get("a")("fetch: could not read from remote");
  await tick();
  assert.deepEqual([...started], ["a", "b", "c"]);
  assert.equal(badgeText("a"), "refresh failed");
  assert.match(ctx.badge("a").title, /could not read from remote/);
  assert.match(ctx.badge("a").cls, /exited/);
  assert.equal(badgeText("c"), "fetching…");
  assert.equal(barOf().note, "1/4 refreshed, 1 failed");

  pending.get("b")();
  await tick();
  assert.deepEqual([...started], ["a", "b", "c", "d"]);
  assert.equal(badgeText("b"), "refreshed");
  assert.equal(summaries, 0, "the row badges are read once, at the end");

  pending.get("c")();
  pending.get("d")();
  await tick(); await tick();
  assert.equal(peak, 2, "never more than the cap at once");
  assert.equal(summaries, 1, "the row badges are read again after the run");
  assert.equal(ctx.all.active, false);
  assert.deepEqual(["a", "b", "c", "d"].map(badgeText),
                   ["refresh failed", "refreshed", "refreshed", "refreshed"]);
  bar = barOf();
  assert.equal(bar.btn.text, "Refresh all (fetch)");
  assert.equal(bar.btn.disabled, false);
  assert.equal(bar.note, "4/4 refreshed, 1 failed");
  assert.ok(renders > 0);

  // A new run starts over: every row queued again, a's old failure forgotten.
  started.length = 0;
  ctx.run();
  await tick();
  assert.deepEqual([...started], ["a", "b"]);
  assert.equal(barOf().note, "0/4 refreshed");
  for (const name of ["a", "b"]) pending.get(name)();
  await tick();
  for (const name of ["c", "d"]) pending.get(name)();
  await tick(); await tick();
  assert.equal(barOf().note, "4/4 refreshed");
  assert.equal(summaries, 2);

  // Nothing present: the press does nothing at all.
  ctx.workspacesCache = [{ name: "gone", exists: false }];
  started.length = 0;
  await ctx.run();
  assert.equal(started.length, 0);
  assert.equal(summaries, 2);

  console.log("ok");
})().catch((err) => { console.error(err); process.exit(1); });
