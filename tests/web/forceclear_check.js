/* The confirm/report modal and the forced clear/delete, run against the real
   showModal and offerForce from app.js.

   The modal replaced the browser's confirm()/alert() on the session-ending
   flows for one reason: a record a mesh row still names used to be a wall
   ("remove them from the roster, then clear again"), and the wall is now a
   question with a third button — force, which re-issues the same call with
   ?force=1 so the daemon releases the rosters itself. What has to hold is
   the question's protocol: every way of not answering (Escape, the
   backdrop, Cancel) is one shape (null), a stray Enter lands on the safe
   button, and the forced re-issue really carries force=1 — appended with
   the right separator whether the path already has a query or not — and
   still reports the memberships that would not release. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

/* Slice one function out of the shipped file by balancing its braces. The
   bodies contain template literals whose ${...} pairs are balanced, so the
   count survives them. */
function slice(decl) {
  const a = src.indexOf(decl);
  if (a < 0) throw new Error(`cannot locate ${decl} in app.js`);
  let depth = 0, end = -1;
  for (let i = src.indexOf(") {", a) + 2; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && !--depth) { end = i + 1; break; }
  }
  if (end < 0) throw new Error(`unbalanced ${decl}`);
  return src.slice(a, end);
}

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* ---------------- showModal: the dialog's own protocol ---------------- */

function stubDom() {
  const els = {};
  for (const id of ["modal-overlay", "modal-title", "modal-body"]) {
    const el = {
      id, textContent: "", onclick: null,
      children: [],
      classes: new Set(id === "modal-overlay" ? ["hidden"] : []),
      classList: {
        add: (c) => el.classes.add(c),
        remove: (c) => el.classes.delete(c),
      },
      appendChild(c) { this.children.push(c); },
    };
    els[id] = el;
  }
  const row = {
    id: "modal-actions", children: [],
    set innerHTML(v) { if (v === "") this.children = []; },
    appendChild(c) { this.children.push(c); },
    querySelector() { return this.children[0] || null; },
  };
  els["modal-actions"] = row;
  const doc = {
    keydown: [],
    createTextNode(text) { return { textContent: text }; },
    createElement() {
      const el = {
        type: "", textContent: "", focused: false, disabled: false,
        checked: false, children: [],
        classes: new Set(), handlers: {},
        classList: {
          add: (c) => el.classes.add(c),
          toggle: (c, on) => (on ? el.classes.add(c) : el.classes.delete(c)),
        },
        addEventListener(ev, fn) { el.handlers[ev] = fn; },
        append(...kids) { el.children.push(...kids); },
        appendChild(kid) { el.children.push(kid); },
        click() { if (el.handlers.click) el.handlers.click(); },
        focus() { el.focused = true; },
      };
      return el;
    },
    addEventListener(ev, fn) { doc.keydown.push(fn); },
    removeEventListener(ev, fn) {
      doc.keydown = doc.keydown.filter((f) => f !== fn);
    },
  };
  const ctx = {};
  new Function("exports", "$", "document",
               slice("function showModal(") +
               "\nexports.showModal = showModal;")(
    ctx, (id) => els[id] || null, doc
  );
  return { showModal: ctx.showModal, els, doc };
}

async function checkShowModal() {
  {
    const { showModal, els, doc } = stubDom();
    const p = showModal({
      title: "Delete?", body: "s1, s2",
      actions: [
        { label: "Cancel", value: null },
        { label: "Delete", value: true, danger: true },
      ],
    });
    const row = els["modal-actions"];
    check("the overlay comes up for the question",
          els["modal-overlay"].classes.has("hidden"), false);
    check("title and body carry the question",
          [els["modal-title"].textContent, els["modal-body"].textContent],
          ["Delete?", "s1, s2"]);
    check("every action is a button, in order",
          row.children.map((b) => b.textContent), ["Cancel", "Delete"]);
    check("the destructive answer reads as what it is",
          row.children.map((b) => b.classes.has("danger")), [false, true]);
    check("a stray Enter lands on the safe answer",
          row.children.map((b) => b.focused), [true, false]);
    row.children[1].click();
    check("pressing an answer resolves its value", await p, true);
    check("the overlay goes away with the answer",
          els["modal-overlay"].classes.has("hidden"), true);
    check("the Escape listener does not outlive the asking",
          doc.keydown.length, 0);
  }
  {
    const { showModal, doc } = stubDom();
    const p = showModal({ title: "t", body: "b",
                          actions: [{ label: "OK", value: true }] });
    doc.keydown.forEach((fn) => fn({ key: "Escape" }));
    check("Escape is a null answer", await p, null);
  }
  {
    const { showModal, els } = stubDom();
    const p = showModal({ title: "t", body: "b",
                          actions: [{ label: "OK", value: true }] });
    const overlay = els["modal-overlay"];
    overlay.onclick({ target: {} });   // a click inside the box is not an answer
    check("a click inside the box leaves the question up",
          overlay.classes.has("hidden"), false);
    overlay.onclick({ target: overlay });
    check("the backdrop is a null answer", await p, null);
  }
  {
    const { showModal, els } = stubDom();
    const p = showModal({
      title: "Force remove?", body: "still in mesh0",
      checkbox: { label: "I understand" },
      actions: [
        { label: "Cancel", value: null },
        { label: "Remove", value: true, requiresCheck: true },
      ],
    });
    const checkBox = els["modal-body"].children[0].children[0];
    const remove = els["modal-actions"].children[1];
    check("a guarded action starts disabled", remove.disabled, true);
    checkBox.checked = true;
    checkBox.handlers.change();
    check("checking the acknowledgement enables it", remove.disabled, false);
    remove.click();
    check("the enabled guarded action resolves", await p, true);
  }
  {
    // A radio group: the answer merges into the pressed action's value, and
    // the button follows the picked option's danger, so "remove them too" is
    // not read from a button that still looks like the safe one.
    const { showModal, els } = stubDom();
    const p = showModal({
      title: "Remove?", body: "b",
      choices: {
        options: [
          { label: "Move them up", value: { children: "escalate" } },
          { label: "Remove them too", value: { children: "remove" },
            destructive: true },
        ],
      },
      actions: [
        { label: "Cancel", value: null },
        { label: "Remove", value: { force: false }, dangerWhen: "destructive" },
      ],
    });
    const group = els["modal-body"].children[0];
    const remove = els["modal-actions"].children[1];
    check("every option is a radio, first one selected",
          group.children.map((c) => c.children[0].checked), [true, false]);
    check("the safe default leaves the button undressed",
          remove.classes.has("danger"), false);
    group.children[1].children[0].handlers.change();
    check("picking the destructive option marks the button",
          remove.classes.has("danger"), true);
    remove.click();
    check("the answer carries both the action's value and the choice's",
          await p, { force: false, children: "remove" });
  }
  {
    const { showModal, els } = stubDom();
    const p = showModal({
      title: "Remove?", body: "b",
      choices: { options: [{ label: "a", value: { children: "escalate" } }] },
      actions: [{ label: "Cancel", value: null }],
    });
    els["modal-actions"].children[0].click();
    check("cancel stays null even with a choice picked", await p, null);
  }
}

/* ------------- offerForce: the hold turned into a question ------------- */

function harness(results, answers) {
  const calls = [], modals = [], infos = [];
  const bulkAction = async (btn, p, opts, verb) => {
    calls.push({ path: p, method: opts.method, verb });
    return results.shift();
  };
  const showModal = async (q) => { modals.push(q); return answers.shift(); };
  const modalInfo = async (title, body) => { infos.push({ title, body }); };
  const ctx = {};
  new Function("exports", "bulkAction", "showModal", "modalInfo",
               slice("async function offerForce(") +
               "\nexports.offerForce = offerForce;")(
    ctx, bulkAction, showModal, modalInfo
  );
  return { offerForce: ctx.offerForce, calls, modals, infos };
}

const HELD = {
  removed: [],
  kept: [{ name: "w1", meshes: [{ mesh: "gds", handle: "w1" }] }],
};

async function checkOfferForce() {
  {
    const h = harness([{ removed: ["a"], kept: [] }], []);
    const r = await h.offerForce(null, "/api/sessions", "clear");
    check("nothing held asks nothing",
          [h.calls.length, h.modals.length, r.removed], [1, 0, ["a"]]);
  }
  {
    const h = harness([HELD], [null]);
    const r = await h.offerForce(null, "/api/sessions", "clear");
    check("a hold is one question, not a second call",
          [h.calls.length, h.modals.length], [1, 1]);
    check("the question names the held record and its mesh",
          /w1.*gds/.test(h.modals[0].body) || /w1.*gds/.test(h.modals[0].title),
          true);
    check("its answers are keep or force, force marked destructive",
          h.modals[0].actions.map((a) => [a.label, !!a.danger]),
          [["Keep them", false], ["Force clear", true]]);
    check("declining keeps the first result", r.kept.length, 1);
  }
  {
    const h = harness([HELD, { removed: ["w1"], kept: [] }], [true]);
    const r = await h.offerForce(null, "/api/sessions", "clear");
    check("forcing re-issues the same call with force=1",
          h.calls.map((c) => c.path),
          ["/api/sessions", "/api/sessions?force=1"]);
    check("and hands back the forced result", r.removed, ["w1"]);
    check("a clean forced result reports nothing", h.infos.length, 0);
  }
  {
    const h = harness([HELD, { removed: [], kept: [] }], [true]);
    await h.offerForce(null, "/api/sessions?running=1", "delete");
    check("a path already carrying a query gets & rather than a second ?",
          h.calls[1].path, "/api/sessions?running=1&force=1");
  }
  {
    const still = {
      removed: [],
      kept: [{ name: "w1",
               meshes: [{ mesh: "gds", handle: "w1",
                          error: "primary unreachable" }] }],
    };
    const h = harness([HELD, still], [true]);
    await h.offerForce(null, "/api/sessions", "clear");
    check("a membership that would not release is reported, with the refusal",
          h.infos.length === 1 && /primary unreachable/.test(h.infos[0].body),
          true);
  }
}

/* -------- individual remove: membership is decided before DELETE -------- */

function removeHarness(meshes, answer, response = { ok: true }, sessions = []) {
  const calls = [], modals = [], infos = [];
  const ctx = {};
  const api = async (url, opts) => {
    calls.push({ url, method: opts.method });
    return { ...response, json: async () => ({ error: "refused" }) };
  };
  const showModal = async (q) => { modals.push(q); return answer; };
  const modalInfo = async (title, body) => infos.push({ title, body });
  const refreshSessions = () => {};
  const detach = () => {};
  const location = { hash: "#/s/s1" };
  new Function(
    "exports", "sessMeshes", "showModal", "api", "modalInfo",
    "refreshSessions", "detach", "location", "sessionsCache",
    slice("function sessionSubtree(") + "\n" +
    slice("async function removeExitedSession(") +
      "\nexports.removeExitedSession = removeExitedSession;"
  )(ctx, () => meshes, showModal, api, modalInfo,
    refreshSessions, detach, location, sessions);
  return { remove: ctx.removeExitedSession, calls, modals, infos, location };
}

async function checkIndividualRemove() {
  {
    const h = removeHarness([], { force: false });
    check("a non-mesh remove uses one dialog and a plain DELETE",
          [await h.remove("s1"), h.modals.length, h.calls],
          [true, 1, [{ url: "/api/sessions/s1", method: "DELETE" }]]);
    check("non-mesh remove needs no acknowledgement",
          [h.modals[0].checkbox, h.modals[0].actions[1].requiresCheck],
          [null, false]);
  }
  {
    const h = removeHarness([{ mesh: "mesh0" }], { force: true });
    const removed = await h.remove("s 1");
    check("a mesh remove names its mesh and gates the same dialog",
          [/mesh0/.test(h.modals[0].body),
           h.modals[0].actions[1].requiresCheck],
          [true, true]);
    check("mesh acknowledgement makes the first DELETE forced",
          [removed, h.calls],
          [true, [{ url: "/api/sessions/s%201?force=1", method: "DELETE" }]]);
    check("mesh remove still used only one dialog", h.modals.length, 1);
  }
  {
    const h = removeHarness([{ mesh: "mesh0" }], null);
    check("cancel sends no DELETE", [await h.remove("s1"), h.calls],
          [false, []]);
  }
}

/* ---- the sessions under it: promoted by default, dropped only if asked ---- */

/* lead -> mid -> (kid -> grand, kid2). Dropping mid's record used to leave
   kid and kid2 naming a session that is gone, which the tree reads as "no
   parent" — so the modal asks instead, and the answer rides the DELETE. */
const TREE = [
  { name: "lead", parent: "", status: "exited" },
  { name: "mid", parent: "lead", status: "exited" },
  { name: "kid", parent: "mid", status: "idle" },
  { name: "kid2", parent: "mid", status: "exited" },
  { name: "grand", parent: "kid", status: "idle" },
];

async function checkChildrenQuestion() {
  {
    const h = removeHarness([], { force: false, children: "escalate" },
                            { ok: true }, TREE);
    await h.remove("mid");
    const q = h.modals[0];
    check("a session with children is asked what happens to them",
          q.choices.options.map((o) => o.value),
          [{ children: "escalate" }, { children: "remove" }]);
    check("the whole subtree is named, not just the direct children",
          /kid, kid2, grand/.test(q.body), true);
    check("the promotion names the grandparent it moves them to",
          /Move them up to 'lead'/.test(q.choices.options[0].label), true);
    check("the destructive answer is marked so the button can follow it",
          [q.choices.options[0].destructive, q.choices.options[1].destructive],
          [undefined, true]);
    check("a still-running session under it is counted in the warning",
          / 2 of them is still running/.test(q.choices.options[1].hint), true);
    check("escalate is the default and adds nothing to the URL",
          h.calls, [{ url: "/api/sessions/mid", method: "DELETE" }]);
  }
  {
    const h = removeHarness([], { force: false, children: "remove" },
                            { ok: true }, TREE);
    await h.remove("mid");
    check("the cascade answer is the one thing that changes the call",
          h.calls,
          [{ url: "/api/sessions/mid?children=remove", method: "DELETE" }]);
  }
  {
    const h = removeHarness([{ mesh: "mesh0" }], { force: true, children: "remove" },
                            { ok: true }, TREE);
    await h.remove("mid");
    check("force and the cascade travel together, one query",
          h.calls,
          [{ url: "/api/sessions/mid?force=1&children=remove",
             method: "DELETE" }]);
  }
  {
    // A root: there is nothing above it, so the safe answer says so rather
    // than naming a session that does not exist.
    const h = removeHarness([], { force: false, children: "escalate" },
                            { ok: true }, TREE);
    await h.remove("lead");
    check("a root's children are left top-level, and it says so",
          h.modals[0].choices.options[0].label,
          "Leave them as top-level sessions");
  }
  {
    const h = removeHarness([], { force: false }, { ok: true }, TREE);
    await h.remove("grand");
    check("a leaf is not asked the question at all",
          [h.modals[0].choices, h.calls],
          [null, [{ url: "/api/sessions/grand", method: "DELETE" }]]);
  }
}

/* --------- the guard the eye can see: disabled has to look disabled -------- */

/* The acknowledgement gate is enforced in JS (requiresCheck above), and for a
   while that was the whole of it: the held button kept full contrast, the
   pointer cursor and its hover fill, so the only way to find out it was inert
   was to press it. These rules are what make the state visible, so they are
   checked here rather than left to a screenshot. */
function checkDisabledStyling() {
  check("a held answer is dimmed and refuses the pointer",
        /#modal-actions button:disabled\s*\{[^}]*opacity:[^}]*cursor:\s*not-allowed/
          .test(css),
        true);
  check("neither hover fill paints over a held answer",
        [/#modal-actions button:hover:not\(:disabled\)/.test(css),
         /#modal-actions button\.danger:hover:not\(:disabled\)/.test(css)],
        [true, true]);
  check("no bare :hover rule is left to override it",
        /#modal-actions button(\.danger)?:hover\s*\{/.test(css), false);
}

(async () => {
  checkDisabledStyling();
  await checkShowModal();
  await checkOfferForce();
  await checkIndividualRemove();
  await checkChildrenQuestion();
  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("forceclear_check: ok");
})();
