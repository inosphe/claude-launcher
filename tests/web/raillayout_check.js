/* How much vertical room one session costs on the rail.

   The rail carries every live session at once, so a row that grows a line
   costs twenty. It grew three: the mesh tags took a line of their own, and
   because a full-width child at the default `order: 0` breaks the flex line
   before the ▸ toggle (`order: 1`) is reached, the toggle took a third.

   What has to hold: the name, the role it holds and the rooms it is in are
   ONE group in ONE container, so they share a line and shrink instead of
   wrapping; nothing that belongs on that line declares itself full-width; the
   only full-width children are the deliberate lines below — the buttons line
   (.rail-actions, claunch-ku033) first, then the sub-lines — each sorting at
   or after the toggle's order; and the tags still say which rooms,
   since a pill that can shrink to an empty capsule has lost the fact it was
   drawn for.

   The real refreshSessions builds the rows here, against a stub DOM. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  const head = src.lastIndexOf("async ", start) === start - 6 ? start - 6 : start;
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error("unbalanced " + name);
}
const capLine = src.match(/^const RAIL_MESH_TAGS = .+$/m);
if (!capLine) throw new Error("cannot locate RAIL_MESH_TAGS in app.js");
/* The card-density block: which role families exist, which of them draw as a
   one-line card, and the decision itself. Sliced whole (the Map's `for` loop
   fills it) because the row builder's `data-rail-mode` comes from it, and
   railcardmode_check pins what it decides — here it only has to run. */
const densitySrc = [
  src.match(/^const RAIL_ROLE_FAMILIES = new Map\(\);[\s\S]*?^}$/m),
  src.match(/^const RAIL_MIN_CARD_FAMILIES = .+$/m),
].map((m) => {
  if (!m) throw new Error("cannot locate the rail card-density constants");
  return m[0];
}).join("\n");

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}
function ok(what, cond, detail) {
  if (!cond) { console.error(`FAIL ${what}${detail ? "\n  " + detail : ""}`); failures++; }
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), dataset: {}, style: {},
    title: "", type: "", parent: null,
    appendChild(c) { c.parent = this; this.kids.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.appendChild(c)); },
    addEventListener() {},
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
    querySelectorAll(sel) {
      const cls = sel.replace(/^\./, "");
      return walk(this).filter((k) => k.classes.has(cls));
    },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) {
      this.classes = new Set(String(v).split(/\s+/).filter(Boolean));
    },
    get innerHTML() { return ""; },
    set innerHTML(v) { this.kids = []; },
  };
  n.classList = {
    add: (...cs) => cs.forEach((c) => n.classes.add(c)),
    contains: (c) => n.classes.has(c),
  };
  return n;
}
function walk(n, out = []) {
  for (const k of n.kids) { out.push(k); walk(k, out); }
  return out;
}
const document = { createElement: (tag) => node(tag) };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}

const list = node("ul");
let served = { sessions: [] };
const api = async () => ({ ok: true, json: async () => served });

/* Everything refreshSessions leans on that is not the row itself. */
const stubs = `
/* The rail filter is REAL here (sliced in below), not a stub that always
   says yes. refreshSessions passes it to Array.filter, which hands a
   callback the index as its second argument — so a stub that ignores its
   arguments cannot see the filter being fed an index in place of the
   rail's filter name. That mistake empties the visible set, and an empty
   visible set flattens every row to depth 0, which is what the indent
   assertions below exist to catch. Only the filter name is stubbed. */
let sessionFilter = "current";
let sessionsCache = [], currentName = null, currentPage = "home";
let attachedPid = null, linkState = "down", sessName = null, snapshotName = null;
// refreshSessions prunes the keep-alive cache of vanished sessions; the
// cache and its disposer are stubs here (the build owns no terminals).
let keptTerms = new Map();
function dropKept() {}
/* The poll's sweep of everything keyed by a session name that has gone
   away. It has its own harness (leak_check, which counts what a tab left
   open all day is still holding); here it is a no-op so the rail stays
   the subject. */
/* The rail hold's state, which refreshSessions now consults before it tears
   the rows down (app.js railHeld). Nothing here is about a press, so the
   answer is always "no press in flight" and the rebuild happens as before —
   railhold_check is the harness that drives the held case. */
function railHeld() { return false; }
let railRedrawPending = false;
function forgetDeadSessions() {}
function refreshResumeChoices() {}
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
function terminalOnScreen() { return false; }
function attach() {}
function setStatusBadge() {}
function $(id) { return list; }
function refreshParentChoices() {}
/* Not a no-op, because what it does touches what this harness pins. The real
   one hangs a context note on the row AND on the name, joining whatever title
   was already there rather than replacing it — so a name clipped to an
   ellipsis can still say what it was. The note is fixed here (the real one
   reads s.context) because the shape is the part that matters: a title may
   grow lines, and the name has to stay the first of them. */
function ctxNoteOnRow(row, name, s) {
  const note = "context not known yet — no reading recorded";
  const join = (had) => [had, note].filter(Boolean).join("\\n");
  if (row) row.title = join(row.title);
  if (name) name.title = join(name.title);
}
/* The context gauge line the real code appends to each row. Fixed here like
   the note above: this harness pins the row's shape (that the line is a
   deliberate full-width line-breaker between the name line and the cflow
   line), not what it says — the reading's wording belongs to railctx_check.
   Returned for every session, since the harness's sessions carry no harness
   field and so are all "claude" to ctxKnowable. */
function ctxRailLine(s) { return el("span", "rail-ctx-line unknown"); }
/* The directory line, fixed the same way: one more deliberate full-width
   breaker under the name (its wording is railcwd_check's). */
function railCwdLine(s) { return el("span", "rail-cwd"); }
/* And the attention line — who has been near this session — fixed the same
   way again: a third always-on full-width breaker under the name, whose
   three readings are railseen_check's subject and not this harness's. */
function railSeenLine(s) { return el("span", "rail-seen"); }
/* The briefing's per-row decoration (the one-line and the collapsed ⟳) is
   its own harness (briefrow_check); here, like applyBriefingCards, it is a
   no-op so what this harness pins — the row's own parts — stays about the
   name-line layout. The one-line's stylesheet contract is still asserted
   below (it is one of the deliberate full-width breakers). */
/* The reader's own note line, stubbed for the same reason as the briefing
   decoration below: its wording and its stylesheet contract are
   railnote_check's subject, not this harness's. */
function decorateNoteRow(li, s) {}
function decorateProgressRow(li, s) {}
function decorateProcsRow(li, s) {}
function decorateBriefingRow(li, s) {}
`;

const ctx = {};
new Function(
  "exports", "document", "el", "api", "list", "meshCache", "sessionGroupByMesh",
  stubs + capLine[0] + "\n" + densitySrc + "\n"
  + slice("sessionCategory") + slice("sessionMatchesFilter")
  + slice("byLineage") + slice("sessionMeshGroup") + slice("sessMeshes") + slice("railMeshTags") + slice("sessHandles") + slice("handleTag")
  + slice("profileHarnessLabel") + slice("railMetaText")
  + slice("railRoleFamily") + slice("railSessionRole") + slice("railCardMode")
  + slice("refreshSessions")
  + `
Object.assign(exports, {
  refresh: refreshSessions,
  setMeshes: (ms) => { meshCache = ms; },
});`)(ctx, document, el, api, list, [], false);
/* The last argument is the mesh grouping, and it is OFF here on purpose.
   This harness is a census of the name line, and under a mesh heading the
   row drops the pill for the heading's own mesh (railMeshTags' `under`), so
   leaving the grouping on would have the census counting a row whose rooms
   were deliberately thinned. The grouped row is railmeshgroup_check's. */

/* ---- the rail, built by the real code ---------------------------------- */
const member = (session) => ({ session, handle: session, role: "worker",
                               local: true, machine: "" });
served = { sessions: [
  { name: "s20", status: "idle", role: "leader", profile: "nc", parent: null },
  { name: "s21", status: "busy", role: "worker", profile: "nc", parent: "s20" },
  { name: "s25", status: "idle", role: "worker", profile: "nc", parent: "s21" },
  { name: "loner", status: "idle", profile: "nc", parent: null },
  { name: "mesh-reviewer", status: "idle", profile: "nc", parent: null },
] };
ctx.setMeshes([
  { name: "mesh0", members: ["s20", "s21", "s25"].map(member) },
  { name: "review", members: [{ session: "mesh-reviewer", handle: "mesh-reviewer",
                                 role: "reviewer", local: true, machine: "" }] },
  { name: "gds", members: [member("s21")] },
]);

(async () => {
  await ctx.refresh();

  const rows = list.kids.filter((r) => r.dataset.name);
  // Plain lineage order, now that this harness runs ungrouped: each parent
  // followed by its children, then the roots that have none.
  check("every session gets a row", rows.map((r) => r.dataset.name),
        ["s20", "s21", "s25", "loner", "mesh-reviewer"]);

  const row = (name) => rows.find((r) => r.dataset.name === name);
  const kidClasses = (r) => r.kids.map((k) => k.className);

  /* The row's own children ARE its lines: anything that is not on the name
     line has to be a full-width child, so counting the direct children is
     how many things are competing for that line. The name, its role and its
     rooms must not be three of them — and neither must the action buttons,
     which travel in the full-width .rail-actions box one line below the
     name line (claunch-ku033). */
  // The role is drawn once, on the name line, on every card (claunch-hknpm
  // removed the selected card's extra role line).
  check("the row's parts are the dot, the name group, the profile, the directory line, the context line, the attention line and the buttons box",
        kidClasses(row("s21")),
        ["dot busy", "rail-head", "meta", "rail-cwd", "rail-ctx-line unknown",
         "rail-seen", "rail-actions"]);
  check("a session with no role and one room keeps the same seven parts",
        kidClasses(row("loner")),
        ["dot idle", "rail-head", "meta", "rail-cwd", "rail-ctx-line unknown",
         "rail-seen", "rail-actions"]);
  check("the spawn + and the ⓘ travel inside the buttons box, not loose on the row",
        (row("s21").querySelector(".rail-actions") || node("span")).kids.map((k) => k.className),
        ["sess-plus", "sess-info"]);

  /* New sessions store their role on mesh membership. The rail must use that
     authoritative value even when /api/sessions intentionally returns no
     duplicated role field. */
  check("the rail reads a new session's role from mesh membership",
        (row("mesh-reviewer").querySelector(".rail-head") || node("span")).querySelectorAll(".mesh-role").map((t) => t.text),
        ["reviewer"]);

  /* The point of the change: role and rooms are siblings inside one box, not
     loose on the row where they wrapped. */
  // `?? node("span")` throughout: when the grouping is gone these read as
  // plain failures rather than a stack trace that hides the rest of the run.
  const some = (n) => n || node("span");
  const head = (name) => some(row(name).querySelector(".rail-head"));
  const find = (name, cls) => some(row(name).querySelector(cls));
  /* The keyboard chip joined that box after the name (railkeys_check owns
     what it says and when). It is listed here because this line is a census
     of the name line: a part that stopped being a child of the box would be
     loose on the row, which is the wrap this whole harness exists to catch. */
  /* The role pill now also carries its family's colour class (role-worker,
     app.js railRoleFamily); the pill's place on the line is what this census
     is about, so the colour class is part of the expected string rather than
     something the check has to look past. */
  check("name, keyboard chip, role and rooms share one container",
        head("s21").kids.map((k) => k.className),
        ["rail-name", "rail-keys hidden", "mesh-role role-worker", "rail-meshes"]);
  check("the mesh tags sit beside the role badge, not under the row",
        some(find("s21", ".rail-mesh").parent).parent?.className ?? "(loose)",
        "rail-head");
  check("the role badge and the mesh tags have the same parent",
        find("s21", ".mesh-role").parent?.className ?? "(none)",
        find("s21", ".rail-meshes").parent?.className ?? "(none)");

  /* Nothing was dropped to win the line back. */
  check("the rooms are still named, and still capped and ordered",
        row("s21").querySelectorAll(".rail-mesh").map((t) => t.text),
        ["gds", "mesh0"]);
  check("a session in one room shows it",
        row("s25").querySelectorAll(".rail-mesh").map((t) => t.text), ["mesh0"]);
  check("the hover text still carries handle and role",
        find("s25", ".rail-mesh").title,
        "mesh mesh0 — joined as s25 (worker)");
  check("a session in no mesh grows no tag box",
        row("loner").querySelectorAll(".rail-meshes").length, 0);
  check("a session with no role still says so — 'free-role', the packaged default",
        (row("loner").querySelector(".rail-head") || node("span")).querySelectorAll(".mesh-role").map((t) => t.text),
        ["free-role"]);

  /* An ellipsised name is only acceptable because the whole one is a hover
     away — the row's own title is about lineage, so the label carries it.

     Pinned as the FIRST LINE rather than the whole title, because the title is
     shared: the context note joins it instead of replacing it, and the next
     thing that wants a word will join it too. Demanding the title be exactly
     the name would fail on a row that is perfectly correct, and pinning
     nothing would let a future note push the name out of reach — which is the
     one thing the ellipsis is relying on. */
  check("the name is still the first line of its hover text",
        find("s25", ".rail-name").title.split("\n")[0], "s25");

  /* The indent may not eat the row: it is the one part that grows without
     bound as the tree deepens, and past a few levels it would spend the
     whole rail on whitespace. */
  const indent = (r) => {
    if (!r.classList.contains("child")) return 8;
    const depth = Number(r.dataset.railDepth);
    check("the whole card uses its depth for responsive indentation", r.style.marginLeft,
      `calc(8px + ${depth} * var(--rail-child-indent, 20px))`);
    return 8 + depth * 12; // Narrow-rail geometry is also measured in the browser.
  };
  const pad = (name) => indent(row(name));
  ok("a child row is indented", pad("s21") > 0, `s21 padding ${pad("s21")}`);
  ok("a grandchild is indented further than its parent",
     pad("s25") > pad("s21"), `s21 ${pad("s21")} vs s25 ${pad("s25")}`);
  ok("the indent leaves most of a 260px rail to the row",
     pad("s25") <= 60, `depth-2 indent is ${pad("s25")}px`);

  /* A deep chain must not indent itself off the rail. The tick and the
     "spawned by" tooltip carry the lineage; the indent only has to make the
     nesting scannable, so past a few levels it stops paying for depth. */
  served = { sessions: [{ name: "d0", status: "idle", profile: "nc", parent: null }]
    .concat([1, 2, 3, 4, 5, 6, 7].map((i) => (
      { name: `d${i}`, status: "idle", profile: "nc", parent: `d${i - 1}` }))) };
  ctx.setMeshes([]);
  await ctx.refresh();
  const deepRows = list.kids.filter((r) => r.dataset.name);
  const deep = deepRows.map(indent);
  check("the rail still lists the whole chain",
        deepRows.map((r) => r.dataset.name).length, 8);
  ok("the deepest row still leaves the rail most of its width",
     Math.max(...deep) <= 60, `indents ${deep.join(",")}`);
  ok("the indent stops growing once the nesting is already legible",
     deep[7] === deep[5], `d5 ${deep[5]}px vs d7 ${deep[7]}px`);
  ok("but it does grow over the first few levels",
     deep[1] > deep[0] && deep[3] > deep[1], `indents ${deep.join(",")}`);

  /* ---- the line budget, which lives in the stylesheet ------------------ */
  ok("style.css comments are balanced",
     css.split("/*").length === css.split("*/").length,
     `${css.split("/*").length - 1} openers, ${css.split("*/").length - 1} closers`);

  const plain = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const rules = new Map();
  for (const m of plain.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const decls = {};
    for (const d of m[2].split(";")) {
      const i = d.indexOf(":");
      if (i > 0) decls[d.slice(0, i).trim()] = d.slice(i + 1).trim();
    }
    for (const sel of m[1].split(",")) {
      const key = sel.trim();
      rules.set(key, Object.assign(rules.get(key) || {}, decls));
    }
  }
  const decl = (sel, prop) => (rules.get(sel) || {})[prop];

  /* A full-width child is a line break. Only the ten deliberate ones may
     be, and each must sort after the toggle — a breaker at the default
     order 0 ends the line before the toggle can land on it, which is the
     bug. (.rail-cwd, .rail-brief, .rail-ctx-line and .rail-seen are the
     always-on one-liners; .rail-quiet is drawn only when one of the two
     settings that silence the daemon is set — see railquiet_check;
     .rail-beads only when the board assigns the session something — see
     railbeads_check; .rail-tps-line only for a session whose calls go
     through the metering shim — see tpsRailLine in app.js;
     .rail-usage-line only for a session whose transcript the daemon can
     total — see usage_check; .rail-ctx-tokens only under a recorded
     context reading, naming its billed buckets and the cost estimate where
     the model is priced — see modelprice_check; .rail-note only
     for a session the reader annotated, and .rail-note-editor in its place
     while that note is being edited on the row — see railnote_check;
     .rail-progress only for a session that published progress — see
     railprogress_check; .rail-commits only for a session with stamped
     commits or a recorded PR — see sessionprs_check; .rail-life, the
     created/lifetime line, for every session with a creation stamp — see
     raillife_check.) */
  const BREAKERS = [
    "#session-list .rail-actions",
    "#session-list .rail-cwd", "#session-list .rail-beads",
    "#session-list .rail-brief", "#session-list .rail-ctx-line",
    "#session-list .rail-seen", "#session-list .rail-life",
    "#session-list .rail-quiet",
    "#session-list .rail-status-checks", "#session-list .rail-talk",
    "#session-list .rail-tps-line",
    "#session-list .rail-usage-line",
    "#session-list .rail-ctx-tokens",
    "#session-list .rail-note", "#session-list .rail-note-editor",
    "#session-list .rail-procs",
    "#session-list .rail-commits",
    "#session-list .rail-progress",
    "#session-list .sess-cflow", "#session-list .sess-cflow2", "#session-list .sess-sims", "#session-list .sess-brief",
  ];
  const fullWidth = [...rules].filter(([sel, d]) =>
    sel.startsWith("#session-list") &&
    (d["flex-basis"] === "100%" || /(^|\s)100%$/.test(d.flex || ""))
  ).map(([sel]) => sel);
  check("only the buttons line, the directory line, the beads line, the one-line, the context line, the throughput line, the usage line, the attention line, the age line, the quiet line, the note line (or its editor), the progress line, the process line, the commit line, the status-check line, the cflow line, the cflow v2 line, the simulation line, the briefing card and the talk line break the row",
        fullWidth.sort(), [...BREAKERS].sort());

  /* The one-line summary is the exception to this rail's ellipsis habit: it
     wraps, because it is the only line that says what a session is DOING and
     a cut one says it half. Its full-width base (asserted above) is what
     makes that affordable — it takes rows off the rail's scroll, never width
     off the name line. Any of the three truncating declarations coming back
     puts the tail back out of reach: nothing carries it, the element's title
     is a fixed label (briefrow_check pins that the text goes in whole). */
  check("the one-line summary wraps instead of being cut",
        decl("#session-list .rail-brief", "white-space"), "pre-wrap");
  for (const prop of ["text-overflow", "overflow"]) {
    check(`the one-line summary declares no ${prop} — it is not truncated`,
          decl("#session-list .rail-brief", prop), undefined);
  }
  /* The one-line (a briefing digest) can carry paths and branch names,
     which offer no break opportunity — without this the rail scrolls
     sideways instead. */
  check("a long unbreakable token breaks rather than widening the rail",
        decl("#session-list .rail-brief", "overflow-wrap"), "anywhere");

  /* The buttons box is the first full-width breaker under the name line, and
     every other sub-line sorts after it. It used to also be the briefing ▸
     toggle's own line, and the assertions here were pinned to the toggle's
     order; the rail has no row fold any more (app.js railCardMode — picking a
     card expands it), so the box's own order is the anchor. */
  const actionsOrder = Number(decl("#session-list .rail-actions", "order"));
  ok("the buttons line declares an order", Number.isFinite(actionsOrder));
  check("the rail draws no briefing toggle — picking a card expands it",
        decl("#session-list .sess-brief-toggle", "order"), undefined);
  for (const sel of BREAKERS) {
    if (sel === "#session-list .rail-actions") continue;
    const o = Number(decl(sel, "order"));
    ok(`${sel} sorts after the buttons line so the name and buttons keep their lines`,
       Number.isFinite(o) && o > actionsOrder, `order ${decl(sel, "order")} vs buttons line ${actionsOrder}`);
  }

  /* The name group holds its line by refusing to wrap and by basing at 0 —
     a flex line is wrapped on base sizes and only shrunk afterwards, so a
     content-sized box would push the meta off before shrinking. The row's
     buttons are not on this line at all any more: they travel in the
     full-width .rail-actions box under it. */
  check("the name group never wraps",
        decl("#session-list .rail-head", "flex-wrap"), "nowrap");
  const headFlex = decl("#session-list .rail-head", "flex") || "";
  ok("the name group bases at 0 so the row's fixed parts are placed first",
     /(^|\s)0(\D|$)/.test(headFlex), `flex is "${headFlex}"`);
  check("the tag box does not take a line of its own",
        decl("#session-list .rail-meshes", "flex-basis"), undefined);

  /* The name itself is the one label on this rail that is never cut — a
     clipped name reads as a different session, which is the one loss a row
     of near-identical names cannot afford (claunch-u6das). It wraps inside
     its span instead, while the group around it keeps the nowrap above; any
     of the truncating declarations coming back silently undoes the
     guarantee, and the narrow-container percentage cap would clip the
     wrapped second line the same way the ellipsis clipped the tail. */
  check("the rail name wraps instead of being cut",
        decl("#session-list .rail-name", "white-space"), "normal");
  check("the rail name declares no text-overflow — it is never truncated",
        decl("#session-list .rail-name", "text-overflow"), undefined);
  ok("the rail name declares no clipping overflow (visible is the default, not a clip)",
     !decl("#session-list .rail-name", "overflow") ||
     decl("#session-list .rail-name", "overflow") === "visible",
     `overflow is ${decl("#session-list .rail-name", "overflow")}`);
  /* The name still wraps whole (never clipped) when the group is squeezed,
     but the break must not be advertised per character: `anywhere` lets a
     squeezed flex line collapse the name to one character per line
     (claunch-ku033). `break-word` breaks an unbroken worktree-style name
     only to fill the width the name is actually given, and the em floor
     keeps the shrink from squeezing it below a readable line. */
  check("an unbroken worktree-style name breaks rather than widening the rail",
        decl("#session-list .rail-name", "overflow-wrap"), "break-word");
  ok("the rail name keeps a readable floor so the shrink cannot squeeze it into a vertical stack",
     /^\d+(\.\d+)?em$/.test(decl("#session-list .rail-name", "min-width") || ""),
     `min-width is ${decl("#session-list .rail-name", "min-width")}`);
  check("the narrow-container name cap is gone — it would clip the wrapped second line",
        decl("#session-list .rail-name", "max-width"), "none");

  /* Shrinking is what replaced wrapping, so it must stop while the pill can
     still say which room: a capsule holding nothing is not an abbreviation. */
  for (const sel of ["#session-list .rail-mesh", "#session-list .mesh-role"]) {
    const mw = decl(sel, "min-width");
    ok(`${sel} keeps a floor so it cannot shrink to an empty capsule`,
       mw && mw !== "0" && parseFloat(mw) > 0, `min-width is ${mw}`);
    check(`${sel} ellipsises rather than clipping`,
          decl(sel, "text-overflow"), "ellipsis");
  }
  check("the +N counter is never abbreviated — '+' would misstate the count",
        decl("#session-list .rail-mesh-more", "flex"), "none");

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("raillayout_check: ok");
})();
