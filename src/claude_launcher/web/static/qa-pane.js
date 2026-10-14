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
   page's markdown renderer, which builds nodes).

   "질문 요청" in the head nudges the session (POST /api/qa/nudge,
   claunch-l8vyi.1): it is told to file its questions with qa_ask or to
   reply qa_nothing. It is sent at once, questions or not; the line under
   the button shows how the last nudge was settled.

   Delivery is shown as it happened (claunch-564kn): the daemon types a
   notification only once the session's terminal is free, so each card says
   whether its latest one is waiting (and whether an unsent line in the
   session's composer is what holds it), offers "지금 전달" to force it, and
   says when it was typed. A closed question can still be answered again
   ("답 다시 고치기"), and an answered or closed one can be sent a direction
   check ("방향 점검") with a note; the session settles that by closing the
   question with a note, shown under the card.

   Thread links (claunch-78dpm, daemon/talk_links.py): each question carries
   `links` to the threads that touch it (the one it was filed from, the ones
   opened about it). They are chips on the card calling opts.onThread(id);
   "쓰레드로 논의" calls opts.onDiscuss(question) to open a new thread about
   it (talk-pane.js). focus(id) scrolls a card into view and marks it. */
globalThis.QaPane = (() => {
  const POLL_MS = 3000;
  const STATUS = {
    open: ["미응답", "qa-open"],
    answered: ["답함", "qa-answered"],
    stale: ["재확인 필요", "qa-stale"],
    closed: ["닫힘", "qa-closed"],
  };
  let host = null, name = "", api = null, onChange = null, onThread = null, onDiscuss = null;
  let focusWant = "";        // a card to bring into view once it is drawn
  let timer = null, seq = 0, data = null, error = "";
  let nudging = false, nudgeNotice = null;   // {text, bad} after a press
  // Per question id: {text, choices: [labels], base: revision the edit
  // started from, notice, conflict}. Outlives redraws and repoints within
  // a page load; dropped on a successful save.
  const drafts = new Map();
  const notices = new Map();
  const cards = new Map();   // id -> {node, sig}
  const editing = new Set();   // closed question ids whose answer editor is open
  const checking = new Map();  // id -> direction-check note being written

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
    return JSON.stringify([q, d && {c: d.choices, b: d.base, t: d.touched}, notices.get(q.id) || null,
                           editing.has(q.id), checking.has(q.id)]);
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
    const ask = node("button", "term-btn qa-nudge", nudging ? "보내는 중…" : "질문 요청");
    ask.type = "button";
    ask.disabled = nudging;
    ask.title = "세션에게 QA 도구로 질문을 올리라고 요청합니다. 질문이 없으면 세션은 '질문 없음'으로 답합니다.";
    ask.addEventListener("click", nudge);
    head.replaceChildren(
      node("h2", null, `${name} — 질문`),
      node("span", "qa-count",
           data ? `미해결 ${data.unresolved} · 전체 ${live.length}` : "불러오는 중…"),
      ask,
    );
    const state = nudgeState(data && data.nudge);
    if (state || nudgeNotice) {
      const line = node("p", "qa-nudge-state");
      if (state) line.append(node("span", `qa-nudge-${data.nudge.status}`, state));
      if (nudgeNotice) line.append(node("span", `qa-notice ${nudgeNotice.bad ? "wf-warning" : ""}`, nudgeNotice.text));
      head.append(line);
    }
    if (error) head.append(node("p", "wf-warning", error));

    const keep = new Set(qs.map((q) => q.id));
    for (const [id, c] of cards) if (!keep.has(id)) { c.node.remove(); cards.delete(id); }
    for (const id of [...drafts.keys()]) if (!keep.has(id)) drafts.delete(id);

    if (!live.length && data) {
      if (!list.querySelector(".qa-empty")) {
        list.replaceChildren(node("p", "wf-note qa-empty",
          "이 세션이 남긴 질문이 없습니다. 세션은 qa_ask 도구로 여기에 질문을 올립니다. " +
          "'질문 요청'을 누르면 세션에게 질문할 것이 있는지 묻습니다."));
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
    if (focusWant) bringIntoView();
  }

  function bringIntoView() {
    const c = cards.get(focusWant);
    if (!c || !c.node) return;
    focusWant = "";
    const details = c.node.closest("details");
    if (details) details.open = true;
    c.node.scrollIntoView({block: "nearest", behavior: "smooth"});
    c.node.classList.add("qa-flash");
    setTimeout(() => c.node.classList.remove("qa-flash"), 1600);
  }

  /* Bring question `id` into view (a link from a thread). A card not drawn
     yet is brought in after the next load. */
  function focus(id) {
    if (!host || !id) return;
    focusWant = id;
    if (cards.has(id)) bringIntoView();
    else load();
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

  function linkRow(q) {
    const row = node("div", "qa-links");
    for (const link of q.links || []) {
      const label = (link.rel === "source" ? "출처 쓰레드 · " : "논의 쓰레드 · ") +
        `${link.id} ${link.status === "deleted" ? "(삭제됨)" : link.title}`;
      const chip = node("button", `qa-link-chip th-st-${link.status}`, label);
      chip.type = "button";
      chip.disabled = link.status === "deleted" || !onThread;
      chip.title = link.status === "closed" ? "닫힌 쓰레드로 이동" : "쓰레드로 이동";
      chip.addEventListener("click", () => onThread && onThread(link.id));
      row.append(chip);
    }
    if (onDiscuss && q.status !== "closed") {
      const go = node("button", "term-btn qa-discuss", "쓰레드로 논의");
      go.type = "button";
      go.title = "이 질문을 주제로 새 쓰레드를 엽니다. 쓰레드와 질문이 서로 연결됩니다.";
      go.addEventListener("click", () => onDiscuss({id: q.id, title: q.title}));
      row.append(go);
    }
    return row.firstChild ? row : null;
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
    const links = linkRow(q);
    if (links) box.append(links);

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
    const sent = deliveryLine(q);
    if (sent) box.append(sent);
    if (q.check) box.append(checkLine(q.check));
    if (q.status === "closed" && !editing.has(q.id)) {
      const actions = node("div", "qa-actions");
      const edit = node("button", "term-btn qa-reopen", "답 다시 고치기");
      edit.type = "button";
      edit.title = "닫힌 질문의 답을 고쳐 저장하면 질문이 다시 열리고 세션에 새 답이 전달됩니다.";
      edit.addEventListener("click", () => { editing.add(q.id); render(); });
      actions.append(edit);
      if (q.answer) actions.append(checkButton(q));
      const n = notices.get(q.id);
      if (n) actions.append(node("span", `qa-notice ${n.bad ? "wf-warning" : ""}`, n.text));
      box.append(actions);
      if (checking.has(q.id)) box.append(checkForm(q));
      return box;
    }

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
    const closed = q.status === "closed";
    const btn = node("button", "term-btn qa-save",
                     closed ? "답 수정 저장 (질문 다시 열기)" : q.answer ? "답 수정 저장" : "저장");
    btn.type = "button";
    btn.addEventListener("click", () => save(q.id));
    actions.append(btn);
    if (q.status === "answered") actions.append(checkButton(q));
    const n = notices.get(q.id);
    if (n) actions.append(node("span", `qa-notice ${n.bad ? "wf-warning" : ""}`, n.text));
    if ((d && d.touched) || closed) {
      const reset = node("button", "term-btn qa-discard", closed ? "취소" : "초안 버리기");
      reset.type = "button";
      reset.addEventListener("click", () => {
        drafts.delete(q.id); notices.delete(q.id); editing.delete(q.id); render();
      });
      actions.append(reset);
    }
    box.append(ta, actions);
    if (checking.has(q.id)) box.append(checkForm(q));
    return box;
  }

  const DELIVERY_WHAT = {answer: "답", recheck: "방향 점검", nudge: "질문 요청"};

  /* The card's latest notification: waiting (and why), typed, or not. */
  function deliveryLine(q) {
    const dv = q.delivery;
    if (!dv || dv.state === "replaced") return null;
    const what = DELIVERY_WHAT[dv.kind] || "알림";
    const line = node("p", `qa-delivery qa-delivery-${dv.state}`);
    if (dv.state === "pending") {
      line.append(node("span", null,
        `${what} 알림 전달 대기 중 (${clock(dv.queued_at)}부터) — ` +
        (dv.draft ? "세션 터미널 입력창에 보내지 않은 줄이 있어 그 줄이 전송되거나 지워질 때까지 기다립니다"
                  : "세션 터미널이 입력을 받을 수 있을 때까지 기다립니다")));
      const now = node("button", "term-btn qa-deliver-now", "지금 전달");
      now.type = "button";
      now.title = "입력창에 보내지 않은 줄이 있으면 그 줄을 먼저 전송한 뒤 이 알림을 이어서 보냅니다.";
      now.addEventListener("click", () => deliverNow(q.id));
      line.append(now);
    } else if (dv.state === "forcing" || dv.state === "typing") {
      line.textContent = `${what} 알림 전달 중…`;
    } else if (dv.state === "held") {
      line.textContent = `${what} 알림: 세션이 hold 상태라 깨어나면 전달됩니다`;
    } else if (dv.state === "delivered") {
      line.textContent = `${what} 알림 세션에 전달됨 (${clock(dv.settled_at)}${dv.forced ? ", 강제 전달" : ""})`;
    } else if (dv.state === "failed") {
      line.textContent = `${what} 알림 전달 실패 — 세션이 종료되었거나 터미널에 쓸 수 없습니다`;
    }
    return line;
  }

  function checkLine(c) {
    const at = clock(c.at);
    const text = c.settled
      ? `방향 점검 (${at}) → 세션 응답: ${c.reply || "(메모 없이 닫음)"}`
      : `방향 점검 요청 (${at}) · 세션 응답 대기`;
    return node("p", `qa-check ${c.settled ? "qa-check-settled" : "qa-check-waiting"}`, text);
  }

  function checkButton(q) {
    const b = node("button", "term-btn qa-check-open", "방향 점검");
    b.type = "button";
    b.title = "세션에게 지금 하는 작업이 이 답과 맞는지 확인하라고 요청합니다. 닫힌 질문은 다시 열립니다.";
    b.addEventListener("click", () => {
      if (checking.has(q.id)) checking.delete(q.id); else checking.set(q.id, "");
      render();
    });
    return b;
  }

  function checkForm(q) {
    const form = node("div", "qa-check-form");
    const ta = node("textarea", "qa-check-note");
    ta.rows = 2;
    ta.placeholder = "어긋났다고 보는 점 (선택) — Enter 보내기, Shift+Enter 줄바꿈";
    ta.value = checking.get(q.id) || "";
    ta.addEventListener("input", () => checking.set(q.id, ta.value));
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); recheck(q.id); }
    });
    const send = node("button", "term-btn qa-check-send", "점검 요청 보내기");
    send.type = "button";
    send.addEventListener("click", () => recheck(q.id));
    form.append(ta, send);
    return form;
  }

  function deliveryNotice(doc) {
    const st = doc.delivery ? doc.delivery.state : (doc.delivered ? "delivered" : "failed");
    if (st === "delivered") return {text: "세션에 전달됨", bad: false};
    if (st === "pending") return {text: "전달 대기 중 (아래 상태 참고)", bad: false};
    if (st === "held") return {text: "세션이 깨어나면 전달됨", bad: false};
    return {text: "세션이 종료되어 전달하지 못함", bad: true};
  }

  async function post(url, body) {
    try {
      const resp = await api(url, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      });
      return {resp, doc: await resp.json().catch(() => ({}))};
    } catch {
      return {resp: null, doc: {}};
    }
  }

  async function recheck(id) {
    const q = ((data && data.questions) || []).find((x) => x.id === id);
    if (!q) return;
    const want = name;
    const {resp, doc} = await post(`/api/qa/${encodeURIComponent(id)}/recheck`,
                                   {revision: q.revision, note: checking.get(id) || ""});
    if (want !== name) return;
    if (!resp) notices.set(id, {text: "보내지 못함: 데몬에 연결할 수 없습니다", bad: true});
    else if (resp.status === 409 && doc.current) {
      replace(doc.current);
      notices.set(id, {text: "질문이 바뀌어 보내지 않았습니다. 현재 질문을 확인한 뒤 다시 보내세요.", bad: true});
    } else if (!resp.ok) notices.set(id, {text: doc.error || `HTTP ${resp.status}`, bad: true});
    else {
      checking.delete(id);
      const dn = deliveryNotice(doc);
      notices.set(id, {text: `방향 점검 요청 · ${dn.text}`, bad: dn.bad});
      replace({...doc.question, check: doc.check, delivery: doc.delivery});
      if (onChange) onChange();
    }
    render();
  }

  async function deliverNow(id) {
    const want = name;
    const q = ((data && data.questions) || []).find((x) => x.id === id);
    if (q && q.delivery) { q.delivery = {...q.delivery, state: "forcing"}; render(); }
    const {resp, doc} = await post(`/api/qa/${encodeURIComponent(id)}/deliver`, {});
    if (want !== name) return;
    if (!resp) notices.set(id, {text: "보내지 못함: 데몬에 연결할 수 없습니다", bad: true});
    else if (!resp.ok && !doc.delivery) notices.set(id, {text: doc.error || `HTTP ${resp.status}`, bad: true});
    const cur = ((data && data.questions) || []).find((x) => x.id === id);
    if (cur && doc.delivery) cur.delivery = doc.delivery;
    render();
    load();
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
    editing.delete(id);
    const dn = deliveryNotice(doc);
    notices.set(id, {text: `저장됨 · ${dn.text}`, bad: dn.bad});
    replace(doc.delivery ? {...doc.question, delivery: doc.delivery} : doc.question);
    render();
    if (onChange) onChange();
  }

  function clock(iso) {
    const t = new Date(iso);
    return isNaN(t) ? "" : t.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  }

  function nudgeState(n) {
    if (!n) return "";
    const at = clock(n.created_at);
    if (n.status === "sent") {
      const dv = n.delivery;
      if (dv && dv.state === "pending") return `질문 요청 (${at}) · 세션 터미널에 전달 대기 중`;
      if (dv && dv.state === "held") return `질문 요청 (${at}) · 세션이 깨어나면 전달`;
      return `질문 요청 보냄 (${at}) · 응답 대기`;
    }
    if (n.status === "asked") return `질문 요청 (${at}) → 질문으로 응답: ${n.questions.join(", ")}`;
    if (n.status === "nothing") return `질문 요청 (${at}) → 질문 없음${n.reply ? `: ${n.reply}` : ""}`;
    return "";
  }

  async function nudge() {
    if (nudging || !name) return;
    const want = name;
    nudging = true;
    nudgeNotice = null;
    render();
    let resp, doc;
    try {
      resp = await api("/api/qa/nudge", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({to: want}),
      });
      doc = await resp.json().catch(() => ({}));
    } catch {
      resp = null;
    }
    if (want !== name) return;
    nudging = false;
    if (!resp) nudgeNotice = {text: "보내지 못함: 데몬에 연결할 수 없습니다", bad: true};
    else if (!resp.ok) nudgeNotice = {text: doc.error || `HTTP ${resp.status}`, bad: true};
    else {
      nudgeNotice = doc.delivered ? null : {text: "세션이 종료되어 전달하지 못함", bad: true};
      if (data) data.nudge = doc.nudge;
    }
    render();
  }

  function replace(q) {
    if (!data || !q) return;
    data.questions = data.questions.map((x) => (x.id === q.id ? {check: x.check, delivery: x.delivery, ...q} : x));
    data.unresolved = data.questions.filter((x) => x.status === "open" || x.status === "stale").length;
  }

  function mount(target, session, opts = {}) {
    if (host === target && name === session && target.querySelector(".qa-pane")) return;
    unmount();
    host = target;
    name = session;
    api = opts.api || globalThis.api;
    onChange = opts.onChange || null;
    onThread = opts.onThread || null;
    onDiscuss = opts.onDiscuss || null;
    focusWant = "";
    data = null;
    error = "";
    nudging = false;
    nudgeNotice = null;
    cards.clear();
    editing.clear();
    checking.clear();
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

  return {mount, unmount, focus, refresh: () => { if (host) load(); }};
})();
