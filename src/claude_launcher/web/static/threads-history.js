/* All threads page (#/threads, claunch-8k02k): every session's thread
   conversations on one screen, most recently active first. The threads pane
   beside a terminal shows one session's threads; this page reads the same
   store (GET /api/threads/history) across sessions, a page of threads at a
   time, so older conversations sit behind the pager instead of all loading
   at once. Read-only: writing stays in the session's threads pane. */
(() => {
  const state = { open: false, page: 1, session: "", status: "", data: null, error: "", seq: 0 };
  const node = (tag, cls, text) => {
    const value = document.createElement(tag);
    if (cls) value.className = cls;
    if (text !== undefined) value.textContent = String(text);
    return value;
  };
  const when = (iso) => {
    const d = new Date(iso);
    return Number.isNaN(d.valueOf()) ? String(iso || "") : d.toLocaleString();
  };
  // app.js's markdown renderer, when it is loaded (it is on the dashboard);
  // plain text otherwise, never HTML.
  const body = (box, text) => {
    if (typeof globalThis.mdInto === "function") return globalThis.mdInto(box, text);
    box.textContent = text;
    return box;
  };

  async function load() {
    const seq = ++state.seq;
    const q = new URLSearchParams({ page: String(state.page) });
    if (state.session) q.set("session", state.session);
    if (state.status) q.set("status", state.status);
    try {
      const resp = await globalThis.api(`/api/threads/history?${q}`);
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
      if (seq !== state.seq || !state.open) return;
      state.data = data;
      state.error = "";
      // A filter can shrink the result under the page we were on.
      if (data.page > data.pages) { state.page = data.pages; return load(); }
    } catch (error) {
      if (seq !== state.seq) return;
      state.error = error.message || String(error);
    }
    if (state.open) render();
  }

  function go(page) {
    state.page = Math.max(1, page);
    load();
  }

  function controls(data) {
    const bar = node("div", "stats-controls thh-controls");
    bar.append(node("h2", "", "Threads"));
    const who = node("select", "thh-session");
    who.append(new Option("all sessions", ""));
    for (const s of (data && data.sessions) || []) {
      who.append(new Option(`${s.session} (${s.threads})`, s.session));
    }
    who.value = state.session;
    who.addEventListener("change", () => { state.session = who.value; go(1); });
    const status = node("select", "thh-status");
    for (const [v, label] of [["", "open + closed"], ["open", "open"], ["closed", "closed"]]) {
      status.append(new Option(label, v));
    }
    status.value = state.status;
    status.addEventListener("change", () => { state.status = status.value; go(1); });
    const reload = node("button", "wf-btn clear", "Refresh");
    reload.type = "button";
    reload.addEventListener("click", load);
    bar.append(who, status, reload);
    return bar;
  }

  function pager(data) {
    const nav = node("div", "thh-pager");
    const prev = node("button", "wf-btn clear", "← newer");
    prev.type = "button";
    prev.disabled = data.page <= 1;
    prev.addEventListener("click", () => go(data.page - 1));
    const next = node("button", "wf-btn clear", "older →");
    next.type = "button";
    next.disabled = data.page >= data.pages;
    next.addEventListener("click", () => go(data.page + 1));
    nav.append(prev, node("span", "wf-note", `page ${data.page} / ${data.pages} · ${data.total} threads`), next);
    return nav;
  }

  function card(t) {
    const box = node("section", "window-section thh-thread");
    box.dataset.thread = t.id;
    const head = node("div", "thh-head");
    const sess = node("a", "thh-sess", t.session);
    sess.href = "#/s/" + encodeURIComponent(t.session);
    sess.title = "open this session";
    head.append(sess, node("strong", "thh-title", t.title),
      node("span", `thh-state thh-${t.status}`, t.status),
      node("span", "wf-note thh-when", when(t.updated_at)));
    box.append(head);
    if (t.omitted > 0) {
      box.append(node("p", "wf-note", `${t.omitted} older message(s) not shown — the session's threads pane has the whole thread.`));
    }
    if (!t.messages.length) box.append(node("p", "wf-note", "no messages yet"));
    for (const m of t.messages) {
      const msg = node("div", `thh-msg thh-${m.author}`);
      msg.dataset.seq = m.seq;
      const meta = node("div", "thh-meta");
      meta.append(node("span", "thh-who", m.author === "user" ? "user" : t.session),
        node("span", "thh-at", when(m.at)));
      msg.append(meta, body(node("div", "thh-body"), m.body));
      box.append(msg);
    }
    return box;
  }

  function render() {
    const view = document.getElementById("threads-view");
    if (!view) return;
    const data = state.data;
    view.replaceChildren(controls(data));
    view.append(node("p", "wf-note",
      "Every session's thread conversations, most recently active first. Reply from the session's threads pane."));
    if (state.error) view.append(node("p", "wf-warning", state.error));
    if (!data) { view.append(node("p", "wf-note", "Loading…")); return; }
    if (!data.threads.length) {
      view.append(node("p", "wf-note", "No threads."));
      return;
    }
    view.append(pager(data));
    for (const t of data.threads) view.append(card(t));
    if (data.pages > 1) view.append(pager(data));
  }

  function open() {
    state.open = true;
    render();
    load();
  }

  function stop() { state.open = false; }
  globalThis.ThreadsHistoryPage = { open, stop, load, render, state };
})();
