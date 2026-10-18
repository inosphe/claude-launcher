/* Operator v2 on the Observer page (claunch-iw5i8.4): the operators as tabs
   above the session cards, the selected one's conversation, and the form that
   creates or edits one. The section has an input row of its own, and the
   page's composer can also talk to an operator — observer.js asks this module
   for the targets it offers and hands it the text. There is no live channel: observer.js polls refresh() with its own
   ten-second tick, and every write here reads again at once.
   The feed draws its newest entries and opens the rest in place, the way a
   session card does. On the page the section sits in the Operator dock, whose
   conversation scrolls inside the dock on a wide screen (observer.css).
   The floating Operator (operator_fab.js, claunch-iw5i8.5) draws the same
   tabs and conversation in its dialog: OperatorView.create makes one view per
   host, each with its own list, selection and feed. Options:
     key      the localStorage key that keeps the selection
     active() whether the conversation is in front of the reader now (an
              unseen ask shown then is marked seen)
     scroller() the element whose scroll a redraw keeps
     manage   the 「+ 새 operator」 tab, the form, start/stop, watch, edit
              and delete (the page); off, the head links to the page instead
     composer an input row of the view's own (the page uses its composer)
   A view with its own input row also holds a session line above the
   conversation (claunch-iw5i8.13): the selected operator, its session and
   that session's state, and a link to the session's page. It sits outside
   the conversation's scroller, so it stays on screen however far the reader
   has scrolled. */
globalThis.OperatorView = {create: options => operatorView(options || {})};
/* On the page the view lives in the Operator dock (claunch-iw5i8.8), the
   right-hand column observer.js opens and closes, with an input row of its
   own. Its conversation is in front of the reader only while the dock is
   open. On a wide screen the dock is held on screen and its conversation
   scrolls inside it; on a phone it is part of the page, which scrolls. */
const dockOpen = () => document.getElementById("observer-view")?.classList.contains("dock-open");
globalThis.ObserverOperators = OperatorView.create({
  key: "claunch-observer-operator", manage: true, composer: true,
  active: () => document.body.classList.contains("observer-active") && dockOpen(),
  scroller: () => {
    const area = document.querySelector("#observer-operators .op-area");
    return area && /auto|scroll/.test(getComputedStyle(area).overflowY) ? area : document.getElementById("observer-view");
  },
});
function operatorView(opts) {
"use strict";
const FEED_SHOWN = 20;
const LEVELS = {info:"알림",attention:"확인 필요",urgent:"긴급"};
const node = (tag, text, cls) => { const e = document.createElement(tag); e.textContent = text; if(cls)e.className=cls; return e; };
let root = null, area = null, bar = null, onChange = () => {};
let list = [], detail = null, selected = "", loaded = false, listError = "";
let projects = [], profiles = [], form = null, formError = "", busy = false, expanded = false;
let listSignature = "", task = null;
const askDrafts = new Map();
try { selected = localStorage.getItem(opts.key) || ""; } catch { /* this visit only */ }
/* api() is app.js's (cookie session, relogin). The daemon answers a refusal
   either as {"error": ...} or, for a 409 it raises, as plain text. */
async function call(path, method = "GET", body) {
  const options = {method};
  if(body !== undefined) { options.headers = {"Content-Type":"application/json"}; options.body = JSON.stringify(body); }
  const response = await api(path, options);
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : {}; } catch { data = null; }
  if(!response.ok) throw Error(data?.error || text || `HTTP ${response.status}`);
  return data || {};
}
const opPath = (id, rest = "") => `api/operators/${encodeURIComponent(id)}${rest}`;
const current = () => list.find(o => o.id === selected) || null;
function choose(id) {
  const moved = id !== selected;
  selected = id; expanded = false; detail = detail?.id === id ? detail : null;
  if(moved) syncDraft();
  try { localStorage.setItem(opts.key, id); } catch { /* this visit only */ }
}
/* Whose scope a session is in: an all-projects operator covers every session,
   a scoped one the sessions filed under its projects. The session's project is
   the app's session list (sessionsCache), the same record the rail reads; a
   session the list has not reached yet reads as the default project, the way
   the daemon's projects.normalize reads a record without one. A session
   read on its own (focusSession, before the list's first poll lands) fills in
   for a list that has not reached it. */
const sessionRows = new Map();
function projectOf(name) {
  const rows = typeof sessionsCache !== "undefined" && Array.isArray(sessionsCache) ? sessionsCache : [];
  const row = rows.find(s => s.name === name) || sessionRows.get(name);
  return String(row?.project || "").trim() || "default";
}
const listed = name => typeof sessionsCache !== "undefined" && Array.isArray(sessionsCache) && sessionsCache.some(s => s.name === name);
const covers = (op, name) => op.all || (op.projects || []).includes(projectOf(name));
/* The operator to ask about one session: the selected one when it covers the
   session, else the first that does; null when none does. */
function ownerOf(name) {
  const own = current();
  if(own && covers(own, name)) return own;
  return list.find(op => covers(op, name)) || null;
}
const scopeText = op => op.all ? "전체 프로젝트" : (op.projects || []).join(", ") || "범위 없음";
const running = op => !!op?.operator?.running;
function stateText(op) {
  if(!op.operator) return "정지됨 · 세션 없음";
  if(!op.operator.running) return `정지됨 · ${op.operator.name}`;
  return `실행 중 · ${op.operator.name}${op.operator.status ? ` (${op.operator.status})` : ""}`;
}
/* Reads the list and, when one is selected, that operator with its feed. A
   failed list read leaves the section saying so; the rest of the page goes on. */
function refresh() {
  if(task) return task;
  task = (async () => {
    try {
      const data = await call("api/operators");
      list = Array.isArray(data.operators) ? data.operators : [];
      listError = "";
    } catch(err) { listError = err.message; }
    loaded = true;
    if(!list.some(o => o.id === selected)) choose(list[0]?.id || "");
    if(selected) {
      try {
        detail = await call(opPath(selected));
        if(detail.unseen > 0 && !document.hidden && opts.active?.()) {
          const seen = await call(opPath(selected, "/seen"), "POST", {});
          list = list.map(o => o.id === seen.id ? {...o, ...seen} : o);
        }
      } catch(err) { detail = {id: selected, error: err.message, feed: []}; }
    } else detail = null;
    const signature = JSON.stringify(list.map(o => [o.id, o.name, o.all, o.projects]));
    if(signature !== listSignature) { listSignature = signature; onChange(); }
    // An ask answer being typed survives a poll that would redraw it away.
    if(!area?.contains(document.activeElement) || !document.activeElement.matches("textarea,input,select")) render();
  })().finally(() => { task = null; });
  return task;
}
async function act(work) {
  if(busy) return;
  busy = true; render();
  try { await work(); formError = ""; }
  catch(err) { if(form) formError = err.message; else notice(err.message); }
  finally { busy = false; await refresh(); render(); }
}
let noticeText = "";
function notice(text) { noticeText = text; }
/* -- the form ---------------------------------------------------------- */
async function openForm(op, message = "") {
  form = op ? {id: op.id, name: op.name, all: op.all, projects: [...(op.projects || [])], profile: op.profile || "", watch: op.watch}
            : {id: "", name: "", all: false, projects: [], profile: "", watch: true};
  formError = message;
  render();
  const [p, f] = await Promise.all([call("api/projects").catch(() => ({})), call("api/profiles").catch(() => ({}))]);
  projects = (p.projects || []).map(x => x.name).filter(Boolean);
  // The launchable selectors (profile:harness), as the new-session form offers them.
  profiles = (f.profile_options || []).map(o => ({value: o.value, label: o.label || o.value}));
  if(!profiles.length) profiles = (f.profiles || []).map(x => typeof x === "string" ? {value: x, label: x} : {value: x.name, label: x.name}).filter(x => x.value);
  render();
}
function formBox() {
  const box = node("form", "", "op-form");
  box.append(node("h3", form.id ? `「${form.name}」 설정` : "새 operator"));
  const name = document.createElement("input");
  name.id = "observer-op-name"; name.value = form.name; name.maxLength = 80; name.required = true;
  name.oninput = () => { form.name = name.value; };
  const nameLabel = node("label", "이름 ");nameLabel.append(name);
  const every = document.createElement("input");
  every.type = "checkbox"; every.id = "observer-op-all"; every.checked = form.all;
  const everyLabel = node("label", "", "op-all");everyLabel.append(every, " 전체 프로젝트");
  const set = node("fieldset", "", "op-projects");
  set.append(node("legend", "대상 프로젝트"));
  const names = [...new Set([...projects, ...form.projects])];
  if(!names.length) set.append(node("small", "프로젝트 목록을 불러오는 중…"));
  for(const project of names) {
    const box = document.createElement("input");
    box.type = "checkbox"; box.value = project; box.checked = form.projects.includes(project); box.disabled = form.all;
    box.onchange = () => { form.projects = box.checked ? [...form.projects, project] : form.projects.filter(p => p !== project); };
    const label = node("label", "");label.append(box, " " + project);set.append(label);
  }
  every.onchange = () => { form.all = every.checked; for(const b of set.querySelectorAll("input"))b.disabled = form.all; };
  const profile = document.createElement("select");
  profile.id = "observer-op-profile";
  for(const [value, label] of [["", "프로필 선택 (시작할 때 필요)"], ...profiles.map(p => [p.value, p.label])]) {
    const o = node("option", label);o.value = value;profile.append(o);
  }
  if(form.profile && !profiles.some(p => p.value === form.profile)) { const o = node("option", form.profile);o.value = form.profile;profile.append(o); }
  profile.value = form.profile;
  profile.onchange = () => { form.profile = profile.value; };
  const profileLabel = node("label", "프로필 ");profileLabel.append(profile);
  const watch = document.createElement("input");
  watch.type = "checkbox"; watch.checked = form.watch;
  watch.onchange = () => { form.watch = watch.checked; };
  const watchLabel = node("label", "");watchLabel.append(watch, " 감시 켜기");
  const save = node("button", form.id ? "저장" : "만들기");save.type = "submit";save.disabled = busy;
  const cancel = node("button", "취소");cancel.type = "button";
  cancel.onclick = () => { form = null; formError = ""; render(); };
  const row = node("div", "", "op-form-actions");row.append(save, cancel);
  box.append(nameLabel, everyLabel, set, profileLabel, watchLabel, row);
  if(formError) box.append(node("p", formError, "op-error"));
  box.onsubmit = event => {
    event.preventDefault();
    if(!form.all && !form.projects.length) { formError = "프로젝트를 하나 이상 고르거나 「전체 프로젝트」를 켜십시오."; render(); return; }
    const body = {name: form.name.trim(), all: form.all, projects: form.all ? [] : form.projects, profile: form.profile || null, watch: form.watch};
    act(async () => {
      const saved = form.id ? await call(opPath(form.id), "PATCH", body) : await call("api/operators", "POST", body);
      choose(saved.id); form = null;
    });
  };
  return box;
}
/* -- the conversation -------------------------------------------------- */
function sessionLink(name) {
  const a = node("a", name, "op-ref");a.href = "#/observer/session/" + encodeURIComponent(name);return a;
}
/* A message body: markdown through the page's renderer (app.js mdInto, which
   builds nodes and never parses HTML), plain text when the page has none. */
function bodyOf(text, cls) {
  const body = node("div", "", cls);
  if(typeof globalThis.mdInto === "function") { body.classList.add("md"); globalThis.mdInto(body, text || ""); }
  else { body.classList.add("plain"); body.textContent = text || ""; }
  return body;
}
function askAnswer(op, e) {
  const box = node("div", "", "op-ask-answer");
  if(e.answer) {
    const said = e.type === "approve" ? (e.answer.decision === "approve" ? "승인" : "거절") : e.answer.decision;
    box.append(node("small", ["사용자 답변", said].filter(Boolean).join(" · ")));
    if(e.answer.text) box.append(bodyOf(e.answer.text, "op-text op-answer-text"));
    return box;
  }
  const field = document.createElement("textarea");
  field.rows = 2; field.value = askDrafts.get(e.id) || "";
  field.placeholder = e.type === "text" ? "답변을 입력하십시오" : "덧붙일 말 (선택)";
  field.setAttribute("aria-label", "operator 질문에 대한 답변");
  field.oninput = () => askDrafts.set(e.id, field.value);
  const answer = (decision) => act(async () => {
    const body = decision === undefined ? {text: field.value} : {decision, ...(field.value.trim() ? {text: field.value} : {})};
    await call(opPath(op.id, `/asks/${encodeURIComponent(e.id)}/answer`), "POST", body);
    askDrafts.delete(e.id);
  });
  const buttons = node("div", "", "op-ask-buttons");
  const button = (label, decision) => {
    const b = node("button", label);b.type = "button";b.disabled = busy;
    b.onclick = () => { if(decision === undefined && !field.value.trim()) return; answer(decision); };
    buttons.append(b);
  };
  if(e.type === "approve") { button("승인", "approve"); button("거절", "deny"); }
  else if(e.type === "choice") for(const c of e.choices || []) button(c, c);
  else button("답변 전송", undefined);
  box.append(field, buttons);
  return box;
}
function entryItem(op, e) {
  const item = node("div", "", `op-entry op-${e.kind}${e.level ? " level-" + e.level : ""}${e.parent ? " op-reply" : ""}`);
  item.dataset.entry = e.id;
  const who = e.kind === "user" ? "나" : e.kind === "system" ? "시스템" : e.kind === "dispatch" ? "전달" : op.name;
  const head = node("small", `${who} · ${new Date(e.at).toLocaleString("ko-KR")}`);
  if(e.kind === "post" || e.kind === "ask") head.textContent += ` · ${LEVELS[e.level] || e.level || ""}`;
  if(e.kind === "ask" && !e.answer) head.textContent += " · 답변 대기";
  item.append(head);
  if(e.kind === "dispatch") {
    const line = node("div", "");
    line.append(node("span", "→ "), sessionLink(e.target || ""), node("span", `: ${e.text || ""}`));
    item.append(line);
    item.append(node("small", [e.on_behalf_of ? `근거 ${e.on_behalf_of}` : "", e.delivery ? `전달 ${e.delivery}` : ""].filter(Boolean).join(" · ")));
  } else item.append(bodyOf(e.text, "op-text"));
  if((e.refs || []).length) {
    const refs = node("div", "", "op-refs");
    for(const r of e.refs) refs.append(sessionLink(r));
    item.append(refs);
  }
  if(e.kind === "ask") item.append(askAnswer(op, e));
  return item;
}
function feedBox(op) {
  const box = node("div", "", "op-feed");
  box.setAttribute("aria-label", `${op.name} 대화`);
  const feed = detail?.id === op.id ? detail.feed || [] : [];
  if(detail?.error) box.append(node("p", detail.error, "op-error"));
  const shown = expanded ? feed : feed.slice(-FEED_SHOWN);
  if(feed.length > shown.length) {
    const more = node("button", `이전 대화 ${feed.length - shown.length}건 펼치기`, "observer-more");
    more.type = "button";
    more.onclick = () => { expanded = true; render(); };
    box.append(more);
  }
  if(!feed.length && !detail?.error) box.append(node("p", "아직 대화가 없습니다. 아래 입력란에 질문을 입력하십시오.", "hint"));
  for(const e of shown) box.append(entryItem(op, e));
  return box;
}
/* -- the section ------------------------------------------------------- */
/* The session line: which operator this conversation is with, the session
   it runs in and that session's state, and the way to that session's page.
   A stopped session keeps its link: its page shows an ended session too. */
function sessionBar(op) {
  if(!bar) return;
  bar.hidden = !op;
  if(!op) { bar.replaceChildren(); return; }
  const who = node("span", "", "op-bar-who");
  who.append(node("span", "", `op-dot${running(op) ? " on" : ""}`), node("strong", op.name));
  const parts = [who];
  if(op.operator) {
    const state = running(op) ? `실행 중${op.operator.status ? ` (${op.operator.status})` : ""}` : "정지됨";
    parts.push(node("span", `${op.operator.name} · ${state}`, "op-bar-session"));
    const link = node("a", "세션 열기", "op-bar-open");
    link.href = "#/s/" + encodeURIComponent(op.operator.name);
    link.title = `세션 ${op.operator.name}의 화면으로 이동합니다`;
    parts.push(link);
  } else parts.push(node("span", "세션 없음", "op-bar-session"));
  bar.replaceChildren(...parts);
}
function head(op) {
  const box = node("div", "", "op-head");
  const title = node("h3", op.name + " ");
  title.append(node("span", running(op) ? "실행 중" : "정지됨", `state ${running(op) ? "working" : "idle"}`));
  box.append(title, node("small", `범위: ${scopeText(op)} · ${stateText(op)}`, "op-scope"));
  if(!opts.manage) {
    const page = node("a", "Observer에서 관리", "op-manage");page.href = "#/observer";
    page.title = "시작·정지, 감시, 범위 설정은 Observer 페이지의 Operator 영역에 있습니다";
    box.append(" ", page);
    return box;
  }
  const row = node("div", "", "op-actions");
  const start = node("button", running(op) ? "정지" : "시작");start.type = "button";start.disabled = busy;
  start.title = running(op) ? "operator 세션을 정지합니다" : op.profile ? `${op.profile}로 operator 세션을 시작합니다` : "시작하려면 설정에서 프로필을 고르십시오";
  start.onclick = () => {
    if(!running(op) && !op.profile) { openForm(op, "시작하려면 프로필을 고르십시오."); return; }
    act(() => call(opPath(op.id, running(op) ? "/stop" : "/start"), "POST", {}));
  };
  const watch = document.createElement("input");
  watch.type = "checkbox"; watch.id = "observer-op-watch"; watch.checked = !!op.watch; watch.disabled = busy;
  watch.onchange = () => act(() => call(opPath(op.id), "PATCH", {watch: watch.checked}));
  const watchLabel = node("label", "", "op-watch");watchLabel.append(watch, " 감시");
  watchLabel.title = "켜면 operator가 범위 안 세션을 주기적으로 관찰합니다. 꺼도 질문에는 답합니다.";
  const edit = node("button", "설정");edit.type = "button";edit.onclick = () => openForm(op);
  const remove = node("button", "삭제");remove.type = "button";remove.disabled = busy || running(op);
  remove.title = running(op) ? "실행 중인 operator는 정지한 뒤 삭제할 수 있습니다" : "이 operator와 대화 기록을 삭제합니다";
  remove.onclick = () => { if(confirm(`operator 「${op.name}」을 삭제합니까?`)) act(async () => { await call(opPath(op.id), "DELETE"); choose(""); }); };
  row.append(start, watchLabel, edit, remove);
  box.append(row);
  return box;
}
function render() {
  if(!root) return;
  const tabs = node("div", "", "op-tabs");
  tabs.setAttribute("role", "tablist");tabs.setAttribute("aria-label", "Operator 목록");
  for(const op of list) {
    const tab = node("button", "", "op-tab");tab.type = "button";
    tab.setAttribute("role", "tab");tab.setAttribute("aria-selected", String(op.id === selected));
    tab.dataset.operator = op.id;
    tab.append(node("span", "", `op-dot${running(op) ? " on" : ""}`), node("span", op.name));
    if(op.unseen > 0 || op.pending > 0) {
      const badge = node("span", String(op.unseen || op.pending), `op-badge${op.unseen ? " unseen" : ""}`);
      badge.title = `답변을 기다리는 질문 ${op.pending}개${op.unseen ? ` · 새 질문 ${op.unseen}개` : ""}`;
      tab.append(badge);
    }
    tab.onclick = () => { choose(op.id); form = null; render(); onChange(); refresh(); };
    tabs.append(tab);
  }
  if(opts.manage) {
    const add = node("button", "+ 새 operator", "op-tab op-new");add.type = "button";
    add.title = "범위(프로젝트 또는 전체 프로젝트)를 정해 operator를 만듭니다";
    add.onclick = () => openForm(null);
    tabs.append(add);
  }
  // Title and tabs share one row, so a page with no operator spends one line
  // on this section and the session board keeps its height.
  const bar = node("div", "", "op-bar");
  bar.append(node("h2", "Operator", "op-title"), tabs);
  const parts = [bar];
  if(listError) parts.push(node("p", `operator 목록을 읽지 못했습니다: ${listError}`, "op-error"));
  if(noticeText) { parts.push(node("p", noticeText, "op-error")); noticeText = ""; }
  if(form) parts.push(formBox());
  const op = current();
  if(op) parts.push(head(op), feedBox(op));
  else if(!opts.manage && loaded && !list.length && !listError) {
    const empty = node("p", "operator가 없습니다. ", "hint");
    const page = node("a", "Observer에서 만들기");page.href = "#/observer";
    empty.append(page);parts.push(empty);
  }
  // A rebuild must not move the scroller it sits in. One that was at the end
  // stays at the end, so a new entry in the view's own conversation shows.
  const page = opts.scroller?.(), top = page?.scrollTop;
  const atEnd = !!page && page.scrollHeight - page.scrollTop - page.clientHeight < 24;
  area.replaceChildren(...parts);
  sessionBar(op);
  if(page) page.scrollTop = opts.composer && atEnd ? page.scrollHeight : top;
  if(input) input.send.disabled = busy || !op;
}
/* -- what observer.js uses --------------------------------------------- */
const TARGET = "operator:";
let input = null;
function mount(element, changed) {
  root = element; onChange = changed || onChange;
  area = root;
  if(opts.composer) {
    bar = node("div", "", "op-sessionbar"); bar.hidden = true;
    area = node("div", "", "op-area");
    input = composerBox();
    root.replaceChildren(bar, area, input.box);
  }
  render();
}
/* The view's own input row: Enter sends to the selected operator, Shift+Enter
   breaks the line. A draft is kept per operator while the page is loaded. */
const drafts = new Map();
function composerBox() {
  const box = node("form", "", "op-composer");
  const text = document.createElement("textarea");
  text.rows = 2; text.placeholder = "operator에게 메시지 (Enter 전송, Shift+Enter 줄바꿈)";
  text.setAttribute("aria-label", "operator에게 보낼 메시지");
  const send = node("button", "보내기");send.type = "submit";
  const status = node("small", "", "op-composer-status");status.setAttribute("aria-live", "polite");
  const row = node("div", "", "op-composer-row");row.append(text, send);
  box.append(row, status);
  text.oninput = () => drafts.set(selected, text.value);
  text.addEventListener("keydown", event => {
    if(event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); box.requestSubmit(); }
  });
  box.onsubmit = async event => {
    event.preventDefault();
    const id = selected, body = text.value;
    if(!id || !body.trim() || send.disabled) return;
    send.disabled = true;
    try {
      status.textContent = await sendTo(id, body);
      drafts.delete(id);
      if(selected === id) text.value = "";
    } catch(err) { status.textContent = `전송하지 못했습니다: ${err.message}`; }
    finally { send.disabled = busy || !current(); text.focus(); }
  };
  return {box, text, send, status};
}
/* The selection changed under the input row: show that operator's draft. */
function syncDraft() {
  if(!input) return;
  input.text.value = drafts.get(selected) || "";
  input.status.textContent = "";
}
/* The composer's operator destinations, selected one first. */
function targets() {
  const own = current();
  return [...(own ? [own] : []), ...list.filter(o => o !== own)].map(o => ({value: TARGET + o.id, label: `operator: ${o.name}`}));
}
const isTarget = value => String(value || "").startsWith(TARGET);
const defaultTarget = () => current() ? TARGET + current().id : "";
const send = (value, text) => sendTo(value.slice(TARGET.length), text);
async function sendTo(id, text) {
  const op = list.find(o => o.id === id);
  const result = await call(opPath(id, "/message"), "POST", {text});
  if(id !== selected) choose(id);
  expanded = false;
  refresh();
  const name = op?.name || id;
  return result.nudge === "no-operator" ? `operator ${name}: 메시지 저장됨 · 세션이 정지되어 있어 시작하면 읽습니다`
    : result.nudge === "pending" ? `operator ${name}: 메시지 저장됨 · 세션 알림 대기`
    : `operator ${name}: 메시지 전달됨`;
}
/* The card's 「operator에게 묻기」: which operator, or why none. A peek (the
   card being drawn) only answers; the press also selects that operator. */
function askAbout(name, peek = false) {
  const op = ownerOf(name);
  if(!op) return {target: "", reason: list.length ? `${name}(${projectOf(name)})을 범위에 둔 operator가 없습니다` : "operator가 없습니다"};
  if(!peek && op.id !== selected) { choose(op.id); render(); refresh(); }
  return {target: TARGET + op.id, name: op.name, reason: ""};
}
/* Open on a session: the operator whose scope holds it, unless the selected
   one already does. Nothing changes when no operator covers it. */
async function focusSession(name) {
  if(name && !listed(name) && !sessionRows.has(name)) {
    try { sessionRows.set(name, await call(`api/sessions/${encodeURIComponent(name)}`)); } catch { /* read as the default project */ }
  }
  await refresh();
  const op = name ? ownerOf(name) : null;
  if(op && op.id !== selected) { choose(op.id); await refresh(); }
}
/* What waits across every operator, for a badge: unanswered asks, and of
   those the ones not shown yet. */
const summary = () => list.reduce((sum, o) => ({pending: sum.pending + (o.pending || 0), unseen: sum.unseen + (o.unseen || 0)}), {pending: 0, unseen: 0});
const focusInput = () => input?.text.focus();
return {mount, refresh, targets, isTarget, defaultTarget, send, askAbout, focusSession, summary, focusInput,
        selected: () => selected, _covers: covers};
}
