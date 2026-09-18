/* The board's two halves: backlog and TODO.

   A board is read for one question first — what has nobody taken up yet — and
   six equal status lanes did not answer it. `open` is that pile: an issue
   exists and no assignee has moved it. Everything from `in_ready` onwards,
   `closed` included, is work already spoken for.

   Four things must hold:

   1. every status lands in exactly one group, and the split is where the
      names say it is (`open` alone on the backlog side);
   2. a group's count is the cards under it, not the number of lanes;
   3. a group with no visible lane is not drawn — a filter narrowed to one
      status draws one lane under its own group and no empty header beside it;
   4. the board section draws the groups in backlog-then-TODO order, and the
      lanes inside a group keep the status order they had before the split.

   The real functions are sliced out of app.js and driven against a stub DOM,
   the same way beadskanban_check.js does it. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, children: [], text: "", classes: new Set(),
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {},
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
  };
  return n;
}

function el(tag, cls, text) {
  const n = node(tag);
  if (cls) for (const c of String(cls).split(/\s+/).filter(Boolean)) n.classes.add(c);
  if (text !== undefined && text !== null) n.text = String(text);
  return n;
}

function find(root, cls) {
  const out = [];
  const walk = (n) => {
    if (n.classes && n.classes.has(cls)) out.push(n);
    for (const c of n.children || []) walk(c);
  };
  walk(root);
  return out;
}

/* ---- the real code ----------------------------------------------------- */
const ctx = {};
new Function(
  "exports", "el",
  "const BEADS_STATUSES = " + JSON.stringify(
    ["open", "in_ready", "in_progress", "in_review", "blocked", "closed"]) + ";\n"
  + "const BEADS_ACTIVE = new Set([\"open\", \"in_ready\", \"in_progress\", "
  + "\"in_review\", \"blocked\"]);\n"
  + "const BEADS_BACKLOG = new Set([\"open\"]);\n"
  + "const BEADS_GROUPS = " + JSON.stringify([
      { key: "backlog", title: "backlog", note: "nobody has taken these up" },
      { key: "todo", title: "TODO", note: "taken up, in review, or finished" },
    ]) + ";\n"
  // A lane that draws its rows as plain nodes: this file is about the
  // grouping, and beadskanban_check.js already pins what a card looks like.
  + "function beadsLane(status, rows) {\n"
  + "  const lane = el('div', 'beads-lane ' + status);\n"
  + "  lane.appendChild(el('span', 'beads-lane-name', status));\n"
  + "  for (const r of rows) lane.appendChild(el('div', 'beads-card', r.issue.id));\n"
  + "  return lane;\n"
  + "}\n"
  + slice("beadsLanes") + slice("beadsGroupOf") + slice("beadsLaneGroups")
  + slice("beadsGroupBlock")
  + "Object.assign(exports, { lanes: beadsLanes, groupOf: beadsGroupOf,"
  + " laneGroups: beadsLaneGroups, block: beadsGroupBlock });",
)(ctx, el);

/* ---- checks ------------------------------------------------------------ */
let failures = 0;
function check(what, got, want) {
  const a = JSON.stringify(got), b = JSON.stringify(want);
  if (a !== b) { failures++; console.error(`FAIL ${what}\n  got  ${a}\n  want ${b}`); }
  else console.log(`ok   ${what}`);
}

const STATUSES = ["open", "in_ready", "in_progress", "in_review", "blocked", "closed"];

/* 1. every status is in exactly one group, and open is the backlog */
check("open is the backlog and nothing else is",
      STATUSES.map(ctx.groupOf),
      ["backlog", "todo", "todo", "todo", "todo", "todo"]);

/* A row as beadsLaneRows hands it over: the issue and its indent. */
const row = (id) => ({ issue: { id }, depth: 0 });
const rowsFor = (per) => (status) => (per[status] || []).map(row);

/* 2. the count on a header is cards, not lanes */
let groups = ctx.laneGroups(ctx.lanes("active"), rowsFor({
  open: ["a", "b", "c"],
  in_ready: ["d"],
  in_progress: ["e", "f"],
}));
check("both halves are drawn, backlog first",
      groups.map((g) => g.key), ["backlog", "todo"]);
check("the TODO half keeps the status order it had before the split",
      groups[1].lanes.map((l) => l.status),
      ["in_ready", "in_progress", "in_review", "blocked"]);
check("a header counts the cards under it",
      groups.map((g) => ctx.block(g))
        .map((b) => find(b, "beads-group-count")[0].text),
      ["3", "3"]);

/* 3. a group with no visible lane is not drawn at all */
groups = ctx.laneGroups(ctx.lanes("open"), rowsFor({ open: ["a"] }));
check("a filter narrowed to open draws the backlog half alone",
      groups.map((g) => [g.key, g.lanes.map((l) => l.status)]),
      [["backlog", ["open"]]]);
groups = ctx.laneGroups(ctx.lanes("in_review"), rowsFor({ in_review: ["a"] }));
check("and a filter narrowed to in_review draws the TODO half alone",
      groups.map((g) => [g.key, g.lanes.map((l) => l.status)]),
      [["todo", ["in_review"]]]);

/* `closed` is TODO's, not a third group: the split is about who has taken an
   issue up, and a finished issue was taken up. */
groups = ctx.laneGroups(ctx.lanes("all"), rowsFor({ open: ["a"], closed: ["b"] }));
check("closed rides in the TODO half",
      groups.map((g) => g.lanes.map((l) => l.status)),
      [["open"], ["in_ready", "in_progress", "in_review", "blocked", "closed"]]);

/* 4. the block itself: a header with the name, the count and the note, then
   the lane grid. */
const block = ctx.block(ctx.laneGroups(ctx.lanes("active"),
  rowsFor({ open: ["a", "b"] }))[0]);
check("the group block names itself", find(block, "beads-group-name")[0].text, "backlog");
check("carries the note that says what the half means",
      find(block, "beads-group-note")[0].text, "nobody has taken these up");
check("and wraps its lanes in one grid",
      [find(block, "beads-lanes").length, find(block, "beads-lane").length], [1, 1]);
check("the empty half still draws its lanes, just with no cards",
      find(ctx.block(ctx.laneGroups(ctx.lanes("active"), rowsFor({}))[1]),
           "beads-card").length, 0);

if (failures) process.exit(1);
console.log("beadsgroups_check ok");
