/* Session QA in the terminal's split pane (? qa) -- daemon/qa_api.py,
   claunch-l8vyi. The session asks through the qa_* MCP tools; the user
   answers here, and an answer is sent only on an explicit save (the Save
   button, or Enter in the text box -- Shift+Enter is a new line).

   Every save names the question revision the reader started from. If the
   agent changed the question meanwhile the daemon refuses with 409 and the
   current question; the pane then shows the new wording with the draft kept
   as it was, and the next save goes through against the revision now shown.

   The pane polls on its own (3s) and never rebuilds a card the reader is
   typing in. All user and agent text goes through textContent (or the
   page's markdown renderer, which builds nodes). */
globalThis.QaPane = (() => {
  const POLL_MS = 3000;
  const STATUS = {
    open: ["미응답", "qa-open"],
    answered: ["답함", "qa-answered"],
    stale: ["재확인 필요", "qa-stale"],
    closed: ["닫힘", "qa-closed"],
  };
  let host = null, name = "", api = null, onChange = null;
  let timer = null, seq = 0, data = null, error = "";
  // Per question id: {text, choices: [labels], base: revision the edit
  // started from, notice, conflict}. Outlives redraws and repoints within
  // a page load; dropped on a successful save.
  const drafts = new Map();
  const notices = new Map();
  const cards = new Map();   // id -> {node, sig}

  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  function markdown(target, text) {
    if (typeof globalThis.mdInto === "function") globalThis.mdInto(target, text);
    else target.textContent = text;
    return target;
  }

  async function load() {
    const mine = ++seq;
    const want = name;
    try {
      const resp = await api(`/api/qa?session=${encodeURIComponent(want)}`);
      const doc = await resp.json().catch(() => ({}));
      if (mine !== seq || want !== name) return;
      if (!resp.ok) { error = doc.error || `HTTP ${resp.status}`; render(); return; }
      error = "";
      data = doc;
      render();
    } catch {
      // offline: the page's health poll says so; keep the last picture
    }
  }

  function draftOf(q) {
    let d = drafts.get(q.id);
    if (!d) {
      d = {
        text: q.answer ? q.answer.text : "",
        choices: q.answer ? [...q.answer.choices] : [],
        base: q.revision,
        touched: false,
      };
      drafts.set(q.id, d);
    }
    return d;
  }

  function cardSig(q) {
    const d = drafts.get(q.id);
    return JSON.stringify([q, d && {c: d.choices, b: d.base, t: d.touched}, notices.get(q.id) || null]);
  }

  function typingIn(n) {
    const a = document.activeElement;
    return !!a && n.contains(a) && (a.tagName === "TEXTAREA" || a.tagName === "INPUT");
  }

  function render() {
    if (!host) return;
    let root = host.querySelector(".qa-pane");
    if (!root) {
      root = node("div", "qa-pane");
      root.append(node("div", "qa-head"), node("div", "qa-list"), node("details", "qa-closed-list"));
      host.replaceChildren(root);
    }
    const head = root.querySelector(".qa-head");
    const list = root.querySelector(".qa-list");
    const closedBox = root.querySelector(".qa-closed-list");
    const qs = (data && data.questions) || [];
    const live = qs.filter((q) => q.status !== "closed");
    const closed = qs.filter((q) => q.status === "closed");
    head.replaceChildren(
      node("h2", null, `${name} — 질문`),
      node("span", "qa-count",
           data ? `미해결 ${data.unresolved} · 전체 ${live.length}` : "불러오는 중…"),
    );
    if (error) head.append(node("p", "wf-warning", error));

    const keep = new Set(qs.map((q) => q.id));
    for (const [id, c] of cards) if (!keep.has(id)) { c.node.remove(); cards.delete(id); }
    for (const id of [...drafts.keys()]) if (!keep.has(id)) drafts.delete(id);

    if (!live.length && data) {
      if (!list.querySelector(".qa-empty")) {
        list.replaceChildren(node("p", "wf-note qa-empty",
          "이 세션이 남긴 질문이 없습니다. 세션은 qa_ask 도구로 여기에 질문을 올립니다."));
      }
    } else {
      list.querySelector(".qa-empty")?.remove();
    }
    place(list, live);
    closedBox.classList.toggle("hidden", !closed.length);
    if (closed.length) {
      let sum = closedBox.querySelector("summary");
      if (!sum) { sum = node("summary"); closedBox.prepend(sum); }
      sum.textContent = `닫힌 질문 ${closed.length}`;
      let box = closedBox.querySelector(".qa-closed-body");
      if (!box) { box = node("div", "qa-closed-body"); closedBox.append(box); }
      place(box, closed);
    }
  }

  /* Cards in question order, each rebuilt only when what it shows changed
     and nobody is typing in it -- a poll must not eat a half-written answer. */
  function place(parent, qs) {
    let prev = null;
    for (const q of qs) {
      let c = cards.get(q.id);
      const sig = cardSig(q);
      if (!c) { c = {node: null, sig: null}; cards.set(q.id, c); }
      if (c.sig !== sig && !(c.node && typingIn(c.node))) {
        const fresh = card(q);
        if (c.node) c.node.replaceWith(fresh);
        c.node = fresh;
        c.sig = sig;
      } else if (c.node && c.sig !== sig) {
        // Typing: leave the inputs alone, but say the question moved.
        const d = drafts.get(q.id);
        c.node.querySelector(".qa-moved")?.classList.toggle("hidden", !(d && d.base < q.revision));
      }
      const want = prev ? prev.nextSibling : parent.firstChild;
      if (c.node.parentNode !== parent || c.node !== want) parent.insertBefore(c.node, want);
      prev = c.node;
    }
  }

  function card(q) {
    const [label, cls] = STATUS[q.status] || [q.status, ""];
    const box = node("article", `qa-card ${cls}`);
    box.dataset.id = q.id;
    const top = node("div", "qa-card-head");
    top.append(node("span", `qa-status ${cls}`, label), node("strong", "qa-title", q.title),
               node("span", "qa-meta", `${q.id} · r${q.revision}`));
    box.append(top);
    if (q.body) box.append(markdown(node("div", "qa-body"), q.body));

    const d = drafts.get(q.id);
    const moved = node("p", "qa-moved wf-warning",
      "답을 쓰는 동안 질문이 바뀌었습니다. 위의 현재 질문을 확인하고 다시 저장하세요.");
    moved.classList.toggle("hidden", !(d && d.base < q.revision));
    box.append(moved);

    if (q.answer) {
      const a = node("div", "qa-answer");
      a.append(node("span", "qa-answer-label",
        q.status === "stale" ? `이전 답 (r${q.answer.revision}, 질문이 그 뒤 바뀜)` : `저장된 답 (r${q.answer.revision})`));
      if (q.answer.choices.length) a.append(node("div", "qa-answer-choices", q.answer.choices.join(" · ")));
      if (q.answer.text) a.append(node("div", "qa-answer-text", q.answer.text));
      box.append(a);
    }
    if (q.status === "closed") return box;

    const cur = d || {text: q.answer ? q.answer.text : "", choices: q.answer ? q.answer.choices : []};
    if (q.options.length) {
      const opts = node("div", "qa-options");
      const group = `qa-${q.id}`;
      for (const o of q.options) {
        const row = node("label", "qa-option");
        const input = node("input");
        input.type = q.multi ? "checkbox" : "radio";
        input.name = group;
        input.value = o.label;
        input.checked = cur.choices.includes(o.label);
        input.addEventListener("change", () => {
          const dd = draftOf(q);
          dd.touched = true;
          if (q.multi) {
            dd.choices = input.checked ? [...new Set([...dd.choices, o.label])]
                                       : dd.choices.filter((c) => c !== o.label);
          } else {
            dd.choices = [o.label];
          }
        });
        const txt = node("span", "qa-option-text");
        txt.append(node("span", "qa-option-label", o.label));
        if (o.description) txt.append(node("span", "qa-option-desc", o.description));
        row.append(input, txt);
        opts.append(row);
      }
      if (!q.multi) {
        const clear = node("button", "term-btn qa-clear", "선택 해제");
        clear.type = "button";
        clear.addEventListener("click", () => {
          const dd = draftOf(q);
          dd.touched = true;
          dd.choices = [];
          for (const i of opts.querySelectorAll("input")) i.checked = false;
        });
        opts.append(clear);
      }
      box.append(opts);
    }
    const ta = node("textarea", "qa-text");
    ta.rows = 3;
    ta.placeholder = q.options.length ? "추가 설명 (선택) — Enter 저장, Shift+Enter 줄바꿈"
                                      : "답변 — Enter 저장, Shift+Enter 줄바꿈";
    ta.value = cur.text;
    ta.addEventListener("input", () => { const dd = draftOf(q); dd.touched = true; dd.text = ta.value; });
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); save(q.id); }
    });
    const actions = node("div", "qa-actions");
    const btn = node("button", "term-btn qa-save", q.answer ? "답 수정 저장" : "저장");
    btn.type = "button";
    btn.addEventListener("click", () => save(q.id));
    actions.append(btn);
    const n = notices.get(q.id);
    if (n) actions.append(node("span", `qa-notice ${n.bad ? "wf-warning" : ""}`, n.text));
    if (d && d.touched) {
      const reset = node("button", "term-btn qa-discard", "초안 버리기");
      reset.type = "button";
      reset.addEventListener("click", () => { drafts.delete(q.id); notices.delete(q.id); render(); });
      actions.append(reset);
    }
    box.append(ta, actions);
    return box;
  }

  async function save(id) {
    const q = ((data && data.questions) || []).find((x) => x.id === id);
    if (!q) return;
    const d = draftOf(q);
    const want = name;
    let resp, doc;
    try {
      resp = await api(`/api/qa/${encodeURIComponent(id)}/answer`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({revision: d.base, text: d.text, choices: d.choices}),
      });
      doc = await resp.json().catch(() => ({}));
    } catch {
      notices.set(id, {text: "저장 실패: 데몬에 연결할 수 없습니다", bad: true});
      render();
      return;
    }
    if (want !== name) return;
    if (resp.status === 409 && doc.current) {
      // The question moved under the draft: show it, keep the draft, and
      // let the next save stand on the revision now in view.
      d.base = doc.current.revision;
      d.touched = true;
      notices.set(id, {text: "질문이 바뀌어 저장하지 않았습니다. 현재 질문을 확인한 뒤 다시 저장하세요.", bad: true});
      replace(doc.current);
      render();
      return;
    }
    if (resp.status === 404) {
      drafts.delete(id);
      notices.delete(id);
      load();
      return;
    }
    if (!resp.ok) {
      notices.set(id, {text: doc.error || `HTTP ${resp.status}`, bad: true});
      render();
      return;
    }
    drafts.delete(id);
    notices.set(id, {text: doc.delivered ? "저장됨 · 세션에 전달됨"
                                         : "저장됨 · 세션이 종료되어 전달하지 못함", bad: !doc.delivered});
    replace(doc.question);
    render();
    if (onChange) onChange();
  }

  function replace(q) {
    if (!data || !q) return;
    data.questions = data.questions.map((x) => (x.id === q.id ? q : x));
    data.unresolved = data.questions.filter((x) => x.status === "open" || x.status === "stale").length;
  }

  function mount(target, session, opts = {}) {
    if (host === target && name === session && target.querySelector(".qa-pane")) return;
    unmount();
    host = target;
    name = session;
    api = opts.api || globalThis.api;
    onChange = opts.onChange || null;
    data = null;
    error = "";
    cards.clear();
    target.replaceChildren();
    render();
    load();
    timer = setInterval(() => { if (!document.hidden) load(); }, POLL_MS);
  }

  function unmount() {
    if (timer) clearInterval(timer);
    timer = null;
    seq++;
    cards.clear();
    if (host) host.replaceChildren();
    host = null;
    name = "";
    data = null;
  }

  return {mount, unmount, refresh: () => { if (host) load(); }};
})();
