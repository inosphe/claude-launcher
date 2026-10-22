/* Subscription quota page. The daemon's profile_usage adapter owns the
   five-minute automatic refresh window; this module owns the page and its
   rendering. A quota belongs to the profile's account at its provider, so the
   page shows one row per (profile, provider) pair from /api/usage/targets,
   whatever harnesses run on that profile. */
(() => {
  const state = {
    open: false, loaded: false, targets: [], active: new Map(),
    values: new Map(), busy: new Set(), error: "",
  };
  const node = (tag, cls, text) => {
    const value = document.createElement(tag);
    if (cls) value.className = cls;
    if (text !== undefined) value.textContent = String(text);
    return value;
  };
  // At most this many account reads at once: a codex read starts a
  // subprocess, and "Refresh all" would otherwise start one per profile.
  const MAX_PARALLEL = 3;

  async function readUsage(row, manual) {
    if (state.busy.has(row.key)) return;
    state.busy.add(row.key);
    render();
    try {
      const suffix = manual ? "&refresh=manual" : "&refresh=auto";
      const response = await globalThis.api(
        `/api/usage?profile=${encodeURIComponent(row.profile)}${suffix}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      state.values.set(row.key, {
        data,
        cached: response.headers.get("X-Usage-Cached") === "true",
        updatedAt: Number(response.headers.get("X-Usage-Updated-At")) || Date.now() / 1000,
        error: "",
      });
    } catch (error) {
      state.values.set(row.key, { data: null, error: error.message || String(error) });
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

  function level(pct) {
    if (!Number.isFinite(pct)) return "";
    if (pct >= 90) return "high";
    if (pct >= 70) return "mid";
    return "low";
  }

  function money(value) {
    const number = Number(value);
    return Number.isFinite(number) ? `$${number.toFixed(2)}` : "";
  }

  function windowLine(window) {
    const line = node("div", "profile-usage-window");
    const pct = Number(window.utilization);
    line.append(
      node("strong", "", String(window.name).replaceAll("_", " ")),
      node("span", `profile-usage-pct ${level(pct)}`, Number.isFinite(pct) ? `${pct.toFixed(1)}%` : "—"),
    );
    const bar = node("div", `profile-usage-bar ${level(pct)}`);
    const fill = node("span");
    fill.style.width = `${Number.isFinite(pct) ? Math.max(0, Math.min(100, pct)) : 0}%`;
    bar.append(fill);
    line.append(bar);
    const notes = [];
    const used = money(window.used_dollars);
    const limit = money(window.limit_dollars);
    if (used || limit) notes.push(limit ? `${used || "$0.00"} of ${limit}` : used);
    if (window.status) notes.push(String(window.status));
    const resets = toDate(window.resets_at);
    if (resets) {
      const left = resets.valueOf() - Date.now();
      notes.push(left > 0
        ? `resets in ${span(left)} (${resets.toLocaleString()})`
        : `reset ${resets.toLocaleString()}`);
    }
    if (notes.length) line.append(node("small", "", notes.join(" · ")));
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
      for (const window of windows) section.append(windowLine(window));
      const updated = toDate(reading.updatedAt);
      if (updated) {
        const ago = Date.now() - updated.valueOf();
        section.append(node("p", "wf-note profile-usage-updated",
          `${reading.cached ? "Cached" : "Updated"} ${span(ago)} ago · ${reading.data.source || ""}`));
      }
    }
    return section;
  }

  function render() {
    const view = document.getElementById("profile-usage-view");
    if (!view) return;
    view.replaceChildren();
    const header = node("div", "stats-controls");
    header.append(node("h2", "", "Profile usage"));
    const supported = state.targets.filter((row) => row.supported);
    const all = node("button", "wf-btn clear", "Refresh all");
    all.type = "button";
    all.disabled = !supported.length || supported.every((row) => state.busy.has(row.key));
    all.addEventListener("click", () => readAll(supported, true));
    header.append(all);
    view.append(header);
    view.append(node("p", "wf-note", "Subscription limits by profile and provider. Opening this page reads the profiles with running sessions, at most once every 5 minutes per account; Refresh reads immediately."));
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
      const targets = (await targetsResponse.json()).targets || [];
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
      readAll(state.targets.filter((row) => row.supported && state.active.get(row.profile)), false);
    } catch (error) {
      state.error = error.message || String(error);
      render();
    }
  }

  function stop() { state.open = false; }
  globalThis.ProfileUsagePage = { open, stop };
})();
