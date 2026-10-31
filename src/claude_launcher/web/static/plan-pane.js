/* Session plan (claunch-7jzp8): the terminal's split pane (app.js openSplit
   "plan") over /api/plan. The session writes its plan with the plan_* MCP
   tools -- steps of one to a few lines, decisions whose options each carry
   an expected effect and a risk, and the closure items (checks a person
   makes by hand, followup candidates) that keep it open. Here the user reads
   it, picks a branch, checks an item, disposes of a followup or comments;
   each is typed into the session (daemon/plan_api.py). All session text
   uses textContent.

   Every decision names the node's rev the pane last read; a 409 means the
   session reworded it, and the pane reloads so the user decides on what it
   says now. A note being typed is kept per node across polls.

   Joint view (claunch-7jzp8.1): when the session's joint tree
   (/api/plan/joint, plan/joint.py) holds another planned session, a toggle
   lists the tree -- spawn children, sessions on the same issue or a
   sub-issue -- each with its progress line, and opens any of them as a
   nested pane of its own, where the user decides on that session's plan. */

const PLAN_STEP_STATUS = {todo: "예정", doing: "진행 중", done: "완료", skipped: "건너뜀", blocked: "막힘"};
const PLAN_VERDICT = {passed: "통과", failed: "실패"};
const PLAN_ACTIONS = [
  {id: "here", label: "여기서 진행"},
  {id: "issue", label: "이슈로 넘김"},
  {id: "drop", label: "버림"},
];
const PLAN_ACTION_LABEL = Object.fromEntries(PLAN_ACTIONS.map(a => [a.id, a.label]));
const PLAN_VIA = {spawn: "자식 세션", same: "같은 이슈", issue: "하위 이슈"};

function planNode(tag, text, cls) {
  const n = document.createElement(tag);
  if (text != null) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}

/* One line saying what the user decided on a node, or null. */
function planResultText(n) {
  const r = n.result;
  if (!r) return null;
  if (n.kind === "decision") return `선택: ${r.choice}`;
  if (n.kind === "verify") return `확인: ${PLAN_VERDICT[r.verdict] || r.verdict}`;
  return `처분: ${PLAN_ACTION_LABEL[r.action] || r.action}` + (n.status === "done" ? " · 처리됨" : "");
}

function makePlanPane({root, session, request, onChange, nested}) {
  const node = planNode;
  const base = "/api/plan";
  let data = null, error = "", busy = false, seq = 0, timer = null, feedback = "";
  let joint = null, jointOpen = false;
  const members = {};    // session -> {el, view} of an opened joint member
  const notes = {};      // node id -> the note being typed
  const told = {};       // node id -> the last write's message

  async function load() {
    const mine = ++seq;
    try {
      const out = await request(`${base}?session=${encodeURIComponent(session)}`);
      if (mine !== seq) return;
      data = out; error = "";
    } catch (exc) { if (mine === seq) error = exc.message; }
    if (!nested) {
      try {
        const out = await request(`${base}/joint?session=${encodeURIComponent(session)}`);
        if (mine === seq) joint = out.group;
      } catch (_exc) { if (mine === seq) joint = null; }
    }
    if (mine === seq) { render(); schedule(); }
  }
  function schedule() { clearTimeout(timer); timer = setTimeout(load, 5000); }

  async function act(id, body, done) {
    if (busy) return;
    busy = true; told[id] = ""; render();
    try {
      await request(`${base}/node/${encodeURIComponent(id)}/decide`, "POST", {session, note: notes[id] || "", ...body});
      told[id] = done; delete notes[id];
      if (onChange) onChange();
    } catch (exc) {
      told[id] = exc.status === 409 ? "세션이 이 항목을 고쳤습니다. 바뀐 내용을 보고 다시 결정하세요." : exc.message;
    } finally { busy = false; await load(); }
  }

  async function send() {
    const text = feedback.trim();
    if (busy || !text) return;
    busy = true; error = ""; render();
    try {
      await request(`${base}/feedback`, "POST", {session, text});
      feedback = ""; told._feedback = "세션에 전달했습니다";
    } catch (exc) { error = exc.message; }
    finally { busy = false; await load(); }
  }

  function noteBox(n, placeholder) {
    const t = node("textarea", null, "plan-note");
    t.value = notes[n.id] || ""; t.placeholder = placeholder; t.rows = 1; t.disabled = busy;
    t.setAttribute("aria-label", placeholder);
    t.oninput = () => { notes[n.id] = t.value; };
    return t;
  }
  function button(label, fn, cls) {
    const b = node("button", label, "wf-btn" + (cls ? " " + cls : ""));
    b.type = "button"; b.disabled = busy; b.onclick = fn;
    return b;
  }
  function tail(n, box) {
    const res = planResultText(n);
    if (res) box.append(node("p", res + (n.result.note ? ` — ${n.result.note}` : ""), "plan-result"));
    else if (n.previous) box.append(node("p", "세션이 내용을 고쳐 이전 결정이 보류되었습니다. 다시 결정하세요.", "plan-stale"));
    if (told[n.id]) box.append(node("p", told[n.id], "plan-told"));
  }

  function decision(n) {
    const box = node("div", null, "plan-decision");
    const opts = node("div", null, "plan-options");
    for (const o of n.options || []) {
      const chosen = n.result && n.result.choice === o.label;
      const card = node("div", null, "plan-option" + (chosen ? " plan-chosen" : "") + (o.recommended ? " plan-recommended" : ""));
      const head = node("div", null, "plan-option-head");
      head.append(node("strong", o.label));
      if (o.recommended) head.append(node("span", "권장", "plan-chip"));
      card.append(head,
        node("p", "기대 효과: " + o.effect, "plan-effect"),
        node("p", "리스크: " + o.risk, "plan-risk"));
      if (!chosen) card.append(button("이 안 선택", () => act(n.id, {rev: n.rev, choice: o.label}, `「${o.label}」을 선택해 세션에 전달했습니다`)));
      opts.append(card);
    }
    box.append(opts, noteBox(n, "선택에 붙일 메모 (선택 사항)"));
    tail(n, box);
    return box;
  }

  function stepTree(nodes) {
    const ul = node("ul", null, "plan-tree");
    for (const n of nodes) {
      const li = node("li", null, `plan-item plan-${n.kind} plan-status-${n.status || (n.result ? "decided" : "open")}`);
      const head = node("div", null, "plan-item-head");
      const chip = n.kind === "decision"
        ? (n.result ? "결정됨" : "결정 필요")
        : (PLAN_STEP_STATUS[n.status] || n.status);
      head.append(node("span", chip, "plan-chip"), node("span", n.title, "plan-title"));
      li.append(head);
      if (n.detail) li.append(node("p", n.detail, "plan-detail"));
      if (n.kind === "decision") li.append(decision(n));
      if ((n.children || []).length) li.append(stepTree(n.children));
      ul.append(li);
    }
    return ul;
  }

  function closureItem(n) {
    const li = node("li", null, `plan-item plan-${n.kind}`);
    const head = node("div", null, "plan-item-head");
    head.append(node("span", n.kind === "verify" ? "사람 확인" : "후속 후보", "plan-chip"), node("span", n.title, "plan-title"));
    li.append(head);
    if (n.detail) li.append(node("p", n.detail, "plan-detail"));
    const row = node("div", null, "plan-actions");
    if (n.kind === "verify") {
      row.append(button("통과", () => act(n.id, {rev: n.rev, verdict: "passed"}, "통과로 전달했습니다")),
                 button("실패", () => act(n.id, {rev: n.rev, verdict: "failed"}, "실패로 전달했습니다")));
      li.append(noteBox(n, "확인 메모 (실패면 필수: 무엇이 잘못됐는지)"), row);
    } else {
      for (const a of PLAN_ACTIONS) row.append(button(a.label, () => act(n.id, {rev: n.rev, action: a.id}, `「${a.label}」로 전달했습니다`)));
      li.append(noteBox(n, "메모 (선택 사항)"), row);
    }
    tail(n, li);
    return li;
  }

  function toggleMember(name) {
    if (members[name]) { members[name].view.stop(); delete members[name]; render(); return; }
    const el = node("div", null, "plan-joint-pane");
    members[name] = {el, view: makePlanPane({root: () => el, session: name, request, onChange, nested: true})};
    render(); members[name].view.open();
  }

  function jointTree() {
    const ul = node("ul", null, "plan-joint");
    for (const m of joint.members) {
      const li = node("li", null, "plan-joint-member" + (m.session === session ? " plan-joint-self" : ""));
      li.setAttribute("style", `margin-left: ${m.depth * 1.2}em`);
      const head = node("div", null, "plan-item-head");
      if (m.via) head.append(node("span", PLAN_VIA[m.via] || m.via, "plan-chip"));
      head.append(node("strong", m.session), node("span", m.title || "", "plan-title"));
      li.append(head);
      if (!m.has_plan) {
        li.append(node("p", "계획 없음", "plan-muted"));
      } else {
        const line = `단계 ${m.done || 0}/${m.steps || 0} 완료` + (m.current ? ` · 지금: ${m.current}` : "")
          + ` · 내 결정 대기 ${m.waiting || 0}건 · ` + (m.closable ? "닫아도 됨" : "닫을 수 없음");
        li.append(node("p", line, m.closable ? "plan-closable" : "plan-progress"));
        if (m.session !== session) {
          li.append(button(members[m.session] ? "접기" : "계획 펼치기", () => toggleMember(m.session)));
          if (members[m.session]) li.append(members[m.session].el);
        }
      }
      ul.append(li);
    }
    return ul;
  }

  function verdictLine(s) {
    if (s.closable) return node("p", "닫아도 됨: 남은 단계·결정·확인 항목이 없습니다.", "plan-closable");
    const mine = s.blockers.filter(b => b.who === "user").length;
    const theirs = s.blockers.length - mine;
    return node("p", `닫을 수 없음: 내 결정 대기 ${mine}건 · 세션 작업 남음 ${theirs}건`, "plan-open");
  }

  function render() {
    const el = root(); if (!el) return;
    el.replaceChildren();
    const shell = node("div", null, "plan-pane");
    if (error) { const a = node("p", error, "plan-error"); a.setAttribute("role", "alert"); shell.append(a); }
    if (!data) { shell.append(node("p", error ? "" : "loading…", "wf-note")); el.append(shell); return; }
    const p = data.plan;
    if (!p) {
      shell.append(node("p", "이 세션은 아직 계획을 쓰지 않았습니다 (plan_set).", "plan-empty"));
      // a parent without a plan of its own still shows its children's
      if (joint && joint.sessions > 0) shell.append(node("h4", "연관 세션", "plan-section"), jointTree());
      el.append(shell); return;
    }
    const s = p.summary;
    const head = node("div", null, "plan-pane-head");
    head.append(node("strong", p.title), node("span", `rev ${p.revision}` + (p.issue ? ` · ${p.issue}` : ""), "plan-muted"));
    shell.append(head);
    if (joint && joint.sessions > 1) {
      const row = node("div", null, "plan-joint-bar");
      row.append(node("span", `연관 세션 ${joint.sessions}개 · 내 결정 대기 ${joint.waiting}건`, "plan-muted"),
                 button(jointOpen ? "이 세션만 보기" : "연관 세션 함께 보기", () => { jointOpen = !jointOpen; render(); }));
      shell.append(row);
      if (jointOpen) {
        shell.append(node("h4", "연관 세션", "plan-section"), jointTree());
        el.append(shell); return;
      }
    }
    if (p.goal) shell.append(node("p", p.goal, "plan-goal"));
    shell.append(node("p", `단계 ${s.done}/${s.steps} 완료` + (s.current ? ` · 지금: ${s.current}` : ""), "plan-progress"), verdictLine(s));

    shell.append(node("h4", "계획", "plan-section"));
    if ((p.steps || []).length) shell.append(stepTree(p.steps));
    else shell.append(node("p", "단계가 없습니다.", "plan-muted"));

    shell.append(node("h4", "종료 조건", "plan-section"));
    if ((p.closure || []).length) {
      const ul = node("ul", null, "plan-tree");
      for (const n of p.closure) ul.append(closureItem(n));
      shell.append(ul);
    } else shell.append(node("p", "사람 확인 항목·후속 후보가 없습니다.", "plan-muted"));

    if (!s.closable && s.blockers.length) {
      const det = node("details", null, "plan-blockers");
      det.append(node("summary", `닫을 수 없는 이유 ${s.blockers.length}건`));
      const ul = node("ul");
      for (const b of s.blockers) ul.append(node("li", `${b.who === "user" ? "[내 차례]" : "[세션]"} ${b.title} — ${b.why}`));
      det.append(ul);
      shell.append(det);
    }

    const fb = node("div", null, "plan-feedback");
    const t = node("textarea"); t.rows = 2; t.value = feedback; t.disabled = busy;
    t.placeholder = "계획에 대한 의견 (방향 수정, 빠진 단계 등) — 세션 터미널로 전달됩니다";
    t.setAttribute("aria-label", "계획에 대한 의견");
    t.oninput = () => { feedback = t.value; };
    fb.append(t, button("의견 보내기", send));
    if (told._feedback) fb.append(node("p", told._feedback, "plan-told"));
    shell.append(fb);
    el.append(shell);
  }

  function stop() {
    ++seq; clearTimeout(timer); timer = null;
    for (const name of Object.keys(members)) { members[name].view.stop(); delete members[name]; }
  }
  return {open: load, stop, _state: () => ({data, notes, joint})};
}

async function planRequest(path, method = "GET", body) {
  const response = await api(path, {method, ...(body ? {headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)} : {})});
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw Object.assign(Error(result.error || `HTTP ${response.status}`), {status: response.status, body: result});
  return result;
}

globalThis.PlanPane = (() => {
  let view = null, el = null, host = null, name = "";
  function mount(target, session, opts = {}) {
    if (view && host === target && name === session && target.contains(el)) return;
    unmount();
    el = document.createElement("div"); el.className = "plan-host";
    target.replaceChildren(el); name = session; host = target;
    view = makePlanPane({root: () => el, session, request: opts.request || planRequest, onChange: opts.onChange});
    view.open();
  }
  function unmount() { if (view) view.stop(); view = null; if (el) el.remove(); el = host = null; name = ""; }
  return {mount, unmount, _view: () => view};
})();
