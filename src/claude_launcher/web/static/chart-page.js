/* The Chart page (#/chart): every session on one time axis -- a bar from its
   creation to its exit (or to now while it runs), its state, how long it has
   lived and where its cflow run stands. Narrowed like the rail: by project,
   and by one mesh or workspace group.

   It owns no poll of its own for the live fleet. The records come from the
   caches app.js already keeps current (sessionsCache, the v1 runs behind
   sessCflowRun, cflow2RailCache, the mesh index behind sessMeshes); the page
   only redraws from them on a timer while it is open. Archived records are
   the one set the rail does not poll, so the page asks for them itself when
   the viewer turns them on. The model (buildModel and the helpers before
   it) takes plain data so tests/web/chartpage_check.js can run it bare. */
globalThis.ChartPage = (() => {
  const RANGES = [
    ["24h", "24시간", 86400000],
    ["7d", "7일", 7 * 86400000],
    ["30d", "30일", 30 * 86400000],
    ["all", "전체", Infinity],
  ];
  const CATEGORIES = ["running", "hold", "paused", "killed", "archived"];
  const CATEGORY_LABEL = {
    running: "running", hold: "hold", paused: "paused",
    killed: "killed", archived: "archived",
  };
  const GROUPS = [["", "Off"], ["mesh", "Mesh"], ["workspace", "Workspace"]];
  const REDRAW_MS = 5000;
  const STORE_KEY = "claunch_chart_view";

  /* The rail's five categories (app.js sessionCategory), repeated here so the
     model runs without app.js. */
  function category(s) {
    if (s && s.archived_at) return "archived";
    if (s && s.status === "exited") {
      if (s.held_at) return "hold";
      return s.paused_at ? "paused" : "killed";
    }
    return "running";
  }

  /* app.js recordProject: a record that never named a project is "default". */
  function projectOf(s) {
    return String((s && s.project) || "").trim() || "default";
  }

  function ms(iso) {
    const t = Date.parse(iso || "");
    return Number.isFinite(t) ? t : null;
  }

  /* When the session started and stopped living, in epoch ms. A running or
     held session is alive up to `now`: a hold is an exited record whose
     program the daemon freed while nobody used it, and opening it wakes it,
     so nobody ended it. An exited one ends at its exit; a record archived
     without an exit time ends at its archiving, and one with neither at its
     last activity. null when there is no creation time to start the bar
     from. */
  function spanOf(s, now) {
    const start = ms(s && s.created_at);
    if (start === null) return null;
    const alive = !!s && !s.archived_at && (s.status !== "exited" || !!s.held_at);
    let end = alive ? now
      : ms(s.exited_at) ?? ms(s.archived_at) ?? ms(s.last_activity_at) ?? start;
    if (end < start) end = start;
    return { start, end, alive };
  }

  /* A lifetime in the two largest units that say something. */
  function formatDuration(d) {
    if (!Number.isFinite(d) || d < 0) return "—";
    const m = Math.floor(d / 60000);
    if (m < 1) return "<1m";
    const h = Math.floor(m / 60), day = Math.floor(h / 24);
    if (day) return h % 24 ? `${day}d ${h % 24}h` : `${day}d`;
    if (h) return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
    return `${m}m`;
  }

  function median(values) {
    if (!values.length) return null;
    const v = values.slice().sort((a, b) => a - b);
    const mid = Math.floor(v.length / 2);
    return v.length % 2 ? v[mid] : (v[mid - 1] + v[mid]) / 2;
  }

  /* Evenly spaced axis labels across [t0, t1]. */
  function ticks(t0, t1, count = 6) {
    if (!(t1 > t0)) return [];
    const out = [];
    for (let i = 0; i <= count; i++) out.push(t0 + (t1 - t0) * i / count);
    return out;
  }

  function formatTick(t, span) {
    const d = new Date(t);
    const pad = (n) => String(n).padStart(2, "0");
    const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    if (span <= 86400000) return hm;
    const md = `${d.getMonth() + 1}/${d.getDate()}`;
    return span <= 3 * 86400000 ? `${md} ${hm}` : md;
  }

  function formatTime(t) {
    if (t === null || t === undefined) return "—";
    const d = new Date(t);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
      `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  /* The rows the page draws, from plain records.

     opts: now (ms), project ("" = all), group ("" | "mesh" | "workspace"),
     groupValue(group, s) -> the group a record is filed under, groupPick
     ("" = every group), range (a RANGES key), archived (include archived
     records), sort ("created" | "lifetime"), cflowOf(name) -> {state,
     label, version} or null.

     A record is kept when its bar overlaps the range window. The axis runs
     from the window's start (or, for "all", the earliest kept creation) to
     now. Groups are listed with their counts before the group pick narrows
     the rows, so the picker can offer every group of the project. */
  function buildModel(sessions, opts = {}) {
    const now = opts.now ?? Date.now();
    const range = RANGES.find((r) => r[0] === opts.range) || RANGES[1];
    const from = Number.isFinite(range[2]) ? now - range[2] : -Infinity;
    const group = opts.group || "";
    const groupValue = opts.groupValue || (() => "");
    const cflowOf = opts.cflowOf || (() => null);
    const base = [];
    for (const s of sessions || []) {
      if (!s || !s.name) continue;
      if (opts.project && projectOf(s) !== opts.project) continue;
      const cat = category(s);
      if (cat === "archived" && !opts.archived) continue;
      const span = spanOf(s, now);
      if (!span || span.end < from) continue;
      base.push({
        name: s.name, session: s, category: cat, span,
        lifetime: span.end - span.start,
        group: group ? groupValue(group, s) : "",
        cflow: cflowOf(s.name),
      });
    }
    const groupCounts = new Map();
    for (const r of base) groupCounts.set(r.group, (groupCounts.get(r.group) || 0) + 1);
    const groups = group
      ? [...groupCounts.entries()].sort((a, b) => a[0].localeCompare(b[0]))
          .map(([value, count]) => ({ value, count }))
      : [];
    const pick = group && opts.groupPick && groupCounts.has(opts.groupPick)
      ? opts.groupPick : "";
    const rows = pick ? base.filter((r) => r.group === pick) : base;
    rows.sort(opts.sort === "lifetime"
      ? (a, b) => b.lifetime - a.lifetime || a.name.localeCompare(b.name)
      : (a, b) => b.span.start - a.span.start || a.name.localeCompare(b.name));
    if (group && !pick) {
      // Stable: within a group the chosen order holds.
      rows.sort((a, b) => a.group.localeCompare(b.group));
    }
    let t0 = from;
    if (!Number.isFinite(t0)) {
      t0 = rows.length ? Math.min(...rows.map((r) => r.span.start)) : now - 3600000;
    }
    if (!(now > t0)) t0 = now - 3600000;
    const counts = Object.fromEntries(CATEGORIES.map((c) => [c, 0]));
    const flow = { running: 0, yours: 0, other: 0, none: 0 };
    for (const r of rows) {
      counts[r.category]++;
      if (!r.cflow) flow.none++;
      else if (r.cflow.state === "yours") flow.yours++;
      else if (r.cflow.state === "running" || r.cflow.state === "held" ||
               r.cflow.state === "peer") flow.running++;
      else flow.other++;
    }
    return {
      now, t0, t1: now, rows, groups, groupPick: pick,
      summary: {
        total: rows.length, counts, flow,
        medianLifetime: median(rows.map((r) => r.lifetime)),
        alive: rows.filter((r) => r.span.alive).length,
      },
    };
  }

  /* The bar's place on the axis, in percent of the track. A bar that started
     before the window is clipped at its left edge; one too short to see keeps
     a sliver so the row is never blank. */
  function barGeometry(span, t0, t1) {
    const width = t1 - t0;
    if (!(width > 0)) return { left: 0, width: 0, clipped: false };
    const s = Math.max(span.start, t0), e = Math.min(span.end, t1);
    const left = ((s - t0) / width) * 100;
    const w = Math.max(((e - s) / width) * 100, 0.25);
    return { left: Math.min(left, 99.75), width: Math.min(w, 100 - Math.min(left, 99.75)),
             clipped: span.start < t0 };
  }

  // ---- the page -------------------------------------------------------

  let timer = null;
  let archivedRecords = [];
  let archivedLoading = false;
  let archivedError = "";
  const state = (() => {
    const fallback = { group: "", groupPick: "", range: "7d",
                       archived: false, sort: "created" };
    try {
      const saved = JSON.parse(localStorage.getItem(STORE_KEY) || "null");
      return Object.assign(fallback, saved && typeof saved === "object" ? saved : {});
    } catch { return fallback; }
  })();

  function remember() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch {}
  }

  function view() { return typeof document !== "undefined" && document.getElementById("chart-view"); }

  function node(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }

  /* app.js globals, read at call time; each guarded so a missing one costs
     its feature and not the page. */
  function liveSessions() {
    const live = typeof sessionsCache !== "undefined" ? sessionsCache || [] : [];
    if (!state.archived) return live;
    const seen = new Set(live.map((s) => s.name));
    return live.concat(archivedRecords.filter((s) => !seen.has(s.name)));
  }

  function appGroupValue(group, s) {
    if (typeof sessionGroupValue === "function") return sessionGroupValue(group, s);
    return "";
  }

  function groupLabel(group, value) {
    if (group === "workspace" && typeof sessionWorkspaceLabel === "function")
      return sessionWorkspaceLabel(value);
    return value;
  }

  /* The session's cflow run, v2 first (its own rail line), then v1. */
  function appCflowOf(name) {
    const v2 = typeof cflow2RailCache !== "undefined" ? (cflow2RailCache || {})[name] : null;
    if (v2 && typeof cflow2RailMarkState === "function") {
      return { version: "v2", state: cflow2RailMarkState(v2),
               label: `${v2.workflow || "cflow2"} · ${cflow2RailLabel(v2)}` };
    }
    const v1 = typeof sessCflowRun === "function" ? sessCflowRun(name) : null;
    if (v1 && typeof wfMarkState === "function") {
      return { version: "v1", state: wfMarkState(v1.status, v1),
               label: `${v1.workflow || "cflow"} · ${sessCflowLabel(v1)}` };
    }
    return null;
  }

  /* The project is the page's one shared pick (app.js currentProject, the
     tabs above the rail and `?project=` in the address): choosing one here
     moves the rail with it, and a tab pressed over the rail redraws this
     page. Without app.js the page keeps a pick of its own. */
  let ownProject = "";
  function currentProjectPick() {
    return typeof currentProject === "string" ? currentProject : ownProject;
  }

  function pickProject(name) {
    state.groupPick = "";
    remember();
    if (typeof setCurrentProject === "function") setCurrentProject(name);
    else ownProject = name;
    render(true);
  }

  function projectNames(sessions) {
    const names = new Set(sessions.map(projectOf));
    if (typeof projectsCache !== "undefined") {
      for (const p of projectsCache || []) if (p && p.name) names.add(p.name);
    }
    return [...names].sort((a, b) => a.localeCompare(b));
  }

  async function loadArchived() {
    if (archivedLoading || typeof api !== "function") return;
    archivedLoading = true; archivedError = "";
    render();
    try {
      const resp = await api("/api/sessions?view=rail&state=archived");
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      archivedRecords = (await resp.json()).sessions || [];
    } catch (e) {
      archivedError = String((e && e.message) || e);
    } finally {
      archivedLoading = false;
      render();
    }
  }

  function seg(label, options, value, onPick) {
    const box = node("div", "chart-seg");
    box.setAttribute("role", "radiogroup");
    box.setAttribute("aria-label", label);
    box.appendChild(node("span", "chart-seg-label", label));
    for (const [key, text] of options) {
      const b = node("button", `chart-seg-btn${key === value ? " on" : ""}`, text);
      b.type = "button";
      b.setAttribute("role", "radio");
      b.setAttribute("aria-checked", String(key === value));
      b.onclick = () => onPick(key);
      box.appendChild(b);
    }
    return box;
  }

  function select(label, options, value, onPick) {
    const wrap = node("label", "chart-select");
    wrap.appendChild(node("span", "chart-seg-label", label));
    const sel = node("select");
    for (const [key, text] of options) {
      const o = node("option", null, text);
      o.value = key;
      if (key === value) o.selected = true;
      sel.appendChild(o);
    }
    sel.onchange = () => onPick(sel.value);
    wrap.appendChild(sel);
    return wrap;
  }

  function set(patch) {
    Object.assign(state, patch);
    remember();
    if (patch.archived && !archivedRecords.length) loadArchived();
    render(true);
  }

  function toolbar(sessions, model) {
    const bar = node("div", "chart-toolbar");
    const project = currentProjectPick();
    bar.appendChild(select("Project",
      [["", "All projects"], ...projectNames(sessions).map((n) => [n, n])],
      project, pickProject));
    bar.appendChild(seg("Group by", GROUPS, state.group,
      (v) => set({ group: v, groupPick: "" })));
    if (state.group) {
      bar.appendChild(select(state.group === "mesh" ? "Mesh" : "Workspace",
        [["", `전체 (${model.groups.length})`],
         ...model.groups.map((g) => [g.value, `${groupLabel(state.group, g.value)} · ${g.count}`])],
        model.groupPick, (v) => set({ groupPick: v })));
    }
    bar.appendChild(seg("Range", RANGES.map((r) => [r[0], r[1]]), state.range,
      (v) => set({ range: v })));
    bar.appendChild(seg("Sort", [["created", "생성 순"], ["lifetime", "생존 시간 순"]],
      state.sort, (v) => set({ sort: v })));
    const arch = node("label", "chart-check");
    const box = node("input");
    box.type = "checkbox"; box.checked = !!state.archived;
    box.onchange = () => set({ archived: box.checked });
    arch.appendChild(box);
    arch.appendChild(node("span", null,
      archivedLoading ? "archived 포함 (읽는 중…)" : "archived 포함"));
    bar.appendChild(arch);
    return bar;
  }

  function summary(model) {
    const s = model.summary;
    const box = node("div", "chart-summary");
    const stat = (label, value, cls) => {
      const item = node("div", `chart-stat${cls ? " " + cls : ""}`);
      item.appendChild(node("span", "chart-stat-value", String(value)));
      item.appendChild(node("span", "chart-stat-label", label));
      box.appendChild(item);
    };
    stat("세션", s.total);
    for (const c of CATEGORIES) {
      if (c === "archived" && !state.archived) continue;
      stat(CATEGORY_LABEL[c], s.counts[c], `cat-${c}`);
    }
    stat("생존 시간 중앙값", s.medianLifetime === null ? "—" : formatDuration(s.medianLifetime));
    stat("cflow 진행", s.flow.running, "flow-running");
    stat("사람 대기", s.flow.yours, "flow-yours");
    stat("cflow 없음", s.flow.none);
    return box;
  }

  function axis(model) {
    const row = node("div", "chart-row chart-axis");
    row.appendChild(node("div", "chart-name", "session"));
    const track = node("div", "chart-track");
    const span = model.t1 - model.t0;
    for (const t of ticks(model.t0, model.t1)) {
      const tick = node("span", "chart-tick", formatTick(t, span));
      tick.style.left = `${((t - model.t0) / span) * 100}%`;
      track.appendChild(tick);
    }
    row.appendChild(track);
    row.appendChild(node("div", "chart-life", "생존"));
    row.appendChild(node("div", "chart-flow", "cflow"));
    return row;
  }

  function sessionRow(r, model) {
    const row = node("div", `chart-row cat-${r.category}`);
    const name = node("a", "chart-name");
    name.href = "#/s/" + encodeURIComponent(r.name);
    name.title = r.name;
    name.appendChild(node("span", `chart-dot cat-${r.category}`));
    name.appendChild(node("span", "chart-name-text", r.name));
    const role = r.session.role;
    if (role) name.appendChild(node("span", "chart-role", role));
    row.appendChild(name);

    const track = node("div", "chart-track");
    const geo = barGeometry(r.span, model.t0, model.t1);
    const bar = node("span", `chart-bar cat-${r.category}${geo.clipped ? " clipped" : ""}` +
      (r.span.alive ? " alive" : ""));
    bar.style.left = `${geo.left}%`;
    bar.style.width = `${geo.width}%`;
    bar.title = [
      r.name,
      `상태: ${CATEGORY_LABEL[r.category]}`,
      `생성: ${formatTime(r.span.start)}`,
      r.span.alive ? "종료: (실행 중)" : `종료: ${formatTime(r.span.end)}`,
      `생존 시간: ${formatDuration(r.lifetime)}`,
      r.cflow ? `cflow ${r.cflow.version}: ${r.cflow.label}` : "cflow: 없음",
    ].join("\n");
    track.appendChild(bar);
    row.appendChild(track);

    row.appendChild(node("div", "chart-life", formatDuration(r.lifetime)));

    const flow = node("div", "chart-flow");
    if (r.cflow) {
      const glyph = typeof WF_GLYPH !== "undefined" ? WF_GLYPH[r.cflow.state] || "" : "";
      flow.appendChild(node("span", `wf-mark wf-mark-${r.cflow.state}`, glyph));
      flow.appendChild(node("span", "chart-flow-ver", r.cflow.version));
      flow.appendChild(node("span", "chart-flow-text", r.cflow.label));
      flow.title = r.cflow.label;
    } else {
      flow.appendChild(node("span", "chart-muted", "—"));
    }
    row.appendChild(flow);
    return row;
  }

  function render(force = false) {
    const root = view();
    if (!root || root.classList.contains("hidden")) return;
    // A redraw replaces the toolbar; skip the timer's while a picker is open
    // in it, or the list would close under the viewer's pointer.
    const focused = typeof document !== "undefined" && document.activeElement;
    if (!force && focused && focused.tagName === "SELECT" && root.contains(focused)) return;
    const top = root.scrollTop;
    const sessions = liveSessions();
    const model = buildModel(sessions, {
      now: Date.now(), project: currentProjectPick(), group: state.group,
      groupValue: appGroupValue, groupPick: state.groupPick, range: state.range,
      archived: state.archived, sort: state.sort, cflowOf: appCflowOf,
    });
    root.innerHTML = "";
    const head = node("div", "wf-head");
    head.appendChild(node("h2", null, "Chart"));
    head.appendChild(node("span", "chart-muted",
      "세션별 생성 시점과 생존 시간, cflow 상태"));
    root.appendChild(head);
    root.appendChild(toolbar(sessions, model));
    if (archivedError) root.appendChild(node("p", "chart-error", `archived 읽기 실패: ${archivedError}`));
    root.appendChild(summary(model));
    const chart = node("div", "chart-grid");
    chart.appendChild(axis(model));
    if (!model.rows.length) {
      chart.appendChild(node("p", "chart-muted chart-empty", "이 조건에 맞는 세션이 없다."));
    }
    let last = null;
    for (const r of model.rows) {
      if (state.group && !model.groupPick && r.group !== last) {
        last = r.group;
        const count = (model.groups.find((g) => g.value === r.group) || {}).count || 0;
        chart.appendChild(node("div", "chart-group-head",
          `${groupLabel(state.group, r.group)} · ${count}`));
      }
      chart.appendChild(sessionRow(r, model));
    }
    root.appendChild(chart);
    root.scrollTop = top;
  }

  function open() {
    if (state.archived && !archivedRecords.length) loadArchived();
    render();
    if (timer) clearInterval(timer);
    timer = setInterval(() => render(), REDRAW_MS);
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
  }

  return { open, stop, render, category, projectOf, spanOf, formatDuration,
           median, ticks, buildModel, barGeometry, RANGES };
})();
