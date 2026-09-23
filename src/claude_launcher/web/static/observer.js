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
let runs = [], gateCache = new Map(), cflowError = false;
let draftTarget = "";
// The composer starts folded at every width and stays where the reader left
// it for as long as the page is loaded; a reload comes back folded.
let composerFolded = true;
let layout = "board", gridLimit = 5;
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
  const s=snapshot.sessions.find(s=>s.name===$("target").value);
  $("send").disabled=pending || !s?.running || !$("prompt").value.trim();
  $("interrupt").disabled=pending || !s?.running;
  $("target").disabled=pending;
}
function sessionRuns(s) {
  return runs.filter(r => r.scope===s.name || (r.sessions||[]).includes(s.name));
}
function activity(s) {
  const own=sessionRuns(s), pending=own.some(r=>String(r.status).startsWith("waiting"));
  const question=(s.events||[]).some(e=>e.question&&!e.answer);
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
    section.append(wfActions(data,{archive:false,reminder:false,host:"observer",after:()=>{gateCache.clear();lastSnapshot="";refresh();}}));
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
    && (!$("actions-only").checked||(s.events||[]).some(e=>e.needs_action&&!e.acknowledged)||sessionRuns(s).some(sessCflowGated)));
}
function syncTargets(visible) {
  const names=visible.filter(s=>s.running).map(s=>s.name);
  drafts.set(draftTarget,$("prompt").value);
  options($("target"),names,"세션 선택");
  if(!names.includes(draftTarget)) chooseTarget("");
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
  toggle.textContent=`입력 대상 세션 ${composerFolded?"▸":"▾"}`;
}
/* Everything a card does to its session is in one row: the three links to what
   already exists, the action that types at this session, and the one that
   observes it now. Both buttons used to be bars under the card, where the
   board's column flex made them as wide as the card and the shared 44px touch
   target made them the tallest thing on it — the type-at action read as the
   card's primary action, which it is not.
   The two act rather than link, so they are chips and the three stay text. */
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
  // the state. This one wears the card's action chip so the row keeps one
  // weight, and CSS dims it until it is on — the same rule and the same 👁
  // the rail uses, so the control is recognisable across both pages.
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
  const note=refreshNotes.get(s.name);
  if(note)links.append(node("small",note,"observer-action-note"));
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
  refreshing.add(name);refreshNotes.set(name,"갱신 중…");render();
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
function chip(kind,text) {
  const c=node("span",kind==="session"&&!text.startsWith("@")?"@"+text:text,`obs-chip obs-chip-${kind}`);
  c.title=CHIP_TITLES[kind]||kind;
  return c;
}
function chipsInto(parent,text) {
  let last=0;
  for(const m of text.matchAll(new RegExp(CHIP_PATTERN,"g"))) {
    if(!m[0])continue;
    if(m.index>last)parent.append(node("span",text.slice(last,m.index)));
    parent.append(chip(chipKind(m.groups,m[0]),m[0]));
    last=m.index+m[0].length;
  }
  if(last<text.length)parent.append(node("span",text.slice(last)));
}
function richLine(text,cls="obs-line") {
  const line=node("div","",cls), source=String(text??"");
  let last=0;
  for(const m of source.matchAll(/`([^`\n]+)`/g)) {
    if(m.index>last)chipsInto(line,source.slice(last,m.index));
    const whole=m[1].match(new RegExp(`^(?:${CHIP_PATTERN})$`));
    line.append(whole?chip(chipKind(whole.groups,m[1]),m[1]):node("code",m[1],"obs-code"));
    last=m.index+m[0].length;
  }
  if(last<source.length)chipsInto(line,source.slice(last));
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
    head.append(richLine(e.text));
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
function fillSession(body,s,actionsOnly=false) {
  body.append(node("p",activity(s).duration,"meta"),node("div",(s.meshes||[]).join(" · ")||"메시 없음","meta"));
  for(const r of sessionRuns(s).filter(r=>String(r.status).startsWith("waiting")))addGate(body,s,r);
  body.append(s.summary?richLine(s.summary,"obs-summary"):node("p","아직 관찰 결과가 없습니다."));
  if(s.error)body.append(node("p",s.error));
  const events=sessionEvents(s).reverse();
  for(const e of events.filter(e=>!actionsOnly||(e.needs_action&&!e.acknowledged)))body.append(eventItem(s,e));
  // A session the meter never reached and whose last call was never recorded
  // has nothing to report; the block is omitted rather than shown empty.
  const usage=usageBlock(s);
  if(usage)body.append(usage);
}
let embedded = null, embeddedPoll = null;
function showError(message) {
  if(embedded) {
    const notice=embedded.host.querySelector('[role="status"]');
    if(notice)notice.textContent=message;
  } else $("notice").textContent=message;
}
function closeSession() {
  clearInterval(embeddedPoll);embeddedPoll=null;embedded=null;
}
function renderSession() {
  if(!embedded)return;
  const {name,host}=embedded, s=snapshot.sessions.find(s=>s.name===name);
  const scroll=host.scrollTop;
  host.replaceChildren();
  const notice=node("p",cflowError?"cflow 상태 조회 실패 · 마지막 조회 결과 표시":snapshot.error||(snapshot.enabled?"관찰 중":"관찰이 꺼져 있습니다 · 「지금 갱신」은 사용할 수 있습니다."));
  notice.setAttribute("role","status");host.append(notice);
  if(!s) {host.append(node("p","아직 이 세션의 관찰 정보가 없습니다."));return;}
  const card=sessionHeader(s), body=node("div");
  fillSession(body,s);card.append(body);host.append(card);host.scrollTop=scroll;
}
async function openSession(name,host) {
  closeSession();
  const target={name,host};embedded=target;
  host.replaceChildren(node("p","세션 정보를 불러오는 중…"));
  const loaded=await refresh();
  if(embedded!==target)return;
  if(loaded)renderSession();
  embeddedPoll=setInterval(()=>{if(!document.hidden)refresh();},10000);
}
function render() {
  const visible=visibleSessions(), cards=$("cards"), mobile=mobileView.matches;
  const view=mobile?"timeline":layout, grid=view==="grid", timeline=view==="timeline";
  for(const mode of ["board","grid","timeline"]) $("layout-"+mode).checked=view===mode;
  $("limit-control").hidden=!grid;
  $("limit").value=String(gridLimit);$("limit-value").value=`${gridLimit}개`;
  syncTargets(visible);
  composerState();
  $("counts").textContent=`${visible.length}개 세션 · 미확인 요청 ${visible.reduce((n,s)=>n+(s.events||[]).filter(e=>e.needs_action&&!e.acknowledged).length+sessionRuns(s).filter(sessCflowGated).length,0)}개`;
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
  $("notice").textContent=(cflowError?"cflow 상태 조회 실패 · 마지막 조회 결과 표시":snapshot.error)||(snapshot.enabled?"관찰 중 · 세션별 순차 처리 · 최소 60초 간격":"관찰이 꺼져 있습니다. 시작하면 ds4-official/deepseek-flash API로 트랜스크립트를 전송합니다. 「지금 갱신」은 꺼져 있어도 그 세션 하나를 한 번 전송합니다.");
  $("sort").hidden=timeline||grid;
  $("layout-hint").textContent=grid?`세션마다 최신 항목 최대 ${gridLimit}개 · 세션 목록은 필터 그대로 · 보고·승인 요청 기준`:timeline?"최신 보고부터 표시하는 타임라인":"세션 보드 · 내용은 자동 갱신되며 세션 순서는 최신순 정렬을 누를 때 바뀝니다.";
  cards.className=grid?"observer-grid":timeline?"observer-timeline":"observer-board";
  const horizontal=cards.scrollLeft;
  const scrolls=new Map([...cards.querySelectorAll(".observer-column-body")].map(e=>[e.parentElement.dataset.session,e.scrollTop]));
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
    }):entries;
    // One meter per session, on that session's newest post: the timeline
    // repeats a busy session for every event, and the same totals under each
    // of them would be noise rather than a measurement.
    const metered=new Set();
    for(const {s,e,r} of shown) {
      const card=sessionHeader(s,"observer-post");
      if(e)card.append(eventItem(s,e));
      else if(r)addGate(card,s,r);
      else card.append(s.summary?richLine(s.summary,"obs-summary"):node("p","아직 관찰 결과가 없습니다."),node("small",activity(s).duration));
      if(!metered.has(s.name)) {metered.add(s.name);const usage=usageBlock(s);if(usage)card.append(usage);}
      cardActions(card,s);cards.append(card);
    }
  } else {
    if(!boardOrder.length)boardOrder=[...snapshot.sessions].sort((a,b)=>latestActivity(b)-latestActivity(a)||a.name.localeCompare(b.name)).map(s=>s.name);
    for(const s of snapshot.sessions)if(!boardOrder.includes(s.name))boardOrder.push(s.name);
    visible.sort((a,b)=>boardOrder.indexOf(a.name)-boardOrder.indexOf(b.name));
    for(const s of visible) {
      const card=sessionHeader(s,"observer-column"), body=node("div","","observer-column-body");
      fillSession(body,s,$("actions-only").checked);
      card.append(body);cardActions(card,s);cards.append(card);body.scrollTop=scrolls.get(s.name)||0;
    }
  }
  if(!cards.children.length)cards.append(node("p","선택한 조건에 해당하는 세션이 없습니다."));
  cards.scrollLeft=horizontal;
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
function refresh() {
  // Route changes share a pending request so direct-link target selection
  // always runs after that response, including on a slow first load.
  if(refreshTask)return refreshTask;
  refreshTask=(async()=>{
    try {
      const [data,cflow]=await Promise.all([request("api/observer"),request("api/cflow?view=rail").catch(()=>({runs:[],error:true}))]);
      cflowError=!!cflow.error;
      if(!cflowError) runs=cflow.runs||[];
      const signature=JSON.stringify([data,runs,cflowError,Math.floor(Date.now()/60000)]);
      if(document.activeElement?.matches(".observer-answer textarea,.observer-gate textarea,.observer-gate input,.observer-gate select"))return;
      if(signature!==lastSnapshot) {
        snapshot=data; lastSnapshot=signature;
        selections();
        if(embedded)renderSession();else render();
      }
      return true;
    } catch(err) { if(embedded)embedded.host.replaceChildren(node("p",err.message));else showError(err.message);lastSnapshot="";return false; }
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
$("selection").onchange=navigate;$("actions-only").onchange=render;$("ended").onchange=render;
$("target").onchange=()=>chooseTarget($("target").value);$("prompt").oninput=controls;
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
async function send(interrupt) {
  const target=$("target").value, text=$("prompt").value;
  if(pending||!target||(!interrupt&&!text.trim()))return;
  pending=true;controls();
  try {
    const inputId = typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `observer-${Date.now()}-${Array.from(crypto.getRandomValues(new Uint32Array(3))).join("-")}`;
    await request(`api/sessions/${encodeURIComponent(target)}/keys`, interrupt?{keys:["Escape"]}:{keys:[text,"Enter"],input_id:inputId});
    $("input-status").textContent=interrupt?`${target}: Esc 전송됨. 실제 상태를 확인하십시오.`:`${target}: 지시 전송됨`;
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
  poll=setInterval(()=>{if(!document.hidden)refresh();},10000);
}
composerState();
return {open, stop, openSession, closeSession};
})();

/* Transcript panels keep the conversation DOM and its scroll position intact. */
globalThis.TranscriptTabs = (() => {
  const transcript=document.getElementById("term-log-pane"), info=document.getElementById("log-info-pane");
  const tabs=[document.getElementById("log-transcript-tab"),document.getElementById("log-info-tab")];
  function select(value) {
    const showInfo=value==="info";
    transcript.hidden=showInfo;info.hidden=!showInfo;
    tabs.forEach((tab,index)=>{const active=(index===1)===showInfo;tab.setAttribute("aria-selected",String(active));tab.tabIndex=active?0:-1;});
    if(showInfo)ObserverPage.openSession(transcriptName,info);
    else ObserverPage.closeSession();
  }
  tabs.forEach((tab,index)=>{
    tab.onclick=()=>select(index?"info":"transcript");
    tab.onkeydown=event=>{
      if(!["ArrowLeft","ArrowRight","Home","End"].includes(event.key))return;
      event.preventDefault();
      const next=event.key==="Home"?0:event.key==="End"?1:1-index;
      select(next?"info":"transcript");tabs[next].focus();
    };
  });
  return {select};
})();
