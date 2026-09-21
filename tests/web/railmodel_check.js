/* Which model a session is answering on, as the dashboard says it — the
   short name on the rail row's gauge line and the full id in the detail
   panel, run against the real functions from app.js.

   The fact itself was already arriving: the daemon reads the model out of
   the same assistant turn it reads the token count from (daemon/ctxsize.py)
   and hangs it on `context.model`. What it was NOT doing was appearing
   anywhere a pointer was not already hovering, and that is the whole of what
   these checks pin, in the two places it now shows:

   - The rail row. The short form is the point: a row has no width for
     `claude-haiku-4-5-20251001`, and the shortening must not quietly become
     a different model's name — `4-5` is one version number and "haiku 4 5"
     would read as two. The full id stays reachable on the chip's title.
   - The detail panel. Its own `model` row, found by BEING the row labelled
     model rather than by its position among a dozen others — the panel is
     re-ordered often, and a row that stops being appended leaves no trace at
     all: no error, no wrong string, just a fact silently gone. That is the
     same class of failure railctx_check exists for, one panel over.

   And in both places, absence is absence: a session that has not completed a
   turn has no model to name, and a harness that keeps no transcript is not
   asked. Neither may come out as a blank that reads like a model. */
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
const domLine = src.match(/^const CTX_DOMAIN = .+$/m);
if (!domLine) throw new Error("cannot locate CTX_DOMAIN in app.js");

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), dataset: {}, style: {},
    title: "", type: "", href: "",
    appendChild(c) { n.kids.push(c); return c; },
    append(...cs) { cs.forEach((c) => n.appendChild(c)); },
    addEventListener() {},
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

/* Everything the detail panel calls that is not this fact. Each is somebody
   else's harness (sessrun_check, sesssend_check, rolepanel_check, …); here
   they are no-ops so what is under test stays the one `dl` between them. */
const view = node("div");
const stubs = `
let sessName = null, sessRunFold = null;
function $(id) { return view; }
function formInUse() { return false; }
function sessLayoutFor() { return { rail: "details" }; }
function stopSessRun() {}
function sessHead() { return el("div", "sess-head"); }
function sessRailTabs() { return el("div", "sess-rail-tabs"); }
function sessWorkflow() { return el("div", "sess-wf"); }
function sessBriefSection() { return el("div", "sess-brief"); }
function sessSend() { return el("div", "sess-send"); }
/* The Hand off box — handoff_check's subject; here it only has to resolve. */
function sessHandoff() { return el("div", "sess-send sess-handoff"); }
/* The Meshes section's enrol row — meshjoin_check's subject; here it is only
   a call that has to resolve. */
function sessMeshJoin() { return el("div", "sess-mesh-join"); }
function sessQueued() { return null; }
function sessBackpressure() { return null; }
function sessReborrow() { return el("div", "sess-reborrow"); }
function sessPerms() { return el("div", "sess-perms"); }
function sessMigrate() { return el("div", "sess-migrate"); }
/* The note editor, fixed like the rest: this harness is about the model row,
   and the note's own behaviour is railnote_check's subject. */
function sessNote() { return el("div", "sess-note"); }
function rolePanels() { return []; }
function sessBeads() { return el("div", "sess-beads"); }
function sessCommits() { return el("div", "sess-commits"); }
function sessTask() { return el("div", "sess-task"); }
function sessInputJournal() { return el("div", "sess-input-journal"); }
/* The mesh handle a row/head wears when it differs from the session name
   (sesshandle_check's subject); here it is only a call that has to
   resolve. */
function sessHandles(name) { return []; }
function handleTag(name) { return null; }
function go() {}
`;

const ctx = {};
new Function(
  "exports", "document", "el", "view",
  stubs
  + slice("fmtAge") + slice("ctxShort") + slice("ctxAgeOf")
  + slice("ctxKnowable") + slice("modelShort") + slice("modelSentence")
  + slice("ctxSentence") + slice("ctxBreakdown") + slice("ctxTooltip")
  + domLine[0] + "\n" + slice("ctxRailLine")
  + slice("profileHarnessLabel") + slice("metaRow") + slice("renderSession")
  + `
Object.assign(exports, {
  short: modelShort,
  sentence: modelSentence,
  railLine: ctxRailLine,
  render: renderSession,
});`)(ctx, document, el, view);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* ---- the short name ---------------------------------------------------- */
/* The vendor word goes because every row in a claude fleet repeats it; the
   date goes because a dated release is the same model; the host path goes
   because it says where the model runs, not which one it is. The version
   survives intact — that is the one the row is actually distinguishing by. */
check("the vendor prefix goes", ctx.short("claude-opus-5"), "opus 5");
check("a dated release reads as its model",
      ctx.short("claude-haiku-4-5-20251001"), "haiku 4.5");
check("a two-part version stays one number",
      ctx.short("claude-3-5-sonnet-20241022"), "3.5 sonnet");
check("a gateway path keeps only the model",
      ctx.short("accounts/fireworks/models/glm-5p2"), "glm 5p2");
check("nothing in is nothing out", [ctx.short(""), ctx.short(null),
                                    ctx.short(undefined)], ["", "", ""]);
/* Eight digits at the end is a date; four is somebody's model name. Dropping
   the wrong one would rename the model on every row that runs it. */
check("a number that is not a date survives", ctx.short("gpt-4o"), "gpt 4o");

/* ---- the detail panel's sentence --------------------------------------- */
const AT = new Date(Date.now() - 185_000).toISOString();   // 3m ago
const READING = {
  tokens: 154706, input: 2, cache_read: 154073, cache_write: 631,
  output: 210, model: "claude-haiku-4-5-20251001", at: AT,
  compact_window: 200_000,
};
const FULL = { name: "full", status: "busy", harness: "claude", profile: "nc:claude",
               cwd: "/w", cols: 80, rows: 24, context: READING };
const QUIET = { name: "quiet", status: "idle", harness: "claude",
                profile: "nc", cwd: "/w", cols: 80, rows: 24 };
const CODEX_READING = {
  tokens: 187281, input: 1169, cache_read: 186112, cache_write: 0,
  output: 59, model: "gpt-5.6-sol", at: AT,
  model_context_window: 258_400,
};
const CODEX = { name: "codex", status: "idle", harness: "codex",
                profile: "nc:codex", cwd: "/w", cols: 80, rows: 24,
                context: CODEX_READING };
/* pi, as measured on a ds4-official:pi probe (2026-09-11): the model id is
   what the provider projection named it, not a dated vendor id. */
const PI_READING = {
  tokens: 14148, input: 1988, cache_read: 12160, cache_write: 0,
  output: 128, model: "deepseek-flash", at: AT,
  compact_window: 600_000, model_context_window: 1_000_000,
};
const PI = { name: "pi", status: "idle", harness: "pi",
             profile: "ds4-official:pi", cwd: "/w", cols: 80, rows: 24,
             context: PI_READING };
const OTHER = { name: "kimi", status: "idle", harness: "kimi", profile: "nc",
                cwd: "/w", cols: 80, rows: 24 };

/* The full id, never the short one: the panel has the width, and this is
   where somebody goes to find out exactly what they are running. The age
   travels with it for the same reason it travels with the count — it is the
   last COMPLETED turn's model, so a /model switch mid-answer is not here
   yet, and a sentence in the bare present tense would say otherwise. */
check("the panel says the full id and how old the reading is",
      ctx.sentence(FULL), "claude-haiku-4-5-20251001 (as of its turn 3m ago)");
check("a session that has not answered yet says so, and does not say a model",
      ctx.sentence(QUIET), "not known yet — no context reading recorded");
check("a Codex rollout names its model with the reading's age",
      ctx.sentence(CODEX), "gpt-5.6-sol (as of its turn 3m ago)");
check("a pi session file names its model with the reading's age",
      ctx.sentence(PI), "deepseek-flash (as of its turn 3m ago)");
check("a harness that keeps no transcript is not asked",
      ctx.sentence(OTHER), "");
/* A reading with the model missing is a reading, not a model: it must fall
   to the same "not known" as no reading at all rather than print an empty
   parenthesis after nothing. */
check("a reading with no model in it is still not a model",
      ctx.sentence({ ...FULL, context: { ...READING, model: null } }),
      "not known yet — no context reading recorded");

/* ---- the rail row's chip ----------------------------------------------- */
const chipOf = (s) => {
  const line = ctx.railLine(s);
  return line && descendants(line).find((k) => k.classes.has("rail-model"));
};
const chip = chipOf(FULL);
check("the rail row shows the short name", chip && chip.text, "haiku 4.5");
check("...with the full id one hover away", chip && chip.title,
      "claude-haiku-4-5-20251001");
/* Before the gauge, because the bar's fill only means anything against the
   window the model has — reading the fill first and the label after is
   reading it against nothing. */
const line = ctx.railLine(FULL);
check("it leads the line, ahead of the bar",
      line.kids.map((k) => k.className), ["rail-model", "rail-ctx-bar", "rail-ctx"]);
/* No turn, no model. The empty track and its greyed "?" already say the
   session has not spoken; a second placeholder beside them would read as a
   model called "?" rather than as the same silence said twice. */
check("a session with no reading gets no chip", chipOf(QUIET), undefined);
check("...and still gets its line", !!ctx.railLine(QUIET), true);
check("a harness with no transcript gets no line at all",
      ctx.railLine(OTHER), null);
check("a Codex row shows the model recorded in its rollout",
      [chipOf(CODEX).text, chipOf(CODEX).title],
      ["gpt 5.6 sol", "gpt-5.6-sol"]);
check("a pi row shows the model recorded in its session file",
      [chipOf(PI).text, chipOf(PI).title],
      ["deepseek flash", "deepseek-flash"]);

/* ---- the panel actually appends it ------------------------------------- */
/* The point of driving the real renderSession: the sentence being right is
   worthless if nothing puts it on the panel. Found by the label, not the
   index — this list is re-ordered whenever somebody adds a fact to it. */
function rowsOf(s, data) {
  ctx.render({ session: s, ...(data || {}) });
  const dl = descendants(view).find((k) => k.classes.has("sess-meta"));
  const out = {};
  for (let i = 0; i + 1 < dl.kids.length; i += 2) {
    if (dl.kids[i].tag === "dt") out[dl.kids[i].text] = dl.kids[i + 1];
  }
  return out;
}
const full = rowsOf(FULL);
check("the panel displays a canonical profile and harness once",
      full["profile / harness"] && full["profile / harness"].text,
      "nc/claude");
check("the panel has no duplicate profile or harness rows",
      ["profile" in full, "harness" in full], [false, false]);
check("the panel has a model row", !!full.model, true);
check("...saying the full id", full.model && full.model.text,
      "claude-haiku-4-5-20251001 (as of its turn 3m ago)");
check("...and explaining which turn it is from on hover",
      !!(full.model && full.model.title.includes("latest context reading")), true);
/* Above the count, which is the reason it is a row of its own: the count is
   read against the model, so the model has to be read first. */
const order = Object.keys(full);
check("it sits above the context row",
      order.indexOf("model") < order.indexOf("context"), true);
check("a session that has not answered still gets the row, saying so",
      (rowsOf(QUIET).model || {}).text,
      "not known yet — no context reading recorded");
const codex = rowsOf(CODEX);
check("a Codex detail panel has the full model id",
      codex.model && codex.model.text,
      "gpt-5.6-sol (as of its turn 3m ago)");
check("a Codex detail panel has its context reading",
      codex.context && codex.context.text.includes("187,281 tokens"), true);
const pi = rowsOf(PI);
check("a pi detail panel has the full model id and its context reading",
      [pi.model && pi.model.text,
       pi.context && pi.context.text.includes("14,148 tokens")],
      ["deepseek-flash (as of its turn 3m ago)", true]);
/* metaRow drops an empty value, so the other harness gets no row rather than
   an empty one — the same silence the gauge line keeps. */
check("another harness gets no model row at all",
      "model" in rowsOf(OTHER), false);

/* ---- the end-of-run protection, on the panel ---------------------------- */
/* `cflow kill-on-end` ends the session driving a finished one-shot run, and
   `SessionDef.keep_alive` is the lever that says "record the ending, skip the
   termination". It is drawn beside `restore` because the two are one question
   asked at the two ends of a session's life — `restore` is whether it comes
   back after a daemon restart, this is whether it is allowed to stay after
   its run ends. Only when set: an "off" row on every session would be the
   noise the rail's own version of this flag exists to avoid. */
const PROTECTED = rowsOf({ ...FULL, name: "protected", keep_alive: true });
check("a protected session gets a keep-alive row",
      !!PROTECTED["keep-alive"], true);
check("...saying what the daemon does at the run's end",
      /records its ending but does not end this session/.test(
        (PROTECTED["keep-alive"] || {}).text || ""), true);
check("...and naming the command that lifts it on hover",
      ((PROTECTED["keep-alive"] || {}).title || "")
        .includes("claunch keep-alive protected off"), true);
check("an unprotected session gets no such row",
      "keep-alive" in rowsOf({ ...FULL, name: "plain" }), false);

/* ---- the chip has to be drawable --------------------------------------- */
/* An unfamiliar id from a gateway can be long, and the row is a flex line:
   without a cap it would push the count off the row it is supposed to
   qualify. Pinned here because the failure is invisible to every check
   above — every string is right and the number is off screen. */
check("the stylesheet caps the chip's width",
      /#session-list \.rail-model \{[^}]*max-width:/.test(css), true);
check("...and clips rather than wraps",
      /#session-list \.rail-model \{[^}]*white-space: nowrap;/.test(css), true);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("railmodel_check: all checks passed");
