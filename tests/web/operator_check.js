/* The Operator panel (operator.js), its page and its modal.

   What has to hold:
     helpers       -- the open asks are the unanswered ones; a notification is
                      raised only for asks and urgent posts not seen before;
                      an answer reads as 승인/거절 for approve asks and as the
                      choice otherwise; the session badge puts an open question
                      ahead of everything else it could say.
     scroll        -- a poll that arrives while the reader is scrolled up keeps
                      their place and counts what is new on a button at the
                      bottom; at the bottom (or after the user sends) it follows.
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
                    "dotClass", "progressChips", "threadsOf", "cardLine", "sameUpdate", "gateActions", "unseenCount", "sessionWords"]) {
  vm.runInContext(slice(src, name), ctx);
}
vm.runInContext(src.match(/^const STAGES = .*$/m)[0].replace("const ", "var "), ctx);
vm.runInContext(src.match(/^const GATE_KINDS = .*$/m)[0].replace("const ", "var "), ctx);
vm.runInContext(src.match(/^const SESSION_WORD = .*$/m)[0].replace("const ", "var "), ctx);

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

const threaded = [
  { id: "c1", kind: "post", text: "## **w1** waits\nat commit" },
  { id: "u1", kind: "update", parent: "c1", session: "w1", text: "게이트 commit 해소" },
  { id: "c2", kind: "post", text: "other" },
  { id: "r1", kind: "post", parent: "c1", text: "landed" },
  { id: "o1", kind: "update", parent: "gone", text: "card trimmed" },
];
check("follow-ups group under their card, oldest first; an orphan has no thread",
      [...ctx.threadsOf(threaded)].map(([k, v]) => [k, v.map((e) => e.id)]), [["c1", ["u1", "r1"]]]);
check("a card reads as its first line without markdown marks", ctx.cardLine(threaded[0]), "w1 waits");
check("a long card line is cut", ctx.cardLine({ text: "x".repeat(80) }).length, 60);
check("the feed lists a follow-up in time order and again under its card",
      [src.includes("const follow = renderFollowup(e, byId.get(e.parent));"),
       src.includes("item.append(renderThread(threads.get(e.id)))")], [true, true]);

const u = { kind: "update", session: "w1", text: "게이트 commit 해소", at: "t" };
check("one pass's update about one session reads as one line in time order",
      [ctx.sameUpdate(u, { ...u, parent: "c2" }), ctx.sameUpdate(u, { ...u, at: "t2" }),
       ctx.sameUpdate(u, { ...u, session: "w2" }), ctx.sameUpdate(null, u)], [true, false, false, false]);
check("same-pass duplicates add a card link to the line already drawn",
      src.includes("if (sameUpdate(last && last.entry, e)) { parentLink(last.item, byId.get(e.parent)); continue; }"), true);
const at = { cwd: "F:/r", scope: "s1" };
check("an approval gate presses the dashboard's approve",
      ctx.gateActions({ kind: "approval", session: "s1", step_id: "end-gate", ...at }).map((a) => [a.label, a.path, a.body]),
      [["승인", "api/cflow/approve", at]]);
check("a selection gate offers each option as a select",
      ctx.gateActions({ kind: "selection", session: "s1", step_id: "pick", ...at, options: [{ name: "a" }, { name: "b", description: "d" }] })
        .map((a) => [a.label, a.path, a.body.option, a.title]),
      [["a", "api/cflow/select", "a", ""], ["b", "api/cflow/select", "b", "d"]]);
check("a goto request is approved or refused through goto/resolve, the refusal asking for a reason",
      ctx.gateActions({ kind: "goto", session: "s1", step_id: "end-gate", ...at, goto_request: { step: "intake" } })
        .map((a) => [a.label, a.body.decision, !!a.reason, !!a.confirm]),
      [["'intake' 이동 승인", "approve", false, true], ["거절", "deny", true, false]]);
check("gate presses go under the newest card naming the session, and in the panel",
      [src.includes("gateCard.set(r, e.id)"), src.includes("if (gateCard.get(r) === e.id) item.append(renderGate(gates.get(r)))"),
       src.includes("승인을 기다리는 cflow 게이트")], [true, true, true]);
check("unseen: entries after the one read at the bottom; none before anything was read; all once it was trimmed",
      [ctx.unseenCount(feed, "c"), ctx.unseenCount(feed, "e"), ctx.unseenCount(feed, null), ctx.unseenCount(feed, "gone"), ctx.unseenCount(null, "a")],
      [2, 0, 0, 5, 0]);
check("scrolled up, the rebuild keeps the reader's entry in place and shows the count instead of jumping",
      [src.includes("const keep = atBottom || !h.scrolled ? null : {top: list.scrollTop, entry: topEntry(list)};"),
       src.includes("list.scrollTop = keep.top;"), src.includes("showUnseen(h);"),
       /if \(atBottom \|\| !h\.scrolled\) \{ list\.scrollTop = list\.scrollHeight/.test(src)],
      [true, true, true, false]);
check("the button sits at the feed's newest end; pressing it or scrolling down clears it",
      [src.includes("feedWrap.append(feed, newer);"), src.includes("if (nearBottom(feed)) markRead(h);"),
       src.includes("newer.onclick = () => { feed.scrollTop = feed.scrollHeight; markRead(h); };")], [true, true, true]);
check("sending a message or switching project goes back to following the bottom",
      [src.includes("h.parts.input.value = \"\"; h.scrolled = false;"), src.includes("h.scrolled = false; h.readId = null;")], [true, true]);
const knownSessions = new Set(["s751", "s469", "s638", "s697"]);
const words = (t) => ctx.sessionWords(t, (n) => knownSessions.has(n));
check("a known session name in prose becomes a link, the text around it kept",
      words("s751의 peer-review는 리더 s469에게 라우팅됐습니다."),
      [{ session: "s751" }, "의 peer-review는 리더 ", { session: "s469" }, "에게 라우팅됐습니다."]);
check("a list of sessions links each one",
      words("end-gate 대기: s638, s697."), ["end-gate 대기: ", { session: "s638" }, ", ", { session: "s697" }, "."]);
check("an unknown s-number, a longer word and a branch name stay text",
      [words("s3 버킷, alias9s751, s751-cflow, xs469"), words("")], [["s3 버킷, alias9s751, s751-cflow, xs469"], []]);
check("every text the feed draws goes through the session linker",
      (src.match(/linkSessions\(/g) || []).length, 7);

check("a daemon restart entry gets its own look", src.includes("operator-event-${e.event}"), true);
check("opening or leaving the page or the modal marks the badge seen",
      [src.includes('request("api/operator/seen", {})'),
       src.includes("if (pageOpen) markSeen();"), src.includes('modal.addEventListener("close", () => { markSeen(); detachIdle(); });'),
       (src.match(/refresh\(\); poll\(\); markSeen\(\);/g) || []).length], [true, true, true, 2]);

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
check("a transcript mode travels in the start request; the default is not sent",
      [ctx.startBody("p", {mode: "transcript"}, ctx.startChoices(options, caps, "a:pi")).mode,
       "mode" in ctx.startBody("p", {mode: "events"}, ctx.startChoices(options, caps, "a:pi"))],
      ["transcript", false]);
check("a running operator's header offers the mode switch, which posts to api/operator/mode",
      [src.includes("start.append(modeSelect(data.mode, switchMode))"), src.includes('request("api/operator/mode"')],
      [true, true]);
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
