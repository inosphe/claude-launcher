/* Chat: a room per mesh where the people on its daemons talk to each other
   (daemon/mesh_chat.py), as a page (#/chat, #/chat/<mesh>) and as a modal
   over any page (the floating button above the Operator's).

   Who is talking is this BROWSER: a random key minted into localStorage the
   first time the page loads, plus a display name the person can change. The
   key is an identifier, not a credential — a second browser (or a private
   window) is a second person. Read positions are per browser too, so the
   badges count what THIS browser has not seen.

   New messages are announced four ways, each for a different moment: the
   badges on the nav entry and the floating button (always), a toast with an
   inline reply box (when the room is not on screen), a desktop notification
   (when the tab is not visible), and a short tone (unless switched off). The
   history a room already had when this browser first saw it is not news.

   Every string here came from a person on some daemon: it is rendered as
   text, never as HTML. Nothing typed here reaches an agent's terminal. */
globalThis.ChatPanel = (() => {
"use strict";
const node = (tag, text, cls) => { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; };
async function request(path, body) {
  const options = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
  const response = await api(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(data.error || `HTTP ${response.status}`);
  return data;
}
const roomPath = mesh => `api/chat/rooms/${encodeURIComponent(mesh)}/messages`;

/* ---- pure helpers (tests/web/chat_check.js) ---- */
const KEY_RE = /^[A-Za-z0-9_-]{8,64}$/;
function mintKey(random) {
  const bytes = new Uint8Array(16);
  random(bytes);
  return Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
}
/* The stored identity if it is one, else a fresh one (and whether it is). */
function identityFrom(raw, random) {
  let doc = null;
  try { doc = JSON.parse(raw || "null"); } catch {}
  if (doc && typeof doc.key === "string" && KEY_RE.test(doc.key)) {
    const name = typeof doc.name === "string" && doc.name.trim() ? doc.name.trim().slice(0, 40) : `guest-${doc.key.slice(0, 4)}`;
    return {key: doc.key, name, fresh: false};
  }
  const key = mintKey(random);
  return {key, name: `guest-${key.slice(0, 4)}`, fresh: true};
}
/* Unread per room: what arrived after this browser's read position. A room
   seen for the first time starts read at its current end — its history is not
   news — which is why this also returns the positions to store. */
function unreadOf(rooms, read) {
  const next = {...read}, counts = {};
  for (const r of rooms || []) {
    if (!(r.mesh in next)) next[r.mesh] = r.last_seq;
    counts[r.mesh] = Math.max(0, r.last_seq - next[r.mesh]);
  }
  return {read: next, counts, total: Object.values(counts).reduce((a, b) => a + b, 0)};
}
/* The rooms that grew since the last poll, with the seq to read after. The
   first poll of a page load primes `known` and announces nothing. */
function grown(known, rooms) {
  const out = [];
  for (const r of rooms || []) {
    const had = known.get(r.mesh);
    if (had != null && r.last_seq > had) out.push({mesh: r.mesh, after: had});
  }
  return out;
}
/* What deserves an alert: messages someone ELSE wrote. */
function alertable(messages, key) {
  return (messages || []).filter(m => m.key !== key);
}
/* Which announcements apply right now. */
function alertPlan(at) {
  return {toast: !at.roomOnScreen, desktop: at.desktop && !at.tabVisible, sound: at.sound && !at.roomOnScreen};
}
function clock(at) {
  const d = new Date(at);
  if (isNaN(d)) return "";
  const pad = n => String(n).padStart(2, "0");
  const today = new Date().toDateString() === d.toDateString();
  return (today ? "" : `${d.getMonth() + 1}/${d.getDate()} `) + `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/* ---- per-browser state (localStorage; every access may throw) ---- */
const ID_KEY = "claunch-chat-identity";
const PREFS_KEY = "claunch-chat-prefs";
// Per daemon address: app.js (loaded after this file) defines BASE.
const readKey = () => `claunch-chat-read:${typeof BASE === "string" ? BASE : ""}`;
const store = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch {} },
  json(key, fallback) { try { return JSON.parse(localStorage.getItem(key) || "null") ?? fallback; } catch { return fallback; } },
};
const random = bytes => (globalThis.crypto?.getRandomValues ? crypto.getRandomValues(bytes) : bytes.forEach((_, i) => { bytes[i] = Math.floor(Math.random() * 256); }));
const me = identityFrom(store.get(ID_KEY), random);
if (me.fresh) store.set(ID_KEY, JSON.stringify({key: me.key, name: me.name}));
const prefs = {sound: true, desktop: true, ...store.json(PREFS_KEY, {})};
let read = null;   // loaded on the first poll, once BASE exists
function setName(name) {
  const clean = String(name || "").trim().slice(0, 40);
  if (!clean) return false;
  me.name = clean;
  store.set(ID_KEY, JSON.stringify({key: me.key, name: me.name}));
  for (const h of hosts) h.nameInput.value = me.name;
  return true;
}
function setPref(name, value) {
  prefs[name] = value;
  store.set(PREFS_KEY, JSON.stringify(prefs));
  if (name === "desktop" && value) askPermission();
  for (const h of hosts) h.prefBoxes[name].checked = value;
}
function askPermission() {
  if (typeof Notification !== "undefined" && Notification.permission === "default") {
    try { Notification.requestPermission(); } catch {}
  }
}

/* ---- data ---- */
let rooms = [], localMachine = "", current = "";
const cache = new Map();     // mesh -> messages this page has loaded
const known = new Map();     // mesh -> last_seq at the previous poll
let primed = false, counts = {};
const hosts = new Set();

function onScreen(mesh) {
  if (document.visibilityState !== "visible" || mesh !== current) return false;
  return [...hosts].some(h => h.root.isConnected && h.root.offsetParent);
}
function markRead(mesh) {
  const room = rooms.find(r => r.mesh === mesh);
  if (!room) return;
  if (read === null) read = store.json(readKey(), {});
  read[mesh] = Math.max(read[mesh] || 0, room.last_seq);
  store.set(readKey(), JSON.stringify(read));
  recount();
}
function recount() {
  if (read === null) read = store.json(readKey(), {});
  const u = unreadOf(rooms, read);
  read = u.read; counts = u.counts;
  store.set(readKey(), JSON.stringify(read));
  for (const id of ["chat-nav-badge", "chat-fab-badge"]) {
    const badge = document.getElementById(id);
    if (!badge) continue;
    badge.textContent = u.total ? String(u.total) : "";
    badge.hidden = !u.total;
  }
  fab.classList.toggle("chat-fab-alert", !!u.total);
}
function merge(mesh, messages) {
  const have = cache.get(mesh);
  if (!have) return;
  for (const m of messages) if (m.seq === (have.length ? have[have.length - 1].seq : 0) + 1) have.push(m);
}
async function loadRoom(mesh) {
  const doc = await request(`${roomPath(mesh)}?limit=200`);
  cache.set(mesh, doc.messages || []);
  return doc;
}
async function poll() {
  let doc;
  try { doc = await request("api/chat/rooms"); } catch (err) { status(err.message); return; }
  status("");
  rooms = doc.rooms || [];
  localMachine = doc.machine || "";
  if (!current || !rooms.some(r => r.mesh === current)) current = rooms[0]?.mesh || "";
  const fresh = primed ? grown(known, rooms) : [];
  for (const r of rooms) known.set(r.mesh, r.last_seq);
  primed = true;
  for (const {mesh, after} of fresh) {
    let got;
    try { got = (await request(`${roomPath(mesh)}?after=${after}`)).messages || []; } catch { continue; }
    merge(mesh, got);
    const others = alertable(got, me.key);
    if (others.length) announce(mesh, others);
  }
  // Not loaded yet, or behind what the room holds (a fetch missed a gap).
  const have = cache.get(current), room = rooms.find(r => r.mesh === current);
  if (current && (!have || (room && (have.length ? have[have.length - 1].seq : 0) < room.last_seq))) {
    try { await loadRoom(current); } catch {}
  }
  if (onScreen(current)) markRead(current);
  recount();
  render();
}

/* ---- announcements ---- */
let audio = null;
// Browsers start an AudioContext suspended until the page has had a gesture;
// make (and resume) it on the first one so a later tone can play.
for (const type of ["pointerdown", "keydown"]) {
  document.addEventListener(type, () => {
    try { audio = audio || new (globalThis.AudioContext || globalThis.webkitAudioContext)(); audio.resume?.(); } catch {}
  }, {once: true, capture: true});
}
function tone() {
  if (!audio || audio.state !== "running") return;
  try {
    const t = audio.currentTime;
    for (const [freq, at] of [[880, 0], [1320, 0.12]]) {
      const osc = audio.createOscillator(), gain = audio.createGain();
      osc.type = "sine"; osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, t + at);
      gain.gain.exponentialRampToValueAtTime(0.15, t + at + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + at + 0.18);
      osc.connect(gain).connect(audio.destination);
      osc.start(t + at); osc.stop(t + at + 0.2);
    }
  } catch {}
}
function announce(mesh, messages) {
  const last = messages[messages.length - 1];
  const plan = alertPlan({roomOnScreen: onScreen(mesh), tabVisible: document.visibilityState === "visible",
                          sound: prefs.sound, desktop: prefs.desktop});
  if (plan.toast) toast(mesh, last, messages.length);
  if (plan.sound) tone();
  if (plan.desktop && typeof Notification !== "undefined" && Notification.permission === "granted") {
    try {
      const n = new Notification(`${last.name} · ${mesh}`, {body: last.body.slice(0, 160), tag: `claunch-chat-${mesh}`});
      n.onclick = () => { try { window.focus(); } catch {} openModal(mesh); n.close(); };
    } catch {}
  }
}

/* A toast per room (a newer message replaces the room's toast), with a reply
   box so answering does not need the room to be opened at all. */
const toasts = node("div", null, "chat-toasts");
toasts.setAttribute("aria-live", "polite");
document.body.append(toasts);
function toast(mesh, message, count) {
  toasts.querySelector(`[data-mesh="${CSS.escape(mesh)}"]`)?.remove();
  const card = node("div", null, "chat-toast");
  card.dataset.mesh = mesh;
  const head = node("div", null, "chat-toast-head");
  head.append(node("strong", message.name), node("span", ` · ${mesh}` + (count > 1 ? ` (+${count - 1})` : ""), "chat-toast-room"));
  const close = node("button", "×", "chat-toast-close"); close.type = "button"; close.setAttribute("aria-label", "닫기");
  close.onclick = () => card.remove();
  head.append(close);
  const body = node("div", message.body.length > 240 ? message.body.slice(0, 240) + "…" : message.body, "chat-toast-body");
  body.onclick = () => { card.remove(); openModal(mesh); };
  const form = node("form", null, "chat-toast-reply");
  const input = node("input"); input.placeholder = "답장…"; input.setAttribute("aria-label", `${mesh}에 답장`);
  const send = node("button", "보내기"); send.type = "submit";
  const open = node("button", "열기"); open.type = "button";
  open.onclick = () => { card.remove(); openModal(mesh); };
  form.append(input, send, open);
  form.onsubmit = async event => {
    event.preventDefault();
    if (!input.value.trim()) return;
    send.disabled = true;
    try { await post(mesh, input.value); card.remove(); }
    catch (err) { input.title = err.message; card.classList.add("chat-toast-failed"); send.disabled = false; }
  };
  card.append(head, body, form);
  let timer = null;
  const arm = () => { clearTimeout(timer); timer = setTimeout(() => { if (!card.matches(":hover") && !card.contains(document.activeElement)) card.remove(); else arm(); }, 15000); };
  arm();
  toasts.prepend(card);
  while (toasts.children.length > 3) toasts.lastChild.remove();
}

/* ---- sending ---- */
async function post(mesh, text) {
  const doc = await request(roomPath(mesh), {key: me.key, name: me.name, body: text});
  const room = rooms.find(r => r.mesh === mesh);
  if (room) room.last_seq = Math.max(room.last_seq, doc.message.seq);
  known.set(mesh, Math.max(known.get(mesh) || 0, doc.message.seq));
  if (!cache.has(mesh)) await loadRoom(mesh).catch(() => {});
  else merge(mesh, [doc.message]);
  if (read === null) read = store.json(readKey(), {});
  read[mesh] = Math.max(read[mesh] || 0, doc.message.seq);
  recount();
  render(true);
  return doc.message;
}

/* ---- the view (built into the page and the modal) ---- */
function build(root) {
  root.replaceChildren();
  const host = node("div", null, "chat-host");
  const head = node("header", null, "chat-head");
  head.append(node("h1", "Chat"));
  const who = node("form", null, "chat-who");
  const nameInput = node("input"); nameInput.value = me.name; nameInput.maxLength = 40;
  nameInput.setAttribute("aria-label", "내 이름");
  const save = node("button", "이름 변경"); save.type = "submit";
  who.append(node("span", "내 이름", "chat-who-label"), nameInput, save,
             node("span", `ID ${me.key.slice(0, 8)}`, "chat-who-key"));
  who.title = "이 브라우저의 식별자입니다. 이름은 이후에 보내는 메시지부터 적용됩니다.";
  who.onsubmit = event => { event.preventDefault(); if (!setName(nameInput.value)) nameInput.value = me.name; };
  const prefsBox = node("div", null, "chat-prefs");
  const prefBoxes = {};
  for (const [name, label] of [["sound", "알림음"], ["desktop", "데스크톱 알림"]]) {
    const lab = node("label"); const box = node("input"); box.type = "checkbox"; box.checked = prefs[name];
    box.onchange = () => setPref(name, box.checked);
    prefBoxes[name] = box;
    lab.append(box, document.createTextNode(" " + label));
    prefsBox.append(lab);
  }
  const statusLine = node("span", "", "chat-status");
  head.append(who, prefsBox, statusLine);
  const body = node("div", null, "chat-body");
  const list = node("ul", null, "chat-rooms");
  const pane = node("section", null, "chat-room");
  const title = node("div", null, "chat-room-title");
  const feed = node("ul", null, "chat-feed");
  const composer = node("form", null, "chat-composer");
  const text = node("textarea"); text.rows = 2; text.placeholder = "메시지 (Enter 전송, Shift+Enter 줄바꿈)";
  const send = node("button", "보내기"); send.type = "submit";
  const error = node("div", "", "chat-error"); error.hidden = true;
  composer.append(text, send);
  text.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); composer.requestSubmit(); }
  });
  composer.onsubmit = async event => {
    event.preventDefault();
    if (!current || !text.value.trim()) return;
    send.disabled = true; error.hidden = true;
    try { await post(current, text.value); text.value = ""; }
    catch (err) { error.textContent = `전송하지 못했습니다: ${err.message}`; error.hidden = false; }
    finally { send.disabled = false; text.focus(); }
  };
  pane.append(title, feed, error, composer);
  body.append(list, pane);
  host.append(head, body);
  root.append(host);
  return {root, host, list, title, feed, text, nameInput, prefBoxes, statusLine};
}
function status(message) { for (const h of hosts) h.statusLine.textContent = message; }
function renderRooms(h) {
  h.list.replaceChildren();
  if (!rooms.length) { h.list.append(node("li", "이 데몬이 참가한 mesh가 없습니다.", "chat-empty")); return; }
  for (const r of rooms) {
    const li = node("li"); const b = node("button", null, "chat-room-pick"); b.type = "button";
    b.classList.toggle("chat-room-current", r.mesh === current);
    const top = node("span", null, "chat-room-top");
    top.append(node("span", r.mesh, "chat-room-name"));
    if (counts[r.mesh]) top.append(node("span", String(counts[r.mesh]), "chat-badge-count"));
    b.append(top);
    if (r.last) b.append(node("span", `${r.last.name}: ${r.last.body.split("\n")[0]}`, "chat-room-last"));
    b.onclick = () => select(r.mesh);
    li.append(b); h.list.append(li);
  }
}
function renderFeed(h, follow) {
  const room = rooms.find(r => r.mesh === current);
  h.title.replaceChildren();
  if (!room) { h.feed.replaceChildren(); return; }
  h.title.append(node("strong", room.address));
  h.title.append(node("span", room.local ? " · 이 데몬이 보관하는 방" : ` · ${room.authority} 데몬이 보관하는 방`, "chat-room-where"));
  const atBottom = h.feed.scrollHeight - h.feed.scrollTop - h.feed.clientHeight < 40;
  const messages = cache.get(current) || [];
  const shown = h.feed.dataset.mesh === current ? Number(h.feed.dataset.seq || 0) : -1;
  const last = messages.length ? messages[messages.length - 1].seq : 0;
  if (shown === last && h.feed.childElementCount) return;
  h.feed.replaceChildren();
  if (!messages.length) h.feed.append(node("li", "아직 메시지가 없습니다. 이 mesh에 참가한 데몬의 사용자와 대화할 수 있습니다.", "chat-empty"));
  for (const m of messages) {
    const li = node("li", null, "chat-msg");
    li.classList.toggle("chat-mine", m.key === me.key);
    const meta = node("div", null, "chat-meta");
    meta.append(node("strong", m.name));
    if (m.machine && m.machine !== localMachine) meta.append(node("span", `@${m.machine}`, "chat-machine"));
    meta.append(node("span", clock(m.at), "chat-time"));
    meta.title = `ID ${m.key.slice(0, 8)}`;
    li.append(meta, node("div", m.body, "chat-text"));
    h.feed.append(li);
  }
  h.feed.dataset.mesh = current; h.feed.dataset.seq = String(last);
  if (follow || atBottom || shown < 0) h.feed.scrollTop = h.feed.scrollHeight;
}
function render(follow) {
  for (const h of hosts) {
    if (!h.root.isConnected) continue;
    renderRooms(h); renderFeed(h, follow);
  }
}
async function select(mesh) {
  current = mesh;
  toasts.querySelector(`[data-mesh="${CSS.escape(mesh)}"]`)?.remove();
  render();
  if (!cache.has(mesh)) { try { await loadRoom(mesh); } catch (err) { status(err.message); } }
  markRead(mesh);
  render(true);
  for (const h of hosts) if (h.root.offsetParent) h.text.focus();
}
function attach(root) {
  for (const h of hosts) if (h.root === root) return h;
  const h = build(root);
  hosts.add(h);
  return h;
}

/* ---- polling: fast while a host is on screen, slow otherwise (the slow
   poll is what makes the badges, toasts and notifications work from any
   page and from a background tab) ---- */
let timer = null;
/* One poll and the next one booked, whatever the poll did: a poll that
   throws must not end the polling. */
function cycle() {
  return poll().catch(err => { status(err.message); console.error("chat poll", err); }).finally(schedule);
}
function schedule() {
  clearTimeout(timer);
  const fast = (pageOpen || modal.open) && document.visibilityState === "visible";
  timer = setTimeout(cycle, fast ? 3000 : document.visibilityState === "visible" ? 8000 : 20000);
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") { if (onScreen(current)) markRead(current); cycle(); }
});

/* ---- the page ---- */
let pageOpen = false;
function open(mesh) {
  pageOpen = true;
  document.body.classList.add("chat-page");
  attach(document.getElementById("chat-view"));
  if (prefs.desktop) askPermission();
  if (mesh) select(mesh);
  cycle();
}
/* route() calls this on every navigation that is not the Chat page, so it
   reschedules only when it actually closes the page: rescheduling each time
   would keep pushing the background poll back and starve it. */
function stop() {
  if (!pageOpen) return;
  pageOpen = false;
  document.body.classList.remove("chat-page");
  schedule();
}

/* ---- the modal ---- */
const modal = document.createElement("dialog");
modal.className = "chat-modal"; modal.setAttribute("aria-label", "Chat");
const modalClose = node("button", "×", "chat-modal-close"); modalClose.type = "button";
modalClose.setAttribute("aria-label", "닫기"); modalClose.title = "닫기 (Esc)";
modalClose.onclick = () => modal.close();
const modalRoot = node("div", null, "chat-modal-root");
modal.append(modalClose, modalRoot);
modal.addEventListener("close", schedule);
document.body.append(modal);
function openModal(mesh) {
  if (pageOpen) { if (mesh) select(mesh); return; }
  if (!modal.open) modal.showModal();
  attach(modalRoot);
  if (prefs.desktop) askPermission();
  if (mesh) select(mesh); else { render(true); if (current) markRead(current); }
  cycle();
  modalRoot.querySelector(".chat-composer textarea")?.focus();
}

/* The floating button: always there, stacked above the Operator's. */
const fab = node("button", null, "chat-fab"); fab.type = "button";
fab.title = "Chat 열기"; fab.setAttribute("aria-label", "Chat 열기");
fab.append(node("span", "✉", "chat-fab-icon"), node("span", "Chat", "chat-fab-label"));
const fabBadge = node("span", "", "chat-badge-count"); fabBadge.id = "chat-fab-badge"; fabBadge.hidden = true;
fab.append(fabBadge);
fab.onclick = () => modal.open ? modal.close() : openModal();
document.body.append(fab);
// app.js (api, BASE, the login) runs after this file. A timer is not enough
// to wait for it — one can fire between two scripts while the next is still
// being fetched — but DOMContentLoaded comes after every script has run.
const start = () => { if (!timer) cycle(); };
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, {once: true});
else setTimeout(start, 0);

return {open, stop, openModal,
        _test: {mintKey, identityFrom, unreadOf, grown, alertable, alertPlan, clock}};
})();
