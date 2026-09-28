/* The rail's Beads panel: this session's slice of the board, as lanes.

   The radio under the session head used to have two panels, Details and
   Workflow, and the board's half lived inside Details as `sessBeads` -- a
   list of rows at the bottom of a column that already carried the metadata,
   the opening task, the briefing, the send box, the queued strip, the
   meshes and the commits. A list has nowhere to put a status except inside
   each row, so "where is all of this up to" was a question the reader
   answered by tallying rows themselves.

   This panel is the run's peer instead: the same issues, in a lane per
   status, drawn with the Board tab's own card, given the whole column.

   Six things must hold, and they are what this file checks:

   1. the panel heads with the issue count and says what a card in it IS --
      an issue that NAMES this session, which is a wider net than its queue;
   2. lanes are the five active statuses always, and `closed` only when
      something here is closed -- a dead column on every session would cost
      the live ones a sixth of a narrow column for nothing;
   3. an issue lands in the lane of its status, drawn as the Board tab's
      card, and an empty lane is drawn rather than skipped;
   4. the issue the session was opened FOR is marked, and the others are
      not -- every card here merely names the session, and that difference
      is most of why the panel gets opened;
   5. a board that could not be read says so and draws no lanes; a session
      with no issue at all says that, and is still offered the create form;
   6. the create form is offered where `sessBeads` offers it -- every live
      session, told whether it already has its issue (then the new one is
      queued, claunch-4g76d) -- and never to an ended session.

   Slice the real functions out of app.js and drive them against a stub
   DOM. */
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
    tag, children: [], text: "", classes: new Set(), handlers: {}, dataset: {},
    style: {}, title: "", href: "", type: "",
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    fire(k, ev) { for (const fn of this.handlers[k] || []) fn(ev || {}); },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get classList() {
      const self = this;
      return {
        add: (...cs) => cs.forEach((c) => self.classes.add(c)),
        remove: (...cs) => cs.forEach((c) => self.classes.delete(c)),
        contains: (c) => self.classes.has(c),
      };
    },
    all() {
      const out = [this];
      for (const k of this.children) out.push(...k.all());
      return out;
    },
    find(cls) { return this.all().filter((n) => n.classes.has(cls)); },
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

/* `sessBeadsCreate` and `go` belong to other files' checks; here they only
   have to be tellable-apart, so the panel can be asked whether it offered
   the form and where its button goes. */
const stubs = `
let beadsFocus = "";
let beadsSession = "";
let beadsWorkspace = "";
const went = [];
function go(h) { went.push(h); }
function stopBeadsPoll() {}
const created = [];
function sessBeadsCreate(name, hasIssue) { created.push([name, hasIssue]); return el("div", "sess-beads-create"); }
`;

const ctx = {};
new Function(
  "exports", "document", "el",
  stubs
  + "const BEADS_STATUSES = " + JSON.stringify(["open", "in_ready", "in_progress", "in_review", "blocked", "closed"]) + ";\n"
  + "const BEADS_ACTIVE = new Set([\"open\", \"in_ready\", \"in_progress\", \"in_review\", \"blocked\"]);\n"
  + slice("beadsPriBadge") + slice("beadsCard")
  + slice("beadsBoardWhere") + slice("sessBeadsBoardLine")
  + slice("sessBeadsPanel") + slice("sessBeadsLane")
  + `
Object.assign(exports, {
  panel: sessBeadsPanel, lane: sessBeadsLane, created, went,
  session: () => beadsSession,
});`)(ctx, document, el);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const issue = (id, status, o = {}) => ({
  id, title: o.title || id, status, priority: o.priority ?? 2,
  assignee: o.assignee, via: o.via || ["assignee"],
});

/* ---- 1 & 2. the head, and which lanes get drawn ----------------------- */
const DATA = {
  session: { name: "s9", status: "running" },
  beads: {
    issue: "b2",
    issues: [
      issue("b1", "open"),
      issue("b2", "in_progress", { title: "the round's own" }),
      issue("b3", "in_progress"),
      issue("b4", "in_review"),
    ],
    reports: [],
  },
};
let box = ctx.panel(DATA);
check("the head counts every issue that names the session",
      box.find("sess-beads-kanban").length && box.children[0].text,
      "Beads (4)");
check("and the panel says what a card in it is, which is wider than a queue",
      /names this session/.test(box.children[1].text), true);
const laneNames = (b) => b.find("sess-beads-lane-name").map((n) => n.text);
check("the five active statuses are lanes, and closed is not one here",
      laneNames(box), ["open", "in_ready", "in_progress", "in_review", "blocked"]);
check("the groups stack down the column instead of sharing a grid row " +
      "(side by side, each card got a fifth of the rail and wrapped every word)",
      box.find("sess-beads-lanes")[0].style.gridTemplateColumns || "", "");
{
  const css = fs.readFileSync(
    path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
              "style.css"), "utf8");
  const rule = (css.match(/\n\.sess-beads-lanes \{([^}]*)\}/) || [])[1] || "";
  check("...and the stylesheet lays them out as a column, not a grid",
        [/flex-direction:\s*column/.test(rule), /display:\s*grid/.test(rule)],
        [true, false]);
}

const WITHCLOSED = {
  session: { name: "s9", status: "running" },
  beads: { issue: "b2", issues: [...DATA.beads.issues, issue("b0", "closed")] },
};
check("closed becomes a lane only when something here is closed",
      laneNames(ctx.panel(WITHCLOSED)),
      ["open", "in_ready", "in_progress", "in_review", "blocked", "closed"]);

/* ---- 3. cards land in the lane of their status ------------------------ */
const inLane = (b) => b.find("sess-beads-lane").map(
  (l) => l.find("beads-id").map((n) => n.text));
check("an issue is drawn in the lane of its status",
      inLane(box), [["b1"], [], ["b2", "b3"], ["b4"], []]);
check("the lane heads count what is in them",
      box.find("sess-beads-lane-n").map((n) => n.text), ["1", "0", "2", "1", "0"]);
check("an empty lane is drawn, not skipped — 'nothing has landed yet' is "
      + "an answer, and a lane that appeared only once full would shift the "
      + "panel's shape under the reader",
      box.find("sess-beads-lane").map((l) => l.find("beads-lane-empty").length),
      [0, 1, 0, 0, 1]);
const card = box.find("beads-card")[0];
check("the card is the Board tab's own, linking to the issue",
      [card.find("beads-id")[0].text, card.find("beads-id")[0].href,
       card.find("beads-card-title")[0].text],
      ["b1", "#/beads/b1", "b1"]);

/* ---- 4. the issue the round is FOR is marked -------------------------- */
const marked = box.find("beads-card").filter((c) => c.classes.has("primary"));
check("exactly the recorded issue is marked primary",
      [marked.length, marked[0].find("beads-id")[0].text,
       marked[0].find("sess-beads-primary")[0].text],
      [1, "b2", "primary"]);
check("and its lane-mate, which merely names the session, is not",
      box.find("sess-beads-lane")[2].find("beads-card")[1].classes.has("primary"),
      false);

/* ---- 5. what the panel says when there is nothing to draw ------------- */
const BROKEN = {
  session: { name: "s9", status: "running" },
  beads: { error: "no board: this directory is not in a repository with a .beads/" },
};
const broke = ctx.panel(BROKEN);
check("a board that could not be read says so and draws no lanes",
      [broke.find("sess-beads-lane").length,
       broke.children[broke.children.length - 1].text],
      [0, "no board: this directory is not in a repository with a .beads/"]);
check("...and offers nothing to press, since there is no board to write to",
      [broke.find("sess-beads-create").length, broke.find("wf-btn").length], [0, 0]);

const EMPTY = { session: { name: "s9", status: "running" }, beads: { issues: [] } };
const empty = ctx.panel(EMPTY);
check("a session no issue names says that, and draws no lanes",
      [empty.find("sess-beads-lane").length,
       empty.find("wf-note")[1].text],
      [0, "no issue on the board names this session"]);

/* ---- 6. the create form, and the way out ------------------------------ */
ctx.created.length = 0;
ctx.panel(EMPTY);
check("a live session with no issue is offered the form", ctx.created, [["s9", false]]);
ctx.created.length = 0;
ctx.panel(DATA);
check("a session that already has its issue is offered it too, to queue",
      ctx.created, [["s9", true]]);
ctx.created.length = 0;
ctx.panel({ session: { name: "s9", status: "exited" }, beads: { issues: [] } });
check("and an ended session is never asked to open one", ctx.created, []);

const out = box.find("wf-btn")[box.find("wf-btn").length - 1];
check("the way out is the board, filtered to this session", out.text, "Open board");
out.fire("click");
check("...which is what the press does",
      [ctx.session(), ctx.went], ["s9", ["#/beads"]]);

/* ---- the winding-down warning is not lost with the move --------------- */
const WIND = {
  session: { name: "s9", status: "running" },
  beads: {
    issue: null, issues: [issue("b1", "in_progress")],
    winddown: { since: "2026-09-02T07:00:00+00:00", issues: ["b1"], grace: 90 },
  },
};
check("a session being wound down still says so here",
      ctx.panel(WIND).find("wf-warning").map((n) => /winding down since 2026-09-02 07:00:00/.test(n.text)),
      [true]);

if (failures) process.exit(1);
console.log("railbeadskanban_check ok");
