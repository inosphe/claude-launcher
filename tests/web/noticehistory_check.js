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
   that thread or that question, as the card did, and closes the panel. A
   row written before rows kept a target opens nothing. Every card carries ×,
   which puts it away without following it; while the panel is open the
   corner strip moves below the panel's head so it does not cover the
   panel's own close button.

   Marks and the All / Marked filter (claunch-knuet): ☆ on a history row marks
   it ★ without reading it or opening its target; Marked shows only those
   rows and All every row again; the filter and the marks survive a reload;
   Clear and the 100-row cap keep marked rows; Clear is only in the All view
   and the cap gives way rather than drop a marked row (claunch-knuet.1). */
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
  const gate = { restart: null, goto: [], reposync: [] };
  const apiStub = async (p) => {
    apiCalls.push(p);
    const answer = (body) => ({ json: async () => JSON.parse(JSON.stringify(body)) });
    if (p === "/api/daemon/restart-request") return answer({ request: gate.restart });
    if (p === "/api/cflow/goto-requests") return answer({ requests: gate.goto });
    if (p === "/api/reposync/requests") return answer({ requests: gate.reposync });
    if (p === "/api/daemon/restart-request/reject") {
      gate.restart = { ...gate.restart, status: "rejected" };
      return answer({ ok: true });
    }
    if (p === "/api/daemon/restart-request/extend") {
      gate.restart = { ...gate.restart, extensions: (gate.restart.extensions || 0) + 1 };
      return answer({ ok: true });
    }
    if (/\/(approve|deny|reject)$/.test(p)) return answer({ ok: true });
    throw new Error(`unexpected api call ${p}`);
  };

  const storage = o.storage === undefined ? memoryStorage() : o.storage;
  const loc = { hash: "" };
  const layouts = {};   // session -> split layout, as setSessLayout left it
  const code = slice("/* notices — the page's own voice",
                     "pollTimer = setInterval(pollTick, DASHBOARD_POLL_MS);");
  const noop = () => {};
  const api = new Function(
    "$", "el", "setTimeout", "clearTimeout", "Date", "api", "location",
    "localStorage", "daemonHealth",
    "renderRelayBadge", "refreshProfiles", "refreshHarnesses", "refreshRoles",
    "refreshWorkspaces", "refreshSessions", "refreshMeshList", "refreshCflow",
    "refreshTermQueued", "reconnectNow", "route", "refreshNewWorktree",
    "refreshProjects", "ensureControlSocket", "refreshCflow2Rail",
    "refreshTermWindow", "refreshTermTimers",
    "sessLayoutFor", "setSessLayout", "talkShows",
    code +
    "\nreturn {notify, refreshRestartGate, refreshGotoGate, refreshReposyncGate," +
    " noticePanelToggle, noticeHistory, qaNoticeSync, threadNoticeSync, threadNoticeClear," +
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
  );

  const all = (n, out) => { out.push(n); n.children.forEach((c) => all(c, out)); return out; };
  return {
    api, nodes, clock, gate, apiCalls, storage, loc, layouts,
    strip: () => nodes.notices.children,
    said: () => nodes.notices.children.map((c) => c.text()).join(" | "),
    panel: () => nodes["notice-panel-body"].text(),
    badge: () => bell.children[0].textContent,
    panelRows: () => nodes["notice-panel-body"].children.filter((c) => c.classList.contains("notice-row")),
    press: (root, label) => all(root, []).find((n) => n.textContent === label),
  };
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
    check("while it is open the strip moves below the panel's head",
          w.nodes.notices.classList.contains("under-panel-head"), w.nodes.notices.className);
    w.api.noticePanelToggle(false);
    check("and back once it closes", !w.nodes.notices.classList.contains("under-panel-head"),
          w.nodes.notices.className);
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

  /* --- marks and the All / Marked filter (claunch-knuet) --------------- */
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
            !w.press(head(), "Marked").classList.contains("active"), head().text());
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
    w.press(head(), "Marked").click();
    check("Marked shows only the marked rows",
          w.panelRows().length === 1 && rowOf("two") && /History \(1\)/.test(w.panel()) &&
            w.press(head(), "Marked").classList.contains("active"), w.panel());
    check("and the choice is stored", JSON.parse(w.storage.getItem("claunch.noticeFilter")) === "marked");
    check("Marked offers no Clear, which would forget rows it hides (claunch-knuet.1)",
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
    check("★ unmarks it, and Marked says it is empty",
          entry("two").marked === false && w.panelRows().length === 0 &&
            /no row is marked/.test(w.panel()), w.panel());
    w.press(head(), "All").click();
    check("All shows every row again", w.panelRows().length === 3, w.panel());
    w.press(rowOf("three"), "☆").click();
    w.press(head(), "Clear").click();
    check("Clear keeps the marked row",
          w.api.noticeHistory.length === 1 && entry("three").marked === true, w.api.noticeHistory);
    check("and offers nothing more to clear", !w.press(head(), "Clear"), w.panel());
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
    w.api.noticePanelToggle(true);
    check("the panel shows the row with the session", /s1 asks you/.test(w.panel()) &&
            /session/.test(w.panel()), w.panel());
    w.strip()[0].click();
    check("clicking the card opens the session on that question",
          w.loc.hash === "#/s/s1" && JSON.stringify(revealed) === JSON.stringify([["s1", "qa-new"]]) &&
            w.layouts.s1 && w.layouts.s1.split === true && w.layouts.s1.pane === "qa",
          { hash: w.loc.hash, revealed, layout: w.layouts.s1 });
    check("and puts the card away", w.strip().length === 0, w.said());

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
    check("and closes the panel", w.nodes["notice-panel"].classList.contains("hidden"),
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
    check("and closes the panel", w.nodes["notice-panel"].classList.contains("hidden"),
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

  /* --- the markup and the stylesheet the code reaches for --------------- */
  check("the rail carries the bell", /id="notice-bell"/.test(html));
  check("so does the phone bar", /id="m-notice-bell"/.test(html));
  check("the panel and its body exist",
        /id="notice-panel"[^>]*class="hidden"/.test(html) && /id="notice-panel-body"/.test(html));
  check("the panel is styled", /#notice-panel\s*\{[^}]*position:\s*fixed/.test(css));
  check("the panel head has its close button", /id="notice-panel-close"/.test(html));
  check("the moved strip clears the panel's head", /#notices\.under-panel-head\s*\{[^}]*top:/.test(css));
  check("the card's × is styled", /\.notice-close\s*\{/.test(css));
  check("the bell opens the panel", /noticePanelToggle\(\)/.test(src));
})().then(() => {
  if (failures) { console.log(`${failures} check(s) failed`); process.exitCode = 1; }
  else console.log("noticehistory_check: ok");
});
