/* The floating Operator (claunch-iw5i8.5): the dock's Operator segment opens
   a dialog over any page with the operator v2 tabs and conversation that the
   Observer page draws (observer_operator.js, one OperatorView per host) and
   an input row of its own. Opened on a session's page it selects the operator
   whose scope holds that session. Alt+O opens and closes it.
   The badge counts the asks waiting across every operator: the ones not
   shown yet when there are any (and the segment turns red), else all that
   are unanswered. Creating, scoping, starting and stopping operators stay on
   the Observer page, which also hides this segment: its operators are there
   already. */
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
async function openModal() {
  if (!mounted) { view.mount(root); mounted = true; }
  if (!modal.open) modal.showModal();
  await view.focusSession(sessionOnScreen());
  setBadge(view.summary());
  const area = root.querySelector(".op-area");
  if (area) area.scrollTop = area.scrollHeight;
  view.focusInput();
  clearInterval(timer);
  timer = setInterval(tick, POLL_OPEN);
}
modal.addEventListener("close", () => { clearInterval(timer); timer = setInterval(tick, POLL_BADGE); refreshBadge(); });
/* Open, the view reads the selected operator and its feed (and marks shown
   asks seen); closed, only the list is read, for the badge. */
async function tick() {
  if (document.visibilityState !== "visible") return;
  if (modal.open) { await view.refresh(); setBadge(view.summary()); }
  else refreshBadge();
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
const start = () => { if (!timer) { refreshBadge(); timer = setInterval(tick, POLL_BADGE); } };
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, {once: true});
else setTimeout(start, 0);

return {openModal, refreshBadge, _test: {sessionOnScreen, setBadge}};
})();
