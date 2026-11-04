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
// Session mode (vm?session=<name>, the dashboard's ▣ vm pane): the same page
// narrowed to one session's connections; it calls the same user-only routes.
const SESSION = new URLSearchParams(location.search).get("session") || "";
if (SESSION) document.body.classList.add("session-mode");
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
  if (!resp.ok) throw Object.assign(new Error(data.error || `HTTP ${resp.status}`), { code: data.code || "" });
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
    sub.textContent = `${vm.template ? "프리셋 템플릿" : "VM"} · ${vm.state}${vm.locked ? " · 🔒" : ""}${vm.controller ? " · " + vm.controller : ""}`;
    if (vm.template) b.classList.add("vm-template-card");
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
  $("vm-template-chip").hidden = !vm.template;
  const tnote = $("vm-template-note");
  tnote.hidden = !vm.template;
  tnote.textContent = vm.template
    ? `이 VM은 프리셋 ${vm.template}의 복사본(preset-${vm.template})입니다. 일반 VM이 아닙니다. 시작해서 쓰면 이후 이 프리셋으로 만드는 복제본이 달라집니다. 새 VM이 필요하면 프리셋 목록의 "새 VM"을 쓰십시오. 등록 해제(claunch vm rm)는 계속 할 수 있습니다.`
    : "";
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
  renderActions(vm, guarded);
  if (!SESSION) loadDestroyPlan(vm);
  $("vm-controller").textContent = vm.controller || "없음";
  $("vm-revoke-control").disabled = !vm.controller;
  const driving = vm.human === "driving";
  takingOver = vms.some((v) => v.human === "driving");
  $("vm-takeover").hidden = driving;
  $("vm-return").hidden = !driving;
  $("vm-human").textContent = driving
    ? `조작 중입니다. 세션의 조작 호출은 거절됩니다${vm.draining ? " (진행 중인 호출이 끝나기를 기다리는 중)" : ""}.`
    : (vm.needs_observe && vm.controller ? "돌려받은 세션이 화면을 다시 확인할 때까지 조작을 시작하지 않습니다." : "");
  if (!SESSION) manageTouch();
  renderGrants(vm);
  if (!SESSION) renderFiles(vm);
  renderShares(vm);
  renderNet(vm);
}
/* Lifecycle buttons. A start, stop, suspend or force stop takes seconds to minutes
   (the request returns when the guest has booted or tart reports the state), and
   the list is polled every 2 s, so a press has to show at once that it was taken.
   `busy` holds the press per VM from the click until the state it asked for shows
   in the list, the request fails, or BUSY_MS passes. While it is held every
   conflicting button of that VM is locked (force stop stays open except during a force stop,
   as the way out of a guest that does not answer). A VM the daemon reports
   in a transition (starting, stopping ...) shows the same progress without a
   press, so a page opened mid-transition does not invite a second one. */
const BUSY_MS = 120000;
const busyMs = () => Number(window.VM_BUSY_MS) || BUSY_MS;     // the page harness shortens it
const busy = {};
const BUSY_LABEL = { start: "시작 중…", resume: "재개 중…", suspend: "일시 중단 중…", stop: "종료 중…", force_stop: "강제 종료 중…" };
const BUSY_TARGET = { start: ["running"], suspend: ["suspended"], stop: ["stopped"], force_stop: ["stopped"] };
const PHASE_ACTION = { starting: "start", restoring: "resume", saving: "suspend", pausing: "suspend", stopping: "stop" };
function activeAction(vm) {
  const held = busy[vm.id];
  return held ? held.action : PHASE_ACTION[vm.state] || "";
}
/* Drop the presses whose state arrived or whose time ran out. */
function settleBusy() {
  for (const [id, held] of Object.entries(busy)) {
    const vm = vms.find((v) => v.id === id);
    const arrived = vm && (BUSY_TARGET[held.action] || []).includes(vm.state) && vm.state !== held.from;
    if (held.done && arrived) { delete busy[id]; continue; }
    if (Date.now() - held.since > busyMs()) {
      delete busy[id];
      say(`${id} ${held.label}: ${busyMs() / 1000}초 안에 상태가 바뀌지 않았습니다. 아직 반영되지 않았습니다. 상태를 확인하십시오`, true);
    }
  }
}
function renderActions(vm, guarded) {
  const running = vm.state === "running";
  const action = activeAction(vm);
  const working = !!action;
  const label = action === "start" && vm.state === "suspended" ? BUSY_LABEL.resume : BUSY_LABEL[action];
  const spin = (id, text) => {
    const b = $(id);
    b.classList.toggle("vm-working", !!text);
    b.setAttribute("aria-busy", text ? "true" : "false");
    if (text) b.textContent = text;
  };
  $("vm-start").disabled = working || guarded || !(vm.state === "stopped" || (vm.state === "suspended" && !hostLocked));
  $("vm-start").textContent = vm.state === "suspended" ? "▶ 재개" : "▶ 시작";
  $("vm-suspend").disabled = working || !running;
  $("vm-suspend").textContent = "⏸ 일시 중단 (선택)";
  // force stop stays open during any other press: the way out of a guest that does not answer
  $("vm-stop").disabled = working || !(running || vm.state === "stopping" || vm.state === "starting");
  $("vm-stop").textContent = "■ 종료";
  $("vm-force").disabled = action === "force_stop" || vm.state === "stopped" || vm.state === "suspended";
  $("vm-force").textContent = "⏻ 강제 종료";
  $("vm-guard-toggle").disabled = working || (!guarded && vm.state !== "stopped");
  for (const id of ["vm-start", "vm-suspend", "vm-stop", "vm-force"]) spin(id, "");
  const shown = { start: "vm-start", resume: "vm-start", suspend: "vm-suspend", stop: "vm-stop", force_stop: "vm-force" }[action];
  if (shown) spin(shown, label);
}
/* Unregister (the daemon forgets the VM; the tart VM stays) and delete for good
   (tart delete). The delete's impact -- size, what stands in the way, what it
   would leave behind -- is the daemon's own plan (GET /api/vm/{id}/destroy), read
   when a VM is shown and again when its state changes, never guessed here. */
let destroyPlan = null;
let destroyFor = "";
let destroyKey = "";
async function loadDestroyPlan(vm) {
  const key = `${vm.id}|${vm.state}|${vm.locked}|${Object.keys(vm.grants || {}).join(",")}|${vm.controller}`;
  if (key === destroyKey) { renderDestroy(vm); return; }
  destroyKey = key;
  if (destroyFor !== vm.id) { destroyPlan = null; $("vm-destroy-confirm").value = ""; }
  destroyFor = vm.id;
  try { destroyPlan = await api(`api/vm/${encodeURIComponent(vm.id)}/destroy`); }
  catch (e) { destroyPlan = { error: e.message }; }
  if (destroyFor === vm.id) renderDestroy(vm);
}
function renderDestroy(vm) {
  const plan = destroyPlan;
  const unreg = ["stopped", "suspended", "fault"].includes(vm.state);
  $("vm-unregister").disabled = !unreg || busy[vm.id] !== undefined;
  $("vm-unregister-why").textContent = unreg ? "" : `${vm.state} 상태에서는 해제할 수 없습니다. 먼저 종료하십시오.`;
  const blockers = $("vm-destroy-blockers"), warnings = $("vm-destroy-warnings"), size = $("vm-destroy-size");
  const fill = (ul, rows) => { ul.replaceChildren(...rows.map((t) => { const li = document.createElement("li"); li.textContent = t; return li; })); ul.hidden = !rows.length; };
  if (!plan || plan.error) {
    size.textContent = plan ? `삭제 정보를 읽지 못했습니다: ${plan.error}` : "삭제 정보를 읽는 중입니다…";
    fill(blockers, []); fill(warnings, []);
    $("vm-destroy").disabled = true;
    return;
  }
  size.textContent = plan.tart_present
    ? `tart VM ${plan.tart_name}: 디스크 사용량 ${plan.size_gb} GB(디스크 크기 ${plan.disk_gb} GB)가 삭제됩니다.`
    : `tart에 ${plan.tart_name}가 없습니다. 등록 기록만 지워집니다.`;
  fill(blockers, plan.blockers.map((b) => `삭제할 수 없음: ${b.reason}`));
  fill(warnings, plan.warnings);
  $("vm-destroy").disabled = plan.blockers.length > 0 || $("vm-destroy-confirm").value.trim() !== vm.id;
}
$("vm-destroy-confirm").addEventListener("input", () => {
  const vm = vms.find((v) => v.id === selected);
  if (vm) renderDestroy(vm);
});
$("vm-unregister").addEventListener("click", () => {
  const vm = vms.find((v) => v.id === selected);
  if (!vm) return;
  const gone = [
    `세션 권한 ${Object.keys(vm.grants || {}).length}건`,
    `공유 폴더 ${(vm.shared_dirs || []).length}개`,
    `대기 요청 ${(vm.requests || []).length}건`,
  ].join(", ");
  if (!confirm(`${vm.id}의 등록만 해제합니다. tart VM ${vm.tart_name}와 디스크 이미지는 호스트에 그대로 남습니다.\n해제되는 기록: ${gone}(공유 카탈로그의 이 VM 지정도 빠집니다).\n계속합니까?`)) return;
  act(`${vm.id} 등록 해제`, () => api(`api/vm/${encodeURIComponent(vm.id)}`, { method: "DELETE" }));
});
$("vm-destroy").addEventListener("click", () => {
  const vm = vms.find((v) => v.id === selected);
  if (!vm || $("vm-destroy-confirm").value.trim() !== vm.id) return;
  const id = vm.id;
  $("vm-destroy").disabled = true;
  act(`${id} 물리적 삭제`, async () => {
    await post(`api/vm/${encodeURIComponent(id)}/destroy`, { confirm: id });
    destroyKey = ""; destroyPlan = null;
    $("vm-destroy-confirm").value = "";
  });
});

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
/* One session at most holds control of a VM. These are the holders other than `except`. */
const controlHolders = (vm, except) => Object.entries((vm && vm.grants) || {})
  .filter(([s, sc]) => s !== except && sc.includes("control")).map(([s]) => s).sort();
/* The "steal" checkbox is the user's explicit wish to take control from its holder(s); it is
   sent only when checked, and shown only when it can matter. */
function showSteal(prefix, vmId, session, wantsControl) {
  const vm = vms.find((v) => v.id === vmId);
  const holders = wantsControl && vm && session && !(vm.grants[session] || []).includes("control")
    ? controlHolders(vm, session) : [];
  $(`${prefix}-steal-row`).hidden = !holders.length;
  $(`${prefix}-steal-label`).textContent = holders.length
    ? `${holders.join(", ")}에서 control을 빼앗아 부여` : "";
  if (!holders.length) $(`${prefix}-steal`).checked = false;
}
function refreshSteal() {
  showSteal("vm-grant", selected, ($("vm-grant-session").hidden ? $("vm-grant-session-text").value : $("vm-grant-session").value).trim(),
    $("vm-grant-control").checked);
  if (SESSION) showSteal("vm-sess", $("vm-sess-pick").value, SESSION, $("vm-sess-ex-control").checked);
}
function renderControlStatus(vm) {
  const el = $("vm-control-status");
  const holders = controlHolders(vm, "");
  el.hidden = holders.length < 2;
  el.classList.toggle("error", holders.length > 1);
  el.textContent = holders.length > 1
    ? `control 보유자 ${holders.length}명(${holders.join(", ")}) — 규칙 위반 상태: 한 VM의 control은 한 세션만 가질 수 있습니다. `
      + "자동으로 회수하지 않으며, 새 control 부여는 보유자 모두에게서 빼앗아야 허용됩니다."
    : "";
}
function renderGrants(vm) {
  renderControlStatus(vm);
  refreshSteal();
  const body = document.querySelector("#vm-grants tbody");
  body.replaceChildren();
  for (const [session, scopes] of Object.entries(vm.grants || {}).sort()) {
    if (SESSION && session !== SESSION) continue;
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
        const others = box.checked && scope === "control" ? controlHolders(vm, session) : [];
        if (others.length && !confirm(`${others.join(", ")}에서 control을 빼앗아 ${session}에 부여합니다. 계속합니까?`)) {
          box.checked = false;
          return;
        }
        act(`${session} ${scope} ${box.checked ? "부여" : "회수"}`, () => box.checked
          ? post(`api/vm/${encodeURIComponent(vm.id)}/grant`, { session, scopes: [scope], ...(others.length ? { steal: true } : {}) })
          : post(`api/vm/${encodeURIComponent(vm.id)}/revoke`, { session, scopes: [scope] }));
      });
      td.appendChild(box);
      tr.appendChild(td);
    }
    const onlyCell = document.createElement("td");
    const only = onlyRows.find((r) => r.session === session);
    const onlyBox = document.createElement("input");
    onlyBox.type = "checkbox";
    onlyBox.checked = !!(only && only.on);
    onlyBox.disabled = !only || (!only.on && !only.can_enable);
    onlyBox.setAttribute("aria-label", `${session} VM 전용`);
    onlyBox.title = only && !only.can_enable && !only.on ? only.why : "호스트에서 빌드·설치·실행을 거절하고 vm_exec로 안내합니다";
    onlyBox.addEventListener("change", () => toggleOnly(session, onlyBox.checked));
    onlyCell.appendChild(onlyBox);
    tr.appendChild(onlyCell);
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
/* VM-only mode: the switch is per session (not per VM); read from /api/vm-only. */
let onlyRows = [];
function toggleOnly(session, on) {
  act(`${session} VM 전용 ${on ? "켜기" : "끄기"}`, () => post("api/vm-only", { session, on }));
}
/* The clone lineage: presets and the VMs made from them, read only. A VM made
   from a preset carries {preset, preset_created_at}; a VM without a record, and
   one whose preset was deleted (or deleted and saved again under the same name:
   another created_at), is shown as such and never linked by guess. */
function lineageGraph(vmList, presets) {
  const nodes = new Map();
  const node = (key, kind, data) => { if (!nodes.has(key)) nodes.set(key, { key, kind, data, children: [], parent: null }); return nodes.get(key); };
  for (const v of vmList) node(`vm:${v.id}`, "vm", v);
  for (const p of presets) node(`preset:${p.name}`, "preset", p);
  for (const p of presets) {
    const me = nodes.get(`preset:${p.name}`);
    if (!p.source) { me.parent = null; continue; }
    me.parent = nodes.get(`vm:${p.source}`) || node(`gone-vm:${p.source}`, "gone-vm", { id: p.source });
  }
  for (const v of vmList) {
    const me = nodes.get(`vm:${v.id}`);
    const c = v.cloned_from;
    if (!c || c.kind !== "preset" || !c.preset) continue;
    const p = presets.find((x) => x.name === c.preset);
    const same = p && (!c.preset_created_at || !p.created_at || c.preset_created_at === p.created_at);
    me.parent = same ? nodes.get(`preset:${p.name}`)
      : node(`gone-preset:${c.preset}:${c.preset_created_at || 0}`, "gone-preset", { name: c.preset });
  }
  // a link that would close a loop is dropped, so every node is reachable from a root
  for (const n of nodes.values()) {
    const seen = new Set([n]);
    for (let up = n.parent; up; up = up.parent) {
      if (seen.has(up)) { n.parent = null; break; }
      seen.add(up);
    }
  }
  const roots = [];
  for (const n of nodes.values()) (n.parent ? n.parent.children : roots).push(n);
  const order = { vm: 0, preset: 1, "gone-vm": 2, "gone-preset": 3 };
  const byName = (a, b) => (order[a.kind] - order[b.kind]) || a.key.localeCompare(b.key);
  for (const n of nodes.values()) n.children.sort(byName);
  return { roots: roots.sort(byName), nodes };
}
const buildLineage = (vmList, presets) => lineageGraph(vmList, presets).roots;
/* One line for the selected VM, read off the same graph: "t01 ← 프리셋 base ← mac-01".
   Where the records stop it says why (no record, preset deleted, source unregistered)
   and never fills the gap. A registered copy of a preset is named as such. */
function lineageChain(vmList, presets, vmId) {
  const { nodes } = lineageGraph(vmList, presets);
  const me = nodes.get(`vm:${vmId}`);
  if (!me) return "";
  const parts = [vmId];
  let up = me.parent;
  let last = null;
  if (me.data.template) {
    const pn = nodes.get(`preset:${me.data.template}`);
    parts[0] = `${vmId}(프리셋 ${me.data.template}의 복사본: 프리셋 템플릿, 일반 VM이 아님)`;
    up = pn ? pn.parent : null;
    last = pn || null;
    if (!pn) parts.push("그 프리셋의 기록이 없음");
  }
  for (let n = up; n; n = n.parent) {
    last = n;
    if (n.kind === "vm") parts.push(n.data.id);
    else if (n.kind === "preset") parts.push(`프리셋 ${n.data.name}`);
    else if (n.kind === "gone-vm") parts.push(`원본 VM ${n.data.id}(등록 해제됨)`);
    else parts.push(`삭제된 프리셋 ${n.data.name}(이 프리셋 이전의 계보는 알 수 없음)`);
  }
  let text;
  if (parts.length === 1 && !last) {
    text = `${vmId}: 출처 기록 없음(직접 등록했거나 이 기능 이전에 만든 VM, 추측하지 않음)`;
  } else {
    text = parts.join(" ← ");
    if (last && last.kind === "vm" && !last.parent) text += "(출처 기록 없음)";
    if (last && last.kind === "preset" && !last.parent) text += "(원본 기록 없음)";
  }
  const made = me.children.filter((c) => c.kind === "preset")
    .map((p) => `${p.data.name}${p.children.length ? ` → ${p.children.map((k) => k.data.id).join(", ")}` : ""}`);
  if (made.length) text += ` · 이 VM에서 만든 프리셋: ${made.join("; ")}`;
  return text;
}
const LIFE_TEXT = { running: "실행 중", stopped: "정지", suspended: "일시 중단", starting: "시작 중", stopping: "종료 중",
  saving: "일시 중단 중", restoring: "재개 중", pausing: "일시 정지 중", paused: "일시 정지", fault: "오류" };
function lineageLabel(n, isRoot) {
  const d = n.data;
  if (n.kind === "vm") {
    const bits = [`VM ${d.id}`, LIFE_TEXT[d.state] || d.state];
    if (d.locked) bits.push("🔒 잠김");
    if (d.template) bits.push("프리셋 템플릿");
    if (isRoot) bits.push(d.cloned_from && d.cloned_from.kind ? "" : "출처 기록 없음");
    return bits.filter(Boolean).join(" · ");
  }
  if (n.kind === "preset") {
    const when = d.created_at ? new Date(d.created_at * 1000).toLocaleString("ko-KR") : "-";
    const full = presetRows.find((x) => x.name === d.name);
    return [`프리셋 ${d.name}`, `저장 ${when}`, `원본 ${d.source || "기록 없음"}`, d.description || "",
      full && full.present === false ? "tart 복사본 없음" : ""].filter(Boolean).join(" · ");
  }
  if (n.kind === "gone-vm") return `원본 VM ${d.id} (등록 해제됨)`;
  return `삭제된 프리셋 ${d.name}에서 복제`;
}
function renderLineage() {
  const tree = $("vm-lineage-tree");
  tree.replaceChildren();
  const graph = lineageGraph(vms, presetsLight);
  const roots = graph.roots;
  $("vm-lineage-empty").hidden = roots.length > 0;
  const line = vms.some((v) => v.id === selected) ? lineageChain(vms, presetsLight, selected) : "";
  for (const id of ["vm-lineage-line", "vm-lineage-current"]) {
    $(id).textContent = line ? (id === "vm-lineage-line" ? `계보: ${line}` : line) : "";
    $(id).hidden = !line;
  }
  const mine = graph.nodes.get(`vm:${selected}`);
  const ancestors = new Set();
  for (let n = mine && mine.parent; n; n = n.parent) ancestors.add(n);
  let selectedLi = null;
  const draw = (n, host, isRoot) => {
    const li = document.createElement("li");
    li.className = `vm-tree-${n.kind}`;
    const text = document.createElement("span");
    text.textContent = lineageLabel(n, isRoot);
    li.append(text);
    if (n === mine) {
      li.classList.add("vm-tree-selected");
      const tag = document.createElement("b");
      tag.className = "vm-tree-here";
      tag.textContent = " ◀ 현재 선택";
      text.append(tag);
      selectedLi = li;
    } else if (ancestors.has(n)) {
      li.classList.add("vm-tree-ancestor");
    }
    if (n.children.length) {
      const ul = document.createElement("ul");
      for (const child of n.children) draw(child, ul, false);
      li.append(ul);
    }
    host.append(li);
  };
  for (const r of roots) draw(r, tree, true);
  if ($("vm-lineage-all").open) showSelectedInTree(selectedLi);
  tree._selected = selectedLi;
}
/* A long tree keeps the selected VM in view when it is open. */
function showSelectedInTree(li) {
  try { if (li && li.scrollIntoView) li.scrollIntoView({ block: "nearest" }); } catch (_) { /* no layout */ }
}
$("vm-lineage-all").addEventListener("toggle", () => {
  if ($("vm-lineage-all").open) showSelectedInTree($("vm-lineage-tree")._selected);
});
function renderOnly() {
  const body = document.querySelector("#vm-only-rows tbody");
  body.replaceChildren();
  for (const r of onlyRows) {
    const tr = document.createElement("tr");
    tr.appendChild(cell(r.session));
    const td = document.createElement("td");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = !!r.on;
    box.disabled = !r.on && !r.can_enable;
    box.setAttribute("aria-label", `${r.session} VM 전용 켜기`);
    box.addEventListener("change", () => toggleOnly(r.session, box.checked));
    td.appendChild(box);
    tr.appendChild(td);
    const blocks = r.blocks || [];
    tr.append(cell(r.on ? `켜짐 (${fmtTime(r.since)}부터)` : (r.can_enable ? "꺼짐" : `켤 수 없음: ${r.why}`)),
              cell(blocks.length
                ? blocks.slice(0, 3).map((b) => `${fmtTime(b.at)} ${b.kind}(${b.rule}) ${b.head}`).join(" / ")
                : "없음"));
    body.appendChild(tr);
  }
  $("vm-only-rows").hidden = !onlyRows.length;
  $("vm-only-empty").hidden = onlyRows.length > 0;
}
/* Shared host folders (tart run --dir) and the requests sessions made for them. */
function renderShares(vm) {
  const roots = $("vm-share-roots");
  roots.textContent = shareRoots.length
    ? `공유를 허용한 루트: ${shareRoots.join(", ")} (이 아래의 폴더만 공유할 수 있고, 홈 폴더 자체와 ~/.ssh, ~/Library, ~/.claude*, 데몬 상태 폴더는 포함할 수 없습니다.)`
    : "공유가 꺼져 있습니다. 공유 폴더 영역의 \"공유 허용 루트\"에 폴더를 추가하면 켜집니다.";
  $("vm-share-go").disabled = !shareRoots.length;
  const pending = $("vm-share-pending");
  const skipped = Object.entries(vm.shares_skipped || {});
  pending.hidden = !(vm.shares_pending || skipped.length);
  pending.textContent = [
    vm.shares_pending ? "공유 폴더 변경은 VM을 다시 시작하면 적용됩니다." : "",
    ...skipped.map(([n, why]) => `마지막 시작에서 제외된 공유 ${n}: ${why}`),
  ].filter(Boolean).join(" ");
  const body = document.querySelector("#vm-shares tbody");
  body.replaceChildren();
  const rows = (vm.share_rows || []).filter((r) => r.host_path);
  for (const sh of rows) {
    const tr = document.createElement("tr");
    tr.append(cell(sh.name), cell(sh.host_path), cell(sh.guest_path), cell(sh.mode), cell(originText(sh.origin)),
              cell(stateText(sh)));
    const td = document.createElement("td");
    if (sh.origin && (sh.origin.kind === "vm" || sh.origin.kind === "request")) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = "삭제";
      b.addEventListener("click", () => act(`공유 ${sh.name} 삭제`,
        () => post(`api/vm/${encodeURIComponent(vm.id)}/share`, { action: "rm", name: sh.name })));
      td.appendChild(b);
    } else {
      td.textContent = "카탈로그에서 변경";
    }
    tr.appendChild(td);
    body.appendChild(tr);
  }
  $("vm-shares").hidden = !rows.length;
}
const STATE_TEXT = { applied: "적용됨", pending: "적용 대기", left_out: "제외" };
function stateText(sh) { return (STATE_TEXT[sh.state] || sh.state || "") + (sh.why ? `: ${sh.why}` : ""); }
function originText(o) {
  if (!o) return "";
  const kind = { vm: "VM 설정", request: "세션 요청", "catalog-all": "카탈로그(전체)", "catalog-vms": "카탈로그(지정)" }[o.kind] || o.kind;
  return o.by_session ? `${kind}: ${o.by_session}` : kind;
}
$("vm-share-go").addEventListener("click", () => {
  const name = $("vm-share-name").value.trim();
  const host_path = $("vm-share-path").value.trim();
  const mode = $("vm-share-rw").checked ? "rw" : "ro";
  if (!selected || !name || !host_path) return;
  if (mode === "rw" && !confirm(`${host_path}를 쓰기 가능(rw)으로 공유합니다. 게스트에서 이 폴더의 파일을 바꾸거나 지울 수 있습니다. 계속합니까?`)) return;
  act(`공유 ${name} 추가`, async () => {
    await post(`api/vm/${encodeURIComponent(selected)}/share`, { action: "add", name, host_path, mode });
    $("vm-share-name").value = "";
    $("vm-share-path").value = "";
    $("vm-share-rw").checked = false;
  });
});
/* The share catalog and the VM x share matrix: page-level, shown whatever VM is selected. */
let shareCatalog = [];
let catEditing = "";
function renderRoots() {
  const body = document.querySelector("#vm-roots tbody");
  body.replaceChildren();
  for (const r of shareRootRows) {
    const tr = document.createElement("tr");
    const path = document.createElement("td");
    path.textContent = r.path + (r.real && r.real !== r.path ? ` → ${r.real}` : "");
    const state = document.createElement("td");
    state.textContent = r.ok ? "적용 중" : `적용 안 됨: ${r.reason}`;
    if (!r.ok) state.className = "vm-warn-text";
    const act_ = document.createElement("td");
    const del = document.createElement("button");
    del.type = "button";
    del.textContent = "삭제";
    del.addEventListener("click", () => act(`공유 루트 ${r.path} 삭제`, () => post("api/vm-share-roots", { action: "rm", path: r.path })));
    act_.append(del);
    tr.append(path, state, act_);
    body.append(tr);
  }
  $("vm-roots").hidden = !shareRootRows.length;
}
$("vm-root-go").addEventListener("click", () => {
  const path = $("vm-root-path").value.trim();
  if (!path) return;
  act(`공유 루트 ${path} 추가`, async () => {
    await post("api/vm-share-roots", { action: "add", path });
    $("vm-root-path").value = "";
  });
});
function renderSharing() {
  renderRoots();
  $("vm-cat-roots").textContent = shareRoots.length
    ? `공유를 허용한 루트: ${shareRoots.join(", ")} (이 아래의 폴더만 공유할 수 있고, 홈 폴더 자체와 ~/.ssh, ~/Library, ~/.claude*, 데몬 상태 폴더는 포함할 수 없습니다.)`
    : "공유가 꺼져 있습니다. 공유 폴더 영역의 \"공유 허용 루트\"에 폴더를 추가하면 켜집니다.";
  $("vm-cat-go").disabled = !shareRoots.length;
  const waiting = vms.filter((v) => v.shares_pending).map((v) => v.id);
  const pend = $("vm-cat-pending");
  pend.hidden = !waiting.length;
  pend.textContent = waiting.length ? `공유 폴더 변경이 적용되려면 다시 시작해야 하는 VM: ${waiting.join(", ")}` : "";
  const body = document.querySelector("#vm-catalog tbody");
  body.replaceChildren();
  for (const e of shareCatalog) {
    const tr = document.createElement("tr");
    tr.append(cell(e.name), cell(e.host_path), cell(e.mode),
              cell(e.scope === "all" ? "모든 VM" : `VM: ${(e.vms || []).join(", ")}`), cell(originText(e.origin)));
    const td = document.createElement("td");
    const edit = document.createElement("button");
    edit.type = "button";
    edit.textContent = "수정";
    edit.addEventListener("click", () => startCatalogEdit(e));
    const del = document.createElement("button");
    del.type = "button";
    del.textContent = "삭제";
    del.addEventListener("click", () => act(`카탈로그 ${e.name} 삭제`, () => post("api/vm-share-catalog", { action: "rm", name: e.name })));
    td.append(edit, del);
    tr.appendChild(td);
    body.appendChild(tr);
  }
  $("vm-catalog").hidden = !shareCatalog.length;
  $("vm-catalog-empty").hidden = shareCatalog.length > 0;
  renderCatalogVms();
  renderMatrix();
}
function renderCatalogVms() {
  const box = $("vm-cat-vms");
  const keep = new Set([...box.querySelectorAll("input:checked")].map((i) => i.value));
  box.replaceChildren();
  for (const v of vms) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = v.id;
    input.checked = keep.has(v.id);
    label.append(input, " " + v.id);
    box.appendChild(label);
  }
  box.hidden = !$("vm-cat-scope-vms").checked;
}
for (const id of ["vm-cat-scope-all", "vm-cat-scope-vms"]) {
  $(id).addEventListener("change", () => { $("vm-cat-vms").hidden = !$("vm-cat-scope-vms").checked; });
}
function startCatalogEdit(e) {
  catEditing = e.name;
  $("vm-cat-name").value = e.name;
  $("vm-cat-name").disabled = true;
  $("vm-cat-path").value = e.host_path;
  $("vm-cat-rw").checked = e.mode === "rw";
  $("vm-cat-scope-all").checked = e.scope === "all";
  $("vm-cat-scope-vms").checked = e.scope === "vms";
  renderCatalogVms();
  for (const i of $("vm-cat-vms").querySelectorAll("input")) i.checked = (e.vms || []).includes(i.value);
  $("vm-cat-go").textContent = "저장";
  $("vm-cat-cancel").hidden = false;
}
function endCatalogEdit() {
  catEditing = "";
  $("vm-cat-name").value = "";
  $("vm-cat-name").disabled = false;
  $("vm-cat-path").value = "";
  $("vm-cat-rw").checked = false;
  $("vm-cat-scope-all").checked = true;
  for (const i of $("vm-cat-vms").querySelectorAll("input")) i.checked = false;
  $("vm-cat-vms").hidden = true;
  $("vm-cat-go").textContent = "추가";
  $("vm-cat-cancel").hidden = true;
}
$("vm-cat-cancel").addEventListener("click", endCatalogEdit);
$("vm-cat-go").addEventListener("click", () => {
  const name = $("vm-cat-name").value.trim();
  const host_path = $("vm-cat-path").value.trim();
  const mode = $("vm-cat-rw").checked ? "rw" : "ro";
  const scope = $("vm-cat-scope-vms").checked ? "vms" : "all";
  const picked = [...$("vm-cat-vms").querySelectorAll("input:checked")].map((i) => i.value);
  if (!name || !host_path) return;
  if (scope === "vms" && !picked.length) { say("지정할 VM을 하나 이상 고르십시오", true); return; }
  let confirm_rw = false;
  if (mode === "rw") {
    const where = scope === "all" ? "모든 VM" : `VM ${picked.join(", ")}`;
    if (!confirm(`${host_path}를 ${where}에 쓰기 가능(rw)으로 공유합니다. 게스트가 이 폴더의 파일을 바꾸거나 지울 수 있고, .git/hooks나 스크립트를 심어 호스트 세션이 실행하게 할 수 있습니다. 계속합니까?`)) return;
    confirm_rw = true;
  }
  const replace = !!catEditing;
  act(`카탈로그 ${name} ${replace ? "저장" : "추가"}`, async () => {
    await post("api/vm-share-catalog", { action: "put", name, host_path, mode, scope, vms: picked, confirm_rw, replace });
    endCatalogEdit();
  });
});
function renderMatrix() {
  const names = [];
  for (const v of vms) for (const r of v.share_rows || []) if (!names.includes(r.name)) names.push(r.name);
  for (const e of shareCatalog) if (!names.includes(e.name)) names.push(e.name);
  const head = document.querySelector("#vm-matrix thead");
  const body = document.querySelector("#vm-matrix tbody");
  head.replaceChildren();
  body.replaceChildren();
  $("vm-matrix-empty").hidden = names.length > 0 && vms.length > 0;
  $("vm-matrix").hidden = !(names.length && vms.length);
  const hr = document.createElement("tr");
  const corner = document.createElement("th");
  corner.textContent = "VM";
  hr.appendChild(corner);
  for (const n of names) { const th = document.createElement("th"); th.textContent = n; hr.appendChild(th); }
  head.appendChild(hr);
  for (const v of vms) {
    const tr = document.createElement("tr");
    const th = document.createElement("th");
    th.textContent = v.id;
    tr.appendChild(th);
    for (const n of names) {
      const r = (v.share_rows || []).find((x) => x.name === n);
      const td = cell(r ? `${r.mode || ""} ${STATE_TEXT[r.state] || r.state}${r.why ? ` — ${r.why}` : ""}`.trim() : "-");
      if (r) {
        td.className = "state-" + r.state;
        td.title = `${r.host_path || ""} (${originText(r.origin)})`;
      }
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
}

function renderRequests() {
  const rows = [];
  for (const v of vms) for (const r of v.requests || []) rows.push([v, r]);
  $("vm-requests-box").hidden = !rows.length;
  $("vm-requests-title").textContent = `대기 중인 요청 ${rows.length}건`;
  const host = $("vm-requests");
  host.replaceChildren();
  const line = (label, text, cls) => {
    const p = document.createElement("p");
    if (cls) p.className = cls;
    const b = document.createElement("b");
    b.textContent = label;
    p.append(b, " ", document.createTextNode(text));
    return p;
  };
  for (const [vm, r] of rows) {
    const card = document.createElement("article");
    card.className = "vm-request";
    const head = document.createElement("h3");
    head.textContent = `${r.session} → VM ${vm.id}`;
    card.append(head, line("권한", (r.scopes || []).join(" + ") || "-"));
    const shares = r.shares || [];
    card.append(line("공유 폴더", shares.map((s) => `${s.name} (${s.mode}) ${s.host_path}`).join("; ") || "-"));
    if ((r.scopes || []).includes("exec"))
      card.append(line("주의", "exec은 게스트에서 셸 실행, 설치, 설정 변경을 허용합니다.", "vm-warn-text"));
    const rw = shares.filter((s) => s.mode === "rw").map((s) => s.name);
    if (rw.length)
      card.append(line("주의", `쓰기 가능(rw) 공유: ${rw.join(", ")}. 게스트가 .git/hooks나 스크립트를 심어 호스트 세션이 실행하게 할 수 있습니다.`, "vm-warn-text"));
    card.append(line("사유", r.reason || "-"));
    const ok = document.createElement("button");
    ok.type = "button";
    ok.textContent = "승인";
    ok.addEventListener("click", () => {
      const risky = [];
      if ((r.scopes || []).includes("exec")) risky.push("exec(게스트 셸 실행과 설치·설정 변경)");
      for (const s of shares) if (s.mode === "rw") risky.push(`${s.name} 쓰기 가능(rw) 공유`);
      if (risky.length && !confirm(`${r.session}에 ${risky.join(", ")}를 줍니다. 계속합니까?`)) return;
      act(`${r.session} 요청 승인`, () => post(`api/vm/${encodeURIComponent(vm.id)}/approve`, { session: r.session, id: r.id }));
    });
    const no = document.createElement("button");
    no.type = "button";
    no.textContent = "거절";
    no.addEventListener("click", () => act(`${r.session} 요청 거절`,
      () => post(`api/vm/${encodeURIComponent(vm.id)}/deny`, { session: r.session, id: r.id })));
    const row = document.createElement("div");
    row.className = "vm-row";
    row.append(ok, no);
    card.append(row);
    host.append(card);
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
  settleBusy();
  const pool = SESSION ? connectedVms() : vms;
  if (!pool.find((v) => v.id === selected)) selected = pool.length ? pool[0].id : "";
  renderList();
  renderDetail(pool.find((v) => v.id === selected) || null);
  if (SESSION) renderSess();
  renderOnly();
  renderLineage();
  renderSharing();
  renderRequests();
  syncPresetSave();
}
let hostLocked = false;
let presetsLight = [];
let shareRoots = [];
let shareRootRows = [];
async function refresh() {
  try {
    const data = await api("api/vm");
    vms = data.vms || [];
    hostLocked = !!data.host_locked;
    shareRoots = data.share_roots || [];
    presetsLight = data.presets || [];
    shareRootRows = data.share_root_rows || [];
    shareCatalog = data.share_catalog || [];
    try { onlyRows = (await api("api/vm-only")).sessions || []; } catch (_) { onlyRows = []; }
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
/* The one grant path: the permissions table's "권한 부여" and the session pane's
   "연결" both end here, so the template-copy and exec confirmations are one piece. */
function grantTo(session, vmId, scopes, steal) {
  const target = vms.find((v) => v.id === vmId);
  if (target && target.template &&
      !confirm(`${vmId}는 프리셋 ${target.template}의 복사본입니다. ${session}이 이 VM을 바꾸면 이후 그 프리셋으로 만드는 복제본이 바뀝니다. 그래도 부여합니까?`)) return null;
  if (scopes.includes("exec") &&
      !confirm(`${session}에 exec를 줍니다. 게스트 셸 실행과 설치·설정 변경을 허용합니다. 계속합니까?`)) return null;
  return act(`${session} ${scopes.join("+")} 부여`,
      () => post(`api/vm/${encodeURIComponent(vmId)}/grant`, { session, scopes, ...(steal ? { steal: true } : {}) }));
}
$("vm-grant").addEventListener("click", () => {
  const session = ($("vm-grant-session").hidden ? $("vm-grant-session-text").value : $("vm-grant-session").value).trim();
  if (!session || !selected) return;
  grantTo(session, selected, grantScopes(), !$("vm-grant-steal-row").hidden && $("vm-grant-steal").checked);
});
for (const id of ["vm-grant-control", "vm-grant-session", "vm-grant-session-text"])
  $(id).addEventListener("input", refreshSteal);

/* ---- session mode: the connected VMs, one-click new VM, connecting an existing one ---- */
const connectedVms = () => vms.filter((v) => v.grants && v.grants[SESSION]);
let sessPresets = { presets: [], default: "", default_lost: "", error: "" };
let sessNewBusy = false;
async function loadSessPresets() {
  try {
    const out = await api("api/vm-presets");
    sessPresets = { presets: out.presets || [], default: out.default || "", default_lost: out.default_lost || "", error: "" };
  } catch (e) {
    sessPresets = { presets: [], default: "", default_lost: "", error: e.message };
  }
  if (SESSION) renderSess();
}
const autoVmName = () => {
  const taken = new Set(vms.flatMap((v) => [v.id, v.tart_name]));
  const base = (SESSION.replace(/[^A-Za-z0-9_.-]/g, "-").replace(/^[-.]+/, "") || "vm").slice(0, 50) + "-vm";
  if (!taken.has(base)) return base;
  for (let n = 2; ; n++) if (!taken.has(`${base}-${n}`)) return `${base}-${n}`;
};
function sessPresetChoice() {
  const usable = (name) => sessPresets.presets.find((p) => p.name === name && p.present !== false);
  return sessPresets.default && usable(sessPresets.default) ? sessPresets.default : $("vm-sess-new-pick").value;
}
/* A select is rebuilt only when its choices changed: the page re-renders every 2 s,
   and rebuilding an open dropdown closes it. */
function setChoices(sel, first, rows) {
  const key = JSON.stringify([first, rows]);
  if (sel.dataset.key === key) return;
  const keep = sel.value;
  sel.dataset.key = key;
  sel.replaceChildren(new Option(first, ""));
  for (const [value, label, disabled] of rows) { const o = new Option(label, value); o.disabled = !!disabled; sel.appendChild(o); }
  sel.value = rows.some(([v, , d]) => v === keep && !d) ? keep : "";
}
function renderSess() {
  $("vm-sess").hidden = false;
  $("vm-sess-title").textContent = `세션 ${SESSION}의 VM`;
  const list = $("vm-sess-list");
  list.replaceChildren();
  const mine = connectedVms();
  $("vm-sess-empty").hidden = mine.length > 0;
  for (const vm of mine) {
    const li = document.createElement("li");
    li.setAttribute("aria-current", String(vm.id === selected));
    const pick = document.createElement("button");
    pick.type = "button";
    pick.className = "vm-sess-pick";
    pick.textContent = vm.id;
    pick.title = "이 VM의 권한과 공유 폴더를 아래에서 편집";
    pick.addEventListener("click", () => { selected = vm.id; render(); });
    const state = document.createElement("span");
    state.className = "vm-chip " + chipClass(vm);
    state.textContent = vm.state;
    li.append(pick, state);
    for (const sc of SCOPES.filter((x) => vm.grants[SESSION].includes(x))) {
      const chip = document.createElement("span");
      chip.className = "vm-chip";
      chip.textContent = sc;
      li.appendChild(chip);
    }
    if (vm.locked) { const c = document.createElement("span"); c.className = "vm-chip warn"; c.textContent = "🔒 잠김"; li.appendChild(c); }
    if (vm.template) { const c = document.createElement("span"); c.className = "vm-chip vm-template"; c.textContent = "프리셋 템플릿"; li.appendChild(c); }
    const off = document.createElement("button");
    off.type = "button";
    off.className = "vm-sess-disconnect";
    off.textContent = "연결 해제";
    off.addEventListener("click", () => {
      if (!confirm(`${SESSION}의 ${vm.id} 연결을 해제합니다. 이 세션의 권한만 회수하며, VM은 정지되거나 삭제되지 않습니다. 계속합니까?`)) return;
      act(`${SESSION} 연결 해제 (${vm.id})`, () => post(`api/vm/${encodeURIComponent(vm.id)}/revoke`, { session: SESSION }));
    });
    li.appendChild(off);
    list.appendChild(li);
  }
  // new VM: the default preset, or a choice when there is none
  const pickSel = $("vm-sess-new-pick");
  const hasDefault = !!sessPresets.default && sessPresets.presets.some((p) => p.name === sessPresets.default && p.present !== false);
  pickSel.hidden = hasDefault;
  if (!hasDefault) {
    setChoices(pickSel, "프리셋 선택", sessPresets.presets.map((p) => [p.name, p.name, p.present === false]));
  }
  const choice = sessPresetChoice();
  $("vm-sess-new-note").textContent = sessPresets.error ? `프리셋을 읽지 못했습니다: ${sessPresets.error}`
    : hasDefault ? `기본 프리셋 ${sessPresets.default}에서 ${autoVmName()}을(를) 만들어 연결합니다(복제 후 시작, 수십 초에서 수 분).`
    : (sessPresets.default_lost ? sessPresets.default_lost + ". " : "")
      + (sessPresets.presets.length ? "기본 프리셋이 없습니다. 아래에서 프리셋을 고르거나, VM 페이지의 프리셋 목록에서 '기본으로 지정'하십시오."
        : "프리셋이 없습니다. VM 페이지에서 정지된 VM을 프리셋으로 저장하십시오.");
  const nb = $("vm-sess-new");
  nb.disabled = sessNewBusy || !choice;
  nb.classList.toggle("vm-working", sessNewBusy);
  nb.textContent = sessNewBusy ? "만드는 중…" : hasDefault ? `새 VM 만들어 연결 (${sessPresets.default})` : "새 VM 만들어 연결";
  // connect an existing VM
  const sel = $("vm-sess-pick");
  setChoices(sel, "VM 선택", vms.filter((v) => !(v.grants && v.grants[SESSION]))
    .map((vm) => [vm.id, `${vm.id} (${vm.state})${vm.template ? " — 프리셋 템플릿" : ""}${vm.locked ? " — 잠김" : ""}`]));
  $("vm-sess-connect").disabled = !sel.value;
  refreshSteal();
  const cur = vms.find((v) => v.id === selected);
  $("vm-sess-editing").textContent = cur ? `${cur.id}: ${SESSION}의 권한과 공유 폴더` : "";
  const sn = $("vm-sess-share-note");
  sn.hidden = !(cur && cur.state === "running");
  sn.textContent = "이 VM은 실행 중입니다. 공유 폴더 변경은 VM을 다시 시작해야 적용됩니다.";
}
$("vm-sess-new-pick").addEventListener("change", () => renderSess());
$("vm-sess-pick").addEventListener("change", () => { $("vm-sess-connect").disabled = !$("vm-sess-pick").value; refreshSteal(); });
$("vm-sess-ex-control").addEventListener("change", refreshSteal);
$("vm-sess-connect").addEventListener("click", () => {
  const id = $("vm-sess-pick").value;
  if (!id) return;
  const scopes = ["view"];
  for (const s of ["control", "exec", "files"]) if ($(`vm-sess-ex-${s}`).checked) scopes.push(s);
  grantTo(SESSION, id, scopes, !$("vm-sess-steal-row").hidden && $("vm-sess-steal").checked);
});
$("vm-sess-new").addEventListener("click", async () => {
  const preset = sessPresetChoice();
  if (sessNewBusy || !preset) return;
  const scopes = ["view", "control"];
  if ($("vm-sess-new-exec").checked) scopes.push("exec");
  if ($("vm-sess-new-files").checked) scopes.push("files");
  if (scopes.includes("exec") &&
      !confirm(`${SESSION}에 exec를 줍니다. 게스트 셸 실행과 설치·설정 변경을 허용합니다. 계속합니까?`)) return;
  sessNewBusy = true;
  renderSess();
  try {
    let id = autoVmName();
    let out;
    for (let tries = 0; ; tries++) {
      try {
        say(`${id}: 복제하고 시작하는 중입니다 (수십 초에서 수 분 걸립니다)`);
        out = await post(`api/vm-presets/${encodeURIComponent(preset)}/create`, { id });
        break;
      } catch (e) {
        if (e.code !== "vm_name_taken" && e.code !== "vm_exists" || tries >= 5) throw e;
        id = `${id.replace(/-\d+$/, "")}-${tries + 2}`;
      }
    }
    await post(`api/vm/${encodeURIComponent(id)}/grant`, { session: SESSION, scopes });
    selected = id;
    say(out.started ? `${id}을(를) 만들어 ${SESSION}에 연결했습니다 (${scopes.join("+")})`
      : `${id}을(를) 등록해 ${SESSION}에 연결했지만 시작하지 못했습니다(정지 상태): ${out.why}`, !out.started);
  } catch (e) {
    say(`새 VM 만들기 실패: ${e.message}`, true);
  } finally {
    sessNewBusy = false;
    await refresh();
  }
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
let presetDefaultLost = "";
async function loadPresets() {
  const note = $("vm-preset-note");
  try {
    const out = await api("api/vm-presets");
    presetRows = out.presets || [];
    presetDefaultLost = out.default_lost || "";
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
let presetManage = "";   // the preset whose management row is open
function presetDeleteBox(name) {
  const box = document.createElement("div");
  box.className = "vm-preset-manage-box";
  const note = document.createElement("p");
  note.className = "vm-warn-text";
  note.textContent = `프리셋 '${name}'과 tart 복사본(preset-${name})을 삭제합니다. 다른 VM은 지우지 않습니다. 확인하려면 프리셋 이름을 그대로 입력하십시오.`;
  const input = document.createElement("input");
  input.type = "text";
  input.className = "vm-preset-del-confirm";
  input.placeholder = "프리셋 이름 입력";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.setAttribute("aria-label", `삭제할 프리셋 이름 ${name}을 그대로 입력`);
  const del = document.createElement("button");
  del.type = "button";
  del.className = "vm-danger-solid vm-preset-del";
  del.textContent = "프리셋 삭제";
  del.disabled = true;
  input.addEventListener("input", () => { del.disabled = input.value !== name; });
  del.addEventListener("click", () => { if (input.value === name) deletePreset(name); });
  box.append(note, input, del);
  return box;
}
function renderPresets() {
  const ul = $("vm-preset-list");
  ul.replaceChildren();
  for (const p of presetRows) {
    const li = document.createElement("li");
    const head = document.createElement("strong");
    head.textContent = p.name;
    if (p.default) {
      const badge = document.createElement("span");
      badge.className = "vm-chip ok vm-preset-default-badge";
      badge.textContent = "기본";
      head.append(" ", badge);
    }
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
    // Delete is not beside "새 VM": it sits behind a "관리" toggle, in its own row,
    // and then asks for the preset's name typed in full.
    const manage = document.createElement("button");
    manage.type = "button";
    manage.className = "vm-preset-manage";
    manage.textContent = "관리 ⋯";
    manage.setAttribute("aria-expanded", presetManage === p.name ? "true" : "false");
    manage.addEventListener("click", () => { presetManage = presetManage === p.name ? "" : p.name; renderPresets(); });
    const dflt = document.createElement("button");
    dflt.type = "button";
    dflt.className = "vm-preset-default-btn";
    dflt.textContent = p.default ? "기본 해제" : "기본으로 지정";
    dflt.title = "세션 VM 패널의 '새 VM 만들어 연결'이 쓰는 프리셋";
    dflt.addEventListener("click", () => setDefaultPreset(p.default ? "" : p.name));
    li.append(head, sub, make, dflt, manage);
    if (presetManage === p.name) li.appendChild(presetDeleteBox(p.name));
    ul.appendChild(li);
  }
  if (!presetRows.length) {
    const li = document.createElement("li");
    li.className = "vm-muted";
    li.textContent = "프리셋이 없습니다.";
    ul.appendChild(li);
  }
  const lost = $("vm-preset-default-note");
  lost.hidden = !presetDefaultLost;
  lost.textContent = presetDefaultLost;
  syncPresetSave();
}
function setDefaultPreset(name) {
  act(name ? `기본 프리셋 ${name} 지정` : "기본 프리셋 해제", async () => {
    await post("api/vm-presets-default", { name });
    await loadPresets();
  });
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
  act(`프리셋 ${name} 삭제`, async () => {
    presetManage = "";
    await post(`api/vm-presets/${encodeURIComponent(name)}/delete`, { confirm: name });
    await loadPresets();
  });
}

/* ---- actions ---- */
const lifecycle = (action, label, ask) => $(`vm-${action === "force_stop" ? "force" : action}`).addEventListener("click", async () => {
  const id = selected;
  const vm = vms.find((v) => v.id === id);
  if (!id || !vm || busy[id] || (ask && !confirm(ask))) return;
  const body = {};
  if (action === "start" && vm.template) {
    if (!confirm(`${id}는 프리셋 ${vm.template}의 복사본입니다. 템플릿을 시작하면 이후 이 프리셋으로 만드는 복제본이 바뀝니다. 계속합니까?`)) return;
    body.confirm_template = true;
  }
  const held = { action, label, from: vm.state, since: Date.now(), done: false };
  busy[id] = held;
  render();
  say(`${id} ${BUSY_LABEL[action === "start" && vm.state === "suspended" ? "resume" : action]}`);
  try {
    await post(`api/vm/${encodeURIComponent(id)}/${action}`, body);
    if (busy[id] === held) held.done = true;
    say(`${label} 요청이 처리되었습니다. 상태 반영을 기다립니다`);
  } catch (e) {
    if (busy[id] === held) delete busy[id];     // the buttons come back, with the reason
    say(`${label} 실패: ${e.message}`, true);
  }
  await refresh();
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

/* ---- the feature switch (daemon.vm_enabled, off by default) ---- */
function start() {
  if (SESSION) {
    refresh().then(loadSessPresets);
    setInterval(loadSessPresets, 10000);
  } else {
    refresh().then(() => { loadSessions(); startFrames(); });
    setInterval(loadSessions, 10000);
  }
  setInterval(refresh, 2000);
}
function showOff(host) {
  $("vm-main").hidden = true;
  $("vm-reconcile").hidden = true;
  $("vm-off").hidden = false;
  const lacks = [];
  if (!host.macos) lacks.push("macOS가 아닙니다");
  else if (!host.apple_silicon) lacks.push("Apple Silicon Mac이 아닙니다");
  if (!host.tart) lacks.push("tart가 설치되어 있지 않습니다");
  $("vm-off-host").textContent = host.ok
    ? "이 호스트는 Apple Silicon macOS이고 tart가 있어 VM을 실행할 수 있습니다."
    : `이 호스트에서는 VM을 실행할 수 없습니다(${lacks.join(", ")}). 기능을 켤 수는 있지만 VM 시작은 실패합니다.`;
}
async function setEnabled(on, label) {
  try {
    await post("api/vm-enabled", { enabled: on });
    location.reload();
  } catch (e) {
    say(`${label} 실패: ${e.message}`, true);
  }
}
$("vm-enable").addEventListener("click", () => setEnabled(true, "VM 기능 켜기"));
$("vm-disable").addEventListener("click", () => {
  if (!confirm("VM 기능을 끕니다. 정지되지 않은 VM이 있으면 거절됩니다. 계속할까요?")) return;
  setEnabled(false, "VM 기능 끄기");
});
async function boot() {
  let state = null;
  // A daemon without the switch (an older one) has no route for it: the page runs as before.
  try { state = await api("api/vm-enabled"); } catch (_) { /* no switch */ }
  if (state && state.enabled === false) { showOff(state.host || {}); return; }
  $("vm-disable").hidden = !state || !!SESSION;
  start();
}
boot();
})();
