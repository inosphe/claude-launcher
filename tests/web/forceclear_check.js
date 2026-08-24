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
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

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
      classes: new Set(id === "modal-overlay" ? ["hidden"] : []),
      classList: {
        add: (c) => el.classes.add(c),
        remove: (c) => el.classes.delete(c),
      },
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
    createElement() {
      const el = {
        type: "", textContent: "", focused: false,
        classes: new Set(), handlers: {},
        classList: { add: (c) => el.classes.add(c) },
        addEventListener(ev, fn) { el.handlers[ev] = fn; },
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

(async () => {
  await checkShowModal();
  await checkOfferForce();
  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("forceclear_check: ok");
})();
