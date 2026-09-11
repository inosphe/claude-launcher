/* The spawn form's connect row, and what each offered peer says about itself.

   The row used to be a line of bare handles, so deciding who a child may talk
   to meant already knowing what every `sN` on the mesh is for. Each candidate
   now carries a state dot on its face and a dialog behind it, holding the same
   summary the rail's briefing card draws: state, one-line job description,
   goal, now, progress, plus the branch and issue the session poll already
   carries.

   What is checked here is the part a browser cannot be asked about in a test:
   where the dialog's content comes from, what it costs, and what it says when
   there is nothing to show.

   - the read is the CACHED one (`?cached=1`), which composes nothing. The
     ordinary endpoint's cache key carries the transcript's mtime, so a live
     session's key has always moved and a hover through it would spend an LLM
     generation per pass of the mouse;
   - one read per session however many times it is hovered, and a read still
     in flight is joined rather than restarted;
   - the first paint comes from the digest the session poll already carries,
     so the dialog is never an empty box while the read is out;
   - the four blank cases say four different things (reading, no session, a
     failed read, nothing summarised yet), and a member of another daemon is
     not read at all;
   - the dialog closes on mouseleave and on blur, and closing takes its scroll
     listener with it; the modal closing takes the node and the reads. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"), "utf8");

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
function sliceStmt(decl) {
  const start = src.indexOf(decl);
  if (start < 0) throw new Error("missing " + decl);
  return src.slice(start, src.indexOf(";", start) + 1);
}

/* ---- stub DOM ----------------------------------------------------------
   Richer than the spawn modal harness's, in the two places this feature
   needs: the dialog measures itself and its anchor to decide which side of
   the row it opens on, and it lives on document.body rather than inside the
   form. Both are stubbed rather than skipped so the placement rule is
   exercised instead of being trusted. */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), handlers: {}, dataset: {},
    attributes: {}, style: {}, value: undefined, checked: false,
    hidden: false, parent: null, rect: null,
    appendChild(c) { this.kids.push(c); c.parent = this; return c; },
    append(...cs) { cs.forEach((c) => this.appendChild(c)); },
    remove() {
      if (this.parent) this.parent.kids.splice(this.parent.kids.indexOf(this), 1);
      this.parent = null;
    },
    addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); },
    removeEventListener(k, fn) {
      this.handlers[k] = (this.handlers[k] || []).filter((f) => f !== fn);
    },
    setAttribute(k, v) { this.attributes[k] = String(v); },
    fire(k, ev) { return Promise.all((this.handlers[k] || []).map((f) => f(ev))); },
    getBoundingClientRect() {
      return this.rect || { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 };
    },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get innerHTML() { return ""; },
    set innerHTML(v) { this.kids = []; },
  };
  if (tag === "input") n.value = "";
  return n;
}
const walk = (n, out = []) => {
  for (const k of n.kids) { out.push(k); walk(k, out); }
  return out;
};
const texts = (n) => walk(n).map((k) => k.text).filter(Boolean).join(" | ");
const tags = (n, tag) => walk(n).filter((k) => k.tag === tag);
const classed = (n, cls) => walk(n).filter((k) => k.classes.has(cls));

const body = node("body");
const document = {
  body,
  createElement: (tag) => node(tag),
  listeners: {},
  addEventListener(k, fn) { (this.listeners[k] ||= []).push(fn); },
  removeEventListener(k, fn) {
    this.listeners[k] = (this.listeners[k] || []).filter((f) => f !== fn);
  },
  count(k) { return (this.listeners[k] || []).length; },
};
const window = { innerWidth: 1200, innerHeight: 800 };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}

/* ---- the daemon --------------------------------------------------------
   Every call is recorded so the cost of a hover can be counted, and the
   briefing answers are held open until the test releases them, which is how
   the first paint (digest only) is told apart from the second (the stored
   briefing). */
let sent = [];
let routes = {};
let held = [];
const api = async (p) => {
  sent.push(p);
  const key = Object.keys(routes)
    .filter((k) => p.startsWith(k)).sort((a, b) => b.length - a.length)[0];
  const r = key ? routes[key] : { status: 404, doc: { error: "no such session" } };
  if (r.throw) throw new Error("offline");
  const resp = {
    ok: r.status === undefined || r.status === 200,
    status: r.status || 200,
    json: async () => r.doc || {},
  };
  if (r.hold) return new Promise((res) => held.push(() => res(resp)));
  return resp;
};
const release = async () => {
  const pending = held; held = [];
  pending.forEach((fn) => fn());
  await new Promise((res) => setTimeout(res, 0));
  await new Promise((res) => setTimeout(res, 0));
};
const settle = () => new Promise((res) => setTimeout(res, 0));

const ctx = {};
new Function("exports", "document", "window", "el", "api", `
let sessionsCache = [];
let spawnModal = null;
${slice("fmtAge")}
${slice("briefingStateClass")}
${slice("spawnMeshNow")}
${slice("sessionCategory")}
${slice("connectCandidate")}
${slice("spawnConnectNow")}
${sliceStmt("let spawnBriefCache = null")}
${slice("spawnBriefRead")}
${slice("spawnBriefNode")}
${slice("spawnBriefHide")}
${slice("spawnBriefDrop")}
${slice("spawnBriefPlace")}
${slice("spawnBriefNote")}
${slice("spawnBriefPaint")}
${slice("spawnBriefShow")}
${slice("refreshSpawnConnect")}
Object.assign(exports, {
  refreshSpawnConnect, spawnBriefDrop, spawnBriefHide,
  setSessions: (a) => { sessionsCache = a; },
  setModal: (m) => { spawnModal = m; },
  pop: () => spawnBriefPop,
  cacheSize: () => (spawnBriefCache ? spawnBriefCache.size : -1),
});
`)(ctx, document, window, el, api);

let failures = 0;
const check = (name, cond, extra) => {
  if (cond) return;
  failures++;
  console.log(`FAIL ${name}${extra === undefined ? "" : " — " + JSON.stringify(extra)}`);
};

/* ---- the roster the form is offered ----------------------------------- */
const MEMBERS = [
  { handle: "lead1", session: "lead1", local: true, role: "leader" },
  { handle: "w2", session: "w2", local: true, role: "worker" },
  { handle: "w6", session: "w6", local: true, role: "worker" },
  { handle: "w7", session: "w7", local: true, role: "reviewer" },
  { handle: "far", session: "r9", local: false, machine: "box-b", role: "worker" },
];
const RUNNING = (name, extra) => ({
  name, status: "running", exited: false, archived_at: null, ...extra,
});

function mkState(meshName) {
  const ui = {
    mesh: { value: meshName === undefined ? "m0" : meshName },
    parentMesh: "m0",
    handle: { value: "kid" },
    parentSess: { _meshHandle: "lead1" },
    connectRow: el("div", "sess-spawn-row sess-spawn-connect"),
    connectHandles: [], _connectChecked: [],
  };
  return { ui };
}

async function buildRow(st) {
  ctx.setModal(st);
  await ctx.refreshSpawnConnect(st);
  return st.ui.connectRow;
}

(async () => {
  ctx.setSessions([
    RUNNING("lead1"),
    RUNNING("w2", {
      briefing: { one_line: "widens the rail's beads block", state: "working" },
      branch: "s-w2-beads-block", issue: "claunch-aaaa",
    }),
    RUNNING("w6", { briefing: { one_line: "chases a flaky attach", state: "blocked" } }),
    RUNNING("w7"),
  ]);
  routes = { "/api/mesh/m0": { doc: { members: MEMBERS } } };

  /* ---- the row's own face ---------------------------------------------- */
  let st = mkState();
  let row = await buildRow(st);
  check("the row offers every running peer but the parent",
    st.ui.connectHandles.join(",") === "w2,w6,w7,far",
    st.ui.connectHandles);
  const dots = classed(row, "sess-spawn-check-dot");
  check("every offered peer gets a dot", dots.length === 4, dots.length);
  check("the dot wears the peer's briefing state",
    dots[0].classes.has("st-working") && dots[1].classes.has("st-blocked"),
    dots.map((d) => d.className));
  /* w7 has a session record but no briefing, and `far` has no local record at
     all. Neither has a state to report, and both keep a dot: it is the mark
     that says the row is hoverable, and the dialog explains the blank. */
  check("a peer with no briefing keeps a dot, dimmed",
    dots[2].classes.has("st-none") && dots[3].classes.has("st-none"),
    dots.slice(2).map((d) => d.className));
  check("the handle is still the label's text",
    classed(row, "sess-spawn-check-name").map((n) => n.text).join(",") ===
      "w2,w6,w7,far",
    classed(row, "sess-spawn-check-name").map((n) => n.text));
  check("ticking a box still reports the handle, dots and all",
    (() => {
      const box = tags(row, "input")[0];
      box.checked = true;
      box.handlers.change[0]();
      return st.ui._connectChecked.join(",") === "w2";
    })(), st.ui._connectChecked);

  /* ---- the first paint: what the session poll already carries ---------- */
  routes["/api/sessions/w2/briefing"] = {
    hold: true,
    doc: {
      session: "w2", cached: true, generated_at: new Date(Date.now() - 90000).toISOString(),
      briefing: {
        state: "working", goal: "give the rail's beads block room",
        now: "rewriting the kanban column widths",
        progress: "3 of 5 columns", "one-line-job-description": "widens the rail's beads block",
      },
    },
  };
  const labels = walk(row).filter((n) => n.tag === "label");
  labels[0].rect = { left: 300, top: 400, right: 340, bottom: 416, width: 40, height: 16 };
  sent = [];
  const hover = labels[0].fire("mouseenter");
  await settle();
  let pop = ctx.pop();
  check("hovering opens the dialog", pop && pop.hidden === false, pop && pop.hidden);
  check("the dialog is drawn on the body, outside the form's scroller",
    pop && pop.parent === body, pop && !!pop.parent);
  check("...and names itself a tooltip",
    pop && pop.attributes.role === "tooltip", pop && pop.attributes);
  check("the first paint is up before the read answers",
    texts(pop).includes("widens the rail's beads block") &&
      texts(pop).includes("working"), texts(pop));
  check("...with the facts the session poll carries",
    texts(pop).includes("s-w2-beads-block") && texts(pop).includes("claunch-aaaa"),
    texts(pop));
  check("...and says the read is still out",
    texts(pop).includes("reading…"), texts(pop));
  check("the read is the cached one, which composes nothing",
    sent.length === 1 && sent[0] === "/api/sessions/w2/briefing?cached=1", sent);

  /* ---- the second paint: the stored briefing --------------------------- */
  await release();
  await hover;
  pop = ctx.pop();
  check("the stored briefing lands",
    texts(pop).includes("give the rail's beads block room") &&
      texts(pop).includes("rewriting the kanban column widths") &&
      texts(pop).includes("3 of 5 columns"), texts(pop));
  check("...with its age, so a stale summary reads as one",
    texts(pop).includes("summarised 1m ago"), texts(pop));
  check("...and the note it replaced is gone",
    !texts(pop).includes("reading…"), texts(pop));

  /* ---- placement ------------------------------------------------------- */
  check("the dialog opens under the row it belongs to",
    pop.style.top === "422px" && pop.style.left === "300px",
    [pop.style.top, pop.style.left]);
  pop.rect = { left: 0, top: 0, right: 340, bottom: 300, width: 340, height: 300 };
  labels[0].rect = { left: 1100, top: 700, right: 1140, bottom: 716, width: 40, height: 16 };
  await labels[0].fire("mouseenter");
  await settle();
  check("...and flips above the row when the bottom edge is nearer than it is tall",
    ctx.pop().style.top === "394px", ctx.pop().style.top);
  check("...and never past the right edge",
    ctx.pop().style.left === "852px", ctx.pop().style.left);

  /* ---- one read per session ------------------------------------------- */
  sent = [];
  await labels[0].fire("mouseleave");
  await labels[0].fire("mouseenter");
  await settle();
  check("hovering the same peer again reads nothing",
    sent.length === 0, sent);
  check("...and repaints from what was already read",
    texts(ctx.pop()).includes("3 of 5 columns"), texts(ctx.pop()));

  /* A read still in flight is joined, not restarted: two hovers that cross
     while the daemon is slow must not become two requests. */
  routes["/api/sessions/w6/briefing"] = { hold: true, doc: { session: "w6", briefing: null } };
  sent = [];
  const first = labels[1].fire("mouseenter");
  await settle();
  const second = labels[1].fire("mouseenter");
  await settle();
  check("two hovers over a slow read are still one request",
    sent.length === 1, sent);
  await release();
  await Promise.all([first, second]);

  /* ---- the blank cases ------------------------------------------------- */
  check("a session with nothing summarised says so, rather than showing a blank",
    texts(ctx.pop()).includes("no briefing summarised yet"), texts(ctx.pop()));
  check("...while still showing the digest the poll carries",
    texts(ctx.pop()).includes("chases a flaky attach"), texts(ctx.pop()));

  sent = [];
  await labels[3].fire("mouseenter");
  await settle();
  check("a member of another daemon is not read at all", sent.length === 0, sent);
  check("...and the dialog says whose it is to answer",
    texts(ctx.pop()).includes("another daemon") &&
      texts(ctx.pop()).includes("box-b"), texts(ctx.pop()));

  routes["/api/sessions/w7/briefing"] = { status: 404, doc: { error: "no session named 'w7'" } };
  await labels[2].fire("mouseenter");
  await settle();
  check("a 404 on the cached read means the session is gone, not the briefing",
    texts(ctx.pop()).includes("no session by that name"), texts(ctx.pop()));

  routes["/api/sessions/w7/briefing"] = { throw: true };
  ctx.spawnBriefDrop();
  st = mkState();
  row = await buildRow(st);
  const later = walk(row).filter((n) => n.tag === "label");
  await later[2].fire("mouseenter");
  await settle();
  check("a failed read is reported as one",
    texts(ctx.pop()).includes("request failed"), texts(ctx.pop()));

  /* ---- closing --------------------------------------------------------- */
  const scrolls = document.count("scroll");
  check("an open dialog listens for the scroll that would strand it",
    scrolls === 1, scrolls);
  await later[2].fire("mouseleave");
  check("leaving the row hides it", ctx.pop().hidden === true);
  check("...and takes the scroll listener with it",
    document.count("scroll") === 0, document.count("scroll"));

  /* The keyboard reaches the same dialog: the box is what a tab lands on. */
  routes["/api/sessions/w6/briefing"] = { doc: { session: "w6", briefing: null } };
  const kbBox = tags(later[1], "input")[0];
  const wasPop = ctx.pop();
  await kbBox.fire("focus");
  await settle();
  check("focusing a box opens the dialog for its peer",
    ctx.pop().hidden === false && texts(ctx.pop()).includes("w6"),
    texts(ctx.pop()));
  check("...in the same node the pointer was using, not a second one",
    ctx.pop() === wasPop && body.kids.length === 1, body.kids.length);
  await kbBox.fire("blur");
  check("...and blurring closes it", ctx.pop().hidden === true);

  ctx.spawnBriefDrop();
  check("closing the modal takes the dialog off the body",
    ctx.pop() === null && body.kids.length === 0,
    body.kids.length);
  check("...and drops the reads, so the next open asks the daemon again",
    ctx.cacheSize() === -1, ctx.cacheSize());

  /* ---- a mesh with nothing to offer ------------------------------------ */
  st = mkState("-");
  row = await buildRow(st);
  check("a child in no mesh is offered no peers and no dialog",
    st.ui.connectHandles.length === 0 && row.kids.length === 0,
    [st.ui.connectHandles, row.kids.length]);

  console.log(failures ? `spawnbrief_check: ${failures} failure(s)` : "spawnbrief_check: ok");
  process.exit(failures ? 1 : 0);
})().catch((err) => { console.error(err); process.exit(1); });
