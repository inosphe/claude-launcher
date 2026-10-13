/* The callstack effect on a move request's card (claunch-1qsz4): the run
   page's "Waiting on you -> move" card draws runtime.move_plan — whether the
   move re-enters a live frame, branches off under a shared frame, or shares
   only the root; how many levels it rewinds and steps in; where the anchor's
   sequence goes; which loops keep their iteration; and the callstack with
   the frames that stay, are cancelled (-) and are entered (+). Slice the
   real function out of app.js and drive it against a stub DOM. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

function node(tag) {
  return {
    tag, kids: [], text: "", className: "", style: {}, attrs: {}, title: "",
    appendChild(c) { this.kids.push(c); return c; },
    append(...cs) { this.kids.push(...cs); },
    setAttribute(k, v) { this.attrs[k] = v; },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
  };
}
function walk(n, out = []) {
  for (const k of n.kids) { out.push(k); walk(k, out); }
  return out;
}
const has = (n, cls) => (n.className || "").split(" ").includes(cls);
const find = (n, cls) => walk(n).filter((k) => has(k, cls));
const texts = (n) => walk(n).map((k) => k.text).filter(Boolean).join(" | ");

const ctx = { document: { createElement: node }, Math };
vm.createContext(ctx);
vm.runInContext([slice("el"), slice("cf2MovePlanView")].join("\n") +
  "\nthis.view = cf2MovePlanView;", ctx);

let failures = 0;
function check(name, ok, detail) {
  if (!ok) { failures++; console.log("FAIL " + name + (detail ? ": " + detail : "")); }
}

const frame = (node, type, extra = {}) => ({ node, label: node, type, ...extra });

/* the worker's shape: the round's seq is at its choice, the driver asks for intake */
const BRANCH = {
  target: frame("intake", "task"), relation: "branch",
  anchor: frame("rounds.do", "seq", { label: null, instance: "rounds.do@3" }),
  kept: [frame("body", "try", { instance: "body@1" }),
         frame("rounds", "loop", { instance: "rounds@2", iter: 2 }),
         frame("rounds.do", "seq", { label: null, instance: "rounds.do@3" })],
  unwound: [frame("pick", "choose", { instance: "pick@6", depth: 1 })],
  rewind: 1, entered: [frame("intake", "task")], step_in: 1,
  before: [["body", "rounds", "rounds.do", "pick"]],
  after: ["body", "rounds", "rounds.do", "intake"],
  seq: { steps: 3, to: 0, to_label: "intake", at: 2, at_label: "pick", direction: "back" },
};
let box = ctx.view(BRANCH);
let head = find(box, "cf2-move-head")[0];
check("branch: the headline says rewind N to the anchor, step in M to the target",
      head && head.text.includes("Rewinds 1 level to 'rounds.do'") &&
      head.text.includes("steps in 1 level to 'intake'") &&
      head.text.includes("not on the current callstack"), head && head.text);
const all = texts(box);
check("branch: the relation, rewind and step-in counts are facts",
      all.includes("relation | branch") && all.includes("rewind | −1") &&
      all.includes("step in | +1"), all);
check("branch: depth before and after", all.includes("depth | 4 → 4 (0)"), all);
check("branch: where the anchor's seq goes",
      all.includes("back 2 steps: 3 'pick' → 1 'intake' of 3"), all);
check("branch: a kept loop keeps its iteration",
      all.includes("loop 'rounds' | stays in iteration 2"), all);
const rows = find(box, "cf2-move-frame");
check("branch: the callstack lists kept, cancelled and entered frames in order",
      rows.map((r) => r.className.split(" ")[1]).join() === "kept,kept,kept,unwound,entered",
      rows.map((r) => r.className).join());
const marks = rows.map((r) => r.kids[0].text).join("");
check("branch: cancelled frames carry -, entered +", marks === "   −+", JSON.stringify(marks));
check("branch: the last kept frame is the anchor",
      texts(rows[2]).includes("stays (anchor)"), texts(rows[2]));
check("branch: the target row says it is the target",
      texts(rows[4]).includes("entered (target)"), texts(rows[4]));
check("branch: cancelled frames sit under the anchor",
      parseInt(rows[3].style.paddingLeft, 10) > parseInt(rows[2].style.paddingLeft, 10));

/* the driver asks for the task it is on: it starts again */
box = ctx.view({ ...BRANCH, relation: "reenter",
  unwound: [frame("intake", "task", { instance: "intake@4", depth: 1 })],
  before: [["body", "rounds", "rounds.do", "intake"]],
  seq: { steps: 3, to: 0, to_label: "intake", at: 0, at_label: "intake", direction: "same" } });
head = find(box, "cf2-move-head")[0];
check("reenter: says it re-enters a frame on the callstack",
      head.text.startsWith("Re-enters 'intake', a frame on the current callstack") &&
      head.text.includes("unwinds 1 level"), head.text);
check("reenter: the same step again", texts(box).includes("same step 1/3 'intake' again"),
      texts(box));

/* only the root is shared: the end decision in the try's finally */
box = ctx.view({ target: frame("intake", "task"), relation: "unrelated",
  anchor: frame("body", "try", { instance: "body@1" }),
  kept: [frame("body", "try", { instance: "body@1" })],
  unwound: [frame("fin", "seq", { instance: "fin@9", depth: 1 }),
            frame("end", "choose", { instance: "end@12", depth: 2 })],
  rewind: 2,
  entered: [frame("rounds", "loop"), frame("rounds.do", "seq"), frame("intake", "task")],
  step_in: 3, before: [["body", "fin", "end"]], after: ["body", "rounds", "rounds.do", "intake"] });
head = find(box, "cf2-move-head")[0];
check("unrelated: says only the root is shared",
      head.text.startsWith("Unrelated to the current callstack: only the root 'body' is shared") &&
      head.text.includes("unwinds 2 levels, then steps in 3 levels"), head.text);
check("unrelated: depth grows by one", texts(box).includes("depth | 3 → 4 (+1)"), texts(box));
check("unrelated: a loop entered fresh starts at iteration 1",
      texts(box).includes("loop 'rounds' | entered fresh: iteration 1"), texts(box));
check("unrelated: no seq fact when the anchor is not a seq", !texts(box).includes("in 'body'"));

/* nothing below the anchor is live */
box = ctx.view({ ...BRANCH, relation: "step_in", unwound: [], rewind: 0,
  before: [["body", "rounds", "rounds.do"]], seq: undefined });
check("step_in: says nothing is unwound",
      find(box, "cf2-move-head")[0].text.includes("nothing is unwound"));

/* a plan the runtime could not make */
box = ctx.view({ problems: ["intake is under par x, whose siblings a move cannot rebuild"] });
check("problems: shown as a warning, nothing else",
      find(box, "wf-warning").length === 1 && !find(box, "cf2-move-frame").length &&
      texts(box).includes("callstack effect unknown"), texts(box));

/* ---- the call site and the stylesheet ---- */
check("the move card draws the plan",
      /if \(mr\.plan\) card\.appendChild\(cf2MovePlanView\(mr\.plan\)\);/.test(src));
check("cancelled frames are struck through",
      /\.cf2-move-frame\.unwound \.cf2-stack-label \{[^}]*line-through/.test(css));

if (failures) { console.log(`${failures} failure(s)`); process.exit(1); }
console.log("moveplan_check ok");
