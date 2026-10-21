/* What one poll costs the rail's context line, when nothing about the reading
   moved.

   The poll is a delta: the daemon answers "these rows changed, these are the
   same" and `sessionRowsTake` hands the unchanged ones back as fresh objects
   parsed from the text it kept (app.js). So every poll presents the rail with
   a new object for the same reading, and anything memoised on the object
   misses on every poll — the count in the sentence, the breakdown in the
   tooltip, and the line the tooltip hangs on, whose title carries the
   reading's age and therefore changes every time.

   The trace behind claunch-hwbuc measured that pass at ~6 ms per poll on a
   working rail, and the line swap beside it (a node built, styled and
   replaced per row) at another ~6 ms. Neither is a redraw: nothing the reader
   sees moved except the age inside a tooltip. What is held here:

   - a reading re-parsed from the same text formats nothing again,
   - a reading that really moved formats again,
   - the gauge line is not rebuilt when only the tooltip's age moved, and its
     title still ends up carrying the new age.

   The harness runs the real `refreshRailSeen` against a stub DOM, because
   the defect lives in the meeting of three functions (the poll's re-parse,
   the memos, the in-place refresh) and each one alone looks correct. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  process.env.CTXPOLL_APP_JS || path.join(
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
function constLine(name) {
  const m = src.match(new RegExp(`^const ${name} = .+$`, "m"));
  if (!m) throw new Error(`cannot locate ${name} in app.js`);
  return m[0] + "\n";
}

/* ---- stub DOM ---- */
let replaced = 0;
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), dataset: {}, style: {},
    title: "", type: "", parentNode: null,
    appendChild(c) { n.kids.push(c); c.parentNode = n; return c; },
    append(...cs) { cs.forEach((c) => n.appendChild(c)); },
    addEventListener() {},
    querySelector(sel) {
      const cls = sel.startsWith(".") ? sel.slice(1) : null;
      return descendants(n).find((k) => cls && k.classes.has(cls)) || null;
    },
    querySelectorAll() { return []; },
    replaceWith(other) {
      replaced++;
      const p = n.parentNode;
      if (!p) return;
      const at = p.kids.indexOf(n);
      p.kids[at] = other;
      other.parentNode = p;
      n.parentNode = null;
    },
    remove() {
      const p = n.parentNode;
      if (!p) return;
      const at = p.kids.indexOf(n);
      if (at >= 0) p.kids.splice(at, 1);
      n.parentNode = null;
    },
    get firstElementChild() { return n.kids.find((k) => k.tag) || null; },
    get textContent() { return n.text; },
    set textContent(v) { n.text = String(v); },
    get className() { return [...n.classes].join(" "); },
    set className(v) { n.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get outerHTML() {
      return `<${n.tag} class="${n.className}" title="${n.title}">` +
        n.kids.map((k) => k.outerHTML).join("") + (n.text || "") + `</${n.tag}>`;
    },
  };
  n.classList = {
    add: (...cs) => cs.forEach((c) => n.classes.add(c)),
    remove: (...cs) => cs.forEach((c) => n.classes.delete(c)),
    contains: (c) => n.classes.has(c),
    toggle: (c, on) => { if (on) n.classes.add(c); else n.classes.delete(c); },
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
/* The one selector refreshRailSeen asks the list for. */
list.querySelectorAll = (sel) => (sel === "li[data-name]"
  ? list.kids.filter((k) => k.tag === "li" && k.dataset.name) : []);
let sessionsCache = [];

/* Everything the in-place refresh leans on that is not the context line. The
   other lines of the row (seen, life, tps, dot, briefing, talk) are its
   neighbour's subjects; here each is the smallest thing that keeps its call
   from being the failure, and the seen line is a real stub node because
   refreshRailSeen skips a row whose seen line is missing — before it ever
   reaches the context line. */
const stubs = `
function railHeld() { return false; }
function forgetDeadSessions() {}
function refreshResumeChoices() {}
function refreshParentChoices() {}
function sessionMatchesFilter() { return true; }
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
function decorateTalkRow() {}
function terminalOnScreen() { return false; }
function attach() {}
function setStatusBadge() {}
function $(id) { return list; }
function seenAgo(iso) { return null; }
function railSeenLine() { return el("span", "rail-seen", "seen stub"); }
function railLifeLine() { return el("span", "rail-life", "life stub"); }
function lifeCreatedText() { return ""; }
function lifeSpanText() { return ""; }
function lifeSecs() { return 0; }
function applyDotGrade() {}
function tpsRailLine() { return null; }
function tpsAgeSecs() { return null; }
function tpsStaleClass() { return ""; }
function ctxNoteOnRow() {}
/* refreshRailSeen reads the cache the poll wrote; the harness hands it the
   fleet the same way, one poll at a time. */
let sessionsCache = [];
function __setSessions(rows) { sessionsCache = rows; }
let currentName = null, sessName = null;
let attachedPid = null, linkState = "down", snapshotName = null;
let keptTerms = new Map();
function dropKept() {}
let railRedrawPending = false;
`;

const ctx = {};
new Function(
  "exports", "document", "el", "list", "meshCache",
  stubs + constLine("RAIL_MESH_TAGS") + constLine("CTX_DOMAIN")
  + constLine("RAIL_STALE_DEFAULT") + constLine("railStale")
  + slice("fmtAge") + slice("ctxShort") + slice("ctxAgeOf") + slice("ctxKnowable")
  + slice("ctxSentence") + slice("ctxBreakdown") + slice("ctxTooltip")
  + slice("modelShort") + slice("profileHarnessLabel") + slice("ctxRailLine")
  + slice("sessionMeshGroup") + slice("sessMeshes") + slice("railMeshTags")
  + slice("sessHandles") + slice("handleTag") + slice("railMetaText")
  + slice("refreshRailSeen")
  + `
Object.assign(exports, {
  refresh: refreshRailSeen,
  tooltip: ctxTooltip,
  railLine: ctxRailLine,
  setSessions: __setSessions,
});`)(ctx, document, el, list, []);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* ---- a rail of rows, the way refreshSessions leaves one ---- */
const SESSIONS = Number(process.env.CTXPOLL_ROWS || 40);
function fleet() {
  const at = new Date(Date.now() - 185_000).toISOString();
  return Array.from({ length: SESSIONS }, (_, i) => ({
    name: `s${i}`, status: "busy", harness: "claude", profile: "nc", parent: null,
    context: {
      tokens: 154_706 + i, input: 2, cache_read: 154_073 + i, cache_write: 631,
      output: 210 + i, model: "claude-opus-5", at,
      compact_window: 200_000,
    },
  }));
}

const base = fleet();
/* The rows are built from a DIFFERENT reading per name — as a page that
   loaded a poll ago would hold them — so the measured first pass below meets
   cold memos and really does the formatting. Building them from `base`
   itself would warm the cache and measure nothing. */
const earlier = base.map((s) => ({
  ...s,
  context: {
    ...s.context,
    tokens: 1_000 + Number(s.name.slice(1)),
    cache_read: 90_000 + Number(s.name.slice(1)),
    output: 900 + Number(s.name.slice(1)),
  },
}));
for (const s of earlier) {
  const li = node("li");
  li.dataset.name = s.name;
  li._railTitleBase = s.name;
  li.className = "rail-row";
  const seen = el("span", "rail-seen", "seen stub");
  const line = ctx.railLine(s);
  line._railKey = JSON.stringify([s.context || null, s.harness || null]);
  const name = el("span", "rail-name", s.name);
  name._railTitleBase = s.name;
  li.append(seen, line, name);
  list.appendChild(li);
}
/* A poll: same fleet, re-parsed — what sessionRowsTake hands back for the rows
   the daemon called unchanged. */
const reparsed = () => JSON.parse(JSON.stringify(base));
ctx.setSessions(base);

function measure(fn) {
  const format = Number.prototype.toLocaleString;
  let formatted = 0;
  Number.prototype.toLocaleString = function (...a) {
    formatted++;
    return format.apply(this, a);
  };
  const started = process.hrtime.bigint();
  try { fn(); } finally {
    Number.prototype.toLocaleString = format;
    var ms = Number(process.hrtime.bigint() - started) / 1e6;
  }
  return { formatted, ms };
}

/* The memos are module state in app.js and have to start cold: this harness
   runs in its own process, so the first pass below is the first pass. */
const ctxLineOf = (name) => {
  const li = list.kids.find((k) => k.dataset.name === name);
  return descendants(li).find((k) => k.classes.has("rail-ctx-line"));
};

/* The wall clock, moved on purpose. The one part of the line's tooltip that
   moves while the reading itself does not is the age, and a harness that runs
   its two polls in the same second cannot tell a line that follows the age
   from one that ignores it — the title would not change either way, and the
   in-place write this file is here to hold would fall out of the code
   unnoticed (review of claunch-hwbuc, s1305-rev: the check that stood here
   compared a copy of the title with the title). So the clock is the harness's
   to move, and the polls below are an hour apart. */
const clock = { skew: 0 };
const realNow = Date.now;
Date.now = () => realNow() + clock.skew;

const first = measure(() => { ctx.setSessions(base); ctx.refresh(list); });
check("the first pass formats every row's numbers", first.formatted >= SESSIONS * 5, true);

const before = ctxLineOf("s0");
check("the line starts with the reading's age", /as of 3m ago/.test(before.title), true);

clock.skew = 3_600_000;
replaced = 0;
const second = measure(() => { ctx.setSessions(reparsed()); ctx.refresh(list); });
check("a poll that re-parses the same readings formats nothing again",
      second.formatted, 0);
check("...and rebuilds no context line", replaced, 0);
check("...while the line is still the node it was", ctxLineOf("s0") === before, true);
check("...and the age in its tooltip moved on without a rebuild",
      /as of 1h\d\dm ago/.test(before.title), true);
check("...with the bar's scale still beside it",
      /bar spans 0–1M tokens/.test(before.title), true);
const secondRebuilds = replaced;
Date.now = realNow;

/* A reading that really changed is not served from the cache. */
const moved = reparsed();
moved[0].context = { ...moved[0].context, tokens: 199_999, output: 1_234 };
replaced = 0;
const third = measure(() => { ctx.setSessions(moved); ctx.refresh(list); });
check("a reading that moved is formatted afresh", third.formatted >= 5, true);
check("...and its line is built again", replaced >= 1, true);
check("...showing the new count",
      /context 199,999 tokens/.test(ctxLineOf("s0").title), true);

/* The measured cost, printed rather than asserted: this is the number the
   issue's before/after is made of, and a check that pins it would go red on
   any machine slower than the one that set it. The count of formats is the
   metric that carries — a toLocaleString is a few microseconds here and
   rather more in a page — and CTXPOLL_ROWS sizes the fleet to the rail being
   talked about (the trace's rail was a few hundred rows). */
console.log(`ctxpoll_check: ${SESSIONS} rows — first pass ${first.ms.toFixed(1)}ms ` +
            `(${first.formatted} formats), a poll an hour later ${second.ms.toFixed(1)}ms ` +
            `(${second.formatted} formats, ${secondRebuilds} rebuilds), ` +
            `moved row ${third.ms.toFixed(1)}ms`);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("ctxpoll_check: ok");
