/* The notification history, run against a stub daemon (claunch-l978l).

   A card in the corner used to be the only record of what the page said, and
   the restart gate's card could not be put away without answering it. The
   history keeps every notice and every gate request this browser saw, and the
   panel behind the rail's bell shows them again.

   What is checked here: notify() writes a history row; the restart card has a
   Hide press that takes the card down without posting anything, and the poll
   does not put it back; the request stays in the bell's count and in the
   panel's waiting list with its buttons, and Show card brings the card back;
   the panel's decisions post to the gate's own endpoints; a settled request
   gets the daemon's word for how it ended; a hidden id is forgotten once its
   request leaves the pending set; and all of it survives a reload through
   localStorage, while a page without storage still works. Time, storage and
   the network belong to the harness.

   QA requests (claunch-k7c32): a question a session files, or an answered
   one it rewrites (stale), puts up a card that opens the session and writes
   a history row naming it; each request is announced once per browser, the
   first poll of a fresh browser announces nothing, and the card goes when
   the request stops waiting.

   Opening from the history (claunch-x76l8): a row about a thread reply or a
   QA request keeps its target, and clicking the row opens that session on
   that thread or that question, as the card did, and leaves the panel open
   (claunch-0fdx7) so the next row can be opened from it too — the same for
   a waiting request's Open press. A row written before rows kept a target
   opens nothing. Every card carries ×, which puts it away without following
   it.

   An open panel hides the corner strip (claunch-k2khb): a notice that
   arrives while it is open is written to the history only and puts up no
   card, now or after the panel closes. Opening the panel puts every card
   away for good (claunch-paqfs): notices are dismissed and waiting requests
   hidden, so closing it brings none back; a request that came in while it
   was open is hidden on close, unless "Show card" put it back in the panel.

   The panel is a column of the layout on a desktop (claunch-w8q00): open, it
   narrows the session area, so a run's approve strip stays on screen beside
   it. A phone lays it over the page.

   Marks and the All / Kept filter (claunch-knuet; Kept was labelled Marked
   before claunch-v995u): ☆ on a history row marks
   it ★ without reading it or opening its target; Kept shows only those
   rows and All every row again; the filter and the marks survive a reload;
   Clear and the 100-row cap keep marked rows; Clear is only in the All view
   and the cap gives way rather than drop a marked row (claunch-knuet.1).
   Not read (claunch-v995u) shows only the unread rows; a row read there
   leaves the view, a filter stored as "marked" still opens on Kept.

   Marking a waiting request read (claunch-qqy2i): Mark read on a row of the
   panel's "Waiting on you" list takes the request off that heading's count
   and off the bell, and lists it under "Read, still open" with its buttons
   and countdown. Nothing is posted — the request stays pending on the daemon
   — and Mark unread counts it again. The flag survives a reload and reaches
   this browser's other tabs; a request that leaves the pending set is
   forgotten, so a later one the daemon gives the same id is unread again. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web",
                         "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

function slice(from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to, a + 1);
  if (a < 0 || b < 0 || b <= a) throw new Error(`cannot slice ${from} .. ${to}`);
  return src.slice(a, b);
}

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

const BASE = 1700000000000;

function memoryStorage() {
  const data = new Map();
  return {
    data,
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => { data.set(k, String(v)); },
    removeItem: (k) => { data.delete(k); },
  };
}

/* ---- stub world ------------------------------------------------------- */
function build(opts) {
  const o = opts || {};

  function node(cls, text) {
    const n = {
      className: cls || "", textContent: text === undefined ? "" : text,
      title: "", children: [], parentNode: null, on: {},
      disabled: false, type: "",
      classList: {
        contains: (c) => n.className.split(" ").includes(c),
        toggle: (c, on) => {
          const has = n.classList.contains(c);
          if (on === undefined ? has : !on) {
            n.className = n.className.split(" ").filter((x) => x !== c).join(" ");
          } else if (!has) { n.className = `${n.className} ${c}`.trim(); }
        },
        add: (c) => n.classList.toggle(c, true),
        remove: (c) => n.classList.toggle(c, false),
      },
      appendChild: (c) => { c.parentNode = n; n.children.push(c); return c; },
      // The corner strip puts a new card on top (claunch-efpoi).
      insertBefore: (c, ref) => {
        const i = n.children.indexOf(ref);
        c.parentNode = n;
        if (i < 0) n.children.push(c); else n.children.splice(i, 0, c);
        return c;
      },
      get firstChild() { return n.children[0] || null; },
      removeChild: (c) => {
        const i = n.children.indexOf(c);
        if (i >= 0) n.children.splice(i, 1);
        c.parentNode = null;
      },
      addEventListener: (ev, fn) => { n.on[ev] = fn; },
      click: (ev) => n.on.click && n.on.click(ev || { stopPropagation() {} }),
      querySelector: (sel) => {
        const cls = sel.slice(1);
        const walk = (x) => {
          for (const c of x.children) {
            if (c.classList.contains(cls)) return c;
            const deep = walk(c);
            if (deep) return deep;
          }
          return null;
        };
        return walk(n);
      },
      isConnected: true,
      text: () => [n.textContent, ...n.children.map((c) => c.text())].join(" "),
    };
    Object.defineProperty(n, "innerHTML", {
      get: () => "",
      set: () => { n.children.forEach((c) => { c.parentNode = null; }); n.children = []; },
    });
    return n;
  }

  const bell = node("notice-bell");
  bell.appendChild(node("notice-bell-count"));
  const nodes = {
    notices: node(),
    "daemon-info": node(),
    "notice-bell": bell,
    "notice-panel": node("hidden"),
    "notice-panel-body": node(),
    "notice-settings": node("hidden"),
    "notice-settings-body": node(),
  };

  const timers = [];
  let ticket = 0;
  const setTimeoutStub = (fn, ms) => { timers.push({ id: ++ticket, fn, ms }); return ticket; };
  const clearTimeoutStub = (id) => {
    const i = timers.findIndex((t) => t.id === id);
    if (i >= 0) timers.splice(i, 1);
  };

  const clock = { at: BASE };
  class FakeDate extends Date {
    constructor(...a) { super(...(a.length ? a : [clock.at])); }
    static now() { return clock.at; }
  }

  const apiCalls = [];
  const apiBodies = [];
  const gate = { restart: null, goto: [], reposync: [], nudges: [], meshjoin: [] };
  const apiStub = async (p, opts) => {
    apiCalls.push(p);
    if (opts && opts.body) apiBodies.push([p, JSON.parse(opts.body)]);
    const answer = (body) => ({ ok: true, json: async () => JSON.parse(JSON.stringify(body)) });
    if (p === "/api/cflow2/approve") return answer({ ok: true });
    if (p === "/api/daemon/restart-request") return answer({ request: gate.restart });
    if (p === "/api/cflow/goto-requests") return answer({ requests: gate.goto });
    if (p === "/api/reposync/requests") return answer({ requests: gate.reposync });
    if (p === "/api/flow-nudges") return answer({ requests: gate.nudges });
    if (p === "/api/mesh-join-requests") return answer({ requests: gate.meshjoin });
    if (p === "/api/daemon/restart-request/reject") {
      gate.restart = { ...gate.restart, status: "rejected" };
      return answer({ ok: true });
    }
    if (p === "/api/daemon/restart-request/extend") {
      gate.restart = { ...gate.restart, extensions: (gate.restart.extensions || 0) + 1 };
      return answer({ ok: true });
    }
    if (/\/(approve|deny|reject)$/.test(p)) return answer({ ok: true });
    if (p === "/api/notices/sync" && o.daemon) {
      if (o.daemon.down) throw new Error("offline");
      if (o.daemon.missing) return { ok: false, status: 404, json: async () => ({}) };
      return answer(o.daemon.sync(JSON.parse(opts.body)));
    }
    throw new Error(`unexpected api call ${p}`);
  };

  const storage = o.storage === undefined ? memoryStorage() : o.storage;
  const loc = { hash: "" };
  const layouts = {};   // session -> split layout, as setSessLayout left it
  const code = slice("/* notices — the page's own voice",
                     "pollTimer = setInterval(pollTick, DASHBOARD_POLL_MS);") +
    // commitRef / commitText (claunch-tb145): the gate's tips line uses them.
    "\n" + slice("/* A commit hash drawn as a link", "function wsWorktreeList(") +
    // The page-wide Escape and `storage` listeners of the panel
    // (claunch-efpoi.1) sit with the other top-level listeners, outside the
    // notices section; they run here against a document and window of
    // their own, so the section above still sees no `document`.
    "\n;(function (document, window) {\n" +
    slice('document.addEventListener("keydown", (ev) => {\n  if (ev.key !== "Escape") return;\n',
          '$("refresh-all").addEventListener') +
    "\n})(__doc, __win);";
  const listeners = { document: {}, window: {} };
  const target = (on) => ({ addEventListener: (ev, fn) => { on[ev] = fn; } });
  const noop = () => {};
  const api = new Function(
    "$", "el", "setTimeout", "clearTimeout", "Date", "api", "location",
    "localStorage", "daemonHealth",
    "renderRelayBadge", "refreshProfiles", "refreshHarnesses", "refreshRoles",
    "refreshWorkspaces", "refreshSessions", "refreshMeshList", "refreshCflow",
    "refreshTermQueued", "reconnectNow", "route", "refreshNewWorktree",
    "refreshProjects", "ensureControlSocket", "refreshCflow2Rail",
    "refreshTermWindow", "refreshTermTimers",
    "sessLayoutFor", "setSessLayout", "talkShows", "BroadcastChannel",
    "__doc", "__win",
    code +
    "\nreturn {notify, refreshRestartGate, refreshGotoGate, refreshReposyncGate, urgentAskSync," +
    " refreshNudgeGate, refreshMeshJoinGate, noticeGatePoll," +
    " noticePanelToggle, noticeHistory, qaNoticeSync, threadNoticeSync, threadNoticeClear," +
    " noticeSyncTick, noticeSync, noticeMarkRead, noticeTabs, gateIsHidden," +
    " killReapedNotify," +
    " NOTICE_SOURCES, gatePending, gateHistoryOpen, noticeSourceSet, noticePanelPaint," +
    " noticeSettingsToggle, noticeBellPaint, noticeSourceOf, noticeGateRefresh," +
    " get cards() { return [...notices.keys()]; }};"
  )(
    (id) => nodes[id],
    (tag, cls, text) => node(cls, text),
    setTimeoutStub, clearTimeoutStub, FakeDate, apiStub, loc,
    storage, async () => null,
    noop, noop, noop, noop, noop, noop, noop, noop, noop, noop, noop, noop,
    noop, noop, noop, noop, noop,
    (name) => layouts[name] || { split: false, pane: "run" },
    (name, patch) => { layouts[name] = { ...(layouts[name] || { split: false, pane: "run" }), ...patch }; },
    (pane, what) => pane === what || pane === "both",
    // Node has a real BroadcastChannel; a world joins a tab hub only when
    // the test gives it one, so worlds never hear each other by accident.
    o.hub ? o.hub.Channel : undefined,
    target(listeners.document), target(listeners.window),
  );

  const all = (n, out) => { out.push(n); n.children.forEach((c) => all(c, out)); return out; };
  return {
    api, nodes, clock, gate, apiCalls, apiBodies, storage, loc, layouts, timers, listeners,
    strip: () => nodes.notices.children,
    said: () => nodes.notices.children.map((c) => c.text()).join(" | "),
    panel: () => nodes["notice-panel-body"].text(),
    badge: () => bell.children[0].textContent,
    panelRows: () => nodes["notice-panel-body"].children.filter((c) => c.classList.contains("notice-row")),
    press: (root, label) => all(root, []).find((n) => n.textContent === label),
  };
}

/* The daemon's notice store (daemon/notice_sync.py), its rules only:
   dismissed goes one way, a read change made against an older base than
   the stored read is refused and the stored record comes back. */
function fakeNoticeDaemon() {
  const d = { epoch: "e1", seq: 0, records: new Map(), bodies: [], down: false, missing: false };
  d.sync = (body) => {
    d.bodies.push(body);
    const conflicts = [];
    for (const ch of body.changes || []) {
      const rec = d.records.get(ch.id) ||
        { id: ch.id, read: null, read_seq: 0, dismissed: false, seq: 0 };
      let moved = false;
      let readMoved = false;
      if (ch.dismissed && !rec.dismissed) { rec.dismissed = true; moved = true; }
      if ("read" in ch && rec.read !== ch.read) {
        if (rec.read !== null && rec.read_seq > (ch.base || 0)) conflicts.push({ ...rec });
        else { rec.read = ch.read; moved = readMoved = true; }
      }
      if (!moved) continue;
      d.seq += 1;
      if (readMoved) rec.read_seq = d.seq;
      rec.seq = d.seq;
      d.records.set(ch.id, rec);
    }
    const since = body.epoch === d.epoch ? body.since : 0;
    const changes = [...d.records.values()].filter((r) => r.seq > since)
      .sort((a, b) => a.seq - b.seq).map((r) => ({ ...r }));
    return { epoch: d.epoch, seq: d.seq, changes, conflicts };
  };
  return d;
}

/* The tabs of one browser (claunch-j6lg0): a BroadcastChannel stand-in that
   hands a message to every other channel of the same name, the way the real
   one does (later, so a tab never hears itself). `sent` keeps every post. */
function tabHub() {
  const hub = { members: [], sent: [], queue: [] };
  hub.Channel = class {
    constructor(name) { this.name = name; this.onmessage = null; hub.members.push(this); }
    postMessage(data) {
      const copy = JSON.parse(JSON.stringify(data));
      hub.sent.push(copy);
      for (const m of hub.members) {
        if (m !== this && m.name === this.name) hub.queue.push(() => m.onmessage && m.onmessage({ data: copy }));
      }
    }
  };
  hub.flush = async () => {
    while (hub.queue.length) hub.queue.shift()();
    await new Promise((r) => setImmediate(r));
    while (hub.queue.length) hub.queue.shift()();
  };
  return hub;
}

function restartRec(rec) {
  return { id: "r1", session: "s9", reason: "serve master 1234abcd",
           requested_at: new Date(BASE).toISOString(),
           deadline: new Date(BASE + 300000).toISOString(),
           status: "pending", extensions: 0, ...(rec || {}) };
}

function gotoRec(rec) {
  return { id: "g1", session: "lead", target_session: "w1", from: "work",
           step: "rebase", reason: "parked wrong", status: "pending",
           deadline: new Date(BASE + 240000).toISOString(), ...(rec || {}) };
}

(async () => {
  /* --- notify() keeps what it said ------------------------------------- */
  {
    const w = build();
    w.api.notify("daemon back", "the same daemon answered again", { key: "daemon-link" });
    check("a notice writes a history row",
          w.api.noticeHistory.length === 1 && w.api.noticeHistory[0].title === "daemon back",
          w.api.noticeHistory);
    check("and the row is stored in this browser",
          /daemon back/.test(w.storage.getItem("claunch.noticeHistory") || ""),
          [...w.storage.data.keys()]);
    check("an unread row lights the bell", w.nodes["notice-bell"].classList.contains("unread"),
          w.nodes["notice-bell"].className);
    w.api.noticePanelToggle(true);
    check("opening the panel shows it", /daemon back/.test(w.panel()), w.panel());
    check("opening it does not read it (claunch-x76l8)",
          w.nodes["notice-bell"].classList.contains("unread") && w.badge() === "1",
          [w.nodes["notice-bell"].className, w.badge()]);
    check("while it is open the strip is hidden (claunch-k2khb)",
          w.nodes.notices.classList.contains("panel-open"), w.nodes.notices.className);
    w.api.noticePanelToggle(false);
    check("and shown again once it closes", !w.nodes.notices.classList.contains("panel-open"),
          w.nodes.notices.className);
  }

  /* --- an open panel takes the notice, no card (claunch-k2khb) ---------- */
  {
    const w = build();
    w.api.notify("before", "a", { key: "k0", sticky: true });
    w.api.noticePanelToggle(true);
    check("opening the panel puts the card up before it away (claunch-paqfs)",
          w.strip().length === 0 && w.api.cards.length === 0, w.said());
    check("its row stays in the history, unread",
          w.api.noticeHistory.some((e) => e.title === "before" && e.read === false), w.api.noticeHistory);
    const node = w.api.notify("while open", "b", { key: "k1", sticky: true });
    check("a notice while the panel is open puts up no card",
          node === null && w.strip().length === 0, w.said());
    check("it is still written to the history and shown in the panel",
          w.api.noticeHistory[0].title === "while open" && /while open/.test(w.panel()),
          w.panel());
    check("and still counts as unread", w.badge() === "2", w.badge());
    w.api.noticePanelToggle(false);
    check("closing the panel brings no card back, before or while open",
          w.strip().length === 0, w.said());
    w.api.notify("after", "c", { key: "k2", sticky: true });
    check("a notice after it closes puts up a card again", /after/.test(w.said()), w.said());
  }

  /* --- read marks (claunch-x76l8) -------------------------------------- */
  {
    const w = build();
    w.api.notify("one", "a", { key: "k1", sticky: true });
    w.api.notify("two", "b", { key: "k2", sticky: true });
    w.api.notify("three", "c", { key: "k3", sticky: true });
    check("each new row is unread and counted", w.badge() === "3" &&
            w.api.noticeHistory.every((e) => e.read === false), [w.badge(), w.api.noticeHistory]);
    w.strip().find((c) => /one/.test(c.text())).click();
    check("clicking a floating card marks its row read",
          w.api.noticeHistory.find((e) => e.title === "one").read === true && w.badge() === "2",
          [w.badge(), w.api.noticeHistory]);
    w.press(w.strip().find((c) => /two/.test(c.text())), "×").click();
    check("× does not read it", w.api.noticeHistory.find((e) => e.title === "two").read === false &&
            w.badge() === "2", w.badge());
    w.api.noticePanelToggle(true);
    const rowOf = (t) => w.panelRows().find((r) => r.querySelector(".notice-title").textContent === t);
    check("an unread row is marked so", rowOf("two").classList.contains("unread") &&
            rowOf("one").classList.contains("read"), [rowOf("two").className, rowOf("one").className]);
    rowOf("two").click();
    check("clicking a history row marks it read",
          w.api.noticeHistory.find((e) => e.title === "two").read === true && w.badge() === "1",
          w.badge());
    w.press(rowOf("two"), "Mark unread").click();
    check("Mark unread counts it again",
          w.api.noticeHistory.find((e) => e.title === "two").read === false && w.badge() === "2" &&
            rowOf("two").classList.contains("unread"), w.badge());
    check("and stays in this browser",
          JSON.parse(w.storage.getItem("claunch.noticeHistory")).find((e) => e.title === "two").read === false);
    w.press(rowOf("three"), "Mark read").click();
    check("Mark read on a row reads only it", w.badge() === "1" &&
            w.api.noticeHistory.find((e) => e.title === "three").read === true, w.badge());
    w.press(w.nodes["notice-panel-body"], "Mark all read").click();
    check("Mark all read empties the count", w.badge() === "" &&
            w.api.noticeHistory.every((e) => e.read), [w.badge(), w.api.noticeHistory]);
    check("and leaves nothing to mark", !w.press(w.nodes["notice-panel-body"], "Mark all read"), w.panel());
  }

  /* --- routine notices (claunch-ewk3x) --------------------------------- */
  /* The reviewer reap repeats whenever a workflow ends one and needs no
     answer: its card is short-lived and its history row is born read, so the
     bell never turns a night of them into a backlog. The ordinary notice's
     own timing and unread row are checked beside it so the routine flag is
     what made the difference. */
  {
    const w = build();
    w.api.killReapedNotify([]);   // a browser's first poll only remembers
    check("a fresh browser says nothing about reaps already on the board",
          w.strip().length === 0 && w.timers.length === 0, w.said());
    w.api.killReapedNotify([
      { id: "k1", status: "reaped", session: "s9", target: "s9-rev", reason: "settle-reap" },
    ]);
    const row = w.api.noticeHistory[0];
    check("a reaped reviewer still gets its card",
          row && row.title === "reviewer ended by its workflow: s9-rev", row && row.title);
    check("its history row is born read, so the bell does not count it",
          row.read === true && w.badge() === "", [row.read, w.badge()]);
    check("and its card leaves after 5 s",
          w.timers.length === 1 && w.timers[0].ms === 5000, w.timers);
    w.timers[0].fn();
    check("the card is gone after that timer", w.strip().length === 0, w.said());

    w.api.notify("ordinary", "x", { key: "k9" });
    const ordinary = w.api.noticeHistory.find((e) => e.title === "ordinary");
    check("an ordinary notice stays 8 s and stays unread",
          w.timers.length === 1 && w.timers[0].ms === 8000 && ordinary.read === false && w.badge() === "1",
          [w.timers, ordinary.read, w.badge()]);

    // The copy confirmation (claunch-ewk3x.1): routine AND an explicit 1500 ms,
    // so the short card is kept and only the unread row goes away.
    w.api.notify("Copied session name", "s1", { key: "term-title-copy", ms: 1500, routine: true });
    const copied = w.api.noticeHistory.find((e) => e.title === "Copied session name");
    check("a routine notice keeps its explicit ms and is born read",
          w.timers[w.timers.length - 1].ms === 1500 && copied.read === true && w.badge() === "1",
          [w.timers, copied.read, w.badge()]);
  }

  /* --- the session link inside a history row (claunch-49fvl) ----------- */
  {
    const w = build();
    w.api.notify("s2 asks", "why", { key: "k1", sticky: true, session: "s2" });
    w.api.notify("no session", "a plain notice", { key: "k2", sticky: true });
    w.api.noticePanelToggle(true);
    const row = () => w.panelRows().find((r) => r.querySelector(".notice-title").textContent === "s2 asks");
    const entry = (t) => w.api.noticeHistory.find((e) => e.title === t);
    check("a row with a session starts unread",
          entry("s2 asks").read === false && w.badge() === "2", [w.api.noticeHistory, w.badge()]);
    w.press(row(), "s2").click();
    check("its session link moves to that session and marks the row read",
          w.loc.hash === "#/s/s2" && entry("s2 asks").read === true && w.badge() === "1",
          [w.loc.hash, w.api.noticeHistory, w.badge()]);
    check("and reads only that row", entry("no session").read === false, w.api.noticeHistory);
  }
  {
    const storage = memoryStorage();
    storage.setItem("claunch.noticeSeen", JSON.stringify(BASE));
    storage.setItem("claunch.noticeHistory", JSON.stringify([
      { id: "h2", at: BASE + 10, title: "after", sub: "", kind: "" },
      { id: "h1", at: BASE - 10, title: "before", sub: "", kind: "" }]));
    const w = build({ storage });
    check("a row saved before read marks is read if the panel was opened after it",
          w.api.noticeHistory.find((e) => e.id === "h1").read === true &&
            w.api.noticeHistory.find((e) => e.id === "h2").read === false, w.api.noticeHistory);
  }

  /* --- marks and the All / Kept filter (claunch-knuet) ----------------- */
  {
    const w = build();
    let opened = 0;
    w.api.notify("one", "a", { key: "k1", sticky: true });
    w.api.notify("two", "b", { key: "k2", sticky: true, session: "s1",
                               target: { thread: "th-1" }, open: () => { opened += 1; } });
    w.api.notify("three", "c", { key: "k3", sticky: true });
    w.api.noticePanelToggle(true);
    const rowOf = (t) => w.panelRows().find((r) => r.querySelector(".notice-title").textContent === t);
    const entry = (t) => w.api.noticeHistory.find((e) => e.title === t);
    const head = () => w.nodes["notice-panel-body"].children
      .find((c) => c.classList.contains("notice-panel-section-row"));
    check("the filter starts at All", w.press(head(), "All").classList.contains("active") &&
            !w.press(head(), "Kept").classList.contains("active") &&
            !w.press(head(), "Not read").classList.contains("active") &&
            !w.press(head(), "Marked"), head().text());
    check("each row has an unmarked ☆", w.panelRows().every((r) => r.querySelector(".notice-star").textContent === "☆"),
          w.panel());
    w.press(rowOf("two"), "☆").click();
    check("☆ marks the row", entry("two").marked === true &&
            rowOf("two").querySelector(".notice-star").textContent === "★" &&
            rowOf("two").querySelector(".notice-star").classList.contains("on"), entry("two"));
    check("without reading it or opening it", entry("two").read === false && opened === 0 &&
            w.loc.hash === "" && w.badge() === "3", [entry("two"), opened, w.badge()]);
    check("and the mark is stored",
          JSON.parse(w.storage.getItem("claunch.noticeHistory")).find((e) => e.title === "two").marked === true);
    w.press(head(), "Kept").click();
    check("Kept shows only the marked rows",
          w.panelRows().length === 1 && rowOf("two") && /History \(1\)/.test(w.panel()) &&
            w.press(head(), "Kept").classList.contains("active"), w.panel());
    check("and the choice is stored", JSON.parse(w.storage.getItem("claunch.noticeFilter")) === "marked");
    check("Kept offers no Clear, which would forget rows it hides (claunch-knuet.1)",
          !w.press(head(), "Clear"), head().text());
    check("Mark all read there reads only what it shows", (() => {
      w.press(head(), "Mark all read").click();
      return entry("two").read === true && entry("one").read === false && w.badge() === "2";
    })(), [w.api.noticeHistory, w.badge()]);
    const second = build({ storage: w.storage });
    second.api.noticePanelToggle(true);
    check("a reload keeps the filter and the mark",
          second.panelRows().length === 1 && /two/.test(second.panel()), second.panel());
    w.press(rowOf("two"), "★").click();
    check("★ unmarks it, and Kept says it is empty",
          entry("two").marked === false && w.panelRows().length === 0 &&
            /no row is kept/.test(w.panel()), w.panel());
    w.press(head(), "All").click();
    check("All shows every row again", w.panelRows().length === 3, w.panel());
    w.press(rowOf("three"), "☆").click();
    w.press(head(), "Clear").click();
    check("Clear keeps the marked row",
          w.api.noticeHistory.length === 1 && entry("three").marked === true, w.api.noticeHistory);
    check("and offers nothing more to clear", !w.press(head(), "Clear"), w.panel());
  }
  /* --- the Not read filter (claunch-v995u) ---------------------------- */
  {
    const w = build();
    w.api.notify("one", "a", { key: "k1", sticky: true });
    w.api.notify("two", "b", { key: "k2", sticky: true });
    w.api.notify("three", "c", { key: "k3", sticky: true });
    w.api.noticePanelToggle(true);
    const rowOf = (t) => w.panelRows().find((r) => r.querySelector(".notice-title").textContent === t);
    const entry = (t) => w.api.noticeHistory.find((e) => e.title === t);
    const head = () => w.nodes["notice-panel-body"].children
      .find((c) => c.classList.contains("notice-panel-section-row"));
    rowOf("one").click();
    w.press(rowOf("three"), "☆").click();
    check("Not read sits after Kept in the filter", (() => {
      const labels = head().children.find((c) => c.classList.contains("notice-filter"))
        .children.map((b) => b.textContent);
      return labels.join(",") === "All,Kept,Not read";
    })(), head().text());
    w.press(head(), "Not read").click();
    check("Not read shows only the unread rows",
          w.panelRows().length === 2 && rowOf("two") && rowOf("three") && !rowOf("one") &&
            /History \(2\)/.test(w.panel()) &&
            w.press(head(), "Not read").classList.contains("active"), w.panel());
    check("and the choice is stored", JSON.parse(w.storage.getItem("claunch.noticeFilter")) === "unread");
    check("Not read offers no Clear, which would forget rows it hides",
          !w.press(head(), "Clear"), head().text());
    w.press(rowOf("two"), "Mark read").click();
    check("a row read there leaves the view", entry("two").read === true &&
            w.panelRows().length === 1 && rowOf("three"), w.panel());
    const second = build({ storage: w.storage });
    second.api.noticePanelToggle(true);
    check("a reload keeps the Not read filter",
          second.panelRows().length === 1 && /three/.test(second.panel()), second.panel());
    w.press(head(), "Mark all read").click();
    check("Mark all read empties it and it says so",
          entry("three").read === true && w.panelRows().length === 0 &&
            /every row has been read/.test(w.panel()) && w.badge() === "", [w.panel(), w.badge()]);
    w.press(head(), "All").click();
    check("All shows the read rows again", w.panelRows().length === 3, w.panel());
  }
  {
    /* A filter stored before the relabel ("marked") opens on Kept; an
       unknown value falls back to All. */
    const storage = build().storage;
    storage.setItem("claunch.noticeFilter", JSON.stringify("marked"));
    const w = build({ storage });
    w.api.notify("one", "a", { key: "k1" });
    w.api.noticePanelToggle(true);
    const head = () => w.nodes["notice-panel-body"].children
      .find((c) => c.classList.contains("notice-panel-section-row"));
    check("a stored \"marked\" filter opens on Kept",
          w.press(head(), "Kept").classList.contains("active"), head().text());
    storage.setItem("claunch.noticeFilter", JSON.stringify("bogus"));
    const v = build({ storage });
    v.api.noticePanelToggle(true);
    const vhead = v.nodes["notice-panel-body"].children
      .find((c) => c.classList.contains("notice-panel-section-row"));
    check("an unknown stored filter falls back to All",
          v.press(vhead, "All").classList.contains("active"), vhead.text());
  }
  {
    const w = build();
    w.api.notify("kept", "", { key: "kept" });
    w.api.noticeHistory[0].marked = true;
    for (let i = 0; i < 120; i += 1) w.api.notify(`n${i}`, "", { key: `n${i}` });
    check("the 100-row cap drops unmarked rows first",
          w.api.noticeHistory.length === 100 && w.api.noticeHistory.some((e) => e.title === "kept") &&
            w.api.noticeHistory[0].title === "n119", w.api.noticeHistory.length);
  }
  {
    /* Every row marked: the cap gives way instead of a ★ row (claunch-knuet.1). */
    const w = build();
    for (let i = 0; i < 100; i += 1) {
      w.api.notify(`m${i}`, "", { key: `m${i}` });
      w.api.noticeHistory[0].marked = true;
    }
    w.api.notify("new1", "", { key: "new1" });
    check("over the cap with every older row marked, no marked row goes",
          w.api.noticeHistory.length === 101 && w.api.noticeHistory.filter((e) => e.marked).length === 100 &&
            w.api.noticeHistory[0].title === "new1", w.api.noticeHistory.length);
    w.api.notify("new2", "", { key: "new2" });
    check("the next row pushes out the unmarked one before it",
          w.api.noticeHistory.length === 101 && w.api.noticeHistory[0].title === "new2" &&
            !w.api.noticeHistory.some((e) => e.title === "new1"), w.api.noticeHistory.slice(0, 3));
    const second = build({ storage: w.storage });
    check("a reload keeps every marked row past the cap",
          second.api.noticeHistory.length === 101 &&
            second.api.noticeHistory.filter((e) => e.marked).length === 100,
          second.api.noticeHistory.length);
  }

  /* --- × puts a card away without following it (claunch-x76l8) --------- */
  {
    const w = build();
    let opened = 0;
    w.api.notify("s1 asks you", "which?", { key: "qa-x", sticky: true, session: "s1",
                                            open: () => { opened += 1; } });
    const x = w.press(w.nodes.notices, "×");
    check("a card carries ×", !!x && x.className === "notice-close", w.said());
    x.click();
    check("× takes the card down", w.strip().length === 0, w.said());
    check("without opening what it points at", opened === 0 && w.loc.hash === "", [opened, w.loc.hash]);
    check("and the history keeps it", w.api.noticeHistory.length === 1, w.api.noticeHistory);
  }

  /* --- the restart card can be hidden without answering ---------------- */
  {
    const w = build();
    w.gate.restart = restartRec();
    await w.api.refreshRestartGate();
    check("a pending restart draws its card", w.strip().length === 1, w.said());
    check("the bell counts the waiting request", w.badge() === "1", w.badge());
    const hide = w.press(w.nodes.notices, "Hide");
    check("the restart card offers Hide", !!hide, w.said());
    hide.click();
    check("Hide takes the card down", w.strip().length === 0, w.said());
    check("and posts nothing",
          w.apiCalls.every((p) => !/approve|reject|extend/.test(p)), w.apiCalls);
    await w.api.refreshRestartGate();
    check("the next poll does not put it back", w.strip().length === 0, w.said());
    check("the bell still counts it — its deadline is running",
          w.badge() === "1" && w.nodes["notice-bell"].classList.contains("has-hidden"),
          [w.badge(), w.nodes["notice-bell"].className]);

    w.api.noticePanelToggle(true);
    check("the panel lists it as waiting, with the reason and the countdown",
          /Waiting on you \(1\)/.test(w.panel()) && /restart requested/.test(w.panel()) &&
            /serve master 1234abcd/.test(w.panel()) && /auto-approves in 5:00/.test(w.panel()),
          w.panel());
    const body = w.nodes["notice-panel-body"];
    check("with the gate's own buttons",
          ["Approve", "+5 min", "Reject", "Show card"].every((l) => w.press(body, l)),
          w.panel());

    w.clock.at += 61000;
    await w.api.refreshRestartGate();
    check("the panel's countdown moves with the poll", /auto-approves in 3:59/.test(w.panel()),
          w.panel());

    w.press(w.nodes["notice-panel-body"], "Show card").click();
    await new Promise((r) => setImmediate(r));
    check("Show card puts the card back in the corner", w.strip().length === 1, w.said());
  }

  /* --- deciding from the panel ------------------------------------------ */
  {
    const w = build();
    w.gate.restart = restartRec();
    await w.api.refreshRestartGate();
    w.press(w.nodes.notices, "Hide").click();
    w.api.noticePanelToggle(true);
    w.press(w.nodes["notice-panel-body"], "+5 min").click();
    await new Promise((r) => setImmediate(r));
    check("+5 min in the panel posts the gate's extend",
          w.apiCalls.includes("/api/daemon/restart-request/extend"), w.apiCalls);
    check("and nothing settles", w.gate.restart.status === "pending", w.gate.restart);

    w.press(w.nodes["notice-panel-body"], "Reject").click();
    await new Promise((r) => setImmediate(r));
    await w.api.refreshRestartGate();
    check("Reject in the panel posts the gate's reject",
          w.apiCalls.includes("/api/daemon/restart-request/reject"), w.apiCalls);
    check("the waiting list empties", /Waiting on you \(0\)/.test(w.panel()), w.panel());
    check("and the history row says how it ended",
          w.api.noticeHistory.some((e) => e.gate && e.gate.rid === "r1" && e.status === "rejected"),
          w.api.noticeHistory);
    check("the bell stops counting", w.badge() === "", w.badge());
    check("the settled id no longer silences a card",
          !/restart:r1/.test(w.storage.getItem("claunch.noticeHidden") || "[]"),
          w.storage.getItem("claunch.noticeHidden"));
  }

  /* --- a waiting request can be marked read (claunch-qqy2i) ------------- */
  {
    const storage = memoryStorage();
    const w = build({ storage });
    w.gate.restart = restartRec();
    w.gate.goto = [gotoRec()];
    await w.api.refreshRestartGate();
    await w.api.refreshGotoGate();
    w.api.noticePanelToggle(true);
    const body = w.nodes["notice-panel-body"];
    check("two requests wait and the panel counts both",
          /Waiting on you \(2\)/.test(w.panel()) && w.badge() === "2", [w.panel(), w.badge()]);
    check("each waiting row carries Mark read", !!w.press(body, "Mark read"), w.panel());

    w.press(body, "Mark read").click();
    check("Mark read takes the request off the waiting count",
          /Waiting on you \(1\)/.test(w.panel()), w.panel());
    check("and lists it under Read, still open",
          /Read, still open \(1\)/.test(w.panel()) && w.panelRows().length === 2, w.panel());
    check("with its countdown and its buttons still there",
          /auto-approves in 5:00/.test(w.panel()) && !!w.press(body, "Approve"), w.panel());
    check("the request itself is still open on the daemon and nothing was posted",
          w.gate.restart.status === "pending" &&
            w.apiCalls.every((p) => !/restart-request\/(approve|reject|extend)/.test(p)),
          w.apiCalls);
    check("the bell stops counting it", w.badge() === "1", w.badge());
    check("and this browser remembers which request was read",
          /restart:r1/.test(storage.getItem("claunch.noticeRead") || "[]"),
          storage.getItem("claunch.noticeRead"));

    w.press(body, "Mark unread").click();
    check("Mark unread counts it again",
          /Waiting on you \(2\)/.test(w.panel()) && !/Read, still open/.test(w.panel()),
          w.panel());
    check("and the bell counts it again", w.badge() === "2", w.badge());

    w.press(body, "Mark read").click();
    const again = build({ storage });
    again.gate.restart = restartRec();
    again.gate.goto = [gotoRec()];
    await again.api.refreshRestartGate();
    await again.api.refreshGotoGate();
    again.api.noticePanelToggle(true);
    check("a reload keeps the read request read",
          /Waiting on you \(1\)/.test(again.panel()) &&
            /Read, still open \(1\)/.test(again.panel()), again.panel());
    check("and out of the bell", again.badge() === "1", again.badge());

    again.press(again.nodes["notice-panel-body"], "Mark read").click();
    check("with every request read the list says so",
          /Waiting on you \(0\)/.test(again.panel()) &&
            /every waiting request has been read/.test(again.panel()) &&
            /Read, still open \(2\)/.test(again.panel()), again.panel());
    check("and the bell counts none", again.badge() === "", again.badge());

    again.gate.restart = null;                 // answered while nobody looked
    await again.api.refreshRestartGate();
    check("a request that left the pending set is forgotten by the read set",
          !/restart:r1/.test(storage.getItem("claunch.noticeRead") || "[]"),
          storage.getItem("claunch.noticeRead"));
    again.gate.restart = restartRec();         // the daemon gives the id again
    await again.api.refreshRestartGate();
    check("so a later request with the same id waits again",
          /Waiting on you \(1\)/.test(again.panel()), again.panel());
  }

  /* --- a reload keeps the history and the hidden card ------------------- */
  {
    const storage = memoryStorage();
    const first = build({ storage });
    first.gate.restart = restartRec();
    await first.api.refreshRestartGate();
    first.press(first.nodes.notices, "Hide").click();

    const second = build({ storage });
    second.gate.restart = restartRec();
    await second.api.refreshRestartGate();
    check("after a reload the hidden card stays hidden", second.strip().length === 0,
          second.said());
    check("and the request is still counted", second.badge() === "1", second.badge());
    check("one history row per request, not one per page load",
          second.api.noticeHistory.filter((e) => e.gate && e.gate.rid === "r1").length === 1,
          second.api.noticeHistory);

    second.gate.restart = null;               // settled while nobody looked
    await second.api.refreshRestartGate();
    check("a request gone from the daemon is marked as no longer pending",
          second.api.noticeHistory.some((e) => e.gate && e.status === "no longer pending"),
          second.api.noticeHistory);
  }

  /* --- the goto and reposync cards hide the same way -------------------- */
  {
    const w = build();
    w.gate.goto = [gotoRec()];
    w.gate.reposync = [{ id: "p1", session: "s4", status: "pending",
                         deadline: new Date(BASE + 120000).toISOString(),
                         plan: { workspace: "claude-launcher", summary: "push master" } }];
    await w.api.refreshGotoGate();
    await w.api.refreshReposyncGate();
    check("both cards are drawn", w.strip().length === 2, w.said());
    check("the bell counts both", w.badge() === "2", w.badge());
    for (let i = 0; i < 2; i += 1) w.press(w.nodes.notices, "Hide").click();
    check("both hide", w.strip().length === 0, w.said());
    await w.api.refreshGotoGate();
    await w.api.refreshReposyncGate();
    check("and stay hidden on the next poll", w.strip().length === 0, w.said());
    w.api.noticePanelToggle(true);
    check("the panel lists both with their own words",
          /run move requested/.test(w.panel()) && /repository sync wants to push/.test(w.panel()) &&
            /expires \(as a refusal\)/.test(w.panel()),
          w.panel());
    w.press(w.nodes["notice-panel-body"], "Deny").click();
    await new Promise((r) => setImmediate(r));
    check("Deny in the panel posts the goto gate's deny",
          w.apiCalls.includes("/api/cflow/goto-requests/g1/deny"), w.apiCalls);
  }

  /* --- opening the panel hides the request cards too (claunch-paqfs) ---- */
  {
    const w = build();
    w.gate.goto = [gotoRec()];
    await w.api.refreshGotoGate();
    check("a request card is up", w.strip().length === 1, w.said());
    w.api.noticePanelToggle(true);
    await new Promise((r) => setImmediate(r));
    check("opening the panel hides it", w.strip().length === 0, w.said());
    check("the request is still counted", w.badge() === "1", w.badge());
    w.gate.reposync = [{ id: "p1", session: "s4", status: "pending",
                         deadline: new Date(BASE + 120000).toISOString(),
                         plan: { workspace: "claude-launcher", summary: "push master" } }];
    await w.api.refreshReposyncGate();
    w.api.noticePanelToggle(false);
    await new Promise((r) => setImmediate(r));
    check("closing it hides a request that came in while it was open",
          w.strip().length === 0, w.said());
    await w.api.refreshGotoGate();
    await w.api.refreshReposyncGate();
    check("and neither comes back on the next poll", w.strip().length === 0 && w.badge() === "2",
          [w.said(), w.badge()]);
    w.api.noticePanelToggle(true);
    w.press(w.panelRows().find((r) => /run move requested/.test(r.text())), "Show card").click();
    await new Promise((r) => setImmediate(r));
    w.api.noticePanelToggle(false);
    await new Promise((r) => setImmediate(r));
    await w.api.refreshGotoGate();
    check("a card put back with Show card while the panel is open stays up after it closes",
          w.strip().length === 1 && /run move requested/.test(w.said()), w.said());
  }

  /* --- QA requests become notices (claunch-k7c32) ---------------------- */
  {
    const ask = (id, title, extra) => ({ id, title, status: "open", revision: 1,
                                         answer_revision: null, ...(extra || {}) });
    const storage = memoryStorage();
    const w = build({ storage });
    const revealed = [];
    globalThis.QaPane = { reveal: (s, id) => revealed.push([s, id]) };
    w.api.qaNoticeSync({ s1: { unresolved: 1, waiting: [ask("qa-old", "what was asked before")] } });
    check("a fresh browser's first poll announces nothing", w.strip().length === 0, w.said());
    check("and remembers what it saw",
          /qa-old:open/.test(storage.getItem("claunch.qaNoticed") || ""), [...storage.data.keys()]);
    w.api.qaNoticeSync({ s1: { unresolved: 2, waiting: [ask("qa-old", "what was asked before"),
                                                         ask("qa-new", "which base branch?")] } });
    check("a new question puts up one card", w.strip().length === 1 && /s1 asks you/.test(w.said()) &&
            /which base branch\?/.test(w.said()), w.said());
    const row = w.api.noticeHistory[0];
    check("and writes a history row naming the session",
          row && row.title === "s1 asks you" && row.sub === "which base branch?" && row.session === "s1",
          row);
    check("and keeping the question it is about", row && row.target && row.target.qa === "qa-new", row);
    w.api.qaNoticeSync({ s1: { unresolved: 2, waiting: [ask("qa-old", "what was asked before"),
                                                         ask("qa-new", "which base branch? (edited)",
                                                             { revision: 2 })] } });
    check("editing a question nobody answered is not a second request",
          w.api.noticeHistory.length === 1, w.api.noticeHistory);
    w.strip()[0].click();
    check("clicking the card opens the session on that question",
          w.loc.hash === "#/s/s1" && JSON.stringify(revealed) === JSON.stringify([["s1", "qa-new"]]) &&
            w.layouts.s1 && w.layouts.s1.split === true && w.layouts.s1.pane === "qa",
          { hash: w.loc.hash, revealed, layout: w.layouts.s1 });
    check("and puts the card away", w.strip().length === 0, w.said());
    w.api.noticePanelToggle(true);
    check("the panel shows the row with the session", /s1 asks you/.test(w.panel()) &&
            /session/.test(w.panel()), w.panel());
    w.api.noticePanelToggle(false);

    w.loc.hash = "";
    revealed.length = 0;
    w.layouts.s1 = { split: true, pane: "threads" };
    w.api.noticePanelToggle(true);
    const qrow = w.panelRows().find((r) => /which base branch/.test(r.text()));
    check("the history row of a QA request can be opened",
          qrow && qrow.classList.contains("openable"), qrow && qrow.className);
    qrow.click();
    check("clicking it opens the session on that question",
          w.loc.hash === "#/s/s1" && JSON.stringify(revealed) === JSON.stringify([["s1", "qa-new"]]),
          { hash: w.loc.hash, revealed });
    check("over an open threads view the QA pane joins it", w.layouts.s1.pane === "both", w.layouts.s1);
    check("and leaves the panel open (claunch-0fdx7)",
          !w.nodes["notice-panel"].classList.contains("hidden"),
          w.nodes["notice-panel"].className);

    // The rest of this block reads the corner, where no card is drawn while
    // the panel is open: close it the way a person would.
    w.api.noticePanelToggle(false);
    check("the panel's × closes it", w.nodes["notice-panel"].classList.contains("hidden"),
          w.nodes["notice-panel"].className);
    w.api.qaNoticeSync({ s2: { unresolved: 1, waiting: [ask("qa-b", "pick one")] } });
    check("a card per request", w.strip().length === 1, w.said());
    w.api.qaNoticeSync({ s2: { unresolved: 0, waiting: [] } });
    check("an answered request takes its card down", w.strip().length === 0, w.said());
    check("the history keeps it", w.api.noticeHistory.some((e) => e.sub === "pick one"),
          w.api.noticeHistory);
    const stale = (rev, arev) => ({ s2: { unresolved: 1, waiting: [
      ask("qa-b", "pick one, again", { status: "stale", revision: rev, answer_revision: arev })] } });
    w.api.qaNoticeSync(stale(3, 2));
    check("a rewritten answered question asks again",
          w.strip().length === 1 && /s2 asks again/.test(w.said()), w.said());
    const n = w.api.noticeHistory.length;
    w.api.qaNoticeSync(stale(4, 2));
    check("further edits before the answer are the same request", w.api.noticeHistory.length === n,
          w.api.noticeHistory.length);

    const tab = build({ storage });
    tab.api.qaNoticeSync(stale(4, 2));
    check("another tab of the same browser does not announce it again",
          tab.strip().length === 0 && tab.api.noticeHistory.length === n, tab.said());
    delete globalThis.QaPane;
  }

  /* --- thread replies become notices (claunch-gfv7l) -------------------- */
  {
    const reply = (seq, thread, title, excerpt) => ({ seq, thread, title, excerpt, at: "t" });
    const storage = memoryStorage();
    const w = build({ storage });
    const focused = [];
    globalThis.ThreadsPane = { focus: (s, t) => focused.push([s, t]) };
    w.api.threadNoticeSync({ s1: { open: 1, agent_seq: 4, replies: [reply(4, "th-a", "deploy", "old answer")] } });
    check("a fresh browser's first threads poll announces nothing", w.strip().length === 0, w.said());
    check("and remembers the last reply it saw",
          JSON.parse(storage.getItem("claunch.threadNoticed") || "{}").s1 === 4, [...storage.data.keys()]);
    w.api.threadNoticeSync({ s1: { open: 1, agent_seq: 4, replies: [reply(4, "th-a", "deploy", "old answer")] } });
    check("the same reply is not announced again", w.strip().length === 0, w.said());
    w.api.threadNoticeSync({ s1: { open: 2, agent_seq: 9, replies: [
      reply(4, "th-a", "deploy", "old answer"), reply(7, "th-a", "deploy", "use the blue one"),
      reply(9, "th-b", "flaky", "it was the clock")] } });
    check("each thread with a new reply puts up one card", w.strip().length === 2 &&
            /s1 replied in “deploy”/.test(w.said()) && /use the blue one/.test(w.said()) &&
            /s1 replied in “flaky”/.test(w.said()) && /it was the clock/.test(w.said()), w.said());
    const rows = w.api.noticeHistory.filter((e) => /replied in/.test(e.title));
    check("and each reply writes a history row naming the session",
          rows.length === 2 && rows.every((e) => e.session === "s1") &&
            rows.some((e) => e.sub === "it was the clock"), rows);
    w.api.threadNoticeSync({ s1: { open: 2, agent_seq: 10, replies: [
      reply(7, "th-a", "deploy", "use the blue one"), reply(9, "th-b", "flaky", "it was the clock"),
      reply(10, "th-a", "deploy", "and restart it")] } });
    check("a newer reply in the same thread replaces its card",
          w.strip().length === 2 && /and restart it/.test(w.said()) && !/use the blue one/.test(w.said()),
          w.said());
    check("while the history keeps both", w.api.noticeHistory.filter((e) => /deploy/.test(e.title)).length === 2,
          w.api.noticeHistory);
    const card = w.strip().find((c) => /and restart it/.test(c.text()));
    card.click();
    check("clicking the card opens the session on that thread",
          w.loc.hash === "#/s/s1" && JSON.stringify(focused) === JSON.stringify([["s1", "th-a"]]) &&
            w.layouts.s1 && w.layouts.s1.split === true && w.layouts.s1.pane === "threads",
          { hash: w.loc.hash, focused, layout: w.layouts.s1 });
    check("and puts that card away", w.strip().length === 1, w.said());
    w.api.threadNoticeClear("s1");
    check("showing the session's threads takes its remaining cards down", w.strip().length === 0, w.said());
    w.layouts.s2 = { split: true, pane: "qa" };
    w.api.threadNoticeSync({ s2: { open: 1, agent_seq: 12, replies: [reply(12, "th-c", "q", "see qa")] } });
    check("a session not seen before announces its replies once the list is kept",
          w.strip().length === 1 && /s2 replied/.test(w.said()), w.said());
    w.strip()[0].click();
    check("over an open QA pane the threads join it", w.layouts.s2.pane === "both", w.layouts.s2);

    focused.length = 0;
    w.loc.hash = "";
    w.api.noticePanelToggle(true);
    const trow = w.panelRows().find((r) => /it was the clock/.test(r.text()));
    check("the history row of a thread reply can be opened",
          trow && trow.classList.contains("openable"), trow && trow.className);
    trow.click();
    check("clicking it opens the session on that thread",
          w.loc.hash === "#/s/s1" && JSON.stringify(focused) === JSON.stringify([["s1", "th-b"]]),
          { hash: w.loc.hash, focused });
    check("and leaves the panel open (claunch-0fdx7)",
          !w.nodes["notice-panel"].classList.contains("hidden"),
          w.nodes["notice-panel"].className);

    const tab = build({ storage });
    tab.api.threadNoticeSync({ s1: { open: 2, agent_seq: 10, replies: [reply(10, "th-a", "deploy", "and restart it")] },
                               s2: { open: 1, agent_seq: 12, replies: [reply(12, "th-c", "q", "see qa")] } });
    check("another tab of the same browser does not announce them again",
          tab.strip().length === 0, tab.said());
    delete globalThis.ThreadsPane;
  }

  /* --- a row from before targets were kept opens nothing ---------------- */
  {
    const storage = memoryStorage();
    storage.setItem("claunch.noticeHistory", JSON.stringify([
      { id: "h1", at: BASE, title: "s1 replied in “old”", sub: "x", kind: "", session: "s1" }]));
    const w = build({ storage });
    w.api.noticePanelToggle(true);
    const old = w.panelRows()[0];
    check("an old row is shown", old && /replied in/.test(old.text()), w.panel());
    check("without the openable mark", old && !old.classList.contains("openable"), old && old.className);
    old.click();
    check("and a click on it goes nowhere", w.loc.hash === "" &&
            !w.nodes["notice-panel"].classList.contains("hidden"), w.loc.hash);
  }

  /* --- urgent asks (claunch-x76l8.1) ------------------------------------ */
  {
    const w = build();
    const urgent = (u) => ({ id: "ask2-aa", instance: "i7", label: "coding-approval",
                             status: "open", resolution: "human", ...(u || {}) });
    const row = (u, answers, extra) => ({
      cwd: "/w/s5", scope: "s5", workflow: "improv-worker-v2", urgent_asks: u,
      answers: answers === undefined ? [{ kind: "ask", ask: "ask2-aa", instance: "i7",
        urgent: true, label: "coding-approval", prompt: "start coding?",
        require_reason: false,
        options: [{ name: "approve" }, { name: "decline" }] }] : answers,
      ...(extra || {}) });

    // an ask without urgent never reaches urgent_asks: the rail row has the
    // human ask in answers only, and nothing changes on the channel
    w.api.urgentAskSync({ s5: row([], [{ kind: "ask", ask: "ask2-bb", instance: "i8",
      label: "plain", prompt: "ok?", options: [{ name: "approve" }, { name: "decline" }] }]) });
    check("an ask without urgent draws no card", w.strip().length === 0 && w.badge() === "" &&
            w.api.noticeHistory.length === 0, [w.said(), w.api.noticeHistory]);

    // urgent, but still routed to agents: unchanged
    for (const resolution of ["routing", "asked", "self"]) {
      w.api.urgentAskSync({ s5: row([urgent({ resolution })], []) });
      check(`an urgent ask at resolution ${resolution} draws no card`,
            w.strip().length === 0 && w.badge() === "" && w.api.noticeHistory.length === 0,
            [w.said(), w.api.noticeHistory]);
    }

    w.api.urgentAskSync({ s5: row([urgent()]) });
    check("an urgent ask fallen to a person draws its card",
          w.strip().length === 1 && /s5/.test(w.said()) && /coding-approval/.test(w.said()) &&
            /start coding\?/.test(w.said()), w.said());
    check("the bell counts it as waiting", w.badge() === "1" &&
            w.nodes["notice-bell"].classList.contains("waiting"), [w.badge()]);
    const hist = w.api.noticeHistory.filter((e) => e.gate && e.gate.type === "ask");
    check("one history row, read, pending, naming the session",
          hist.length === 1 && hist[0].read === true && hist[0].status === "pending" &&
            hist[0].session === "s5", hist);
    w.api.urgentAskSync({ s5: row([urgent()]) });
    check("the next poll keeps one card and one row",
          w.strip().length === 1 &&
            w.api.noticeHistory.filter((e) => e.gate && e.gate.type === "ask").length === 1,
          w.said());

    w.press(w.nodes.notices, "approve").click();
    await new Promise((r) => setImmediate(r));
    const posted = w.apiBodies.find(([p]) => p === "/api/cflow2/approve");
    check("approve posts the run page's own approve with the instance",
          posted && posted[1].node === "i7" && posted[1].decision === "approve" &&
            posted[1].cwd === "/w/s5" && posted[1].scope === "s5", w.apiBodies);
    check("and the card goes", w.strip().length === 0, w.said());

    w.api.urgentAskSync({ s5: row([urgent({ status: "answered", decision: "approve", by: "web" })], []) });
    check("the answered ask leaves the waiting count", w.badge() === "" &&
            !w.nodes["notice-bell"].classList.contains("waiting"), w.badge());
    check("and its history row says how it ended",
          w.api.noticeHistory.some((e) => e.gate && e.gate.type === "ask" &&
            e.status === "answered: approve by web"), w.api.noticeHistory);
  }
  {
    // an urgent ask whose answer wants text: no presses, only the way there
    const w = build();
    w.api.urgentAskSync({ s6: { cwd: "/w/s6", scope: "s6", urgent_asks: [{ id: "ask2-cc",
      instance: "i1", label: "plan", status: "open", resolution: "human" }],
      answers: [{ kind: "ask", ask: "ask2-cc", instance: "i1", prompt: "which plan?",
                  require_reason: false,
                  options: [{ name: "a" }, { name: "b" }, { name: "reframe" }] }] } });
    check("a brief ask's card has no decision presses",
          w.strip().length === 1 && !w.press(w.nodes.notices, "a") &&
            !w.press(w.nodes.notices, "reframe") && /needs a reason/.test(w.said()), w.said());
    w.api.noticePanelToggle(true);
    check("the panel lists it as waiting with an Open press",
          /Waiting on you \(1\)/.test(w.panel()) && !!w.press(w.nodes["notice-panel-body"], "Open"),
          w.panel());
    w.press(w.nodes["notice-panel-body"], "Open").click();
    check("Open goes to the session and leaves the panel open (claunch-0fdx7)",
          w.loc.hash === "#/s/s6" && !w.nodes["notice-panel"].classList.contains("hidden"),
          [w.loc.hash, w.nodes["notice-panel"].className]);
    w.api.urgentAskSync({ s6: { cwd: "/w/s6", scope: "s6", urgent_asks: [{ id: "ask2-cc",
      instance: "i1", label: "plan", status: "open", resolution: "human" }], answers: [] } });
    check("opening the panel hid it: off the corner and in the count (claunch-paqfs)",
          w.strip().length === 0 &&
            w.badge() === "1", [w.said(), w.badge()]);
    w.api.urgentAskSync({});
    check("a session gone from the rail settles it", w.badge() === "" &&
            w.api.noticeHistory.some((e) => e.gate && e.gate.rid === "ask2-cc" &&
              e.status === "no longer pending"), w.api.noticeHistory);
  }

  /* --- Open puts the card away on the way in (claunch-yjm0v) ------------ */
  {
    const w = build();
    const pending = () => ({ id: "n1", session: "s7", items: ["its end decision"],
      message: "the run waits on your pick", workflow: "improv-worker-v2",
      status: "pending" });
    w.gate.nudges = [pending()];
    await w.api.refreshNudgeGate();
    check("a pending nudge draws its card",
          w.strip().length === 1 && /s7\s+needs you/.test(w.said()), w.said());
    w.press(w.nodes.notices, "Open").click();
    check("Open opens the session AND puts the card away",
          w.loc.hash === "#/s/s7" && w.strip().length === 0 &&
            w.api.gateIsHidden("nudge", "n1"), [w.loc.hash, w.said()]);
    check("...without answering it: the bell still counts the wait",
          w.badge() === "1", w.badge());
    await w.api.refreshNudgeGate();
    check("and the next poll does not draw it again", w.strip().length === 0, w.said());
    w.api.noticePanelToggle(true);
    check("the panel keeps it, so nothing is lost by going in",
          /Waiting on you \(1\)/.test(w.panel()), w.panel());
  }
  {
    // the card's own body is the same way in
    const w = build();
    w.gate.nudges = [{ id: "n2", session: "s8", items: ["a pick"],
      status: "pending" }];
    await w.api.refreshNudgeGate();
    w.strip()[0].click();
    check("a click on the nudge card opens the session and puts the card away",
          w.loc.hash === "#/s/s8" && w.strip().length === 0 &&
            w.api.gateIsHidden("nudge", "n2"), [w.loc.hash, w.said()]);
  }
  {
    // and the urgent ask's card, which has no Open button of its own
    const w = build();
    w.api.urgentAskSync({ s5: { cwd: "/w/s5", scope: "s5", urgent_asks: [{ id: "ask2-dd",
      instance: "i9", label: "coding-approval", status: "open", resolution: "human" }],
      answers: [{ kind: "ask", ask: "ask2-dd", instance: "i9", prompt: "start coding?",
                  require_reason: false,
                  options: [{ name: "approve" }, { name: "decline" }] }] } });
    w.strip()[0].click();
    check("a click on the urgent-ask card opens the session and puts the card away",
          w.loc.hash === "#/s/s5" && w.strip().length === 0 &&
            w.api.gateIsHidden("ask", "ask2-dd"), [w.loc.hash, w.said()]);
    check("...still unanswered: the bell counts it and the panel lists it",
          w.badge() === "1", w.badge());
  }

  /* --- no storage is not an error --------------------------------------- */
  {
    const w = build({ storage: null });
    w.api.notify("copy not taken", "too large", {});
    w.gate.restart = restartRec();
    await w.api.refreshRestartGate();
    w.press(w.nodes.notices, "Hide").click();
    await w.api.refreshRestartGate();
    check("without storage the page still keeps the history in memory",
          w.api.noticeHistory.length === 2 && !/restart requested/.test(w.said()),
          [w.api.noticeHistory.length, w.said()]);
  }

  /* --- what one browser did with a card, the others learn (claunch-mfzlv) */
  {
    const ask = (id, title) => ({ id, title, status: "open", revision: 1, answer_revision: null });
    const reply = (seq, thread, title, excerpt) => ({ seq, thread, title, excerpt, at: "t" });
    const daemon = fakeNoticeDaemon();
    const browsers = () => {
      const made = [build({ daemon }), build({ daemon })];
      for (const w of made) {
        // Each browser has polled before, so the next request is announced.
        w.api.qaNoticeSync({});
        w.api.threadNoticeSync({ s1: { open: 0, agent_seq: 1, replies: [] } });
      }
      return made;
    };
    const tickAll = async (...ws) => { for (const w of ws) await w.api.noticeSyncTick(); };
    const rowOf = (w, sub) => w.api.noticeHistory.find((e) => e.sub === sub);
    globalThis.QaPane = { reveal: () => {} };
    globalThis.ThreadsPane = { focus: () => {} };

    const [a, b] = browsers();
    const one = { s1: { unresolved: 1, waiting: [ask("qa-1", "first question")] } };
    a.api.qaNoticeSync(one);
    b.api.qaNoticeSync(one);
    check("both browsers put up the card", a.strip().length === 1 && b.strip().length === 1,
          [a.said(), b.said()]);
    check("the card's row carries the event's sync id",
          rowOf(a, "first question").sync === "qa:qa-1:open" &&
            rowOf(b, "first question").sync === "qa:qa-1:open", rowOf(a, "first question"));
    a.strip()[0].click();
    await tickAll(a, b);
    check("a click in one browser sends dismissed and read",
          daemon.bodies.some((x) => (x.changes || []).some((c) => c.id === "qa:qa-1:open" &&
            c.dismissed === true && c.read === true)), daemon.bodies);
    check("the other browser's card goes down", b.strip().length === 0, b.said());
    check("and its row reads as read", rowOf(b, "first question").read === true, rowOf(b, "first question"));

    const two = { s1: { unresolved: 1, waiting: [ask("qa-2", "second question")] } };
    a.api.qaNoticeSync(two);
    b.api.qaNoticeSync(two);
    const close = a.strip()[0].children.find((c) => c.className === "notice-close");
    close.click();
    await tickAll(a, b);
    check("× in one browser takes the other's card down", b.strip().length === 0, b.said());
    check("without reading it there", rowOf(b, "second question").read === false &&
            rowOf(a, "second question").read === false, [rowOf(a, "second question"), rowOf(b, "second question")]);

    a.api.threadNoticeSync({ s1: { open: 1, agent_seq: 5, replies: [reply(5, "th-a", "deploy", "done")] } });
    b.api.threadNoticeSync({ s1: { open: 1, agent_seq: 5, replies: [reply(5, "th-a", "deploy", "done")] } });
    check("a thread reply's card is up in both", a.strip().length === 1 && b.strip().length === 1,
          [a.said(), b.said()]);
    a.api.threadNoticeClear("s1");
    await tickAll(a, b);
    check("the threads pane shown in one browser takes the other's card down",
          b.strip().length === 0, b.said());

    // A hidden tab left on a threads pane is not someone reading it: the
    // poll still takes its own card down, but tells no other browser.
    a.api.threadNoticeSync({ s1: { open: 1, agent_seq: 6, replies: [reply(6, "th-a", "deploy", "again")] } });
    b.api.threadNoticeSync({ s1: { open: 1, agent_seq: 6, replies: [reply(6, "th-a", "deploy", "again")] } });
    globalThis.document = { hidden: true };
    a.api.threadNoticeClear("s1");
    delete globalThis.document;
    await tickAll(a, b);
    check("a hidden browser's threads pane takes only its own card down",
          a.strip().length === 0 && b.strip().length === 1, [a.said(), b.said()]);
    if (b.strip()[0]) b.strip()[0].click();
    await tickAll(a, b);

    // A change made while this page's own request is in flight is sent with
    // the base that request's answer brought, so it is not refused against
    // this page's own write.
    const four = { s1: { unresolved: 1, waiting: [ask("qa-4", "fourth question")] } };
    a.api.qaNoticeSync(four);
    a.strip()[0].click();
    const flight = a.api.noticeSyncTick();
    a.api.noticeMarkRead([rowOf(a, "fourth question").id], false);
    await flight;
    await a.api.noticeSyncTick();
    check("Mark unread made during the page's own request is kept",
          daemon.records.get("qa:qa-4:open").read === false && rowOf(a, "fourth question").read === false,
          [daemon.records.get("qa:qa-4:open"), rowOf(a, "fourth question")]);

    const three = { s1: { unresolved: 1, waiting: [ask("qa-3", "third question")] } };
    a.api.qaNoticeSync(three);
    a.strip()[0].click();
    await tickAll(a, b);
    b.api.qaNoticeSync(three);
    check("a notice that reaches a browser after another dealt with it puts up no card",
          b.strip().length === 0, b.said());
    check("and is written to the history as read", rowOf(b, "third question") &&
            rowOf(b, "third question").read === true, rowOf(b, "third question"));

    // Mark unread in one browser reaches the other's row.
    a.api.noticeMarkRead([rowOf(a, "first question").id], false);
    await tickAll(a, b);
    check("Mark unread in one browser reaches the other",
          rowOf(b, "first question").read === false, rowOf(b, "first question"));

    // Conflict: c has not heard of the unread above and marks the row read
    // against an older base; the daemon's value stands and c takes it.
    const c = build({ daemon });
    c.api.qaNoticeSync({});
    c.api.noticeHistory.unshift({ id: "hc", at: BASE, title: "s1 asks you", sub: "first question",
                                  sync: "qa:qa-1:open", read: false });
    c.api.noticeSync.remote.set("qa:qa-1:open", { id: "qa:qa-1:open", read: true, read_seq: 1, seq: 1 });
    c.api.noticeSync.epoch = daemon.epoch;
    c.api.noticeSync.seq = daemon.seq;
    c.api.noticeMarkRead(["hc"], true);
    await c.api.noticeSyncTick();
    check("a read change against an older base is refused",
          daemon.records.get("qa:qa-1:open").read === false, daemon.records.get("qa:qa-1:open"));
    check("and the browser that sent it takes the daemon's value",
          c.api.noticeHistory.find((e) => e.id === "hc").read === false,
          c.api.noticeHistory.find((e) => e.id === "hc"));
    check("the refused change is not sent again", c.api.noticeSync.out.size === 0,
          [...c.api.noticeSync.out]);

    // Offline: what was queued goes out with the next request.
    daemon.down = true;
    a.api.noticeMarkRead([rowOf(a, "second question").id], true);
    await a.api.noticeSyncTick();
    check("a failed request keeps the change", a.api.noticeSync.out.has("qa:qa-2:open"),
          [...a.api.noticeSync.out]);
    daemon.down = false;
    await tickAll(a, b);
    check("and sends it once the daemon answers", a.api.noticeSync.out.size === 0 &&
            rowOf(b, "second question").read === true, rowOf(b, "second question"));

    // A new store (another epoch) is read from the start.
    daemon.epoch = "e2";
    const before = b.api.noticeSync.seq;
    await b.api.noticeSyncTick();
    check("a new epoch reads from seq 0", daemon.bodies[daemon.bodies.length - 1].since === before &&
            b.api.noticeSync.epoch === "e2", b.api.noticeSync.epoch);

    // A daemon without the route: the page stays local and stops asking.
    const old = fakeNoticeDaemon();
    old.missing = true;
    const lone = build({ daemon: old });
    await lone.api.noticeSyncTick();
    check("a daemon without the route turns sync off", lone.api.noticeSync.off === true,
          lone.api.noticeSync.off);
    const n = lone.apiCalls.length;
    await lone.api.noticeSyncTick();
    check("and it is not asked again", lone.apiCalls.length === n, lone.apiCalls);
    delete globalThis.QaPane;
    delete globalThis.ThreadsPane;
  }

  /* --- this browser's other tabs learn at once (claunch-j6lg0) ---------- */
  {
    const hub = tabHub();
    const tabs = () => [build({ hub }), build({ hub })];
    const rowOf = (w, title) => w.api.noticeHistory.find((e) => e.title === title);

    // A card with no sync id, up in both tabs: × in one puts both away.
    let [a, b] = tabs();
    check("every tab joins the channel", hub.members.length === 2 &&
            a.api.noticeTabs.chan && b.api.noticeTabs.chan, hub.members.length);
    for (const w of [a, b]) w.api.notify("daemon restarted", "pid 41 → 42", { key: "daemon-boot", sticky: true });
    a.press(a.nodes.notices, "×").click();
    await hub.flush();
    check("× in one tab takes the card down in the other", b.strip().length === 0, b.said());
    check("× is not a read, there either", rowOf(b, "daemon restarted").read === false,
          rowOf(b, "daemon restarted"));
    check("the other tab does not echo it back", hub.sent.length === 1, hub.sent);

    // A click is a read: the card goes and the row reads in the other tab.
    for (const w of [a, b]) w.api.notify("copy failed", "select it by hand", { sticky: true });
    a.strip()[0].click();
    await hub.flush();
    check("a click in one tab takes the card down in the other", b.strip().length === 0, b.said());
    check("and reads its row there", rowOf(b, "copy failed").read === true, rowOf(b, "copy failed"));

    // A row with a sync id is found by it, whatever its words.
    [a, b] = tabs();
    a.api.notify("s1 asks you", "which base?", { key: "qa-q1", sticky: true, sync: "qa:q1:open" });
    b.api.notify("s1 asks you", "which base? (edited)", { key: "qa-q1", sticky: true, sync: "qa:q1:open" });
    a.api.noticeMarkRead([rowOf(a, "s1 asks you").id], true);
    await hub.flush();
    check("Mark read in one tab reads the row in the other by its sync id",
          rowOf(b, "s1 asks you").read === true, rowOf(b, "s1 asks you"));
    check("and takes its card down", b.strip().length === 0, b.said());
    check("the bell there counts it no more", b.badge() === "", b.badge());
    a.api.noticeMarkRead([rowOf(a, "s1 asks you").id], false);
    await hub.flush();
    check("Mark unread goes across too", rowOf(b, "s1 asks you").read === false, rowOf(b, "s1 asks you"));
    check("and the other tab stores what it was told",
          /"read":false/.test(b.storage.getItem("claunch.noticeHistory") || ""));

    // Words alone match only a row written about the same time.
    [a, b] = tabs();
    a.api.notify("daemon back", "the same daemon answered again", {});
    b.clock.at = BASE + 120000;
    b.api.notify("daemon back", "the same daemon answered again", {});
    a.api.noticeMarkRead([rowOf(a, "daemon back").id], true);
    await hub.flush();
    check("a row with the same words from minutes later is another notice",
          rowOf(b, "daemon back").read === false, rowOf(b, "daemon back"));

    // Mark all read in the panel.
    [a, b] = tabs();
    for (const w of [a, b]) {
      w.api.notify("one", "1", { sync: "thread:t1:3" });
      w.api.notify("two", "2", {});
    }
    a.api.noticePanelToggle(true);
    a.press(a.nodes["notice-panel-body"], "Mark all read").click();
    await hub.flush();
    check("Mark all read in one tab reads every row in the other",
          b.api.noticeHistory.every((e) => e.read === true), b.api.noticeHistory);
    check("opening the panel put the other tab's cards away too", b.strip().length === 0, b.said());

    // A gate card hidden in one tab, then shown again.
    [a, b] = tabs();
    for (const w of [a, b]) { w.gate.goto = [gotoRec()]; await w.api.refreshGotoGate(); }
    check("the request is up in both tabs", a.strip().length === 1 && b.strip().length === 1,
          [a.said(), b.said()]);
    a.press(a.nodes.notices, "Hide").click();
    await hub.flush();
    check("Hide in one tab hides the card in the other", b.strip().length === 0, b.said());
    check("as hidden, not answered: still counted there",
          b.api.gateIsHidden("goto", "g1") && b.badge() === "1", b.badge());
    await b.api.refreshGotoGate();
    check("and the other tab's next poll keeps it hidden", b.strip().length === 0, b.said());
    a.api.noticePanelToggle(true);
    a.press(a.panelRows().find((r) => /run move requested/.test(r.text())), "Show card").click();
    await hub.flush();
    check("Show card in one tab puts the card back in the other",
          !b.api.gateIsHidden("goto", "g1") && b.strip().length === 1, b.said());

    // A waiting request marked read in one tab is read in the other
    // (claunch-qqy2i).
    [a, b] = tabs();
    for (const w of [a, b]) { w.gate.goto = [gotoRec()]; await w.api.refreshGotoGate(); }
    check("the request waits in both tabs", a.badge() === "1" && b.badge() === "1",
          [a.badge(), b.badge()]);
    a.api.noticePanelToggle(true);
    a.press(a.nodes["notice-panel-body"], "Mark read").click();
    await hub.flush();
    check("Mark read in one tab takes the request off the other tab's count",
          b.badge() === "", b.badge());
    b.api.noticePanelToggle(true);
    check("and lists it as read there",
          /Waiting on you \(0\)/.test(b.panel()) && /Read, still open \(1\)/.test(b.panel()),
          b.panel());
    check("the other tab does not echo it back",
          hub.sent.filter((m) => m.t === "gate-read").length === 1,
          hub.sent.filter((m) => m.t === "gate-read"));

    // A gate answered in one tab: the other polls the gates now.
    [a, b] = tabs();
    for (const w of [a, b]) { w.gate.goto = [gotoRec()]; await w.api.refreshGotoGate(); }
    const before = b.apiCalls.filter((p) => p === "/api/cflow/goto-requests").length;
    b.gate.goto = [gotoRec({ status: "approved" })];
    a.press(a.nodes.notices, "Approve").click();
    await new Promise((r) => setImmediate(r));
    await hub.flush();
    check("an answer in one tab makes the other poll the gates",
          b.apiCalls.filter((p) => p === "/api/cflow/goto-requests").length === before + 1,
          b.apiCalls);
    check("and its card goes", b.strip().length === 0, b.said());

    // Applying what another tab said never goes back out.
    const n = hub.sent.length;
    b.api.noticeTabs.chan.onmessage({ data: { t: "gate-hide", type: "goto", rid: "g9" } });
    check("a tab applying a message posts nothing", hub.sent.length === n, hub.sent.slice(n));
  }

  /* --- the markup and the stylesheet the code reaches for --------------- */
  check("the rail carries the bell", /id="notice-bell"/.test(html));
  check("so does the phone bar", /id="m-notice-bell"/.test(html));
  check("the panel and its body exist",
        /id="notice-panel"[^>]*class="hidden"/.test(html) && /id="notice-panel-body"/.test(html));
  // A column of the layout on a desktop, laid over the page on a phone
  // (claunch-w8q00): open, it narrows the session area instead of covering
  // the session's buttons.
  const layoutAt = html.indexOf('<div id="layout">');
  const panelAt = html.indexOf('<aside id="notice-panel"');
  check("the panel sits inside the layout", layoutAt >= 0 && panelAt > layoutAt, [layoutAt, panelAt]);
  const desk = css.match(/^#notice-panel\s*\{([^}]*)\}/m);
  check("on a desktop it is a column of the layout, not laid over it",
        !!desk && /flex:\s*none/.test(desk[1]) && !/position:\s*fixed/.test(desk[1]),
        desk && desk[1]);
  check("on a phone it is laid over the page",
        /@media \(max-width: 820px\)\s*\{\s*#notice-panel\s*\{[^}]*position:\s*fixed/.test(css));
  check("the panel carries a resize bar", /<aside id="notice-panel"[\s\S]*?id="notice-split"/.test(html));
  check("the bar sets the panel's width",
        /width:\s*var\(--notice-w/.test(css) && /claunch_noticew:/.test(src)
          && /\$\("notice-split"\)\.addEventListener\("pointermove"/.test(src));
  check("a phone hides the bar", /#notice-split\s*\{\s*display:\s*none/.test(css));
  check("toggling it refits the terminal",
        /function noticePanelToggle[\s\S]*?refitSoon\(0\)[\s\S]*?\n\}/.test(src));
  check("the panel head has its close button", /id="notice-panel-close"/.test(html));
  check("the open panel hides the strip", /#notices\.panel-open\s*\{[^}]*display:\s*none/.test(css));
  check("the card's × is styled", /\.notice-close\s*\{/.test(css));
  check("the bell opens the panel", /noticePanelToggle\(\)/.test(src));

  /* --- one list of notice kinds (claunch-efpoi, claunch-efpoi.1) --------
     The spawn gate was added to four of the five places that spelled the
     gate kinds out, and its waiting requests counted on the bell and showed
     in no list. Every gate kind of NOTICE_SOURCES now has a row in the
     panel, whatever kind is added next. */
  function gateRec(type, id, extra) {
    return {
      id, status: "pending", session: "s1", target: "w1", target_session: "w1",
      from: "work", step: "rebase", reason: "because", label: "approve plan",
      message: "waits", kind: "new", hold: "", plan: { workspace: "ws", summary: "push" },
      requested_at: new Date(BASE).toISOString(),
      deadline: new Date(BASE + 300000).toISOString(), ...(extra || {}),
    };
  }
  {
    const w = build();
    const gates = w.api.NOTICE_SOURCES.filter((s) => s.gate).map((s) => s.id);
    check("spawn is a gate kind", gates.includes("spawn"), gates);
    check("the pending store has every gate kind and no other",
          JSON.stringify(Object.keys(w.api.gatePending).sort()) === JSON.stringify([...gates].sort()),
          Object.keys(w.api.gatePending));
    // Opened first: opening re-polls the gates, which would empty the store.
    w.api.noticePanelToggle(true);
    for (const type of gates) {
      w.api.gatePending[type] = [gateRec(type, `${type}-1`)];
      w.api.gateHistoryOpen(type, `${type}-1`, `${type} asked`, "", {});
    }
    w.api.noticeBellPaint();
    w.api.noticePanelPaint();
    const rows = w.panelRows().filter((r) => r.classList.contains("pending"));
    check("every gate kind's waiting request has a panel row", rows.length === gates.length,
          [rows.length, gates.length]);
    check("the bell counts what the panel lists", w.badge() === String(gates.length), w.badge());
    check("a waiting spawn request is in the panel",
          /spawn|session/i.test(rows.map((r) => r.text()).join(" ")));
  }

  /* --- newest on top: the strip, the waiting list, the history -------- */
  {
    const w = build();
    w.api.notify("first", "", { source: "page", key: "a" });
    w.clock.at += 1000;
    w.api.notify("second", "", { source: "page", key: "b" });
    check("a new card goes on top of the strip", /second/.test(w.strip()[0].text()),
          w.strip().map((c) => c.text()));
    check("the history has the newest row first", w.api.noticeHistory[0].title === "second");
    w.api.noticePanelToggle(true);
    w.api.gatePending.kill = [gateRec("kill", "k-old")];
    w.api.gateHistoryOpen("kill", "k-old", "kill old", "", {});
    w.clock.at += 1000;
    w.api.gatePending.spawn = [gateRec("spawn", "s-new")];
    w.api.gateHistoryOpen("spawn", "s-new", "spawn new", "", {});
    w.api.noticePanelPaint();
    const rows = w.panelRows().filter((r) => r.classList.contains("pending"));
    check("the waiting list has the newest request first, whatever its kind",
          rows.length === 2 && /kill/i.test(rows[1].text()) && !/kill/i.test(rows[0].text()),
          rows.map((r) => r.text()));
  }

  /* --- every card names its kind and goes through one door ------------ */
  {
    const calls = [];
    for (let i = src.indexOf("notify("); i >= 0; i = src.indexOf("notify(", i + 1)) {
      if (/function\s+$/.test(src.slice(Math.max(0, i - 9), i)) || /[\w.]$/.test(src[i - 1] || "")) continue;
      let depth = 0;
      let j = i + "notify".length;
      for (; j < src.length; j += 1) {
        if (src[j] === "(") depth += 1;
        else if (src[j] === ")" && --depth === 0) break;
      }
      const call = src.slice(i, j + 1);
      if (/^notify\(\)/.test(call)) continue;   // prose: "notify()"
      calls.push(call);
    }
    const bare = calls.filter((c) => !/source:\s*"\w+"/.test(c));
    check("every notify() call names its kind", calls.length > 10 && !bare.length,
          bare.map((c) => c.slice(0, 80)));
    const kinds = new Set(build().api.NOTICE_SOURCES.map((s) => s.id));
    const unknown = calls.map((c) => (c.match(/source:\s*"(\w+)"/) || [])[1]).filter((k) => !kinds.has(k));
    check("and the kind is one NOTICE_SOURCES lists", !unknown.length, unknown);
    const section = slice("/* notices — the page's own voice",
                          "pollTimer = setInterval(pollTick, DASHBOARD_POLL_MS);");
    const direct = section.split("\n").filter((l) => /host\.appendChild\(node\)/.test(l));
    check("a card reaches the strip only through noticeCardAdd", direct.length === 1,
          direct);
    check("chat's toast is recorded through the page's notices",
          /claunchNotices/.test(fs.readFileSync(path.join(STATIC, "chat.js"), "utf8")) &&
            /window\.claunchNotices\s*=/.test(src));
  }

  /* --- hiding a kind (claunch-efpoi.1) --------------------------------- */
  {
    const storage = memoryStorage();
    const w = build({ storage });
    w.api.noticeSourceSet("thread", false);
    check("the choice is kept in this browser",
          storage.getItem("claunch.noticeSourcesHidden") === JSON.stringify(["thread"]),
          storage.getItem("claunch.noticeSourcesHidden"));
    w.api.notify("s1 replied", "hello", { source: "thread", key: "t1", session: "s1" });
    check("a hidden kind puts up no card", w.strip().length === 0, w.said());
    check("and is still recorded", w.api.noticeHistory.some((e) => e.title === "s1 replied"));
    check("and is not on the bell", w.badge() === "", w.badge());
    w.api.notify("copy failed", "", { source: "page", key: "c1" });
    w.api.noticePanelToggle(true);
    check("the panel leaves the hidden kind out", !/s1 replied/.test(w.panel()) && /copy failed/.test(w.panel()),
          w.panel());
    check("and says how much it leaves out", /1 kind\(s\) hidden · 1 row\(s\) not shown/.test(w.panel()),
          w.panel());
    w.api.gatePending.spawn = [gateRec("spawn", "s-h")];
    w.api.gateHistoryOpen("spawn", "s-h", "spawn hidden", "", {});
    w.api.noticeBellPaint();
    const before = w.badge();
    w.api.noticeSourceSet("spawn", false);
    check("a hidden gate kind is off the bell", before === "2" && w.badge() === "1",
          [before, w.badge()]);
    check("the panel names the requests still waiting in it", /1 request\(s\) of them still waiting/.test(w.panel()),
          w.panel());
    w.api.noticeSettingsToggle(true);
    const settings = w.nodes["notice-settings-body"].text();
    check("the settings list every kind", w.api.NOTICE_SOURCES.every((s) => settings.includes(s.label)),
          settings);
    check("a hidden gate kind says what happens when nobody answers",
          /a child request is created as asked, a top-level request is created as the asker's child/.test(settings), settings);

    const again = build({ storage });
    again.api.notify("s2 replied", "", { source: "thread", key: "t2" });
    check("the choice survives a reload", again.strip().length === 0, again.said());
    again.api.noticeSourceSet("thread", true);
    again.api.noticePanelToggle(true);
    check("showing the kind again brings back what came meanwhile", /s2 replied/.test(again.panel()),
          again.panel());
    check("the panel has its settings press and overlay",
          /id="notice-panel-settings"/.test(html) && /id="notice-settings"/.test(html));
  }
  /* --- what hides a kind, one guard at a time (claunch-efpoi.1) -------
     Each check below is the one that fails when its guard is taken out
     of app.js; the review that asked for them removed each guard and saw
     the checks above still pass. */
  {
    const w = build();
    w.api.noticeSourceSet("goto", false);
    check("a hidden gate kind's request is hidden though nobody put it away",
          w.api.gateIsHidden("goto", "g1") === true && w.api.gateIsHidden("kill", "k1") === false);
    w.gate.goto = [gotoRec()];
    await w.api.refreshGotoGate();
    check("a hidden gate kind's refresh draws no card", w.strip().length === 0, w.said());
    w.api.noticeBellPaint();
    const title = w.nodes["notice-bell"].title;
    check("the bell names the requests waiting in a hidden kind",
          /1 waiting in kinds you hid/.test(title), title);
    check("and counts none of them as read, still open", !/read, still open/.test(title), title);
    check("nor on its number", w.badge() === "", w.badge());
  }
  {
    const w = build();
    const of = (entry) => w.api.noticeSourceOf(entry);
    check("an old row is placed by what it carries",
          of({ gate: { type: "spawn", rid: "s1" } }) === "spawn" &&
            of({ target: { thread: "th-1" } }) === "thread" &&
            of({ target: { qa: "q-1" } }) === "qa" &&
            of({ target: {} }) === "page" && of({}) === "page" &&
            of({ gate: { type: "bogus" } }) === "page",
          [of({ gate: { type: "spawn" } }), of({ target: { thread: "th-1" } }),
           of({ target: { qa: "q-1" } }), of({ gate: { type: "bogus" } })]);
    check("a named kind wins, an unknown one falls back",
          of({ source: "chat", target: { thread: "th-1" } }) === "chat" &&
            of({ source: "bogus", target: { qa: "q-1" } }) === "qa");
  }
  {
    const w = build();
    w.api.notify("odd", "", { source: "bogus", key: "o1" });
    check("a kind NOTICE_SOURCES does not list is recorded as the page's",
          w.api.noticeHistory[0].source === "page", w.api.noticeHistory[0]);
    let threw = null;
    let got;
    try { got = w.api.noticeGateRefresh("qa"); } catch (e) { threw = String(e); }
    check("a notice kind has no gate refresh to run", threw === null && got === null, threw);
    check("nor does an unknown one", w.api.noticeGateRefresh("bogus") === null);
  }
  {
    const storage = memoryStorage();
    const w = build({ storage });
    const onStorage = w.listeners.window.storage;
    check("the page listens for another tab's choice", typeof onStorage === "function");
    w.api.noticeSourceSet("spawn", false);
    onStorage({ key: "claunch.noticeHistory", newValue: "[]" });
    check("another key's change leaves the hidden kinds alone",
          w.api.gateIsHidden("spawn", "x") === true);
    onStorage({ key: "claunch.noticeSourcesHidden", newValue: JSON.stringify(["bogus", "thread"]) });
    check("another tab's choice is taken", w.api.gateIsHidden("spawn", "x") === false);
    w.api.notify("s1 replied", "", { source: "thread", key: "t1" });
    w.api.noticePanelToggle(true);
    w.api.noticePanelPaint();
    check("and a kind it names that this page lacks is dropped",
          /1 kind\(s\) hidden/.test(w.panel()), w.panel());
  }
  {
    const w = build();
    const onKey = w.listeners.document.keydown;
    check("the page listens for Escape", typeof onKey === "function");
    w.api.noticePanelToggle(true);
    w.api.noticeSettingsToggle(true);
    const open = (id) => !w.nodes[id].classList.contains("hidden");
    onKey({ key: "Escape" });
    check("Escape closes the settings first", !open("notice-settings") && open("notice-panel"),
          [open("notice-settings"), open("notice-panel")]);
    onKey({ key: "Escape" });
    check("and the panel next", !open("notice-panel"));
  }
  /* --- mesh join requests from other machines (claunch-efpoi.2) ------ */
  {
    const w = build();
    const joinRec = (extra) => ({
      mesh: "m", id: "req-1", machine: "pcB", session: "sb", handle: "bob",
      role: "worker", requested_at: new Date(BASE).toISOString(), attach: false,
      ...(extra || {}),
    });
    // Two meshes may each hold a request of the same id.
    w.gate.meshjoin = [joinRec(), joinRec({ mesh: "n", machine: "pcC", session: "", attach: true })];
    await w.api.refreshMeshJoinGate();
    check("each mesh's join request draws its own card", w.strip().length === 2, w.said());
    check("the card names who asks and which mesh",
          /bob asks to join mesh m/.test(w.said()) && /pcB\/sb as worker/.test(w.said()), w.said());
    check("an attach request says it is a daemon",
          /daemon pcC asks to attach to mesh n/.test(w.said()), w.said());
    check("the bell counts both", w.badge() === "2", w.badge());
    check("the history keys them by mesh and id",
          ["m/req-1", "n/req-1"].every((rid) =>
            w.api.noticeHistory.some((e) => e.gate && e.gate.type === "meshjoin" && e.gate.rid === rid)),
          w.api.noticeHistory.map((e) => e.gate));
    const card = w.strip().find((c) => /mesh m/.test(c.text()));
    w.press(card, "Approve").click();
    await new Promise((r) => setImmediate(r));
    check("Approve calls the mesh's own request route",
          w.apiCalls.includes("/api/mesh/m/requests/req-1/approve"), w.apiCalls);
    check("and the answered card goes", w.strip().length === 1, w.said());

    w.gate.meshjoin = [joinRec({ mesh: "n" })];
    await w.api.refreshMeshJoinGate();
    w.api.noticePanelToggle(true);
    await new Promise((r) => setImmediate(r));
    const row = w.panelRows().find((r) => r.classList.contains("pending") && /mesh n/.test(r.text()));
    check("the panel lists the waiting request", !!row, w.panel());
    w.press(row, "Deny").click();
    await new Promise((r) => setImmediate(r));
    check("Deny in the panel calls the deny route",
          w.apiCalls.includes("/api/mesh/n/requests/req-1/deny"), w.apiCalls);
    w.gate.meshjoin = [];
    await w.api.refreshMeshJoinGate();
    check("a request gone from the poll settles its history row",
          w.api.noticeHistory.filter((e) => e.gate && e.gate.type === "meshjoin")
            .every((e) => e.status !== "pending"),
          w.api.noticeHistory.filter((e) => e.gate).map((e) => [e.gate.rid, e.status]));
  }
  /* --- the dashboard poll refreshes every gate kind (claunch-efpoi.2) --
     Without it a kind's card and bell count come only once the panel is
     opened; the mesh join gate shipped that way at first. */
  {
    const w = build();
    const called = [];
    for (const s of w.api.NOTICE_SOURCES) {
      if (s.gate) s.refresh = () => { called.push(s.id); return null; };
    }
    w.api.noticeGatePoll();
    const want = w.api.NOTICE_SOURCES.filter((s) => s.gate && s.id !== "ask").map((s) => s.id);
    check("the poll refreshes every gate kind but the one another feed keeps",
          JSON.stringify(called) === JSON.stringify(want), [called, want]);
    check("only the urgent asks are left to their own feed",
          w.api.NOTICE_SOURCES.filter((s) => s.polled === false).map((s) => s.id).join() === "ask");
    const tick = slice("async function pollTick(", "pollTimer = setInterval(pollTick");
    check("pollTick takes its gates from that list", /\.\.\.noticeGatePoll\(\)/.test(tick) &&
          !/refresh(Restart|Goto|Reposync|Resume|Kill|Spawn|Nudge|MeshJoin)Gate\(\)/.test(tick));
  }
  {
    const w = build();
    w.api.noticeSourceSet("meshjoin", false);
    w.gate.meshjoin = [{ mesh: "m", id: "req-1", machine: "pcB", session: "sb",
                         handle: "bob", requested_at: new Date(BASE).toISOString() }];
    await w.api.refreshMeshJoinGate();
    check("a hidden mesh join kind draws no card", w.strip().length === 0, w.said());
  }
})().then(() => {
  if (failures) { console.log(`${failures} check(s) failed`); process.exitCode = 1; }
  else console.log("noticehistory_check: ok");
});
