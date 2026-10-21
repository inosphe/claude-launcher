/* The rail's cleanup button — the web form of `claunch clear-sessions
   [--logs]` — driven against the real `clearExitedSessions` from app.js.

   Two things make this worth its own harness. First, the plan: the button
   asks the daemon (`DELETE /api/sessions?dry_run=1`) before it asks the
   person, because the page cannot count this itself — its poll leaves
   archived records out and only the daemon knows which records a mesh row
   holds. The confirmation must then carry the daemon's numbers, not the
   button's. Second, the choice: records only (the default) or records and
   the output under sessions/<name>/; the route it finally calls differs by
   exactly that, and nothing may be sent before a person answers.
*/
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
  const head = src.lastIndexOf("async ", start) === start - 6 ? start - 6 : start;
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

const button = {
  id: "cleanup-exited", disabled: false, dataset: {},
  classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
};
const $ = (id) => {
  if (id !== "cleanup-exited") throw new Error("unexpected $: " + id);
  return button;
};

const stubs = `
let planReply = null;          // what the dry run answers
let deleteReply = null;        // what the real clear answers
let modalAnswer = null;        // what a person presses (null = cancel)
let calls = [];                // every api() and bulkAction() call, in order
let questions = [];            // every showModal() question
let infos = [];                // every modalInfo() call
let refreshed = 0;

async function api(url, opts) {
  calls.push(["api", url, (opts || {}).method || "GET"]);
  const r = url.includes("dry_run=1") ? planReply : deleteReply;
  return { ok: r.ok, status: r.status || (r.ok ? 200 : 500),
           json: async () => r.doc };
}
async function bulkAction(btn, url, opts, verb) {
  calls.push(["bulk", url, (opts || {}).method || "GET", verb]);
  const r = deleteReply;
  return r && r.ok ? r.doc : null;
}
async function showModal(q) { questions.push(q); return modalAnswer; }
async function modalInfo(title, body) { infos.push([title, body]); }
async function refreshSessions() { refreshed++; }
`;

const ctx = {};
new Function("exports", "$", stubs + slice("clearExitedSessions") + `
Object.assign(exports, {
  clearExitedSessions,
  setPlan: (r) => { planReply = r; },
  setDelete: (r) => { deleteReply = r; },
  answer: (a) => { modalAnswer = a; },
  calls: () => calls,
  questions: () => questions,
  infos: () => infos,
  reset: () => { calls = []; questions = []; infos = []; refreshed = 0; },
  refreshed: () => refreshed,
});`)(ctx, $);

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) return;
  failures++;
  console.log(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
};

const PLAN = {
  ok: true,
  doc: {
    removed: ["old", "paused-one"],
    kept: [{ name: "held", meshes: [{ mesh: "team" }] }],
    logs: false,
    stopped: [],
    marks: { paused: 1, archived: 0 },
    dry_run: true,
  },
};

(async () => {
  /* ---- the plan comes first, and nothing is dropped before the answer ---- */
  ctx.reset();
  ctx.setPlan(PLAN);
  ctx.setDelete({ ok: true, doc: { removed: ["old", "paused-one"],
                                   kept: [], logs: false, stopped: [] } });
  ctx.answer(null);  // the person presses Cancel
  await ctx.clearExitedSessions();

  check("the first call is the daemon's plan, by DELETE",
        ctx.calls()[0] && ctx.calls()[0][0] === "api"
        && ctx.calls()[0][1] === "/api/sessions?dry_run=1"
        && ctx.calls()[0][2] === "DELETE",
        ctx.calls());
  check("a cancelled confirmation sends no clear at all",
        !ctx.calls().some((c) => c[0] === "bulk"), ctx.calls());

  const q = ctx.questions()[0];
  check("the confirmation states the daemon's count, not the button's",
        q.title.includes("2 exited"), q.title);
  check("it names every record the clear will take",
        q.body.includes("old") && q.body.includes("paused-one"), q.body);
  check("it says how many of them were filed as paused",
        /1 of them are filed as paused/.test(q.body), q.body);
  check("it names the record a mesh holds, and why it survives",
        /held \(team\)/.test(q.body) && /mesh row/.test(q.body), q.body);
  check("it says round reports are outside the session directory",
        /reports\/<session>/.test(q.body), q.body);
  check("the default answer is records only",
        q.choices.options[0].value.logs === false, q.choices);
  check("the second answer takes the captured output",
        q.choices.options[1].value.logs === true, q.choices);
  check("the confirming button is not the safe default answer",
        q.actions[q.actions.length - 1].danger === true, q.actions);

  /* ---- records only: the route carries no logs flag ---- */
  ctx.reset();
  ctx.setPlan(PLAN);
  const result = { ok: true, doc: { removed: ["old", "paused-one"],
                                    kept: [{ name: "held", meshes: [{ mesh: "team" }] }],
                                    logs: false, stopped: [] } };
  ctx.setDelete(result);
  ctx.answer({ clean: true, logs: false });
  await ctx.clearExitedSessions();
  const clear = ctx.calls().find((c) => c[0] === "bulk");
  check("records-only clears through DELETE /api/sessions",
        clear && clear[1] === "/api/sessions" && clear[2] === "DELETE", ctx.calls());
  check("the rail is re-read after the clear", ctx.refreshed() === 1, ctx.refreshed());
  check("a kept record is reported after the fact, so the rows left make sense",
        ctx.infos().some(([, body]) => /held/.test(body) && /team/.test(body)),
        ctx.infos());

  /* ---- records and output: the logs flag goes to the same route ---- */
  ctx.reset();
  ctx.setPlan(PLAN);
  ctx.setDelete({ ok: true, doc: { removed: ["old", "paused-one"], kept: [],
                                   logs: true, stopped: [] } });
  ctx.answer({ clean: true, logs: true });
  await ctx.clearExitedSessions();
  const withLogs = ctx.calls().find((c) => c[0] === "bulk");
  check("records-and-output clears through DELETE /api/sessions?logs=1",
        withLogs && withLogs[1] === "/api/sessions?logs=1" && withLogs[2] === "DELETE",
        ctx.calls());
  check("the daemon's own logs flag is what travelled, not a client guess",
        !ctx.calls().some((c) => c[0] === "bulk" && !c[1].includes("dry_run")
                          && c[1] === "/api/sessions"),
        ctx.calls());

  /* ---- nothing to drop: the modal says so and nothing is sent ---- */
  ctx.reset();
  ctx.setPlan({ ok: true, doc: { removed: [], kept: [], logs: false,
                                 stopped: [], marks: { paused: 0, archived: 0 } } });
  ctx.setDelete({ ok: true, doc: {} });
  await ctx.clearExitedSessions();
  check("an empty plan explains there is nothing to drop",
        ctx.infos().some(([t]) => /Nothing to clean up/.test(t)), ctx.infos());
  check("...and sends no clear", !ctx.calls().some((c) => c[0] === "bulk"), ctx.calls());

  /* ---- every record held by a mesh: the fix is to leave the mesh ---- */
  ctx.reset();
  ctx.setPlan({ ok: true,
                doc: { removed: [], kept: [{ name: "held", meshes: [{ mesh: "team" }] }],
                       logs: false, stopped: [], marks: { paused: 0, archived: 0 } } });
  ctx.setDelete({ ok: true, doc: {} });
  await ctx.clearExitedSessions();
  check("an all-kept plan points at the mesh, not at the button",
        ctx.infos().some(([, body]) => /Leave the mesh/.test(body) && /held/.test(body)),
        ctx.infos());

  /* ---- a failed plan is not a reason to clear anything ---- */
  ctx.reset();
  ctx.setPlan({ ok: false, status: 503, doc: { error: "daemon is shutting down" } });
  ctx.setDelete({ ok: true, doc: {} });
  await ctx.clearExitedSessions();
  check("a refused plan reports the daemon's reason",
        ctx.infos().some(([t, b]) =>
          /cleanup plan/.test(t) && /daemon is shutting down/.test(b)), ctx.infos());
  check("...and never asks a person to confirm a plan it does not have",
        ctx.questions().length === 0, ctx.questions());
  check("...and sends no clear", !ctx.calls().some((c) => c[0] === "bulk"), ctx.calls());

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("cleanup_check: ok");
})();
