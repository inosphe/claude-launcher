/* The session rail's Commits card: what this session actually committed.

   Three answers this block has to tell apart, and the wrong pairing of any
   two of them is what the checks below are for:

     nothing yet   -- the daemon read the repository and found no stamped
                      commit. Drawn, and said, because "this round has not
                      committed anything" is exactly what a reader watching a
                      round wants to know.
     no answer     -- an older daemon serves no `commits` at all. Nothing is
                      drawn: claiming "none" for a daemon that was never asked
                      would be a fact this page does not have.
     some commits  -- rows, newest first, each carrying the short sha a reader
                      copies out and the full one in its title.

   Slice the real function out of app.js and drive it against a stub DOM. */
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
    tag, kids: [], text: "", classes: new Set(), title: "",
    appendChild(c) { this.kids.push(c); return c; },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    all() {
      const out = [this];
      for (const k of this.kids) out.push(...k.all());
      return out;
    },
    find(cls) { return this.all().filter((n) => n.classes.has(cls)); },
    words() { return this.all().map((n) => n.text).join(" "); },
  };
  return n;
}
const document = { createElement: (t) => node(t) };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const ctx = {};
new Function("exports", "document", "el",
  slice("sessCommits")
  + "Object.assign(exports, { commits: sessCommits });")(ctx, document, el);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const ROWS = [
  {
    sha: "65c145d8243ec216b77b6f3d59cf795250ff0825",
    short: "65c145d",
    committed_at: "2026-08-27T13:14:58+09:00",
    subject: "test(gate): order the blind-edit setup",
    worktree: "s189-gate-receipt-key",
  },
  {
    sha: "d5c31cda5da7297e774156f0da0fce5fee1697a2",
    short: "d5c31cd",
    committed_at: "2026-08-27T13:06:11+09:00",
    subject: "fix(gate): the tree key must survive a stat cache",
  },
];

/* ---- some commits ------------------------------------------------------ */
let box = ctx.commits({ commits: { commits: ROWS, count: 2, latest: "65c145d",
                                   worktrees: ["s189-gate-receipt-key"] } });
check("a row per commit", box.find("sess-commit").length, 2);
check("the count comes from the daemon, not from the rows drawn",
      box.all().some((n) => n.text === "Commits (2)"), true);
check("the short sha is what the row leads with",
      box.find("sess-commit-sha").map((n) => n.text), ["65c145d", "d5c31cd"]);
check("in the order the daemon sent them -- newest first, not re-sorted here",
      box.find("sess-commit-subject")[0].text,
      "test(gate): order the blind-edit setup");
/* The full sha is the row's title rather than its text: 40 characters would
   push the subject out of a rail that can be dragged to 220px, and the reader
   who wants it is the one who hovers. */
check("the full sha is on the row, not in it",
      box.find("sess-commit")[0].title, ROWS[0].sha);
check("the timestamp loses its T and its seconds-and-zone tail",
      box.find("sess-commit-bits")[0].text.startsWith("2026-08-27 13:14:58"), true);
check("a worktree is named when the commit was stamped with one",
      box.find("sess-commit-bits")[0].text.includes("s189-gate-receipt-key"), true);
check("and nothing stands in for one that was not",
      box.find("sess-commit-bits")[1].text, "2026-08-27 13:06:11");

/* ---- nothing committed yet --------------------------------------------- */
box = ctx.commits({ commits: { commits: [], count: 0, latest: null, worktrees: [] } });
check("an empty round still draws its card", box.classes.has("sess-commits"), true);
check("and says so in words rather than showing an empty card",
      box.words().includes("no stamped commit"), true);
check("with no rows", box.find("sess-commit").length, 0);

/* ---- a daemon that was never asked ------------------------------------- */
box = ctx.commits({});
check("an older daemon draws nothing at all", box.kids.length, 0);
check("-- in particular it does not claim the session committed nothing",
      box.words().includes("no stamped commit"), false);

if (failures) process.exit(1);
console.log("sesscommits_check ok");
