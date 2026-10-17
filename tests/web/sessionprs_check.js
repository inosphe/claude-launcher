const fs = require("fs");
const path = require("path");
const assert = require("assert");
const src = fs.readFileSync(path.join(__dirname, "../../src/claude_launcher/web/static/app.js"), "utf8");
const start = src.indexOf("function sessionPrRows(");
const end = src.indexOf("function sessDetailTabs(", start);
function el(tag, className, textContent) {
  return {tag, className, textContent, children: [], title: "", parent: null, listeners: {},
    appendChild(child) { child.parent = this; this.children.push(child); return child; },
    addEventListener(type, fn) { this.listeners[type] = fn; },
    set innerHTML(_) { this.children = []; },
    remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); },
  };
}
// The card: querySelector by class over its direct children, and the insert
// railNotePlace uses (before .rail-cwd).
function card() {
  const li = el("li", "");
  const cwd = el("div", "rail-cwd");
  li.appendChild(cwd);
  li.querySelector = (sel) => li.children.find((c) => `.${c.className}` === sel) || null;
  li.insertBefore = (node, ref) => {
    node.parent = li;
    li.children.splice(li.children.indexOf(ref), 0, node);
  };
  return li;
}
function railNotePlace(li, node) {
  const cwd = li.querySelector(".rail-cwd");
  if (cwd) li.insertBefore(node, cwd);
  else li.appendChild(node);
}
// The selected card's read of every commit (claunch-7myrv): each call is
// recorded and answered by whatever `railAnswer` holds.
const railCalls = [];
let railAnswer = null;
function fetchStub(path) {
  railCalls.push(path);
  const body = railAnswer;
  return Promise.resolve({ok: !!body, json: () => Promise.resolve(body)});
}
const api = new Function("el", "railNotePlace", "api", src.slice(start, end)
  + "return {sessionPrPanel, decorateCommitsRow, railCommitChip};")(el, railNotePlace, fetchStub);
assert.equal(api.sessionPrPanel({prs: []}), null);
const pr = {url: "https://ghe.example.com/o/r/pull/7", number: 7,
  repo: "ghe.example.com/o/r", title: "<script>literal title</script>",
  branch: "s1-feature", base: "master", status: "merged", issues: ["issue-1"]};
const panel = api.sessionPrPanel({prs: [pr]});
const row = panel.children[2];
const link = row.children[0];
assert.equal(link.href, pr.url);
assert.equal(link.target, "_blank");
assert.equal(link.rel, "noopener noreferrer");
assert(link.textContent.includes(pr.title));
assert(row.children[2].textContent.includes("s1-feature → master"));
assert(row.children[2].textContent.includes("merged"));
for (const url of ["javascript:alert(1)", "https://u:p@ghe.example.com/o/r/pull/7", "https://ghe.example.com/o/r/issues/7"]) {
  assert.equal(api.sessionPrPanel({prs: [{...pr, url}]}), null);
  const li = card();
  api.decorateCommitsRow(li, {prs: [{...pr, url}]});
  assert.equal(li.querySelector(".rail-commits"), null, "an unsafe PR url draws no frame");
}

// The marks: one glyph per state, and the 7-character hash beside it.
const chip = (state) => api.railCommitChip({short: "abcdef12", subject: "s", state});
for (const [state, mark] of [["merged", "✓"], ["pushed", "↑"], ["local", "○"]]) {
  const c = chip(state);
  assert.equal(c.className, `rail-commit rail-commit-${state}`);
  assert.equal(c.children[0].textContent, mark);
  assert.equal(c.children[1].textContent, "abcdef1");
  assert(c.title.includes("abcdef12 s"));
}
assert.equal(chip("weird").className, "rail-commit rail-commit-unknown");
assert.equal(chip("weird").children[0].textContent, "?");

// Nothing to draw: no line, and a line drawn earlier goes.
let li = card();
api.decorateCommitsRow(li, {});
assert.equal(li.querySelector(".rail-commits"), null);

// The PR frames the commits it carries; the rest follow the frames.
const commits = [
  {short: "aaaaaaa1", subject: "loose", state: "local"},
  {short: "bbbbbbb2", subject: "in seven", state: "pushed", pr: 7},
  {short: "ccccccc3", subject: "in seven too", state: "merged", pr: 7},
  {short: "ddddddd4", subject: "unknown pr", state: "merged", pr: 99},
];
const second = {...pr, url: "https://ghe.example.com/o/r/pull/8", number: 8, status: "open"};
api.decorateCommitsRow(li, {prs: [pr, second], rail_commits: {commits, more: 3}});
const line = li.querySelector(".rail-commits");
assert(line, "the line is drawn");
assert.equal(li.children.indexOf(line), 0, "placed before the cwd line");
const [frame7, frame8, loose1, loose2, more] = line.children;
assert.equal(frame7.className, "rail-prgroup rail-prgroup-merged");
assert.equal(frame7.children[0].className, "rail-prgroup-label");
assert.equal(frame7.children[0].textContent, "#7");
assert.equal(frame7.children[0].href, pr.url);
assert.equal(frame7.children[0].rel, "noopener noreferrer");
assert.equal(typeof frame7.children[0].listeners.click, "function");
assert.deepEqual(frame7.children.slice(1).map((c) => c.children[1].textContent), ["bbbbbbb", "ccccccc"]);
assert.equal(frame8.className, "rail-prgroup rail-prgroup-open");
assert.equal(frame8.children.length, 1, "a PR with no known commits is its number alone");
assert.deepEqual([loose1, loose2].map((c) => c.children[1].textContent), ["aaaaaaa", "ddddddd"]);
assert.equal(more.className, "rail-commit-more");
assert.equal(more.textContent, "+3");

// Redrawn in place: the same node, new content.
api.decorateCommitsRow(li, {prs: [], rail_commits: {commits: [commits[0]], more: 0}});
assert.equal(li.querySelector(".rail-commits"), line);
assert.equal(line.children.length, 1);
api.decorateCommitsRow(li, {prs: []});
assert.equal(li.querySelector(".rail-commits"), null, "cleared commits remove the line");

// The selected card unfolds its +N: one read of every commit, drawn in place
// of the newest few, and no second read while the key has not moved.
(async () => {
  const selected = () => {
    const c = card();
    c.classList = {on: true, contains: (k) => k === "active" && c.classList.on};
    c.isConnected = true;
    return c;
  };
  const few = {commits: [commits[0]], more: 2};
  const every = {commits: [commits[0],
    {sha: "e", short: "eeeeeee5", subject: "older", state: "merged"},
    {sha: "f", short: "fffffff6", subject: "oldest", state: "merged"}], more: 0};
  const s = {name: "s 1", prs: [], rail_commits: few};

  let li = card();
  api.decorateCommitsRow(li, s);
  assert.equal(railCalls.length, 0, "an unselected card reads nothing");
  assert.equal(li.querySelector(".rail-commits").children.at(-1).textContent, "+2");

  li = selected();
  railAnswer = {session: "s 1", rail_commits: every};
  api.decorateCommitsRow(li, s);
  assert.deepEqual(railCalls, ["/api/sessions/s%201/rail-commits"]);
  await new Promise((r) => setTimeout(r, 0));
  let line = li.querySelector(".rail-commits");
  assert.deepEqual(line.children.map((c) => c.children[1] && c.children[1].textContent),
    ["aaaaaaa", "eeeeeee", "fffffff"], "every commit, and no +N");

  api.decorateCommitsRow(li, s);
  assert.equal(railCalls.length, 1, "a poll redraw reuses what was read");
  assert.equal(li.querySelector(".rail-commits").children.length, 3);

  li.classList.on = false;
  api.decorateCommitsRow(li, s);
  assert.equal(li.querySelector(".rail-commits").children.at(-1).textContent, "+2", "folded again");

  // A failed read keeps the +N and is not retried until the key moves.
  const t = {name: "s2", prs: [], rail_commits: few};
  li = selected();
  railAnswer = null;
  api.decorateCommitsRow(li, t);
  await new Promise((r) => setTimeout(r, 0));
  api.decorateCommitsRow(li, t);
  assert.equal(railCalls.filter((p) => p.includes("/s2/")).length, 1);
  assert.equal(li.querySelector(".rail-commits").children.at(-1).textContent, "+2");
  api.decorateCommitsRow(li, {...t, rail_commits: {commits: [commits[1]], more: 2}});
  assert.equal(railCalls.filter((p) => p.includes("/s2/")).length, 2, "a new commit reads again");
  console.log("selected card unfolds its commits: passed");
})().catch((e) => { console.error(e); process.exit(1); });

assert(!src.includes("sessionPrBadge"), "the name-line PR badge is gone");
assert(src.includes('if (typeof decorateCommitsRow === "function") decorateCommitsRow(li, s);'));
assert(src.includes('if (typeof decorateCommitsRow === "function") decorateCommitsRow(keptRow, s);'));
assert(src.includes("const prs = sessionPrPanel(s)"));
console.log("session PR frames, commit chips and details: passed");
