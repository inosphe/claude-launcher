/* The session's conversation pane (talk-pane.js) over the threads and QA
   panes, and the links between them, claunch-78dpm.

   What has to hold:
     helpers  -- three modes; "both" shows both panes; following a link to a
                 pane not on screen moves to "both", one on screen stays.
     tabs     -- mounting in a mode places only that mode's panes; a tab
                 press asks the page (onMode) and the page's setMode places
                 the panes without reloading the one that stays.
     links    -- a QA card draws its thread links as chips; a chip selects
                 that thread (from "qa" through "both"); "쓰레드로 논의"
                 opens the new-thread form about the question and the
                 thread is created with {qa}; a thread's question chip
                 brings the card in; "QA로 정리 요청" posts ask_qa.
     wired    -- talk-pane.js loads after both panes and before app.js;
                 openSplit/closeSplit go through TalkPane; the header
                 buttons read "both" as on, and pressing one there hides
                 only its column; talk-pane.js never assigns HTML. */
const assert = require("assert/strict");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const STATIC = path.join(__dirname, "../../src/claude_launcher/web/static");
const read = (f) => fs.readFileSync(path.join(STATIC, f), "utf8");

class N {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.className = "";
    this._text = "";
    this.dataset = {};
    this.handlers = {};
    this.attrs = {};
    this.value = "";
    this.disabled = false;
    this.open = false;
    const self = this;
    this.classList = {
      contains: (c) => self.className.split(/\s+/).includes(c),
      add: (c) => self.classList.toggle(c, true),
      remove: (c) => self.classList.toggle(c, false),
      toggle(c, on) {
        const set = new Set(self.className.split(/\s+/).filter(Boolean));
        if (on === undefined) on = !set.has(c);
        if (on) set.add(c); else set.delete(c);
        self.className = [...set].join(" ");
        return on;
      },
    };
  }
  set textContent(t) { this.children = []; this._text = String(t); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  _adopt(n) { if (n.parentNode) n.remove(); n.parentNode = this; return n; }
  append(...ns) { for (const n of ns) this.children.push(this._adopt(n)); }
  appendChild(n) { this.append(n); return n; }
  prepend(n) { this.children.unshift(this._adopt(n)); }
  replaceChildren(...ns) { for (const c of this.children) c.parentNode = null; this.children = []; this.append(...ns); }
  insertBefore(n, ref) {
    this._adopt(n);
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(n); else this.children.splice(i, 0, n);
  }
  replaceWith(n) {
    const p = this.parentNode;
    if (!p) return;
    p._adopt(n);
    p.children.splice(p.children.indexOf(this), 1, n);
    this.parentNode = null;
  }
  remove() { if (this.parentNode) { const p = this.parentNode; p.children = p.children.filter((c) => c !== this); this.parentNode = null; } }
  contains(n) { for (let x = n; x; x = x.parentNode) if (x === this) return true; return false; }
  closest(sel) { for (let x = this; x; x = x.parentNode) if (x._match(sel)) return x; return null; }
  scrollIntoView() { this.scrolled = true; }
  focus() {}
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  get nextSibling() { const p = this.parentNode; return p ? p.children[p.children.indexOf(this) + 1] || null : null; }
  addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); }
  fire(k, e = {}) { for (const fn of this.handlers[k] || []) fn({preventDefault() {}, ...e}); }
  _match(sel) { return sel.startsWith(".") ? this.classList.contains(sel.slice(1)) : this.tagName === sel.toUpperCase(); }
  /* Descendant selectors ("a .b c") are enough for the panes. */
  querySelectorAll(sel) {
    const parts = sel.trim().split(/\s+/);
    let scope = [this];
    for (const part of parts) {
      const next = [];
      for (const s of scope) {
        const walk = (n) => { for (const c of n.children) { if (c._match(part) && !next.includes(c)) next.push(c); walk(c); } };
        walk(s);
      }
      scope = next;
    }
    return scope;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

const document = {createElement: (t) => new N(t), activeElement: null, hidden: false};
const settle = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r)); };

function harness() {
  const threads = [
    {id: "th-1", title: "Q: Which base?", status: "open", opened_by: "user", last_seq: 1, qa: "qa-1",
     last: {author: "user", body: "why?"},
     links: [{id: "qa-1", title: "Which base?", status: "open", rel: "subject"}]},
    {id: "th-2", title: "Ports", status: "open", opened_by: "agent", last_seq: 0, qa: null, last: null,
     links: [{id: "qa-2", title: "Which port?", status: "answered", rel: "filed"}]},
  ];
  const questions = [
    {id: "qa-1", session: "s1", title: "Which base?", body: "", options: [], multi: false, revision: 1,
     status: "open", answer: null, thread: null,
     links: [{id: "th-1", title: "Q: Which base?", status: "open", rel: "discussion"}]},
    {id: "qa-2", session: "s1", title: "Which port?", body: "", options: [], multi: false, revision: 2,
     status: "answered", answer: {text: "8080", choices: [], revision: 2}, thread: "th-2",
     links: [{id: "th-2", title: "Ports", status: "open", rel: "source"}]},
    {id: "qa-3", session: "s1", title: "Settled?", body: "", options: [], multi: false, revision: 3,
     status: "closed", answer: {text: "yes", choices: [], revision: 2}, thread: null, links: []},
  ];
  const calls = [];
  const api = async (p, opts = {}) => {
    const body = opts.body ? JSON.parse(opts.body) : undefined;
    calls.push([opts.method || "GET", p, body]);
    let doc = {};
    if (p.startsWith("/api/threads?session=")) doc = {session: "s1", threads, open: 2, agent_seq: 0};
    else if (/^\/api\/threads\/th-\d\?after=/.test(p)) {
      const id = p.split("?")[0].split("/").pop();
      doc = {thread: threads.find((t) => t.id === id), messages: []};
    } else if (p.startsWith("/api/qa?session=")) doc = {session: "s1", questions, unresolved: 1, agent_seq: 1, nudge: null};
    else if (p === "/api/threads") doc = {thread: {...threads[0], id: "th-9"}, delivered: true};
    else if (/\/messages$/.test(p)) doc = {message: {seq: 2}, delivered: true};
    return {ok: true, status: 200, json: async () => doc};
  };
  const ctx = vm.createContext({
    document, console, JSON, Date,
    setInterval: () => 1, clearInterval() {}, setTimeout: () => 0,
    localStorage: {getItem: () => null, setItem() {}},
  });
  for (const f of ["threads-pane.js", "qa-pane.js", "talk-pane.js"]) vm.runInContext(read(f), ctx);
  return {ctx, api, calls};
}

(async () => {
  /* --- helpers ----------------------------------------------------------- */
  {
    const {ctx} = harness();
    const T = ctx.TalkPane;
    assert.ok(T.isMode("threads") && T.isMode("qa") && T.isMode("both") && !T.isMode("run"));
    assert.deepEqual({...T.shows("both")}, {threads: true, qa: true});
    assert.deepEqual({...T.shows("qa")}, {threads: false, qa: true});
    assert.equal(T.modeFor("qa", "threads"), "both", "a link to the other pane keeps the origin on screen");
    assert.equal(T.modeFor("both", "threads"), "both");
    assert.equal(T.modeFor("threads", "threads"), "threads", "a target already shown changes nothing");
  }

  /* --- tabs ---------------------------------------------------------------- */
  {
    const {ctx, api} = harness();
    const T = ctx.TalkPane;
    const host = new N("div");
    const asked = [];
    const opts = {mode: "qa", api, onMode: (m) => { asked.push(m); T.setMode(m); }};
    T.mount(host, "s1", opts);
    await settle();
    const tcol = host.querySelector(".talk-threads");
    const qcol = host.querySelector(".talk-qa");
    assert.ok(tcol.classList.contains("hidden") && !qcol.classList.contains("hidden"), "qa mode shows QA only");
    assert.ok(qcol.querySelector(".qa-card"), "the QA pane is mounted in its column");
    assert.equal(tcol.children.length, 0, "the threads pane is not mounted");
    const tabs = host.querySelectorAll(".talk-tab");
    assert.deepEqual(tabs.map((b) => b.dataset.mode), ["threads", "qa", "both"]);
    assert.equal(tabs[1].attrs["aria-selected"], "true");

    const qaPane = qcol.querySelector(".qa-pane");
    tabs[2].fire("click");
    await settle();
    assert.deepEqual(asked, ["both"], "a tab asks the page");
    assert.ok(!tcol.classList.contains("hidden") && !qcol.classList.contains("hidden"), "both: two columns");
    assert.equal(host.querySelector(".talk-body").dataset.mode, "both");
    assert.equal(qcol.querySelector(".qa-pane"), qaPane, "the QA pane that stays is not rebuilt");
    assert.ok(tcol.querySelector(".th-item"), "the threads pane is mounted");

    tabs[0].fire("click");
    await settle();
    assert.equal(qcol.children.length, 0, "threads mode unmounts the QA pane");
    T.unmount();
    assert.equal(host.children.length, 0);
  }

  /* --- links: from a QA card --------------------------------------------- */
  {
    const {ctx, api, calls} = harness();
    const T = ctx.TalkPane;
    const host = new N("div");
    const asked = [];
    T.mount(host, "s1", {mode: "qa", api, onMode: (m) => { asked.push(m); T.setMode(m); }});
    await settle();
    const cards = host.querySelectorAll(".qa-card");
    const chips = cards[0].querySelectorAll(".qa-link-chip");
    assert.equal(chips.length, 1);
    assert.ok(chips[0].textContent.includes("논의 쓰레드") && chips[0].textContent.includes("th-1"));
    assert.ok(cards[1].querySelector(".qa-link-chip").textContent.includes("출처 쓰레드"), "a filed question names its source");

    // th-2 is not the thread the pane would show by itself (th-1 is).
    cards[1].querySelector(".qa-link-chip").fire("click");
    await settle();
    assert.deepEqual(asked, ["both"], "following a thread link from QA moves to both");
    const on = host.querySelectorAll(".th-item").find((i) => i.classList.contains("on"));
    assert.equal(on && on.dataset.id, "th-2", "the linked thread is selected");

    // 쓰레드로 논의: the new-thread form about the question, created with {qa}
    host.querySelectorAll(".qa-card")[0].querySelector(".qa-discuss").fire("click");
    await settle();
    const form = host.querySelector(".th-newform");
    assert.ok(form, "the new-thread form opens");
    assert.equal(form.querySelector(".th-title-input").value, "qa-1: Which base?");
    assert.ok(form.querySelector(".th-about").textContent.includes("qa-1"), "the form names the question");
    const start = form.querySelectorAll(".term-btn").find((b) => b.textContent === "시작");
    start.fire("click");
    await settle();
    const created = calls.find((c) => c[0] === "POST" && c[1] === "/api/threads");
    assert.deepEqual(created[2], {to: "s1", title: "qa-1: Which base?", qa: "qa-1"});
    // A closed question can be taken up again in a thread (claunch-236ei).
    const settled = host.querySelectorAll(".qa-card").find((c) => c.dataset.id === "qa-3");
    assert.ok(settled && settled.querySelector(".qa-discuss"), "a closed question offers 쓰레드로 논의");
  }

  /* --- links: from a thread ----------------------------------------------- */
  {
    const {ctx, api, calls} = harness();
    const T = ctx.TalkPane;
    const host = new N("div");
    const asked = [];
    T.mount(host, "s1", {mode: "threads", api, onMode: (m) => { asked.push(m); T.setMode(m); }});
    await settle();
    const item = host.querySelectorAll(".th-item")[0];
    assert.ok(item.querySelector(".th-chip-qa"), "a thread with links carries a QA chip in the list");
    const chip = host.querySelector(".th-links .th-link-chip");
    assert.ok(chip.textContent.includes("다루는 질문") && chip.textContent.includes("qa-1"));
    chip.fire("click");
    await settle();
    assert.deepEqual(asked, ["both"], "following a question link from threads moves to both");
    const card = host.querySelectorAll(".qa-card").find((c) => c.dataset.id === "qa-1");
    assert.ok(card.scrolled && card.classList.contains("qa-flash"), "the linked card is brought in and marked");

    // QA로 정리 요청: posts ask_qa with the composer's text, or a default
    host.querySelector(".th-ask-qa").fire("click");
    await settle();
    const ask = calls.find((c) => c[0] === "POST" && c[1] === "/api/threads/th-1/messages");
    assert.equal(ask[2].ask_qa, true);
    assert.ok(ask[2].text.includes("QA 질문"), "an empty composer sends the default request");
  }

  /* --- wiring -------------------------------------------------------------- */
  {
    const app = read("app.js");
    const html = read("index.html");
    const src = read("talk-pane.js");
    const at = (s) => html.indexOf(`src="static/${s}"`);
    assert.ok(at("talk-pane.js") > at("threads-pane.js") && at("talk-pane.js") > at("qa-pane.js"));
    assert.ok(at("talk-pane.js") < at("app.js"));
    assert.ok(/isTalkPane\(kind\)\)[\s\S]{0,400}TalkPane\?\.mount\(/.test(app), "openSplit mounts TalkPane");
    assert.ok(/isTalkPane\(splitKind\) && isTalkPane\(kind\)[\s\S]{0,120}TalkPane\?\.setMode\(kind\)/.test(app),
              "a tab change inside the pane does not reopen it");
    assert.ok(app.includes('onMode: (m) => setSessLayout(name, { split: true, pane: m })'));
    assert.ok(app.includes('const qaOn = !!lay && lay.split && talkShows(lay.pane, "qa");'));
    assert.ok(app.includes('const threadsOn = !!lay && lay.split && talkShows(lay.pane, "threads");'));
    assert.ok(!/innerHTML/.test(src), "talk-pane.js never assigns HTML");
    // A header button that reads as on turns its pane off: in "both" only
    // its column goes, alone the split closes; an off button opens its pane.
    const start = app.indexOf("function splitAfterPress(");
    let end = app.indexOf("{", start);
    for (let depth = 0; ; end++) {
      if (app[end] === "{") depth++;
      else if (app[end] === "}" && !--depth) break;
    }
    const press = vm.runInNewContext(`(${app.slice(start, end + 1)})`);
    const on = (pane) => ({split: true, pane});
    assert.deepEqual({...press(on("both"), "qa")}, {split: true, pane: "threads"});
    assert.deepEqual({...press(on("both"), "threads")}, {split: true, pane: "qa"});
    assert.deepEqual({...press(on("qa"), "qa")}, {split: false, max: false});
    assert.deepEqual({...press(on("threads"), "threads")}, {split: false, max: false});
    assert.deepEqual({...press(on("threads"), "qa")}, {split: true, pane: "qa"});
    assert.deepEqual({...press(on("both"), "chart")}, {split: true, pane: "chart"});
    assert.deepEqual({...press({split: false, pane: "both"}, "qa")}, {split: true, pane: "qa"});
    assert.ok(app.includes("setSessLayout(currentName, splitAfterPress(sessLayoutFor(currentName), pane));"));
    assert.ok(app.includes('"hide the questions column — the threads keep the pane"'));
    assert.ok(app.includes('"hide the threads column — the questions keep the pane"'));
  }

  /* --- controls: × closes, ⤢/⤡ enlarges and shrinks (claunch-qkvb1) ------- */
  {
    const {ctx, api} = harness();
    const T = ctx.TalkPane;
    const host = new N("div");
    const asked = [];
    T.mount(host, "s1", {
      mode: "threads", api, max: false,
      onClose: () => asked.push("close"),
      onMax: (on) => { asked.push(on ? "max" : "min"); T.setMax(on); },
    });
    await settle();
    const ctl = host.querySelector(".talk-tabs .talk-ctl");
    assert.ok(ctl, "the controls sit in the tab bar");
    const big = ctl.querySelector(".talk-max");
    const shut = ctl.querySelector(".talk-close");
    assert.equal(big.textContent, "⤢");
    assert.equal(big.attrs["aria-pressed"], "false");
    big.fire("click");
    assert.deepEqual(asked, ["max"], "⤢ asks the page to enlarge");
    assert.equal(big.textContent, "⤡", "enlarged, the button shrinks");
    assert.equal(big.attrs["aria-pressed"], "true");
    assert.equal(T.max(), true);
    big.fire("click");
    assert.deepEqual(asked, ["max", "min"]);
    assert.equal(big.textContent, "⤢");
    shut.fire("click");
    assert.deepEqual(asked, ["max", "min", "close"], "× asks the page to close");
    // A tab switch keeps the controls and their state.
    T.setMax(true);
    T.setMode("both");
    assert.equal(host.querySelector(".talk-max").textContent, "⤡");
    T.unmount();

    const app = read("app.js");
    const css = read("style.css");
    assert.ok(app.includes("onClose: () => setSessLayout(name, { split: false, max: false }),"));
    assert.ok(app.includes("onMax: (on) => setSessLayout(name, { max: !!on }),"));
    assert.ok(app.includes("const max = up && isTalkPane(lay.pane) && lay.max;"),
              "only an open conversation pane enlarges");
    assert.ok(app.includes('document.body.classList.toggle("split-max", max);'));
    assert.ok(/body\.split-max #terminal[^{]*\{ display: none; \}/.test(css), "enlarged, the terminal steps aside");
  }

  /* --- a run read in flight does not paint over the conversation pane ------
     The run pane's refreshSplit awaits the meta endpoint and the run; if the
     same session's pane moves to its threads meanwhile, splitFor is still that
     session, and the late reply used to render the run into #term-wf under a
     lit 🗪 threads button (claunch-qkvb1). */
  {
    const app = read("app.js");
    const fn = (name) => {
      const start = app.search(new RegExp(`^(async )?function ${name}\\(`, "m"));
      assert.ok(start >= 0, name);
      let end = app.indexOf("{", app.indexOf(")", start));
      for (let depth = 0; ; end++) {
        if (app[end] === "{") depth++;
        else if (app[end] === "}" && !--depth) break;
      }
      return app.slice(start, end + 1);
    };
    const wf = new N("div");
    wf.innerHTML = "";
    const painted = [];
    const pending = [];
    const deferred = () => {
      let resolve;
      const p = new Promise((r) => { resolve = r; });
      pending.push(resolve);
      return p;
    };
    const ctx = {
      $: (id) => (id === "term-wf" ? wf : new N("div")),
      el: (tag, cls, text) => { const n = new N(tag); n.className = cls || ""; if (text) n.textContent = text; return n; },
      api: () => deferred(),
      fetchCflow2Run: () => Promise.resolve(null),
      renderSplit: () => painted.push("run v1"),
      renderCflow2Into: () => painted.push("run v2"),
      visiblePoll: (f) => f,
      setInterval: () => 1, clearInterval: () => {},
      isTalkPane: (k) => k === "threads" || k === "qa" || k === "both",
      threadsReadKey: () => "k",
      sessLayoutFor: () => ({max: false}),
      setSessLayout: () => {}, refreshQaRail: () => {}, refreshThreadsRail: () => {},
      globalThis: {TalkPane: {mount: () => painted.push("talk"), unmount: () => {}, setMode: () => {}}},
    };
    vm.createContext(ctx);
    vm.runInContext(`
      let splitFor = null, splitKind = null, splitCwd = null, splitScope = null;
      let splitPollTimer = null, splitSelectedStep = null, splitLastData = null;
      let splitDurable = null, splitGen = 0;
      ${fn("openSplit")}
      ${fn("closeSplit")}
      ${fn("refreshSplit")}
      this.openSplit = openSplit;
      this.state = () => ({splitFor, splitKind});
    `, ctx);
    ctx.openSplit("s1", "run");             // starts a meta read
    assert.equal(pending.length, 1);
    ctx.openSplit("s1", "threads");         // same session, the pane moves on
    assert.deepEqual(painted, ["talk"]);
    pending[0]({ok: true, json: async () => ({cflow: {cwd: "/w", scope: "s1"}})});
    await settle();
    // Had the stale read gone on, it would have asked for the run next.
    assert.equal(pending.length, 1, "the stale read stops after the meta reply");
    assert.deepEqual(painted, ["talk"], "the run is not painted over the threads pane");
    assert.deepEqual({...ctx.state()}, {splitFor: "s1", splitKind: "threads"});

    // And the same after the run read: back on the run, then away again.
    ctx.openSplit("s1", "run");
    pending[1]({ok: true, json: async () => ({cflow: {cwd: "/w", scope: "s1"}})});
    await settle();
    assert.equal(pending.length, 3, "a live read goes on to the run");
    ctx.openSplit("s1", "qa");
    pending[2]({ok: true, json: async () => ({status: "running"})});
    await settle();
    assert.deepEqual(painted, ["talk", "talk"], "a run reply that lands after the switch is dropped");
  }
  console.log("talk pane checks passed");
})().catch((e) => { console.error(e); process.exitCode = 1; });
