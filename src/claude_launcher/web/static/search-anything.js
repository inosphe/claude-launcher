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
  // The store slug the daemon writes a DB name as (store.rag_model_key_slug):
  // lowercase, every run of other characters one hyphen, trimmed, 48 chars.
  // The card shows it before a save so the name on disk is never a surprise.
  function ragDbSlug(name) {
    return String(name || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 48);
  }
  // A preset's DB as the daemon resolves it: its db, else its model key,
  // else the embedding model's slug (store.rag_config).
  function ragPresetDb(p) {
    if (!p) return "";
    return ragDbSlug(p.db) || ragDbSlug(p.model_key || p.embedding_model);
  }
  // Why a preset id cannot be used, or "" when it can (store.RAG_PRESET_ID_RE
  // plus the list's own limits).
  function ragPresetIdProblem(id, presets, max, except) {
    if (!id) return "프리셋 이름을 입력하세요.";
    if (!/^[a-z0-9][a-z0-9-]{0,31}$/.test(id))
      return "프리셋 이름은 영문 소문자·숫자로 시작하고 영문 소문자·숫자·하이픈만 씁니다(최대 32자).";
    if (id !== except && (presets || []).some(p => p.id === id)) return `프리셋 ${id}이 이미 있습니다. 다른 이름을 쓰세요.`;
    if (except === undefined && max && (presets || []).length >= max)
      return `프리셋은 ${max}개까지 저장됩니다. 프리셋을 삭제한 뒤 다시 시도하세요.`;
    return "";
  }
  // The DB panel's rows: one per named DB (its collections across corpora
  // folded together), and one per collection of the pre-preset format,
  // which has no name to group by (claunch-r4796).
  function ragDbGroups(rows) {
    const groups = [], named = new Map();
    for (const row of Array.isArray(rows) ? rows : []) {
      if (!row) continue;
      const presets = Array.isArray(row.presets) ? row.presets : [];
      if (!row.identity) {
        groups.push({ name: row.name, legacy: true, fill: ragDbSlug(row.model_key || row.model),
                      collections: [row], dims: row.dims ? [row.dims] : [], models: row.model ? [row.model] : [],
                      presets: [...presets], in_use: Boolean(row.in_use), bytes: Number(row.bytes) || 0 });
        continue;
      }
      let g = named.get(row.identity);
      if (!g) {
        g = { name: row.identity, legacy: false, fill: row.identity, collections: [], dims: [], models: [],
              presets: [], in_use: false, bytes: 0 };
        named.set(row.identity, g); groups.push(g);
      }
      g.collections.push(row);
      if (row.dims && !g.dims.includes(row.dims)) g.dims.push(row.dims);
      if (row.model && !g.models.includes(row.model)) g.models.push(row.model);
      for (const p of presets) if (!g.presets.includes(p)) g.presets.push(p);
      g.in_use = g.in_use || Boolean(row.in_use);
      g.bytes += Number(row.bytes) || 0;
    }
    return groups;
  }
  function ragBytes(n) {
    n = Number(n) || 0;
    if (n >= 1 << 30) return `${(n / (1 << 30)).toFixed(1)} GB`;
    if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(1)} MB`;
    if (n >= 1 << 10) return `${Math.round(n / (1 << 10))} KB`;
    return `${n} B`;
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
    card.append(node("p", "의미 검색에 쓰는 embedding·rerank 연결을 프리셋으로 저장합니다. 검색과 색인에는 활성 프리셋 하나만 쓰입니다.", "wf-note"));
    settingsElement = card;
    const status = node("p", "설정을 불러오는 중…", "rag-status"); status.setAttribute("role", "status"); card.append(status);
    request("api/rag/settings").then(cfg => {
      const form = document.createElement("form"), fields = {};
      const hint = (text, parent) => { const p = node("p", text, "rag-hint"); if (parent) parent.append(p); return p; };
      const button = (text, cls = "wf-btn clear") => { const b = node("button", text, cls); b.type = "button"; return b; };
      const section = title => { const box = document.createElement("fieldset"); box.className = "rag-section"; box.append(node("legend", title)); return box; };
      // Every control that writes is locked while one write is in flight, so
      // two PUTs never race over the same preset list.
      const writers = [];
      const busy = on => { for (const b of writers) b.disabled = on; if (!on) presetLimits(); };

      // ---- what is in force: the active preset and the backfill ----------
      // The daemon's own record of what it serves plus the backfill progress
      // after a switch; refreshed by watchBackfill. Kept apart from the
      // picker below, which only chooses what the fields edit.
      const summary = node("div", "", "rag-summary");
      const activeRow = node("div", "", "rag-summary-row"), activeLine = node("span", "", "rag-summary-value");
      activeRow.append(node("span", "활성 프리셋", "rag-summary-key"), activeLine);
      const backfillRow = node("div", "", "rag-summary-row");
      const ragState = node("span", "", "rag-preset-state");
      const pauseBtn = button("일시정지");
      backfillRow.append(node("span", "소급 임베딩", "rag-summary-key"), ragState, pauseBtn);
      summary.append(activeRow, backfillRow); card.insertBefore(summary, status);
      let backfillPolls = 0, backfillPaused = false;
      async function watchBackfill() {
        try {
          const st = await request("api/rag/status");
          backfillPaused = Boolean(st.backfill_paused);
          pauseBtn.textContent = backfillPaused ? "재개" : "일시정지";
          const line = ragBackfillStateLine(st.indexes, st.active, backfillPaused);
          ragState.textContent = line.text;
          if (line.state === "running" && backfillPolls < 200) {
            backfillPolls++;
            setTimeout(watchBackfill, 3000);
          }
        } catch { /* the status read is best-effort; leave the last line */ }
      }
      watchBackfill();
      // Pause holds the daemon's backfill consumer before its next key; the
      // in-flight pass finishes, queued corpora keep their order, and resume
      // drains from where the pause left it (POST api/rag/backfill/pause|resume).
      pauseBtn.onclick = async () => {
        busy(true);
        const resuming = backfillPaused;
        status.textContent = resuming ? "소급 임베딩을 재개하는 중…" : "소급 임베딩을 일시정지하는 중…";
        try {
          const data = await request(resuming ? "api/rag/backfill/resume" : "api/rag/backfill/pause", {}, "POST");
          backfillPaused = Boolean(data.paused);
          pauseBtn.textContent = backfillPaused ? "재개" : "일시정지";
          status.textContent = backfillPaused
            ? "소급 임베딩을 일시정지했습니다. 재개하면 남은 문서부터 이어서 처리합니다."
            : "소급 임베딩을 재개했습니다.";
          watchBackfill();
        } catch (error) {
          status.textContent = error.message;
        } finally { busy(false); }
      };
      writers.push(pauseBtn);

      function field(key, title, type = "text", parent = form, note = "") {
        const label = node("label", ""), input = document.createElement("input");
        const head = node("span", title, "rag-field-title");
        input.type = type; input.name = key; input.value = type === "password" ? "" : cfg[key] ?? "";
        if (type === "number") { input.min = key === "watch_interval" ? "0" : "1"; input.max = "10000"; }
        if (type === "password") { input.autocomplete = "new-password"; input.placeholder = cfg.api_key_set ? "저장됨 · 비워 두면 그대로 유지" : "API key"; }
        if (type === "checkbox") { input.checked = cfg[key]; label.className = "rag-check"; label.append(input, head); }
        else label.append(head, input);
        parent.append(label); fields[key] = input;
        if (note) hint(note, parent);
        return input;
      }

      // Two connection paths, each stored with its own endpoint and model ids
      // (claunch-pxxn4). The model inputs are shared on screen but are saved
      // into the path the radio picks, and switching the radio reloads that
      // path's saved values -- so a custom oMLX model id and a provider's
      // model id never overwrite each other.
      const saved = cfg.sources || { custom: {}, profile: {} };
      // Model presets (claunch-7m5v8) are the only connection model left
      // (claunch-rag-preset-always-hn5bc): the picker addresses one, its
      // values load into the fields below, and the save writes them back
      // over that preset and makes it active.
      const presets = Array.isArray(cfg.presets) ? cfg.presets : [];
      // The preset the daemon is running with -- separate from the picker's
      // selection, because selecting only loads values into the fields.
      // Renaming an id moves this pointer only when the pointer WAS that id;
      // moving it otherwise would activate a preset the reader only renamed
      // (claunch-fhs7f.1 review).
      let activeId = typeof cfg.preset === "string" ? cfg.preset : "";
      const presetMax = Number(cfg.preset_max) > 0 ? Number(cfg.preset_max) : 20;

      // ---- 1. preset ------------------------------------------------------
      const presetBox = section("1. 프리셋");
      const presetLabel = node("label", "", "rag-preset-row"), presetSelect = document.createElement("select");
      presetSelect.name = "preset";
      const presetBadge = node("span", "", "rag-badge");
      const presetCount = node("span", "", "rag-preset-count");
      presetLabel.append(node("span", "편집할 프리셋", "rag-field-title"), presetSelect, presetBadge, presetCount);
      presetBox.append(presetLabel);
      const presetNote = hint("", presetBox);
      hint("프리셋을 고르면 그 값이 아래 칸에 채워집니다. ‘저장하고 활성화’를 누르기 전에는 서버 설정이 바뀌지 않습니다. 다른 프리셋을 고르면 저장하지 않은 수정 내용은 사라집니다.", presetBox);
      const presetActions = node("div", "", "rag-actions-row");
      const renameOpen = button("이름 변경"), createOpen = button("새 프리셋으로 저장"), deletePresetBtn = button("삭제", "wf-btn clear rag-preset-delete");
      presetActions.append(renameOpen, createOpen, deletePresetBtn); presetBox.append(presetActions);
      // One inline sub-form at a time: the rename and the new-preset input
      // each say their rule next to the field instead of after a failure.
      function subform(title, go, note) {
        const box = node("div", "", "rag-subform"); box.hidden = true;
        const label = node("label", ""), input = document.createElement("input");
        input.placeholder = "예: qwen3-8b-omlx"; input.maxLength = 32; input.autocomplete = "off";
        label.append(node("span", title, "rag-field-title"), input);
        const problem = node("p", "", "rag-problem");
        const row = node("div", "", "rag-actions-row");
        const ok = button(go, "wf-btn option"), cancel = button("취소");
        row.append(ok, cancel); box.append(label, hint(note), problem, row);
        cancel.onclick = () => { box.hidden = true; input.value = ""; problem.textContent = ""; };
        return { box, input, ok, problem };
      }
      const rename = subform("새 프리셋 이름", "이름 변경",
        "영문 소문자·숫자로 시작하고 영문 소문자·숫자·하이픈만 씁니다(최대 32자). 저장된 API key, 연결 값, DB는 그대로 유지되고 활성 여부도 바뀌지 않습니다.");
      const create = subform("새 프리셋 이름", "만들고 활성화",
        `지금 아래 칸에 있는 값으로 새 프리셋을 만들고 바로 활성 프리셋으로 바꿉니다. 이름 규칙은 위와 같고, 프리셋은 ${presetMax}개까지 저장됩니다.`);
      presetBox.append(rename.box, create.box);
      renameOpen.onclick = () => { create.box.hidden = true; rename.box.hidden = false; rename.input.value = presetSelect.value; rename.input.focus(); };
      createOpen.onclick = () => { rename.box.hidden = true; create.box.hidden = false; create.input.focus(); };
      for (const sub of [rename, create]) sub.input.oninput = () => {
        const id = sub.input.value.trim();
        sub.problem.textContent = id ? ragPresetIdProblem(id, presets, sub === create ? presetMax : 0, sub === rename ? presetSelect.value : undefined) : "";
      };
      form.append(presetBox);

      // ---- 2. connection ----------------------------------------------------
      const connBox = section("2. 연결");
      const sources = {};
      for (const [value, title, note] of [
        ["custom", "Custom API", "OpenAI 호환 주소와 API key를 직접 입력합니다(oMLX, TEI 등)."],
        ["profile", "Profile", "claunch 프로필에 등록된 provider의 주소와 API key를 씁니다."],
      ]) {
        const label = node("label", "", "rag-radio"), radio = document.createElement("input");
        radio.type = "radio"; radio.name = "rag-source"; radio.value = value; radio.checked = (cfg.source || "custom") === value;
        const text = node("span", ""); text.append(node("strong", title), node("span", " — " + note, "rag-hint-inline"));
        label.append(radio, text); connBox.append(label); sources[value] = radio;
      }
      const customBox = node("div", "", "rag-source-box");
      field("base_url", "API 주소 (필수)", "text", customBox,
        "예: https://host/v1 — 주소가 /v1 같은 버전 경로로 끝나지 않으면 요청할 때 /v1을 붙입니다.");
      fields.base_url.placeholder = "https://host/v1";
      field("api_key", "API key", "password", customBox, "입력하지 않으면 이 프리셋에 저장된 key를 그대로 씁니다.");
      // The /v1 correction's opt-out (claunch-rag-preset-always-hn5bc.1): a
      // root-serving endpoint (TEI spells /embeddings at the host root, so
      // <host>/v1/embeddings does not exist) says so here, and the flag is
      // saved on the preset row.
      field("root_serving", "루트 경로에 직접 서빙 (TEI 등 — /v1 경로 없음)", "checkbox", customBox,
        "켜면 /v1을 붙이지 않고 입력한 주소 바로 아래의 /embeddings, /rerank를 호출합니다.");
      const profileBox = node("div", "", "rag-source-box");
      const profileLabel = node("label", ""), profileSelect = document.createElement("select");
      profileSelect.name = "profile"; profileSelect.append(new Option("프로필 선택", ""));
      const rows = cfg.profiles || [];
      for (const row of rows) profileSelect.append(new Option(`${row.name} · ${row.base_url}${row.has_key ? "" : " · API key 없음"}`, row.name));
      const chosenProfile = saved.profile?.profile || "";
      if (chosenProfile && !rows.some(row => row.name === chosenProfile)) profileSelect.append(new Option(`${chosenProfile} · 사용 불가`, chosenProfile));
      profileSelect.value = chosenProfile;
      profileLabel.append(node("span", "프로필 (필수)", "rag-field-title"), profileSelect); profileBox.append(profileLabel); fields.profile = profileSelect;
      hint("목록에는 OpenAI 호환 endpoint가 있는 프로필만 나옵니다. ‘API key 없음’ 프로필은 연결에 실패합니다.", profileBox);
      if (cfg.profile_error && cfg.source === "profile") profileBox.append(node("p", "현재 프로필을 사용할 수 없습니다: " + cfg.profile_error, "rag-problem"));
      connBox.append(customBox, profileBox);
      form.append(connBox);
      fields.base_url.value = saved.custom?.base_url ?? "";

      // ---- 3. models --------------------------------------------------------
      const modelBox = section("3. 모델");
      field("embedding_model", "Embedding 모델 (필수)", "text", modelBox,
        "차원 수는 모델 응답에서 자동으로 읽습니다. 모델을 바꾸고 아래 DB 이름을 비워 두면 새 모델 이름의 DB로 옮겨 기존 문서를 처음부터 다시 임베딩합니다.");
      field("rerank_model", "Rerank 모델 (선택)", "text", modelBox, "비워 두면 rerank 없이 벡터 유사도 순서로만 정렬합니다.");
      form.append(modelBox);

      // ---- 4. DB --------------------------------------------------------------
      // The collection this preset reads and writes. Empty keeps the pre-db
      // rule: the preset's model identity. Naming one is what lets a preset
      // change its model or provider and still answer from the vectors it
      // already has (claunch-fhs7f.1).
      const dbBox = section("4. 저장 DB");
      const dbLabel = node("label", "", "rag-preset-db"), dbInput = document.createElement("input");
      dbInput.name = "db"; dbInput.autocomplete = "off"; dbInput.placeholder = "비워 두면 Embedding 모델 이름에서 만듭니다";
      dbLabel.append(node("span", "DB 이름 (선택)", "rag-field-title"), dbInput); dbBox.append(dbLabel);
      const dbResolved = hint("", dbBox);
      hint("저장할 때 영문 소문자·숫자·하이픈만 남기고 나머지는 하이픈으로 바꿉니다(최대 48자). 이미 있는 DB를 쓰려면 아래 목록에서 이름을 누르세요. 차원 수가 다른 모델의 DB를 고르면 저장이 거절됩니다.", dbBox);
      const dbList = node("div", "", "rag-db-list"); dbBox.append(dbList);
      form.append(dbBox);
      // What the save will store, spelled out under the field: the slug of
      // what was typed, or the model-derived name an empty field falls back
      // to (claunch-r4796).
      function showDbResolved() {
        const typed = dbInput.value.trim(), slug = ragDbSlug(typed);
        const derived = ragDbSlug(fields.embedding_model.value);
        if (!typed) dbResolved.textContent = derived ? `저장되는 DB: ${derived} (Embedding 모델 이름에서 만든 이름)` : "저장되는 DB: Embedding 모델을 입력하면 정해집니다.";
        else if (!slug) dbResolved.textContent = "쓸 수 있는 글자가 없습니다. 영문 소문자·숫자·하이픈을 쓰세요.";
        else dbResolved.textContent = slug === typed ? `저장되는 DB: ${slug}` : `저장되는 DB: ${slug} (입력한 이름을 변환함)`;
        for (const b of dbList.querySelectorAll(".rag-db-use")) b.setAttribute("aria-pressed", String(b.dataset.db === (slug || derived)));
      }
      dbInput.oninput = showDbResolved;
      fields.embedding_model.addEventListener("input", showDbResolved);
      // The width the last connection test measured: a DB of another width
      // is marked, since a save pointing at it is refused.
      let testedDims = 0, dbGroups = [];
      // One row per DB. Clicking the name fills the field above; rename and
      // delete act on the DB as a whole (every corpus's collection). A DB a
      // preset reads cannot be deleted, so the button is disabled and says
      // why (claunch-fhs7f.1, claunch-r4796).
      function renderDbs() {
        dbList.replaceChildren();
        if (!dbGroups.length) {
          dbList.append(hint("저장된 DB가 없습니다. 프리셋을 저장하면 그 DB가 만들어집니다."));
          return;
        }
        const table = document.createElement("table"); table.className = "rag-db-table";
        const head = document.createElement("tr");
        for (const h of ["DB 이름", "차원", "만든 모델", "쓰는 프리셋", "크기", ""]) head.append(node("th", h));
        const thead = document.createElement("thead"); thead.append(head); table.append(thead);
        const body = document.createElement("tbody");
        for (const g of dbGroups) {
          const tr = document.createElement("tr"); tr.className = "rag-db-entry";
          const nameCell = node("td", "");
          const use = button(g.legacy ? `${g.name} (이전 형식)` : g.name, "rag-db-use");
          use.dataset.db = g.fill;
          use.title = g.fill ? `DB 이름 칸에 ${g.fill}을 채웁니다` : "이 컬렉션은 이름으로 지정할 수 없습니다";
          use.disabled = !g.fill;
          use.onclick = () => {
            dbInput.value = g.fill; showDbResolved();
            const width = g.dims.length === 1 ? g.dims[0] : 0;
            status.textContent = `DB 이름 칸에 ${g.fill}을 채웠습니다. ‘저장하고 활성화’를 눌러야 적용됩니다.` +
              (width && testedDims && width !== testedDims ? ` 이 DB는 ${width}차원이고 마지막 연결 테스트의 모델은 ${testedDims}차원이라 저장이 거절됩니다.` : "");
          };
          nameCell.append(use);
          if (!g.legacy && g.collections.length > 1) nameCell.append(node("span", ` 컬렉션 ${g.collections.length}개`, "rag-hint-inline"));
          const dimsCell = node("td", g.dims.length ? g.dims.map(d => `${d}`).join(", ") : "—");
          if (testedDims && g.dims.length && !g.dims.includes(testedDims)) {
            dimsCell.append(node("span", "차원 불일치", "rag-badge rag-badge-warn"));
          }
          const readers = g.presets.length ? g.presets.join(", ") : (g.in_use ? "현재 활성 설정" : "없음");
          const actions = node("td", "", "rag-db-actions");
          const occupied = Boolean(g.presets.length || g.in_use);
          const del = button("삭제", "wf-btn clear rag-db-delete");
          del.disabled = occupied;
          if (occupied) del.title = "이 DB를 쓰는 프리셋이 있어 삭제할 수 없습니다. 그 프리셋의 DB를 다른 DB로 바꾼 뒤 삭제하세요.";
          del.onclick = async () => {
            if (!window.confirm(`DB ${g.name}과 그 벡터를 삭제합니다. 되돌릴 수 없습니다. 계속할까요?`)) return;
            del.disabled = true;
            try {
              await request("api/rag/dbs/delete", g.legacy ? { name: g.name } : { db: g.name }, "POST");
              status.textContent = `DB ${g.name}을 삭제했습니다.`;
            } catch (error) {
              status.textContent = `DB 삭제 실패 · ${error.message}`;
            }
            await loadDbs();
          };
          if (!g.legacy) {
            const ren = button("이름 변경", "wf-btn clear rag-db-rename");
            ren.onclick = () => renameDbRow(tr, g);
            actions.append(ren);
          }
          actions.append(del);
          tr.append(nameCell, dimsCell, node("td", g.models.join(", ") || "—"), node("td", readers),
                    node("td", ragBytes(g.bytes)), actions);
          body.append(tr);
        }
        table.append(body);
        const scroller = node("div", "", "rag-db-scroll"); scroller.append(table);
        dbList.append(scroller);
        hint("DB 하나는 corpus(sessions, all, 보드별 beads)마다 컬렉션을 하나씩 가집니다. ‘이전 형식’은 프리셋 도입 전에 만든 컬렉션이고, 이름을 누르면 그 모델의 이름이 채워집니다.", dbList);
        showDbResolved();
      }
      // Rename one DB in place: an input row under it, the slug the daemon
      // will store shown as it is typed, then POST api/rag/dbs/rename. The
      // daemon moves the collections and repoints every preset that read the
      // old name, so the field above follows when it held the old name.
      function renameDbRow(tr, g) {
        if (tr.nextSibling && tr.nextSibling.classList && tr.nextSibling.classList.contains("rag-db-rename-row")) return;
        const row = document.createElement("tr"); row.className = "rag-db-rename-row";
        const cell = document.createElement("td"); cell.colSpan = 6;
        const input = document.createElement("input"); input.value = g.name; input.autocomplete = "off"; input.maxLength = 64;
        const shown = node("span", "", "rag-hint-inline");
        const go = button("이름 변경", "wf-btn option"), cancel = button("취소");
        const update = () => {
          const slug = ragDbSlug(input.value);
          shown.textContent = !slug ? " 쓸 수 있는 글자가 없습니다." : slug === g.name ? "" : ` 저장되는 이름: ${slug}`;
          go.disabled = !slug || slug === g.name || dbGroups.some(o => !o.legacy && o.name === slug);
          if (slug !== g.name && dbGroups.some(o => !o.legacy && o.name === slug)) shown.textContent = ` DB ${slug}이 이미 있습니다.`;
        };
        input.oninput = update;
        cancel.onclick = () => row.remove();
        go.onclick = async () => {
          const to = ragDbSlug(input.value);
          busy(true); go.disabled = true;
          status.textContent = `DB ${g.name}의 이름을 ${to}으로 바꾸는 중…`;
          try {
            const data = await request("api/rag/dbs/rename", { from: g.name, to }, "POST");
            // The field follows the rename whenever the preset on screen
            // reads that DB: by the typed name, or — with the field empty —
            // by its model name, or because the daemon re-pointed it. An
            // empty field would otherwise be saved as "no db", and the
            // preset would resolve back to the old name the vectors just
            // left (claunch-r4796 review).
            const screenDb = ragDbSlug(dbInput.value) || ragDbSlug(fields.embedding_model.value);
            const repointed = (data.presets || []).includes(presetSelect.value);
            if (screenDb === g.name || repointed) dbInput.value = data.db;
            for (const p of presets) if (data.presets && data.presets.includes(p.id)) p.db = data.db;
            const moved = data.presets && data.presets.length ? ` 이 DB를 쓰던 프리셋(${data.presets.join(", ")})도 새 이름을 쓰도록 바꿨습니다.` : "";
            status.textContent = `DB ${g.name}의 이름을 ${data.db}으로 바꿨습니다. 벡터는 그대로입니다.${moved}`;
            showPreset();
          } catch (error) {
            status.textContent = `DB 이름 변경 실패 · ${error.message}`;
          } finally { busy(false); }
          await loadDbs();
        };
        const wrap = node("div", "", "rag-actions-row");
        wrap.append(node("span", "새 DB 이름", "rag-field-title"), input, go, cancel, shown);
        cell.append(wrap, hint("이 DB의 모든 컬렉션을 새 이름으로 옮기고, 이 DB를 쓰는 프리셋의 DB 이름도 함께 바꿉니다. 옮기는 동안 검색과 색인이 잠시 멈춥니다."));
        row.append(cell); tr.after(row); update(); input.focus();
      }
      async function loadDbs() {
        try {
          const data = await request("api/rag/dbs", undefined, "GET");
          dbGroups = ragDbGroups(data.dbs);
        } catch { dbGroups = []; }
        renderDbs();
      }

      // ---- 5. advanced ------------------------------------------------------
      const advanced = document.createElement("details"); advanced.className = "rag-section rag-advanced";
      advanced.append(node("summary", "5. 고급 설정 (모든 프리셋에 공통)"));
      for (const [key, title, note] of [
        ["candidates", "검색 후보 수", "벡터 검색에서 먼저 가져오는 결과 수입니다(1–10000)."],
        ["rerank_top", "Rerank 대상 수", "후보 중 rerank 모델에 보내는 상위 결과 수입니다(1–10000)."],
        ["batch", "Embedding 배치 크기", "embedding 요청 한 번에 보내는 문서 수입니다(1–10000)."],
        ["timeout", "요청 제한 시간 (초)", "embedding·rerank 요청 하나를 기다리는 최대 시간입니다."],
        ["watch_interval", "변경 확인 주기 (초)", "보드와 세션의 변경을 확인하는 간격입니다. 0이면 주기 확인을 하지 않습니다."],
      ]) field(key, title, "number", advanced, note);
      field("verify_tls", "TLS 인증서 검증", "checkbox", advanced, "자체 서명 인증서를 쓰는 endpoint에서만 끕니다.");
      form.append(advanced);

      function showSource() {
        const picked = sources.profile.checked ? "profile" : "custom";
        customBox.hidden = picked !== "custom"; profileBox.hidden = picked !== "profile";
        // The fields are repointed at the path now in force, so whichever way
        // the reader switches, the models shown are that path's.
        fields.embedding_model.value = saved[picked]?.embedding_model ?? "";
        fields.rerank_model.value = saved[picked]?.rerank_model ?? "";
        // The key field is not reloaded: it never holds a saved value, and
        // clearing on every switch would discard a key typed and then hidden
        // by one look at the other path.
        showDbResolved();
      }
      for (const radio of Object.values(sources)) radio.onchange = showSource;
      // Rebuilds the options and keeps the selection when it still exists:
      // the picker chooses what the fields edit, and a refill must not move
      // the reader to another preset.
      function fillPicker() {
        const keep = presetSelect.value;
        presetSelect.length = 0;
        for (const p of presets) presetSelect.append(new Option(ragPresetOptionLabel(p) + (p.id === activeId ? " (활성)" : ""), p.id));
        if (presets.some(p => p.id === keep)) presetSelect.value = keep;
        presetLimits();
      }
      // The two preset controls a list limit disables, with the reason; run
      // after every write so the busy lock does not leave them enabled.
      function presetLimits() {
        presetCount.textContent = `${presets.length}/${presetMax}개`;
        createOpen.disabled = presets.length >= presetMax;
        createOpen.title = createOpen.disabled ? `프리셋은 ${presetMax}개까지 저장됩니다. 먼저 하나를 삭제하세요.` : "";
        deletePresetBtn.disabled = presets.length <= 1;
        deletePresetBtn.title = deletePresetBtn.disabled ? "마지막 프리셋은 삭제할 수 없습니다. 값을 바꾸려면 고쳐서 저장하세요." : "";
      }
      function showPreset() {
        const selected = presets.find(p => p.id === presetSelect.value);
        const active = presets.find(p => p.id === activeId);
        activeLine.textContent = active ? ragPresetSummary(active) + ` · DB ${ragPresetDb(active) || "—"}` : "없음 — 프리셋을 저장하면 그 프리셋이 활성화됩니다.";
        presetBadge.textContent = !selected ? "" : selected.id === activeId ? "활성" : "비활성";
        presetBadge.className = "rag-badge" + (selected && selected.id === activeId ? " rag-badge-on" : "");
        presetNote.textContent = !selected
          ? "저장된 프리셋이 없습니다. 아래 칸에 연결 값을 입력하고 ‘새 프리셋으로 저장’으로 첫 프리셋을 만드세요."
          : selected.id === activeId
            ? "지금 쓰이는 프리셋을 편집합니다. 저장하면 바로 반영됩니다."
            : `편집 중인 ${selected.id}은 활성 프리셋이 아닙니다. 저장하면 이 프리셋이 활성 프리셋으로 바뀝니다.`;
        saveBtn.textContent = selected ? `${selected.id} 저장하고 활성화` : "저장하고 활성화";
      }
      // Load a preset's values into the fields (local only). Both source rows
      // mirror it: a preset is one connection, not two paths' worth.
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
        fields.api_key.value = ""; fields.api_key.placeholder = data.api_key_set ? "저장됨 · 비워 두면 그대로 유지" : "API key";
        Object.assign(saved, data.sources || {});
        if (Array.isArray(data.presets)) {
          presets.length = 0;
          presets.push(...data.presets);
        }
        if (typeof data.preset === "string") activeId = data.preset;
        const keep = presetSelect.value;
        fillPicker();
        presetSelect.value = presets.some(p => p.id === keep) ? keep : activeId;
        showPreset();
      }
      function values() {
        const out = Object.fromEntries(Object.entries(fields).map(([key, input]) => [key, input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value]));
        const picked = sources.profile.checked ? "profile" : "custom";
        out.source = picked;
        out.preset = presetSelect.value;
        // Both rows go with every save: the picked path takes what is on
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
      // replaced by what is on screen, plus the pointer. The test probe sends
      // values() as-is; the daemon's /api/rag/test ignores the preset keys and
      // probes the submitted rows, so both paths test the screen.
      function saveBody() {
        const body = values();
        const id = presetSelect.value;
        if (!id) return body;
        body.presets = presets.map(p => p.id === id ? editedPreset(id) : p);
        return body;
      }

      // ---- preset writes ------------------------------------------------------
      // A new preset: the screen values appended to the list the card loaded,
      // made active (the settings PUT takes the whole saved list).
      create.ok.onclick = async () => {
        const id = create.input.value.trim();
        const problem = ragPresetIdProblem(id, presets, presetMax);
        if (problem) { create.problem.textContent = problem; return; }
        const preset = editedPreset(id);
        busy(true); status.textContent = `프리셋 ${id}을 만드는 중…`;
        try {
          const data = await request("api/rag/settings", { ...values(), presets: [...presets, preset], preset: id }, "PUT");
          presetSelect.append(new Option(id, id)); presetSelect.value = id;
          afterSave(data);
          create.box.hidden = true; create.input.value = ""; create.problem.textContent = "";
          status.textContent = `프리셋 ${id}을 만들고 활성화했습니다. 기존 문서의 임베딩을 이 모델로 소급합니다.`;
        } catch (error) {
          status.textContent = `프리셋 저장 실패 · ${error.message}`;
        } finally { busy(false); }
      };
      // Delete one preset row: the list minus the row, with the pointer moved
      // to the first remaining preset when the active one went away. The
      // card keeps at least one preset -- an empty list would be re-wrapped
      // by the read-side migration and the delete would appear to do nothing
      // (claunch-rag-preset-always-hn5bc.2).
      deletePresetBtn.onclick = async () => {
        const id = presetSelect.value;
        if (!id) { status.textContent = "삭제할 프리셋을 먼저 고르세요."; return; }
        if (presets.length <= 1) {
          status.textContent = "마지막 프리셋은 삭제할 수 없습니다. 값을 바꾸려면 이 프리셋을 고쳐 저장하세요.";
          return;
        }
        const wasActive = id === activeId;
        if (!window.confirm(`프리셋 ${id}을 삭제합니다. 저장된 연결 값과 API key가 지워집니다. DB와 벡터는 남습니다.` +
          (wasActive ? " 활성 프리셋이므로 남은 첫 프리셋이 활성화됩니다." : ""))) return;
        const remaining = presets.filter(p => p.id !== id);
        const next = wasActive ? (remaining.length ? remaining[0].id : "") : activeId;
        busy(true); status.textContent = `프리셋 ${id}을 삭제하는 중…`;
        try {
          const data = await request("api/rag/settings", { ...values(), presets: remaining, preset: next }, "PUT");
          afterSave(data);
          presetSelect.value = activeId;
          loadPreset(presets.find(p => p.id === presetSelect.value));
          showPreset();
          status.textContent = wasActive ? `프리셋 ${id}을 삭제했습니다. 활성 프리셋은 ${activeId}입니다.` : `프리셋 ${id}을 삭제했습니다.`;
        } catch (error) {
          status.textContent = `프리셋 삭제 실패 · ${error.message}`;
        } finally { busy(false); }
      };
      // Rename a preset. The id is the name and also the key its saved secret
      // is filed under, so the row carries renamed_from and the daemon moves
      // the secret across. The vectors do not move: the collection is named
      // by db/model_key, not by the preset id (claunch-fhs7f.1).
      rename.ok.onclick = async () => {
        const from = presetSelect.value, to = rename.input.value.trim();
        if (!from) { rename.problem.textContent = "이름을 바꿀 프리셋을 먼저 고르세요."; return; }
        if (to === from) { rename.box.hidden = true; return; }
        const problem = ragPresetIdProblem(to, presets, 0, from);
        if (problem) { rename.problem.textContent = problem; return; }
        const rows = presets.map(p => p.id === from ? { ...p, id: to, renamed_from: from } : p);
        // The pointer follows only if this preset was the active one. values()
        // carries the picker's selection, which is not the active preset, so
        // the rename body states the pointer explicitly (claunch-fhs7f.1).
        const body = { ...values(), presets: rows };
        body.preset = activeId === from ? to : activeId;
        busy(true); status.textContent = `프리셋 ${from}의 이름을 ${to}으로 바꾸는 중…`;
        try {
          const data = await request("api/rag/settings", body, "PUT");
          presetSelect.append(new Option(to, to)); presetSelect.value = to;
          afterSave(data);
          presetSelect.value = to;
          rename.box.hidden = true; rename.problem.textContent = "";
          loadPreset(presets.find(p => p.id === to));
          showPreset();
          status.textContent = activeId === to
            ? `프리셋 ${from}의 이름을 ${to}으로 바꿨습니다. API key와 연결 값은 그대로이고, 활성 프리셋도 ${to}입니다.`
            : `프리셋 ${from}의 이름을 ${to}으로 바꿨습니다. API key와 연결 값은 그대로이고, 활성 프리셋은 ${activeId || "(없음)"} 그대로입니다.`;
        } catch (error) {
          status.textContent = `이름 변경 실패 · ${error.message}`;
        } finally { busy(false); }
      };

      // ---- actions ----------------------------------------------------------
      const actions = node("div", "", "rag-actions-row rag-footer");
      const saveBtn = node("button", "저장하고 활성화", "wf-btn approve"); saveBtn.type = "submit";
      const test = button("연결 테스트", "wf-btn"), selftest = button("Selftest", "wf-btn");
      actions.append(saveBtn, test, selftest);
      form.append(actions);
      hint("‘저장하고 활성화’는 편집 중인 프리셋을 화면의 값으로 덮어쓰고 활성 프리셋으로 바꿉니다. 연결 방식, 주소, Embedding 모델, DB가 바뀌면 색인을 다시 구성합니다. ‘연결 테스트’는 저장하지 않고 화면의 값으로 접속만 확인합니다. ‘Selftest’는 활성 프리셋의 색인에서 표본 문서가 검색되는지 확인합니다.", form);
      const selftestResult = node("p", "", "rag-selftest-result"); selftestResult.hidden = true; form.append(selftestResult);
      writers.push(saveBtn, test, selftest, renameOpen, createOpen, deletePresetBtn, rename.ok, create.ok);

      async function action(testOnly) {
        if (!testOnly && !presetSelect.value) {
          status.textContent = "선택된 프리셋이 없습니다. ‘새 프리셋으로 저장’으로 먼저 프리셋을 만드세요.";
          return;
        }
        busy(true); status.textContent = testOnly ? "연결을 확인하는 중…" : "저장하는 중…";
        try {
          const data = await request(testOnly ? "api/rag/test" : "api/rag/settings", testOnly ? values() : saveBody(), testOnly ? "POST" : "PUT");
          if (testOnly) {
            testedDims = Number(data.dimensions) || 0;
            status.textContent = `연결 확인됨 · 이 모델의 차원 ${data.dimensions} · rerank ${data.rerank ? "확인됨" : "사용 안 함"}`;
            renderDbs();
          } else {
            afterSave(data);
            status.textContent = `프리셋 ${data.preset}을 저장했고 활성 프리셋이 됐습니다. 바뀐 값에 따라 기존 문서를 이 모델로 소급 임베딩합니다.`;
          }
        } catch (error) { status.textContent = error.message; }
        finally { busy(false); }
      }
      selftest.onclick = async () => {
        busy(true); status.textContent = "Selftest 실행 중…";
        try {
          const data = await request("api/rag/selftest", {threshold: 0.5, top_k: 5, sample: 20});
          const verdict = data.failed === 0 ? "통과" : `미통과 ${data.failed}/${data.sampled}건`;
          const sim = data.similarity || {};
          selftestResult.textContent = `Selftest ${verdict} · 임계값 ${data.threshold} · 유사도 min ${sim.min} / 중간 ${sim.median} / max ${sim.max}${data.failures && data.failures.length ? ` · 미달: ${data.failures.slice(0, 3).map(f => f.id).join(", ")}${data.failures.length > 3 ? " 등" : ""}` : ""}`;
        } catch (error) {
          selftestResult.textContent = `Selftest 실패 · ${error.message}`;
        }
        selftestResult.hidden = false; status.textContent = "";
        busy(false);
      };
      test.onclick = () => action(true); form.onsubmit = event => { event.preventDefault(); action(false); };

      // Selecting a preset only loads its values. Activation -- the daemon
      // restart and the backfill it starts -- happens on save, so peeking at
      // another preset costs nothing (search_anything.py restarts the service
      // and enqueues "all" on the settings PUT).
      presetSelect.onchange = () => {
        rename.box.hidden = true;
        loadPreset(presets.find(p => p.id === presetSelect.value));
        showPreset();
      };
      fillPicker();
      presetSelect.value = activeId || (presets[0] ? presets[0].id : "");
      if (presetSelect.value) loadPreset(presets.find(p => p.id === presetSelect.value));
      else showSource();
      showPreset();
      card.append(form); status.textContent = "";
      loadDbs();
    }).catch(error => { status.textContent = error.message; });
    return card;
  }
  return {open, settingsCard, shortcut};
})();
