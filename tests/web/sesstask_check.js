/* The session detail panel's Opening task section.

   The record has been in every session payload the daemon serves for a long
   time (`SessionDef.task`). It once doubled as the rail row's one-line job
   description — the fallback for a session with no LLM briefing; that
   fallback is gone, the row shows the briefing digest only (briefrow_check),
   and this detail section is the one place left where the task is drawn. It
   sits under the facts and above the briefing.

   What has to hold, and what each check below is for:

     as typed      -- an opening task is instructions, usually a list. It is
                      drawn verbatim in a <pre>: no markdown pass, no
                      truncation with an ellipsis, newlines kept.
     empty         -- drawn, not hidden. A session a person opened by hand has
                      no task at all, so a section that appeared only
                      sometimes would read as one that failed to load. The
                      sentence is about the RECORD, never a claim about what
                      the session did.
     long          -- clipped to about eight lines with a toggle, because the
                      briefing, the send box and the board sit under this one
                      and a twenty-line brief would push them off the rail.
     the fold      -- the toggle's state lives in a set OUTSIDE the panel. The
                      detail panel is rebuilt from scratch every 2s, so state
                      held on the node would fold shut under the reader's hand
                      two seconds after they opened it. Per session, too: two
                      sessions' tasks do not share one open flag.

   Where the section sits in the panel is checked here on the source rather
   than by drawing it -- renderSession has one harness (railmodel_check) and
   it stubs every section out, this one included, so the ORDER is the only
   part of that wiring a second harness can honestly pin. */
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

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), title: "", listeners: {},
    appendChild(c) { this.kids.push(c); return c; },
    append(...cs) { this.kids.push(...cs); },
    addEventListener(t, fn) { this.listeners[t] = fn; },
    click() { if (this.listeners.click) this.listeners.click(); },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    all() {
      const out = [this];
      for (const k of this.kids) out.push(...k.all());
      return out;
    },
    find(cls) { return this.all().filter((k) => k.classes.has(cls)); },
    words() { return this.all().map((k) => k.text).join(" "); },
  };
  return n;
}
const document = { createElement: (t) => node(t) };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const ctx = {};
let redraws = 0;
/* The section draws the opening's Re-inject / Copy buttons under the task
   (`sessOpeningActions`). They are not this harness's subject -- their own is
   openinginject_check.js -- so they go in as one marker node, which is also
   what proves the call is guarded by the session name: a nameless row gets
   no marker here. */
function sessOpeningActionsStub() { return el("div", "sess-opening-actions"); }
/* The attachments list asks the daemon once per session. `answers` is what
   the stub daemon says per session name; `asked` records each request. */
const answers = {};
const asked = [];
function api(path) {
  asked.push(path);
  const name = decodeURIComponent(path.split("/")[3]);
  const doc = answers[name];
  return Promise.resolve(doc === undefined
    ? { ok: false, json: () => Promise.resolve({}) }
    : { ok: true, json: () => Promise.resolve(doc) });
}
const url = (path) => "/base/" + path;
new Function("exports", "document", "el", "refreshSession", "sessOpeningActions",
  "api", "url",
  slice("taskIsLong") + slice("sessTask") + slice("sessOpeningAttachments") +
  slice("uploadSize") + `
const sessTaskOpen = new Set();
const sessOpeningAttach = new Map();
Object.assign(exports, {
  task: sessTask, long: taskIsLong, open: sessTaskOpen, attach: sessOpeningAttach,
});`)(ctx, document, el, () => { redraws++; }, sessOpeningActionsStub, api, url);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const SHORT = "web ui - detail 패널에 opening task 표시해줘";
const LONG = Array.from({ length: 14 }, (_, i) => `line ${i + 1}`).join("\n");
const text = (box) => {
  const pre = box.find("sess-task-text")[0];
  return pre ? pre.text : null;
};

/* ---- a recorded task --------------------------------------------------- */
let box = ctx.task({ name: "s209", task: SHORT });
check("the section is drawn", box.classes.has("sess-task"), true);
check("under a heading that says what it is",
      box.kids[0].tag === "h3" && box.kids[0].text, "Opening task");
/* Recorded at creation and typed in once: the panel must not read as a live
   instruction the session is following right now. */
check("...whose hover says it is a record",
      box.kids[0].title.includes("recorded at creation"), true);
check("the task is drawn in full", text(box), SHORT);
check("in a pre, so the shape it was typed in survives",
      box.find("sess-task-text")[0].tag, "pre");
check("a short task is not clipped",
      box.find("sess-task-text")[0].classes.has("clipped"), false);
check("...and gets no toggle", box.find("sess-task-more").length, 0);

/* Verbatim, not rendered. A task is instructions: a markdown pass would eat
   the leading dashes of a list and the asterisks that are part of a glob. */
const MD = "- fix *.js\n- **do not** touch master\n# not a heading";
check("markdown characters survive as characters",
      text(ctx.task({ name: "s1", task: MD })), MD);

/* ---- no record --------------------------------------------------------- */
for (const [what, s] of [["no task field", { name: "s2" }],
                         ["an empty string", { name: "s3", task: "" }],
                         ["whitespace only", { name: "s4", task: "  \n " }]]) {
  box = ctx.task(s);
  check(`${what} still draws the section`, box.classes.has("sess-task"), true);
  check(`-- ${what} draws no text block`, box.find("sess-task-text").length, 0);
  check(`-- ${what} says so in words`,
        box.words().includes("no opening task recorded"), true);
  /* The sentence is about the record. "this session was given nothing to do"
     would be a claim about the session, and a task delivered by hand after
     launch leaves exactly this empty record. */
  check(`-- ${what} does not claim anything about the session's work`,
        /did nothing|no work|idle/.test(box.words()), false);
}

/* ---- the opening buttons belong to a named session --------------------- */
/* They address the daemon by session name, so the section draws them only
   where there is one; their own harness is openinginject_check.js. */
check("a named session's task carries the opening actions",
      ctx.task({ name: "s5", task: SHORT }).find("sess-opening-actions").length, 1);
check("a nameless row does not",
      ctx.task({ task: SHORT }).find("sess-opening-actions").length, 0);

/* ---- long enough to fold ----------------------------------------------- */
check("eight lines is not long", ctx.long("a\nb\nc\nd\ne\nf\ng\nh"), false);
check("nine is", ctx.long("a\nb\nc\nd\ne\nf\ng\nh\ni"), true);
/* The backstop for a task typed as one unbroken paragraph, which is one line
   and can still be half a screen. */
check("one line of 480 characters is not long", ctx.long("x".repeat(480)), false);
check("one line of 481 is", ctx.long("x".repeat(481)), true);

box = ctx.task({ name: "s209", task: LONG });
check("a long task is clipped",
      box.find("sess-task-text")[0].classes.has("clipped"), true);
check("...with the whole of it still in the page, not cut short",
      text(box), LONG);
check("...and a toggle that opens it",
      box.find("sess-task-more").map((n) => n.text), ["Show all"]);
check("...saying how much is folded away",
      box.find("sess-task-more")[0].title.includes("14 lines"), true);

/* ---- the fold survives the poll ---------------------------------------- */
/* The panel is rebuilt from scratch every 2s. The open flag therefore cannot
   live on the node: press the toggle, throw the whole box away, draw a new
   one from the same data -- which is exactly what the poll does -- and the
   task has to still be open. */
const before = redraws;
box.find("sess-task-more")[0].click();
check("the press asks for a redraw rather than waiting for the poll",
      redraws - before, 1);
check("...and is remembered outside the panel", ctx.open.has("s209"), true);
box = ctx.task({ name: "s209", task: LONG });
check("so the rebuilt panel is still open",
      box.find("sess-task-text")[0].classes.has("clipped"), false);
check("...and offers the way back", box.find("sess-task-more")[0].text, "Show less");

/* Per session: the rail can be pointed at another session between two polls,
   and one open task must not unfold every other session's. */
const other = ctx.task({ name: "s210", task: LONG });
check("another session's task is still folded",
      other.find("sess-task-text")[0].classes.has("clipped"), true);

box.find("sess-task-more")[0].click();
check("pressing again folds it back", ctx.open.has("s209"), false);
check("...as the next rebuild draws",
      ctx.task({ name: "s209", task: LONG })
         .find("sess-task-text")[0].classes.has("clipped"), true);

/* ---- what the opening carried besides its text (claunch-5lro4) -------- */
/* Asked once per session, held outside the panel (the 2s rebuild would
   otherwise ask every poll), drawn on the redraw the answer triggers. */
async function attachments() {
  answers.s300 = {
    images: [{ name: "01-" + "a".repeat(20) + ".png", type: "image/png" }],
    files: [{ id: "b".repeat(20), name: "frame trace.utrace", bytes: 2048 },
            { id: "c".repeat(20), name: "gone.json", bytes: 5, missing: true }],
  };
  answers.s301 = { images: [], files: [] };
  // The sessions drawn above asked too; let their answers land first.
  await new Promise((r) => setTimeout(r, 0));
  const drawn = redraws;
  box = ctx.task({ name: "s300", task: SHORT });
  check("the first draw asks the daemon", asked.filter((a) => a.includes("s300")),
        ["/api/sessions/s300/opening/attachments"]);
  check("...and draws nothing while it waits", box.find("sess-task-attach").length, 0);
  await new Promise((r) => setTimeout(r, 0));
  check("the answer asks for a redraw", redraws - drawn, 1);
  box = ctx.task({ name: "s300", task: SHORT });
  check("a rebuild does not ask again", asked.filter((a) => a.includes("s300")).length, 1);
  const att = box.find("sess-task-attach");
  check("the attachments are drawn", att.length, 1);
  check("under the task text, ahead of the opening buttons",
        box.kids.indexOf(att[0]) > box.kids.indexOf(box.find("sess-task-text")[0]) &&
        box.kids.indexOf(att[0]) < box.kids.indexOf(box.find("sess-opening-actions")[0]), true);
  const img = box.find("sess-task-image")[0];
  const src = "/base/api/sessions/s300/opening/images/01-" + "a".repeat(20) + ".png";
  check("an image is a thumbnail", img.kids[0].tag === "img" && img.kids[0].src, src);
  check("...that opens full size", img.href, src);
  const files = box.find("sess-task-file");
  check("each file is listed", files.length, 2);
  check("a file is a download link with its size",
        [files[0].kids[0].tag, files[0].kids[0].href, files[0].words().includes("2.0 KiB")],
        ["a", "/base/api/sessions/s300/opening/files/" + "b".repeat(20) + "/frame%20trace.utrace", true]);
  check("a file no longer on disk is named, not linked",
        [files[1].all().some((n) => n.tag === "a"), files[1].words().includes("no longer on disk")],
        [false, true]);

  ctx.task({ name: "s301", task: SHORT });
  ctx.task({ name: "s302", task: SHORT });
  await new Promise((r) => setTimeout(r, 0));
  check("an opening without attachments draws nothing",
        ctx.task({ name: "s301", task: SHORT }).find("sess-task-attach").length, 0);
  check("nor does one the daemon could not answer for",
        ctx.task({ name: "s302", task: SHORT }).find("sess-task-attach").length, 0);
  check("an empty task still shows what the opening carried",
        ctx.task({ name: "s300" }).find("sess-task-attach").length, 1);
  check("a nameless row asks nothing", (() => {
    const n = asked.length; ctx.task({ task: SHORT }); return asked.length - n;
  })(), 0);
}

/* ---- where it sits in the panel ---------------------------------------- */
/* Source order, for the reason in this file's header: renderSession's only
   harness stubs this section out, so nothing else pins that the call is
   there at all, nor that it comes before the briefing -- which is the whole
   point of the placement. The briefing is a reading OF the task, and a
   reading printed above the thing it reads cannot be compared with it. */
const body = slice("renderSession");
check("renderSession draws it", body.includes("sessTask(s)"), true);
check("after the facts list", body.indexOf("appendChild(dl)") < body.indexOf("sessTask(s)"), true);
check("and before the briefing that summarises it",
      body.indexOf("sessTask(s)") < body.indexOf("sessBriefSection("), true);

attachments().then(() => {
  if (failures) process.exit(1);
  console.log("sesstask_check ok");
});
