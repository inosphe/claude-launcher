/* Observer uses the main app router, authentication and viewport. */
globalThis.ObserverPage = (() => {
"use strict";
const $ = id => document.getElementById("observer-" + id);
let snapshot = {sessions: [], enabled: false}, pending = false, refreshTask = null, lastSnapshot = "";
const drafts = new Map();
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
function render() {
  const scope=$("scope").value, selected=$("selection").value;
  const active=snapshot.sessions.filter(s=>$("ended").checked||s.running);
  const visible=active.filter(s=>scope==="global" || (scope==="session" ? s.name===selected : s.meshes.includes(selected)));
  const attention=s=> (s.events||[]).filter(e=>e.needs_action&&!e.acknowledged);
  $("counts").textContent=`${visible.length}개 세션 · 미확인 요청 ${visible.reduce((n,s)=>n+attention(s).length,0)}개`;
  $("monitor").textContent=snapshot.enabled?"관찰 끄기":"관찰 시작";
  $("notice").textContent=snapshot.error || (snapshot.enabled ? "관찰 중 · 세션별 순차 처리 · 최소 60초 간격" : "관찰이 꺼져 있습니다. 시작하면 ds4-official/deepseek-flash API로 트랜스크립트를 전송합니다.");
  const cards=$("cards"); cards.replaceChildren();
  visible.sort((a,b)=>attention(b).length-attention(a).length);
  for(const s of visible) {
    if($("actions-only").checked&&!attention(s).length) continue;
    const card=node("article","","observer-card");
    const title=node("h2",s.name+" "); title.append(node("span",s.status||"unknown","state")); card.append(title);
    const links=node("nav", "", "observer-links");
    for (const [label, href] of [["관찰 결과", "#/observer/session/"], ["터미널", "#/s/"], ["트랜스크립트", "#/log/"]]) {
      const link=node("a",label); link.href=href+encodeURIComponent(s.name); links.append(link);
    }
    card.append(links);
    card.append(node("div",s.meshes.join(" · ")||"메시 없음","meta"));
    card.append(node("p",s.summary||"아직 관찰 결과가 없습니다."));
    if(s.error)card.append(node("p",s.error));
    if(s.generated_at)card.append(node("div",`마지막 요약 ${new Date(s.generated_at).toLocaleString()}`,"meta"));
    const events = [...(s.events||[])].reverse();
    const eventLimit = scope === "session" || $("actions-only").checked ? 200 : 20;
    if(events.length>eventLimit)card.append(node("p",`최근 ${eventLimit}개 이벤트 · 세션 보기에서 전체 이력을 확인하십시오.`,"meta"));
    for(const e of events.slice(0,eventLimit)) {
      if($("actions-only").checked&&(!e.needs_action||e.acknowledged))continue;
      const item=node("div","",`event${e.needs_action&&!e.acknowledged?" action":""}`);
      item.append(node("small",`${e.kind} · ${new Date(e.at).toLocaleString()}${e.acknowledged?" · 확인됨":""}`),node("div",e.text));
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
      const data=await request("api/observer"), signature=JSON.stringify(data);
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
