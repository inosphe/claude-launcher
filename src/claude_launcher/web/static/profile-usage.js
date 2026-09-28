/* Subscription quota page. The daemon's profile_usage adapter owns the
   five-minute automatic refresh window; this module owns the page and its
   harness-neutral rendering. */
(() => {
  const state = { open: false, profiles: [], values: new Map(), busy: new Set(), error: "" };
  const node = (tag, cls, text) => {
    const value = document.createElement(tag);
    if (cls) value.className = cls;
    if (text !== undefined) value.textContent = String(text);
    return value;
  };
  const selectorFor = (row) => `${row.profile}:${row.harness}`;
  const labelFor = (row) => `${row.profile} / ${row.harness}`;

  async function readUsage(row, manual) {
    const selector = selectorFor(row);
    if (state.busy.has(selector)) return;
    state.busy.add(selector);
    render();
    try {
      const suffix = manual ? "&refresh=manual" : "&refresh=auto";
      const response = await globalThis.api(
        `/api/usage?profile=${encodeURIComponent(selector)}${suffix}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      state.values.set(selector, {
        data,
        cached: response.headers.get("X-Usage-Cached") === "true",
        updatedAt: Number(response.headers.get("X-Usage-Updated-At")) || Date.now() / 1000,
        error: "",
      });
    } catch (error) {
      state.values.set(selector, { data: null, error: error.message || String(error) });
    } finally {
      state.busy.delete(selector);
      if (state.open) render();
    }
  }

  function formatTime(value) {
    if (!value) return "";
    const date = new Date(value * (typeof value === "number" ? 1000 : 1));
    return Number.isNaN(date.valueOf()) ? "" : date.toLocaleString();
  }

  function render() {
    const view = document.getElementById("profile-usage-view");
    if (!view) return;
    view.replaceChildren();
    const header = node("div", "stats-controls");
    header.append(node("h2", "", "Profile usage"));
    header.append(node("p", "wf-note", "Subscription limits by profile and harness. Automatic updates are limited to once every 5 minutes per profile; refresh buttons run immediately."));
    view.append(header);
    if (state.error) view.append(node("p", "wf-warning", state.error));
    if (!state.profiles.length) {
      view.append(node("p", "wf-note", "Loading profiles…"));
      return;
    }
    for (const row of state.profiles) {
      const selector = selectorFor(row);
      const card = node("section", "window-section stats-section profile-usage-card");
      const title = node("div", "stats-controls");
      title.append(node("h3", "", labelFor(row)));
      const refresh = node("button", "wf-btn clear", state.busy.has(selector) ? "Updating…" : "Refresh");
      refresh.type = "button";
      refresh.disabled = state.busy.has(selector);
      refresh.addEventListener("click", () => readUsage(row, true));
      title.append(refresh);
      card.append(title);
      const reading = state.values.get(selector);
      if (!reading) {
        card.append(node("p", "wf-note", row.active ? "Active profile; loading usage…" : "Not active"));
      } else if (reading.error) {
        card.append(node("p", "wf-warning", reading.error));
      } else {
        const updated = formatTime(reading.updatedAt);
        if (updated) card.append(node("p", "wf-note", `${reading.cached ? "Last updated" : "Updated"}: ${updated}`));
        const windows = reading.data.windows || [];
        if (!windows.length) card.append(node("p", "wf-note", "No usage windows reported."));
        for (const window of windows) {
          const line = node("div", "profile-usage-window");
          const name = node("strong", "", String(window.name).replaceAll("_", " "));
          const pct = Number(window.utilization);
          line.append(name, node("span", "", Number.isFinite(pct) ? `${pct.toFixed(1)}%` : "—"));
          const bar = node("progress", "", "");
          bar.max = 100;
          bar.value = Number.isFinite(pct) ? Math.max(0, Math.min(100, pct)) : 0;
          line.append(bar);
          if (window.resets_at) line.append(node("small", "", `Resets ${new Date(window.resets_at).toLocaleString()}`));
          card.append(line);
        }
      }
      view.append(card);
    }
  }

  async function open() {
    state.open = true;
    state.error = "";
    render();
    try {
      const [profilesResponse, sessionsResponse] = await Promise.all([
        globalThis.api("/api/profiles"),
        globalThis.api("/api/sessions?view=rail&state=active"),
      ]);
      if (!profilesResponse.ok) throw new Error("Could not load profiles.");
      const profiles = await profilesResponse.json();
      const sessions = sessionsResponse.ok ? (await sessionsResponse.json()).sessions || [] : [];
      const active = new Set(sessions
        .filter((session) => session.status !== "exited" && session.profile)
        .map((session) => `${session.profile}:${session.harness || ""}`));
      state.profiles = (profiles.profile_details || []).map((row) => ({
        profile: row.profile,
        harness: row.harness,
        active: active.has(`${row.profile}:${row.harness}`),
      })).filter((row) => row.profile && row.harness)
        .filter((row, index, all) => all.findIndex((other) => selectorFor(other) === selectorFor(row)) === index);
      render();
      for (const row of state.profiles) {
        if (row.active) readUsage(row, false);
      }
    } catch (error) {
      state.error = error.message || String(error);
      render();
    }
  }

  function stop() { state.open = false; }
  globalThis.ProfileUsagePage = { open, stop };
})();
