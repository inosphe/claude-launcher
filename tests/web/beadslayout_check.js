/* The Beads page's three tabs share one layout (claunch-28xjt).

   Board, Queues and Reports each used to stack their parts in their own
   order: Board put its filters above the workspace row, Queues had two intro
   paragraphs and no gap before its "loading…", Reports pinned its order flip
   to the far edge of the page. And opening Queues directly showed "loading…"
   for fifteen seconds, because only the poll ever read its endpoint.

   What is held here:
     1. every tab draws head, page tabs, ONE intro paragraph, then (Board and
        Queues) the workspace row, then the filters, in that order;
     2. no workspace row is drawn while there is no board to choose;
     3. opening Queues reads /api/beads/queues at once, not on the poll;
     4. the CSS that makes the rows read alike: link tabs carry no underline,
        groups nested in a filter row do not add its top margin again, the
        Sort label is the chips' size.

   Slice the real functions out of app.js and drive them against a stub DOM. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

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
    tag, children: [], text: "", classes: new Set(), handlers: {}, id: "",
    href: "", type: "", title: "", scrollTop: 0,
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    set innerHTML(v) { if (!v) this.children = []; },
    get innerHTML() { return ""; },
    get className() { return [...this.classes].join(" "); },
    set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    querySelector() { return null; },
    all() {
      const out = [this];
      for (const k of this.children) out.push(...k.all());
      return out;
    },
    find(cls) { return this.all().filter((x) => x.classes.has(cls)); },
  };
  return n;
}
const document = { createElement: (t) => node(t) };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.text = String(text);
  return n;
}

/* The parts each tab draws below its intro are other functions with their
   own checks; here they only need to be there, recognisable by class. */
const stubs = `
const view = el("div");
function $(id) { return id === "beads-view" ? view : null; }
function formInUse() { return false; }
let beadsLayout = "board";
let beadsRenderedPage = 0, beadsRenderedFocus = "", beadsPage = 0, beadsSection = "board";
let beadsError = "", beadsCache = null, beadsSession = "", beadsWorkspace = "", beadsLoadedWorkspace = "";
let beadsFocus = "", beadsSearch = { q: "" }, beadsQueues = null, beadsQueuesError = "";
let reportsError = "", reportsCache = null;
const BEADS_Q_CELL_CAP = 24;
const BEADS_STATUSES = ["open", "in_ready", "in_progress", "in_review", "blocked", "closed"];
const BEADS_ACTIVE = new Set(["open", "in_ready", "in_progress", "in_review", "blocked"]);
function beadsFilterBar() { return el("div", "seq-tabs beads-filters"); }
function beadsNewBlock() { return el("div", "beads-new"); }
function beadsDetailPane() { return el("div", "beads-detail"); }
function beadsBoardSection() { return el("div", "beads-board"); }
function beadsQueuesBoard() { return el("div", "beads-board beads-queues-board"); }
function beadsPager() { return el("div", "beads-pager"); }
function reportsFilterBar() { return el("div", "seq-tabs reports-filters"); }
function reportsShown(rows) { return rows; }
function reportsRow() { return el("div", "reports-row"); }
function requestAnimationFrame() {}
// openBeads' collaborators, recording what was asked of them.
const asked = [];
let beadsDetail = null, beadsOpen = false, beadsTimer = null;
function showView() {}
function clearInterval() {}
function stopReportsPoll() {}
function openReports() { asked.push("openReports"); }
function restartBeadsStream() { asked.push("restartBeadsStream"); }
async function refreshBeadsDetail() { asked.push("refreshBeadsDetail"); }
function refreshBeadsRelated() {}
function refreshQueues() { asked.push("refreshQueues"); }
function refreshBeads() {}
function setInterval() { return 1; }
function go() { openBeads("", "board"); }
function sessBeadsBoardLine() { return null; }
function clearBeadsSearch() { beadsSearch = { q: "" }; }
`;

const ctx = {};
new Function(
  "exports", "document", "el",
  stubs
  + slice("selectionInUse") + slice("renderBeads") + slice("beadsPageTabs") + slice("beadsWorkspaceTabs")
  // The workspace row names each board and titles it with its database path
  // (claunch-4aesv); real helpers, so the row reads as the page draws it.
  + slice("beadsBoardLabel") + slice("beadsBoardWhere")
  + slice("renderQueues") + slice("renderReports") + slice("openBeads")
  + slice("stopBeadsPoll")
  + slice("sessBeads") + slice("sessBeadsPanel")
  + `
Object.assign(exports, {
  view, render: renderBeads, open: openBeads, asked,
  panels: [sessBeads, sessBeadsPanel],
  filters: () => [beadsWorkspace, beadsSession],
  set(k, v) { eval(k + " = v"); },
});`)(ctx, document, el);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* The page's top-level rows, named by the class that says what each is. */
const ROLES = ["wf-head", "beads-page-tabs", "beads-intro", "beads-workspace-tabs",
               "beads-filters", "reports-filters"];
function layout() {
  return ctx.view.children
    .map((n) => ROLES.find((r) => n.classes.has(r)))
    .filter(Boolean);
}
const BOARDS = [{ root: "/repo", sessions: [] }, { root: "/other", sessions: [] }];

/* ---- 1. one order on every tab ---------------------------------------- */
ctx.set("beadsSection", "board");
ctx.set("beadsCache", { boards: BOARDS });
ctx.render();
check("Board: intro, then the workspace row, then the filters",
      layout(), ["wf-head", "beads-page-tabs", "beads-intro", "beads-workspace-tabs", "beads-filters"]);

ctx.set("beadsSection", "queues");
ctx.set("beadsQueues", { statuses: [], boards: BOARDS });
ctx.render();
check("Queues: the same order, with no filter row of its own",
      layout(), ["wf-head", "beads-page-tabs", "beads-intro", "beads-workspace-tabs"]);
check("Queues says its piece in one intro paragraph, as the other tabs do",
      ctx.view.find("beads-intro").length, 1);

ctx.set("beadsSection", "reports");
ctx.set("reportsCache", [{ session: "s1" }]);
ctx.render();
check("Reports: intro, then its filters (it is machine-wide: no workspace row)",
      layout(), ["wf-head", "beads-page-tabs", "beads-intro", "reports-filters"]);

/* ---- 2. no empty workspace row ---------------------------------------- */
ctx.set("beadsSection", "board");
ctx.set("beadsCache", null);
ctx.render();
check("Board before its first page: no workspace row yet, the filters, then loading",
      [layout(), ctx.view.children[ctx.view.children.length - 1].text],
      [["wf-head", "beads-page-tabs", "beads-intro", "beads-filters"], "loading…"]);
ctx.set("beadsSection", "queues");
ctx.set("beadsQueues", { statuses: [], boards: [] });
ctx.render();
check("Queues with no board draws no empty workspace row",
      ctx.view.find("beads-workspace-tabs").length, 0);

/* ---- 3. Queues is read when it is opened ------------------------------ */
ctx.asked.length = 0;
ctx.open("", "queues");
check("opening Queues reads its endpoint at once, not fifteen seconds later",
      ctx.asked.includes("refreshQueues"), true);
ctx.asked.length = 0;
ctx.open("", "board");
check("opening Board does not read the queues",
      ctx.asked.includes("refreshQueues"), false);
ctx.set("beadsCache", { boards: [{ root: "/a" }] });
ctx.set("beadsLoadedWorkspace", "/a");
ctx.set("beadsWorkspace", "/a");
ctx.asked.length = 0;
ctx.open("issue-on-page-3", "board");
check("opening an issue preserves the board's current pages",
      ctx.asked.includes("restartBeadsStream"), false);
check("opening an issue refreshes its detail",
      ctx.asked.includes("refreshBeadsDetail"), true);
ctx.asked.length = 0;
ctx.set("beadsWorkspace", "/b");
ctx.open("issue-from-search", "board");
check("a search link into a different workspace reloads that board",
      ctx.asked.includes("restartBeadsStream"), true);

for (const panel of ctx.panels) {
 for (const priorWorkspace of ["/a", "/b"]) {
  ctx.set("beadsCache", { boards: [{ root: "/a" }, { root: "/b" }] });
  ctx.set("beadsWorkspace", priorWorkspace);
  ctx.set("beadsLoadedWorkspace", priorWorkspace);
  ctx.set("beadsOpen", true);
  ctx.set("beadsSession", "s-a");
  ctx.asked.length = 0;
  const box = panel({ session: { name: "s-b", status: "exited" },
    beads: { root: "/b", issues: [] } });
  const button = box.all().find(n => n.text === "Open board");
  button.handlers.click[0]();
  check(panel.name + " selects the session's board and assignee",
        ctx.filters(), ["/b", "s-b"]);
  check(panel.name + " reloads after session navigation from " + priorWorkspace,
        ctx.asked.includes("restartBeadsStream"), true);
  const tab = ctx.view.find("beads-workspace-tabs")[0].children[0];
  tab.handlers.click[0]();
  check(panel.name + " still lets a workspace tab clear the session filter",
        ctx.filters(), ["/a", ""]);
 }
}

/* ---- 4. the rules that make the rows read alike ----------------------- */
const rule = (sel) => {
  const at = css.indexOf("\n" + sel + " {");
  return at < 0 ? "" : css.slice(at, css.indexOf("}", at));
};
check("a tab that is a link carries no underline",
      /text-decoration:\s*none/.test(rule("a.seq-tab")), true);
check("a group nested in a filter row does not add the row's top margin again",
      /margin-top:\s*0/.test(rule(".beads-filters .seq-tabs")), true);
check("the filter rows and the workspace row sit on the 12px rhythm, not .seq-tabs' 10px top",
      [/margin-top:\s*0/.test(rule(".seq-tabs.beads-filters, .seq-tabs.reports-filters")),
       /margin-top:\s*0/.test(rule(".seq-tabs.beads-workspace-tabs"))],
      [true, true]);
check("the Sort label is the chips' size",
      /font-size:\s*12px/.test(rule(".beads-sort")), true);
check("the Reports order flip is no longer pushed to the page's far edge",
      /margin-left:\s*auto/.test(rule(".reports-order")), false);

if (failures) process.exit(1);
console.log("beadslayout_check ok");
