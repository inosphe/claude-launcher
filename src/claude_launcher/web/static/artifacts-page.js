/* The Artifacts page (#/artifacts): what artifact cleanup looks at, session
   by session -- the scratch directory and the basetemp trees each session
   owns, their disk usage, the orphans no session owns, and the cleanup
   receipts and schedules the daemon keeps. Drawn from GET /api/artifacts;
   sizes are walked in the background by POST /api/artifacts/measure
   (daemon/artifact_inventory.py), so the page never waits on a walk. */
globalThis.ArtifactsPage = (() => {
  const AREA_LABEL = { basetemp: "basetemp", scratch: "스크래치" };
  const KIND_LABEL = {
    "test-dir": "세션 테스트 디렉터리",
    sweep: "스윕",
    run: "테스트 실행",
    manual: "직접 지정",
    pytest: "pytest",
    browser: "브라우저 프로필",
    preview: "프리뷰 워크트리",
    scratch: "스크래치 전체",
    other: "기타",
    orphan: "고아",
  };
  const KIND_TITLE = {
    "test-dir": "$CLAUNCH_TEST_DIR — 이 세션이 pytest 밖 산출물을 쓰는 디렉터리",
    sweep: "tools/sweep.py가 만든 베이스텀프",
    run: "tools/changed_tests.py가 실행마다 만든 베이스텀프",
    manual: "세션이 직접 이름 붙인 베이스텀프",
    scratch: "세션의 스크래치 디렉터리 전체 — 아래 스크래치 항목은 이 안에 들어 있다",
    orphan: "레지스트리의 어느 세션 이름과도 맞지 않아 exit reaper가 집지 않는다",
  };
  const UNITS = ["B", "KB", "MB", "GB", "TB"];

  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  /* Binary units, one decimal below ten so "1.4 GB" and "940 MB" are told
     apart at a glance. A missing measurement says so instead of showing 0. */
  function formatBytes(bytes) {
    if (!Number.isFinite(bytes)) return "미측정";
    let value = Math.max(0, bytes), unit = 0;
    while (value >= 1024 && unit < UNITS.length - 1) { value /= 1024; unit += 1; }
    return unit === 0 ? `${Math.round(value)} B`
      : `${value.toFixed(value >= 10 ? 0 : 1)} ${UNITS[unit]}`;
  }

  /* How long ago a measurement was taken, in the reader's own clock. */
  function age(iso, now = Date.now()) {
    const then = new Date(iso || "").getTime();
    if (!Number.isFinite(then)) return "";
    const seconds = Math.max(0, Math.round((now - then) / 1000));
    if (seconds < 60) return "방금 전";
    if (seconds < 3600) return `${Math.round(seconds / 60)}분 전`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)}시간 전`;
    return `${Math.round(seconds / 86400)}일 전`;
  }

  /* Sessions live first (they are the ones a cleanup can act on), then by
     name; the name is a number, so "s99" sorts before "s995". */
  function sortedSessions(sessions) {
    return [...(sessions || [])].sort((a, b) =>
      (b.live ? 1 : 0) - (a.live ? 1 : 0) ||
      String(a.name).localeCompare(String(b.name), undefined, { numeric: true }));
  }

  function matches(session, query) {
    const q = String(query || "").trim().toLowerCase();
    if (!q) return true;
    const haystack = [session.name, session.cwd, session.category,
      ...(session.items || []).map(row => `${row.path} ${row.kind}`)];
    return haystack.some(value => String(value || "").toLowerCase().includes(q));
  }

  /* Whether a session holds anything worth listing. A fleet of hundreds of
     sessions leaves an empty scratch directory behind for most of them, and a
     card apiece makes the page a list of nothings; the empty ones are one
     checkbox away. */
  function hasContent(session) {
    const scratch = session.scratch || {};
    const cleanup = session.cleanup || {};
    return (session.items || []).length > 0 ||
      Number(scratch.entries) > 0 || Number.isFinite(scratch.bytes) ||
      !!cleanup.scheduled || (cleanup.receipts || []).length > 0;
  }

  /* The reader's two category filters, each its own checkbox: a fleet keeps
     hundreds of killed and archived sessions in the registry, and someone
     chasing live disk usage should not have to scroll past them. Both are off
     by default, so the page opens on the same list it always showed. */
  function excluded(session, options) {
    const opts = options || {};
    if (opts.excludeKilled && session.category === "killed") return true;
    if (opts.excludeArchived && session.category === "archived") return true;
    return false;
  }

  function visibleSessions(data, query, showEmpty = false, options = {}) {
    return sortedSessions(data && data.sessions)
      .filter(s => !excluded(s, options))
      .filter(s => (showEmpty || hasContent(s)) && matches(s, query));
  }

  /* How many sessions the category filters took away, for the line that says
     so -- a filter that hides rows silently reads as "none exist". */
  function hiddenByCategory(data, options) {
    return sortedSessions(data && data.sessions).filter(s => excluded(s, options)).length;
  }

  /* The paths the daemon would walk for this listing -- the same set its
     POST derives, read here only to tell the reader what is not measured yet. */
  function measurable(data) {
    return (data && data.measurable) || [];
  }

  function unmeasured(data) {
    const done = new Set();
    const seen = (row) => {
      // An error is an answer too: walking that path again this visit would
      // only fail again, so it is not "not measured yet".
      if (row && (Number.isFinite(row.bytes) || row.error)) done.add(row.path);
    };
    for (const row of (data && data.sessions) || []) {
      seen(row.scratch);
      for (const item of row.items || []) seen(item);
    }
    for (const row of (data && data.orphans) || []) seen(row);
    return measurable(data).filter(path => !done.has(path));
  }

  let data = null, error = "", loading = false, notice = "";
  let query = "", showEmpty = false, sequence = 0, pollTimer = null;
  // The category filters outlive a visit: leaving the page and coming back
  // keeps the list the reader narrowed down. Only a full page reload clears
  // them -- `stop` (which runs on every route change) leaves them alone.
  let excludeKilled = false, excludeArchived = false;
  // A walk the reader asked for, and the timer that watches it. Nothing here
  // starts on its own: the page used to re-render itself on open and while a
  // walk ran, which made reading impossible and hid a failing tree behind a
  // loop (the reader reported both).
  let measuring = false;

  function view() { return document.getElementById("artifacts-view"); }

  function schedulePoll(ms) {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(() => { pollTimer = null; tick(); }, ms);
  }

  /* The measure job as the bar reads it: how many paths it set out to walk
     and how many have answered. */
  function progress(source) {
    const job = (source && source.job) || {};
    const total = Number(job.total) || 0;
    const done = Math.min(total, Number(job.done) || 0);
    return { total, done, started: !!job.started, finished: !!job.finished };
  }

  async function load() {
    if (view() && view().classList.contains("hidden")) return;
    const seq = ++sequence;
    loading = true; render();
    try {
      const response = await api("/api/artifacts");
      const body = await response.json();
      if (seq !== sequence) return;
      if (!response.ok) throw Error(body.error || `HTTP ${response.status}`);
      data = body; error = "";
    } catch (exc) {
      if (seq !== sequence) return;
      error = String(exc.message || exc);
    } finally {
      if (seq !== sequence) return;
      loading = false; render();
    }
  }

  /* One poll of the walk the reader started. It refreshes the bar in place --
     rebuilding the list under the reader's cursor is the flicker they saw. */
  async function tick() {
    if (!measuring) return;
    const seq = ++sequence;
    try {
      const response = await api("/api/artifacts");
      const body = await response.json();
      if (seq !== sequence || !measuring) return;
      if (!response.ok) throw Error(body.error || `HTTP ${response.status}`);
      data = body; error = "";
      const state = progress(body);
      const running = (body.running || []).length;
      if (state.finished || (!running && state.done >= state.total)) {
        measuring = false;
        notice = `측정 완료 · ${state.done}개 경로`;
        render();
        return;
      }
      updateProgress();
      schedulePoll(700);
    } catch (exc) {
      if (seq !== sequence) return;
      measuring = false;
      error = String(exc.message || exc);
      render();
    }
  }

  async function requestMeasure(body) {
    try {
      const response = await api("/api/artifacts/measure", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
      const answer = await response.json();
      if (!response.ok) throw Error(answer.error || `HTTP ${response.status}`);
      if (!answer.paths) { notice = "측정할 경로가 없다"; render(); return; }
      notice = `측정 시작 · ${answer.paths}개 경로`;
      measuring = true;
      render();
      schedulePoll(500);
    } catch (exc) {
      notice = `측정 실패: ${exc.message || exc}`;
      measuring = false;
      render();
    }
  }

  function sizeText(row) {
    if (row.running) return "측정 중…";
    if (row.error) return "읽기 실패";
    if (!Number.isFinite(row.bytes)) return row.exists === false ? "없음" : "미측정";
    const ageText = age(row.at);
    return `${formatBytes(row.bytes)}${ageText ? ` · ${ageText}` : ""}`;
  }

  function itemRow(row) {
    const line = node("div", `ar-row ${row.area} ${row.kind}`);
    line.appendChild(node("span", "wt-chip ar-area", AREA_LABEL[row.area] || row.area));
    const kind = node("span", "wt-chip ar-kind", KIND_LABEL[row.kind] || row.kind);
    kind.title = KIND_TITLE[row.kind] || "";
    line.appendChild(kind);
    const path = node("code", "ar-path", row.path);
    path.title = row.path;
    line.appendChild(path);
    const size = node("span", "ar-size", sizeText(row));
    if (row.error) size.title = row.error;
    line.appendChild(size);
    if (row.stale) {
      const stale = node("span", "wt-chip ar-stale", "변경됨");
      stale.title = "측정한 뒤 디렉터리가 바뀌었다. 다시 측정하면 현재 값이 나온다";
      line.appendChild(stale);
    }
    if (row.reason) {
      const why = node("span", "wt-chip ar-why", "정리 제외");
      why.title = row.reason;
      line.appendChild(why);
    }
    if (row.modified) {
      const when = node("span", "ar-muted", `수정 ${age(row.modified)}`);
      when.title = row.modified;
      line.appendChild(when);
    }
    return line;
  }

  function cleanupBlock(cleanup) {
    const box = node("div", "ar-cleanup");
    const scheduled = cleanup && cleanup.scheduled;
    const line = node("div", "ar-cleanup-line");
    if (scheduled) {
      line.appendChild(node("span", "wt-chip ar-scheduled", "정리 예약됨"));
      const parts = [];
      if (scheduled.attempts != null) parts.push(`시도 ${scheduled.attempts}회`);
      if (scheduled.last_reason) parts.push(scheduled.last_reason);
      if (scheduled.created_at) parts.push(`예약 ${age(scheduled.created_at)}`);
      if (scheduled.expires_at) parts.push(`만료 ${age(scheduled.expires_at)}`);
      line.appendChild(node("span", "ar-muted", parts.join(" · ")));
    } else {
      line.appendChild(node("span", "ar-muted", "예약된 정리 없음"));
    }
    box.appendChild(line);
    for (const receipt of (cleanup && cleanup.receipts) || []) {
      const row = node("div", "ar-cleanup-line");
      row.appendChild(node("span", "wt-chip ar-receipt", "정리 기록"));
      const parts = [`기록 ${age(receipt.created_at)}`];
      if (Number.isFinite(receipt.removed)) parts.push(`삭제 ${receipt.removed}개`);
      if (Number.isFinite(receipt.bytes) && receipt.bytes) parts.push(formatBytes(receipt.bytes));
      if (Number.isFinite(receipt.cleanup_pending) && receipt.cleanup_pending) {
        parts.push(`보류 ${receipt.cleanup_pending}개`);
      }
      row.appendChild(node("span", "ar-muted", parts.join(" · ")));
      row.title = receipt.plan_id;
      box.appendChild(row);
    }
    return box;
  }

  function sessionCard(session) {
    const card = node("section", `ar-session${session.live ? " live" : ""}`);
    const head = node("div", "ar-session-head");
    const link = node("a", "ar-name", session.name);
    link.href = `#/s/${encodeURIComponent(session.name)}`;
    head.appendChild(link);
    head.appendChild(node("span", `wt-chip ${session.category}`, session.category));
    if (session.cwd) {
      const cwd = node("code", "ar-cwd", session.cwd);
      cwd.title = session.cwd;
      head.appendChild(cwd);
    }
    const totals = session.totals || {};
    head.appendChild(node("span", "ar-size",
      `${formatBytes(totals.bytes)} · 정리 후보 ${(session.items || []).length}개` +
      (totals.unmeasured ? ` · 미측정 ${totals.unmeasured}` : "")));
    const measure = node("button", "wf-btn", "측정");
    measure.type = "button";
    measure.title = "이 세션의 스크래치와 basetemp를 다시 잰다";
    measure.onclick = () => requestMeasure({ session: session.name, force: true });
    head.appendChild(measure);
    card.appendChild(head);

    const rows = node("div", "ar-rows");
    rows.appendChild(itemRow({ ...session.scratch, area: "scratch", kind: "scratch" }));
    const items = session.items || [];
    if (!items.length) rows.appendChild(node("p", "ar-muted", "정리 대상 항목 없음"));
    for (const item of items) rows.appendChild(itemRow(item));
    card.appendChild(rows);
    card.appendChild(cleanupBlock(session.cleanup));
    return card;
  }

  function progressText(state) {
    const pct = state.total ? Math.round((state.done / state.total) * 100) : 0;
    return `측정 중 ${state.done}/${state.total} (${pct}%)`;
  }

  function progressBar(source) {
    const state = progress(source);
    const bar = node("div", "ar-progress");
    const track = node("div", "ar-progress-track");
    const fill = node("div", "ar-progress-fill");
    fill.style.width = `${state.total ? Math.round((state.done / state.total) * 100) : 0}%`;
    track.appendChild(fill);
    bar.appendChild(track);
    bar.appendChild(node("span", "ar-progress-text", progressText(state)));
    return bar;
  }

  /* While a walk runs only the bar moves; the list below is left alone so the
     reader's place and selection survive the poll. */
  function updateProgress() {
    const root = view();
    const bar = root && root.querySelector(".ar-progress");
    if (!bar || !data) return;
    const state = progress(data);
    const fill = bar.querySelector(".ar-progress-fill");
    if (fill) fill.style.width = `${state.total ? Math.round((state.done / state.total) * 100) : 0}%`;
    const text = bar.querySelector(".ar-progress-text");
    if (text) text.textContent = progressText(state);
  }

  function render() {
    const root = view();
    if (!root || root.classList.contains("hidden")) return;
    const top = root.scrollTop;
    root.innerHTML = "";
    const head = node("div", "wf-head");
    head.appendChild(node("h2", null, "Artifacts"));
    const measureAll = node("button", "wf-btn", loading ? "읽는 중…" : "전체 측정");
    measureAll.type = "button";
    measureAll.disabled = loading;
    measureAll.title = "표시된 모든 경로를 다시 잰다 (백그라운드)";
    measureAll.onclick = () => requestMeasure({ force: true });
    head.appendChild(measureAll);
    const refresh = node("button", "wf-btn", "새로 고침");
    refresh.type = "button";
    refresh.disabled = loading;
    refresh.onclick = () => load();
    head.appendChild(refresh);
    root.appendChild(head);

    if (notice) root.appendChild(node("p", "wt-notice", notice));
    if (error) root.appendChild(node("p", "wt-error", `읽기 실패: ${error}`));
    if (measuring) root.appendChild(progressBar(data));
    if (!data) {
      if (loading) root.appendChild(node("p", "ar-muted", "읽는 중…"));
      root.scrollTop = top;
      return;
    }

    const totals = data.totals || {};
    const running = (data.running || []).length;
    const summary = node("p", "ar-summary");
    summary.appendChild(node("span", null,
      `세션 ${(data.sessions || []).length} · 측정된 디스크 ${formatBytes(totals.bytes)}`));
    if (totals.unmeasured) summary.appendChild(node("span", "ar-muted", ` · 미측정 ${totals.unmeasured}`));
    if (running) summary.appendChild(node("span", "wt-chip ar-running", `측정 중 ${running}`));
    root.appendChild(summary);
    const where = node("p", "ar-root");
    where.appendChild(node("span", "ar-muted", "basetemp 루트 "));
    where.appendChild(node("code", null, data.root ? data.root.path : ""));
    if (data.root && !data.root.exists) where.appendChild(node("span", "ar-muted", " (없음)"));
    root.appendChild(where);

    const bar = node("div", "wt-toolbar");
    const filter = node("input", "wt-filter");
    filter.type = "search";
    filter.placeholder = "세션·경로·종류 필터";
    filter.value = query;
    filter.oninput = () => { query = filter.value; renderBody(); };
    bar.appendChild(filter);
    const includeEmpty = node("label", "ar-check");
    const box = node("input");
    box.type = "checkbox";
    box.checked = showEmpty;
    box.onchange = () => { showEmpty = box.checked; renderBody(); };
    includeEmpty.append(box, document.createTextNode(" 빈 세션 포함"));
    includeEmpty.title = "스크래치가 비어 있고 정리 기록도 없는 세션까지 표시한다";
    bar.appendChild(includeEmpty);

    const noKilled = node("label", "ar-check");
    const killedBox = node("input");
    killedBox.type = "checkbox";
    killedBox.checked = excludeKilled;
    killedBox.onchange = () => { excludeKilled = killedBox.checked; renderBody(); };
    noKilled.append(killedBox, document.createTextNode(" killed 제외"));
    noKilled.title = "killed로 끝난 세션을 목록에서 뺀다";
    bar.appendChild(noKilled);

    const noArchived = node("label", "ar-check");
    const archivedBox = node("input");
    archivedBox.type = "checkbox";
    archivedBox.checked = excludeArchived;
    archivedBox.onchange = () => { excludeArchived = archivedBox.checked; renderBody(); };
    noArchived.append(archivedBox, document.createTextNode(" archived 제외"));
    noArchived.title = "archived된 세션을 목록에서 뺀다";
    bar.appendChild(noArchived);
    if (unmeasured(data).length) {
      bar.appendChild(node("span", "ar-muted", `미측정 ${unmeasured(data).length}개 경로`));
    }
    root.appendChild(bar);

    const body = node("div", "ar-body");
    root.appendChild(body);
    renderBody();
    root.scrollTop = top;
  }

  function renderBody() {
    const root = view();
    const body = root && root.querySelector(".ar-body");
    if (!body) return;
    body.innerHTML = "";
    if (!data) return;
    const options = { excludeKilled, excludeArchived };
    const sessions = visibleSessions(data, query, showEmpty, options);
    const total = (data.sessions || []).length;
    for (const session of sessions) body.appendChild(sessionCard(session));
    const filtered = hiddenByCategory(data, options);
    if (!sessions.length) {
      body.appendChild(node("p", "ar-muted",
        filtered ? `표시할 세션이 없다 — killed/archived 제외로 ${filtered}개를 뺐다.`
          : "표시할 세션이 없다."));
    } else if (sessions.length < total) {
      const parts = [];
      if (filtered) parts.push(`killed/archived 제외 ${filtered}개`);
      if (!showEmpty) parts.push("스크래치가 비어 있는 세션");
      body.appendChild(node("p", "ar-muted",
        `세션 ${total}개 중 ${total - sessions.length}개를 숨겼다 (${parts.join(", ")}).`));
    }

    const q = query.trim().toLowerCase();
    const orphans = (data.orphans || []).filter(row =>
      !q || `${row.path} ${row.kind}`.toLowerCase().includes(q));
    if (orphans.length) {
      const section = node("section", "ar-session orphaned");
      const head = node("div", "ar-session-head");
      head.appendChild(node("h3", "ar-name", "고아 항목"));
      head.appendChild(node("span", "ar-muted",
        "레지스트리의 어느 세션 이름과도 맞지 않아 exit reaper가 집지 않는다"));
      const total = orphans.reduce((sum, row) => sum + (Number.isFinite(row.bytes) ? row.bytes : 0), 0);
      head.appendChild(node("span", "ar-size", formatBytes(total)));
      section.appendChild(head);
      const rows = node("div", "ar-rows");
      for (const row of orphans) rows.appendChild(itemRow(row));
      section.appendChild(rows);
      body.appendChild(section);
    }
  }

  function open() {
    // No automatic walk and no polling on open: the list is read once and the
    // reader presses 측정 when they want sizes.
    notice = "";
    render();
    load();
  }

  function stop() {
    sequence++;
    loading = false;
    measuring = false;
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  return { open, stop, formatBytes, age, sortedSessions, visibleSessions,
    hiddenByCategory, progress, unmeasured, measurable, hasContent };
})();
