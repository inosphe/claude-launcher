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
  "term-pause": node("button"), "term-archive": node("button"),
};
const $ = (id) => elements[id] || null;

let served = { sessions: [] };
/* Every request the sliced code makes, so the kill ones can be read apart
   from the poll's own -- the poll runs on the same `api` and would otherwise
   count as traffic in the checks below that assert nothing was sent. */
const calls = [];
const api = async (url, options) => {
  calls.push({ url, options });
  const verb = ["/kill", "/pause", "/archive"].find((v) => url.endsWith(v));
  return {
    ok: true, status: 200,
    json: async () => (verb
      ? { status: "exited", ...(verb === "/pause" ? { paused_at: "now" } : {}),
          ...(verb === "/archive" ? { archived_at: "now" } : {}) }
      : served),
  };
};
const kills = () => calls.filter((c) => c.url.includes("/kill"));
const posted = (verb) => calls.filter((c) => c.url.endsWith(verb)).map((c) => c.url);
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
/* Archiving a LIVE session ends it, so the page asks first. The stub
   records what was asked and answers with whatever the check set, which is
   how the refusal path ("the reader said no") is exercised at all. */
let confirms = [], confirmAnswer = true;
async function modalConfirm(title, body, label) {
  confirms.push({ title, body, label });
  return confirmAnswer;
}
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
/* The reader's own note line, stubbed for the same reason as the briefing
   decoration below: its wording and its stylesheet contract are
   railnote_check's subject, not this harness's. */
function decorateNoteRow(li, s) {}
function decorateBriefingRow(li, s) {}
function terminalOnScreen() { return false; }
function attach() {}
function setStatusBadge() {}
/* The three verbs the new card keys reach for, as the page's own functions
   see them. cflowAction is the one that is stubbed rather than sliced: the
   real one repaints two views this harness does not build, and what is
   being checked here is only which run the key aims the approval at. */
let cflowCache = [];
let approvals = [];
function cflowAction(path, body) { approvals.push({ path, body }); }
let pinPresses = [];
function toggleSessionPin(name) { pinPresses.push(name); }
function refreshWf() {}
function refreshCflow() {}
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
  + slice("answerFellToUs") + slice("answerBranchOptions")
  + slice("sessCflowRun")
  + keysBlock() + slice("killSession") + slice("pauseSession")
  + slice("archiveSession") + slice("refreshSessions")
  + `
Object.assign(exports, {
  refresh: refreshSessions,
  state: railKeysState,
  sync: syncRailKeys,
  focused: railFocusedCardName,
  restore: restoreRailFocus,
  key: railCardKey,
  kill: railCardKill,
  pause: railCardPause,
  archive: railCardArchive,
  approve: railCardApprove,
  bindings: RAIL_CARD_KEYS,
  chip: RAIL_KEYS_CHIP,
  attachTo: (name) => { currentName = name; },
  setSessions: (v) => { sessionsCache = v; },
  setCflow: (v) => { cflowCache = v; },
  approvals: () => approvals,
  pinPresses: () => pinPresses,
  confirms: () => confirms,
  answerConfirm: (v) => { confirmAnswer = v; },
  resetPresses: () => { approvals = []; pinPresses = []; confirms = []; },
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
  /* the other card verbs: pause, archive, approve, pin                */
  /* ---------------------------------------------------------------- */
  /* Each of these acts on the CARD, and each refuses on a state the route
     would refuse anyway -- the point of checking the refusals is that a key
     that posts and then raises a modal is worse than a key that does
     nothing, because the reader learns nothing about which key was wrong. */
  calls.length = 0;
  ctx.resetPresses();
  ctx.setSessions([
    { name: "s1", status: "busy" },
    { name: "s2", status: "idle" },
    { name: "s3", status: "exited" },
    { name: "s4", status: "exited", archived_at: "2026-09-01T00:00:00Z" },
  ]);

  check("p pauses the card's session",
        ctx.key(ev("p", { target: row("s2") }), "s2"), true);
  await Promise.resolve();
  check("...by the pause route, for that card",
        posted("/pause"), ["/api/sessions/s2/pause"]);
  check("...and ends nothing", kills().length, 0);
  check("an exited record has nothing to pause", ctx.pause("s3"), false);
  check("...nor a name the rail does not know", ctx.pause("s9"), false);
  check("...so nothing more was sent", posted("/pause").length, 1);

  calls.length = 0;
  check("e archives an exited record",
        ctx.key(ev("e", { target: row("s3") }), "s3"), true);
  await Promise.resolve();
  check("...by the archive route, for that card",
        posted("/archive"), ["/api/sessions/s3/archive"]);
  /* Let the requests above settle before reading the cache again: each of
     them refreshes the rail when it lands, and a refusal checked against a
     half-updated record would pass or fail on timing. */
  await new Promise((r) => setTimeout(r, 0));
  calls.length = 0;
  ctx.setSessions([
    { name: "s1", status: "busy" },
    { name: "s2", status: "idle" },
    { name: "s3", status: "exited" },
    { name: "s4", status: "exited", archived_at: "2026-09-01T00:00:00Z" },
  ]);
  /* A live session IS archivable — the route ends it and files it in one
     call — but ending a running program is not a keypress's to take
     silently, so the press asks first and only then sends. */
  ctx.resetPresses();
  check("a live session is archivable too", ctx.archive("s1"), true);
  await new Promise((r) => setTimeout(r, 0));
  check("...but it asks before ending it", ctx.confirms().length, 1);
  check("...naming the session in the question",
        ctx.confirms()[0].title.includes("s1"), true);
  check("...and only then sends", posted("/archive"), ["/api/sessions/s1/archive"]);

  calls.length = 0;
  ctx.resetPresses();
  ctx.answerConfirm(false);
  check("the press is still accepted when the reader may say no",
        ctx.archive("s2"), true);
  await new Promise((r) => setTimeout(r, 0));
  check("...but a refused confirmation sends nothing",
        posted("/archive").length, 0);
  ctx.answerConfirm(true);

  calls.length = 0;
  ctx.resetPresses();
  check("a record already archived has nowhere further to go",
        ctx.archive("s4"), false);
  check("...so nothing was asked and nothing was sent",
        [ctx.confirms().length, posted("/archive").length], [0, 0]);

  /* `a` is the one key that reads a second cache. A run stopped on an
     approval is the reader's press; a run stopped on a CHOICE is not, and
     picking an option for them is the failure this refusal exists for. */
  ctx.resetPresses();
  ctx.setCflow([
    { scope: "s1", cwd: "/w/s1", status: "running" },
    { scope: "s2", cwd: "/w/s2", status: "waiting_approval", sessions: ["s2"] },
    { scope: "s3", cwd: "/w/s3", status: "waiting_selection", sessions: ["s3"],
      options: [{ name: "go" }] },
  ]);
  check("a approves the card's waiting gate",
        ctx.key(ev("a", { target: row("s2") }), "s2"), true);
  check("...on that run, by cwd and scope", ctx.approvals(),
        [{ path: "/api/cflow/approve", body: { cwd: "/w/s2", scope: "s2" } }]);
  check("a run stopped on a choice is not a bare approval",
        ctx.approve("s3"), false);
  check("a running run has no gate to clear", ctx.approve("s1"), false);
  check("...nor has a card with no run at all", ctx.approve("s4"), false);
  check("...and none of those sent anything", ctx.approvals().length, 1);

  /* An ask that reached nobody is the reader's to approve -- unless it
     carries branch options, in which case it is a choice again. */
  ctx.resetPresses();
  ctx.setCflow([
    { scope: "s1", cwd: "/w/s1", status: "waiting_answer", sessions: ["s1"],
      ask: { asked: [] } },
  ]);
  check("an ask that reached nobody is approvable here", ctx.approve("s1"), true);
  ctx.setCflow([
    { scope: "s1", cwd: "/w/s1", status: "waiting_answer", sessions: ["s1"],
      reason: "branch", options: [{ name: "go" }] },
  ]);
  check("...but not when it is a branch", ctx.approve("s1"), false);
  ctx.setCflow([
    { scope: "s1", cwd: "/w/s1", status: "waiting_answer", sessions: ["s1"],
      ask: { asked: ["s469"] } },
  ]);
  check("...and not while it is genuinely with somebody else",
        ctx.approve("s1"), false);
  check("only the first of those was sent", ctx.approvals().length, 1);

  /* Pin moved off `p` when `p` became pause. The two must not both answer
     one key: the reader who meant to pin would end the session instead. */
  ctx.resetPresses();
  calls.length = 0;
  check("f pins the card", ctx.key(ev("f", { target: row("s2") }), "s2"), true);
  check("...that card", ctx.pinPresses(), ["s2"]);
  await Promise.resolve();
  check("...and sends no session verb to the daemon",
        [posted("/kill"), posted("/pause"), posted("/archive")].map((v) => v.length),
        [0, 0, 0]);

  check("every bound key is spelt once across the table",
        (() => {
          const seen = ctx.bindings.flatMap((b) => b.keys);
          return seen.length === new Set(seen).size;
        })(), true);
  check("the table binds exactly the documented set",
        ctx.bindings.map((b) => b.label),
        ["Enter / Space", "k", "p", "a", "e", "q", "f"]);
  check("the chip names each of them",
        ["k", "p", "a", "e", "q", "f"].every(
          (k) => new RegExp(`\\b${k} `).test(ctx.chip.card.title)), true);

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
  /* ---------------------------------------------------------------- */
  /* Settings > Help documents the same table                          */
  /* ---------------------------------------------------------------- */
  /* The card is built from RAIL_CARD_KEYS rather than a retyped list, so
     what this pins is that the section exists, is reachable from the
     Settings renderer, and has a stylesheet to draw it. */
  check("the help card is drawn from the binding table",
        /function keyHelpCard\(\)[\s\S]*?RAIL_CARD_KEYS\.map/.test(src), true);
  check("...and Settings appends it",
        /function renderWorkspaces\(\)[\s\S]*?keyHelpCard\(\)/.test(src), true);
  check("...under a heading that says Help",
        /keyHelpCard[\s\S]*?"Help — keyboard shortcuts"/.test(src), true);
  check("the help rows have a rule to draw them",
        /\.key-help-row\s*\{[^}]*display:/s.test(css), true);
  check("...and the key itself is set apart",
        /\.key-help-key\s*\{[^}]*border:/s.test(css), true);

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
