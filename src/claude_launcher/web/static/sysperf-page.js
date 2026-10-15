/* System performance (claunch-zuk0b): the rail header's CPU / MEM / IO badge
   and the #/sysperf page behind it. The daemon samples the machine
   (daemon/sysperf.py) every few seconds into an in-memory history and a
   rotating log; this module reads GET /api/sysperf.

   The badge asks for the latest sample only (a few hundred bytes) and skips
   its poll while the tab is hidden. The page reads the whole history once on
   entry, then appends the sample each badge poll brings, capped at the
   daemon's own history length, and drops it when the page is left. */
(() => {
  const POLL_MS = 5000;
  const SVG = "http://www.w3.org/2000/svg";
  const page = { open: false, history: [], max: 720, info: null, error: "" };
  let badgeTimer = null;

  const node = (tag, cls, text) => {
    const value = document.createElement(tag);
    if (cls) value.className = cls;
    if (text !== undefined) value.textContent = String(text);
    return value;
  };
  const svg = (tag, attrs) => {
    const value = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs || {})) value.setAttribute(k, String(v));
    return value;
  };

  function fmtBytes(n, suffix = "") {
    if (typeof n !== "number" || !Number.isFinite(n)) return "—";
    const units = ["B", "K", "M", "G", "T"];
    let i = 0;
    while (Math.abs(n) >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    const digits = i === 0 || n >= 100 ? 0 : 1;
    return `${n.toFixed(digits)}${units[i]}${suffix}`;
  }
  const fmtPct = (n) => (typeof n === "number" && Number.isFinite(n) ? `${n.toFixed(n >= 10 ? 0 : 1)}%` : "—");
  const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const ioTotal = (s) => (typeof s.disk_read_bps === "number" ? s.disk_read_bps + (s.disk_write_bps || 0) : null);

  function grade(s) {
    if (!s) return "latency-idle";
    const worst = Math.max(s.cpu_pct || 0, s.mem_pct || 0, s.disk_busy_pct || 0);
    if (worst >= 90) return "latency-bad";
    if (worst >= 70) return "latency-slow";
    return "latency-ok";
  }

  async function fetchSysperf(history) {
    const response = await globalThis.api(`/api/sysperf${history ? "?history=1" : ""}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  // ------------------------------------------------------------------ badge

  function renderBadge(data, error) {
    const badge = document.getElementById("sysperf-badge");
    if (!badge) return;
    if (data && data.enabled === false) { badge.classList.add("hidden"); return; }
    badge.classList.remove("hidden");
    const s = data && data.latest;
    if (!s) {
      badge.textContent = "CPU … · MEM … · IO …";
      badge.className = "badge latency-idle";
      badge.title = error || (data && data.error) || "waiting for the first sample";
      return;
    }
    badge.textContent = `CPU ${fmtPct(s.cpu_pct)} · MEM ${fmtPct(s.mem_pct)} · IO ${fmtBytes(ioTotal(s), "/s")}`;
    badge.className = `badge ${grade(s)}`;
    badge.title = [
      `CPU ${fmtPct(s.cpu_pct)} of ${s.cpus || "?"} CPUs` + (s.load1 != null ? `, load ${s.load1}` : ""),
      `memory ${fmtBytes(s.mem_used)} / ${fmtBytes(s.mem_total)} (${fmtPct(s.mem_pct)})`,
      `disk read ${fmtBytes(s.disk_read_bps, "/s")}, write ${fmtBytes(s.disk_write_bps, "/s")}` +
        (s.disk_busy_pct != null ? `, busiest disk ${fmtPct(s.disk_busy_pct)} busy` : ""),
      `source: ${s.source || "?"} · ${fmtTime(s.ts)} — click for history`,
    ].join("\n");
  }

  async function pollBadge() {
    if (document.hidden) return;
    try {
      const data = await fetchSysperf(false);
      renderBadge(data, "");
      if (page.open) absorb(data);
    } catch (error) {
      renderBadge(null, error.message || String(error));
    }
  }

  function onVisible() { if (!document.hidden) pollBadge(); }

  function startBadge() {
    if (badgeTimer) return;
    pollBadge();
    badgeTimer = setInterval(pollBadge, POLL_MS);
    document.addEventListener("visibilitychange", onVisible);
  }

  // For page harnesses that record every api() call: the badge's poll is a
  // GET every few seconds that would land in their recording.
  function stopBadge() {
    if (badgeTimer) { clearInterval(badgeTimer); badgeTimer = null; }
    document.removeEventListener("visibilitychange", onVisible);
  }

  // ------------------------------------------------------------------ chart

  function niceMax(v) {
    if (!(v > 0)) return 1;
    const p = 10 ** Math.floor(Math.log10(v));
    for (const m of [1, 2, 5, 10]) if (m * p >= v) return m * p;
    return 10 * p;
  }
  // The same in the 1024-based units fmtBytes prints, so the axis reads 2M,
  // not 4.8M.
  function niceBytes(v) {
    if (!(v > 0)) return 1024;
    const unit = 1024 ** Math.max(0, Math.floor(Math.log(v) / Math.log(1024)));
    return niceMax(v / unit) * unit;
  }

  /* One line chart: one y-axis, recessive grid, a crosshair that snaps to
     the nearest sample and a tooltip listing every series at that time. */
  function lineChart({ title, series, points, yMax, fmt, nice = niceMax }) {
    const card = node("section", "window-section sysperf-card");
    const head = node("div", "sysperf-card-head");
    head.append(node("h3", "", title));
    if (series.length > 1) {
      const legend = node("div", "sysperf-legend");
      for (const s of series) {
        const item = node("span", "sysperf-legend-item");
        item.append(node("i", `sysperf-key ${s.cls}`), document.createTextNode(s.label));
        legend.append(item);
      }
      head.append(legend);
    }
    card.append(head);
    if (points.length < 2) {
      card.append(node("p", "wf-note", "Collecting samples…"));
      return card;
    }
    const W = 640, H = 170, L = 48, R = 12, T = 8, B = 22;
    const t0 = points[0].ts, t1 = points[points.length - 1].ts;
    const span = Math.max(t1 - t0, 1);
    let top = yMax;
    if (top == null) {
      let m = 0;
      for (const p of points) for (const s of series) { const v = s.get(p); if (typeof v === "number") m = Math.max(m, v); }
      top = nice(m);
    }
    const x = (ts) => L + ((ts - t0) / span) * (W - L - R);
    const y = (v) => T + (1 - Math.min(v, top) / top) * (H - T - B);
    const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "sysperf-chart", role: "img", "aria-label": title });
    for (const f of [0, 0.5, 1]) {
      const gy = y(top * f);
      root.append(svg("line", { x1: L, x2: W - R, y1: gy, y2: gy, class: "sysperf-grid" }));
      const label = svg("text", { x: L - 6, y: gy + 4, class: "sysperf-axis", "text-anchor": "end" });
      label.textContent = fmt(top * f);
      root.append(label);
    }
    for (const [ts, anchor] of [[t0, "start"], [t1, "end"]]) {
      const label = svg("text", { x: x(ts), y: H - 6, class: "sysperf-axis", "text-anchor": anchor });
      label.textContent = fmtTime(ts);
      root.append(label);
    }
    for (const s of series) {
      let d = "";
      let pen = false;
      for (const p of points) {
        const v = s.get(p);
        if (typeof v !== "number") { pen = false; continue; }
        d += `${pen ? "L" : "M"}${x(p.ts).toFixed(1)},${y(v).toFixed(1)}`;
        pen = true;
      }
      if (d) root.append(svg("path", { d, class: `sysperf-line ${s.cls}` }));
    }
    const hair = svg("line", { y1: T, y2: H - B, class: "sysperf-hair hidden" });
    root.append(hair);
    const dots = series.map((s) => {
      const dot = svg("circle", { r: 4, class: `sysperf-dot ${s.cls} hidden` });
      root.append(dot);
      return dot;
    });
    const hit = svg("rect", { x: L, y: T, width: W - L - R, height: H - T - B, class: "sysperf-hit" });
    root.append(hit);
    const wrap = node("div", "sysperf-chart-wrap");
    const tip = node("div", "sysperf-tip hidden");
    wrap.append(root, tip);
    card.append(wrap);

    const hide = () => {
      hair.classList.add("hidden");
      dots.forEach((dot) => dot.classList.add("hidden"));
      tip.classList.add("hidden");
    };
    hit.addEventListener("pointerleave", hide);
    hit.addEventListener("pointermove", (event) => {
      const box = root.getBoundingClientRect();
      const px = ((event.clientX - box.left) / box.width) * W;
      const ts = t0 + ((px - L) / (W - L - R)) * span;
      let best = points[0];
      for (const p of points) if (Math.abs(p.ts - ts) < Math.abs(best.ts - ts)) best = p;
      const bx = x(best.ts);
      hair.setAttribute("x1", bx); hair.setAttribute("x2", bx);
      hair.classList.remove("hidden");
      tip.replaceChildren(node("div", "sysperf-tip-time", fmtTime(best.ts)));
      series.forEach((s, i) => {
        const v = s.get(best);
        if (typeof v === "number") {
          dots[i].setAttribute("cx", bx); dots[i].setAttribute("cy", y(v));
          dots[i].classList.remove("hidden");
        } else {
          dots[i].classList.add("hidden");
        }
        const row = node("div", "sysperf-tip-row");
        row.append(node("i", `sysperf-tip-key ${s.cls}`), node("strong", "", typeof v === "number" ? fmt(v) : "—"),
          node("span", "", s.label));
        tip.append(row);
      });
      tip.classList.remove("hidden");
      const left = (bx / W) * box.width;
      tip.style.left = `${Math.min(Math.max(left + 10, 0), Math.max(box.width - tip.offsetWidth, 0))}px`;
    });
    return card;
  }

  // ------------------------------------------------------------------- page

  function absorb(data) {
    const s = data && data.latest;
    if (!s) return;
    const last = page.history[page.history.length - 1];
    if (last && last.ts >= s.ts) return;
    page.history.push(s);
    if (page.history.length > page.max) page.history.splice(0, page.history.length - page.max);
    if (data.error !== undefined && page.info) page.info.error = data.error;
    render();
  }

  function detailTable(s) {
    const table = node("table", "sysperf-table");
    const add = (k, v) => {
      const tr = node("tr");
      tr.append(node("th", "", k), node("td", "", v));
      table.append(tr);
    };
    add("CPU", `${fmtPct(s.cpu_pct)} of ${s.cpus || "?"} logical CPUs`);
    if (s.load1 != null) add("Load (1 min)", s.load1);
    add("Memory", `${fmtBytes(s.mem_used)} used of ${fmtBytes(s.mem_total)} (${fmtPct(s.mem_pct)})`);
    if (s.swap_total) add("Swap", `${fmtBytes(s.swap_used)} used of ${fmtBytes(s.swap_total)}`);
    add("Disk read", fmtBytes(s.disk_read_bps, "/s"));
    add("Disk write", fmtBytes(s.disk_write_bps, "/s"));
    if (s.disk_busy_pct != null) add("Busiest disk", `${fmtPct(s.disk_busy_pct)} busy`);
    add("Sampled", `${new Date(s.ts * 1000).toLocaleString()} (every ${s.interval_s}s)`);
    return table;
  }

  function diskTable(disks) {
    const names = Object.keys(disks || {});
    if (!names.length) return null;
    const table = node("table", "sysperf-table sysperf-disks");
    const head = node("tr");
    for (const h of ["Disk", "Read", "Write", "Busy"]) head.append(node("th", "", h));
    table.append(head);
    for (const name of names) {
      const d = disks[name];
      const tr = node("tr");
      tr.append(node("td", "", name), node("td", "", fmtBytes(d.read_bps, "/s")),
        node("td", "", fmtBytes(d.write_bps, "/s")), node("td", "", fmtPct(d.busy_pct)));
      table.append(tr);
    }
    return table;
  }

  function render() {
    const view = document.getElementById("sysperf-view");
    if (!view || !page.open) return;
    view.replaceChildren();
    const header = node("div", "stats-controls");
    header.append(node("h2", "", "System performance"));
    view.append(header);
    const info = page.info || {};
    const latest = page.history[page.history.length - 1];
    const note = [];
    if (latest && latest.source) {
      note.push(`Source: ${latest.source}` +
        (latest.source === "procfs" ? " (under WSL this is the WSL VM, not the Windows host)" : "") + ".");
    }
    if (info.interval_s) note.push(`A sample every ${info.interval_s}s; the last ${page.max} are kept here.`);
    if (note.length) view.append(node("p", "wf-note", note.join(" ")));
    if (page.error) view.append(node("p", "wf-warning", page.error));
    if (info.enabled === false) {
      view.append(node("p", "wf-warning", "The collector is off (daemon.sysperf_interval_s is 0)."));
    }
    if (info.error) view.append(node("p", "wf-warning", `Last read failed: ${info.error}`));

    const pts = page.history;
    const pct = (v) => `${Math.round(v)}%`;
    view.append(
      lineChart({ title: "CPU", series: [{ label: "CPU", cls: "s-a", get: (p) => p.cpu_pct }], points: pts, yMax: 100, fmt: pct }),
      lineChart({ title: "Memory", series: [{ label: "Memory used", cls: "s-a", get: (p) => p.mem_pct }], points: pts, yMax: 100, fmt: pct }),
      lineChart({
        title: "Disk I/O",
        series: [
          { label: "Read", cls: "s-a", get: (p) => p.disk_read_bps },
          { label: "Write", cls: "s-b", get: (p) => p.disk_write_bps },
        ],
        points: pts,
        yMax: null,
        nice: niceBytes,
        fmt: (v) => fmtBytes(v, "/s"),
      }),
    );
    if (latest) {
      const card = node("section", "window-section sysperf-card");
      card.append(node("h3", "", "Now"), detailTable(latest));
      const disks = diskTable(latest.disks);
      if (disks) card.append(disks);
      view.append(card);
    }
    if (info.log && info.log.path) {
      const card = node("section", "window-section sysperf-card");
      card.append(node("h3", "", "Log"));
      const p = node("p", "wf-note");
      p.append(document.createTextNode("Every sample is one JSON line in "), node("code", "", info.log.path),
        document.createTextNode(`, rotated at ${fmtBytes(info.log.max_bytes)} with ${info.log.backups} old files kept (.1 … .${info.log.backups}).`));
      card.append(p);
      view.append(card);
    }
  }

  async function open() {
    page.open = true;
    page.error = "";
    render();
    try {
      const data = await fetchSysperf(true);
      page.info = data;
      page.max = data.history_max || page.max;
      page.history = (data.history || []).slice(-page.max);
    } catch (error) {
      page.error = error.message || String(error);
    }
    render();
  }

  function stop() {
    page.open = false;
    page.history = [];
  }

  globalThis.SysPerfPage = { open, stop, startBadge, stopBadge };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", startBadge);
  else startBadge();
})();
