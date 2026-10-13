/* Session threads in the terminal's split pane (🗪 threads) --
   daemon/threads_api.py, claunch-doj98.

   One session, several sub-topic conversations: the list of threads on the
   left, the chosen thread's messages and a composer on the right. What the
   user sends is typed into the session as a "[claunch thread <id>]" block;
   the agent answers into the same thread with the thread_* MCP tools, so
   each topic keeps its own history instead of interleaving in the
   terminal's scrollback.

   The pane polls on its own (3s): the list every tick, the open thread only
   for messages after the last one it holds. A draft is kept per thread for
   the page's lifetime. Which agent messages this browser has seen is kept
   per thread in localStorage, so a thread with an unseen reply carries a
   dot. User text is rendered as text; agent text through the page's
   markdown renderer, which builds nodes. */
globalThis.ThreadsPane = (() => {
  "use strict";
  const POLL_MS = 3000;
  let host = null, name = "", api = null, onChange = null, storageKey = "";
  let timer = null, seq = 0, list = null, error = "";
  let current = "";          // the selected thread id
  let convo = null;          // {id, thread, messages}
  let composing = false;     // the new-thread form is open
  let notice = "";
  const drafts = new Map();  // thread id ("" = new thread) -> text
  const inflight = new Set(); // thread ids ("" = new thread) with a post still out
  const drawn = {side: "", main: ""};   // what each half last drew

  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  /* ---- pure helpers (tests/web/threadspane_check.js) ---- */

  /* The first line of a message, shortened for the list. */
  function excerpt(text, max = 80) {
    const line = String(text || "").split("\n").find((l) => l.trim()) || "";
    return line.length > max ? line.slice(0, max - 1) + "…" : line;
  }

  /* Threads whose last message is the agent's and newer than what this
     browser has seen in them. */
  function unseen(threads, read) {
    const out = new Set();
    for (const t of threads || []) {
      if (t.last && t.last.author === "agent" && t.last_seq > (read[t.id] || 0)) out.add(t.id);
    }
    return out;
  }

  /* Enter sends, Shift+Enter is a new line; an IME composition is never
     a send. */
  function sendKey(e) {
    return e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229;
  }

  /* The thread to show: the remembered one if it still exists, else the
     first open one, else the first. */
  function pick(threads, want) {
    const ts = threads || [];
    if (want && ts.some((t) => t.id === want)) return want;
    const open = ts.find((t) => t.status === "open");
    return (open || ts[0] || {}).id || "";
  }

  /* ---- read positions ---- */

  function loadRead() {
    try {
      const doc = JSON.parse(localStorage.getItem(storageKey) || "{}");
      return doc && typeof doc === "object" && !Array.isArray(doc) ? doc : {};
    } catch {
      return {};
    }
  }

  function markRead(tid, upto) {
    if (!tid || !upto || !storageKey) return;
    const read = loadRead();
    if ((read[tid] || 0) >= upto) return;
    read[tid] = upto;
    try { localStorage.setItem(storageKey, JSON.stringify(read)); } catch { /* private mode */ }
  }

  /* ---- talking to the daemon ---- */

  async function request(path, body) {
    const opts = body === undefined ? {} : {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    };
    const resp = await api(path, opts);
    const doc = await resp.json().catch(() => ({}));
    if (!resp.ok) throw Error(doc.error || `HTTP ${resp.status}`);
    return doc;
  }

  async function load() {
    const mine = ++seq;
    const want = name;
    try {
      const doc = await request(`/api/threads?session=${encodeURIComponent(want)}`);
      if (mine !== seq || want !== name) return;
      error = "";
      list = doc;
      current = pick(list.threads, current);
      await loadThread();
    } catch (e) {
      if (mine !== seq || want !== name) return;
      if (e && e.message && !/fetch/i.test(e.message)) error = e.message;
    }
    if (mine === seq && want === name) render();
  }

  async function loadThread() {
    const tid = current;
    if (!tid) { convo = null; return; }
    const have = convo && convo.id === tid ? convo : null;
    const after = have && have.messages.length ? have.messages[have.messages.length - 1].seq : 0;
    const row = (list.threads || []).find((t) => t.id === tid);
    if (have && row && row.last_seq <= after) { have.thread = row; return; }
    const doc = await request(`/api/threads/${encodeURIComponent(tid)}?after=${after}`);
    if (tid !== current) return;
    convo = {id: tid, thread: doc.thread, messages: (have ? have.messages : []).concat(doc.messages || [])};
  }

  async function act(fn) {
    try {
      notice = "";
      await fn();
    } catch (e) {
      notice = (e && e.message) || String(e);
    }
    await load();
    if (onChange) onChange();
  }

  /* One post per target at a time: a double click (or Enter pressed twice)
     before the first answer would otherwise open two threads or send the
     message twice (claunch-7xp0f). The button shows it as disabled. */
  async function once(key, fn) {
    if (inflight.has(key)) return;
    inflight.add(key);
    render();
    try {
      await fn();
    } finally {
      inflight.delete(key);
      render();
    }
  }

  function send(tid, text) {
    return once(tid, () => act(async () => {
      const res = await request(`/api/threads/${encodeURIComponent(tid)}/messages`, {text});
      drafts.delete(tid);
      if (res.delivered === false) notice = `${name} 세션에 전달하지 못했습니다(종료됨). 메시지는 쓰레드에 남았습니다.`;
    }));
  }

  function create(title, text) {
    return once("", () => act(async () => {
      const body = {to: name, title};
      if (text) body.text = text;
      const res = await request("/api/threads", body);
      drafts.delete("");
      composing = false;
      current = res.thread.id;
      if (res.delivered === false) notice = `${name} 세션에 전달하지 못했습니다(종료됨). 메시지는 쓰레드에 남았습니다.`;
    }));
  }

  function setStatus(tid, verb) {
    return act(() => request(`/api/threads/${encodeURIComponent(tid)}/${verb}`, {}));
  }

  /* ---- drawing ---- */

  function typingIn(n) {
    const a = document.activeElement;
    return !!a && !!n && n.contains(a) && (a.tagName === "TEXTAREA" || a.tagName === "INPUT");
  }

  function textBox(cls, key, placeholder, onSend) {
    const box = node("textarea", cls);
    box.rows = 3;
    box.placeholder = placeholder;
    box.value = drafts.get(key) || "";
    box.addEventListener("input", () => drafts.set(key, box.value));
    if (onSend) box.addEventListener("keydown", (e) => { if (sendKey(e)) { e.preventDefault(); onSend(); } });
    return box;
  }

  function renderList(side, threads, read) {
    const fresh = unseen(threads, read);
    side.replaceChildren();
    const add = node("button", "term-btn th-new", composing ? "취소" : "+ 새 쓰레드");
    add.type = "button";
    add.addEventListener("click", () => { composing = !composing; render(); });
    side.append(add);
    if (composing) {
      const form = node("div", "th-newform");
      const title = node("input", "th-title-input");
      title.placeholder = "주제(한 줄)";
      title.value = drafts.get("\u0000title") || "";
      title.addEventListener("input", () => drafts.set("\u0000title", title.value));
      const first = textBox("th-text", "", "첫 메시지(선택) — 세션에 전달됩니다");
      const go = node("button", "term-btn", "시작");
      go.type = "button";
      go.disabled = inflight.has("");
      const submit = () => {
        if (inflight.has("")) return;
        if (!title.value.trim()) { title.focus(); return; }
        drafts.delete("\u0000title");
        go.disabled = true;
        create(title.value.trim(), first.value.trim());
      };
      go.addEventListener("click", submit);
      title.addEventListener("keydown", (e) => { if (sendKey(e)) { e.preventDefault(); first.focus(); } });
      form.append(title, first, go);
      side.append(form);
      if (!title.value) setTimeout(() => title.focus(), 0);
    }
    if (!threads.length) {
      side.append(node("p", "wf-note", "쓰레드가 없습니다. 주제별로 쓰레드를 열면 세션과 따로 대화할 수 있습니다."));
      return;
    }
    const ul = node("ul", "th-list");
    for (const t of threads) {
      const li = node("li", "th-item");
      li.classList.toggle("on", t.id === current);
      li.classList.toggle("closed", t.status === "closed");
      li.classList.toggle("unseen", fresh.has(t.id));
      li.dataset.id = t.id;
      const top = node("div", "th-item-top");
      top.append(node("span", "th-item-title", t.title));
      if (t.status === "closed") top.append(node("span", "th-chip", "닫힘"));
      if (fresh.has(t.id)) top.append(node("span", "th-dot", "●"));
      li.append(top);
      if (t.last) {
        const who = t.last.author === "agent" ? `${name}: ` : "나: ";
        li.append(node("div", "th-item-last", who + excerpt(t.last.body)));
      }
      li.title = `${t.id} · ${t.opened_by === "agent" ? "세션이 연 쓰레드" : "내가 연 쓰레드"}`;
      li.addEventListener("click", () => {
        if (current === t.id) return;
        current = t.id;
        convo = null;
        load();
      });
      ul.append(li);
    }
    side.append(ul);
  }

  function renderConvo(main) {
    if (!convo) {
      main.replaceChildren(node("p", "wf-note", "왼쪽에서 쓰레드를 고르거나 새 쓰레드를 여세요."));
      return;
    }
    const t = convo.thread;
    const head = node("div", "th-convo-head");
    head.append(node("h3", "th-convo-title", t.title), node("span", "th-meta", t.id));
    const toggle = node("button", "term-btn", t.status === "closed" ? "다시 열기" : "닫기");
    toggle.type = "button";
    toggle.title = t.status === "closed"
      ? "reopen this thread"
      : "close this thread once its topic is settled — a new message reopens it";
    toggle.addEventListener("click", () => setStatus(t.id, t.status === "closed" ? "reopen" : "close"));
    head.append(toggle);

    const msgs = node("div", "th-messages");
    for (const m of convo.messages) {
      const row = node("div", `th-msg th-${m.author}`);
      const meta = node("div", "th-msg-meta");
      meta.append(node("span", "th-msg-who", m.author === "agent" ? name : "나"));
      const at = new Date(m.at);
      meta.append(node("span", "th-meta", isNaN(at) ? m.at : at.toLocaleString()));
      const body = node("div", "th-msg-body");
      if (m.author === "agent" && typeof globalThis.mdInto === "function") globalThis.mdInto(body, m.body);
      else body.textContent = m.body;
      row.append(meta, body);
      msgs.append(row);
    }
    if (!convo.messages.length) msgs.append(node("p", "wf-note", "아직 메시지가 없습니다."));

    const compose = node("div", "th-compose");
    const box = textBox("th-text", t.id, `${name}에게 이 쓰레드로 보내기 — Enter 전송, Shift+Enter 줄바꿈`,
                        () => { const v = box.value.trim(); if (v) send(t.id, v); });
    const go = node("button", "term-btn", "보내기");
    go.type = "button";
    go.disabled = inflight.has(t.id);
    go.addEventListener("click", () => { const v = box.value.trim(); if (v) send(t.id, v); });
    compose.append(box, go);

    main.replaceChildren(head, msgs, compose);
    msgs.scrollTop = msgs.scrollHeight;
    const last = convo.messages[convo.messages.length - 1];
    if (last) markRead(t.id, last.seq);
  }

  function render() {
    if (!host) return;
    let root = host.querySelector(".th-pane");
    if (!root) {
      root = node("div", "th-pane");
      root.append(node("div", "th-head"), node("div", "th-body"));
      root.lastChild.append(node("div", "th-side"), node("div", "th-main"));
      host.replaceChildren(root);
    }
    const head = root.querySelector(".th-head");
    const side = root.querySelector(".th-side");
    const main = root.querySelector(".th-main");
    const threads = (list && list.threads) || [];
    head.replaceChildren(
      node("h2", null, `${name} — 쓰레드`),
      node("span", "th-meta", list ? `열림 ${list.open} · 전체 ${threads.length}` : "불러오는 중…"),
    );
    if (error) head.append(node("p", "wf-warning", error));
    if (notice) head.append(node("p", "wf-warning", notice));
    // Redraw only what changed, so a poll neither scrolls the reader back
    // to the bottom nor rebuilds what they are typing in.
    const read = loadRead();
    const sideSig = JSON.stringify([threads, current, composing, [...unseen(threads, read)], inflight.has("")]);
    if (!typingIn(side) && (sideSig !== drawn.side || !side.firstChild)) {
      drawn.side = sideSig;
      renderList(side, threads, read);
    }
    const t = convo && convo.thread;
    const mainSig = JSON.stringify(convo ? [convo.id, t.title, t.status, convo.messages.length, inflight.has(convo.id)] : null);
    if (mainSig === drawn.main && main.firstChild) return;
    drawn.main = mainSig;
    const keep = main.querySelector(".th-compose textarea");
    const focused = keep && document.activeElement === keep;
    renderConvo(main);
    if (focused) main.querySelector(".th-compose textarea")?.focus();
  }

  function mount(target, session, opts = {}) {
    if (host === target && name === session && target.querySelector(".th-pane")) return;
    unmount();
    host = target;
    name = session;
    api = opts.api || globalThis.api;
    onChange = opts.onChange || null;
    storageKey = opts.storageKey || "claunch_threadread";
    host.replaceChildren(node("p", "wf-note", "불러오는 중…"));
    load();
    timer = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      load();
    }, POLL_MS);
  }

  function unmount() {
    if (timer) clearInterval(timer);
    timer = null;
    seq++;
    if (host) host.replaceChildren();
    host = null;
    name = "";
    list = null;
    convo = null;
    current = "";
    composing = false;
    inflight.clear();
    notice = error = "";
    drawn.side = drawn.main = "";
  }

  return {mount, unmount, refresh: () => host && load(), excerpt, unseen, sendKey, pick};
})();
