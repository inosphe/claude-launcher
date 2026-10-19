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
   dot. Both sides' messages go through the page's markdown renderer
   (mdInto, app.js), which builds nodes and never parses HTML; without it a
   message is shown as plain text (claunch-5usj6).

   Sending (claunch-5usj6): Enter empties the composer at once and shows the
   message in the conversation as a pending bubble ("전송 중…") until the
   daemon answers; a failed post marks the bubble "전송 실패" and puts the text
   back in the composer. One post per thread is out at a time (claunch-7xp0f).

   QA links (claunch-78dpm, daemon/talk_links.py): each thread carries
   `links` to the QA questions it touches. The conversation head shows them
   as chips that call opts.onQuestion(id) (talk-pane.js moves to the card);
   "QA로 정리 요청" posts a message with ask_qa, asking the session to file
   the thread's open decision as a question linked back here. compose()
   opens the new-thread form prefilled for a question (QA card's "쓰레드로
   논의", closed questions too); the title starts with the question id and
   the thread is opened with `qa` set. A title typed by hand that names one
   of the session's question ids is linked by the daemon the same way
   (claunch-236ei).

   Renaming (claunch-6st9p): the topic is edited in place -- a click on the
   conversation's title (or its ✎, or a double click on the thread in the
   list) turns the title into a box; Enter or 저장 posts it to
   /api/threads/<id>/title, Esc or 취소 puts the title back. A title that
   names one of the session's question ids is linked as at opening. What is
   typed survives a poll's redraw (it is kept in `drafts`).

   Images (claunch-shl2r): a picture pasted (Ctrl+V), dropped or picked
   (🖼 button) in a composer waits under it as a thumbnail until the message
   is sent; then each one is uploaded (POST /api/threads/images, the
   opening-image staging) and the post names the returned ids in `images`.
   A message may be images alone. Messages show their images from
   GET /api/threads/<id>/images/<name>, so the conversation keeps them.

   Space (claunch-1n081): "◂ 목록" in the head folds the thread list away
   while a conversation is open, so the bubbles take the pane's whole
   width; the fold is kept in localStorage (FOLD_KEY) for every session.

   Width (claunch-46lbf): the bar between the two halves (.th-split) is
   #rail-split's mirror inside the pane. Dragging it sets the list column's
   width (--th-side-w on .th-pane), applied live while the hand moves and
   written down once at release (SIDE_W_KEY, every session); a double click
   forgets it. With nothing stored the stylesheet's width holds (220px, 170px
   beside the QA column). The bar is hidden while the pane has one half
   (.th-solo, .th-folded) and on a phone. */
globalThis.ThreadsPane = (() => {
  "use strict";
  const POLL_MS = 3000;
  const REMEMBER = 64;       // sessions whose last-shown thread is kept
  let host = null, name = "", api = null, onChange = null, onQuestion = null, storageKey = "";
  let timer = null, seq = 0, list = null, error = "";
  let current = "";          // the selected thread id
  let convo = null;          // {id, thread, messages}
  let composing = false;     // the new-thread form is open
  let composeQa = null;      // {id, title}: the question the new thread is about
  let renaming = "";         // the thread whose title is being edited
  let notice = "";
  let wanted = null;         // {session, tid}: the thread to show at the next mount there
  let focusCompose = false;  // a thread was just opened: its composer takes the focus
  const drafts = new Map();  // thread id ("" = new thread) -> text
  const inflight = new Set(); // thread ids ("" = new thread) with a post still out
  const pending = new Map();  // thread id -> {text, images, failed?: error}: the message being sent
  const composers = new WeakMap(); // composer textarea -> its draft key
  const attached = new Map(); // thread id ("" = new thread) -> [{file, src}]: images waiting to be sent
  const drawn = {side: "", main: ""};   // what each half last drew
  const FOLD_KEY = "claunch_threadfold";  // localStorage: the list column is folded away
  const SIDE_W_KEY = "claunch_threadsidew"; // localStorage: the list column's width in px
  const SIDE_W_MIN = 140;    // narrower and every title is ellipsis
  const MAIN_W_MIN = 240;    // the conversation keeps at least this much

  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  /* ---- pure helpers (tests/web/threadspane_check.js) ---- */

  /* The media types the daemon keeps (daemon/opening_images.TYPES), by
     extension for a picked file whose browser left `type` empty. */
  const IMAGE_KINDS = {png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp"};

  function imageKind(file) {
    const kind = String((file && file.type) || "").toLowerCase();
    if (kind) return Object.values(IMAGE_KINDS).includes(kind) ? kind : "";
    const m = /\.([a-z0-9]+)$/i.exec(String((file && file.name) || ""));
    return (m && IMAGE_KINDS[m[1].toLowerCase()]) || "";
  }

  /* The image files of a paste or a drop. `items` is where a screenshot
     arrives; `files` (a drop, a file manager copy) is read only when `items`
     had none, because the two can list the same file twice. */
  function imageFiles(data) {
    if (!data) return [];
    const out = [];
    for (const it of Array.from(data.items || [])) {
      if (it.kind !== "file" || !imageKind({type: it.type})) continue;
      const f = it.getAsFile && it.getAsFile();
      if (f) out.push(f);
    }
    if (out.length) return out;
    return Array.from(data.files || []).filter((f) => imageKind(f));
  }

  /* Where the daemon serves image `name` of thread `tid`, through the
     page's base (app.js url()) when it is loaded. */
  function imageSrc(tid, name) {
    const path = `api/threads/${encodeURIComponent(tid)}/images/${encodeURIComponent(name)}`;
    return typeof globalThis.url === "function" ? globalThis.url(path) : path;
  }

  /* What the list says of a message: its first line, or that it is images. */
  function lastLine(m) {
    const text = excerpt(m && m.body);
    const n = ((m && m.images) || []).length;
    if (!n) return text;
    return text ? `${text} 🖼${n}` : `🖼 이미지 ${n}장`;
  }

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

  /* Ctrl+J is a new line too, as in the session's own composer (app.js
     isCtrlJ, claunch-e7z8z): "j" or "J", keyCode 74 for a layout that labels
     the key otherwise, "\n" from a terminal-style keymap. */
  function newlineKey(e) {
    if (!e.ctrlKey || e.altKey || e.metaKey || e.isComposing || e.keyCode === 229) return false;
    return e.key === "j" || e.key === "J" || e.key === "\n" || e.keyCode === 74;
  }

  /* Put a line break at the caret of composer `box` (draft `key`), the
     caret after it. A box that reports no selection is appended to. */
  function breakLine(box, key) {
    const start = Number.isInteger(box.selectionStart) ? box.selectionStart : box.value.length;
    const end = Number.isInteger(box.selectionEnd) ? box.selectionEnd : start;
    box.value = box.value.slice(0, start) + "\n" + box.value.slice(end);
    if (typeof box.setSelectionRange === "function") box.setSelectionRange(start + 1, start + 1);
    drafts.set(key, box.value);
    fit(box);
  }

  /* Ctrl+J, taken before the browser gets to it. Chrome and Firefox open
     the downloads panel on Ctrl+J, and a listener on the box alone does not
     stop that in Firefox (see onWindowCtrlJ in app.js), so the key is
     claimed in the capture phase at the window -- only while one of this
     pane's composers holds the focus; anywhere else it stays the
     browser's. */
  function onWindowCtrlJ(e) {
    if (!newlineKey(e) || !host) return;
    const box = typeof document !== "undefined" ? document.activeElement : null;
    if (!box || !composers.has(box) || !host.contains(box)) return;
    e.preventDefault();
    e.stopPropagation();
    breakLine(box, composers.get(box));
  }
  if (typeof globalThis.addEventListener === "function") globalThis.addEventListener("keydown", onWindowCtrlJ, true);

  /* The thread to show: the remembered one if it still exists, else the
     first open one, else the first. */
  function pick(threads, want) {
    const ts = threads || [];
    if (want && ts.some((t) => t.id === want)) return want;
    const open = ts.find((t) => t.status === "open");
    return (open || ts[0] || {}).id || "";
  }

  /* A message's time for its bubble: the clock for today, else the date
     and the clock. */
  function stamp(at, now = new Date()) {
    const d = new Date(at);
    if (isNaN(d)) return String(at || "");
    const hm = d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
    return d.toDateString() === now.toDateString() ? hm : `${d.toLocaleDateString()} ${hm}`;
  }

  /* What a link chip says: the relation, the question id and its title. */
  function linkLabel(link) {
    const rel = link.rel === "subject" ? "다루는 질문" : "여기서 만든 질문";
    const title = link.status === "deleted" ? "(삭제됨)" : excerpt(link.title, 40);
    return `${rel} · ${link.id} ${title}`;
  }

  /* The default text of a "QA로 정리 요청" post when the composer is empty. */
  const ASK_QA_TEXT = "이 쓰레드에서 내가 결정해야 할 것을 QA 질문으로 올려 주세요.";

  /* ---- read positions ---- */

  function loadRead() {
    try {
      const doc = JSON.parse(localStorage.getItem(storageKey) || "{}");
      return doc && typeof doc === "object" && !Array.isArray(doc) ? doc : {};
    } catch {
      return {};
    }
  }

  /* The thread last shown for each session, so the next open of the pane
     can read it beside the list (load). */
  function remembered(session) {
    try {
      const doc = JSON.parse(localStorage.getItem(`${storageKey}:open`) || "{}");
      return doc && typeof doc[session] === "string" ? doc[session] : "";
    } catch {
      return "";
    }
  }

  function remember(tid) {
    if (!tid || !storageKey || remembered(name) === tid) return;
    try {
      const doc = JSON.parse(localStorage.getItem(`${storageKey}:open`) || "{}");
      const out = doc && typeof doc === "object" && !Array.isArray(doc) ? doc : {};
      delete out[name];   // re-added last, so the oldest session goes first
      out[name] = tid;
      for (const key of Object.keys(out).slice(0, -REMEMBER)) delete out[key];
      localStorage.setItem(`${storageKey}:open`, JSON.stringify(out));
    } catch { /* private mode */ }
  }

  function markRead(tid, upto) {
    if (!tid || !upto || !storageKey) return;
    const read = loadRead();
    if ((read[tid] || 0) >= upto) return;
    read[tid] = upto;
    try { localStorage.setItem(storageKey, JSON.stringify(read)); } catch { /* private mode */ }
  }

  /* ---- the list column folded away (claunch-1n081) ---- */

  /* Folding hides the thread list so the conversation takes the pane's
     whole width; the choice is this browser's, for every session. */
  function loadFold() {
    try { return localStorage.getItem(FOLD_KEY) === "1"; } catch { return false; }
  }

  function saveFold(on) {
    try { localStorage.setItem(FOLD_KEY, on ? "1" : "0"); } catch { /* private mode */ }
  }

  /* ---- the list column's width (claunch-46lbf) ---- */

  /* The list column's width, held between SIDE_W_MIN and what leaves the
     conversation MAIN_W_MIN of a `room`px body (no ceiling while the body's
     width is unknown). NaN in, NaN out: nothing stored means the stylesheet's. */
  function clampSideW(px, room = 0) {
    if (!Number.isFinite(px) || px <= 0) return NaN;
    const max = room > 0 ? Math.max(SIDE_W_MIN, room - MAIN_W_MIN) : Infinity;
    return Math.min(max, Math.max(SIDE_W_MIN, Math.round(px)));
  }

  function loadSideW() {
    try { return clampSideW(Number(localStorage.getItem(SIDE_W_KEY))); } catch { return NaN; }
  }

  function saveSideW(px) {
    try {
      if (Number.isFinite(px)) localStorage.setItem(SIDE_W_KEY, String(px));
      else localStorage.removeItem(SIDE_W_KEY);
    } catch { /* private mode */ }
  }

  function applySideW(root, px) {
    if (Number.isFinite(px)) root.style.setProperty("--th-side-w", `${px}px`);
    else root.style.removeProperty("--th-side-w");
  }

  /* The bar's drag: the list's left edge stays put, the bar follows the hand. */
  function wireSplit(root, bar) {
    let dragW = null;   // non-null only mid-drag
    bar.addEventListener("pointerdown", (e) => {
      e.preventDefault();   // a drag must not start selecting list text
      bar.setPointerCapture?.(e.pointerId);
      bar.classList.add("dragging");
      dragW = root.querySelector(".th-side").getBoundingClientRect().width;
    });
    bar.addEventListener("pointermove", (e) => {
      if (dragW === null) return;
      const side = root.querySelector(".th-side").getBoundingClientRect();
      const room = root.querySelector(".th-body").getBoundingClientRect().width;
      dragW = clampSideW(e.clientX - side.left, room);
      applySideW(root, dragW);
    });
    const end = () => {
      if (dragW === null) return;
      bar.classList.remove("dragging");
      saveSideW(dragW);
      dragW = null;
    };
    bar.addEventListener("pointerup", end);
    bar.addEventListener("pointercancel", end);
    bar.addEventListener("dblclick", () => { saveSideW(NaN); applySideW(root, NaN); });
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

  /* Upload one image to the staging area; its id names it in the post. */
  async function stage(file) {
    const resp = await api("/api/threads/images", {
      method: "POST", headers: {"Content-Type": imageKind(file)}, body: file,
    });
    const doc = await resp.json().catch(() => ({}));
    if (!resp.ok || !doc.id) throw Error(doc.error || `이미지 업로드 실패 (HTTP ${resp.status})`);
    return doc.id;
  }

  /* The ids of the images waiting for `key`, uploaded in order. */
  async function stageAll(key) {
    const ids = [];
    for (const a of attached.get(key) || []) ids.push(await stage(a.file));
    return ids;
  }

  function dropAttached(key) {
    for (const a of attached.get(key) || []) {
      try { URL.revokeObjectURL(a.src); } catch { /* no object URLs here */ }
    }
    attached.delete(key);
  }

  /* Add image files to what `key`'s composer will send; files that are not
     images are left out and counted in the notice. */
  function attach(key, files) {
    const list = Array.from(files || []);
    const pics = list.filter((f) => imageKind(f));
    if (list.length > pics.length) notice = `이미지가 아닌 파일 ${list.length - pics.length}개는 첨부하지 않았습니다.`;
    if (!pics.length) { render(); return; }
    const have = attached.get(key) || [];
    for (const file of pics) {
      let src = "";
      try { src = URL.createObjectURL(file); } catch { /* no object URLs here */ }
      have.push({file, src});
    }
    attached.set(key, have);
    drawStrips();
    render();
  }

  async function load() {
    const mine = ++seq;
    const want = name;
    try {
      // A thread with nothing on screen yet is read beside the list rather
      // than after it: the two go out in one batch, so opening the pane
      // costs one round trip to the daemon instead of two (claunch-8utpv).
      // A poll of a thread already shown still waits for the list, which
      // says whether there is anything new to read.
      const early = current && !(convo && convo.id === current) ? readThread(current, 0) : null;
      const doc = await request(`/api/threads?session=${encodeURIComponent(want)}`);
      if (mine !== seq || want !== name) return;
      error = "";
      list = doc;
      current = pick(list.threads, current);
      remember(current);
      await loadThread(early);
    } catch (e) {
      if (mine !== seq || want !== name) return;
      if (e && e.message && !/fetch/i.test(e.message)) error = e.message;
    }
    if (mine === seq && want === name) render();
  }

  function readThread(tid, after) {
    const promise = request(`/api/threads/${encodeURIComponent(tid)}?after=${after}`);
    promise.catch(() => {});   // an early read the list made moot is not an error
    return {tid, after, promise};
  }

  async function loadThread(early = null) {
    const tid = current;
    if (!tid) { convo = null; return; }
    const have = convo && convo.id === tid ? convo : null;
    const after = have && have.messages.length ? have.messages[have.messages.length - 1].seq : 0;
    const row = (list.threads || []).find((t) => t.id === tid);
    if (have && row && row.last_seq <= after) { have.thread = row; return; }
    const read = early && early.tid === tid && early.after === after ? early : readThread(tid, after);
    const doc = await read.promise;
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

  /* What the post's answer says about the message reaching the terminal.
     delivered=true only means the session accepted it; `hold` names what
     is keeping it out of the terminal right now (claunch-a41i5), and
     without saying so a held message looks like a lost one. */
  function deliveryNotice(res) {
    if (res.delivered === false) return `${name} 세션에 전달하지 못했습니다(종료됨). 메시지는 쓰레드에 남았습니다.`;
    if (res.hold === "draft") return `${name} 터미널 입력창에 보내지 않은 입력이 있어 전달 대기 중입니다. 그 입력을 보내거나(Enter) 지우면(Ctrl-C) 이어서 전달됩니다.`;
    if (res.hold === "typing") return `${name} 터미널에 키보드 입력이 이어지고 있어 전달 대기 중입니다. 입력이 멈추면 전달됩니다.`;
    if (res.hold === "asleep") return `${name} 세션을 깨우는 중입니다. 깨어나면 전달됩니다.`;
    return "";
  }

  function send(tid, text, askQa = false) {
    if (inflight.has(tid)) return undefined;
    // The composer empties now, before the answer: what was sent shows as a
    // pending bubble, so nothing is left in the box to send a second time.
    // Attached images stay attached until the post is in: a failure keeps
    // them for the next try.
    drafts.delete(tid);
    const pics = (attached.get(tid) || []).map((a) => a.src);
    pending.set(tid, {text, images: pics});
    return once(tid, () => act(async () => {
      const body = {text};
      if (askQa) body.ask_qa = true;
      let res;
      try {
        const ids = await stageAll(tid);
        if (ids.length) body.images = ids;
        res = await request(`/api/threads/${encodeURIComponent(tid)}/messages`, body);
      } catch (e) {
        pending.set(tid, {text, images: pics, failed: (e && e.message) || String(e)});
        if (!(drafts.get(tid) || "").trim()) drafts.set(tid, text);
        throw e;
      }
      pending.delete(tid);
      dropAttached(tid);
      notice = deliveryNotice(res);
    }));
  }

  function create(title, text) {
    return once("", () => act(async () => {
      const body = {to: name, title};
      if (text) body.text = text;
      if (composeQa) body.qa = composeQa.id;
      const ids = await stageAll("");
      if (ids.length) body.images = ids;
      const res = await request("/api/threads", body);
      dropAttached("");
      drafts.delete("");
      drafts.delete("\u0000title");
      composing = false;
      composeQa = null;
      current = res.thread.id;
      focusCompose = true;
      notice = deliveryNotice(res);
    }));
  }

  function setStatus(tid, verb) {
    return act(() => request(`/api/threads/${encodeURIComponent(tid)}/${verb}`, {}));
  }

  const RENAME = "\u0000rename";   // drafts key of the title being edited

  function startRename(tid) {
    if (renaming !== tid) drafts.delete(RENAME);
    renaming = tid;
    render();
  }

  function stopRename() {
    renaming = "";
    drafts.delete(RENAME);
    render();
  }

  /* A blank or unchanged title closes the box without a post; a failed post
     keeps the box and what was typed, and the head says why. */
  function rename(tid, title, was) {
    if (!title || title === was) { stopRename(); return undefined; }
    return once(RENAME, () => act(async () => {
      await request(`/api/threads/${encodeURIComponent(tid)}/title`, {title});
      if (renaming === tid) { renaming = ""; drafts.delete(RENAME); }
    }));
  }

  /* ---- drawing ---- */

  function typingIn(n) {
    const a = document.activeElement;
    return !!a && !!n && n.contains(a) && (a.tagName === "TEXTAREA" || a.tagName === "INPUT");
  }

  /* The box grows with what is typed, up to a cap, then scrolls. */
  function fit(box) {
    if (!box.style) return;
    box.style.height = "auto";
    box.style.height = `${Math.min(box.scrollHeight || 0, 220)}px`;
  }

  function textBox(cls, key, placeholder, onSend, rows = 3) {
    const box = node("textarea", cls);
    box.rows = rows;
    box.placeholder = placeholder;
    box.value = drafts.get(key) || "";
    box.addEventListener("input", () => { drafts.set(key, box.value); fit(box); });
    composers.set(box, key);
    box.addEventListener("keydown", (e) => { if (newlineKey(e)) { e.preventDefault(); breakLine(box, key); } });
    if (onSend) box.addEventListener("keydown", (e) => { if (sendKey(e)) { e.preventDefault(); onSend(); } });
    // A paste carrying images: the images are attached, the text of a mixed
    // paste is left to the browser.
    box.addEventListener("paste", (e) => {
      const files = imageFiles(e.clipboardData);
      if (!files.length) return;
      e.preventDefault();
      attach(key, files);
    });
    return box;
  }

  /* The thumbnails waiting under composer `key`, each with a remove button. */
  function fillStrip(strip) {
    const key = strip.dataset.key;
    const list = attached.get(key) || [];
    strip.replaceChildren();
    strip.hidden = !list.length;
    list.forEach((a, i) => {
      const item = node("span", "th-attach-item");
      const pic = node("img");
      pic.src = a.src;
      pic.alt = a.file.name || "첨부 이미지";
      const drop = node("button", "th-attach-drop", "×");
      drop.type = "button";
      drop.title = "첨부 취소";
      drop.addEventListener("click", () => {
        const rest = (attached.get(key) || []).filter((_, j) => j !== i);
        try { URL.revokeObjectURL(a.src); } catch { /* no object URLs here */ }
        if (rest.length) attached.set(key, rest); else attached.delete(key);
        drawStrips();
        render();
      });
      item.append(pic, drop);
      strip.append(item);
    });
  }

  function strip(key) {
    const box = node("div", "th-attach");
    box.dataset.key = key;
    fillStrip(box);
    return box;
  }

  /* Redraw every strip in place: a paste into a focused composer must show
     at once, and the list half is not redrawn while someone types in it. */
  function drawStrips() {
    if (!host || !host.querySelectorAll) return;
    for (const box of host.querySelectorAll(".th-attach")) fillStrip(box);
  }

  /* The 🖼 button and its hidden file input: several images at once. */
  function pickButton(key) {
    const input = node("input");
    input.type = "file";
    input.accept = Object.values(IMAGE_KINDS).join(",");
    input.multiple = true;
    input.hidden = true;
    input.addEventListener("change", () => { attach(key, input.files); input.value = ""; });
    const pick = node("button", "term-btn th-pick", "🖼");
    pick.type = "button";
    pick.title = "이미지 첨부 — 입력창에 붙여넣거나(Ctrl+V) 끌어다 놓아도 됩니다";
    pick.addEventListener("click", () => input.click());
    return [pick, input];
  }

  /* Images dropped anywhere on `area` go to composer `key`. */
  function dropTarget(area, key) {
    area.addEventListener("dragover", (e) => {
      if (!Array.from((e.dataTransfer || {}).types || []).includes("Files")) return;
      e.preventDefault();
      area.classList.add("dragover");
    });
    area.addEventListener("dragleave", () => area.classList.remove("dragover"));
    area.addEventListener("drop", (e) => {
      area.classList.remove("dragover");
      const files = imageFiles(e.dataTransfer);
      if (!files.length) return;
      e.preventDefault();
      attach(key, files);
    });
  }

  /* A message body: markdown for both authors when the renderer is on the
     page, plain text otherwise. */
  function bodyOf(text) {
    const body = node("div", "th-msg-body");
    if (typeof globalThis.mdInto === "function") {
      body.classList.add("md");
      globalThis.mdInto(body, text);
    } else {
      body.classList.add("plain");
      body.textContent = text;
    }
    return body;
  }

  /* A message's images as thumbnails, each a link to the full picture. */
  function picturesOf(srcs) {
    const box = node("div", "th-images");
    for (const src of srcs) {
      const link = node("a", "th-image");
      link.href = src;
      link.target = "_blank";
      link.rel = "noopener";
      const pic = node("img");
      pic.src = src;
      pic.alt = "첨부 이미지";
      pic.loading = "lazy";
      link.append(pic);
      box.append(link);
    }
    return box;
  }

  function bubble(author, text, when, state, images = []) {
    const row = node("div", `th-msg th-${author}`);
    if (state) row.classList.add(`th-${state}`);
    const meta = node("div", "th-msg-meta");
    meta.append(node("span", "th-msg-who", author === "agent" ? name : "나"));
    if (when) meta.append(node("span", "th-msg-at", when));
    if (state === "sending") meta.append(node("span", "th-msg-state", "전송 중…"));
    if (state === "failed") meta.append(node("span", "th-msg-state", "전송 실패 — 입력창에 되돌렸습니다"));
    row.append(meta);
    if (text || !images.length) row.append(bodyOf(text));
    if (images.length) row.append(picturesOf(images));
    return row;
  }

  function renderList(side, threads, read) {
    const fresh = unseen(threads, read);
    side.replaceChildren();
    const add = node("button", "term-btn th-new", composing ? "취소" : "+ 새 쓰레드");
    add.type = "button";
    add.addEventListener("click", () => { composing = !composing; composeQa = null; render(); });
    side.append(add);
    if (composing) {
      const form = node("div", "th-newform");
      const title = node("input", "th-title-input");
      title.placeholder = "주제(선택) — 비우면 Thread #N, qa-xxxxxxxx를 넣으면 그 QA 질문과 연결";
      title.value = drafts.get("\u0000title") || "";
      title.addEventListener("input", () => drafts.set("\u0000title", title.value));
      const first = textBox("th-text", "", "첫 메시지(선택) — 세션에 전달됩니다. 이미지는 붙여넣기·끌어다 놓기");
      let about = null;
      if (composeQa) {
        about = node("div", "th-about");
        about.append(node("span", "th-link-chip", `다루는 질문 · ${composeQa.id} ${excerpt(composeQa.title, 40)}`));
        const drop = node("button", "term-btn th-about-drop", "연결 해제");
        drop.type = "button";
        drop.addEventListener("click", () => { composeQa = null; render(); });
        about.append(drop);
      }
      const go = node("button", "term-btn th-send", inflight.has("") ? "여는 중…" : "시작");
      go.type = "button";
      go.disabled = inflight.has("");
      const submit = () => {
        if (inflight.has("")) return;
        go.disabled = true;
        create(title.value.trim(), first.value.trim());
      };
      go.addEventListener("click", submit);
      title.addEventListener("keydown", (e) => { if (sendKey(e)) { e.preventDefault(); first.focus(); } });
      dropTarget(form, "");
      form.append(title, ...(about ? [about] : []), first, strip(""), ...pickButton(""), go);
      side.append(form);
      if (!title.value) setTimeout(() => title.focus(), 0);
    }
    if (!threads.length) {
      side.append(node("p", "wf-note", "쓰레드가 없습니다. 주제별로 쓰레드를 열면 세션과 따로 대화할 수 있습니다. 주제와 첫 메시지는 비워 두어도 됩니다."));
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
      if ((t.links || []).length) {
        const q = node("span", "th-chip th-chip-qa", `QA ${t.links.length}`);
        q.title = t.links.map(linkLabel).join("\n");
        top.append(q);
      }
      if (fresh.has(t.id)) top.append(node("span", "th-dot", "●"));
      li.append(top);
      if (t.last) {
        const who = t.last.author === "agent" ? `${name}: ` : "나: ";
        li.append(node("div", "th-item-last", who + lastLine(t.last)));
      }
      li.title = `${t.id} · ${t.opened_by === "agent" ? "세션이 연 쓰레드" : "내가 연 쓰레드"}`;
      li.addEventListener("click", () => {
        if (current === t.id) return;
        if (renaming) { renaming = ""; drafts.delete(RENAME); }
        current = t.id;
        convo = null;
        load();
      });
      li.addEventListener("dblclick", () => {
        if (current !== t.id) { current = t.id; convo = null; load(); }
        startRename(t.id);
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
    if (renaming === t.id) {
      const box = node("input", "th-rename-input");
      box.value = drafts.has(RENAME) ? drafts.get(RENAME) : t.title;
      box.placeholder = "쓰레드 주제";
      box.title = "Enter 저장 · Esc 취소 · qa-xxxxxxxx를 넣으면 그 QA 질문과 연결";
      box.addEventListener("input", () => drafts.set(RENAME, box.value));
      const save = () => rename(t.id, box.value.trim(), t.title);
      box.addEventListener("keydown", (e) => {
        if (sendKey(e)) { e.preventDefault(); save(); }
        else if (e.key === "Escape") { e.preventDefault(); stopRename(); }
      });
      const ok = node("button", "term-btn th-rename-save", inflight.has(RENAME) ? "저장 중…" : "저장");
      ok.type = "button";
      ok.disabled = inflight.has(RENAME);
      ok.addEventListener("click", save);
      const no = node("button", "term-btn th-rename-cancel", "취소");
      no.type = "button";
      no.addEventListener("click", stopRename);
      head.append(box, ok, no);
    } else {
      const title = node("h3", "th-convo-title", t.title);
      title.title = "눌러서 주제 수정";
      title.addEventListener("click", () => startRename(t.id));
      const edit = node("button", "th-rename-btn", "✎");
      edit.type = "button";
      edit.title = "주제 수정";
      edit.setAttribute("aria-label", "주제 수정");
      edit.addEventListener("click", () => startRename(t.id));
      head.append(title, edit, node("span", "th-meta", t.id));
    }
    const toggle = node("button", "term-btn", t.status === "closed" ? "다시 열기" : "닫기");
    toggle.type = "button";
    toggle.title = t.status === "closed"
      ? "reopen this thread"
      : "close this thread once its topic is settled — a new message reopens it";
    toggle.addEventListener("click", () => setStatus(t.id, t.status === "closed" ? "reopen" : "close"));
    const askQa = node("button", "term-btn th-ask-qa", "QA로 정리 요청");
    askQa.type = "button";
    askQa.title = "세션에게 이 쓰레드에서 결정할 것을 QA 질문으로 올리라고 요청합니다. " +
      "입력창에 쓴 내용이 있으면 그 내용을 함께 보냅니다.";
    askQa.disabled = inflight.has(t.id);
    askQa.addEventListener("click", () => {
      const box = main.querySelector(".th-compose textarea");
      const v = ((box && box.value) || "").trim();
      send(t.id, v || ASK_QA_TEXT, true);
    });
    head.append(toggle, askQa);

    let links = null;
    if ((t.links || []).length) {
      links = node("div", "th-links");
      for (const link of t.links) {
        const chip = node("button", `th-link-chip qa-st-${link.status}`, linkLabel(link));
        chip.type = "button";
        chip.disabled = link.status === "deleted" || !onQuestion;
        chip.title = link.status === "deleted" ? "삭제된 질문" : "QA 카드로 이동";
        chip.addEventListener("click", () => onQuestion && onQuestion(link.id));
        links.append(chip);
      }
    }

    const msgs = node("div", "th-messages");
    for (const m of convo.messages) {
      msgs.append(bubble(m.author, m.body, stamp(m.at), "", (m.images || []).map((i) => imageSrc(t.id, i.name))));
    }
    const out = pending.get(t.id);
    if (out) msgs.append(bubble("user", out.text, "", out.failed ? "failed" : "sending", out.images || []));
    if (!convo.messages.length && !out) {
      msgs.append(node("p", "th-empty", "아직 메시지가 없습니다. 아래 입력창에 첫 메시지를 쓰세요."));
    }

    const sending = inflight.has(t.id);
    const compose = node("div", "th-compose");
    const submit = () => {
      const v = box.value.trim();
      if (v || (attached.get(t.id) || []).length) send(t.id, v);
    };
    const box = textBox("th-text", t.id, `${name}에게 보내기`, submit, 1);
    const foot = node("div", "th-compose-foot");
    foot.append(...pickButton(t.id), node("span", "th-hint", "Enter 전송 · Shift+Enter/Ctrl+J 줄바꿈 · markdown 지원 · 이미지 붙여넣기"));
    const go = node("button", "term-btn th-send", sending ? "전송 중…" : "보내기");
    go.type = "button";
    go.disabled = sending;
    go.addEventListener("click", submit);
    foot.append(go);
    dropTarget(compose, t.id);
    compose.append(strip(t.id), box, foot);

    main.replaceChildren(head, ...(links ? [links] : []), msgs, compose);
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
      const bar = node("div", "th-split");
      bar.title = "끌어서 목록 폭을 바꿉니다 · 두 번 누르면 기본 폭";
      root.lastChild.append(node("div", "th-side"), bar, node("div", "th-main"));
      wireSplit(root, bar);
      host.replaceChildren(root);
    }
    applySideW(root, loadSideW());
    // With no thread on screen there is no conversation to hold the right
    // half for: the list (and the new-thread form) takes the whole width.
    root.classList.toggle("th-solo", !convo);
    const head = root.querySelector(".th-head");
    const side = root.querySelector(".th-side");
    const main = root.querySelector(".th-main");
    const threads = (list && list.threads) || [];
    // The list folds only while a conversation is on screen: without one
    // the list is the only thing the pane has to show.
    const folded = loadFold();
    root.classList.toggle("th-folded", folded && !!convo);
    const fold = node("button", "term-btn th-fold", folded ? "▸ 목록" : "◂ 목록");
    fold.type = "button";
    fold.title = folded ? "쓰레드 목록을 다시 펼칩니다" : "쓰레드 목록을 접어 대화에 폭 전체를 씁니다";
    fold.setAttribute("aria-pressed", folded ? "true" : "false");
    fold.disabled = !convo;
    fold.addEventListener("click", () => { saveFold(!loadFold()); render(); });
    head.replaceChildren(
      fold,
      node("h2", null, `${name} — 쓰레드`),
      node("span", "th-meta", list ? `열림 ${list.open} · 전체 ${threads.length}` : "불러오는 중…"),
    );
    if (error) head.append(node("p", "wf-warning", error));
    if (notice) head.append(node("p", "wf-warning", notice));
    // Redraw only what changed, so a poll neither scrolls the reader back
    // to the bottom nor rebuilds what they are typing in.
    const read = loadRead();
    const sideSig = JSON.stringify([threads, current, composing, composeQa, [...unseen(threads, read)], inflight.has(""),
                                    (attached.get("") || []).length]);
    if (!typingIn(side) && (sideSig !== drawn.side || !side.firstChild)) {
      drawn.side = sideSig;
      renderList(side, threads, read);
    }
    const t = convo && convo.thread;
    const editing = !!convo && renaming === convo.id;
    const mainSig = JSON.stringify(convo ? [convo.id, t.title, t.status, t.links || [], convo.messages.length,
                                            inflight.has(convo.id), pending.get(convo.id) || null,
                                            (attached.get(convo.id) || []).length,
                                            editing, inflight.has(RENAME)] : null);
    if (mainSig === drawn.main && main.firstChild) return;
    const oldTitleBox = main.querySelector(".th-rename-input");
    const titleFocused = !!oldTitleBox && document.activeElement === oldTitleBox;
    drawn.main = mainSig;
    const keep = main.querySelector(".th-compose textarea");
    const focused = keep && document.activeElement === keep;
    renderConvo(main);
    const box = main.querySelector(".th-compose textarea");
    if (box) fit(box);
    // The title box takes the focus when it opens and keeps it through a
    // redraw; the composer keeps it the same way when the focus went there.
    const titleBox = main.querySelector(".th-rename-input");
    if (titleBox && !oldTitleBox) {
      titleBox.focus();
      titleBox.select?.();
    } else if (titleBox && titleFocused) titleBox.focus();
    else if (focused || (focusCompose && convo)) box?.focus();
    if (convo) focusCompose = false;
  }

  function mount(target, session, opts = {}) {
    if (host === target && name === session && target.querySelector(".th-pane")) return;
    unmount();
    host = target;
    name = session;
    api = opts.api || globalThis.api;
    onChange = opts.onChange || null;
    onQuestion = opts.onQuestion || null;
    storageKey = opts.storageKey || "claunch_threadread";
    current = remembered(session);
    if (wanted && wanted.session === session) { current = wanted.tid; wanted = null; }
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
    composeQa = null;
    focusCompose = false;
    inflight.clear();
    pending.clear();
    for (const key of [...attached.keys()]) dropAttached(key);
    notice = error = "";
    drawn.side = drawn.main = "";
  }

  /* Show thread `tid` (a link from a QA card). */
  function select(tid) {
    if (!host || !tid) return;
    composing = false;
    if (current !== tid) { current = tid; convo = null; }
    load();
  }

  /* Show thread `tid` of `session` (a notice card about its reply): now
     if the pane is on that session, else as soon as it is mounted there. */
  function focus(session, tid) {
    if (!session || !tid) return;
    if (host && name === session) { wanted = null; select(tid); return; }
    wanted = {session, tid};
  }

  /* Open the new-thread form about question `q` ({id, title}), its title
     prefilled; a title already typed for this question is kept. */
  function compose(q) {
    if (!host || !q) return;
    if (!composeQa || composeQa.id !== q.id) drafts.set("\u0000title", `${q.id}: ${q.title}`);
    composing = true;
    composeQa = {id: q.id, title: q.title};
    drawn.side = "";
    render();
  }

  return {mount, unmount, refresh: () => host && load(), select, focus, compose, excerpt, unseen, sendKey, newlineKey, pick, linkLabel, stamp,
          imageKind, imageFiles, imageSrc, lastLine, clampSideW, FOLD_KEY, SIDE_W_KEY};
})();
