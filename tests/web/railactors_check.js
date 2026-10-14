/* Who created a session and who ended it, on its rail row (claunch-o6xws),
   run against the real functions from app.js and the real refreshSessions
   against a stub DOM.

   The daemon records both as actors (daemon/actors.py): ``created_by`` on
   the definition, ``ended_by`` on the record. The row says them in the
   fewest words it can spare -- the ender right after the state it
   qualifies, the creator closing the meta line -- and the full actors ride
   the row's hover text. A record from before the fields carries neither,
   and its line is exactly what it was. */

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
let attachedPid = null, linkState = "down", sessName = null, snapshotName = null;
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
/* The reader's own note line, stubbed for the same reason as the briefing
   decoration below: its wording and its stylesheet contract are
   railnote_check's subject, not this harness's. */
function decorateNoteRow(li, s) {}
function decorateProgressRow(li, s) {}
function decorateProcsRow(li, s) {}
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
  + slice("actorWho") + slice("railEndedText") + slice("railCreatedText") + slice("actorLabel")
  + slice("refreshSessions")
  + `
Object.assign(exports, {
  refresh: refreshSessions,
  text: railMetaText,
  label: actorLabel,
});`)(ctx, document, el, api, list, [], true);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const base = { profile: "nc", harness: "claude" };
const ex = (extra) => ({ ...base, status: "exited", exit_code: 0, ...extra });

/* ---- the ender, one kind per case -------------------------------------- */
check("a person's kill",
      ctx.text(ex({ ended_by: { kind: "user", via: "web" } })),
      "nc/claude · exit 0 · killed by user");
check("an agent's kill names the agent",
      ctx.text(ex({ ended_by: { kind: "agent", name: "s12" } })),
      "nc/claude · exit 0 · killed by s12");
check("a workflow run's kill",
      ctx.text(ex({ ended_by: { kind: "cflow", name: "s12", reason: "kill-on-end" } })),
      "nc/claude · exit 0 · killed by cflow");
check("the session's own exit",
      ctx.text(ex({ ended_by: { kind: "self" } })),
      "nc/claude · exit 0 · self-exit");
check("a handoff is the session's own ending, said as one",
      ctx.text(ex({ ended_by: { kind: "self", reason: "handoff" } })),
      "nc/claude · exit 0 · handed off");
check("the machine going down",
      ctx.text(ex({ exit_code: 1073807364, ended_by: { kind: "os" } })),
      "nc/claude · exit 1073807364 · OS shutdown");
check("a pause says paused by",
      ctx.text(ex({ paused_at: "t", ended_by: { kind: "user", via: "cli" } })),
      "nc/claude · paused · paused by user");
check("an idle hold says held by the daemon",
      ctx.text(ex({ held_at: "t", ended_by: { kind: "daemon", reason: "idle" } })),
      "nc/claude · hold · held by daemon");
check("an archived row says archived, and no ender",
      ctx.text(ex({ archived_at: "t", ended_by: { kind: "user" } })),
      "nc/claude · archived");
check("a running row has no ender even if one is recorded",
      ctx.text({ ...base, status: "busy", ended_by: { kind: "user" } }),
      "nc/claude");
check("a record from before the field reads as it always did",
      ctx.text(ex({})), "nc/claude · exit 0");

/* ---- the creator --------------------------------------------------------- */
check("a session a person created",
      ctx.text({ ...base, status: "idle", created_by: { kind: "user", via: "web" } }),
      "nc/claude · spawned by user");
check("a session an agent spawned names the agent",
      ctx.text({ ...base, status: "idle", created_by: { kind: "agent", name: "lead" } }),
      "nc/claude · spawned by lead");
check("both, in order: state, ender, creator",
      ctx.text(ex({ ended_by: { kind: "cflow", name: "lead" },
                    created_by: { kind: "agent", name: "lead" } })),
      "nc/claude · exit 0 · killed by cflow · spawned by lead");

/* ---- the long form ------------------------------------------------------- */
check("the hover label carries every part",
      ctx.label({ kind: "cflow", name: "s3", reason: "kill-on-end" }),
      "cflow s3 [kill-on-end]");
check("...and the surface a person used",
      ctx.label({ kind: "user", via: "web" }), "user (web)");

/* ---- on the row, built by the real code -------------------------------- */
served = { sessions: [
  { name: "k1", status: "exited", exit_code: 143, profile: "nc", harness: "claude",
    parent: null, ended_by: { kind: "user", via: "web" },
    created_by: { kind: "user", via: "cli" } },
  { name: "c1", status: "idle", profile: "nc", harness: "claude", parent: null,
    created_by: { kind: "agent", name: "k1" } },
  { name: "old", status: "exited", exit_code: 0, profile: "nc", harness: "claude",
    parent: null },
] };

(async () => {
  await ctx.refresh();
  const rows = list.kids.filter((r) => r.dataset.name);
  const row = (name) => rows.find((r) => r.dataset.name === name);
  const metaOf = (name) => descendants(row(name)).find((k) => k.classes.has("meta"));

  check("the killed row's line says who",
        metaOf("k1").text, "nc/claude · exit 143 · killed by user · spawned by user");
  check("its hover text says it in full",
        row("k1").title.split("\n").slice(1),
        ["ended by: user (web)", "created by: user (cli)"]);
  check("a live row's hover text names its creator",
        row("c1").title, "created by: agent k1");
  check("an old record's row is unchanged",
        [metaOf("old").text, row("old").title],
        ["nc/claude · exit 0", "exited — open it to resume or archive"]);

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("railactors: ok");
})().catch((e) => { console.error(e); process.exit(1); });
