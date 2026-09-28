/* Pin persistence and controls; rail rows retain ordinary order and filters. */
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
/* The rail card's key table, which railCardKey dispatches on. Sliced whole
   rather than retyped: a binding this harness stubbed out by hand would let
   `f` keep pinning here after the page had moved it. */
function keyTable() {
  const a = src.indexOf("const RAIL_CARD_KEYS = [");
  if (a < 0) throw new Error("cannot locate RAIL_CARD_KEYS in app.js");
  const b = src.indexOf("\n];", a);
  if (b <= a) throw new Error("unbalanced RAIL_CARD_KEYS");
  return src.slice(a, b + 3);
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
/* The reader's own note line, stubbed for the same reason as the briefing
   decoration below: its wording and its stylesheet contract are
   railnote_check's subject, not this harness's. */
function decorateNoteRow(li, s) {}
function decorateBriefingRow(li, s) {}
function terminalOnScreen() { return false; }
function attach() {}
function setStatusBadge() {}
function reconcileKillUiState() {}
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
  + slice("regroupDepths") + slice("sessionGroupRows") + filterBlock()
  + slice("refreshSessions")
  + `
function railCardKill() { return false; }
function railCardPause() { return false; }
function railCardArchive() { return false; }
function railCardApprove() { return false; }
` + keyTable() + slice("railCardKey") + `
Object.assign(exports, {
  refresh: refreshSessions,
  pinned: sessionPinnedNames,
  isPinned: isSessionPinned,
  setPinned: setSessionPinned,
  toggle: toggleSessionPin,
  clear: clearSessionPins,
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
/* The pin strip above the list, and the label cards on it. */
const bar = () => list.kids.find((k) => k.classes.has("session-pinbar")) || null;
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

  check("partial polls preserve pins outside the response category", ctx.pinned(), ["s2", "gone"]);
  ctx.setPinned("gone", false);
  check("the rail has no pinned label strip", !!bar(), false);
  check("pinning does not move rows", names(), ["s1", "s2", "s3", "s4"]);
  check("the pinned session keeps its place and indent in the tree",
        [row("s2").classes.has("child"), row("s2").style.paddingLeft,
         row("s2").title], [true, "22px", "spawned by s1"]);
  check("its child is still under it rather than promoted to a root",
        [row("s3").classes.has("child"), row("s3").style.paddingLeft],
        [true, "32px"]);
  check("every row carries a pin toggle that names its session",
        rows().map((r) => pinButton(r.dataset.name).dataset.name), ["s1", "s2", "s3", "s4"]);
  check("the toggle is lit on the pinned row alone, and says so to a reader",
        rows().map((r) => [pinButton(r.dataset.name).classes.has("on"),
                           pinButton(r.dataset.name).attrs["aria-pressed"]]),
        [[false, "false"], [true, "true"], [false, "false"], [false, "false"]]);
  check("the pin button sits between the + and the ⓘ",
        row("s1").kids.map((k) => k.className).filter((c) =>
          /sess-(plus|pin|info)/.test(c)), ["sess-plus", "sess-pin", "sess-info"]);

  // The row's pin button is the toggle, and toggling rebuilds the rail at once.
  pinButton("s4").handlers.click({ stopPropagation() {} });
  await new Promise((r) => setTimeout(r, 0));
  check("pinning appends without reordering existing pins",
        ctx.pinned(), ["s2", "s4"]);
  check("pinning leaves the tree in place",
        names(), ["s1", "s2", "s3", "s4"]);
  check("the pin is remembered in this browser",
        writes.at(-1), ["claunch_session_pins:/t/local/", "[\"s2\",\"s4\"]"]);

  // `f` on the focused card is the same verb. It was `p` until `p` became
  // pause (claunch-vxji); the two verbs must never share a key, because the
  // reader who means one of them gets no warning before the other happens.
  const s1 = row("s1");
  const press = ev("f", { target: s1 });
  check("`f` on a card pins its session", ctx.key(press, "s1"), true);
  check("the keypress is consumed", press.prevented, 1);
  await new Promise((r) => setTimeout(r, 0));
  check("the card's session joins the pins",
        ctx.pinned(), ["s2", "s4", "s1"]);
  check("`f` with a modifier is the browser's",
        ctx.key(ev("f", { target: s1, ctrlKey: true }), "s1"), false);
  check("`f` on the card's button is the button's",
        ctx.key(ev("f", { target: s1, currentTarget: pinButton("s1") }), "s1"), false);
  check("`p` no longer pins -- it is pause, and this harness stubs it out",
        ctx.key(ev("p", { target: s1 }), "s1"), false);
  check("`p` did not move the pinned set", ctx.pinned(), ["s2", "s4", "s1"]);

  // Pressing again lets go, and only the strip changes.
  ctx.key(ev("f", { target: row("s1") }), "s1");
  await new Promise((r) => setTimeout(r, 0));
  check("`f` on a pinned card unpins it",
        ctx.pinned(), ["s2", "s4"]);
  check("the tree was never disturbed by any of it",
        [names(), row("s2").classes.has("child"), row("s3").classes.has("child")],
        [["s1", "s2", "s3", "s4"], true, true]);

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
        [["s1", false], ["s2", false], ["s3", true], ["s4", false]]);
  ctx.setPinned("s3", true);
  ctx.syncFilters(ctx.cache());
  check("pinning leaves the running filter unchanged",
        row("s3").classes.has("session-filtered"), true);
  ctx.setPinned("s3", false);
  ctx.setFilter("current");

  // Grouping: the strip stays above every group and outside them.
  ctx.setGroups(["mesh"]);
  await ctx.refresh();
  check("grouping has no pinned strip",
        [list.kids[0].classes.has("session-pinbar"), headings().map(headingText)],
        [false, ["mesh · (no mesh)"]]);
  check("the pinned sessions are grouped with everything else, not lifted out",
        list.kids.filter((k) => k.classes.has("session-group")).map((k) =>
          k.querySelectorAll("li[data-name]").map((r) => r.dataset.name)),
        [["s1", "s2", "s3", "s4"]]);
  ctx.setGroups([]);

  // Unpin all: the strip goes, the rows never moved.
  await ctx.refresh();
  ctx.clear();
  await ctx.refresh();
  await new Promise((r) => setTimeout(r, 0));
  check("unpin all empties the pins", ctx.pinned(), []);
  check("and the strip goes with them", [!!bar(), names()],
        [false, ["s1", "s2", "s3", "s4"]]);
  check("the lineage is untouched: s2 under s1, s3 under s2",
        [row("s2").classes.has("child"), row("s3").classes.has("child"),
         row("s3").style.paddingLeft], [true, true, "32px"]);
  check("the empty list is remembered too",
        writes.at(-1), ["claunch_session_pins:/t/local/", "[]"]);

  // The shipped page and stylesheet carry the controls the code draws.
  check("the header has the pin chip", html.includes('id="term-pin"'), true);
  check("the chip is wired to the attached session",
        src.includes('$("term-pin").addEventListener("click"'), true);
  check("the main region has session tabs", html.includes('id="session-tabs"'), true);
  check("the old pinned strip styles are removed", css.includes("li.session-pinbar"), false);
  check("the pins are part of the rail's rebuild signature",
        src.includes("[briefingLLM, sessionsCache, groupOrder, meshCache, pins]"), true);

  if (failures) process.exit(1);
  console.log("sessionpins_check: ok");
})().catch((e) => { console.error(e); process.exit(1); });
