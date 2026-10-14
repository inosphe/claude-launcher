/* Session threads pane (threads-pane.js) and its header button (app.js),
   claunch-doj98.

   What has to hold:
     helpers   -- excerpt takes the first non-empty line and shortens it;
                  unseen marks threads whose last message is the agent's and
                  newer than this browser's read position; Enter sends,
                  Shift+Enter and IME composition do not; pick keeps the
                  remembered thread, else the first open one.
     pane      -- mounting lists the session's threads, opens the first open
                  one, renders both authors' text through mdInto (HTML stays
                  text), and a send posts the text to that thread; Enter
                  empties the composer at once and shows a pending bubble
                  until the answer, and a failed send marks the bubble and
                  puts the text back (claunch-5usj6); a new thread posts {to, title, text}; close
                  posts to /close; reading a thread moves its read position;
                  unmount stops the poll; ◂ 목록 folds the list away while a
                  conversation is open and the fold is remembered
                  (claunch-1n081).
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
      attrs: {},
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
  const localStorage = {getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v))};
  const timers = [];
  const rendered = [];   // [class of the bubble, text] for each mdInto call
  const mdInto = (n, text) => { rendered.push(text); n.textContent = text; return n; };
  const ctx = vm.createContext({
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
  const api = async (p, opts = {}) => {
    calls.push([opts.method || "GET", p, opts.body ? JSON.parse(opts.body) : undefined]);
    if (failCreate && p === "/api/threads") return {ok: false, status: 500, json: async () => ({error: "boom"})};
    if (failPost && p === "/api/threads/th-1/messages") return {ok: false, status: 500, json: async () => ({error: "lost"})};
    let doc = {};
    if (p.startsWith("/api/threads?session=")) doc = {session: "s1", threads, open: 1, agent_seq: 2};
    else if (/^\/api\/threads\/th-1\?after=/.test(p)) {
      const after = Number(p.split("after=")[1]);
      doc = {thread: threads[0], messages: messages["th-1"].filter((m) => m.seq > after)};
    } else if (p === "/api/threads/th-1/messages") doc = {message: {seq: 3}, delivered: true, hold: postHold};
    else if (p === "/api/threads") doc = {thread: {id: "th-9"}, delivered: false};
    else if (p.endsWith("/close")) doc = {thread: threads[0]};
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

  // send: Enter in the composer posts to the thread
  const box = host.querySelector(".th-compose .th-text");
  box.value = "and yaml?";
  box.fire("keydown", {key: "Enter"});
  await settle();
  assert.deepEqual(calls.find((c) => c[0] === "POST"), ["POST", "/api/threads/th-1/messages", {text: "and yaml?"}]);
  assert.ok(changed >= 1, "a send refreshes the header light");

  // a second Enter before the first answer is not a second message (claunch-7xp0f)
  const posts = () => calls.filter((c) => c[0] === "POST" && c[1] === "/api/threads/th-1/messages").length;
  const sent = posts();
  const box2 = host.querySelector(".th-compose .th-text");
  box2.value = "twice?";
  box2.fire("keydown", {key: "Enter"});
  assert.ok(host.querySelector(".th-compose .term-btn").disabled, "보내기 is disabled while the post is out");
  assert.equal(host.querySelector(".th-compose .th-text").value, "", "Enter empties the composer before the answer");
  const out = host.querySelectorAll(".th-sending");
  assert.equal(out.length, 1, "the message being sent shows as a pending bubble");
  assert.equal(out[0].querySelector(".th-msg-body").textContent, "twice?");
  host.querySelector(".th-compose .th-text").fire("keydown", {key: "Enter"});
  host.querySelector(".th-compose .term-btn").fire("click");
  await settle();
  assert.equal(posts(), sent + 1, "one message for a double send");
  assert.equal(host.querySelectorAll(".th-sending").length, 0, "the pending bubble goes once answered");
  assert.ok(!host.querySelector(".th-compose .term-btn").disabled, "보내기 comes back after the answer");
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
  host.querySelector(".th-newform .term-btn").fire("click");
  await settle();
  assert.equal(host.querySelector(".th-newform .th-title-input").value, "Deploy", "a failed create keeps the title");
  assert.ok(!host.querySelector(".th-newform .term-btn").disabled, "시작 comes back after a failure");
  assert.match(host.querySelector(".th-head").text(), /boom/);
  failCreate = false;
  calls.length = 0;
  const form = host.querySelector(".th-newform");
  form.querySelector(".th-title-input").value = "Deploy";
  form.querySelector(".th-text").value = "when?";
  const startBtn = form.querySelector(".term-btn");
  startBtn.fire("click");
  startBtn.fire("click");   // a double click before the first answer (claunch-7xp0f)
  assert.ok(startBtn.disabled, "시작 is disabled while the post is out");
  await settle();
  assert.equal(calls.filter((c) => c[1] === "/api/threads").length, 1, "one thread for a double click");
  assert.deepEqual(calls.filter((c) => c[1] === "/api/threads").pop(), ["POST", "/api/threads", {to: "s1", title: "Deploy", text: "when?"}]);
  assert.match(host.querySelector(".th-head").text(), /전달하지 못했습니다/);

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

  /* the header button's rail (app.js) */
  const start = app.indexOf("function syncSessionResourceButtons(");
  const end = app.indexOf("async function refreshCflow2Rail(");
  assert.ok(start > 0 && end > start);
  const btn = document.createElement("button");
  const rail = {sessions: {}};
  const appStore = new Map();
  const actx = vm.createContext({
    BASE: "/", currentPage: "terminal", currentName: "s1", sessionSimCache: [], cflow2RailCache: {},
    sessionsCache: [], sessCflowRun: () => null, isPiDurable: () => false, $: (id) => (id === "term-threadsbtn" ? btn : null),
    sessLayoutFor: () => ({split: false, pane: "run"}),
    localStorage: {getItem: (k) => (appStore.has(k) ? appStore.get(k) : null), setItem: (k, v) => appStore.set(k, String(v))},
    api: async () => ({ok: true, json: async () => rail}),
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
  actx.markThreadsSeen("s1");
  actx.syncSessionResourceButtons();
  assert.ok(!btn.classList.contains("has-news"), "seen is caught up");
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
