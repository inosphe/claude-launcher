/* claunch web UI: session list + live xterm.js terminal over WebSocket. */
"use strict";

const $ = (id) => document.getElementById(id);

let currentName = null;
let ws = null;
let term = null;
let fitAddon = null;
let sessionsCache = [];
let attachedPid = null;           // pid of the incarnation this socket is bound to
let applyingRemoteResize = false; // guards against echoing a server-driven resize
let fitTimer = null;              // debounces viewport-driven fit() calls

/* Base path of the current page: "/" when served directly, "/t/<name>/" when
 * reached through a relay tunnel. All API/WS/static requests are resolved
 * against it so the same assets work in both cases. */
const BASE = location.pathname.replace(/[^/]*$/, "");
function url(path) {
  return BASE + String(path).replace(/^\//, "");
}

/* ------------------------------------------------------------------ */
/* auth                                                               */
/* ------------------------------------------------------------------ */
/* The login cookie lives in the daemon's memory, so every daemon restart
   invalidates it. The pasted token itself stays valid until rotated —
   remember it in localStorage and re-login transparently on 401, so the
   paste-the-token prompt only ever shows for a fresh browser or after
   `claunch daemon token --rotate`. */
const TOKEN_KEY = "claunch_token";
let reloginPromise = null;

async function tryStoredLogin() {
  const token = localStorage.getItem(TOKEN_KEY);
  if (!token) return false;
  const resp = await fetch(url("/api/auth/session"), {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
  if (resp.ok) return true;
  if (resp.status === 401) localStorage.removeItem(TOKEN_KEY); // rotated
  return false;
}

function relogin() {
  // Memoized: concurrent 401s (session + cflow polls) share one attempt.
  if (!reloginPromise) {
    reloginPromise = tryStoredLogin().finally(() => { reloginPromise = null; });
  }
  return reloginPromise;
}

async function api(path, opts = {}) {
  let resp = await fetch(url(path), { credentials: "same-origin", ...opts });
  if (resp.status === 401) {
    if (await relogin()) {
      resp = await fetch(url(path), { credentials: "same-origin", ...opts });
      if (resp.status !== 401) return resp;
    }
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
  const resp = await fetch(url("/api/auth/session"), {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
  if (!resp.ok) {
    $("auth-error").classList.remove("hidden");
    return;
  }
  localStorage.setItem(TOKEN_KEY, token); // survive daemon restarts
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
    if (s.status === "exited") li.title = "exited — open it to resume";
    li.append(dot, label, meta);
    li.addEventListener("click", () => attach(s.name));
    list.appendChild(li);
  }
  // Exited sessions are kept indefinitely so they stay resumable; dropping
  // them is the user's call, in bulk, from here.
  const dead = sessionsCache.filter((s) => s.status === "exited");
  const clear = $("clear-exited");
  clear.classList.toggle("hidden", dead.length === 0);
  clear.textContent = `clear ${dead.length} exited`;

  const cur = currentName && sessionsCache.find((s) => s.name === currentName);
  if (cur && attachedPid && cur.pid !== attachedPid) {
    // Someone else (a `claunch respawn`, another tab) resumed this session:
    // our socket is bound to the replaced, now-dead child, so follow the new
    // one instead of showing its frozen last screen. Only while the terminal
    // is the visible view — reattaching must not yank the user off a
    // workflow page (it stays pending until they come back).
    if (!$("terminal").classList.contains("hidden")) attach(currentName);
  } else if (cur && !(ws && ws.readyState === WebSocket.OPEN)) {
    // Otherwise an open socket stays authoritative: it sees this session's
    // every state change first-hand (an exit reaches it seconds before the
    // next poll), so a stale entry can't flicker the resume/kill controls.
    setStatusBadge(cur.status);
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
    head.append(dot, name);
    if (r.scope && r.scope !== "default") {
      const scope = document.createElement("span");
      scope.className = "cflow-scope";
      scope.textContent = r.scope;
      head.appendChild(scope);
    }
    head.appendChild(st);
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
      location.hash = "#/wf/" + encodeURIComponent(`${r.scope || "default"}|${r.cwd}`);
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
  const name = currentName;
  // On an exited session DELETE deregisters the record rather than killing
  // anything — that is the one path that makes the session unresumable, so
  // it asks first (killing a live program keeps its existing behaviour).
  const exited = $("term-status").textContent === "exited";
  if (exited && !confirm(
    `Remove exited session '${name}'?\n\nThe daemon forgets it, so it can no ` +
    `longer be resumed from here.`
  )) return;
  await api(`/api/sessions/${encodeURIComponent(name)}`, { method: "DELETE" });
  if (exited) {
    detach();
    currentName = null;
    showView("placeholder");
  }
  refreshSessions();
});

$("clear-exited").addEventListener("click", async () => {
  const dead = sessionsCache.filter((s) => s.status === "exited").map((s) => s.name);
  if (!dead.length) return;
  if (!confirm(
    `Drop the records of ${dead.length} exited session(s)?\n\n${dead.join(", ")}\n\n` +
    `They can no longer be resumed. Running sessions are untouched.`
  )) return;
  await api("/api/sessions", { method: "DELETE" });
  if (currentName && dead.includes(currentName)) {
    detach();
    currentName = null;
    showView("placeholder");
  }
  refreshSessions();
});

/* Resume: relaunch an exited session under its own name and definition. The
   claude harness comes back with `--resume` of the conversation pinned at
   creation, so quitting it by accident (double Ctrl+C) is recoverable from the
   browser too, not just via `claunch respawn`. */
$("term-resume").addEventListener("click", async () => {
  if (!currentName) return;
  const name = currentName;
  const btn = $("term-resume");
  btn.disabled = true;
  try {
    const resp = await api(
      `/api/sessions/${encodeURIComponent(name)}/respawn`, { method: "POST" }
    );
    const info = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      alert(info.error || `HTTP ${resp.status}`);
      return;
    }
    // The old child (and this tab's socket with it) is gone: the respawn
    // spawned a fresh PTY under the same name, so reattach to it. Detaching
    // first drops the stale pid, so the poll leaves the reattach to us.
    detach();
    await refreshSessions();
    attach(info.name || name);
  } catch { /* auth overlay is up */ }
  finally { btn.disabled = false; }
});

/* ------------------------------------------------------------------ */
/* terminal attachment                                                */
/* ------------------------------------------------------------------ */
function setStatusBadge(status) {
  const badge = $("term-status");
  badge.textContent = status;
  badge.className = `badge ${status}`;
  // An exited session is revivable, not attachable — offer resume, and make
  // kill what it actually is there: dropping the daemon's record of it.
  const exited = status === "exited";
  $("term-resume").classList.toggle("hidden", !exited);
  const kill = $("term-kill");
  kill.textContent = exited ? "remove" : "kill";
  kill.title = exited
    ? "forget this exited session (it can no longer be resumed here)"
    : "terminate the program running in this session";
}

function detach() {
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
  if (term) { term.dispose(); term = null; fitAddon = null; }
  attachedPid = null; // re-learned from the next socket's init frame
}

function attach(name) {
  detach();
  currentName = name;
  stopWfPoll();
  stopMeshPoll();
  if (location.hash.startsWith("#/wf/") || location.hash.startsWith("#/mesh/")) {
    history.replaceState(null, "", "#");
  }
  showView("terminal");
  $("term-title").textContent = name;
  // Seed the header from the list until the socket's `init` says otherwise,
  // so the previous session's controls never linger on this one.
  setStatusBadge((sessionsCache.find((s) => s.name === name) || {}).status || "starting");
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
  ws = new WebSocket(
    `${proto}://${location.host}${url(`/api/sessions/${encodeURIComponent(name)}/ws`)}`
  );
  ws.binaryType = "arraybuffer";

  const encoder = new TextEncoder();
  term.onData((data) => {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(encoder.encode(data));
  });
  term.onResize(({ cols, rows }) => {
    // A resize we applied from a server broadcast must not be echoed back, or
    // two viewers (or a stale echo over a high-latency relay) ping-pong forever.
    if (applyingRemoteResize) return;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "resize", cols, rows }));
    }
  });

  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "init") {
        // Seed the grid with the session's current size without echoing it back;
        // the fit() below sends this viewer's own size as the single resize.
        applyingRemoteResize = true;
        try { term.resize(msg.cols, msg.rows); }
        finally { applyingRemoteResize = false; }
        attachedPid = msg.pid || null;
        setStatusBadge(msg.status);
        // Adopt the viewer's size once attached.
        setTimeout(() => { if (fitAddon) fitAddon.fit(); }, 50);
      } else if (msg.type === "state") {
        setStatusBadge(msg.status);
      } else if (msg.type === "resize") {
        // Only a background viewer adopts another viewer's size; a visible
        // viewer's own fit stays authoritative. Otherwise a stale echo arriving
        // late over the relay fights the local fit and the grid churns. On focus
        // regain, resyncTerminal() re-asserts this viewer's size.
        if (document.hidden && (term.cols !== msg.cols || term.rows !== msg.rows)) {
          applyingRemoteResize = true;
          try { term.resize(msg.cols, msg.rows); }
          finally { applyingRemoteResize = false; }
        }
      } else if (msg.type === "exit") {
        setStatusBadge("exited");
        term.write(
          `\r\n\x1b[90m[session exited (code ${msg.code})] ` +
          `- press "resume" above to relaunch it\x1b[0m\r\n`
        );
      }
    } else {
      term.write(new Uint8Array(ev.data));
    }
  };
  ws.onclose = () => {
    if (term) term.write("\r\n\x1b[90m[disconnected]\x1b[0m\r\n");
  };
}

// Debounce viewport-driven fits. On a phone the visual viewport jitters (URL
// bar collapsing, keyboard) and firing fit() on every event floods the session
// with resizes — most visible over a relay, where each round-trip lags.
window.addEventListener("resize", () => {
  if (!fitAddon) return;
  clearTimeout(fitTimer);
  fitTimer = setTimeout(() => { if (fitAddon) fitAddon.fit(); }, 150);
});

/* Another viewer (e.g. `claunch attach`) may have resized the session while
   this tab was in the background, leaving the grid garbled. On focus regain,
   re-assert this viewer's size and ask the daemon for a fresh repaint —
   event-driven only, no polling. */
function resyncTerminal() {
  if (!term || !ws || ws.readyState !== WebSocket.OPEN) return;
  fitAddon.fit(); // fires term.onResize -> server resize when dims changed
  ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
  ws.send(JSON.stringify({ type: "repaint" }));
}
window.addEventListener("focus", resyncTerminal);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) resyncTerminal();
});

/* ------------------------------------------------------------------ */
/* workflow detail page (#/wf/<cwd>) — diagram, reports, actions      */
/* ------------------------------------------------------------------ */
let wfCwd = null;
let wfScope = "default";
let wfPollTimer = null;
let wfSelectedStep = null; // node picked in the diagram (null = show all)
let wfLastData = null;     // last payload, for instant re-render on selection

function showView(name) {
  const showTerm = name === "terminal";
  $("term-header").classList.toggle("hidden", !(showTerm && currentName));
  $("terminal").classList.toggle("hidden", !showTerm);
  $("wf-view").classList.toggle("hidden", name !== "wf");
  $("mesh-view").classList.toggle("hidden", name !== "mesh");
  $("placeholder").classList.toggle(
    "hidden", showTerm || name === "wf" || name === "mesh"
  );
}

function stopWfPoll() {
  if (wfPollTimer) { clearInterval(wfPollTimer); wfPollTimer = null; }
  wfCwd = null;
}

async function openWorkflow(cwd, scope) {
  if (wfPollTimer) clearInterval(wfPollTimer);
  wfCwd = cwd;
  wfScope = scope || "default";
  wfSelectedStep = null;
  wfLastData = null;
  showView("wf");
  $("wf-view").innerHTML = "<p class='wf-note'>loading…</p>";
  await refreshWf();
  wfPollTimer = setInterval(refreshWf, 2000);
}

async function refreshWf() {
  if (!wfCwd) return;
  let data;
  try {
    const resp = await api(
      `/api/cflow/run?cwd=${encodeURIComponent(wfCwd)}&scope=${encodeURIComponent(wfScope)}`
    );
    data = await resp.json();
    if (!resp.ok) {
      $("wf-view").innerHTML = "";
      $("wf-view").appendChild(el("p", "wf-warning", data.error || "cannot load run"));
      return;
    }
  } catch {
    return;
  }
  wfLastData = data;
  renderWf(data);
}

function selectWfStep(step) {
  wfSelectedStep = step;
  if (wfLastData) renderWf(wfLastData);
}

function route() {
  const h = location.hash || "";
  if (h.startsWith("#/wf/")) {
    stopMeshPoll();
    const token = decodeURIComponent(h.slice(5));
    const sep = token.indexOf("|");
    if (sep >= 0) openWorkflow(token.slice(sep + 1), token.slice(0, sep));
    else openWorkflow(token, "default"); // pre-scope links
  } else if (h.startsWith("#/mesh/")) {
    stopWfPoll();
    openMesh(decodeURIComponent(h.slice(7)));
  } else {
    stopWfPoll();
    stopMeshPoll();
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
  if (data.status === "idle") {
    // Built once and left alone: the 2s poll must not wipe the user's
    // in-progress picker/context input.
    if (!view.querySelector(".wf-start")) renderWfIdle(view, data);
    return;
  }
  view.innerHTML = "";
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
  if (data.scope && data.scope !== "default") {
    meta.appendChild(el("span", "cflow-scope", `session ${data.scope}`));
  }
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

  // drop a stale selection if the workflow changed under us
  if (
    wfSelectedStep && wfSelectedStep !== "end" &&
    !(wf.steps || []).some((s) => s.id === wfSelectedStep)
  ) {
    wfSelectedStep = null;
  }

  const cols = el("div", "wf-cols");
  const dia = el("div", "wf-diagram");
  dia.innerHTML = wfDiagramSvg(wf, run, wfSelectedStep);
  const finished = run.status === "done" || run.status === "aborted";
  dia.querySelectorAll("g.wfd-node[data-step]").forEach((g) => {
    g.addEventListener("click", () => {
      const step = g.dataset.step;
      selectWfStep(wfSelectedStep === step ? null : step); // click again = clear
    });
  });
  cols.appendChild(dia);
  dia.appendChild(el(
    "p", "wf-note",
    "click a step to inspect its reports (click again to clear)"
  ));

  const forceBtn = el(
    "button", "wf-btn force",
    !wfSelectedStep
      ? "Force set state (select a step first)"
      : wfSelectedStep === "end"
        ? "Force-finish this run"
        : `Force run to '${wfSelectedStep}'`
  );
  forceBtn.disabled = !wfSelectedStep;
  if (wfSelectedStep) {
    const step = wfSelectedStep;
    forceBtn.addEventListener("click", () => {
      const q = step === "end"
        ? "Force-FINISH this workflow run?"
        : `Force the run's current step to '${step}'?` +
          (finished ? " (this reopens the finished run)" : "") +
          " The session will be nudged to continue from there.";
      if (confirm(q)) {
        cflowAction("/api/cflow/goto", { cwd: data.cwd, scope: data.scope, step });
      }
    });
  }
  dia.appendChild(forceBtn);

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
      if (confirm(q)) {
        cflowAction("/api/cflow/approve", { cwd: data.cwd, scope: data.scope });
      }
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
            cflowAction("/api/cflow/select", {
              cwd: data.cwd, scope: data.scope, option: o.name,
            });
          }
        });
        box.appendChild(btn);
      }
      box.appendChild(el("p", "wf-note",
        "confirming unblocks the agent; its managed session is nudged automatically"));
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
  if (run.status !== "done" && run.status !== "aborted") {
    const btn = el("button", "wf-btn nudge", "Nudge session");
    if ((data.sessions || []).length) {
      const msg = data.nudge_message || "cflow: continue per the /cflow protocol";
      const targets = (data.sessions || []).join(", ");
      btn.title = `type "${msg}" + Enter into session ${targets}`;
      btn.addEventListener("click", () => {
        if (confirm(
          `Nudge session '${targets}'?\n\nThis types the following line ` +
          `into its terminal and presses Enter:\n\n    ${msg}`
        )) {
          nudgeRun(data.cwd, data.scope);
        }
      });
    } else {
      btn.disabled = true;
      btn.title = "nothing to nudge: this run has no live session of its own";
      box.appendChild(el(
        "p", "wf-note",
        "this run is not bound to a live managed session — nudge the agent wherever it runs"
      ));
    }
    box.appendChild(btn);
  }

  const finished = run.status === "done" || run.status === "aborted";
  const arch = el(
    "button", "wf-btn archive",
    finished ? "Archive run" : "Abort & archive run"
  );
  arch.title =
    "move this run's state and journal into .cflow archive, " +
    "freeing the slot for a new workflow";
  arch.addEventListener("click", () => {
    const q = finished
      ? "Archive this finished run?\n\nIts state and journal move into " +
        ".cflow archive; a new workflow can then be started here."
      : "This run is still ACTIVE.\n\nAbort it and archive its state and " +
        "journal? The agent driving it loses the run.";
    if (confirm(q)) {
      cflowAction("/api/cflow/archive", { cwd: data.cwd, scope: data.scope });
    }
  });
  box.appendChild(arch);
  return box;
}

/* Idle (cwd, scope): offer to start a new run. Starting nudges the scope's
   session so its agent picks the workflow up per the /cflow protocol. */
async function renderWfIdle(view, data) {
  view.innerHTML = "";
  view.appendChild(el("p", "wf-note", `no active cflow run in ${data.cwd}`));
  const box = el("div", "wf-start");
  view.appendChild(box); // present immediately so the poll doesn't rebuild
  const cwd = data.cwd;
  const scope = data.scope || "default";
  box.appendChild(el("h3", null,
    scope !== "default" ? `Start a workflow — session ${scope}` : "Start a workflow"));

  let flows = [];
  try {
    const resp = await api(`/api/cflow/workflows?cwd=${encodeURIComponent(cwd)}`);
    flows = ((await resp.json()).workflows || []).filter((w) => !w.error);
  } catch { return; }
  if (wfCwd !== cwd) return; // navigated away while loading
  if (!flows.length) {
    box.appendChild(el(
      "p", "wf-note",
      "no workflows found — add one under .claunch/workflows/ " +
      "or scaffold with 'claunch cflow example'"
    ));
    return;
  }

  const sel = document.createElement("select");
  sel.className = "wf-start-select";
  for (const w of flows) {
    const opt = document.createElement("option");
    opt.value = w.name;
    opt.textContent = w.description ? `${w.name} — ${w.description}` : w.name;
    sel.appendChild(opt);
  }
  const ctx = document.createElement("input");
  ctx.type = "text";
  ctx.className = "wf-start-context";
  ctx.placeholder = "context for the run (optional)";
  const btn = el("button", "wf-btn approve", "Start");
  btn.addEventListener("click", async () => {
    const workflow = sel.value;
    const who = scope !== "default" ? ` for session '${scope}'` : "";
    if (!confirm(`Start workflow '${workflow}'${who}?`)) return;
    try {
      const resp = await api("/api/cflow/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cwd, scope, workflow, context: ctx.value.trim() }),
      });
      const doc = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        alert(doc.error || `HTTP ${resp.status}`);
        return;
      }
      if (!(doc.nudged_sessions || []).length) {
        alert(
          "run started, but no live session was nudged — tell the agent " +
          "to continue (it picks the run up via the /cflow protocol)"
        );
      }
    } catch { return; }
    refreshWf();
    refreshCflow();
  });
  const row = el("div", "wf-start-row");
  row.append(sel, ctx, btn);
  box.appendChild(row);
  box.appendChild(el(
    "p", "wf-note",
    "starting nudges this run's session so its agent picks the workflow up"
  ));
}

async function nudgeRun(cwd, scope) {
  let doc = {};
  try {
    const resp = await api("/api/cflow/nudge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cwd, scope }),
    });
    doc = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      alert(doc.error || `HTTP ${resp.status}`);
      return;
    }
  } catch {
    return;
  }
  if (!(doc.nudged_sessions || []).length) {
    alert("no live session in this run's directory to nudge");
  }
}

function wfReports(data) {
  const box = el("div", "wf-reports");
  const head = el("div", "wf-reports-head");
  head.appendChild(el("h3", null,
    wfSelectedStep ? `Step reports — ${wfSelectedStep}` : "Step reports"));
  if (wfSelectedStep) {
    const clear = el("button", "wf-btn clear", "Show all");
    clear.title = "clear the step selection and expand every report";
    clear.addEventListener("click", () => selectWfStep(null));
    head.appendChild(clear);
  }
  box.appendChild(head);

  if (wfSelectedStep && wfSelectedStep !== "end") {
    const step = ((data.workflow || {}).steps || [])
      .find((s) => s.id === wfSelectedStep);
    if (step && (step.instructions || step.select || step.gate)) {
      const inst = el("div", "wf-instructions");
      inst.appendChild(el("h4", null, "Instructions"));
      if (step.gate) inst.appendChild(el("p", "wf-instructions-gate", `gate: ${step.gate}`));
      if (step.instructions) {
        inst.appendChild(el("pre", "wf-instructions-text", step.instructions.trimEnd()));
      }
      if (step.select) {
        inst.appendChild(el(
          "p", "wf-instructions-select",
          `select (${step.select.chooser}): ${step.select.prompt.trim()}`
        ));
      }
      if (step.verify) {
        inst.appendChild(el("p", "wf-instructions-verify mono", `verify: ${step.verify}`));
      }
      box.appendChild(inst);
    }
  }

  const reports = (data.reports || []).slice(); // journal order: oldest first
  if (!reports.length) box.appendChild(el("p", "wf-note", "no reports yet"));
  if (wfSelectedStep && reports.length &&
      !reports.some((r) => r.step === wfSelectedStep)) {
    box.appendChild(el("p", "wf-note", `no reports for '${wfSelectedStep}' yet`));
  }
  for (const r of reports) {
    const expanded = !wfSelectedStep || r.step === wfSelectedStep;
    const card = el("div", expanded ? "wf-report" : "wf-report folded");
    const rhead = el("div", "wf-report-head");
    rhead.appendChild(el("span", "wf-report-step", r.visit > 1 ? `${r.step} ×${r.visit}` : r.step));
    rhead.appendChild(el("span", "wf-report-at", (r.at || "").replace("T", " ")));
    card.appendChild(rhead);
    if (expanded) {
      card.appendChild(el("p", "wf-report-summary", r.summary || ""));
      if (r.details) card.appendChild(el("pre", "wf-report-details", r.details));
    } else {
      card.title = `show reports for '${r.step}'`;
      card.addEventListener("click", () => selectWfStep(r.step));
    }
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

function wfDiagramSvg(wf, run, selected) {
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
    if (id === selected) cls.push("selected");
    const flags = [];
    if (s.gate) flags.push("gate");
    if (s.verify) flags.push("verify");
    if (s.select) flags.push(`select:${s.select.chooser}`);
    const title = s.title && s.title !== s.id ? `${s.id} — ${s.title}` : s.id;
    parts.push(`<g class="${cls.join(" ")}" data-step="${escXml(s.id)}">`);
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
    const endCls = selected === "end" ? "wfd-node end selected" : "wfd-node end";
    parts.push(
      `<g class="${endCls}" data-step="end"><rect x="${(W - 90) / 2}" y="${y}" width="90" height="30" rx="15"/>` +
      `<text class="wfd-title" x="${W / 2}" y="${y + 20}" text-anchor="middle">end</text></g>`
    );
  }
  parts.push("</svg>");
  return parts.join("");
}

/* ------------------------------------------------------------------ */
/* mesh: group sessions, message between them                         */
/* ------------------------------------------------------------------ */
let meshName = null;      // mesh open in the detail view
let meshPollTimer = null;
let meshCache = [];       // sidebar list payload
let meshInviteCodes = {}; // mesh -> last minted invite code (survives rerenders)

/* Relay connectivity is surfaced permanently in the header: mesh can only
   span machines while the uplink is registered, so the state must never be
   more than one glance away. */
function renderRelayBadge(relay) {
  const badge = $("relay-badge");
  if (!relay) return;
  badge.classList.remove("hidden");
  if (!relay.configured) {
    badge.textContent = "relay: off";
    badge.className = "badge relay-off";
    badge.title = "no relay uplink configured — sessions and mesh are local to this machine";
  } else if (relay.connected) {
    badge.textContent = `relay: ${relay.name}`;
    badge.className = "badge relay-on";
    badge.title = `connected to ${relay.url || "the relay"} as '${relay.name}'`;
  } else {
    badge.textContent = "relay: down";
    badge.className = "badge relay-down";
    badge.title = `uplink to ${relay.url || "the relay"} is disconnected — remote machines unreachable`;
  }
}

async function refreshMeshList() {
  let data;
  try {
    const resp = await api("/api/mesh");
    data = await resp.json();
  } catch {
    return;
  }
  renderRelayBadge(data.relay);
  meshCache = data.meshes || [];
  const list = $("mesh-list");
  list.innerHTML = "";
  if (!meshCache.length) {
    const li = el("li", "mesh-empty", "no meshes — create one below");
    list.appendChild(li);
  }
  for (const m of meshCache) {
    const li = document.createElement("li");
    li.className = "clickable";
    if (m.name === meshName) li.classList.add("active");
    const label = el("span", null, m.name);
    li.appendChild(label);
    // a mirror is somebody else's mesh: say so before the counts, since what
    // you can do here (no invites, no policy edits) depends on it
    if (m.primary) li.appendChild(el("span", "mesh-tag", `mirror · ${m.primary}`));
    const inbound = (m.requests || []).length;
    if (inbound) {
      const req = el("span", "mesh-tag", `${inbound} join req`);
      req.style.background = "#0d2818";
      req.style.color = "#3fb950";
      req.style.borderColor = "#1b4522";
      li.appendChild(req);
    }
    li.appendChild(el(
      "span", "meta",
      `${m.members.length} member${m.members.length === 1 ? "" : "s"} · ${m.messages} msg`
    ));
    li.addEventListener("click", () => {
      location.hash = "#/mesh/" + encodeURIComponent(m.name);
    });
    list.appendChild(li);
  }
  renderOutgoingJoins(data.outgoing || []);
}

/* Our own join requests still waiting on another machine's operator. They
   live outside any mesh (nothing is mounted locally until the grant lands),
   so the sidebar is the only place they can be seen. */
function renderOutgoingJoins(outgoing) {
  const box = $("mesh-outgoing");
  box.innerHTML = "";
  box.classList.toggle("hidden", !outgoing.length);
  for (const r of outgoing) {
    const li = document.createElement("li");
    li.append(
      el("span", null, `${r.mesh}@${r.primary}`),
      el("span", "meta", `awaiting approval as '${r.handle}'`)
    );
    const cancel = el("button", "mesh-kick", "×");
    cancel.title = "forget this request locally (the owner still sees it)";
    cancel.addEventListener("click", async () => {
      await api(`/api/mesh/outgoing/${encodeURIComponent(r.request_id)}`,
                { method: "DELETE" });
      refreshMeshList();
    });
    li.appendChild(cancel);
    box.appendChild(li);
  }
}

/* One field, two verbs: a bare name creates a mesh here, 'mesh@machine'
   asks that machine's daemon to admit one of our sessions. */
$("new-mesh").querySelector("input[name=name]").addEventListener("input", (e) => {
  const joining = e.target.value.includes("@");
  $("mesh-join-extra").classList.toggle("hidden", !joining);
  $("new-mesh").querySelector("button").textContent = joining ? "Join" : "Create";
  if (!joining) return;
  const sel = $("mesh-join-session");
  const keep = sel.value;
  sel.innerHTML = "";
  for (const s of sessionsCache.filter((s) => s.status !== "exited")) {
    const opt = document.createElement("option");
    opt.value = s.name;
    opt.textContent = s.name;
    sel.appendChild(opt);
  }
  if (keep) sel.value = keep;
});

$("new-mesh").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const name = f.name.value.trim();
  if (!name) return;
  const err = $("mesh-error");
  const fail = async (resp) => {
    const doc = await resp.json().catch(() => ({}));
    err.textContent = doc.error || `HTTP ${resp.status}`;
    err.classList.remove("hidden");
  };
  if (name.includes("@")) {
    const session = $("mesh-join-session").value;
    if (!session) {
      err.textContent = "no live session to enrol";
      err.classList.remove("hidden");
      return;
    }
    const resp = await api(`/api/mesh/${encodeURIComponent(name)}/members`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session,
        handle: $("mesh-join-handle").value.trim(),
        code: $("mesh-join-code").value.trim(),
      }),
    });
    if (!resp.ok) return fail(resp);
    const doc = await resp.json().catch(() => ({}));
    err.classList.add("hidden");
    f.name.value = "";
    $("mesh-join-handle").value = "";
    $("mesh-join-code").value = "";
    $("mesh-join-extra").classList.add("hidden");
    f.querySelector("button").textContent = "Create";
    await refreshMeshList();
    if (!doc.pending) location.hash = "#/mesh/" + encodeURIComponent(name.split("@")[0]);
    return;
  }
  const resp = await api("/api/mesh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!resp.ok) return fail(resp);
  err.classList.add("hidden");
  f.name.value = "";
  await refreshMeshList();
  location.hash = "#/mesh/" + encodeURIComponent(name);
});

function stopMeshPoll() {
  if (meshPollTimer) { clearInterval(meshPollTimer); meshPollTimer = null; }
  meshName = null;
}

async function openMesh(name) {
  if (meshPollTimer) clearInterval(meshPollTimer);
  meshName = name;
  showView("mesh");
  $("mesh-view").innerHTML = "<p class='wf-note'>loading…</p>";
  await refreshMeshView();
  meshPollTimer = setInterval(refreshMeshView, 2000);
}

async function refreshMeshView() {
  if (!meshName) return;
  let info, history;
  try {
    const [r1, r2] = await Promise.all([
      api(`/api/mesh/${encodeURIComponent(meshName)}`),
      api(`/api/mesh/${encodeURIComponent(meshName)}/messages?limit=100`),
    ]);
    info = await r1.json();
    if (!r1.ok) {
      $("mesh-view").innerHTML = "";
      $("mesh-view").appendChild(el("p", "wf-warning", info.error || "cannot load mesh"));
      return;
    }
    history = r2.ok ? (await r2.json()).messages || [] : [];
  } catch {
    return;
  }
  renderRelayBadge(info.relay);
  renderMesh(info, history);
}

/* The send/add forms must survive the 2s poll: rebuild everything except a
   form the user is currently typing in. */
function formInUse(root) {
  return root.contains(document.activeElement) &&
    ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
}

function renderMesh(info, history) {
  const view = $("mesh-view");
  if (formInUse(view)) return; // don't wipe in-progress input
  view.innerHTML = "";

  // Federation v2: '' machine = the primary daemon's own member. On a
  // mirror, OUR members carry our relay name; selfMachine tells them apart.
  const selfMachine = (info.relay && info.relay.name) || "";
  const isMirror = !!info.primary;
  const isLocalMember = (m) =>
    isMirror ? m.machine === selfMachine : (!m.machine || m.machine === selfMachine);

  const head = el("div", "wf-head");
  head.appendChild(el("h2", null, `mesh: ${info.name}`));
  if (isMirror) {
    head.appendChild(el("span", "mesh-mirror-badge", `mirror of ${info.primary}`));
  }
  const rm = el("button", "wf-btn archive", "Remove mesh");
  rm.addEventListener("click", async () => {
    if (!confirm(`Remove mesh '${info.name}'? Its history is retired on disk.`)) return;
    await api(`/api/mesh/${encodeURIComponent(info.name)}`, { method: "DELETE" });
    location.hash = "#";
    refreshMeshList();
  });
  head.appendChild(rm);
  view.appendChild(head);
  view.appendChild(el(
    "p", "wf-desc",
    "messages are typed into recipients' terminals by the daemon — " +
    "agents reply with: claunch mesh send " + info.name + " <to|*> \"...\""
  ));

  // members table
  const members = info.members || [];
  const box = el("div", "mesh-members");
  box.appendChild(el("h3", null, "Members"));
  if (!members.length) {
    box.appendChild(el("p", "wf-note", "no members yet — enrol a session below"));
  }
  for (const m of members) {
    const row = el("div", "mesh-member");
    const dot = el("span", `dot ${meshDotClass(m.reachability)}`);
    const name = el("span", "mesh-handle", m.handle);
    const role = el("span", "mesh-role", m.role);
    const machineLabel = m.machine || (isMirror ? info.primary : "");
    const where = el(
      "span", "mesh-session mono",
      (machineLabel ? machineLabel + "/" : "") + m.session
    );
    if (isLocalMember(m)) {
      where.classList.add("linkish");
      where.title = "attach this session's terminal";
      where.addEventListener("click", () => attach(m.session));
    }
    const state = el("span", "meta", m.reachability +
      (m.pending ? ` · ${m.pending} pending` : ""));
    row.append(dot, name, role, where, state);
    // A mirror may only remove its own members; the primary's roster is the
    // primary's to edit (and whole guest machines go via 'revoke' below).
    if (!isMirror || isLocalMember(m)) {
      const kick = el("button", "mesh-kick", "×");
      kick.title = `remove '${m.handle}' from the mesh`;
      kick.addEventListener("click", async () => {
        if (!confirm(`Remove member '${m.handle}'?`)) return;
        const resp = await api(
          `/api/mesh/${encodeURIComponent(info.name)}/members/${encodeURIComponent(m.handle)}`,
          { method: "DELETE" }
        );
        if (!resp.ok) {
          const doc = await resp.json().catch(() => ({}));
          alert(doc.error || `HTTP ${resp.status}`);
          return;
        }
        refreshMeshView();
        refreshMeshList();
      });
      row.appendChild(kick);
    }
    box.appendChild(row);
  }

  // enrol form: any live session not already a member
  const taken = new Set(
    members.filter((m) => isLocalMember(m)).map((m) => m.session)
  );
  const candidates = sessionsCache.filter(
    (s) => s.status !== "exited" && !taken.has(s.name)
  );
  const addRow = el("div", "mesh-add");
  const sel = document.createElement("select");
  for (const s of candidates) {
    const opt = document.createElement("option");
    opt.value = s.name;
    opt.textContent = s.name;
    sel.appendChild(opt);
  }
  const handle = document.createElement("input");
  handle.placeholder = "handle (default: session name)";
  const addBtn = el("button", "wf-btn option", "Add to mesh");
  addBtn.disabled = !candidates.length;
  if (!candidates.length) addBtn.title = "no unenrolled live sessions";
  addBtn.addEventListener("click", async () => {
    const resp = await api(`/api/mesh/${encodeURIComponent(info.name)}/members`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session: sel.value, handle: handle.value.trim() }),
    });
    if (!resp.ok) {
      const doc = await resp.json().catch(() => ({}));
      alert(doc.error || `HTTP ${resp.status}`);
      return;
    }
    handle.value = "";
    refreshMeshView();
    refreshMeshList();
  });
  addRow.append(sel, handle, addBtn);
  box.appendChild(addRow);
  view.appendChild(box);

  // membership from other machines: who joined us (guests) or who owns us
  const fed = el("div", "mesh-fed");
  fed.appendChild(el("h3", null, isMirror ? "Primary daemon" : "Guest daemons"));
  const peers = info.peers || [];
  if (!peers.length) {
    fed.appendChild(el(
      "p", "wf-note",
      "no other machine has joined — sessions elsewhere join with " +
      `'claunch mesh join ${info.name}@${selfMachine || "<this-machine>"}' ` +
      "and appear below once approved (both daemons need a relay uplink)"
    ));
  }
  for (const p of peers) {
    const row = el("div", "mesh-member");
    const ok = p.ok === true;
    const state = p.ok === false ? `unreachable — ${p.error || "?"}` :
      (ok ? "ok" : "linked, no traffic yet");
    row.appendChild(el("span", `dot ${ok ? "idle" : (p.ok === false ? "exited" : "starting")}`));
    row.appendChild(el("span", "mesh-handle mono", p.machine));
    row.appendChild(el("span", "mesh-role", p.role || ""));
    row.appendChild(el("span", "meta", state + (p.queued ? ` · ${p.queued} queued` : "")));
    if (!isMirror) {
      const revoke = el("button", "mesh-kick", "×");
      revoke.title = `unlink ${p.machine}: drop its members and its mirror`;
      revoke.addEventListener("click", async () => {
        if (!confirm(
          `Unlink guest '${p.machine}'? Its members leave the mesh and its ` +
          "mirror is dropped."
        )) return;
        const resp = await api(
          `/api/mesh/${encodeURIComponent(info.name)}/guests/${encodeURIComponent(p.machine)}`,
          { method: "DELETE" }
        );
        if (!resp.ok) {
          const doc = await resp.json().catch(() => ({}));
          alert(doc.error || `HTTP ${resp.status}`);
          return;
        }
        refreshMeshView();
        refreshMeshList();
      });
      row.appendChild(revoke);
    }
    fed.appendChild(row);
  }
  if (!isMirror) {
    // Joins are requests: the owner admits them. A ticket is only a way to
    // pre-approve one, for automation that cannot wait for a human.
    for (const r of info.requests || []) {
      const row = el("div", "mesh-member");
      row.appendChild(el("span", "dot starting"));
      row.appendChild(el("span", "mesh-handle", r.handle));
      row.appendChild(el("span", "mesh-role", r.role || ""));
      row.appendChild(el("span", "mesh-session mono", `${r.machine}/${r.session}`));
      row.appendChild(el("span", "meta", "wants to join"));
      for (const [label, verb, cls] of [
        ["Approve", "approve", "approve"], ["Deny", "deny", "archive"],
      ]) {
        const btn = el("button", `wf-btn ${cls}`, label);
        btn.addEventListener("click", async () => {
          const resp = await api(
            `/api/mesh/${encodeURIComponent(info.name)}/requests/` +
            `${encodeURIComponent(r.id)}/${verb}`,
            { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }
          );
          const doc = await resp.json().catch(() => ({}));
          if (!resp.ok) { alert(doc.error || `HTTP ${resp.status}`); return; }
          if (verb === "approve" && doc.delivered === false) {
            alert(`admitted '${doc.handle}' — ${doc.machine} is unreachable, ` +
                  "the grant is queued and retried");
          }
          refreshMeshView();
          refreshMeshList();
        });
        row.appendChild(btn);
      }
      fed.appendChild(row);
    }
    const fedRow = el("div", "mesh-add");
    const inviteBtn = el("button", "wf-btn option", "Mint invite ticket…");
    const codeOut = document.createElement("input");
    codeOut.readOnly = true;
    codeOut.placeholder =
      "single-use ticket appears here — it pre-approves one join";
    // The view is rebuilt by the 2s poll, which can detach this input while
    // the invite request is in flight — so the code lives in meshInviteCodes
    // (module state) and every rebuild re-renders it from there.
    codeOut.value = meshInviteCodes[info.name] || "";
    codeOut.addEventListener("focus", () => codeOut.select());
    inviteBtn.addEventListener("click", async () => {
      const resp = await api(`/api/mesh/${encodeURIComponent(info.name)}/invite`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      });
      const doc = await resp.json().catch(() => ({}));
      if (!resp.ok) { alert(doc.error || `HTTP ${resp.status}`); return; }
      meshInviteCodes[info.name] = doc.code || "";
      refreshMeshView();
    });
    fedRow.append(inviteBtn, codeOut);
    fed.appendChild(fedRow);
    fed.appendChild(el(
      "p", "wf-note",
      `redeemed there with: claunch mesh join ${info.name}@${selfMachine || "<this-machine>"} --code <ticket>`
    ));
  }
  view.appendChild(fed);
  const polBox = renderMeshPolicy(info);
  if (isMirror) {
    // the policy engine runs on the primary; the mirror's copy is read-only
    polBox.querySelectorAll("input,select,button").forEach((n) => { n.disabled = true; });
    polBox.appendChild(el(
      "p", "wf-note",
      `policy is owned by the primary daemon (${info.primary}) — edit it there`
    ));
  }
  view.appendChild(polBox);

  // send box: as the human operator, or on behalf of a member
  const send = el("div", "mesh-send");
  send.appendChild(el("h3", null, "Send message"));
  const from = document.createElement("select");
  {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "from: operator (you)";
    from.appendChild(opt);
  }
  // Only sessions this daemon actually hosts: speaking as a member on another
  // machine is impersonation, and the primary rejects it.
  for (const m of members.filter((m) => isLocalMember(m))) {
    const opt = document.createElement("option");
    opt.value = m.handle;
    opt.textContent = `from: ${m.handle}`;
    from.appendChild(opt);
  }
  const to = document.createElement("select");
  {
    const opt = document.createElement("option");
    opt.value = "*";
    opt.textContent = "to: * (everyone)";
    to.appendChild(opt);
  }
  for (const m of members) {
    const opt = document.createElement("option");
    opt.value = m.handle;
    opt.textContent = `to: ${m.handle}`;
    to.appendChild(opt);
  }
  const intent = document.createElement("select");
  for (const [v, label] of [
    ["say", "type: say"], ["ask", "type: ask (expects reply)"],
    ["fyi", "type: fyi (no reply)"], ["ack", "type: ack (no reply)"],
  ]) {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = label;
    intent.appendChild(opt);
  }
  const text = document.createElement("textarea");
  text.placeholder = "message — delivered by typing into the recipient's terminal";
  text.rows = 3;
  const sendBtn = el("button", "wf-btn approve", "Send");
  sendBtn.addEventListener("click", async () => {
    const body = text.value.trim();
    if (!body) return;
    const external = !from.value;
    const resp = await api(`/api/mesh/${encodeURIComponent(info.name)}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        from: from.value || "operator",
        to: to.value === "*" ? "*" : to.value,
        body,
        external,
        type: intent.value,
      }),
    });
    const doc = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      alert(doc.error || `HTTP ${resp.status}`);
      return;
    }
    text.value = "";
    text.blur();
    if (doc.queued) {
      // mirror with its primary unreachable: durably queued, not yet in the log
      alert(`queued ${doc.id} — the primary daemon (${info.primary}) is ` +
            "unreachable; it will be forwarded, in order, on reconnect");
    } else if ((doc.queued_remote || []).length) {
      alert(`sent — queued for unreachable machines: ${doc.queued_remote.join(", ")}`);
    }
    refreshMeshView();
  });
  const row = el("div", "mesh-send-row");
  row.append(from, to, intent);
  send.append(row, text, sendBtn);
  view.appendChild(send);

  // message log (latest last, like a chat)
  const logBox = el("div", "mesh-log");
  logBox.appendChild(el("h3", null, `Messages (${info.messages})`));
  if (!history.length) logBox.appendChild(el("p", "wf-note", "no messages yet"));
  for (const m of history) {
    const line = el("div", "mesh-msg");
    const meta = el("div", "mesh-msg-meta");
    const toS = m.to === "*" ? "everyone" : (Array.isArray(m.to) ? m.to.join(", ") : m.to);
    meta.appendChild(el("span", "mesh-msg-from", m.from));
    meta.appendChild(el("span", null, `→ ${toS}`));
    if (m.type && m.type !== "say") {
      meta.appendChild(el("span", "mesh-msg-type", m.type));
    }
    if (m.reply_to) {
      meta.appendChild(el("span", "mesh-msg-type", `re ${m.reply_to}`));
    }
    if (m.sections) {
      meta.appendChild(el("span", "mesh-msg-type", "batch"));
    }
    meta.appendChild(el("span", "mesh-msg-at", (m.ts || "").replace("T", " ")));
    line.appendChild(meta);
    line.appendChild(el("div", "mesh-msg-body", m.body || ""));
    logBox.appendChild(line);
  }
  view.appendChild(logBox);
}

/* Nudge-policy editor: heartbeat / task-poll / stall warnings, per mesh.
   All nudges are terminal injections (they consume the agent's turn), so
   every section ships disabled until deliberately switched on here. */
function renderMeshPolicy(info) {
  const pol = info.policy || {};
  const box = el("div", "mesh-policy");
  box.appendChild(el("h3", null, "Nudge policy"));
  box.appendChild(el(
    "p", "wf-note",
    "nudges are typed into the member's terminal, so each one costs the " +
    "agent a turn — enable deliberately"
  ));
  const fields = {};
  const num = (val) => {
    const inp = document.createElement("input");
    inp.type = "number"; inp.min = "1"; inp.value = val;
    inp.className = "pol-num";
    return inp;
  };
  const section = (key, title, rows) => {
    const sec = el("div", "pol-section");
    const head = el("label", "pol-head");
    const on = document.createElement("input");
    on.type = "checkbox"; on.checked = !!(pol[key] || {}).enabled;
    head.append(on, el("span", null, title));
    sec.appendChild(head);
    fields[key] = { enabled: on };
    for (const [name, label, input] of rows) {
      const row = el("div", "pol-row");
      row.append(el("span", "pol-label", label), input);
      sec.appendChild(row);
      fields[key][name] = input;
    }
    box.appendChild(sec);
  };

  const hb = pol.heartbeat || {};
  const hbBody = document.createElement("input");
  hbBody.value = hb.body || "";
  section("heartbeat", "heartbeat — remind a member sitting on unanswered messages", [
    ["interval", "first nudge after (s)", num(hb.interval ?? 180)],
    ["max_interval", "backoff ceiling (s)", num(hb.max_interval ?? 1800)],
    ["body", "message", hbBody],
  ]);

  const tp = pol.task_poll || {};
  const tpRoles = document.createElement("input");
  tpRoles.value = (tp.roles || ["worker"]).join(", ");
  const tpBody = document.createElement("input");
  const firstRole = (tp.roles || ["worker"])[0] || "worker";
  tpBody.value = (tp.bodies || {})[firstRole] || "";
  section("task_poll", "task-poll — poke idle, caught-up members of these roles", [
    ["interval", "poke after idle (s)", num(tp.interval ?? 600)],
    ["max_interval", "backoff ceiling (s)", num(tp.max_interval ?? 3600)],
    ["roles", "roles (comma-sep)", tpRoles],
    ["body", `message (role: ${firstRole})`, tpBody],
  ]);

  const sw = pol.stall_warn || {};
  section("stall_warn", "stall warning — message the leaders about a stuck member", [
    ["warn_secs", "warn after (s)", num(sw.warn_secs ?? 600)],
  ]);

  const save = el("button", "wf-btn approve", "Save policy");
  save.addEventListener("click", async () => {
    const roles = tpRoles.value.split(",").map((r) => r.trim()).filter(Boolean);
    const patch = {
      heartbeat: {
        enabled: fields.heartbeat.enabled.checked,
        interval: +fields.heartbeat.interval.value,
        max_interval: +fields.heartbeat.max_interval.value,
        body: hbBody.value,
      },
      task_poll: {
        enabled: fields.task_poll.enabled.checked,
        interval: +fields.task_poll.interval.value,
        max_interval: +fields.task_poll.max_interval.value,
        roles,
        bodies: tpBody.value ? { [roles[0] || "worker"]: tpBody.value } : {},
      },
      stall_warn: {
        enabled: fields.stall_warn.enabled.checked,
        warn_secs: +fields.stall_warn.warn_secs.value,
      },
    };
    const resp = await api(`/api/mesh/${encodeURIComponent(info.name)}/policy`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    const doc = await resp.json().catch(() => ({}));
    if (!resp.ok) { alert(doc.error || `HTTP ${resp.status}`); return; }
    document.activeElement?.blur?.();
    refreshMeshView();
  });
  box.appendChild(save);
  return box;
}

function meshDotClass(reachability) {
  if (reachability === "idle") return "idle";
  if (reachability === "busy" || reachability === "starting") return "busy";
  if (reachability === "remote-connected") return "starting";
  return "exited"; // exited / missing / remote-disconnected / unknown
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
    renderRelayBadge(info.relay);
  } catch {
    return; // auth overlay is up
  }
  refreshProfiles();
  refreshSessions();
  refreshMeshList();
  refreshCflow();
  route();
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    refreshSessions();
    refreshMeshList();
    refreshCflow();
  }, 2000);
}

boot();
