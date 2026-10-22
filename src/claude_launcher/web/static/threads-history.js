/* All threads page (#/threads, claunch-8k02k): every session's thread
   conversations on one screen. The threads pane beside a terminal shows one
   session's threads; this page reads the same store across sessions.
   tree and newest carry a composer under the thread shown (claunch-1qj0b),
   the same post as the session's threads pane: text, and images pasted,
   dropped or picked (🖼), each staged first. feed stays read-only.

   Three view modes (claunch-zny1o), each its own route:
   - tree   (#/threads, #/threads/tree/<thread>): a list panel of sessions,
     each with its threads under it; the thread picked shows on the right.
   - newest (#/threads/newest): the list panel lists every thread, most
     recently active first, and the right side shows only the newest one.
   - feed   (#/threads/feed): every thread with its conversation as cards,
     a page at a time (GET /api/threads/history).
   tree and newest read the list from GET /api/threads/index (no message
   bodies) and one conversation from GET /api/threads/<id>, which reads
   without moving any read position.

   Every mode keeps itself current (claunch-8vofx): while the page is open
   and its tab is shown, a poll every POLL_MS asks the same route with
   `since=<stamp>`, the store's change stamp from the last answer. Nothing
   changed is a few bytes and no redraw; a change brings the list (or the
   feed page), redrawn only when it differs, and the conversation on the
   right is read again only when its row moved. A poll does not run over a
   read or a post of the page's own, and a redraw keeps the list's scroll
   and the composer's caret. */
(() => {
  const POLL_MS = 3000;
  const VIEWS = [
    ["tree", "Sessions › threads", "Sessions with their threads in a list panel; pick a thread to read it."],
    ["newest", "Newest thread", "Only the most recently active thread, with the thread list beside it."],
    ["feed", "Feed", "Every thread with its conversation, most recently active first, a page at a time."],
  ];
  const isView = (v) => VIEWS.some(([name]) => name === v);
  const state = {
    open: false, view: "tree", selected: "", page: 1, session: "", status: "",
    data: null, index: null, detail: null, error: "", detailError: "",
    collapsed: new Set(), seq: 0, detailSeq: 0,
    // The poll: the change stamp of the last answer and what it was asked
    // with (mode, filters, page), the timer, and reads of the page's own
    // still out.
    stamp: "", stampKey: "", timer: null, busy: 0,
    // The composer, per thread: typed text, images waiting to be sent
    // ([{file, src}]), posts still out, and what the last post said.
    drafts: new Map(), attached: new Map(), sending: new Set(), notice: null, refocus: "",
  };
  // The threads pane's helpers (threads-pane.js loads first on the dashboard).
  const pane = () => globalThis.ThreadsPane || {};
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
  const route = (view, id) => "#/threads/" + view + (id ? "/" + encodeURIComponent(id) : "");
  // Moving between modes and threads goes through the address, so Back and a
  // shared link land on the same view; app.js's router calls open() again.
  function go(view, id) {
    const hash = route(view, id);
    if (globalThis.location && globalThis.location.hash !== hash) globalThis.location.hash = hash;
    else open(view, id);
  }
  const filters = () => {
    const q = new URLSearchParams();
    if (state.session) q.set("session", state.session);
    if (state.status) q.set("status", state.status);
    return q;
  };
  // The thread the right side shows: the picked one in tree, the newest in newest.
  const target = () => (state.view === "newest"
    ? ((state.index && state.index.threads[0]) || {}).id || ""
    : state.selected);

  async function fetchJson(url) {
    const resp = await globalThis.api(url);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
  }

  // What a list answer depends on: the mode's route and its query.
  const listQuery = () => (state.view === "feed"
    ? new URLSearchParams([["page", String(state.page)], ...filters()])
    : filters());
  const listUrl = (q) => (state.view === "feed" ? "/api/threads/history" : "/api/threads/index")
    + (q.toString() ? `?${q}` : "");
  const queryKey = () => `${state.view === "feed" ? "feed" : "index"}?${listQuery()}`;
  function keep(data) {
    state.stamp = (data && data.stamp) || "";
    state.stampKey = queryKey();
  }

  async function load() {
    if (state.view === "feed") return loadFeed();
    const seq = ++state.seq;
    state.busy++;
    try {
      const data = await fetchJson(listUrl(listQuery()));
      if (seq !== state.seq || !state.open) return;
      state.index = data;
      keep(data);
      state.error = "";
    } catch (error) {
      if (seq !== state.seq) return;
      state.error = error.message || String(error);
    } finally {
      state.busy--;
    }
    if (!state.open) return;
    render();
    return loadDetail();
  }

  async function loadFeed() {
    const seq = ++state.seq;
    state.busy++;
    try {
      const data = await fetchJson(listUrl(listQuery()));
      if (seq !== state.seq || !state.open) return;
      state.data = data;
      keep(data);
      state.error = "";
      // A filter can shrink the result under the page we were on.
      if (data.page > data.pages) { state.page = data.pages; return loadFeed(); }
    } catch (error) {
      if (seq !== state.seq) return;
      state.error = error.message || String(error);
    } finally {
      state.busy--;
    }
    if (state.open) render();
  }

  // Whether the conversation shown is behind its row in the list.
  function detailStale() {
    const id = target();
    if (!id) return !!state.detail;
    const d = state.detail;
    if (!d || d.thread.id !== id) return true;
    const row = ((state.index && state.index.threads) || []).find(t => t.id === id);
    if (!row) return false;     // filtered out of the list: nothing says it moved
    const t = d.thread;
    return row.last_seq !== t.last_seq || row.updated_at !== t.updated_at
      || row.status !== t.status || row.title !== t.title;
  }

  // One poll tick (claunch-8vofx). Skipped while a read or a post of the
  // page's own is out: that one redraws anyway, and the next tick sees it.
  async function poll() {
    if (!state.open || state.busy || state.sending.size) return;
    const seq = state.seq;
    const q = listQuery();
    const key = queryKey();
    if (state.stamp && state.stampKey === key) q.set("since", state.stamp);
    let data;
    try {
      data = await fetchJson(listUrl(q));
    } catch (error) {
      if (seq !== state.seq || !state.open) return;
      const text = error.message || String(error);
      if (text !== state.error) { state.error = text; render(); }
      return;
    }
    // A read of the page's own started meanwhile: its answer is the newer one.
    if (seq !== state.seq || !state.open || key !== queryKey()) return;
    const cleared = !!state.error;
    state.error = "";
    if (data.unchanged) {
      if (cleared) render();
      return;
    }
    const before = state.view === "feed" ? state.data : state.index;
    const same = !!before && JSON.stringify({ ...before, stamp: "" }) === JSON.stringify({ ...data, stamp: "" });
    keep(data);
    if (state.view === "feed") {
      state.data = data;
      if (data.page > data.pages) { state.page = data.pages; return loadFeed(); }
      if (!same || cleared) render();
      return;
    }
    state.index = data;
    if (!same || cleared) render();
    if (detailStale()) return loadDetail();
  }

  function startPoll() {
    if (state.timer || typeof setInterval !== "function") return;
    // app.js's visiblePoll skips a hidden tab and drops a tick that would
    // stack on a read still out; without it, the hidden check alone.
    const tick = typeof globalThis.visiblePoll === "function"
      ? globalThis.visiblePoll(poll, () => state.timer)
      : () => (typeof document !== "undefined" && document.hidden ? undefined : poll());
    state.timer = setInterval(tick, POLL_MS);
  }

  async function loadDetail() {
    const id = target();
    const seq = ++state.detailSeq;
    if (!id) { state.detail = null; state.detailError = ""; if (state.open) render(); return; }
    if (state.detail && state.detail.thread.id !== id) state.detail = null;
    try {
      const data = await fetchJson(`/api/threads/${encodeURIComponent(id)}`);
      if (seq !== state.detailSeq || !state.open) return;
      state.detail = data;
      state.detailError = "";
    } catch (error) {
      if (seq !== state.detailSeq) return;
      state.detail = null;
      state.detailError = error.message || String(error);
    }
    if (state.open) render();
  }

  // One image to the staging area (the pane's stage()); its id names it in the post.
  async function stage(file) {
    const resp = await globalThis.api("/api/threads/images", {
      method: "POST", headers: { "Content-Type": pane().imageKind(file) }, body: file,
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.id) throw new Error(data.error || `이미지 업로드 실패 (HTTP ${resp.status})`);
    return data.id;
  }

  function dropAttached(tid) {
    for (const a of state.attached.get(tid) || []) {
      try { URL.revokeObjectURL(a.src); } catch { /* no object URLs here */ }
    }
    state.attached.delete(tid);
  }

  // Image files for thread `tid`'s composer; what is not an image is left
  // out. Nothing is added while a post is out: it sends what it took.
  function attach(tid, files) {
    if (state.sending.has(tid)) return;
    const list = Array.from(files || []);
    const pics = list.filter(f => pane().imageKind(f));
    state.notice = list.length > pics.length
      ? { tid, text: `이미지가 아닌 파일 ${list.length - pics.length}개는 첨부하지 않았습니다.` } : null;
    const have = state.attached.get(tid) || [];
    for (const file of pics) {
      let src = "";
      try { src = URL.createObjectURL(file); } catch { /* no object URLs here */ }
      have.push({ file, src });
    }
    if (have.length) state.attached.set(tid, have);
    render();
  }

  // Post what thread `t`'s composer holds. The text and the images stay in
  // the composer until the post is in, so a failure keeps them for a retry.
  async function send(t) {
    const tid = t.id;
    const text = (state.drafts.get(tid) || "").trim();
    const pics = state.attached.get(tid) || [];
    if (state.sending.has(tid) || (!text && !pics.length)) return;
    state.sending.add(tid);
    state.notice = null;
    render();
    try {
      const body = { text };
      const ids = [];
      for (const a of pics) ids.push(await stage(a.file));
      if (ids.length) body.images = ids;
      const resp = await globalThis.api(`/api/threads/${encodeURIComponent(tid)}/messages`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      const res = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(res.error || `HTTP ${resp.status}`);
      state.drafts.delete(tid);
      dropAttached(tid);
      const said = pane().deliveryNotice ? pane().deliveryNotice(res, t.session) : "";
      state.notice = said ? { tid, text: said } : null;
    } catch (error) {
      state.notice = { tid, text: error.message || String(error) };
    } finally {
      state.sending.delete(tid);
      state.refocus = tid;     // the box was disabled while the post was out
    }
    if (!state.open) return;
    render();
    return loadDetail();
  }

  function refilter() {
    state.page = 1;
    load();
  }

  function modes() {
    const bar = node("div", "thh-modes");
    bar.setAttribute?.("role", "tablist");
    for (const [name, label] of VIEWS) {
      const tab = node("button", "thh-mode" + (state.view === name ? " active" : ""), label);
      tab.type = "button";
      tab.dataset.view = name;
      tab.setAttribute?.("role", "tab");
      tab.setAttribute?.("aria-selected", String(state.view === name));
      tab.addEventListener("click", () => go(name, name === "tree" ? state.selected : ""));
      bar.append(tab);
    }
    return bar;
  }

  function controls() {
    const bar = node("div", "stats-controls thh-controls");
    bar.append(node("h2", "", "Threads"), modes());
    const who = node("select", "thh-session");
    who.append(new Option("all sessions", ""));
    // Every session with threads, whatever the filters keep: picking one
    // must not take the others out of the list.
    const all = state.view === "feed"
      ? state.data && state.data.sessions
      : state.index && state.index.all_sessions;
    for (const s of all || []) {
      who.append(new Option(`${s.session} (${s.threads})`, s.session));
    }
    who.value = state.session;
    who.addEventListener("change", () => { state.session = who.value; refilter(); });
    const status = node("select", "thh-status");
    for (const [v, label] of [["", "open + closed"], ["open", "open"], ["closed", "closed"]]) {
      status.append(new Option(label, v));
    }
    status.value = state.status;
    status.addEventListener("change", () => { state.status = status.value; refilter(); });
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
    prev.addEventListener("click", () => { state.page = Math.max(1, data.page - 1); loadFeed(); });
    const next = node("button", "wf-btn clear", "older →");
    next.type = "button";
    next.disabled = data.page >= data.pages;
    next.addEventListener("click", () => { state.page = data.page + 1; loadFeed(); });
    nav.append(prev, node("span", "wf-note", `page ${data.page} / ${data.pages} · ${data.total} threads`), next);
    return nav;
  }

  function sessionLink(session) {
    const sess = node("a", "thh-sess", session);
    sess.href = "#/s/" + encodeURIComponent(session);
    sess.title = "open this session";
    return sess;
  }

  function message(m, session) {
    const msg = node("div", `thh-msg thh-${m.author}`);
    msg.dataset.seq = m.seq;
    const meta = node("div", "thh-meta");
    // A guest writes under its own name (claunch-p9wes); a daemon notice says
    // so rather than passing as the session's (claunch-p9wes.1).
    const who = m.author === "user" ? "user"
      : m.author === "guest" ? `${m.guest || "guest"} (guest)`
        : m.author === "system" ? "daemon" : session;
    meta.append(node("span", "thh-who", who),
      node("span", "thh-at", when(m.at)));
    if (m.body) msg.append(meta, body(node("div", "thh-body"), m.body));
    else msg.append(meta);
    // An agent's ask (thread_ask, claunch-etw0s): this page is read-only,
    // so it shows the answer state instead of the pane's answer controls.
    if (m.kind === "ask") {
      const data = m.data || {};
      const answer = data.answer || {};
      const said = data.type === "approve" ? (answer.decision === "approve" ? "승인" : "거절")
        : data.type === "choice" ? answer.decision : null;
      meta.append(node("span", answer.at ? "thh-ask-answered" : "thh-ask-waiting",
                       answer.at ? ["사용자 답변", said].filter(Boolean).join(" · ") : "답변 대기"));
      if (answer.at && answer.text) msg.append(body(node("div", "thh-body thh-answer"), answer.text));
    }
    const pics = images(m);
    if (pics) msg.append(pics);
    return msg;
  }

  // The images the user attached to a message (claunch-shl2r), each a link
  // to the full picture. Served by the daemon with the page's own login.
  function images(m) {
    if (!(m.images || []).length) return null;
    const box = node("div", "th-images");
    for (const img of m.images) {
      const path = `api/threads/${encodeURIComponent(m.thread)}/images/${encodeURIComponent(img.name)}`;
      const link = node("a", "th-image");
      link.href = typeof globalThis.url === "function" ? globalThis.url(path) : path;
      link.target = "_blank";
      link.rel = "noopener";
      const pic = node("img");
      pic.src = link.href;
      pic.alt = "첨부 이미지";
      pic.loading = "lazy";
      link.append(pic);
      box.append(link);
    }
    return box;
  }

  function card(t) {
    const box = node("section", "window-section thh-thread");
    box.dataset.thread = t.id;
    const head = node("div", "thh-head");
    head.append(sessionLink(t.session), node("strong", "thh-title", t.title),
      node("span", `thh-state thh-${t.status}`, t.status),
      node("span", "wf-note thh-when", when(t.updated_at)));
    box.append(head);
    if (t.omitted > 0) {
      box.append(node("p", "wf-note", `${t.omitted} older message(s) not shown — the session's threads pane has the whole thread.`));
    }
    if (!t.messages.length) box.append(node("p", "wf-note", "no messages yet"));
    for (const m of t.messages) box.append(message(m, t.session));
    return box;
  }

  // One thread row of the list panel. In tree it picks that thread; in
  // newest the list is there to read, and a row opens the thread in tree.
  function threadRow(t, { withSession = false, newest = false } = {}) {
    const row = node("button", "thh-row thh-trow" + (t.id === target() ? " active" : ""));
    row.type = "button";
    row.dataset.thread = t.id;
    const line = node("div", "thh-rowline");
    if (newest) line.append(node("span", "thh-badge", "newest"));
    if (withSession) line.append(node("span", "thh-sess", t.session));
    line.append(node("span", "thh-title", t.title), node("span", `thh-state thh-${t.status}`, t.status));
    row.append(line);
    const sub = node("div", "thh-rowsub wf-note");
    sub.append(node("span", "thh-excerpt", t.excerpt || "no messages yet"),
      node("span", "thh-when", when(t.updated_at)));
    row.append(sub);
    row.addEventListener("click", () => go("tree", t.id));
    return row;
  }

  function treeList(index) {
    const list = node("nav", "thh-list thh-tree");
    const bySession = new Map(index.sessions.map(s => [s.session, []]));
    for (const t of index.threads) bySession.get(t.session)?.push(t);
    for (const s of index.sessions) {
      const group = node("div", "thh-group");
      group.dataset.session = s.session;
      const shut = state.collapsed.has(s.session);
      const head = node("button", "thh-row thh-srow" + (shut ? " collapsed" : ""));
      head.type = "button";
      head.setAttribute?.("aria-expanded", String(!shut));
      head.append(node("span", "thh-caret", shut ? "▸" : "▾"), node("span", "thh-sess", s.session),
        node("span", "wf-note thh-count", `${s.threads} thread(s) · ${s.open} open`));
      head.addEventListener("click", () => {
        if (state.collapsed.has(s.session)) state.collapsed.delete(s.session);
        else state.collapsed.add(s.session);
        render();
      });
      group.append(head);
      if (!shut) {
        const kids = node("div", "thh-kids");
        for (const t of bySession.get(s.session) || []) kids.append(threadRow(t));
        group.append(kids);
      }
      list.append(group);
    }
    return list;
  }

  function flatList(index) {
    const list = node("nav", "thh-list thh-flat");
    index.threads.forEach((t, i) => list.append(threadRow(t, { withSession: true, newest: i === 0 })));
    return list;
  }

  // The composer under the thread shown: Enter sends, Shift+Enter is a new
  // line; images come in by paste, drop or the 🖼 button.
  function composer(t) {
    const tid = t.id;
    const sending = state.sending.has(tid);
    const box = node("div", "th-compose thh-compose");
    box.dataset.thread = tid;
    const strip = node("div", "th-attach");
    const pics = state.attached.get(tid) || [];
    strip.hidden = !pics.length;
    pics.forEach((a, i) => {
      const item = node("span", "th-attach-item");
      const pic = node("img");
      pic.src = a.src;
      pic.alt = a.file.name || "첨부 이미지";
      const drop = node("button", "th-attach-drop", "×");
      drop.type = "button";
      drop.title = "첨부 취소";
      drop.disabled = sending;
      drop.addEventListener("click", () => {
        const rest = pics.filter((_, j) => j !== i);
        try { URL.revokeObjectURL(a.src); } catch { /* no object URLs here */ }
        if (rest.length) state.attached.set(tid, rest); else state.attached.delete(tid);
        render();
      });
      item.append(pic, drop);
      strip.append(item);
    });
    const text = node("textarea", "th-text");
    text.rows = 2;
    text.placeholder = `${t.session}에게 보내기`;
    text.value = state.drafts.get(tid) || "";
    text.disabled = sending;
    text.addEventListener("input", () => state.drafts.set(tid, text.value));
    text.addEventListener("keydown", (e) => {
      if (pane().sendKey && pane().sendKey(e)) { e.preventDefault(); send(t); }
    });
    text.addEventListener("paste", (e) => {
      const files = pane().imageFiles ? pane().imageFiles(e.clipboardData) : [];
      if (!files.length) return;
      e.preventDefault();
      attach(tid, files);
    });
    box.addEventListener("dragover", (e) => {
      if (!Array.from((e.dataTransfer || {}).types || []).includes("Files")) return;
      e.preventDefault();
      box.classList.add("dragover");
    });
    box.addEventListener("dragleave", () => box.classList.remove("dragover"));
    box.addEventListener("drop", (e) => {
      box.classList.remove("dragover");
      const files = pane().imageFiles ? pane().imageFiles(e.dataTransfer) : [];
      if (!files.length) return;
      e.preventDefault();
      attach(tid, files);
    });
    const input = node("input");
    input.type = "file";
    input.accept = "image/png,image/jpeg,image/gif,image/webp";
    input.multiple = true;
    input.hidden = true;
    input.addEventListener("change", () => { attach(tid, input.files); input.value = ""; });
    const pick = node("button", "term-btn th-pick", "🖼");
    pick.type = "button";
    pick.title = "이미지 첨부 — 입력창에 붙여넣거나(Ctrl+V) 끌어다 놓아도 됩니다";
    pick.disabled = sending;
    pick.addEventListener("click", () => input.click());
    const go = node("button", "term-btn th-send", sending ? "전송 중…" : "보내기");
    go.type = "button";
    go.disabled = sending;
    go.addEventListener("click", () => send(t));
    const foot = node("div", "th-compose-foot");
    foot.append(pick, input, node("span", "th-hint", "Enter 전송 · Shift+Enter 줄바꿈 · markdown 지원 · 이미지 붙여넣기"), go);
    box.append(strip, text, foot);
    return box;
  }

  function detail() {
    const box = node("section", "thh-detail");
    const id = target();
    if (state.detailError) box.append(node("p", "wf-warning", state.detailError));
    if (!id) {
      box.append(node("p", "wf-note", state.view === "newest"
        ? "No threads." : "Pick a thread in the list to read it."));
      return box;
    }
    const d = state.detail;
    if (!d || d.thread.id !== id) {
      if (!state.detailError) box.append(node("p", "wf-note", "Loading…"));
      return box;
    }
    const t = d.thread;
    box.dataset.thread = t.id;
    const head = node("div", "thh-head");
    if (state.view === "newest") head.append(node("span", "thh-badge", "newest"));
    head.append(sessionLink(t.session), node("strong", "thh-title", t.title),
      node("span", `thh-state thh-${t.status}`, t.status),
      node("span", "wf-note thh-when", when(t.updated_at)));
    box.append(head);
    const msgs = node("div", "thh-msgs");
    if (!d.messages.length) msgs.append(node("p", "wf-note", "no messages yet"));
    for (const m of d.messages) msgs.append(message(m, t.session));
    box.append(msgs);
    if (d.more) {
      box.append(node("p", "wf-note", "Later messages not shown — the session's threads pane has the whole thread."));
    }
    box.append(composer(t));
    if (state.notice && state.notice.tid === t.id) box.append(node("p", "wf-warning thh-notice", state.notice.text));
    return box;
  }

  function render() {
    const view = document.getElementById("threads-view");
    if (!view) return;
    // A redraw rebuilds the composer: whoever was typing in it keeps the
    // focus and the caret; the list panel keeps its scroll.
    const was = view.querySelector?.(".thh-compose textarea");
    const typing = !!was && document.activeElement === was;
    const caret = typing ? [was.selectionStart, was.selectionEnd] : null;
    const scrolled = view.querySelector?.(".thh-list")?.scrollTop || 0;
    view.replaceChildren(controls());
    const [, , note] = VIEWS.find(([name]) => name === state.view);
    view.append(node("p", "wf-note thh-modenote",
      note + (state.view === "feed" ? " Reply from Sessions › threads or the session's threads pane." : "")));
    if (state.error) view.append(node("p", "wf-warning", state.error));
    if (state.view === "feed") return renderFeed(view);
    const index = state.index;
    if (!index) { view.append(node("p", "wf-note", "Loading…")); return; }
    if (!index.threads.length) { view.append(node("p", "wf-note", "No threads.")); return; }
    if (index.truncated) {
      view.append(node("p", "wf-note", `Showing the ${index.threads.length} most recent of ${index.total} threads.`));
    }
    const split = node("div", `thh-split thh-split-${state.view}`);
    split.append(state.view === "tree" ? treeList(index) : flatList(index), detail());
    view.append(split);
    const box = view.querySelector?.(".thh-compose textarea");
    if (box && (typing || state.refocus === box.closest?.(".thh-compose")?.dataset.thread)) box.focus();
    if (box && caret && box.value === was.value) {
      try { box.setSelectionRange(caret[0], caret[1]); } catch { /* not a text box here */ }
    }
    state.refocus = "";
    const list = scrolled && view.querySelector?.(".thh-list");
    if (list) list.scrollTop = scrolled;
  }

  function renderFeed(view) {
    const data = state.data;
    if (!data) { view.append(node("p", "wf-note", "Loading…")); return; }
    if (!data.threads.length) {
      view.append(node("p", "wf-note", "No threads."));
      return;
    }
    view.append(pager(data));
    for (const t of data.threads) view.append(card(t));
    if (data.pages > 1) view.append(pager(data));
  }

  function open(view, id) {
    const next = isView(view) ? view : "tree";
    const listed = state.open && next !== "feed" && state.view !== "feed" && state.index;
    state.view = next;
    state.selected = next === "tree" ? (id || state.selected || "") : state.selected;
    state.open = true;
    startPoll();
    if (listed) {
      // Same list, another thread or mode: only the right side changes.
      render();
      loadDetail();
      return;
    }
    render();
    load();
  }

  function stop() {
    state.open = false;
    if (state.timer) clearInterval(state.timer);
    state.timer = null;
  }
  globalThis.ThreadsHistoryPage = { open, stop, load, render, poll, state, route };
})();
