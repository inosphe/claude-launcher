/* The reader's own note on a session, in the three web places it is drawn —
   the rail row, the terminal header's chip, and the detail panel — run
   against the real functions from app.js and the real refreshSessions
   against a stub DOM.

   The note is the one thing on a session record that the *person* wrote.
   Everything else there is the launch's or the daemon's bookkeeping, so the
   failures worth pinning are the ones where a user's own words go missing or
   come back changed:

   - It is on the row. `decorateNoteRow` is driven through refreshSessions,
     because a helper that is right and is never called leaves no trace. An
     absent note draws nothing at all (not an empty line that reads as a
     rendering bug), and a whitespace-only note counts as absent.
   - It lands in the right place: under the name, ahead of the directory
     line. Both lines are `order: 2` full-width breakers, so DOM order is
     what decides, and appending would put the reader's own line at the
     bottom of the row — under the facts they wrote it to annotate.
   - It is read-only to the page. The note goes in as TEXT: a note is
     arbitrary user input and the row is built with createElement, so a note
     holding markup must come out as its own literal characters.
   - The header chip is a LABEL. The bar is one line at a fixed width and a
     note has no length limit, so the chip says that a note exists and its
     title carries the note. The chip is written only when the value moves:
     toolbar.js's fit() watches this header's class and title, so a write
     that repeats itself buys a re-measure of the whole bar every poll.
   - Both the rail line and the chip are in the stylesheet the way a
     full-width rail line and a hover-label have to be. raillayout_check
     pins the breaker budget; this pins these entries. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8"
);
const css = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "style.css"),
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
    tag, kids: [], parent: null, text: "", classes: new Set(), dataset: {}, style: {},
    title: "", type: "",
    appendChild(c) { c.parent = n; n.kids.push(c); return c; },
    insertBefore(c, ref) {
      const at = n.kids.indexOf(ref);
      c.parent = n;
      n.kids.splice(at < 0 ? n.kids.length : at, 0, c);
      return c;
    },
    append(...cs) { cs.forEach((c) => n.appendChild(c)); },
    remove() {
      if (!n.parent) return;
      const at = n.parent.kids.indexOf(n);
      if (at >= 0) n.parent.kids.splice(at, 1);
      n.parent = null;
    },
    addEventListener() {},
    /* decorateNoteRow finds its own line through this, so it has to be a
       real lookup rather than a per-test override: the "repaint, do not
       stack" and "a cleared note takes its line off" checks are exactly
       what a stub that always answered null would hide. Only the `.class`
       form the rail uses is supported; anything else is no match. */
    querySelector(sel) {
      const want = String(sel).startsWith(".") ? String(sel).slice(1) : null;
      if (!want) return null;
      return descendants(n).find((k) => k.classes.has(want)) || null;
    },
    querySelectorAll() { return []; },
    get textContent() { return n.text; },
    set textContent(v) { n.text = String(v); },
    get className() { return [...n.classes].join(" "); },
    set className(v) { n.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get innerHTML() { return n.innerHTMLWrites.length ? "<set>" : ""; },
    set innerHTML(v) {
      n.innerHTMLWrites.push(String(v));
      n.kids.forEach((k) => { k.parent = null; });
      n.kids = [];
    },
  };
  // How many times this node was built by HTML rather than by createElement.
  // The note line must never be; see the markup-as-text check below.
  n.innerHTMLWrites = [];
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
// The header chip the real renderTermNote paints. A real element so the
// "written only on change" check can watch it the way toolbar.js does.
const chip = node("span");
chip.className = "term-note hidden";
let served = { sessions: [] };
const api = async () => ({ ok: true, json: async () => served });

/* Everything refreshSessions and renderTermNote lean on that is not this
   feature. The other rail lines are the same fixed/no-op stubs railcwd_check
   and railseen_check carry — each is another harness's subject. */
const stubs = `
function sessionMatchesFilter() { return true; }
let sessionsCache = [], currentName = null, currentPage = "home";
let attachedPid = null, linkState = "down", sessName = null, snapshotName = null;
function railHeld() { return false; }
let railRedrawPending = false;
function forgetDeadSessions() {}
function refreshResumeChoices() {}
function refreshParentChoices() {}
function renderHome() {}
function syncBulkActions() {}
function syncMobileBars() {}
function renderTermHandle() {}
function applyCflowBadges() {}
function applyGotoFlash() {}
function applyRailQuiet() {}
function applyBriefingCards() {}
function decorateBriefingRow(li, s) {}
function terminalOnScreen() { return false; }
function attach() {}
function setStatusBadge() {}
function ctxNoteOnRow() {}
function ctxRailLine(s) { return el("span", "rail-ctx-line unknown"); }
function railSeenLine(s) { return el("span", "rail-seen"); }
function $(id) { return id === "term-note" ? chip : list; }
const MOBILE_MQ = { get matches() { return false; } };
function openSpawnModal() {}
function go() {}
function closeDetail() {}
`;

const ctx = {};
new Function(
  "exports", "document", "el", "api", "list", "meshCache", "sessionGroupByMesh",
  "chip",
  stubs + capLine[0] + "\n"
  + slice("byLineage") + slice("sessionMeshGroup") + slice("sessMeshes")
  + slice("railMeshTags") + slice("sessHandles") + slice("handleTag")
  + slice("shortenPath") + slice("cwdSplit") + slice("cwdShort")
  + slice("cwdLine") + slice("railCwdLine")
  + slice("profileHarnessLabel") + slice("railMetaText")
  + slice("decorateNoteRow")
  + slice("renderTermNote")
  + slice("refreshSessions")
  + `
Object.assign(exports, {
  refresh: refreshSessions,
  decorate: decorateNoteRow,
  termNote: renderTermNote,
  setSessions: (rows) => { sessionsCache = rows; served = { sessions: rows }; },
  setCurrent: (name) => { currentName = name; },
});`)(ctx, document, el, api, list, [], true, chip);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const NOTE = "kept for the migration thread";
const row = (s) => { const li = el("li", "sess-card"); ctx.decorate(li, s); return li; };
const noteIn = (li) => descendants(li).filter((k) => k.classes.has("rail-note"));

/* ---- presence ---------------------------------------------------------- */
check("a session with no note draws no line at all",
      noteIn(row({ name: "s1" })).length, 0);
/* Whitespace is not an annotation, and the row would otherwise carry an
   empty line that reads as a rendering bug. */
check("...nor does one whose note is blank",
      noteIn(row({ name: "s1", note: "   \n  " })).length, 0);
check("...nor a null note", noteIn(row({ name: "s1", note: null })).length, 0);

const one = row({ name: "s1", note: NOTE });
check("a note draws exactly one line", noteIn(one).length, 1);
check("...carrying the note", noteIn(one)[0].text, NOTE);
check("...and the whole of it on hover, since the column clips it",
      noteIn(one)[0].title, NOTE);

/* ---- placement --------------------------------------------------------- */
/* Both this line and the directory line are order-2 full-width breakers, so
   the DOM position is what decides which is read first — and the reader's
   own words belong above the facts they annotate. */
const placed = el("li", "sess-card");
placed.appendChild(el("div", "rail-cwd"));
ctx.decorate(placed, { name: "s1", note: NOTE });
check("the line lands before the directory line",
      placed.kids.map((k) => k.className), ["rail-note", "rail-cwd"]);
/* A row's cwd line is always drawn (railCwdLine returns one for a session
   with no directory of its own too), but the fallback has to append rather
   than throw if one is ever missing. */
const orphan = el("li", "sess-card");
ctx.decorate(orphan, { name: "s1", note: NOTE });
check("with no directory line to sit before it still draws",
      orphan.kids.map((k) => k.className), ["rail-note"]);

/* ---- repaint, don't duplicate ------------------------------------------ */
const again = el("li", "sess-card");
ctx.decorate(again, { name: "s1", note: NOTE });
ctx.decorate(again, { name: "s1", note: NOTE });
check("a repeat call repaints rather than stacking a second line",
      again.kids.filter((k) => k.classes.has("rail-note")).length, 1);
ctx.decorate(again, { name: "s1", note: "something else now" });
check("...and a changed note is what the line then says",
      again.kids.filter((k) => k.classes.has("rail-note")).map((k) => k.text),
      ["something else now"]);
ctx.decorate(again, { name: "s1", note: "" });
check("a cleared note takes its line off the row", again.kids.length, 0);

/* ---- the page never parses it ------------------------------------------ */
/* A note is arbitrary user input. The row is built with createElement, so a
   note holding markup must survive as its own characters — the one failure
   here that is a security bug rather than a display bug. */
const HOSTILE = '"><img src=x onerror=alert(1)>';
const hostileLine = row({ name: "s1", note: HOSTILE });
check("markup in a note stays text, character for character",
      noteIn(hostileLine)[0].text, HOSTILE);
check("...and builds nothing from it",
      noteIn(hostileLine)[0].kids.length, 0);
check("...and is never written as HTML",
      noteIn(hostileLine)[0].innerHTMLWrites.length, 0);

/* ---- the header chip --------------------------------------------------- */
/* The chip is a LABEL and the note is the title: the bar is one line at a
   fixed width, and a note has whatever length the person typed. */
ctx.setSessions([{ name: "s1", note: NOTE }]);
ctx.setCurrent("s1");
ctx.termNote();
check("the chip is up for a session with a note",
      chip.classes.has("hidden"), false);
check("...saying only that a note exists, not the note",
      chip.textContent, "note");
check("...with the note itself one hover away", chip.title, NOTE);

ctx.setSessions([{ name: "s1" }]);
ctx.termNote();
check("...and away for one without", chip.classes.has("hidden"), true);
check("...carrying nothing", [chip.textContent, chip.title], ["", ""]);

/* The chip is repainted on every poll, and toolbar.js's fit() watches this
   header's title and class — so a write that repeats the last one makes the
   bar re-measure itself once per interval for nothing. The chip must leave a
   title alone that it has already set. */
ctx.setSessions([{ name: "s1", note: NOTE }]);
ctx.termNote();
const painted = chip.title;
chip.title = `${painted} — refit by toolbar`;   // what toolbar.js leaves behind
ctx.termNote();
check("an unchanged note does not rewrite the chip",
      chip.title, `${painted} — refit by toolbar`);
chip.title = painted;                            // and restore for the next check
ctx.setSessions([{ name: "s1", note: "a different note" }]);
ctx.termNote();
check("...but a changed one does", chip.title, "a different note");

/* The open session is the one whose note the header shows, and the header
   has no session of its own to ask. */
ctx.setSessions([{ name: "s1", note: NOTE }, { name: "s2", note: "the other one" }]);
ctx.setCurrent("s2");
ctx.termNote();
check("the header shows the ATTACHED session's note", chip.title, "the other one");

/* ---- the stylesheet ---------------------------------------------------- */
/* Full-width so it breaks the row (a loose span would squeeze the name), and
   wrapping rather than ellipsised: the briefing one-liner beside it wraps for
   the same reason, and a note is prose whose length nobody chose for a 260px
   column. `anywhere` because it may hold a path or a branch name that offers
   no break of its own. */
const rule = (css.match(/#session-list \.rail-note \{([^}]*)\}/) || [])[1] || "";
check("the rail line is full-width", /flex-basis:\s*100%/.test(rule), true);
check("...and wraps rather than being cut", /white-space:\s*pre-wrap/.test(rule), true);
check("...breaking inside long words", /overflow-wrap:\s*anywhere/.test(rule), true);
const order = Number((rule.match(/order:\s*(\d+)/) || [])[1]);
const toggle = Number(((css.match(/#session-list \.sess-brief-toggle \{([^}]*)\}/) || [])[1]
                       .match(/order:\s*(\d+)/) || [])[1]);
check("...sorted after the ▸ toggle so the toggle keeps the name line",
      Number.isFinite(order) && Number.isFinite(toggle) && order > toggle, true);

const chipRule = (css.match(/\.term-note \{([^}]*)\}/) || [])[1] || "";
check("the header chip has a rule of its own", chipRule.length > 0, true);
/* `help` because the hover IS the interaction: the chip says nothing on its
   own and everything it has to say is in the title. */
check("...and says with the pointer that hovering is the point",
      /cursor:\s*help/.test(chipRule), true);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("railnote_check: all checks passed");
