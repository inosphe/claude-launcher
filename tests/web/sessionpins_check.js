/* Session pins: the rows the reader lifted to the top of the rail.

   Three things share one feature and fail apart. The state helpers (what
   is pinned, in what order, what survives a poll); the rail (a pinned row
   is drawn once, at the top, out of the lineage tree, and the state filter
   leaves it alone); and the controls (the row's 📌, `p` on a focused card,
   the header chip) that all have to call the same verb. The checks below
   pin each against the shipped code rather than a description of it.

   The row builder is sliced out of refreshSessions the way railkeys_check
   does it, with the same standing liability: a branch that adds a call to
   the builder breaks this harness with a ReferenceError that reads like a
   defect and is not one. The stubs list at the bottom is where to add the
   missing name. */
const fs = require("fs");
const path = require("path");

const root = path.join(__dirname, "..", "..");
const src = fs.readFileSync(path.join(
  root, "src", "claude_launcher", "web", "static", "app.js"), "utf8");
const html = fs.readFileSync(path.join(
  root, "src", "claude_launcher", "web", "static", "index.html"), "utf8");
const css = fs.readFileSync(path.join(
  root, "src", "claude_launcher", "web", "static", "style.css"), "utf8");

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
function constLine(name) {
  const m = src.match(new RegExp(`^const ${name} = .+$`, "m"));
  if (!m) throw new Error(`cannot locate ${name} in app.js`);
  return m[0] + "\n";
}
/* The pin block whole: the storage, the helpers, the toggle. */
function pinsBlock() {
  const a = src.indexOf("const SESSION_PIN_KEY");
  const b = src.indexOf("/* ---- end rail pins", a);
  if (a < 0 || b <= a) throw new Error("cannot locate the rail pins block");
  return src.slice(a, b);
}
/* The group helpers the rail needs to draw a section: key, fold state. */
function groupBlock() {
  const a = src.indexOf("const SESSION_GROUP_COLLAPSE_KEY");
  const b = src.indexOf("/* ---- rail pins", a);
  if (a < 0 || b <= a) throw new Error("cannot locate the group fold block");
  return src.slice(a, b);
}
/* The filter sync, which is what applies the state filter to the rows. */
function filterBlock() {
  const a = src.indexOf("function sessionCategory(");
  const b = src.indexOf("/* ---- rail keyboard focus", a);
  if (a < 0 || b <= a) throw new Error("cannot locate the session filter block");
  return src.slice(a, b);
}

/* ---- stub DOM ---- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), dataset: {}, style: {
      setProperty() {},
    },
    attrs: {}, title: "", type: "", tabIndex: undefined, handlers: {},
    appendChild(c) { n.kids.push(c); c.parentNode = n; return c; },
    append(...cs) { cs.forEach((c) => n.appendChild(c)); },
    addEventListener(name, fn) { n.handlers[name] = fn; },
    setAttribute(name, value) { n.attrs[name] = value; },
    focus() {},
    contains(other) { return other === n || descendants(n).includes(other); },
    querySelector(sel) {
      const cls = sel.startsWith(".") ? sel.slice(1) : null;
      return descendants(n).find((k) => cls && k.classes.has(cls)) || null;
    },
    querySelectorAll(sel) {
      if (sel === "li[data-name]") {
        return descendants(n).filter((k) => k.tag === "li" && k.dataset.name);
      }
      const cls = sel.startsWith(".") ? sel.slice(1) : null;
      return descendants(n).filter((k) => cls && k.classes.has(cls));
    },
    getBoundingClientRect() { return { height: 20 }; },
    get textContent() { return n.text; },
    set textContent(v) { n.text = String(v); },
    get className() { return [...n.classes].join(" "); },
    set className(v) { n.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get innerHTML() { return ""; },
    set innerHTML(v) { n.kids = []; },
    get offsetHeight() { return 20; },
    get clientHeight() { return 800; },
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
const document = {
  createElement: node,
  createDocumentFragment: () => node("fragment"),
  createTextNode: (text) => ({ textContent: text }),
  addEventListener() {},
  querySelectorAll() { return []; },
};
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const list = node("ul");
const termPin = node("button");
const elements = { "session-list": list, "term-pin": termPin };
const $ = (id) => elements[id] || null;

const store = new Map();
const writes = [];
const localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => { store.set(k, v); writes.push([k, v]); },
  removeItem: (k) => store.delete(k),
};
// A pin remembered by an earlier visit, and a stale one for a session that
// is no longer in the fleet -- the prune has to drop the second and keep
// the first.
store.set("claunch_session_pins:/t/local/", JSON.stringify(["s2", "gone"]));

let served = { sessions: [] };
const api = async () => ({ ok: true, status: 200, json: async () => served });
const location = { hash: "" };

/* Everything the row builder leans on that this harness is not about. */
const stubs = `
let sessionsCache = [], currentName = null, currentPage = "home";
let attachedPid = null, linkState = "down", sessName = null, snapshotName = null;
let keptTerms = new Map();
let briefingLLM = true, ragConfigured = false;
let sessionGroupOrder = [], sessionGroupByMesh = false, sessionGroupByWorkspace = false;
let sessionFilter = "current";
const SESSION_FILTERS = ["current", "running", "killed", "paused", "archived"];
const meshCache = [];
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
function reconcileKillUiState() {}
function meshGroupSpawnTarget() { return { name: null, reason: "no leader" }; }
function openSpawnModal() {}
function openDetail() {}
function sessionWorkspaceLabel(v) { return v; }
function sessionGroupValue(group, s) { return group === "mesh" ? sessionMeshGroup(s) : "ws"; }
function railCardKey() {}
function syncRailKeys() {}
function railFocusedCardName() { return null; }
function restoreRailFocus() {}
function refreshRailSeen() {}
function ctxNoteOnRow() {}
function ctxRailLine() { return null; }
function tpsRailLine() { return null; }
function railCwdLine() { return el("span", "rail-cwd"); }
function railSeenLine() { return el("span", "rail-seen"); }
function railMetaText() { return ""; }
function handleTag() { return null; }
function railMeshTags() { return []; }
function sessMeshes() { return []; }
function sessionMeshGroup() { return "(no mesh)"; }
function syncSessionGroupStickyOffsets() {}
`;

const ctx = {};
new Function(
  "exports", "document", "el", "api", "$", "location", "localStorage", "BASE",
  stubs + slice("byLineage") + groupBlock() + pinsBlock()
  + slice("sessionGroupRows") + filterBlock()
  + slice("refreshSessions")
  + `
function railCardKill() { return false; }
` + slice("railCardKey") + `
Object.assign(exports, {
  refresh: refreshSessions,
  pinned: sessionPinnedNames,
  isPinned: isSessionPinned,
  setPinned: setSessionPinned,
  toggle: toggleSessionPin,
  clear: clearSessionPins,
  prune: pruneSessionPins,
  rowsOf: sessionPinRows,
  syncUi: syncSessionPinUi,
  syncFilters: syncSessionFilters,
  setFilter: (f) => { sessionFilter = f; },
  key: railCardKey,
  attachTo: (name) => { currentName = name; },
  setGroups: (order) => { sessionGroupOrder = order; sessionGroupByMesh = order.includes("mesh"); },
  cache: () => sessionsCache,
});`)(ctx, document, el, api, $, location, localStorage, "/t/local/");

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const rows = () => list.querySelectorAll("li[data-name]");
const names = () => rows().map((r) => r.dataset.name);
const row = (name) => rows().find((r) => r.dataset.name === name);
const pinButton = (name) => row(name).querySelector(".sess-pin");
const headings = () => list.querySelectorAll(".session-group-heading");
// The stub DOM does not fold child text into a parent's textContent, so a
// nested heading's label is read off its name span.
const headingText = (h) => {
  const name = h.querySelector(".session-group-name");
  return name ? name.textContent : h.textContent;
};
const ev = (key, over = {}) => Object.assign({
  key, ctrlKey: false, metaKey: false, altKey: false, shiftKey: false,
  prevented: 0, preventDefault() { this.prevented += 1; },
  target: over.target, currentTarget: over.target,
}, over);

(async () => {
  check("pins are read back from the browser, in order",
        ctx.pinned(), ["s2", "gone"]);

  served = { sessions: [
    { name: "s1", status: "busy" },
    { name: "s2", status: "idle", parent: "s1" },
    { name: "s3", status: "exited", parent: "s2" },
    { name: "s4", status: "idle" },
  ] };
  await ctx.refresh();

  check("a pin whose session left the fleet is dropped at the poll",
        ctx.pinned(), ["s2"]);
  check("the prune is written back so a reload does not resurrect it",
        writes.at(-1), ["claunch_session_pins:/t/local/", "[\"s2\"]"]);
  check("the pinned row is drawn once, first, out of its parent's tree",
        names(), ["s2", "s1", "s3", "s4"]);
  check("the pinned section is a heading of its own above the fleet",
        headings().map(headingText), ["📌 pinned"]);
  check("the section counts its rows",
        headings()[0].querySelector(".session-group-count").textContent, "1");
  check("the section carries the one action that empties it",
        !!headings()[0].querySelector(".session-group-unpin"), true);
  check("the pinned row is marked and not indented as a child",
        [row("s2").classes.has("pinned"), row("s2").classes.has("child"),
         row("s2").style.paddingLeft], [true, false, undefined]);
  check("the lineage the pinned row left is in its tooltip",
        row("s2").title, "spawned by s1");
  check("the pinned row's child is promoted to a root, not orphaned",
        [row("s3").classes.has("child"), !!row("s3")], [false, true]);
  check("the rows after the section land in the list, not in the section",
        list.kids.map((k) => k.classes.has("session-group") ? "group" : k.dataset.name),
        ["group", "s1", "s3", "s4"]);
  check("every row carries a pin toggle that names its session",
        rows().map((r) => pinButton(r.dataset.name).dataset.name), ["s2", "s1", "s3", "s4"]);
  check("the toggle is lit on the pinned row alone, and says so to a reader",
        rows().map((r) => [pinButton(r.dataset.name).classes.has("on"),
                           pinButton(r.dataset.name).attrs["aria-pressed"]]),
        [[true, "true"], [false, "false"], [false, "false"], [false, "false"]]);
  check("the pin button sits between the + and the ⓘ",
        row("s1").kids.map((k) => k.className).filter((c) =>
          /sess-(plus|pin|info)/.test(c)), ["sess-plus", "sess-pin", "sess-info"]);

  // The row's 📌 is the toggle, and toggling rebuilds the rail at once.
  pinButton("s4").handlers.click({ stopPropagation() {} });
  await new Promise((r) => setTimeout(r, 0));
  check("pinning appends: the newest pin is the last of the section",
        ctx.pinned(), ["s2", "s4"]);
  check("the rail moves the row at the click, not at the next poll",
        names(), ["s2", "s4", "s1", "s3"]);
  check("the pin is remembered in this browser",
        writes.at(-1), ["claunch_session_pins:/t/local/", "[\"s2\",\"s4\"]"]);

  // `p` on the focused card is the same verb.
  const s1 = row("s1");
  const press = ev("p", { target: s1 });
  check("`p` on a card pins its session", ctx.key(press, "s1"), true);
  check("the keypress is consumed", press.prevented, 1);
  await new Promise((r) => setTimeout(r, 0));
  check("the card's session joins the section", ctx.pinned(), ["s2", "s4", "s1"]);
  check("`p` with a modifier is the browser's",
        ctx.key(ev("p", { target: s1, ctrlKey: true }), "s1"), false);
  check("`p` on the card's button is the button's",
        ctx.key(ev("p", { target: s1, currentTarget: pinButton("s1") }), "s1"), false);

  // Pressing again lets go, and the row goes back to its place in the tree.
  ctx.key(ev("p", { target: row("s1") }), "s1");
  await new Promise((r) => setTimeout(r, 0));
  check("`p` on a pinned card unpins it", ctx.pinned(), ["s2", "s4"]);
  check("the unpinned row returns to the tree with its child under it",
        names(), ["s2", "s4", "s1", "s3"]);
  check("s3 stays a root while its own parent s2 is still pinned",
        [row("s3").classes.has("child"), row("s1").classes.has("child")], [false, false]);

  // The header chip follows the attached session.
  ctx.attachTo("s4");
  ctx.syncUi();
  check("the header chip is pressed while the attached session is pinned",
        [termPin.attrs["aria-pressed"], termPin.classes.has("on")], ["true", true]);
  ctx.attachTo("s1");
  ctx.syncUi();
  check("and released when it is not",
        [termPin.attrs["aria-pressed"], termPin.classes.has("on")], ["false", false]);

  // The state filter leaves a pinned row alone; the search does not.
  ctx.setFilter("running");
  ctx.syncFilters(ctx.cache());
  check("a pinned exited row is not hidden by the running filter",
        rows().map((r) => [r.dataset.name, r.classes.has("session-filtered")]),
        [["s2", false], ["s4", false], ["s1", false], ["s3", true]]);
  ctx.setPinned("s3", true);
  ctx.syncFilters(ctx.cache());
  check("pinning an exited row brings it through the running filter",
        row("s3").classes.has("session-filtered"), false);
  ctx.setPinned("s3", false);
  ctx.setFilter("current");

  // Grouping: the pinned section stays ahead of every group and outside them.
  ctx.setGroups(["mesh"]);
  await ctx.refresh();
  check("with grouping on, the pinned section is still the first heading",
        headings().map(headingText), ["📌 pinned", "mesh · (no mesh)"]);
  check("the pinned rows are in the section, the rest in their group",
        list.kids.map((k) => [k.classes.has("session-group-pinned"),
          k.querySelectorAll("li[data-name]").map((r) => r.dataset.name)]),
        [[true, ["s2", "s4"]], [false, ["s1", "s3"]]]);
  ctx.setGroups([]);

  // Unpin all: the section goes, the rows return to the tree.
  await ctx.refresh();
  headings()[0].querySelector(".session-group-unpin").handlers.click({ stopPropagation() {} });
  await new Promise((r) => setTimeout(r, 0));
  check("unpin all empties the pins", ctx.pinned(), []);
  check("and the rail is the plain lineage tree again",
        [names(), headings().length], [["s1", "s2", "s3", "s4"], 0]);
  check("the lineage is back: s2 under s1, s3 under s2",
        [row("s2").classes.has("child"), row("s3").classes.has("child"),
         row("s3").style.paddingLeft], [true, true, "32px"]);
  check("the empty list is remembered too",
        writes.at(-1), ["claunch_session_pins:/t/local/", "[]"]);

  // The shipped page and stylesheet carry the controls the code draws.
  check("the header has the pin chip", html.includes('id="term-pin"'), true);
  check("the chip is wired to the attached session",
        src.includes('$("term-pin").addEventListener("click"'), true);
  check("the pin state is styled on the row, the section and the chip",
        [/#session-list \.sess-pin\.on/.test(css),
         /#session-list li\.pinned/.test(css),
         /\.session-group-unpin/.test(css),
         /\.pin-chip\[aria-pressed="true"\]/.test(css)],
        [true, true, true, true]);
  check("the pins are part of the rail's rebuild signature",
        src.includes("[briefingLLM, sessionsCache, groupOrder, meshCache, pins]"), true);

  if (failures) process.exit(1);
  console.log("sessionpins_check: ok");
})().catch((e) => { console.error(e); process.exit(1); });
