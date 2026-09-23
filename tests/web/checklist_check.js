/* Run the real checklist renderers against a stub DOM and read what they drew.

   The `checklist:` gate exists so that a person can see WHICH conditions hold
   without asking the agent, so the rendering is the feature rather than a
   presentation detail of it. Two properties are worth pinning:

   - the three item states stay distinguishable. `false` and "could not
     measure" are different facts -- one says the condition is not met, the
     other says nobody could tell -- and a renderer that folded them together
     would send a reader looking in the wrong place.
   - the page never offers a button. Every other stop on the run page is a
     person's to grant; this one is not, and a card that grew an Approve
     control would be inviting exactly the override the gate refuses. */
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

const code = slice("function cflowLine(", "function cflowHint(");

/* ---- stub DOM --------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(),
    appendChild(c) { this.kids.push(c); return c; },
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

const scope = { document };
new Function("document", code + "\nthis.checklistLines = checklistLines;"
  + "\nthis.checklistMark = checklistMark;"
  + "\nthis.checklistClass = checklistClass;").call(scope, document);

/* ---- assertions ------------------------------------------------------- */
let failures = 0;
function check(name, fn) {
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

const GATE = {
  prompt: "has this branch actually landed?",
  then: "wrapup",
  poll: 60,
  total: 3,
  passed: 1,
  all_true: false,
  report_filed: true,
  items: [
    { id: "merged", describe: "a merge commit lists my tip as a parent",
      ok: false, exit_code: 1, measured_at: "2026-08-28T05:00:00+00:00" },
    { id: "frozen", describe: "the working tree is clean",
      ok: true, exit_code: 0, measured_at: "2026-08-28T05:00:00+00:00" },
    { id: "deployed", describe: "the daemon restarted after the merge",
      ok: null, exit_code: null, measured_at: "2026-08-28T05:00:00+00:00" },
  ],
};

check("the three item states get three different marks", () => {
  eq([scope.checklistMark(true), scope.checklistMark(false),
      scope.checklistMark(null)],
     ["✓", "×", "?"], "marks");
  eq([scope.checklistClass(true), scope.checklistClass(false),
      scope.checklistClass(null)],
     ["ok", "no", "unknown"], "classes");
});

check("every item is drawn, with its own state class", () => {
  const lines = scope.checklistLines(GATE);
  const items = lines.filter((l) => l.classes.has("checklist-item"));
  eq(items.length, 3, "item count");
  eq(items.map((l) => [...l.classes].filter((c) => c !== "cflow-line"
      && c !== "checklist-item")[0]),
     ["no", "ok", "unknown"], "state classes");
  if (!items[0].text.includes("merged")) {
    throw new Error(`item text lost its id: ${items[0].text}`);
  }
  if (!items[0].text.includes("a merge commit lists my tip as a parent")) {
    throw new Error(`item text lost its description: ${items[0].text}`);
  }
});

check("an unmeasurable item does not read as a failing one", () => {
  const lines = scope.checklistLines(GATE);
  const items = lines.filter((l) => l.classes.has("checklist-item"));
  if (!items[2].text.includes("could not measure")) {
    throw new Error(`unmeasurable item reads as: ${items[2].text}`);
  }
  if (items[1].text.includes("could not measure")) {
    throw new Error("a measured item claims it could not be measured");
  }
  if (!items[0].text.includes("exit 1")) {
    throw new Error(`a false item hides its exit code: ${items[0].text}`);
  }
});

check("an item nobody has measured yet says so", () => {
  const fresh = {
    ...GATE, passed: 0,
    items: [{ id: "merged", describe: "d", ok: null, exit_code: null,
              measured_at: null }],
  };
  const items = scope.checklistLines(fresh)
    .filter((l) => l.classes.has("checklist-item"));
  if (!items[0].text.includes("not measured yet")) {
    throw new Error(`fresh item reads as: ${items[0].text}`);
  }
});

check("a ticked item says who ticks it, never 'could not measure'", () => {
  const gate = {
    ...GATE, total: 2, passed: 1,
    items: [
      { id: "approved", describe: "the person looked", ok: false,
        exit_code: null, measured_at: null, by: ["user"],
        path: "steps.landed.checklist.approved" },
      { id: "signed", describe: "signed off", ok: true, exit_code: null,
        measured_at: "2026-09-23T06:00:00+00:00", by: ["user"],
        output: "set by user" },
    ],
  };
  const items = scope.checklistLines(gate)
    .filter((l) => l.classes.has("checklist-item"));
  if (!items[0].text.includes(
      "waits for user to tick it (claunch cflow set "
      + "steps.landed.checklist.approved true)")) {
    throw new Error(`unticked item reads as: ${items[0].text}`);
  }
  if (!items[1].text.includes("set by user")
      || items[1].text.includes("could not measure")) {
    throw new Error(`ticked item reads as: ${items[1].text}`);
  }
});

check("the head counts what is true and names who moves the run", () => {
  const lines = scope.checklistLines(GATE);
  if (!lines[0].text.includes("1/3")) {
    throw new Error(`head lost its count: ${lines[0].text}`);
  }
  const tail = lines[lines.length - 1].text;
  if (!tail.includes("'wrapup'") || !tail.includes("nobody advances")) {
    throw new Error(`the destination line is gone: ${tail}`);
  }
});

check("all true, report missing: the head says what is holding it", () => {
  const held = { ...GATE, passed: 3, all_true: true, report_filed: false };
  const head = scope.checklistLines(held)[0].text;
  if (!head.includes("report")) {
    throw new Error(`head does not name the held condition: ${head}`);
  }
});

check("all true and reported: the head says the daemon has it", () => {
  const going = { ...GATE, passed: 3, all_true: true, report_filed: true };
  const head = scope.checklistLines(going)[0].text;
  if (!head.includes("daemon")) {
    throw new Error(`head does not say who moves it: ${head}`);
  }
});

check("the run page offers no button for this gate", () => {
  /* Read from the source rather than rendered: what matters is that the
     branch does not construct one, and a stub that never gets a click would
     pass either way. */
  const branch = slice('} else if (run.status === "waiting_checklist") {',
                       '} else if (run.status === "waiting_selection"');
  if (/wf-btn/.test(branch)) {
    throw new Error("the checklist branch builds a button — this gate is not "
      + "a person's to grant");
  }
  if (/cflowAction/.test(branch)) {
    throw new Error("the checklist branch posts a cflow action — nothing on "
      + "this page may move a checklist gate");
  }
});

process.exit(failures ? 1 : 0);
