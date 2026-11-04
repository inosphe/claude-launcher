/* The confirm step in front of #new-session's submit, checked against the
   shipped handler and the shipped judgement.

   A session started with permission skipping off keeps Claude's permission
   classifier on, and on a provider that is not Claude's own that classifier
   still calls Claude, which can add Claude usage on top of that provider —
   the cost the provider hint above the form and the running session's strip
   name. So a create that would go out with the box unticked is asked about
   once more (claunch-1ztit.1.1), and the two answers are opposite actions:
   "Yes" starts it as it stands, "No" is the form, with nothing sent.

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
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8").replace(/\r\n/g, "\n"); // CRLF on a Windows checkout

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
    check("...asked over the form it came from, so 'Back' has a place to go",
          [asked.overForm], [true]);
    check("...and it names the cost with the attribution the page already uses",
          [/permission skipping off/.test(asked.body),
           /not Claude's own/.test(asked.body),
           /still calls Claude/.test(asked.body),
           /can add\s+Claude usage/.test(asked.body),
           /usage on that provider/.test(asked.body),
           /charged|billed/.test(asked.body)],
          [true, true, true, true, false, false]);
    check("...and a Yes does not tick the box for the operator",
          [h.state.checked, h.ctx.answer(h.form, null)], [false, false]);

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

    /* The fourth item of the issue: after a Yes the request does go out, and
       it goes out without the skip-permission args. One line adds them, and
       only when the box is ticked — which the confirm leaves as it found it,
       so a Yes on an unticked row sends the body the form was showing. */
    check("the create body carries the skip args only when the box is ticked",
          handler.indexOf(
            "if (f.skip_permissions.checked) body.args.push(...permissionArgs);") > 0,
          true);
    check("...in the claude branch, right after the args are stripped",
          handler.includes(
            "body.args = withoutArgGroups(body.args, [permissionArgs]);\n" +
            "      if (f.skip_permissions.checked) body.args.push(...permissionArgs);"),
          true);
    check("...because the confirm never ticks the box on the operator's behalf",
          /skip_permissions\.checked\s*=/.test(body), false);
  }

  if (failures) {
    console.error(`${failures} failure(s)`);
    process.exit(1);
  }
  console.log("skip-permissions confirm: ok");
})();

/* ---- the question is asked OVER the form, not after closing it ----------
   The confirm is raised from #new-session's own submit, and that form lives
   inside the shared dialog's body. showModal used to close the session modal
   first -- and sessionModalClose puts every row back to what it held when
   the box opened -- so "Back to the form" ended on a form that was gone and
   a box whose typing had been undone (claunch-9mwba). The two halves of the
   fix are pinned here against the shipped functions: the caller asks with
   `overForm`, and showModal parks the form node (never a field) and puts it
   back whatever the answer is. */

let focusedNow = null;   // node.focus() lands here; document.activeElement reads it

function fakeNode(tag) {
  const n = {
    tag, textContent: "", innerText: "", type: "", disabled: false,
    dataset: {}, children: [], listeners: {}, onclick: null,
    classList: {
      set: new Set(),
      add(c) { this.set.add(c); },
      remove(c) { this.set.delete(c); },
      contains(c) { return this.set.has(c); },
      toggle(c, on) { if (on) this.set.add(c); else this.set.delete(c); },
    },
    appendChild(child) { n.children.push(child); return child; },
    addEventListener(ev, fn) { n.listeners[ev] = fn; },
    removeEventListener(ev) { delete n.listeners[ev]; },
    querySelector() { return n.children.find((c) => c.tag === "button") || null; },
    focus() { focusedNow = n; },
  };
  // Both spellings clear the node: showModal writes `textContent = body`,
  // the parking writes `innerText = ""`.
  const clear = { get() { return ""; }, set() { n.children.length = 0; } };
  Object.defineProperty(n, "textContent", {
    get() { return n._text || ""; },
    set(v) { n._text = v; n.children.length = 0; },
  });
  Object.defineProperty(n, "innerHTML", clear);
  Object.defineProperty(n, "innerText", clear);
  return n;
}

/* sliceFrom above starts counting at the first "{" after the name, which is
   the parameter list for a function whose arguments are destructured --
   showModal's are. This one starts at the "{" of the ") {" that opens the
   body. */
function sliceFunction(marker) {
  const start = src.indexOf(marker);
  if (start < 0) throw new Error(`cannot slice ${marker} from app.js`);
  const open = src.indexOf(") {", src.indexOf("(", start)) + 2;
  if (open < 2) throw new Error(`no body after ${marker}`);
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

function overFormWorld() {
  const els = {
    "modal-overlay": fakeNode("div"),
    "modal-title": fakeNode("h2"),
    "modal-body": fakeNode("div"),
    "modal-actions": fakeNode("div"),
    "new-session": fakeNode("form"),
    "new-view": fakeNode("div"),
  };
  els["new-session"].children.push(fakeNode("button"));  // its submit press
  focusedNow = els["new-session"].children[0];
  const doc = {
    get active() { return focusedNow; },
    listeners: {},
    addEventListener(ev, fn) { doc.listeners[ev] = fn; },
    removeEventListener(ev, fn) {
      if (doc.listeners[ev] === fn) delete doc.listeners[ev];
    },
    createElement(tag) { return fakeNode(tag); },
    contains() { return true; },
  };
  const state = { closed: 0, sessionModal: {} };
  const out = {};
  new Function(
    "exports", "$", "document", "sessionModal", "sessionModalClose",
    "sessionModalKey", "syncSessionModalChrome", "refitSoon",
    sliceFunction("function showModal(") +
    sliceFunction("function parkSessionForm(") +
    sliceFunction("function restoreSessionForm(") +
    "\nexports.ask = showModal;\n"
  )(
    out,
    (id) => els[id] || null,
    doc,
    state.sessionModal,
    () => { state.closed++; },
    () => {},
    () => {},
    () => {}
  );
  return { els, doc, state, ask: (opts) => out.ask(opts) };
}

(async () => {
  const ASK = {
    title: "Start without skip permissions?",
    body: "This session starts with permission skipping off.",
    actions: [
      { label: "Back to the form", value: null },
      { label: "Start without skip permissions", value: true },
    ],
    overForm: true,
  };

  {
    const w = overFormWorld();
    const form = w.els["new-session"];
    w.els["modal-body"].appendChild(form);   // where openSessionModal puts it
    const pending = w.ask(ASK);
    check("overForm parks the form out of the dialog body",
          [w.els["modal-body"].children.includes(form),
           w.els["new-view"].children.includes(form)],
          [false, true]);
    check("...without closing the session modal or resetting a row",
          w.state.closed, 0);
    check("...and the question owns the dialog meanwhile",
          [w.els["modal-title"].textContent, w.els["modal-body"].children.includes(form)],
          ["Start without skip permissions?", false]);
    const back = w.els["modal-actions"].children.find(
      (b) => b.tag === "button" && b.textContent === "Back to the form");
    back.listeners.click();
    check("Back to the form answers null", await pending, null);
    check("...and the form is back in the dialog, still open",
          [w.els["modal-body"].children.includes(form),
           w.els["modal-overlay"].classList.contains("hidden")],
          [true, false]);
    check("...under the session modal's own Escape, not the question's",
          [typeof w.doc.listeners.keydown, w.doc.listeners.keydown === undefined],
          ["function", false]);
    check("...and the session modal was never closed", w.state.closed, 0);
    // The question's answers go with the question: left in the bar they
    // stand over a form that never asks them again, and a press on one
    // settles a promise that settled already (claunch-9mwba review).
    check("...with the question's own buttons gone from the action bar",
          w.els["modal-actions"].children.length, 0);
  }

  {
    // The explicit start ("yes") must put the form back just the same: the
    // submit goes on to launch and closes the box itself, and a form left in
    // #new-view would make that close the box with an empty body.
    const w = overFormWorld();
    const form = w.els["new-session"];
    w.els["modal-body"].appendChild(form);
    const pending = w.ask(ASK);
    const yes = w.els["modal-actions"].children.find(
      (b) => b.tag === "button" && b.textContent === "Start without skip permissions");
    yes.listeners.click();
    check("the explicit start answers true", await pending, true);
    check("...and the form is in the dialog for the launch that follows",
          w.els["modal-body"].children.includes(form), true);
    check("...with the session modal still open", w.state.closed, 0);
    check("...and no answer of the question left behind in the bar",
          w.els["modal-actions"].children.length, 0);
  }

  if (failures) {
    console.error(`${failures} failure(s)`);
    process.exit(1);
  }
  console.log("skip-permissions confirm: ok");
})();
