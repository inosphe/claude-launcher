/* quick-fork and handoff (daemon/handoff.py) on the page: which header
   button a record gets, what the merge button says while a request is
   pending, what a press posts, and that a second press is the plain kill. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);
const html = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "index.html"),
  "utf8"
);

const a = src.indexOf("/* ---- quick-fork and handoff (daemon/handoff.py)");
const b = src.indexOf("/* ---- end quick-fork and handoff", a);
if (a < 0 || b <= a) throw new Error("cannot locate the quick-fork section");
const code = src.slice(a, b);

function button() {
  const classes = new Set();
  return {
    textContent: "", title: "", handlers: {}, classes,
    classList: {
      toggle(name, on) { on ? classes.add(name) : classes.delete(name); },
    },
    addEventListener(name, fn) { this.handlers[name] = fn; },
  };
}

const buttons = { "term-fork": button(), "term-merge": button() };
const calls = [];
const replies = [];
const errors = [];
const kills = [];
let refreshes = 0;
const location = { hash: "" };

const api = (url, options) => {
  calls.push({ url, options });
  return new Promise((resolve, reject) => replies.push({ resolve, reject }));
};
const modalInfo = async (title, detail) => { errors.push([title, detail]); };
const refreshSessions = async () => { refreshes += 1; };
const killSession = async (name) => { kills.push(name); };
const $ = (id) => buttons[id];

// The fork dialog: what it was asked, and what the next press answers. A
// null answer is Cancel, an object is a pressed action merged with the
// picked option — the shape showModal actually resolves to.
const modals = [];
let modalAnswer = {};
const showModal = async (spec) => { modals.push(spec); return modalAnswer; };
// Neither of these is on the session record; the page reads them from the
// mesh and cflow polls, so the check supplies the same two readers.
let meshOf = {};
let runOf = {};

const ctx = {};
new Function(
  "exports", "$", "api", "modalInfo", "refreshSessions", "killSession", "location",
  "showModal", "meshOf", "runOf",
  `let currentName = "a";
   let sessionsCache = [
     {name: "a", status: "busy", harness: "claude", conversation_id: "u1"},
     {name: "a-qf1", status: "idle", harness: "claude", conversation_id: "u2", quick_fork_of: "a"},
     {name: "py", status: "busy", harness: "py"},
   ];
   function railCardRecord(name) { return sessionsCache.find((s) => s.name === name) || null; }
   function sessMeshes(name) { return meshOf[name] ? [{mesh: meshOf[name]}] : []; }
   function sessCflowRun(name) { return runOf[name] ? {workflow: runOf[name]} : null; }
   function openSessionModal(opts) { return showModal(opts); }
   ${src.slice(src.indexOf("function sessionFormConfig("), src.indexOf("function createSessionFormView("))}
   ${code}
   Object.assign(exports, {
     forkControlState, handoffControlState, quickForkSession, requestHandoff,
     cancelHandoff, syncSessionHandoffControls, railCardQuickFork,
     quickForkInheritance, openQuickForkModal,
     sessions: () => sessionsCache,
     setCurrent: (name) => { currentName = name; },
   });`
)(ctx, $, api, modalInfo, refreshSessions, killSession, location,
  showModal, meshOf, runOf);

let failures = 0;
function check(name, actual, expected) {
  if (JSON.stringify(actual) === JSON.stringify(expected)) return;
  failures += 1;
  console.log(`FAIL ${name}: ${JSON.stringify(actual)} != ${JSON.stringify(expected)}`);
}

const response = (body, ok = true, status = 200) => ({
  ok, status, json: async () => body,
});

(async () => {
  check("the header carries both buttons, hidden until a record earns them",
        [/id="term-fork" class="term-btn hidden"/.test(html),
         /id="term-merge" class="term-btn hidden"/.test(html)],
        [true, true]);

  // The pair reads the SESSION LIST (quick_fork_of, handoff), which arrives
  // on the poll — so the poll has to repaint it. Hung off setStatusBadge
  // alone it was repainted only when the status changed, and a tab attached
  // before the first list landed hid both buttons for good: a copy with no
  // way back, reported live on s556-qf1. Asserted on the source outside this
  // section because that caller is refreshSessions, not sliced here.
  const poll = src.slice(0, a);
  check("the session poll repaints the fork/merge pair, not only setStatusBadge",
        /syncSessionHandoffControls/.test(poll), true);

  // which button a record gets
  check("a live claude session with a conversation gets fork, not merge",
        ctx.forkControlState(ctx.sessions()[0]), { fork: true, merge: false });
  check("a quick-fork gets merge, not fork",
        ctx.forkControlState(ctx.sessions()[1]), { fork: false, merge: true });
  check("a non-claude session gets neither",
        ctx.forkControlState(ctx.sessions()[2]), { fork: false, merge: false });
  check("an exited record gets neither",
        ctx.forkControlState({ name: "x", status: "exited", harness: "claude", conversation_id: "u", quick_fork_of: "a" }),
        { fork: false, merge: false });

  ctx.syncSessionHandoffControls();
  check("the attached origin shows fork only",
        [buttons["term-fork"].classes.has("hidden"), buttons["term-merge"].classes.has("hidden")],
        [false, true]);

  // the fork press
  const fork = ctx.quickForkSession("a");
  check("fork posts the quick-fork route with an empty body",
        calls[0], { url: "/api/sessions/a/quick-fork", options: {
          method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" } });
  replies.shift().resolve(response({ session: { name: "a-qf2" }, marker: "qf-1" }));
  await fork;
  check("a fork opens the copy", location.hash, "#/s/a-qf2");
  check("refresh follows a fork", refreshes, 1);

  // the merge press, on the copy
  ctx.setCurrent("a-qf1");
  ctx.syncSessionHandoffControls();
  check("the attached copy shows merge only, in its resting face",
        [buttons["term-fork"].classes.has("hidden"), buttons["term-merge"].classes.has("hidden"),
         buttons["term-merge"].textContent],
        [true, false, "↩ merge"]);
  const merge = ctx.requestHandoff("a-qf1", "", "merge");
  check("merge posts the handoff route as a request (no text), kind merge",
        calls[1], { url: "/api/sessions/a-qf1/handoff", options: {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ kind: "merge" }) } });
  replies.shift().resolve(response({ requested: true, kind: "merge", target: "a", requested_at: "t" }));
  await merge;
  check("the pending request is on the record before the next poll",
        ctx.sessions()[1].handoff, { kind: "merge", target: "a", requested_at: "t" });
  check("the merge button wears the pending face",
        [buttons["term-merge"].textContent, buttons["term-merge"].classes.has("kill-winddown")],
        ["merging…", true]);
  check("the pending title names the target and the second press",
        /'a'/.test(buttons["term-merge"].title) && /press again to stop/.test(buttons["term-merge"].title), true);

  // a second press is the plain kill
  await ctx.requestHandoff("a-qf1", "", "merge");
  check("a press while pending is the plain kill, no second request",
        [kills, calls.length], [["a-qf1"], 2]);

  // withdrawing
  const cancel = ctx.cancelHandoff("a-qf1");
  check("withdraw is the DELETE on the same route",
        calls[2], { url: "/api/sessions/a-qf1/handoff", options: { method: "DELETE" } });
  replies.shift().resolve(response({ cancelled: true }));
  await cancel;
  check("withdraw clears the record and the face",
        [ctx.sessions()[1].handoff, buttons["term-merge"].textContent],
        [undefined, "↩ merge"]);

  // a handoff to a picked session
  const ho = ctx.requestHandoff("a", "py", "handoff");
  check("a handoff names its target",
        JSON.parse(calls[3].options.body), { to: "py", kind: "handoff" });
  replies.shift().resolve(response({ requested: true, kind: "handoff", target: "py" }));
  await ho;
  check("a pending handoff has its own face",
        ctx.handoffControlState(ctx.sessions()[0]).label, "handing off…");

  // refusals
  const refused = ctx.requestHandoff("py", "py", "handoff");
  replies.shift().resolve(response({ error: "cannot hand off to itself" }, false, 400));
  await refused;
  check("an API refusal is reported with the daemon's reason",
        errors[0], ["Could not ask 'py'", "cannot hand off to itself"]);
  const offline = ctx.quickForkSession("a");
  replies.shift().reject(new Error("offline"));
  await offline;
  check("a network failure on fork is reported",
        errors[1], ["Could not quick-fork 'a'", "offline"]);

  // the rail key
  check("q forks a forkable card and refuses the others",
        [ctx.railCardQuickFork("py"), ctx.railCardQuickFork("a-qf1")], [false, false]);
  const postsBefore = calls.length;
  const viaKey = ctx.railCardQuickFork("a");
  check("q opens the reusable Fork view without posting",
        [viaKey, modals.at(-1).tab, modals.at(-1).source.name, calls.length],
        [true, "fork", "a", postsBefore]);
  check("no mesh/run exposes no inheritance", ctx.quickForkInheritance({name:"a"}),
        {mesh:"", workflow:""});
  meshOf.a = "mesh-1"; runOf.a = "improv-worker";
  check("inheritance reads the same cached mesh/run as the rail",
        ctx.quickForkInheritance({name:"a"}), {mesh:"mesh-1", workflow:"improv-worker"});
  await buttons["term-fork"].handlers.click();
  check("header Fork opens the same component", modals.at(-1).tab, "fork");

  // ...and the press sends what was picked.
  const carried = ctx.quickForkSession("a", "", { mesh: "mesh-1", workflow: "improv-worker" });
  check("a fork that was told to carry them says so in the body",
        JSON.parse(calls[calls.length - 1].options.body),
        { mesh: "mesh-1", workflow: "improv-worker" });
  replies.shift().resolve(response({ session: { name: "a-qf4" } }));
  await carried;

  if (failures) process.exit(1);
  console.log("handoff_check: ok");
})();
