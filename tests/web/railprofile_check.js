/* What a rail row's meta line says: WHO the session runs as, then WHAT
   became of it, run against the real functions from app.js and the real
   refreshSessions against a stub DOM.

   The line used to draw either one. An exited row read "exit 1" — the
   profile that had run there was a fact the row had and chose not to say —
   and a borrowed token was never named on the rail at all (the detail
   panel's `borrow` row was the only place, and the list is what a person
   scans to tell twenty sessions apart). The checks here pin:

   - The identity. `s.profile || s.harness` was the old fallback; the
     PROFILE:HARNESS canonicalization (profile-display-canonical) means
     "codex:claude" reads as "codex/claude", and the harness is the fallback
     when no profile was selected.
   - The lender. A session running on a borrowed token says so on the row —
     "profile → lender", not the profile alone — because whose TOKEN it runs
     on is the fact that changes how its output reads.
   - The state joins, it does not replace. "exit N" and "winding down" sort
     AFTER the identity, and exited keeps its precedence over winding down.
   - The stylesheet caps the line (a rail row is ~260px, and "codex/claude
     → kimi · exit 1" would push the name off the other side), and the full
     text stays one hover away on the element's own title — the same
     recovery the ellipsised name has.
   - The meta stays ONE child of the row with class "meta": it is a fact
     now, not a new line (raillayout_check pins the row's child budget;
     this pins that the bigger fact did not cost a new one. */

const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);
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
const capLine = src.match(/^const RAIL_MESH_TAGS = .+$/m);
if (!capLine) throw new Error("cannot locate RAIL_MESH_TAGS in app.js");

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), dataset: {}, style: {},
    title: "", type: "",
    appendChild(c) { n.kids.push(c); return c; },
    append(...cs) { cs.forEach((c) => n.appendChild(c)); },
    addEventListener() {},
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
    querySelectorAll(sel) {
      const cls = sel.replace(/^\./, "");
      return descendants(this).filter((k) => k.classes.has(cls));
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
const document = { createElement: node };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const list = node("ul");
let served = { sessions: [] };
const api = async () => ({ ok: true, json: async () => served });

/* Everything refreshSessions leans on that is not this line. The context
   gauge, the directory line and the attention line are fixed (their wording
   is their own harnesses'); the rest are the same no-ops the other rail
   harnesses carry. */
const stubs = `
function sessionMatchesFilter() { return true; }
let sessionsCache = [], currentName = null, currentPage = "home";
let attachedPid = null, linkState = "down", sessName = null;
function railHeld() { return false; }
let railRedrawPending = false;
function forgetDeadSessions() {}
function refreshResumeChoices() {}
function refreshParentChoices() {}
function renderHome() {}
function syncBulkActions() {}
function syncMobileBars() {}
/* The terminal header's mesh-handle chip, which the session poll
   repaints (sesshandle_check's subject); here it is only a call that
   has to resolve — this harness draws no header. */
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
function railCwdLine(s) { return el("span", "rail-cwd"); }
function railSeenLine(s) { return el("span", "rail-seen"); }
function $(id) { return list; }
`;

const ctx = {};
new Function(
  "exports", "document", "el", "api", "list", "meshCache", "sessionGroupByMesh",
  stubs + capLine[0] + "\n"
  + slice("byLineage") + slice("sessionMeshGroup") + slice("sessMeshes") + slice("railMeshTags") + slice("sessHandles") + slice("handleTag")
  + slice("profileHarnessLabel") + slice("railMetaText")
  + slice("refreshSessions")
  + `
Object.assign(exports, {
  refresh: refreshSessions,
  text: railMetaText,
});`)(ctx, document, el, api, list, [], true);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* ---- the composition, one fact per case -------------------------------- */
check("an idle session says its profile/harness pair",
      ctx.text({ status: "idle", profile: "nc", harness: "claude" }), "nc/claude");
check("the harness is the fallback when no profile was chosen",
      ctx.text({ status: "idle", profile: "", harness: "claude" }), "claude");
check("a profile alone (no harness recorded) stands by itself",
      ctx.text({ status: "idle", profile: "nc", harness: "" }), "nc");
check("PROFILE:HARNESS is presented in the canonical PROFILE/HARNESS form",
      ctx.text({ status: "idle", profile: "codex:claude", harness: "claude" }),
      "codex/claude");
check("a borrowed token names the lender after the identity",
      ctx.text({ status: "idle", profile: "nc", harness: "claude", borrow: "kimi" }),
      "nc/claude → kimi");
check("the canonical form still applies under a borrow",
      ctx.text({ status: "idle", profile: "codex:claude", harness: "claude",
                 borrow: "kimi" }),
      "codex/claude → kimi");
check("a borrow with no profile of its own names only the lender",
      ctx.text({ status: "idle", profile: "", harness: "claude", borrow: "kimi" }),
      "claude → kimi");

/* ---- the state joins the identity; it never replaces it ---------------- */
check("an exited row says its identity AND its exit code",
      ctx.text({ status: "exited", exit_code: 1, profile: "nc", harness: "claude" }),
      "nc/claude · exit 1");
check("an exit code nobody recorded is said as unknown, not dropped",
      ctx.text({ status: "exited", exit_code: null, profile: "nc", harness: "claude" }),
      "nc/claude · exit ?");
check("a winding-down row keeps its identity too",
      ctx.text({ status: "busy", winddown: {}, profile: "nc", harness: "claude" }),
      "nc/claude · winding down");
check("exited keeps its precedence over winding down",
      ctx.text({ status: "exited", exit_code: 2, winddown: {},
                 profile: "nc", harness: "claude" }),
      "nc/claude · exit 2");
check("the two read in one order everywhere: identity · state",
      ctx.text({ status: "exited", exit_code: 1, profile: "nc",
                 harness: "claude", borrow: "kimi" }),
      "nc/claude → kimi · exit 1");
check("a session with neither profile nor harness says only its state",
      ctx.text({ status: "exited", exit_code: 3, profile: "", harness: "" }),
      "exit 3");

/* ---- on the row, built by the real code -------------------------------- */
served = { sessions: [
  { name: "s45", status: "exited", exit_code: 1, profile: "nc",
    harness: "claude", parent: null },
  { name: "s84", status: "busy", profile: "nc", harness: "claude",
    borrow: "kimi", parent: "s45" },
  { name: "s85", status: "idle", profile: "codex:claude", harness: "claude",
    parent: null },
  { name: "cn", status: "exited", exit_code: null, profile: "",
    harness: "claude", parent: null },
] };

(async () => {
  await ctx.refresh();
  const rows = list.kids.filter((r) => r.dataset.name);
  const row = (name) => rows.find((r) => r.dataset.name === name);
  const metaOf = (name) =>
    descendants(row(name)).find((k) => k.classes.has("meta"));

  check("every session still gets a row",
        rows.map((r) => r.dataset.name), ["s45", "s84", "s85", "cn"]);
  check("the exited row says identity · exit code",
        metaOf("s45").text, "nc/claude · exit 1");
  check("the borrowed row says lender, on the rail",
        metaOf("s84").text, "nc/claude → kimi");
  check("the canonicalized profile reads on the rail",
        metaOf("s85").text, "codex/claude");
  check("an unknown exit code reads as '?'",
        metaOf("cn").text, "claude · exit ?");
  /* The cap clips when the line runs long; the whole of it stays one hover
     away, the same recovery the ellipsised name gets from its title. */
  check("the full text rides the element's title, clipped or not",
        metaOf("s84").title, "nc/claude → kimi");
  check("...and so does an exited row's",
        metaOf("s45").title, "nc/claude · exit 1");
  /* One child of the row, with the class the rail has always drawn — the
     bigger fact did not cost the row a new line (raillayout_check pins the
     child budget). */
  check("the meta stays one child of the row",
        row("s84").kids.filter((k) => k.className === "meta").length, 1);
  check("the row's parts are unchanged (dot, name group, meta, the three deliberate lines, spawn +, ⓘ)",
        row("s84").kids.map((k) => k.className),
        ["dot busy", "rail-head", "meta", "rail-cwd", "rail-ctx-line unknown",
         "rail-seen", "sess-plus", "sess-info"]);

  /* ---- the stylesheet's part: a cap, not a new line -------------------- */
  const rule = (css.match(/#session-list \.meta \{([^}]*)\}/) || [])[1] || "";
  check("the line is capped so it cannot push the name off the row",
        /max-width/.test(rule), true);
  check("it never wraps",
        /white-space:\s*nowrap/.test(rule), true);
  check("it ellipsises rather than clips",
        /text-overflow:\s*ellipsis/.test(rule) && /overflow:\s*hidden/.test(rule),
        true);
  check("it is still not a full-width breaker",
        !/flex-basis:\s*100%/.test(rule), true);

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("railprofile_check: all checks passed");
})();
