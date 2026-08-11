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
  return n;
}
const ids = {};
for (const id of ["layout", "sidebar", "main", "mobile-bottom", "sess-view"]) {
  ids[id] = node(id);
}
// as index.html declares them: the rail, the page slot, the phone's bottom
// bar — and the detail sitting in the page slot until the layout moves it
ids.layout.appendChild(ids.sidebar);
ids.layout.appendChild(ids.main);
ids.layout.appendChild(ids["mobile-bottom"]);
ids.main.appendChild(ids["sess-view"]);
const $ = (id) => ids[id] || (ids[id] = node(id));
const document = { querySelectorAll: () => [] };

let narrow = false;
const MOBILE_MQ = { get matches() { return narrow; } };

/* The parts not under test, defined in the same scope as the sliced code so
   they share its state. showView and syncLayout mirror the real ones: both
   end in syncDetailPanel, and showView drops the detail when a phone
   navigates off the page it was borrowing. */
const stubs = `
let currentPage = "terminal", currentName = "coder2";
let sessName = null, sessPollTimer = null, sessStartBox = null;
let detailWasUp = false;
let refreshes = 0, gone = null, fits = 0;
function refreshSession() { refreshes++; }
function refitSoon() { fits++; }
function showView(name) {
  currentPage = name;
  if (name !== "session" && MOBILE_MQ.matches) dropDetail();
  syncLayout();
}
function syncLayout() { syncDetailPanel(); }
function go(hash) { gone = hash; }
`;

const ctx = {};
new Function(
  "exports", "$", "document", "MOBILE_MQ", "setInterval", "clearInterval",
  stubs +
  [slice("parseHash"), slice("syncDetailPanel"), slice("markDetailRow"),
   slice("dropDetail"), slice("openDetail"), slice("closeDetail")].join("\n") +
  `
Object.assign(exports, {
  parseHash, syncDetailPanel, openDetail, closeDetail, dropDetail, showView,
  page: () => currentPage, open: () => sessName, polls: () => refreshes,
  went: () => gone, fits: () => fits,
  setPage: (p) => { currentPage = p; }, setCur: (c) => { currentName = c; },
});`
)(ctx, $, document, MOBILE_MQ, () => 1, () => {});

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) return;
  failures++;
  console.log(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
};
const view = ids["sess-view"];
const where = () => (view.parentNode || {}).id;
const up = () => !view.classes.has("hidden");

/* ---- routing: /info is gone ---- */
check("info link lands on the terminal",
      ctx.parseHash("#/s/coder2/info").page === "terminal" &&
      ctx.parseHash("#/s/coder2/info").name === "coder2",
      ctx.parseHash("#/s/coder2/info"));
check("plain session link still attaches",
      ctx.parseHash("#/s/coder2").page === "terminal");
check("no session page in the router",
      !["#/s/a/info", "#/s/a", "#/", "#/flows"].some((h) => ctx.parseHash(h).page === "session"));

/* ---- wide: the right rail ---- */
narrow = false;
ctx.syncDetailPanel();
check("wide: it docks in #layout, not in a page", where() === "layout", where());
check("wide: to the right of #main, left of the phone bar",
      ids.layout.kids.map((k) => k.id).join(",") ===
        "sidebar,main,sess-view,mobile-bottom",
      ids.layout.kids.map((k) => k.id));
check("wide: closed by default", !up());
check("wide: carries .docked", view.classes.has("docked"));

const fitsBefore = ctx.fits();
ctx.openDetail("coder2");
check("wide: opening does not change the page", ctx.page() === "terminal", ctx.page());
check("wide: the rail is up", up() && where() === "layout");
check("wide: it polls", ctx.polls() === 1, ctx.polls());
// it just took a column off #main, and no resize event says so
check("wide: opening re-fits the terminal", ctx.fits() === fitsBefore + 1);

ctx.setPage("flows"); ctx.syncDetailPanel();   // navigating away must not close it
check("wide: survives navigation", up() && ctx.open() === "coder2");
check("wide: and does not re-fit for nothing", ctx.fits() === fitsBefore + 1);
ctx.setPage("terminal");

const fitsOpen = ctx.fits();
ctx.openDetail("coder2");                      // the same button closes it
check("wide: the button toggles it off", !up() && ctx.open() === null);
check("wide: closing gives the width back", ctx.fits() === fitsOpen + 1);

/* ---- narrow: the page slot ---- */
narrow = true;
ctx.openDetail("coder2");
check("narrow: it moves into the page slot", where() === "main", where());
check("narrow: and takes the page", ctx.page() === "session", ctx.page());
check("narrow: it is up", up());
check("narrow: drops .docked", !view.classes.has("docked"));

ctx.showView("home");                          // leaving the page closes it
check("narrow: leaving the page closes it", ctx.open() === null && !up());

ctx.openDetail("coder2");
ctx.closeDetail();
check("narrow: close hands the slot back", ctx.went() === "#/s/coder2", ctx.went());
ctx.setCur(null);
ctx.openDetail("coder2");
ctx.closeDetail();
check("narrow: with nothing attached it falls back to home",
      ctx.went() === "#/", ctx.went());

/* ---- back to wide: the node moves, nothing is duplicated ---- */
narrow = false;
ctx.setCur("coder2");
ctx.openDetail("coder2");
check("re-widening puts it back on the right, once",
      where() === "layout" && ids.main.kids.length === 0 &&
      ids.layout.kids.filter((k) => k.id === "sess-view").length === 1,
      { main: ids.main.kids.length, layout: ids.layout.kids.map((k) => k.id) });

console.log(failures ? `\n${failures} failure(s)` : "all panel checks passed");
process.exit(failures ? 1 : 0);
