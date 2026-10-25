/* Subscription quota page. The daemon's profile_usage adapter owns the
   fifteen-minute refresh period and its clock reads every key on its own
   timer; this module owns the page and its rendering. A quota belongs to the
   profile's account at its provider, so the page shows one row per
   (profile, provider) pair from /api/usage/targets, whatever harnesses run
   on that profile. Each card counts down to its key's next read and opens a
   history chart of the samples those reads left (claunch-f5w4x.3). */
(() => {
  const state = {
    open: false, loaded: false, targets: [], active: new Map(),
    values: new Map(), busy: new Set(), error: "",
    // Seconds between two reads of a key (the daemon's PERIOD_SECONDS).
    period: 900,
    // key -> epoch seconds of the key's next periodic read.
    nextAt: new Map(),
    // key -> the countdown element of its card, rewritten every second.
    countdowns: new Map(),
    // Keys whose history is open, and key -> {range, data, error, loading}.
    expanded: new Set(), histories: new Map(),
    // What the whole history directory weighs ({bytes, files, dir}).
    disk: null, ticker: null,
  };
  const SVG = "http://www.w3.org/2000/svg";
  const node = (tag, cls, text) => {
    const value = document.createElement(tag);
    if (cls) value.className = cls;
    if (text !== undefined) value.textContent = String(text);
    return value;
  };
  // At most this many account reads at once: a codex read starts a
  // subprocess, and "Refresh all" would otherwise start one per profile.
  const MAX_PARALLEL = 3;
  // The source names `claunch usage` prints (cli._print_usage).
  const SOURCES = {
    "ratelimit-headers": "via rate-limit headers",
    "codex-app-server": "via Codex app-server",
    "kimi-managed-usage": "via Kimi Code usage API",
    "kimi-web-server": "via Kimi Code local server",
    "fireworks-billing": "via Fireworks billing API",
    "opencode-go": "via OpenCode Go usage API",
    "opengateway-local": "via this machine's request records",
  };
  // Statuses `claunch usage` leaves out: the ones that say nothing is wrong.
  const QUIET_STATUSES = new Set(["allowed", "ok"]);
  // The windows the history chart draws, by the name a reading gives them,
  // with the label and line colour of each. A source whose windows have
  // other names gets a row here; until then its chart says it has none.
  const CHART_WINDOWS = [
    ["five_hour", "5h", "#388bfd"],
    ["seven_day", "7d", "#db6d28"],
    // Calendar-week and plan-month windows (Kimi, OpenCode Go).
    ["weekly", "week", "#a371f7"],
    ["monthly", "month", "#1f9e89"],
  ];
  // A source that reports dollars and no percentage window (Fireworks,
  // claunch-f5w4x.5) is charted in dollars: per window the field drawn.
  const DOLLAR_WINDOWS = [
    ["credit", "credit left", "#388bfd", "remaining_dollars"],
    ["monthly_spend", "month spend", "#db6d28", "used_dollars"],
  ];
  // How a card names a window whose reading name is not self-explanatory.
  const WINDOW_LABELS = { monthly_spend: "this month", credit: "credit" };
  // The ranges a history chart offers, in seconds (null: every sample).
  const RANGES = [["1d", 86400], ["7d", 7 * 86400], ["30d", 30 * 86400], ["All", null]];

  /* The cache facts of one read. The body carries them; the headers are a
     fallback, and a read that went through /api/batch has none
     (claunch-f5w4x.1). */
  function readFacts(response, data) {
    const header = (name) => (response.headers ? response.headers.get(name) : null);
    const cached = typeof data.cached === "boolean"
      ? data.cached : header("X-Usage-Cached") === "true";
    const updatedAt = Number(data.updated_at ?? header("X-Usage-Updated-At")) || Date.now() / 1000;
    return { cached, updatedAt };
  }

  async function readUsage(row, manual) {
    if (state.busy.has(row.key)) return;
    state.busy.add(row.key);
    render();
    try {
      const suffix = manual ? "&refresh=manual" : "&refresh=auto";
      // Out of the batch: a codex read starts a subprocess, and a batch
      // answers when its slowest path does.
      const response = await globalThis.api(
        `/api/usage?profile=${encodeURIComponent(row.profile)}${suffix}`, { noBatch: true });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      const facts = readFacts(response, data);
      state.values.set(row.key, { data, ...facts, error: "" });
      state.nextAt.set(row.key, Number(data.next_at) || facts.updatedAt + state.period);
      if (!facts.cached && state.expanded.has(row.key)) readHistory(row);
    } catch (error) {
      state.values.set(row.key, { data: null, error: error.message || String(error) });
      // A failed read keeps its error for one period, as the daemon does.
      state.nextAt.set(row.key, Date.now() / 1000 + state.period);
    } finally {
      state.busy.delete(row.key);
      if (state.open) render();
    }
  }

  async function readAll(rows, manual) {
    const queue = rows.slice();
    const worker = async () => {
      while (queue.length) await readUsage(queue.shift(), manual);
    };
    await Promise.all(Array.from({ length: Math.min(MAX_PARALLEL, queue.length) }, worker));
  }

  async function readHistory(row, range) {
    const held = state.histories.get(row.key) || { range: "7d", data: null, error: "" };
    const next = { ...held, range: range || held.range, loading: true };
    state.histories.set(row.key, next);
    if (state.open) render();
    const seconds = (RANGES.find(([name]) => name === next.range) || [])[1];
    const since = seconds ? `&since=${Math.floor(Date.now() / 1000 - seconds)}` : "";
    try {
      const response = await globalThis.api(
        `/api/usage/history?key=${encodeURIComponent(row.key)}${since}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      state.histories.set(row.key, { range: next.range, data, error: "", loading: false });
    } catch (error) {
      state.histories.set(row.key, {
        range: next.range, data: null, error: error.message || String(error), loading: false });
    }
    if (state.open) render();
  }

  /* "next read in 14:03": minutes and seconds to the key's next periodic
     read; "due" once it has passed (the daemon's clock looks every 30s). */
  function countdownText(nextAt, now) {
    if (!Number.isFinite(nextAt)) return "next read: due";
    const left = Math.round(nextAt - now / 1000);
    if (left <= 0) return "next read: due";
    const m = Math.floor(left / 60);
    return `next read in ${m}:${String(left % 60).padStart(2, "0")}`;
  }

  function sizeText(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024) return `${value} B`;
    if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
    if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
    return `${(value / 1024 ** 3).toFixed(2)} GB`;
  }

  /* The lines of a history chart: per CHART_WINDOWS entry the [at, pct]
     points of the samples that report it, split into runs wherever two
     samples are more than two periods apart (the daemon was down, or the
     account could not be read), so a gap is drawn as a gap. */
  function seriesOf(samples, period, name, value) {
    const runs = [];
    let run = [], last = null;
    for (const sample of samples) {
      const window = (sample.windows || []).find((w) => w.name === name);
      const y = window ? value(window) : NaN;
      if (!Number.isFinite(y)) continue;
      if (last !== null && sample.at - last > 2 * period && run.length) { runs.push(run); run = []; }
      run.push([sample.at, y]);
      last = sample.at;
    }
    if (run.length) runs.push(run);
    return runs;
  }

  function chartSeries(samples, period) {
    const series = [];
    for (const [name, label, color] of CHART_WINDOWS) {
      const runs = seriesOf(samples, period, name, (w) => {
        const pct = w.utilization === null || w.utilization === undefined ? NaN : Number(w.utilization);
        return Number.isFinite(pct) ? Math.max(0, Math.min(100, pct)) : NaN;
      });
      if (runs.length) series.push({ name, label, color, runs });
    }
    return series;
  }

  /* The dollar lines (DOLLAR_WINDOWS) of the same samples, unclamped. */
  function dollarSeries(samples, period) {
    const series = [];
    for (const [name, label, color, field] of DOLLAR_WINDOWS) {
      const runs = seriesOf(samples, period, name, (w) =>
        (w[field] === null || w[field] === undefined ? NaN : Number(w[field])));
      if (runs.length) series.push({ name, label, color, field, runs });
    }
    return series;
  }

  /* What a history chart draws: the percentage lines when the samples have
     any, else the dollar lines. One axis either way. */
  function chartPlan(samples, period) {
    const pct = chartSeries(samples, period);
    if (pct.length) return { unit: "pct", series: pct };
    return { unit: "usd", series: dollarSeries(samples, period) };
  }

  /* The top of a dollar axis: the next 1, 2 or 5 step above the largest
     value, so the grid reads in round amounts. */
  function niceMax(value) {
    if (!(value > 0)) return 1;
    const step = 10 ** Math.floor(Math.log10(value));
    for (const k of [1, 2, 5, 10]) if (value <= k * step) return k * step;
    return 10 * step;
  }

  function svg(tag, attrs) {
    const value = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs || {})) value.setAttribute(k, String(v));
    return value;
  }

  function stamp(at) {
    return new Date(at * 1000).toLocaleString([], {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  /* The lines on one axis (0-100%, or 0 to a round dollar amount), a
     crosshair and a tooltip on hover. */
  function chart(samples) {
    const { unit, series } = chartPlan(samples, state.period);
    const box = node("div", "profile-usage-chart");
    if (!series.length) {
      box.append(node("p", "wf-note", samples.length
        ? "This source reports no 5h, 7d, week, month or dollar window to chart."
        : "No samples in this range yet."));
      return box;
    }
    const W = 640, H = 180, L = 36, R = 34, T = 10, B = 22;
    const ats = series.flatMap((s) => s.runs.flat().map(([at]) => at));
    let x0 = Math.min(...ats), x1 = Math.max(...ats);
    if (x1 - x0 < 60) { x0 -= 1800; x1 += 1800; }
    const top = unit === "pct" ? 100
      : niceMax(Math.max(...series.flatMap((s) => s.runs.flat().map(([, v]) => v))));
    const bottom = unit === "pct" ? 0
      : Math.min(0, ...series.flatMap((s) => s.runs.flat().map(([, v]) => v)));
    const x = (at) => L + (at - x0) / (x1 - x0) * (W - L - R);
    const y = (v) => T + (top - v) / (top - bottom) * (H - T - B);
    const tick = (v) => (unit === "pct" ? `${v}%` : `$${Number(v.toFixed(2))}`);
    const plot = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "profile-usage-svg",
      role: "img", "aria-label": `usage history, ${samples.length} samples` });
    for (const v of [bottom, (bottom + top) / 2, top]) {
      plot.append(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "pu-grid" }));
      const label = svg("text", { x: L - 6, y: y(v) + 4, class: "pu-axis", "text-anchor": "end" });
      label.textContent = tick(v);
      plot.append(label);
    }
    for (const [at, anchor] of [[x0, "start"], [x1, "end"]]) {
      const label = svg("text", { x: x(at), y: H - 6, class: "pu-axis", "text-anchor": anchor });
      label.textContent = stamp(at);
      plot.append(label);
    }
    for (const s of series) {
      for (const run of s.runs) {
        if (run.length === 1) {
          plot.append(svg("circle", { cx: x(run[0][0]), cy: y(run[0][1]), r: 2.5, fill: s.color }));
          continue;
        }
        const d = run.map(([at, pct], i) => `${i ? "L" : "M"}${x(at).toFixed(1)},${y(pct).toFixed(1)}`).join("");
        plot.append(svg("path", { d, class: "pu-line", stroke: s.color }));
      }
      // Direct label at the line's last point.
      const [at, pct] = s.runs[s.runs.length - 1].slice(-1)[0];
      const tag = svg("text", { x: x(at) + 4, y: y(pct) + 4, class: "pu-direct" });
      tag.textContent = s.label;
      plot.append(tag);
    }
    const cross = svg("line", { y1: T, y2: H - B, class: "pu-cross", visibility: "hidden" });
    plot.append(cross);
    const tip = node("div", "profile-usage-tip hidden");
    const hit = svg("rect", { x: L, y: T, width: W - L - R, height: H - T - B, fill: "transparent" });
    hit.addEventListener("mousemove", (event) => {
      const rect = plot.getBoundingClientRect();
      const at = x0 + ((event.clientX - rect.left) * W / rect.width - L) / (W - L - R) * (x1 - x0);
      let best = null;
      for (const sample of samples) {
        if (!best || Math.abs(sample.at - at) < Math.abs(best.at - at)) best = sample;
      }
      if (!best) return;
      cross.setAttribute("x1", x(best.at));
      cross.setAttribute("x2", x(best.at));
      cross.setAttribute("visibility", "visible");
      const parts = series.map((s) => {
        const w = (best.windows || []).find((item) => item.name === s.name);
        if (!w) return null;
        const v = Number(unit === "pct" ? w.utilization : w[s.field]);
        if (!Number.isFinite(v)) return null;
        return unit === "pct" ? `${s.label} ${v.toFixed(1)}%` : `${s.label} ${money(v)}`;
      }).filter(Boolean);
      tip.textContent = `${stamp(best.at)} · ${parts.join(" · ")}`;
      tip.classList.remove("hidden");
      tip.style.left = `${Math.min(event.clientX - rect.left + 12, rect.width - 180)}px`;
    });
    hit.addEventListener("mouseleave", () => {
      cross.setAttribute("visibility", "hidden");
      tip.classList.add("hidden");
    });
    plot.append(hit);
    const legend = node("div", "profile-usage-legend");
    for (const s of series) {
      const item = node("span", "profile-usage-legend-item");
      const swatch = node("span", "profile-usage-swatch");
      swatch.style.background = s.color;
      item.append(swatch, document.createTextNode(s.label));
      legend.append(item);
    }
    box.append(legend, plot, tip);
    return box;
  }

  function historyBlock(row) {
    const block = node("div", "profile-usage-history");
    const held = state.histories.get(row.key) || { range: "7d" };
    const ranges = node("div", "profile-usage-ranges");
    for (const [name] of RANGES) {
      const button = node("button", `wf-btn clear${held.range === name ? " active" : ""}`, name);
      button.type = "button";
      button.addEventListener("click", () => readHistory(row, name));
      ranges.append(button);
    }
    block.append(ranges);
    if (held.error) block.append(node("p", "wf-warning", held.error));
    else if (!held.data) block.append(node("p", "wf-note", "Loading history…"));
    else {
      block.append(chart(held.data.samples || []));
      const st = held.data.stats || {};
      const notes = [`${sizeText(st.bytes)} on disk`, `${st.samples || 0} samples`];
      if (Number.isFinite(st.per_day_bytes)) {
        notes.push(`${sizeText(Math.round(st.per_day_bytes))}/day ${st.basis === "observed" ? "on average" : "projected"}`);
      }
      if (st.first_at) notes.push(`since ${stamp(st.first_at)}`);
      block.append(node("p", "wf-note profile-usage-disk", notes.join(" · ")));
    }
    return block;
  }

  function tick() {
    if (!state.open) return;
    const now = Date.now();
    for (const [key, el] of state.countdowns) el.textContent = countdownText(state.nextAt.get(key), now);
    // A key whose period ran out: read it again (automatic, so it answers
    // from what the daemon's clock just read, or reads it if the clock has
    // not yet).
    for (const row of state.targets) {
      if (!row.supported || state.busy.has(row.key)) continue;
      const nextAt = state.nextAt.get(row.key);
      if (Number.isFinite(nextAt) && nextAt <= now / 1000) readUsage(row, false);
    }
  }

  function toDate(value) {
    if (value === null || value === undefined || value === "") return null;
    const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
    return Number.isNaN(date.valueOf()) ? null : date;
  }

  function span(ms) {
    const minutes = Math.round(Math.abs(ms) / 60000);
    if (minutes < 1) return "<1m";
    const days = Math.floor(minutes / 1440);
    const hours = Math.floor((minutes % 1440) / 60);
    const rest = minutes % 60;
    if (days) return hours ? `${days}d ${hours}h` : `${days}d`;
    if (hours) return rest ? `${hours}h ${rest}m` : `${hours}h`;
    return `${rest}m`;
  }

  /* "2h16m" as `claunch usage` writes it (cli._fmt_reset): whole minutes,
     hours without days. */
  function resetText(resets, now) {
    const minutes = Math.floor((resets.valueOf() - now) / 60000);
    if (minutes <= 0) return "resetting";
    if (minutes < 60) return `resets in ${minutes}m`;
    return `resets in ${Math.floor(minutes / 60)}h${String(minutes % 60).padStart(2, "0")}m`;
  }

  /* The windows `claunch usage` prints: the ones with use or a reset time,
     or all of them when none has either. */
  function shownWindows(windows) {
    const active = windows.filter((window) => Number(window.utilization) > 0 || window.resets_at
      || Number.isFinite(dollars(window.used_dollars)) || window.detail);
    return active.length ? active : windows;
  }

  function level(pct) {
    if (!Number.isFinite(pct)) return "";
    if (pct >= 90) return "high";
    if (pct >= 70) return "mid";
    return "low";
  }

  // A dollar field as a number; null and missing are not $0.
  function dollars(value) {
    return value === null || value === undefined || value === "" ? NaN : Number(value);
  }

  function money(value) {
    const number = dollars(value);
    return Number.isFinite(number) ? `$${number.toFixed(2)}` : "";
  }

  function windowLine(window) {
    const line = node("div", "profile-usage-window");
    // A reading with no percentage to show: the reader's own line
    // (opengateway-local) rather than a bar at zero.
    const detail = typeof window.detail === "string" && window.detail ? window.detail : "";
    // Spend with no limit has no percentage: the line shows the amount.
    const amountOnly = Number.isFinite(dollars(window.used_dollars))
      && !Number.isFinite(dollars(window.limit_dollars));
    const pct = amountOnly || detail ? NaN : Number(window.utilization);
    line.append(
      node("strong", "", WINDOW_LABELS[window.name] || String(window.name)),
      node("span", `profile-usage-pct ${level(pct)}`, detail || (amountOnly ? money(window.used_dollars)
        : Number.isFinite(pct) ? `${pct.toFixed(1)}%` : "—")),
    );
    const bar = node("div", `profile-usage-bar ${level(pct)}`);
    const fill = node("span");
    fill.style.width = `${Number.isFinite(pct) ? Math.max(0, Math.min(100, pct)) : 0}%`;
    bar.append(fill);
    if (amountOnly || detail) bar.classList.add("hidden");
    line.append(bar);
    const notes = [];
    const used = money(window.used_dollars);
    const limit = money(window.limit_dollars);
    const left = money(window.remaining_dollars);
    if (limit) notes.push(`${used || "$0.00"} of ${limit}`);
    else if (used && !amountOnly) notes.push(used);
    if (left) notes.push(`${left} left`);
    const resets = toDate(window.resets_at);
    if (resets) notes.push(resetText(resets, Date.now()));
    if (window.status && !QUIET_STATUSES.has(String(window.status))) notes.push(`[${window.status}]`);
    if (notes.length) {
      const small = node("small", "", notes.join(" · "));
      if (resets) small.title = `resets ${resets.toLocaleString()}`;
      line.append(small);
    }
    return line;
  }

  function card(row) {
    const busy = state.busy.has(row.key);
    const section = node("section", "window-section stats-section profile-usage-card");
    const title = node("div", "profile-usage-head");
    const name = node("h3", "", row.profile);
    title.append(name);
    title.append(node("span", "profile-usage-chip", `provider ${row.provider}`));
    title.append(node("span", "profile-usage-chip muted", `via ${row.harness}`));
    const sessions = state.active.get(row.profile) || 0;
    if (sessions) title.append(node("span", "profile-usage-chip live", `${sessions} running`));
    const countdown = node("span", "profile-usage-chip muted profile-usage-next",
      countdownText(state.nextAt.get(row.key), Date.now()));
    countdown.title = `The daemon reads every account once per ${Math.round(state.period / 60)} minutes; Refresh reads now and restarts this timer.`;
    state.countdowns.set(row.key, countdown);
    title.append(countdown);
    const open = state.expanded.has(row.key);
    const toggle = node("button", `wf-btn clear${open ? " active" : ""}`, "History");
    toggle.type = "button";
    toggle.addEventListener("click", () => {
      if (state.expanded.has(row.key)) { state.expanded.delete(row.key); render(); return; }
      state.expanded.add(row.key);
      readHistory(row);
    });
    title.append(toggle);
    const refresh = node("button", "wf-btn clear", busy ? "Updating…" : "Refresh");
    refresh.type = "button";
    refresh.disabled = busy;
    refresh.addEventListener("click", () => readUsage(row, true));
    title.append(refresh);
    section.append(title);
    const reading = state.values.get(row.key);
    if (!reading) {
      section.append(node("p", "wf-note", busy ? "Loading usage…" : "Not read yet. Refresh to read this account."));
    } else if (reading.error) {
      section.append(node("p", "wf-warning", reading.error));
    } else {
      const windows = reading.data.windows || [];
      if (!windows.length) section.append(node("p", "wf-note", "No usage windows reported."));
      for (const window of shownWindows(windows)) section.append(windowLine(window));
      // The Fireworks API reports no prepaid balance (claunch-f5w4x.5).
      if (reading.data.source === "fireworks-billing" && !windows.some((w) => w.name === "credit")) {
        section.append(node("p", "wf-note", "To show credit left, copy the balance from the Fireworks console into ~/.claude-launcher/.claunch.yaml as credit: {balance_usd: <amount>, as_of: <YYYY-MM-DD>} on this provider or profile."));
      }
      const updated = toDate(reading.updatedAt);
      if (updated) {
        const ago = Date.now() - updated.valueOf();
        section.append(node("p", "wf-note profile-usage-updated",
          `${reading.cached ? "Cached" : "Updated"} ${span(ago)} ago · ${SOURCES[reading.data.source] || reading.data.source || ""}`));
      }
    }
    if (open) section.append(historyBlock(row));
    return section;
  }

  function render() {
    const view = document.getElementById("profile-usage-view");
    if (!view) return;
    view.replaceChildren();
    state.countdowns = new Map();
    const header = node("div", "stats-controls");
    header.append(node("h2", "", "Profile usage"));
    const supported = state.targets.filter((row) => row.supported);
    const all = node("button", "wf-btn clear", "Refresh all");
    all.type = "button";
    all.disabled = !supported.length || supported.every((row) => state.busy.has(row.key));
    all.addEventListener("click", () => readAll(supported, true));
    header.append(all);
    view.append(header);
    const minutes = Math.round(state.period / 60);
    view.append(node("p", "wf-note", `Subscription limits and account spend by profile and provider. The daemon reads every account once every ${minutes} minutes, each on its own timer, and keeps every reading as history; Refresh reads immediately and restarts that account's timer.`));
    if (state.disk) {
      view.append(node("p", "wf-note profile-usage-disk",
        `History on disk: ${sizeText(state.disk.bytes)} in ${state.disk.files} file(s) · ${state.disk.dir}`));
    }
    if (state.error) view.append(node("p", "wf-warning", state.error));
    if (!state.loaded) {
      if (!state.error) view.append(node("p", "wf-note", "Loading profiles…"));
      return;
    }
    if (!supported.length) view.append(node("p", "wf-note", "No profile has a provider whose usage can be read."));
    for (const row of supported) view.append(card(row));
    const other = state.targets.filter((row) => !row.supported);
    if (other.length) {
      const details = node("details", "profile-usage-other");
      details.append(node("summary", "", `${other.length} profile(s) without a usage reader`));
      const list = node("ul");
      for (const row of other) {
        const item = node("li");
        item.append(node("strong", "", row.profile));
        if (row.provider) item.append(node("span", "profile-usage-chip", `provider ${row.provider}`));
        item.append(node("small", "", row.reason || ""));
        list.append(item);
      }
      details.append(list);
      view.append(details);
    }
  }

  async function open() {
    state.open = true;
    state.error = "";
    render();
    try {
      const [targetsResponse, sessionsResponse] = await Promise.all([
        globalThis.api("/api/usage/targets"),
        globalThis.api("/api/sessions?view=rail&state=active"),
      ]);
      if (!targetsResponse.ok) throw new Error("Could not load profiles.");
      const listing = await targetsResponse.json();
      const targets = listing.targets || [];
      if (Number(listing.period_s) > 0) state.period = Number(listing.period_s);
      state.disk = listing.history || null;
      for (const row of targets) {
        const nextAt = Number(row.schedule && row.schedule.next_at);
        if (Number.isFinite(nextAt) && nextAt > 0) state.nextAt.set(row.key, nextAt);
      }
      const sessions = sessionsResponse.ok ? (await sessionsResponse.json()).sessions || [] : [];
      state.active = new Map();
      for (const session of sessions) {
        if (session.status === "exited" || !session.profile) continue;
        const profile = String(session.profile).split(":")[0];
        state.active.set(profile, (state.active.get(profile) || 0) + 1);
      }
      // Profiles with running sessions first; the rest keep name order.
      const running = (row) => (state.active.get(row.profile) ? 0 : 1);
      state.targets = targets.filter((row) => row.profile)
        .sort((a, b) => running(a) - running(b) || a.profile.localeCompare(b.profile));
      state.loaded = true;
      render();
      // Every account, automatic: the daemon's clock keeps them all in its
      // cache, so this costs a provider call only for one it has not read.
      readAll(state.targets.filter((row) => row.supported), false);
      if (!state.ticker) state.ticker = setInterval(tick, 1000);
    } catch (error) {
      state.error = error.message || String(error);
      render();
    }
  }

  function stop() {
    state.open = false;
    if (state.ticker) { clearInterval(state.ticker); state.ticker = null; }
  }
  globalThis.ProfileUsagePage = {
    open, stop,
    _test: {
      readUsage, readFacts, resetText, shownWindows, countdownText, sizeText, chartSeries,
      dollarSeries, chartPlan, niceMax, windowLine, tick, state,
    },
  };
})();
