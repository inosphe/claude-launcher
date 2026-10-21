/* The observer's three weights and the session's waiting gates, run against
   the real code from observer.js (claunch-kuopi).

   What has to hold:
   - the weight is the daemon's answer (event.priority), read here rather than
     recomputed: an event written before the field existed reads as 보통, and
     a value the page does not know does too;
   - the page's 알림 filter never widens what the daemon called 긴급: "보통
     이상" drops 통지 only, "긴급만" keeps exactly the unanswered requests;
   - runs of the same routine daemon event fold — the operator's own
     complaint was an `adopt` line repeated under one card — while `exit`,
     which means the session stopped (again), never folds;
   - the gate block names the session the request is about (asker or target),
     draws at most four rows with the rest counted, and draws nothing for a
     session no request names. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "observer.js"),
  "utf8");

function slice(start, end) {
  const a = src.indexOf(start), b = src.indexOf(end, a);
  if (a < 0 || b < a) throw new Error(`cannot locate ${start}`);
  return src.slice(a, b);
}
const nodeSrc = src.slice(src.indexOf("const node = "), src.indexOf("\n", src.indexOf("const node = ")));
const weights = slice("const eventPriority = e =>", "function sessionEvents(s) {");
const fold = slice("const ROUTINE = new Set(", "const clock = value =>");
const gates = slice("const GATE_LABELS =", "/* Run state and notices once per session");

function mkel(tag) {
  return { tag, className: "", title: "", textContent: "", href: "", type: "", children: [],
           append(...cs) { for (const c of cs) this.children.push(c); } };
}
const env = { gatePending: {} };
const ctx = { location: { hash: "" } };
new Function("exports", "document", "env", "location", `
let priorityFilter = "all";
const gatePending = env.gatePending;
const noticePanelToggle = () => { exports.panelOpened = true; };
` + nodeSrc + "\n" + weights + "\n" + fold + "\n" + gates + `
exports.eventPriority = eventPriority;
exports.setFilter = (value) => { priorityFilter = value; };
exports.priorityShown = priorityShown;
exports.collapseRoutine = collapseRoutine;
exports.gatesBlock = gatesBlock;
exports.gateWaits = gateWaits;
exports.gateLine = gateLine;
`)(ctx, { createElement: mkel }, env, ctx.location);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) { console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`); failures++; }
}
const text = (n) => n.textContent + n.children.map(text).join("");

/* The weight is read, not recomputed. */
check("an event stored before the field reads as 보통", ctx.eventPriority({ kind: "commit" }), "normal");
check("통지 stays 통지", ctx.eventPriority({ kind: "adopt", origin: "daemon", priority: "notice" }), "notice");
check("긴급 stays 긴급", ctx.eventPriority({ needs_action: true, priority: "urgent" }), "urgent");
check("an unknown weight is not trusted into 긴급", ctx.eventPriority({ priority: "critical" }), "normal");

/* The filter. */
const notice = { priority: "notice" }, normal = { priority: "normal" }, urgent = { priority: "urgent" };
ctx.setFilter("all");
check("전체 keeps every weight", [notice, normal, urgent].map(ctx.priorityShown), [true, true, true]);
ctx.setFilter("normal");
check("보통 이상 drops 통지", [notice, normal, urgent].map(ctx.priorityShown), [false, true, true]);
ctx.setFilter("urgent");
check("긴급만 keeps the requests", [notice, normal, urgent].map(ctx.priorityShown), [false, false, true]);
ctx.setFilter("all");

/* Folding the routine run — the operator's own case (2026-10-21). */
const ev = (id, kind, minute, t = "데몬 재시작 후 PTY 호스트의 세션 인수") =>
  ({ id, kind, origin: "daemon", text: t, at: `2026-10-21T07:${minute}:00+00:00` });
const folded = ctx.collapseRoutine([
  ev("a1", "adopt", "22"),
  ev("a2", "adopt", "37"),
  ev("a3", "adopt", "23"),
  ev("x", "exit", "40", "세션 프로세스 종료"),
  ev("a4", "adopt", "50"),
]);
check("adjacent adopts fold; the exit in between is not folded across",
      folded.map((e) => [e.id, e.run ? e.run.count : 1]),
      [["a1", 3], ["x", 1], ["a4", 1]]);
check("the fold keeps its first and last time", [folded[0].run.first, folded[0].run.last],
      ["2026-10-21T07:22:00+00:00", "2026-10-21T07:23:00+00:00"]);
check("two identical exits are two events — the session stopped twice",
      ctx.collapseRoutine([ev("x1", "exit", "00", "세션 프로세스 종료"),
                           ev("x2", "exit", "10", "세션 프로세스 종료")]).length, 2);
check("different texts never fold", ctx.collapseRoutine([
  ev("b1", "adopt", "00"), ev("b2", "adopt", "10", "다른 문구")]).length, 2);

/* The gate block. */
const s1 = { name: "s1" };
check("no gate names this session: no block", ctx.gatesBlock(s1), null);
env.gatePending.restart = [{ id: "rq1", session: "s1" }];
env.gatePending.kill = [{ id: "kq1", session: "s2", target: "s1" }];
env.gatePending.ask = [{ id: "aq1", session: "s1", label: "coding approval" }];
let block = ctx.gatesBlock(s1);
check("the block counts the requests", text(block.children[0].children[0]), "사용자 승인 대기 3건");
check("rows name kind, role and what the request is about",
      block.children.slice(1).map(text),
      ["daemon 재시작요청s1", "세션 종료대상s2 → s1", "cflow 답변요청s1 · coding approval"]);
env.gatePending.nudge = [{ id: "n1", session: "s1", items: ["choice", "limit"] }];
env.gatePending.goto = [{ id: "g1", session: "s1", target_session: "s2" }];
block = ctx.gatesBlock(s1);
check("at most four rows, the rest counted", [block.children.length, text(block.children.at(-1))],
      [6, "외 1건"]);
env.gatePending.restart = [];
env.gatePending.kill = [];
env.gatePending.ask = [];
env.gatePending.nudge = [];
env.gatePending.goto = [];
check("a settled request leaves the card", ctx.gatesBlock(s1), null);
check("a session no request names has no gate", ctx.gateWaits({ name: "s9" }), []);

if (failures) { console.error(`observerpriority_check: ${failures} failure(s)`); process.exit(1); }
console.log("observerpriority_check: ok");
