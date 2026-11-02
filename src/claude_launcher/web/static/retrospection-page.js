/* Session retrospectives at #/retrospection (claunch-lmnlv). Each record is what a
   session wrote about its own work and the cflow definition it followed (the
   `retrospective` prompt pack), recorded from its wrap-up draft: at once when the run
   input retrospection_auto_record (or `claunch retro`'s default) said auto, or when the
   user confirmed it in the session's wrap-up pane. Read only: the page lists records
   newest first (/api/retrospection, body cut to an excerpt), filters by workspace,
   session and text, and opens one whole (/api/retrospection/<id>, #/retrospection/<id>).
   All user text uses textContent; a body goes through the page's markdown renderer
   (mdInto, app.js) when it is loaded. Layout borrows the glossary page's classes. */
globalThis.RetrospectionPage = (() => {
  const node = (tag, text, cls) => {
    const n = document.createElement(tag);
    if (text != null) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  };
  let workspace = "", session = "", query = "", sequence = 0;
  let facets = {workspaces: [], sessions: []}, records = [], selected = null, selectedId = "", error = "";
  const view = () => document.getElementById("retrospection-view");
  function markdown(target, text) {
    if (typeof globalThis.mdInto === "function") {
      target.classList.add("md");
      globalThis.mdInto(target, text || "");
    } else target.textContent = text || "";
    return target;
  }
  async function request(path) {
    const response = await api(path);
    const result = await response.json();
    if (!response.ok) throw Error(result.error || `HTTP ${response.status}`);
    return result;
  }
  async function load() {
    const seq = ++sequence;
    try {
      const params = new URLSearchParams({limit: "100"});
      if (workspace) params.set("workspace", workspace);
      if (session) params.set("session", session);
      if (query) params.set("q", query);
      const [list, f] = await Promise.all([request("/api/retrospection?" + params), request("/api/retrospection/facets")]);
      if (seq !== sequence) return;
      records = list.records || []; facets = f; error = "";
      if (selectedId && (!selected || selected.id !== selectedId)) await pick(selectedId, seq);
    } catch (exc) { if (seq === sequence) error = exc.message; }
    if (seq === sequence) render();
  }
  async function pick(id, seq = sequence) {
    selectedId = id;
    if (!id) { selected = null; return; }
    try {
      const result = await request("/api/retrospection/" + encodeURIComponent(id));
      if (seq === sequence && selectedId === id) { selected = result.record; error = ""; }
    } catch (exc) { if (seq === sequence) { selected = null; error = exc.message; } }
  }
  async function choose(id) {
    if (typeof location !== "undefined" && location.hash !== "#/retrospection/" + id) {
      selectedId = id; location.hash = "#/retrospection/" + encodeURIComponent(id);
      return;          // the router calls open(id)
    }
    const seq = ++sequence;
    await pick(id, seq);
    if (seq === sequence) render();
  }
  function select(label, options, value, onchange) {
    const s = node("select"); s.setAttribute("aria-label", label);
    for (const [v, text] of options) { const o = node("option", text); o.value = v; s.append(o); }
    s.value = value; s.onchange = () => onchange(s.value); return s;
  }
  function empty(title, description) {
    const n = node("div", null, "glossary-empty");
    n.append(node("span", "↺", "glossary-empty-symbol"), node("h3", title), node("p", description));
    return n;
  }
  const day = at => String(at || "").replace("T", " ").slice(0, 16);
  function meta(row) {
    return [row.trigger === "manual" ? "수동 회고" : "런 회고", row.session, row.workspace, row.workflow,
      row.issue, day(row.created_at), row.auto ? "자동 기록" : "사용자 확정"].filter(Boolean).join(" · ");
  }
  function card(row) {
    const c = node("article", null, "glossary-card retrospection-card" + (row.id === selectedId ? " is-selected" : ""));
    const h = node("h4");
    h.append(node("span", `문제 ${row.problems} · 제안 ${row.proposals}`, "glossary-badge"), node("span", " " + row.title));
    c.append(h, node("p", row.excerpt || "", "glossary-definition"));
    const footer = node("div", null, "glossary-card-footer");
    footer.append(node("code", row.id), node("span", meta(row), "glossary-muted"));
    const actions = node("div", null, "glossary-actions");
    const b = node("button", "열기", "wf-btn"); b.type = "button"; b.onclick = () => choose(row.id);
    actions.append(b); footer.append(actions); c.append(footer);
    return c;
  }
  function detail() {
    const box = node("article", null, "glossary-help retrospection-detail");
    const close = node("button", "닫기", "wf-btn"); close.type = "button";
    close.onclick = () => {
      selected = null; selectedId = "";
      if (typeof location !== "undefined") location.hash = "#/retrospection"; else render();
    };
    const head = node("div", null, "glossary-toolbar");
    head.append(node("h3", selected.title), close);
    box.append(head, node("p", meta(selected), "glossary-muted"),
      node("p", `문제 ${selected.problems} · 제안 ${selected.proposals}` + (selected.run ? ` · 런 ${selected.run}` : ""), "glossary-muted"),
      markdown(node("div", null, "glossary-definition retrospection-body"), selected.body));
    return box;
  }
  function render() {
    const root = view(); if (!root) return;
    root.replaceChildren();
    const shell = node("div", null, "glossary-shell"); root.append(shell);
    const head = node("header", null, "glossary-head");
    const title = node("div");
    title.append(node("span", "SESSION RETROSPECTION", "glossary-eyebrow"), node("h2", "세션 회고"),
      node("p", "세션이 자기 작업과 따른 cflow 정의를 되짚은 기록입니다. improv-worker-v2의 회고 노드와 claunch retro가 남깁니다.", "glossary-muted"));
    const scope = node("label", null, "glossary-scope"); scope.append(node("span", "Workspace"));
    scope.append(select("Workspace", [["", "전체 workspace"], ...facets.workspaces.map(w => [w.name, `${w.name} (${w.count})`])],
      workspace, v => { workspace = v; session = ""; load(); }));
    head.append(title, scope); shell.append(head);
    if (error) { const alert = node("p", error, "glossary-error"); alert.setAttribute("role", "alert"); shell.append(alert); }
    const layout = node("div", null, "glossary-layout" + (selected ? " is-editing" : ""));
    const main = node("section", null, "glossary-main");
    const toolbar = node("div", null, "glossary-toolbar");
    const form = node("form", null, "glossary-search");
    const search = node("input"); search.value = query; search.placeholder = "제목·본문·세션·이슈 검색…";
    search.setAttribute("aria-label", "회고 검색");
    search.oninput = () => { query = search.value; };
    const submit = node("button", "검색", "wf-btn");
    form.append(search, submit); form.onsubmit = e => { e.preventDefault(); load(); };
    const sessions = facets.sessions.filter(s => !workspace || s.workspace === workspace);
    toolbar.append(form, select("세션", [["", "전체 세션"], ...sessions.map(s => [s.name, `${s.name} (${s.count})`])],
      session, v => { session = v; load(); }));
    main.append(toolbar, node("p", `기록 ${records.length}개 · 최대 100개 표시`, "glossary-list-meta"));
    if (!records.length) main.append(empty(query || session || workspace ? "조건에 맞는 회고가 없습니다" : "아직 회고 기록이 없습니다",
      "세션이 끝날 때 improv-worker-v2의 회고 노드가, 또는 claunch retro <세션>이 기록을 남깁니다. 시작 입력 retrospection_auto_record가 꺼져 있으면 세션의 wrap-up pane에서 확정해야 기록됩니다."));
    for (const row of records) main.append(card(row));
    const aside = node("aside", null, "glossary-aside");
    if (selected) aside.append(detail());
    else {
      const help = node("div", null, "glossary-help");
      help.append(node("span", "기록 경로", "glossary-eyebrow"), node("h3", "런 회고와 수동 회고"),
        node("p", "런 회고: improv-worker-v2가 정산 뒤, 종료 결정 전에 retrospective 노드를 돈다. 시작 입력 retrospection_auto_record(기본 켜짐)가 켜져 있으면 바로 기록되고, 꺼져 있으면 wrap-up pane의 Retrospection 탭에서 확정할 때 기록된다."),
        node("p", "수동 회고: claunch retro <세션>(또는 세션 화면의 retro 버튼)이 보낸 회고는 바로 기록된다. claunch retro <세션> --review면 wrap-up pane에서 확정한다."),
        node("code", "wrapup_draft kind=retrospection"));
      aside.append(help);
    }
    layout.append(main, aside); shell.append(layout);
  }
  async function open(id = "") {
    selectedId = id || "";
    if (!selectedId) selected = null;
    render();
    await load();
  }
  return {open, stop: () => { ++sequence; }};
})();
