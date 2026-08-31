/* The rail's cflow badge, run against the real functions from app.js.

   Each session row grows one line saying where that session's workflow run
   stands. What has to hold: the line appears only for a run scoped to that
   session; a run stopped on a HUMAN (gate approval, branch choice) is
   flagged as the reader's move, while one delegated to another agent is
   not; after a migrate-session leaves a stale run under the old cwd, the
   run whose canonical cwd still holds the live session wins; the badge
   element survives re-application, because its click listener (the walk to
   the run page) is attached once for its lifetime; and the mark the line
   opens with carries NO colour of its own — it is a glyph, one per state,
   inheriting the line's colour. That last one is the reported bug: it used
   to be a coloured dot, the palettes are one palette (a done run and an idle
   session are both #3fb950), and a reader scanning the rail took the second
   green circle for another session. It is held on both sides here — the
   glyphs, which must stay distinct from each other, and the stylesheet,
   which must not paint them. */
const fs = require("fs");
const path = require("path");
const staticDir = path.join(__dirname, "..", "..", "src", "claude_launcher",
                            "web", "static");
const src = fs.readFileSync(path.join(staticDir, "app.js"), "utf8");
const css = fs.readFileSync(path.join(staticDir, "style.css"), "utf8");

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

/* WF_GLYPH is a const table, not a function, so it needs its own cut. */
function wfGlyphTable() {
  const start = src.indexOf("const WF_GLYPH = {");
  if (start < 0) throw new Error("cannot locate WF_GLYPH in app.js");
  return src.slice(start, src.indexOf("};", start) + 2);
}

/* ---- stub DOM ---- */
function el(tag) {
  const node = {
    tag, className: "", title: "", dataset: {}, children: [],
    listeners: {}, parent: null,
    appendChild(c) { this.children.push(c); c.parent = this; return c; },
    append(...cs) { for (const c of cs) this.appendChild(c); },
    remove() {
      if (this.parent) {
        this.parent.children.splice(this.parent.children.indexOf(this), 1);
      }
    },
    addEventListener(t, fn) { this.listeners[t] = fn; },
    querySelector(sel) {
      return this.children.find(
        (c) => c.className.split(" ").includes(sel.slice(1))
      ) || null;
    },
    querySelectorAll(sel) {
      if (sel === "li[data-name]") {
        return this.children.filter((c) => c.tag === "li" && c.dataset.name);
      }
      throw new Error(`unexpected selector ${sel}`);
    },
  };
  // Assigning textContent clears the children, like the real DOM — the
  // badge is rebuilt through exactly that assignment on every poll.
  let text = "";
  Object.defineProperty(node, "textContent", {
    get() { return text; },
    set(v) { text = v; node.children.length = 0; },
  });
  return node;
}

const list = el("ul");
function row(name) {
  const li = el("li");
  li.dataset.name = name;
  list.appendChild(li);
  return li;
}

const location = { hash: "" };
const ctx = {};
/* `cflowCache` is an app.js global; here it is a parameter of the wrapper,
   which the sliced functions and setRuns share as one binding. */
new Function(
  "exports", "$", "document", "location", "cflowCache",
  [wfGlyphTable(), slice("wfDotClass"), slice("wfMarkState"), slice("wfMark"), slice("askWho"),
   slice("answerFellToUs"), slice("answerBranchOptions"), slice("sessCflowRun"),
   slice("sessCflowGated"), slice("sessCflowLabel"), slice("fmtOpensAt"),
   slice("applyCflowBadges")].join("\n") + `
exports.apply = applyCflowBadges;
exports.setRuns = (runs) => { cflowCache = runs; };
`)(ctx, (id) => (id === "session-list" ? list : null),
   { createElement: el }, location, []);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const badge = (li) => li.querySelector(".sess-cflow");
const badgeText = (li) => {
  const b = badge(li);
  return b && b.children[1] ? b.children[1].textContent : null;
};

const rows = { s19: row("s19"), quiet: row("quiet"), old: row("old") };

/* A running step: the badge names the workflow and the step, blue dot, and
   the neighbouring session shows nothing. */
ctx.setRuns([{ scope: "s19", cwd: "F:/repo", status: "step", workflow: "ship",
               step_id: "build", title: "Build it", sessions: ["s19"] }]);
ctx.apply();
check("a running step is named on its session's row",
      badgeText(rows.s19), "ship · Build it");
check("the mark says running by its class...",
      badge(rows.s19).children[0].className, "wf-mark wf-mark-running");
check("...and by a glyph, which is all it says on its own",
      badge(rows.s19).children[0].textContent, "▸");
check("a session with no run grows no badge", badge(rows.quiet), null);
check("a running step is not flagged as the reader's move",
      badge(rows.s19).className, "sess-cflow");

/* A branch choice in front of a human: amber-flagged, and the hover text
   carries the question and its options. */
ctx.setRuns([{ scope: "s19", cwd: "F:/repo", status: "waiting_selection",
               workflow: "ship", prompt: "which lane?",
               options: ["fast", "safe"], sessions: ["s19"] }]);
ctx.apply();
check("a branch choice is flagged as the reader's move",
      badge(rows.s19).className, "sess-cflow gated");
check("the flagged line says a choice is owed",
      badgeText(rows.s19), "ship · ⚑ choose an option");
check("the hover text carries the question and the options",
      badge(rows.s19).title, "which lane? — options: fast, safe");

/* Gate approvals, in their flavours. */
ctx.setRuns([{ scope: "s19", cwd: "F:/repo", status: "waiting_approval",
               reason: "gate", gate: "ship it?", sessions: ["s19"] }]);
ctx.apply();
check("a gate is flagged and asks for approval",
      [badge(rows.s19).className, badgeText(rows.s19)],
      ["sess-cflow gated", "cflow · ⚑ approval needed"]);
ctx.setRuns([{ scope: "s19", cwd: "F:/repo", status: "waiting_approval",
               reason: "loop_limit", workflow: "ship", sessions: ["s19"] }]);
ctx.apply();
check("a loop limit says what approving does",
      badgeText(rows.s19), "ship · ⚑ loop limit — approve to continue");

/* Delegated to another agent: shown, but deliberately NOT the reader's
   move — it is with a peer, and amber would grow a queue of fake work. */
ctx.setRuns([{ scope: "s19", cwd: "F:/repo", status: "waiting_answer",
               workflow: "ship",
               ask: { asked: [{ kind: "member", handle: "reviewer" }] },
               sessions: ["s19"] }]);
ctx.apply();
check("a question with another agent is not flagged",
      badge(rows.s19).className, "sess-cflow");
check("it says who has it", badgeText(rows.s19), "ship · with reviewer");

/* After a migrate-session two runs share the scope; the one whose canonical
   cwd still holds the live session wins, and the click key follows it. */
ctx.setRuns([
  { scope: "s19", cwd: "F:/stale", status: "step", workflow: "ship",
    title: "Old", sessions: [] },
  { scope: "s19", cwd: "F:/fresh", status: "step", workflow: "ship",
    title: "New", sessions: ["s19"] },
]);
ctx.apply();
check("the run whose cwd holds the live session wins",
      badgeText(rows.s19), "ship · New");
check("the click key names that run", badge(rows.s19).dataset.wf,
      "s19|F:/fresh");
const firstBadge = badge(rows.s19);
firstBadge.listeners.click({ stopPropagation() {} });
check("clicking the badge walks to the run page",
      location.hash, "#/wf/" + encodeURIComponent("s19|F:/fresh"));

/* The badge element is reused across polls — the click listener is attached
   once, so a rebuilt-in-place badge must be the same node. */
ctx.setRuns([{ scope: "s19", cwd: "F:/fresh", status: "done",
               workflow: "ship", sessions: ["s19"] }]);
ctx.apply();
check("re-application reuses the badge element",
      badge(rows.s19) === firstBadge, true);
check("a finished run reads as done", badgeText(rows.s19), "ship · done");

/* A run that ends (archived) takes its badge with it; an idle slot with only
   a pending start never grows one. */
ctx.setRuns([{ scope: "s19", cwd: "F:/fresh", status: "idle",
               pending_start: { name: "ship" }, sessions: ["s19"] }]);
ctx.apply();
check("an idle slot shows no badge", badge(rows.s19), null);
ctx.setRuns([]);
ctx.apply();
check("no runs, no badges",
      Object.values(rows).map((li) => badge(li)), [null, null, null]);

/* ---- waiting_answer: with a peer, versus put to nobody ---------------- */
/* Same status word, opposite meaning for the person reading the rail. The
   delivered one must stay quiet (its own colour, not the gate's amber, or
   the rail grows a queue of things that are not the reader's); the stranded
   one must show up as theirs, because nobody else will ever clear it. */
const answerRow = (ask) => [{
  scope: "s19", cwd: "F:/repo", status: "waiting_answer", workflow: "ship",
  step_id: "plan", sessions: ["s19"], ask,
}];

ctx.setRuns(answerRow({ prompt: "ship?", asked: [{ handle: "lead" }] }));
ctx.apply();
check("a delivered ask names its holder", badgeText(rows.s19), "ship · with lead");
check("...and is marked as somebody else's: hollow, not filled",
      [badge(rows.s19).children[0].className,
       badge(rows.s19).children[0].textContent], ["wf-mark wf-mark-peer", "◇"]);

ctx.setRuns(answerRow({ prompt: "ship?", asked: [] }));
ctx.apply();
/* The flag is the whole point: sessCflowGated now counts this as the
   reader's move, so the rail marks it like any other gate. */
check("an ask that reached nobody says so, and is flagged as yours", badgeText(rows.s19),
      "ship · ⚑ asked of nobody — approve to continue");
check("...and is marked as the reader's: the filled twin of the same shape",
      [badge(rows.s19).children[0].className,
       badge(rows.s19).children[0].textContent], ["wf-mark wf-mark-yours", "◆"]);

ctx.setRuns(answerRow(undefined));
ctx.apply();
check("no ask at all reads the same way (a forced goto leaves this)",
      badgeText(rows.s19), "ship · ⚑ asked of nobody — approve to continue");

ctx.setRuns([{
  scope: "s19", cwd: "F:/repo", status: "waiting_answer", workflow: "ship",
  step_id: "landing-review", reason: "branch", sessions: ["s19"],
  options: [{ name: "request" }, { name: "hold" }],
  user_door: { command: "claunch cflow select <request|hold>" },
}]);
ctx.apply();
check("a branch put to nobody names the owed selection",
      badgeText(rows.s19), "ship · ⚑ asked of nobody — choose request|hold");

/* ---- a paced hold is nobody's move, not a peer's ----------------------- */
/* `waiting_window` shares wf-delegated with an ask sitting on a peer, because
   one colour could only say "not yours". The shape says the rest: a peer can
   be chased, the clock cannot, so the two must not read alike. The wording is
   shared with the diagram (s107) — one state, one word. */
ctx.setRuns([{ scope: "s19", cwd: "F:/repo", status: "waiting_window",
               workflow: "ship", option: "fast",
               opens_at: "2026-08-25T10:52:00Z", sessions: ["s19"] }]);
ctx.apply();
check("a held choice takes the pause of the play/pause pair, not the peer's ◇",
      badge(rows.s19).children[0].className, "wf-mark wf-mark-held");
check("...and says so in the word the other surfaces use",
      badgeText(rows.s19).startsWith("ship · 'fast' held → "), true);
check("...and is not flagged as the reader's move — nobody can hurry a clock",
      badge(rows.s19).className, "sess-cflow");

/* ---- one glyph per state, and no colour anywhere ----------------------- */
/* Every state has to be told from every other by shape alone now, so the
   glyphs must not collide — two states sharing one would be invisible in
   every check above, which only ever looks at one state at a time. */
const glyphs = ["step", "waiting_approval", "waiting_window", "done", "error"]
  .map((st) => {
    ctx.setRuns([{ scope: "s19", cwd: "F:/repo", status: st, workflow: "ship",
                   option: "fast", opens_at: "2026-08-25T10:52:00Z",
                   sessions: ["s19"] }]);
    ctx.apply();
    return badge(rows.s19).children[0].textContent;
  });
ctx.setRuns(answerRow({ prompt: "ship?", asked: [{ handle: "lead" }] }));
ctx.apply();
glyphs.push(badge(rows.s19).children[0].textContent);
check("every state gets its own glyph — none reused, none empty",
      [new Set(glyphs).size, glyphs.filter(Boolean).length], [6, 6]);

/* The stylesheet is the other half. The class and the glyph prove nothing on
   their own: a `background` on .wf-mark, or a surviving `.dot.wf-*` rule,
   puts the colour straight back with every check above still green. */
const markRule = (css.match(/\n\.wf-mark \{([^}]*)\}/) || [])[1] || "";
check("the mark takes the line's colour rather than one of its own",
      /color:\s*inherit/.test(markRule), true);
check("...and paints nothing itself",
      /background|#[0-9a-fA-F]{3}/.test(markRule), false);
/* Comments stripped, because this file argues about `.dot.wf-*` in prose
   right where it stopped declaring it — matching the prose would make the
   check pass forever. */
const rules = css.replace(/\/\*[\s\S]*?\*\//g, "");
check("the run's old dot colours are gone, not merely unused",
      /\.dot\.wf-/.test(rules), false);
/* The session's dot is the one thing on this rail that still speaks in
   colour, and it has to keep doing so — the fix is that it is now alone in
   it, not that everything went grey. */
check("the session's own liveness dot keeps its colour and its circle",
      /\.dot\.idle \{[^}]*#3fb950/.test(css) &&
      /#session-list \.dot \{[^}]*border-radius:\s*50%/.test(css), true);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("railbadge_check: ok");
