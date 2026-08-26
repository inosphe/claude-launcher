/* The Beads page and the rail's beads block: the board drawn against the
   fleet. Four things must hold: the status filter defaults to what is still
   to do and the session filter follows the daemon's tags (not the assignee
   column alone); rows sort by what is being worked, then priority, then
   recency; an issue row links its id to the detail route and each tagged
   session to its terminal, with the match reason in the title; and the
   rail's block says why a board is unreadable, offers the create form only
   to a live session with no linked issue, and shows a running wind-down.
   And a fifth: the issue's detail pane hands back the rounds written up for
   it, labelled by the session that wrote them -- the reader there is looking
   at a closed issue whose session is gone, so the session is the half of the
   row that is news.
   And a sixth: the pane says where the issue sits in the family -- its parent
   and its children, both taken from the board listing's edges, because `br
   show` resolves what an issue depends on but nothing on that side names the
   children, and the children are half of what a reader opens a parent to see.
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
  const head = src.lastIndexOf("async ", start) === start - 6 ? start - 6 : start;
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
  const n = {
    tag, kids: [], text: "", classes: new Set(), handlers: {}, dataset: {},
    value: "", disabled: false, placeholder: "", title: "", href: "", type: "",
    appendChild(c) { this.kids.push(c); return c; },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get classList() {
      const self = this;
      return {
        add: (...cs) => cs.forEach((c) => self.classes.add(c)),
        contains: (c) => self.classes.has(c),
        toggle: (c, on) => { on ? self.classes.add(c) : self.classes.delete(c); },
      };
    },
    all() {
      const out = [this];
      for (const k of this.kids) out.push(...k.all());
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

const stubs = `
let beadsFocus = "";
let beadsDetail = null;
function setDetail(d) { beadsDetail = d; }
function setFocus(f) { beadsFocus = f; }
let sessBeadsBox = null;
let sessionsCache = [];
let beadsCache = null;
function setBoards(b) { beadsCache = b; }
const gone = [];
function go(h) { gone.push(h); }
async function api() { return { ok: true, json: async () => ({}) }; }
function refreshSession() {}
`;

const ctx = {};
new Function(
  "exports", "document", "el",
  stubs
  + "const BEADS_STATUSES = " + JSON.stringify(["open", "in_progress", "in_review", "blocked", "closed"]) + ";\n"
  + "const BEADS_ACTIVE = new Set([\"open\", \"in_progress\", \"in_review\", \"blocked\"]);\n"
  + slice("beadsFilterIssues") + slice("beadsSortIssues")
  + slice("beadsStatusBadge") + slice("beadsIssueRow")
  + slice("sessBeads") + slice("sessBeadsCreate")
  + slice("sessReports") + slice("beadsHierarchy")
  + slice("beadsRelationBlock") + slice("beadsDetailPane")
  + `
Object.assign(exports, {
  filter: beadsFilterIssues, sort: beadsSortIssues, row: beadsIssueRow,
  rail: sessBeads, reports: sessReports, pane: beadsDetailPane,
  setDetail, setFocus, setBoards, gone,
});`)(ctx, document, el);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* ---- filters ---------------------------------------------------------- */
const issues = [
  { id: "a", status: "open", priority: 3, updated_at: "2026-01-01", sessions: [{ name: "s1", via: ["assignee"], status: "idle" }] },
  { id: "b", status: "in_progress", priority: 2, updated_at: "2026-01-02", sessions: [{ name: "s2", via: ["link"], status: "busy" }] },
  { id: "c", status: "closed", priority: 1, updated_at: "2026-01-03", sessions: [{ name: "s1", via: ["created_by"], status: "idle" }] },
  { id: "d", status: "in_progress", priority: 1, updated_at: "2026-01-04", sessions: [] },
];
check("active hides closed", ctx.filter(issues, "active", "").map((i) => i.id), ["a", "b", "d"]);
check("all shows everything", ctx.filter(issues, "all", "").map((i) => i.id), ["a", "b", "c", "d"]);
check("one status", ctx.filter(issues, "closed", "").map((i) => i.id), ["c"]);
check("session filter follows the tags, whichever link",
      ctx.filter(issues, "all", "s1").map((i) => i.id), ["a", "c"]);
check("session filter + status", ctx.filter(issues, "active", "s1").map((i) => i.id), ["a"]);

/* ---- order ------------------------------------------------------------- */
check("worked first, then priority, then recency",
      ctx.sort(issues).map((i) => i.id), ["d", "b", "a", "c"]);

/* ---- a row ------------------------------------------------------------- */
const row = ctx.row(issues[1]);
const id = row.find("beads-id")[0];
check("id links to the detail route", id.href, "#/beads/b");
check("status badge", row.find("beads-status")[0].text, "in_progress");
const tag = row.find("beads-sess")[0];
check("session tag links to the terminal", tag.href, "#/s/s2");
check("session tag carries state and reason", [tag.classes.has("busy"), tag.title],
      [true, "s2 (busy) — link"]);
const compact = ctx.row({ ...issues[1], assignee: "s2", via: ["link", "assignee"] }, { compact: true });
check("compact rows have no session column", compact.find("beads-sessions").length, 0);
check("compact rows say why they matched",
      compact.find("beads-bits")[0].text, "→ s2  via link, assignee");

/* ---- the rail block --------------------------------------------------- */
function railOf(session, beads) { return ctx.rail({ session, beads }); }

let box = railOf({ name: "s1", status: "idle" }, { error: "no board: not a repository" });
check("an unreadable board says why", box.find("wf-note")[0].text, "no board: not a repository");
check("and offers no create form", box.find("sess-beads-create").length, 0);

box = railOf({ name: "s1", status: "idle" }, { issue: null, issues: [] });
check("no issue: a note and the create form",
      [box.find("wf-note")[0].text, box.find("sess-beads-create").length],
      ["no issue on the board names this session", 1]);
check("the create form is hoisted per session",
      railOf({ name: "s1", status: "idle" }, { issues: [] }).find("sess-beads-create")[0]
        === box.find("sess-beads-create")[0], true);
check("but rebuilt for another session",
      railOf({ name: "s2", status: "idle" }, { issues: [] }).find("sess-beads-create")[0]
        !== box.find("sess-beads-create")[0], true);

box = railOf({ name: "s1", status: "idle" },
             { issue: "b", issues: [{ ...issues[1], via: ["link"] }] });
check("a linked issue: rows, no create form",
      [box.find("beads-row").length, box.find("sess-beads-create").length], [1, 0]);
check("the heading counts", box.kids[0].text, "Beads (1)");

box = railOf({ name: "s1", status: "exited" }, { issues: [] });
check("an exited session gets no create form", box.find("sess-beads-create").length, 0);

box = railOf({ name: "s1", status: "busy" },
             { issue: "b", issues: [],
               winddown: { since: "2026-08-25T18:00:00+09:00", grace: 120, issues: ["b"] } });
check("a running wind-down is said, with what was asked",
      box.find("wf-warning")[0].text.startsWith("winding down since 2026-08-25 18:00:00 — asked to settle b;"),
      true);
check("and how to cut it short", box.find("wf-warning")[0].text.includes("Kill again to stop now"), true);

/* the Open board button goes through the router, filtered to this session */
const open = box.find("wf-btn").find((b) => b.text === "Open board");
open.handlers.click[0]();
check("open board routes to the page", ctx.gone, ["#/beads"]);

/* ---- the issue detail pane: where it sits in the family ---------------- */
/* The edges are the board's, not the issue payload's -- `from` is the child,
   which is the direction `br dep add <child> <parent>` stores. */
const FAM = [
  { id: "epic", title: "the epic", status: "open", priority: 1 },
  { id: "kid", title: "the child", status: "in_progress", priority: 2 },
  { id: "other", title: "unrelated", status: "open", priority: 3 },
];
const FAM_DEPS = [{ from: "kid", to: "epic", type: "parent-child" }];
ctx.setBoards({ boards: [{ root: "/repo", issues: FAM, deps: FAM_DEPS }] });

ctx.setFocus("epic");
ctx.setDetail({ issue: { id: "epic", title: "the epic", comments: [] } });
let fam = ctx.pane().find("beads-detail-rel")[0];
check("a parent's pane lists the children the issue payload cannot name",
      fam.find("beads-rel-link").map((a) => a.text), ["kid"]);
check("the children are counted and linked",
      [fam.find("beads-rel-label")[0].text, fam.find("beads-rel-link")[0].href],
      ["children (1)", "#/beads/kid"]);

ctx.setFocus("kid");
ctx.setDetail({ issue: { id: "kid", title: "the child", comments: [] } });
fam = ctx.pane().find("beads-detail-rel")[0];
check("a child's pane names its parent, with the parent's own state",
      [fam.find("beads-rel-label")[0].text, fam.find("beads-rel-link")[0].text,
       fam.find("beads-status")[0].text], ["parent", "epic", "open"]);

/* Most issues are related to nothing. The pane must not spend a box saying so,
   and it must not break when the listing has not arrived yet -- the detail
   fetch and the listing go out together, so on the very first draw of a
   direct #/beads/<id> link there is no board to read. */
ctx.setFocus("other");
ctx.setDetail({ issue: { id: "other", title: "unrelated", comments: [] } });
check("an issue with no family gets no family box",
      ctx.pane().find("beads-detail-rel").length, 0);

ctx.setBoards(null);
ctx.setFocus("kid");
ctx.setDetail({ issue: { id: "kid", title: "the child", comments: [] } });
check("and neither does one whose board listing has not arrived yet",
      ctx.pane().find("beads-detail-rel").length, 0);

/* The family goes between the facts and the description, which keeps it clear
   of the Reports block below -- that block is the only route to a report whose
   session is gone, so nothing may be inserted on top of it. */
ctx.setBoards({ boards: [{ root: "/repo", issues: FAM, deps: FAM_DEPS }] });
// A report row of this block's own, so the ordering check does not reach
// into the Reports block below for its fixture.
ctx.setDetail({ issue: { id: "kid", title: "the child", description: "why",
                         comments: [] },
                reports: [{ file: "r.html", session: "s1", issue: "kid",
                            at: "2026-08-26T05:13:22Z", size: 10,
                            url: "/api/sessions/s1/reports/r.html" }] });
const order = ctx.pane().kids.map((k) => [...k.classes][0] || k.tag);
check("family, then description, then the rounds written up for it",
      [order.indexOf("beads-detail-rel") < order.indexOf("beads-desc"),
       order.indexOf("beads-desc") < order.indexOf("sess-reports")],
      [true, true]);

/* ---- the issue detail pane: the rounds written up for this issue -------- */
ctx.setFocus("claunch-j31");

const ROWS = [
  { file: "20260826T051322Z-claunch-j31.html", session: "s121",
    at: "2026-08-26T05:13:22Z", issue: "claunch-j31", size: 19591,
    url: "/api/sessions/s121/reports/20260826T051322Z-claunch-j31.html" },
  { file: "20260825T090000Z-claunch-j31.html", session: "s99",
    at: "2026-08-25T09:00:00Z", issue: "claunch-j31", size: 4200,
    url: "/api/sessions/s99/reports/20260825T090000Z-claunch-j31.html" },
];

ctx.setDetail({ issue: { id: "claunch-j31", title: "the round", comments: [] },
                reports: ROWS });
let pane = ctx.pane();
let links = pane.find("sess-report-link");
check("the pane lists every round written for the issue", links.length, 2);
check("labelled by the session, not by the issue the page already names",
      links.map((a) => a.text), ["s121", "s99"]);
check("each link opens the served page in its own tab",
      [links[0].href, links[0].target, links[0].rel],
      [ROWS[0].url, "_blank", "noopener"]);
check("the heading counts", pane.find("sess-reports")[0].kids[0].text, "Reports (2)");

/* A second session's report for the same issue is the case the by-session
   index could not answer at all -- both must be reachable from here. */
check("rounds from different sessions both survive to the pane",
      links.map((a) => a.text).sort(), ["s121", "s99"]);

ctx.setDetail({ issue: { id: "claunch-j31", title: "the round", comments: [] },
                reports: [] });
check("an issue nobody wrote up shows no empty Reports box",
      ctx.pane().find("sess-reports").length, 0);

ctx.setDetail({ issue: { id: "claunch-j31", title: "t", comments: [] } });
check("an older payload with no reports field does not break the pane",
      ctx.pane().find("sess-reports").length, 0);

/* The rail keeps its own label: there the session is the heading, so the
   issue is what tells two rounds apart. */
const railBox = ctx.reports(ROWS);
check("the rail labels the same rows by issue",
      railBox.find("sess-report-link").map((a) => a.text),
      ["claunch-j31", "claunch-j31"]);

if (failures) process.exit(1);
console.log("beads_check ok");
