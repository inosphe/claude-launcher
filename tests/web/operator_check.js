/* The Operator panel (operator.js), its page and its modal.

   What has to hold:
     helpers       -- the open asks are the unanswered ones; a notification is
                      raised only for asks and urgent posts not seen before;
                      an answer reads as 승인/거절 for approve asks and as the
                      choice otherwise; the session badge puts an open question
                      ahead of everything else it could say.
     text only     -- operator.js never assigns HTML: every string in the feed
                      came from an agent or a user.
     wired         -- the page has a route, a view, a nav entry with the badge,
                      and the script and stylesheet are loaded; the modal is
                      reachable from any page through the floating button and
                      Alt+O. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const root = path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static");
const src = fs.readFileSync(path.join(root, "operator.js"), "utf8");
const app = fs.readFileSync(path.join(root, "app.js"), "utf8");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");

function slice(source, name) {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name}`);
  let depth = 0;
  for (let j = source.indexOf("{", start); j < source.length; j++) {
    if (source[j] === "{") depth++;
    else if (source[j] === "}") { depth--; if (!depth) return source.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

let failures = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  if (a !== e) { failures++; console.error(`FAIL ${label}\n  got      ${a}\n  expected ${e}`); }
}

const ctx = {};
vm.createContext(ctx);
for (const name of ["openAsks", "freshUrgent", "answerLine", "deliveryLabel", "sessionBadge", "startChoices", "startBody",
                    "dotClass", "progressChips"]) {
  vm.runInContext(slice(src, name), ctx);
}
vm.runInContext(src.match(/^const STAGES = .*$/m)[0].replace("const ", "var "), ctx);

const feed = [
  { id: "a", kind: "ask", type: "approve", answer: null, text: "merge?" },
  { id: "b", kind: "ask", type: "choice", answer: { decision: "x", text: "because" } },
  { id: "c", kind: "post", level: "urgent", text: "gate" },
  { id: "d", kind: "post", level: "info", text: "fyi" },
  { id: "e", kind: "user", text: "hi" },
];
check("open asks are the unanswered ones", ctx.openAsks(feed).map((e) => e.id), ["a"]);
check("fresh urgent: open asks and urgent posts not seen yet",
      ctx.freshUrgent(new Set(["c"]), feed).map((e) => e.id), ["a"]);
check("nothing fresh once everything was seen", ctx.freshUrgent(new Set(["a", "c"]), feed), []);
check("approve answer reads 거절 with its note",
      ctx.answerLine({ type: "approve", answer: { decision: "deny", text: "not yet" } }), "거절 · not yet");
check("approve answer reads 승인", ctx.answerLine({ type: "approve", answer: { decision: "approve", text: null } }), "승인");
check("choice answer reads the choice", ctx.answerLine(feed[1]), "x · because");
check("text answer reads the text", ctx.answerLine({ type: "text", answer: { decision: null, text: "why" } }), "why");
check("no answer, no line", ctx.answerLine({ type: "text", answer: null }), "");
check("delivery labels", ["sent", "pending", "unknown"].map(ctx.deliveryLabel), ["전달됨", "전달 대기", "전달 결과 미확인"]);
check("an open question outranks a busy status",
      ctx.sessionBadge({ questions: 2, status: "busy" }), { label: "질문 2", kind: "waiting" });
check("blocked outranks busy", ctx.sessionBadge({ state: "blocked", status: "busy" }).kind, "blocked");
check("busy reads 동작 중", ctx.sessionBadge({ status: "busy" }).label, "동작 중");

check("a paused session reads 일시정지 even with open questions",
      ctx.sessionBadge({category: "paused", questions: 3, state: "blocked"}), {label: "일시정지", kind: "paused"});
check("dot classes follow the session list: paused is exited + paused",
      [ctx.dotClass({status: "idle"}), ctx.dotClass({status: "exited", paused_at: "t"}), ctx.dotClass({category: "paused", status: "exited"}),
       ctx.dotClass({status: "exited"}), ctx.dotClass(null)],
      ["dot idle", "dot exited paused", "dot exited paused", "dot exited", "dot exited"]);
check("progress chips: the answered checks, then the issue's stage",
      ctx.progressChips({checks: [{name: "C", question: "커밋?", answer: "yes"}, {name: "M", answer: "no"}, {name: "T", answer: null}],
                         issue: {id: "cl-1", status: "in_review", title: "t"}}).map((c) => [c.label, c.kind]),
      [["C ✓", "yes"], ["M ✗", "no"], ["cl-1 · 머지 요청", "issue-in_review"]]);
check("session labels open the list's card on hover and drop it when rebuilt",
      [src.includes("scheduleSessionCardTip(a, name)"), src.includes("tipAnchor && !tipAnchor.isConnected")], [true, true]);
check("app.js offers the grid's hover card for any anchor, inside an open dialog too",
      [app.includes("function showSessionCardTip(anchor, name"), app.includes('anchor.closest("dialog[open]")')], [true, true]);

const options = [{value: "a:claude", harness: "claude"}, {value: "a:pi", harness: "pi"}];
const caps = {claude: {models: ["sonnet", "opus"], efforts: ["high"]}, pi: {models: [], efforts: []}};
check("a harness with model choices offers them",
      ctx.startChoices(options, caps, "a:claude"), {profile: "a:claude", harness: "claude", models: ["sonnet", "opus"], efforts: ["high"]});
check("a harness without model choices offers no picker", ctx.startChoices(options, caps, "a:pi").models, []);
check("an unknown selector falls back to the first profile", ctx.startChoices(options, caps, "gone").profile, "a:claude");
check("the start body carries the picked model and effort",
      ctx.startBody("p", {model: "opus", effort: "high"}, ctx.startChoices(options, caps, "a:claude")),
      {project: "p", profile: "a:claude", model: "opus", effort: "high"});
check("a model the harness does not offer is not sent",
      ctx.startBody("p", {model: "opus", effort: ""}, ctx.startChoices(options, caps, "a:pi")),
      {project: "p", profile: "a:pi"});
check("the operator's own session links to its terminal",
      [src.includes('sessionLink(op.name, "operator-self")'), src.includes('a.href = "#/s/" + encodeURIComponent(name)')], [true, true]);
check("a session link in the modal closes the modal",
      /modalRoot\.addEventListener\("click"[^\n]*\n[^\n]*a\[href\^="#\/"\][^\n]*modal\.close\(\)/.test(src), true);

check("operator.js never assigns HTML", /innerHTML|outerHTML|insertAdjacentHTML/.test(src), false);
check("the route parses", app.includes('if (parts[0] === "operator") return { page: "operator" };'), true);
check("the route opens the page", app.includes('case "operator": showView("operator"); globalThis.OperatorPanel.open(); break;'), true);
check("leaving the page stops its poll", app.includes('if (r.page !== "operator") globalThis.OperatorPanel?.stop();'), true);
check("the view is registered", app.includes('operator: "operator-view",'), true);
check("the view exists", html.includes('<section id="operator-view"'), true);
check("the nav entry sits next to Observer and carries the badge",
      /data-page="observer">[^\n]*\n\s*<a href="#\/operator" data-page="operator">[^\n]*id="operator-nav-badge"/.test(html), true);
check("script and stylesheet are loaded",
      [html.includes('<script src="static/operator.js"></script>'), html.includes('href="static/operator.css"')], [true, true]);
check("the modal opens from anywhere: floating button and Alt+O",
      [src.includes('document.body.append(fab)'), /event\.altKey[^\n]*event\.key === "o"/.test(src)], [true, true]);
check("user input goes to the daemon, not a terminal",
      [src.includes("api/operator/message"), /api\/sessions\/[^"`]*\/(deliver|keys)/.test(src)], [true, false]);

if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
console.log("operator_check: ok");
