/* The VM page (vm.html; daemon/vm_api.py). Every user action here is a call to
   a route that refuses a session as its caller; the page holds no state of its
   own beyond which VM is selected and the user's takeover timer. */
"use strict";
(() => {
const BASE = location.pathname.replace(/[^/]*$/, "");
const TOKEN_KEY = `claunch_token:${BASE}`;
const $ = (id) => document.getElementById(id);
const url = (path) => BASE + String(path).replace(/^\//, "");
const SCOPES = ["view", "control", "exec", "files"];

if (new URLSearchParams(location.search).get("embed")) document.body.classList.add("embed");
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
    sub.textContent = `${vm.state}${vm.locked ? " · 🔒" : ""}${vm.controller ? " · " + vm.controller : ""}`;
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
  $("vm-lock").hidden = !(hostLocked && vm.state === "suspended");
  const guarded = !!vm.locked;
  $("vm-guard-chip").hidden = !guarded;
  const note = $("vm-guard-note");
  note.hidden = !guarded;
  note.textContent = !guarded ? "" : vm.locked_but_running
    ? "이 VM은 잠겨 있지만 실행 중입니다(데몬이 자동으로 종료하지 않습니다). 세션은 사용할 수 없습니다. 종료하려면 '종료'를 누르십시오."
    : "이 VM은 잠겨 있어 시작할 수 없고 세션이 사용할 수 없습니다. 관리 작업이 필요하면 '잠금 해제'를 누르십시오.";
  const toggle = $("vm-guard-toggle");
  toggle.textContent = guarded ? "🔓 잠금 해제" : "🔒 VM 잠그기";
  toggle.disabled = !guarded && vm.state !== "stopped";
  toggle.title = !guarded && vm.state !== "stopped"
    ? "정지된 VM만 잠글 수 있습니다. 먼저 정상 종료하십시오." : toggle.title;
  $("vm-start").disabled = guarded || !(vm.state === "stopped" || (vm.state === "suspended" && !hostLocked));
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
  renderFiles(vm);
  renderNet(vm);
}
function renderNet(vm) {
  const pending = $("vm-net-pending");
  const listed = (vm.expose || []).length + (vm.allow || []).length > 0;
  pending.hidden = !(vm.net_pending);
  pending.textContent = vm.net_pending
    ? (vm.net_applied_known ? "저장된 설정이 실행 중인 VM과 다릅니다. VM을 종료한 뒤 다시 시작하면 적용됩니다."
      : (listed ? "실행 중인 VM이 이 설정으로 시작되었는지 알 수 없습니다(데몬이 재기동된 뒤 채택된 VM). 종료한 뒤 다시 시작하면 적용됩니다." : ""))
    : "";
  const ex = document.querySelector("#vm-expose tbody");
  ex.replaceChildren();
  for (const pair of vm.expose || []) {
    const [ext, inner] = pair.split(":");
    const tr = document.createElement("tr");
    tr.append(cell(ext), cell(inner));
    const td = document.createElement("td");
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = "삭제";
    b.addEventListener("click", () => act(`포트 ${ext} 노출 해제`,
      () => post(`api/vm/${encodeURIComponent(vm.id)}/expose`, { action: "rm", external: ext })));
    td.appendChild(b);
    tr.appendChild(td);
    ex.appendChild(tr);
  }
  const al = document.querySelector("#vm-allow tbody");
  al.replaceChildren();
  for (const cidr of vm.allow || []) {
    const tr = document.createElement("tr");
    tr.appendChild(cell(cidr));
    const td = document.createElement("td");
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = "삭제";
    b.addEventListener("click", () => act(`허용 ${cidr} 해제`,
      () => post(`api/vm/${encodeURIComponent(vm.id)}/allow`, { action: "rm", cidr })));
    td.appendChild(b);
    tr.appendChild(td);
    al.appendChild(tr);
  }
}
$("vm-expose-go").addEventListener("click", () => {
  const external = $("vm-expose-ext").value.trim();
  const internal = $("vm-expose-int").value.trim();
  if (!selected || !external || !internal) return;
  if (!confirm(`호스트의 ${external} 포트를 게스트의 ${internal} 포트로 연결합니다. 같은 LAN의 모든 기기에서 접속할 수 있게 됩니다. 계속합니까?`)) return;
  act(`포트 ${external}:${internal} 노출`, async () => {
    await post(`api/vm/${encodeURIComponent(selected)}/expose`, { action: "add", external, internal });
    $("vm-expose-ext").value = "";
    $("vm-expose-int").value = "";
  });
});
$("vm-allow-go").addEventListener("click", () => {
  const cidr = $("vm-allow-cidr").value.trim();
  if (!selected || !cidr) return;
  act(`허용 ${cidr}`, async () => {
    await post(`api/vm/${encodeURIComponent(selected)}/allow`, { action: "add", cidr });
    $("vm-allow-cidr").value = "";
  });
});
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
/* The file exchange: the two folders, the guest folder setting and the recent
   transfers. The route is the user's, so this is read from it rather than from
   the VM row (a session's row carries the folders but not the log). */
let filesFor = "";
function fmtTime(at) {
  const d = new Date(at * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
async function renderFiles(vm) {
  if (filesFor !== vm.id) delete $("vm-files-dir").dataset.dirty;
  filesFor = vm.id;
  let info;
  try { info = await api(`api/vm/${encodeURIComponent(vm.id)}/files`); } catch (_) { return; }
  if (filesFor !== vm.id) return;
  $("vm-files-host").textContent = info.host_dir;
  const input = $("vm-files-dir");
  if (document.activeElement !== input && !input.dataset.dirty) {
    input.value = info.guest_dir;
    input.dataset.default = info.guest_dir_default;
  }
  $("vm-files-default").disabled = info.guest_dir === info.guest_dir_default;
  const body = document.querySelector("#vm-files-log tbody");
  body.replaceChildren();
  for (const row of (info.recent || []).slice().reverse()) {
    const tr = document.createElement("tr");
    tr.append(cell(fmtTime(row.at)), cell(row.session || ""), cell(row.op), cell(row.path || ""),
              cell(row.ok && row.op !== "ls" && row.op !== "rm" ? `${row.bytes || 0} B` : ""),
              cell(row.ok ? "완료" : `거절: ${row.code || ""}`));
    body.appendChild(tr);
  }
  $("vm-files-log").hidden = !(info.recent || []).length;
}
$("vm-files-dir").addEventListener("input", () => { $("vm-files-dir").dataset.dirty = "1"; });
async function saveFilesDir(dir) {
  if (!selected) return;
  delete $("vm-files-dir").dataset.dirty;
  await act(dir ? "게스트 작업 폴더 저장" : "게스트 작업 폴더 기본값",
            () => post(`api/vm/${encodeURIComponent(selected)}/files_dir`, { dir }));
  $("vm-files-dir").blur();
}
$("vm-files-save").addEventListener("click", () => saveFilesDir($("vm-files-dir").value.trim()));
$("vm-files-default").addEventListener("click", () => saveFilesDir(""));
function render() {
  if (!vms.find((v) => v.id === selected)) selected = vms.length ? vms[0].id : "";
  renderList();
  renderDetail(vms.find((v) => v.id === selected) || null);
  syncPresetSave();
}
let hostLocked = false;
async function refresh() {
  try {
    const data = await api("api/vm");
    vms = data.vms || [];
    hostLocked = !!data.host_locked;
    $("vm-link").classList.add("up");
  } catch (_) { $("vm-link").classList.remove("up"); return; }
  render();
}

/* ---- sessions to grant to ---- */
async function loadSessions() {
  let rows = [];
  const sel = $("vm-grant-session");
  // The list is the way to pick a session; when it cannot be read the name is
  // typed instead.
  try { rows = (await api("api/webdebug/sessions")).sessions || []; } catch (_) {
    sel.hidden = true;
    $("vm-grant-session-text").hidden = false;
    return;
  }
  sel.hidden = false;
  $("vm-grant-session-text").hidden = true;
  const keep = sel.value;
  sel.replaceChildren(new Option("세션 선택", ""));
  for (const s of rows) sel.appendChild(new Option(`${s.name} (${s.status})`, s.name));
  sel.value = keep;
}
function grantScopes() {
  const scopes = ["view"];
  if ($("vm-grant-control").checked) scopes.push("control");
  if ($("vm-grant-exec").checked) scopes.push("exec");
  if ($("vm-grant-files").checked) scopes.push("files");
  return scopes;
}
$("vm-grant").addEventListener("click", () => {
  const session = ($("vm-grant-session").hidden ? $("vm-grant-session-text").value : $("vm-grant-session").value).trim();
  if (!session || !selected) return;
  const scopes = grantScopes();
  if (scopes.includes("exec") &&
      !confirm(`${session}에 exec를 줍니다. 게스트 셸 실행과 설치·설정 변경을 허용합니다. 계속합니까?`)) return;
  act(`${session} ${scopes.join("+")} 부여`,
      () => post(`api/vm/${encodeURIComponent(selected)}/grant`, { session, scopes }));
});

/* ---- registering a tart VM ---- */
let tartRows = [];
async function loadTart() {
  const note = $("vm-register-note");
  try {
    tartRows = (await api("api/vm-tart")).tart || [];
    note.hidden = true;
  } catch (e) {
    tartRows = [];
    note.hidden = false;
    note.textContent = /HTTP 404/.test(e.message)
      ? "이 데몬에는 tart 목록 API가 없습니다. 데몬을 재기동한 뒤 사용할 수 있습니다. 셸에서는 claunch vm add <id>로 등록합니다."
      : `tart 목록을 읽지 못했습니다: ${e.message}`;
  }
  const sel = $("vm-tart-pick");
  const keep = sel.value;
  sel.replaceChildren(new Option("tart VM 선택", ""));
  for (const row of tartRows) {
    const label = row.registered ? `${row.name} (${row.state}) — 등록됨: ${row.id}`
      : row.usable ? `${row.name} (${row.state})`
      : row.preset ? `${row.name} — 프리셋 복사본` : `${row.name} — 등록할 수 없는 이름`;
    const opt = new Option(label, row.name);
    opt.disabled = row.registered || !row.usable;
    sel.appendChild(opt);
  }
  sel.value = tartRows.some((r) => r.name === keep && !r.registered && r.usable) ? keep : "";
  syncRegister();
}
function syncRegister() {
  $("vm-register-go").disabled = !$("vm-tart-pick").value;
}
$("vm-tart-pick").addEventListener("change", () => {
  const id = $("vm-register-id");
  if (!id.value || id.dataset.auto === "1") { id.value = $("vm-tart-pick").value; id.dataset.auto = "1"; }
  syncRegister();
});
$("vm-register-id").addEventListener("input", () => { $("vm-register-id").dataset.auto = "0"; });
$("vm-register").addEventListener("toggle", () => { if ($("vm-register").open) loadTart(); });
$("vm-register-go").addEventListener("click", () => {
  const tart = $("vm-tart-pick").value;
  const id = ($("vm-register-id").value || tart).trim();
  if (!tart || !id) return;
  act(`${id} 등록`, async () => {
    await post("api/vm", { id, tart_name: tart });
    selected = id;
    // Adopt what tart already runs: the same step `claunch vm reconcile` is.
    await post("api/vm-reconcile");
    $("vm-register-id").value = "";
    await loadTart();
  });
});

/* ---- presets ---- */
let presetRows = [];
async function loadPresets() {
  const note = $("vm-preset-note");
  try {
    presetRows = (await api("api/vm-presets")).presets || [];
    note.hidden = true;
  } catch (e) {
    presetRows = [];
    note.hidden = false;
    note.textContent = /HTTP 404/.test(e.message)
      ? "이 데몬에는 프리셋 API가 없습니다. 데몬을 재기동한 뒤 사용할 수 있습니다."
      : `프리셋을 읽지 못했습니다: ${e.message}`;
  }
  renderPresets();
}
function renderPresets() {
  const ul = $("vm-preset-list");
  ul.replaceChildren();
  for (const p of presetRows) {
    const li = document.createElement("li");
    const head = document.createElement("strong");
    head.textContent = p.name;
    const sub = document.createElement("span");
    sub.className = "sub";
    const when = p.created_at ? new Date(p.created_at * 1000).toLocaleString("ko-KR") : "-";
    sub.textContent = `${p.description || "설명 없음"} · 원본 ${p.source || "-"} · ${when}`
      + (p.present === false ? " · tart 복사본 없음" : "");
    const make = document.createElement("button");
    make.type = "button";
    make.textContent = "새 VM";
    make.disabled = p.present === false;
    make.addEventListener("click", () => createFromPreset(p.name));
    const del = document.createElement("button");
    del.type = "button";
    del.className = "vm-danger";
    del.textContent = "삭제";
    del.addEventListener("click", () => deletePreset(p.name));
    li.append(head, sub, make, del);
    ul.appendChild(li);
  }
  if (!presetRows.length) {
    const li = document.createElement("li");
    li.className = "vm-muted";
    li.textContent = "프리셋이 없습니다.";
    ul.appendChild(li);
  }
  syncPresetSave();
}
function syncPresetSave() {
  const vm = vms.find((v) => v.id === selected);
  const ok = !!vm && vm.state === "stopped";
  $("vm-preset-save-go").disabled = !ok || !$("vm-preset-name").value.trim();
  $("vm-preset-save-note").textContent = !vm ? "VM을 선택하십시오."
    : ok ? `${vm.id}을(를) 복사합니다.` : `${vm.id}은(는) ${vm.state} 상태입니다. 먼저 정상 종료하십시오.`;
}
$("vm-preset-name").addEventListener("input", syncPresetSave);
$("vm-presets").addEventListener("toggle", () => { if ($("vm-presets").open) loadPresets(); });
$("vm-preset-save-go").addEventListener("click", () => {
  const vm = selected;
  const name = $("vm-preset-name").value.trim();
  if (!vm || !name) return;
  act(`${vm} 프리셋 저장`, async () => {
    await post("api/vm-presets", { vm, name, description: $("vm-preset-desc").value });
    $("vm-preset-name").value = "";
    $("vm-preset-desc").value = "";
    await loadPresets();
  });
});
function createFromPreset(name) {
  const id = $("vm-preset-newid").value.trim();
  if (!id) { say("새 VM 이름을 입력하십시오.", true); return; }
  act(`${name}에서 ${id} 만들기`, async () => {
    say(`${id}: 복제하고 시작하는 중입니다 (수십 초에서 수 분 걸립니다)`);
    const out = await post(`api/vm-presets/${encodeURIComponent(name)}/create`, { id });
    selected = id;
    $("vm-preset-newid").value = "";
    if (!out.started) say(`${id}을(를) 등록했지만 시작하지 못했습니다: ${out.why}`, true);
  });
}
function deletePreset(name) {
  if (!confirm(`프리셋 '${name}'과 그 tart 복사본(preset-${name})을 삭제합니다. 다른 VM은 지우지 않습니다. 계속합니까?`)) return;
  act(`프리셋 ${name} 삭제`, async () => {
    await post(`api/vm-presets/${encodeURIComponent(name)}/delete`, { confirm: name });
    await loadPresets();
  });
}

/* ---- actions ---- */
const lifecycle = (action, label, ask) => $(`vm-${action === "force_stop" ? "force" : action}`).addEventListener("click", () => {
  if (!selected || (ask && !confirm(ask))) return;
  act(label, () => post(`api/vm/${encodeURIComponent(selected)}/${action}`));
});
$("vm-guard-toggle").addEventListener("click", () => {
  const vm = vms.find((v) => v.id === selected);
  if (!vm) return;
  const op = vm.locked ? "unlock" : "lock";
  if (op === "unlock" && !confirm(`${vm.id}의 잠금을 해제합니다. 관리 작업이 끝나면 다시 잠그십시오. 계속합니까?`)) return;
  act(op === "lock" ? `${vm.id} 잠금` : `${vm.id} 잠금 해제`, () => post(`api/vm/${encodeURIComponent(vm.id)}/${op}`));
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
