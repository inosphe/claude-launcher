/* Session QA pane (qa-pane.js) and the header's auto-open rule (app.js
   maybeOpenQaPane), claunch-l8vyi: an answer leaves only on an explicit save
   and names the revision it was written against; a 409 keeps the draft and
   shows the new question; a poll never rebuilds the card being typed in;
   the pane opens when the session changes a question and stays shut after
   the reader closes it until the next change. */
const assert = require("assert/strict");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const STATIC = path.join(__dirname, "../../src/claude_launcher/web/static");

class N {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.className = "";
    this._text = "";
    this.dataset = {};
    this.handlers = {};
    this.value = "";
    this.checked = false;
    const self = this;
    this.classList = {
      contains: (c) => self.className.split(/\s+/).includes(c),
      toggle(c, on) {
        const set = new Set(self.className.split(/\s+/).filter(Boolean));
        if (on === undefined) on = !set.has(c);
        if (on) set.add(c); else set.delete(c);
        self.className = [...set].join(" ");
      },
      add(c) { this.toggle(c, true); },
      remove(c) { this.toggle(c, false); },
    };
  }
  set textContent(t) { this.children = []; this._text = String(t); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
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
    const i = p.children.indexOf(this);
    p._adopt(n);
    p.children.splice(p.children.indexOf(this), 1, n);
    this.parentNode = null;
    void i;
  }
  remove() { if (this.parentNode) { const p = this.parentNode; p.children = p.children.filter((c) => c !== this); this.parentNode = null; } }
  contains(n) { for (let x = n; x; x = x.parentNode) if (x === this) return true; return false; }
  get firstChild() { return this.children[0] || null; }
  get nextSibling() { const p = this.parentNode; return p ? p.children[p.children.indexOf(this) + 1] || null : null; }
  addEventListener(k, fn) { (this.handlers[k] ||= []).push(fn); }
  fire(k, e = {}) { for (const fn of this.handlers[k] || []) fn({preventDefault() {}, ...e}); }
  _match(sel) { return sel.startsWith(".") ? this.classList.contains(sel.slice(1)) : this.tagName === sel.toUpperCase(); }
  querySelectorAll(sel) {
    const out = [];
    const walk = (n) => { for (const c of n.children) { if (c._match(sel)) out.push(c); walk(c); } };
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

const document = { createElement: (t) => new N(t), activeElement: null, hidden: false };
const tick = () => new Promise((r) => setImmediate(r));

function paneHarness() {
  let list = { session: "s1", unresolved: 1, agent_seq: 3, questions: [] };
  const posts = [];
  let answerReply = null;
  const api = async (url, opts = {}) => {
    if (!opts.method) return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(list)) };
    const body = JSON.parse(opts.body);
    posts.push({ url, body });
    const r = answerReply(body);
    return { ok: r.status === 200, status: r.status, json: async () => r.doc };
  };
  const ctx = vm.createContext({ document, setInterval: () => 1, clearInterval() {}, setTimeout: () => 0, console, JSON });
  vm.runInContext(fs.readFileSync(path.join(STATIC, "qa-pane.js"), "utf8"), ctx);
  return {
    pane: ctx.QaPane, api, posts,
    setList(l) { list = l; },
    reply(fn) { answerReply = fn; },
  };
}

function q(over = {}) {
  return { id: "qa-1", session: "s1", title: "Which base?", body: "", options: [{ label: "master" }, { label: "stack" }],
           multi: false, revision: 1, status: "open", answer: null, created_at: "", updated_at: "", ...over };
}

(async () => {
  /* --- answering: explicit save, revision, 409 keeps the draft ---------- */
  {
    const h = paneHarness();
    h.setList({ session: "s1", unresolved: 1, agent_seq: 3, questions: [q()] });
    const host = new N("div");
    let changed = 0;
    h.pane.mount(host, "s1", { api: h.api, onChange: () => changed++ });
    await tick();
    const card = host.querySelector(".qa-card");
    assert.ok(card, "a card per question");
    assert.ok(card.textContent.includes("Which base?"));
    assert.equal(card.querySelectorAll("input").length, 3, "one radio per option, plus the fixed rediscuss one");
    const [master] = card.querySelectorAll("input");
    master.checked = true; master.fire("change");
    const ta = card.querySelector("textarea");
    ta.value = "because"; ta.fire("input");
    assert.equal(h.posts.length, 0, "choosing and typing send nothing");
    ta.fire("keydown", { key: "Enter", shiftKey: true });
    assert.equal(h.posts.length, 0, "Shift+Enter is a new line, not a save");

    // The agent rewrote the question in the meantime: the daemon refuses.
    h.reply(() => ({ status: 409, doc: { error: "moved", current: q({ title: "Which base, really?", revision: 2 }) } }));
    ta.fire("keydown", { key: "Enter" });
    await tick();
    assert.equal(h.posts.length, 1);
    assert.deepEqual(h.posts[0].body, { revision: 1, text: "because", choices: ["master"] });
    assert.equal(h.posts[0].url, "/api/qa/qa-1/answer");
    const after = host.querySelector(".qa-card");
    assert.ok(after.textContent.includes("Which base, really?"), "the new wording is shown");
    assert.equal(after.querySelector("textarea").value, "because", "the draft survives the refusal");
    assert.ok(after.querySelector("input").checked, "so does the choice");
    assert.ok(!after.querySelector(".qa-moved").classList.contains("hidden") ||
              after.textContent.includes("질문이 바뀌어 저장하지 않았습니다"), "the refusal is said");
    assert.equal(changed, 0, "a refused save moves no light");

    // Saved again: now against the revision on screen.
    h.reply(() => ({ status: 200, doc: { delivered: true, previous: null,
      question: q({ title: "Which base, really?", revision: 3, status: "answered",
                    answer: { text: "because", choices: ["master"], revision: 3, at: "", saves: 1 } }) } }));
    after.querySelector(".qa-save").fire("click");
    await tick();
    assert.equal(h.posts[1].body.revision, 2);
    assert.ok(host.textContent.includes("세션에 전달됨"));
    assert.equal(changed, 1, "a save refreshes the header's light");
    h.pane.unmount();
  }

  /* --- the fixed rediscuss choice (claunch-rwcnh) ------------------------ */
  {
    const h = paneHarness();
    h.setList({ session: "s1", unresolved: 2, agent_seq: 3, questions: [
      q({ options: [], rediscuss: { label: "질문 재논의 (질문 취소)", description: "d" } }),
      q({ id: "qa-2", multi: true, options: [{ label: "a" }, { label: "b" }] }),
    ] });
    const host = new N("div");
    h.pane.mount(host, "s1", { api: h.api });
    await tick();
    const [plain, multi] = host.querySelectorAll(".qa-card");
    const only = plain.querySelectorAll("input");
    assert.equal(only.length, 1, "a question without options still offers the fixed choice");
    assert.ok(plain.textContent.includes("질문 재논의 (질문 취소)"));
    only[0].checked = true; only[0].fire("change");
    const ta = plain.querySelector("textarea");
    assert.ok(ta.placeholder.includes("코멘트 (필수)"), "choosing it asks for the comment");
    ta.value = "ask me later"; ta.fire("input");
    h.reply(() => ({ status: 200, doc: { delivered: true, previous: null, question: q({ options: [] }) } }));
    plain.querySelector(".qa-save").fire("click");
    await tick();
    assert.deepEqual(h.posts[0].body, { revision: 1, text: "ask me later", choices: ["질문 재논의 (질문 취소)"] });

    // On a multi question it is exclusive: it clears the boxes, and a box clears it.
    const [a, b, fixed] = multi.querySelectorAll("input");
    assert.equal(fixed.type, "radio");
    assert.equal(fixed.value, "질문 재논의 (질문 취소)", "the built-in label when the row carries none");
    a.checked = true; a.fire("change");
    b.checked = true; b.fire("change");
    fixed.checked = true; fixed.fire("change");
    assert.ok(!a.checked && !b.checked && fixed.checked);
    a.checked = true; a.fire("change");
    assert.ok(a.checked && !fixed.checked);
    h.pane.unmount();
  }

  /* --- a poll never rebuilds the card being typed in --------------------- */
  {
    const h = paneHarness();
    h.setList({ session: "s1", unresolved: 1, agent_seq: 3, questions: [q({ options: [] })] });
    const host = new N("div");
    h.pane.mount(host, "s1", { api: h.api });
    await tick();
    const card = host.querySelector(".qa-card");
    const ta = card.querySelector("textarea");
    ta.value = "half wri"; ta.fire("input");
    document.activeElement = ta;
    h.setList({ session: "s1", unresolved: 1, agent_seq: 4, questions: [q({ options: [], title: "Edited", revision: 2 })] });
    h.pane.refresh();
    await tick();
    assert.equal(host.querySelector(".qa-card"), card, "the card being typed in is left alone");
    assert.equal(ta.value, "half wri");
    assert.ok(!card.querySelector(".qa-moved").classList.contains("hidden"), "...but says the question moved");
    document.activeElement = null;
    h.pane.refresh();
    await tick();
    const fresh = host.querySelector(".qa-card");
    assert.notEqual(fresh, card, "rebuilt once the reader lets go");
    assert.ok(fresh.textContent.includes("Edited"));
    assert.equal(fresh.querySelector("textarea").value, "half wri", "with the draft");
    h.pane.unmount();
  }

  /* --- closed questions fold away; an emptied list says so --------------- */
  {
    const h = paneHarness();
    h.setList({ session: "s1", unresolved: 0, agent_seq: 5, questions: [q({ status: "closed", revision: 2 })] });
    const host = new N("div");
    h.pane.mount(host, "s1", { api: h.api });
    await tick();
    assert.ok(host.querySelector(".qa-empty"), "no live question: the empty note");
    const closed = host.querySelector(".qa-closed-list");
    assert.ok(closed.textContent.includes("닫힌 질문 1"));
    assert.equal(closed.querySelector("textarea"), null, "a closed card shows no editor until asked");
    h.pane.unmount();
  }

  /* --- delivery state: waiting behind a draft, forced, typed (claunch-564kn) */
  {
    const h = paneHarness();
    const answer = { text: "yes", choices: [], revision: 2, at: "", saves: 1 };
    const waiting = { kind: "answer", state: "pending", queued_at: "2026-10-14T07:00:00+00:00",
                      settled_at: null, forced: false, draft: true };
    h.setList({ session: "s1", unresolved: 0, agent_seq: 3,
                questions: [q({ options: [], status: "answered", revision: 2, answer, delivery: waiting })] });
    const host = new N("div");
    h.pane.mount(host, "s1", { api: h.api });
    await tick();
    const line = host.querySelector(".qa-delivery");
    assert.ok(line.textContent.includes("전달 대기 중"), "a waiting notification is said to wait");
    assert.ok(line.textContent.includes("보내지 않은 줄"), "...and why");
    const typed = { ...waiting, state: "delivered", settled_at: "2026-10-14T07:01:00+00:00", forced: true };
    delete typed.draft;
    h.reply(() => ({ status: 200, doc: { delivery: typed } }));
    h.setList({ session: "s1", unresolved: 0, agent_seq: 3,
                questions: [q({ options: [], status: "answered", revision: 2, answer, delivery: typed })] });
    host.querySelector(".qa-deliver-now").fire("click");
    await tick(); await tick();
    assert.equal(h.posts.at(-1).url, "/api/qa/qa-1/deliver");
    assert.ok(host.querySelector(".qa-delivery").textContent.includes("세션에 전달됨"));
    assert.ok(host.querySelector(".qa-delivery").textContent.includes("강제 전달"));

    // A save that is only queued says so, not "delivered".
    h.setList({ session: "s1", unresolved: 1, agent_seq: 4, questions: [q({ options: [] })] });
    h.pane.refresh();
    await tick();
    const ta = host.querySelector(".qa-card").querySelector("textarea");
    ta.value = "go"; ta.fire("input");
    h.reply(() => ({ status: 200, doc: { delivered: true, previous: null, reopened: false,
      delivery: { ...waiting, draft: false },
      question: q({ options: [], status: "answered", revision: 2, answer: { ...answer, text: "go" } }) } }));
    host.querySelector(".qa-save").fire("click");
    await tick();
    assert.ok(host.textContent.includes("저장됨 · 전달 대기 중"));
    assert.ok(!host.textContent.includes("세션에 전달됨"));
    h.pane.unmount();
  }

  /* --- closed: answer again (reopens) and the direction check ------------ */
  {
    const h = paneHarness();
    const answer = { text: "master", choices: [], revision: 2, at: "", saves: 1 };
    h.setList({ session: "s1", unresolved: 0, agent_seq: 5,
                questions: [q({ options: [], status: "closed", revision: 3, answer })] });
    const host = new N("div");
    h.pane.mount(host, "s1", { api: h.api });
    await tick();
    const closedBox = host.querySelector(".qa-closed-list");
    closedBox.querySelector(".qa-reopen").fire("click");
    const editor = host.querySelector(".qa-closed-list").querySelector("textarea");
    assert.ok(editor, "answering again opens the editor on a closed card");
    assert.equal(editor.value, "master", "starting from the saved answer");
    editor.value = "stack"; editor.fire("input");
    h.reply(() => ({ status: 200, doc: { delivered: true, reopened: true, previous: answer,
      delivery: { kind: "answer", state: "delivered", queued_at: "", settled_at: "2026-10-14T07:02:00+00:00", forced: false },
      question: q({ options: [], status: "answered", revision: 4, answer: { ...answer, text: "stack", revision: 4 } }) } }));
    host.querySelector(".qa-save").fire("click");
    await tick();
    assert.deepEqual(h.posts.at(-1).body, { revision: 3, text: "stack", choices: [] });
    assert.ok(host.querySelector(".qa-list").textContent.includes("stack"), "reopened: back among the live ones");

    // Direction check from the (now answered) card, with a note.
    host.querySelector(".qa-check-open").fire("click");
    const note = host.querySelector(".qa-check-note");
    assert.ok(note, "the check opens a note box");
    note.value = "you are still on master"; note.fire("input");
    h.reply(() => ({ status: 200, doc: { reopened: false,
      question: q({ options: [], status: "answered", revision: 4, answer: { ...answer, text: "stack", revision: 4 } }),
      check: { seq: 9, at: "2026-10-14T07:03:00+00:00", note: "you are still on master", settled: false, reply: "", settled_at: null },
      delivery: { kind: "recheck", state: "pending", queued_at: "2026-10-14T07:03:00+00:00", settled_at: null, forced: false, draft: false } } }));
    host.querySelector(".qa-check-send").fire("click");
    await tick();
    assert.deepEqual(h.posts.at(-1), { url: "/api/qa/qa-1/recheck", body: { revision: 4, note: "you are still on master" } });
    assert.ok(host.querySelector(".qa-check").textContent.includes("세션 응답 대기"));
    assert.ok(host.querySelector(".qa-delivery").textContent.includes("방향 점검 알림 전달 대기 중"));

    h.setList({ session: "s1", unresolved: 0, agent_seq: 6, questions: [q({ options: [], status: "closed", revision: 5,
      answer: { ...answer, text: "stack", revision: 4 },
      check: { seq: 9, at: "2026-10-14T07:03:00+00:00", note: "x", settled: true, reply: "moved to stack", settled_at: "y" } })] });
    h.pane.refresh();
    await tick();
    assert.ok(host.querySelector(".qa-check").textContent.includes("세션 응답: moved to stack"));
    h.pane.unmount();
  }

  /* --- 질문 요청: sent at once, questions or not; the head says how it ended */
  {
    const h = paneHarness();
    h.setList({ session: "s1", unresolved: 0, agent_seq: 5, questions: [], nudge: null });
    const host = new N("div");
    h.pane.mount(host, "s1", { api: h.api });
    await tick();
    const btn = host.querySelector(".qa-nudge");
    assert.ok(btn, "the head carries the nudge button");
    assert.equal(host.querySelector(".qa-nudge-state"), null, "no nudge yet: no state line");
    const sent = { id: "qn-1", session: "s1", status: "sent", reply: "", questions: [],
                   created_at: "2026-10-14T01:00:00+00:00", settled_at: null };
    h.reply(() => ({ status: 200, doc: { nudge: sent, delivered: true } }));
    btn.fire("click");
    await tick();
    assert.equal(h.posts.length, 1, "one press, one POST -- no confirmation, no question needed");
    assert.equal(h.posts[0].url, "/api/qa/nudge");
    assert.deepEqual(h.posts[0].body, { to: "s1" });
    assert.ok(host.querySelector(".qa-nudge-state").textContent.includes("응답 대기"));

    h.setList({ session: "s1", unresolved: 0, agent_seq: 6, questions: [],
                nudge: { ...sent, status: "nothing", reply: "all decided", settled_at: "x" } });
    h.pane.refresh();
    await tick();
    assert.ok(host.querySelector(".qa-nudge-state").textContent.includes("질문 없음: all decided"));

    h.setList({ session: "s1", unresolved: 1, agent_seq: 7, questions: [q()],
                nudge: { ...sent, id: "qn-2", status: "asked", questions: ["qa-1"], settled_at: "x" } });
    h.pane.refresh();
    await tick();
    assert.ok(host.querySelector(".qa-nudge-state").textContent.includes("질문으로 응답: qa-1"));

    h.reply(() => ({ status: 200, doc: { nudge: { ...sent, id: "qn-3" }, delivered: false } }));
    host.querySelector(".qa-nudge").fire("click");
    await tick();
    assert.ok(host.querySelector(".qa-nudge-state").textContent.includes("전달하지 못함"), "a gone session is said");
    h.pane.unmount();
  }

  /* --- reveal: a notice names the session and the question (claunch-x76l8) */
  {
    N.prototype.closest = function () { return null; };
    N.prototype.scrollIntoView = function () { this.scrolled = true; };
    const h = paneHarness();
    h.setList({ session: "s1", unresolved: 2, agent_seq: 3,
                questions: [q(), q({ id: "qa-2", title: "Which port?" })] });
    const shown = (host, id) => host.querySelectorAll(".qa-card")
      .find((c) => c.scrolled && c.textContent.includes(id === "qa-2" ? "Which port?" : "Which base?"));
    h.pane.reveal("s1", "qa-2");
    const other = new N("div");
    h.pane.mount(other, "s2", { api: h.api });
    await tick();
    assert.equal(shown(other, "qa-2"), undefined, "a mount on another session does not take the request");
    h.pane.unmount();
    const host = new N("div");
    h.pane.mount(host, "s1", { api: h.api });
    await tick();
    assert.ok(shown(host, "qa-2"), "the next mount on that session brings the question into view");
    assert.equal(shown(host, "qa-1"), undefined, "and only that one");
    h.pane.reveal("s1", "qa-1");
    await tick();
    assert.ok(shown(host, "qa-1"), "on the session already mounted it is brought in at once");
    h.pane.unmount();
    delete N.prototype.closest;
    delete N.prototype.scrollIntoView;
  }

  /* --- app.js: the pane opens itself on the session's change ------------- */
  {
    const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
    const store = new Map();
    const layouts = {};
    const ctx = vm.createContext({
      BASE: "/", currentPage: "terminal", currentName: "s1",
      localStorage: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)) },
      sessLayoutFor: (n) => ({ split: false, pane: "run", ...(layouts[n] || {}) }),
      setSessLayout: (n, p) => { layouts[n] = { ...(layouts[n] || {}), ...p }; },
      JSON,
    });
    vm.runInContext(src.slice(src.indexOf("let qaRailCache ="), src.indexOf("async function refreshQaRail(")), ctx);
    const rail = (rows) => vm.runInContext(`qaRailCache = ${JSON.stringify(rows)}`, ctx);

    rail({ s1: { unresolved: 0, total: 2, agent_seq: 7 } });
    ctx.maybeOpenQaPane();
    assert.equal(layouts.s1, undefined, "first sight of a settled history does not open the pane");
    rail({ s1: { unresolved: 1, total: 3, agent_seq: 8 } });
    ctx.maybeOpenQaPane();
    assert.deepEqual(layouts.s1, { split: true, pane: "qa" }, "the session's change opens it");
    layouts.s1 = { split: false };   // the reader closes it
    ctx.maybeOpenQaPane();
    assert.deepEqual(layouts.s1, { split: false }, "closed by hand stays closed");
    rail({ s1: { unresolved: 1, total: 3, agent_seq: 9 } });
    ctx.maybeOpenQaPane();
    assert.deepEqual(layouts.s1, { split: true, pane: "qa" }, "...until the session changes a question again");

    rail({ s2: { unresolved: 2, total: 2, agent_seq: 3 } });
    ctx.currentName = "s2";
    ctx.maybeOpenQaPane();
    assert.deepEqual(layouts.s2, { split: true, pane: "qa" }, "first sight with questions waiting opens it");
    ctx.currentPage = "home"; layouts.s2 = { split: false };
    rail({ s2: { unresolved: 2, total: 2, agent_seq: 4 } });
    ctx.maybeOpenQaPane();
    assert.deepEqual(layouts.s2, { split: false }, "only the terminal on screen opens");
  }

  console.log("qa pane checks passed");
})().catch((e) => { console.error(e); process.exitCode = 1; });
