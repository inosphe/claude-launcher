/* Observer uses the main app router, authentication and viewport. */
globalThis.ObserverPage = (() => {
"use strict";
const $ = id => document.getElementById("observer-" + id);
let snapshot = {sessions: [], enabled: false}, pending = false, refreshTask = null, lastSnapshot = "";
const drafts = new Map(), answerDrafts = new Map();
let runs = [], gateCache = new Map(), cflowError = false;
let draftTarget = "";
// The composer starts folded at every width and stays where the reader left
// it for as long as the page is loaded; a reload comes back folded.
let composerFolded = true;
let layout = "board", gridLimit = 5;
try {
  layout = localStorage.getItem("claunch-observer-layout") === "grid" ? "grid" : "board";
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
function inputButton(card,s) {
  const button=node("button","이 세션에 입력");button.disabled=!s.running;
  // Picking a session is a request to type at it, so the composer opens first
  // when it is folded — a focus() on a hidden textarea would go nowhere.
  button.onclick=()=>{chooseTarget(s.name);composerFolded=false;composerState();$("prompt").focus();};card.append(button);
}
function eventItem(s,e) {
  const item=node("div","",`event${e.needs_action&&!e.acknowledged?" action":""}`);
  item.dataset.event=e.id;
  const origin=e.origin==="daemon"?"세션 이벤트":e.origin==="agent"?"에이전트 직접 보고":"자동 관찰";
  item.append(node("small",`${origin} · ${e.kind} · ${new Date(e.at).toLocaleString()}${e.acknowledged?" · 확인됨":""}`),node("div",e.text));
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
  detail.append(node("summary",`근거 · ${e.source}`),evidence);
  detail.ontoggle=async()=>{if(!detail.open||detail.dataset.loaded)return;try{const data=await request(`api/observer/${encodeURIComponent(s.name)}/events/${encodeURIComponent(e.id)}`);evidence.textContent=JSON.stringify(data,null,2);detail.dataset.loaded="1";}catch(err){evidence.textContent=err.message;}};
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
   instead of being passed off as a lifetime total. */
const usageCount = value => Number.isFinite(value) ? value.toLocaleString("ko-KR") : "0";
const usageNumber = value => Number.isFinite(Number(value)) ? Number(value) : 0;
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
function fillSession(body,s,actionsOnly=false) {
  body.append(node("p",activity(s).duration,"meta"),node("div",(s.meshes||[]).join(" · ")||"메시 없음","meta"));
  for(const r of sessionRuns(s).filter(r=>String(r.status).startsWith("waiting")))addGate(body,s,r);
  body.append(node("p",s.summary||"아직 관찰 결과가 없습니다."));
  if(s.error)body.append(node("p",s.error));
  const events=[...(s.events||[])].sort((a,b)=>timestamp(b.at)-timestamp(a.at));
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
  const notice=node("p",cflowError?"cflow 상태 조회 실패 · 마지막 조회 결과 표시":snapshot.error||(snapshot.enabled?"관찰 중":"관찰이 꺼져 있습니다."));
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
  const grid=layout==="grid";
  $("layout-board").checked=!grid;$("layout-grid").checked=grid;
  $("board-label").textContent=mobile?"타임라인":"보드";
  $("limit-control").hidden=!grid;
  $("limit").value=String(gridLimit);$("limit-value").value=`${gridLimit}개`;
  syncTargets(visible);
  composerState();
  $("counts").textContent=`${visible.length}개 세션 · 미확인 요청 ${visible.reduce((n,s)=>n+(s.events||[]).filter(e=>e.needs_action&&!e.acknowledged).length+sessionRuns(s).filter(sessCflowGated).length,0)}개`;
  const usage=usageText(snapshot.sessions);
  $("usage").hidden=!usage;
  $("usage-body").textContent=usage;
  $("monitor").textContent=snapshot.enabled?"관찰 끄기":"관찰 시작";
  $("mobile-monitor").textContent=$("monitor").textContent;
  $("notice").textContent=(cflowError?"cflow 상태 조회 실패 · 마지막 조회 결과 표시":snapshot.error)||(snapshot.enabled?"관찰 중 · 세션별 순차 처리 · 최소 60초 간격":"관찰이 꺼져 있습니다. 시작하면 ds4-official/deepseek-flash API로 트랜스크립트를 전송합니다.");
  $("sort").hidden=mobile||grid;
  $("layout-hint").textContent=grid?`세션마다 최신 항목 최대 ${gridLimit}개 · 세션 목록은 필터 그대로 · 보고·승인 요청 기준`:mobile?"최신 보고부터 표시하는 타임라인":"세션 보드 · 내용은 자동 갱신되며 세션 순서는 최신순 정렬을 누를 때 바뀝니다.";
  cards.className=grid?"observer-grid":mobile?"observer-timeline":"observer-board";
  const horizontal=cards.scrollLeft;
  const scrolls=new Map([...cards.querySelectorAll(".observer-column-body")].map(e=>[e.parentElement.dataset.session,e.scrollTop]));
  cards.replaceChildren();
  if(mobile||grid) {
    const entries=[];
    for(const s of visible) {
      const events=(s.events||[]).filter(e=>!$("actions-only").checked||(e.needs_action&&!e.acknowledged));
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
      else card.append(node("p",s.summary||"아직 관찰 결과가 없습니다."),node("small",activity(s).duration));
      if(!metered.has(s.name)) {metered.add(s.name);const usage=usageBlock(s);if(usage)card.append(usage);}
      inputButton(card,s);cards.append(card);
    }
  } else {
    if(!boardOrder.length)boardOrder=[...snapshot.sessions].sort((a,b)=>latestActivity(b)-latestActivity(a)||a.name.localeCompare(b.name)).map(s=>s.name);
    for(const s of snapshot.sessions)if(!boardOrder.includes(s.name))boardOrder.push(s.name);
    visible.sort((a,b)=>boardOrder.indexOf(a.name)-boardOrder.indexOf(b.name));
    for(const s of visible) {
      const card=sessionHeader(s,"observer-column"), body=node("div","","observer-column-body");
      fillSession(body,s,$("actions-only").checked);
      card.append(body);inputButton(card,s);cards.append(card);body.scrollTop=scrolls.get(s.name)||0;
    }
  }
  if(!cards.children.length)cards.append(node("p","선택한 조건에 해당하는 세션이 없습니다."));
  cards.scrollLeft=horizontal;
  controls();
}
for(const mode of ["board","grid"]) $("layout-"+mode).onchange=()=>{layout=mode;saveView();render();};
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
