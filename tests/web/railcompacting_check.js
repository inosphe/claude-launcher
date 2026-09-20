/* The rail card's compaction label, run against the real refreshSessions
   from app.js against a stub DOM.

   The label is the display half of daemon/compacting.py: the daemon turns a
   live compaction paint (notice + progress bar) into a `compacting` flag on
   /api/sessions, and the rail row buys it a pill beside the role. This check
   pins the three things that make the pill worth drawing:

   - A session whose poll says `compacting: true` gets a pill whose class is
     `rail-compacting` and whose text is "compacting" — spelling is the
     contract, a reader matches it by eye.
   - The pill sits where it was placed: after the role, before the rooms.
     Identity reads left, state after, belonging last — the same order the
     row's other tags keep.
   - A session without the flag — false or key absent — gets no pill at all,
     and nothing about its row changes: the flag is additive.
   - The tooltip says what the mechanism does: the sticky window keeps the
     label up for a bit after the notice leaves, so the sentence says "or has
     just finished" rather than "right now".
   - The stylesheet owns a rule for the pill (it is not left to the default
     span): rigid, so "compacting" is never half-shown against the ellipsis.
*/

const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  process.env.RAILCOMPACTING_APP_JS || path.join(
    __dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
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
    querySelector() { return null; },
    querySelectorAll() { return []; },
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

/* Everything refreshSessions leans on that is not this pill. The context
   gauge, the directory line and the attention line are other checks'
   subjects and stubbed here; the rest are the same no-ops the other rail
   harnesses carry (railcwd_check.js). */
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
function applyCflowBadges() {}
function applyGotoFlash() {}
function applyRailQuiet() {}
function applyBriefingCards() {}
/* The reader's own note line, stubbed for the same reason as the briefing
   decoration below: its wording and its stylesheet contract are
   railnote_check's subject, not this harness's. */
function decorateNoteRow(li, s) {}
function decorateBriefingRow(li, s) {}
/* s248's handle pill and header chip are another session's harness subject
   (sesshandle_check), not this one's — no-op them the way the other
   out-of-scope lines are. */
function handleTag(s) { return null; }
function renderTermHandle() {}
function terminalOnScreen() { return false; }
function attach() {}
function setStatusBadge() {}
function ctxNoteOnRow() {}
function ctxRailLine(s) { return el("span", "rail-ctx-line unknown"); }
function railSeenLine(s) { return el("span", "rail-seen"); }
function railCwdLine(s) { return el("span", "rail-cwd"); }
function $(id) { return list; }
function openSpawnModal() {}
`;

const ctx = {};
const meshCache = [
  { name: "mesh-a", members: [{ local: true, session: "comp", handle: "c" }] },
];
new Function(
  "exports", "document", "el", "api", "list", "meshCache", "sessionGroupByMesh",
  stubs + capLine[0] + "\n"
  + slice("byLineage") + slice("sessionMeshGroup") + slice("sessMeshes") + slice("railMeshTags")
  + slice("profileHarnessLabel") + slice("railMetaText") + slice("refreshSessions")
  + `
Object.assign(exports, { refresh: refreshSessions });`
)(ctx, document, el, api, list, meshCache, false);
/* Mesh grouping off: this harness pins where the compacting pill sits
   relative to the rooms, and a grouped row drops the pill for its own
   heading's mesh, which can leave no rooms to sit before. */

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

(async () => {
  function headOf(row) {
    /* The rail-head is a span whose className is exactly "rail-head". */
    return row.kids.find((k) => k.className === "rail-head");
  }

  /* ---- the flag draws the pill, next to the role ----------------------- */
  served = {
    sessions: [
      { name: "comp", harness: "claude", profile: "p", status: "busy",
        compacting: true, role: "worker" },
      { name: "falsey", harness: "claude", profile: "p", status: "busy",
        compacting: false, role: "worker" },
      { name: "absent", harness: "claude", profile: "p", status: "idle",
        role: "worker" },
    ],
  };
  await ctx.refresh();
  const rows = new Map(list.kids.map((r) => [r.dataset.name, r]));

  const compHead = headOf(rows.get("comp"));
  check("the compacting session has exactly one rail-compacting pill",
        descendants(compHead).filter((k) => k.className === "rail-compacting").length,
        1);
  const pill = descendants(compHead).find((k) => k.className === "rail-compacting");
  check("the pill is labelled 'compacting'", pill.text, "compacting");
  check("the pill sits after the role", true,
        compHead.kids.findIndex((k) => k.className === "mesh-role") <
        compHead.kids.findIndex((k) => k.className === "rail-compacting"));
  check("the pill sits before the rooms", true,
        compHead.kids.findIndex((k) => k.className === "rail-compacting") <
        compHead.kids.findIndex((k) => k.className === "rail-meshes"));
  check("the pill explains the sticky window in its tooltip", true,
        /just finished/.test(pill.title) && !/right now/.test(pill.title));

  for (const name of ["falsey", "absent"]) {
    check(`the ${name} session draws no pill`,
          descendants(headOf(rows.get(name)))
            .filter((k) => k.className === "rail-compacting").length, 0);
  }

  /* ---- the stylesheet gives the pill a rule of its own ----------------- */
  const rule = (css.match(/#session-list \.rail-compacting \{([^}]*)\}/) || [])[1] || "";
  check("the pill has its own rule", rule.length > 0, true);
  check("...rigid, so it is never half-shown", /flex:\s*none/.test(rule), true);
  check("...and purple, the transient-state colour", /#a371f7/.test(rule), true);

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("railcompacting_check: all checks passed");
})();
