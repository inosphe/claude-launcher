/* Search source text is rendered as text, never HTML. */
globalThis.SearchAnything = (() => {
  const node = (tag, text, cls) => { const n = document.createElement(tag); n.textContent = text; if (cls) n.className = cls; return n; };
  async function request(path, body, method = "POST", signal) {
    const response = await api(path, body === undefined ? {signal} : {
      method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body), signal,
    });
    const data = await response.json();
    if (!response.ok) throw Error(data.error || `HTTP ${response.status}`);
    return data;
  }
  const modal = document.createElement("dialog");
  modal.className = "search-anything"; modal.setAttribute("aria-labelledby", "search-anything-title");
  const title = node("h2", "Search anything"); title.id = "search-anything-title";
  const close = node("button", "×", "search-anything-close"); close.type = "button";
  close.setAttribute("aria-label", "닫기"); close.title = "닫기 (Esc)";
  const header = node("div", "", "search-anything-header"); header.append(title, close);
  const form = document.createElement("form"), input = document.createElement("input");
  input.type = "search"; input.placeholder = "Beads, Observer, briefing, checks 검색";
  input.setAttribute("aria-label", "통합 검색어"); input.maxLength = 2000;
  const submit = node("button", "검색"); submit.type = "submit";
  const notice = node("p", "Enter로 검색 · Esc로 닫기", "search-anything-notice"); notice.setAttribute("role", "status");
  const results = node("div", "", "search-anything-results");
  form.append(input, submit); modal.append(header, form, notice, results); document.body.append(modal);
  let sequence = 0, controller = null, previousFocus = null;
  function open() {
    if (modal.open) { input.focus(); return; }
    previousFocus = document.activeElement; modal.showModal(); input.focus();
  }
  close.onclick = () => modal.close();
  modal.addEventListener("keydown", event => {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); modal.close(); }
  });
  modal.addEventListener("close", () => {
    sequence++; controller?.abort(); submit.disabled = false; previousFocus?.focus();
  });
  input.addEventListener("input", () => { sequence++; controller?.abort(); submit.disabled = false; });
  function addResult(row) {
    const item = node("article", "", "search-anything-result");
    item.append(node("small", [row.kind, row.at ? new Date(row.at).toLocaleString() : "", row.root || ""].filter(Boolean).join(" · ")));
    const link = node("a", row.title || row.id); link.href = row.href;
    link.onclick = () => { if (row.root && typeof beadsWorkspace !== "undefined") beadsWorkspace = row.root; modal.close(); };
    item.append(link, node("p", row.excerpt || ""));
    const sessions = node("div", "", "search-anything-sessions");
    for (const session of row.sessions || []) {
      const a = node("a", session.name); a.href = "#/s/" + encodeURIComponent(session.name);
      a.title = (session.via || []).join(", "); a.onclick = () => modal.close(); sessions.append(a);
    }
    if (!sessions.childNodes.length) sessions.append(node("span", "연결된 세션 없음"));
    item.append(sessions);
    if (row.source_url) {
      const detail = document.createElement("details"), text = node("pre", "불러오는 중…");
      detail.append(node("summary", "원문 보기"), text);
      detail.ontoggle = async () => {
        if (!detail.open || detail.dataset.loaded) return;
        try { const data = await request(row.source_url); text.textContent = JSON.stringify(data, null, 2); detail.dataset.loaded = "1"; }
        catch (error) { text.textContent = error.message; }
      };
      item.append(detail);
    }
    results.append(item);
  }
  form.onsubmit = async event => {
    event.preventDefault(); const query = input.value.trim(); if (!query) return;
    controller?.abort(); controller = new AbortController(); const ticket = ++sequence;
    submit.disabled = true; notice.textContent = "검색 중…"; results.replaceChildren();
    try {
      const data = await request("api/search?kind=all&limit=30&q=" + encodeURIComponent(query), undefined, "GET", controller.signal);
      if (ticket !== sequence || !modal.open) return;
      for (const row of data.results || []) addResult(row);
      const index = data.index || {};
      notice.textContent = `${data.results.length}개 결과 · 색인 ${index.indexed || 0}/${index.total || 0}`
        + (index.pending || index.syncing ? " · 색인 진행 중, 다시 검색하면 추가 결과가 표시됩니다." : "")
        + (index.error ? " · 색인 오류: " + index.error : "");
      if (data.warnings?.length) notice.textContent += " · Rerank 사용 불가: embedding 및 정확한 단어 일치 기준으로 표시합니다.";
    } catch (error) { if (ticket === sequence && error.name !== "AbortError") notice.textContent = error.message; }
    finally { if (ticket === sequence) submit.disabled = false; }
  };
  function shortcut(event) {
    return event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey && !event.isComposing && !event.defaultPrevented
      && !event.target?.closest?.("input, textarea, select, [contenteditable]:not([contenteditable=false]), .xterm, dialog, [role=dialog]");
  }
  document.addEventListener("keydown", event => { if (shortcut(event)) { event.preventDefault(); open(); } });
  document.getElementById("search-anything-open")?.addEventListener("click", open);
  let settingsElement = null;
  function settingsCard() {
    if (settingsElement) return settingsElement;
    const card = node("section", "", "ws-add search-settings"); card.append(node("h3", "Search settings"));
    settingsElement = card;
    const status = node("p", "설정을 불러오는 중…"); status.setAttribute("role", "status"); card.append(status);
    request("api/rag/settings").then(cfg => {
      const form = document.createElement("form"), fields = {};
      function field(key, title, type = "text", parent = form) {
        const label = node("label", title), input = document.createElement("input");
        input.type = type; input.name = key; input.value = type === "password" ? "" : cfg[key] ?? "";
        if (type === "number") { input.min = key === "watch_interval" ? "0" : "1"; input.max = "10000"; }
        if (type === "password") { input.autocomplete = "new-password"; input.placeholder = cfg.api_key_set ? "설정됨 · 비워두면 유지" : "API key"; }
        if (type === "checkbox") input.checked = cfg[key];
        label.append(input); parent.append(label); fields[key] = input;
      }
      field("base_url", "oMLX API 주소"); field("api_key", "API key", "password");
      field("embedding_model", "Embedding model"); field("rerank_model", "Rerank model (선택)");
      const advanced = document.createElement("details"); advanced.append(node("summary", "고급 설정"));
      for (const [key, title] of [["candidates", "검색 후보 수"], ["rerank_top", "Rerank 대상 수"], ["batch", "Embedding 배치 크기"], ["timeout", "요청 제한 시간 (초)"], ["watch_interval", "변경 확인 주기 (초)"]]) field(key, title, "number", advanced);
      field("verify_tls", "TLS 인증서 검증", "checkbox", advanced); form.append(advanced);
      form.append(node("p", "차원 수는 모델 응답에서 자동으로 읽습니다. Embedding 모델이나 API 주소를 변경하면 색인을 다시 구성합니다."));
      const save = node("button", "저장"), test = node("button", "연결 테스트"); save.type = "submit"; test.type = "button";
      function values() { return Object.fromEntries(Object.entries(fields).map(([key, input]) => [key, input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value])); }
      async function action(testOnly) {
        save.disabled = test.disabled = true; status.textContent = testOnly ? "연결 확인 중…" : "저장 중…";
        try {
          const data = await request(testOnly ? "api/rag/test" : "api/rag/settings", values(), testOnly ? "POST" : "PUT");
          status.textContent = testOnly ? `연결 확인됨 · 실제 차원 ${data.dimensions} · rerank ${data.rerank ? "확인됨" : "사용 안 함"}` : "저장됨 · 검색 색인을 갱신합니다.";
          if (!testOnly) { fields.api_key.value = ""; fields.api_key.placeholder = data.api_key_set ? "설정됨 · 비워두면 유지" : "API key"; }
        } catch (error) { status.textContent = error.message; }
        finally { save.disabled = test.disabled = false; }
      }
      test.onclick = () => action(true); form.onsubmit = event => { event.preventDefault(); action(false); };
      form.append(save, test); card.append(form); status.textContent = "현재 설정";
    }).catch(error => { status.textContent = error.message; });
    return card;
  }
  return {open, settingsCard, shortcut};
})();
