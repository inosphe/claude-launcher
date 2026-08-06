/* claunch web UI: session list + live xterm.js terminal over WebSocket. */
"use strict";

const $ = (id) => document.getElementById(id);

let currentName = null;
let ws = null;
let term = null;
let fitAddon = null;
let sessionsCache = [];

/* ------------------------------------------------------------------ */
/* auth                                                               */
/* ------------------------------------------------------------------ */
async function api(path, opts = {}) {
  const resp = await fetch(path, { credentials: "same-origin", ...opts });
  if (resp.status === 401) {
    showAuth();
    throw new Error("unauthorized");
  }
  return resp;
}

function showAuth() {
  $("auth-overlay").classList.remove("hidden");
  $("auth-token").focus();
}

$("auth-submit").addEventListener("click", doAuth);
$("auth-token").addEventListener("keydown", (e) => {
  if (e.key === "Enter") doAuth();
});

async function doAuth() {
  const token = $("auth-token").value.trim();
  const resp = await fetch("/api/auth/session", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
  if (!resp.ok) {
    $("auth-error").classList.remove("hidden");
    return;
  }
  $("auth-error").classList.add("hidden");
  $("auth-overlay").classList.add("hidden");
  $("auth-token").value = "";
  boot();
}

/* ------------------------------------------------------------------ */
/* session list                                                       */
/* ------------------------------------------------------------------ */
async function refreshSessions() {
  let data;
  try {
    const resp = await api("/api/sessions");
    data = await resp.json();
  } catch {
    return;
  }
  sessionsCache = data.sessions || [];
  const list = $("session-list");
  list.innerHTML = "";
  for (const s of sessionsCache) {
    const li = document.createElement("li");
    li.dataset.name = s.name;
    if (s.name === currentName) li.classList.add("active");
    const dot = document.createElement("span");
    dot.className = `dot ${s.status}`;
    const label = document.createElement("span");
    label.textContent = s.name;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = s.status === "exited"
      ? `exit ${s.exit_code ?? "?"}`
      : (s.profile || s.harness);
    li.append(dot, label, meta);
    li.addEventListener("click", () => attach(s.name));
    list.appendChild(li);
  }
  if (currentName) {
    const cur = sessionsCache.find((s) => s.name === currentName);
    if (cur) setStatusBadge(cur.status);
  }
}

/* ------------------------------------------------------------------ */
/* cflow workflow monitoring                                          */
/* ------------------------------------------------------------------ */
function wfDotClass(status) {
  if (status === "step" || status === "select" || status === "reported") return "wf-running";
  if (status === "waiting_approval" || status === "waiting_selection" || status === "report_required") return "wf-waiting";
  if (status === "done") return "wf-done";
  if (status === "error" || status === "aborted") return "wf-error";
  return "wf-running";
}

function shortenPath(p) {
  const parts = (p || "").split(/[\\/]+/).filter(Boolean);
  return parts.length > 2 ? "…/" + parts.slice(-2).join("/") : p;
}

function cflowLine(text, cls) {
  const el = document.createElement("div");
  el.className = `cflow-line${cls ? " " + cls : ""}`;
  el.textContent = text;
  return el;
}

function cflowHint(cmd) {
  const el = document.createElement("code");
  el.className = "cflow-hint";
  el.textContent = cmd;
  return el;
}

async function refreshCflow() {
  let data;
  try {
    const resp = await api("/api/cflow");
    data = await resp.json();
  } catch {
    return;
  }
  const runs = data.runs || [];
  $("cflow-panel").classList.remove("hidden");
  const list = $("cflow-list");
  list.innerHTML = "";
  if (runs.length === 0) {
    const li = document.createElement("li");
    li.className = "cflow-empty";
    li.textContent = "no cflow runs — start one with /cflow in a session";
    list.appendChild(li);
    return;
  }
  for (const r of runs) {
    const li = document.createElement("li");

    const head = document.createElement("div");
    head.className = "cflow-head";
    const dot = document.createElement("span");
    dot.className = `dot ${wfDotClass(r.status)}`;
    const name = document.createElement("span");
    name.textContent = r.workflow || "(workflow)";
    const st = document.createElement("span");
    st.className = "meta";
    st.textContent =
      r.status === "waiting_approval" && r.reason === "loop_limit"
        ? "loop limit" : r.status;
    head.append(dot, name, st);
    li.appendChild(head);

    if (r.step_id) {
      const visit = r.visit > 1 ? ` · visit ${r.visit}` : "";
      li.appendChild(cflowLine(
        `step: ${r.title || r.step_id}${visit} · ${r.steps_completed ?? 0} done`
      ));
    }

    // Latest step reports: the agent's own account of each finished step
    // (plus the current step's filed-but-not-advanced report, if any).
    const reports = (r.reports || []).slice(-3);
    for (const rep of reports) {
      const line = cflowLine(`${rep.step}: ${rep.summary || ""}`, "report");
      if (rep.details) line.title = rep.details;
      li.appendChild(line);
    }

    const cwdLine = cflowLine(shortenPath(r.cwd), "dim");
    cwdLine.title = r.cwd;
    li.appendChild(cwdLine);
    li.classList.add("clickable");
    li.addEventListener("click", () => {
      location.hash = "#/wf/" + encodeURIComponent(r.cwd);
    });
    if ((r.sessions || []).length) {
      const sess = cflowLine(`session: ${r.sessions.join(", ")}`, "dim");
      sess.classList.add("linkish");
      sess.title = "attach the session's terminal";
      sess.addEventListener("click", (e) => {
        e.stopPropagation();
        attach(r.sessions[0]);
      });
      li.appendChild(sess);
    }

    if (r.status === "waiting_approval") {
      li.appendChild(cflowHint("claunch cflow approve"));
    } else if (r.status === "waiting_selection" || r.status === "select") {
      if (r.proposal) {
        li.appendChild(cflowLine(
          `agent proposes: ${r.proposal.option} — ${r.proposal.reason || ""}`
        ));
      }
      if (r.status === "waiting_selection" || r.chooser === "user") {
        const opts = (r.options || []).map((o) => o.name).join("|");
        li.appendChild(cflowHint(`claunch cflow select <${opts}>`));
      }
    } else if (r.status === "error") {
      li.appendChild(cflowLine(r.error || "error", "error"));
    }

    list.appendChild(li);
  }
}

async function refreshProfiles() {
  try {
    const resp = await api("/api/profiles");
    const data = await resp.json();
    const select = document.querySelector("#new-session select[name=profile]");
    select.innerHTML = "";
    for (const name of data.profiles || []) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      select.appendChild(opt);
    }
  } catch { /* ignore */ }
}

$("new-session").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = {
    name: f.name.value.trim(),
    harness: f.harness.value.trim() || "claude",
    profile: f.profile.value || null,
    cwd: f.cwd.value.trim(),
    args: f.args.value.trim() ? f.args.value.trim().split(/\s+/) : [],
  };
  const resp = await api("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const err = $("create-error");
  if (!resp.ok) {
    const doc = await resp.json().catch(() => ({}));
    err.textContent = doc.error || `HTTP ${resp.status}`;
    err.classList.remove("hidden");
    return;
  }
  err.classList.add("hidden");
  f.name.value = "";
  const info = await resp.json();
  await refreshSessions();
  attach(info.name);
});

$("term-kill").addEventListener("click", async () => {
  if (!currentName) return;
  await api(`/api/sessions/${encodeURIComponent(currentName)}`, { method: "DELETE" });
  refreshSessions();
});

/* ------------------------------------------------------------------ */
/* terminal attachment                                                */
/* ------------------------------------------------------------------ */
function setStatusBadge(status) {
  const badge = $("term-status");
  badge.textContent = status;
  badge.className = `badge ${status}`;
}

function detach() {
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
  if (term) { term.dispose(); term = null; fitAddon = null; }
}

function attach(name) {
  detach();
  currentName = name;
  stopWfPoll();
  if (location.hash.startsWith("#/wf/")) history.replaceState(null, "", "#");
  showView("terminal");
  $("term-title").textContent = name;
  document.querySelectorAll("#session-list li").forEach((li) =>
    li.classList.toggle("active", li.dataset.name === name)
  );

  term = new Terminal({
    fontFamily: "Cascadia Mono, Consolas, Menlo, monospace",
    fontSize: 13,
    theme: { background: "#14161a" },
    scrollback: 5000,
  });
  fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open($("terminal"));
  fitAddon.fit();

  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/api/sessions/${encodeURIComponent(name)}/ws`);
  ws.binaryType = "arraybuffer";

  const encoder = new TextEncoder();
  term.onData((data) => {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(encoder.encode(data));
  });
  term.onResize(({ cols, rows }) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "resize", cols, rows }));
    }
  });

  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "init") {
        term.resize(msg.cols, msg.rows);
        setStatusBadge(msg.status);
        // Adopt the viewer's size once attached.
        setTimeout(() => { fitAddon.fit(); }, 50);
      } else if (msg.type === "state") {
        setStatusBadge(msg.status);
      } else if (msg.type === "resize") {
        if (term.cols !== msg.cols || term.rows !== msg.rows) {
          term.resize(msg.cols, msg.rows);
        }
      } else if (msg.type === "exit") {
        setStatusBadge("exited");
        term.write(`\r\n\x1b[90m[session exited (code ${msg.code})]\x1b[0m\r\n`);
      }
    } else {
      term.write(new Uint8Array(ev.data));
    }
  };
  ws.onclose = () => {
    if (term) term.write("\r\n\x1b[90m[disconnected]\x1b[0m\r\n");
  };
}

window.addEventListener("resize", () => {
  if (fitAddon) fitAddon.fit();
});

/* ------------------------------------------------------------------ */
/* workflow detail page (#/wf/<cwd>) — diagram, reports, actions      */
/* ------------------------------------------------------------------ */
let wfCwd = null;
let wfPollTimer = null;

function showView(name) {
  const showTerm = name === "terminal";
  $("term-header").classList.toggle("hidden", !(showTerm && currentName));
  $("terminal").classList.toggle("hidden", !showTerm);
  $("wf-view").classList.toggle("hidden", name !== "wf");
  $("placeholder").classList.toggle("hidden", showTerm || name === "wf");
}

function stopWfPoll() {
  if (wfPollTimer) { clearInterval(wfPollTimer); wfPollTimer = null; }
  wfCwd = null;
}

async function openWorkflow(cwd) {
  if (wfPollTimer) clearInterval(wfPollTimer);
  wfCwd = cwd;
  showView("wf");
  $("wf-view").innerHTML = "<p class='wf-note'>loading…</p>";
  await refreshWf();
  wfPollTimer = setInterval(refreshWf, 2000);
}

async function refreshWf() {
  if (!wfCwd) return;
  let data;
  try {
    const resp = await api(`/api/cflow/run?cwd=${encodeURIComponent(wfCwd)}`);
    data = await resp.json();
    if (!resp.ok) {
      $("wf-view").innerHTML = "";
      $("wf-view").appendChild(el("p", "wf-warning", data.error || "cannot load run"));
      return;
    }
  } catch {
    return;
  }
  renderWf(data);
}

function route() {
  const h = location.hash || "";
  if (h.startsWith("#/wf/")) {
    openWorkflow(decodeURIComponent(h.slice(5)));
  } else {
    stopWfPoll();
    showView(currentName && term ? "terminal" : "placeholder");
  }
}
window.addEventListener("hashchange", route);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function cflowAction(path, body) {
  try {
    const resp = await api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const doc = await resp.json().catch(() => ({}));
      alert(doc.error || `HTTP ${resp.status}`);
    }
  } catch { /* auth overlay is up */ }
  refreshWf();
  refreshCflow();
}

function renderWf(data) {
  const view = $("wf-view");
  view.innerHTML = "";
  if (data.status === "idle") {
    view.appendChild(el("p", "wf-note", `no active cflow run in ${data.cwd}`));
    return;
  }
  const run = data.run || {};
  const wf = data.workflow || { steps: [] };

  const head = el("div", "wf-head");
  head.appendChild(el("h2", null, wf.name || run.workflow || "workflow"));
  head.appendChild(el(
    "span",
    `badge ${wfDotClass(run.status)}`,
    run.status === "waiting_approval" && run.reason === "loop_limit"
      ? "loop limit" : run.status
  ));
  view.appendChild(head);
  if (wf.description) view.appendChild(el("p", "wf-desc", wf.description));

  const meta = el("div", "wf-meta");
  meta.appendChild(el("span", null, `run ${run.run || "?"}`));
  meta.appendChild(el("span", null, `started ${(run.started_at || "?").replace("T", " ")}`));
  meta.appendChild(el("span", null, `${run.steps_completed ?? 0} steps done`));
  meta.appendChild(el("span", "mono", data.cwd));
  for (const s of data.sessions || []) {
    const link = el("a", "wf-session", `attach: ${s}`);
    link.href = "#";
    link.addEventListener("click", (e) => { e.preventDefault(); attach(s); });
    meta.appendChild(link);
  }
  view.appendChild(meta);
  if (run.context) view.appendChild(el("p", "wf-context", `context: ${run.context}`));
  for (const w of wf.warnings || []) {
    view.appendChild(el("p", "wf-warning", `⚠ ${w}`));
  }

  view.appendChild(wfActions(data));

  const cols = el("div", "wf-cols");
  const dia = el("div", "wf-diagram");
  dia.innerHTML = wfDiagramSvg(wf, run);
  cols.appendChild(dia);
  cols.appendChild(wfReports(data));
  view.appendChild(cols);

  const journal = document.createElement("details");
  journal.className = "wf-journal";
  journal.appendChild(el("summary", null, `journal (${(data.journal || []).length} events)`));
  for (const e of (data.journal || []).slice().reverse()) {
    const line =
      `${(e.at || "").replace("T", " ")}  ${e.event || ""}` +
      `${e.step ? "  " + e.step : ""}${e.option ? "  -> " + e.option : ""}`;
    journal.appendChild(el("div", "wf-journal-line mono", line));
  }
  view.appendChild(journal);
}

function wfActions(data) {
  const run = data.run || {};
  const box = el("div", "wf-actions");
  if (run.status === "waiting_approval") {
    const isLoop = run.reason === "loop_limit";
    box.appendChild(el("p", "wf-gate", run.gate || "waiting for approval"));
    const btn = el("button", "wf-btn approve", isLoop ? "Extend loop limit" : "Approve gate");
    btn.addEventListener("click", () => {
      const q = isLoop
        ? `Extend the loop limit at step '${run.step_id}'?`
        : `Approve the gate at step '${run.step_id}'?`;
      if (confirm(q)) cflowAction("/api/cflow/approve", { cwd: data.cwd });
    });
    box.appendChild(btn);
  } else if (run.status === "waiting_selection" || run.status === "select") {
    box.appendChild(el("p", "wf-gate", run.prompt || "decision point"));
    if (run.proposal) {
      box.appendChild(el(
        "p", "wf-proposal",
        `agent proposes: ${run.proposal.option} — ${run.proposal.reason || ""}`
      ));
    }
    if (run.status === "waiting_selection" || run.chooser === "user") {
      for (const o of run.options || []) {
        const btn = el("button", "wf-btn option", o.name);
        btn.title = o.description || "";
        btn.addEventListener("click", () => {
          if (confirm(`Select '${o.name}'?`)) {
            cflowAction("/api/cflow/select", { cwd: data.cwd, option: o.name });
          }
        });
        box.appendChild(btn);
      }
      box.appendChild(el("p", "wf-note", "confirming here unblocks the agent (nudge it to continue)"));
    } else {
      box.appendChild(el("p", "wf-note", "the agent decides this branch on its own"));
    }
  } else if (run.status === "done" || run.status === "aborted") {
    box.appendChild(el("p", "wf-note", `workflow ${run.status}`));
  } else {
    box.appendChild(el(
      "p", "wf-note",
      `agent is working on '${run.step_id}' — nothing needs a human right now`
    ));
  }
  return box;
}

function wfReports(data) {
  const box = el("div", "wf-reports");
  box.appendChild(el("h3", null, "Step reports"));
  const reports = (data.reports || []).slice().reverse();
  if (!reports.length) box.appendChild(el("p", "wf-note", "no reports yet"));
  for (const r of reports) {
    const card = el("div", "wf-report");
    const head = el("div", "wf-report-head");
    head.appendChild(el("span", "wf-report-step", r.visit > 1 ? `${r.step} ×${r.visit}` : r.step));
    head.appendChild(el("span", "wf-report-at", (r.at || "").replace("T", " ")));
    card.appendChild(head);
    card.appendChild(el("p", "wf-report-summary", r.summary || ""));
    if (r.details) card.appendChild(el("pre", "wf-report-details", r.details));
    box.appendChild(card);
  }
  return box;
}

/* SVG graph: BFS rows from start, forward edges on the right rail,
   back edges (cycles) on the left rail, select options as edge labels. */
function escXml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function wfDiagramSvg(wf, run) {
  const steps = wf.steps || [];
  const byId = {};
  for (const s of steps) byId[s.id] = s;
  const outsOf = (s) =>
    s.select ? s.select.options.map((o) => [o.next, o.name]) : [[s.next, null]];

  const order = [];
  const seen = new Set();
  const queue = [wf.start];
  while (queue.length) {
    const id = queue.shift();
    if (!id || seen.has(id) || !byId[id]) continue;
    seen.add(id);
    order.push(id);
    for (const [t] of outsOf(byId[id])) if (t && !seen.has(t)) queue.push(t);
  }
  for (const s of steps) if (!seen.has(s.id)) order.push(s.id);

  const rows = {};
  order.forEach((id, i) => { rows[id] = i; });

  const edges = [];
  let hasEnd = false;
  for (const s of steps) {
    outsOf(s).forEach(([t, label], i) => {
      if (t) edges.push({ from: s.id, to: t, label, i });
      else { hasEnd = true; edges.push({ from: s.id, to: "end", label, i }); }
    });
  }

  const NW = 210, NH = 44, ROWH = 82, W = 480;
  const NX = (W - NW) / 2;
  const endRow = order.length;
  const H = (endRow + (hasEnd ? 1 : 0)) * ROWH + 10;
  const rowOf = (id) => (id === "end" ? endRow : rows[id]);
  const yTop = (id) => 8 + rowOf(id) * ROWH;

  const parts = [];
  parts.push(`<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" class="wfd">`);
  parts.push(
    '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" ' +
    'markerWidth="7" markerHeight="7" orient="auto-start-reverse">' +
    '<path d="M 0 0 L 10 5 L 0 10 z" fill="#4d5566"/></marker></defs>'
  );

  for (const e of edges) {
    const r1 = rowOf(e.from), r2 = rowOf(e.to);
    let d, lx, ly, anchor = "start";
    if (r2 === r1 + 1) {
      const x = W / 2 + (e.i ? (e.i % 2 ? -1 : 1) * 18 * Math.ceil(e.i / 2) : 0);
      const y1 = yTop(e.from) + NH, y2 = yTop(e.to) - 2;
      d = `M ${x} ${y1} L ${x} ${y2}`;
      lx = x + 7; ly = (y1 + y2) / 2 + 4;
    } else if (r2 > r1) {
      const y1 = yTop(e.from) + NH / 2, y2 = yTop(e.to) + 8;
      const b = NX + NW + 30 + 16 * (e.i || 0);
      d = `M ${NX + NW} ${y1} C ${b} ${y1}, ${b} ${y2}, ${NX + NW + 2} ${y2}`;
      lx = NX + NW + 8; ly = y1 - 8;
    } else {
      const y1 = yTop(e.from) + NH / 2, y2 = yTop(e.to) + NH / 2;
      const b = NX - 30 - 16 * (e.i || 0);
      d = `M ${NX} ${y1} C ${b} ${y1}, ${b} ${y2}, ${NX - 2} ${y2}`;
      lx = NX - 8; ly = (y1 + y2) / 2 + 4; anchor = "end";
    }
    parts.push(`<path class="wfd-edge" d="${d}" marker-end="url(#arrow)"/>`);
    if (e.label) {
      parts.push(
        `<text class="wfd-elabel" x="${lx}" y="${ly}" text-anchor="${anchor}">` +
        `${escXml(e.label)}</text>`
      );
    }
  }

  const visits = (run && run.visits) || {};
  for (const id of order) {
    const s = byId[id];
    const y = yTop(id);
    const cls = ["wfd-node"];
    const active = run && run.step_id === id
      && run.status !== "done" && run.status !== "aborted";
    if (active) cls.push("current");
    else if (visits[id]) cls.push("visited");
    const flags = [];
    if (s.gate) flags.push("gate");
    if (s.verify) flags.push("verify");
    if (s.select) flags.push(`select:${s.select.chooser}`);
    const title = s.title && s.title !== s.id ? `${s.id} — ${s.title}` : s.id;
    parts.push(`<g class="${cls.join(" ")}">`);
    parts.push(`<rect x="${NX}" y="${y}" width="${NW}" height="${NH}" rx="8"/>`);
    parts.push(
      `<text class="wfd-title" x="${NX + 12}" y="${y + (flags.length ? 19 : 27)}">` +
      `${escXml(title)}</text>`
    );
    if (flags.length) {
      parts.push(
        `<text class="wfd-flags" x="${NX + 12}" y="${y + 35}">` +
        `${escXml(flags.join(" · "))}</text>`
      );
    }
    if (visits[id] > 1) {
      parts.push(
        `<text class="wfd-visits" x="${NX + NW - 12}" y="${y + 19}" text-anchor="end">` +
        `×${visits[id]}</text>`
      );
    }
    parts.push("</g>");
  }
  if (hasEnd) {
    const y = yTop("end");
    parts.push(
      `<g class="wfd-node end"><rect x="${(W - 90) / 2}" y="${y}" width="90" height="30" rx="15"/>` +
      `<text class="wfd-title" x="${W / 2}" y="${y + 20}" text-anchor="middle">end</text></g>`
    );
  }
  parts.push("</svg>");
  return parts.join("");
}

/* ------------------------------------------------------------------ */
/* boot                                                               */
/* ------------------------------------------------------------------ */
let pollTimer = null;
async function boot() {
  try {
    const resp = await api("/api/daemon");
    const info = await resp.json();
    $("daemon-info").textContent = `v${info.version}`;
  } catch {
    return; // auth overlay is up
  }
  refreshProfiles();
  refreshSessions();
  refreshCflow();
  route();
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => { refreshSessions(); refreshCflow(); }, 2000);
}

boot();
