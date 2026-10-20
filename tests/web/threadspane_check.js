/* Session threads pane (threads-pane.js) and its header button (app.js),
   claunch-doj98.

   What has to hold:
     helpers   -- excerpt takes the first non-empty line and shortens it;
                  unseen marks threads whose last message is the agent's and
                  newer than this browser's read position; Enter sends,
                  Shift+Enter and IME composition do not; Ctrl+J is a new
                  line (claunch-e7z8z); pick keeps the
                  remembered thread, else the first open one.
     ctrl+j    -- Ctrl+J in a composer puts a newline at the caret and
                  posts nothing; the window capture listener claims the key
                  only while a pane composer holds the focus (claunch-e7z8z).
     pane      -- mounting lists the session's threads, opens the first open
                  one, renders both authors' text through mdInto (HTML stays
                  text), and a send posts the text to that thread; Enter
                  empties the composer at once and shows a pending bubble
                  until the answer, and a failed send marks the bubble and
                  puts the text back (claunch-5usj6); a new thread posts {to, title, text}; close
                  posts to /close; reading a thread moves its read position;
                  unmount stops the poll; ◂ 목록 folds the list away while a
                  conversation is open and the fold is remembered
                  (claunch-1n081); dragging .th-split sets the list's width
                  (held between its floor and what leaves the conversation
                  room), the width is kept at release and comes back at the
                  next mount, a double click forgets it, and the bar is hidden
                  for a one-half pane and on a phone (claunch-46lbf);
                  a click on the title (or ✎, or a double click in the
                  list) opens a box: Enter posts {title} to /title, Esc
                  and a blank or unchanged title post nothing, a failed
                  rename keeps the box and its text, and a poll keeps
                  what is typed (claunch-6st9p).
     rail      -- 🗪 threads lights while the session has an open thread, and
                  carries has-news when the session wrote past what this
                  browser saw; a session never seen here starts caught up.
     wired     -- the button and script in index.html (before app.js), the
                  split pane opens/closes ThreadsPane, the poll and boot read
                  the rail, and threads-pane.js never assigns HTML. */
const assert = require("assert/strict");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const root = path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static");
const src = fs.readFileSync(path.join(root, "threads-pane.js"), "utf8");
const app = fs.readFileSync(path.join(root, "app.js"), "utf8");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const css = fs.readFileSync(path.join(root, "style.css"), "utf8");

/* ---- a small DOM: enough for the pane's own calls ---- */
function makeDoc() {
  const doc = {activeElement: null};
  function el(tag) {
    const classes = new Set();
    const n = {
      tagName: tag.toUpperCase(), children: [], parent: null, handlers: {}, dataset: {},
      textContent: "", value: "", title: "", placeholder: "", rows: 0, type: "", scrollTop: 0, scrollHeight: 0,
      get className() { return [...classes].join(" "); },
      set className(v) { classes.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c)); },
      classList: {
        add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c),
        toggle: (c, on) => { const want = on === undefined ? !classes.has(c) : !!on; want ? classes.add(c) : classes.delete(c); },
      },
      get firstChild() { return n.children[0] || null; },
      get lastChild() { return n.children[n.children.length - 1] || null; },
      append(...kids) { for (const k of kids) { k.parent = n; n.children.push(k); } },
      replaceChildren(...kids) { n.children = []; n.append(...kids); },
      attrs: {}, rect: {left: 0, width: 0},
      style: {props: {}, setProperty(k, v) { n.style.props[k] = v; }, removeProperty(k) { delete n.style.props[k]; }},
      getBoundingClientRect() { return n.rect; },
      setAttribute(k, v) { n.attrs[k] = String(v); },
      addEventListener(k, fn) { (n.handlers[k] ||= []).push(fn); },
      fire(k, e = {}) { (n.handlers[k] || []).forEach((fn) => fn({preventDefault() {}, ...e})); },
      focus() { doc.activeElement = n; },
      contains(o) { for (let x = o; x; x = x.parent) if (x === n) return true; return false; },
      all() { return n.children.flatMap((c) => [c, ...c.all()]); },
      querySelector(sel) { return n.querySelectorAll(sel)[0] || null; },
      querySelectorAll(sel) {
        const parts = sel.trim().split(/\s+/);
        const match = (x, p) => p.startsWith(".") ? x.classList.contains(p.slice(1)) : x.tagName === p.toUpperCase();
        let found = n.all().filter((x) => match(x, parts[parts.length - 1]));
        if (parts.length === 2) found = found.filter((x) => { for (let y = x.parent; y; y = y.parent) if (match(y, parts[0])) return true; return false; });
        return found;
      },
      text() { return n.textContent + n.children.map((c) => c.text()).join(""); },
    };
    Object.defineProperty(n, "innerHTML", {set() { throw new Error("threads-pane.js assigned innerHTML"); }});
    return n;
  }
  doc.createElement = el;
  doc.hidden = false;
  return doc;
}

const settle = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };

(async () => {
  const document = makeDoc();
  const store = new Map();
  const localStorage = {getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)),
                        removeItem: (k) => store.delete(k)};
  const timers = [];
  const rendered = [];   // [class of the bubble, text] for each mdInto call
  const mdInto = (n, text) => { rendered.push(text); n.textContent = text; return n; };
  const winKeys = [];    // [type, listener, capture] mounted on the window
  const ctx = vm.createContext({
    addEventListener: (type, fn, capture) => winKeys.push([type, fn, capture]),
    document, localStorage, console, JSON, Date, Map, Set, Error, Promise, mdInto,
    setInterval: (fn, ms) => { timers.push(fn); return timers.length; },
    clearInterval: (id) => { timers[id - 1] = null; },
    setTimeout: () => 0,
  });
  vm.runInContext(src, ctx);
  const P = vm.runInContext("ThreadsPane", ctx);

  /* helpers */
  assert.equal(P.excerpt("\n  \nfirst line\nsecond"), "first line");
  assert.equal(P.excerpt("x".repeat(100), 10), "xxxxxxxxx…");
  const ts = [
    {id: "th-a", status: "closed", last_seq: 9, last: {author: "agent"}},
    {id: "th-b", status: "open", last_seq: 5, last: {author: "agent"}},
    {id: "th-c", status: "open", last_seq: 7, last: {author: "user"}},
  ];
  assert.deepEqual([...P.unseen(ts, {"th-a": 9, "th-b": 4})], ["th-b"]);
  assert.equal(P.sendKey({key: "Enter"}), true);
  assert.equal(P.sendKey({key: "Enter", shiftKey: true}), false);
  assert.equal(P.sendKey({key: "Enter", isComposing: true}), false);
  assert.equal(P.sendKey({key: "Enter", keyCode: 229}), false);
  assert.equal(P.newlineKey({key: "j", ctrlKey: true}), true);
  assert.equal(P.newlineKey({key: "J", ctrlKey: true, shiftKey: true}), true);
  assert.equal(P.newlineKey({key: "\n", ctrlKey: true}), true);
  assert.equal(P.newlineKey({key: "ㅓ", ctrlKey: true, keyCode: 74}), true, "the physical J key on another layout");
  assert.equal(P.newlineKey({key: "j"}), false);
  assert.equal(P.newlineKey({key: "j", ctrlKey: true, altKey: true}), false);
  assert.equal(P.newlineKey({key: "j", ctrlKey: true, metaKey: true}), false);
  assert.equal(P.newlineKey({key: "j", ctrlKey: true, isComposing: true}), false);
  assert.equal(P.sendKey({key: "j", ctrlKey: true}), false, "Ctrl+J never sends");
  assert.equal(P.pick(ts, "th-c"), "th-c");
  assert.equal(P.pick(ts, "th-gone"), "th-b");
  assert.equal(P.pick([], ""), "");

  /* the pane against a fake daemon */
  const threads = [
    {id: "th-1", title: "Log format", status: "open", opened_by: "user", last_seq: 2,
     last: {author: "agent", body: "because tools"}},
    {id: "th-2", title: "Old topic", status: "closed", opened_by: "agent", last_seq: 1, last: null},
  ];
  const messages = {"th-1": [
    {seq: 1, author: "user", body: "<b>why json?</b>", at: "2026-10-13T00:00:00+00:00"},
    {seq: 2, author: "agent", body: "because tools", at: "2026-10-13T00:00:01+00:00"},
  ]};
  const calls = [];
  let failCreate = false;
  let postHold = null;
  let failPost = false;
  let failRename = false;
  const api = async (p, opts = {}) => {
    calls.push([opts.method || "GET", p, opts.body ? JSON.parse(opts.body) : undefined]);
    if (failCreate && p === "/api/threads") return {ok: false, status: 500, json: async () => ({error: "boom"})};
    if (failPost && p === "/api/threads/th-1/messages") return {ok: false, status: 500, json: async () => ({error: "lost"})};
    if (failRename && p.endsWith("/title")) return {ok: false, status: 400, json: async () => ({error: "title too long"})};
    let doc = {};
    if (p.startsWith("/api/threads?session=")) doc = {session: "s1", threads, open: 1, agent_seq: 2};
    else if (/^\/api\/threads\/th-1\?after=/.test(p)) {
      const after = Number(p.split("after=")[1]);
      doc = {thread: threads[0], messages: messages["th-1"].filter((m) => m.seq > after)};
    } else if (/^\/api\/threads\/th-2\?after=/.test(p)) {
      doc = {thread: threads[1], messages: []};
    } else if (p === "/api/threads/th-1/messages") doc = {message: {seq: 3}, delivered: true, hold: postHold};
    else if (p === "/api/threads") doc = {thread: {id: "th-9"}, delivered: false};
    else if (p.endsWith("/close")) doc = {thread: threads[0]};
    else if (/^\/api\/threads\/th-\d\/title$/.test(p)) {
      const t = threads.find((x) => p === `/api/threads/${x.id}/title`);
      t.title = JSON.parse(opts.body).title;
      doc = {thread: t};
    }
    return {ok: true, status: 200, json: async () => doc};
  };
  let changed = 0;
  const host = document.createElement("div");
  P.mount(host, "s1", {api, storageKey: "k", onChange: () => changed++});
  await settle();
  assert.equal(calls[0][1], "/api/threads?session=s1");
  const items = host.querySelectorAll(".th-item");
  assert.deepEqual(items.map((i) => i.dataset.id), ["th-1", "th-2"]);
  assert.ok(items[0].classList.contains("on"), "the first open thread is shown");
  assert.ok(items[1].classList.contains("closed"));
  const msgs = host.querySelectorAll(".th-msg");
  assert.equal(msgs.length, 2);
  assert.equal(msgs[0].querySelector(".th-msg-body").textContent, "<b>why json?</b>", "user text stays text");
  assert.deepEqual(rendered, ["<b>why json?</b>", "because tools"], "both authors go through the markdown renderer");
  assert.ok(msgs.every((m) => m.querySelector(".th-msg-body").classList.contains("md")));
  assert.deepEqual(JSON.parse(store.get("k")), {"th-1": 2}, "showing a thread reads it");

  // ctrl+j: a newline at the caret, nothing posted (claunch-e7z8z)
  const box = host.querySelector(".th-compose .th-text");
  const postsBefore = calls.filter((c) => c[0] === "POST").length;
  box.value = "ab";
  box.selectionStart = box.selectionEnd = 1;
  let stopped = 0, prevented = 0;
  const chord = (extra = {}) => ({key: "j", ctrlKey: true, preventDefault() { prevented++; }, stopPropagation() { stopped++; }, ...extra});
  box.fire("keydown", chord());
  assert.equal(box.value, "a\nb", "Ctrl+J breaks the line at the caret");
  assert.equal(prevented, 1, "the browser's Ctrl+J is cancelled");
  await settle();
  assert.equal(calls.filter((c) => c[0] === "POST").length, postsBefore, "Ctrl+J posts nothing");
  const capture = winKeys.filter(([type, , cap]) => type === "keydown" && cap === true);
  assert.equal(capture.length, 1, "one capture-phase keydown listener on the window");
  const fresh = host.querySelector(".th-compose .th-text");
  fresh.value = "x";
  document.activeElement = null;
  prevented = stopped = 0;
  capture[0][1](chord());
  assert.equal(fresh.value, "x", "a Ctrl+J outside the pane's composers stays the browser's");
  assert.equal(prevented, 0);
  fresh.focus();
  capture[0][1](chord());
  assert.equal(fresh.value, "x\n", "the window listener claims Ctrl+J in a focused composer");
  assert.equal(prevented, 1);
  assert.equal(stopped, 1, "the key goes no further, so the box's own listener does not add a second newline");
  capture[0][1](chord({ctrlKey: false}));
  assert.equal(fresh.value, "x\n", "a bare J is typed by the browser");
  fresh.value = "";
  document.activeElement = null;

  // send: Enter in the composer posts to the thread
  const box1 = host.querySelector(".th-compose .th-text");
  box1.value = "and yaml?";
  box1.fire("keydown", {key: "Enter"});
  await settle();
  assert.deepEqual(calls.find((c) => c[0] === "POST"), ["POST", "/api/threads/th-1/messages", {text: "and yaml?"}]);
  assert.ok(changed >= 1, "a send refreshes the header light");

  // a second Enter before the first answer is not a second message (claunch-7xp0f)
  const posts = () => calls.filter((c) => c[0] === "POST" && c[1] === "/api/threads/th-1/messages").length;
  const sent = posts();
  const box2 = host.querySelector(".th-compose .th-text");
  box2.value = "twice?";
  box2.fire("keydown", {key: "Enter"});
  assert.ok(host.querySelector(".th-compose .th-send").disabled, "보내기 is disabled while the post is out");
  assert.equal(host.querySelector(".th-compose .th-text").value, "", "Enter empties the composer before the answer");
  const out = host.querySelectorAll(".th-sending");
  assert.equal(out.length, 1, "the message being sent shows as a pending bubble");
  assert.equal(out[0].querySelector(".th-msg-body").textContent, "twice?");
  host.querySelector(".th-compose .th-text").fire("keydown", {key: "Enter"});
  host.querySelector(".th-compose .th-send").fire("click");
  await settle();
  assert.equal(posts(), sent + 1, "one message for a double send");
  assert.equal(host.querySelectorAll(".th-sending").length, 0, "the pending bubble goes once answered");
  assert.ok(!host.querySelector(".th-compose .th-send").disabled, "보내기 comes back after the answer");
  assert.equal(host.querySelectorAll(".wf-warning").length, 0, "a message that went straight in says nothing");

  // a message the session accepted but is holding says why (claunch-a41i5)
  for (const [hold, words] of [["draft", "보내지 않은 입력"], ["typing", "키보드 입력"], ["asleep", "깨우는 중"]]) {
    postHold = hold;
    host.querySelector(".th-compose .th-text").value = `held ${hold}?`;
    host.querySelector(".th-compose .th-text").fire("keydown", {key: "Enter"});
    await settle();
    const warn = host.querySelectorAll(".wf-warning");
    assert.equal(warn.length, 1, `a ${hold} hold is shown`);
    assert.ok(warn[0].textContent.includes(words) && warn[0].textContent.includes("s1"), warn[0].textContent);
  }
  postHold = null;

  // a failed send keeps the message: the bubble says so and the text is back in the composer
  host.querySelector(".th-compose .th-text").value = "lost one";
  failPost = true;
  host.querySelector(".th-compose .th-text").fire("keydown", {key: "Enter"});
  await settle();
  failPost = false;
  const failed = host.querySelectorAll(".th-failed");
  assert.equal(failed.length, 1, "a failed send is marked");
  assert.match(failed[0].text(), /전송 실패/);
  assert.equal(host.querySelector(".th-compose .th-text").value, "lost one", "the failed text is back in the composer");
  assert.match(host.querySelector(".th-head").text(), /lost/);
  host.querySelector(".th-compose .th-send").fire("click");
  await settle();
  assert.equal(host.querySelectorAll(".th-failed").length, 0, "a retry clears the failure");

  // fold: the list goes, the conversation keeps the pane; the fold is remembered
  const pane = host.querySelector(".th-pane");
  const foldBtn = () => host.querySelector(".th-head .th-fold");
  assert.ok(!pane.classList.contains("th-folded"), "the list starts unfolded");
  assert.equal(foldBtn().attrs["aria-pressed"], "false");
  assert.ok(!foldBtn().disabled, "a shown conversation can fold the list");
  foldBtn().fire("click");
  assert.ok(pane.classList.contains("th-folded"), "◂ 목록 folds the list");
  assert.equal(store.get(P.FOLD_KEY), "1", "the fold is kept for this browser");
  assert.equal(foldBtn().attrs["aria-pressed"], "true");
  assert.ok(host.querySelector(".th-compose"), "the conversation stays on screen");
  foldBtn().fire("click");
  assert.ok(!pane.classList.contains("th-folded"), "▸ 목록 brings the list back");
  assert.equal(store.get(P.FOLD_KEY), "0");
  assert.ok(/\.th-pane\.th-folded \.th-side \{ display: none; \}/.test(css), "a folded pane hides the list column");
  assert.ok(!/\.th-msg \{[^}]*760px/.test(css), "a bubble has no fixed width cap");

  // width (claunch-46lbf): the bar between the halves moves the list's edge
  assert.equal(P.clampSideW(NaN), NaN, "nothing stored: the stylesheet's width");
  assert.equal(P.clampSideW(50), 140, "the list keeps a floor");
  assert.equal(P.clampSideW(900, 800), 560, "the conversation keeps 240px of the body");
  assert.equal(P.clampSideW(300.4, 800), 300);
  const bar = host.querySelector(".th-body .th-split");
  assert.ok(bar, "a split bar sits between the halves");
  const bodyKids = host.querySelector(".th-body").children.map((c) => c.className);
  assert.deepEqual(bodyKids, ["th-side", "th-split", "th-main"], "list | bar | conversation");
  assert.equal(pane.style.props["--th-side-w"], undefined, "no width until someone moves it");
  host.querySelector(".th-side").rect = {left: 100, width: 220};
  host.querySelector(".th-body").rect = {left: 100, width: 800};
  bar.fire("pointerdown", {pointerId: 1, clientX: 320});
  assert.ok(bar.classList.contains("dragging"));
  bar.fire("pointermove", {clientX: 400});
  assert.equal(pane.style.props["--th-side-w"], "300px", "the width follows the hand");
  assert.equal(store.get(P.SIDE_W_KEY), undefined, "nothing is written mid-drag");
  bar.fire("pointermove", {clientX: 2000});
  assert.equal(pane.style.props["--th-side-w"], "560px", "the conversation keeps its floor");
  bar.fire("pointermove", {clientX: 450});
  bar.fire("pointerup", {});
  assert.ok(!bar.classList.contains("dragging"));
  assert.equal(store.get(P.SIDE_W_KEY), "350", "the width is kept at release");
  bar.fire("pointermove", {clientX: 600});
  assert.equal(pane.style.props["--th-side-w"], "350px", "a move without a press does nothing");
  P.refresh();
  await settle();
  assert.equal(host.querySelector(".th-pane").style.props["--th-side-w"], "350px", "a redraw keeps the width");
  bar.fire("dblclick", {});
  assert.equal(store.get(P.SIDE_W_KEY), undefined, "a double click forgets the width");
  assert.equal(pane.style.props["--th-side-w"], undefined, "...and the stylesheet's width is back");
  store.set(P.SIDE_W_KEY, "280");
  assert.ok(/\.th-side \{ flex: 0 0 var\(--th-side-w, 220px\)/.test(css), "the list reads the width, 220px by default");
  assert.ok(/\.talk-body\[data-mode="both"\] \.th-side \{ flex-basis: var\(--th-side-w, 170px\); \}/.test(css),
            "beside the QA column the default stays 170px");
  assert.ok(/\.th-pane\.th-solo \.th-split, \.th-pane\.th-folded \.th-split \{ display: none; \}/.test(css),
            "a one-half pane has no bar");
  const phone = css.slice(css.indexOf(".th-pane.th-solo .th-split"));
  assert.ok(/@media \(max-width: 820px\) \{\s*\.th-split \{ display: none; \}/.test(phone),
            "the phone hides the bar, after the rule that shows it");
  assert.ok(/\.th-split \{[^}]*width: 8px;[^}]*cursor: col-resize/.test(css), "#rail-split's shape");

  // rename (claunch-6st9p): the title turns into a box, Enter posts it
  const renames = () => calls.filter((c) => c[0] === "POST" && c[1].endsWith("/title"));
  host.querySelector(".th-convo-title").fire("click");
  let rbox = host.querySelector(".th-convo-head .th-rename-input");
  assert.ok(rbox, "a click on the title opens the box");
  assert.equal(rbox.value, "Log format", "the box starts with the title");
  assert.equal(document.activeElement, rbox, "the box takes the focus");
  rbox.fire("keydown", {key: "Escape"});
  assert.ok(!host.querySelector(".th-rename-input"), "Esc closes the box");
  assert.equal(host.querySelector(".th-convo-title").textContent, "Log format");
  host.querySelector(".th-rename-btn").fire("click");
  rbox = host.querySelector(".th-rename-input");
  assert.ok(rbox, "✎ opens the box");
  rbox.fire("keydown", {key: "Enter"});
  await settle();
  assert.equal(renames().length, 0, "an unchanged title posts nothing");
  assert.ok(!host.querySelector(".th-rename-input"));
  host.querySelector(".th-rename-btn").fire("click");
  rbox = host.querySelector(".th-rename-input");
  rbox.value = "   ";
  rbox.fire("input");
  rbox.fire("keydown", {key: "Enter"});
  await settle();
  assert.equal(renames().length, 0, "a blank title posts nothing");
  // what is typed survives a poll's redraw
  host.querySelector(".th-rename-btn").fire("click");
  rbox = host.querySelector(".th-rename-input");
  rbox.value = "Log format v2";
  rbox.fire("input");
  messages["th-1"].push({seq: 3, author: "agent", body: "more", at: "2026-10-13T00:00:02+00:00"});
  threads[0].last_seq = 3;
  timers.filter(Boolean).forEach((fn) => fn());
  await settle();
  rbox = host.querySelector(".th-rename-input");
  assert.equal(rbox.value, "Log format v2", "a poll keeps what is typed");
  assert.equal(document.activeElement, rbox, "and the focus");
  // a poll does not pull the focus from the composer into the title box
  const composer = host.querySelector(".th-compose .th-text");
  composer.focus();
  messages["th-1"].push({seq: 4, author: "agent", body: "even more", at: "2026-10-13T00:00:03+00:00"});
  threads[0].last_seq = 4;
  timers.filter(Boolean).forEach((fn) => fn());
  await settle();
  assert.ok(host.querySelector(".th-rename-input"), "the box stays open");
  assert.ok(document.activeElement.classList.contains("th-text"), "the composer keeps the focus through a poll");
  rbox = host.querySelector(".th-rename-input");
  rbox.focus();
  // a failed rename keeps the box and says why
  failRename = true;
  rbox.fire("keydown", {key: "Enter"});
  await settle();
  failRename = false;
  assert.equal(host.querySelector(".th-rename-input").value, "Log format v2", "a failed rename keeps the text");
  assert.match(host.querySelector(".th-head").text(), /title too long/);
  host.querySelector(".th-rename-save").fire("click");
  await settle();
  assert.deepEqual(renames().pop(), ["POST", "/api/threads/th-1/title", {title: "Log format v2"}]);
  assert.ok(!host.querySelector(".th-rename-input"), "a saved title closes the box");
  assert.equal(host.querySelector(".th-convo-title").textContent, "Log format v2", "the head shows the new title");
  assert.equal(host.querySelector(".th-item .th-item-title").textContent, "Log format v2", "and so does the list");
  // moving to another thread drops an open box and its draft
  host.querySelector(".th-rename-btn").fire("click");
  rbox = host.querySelector(".th-rename-input");
  rbox.value = "abandoned";
  rbox.fire("input");
  host.querySelectorAll(".th-item")[1].fire("click");
  await settle();
  assert.ok(!host.querySelector(".th-rename-input"), "another thread opens without a box");
  host.querySelectorAll(".th-item")[0].fire("click");
  await settle();
  assert.ok(!host.querySelector(".th-rename-input"), "coming back does not reopen it");
  host.querySelector(".th-rename-btn").fire("click");
  assert.equal(host.querySelector(".th-rename-input").value, "Log format v2", "the abandoned draft is gone");
  host.querySelector(".th-rename-cancel").fire("click");
  // a double click in the list opens that thread with its title in a box
  host.querySelectorAll(".th-item")[1].fire("dblclick");
  await settle();
  rbox = host.querySelector(".th-rename-input");
  assert.ok(rbox, "a double click on a thread opens its box");
  assert.equal(rbox.value, "Old topic");
  rbox.value = "New topic";
  rbox.fire("input");
  rbox.fire("keydown", {key: "Enter"});
  await settle();
  assert.deepEqual(renames().pop(), ["POST", "/api/threads/th-2/title", {title: "New topic"}]);
  assert.match(css, /\.th-rename-input \{/, "the box is styled");
  host.querySelectorAll(".th-item")[0].fire("click");
  await settle();

  // close
  const toggle = host.querySelector(".th-convo-head .term-btn");
  assert.equal(toggle.textContent, "닫기");
  toggle.fire("click");
  await settle();
  assert.ok(calls.some((c) => c[0] === "POST" && c[1] === "/api/threads/th-1/close"));

  // new thread: + opens the form, 시작 posts {to, title, text}; a failed delivery is said
  host.querySelector(".th-new").fire("click");
  await settle();
  // a failed create keeps what was typed and frees 시작 for a retry
  failCreate = true;
  const typed = host.querySelector(".th-newform .th-title-input");
  typed.value = "Deploy";
  typed.fire("input");
  host.querySelector(".th-newform .th-send").fire("click");
  await settle();
  assert.equal(host.querySelector(".th-newform .th-title-input").value, "Deploy", "a failed create keeps the title");
  assert.ok(!host.querySelector(".th-newform .th-send").disabled, "시작 comes back after a failure");
  assert.match(host.querySelector(".th-head").text(), /boom/);
  failCreate = false;
  calls.length = 0;
  const form = host.querySelector(".th-newform");
  form.querySelector(".th-title-input").value = "Deploy";
  form.querySelector(".th-text").value = "when?";
  const startBtn = form.querySelector(".th-send");
  startBtn.fire("click");
  startBtn.fire("click");   // a double click before the first answer (claunch-7xp0f)
  assert.ok(startBtn.disabled, "시작 is disabled while the post is out");
  await settle();
  assert.equal(calls.filter((c) => c[1] === "/api/threads").length, 1, "one thread for a double click");
  assert.deepEqual(calls.filter((c) => c[1] === "/api/threads").pop(), ["POST", "/api/threads", {to: "s1", title: "Deploy", text: "when?"}]);
  assert.match(host.querySelector(".th-head").text(), /전달하지 못했습니다/);

  assert.ok(!host.querySelector(".th-pane").classList.contains("th-solo"), "a shown thread keeps the two halves");

  // unmount stops the poll and empties the host
  P.unmount();
  assert.equal(host.children.length, 0);
  assert.ok(timers.every((t) => t === null), "no poll outlives the pane");

  // reopening the pane reads the thread it last showed beside the list, in
  // the same turn -- one batch, one round trip (claunch-8utpv)
  assert.deepEqual(JSON.parse(store.get("k:open")), {s1: "th-1"}, "the shown thread is remembered per session");
  calls.length = 0;
  P.mount(host, "s1", {api, storageKey: "k"});
  assert.deepEqual(calls.map((c) => c[1]).sort(), ["/api/threads/th-1?after=0", "/api/threads?session=s1"],
    "list and conversation go out before either answers");
  await settle();
  assert.equal(calls.filter((c) => c[1].startsWith("/api/threads/th-1?")).length, 1, "the early read is used, not repeated");
  assert.equal(host.querySelectorAll(".th-msg").length >= 2, true);
  P.unmount();
  // a session never shown here still starts with the list alone
  calls.length = 0;
  P.mount(host, "s2", {api, storageKey: "k"});
  assert.deepEqual(calls.map((c) => c[1]), ["/api/threads?session=s2"]);
  await settle();
  P.unmount();
  // a reply's notice card asks for its thread before the pane is mounted
  // there (claunch-gfv7l): a mount on another session leaves the wish alone,
  // the mount on that session opens the thread instead of the remembered one
  P.focus("s1", "th-2");
  calls.length = 0;
  P.mount(host, "s2", {api, storageKey: "k"});
  assert.ok(!calls.some((c) => c[1].startsWith("/api/threads/th-2?")), "another session ignores the wish");
  await settle();
  P.unmount();
  calls.length = 0;
  P.mount(host, "s1", {api, storageKey: "k"});
  assert.ok(calls.some((c) => c[1].startsWith("/api/threads/th-2?")), "the wished thread is read, not th-1");
  await settle();
  P.unmount();
  calls.length = 0;
  P.mount(host, "s1", {api, storageKey: "k"});
  assert.ok(P.focus.length === 2, "focus(session, tid)");
  await settle();
  assert.equal(host.querySelector(".th-pane").style.props["--th-side-w"], "280px", "a kept width comes back at mount");
  P.unmount();

  /* claunch-bvbmi: with no thread the list takes the whole width, and a
     thread opens with neither a title nor a first message */
  const none = [];
  const calls2 = [];
  const api2 = async (p, opts = {}) => {
    calls2.push([opts.method || "GET", p, opts.body ? JSON.parse(opts.body) : undefined]);
    let doc = {};
    if (p.startsWith("/api/threads?session=")) doc = {session: "s2", threads: none, open: none.length, agent_seq: 0};
    else if (p === "/api/threads") {
      none.push({id: "th-7", title: "Thread #1", status: "open", opened_by: "user", last_seq: 0, last: null});
      doc = {thread: none[0], message: null};
    } else if (/^\/api\/threads\/th-7\?after=/.test(p)) doc = {thread: none[0], messages: []};
    return {ok: true, status: 200, json: async () => doc};
  };
  const host2 = document.createElement("div");
  P.mount(host2, "s2", {api: api2, storageKey: "k2"});
  await settle();
  assert.ok(host2.querySelector(".th-pane").classList.contains("th-solo"), "no thread: the list fills the pane");
  host2.querySelector(".th-new").fire("click");
  await settle();
  host2.querySelector(".th-newform .th-send").fire("click");
  await settle();
  assert.deepEqual(calls2.filter((c) => c[1] === "/api/threads").pop(), ["POST", "/api/threads", {to: "s2", title: ""}],
                   "시작 with nothing typed opens an untitled, empty thread");
  assert.ok(!host2.querySelector(".th-pane").classList.contains("th-solo"), "the new thread takes the right half");
  assert.equal(host2.querySelector(".th-convo-title").textContent, "Thread #1");
  assert.equal(document.activeElement, host2.querySelector(".th-compose .th-text"), "its composer has the focus");
  P.unmount();

  /* images on a message (claunch-shl2r) */
  assert.equal(P.imageKind({type: "image/png"}), "image/png");
  assert.equal(P.imageKind({type: "image/svg+xml"}), "", "a type the daemon does not keep is not an image here");
  assert.equal(P.imageKind({type: "", name: "shot.JPG"}), "image/jpeg");
  assert.equal(P.imageKind({type: "", name: "notes.txt"}), "");
  const png = {type: "image/png", name: "a.png"};
  const txt = {type: "text/plain", name: "a.txt"};
  assert.deepEqual([...P.imageFiles({items: [{kind: "string", type: "text/plain"}, {kind: "file", type: "image/png", getAsFile: () => png}],
                                 files: [png]})], [png], "items first, never the same file twice");
  assert.deepEqual([...P.imageFiles({items: [], files: [txt, png]})], [png], "a drop's files, images only");
  assert.deepEqual([...P.imageFiles(null)], []);
  assert.equal(P.imageSrc("th-1", "ab.png"), "api/threads/th-1/images/ab.png");
  assert.equal(P.lastLine({body: "", images: [{}, {}]}), "🖼 이미지 2장");
  assert.equal(P.lastLine({body: "look\nmore", images: [{}]}), "look 🖼1");
  assert.equal(P.lastLine({body: "plain"}), "plain");

  ctx.URL = {createObjectURL: (f) => `blob:${f.name}`, revokeObjectURL: () => {}};
  const imgThread = {id: "th-5", title: "Pictures", status: "open", opened_by: "user", last_seq: 1,
                     last: {author: "user", body: "", images: [{name: "aa.png"}]}};
  const imgMessages = [{seq: 1, author: "user", body: "", images: [{name: "aa.png", type: "image/png"}],
                        at: "2026-10-13T00:00:00+00:00"}];
  const calls3 = [];
  let failStage = false;
  let staged = 0;
  const api3 = async (p, opts = {}) => {
    const isStage = p === "/api/threads/images";
    const isUpload = p.startsWith("/api/uploads?");
    calls3.push([opts.method || "GET", p, isStage || isUpload ? opts.headers["Content-Type"]
                 : (opts.body ? JSON.parse(opts.body) : undefined)]);
    let doc = {};
    if (isUpload) doc = {id: "f".repeat(20)};
    else if (isStage) {
      if (failStage) return {ok: false, status: 413, json: async () => ({error: "too big"})};
      doc = {id: `img${++staged}.png`};
    } else if (p.startsWith("/api/threads?session=")) doc = {session: "s3", threads: [imgThread], open: 1, agent_seq: 0};
    else if (/^\/api\/threads\/th-5\?after=/.test(p)) {
      const after = Number(p.split("after=")[1]);
      doc = {thread: imgThread, messages: imgMessages.filter((m) => m.seq > after)};
    } else if (p === "/api/threads/th-5/messages") doc = {message: {seq: 2}, delivered: true};
    else if (p === "/api/threads") doc = {thread: {id: "th-5"}, delivered: true};
    return {ok: true, status: 200, json: async () => doc};
  };
  const host3 = document.createElement("div");
  P.mount(host3, "s3", {api: api3, storageKey: "k3"});
  await settle();
  const pics = host3.querySelectorAll(".th-images img");
  assert.equal(pics.length, 1, "a message shows its images");
  assert.equal(pics[0].src, "api/threads/th-5/images/aa.png");
  assert.equal(host3.querySelectorAll(".th-msg .th-msg-body").length, 0, "an images-only message draws no empty text");
  assert.match(host3.querySelector(".th-item-last").text(), /🖼 이미지 1장/);

  // paste an image: it waits under the composer; text of the paste is not taken
  const cbox = host3.querySelector(".th-compose .th-text");
  cbox.fire("paste", {clipboardData: {items: [{kind: "file", type: "image/png", getAsFile: () => ({type: "image/png", name: "shot.png"})}]}});
  await settle();
  let thumbs = host3.querySelectorAll(".th-attach-item img");
  assert.equal(thumbs.length, 1, "the pasted image waits as a thumbnail");
  assert.equal(thumbs[0].src, "blob:shot.png");
  cbox.fire("paste", {clipboardData: {items: [{kind: "string", type: "text/plain"}]}});
  assert.equal(host3.querySelectorAll(".th-compose .th-attach-item").length, 1, "a text paste attaches nothing");

  // a failed upload keeps the image for the next try and posts nothing
  failStage = true;
  host3.querySelector(".th-compose .th-send").fire("click");
  await settle();
  assert.ok(!calls3.some((c) => c[1] === "/api/threads/th-5/messages"), "no post when an image did not upload");
  assert.match(host3.querySelector(".th-head").text(), /too big/);
  assert.equal(host3.querySelectorAll(".th-compose .th-attach-item").length, 1, "the image stays attached");
  failStage = false;

  // Enter with no text sends the image alone: staged first, then named in the post
  calls3.length = 0;
  host3.querySelector(".th-compose .th-text").fire("keydown", {key: "Enter"});
  await settle();
  assert.deepEqual(calls3.filter((c) => c[0] === "POST").map((c) => [c[1], c[2]]),
                   [["/api/threads/images", "image/png"], ["/api/threads/th-5/messages", {text: "", images: ["img1.png"]}]]);
  assert.equal(host3.querySelectorAll(".th-compose .th-attach-item").length, 0, "sent images leave the composer");

  // the × takes an attached image back out
  host3.querySelector(".th-compose .th-text").fire("paste", {clipboardData: {files: [{type: "image/webp", name: "w.webp"}]}});
  await settle();
  host3.querySelector(".th-attach-drop").fire("click");
  await settle();
  assert.equal(host3.querySelectorAll(".th-compose .th-attach-item").length, 0);

  // a new thread carries the images dropped on its form
  host3.querySelector(".th-new").fire("click");
  await settle();
  const nform = host3.querySelector(".th-newform");
  nform.fire("drop", {dataTransfer: {files: [{type: "image/png", name: "d.png"}, {type: "text/plain", name: "x.txt"}]}});
  await settle();
  // claunch-5gpcf: the file that is not an image is attached as a file
  assert.equal(nform.querySelectorAll(".th-attach-item").length, 2, "the image and the file are attached");
  assert.equal(nform.querySelectorAll(".th-attach-file").length, 1, "the text file waits as a file chip");
  calls3.length = 0;
  host3.querySelector(".th-newform .th-send").fire("click");
  await settle();
  assert.deepEqual(calls3.filter((c) => c[0] === "POST").map((c) => [c[1], c[2]]),
                   [["/api/threads/images", "image/png"], ["/api/uploads?name=x.txt", "application/octet-stream"],
                    ["/api/threads", {to: "s3", title: "", images: ["img2.png"], files: ["f".repeat(20)]}]]);
  P.unmount();

  /* the header button's rail (app.js) */
  const start = app.indexOf("function syncSessionResourceButtons(");
  const end = app.indexOf("async function refreshCflow2Rail(");
  assert.ok(start > 0 && end > start);
  const btn = document.createElement("button");
  const rail = {sessions: {}};
  const appStore = new Map();
  const noticed = [], cleared = [];
  const actx = vm.createContext({
    BASE: "/", currentPage: "terminal", currentName: "s1", sessionSimCache: [], cflow2RailCache: {},
    sessionsCache: [], sessCflowRun: () => null, isPiDurable: () => false, $: (id) => (id === "term-threadsbtn" ? btn : null),
    sessLayoutFor: () => ({split: false, pane: "run"}),
    talkShows: (pane, what) => pane === what || pane === "both",
    localStorage: {getItem: (k) => (appStore.has(k) ? appStore.get(k) : null), setItem: (k, v) => appStore.set(k, String(v))},
    api: async () => ({ok: true, json: async () => rail}),
    threadNoticeSync: (rows) => noticed.push(Object.keys(rows)),
    threadNoticeClear: (name) => cleared.push(name),
    JSON,
  });
  vm.runInContext(app.slice(start, end), actx);
  rail.sessions = {s1: {open: 1, agent_seq: 5}};
  await actx.refreshThreadsRail();
  assert.ok(btn.classList.contains("has-resource"), "an open thread lights 🗪");
  assert.ok(!btn.classList.contains("has-news"), "a session never seen here starts caught up");
  rail.sessions = {s1: {open: 1, agent_seq: 8}};
  await actx.refreshThreadsRail();
  assert.ok(btn.classList.contains("has-news"), "a newer agent message is news");
  assert.deepEqual(noticed, [["s1"], ["s1"]], "each changed rail goes to the reply notices");
  actx.markThreadsSeen("s1");
  actx.syncSessionResourceButtons();
  assert.ok(!btn.classList.contains("has-news"), "seen is caught up");
  assert.deepEqual(cleared, [], "the button's news alone leaves the reply cards up");
  actx.sessLayoutFor = () => ({split: true, pane: "threads"});
  rail.sessions = {s1: {open: 1, agent_seq: 9}};
  await actx.refreshThreadsRail();
  assert.deepEqual(cleared, ["s1"], "a threads pane on the session takes its reply cards down");
  actx.sessLayoutFor = () => ({split: false, pane: "run"});
  rail.sessions = {s1: {open: 0, agent_seq: 8}};
  await actx.refreshThreadsRail();
  assert.ok(!btn.classList.contains("has-resource"), "all closed: dark");

  /* wiring */
  assert.ok(html.includes('id="term-threadsbtn"'));
  assert.ok(html.indexOf('src="static/threads-pane.js"') > 0
            && html.indexOf('src="static/threads-pane.js"') < html.indexOf('src="static/app.js"'));
  // The threads pane is placed by the conversation pane (talk-pane.js,
  // claunch-78dpm): openSplit mounts TalkPane for threads, qa and both.
  assert.ok(/isTalkPane\(kind\)[\s\S]{0,200}TalkPane\?\.mount/.test(app), "openSplit mounts the pane");
  assert.ok(app.includes('if (isTalkPane(splitKind)) globalThis.TalkPane?.unmount();'));
  assert.ok(app.includes('$("term-threadsbtn").addEventListener("click", () => toggleSplitPane("threads"));'));
  assert.equal(app.split("refreshThreadsRail(),").length - 1, 2, "boot and the poll both read the rail");
  assert.ok(!/innerHTML/.test(src), "threads-pane.js never assigns HTML");
  console.log("threads pane checks passed");
})().catch((e) => { console.error(e); process.exitCode = 1; });
