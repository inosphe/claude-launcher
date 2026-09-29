/* The Worktrees page (#/worktrees): every launcher worktree of the
   repositories the fleet has touched, grouped by when it was made, with the
   sessions that stand in it, whether its branch is in the trunk, and the
   removal of one checkout or of a whole group. Drawn from GET /api/worktrees;
   removal is POST /api/worktrees/remove (daemon/worktree_inventory.py). */
globalThis.WorktreesPage = (() => {
  const GROUPS = [
    ["today", "오늘"],
    ["week", "1주일"],
    ["month", "한 달"],
    ["older", "그 외"],
  ];
  const STATE_LABEL = {
    active: "세션 연결됨",
    orphaned: "orphaned",
    unlinked: "세션 기록 없음",
  };
  const STATE_TITLE = {
    active: "archived가 아닌 세션(running·paused·killed)이 이 워크트리를 쓰고 있다",
    orphaned: "이 워크트리를 쓴 세션이 모두 archived다",
    unlinked: "이 워크트리를 디렉터리로 가진 세션 기록이 없다",
  };

  /* Which of the four groups a creation time falls in, in the viewer's own
     calendar: today is the local date, then the last 7 and 30 days. */
  function bucketOf(iso, now = new Date()) {
    const t = new Date(iso || "");
    if (!Number.isFinite(t.getTime())) return "older";
    const startOfDay = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    if (t >= startOfDay) return "today";
    const day = 86400000;
    if (t >= new Date(startOfDay.getTime() - 6 * day)) return "week";
    if (t >= new Date(startOfDay.getTime() - 29 * day)) return "month";
    return "older";
  }

  function mergeLabel(w) {
    if (!w.trunk) return ["unknown", "trunk 없음", "master/main 브랜치가 없다"];
    if (w.is_trunk) return ["trunk", w.trunk, "트렁크 자체가 체크아웃되어 있다"];
    if (w.merged === true) {
      return ["merged", `merged → ${w.trunk}`,
        `${w.trunk}에 없는 커밋이 없다` + (w.behind != null ? ` (behind ${w.behind})` : "")];
    }
    if (w.merged === false) {
      return ["unmerged",
        w.ahead != null ? `미머지 · ${w.ahead} ahead` : "미머지",
        `${w.trunk}에 없는 커밋이 있다`];
    }
    return ["unknown", "머지 여부 미상", "git이 판정하지 못했다"];
  }

  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  let data = null, error = "", loading = false, busy = false;
  let stateFilter = "all", query = "", notice = "";
  let sequence = 0;

  function view() { return document.getElementById("worktrees-view"); }

  async function load() {
    const seq = ++sequence;
    loading = true; render();
    try {
      const response = await api("/api/worktrees");
      const body = await response.json();
      if (seq !== sequence) return;
      if (!response.ok) throw Error(body.error || `HTTP ${response.status}`);
      data = body; error = "";
    } catch (exc) {
      if (seq !== sequence) return;
      error = String(exc.message || exc);
    } finally {
      if (seq === sequence) { loading = false; render(); }
    }
  }

  function items() {
    if (!data) return [];
    const multi = (data.repos || []).length > 1;
    const out = [];
    for (const repo of data.repos || []) {
      for (const w of repo.worktrees || []) out.push({ ...w, root: repo.root, multi });
    }
    return out;
  }

  function visible(all) {
    const q = query.trim().toLowerCase();
    return all.filter(w => (stateFilter === "all" || w.state === stateFilter) &&
      (!q || [w.name, w.branch, w.path, ...(w.sessions || []).map(s => s.name)]
        .some(v => String(v || "").toLowerCase().includes(q))));
  }

  /* One confirmation for one checkout or a batch. Resolves to
     {archive, force} or null when cancelled. */
  function confirmRemove(targets, { title, forceOffered = false } = {}) {
    return new Promise(resolve => {
      const live = targets.filter(w => w.state === "active");
      const running = live.flatMap(w => (w.sessions || [])
        .filter(s => s.category === "running").map(s => s.name));
      const dialog = node("dialog", "wt-dialog");
      dialog.appendChild(node("h3", null, title || "워크트리 삭제"));
      const list = node("ul", "wt-dialog-list");
      for (const w of targets.slice(0, 12)) {
        const li = node("li", null, `${w.name}${w.branch ? ` (${w.branch})` : ""}`);
        if (w.state === "active") li.appendChild(node("span", "wt-chip active", "세션 연결됨"));
        list.appendChild(li);
      }
      if (targets.length > 12) list.appendChild(node("li", "wt-muted", `외 ${targets.length - 12}개`));
      dialog.appendChild(list);
      dialog.appendChild(node("p", "wt-muted",
        "워크트리 디렉터리를 지운다. 브랜치는 남는다. 안의 링크(junction·symlink)는 대상을 따라가지 않고 링크만 끊는다."));
      let archive = null;
      if (live.length) {
        const label = node("label", "wt-check");
        archive = node("input"); archive.type = "checkbox";
        label.append(archive, document.createTextNode(
          ` 연결된 세션을 archive하고 함께 삭제한다 (${live.length}개 워크트리` +
          (running.length ? `, 실행 중인 세션 ${running.length}개를 종료함: ${running.slice(0, 6).join(", ")}` : "") + ")"));
        dialog.appendChild(label);
        dialog.appendChild(node("p", "wt-muted",
          "체크하지 않으면 archived가 아닌 세션이 연결된 워크트리는 건너뛴다."));
      }
      const forceLabel = node("label", "wt-check");
      const force = node("input"); force.type = "checkbox"; force.checked = forceOffered;
      forceLabel.append(force, document.createTextNode(
        " 커밋하지 않은 변경·추적되지 않은 파일이 있어도 지운다 (--force)"));
      dialog.appendChild(forceLabel);
      const actions = node("div", "wt-dialog-actions");
      const cancel = node("button", "wf-btn", "취소"); cancel.type = "button";
      const ok = node("button", "wf-btn wt-danger", "삭제"); ok.type = "button";
      actions.append(cancel, ok);
      dialog.appendChild(actions);
      const finish = value => { dialog.close(); dialog.remove(); resolve(value); };
      cancel.onclick = () => finish(null);
      dialog.addEventListener("cancel", event => { event.preventDefault(); finish(null); });
      ok.onclick = () => finish({ archive: !!(archive && archive.checked), force: force.checked });
      document.body.appendChild(dialog);
      dialog.showModal();
      ok.focus();
    });
  }

  async function removeTargets(targets, title) {
    if (busy || !targets.length) return;
    const answer = await confirmRemove(targets, { title });
    if (!answer) return;
    const paths = targets
      .filter(w => answer.archive || w.state !== "active")
      .map(w => w.path);
    const skipped = targets.length - paths.length;
    if (!paths.length) {
      notice = "삭제할 수 있는 워크트리가 없다 — 모두 archived가 아닌 세션이 연결되어 있다.";
      render(); return;
    }
    busy = true; notice = `${paths.length}개 삭제 중…`; render();
    try {
      const response = await api("/api/worktrees/remove", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths, archive: answer.archive, force: answer.force }),
      });
      const body = await response.json();
      if (!response.ok) throw Error(body.error || `HTTP ${response.status}`);
      const parts = [`삭제 ${body.removed.length}개`];
      if (body.archived.length) parts.push(`archive ${body.archived.length}개 세션`);
      if (skipped) parts.push(`건너뜀 ${skipped}개(연결된 세션)`);
      if (body.failed.length) {
        parts.push(`실패 ${body.failed.length}개: ` + body.failed.slice(0, 4)
          .map(f => `${f.path.split(/[\\/]/).pop()} — ${f.error}`).join("; "));
      }
      notice = parts.join(" · ");
    } catch (exc) {
      notice = `삭제 실패: ${exc.message || exc}`;
    } finally {
      busy = false;
      await load();
    }
  }

  function sessionRow(s) {
    const a = node("a", `wt-session ${s.category}`);
    a.href = `#/s/${encodeURIComponent(s.name)}`;
    a.appendChild(node("span", "wt-session-name", s.name));
    a.appendChild(node("span", `wt-chip ${s.category}`, s.category));
    a.title = `${s.name} · ${s.category}${s.status ? ` · ${s.status}` : ""}`;
    return a;
  }

  function card(w) {
    const box = node("div", `wt-card ${w.state}`);
    const top = node("div", "wt-card-top");
    const name = node("span", "wt-name", w.name);
    name.title = w.path;
    top.appendChild(name);
    const state = node("span", `wt-chip ${w.state}`, STATE_LABEL[w.state] || w.state);
    state.title = STATE_TITLE[w.state] || "";
    top.appendChild(state);
    box.appendChild(top);
    const branchLine = node("div", "wt-line");
    branchLine.appendChild(node("span", "wt-branch",
      w.branch || `detached @ ${w.head || "?"}`));
    const [cls, text, title] = mergeLabel(w);
    const merged = node("span", `wt-chip merge-${cls}`, text);
    merged.title = title;
    branchLine.appendChild(merged);
    box.appendChild(branchLine);
    const meta = node("div", "wt-line wt-muted");
    const when = w.created_at ? new Date(w.created_at) : null;
    meta.appendChild(node("span", null, when && Number.isFinite(when.getTime())
      ? when.toLocaleString() : "생성 시각 미상"));
    if (w.multi) meta.appendChild(node("span", "wt-repo", w.root));
    if (w.missing) meta.appendChild(node("span", "wt-chip unknown", "디렉터리 없음"));
    if (w.locked) meta.appendChild(node("span", "wt-chip unknown", "locked"));
    box.appendChild(meta);
    const sessions = node("div", "wt-sessions");
    if ((w.sessions || []).length) for (const s of w.sessions) sessions.appendChild(sessionRow(s));
    else sessions.appendChild(node("span", "wt-muted", "연결된 세션 없음"));
    box.appendChild(sessions);
    const actions = node("div", "wt-actions");
    const del = node("button", "wf-btn wt-danger",
      w.state === "active" ? "archive + 삭제" : "삭제");
    del.type = "button";
    del.disabled = busy;
    del.title = w.state === "active"
      ? "연결된 세션을 archive(실행 중이면 종료)하고 워크트리를 삭제한다"
      : "워크트리를 삭제한다. 브랜치는 남는다";
    del.onclick = () => removeTargets([w], `워크트리 삭제: ${w.name}`);
    actions.appendChild(del);
    box.appendChild(actions);
    return box;
  }

  function render() {
    const root = view();
    if (!root || root.classList.contains("hidden")) return;
    const top = root.scrollTop;
    root.innerHTML = "";
    const head = node("div", "wf-head");
    head.appendChild(node("h2", null, "Worktrees"));
    const refresh = node("button", "wf-btn", loading ? "읽는 중…" : "새로 고침");
    refresh.type = "button"; refresh.disabled = loading || busy;
    refresh.onclick = () => load();
    head.appendChild(refresh);
    root.appendChild(head);

    const bar = node("div", "wt-toolbar");
    const filter = node("input", "wt-filter");
    filter.type = "search"; filter.placeholder = "이름·브랜치·세션 필터";
    filter.value = query;
    filter.oninput = () => { query = filter.value; renderBody(); };
    bar.appendChild(filter);
    for (const [value, label] of [["all", "전체"], ["active", "세션 연결됨"],
      ["orphaned", "orphaned"], ["unlinked", "세션 기록 없음"]]) {
      const b = node("button", `wf-btn wt-filter-btn${stateFilter === value ? " on" : ""}`, label);
      b.type = "button";
      b.onclick = () => { stateFilter = value; render(); };
      bar.appendChild(b);
    }
    root.appendChild(bar);
    if (notice) root.appendChild(node("p", "wt-notice", notice));
    if (error) root.appendChild(node("p", "wt-error", `읽기 실패: ${error}`));
    for (const e of (data && data.errors) || []) {
      root.appendChild(node("p", "wt-error", `${e.root}: ${e.error}`));
    }
    const body = node("div", "wt-body");
    root.appendChild(body);
    renderBody();
    root.scrollTop = top;
  }

  function renderBody() {
    const root = view();
    const body = root && root.querySelector(".wt-body");
    if (!body) return;
    body.innerHTML = "";
    if (!data) {
      body.appendChild(node("p", "wt-muted", loading ? "읽는 중…" : ""));
      return;
    }
    const all = visible(items());
    if (!all.length) {
      body.appendChild(node("p", "wt-muted", "표시할 워크트리가 없다."));
      return;
    }
    const now = new Date();
    for (const [key, label] of GROUPS) {
      const group = all.filter(w => bucketOf(w.created_at, now) === key);
      const section = node("section", `wt-group ${key}`);
      const gh = node("div", "wt-group-head");
      gh.appendChild(node("h3", null, `${label} · ${group.length}`));
      const removable = group.filter(w => w.state !== "active").length;
      const live = group.length - removable;
      if (group.length) {
        const summary = node("span", "wt-muted",
          `삭제 가능 ${removable}` + (live ? ` · 세션 연결 ${live}` : ""));
        gh.appendChild(summary);
        const batch = node("button", "wf-btn wt-danger", `${label} 그룹 삭제`);
        batch.type = "button";
        batch.disabled = busy;
        batch.onclick = () => removeTargets(group, `${label} 그룹 삭제 (${group.length}개)`);
        gh.appendChild(batch);
      }
      section.appendChild(gh);
      const grid = node("div", "wt-grid");
      if (!group.length) grid.appendChild(node("p", "wt-muted", "없음"));
      for (const w of group) grid.appendChild(card(w));
      section.appendChild(grid);
      body.appendChild(section);
    }
  }

  function open() {
    notice = "";
    render();
    load();
  }

  function stop() { sequence++; loading = false; }

  return { open, stop, bucketOf, mergeLabel };
})();
