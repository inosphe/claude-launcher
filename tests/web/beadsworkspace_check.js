/* The issue's target workspace in the detail pane.

   An issue says what to do and said nothing about where, so a session created
   from one had its directory picked by hand every time. The daemon records it
   as YAML front matter on the description; this pane reads it back as a row
   the operator can change, and keeps the front matter out of the Description
   section so the spec still reads as a spec.

   What must hold:

   1. `beadsStripMeta` takes off exactly what the daemon writes, and leaves a
      description that merely opens with a horizontal rule alone;
   2. the picker offers the registered workspaces and nothing else, selects
      what the issue records, and offers "none recorded" as a real answer;
   3. a daemon with no registered workspace says so instead of drawing an
      empty picker;
   4. changing the picker POSTs the chosen name to the right route, and a
      refusal leaves the picker showing what the issue still records. */
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
  return {
    tag, children: [], text: "", classes: new Set(), handlers: {},
    value: "", disabled: false,
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
  };
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

function tags(root, tag) {
  const out = [];
  const walk = (n) => {
    if (n.tag === tag) out.push(n);
    for (const c of n.children || []) walk(c);
  };
  walk(root);
  return out;
}

/* ---- the real code ----------------------------------------------------- */
const calls = [];
let answer = { ok: true, body: { workspace: "beta" } };

const ctx = {};
new Function(
  "exports", "el", "api", "record",
  "let beadsDetail = null;\n"
  + "let beadsFocus = '';\n"
  + "function renderBeads() { record('render'); }\n"
  + "function setState(detail, focus) { beadsDetail = detail; beadsFocus = focus; }\n"
  + "function detailState() { return beadsDetail; }\n"
  + "function openSessionModal(o) { record('modal:' + JSON.stringify(o)); }\n"
  + slice("beadsStripMeta") + slice("beadsWorkspaceBlock") + slice("beadsStartBlock")
  + "Object.assign(exports, { strip: beadsStripMeta, block: beadsWorkspaceBlock,"
  + " start: beadsStartBlock, setState, detailState });",
)(
  ctx,
  el,
  async (url, opts) => {
    calls.push({ url, ...opts });
    return {
      ok: answer.ok,
      status: answer.ok ? 200 : 400,
      json: async () => answer.body,
    };
  },
  (what) => calls.push({ event: what }),
);

/* ---- checks ------------------------------------------------------------ */
let failures = 0;
function check(what, got, want) {
  const a = JSON.stringify(got), b = JSON.stringify(want);
  if (a !== b) { failures++; console.error(`FAIL ${what}\n  got  ${a}\n  want ${b}`); }
  else console.log(`ok   ${what}`);
}

const SPEC = "## 목표\n무언가를 한다\n";

/* 1. the front matter is taken off the description, and only that */
check("front matter is stripped from the description",
      ctx.strip("---\nworkspace: alpha\n---\n" + SPEC), SPEC);
check("a description with no front matter is untouched", ctx.strip(SPEC), SPEC);
check("an unterminated fence is left alone",
      ctx.strip("---\nworkspace: alpha\n" + SPEC),
      "---\nworkspace: alpha\n" + SPEC);
check("and so is an empty description", ctx.strip(""), "");
check("prose that merely opens with a rule keeps its first line",
      ctx.strip("--- a rule ---\ntext\n"), "--- a rule ---\ntext\n");

/* 2. the picker */
const WORKSPACES = [
  { name: "alpha", path: "F:/works/alpha" },
  { name: "beta", path: "F:/works/beta" },
];
ctx.setState(
  { root: "/repo", workspace: "alpha", workspaces: WORKSPACES, issue: {} },
  "claunch-1",
);
let block = ctx.block();
check("the picker offers the registered workspaces and an empty answer",
      tags(block, "option").map((o) => o.value), ["", "alpha", "beta"]);
check("an option names the path so two similar names are distinguishable",
      tags(block, "option")[1].text, "alpha — F:/works/alpha");
check("and it opens on what the issue records",
      find(block, "beads-ws-pick")[0].value, "alpha");

ctx.setState(
  { root: "/repo", workspace: "", workspaces: WORKSPACES, issue: {} },
  "claunch-1",
);
check("an issue recording none opens on the empty answer",
      find(ctx.block(), "beads-ws-pick")[0].value, "");

/* 3. a daemon with nothing registered says so */
ctx.setState({ root: "/repo", workspace: "", workspaces: [], issue: {} }, "claunch-1");
block = ctx.block();
check("nothing registered draws no picker",
      [find(block, "beads-ws-pick").length, find(block, "wf-note")[0].text],
      [0, "no workspace is registered on this daemon (claunch workspace add <dir>)"]);
ctx.setState({ root: "/repo", workspace: "gone", workspaces: [], issue: {} }, "claunch-1");
check("but a recorded name is still shown when the registry cannot offer it",
      find(ctx.block(), "wf-note")[0].text,
      "gone — no workspace is registered on this daemon, so it cannot be changed here");

/* 4. the write. Wrapped rather than awaited at the top level: a top-level
   await turns this file into an ES module and `require` stops existing. */
async function writes() {
ctx.setState(
  { root: "/repo", workspace: "alpha", workspaces: WORKSPACES, issue: {} },
  "claunch-1",
);
block = ctx.block();
let pick = find(block, "beads-ws-pick")[0];
pick.value = "beta";
calls.length = 0;
await pick.handlers.change[0]();
check("the change POSTs the chosen name against the issue's own board",
      [calls[0].url, calls[0].method, JSON.parse(calls[0].body)],
      ["/api/beads/claunch-1/workspace", "POST",
       { workspace: "beta", cwd: "/repo" }]);
check("and the pane is redrawn, since the description carries the block",
      calls.some((c) => c.event === "render"), true);

answer = { ok: false, body: { error: "no workspace named 'nowhere'" } };
ctx.setState(
  { root: "/repo", workspace: "alpha", workspaces: WORKSPACES, issue: {} },
  "claunch-1",
);
block = ctx.block();
pick = find(block, "beads-ws-pick")[0];
pick.value = "beta";
await pick.handlers.change[0]();
check("a refusal is shown and the picker goes back to what is recorded",
      [find(block, "beads-ws-note")[0].text, pick.value, pick.disabled],
      ["no workspace named 'nowhere'", "alpha", false]);

}

writes().then(() => {
  /* ---- starting a session on the issue ----------------------------------- */
ctx.setState(
  { root: "/repo", workspace: "alpha", workspaces: WORKSPACES,
    issue: { id: "claunch-7" } },
  "claunch-7",
);
let start = ctx.start();
check("two answers are offered, and neither is the default",
      tags(start, "button").map((b) => b.text), ["New session", "Spawn child"]);
check("the recorded workspace is named where the operator can see it",
      find(start, "wf-note")[0].text,
      "opens in alpha, which this issue records");
check("and the blank-parent consequence is stated once",
      find(start, "wf-note")[1].text,
      "Spawn child asks for the parent in the form; leaving it blank creates a "
      + "root session instead");

calls.length = 0;
tags(start, "button")[0].handlers.click[0]();
check("New session opens the modal on the new tab, seeded with issue and workspace",
      JSON.parse(String(calls[0].event).slice("modal:".length)),
      { tab: "new", seed: { issue: "claunch-7", workspace: "alpha" } });
calls.length = 0;
tags(start, "button")[1].handlers.click[0]();
check("Spawn child opens the same modal on the spawn tab",
      JSON.parse(String(calls[0].event).slice("modal:".length)).tab, "spawn");

ctx.setState(
  { root: "/repo", workspace: "", workspaces: WORKSPACES,
    issue: { id: "claunch-8" } },
  "claunch-8",
);
start = ctx.start();
calls.length = 0;
tags(start, "button")[0].handlers.click[0]();
check("an issue recording no workspace seeds only the issue id",
      JSON.parse(String(calls[0].event).slice("modal:".length)).seed,
      { issue: "claunch-8" });
check("and says the form will open on its own default",
      find(start, "wf-note")[0].text,
      "this issue records no workspace, so the form opens on its own default");

if (failures) process.exit(1);
  console.log("beadsworkspace_check ok");
});
