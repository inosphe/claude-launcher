/* Observer uses the main app router, authentication and viewport. */
globalThis.ObserverPage = (() => {
"use strict";
const $ = id => document.getElementById("observer-" + id);
let snapshot = {sessions: [], enabled: false}, pending = false, refreshTask = null, lastSnapshot = "";
const drafts = new Map(), answerDrafts = new Map();
let runs = [], gateCache = new Map(), cflowError = false;
let draftTarget = "";
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
        catch(err) {$("notice").textContent=err.message;} finally {retry.disabled=false;}
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
function render() {
  const scope=$("scope").value, selected=$("selection").value;
  const active=snapshot.sessions.filter(s=>($("ended").checked||s.running) && ($("activity").value==="all" || activity(s).kind===$("activity").value));
  const visible=active.filter(s=>scope==="global" || (scope==="session" ? s.name===selected : s.meshes.includes(selected)));
  const attention=s=> (s.events||[]).filter(e=>e.needs_action&&!e.acknowledged);
  $("counts").textContent=`${visible.length}개 세션 · 미확인 요청 ${visible.reduce((n,s)=>n+attention(s).length+sessionRuns(s).filter(sessCflowGated).length,0)}개`;
  $("monitor").textContent=snapshot.enabled?"관찰 끄기":"관찰 시작";
  $("notice").textContent=(cflowError?"cflow 상태 조회 실패 · 마지막 조회 결과 표시":snapshot.error) || (snapshot.enabled ? "관찰 중 · 세션별 순차 처리 · 최소 60초 간격" : "관찰이 꺼져 있습니다. 시작하면 ds4-official/deepseek-flash API로 트랜스크립트를 전송합니다.");
  const cards=$("cards"); cards.replaceChildren();
  visible.sort((a,b)=>(attention(b).length+sessionRuns(b).filter(sessCflowGated).length)-(attention(a).length+sessionRuns(a).filter(sessCflowGated).length));
  for(const s of visible) {
    if($("actions-only").checked&&!attention(s).length&&!sessionRuns(s).some(sessCflowGated)) continue;
    const card=node("article","","observer-card");
    const title=node("h2",s.name+" "); const active=activity(s); title.append(node("span",active.label,`state ${active.kind}`)); card.append(title);
    const links=node("nav", "", "observer-links");
    for (const [label, href] of [["관찰 결과", "#/observer/session/"], ["터미널", "#/s/"], ["트랜스크립트", "#/log/"]]) {
      const link=node("a",label); link.href=href+encodeURIComponent(s.name); links.append(link);
    }
    card.append(links,node("p",`${active.duration} · 하니스: ${s.status||"unknown"}`,"meta"));
    for(const run of sessionRuns(s).filter(r=>String(r.status).startsWith("waiting"))) addGate(card,s,run);
    card.append(node("div",s.meshes.join(" · ")||"메시 없음","meta"));
    card.append(node("p",s.summary||"아직 관찰 결과가 없습니다."));
    if(s.error)card.append(node("p",s.error));
    if(s.generated_at)card.append(node("div",`마지막 요약 ${new Date(s.generated_at).toLocaleString()}`,"meta"));
    const all = [...(s.events||[])].reverse();
    const events = $("actions-only").checked ? all.filter(e=>e.needs_action&&!e.acknowledged) : all;
    // Unanswered requests stay visible even when newer progress fills history.
    const urgent = events.filter(e=>e.needs_action&&!e.acknowledged);
    const ordinary = events.filter(e=>!urgent.includes(e));
    const shown = scope === "session" || $("actions-only").checked ? events : [...urgent,...ordinary.slice(0,20)];
    if(shown.length<events.length)card.append(node("p","미확인 요청과 최근 20개 결과 · 세션 보기에서 전체 이력 확인","meta"));
    for(const e of shown) {
      const item=node("div","",`event${e.needs_action&&!e.acknowledged?" action":""}`);
      item.append(node("small",`${e.origin==="agent"?"에이전트 직접 보고":"자동 관찰"} · ${e.kind} · ${new Date(e.at).toLocaleString()}${e.acknowledged?" · 확인됨":""}`),node("div",e.text));
      if(e.origin==="agent") {addDirect(item,s,e);card.append(item);continue;}
      const detail=document.createElement("details"), evidence=node("pre","불러오는 중…");
      detail.append(node("summary",`근거 · ${e.source}`),evidence);
      detail.ontoggle=async()=>{if(!detail.open||detail.dataset.loaded)return;try{const data=await request(`api/observer/${encodeURIComponent(s.name)}/events/${encodeURIComponent(e.id)}`);evidence.textContent=JSON.stringify(data,null,2);detail.dataset.loaded="1";}catch(err){evidence.textContent=err.message;}};
      item.append(detail);
      if(e.needs_action&&!e.acknowledged) {const b=node("button","확인 표시"); b.onclick=async()=>{try{await request(`api/observer/${encodeURIComponent(s.name)}/acknowledge`,{id:e.id});await refresh();}catch(err){$("notice").textContent=err.message;}};item.append(b);}
      card.append(item);
    }
    const more=document.createElement("details");more.append(node("summary","관찰 API 사용량"),node("pre",JSON.stringify({usage:s.usage,context_rotations:s.rotations},null,2)));card.append(more);
    const input=node("button","이 세션에 입력");input.disabled=!s.running;input.onclick=()=>{chooseTarget(s.name);$("prompt").focus();};card.append(input);
    cards.append(card);
  }
  if(!cards.children.length)cards.append(node("p","선택한 조건에 해당하는 세션이 없습니다."));
  controls();
}
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
        options($("target"),snapshot.sessions.filter(s=>s.running).map(s=>s.name),"세션 선택");
        render();
      }
    } catch(err) { $("notice").textContent=err.message; }
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
$("monitor").onclick=async()=>{try{await request("api/observer/settings",{enabled:!snapshot.enabled});await refresh();}catch(err){$("notice").textContent=err.message;}};
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
function stop() { clearInterval(poll); poll=null; generation++; }
async function open(scope = "global", name = "") {
  stop();
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
    chooseTarget(session?.running ? name : "");
  }
  render();
  poll=setInterval(()=>{if(!document.hidden)refresh();},10000);
}
return {open, stop};
})();
