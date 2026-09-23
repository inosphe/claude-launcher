/* The board's own create form (POST /api/beads).

   The board is where work is decided, and until this existed the only way to
   put an issue on it was a shell — so an operator who saw the gap while
   reading the board had to leave it to file one, and the issue that was
   noticed and not filed is the one that goes missing.

   What must hold:

   1. the form is folded until it is asked for, and asking for it is what
      fetches its option sets — a board poll must not pay for them;
   2. what it posts is what was typed, against the board the workspace tabs
      have selected;
   3. a title is refused in the page rather than travelling to the daemon to
      be refused there;
   4. a filed issue opens, so the next thing (starting a session on it) is
      one click away rather than a search. */
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
  const head = src.slice(start - 6, start) === "async " ? start - 6 : start;
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  return {
    tag, children: [], text: "", classes: new Set(), handlers: {},
    value: "", disabled: false, rows: 0, placeholder: "", href: "", title: "",
    type: "",
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { for (const c of cs) this.children.push(c); },
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
const hash = [];
let answer = { ok: true, status: 201, body: { issue: "claunch-new", root: "/repo" } };

const ctx = {};
new Function(
  "exports", "el", "api", "document", "location",
  "let beadsOpen = true, beadsSection = 'board', beadsWorkspace = '/repo';\n"
  + "function renderBeads() {}\n"
  + "function restartBeadsStream() { exports.restarted = true; }\n"
  + "let beadsCache = null;\n"
  + slice("beadsBoardLabel") + slice("beadsBoardWhere")
  + slice("loadBeadsNewOptions") + slice("submitNewIssue") + slice("beadsNewBlock")
  + "let beadsNew = { open: false, busy: false, error: '', done: '',"
  + "  boards: null, workspaces: [], loading: false,"
  + "  draft: { title: '', description: '', priority: '2', type: 'task',"
  + "           labels: '', assignee: '', workspace: '', status: 'open' } };\n"
  + "Object.assign(exports, { block: beadsNewBlock, load: loadBeadsNewOptions,"
  + "  submit: submitNewIssue, state: () => beadsNew,"
  + "  board: (r) => { beadsWorkspace = r; } });",
)(
  ctx,
  el,
  async (url, opts) => {
    calls.push({ url, ...opts });
    return { ok: answer.ok, status: answer.status, json: async () => answer.body };
  },
  { createTextNode: (t) => el("span", null, t) },
  { set hash(v) { hash.push(v); }, get hash() { return hash[hash.length - 1] || ""; } },
);

/* ---- checks ------------------------------------------------------------ */
let failures = 0;
function check(what, got, want) {
  const a = JSON.stringify(got), b = JSON.stringify(want);
  if (a !== b) { failures++; console.error(`FAIL ${what}\n  got  ${a}\n  want ${b}`); }
  else console.log(`ok   ${what}`);
}

(async () => {
  /* 1. folded until asked for */
  let block = ctx.block();
  check("folded, the form is one button and nothing else",
        [tags(block, "button").map((b) => b.text), find(block, "beads-new-form").length],
        [["+ New issue"], 0]);
  check("and it has asked the daemon for nothing", calls.length, 0);

  tags(block, "button")[0].handlers.click[0]();
  check("opening it is what fetches the option sets",
        calls.map((c) => c.url), ["/api/beads/boards"]);
  answerBoards();
  await flush();

  block = ctx.block();
  check("open, it draws a field per answer the board takes",
        find(block, "beads-new-row").map((r) => find(r, "beads-new-label")[0].text),
        ["Title", "Description", "Priority", "Type", "Labels", "Assignee",
         "Workspace", "Board"]);
  check("the assignee picker offers the sessions on THIS board only",
        tags(find(block, "beads-new-row")[5], "option").map((o) => o.value),
        ["", "s1", "s2"]);
  check("the workspace picker offers the registry and an empty answer",
        tags(find(block, "beads-new-row")[6], "option").map((o) => o.value),
        ["", "alpha"]);
  check("and the board it will be filed on is named, not guessed at",
        find(block, "beads-new-board")[0].text, "/repo");

  /* 3. a title is refused here */
  calls.length = 0;
  await ctx.submit();
  check("a titleless issue never leaves the page",
        [calls.length, ctx.state().error], [0, "an issue needs a title"]);

  /* 2. what it posts is what was typed */
  const fields = find(ctx.block(), "beads-new-input");
  fields[0].value = "  the board is slow  ";
  fields[0].handlers.input[0]();
  fields[1].value = "## 목표\nmake it fast";
  fields[1].handlers.input[0]();
  fields[2].value = "1";
  fields[2].handlers.change[0]();
  fields[4].value = "ui, perf";
  fields[4].handlers.input[0]();
  fields[5].value = "s2";
  fields[5].handlers.change[0]();
  fields[6].value = "alpha";
  fields[6].handlers.change[0]();

  // Back to the create route's own answer: `answerBoards` left the stub
  // handing back the boards listing.
  answer = { ok: true, status: 201, body: { issue: "claunch-new", root: "/repo" } };
  calls.length = 0;
  await ctx.submit();
  check("the request carries the typed answers and the selected board",
        JSON.parse(calls[0].body),
        { title: "the board is slow", description: "## 목표\nmake it fast",
          priority: "1", type: "task", labels: "ui, perf", assignee: "s2",
          workspace: "alpha", status: "open", cwd: "/repo" });
  check("as a POST to the board route", [calls[0].url, calls[0].method],
        ["/api/beads", "POST"]);

  /* 4. the filed issue opens, and the draft is cleared behind it */
  check("the new issue is opened", hash[hash.length - 1], "#/beads/claunch-new");
  check("the form folds and says what it filed",
        [ctx.state().open, ctx.state().done], [false, "claunch-new"]);
  check("and the draft is empty, so the next issue starts blank",
        ctx.state().draft.title, "");

  /* a refusal is shown where it was typed */
  answer = { ok: false, status: 400, body: { error: "unknown type 'tsak'" } };
  ctx.state().draft.title = "again";
  await ctx.submit();
  check("a refusal is shown on the form rather than thrown away",
        ctx.state().error, "unknown type 'tsak'");
  check("and the draft is kept so it can be corrected",
        ctx.state().draft.title, "again");

  console.log(failures ? `beadsnew_check FAILED (${failures})` : "beadsnew_check ok");
  if (failures) process.exitCode = 1;
})().catch((err) => { console.error(err); process.exitCode = 1; });

function answerBoards() {
  answer = { ok: true, status: 200, body: {
    boards: [
      { root: "/repo", sessions: [{ name: "s1", status: "idle" },
                                  { name: "s2", status: "busy" }] },
      { root: "/other", sessions: [{ name: "s9", status: "idle" }] },
    ],
    workspaces: [{ name: "alpha", path: "F:/works/alpha" }],
  } };
}
function flush() { return new Promise((r) => setImmediate(r)); }
