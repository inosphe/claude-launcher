/* The plan page at #/plan (claunch-7jzp8.2): every running session's stage
   on one screen, from /api/plan/overview (plan/overview.py) -- what a person
   owes it, its cflow v2 run's current task, its plan's progress and whether
   it may close. Grouped by stage (the default) or by joint tree
   (/api/plan/joint, plan/joint.py), narrowed by project and stage. Each row
   links to its session; the decisions themselves are taken in the session's
   ☰ plan pane. With a project picked, the page shows that project's
   aggregation session (claunch-7jzp8.3): its plan -- the project plan -- in
   a plan pane decided here, or a start button when none runs. All session
   text uses textContent. */
const PLAN_STAGE = {
  user: "내 차례",
  blocked: "막힘",
  closable: "닫아도 됨",
  working: "진행 중",
  idle: "계획·런 없음",
};

function makePlanPage({root, request}) {
  const node = (tag, text, cls) => {
    const n = document.createElement(tag);
    if (text != null) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  };
  let data = null, joint = null, error = "", seq = 0, timer = null;
  let everyone = false, by = "stage", project = "", only = "";
  let profiles = null, profile = "", starting = false, startError = "", agg = null;

  async function load() {
    const mine = ++seq;
    try {
      const [o, j] = await Promise.all([
        request("/api/plan/overview" + (everyone ? "?all=1" : "")),
        by === "joint" ? request("/api/plan/joint") : Promise.resolve(null),
      ]);
      if (mine !== seq) return;
      data = o; joint = j; error = "";
    } catch (exc) { if (mine === seq) error = exc.message; }
    if (mine === seq) { render(); clearTimeout(timer); timer = setTimeout(load, 10000); }
  }

  function button(label, fn, pressed) {
    const b = node("button", label, "wf-btn" + (pressed ? " plan-page-on" : ""));
    b.type = "button"; b.onclick = fn;
    b.setAttribute("aria-pressed", pressed ? "true" : "false");
    return b;
  }

  function line(r) {
    const li = node("li", null, `plan-page-row plan-stage-${r.stage}`);
    const head = node("div", null, "plan-item-head");
    const link = node("a", r.session, "plan-page-session");
    link.href = "#/s/" + encodeURIComponent(r.session);
    head.append(node("span", PLAN_STAGE[r.stage] || r.stage, "plan-chip"), link);
    if (r.project) head.append(node("span", r.project, "plan-muted"));
    if (r.aggregator) head.append(node("span", "집계", "plan-chip"));
    if (r.status === "exited") head.append(node("span", r.category, "plan-muted"));
    li.append(head);
    const p = r.plan, run = r.run;
    if (p) {
      li.append(node("p", (p.title || "") + ` — 단계 ${p.done || 0}/${p.steps || 0}`
        + (p.current ? ` · 지금: ${p.current}` : "")
        + (p.waiting ? ` · 내 결정 대기 ${p.waiting}건` : "")
        + (p.blocked ? ` · 막힌 단계 ${p.blocked}` : "")
        + (p.closable ? " · 닫아도 됨" : ""), "plan-page-plan"));
    }
    if (run) {
      li.append(node("p", `${run.workflow || "런"}` + (run.round ? ` r${run.round}` : "")
        + ((run.current || []).length ? ` · ${run.current.join(", ")}` : "")
        + (run.status && run.status !== "running" ? ` · ${run.status}` : ""), "plan-muted"));
      for (const o of run.owed || []) li.append(node("p", "런이 기다림: " + o, "plan-open"));
    }
    return li;
  }

  function filtered() {
    return (data.rows || []).filter(r => (!project || r.project === project) && (!only || r.stage === only));
  }

  function byStage(rows) {
    const out = [];
    for (const stage of Object.keys(PLAN_STAGE)) {
      const mine = rows.filter(r => r.stage === stage);
      if (!mine.length) continue;
      out.push(node("h4", `${PLAN_STAGE[stage]} (${mine.length})`, "plan-section"));
      const ul = node("ul", null, "plan-page-list");
      for (const r of mine) ul.append(line(r));
      out.push(ul);
    }
    return out;
  }

  function byJoint(rows) {
    const index = Object.fromEntries(rows.map(r => [r.session, r]));
    const placed = new Set(), out = [];
    for (const g of (joint && joint.groups) || []) {
      const members = g.members.filter(m => index[m.session]);
      if (!members.length) continue;
      out.push(node("h4", `${g.root} · 세션 ${members.length}개 · 내 결정 대기 ${g.waiting}건`, "plan-section"));
      const ul = node("ul", null, "plan-page-list");
      for (const m of members) {
        const li = line(index[m.session]);
        li.setAttribute("style", `margin-left: ${m.depth * 1.2}em`);
        ul.append(li); placed.add(m.session);
      }
      out.push(ul);
    }
    const rest = rows.filter(r => !placed.has(r.session));
    if (rest.length) {
      out.push(node("h4", `계획 없는 세션 (${rest.length})`, "plan-section"));
      const ul = node("ul", null, "plan-page-list");
      for (const r of rest) ul.append(line(r));
      out.push(ul);
    }
    return out;
  }

  async function loadProfiles() {
    if (profiles) return;
    profiles = [];
    try {
      const f = await request("/api/profiles");
      profiles = (f.profile_options || []).map(o => o.value).filter(Boolean);
      if (!profiles.length) profiles = (f.profiles || []).map(x => typeof x === "string" ? x : x.name).filter(Boolean);
    } catch (exc) { startError = exc.message; }
    profile = profile || profiles[0] || "";
    render();
  }

  async function start() {
    if (starting || !profile) return;
    starting = true; startError = ""; render();
    try {
      await request(`/api/plan/aggregators/${encodeURIComponent(project)}/start`, "POST", {profile});
    } catch (exc) { startError = exc.message; }
    starting = false;
    load();
  }

  // The aggregation session's pane is kept across the 10 s redraws: it reads
  // and polls on its own, and a decision being typed into it stays.
  function paneOf(session) {
    if (agg && agg.session === session) return agg.el;
    stopPane();
    const el = node("div", null, "plan-host");
    if (typeof makePlanPane !== "function") { el.textContent = "plan 패널을 불러오지 못했습니다."; return el; }
    agg = {session, el, view: makePlanPane({root: () => el, session, request, nested: true})};
    agg.view.open();
    return el;
  }
  function stopPane() { if (agg && agg.view) agg.view.stop(); agg = null; }

  function aggregatorBlock() {
    const box = node("div", null, "plan-page-agg");
    const aggs = data.aggregators || [];
    box.append(node("h4", "프로젝트 집계 세션", "plan-section"));
    if (!project) {
      stopPane();
      const running = aggs.filter(a => a.running);
      box.append(node("p", running.length ? running.map(a => `${a.project}: ${a.session}`).join(" · ")
        : "실행 중인 집계 세션이 없습니다.", "plan-muted"));
      box.append(node("p", "프로젝트를 고르면 그 프로젝트의 집계 계획을 보고 결정하거나 집계 세션을 시작할 수 있습니다.",
        "plan-muted"));
      return box;
    }
    const mine = aggs.find(a => a.project === project && a.running);
    if (mine) {
      const head = node("div", null, "plan-item-head");
      const link = node("a", mine.session, "plan-page-agg-session");
      link.href = "#/s/" + encodeURIComponent(mine.session);
      head.append(node("span", "집계", "plan-chip"), link);
      box.append(head, paneOf(mine.session));
      return box;
    }
    stopPane();
    box.append(node("p", `${project}에는 실행 중인 집계 세션이 없습니다. 시작하면 이 프로젝트 세션들의 계획을 `
      + "읽어 프로젝트 계획 하나를 쓰고, 여러 세션에 걸친 결정을 여기에 올립니다.", "plan-muted"));
    const row = node("div", null, "plan-joint-bar");
    if (!profiles) loadProfiles();
    const pick = node("select"); pick.setAttribute("aria-label", "집계 세션 프로필");
    for (const v of profiles || []) { const o = node("option", v); o.value = v; o.selected = v === profile; pick.append(o); }
    pick.onchange = () => { profile = pick.value; };
    const go = button(starting ? "시작 중…" : "집계 세션 시작", start, false);
    go.disabled = starting || !profile;
    row.append(pick, go);
    box.append(row);
    if (startError) { const a = node("p", startError, "plan-error"); a.setAttribute("role", "alert"); box.append(a); }
    return box;
  }

  function render() {
    const el = root(); if (!el) return;
    el.replaceChildren();
    const shell = node("div", null, "plan-pane plan-page");
    shell.append(node("h3", "세션 진행 개요"));
    if (error) { const a = node("p", error, "plan-error"); a.setAttribute("role", "alert"); shell.append(a); }
    if (!data) { shell.append(node("p", error ? "" : "loading…", "wf-note")); el.append(shell); return; }

    const bar = node("div", null, "plan-joint-bar");
    for (const [stage, label] of Object.entries(PLAN_STAGE)) {
      bar.append(button(`${label} ${data.counts[stage] || 0}`, () => { only = only === stage ? "" : stage; render(); },
                        only === stage));
    }
    shell.append(bar);

    const tools = node("div", null, "plan-joint-bar");
    const pick = node("select"); pick.setAttribute("aria-label", "프로젝트");
    const projects = [...new Set((data.rows || []).map(r => r.project).filter(Boolean))].sort();
    for (const v of ["", ...projects]) {
      const o = node("option", v || "모든 프로젝트"); o.value = v; o.selected = v === project; pick.append(o);
    }
    pick.onchange = () => { project = pick.value; render(); };
    tools.append(pick,
      button("단계별", () => { by = "stage"; render(); }, by === "stage"),
      button("연관 세션별", () => { by = "joint"; load(); }, by === "joint"),
      button("끝난 세션 포함", () => { everyone = !everyone; load(); }, everyone));
    shell.append(tools, aggregatorBlock());

    const rows = filtered();
    if (!rows.length) shell.append(node("p", "해당하는 세션이 없습니다.", "plan-muted"));
    else shell.append(...(by === "joint" ? byJoint(rows) : byStage(rows)));
    el.append(shell);
  }

  return {open: load, stop: () => { ++seq; clearTimeout(timer); timer = null; stopPane(); },
          _state: () => ({data, by, project, only})};
}

async function planPageRequest(path, method = "GET", body) {
  const response = await api(path, {method, ...(body ? {headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)} : {})});
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw Object.assign(Error(result.error || `HTTP ${response.status}`), {status: response.status});
  return result;
}

globalThis.PlanPage = (() => {
  let view = null;
  const host = () => document.getElementById("plan-view");
  function open(opts = {}) {
    if (view) view.stop();
    view = makePlanPage({root: host, request: opts.request || planPageRequest});
    view.open();
  }
  function stop() { if (view) view.stop(); view = null; }
  return {open, stop, _view: () => view};
})();
