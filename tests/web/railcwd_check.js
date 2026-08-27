/* Where a session RUNS, on its rail row — the directory line under the name,
   run against the real functions from app.js and the real refreshSessions
   against a stub DOM.

   The fact was always in the poll: /api/sessions carries each session's
   `cwd` (it is the SessionDef's own field), and the detail panel has said it
   for a while. What was missing was the rail: twenty rows from one
   repository, most of them agents in their own worktree, and no way to tell
   from the list which checkout any of them was in without opening each. The
   checks here pin the three things that make the line worth its height:

   - The worktree form. This launcher puts agents in
     `<repo>/.claude/worktrees/<name>`, so the ordinary tail-of-path
     shortening would print "…/worktrees/<name>" on every such row — the one
     constant segment kept, the repository dropped. The line must say the
     repository and the checkout, and nothing in between.
   - The full path is one hover away. Folding is fine; losing is not.
   - It is actually on the row, as a full-width line of its own, and it
     lands BEFORE the context gauge — under the name, where "where" reads
     before "how full". Driven through the real refreshSessions, because a
     helper that is right and never called leaves no trace.
   - And it is under the name in the detail panel's head too, in every
     arrangement of that head — the Details list's `directory` row is the
     seventh row down and not on the Workflow tab at all.
   - The git BRANCH the checkout is on rides beside the path as a sibling
     that never shrinks: a worktree's "repository › checkout" already fills
     a 260px rail, and the one fact that tells two sessions in one worktree
     apart must survive where the path's tail gets the ellipsis — and be
     absent when there is nothing to say (no directory, no git, detached
     HEAD). */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  process.env.RAILCTX_APP_JS || path.join(
    __dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8"
);
const css = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "style.css"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  const head = src.lastIndexOf("async ", start) === start - 6 ? start - 6 : start;
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}
const capLine = src.match(/^const RAIL_MESH_TAGS = .+$/m);
if (!capLine) throw new Error("cannot locate RAIL_MESH_TAGS in app.js");

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), dataset: {}, style: {},
    title: "", type: "",
    appendChild(c) { n.kids.push(c); return c; },
    append(...cs) { cs.forEach((c) => n.appendChild(c)); },
    addEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    get textContent() { return n.text; },
    set textContent(v) { n.text = String(v); },
    get className() { return [...n.classes].join(" "); },
    set className(v) { n.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get innerHTML() { return ""; },
    set innerHTML(v) { n.kids = []; },
  };
  n.classList = {
    add: (...cs) => cs.forEach((c) => n.classes.add(c)),
    remove: (...cs) => cs.forEach((c) => n.classes.delete(c)),
    contains: (c) => n.classes.has(c),
    toggle: (c, on) => (on ? n.classes.add(c) : n.classes.delete(c)),
  };
  return n;
}
function descendants(n, out = []) {
  for (const k of n.kids) { out.push(k); descendants(k, out); }
  return out;
}
const document = { createElement: node };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const list = node("ul");
let served = { sessions: [] };
const api = async () => ({ ok: true, json: async () => served });

/* Everything refreshSessions leans on that is not this line. The context
   gauge is fixed (its wording is railctx_check's and railmodel_check's);
   the rest are the same no-ops the other rail harnesses carry. */
const stubs = `
let sessionsCache = [], currentName = null, currentPage = "home";
let attachedPid = null, linkState = "down", sessName = null;
/* The rail hold's state, which refreshSessions now consults before it tears
   the rows down (app.js railHeld). Nothing here is about a press, so the
   answer is always "no press in flight" and the rebuild happens as before —
   railhold_check is the harness that drives the held case. */
function railHeld() { return false; }
let railRedrawPending = false;
function forgetDeadSessions() {}
function refreshResumeChoices() {}
function refreshParentChoices() {}
function renderHome() {}
function syncBulkActions() {}
function syncMobileBars() {}
function applyCflowBadges() {}
function applyGotoFlash() {}
function applyBriefingCards() {}
function decorateBriefingRow(li, s) {}
function terminalOnScreen() { return false; }
function attach() {}
function setStatusBadge() {}
function ctxNoteOnRow() {}
function ctxRailLine(s) { return el("span", "rail-ctx-line unknown"); }
/* The attention line ("seen / typed / moved"), stubbed for the same reason:
   it is one more full-width breaker under the name, and what it SAYS is
   railseen_check's. */
function railSeenLine(s) { return el("span", "rail-seen"); }
function $(id) { return list; }
/* The panel head's other callers (panel_check's subject): the arrangement
   is steered from here so the line can be shown to survive every one. */
let termUp = false, narrow = false;
function terminalOnScreen() { return termUp; }
const MOBILE_MQ = { get matches() { return narrow; } };
function openSpawnModal() {}
function go() {}
function closeDetail() {}
`;

const ctx = {};
new Function(
  "exports", "document", "el", "api", "list", "meshCache",
  stubs + capLine[0] + "\n"
  + slice("byLineage") + slice("sessMeshes") + slice("railMeshTags")
  + slice("shortenPath") + slice("cwdSplit") + slice("cwdShort")
  + slice("cwdLine") + slice("railCwdLine") + slice("refreshSessions")
  + slice("sessHead")
  + `
Object.assign(exports, {
  refresh: refreshSessions,
  split: cwdSplit,
  short: cwdShort,
  line: railCwdLine,
  head: sessHead,
  arrange: (o) => { termUp = !!o.termUp; narrow = !!o.narrow; currentName = o.cur || null; },
});`)(ctx, document, el, api, list, []);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* ---- the short form ---------------------------------------------------- */
const WT = "F:\\works\\claude-launcher\\.claude\\worktrees\\s84-scroll-restore";
const WT_POSIX = "/home/me/src/claude-launcher/.claude/worktrees/s45-20260825-130717";
check("a worktree reads as its repository and its checkout",
      ctx.short(WT), "claude-launcher › s84-scroll-restore");
check("...on either kind of slash",
      ctx.short(WT_POSIX), "claude-launcher › s45-20260825-130717");
/* A session may sit deeper than the checkout's root (a --worktree session
   started in a subdirectory); the part under the worktree still belongs to
   the worktree, not to the repository. */
check("a directory inside a worktree keeps its tail",
      ctx.short(WT + "\\src\\web"), "claude-launcher › s84-scroll-restore/src/web");
/* The checkout the daemon itself stands in is not a worktree, and must not
   be dressed as one — the ordinary tail form is the honest one there. */
check("the main checkout keeps the ordinary tail form",
      ctx.short("F:\\works\\claude-launcher"), "…/works/claude-launcher");
check("another repository the same",
      ctx.short("/home/me/src/gds5"), "…/src/gds5");
check("a short path is itself", ctx.short("F:\\works"), "F:\\works");
/* The marker is the PAIR `.claude/worktrees`, not the word: a repository
   that happens to be called "worktrees", or has a "worktrees" folder of its
   own, is not a launcher worktree and must not have a repo invented for it. */
check("a bare 'worktrees' folder is not the marker",
      ctx.split("/srv/worktrees/thing"), null);
check("...nor is '.claude/worktrees' with nothing under it",
      ctx.split("/srv/repo/.claude/worktrees"), null);
check("...nor '.claude/worktrees' at the root with no repository above it",
      ctx.split("/.claude/worktrees/x"), null);
check("nothing in is nothing out", [ctx.short(""), ctx.short(null)], ["", ""]);

/* The line is a path child plus an optional branch sibling: the path is
   what ellipsises, the branch is what must survive the fold. These helpers
   dig each piece out of the real line. */
const pathText = (lineEl) => {
  const p = descendants(lineEl).find((k) => k.classes.has("cwd-path"));
  return p ? p.text : "";
};
const branchEl = (lineEl) =>
  descendants(lineEl).find((k) => k.classes.has("cwd-branch"));

/* ---- the line itself --------------------------------------------------- */
const wtLine = ctx.line({ name: "s84", cwd: WT, branch: "s84-scroll-restore" });
check("the worktree line says the short form", pathText(wtLine),
      "claude-launcher › s84-scroll-restore");
check("...with the branch beside it", branchEl(wtLine).text, "⎇ s84-scroll-restore");
check("...the branch naming itself on hover", branchEl(wtLine).title,
      "git branch s84-scroll-restore");
check("...is flagged as a worktree", wtLine.classes.has("worktree"), true);
check("...and carries the whole path on hover",
      wtLine.title.split("\n").pop(), WT);
check("...saying which worktree of which repository first",
      wtLine.title.split("\n")[0], "worktree s84-scroll-restore of claude-launcher");
const plainLine = ctx.line({ name: "s45", cwd: "F:\\works\\claude-launcher", branch: "master" });
check("a plain directory is not flagged", plainLine.classes.has("worktree"), false);
check("...but still has the whole path on hover",
      plainLine.title.split("\n").pop(), "F:\\works\\claude-launcher");
check("...and still carries its branch",
      [pathText(plainLine), branchEl(plainLine).text],
      ["…/works/claude-launcher", "⎇ master"]);
/* No directory was given: the session runs in the daemon's. Said in the
   words the create form uses for the same choice, and set apart by class so
   the stylesheet can grey it — never an empty line that reads as a blank. */
const none = ctx.line({ name: "bare", cwd: "" });
check("no directory of its own says so, in the form's own words",
      pathText(none), "(daemon cwd)");
check("...and is set apart", none.classes.has("unknown"), true);
check("...and not called a worktree", none.classes.has("worktree"), false);
check("...with a title that explains rather than repeats",
      none.title.includes("daemon"), true);
/* A git session with nothing to report (a non-repo directory, a detached
   HEAD) must not draw a blank tag — the branch is simply absent. */
const branchless = ctx.line({ name: "s46", cwd: "F:\\works\\claude-launcher" });
check("a branch-less session draws no branch tag",
      descendants(branchless).filter((k) => k.classes.has("cwd-branch")).length, 0);

/* ---- and under the name in the detail panel's head --------------------- */
/* The same line, drawn by the real sessHead: the panel's Details list has
   a `directory` row, but it is the seventh row down and not on the Workflow
   tab at all, so the head — the one part both tabs share — says it too.
   Every arrangement of that head must carry it: the head drops its buttons
   when docked beside its own terminal, and the line must not go with them. */
const cwdInHead = (h) => descendants(h).filter((k) => k.classes.has("sess-cwd"));
const branchInHead = (h) => descendants(h).filter((k) => k.classes.has("cwd-branch"));
ctx.arrange({ termUp: true, narrow: false, cur: "s84" });
let head = ctx.head({ name: "s84", status: "busy", cwd: WT, branch: "s84-scroll-restore" });
check("docked beside its own terminal (no buttons) the head still says where",
      cwdInHead(head).map((k) => pathText(k)), ["claude-launcher › s84-scroll-restore"]);
check("...and which branch", branchInHead(head).map((k) => k.text), ["⎇ s84-scroll-restore"]);
check("...flagged as a worktree, whole path on hover",
      [cwdInHead(head)[0].classes.has("worktree"), cwdInHead(head)[0].title.split("\n").pop()],
      [true, WT]);
check("...and the name is still the head's first word", head.kids[0].text, "s84");
ctx.arrange({ termUp: true, narrow: false, cur: "other" });
head = ctx.head({ name: "s84", status: "busy", cwd: WT, branch: "s84-scroll-restore" });
check("aimed at another session (buttons kept) it says where, once",
      cwdInHead(head).map((k) => pathText(k)), ["claude-launcher › s84-scroll-restore"]);
ctx.arrange({ termUp: false, narrow: true, cur: null });
head = ctx.head({ name: "bare", status: "idle", cwd: "" });
check("on a phone, a session with no directory of its own says whose",
      cwdInHead(head).map((k) => [pathText(k), k.classes.has("unknown")]),
      [["(daemon cwd)", true]]);
check("...with no branch to invent", branchInHead(head).length, 0);
/* The stylesheet's part: a line of its own under everything the head holds
   (order past the buttons, which declare none), never wrapping — and a flex
   line inside it, so the branch keeps its width when the path gives way. */
const headRule = (css.match(/\.sess-head \.sess-cwd \{([^}]*)\}/) || [])[1] || "";
check("the head's line is full-width", /flex-basis:\s*100%/.test(headRule), true);
check("...sorted last", Number((headRule.match(/order:\s*(\d+)/) || [])[1]) > 0, true);
check("...a flex line, so the branch keeps its width", /display:\s*flex/.test(headRule), true);
const headPathRule =
  (css.match(/\.sess-head \.sess-cwd \.cwd-path \{([^}]*)\}/) || [])[1] || "";
check("...one line, ellipsised, where the path gives way",
      /white-space:\s*nowrap/.test(headPathRule) && /text-overflow:\s*ellipsis/.test(headPathRule),
      true);

/* ---- on the row, built by the real code -------------------------------- */
served = { sessions: [
  { name: "s45", status: "idle", role: "leader", profile: "nc", parent: null,
    cwd: "F:\\works\\claude-launcher", branch: "master" },
  { name: "s84", status: "busy", role: "worker", profile: "nc", parent: "s45",
    cwd: WT, branch: "s84-scroll-restore" },
  { name: "bare", status: "idle", profile: "nc", parent: null, cwd: "" },
] };

(async () => {
  await ctx.refresh();
  const rows = list.kids;
  check("every session still gets a row",
        rows.map((r) => r.dataset.name), ["s45", "s84", "bare"]);
  const row = (name) => rows.find((r) => r.dataset.name === name);
  const cwdOf = (name) =>
    descendants(row(name)).filter((k) => k.classes.has("rail-cwd"));
  const branchOf = (name) =>
    descendants(row(name)).filter((k) => k.classes.has("cwd-branch"));

  check("each row carries exactly one directory line",
        rows.map((r) => descendants(r).filter((k) => k.classes.has("rail-cwd")).length),
        [1, 1, 1]);
  check("the worktree row says repository › checkout",
        pathText(cwdOf("s84")[0]), "claude-launcher › s84-scroll-restore");
  check("the main-checkout row says the tail of its path",
        pathText(cwdOf("s45")[0]), "…/works/claude-launcher");
  check("the row with no directory says whose it uses",
        pathText(cwdOf("bare")[0]), "(daemon cwd)");
  check("the git rows carry their branches",
        [branchOf("s45").map((k) => k.text), branchOf("s84").map((k) => k.text)],
        [["⎇ master"], ["⎇ s84-scroll-restore"]]);
  check("...the branch lives inside the directory line, not beside it",
        cwdOf("s84")[0].kids.map((k) => k.className), ["cwd-path", "cwd-branch"]);
  check("the row with no directory has no branch",
        branchOf("bare").length, 0);

  /* A direct child of the row, because on this rail the row's own children
     are its lines (raillayout_check pins that), and before the context
     gauge: "where" is read under the name, "how full" under that. */
  const kids = row("s84").kids.map((k) => k.className);
  check("the directory line is the row's own child, right after the name line",
        kids.slice(0, 4), ["dot busy", "rail-head", "meta", "rail-cwd worktree"]);
  check("...ahead of the context gauge",
        kids.indexOf("rail-cwd worktree") < kids.indexOf("rail-ctx-line unknown"),
        true);

  /* ---- and the stylesheet lets it be a line ---------------------------- */
  /* Full-width so it breaks the row (a loose span would squeeze the name),
     one line only (twenty rows, a wrapping path doubles each), the path
     ellipsised rather than clipped while the branch keeps its width, and
     sorted after the ▸ toggle like every other breaker (raillayout_check
     pins the budget; this pins the entry). */
  const rule = (css.match(/#session-list \.rail-cwd \{([^}]*)\}/) || [])[1] || "";
  check("the line is full-width", /flex-basis:\s*100%/.test(rule), true);
  check("...a flex line, so the branch survives the fold", /display:\s*flex/.test(rule), true);
  const pathRule = (css.match(/#session-list \.rail-cwd \.cwd-path \{([^}]*)\}/) || [])[1] || "";
  check("the path never wraps", /white-space:\s*nowrap/.test(pathRule), true);
  check("...and ellipsises", /text-overflow:\s*ellipsis/.test(pathRule), true);
  check("...leaving room for the branch", /min-width:\s*0/.test(pathRule), true);
  const branchRule =
    (css.match(/#session-list \.rail-cwd \.cwd-branch \{([^}]*)\}/) || [])[1] || "";
  check("the branch never shrinks to nothing", /flex:\s*none/.test(branchRule), true);
  const order = Number((rule.match(/order:\s*(\d+)/) || [])[1]);
  const toggle = Number(((css.match(/#session-list \.sess-brief-toggle \{([^}]*)\}/) || [])[1]
                         .match(/order:\s*(\d+)/) || [])[1]);
  check("...and sorts after the ▸ toggle so the toggle keeps the name line",
        Number.isFinite(order) && Number.isFinite(toggle) && order > toggle, true);
  check("the daemon-cwd form has its own, dimmer rule",
        /#session-list \.rail-cwd\.unknown \{/.test(css), true);

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("railcwd_check: all checks passed");
})();
