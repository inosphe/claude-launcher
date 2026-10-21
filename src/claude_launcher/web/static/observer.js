/* Observer uses the main app router, authentication and viewport. */
globalThis.ObserverPage = (() => {
"use strict";
const $ = id => document.getElementById("observer-" + id);
let snapshot = {sessions: [], enabled: false}, pending = false, refreshTask = null, lastSnapshot = "";
const drafts = new Map(), answerDrafts = new Map();
// A refresh is one request per session, so its state is per session too: the
// board is rebuilt on every poll, and a note kept in the card's DOM would go
// with the card it was typed into.
const refreshing = new Set(), refreshNotes = new Map();
// The v1 runs are the app's own rail cache (refreshCflow, on the dashboard
// poll): the page used to fetch the same /api/cflow?view=rail again beside it.
let runs = [], gateCache = new Map();
const appRuns = () => typeof cflowCache !== "undefined" && Array.isArray(cflowCache) ? cflowCache : [];
// v2 runs, QA and threads come from the app's rail caches the same way: one
// row per session, refreshed by the dashboard poll whatever page is shown.
const appRow = (cache, name) => cache && typeof cache === "object" ? cache[name] || null : null;
const run2Of = s => appRow(typeof cflow2RailCache !== "undefined" ? cflow2RailCache : null, s.name);
const qaOf = s => appRow(typeof qaRailCache !== "undefined" ? qaRailCache : null, s.name);
const threadsOf = s => appRow(typeof threadsRailCache !== "undefined" ? threadsRailCache : null, s.name);
let draftTarget = "";
// Set once the reader picks a destination in the select: from then on an empty
// one (「세션 선택」) is their choice, and the operator default stays out of it.
let targetPicked = false;
// The composer starts folded at every width and stays where the reader left
// it for as long as the page is loaded; a reload comes back folded.
let composerFolded = true;
let layout = "board", gridLimit = 5;
/* The timeline draws its newest posts and grows on request: drawn whole it
   was one card per event of every visible session (3,217 for the 57 running
   sessions on 2026-10-15), rebuilt on each redraw. */
const TIMELINE_STEP = 200;
let timelineLimit = TIMELINE_STEP;
try {
  const savedLayout = localStorage.getItem("claunch-observer-layout");
  if(["board","grid","timeline"].includes(savedLayout)) layout = savedLayout;
  const saved = Number(localStorage.getItem("claunch-observer-grid-limit"));
  if(Number.isInteger(saved) && saved >= 1 && saved <= 10) gridLimit = saved;
} catch { /* Storage may be unavailable; controls still work for this visit. */ }
function saveView() {
  try {
    localStorage.setItem("claunch-observer-layout",layout);
    localStorage.setItem("claunch-observer-grid-limit",String(gridLimit));
  } catch { /* Keep the in-memory preference. */ }
}
/* The Operator dock (claunch-iw5i8.8): the operators' column to the right of
   the page, opened and closed from the header (or the phone's top bar) and
   kept per browser. It starts closed. Its height is the page viewport's, set
   here because the dock is held on screen inside #observer-view, the
   element that scrolls. */
const DOCK_KEY="claunch-observer-operator-dock";
let dockOpen=false;
try { dockOpen=localStorage.getItem(DOCK_KEY)==="open"; } catch { /* closed for this visit */ }
function dockState() {
  $("view").classList.toggle("dock-open",dockOpen);
  $("dock").hidden=!dockOpen;
  for(const button of [$("operator-toggle"),$("mobile-operator")]) {
    if(!button)continue;
    button.setAttribute("aria-pressed",String(dockOpen));
    button.title=dockOpen?"Operator 대화창을 닫습니다":"Operator 대화창을 오른쪽 열에 엽니다";
  }
  $("operator-toggle").textContent=dockOpen?"Operator ◂":"Operator ▸";
}
function setDock(open) {
  dockOpen=open;
  try { localStorage.setItem(DOCK_KEY,open?"open":"closed"); } catch { /* keep it for this visit */ }
  dockState();
  if(open)operators()?.refresh();
}
if(typeof ResizeObserver==="function")
  new ResizeObserver(()=>$("view").style.setProperty("--observer-viewport",`${$("view").clientHeight}px`)).observe($("view"));
/* The operators section (observer_operator.js). Read through this so a page
   or harness that loads observer.js without it draws no operator controls. */
const operators = () => globalThis.ObserverOperators || null;
const node = (tag, text, cls) => { const e = document.createElement(tag); e.textContent = text; if(cls)e.className=cls; return e; };
async function request(path, body) {
  const options = body === undefined ? {} : {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)};
  const response = await api(path, options);
  const data = await response.json();
  if(!response.ok) throw Error(data.error || `HTTP ${response.status}`);
  return data;
}
function options(select, values, placeholder) {
  const old=select.value;
  select.replaceChildren();
  if(placeholder) { const o=node("option",placeholder);o.value="";select.append(o); }
  for(const value of values) { const o=node("option",value);o.value=value;select.append(o); }
  if(values.includes(old)) select.value=old;
}
function chooseTarget(name) {
  drafts.set(draftTarget,$("prompt").value);
  $("target").value=name;
  draftTarget=name;
  $("prompt").value=drafts.get(name)||"";
  controls();
}
function controls() {
  autosize();
  const target=$("target").value, s=snapshot.sessions.find(s=>s.name===target);
  // An operator reads its messages when it next runs, so a stopped one takes
  // them too; there is nothing to interrupt in it from here.
  const operator=!!operators()?.isTarget(target);
  $("send").disabled=pending || !(operator||s?.running) || !$("prompt").value.trim();
  $("interrupt").disabled=pending || operator || !s?.running;
  $("target").disabled=pending;
}
function sessionRuns(s) {
  return runs.filter(r => r.scope===s.name || (r.sessions||[]).includes(s.name));
}
function activity(s) {
  const own=sessionRuns(s), pending=own.some(r=>String(r.status).startsWith("waiting"));
  const question=(s.events||[]).some(e=>e.question&&!e.answer)||qaWaiting(s).length>0||!!run2Of(s)?.gated;
  const stamp=s.last_activity_at, age=stamp?Math.max(0,Date.now()-Date.parse(stamp)):null;
  const duration=Number.isFinite(age)?`${Math.floor(age/60000)}분 전 마지막 활동`:"활동 시각 미확인";
  if(!s.running)return {label:"종료됨",kind:"ended",duration};
  if(s.status==="busy" || s.status==="working")return {label:"동작 중",kind:"working",duration};
  if(pending||question||s.state==="blocked"||s.state==="waiting")
    return {label:age>=30*60000?"장기 대기":"응답·작업 대기",kind:"waiting",duration};
  if(s.status==="idle" && (own.some(r=>r.status==="done")||s.state==="done"))
    return {label:"완료 후 유휴",kind:"done",duration};
  return {label:s.status==="idle"?"유휴":"상태 미확인",kind:"idle",duration};
}
function addGate(card, s, r) {
  const section=node("section","","observer-gate");
  section.append(node("h3",`cflow · ${r.title||r.step_id||r.step||r.workflow}`));
  const link=node("a","워크플로 열기");
  link.href="#/wf/"+encodeURIComponent(`${r.scope||s.name}|${r.cwd||s.cwd}`);
  section.append(link,node("p","승인·선택 항목을 불러오는 중…"));
  card.append(section);
  const key=JSON.stringify([r.cwd,r.scope,r.run,r.step_id,r.status,r.ask,r.goto_request]);
  const get=gateCache.get(key)||request(`api/cflow/run?cwd=${encodeURIComponent(r.cwd||s.cwd)}&scope=${encodeURIComponent(r.scope||s.name)}`);
  gateCache.set(key,get);
  get.then(data=>{
    if(!section.isConnected)return;
    section.lastChild.remove();
    section.append(wfActions(data,{archive:false,reminder:false,host:"observer",after:async()=>{
      // The runs are the app's cache: read them again now rather than at the
      // dashboard's next poll, so the answered gate goes away with the press.
      gateCache.clear();lastSnapshot="";
      if(typeof refreshCflow==="function")await refreshCflow().catch(()=>{});
      refresh();
    }}));
  }).catch(err=>{gateCache.delete(key);section.lastChild.textContent=err.message;});
}
function addDirect(item, s, e) {
  for(const attachment of e.attachments||[]) {
    const link=node("a", "", "observer-image");
    link.href=url(`api/observer/${encodeURIComponent(s.name)}/reports/${encodeURIComponent(e.id)}/images/${attachment.id}`);
    link.target="_blank";link.rel="noopener";
    const img=document.createElement("img");img.src=link.href;img.alt="에이전트 보고 스크린샷";img.loading="lazy";
    link.append(img);item.append(link);
  }
  if(!e.question)return;
  if(e.answer) {
    item.append(node("p",`사용자 답변: ${e.answer.text}`));
    item.append(node("small",e.delivery==="sent"?"세션 전달됨":e.delivery==="pending"?"답변 저장됨 · 세션 전달 대기":"답변 저장됨 · 전달 결과 미확인 (MCP에서 조회 가능)"));
    if(e.delivery==="pending") {
      const retry=node("button","세션 전달 재시도");
      retry.onclick=async()=>{
        retry.disabled=true;
        try {await request(`api/observer/${encodeURIComponent(s.name)}/reports/${e.id}/answer`,{text:e.answer.text});lastSnapshot="";await refresh();}
        catch(err) {showError(err.message);} finally {retry.disabled=false;}
      };
      item.append(retry);
    }
    return;
  }
  const form=node("form","","observer-answer"), field=document.createElement("textarea");
  field.placeholder="선택지를 고르거나 답변을 입력하십시오";field.setAttribute("aria-label","사용자 답변");field.value=answerDrafts.get(e.id)||"";
  field.oninput=()=>answerDrafts.set(e.id,field.value);
  for(const choice of e.choices||[]) {
    const button=node("button",choice);button.type="button";
    button.onclick=()=>{field.value=choice;answerDrafts.set(e.id,choice);field.focus();};form.append(button);
  }
  const submit=node("button","답변 전송"), status=node("p","");submit.type="submit";form.append(field,submit,status);
  form.onsubmit=async event=>{
    event.preventDefault();if(submit.disabled||!field.value.trim())return;
    submit.disabled=true;field.disabled=true;
    try {
      const result=await request(`api/observer/${encodeURIComponent(s.name)}/reports/${e.id}/answer`,{text:field.value});
      answerDrafts.delete(e.id);field.blur();
      status.textContent=result.delivery==="sent"?"답변 전달됨":"답변 저장됨 · 세션 전달 대기";
      lastSnapshot="";await refresh();
    } catch(err) {status.textContent=err.message;} finally {submit.disabled=false;field.disabled=false;}
  };
  item.append(form);
}
const mobileView = matchMedia("(max-width:820px)");
let boardOrder = [];
const timestamp = value => Number.isFinite(Date.parse(value)) ? Date.parse(value) : 0;
const latestActivity = s => Math.max(timestamp(s.last_activity_at),timestamp(s.generated_at),...(s.events||[]).map(e=>timestamp(e.at)));
function ageText(value) {
  const at=timestamp(value);
  if(!at)return null;
  const age=Math.max(0,Date.now()-at);
  if(age<60000)return "방금";
  if(age<3600000)return `${Math.floor(age/60000)}분 전`;
  if(age<86400000)return `${Math.floor(age/3600000)}시간 전`;
  return `${Math.floor(age/86400000)}일 전`;
}
/* How stale a card is, in two parts because they answer different questions:
   when the observer last rewrote this session's summary, and when the session
   itself last did anything. Both absent (a row written before the meter) is
   answered by saying nothing rather than by a dash. */
function updatedText(s) {
  const parts=[];
  const generated=ageText(s.generated_at), active=ageText(s.last_activity_at);
  if(generated)parts.push(`업데이트 ${generated}`);
  if(active)parts.push(`활동 ${active}`);
  return parts.length?parts.join(" · "):null;
}
function visibleSessions() {
  const scope=$("scope").value, selected=$("selection").value;
  return snapshot.sessions.filter(s=>($("ended").checked||s.running)
    && ($("activity").value==="all"||activity(s).kind===$("activity").value)
    && (scope==="global"||(scope==="session"?s.name===selected:(s.meshes||[]).includes(selected)))
    && (!$("actions-only").checked||actionCount(s)>0));
}
/* What the reader owes one session: unacknowledged events, v1 runs waiting
   on a person, a v2 run gated on a person, and QA questions waiting. */
function actionCount(s) {
  return (s.events||[]).filter(e=>e.needs_action&&!e.acknowledged).length
    +sessionRuns(s).filter(sessCflowGated).length
    +(run2Of(s)?.gated?1:0)
    +qaWaiting(s).length;
}
/* Sessions first, as before, then the operators in their own group. With no
   target picked yet the selected operator is the default: the composer is
   where the reader talks to it. An empty target the reader picked
   (「세션 선택」) stays empty; a target that went away falls back to it. */
function syncTargets(visible) {
  const names=visible.filter(s=>s.running).map(s=>s.name);
  const bots=operators()?.targets()||[];
  drafts.set(draftTarget,$("prompt").value);
  options($("target"),names,"세션 선택");
  if(bots.length) {
    const group=document.createElement("optgroup");group.label="Operator";
    for(const b of bots){const o=node("option",b.label);o.value=b.value;group.append(o);}
    $("target").append(group);
    if(bots.some(b=>b.value===draftTarget))$("target").value=draftTarget;
  }
  const valid=[...names,...bots.map(b=>b.value)];
  if(draftTarget&&!valid.includes(draftTarget)) chooseTarget(operators()?.defaultTarget()||"");
  else if(!draftTarget&&bots.length&&!targetPicked) chooseTarget(operators().defaultTarget());
}
function sessionHeader(s, cls="") {
  const card=node("article","",`observer-card ${cls}`);card.dataset.session=s.name;
  const title=node("h2",s.name+" "), state=activity(s);
  title.append(node("span",state.label,`state ${state.kind}`));card.append(title);
  const updated=updatedText(s);
  if(updated)card.append(node("small",updated,"observer-updated"));
  const links=node("nav","","observer-links");
  for(const [label,href] of [["관찰 결과","#/observer/session/"],["터미널","#/s/"],["트랜스크립트","#/log/"]]) {
    const link=node("a",label);link.href=href+encodeURIComponent(s.name);links.append(link);
  }
  card.append(links);
  return card;
}
function composerState() {
  const toggle=$("composer-toggle");
  $("composer").classList.toggle("folded",composerFolded);
  toggle.setAttribute("aria-expanded",String(!composerFolded));
  toggle.textContent=`입력 대상 세션·operator ${composerFolded?"▸":"▾"}`;
}
/* Destinations and actions share a compact two-row toolbar. */
function cardActions(card,s) {
  const links=card.querySelector(".observer-links");
  const input=node("button","이 세션에 입력","observer-action");
  // Picking a session is a request to type at it, so the composer opens first
  // when it is folded — a focus() on a hidden textarea would go nowhere.
  input.disabled=!s.running;
  input.onclick=()=>{chooseTarget(s.name);composerFolded=false;composerState();$("prompt").focus();};
  const update=node("button","지금 갱신","observer-action");
  update.disabled=refreshing.has(s.name);
  update.title=`${s.name}을 지금 한 번 관찰합니다. 관찰이 꺼져 있어도 이 한 번은 수행됩니다.`;
  update.onclick=()=>oneShot(s.name);
  // The same flag the rail's glyph writes, drawn here so the reader can see
  // which sessions the pinned-only scope covers without leaving the page.
  // All four surfaces read it off the session record, so none of them owns
  // the state. The card uses the same eye glyph as the rail and marks the
  // selected state with a contrasting fill, border and aria-pressed.
  // The sentence comes from observePinTitle, which is also where the tab, the
  // rail row and the detail panel's Flags box get theirs. A title written here
  // would be a fourth wording of one flag, and a fixed one would go on saying
  // 넣습니다 after the flag is already on — telling the reader the opposite
  // of what the press would do. This file reaches an app.js global the way
  // request() already reaches api(): both are classic scripts in one document,
  // and cardActions runs at render time, long after each has been parsed.
  const pin=node("button","👁 관찰 고정","observer-action observer-pin");
  pin.type="button";
  pin.classList.toggle("on",!!s.observe_pin);
  pin.setAttribute("aria-pressed",String(!!s.observe_pin));
  pin.title=observePinTitle(s.name,!!s.observe_pin);
  pin.onclick=()=>setObservePin(s.name,!s.observe_pin);
  links.append(pin,input,update);
  // Asking an operator about this session: drawn only while there are
  // operators, and pressable only when one has the session's project in scope.
  if(operators()?.targets().length) {
    const ask=node("button","operator에게 묻기","observer-action observer-ask-operator");
    ask.type="button";
    const owner=operators().askAbout(s.name,true);
    ask.disabled=!owner.target;
    ask.title=owner.target?`operator ${owner.name}에게 ${s.name}에 대해 묻습니다`:owner.reason;
    ask.onclick=()=>{
      const chosen=operators().askAbout(s.name);
      if(!chosen.target)return;
      // The answer shows in the dock, so a closed one opens.
      if(!dockOpen)setDock(true);
      syncTargets(visibleSessions());chooseTarget(chosen.target);
      if(!$("prompt").value.trim())$("prompt").value=`${s.name}에 대해: `;
      composerFolded=false;composerState();controls();$("prompt").focus();
    };
    links.append(ask);
  }
  const note=refreshNotes.get(s.name);
  if(note)links.append(node("small",note,"observer-action-note"));
}
/* What else waits on the reader in a session: its QA questions and the
   thread replies this browser has not shown yet. Both are the app's rail
   caches (/api/qa/rail, /api/threads/rail), so the card says what the
   session header and the notice cards say, from the same rows. A press opens
   the session with that pane, the way the notice cards do. */
function openTalk(name, pane, thread) {
  if(pane==="threads"&&typeof threadNoticeOpen==="function") {threadNoticeOpen(name,thread);return;}
  try {
    const lay=sessLayoutFor(name);
    if(!(lay.split&&talkShows(lay.pane,pane)))
      setSessLayout(name,{split:true,pane:lay.split&&lay.pane==="threads"?"both":pane});
  } catch { /* storage off: the session still opens */ }
  location.hash="#/s/"+encodeURIComponent(name);
}
const qaWaiting = s => (qaOf(s)?.waiting)||[];
/* Replies newer than the mark this browser keeps per session (the one the
   session header's 🗪 reads). A session never marked has no news yet: its
   history is not announced, as on the header. */
function newReplies(s) {
  const row=threadsOf(s);
  if(!row||!row.agent_seq||typeof loadThreadsSeen!=="function")return [];
  const seen=loadThreadsSeen()[s.name];
  if(seen===undefined)return [];
  return (row.replies||[]).filter(r=>Number(r.seq)>Number(seen));
}
const REPLIES_SHOWN = 3;
function noticesBlock(s) {
  const questions=qaWaiting(s), replies=newReplies(s);
  if(!questions.length&&!replies.length)return null;
  const box=node("div","","observer-notices");
  for(const q of questions) {
    const b=node("button","","observer-notice notice-qa");b.type="button";
    b.append(node("span",q.status==="stale"?"QA 재질문":"QA 질문","obs-notice-kind"),node("span",q.title||q.id,"obs-notice-text"));
    b.title=`${s.name}의 QA 창에서 답합니다`;
    b.onclick=()=>openTalk(s.name,"qa");
    box.append(b);
  }
  for(const r of replies.slice(-REPLIES_SHOWN).reverse()) {
    const b=node("button","","observer-notice notice-thread");b.type="button";
    b.append(node("span","쓰레드 답장","obs-notice-kind"),node("span",`「${r.title}」 ${r.excerpt||""}`,"obs-notice-text"));
    b.title=`${s.name}의 쓰레드 창을 엽니다`;
    b.onclick=()=>openTalk(s.name,"threads",r.thread);
    box.append(b);
  }
  if(replies.length>REPLIES_SHOWN)box.append(node("small",`외 쓰레드 답장 ${replies.length-REPLIES_SHOWN}개`,"observer-notice-more"));
  return box;
}
/* The session's workflow run, where it stands now: the v2 run from the v2
   rail cache, and a v1 run that is not waiting (a waiting one is drawn as its
   gate with the answer form) and not finished. Each line opens the run page. */
function runBlock(s) {
  const lines=[];
  const r2=run2Of(s);
  if(r2) {
    const mark=typeof cflow2RailMarkState==="function"?cflow2RailMarkState(r2):"running";
    const label=typeof cflow2RailLabel==="function"?cflow2RailLabel(r2):String(r2.status||"");
    const a=node("a","",`observer-run run-${mark}`);
    a.href="#/wf/"+encodeURIComponent(`${r2.scope}|${r2.cwd}`);
    a.append(node("span","cflow2","obs-run-kind"),node("span",r2.workflow||"","obs-run-wf"),node("span",label,"obs-run-state"));
    lines.push(a);
  }
  for(const r of sessionRuns(s)) {
    const status=String(r.status||"");
    if(status.startsWith("waiting")||status==="done")continue;
    const a=node("a","",`observer-run run-${status==="running"?"running":"error"}`);
    a.href="#/wf/"+encodeURIComponent(`${r.scope||s.name}|${r.cwd||s.cwd}`);
    a.append(node("span","cflow","obs-run-kind"),node("span",r.workflow||"","obs-run-wf"),
      node("span",[status,r.title||r.step_id||""].filter(Boolean).join(" · "),"obs-run-state"));
    lines.push(a);
  }
  if(!lines.length)return null;
  const box=node("div","","observer-runs");
  box.append(...lines);
  return box;
}
/* Run state and notices once per session: a board column carries them at
   its top, and the grid and timeline on the session's newest post only. */
function sessionStatus(s) {
  return [runBlock(s),noticesBlock(s)].filter(Boolean);
}
/* One observation pass for one session, now. The button is a request, not a
   mode: the daemon runs one pass and answers what it did, and it is served
   while observation is off as well — the switch governs the loop's standing
   bill over the whole fleet, and this press buys one call for the session the
   reader named. A pass with nothing new to read spends no API call at all, and saying
   that is half of what this reports — the reader pressed a button that costs
   money exactly when there is something new. */
/* Which sessions the 「고정만 관찰」 scope covers. The flag is a session
   definition field, so this writes it through the observer's route and the
   rail's box reads the same value off its own poll — the page keeps no copy,
   and the reply is what redraws, so a refusal does not read as applied. */
async function setObservePin(name,on) {
  try {
    await request(`api/observer/${encodeURIComponent(name)}/pin`,{pinned:on});
  } catch(err) {
    showError(err.message);
  } finally {
    lastSnapshot="";await refresh();
  }
}
async function oneShot(name) {
  if(refreshing.has(name))return;
  refreshing.add(name);refreshNotes.set(name,"갱신 중…");redraw();
  try {
    const result=await request(`api/observer/${encodeURIComponent(name)}/refresh`,{});
    refreshNotes.set(name,result.called?`갱신됨 · 새 항목 ${result.events}개`:"갱신됨 · 새 기록 없음");
  } catch(err) {
    refreshNotes.set(name,err.message);
  } finally {
    refreshing.delete(name);lastSnapshot="";await refresh();
  }
}
/* Event text as one compact line with its identifiers drawn as chips.
   It is done here, at render time, and not by the model or the daemon: stored
   events keep the text they were written with, so a format decided in the
   page reaches every event already on the timeline as well as the next one.
   Only the kinds of identifier these reports are made of are recognised —
   beads issues, cflow run ids, sessions, commit hashes and test counts — and
   anything else stays literal text. A backtick span is a code span unless the
   whole span is one of those identifiers. */
const CHIP_TITLES = {beads:"beads 이슈",run:"cflow 런",session:"세션",commit:"커밋",pass:"테스트 통과",fail:"테스트 실패",count:"테스트 수치"};
const CHIP_PATTERN = [
  "(?<beads>\\bclaunch-[a-z0-9]{3,8}(?:\\.\\d+)*(?![\\w-]))",
  "(?<run>\\brun-[0-9a-f]{8}\\b)",
  "(?<session>@?\\bs\\d{3,4}\\b(?![\\w-]))",
  "(?<tests>\\b\\d+\\s*(?:passed|failed|skipped|errors?|error|xfailed|xpassed)\\b)",
  "(?<commit>\\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\\d)[0-9a-f]{7,40}\\b)",
  // An all-digit hash is only a hash where the text says so.
  "(?<=(?:tip|커밋|commit|머지|merge|HEAD|@)\\s?)(?<digits>\\b\\d{7,12}\\b)",
].join("|");
function chipKind(groups, text) {
  if(groups.tests!==undefined) {
    const n=Number(text.match(/\d+/)[0]);
    return /fail|error/.test(text)?(n?"fail":"count"):/passed/.test(text)?"pass":"count";
  }
  if(groups.digits!==undefined) return "commit";
  return Object.keys(groups).find(k=>groups[k]!==undefined);
}
/* A commit chip in a session's text links to the commit's page in that
   session's repository (commit-page.js, claunch-tb145) where the dashboard
   has that page; observer.html alone does not, and keeps a plain chip. */
function chip(kind,text,session) {
  const c=node("span",kind==="session"&&!text.startsWith("@")?"@"+text:text,`obs-chip obs-chip-${kind}`);
  c.title=CHIP_TITLES[kind]||kind;
  if(kind==="commit"&&session&&globalThis.CommitPage) {
    globalThis.CommitPage.link(c,text,{session});
    c.title+=" · 클릭: 커밋과 diff 보기";
  }
  return c;
}
function chipsInto(parent,text,session) {
  let last=0;
  for(const m of text.matchAll(new RegExp(CHIP_PATTERN,"g"))) {
    if(!m[0])continue;
    if(m.index>last)parent.append(node("span",text.slice(last,m.index)));
    parent.append(chip(chipKind(m.groups,m[0]),m[0],session));
    last=m.index+m[0].length;
  }
  if(last<text.length)parent.append(node("span",text.slice(last)));
}
function richLine(text,cls="obs-line",session="") {
  const line=node("div","",cls), source=String(text??"");
  let last=0;
  for(const m of source.matchAll(/`([^`\n]+)`/g)) {
    if(m.index>last)chipsInto(line,source.slice(last,m.index),session);
    const whole=m[1].match(new RegExp(`^(?:${CHIP_PATTERN})$`));
    line.append(whole?chip(chipKind(whole.groups,m[1]),m[1],session):node("code",m[1],"obs-code"));
    last=m.index+m[0].length;
  }
  if(last<source.length)chipsInto(line,source.slice(last),session);
  return line;
}
/* Runs of the same routine daemon event — a restore after every daemon
   restart is the common one: s697 carried 20 of them among 31 events — are
   drawn as one entry with a count and a time span. Only events next to each
   other in one session's own order are merged, so nothing is moved across
   an event that happened in between. */
const ROUTINE = new Set(["resume"]);
function collapseRoutine(events) {
  const out=[];
  for(const e of events) {
    const prev=out[out.length-1];
    if(prev&&e.origin==="daemon"&&ROUTINE.has(e.kind)&&prev.origin==="daemon"&&prev.kind===e.kind&&prev.text===e.text) {
      const first=prev.run?prev.run.first:prev.at;
      out[out.length-1]={...e,id:prev.id,run:{count:(prev.run?prev.run.count:1)+1,first,last:e.at}};
      continue;
    }
    out.push(e);
  }
  return out;
}
const clock = value => {
  const d=new Date(value);
  if(!Number.isFinite(d.getTime()))return "";
  const pad=n=>String(n).padStart(2,"0");
  return `${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
};
const KIND_LABELS = {cflow:"cflow",commit:"commit",merge:"merge",test:"test",action:"요청",result:"결과",
  briefing:"브리핑",checks:"체크",create:"생성",resume:"복원",exit:"종료",respawn:"재실행",borrow:"인증",worktree:"워크트리"};
/* briefing/checks records (search_records.capture, kind "briefing"/"checks")
   carry their payload as e.text = JSON.stringify(payload,null,2) already, so
   the structure is on hand without the lazy /events/{id} fetch below. */
function recordPayload(e) {
  try { return JSON.parse(e.text); } catch { return null; }
}
/* Same box the session rail's briefing card uses (style.css .sess-brief*,
   unscoped there precisely so other panels can reuse it) so one snapshot
   reads the same way whether it is seen live on the rail or archived here. */
function briefingSnapshot(payload) {
  const box=node("div","","sess-brief");
  if(!payload||typeof payload!=="object") {
    box.append(node("div","브리핑 데이터를 표시할 수 없습니다.","sess-brief-note"));
    return box;
  }
  if(payload.raw) {
    // The model answered outside the agreed shape; its words are still the
    // best available summary, shown as they came (matches app.js's fallback).
    box.append(node("pre",payload.raw,"sess-brief-raw"));
    return box;
  }
  if(payload.state) box.append(node("span",payload.state,`sess-brief-state st-${briefingStateClass(payload.state)}`));
  if(payload["one-line-job-description"]) box.append(node("div",payload["one-line-job-description"],"sess-brief-one"));
  // On the timeline a briefing is one post among many, so it shows what the
  // session is doing now; the goal, progress and FAQ are one click away.
  const row=(key,label,val,cls="sess-brief-row")=>{
    const r=node("div","",cls);
    r.append(node("span",label,"sess-brief-k"),richLine(String(val),"sess-brief-v"));
    return r;
  };
  if(payload.now) box.append(row("now","현재",payload.now));
  const more=[];
  for(const [key,label] of [["goal","목표"],["progress","진행"]]) if(payload[key]) more.push(row(key,label,payload[key]));
  if(Array.isArray(payload.faq)) {
    for(const item of payload.faq) {
      if(!item||!item.question||!item.answer)continue;
      more.push(row("faq",String(item.question),item.answer,"sess-brief-row sess-brief-faq"));
    }
  }
  if(more.length) {
    const rest=node("details","","sess-brief-more");
    rest.append(node("summary",`브리핑 전체 (${more.length})`),...more);
    box.append(rest);
  }
  if(!box.children.length) box.append(node("div","빈 브리핑입니다.","sess-brief-note"));
  return box;
}
function checksSnapshot(payload) {
  const box=node("div","","sess-brief");
  if(!Array.isArray(payload)||!payload.length) {
    box.append(node("div","표시할 체크 항목이 없습니다.","sess-brief-note"));
    return box;
  }
  for(const check of payload) {
    const row=node("div","","sess-brief-row sess-brief-check");
    const answer=node("span",statusCheckIcon(check),`status-check-icon check-${statusCheckText(check)}`);
    answer.title=String(check.question||"");
    row.append(answer,node("span",statusCheckName(check),"sess-brief-v"));
    box.append(row);
  }
  return box;
}
function eventItem(s,e) {
  const item=node("div","",`event kind-${e.kind}${e.needs_action&&!e.acknowledged?" action":""}${e.pivot?" pivot":""}${e.origin==="daemon"?" routine":""}`);
  item.dataset.event=e.id;
  const origin=e.origin==="record"?"저장된 기록":e.origin==="daemon"?"세션 이벤트":e.origin==="agent"?"에이전트 직접 보고":"자동 관찰";
  const when=e.run?`${clock(e.run.first)} ~ ${clock(e.run.last)}`:clock(e.at);
  const meta=node("small",`${s.name} · ${origin} · ${when}${e.acknowledged?" · 확인됨":""}`);
  meta.title=[new Date(e.at).toLocaleString(),e.observed_at?`관찰 ${new Date(e.observed_at).toLocaleString()}`:""].filter(Boolean).join(" · ");
  item.append(meta);
  const isRecord=e.origin==="record"&&(e.kind==="briefing"||e.kind==="checks");
  const snapshot=isRecord?recordPayload(e):null;
  const head=node("div","","obs-head");
  head.append(node("span",KIND_LABELS[e.kind]||e.kind,`obs-kind obs-kind-${e.kind}`));
  if(e.pivot) head.append(node("span","판단 변경","obs-pivot"));
  if(e.needs_action&&!e.acknowledged) head.append(node("span","확인 필요","obs-needs"));
  item.append(head);
  if(e.kind==="briefing"&&isRecord) item.append(briefingSnapshot(snapshot));
  else if(e.kind==="checks"&&isRecord) item.append(checksSnapshot(snapshot));
  else {
    head.append(richLine(e.text,"obs-line",s.name));
    if(e.run) head.append(node("span",`×${e.run.count}`,"obs-count"));
  }
  if(e.origin==="daemon") {
    if(e.kind==="borrow") {
      const d=e.details||{}, before=d.previous_null?"인증 없음":d.previous||"자체 프로파일", after=d.null_token?"인증 없음":d.current||"자체 프로파일";
      item.append(node("div",`${before} → ${after}`));
    } else if(e.kind==="worktree") {
      item.append(node("div",`${e.details?.previous||""} → ${e.details?.current||""}`));
    }
    if(Object.keys(e.details||{}).length) {
      const detail=node("details","");detail.append(node("summary","이벤트 상세"),node("pre",JSON.stringify(e.details,null,2)));item.append(detail);
    }
    return item;
  }
  if(e.origin==="agent") {addDirect(item,s,e);return item;}
  const detail=document.createElement("details"), evidence=node("pre","불러오는 중…");
  detail.append(node("summary",snapshot?"원본 JSON 보기":`근거 · ${e.source}`),evidence);
  if(snapshot) {
    // Already have the full payload from e.text — no round trip needed.
    detail.ontoggle=()=>{if(!detail.open||detail.dataset.loaded)return;evidence.textContent=JSON.stringify(snapshot,null,2);detail.dataset.loaded="1";};
  } else {
    detail.ontoggle=async()=>{if(!detail.open||detail.dataset.loaded)return;try{const data=await request(`api/observer/${encodeURIComponent(s.name)}/events/${encodeURIComponent(e.id)}`);evidence.textContent=JSON.stringify(data,null,2);detail.dataset.loaded="1";}catch(err){evidence.textContent=err.message;}};
  }
  item.append(detail);
  if(e.needs_action&&!e.acknowledged) {
    const button=node("button","확인 표시");button.onclick=async()=>{try{await request(`api/observer/${encodeURIComponent(s.name)}/acknowledge`,{id:e.id});await refresh();}catch(err){showError(err.message);}};item.append(button);
  }
  return item;
}
/* The observation meter, reported as the three figures the panel is for: the
   prompt that was not served from cache (입력), the part that was (캐시), and
   what came back (출력). `prompt_tokens` spans both cache sides, so a reported
   miss is the uncached input; when no cache counter was reported at all the
   miss side is unknown too, and the whole prompt is shown as 입력 with the
   cache column dropped rather than claimed as zero.
   The daemon keeps a lifetime total and a per-day breakdown per session; the
   page-level figure is those added up here, so the daemon stores one shape and
   the client decides how to read it. A row written before the meter carries
   only the last call's `usage`, and that is labelled as a last-call figure
   instead of being passed off as a lifetime total.
   The window cards and the per-session block read those same three figures
   off the same shape, which is what makes one word mean one quantity across
   the page: naming `prompt_tokens` "입력" in a card while the block named the
   uncached remainder by that word made the same usage read 9.8x apart
   (measured 2026-09-20: 73,965,831 against 7,583,639 over 53 session rows,
   with 89.5% of prompt tokens served from cache). The headline stays the
   summed `total_tokens`, so 입력 + 캐시 + 출력 still adds up to it. */
const usageCount = value => Number.isFinite(value) ? value.toLocaleString("ko-KR") : "0";
const usageNumber = value => Number.isFinite(Number(value)) ? Number(value) : 0;
function renderTokenSummary() {
  const summary=snapshot.usage_summary;
  const periods=[["hour","최근 1시간","1h"],["day","최근 24시간","24h"],["week","최근 일주일","7d"]];
  const compact=new Intl.NumberFormat("en",{notation:"compact",maximumFractionDigits:1});
  const cards=[], short=[];
  for(const [key,label,abbreviation] of periods) {
    const usage=summary?.windows?.[key];
    const total=usage?usageNumber(usage.total_tokens):null;
    short.push(`${abbreviation} ${total===null?"—":compact.format(total)}`);
    const card=node("div","","observer-token-window");
    card.append(node("span",label),node("strong",total===null?"—":usageCount(total)));
    if(usage){
      const shape=usageShape(usage);
      const parts=[`입력 ${usageCount(shape.input)}`];
      if(shape.counted)parts.push(`캐시 ${usageCount(shape.cached)}`);
      parts.push(`출력 ${usageCount(shape.output)}`);
      // Each figure is its own nowrap span so a narrow card breaks between
      // figures rather than between a label and the number it labels.
      const line=node("small","");
      parts.forEach((part,index)=>{
        if(index)line.append(" · ");
        line.append(node("span",part));
      });
      card.append(line);
    }
    cards.push(card);
  }
  $("token-compact").textContent=short.join(" · ");
  $("token-windows").replaceChildren(...cards);
  const since=summary?.since && new Date(summary.since);
  $("token-note").textContent=since && Number.isFinite(since.getTime())
    ? `집계 시작 ${since.toLocaleString("ko-KR")} · 시작 이전 사용량 제외 · 캐시는 입력과 별도로 표시`
    : "기간별 사용량을 조회할 수 없습니다.";
}
function usageShape(row) {
  const tokens=usageNumber(row.prompt_tokens), output=usageNumber(row.completion_tokens);
  const hit=usageNumber(row.prompt_cache_hit_tokens), miss=usageNumber(row.prompt_cache_miss_tokens);
  // A provider reports the cache as a pair; both sides zero means it reported
  // no cache at all, and then the whole prompt is the input.
  const reported=hit+miss>0;
  const calls=Number.isFinite(Number(row.calls))?Number(row.calls):null;
  return {calls,input:reported?Math.max(0,tokens-hit):tokens,cached:reported?hit:0,output,counted:reported};
}
function usageSum(rows) {
  const total={calls:0,hasCalls:false,input:0,cached:0,output:0,counted:false};
  for(const row of rows) {
    if(!row)continue;
    const shape=usageShape(row);
    total.input+=shape.input;total.cached+=shape.cached;total.output+=shape.output;
    total.counted=total.counted||shape.counted;
    if(shape.calls!==null){total.calls+=shape.calls;total.hasCalls=true;}
  }
  return total;
}
function usageLine(label,total) {
  const parts=[];
  if(total.hasCalls)parts.push(`호출 ${usageCount(total.calls)}회`);
  parts.push(`입력 ${usageCount(total.input)}`);
  if(total.counted)parts.push(`캐시 ${usageCount(total.cached)}`);
  return `${label}  ${parts.join(" · ")} · 출력 ${usageCount(total.output)}`;
}
function usageText(sessions) {
  const metered=sessions.filter(s=>s.usage_totals), lastCall=sessions.filter(s=>!s.usage_totals&&s.usage);
  const daily={};
  for(const s of sessions)for(const [day,u] of Object.entries(s.usage_daily||{}))(daily[day]||=[]).push(u);
  const lines=[];
  if(metered.length)lines.push(usageLine("누적",usageSum(metered.map(s=>s.usage_totals))));
  // Only the sessions the meter never reached: their last call is already
  // inside the lifetime total of the others, and counting it twice would
  // overstate the fleet.
  if(lastCall.length)lines.push(usageLine("마지막 관찰",usageSum(lastCall.map(s=>s.usage))));
  for(const day of Object.keys(daily).sort().reverse())lines.push(usageLine(day,usageSum(daily[day])));
  return lines.join("\n");
}
function usageBlock(s) {
  const lines=s.usage_totals?[usageLine("누적",usageSum([s.usage_totals])),
      ...Object.keys(s.usage_daily||{}).sort().reverse().map(day=>usageLine(day,usageSum([s.usage_daily[day]])))]
    : s.usage?[usageLine("마지막 관찰",usageSum([s.usage]))]:[];
  if(Number(s.rotations)>0)lines.push(`컨텍스트 회전 ${usageCount(Number(s.rotations))}회`);
  if(!lines.length)return null;
  const more=document.createElement("details");
  more.append(node("summary",`관찰 API 사용량${s.usage_totals?" (누적)":" (마지막 관찰)"}`),node("pre",lines.join("\n")));
  return more;
}
/* One session's events, oldest first, with routine runs folded. */
function sessionEvents(s) {
  return collapseRoutine([...(s.events||[])].sort((a,b)=>timestamp(a.at)-timestamp(b.at)));
}
/* A session card draws its newest events — as many as the slider says, the
   same count the latest grid uses — and folds the rest behind a button that
   opens them in place: the busiest sessions carried 200 events each (s29,
   s15 on 2026-10-15), and a card no longer scrolls inside itself, so every
   event it draws is page height (claunch-iw5i8.1). */
const expandedColumns = new Set();
function fillSession(body,s,actionsOnly=false) {
  body.append(node("p",activity(s).duration,"meta"),node("div",(s.meshes||[]).join(" · ")||"메시 없음","meta"));
  body.append(...sessionStatus(s));
  for(const r of sessionRuns(s).filter(r=>String(r.status).startsWith("waiting")))addGate(body,s,r);
  body.append(s.summary?richLine(s.summary,"obs-summary",s.name):node("p","아직 관찰 결과가 없습니다."));
  if(s.error)body.append(node("p",s.error));
  const events=sessionEvents(s).reverse().filter(e=>!actionsOnly||(e.needs_action&&!e.acknowledged));
  const limit=expandedColumns.has(s.name)?events.length:gridLimit;
  for(const e of events.slice(0,limit))body.append(eventItem(s,e));
  if(events.length>limit) {
    const more=node("button",`이전 항목 ${events.length-limit}개 더 보기`,"observer-more");
    more.type="button";
    more.onclick=()=>{expandedColumns.add(s.name);render();};
    body.append(more);
  }
  // A session the meter never reached and whose last call was never recorded
  // has nothing to report; the block is omitted rather than shown empty.
  const usage=usageBlock(s);
  if(usage)body.append(usage);
}
/* The detail panel's Observation section: what this page knows about one
   session, drawn in the rail beside what the panel already says about it.
   The rail rebuilds every 2s and calls this on each build, so the box is kept
   here and handed back each time (the panel moves the same node, as it does
   its send box), and the observer is asked again at most once every ten
   seconds — the cadence the observer page itself polls at. Nothing runs
   while the rail is not drawing the section, since only its calls ask.
   It carries only what the rest of the panel does not: the meshes are the
   Meshes box, a waiting cflow gate is the Workflow panel, the pin is the
   Flags box, the times are the metadata, and the terminal and transcript are
   the header's view radio. */
let panelBox = null, panelAsked = 0, panelLoaded = false;
const PANEL_POLL_MS = 10000;
function panelSection(name) {
  if(!panelBox||panelBox.dataset.session!==name) {
    panelBox=node("div","","sess-observer");panelBox.dataset.session=name;
    panelAsked=0;
    renderPanel();
  }
  if(Date.now()-panelAsked>=PANEL_POLL_MS&&!document.hidden) {
    panelAsked=Date.now();refresh();
  }
  return panelBox;
}
/* The panel's record log, closed on every draw. `details` learns its open
   state from the attribute alone, so a rebuilt node comes back folded and the
   reader's choice is restored by renderPanel's fold key, not by this node. */
function eventLog(s,events) {
  const log=document.createElement("details");
  log.className="obs-history";
  log.dataset.fold="history";
  log.append(node("summary",`세션 기록 ${events.length}건`));
  for(const e of events)log.append(eventItem(s,e));
  return log;
}
function renderPanel() {
  if(!panelBox)return;
  const name=panelBox.dataset.session, s=snapshot.sessions.find(s=>s.name===name);
  // A redraw closes every fold it replaces, so the open ones are read off
  // the old draw first and opened again on the new one. A fold that carries
  // its own key keeps it across a changing count (the record log does: its
  // summary says how many records are inside).
  const foldKey=d=>d.dataset.fold||`${d.closest("[data-event]")?.dataset.event||""}|${d.querySelector("summary")?.textContent||""}`;
  const open=new Set([...panelBox.querySelectorAll("details")].filter(d=>d.open).map(foldKey));
  const title=node("h3","Observation ");
  if(s){const state=activity(s);title.append(node("span",state.label,`state ${state.kind}`));}
  const parts=[title];
  const updated=s&&updatedText(s);
  if(updated)parts.push(node("small",updated,"observer-updated"));
  const notice=node("p",!panelLoaded?"관찰 정보를 불러오는 중…":snapshot.error||(snapshot.enabled?"관찰 중":"관찰이 꺼져 있습니다 · 「지금 갱신」은 사용할 수 있습니다."),"meta");
  notice.setAttribute("role","status");parts.push(notice);
  const links=node("nav","","observer-links");
  const page=node("a","관찰 결과");page.href="#/observer/session/"+encodeURIComponent(name);
  const update=node("button","지금 갱신","observer-action");update.type="button";
  update.disabled=refreshing.has(name);
  update.title=`${name}을 지금 한 번 관찰합니다. 관찰이 꺼져 있어도 이 한 번은 수행됩니다.`;
  update.onclick=()=>oneShot(name);
  links.append(page,update);
  const note=refreshNotes.get(name);
  if(note)links.append(node("small",note,"observer-action-note"));
  parts.push(links);
  if(panelLoaded&&!s)parts.push(node("p","아직 이 세션의 관찰 정보가 없습니다."));
  if(s) {
    parts.push(s.summary?richLine(s.summary,"obs-summary",s.name):node("p","아직 관찰 결과가 없습니다."));
    if(s.error)parts.push(node("p",s.error));
    // The record log folds shut (claunch-822yy): one session carried seven
    // adopt lines from daemon restarts alone, and the panel hands every event
    // it knows to the rail, so the log pushed the section's own summary and
    // meter out of view. An unacknowledged request is the exception — hiding
    // it behind a click is what this section exists to prevent.
    const events=sessionEvents(s).reverse();
    const requests=events.filter(e=>e.needs_action&&!e.acknowledged);
    const records=events.filter(e=>!(e.needs_action&&!e.acknowledged));
    for(const e of requests)parts.push(eventItem(s,e));
    if(records.length)parts.push(eventLog(s,records));
    // A session the meter never reached and whose last call was never recorded
    // has nothing to report; the block is omitted rather than shown empty.
    const usage=usageBlock(s);
    if(usage)parts.push(usage);
  }
  panelBox.replaceChildren(...parts);
  for(const d of panelBox.querySelectorAll("details"))if(open.has(foldKey(d)))d.open=true;
}
function showError(message) {
  const notice=panelBox?.isConnected&&panelBox.querySelector('[role="status"]');
  if(notice&&!document.body.classList.contains("observer-active"))notice.textContent=message;
  else $("notice").textContent=message;
}
/* Both readers of the snapshot: the observer page when it is on screen, and
   the detail panel's section when the rail has one. */
function redraw() {
  if(document.body.classList.contains("observer-active"))render();
  renderPanel();
}
function render() {
  const visible=visibleSessions(), cards=$("cards"), mobile=mobileView.matches;
  const view=mobile?"timeline":layout, grid=view==="grid", timeline=view==="timeline";
  for(const mode of ["board","grid","timeline"]) $("layout-"+mode).checked=view===mode;
  $("limit-control").hidden=timeline;
  $("limit").value=String(gridLimit);$("limit-value").value=`${gridLimit}개`;
  syncTargets(visible);
  composerState();
  const replies=visible.reduce((n,s)=>n+newReplies(s).length,0);
  $("counts").textContent=`${visible.length}개 세션 · 미확인 요청 ${visible.reduce((n,s)=>n+actionCount(s),0)}개${replies?` · 새 쓰레드 답장 ${replies}개`:""}`;
  const usage=usageText(snapshot.sessions);
  renderTokenSummary();
  $("usage").hidden=!usage;
  $("usage-body").textContent=usage;
  $("monitor").textContent=snapshot.enabled?"관찰 끄기":"관찰 시작";
  $("mobile-monitor").textContent=$("monitor").textContent;
  // Drawn from the snapshot, never from the click: the scope is the daemon's
  // answer, and a request that failed must not leave the box reading as set.
  const scoped=snapshot.scope==="pinned";
  $("scope-pinned").checked=scoped;
  $("mobile-scope-pinned").checked=scoped;
  $("notice").textContent=snapshot.error||(snapshot.enabled?"관찰 중 · 세션별 순차 처리 · 최소 60초 간격":"관찰이 꺼져 있습니다. 시작하면 ds4-official/deepseek-flash API로 트랜스크립트를 전송합니다. 「지금 갱신」은 꺼져 있어도 그 세션 하나를 한 번 전송합니다.");
  $("sort").hidden=timeline||grid;
  $("layout-hint").textContent=grid?`세션마다 최신 항목 최대 ${gridLimit}개 · 세션 목록은 필터 그대로 · 보고·승인 요청 기준`:timeline?"최신 보고부터 표시하는 타임라인":`세션 카드 · 카드마다 최신 항목 ${gridLimit}개, 나머지는 카드의 「더 보기」로 펼칩니다 · 내용은 자동 갱신되며 세션 순서는 최신순 정렬을 누를 때 바뀝니다.`;
  cards.className=grid?"observer-grid":timeline?"observer-timeline":"observer-board";
  // The page is the one scroller: keep where the reader is across the
  // rebuild, which empties the cards for a moment and would clamp it.
  const page=$("view"), top=page.scrollTop;
  cards.replaceChildren();
  if(timeline||grid) {
    const entries=[];
    for(const s of visible) {
      const events=sessionEvents(s).filter(e=>!$("actions-only").checked||(e.needs_action&&!e.acknowledged));
      for(const e of events) entries.push({s,e,at:timestamp(e.at),key:s.name+":"+e.id});
      for(const r of sessionRuns(s).filter(r=>String(r.status).startsWith("waiting")))
        entries.push({s,r,at:timestamp(r.updated_at||r.started_at)||latestActivity(s),key:s.name+":gate:"+(r.run||r.step_id)});
      if(!events.length&&!sessionRuns(s).some(r=>String(r.status).startsWith("waiting")))entries.push({s,at:latestActivity(s),key:s.name+":summary"});
    }
    entries.sort((a,b)=>b.at-a.at||a.key.localeCompare(b.key));
    // The slider bounds each session's history, not the fleet: the filtered
    // session list is what the reader asked to see, so every session in it is
    // drawn and the cut falls inside each one's own posts. `entries` is newest
    // first, so the first N a session contributes are its latest N.
    const perSession=new Map();
    const shown=grid?entries.filter(({s})=>{
      const seen=perSession.get(s.name)||0;
      if(seen>=gridLimit)return false;
      perSession.set(s.name,seen+1);
      return true;
    }):entries.slice(0,timelineLimit);
    // One meter per session, on that session's newest post: the timeline
    // repeats a busy session for every event, and the same totals under each
    // of them would be noise rather than a measurement.
    const metered=new Set();
    for(const {s,e,r} of shown) {
      const card=sessionHeader(s,"observer-post");
      if(!metered.has(s.name))card.append(...sessionStatus(s));
      if(e)card.append(eventItem(s,e));
      else if(r)addGate(card,s,r);
      else card.append(s.summary?richLine(s.summary,"obs-summary",s.name):node("p","아직 관찰 결과가 없습니다."),node("small",activity(s).duration));
      if(!metered.has(s.name)) {metered.add(s.name);const usage=usageBlock(s);if(usage)card.append(usage);}
      cardActions(card,s);cards.append(card);
    }
    if(!grid&&entries.length>shown.length) {
      const more=node("button",`이전 항목 ${entries.length-shown.length}개 중 ${Math.min(TIMELINE_STEP,entries.length-shown.length)}개 더 보기`,"observer-more");
      more.type="button";
      more.onclick=()=>{timelineLimit+=TIMELINE_STEP;render();};
      cards.append(more);
    }
  } else {
    if(!boardOrder.length)boardOrder=[...snapshot.sessions].sort((a,b)=>latestActivity(b)-latestActivity(a)||a.name.localeCompare(b.name)).map(s=>s.name);
    for(const s of snapshot.sessions)if(!boardOrder.includes(s.name))boardOrder.push(s.name);
    visible.sort((a,b)=>boardOrder.indexOf(a.name)-boardOrder.indexOf(b.name));
    for(const s of visible) {
      const card=sessionHeader(s,"observer-column"), body=node("div","","observer-column-body");
      fillSession(body,s,$("actions-only").checked);
      card.append(body);cardActions(card,s);cards.append(card);
    }
  }
  if(!cards.children.length)cards.append(node("p","선택한 조건에 해당하는 세션이 없습니다."));
  page.scrollTop=top;
  controls();
}
for(const mode of ["board","grid","timeline"]) $("layout-"+mode).onchange=()=>{layout=mode;saveView();render();};
$("limit").oninput=()=>{gridLimit=Number($("limit").value);saveView();render();};
$("sort").onclick=()=>{boardOrder=[...snapshot.sessions].sort((a,b)=>latestActivity(b)-latestActivity(a)||a.name.localeCompare(b.name)).map(s=>s.name);render();};
mobileView.addEventListener("change",()=>{if(document.body.classList.contains("observer-active"))render();});
function selections() {
  const scope=$("scope").value;
  $("selection-label").hidden=scope==="global";
  options($("selection"),scope==="mesh"?[...new Set(snapshot.sessions.flatMap(s=>s.meshes))].sort():snapshot.sessions.map(s=>s.name));
  if(scope===routeScope && routeName) $("selection").value=routeName;
}
/* Whether this read needs exited sessions' timelines. The daemon leaves them
   out unless asked (?ended=1): they were most of the answer (9.5 MB of
   which 1.3 MB was the running sessions, 2026-10-15) and the page hides
   them until 「종료된 세션 포함」 is on. A direct link to an exited session,
   or the detail panel open on one, needs that one's timeline as well. */
function wantEnded() {
  const onPage=document.body.classList.contains("observer-active");
  if(onPage&&$("ended").checked)return true;
  const names=[onPage&&routeScope==="session"?routeName:"",panelBox?.isConnected?panelBox.dataset.session:""];
  // Asked of the session's state, not of whether its timeline came: a full
  // answer has no ``events_omitted``, and asking that would send the next
  // poll light again and the one after full, every ten seconds.
  return names.some(n=>n&&snapshot.sessions.some(s=>s.name===n&&!s.running));
}
/* The rail caches' own signatures, so a QA question, a thread reply or a run
   moving redraws the cards even when the observer's answer did not change. */
const railSignature = () => [typeof cflowCache!=="undefined"?JSON.stringify(cflowCache):"",
  typeof cflow2RailSig!=="undefined"?cflow2RailSig:"",typeof qaRailSig!=="undefined"?qaRailSig:"",
  typeof threadsRailSig!=="undefined"?threadsRailSig:""].join("\u0000");
function refresh() {
  // Route changes share a pending request so direct-link target selection
  // always runs after that response, including on a slow first load.
  if(refreshTask)return refreshTask;
  refreshTask=(async()=>{
    try {
      const ended=wantEnded();
      const response=await api(ended?"api/observer?ended=1":"api/observer");
      const text=await response.text();
      const data=JSON.parse(text);
      if(!response.ok)throw Error(data.error||`HTTP ${response.status}`);
      // The response text is the signature: it is already one string, and
      // stringifying the parsed answer again cost as much as parsing it.
      const signature=[text,railSignature(),Math.floor(Date.now()/60000)].join("\u0000");
      if(document.activeElement?.matches(".observer-answer textarea,.observer-gate textarea,.observer-gate input,.observer-gate select"))return;
      panelLoaded=true;
      if(signature!==lastSnapshot) {
        snapshot=data; runs=appRuns(); lastSnapshot=signature;
        selections();
        redraw();
      }
      // The first answer is what says a linked session has exited; ask again
      // for its timeline, once (the second answer carries ``ended``).
      // A daemon without the switch has no ``ended`` key and always answers
      // in full, so only an explicit false asks again.
      if(data.ended===false&&wantEnded())setTimeout(()=>{lastSnapshot="";refresh();});
      return true;
    } catch(err) { showError(err.message);lastSnapshot="";return false; }
    finally { refreshTask=null; }
  })();
  return refreshTask;
}
function navigate() {
  const scope=$("scope").value, name=$("selection").value;
  go(scope === "global" ? "#/observer" : `#/observer/${scope}/${encodeURIComponent(name)}`);
}
$("scope").onchange=()=>{selections();navigate();};
$("activity").onchange=render;
$("selection").onchange=navigate;$("actions-only").onchange=render;
$("ended").onchange=()=>{render();if($("ended").checked&&snapshot.ended===false){lastSnapshot="";refresh();}};
/* The draft box grows with its text up to 40% of the viewport and scrolls
   inside itself past that (observer.css): the composer is pinned to the
   bottom of the screen, and a box that kept growing would cover the page it
   sits on. It is the page's one inner scroller, and only while a draft is
   that long. field-sizing does the growing where supported, and this is the
   fallback. Every change of the draft (typing, switching target, clearing on
   send) ends in controls(), so that is where it runs. */
function autosize() {
  const box=$("prompt");
  if(CSS.supports?.("field-sizing","content"))return;
  box.style.height="auto";
  // Folded, the box has no layout; leave it to its CSS height until shown.
  if(box.scrollHeight)box.style.height=Math.min(box.scrollHeight,innerHeight*0.4)+"px";
}
$("target").onchange=()=>{targetPicked=true;chooseTarget($("target").value);};$("prompt").oninput=controls;
$("composer-toggle").onclick=()=>{composerFolded=!composerFolded;composerState();if(!composerFolded)$("prompt").focus();};
$("monitor").onclick=async()=>{try{await request("api/observer/settings",{enabled:!snapshot.enabled});await refresh();}catch(err){showError(err.message);}};
/* The scope rides the same settings call as the monitor switch, and both
   sides are sent every time: the route takes ``enabled`` as required and
   ``scope`` as optional, so leaving one out is how a control would silently
   undo the other's setting. Two boxes drive it — the header's, and the one
   that stands in for the header on a phone — and both are redrawn from the
   snapshot, so they cannot disagree. */
async function setScope(pinned) {
  try {
    await request("api/observer/settings",{enabled:snapshot.enabled,scope:pinned?"pinned":"all"});
  } catch(err) {showError(err.message);}
  finally {lastSnapshot="";await refresh();}
}
$("scope-pinned").onchange=()=>setScope($("scope-pinned").checked);
$("mobile-scope-pinned").onchange=()=>setScope($("mobile-scope-pinned").checked);
$("token-toggle").onclick=()=>{
  const expanded=$("token-toggle").getAttribute("aria-expanded")!=="true";
  $("token-toggle").setAttribute("aria-expanded",String(expanded));
  $("token-summary").classList.toggle("expanded",expanded);
  $("token-chevron").textContent=expanded?"⌃":"⌄";
};
$("mobile-monitor").onclick=()=>$("monitor").click();
$("operator-toggle").onclick=()=>setDock(!dockOpen);
$("mobile-operator").onclick=()=>setDock(!dockOpen);
async function send(interrupt) {
  const target=$("target").value, text=$("prompt").value;
  if(pending||!target||(!interrupt&&!text.trim()))return;
  pending=true;controls();
  if(operators()?.isTarget(target)) {
    try {
      if(interrupt)return;
      $("input-status").textContent=await operators().send(target,text);
      if($("target").value===target&&$("prompt").value===text){$("prompt").value="";drafts.delete(target);}
    } catch(err){$("input-status").textContent=err.message;} finally{pending=false;controls();}
    return;
  }
  try {
    const inputId = typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `observer-${Date.now()}-${Array.from(crypto.getRandomValues(new Uint32Array(3))).join("-")}`;
    const sent=await request(`api/sessions/${encodeURIComponent(target)}/keys`, interrupt?{keys:["Escape"]}:{keys:[text,"Enter"],input_id:inputId});
    // An exited target answers 202 queued: nothing was typed yet, and the
    // line waits in the session's input journal until it is resumed.
    $("input-status").textContent=interrupt?`${target}: Esc 전송됨. 실제 상태를 확인하십시오.`:sent&&sent.queued?`${target}: 세션이 종료되어 지시를 대기열에 넣었습니다(${sent.position||1}번째). 세션을 재개하면 입력됩니다.`:`${target}: 지시 전송됨`;
    if(!interrupt&&$("target").value===target&&$("prompt").value===text){$("prompt").value="";drafts.delete(target);}
  }catch(err){$("input-status").textContent=err.message;}finally{pending=false;controls();}
}
$("send").onclick=()=>send(false);$("interrupt").onclick=()=>send(true);
let poll = null, routeScope = "global", routeName = "", generation = 0;
function stop() { clearInterval(poll); poll=null; generation++; document.body.classList.remove("observer-active"); }
async function open(scope = "global", name = "") {
  stop();
  document.body.classList.add("observer-active");
  const ticket=generation;
  routeScope=scope; routeName=name;
  $("scope").value=scope;
  await refresh();
  if(ticket!==generation)return;
  selections();
  if(name) $("selection").value=name;
  if(scope === "session") {
    const session=snapshot.sessions.find(s=>s.name===name);
    if(session&&!session.running) $("ended").checked=true;
    render();
    chooseTarget(session?.running && visibleSessions().some(s=>s.name===name) ? name : "");
  }
  render();
  operators()?.refresh();
  poll=setInterval(()=>{if(!document.hidden){refresh();operators()?.refresh();}},10000);
}
composerState();
dockState();
/* A change in the operator list changes the composer's targets and which
   cards can ask about their session. */
operators()?.mount($("operators"),()=>{if(document.body.classList.contains("observer-active"))render();});
return {open, stop, panelSection};
})();
