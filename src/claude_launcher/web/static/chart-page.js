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

  /* The token series (GET /api/stats/series): what the fleet spent in each
     bucket of the same time axis the session bars use (claunch-x15q5).

     The payload carries SUM per bucket, and the chart draws them as a RATE --
     tokens per second over that bucket -- so the area under the curve over
     any span is the tokens spent in it. That is the same reading the
     terminal overlay's throughput line gives for one call, and it is why the
     rate is drawn rather than the sum: on a log axis, or across buckets of
     different widths, a sum also reads as a shape, but its integral no
     longer equals the total (the reason the ticket asked for the TPS
     algorithm). */

  /* One payload -> the drawing model. ``rate`` is tokens/s in the bucket,
     ``share`` each component's part of the bucket's total, and ``peak`` the
     busiest rate (the height axis, which starts at zero because the area is
     the quantity). Points the payload pads with zeros keep their place on
     the axis: a quiet hour is a gap at the baseline, not a straight line
     between the calls on either side of it.

     ``bySession`` carries the same four components per session over the same
     window and the same filters, which is what a row prints in its token
     column -- the figure the Stats page's table prints for that session, from
     the same read. A session the ledger cannot speak for is absent from the
     map, and the row draws a dash rather than a zero. */
  function buildSeriesModel(payload) {
    if (!payload || !Array.isArray(payload.points) || !payload.points.length) return null;
    const width = Number(payload.bucket_s) || 0;
    if (!(width > 0)) return null;
    // The endpoint emits epoch SECONDS (`statsboard.Ledger.series` reads
    // `time.time()`), while every instant this page places on its axis --
    // `model.axisStart`, `model.t1`, the session spans -- is in
    // MILLISECONDS. Convert once, at the payload boundary: a bucket left in
    // seconds lands at -300% and the row draws nothing (claunch-mlrga
    // review, s1697).
    const toMs = (value) => {
      const n = Number(value);
      return Number.isFinite(n) ? n * 1000 : 0;
    };
    const points = payload.points.map((p) => {
      const total = Number(p.total) || 0;
      return {
        t: toMs(p.t),
        width,
        input: Number(p.input) || 0,
        cache_read: Number(p.cache_read) || 0,
        cache_write: Number(p.cache_write) || 0,
        output: Number(p.output) || 0,
        total,
        requests: Number(p.requests) || 0,
        rate: total / width,
      };
    });
    const peak = Math.max(0, ...points.map((p) => p.rate));
    const totals = payload.totals || {};
    const bySession = new Map();
    const raw = payload.by_session && typeof payload.by_session === "object"
      ? payload.by_session : {};
    for (const [name, value] of Object.entries(raw)) {
      if (!name || !value || typeof value !== "object") continue;
      const parts = {};
      for (const key of ["input", "cache_read", "cache_write", "output"]) {
        parts[key] = Number(value[key]) || 0;
      }
      parts.requests = Number(value.requests) || 0;
      parts.total = Number(value.total) || (parts.input + parts.cache_read +
        parts.cache_write + parts.output);
      // The buckets the session spent anything in ([t seconds, total,
      // requests]) on the `points_bucket_s` grid; what is not listed is a
      // bucket it was measured in and sat idle (claunch-58pvy).
      parts.points = (Array.isArray(value.points) ? value.points : [])
        .filter((p) => Array.isArray(p) && Number.isFinite(Number(p[0])))
        .map((p) => ({ t: toMs(p[0]), total: Number(p[1]) || 0,
                       requests: Number(p[2]) || 0 }))
        .filter((p) => p.total > 0 || p.requests > 0)
        .sort((a, b) => a.t - b.t);
      bySession.set(name, parts);
    }
    return {
      // `width` is the payload's seconds (it divides the sums into a rate);
      // `widthMs` is the same span in the axis's own unit, which the times
      // beside it are in.
      points, width, widthMs: width * 1000, peak,
      // The rows' activity grid: the ledger's five-minute bin whatever the
      // window's bucket (claunch-9n7g1); an older daemon sent the rows on
      // the series' own grid and no field for it.
      activityWidth: Number(payload.points_bucket_s) || width,
      activityMs: (Number(payload.points_bucket_s) || width) * 1000,
      start: toMs(payload.start) || points[0].t,
      until: toMs(payload.until) ||
        points[points.length - 1].t + width * 1000,
      totals: {
        total: Number(totals.total) || 0,
        requests: Number(totals.requests) || 0,
        input: Number(totals.input) || 0,
        cache_read: Number(totals.cache_read) || 0,
        cache_write: Number(totals.cache_write) || 0,
        output: Number(totals.output) || 0,
      },
      bySession,
      cacheShare: Number.isFinite(payload.cache_share) ? payload.cache_share : null,
      sessions: Number(payload.sessions) || 0,
      unmetered: Number(payload.unmetered) || 0,
      ledgerSince: payload.ledger_since || "",
    };
  }

  /* Where one bucket sits on the page's axis, in percent: its start and its
     end, each placed by the same positionOf the session bars use, so the
     token chart and the rows under it name the same instants. The last
     bucket runs to `until` rather than a whole width past the last zero
     bucket (the payload pads the window's tail). The first bucket starts on
     the series' grid, up to one width before the axis does; its left edge is
     held at 0 and the part before the axis is dropped from its width. */
  function seriesSpan(point, smodel, model, widthMs = smodel.widthMs) {
    const left = Math.max(0, positionOf(point.t, model));
    const end = Math.min(point.t + widthMs, model.t1);
    const right = Math.max(left, positionOf(Math.max(end, point.t + 1000), model));
    return { left, right };
  }

  /* How much of a session's life on its activity grid (`activityMs`, the
     ledger's five-minute bin) had a request in it (claunch-58pvy,
     claunch-9n7g1). ``covered`` counts the buckets its span touches inside
     the series window, ``busy`` those it spent anything in, ``last`` the end
     of its last busy bucket (ms) and ``idleFor`` how long a live session has
     gone since then (null when it never spent anything in the window).
     null when the row has no figures to say it with: no series read yet, or
     a session the ledger never saw -- unmeasured is not idle. */
  function activityStats(span, tokens, smodel, now) {
    if (!span || !tokens || !smodel || !(smodel.activityMs > 0)) return null;
    const w = smodel.activityMs;
    const from = Math.max(span.start, smodel.start);
    const to = Math.min(span.end, smodel.until);
    const points = tokens.points || [];
    let covered = 0;
    if (to > from) {
      const first = Math.floor((from - smodel.start) / w);
      const last = Math.ceil((to - smodel.start) / w) - 1;
      covered = Math.max(0, last - first + 1);
    }
    const busy = points.filter((p) => p.t + w > span.start && p.t < span.end).length;
    const lastPoint = points.length ? points[points.length - 1] : null;
    const last = lastPoint ? Math.min(lastPoint.t + w, smodel.until) : null;
    const at = now ?? smodel.until;
    return {
      busy, covered: Math.max(covered, busy),
      ratio: covered ? Math.min(1, busy / covered) : null,
      last,
      idleFor: span.alive && last !== null ? Math.max(0, at - last) : null,
    };
  }

  /* The bucket of one session that holds instant ``t``: its figures, or a
     zero bucket when the session was measured and spent nothing then, or
     null when ``t`` is outside the series or the session is unmeasured. */
  function bucketAt(tokens, smodel, t) {
    if (!tokens || !smodel || t === null || t === undefined) return null;
    if (t < smodel.start || t >= smodel.until) return null;
    const w = smodel.activityMs;
    const start = smodel.start + Math.floor((t - smodel.start) / w) * w;
    const hit = (tokens.points || []).find((p) => p.t === start);
    return { t: start, end: Math.min(start + w, smodel.until),
             total: hit ? hit.total : 0, requests: hit ? hit.requests : 0 };
  }

  /* The briefing digest's one line (briefing.digest names it `one_line`;
     the megathread picker reads the same field, claunch-g07ra.1). */
  function briefOf(s) {
    const b = s && s.briefing;
    if (!b) return "";
    if (typeof b === "string") return b.trim();
    return String(b.one_line || b["one-line-job-description"] || "").trim();
  }

  /* The hover card of one row, as plain data so it can be tested bare:
     a title line, the briefing's one line, and label/value rows. ``at`` is
     the instant under the pointer (null over the name column), which adds
     the bucket it falls in. */
  function rowTip(r, model, smodel, at) {
    const s = r.session || {};
    const rows = [];
    rows.push(["상태", CATEGORY_LABEL[r.category] || r.category]);
    rows.push(["생성", formatTime(r.span.start)]);
    rows.push(["종료", r.span.alive ? "(실행 중)" : formatTime(r.span.end)]);
    rows.push(["생존 시간", formatDuration(r.lifetime)]);
    if (r.tokens === null) {
      rows.push(["토큰(창)", "계측 없음 (원장 기록 없음)"]);
    } else if (r.tokens) {
      rows.push(["토큰(창)", `${formatTokens(r.tokens.total)} · 요청 ` +
        `${(Number(r.tokens.requests) || 0).toLocaleString()}`]);
      const parts = SERIES_PARTS.filter(([key]) => r.tokens[key])
        .map(([key, label]) => `${label} ${formatTokens(r.tokens[key])}`);
      if (parts.length) rows.push(["구성", parts.join(" · ")]);
      const act = activityStats(r.span, r.tokens, smodel, model.now);
      if (act && act.covered) {
        rows.push(["활동 구간", `${act.busy}/${act.covered} 버킷 ` +
          `(${formatPct(act.ratio)}, ${formatBucketWidth(smodel.activityWidth)} 단위)`]);
      }
      if (act && act.last !== null) {
        rows.push(["마지막 요청", formatTime(act.last) +
          (act.idleFor !== null && act.idleFor >= smodel.activityMs
            ? ` · ${formatDuration(act.idleFor)}째 유휴` : "")]);
      } else if (act) {
        rows.push(["마지막 요청", "이 창에서 요청 없음"]);
      }
      const bucket = bucketAt(r.tokens, smodel, at);
      if (bucket && at >= r.span.start && at <= r.span.end) {
        rows.push(["커서 위치", `${formatTime(bucket.t)}~ · ` +
          (bucket.total || bucket.requests
            ? `${formatTokens(bucket.total)} · 요청 ${bucket.requests.toLocaleString()}`
            : "요청 없음")]);
      }
    }
    if (r.cflow) {
      const age = cflowAge(r.cflow, model.now);
      rows.push(["cflow", `${r.cflow.version} ${r.cflow.label}` +
        (age.elapsed !== null ? ` · ${formatDuration(age.elapsed)} 전` : "") +
        (age.stalled ? " ⚠" : "")]);
      if (age.stalled) {
        rows.push(["정지 의심", `${formatDuration(STALL_MS)} 이상 무변화`]);
      }
    }
    if (s.profile) rows.push(["profile", s.profile]);
    return { title: r.name, role: r.role || "", brief: briefOf(s), rows };
  }

  /* The time at a horizontal place on the track, in percent: positionOf
     turned around, for both scales. */
  function timeAt(pct, model) {
    const wide = model.t1 - model.axisStart;
    if (!(wide > 0)) return model.t1;
    if (model.scale !== "log") return model.axisStart + (pct / 100) * wide;
    const den = Math.log1p(wide / LOG_UNIT_MS);
    return model.t1 - Math.expm1((1 - pct / 100) * den) * LOG_UNIT_MS;
  }

  /* The rows the page draws, from plain records.

     opts: now (ms), project ("" = all), group ("" | "mesh" | "workspace"),
     groupValue(group, s) -> the group a record is filed under, groupPick
     ("" = every group), range (a RANGES key), archived (include archived
     records), excludeKilled (drop killed records), sort ("created" |
     "lifetime" | "tokens"), cflowOf(name) -> {state,
     label, version} or null, meshesOf(name) -> the session's mesh
     memberships (for its role), tokensOf(name) -> the session's usage in the
     window ({total, requests, input, cache_read, cache_write, output}), null
     when the ledger has no record for it, or undefined when no series has
     been read yet.

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
    const tokensOf = opts.tokensOf || (() => undefined);
    const base = [];
    for (const s of sessions || []) {
      if (!s || !s.name) continue;
      if (opts.project && projectOf(s) !== opts.project) continue;
      const cat = category(s);
      if (cat === "archived" && !opts.archived) continue;
      if (cat === "killed" && opts.excludeKilled) continue;
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
        tokens: tokensOf(s.name),
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
    const spent = (r) => (r.tokens && Number(r.tokens.total)) || 0;
    rows.sort(opts.sort === "lifetime"
      ? (a, b) => b.lifetime - a.lifetime || a.name.localeCompare(b.name)
      : opts.sort === "tokens"
        ? (a, b) => spent(b) - spent(a) || a.name.localeCompare(b.name)
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
    let tokens = 0, requests = 0, metered = 0, unmetered = 0, unread = 0;
    for (const r of rows) {
      counts[r.category]++;
      if (!r.cflow) flow.none++;
      else if (r.cflow.state === "yours") flow.yours++;
      else if (r.cflow.state === "running" || r.cflow.state === "held" ||
               r.cflow.state === "peer") flow.running++;
      else flow.other++;
      if (cflowAge(r.cflow, now).stalled) flow.stalled++;
      if (r.tokens === undefined) unread++;
      else if (r.tokens === null) unmetered++;
      else {
        metered++;
        tokens += spent(r);
        requests += Number(r.tokens.requests) || 0;
      }
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
        // The rows' own figures: the sum of what their token column prints,
        // so the summary and the column cannot disagree. `unmetered` rows
        // (the ledger has no record) and `unread` (no series yet) are counted
        // apart from each other and from that sum.
        tokens: { total: tokens, requests, metered, unmetered, unread },
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
                       archived: false, excludeKilled: false, sort: "created",
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

  /* The token series the page holds: the payload for one query key, when it
     was read, and whether a read is in flight. The page redraws every 5 s;
     the ledger is one incremental read on the daemon, but a fresh request on
     every redraw is still a request per 5 s per open tab, so this keeps an
     answer for SERIES_TTL_MS and redraws from it in between. */
  const SERIES_TTL_MS = 15000;
  // key is the query in flight or last answered; dataKey is the query the
  // payload in hand answers. They differ between a pick changing the query
  // and its answer arriving, and drawing the old payload under the new
  // filters would put a figure on the page that its own picker contradicts.
  let seriesRead = { key: "", dataKey: "", at: 0, pending: false, error: "" };
  let seriesPayload = null;

  /* The query the page's own picks make: the same range, project and two
     ended boxes the rows use. A range with no series counterpart ("all" is
     the same key on both sides) is not possible -- RANGES and
     statsboard.SERIES_WINDOWS name the same four. */
  function seriesQuery() {
    return [
      state.range || "7d",
      currentProjectPick() || "",
      state.archived ? "1" : "0",
      state.excludeKilled ? "0" : "1",
    ].join("|");
  }

  function seriesUrl() {
    const q = new URLSearchParams({
      window: state.range || "7d",
      killed: state.excludeKilled ? "0" : "1",
      archived: state.archived ? "1" : "0",
    });
    const project = currentProjectPick();
    if (project) q.set("project", project);
    return `/api/stats/series?${q.toString()}`;
  }

  function readSeries(now = Date.now()) {
    if (typeof api !== "function") return;
    const key = seriesQuery();
    if (seriesRead.dataKey !== key) {
      // The picks moved away from what is drawn: drop it rather than show a
      // figure the picker beside it no longer asks for.
      seriesPayload = null;
      seriesRead.error = "";
    }
    if (seriesRead.pending) return;
    if (seriesRead.key === key && now - seriesRead.at < SERIES_TTL_MS) return;
    seriesRead.pending = true;
    seriesRead.key = key;
    api(seriesUrl(), { noBatch: true })
      .then(async (resp) => {
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        if (seriesRead.key === key) {
          seriesPayload = data;
          seriesRead.dataKey = key;
          seriesRead.error = "";
        }
      })
      .catch((e) => {
        if (seriesRead.key === key) {
          seriesPayload = null;
          seriesRead.dataKey = key;
          seriesRead.error = String((e && e.message) || e);
        }
      })
      .finally(() => {
        seriesRead.pending = false;
        seriesRead.at = Date.now();
        // Repaint now that the answer is in hand, so the block is not empty
        // for the five seconds up to the next timer tick. The unforced
        // render skips itself while a picker is open or the divider is being
        // dragged, exactly as the timer's does.
        render();
      });
  }

  /* The drawing model of the payload in hand, built once per payload: the
     rows ask it for one session each, and rebuilding it per row would walk
     every bucket per row. */
  let seriesModelCache = null;
  function currentSeries() {
    if (!seriesPayload) return null;
    if (!seriesModelCache || seriesModelCache.payload !== seriesPayload) {
      seriesModelCache = { payload: seriesPayload,
                           model: buildSeriesModel(seriesPayload) };
    }
    return seriesModelCache.model;
  }

  /* One session's usage in the window, as its row's token column prints it.
     undefined when no series has been read at all (the column is blank until
     it is), null when the ledger has no record for the session (a dash -- it
     is not a zero), else the four components' sums and the request count. */
  function seriesTokens(name) {
    const smodel = currentSeries();
    if (!smodel) return undefined;
    return smodel.bySession.get(name) || null;
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
    bar.appendChild(seg("Sort", [["created", "생성 순"], ["lifetime", "생존 시간 순"],
                                 ["tokens", "토큰 순"]],
      state.sort, (v) => set({ sort: v })));
    const arch = node("label", "chart-check");
    const box = node("input");
    box.type = "checkbox"; box.checked = !!state.archived;
    box.onchange = () => set({ archived: box.checked });
    arch.appendChild(box);
    arch.appendChild(node("span", null,
      archivedLoading ? "archived 포함 (읽는 중…)" : "archived 포함"));
    bar.appendChild(arch);
    // Killed sessions can outnumber the live fleet on a busy machine and
    // flatten the chart, so the reader may fold them away (claunch-xu9ti).
    // The same chart-check shape as the archived row beside it.
    const killed = node("label", "chart-check");
    const killedBox = node("input");
    killedBox.type = "checkbox"; killedBox.checked = !!state.excludeKilled;
    killedBox.onchange = () => set({ excludeKilled: killedBox.checked });
    killed.appendChild(killedBox);
    killed.appendChild(node("span", null, "killed 제외"));
    bar.appendChild(killed);
    return bar;
  }

  /* The token usage block above the timeline: the four figures the Stats
     page's overview reports for the same window and filters, then the series
     drawn against the timeline's own axis. The numbers are the payload's
     sums, not an area measured off the chart -- they come from the same read,
     so the two agree, and printing the sum is what makes the curve checkable
     against it. */
  function usageBlock(model, smodel, error) {
    const box = node("div", "chart-usage");
    const head = node("div", "chart-usage-head");
    head.appendChild(node("h3", null, "토큰 사용량"));
    head.appendChild(node("span", "chart-muted",
      smodel
        ? `${formatBucketWidth(smodel.width)} 버킷 · 계측 세션 ${smodel.sessions}` +
          (smodel.unmetered ? ` · 계측 없음 ${smodel.unmetered}` : "") +
          // The ledger starts when metering was switched on: a window reaching
          // past that date is not a window the figures can cover, and saying
          // so is how "nobody measured" stays apart from "spent nothing".
          (smodel.ledgerSince ? ` · 원장 ${smodel.ledgerSince}부터` : "")
        : ""));
    box.appendChild(head);
    if (error) {
      box.appendChild(node("p", "chart-error", `토큰 사용량 읽기 실패: ${error}`));
      return box;
    }
    if (!smodel) {
      box.appendChild(node("p", "chart-muted chart-usage-empty",
        seriesRead.pending ? "토큰 사용량을 읽는 중…"
          : "이 창에 기록된 토큰 사용량이 없다."));
      return box;
    }
    const stats = node("div", "chart-usage-stats");
    const stat = (label, value, cls) => {
      const item = node("div", `chart-usage-stat${cls ? " " + cls : ""}`);
      item.appendChild(node("span", "chart-usage-value", value));
      item.appendChild(node("span", "chart-usage-label", label));
      stats.appendChild(item);
    };
    stat("총 토큰", formatTokens(smodel.totals.total));
    stat("요청", smodel.totals.requests.toLocaleString());
    stat("캐시 비중", smodel.cacheShare === null ? "—" : formatPct(smodel.cacheShare));
    stat("최고 속도", smodel.peak > 0 ? formatRate(smodel.peak) : "—");
    box.appendChild(stats);
    const legend = node("div", "chart-usage-legend");
    for (const [key, label, cls] of SERIES_PARTS) {
      const item = node("span", "chart-usage-legend-item");
      item.appendChild(node("span", `chart-usage-swatch ${cls}`));
      item.appendChild(node("span", null, `${label} ${formatTokens(smodel.totals[key])}`));
      legend.appendChild(item);
    }
    box.appendChild(legend);
    box.appendChild(node("span", "chart-muted chart-usage-note",
      "아래 타임라인의 토큰/초 행: 높이는 초당 토큰, 넓이는 구간이므로 " +
      "막대의 면적이 그 구간의 토큰이다."));
    // The curve itself is a row of the timeline grid (seriesRow), so its
    // buckets sit on the bars' own axis instead of on a second one.
    return box;
  }

  /* The series as a row of the timeline grid, so its columns sit exactly
     under the session bars' instants (both use positionOf on the same model):
     a session's bar and the tokens it was spending line up vertically, which
     is the reading the ticket asked for. The row carries its own name cell
     and lifetime cell so the grid columns keep their widths; its token cell
     is the fleet's total, the sum of the columns above it. */
  function seriesRow(model, smodel) {
    const row = node("div", "chart-row chart-series-row");
    const name = node("div", "chart-name");
    name.appendChild(node("span", "chart-series-swatch"));
    name.appendChild(node("span", "chart-name-text", "토큰/초"));
    row.appendChild(name);
    const track = node("div", "chart-track chart-series-track");
    track.appendChild(seriesChart(model, smodel));
    row.appendChild(track);
    row.appendChild(node("div", "chart-life", ""));
    const total = node("div", "chart-tokens", formatTokens(smodel.totals.total));
    total.title = "이 창의 함대 전체 토큰 (아래 행들의 합)";
    row.appendChild(total);
    row.appendChild(node("div", "chart-flow chart-muted",
      smodel.peak > 0 ? formatRate(smodel.peak) : "—"));
    row.title = `토큰 사용량 시계열 · ${formatBucketWidth(smodel.width)} 버킷\n` +
      `총 ${formatTokens(smodel.totals.total)} tokens · ` +
      `요청 ${smodel.totals.requests.toLocaleString()}`;
    return row;
  }

  /* The four parts in drawing order (bottom first), with the classes the
     Stats page's stacked areas already use for them. */
  const SERIES_PARTS = [
    ["input", "fresh input", "in"],
    ["cache_read", "cache read", "cr"],
    ["cache_write", "cache write", "cw"],
    ["output", "output", "out"],
  ];

  /* The series as a stacked column chart on the timeline's axis: each bucket
     is a column whose height is its rate in tokens/초, split into the four
     parts. A column's width is the bucket's real span on that axis, so its
     area -- width x height -- is the tokens spent in it, and the area over
     any stretch of the axis is the tokens spent in that stretch. */
  function seriesChart(model, smodel) {
    const NS = "http://www.w3.org/2000/svg";
    const box = node("div", "chart-usage-chart");
    const height = 86;
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", "chart-series");
    svg.setAttribute("viewBox", `0 0 1000 ${height}`);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label",
      `토큰 사용량 시계열: 버킷당 초당 토큰, 최고 ${formatRate(smodel.peak)}`);
    const peak = smodel.peak > 0 ? smodel.peak : 1;
    const yOf = (v) => height - 1 - (v / peak) * (height - 8);
    const floor = smodel.points.map(() => 0);
    const leastPx = 1000 / Math.max(400, smodel.points.length) * 0.6;
    for (const [key, , cls] of SERIES_PARTS) {
      const roof = smodel.points.map((p, i) => floor[i] + p[key] / smodel.width);
      smodel.points.forEach((p, i) => {
        if (!roof[i] && !floor[i]) return;
        const span = seriesSpan(p, smodel, model);
        // A bucket narrower than a pixel keeps a sliver so a busy minute is
        // never drawn as nothing.
        const wPct = Math.max(span.right - span.left, leastPx / 10);
        const rect = document.createElementNS(NS, "rect");
        rect.setAttribute("class", `chart-series-area ${cls}`);
        rect.setAttribute("x", ((span.left / 100) * 1000).toFixed(2));
        rect.setAttribute("width", ((wPct / 100) * 1000).toFixed(2));
        rect.setAttribute("y", yOf(roof[i]).toFixed(2));
        rect.setAttribute("height", Math.max(0, yOf(floor[i]) - yOf(roof[i])).toFixed(2));
        const title = document.createElementNS(NS, "title");
        title.textContent = seriesTip(p);
        rect.appendChild(title);
        svg.appendChild(rect);
      });
      roof.forEach((v, i) => { floor[i] = v; });
    }
    box.appendChild(svg);
    // No caption here: the block above already carries the legend and the
    // sums, and this box is the grid row's track, which has one height.
    return box;
  }

  /* One column's hover text: when, how much, how fast, and the parts. */
  function seriesTip(p) {
    const lines = [
      `${formatTime(p.t)} ~ ${formatTime(p.t + p.width * 1000)}`,
      `합계 ${formatTokens(p.total)} tokens · 요청 ${p.requests.toLocaleString()}`,
      `속도 ${formatRate(p.rate)}`,
    ];
    for (const [key, label] of SERIES_PARTS) {
      if (p[key]) lines.push(`  ${label} ${formatTokens(p[key])}`);
    }
    return lines.join("\n");
  }

  function formatBucketWidth(s) {
    if (s >= 3600 && s % 3600 === 0) return `${s / 3600}시간`;
    if (s >= 60) return `${Math.round(s / 60)}분`;
    return `${Math.round(s)}초`;
  }

  /* 999, 1.2k, 45k, 1.2M, 3.66B: the Stats page's own shorthand (app.js
     usageShort), repeated here so the model runs without app.js. */
  function formatTokens(n) {
    if (!Number.isFinite(n) || n < 0) return "?";
    if (n < 1000) return String(Math.round(n));
    const units = [[1e9, "B"], [1e6, "M"], [1e3, "k"]];
    for (const [size, suffix] of units) {
      if (n < size) continue;
      const v = n / size;
      return (v < 10 ? v.toFixed(2) : v < 100 ? v.toFixed(1)
        : String(Math.round(v))) + suffix;
    }
    return String(Math.round(n));
  }

  function formatRate(v) {
    if (!Number.isFinite(v) || v <= 0) return "0 tok/s";
    return `${formatTokens(v)} tok/s`;
  }

  function formatPct(v) {
    return Number.isFinite(v) ? `${(v * 100).toFixed(1)}%` : "—";
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
      if (c === "killed" && state.excludeKilled) continue;
      stat(CATEGORY_LABEL[c], s.counts[c], `cat-${c}`);
    }
    stat("생존 시간 중앙값", s.medianLifetime === null ? "—" : formatDuration(s.medianLifetime));
    // The rows' own token figures, summed: the same read the usage block
    // above prints, so a reader can check one against the other. Sessions the
    // ledger cannot speak for are named apart rather than folded in as zero.
    if (s.tokens.metered || s.tokens.unmetered) {
      const stat0 = node("div", "chart-stat chart-token-stat");
      stat0.appendChild(node("span", "chart-stat-value",
        s.tokens.metered ? formatTokens(s.tokens.total) : "—"));
      stat0.appendChild(node("span", "chart-stat-label",
        "토큰" + (s.tokens.unmetered ? ` · 계측 없음 ${s.tokens.unmetered}` : "")));
      stat0.title = s.tokens.metered
        ? `이 창에서 이 행들이 쓴 토큰 ${s.tokens.total.toLocaleString()}` +
          ` · 요청 ${s.tokens.requests.toLocaleString()}` +
          (s.tokens.unmetered
            ? `\n계측 기록이 없는 세션 ${s.tokens.unmetered}개는 합계에서 빠진다` : "")
        : "이 창의 행 가운데 계측 기록이 있는 세션이 없다";
      box.appendChild(stat0);
    }
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
    const tokens = node("div", "chart-tokens", "토큰");
    tokens.title = "선택한 범위 안에서 그 세션이 쓴 토큰 (Stats 페이지의 같은 값)";
    row.appendChild(tokens);
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
    // The row's hover card (rowTip) replaces the browser's title tooltips on
    // its cells: two tooltips over one row would cover each other.
    const name = node("div", "chart-name");
    name.appendChild(node("span", `chart-dot cat-${r.category}`));
    name.appendChild(node("span", "chart-name-text", r.name));
    name.appendChild(node("span", "chart-role", r.role));
    row.appendChild(name);

    const track = node("div", "chart-track");
    const geo = barGeometry(r.span, model);
    const smodel = currentSeries();
    const busy = r.tokens && smodel ? r.tokens.points || [] : null;
    const bar = node("span", `chart-bar cat-${r.category}${geo.clipped ? " clipped" : ""}` +
      (r.span.alive ? " alive" : "") + (busy ? " metered" : ""));
    bar.style.left = `${geo.left}%`;
    bar.style.width = `${geo.width}%`;
    track.appendChild(bar);
    /* The buckets the session spent in, drawn over its bar on the ledger's
       five-minute grid (claunch-58pvy, claunch-9n7g1): a column per busy
       bucket in the bar's own status colour, as tall as its tokens against
       the row's own busiest bucket. The bar is dimmed under them, so the
       stretches of a live bar with no column are the idle ones. */
    if (busy && busy.length) {
      const strip = node("span", "chart-activity");
      const peak = Math.max(1, ...busy.map((p) => p.total));
      for (const p of busy) {
        const span = seriesSpan(p, smodel, model, smodel.activityMs);
        const mark = node("span", `chart-activity-mark cat-${r.category}`);
        mark.style.left = `${span.left}%`;
        mark.style.width = `${Math.max(span.right - span.left, 0.15)}%`;
        mark.style.height = `${Math.round(40 + 60 * Math.sqrt(p.total / peak))}%`;
        strip.appendChild(mark);
      }
      track.appendChild(strip);
    }
    row.appendChild(track);
    row.onmouseenter = (e) => showTip(r, model, e);
    row.onmousemove = (e) => showTip(r, model, e);
    row.onmouseleave = hideTip;

    row.appendChild(node("div", "chart-life", formatDuration(r.lifetime)));

    /* The window's tokens, the figure the Stats page's table prints for this
       session. A session the ledger cannot speak for (an OAuth route writes
       no metering line) shows a dash, never a zero -- "not measured here" and
       "spent nothing" are different answers (claunch-ha2s9). */
    const tokenCell = node("div", "chart-tokens");
    if (r.tokens === undefined) {
      // No series read yet: an empty cell, not a dash that would claim
      // "unmetered" before anything was asked.
      tokenCell.className = "chart-tokens chart-muted";
    } else if (r.tokens === null) {
      tokenCell.className = "chart-tokens chart-muted";
      tokenCell.textContent = "—";
    } else {
      tokenCell.textContent = formatTokens(r.tokens.total);
    }
    row.appendChild(tokenCell);

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
    } else {
      flow.appendChild(node("span", "chart-muted", "—"));
    }
    row.appendChild(flow);
    return row;
  }

  /* One hover card for the whole page, moved and refilled as the pointer
     crosses the rows. It lives on <body> so the grid's scroller does not
     clip it, and a redraw (every REDRAW_MS) leaves it in place. */
  let tipBox = null;
  let tipPointer = null;
  function showTip(r, model, e) {
    if (typeof document === "undefined" || !document.body) return;
    tipPointer = { x: e.clientX, y: e.clientY };
    if (!tipBox || !tipBox.isConnected) {
      tipBox = node("div", "chart-tip");
      tipBox.setAttribute("role", "tooltip");
      document.body.appendChild(tipBox);
    }
    let at = null;
    const track = e.target && e.target.closest ? e.target.closest(".chart-track") : null;
    if (track) {
      const box = track.getBoundingClientRect();
      if (box.width > 0) {
        const pct = Math.max(0, Math.min(100, ((e.clientX - box.left) / box.width) * 100));
        at = timeAt(pct, model);
      }
    }
    const tip = rowTip(r, model, currentSeries(), at);
    tipBox.innerHTML = "";
    const head = node("div", "chart-tip-head");
    head.appendChild(node("span", `chart-dot cat-${r.category}`));
    head.appendChild(node("strong", null, tip.title));
    if (tip.role) head.appendChild(node("span", "chart-role", tip.role));
    tipBox.appendChild(head);
    if (tip.brief) tipBox.appendChild(node("div", "chart-tip-brief", tip.brief));
    const dl = node("dl", "chart-tip-list");
    for (const [label, value] of tip.rows) {
      dl.appendChild(node("dt", null, label));
      dl.appendChild(node("dd", null, value));
    }
    tipBox.appendChild(dl);
    tipBox.classList.add("shown");
    // Beside the pointer, flipped to the other side near the window's edges.
    const pad = 14;
    const w = tipBox.offsetWidth, h = tipBox.offsetHeight;
    const vw = window.innerWidth, vh = window.innerHeight;
    let x = e.clientX + pad, y = e.clientY + pad;
    if (x + w > vw - 4) x = Math.max(4, e.clientX - pad - w);
    if (y + h > vh - 4) y = Math.max(4, e.clientY - pad - h);
    tipBox.style.left = `${x}px`;
    tipBox.style.top = `${y}px`;
  }

  function hideTip() {
    tipPointer = null;
    if (tipBox) tipBox.classList.remove("shown");
  }

  /* A redraw replaces the rows under a resting pointer, and the replaced
     row never says the pointer left. Refill the card from whatever row is
     now under it, or put it away. */
  function refreshTip(model) {
    if (!tipPointer || !tipBox || !tipBox.classList.contains("shown")) return;
    const { x, y } = tipPointer;
    const el = document.elementFromPoint ? document.elementFromPoint(x, y) : null;
    const row = el && el.closest ? el.closest(".chart-row[data-name]") : null;
    const r = row && model.rows.find((x2) => x2.name === row.dataset.name);
    if (r) showTip(r, model, { clientX: x, clientY: y, target: el });
    else hideTip();
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
    /* The window's token use, the figure the Stats page's detail panel
       reports for the same window, from the same ledger read. The four parts
       are listed beside the total the way that panel lists them, so a reader
       can tell a cache-heavy session from a fresh-input one. */
    const tokens = r.tokens !== undefined ? r.tokens : seriesTokens(r.name);
    if (tokens) {
      const parts = node("ul", "chart-detail-tokens");
      for (const [key, label] of SERIES_PARTS) {
        if (!tokens[key]) continue;
        parts.appendChild(node("li", null, `${label} ${formatTokens(tokens[key])}`));
      }
      const value = node("div", "chart-detail-token-value");
      value.appendChild(node("strong", null, formatTokens(tokens.total)));
      if (tokens.requests) {
        value.appendChild(node("span", "chart-muted",
          ` · 요청 ${tokens.requests.toLocaleString()}`));
      }
      // node() takes text, not children: build the wrapper and append.
      const wrap = node("div", null);
      wrap.appendChild(value);
      if (parts.childElementCount) wrap.appendChild(parts);
      item("토큰(창)", wrap);
    } else if (tokens === null) {
      item("토큰(창)", node("span", "chart-muted", "— 계측 원장에 기록이 없다"));
    }
    const act = activityStats(r.span, tokens, currentSeries(), model.now);
    if (act && act.covered) {
      item("활동 구간", `${act.busy}/${act.covered} 버킷 (${formatPct(act.ratio)})` +
        (act.idleFor !== null && act.idleFor >= currentSeries().activityMs
          ? ` · ${formatDuration(act.idleFor)}째 유휴` : ""));
    }
    item("briefing", briefOf(s));
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
    // The fleet's token use over the same window and the same picks
    // (claunch-x15q5) is read before the rows are built: the rows print their
    // own figures from the same payload, so the column and the chart cannot
    // come from two different reads. A request that has to go out leaves the
    // rows' token cells blank until it answers (REDRAW_MS at the latest).
    readSeries();
    const smodel = currentSeries();
    const sessions = liveSessions();
    const model = buildModel(sessions, {
      now: Date.now(), project: currentProjectPick(), group: state.group,
      groupValue: appGroupValue, groupPick: state.groupPick, range: state.range,
      archived: state.archived, excludeKilled: state.excludeKilled,
      sort: state.sort, cflowOf: appCflowOf,
      meshesOf: appMeshesOf, scale: state.scale, tokensOf: seriesTokens,
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
    root.appendChild(usageBlock(model, smodel, seriesRead.error));
    if (model.axisWidthPct > 100) {
      root.appendChild(node("p", "chart-muted chart-hint",
        `가로 스크롤: 한 화면에 ${AXIS_CAP_MS / 86400000}일까지 그린다. ` +
        "왼쪽으로 넘기면 이전 구간이 나온다."));
    }
    const chart = node("div", "chart-grid");
    chart.style.width = `${model.axisWidthPct}%`;
    chart.appendChild(axis());
    // The token series rides directly under the axis, above the session rows:
    // its columns land on the same instants their bars do, so "this session
    // was running while the fleet was spending this much" is one vertical
    // line rather than two charts to compare (claunch-x15q5).
    if (smodel) chart.appendChild(seriesRow(model, smodel));
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
    refreshTip(model);
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
    hideTip();
  }

  return { open, stop, render, category, projectOf, spanOf, formatDuration,
           median, ticks, buildModel, barGeometry, roleOf, clampSplit,
           positionOf, axisTicks, cflowAge, cflowChangedAt,
           buildSeriesModel, seriesSpan, seriesTip, formatTokens, formatRate,
           activityStats, bucketAt, briefOf, rowTip, timeAt,
           RANGES, STALL_MS, AXIS_CAP_MS, LOG_UNIT_MS, SERIES_PARTS };
})();
