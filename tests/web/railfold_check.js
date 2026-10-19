/* Exited rows fold away at the foot of the rail (claunch-4thlk.6) -- run
   against the real railFoldExited and its helpers, and the real
   refreshSessions over a stub DOM that moves nodes the way a browser does.

   On the measured machine 144 of the rail's 165 rows were exited sessions,
   each a full card rebuilt into the DOM. What has to hold: by default the
   exited rows are not drawn and one line says how many there are; opening
   the line draws them, and the browser remembers it; storage that throws
   reads as folded; a live row is never folded, nor an exited one that is
   open in the terminal or the detail panel, nor one a live session descends
   from; a search or a state filter without "running" folds nothing; and
   the counts taken from the row list do not change with the fold. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  process.env.RAILFOLD_APP_JS || path.join(
    __dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
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
function lines(re, what) {
  const m = src.match(re);
  if (!m) throw new Error(`cannot locate ${what} in app.js`);
  return m[0] + "\n";
}

function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), dataset: {}, style: {}, attrs: {},
    title: "", parentNode: null, listeners: {},
    get childNodes() { return n.kids; },
    appendChild(c) { return n.insertBefore(c, null); },
    insertBefore(c, ref) {
      if (c.parentNode) c.parentNode.removeChild(c);
      const at = ref ? n.kids.indexOf(ref) : -1;
      if (at < 0) n.kids.push(c); else n.kids.splice(at, 0, c);
      c.parentNode = n;
      return c;
    },
    removeChild(c) {
      const at = n.kids.indexOf(c);
      if (at >= 0) n.kids.splice(at, 1);
      c.parentNode = null;
      return c;
    },
    append(...cs) { cs.forEach((c) => n.appendChild(c)); },
    setAttribute(k, v) { n.attrs[k] = String(v); },
    addEventListener(type, fn) { n.listeners[type] = fn; },
    querySelector(sel) {
      const m = sel.match(/^li\[data-name="(.+)"\]$/);
      for (const k of n.kids) {
        if (m && k.dataset && k.dataset.name === m[1]) return k;
        const deep = k.querySelector ? k.querySelector(sel) : null;
        if (deep) return deep;
      }
      return null;
    },
    querySelectorAll() { return []; },
    get textContent() { return n.text; },
    set textContent(v) { n.text = String(v); },
    get className() { return [...n.classes].join(" "); },
    set className(v) { n.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get innerHTML() { return ""; },
    set innerHTML(v) { n.kids.slice().forEach((c) => n.removeChild(c)); },
  };
  n.classList = {
    add: (...cs) => cs.forEach((c) => n.classes.add(c)),
    remove: (...cs) => cs.forEach((c) => n.classes.delete(c)),
    contains: (c) => n.classes.has(c),
    toggle: (c, on) => (on ? n.classes.add(c) : n.classes.delete(c)),
  };
  return n;
}
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const stubs = `
function sessionMatchesFilter() { return true; }
let sessionsCache = [], currentName = null, currentPage = "home";
let attachedPid = null, linkState = "down", sessName = null, briefingLLM = true, snapshotName = null;
let sessionFilter = ["running", "hold", "delayed", "failed", "killed", "paused"];
let sessionSearch = { q: "" };
const BASE = "";
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
function decorateNoteRow() {}
function decorateProgressRow() {}
function decorateProcsRow() {}
function decorateBriefingRow() {}
function terminalOnScreen() { return false; }
function attach() {}
function setStatusBadge() {}
function ctxNoteOnRow() {}
function ctxRailLine() { return el("span", "rail-ctx-line unknown"); }
function railCwdLine() { return el("span", "rail-cwd"); }
function railSeenLine() { return el("span", "rail-seen"); }
function railMeshTags() { return []; }
function handleTag() { return null; }
function sessMeshes() { return []; }
function openSpawnModal() {}
function openDetail() {}
function $(id) { return list; }
`;

const RAIL_LINES = lines(/^const RAIL_HOLD_MS = .+$/m, "RAIL_HOLD_MS")
  + lines(/^let railHeldUntil = .+$/m, "railHeldUntil")
  + lines(/^let railRedrawPending = .+$/m, "railRedrawPending")
  + lines(/^const RAIL_EXITED_OPEN_KEY = .+$/m, "RAIL_EXITED_OPEN_KEY")
  + lines(/^let railExitedOpen = \(\(\) => \{[\s\S]+?^\}\)\(\);$/m, "railExitedOpen");

function page(storage, served) {
  const list = node("ul");
  const ctx = { list, redraws: 0 };
  new Function(
    "exports", "document", "el", "api", "list", "setTimeout", "meshCache",
    "sessionGroupByMesh", "localStorage", "CSS",
    stubs + RAIL_LINES
    + slice("railHeld")
    + slice("byLineage") + slice("sessionMeshGroup") + slice("profileHarnessLabel")
    + slice("railMetaText") + slice("regroupDepths") + slice("sessionGroupRows")
    + slice("railExitedFolds") + slice("railExitedShown") + slice("railExitedFoldState")
    + slice("railFoldExited") + slice("toggleRailExitedFold") + slice("railRevealFolded")
    + slice("refreshSessions")
    + `
const realRefresh = refreshSessions;
refreshSessions = (opts) => { exports.redraws++; return realRefresh(opts); };
Object.assign(exports, {
  refresh: (opts) => realRefresh(opts),
  fold: railFoldExited,
  toggle: toggleRailExitedFold,
  reveal: railRevealFolded,
  open: () => railExitedOpen,
  set: (k, v) => {
    if (k === "currentName") currentName = v;
    if (k === "sessName") sessName = v;
    if (k === "sessionFilter") sessionFilter = v;
    if (k === "sessionSearch") sessionSearch = v;
  },
});`)(ctx, { createElement: node, createDocumentFragment: () => node("#fragment") },
       el, async () => ({ ok: true, json: async () => served() }), list,
       setTimeout, [], false, storage, { escape: (s) => s });
  return ctx;
}

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

function memoryStorage() {
  const m = new Map();
  return {
    m,
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
  };
}
const throwing = {
  getItem() { throw new Error("denied"); },
  setItem() { throw new Error("denied"); },
  removeItem() { throw new Error("denied"); },
};

const S = (name, status, parent) => ({ name, status, parent, harness: "claude", cwd: "" });
const FLEET = [
  S("live1", "idle"),
  S("dead1", "exited"),
  S("deadparent", "exited"),
  S("livechild", "busy", "deadparent"),
  S("dead2", "exited"),
  S("deadkid", "exited", "dead2"),
];

(async () => {
  /* ---- the row list ---------------------------------------------------- */
  const store = memoryStorage();
  const p = page(store, () => ({ sessions: FLEET, llm_configured: true }));
  const items = (fleet) => fleet.map((session) => ({
    type: "session", session, depth: session.parent ? 1 : 0, meshGroup: null, kin: false }));
  const out = p.fold(items(FLEET));
  const shape = (rows) => rows.map((r) => r.type === "fold"
    ? `fold:${r.count}:${r.open}` : `${r.session.name}${r.folded ? "*" : ""}:${r.depth}`);
  check("exited rows move behind one fold line, folded by default",
        shape(out),
        ["live1:0", "deadparent:0", "livechild:1",
         "fold:3:false", "dead1*:0", "dead2*:0", "deadkid*:1"]);
  check("...and every session stays in the list the counts read",
        out.filter((r) => r.type === "session").length, FLEET.length);
  check("a fleet with nothing exited gets no fold line",
        shape(p.fold(items([S("a", "idle"), S("b", "busy")]))), ["a:0", "b:0"]);

  p.set("currentName", "dead1");
  p.set("sessName", "dead2");
  check("the sessions open in the terminal and the detail panel stay in place",
        shape(p.fold(items(FLEET))),
        ["live1:0", "dead1:0", "deadparent:0", "livechild:1", "dead2:0",
         "fold:1:false", "deadkid*:0"]);
  p.set("currentName", null);
  p.set("sessName", null);

  p.set("sessionSearch", { q: "dead" });
  check("a search folds nothing", shape(p.fold(items(FLEET))).includes("fold:3:false"), false);
  p.set("sessionSearch", { q: "  " });
  check("...a blank one is no search", shape(p.fold(items(FLEET))).includes("fold:3:false"), true);
  p.set("sessionSearch", { q: "" });
  p.set("sessionFilter", ["killed", "paused"]);
  check("a filter that picked the exited rows folds nothing",
        shape(p.fold(items(FLEET))).includes("fold:3:false"), false);
  p.set("sessionFilter", ["running", "killed"]);

  /* ---- the drawn rail -------------------------------------------------- */
  const drawn = () => p.list.kids.map((li) => li.classes.has("rail-fold")
    ? `fold ${li.textContent}` : li.dataset.name);
  await p.refresh({ state: "current" });
  check("folded rows are not drawn; the fold line says how many",
        drawn(), ["live1", "deadparent", "livechild", "fold ▸ exited · 3"]);
  const line = p.list.kids.find((li) => li.classes.has("rail-fold"));
  check("...and is a button that says it is closed",
        [line.attrs.role, line.attrs["aria-expanded"], line.tabIndex], ["button", "false", 0]);

  line.listeners.click();
  await new Promise((r) => setTimeout(r, 0));
  check("opening it draws the exited rows after the line",
        drawn(), ["live1", "deadparent", "livechild", "fold ▾ exited · 3",
                  "dead1", "dead2", "deadkid"]);
  check("...and the browser remembers it", store.m.get("claunch_rail_exited_open:"), "1");
  check("a page loaded with that remembered starts open",
        page(store, () => ({ sessions: [] })).open(), true);

  p.toggle();
  await new Promise((r) => setTimeout(r, 0));
  check("closing it takes them out again",
        drawn(), ["live1", "deadparent", "livechild", "fold ▸ exited · 3"]);
  check("...and forgets the open choice", store.m.has("claunch_rail_exited_open:"), false);

  /* A pick of a folded session (a tab, the address) draws its row. */
  p.set("currentName", "dead1");
  const before = p.redraws;
  p.reveal("dead1");
  await new Promise((r) => setTimeout(r, 0));
  check("picking a folded session redraws the rail", p.redraws, before + 1);
  check("...with its row out of the fold",
        drawn(), ["live1", "dead1", "deadparent", "livechild", "fold ▸ exited · 2"]);
  p.reveal("live1");
  check("picking a drawn row redraws nothing", p.redraws, before + 1);

  /* ---- storage that throws -------------------------------------------- */
  let q;
  try {
    q = page(throwing, () => ({ sessions: FLEET, llm_configured: true }));
  } catch (err) {
    q = null;
    check("a page whose storage throws still loads", String(err), "");
  }
  if (q) {
    check("storage that throws reads as folded", q.open(), false);
    let threw = null;
    try { q.toggle(); } catch (err) { threw = String(err); }
    check("...and the line still opens, for this page", [threw, q.open()], [null, true]);
  }

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("railfold_check: ok");
})();
