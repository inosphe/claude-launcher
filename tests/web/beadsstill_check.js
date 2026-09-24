/* The Beads page holding still under its own poll.

   renderBeads empties `#beads-view` and rebuilds it on every tick: 15 s on
   the Board and Queues tabs, 30 s on Reports (refreshReports ends in
   renderBeads too). It already kept one scroll -- the board canvas's, for
   a poll of the same page -- and lost every other one the page has:

     the page       -- `#beads-view` is itself the scroll container
                       (style.css: overflow-y: auto). Emptying it clamps its
                       scrollTop to 0, so each tick threw the reader back to
                       the top of whichever tab they were on.
     the detail     -- `.beads-detail` scrolls on its own (overflow-y: auto,
                       capped at the viewport). A long issue read halfway
                       down snapped back to its title.
     the queues     -- `.beads-queues` scrolls sideways (overflow-x: auto);
                       a grid scrolled to the right-hand sessions came back
                       at the left edge.
     a selection    -- the same hole the session panel had (sessstill_check):
                       issue text is plain divs, so formInUse never held the
                       rebuild off while somebody was dragging across it.

   Reported as "the beads page keeps refreshing and the scroll resets"
   (claunch-t80om). renderBeads is driven here with its sections stubbed,
   the way beadslayout_check drives it; what is under test is the handful
   of lines around the wipe. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  let depth = 0;
  for (let j = src.indexOf(") {", start) + 2; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

let failures = 0;
function check(label, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g === w) return;
  failures++;
  console.error(`FAIL ${label}\n  got  ${g}\n  want ${w}`);
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag = "div") {
  const n = {
    tag, nodeType: 1, id: "", kids: [], text: "", classes: new Set(),
    listeners: {}, scrollTop: 0, scrollLeft: 0,
    appendChild(c) { n.kids.push(c); return c; },
    addEventListener(t, fn) { (n.listeners[t] ||= []).push(fn); },
    contains(x) {
      if (x === n) return true;
      return n.kids.some((k) => k.contains && k.contains(x));
    },
    // Enough of a query engine for the three selectors renderBeads asks:
    // `#id` and `.class`, depth first, excluding the root itself.
    querySelector(sel) {
      const hit = (k) => sel[0] === "#" ? k.id === sel.slice(1)
        : k.classes.has(sel.slice(1));
      const walk = (k) => {
        for (const c of k.kids) {
          if (hit(c)) return c;
          const deeper = walk(c);
          if (deeper) return deeper;
        }
        return null;
      };
      return walk(n);
    },
    get textContent() { return n.text; },
    set textContent(v) { n.text = String(v); },
    get className() { return [...n.classes].join(" "); },
    set className(v) {
      n.classes = new Set(String(v).split(/\s+/).filter(Boolean));
    },
    get innerHTML() { return ""; },
    // What the browser does to a scroll container that is emptied: its
    // content height goes to 0, so its scroll clamps to 0 with it.
    set innerHTML(v) {
      if (v === "") { n.kids.length = 0; n.scrollTop = 0; n.scrollLeft = 0; }
    },
  };
  return n;
}
const document = { createElement: (t) => node(t), activeElement: null };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
}

/* ---- the stylesheet facts the rule stands on --------------------------- */
check("the page view is a scroll container",
      /#beads-view \{[^}]*\}/.test(css) &&
      /#home-view, [^{]*#beads-view \{[^}]*overflow-y: auto/.test(css), true);
check("the detail pane scrolls on its own",
      /\.beads-detail \{[^}]*overflow-y: auto/.test(css), true);
check("the queues grid scrolls sideways",
      /\.beads-queues \{[^}]*overflow-x: auto/.test(css), true);

/* ---- renderBeads, with every section it draws stubbed away ------------- */
const view = node("div");
let selection = null;
const stubs = `
function $(id) { return id === "beads-view" ? view : null; }
function formInUse() { return false; }
let beadsRenderedPage = 0, beadsRenderedFocus = "", beadsPage = 0;
let beadsSection = "board", beadsError = "", beadsSession = "";
let beadsWorkspace = "/repo", beadsFocus = "", beadsSearch = { q: "" };
let beadsCache = { boards: [{ root: "/repo", sessions: [] }] };
function beadsPageTabs() { return el("div", "beads-page-tabs"); }
function beadsWorkspaceTabs() { return el("div", "beads-workspace-tabs"); }
function beadsFilterBar() { return el("div", "beads-filters"); }
function beadsNewBlock() { return el("div", "beads-new"); }
function beadsBoardSection() { return el("div", "beads-board"); }
function beadsPager() { return el("div", "beads-pager"); }
function beadsSearchSection() { return el("div", "beads-search"); }
function beadsDetailPane() { return el("div", "beads-detail"); }
function renderReports(v) { v.appendChild(el("div", "reports-table")); }
function renderQueues(v) {
  const board = el("div", "beads-board");
  board.appendChild(el("div", "beads-queues"));
  v.appendChild(board);
}
function requestAnimationFrame(fn) { fn(); }
`;
const ctx = {};
new Function(
  "exports", "document", "el", "view", "window",
  stubs + slice("selectionInUse") + slice("renderBeads") + `
Object.assign(exports, { render: renderBeads, set(k, v) { eval(k + " = v"); } });`
)(ctx, document, el, view, { getSelection: () => selection });

/* ---- the page scroll, on each of the three tabs ------------------------ */
ctx.render();
view.scrollTop = 800;
ctx.render();
check("Board: a poll leaves the page where the reader scrolled it",
      view.scrollTop, 800);

ctx.set("beadsSection", "queues");
view.scrollTop = 450;
ctx.render();
check("Queues: the same, through that tab's early return",
      view.scrollTop, 450);

ctx.set("beadsSection", "reports");
view.scrollTop = 220;
ctx.render();
check("Reports: the same, through its early return too",
      view.scrollTop, 220);

ctx.set("beadsSection", "board");
ctx.set("beadsSearch", { q: "scroll" });
view.scrollTop = 130;
ctx.render();
check("a search result list keeps the page scroll as well",
      view.scrollTop, 130);
ctx.set("beadsSearch", { q: "" });

ctx.set("beadsCache", null);
view.scrollTop = 90;
ctx.render();
check("...and so does the loading state", view.scrollTop, 90);
ctx.set("beadsCache", { boards: [{ root: "/repo", sessions: [] }] });

view.scrollTop = 0;
ctx.render();
check("a page at the top is left at the top", view.scrollTop, 0);

/* ---- the board canvas, which was already kept -------------------------- */
ctx.render();
view.querySelector("#beads-canvas").scrollTop = 300;
ctx.render();
check("the canvas keeps its scroll on a poll of the same page",
      view.querySelector("#beads-canvas").scrollTop, 300);

/* ---- the detail pane ---------------------------------------------------- */
ctx.set("beadsFocus", "claunch-aaa");
ctx.render();
view.querySelector(".beads-detail").scrollTop = 640;
ctx.render();
check("the open issue keeps its place in the detail pane",
      view.querySelector(".beads-detail").scrollTop, 640);

view.querySelector(".beads-detail").scrollTop = 640;
ctx.set("beadsFocus", "claunch-bbb");
ctx.render();
check("another issue opened in the pane starts at its own top",
      view.querySelector(".beads-detail").scrollTop, 0);
ctx.set("beadsFocus", "");

/* ---- the queues grid's sideways scroll ----------------------------------- */
ctx.set("beadsSection", "queues");
ctx.render();
view.querySelector(".beads-queues").scrollLeft = 520;
ctx.render();
check("the queues grid stays scrolled to the sessions on the right",
      view.querySelector(".beads-queues").scrollLeft, 520);
ctx.set("beadsSection", "board");

/* ---- a selection -------------------------------------------------------- */
ctx.render();
const inside = view.querySelector(".beads-board");
function sel(collapsed, container) {
  return {
    isCollapsed: collapsed, rangeCount: 1,
    getRangeAt: () => ({ commonAncestorContainer: container }),
  };
}
selection = sel(false, { nodeType: 3, parentNode: inside });
ctx.render();
check("text being selected on the page holds the rebuild off",
      view.querySelector(".beads-board") === inside, true);

selection = sel(true, inside);
ctx.render();
check("a caret is not a selection, so the poll paints",
      view.querySelector(".beads-board") === inside, false);
selection = null;

if (failures) {
  console.error(`beadsstill_check: ${failures} failing`);
  process.exit(1);
}
console.log("beadsstill_check: ok");
