/* Run the real run-state and landing-queue renderers against a stub DOM.

   The run page is where a person reads a run, so the two blocks are the
   web face of `editable:` (claunch-36rn5) and `landing_queue:`
   (claunch-3u15l). What is worth pinning:

   - a block exists only when the payload carries its field: a run whose
     workflow declares neither must look exactly as before;
   - a path a person may write gets a control and posts to /api/cflow/state;
     an agent-only path is read-only, and a finished run offers no control;
   - the state box survives a poll while a text field holds an unsaved edit
     (the 5s rebuild must not wipe a half-typed note), and is redrawn when
     the values change otherwise;
   - the landing queue lists every entry and folds the last reset open when
     it carries a board warning. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to, a + 1);
  if (a < 0 || b < 0 || b <= a) throw new Error(`cannot slice ${from} .. ${to}`);
  return src.slice(a, b);
}

const code = slice("let wfStateBoxes = {};",
                   "/* Idle (cwd, scope): offer to start a new run. */");

/* ---- stub DOM --------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), dataset: {}, listeners: {},
    value: "", checked: false, disabled: false, open: false,
    appendChild(c) { this.kids.push(c); return c; },
    addEventListener(ev, fn) { this.listeners[ev] = fn; },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
  };
  Object.defineProperty(n, "className", {
    get() { return [...n.classes].join(" "); },
    set(v) { n.classes = new Set(String(v).split(" ").filter(Boolean)); },
  });
  return n;
}
const document = { createElement: (tag) => node(tag) };
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}
const posted = [];
function cflowAction(p, body, after) { posted.push({ path: p, body, after }); }

const scope = {};
new Function("document", "el", "cflowAction",
  code + "\nthis.wfStateBlock = wfStateBlock;"
  + "\nthis.wfLandingBlock = wfLandingBlock;"
  + "\nthis.reset = () => { wfStateBoxes = {}; };"
).call(scope, document, el, cflowAction);

/* ---- helpers ---------------------------------------------------------- */
function all(n, pred, out = []) {
  if (pred(n)) out.push(n);
  for (const k of n.kids || []) all(k, pred, out);
  return out;
}
const byClass = (n, c) => all(n, (x) => x.classes && x.classes.has(c));
const texts = (n) => all(n, () => true).map((x) => x.text).join(" | ");

let failures = 0;
function check(name, fn) {
  scope.reset();
  posted.length = 0;
  try {
    fn();
    console.log(`ok   ${name}`);
  } catch (e) {
    failures += 1;
    console.log(`FAIL ${name}\n     ${e.message}`);
  }
}
function eq(got, want, what) {
  const a = JSON.stringify(got);
  const b = JSON.stringify(want);
  if (a !== b) throw new Error(`${what || ""} got ${a} want ${b}`);
}

const UI = { host: "page", actions: {} };
const STATE = [
  { path: "steps.review.skip", type: "bool", by: ["user"], value: false,
    describe: "skip the review step" },
  { path: "notes", type: "text", by: ["user"], value: "keep it small",
    set_by: "user", set_at: "2026-09-23T11:00:00+00:00" },
  { path: "auto", type: "bool", by: ["agent"], value: false },
];
/* The page's data is GET /api/cflow/run: the run payload rides in `run`. */
function data(run = {}) {
  return {
    cwd: "C:/proj", scope: "s1",
    run: { status: "step", state: STATE, ...run },
  };
}
const lq = (run) => ({ cwd: "C:/proj", scope: "s1", run });

/* ---- run state -------------------------------------------------------- */
check("no editable declaration, no state block", () => {
  eq(scope.wfStateBlock({ cwd: "C:/proj", scope: "s1", run: {} }, UI), null);
  eq(scope.wfStateBlock({ cwd: "C:/proj", scope: "s1" }, UI), null);
  eq(scope.wfStateBlock(data({ state: [] }), UI), null);
});

check("every declared path is a row, with who may write it", () => {
  const box = scope.wfStateBlock(data(), UI);
  const rows = byClass(box, "wf-state-row");
  eq(rows.map((r) => r.dataset.path),
     ["steps.review.skip", "notes", "auto"]);
  const all_ = texts(box);
  if (!/written by user/.test(all_)) throw new Error("no writer line: " + all_);
  if (!/last set by user at 2026-09-23 11:00/.test(all_)) {
    throw new Error("no last-writer line: " + all_);
  }
});

check("a person's bool posts the new value as a state write", () => {
  const box = scope.wfStateBlock(data(), UI);
  const cb = byClass(box, "wf-state-bool")[0];
  eq(cb.disabled, false, "user bool disabled");
  cb.checked = true;
  cb.listeners.change();
  eq(posted.map((p) => [p.path, p.body]), [["/api/cflow/state", {
    cwd: "C:/proj", scope: "s1", path: "steps.review.skip", value: true,
  }]]);
});

check("a person's text is edited and saved; an agent path is read-only", () => {
  const box = scope.wfStateBlock(data(), UI);
  const areas = byClass(box, "wf-state-text");
  eq(areas.length, 1, "text fields");
  eq(areas[0].value, "keep it small");
  areas[0].value = "keep it smaller";
  byClass(box, "wf-state-save")[0].listeners.click();
  eq(posted[0].body, {
    cwd: "C:/proj", scope: "s1", path: "notes", value: "keep it smaller",
  });
  eq(byClass(box, "wf-state-bool").map((n) => n.disabled), [false, true],
     "the agent-only bool is read-only");
});

check("a finished run offers no control", () => {
  const box = scope.wfStateBlock(data({ status: "done" }), UI);
  eq(byClass(box, "wf-state-text").length, 0, "text fields");
  eq(byClass(box, "wf-state-save").length, 0, "save buttons");
  eq(byClass(box, "wf-state-bool")[0].disabled, true, "bool disabled");
  eq(byClass(box, "wf-state-value").map((n) => n.text), ["keep it small"],
     "the note is shown as text");
});

check("a poll keeps a half-typed note and redraws on a new value otherwise", () => {
  const first = scope.wfStateBlock(data(), UI);
  eq(scope.wfStateBlock(data(), UI) === first, true, "same values, same box");
  byClass(first, "wf-state-text")[0].listeners.input();
  const changed = STATE.map((e) =>
    e.path === "auto" ? { ...e, value: true } : e);
  eq(scope.wfStateBlock(data({ state: changed }), UI) === first, true,
     "an unsaved edit survives a changed poll");
  scope.reset();
  const clean = scope.wfStateBlock(data(), UI);
  eq(scope.wfStateBlock(data({ state: changed }), UI) === clean, false,
     "no edit in hand: a changed value redraws");
});

/* ---- landing queue ---------------------------------------------------- */
check("no landing_queue declaration, no landing block", () => {
  eq(scope.wfLandingBlock(lq({})), null);
  eq(scope.wfLandingBlock({}), null);
});

check("an empty queue says so", () => {
  const box = scope.wfLandingBlock(lq({ landing_queue: [] }));
  if (!/landing queue \(0\)/.test(texts(box))) throw new Error(texts(box));
  if (!/empty/.test(texts(box))) throw new Error(texts(box));
});

check("every entry is a row with issue, status, branch @ tip and requester", () => {
  const box = scope.wfLandingBlock(lq({ landing_queue: [
    { issue: "claunch-a1", status: "waiting", branch: "s1-x",
      tip: "7faf432c0b0f28e9", requested_by: "s1",
      requested_at: "2026-09-23T10:00:00+00:00", note: "this round" },
    { issue: "claunch-b2", status: "deferred", branch: "s2-y", tip: "abc" },
  ] }));
  const rows = byClass(box, "wf-landing-row");
  eq(rows.length, 2, "rows");
  const t = texts(box);
  for (const want of ["claunch-a1", "waiting", "s1-x @ 7faf432c",
                      "s1 · 2026-09-23 10:00", "this round", "claunch-b2",
                      "deferred"]) {
    if (!t.includes(want)) throw new Error(`missing ${want}: ${t}`);
  }
});

check("the last reset is shown, and opened when it carries a warning", () => {
  const quiet = scope.wfLandingBlock(lq({ landing_queue: [], landing_reset: {
    run: "run-1", dropped: [{ issue: "claunch-a1", status: "landed" }],
    carried: [{ issue: "claunch-b2", status: "requested", was: "deferred" }],
    warnings: [],
  } }));
  const fold = byClass(quiet, "wf-landing-reset")[0];
  eq(fold.open, false, "quiet reset folded");
  const t = texts(fold);
  if (!t.includes("dropped: claunch-a1 (landed)")) throw new Error(t);
  if (!t.includes("carried: claunch-b2 (requested, was deferred)")) throw new Error(t);
  const loud = scope.wfLandingBlock(lq({ landing_queue: [], landing_reset: {
    run: "run-1", dropped: [], carried: [],
    warnings: ["claunch-c3: queued (requested) but the board has it closed"],
  } }));
  const lfold = byClass(loud, "wf-landing-reset")[0];
  eq(lfold.open, true, "warning opens the fold");
  eq(byClass(lfold, "wf-warning").length, 1, "warning lines");
});

check("the landing block posts nothing", () => {
  /* Read from the source: moving entries is the leader's MCP tool, and
     `landed` is the daemon's git measurement -- a button here would be a
     third writer nobody asked for. */
  const fn = slice("function wfLandingBlock(",
                   "/* Idle (cwd, scope): offer to start a new run. */");
  if (/cflowAction|wf-btn/.test(fn)) {
    throw new Error("the landing block builds a control");
  }
});

process.exit(failures ? 1 : 0);
