/* The VM page (vm.html; daemon/vm_api.py). Every user action here is a call to
   a route that refuses a session as its caller; the page holds no state of its
   own beyond which VM is selected and the user's takeover timer. */
"use strict";
(() => {
const BASE = location.pathname.replace(/[^/]*$/, "");
const TOKEN_KEY = `claunch_token:${BASE}`;
const $ = (id) => document.getElementById(id);
const url = (path) => BASE + String(path).replace(/^\//, "");
const SCOPES = ["view", "control", "exec"];

let vms = [];
let selected = new URLSearchParams(location.search).get("vm") || "";
let takingOver = false;
let touchTimer = null;

/* ---- daemon API ---- */
async function tryStoredLogin() {
  let token = null;
  try { token = localStorage.getItem(TOKEN_KEY); } catch (_) { /* storage blocked */ }
  if (!token) return false;
  const resp = await fetch(url("api/auth/session"), {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }),
  });
  return resp.ok;
}
async function api(path, opts = {}) {
  let resp = await fetch(url(path), { credentials: "same-origin", ...opts });
  if (resp.status === 401 && await tryStoredLogin()) {
    resp = await fetch(url(path), { credentials: "same-origin", ...opts });
  }
  if (resp.status === 401) {
    say("로그인이 필요합니다. 대시보드에서 먼저 로그인한 뒤 이 페이지를 다시 여십시오.", true);
    throw new Error("unauthorized");
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}
const post = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
});

let sayTimer = null;
function say(text, bad) {
  const el = $("vm-activity");
  el.textContent = text;
  el.classList.toggle("error", !!bad);
  clearTimeout(sayTimer);
  if (!bad) sayTimer = setTimeout(() => { el.textContent = ""; }, 8000);
}
async function act(label, fn) {
  try { await fn(); say(label + " 완료"); } catch (e) { say(`${label} 실패: ${e.message}`, true); }
  await refresh();
}

/* ---- list and detail ---- */
function chipClass(vm) {
  if (vm.state === "fault") return "bad";
  if (vm.state === "running") return vm.ready ? "ok" : "warn";
  return "";
}
function renderList() {
  const nav = $("vm-list");
  nav.replaceChildren();
  for (const vm of vms) {
    const b = document.createElement("button");
    b.className = "vm-card";
    b.type = "button";
    if (vm.id === selected) b.setAttribute("aria-current", "true");
    const name = document.createElement("span");
    name.textContent = vm.id;
    const sub = document.createElement("span");
    sub.className = "sub";
    sub.textContent = `${vm.state}${vm.controller ? " · " + vm.controller : ""}`;
    b.append(name, sub);
    b.addEventListener("click", () => { selected = vm.id; showFrame(null); render(); startFrames(); });
    nav.appendChild(b);
  }
  $("vm-empty").hidden = vms.length > 0;
}
function cell(text) { const td = document.createElement("td"); td.textContent = text; return td; }
function renderDetail(vm) {
  $("vm-detail").hidden = !vm;
  if (!vm) return;
  $("vm-name").textContent = vm.id;
  const st = $("vm-state");
  st.textContent = vm.state;
  st.className = "vm-chip " + chipClass(vm);
  const ch = $("vm-channel");
  ch.textContent = `채널 ${vm.channel || "-"}`;
  ch.className = "vm-chip " + (vm.channel === "up" ? "ok" : vm.channel === "failed" ? "bad" : vm.channel === "idle" ? "" : "warn");
  $("vm-ip").textContent = vm.ip || "";
  const problem = $("vm-problem");
  const why = vm.fault_reason || (vm.channel === "failed" || vm.channel === "down" ? vm.channel_error : "");
  problem.hidden = !why;
  problem.textContent = why || "";
  const running = vm.state === "running";
  $("vm-start").disabled = !(vm.state === "stopped" || vm.state === "suspended");
  $("vm-start").textContent = vm.state === "suspended" ? "▶ 재개" : "▶ 시작";
  $("vm-suspend").disabled = !running;
  $("vm-stop").disabled = !(running || vm.state === "stopping" || vm.state === "starting");
  $("vm-force").disabled = vm.state === "stopped" || vm.state === "suspended";
  $("vm-controller").textContent = vm.controller || "없음";
  $("vm-revoke-control").disabled = !vm.controller;
  const driving = vm.human === "driving";
  takingOver = vms.some((v) => v.human === "driving");
  $("vm-takeover").hidden = driving;
  $("vm-return").hidden = !driving;
  $("vm-human").textContent = driving
    ? `조작 중입니다. 세션의 조작 호출은 거절됩니다${vm.draining ? " (진행 중인 호출이 끝나기를 기다리는 중)" : ""}.`
    : (vm.needs_observe && vm.controller ? "돌려받은 세션이 화면을 다시 확인할 때까지 조작을 시작하지 않습니다." : "");
  manageTouch();
  renderGrants(vm);
}
function renderGrants(vm) {
  const body = document.querySelector("#vm-grants tbody");
  body.replaceChildren();
  for (const [session, scopes] of Object.entries(vm.grants || {}).sort()) {
    const tr = document.createElement("tr");
    tr.appendChild(cell(session));
    for (const scope of SCOPES) {
      const td = document.createElement("td");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = scopes.includes(scope);
      box.setAttribute("aria-label", `${session} ${scope}`);
      box.addEventListener("change", () => {
        if (box.checked && scope === "exec" &&
            !confirm(`${session}에 exec를 줍니다. 게스트 셸 실행과 설치·설정 변경을 허용합니다. 계속합니까?`)) {
          box.checked = false;
          return;
        }
        act(`${session} ${scope} ${box.checked ? "부여" : "회수"}`, () => box.checked
          ? post(`api/vm/${encodeURIComponent(vm.id)}/grant`, { session, scopes: [scope] })
          : post(`api/vm/${encodeURIComponent(vm.id)}/revoke`, { session, scopes: [scope] }));
      });
      td.appendChild(box);
      tr.appendChild(td);
    }
    const del = document.createElement("td");
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = "모두 회수";
    b.addEventListener("click", () => act(`${session} 권한 회수`,
      () => post(`api/vm/${encodeURIComponent(vm.id)}/revoke`, { session })));
    del.appendChild(b);
    tr.appendChild(del);
    body.appendChild(tr);
  }
}
function render() {
  if (!vms.find((v) => v.id === selected)) selected = vms.length ? vms[0].id : "";
  renderList();
  renderDetail(vms.find((v) => v.id === selected) || null);
}
async function refresh() {
  try {
    vms = (await api("api/vm")).vms || [];
    $("vm-link").classList.add("up");
  } catch (_) { $("vm-link").classList.remove("up"); return; }
  render();
}

/* ---- sessions to grant to ---- */
async function loadSessions() {
  let rows = [];
  try { rows = (await api("api/webdebug/sessions")).sessions || []; } catch (_) { return; }
  const sel = $("vm-grant-session");
  const keep = sel.value;
  sel.replaceChildren(new Option("세션 선택", ""));
  for (const s of rows) sel.appendChild(new Option(`${s.name} (${s.status})`, s.name));
  sel.value = keep;
}
$("vm-grant").addEventListener("click", () => {
  const session = $("vm-grant-session").value;
  if (!session || !selected) return;
  act(`${session} view 부여`, () => post(`api/vm/${encodeURIComponent(selected)}/grant`, { session, scopes: ["view"] }));
});

/* ---- actions ---- */
const lifecycle = (action, label, ask) => $(`vm-${action === "force_stop" ? "force" : action}`).addEventListener("click", () => {
  if (!selected || (ask && !confirm(ask))) return;
  act(label, () => post(`api/vm/${encodeURIComponent(selected)}/${action}`));
});
lifecycle("start", "시작");
lifecycle("suspend", "일시 중단");
lifecycle("stop", "종료");
lifecycle("force_stop", "강제 종료",
  "강제 종료하면 게스트가 저장하지 않은 쓰기가 사라질 수 있습니다. 먼저 '종료'를 쓰십시오. 계속합니까?");
$("vm-reconcile").addEventListener("click", () => act("상태 맞추기", () => post("api/vm-reconcile")));
$("vm-revoke-control").addEventListener("click", () => selected &&
  act("조작권 회수", () => post(`api/vm/${encodeURIComponent(selected)}/revoke_controller`)));

/* ---- the user at the screen ---- */
function human(action) {
  return post(`api/vm/${encodeURIComponent(selected)}/human`, { action });
}
$("vm-takeover").addEventListener("click", () => selected && act("조작 시작", () => human("driving")));
$("vm-return").addEventListener("click", () => selected && act("세션에 돌려주기", () => human("idle")));
/* While the user is driving, this page says so every few seconds; the daemon
   hands the screen back by itself when that stops (daemon.vm_human_idle).
   The user drives in the host's tart window, so this tab is usually in the
   background: the beat does not look at tab visibility, and its clock is a
   Worker's, which a hidden tab's timer throttling does not slow down. */
const BEAT_MS = 5000;
let beatWorker = null;
function beat() {
  // The VMs the user is driving, not the one this page happens to show.
  for (const vm of vms) {
    if (vm.human === "driving") {
      post(`api/vm/${encodeURIComponent(vm.id)}/human`, { action: "touch" }).catch(() => {});
    }
  }
}
function startBeat() {
  try {
    const src = `setInterval(() => postMessage(0), ${BEAT_MS});`;
    beatWorker = new Worker(URL.createObjectURL(new Blob([src], { type: "text/javascript" })));
    beatWorker.onmessage = beat;
  } catch (_) {
    beatWorker = null;
    touchTimer = setInterval(beat, BEAT_MS);  // no Worker: the plain timer
  }
}
function stopBeat() {
  if (beatWorker) { beatWorker.terminate(); beatWorker = null; }
  if (touchTimer) { clearInterval(touchTimer); touchTimer = null; }
}
function manageTouch() {
  const beating = beatWorker !== null || touchTimer !== null;
  if (takingOver && !beating) startBeat();
  else if (!takingOver && beating) stopBeat();
}
document.addEventListener("visibilitychange", () => { if (takingOver && !document.hidden) beat(); });

/* ---- the screen ---- */
let frameRun = 0;
let frameUrl = "";
function showFrame(blob, note) {
  const img = $("vm-screen");
  if (frameUrl) { URL.revokeObjectURL(frameUrl); frameUrl = ""; }
  if (blob) {
    frameUrl = URL.createObjectURL(blob);
    img.src = frameUrl;
    img.hidden = false;
    $("vm-screen-note").hidden = true;
  } else {
    img.hidden = true;
    img.removeAttribute("src");
    $("vm-screen-note").hidden = false;
    $("vm-screen-note").textContent = note || "VM이 실행 중이면 화면이 여기에 표시됩니다.";
  }
}
async function startFrames() {
  const run = ++frameRun;
  let seq = 0;
  while (run === frameRun && selected) {
    if (document.hidden) { await new Promise((r) => setTimeout(r, 1000)); continue; }
    try {
      const resp = await fetch(url(`api/vm/${encodeURIComponent(selected)}/frame?since=${seq}`),
                               { credentials: "same-origin", cache: "no-store" });
      if (run !== frameRun) return;
      const got = parseInt(resp.headers.get("X-Frame-Seq") || "0", 10);
      if (resp.status === 200) {
        seq = got;
        showFrame(await resp.blob());
      } else if (resp.status === 304) {
        seq = got || seq;
      } else {
        const data = await resp.json().catch(() => ({}));
        seq = got || seq;
        showFrame(null, data.error || `화면을 받지 못했습니다 (HTTP ${resp.status})`);
        await new Promise((r) => setTimeout(r, 2000));
      }
    } catch (_) {
      await new Promise((r) => setTimeout(r, 2000));
    }
  }
}

refresh().then(() => { loadSessions(); startFrames(); });
setInterval(refresh, 2000);
setInterval(loadSessions, 10000);
})();
