/* The Chart page (#/chart): every session on one time axis -- a bar from its
   creation to its exit (or to now while it runs), its state, how long it has
   lived and where its cflow run stands. Narrowed like the rail: by project,
   and by one mesh or workspace group.

   Two columns split by a draggable divider: the timeline on the left and,
   on the right, the detail of the row the viewer clicked (that row stays
   highlighted). The divider's place and the picked session are kept with
   the rest of the view in localStorage.

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
  // The timeline column's share of the split, in percent.
  const SPLIT_DEFAULT = 64, SPLIT_MIN = 30, SPLIT_MAX = 82;
  /* The longest span one screen width draws. A fleet whose oldest session
     dates back weeks would otherwise squeeze a session made ten minutes ago
     into a sliver: past this the track grows and the grid scrolls, so the
     axis stays proportional to time instead of distorting to fit. */
  const AXIS_CAP_MS = 7 * 86400000;
  /* The unit of the log axis: a run of a few minutes still gets a visible
     bar next to one that has lived for weeks. */
  const LOG_UNIT_MS = 3600000;
  /* Ages the log axis labels, so its ticks read as "1시간 전" rather than as
     clock times whose spacing the scale does not honour. */
  const LOG_TICKS_MS = [
    60000, 300000, 900000, 1800000, 3600000, 7200000, 21600000, 43200000,
    86400000, 2 * 86400000, 7 * 86400000, 14 * 86400000, 30 * 86400000,
    90 * 86400000,
  ];
  /* How wide an axis label renders, per character, and the least clear space
     between two of them. The labels are drawn at 11px, so roughly 6px a
     character; measured, "1d 전" is 28.5px and "12h 전" 35.5px. Estimates
     stand in for measuring while the ticks are being chosen, and the laid-out
     labels are then measured for what the estimate cannot promise. */
  const TICK_CHAR_PX = 6.2;
  const TICK_GAP_PX = 6;

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

  /* The role a row names, as the rail spells it (app.js renderRow): the
     session's own field first, then the role it holds in the mesh the rows
     are grouped under, then its first membership, else "free-role".
     memberships are sessMeshes(name) entries: {mesh, handle, label}. */
  function roleOf(s, memberships, meshGroup) {
    if (s && s.role) return String(s.role);
    const ms = memberships || [];
    const under = meshGroup ? ms.find((m) => m.mesh === meshGroup) : null;
    if (under && under.label) return under.label;
    const first = ms.find((m) => m.label);
    return first ? first.label : "free-role";
  }

  function clampSplit(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return SPLIT_DEFAULT;
    return Math.min(SPLIT_MAX, Math.max(SPLIT_MIN, n));
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

  /* A label sized to the space between ticks (not to the whole axis): a
     widened grid puts them hours apart, and "10/20, 10/20, 10/20" says
     nothing. */
  function formatTick(t, spacing) {
    const d = new Date(t);
    const pad = (n) => String(n).padStart(2, "0");
    const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    const md = `${d.getMonth() + 1}/${d.getDate()}`;
    if (spacing <= 3600000) return hm;
    return spacing <= 86400000 ? `${md} ${hm}` : md;
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
     label, version} or null, meshesOf(name) -> the session's mesh
     memberships (for its role).

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
    const meshesOf = opts.meshesOf || (() => []);
    const base = [];
    for (const s of sessions || []) {
      if (!s || !s.name) continue;
      if (opts.project && projectOf(s) !== opts.project) continue;
      const cat = category(s);
      if (cat === "archived" && !opts.archived) continue;
      const span = spanOf(s, now);
      if (!span || span.end < from) continue;
      const g = group ? groupValue(group, s) : "";
      const meshes = meshesOf(s.name) || [];
      base.push({
        name: s.name, session: s, category: cat, span,
        lifetime: span.end - span.start,
        group: g, meshes,
        role: roleOf(s, meshes, group === "mesh" ? g : ""),
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
    const flow = { running: 0, yours: 0, other: 0, none: 0, stalled: 0 };
    for (const r of rows) {
      counts[r.category]++;
      if (!r.cflow) flow.none++;
      else if (r.cflow.state === "yours") flow.yours++;
      else if (r.cflow.state === "running" || r.cflow.state === "held" ||
               r.cflow.state === "peer") flow.running++;
      else flow.other++;
      if (cflowAge(r.cflow, now).stalled) flow.stalled++;
    }
    /* What one screen width holds, per scale (claunch-c2emv). Both scales
       place every time against the same window t0..t1 -- that is what the
       range picker means, and it is what keeps the bars and the ticks in one
       coordinate system:
       - "log" compresses the distance from now, so a session made minutes
         ago and one a month old are both visible in one screen;
       - "linear" keeps distances proportional and draws at most AXIS_CAP_MS
         per screen: the grid is widened by span/CAP and the viewer scrolls
         left for the older sessions, which stay at their real positions. */
    const scale = opts.scale === "linear" ? "linear" : "log";
    const axisStart = t0;
    const axisWidthPct = scale === "log" ? 100
      : Math.max(100, ((now - t0) / AXIS_CAP_MS) * 100);
    return {
      now, t0, t1: now, axisStart, axisWidthPct, scale, rows, groups, groupPick: pick,
      summary: {
        total: rows.length, counts, flow,
        medianLifetime: median(rows.map((r) => r.lifetime)),
        alive: rows.filter((r) => r.span.alive).length,
      },
    };
  }

  /* Where a time sits across the track, in percent, for the model's scale.
     0 is the axis start (the left edge), 100 is now. */
  function positionOf(t, model) {
    const wide = model.t1 - model.axisStart;
    if (!(wide > 0)) return 0;
    if (model.scale !== "log") return ((t - model.axisStart) / wide) * 100;
    const den = Math.log1p(wide / LOG_UNIT_MS);
    if (!(den > 0)) return 0;
    const age = Math.max(0, model.t1 - t);
    return (1 - Math.log1p(age / LOG_UNIT_MS) / den) * 100;
  }

  /* The bar's place on the axis. Its two ends are placed independently, so
     the same rule serves both scales. A bar that started before the axis is
     clipped at its left edge; one too short to see keeps a sliver so the row
     is never blank. */
  function barGeometry(span, model) {
    const left = Math.max(0, Math.min(positionOf(span.start, model), 99.75));
    const right = Math.max(left, positionOf(span.end, model));
    const width = Math.min(Math.max(right - left, 0.25), 100 - left);
    return { left, width, clipped: span.start < model.axisStart };
  }

  // ---- the page -------------------------------------------------------

  let timer = null;
  let onResize = null;
  let dragging = false;
  let archivedRecords = [];
  let archivedLoading = false;
  let archivedError = "";
  const state = (() => {
    const fallback = { group: "", groupPick: "", range: "7d",
                       archived: false, sort: "created",
                       split: SPLIT_DEFAULT, selected: "", scale: "log" };
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

  /* When the run last moved, in epoch ms: the state file's `updated` (every
     report, choice and approval writes it), or the oldest active task's
     `entered_at` for a run whose state carries no `updated`. null when the
     row has neither, and the page then shows the status without an age. */
  function cflowChangedAt(r) {
    const updated = ms(r && r.updated);
    if (updated !== null) return updated;
    const entered = r && r.entered_at;
    return typeof entered === "number" && Number.isFinite(entered)
      ? entered * 1000 : null;
  }

  /* The session's cflow run, v2 first (its own rail line), then v1. `changedAt`
     is what the chart's stall mark reads; a v1 run has no such clock here. */
  function appCflowOf(name) {
    const v2 = typeof cflow2RailCache !== "undefined" ? (cflow2RailCache || {})[name] : null;
    if (v2 && typeof cflow2RailMarkState === "function") {
      return { version: "v2", state: cflow2RailMarkState(v2),
               label: `${v2.workflow || "cflow2"} · ${cflow2RailLabel(v2)}`,
               changedAt: cflowChangedAt(v2) };
    }
    const v1 = typeof sessCflowRun === "function" ? sessCflowRun(name) : null;
    if (v1 && typeof wfMarkState === "function") {
      return { version: "v1", state: wfMarkState(v1.status, v1),
               label: `${v1.workflow || "cflow"} · ${sessCflowLabel(v1)}` };
    }
    return null;
  }

  function appMeshesOf(name) {
    return typeof sessMeshes === "function" ? sessMeshes(name) || [] : [];
  }

  /* A run in one task this long has stopped moving until something says
     otherwise. Not a verdict: a full sweep is a long task too, so the page
     marks it and the reader judges. */
  const STALL_MS = 30 * 60000;

  /* The age to show beside a row's cflow line, and whether it is old enough
     to mark. A run a person owes (`yours`) is not stalled -- it is waiting
     on the reader, and the summary counts those apart. */
  function cflowAge(cflow, now) {
    if (!cflow || !Number.isFinite(cflow.changedAt)) return { elapsed: null, stalled: false };
    const elapsed = Math.max(0, now - cflow.changedAt);
    return { elapsed, stalled: cflow.state === "running" && elapsed >= STALL_MS };
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
    bar.appendChild(seg("Scale", [["log", "로그"], ["linear", "선형"]], state.scale,
      (v) => set({ scale: v })));
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
    stat("정지 의심", s.flow.stalled, "flow-stall");
    stat("cflow 없음", s.flow.none);
    return box;
  }

  /* The label a log tick carries: the age it stands for, not a clock time. */
  function logTickText(model, at) {
    return `${formatDuration(model.t1 - at)} 전`;
  }

  /* The instants the axis labels. Linear spaces them evenly (more of them on
     a widened grid); the log axis labels round ages, because the spacing it
     draws them at is not the spacing of clock time.

     `opts.trackPx` is the timeline column's width when it is known. Two labels
     sit on top of each other once they are narrower apart than their own
     widths, so the log axis drops a tick whose label would touch the one kept
     to its left -- a percentage of the axis cannot answer that, because the
     column is only part of the page (claunch-c2emv). */
  function axisTicks(model, opts = {}) {
    if (model.scale === "log") {
      const span = model.t1 - model.axisStart;
      const ages = LOG_TICKS_MS.filter((a) => a < span);
      const step = Math.max(1, Math.ceil(ages.length / 10));
      const trackPx = opts.trackPx;
      const out = [];
      let last = -Infinity;
      // Oldest first, so the positions ascend and "too close" compares
      // against the label already placed to the left.
      for (let i = ages.length - 1; i >= 0; i -= step) {
        const at = model.t1 - ages[i];
        const pos = positionOf(at, model);
        if (trackPx > 0) {
          const px = (pos / 100) * trackPx;
          const half = (logTickText(model, at).length * TICK_CHAR_PX) / 2;
          if (px - half < last + TICK_GAP_PX) continue;
          out.push(at);
          last = px + half;
          continue;
        }
        // Without a width: the old percentage rule, which only keeps apart
        // labels of a track wide enough for it.
        if (pos - last < 8) continue;
        out.push(at);
        last = pos;
      }
      return out;
    }
    const count = Math.min(24, Math.max(6, Math.round(6 * model.axisWidthPct / 100)));
    return ticks(model.axisStart, model.t1, count);
  }

  /* The axis header. Its ticks are placed by layoutTicks once the column has
     been laid out and its width is a number. */
  function axis() {
    const row = node("div", "chart-row chart-axis");
    row.appendChild(node("div", "chart-name", "session"));
    row.appendChild(node("div", "chart-track"));
    row.appendChild(node("div", "chart-life", "생존"));
    row.appendChild(node("div", "chart-flow", "cflow"));
    return row;
  }

  /* Place the axis labels against the column's real width: choose ticks by
     their labels' widths, then measure the labels themselves and hide the ones
     that still touch a neighbour or would be cut by the column's edge. */
  function layoutTicks(main, model) {
    const track = main.querySelector(".chart-axis .chart-track");
    if (!track) return;
    track.textContent = "";
    const marks = axisTicks(model, { trackPx: track.clientWidth });
    const span = model.t1 - model.axisStart;
    const spacing = marks.length > 1 ? span / (marks.length - 1) : span;
    const built = [];
    for (const t of marks) {
      const text = model.scale === "log" ? logTickText(model, t) : formatTick(t, spacing);
      const tick = node("span", "chart-tick", text);
      tick.style.left = `${positionOf(t, model)}%`;
      track.appendChild(tick);
      built.push(tick);
    }
    const edge = track.getBoundingClientRect();
    let lastRight = -Infinity;
    for (const tick of built) {
      const box = tick.getBoundingClientRect();
      if (box.left < edge.left - 0.5 || box.right > edge.right + 0.5 ||
          box.left < lastRight + TICK_GAP_PX) {
        tick.style.visibility = "hidden";
        continue;
      }
      lastRight = box.right;
    }
  }

  function pick(name) {
    state.selected = state.selected === name ? "" : name;
    remember();
    render(true);
  }

  function sessionRow(r, model) {
    const on = r.name === state.selected;
    const row = node("div", `chart-row cat-${r.category}${on ? " selected" : ""}`);
    row.dataset.name = r.name;
    row.tabIndex = 0;
    row.setAttribute("role", "option");
    row.setAttribute("aria-selected", String(on));
    row.onclick = () => pick(r.name);
    row.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(r.name); }
    };
    const name = node("div", "chart-name");
    name.title = r.name;
    name.appendChild(node("span", `chart-dot cat-${r.category}`));
    name.appendChild(node("span", "chart-name-text", r.name));
    name.appendChild(node("span", "chart-role", r.role));
    row.appendChild(name);

    const track = node("div", "chart-track");
    const geo = barGeometry(r.span, model);
    const bar = node("span", `chart-bar cat-${r.category}${geo.clipped ? " clipped" : ""}` +
      (r.span.alive ? " alive" : ""));
    bar.style.left = `${geo.left}%`;
    bar.style.width = `${geo.width}%`;
    const change = cflowAge(r.cflow, model.now);
    bar.title = [
      r.name,
      `상태: ${CATEGORY_LABEL[r.category]}`,
      `생성: ${formatTime(r.span.start)}`,
      r.span.alive ? "종료: (실행 중)" : `종료: ${formatTime(r.span.end)}`,
      `생존 시간: ${formatDuration(r.lifetime)}`,
      r.cflow ? `cflow ${r.cflow.version}: ${r.cflow.label}` +
        (change.elapsed !== null ? ` · 마지막 갱신 ${formatDuration(change.elapsed)} 전` : "")
        : "cflow: 없음",
    ].join("\n");
    track.appendChild(bar);
    row.appendChild(track);

    row.appendChild(node("div", "chart-life", formatDuration(r.lifetime)));

    const age = cflowAge(r.cflow, model.now);
    const flow = node("div", `chart-flow${age.stalled ? " stalled" : ""}`);
    if (r.cflow) {
      const glyph = typeof WF_GLYPH !== "undefined" ? WF_GLYPH[r.cflow.state] || "" : "";
      flow.appendChild(node("span", `wf-mark wf-mark-${r.cflow.state}`, glyph));
      flow.appendChild(node("span", "chart-flow-ver", r.cflow.version));
      flow.appendChild(node("span", "chart-flow-text", r.cflow.label));
      if (age.elapsed !== null) {
        flow.appendChild(node("span", "chart-flow-age",
          (age.stalled ? "⚠ " : "") + formatDuration(age.elapsed)));
      }
      flow.title = [
        r.cflow.label,
        age.elapsed !== null
          ? `마지막 갱신: ${formatDuration(age.elapsed)} 전` +
            (age.stalled ? ` (${formatDuration(STALL_MS)} 이상 무변화)` : "")
          : null,
      ].filter(Boolean).join("\n");
    } else {
      flow.appendChild(node("span", "chart-muted", "—"));
    }
    row.appendChild(flow);
    return row;
  }

  /* The right column: everything known about the picked session. The record
     is looked up in every session the page holds, so a pick that the
     current filter hides still shows (with a note saying so). */
  function detail(model, sessions) {
    const box = node("aside", "chart-detail");
    box.setAttribute("aria-label", "session detail");
    const name = state.selected;
    if (!name) {
      box.appendChild(node("p", "chart-muted chart-detail-hint",
        "왼쪽에서 세션 행을 클릭하면 상세 정보가 여기에 표시된다."));
      return box;
    }
    let r = model.rows.find((x) => x.name === name);
    const hidden = !r;
    if (!r) {
      const s = sessions.find((x) => x.name === name);
      if (!s) {
        box.appendChild(node("p", "chart-muted chart-detail-hint",
          `세션 ${name}의 기록이 지금 목록에 없다.`));
        return box;
      }
      const span = spanOf(s, model.now) || { start: null, end: null, alive: false };
      const meshes = appMeshesOf(s.name);
      r = { name: s.name, session: s, category: category(s), span,
            lifetime: span.start === null ? null : span.end - span.start,
            meshes, role: roleOf(s, meshes, ""), cflow: appCflowOf(s.name) };
    }
    const s = r.session;
    const head = node("div", "chart-detail-head");
    head.appendChild(node("span", `chart-dot cat-${r.category}`));
    head.appendChild(node("h3", "chart-detail-name", r.name));
    const close = node("button", "chart-detail-close", "×");
    close.type = "button";
    close.title = "선택 해제";
    close.onclick = () => pick(r.name);
    head.appendChild(close);
    box.appendChild(head);
    if (hidden) box.appendChild(node("p", "chart-muted chart-detail-note",
      "현재 필터 조건에는 이 세션이 포함되지 않는다."));

    const dl = node("dl", "chart-detail-list");
    const item = (label, value) => {
      if (value === undefined || value === null || value === "") return;
      dl.appendChild(node("dt", null, label));
      const dd = node("dd");
      if (value instanceof Node) dd.appendChild(value); else dd.textContent = String(value);
      dl.appendChild(dd);
    };
    item("상태", CATEGORY_LABEL[r.category]);
    item("role", r.role);
    item("생성", r.span.start === null ? "" : formatTime(r.span.start));
    item("종료", r.span.alive ? "(실행 중)" : r.span.end === null ? "" : formatTime(r.span.end));
    item("생존 시간", r.lifetime === null ? "" : formatDuration(r.lifetime));
    item("프로젝트", projectOf(s));
    item("workspace", s.cwd || "");
    item("parent", s.parent && s.parent !== s.name ? s.parent : "");
    item("profile", s.profile || "");
    item("harness", s.harness || "");
    item("model", s.model || "");
    const last = ms(s.last_activity_at);
    item("마지막 활동", last === null ? "" : formatTime(last));
    if (r.meshes && r.meshes.length) {
      const ul = node("ul", "chart-detail-meshes");
      for (const m of r.meshes) {
        ul.appendChild(node("li", null,
          `${m.mesh} · ${m.handle || "?"}${m.label ? ` (${m.label})` : ""}`));
      }
      item("mesh", ul);
    }
    if (r.cflow) {
      const f = node("span", "chart-flow");
      const glyph = typeof WF_GLYPH !== "undefined" ? WF_GLYPH[r.cflow.state] || "" : "";
      f.appendChild(node("span", `wf-mark wf-mark-${r.cflow.state}`, glyph));
      f.appendChild(node("span", "chart-flow-ver", r.cflow.version));
      f.appendChild(node("span", "chart-detail-flow-text", r.cflow.label));
      item("cflow", f);
    } else {
      item("cflow", "없음");
    }
    box.appendChild(dl);
    const link = node("a", "chart-detail-open", "세션 열기 →");
    link.href = "#/s/" + encodeURIComponent(r.name);
    box.appendChild(link);
    return box;
  }

  /* The bar between the columns. Dragging it (or the arrow keys while it has
     focus) moves the split; the new share is stored once the drag ends. */
  function divider(split) {
    const bar = node("div", "chart-divider");
    bar.tabIndex = 0;
    bar.setAttribute("role", "separator");
    bar.setAttribute("aria-orientation", "vertical");
    bar.setAttribute("aria-label", "열 너비 조절");
    bar.setAttribute("aria-valuemin", String(SPLIT_MIN));
    bar.setAttribute("aria-valuemax", String(SPLIT_MAX));
    bar.setAttribute("aria-valuenow", String(Math.round(state.split)));
    const apply = (v) => {
      state.split = clampSplit(v);
      split.style.setProperty("--chart-left", `${state.split}%`);
      bar.setAttribute("aria-valuenow", String(Math.round(state.split)));
    };
    bar.onpointerdown = (e) => {
      e.preventDefault();
      dragging = true;
      bar.classList.add("dragging");
      if (bar.setPointerCapture) { try { bar.setPointerCapture(e.pointerId); } catch {} }
      const move = (ev) => {
        const box = split.getBoundingClientRect();
        if (box.width > 0) apply(((ev.clientX - box.left) / box.width) * 100);
      };
      const up = () => {
        dragging = false;
        bar.classList.remove("dragging");
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        remember();
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    };
    bar.onkeydown = (e) => {
      const step = e.key === "ArrowLeft" ? -2 : e.key === "ArrowRight" ? 2 : 0;
      if (!step) return;
      e.preventDefault();
      apply(state.split + step);
      remember();
    };
    return bar;
  }

  function render(force = false) {
    const root = view();
    if (!root || root.classList.contains("hidden")) return;
    // A redraw replaces the toolbar; skip the timer's while a picker is open
    // in it, or the list would close under the viewer's pointer. A drag of
    // the divider is skipped the same way.
    if (!force && dragging) return;
    const focused = typeof document !== "undefined" && document.activeElement;
    if (!force && focused && focused.tagName === "SELECT" && root.contains(focused)) return;
    const keepFocus = focused && root.contains(focused)
      ? (focused.classList.contains("chart-divider") ? "divider"
         : focused.dataset && focused.dataset.name ? focused.dataset.name : "")
      : "";
    const oldMain = root.querySelector(".chart-main");
    const oldSide = root.querySelector(".chart-detail");
    const tops = [oldMain ? oldMain.scrollTop : 0, oldSide ? oldSide.scrollTop : 0];
    // The grid keeps its own horizontal place across a redraw; read it before
    // the rebuild replaces the node. The timeline column (.chart-main) is the
    // scroller, so the axis header and the name column stick to it.
    const prevScroll = root.querySelector(".chart-main");
    const prevLeft = prevScroll ? prevScroll.scrollLeft : null;
    const prevPct = prevScroll ? prevScroll.dataset.widthpct : null;
    const sessions = liveSessions();
    const model = buildModel(sessions, {
      now: Date.now(), project: currentProjectPick(), group: state.group,
      groupValue: appGroupValue, groupPick: state.groupPick, range: state.range,
      archived: state.archived, sort: state.sort, cflowOf: appCflowOf,
      meshesOf: appMeshesOf, scale: state.scale,
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
    if (model.axisWidthPct > 100) {
      root.appendChild(node("p", "chart-muted chart-hint",
        `가로 스크롤: 한 화면에 ${AXIS_CAP_MS / 86400000}일까지 그린다. ` +
        "왼쪽으로 넘기면 이전 구간이 나온다."));
    }
    const chart = node("div", "chart-grid");
    chart.style.width = `${model.axisWidthPct}%`;
    chart.appendChild(axis());
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
    const split = node("div", "chart-split");
    split.style.setProperty("--chart-left", `${clampSplit(state.split)}%`);
    const main = node("div", "chart-main");
    main.setAttribute("role", "listbox");
    main.setAttribute("aria-label", "sessions");
    main.dataset.widthpct = String(model.axisWidthPct);
    // The timeline scrolls sideways inside the split's left column when the
    // axis is wider than one screen (AXIS_CAP_MS); the detail pane beside it
    // does not.
    main.appendChild(chart);
    split.appendChild(main);
    split.appendChild(divider(split));
    const side = detail(model, sessions);
    split.appendChild(side);
    root.appendChild(split);
    // The tick spacing is a question about pixels, so it waits for the column
    // to have a width.
    layoutTicks(main, model);
    main.scrollTop = tops[0];
    side.scrollTop = tops[1];
    // Same axis width: the viewer keeps their place. A new width (range or
    // cap changed) opens on the newest time -- the right edge.
    const sameWidth = prevPct !== null && prevPct === String(model.axisWidthPct);
    main.scrollLeft = sameWidth ? prevLeft : main.scrollWidth;
    if (keepFocus === "divider") split.querySelector(".chart-divider").focus();
    else if (keepFocus) {
      const again = [...main.querySelectorAll(".chart-row")].find((x) => x.dataset.name === keepFocus);
      if (again) again.focus({ preventScroll: true });
    }
  }

  function open() {
    if (state.archived && !archivedRecords.length) loadArchived();
    render();
    if (timer) clearInterval(timer);
    timer = setInterval(() => render(), REDRAW_MS);
    // A window that changed width would keep the old tick spacing until the
    // next redraw. Registered once; a redraw of the hidden view does nothing.
    if (!onResize) {
      onResize = () => render();
      if (typeof window !== "undefined" && window.addEventListener) {
        window.addEventListener("resize", onResize);
      }
    }
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
  }

  return { open, stop, render, category, projectOf, spanOf, formatDuration,
           median, ticks, buildModel, barGeometry, roleOf, clampSplit,
           positionOf, axisTicks, cflowAge, cflowChangedAt,
           RANGES, STALL_MS, AXIS_CAP_MS, LOG_UNIT_MS };
})();
