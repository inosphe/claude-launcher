/* Search source text is rendered as text, never HTML. */
globalThis.SearchAnything = (() => {
  const node = (tag, text, cls) => { const n = document.createElement(tag); n.textContent = text; if (cls) n.className = cls; return n; };
  function ragPresetOptionLabel(p) {
    const parts = [p.id];
    if (p.embedding_model) parts.push(p.embedding_model);
    if (p.model_key && p.model_key !== p.embedding_model) parts.push(`model_key ${p.model_key}`);
    return parts.join(" · ");
  }
  function ragPresetSummary(p) {
    const where = p.source === "profile" ? `profile ${p.profile || ""}`.trim() : (p.base_url || "custom endpoint");
    return `${p.id} — ${p.embedding_model || "모델 미지정"} (${where})`;
  }
  function ragBackfillTotals(indexes) {
    let pending = 0, total = 0;
    for (const idx of indexes || []) {
      pending += Number(idx.pending) || 0;
      total += Number(idx.total) || 0;
    }
    return { pending, total };
  }
  function ragBackfillStateLine(indexes, active, paused) {
    const rows = Array.isArray(indexes) ? indexes : [];
    const syncing = rows.some(r => r && r.syncing);
    const totals = ragBackfillTotals(rows);
    const activeEmpty = !active || (!active.preset && !active.embedding_model && !active.model_key);
    if (paused)
      return { state: "paused", text: `소급 임베딩 일시정지됨 · 남은 문서 ${totals.pending} / ${totals.total}건` };
    if (syncing || totals.pending > 0)
      return { state: "running", text: `소급 임베딩 진행 중 · 남은 문서 ${totals.pending} / ${totals.total}건` };
    if (rows.length === 0 || totals.total === 0) {
      // No rows is two different states. With a preset in force it means this
      // preset has no collection under its name yet -- the case right after a
      // save, when the old text read as "there are no documents at all"
      // (claunch-fhs7f.1).
      if (!activeEmpty) {
        const which = active && active.preset ? `프리셓 ${active.preset}의 ` : "";
        return { state: "idle", text: `${which}색인은 아직 없습니다 — 소급 임베딩이 시작되면 진행률이 여기에 표시됩니다.` };
      }
      return { state: "idle", text: "색인 미구동 — 아직 임베딩된 문서가 없습니다." };
    }
    const activeText = ragActiveText(active);
    if (activeEmpty) return { state: "done", text: "색인 갱신 완료" };
    return { state: "done", text: `${activeText} · 색인 갱신 완료` };
  }
  function ragActiveText(active) {
    if (!active || (!active.preset && !active.embedding_model && !active.model_key)) return "";
    const name = active.preset ? `프리셋 ${active.preset}` : "직접 설정";
    const model = active.embedding_model || "";
    const key = active.model_key ? ` · model_key ${active.model_key}` : "";
    return `활성: ${name}${model ? " · " + model : ""}${key}`;
  }
  async function request(path, body, method = "POST", signal) {
    const response = await api(path, body === undefined ? {signal} : {
      method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body), signal,
    });
    const data = await response.json().catch(() => ({}));
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
  input.type = "search"; input.placeholder = "Beads, opening task, Observer, briefing, checks 검색";
  input.setAttribute("aria-label", "통합 검색어"); input.maxLength = 2000;
  const submit = node("button", "검색"); submit.type = "submit";
  const notice = node("p", "Enter로 검색 · Esc로 닫기", "search-anything-notice"); notice.setAttribute("role", "status");
  // The reranker's order, offered rather than applied. The first list arrives
  // from the vector ranking in tens of milliseconds and the reranker's in
  // 1-11 s; the two share 8-10 of their top 10 but the reranker changes the
  // first row in most queries (claunch-1sszr), so swapping the list under a
  // reader would move the row they were reading. The swap waits for a click.
  const apply = node("button", "정렬 개선됨 — 적용", "search-anything-apply"); apply.type = "button"; apply.hidden = true;
  // The kind filter, between the answer's own line and the rows it narrows.
  const filters = node("div", "", "search-anything-filters");
  filters.setAttribute("role", "group"); filters.setAttribute("aria-label", "항목 종류 필터");
  const results = node("div", "", "search-anything-results");
  const modes = node("fieldset", "", "search-anything-modes");
  modes.append(node("legend", "검색 범위"));
  let searchMode = "general";
  for (const [value, text] of [["general", "일반"], ["active", "활성 세션 + Beads"]]) {
    const label = node("label", ""), radio = document.createElement("input");
    radio.type = "radio"; radio.name = "search-mode"; radio.value = value; radio.checked = value === searchMode;
    radio.onchange = () => {
      if (!radio.checked) return;
      searchMode = value;
      controller?.abort(); ++sequence;
      offered = null; apply.hidden = true; submit.disabled = false;
      lastRows = []; kindFilter = ""; results.replaceChildren(); filters.replaceChildren();
      notice.textContent = "Enter로 검색 · Esc로 닫기";
      if (input.value.trim()) form.requestSubmit();
    };
    label.append(radio, document.createTextNode(text)); modes.append(label);
  }
  form.append(input, submit); modal.append(header, modes, form, notice, apply, filters, results); document.body.append(modal);
  let sequence = 0, controller = null, previousFocus = null, timeRefresh = null;
  // The last answer, kept so that changing the filter re-narrows what is
  // already on screen instead of asking the daemon the same question again.
  // `kindFilter` empty means every kind; `lastNotice` is that answer's own
  // line, which the filter appends its count to. `rerankNote` says where the
  // reranker's second answer stands; `offered` holds it until it is applied.
  let lastRows = [], lastNotice = "", kindFilter = "", rerankNote = "", offered = null;
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
    offered = null; apply.hidden = true;
  });
  // Typing supersedes the answer in hand, and with it any reranked order
  // offered for the old query.
  input.addEventListener("input", () => {
    sequence++; controller?.abort(); submit.disabled = false; offered = null; apply.hidden = true;
  });
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
  const rowKinds = row => new Set([kindLabel(row), ...(row.matches || []).map(kindLabel)]);
  // How many rows of each kind are in hand, most numerous first, ties keeping
  // the order the answer arrived in. The filter chips and the second list's
  // heading are both built from this one count, so the two cannot disagree
  // about which kinds an answer holds.
  function kindCounts(rows) {
    const counts = new Map();
    for (const row of rows) {
      for (const kind of rowKinds(row)) counts.set(kind, (counts.get(kind) || 0) + 1);
    }
    return [...counts].sort((a, b) => b[1] - a[1]);
  }
  // What the state of a session is called here. `status` is the fleet's live
  // reading (starting/busy/idle/exited) and comes from the daemon on every
  // answer rather than from the index, which is embedded in the background
  // and would report whatever was true when it was last synced. A record that
  // was paused or archived is named by that: its process is gone either way,
  // so `exited` would be true and would not be what a reader is looking at.
  function sessionState(session) {
    if (session.archived) return "archived";
    if (session.hold) return "hold";
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
  // An opening task is prose and its record is only the envelope around
  // it, so the raw text is what the reader asked for; every other record
  // is a structured snapshot and stays readable as JSON.
  const sourceText = (row, data) => row.kind === "opening-task" && typeof data.text === "string"
    ? data.text : JSON.stringify(data, null, 2);
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
    if (row.matches?.length) {
      const matches = document.createElement("details");
      matches.className = "search-anything-matches";
      matches.append(node("summary", `관련 구절 ${row.matches.length}개`));
      for (const match of row.matches) {
        matches.append(node("span", kindLabel(match), "search-anything-kind"));
        matches.append(node("p", match.excerpt || ""));
      }
      item.append(matches);
    }
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
        try { const data = await request(row.source_url); text.textContent = sourceText(row, data); detail.dataset.loaded = "1"; }
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
  // The first list is all sessions, so one word names it. The second is
  // everything else, which is not one thing — so it is named by the kinds it
  // actually holds, from the same count the chips are built from. 「그 외 항목」
  // said only that those rows are not sessions, which the row badges already
  // say, and left a reader to open the list to learn what was inside it.
  function addGroups(rows) {
    const sessions = rows.filter(row => row.kind === SESSION);
    const rest = rows.filter(row => row.kind !== SESSION);
    const groups = [["세션", sessions], [kindCounts(rest).map(([kind]) => kind).join(" · "), rest]];
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
    const chips = [["", "전체", rows.length], ...kindCounts(rows).map(([kind, count]) => [kind, kind, count])];
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
    const rows = kindFilter ? lastRows.filter(row => rowKinds(row).has(kindFilter)) : lastRows;
    results.replaceChildren();
    addGroups(rows);
    notice.textContent = lastNotice + rerankNote + (kindFilter ? ` · 표시 ${rows.length}` : "");
  }
  // One answer on screen. A new query starts unfiltered: the chips belong to
  // the answer in hand, so the line that counts it and the rows under it
  // always agree when it arrives, and a chip is re-picked deliberately rather
  // than inherited from a query the reader has already moved on from. The
  // reranker's order for the *same* query keeps the chip the reader picked,
  // as long as the new answer still holds that kind.
  function show(data, keepFilter = false) {
    const rows = data.results || [];
    const index = data.index || {};
    const sessions = rows.filter(row => row.kind === SESSION).length;
    lastRows = rows;
    if (!keepFilter || !rows.some(row => rowKinds(row).has(kindFilter))) kindFilter = "";
    lastNotice = `${rows.length}개 결과 · 세션 ${sessions} · 그 외 ${rows.length - sessions} · 색인 ${index.indexed || 0}/${index.total || 0}`
      + (index.pending || index.syncing ? " · 색인 진행 중, 다시 검색하면 추가 결과가 표시됩니다." : "")
      + (index.error ? " · 색인 오류: " + index.error : "");
    renderFilters(rows);
    renderResults();
  }
  apply.onclick = () => {
    const data = offered; offered = null; apply.hidden = true;
    if (!data) return;
    rerankNote = " · 정렬 개선 적용됨";
    show(data, true);
  };
  // Server-sent events off a fetch body, rather than EventSource: the dialog
  // already cancels a superseded search with an AbortController, which an
  // EventSource does not take, and an EventSource reconnects -- re-running
  // the search -- when a finished stream closes.
  async function readEvents(response, onEvent) {
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const {value, done} = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
      let cut;
      while ((cut = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, cut); buffer = buffer.slice(cut + 2);
        let name = "message", data = "";
        for (const line of block.split("\n")) {
          if (line.startsWith("event: ")) name = line.slice(7);
          else if (line.startsWith("data: ")) data += line.slice(6);
        }
        if (data) onEvent(name, JSON.parse(data));
      }
      if (done) return;
    }
  }
  form.onsubmit = async event => {
    event.preventDefault(); const query = input.value.trim(); if (!query) return;
    controller?.abort(); controller = new AbortController(); const ticket = ++sequence;
    submit.disabled = true; notice.textContent = "검색 중…"; results.replaceChildren(); filters.replaceChildren();
    offered = null; apply.hidden = true; rerankNote = "";
    try {
      const response = await api("api/search/stream?kind=all&limit=30&mode=" + searchMode + "&q=" + encodeURIComponent(query),
        {signal: controller.signal, noBatch: true});
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw Error(data.error || `HTTP ${response.status}`);
      }
      await readEvents(response, (name, data) => {
        if (ticket !== sequence || !modal.open) return;
        if (name === "ranked") {
          // The list is usable now; the search button is free for the next
          // query, which aborts this stream like any superseded search.
          submit.disabled = false;
          rerankNote = data.rerank_pending ? " · 정렬 개선 중…" : "";
          show(data);
        } else if (name === "reranked") {
          offered = data; apply.hidden = false;
          rerankNote = " · 정렬 개선 가능";
          renderResults();
        } else if (name === "error") {
          rerankNote = " · Rerank 사용 불가: embedding 및 정확한 단어 일치 기준으로 표시합니다.";
          renderResults();
        }
      });
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
      // The daemon's own record of what it serves (active.json) plus the
      // backfill progress after a switch; refreshed by watchBackfill.
      const ragState = node("p", "", "rag-preset-state"); card.append(ragState);
      let backfillPolls = 0;
      // The pause/resume toggle (claunch-fhs7f) — created below once the
      // form builds; watchBackfill keeps its label honest from the status
      // poll so an outside change (another client, a restart) shows up.
      let pauseBtn = null, backfillPaused = false;
      async function watchBackfill() {
        try {
          const st = await request("api/rag/status");
          backfillPaused = Boolean(st.backfill_paused);
          if (pauseBtn) pauseBtn.textContent = backfillPaused ? "재개" : "일시정지";
          const line = ragBackfillStateLine(st.indexes, st.active, backfillPaused);
          if (line.state === "running" && backfillPolls < 200) {
            backfillPolls++;
            ragState.textContent = line.text;
            setTimeout(watchBackfill, 3000);
            return;
          }
          ragState.textContent = line.text;
        } catch { /* the status read is best-effort; leave the last line */ }
      }
      watchBackfill();
      function field(key, title, type = "text", parent = form) {
        const label = node("label", title), input = document.createElement("input");
        input.type = type; input.name = key; input.value = type === "password" ? "" : cfg[key] ?? "";
        if (type === "number") { input.min = key === "watch_interval" ? "0" : "1"; input.max = "10000"; }
        if (type === "password") { input.autocomplete = "new-password"; input.placeholder = cfg.api_key_set ? "설정됨 · 비워두면 유지" : "API key"; }
        if (type === "checkbox") input.checked = cfg[key];
        label.append(input); parent.append(label); fields[key] = input;
      }
      // Two connection paths, each stored with its own endpoint and model ids
      // (claunch-pxxn4). The model inputs below are shared on screen but are
      // saved into the path the radio picks, and switching the radio reloads
      // that path's saved values -- so a custom oMLX model id and a provider's
      // model id never overwrite each other.
      const saved = cfg.sources || { custom: {}, profile: {} };
      // Model presets (claunch-7m5v8) are the only connection model left
      // (claunch-rag-preset-always-hn5bc): the picker addresses one, its
      // values load into the fields below, and 저장 writes them back over
      // that preset -- selecting and editing are the same gesture, and
      // switching what is active happens on save, not on select.
      const presets = Array.isArray(cfg.presets) ? cfg.presets : [];
      // The preset the daemon is running with -- separate from the picker's
      // selection, because selecting only loads values into the fields
      // (showPreset). Renaming an id has to move this pointer only when the
      // pointer WAS that id; moving it otherwise would activate a preset the
      // reader only renamed, and the settings PUT starts that preset's model
      // on the whole corpus (claunch-fhs7f.1 review).
      let activeId = typeof cfg.preset === "string" ? cfg.preset : "";
      const sourceBox = document.createElement("fieldset"); sourceBox.append(node("legend", "연결 방식"));
      const sources = {};
      for (const [value, title] of [["custom", "Custom API (직접 입력)"], ["profile", "Profile (provider)"]]) {
        const label = node("label", ""), radio = document.createElement("input");
        radio.type = "radio"; radio.name = "rag-source"; radio.value = value; radio.checked = (cfg.source || "custom") === value;
        label.append(radio, " " + title); sourceBox.append(label); sources[value] = radio;
      }
      const presetBox = document.createElement("fieldset"); presetBox.append(node("legend", "모델 프리셋"));
      const presetLabel = node("label", "프리셋", "rag-preset-row"), presetSelect = document.createElement("select");
      presetSelect.name = "preset";
      for (const p of presets) presetSelect.append(new Option(ragPresetOptionLabel(p), p.id));
      presetSelect.value = cfg.preset || "";
      // The saved-list ceiling the daemon enforces, surfaced before the
      // reader hits it, and a per-preset delete control — the list is the
      // whole PUT body, so without a delete the card only ever grows
      // (claunch-rag-preset-always-hn5bc.2).
      const presetMax = Number(cfg.preset_max) > 0 ? Number(cfg.preset_max) : 20;
      const presetCount = node("span", ` ${presets.length}/${presetMax}`, "rag-preset-count");
      const deletePresetBtn = node("button", "삭제", "rag-preset-delete"); deletePresetBtn.type = "button";
      presetLabel.append(presetSelect, presetCount, " ", deletePresetBtn); presetBox.append(presetLabel);
      const presetNote = node("p", ""); presetBox.append(presetNote);
      // Save the connection values on screen as a NEW preset, and make it
      // the active one. The daemon's settings PUT takes the whole saved
      // list, so this appends to the list the card loaded.
      const savePresetRow = node("p", "");
      const savePresetId = document.createElement("input");
      savePresetId.placeholder = "새 프리셋 id (소문자·숫자·하이픈)";
      const savePresetBtn = node("button", "현재 연결 값으로 새 프리셓 저장"); savePresetBtn.type = "button";
      savePresetRow.append(savePresetId, savePresetBtn); presetBox.append(savePresetRow);
      savePresetBtn.onclick = async () => {
        const id = savePresetId.value.trim();
        if (!/^[a-z0-9-]+$/.test(id)) { status.textContent = "프리셋 id는 소문자·숫자·하이픈만 씁니다."; return; }
        if (presets.some(p => p.id === id)) { status.textContent = `프리셋 ${id}이 이미 있습니다 — 다른 id를 쓰세요.`; return; }
        if (presets.length >= presetMax) { status.textContent = `프리셓은 ${presetMax}개까지 저장됩니다 — 프리셓을 삭제하고 다시 시도하세요.`; return; }
        const preset = editedPreset(id);
        if (!preset) return;
        savePresetBtn.disabled = true; status.textContent = `프리셓 ${id}을 저장하는 중…`;
        try {
          const data = await request("api/rag/settings", { ...values(), presets: [...presets, preset], preset: id }, "PUT");
          presets.push(preset);
          presetSelect.append(new Option(ragPresetOptionLabel(preset), preset.id));
          presetSelect.value = id;
          savePresetId.value = "";
          presetCount.textContent = ` ${presets.length}/${presetMax}`;
          afterSave(data);
          status.textContent = `프리셓 ${id}을 저장하고 활성화했습니다 — 기존 문서의 임베딩을 이 모델로 소급합니다.`;
        } catch (error) {
          status.textContent = `프리셓 저장 실패 · ${error.message}`;
        } finally { savePresetBtn.disabled = false; }
      };
      // Delete one preset row: the settings PUT takes the whole saved list,
      // so deletion is the list minus the row, with the pointer moved to the
      // first remaining preset when the active one went away. The screen
      // values ride along as the legacy rows, so the deleted preset's
      // connection stays as the visible fallback rather than a dangling
      // pointer over empty rows.
      // preset-only card: the list never holds zero presets. Deleting the
      // last one is refused up front — if an empty list ever reached the
      // server, the read-side migration would immediately re-wrap the
      // legacy rows into a fresh preset, so the delete would appear to do
      // nothing. Edit-in-place is the honest path for the last preset.
      deletePresetBtn.onclick = async () => {
        const id = presetSelect.value;
        if (!id) { status.textContent = "삭제할 프리셓을 먼저 선택하세요."; return; }
        if (presets.length <= 1) {
          status.textContent = "마지막 프리셓은 삭제할 수 없습니다 — 이 카드는 프리셓을 하나 이상 유지합니다. 연결 값을 바꾸려면 이 프리셓을 고쳐 저장하세요.";
          return;
        }
        if (!window.confirm(`프리셓 ${id}을 삭제합니다. 저장된 연결 값과 API key가 지워집니다.`)) return;
        const remaining = presets.filter(p => p.id !== id);
        const next = presetSelect.value === id ? (remaining.length ? remaining[0].id : "") : presetSelect.value;
        save.disabled = test.disabled = selftest.disabled = deletePresetBtn.disabled = savePresetBtn.disabled = true;
        status.textContent = `프리셓 ${id}을 삭제하는 중…`;
        try {
          const data = await request("api/rag/settings", { ...values(), presets: remaining, preset: next }, "PUT");
          afterSave(data);
          presetSelect.length = 0;
          for (const p of presets) presetSelect.append(new Option(ragPresetOptionLabel(p), p.id));
          presetSelect.value = typeof data.preset === "string" ? data.preset : next;
          presetCount.textContent = ` ${presets.length}/${presetMax}`;
          showPreset();
          loadPreset(presets.find(p => p.id === presetSelect.value));
          status.textContent = `프리셓 ${id}을 삭제했습니다.`;
        } catch (error) {
          status.textContent = `프리셓 삭제 실패 · ${error.message}`;
        } finally { save.disabled = test.disabled = selftest.disabled = deletePresetBtn.disabled = savePresetBtn.disabled = false; }
      };
      // The collection this preset reads and writes. Empty keeps the
      // pre-db rule: the preset's model identity. Naming one is what lets a
      // preset change its model or provider and still answer from the
      // vectors it already has (claunch-fhs7f.1).
      const dbRow = node("p", "rag-preset-db");
      const dbInput = document.createElement("input");
      dbInput.placeholder = "DB 이름 (비우면 모델 이름에서 유도)";
      dbRow.append(node("span", "DB "), dbInput); presetBox.append(dbRow);
      const dbList = node("div", "", "rag-db-list"); presetBox.append(dbList);
      // One row per collection on disk. The delete control is the panel's
      // management half: a collection a preset reads cannot go, because its
      // vectors would have to be asked for again, so the button is disabled
      // and says why. Deleting a collection nothing reads changes nothing
      // downstream (claunch-fhs7f.1).
      async function loadDbs() {
        try {
          const data = await request("api/rag/dbs", undefined, "GET");
          const rows = Array.isArray(data.dbs) ? data.dbs : [];
          dbList.replaceChildren();
          if (!rows.length) {
            dbList.append(node("span", "저장된 DB가 없습니다 — 첫 프리셓을 저장하면 이 모델의 DB가 만들어집니다."));
            return;
          }
          for (const row of rows) {
            const name = row.identity || row.name;
            const bits = [];
            if (row.dims) bits.push(`${row.dims}차원`);
            if (row.presets && row.presets.length) bits.push(row.presets.join(", "));
            const line = node("span", bits.length ? `${name} (${bits.join(" · ")})` : name, "rag-db-row");
            const occupied = Boolean((row.presets && row.presets.length) || row.in_use);
            const del = node("button", "삭제", "rag-db-delete"); del.type = "button";
            del.disabled = occupied;
            if (occupied) del.title = "이 DB를 쓰는 프리셓이 있어 삭제할 수 없습니다 — 그 프리셓의 DB 이름을 먼저 바꾸세요.";
            del.onclick = async () => {
              if (!window.confirm(`DB ${name}과 그 벡터를 삭제합니다. 되돌릴 수 없습니다. 계속할까요?`)) return;
              del.disabled = true;
              try {
                await request("api/rag/dbs/delete", { name: row.name }, "POST");
                status.textContent = `DB ${name}을 삭제했습니다 — 이 DB의 벡터는 사라졌습니다.`;
              } catch (error) {
                status.textContent = `DB 삭제 실패 · ${error.message}`;
              }
              await loadDbs();
            };
            const entry = node("p", "", "rag-db-entry");
            entry.append(line, del); dbList.append(entry);
          }        } catch { dbList.textContent = ""; }
      }
      // Rename a preset. The id is the name and also the key its saved secret
      // is filed under, so the row carries renamed_from and the daemon moves
      // the secret across. The vectors do not move: the collection is named
      // by db/model_key, not by the preset id (claunch-fhs7f.1).
      const renameRow = node("p", "rag-preset-rename");
      const renameTo = document.createElement("input");
      renameTo.placeholder = "새 프리셓 이름 (소문자·숫자·하이픈)";
      const renameBtn = node("button", "이름 변경"); renameBtn.type = "button";
      renameRow.append(renameTo, renameBtn); presetBox.append(renameRow);
      renameBtn.onclick = async () => {
        const from = presetSelect.value, to = renameTo.value.trim();
        if (!from) { status.textContent = "이름을 바꿀 프리셓을 먼저 선택하세요."; return; }
        if (!/^[a-z0-9-]+$/.test(to)) { status.textContent = "프리셓 이름은 소문자·숫자·하이픈만 씁니다."; return; }
        if (to === from) { renameTo.value = ""; return; }
        if (presets.some(p => p.id === to)) { status.textContent = `프리셓 ${to}이 이미 있습니다 — 다른 이름을 쓰세요.`; return; }
        const at = presets.findIndex(p => p.id === from);
        if (at < 0) { status.textContent = `프리셓 ${from}을 찾지 못했습니다.`; return; }
        const rows = presets.map((p, i) => i === at ? { ...p, id: to, renamed_from: from } : p);
        // The pointer follows only if this preset was the active one. values()
        // carries the picker's selection, which is not the active preset, so
        // the rename body states the pointer explicitly (claunch-fhs7f.1).
        const body = { ...values(), presets: rows };
        body.preset = activeId === from ? to : activeId;
        renameBtn.disabled = save.disabled = test.disabled = selftest.disabled = deletePresetBtn.disabled = savePresetBtn.disabled = true;
        status.textContent = `프리셓 ${from}의 이름을 ${to}으로 바꾸는 중…`;
        try {
          const data = await request("api/rag/settings", body, "PUT");
          afterSave(data);
          presetSelect.length = 0;
          for (const p of presets) presetSelect.append(new Option(ragPresetOptionLabel(p), p.id));
          presetSelect.value = to;
          presetCount.textContent = ` ${presets.length}/${presetMax}`;
          renameTo.value = "";
          showPreset();
          loadPreset(presets.find(p => p.id === to));
          status.textContent = activeId === to
            ? `프리셓 ${from}의 이름을 ${to}으로 바꿨습니다 — 저장된 API key와 연결 값은 그대로이고, 활성 프리셓도 ${to}입니다.`
            : `프리셓 ${from}의 이름을 ${to}으로 바꿨습니다 — 저장된 API key와 연결 값은 그대로이고, 활성 프리셓은 ${activeId || "(없음)"} 그대로입니다.`;
        } catch (error) {
          status.textContent = `이름 변경 실패 · ${error.message}`;
        } finally { renameBtn.disabled = save.disabled = test.disabled = selftest.disabled = deletePresetBtn.disabled = savePresetBtn.disabled = false; }
      };
      form.append(presetBox);
      form.append(sourceBox);
      const customBox = document.createElement("fieldset"); customBox.append(node("legend", "Custom API"));
      field("base_url", "API 주소 (예: oMLX)", "text", customBox); field("api_key", "API key", "password", customBox);
      // The /v1 correction's opt-out (claunch-rag-preset-always-hn5bc.1): a
      // root-serving endpoint (TEI spells /embeddings at the host root, so
      // <host>/v1/embeddings does not exist) says so here, and the flag is
      // saved on the preset row.
      field("root_serving", "루트 경로에 직접 서빙 (TEI 등 — /v1 경로 없음)", "checkbox", customBox);
      const profileBox = document.createElement("fieldset"); profileBox.append(node("legend", "Profile"));
      const profileLabel = node("label", "프로필"), profileSelect = document.createElement("select");
      profileSelect.name = "profile"; profileSelect.append(new Option("프로필 선택", ""));
      const rows = cfg.profiles || [];
      for (const row of rows) profileSelect.append(new Option(`${row.name} · ${row.base_url}${row.has_key ? "" : " · API key 없음"}`, row.name));
      const chosenProfile = saved.profile?.profile || "";
      if (chosenProfile && !rows.some(row => row.name === chosenProfile)) profileSelect.append(new Option(`${chosenProfile} · 사용 불가`, chosenProfile));
      profileSelect.value = chosenProfile;
      profileLabel.append(profileSelect); profileBox.append(profileLabel); fields.profile = profileSelect;
      profileBox.append(node("p", "프로필의 provider가 가진 OpenAI 호환 endpoint와 API key를 사용합니다. Embedding 모델은 이 방식의 값으로 따로 저장됩니다."));
      if (cfg.profile_error && cfg.source === "profile") profileBox.append(node("p", "현재 프로필을 사용할 수 없습니다: " + cfg.profile_error));
      form.append(customBox, profileBox);
      fields.base_url.value = saved.custom?.base_url ?? "";
      function showSource() {
        const picked = sources.profile.checked ? "profile" : "custom";
        customBox.hidden = picked !== "custom"; profileBox.hidden = picked !== "profile";
        // The field the radio hides is repointed at the path now in force, so
        // whichever way the reader switches, the models shown are that path's.
        fields.embedding_model.value = saved[picked]?.embedding_model ?? "";
        fields.rerank_model.value = saved[picked]?.rerank_model ?? "";
        // The key field is not reloaded: it never holds a saved value, and
        // clearing on every switch would discard a key typed and then hidden
        // by one look at the other path.
      }
      for (const radio of Object.values(sources)) radio.onchange = showSource;
      field("embedding_model", "Embedding model"); field("rerank_model", "Rerank model (선택)");
      showSource();
      function showPreset() {
        const active = presets.find(p => p.id === presetSelect.value);
        // Nothing is read-only under the new model: the fields always edit
        // the selected preset, and 저장 is what writes them back.
        sourceBox.disabled = customBox.disabled = profileBox.disabled = false;
        savePresetId.disabled = savePresetBtn.disabled = false;
        presetNote.textContent = active
          ? `선택한 프리셓을 아래에서 고쳐 저장하면 이 프리셓이 바뀝니다 — ${ragPresetSummary(active)}. Embedding 모델을 바꾸면 기존 문서의 임베딩을 새 모델로 소급합니다. 다른 프리셓으로 옮기면 저장하지 않은 수정은 사라집니다.`
          : "저장된 프리셓이 없습니다 — 아래에 연결 값을 입력하고 ‘현재 연결 값으로 새 프리셓 저장’으로 첫 프리셓을 만드세요.";
      }
      // Load a preset's values into the fields (local only). Both source
      // rows mirror it: a preset is one connection, not two paths' worth.
      function loadPreset(p) {
        if (!p) return;
        sources[p.source === "profile" ? "profile" : "custom"].checked = true;
        const model = p.embedding_model || "";
        saved.custom = { base_url: p.base_url || "", embedding_model: model, rerank_model: p.rerank_model || "" };
        saved.profile = { profile: p.profile || "", embedding_model: model, rerank_model: p.rerank_model || "" };
        fields.base_url.value = p.base_url || "";
        fields.profile.value = p.profile || "";
        fields.embedding_model.value = model;
        fields.rerank_model.value = p.rerank_model || "";
        fields.root_serving.checked = p.root_serving === true;
        dbInput.value = p.db || "";
        showSource();
      }
      // The screen values as one preset row. ``model_key`` survives an edit
      // only while the embedding model is untouched -- carrying it across a
      // model change would alias the new model to the old vector store.
      function editedPreset(id) {
        const picked = sources.profile.checked ? "profile" : "custom";
        const preset = { id, source: picked, embedding_model: fields.embedding_model.value.trim(), rerank_model: fields.rerank_model.value.trim() };
        if (picked === "profile") preset.profile = fields.profile.value;
        else {
          preset.base_url = fields.base_url.value.trim();
          if (fields.api_key.value) preset.api_key = fields.api_key.value;
          if (fields.root_serving.checked) preset.root_serving = true;
        }
        const old = presets.find(p => p.id === id);
        if (old && old.model_key && old.embedding_model === preset.embedding_model) preset.model_key = old.model_key;
        // An empty field clears it: the preset goes back to its model
        // identity, which is what every preset written before db existed
        // meant (claunch-fhs7f.1).
        const db = dbInput.value.trim();
        if (db) preset.db = db;
        return preset;
      }
      // After a save the daemon's reply is the new truth: mirror the rows
      // and the preset list (secrets stay stripped) into the card's state.
      function afterSave(data) {
        backfillPolls = 0; watchBackfill(); loadDbs();
        fields.api_key.value = ""; fields.api_key.placeholder = data.api_key_set ? "설정됨 · 비워두면 유지" : "API key";
        Object.assign(saved, data.sources || {});
        if (Array.isArray(data.presets)) {
          presets.length = 0;
          presets.push(...data.presets);
        }
        if (typeof data.preset === "string") { presetSelect.value = data.preset; activeId = data.preset; }
      }
      showPreset();
      // Selecting a preset only loads its values. Activation -- the daemon
      // restart and the backfill it starts -- happens on 저장, so peeking at
      // another preset costs nothing and an unfinished edit is not lost by
      // a stray click (search_anything.py restarts the service and enqueues
      // "all" on the settings PUT).
      presetSelect.onchange = () => {
        loadPreset(presets.find(p => p.id === presetSelect.value));
        showPreset();
      };
      if (cfg.preset) loadPreset(presets.find(p => p.id === cfg.preset));
      else showSource();
      const advanced = document.createElement("details"); advanced.append(node("summary", "고급 설정"));
      for (const [key, title] of [["candidates", "검색 후보 수"], ["rerank_top", "Rerank 대상 수"], ["batch", "Embedding 배치 크기"], ["timeout", "요청 제한 시간 (초)"], ["watch_interval", "변경 확인 주기 (초)"]]) field(key, title, "number", advanced);
      field("verify_tls", "TLS 인증서 검증", "checkbox", advanced); form.append(advanced);
      form.append(node("p", "차원 수는 모델 응답에서 자동으로 읽습니다. 연결 방식, Embedding 모델이나 API 주소를 변경하면 색인을 다시 구성합니다. 선택하지 않은 방식의 값도 설정 파일에 그대로 보존됩니다."));
      // The label says what the button does: this form writes the SELECTED
      // preset's row and makes it active (saveBody), so "저장" alone read as a
      // settings save rather than a preset save (claunch-fhs7f.1).
      const save = node("button", "선택한 프리셓 저장"), test = node("button", "연결 테스트"), selftest = node("button", "Selftest");
      save.type = "submit"; test.type = "button"; selftest.type = "button";
      pauseBtn = node("button", "일시정지"); pauseBtn.type = "button";
      const selftestResult = node("p", "", "rag-selftest-result"); selftestResult.hidden = true; form.append(selftestResult);
      // Pause holds the daemon's backfill consumer before its next key; the
      // in-flight pass finishes, queued corpora keep their order, and resume
      // drains from where the pause left it (POST api/rag/backfill/pause|resume).
      pauseBtn.onclick = async () => {
        save.disabled = test.disabled = selftest.disabled = pauseBtn.disabled = true;
        const resuming = backfillPaused;
        status.textContent = resuming ? "소급 임베딩을 재개하는 중…" : "소급 임베딩을 일시정지하는 중…";
        try {
          const data = await request(resuming ? "api/rag/backfill/resume" : "api/rag/backfill/pause", {}, "POST");
          backfillPaused = Boolean(data.paused);
          pauseBtn.textContent = backfillPaused ? "재개" : "일시정지";
          status.textContent = backfillPaused
            ? "소급 임베딩이 일시정지됐습니다 — 재개하면 남은 문서부터 이어집니다."
            : "소급 임베딩을 재개했습니다.";
          watchBackfill();
        } catch (error) {
          status.textContent = error.message;
        } finally { save.disabled = test.disabled = selftest.disabled = pauseBtn.disabled = false; }
      };
      function values() {
        const out = Object.fromEntries(Object.entries(fields).map(([key, input]) => [key, input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value]));
        const picked = sources.profile.checked ? "profile" : "custom";
        out.source = picked;
        out.preset = presetSelect.value;        // Both rows go with every save: the picked path takes what is on
        // screen, the other keeps what it had. An empty password field is
        // dropped so the daemon keeps the saved secret.
        out.sources = {
          custom: { base_url: fields.base_url.value, embedding_model: fields.embedding_model.value,
                    rerank_model: fields.rerank_model.value },
          profile: { profile: fields.profile.value, embedding_model: fields.embedding_model.value,
                     rerank_model: fields.rerank_model.value },
        };
        if (fields.api_key.value) out.sources.custom.api_key = fields.api_key.value;
        if (picked !== "custom") {
          out.sources.custom.embedding_model = saved.custom?.embedding_model ?? "";
          out.sources.custom.rerank_model = saved.custom?.rerank_model ?? "";
        }
        if (picked !== "profile") {
          out.sources.profile.embedding_model = saved.profile?.embedding_model ?? "";
          out.sources.profile.rerank_model = saved.profile?.rerank_model ?? "";
        }
        // The flat fields would land in the picked row anyway; keeping them out
        // of the body leaves one spelling of a save.
        for (const key of ["profile", "base_url", "api_key", "embedding_model", "rerank_model"]) delete out[key];
        return out;
      }
      // The save body: the loaded preset list with the selected preset's row
      // replaced by what is on screen, plus the pointer. The test probe
      // sends values() as-is; the daemon's /api/rag/test ignores the preset
      // keys and probes the submitted rows, so both paths test the screen.
      function saveBody() {
        const body = values();
        const id = presetSelect.value;
        if (!id) return body;
        body.presets = presets.map(p => p.id === id ? editedPreset(id) : p);
        return body;
      }
      async function action(testOnly) {
        save.disabled = test.disabled = selftest.disabled = pauseBtn.disabled = true; status.textContent = testOnly ? "연결 확인 중…" : "저장 중…";
        try {
          if (!testOnly && !presetSelect.value) {
            status.textContent = "선택된 프리셓이 없습니다 — ‘현재 연결 값으로 새 프리셓 저장’으로 먼저 프리셓을 만드세요.";
            return;
          }
          const data = await request(testOnly ? "api/rag/test" : "api/rag/settings", testOnly ? values() : saveBody(), testOnly ? "POST" : "PUT");
          status.textContent = testOnly ? `연결 확인됨 · 실제 차원 ${data.dimensions} · rerank ${data.rerank ? "확인됨" : "사용 안 함"}` : `프리셓 ${data.preset}을 덮어써 저장했습니다 · 이 프리셓이 활성화됐고 기존 문서의 임베딩을 이 모델로 소급합니다.`;
          if (!testOnly) afterSave(data);
        } catch (error) { status.textContent = error.message; }
        finally { save.disabled = test.disabled = selftest.disabled = pauseBtn.disabled = false; }
      }
      selftest.onclick = async () => {
        save.disabled = test.disabled = selftest.disabled = pauseBtn.disabled = true; status.textContent = "Selftest 실행 중…";
        try {
          const data = await request("api/rag/selftest", {threshold: 0.5, top_k: 5, sample: 20});
          const verdict = data.failed === 0 ? "통과" : `미통과 ${data.failed}/${data.sampled}건`;
          const sim = data.similarity || {};
          selftestResult.textContent = `Selftest ${verdict} · 임계값 ${data.threshold} · 유사도 min ${sim.min} / 중간 ${sim.median} / max ${sim.max}${data.failures && data.failures.length ? ` · 미달: ${data.failures.slice(0, 3).map(f => f.id).join(", ")}${data.failures.length > 3 ? " 등" : ""}` : ""}`;
          selftestResult.hidden = false;
          status.textContent = "현재 설정";
        } catch (error) {
          selftestResult.textContent = `Selftest 실패 · ${error.message}`;
          selftestResult.hidden = false;
          status.textContent = "현재 설정";
        }
        finally { save.disabled = test.disabled = selftest.disabled = pauseBtn.disabled = false; }
      };
      test.onclick = () => action(true); form.onsubmit = event => { event.preventDefault(); action(false); };
      form.append(save, test, selftest, pauseBtn); card.append(form); status.textContent = "현재 설정";
      loadDbs();
    }).catch(error => { status.textContent = error.message; });
    return card;
  }
  return {open, settingsCard, shortcut};
})();
