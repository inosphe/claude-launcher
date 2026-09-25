/* The session detail is one node with two homes — the rail down the right of
   a wide screen, and the page slot on a phone — and which one it is in is
   decided in JS, not by a media query. That makes it exactly the kind of rule
   a stylesheet cannot be asked to prove: slice the real functions out of
   app.js, drive them against a stub DOM, and check where the node lands, when
   it is up, that opening it re-fits the terminal it just narrowed, and what
   closing it leaves behind. */
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
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

/* ---- stub DOM ---- */
function node(id) {
  const n = { id, kids: [], parentNode: null, _html: "", classes: new Set() };
  n.appendChild = (c) => {
    if (c.parentNode) c.parentNode.kids = c.parentNode.kids.filter((k) => k !== c);
    c.parentNode = n; n.kids.push(c); return c;
  };
  n.insertBefore = (c, ref) => {
    if (c.parentNode) c.parentNode.kids = c.parentNode.kids.filter((k) => k !== c);
    c.parentNode = n; n.kids.splice(n.kids.indexOf(ref), 0, c); return c;
  };
  Object.defineProperty(n, "innerHTML", {
    get() { return n._html; }, set(v) { n._html = v; },
  });
  n.classList = {
    add: (c) => n.classes.add(c),
    toggle: (c, on) => (on ? n.classes.add(c) : n.classes.delete(c)),
    contains: (c) => n.classes.has(c),
  };
  n.attrs = {};
  n.setAttribute = (k, v) => { n.attrs[k] = String(v); };
  n.getAttribute = (k) => (k in n.attrs ? n.attrs[k] : null);
  n.addEventListener = () => {};
  n.text = "";
  Object.defineProperty(n, "textContent", {
    get() { return n.text; }, set(v) { n.text = String(v); },
  });
  return n;
}
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.textContent = text;
  return n;
}
const kidsOf = (n, cls) => n.kids.filter((k) => k.classes.has(cls));
const ids = {};
for (const id of ["layout", "sidebar", "main", "mobile-bottom",
                  "detail-split", "sess-view"]) {
  ids[id] = node(id);
}
// as index.html declares them: the rail, the page slot, the phone's bottom
// bar — and the detail, with its resize handle beside it, sitting in the
// page slot until the layout moves the pair
ids.layout.appendChild(ids.sidebar);
ids.layout.appendChild(ids.main);
ids.layout.appendChild(ids["mobile-bottom"]);
ids.main.appendChild(ids["detail-split"]);
ids.main.appendChild(ids["sess-view"]);
const $ = (id) => ids[id] || (ids[id] = node(id));
const document = { querySelectorAll: () => [] };

let narrow = false;
const MOBILE_MQ = { get matches() { return narrow; } };
// the real one is "#terminal is not hidden and the phone's rail is not over
// it"; what the head cares about is only the answer
let termUp = true;
const terminalOnScreen = () => termUp;

/* The parts not under test, defined in the same scope as the sliced code so
   they share its state. showView and syncLayout mirror the real ones: both
   end in syncDetailPanel, and neither decides whether the detail is up — the
   router does, from the address. go() stands in for the browser: assigning
   the hash fires hashchange, which routes. attach() mirrors the real one only
   in what route() depends on — that it takes the terminal to the named
   session, through showView, and re-marks the detail button, which is how
   walking into the terminal an open panel already describes lights it. */
const stubs = `
let currentPage = "terminal", currentName = "coder2";
let sessName = null, sessPollTimer = null, sessStartBox = null, sessSendBox = null;
let sessRunFold = null, sessRunStops = 0;
function stopSessRun() { sessRunStops++; }
// The role panels' own state and poll: their content is rolepanel_check's,
// but dropping/repointing the panel has to take them down with it, so the
// hooks have to exist here too.
let sessQuickJobBox = null, sessKidsBox = null, sessKidsStops = 0;
function stopSessKids() { sessKidsStops++; }
let detailWasUp = false;
let refreshes = 0, gone = null, fits = 0;
let term = {}, location = { hash: "#/s/coder2" };
// One history entry per assignment, as the browser keeps them: a link
// followed is a new entry with no state; Back (goBack) returns to an entry
// the router has already stamped.
const history = {
  state: null,
  replaceState(st, _t, h) { this.state = st; if (h !== undefined) location.hash = h; },
};
function refreshSession() { refreshes++; }
function refitSoon() { fits++; }
function showView(name) {
  currentPage = name;
  syncLayout();
}
function syncLayout() { syncDetailPanel(); }
function go(hash) {
  gone = hash;
  if (location.hash === hash) { route(); return; }
  location.hash = hash; history.state = null; route();
}
function attach(name) {
  currentName = name; term = {}; showView("terminal"); markDetailRow();
}
// Walking back into a terminal nudges its socket, which is the link's affair
// and reconnect_check's to test; here it only has to exist.
function reconnectNow() {}
function stopWfPoll() {}
function stopMsgPoll() {}
function openTrace() {}
function stopMeshPoll() {}
function stopFlowPoll() {}
function closeWorkspaces() {}
function stopWindowPoll() {}
function stopBeadsPoll() {}
function stopReportsPoll() {}
function closeTranscript() {}
function openWorkflow() {}
function openMesh() {}
function openFlowTopology() {}
function openWorkspaces() {}
function openWindowPage() {}
function openReports() {}
function openHome() { showView("home"); }
function openFlows() { showView("flows"); }
function openBeads() { showView("beads"); }
function openSettings() { showView("settings"); }
function openCli() {}
function openTranscript() {}
function openNewSession() {}
/* The mesh handle a row/head wears when it differs from the session name
   (sesshandle_check's subject); here it is only a call that has to
   resolve. */
function handleTag(name) { return null; }
function refreshMeshList() {}
function refreshWorkflowChoices() {}
function refreshCflow() {}
`;

// The two pages that are objects rather than functions; route() calls them.
globalThis.ObserverPage = { open() {}, stop() {} };
globalThis.OperatorPanel = { open() {}, stop() {} };
const ctx = {};
new Function(
  "exports", "$", "document", "MOBILE_MQ", "setInterval", "clearInterval",
  "el", "terminalOnScreen",
  stubs +
  [slice("parseHash"), slice("hashQuery"), slice("hashDetail"),
   slice("hashWithDetail"), slice("syncDetailPanel"), slice("markDetailRow"),
   slice("dropDetail"), slice("openDetail"), slice("repointDetail"),
   slice("closeDetail"), slice("route"),
   // The head now carries the session's directory line (railcwd_check's
   // subject); here these are only calls that have to resolve.
   slice("shortenPath"), slice("cwdSplit"), slice("cwdShort"), slice("cwdLine"),
   slice("sessHead")].join("\n") +
  `
Object.assign(exports, {
  parseHash, syncDetailPanel, openDetail, closeDetail, dropDetail, showView,
  sessHead,
  page: () => currentPage, open: () => sessName, polls: () => refreshes,
  went: () => gone, fits: () => fits, cur: () => currentName,
  setPage: (p) => { currentPage = p; }, setCur: (c) => { currentName = c; },
  goto: (h) => { location.hash = h; history.state = null; route(); },
  goBack: (h) => { location.hash = h; history.state = { routed: true }; route(); },
  hash: () => location.hash,
  runFold: () => sessRunFold, runStops: () => sessRunStops,
  holdRun: () => { sessRunFold = "the open fold"; },
  kidsBox: () => sessKidsBox, kidsStops: () => sessKidsStops,
  holdKids: () => { sessKidsBox = "the children roster"; },
});`
)(ctx, $, document, MOBILE_MQ, () => 1, () => {}, el, terminalOnScreen);

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) return;
  failures++;
  console.log(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
};
const view = ids["sess-view"];
const split = ids["detail-split"];   // its resize handle, carried along
const where = () => (view.parentNode || {}).id;
const up = () => !view.classes.has("hidden");
const handleUp = () => !split.classes.has("hidden");

/* ---- routing: /info is gone ---- */
check("info link lands on the terminal",
      ctx.parseHash("#/s/coder2/info").page === "terminal" &&
      ctx.parseHash("#/s/coder2/info").name === "coder2",
      ctx.parseHash("#/s/coder2/info"));
check("plain session link still attaches",
      ctx.parseHash("#/s/coder2").page === "terminal");
check("the detail query is not part of the page",
      ctx.parseHash("#/operator?detail=coder2").page === "operator" &&
      ctx.parseHash("#/s/coder2?detail=coder3").name === "coder2",
      ctx.parseHash("#/s/coder2?detail=coder3"));
check("no session page in the router",
      !["#/s/a/info", "#/s/a", "#/", "#/flows"].some((h) => ctx.parseHash(h).page === "session"));

/* ---- wide: the right rail ---- */
narrow = false;
ctx.syncDetailPanel();
check("wide: it docks in #layout, not in a page", where() === "layout", where());
check("wide: to the right of #main, left of the phone bar — its resize handle between them",
      ids.layout.kids.map((k) => k.id).join(",") ===
        "sidebar,main,detail-split,sess-view,mobile-bottom",
      ids.layout.kids.map((k) => k.id));
check("wide: closed by default", !up());
check("wide: carries .docked", view.classes.has("docked"));
check("wide: the resize handle is down with it",
      !handleUp() && split.parentNode === ids.layout);

ctx.goto("#/s/coder2");
const fitsBefore = ctx.fits();
ctx.openDetail("coder2");
check("wide: opening writes the address", ctx.hash() === "#/s/coder2?detail=coder2", ctx.hash());
check("wide: opening does not change the page", ctx.page() === "terminal", ctx.page());
check("wide: the rail is up", up() && where() === "layout");
check("wide: it polls", ctx.polls() === 1, ctx.polls());
// it just took a column off #main, and no resize event says so
check("wide: opening re-fits the terminal", ctx.fits() === fitsBefore + 1);
check("wide: the handle is up beside it", handleUp() && split.parentNode === ids.layout);

const fitsOpen = ctx.fits();
ctx.openDetail("coder2");                      // the same button closes it
check("wide: the button toggles it off", !up() && ctx.open() === null);
check("wide: ...and takes it out of the address", ctx.hash() === "#/s/coder2", ctx.hash());
check("wide: closing gives the width back", ctx.fits() === fitsOpen + 1);
check("wide: the handle goes down with it", !handleUp());

/* ---- wide: the rail is the address's, on every page ---- */
/* It used to live only in memory: a link to another page (the Operator tab,
   say) left some session's rail docked beside that page, and a reload of the
   same address then showed no rail at all. Every page link carries no
   `detail`, so following one closes it; an address that carries one opens
   it, reload or not. */
for (const page of ["#/operator", "#/observer", "#/beads", "#/flows",
                    "#/settings", "#/", "#/wf/default|C:/w", "#/mesh/m1"]) {
  ctx.goto("#/s/coder2");
  ctx.openDetail("coder2");
  const fitsAt = ctx.fits();
  ctx.goto(page);
  check(`wide: a link to ${page} closes the rail`, !up() && ctx.open() === null,
        { open: ctx.open(), up: up() });
  check(`wide: ...and gives ${page} the width back`, ctx.fits() === fitsAt + 1);
}
for (const page of ["#/operator", "#/observer", "#/beads", "#/"]) {
  ctx.dropDetail();                            // a fresh load: nothing in memory
  ctx.goto(page + "?detail=coder3");
  check(`wide: ${page}?detail= opens the rail beside that page`,
        up() && ctx.open() === "coder3" && where() === "layout",
        { open: ctx.open(), up: up(), where: where() });
}
ctx.goto("#/operator?detail=coder3");
ctx.goBack("#/operator");                      // Back, from the ⓘ that opened it
check("wide: Back past the ⓘ closes it again", !up() && ctx.open() === null);
ctx.goto("#/s/coder2");
ctx.openDetail("coder2");
ctx.goBack("#/s/coder2");                      // ...and on the terminal it describes
check("wide: Back past the ⓘ on a terminal closes it too (it is not a terminal switch)",
      !up() && ctx.open() === null && ctx.hash() === "#/s/coder2",
      { open: ctx.open(), hash: ctx.hash() });
ctx.goto("#/?detail=coder3");                  // opened over home, then a rail row
ctx.goto("#/s/coder2");
check("wide: walking from a page into a terminal carries the open rail along",
      up() && ctx.open() === "coder2" && ctx.hash() === "#/s/coder2?detail=coder2",
      { open: ctx.open(), hash: ctx.hash() });
ctx.closeDetail();

/* ---- wide: the rail describes the session on screen ---- */
/* The rail is not in the URL, so nothing re-aims it when the terminal
   changes unless the router does. Left behind, it labels the session we came
   FROM — and its Workflow block then offers to open another session's run,
   which is exactly how one gets read as the other. */
ctx.setCur("coder2");
ctx.openDetail("coder2");
ctx.goto("#/s/coder3");
check("the terminal moved", ctx.cur() === "coder3", ctx.cur());
check("the open rail follows it", ctx.open() === "coder3", ctx.open());
check("...and the address says so, in place",
      ctx.hash() === "#/s/coder3?detail=coder3", ctx.hash());
check("and is still up", up() && where() === "layout");
check("re-aiming re-polls at once", ctx.polls() > 0);

ctx.goto("#/s/coder3");
check("re-entering the same terminal keeps it", ctx.open() === "coder3");
check("...in the address too", ctx.hash() === "#/s/coder3?detail=coder3", ctx.hash());

ctx.closeDetail();
ctx.goto("#/s/coder2");
check("a closed rail does not spring open", ctx.open() === null && !up(),
      { open: ctx.open(), up: up() });

/* ---- the run fold does not follow the panel to another session ---- */
/* It holds a poll aimed at one (directory, scope). Carried across a repoint it
   would keep reading the run we just navigated away from, under the new
   session's name. */
ctx.openDetail("coder2");
ctx.holdRun();
const stops = ctx.runStops();
ctx.openDetail("coder3");                      // repoint
check("re-aiming lets go of the open run fold", ctx.runFold() === null);
check("...and stops its poll", ctx.runStops() > stops);
ctx.holdRun();
ctx.closeDetail();
check("closing the panel does too",
      ctx.runFold() === null && ctx.runStops() > stops + 1);

/* ---- and neither does a leader's children roster ---- */
/* Same reason, one poll further: it is aimed at one session's subtree, and
   carried across a repoint it would list the previous leader's children
   under the new session's name. */
ctx.openDetail("coder2");
ctx.holdKids();
const kidStops = ctx.kidsStops();
ctx.openDetail("coder3");                      // repoint
check("re-aiming lets go of the children roster", ctx.kidsBox() === null);
check("...and stops its poll", ctx.kidsStops() > kidStops);
ctx.holdKids();
ctx.closeDetail();
check("closing the panel does too",
      ctx.kidsBox() === null && ctx.kidsStops() > kidStops + 1);
ctx.setCur("coder2");

/* ---- the header's `details` says whether it would close ---- */
/* Pressing it is a toggle for the session on screen and a re-aim for any
   other, and those look nothing alike to the user pressing it. Lit means the
   next press closes. */
const pressed = () => $("term-details").getAttribute("aria-pressed");
check("closed rail leaves the button unlit", pressed() === "false", pressed());
ctx.openDetail("coder2");
check("open on the terminal on screen lights it", pressed() === "true");
ctx.openDetail("coder3");                      // another row's ⓘ, terminal unmoved
check("open on some other session does not", pressed() === "false", pressed());
ctx.goto("#/s/coder3");                        // ...until we walk into it
check("walking into that terminal lights it", pressed() === "true", pressed());
ctx.closeDetail();
check("closing puts it out", pressed() === "false");
ctx.goto("#/s/coder2");
check("and it says which press it is offering",
      $("term-details").title.includes("metadata"), $("term-details").title);
ctx.openDetail("coder2");
check("...a close, once it is the panel's ×",
      $("term-details").title.includes("close"), $("term-details").title);
ctx.closeDetail();

/* ---- the panel head hands its × to that chip, but only there ---- */
/* Docked beside its own terminal, the chip is pinned over this head's corner
   and closes the panel: a × under a close button, and an "Open terminal" for
   the terminal already on screen, are both noise. Nowhere else. */
const headOf = (name) => ctx.sessHead({ name, status: "idle" });
narrow = false; termUp = true;
ctx.setCur("coder3");
let h = headOf("coder3");
check("docked beside its own terminal: no ×", kidsOf(h, "sess-close").length === 0);
check("...and no Open terminal that opens what is open",
      kidsOf(h, "wf-btn").length === 0);
check("...but still says which session", h.kids[0].text === "coder3");

h = headOf("s7");
// wf-btn is three verbs since the PR wizard joined the head: Spawn, Open PR,
// Open terminal. The × is its own class.
check("aimed elsewhere it keeps both ways out and the spawn and PR verbs",
      kidsOf(h, "sess-close").length === 1 && kidsOf(h, "wf-btn").length === 3);
check("...and says it is elsewhere", kidsOf(h, "sess-elsewhere").length === 1);

termUp = false;                                // a page is over the terminal
h = headOf("coder3");
check("with the terminal covered both come back — there is no chip on screen",
      kidsOf(h, "sess-close").length === 1 && kidsOf(h, "wf-btn").length === 3);

termUp = true; narrow = true;                  // and a phone has no header at all
h = headOf("coder3");
check("on a phone both stay", kidsOf(h, "sess-close").length === 1 &&
      kidsOf(h, "wf-btn").length === 3);
narrow = false;
ctx.setCur("coder2");

/* ---- narrow: the page slot ---- */
narrow = true;
ctx.goto("#/s/coder2");
ctx.openDetail("coder2");
check("narrow: it moves into the page slot", where() === "main", where());
check("narrow: and takes the page", ctx.page() === "session", ctx.page());
check("narrow: it is up", up());
check("narrow: drops .docked", !view.classes.has("docked"));
check("narrow: the handle is a page sign, not a column", !handleUp());

ctx.goto("#/");                                // leaving the page closes it
check("narrow: leaving the page closes it", ctx.open() === null && !up());

ctx.dropDetail();
ctx.goto("#/s/coder2?detail=coder2");         // a reload of the detail page
check("narrow: the address alone brings the page back",
      ctx.page() === "session" && ctx.open() === "coder2" && up(),
      { page: ctx.page(), open: ctx.open() });

ctx.goto("#/s/coder2");
ctx.openDetail("coder2");
ctx.goto("#/s/coder3");                        // ...and so does another terminal
check("narrow: a terminal takes the slot back rather than re-aiming the rail",
      ctx.open() === null && !up() && ctx.page() === "terminal",
      { open: ctx.open(), page: ctx.page() });
ctx.setCur("coder2");                          // that navigation really moved

ctx.goto("#/s/coder2");
ctx.openDetail("coder2");
ctx.closeDetail();
check("narrow: close hands the slot back", ctx.went() === "#/s/coder2", ctx.went());
check("...to the terminal", ctx.page() === "terminal", ctx.page());
ctx.goto("#/");
ctx.openDetail("coder2");
ctx.closeDetail();
check("narrow: opened over home, it closes back to home",
      ctx.went() === "#/" && ctx.page() === "home", { went: ctx.went(), page: ctx.page() });

/* ---- back to wide: the node moves, nothing is duplicated ---- */
narrow = false;
ctx.goto("#/s/coder2");
ctx.openDetail("coder2");
check("re-widening puts it back on the right, once",
      where() === "layout" && ids.main.kids.length === 0 &&
      ids.layout.kids.filter((k) => k.id === "sess-view").length === 1 &&
      ids.layout.kids.filter((k) => k.id === "detail-split").length === 1,
      { main: ids.main.kids.length, layout: ids.layout.kids.map((k) => k.id) });

console.log(failures ? `\n${failures} failure(s)` : "all panel checks passed");
process.exit(failures ? 1 : 0);
