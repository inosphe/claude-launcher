/* The rail's "the daemon will not type here" line, run against the real
   functions from app.js.

   Two settings put it there, and they arrive on two different polls: the
   delivery hold rides /api/sessions (one field per row), the cflow reminder
   rides /api/cflow (inside that run's timers). What has to hold:

   - a row with neither setting grows no line at all, because an
     "everything is normal" pill on twenty rows is a row of noise;
   - each flag appears on its own, and both appear together;
   - an EXITED row never shows the hold, however the record reads — a record
     with no terminal holds nothing back (DeadSession.delivery_held), and a
     pill there would name a hold that is not being applied;
   - the reminder pill separates whose decision it was, because the run's own
     override and the machine-wide default read identically on the row and
     are undone in two different places;
   - the line is rebuilt in place across polls and removed when the last flag
     clears, so a setting that was switched off does not leave its pill
     behind. */
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

/* ---- stub DOM ---- */
function makeEl(tag) {
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
  let text = "";
  Object.defineProperty(node, "textContent", {
    get() { return text; },
    set(v) { text = v; node.children.length = 0; },
  });
  return node;
}

const list = makeEl("ul");
function row(name) {
  const li = makeEl("li");
  li.dataset.name = name;
  list.appendChild(li);
  return li;
}

/* `sessionsCache` and `cflowCache` are app.js globals; here they are
   parameters of the wrapper, so the sliced functions and the setters below
   share one binding each. */
const ctx = {};
new Function(
  "exports", "$", "document", "sessionsCache", "cflowCache",
  [slice("el"), slice("sessCflowRun"), slice("railQuietFlags"),
   slice("applyRailQuiet")].join("\n") + `
exports.apply = applyRailQuiet;
exports.flags = railQuietFlags;
exports.setSessions = (rows) => { sessionsCache = rows; };
exports.setRuns = (runs) => { cflowCache = runs; };
`)(ctx, (id) => (id === "session-list" ? list : null),
   { createElement: makeEl }, [], []);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const quiet = (li) => li.querySelector(".rail-quiet");
const pills = (li) => {
  const line = quiet(li);
  // Each pill is [glyph, text]; the reading a person takes off the row is
  // the text, so that is what these checks compare.
  return line ? line.children.map((p) => p.children[1].textContent) : null;
};

const rows = { s1: row("s1"), s2: row("s2"), dead: row("dead") };
const live = (name, extra) => Object.assign(
  { name, status: "idle", delivery_hold: false }, extra || {}
);
const run = (scope, reminder, override) => ({
  scope, cwd: "F:/repo", status: "step", workflow: "ship", sessions: [scope],
  timers: { reminder: { enabled: reminder } },
  ...(override === undefined ? {} : { reminder: { enabled: override } }),
});

/* Nothing set: no line. This is the common row, and it must stay empty. */
ctx.setSessions([live("s1"), live("s2")]);
ctx.setRuns([run("s1", true)]);
ctx.apply();
check("a row with neither setting grows no line", quiet(rows.s1), null);
check("...and neither does one with no run at all", quiet(rows.s2), null);

/* The hold alone. */
ctx.setSessions([live("s1", { delivery_hold: true }), live("s2")]);
ctx.apply();
check("a held session says so", pills(rows.s1), ["held"]);
check("the hold names the release command in its hover",
      quiet(rows.s1).children[0].title.includes(
        "claunch delivery-hold s1 --off"), true);
check("the neighbour is untouched", quiet(rows.s2), null);

/* The reminder alone, and whose decision it was. */
ctx.setSessions([live("s1"), live("s2")]);
ctx.setRuns([run("s1", false, false), run("s2", false)]);
ctx.apply();
check("a run with its reminder off says so", pills(rows.s1), ["reminder off"]);
check("the run's own override points at the run page",
      quiet(rows.s1).children[0].title.includes("Set for this run"), true);
check("the machine-wide default says it is not this run's doing",
      quiet(rows.s2).children[0].title.includes("machine-wide"), true);

/* Both at once, in a fixed order: the hold first. It is the sharper of the
   two — it stops other people reaching a person, where the reminder only
   stops the daemon repeating itself. */
ctx.setSessions([live("s1", { delivery_hold: true }), live("s2")]);
ctx.setRuns([run("s1", false, false)]);
ctx.apply();
check("both flags share the line, hold first",
      pills(rows.s1), ["held", "reminder off"]);

/* Rebuilt in place: the line element survives a poll rather than being
   replaced, the same way the cflow badge beside it does. */
const firstLine = quiet(rows.s1);
ctx.apply();
check("re-application reuses the line element", quiet(rows.s1) === firstLine,
      true);
check("...and does not double its pills",
      pills(rows.s1), ["held", "reminder off"]);

/* Clearing the settings clears the line. A pill left behind would report a
   silence that has already been lifted — the exact failure the feature
   exists to prevent, inverted. */
ctx.setSessions([live("s1")]);
ctx.setRuns([run("s1", true)]);
ctx.apply();
check("clearing both settings removes the line", quiet(rows.s1), null);

/* An exited row: the record may still carry the flag, and the row must not
   draw it. */
ctx.setSessions([{ name: "dead", status: "exited", delivery_hold: true }]);
ctx.setRuns([]);
ctx.apply();
check("an exited row draws no hold", quiet(rows.dead), null);

/* An older daemon, publishing neither field: no crash, no line. */
ctx.setSessions([{ name: "s1", status: "idle" }]);
ctx.setRuns([{ scope: "s1", cwd: "F:/repo", status: "step", sessions: ["s1"] }]);
ctx.apply();
check("a payload without the fields draws nothing", quiet(rows.s1), null);

/* The stylesheet: grey, not amber. Amber on this rail means the run wants
   the reader's keyboard (.sess-cflow.gated); these two are settings that are
   already decided and want nothing, so borrowing that colour would grow a
   queue of work that does not exist. */
const quietCss = css.slice(css.indexOf("#session-list .rail-quiet-pill"),
                           css.indexOf("#session-list .quiet-hold") + 200);
check("the pill is not painted in the amber that means 'your move'",
      /#d29922/.test(quietCss), false);
check("the line is a full-width line-breaker like the cflow badge",
      /#session-list \.rail-quiet \{[^}]*flex-basis: 100%/.test(css), true);
check("...and carries an order above the row's toggle",
      /#session-list \.rail-quiet \{[^}]*order: 2/.test(css), true);

/* --- what is waiting to be typed in ------------------------------------- */
/* The two pills above are silences somebody chose. This one is the opposite:
   a message that was accepted and has not gone in yet, because the delivery
   waits for the harness to be ready and for any draft to be sent. That wait
   was invisible, so pressing a cflow button and seeing nothing read exactly
   like the press having been lost, and it was pressed again
   (claunch-restart-disconnect-banner-12p2). */
ctx.setSessions([{
  name: "s1", status: "busy",
  pending_deliveries: [
    { id: 1, at: "2026-09-18T06:48:10+00:00", chars: 40,
      preview: "cflow: continue per the /cflow protocol" },
    { id: 2, at: "2026-09-18T06:48:11+00:00", chars: 12, preview: "hello there" },
  ],
}]);
ctx.setRuns([]);
ctx.apply();
check("a session with messages waiting says how many", pills(rows.s1),
      ["2 waiting"]);
const waitPill = quiet(rows.s1).children[0];
check("...and not with the pause glyph the chosen silences share",
      waitPill.children[0].text !== "⏸", true);
check("the tooltip carries each message, so the reader knows what is held up",
      /cflow: continue/.test(waitPill.title)
      && /hello there/.test(waitPill.title), true);
check("...and says an accepted message is not a delivered one",
      /Nothing is lost while it waits/.test(waitPill.title), true);
check("...and that the queue does not survive the daemon",
      /daemon restart drops/.test(waitPill.title), true);

/* Delivered: the pill goes with it. A count that only grew would report a
   backlog that is not there. */
ctx.setSessions([{ name: "s1", status: "busy", pending_deliveries: [] }]);
ctx.apply();
check("an empty queue draws no pill", quiet(rows.s1), null);

/* It coexists with the settings rather than replacing them: a held session
   can have mail waiting on the hold being lifted. */
ctx.setSessions([{
  name: "s1", status: "idle", delivery_hold: true,
  pending_deliveries: [{ id: 3, at: "t", chars: 4, preview: "mail" }],
}]);
ctx.apply();
check("a held session shows both its hold and its backlog",
      pills(rows.s1), ["held", "1 waiting"]);

/* An older daemon publishing no such field draws nothing, as with the rest. */
ctx.setSessions([{ name: "s1", status: "idle" }]);
ctx.apply();
check("a payload without the field draws nothing", quiet(rows.s1), null);

/* --- the end-of-run protection ------------------------------------------ */
/* The one flag on this line that is not a silence. `cflow kill-on-end` ends
   the session driving a finished one-shot run; keep-alive says "record the
   ending, skip the termination". Nothing else on the row moves when it is
   set, so a protected session and an unprotected one looked identical until
   the run ended and one of them disappeared. */

/* Alone, and on a row with no other setting. */
ctx.setSessions([live("s1", { keep_alive: true }), live("s2")]);
ctx.setRuns([]);
ctx.apply();
check("a protected session says so", pills(rows.s1), ["keep-alive"]);
check("the neighbour is untouched", quiet(rows.s2), null);
/* Read through `pills`' own path rather than off a child that may not be
   there: the first check above is the one that should report a missing pill,
   and a later line dereferencing null would replace its message with a
   stack trace from the harness itself. */
const kaTitle = (quiet(rows.s1) && quiet(rows.s1).children[0]
                 ? quiet(rows.s1).children[0].title : "");
check("the tooltip names the command that clears it",
      kaTitle.includes("claunch keep-alive s1 off"), true);
check("...and says the flag outlives a restart",
      /persists across daemon restarts/.test(kaTitle), true);

/* Off is the default and draws nothing: an "off" pill on twenty rows is the
   noise the whole line exists to avoid. */
ctx.setSessions([live("s1", { keep_alive: false })]);
ctx.apply();
check("an unprotected session draws no pill", quiet(rows.s1), null);

/* An exited record may still carry the flag — the definition keeps it — and
   the row must not draw it: there is no terminal left to protect. */
ctx.setSessions([{ name: "dead", status: "exited", keep_alive: true }]);
ctx.apply();
check("an exited row draws no keep-alive", quiet(rows.dead), null);

/* It shares the line with the settings rather than replacing them, and sorts
   after the two that stop typing: those are about this terminal's input, this
   one is about how long it lives. */
ctx.setSessions([live("s1", { delivery_hold: true, keep_alive: true })]);
ctx.setRuns([run("s1", false, false)]);
ctx.apply();
check("keep-alive shares the line with the hold and the reminder",
      pills(rows.s1), ["held", "keep-alive", "reminder off"]);

/* And it is the flag that survives the others being cleared. */
ctx.setSessions([live("s1", { keep_alive: true })]);
ctx.setRuns([run("s1", true)]);
ctx.apply();
check("clearing the silences leaves keep-alive standing",
      pills(rows.s1), ["keep-alive"]);

/* The stylesheet: grey like the decided flags, and not the amber that means
   "your move", nor the blue that means something is in motion. */
check("the keep-alive pill has a rule of its own",
      /#session-list \.quiet-keepalive \{/.test(css), true);
const kaRule = css.slice(css.indexOf("#session-list .quiet-keepalive {"));
const kaDecl = kaRule.slice(0, kaRule.indexOf("}"));
check("...painted grey and not amber",
      /#d29922/.test(kaRule.slice(0, 200)), false);
check("...and not the blue the in-motion pill takes",
      /#58a6ff/.test(kaDecl), false);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("railquiet_check: ok");
