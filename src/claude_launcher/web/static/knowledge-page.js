/* Workspace knowledge records at #/knowledge (claunch-269q1). All user text uses textContent.
   Layout borrows the glossary page's classes; file records (.claunch/knowledge/*.md) are read only. */
globalThis.KnowledgePage = (() => {
  const KINDS = [["vision", "비전"], ["need", "니즈"], ["decision", "의사결정"], ["principle", "원칙"], ["lesson", "교훈"]];
  const kindLabel = Object.fromEntries(KINDS);
  const node = (tag, text, cls) => {
    const n = document.createElement(tag);
    if (text != null) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  };
  let workspace = "", query = "", kind = "", status = "active", sequence = 0;
  let workspaces = [], records = [], error = "", busy = false, draft = null;
  const view = () => document.getElementById("knowledge-view");
  const url = (path, params = {}) => path + "?" + new URLSearchParams({workspace, ...params});
  async function request(path, method = "GET", body) {
    const response = await api(path, {method, ...(body ? {headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)} : {})});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || `HTTP ${response.status}`);
    return result;
  }
  async function load() {
    const seq = ++sequence;
    try {
      const result = await request(url("/api/knowledge", {q: query, kind, status, limit: 100}));
      if (seq !== sequence) return;
      records = result.records; error = "";
    } catch (exc) { if (seq === sequence) error = exc.message; }
    if (seq === sequence) render();
  }
  async function act(action, after = () => {}) {
    if (busy) return;
    busy = true; error = ""; render();
    try { await action(); after(); await load(); }
    catch (exc) { error = exc.message; }
    finally { busy = false; render(); }
  }
  function button(text, action, style = "") {
    const b = node("button", text, "wf-btn " + style); b.type = "button";
    b.disabled = busy; b.onclick = action; return b;
  }
  function select(label, options, value, onchange) {
    const s = node("select"); s.setAttribute("aria-label", label); s.disabled = busy;
    for (const [v, text] of options) { const o = node("option", text); o.value = v; s.append(o); }
    s.value = value; s.onchange = () => onchange(s.value); return s;
  }
  function edit(row) {
    draft = row ? {...row, tags: [...row.tags], refs: [...row.refs]}
      : {kind: "decision", title: "", body: "", tags: [], refs: [], source: ""};
    render();
  }
  function empty(title, description) {
    const n = node("div", null, "glossary-empty");
    n.append(node("span", "◆", "glossary-empty-symbol"), node("h3", title), node("p", description));
    return n;
  }
  function render() {
    const root = view(); if (!root) return;
    root.replaceChildren();
    const shell = node("div", null, "glossary-shell"); root.append(shell);
    const head = node("header", null, "glossary-head");
    const title = node("div");
    title.append(node("span", "WORKSPACE KNOWLEDGE", "glossary-eyebrow"), node("h2", "결정과 그 이유"),
      node("p", "의사결정·비전·니즈·원칙·교훈을 기록하고 세션이 작업 전에 참조합니다.", "glossary-muted"));
    const scope = node("label", null, "glossary-scope"); scope.append(node("span", "Workspace"));
    const picker = select("Workspace", workspaces.map(w => [w.name, w.name]), workspace, v => {
      workspace = v; draft = null; records = []; render(); load();
    });
    scope.append(picker); head.append(title, scope); shell.append(head);
    if (error) { const alert = node("p", error, "glossary-error"); alert.setAttribute("role", "alert"); shell.append(alert); }
    if (!workspaces.length) { shell.append(empty("Workspace를 먼저 등록하세요", "Settings → Workspaces에서 작업 위치를 추가하세요.")); return; }
    const layout = node("div", null, "glossary-layout" + (draft ? " is-editing" : ""));
    const main = node("section", null, "glossary-main");
    const toolbar = node("div", null, "glossary-toolbar");
    const searchForm = node("form", null, "glossary-search");
    const search = node("input"); search.value = query; search.placeholder = "제목·태그·본문 검색…";
    search.setAttribute("aria-label", "기록 검색"); search.disabled = busy;
    search.oninput = () => { query = search.value; };
    const submit = node("button", "검색", "wf-btn"); submit.disabled = busy;
    searchForm.append(search, submit); searchForm.onsubmit = e => { e.preventDefault(); load(); };
    toolbar.append(searchForm,
      select("종류", [["", "전체 종류"], ...KINDS], kind, v => { kind = v; load(); }),
      select("상태", [["active", "유효"], ["superseded", "대체됨"], ["all", "전체"]], status, v => { status = v; load(); }),
      button("기록 추가", () => edit(), "glossary-primary"));
    const exportLink = node("a", "Markdown 내보내기"); exportLink.href = url("/api/knowledge/export");
    exportLink.target = "_blank"; exportLink.rel = "noopener";
    main.append(toolbar, node("p", `기록 ${records.length}개 · 최대 100개 표시`, "glossary-list-meta"), exportLink);
    if (!records.length) main.append(empty(query ? "검색 결과가 없습니다" : "첫 기록을 남기세요",
      query ? "다른 낱말로 검색해 보세요." : "저장소의 .claunch/knowledge/*.md 파일이나 이 페이지, MCP knowledge_record로 기록이 쌓입니다."));
    for (const row of records) main.append(card(row));
    const aside = node("aside", null, "glossary-aside");
    if (draft) aside.append(editor());
    else {
      const help = node("div", null, "glossary-help");
      help.append(node("span", "두 가지 출처", "glossary-eyebrow"), node("h3", "파일 기록과 세션 기록"),
        node("p", "파일 기록은 저장소의 .claunch/knowledge/*.md이며 git 리뷰를 거쳐 바뀝니다. 이 페이지에서는 읽기만 합니다."),
        node("p", "세션 기록은 improv-worker-v2의 knowledge-record 노드(시작 입력 knowledge_record)와 이 페이지에서 남깁니다."),
        node("code", "knowledge_search · knowledge_record"));
      aside.append(help);
    }
    layout.append(main, aside); shell.append(layout);
  }
  function card(row) {
    const c = node("article", null, "glossary-card knowledge-card");
    const badge = node("span", kindLabel[row.kind] || row.kind, "glossary-badge");
    const h = node("h4"); h.append(badge, node("span", " " + row.title));
    c.append(h, node("p", row.body, "glossary-definition knowledge-body"));
    const tags = [...row.tags, ...row.refs];
    if (tags.length) {
      const t = node("div", null, "glossary-aliases");
      for (const tag of tags) t.append(node("span", tag));
      c.append(t);
    }
    const meta = [row.origin === "file" ? row.path : "세션 기록", row.author, (row.updated_at || "").slice(0, 10),
      row.status === "superseded" ? "대체됨" : ""].filter(Boolean).join(" · ");
    const footer = node("div", null, "glossary-card-footer");
    const id = node("code", row.id); id.title = "기록 id (supersedes에 쓴다)";
    footer.append(id, node("span", meta, "glossary-muted"));
    if (row.origin !== "file") {
      const actions = node("div", null, "glossary-actions");
      actions.append(button("편집", () => edit(row)), button("삭제", () => {
        if (globalThis.confirm(`“${row.title}” 기록을 삭제하시겠습니까?`))
          act(() => request(url("/api/knowledge/" + row.id), "DELETE"), () => { if (draft?.id === row.id) draft = null; });
      }, "glossary-danger"));
      footer.append(actions);
    }
    c.append(footer);
    return c;
  }
  function field(form, title, key, multiline = false) {
    const label = node("label", title);
    const input = node(multiline ? "textarea" : "input");
    const list = key === "tags" || key === "refs";
    input.name = key; input.value = list ? draft[key].join("\n") : draft[key] || "";
    input.required = key === "title" || key === "body";
    input.disabled = busy;
    input.oninput = () => { draft[key] = list ? input.value.split("\n").map(s => s.trim()).filter(Boolean) : input.value; };
    label.append(input); form.append(label);
  }
  function editor() {
    const form = node("form", null, "glossary-form glossary-editor");
    const heading = node("div", null, "glossary-toolbar");
    heading.append(node("h3", draft.id ? "기록 편집" : "새 기록"), button("취소", () => { draft = null; render(); }));
    const kindPick = select("기록 종류", KINDS, draft.kind, v => { draft.kind = v; });
    kindPick.name = "kind";
    form.append(heading, kindPick);
    field(form, "제목", "title"); field(form, "본문 (markdown)", "body", true);
    field(form, "태그 · 줄마다 하나", "tags", true); field(form, "근거 · 이슈 id, 세션, 경로", "refs", true);
    field(form, "출처 (선택)", "source", true);
    const save = node("button", "저장", "wf-btn glossary-primary"); save.disabled = busy;
    form.append(save);
    form.onsubmit = e => {
      e.preventDefault(); const body = {...draft};
      act(() => request(url("/api/knowledge" + (body.id ? "/" + body.id : "")), body.id ? "PUT" : "POST", body), () => { draft = null; });
    };
    return form;
  }
  async function open() {
    const seq = ++sequence;
    try {
      const result = await request("/api/workspaces");
      if (seq !== sequence) return;
      workspaces = result.workspaces;
      if (!workspaces.some(w => w.name === workspace)) workspace = workspaces[0]?.name || "";
      if (workspace) await load(); else render();
    } catch (exc) { if (seq === sequence) { error = exc.message; render(); } }
  }
  return {open, stop: () => { ++sequence; }};
})();
