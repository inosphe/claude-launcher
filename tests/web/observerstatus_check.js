/* The observer card's run state and notices, run against the real code from
   observer.js (claunch-ozuag).

   What has to hold:
   - the v2 run comes from the app's v2 rail cache and wears the rail's own
     mark and label (cflow2RailMarkState / cflow2RailLabel), linking the run
     page of its slot; a v1 run is drawn only while it is neither waiting (its
     gate is drawn instead, with the answer form) nor done;
   - a QA question waiting in the QA rail cache is a notice, and so is a
     thread reply newer than this browser's seen mark; a session never marked
     announces nothing (its history is not news), and only the newest three
     replies are drawn, with the rest counted;
   - what the reader owes a session (actionCount) adds a gated v2 run and the
     waiting QA questions to the events and v1 gates it counted before;
   - nothing is drawn for a session with neither. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "observer.js"),
  "utf8");

function between(start, end) {
  const a = src.indexOf(start), b = src.indexOf(end, a);
  if (a < 0 || b < a) throw new Error(`cannot locate ${start}`);
  return src.slice(a, b);
}
const nodeSrc = src.slice(src.indexOf("const node = "), src.indexOf("\n", src.indexOf("const node = ")));
const accessors = between("const appRow = ", "let draftTarget");
const block = between("/* What else waits on the reader in a session", "/* One observation pass for one session, now.");
const action = between("function actionCount(s) {", "function syncTargets(");

function mkel(tag) {
  return { tag, className: "", title: "", textContent: "", href: "", type: "", children: [],
           append(...cs) { for (const c of cs) this.children.push(c); } };
}
const env = {
  cflow2RailCache: {}, qaRailCache: {}, threadsRailCache: {}, seen: {}, runs: [],
  gatePending: {},
};
const ctx = {};
new Function("exports", "document", "env", `
const cflow2RailCache = env.cflow2RailCache, qaRailCache = env.qaRailCache, threadsRailCache = env.threadsRailCache;
const gatePending = env.gatePending;
const loadThreadsSeen = () => env.seen;
const cflow2RailMarkState = r => r.gated ? "yours" : r.status === "done" ? "done" : "running";
const cflow2RailLabel = r => r.gated ? "gated" : "label:" + r.status;
const sessCflowGated = r => String(r.status).startsWith("waiting");
const sessionRuns = s => env.runs.filter(r => r.scope === s.name);
` + nodeSrc + "\n" + accessors + "\n" + block + "\n" + action + `
exports.sessionStatus = sessionStatus; exports.actionCount = actionCount; exports.newReplies = newReplies;
exports.gateWaits = gateWaits;
`)(ctx, { createElement: mkel }, env);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) { console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`); failures++; }
}
const text = (n) => n.textContent + n.children.map(text).join("");
const s1 = { name: "s1", cwd: "/repo", events: [] };

check("nothing to draw", ctx.sessionStatus(s1), []);

env.cflow2RailCache.s1 = { scope: "s1@main", cwd: "/repo", workflow: "improv-worker-v2", status: "running" };
env.runs.push({ scope: "s1", cwd: "/repo", workflow: "old", status: "done" },
              { scope: "s1", cwd: "/repo", workflow: "gate", status: "waiting_approval" },
              { scope: "s1", cwd: "/repo", workflow: "v1run", status: "running", step_id: "work" });
let [runs] = ctx.sessionStatus(s1);
check("one line per drawn run", runs.children.length, 2);
check("v2 line", [runs.children[0].className, runs.children[0].href, text(runs.children[0])],
      ["observer-run run-running", "#/wf/" + encodeURIComponent("s1@main|/repo"), "cflow2improv-worker-v2label:running"]);
check("v1 line skips done and waiting", text(runs.children[1]), "cflowv1runrunning · work");

env.cflow2RailCache.s1.gated = true;
check("a gated v2 run is owed", ctx.actionCount(s1), 2);
check("gated mark", ctx.sessionStatus(s1)[0].children[0].className, "observer-run run-yours");

env.qaRailCache.s1 = { waiting: [{ id: "qa-1", title: "어느 쪽으로?", status: "open" }] };
check("a waiting question is owed", ctx.actionCount(s1), 3);
let notices = ctx.sessionStatus(s1)[1];
check("question notice", [notices.children[0].className, text(notices.children[0])],
      ["observer-notice notice-qa", "QA 질문어느 쪽으로?"]);

env.threadsRailCache.s1 = { agent_seq: 9, replies: [5, 6, 7, 8, 9].map(seq => ({ seq, thread: "th-" + seq, title: "t" + seq, excerpt: "x" })) };
check("never marked: no news", ctx.newReplies(s1).length, 0);
env.seen.s1 = 4;
notices = ctx.sessionStatus(s1)[1];
const replies = notices.children.filter(c => c.className === "observer-notice notice-thread");
check("newest three replies, newest first", replies.map(text), ["쓰레드 답장「t9」 x", "쓰레드 답장「t8」 x", "쓰레드 답장「t7」 x"]);
check("the rest counted", text(notices.children.at(-1)), "외 쓰레드 답장 2개");
env.seen.s1 = 9;
check("all seen: no thread notice", ctx.sessionStatus(s1)[1].children.length, 1);

/* A request waiting on a person (claunch-kuopi): the daemon's gate poll is
   already in the dashboard's memory, so the card reads it instead of asking
   the gate routes again, and names the session the request is about — the
   asker (요청) or the session whose answer moves it (대상). */
env.gatePending.restart = [{ id: "rq1", session: "s1", reason: "apply" }];
check("a pending gate is owed", ctx.actionCount(s1), 4);
const gates = ctx.sessionStatus(s1)[0];
check("the gate block comes first", gates.className, "observer-gates");
check("it counts the requests", text(gates.children[0].children[0]), "사용자 승인 대기 1건");
check("the row names the request and the role", text(gates.children[1]),
      "daemon 재시작요청s1");
env.gatePending.goto = [{ id: "g1", session: "s2", target_session: "s1" }];
check("a request that moves this session is on its card too",
      ctx.gateWaits(s1).map((w) => [w.type, w.role]),
      [["restart", "요청"], ["goto", "대상"]]);
check("another session's request is not on this card", ctx.gateWaits({ name: "s9" }), []);

if (failures) { console.error(`observerstatus_check: ${failures} failure(s)`); process.exit(1); }
console.log("observerstatus_check: ok");
