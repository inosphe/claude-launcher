/* The web debug page (claunch-dsb28; daemon/webdebug.py, webdebug-probe.js).

   The page under test is framed through the daemon's proxy, which runs it at
   an origin of its own and injects the probe. This page talks to the probe
   with postMessage and to the daemon with one WebSocket, on which the
   session the user picked in the top bar sends its commands.

   Everything the user hands to the session is explicit: picking, a
   screenshot region, a note -- each ends in a composer, and only its Send
   button types anything into the session. Screen sharing for the agent's own
   screenshots is a toggle the user turns on and can turn off at any time. */
"use strict";
(() => {
const BASE = location.pathname.replace(/[^/]*$/, "");
const TOKEN_KEY = `claunch_token:${BASE}`;
const $ = (id) => document.getElementById(id);
const url = (path) => BASE + String(path).replace(/^\//, "");
const params = new URLSearchParams(location.search);

/* ---- this window's id ---- */
function newWindowId() {
  const r = crypto.getRandomValues(new Uint32Array(2));
  return "w-" + (r[0].toString(36) + r[1].toString(36)).slice(0, 8);
}
let windowId = /^w-[0-9a-z]{4,16}$/.test(params.get("w") || "") ? params.get("w") : newWindowId();
let session = params.get("session") || "";
function syncQuery() {
  const q = new URLSearchParams(location.search);
  q.set("w", windowId);
  if (session) q.set("session", session); else q.delete("session");
  if (target.url) q.set("url", target.url); else q.delete("url");
  history.replaceState(null, "", location.pathname + "?" + q.toString());
  $("wd-window").textContent = windowId;
  document.title = session ? `claunch web · ${session}` : "claunch web";
}

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
    activity("로그인이 필요합니다. 대시보드에서 먼저 로그인한 뒤 이 페이지를 다시 여십시오.", "error");
    throw new Error("unauthorized");
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}
const postJSON = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});

/* ---- status line ---- */
let activityTimer = null;
function activity(text, kind = "") {
  const el = $("wd-activity");
  el.textContent = text;
  el.className = "wd-activity" + (kind ? " " + kind : "");
  el.title = text;
  clearTimeout(activityTimer);
  if (kind !== "error") activityTimer = setTimeout(() => { el.textContent = ""; }, 8000);
}

/* ---- session picker ---- */
async function loadSessions() {
  let rows = [];
  try { rows = (await api("api/webdebug/sessions")).sessions || []; } catch (_) { return; }
  const sel = $("wd-session");
  const names = rows.map((s) => s.name);
  if (session && !names.includes(session)) rows.push({ name: session, status: "exited?" });
  const want = rows.map((s) => `${s.name}\u0000${s.status}`).join("|");
  if (sel.dataset.rows === want) return;
  sel.dataset.rows = want;
  sel.replaceChildren(new Option("세션 선택", ""));
  for (const s of rows) {
    const opt = new Option(`${s.name} (${s.status})`, s.name);
    opt.title = s.cwd || "";
    sel.appendChild(opt);
  }
  sel.value = session;
}
$("wd-session").addEventListener("change", (e) => {
  session = e.target.value;
  syncQuery();
  sendState({ session });
  activity(session ? `이 창은 이제 ${session} 세션이 봅니다.` : "세션 선택이 해제됐습니다.");
});

/* ---- the socket to the daemon ---- */
let ws = null;
let wsDelay = 1000;
function sendState(extra) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "state", ...extra }));
}
function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}${url("api/webdebug/ws")}?window=${encodeURIComponent(windowId)}`);
  ws.onopen = () => {
    wsDelay = 1000;
    $("wd-link").classList.add("up");
    $("wd-link").title = "데몬에 연결됨";
    ws.send(JSON.stringify({ type: "hello", session, url: target.url, title: target.title, sharing: !!share.stream }));
  };
  ws.onclose = (e) => {
    $("wd-link").classList.remove("up");
    $("wd-link").title = "데몬 연결 끊김 (다시 연결 중)";
    if (e.code === 4000) { windowId = newWindowId(); syncQuery(); }
    setTimeout(connect, wsDelay);
    wsDelay = Math.min(wsDelay * 2, 15000);
  };
  ws.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch (_) { return; }
    if (msg.type === "command") runCommand(msg);
  };
}
window.addEventListener("focus", () => sendState({ focused: true }));

/* ---- the frame and its target ---- */
const frame = $("wd-frame");
const target = { url: "", title: "", origin: "", proxyOrigin: "", probe: false };
let readyWaiters = [];
function displayUrl(href) {
  if (target.proxyOrigin && href.startsWith(target.proxyOrigin)) return target.origin + href.slice(target.proxyOrigin.length);
  return href;
}
async function navigate(raw) {
  const text = String(raw || "").trim();
  if (!text) return;
  if (location.protocol === "https:") {
    activity("대시보드가 https로 열려 있어 http 프록시 페이지를 프레임에 넣을 수 없습니다.", "error");
  }
  const res = await postJSON("api/webdebug/proxy", { url: text });
  target.origin = res.origin;
  target.proxyOrigin = `${location.protocol}//${location.hostname}:${res.port}`;
  target.url = res.origin + res.path;
  target.probe = false;
  $("wd-url").value = target.url;
  $("wd-empty").classList.add("hidden");
  frame.src = target.proxyOrigin + res.enter + "&parent=" + encodeURIComponent(location.origin);
  syncQuery();
  sendState({ url: target.url });
  try { localStorage.setItem("claunch_webdebug_url:" + (session || ""), target.url); } catch (_) { /* ignore */ }
}
function waitReady(ms = 15000) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error("the page did not finish loading (or the probe is absent)")), ms);
    readyWaiters.push(() => { clearTimeout(t); resolve(); });
  });
}
frame.addEventListener("load", () => {
  // A page that left the proxy (another origin) loads without the probe.
  setTimeout(() => {
    if (!target.probe) activity("이 프레임에는 probe가 없습니다 (프록시 밖 주소로 이동했거나 HTML이 아닌 응답).", "error");
  }, 2500);
});
$("wd-address").addEventListener("submit", (e) => {
  e.preventDefault();
  navigate($("wd-url").value).catch((err) => activity(String(err.message || err), "error"));
});
$("wd-back").addEventListener("click", () => probe("back").catch(() => {}));
$("wd-forward").addEventListener("click", () => probe("forward").catch(() => {}));
$("wd-reload").addEventListener("click", () => {
  if (target.probe) probe("reload").catch(() => {}); else if (target.url) navigate(target.url).catch(() => {});
});

/* ---- the probe ---- */
const probeWaiters = new Map();
let probeSeq = 0;
function probe(op, args = {}, ms = 15000) {
  if (!frame.contentWindow || !target.proxyOrigin) return Promise.reject(new Error("no page is open"));
  const id = "p" + (++probeSeq);
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => { probeWaiters.delete(id); reject(new Error(`the page did not answer ${op}`)); }, ms);
    probeWaiters.set(id, { resolve, reject, t });
    frame.contentWindow.postMessage({ claunch: "wd", type: "cmd", id, op, args }, target.proxyOrigin);
  });
}
window.addEventListener("message", (e) => {
  if (e.source !== frame.contentWindow || e.origin !== target.proxyOrigin) return;
  const msg = e.data;
  if (!msg || msg.claunch !== "wd") return;
  if (msg.type === "res") {
    const w = probeWaiters.get(msg.id);
    if (!w) return;
    probeWaiters.delete(msg.id);
    clearTimeout(w.t);
    if (msg.ok) w.resolve(msg.value); else w.reject(new Error(msg.error));
  } else if (msg.type === "ready" || msg.type === "nav") {
    target.probe = true;
    target.url = displayUrl(msg.url);
    target.title = msg.title || "";
    if (document.activeElement !== $("wd-url")) $("wd-url").value = target.url;
    syncQuery();
    sendState({ url: target.url, title: target.title });
    const waiters = readyWaiters; readyWaiters = [];
    waiters.forEach((f) => f());
  } else if (msg.type === "picked") {
    setPicking(false);
    activity("");
    openComposer({ kind: "element", element: msg.element, url: displayUrl(msg.url), title: msg.title });
  } else if (msg.type === "pick-cancel") {
    setPicking(false);
  } else if (msg.type === "console" && msg.entry) {
    activity("페이지 오류: " + msg.entry.text.split("\n")[0], "error");
  }
});

/* ---- picking ---- */
let picking = false;
function setPicking(on) {
  picking = on;
  $("wd-pick").setAttribute("aria-pressed", on ? "true" : "false");
}
$("wd-pick").addEventListener("click", async () => {
  try {
    if (picking) { await probe("pick-cancel"); setPicking(false); return; }
    await probe("pick");
    setPicking(true);
    activity("페이지에서 요소를 클릭하십시오. Esc는 취소.");
    frame.focus();
  } catch (err) { activity(String(err.message || err), "error"); }
});

/* ---- screen capture ---- */
const share = { stream: null, persistent: false };
async function startStream() {
  if (share.stream) return share.stream;
  const stream = await navigator.mediaDevices.getDisplayMedia({
    video: { displaySurface: "browser" }, audio: false,
    preferCurrentTab: true, selfBrowserSurface: "include", surfaceSwitching: "exclude",
  });
  const track = stream.getVideoTracks()[0];
  const surface = (track.getSettings && track.getSettings().displaySurface) || "";
  if (surface && surface !== "browser") {
    activity("이 탭이 아닌 화면이 공유됐습니다. 좌표가 맞지 않을 수 있으니 이 탭을 고르십시오.", "error");
  }
  track.addEventListener("ended", () => stopStream());
  share.stream = stream;
  const video = $("wd-video");
  video.srcObject = stream;
  await video.play();
  return stream;
}
function stopStream() {
  if (share.stream) share.stream.getTracks().forEach((t) => t.stop());
  share.stream = null;
  share.persistent = false;
  $("wd-video").srcObject = null;
  $("wd-share").setAttribute("aria-pressed", "false");
  sendState({ sharing: false });
}
$("wd-share").addEventListener("click", async () => {
  if (share.persistent) { stopStream(); activity("화면 공유를 껐습니다."); return; }
  try {
    await startStream();
    share.persistent = true;
    $("wd-share").setAttribute("aria-pressed", "true");
    sendState({ sharing: true });
    activity("화면 공유 중: 세션이 이 창의 스크린샷을 찍을 수 있습니다.");
  } catch (err) { activity("화면 공유를 시작하지 못했습니다: " + (err.message || err), "error"); }
});
const nextFrame = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
async function freshFrame(video) {
  await nextFrame();
  if (video.requestVideoFrameCallback) {
    await new Promise((r) => { const t = setTimeout(r, 400); video.requestVideoFrameCallback(() => { clearTimeout(t); r(); }); });
  } else {
    await new Promise((r) => setTimeout(r, 200));
  }
}
/* Capture `rect` (CSS px of this page's viewport) from the shared tab. */
async function capture(rect) {
  const video = $("wd-video");
  const composer = $("wd-composer");
  const wasHidden = composer.classList.contains("hidden");
  composer.classList.add("hidden");
  try {
    await freshFrame(video);
    const sx = video.videoWidth / window.innerWidth;
    const sy = video.videoHeight / window.innerHeight;
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(rect.width * sx));
    canvas.height = Math.max(1, Math.round(rect.height * sy));
    canvas.getContext("2d").drawImage(video, rect.x * sx, rect.y * sy, rect.width * sx, rect.height * sy,
      0, 0, canvas.width, canvas.height);
    return await new Promise((r) => canvas.toBlob(r, "image/png"));
  } finally {
    if (!wasHidden) composer.classList.remove("hidden");
  }
}
function frameRect() {
  const r = frame.getBoundingClientRect();
  return { x: r.left, y: r.top, width: r.width, height: r.height };
}
async function captureElement(args) {
  const inner = (await probe("rect", args)).rect;
  const f = frameRect();
  const x = Math.max(f.x, f.x + inner.x), y = Math.max(f.y, f.y + inner.y);
  const right = Math.min(f.x + f.width, f.x + inner.x + inner.width);
  const bottom = Math.min(f.y + f.height, f.y + inner.y + inner.height);
  if (right <= x || bottom <= y) throw new Error("the element is outside the visible page");
  return capture({ x, y, width: right - x, height: bottom - y });
}
async function upload(blob) {
  return api("api/webdebug/images", { method: "POST", headers: { "Content-Type": blob.type || "image/png" }, body: blob });
}
function selectRegion() {
  return new Promise((resolve) => {
    const layer = $("wd-region"), box = $("wd-region-box");
    layer.classList.remove("hidden");
    box.style.display = "none";
    let start = null;
    const stage = layer.getBoundingClientRect();
    const done = (value) => {
      layer.classList.add("hidden");
      layer.onpointerdown = layer.onpointermove = layer.onpointerup = null;
      document.removeEventListener("keydown", onKey, true);
      resolve(value);
    };
    const onKey = (e) => { if (e.key === "Escape") { e.preventDefault(); done(null); } };
    document.addEventListener("keydown", onKey, true);
    layer.onpointerdown = (e) => { start = { x: e.clientX, y: e.clientY }; layer.setPointerCapture(e.pointerId); };
    layer.onpointermove = (e) => {
      if (!start) return;
      const x = Math.min(start.x, e.clientX), y = Math.min(start.y, e.clientY);
      Object.assign(box.style, { display: "block", left: (x - stage.left) + "px", top: (y - stage.top) + "px",
        width: Math.abs(e.clientX - start.x) + "px", height: Math.abs(e.clientY - start.y) + "px" });
    };
    layer.onpointerup = (e) => {
      if (!start) return;
      const w = Math.abs(e.clientX - start.x), h = Math.abs(e.clientY - start.y);
      if (w < 4 || h < 4) { done(frameRect()); return; }
      done({ x: Math.min(start.x, e.clientX), y: Math.min(start.y, e.clientY), width: w, height: h });
    };
  });
}
$("wd-shot").addEventListener("click", async () => {
  if (!target.proxyOrigin) { activity("먼저 페이지를 여십시오.", "error"); return; }
  let temporary = false;
  try {
    if (!share.stream) { await startStream(); temporary = true; }
    const region = await selectRegion();
    if (!region) return;
    const blob = await capture(region);
    const f = frameRect();
    openComposer({ kind: "screenshot", blob, url: target.url, title: target.title,
      region: { x: Math.round(region.x - f.x), y: Math.round(region.y - f.y),
        width: Math.round(region.width), height: Math.round(region.height) } });
  } catch (err) {
    activity("스크린샷을 찍지 못했습니다: " + (err.message || err), "error");
  } finally {
    if (temporary && !share.persistent) stopStream();
  }
});
$("wd-note").addEventListener("click", () => openComposer({ kind: "note", url: target.url, title: target.title }));

/* ---- composer: the only way anything reaches the session ---- */
let draft = null;
function dl(rows) {
  const d = document.createElement("dl");
  for (const [k, v] of rows) {
    if (!v) continue;
    d.append(Object.assign(document.createElement("dt"), { textContent: k }),
      Object.assign(document.createElement("dd"), { textContent: v }));
  }
  return d;
}
function openComposer(item) {
  draft = item;
  const body = $("wd-composer-body");
  body.replaceChildren();
  const titles = { element: "요소 보내기", screenshot: "스크린샷 보내기", note: "메모 보내기" };
  $("wd-composer-title").textContent = `${titles[item.kind]} → ${session || "(세션 선택 필요)"}`;
  const rows = [["page", item.url || "(열린 페이지 없음)"]];
  if (item.kind === "element") {
    const el = item.element;
    rows.push(["element", el.label], ["selector", el.selector], ["text", el.text],
      ["rect", `${el.rect.x},${el.rect.y} ${el.rect.width}×${el.rect.height}`]);
    body.appendChild(dl(rows));
    const pre = document.createElement("pre");
    pre.textContent = el.html;
    body.appendChild(pre);
  } else {
    body.appendChild(dl(rows));
  }
  if (item.blob) {
    const img = document.createElement("img");
    img.src = URL.createObjectURL(item.blob);
    img.alt = "스크린샷 미리보기";
    body.appendChild(img);
  }
  $("wd-composer-capture-row").classList.toggle("hidden", !(item.kind === "element" && share.stream));
  $("wd-composer-capture").checked = false;
  $("wd-composer-note").value = "";
  $("wd-composer-status").textContent = session ? "" : "위 막대에서 세션을 먼저 고르십시오.";
  $("wd-composer-send").disabled = false;
  $("wd-composer").classList.remove("hidden");
  $("wd-composer-note").focus();
}
function closeComposer() {
  $("wd-composer").classList.add("hidden");
  draft = null;
  probe("hide").catch(() => {});
}
$("wd-composer-close").addEventListener("click", closeComposer);
$("wd-composer-cancel").addEventListener("click", closeComposer);
$("wd-composer-note").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $("wd-composer-send").click(); }
  if (e.key === "Escape") closeComposer();
});
$("wd-composer-send").addEventListener("click", async () => {
  if (!draft) return;
  if (!session) { $("wd-composer-status").textContent = "위 막대에서 세션을 먼저 고르십시오."; return; }
  const btn = $("wd-composer-send");
  btn.disabled = true;
  $("wd-composer-status").textContent = "보내는 중…";
  try {
    const note = $("wd-composer-note").value.trim();
    let blob = draft.blob || null;
    if (!blob && draft.kind === "element" && $("wd-composer-capture").checked) {
      blob = await captureElement({ selector: draft.element.selector });
    }
    if (draft.kind === "note" && !note) throw new Error("메모를 입력하십시오.");
    const image = blob ? (await upload(blob)).id : undefined;
    const res = await postJSON("api/webdebug/send", {
      session, window: windowId, kind: draft.kind, url: draft.url, title: draft.title, note,
      element: draft.element, region: draft.region, image,
    });
    closeComposer();
    activity(res.delivered ? `${session}에 보냈습니다.` : `${session}에 전달하지 못했습니다 (세션이 종료됐을 수 있음).`,
      res.delivered ? "" : "error");
  } catch (err) {
    $("wd-composer-status").textContent = String(err.message || err);
    btn.disabled = false;
  }
});

/* ---- commands from the session's agent ---- */
const VERB = { snapshot: "페이지 읽기", query: "요소 찾기", click: "클릭", type: "입력", press: "키 입력",
  scroll: "스크롤", navigate: "이동", eval: "스크립트 실행", console: "콘솔 읽기", screenshot: "스크린샷",
  highlight: "강조" };
function describe(op, args) {
  const what = args.ref || args.selector || args.url || args.key || "";
  return `에이전트(${session}): ${VERB[op] || op}${what ? " " + String(what).slice(0, 80) : ""}`;
}
async function runCommand(msg) {
  const { id, op } = msg;
  const args = msg.args || {};
  activity(describe(op, args), "agent");
  const reply = (body) => { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "result", id, ...body })); };
  try {
    let value;
    if (op === "navigate") {
      const ready = waitReady();
      await navigate(args.url);
      await ready;
      value = { url: target.url, title: target.title };
    } else if (op === "screenshot") {
      if (!share.stream) {
        throw new Error("the user has not turned on screen sharing in this window; ask them to press "
          + "'화면 공유' in its top bar (or to send a screenshot themselves)");
      }
      const blob = (args.ref || args.selector) ? await captureElement(args) : await capture(frameRect());
      const up = await upload(blob);
      value = { image: up.id, bytes: up.bytes };
    } else {
      if (!target.probe) throw new Error("no page with the probe is open in this window");
      value = await probe(op, args, Math.min(Number(args.timeout_ms) || 15000, 60000));
    }
    reply({ ok: true, value });
  } catch (err) {
    reply({ ok: false, error: String(err.message || err) });
    activity(describe(op, args) + " 실패: " + (err.message || err), "error");
  }
}

/* ---- start ---- */
syncQuery();
loadSessions();
setInterval(loadSessions, 15000);
connect();
if (BASE !== "/") activity("릴레이 경로로 열린 대시보드에서는 프록시 포트에 닿지 못할 수 있습니다.", "error");
// A link that names the page opens it; otherwise the last address is only
// put back in the bar, and opening it is the user's press of 이동.
const first = params.get("url");
if (first) {
  $("wd-url").value = first;
  navigate(first).catch((err) => activity(String(err.message || err), "error"));
} else {
  try { $("wd-url").value = localStorage.getItem("claunch_webdebug_url:" + (session || "")) || ""; } catch (_) { /* ignore */ }
  $("wd-url").focus();
}
})();
