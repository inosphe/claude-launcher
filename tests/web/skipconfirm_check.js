/* The confirm step in front of #new-session's submit, checked against the
   shipped handler and the shipped judgement.

   A session started with permission skipping off keeps Claude's permission
   classifier on, and on a provider that is not Anthropic's own those checks
   are usage on that provider — the cost the provider hint above the form
   warns about. So a create that would go out with the box unticked is asked
   about once more (claunch-1ztit.1.1), and the two answers are opposite
   actions: "Yes" starts it as it stands, "No" is the form, with nothing sent.

   Four things can break silently here:

   - When the question is asked at all. It belongs to the submit that answers
     the row "off" — a ticked row is the operator's own answer, a child whose
     row the spawn policy locked inherits its parent's answer (the child is
     not being started on what this form shows), and a harness with no claude
     runtime panel has no such row. Asking any of those is a second question
     about an answer nobody here gave.
   - Which answer goes with which press. "No", Escape and a backdrop press
     must all be the form; only the explicit start may let the create go out.
   - That nothing is sent before the answer. The check in the handler has to
     sit before the payload is built, or "No" arrives after the request.
   - That "No" is not an error. It writes nothing to #create-error: the rows
     keep what was typed and the operator is back where they were.
*/
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

function sliceFrom(marker) {
  const start = src.indexOf(marker);
  if (start < 0) throw new Error(`cannot slice ${marker} from app.js`);
  const open = src.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") {
      depth--;
      if (depth === 0) return src.slice(start, i + 1) + "\n";
    }
  }
  throw new Error(`unbalanced braces after ${marker}`);
}

/* The three pieces under test, cut out of app.js and run against stubs: the
   panel is the row's own visibility, `newProfileHarnessName` decides which
   harness the form is building, and showModal is where the question goes. */
function harness() {
  const state = {
    harness: "claude", checked: false, disabled: false, hidden: false,
    answer: null,
  };
  const modals = [];
  const panel = {
    classList: { contains: (c) => (c === "hidden" ? state.hidden : false) },
  };
  const form = {
    get skip_permissions() {
      return { checked: state.checked, disabled: state.disabled };
    },
  };
  const ctx = {};
  new Function(
    "exports", "$", "newProfileSelector", "newProfileHarnessName", "showModal",
    sliceFrom("function newClaudePermissionOpen(") +
    sliceFrom("function newSkipPermissionsAnswer(") +
    sliceFrom("async function confirmStartWithoutSkipPermissions(") +
    "\nexports.open = newClaudePermissionOpen;\n" +
    "exports.answer = newSkipPermissionsAnswer;\n" +
    "exports.confirm = confirmStartWithoutSkipPermissions;\n"
  )(
    ctx,
    (id) => (id === "new-claude-runtime" ? panel : null),
    () => "p:claude",
    () => state.harness,
    async (opts) => { modals.push(opts); return state.answer; },
  );
  return { state, modals, form, ctx };
}

(async () => {
  /* ---- which answer the form is giving ---- */
  {
    const h = harness();
    check("a claude create with the box unticked answers off",
          h.ctx.answer(h.form, null), false);
    h.state.checked = true;
    check("...and a ticked box answers on",
          h.ctx.answer(h.form, null), true);

    h.state.checked = false;
    h.state.disabled = true;
    check("a locked row (spawn inheriting the parent) answers nothing here",
          h.ctx.answer(h.form, null), null);

    h.state.disabled = false;
    h.state.hidden = true;
    check("a harness with no claude panel answers nothing here",
          h.ctx.answer(h.form, null), null);
    h.state.harness = "codex";
    check("...whether the panel is hidden or the harness just is not claude",
          h.ctx.answer(h.form, null), null);

    h.state.hidden = false;
    h.state.harness = "claude";
    const parent = { name: "p1", harness: "claude" };
    check("a spawn on a claude child with the box unticked answers off",
          h.ctx.answer(h.form, parent), false);
    h.state.checked = true;
    check("...and a ticked one answers on",
          h.ctx.answer(h.form, parent), true);
    h.state.checked = false;
    h.state.disabled = true;
    check("...and a row the policy locked is the parent's answer, not this one",
          h.ctx.answer(h.form, parent), null);
    check("the open-row gate is the one the spawn body uses",
          src.includes("const claudeOpen = newClaudePermissionOpen(f, harnessName);"),
          true);
  }

  /* ---- the presses ---- */
  {
    const h = harness();
    h.state.answer = true;
    h.state.checked = true;
    check("a ticked row is never asked about",
          [await h.ctx.confirm(h.form, null), h.modals.length], [true, 0]);
    h.state.disabled = true;
    check("...nor is an inherited one",
          [await h.ctx.confirm(h.form, null), h.modals.length], [true, 0]);

    h.state.disabled = false;
    h.state.checked = false;
    check("an unticked row is asked about, and Yes lets the create go out",
          [await h.ctx.confirm(h.form, null), h.modals.length], [true, 1]);
    const asked = h.modals[0];
    check("the question is the page's own modal, with one action per answer",
          [asked.title, asked.actions.map((a) => [a.label, a.value])],
          ["Start without skip permissions?",
           [["Back to the form", null],
            ["Start without skip permissions", true]]]);
    check("...and it names the cost it is about, without asserting a charge",
          [/permission skipping off/.test(asked.body),
           /not Anthropic's own/.test(asked.body),
           /charged|billed/.test(asked.body)],
          [true, true, false]);

    h.state.answer = null;
    check("No keeps the form and sends nothing",
          [await h.ctx.confirm(h.form, null), h.modals.length], [false, 2]);
    h.state.answer = undefined;
    check("an unanswered modal (Escape, a backdrop press) is the same answer",
          await h.ctx.confirm(h.form, null), false);
  }

  /* ---- the handler: nothing is sent before the answer ---- */
  {
    const at = src.indexOf('$("new-session").addEventListener("submit"');
    if (at < 0) throw new Error("cannot locate the submit handler in app.js");
    const handler = src.slice(at);
    const ask = handler.indexOf("confirmStartWithoutSkipPermissions(f, parent)");
    const payload = handler.indexOf(
      "const payload = sessionFormPayload(sessionFormView().mode, body);");
    const busy = handler.indexOf("createBusy = true;");
    check("the handler asks the question",
          ask > 0, true);
    check("...before the payload is built and the button is made busy",
          [ask < payload, ask < busy, payload > 0, busy > 0],
          [true, true, true, true]);
    check("...and a No returns without building one",
          /if \(!\(await confirmStartWithoutSkipPermissions\(f, parent\)\)\) return;/.test(handler),
          true);
    const body = sliceFrom("async function confirmStartWithoutSkipPermissions(");
    check("a No is not an error: the confirm never writes #create-error",
          /create-error/.test(body), false);
    check("the modal is the page's, not the browser's",
          [/showModal\(/.test(body), /window\.confirm/.test(body)], [true, false]);
  }

  if (failures) {
    console.error(`${failures} failure(s)`);
    process.exit(1);
  }
  console.log("skip-permissions confirm: ok");
})();
