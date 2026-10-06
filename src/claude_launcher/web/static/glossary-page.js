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
  let draft = null, preview = null;
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
  function button(text, action) {
    const b = node("button", text, "wf-btn"); b.type = "button";
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
    label.append(input); form.append(label);
  }
  function render() {
    const root = view(); if (!root) return;
    root.replaceChildren();
    const head = node("div", null, "page-head"); head.append(node("h2", "용어집")); root.append(head);
    const controls = node("div", null, "glossary-controls");
    const picker = node("select"); picker.setAttribute("aria-label", "Workspace"); picker.disabled = busy;
    for (const w of workspaces) { const o = node("option", w.name); o.value = w.name; picker.append(o); }
    picker.value = workspace;
    picker.onchange = () => { workspace = picker.value; draft = preview = null; terms = proposals = []; load(); };
    controls.append(picker, button("새로고침", load)); root.append(controls);
    root.append(node("p", "MCP 제안은 검토 대기로 등록됩니다. 수락된 용어와 직접 추가한 용어가 검색에 포함됩니다.", "wf-note"));
    if (error) root.append(node("p", error, "glossary-error"));
    if (!workspaces.length) { root.append(node("p", "Settings → Workspaces에서 workspace를 등록하세요.")); return; }
    const panel = node("div", null, "glossary-columns");
    const accepted = node("section"); accepted.append(node("h3", "등록된 용어"));
    const searchForm = node("form", null, "glossary-controls");
    const search = node("input"); search.value = query; search.placeholder = "용어·별칭·유사 표기 검색";
    search.setAttribute("aria-label", "용어 검색"); search.disabled = busy;
    search.oninput = () => { query = search.value; };
    const submit = node("button", "검색", "wf-btn"); submit.disabled = busy;
    searchForm.append(search, submit); searchForm.onsubmit = e => { e.preventDefault(); load(); };
    accepted.append(searchForm, button("용어 추가", () => { draft = {term: "", definition: "", aliases: [], source: ""}; render(); }));
    accepted.append(node("p", `${terms.length}개 표시 · 최대 100개, 검색으로 범위를 좁힐 수 있습니다.`, "wf-note"));
    for (const row of terms) {
      const card = termCard(row);
      card.append(node("code", row.reference));
      card.append(button("편집", () => { draft = {...row, aliases: [...row.aliases]}; render(); }));
      card.append(button("삭제", () => {
        if (globalThis.confirm(`“${row.term}” 용어를 삭제하시겠습니까?`))
          act(() => request(url("/api/glossary/" + row.id), "DELETE"), () => { if (draft?.id === row.id) draft = null; });
      }));
      accepted.append(card);
    }
    const review = node("section"); review.append(node("h3", "제안 검토"));
    const filter = node("select"); filter.setAttribute("aria-label", "제안 상태"); filter.disabled = busy;
    for (const [value, label] of [["pending", "검토 대기"], ["accepted", "수락됨"], ["rejected", "거절됨"], ["all", "전체"]]) {
      const option = node("option", label); option.value = value; filter.append(option);
    }
    filter.value = status; filter.onchange = () => { status = filter.value; load(); }; review.append(filter);
    if (!proposals.length) review.append(node("p", "해당 상태의 제안이 없습니다.", "wf-note"));
    for (const row of proposals) {
      const card = termCard(row);
      card.append(node("p", `${row.status} · ${row.proposed_by} · ${row.created_at}`, "wf-note"));
      if (row.status === "pending") for (const [decision, label] of [["accept", "수락"], ["reject", "거절"]])
        card.append(button(label, () => act(() => request(url(`/api/glossary/suggestions/${row.id}/review`), "POST", {decision}))));
      review.append(card);
    }
    panel.append(accepted, review); root.append(panel);
    if (draft) {
      const form = node("form", null, "glossary-form");
      form.append(node("h3", draft.id ? "용어 편집" : "용어 추가"));
      field(form, "용어", "term"); field(form, "정의", "definition", true);
      field(form, "별칭 (줄마다 하나)", "aliases", true); field(form, "출처·사용 맥락", "source", true);
      const save = node("button", "저장", "wf-btn"); save.disabled = busy;
      form.append(save, button("취소", () => { draft = null; render(); }));
      form.onsubmit = e => {
        e.preventDefault(); const body = {...draft};
        act(() => request(url("/api/glossary" + (body.id ? "/" + body.id : "")), body.id ? "PUT" : "POST", body), () => { draft = null; });
      };
      root.append(form);
    }
    const refForm = node("form", null, "glossary-form");
    refForm.append(node("h3", "명시적 용어 참조 (quasi-quotation)"));
    refForm.append(node("p", "문장에 [[term:용어]] 또는 위 ID 참조를 사용하세요. 원문을 유지하고 참조의 정의를 별도로 반환합니다. 중의성·오타는 후보를 확인한 뒤 ID로 지정하세요.", "wf-note"));
    const text = node("textarea"); text.value = preview?.text || ""; text.placeholder = "[[term:기반 모델]]을 비교해주세요.";
    text.setAttribute("aria-label", "용어 참조 원문"); text.maxLength = 20000; text.disabled = busy;
    text.oninput = () => { preview = {text: text.value}; };
    const resolve = node("button", "참조 확인", "wf-btn"); resolve.disabled = busy;
    refForm.append(text, resolve);
    refForm.onsubmit = e => { e.preventDefault(); const value = text.value;
      act(async () => { preview = await request(url("/api/glossary/resolve"), "POST", {text: value}); }); };
    if (preview?.references) refForm.append(node("pre", JSON.stringify(preview.references, null, 2)));
    root.append(refForm);
  }
  function termCard(row) {
    const card = node("article", null, "glossary-card");
    card.append(node("h4", row.term), node("p", row.definition, "glossary-definition"));
    if (row.aliases.length) card.append(node("p", "별칭: " + row.aliases.join(", ")));
    if (row.source) card.append(node("p", "출처: " + row.source, "glossary-definition"));
    if (row.match) card.append(node("p", `${row.match} · ${row.matched_label} · 유사도 ${row.score}`, "wf-note"));
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
