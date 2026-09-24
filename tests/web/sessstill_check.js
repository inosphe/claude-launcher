/* The session detail panel holding still under its own poll.

   `#sess-view` is repainted from scratch every five seconds: renderSession
   empties the node and rebuilds every section from the /meta answer. That is
   fine for the facts it draws and wrong for everything a reader is in the
   middle of doing to it, and three of those had no defence at all:

     the scroll     -- `#sess-view` is the scroll container (style.css:
                       `overflow-y: auto`). Emptying it collapses its content
                       to nothing, the browser clamps scrollTop to 0, and the
                       rebuild leaves it there. So every poll threw the reader
                       back to the top and nothing below the fold could be
                       used for longer than five seconds at a time.
     a selection    -- the run of text somebody has dragged across. A focused
                       FIELD already held the poll back (formInUse), but the
                       lines worth copying in this panel are plain divs and
                       take no focus, so the rebuild took the selection with
                       the nodes that carried it.
     the journal    -- `sess-input-journal` is a <details>. Built fresh every
                       poll, an expanded fold snapped shut and its lines were
                       replaced underneath whoever was reading them. It is now
                       the same kind of long-lived node as the send box: kept
                       across polls, reread when it is reopened, and refetched
                       in the background only while it is shut.

   All three were reported as one thing: the detail panel keeps refreshing,
   so dragging does not work, the input journal worst of all (claunch-ke7ke).

   renderSession itself is driven here rather than read, with every section
   stubbed out the way railmodel_check stubs them: what is under test is the
   handful of lines around the rebuild, not what the rebuild draws. */
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
    // nodeType is what tells an element from the text inside it, which is
    // the distinction selectionInUse has to make about a range's ancestor.
    tag, nodeType: 1, kids: [], text: "", classes: new Set(), dataset: {},
    listeners: {}, open: false, scrollTop: 0,
    appendChild(c) { n.kids.push(c); return c; },
    addEventListener(t, fn) { (n.listeners[t] ||= []).push(fn); },
    fire(t) { (n.listeners[t] || []).forEach((fn) => fn({})); },
    contains(x) {
      if (x === n) return true;
      return n.kids.some((k) => k.contains && k.contains(x));
    },
    get textContent() { return n.text; },
    set textContent(v) { n.text = String(v); },
    get className() { return [...n.classes].join(" "); },
    set className(v) {
      n.classes = new Set(String(v).split(/\s+/).filter(Boolean));
    },
    get innerHTML() { return ""; },
    // The browser's own consequence, and the whole reason the scroll has to
    // be written down: emptying the container clamps its scroll to 0.
    set innerHTML(v) { if (v === "") { n.kids.length = 0; n.scrollTop = 0; } },
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

/* ---- the stylesheet fact the scroll rule stands on --------------------- */
check("the panel is the scroll container, so emptying it resets the scroll",
      /#sess-view \{[^}]*overflow-y: auto/.test(css), true);

/* ---- renderSession, with every section it draws stubbed away ----------- */
const view = node("div");
let selection = null;
let railNow = "details";
const stubs = `
let sessName = "coder1", sessRunFold = null;
function $(id) { return view; }
function formInUse() { return false; }
function sessLayoutFor() { return { rail: rail() }; }
function stopSessRun() {}
function metaRow() {}
function profileHarnessLabel() { return ""; }
function modelSentence() { return ""; }
function ctxSentence() { return ""; }
function ctxBreakdown() { return ""; }
function sessHead() { return el("div", "sess-head"); }
function sessRailTabs() { return el("div", "sess-rail-tabs"); }
/* The Details sub-tabs and their group heads — sesslayout_check and
   sesssubtabs_check are their harnesses; here they only have to resolve. */
function sessDetailTabs() { return el("div", "sess-subtabs"); }
function sessGroupHead(g) { return el("h2", "sess-group-head", g); }
function sessWorkflow() { return el("div", "sess-wf"); }
function sessBeadsPanel() { return el("div", "sess-beads-panel"); }
function sessBriefSection() { return el("div", "sess-brief"); }
function sessSend() { return el("div", "sess-send"); }
function sessHandoff() { return el("div", "sess-handoff"); }
function sessMeshJoin() { return el("div", "sess-mesh-join"); }
function sessQueued() { return null; }
function sessBackpressure() { return null; }
function sessReborrow() { return el("div", "sess-reborrow"); }
function sessPerms() { return el("div", "sess-perms"); }
function sessMigrate() { return el("div", "sess-migrate"); }
function sessNote() { return el("div", "sess-note"); }
function sessFlags() { return el("div", "sess-flags"); }
function sessTask() { return el("div", "sess-task"); }
function sessInputJournal() { return el("div", "sess-input-journal"); }
function rolePanels() { return []; }
function sessBeads() { return el("div", "sess-beads"); }
function sessCommits() { return el("div", "sess-commits"); }
function sessHandles() { return []; }
function handleTag() { return null; }
function go() {}
`;

const ctx = {};
new Function(
  "exports", "document", "el", "view", "window", "rail",
  stubs + slice("selectionInUse") + slice("renderSession") + `
Object.assign(exports, { render: renderSession });`
)(ctx, document, el, view,
  { getSelection: () => selection }, () => railNow);

const meta = { session: { name: "coder1", cols: 80, rows: 24 } };

/* ---- the scroll ---------------------------------------------------------
   Read before the wipe and written after the rebuild, at every exit this
   function has. The panel has two early returns of its own -- the workflow
   tab and the board tab -- and a restore placed only at the bottom would
   leave both of those tabs behaving exactly as the bug did. */
ctx.render(meta);
view.scrollTop = 940;          // the reader scrolled down to the journal
ctx.render(meta);
check("a poll leaves the reader where they had scrolled to",
      view.scrollTop, 940);

railNow = "wf";
view.scrollTop = 512;
ctx.render(meta);
check("...and so does a poll on the workflow tab, which returns early",
      view.scrollTop, 512);

railNow = "beads";
view.scrollTop = 333;
ctx.render(meta);
check("...and on the board tab, which returns early too",
      view.scrollTop, 333);
railNow = "details";

// A panel scrolled to the top has nothing to put back, and writing a 0 into a
// container that is already at 0 is a write nobody asked for.
view.scrollTop = 0;
ctx.render(meta);
check("a panel at the top is left at the top", view.scrollTop, 0);

/* ---- the selection ------------------------------------------------------ */
const inside = node("div");
const outside = node("div");

function sel(collapsed, container) {
  return {
    isCollapsed: collapsed, rangeCount: 1,
    getRangeAt: () => ({ commonAncestorContainer: container }),
  };
}

selection = null;
view.appendChild(inside);
ctx.render(meta);
check("no selection at all is no claim, so the poll paints",
      view.kids.includes(inside), false);

view.appendChild(inside);
selection = sel(true, inside);
ctx.render(meta);
check("a caret inside the panel is not a selection either",
      view.kids.includes(inside), false);

view.appendChild(inside);
selection = sel(false, inside);
const before = view.kids.length;
ctx.render(meta);
check("a live selection inside the panel holds the rebuild off",
      [view.kids.includes(inside), view.kids.length], [true, before]);

selection = sel(false, outside);
ctx.render(meta);
check("...while one made elsewhere on the page does not",
      view.kids.includes(inside), false);

// The case a real drag across a journal line actually produces: the range's
// common ancestor is the text node, not the element that holds it.
view.appendChild(inside);
selection = sel(false, { nodeType: 3, parentNode: inside });
ctx.render(meta);
check("a drag within one line reports a text node, and still counts",
      view.kids.includes(inside), true);
selection = null;

/* ---- the input journal -------------------------------------------------- */
const fetched = [];
const journalCtx = {};
new Function(
  "exports", "document", "el", "api", "encodeURIComponent",
  `let sessJournalBox = null;
` + slice("sessInputJournalFill") + slice("sessInputJournal") + `
Object.assign(exports, { build: sessInputJournal });`
)(journalCtx, document, el,
  // Never settles: what these checks count is the asking, and a resolved
  // answer would race the synchronous assertions below it.
  (url) => { fetched.push(url); return new Promise(() => {}); },
  (s) => s);

const first = journalCtx.build("coder1");
check("the fold is read once when it is built", fetched.length, 1);
check("...and it records whose journal it is", first.dataset.session, "coder1");

const again = journalCtx.build("coder1");
check("a poll gets the same live node back rather than a fresh one",
      again === first, true);
check("...and a shut fold is kept current in the background",
      fetched.length, 2);

first.open = true;
const whileOpen = journalCtx.build("coder1");
check("an open fold is the same node still", whileOpen === first, true);
check("...and is not refetched under the reader", fetched.length, 2);

first.fire("toggle");
check("reopening it IS the reread", fetched.length, 3);

first.open = false;
first.fire("toggle");
check("shutting it asks for nothing", fetched.length, 3);

const other = journalCtx.build("coder2");
check("repointing the panel at another session draws that session's journal",
      [other === first, other.dataset.session], [false, "coder2"]);

check("a background reread is abandoned if the fold opened meanwhile",
      /if \(quiet && box\.open\) return;/.test(slice("sessInputJournalFill")),
      true);
check("the live node is released on both teardowns, repoint and drop",
      (src.match(/^  sessJournalBox = null;$/gm) || []).length, 2);

if (failures) {
  console.error(`sessstill_check: ${failures} failing`);
  process.exit(1);
}
console.log("sessstill_check: ok");
