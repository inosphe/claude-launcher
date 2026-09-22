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
  // The kind filter, between the answer's own line and the rows it narrows.
  const filters = node("div", "", "search-anything-filters");
  filters.setAttribute("role", "group"); filters.setAttribute("aria-label", "항목 종류 필터");
  const results = node("div", "", "search-anything-results");
  form.append(input, submit); modal.append(header, form, notice, filters, results); document.body.append(modal);
  let sequence = 0, controller = null, previousFocus = null, timeRefresh = null;
  // The last answer, kept so that changing the filter re-narrows what is
  // already on screen instead of asking the daemon the same question again.
  // `kindFilter` empty means every kind; `lastNotice` is that answer's own
  // line, which the filter appends its count to.
  let lastRows = [], lastNotice = "", kindFilter = "";
  const relativeTime = new Intl.RelativeTimeFormat("ko", {numeric: "always"});
  function formatTime(value) {
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return value;
    const seconds = (date.getTime() - Date.now()) / 1000;
    let relative = "방금 전";
    if (Math.abs(seconds) >= 60) {
      const [unit, size] = [["year", 31536000], ["month", 2592000], ["day", 86400], ["hour", 3600], ["minute", 60]]
        .find(([, size]) => Math.abs(seconds) >= size);
      relative = relativeTime.format(Math.trunc(seconds / size), unit);
    } else if (seconds > 0) relative = "곧";
    return `${date.toLocaleString()} (${relative})`;
  }
  function refreshTimes() {
    for (const time of results.querySelectorAll("time")) time.textContent = formatTime(time.dateTime);
  }
  function open() {
    if (modal.open) { input.focus(); return; }
    previousFocus = document.activeElement; modal.showModal(); input.focus();
    refreshTimes(); timeRefresh = setInterval(refreshTimes, 30000);
  }
  close.onclick = () => modal.close();
  modal.addEventListener("keydown", event => {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); modal.close(); }
  });
  modal.addEventListener("close", () => {
    clearInterval(timeRefresh); timeRefresh = null;
    sequence++; controller?.abort(); submit.disabled = false; previousFocus?.focus();
  });
  input.addEventListener("input", () => { sequence++; controller?.abort(); submit.disabled = false; });
  // A result is either a session or a record about one, and the two are drawn
  // as two shapes rather than as one shape with different words: a session row
  // is headed by the session and by the state it is in right now, a record row
  // by the source it came from. The daemon marks a session row with this kind
  // (see the unified corpus in daemon/search_anything.py), which is the only
  // thing this dialog decides a row's class from.
  const SESSION = "session";
  // What a row's source is called: the daemon's own word for it, and 기록 for a
  // row that arrived without one. The filter chips are built from this same
  // value, so a chip and the badge on the rows it selects always agree.
  const kindLabel = row => row.kind || "기록";
  // What the state of a session is called here. `status` is the fleet's live
  // reading (starting/busy/idle/exited) and comes from the daemon on every
  // answer rather than from the index, which is embedded in the background
  // and would report whatever was true when it was last synced. A record that
  // was paused or archived is named by that: its process is gone either way,
  // so `exited` would be true and would not be what a reader is looking at.
  function sessionState(session) {
    if (session.archived) return "archived";
    if (session.paused) return "paused";
    return session.status || "";
  }
  // One session, drawn the same wherever a result names one. It wears the
  // state chip the board rows and the queue lanes already wear (`beads-sess`,
  // style.css) rather than a copy of its colours — the reuse the spawn form's
  // fieldsets document for `.sess-spawn-step`. The state is in the chip twice
  // over: the class is the colour the rest of the app gives that state, and
  // the word is the same fact without colour.
  function sessionChip(session) {
    const state = sessionState(session);
    const link = node("a", session.name, "beads-sess" + (state ? " " + state : ""));
    link.href = "#/s/" + encodeURIComponent(session.name);
    const via = (session.via || []).join(", ");
    link.title = session.name + (state ? ` (${state})` : "") + (via ? " — " + via : "");
    if (state) link.append(node("span", state, "beads-sess-state"));
    link.onclick = () => modal.close();
    return link;
  }
  function resultHeader(row) {
    const head = node("div", "", "search-anything-meta");
    head.append(node("span", kindLabel(row), "search-anything-kind"));
    if (row.at) { const time = node("time", formatTime(row.at)); time.dateTime = row.at; head.append(time); }
    if (row.root) head.append(node("span", row.root, "search-anything-root"));
    return head;
  }
  function addResult(row) {
    const isSession = row.kind === SESSION;
    const item = node("article", "", "search-anything-result " + (isSession ? "search-anything-session" : "search-anything-record"));
    item.append(resultHeader(row));
    if (isSession) {
      // The row is the session, so its own name and state are the link. The
      // `sessions` this row also carries would only repeat it. The chip sits
      // in a line of its own because style.css styles a record's headline
      // through `.search-anything-result > a`, whose rules a chip must not
      // inherit (a block-level, bold, 13px pill would not be a chip).
      const title = node("div", "", "search-anything-session-title");
      title.append(sessionChip(row));
      item.append(title);
    } else {
      const link = node("a", row.title || row.id); link.href = row.href;
      link.onclick = () => { if (row.root && typeof beadsWorkspace !== "undefined") beadsWorkspace = row.root; modal.close(); };
      item.append(link);
    }
    item.append(node("p", row.excerpt || ""));
    if (!isSession) {
      const sessions = node("div", "", "search-anything-sessions");
      for (const session of row.sessions || []) sessions.append(sessionChip(session));
      if (!sessions.childNodes.length) sessions.append(node("span", "연결된 세션 없음"));
      item.append(sessions);
    }
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
    return item;
  }
  // The results as two labelled lists rather than one. The rank still orders
  // every answer, but it orders it *within* the class it belongs to: whether a
  // row is a session is the first thing a reader needs from it, and a session
  // mixed into a list of records cannot be picked out of it by rank alone.
  // Rows arrive ranked, so each list keeps that order among its own rows.
  function addGroups(rows) {
    const groups = [["세션", rows.filter(row => row.kind === SESSION)],
                    ["그 외 항목", rows.filter(row => row.kind !== SESSION)]];
    for (const [title, group] of groups) {
      if (!group.length) continue;
      const section = node("section", "", "search-anything-group");
      const heading = node("h3", title);
      heading.append(node("span", group.length, "search-anything-group-count"));
      section.append(heading);
      for (const row of group) section.append(addResult(row));
      results.append(section);
    }
    return groups.map(([, group]) => group.length);
  }
  // The kind filter: 전체 plus one chip per kind this answer actually holds,
  // each carrying how many rows it would leave. The chips are built from the
  // rows rather than from a fixed list of kinds, so a chip never promises rows
  // the answer does not have. What that leaves out — a kind that exists in the
  // index but not in this answer's window — is written down in
  // docs/search-anything.md instead of being papered over here.
  function renderFilters(rows) {
    const counts = new Map();
    for (const row of rows) counts.set(kindLabel(row), (counts.get(kindLabel(row)) || 0) + 1);
    const chips = [["", "전체", rows.length]];
    for (const [kind, count] of [...counts].sort((a, b) => b[1] - a[1])) chips.push([kind, kind, count]);
    filters.replaceChildren();
    for (const [value, label, count] of chips) {
      const chip = node("button", "", "search-anything-filter"); chip.type = "button";
      chip.append(node("span", label), node("span", count, "search-anything-filter-count"));
      chip.dataset.kind = value;
      chip.setAttribute("aria-pressed", String(value === kindFilter));
      chip.onclick = () => { kindFilter = value; renderFilters(rows); renderResults(); };
      filters.append(chip);
    }
  }
  // The rows the filter leaves, drawn as the two lists. Called on every new
  // answer and on every filter change; a filter change does not re-ask the
  // daemon, so a chip narrows exactly what is already on screen and the
  // answer's own line keeps its meaning.
  function renderResults() {
    const rows = kindFilter ? lastRows.filter(row => kindLabel(row) === kindFilter) : lastRows;
    results.replaceChildren();
    addGroups(rows);
    notice.textContent = lastNotice + (kindFilter ? ` · 표시 ${rows.length}` : "");
  }
  form.onsubmit = async event => {
    event.preventDefault(); const query = input.value.trim(); if (!query) return;
    controller?.abort(); controller = new AbortController(); const ticket = ++sequence;
    submit.disabled = true; notice.textContent = "검색 중…"; results.replaceChildren(); filters.replaceChildren();
    try {
      const data = await request("api/search?kind=all&limit=30&q=" + encodeURIComponent(query), undefined, "GET", controller.signal);
      if (ticket !== sequence || !modal.open) return;
      const rows = data.results || [];
      const index = data.index || {};
      const sessions = rows.filter(row => row.kind === SESSION).length;
      lastRows = rows;
      // A new answer starts unfiltered: the chips belong to the answer in hand,
      // so the line that counts it and the rows under it always agree when it
      // arrives, and a chip is re-picked deliberately rather than inherited
      // from a query the reader has already moved on from.
      kindFilter = "";
      lastNotice = `${rows.length}개 결과 · 세션 ${sessions} · 그 외 ${rows.length - sessions} · 색인 ${index.indexed || 0}/${index.total || 0}`
        + (index.pending || index.syncing ? " · 색인 진행 중, 다시 검색하면 추가 결과가 표시됩니다." : "")
        + (index.error ? " · 색인 오류: " + index.error : "");
      if (data.warnings?.length) lastNotice += " · Rerank 사용 불가: embedding 및 정확한 단어 일치 기준으로 표시합니다.";
      renderFilters(rows);
      renderResults();
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
