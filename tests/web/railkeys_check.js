/* Where the keyboard is, and what `k` does with it.

   Two facts share one card. `active` says this session's terminal is the one
   on screen; the chip this harness guards says the browser's keyboard is on
   the CARD rather than in that terminal. They are independent -- a card can
   be active while the keyboard is in the terminal, in the search box, or
   nowhere at all -- and the whole point of drawing the second one is that
   the reader is about to press `k`, which on the card ends a session and in
   the terminal is a letter typed into a prompt.

   So the checks below pin three things that fail silently and separately:
   the chip's state against the element that actually holds focus (a chip
   still reading `card` after the keyboard left is worse than no chip), the
   shortcut against the CARD's session rather than the attached one (the two
   differ the moment somebody tabs down the rail), and the row's own
   focusability, which is what makes the whole arrangement reachable without
   a pointer.

   The row builder is sliced out of refreshSessions rather than asserted
   against the source text, because the failure this is really for is the
   chip quietly ceasing to be appended: the state helpers would still be
   right, and every check that only read them would still be green. That
   slice carries the standing liability railseen_check names -- a branch
   that adds a call to the row builder breaks this harness with a
   ReferenceError that reads like a defect and is not one. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  process.env.RAILKEYS_APP_JS || path.join(
    __dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8"
);
/* The two states are colours before they are words, and a class the
   stylesheet has no rule for is invisible to every DOM check here. */
const css = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "style.css"),
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
function constLine(name) {
  const m = src.match(new RegExp(`^const ${name} = .+$`, "m"));
  if (!m) throw new Error(`cannot locate ${name} in app.js`);
  return m[0] + "\n";
}
/* The focus block whole: the state helpers, the chip copy and the two
   document listeners that move it. */
function keysBlock() {
  const a = src.indexOf("function railActiveElement(");
  const b = src.indexOf("/* ---- end rail keyboard focus", a);
  if (a < 0 || b <= a) throw new Error("cannot locate the rail keyboard block");
  return src.slice(a, b);
}

/* ---- stub DOM ---- */
let activeElement = null;
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), dataset: {}, style: {},
    title: "", type: "", tabIndex: undefined, handlers: {}, focused: 0,
    appendChild(c) { n.kids.push(c); c.parentNode = n; return c; },
    append(...cs) { cs.forEach((c) => n.appendChild(c)); },
    addEventListener(name, fn) { n.handlers[name] = fn; },
    focus(opts) { n.focused += 1; n.focusOpts = opts; activeElement = n; },
    contains(other) { return other === n || descendants(n).includes(other); },
    querySelector(sel) {
      const cls = sel.startsWith(".") ? sel.slice(1) : null;
      return descendants(n).find((k) => cls && k.classes.has(cls)) || null;
    },
    querySelectorAll(sel) {
      if (sel !== "li[data-name]") return [];
      return descendants(n).filter((k) => k.tag === "li" && k.dataset.name);
    },
    replaceWith(other) {
      const p = n.parentNode;
      if (!p) return;
      p.kids[p.kids.indexOf(n)] = other;
      other.parentNode = p;
    },
    get textContent() { return n.text; },
    set textContent(v) { n.text = String(v); },
    get className() { return [...n.classes].join(" "); },
    set className(v) { n.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get innerHTML() { return ""; },
    set innerHTML(v) { n.kids = []; },
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
const docHandlers = {};
const document = {
  createElement: node,
  get activeElement() { return activeElement; },
  addEventListener(name, fn) { docHandlers[name] = fn; },
  querySelectorAll() { return []; },
};
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const list = node("ul");
const terminal = node("div");
const termField = node("input");
const elements = {
  "session-list": list, "terminal": terminal, "term-input-field": termField,
  "term-kill": node("button"), "m-kill": node("button"),
};
const $ = (id) => elements[id] || null;

let served = { sessions: [] };
/* Every request the sliced code makes, so the kill ones can be read apart
   from the poll's own -- the poll runs on the same `api` and would otherwise
   count as traffic in the checks below that assert nothing was sent. */
const calls = [];
const api = async (url, options) => {
  calls.push({ url, options });
  const kill = url.includes("/kill");
  return {
    ok: true, status: 200,
    json: async () => (kill ? { status: "exited" } : served),
  };
};
const kills = () => calls.filter((c) => c.url.includes("/kill"));
const location = { hash: "" };

/* Everything the row builder leans on that this harness is not about. */
const stubs = `
function sessionMatchesFilter() { return true; }
let sessionsCache = [], currentName = null, currentPage = "home";
let attachedPid = null, linkState = "down", sessName = null, snapshotName = null;
let keptTerms = new Map();
const killUiState = new Map();
function syncSessionKillControls() {}
function modalInfo() {}
function dropKept() {}
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
`;

const ctx = {};
new Function(
  "exports", "document", "el", "api", "list", "meshCache", "sessionGroupByMesh",
  "$", "location",
  stubs + constLine("RAIL_MESH_TAGS") + constLine("CTX_DOMAIN")
  + constLine("RAIL_STALE_DEFAULT") + constLine("railStale")
  + slice("byLineage") + slice("sessionMeshGroup") + slice("sessMeshes")
  + slice("railMeshTags") + slice("sessHandles") + slice("handleTag")
  + slice("fmtAge") + slice("ctxShort") + slice("ctxAgeOf")
  + slice("ctxKnowable") + slice("ctxSentence") + slice("ctxBreakdown")
  + slice("ctxTooltip") + slice("ctxNoteOnRow") + slice("modelShort")
  + slice("shortenPath") + slice("cwdSplit") + slice("cwdShort")
  + slice("cwdLine") + slice("railCwdLine") + slice("ctxRailLine")
  + slice("seenAgo") + slice("seenPair") + slice("railSeenLine")
  + slice("profileHarnessLabel") + slice("railMetaText")
  + keysBlock() + slice("killSession") + slice("refreshSessions")
  + `
Object.assign(exports, {
  refresh: refreshSessions,
  state: railKeysState,
  sync: syncRailKeys,
  focused: railFocusedCardName,
  restore: restoreRailFocus,
  key: railCardKey,
  kill: railCardKill,
  attachTo: (name) => { currentName = name; },
  setSessions: (v) => { sessionsCache = v; },
});`)(ctx, document, el, api, list, [], false, $, location);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const rows = () => list.querySelectorAll("li[data-name]");
const row = (name) => rows().find((r) => r.dataset.name === name);
const chipText = (name) => row(name).querySelector(".rail-keys").textContent;
const hidden = () => rows().map((r) => r.querySelector(".rail-keys").classes.has("hidden"));
const ev = (key, over = {}) => Object.assign({
  key, ctrlKey: false, metaKey: false, altKey: false, shiftKey: false,
  prevented: 0, preventDefault() { this.prevented += 1; },
  target: over.target, currentTarget: over.target,
}, over);

(async () => {
  served = { sessions: [
    { name: "s1", status: "busy" },
    { name: "s2", status: "idle" },
    { name: "s3", status: "exited" },
  ] };
  await ctx.refresh();
  ctx.setSessions(served.sessions);
  ctx.attachTo("s1");

  /* ---------------------------------------------------------------- */
  /* the row is reachable, and carries the chip                        */
  /* ---------------------------------------------------------------- */
  check("every rail row is a card",
        rows().map((r) => r.classes.has("sess-card")), [true, true, true]);
  check("...and a tab stop, so the shortcut needs no pointer",
        rows().map((r) => r.tabIndex), [0, 0, 0]);
  check("every row carries the chip",
        rows().map((r) => !!r.querySelector(".rail-keys")), [true, true, true]);
  check("...hidden while the keyboard is elsewhere", hidden(),
        [true, true, true]);
  check("the row listens for its own keys",
        typeof row("s1").handlers.keydown, "function");

  /* ---------------------------------------------------------------- */
  /* what the chip says, against what actually holds focus             */
  /* ---------------------------------------------------------------- */
  row("s2").focus();
  ctx.sync();
  check("a focused card says so", ctx.state("s2"), "card");
  check("...in the chip", chipText("s2"), "⌨ card");
  check("...and says nothing on the other rows",
        [chipText("s1"), chipText("s3")], ["", ""]);
  check("...including the attached row, which is not holding the keyboard",
        ctx.state("s1"), "");

  /* The terminal: the keyboard is in the session window, and the fact
     belongs to the attached row alone -- a second row claiming it would
     point at a terminal that is not on screen. */
  activeElement = terminal;
  ctx.sync();
  check("the terminal holds it", ctx.state("s1"), "term");
  check("...and the chip says which", chipText("s1"), "⌨ session");
  check("...only on the attached row",
        [ctx.state("s2"), ctx.state("s3")], ["", ""]);

  /* xterm's keyboard target is a textarea inside #terminal, never the box
     itself: a check that only knew the box would read "nothing" for every
     keystroke the reader actually sent to the session. */
  const xtermInput = node("textarea");
  terminal.appendChild(xtermInput);
  activeElement = xtermInput;
  ctx.sync();
  check("...including the element xterm really focuses", ctx.state("s1"), "term");

  activeElement = termField;
  ctx.sync();
  check("the send-keys field is the session window too", ctx.state("s1"), "term");

  /* A control inside the row is not the row. The info button carries the
     same data-name, so a check that matched on the name would arm `k` while
     the keyboard was on a button that only opens a panel. */
  const info = descendants(row("s2")).find((k) => k.classes.has("sess-info"));
  activeElement = info;
  ctx.sync();
  check("a button inside the card is not the card", ctx.state("s2"), "");
  check("...and nothing on the row claims otherwise", chipText("s2"), "");

  activeElement = null;
  ctx.sync();
  check("the keyboard nowhere is the third answer, not one of the two",
        hidden(), [true, true, true]);

  /* focusout fires BEFORE the new element takes focus, so the document
     answers `body` there. Reading the incoming element off the event is
     what keeps the chip from blinking off between two cards. */
  docHandlers.focusout({ relatedTarget: row("s2") });
  check("focusout reads the incoming element, not the document",
        chipText("s2"), "⌨ card");

  /* ---------------------------------------------------------------- */
  /* the shortcut                                                      */
  /* ---------------------------------------------------------------- */
  calls.length = 0;
  row("s2").focus();
  ctx.sync();
  row("s2").handlers.keydown(ev("k", { target: row("s2") }));
  await Promise.resolve();
  check("k ends the card's session", kills().map((c) => c.url),
        ["/api/sessions/s2/kill"]);
  check("...which is not the attached one", ctx.state("s1"), "");
  check("...as a POST", kills().map((c) => c.options.method), ["POST"]);

  calls.length = 0;
  check("K is the same key", ctx.key(ev("K", { target: row("s1") }), "s1"), true);
  await Promise.resolve();
  check("...and posts for that card", kills().map((c) => c.url),
        ["/api/sessions/s1/kill"]);

  calls.length = 0;
  for (const mod of ["ctrlKey", "metaKey", "altKey"]) {
    ctx.key(ev("k", { target: row("s2"), [mod]: true }), "s2");
  }
  check("a modifier belongs to the browser", kills().length, 0);

  const inner = ev("k", { target: info, currentTarget: row("s2") });
  check("a press that reached a control inside the row is that control's",
        ctx.key(inner, "s2"), false);
  check("...and is not swallowed either", inner.prevented, 0);
  check("...and sends nothing", kills().length, 0);

  check("an exited record has nothing to end", ctx.kill("s3"), false);
  check("...so nothing is sent", kills().length, 0);
  check("...nor for a name the rail does not know", ctx.kill("s9"), false);

  const other = ev("j", { target: row("s1") });
  check("another key is not this row's", ctx.key(other, "s1"), false);
  check("...and keeps its default", other.prevented, 0);

  const enter = ev("Enter", { target: row("s2") });
  check("Enter opens the session, like the click", ctx.key(enter, "s2"), true);
  check("...by the same route", location.hash, "#/s/s2");
  check("...and is swallowed, or the page scrolls", enter.prevented, 1);
  ctx.key(ev(" ", { target: row("s3") }), "s3");
  check("Space does too", location.hash, "#/s/s3");
  check("...and none of that ended anything", kills().length, 0);

  /* ---------------------------------------------------------------- */
  /* the poll rebuilds the rows under the reader                       */
  /* ---------------------------------------------------------------- */
  row("s2").focus();
  ctx.sync();
  served = { sessions: [
    { name: "s1", status: "idle" },
    { name: "s2", status: "idle" },
    { name: "s3", status: "exited" },
  ] };
  await ctx.refresh();
  check("a rebuild really replaced the nodes", row("s2").focused, 1);
  check("the keyboard comes back to the same card", ctx.focused(), "s2");
  check("...and the chip with it", chipText("s2"), "⌨ card");
  check("...without scrolling the rail out from under the reader",
        row("s2").focusOpts, { preventScroll: true });
  check("a card that has gone is not forced back", ctx.restore("s9"), false);

  /* ---------------------------------------------------------------- */
  /* the stylesheet                                                    */
  /* ---------------------------------------------------------------- */
  check("the card state is drawn",
        /#session-list \.rail-keys\.on-card\s*\{[^}]*background:/s.test(css), true);
  check("the session state is drawn, and differently",
        /#session-list \.rail-keys\.on-term\s*\{[^}]*background:/s.test(css), true);
  check("the focused row is marked on the row itself",
        /#session-list li\.sess-card:focus\s*\{[^}]*box-shadow:/s.test(css), true);
  /* The ring replaces the browser's outline; dropping that outline without
     putting one back is how a rail of near-identical rows loses the one the
     reader's `k` is pointed at. */
  check("...and the default outline it replaces is gone",
        /#session-list li\.sess-card:focus\s*\{[^}]*outline:\s*none/s.test(css), true);

  if (failures) {
    console.error(`${failures} rail keyboard check(s) failed`);
    process.exit(1);
  }
  console.log("railkeys_check ok");
})();
