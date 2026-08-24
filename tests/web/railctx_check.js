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
function refreshResumeChoices() {}
function refreshParentChoices() {}
function renderHome() {}
function syncBulkActions() {}
function syncMobileBars() {}
function applyCflowBadges() {}
function applyBriefingCards() {}
function terminalOnScreen() { return false; }
function attach() {}
function setStatusBadge() {}
function $(id) { return list; }
`;

const ctx = {};
new Function(
  "exports", "document", "el", "api", "list", "meshCache",
  stubs + capLine[0] + "\n"
  + slice("byLineage") + slice("sessMeshes") + slice("railMeshTags")
  + slice("fmtAge") + slice("ctxShort") + slice("ctxAgeOf")
  + slice("ctxKnowable") + slice("ctxSentence") + slice("ctxBreakdown")
  + slice("ctxTooltip") + slice("ctxNoteOnRow")
  + slice("refreshSessions")
  + `
Object.assign(exports, { refresh: refreshSessions, tooltip: ctxTooltip });`
)(ctx, document, el, api, list, []);

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
             output: 210, model: "claude-opus-5", at: AT },
};
const QUIET = { name: "quiet", status: "idle", harness: "claude",
                profile: "nc", parent: null };
const OTHER = { name: "pi", status: "idle", harness: "codex",
                profile: "nc", parent: null };
served = { sessions: [FULL, QUIET, OTHER] };

(async () => {
  await ctx.refresh();

  const rows = list.kids;
  check("every session still gets a row",
        rows.map((r) => r.dataset.name), ["full", "quiet", "pi"]);

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

  check("a harness that keeps no transcript is told nothing about context",
        [row("pi").title, (nameEl(OTHER) || {}).title || ""]
          .some((t) => t.includes("context")), false);

  check("no row claims a percentage — there is no denominator to make one",
        rows.some((r) => /%/.test(r.title)), false);

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("railctx_check: ok");
})();
