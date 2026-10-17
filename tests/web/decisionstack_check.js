/* The create form's decision stack (claunch-ymjp8), checked against the
   shipped markup, stylesheet and app.js.

   The form asks eight groups of questions; once the reader has scrolled past
   a group, its header is repeated at the top of the form with the group's
   answers on the same line. What can break silently:

   - The list of groups. DECISION_SECTIONS must name every numbered group on
     the form, in form order: a group added to the markup and not to the list
     simply never stacks, and nothing else would notice.
   - The box's placement and shape. It must sit inside the form ahead of the
     first group (so it is at the top while the form scrolls), sticky, and of
     zero height: a box that grows in flow pushes the form down under the
     reader's scroll position every time a row is added.
   - The row height. app.js measures the stack in DECISION_ROW_PX; the
     stylesheet draws the rows. Two numbers for one fact drift apart, and the
     stack then decides a header has passed while it is still visible.
   - decisionSummary, run against stub forms: each group says its answers,
     a row the form hides does not speak, and the four board answers read
     differently.
   - decisionRowsPassed, run against stub rects: only headers above the
     stack's lower edge stack, the edge moves a row per group taken, and a
     hidden group never stacks. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

let failures = 0;
function check(name, got, want) {
  const a = JSON.stringify(got), b = JSON.stringify(want);
  if (a === b) console.log(`ok   ${name}`);
  else { failures++; console.log(`FAIL ${name}\n     got  ${a}\n     want ${b}`); }
}

function sliceFn(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  let depth = 0;
  for (let j = src.indexOf("{", src.indexOf(")", start)); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}
function sliceConst(name) {
  const start = src.indexOf(`const ${name} =`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  return src.slice(start, src.indexOf(";", start) + 1);
}

/* ---- the groups, as the markup numbers them ---- */
const formStart = html.indexOf('<form id="new-session">');
const formEnd = html.indexOf("</form>", formStart);
const form = html.slice(formStart, formEnd);
const numbered = [...form.matchAll(
  /<(?:fieldset|details) id="([\w-]+)"[^>]*>\s*<(?:legend|summary)><span class="sess-spawn-step">(\d+)<\/span>/g,
)].map((m) => ({ id: m[1], step: Number(m[2]) }));
const SECTIONS = new Function(`${sliceConst("DECISION_SECTIONS")} return DECISION_SECTIONS;`)();
check("the form numbers eight groups, 1 to 8 in order",
      numbered.map((g) => g.step), [1, 2, 3, 4, 5, 6, 7, 8]);
check("DECISION_SECTIONS is every numbered group, in form order",
      SECTIONS, numbered.map((g) => g.id));

/* ---- the box ---- */
const boxAt = form.indexOf('<div id="new-decisions"');
check("the stack is inside the form", boxAt > 0, true);
check("...ahead of every group",
      numbered.every((g) => form.indexOf(`id="${g.id}"`) > boxAt), true);
check("...and ahead of the quick fork's fieldset too",
      form.indexOf('id="new-quick-fork"') > boxAt, true);

function rule(selector) {
  const at = css.indexOf(`${selector} {`);
  if (at < 0) return "";
  return css.slice(at, css.indexOf("}", at));
}
const boxRule = rule("#new-session > #new-decisions");
check("the box is sticky at the top, with no height of its own",
      [/position:\s*sticky/.test(boxRule), /top:\s*0/.test(boxRule),
       /height:\s*0/.test(boxRule)],
      [true, true, true]);
check("the list hangs from it, absolutely placed",
      /position:\s*absolute/.test(rule("#new-decisions-list")), true);
check("an empty list draws nothing", /display:\s*none/.test(rule("#new-decisions-list:empty")), true);

const ROW_PX = new Function(`${sliceConst("DECISION_ROW_PX")} return DECISION_ROW_PX;`)();
const rowRule = rule("#new-session .new-decision-row");
check("the row height in the stylesheet is DECISION_ROW_PX",
      (rowRule.match(/\bheight:\s*(\d+)px/) || [])[1], String(ROW_PX));
check("a row is one line, the answers cut with an ellipsis",
      [/white-space:\s*nowrap/.test(rule(".new-decision-sum")),
       /text-overflow:\s*ellipsis/.test(rule(".new-decision-sum"))],
      [true, true]);

/* ---- decisionSummary ---- */
const summary = new Function(
  [sliceFn("decisionOptionText"), sliceFn("decisionFirstLine"),
   sliceFn("decisionSummary"), "return decisionSummary;"].join("\n"),
)();
function cls(hidden) {
  return { contains: (c) => c === "hidden" && hidden };
}
function sel(value, text) {
  return { value, selectedOptions: value === undefined ? [] : [{ textContent: text ?? value }] };
}
function box(value) { return { value }; }
function rows(hidden) {
  const map = {};
  for (const [id, h] of Object.entries(hidden)) map[id] = { classList: cls(h) };
  return (id) => map[id] || null;
}

const top = {
  project: sel("claunch"), cwd: sel("/w/claunch", "claunch — /w/claunch"),
  parent: sel(""), fork_parent: { checked: false }, name: box("  "),
  profile: sel("nc"), harness: sel("claude"), model: sel("opus"), effort: sel("high"),
  mesh: sel("claunch"), handle: box("w1"), role: sel("worker"),
  workflow: sel("improv-worker-v2", "improv-worker-v2 (v2)"),
  worktree_mode: box(""), worktree_name: box(""), worktree_from: sel(""),
  worktree_existing: sel(""), worktree_rebase: box(""),
  task: box("\n  fix the rail\nsecond line"), beads: box("new"), issue: sel(""),
  issue_text: box(""),
};
const get = rows({ "new-model-row": true, "new-effort-row": false, "new-handle-row": true });
const say = (id, f, opts = {}) => summary(id, f, { get, ...opts });

check("Where: project and directory as the pickers show them",
      say("new-where", top), "claunch · claunch — /w/claunch");
check("Identity: no parent is top-level, an empty name is auto",
      say("new-identity", top), "top-level · name auto");
check("Identity: a child names its parent and the fork",
      say("new-identity", { ...top, parent: sel("s29"), fork_parent: { checked: true },
                            name: box("s29-a") }),
      "child of s29 (--fork) · s29-a");
check("Runs as: a hidden model row does not speak, a shown effort row does",
      say("new-runs-on", top), "nc · claude · effort high");
check("Start it working: mesh, role, workflow; a hidden handle row is silent",
      say("new-onboard", top), "mesh claunch · role worker · workflow improv-worker-v2 (v2)");
check("Start it working: the handle speaks once its row shows",
      summary("new-onboard", top, { get: rows({ "new-handle-row": false }) }),
      "mesh claunch · as w1 · role worker · workflow improv-worker-v2 (v2)");
check("Start it working: nothing picked says so",
      say("new-onboard", { ...top, mesh: sel(""), role: sel(""), workflow: sel("") }),
      "no mesh · no role · no workflow");

const runtimeSum = (text) => (id) => (id === "new-runtime-sum" ? { textContent: text } : get(id));
check("How it runs: the fold's own summary, without its dash",
      summary("new-runtime", top, { get: runtimeSum("— borrow work · +args") }),
      "borrow work · +args");
check("How it runs: an empty summary is the defaults",
      summary("new-runtime", top, { get: runtimeSum("") }), "defaults");

check("Worktree: none", say("new-worktree", top), "no worktree");
check("Worktree: a new one, named and branched",
      say("new-worktree", { ...top, worktree_mode: box("new"), worktree_name: box("s1-x"),
                            worktree_from: sel("master") }),
      "new s1-x · from master");
check("Worktree: a reused one and its rebase",
      say("new-worktree", { ...top, worktree_mode: box("existing"),
                            worktree_existing: sel("/w/a", "a (s12)"),
                            worktree_rebase: box("master") }),
      "reuse a (s12) · rebase onto master");

check("Opening task: its first non-empty line",
      say("new-task", top), "fix the rail");
check("Opening task: empty, with images counted",
      say("new-task", { ...top, task: box("") }, { images: 2 }), "empty · +2 images");

check("Board: the four answers read differently",
      ["new", "existing", "none", "none-auto"].map((v) =>
        say("new-beads", { ...top, beads: box(v), issue: sel(v === "existing" ? "claunch-x" : "") })),
      ["new issue from the task", "issue claunch-x",
       "no issue — waits for instructions", "no issue — picks its own"]);
check("Board: a new issue with its own text names its title",
      say("new-beads", { ...top, issue_text: box("Title line\nbody") }), "new issue: Title line");
check("Board: existing with nothing picked says so",
      say("new-beads", { ...top, beads: box("existing") }), "existing (none picked)");

/* ---- decisionRowsPassed ---- */
const passed = new Function(
  [sliceFn("decisionHead"), sliceFn("decisionRowsPassed"),
   "return decisionRowsPassed;"].join("\n"),
)();
function section(id, headTop, hidden = false) {
  const head = { tagName: "LEGEND", getBoundingClientRect: () => ({ top: headTop }) };
  return {
    id, children: [head],
    classList: { contains: (c) => hidden && c === "session-field-hidden" },
  };
}
const ids = (list) => list.map((r) => r.section.id);
check("nothing scrolled: no header is above the stack",
      ids(passed([section("a", 10), section("b", 200)], 0, ROW_PX)), []);
check("a header above the stack's top stacks",
      ids(passed([section("a", -40), section("b", 200)], 0, ROW_PX)), ["a"]);
check("the edge moves a row per group taken",
      ids(passed([section("a", -40), section("b", ROW_PX - 1), section("c", 2 * ROW_PX + 1)],
                 0, ROW_PX)),
      ["a", "b"]);
check("a hidden group never stacks",
      ids(passed([section("a", -40, true), section("b", -20)], 0, ROW_PX)), ["b"]);
check("two groups side by side stack together",
      ids(passed([section("a", -40), section("b", -40)], 0, ROW_PX)), ["a", "b"]);

/* ---- wiring ---- */
check("renderRuntimeSummary schedules the stack, guarded for sliced checks",
      /typeof scheduleDecisionStack === "function"\) scheduleDecisionStack\(\)/
        .test(sliceFn("renderRuntimeSummary")),
      true);
check("renderOpeningImages schedules the stack, so +N images follows add/remove/clear",
      /typeof scheduleDecisionStack === "function"\) scheduleDecisionStack\(\)/
        .test(sliceFn("renderOpeningImages")),
      true);
check("the stack follows scrolling of whichever box holds the form",
      /document\.addEventListener\("scroll"[\s\S]{0,200}contains\(f\)/.test(src), true);

if (failures) {
  console.log(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall decision-stack checks passed");
