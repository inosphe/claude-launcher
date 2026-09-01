/* The Beads page's Queues tab: every session's queue as a swimlane.

   A session's queue is the board's own reading -- the active issues assigned
   to it, in the order its worker takes them -- and the page stores nothing
   beside the session to draw it. Rows are sessions plus an unassigned pool,
   columns are statuses, and a card dragged to another row is ONE write, the
   assignment; the column is the assignee's to move and this page never does.

   Five things must hold, and they are what this file checks:

   1. the page has the third tab, between Board and Reports, on its own
      route;
   2. a board draws one row per lane the daemon answers with, in the daemon's
      order, and the unassigned pool last; the head of a row says the
      session's state, its cflow step, and the queue in numbers;
   3. a card lands in the cell of its status, is the same card the Board tab
      draws, is draggable, and the one the worker takes next says so;
   4. a drop on a cell assigns the dragged issue to that ROW -- a session's
      name, or null for the pool -- and never sends a status;
   5. a refused assignment (409) is said where the drop happened.

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
let beadsSection = "queues";
let beadsQueues = null;
let beadsQueuesError = "";
let beadsDragging = "";
let beadsOpen = true;
function setSection(s) { beadsSection = s; }
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
  + slice("beadsPageTabs")
  + slice("renderQueues") + slice("beadsQueuesBoard") + slice("beadsQueueLane")
  + slice("beadsQueueSummaryText") + slice("beadsQueueCell") + slice("beadsQueueCard")
  + slice("beadsAssign") + slice("refreshQueues")
  + `
Object.assign(exports, {
  tabs: beadsPageTabs, render: renderQueues, board: beadsQueuesBoard,
  setSection, setQueues, setError, setAnswer, calls,
  refreshed: () => refreshed,
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
      session: "s1", known: true, status: "busy", issue: "a",
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
      sec.find("beads-board-head")[0].find("wf-note")[0].text, "2 queues · 1 unassigned");

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
        ctx.calls.length >= 2 && ctx.calls[1].path, "/api/beads/queues");
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
  ctx.setQueues({ statuses: STATUSES, boards: [] });
  const view3 = el("div");
  ctx.render(view3);
  check("no board is said, not hidden",
        view3.find("wf-note").some((n) => n.text.startsWith("no board")), true);

  if (failures) process.exit(1);
  console.log("queues_check ok");
})();
