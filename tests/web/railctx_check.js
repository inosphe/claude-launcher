/* Where the context reading lands on a rail row — the real refreshSessions
   building real rows against a stub DOM.

   `ctxsize_check` proves the wording is right. This proves it is *attached*,
   which is a different failure and a quieter one: the row builder is a busy
   piece of code that several hands edit, and a note that stops being hung on
   anything leaves no trace — no error, no failing assertion about a string,
   just a tooltip that is silently gone.

   So the checks here deliberately avoid naming the row's inner structure.
   The name element is found by *being the one that says the session's name*,
   not by its class or its position, because the whole point is to keep
   holding when the row is rearranged around it. A child's `title` beats its
   parent's wherever the pointer actually lands, and the pointer lands on the
   name — so if that element ever stops carrying the note, this fails. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  process.env.RAILCTX_APP_JS || path.join(
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
const capLine = src.match(/^const RAIL_MESH_TAGS = .+$/m);
if (!capLine) throw new Error("cannot locate RAIL_MESH_TAGS in app.js");
const domLine = src.match(/^const CTX_DOMAIN = .+$/m);
if (!domLine) throw new Error("cannot locate CTX_DOMAIN in app.js");
const coldLine = src.match(/^const SEEN_COLD = .+$/m);
if (!coldLine) throw new Error("cannot locate SEEN_COLD in app.js");
const staleLine = src.match(/^const TYPED_STALE = .+$/m);
if (!staleLine) throw new Error("cannot locate TYPED_STALE in app.js");

/* ---- stub DOM: nested nodes, because the note may be hung on a child ---- */
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

/* Everything refreshSessions leans on that is not the row itself.

   This list is a standing liability and worth naming as one: it has to hold
   every call the function makes, including ones this harness has no interest
   in, so any branch that adds a call to `refreshSessions` breaks every
   harness that slices it — with a ReferenceError that reads like a defect
   and is not one. It bit twice in one batch (`refreshParentChoices` below,
   which arrived with the spawn form, and `ctxNoteOnRow`, which arrived from
   here and broke someone else's). Point RAILCTX_APP_JS at the merged tree
   before believing a green here. */
const stubs = `
let sessionsCache = [], currentName = null, currentPage = "home";
let attachedPid = null, linkState = "down", sessName = null;
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
/* The briefing's per-row decoration (the one-line and the collapsed ⟳) is
   its own harness (briefrow_check); here, like applyBriefingCards, it is a
   no-op so what this harness pins — the context note's containment — stays
   about context. */
function decorateBriefingRow(li, s) {}
function terminalOnScreen() { return false; }
function attach() {}
function setStatusBadge() {}
function $(id) { return list; }
`;

const ctx = {};
new Function(
  "exports", "document", "el", "api", "list", "meshCache", "sessionGroupByMesh",
  stubs + capLine[0] + "\n"
  + slice("byLineage") + slice("sessionMeshGroup") + slice("sessMeshes") + slice("railMeshTags") + slice("sessHandles") + slice("handleTag")
  + slice("fmtAge") + slice("ctxShort") + slice("ctxAgeOf")
  + slice("ctxKnowable") + slice("ctxSentence") + slice("ctxBreakdown")
  + slice("ctxTooltip") + slice("ctxNoteOnRow") + domLine[0] + "\n"
  // The gauge line now names the model that the count was taken on, so it
  // needs the shortener too. What that name is, and where it lands, is
  // railmodel_check's subject; here it is only a call that has to resolve.
  + slice("modelShort")
  // The row also spends a line on its directory now (railcwd_check's
  // subject); here it is only a call that has to resolve.
  + slice("shortenPath") + slice("cwdSplit") + slice("cwdShort")
  + slice("cwdLine") + slice("railCwdLine")
  // ...and a line saying who has been near it (railseen_check's subject);
  // sliced rather than stubbed for the same reason as the directory line
  // above — the real call is what has to keep resolving.
  + coldLine[0] + "\n" + staleLine[0] + "\n"
  + slice("seenAgo") + slice("seenPair")
  + slice("railSeenLine")
  + slice("ctxRailLine") + slice("profileHarnessLabel")
  + slice("railMetaText")
  + slice("refreshSessions")
  + `
Object.assign(exports, {
  refresh: refreshSessions,
  tooltip: ctxTooltip,
  railLine: ctxRailLine,
});`)(ctx, document, el, api, list, [], true);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const AT = new Date(Date.now() - 185_000).toISOString();
const FULL = {
  name: "full", status: "busy", harness: "claude", profile: "nc", parent: null,
  context: { tokens: 154706, input: 2, cache_read: 154073, cache_write: 631,
             output: 210, model: "claude-opus-5", at: AT,
             compact_window: 200_000 },
};
const QUIET = { name: "quiet", status: "idle", harness: "claude",
                profile: "nc", parent: null };
const CODEX = {
  name: "codex", status: "idle", harness: "codex", profile: "nc", parent: null,
  context: { tokens: 187281, input: 1169, cache_read: 186112, cache_write: 0,
             output: 59, model: "gpt-5.6-sol", at: AT,
             model_context_window: 258_400 },
};
const OTHER = { name: "pi", status: "idle", harness: "pi",
                profile: "nc", parent: null };
served = { sessions: [FULL, QUIET, CODEX, OTHER] };

(async () => {
  await ctx.refresh();

  const rows = list.kids.filter((r) => r.dataset.name);
  check("every session still gets a row",
        rows.map((r) => r.dataset.name), ["full", "quiet", "codex", "pi"]);

  const row = (name) => rows.find((r) => r.dataset.name === name);
  /* Found by what it says, not by where it sits or what it is called: this
     check has to survive the row being rearranged around the name. */
  const nameEl = (s) =>
    descendants(row(s.name)).find((k) => k.textContent === s.name);

  /* Containment, not equality, throughout: both the row and the name may
     already carry a tooltip of their own (what spawned this, what the full
     name was), and the note joins those rather than replacing them. Asking
     for equality would pin whichever of those happens to exist today. */
  const carries = (n, text) => !!n && n.title.includes(text);

  const note = ctx.tooltip(FULL);
  check("the row carries the reading", carries(row("full"), note), true);
  check("...and so does the name inside it, which is what a pointer hits",
        carries(nameEl(FULL), note), true);

  const unknown = "context not known yet";
  check("a session that has not answered yet says so on its row",
        carries(row("quiet"), unknown), true);
  check("...and on its name", carries(nameEl(QUIET), unknown), true);

  const codexNote = ctx.tooltip(CODEX);
  check("a Codex row carries its rollout reading",
        [carries(row("codex"), codexNote), carries(nameEl(CODEX), codexNote)],
        [true, true]);

  check("a harness that keeps no transcript is told nothing about context",
        [row("pi").title, (nameEl(OTHER) || {}).title || ""]
          .some((t) => t.includes("context")), false);

  /* The count now takes one deliberate line on the row: a gauge bar beside
     the short count — found by class, not position, for the same reason the
     name is: the row gets rearranged around this. */
  const lineOf = (name) =>
    descendants(row(name)).find((k) => k.className === "rail-ctx-line"
      || k.className === "rail-ctx-line unknown");
  const under = (name, cls) =>
    descendants(lineOf(name) || node("span")).find((k) => k.classes.has(cls));
  const numText = (name) => (under(name, "rail-ctx") || {}).text;

  check("the row's line shows the short count", numText("full"), "155k");
  check("...beside a bar whose fill is the count over the fixed 0–1M domain, " +
        "coloured against the compact window it will actually compact at",
        [(under("full", "rail-ctx-fill") || {}).className,
         ((under("full", "rail-ctx-fill") || {}).style || {}).width],
        ["rail-ctx-fill warm", "15.5%"]);
  check("...and a tick where this session's auto-compact window sits",
        ((under("full", "rail-ctx-tick") || {}).style || {}).left, "20.0%");
  check("a Codex row shows the rollout count against the fixed domain",
        [numText("codex"),
         (under("codex", "rail-ctx-fill") || {}).className,
         ((under("codex", "rail-ctx-fill") || {}).style || {}).width],
        ["187k", "rail-ctx-fill warm", "18.7%"]);
  check("Codex marks its reported model context window",
        [((under("codex", "rail-ctx-tick") || {}).style || {}).left,
         (under("codex", "rail-ctx-tick") || {}).title],
        ["25.8%", "model context window: 258k tokens"]);
  check("a session that has not answered shows a greyed ?, not a count",
        numText("quiet"), "?");
  check("and its line is marked, so it reads as absence, not a small count",
        [lineOf("quiet").className,
         (under("quiet", "rail-ctx") || {}).className],
        ["rail-ctx-line unknown", "rail-ctx unknown"]);
  check("...with an empty track, never a zero-width fill pretending to measure",
        under("quiet", "rail-ctx-fill"), undefined);
  check("a harness that keeps no transcript grows no line",
        lineOf("pi"), undefined);
  check("a Codex session awaiting its first reading still gets an unknown line",
        ctx.railLine({ harness: "codex" }).className, "rail-ctx-line unknown");

  /* The line is the glance; the reading stays one hover away on it. */
  const rail = ctx.railLine(FULL);
  check("the line's tooltip is the same story the row carries",
        carries(rail, note), true);
  check("...plus the domain and where the tick is",
        [rail.title.includes("bar spans 0–1M tokens"),
         rail.title.includes("auto-compact window at 200k")],
        [true, true]);
  const codexRail = ctx.railLine(CODEX);
  check("the Codex tooltip names the model window threshold",
        [codexRail.title.includes("model context window at 258k"),
         codexRail.title.includes("auto-compact")],
        [true, false]);

  /* The colour is judged against the compact window (compaction fires at the
     tick, not at 1M), the fill against the domain — two different questions
     one bar answers. */
  const fillOf = (line) =>
    descendants(line).find((k) => k.classes.has("rail-ctx-fill"));
  const hot = ctx.railLine({ harness: "claude",
    context: { ...FULL.context, tokens: 190_000 } });
  check("a count at 95% of its compact window is hot while the bar is short",
        [fillOf(hot).className, fillOf(hot).style.width],
        ["rail-ctx-fill hot", "19.0%"]);
  const free = ctx.railLine({ harness: "claude",
    context: { ...FULL.context, compact_window: undefined } });
  check("with no window configured there is no tick, and the colour falls " +
        "back to the domain",
        [descendants(free).some((k) => k.classes.has("rail-ctx-tick")),
         fillOf(free).className],
        [false, "rail-ctx-fill"]);
  check("...and the tooltip then claims only the domain, never a window",
        [free.title.includes("bar spans 0–1M tokens"),
         free.title.includes("auto-compact")],
        [true, false]);
  const past = ctx.railLine({ harness: "claude",
    context: { ...FULL.context, tokens: 1_200_000 } });
  check("a count past the domain clamps at full rather than overrunning",
        fillOf(past).style.width, "100.0%");

  check("no row claims a percentage — there is no denominator to make one",
        rows.concat(rows.flatMap((r) => descendants(r)))
          .some((n) => /%/.test(n.title)), false);

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("railctx_check: ok");
})();
