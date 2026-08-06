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
  $("cflow-panel").classList.toggle("hidden", runs.length === 0);
  const list = $("cflow-list");
  list.innerHTML = "";
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
    if ((r.sessions || []).length) {
      li.appendChild(cflowLine(`session: ${r.sessions.join(", ")}`, "dim"));
      li.classList.add("clickable");
      li.addEventListener("click", () => attach(r.sessions[0]));
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
  $("placeholder").classList.add("hidden");
  $("term-header").classList.remove("hidden");
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
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => { refreshSessions(); refreshCflow(); }, 2000);
}

boot();
