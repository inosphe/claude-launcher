/* The rail's "the daemon will not type here" line, run against the real
   functions from app.js.

   Two settings put it there, and they arrive on two different polls: the
   delivery hold rides /api/sessions (one field per row), the step reminder
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

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("railquiet_check: ok");
