/* Disk usage on the Workspaces rows (claunch-2oait), run against the real
   refreshWsUsage / wsUsagePoll / wsUsageMeasure / wsFmtBytes / wsUsageButton
   from app.js.

   What has to hold: a row with no measurement offers "Measure size"; one
   with a measurement shows the size and how old it is, with every checkout
   and the time it was taken in the tooltip; a press POSTs to the workspace's
   usage route and the row says measuring… (disabled) at once; the page polls
   only while a measurement runs and stops once none does; a failed one keeps
   the last number and says it failed; an older daemon (404) leaves the rows
   without a size. */
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
    children: [], handlers: {}, title: "", disabled: false,
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(ev, fn) { (this.handlers[ev] = this.handlers[ev] || []).push(fn); },
  };
}

let answer = null;
const calls = [];
const timers = [];
let renders = 0;
const ctx = {
  el,
  wsOpen: true,
  renderWorkspaces: () => { renders++; },
  setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
  api: async (url, opts) => {
    calls.push([url, (opts && opts.method) || "GET"]);
    return typeof answer === "function" ? answer(url, opts) : answer;
  },
};
vm.createContext(ctx);
vm.runInContext(
  "let wsUsage = {};\nlet wsUsageTimer = null;\n" +
  [slice("fmtAge"), slice("refreshWsUsage"), slice("wsUsagePoll"), slice("wsUsageMeasure"),
   slice("wsFmtBytes"), slice("wsUsageButton")].join("\n") +
  "\nthis.refresh = refreshWsUsage; this.measure = wsUsageMeasure; this.fmt = wsFmtBytes;" +
  "\nthis.button = wsUsageButton; this.usage = () => wsUsage;",
  ctx
);

const flush = () => new Promise((r) => setImmediate(r));
const json = (doc) => ({ ok: true, status: 200, json: async () => doc, text: async () => "" });
const NOW = Date.parse("2026-10-08T12:00:00Z");

// sizes as a person reads them, up to gigabytes
assert.equal(ctx.fmt(512), "512 B");
assert.equal(ctx.fmt(1536), "1.5 KB");
assert.equal(ctx.fmt(300 * 1024 * 1024), "300 MB");
assert.equal(ctx.fmt(3.2 * 1024 ** 3), "3.2 GB");
assert.equal(ctx.fmt(2048 * 1024 ** 3), "2048 GB");
assert.equal(ctx.fmt(undefined), "?");

(async () => {
  // never measured: the row offers a measurement
  let b = ctx.button({ name: "proj" }, NOW);
  assert.equal(b.text, "Measure size");
  assert.match(b.title, /can take a while/);

  // the cache lands in the rows; nothing runs, so nothing polls
  answer = json({ workspaces: { proj: {
    bytes: 3 * 1024 ** 3, files: 120, seconds: 4.5, at: "2026-10-08T11:58:00+00:00",
    parts: [{ path: "C:/r", bytes: 2 * 1024 ** 3, files: 100, workspace: true },
            { path: "C:/r/.claude/worktrees/wt-a", bytes: 1024 ** 3, files: 20, workspace: false }],
  } } });
  await ctx.refresh();
  assert.equal(renders, 1);
  assert.equal(timers.length, 0);
  b = ctx.button({ name: "proj" }, NOW);
  assert.equal(b.text, "3.0 GB · 2m ago");
  assert.match(b.title, /3\.0 GB in 120 files: the repository and 1 worktree, measured 2026-10-08T11:58:00\+00:00 \(took 4\.5s\)/);
  assert.match(b.title, /2\.0 GB {2}C:\/r\n/);
  assert.match(b.title, /1\.0 GB {2}C:\/r\/\.claude\/worktrees\/wt-a/);
  assert.match(b.title, /click to measure again/);
  assert.doesNotMatch(b.cls, /ws-usage-failed/);

  // a press: POST to the workspace's route, measuring… at once, then a poll
  calls.length = 0;
  answer = { ok: true, status: 202, json: async () => ({ running: true }) };
  b.handlers.click[0]();
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(calls[0], ["/api/workspaces/proj/usage", "POST"]);
  b = ctx.button({ name: "proj" }, NOW);
  assert.equal(b.text, "measuring…");
  assert.equal(b.disabled, true);
  assert.equal(timers.length, 1, "polls while it runs");
  assert.equal(timers[0].ms, 2000);

  // the poll reads it still running and arms one more; then it is done
  answer = json({ workspaces: { proj: { ...ctx.usage().proj, running: true } } });
  timers.shift().fn();
  await flush();
  assert.equal(timers.length, 1);
  answer = json({ workspaces: { proj: { bytes: 1024, files: 1, seconds: 0.1, parts: [],
                                        at: "2026-10-08T11:59:59+00:00" } } });
  timers.shift().fn();
  await flush();
  assert.equal(timers.length, 0, "no poll once nothing runs");
  assert.equal(ctx.button({ name: "proj" }, NOW).text, "1.0 KB · 1s ago");

  // a failed measurement keeps the last number and says so
  answer = json({ workspaces: { proj: { bytes: 1024, files: 1, seconds: 0.1, parts: [],
                                        at: "2026-10-08T11:59:59+00:00",
                                        error: "access denied", failed_at: "2026-10-08T12:00:00+00:00" } } });
  await ctx.refresh();
  b = ctx.button({ name: "proj" }, NOW);
  assert.equal(b.text, "1.0 KB · 1s ago");
  assert.match(b.cls, /ws-usage-failed/);
  assert.match(b.title, /the last measurement failed: access denied/);

  // a refused press (unknown workspace) is not left spinning
  answer = { ok: false, status: 404, json: async () => ({}), text: async () => "no workspace named 'x'" };
  await ctx.measure("x");
  b = ctx.button({ name: "x" }, NOW);
  assert.equal(b.text, "Measure size");
  assert.match(b.title, /no workspace named/);

  // an older daemon: 404 leaves the cache as it was
  const before = renders;
  answer = { ok: false, status: 404, json: async () => ({}) };
  await ctx.refresh();
  assert.equal(renders, before);
  assert.equal(ctx.usage().proj.bytes, 1024);

  console.log("wsusage_check ok");
})().catch((e) => { console.error(e); process.exit(1); });
