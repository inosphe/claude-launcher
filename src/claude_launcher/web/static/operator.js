/* Operator: the project bot's conversation, as a page (#/operator) and as a
   modal that opens over any page (the floating button, or Alt+O).

   Layout follows the agent consoles people already know (Devin): the
   conversation on the left — the bot's posts, the user's messages, asks with
   their buttons, relayed instructions — and on the right the sessions the bot
   is watching, what each is doing, and which wait on a person. The same view
   is built into both hosts; one poll feeds whichever is on screen.

   The bot's terminal is never shown here: what the user reads is what it
   posted with operator_post/operator_ask, and what the user types goes to the
   daemon (POST /api/operator/message), which nudges the bot to read it with
   operator_inbox. Every text from the feed is rendered as text or through the
   app's markdown renderer, never as HTML. */
globalThis.OperatorPanel = (() => {
"use strict";
const node = (tag, text, cls) => { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; };
async function request(path, body) {
  const options = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
  const response = await api(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(data.error || `HTTP ${response.status}`);
  return data;
}

/* ---- pure helpers (tests/web/operator_check.js) ---- */
function openAsks(feed) {
  return (feed || []).filter(e => e.kind === "ask" && !e.answer);
}
/* The asks that appeared since the ids already seen and want the user now:
   what raises a browser notification. */
function freshUrgent(seen, feed) {
  return openAsks(feed).concat((feed || []).filter(e => e.kind === "post" && e.level === "urgent"))
    .filter(e => !seen.has(e.id));
}
function answerLine(entry) {
  const a = entry.answer;
  if (!a) return "";
  const decision = entry.type === "approve" ? (a.decision === "approve" ? "승인" : "거절") : a.decision;
  return [decision, a.text].filter(Boolean).join(" · ");
}
function deliveryLabel(value) {
  return {sent: "전달됨", pending: "전달 대기", unknown: "전달 결과 미확인"}[value] || String(value || "");
}
function sessionBadge(row) {
  // A paused session is stopped on purpose: nothing in it waits on the user
  // until it is resumed, so it outranks every "needs you" reading.
  if (row.category === "paused") return {label: "일시정지", kind: "paused"};
  if (row.category === "killed" || row.category === "archived") return {label: "종료", kind: "idle"};
  if (row.questions) return {label: `질문 ${row.questions}`, kind: "waiting"};
  if (row.state === "blocked") return {label: "차단", kind: "blocked"};
  if (row.status === "busy" || row.status === "working") return {label: "동작 중", kind: "working"};
  if (row.state === "waiting") return {label: "대기", kind: "waiting"};
  if (row.state === "done") return {label: "완료", kind: "done"};
  return {label: row.status || "상태 미확인", kind: "idle"};
}
/* What the Start row offers for one profile choice: the harness's declared
   model and effort choices (GET /api/harnesses, the same source the
   new-session form reads). A harness that declares none gets no picker. */
function startChoices(options, harnesses, selector) {
  const option = (options || []).find(o => o.value === selector) || (options || [])[0];
  const caps = (option && (harnesses || {})[option.harness]) || {};
  return {profile: option ? option.value : "", harness: option ? option.harness : "",
          models: (caps.models || []).map(String), efforts: (caps.efforts || []).map(String)};
}
/* The start request. A model or effort the chosen harness does not offer is
   dropped rather than sent, so a choice left over from another profile never
   reaches the create path. Empty means the harness default. */
function startBody(projectName, choice, offered) {
  const body = {project: projectName, profile: offered.profile};
  if (choice.model && offered.models.includes(choice.model)) body.model = choice.model;
  if (choice.effort && offered.efforts.includes(choice.effort)) body.effort = choice.effort;
  if (choice.mode && choice.mode !== "events") body.mode = choice.mode;
  return body;
}
/* How the operator observes (operator_transcript.MODES): the Observer's
   events, or — the trial — the sessions' conversations read by the bot. */
const MODES = [["events", "관찰: Observer 이벤트"], ["transcript", "관찰: transcript 직접 폴링 (시범)"]];
function modeSelect(current, onpick) {
  const select = document.createElement("select");
  select.className = "operator-mode";
  select.setAttribute("aria-label", "Operator 관찰 모드");
  for (const [value, label] of MODES) {
    const o = document.createElement("option"); o.value = value; o.textContent = label; select.append(o);
  }
  select.value = current || "events";
  select.onchange = () => onpick(select.value);
  return select;
}
/* The status dot's class for a session record, the session list's own
   `.dot` classes: idle / busy / starting / exited, and exited + paused for a
   paused record (drawn blue, apart from a kill's grey). */
function dotClass(record) {
  if (!record) return "dot exited";
  const status = record.status || "exited";
  const paused = record.category === "paused" || (status === "exited" && record.paused_at);
  return `dot ${paused ? "exited paused" : status}`;
}
const STAGES = {open: "열림", in_ready: "준비됨", in_progress: "작업 중", in_review: "머지 요청", blocked: "차단", closed: "닫힘"};
/* One session's work progress as short labels: its status-check answers
   (the user's own questions — commit, tests, merge) and its issue's stage. */
function progressChips(row) {
  const chips = (row.checks || []).filter(c => c.answer).map(c => ({
    label: `${c.name} ${c.answer === "yes" ? "✓" : "✗"}`, title: `${c.question || c.name}: ${c.answer}`,
    kind: c.answer === "yes" ? "yes" : "no"}));
  if (row.issue && row.issue.id) {
    chips.push({label: `${row.issue.id} · ${STAGES[row.issue.status] || row.issue.status}`,
                title: row.issue.title || row.issue.id, kind: `issue-${row.issue.status}`});
  }
  return chips;
}
/* How many feed entries came after the last one the reader saw at the
   bottom: what the "새 내용" button counts. No id yet counts nothing; an id
   the feed no longer holds (trimmed away) counts everything. */
function unseenCount(feed, readId) {
  if (!readId) return 0;
  const list = feed || [];
  const at = list.findIndex(e => e.id === readId);
  return at < 0 ? list.length : list.length - 1 - at;
}
/* Follow-ups by card: an entry with a `parent` (the daemon's update on a
   session the card names, or the bot's reply_to) is listed under that card
   as well as in time order. Children whose card was trimmed away stay in
   the time order only. */
function threadsOf(feed) {
  const ids = new Set((feed || []).map(e => e.id));
  const threads = new Map();
  for (const e of feed || []) {
    if (!e.parent || !ids.has(e.parent)) continue;
    if (!threads.has(e.parent)) threads.set(e.parent, []);
    threads.get(e.parent).push(e);
  }
  return threads;
}
/* The card a follow-up points back to, as one short line. */
function cardLine(card) {
  const first = String(card?.text || "").split("\n").find(line => line.trim()) || "";
  const line = first.replace(/^[#>\-\s]+/, "").replace(/[*`]/g, "").trim();
  return line.length > 60 ? line.slice(0, 59) + "…" : line;
}
/* Updates the daemon wrote in one pass about one session read alike: when
   several cards name that session, each card gets its own copy for its
   thread, and in time order they are one line naming every card. */
function sameUpdate(a, b) {
  return !!a && !!b && a.kind === "update" && b.kind === "update" &&
    a.session === b.session && a.text === b.text && a.at === b.at;
}
const GATE_KINDS = {approval: "승인 대기", selection: "선택 대기", goto: "이동 요청"};
/* The presses that settle one waiting cflow gate: the dashboard's own
   requests (app.js wfActions), with the run's cwd and scope from the
   daemon's gate list. The bot never presses these; the user does. */
function gateActions(gate) {
  const at = {cwd: gate.cwd, scope: gate.scope};
  const where = `${gate.session} · ${gate.step_id || "?"}`;
  if (gate.kind === "goto") {
    const step = (gate.goto_request || {}).step || "?";
    return [
      {label: `'${step}' 이동 승인`, cls: "operator-approve", path: "api/cflow/goto/resolve",
       body: {...at, decision: "approve"}, confirm: `${where}: 런을 '${step}' 스텝으로 옮깁니까?`},
      {label: "거절", cls: "operator-deny", path: "api/cflow/goto/resolve",
       body: {...at, decision: "deny"}, reason: true},
    ];
  }
  if (gate.kind === "selection") {
    return (gate.options || []).map(o => ({
      label: o.name, title: o.description || "", cls: "operator-choice", path: "api/cflow/select",
      body: {...at, option: o.name}, confirm: `${where}: '${o.name}' 선택지를 고릅니까?`}));
  }
  return [{label: "승인", cls: "operator-approve", path: "api/cflow/approve", body: at,
           confirm: `${where}: 게이트를 승인합니까?`}];
}
function operatorLabel(op) {
  if (!op) return "";
  return [op.harness, op.model].filter(Boolean).join(" · ");
}

/* ---- state ---- */
let project = "", data = null, projectNames = [], timer = null, badgeTimer = null;
/* The Start row's choices. The row is rebuilt on every poll, so what the user
   picked lives here rather than in the elements. */
let profileOptions = [], modelIds = {}, harnesses = {};
const startChoice = {profile: "", model: "", effort: "", mode: "events"};
const hosts = new Set();
const drafts = new Map();          // ask id -> note text, so a poll never eats a half-typed note
let composer = "";
const seen = new Set();
let primed = false;

function chosenProject() {
  if (project) return project;
  try { project = localStorage.getItem("claunch-operator-project") || ""; } catch {}
  if (!project && typeof currentProject === "string") project = currentProject;
  return project || "default";
}
function setProject(name) {
  project = name;
  try { localStorage.setItem("claunch-operator-project", name); } catch {}
  data = null; seen.clear(); primed = false;
  for (const h of hosts) { h.scrolled = false; h.readId = null; }
  refresh();
}

/* ---- building one host ---- */
function build(root) {
  root.replaceChildren();
  root.classList.add("operator-host");
  const head = node("header", null, "operator-head");
  const title = node("h1", "Operator");
  const select = node("select"); select.className = "operator-project"; select.setAttribute("aria-label", "프로젝트");
  select.onchange = () => setProject(select.value);
  const status = node("span", "", "operator-status");
  const start = node("span", null, "operator-start");
  head.append(title, select, status, start);
  const body = node("div", null, "operator-body");
  const feedCol = node("section", null, "operator-conversation");
  const feed = node("ol", null, "operator-feed"); feed.setAttribute("aria-live", "polite");
  // What arrived while the reader was scrolled up: a button at the newest
  // end of the feed, instead of a jump that loses their place.
  const newer = node("button", "", "operator-newer"); newer.type = "button"; newer.hidden = true;
  const feedWrap = node("div", null, "operator-feed-wrap");
  feedWrap.append(feed, newer);
  const form = node("form", null, "operator-composer");
  const input = node("textarea"); input.rows = 2; input.maxLength = 12000;
  input.placeholder = "Operator에게 말하기 — 질문, 판단 요청, 다른 세션에 전달할 지시 (Enter 전송, Shift+Enter 줄바꿈)";
  input.setAttribute("aria-label", "Operator에게 보낼 메시지");
  input.value = composer;
  input.oninput = () => { composer = input.value; for (const h of hosts) if (h.parts.input !== input) h.parts.input.value = composer; };
  input.onkeydown = event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); form.requestSubmit(); }
  };
  const send = node("button", "보내기"); send.type = "submit";
  form.append(input, send);
  form.onsubmit = async event => {
    event.preventDefault();
    const text = composer.trim();
    if (!text) return;
    send.disabled = true;
    try {
      await request(`api/operator/message?project=${encodeURIComponent(chosenProject())}`, {text});
      composer = "";
      // The user's own message is where they want to be: back to the bottom.
      for (const h of hosts) { h.parts.input.value = ""; h.scrolled = false; }
      await refresh();
    } catch (err) { notice(err.message); } finally { send.disabled = false; }
  };
  const note = node("p", "", "operator-notice"); note.setAttribute("role", "status");
  feedCol.append(feedWrap, note, form);
  const side = node("aside", null, "operator-side");
  side.setAttribute("aria-label", "관찰 중인 세션");
  body.append(feedCol, side);
  root.append(head, body);
  const h = {root, parts: {select, status, start, feed, newer, side, input, note}};
  feed.addEventListener("scroll", () => { if (nearBottom(feed)) markRead(h); });
  newer.onclick = () => { feed.scrollTop = feed.scrollHeight; markRead(h); };
  return h;
}

function nearBottom(list) { return list.scrollHeight - list.scrollTop - list.clientHeight < 40; }
/* The reader has seen the feed down to its newest entry. */
function markRead(h) {
  const feed = data?.feed || [];
  h.readId = feed.length ? feed[feed.length - 1].id : null;
  h.parts.newer.hidden = true;
}
function showUnseen(h) {
  const count = unseenCount(data?.feed, h.readId);
  h.parts.newer.hidden = !count;
  h.parts.newer.textContent = `새 내용 ${count}건 ↓`;
}
/* The entry at the top of the reader's view and how far it sits from the
   list's top edge, so a rebuild can put the same entry back in the same spot. */
function topEntry(list) {
  const top = list.getBoundingClientRect().top;
  for (const li of list.children) {
    const box = li.getBoundingClientRect();
    if (box.bottom > top) return {id: li.dataset.entry, offset: box.top - top};
  }
  return null;
}

function notice(text) { for (const h of hosts) h.parts.note.textContent = text || ""; }

function renderHead(h) {
  const {select, status, start} = h.parts;
  const names = projectNames.length ? projectNames : [chosenProject()];
  if ([...select.options].map(o => o.value).join("\n") !== names.join("\n")) {
    select.replaceChildren(...names.map(n => { const o = node("option", n); o.value = n; return o; }));
  }
  select.value = chosenProject();
  const op = data?.operator;
  // The bot's own session is an ordinary claunch session: its name links to
  // its terminal (#/s/<name>), the same destination as every session row.
  status.replaceChildren();
  if (!data) status.textContent = "불러오는 중…";
  else if (!op) status.textContent = "Operator 없음";
  else {
    const link = sessionLink(op.name, "operator-self");
    link.title = "Operator 세션의 터미널 열기";
    const label = operatorLabel(op);
    status.append(link, ` · ${op.running ? (op.status || "running") : "종료됨"}` + (label ? ` · ${label}` : ""));
  }
  status.dataset.state = !op ? "none" : op.running ? "running" : "ended";
  if (start.contains(document.activeElement)) return;  // a picker is open: do not rebuild under it
  start.replaceChildren();
  if (data && (!op || !op.running)) renderStart(start);
  else if (op && op.running) start.append(modeSelect(data.mode, switchMode));
}

function renderStart(start) {
  const offered = startChoices(profileOptions, harnesses, startChoice.profile);
  startChoice.profile = offered.profile;
  const picker = (label, values, current, labelOf, onpick) => {
    const select = node("select"); select.setAttribute("aria-label", label);
    for (const v of values) { const o = node("option", labelOf(v)); o.value = v; select.append(o); }
    select.value = current;
    select.onchange = () => { onpick(select.value); start.replaceChildren(); renderStart(start); };
    return select;
  };
  start.append(picker("Operator 세션의 프로필", profileOptions.map(o => o.value), offered.profile,
    v => (profileOptions.find(o => o.value === v) || {}).label || v,
    v => { startChoice.profile = v; }));
  const ids = modelIds[offered.profile] || {};
  if (offered.models.length) {
    if (!offered.models.includes(startChoice.model)) startChoice.model = "";
    start.append(picker("Operator 세션의 모델", ["", ...offered.models], startChoice.model,
      v => !v ? "모델: harness 기본값" : ids[v] ? `${v} (${ids[v]})` : v,
      v => { startChoice.model = v; }));
  }
  if (offered.efforts.length) {
    if (!offered.efforts.includes(startChoice.effort)) startChoice.effort = "";
    start.append(picker("Operator 세션의 effort", ["", ...offered.efforts], startChoice.effort,
      v => v || "effort: 기본값", v => { startChoice.effort = v; }));
  }
  start.append(modeSelect(startChoice.mode, v => { startChoice.mode = v; }));
  const go = node("button", "Operator 시작"); go.type = "button";
  go.disabled = !offered.profile;
  go.onclick = async () => {
    go.disabled = true;
    const current = startChoices(profileOptions, harnesses, startChoice.profile);
    try { await request("api/operator/start", startBody(chosenProject(), startChoice, current)); await refresh(); }
    catch (err) { notice(err.message); } finally { go.disabled = false; }
  };
  start.append(go);
}

/* Switch the running operator's mode; the daemon records it in the feed and
   nudges the bot, which applies it from its next poll. */
async function switchMode(mode) {
  try {
    await request("api/operator/mode", {project: chosenProject(), mode});
    await refresh();
  } catch (err) { notice(err.message); await refresh(); }
}

/* The session record behind a label: the rail's list (every page keeps it
   current), else the panel row this view already has. */
function sessionRecord(name) {
  const list = typeof sessionsCache !== "undefined" && Array.isArray(sessionsCache) ? sessionsCache : [];
  return list.find(s => s.name === name) || (data?.sessions || []).find(s => s.name === name) || null;
}
/* A session label: its status dot, its name, a link to its terminal, and —
   on mouse hover (after the grid's delay) or keyboard focus — the session
   list's card for it, the grid view's hover card. */
let tipAnchor = null;   // the label a hover card is up (or pending) for
function sessionLink(name, cls = "operator-ref") {
  const a = node("a", null, cls);
  a.href = "#/s/" + encodeURIComponent(name);
  a.append(node("span", null, dotClass(sessionRecord(name))), node("span", name));
  a.setAttribute("aria-label", `${name} 세션 열기`);
  if (typeof showSessionCardTip === "function") {
    a.addEventListener("pointerenter", ev => { if (ev.pointerType === "mouse") { tipAnchor = a; scheduleSessionCardTip(a, name); } });
    a.addEventListener("focus", () => { if (a.matches(":focus-visible")) showSessionCardTip(a, name); });
    for (const type of ["pointerleave", "blur", "pointerdown"]) a.addEventListener(type, hideSessionGridTip);
  }
  return a;
}
/* Session names in prose ("s751의 peer-review", "s638, s697") read as the
   same links the refs row draws. Only names this page knows become links, so
   an "s3" that is not a session stays text; words inside a link or code are
   left alone. sessionWords is the pure split (tests/web/operator_check.js). */
const SESSION_WORD = /(?<![\w-])s\d{1,5}(?![\w-])/g;
function sessionWords(text, known) {
  const out = [];
  let last = 0;
  for (const m of String(text ?? "").matchAll(SESSION_WORD)) {
    if (!known(m[0])) continue;
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push({session: m[0]});
    last = m.index + m[0].length;
  }
  if (last < String(text ?? "").length) out.push(String(text).slice(last));
  return out;
}
function linkSessions(root) {
  const known = name => !!sessionRecord(name);
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: t => t.parentElement?.closest("a, code, pre") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
  });
  const texts = [];
  for (let t = walker.nextNode(); t; t = walker.nextNode()) texts.push(t);
  for (const t of texts) {
    const parts = sessionWords(t.nodeValue, known);
    if (!parts.some(x => typeof x !== "string")) continue;
    t.replaceWith(...parts.map(x => typeof x === "string" ? x : sessionLink(x.session)));
  }
  return root;
}

function renderAsk(item, e) {
  if (e.answer) {
    item.append(node("p", `답변: ${answerLine(e)}`, "operator-answer"));
    return;
  }
  const box = node("div", null, "operator-ask-actions");
  const note = node("textarea"); note.rows = 1; note.maxLength = 12000;
  note.placeholder = e.type === "text" ? "답변 입력" : "메모 (선택)";
  note.setAttribute("aria-label", note.placeholder);
  note.value = drafts.get(e.id) || "";
  note.oninput = () => drafts.set(e.id, note.value);
  const answer = async body => {
    for (const b of box.querySelectorAll("button")) b.disabled = true;
    try {
      await request(`api/operator/asks/${encodeURIComponent(e.id)}/answer?project=${encodeURIComponent(chosenProject())}`,
        {...body, text: note.value.trim() || undefined});
      drafts.delete(e.id);
      await refresh();
    } catch (err) { notice(err.message); for (const b of box.querySelectorAll("button")) b.disabled = false; }
  };
  const button = (label, cls, body) => { const b = node("button", label, cls); b.type = "button"; b.onclick = () => answer(body); return b; };
  if (e.type === "approve") box.append(button("승인", "operator-approve", {decision: "approve"}), button("거절", "operator-deny", {decision: "deny"}));
  else if (e.type === "choice") for (const c of e.choices || []) box.append(button(c, "operator-choice", {decision: c}));
  else box.append(button("답변 보내기", "operator-approve", {}));
  item.append(note, box);
}

function renderFeed(h) {
  const list = h.parts.feed;
  const atBottom = nearBottom(list);
  const focused = list.contains(document.activeElement) ? document.activeElement : null;
  if (focused) return;  // never rebuild under the reader's cursor
  // Scrolled up: the rebuild keeps the reader's place (replaceChildren alone
  // would drop it to the top).
  const keep = atBottom || !h.scrolled ? null : {top: list.scrollTop, entry: topEntry(list)};
  list.replaceChildren();
  const feed = data?.feed || [];
  if (!feed.length) {
    list.append(node("li", data?.operator ? "아직 게시된 내용이 없습니다." : "이 프로젝트에는 Operator가 없습니다. 위에서 시작하십시오.", "operator-empty"));
  }
  const threads = threadsOf(feed);
  const byId = new Map(feed.map(e => [e.id, e]));
  // A waiting gate's buttons go under the newest card that names its
  // session, not under every card that ever did.
  const gates = new Map((data?.gates || []).map(g => [g.session, g]));
  const gateCard = new Map();
  for (const e of feed) {
    if ((e.kind === "post" || e.kind === "ask") && !e.parent) for (const r of e.refs || []) if (gates.has(r)) gateCard.set(r, e.id);
  }
  let last = null;
  for (const e of feed) {
    if (e.parent) {
      if (sameUpdate(last && last.entry, e)) { parentLink(last.item, byId.get(e.parent)); continue; }
      const follow = renderFollowup(e, byId.get(e.parent));
      follow.dataset.entry = e.id;
      last = {entry: e, item: follow};
      list.append(follow);
      continue;
    }
    last = null;
    const item = node("li", null, `operator-item operator-${e.kind}` + (e.level ? ` operator-level-${e.level}` : "") +
                      (e.event ? ` operator-event-${e.event}` : ""));
    item.dataset.id = e.id; item.dataset.entry = e.id;
    const meta = node("div", null, "operator-meta");
    const who = e.role === "user" ? "나" : e.role === "system" ? "system" : "Operator";
    meta.append(node("strong", who));
    const time = node("time", new Date(e.at).toLocaleTimeString()); time.dateTime = e.at; meta.append(time);
    if (e.level && e.level !== "info") meta.append(node("span", e.level === "urgent" ? "즉시 조치" : "확인 필요", "operator-level"));
    item.append(meta);
    if (e.kind === "dispatch") {
      const line = node("p");
      line.append("→ ", sessionLink(e.target), ` 에 전달 (${deliveryLabel(e.delivery)}): `);
      line.append(linkSessions(node("span", e.text)));
      item.append(line);
    } else if (e.kind === "user" || e.kind === "system") {
      item.append(linkSessions(node("p", e.text)));
    } else {
      const body = node("div", null, "operator-text");
      if (typeof mdInto === "function") mdInto(body, e.text); else body.textContent = e.text;
  linkSessions(body);
      item.append(body);
    }
    if ((e.refs || []).length) {
      const refs = node("div", null, "operator-refs");
      for (const r of e.refs) refs.append(sessionLink(r));
      item.append(refs);
    }
    if (e.kind === "ask") renderAsk(item, e);
    for (const r of e.refs || []) if (gateCard.get(r) === e.id) item.append(renderGate(gates.get(r)));
    if (threads.has(e.id)) item.append(renderThread(threads.get(e.id)));
    list.append(item);
  }
  if (!keep) { list.scrollTop = list.scrollHeight; h.scrolled = true; markRead(h); return; }
  list.scrollTop = keep.top;
  const again = keep.entry?.id && list.querySelector(`:scope > li[data-entry="${CSS.escape(keep.entry.id)}"]`);
  if (again) list.scrollTop += again.getBoundingClientRect().top - list.getBoundingClientRect().top - keep.entry.offset;
  showUnseen(h);
}

/* A follow-up in time order: one compact line that names its card, which
   scrolls to the card and marks it. */
function renderFollowup(e, card) {
  const item = node("li", null, `operator-item operator-followup operator-${e.kind}`);
  const meta = node("div", null, "operator-meta");
  meta.append(node("strong", e.role === "bot" ? "Operator" : "system"));
  const time = node("time", new Date(e.at).toLocaleTimeString()); time.dateTime = e.at; meta.append(time);
  item.append(meta, followupBody(e));
  parentLink(item, card);
  if (e.kind === "ask") renderAsk(item, e);
  return item;
}
/* One more "↳ card" link on a follow-up line: scrolls to that card and marks it. */
function parentLink(item, card) {
  if (!card) return;
  const back = node("a", `↳ ${cardLine(card) || "원래 카드"}`, "operator-parent");
  back.href = "#";
  back.onclick = ev => {
    ev.preventDefault();
    const target = item.parentElement?.querySelector(`li[data-id="${CSS.escape(card.id)}"]`);
    if (!target) return;
    target.scrollIntoView({block: "center"});
    target.classList.add("operator-flash");
    setTimeout(() => target.classList.remove("operator-flash"), 1600);
  };
  item.querySelector(".operator-meta").append(back);
}
/* A cflow gate waiting on the user, with the presses that settle it. */
function renderGate(g) {
  const box = node("div", null, "operator-gate");
  const head = node("div", null, "operator-gate-head");
  head.append(sessionLink(g.session), node("span", `${GATE_KINDS[g.kind] || g.kind} · ${g.step_id || "?"}`, "operator-badge operator-badge-waiting"));
  box.append(head);
  const goto = g.goto_request || {};
  const text = g.kind === "goto" ? `'${goto.from || g.step_id}' → '${goto.step}': ${goto.reason || ""}` : (g.prompt || g.title || "");
  if (text) box.append(linkSessions(node("p", text, "operator-gate-text")));
  const actions = node("div", null, "operator-gate-actions");
  const buttons = () => actions.querySelectorAll("button");
  for (const a of gateActions(g)) {
    const b = node("button", a.label, a.cls); b.type = "button";
    if (a.title) b.title = a.title;
    b.onclick = async () => {
      if (a.confirm && !confirm(a.confirm)) return;
      let body = a.body;
      if (a.reason) {
        const why = prompt("거절 사유(선택 — 에이전트가 읽습니다):", "");
        if (why === null) return;
        body = {...body, reason: why.trim() || undefined};
      }
      for (const x of buttons()) x.disabled = true;
      try { await request(a.path, body); await refresh(); }
      catch (err) { notice(err.message); for (const x of buttons()) x.disabled = false; }
    };
    actions.append(b);
  }
  box.append(actions);
  return box;
}
function followupBody(e) {
  if (e.kind === "update") {
    const p = node("p", null, "operator-update");
    if (e.session) p.append(sessionLink(e.session), " ");
    p.append(linkSessions(node("span", e.text)));
    return p;
  }
  const body = node("div", null, "operator-text");
  if (typeof mdInto === "function") mdInto(body, e.text); else body.textContent = e.text;
  linkSessions(body);
  return body;
}
/* The same follow-ups under their card, oldest first. */
function renderThread(children) {
  const thread = node("ol", null, "operator-thread");
  thread.setAttribute("aria-label", `후속 ${children.length}건`);
  for (const c of children) {
    const li = node("li");
    const time = node("time", new Date(c.at).toLocaleTimeString()); time.dateTime = c.at;
    li.append(time, followupBody(c));
    if (c.kind === "ask") li.append(node("p", c.answer ? `답변: ${answerLine(c)}` : "답변 대기", "operator-answer"));
    thread.append(li);
  }
  return thread;
}

function renderSide(h) {
  const side = h.parts.side;
  side.replaceChildren();
  const asks = openAsks(data?.feed);
  const pausedCount = (data?.sessions || []).filter(r => r.category === "paused").length;
  const head = node("h2", `관찰 중인 세션 ${(data?.sessions || []).length - pausedCount}` + (pausedCount ? ` · 일시정지 ${pausedCount}` : ""));
  side.append(head);
  if (asks.length) side.append(node("p", `응답을 기다리는 질문 ${asks.length}건`, "operator-pending"));
  const gates = data?.gates || [];
  if (gates.length) {
    side.append(node("h2", `승인을 기다리는 cflow 게이트 ${gates.length}`));
    const box = node("div", null, "operator-gates");
    for (const g of gates) box.append(renderGate(g));
    side.append(box);
  }
  const list = node("ul", null, "operator-sessions");
  for (const row of data?.sessions || []) {
    const item = node("li");
    const badge = sessionBadge(row);
    const top = node("div", null, "operator-session-top");
    top.append(sessionLink(row.name), node("span", badge.label, `operator-badge operator-badge-${badge.kind}`));
    item.append(top);
    if (row.category === "paused") item.classList.add("operator-paused");
    const chips = progressChips(row);
    if (chips.length) {
      const line = node("div", null, "operator-progress");
      for (const c of chips) { const chip = node("span", c.label, `operator-chip operator-chip-${c.kind}`); chip.title = c.title; line.append(chip); }
      item.append(line);
    }
    if (row.summary) item.append(node("p", row.summary, "operator-summary"));
    list.append(item);
  }
  if (!list.children.length) list.append(node("li", "실행 중인 세션이 없습니다.", "operator-empty"));
  side.append(list);
  const link = node("a", "Observer에서 자세히 보기"); link.href = "#/observer";
  side.append(link);
}

function render() {
  for (const h of hosts) {
    if (!h.root.isConnected) continue;
    renderHead(h); renderFeed(h); renderSide(h);
  }
  // A poll rebuilt the label the card belongs to: the card goes with it.
  if (tipAnchor && !tipAnchor.isConnected) { tipAnchor = null; if (typeof hideSessionGridTip === "function") hideSessionGridTip(); }
}

/* ---- polling, badge, notification ---- */
function announce(feed) {
  const fresh = freshUrgent(seen, feed);
  for (const e of feed || []) seen.add(e.id);
  if (!primed) { primed = true; return; }   // the first load is history, not news
  if (!fresh.length || typeof Notification === "undefined" || Notification.permission !== "granted") return;
  if (document.visibilityState === "visible" && [...hosts].some(h => h.root.isConnected && h.root.offsetParent)) return;
  try { new Notification("Operator", {body: fresh[0].text.slice(0, 160), tag: "claunch-operator"}); } catch {}
}
function setBadge(total) {
  for (const id of ["operator-nav-badge", "operator-fab-badge"]) {
    const badge = document.getElementById(id);
    if (!badge) continue;
    badge.textContent = total ? String(total) : "";
    badge.hidden = !total;
  }
  fab.classList.toggle("operator-fab-alert", !!total);
}
/* While the Operator is on screen everything in it is in front of the user,
   so each badge poll marks it seen again: a seen that failed once (a daemon
   without the route, a stale cookie) is retried, and an ask that arrives
   while the page is open does not pile up on the badge. */
async function refreshBadge() {
  if (pageOpen || modal.open) return markSeen();
  try { setBadge((await request("api/operator/pending")).total); } catch {}
}
/* Opening (and leaving) the Operator is taking notice of what waits there:
   the badge drops to 0 and counts only what is asked after, answered or not. */
async function markSeen() {
  setBadge(0);
  try { setBadge((await request("api/operator/seen", {})).total); } catch {}
}
async function refresh() {
  try {
    const [view, projectList] = await Promise.all([
      request(`api/operator?project=${encodeURIComponent(chosenProject())}`),
      projectNames.length ? null : request("api/projects").catch(() => null),
    ]);
    data = view;
    if (projectList) projectNames = (projectList.projects || []).map(p => p.name);
    announce(view.feed);
    render();
  } catch (err) { notice(err.message); }
  refreshBadge();
}
/* The launchable profile choices (profile:harness selectors, the list the
   new-session form offers) and each harness's model/effort choices. */
async function loadProfiles() {
  if (profileOptions.length) return;
  try {
    const [profilesDoc, harnessDoc] = await Promise.all([
      request("api/profiles"), request("api/harnesses").catch(() => ({harnesses: []}))]);
    profileOptions = (profilesDoc.profile_options || []).map(o => ({value: o.value, label: o.label || o.value, harness: o.harness}));
    if (!profileOptions.length) profileOptions = (profilesDoc.profiles || []).map(p => ({value: p, label: p, harness: ""}));
    modelIds = {};
    for (const d of profilesDoc.profile_details || []) modelIds[d.name] = d.model_ids || {};
    // A profile's default row is named by the bare profile; its option by profile:harness.
    for (const o of profilesDoc.profile_options || []) if (o.default && modelIds[o.profile]) modelIds[o.value] = modelIds[o.profile];
    harnesses = {};
    for (const h of harnessDoc.harnesses || []) if (h && h.name) harnesses[h.name] = h;
  } catch {}
}
function poll() {
  clearInterval(timer);
  timer = setInterval(() => { if (document.visibilityState === "visible") refresh(); }, 4000);
}
function attach(root) {
  for (const h of hosts) if (h.root === root) return h;
  const h = build(root);
  hosts.add(h);
  loadProfiles().then(render);
  return h;
}
/* Hosts are kept for reuse; only the poll stops once neither is on screen.
   The badge keeps its own slower poll either way. */
function detachIdle() {
  if (!pageOpen && !modal.open) { clearInterval(timer); timer = null; }
}

/* ---- the page ---- */
let pageOpen = false;
function open() {
  pageOpen = true;
  document.body.classList.add("operator-page");
  attach(document.getElementById("operator-view"));
  if (typeof Notification !== "undefined" && Notification.permission === "default") {
    try { Notification.requestPermission(); } catch {}
  }
  refresh(); poll(); markSeen();
}
function stop() {
  if (pageOpen) markSeen();
  pageOpen = false; document.body.classList.remove("operator-page"); detachIdle();
}

/* ---- the modal ---- */
const modal = document.createElement("dialog");
modal.className = "operator-modal"; modal.setAttribute("aria-label", "Operator");
const modalClose = node("button", "×", "operator-modal-close"); modalClose.type = "button";
modalClose.setAttribute("aria-label", "닫기"); modalClose.title = "닫기 (Esc)";
modalClose.onclick = () => modal.close();
const modalRoot = node("div", null, "operator-modal-root");
modal.append(modalClose, modalRoot);
modal.addEventListener("close", () => { markSeen(); detachIdle(); });
// A session link inside the modal goes to that session's page; the modal
// would otherwise stay over the terminal it just opened.
modalRoot.addEventListener("click", event => {
  if (event.target.closest?.('a[href^="#/"]')) modal.close();
});
document.body.append(modal);
function openModal() {
  if (!modal.open) modal.showModal();
  attach(modalRoot);
  refresh(); poll(); markSeen();
  modalRoot.querySelector(".operator-composer textarea")?.focus();
}

/* The floating button: always there, whatever page is on screen. */
const fab = node("button", null, "operator-fab"); fab.type = "button";
fab.title = "Operator 열기 (Alt+O)"; fab.setAttribute("aria-label", "Operator 열기 (Alt+O)");
fab.append(node("span", "◍", "operator-fab-icon"), node("span", "Operator", "operator-fab-label"));
const fabBadge = node("span", "", "operator-badge-count"); fabBadge.id = "operator-fab-badge"; fabBadge.hidden = true;
fab.append(fabBadge);
fab.onclick = () => modal.open ? modal.close() : openModal();
document.body.append(fab);
document.addEventListener("keydown", event => {
  if (event.altKey && !event.ctrlKey && !event.metaKey && (event.key === "o" || event.key === "O")) {
    event.preventDefault();
    modal.open ? modal.close() : openModal();
  }
});
refreshBadge();
badgeTimer = setInterval(() => { if (document.visibilityState === "visible") refreshBadge(); }, 15000);

return {open, stop, openModal, refresh,
        _test: {openAsks, freshUrgent, answerLine, deliveryLabel, sessionBadge, startChoices, startBody, dotClass, progressChips,
                threadsOf, cardLine, sameUpdate, gateActions, sessionWords}};
})();
