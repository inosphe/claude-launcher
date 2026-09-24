/* The Beads page's Queues tab: every session's queue as a swimlane.

   A session's queue is the board's own reading -- the active issues assigned
   to it, in the order its worker takes them -- and the page stores nothing
   beside the session to draw it. Rows are sessions plus an unassigned pool,
   columns are statuses, and a card dragged to another row is ONE write, the
   assignment; the column is the assignee's to move and this page never does.

   Eight things must hold, and they are what this file checks:

   1. the page has the third tab, between Board and Reports, on its own
      route;
   2. a board draws one row per lane the daemon answers with, and the
      unassigned pool last; the head of a row says the session's state, its
      cflow step, and the queue in numbers;
   3. a card lands in the cell of its status, is the same card the Board tab
      draws, is draggable, and the one the worker takes next says so;
   4. a drop on a cell assigns the dragged issue to that ROW -- a session's
      name, or null for the pool -- and never sends a status;
   5. a refused assignment (409) is said where the drop happened;
   6. rows come busiest first, not in the daemon's creation order, and
      only running and paused sessions are among them: a killed or archived
      session, and an assignee that is not a session here, is folded out
      into a group of its own -- with the head still counting what the fold
      hides. A paused session has exited too, so the line is drawn by the
      lane's `category`, not by its status;
   7. an unassigned pool bigger than the cap is one folded cell that is
      still a drop target, and a cell opened past the cap offers the rest
      behind a button rather than drawing three hundred cards;
   8. the stylesheet lays a cell's cards out as a wrapping row -- the case
      of two or more issues in one status, which stacked one per line makes
      the row as tall as its fullest cell and (align-items: stretch) charges
      that height to every other cell in the row.

   Slice the real functions out of app.js and drive them against a stub
   DOM. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  const head = src.lastIndexOf("async ", start) === start - 6 ? start - 6 : start;
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, children: [], text: "", classes: new Set(), handlers: {}, dataset: {},
    style: {}, title: "", href: "", type: "", draggable: false,
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    fire(k, ev) { for (const fn of this.handlers[k] || []) fn(ev || {}); },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get classList() {
      const self = this;
      return {
        add: (...cs) => cs.forEach((c) => self.classes.add(c)),
        remove: (...cs) => cs.forEach((c) => self.classes.delete(c)),
        contains: (c) => self.classes.has(c),
        toggle: (c, on) => { on ? self.classes.add(c) : self.classes.delete(c); },
      };
    },
    all() {
      const out = [this];
      for (const k of this.children) out.push(...k.all());
      return out;
    },
    find(cls) { return this.all().filter((n) => n.classes.has(cls)); },
  };
  return n;
}
const document = { createElement: (t) => node(t) };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const stubs = `
let beadsFocus = "";
let beadsWorkspace = "";
let beadsDetail = null;
let beadsSession = "";
function clearBeadsSearch() { renderBeads(); }
function $() { return null; }
let beadsSection = "queues";
let beadsQueues = null;
let beadsQueuesError = "";
let beadsDragging = "";
let beadsOpen = true;
const beadsQSpentOpen = new Set();
const beadsQPoolOpen = new Set();
const beadsQCellOpen = new Set();
const BEADS_Q_CELL_CAP = 24;
function setSection(s) { beadsSection = s; }
function setWorkspace(w) { beadsWorkspace = w; }
function setQueues(q) { beadsQueues = q; }
function setError(e) { beadsQueuesError = e; }
const calls = [];
let answer = { ok: true, status: 200, body: {} };
function setAnswer(a) { answer = a; }
async function api(path, opts) {
  calls.push({ path, opts });
  const a = answer;
  return { ok: a.ok, status: a.status, json: async () => a.body };
}
let refreshed = 0;
function renderBeads() { refreshed++; }
`;

const ctx = {};
new Function(
  "exports", "document", "el",
  stubs
  + "const BEADS_STATUSES = " + JSON.stringify(["open", "in_ready", "in_progress", "in_review", "blocked", "closed"]) + ";\n"
  + "const BEADS_ACTIVE = new Set([\"open\", \"in_ready\", \"in_progress\", \"in_review\", \"blocked\"]);\n"
  + slice("beadsSortIssues") + slice("beadsPriBadge") + slice("beadsCard")
  + slice("beadsBoardLabel") + slice("beadsBoardWhere")
  + slice("beadsPageTabs") + slice("beadsWorkspaceTabs")
  + slice("renderQueues") + slice("beadsQueuesBoard") + slice("beadsQueueLane")
  + slice("beadsLaneCount") + slice("beadsLaneSpent") + slice("beadsQueueOrder") + slice("beadsQFoldBar")
  + slice("beadsQueueSummaryText") + slice("beadsQueueCell") + slice("beadsQueueCard")
  + slice("beadsAssign") + slice("beadsQueuesUrl") + slice("refreshQueues")
  + `
Object.assign(exports, {
  tabs: beadsPageTabs, render: renderQueues, board: beadsQueuesBoard,
  setSection, setWorkspace, setQueues, setError, setAnswer, calls,
  refreshed: () => refreshed,
  spentOpen: beadsQSpentOpen, poolOpen: beadsQPoolOpen,
  cellOpen: beadsQCellOpen, CAP: BEADS_Q_CELL_CAP,
});`)(ctx, document, el);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* ---- 1. the tab ------------------------------------------------------- */
const tabs = ctx.tabs();
check("the page has three readings, Queues in the middle",
      tabs.children.map((n) => n.text), ["Board", "Queues", "Reports"]);
check("each on its own route", tabs.children.map((n) => n.href),
      ["#/beads", "#/beads/queues", "#/beads/reports"]);
check("the Queues reading is selected here",
      tabs.children.map((n) => n.classes.has("on")), [false, true, false]);

/* ---- 2. rows ------------------------------------------------------------ */
const STATUSES = ["open", "in_ready", "in_progress", "in_review", "blocked"];
const BOARD = {
  root: "/repo",
  error: null,
  lanes: [
    {
      session: "s1", known: true, status: "busy", category: "running", issue: "a",
      cflow: { workflow: "improv-worker", step: "work" },
      issues: [
        { id: "a", title: "first", status: "in_progress", priority: 1, assignee: "s1" },
        { id: "b", title: "second", status: "open", priority: 2, assignee: "s1" },
        { id: "c", title: "third", status: "in_ready", priority: 3, assignee: "s1" },
      ],
      summary: { total: 3, waiting: 2, working: 1, review: 0, blocked: 0, next: "b" },
    },
    {
      session: "lead", known: false, status: null, issue: null, cflow: null,
      issues: [{ id: "d", title: "held by a human", status: "blocked", priority: 2, assignee: "lead" }],
      summary: { total: 1, waiting: 0, working: 0, review: 0, blocked: 1, next: null },
    },
  ],
  unassigned: [{ id: "e", title: "pool", status: "open", priority: 0 }],
};

// the unknown assignee's row sits behind the fold (section 6); open it so
// these sections see every kind of row
ctx.spentOpen.add("/repo");
let sec = ctx.board(BOARD, STATUSES);
const grid = sec.find("beads-queues")[0];
check("the grid's columns follow the daemon's status list",
      grid.style.gridTemplateColumns, "200px repeat(5, minmax(170px, 1fr))");
check("the column heads are the statuses, after a corner cell",
      grid.children.slice(0, 6).map((n) => n.text),
      ["session", "open", "in_ready", "in_progress", "in_review", "blocked"]);
const heads = sec.find("beads-q-head");
check("one row per lane, the pool last",
      heads.map((h) => h.find("beads-q-name")[0].text), ["s1", "lead", "unassigned"]);
check("a session's name links to its terminal", heads[0].find("beads-q-name")[0].href, "#/s/s1");
check("the head says state, step and the queue in numbers",
      [heads[0].find("beads-sess")[0].text, heads[0].find("beads-q-step")[0].text,
       heads[0].find("beads-q-sum")[0].text],
      ["busy", "improv-worker · work", "2 waiting · 1 working"]);
check("the primary issue is named", heads[0].find("beads-q-primary")[0].text, "primary a");
check("an assignee the daemon does not know is said to be one",
      [heads[1].find("beads-sess")[0].text, heads[1].find("beads-q-step").length,
       heads[1].find("beads-q-sum")[0].text],
      ["not a session here", 0, "0 waiting · 0 working · 1 blocked"]);
check("the pool row counts what waits for a queue",
      [heads[2].classes.has("pool"), heads[2].find("beads-q-sum")[0].text],
      [true, "1 waiting for a queue"]);
check("the board head counts queues and the pool",
      sec.find("beads-board-head")[0].find("wf-note")[0].text,
      "1 live queue · 1 hidden · 1 unassigned");

/* ---- 3. cards ----------------------------------------------------------- */
const cells = sec.find("beads-q-cell");
check("five cells per row, three rows", cells.length, 15);
const cellOf = (row, status) => cells[row * 5 + STATUSES.indexOf(status)];
check("a card sits in the cell of its status, in queue order",
      [cellOf(0, "in_progress").find("beads-id").map((n) => n.text),
       cellOf(0, "open").find("beads-id").map((n) => n.text),
       cellOf(0, "in_ready").find("beads-id").map((n) => n.text),
       cellOf(1, "blocked").find("beads-id").map((n) => n.text),
       cellOf(2, "open").find("beads-id").map((n) => n.text)],
      [["a"], ["b"], ["c"], ["d"], ["e"]]);
const cardB = cellOf(0, "open").find("beads-q-card")[0];
check("it is the Board tab's card, made draggable",
      [cardB.classes.has("beads-card"), cardB.draggable, cardB.dataset.issue,
       cardB.find("beads-id")[0].href],
      [true, true, "b", "#/beads/b"]);
check("the issue the worker takes next says so",
      [cardB.classes.has("next"), cardB.find("beads-q-next").length,
       cellOf(0, "in_ready").find("beads-q-card")[0].find("beads-q-next").length],
      [true, 1, 0]);
check("every cell remembers its row's session",
      [cellOf(0, "open").dataset.session, cellOf(1, "open").dataset.session,
       cellOf(2, "open").dataset.session],
      ["s1", "lead", ""]);
check("a board that could not be read says so and draws no grid",
      (() => { const s = ctx.board({ root: "/x", error: "br failed", lanes: [] }, STATUSES);
               return [s.find("wf-warning")[0].text, s.find("beads-queues").length]; })(),
      ["br failed", 0]);

/* ---- 4. a drop assigns to the row ---------------------------------------- */
async function drop(card, cell) {
  card.fire("dragstart", { dataTransfer: { setData() {} } });
  cell.fire("dragover", { preventDefault() {} });
  const over = cell.classes.has("over");
  cell.fire("drop", { preventDefault() {}, dataTransfer: { getData: () => "" } });
  // the handler's assign is async: let it run
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  return over;
}

(async () => {
  ctx.setAnswer({ ok: true, status: 200, body: { issue: "e", assignee: "s1", changed: true } });
  const poolCard = cellOf(2, "open").find("beads-q-card")[0];
  let over = await drop(poolCard, cellOf(0, "in_review"));
  check("a cell lights up while a card is over it", over, true);
  const writes = () => ctx.calls.filter((c) => c.opts).map((c) => [c.path, JSON.parse(c.opts.body)]);
  check("dropping on a session's row assigns to that session, on that board, "
        + "and sends no status",
        writes(), [["/api/beads/e/assign", { session: "s1", cwd: "/repo" }]]);
  check("the board is read again after a write",
        ctx.calls.length >= 2 && ctx.calls[1].path, "/api/beads/queues?fold=1");
  ctx.calls.length = 0;

  over = await drop(cardB, cellOf(2, "in_progress"));
  check("dropping on the pool takes the issue off every queue",
        writes()[0], ["/api/beads/b/assign", { session: null, cwd: "/repo" }]);
  ctx.calls.length = 0;

  /* ---- 5. a refusal is said where it happened -------------------------- */
  ctx.setAnswer({ ok: false, status: 409,
                  body: { error: "a is in_progress under s1, which is still running" } });
  const cardA = cellOf(0, "in_progress").find("beads-q-card")[0];
  const target = cellOf(1, "in_progress");
  await drop(cardA, target);
  check("the refusal lands in the cell that was dropped on",
        target.find("beads-q-note").map((n) => n.text),
        ["a is in_progress under s1, which is still running"]);
  check("and no re-read follows a refused write", ctx.calls.map((c) => c.path),
        ["/api/beads/a/assign"]);

  /* ---- the page around the grid ---------------------------------------- */
  ctx.setQueues(null);
  const view = el("div");
  ctx.render(view);
  check("before the daemon answers the page says so",
        view.find("wf-note").map((n) => n.text).slice(-1), ["loading…"]);
  ctx.setQueues({ statuses: STATUSES, boards: [BOARD] });
  const view2 = el("div");
  ctx.render(view2);
  check("with an answer it draws one grid per board", view2.find("beads-queues").length, 1);
  ctx.setQueues({ statuses: STATUSES, boards: [BOARD,
  { ...BOARD, root: "/second", board: "second-board",
    db: "/second/.beads/beads.db" }] });
  const multi = el("div");
  ctx.render(multi);
  check("workspace tabs keep one board visible", multi.find("beads-queues").length, 1);
  const tabs = multi.find("beads-workspace-tabs")[0];
  check("one tab per workspace", tabs.children.length, 2);
  tabs.children[1].handlers.click[0]();
  const second = el("div");
  ctx.render(second);
  // The head names the board, because that is what the issues on it are
  // filed under; the directory and the database file are in its title.
  check("workspace switch shows the selected board's name",
        second.find("beads-board-head")[0].children[0].text, "second-board");
  ctx.setQueues({ statuses: STATUSES, boards: [] });
  const view3 = el("div");
  ctx.render(view3);
  check("no board is said, not hidden",
        view3.find("wf-note").some((n) => n.text.startsWith("no board")), true);

  /* ---- 6. the order of the rows, and the ended ones ---------------------
     The daemon answers in ITS session order, which is creation order. On a
     machine that has run sessions for a fortnight that puts the oldest
     exited ones at the top and the running one at the bottom -- measured on
     this repository's own board: 29 lanes, 23 of them holding one issue,
     most of those sessions exited, and 4 issues in flight among the lot. */
  const lane = (session, o = {}) => ({
    session, known: o.known !== false, status: o.status || "idle",
    category: o.known === false ? null : (o.category || "running"),
    issue: null, cflow: null,
    issues: (o.issues || []).map((id) => ({
      id, title: id, status: o.st || "open", priority: 2, assignee: session,
    })),
    summary: {
      total: (o.issues || []).length,
      waiting: o.working ? 0 : (o.issues || []).length,
      working: o.working || 0, review: o.review || 0, blocked: 0, next: null,
    },
  });
  const BUSY = {
    root: "/many", error: null,
    lanes: [
      lane("s01", { status: "exited", category: "killed", issues: ["x1"] }),
      lane("s02", { status: "exited", category: "archived", issues: ["x2"] }),
      lane("s03", {}),
      lane("s04", { issues: ["w1", "w2"] }),
      lane("s05", { issues: ["p1"], st: "in_progress", working: 1 }),
      lane("human", { known: false, status: null, issues: ["h1"] }),
      lane("s06", { status: "exited", category: "paused", issues: ["z1"] }),
    ],
    unassigned: [],
  };
  const rowNames = (s) =>
    s.find("beads-q-head").map((h) => h.find("beads-q-name")[0].text);
  ctx.spentOpen.clear(); ctx.poolOpen.clear(); ctx.cellOpen.clear();
  let busy = ctx.board(BUSY, STATUSES);
  check("in flight first, then queues with work waiting (fullest first), "
        + "then idle — running and paused sessions only",
        rowNames(busy), ["s05", "s04", "s06", "s03", "unassigned"]);
  check("a paused session is drawn although its status is exited, and its "
        + "badge says paused",
        busy.find("beads-q-head")[2].find("beads-sess")[0].text, "paused");
  check("killed, archived and not-a-session-here rows are all folded away",
        ["s01", "s02", "human"].some((n) => rowNames(busy).includes(n)), false);
  check("the head counts what the fold hides, so the number a reader opens "
        + "it on is not the number it hid",
        busy.find("beads-board-head")[0].find("wf-note")[0].text,
        "4 live queues · 3 hidden · 0 unassigned");
  const fold = busy.find("beads-q-fold");
  check("one toggle stands where the hidden rows would stand, saying how "
        + "many and how much they hold",
        [fold.length, fold[0].children[0].text],
        [1, "▸ 3 not running or paused, holding 3 issues"]);
  fold[0].children[0].fire("click");
  busy = ctx.board(BUSY, STATUSES);
  check("opened, the hidden rows are drawn after the live ones and before "
        + "the pool",
        rowNames(busy),
        ["s05", "s04", "s06", "s03", "human", "s01", "s02", "unassigned"]);
  check("and their heads are marked",
        busy.find("beads-q-head").filter((h) => h.classes.has("spent")).map((h) => h.find("beads-q-name")[0].text),
        ["human", "s01", "s02"]);
  check("the toggle now folds them back", busy.find("beads-q-fold")[0]
        .children[0].text, "▾ 3 not running or paused, holding 3 issues");
  check("a daemon without `category` is read by status: exited is folded",
        rowNames(ctx.board({ root: "/old", error: null, unassigned: [], lanes: [
          { ...lane("o1", { issues: ["q"] }), category: undefined },
          { ...lane("o2", { status: "exited", issues: ["r"] }), category: undefined },
        ] }, STATUSES)), ["o1", "unassigned"]);

  /* ---- 7. a pool too tall to draw ------------------------------------- */
  const POOL = {
    root: "/pool", error: null, lanes: [lane("s1", { issues: ["a"] })],
    unassigned: Array.from({ length: 30 }, (_, i) => ({
      id: "u" + i, title: "u" + i, status: "open", priority: 2,
    })),
  };
  ctx.spentOpen.clear(); ctx.poolOpen.clear(); ctx.cellOpen.clear();
  let pooled = ctx.board(POOL, STATUSES);
  const poolCells = pooled.find("beads-q-cell").filter((c) => !c.dataset.session);
  check("a pool past the cap is one cell across the row, not five",
        [poolCells.length, poolCells[0].classes.has("folded"),
         poolCells[0].style.gridColumn, poolCells[0].find("beads-card").length],
        [1, true, "span 5", 0]);
  check("and it says how many it is not drawing",
        poolCells[0].children[0].text, "▸ 30 unassigned issues — open the pool");
  ctx.calls.length = 0;
  ctx.setAnswer({ ok: true, status: 200, body: { changed: true } });
  const liveCard = pooled.find("beads-q-card")[0];
  await drop(liveCard, poolCells[0]);
  check("the folded pool is still a drop target, so taking an issue off a "
        + "queue never needs the fold opened first",
        ctx.calls.filter((c) => c.opts).map((c) => [c.path, JSON.parse(c.opts.body)]),
        [["/api/beads/a/assign", { session: null, cwd: "/pool" }]]);
  ctx.calls.length = 0;
  poolCells[0].children[0].fire("click");
  pooled = ctx.board(POOL, STATUSES);
  const opened = pooled.find("beads-q-cell").filter(
    (c) => !c.dataset.session && c.classes.has("open"))[0];
  check("opened, the pool is a normal row again — but capped, because one "
        + "tall cell sets the height of every other cell in its row",
        [opened.find("beads-card").length, opened.find("beads-q-more")[0].text],
        [ctx.CAP, "+6 more"]);
  opened.find("beads-q-more")[0].fire("click");
  pooled = ctx.board(POOL, STATUSES);
  const whole = pooled.find("beads-q-cell").filter(
    (c) => !c.dataset.session && c.classes.has("open"))[0];
  check("and the button hands over the rest of that one cell",
        [whole.find("beads-card").length, whole.find("beads-q-more").length],
        [30, 0]);
  ctx.spentOpen.clear(); ctx.poolOpen.clear(); ctx.cellOpen.clear();

  /* ---- 8. two in one status wrap, they do not stack ---------------------
     The layout half of the same defect. A cell is a column of cards in CSS,
     so a status holding two issues makes its cell twice as tall -- and the
     grid is `align-items: stretch`, so the four other cells in that row are
     charged the same height with nothing in them. */
  const css = fs.readFileSync(
    path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
              "style.css"),
    "utf8"
  );
  const rule = (sel) => {
    const at = css.indexOf(sel + " {");
    return at < 0 ? "" : css.slice(at, css.indexOf("}", at));
  };
  check("a cell lays its cards out as a wrapping row",
        /flex-flow:\s*row wrap/.test(rule(".beads-q-cell")), true);
  check("with a basis for the wrap to happen on, so a narrow column takes "
        + "one card a line and a wide one takes two",
        /flex:\s*1 1 \d+px/.test(rule(".beads-q-card")), true);
  check("cards pack to the top rather than spreading down the cell",
        /align-content:\s*flex-start/.test(rule(".beads-q-cell")), true);
  check("the fold row spans every column",
        /grid-column:\s*1 \/ -1/.test(rule(".beads-q-fold")), true);
  check("a refusal and a '+n more' each take a line of their own, whatever "
        + "the cards beside them did",
        [/flex:\s*1 1 100%/.test(rule(".beads-q-note")),
         /flex:\s*1 1 100%/.test(rule(".beads-q-more"))],
        [true, true]);

  /* ---- 9. folded rows come as counts (claunch-fa1xk) -------------------
     The daemon answers `fold=1` with the folded lanes' and the tall pool's
     cards left out: a count per lane, `unassigned_count` for the pool. The
     page must count them the same as cards it holds, ask for the cards when
     a fold is opened, and not draw an opened fold empty meanwhile. */
  const SLIM = {
    root: "/slim", error: null,
    lanes: [
      lane("s1", { issues: ["a"] }),
      { ...lane("s9", { status: "exited", issues: [] }), category: "killed",
        folded: true, count: 4 },
    ],
    unassigned: [], unassigned_count: 30,
  };
  ctx.spentOpen.clear(); ctx.poolOpen.clear(); ctx.cellOpen.clear();
  ctx.setWorkspace("/slim");
  let slim = ctx.board(SLIM, STATUSES);
  check("the head counts what the answer only counted",
        slim.find("beads-board-head")[0].children.at(-1).text,
        "1 live queue · 1 hidden · 30 unassigned");
  check("so does the fold",
        slim.find("beads-q-fold")[0].children[0].text,
        "▸ 1 not running or paused, holding 4 issues");
  const slimPool = slim.find("beads-q-cell").filter((c) => !c.dataset.session)[0];
  check("and the folded pool",
        [slimPool.classes.has("folded"), slimPool.children[0].text],
        [true, "▸ 30 unassigned issues — open the pool"]);
  ctx.calls.length = 0;
  ctx.setAnswer({ ok: true, status: 200, body: { boards: [] } });
  slim.find("beads-q-fold")[0].children[0].fire("click");
  check("opening the fold asks for its cards",
        ctx.calls.map((c) => c.path), ["/api/beads/queues?fold=1&open=spent"]);
  slim = ctx.board(SLIM, STATUSES);
  check("and until they come the fold says so rather than drawing rows "
        + "with nothing in them",
        [slim.find("beads-q-fold")[0].children[0].text, rowNames(slim)],
        ["▾ 1 not running or paused, holding 4 issues — loading…",
         ["s1", "unassigned"]]);
  ctx.calls.length = 0;
  slimPool.children[0].fire("click");
  check("opening the pool asks for it too",
        ctx.calls.map((c) => c.path), ["/api/beads/queues?fold=1&open=spent&open=pool"]);
  slim = ctx.board(SLIM, STATUSES);
  const waiting = slim.find("beads-q-cell").filter((c) => !c.dataset.session);
  check("and the pool stays one cell saying it is loading",
        [waiting.length, waiting[0].children[0].text],
        [1, "30 unassigned issues — loading…"]);
  ctx.spentOpen.clear(); ctx.poolOpen.clear(); ctx.cellOpen.clear();
  ctx.setWorkspace("");

  if (failures) process.exit(1);
  console.log("queues_check ok");
})();
