/* Workspace terms and human review at #/glossary. All user text uses textContent. */
globalThis.GlossaryPage = (() => {
  const node = (tag, text, cls) => {
    const n = document.createElement(tag);
    if (text != null) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  };
  let workspace = "", query = "", status = "pending", sequence = 0;
  let workspaces = [], terms = [], proposals = [], error = "", busy = false;
  let draft = null, preview = null, tab = "terms";
  const view = () => document.getElementById("glossary-view");
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
      const [a, b] = await Promise.all([
        request(url("/api/glossary", {q: query, limit: 100})),
        request(url("/api/glossary/suggestions", {status})),
      ]);
      if (seq !== sequence) return;
      terms = a.terms; proposals = b.suggestions; error = "";
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
  function field(form, title, key, multiline = false) {
    const label = node("label", title);
    const input = node(multiline ? "textarea" : "input");
    input.name = key; input.value = key === "aliases" ? draft.aliases.join("\n") : draft[key] || "";
    input.required = key === "term" || key === "definition";
    input.maxLength = key === "definition" ? 10000 : key === "source" ? 2000 : key === "aliases" ? 10050 : 200;
    input.disabled = busy;
    input.oninput = () => { draft[key] = key === "aliases" ? input.value.split("\n").filter(s => s.trim()) : input.value; };
    input.id = "glossary-" + key;
    label.append(input); form.append(label);
  }
  function edit(row) {
    draft = row ? {...row, aliases: [...row.aliases]} : {term: "", definition: "", aliases: [], source: ""};
    tab = "terms"; render();
    document.getElementById("glossary-term")?.focus?.();
  }
  function empty(title, description) {
    const n = node("div", null, "glossary-empty");
    n.append(node("span", "Aa", "glossary-empty-symbol"), node("h3", title), node("p", description));
    return n;
  }
  function render() {
    const root = view(); if (!root) return;
    root.replaceChildren();
    const shell = node("div", null, "glossary-shell"); root.append(shell);
    const head = node("header", null, "glossary-head");
    const title = node("div");
    title.append(node("span", "WORKSPACE GLOSSARY", "glossary-eyebrow"), node("h2", "같은 표현, 명확한 의미"),
      node("p", "함께 사용하는 용어와 해석을 정리하고 대화에 적용하세요.", "glossary-muted"));
    const scope = node("label", null, "glossary-scope"); scope.append(node("span", "Workspace"));
    const picker = node("select"); picker.setAttribute("aria-label", "Workspace"); picker.disabled = busy;
    for (const w of workspaces) { const o = node("option", w.name); o.value = w.name; picker.append(o); }
    picker.value = workspace;
    picker.onchange = () => { workspace = picker.value; draft = preview = null; terms = proposals = []; render(); load(); };
    scope.append(picker); head.append(title, scope); shell.append(head);
    if (error) { const alert = node("p", error, "glossary-error"); alert.setAttribute("role", "alert"); shell.append(alert); }
    if (!workspaces.length) { shell.append(empty("Workspace를 먼저 등록하세요", "Settings → Workspaces에서 용어집을 사용할 작업 위치를 추가하세요.")); return; }
    const nav = node("nav", null, "glossary-tabs"); nav.setAttribute("aria-label", "용어집 작업");
    for (const [key, label] of [["terms", "등록된 용어"], ["review", "제안 검토"], ["reference", "참조 확인"]]) {
      const b = button(label, () => { tab = key; render(); }, tab === key ? "is-active" : "");
      b.setAttribute("aria-pressed", String(tab === key)); nav.append(b);
    }
    nav.append(button("새로고침", load, "glossary-refresh")); shell.append(nav);
    if (tab === "reference") { shell.append(referenceForm()); return; }
    const layout = node("div", null, "glossary-layout" + (draft && tab === "terms" ? " is-editing" : ""));
    const main = node("section", null, "glossary-main");
    const toolbar = node("div", null, "glossary-toolbar");
    if (tab === "terms") {
      const searchForm = node("form", null, "glossary-search");
      const search = node("input"); search.value = query; search.placeholder = "용어 또는 별칭 검색…";
      search.setAttribute("aria-label", "용어 검색"); search.disabled = busy;
      search.oninput = () => { query = search.value; };
      const submit = node("button", "검색", "wf-btn"); submit.disabled = busy;
      searchForm.append(search, submit); searchForm.onsubmit = e => { e.preventDefault(); load(); };
      toolbar.append(searchForm, button("용어 추가", () => edit(), "glossary-primary"));
      main.append(toolbar, node("p", `검색 결과 ${terms.length}개 · 최대 100개 표시`, "glossary-list-meta"));
      if (!terms.length) main.append(empty(query ? "검색 결과가 없습니다" : "첫 용어를 등록하세요",
        query ? "다른 표기나 별칭으로 검색해 보세요." : "정의와 별칭을 등록하면 세션 입력창에서 바로 찾아 사용할 수 있습니다."));
      for (const row of terms) {
        const card = termCard(row);
        const footer = node("div", null, "glossary-card-footer");
        const code = node("code", row.reference); code.title = "세션 입력창에서 사용할 수 있는 고정 참조";
        const actions = node("div", null, "glossary-actions");
        actions.append(button("편집", () => edit(row)), button("삭제", () => {
          if (globalThis.confirm(`“${row.term}” 용어를 삭제하시겠습니까?`))
            act(() => request(url("/api/glossary/" + row.id), "DELETE"), () => { if (draft?.id === row.id) draft = null; });
        }, "glossary-danger"));
        footer.append(code, actions); card.append(footer); main.append(card);
      }
    } else {
      toolbar.append(node("h3", "제안 검토"));
      const filter = node("select"); filter.setAttribute("aria-label", "제안 상태"); filter.disabled = busy;
      for (const [value, label] of [["pending", "검토 대기"], ["accepted", "수락됨"], ["rejected", "거절됨"], ["all", "전체"]]) {
        const option = node("option", label); option.value = value; filter.append(option);
      }
      filter.value = status; filter.onchange = () => { status = filter.value; load(); }; toolbar.append(filter);
      main.append(toolbar, node("p", `${proposals.length}개의 제안 · 수락하면 용어집 검색에 포함됩니다.`, "glossary-list-meta"));
      if (!proposals.length) main.append(empty("해당 상태의 제안이 없습니다", "에이전트가 MCP로 제안한 용어를 이곳에서 검토할 수 있습니다."));
      for (const row of proposals) {
        const card = termCard(row);
        const footer = node("div", null, "glossary-card-footer");
        const labels = {pending: "검토 대기", accepted: "수락됨", rejected: "거절됨"};
        footer.append(node("span", `${labels[row.status]} · ${row.proposed_by || "MCP"} · ${row.created_at.slice(0, 10)}`, "glossary-muted"));
        const actions = node("div", null, "glossary-actions");
        if (row.status === "pending") for (const [decision, label] of [["reject", "거절"], ["accept", "수락"]])
          actions.append(button(label, () => act(() => request(url(`/api/glossary/suggestions/${row.id}/review`), "POST", {decision})), decision === "accept" ? "glossary-primary" : ""));
        footer.append(actions); card.append(footer); main.append(card);
      }
    }
    const aside = node("aside", null, "glossary-aside");
    if (draft && tab === "terms") aside.append(editor());
    else {
      const help = node("div", null, "glossary-help");
      help.append(node("span", "대화에서 활용하기", "glossary-eyebrow"), node("h3", "입력한 표현의 의미를 선택하세요"),
        node("p", "세션 입력창에 용어를 쓰면 감지된 표현이 표시됩니다. 원하는 정의를 선택하면 메시지와 함께 전달됩니다."),
        node("code", "[[term:자세히]]"), node("p", "명시적으로 참조를 입력하거나, 입력창의 ‘용어집’ 버튼으로 검색할 수도 있습니다."));
      aside.append(help);
      const meaning = node("div", null, "glossary-help glossary-help-secondary");
      meaning.append(node("h3", "하나의 표현에 여러 의미가 있다면"), node("p", "‘자세히 · 구체적 설명’과 ‘자세히 · 추가 탐색’을 각각 등록하고, 두 항목의 별칭에 ‘자세히’를 넣으세요. 입력할 때 의도에 맞는 정의를 고를 수 있습니다."));
      aside.append(meaning);
    }
    layout.append(main, aside); shell.append(layout);
  }
  function editor() {
    const form = node("form", null, "glossary-form glossary-editor");
    const heading = node("div", null, "glossary-toolbar");
    heading.append(node("h3", draft.id ? "용어 편집" : "새 용어"), button("취소", () => { draft = null; render(); }));
    form.append(heading, node("p", "의미를 구분할 수 있는 이름과 정의를 작성하세요.", "glossary-muted"));
    field(form, "용어 이름", "term"); field(form, "정의", "definition", true);
    field(form, "별칭 · 줄마다 하나", "aliases", true); field(form, "출처 또는 사용 맥락 (선택)", "source", true);
    const save = node("button", "저장", "wf-btn glossary-primary"); save.disabled = busy;
    form.append(save);
    form.onsubmit = e => {
      e.preventDefault(); const body = {...draft};
      act(() => request(url("/api/glossary" + (body.id ? "/" + body.id : "")), body.id ? "PUT" : "POST", body), () => { draft = null; });
    };
    return form;
  }
  function referenceForm() {
    const form = node("form", null, "glossary-form glossary-reference");
    form.append(node("span", "QUASI-QUOTATION", "glossary-eyebrow"), node("h3", "문장의 용어 참조 확인"),
      node("p", "[[term:용어]] 또는 [[term:#ID]]를 포함한 문장을 입력하세요. 참조에 연결된 정의를 확인할 수 있습니다.", "glossary-muted"));
    const text = node("textarea"); text.value = preview?.text || ""; text.placeholder = "[[term:자세히]] 설명해 주세요.";
    text.setAttribute("aria-label", "용어 참조 원문"); text.maxLength = 20000; text.disabled = busy;
    text.oninput = () => { preview = {text: text.value}; };
    const resolve = node("button", "참조 확인", "wf-btn glossary-primary"); resolve.disabled = busy;
    form.append(text, resolve);
    form.onsubmit = e => { e.preventDefault(); const value = text.value;
      act(async () => { preview = await request(url("/api/glossary/resolve"), "POST", {text: value}); }); };
    if (preview?.references) {
      if (!preview.references.length) form.append(node("p", "참조 표기가 없습니다.", "glossary-list-meta"));
      for (const ref of preview.references) {
        const result = node("div", null, "glossary-reference-result");
        const labels = {resolved: "정의 확인됨", ambiguous: "의미 선택 필요", missing: "일치하는 용어 없음"};
        result.append(node("code", ref.literal), node("span", labels[ref.status], "glossary-badge"));
        if (ref.term) result.append(termCard(ref.term));
        for (const candidate of ref.candidates || []) result.append(termCard(candidate));
        form.append(result);
      }
    }
    return form;
  }
  function termCard(row) {
    const card = node("article", null, "glossary-card");
    card.append(node("h4", row.term), node("p", row.definition, "glossary-definition"));
    if (row.aliases.length) {
      const tags = node("div", null, "glossary-aliases");
      for (const alias of row.aliases) tags.append(node("span", alias));
      card.append(tags);
    }
    if (row.source) card.append(node("p", row.source, "glossary-source"));
    if (row.match) card.append(node("p", `${{exact: "정확 일치", substring: "부분 일치", fuzzy: "유사 표기"}[row.match]} · ${row.matched_label}`, "glossary-match"));
    return card;
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
