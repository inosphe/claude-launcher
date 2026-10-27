/* The floating Operator (claunch-iw5i8.5): the dock's Operator segment opens
   a dialog over any page with the operator v2 tabs and conversation that the
   Observer page draws (observer_operator.js, one OperatorView per host) and
   an input row of its own. Opened on a session's page it selects the operator
   whose scope holds that session. Alt+O opens and closes it; Escape, the ×
   button and a click on the backdrop close it.
   The badge counts the asks waiting across every operator: the ones not
   shown yet when there are any (and the segment turns red), else all that
   are unanswered. Creating, scoping, starting and stopping operators stay on
   the Observer page, which also hides this segment: its operators are there
   already.
   The dialog's size is the reader's (claunch-iw5i8.14): handles on its edges
   and corners resize it about its centre, so it stays in the middle of the
   screen, and the size is kept in localStorage for the next time it opens.
   A double click on a handle returns it to the default size. On a phone the
   dialog takes the screen and has no handles. */
globalThis.OperatorFab = (() => {
"use strict";
const node = (tag, text, cls) => { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; };
const POLL_OPEN = 10000, POLL_BADGE = 15000;

const modal = node("dialog", null, "opfab-modal"); modal.setAttribute("aria-label", "Operator");
const close = node("button", "×", "opfab-close"); close.type = "button";
close.setAttribute("aria-label", "닫기"); close.title = "닫기 (Esc)";
close.onclick = () => modal.close();
const root = node("div"); root.id = "opfab-view";
modal.append(close, root);
document.body.append(modal);

/* -- the size ------------------------------------------------------------ */
const SIZE_KEY = "claunch-fab-operator-size";
const MIN_W = 360, MIN_H = 320;
// The room a modal dialog gets: the CSS keeps it 32px narrower and 48px
// shorter than the screen, and the browser's own rule 2em + 6px.
const room = () => ({w: Math.max(0, innerWidth - 40), h: Math.max(0, innerHeight - 48)});
const clamp = (v, lo, hi) => Math.round(Math.min(Math.max(v, Math.min(lo, hi)), hi));
function readSize() {
  try {
    const v = JSON.parse(localStorage.getItem(SIZE_KEY) || "null");
    if(v && Number.isFinite(v.w) && Number.isFinite(v.h) && v.w > 0 && v.h > 0) return {w: v.w, h: v.h};
  } catch { /* blocked or damaged: the default size */ }
  return null;
}
function writeSize(value) {
  try { value ? localStorage.setItem(SIZE_KEY, JSON.stringify(value)) : localStorage.removeItem(SIZE_KEY); }
  catch { /* the size lasts until the page reloads */ }
}
// The CSS caps the size at the screen, so a stored size wider than this
// screen shows as wide as it can and is kept for a larger one.
function applySize(value) {
  if(!value) { modal.style.removeProperty("--opfab-w"); modal.style.removeProperty("--opfab-h"); return; }
  modal.style.setProperty("--opfab-w", `${Math.max(value.w, MIN_W)}px`);
  modal.style.setProperty("--opfab-h", `${Math.max(value.h, MIN_H)}px`);
}
applySize(readSize());
// Edge handles: the side moves by the pointer's travel and the opposite side
// by the same amount the other way, which keeps the dialog centred.
for(const edge of ["n", "s", "e", "w", "ne", "nw", "se", "sw"]) {
  const grip = node("div", null, `opfab-grip opfab-grip-${edge}`);
  grip.dataset.edge = edge;
  grip.title = "끌어서 크기 조절 · 두 번 눌러 기본 크기";
  grip.setAttribute("aria-hidden", "true");
  const sx = edge.includes("e") ? 1 : edge.includes("w") ? -1 : 0;
  const sy = edge.includes("s") ? 1 : edge.includes("n") ? -1 : 0;
  let start = null;
  grip.addEventListener("pointerdown", event => {
    if(event.button !== 0) return;
    event.preventDefault();
    const box = modal.getBoundingClientRect();
    start = {x: event.clientX, y: event.clientY, w: box.width, h: box.height};
    grip.setPointerCapture(event.pointerId);
    modal.classList.add("opfab-resizing");
  });
  grip.addEventListener("pointermove", event => {
    if(!start) return;
    const r = room();
    applySize({
      w: clamp(start.w + sx * 2 * (event.clientX - start.x), MIN_W, r.w),
      h: clamp(start.h + sy * 2 * (event.clientY - start.y), MIN_H, r.h),
    });
  });
  const end = () => {
    if(!start) return;
    start = null;
    modal.classList.remove("opfab-resizing");
    const box = modal.getBoundingClientRect();
    writeSize({w: Math.round(box.width), h: Math.round(box.height)});
  };
  grip.addEventListener("pointerup", end);
  grip.addEventListener("pointercancel", end);
  grip.addEventListener("dblclick", () => { writeSize(null); applySize(null); });
  modal.append(grip);
}
const view = OperatorView.create({
  key: "claunch-fab-operator", composer: true,
  active: () => modal.open,
  scroller: () => root.querySelector(".op-area"),
});
// A session link inside the dialog opens that session's page; the dialog
// would otherwise stay over it.
root.addEventListener("click", event => {
  if (event.target.closest?.('a[href^="#/"]')) modal.close();
});
// A click on the backdrop closes the dialog (claunch-ho56l). The backdrop has
// no element of its own: a click there lands on the dialog itself, outside
// its box. A press that starts inside (selecting text, dragging a handle) and
// is let go outside also lands on the dialog, so the press must start
// outside too.
const outside = event => {
  const box = modal.getBoundingClientRect();
  return event.target === modal && (event.clientX < box.left || event.clientX > box.right
    || event.clientY < box.top || event.clientY > box.bottom);
};
let pressedOutside = false;
modal.addEventListener("pointerdown", event => { pressedOutside = outside(event); });
modal.addEventListener("click", event => {
  if (pressedOutside && outside(event)) modal.close();
  pressedOutside = false;
});

/* The dock: one control in the corner that Chat (chat.js) joins too. */
function fabDock() {
  let dock = document.getElementById("fab-dock");
  if (!dock) {
    dock = node("div", null, "fab-dock"); dock.id = "fab-dock";
    dock.setAttribute("role", "group"); dock.setAttribute("aria-label", "빠른 열기");
    document.body.append(dock);
  }
  return dock;
}
const fab = node("button", null, "operator-fab"); fab.type = "button";
fab.title = "Operator 열기 (Alt+O)"; fab.setAttribute("aria-label", "Operator 열기 (Alt+O)");
fab.append(node("span", "◍", "operator-fab-icon"), node("span", "Operator", "operator-fab-label"));
const badge = node("span", "", "operator-fab-count"); badge.id = "operator-fab-badge"; badge.hidden = true;
fab.append(badge);
fab.onclick = () => modal.open ? modal.close() : openModal();
fabDock().append(fab);
document.addEventListener("keydown", event => {
  if (event.altKey && !event.ctrlKey && !event.metaKey && (event.key === "o" || event.key === "O")) {
    event.preventDefault();
    modal.open ? modal.close() : openModal();
  }
});

function setBadge({pending, unseen}) {
  const total = unseen || pending;
  badge.textContent = total ? String(total) : "";
  badge.hidden = !total;
  badge.title = total ? `답변을 기다리는 질문 ${pending}개${unseen ? ` · 새 질문 ${unseen}개` : ""}` : "";
  fab.classList.toggle("operator-fab-alert", unseen > 0);
}
/* The session whose page is on screen, from the route (#/s/<name>). */
function sessionOnScreen() {
  const match = /^#\/s\/([^/?]+)/.exec(location.hash || "");
  if (!match) return "";
  try { return decodeURIComponent(match[1]); } catch { return ""; }
}
let mounted = false, timer = null;
const mountView = () => { if (!mounted) { view.mount(root); mounted = true; } };
const toEnd = () => { const area = root.querySelector(".op-area"); if (area) area.scrollTop = area.scrollHeight; };
/* The dialog opens on what the closed-dialog poll last read (tick, below),
   so it shows at once; the read for the session on screen follows. */
async function openModal() {
  mountView();
  if (!modal.open) modal.showModal();
  toEnd();
  view.focusInput();
  clearInterval(timer);
  timer = setInterval(tick, POLL_OPEN);
  await view.focusSession(sessionOnScreen());
  setBadge(view.summary());
  toEnd();
}
modal.addEventListener("close", () => { clearInterval(timer); timer = setInterval(tick, POLL_BADGE); });
/* Open or closed, the view reads the list and the selected operator's
   conversation (claunch-fokj3): the read is the conversation alone
   (panel=0, under 1ms of the daemon's time), and keeping it current while
   closed is what lets the dialog open with its content instead of an empty
   frame. Before the first open the view is not mounted: the read is kept
   and drawn when the dialog mounts it, and the page holds no second copy
   of the conversation the Observer page draws. Only an open dialog marks
   the shown asks seen (opts.active). On the Observer page, which hides
   this segment, only the list is read, for the badge -- and so it is
   everywhere once a read came back with the session panel: that daemon
   predates panel=0 and each read costs it seconds, so the dialog reads
   only when opened until the daemon is restarted. */
async function tick() {
  if (document.visibilityState !== "visible") return;
  if (!modal.open && (document.body.classList.contains("observer-active") || !view.lightReads())) return refreshBadge();
  await view.refresh();
  setBadge(view.summary());
}
async function refreshBadge() {
  try {
    const response = await api("api/operators");
    if (!response.ok) return;
    const data = await response.json();
    const sum = {pending: 0, unseen: 0};
    for (const o of data.operators || []) { sum.pending += o.pending || 0; sum.unseen += o.unseen || 0; }
    setBadge(sum);
  } catch { /* the next tick tries again */ }
}
// app.js (api, the login) runs after this file; DOMContentLoaded comes after
// every script has run.
const start = () => { if (!timer) { tick(); timer = setInterval(tick, POLL_BADGE); } };
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, {once: true});
else setTimeout(start, 0);

return {openModal, refreshBadge, _test: {sessionOnScreen, setBadge, readSize, SIZE_KEY}};
})();
