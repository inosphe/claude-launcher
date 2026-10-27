/* The web debug page (claunch-o8pb7.2; daemon/webdebug.py).

   The page under test runs inside the daemon's own headless browser. This tab
   does not frame it: it receives that browser's screen as JPEG frames over
   one WebSocket (binary), draws them in an <img>, and sends the user's mouse,
   wheel and key events back up the same socket. A screenshot is the daemon's
   capture, so it works whether or not the user is watching.

   Everything the user hands to the session is explicit: picking, a
   screenshot, a note -- each ends in a composer, and only its Send button
   types anything into the session. */
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
const target = { url: params.get("url") || "", title: "" };
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
  send({ type: "session", session });
  activity(session ? `이 창은 이제 ${session} 세션이 봅니다.` : "세션 선택이 해제됐습니다.");
});

/* ---- the socket to the daemon ---- */
let ws = null;
let wsDelay = 1000;
function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) { ws.send(JSON.stringify(msg)); return true; }
  return false;
}
function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const q = new URLSearchParams({ window: windowId });
  if (session) q.set("session", session);
  if (target.url) q.set("url", target.url);
  ws = new WebSocket(`${proto}//${location.host}${url("api/webdebug/ws")}?${q}`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    wsDelay = 1000;
    $("wd-link").classList.add("up");
    $("wd-link").title = "데몬에 연결됨";
    send({ type: "hello", session });
  };
  ws.onclose = (e) => {
    $("wd-link").classList.remove("up");
    if (e.code === 4001) {
      // The daemon closed this window because another one opened (one browser
      // page at a time). Reconnecting would close that one in turn.
      $("wd-link").title = "이 창은 닫혔습니다";
      activity("다른 웹 디버그 창이 열려 이 창은 닫혔습니다. 새로고침하면 이 창이 다시 열립니다.", "error");
      return;
    }
    $("wd-link").title = "데몬 연결 끊김 (다시 연결 중)";
    setTimeout(connect, wsDelay);
    wsDelay = Math.min(wsDelay * 2, 15000);
  };
  ws.onmessage = (e) => {
    if (e.data instanceof ArrayBuffer) { showFrame(e.data); return; }
    let msg;
    try { msg = JSON.parse(e.data); } catch (_) { return; }
    handle(msg);
  };
}

/* ---- the screen ---- */
const screen = $("wd-screen");
let frameUrl = null;
let frames = 0;
let bytes = 0;
let statsAt = performance.now();
function showFrame(buffer) {
  const blob = new Blob([buffer], { type: "image/jpeg" });
  const next = URL.createObjectURL(blob);
  const old = frameUrl;
  frameUrl = next;
  screen.src = next;
  // The previous frame is still being decoded for a moment; revoking its URL
  // immediately can leave the <img> blank. Give it a second, and keep the
  // queue bounded (45 fps x ~7 KiB x 2 s).
  if (old) setTimeout(() => URL.revokeObjectURL(old), 2000);
  $("wd-empty").classList.add("hidden");
  $("wd-busy").classList.add("hidden");
  frames += 1;
  bytes += buffer.byteLength;
  const now = performance.now();
  if (now - statsAt > 1000) {
    const secs = (now - statsAt) / 1000;
    $("wd-stat").textContent =
      `${screen.naturalWidth || "?"}×${screen.naturalHeight || "?"} · `
      + `${(frames / secs).toFixed(0)} fps · ${(bytes / secs / 1024).toFixed(0)} KiB/s`;
    frames = 0; bytes = 0; statsAt = now;
  }
}
function handle(msg) {
  const kind = msg.type;
  if (kind === "state") {
    target.url = msg.url || target.url;
    target.title = msg.title || "";
    if (!$("wd-url").matches(":focus") && target.url) $("wd-url").value = target.url;
    if (msg.session) { session = msg.session; $("wd-session").value = session; }
    $("wd-busy").classList.add("hidden");
    syncQuery();
  } else if (kind === "picked") {
    setPicking(false);
    openComposer({ kind: "element", url: msg.url || target.url, title: msg.title || target.title, element: msg.element });
  } else if (kind === "snapshot") {
    showPanel("페이지 읽기", snapshotNode(msg.value));
  } else if (kind === "console") {
    showPanel("콘솔", consoleNode(msg.value));
  } else if (kind === "screenshot") {
    openComposer({ kind: "screenshot", url: msg.url || target.url, title: msg.title || target.title, image: msg.image });
  } else if (kind === "activity") {
    activity(msg.text || "", "agent");
  } else if (kind === "error") {
    activity(String(msg.message || "오류"), "error");
    $("wd-busy").classList.add("hidden");
  }
}
screen.addEventListener("load", () => { $("wd-empty").classList.add("hidden"); });
window.addEventListener("focus", () => send({ type: "hello", session }));

/* ---- mouse, wheel and keys ---- */
// DOM and CDP use the same button names and the same buttons bitmask.
const BUTTON_NAMES = ["left", "middle", "right", "back", "forward"];
const buttonName = (n) => BUTTON_NAMES[n] || "none";
function pagePoint(e) {
  const r = screen.getBoundingClientRect();
  const w = screen.naturalWidth || r.width;
  const h = screen.naturalHeight || r.height;
  return { x: (e.clientX - r.left) * (w / r.width), y: (e.clientY - r.top) * (h / r.height) };
}
let lastDown = 0;
let lastAt = 0;
function modifiers(e) {
  return (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0);
}
screen.addEventListener("mousedown", (e) => {
  screen.focus();
  if (picking) return;
  const p = pagePoint(e);
  const now = performance.now();
  const count = (e.button === 0 && now - lastAt < 400 && Math.abs(e.clientX - lastDown) < 4) ? 2 : 1;
  lastAt = now; lastDown = e.clientX;
  send({ type: "input", event: { kind: "mouse", type: "mousePressed", ...p, button: buttonName(e.button),
    buttons: e.buttons, clickCount: count, modifiers: modifiers(e) } });
  e.preventDefault();
});
screen.addEventListener("mouseup", (e) => {
  if (picking) {
    const p = pagePoint(e);
    if (send({ type: "pick", x: p.x, y: p.y })) activity("요소를 고르는 중…", "agent");
    e.preventDefault();
    return;
  }
  const p = pagePoint(e);
  send({ type: "input", event: { kind: "mouse", type: "mouseReleased", ...p, button: buttonName(e.button),
    buttons: e.buttons, clickCount: 1, modifiers: modifiers(e) } });
  e.preventDefault();
});
let moveAt = 0;
let movePending = null;
screen.addEventListener("mousemove", (e) => {
  if (picking) return;
  const p = pagePoint(e);
  // e.buttons carries a drag: a pressed button stays held while the pointer moves.
  const ev = { kind: "mouse", type: "mouseMoved", ...p, button: buttonName(e.button),
    buttons: e.buttons, modifiers: modifiers(e) };
  const now = performance.now();
  if (now - moveAt > 33) {  // ~30 moves/s is enough to feel live
    moveAt = now;
    send({ type: "input", event: ev });
  } else if (!movePending) {
    movePending = setTimeout(() => {
      movePending = null;
      moveAt = performance.now();
      send({ type: "input", event: ev });
    }, 34);
  }
});
screen.addEventListener("contextmenu", (e) => e.preventDefault());
screen.addEventListener("wheel", (e) => {
  const p = pagePoint(e);
  send({ type: "input", event: { kind: "mouse", type: "mouseWheel", ...p,
    deltaX: e.deltaX, deltaY: e.deltaY, modifiers: modifiers(e) } });
  e.preventDefault();
}, { passive: false });
// Backspace and Enter only act on the page when Chrome is told what they
// produce; Delete and Tab already work without text (s1262-rev measured all
// four), so only these two carry it.
const EDIT_TEXT = { Backspace: "\b", Enter: "\r" };
function forwardKey(e, type) {
  if (document.activeElement !== screen) return;
  const printable = e.key.length === 1;
  const text = type !== "keyDown" ? undefined : (printable ? e.key : EDIT_TEXT[e.key]);
  send({ type: "input", event: { kind: "key", type, key: e.key, code: e.code,
    keyCode: e.keyCode, text, modifiers: modifiers(e) } });
  if (e.key === "Escape" && type === "keyDown") { screen.blur(); activity("키보드 전달을 멈췄습니다."); }
  if (printable || e.key === "Backspace" || e.key === "Enter" || e.key === "Tab" || e.key.startsWith("Arrow")) {
    e.preventDefault();
  }
}
document.addEventListener("keydown", (e) => forwardKey(e, "keyDown"), true);
document.addEventListener("keyup", (e) => forwardKey(e, "keyUp"), true);
document.addEventListener("paste", (e) => {
  if (document.activeElement !== screen) return;
  const text = e.clipboardData && e.clipboardData.getData("text");
  if (text) { send({ type: "input", event: { kind: "text", text } }); e.preventDefault(); }
});

/* ---- picking ---- */
let picking = false;
function setPicking(on) {
  picking = on;
  $("wd-pick").setAttribute("aria-pressed", on ? "true" : "false");
  screen.classList.toggle("picking", on);
  if (on) activity("요소를 고르려면 화면에서 클릭하십시오 (Esc 취소).", "agent");
}
$("wd-pick").addEventListener("click", () => setPicking(!picking));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && picking) { setPicking(false); activity("요소 선택을 취소했습니다."); }
});

/* ---- the address bar ---- */
function go(address) {
  const value = String(address || "").trim();
  if (!value) return;
  target.url = value;
  $("wd-url").value = value;
  syncQuery();
  $("wd-busy").classList.remove("hidden");
  if (!send({ type: "navigate", url: value })) activity("데몬에 연결되지 않았습니다.", "error");
  try { localStorage.setItem("claunch_webdebug_url:" + (session || ""), value); } catch (_) { /* ignore */ }
}
$("wd-address").addEventListener("submit", (e) => { e.preventDefault(); go($("wd-url").value); });
$("wd-back").addEventListener("click", () => send({ type: "back" }));
$("wd-forward").addEventListener("click", () => send({ type: "forward" }));
$("wd-reload").addEventListener("click", () => {
  if (target.url) go(target.url); else activity("먼저 주소를 입력하십시오.");
});
$("wd-quality").addEventListener("change", (e) => send({ type: "quality", quality: Number(e.target.value) }));

/* ---- side panel ---- */
function showPanel(title, node) {
  $("wd-side-title").textContent = title;
  $("wd-side-body").replaceChildren(node);
  $("wd-side").classList.remove("hidden");
}
$("wd-side-close").addEventListener("click", () => $("wd-side").classList.add("hidden"));
function snapshotNode(value) {
  const v = value || {};
  const wrap = document.createElement("div");
  const head = document.createElement("p");
  head.className = "wd-muted";
  head.textContent = `${v.title || ""} — ${v.url || ""}`;
  const pre = document.createElement("pre");
  pre.textContent = v.elements || "(요소 없음)";
  const text = document.createElement("pre");
  text.className = "wd-text";
  text.textContent = v.text || "";
  wrap.append(head, pre, text);
  return wrap;
}
function consoleNode(value) {
  const v = value || {};
  const pre = document.createElement("pre");
  pre.textContent = (v.entries || []).map((e) => `[${e.level}] ${e.text}`).join("\n") || "(출력 없음)";
  return pre;
}
$("wd-snap").addEventListener("click", () => send({ type: "snapshot" }));
$("wd-log").addEventListener("click", () => send({ type: "console" }));

/* ---- sends to the session ---- */
$("wd-shot").addEventListener("click", () => send({ type: "screenshot" }));
$("wd-note").addEventListener("click", () => openComposer({ kind: "note", url: target.url, title: target.title }));

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
    const el = item.element || {};
    rows.push(["element", el.label], ["selector", el.selector], ["text", el.text],
      ["rect", el.rect ? `${el.rect.x},${el.rect.y} ${el.rect.width}×${el.rect.height}` : ""]);
    body.appendChild(dl(rows));
    const pre = document.createElement("pre");
    pre.textContent = el.html || "";
    body.appendChild(pre);
  } else {
    body.appendChild(dl(rows));
  }
  if (item.image) {
    const img = document.createElement("img");
    img.src = url(`api/webdebug/images/${item.image}`);
    img.alt = "스크린샷 미리보기";
    body.appendChild(img);
  }
  $("wd-composer-note").value = "";
  $("wd-composer-status").textContent = session ? "" : "위 막대에서 세션을 먼저 고르십시오.";
  $("wd-composer-send").disabled = false;
  $("wd-composer").classList.remove("hidden");
  $("wd-composer-note").focus();
}
function closeComposer() {
  $("wd-composer").classList.add("hidden");
  draft = null;
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
    if (draft.kind === "note" && !note) throw new Error("메모를 입력하십시오.");
    const res = await postJSON("api/webdebug/send", {
      session, window: windowId, kind: draft.kind, url: draft.url, title: draft.title, note,
      element: draft.element, image: draft.image,
    });
    closeComposer();
    activity(res.delivered ? `${session}에 보냈습니다.` : `${session}에 전달하지 못했습니다 (세션이 종료됐을 수 있음).`,
      res.delivered ? "" : "error");
  } catch (err) {
    $("wd-composer-status").textContent = String(err.message || err);
    btn.disabled = false;
  }
});

/* ---- start ---- */
syncQuery();
loadSessions();
setInterval(loadSessions, 15000);
connect();
$("wd-url").value = target.url || (() => {
  try { return localStorage.getItem("claunch_webdebug_url:" + (session || "")) || ""; } catch (_) { return ""; }
})();
if (!target.url) $("wd-url").focus();
})();
