/* Observer's compact event line, run against the real code from observer.js.

   What has to hold: the identifiers an observer report is made of (beads
   issues, cflow run ids, sessions, commit hashes, test counts) are drawn as
   chips wherever they appear — in plain text or as a whole backtick span —
   and everything else stays literal, so a report written before this format
   existed reads with the same chips (the texts below are s697's, 2026-09-23).
   A run of identical routine daemon events next to each other folds into one
   entry with a count and a span, and nothing folds across another event.
   A pivot event carries its badge. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "observer.js"),
  "utf8");

function block(source) {
  const start = source.indexOf("/* Event text as one compact line");
  const end = source.indexOf("/* briefing/checks records");
  if (start < 0 || end < start) throw new Error("cannot locate the compact-line block");
  return source.slice(start, end);
}
const nodeSrc = src.slice(src.indexOf("const node = "), src.indexOf("\n", src.indexOf("const node = ")));

function mkel(tag) {
  return { tag, className: "", title: "", textContent: "", children: [],
           append(...cs) { for (const c of cs) this.children.push(c); } };
}
const ctx = {};
new Function("exports", "document", nodeSrc + "\n" + block(src) + `
exports.richLine = richLine; exports.collapseRoutine = collapseRoutine;
`)(ctx, { createElement: mkel });

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) { console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`); failures++; }
}
const text = (n) => n.textContent + n.children.map(text).join("");
const chips = (line) => line.children.filter((c) => c.className.startsWith("obs-chip"))
  .map((c) => [c.className.replace("obs-chip obs-chip-", ""), c.textContent]);

/* s697's own texts, as stored — no backticks, prose around identifiers. */
const a = ctx.richLine("s469 결정: 크래시 결함은 별도 이슈 claunch-lxwwf(P1, found)로 등록하고 review→rebase goto 요청(gr-c6dc16)을 걸었으며, 브랜치는 수정하지 말라고 지시");
check("a session and an issue in old prose become chips", chips(a),
      [["session", "@s469"], ["beads", "claunch-lxwwf"]]);
check("the words around them are kept as they were",
      text(a), "@s469 결정: 크래시 결함은 별도 이슈 claunch-lxwwf(P1, found)로 등록하고 review→rebase goto 요청(gr-c6dc16)을 걸었으며, 브랜치는 수정하지 말라고 지시");

const b = ctx.richLine("cflow가 rebase 스텝으로 강제 전환됨(run-bd90a11c, steps_completed=5, visit 1)");
check("a cflow run id is a run chip", chips(b), [["run", "run-bd90a11c"]]);

const c = ctx.richLine("rebase 후 표적 게이트 결과 1 failed/2997 passed/8 skipped — 새 tip 22666072, 프리뷰 머지 d3ff019e(master 529ac5c4)");
check("test counts are coloured by outcome and hashes are commit chips", chips(c),
      [["fail", "1 failed"], ["pass", "2997 passed"], ["count", "8 skipped"],
       ["commit", "22666072"], ["commit", "d3ff019e"], ["commit", "529ac5c4"]]);

check("a zero failure count is not drawn as a failure",
      chips(ctx.richLine("0 failed, 12 passed")), [["count", "0 failed"], ["pass", "12 passed"]]);

/* A branch name that starts with a session handle is not a mention, and a
   bare number is not a hash unless the text calls it one. */
check("a session-prefixed branch name stays text",
      chips(ctx.richLine("브랜치 s697-cflow-run-state를 rebase했다 (2997건)")), []);
check("a digit-only number without a hash cue stays text",
      chips(ctx.richLine("20260923 기준 3000000 records")), []);

/* The new format: identifiers in backticks. */
const d = ctx.richLine("`claunch-mwvfg` 커밋 `0e70cea1` · 경로 `src/x.py` · @s730");
check("a backtick span that is one identifier is that identifier's chip",
      chips(d), [["beads", "claunch-mwvfg"], ["commit", "0e70cea1"], ["session", "@s730"]]);
check("any other backtick span is a code span",
      d.children.filter((n) => n.className === "obs-code").map((n) => n.textContent), ["src/x.py"]);

/* Folding routine daemon events. */
const ev = (id, kind, at, origin = "daemon", t = "데몬 재시작 후 세션 복원") => ({ id, kind, at, origin, text: t });
const folded = ctx.collapseRoutine([
  ev("c", "create", "2026-09-22T10:37:24Z", "daemon", "세션 생성"),
  ev("r1", "resume", "2026-09-22T10:41:17Z"),
  ev("r2", "resume", "2026-09-22T11:24:57Z"),
  ev("r3", "resume", "2026-09-22T12:10:37Z"),
  ev("k", "checks", "2026-09-22T12:30:00Z", "record", "[]"),
  ev("r4", "resume", "2026-09-22T13:05:37Z"),
]);
check("adjacent resumes fold; one in between is not folded across",
      folded.map((e) => [e.id, e.kind, e.run ? e.run.count : 1]),
      [["c", "create", 1], ["r1", "resume", 3], ["k", "checks", 1], ["r4", "resume", 1]]);
check("the fold keeps its first and last time, and sits at the last",
      [folded[1].run.first, folded[1].run.last, folded[1].at],
      ["2026-09-22T10:41:17Z", "2026-09-22T12:10:37Z", "2026-09-22T12:10:37Z"]);
check("a non-routine daemon event never folds",
      ctx.collapseRoutine([ev("x1", "exit", "2026-09-23T01:00:00Z", "daemon", "세션 프로세스 종료"),
                           ev("x2", "exit", "2026-09-23T02:00:00Z", "daemon", "세션 프로세스 종료")]).length, 2);

if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
console.log("observercompact_check: ok");
