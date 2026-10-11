/* World-model simulations at #/sims and #/sims/<id> (daemon/sim_api.py,
   docs/simulation.md). All user text goes through textContent. */
globalThis.SimPage = (() => {
  const node = (tag, text, cls) => {
    const n = document.createElement(tag);
    if (text != null) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  };
  const STATE_LABEL = {idle: "대기", running: "실행 중", stopped: "중지됨"};
  const STATUS_LABEL = {open: "열림", question: "질문", answered: "답함", dismissed: "제외", resolved: "해결"};
  const KIND_LABEL = {unhandled: "규칙 없음", conflict: "충돌", invariant: "불변식 위반", stuck: "막힘", loop: "무한 연쇄", "eval-error": "식 오류"};
  const REASONING = ["none", "low", "medium", "high"];
  let simId = "", tab = "findings", project = "", statusFilter = "open,question,answered";
  let list = [], sessions = [], profiles = {profiles: [], defaults: {}};
  let detail = null, findings = null, model = null, coverage = null;
  let error = "", notice = "", busy = false, creating = false, timer = null, seq = 0;
  // Drafts outlive a redraw (every poll redraws): the create form, the
  // settings form of the open simulation, answers by finding key, the nudge.
  let draft = null, settingsDraft = null, nudgeDraft = "", transferTo = "";
  let replays = {}, proposals = null, proposalNotes = {};
  const answers = {};
  const view = () => document.getElementById("sims-view");

  async function request(path, method = "GET", body) {
    const response = await api(path, {method, ...(body ? {headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)} : {})});
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(result.error || `HTTP ${response.status}`);
    return result;
  }
  const enc = encodeURIComponent;

  async function load() {
    const mine = ++seq;
    try {
      const sess = await request("/api/sessions?state=active");
      if (mine !== seq) return;
      sessions = (sess.sessions || []).filter(s => s.status !== "exited");
      if (!simId) {
        const q = project ? "?project=" + enc(project) : "";
        const [l, p] = await Promise.all([request("/api/sims" + q), request("/api/sims/profiles")]);
        if (mine !== seq) return;
        list = l.sims || []; profiles = p;
      } else {
        const base = "/api/sims/" + enc(simId);
        const [d, f, p] = await Promise.all([
          request(base), request(base + "/findings?limit=300&status=" + enc(statusFilter)), request("/api/sims/profiles")]);
        if (mine !== seq) return;
        detail = d; findings = f; profiles = p;
        if (tab === "model" || tab === "map") {
          const [m, c] = await Promise.all([request(base + "/model"), request(base + "/coverage")]);
          if (mine !== seq) return;
          model = m.model; coverage = c;
        }
        if (tab === "proposals") {
          const pr = await request(base + "/proposals");
          if (mine !== seq) return;
          proposals = pr.proposals || [];
        }
      }
      error = "";
    } catch (exc) { if (mine === seq) error = exc.message; }
    if (mine !== seq) return;
    // A poll must not redraw under someone typing; the next one will.
    const active = document.activeElement;
    if (active && view()?.contains(active) && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName)) return;
    render();
  }

  async function act(action, done) {
    if (busy) return;
    busy = true; error = ""; notice = ""; render();
    try { const r = await action(); if (done) notice = done(r) || ""; await load(); }
    catch (exc) { error = exc.message; }
    finally { busy = false; render(); }
  }
  function button(text, onclick, cls = "") {
    const b = node("button", text, "wf-btn " + cls); b.type = "button";
    b.disabled = busy; b.onclick = onclick; return b;
  }
  function select(options, value, onchange, label) {
    const s = node("select"); if (label) s.setAttribute("aria-label", label);
    for (const [v, t] of options) { const o = node("option", t); o.value = v; s.append(o); }
    s.value = value; s.disabled = busy; s.onchange = () => onchange(s.value); return s;
  }
  const sessionOptions = (blank) => [["", blank], ...sessions.map(s => [s.name, s.name + (s.project ? ` (${s.project})` : "")])];
  const PROPOSAL_LABEL = {proposed: "검토 대기", accepted: "수락", rejected: "거절"};
  const EDIT_KIND_LABEL = {clarify: "명료화", complete: "보충", decide: "결정 필요"};
  const TRIAGE_LABEL = {model: "모델이 원천을 잘못 읽음", spec: "원천에 정해진 내용 없음 (사람이 결정)", intended: "결함 아님"};
  const MODEL_KEYS = ["interpreter_profile", "judge_profile", "interpreter_model", "judge_model", "interpreter_reasoning", "judge_reasoning"];

  function render() {
    const root = view(); if (!root) return;
    root.replaceChildren();
    const shell = node("div", null, "sim-shell"); root.append(shell);
    if (error) { const a = node("p", error, "sim-error"); a.setAttribute("role", "alert"); shell.append(a); }
    if (notice) shell.append(node("p", notice, "sim-notice"));
    if (simId) renderDetail(shell); else renderList(shell);
  }

  // -- list ------------------------------------------------------------------
  function renderList(shell) {
    const head = node("header", null, "sim-head");
    const title = node("div");
    title.append(node("span", "WORLD-MODEL SIMULATIONS", "sim-eyebrow"), node("h2", "시뮬레이션"),
      node("p", "세션이 명세·코드·시나리오로 월드 모델을 세우고, 데몬이 그 모델을 걸어 빈칸·충돌·불변식 위반·막힘을 찾습니다. 소유 세션만 실행·중지할 수 있고, 다른 세션은 읽을 수 있습니다.", "sim-muted"));
    const projects = [...new Set([...sessions.map(s => s.project), ...list.map(s => s.project)].filter(Boolean))].sort();
    const tools = node("div", null, "sim-tools");
    tools.append(select([["", "모든 프로젝트"], ...projects.map(p => [p, p])], project, v => { project = v; load(); }, "프로젝트"),
      button(creating ? "닫기" : "새 시뮬레이션", () => { creating = !creating; render(); }, "sim-primary"),
      button("새로고침", load));
    head.append(title, tools); shell.append(head);
    if (creating) shell.append(createForm());
    if (!list.length) { shell.append(node("p", "시뮬레이션이 없습니다. 세션이 sim_create로 만들거나, 위의 「새 시뮬레이션」으로 세션에 요청할 수 있습니다.", "sim-empty")); return; }
    const table = node("table", null, "sim-table");
    const hr = node("tr");
    for (const h of ["제목", "소유 세션", "상태", "열림", "질문", "라운드", "모델", "갱신"]) hr.append(node("th", h));
    table.append(hr);
    for (const s of list) {
      const tr = node("tr"); tr.tabIndex = 0;
      tr.onclick = () => { location.hash = "#/sims/" + s.id; };
      tr.onkeydown = e => { if (e.key === "Enter") tr.onclick(); };
      const t = node("td"); t.append(node("strong", s.title), node("div", s.id + (s.project ? " · " + s.project : ""), "sim-muted"));
      const st = node("td"); st.append(node("span", STATE_LABEL[s.state] || s.state, "sim-badge sim-" + s.state));
      const ms = s.model_stats || {};
      tr.append(t, node("td", s.owner || "없음"), st, node("td", String(s.counts?.open ?? 0)),
        node("td", String((s.counts?.question ?? 0) + (s.counts?.answered ?? 0))),
        node("td", String(s.last_round?.round ?? 0)),
        node("td", `${ms.machines || 0}기계 · ${ms.rules || 0}규칙`), node("td", (s.updated_at || "").replace("T", " ").slice(0, 16)));
      table.append(tr);
    }
    const wrap = node("div", null, "sim-scroll"); wrap.append(table); shell.append(wrap);
  }

  function createForm() {
    const form = node("form", null, "sim-form");
    draft = draft || {title: "", goal: "", sources: "", owner: "", interpreter_profile: profiles.defaults?.interpreter_profile || "",
      judge_profile: profiles.defaults?.judge_profile || "", interpreter_model: "", judge_model: "",
      interpreter_reasoning: "low", judge_reasoning: "low"};
    const field = (label, key, multiline) => {
      const l = node("label", label); const i = node(multiline ? "textarea" : "input");
      i.value = draft[key]; i.oninput = () => { draft[key] = i.value; }; i.disabled = busy; l.append(i); form.append(l); return i;
    };
    field("제목", "title").required = true;
    field("목표 — 무엇을 확인하려는가", "goal", true);
    field("원천 — 모델을 세울 파일·glob (한 줄에 하나)", "sources", true);
    const o = node("label", "맡길 세션 — 이 세션이 소유자가 되고 요청이 그 세션에 입력됩니다");
    o.append(select(sessionOptions("선택"), draft.owner, v => { draft.owner = v; }, "맡길 세션")); form.append(o);
    form.append(profileFields(draft, v => { Object.assign(draft, v); render(); }));
    const from = node("label", "이어 받기 — 고른 시뮬레이션의 모델·판정·답한 질문으로 시작합니다");
    from.append(select([["", "없음 (빈 모델)"], ...list.map(x => [x.id, `${x.title} (${x.id})`])], draft.from || "",
      v => { draft.from = v; }, "이어 받을 시뮬레이션"));
    form.append(from);
    const submit = node("button", "만들고 요청 보내기", "wf-btn sim-primary"); submit.disabled = busy;
    form.append(submit);
    form.onsubmit = e => {
      e.preventDefault();
      if (!draft.owner) { error = "맡길 세션을 고르세요"; render(); return; }
      const settings = {};
      for (const k of MODEL_KEYS) settings[k] = draft[k];
      act(() => request("/api/sims", "POST", {title: draft.title, goal: draft.goal, owner: draft.owner,
        sources: draft.sources.split("\n").map(s => s.trim()).filter(Boolean), settings, ...(draft.from ? {from: draft.from} : {})}),
        r => { creating = false; draft = null; location.hash = "#/sims/" + r.sim.id; return r.nudged ? `${r.sim.owner}에 요청을 보냈습니다` : "요청을 입력하지 못했습니다 — 세션 상태를 확인하세요"; });
    };
    return form;
  }

  function profileFields(cur, set) {
    const box = node("fieldset", null, "sim-models");
    box.append(node("legend", "모델 — pi로 실행할 수 있는 프로필만 고를 수 있습니다"));
    const models = profiles.models || {};
    const bProfile = cur.interpreter_profile || profiles.defaults?.interpreter_profile || "";
    const cProfile = cur.judge_profile || profiles.defaults?.judge_profile || "";
    const bModel = cur.interpreter_model || (models[bProfile] || [])[0] || "";
    const cModel = cProfile && cProfile !== bProfile ? (cur.judge_model || (models[cProfile] || [])[0] || "") : (cur.judge_model || bModel);
    const same = bProfile && (cProfile || bProfile) === bProfile && cModel === bModel;
    for (const [role, label] of [["interpreter", "해석(B)"], ["judge", "판정(C)"]]) {
      const row = node("div", null, "sim-row");
      const names = role === "judge" ? (profiles.judges || []) : (profiles.profiles || []);
      const prof = role === "judge" ? (cProfile || bProfile) : bProfile;
      row.append(node("span", label),
        select([["", role === "judge" ? "해석과 같은 프로필" : "기본값"], ...names.map(p => [p, p])], cur[role + "_profile"] || "",
          v => set({[role + "_profile"]: v, [role + "_model"]: ""}), label + " 프로필"),
        select([["", "프로필 기본 모델"], ...(models[prof] || []).map(m => [m, m])], cur[role + "_model"] || "",
          v => set({[role + "_model"]: v}), label + " 모델"),
        select(REASONING.map(r => [r, "reasoning " + r]), cur[role + "_reasoning"] || "low", v => set({[role + "_reasoning"]: v}), label + " reasoning"));
      box.append(row);
    }
    if (!(profiles.profiles || []).length) box.append(node("p", "pi로 실행할 수 있는 프로필이 없습니다.", "sim-muted"));
    if (same) box.append(node("p", "해석과 판정이 같은 모델이면 판정이 독립적이지 않습니다. 판정 모델을 다르게 고르세요.", "sim-warn"));
    return box;
  }

  // -- detail ----------------------------------------------------------------
  function renderDetail(shell) {
    if (!detail) { shell.append(node("p", "불러오는 중…", "sim-muted")); return; }
    const s = detail.sim, base = "/api/sims/" + enc(s.id);
    const head = node("header", null, "sim-head");
    const title = node("div");
    const back = node("a", "← 시뮬레이션", "sim-back"); back.href = "#/sims";
    title.append(back, node("h2", s.title));
    const meta = node("p", null, "sim-muted");
    meta.append(node("span", STATE_LABEL[s.state] || s.state, "sim-badge sim-" + s.state),
      node("span", ` ${s.state_reason || ""} · ${s.id} · 소유 ${s.owner || "없음"}${s.owner && !detail.owner_live ? " (종료됨)" : ""}` +
        (s.root ? ` · ${s.root}` : "")));
    title.append(meta);
    if (s.runner && s.runner.session) {
      const run = node("p", null, "sim-runner");
      const link = node("a", s.runner.session); link.href = "#/s/" + enc(s.runner.session);
      run.append(node("span", "실행 세션 "), link, node("span", ` · 해석 ${s.settings.interpreter_profile || "-"} · 판정 ${s.settings.judge_profile || "(해석과 같음)"} · 시작 ${s.runner.started_at || ""}`));
      title.append(run);
    } else if (s.last_runner) {
      const lr = s.last_runner;
      title.append(node("p", `지난 실행 세션 ${lr.session}: ${lr.failed ? "실패" : "끝남"} · ${lr.reason || ""}`, lr.failed ? "sim-warn" : "sim-muted"));
    }
    if (s.from) {
      const f = node("p", "이어 받음: ", "sim-muted"); const a = node("a", s.from); a.href = "#/sims/" + enc(s.from); f.append(a); title.append(f);
    }
    if ((s.stale_sources || []).length)
      title.append(node("p", `원천이 바뀌어 인용을 잃은 규칙 ${s.stale_sources.length}개: ${s.stale_sources.map(x => x.id).join(", ")}`, "sim-warn"));
    if (s.goal) title.append(node("p", s.goal, "sim-goal"));
    head.append(title); shell.append(head);

    const actions = node("div", null, "sim-actions");
    if (s.state === "running") actions.append(button("중지", () => act(() => request(base + "/stop", "POST", {}), r => r.notified ? "중지했고 소유 세션에 알렸습니다" : "중지했습니다")));
    else actions.append(button("실행", () => act(() => request(base + "/start", "POST", {}), r => r.runner ? `실행 세션 ${r.runner}을 띄웠습니다` : r.manual ? `실행했습니다 (소유 세션이 해석: ${r.manual})` : "실행했습니다"), "sim-primary"));
    actions.append(button("라운드 한 번", () => act(() => request(base + "/round", "POST", {}), r => `라운드 ${r.result.round}: 새 ${r.result.new} · 열림 ${r.result.open}`)));
    const say = node("input"); say.placeholder = "소유 세션에 전할 말 (비우면 계속 진행 요청)"; say.disabled = busy || !s.owner;
    say.value = nudgeDraft; say.oninput = () => { nudgeDraft = say.value; };
    actions.append(say, button("소유 세션에 요청", () => act(() => request(base + "/nudge", "POST", {text: nudgeDraft}),
      r => { nudgeDraft = ""; return r.nudged ? `${r.to}에 입력했습니다` : "입력하지 못했습니다"; })));
    actions.append(select(sessionOptions("이관할 세션"), transferTo, v => { transferTo = v; }, "이관할 세션"),
      button("소유권 이관", () => { const to = transferTo; if (to) act(() => request(base + "/transfer", "POST", {to}), () => { transferTo = ""; return `${to}로 이관했습니다`; }); }));
    if (s.owner) actions.append(button("소유권 해제", () => act(() => request(base + "/release", "POST", {}), () => "해제했습니다")));
    actions.append(button("삭제", () => {
      if (globalThis.confirm(`${s.id} 「${s.title}」를 삭제할까요? 모델과 기록이 모두 지워집니다.`))
        act(() => request(base, "DELETE"), () => { location.hash = "#/sims"; return ""; });
    }, "sim-danger"));
    shell.append(actions);

    const c = s.counts || {}, ms = s.model_stats || {}, last = s.last_round || {};
    const stats = node("div", null, "sim-stats");
    for (const [label, value] of [["열림", c.open || 0], ["질문 대기", c.question || 0], ["답함", c.answered || 0],
      ["제외", c.dismissed || 0], ["해결", c.resolved || 0], ["규칙", `${ms.rules || 0} (임시 ${ms.provisional || 0})`],
      ["상태 기계", ms.machines || 0], ["라운드", last.round || 0],
      ["도달 칸", last.coverage ? `${last.coverage.reached}/${last.coverage.cells}` : "-"]]) {
      const b = node("div", null, "sim-stat"); b.append(node("span", label), node("strong", String(value))); stats.append(b);
    }
    shell.append(stats);

    const nav = node("nav", null, "sim-tabs");
    for (const [key, label] of [["findings", "발견"], ["map", "빈칸 지도"], ["model", "모델"], ["proposals", "편집 제안" + ((s.proposals?.proposed) ? ` (${s.proposals.proposed})` : "")], ["rounds", "라운드"], ["settings", "설정"], ["journal", "기록"]]) {
      const b = button(label, () => { tab = key; location.hash = `#/sims/${s.id}/${key}`; }, tab === key ? "is-active" : "");
      b.setAttribute("aria-pressed", String(tab === key)); nav.append(b);
    }
    shell.append(nav);
    const body = node("section", null, "sim-body"); shell.append(body);
    if (tab === "findings") renderFindings(body, s, base);
    else if (tab === "map") renderMap(body);
    else if (tab === "model") renderModel(body);
    else if (tab === "proposals") renderProposals(body, base);
    else if (tab === "rounds") renderRounds(body);
    else if (tab === "settings") renderSettings(body, s, base);
    else renderJournal(body);
  }

  function renderFindings(body, s, base) {
    const bar = node("div", null, "sim-tools");
    bar.append(select([["open,question,answered", "처리할 것"], ["question", "질문 대기"], ["open", "열림"],
      ["dismissed", "제외"], ["resolved", "해결"], ["", "전체"]], statusFilter, v => { statusFilter = v; load(); }, "상태"),
      node("span", `${findings?.total ?? 0}건`, "sim-muted"));
    body.append(bar);
    for (const f of findings?.findings || []) {
      const card = node("article", null, "sim-finding sim-f-" + f.status);
      const h = node("header");
      h.append(node("span", STATUS_LABEL[f.status] || f.status, "sim-badge"), node("strong", KIND_LABEL[f.kind] || f.kind),
        node("code", f.key), node("span", `${f.count}회 · 라운드 ${f.first_round}–${f.last_round}`, "sim-muted"));
      card.append(h, node("p", f.detail));
      if (f.assumed?.length) card.append(node("p", "가정에 기댐: " + f.assumed.join(", "), "sim-warn"));
      if (f.provisional?.length) card.append(node("p", "임시 규칙에 기댐: " + f.provisional.join(", "), "sim-warn"));
      const steps = (f.witness?.steps || []).map(x => x.event).join(" → ");
      if (steps) {
        const w = node("p", "재현 경로: " + (f.witness.checkpoint ? `[${f.witness.checkpoint}] ` : "") + steps, "sim-witness");
        w.append(button(replays[f.key] ? "재생 닫기" : "재생", () => toggleReplay(base, f.key)));
        card.append(w);
      }
      if (replays[f.key]) card.append(renderReplay(replays[f.key]));
      if (f.resolution) card.append(node("p", `${f.resolution.by}: ${f.resolution.reason || ""}`, "sim-muted"));
      if (f.triage) card.append(node("p", `판정자 분류: ${TRIAGE_LABEL[f.triage.verdict] || f.triage.verdict} — ${f.triage.why}`, "sim-triage"));
      if (f.stale) card.append(node("p", "지금 모델에서 재현 경로를 재생할 수 없습니다: " + f.stale, "sim-warn"));
      if (f.reopened) card.append(node("p", `다시 열림: 질문 뒤 원천이 바뀌었습니다 (이전 질문: ${f.prev_question?.text || ""})`, "sim-warn"));
      if (f.question) {
        const q = node("div", null, "sim-question");
        q.append(node("p", "질문 (" + f.question.by + "): " + f.question.text));
        if (f.question.options?.length) q.append(node("p", "선택지: " + f.question.options.join(" / "), "sim-muted"));
        if (f.answer) q.append(node("p", "답: " + f.answer.text));
        const a = node("textarea"); a.placeholder = "답을 쓰면 소유 세션에 전달됩니다"; a.disabled = busy;
        a.value = answers[f.key] || ""; a.oninput = () => { answers[f.key] = a.value; };
        const opts = node("div", null, "sim-row");
        for (const o of f.question.options || []) opts.append(button(o, () => { a.value = answers[f.key] = o; }));
        q.append(opts, a, button(f.answer ? "답 고치기" : "답하기", () => {
          const text = answers[f.key] || "";
          if (text.trim()) act(() => request(base + "/answer", "POST", {key: f.key, text}),
            r => { delete answers[f.key]; return r.nudged ? "답을 소유 세션에 전달했습니다" : "답을 저장했습니다 (소유 세션 없음)"; });
        }, "sim-primary"));
        card.append(q);
      }
      body.append(card);
    }
    if (!(findings?.findings || []).length) body.append(node("p", "해당하는 발견이 없습니다.", "sim-empty"));
  }

  // Replay a finding's witness on the current model, step by step.
  async function toggleReplay(base, key) {
    if (replays[key]) { delete replays[key]; render(); return; }
    replays[key] = {loading: true}; render();
    try { replays[key] = await request(base + "/replay?key=" + enc(key)); }
    catch (exc) { replays[key] = {stale: true, why: exc.message}; }
    render();
  }

  const showState = st => Object.entries(st.sm || {}).map(([m, v]) => `${m}=${v}`)
    .concat(Object.entries(st.vars || {}).map(([k, v]) => `$${k}=${JSON.stringify(v)}`)).join(" · ") || "(없음)";

  function renderReplay(r) {
    const box = node("div", null, "sim-replay");
    if (r.loading) { box.append(node("p", "재생 중…", "sim-muted")); return box; }
    if (r.stale) { box.append(node("p", "지금 모델에서는 재생할 수 없습니다: " + (r.why || ""), "sim-warn")); return box; }
    box.append(node("p", r.reproduces ? "지금 모델에서도 같은 발견이 다시 납니다." : "지금 모델에서는 이 발견이 다시 나지 않습니다.",
      r.reproduces ? "sim-muted" : "sim-warn"));
    const ol = node("ol", null, "sim-steps");
    const first = node("li", null, "sim-step");
    first.append(node("strong", "시작" + (r.checkpoint ? ` [${r.checkpoint}]` : "")), node("span", " " + showState(r.start)));
    ol.append(first);
    for (const st of r.steps || []) {
      const li = node("li", null, "sim-step" + (st.n === r.raised_at ? " sim-step-raised" : ""));
      li.append(node("strong", st.event));
      const ch = Object.entries(st.changed || {}).map(([k, [a, b]]) => `${k}: ${a ?? "-"} → ${b}`).join(", ");
      li.append(node("span", " " + (ch || "변화 없음"), ch ? "" : "sim-muted"));
      if (st.fired?.length) li.append(node("div", "적용된 규칙: " + st.fired.join(", "), "sim-muted"));
      const assumed = Object.entries(st.assume || {});
      if (assumed.length) li.append(node("div", "가정: " + assumed.map(([k, v]) => `${k}=${v ? "참" : "거짓"}`).join(", "), "sim-warn"));
      if (st.findings?.length) li.append(node("div", "발견: " + st.findings.join(", "), st.n === r.raised_at ? "sim-warn" : "sim-muted"));
      ol.append(li);
    }
    box.append(ol);
    const last = (r.steps || []).at(-1);
    if (last) box.append(node("p", "끝 상태: " + showState(last), "sim-muted"));
    return box;
  }

  function renderMap(body) {
    if (!coverage) { body.append(node("p", "불러오는 중…", "sim-muted")); return; }
    body.append(node("p", "칸 = 상태 기계의 상태 × 그 기계의 규칙이 받는 사건. 초록: 규칙 있음 · 빨강: 걸음이 도달했는데 규칙 없음(열림) · 노랑: 질문/답 · 회색: 제외·해결 · 빈칸: 아직 도달 안 함.", "sim-muted"));
    const handled = new Set(coverage.handled || []);
    for (const g of coverage.grid || []) {
      const t = node("table", null, "sim-map");
      const hr = node("tr"); hr.append(node("th", g.machine));
      for (const e of g.events) hr.append(node("th", e));
      t.append(hr);
      for (const st of g.states) {
        const tr = node("tr"); tr.append(node("th", st));
        for (const e of g.events) {
          const k = `${g.machine}\u0000${st}\u0000${e}`;
          const gap = coverage.gaps?.[k];
          const cls = handled.has(k) ? "ok" : gap === "open" ? "gap" : gap === "question" || gap === "answered" ? "ask" : gap ? "done" : "none";
          const td = node("td", cls === "ok" ? "✓" : cls === "gap" ? "!" : cls === "ask" ? "?" : cls === "done" ? "–" : "", "sim-cell-" + cls);
          td.title = `${st} × ${e}` + (gap ? ` (${STATUS_LABEL[gap] || gap})` : "");
          tr.append(td);
        }
        t.append(tr);
      }
      const wrap = node("div", null, "sim-scroll"); wrap.append(t); body.append(wrap);
    }
    if (!(coverage.grid || []).length) body.append(node("p", "상태 기계가 없습니다.", "sim-empty"));
  }

  // The editor's proposals: never applied by the daemon; an accepted one goes to the owner session.
  function renderProposals(body, base) {
    if (!proposals) { body.append(node("p", "불러오는 중…", "sim-muted")); return; }
    body.append(node("p", "편집 제안은 원천 파일에 적용되지 않은 상태입니다. 수락하면 소유 세션에 적용 요청이 입력됩니다.", "sim-muted"));
    if (!proposals.length) { body.append(node("p", "편집 제안이 없습니다. 설정의 「최대 편집 제안」을 1 이상으로 두면 실행이 끝날 때 만들어집니다.", "sim-empty")); return; }
    for (const p of proposals) {
      const card = node("article", null, "sim-proposal sim-p-" + p.status);
      const h = node("header");
      h.append(node("span", PROPOSAL_LABEL[p.status] || p.status, "sim-badge"), node("strong", EDIT_KIND_LABEL[p.kind] || p.kind),
        node("code", p.file), node("span", p.id, "sim-muted"));
      card.append(h, node("p", p.reason), node("p", "해결하는 발견: " + p.findings.join(", "), "sim-muted"));
      const diff = node("div", null, "sim-diff");
      if (p.old) diff.append(node("pre", p.old, "sim-del"));
      else diff.append(node("p", "파일 끝에 추가", "sim-muted"));
      diff.append(node("pre", p.new, "sim-add"));
      card.append(diff);
      if (p.check?.breaks?.length) card.append(node("p", "이 편집을 적용하면 인용을 잃는 규칙: " + p.check.breaks.join(", "), "sim-warn"));
      if (p.decision?.note) card.append(node("p", "메모: " + p.decision.note, "sim-muted"));
      if (p.status === "proposed") {
        const note = node("input"); note.placeholder = "메모 (선택)"; note.disabled = busy;
        note.value = proposalNotes[p.id] || ""; note.oninput = () => { proposalNotes[p.id] = note.value; };
        const decide = accept => act(() => request(base + "/proposals/" + enc(p.id) + "/decide", "POST", {accept, note: proposalNotes[p.id] || ""}),
          r => { delete proposalNotes[p.id]; return accept ? (r.nudged ? "수락했고 소유 세션에 적용 요청을 입력했습니다" : "수락했습니다 (소유 세션 없음)") : "거절했습니다"; });
        const row = node("div", null, "sim-row");
        row.append(note, button("수락", () => decide(true), "sim-primary"), button("거절", () => decide(false)));
        card.append(row);
      }
      body.append(card);
    }
  }

  function renderModel(body) {
    if (!model) { body.append(node("p", "불러오는 중…", "sim-muted")); return; }
    const section = (title, rows, fmt) => {
      body.append(node("h3", `${title} (${rows.length})`));
      const ul = node("ul", null, "sim-model");
      for (const r of rows) ul.append(fmt(r));
      body.append(ul);
    };
    section("상태 기계", model.machines, m => node("li", `${m.id}: ${m.states.join(" · ")} (처음 ${m.initial})`));
    section("변수", model.vars, v => node("li", `${v.name} = ${JSON.stringify(v.init)}` + (v.domain ? ` ∈ ${JSON.stringify(v.domain)}` : "")));
    section("사건", model.events, e => node("li", `${e.name} [${e.kind || "actor"}]` + (e.available ? " — 조건 " + JSON.stringify(e.available) : "")));
    section("규칙", model.rules, r => {
      const li = node("li");
      li.append(node("strong", r.id), node("span", ` on ${r.on?.event}` + (r.status && r.status !== "proposed" ? ` [${r.status}]` : "")));
      if (r.when) li.append(node("div", "when " + JSON.stringify(r.when), "sim-code"));
      li.append(node("div", "do " + JSON.stringify(r.do || []), "sim-code"));
      if (r.provisional) li.append(node("div", "임시: " + r.provisional.why, "sim-warn"));
      for (const src of r.source || []) li.append(node("div", `${src.ref}: “${src.quote}”`, "sim-source"));
      return li;
    });
    section("불변식", model.invariants, i => node("li", `${i.id}: ${JSON.stringify(i.holds)}`));
  }

  function renderRounds(body) {
    const rows = [...(detail.rounds || [])].reverse();
    const t = node("table", null, "sim-table");
    const hr = node("tr");
    for (const h of ["라운드", "시각", "실행", "새", "다시 열림", "해결", "열림", "도달 칸", "걸음", "모델"]) hr.append(node("th", h));
    t.append(hr);
    for (const r of rows) {
      const tr = node("tr");
      for (const v of [r.round, (r.at || "").replace("T", " ").slice(0, 19), r.by, r.new, r.reopened, r.resolved, r.open,
        r.coverage ? `${r.coverage.reached}/${r.coverage.cells}` : "", r.steps, r.model]) tr.append(node("td", String(v ?? "")));
      t.append(tr);
    }
    const wrap = node("div", null, "sim-scroll"); wrap.append(t); body.append(wrap);
    if (!rows.length) body.append(node("p", "아직 라운드가 없습니다.", "sim-empty"));
  }

  function renderSettings(body, s, base) {
    if (!settingsDraft || settingsDraft.id !== s.id) settingsDraft = {id: s.id, ...s.settings, sources: (s.sources || []).join("\n")};
    const cur = settingsDraft;
    const form = node("form", null, "sim-form");
    form.append(profileFields(cur, v => { Object.assign(cur, v); render(); }));
    for (const [k, label] of [["max_rounds", "최대 라운드"], ["budget", "라운드당 걸음"], ["horizon", "한 번에 이어 가는 걸음"], ["seed", "씨앗"], ["max_edits", "최대 편집 제안 (0 = 만들지 않음)"]]) {
      const l = node("label", label); const i = node("input"); i.type = "number"; i.min = k === "max_edits" ? "0" : "1"; i.value = cur[k] ?? 0;
      i.oninput = () => { cur[k] = parseInt(i.value, 10); }; i.disabled = busy; l.append(i); form.append(l);
    }
    const tri = node("label", null, "sim-check"); const cb = node("input"); cb.type = "checkbox"; cb.checked = cur.triage !== false;
    cb.disabled = busy; cb.setAttribute("aria-label", "판정자 분류");
    cb.onchange = () => { cur.triage = cb.checked; };
    tri.append(cb, node("span", " 해석 전에 판정자가 빈칸을 분류 (모델 오류 / 원천에 없음 / 결함 아님)")); form.append(tri);
    const src = node("label", "원천 (한 줄에 하나)"); const ta = node("textarea"); ta.value = cur.sources; ta.oninput = () => { cur.sources = ta.value; }; ta.disabled = busy; src.append(ta); form.append(src);
    const submit = node("button", "저장", "wf-btn sim-primary"); submit.disabled = busy; form.append(submit);
    form.append(node("p", "바꾼 설정은 다음 라운드와 다음 실행부터 적용됩니다.", "sim-muted"));
    form.onsubmit = e => {
      e.preventDefault();
      const settings = {};
      for (const k of [...MODEL_KEYS, "max_rounds", "budget", "horizon", "seed", "max_edits"]) settings[k] = cur[k] ?? 0;
      settings.triage = cur.triage !== false;
      act(() => request(base + "/settings", "POST", {settings, sources: cur.sources.split("\n").map(x => x.trim()).filter(Boolean)}),
        () => { settingsDraft = null; return "저장했습니다"; });
    };
    body.append(form);
  }

  function renderJournal(body) {
    const ul = node("ul", null, "sim-journal");
    for (const j of [...(detail.journal || [])].reverse()) {
      const {at, by, event, ...rest} = j;
      ul.append(node("li", `${(at || "").replace("T", " ").slice(0, 19)} ${by} ${event} ${Object.keys(rest).length ? JSON.stringify(rest) : ""}`));
    }
    body.append(ul);
  }

  function open(id = "", section = "") {
    if (id !== simId) { detail = findings = model = coverage = settingsDraft = null; notice = ""; nudgeDraft = transferTo = ""; replays = {}; proposals = null; proposalNotes = {}; }
    simId = id || ""; if (section) tab = section; else if (!id) tab = "findings";
    render(); load();
    clearInterval(timer);
    timer = setInterval(() => { if (!busy) load(); }, 5000);
  }
  function stop() { clearInterval(timer); timer = null; seq++; }
  return {open, stop};
})();
