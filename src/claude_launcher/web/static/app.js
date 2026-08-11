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
   `claunch daemon token --rotate`.

   The key is scoped by BASE because several daemons reach the browser through
   one relay origin ("/t/<name>/" each) and therefore share one localStorage:
   under a single global key each tunnel overwrites its siblings' token, and
   the next daemon restart makes them re-prompt with a token that isn't
   theirs. (Their session cookies don't collide — the relay rewrites Path to
   the tunnel prefix.) */
const TOKEN_KEY = `claunch_token:${BASE}`;
let reloginPromise = null;

/* One-shot migration off the old unscoped key: whichever tunnel loads first
   inherits it, the rest paste their token once more. */
(function migrateLegacyToken() {
  const legacy = localStorage.getItem("claunch_token");
  if (legacy === null) return;
  localStorage.removeItem("claunch_token");
  if (localStorage.getItem(TOKEN_KEY) === null) {
    localStorage.setItem(TOKEN_KEY, legacy);
  }
})();

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
/* Order sessions parent-before-child, returning [session, depth] pairs.
   A fleet is a tree — one lead and the workers it spawned — and a flat
   alphabetical list is the one view that hides which is which.

   A session whose parent is not in the list (its record cleared away, a
   hand-edited definition) is shown as a root rather than dropped: the list
   accounts for every session, and a dangling name or a cycle must not make
   one invisible. This mirrors _by_lineage in cli_sessions.py — the CLI has
   printed the tree since sessions could have parents, and the two listings
   disagreeing about who is whose would be worse than either being wrong. */
function byLineage(sessions) {
  const byName = new Map(sessions.map((s) => [s.name, s]));
  const kids = new Map();
  const roots = [];
  for (const s of sessions) {
    if (s.parent && s.parent !== s.name && byName.has(s.parent)) {
      if (!kids.has(s.parent)) kids.set(s.parent, []);
      kids.get(s.parent).push(s);
    } else {
      roots.push(s);
    }
  }
  const out = [];
  const seen = new Set();
  const walk = (node, depth) => {
    if (seen.has(node.name)) return;   // cycle guard
    seen.add(node.name);
    out.push([node, depth]);
    for (const kid of kids.get(node.name) || []) walk(kid, depth + 1);
  };
  for (const root of roots) walk(root, 0);
  for (const s of sessions) if (!seen.has(s.name)) out.push([s, 0]);
  return out;
}

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
  for (const [s, depth] of byLineage(sessionsCache)) {
    const li = document.createElement("li");
    li.dataset.name = s.name;
    if (s.name === currentName) li.classList.add("active");
    // The indent goes on the row, not on a spacer element, so the whole row
    // stays one click target and the hover/active background still spans it.
    if (depth) {
      li.style.paddingLeft = `${16 + depth * 14}px`;
      li.classList.add("child");
      li.title = `spawned by ${s.parent}`;
    }
    const dot = document.createElement("span");
    dot.className = `dot ${s.status}`;
    const label = document.createElement("span");
    label.textContent = s.name;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = s.status === "exited"
      ? `exit ${s.exit_code ?? "?"}`
      : (s.profile || s.harness);
    if (s.status === "exited") {
      li.title = [li.title, "exited — open it to resume"].filter(Boolean).join(" · ");
    }
    // A second destination per row: the terminal is what the session is
    // doing, this is what it *is* (definition, meshes, its cflow run).
    const info = document.createElement("button");
    info.className = "sess-info";
    info.type = "button";
    info.textContent = "ⓘ";
    info.title = "session details: harness, directory, meshes, workflow";
    info.addEventListener("click", (e) => {
      e.stopPropagation();   // the row itself attaches; this button does not
      location.hash = "#/s/" + encodeURIComponent(s.name) + "/info";
    });
    li.append(dot, label, meta, info);
    li.addEventListener("click", () => {
      location.hash = "#/s/" + encodeURIComponent(s.name);
    });
    list.appendChild(li);
  }
  refreshResumeChoices();  // the spawn form offers these same conversations
  if (currentPage === "home") renderHome();
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
    // workflow page, or out of the mobile menu (it stays pending until they
    // come back, since the stale pid keeps failing this test).
    if (terminalOnScreen()) attach(currentName);
  } else if (cur && !(ws && ws.readyState === WebSocket.OPEN)) {
    // Otherwise an open socket stays authoritative: it sees this session's
    // every state change first-hand (an exit reaches it seconds before the
    // next poll), so a stale entry can't flicker the resume/kill controls.
    setStatusBadge(cur.status);
  }
  // The mobile bottom bar carries this session's harness/profile, which only
  // the list knows.
  syncMobileBars();
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
  cflowCache = runs;
  if (currentPage === "home") renderHome();
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
    // A slot with no run but a request filed against it is listed too — that
    // waiting period is exactly when a human wants to see something.
    if (r.pending_start) {
      li.appendChild(cflowLine(
        `start requested: ${r.pending_start.name || r.pending_start.workflow}`,
        "report"
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
        location.hash = "#/s/" + encodeURIComponent(r.sessions[0]);
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

/* ------------------------------------------------------------------ */
/* new-session form: role, resume, fork                               */
/* ------------------------------------------------------------------ */
/* The DOM sentinel for a bare --resume. The API spells it as an empty
   string, which the <select> already spends on "(new conversation)". */
const PICKER = "@picker";

/* Roles keyed by name, so picking one can show the stance it would inject
   without a second round-trip. The vocabulary is fixed for the daemon's
   lifetime — fetched once at boot, never polled. */
let rolesByName = {};

/* Signature of the workspace list currently rendered. The registry changes
   from the CLI (`claunch workspace add`), so it IS polled — but rebuilding
   the <select> on every poll would slam shut a dropdown the user has open,
   so the options are only rebuilt when the list actually differs. */
let workspacesRendered = null;

/* Last workspace list the poll saw, for the manage page (#/workspaces). */
let workspacesCache = [];

/* Last cflow run list the poll saw, for the home dashboard. */
let cflowCache = [];

/* The declared harnesses. Fetched once: the set is declared in YAML and
   changes when someone edits config or installs a program, neither of which
   happens mid-session — a reload is the honest way to pick those up. */
async function refreshHarnesses() {
  const select = document.querySelector("#new-session select[name=harness]");
  let list = [];
  try {
    const resp = await api("/api/harnesses");
    if (!resp.ok) throw new Error(String(resp.status));
    list = (await resp.json()).harnesses || [];
  } catch {
    // Older daemon, same fallback reasoning as refreshWorkspaces: claude is
    // the one harness that is always there, so the form stays usable.
    list = [{ name: "claude", available: true, description: "" }];
  }
  select.innerHTML = "";
  for (const h of list) {
    const opt = new Option(
      h.available ? h.name : `${h.name} (not installed)`,
      h.name
    );
    // Declared but missing: shown, not hidden. Hiding it would read as
    // "claunch does not support pi", which is the wrong thing to learn.
    opt.disabled = !h.available;
    opt.title = h.available
      ? h.description || h.name
      : `${h.description || h.name}\n\n'${h.program || h.name}' is not on PATH`;
    select.appendChild(opt);
  }
  const first = [...select.options].find((o) => !o.disabled);
  select.value = first ? first.value : "";
  syncForkAvailability();  // role/resume/fork only apply to the claude harness
}

async function refreshWorkspaces() {
  const select = document.querySelector("#new-session select[name=cwd]");
  let list;
  try {
    const resp = await api("/api/workspaces");
    if (!resp.ok) throw new Error(String(resp.status));
    list = (await resp.json()).workspaces || [];
  } catch {
    // A daemon older than these assets has no /api/workspaces (it serves
    // static files from disk but runs the Python it started with). Leave the
    // form usable rather than showing an empty, un-submittable picker.
    if (!select.options.length) {
      select.appendChild(new Option("(daemon cwd)", ""));
      const stale = $("cwd-hint");
      stale.textContent =
        "workspace list unavailable — 'claunch daemon restart' to pick up this version";
      stale.classList.remove("hidden");
    }
    return;
  }
  const signature = JSON.stringify(list);
  if (signature === workspacesRendered) return;
  workspacesRendered = signature;
  // The manage page reads the same poll: the registry is also edited from a
  // shell, so a 'claunch workspace add' in another window lands here too.
  workspacesCache = list;
  if (wsOpen) renderWorkspaces();
  if (currentPage === "home") renderHome();

  const previous = select.value;
  select.innerHTML = "";
  // The daemon's own directory is always available and needs no registering,
  // so the form still works on a machine with an empty registry.
  select.appendChild(new Option("(daemon cwd)", ""));
  for (const w of list) {
    const opt = new Option(
      w.exists ? `${w.name} — ${w.path}` : `${w.name} — ${w.path} (missing)`,
      w.path
    );
    opt.title = w.path;
    // A directory that is not there right now would fail to spawn; the entry
    // stays visible (it is still the user's) but cannot be chosen.
    opt.disabled = !w.exists;
    select.appendChild(opt);
  }
  // Falling back to "(daemon cwd)" also covers a workspace that went missing
  // while it was selected: Chrome will happily keep a disabled option
  // selected, and Create would then fail on a directory that isn't there.
  const kept = [...select.options].find((o) => o.value === previous);
  select.value = kept && !kept.disabled ? previous : "";

  const hint = $("cwd-hint");
  hint.textContent = list.length
    ? ""
    : "no workspaces yet — register one with: claunch workspace add <dir>";
  hint.classList.toggle("hidden", list.length > 0);
}

async function refreshRoles() {
  const select = document.querySelector("#new-session select[name=role]");
  try {
    const resp = await api("/api/roles");
    const data = await resp.json();
    rolesByName = {};
    select.innerHTML = "";
    select.appendChild(new Option("(no role)", ""));
    for (const role of data.roles || []) {
      rolesByName[role.name] = role;
      const label = role.aliases && role.aliases.length
        ? `${role.name} — ${role.aliases.join(", ")}`
        : role.name;
      select.appendChild(new Option(label, role.name));
    }
  } catch { /* ignore */ }
}

/* What the chosen role would put in the session's system prompt. Shown in
   full rather than summarised: it is the one thing about a spawned session
   the user cannot inspect afterwards from the terminal. */
function renderRoleStance() {
  const select = document.querySelector("#new-session select[name=role]");
  const box = $("role-stance");
  const role = rolesByName[select.value];
  box.textContent = role ? (role.stance || "(this role declares no stance)") : "";
  box.classList.toggle("hidden", !role);
}

/* The resume picker: claude's own interactive picker, or the conversation of
   a session this daemon knows. Exited sessions are offered too — their
   conversation outlives them, and picking one up elsewhere is the point.
   Rebuilt on every poll, so the current choice is preserved by hand. */
function refreshResumeChoices() {
  const select = document.querySelector("#new-session select[name=resume]");
  const previous = select.value;
  select.innerHTML = "";
  select.appendChild(new Option("(new conversation)", ""));
  select.appendChild(new Option("pick in claude's picker (--resume)", PICKER));
  for (const s of sessionsCache) {
    if (!s.conversation_id) continue;  // nothing pinned to resume
    select.appendChild(new Option(`${s.name} — ${s.status}`, s.name));
  }
  // A session that vanished (cleared, renamed) takes its option with it;
  // falling back to "(new conversation)" beats silently resuming a stranger.
  select.value = [...select.options].some((o) => o.value === previous)
    ? previous
    : "";
  syncForkAvailability();
}

/* --fork-session is claude's own "use with --resume or --continue": with
   nothing to fork it is not a weaker choice, it is a rejected one. */
function syncForkAvailability() {
  const f = $("new-session");
  const resuming = f.resume.value !== "";
  const claude = (f.harness.value || "claude") === "claude";
  f.fork.disabled = !resuming || !claude;
  if (f.fork.disabled) f.fork.checked = false;
  f.role.disabled = !claude;
  f.resume.disabled = !claude;
  if (!claude) {
    f.role.value = "";
    f.resume.value = "";
    renderRoleStance();
  }
}

document
  .querySelector("#new-session select[name=role]")
  .addEventListener("change", renderRoleStance);
$("new-session").resume.addEventListener("change", syncForkAvailability);
$("new-session").harness.addEventListener("change", syncForkAvailability);

$("new-session").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = {
    name: f.name.value.trim(),
    harness: f.harness.value || "claude",
    profile: f.profile.value || null,
    cwd: f.cwd.value,  // a registered workspace path, or "" = the daemon's cwd
    args: f.args.value.trim() ? f.args.value.trim().split(/\s+/) : [],
  };
  if (f.role.value) body.role = f.role.value;
  // Onboarding: only sent when chosen. The daemon checks each before it
  // builds anything, so a stale mesh or workflow is refused with nothing
  // left behind.
  if (f.mesh.value) {
    body.mesh = f.mesh.value;
    if (f.handle.value.trim()) body.handle = f.handle.value.trim();
  }
  if (f.workflow.value) {
    body.workflow = f.workflow.value;
    if (f.context.value.trim()) body.context = f.context.value.trim();
  }
  if (f.task.value.trim()) body.task = f.task.value.trim();
  if (f.resume.value) {
    // "" (no resume) is left off entirely: the API reads a missing key as
    // "a new conversation" and an empty string as "open the picker".
    body.resume = f.resume.value === PICKER ? "" : f.resume.value;
    body.fork_session = f.fork.checked;
  }
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
  // A resume choice is spent: leaving it selected would point the next
  // Create at the same conversation and quietly open it twice. The role is
  // left alone — spawning a second worker is a normal thing to want.
  f.resume.value = "";
  // The opening task named this session's job, so it is spent too — the
  // mesh and workflow pickers are not, since a second worker on the same
  // team is the normal next thing to want.
  f.task.value = "";
  f.context.value = "";
  syncForkAvailability();
  const info = await resp.json();
  await refreshSessions();
  location.hash = "#/s/" + encodeURIComponent(info.name);
});

$("term-details").addEventListener("click", () => {
  if (currentName) {
    location.hash = "#/s/" + encodeURIComponent(currentName) + "/info";
  }
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
    location.hash = "#/";
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
    location.hash = "#/";
  }
  refreshSessions();
});

/* The rail polls, but a poll is a tick behind at best: a session spawned from
   somewhere else — another agent's `spawn`, a `claunch new` in a terminal, a
   relay that dropped a beat — shows up only when the timer next comes round.
   This is that timer, on demand, for the whole rail at once. */
$("refresh-all").addEventListener("click", async () => {
  const btn = $("refresh-all");
  if (btn.disabled) return;
  btn.disabled = true;
  btn.classList.add("spinning");
  try {
    await Promise.all([
      refreshSessions(),
      refreshMeshList(),
      refreshCflow(),
      refreshWorkspaces(),
      // The spawn form's pickers come from the same daemon and go stale the
      // same way — a workspace or harness added since load belongs here too.
      refreshHarnesses(),
      refreshRoles(),
      refreshProfiles(),
      // A local daemon answers before the eye registers the spin; without a
      // floor the button just flickers and reads as "nothing happened".
      new Promise((done) => setTimeout(done, 400)),
    ]);
  } finally {
    btn.disabled = false;
    btn.classList.remove("spinning");
  }
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
  syncMobileBars();  // the mobile bars mirror this header
}

function detach() {
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
  if (term) { term.dispose(); term = null; fitAddon = null; }
  attachedPid = null; // re-learned from the next socket's init frame
}

/* Bind the terminal to a session. Called only by the #/s/<name> route, so
   the attached session is in the URL: a reload, a bookmark or a shared link
   lands back on the same terminal instead of on an empty slot. */
function attach(name) {
  detach();
  currentName = name;
  stopWfPoll();
  stopMeshPoll();
  // showView first, and before the terminal is opened: on mobile #main is
  // display:none while the rail is up, and a terminal opened into a
  // zero-height box fits to nothing.
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
        refitSoon(50);
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

/* Not merely "the terminal isn't hidden": on mobile the rail takes the whole
   screen and #main goes with it, so the terminal is up but nobody can see it. */
function terminalOnScreen() {
  return !$("terminal").classList.contains("hidden") && !railOpen;
}

/* A fit is only meaningful while the terminal is actually on screen — off it,
   the box measures zero and the session would be sent a garbage size. */
function canFit() {
  return !!fitAddon && terminalOnScreen() && $("terminal").clientHeight > 0;
}

// Debounce viewport-driven fits. On a phone the visual viewport jitters (URL
// bar collapsing, keyboard) and firing fit() on every event floods the session
// with resizes — most visible over a relay, where each round-trip lags.
function refitSoon(delay = 150) {
  if (!fitAddon) return;
  clearTimeout(fitTimer);
  fitTimer = setTimeout(() => { if (canFit()) fitAddon.fit(); }, delay);
}
window.addEventListener("resize", () => refitSoon());

/* Another viewer (e.g. `claunch attach`) may have resized the session while
   this tab was in the background, leaving the grid garbled. On focus regain,
   re-assert this viewer's size and ask the daemon for a fresh repaint —
   event-driven only, no polling. */
function resyncTerminal() {
  if (!term || !ws || ws.readyState !== WebSocket.OPEN) return;
  if (canFit()) fitAddon.fit(); // fires term.onResize -> server resize when dims changed
  ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
  ws.send(JSON.stringify({ type: "repaint" }));
}
window.addEventListener("focus", resyncTerminal);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) resyncTerminal();
});

/* ------------------------------------------------------------------ */
/* layout: the one place that knows how wide the screen is             */
/* ------------------------------------------------------------------ */
/* Everything above the breakpoint shows the rail and the page at once;
   everything below shows one or the other. That single rule is the whole
   difference between the two form factors, and it lives HERE — routing and
   the page renderers are written as if the screen were infinite.

   It used to be scattered: four page-open functions each carried the same
   `setMenuOpen(false)  // the page it opens lives where the terminal was`,
   and the sidebar's sections were shown and hidden by CSS keyed on a
   `data-mtab` attribute, which is to say navigation implemented in a
   stylesheet. Both are gone: sections became routes, and the concern got a
   home. */
const MOBILE_MQ = window.matchMedia("(max-width: 820px)");

/* Below the breakpoint, whether the rail is the thing on screen. Above it
   the rail is always docked and this stays false, so nothing else has to
   read the flag and the media query together. */
let railOpen = false;

/* Reconcile the chrome with (breakpoint, current page, attached session).
   Derived, never toggled from the outside: every navigation ends here, so a
   page cannot forget to do its half. */
function syncLayout() {
  const page = currentPage;
  document.body.dataset.page = page;
  document.querySelectorAll("#rail-nav a").forEach((a) =>
    a.classList.toggle("active", a.dataset.page === page)
  );
  // On a phone home IS the menu: the rail is the whole of that page, so
  // there is nothing to lay over it. Above the breakpoint the rail is docked
  // beside the page and is never a mode.
  const wasOpen = railOpen;
  railOpen = MOBILE_MQ.matches && page === "home";
  document.body.classList.toggle("rail-open", railOpen);
  syncMobileBars();
  // Coming back from the rail the terminal was display:none, so its grid is
  // whatever it was before the viewport last changed. Re-fit it.
  if (wasOpen && !railOpen) refitSoon(60);
}

/* What the top bar calls the thing on screen. Pages live in the same slot as
   the terminal, so the bar names them too. */
function mobileTitle() {
  switch (currentPage) {
    case "home": return "claunch";
    case "new": return "new session";
    case "meshes": return "mesh";
    case "flows": return "workflows";
    case "ws": return "workspaces";
    case "mesh": return `mesh · ${meshName}`;
    case "wf": return `workflow · ${shortenPath(wfCwd || "")}`;
    case "session": return `session · ${sessName}`;
    default: return currentName || "no session";
  }
}

function syncMobileBars() {
  const has = !!currentName;
  // term-status is the socket-fed truth; the list poll trails it by seconds.
  const status = has ? ($("term-status").textContent || "") : "";
  const sess = has ? sessionsCache.find((s) => s.name === currentName) : null;

  $("m-title").textContent = mobileTitle();
  const dot = $("m-dot");
  dot.className = `dot ${status}`;
  dot.classList.toggle("hidden", !has);
  const badge = $("m-status");
  badge.textContent = status;
  badge.className = `badge ${status}`;
  badge.classList.toggle("hidden", !has);
  // Mirrors of the hidden header's buttons — see the click handlers below.
  $("m-resume").classList.toggle("hidden", status !== "exited");
  const kill = $("m-kill");
  kill.classList.toggle("hidden", !has);
  kill.textContent = status === "exited" ? "remove" : "kill";

  const bDot = $("mb-dot");
  bDot.className = `dot ${status}`;
  bDot.classList.toggle("hidden", !has);
  $("mb-name").textContent = has ? currentName : "no session open";
  $("mb-meta").textContent = has
    ? [status, sess && (sess.profile || sess.harness)].filter(Boolean).join(" · ")
    : "pick one from the list";
  $("mobile-bottom").classList.toggle("empty", !has);
}

// ☰ is "show me the rail" — which is the home route, the one page that IS
// the rail on a phone. Going through the router rather than flipping the
// flag keeps the URL honest about what is on screen.
$("m-menu").addEventListener("click", () => { location.hash = "#/"; });
// The header's controls are the real ones; these just reach them, so kill's
// confirm-before-forgetting and resume's reattach stay in one place.
$("m-kill").addEventListener("click", () => $("term-kill").click());
$("m-resume").addEventListener("click", () => $("term-resume").click());

$("mobile-bottom").addEventListener("click", () => {
  if (!currentName) return;
  location.hash = "#/s/" + encodeURIComponent(currentName);
});

/* The layout viewport doesn't shrink when the on-screen keyboard opens, so
   100dvh would push the prompt behind the keys. Track the visual viewport
   instead and let the terminal fit what's actually visible. */
function syncViewportHeight() {
  const vv = window.visualViewport;
  document.documentElement.style.setProperty(
    "--app-h", `${Math.round(vv ? vv.height : window.innerHeight)}px`
  );
}
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", () => {
    syncViewportHeight();
    refitSoon();
  });
}
window.addEventListener("resize", syncViewportHeight);
syncViewportHeight();

MOBILE_MQ.addEventListener("change", () => {
  // Rotating a tablet, or dragging a window across the breakpoint. The route
  // does not change — only whether the rail and the page can share the
  // screen — so re-deriving the chrome from the same page is the whole job.
  syncLayout();
  refitSoon();
});

/* ------------------------------------------------------------------ */
/* workflow detail page (#/wf/<cwd>) — diagram, reports, actions      */
/* ------------------------------------------------------------------ */
let wfCwd = null;
let wfScope = "default";
let wfPollTimer = null;
let wfSelectedStep = null; // node picked in the diagram (null = show all)
let wfLastData = null;     // last payload, for instant re-render on selection

/* Every page's container, by page name. The terminal is deliberately not in
   here: it is not swapped in and out, it is *covered* — see showView. */
const VIEWS = {
  home: "home-view",
  new: "new-view",
  meshes: "meshes-view",
  flows: "flows-view",
  session: "sess-view",
  wf: "wf-view",
  mesh: "mesh-view",
  ws: "ws-view",
};

/* The page on screen. Read by the layout and the mobile bars; written only
   by showView, which the router is the only caller of. */
let currentPage = "home";

function showView(name) {
  currentPage = name;
  const showTerm = name === "terminal";
  // The terminal element is never removed and `term`/`ws` are never touched
  // here: a live PTY socket and 5000 lines of scrollback must survive every
  // navigation, so pages hide it rather than replace it. Only attach() and
  // detach() own that object's life.
  $("term-header").classList.toggle("hidden", !(showTerm && currentName));
  $("terminal").classList.toggle("hidden", !showTerm);
  for (const [page, id] of Object.entries(VIEWS)) {
    $(id).classList.toggle("hidden", name !== page);
  }
  if (name !== "session") stopSessionPoll();
  syncLayout();          // rail mode, nav highlight, and the bars' titles
  if (showTerm) refitSoon(60);
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

/* ------------------------------------------------------------------ */
/* router: hash -> page. Knows nothing about screen width.             */
/* ------------------------------------------------------------------ */
/*   #/                  home — the dashboard, and the rail itself on a phone
 *   #/s/<name>          that session's terminal (attached)
 *   #/s/<name>/info     ...and what it *is*: definition, meshes, its run
 *   #/new               the create form
 *   #/mesh              the mesh list, and create/join
 *   #/mesh/<name>       one mesh
 *   #/flows             cflow runs
 *   #/wf/<scope|cwd>    one run
 *   #/workspaces        the workspace registry
 */
function parseHash(h) {
  const raw = (h || "").replace(/^#\/?/, "");
  if (!raw) return { page: "home" };
  const parts = raw.split("/").map(decodeURIComponent);
  if (parts[0] === "s" && parts[1]) {
    return parts[2] === "info"
      ? { page: "session", name: parts[1] }
      : { page: "terminal", name: parts[1] };
  }
  if (parts[0] === "wf" && parts[1]) {
    // The scope is glued to the cwd with '|' because a Windows path is full
    // of the separators a path segment would otherwise be split on.
    const token = parts.slice(1).join("/");
    const sep = token.indexOf("|");
    return sep >= 0
      ? { page: "wf", cwd: token.slice(sep + 1), scope: token.slice(0, sep) }
      : { page: "wf", cwd: token, scope: "default" };  // pre-scope links
  }
  if (parts[0] === "mesh") {
    return parts[1] ? { page: "mesh", name: parts[1] } : { page: "meshes" };
  }
  if (parts[0] === "new") return { page: "new" };
  if (parts[0] === "flows") return { page: "flows" };
  if (parts[0] === "workspaces") return { page: "ws" };
  return { page: "home" };   // an unknown link is a wrong turn, not an error
}

function route() {
  const r = parseHash(location.hash);
  // Leaving a page stops what it was polling. Done centrally so a page's
  // open function never has to know which other pages exist.
  if (r.page !== "wf") stopWfPoll();
  if (r.page !== "mesh") stopMeshPoll();
  if (r.page !== "ws") closeWorkspaces();

  switch (r.page) {
    case "terminal":
      // Re-entering the route we are already attached to must not tear the
      // socket down and build it again — coming back from another page is
      // the common case, and it would cost the scrollback every time.
      if (currentName === r.name && term) showView("terminal");
      else attach(r.name);
      break;
    case "session": openSession(r.name); break;
    case "wf": openWorkflow(r.cwd, r.scope); break;
    case "mesh": openMesh(r.name); break;
    case "meshes": showView("meshes"); refreshMeshList(); break;
    case "new": showView("new"); refreshWorkflowChoices(); break;
    case "flows": showView("flows"); refreshCflow(); break;
    case "ws": openWorkspaces(); break;
    default: openHome();
  }
}
window.addEventListener("hashchange", route);

/* The create form's mesh and workflow pickers. Both are lists the daemon
   already publishes, so neither is a text box: a mesh that does not exist or
   a workflow that is not declared here would be refused at create time, and
   the refusal is cheaper never to provoke. */
function syncOnboardPickers() {
  const form = $("new-session");
  const mesh = form.mesh;
  const keptMesh = mesh.value;
  mesh.innerHTML = "";
  mesh.appendChild(new Option("(none)", ""));
  for (const m of meshCache) mesh.appendChild(new Option(m.name, m.name));
  mesh.value = [...mesh.options].some((o) => o.value === keptMesh) ? keptMesh : "";
  $("new-handle-row").classList.toggle("hidden", !mesh.value);

  const wf = form.workflow;
  const keptWf = wf.value;
  wf.innerHTML = "";
  wf.appendChild(new Option("(none)", ""));
  for (const name of workflowsCache) wf.appendChild(new Option(name, name));
  wf.value = [...wf.options].some((o) => o.value === keptWf) ? keptWf : "";
  $("new-context-row").classList.toggle("hidden", !wf.value);
}

/* Workflows are declared per directory, so the list follows the Directory
   picker rather than being fetched once. */
let workflowsCache = [];
let workflowsFor = null;
async function refreshWorkflowChoices() {
  const cwd = document.querySelector("#new-session select[name=cwd]").value;
  if (cwd === workflowsFor) return;
  workflowsFor = cwd;
  try {
    const resp = await api(`/api/cflow/workflows?cwd=${encodeURIComponent(cwd)}`);
    workflowsCache = resp.ok
      ? ((await resp.json()).workflows || []).map((w) => w.name || w)
      : [];
  } catch {
    workflowsCache = [];
  }
  syncOnboardPickers();
}

$("new-session").mesh.addEventListener("change", syncOnboardPickers);
$("new-session").workflow.addEventListener("change", syncOnboardPickers);
document
  .querySelector("#new-session select[name=cwd]")
  .addEventListener("change", refreshWorkflowChoices);


function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* ------------------------------------------------------------------ */
/* home (#/) — what this daemon is doing, and the way in to each part  */
/* ------------------------------------------------------------------ */
/* On a phone this page IS the rail (see syncLayout), so #main is not on
   screen and rendering it is wasted — but it is cheap, and rendering it
   anyway means rotating a tablet never lands on a stale dashboard. */
function openHome() {
  showView("home");
  renderHome();
}

function homeCard(title, href, subtitle) {
  const card = el("a", "home-card");
  card.href = href;
  const head = el("div", "home-card-head");
  head.appendChild(el("h3", null, title));
  head.appendChild(el("span", "home-go", "›"));
  card.appendChild(head);
  if (subtitle) card.appendChild(el("p", "home-sub", subtitle));
  return card;
}

function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many || one + "s"}`;
}

function renderHome() {
  if (currentPage !== "home") return;
  const view = $("home-view");
  view.innerHTML = "";

  const grid = el("div", "home-grid");

  // Sessions. The rail already lists them on a wide screen, but this page is
  // the menu on a narrow one, where the rail is all there is.
  const live = sessionsCache.filter((s) => s.status !== "exited");
  const busy = live.filter((s) => s.status === "busy").length;
  const sessions = homeCard(
    "Sessions",
    "#/new",
    live.length
      ? `${plural(live.length, "running")}, ${busy} busy`
      : "none running"
  );
  const rows = el("div", "home-rows");
  for (const s of live.slice(0, 6)) {
    const row = el("a", "home-row");
    row.href = "#/s/" + encodeURIComponent(s.name);
    row.appendChild(el("span", `dot ${s.status}`));
    row.appendChild(el("span", "home-row-name", s.name));
    row.appendChild(el("span", "meta", s.profile || s.harness || ""));
    rows.appendChild(row);
  }
  if (live.length > 6) {
    rows.appendChild(el("p", "wf-note", `…and ${live.length - 6} more`));
  }
  if (!live.length) {
    rows.appendChild(el("p", "wf-note", "Create one to get started."));
  }
  sessions.appendChild(rows);
  // The card's own href is the create form: with nothing running that is the
  // only useful destination, and with something running it still is.
  grid.appendChild(sessions);

  grid.appendChild(homeCard(
    "Mesh", "#/mesh",
    meshCache.length
      ? meshCache.map((m) => `${m.name} (${m.members.length})`).join(" · ")
      : "no meshes yet"
  ));

  const runs = cflowCache.filter((r) => r.status && r.status !== "idle");
  grid.appendChild(homeCard(
    "Workflows", "#/flows",
    runs.length ? plural(runs.length, "run") + " active" : "no active runs"
  ));

  const missing = workspacesCache.filter((w) => !w.exists).length;
  grid.appendChild(homeCard(
    "Workspaces", "#/workspaces",
    workspacesCache.length
      ? plural(workspacesCache.length, "directory", "directories") +
        (missing ? ` · ${missing} missing` : "")
      : "none registered"
  ));

  view.appendChild(grid);
}

/* ------------------------------------------------------------------ */
/* workspaces page (#/workspaces) — the registry, managed              */
/*                                                                    */
/* The create form's Directory field is a picker precisely so a path   */
/* is never typed twice; this page is where it is typed the ONE time,  */
/* and the daemon checks it against the filesystem before storing it.  */
/* Registering is the vouching step, so it has to be spellable         */
/* somewhere — what the registry buys is that nowhere else is.         */
/* ------------------------------------------------------------------ */
let wsOpen = false;
let wsError = "";                    // last add/remove failure
let wsDraft = { path: "", name: "" }; // survives a poll-driven rebuild

function openWorkspaces() {
  wsOpen = true;
  showView("ws");
  renderWorkspaces();
  refreshWorkspaces();  // don't make the user wait out the 2s poll
}

function closeWorkspaces() {
  if (!wsOpen) return;
  wsOpen = false;
  wsError = "";
}

/* Sessions currently running in a directory. normcase-style comparison,
   matching the daemon's own (`workspaces._same_path`): on Windows 'F:\Works'
   and 'f:\works' are one directory, and a count that said otherwise would
   under-warn on the unregister confirm. */
function wsSessionsIn(path) {
  const norm = (p) => (p || "").replace(/[\\/]+$/, "").toLowerCase();
  const want = norm(path);
  return want ? sessionsCache.filter((s) => norm(s.cwd) === want) : [];
}

function renderWorkspaces() {
  const view = $("ws-view");
  const focused = document.activeElement && document.activeElement.id;
  view.innerHTML = "";

  const head = el("div", "wf-head");
  head.appendChild(el("h2", null, "Workspaces"));
  const back = el("button", "wf-btn clear", "Back");
  back.addEventListener("click", () => { location.hash = "#"; });
  head.appendChild(back);
  view.appendChild(head);
  view.appendChild(el(
    "p", "wf-note",
    "The directories a session may be spawned in. The create form's " +
    "Directory field is exactly this list — and so is where an agent may " +
    "send a session it spawns, unless spawn.allow_workspace is turned off."
  ));

  view.appendChild(wsAddCard());

  const list = el("div", "ws-list");
  list.appendChild(el("h3", null, `Registered (${workspacesCache.length})`));
  if (!workspacesCache.length) {
    list.appendChild(el(
      "p", "wf-note",
      "Nothing registered yet. Until there is, the create form offers only " +
      "the daemon's own directory and an agent has nowhere to send a child."
    ));
  }
  for (const w of workspacesCache) list.appendChild(wsRow(w));
  view.appendChild(list);

  if (focused) {
    const again = $(focused);
    if (again) {
      again.focus();
      if (again.setSelectionRange) {
        const end = again.value.length;
        again.setSelectionRange(end, end);
      }
    }
  }
}

function wsAddCard() {
  const card = el("form", "ws-add");
  card.appendChild(el("h3", null, "Register a directory"));
  card.appendChild(el(
    "p", "wf-note",
    "Resolved on the daemon's machine, not this browser's, and it must " +
    "already exist — a path that is not there is the mistake the registry " +
    "is here to catch."
  ));

  const path = el("input", "mono");
  path.id = "ws-path";
  path.placeholder = "directory, e.g. D:\\works\\hq";
  path.autocomplete = "off";
  path.spellcheck = false;
  path.value = wsDraft.path;
  path.addEventListener("input", () => { wsDraft.path = path.value; });

  const name = el("input");
  name.id = "ws-name";
  name.placeholder = "name in the picker (optional)";
  name.autocomplete = "off";
  name.value = wsDraft.name;
  name.addEventListener("input", () => { wsDraft.name = name.value; });

  const row = el("div", "ws-add-row");
  row.appendChild(path);
  row.appendChild(name);
  const submit = el("button", "wf-btn approve", "Register");
  submit.type = "submit";
  row.appendChild(submit);
  card.appendChild(row);

  if (wsError) card.appendChild(el("p", "error", wsError));

  card.addEventListener("submit", async (e) => {
    e.preventDefault();
    await wsAdd(path.value, name.value);
  });
  return card;
}

function wsRow(w) {
  const row = el("div", "ws-row");
  const text = el("div", "ws-text");
  text.appendChild(el("span", "ws-name", w.name));
  text.appendChild(el("span", "ws-path mono", w.path));
  row.appendChild(text);

  // A workspace on a removable drive is legitimately absent half the time,
  // so the entry stays (it is still the user's) and says which it is.
  if (!w.exists) row.appendChild(el("span", "badge exited", "missing"));
  const here = wsSessionsIn(w.path);
  if (here.length) {
    row.appendChild(el(
      "span", "badge idle",
      here.length === 1 ? "1 session" : `${here.length} sessions`
    ));
  }

  const rm = el("button", "wf-btn clear", "Unregister");
  rm.addEventListener("click", () => wsRemove(w, here));
  row.appendChild(rm);
  return row;
}

async function wsAdd(rawPath, rawName) {
  wsError = "";
  const path = (rawPath || "").trim();
  if (!path) {
    wsError = "a workspace needs a directory path";
    renderWorkspaces();
    return;
  }
  const body = { path };
  if ((rawName || "").trim()) body.name = rawName.trim();
  try {
    const resp = await api("/api/workspaces", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const doc = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      // The daemon's refusals already say what to do about them (no such
      // directory, name taken by another path) — passing one through beats
      // inventing a vaguer sentence here.
      wsError = doc.error || `HTTP ${resp.status}`;
      renderWorkspaces();
      return;
    }
    wsDraft = { path: "", name: "" };
  } catch (err) {
    wsError = String(err);
    renderWorkspaces();
    return;
  }
  await refreshWorkspaces();
  renderWorkspaces();
}

async function wsRemove(w, here) {
  const running = (here || []).filter((s) => s.status !== "exited");
  const warning = running.length
    ? `\n\n${running.length} session(s) are running there (` +
      `${running.map((s) => s.name).join(", ")}). They keep running: this ` +
      "decides what may be spawned next, not what is already up."
    : "";
  if (!confirm(
    `Unregister workspace '${w.name}'?\n\nThe directory ${w.path} is not ` +
    `touched — only the registry entry goes.${warning}`
  )) return;
  wsError = "";
  try {
    const resp = await api(`/api/workspaces/${encodeURIComponent(w.name)}`, {
      method: "DELETE",
    });
    if (!resp.ok) {
      const doc = await resp.json().catch(() => ({}));
      wsError = doc.error || `HTTP ${resp.status}`;
    }
  } catch (err) {
    wsError = String(err);
  }
  await refreshWorkspaces();
  renderWorkspaces();
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
    link.href = "#/s/" + encodeURIComponent(s);
    meta.appendChild(link);
  }
  view.appendChild(meta);
  if (run.context) view.appendChild(el("p", "wf-context", `context: ${run.context}`));
  for (const w of wf.warnings || []) {
    view.appendChild(el("p", "wf-warning", `⚠ ${w}`));
  }

  const pending = pendingBanner(data, () => refreshWf());
  if (pending) view.appendChild(pending);

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

/* Idle (cwd, scope): offer to start a new run. */
async function renderWfIdle(view, data) {
  view.innerHTML = "";
  view.appendChild(el("p", "wf-note", `no active cflow run in ${data.cwd}`));
  const pending = pendingBanner(data, () => refreshWf());
  if (pending) view.appendChild(pending);
  const box = el("div", "wf-start");
  view.appendChild(box); // present immediately so the poll doesn't rebuild
  await buildStartPanel(box, {
    cwd: data.cwd,
    scope: data.scope || "default",
    sessions: data.sessions || [],
    stillHere: () => wfCwd === data.cwd,
    after: () => { refreshWf(); refreshCflow(); },
  });
}

/* A human's pending start request, with the way to take it back.
   Rendered wherever a slot is shown, because between the request and the
   agent acting on it this is the only sign that anything is coming. */
function pendingBanner(data, after) {
  const req = data.pending_start || (data.run || {}).pending_start;
  if (!req) return null;
  const box = el("div", "wf-pending");
  box.appendChild(el(
    "p", "wf-pending-head",
    `start requested: ${req.name || req.workflow}`
  ));
  if (req.context) box.appendChild(el("p", "wf-pending-ctx", req.context));
  box.appendChild(el(
    "p", "wf-note",
    `asked by ${req.by || "?"} at ${(req.at || "").replace("T", " ")} — the ` +
    `session's agent starts it itself, so it knows what it is running. It ` +
    `picks the request up on its next cflow 'status' call.`
  ));
  const cancel = el("button", "wf-btn clear", "Withdraw request");
  cancel.addEventListener("click", async () => {
    if (!confirm(`Withdraw the pending start of '${req.name || req.workflow}'?`)) return;
    await cflowPost("/api/cflow/request/cancel", {
      cwd: data.cwd, scope: data.scope,
    });
    if (after) after();
    refreshCflow();
  });
  box.appendChild(cancel);
  return box;
}

/* POST a cflow action, surfacing the daemon's error text. Returns the parsed
   body on success, null otherwise. */
async function cflowPost(path, body) {
  try {
    const resp = await api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const doc = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      alert(doc.error || `HTTP ${resp.status}`);
      return null;
    }
    return doc;
  } catch {
    return null; // auth overlay is up
  }
}

/* The workflow picker, shared by the run page and the session page.
   It offers the two creation paths, deliberately unequal:

   - "Ask the agent" writes only a request; the agent performs the start and
     therefore knows the run exists and what it is for. The only path that
     cannot leave an agent driving a run it never read.
   - "Start directly" writes the run here and nudges the terminal. For a slot
     with no live session (an agent that attaches later, a script), where
     there is nobody to ask. */
async function buildStartPanel(box, { cwd, scope, sessions, stillHere, after }) {
  box.dataset.slot = `${scope}|${cwd}`;
  box.appendChild(el("h3", null,
    scope !== "default" ? `Start a workflow — session ${scope}` : "Start a workflow"));

  let flows = [];
  try {
    const resp = await api(`/api/cflow/workflows?cwd=${encodeURIComponent(cwd)}`);
    flows = ((await resp.json()).workflows || []).filter((w) => !w.error);
  } catch { return; }
  if (stillHere && !stillHere()) return; // navigated away while loading
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

  const live = (sessions || []).length > 0;
  const ask = el("button", "wf-btn approve", "Ask the agent to start");
  ask.title = live
    ? `type a request into session ${sessions.join(", ")}; the agent starts it`
    : "no live session in this slot to ask";
  ask.disabled = !live;
  ask.addEventListener("click", async () => {
    const workflow = sel.value;
    if (!confirm(
      `Ask session '${sessions[0]}' to start '${workflow}'?\n\n` +
      `The request is recorded and the session is nudged; its agent reads ` +
      `the request and runs the start itself.`
    )) return;
    const doc = await cflowPost("/api/cflow/request", {
      cwd, scope, workflow, context: ctx.value.trim(),
    });
    if (doc && !(doc.nudged_sessions || []).length) {
      alert(
        "request recorded, but the session could not be nudged — it will " +
        "still be picked up on the agent's next cflow 'status' call"
      );
    }
    if (doc && after) after();
  });

  const direct = el("button", "wf-btn", "Start directly");
  direct.title =
    "write the run now, without waiting for the agent to start it";
  direct.addEventListener("click", async () => {
    const workflow = sel.value;
    if (!confirm(
      `Start '${workflow}' directly?\n\n` +
      (live
        ? "The run is created here and the session is nudged. Its agent has " +
          "NOT read the run yet — if it is mid-task it may keep working for a " +
          "while. Prefer 'Ask the agent to start' when the session is live.\n\n"
        : "This slot has no live session, so nothing will be nudged: the run " +
          "waits for an agent to pick it up.\n\n") +
      "Continue?"
    )) return;
    const doc = await cflowPost("/api/cflow/start", {
      cwd, scope, workflow, context: ctx.value.trim(),
    });
    if (doc && !(doc.nudged_sessions || []).length) {
      alert(
        "run started, but no live session was nudged — tell the agent " +
        "to continue (it picks the run up via the /cflow protocol)"
      );
    }
    if (doc && after) after();
  });

  const row = el("div", "wf-start-row");
  row.append(sel, ctx, ask, direct);
  box.appendChild(row);
  box.appendChild(el(
    "p", "wf-note",
    live
      ? "asking keeps one writer: the agent starts the run, so the run on " +
        "disk and the run it thinks it is driving are the same thing"
      : "no live session here — 'Start directly' is the only path, and the " +
        "run will sit until an agent picks it up"
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
/* session detail page (#/s/<name>/info) — what a session IS          */
/* ------------------------------------------------------------------ */
/* The session list answers "which sessions exist"; the terminal answers
   "what is it doing right now". Neither answers "what is this session" —
   which harness and profile, whose directory, which role, which meshes, and
   above all which cflow run it drives. Four registries hold those answers and
   they only ever met in the operator's head; /api/sessions/<name>/meta
   gathers them, keyed by the one thing they share: the session name. */
let sessName = null;
let sessPollTimer = null;
let sessStartBox = null;  // reused across polls: it holds the user's typing

function stopSessionPoll() {
  if (sessPollTimer) { clearInterval(sessPollTimer); sessPollTimer = null; }
  sessName = null;
  sessStartBox = null;
}

async function openSession(name) {
  if (sessPollTimer) clearInterval(sessPollTimer);
  sessStartBox = null;
  showView("session");
  sessName = name;
  $("sess-view").innerHTML = "<p class='wf-note'>loading…</p>";
  await refreshSession();
  sessPollTimer = setInterval(refreshSession, 2000);
}

async function refreshSession() {
  if (!sessName) return;
  const want = sessName;
  let data;
  try {
    const resp = await api(`/api/sessions/${encodeURIComponent(want)}/meta`);
    data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      $("sess-view").innerHTML = "";
      $("sess-view").appendChild(el(
        "p", "wf-warning",
        // A daemon older than these assets serves the static files from disk
        // but runs the Python it started with, so this route may not exist
        // yet. Say what to do rather than sitting on "loading…".
        data.error || (resp.status === 404
          ? "this daemon has no session-details endpoint — " +
            "'claunch daemon restart' to pick up this version"
          : `cannot load this session (HTTP ${resp.status})`)
      ));
      return;
    }
  } catch {
    return;
  }
  if (sessName !== want) return; // navigated away mid-flight
  renderSession(data);
}

function metaRow(dl, label, value, title) {
  if (value === null || value === undefined || value === "") return;
  dl.appendChild(el("dt", null, label));
  const dd = el("dd", null, String(value));
  if (title) dd.title = title;
  dl.appendChild(dd);
}

function renderSession(data) {
  const view = $("sess-view");
  const s = data.session || {};
  view.innerHTML = "";

  const head = el("div", "wf-head");
  head.appendChild(el("h2", null, s.name || "session"));
  head.appendChild(el("span", `badge ${s.status || ""}`, s.status || "?"));
  const open = el("button", "wf-btn", "Open terminal");
  // Through the router, so the terminal it opens is the one the URL names.
  open.addEventListener("click", () => {
    location.hash = "#/s/" + encodeURIComponent(s.name);
  });
  head.appendChild(open);
  view.appendChild(head);

  const dl = el("dl", "sess-meta");
  metaRow(dl, "harness", s.harness, (data.harness || {}).description);
  metaRow(dl, "profile", s.profile);
  // Whose this session is. Near the top because it changes how everything
  // below it reads: an inherited mesh and a scoped run are the parent's
  // arrangement, not choices this session made.
  metaRow(dl, "spawned by", s.parent, "the session that created this one");
  metaRow(
    dl, "role", data.role ? data.role.name : s.role,
    data.role ? data.role.stance : ""
  );
  metaRow(
    dl, "directory",
    data.workspace ? `${s.cwd}  (workspace ${data.workspace.name})` : s.cwd,
    s.cwd
  );
  metaRow(dl, "conversation", s.conversation_id, "claude --session-id");
  if (s.resume !== null && s.resume !== undefined) {
    metaRow(
      dl, "opened",
      (s.resume === "" ? "conversation picker" : `resume ${s.resume}`) +
      (s.fork_session ? " (forked)" : "")
    );
  }
  if ((s.args || []).length) metaRow(dl, "args", s.args.join(" "));
  const envKeys = Object.keys(s.env || {});
  if (envKeys.length) metaRow(dl, "env", envKeys.join(", "));
  metaRow(dl, "size", `${s.cols}×${s.rows}`);
  metaRow(dl, "restore", s.restore ? "yes (relaunched with the daemon)" : "no");
  metaRow(dl, "pid", s.pid);
  metaRow(dl, "created", (s.created_at || "").replace("T", " "));
  metaRow(dl, "last output", (s.last_output_at || "").replace("T", " "));
  if (s.status === "exited") {
    metaRow(dl, "exited", `${(s.exited_at || "").replace("T", " ")} (code ${s.exit_code ?? "?"})`);
  }
  view.appendChild(dl);

  const meshes = data.meshes || [];
  const meshBox = el("div", "sess-meshes");
  meshBox.appendChild(el("h3", null, `Meshes (${meshes.length})`));
  if (!meshes.length) {
    meshBox.appendChild(el("p", "wf-note", "not a member of any mesh"));
  }
  for (const m of meshes) {
    const chip = el("a", "sess-mesh", `${m.mesh} · ${m.handle} (${m.role})`);
    chip.href = "#/mesh/" + encodeURIComponent(m.mesh);
    chip.title = `${m.members} member(s); joined ${(m.joined_at || "").replace("T", " ")}`;
    meshBox.appendChild(chip);
  }
  view.appendChild(meshBox);

  view.appendChild(sessWorkflow(data));
}

/* The session's cflow slot: a run is keyed by (directory, scope) and the
   scope IS this session's name, so there is exactly one to show. */
function sessWorkflow(data) {
  const box = el("div", "sess-wf");
  box.appendChild(el("h3", null, "Workflow"));
  const flow = data.cflow;
  if (!flow) {
    box.appendChild(el("p", "wf-note", "this session has no working directory"));
    return box;
  }
  const slot = { cwd: flow.cwd, scope: flow.scope, sessions: flow.sessions || [] };

  if (flow.status === "error") {
    box.appendChild(el("p", "wf-warning", flow.error || "cannot read this run"));
    return box;
  }

  const pending = pendingBanner(flow, () => refreshSession());

  if (flow.status && flow.status !== "idle") {
    const line = el("div", "sess-wf-run");
    line.appendChild(el("span", `dot ${wfDotClass(flow.status)}`));
    line.appendChild(el("span", "sess-wf-name", flow.workflow || "(workflow)"));
    line.appendChild(el("span", "meta", flow.status));
    box.appendChild(line);
    if (flow.step_id) {
      box.appendChild(el(
        "p", "wf-note",
        `step: ${flow.title || flow.step_id}` +
        (flow.visit > 1 ? ` · visit ${flow.visit}` : "") +
        ` · ${flow.steps_completed ?? 0} done`
      ));
    }
    if (flow.context) box.appendChild(el("p", "wf-context", `context: ${flow.context}`));
    for (const rep of (flow.reports || []).slice(-3)) {
      const line2 = cflowLine(`${rep.step}: ${rep.summary || ""}`, "report");
      if (rep.details) line2.title = rep.details;
      box.appendChild(line2);
    }
    const link = el("button", "wf-btn approve", "Open the run page");
    link.title = "diagram, step reports, journal, and the human controls";
    link.addEventListener("click", () => {
      location.hash = "#/wf/" + encodeURIComponent(`${flow.scope}|${flow.cwd}`);
    });
    box.appendChild(link);
    if (pending) box.appendChild(pending);
    return box;
  }

  box.appendChild(el("p", "wf-note", `no active cflow run in ${flow.cwd}`));
  if (pending) box.appendChild(pending);
  // Rebuilt only when the slot changes: the poll must not wipe a half-typed
  // context line out from under the user.
  const key = `${slot.scope}|${slot.cwd}`;
  if (sessStartBox && sessStartBox.dataset.slot === key) {
    box.appendChild(sessStartBox);   // appending moves the live node here
    return box;
  }
  sessStartBox = el("div", "wf-start");
  box.appendChild(sessStartBox);
  buildStartPanel(sessStartBox, {
    ...slot,
    stillHere: () => sessName === (data.session || {}).name,
    after: () => { refreshSession(); refreshCflow(); },
  });
  return box;
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
  syncOnboardPickers();
  if (currentPage === "home") renderHome();
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

/* An invite code is base64url JSON {v:2, mesh, machine, token} — decodable
   client-side, so pasting one straight into the mesh field can become a
   fully-formed join with no address typing. */
function decodeInviteCode(raw) {
  const s = (raw || "").trim();
  if (s.length < 24 || /[@\s]/.test(s) || !/^[A-Za-z0-9_-]+=*$/.test(s)) return null;
  try {
    const b64 = s.replace(/-/g, "+").replace(/_/g, "/");
    const doc = JSON.parse(atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4)));
    if (doc && doc.v === 2 && doc.mesh && doc.machine && doc.token) {
      return { mesh: String(doc.mesh), machine: String(doc.machine) };
    }
  } catch { /* not a code — fall through */ }
  return null;
}

/* One field, three verbs: a bare name creates a mesh here, 'mesh@machine'
   asks that machine's daemon to admit one of our sessions, and a pasted
   invite code is redeemed directly (the address comes from the code). */
$("new-mesh").querySelector("input[name=name]").addEventListener("input", (e) => {
  const code = decodeInviteCode(e.target.value);
  const joining = code !== null || e.target.value.includes("@");
  $("mesh-join-extra").classList.toggle("hidden", !joining);
  // the pasted code IS the ticket — the separate code field would be noise
  $("mesh-join-code").classList.toggle("hidden", code !== null);
  const hint = $("mesh-join-hint");
  hint.classList.toggle("hidden", code === null);
  if (code) hint.textContent = `invite ticket for mesh '${code.mesh}' on '${code.machine}'`;
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
  const pasted = decodeInviteCode(name);
  if (pasted || name.includes("@")) {
    const addr = pasted ? `${pasted.mesh}@${pasted.machine}` : name;
    const session = $("mesh-join-session").value;
    if (!session) {
      err.textContent = "no live session to enrol";
      err.classList.remove("hidden");
      return;
    }
    const resp = await api(`/api/mesh/${encodeURIComponent(addr)}/members`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session,
        handle: $("mesh-join-handle").value.trim(),
        code: pasted ? name : $("mesh-join-code").value.trim(),
      }),
    });
    if (!resp.ok) return fail(resp);
    const doc = await resp.json().catch(() => ({}));
    err.classList.add("hidden");
    f.name.value = "";
    $("mesh-join-handle").value = "";
    $("mesh-join-code").value = "";
    $("mesh-join-code").classList.remove("hidden");
    $("mesh-join-hint").classList.add("hidden");
    $("mesh-join-extra").classList.add("hidden");
    f.querySelector("button").textContent = "Create";
    await refreshMeshList();
    if (!doc.pending) location.hash = "#/mesh/" + encodeURIComponent(addr.split("@")[0]);
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
  missingMeshShown = "";   // a different route deserves a fresh verdict
  rolesEditor = "";        // never carry one mesh's open editor into another
  showView("mesh");
  $("mesh-view").innerHTML = "<p class='wf-note'>loading…</p>";
  await refreshMeshView();
  meshPollTimer = setInterval(refreshMeshView, 2000);
}

/* `force` redraws even while a field has focus: picking from the wizard's
   selects leaves the focus right there, and the poll's don't-wipe-input guard
   would otherwise hold back the very list the pick just asked for. */
async function refreshMeshView(force = false) {
  if (!meshName) return;
  // An open role-set editor holds unsaved YAML in a textarea the rebuild
  // below would discard. A poll-driven refresh stands down until it closes;
  // an explicit one (a save, a cancel) still goes through.
  if (rolesEditor && !force) return;
  let info, history, owed;
  try {
    const [r1, r2, r3] = await Promise.all([
      api(`/api/mesh/${encodeURIComponent(meshName)}`),
      api(`/api/mesh/${encodeURIComponent(meshName)}/messages?limit=100`),
      api(`/api/mesh/${encodeURIComponent(meshName)}/owed`),
    ]);
    info = await r1.json();
    if (!r1.ok) {
      renderMissingMesh(meshName, info.error || "cannot load mesh");
      return;
    }
    history = r2.ok ? (await r2.json()).messages || [] : [];
    // A daemon too old to know the route still renders everything else.
    owed = r3.ok ? await r3.json() : null;
    missingMeshShown = "";  // it loaded, so arm the panel again
  } catch {
    return;
  }
  renderRelayBadge(info.relay);
  renderMesh(info, history, force, owed);
}

/* A mesh route can outlive its mesh: a bookmark or a shared link naming a
   mesh this daemon never had, or a mirror that was dropped when the owner
   unlinked us (removing the invited session does that). The route then
   pointed at an error with no way out but editing the URL — every 2s poll
   just repainted it. So the dead end becomes a junction: leave, go to one
   of the meshes that IS here, or make one under that name.

   Rebuilt only when the message changes, or the poll would yank the buttons
   out from under the pointer twice a minute. */
let missingMeshShown = "";

function renderMissingMesh(name, error) {
  const key = `${name} ${error}`;
  if (missingMeshShown === key) return;
  missingMeshShown = key;
  const view = $("mesh-view");
  view.innerHTML = "";
  view.appendChild(el("p", "wf-warning", error));
  const others = (meshCache || []).filter((m) => m.name !== name);
  view.appendChild(el(
    "p", "wf-note",
    "this link names a mesh that is not on this daemon — either it never " +
    "was, or its mirror was dropped (removing the invited session, or being " +
    "unlinked by the owner, does that). " +
    (others.length
      ? "the meshes that are here:"
      : "there are no meshes on this daemon at all.")
  ));
  const row = el("div", "mesh-missing-actions");
  const back = el("button", "wf-btn option", "Back");
  back.addEventListener("click", () => { location.hash = "#"; });
  row.appendChild(back);
  for (const m of others) {
    const link = el("button", "wf-btn option", m.name);
    link.addEventListener("click", () => {
      location.hash = "#/mesh/" + encodeURIComponent(m.name);
    });
    row.appendChild(link);
  }
  const make = el("button", "wf-btn nudge", `Create '${name}' here`);
  make.addEventListener("click", async () => {
    if (!confirm(
      `Create a NEW local mesh called '${name}' on this daemon?\n\n` +
      "This does not rejoin the remote mesh of the same name — to get back " +
      "into that one, the machine that owns it has to invite this daemon " +
      "again (or give you a join ticket)."
    )) return;
    const resp = await api("/api/mesh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (!resp.ok) {
      const doc = await resp.json().catch(() => ({}));
      alert(doc.error || `HTTP ${resp.status}`);
      return;
    }
    missingMeshShown = "";
    await refreshMeshList();
    await refreshMeshView(true);
  });
  row.appendChild(make);
  view.appendChild(row);
}

/* ---- owner-side invitation wizard -------------------------------------- */
/* The complement of joining: instead of minting a ticket and carrying it to
   the other machine by hand, the owner browses the daemons on the relay, picks
   one of their live sessions and pulls it in (POST .../invitations — the
   primary pushes the invitation over the relay and the target joins back).
   Panel state lives here because the 2s poll rebuilds the whole mesh view:
   which machine is chosen, the fetched lists and the typed handle would all
   evaporate otherwise. */
let meshInvitePanels = {};

function invitePanel(mesh) {
  if (!meshInvitePanels[mesh]) {
    meshInvitePanels[mesh] = {
      open: false,
      peers: null,     // null = not fetched yet, [] = fetched and empty
      machine: "",
      sessions: null,
      session: "",
      handle: "",
      role: "",
      note: "",
      bad: false,      // note is an error, not progress
      busy: false,
    };
  }
  return meshInvitePanels[mesh];
}

function inviteFail(st, resp, doc) {
  st.note = doc.error || `HTTP ${resp.status}`;
  st.bad = true;
}

async function loadInvitePeers(mesh) {
  const st = invitePanel(mesh);
  st.note = "listing daemons on the relay…";
  st.bad = false;
  refreshMeshView(true);
  const resp = await api("/api/relay/peers");
  const doc = await resp.json().catch(() => ({}));
  st.peers = doc.peers || [];
  if (!resp.ok) inviteFail(st, resp, doc);
  else {
    st.bad = false;
    st.note = st.peers.length ? "" : "no other daemon is registered on the relay";
  }
  refreshMeshView(true);
}

async function loadInviteSessions(mesh, machine) {
  const st = invitePanel(mesh);
  st.machine = machine;
  st.session = "";
  st.sessions = null;
  st.bad = false;
  st.note = machine ? `asking ${machine} for its live sessions…` : "";
  refreshMeshView(true);
  if (!machine) return;
  const resp = await api(`/api/relay/peers/${encodeURIComponent(machine)}/sessions`);
  const doc = await resp.json().catch(() => ({}));
  if (st.machine !== machine) return; // a newer pick already won
  st.sessions = doc.sessions || [];
  if (!resp.ok) inviteFail(st, resp, doc);
  else {
    st.bad = false;
    st.note = st.sessions.length ? "" : `${machine} has no live session to enrol`;
  }
  refreshMeshView(true);
}

/* Renders into the "Guest daemons" box; primary-only (a mirror owns nothing
   to invite anyone into). `members` filters out sessions already enrolled. */
function renderInviteWizard(info, fed, members) {
  const st = invitePanel(info.name);
  const row = el("div", "mesh-add");
  const toggle = el("button", "wf-btn option",
                    st.open ? "Close" : "Invite a remote session…");
  toggle.addEventListener("click", async () => {
    st.open = !st.open;
    st.note = "";
    st.bad = false;
    if (!st.open) return refreshMeshView(true);
    // Always re-list on open: daemons come and go on the relay, and a stale
    // roster here means picking a machine that is no longer there.
    await loadInvitePeers(info.name);
    if (st.machine && !st.bad) loadInviteSessions(info.name, st.machine);
  });
  row.appendChild(toggle);

  if (st.open) {
    const machineSel = document.createElement("select");
    machineSel.appendChild(el(
      "option", null, st.peers === null ? "loading…" : "machine…"
    ));
    for (const p of st.peers || []) {
      const opt = el("option", null, p);
      opt.value = p;
      machineSel.appendChild(opt);
    }
    machineSel.value = st.machine;
    machineSel.addEventListener("change", () => {
      loadInviteSessions(info.name, machineSel.value);
    });

    // Its daemon may host sessions that already sit in this mesh — offering
    // them again would only earn a 400 from the primary.
    const taken = new Set(
      members.filter((m) => m.machine === st.machine).map((m) => m.session)
    );
    const free = (st.sessions || []).filter((s) => !taken.has(s.name));
    const sessionSel = document.createElement("select");
    sessionSel.appendChild(el("option", null,
      !st.machine ? "pick a machine first"
        : (st.sessions === null ? "loading…" : "session…")));
    for (const s of free) {
      const opt = el("option", null, `${s.name} · ${s.status}`);
      opt.value = s.name;
      sessionSel.appendChild(opt);
    }
    sessionSel.disabled = !free.length;
    sessionSel.value = st.session;
    sessionSel.addEventListener("change", () => {
      st.session = sessionSel.value;
      refreshMeshView(true); // the pick is what un-greys Invite
    });

    const handle = document.createElement("input");
    handle.className = "mesh-plain";
    handle.placeholder = "handle (default: session name)";
    handle.value = st.handle;
    handle.addEventListener("input", () => { st.handle = handle.value; });
    const role = document.createElement("input");
    role.className = "mesh-plain";
    role.placeholder = "role (optional)";
    role.value = st.role;
    role.addEventListener("input", () => { st.role = role.value; });

    const go = el("button", "wf-btn approve", "Invite");
    go.disabled = st.busy || !st.machine || !st.session;
    go.addEventListener("click", async () => {
      const { machine, session } = st;
      st.busy = true;
      st.bad = false;
      st.note = `inviting ${machine}/${session}…`;
      refreshMeshView(true);
      const resp = await api(
        `/api/mesh/${encodeURIComponent(info.name)}/invitations`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            machine, session, handle: st.handle.trim(), role: st.role.trim(),
          }),
        }
      );
      const doc = await resp.json().catch(() => ({}));
      st.busy = false;
      if (!resp.ok) {
        inviteFail(st, resp, doc);
        refreshMeshView(true);
        return;
      }
      const member = doc.member || {};
      // Keep the machine (inviting its siblings next is the common case), drop
      // everything that was about this one session.
      st.open = false;
      st.session = "";
      st.handle = "";
      st.role = "";
      st.sessions = null;
      st.bad = false;
      st.note = `added '${member.handle || session}' (${machine}/${session}) — ` +
        "its daemon now mirrors this mesh and the member was briefed";
      refreshMeshView(true);
      refreshMeshList();
    });
    row.append(machineSel, sessionSel, handle, role, go);
  } else {
    row.appendChild(el(
      "span", "wf-note",
      "enrol a session from another daemon on the relay — nothing to carry over"
    ));
  }
  fed.appendChild(row);
  if (st.note) fed.appendChild(el("p", st.bad ? "wf-warning" : "wf-note", st.note));
}

/* ---- topology diagram --------------------------------------------------- */
/* Three things are true about a mesh at once, and they live at three
   different layers: which daemons are linked (the peer graph), who spawned
   whom (the session tree), and who may message whom (the member graph). One
   picture holds all three, because separate pictures make the reader do the
   join — and the join is where the interesting questions are ("that agent is
   isolated; is that the spawn, or a cut?").

   So: a CLUSTER per daemon, laid out on the rank ring that used to hold bare
   nodes — rank 0 at 12 o'clock, clockwise from there, position still reading
   as precedence. Inside each cluster, its agents as a tidy spawn forest.
   Between clusters, the peer edges, with the four states they always had.

   What is NOT drawn is the point of the design. The member graph is complete
   by default, so drawing every member pair would be n² hairlines saying
   nothing; the information lives in the exceptions. Cuts are drawn (dashed
   red). Everything else answers on demand: click an agent and its reachable
   set lights up. And the transport behind a cross-machine conversation is a
   property of the two DAEMONS, not of the pair of agents — so it belongs on
   the cluster boundary, drawn once, rather than smeared over every member
   pair that crosses it.

   Hand-rolled inline SVG, like the rest of the dashboard — no build step and
   no vendored library. Drawn 1:1 (one SVG unit is one CSS pixel) so the panel
   stays the size the content needs instead of stretching to a fixed canvas. */
const RING = {
  /* Cluster chrome, and the gap the ring must clear between neighbours.
     pad.top holds the machine-name header AND the disc of the first row,
     which hangs half its radius above that row's centre line — too small a
     value here and a long machine name runs under the topmost agents. */
  gap: 42, pad: { x: 26, top: 46, bottom: 14 },
  /* One agent: disc radius, its column, and the row a generation occupies.
     colW is sized for the HANDLE, not the disc — the label is the wide part,
     and clearing only the discs lets names collide. */
  node: 15, colW: 104, rowH: 54,
  /* Room under the deepest row for a disc and the handle hanging below it. */
  leaf: 40,
};
let meshDrag = null;   // {from: machine} while a cluster is being dragged
let meshBusy = false;  // an edit is in flight; suppress the poll's redraw
/* The agent whose reachable set is on show, or null. Module state, not DOM
   state, so the 2s poll rebuilding the whole panel does not drop the
   selection out from under whoever is reading it. */
let meshFocus = null;

/* A drag released anywhere but on a node is a cancel. Registered once, at
   the window, because the node handlers only see drops that land on them —
   without this a stray release would leave meshDrag set and freeze the
   poll's redraw. The node's own pointerup runs first (bubbling), so the
   deferred check only sees genuinely stray releases. */
window.addEventListener("pointerup", () => {
  if (!meshDrag) return;
  setTimeout(() => {
    if (!meshDrag) return;
    meshDrag = null;
    refreshMeshView(true);
  }, 0);
});

/* Chord between neighbours is 2r·sin(pi/n); asking that to span one cell
   gives the radius directly. One cluster sits at the centre. The cell is the
   caller's, because what has to clear is now a whole cluster box and only the
   caller has measured them. */
function ringRadius(count, cell) {
  if (count < 2) return 0;
  return cell / (2 * Math.sin(Math.PI / count));
}

function ringPoint(index, count, radius) {
  // -90deg puts rank 0 at the top; clockwise from there. Centre is (0,0);
  // the viewBox is fitted around the result afterwards.
  const angle = (2 * Math.PI * index) / Math.max(1, count) - Math.PI / 2;
  return { x: radius * Math.cos(angle), y: radius * Math.sin(angle) };
}

function svg(tag, attrs, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  if (text !== undefined) node.textContent = text;
  return node;
}

/* Cut state is per EDGE (the daemon ships the whole table, including edges
   we are not an endpoint of); reachability is per NODE and only observable
   for edges we terminate — nobody can report on a link between two other
   machines, so those draw plain. */
function edgeClass(edge, byName) {
  if (!edge.enabled) return "cut";
  const pa = byName[edge.a] || {}, pb = byName[edge.b] || {};
  if (!pa.self && !pb.self) return "ok";
  const far = pa.self ? pb : pa;
  if (far.ok === false) return "down";
  if (far.queued) return "queued";
  return "ok";
}

async function meshEdit(path, options, what) {
  meshBusy = true;
  try {
    const resp = await api(path, {
      headers: { "Content-Type": "application/json" }, ...options,
    });
    if (!resp.ok) {
      const doc = await resp.json().catch(() => ({}));
      alert(doc.error || `HTTP ${resp.status}`);
      return false;
    }
    return true;
  } finally {
    meshBusy = false;
    await refreshMeshView(true);
    refreshMeshList();
  }
}

/* One place decides what cutting an edge means, so the diagram and the list
   below it cannot drift apart. */
function toggleEdge(info, edge) {
  const { a, b } = edge;
  const enable = !edge.enabled;
  if (!confirm(
    enable
      ? `Restore the direct link ${a} <-> ${b}?`
      : `Cut the direct link ${a} <-> ${b}? Their traffic will go through ` +
        `${info.authority} instead — slower, but nothing is lost.`
  )) return;
  return meshEdit(
    `/api/mesh/${encodeURIComponent(info.name)}/links/` +
    `${encodeURIComponent(a)}/${encodeURIComponent(b)}`,
    { method: "PATCH", body: JSON.stringify({ enabled: enable }) }
  );
}

/* Aiming at a hairline is a poor way to run a network, and edges that pass
   behind a node are barely clickable at all. So the same edits are also a
   plain list: every pair, its state, and the button that changes it. */
function renderLinkEditor(info) {
  const edges = info.links || [];
  const box = el("div", "mesh-links");
  box.appendChild(el("h3", null, "Links"));
  if (!edges.length) return box;
  const self = (info.peers || []).find((p) => p.self);
  const me = self ? self.machine : "";
  for (const edge of edges) {
    const row = el("div", "mesh-member");
    const cls = edgeClass(edge, Object.fromEntries(
      (info.peers || []).map((p) => [p.machine, p])
    ));
    row.appendChild(el("span", `mesh-link-swatch ${cls}`));
    row.appendChild(el(
      "span", "mesh-handle mono",
      `${edge.a} ↔ ${edge.b}`
    ));
    row.appendChild(el("span", "meta", {
      ok: "linked", queued: "linked · traffic queued",
      down: "linked · peer unreachable", cut: "cut — routed via the authority",
    }[cls]));
    if (edge.editable) {
      const btn = el("button", "wf-btn option", edge.enabled ? "Cut" : "Restore");
      btn.addEventListener("click", () => toggleEdge(info, edge));
      row.appendChild(btn);
    } else {
      // Say which of the two rules blocked it, and where the operator can
      // act instead — "disabled" alone is a dead end for whoever is trying
      // to change something.
      const far = edge.a === info.authority ? edge.b : edge.a;
      row.appendChild(el(
        "span", "wf-note",
        !edge.cuttable
          ? (far === me
            ? "carries the log — leave with Remove mesh, or be unlinked "
              + `from ${info.authority}`
            : info.primary === null
              ? `carries the log — unlink ${far} to remove it`
              : `carries the log — ${info.authority} unlinks ${far}`)
          : `not this daemon's edge — edit it on ${info.authority}, `
            + `${edge.a} or ${edge.b}`
      ));
    }
    box.appendChild(row);
  }
  return box;
}

/* Group the roster into one cluster per daemon, in rank order.

   A mesh that never federated has no peer list at all — and that is exactly
   the case where the tree is the whole story, so it gets a single unnamed
   cluster rather than the "nothing to draw yet" the bare ring used to show.
   `machine` is blank on the primary's own members (federation v2), so the
   daemon's own name fills in, matching how the daemon buckets them itself. */
function meshClusters(info) {
  const peers = info.peers || [];
  const order = peers.length
    ? peers.map((p) => p.machine)
    : [info.self || ""];
  const buckets = new Map(order.map((m) => [m, []]));
  for (const m of info.members || []) {
    const home = m.machine || info.self || "";
    buckets.get(buckets.has(home) ? home : order[0]).push(m);
  }
  return order.map((machine, i) => ({
    machine, peer: peers[i] || null, members: buckets.get(machine) || [],
  }));
}

/* The spawn forest inside one cluster: parent handles turned into children
   lists, with anything unreachable from a root promoted to one.

   That promotion is not distrust of the daemon, it is what makes a cycle
   drawable. `parent` resolves to a plain field that a hand-edited
   sessions.json can point in a circle, and a pair pointing at each other sits
   in no root's subtree — so without this the two of them would simply vanish
   from the picture. A wrong-but-visible tree beats a missing agent. */
function meshForest(members) {
  const known = new Set(members.map((m) => m.handle));
  const kids = new Map();
  const roots = [];
  for (const m of members) {
    // A child is always spawned on its parent's own daemon, so a parent that
    // is not in this cluster is lineage gone stale — draw it as a root.
    const p = m.parent && m.parent !== m.handle && known.has(m.parent)
      ? m.parent : null;
    if (!p) { roots.push(m.handle); continue; }
    if (!kids.has(p)) kids.set(p, []);
    kids.get(p).push(m.handle);
  }
  const seen = new Set();
  const walk = (h) => {
    if (seen.has(h)) return;
    seen.add(h);
    for (const c of kids.get(h) || []) walk(c);
  };
  roots.forEach(walk);
  for (const m of members) {
    if (!seen.has(m.handle)) { roots.push(m.handle); walk(m.handle); }
  }
  return { roots, kids };
}

/* Tidy layered layout: depth picks the row, a leaf takes the next free column
   and a parent centres over its children. Deterministic on purpose — the
   panel is rebuilt every 2s, and a force simulation would redraw a slightly
   different picture each time for a graph whose shape we already know. */
function layoutForest(forest) {
  const pos = new Map();
  let col = 0;
  const place = (handle, depth) => {
    if (pos.has(handle)) return null;  // already placed: a cycle led back here
    pos.set(handle, null);             // reserve before recursing
    const xs = (forest.kids.get(handle) || [])
      .map((c) => place(c, depth + 1))
      .filter((x) => x !== null);
    const x = xs.length ? (xs[0] + xs[xs.length - 1]) / 2 : col++;
    pos.set(handle, { x: x * RING.colW, y: depth * RING.rowH });
    return x;
  };
  for (const root of forest.roots) {
    place(root, 0);
    col += 0.55;  // a gap between sibling trees, so two teams read as two
  }
  return pos;
}

/* Lay a cluster out in its own coordinates and measure the box it needs. */
function measureCluster(cluster) {
  const pos = layoutForest(meshForest(cluster.members));
  const nodes = [...pos].map(([handle, p]) => ({ handle, ...p }));
  const xs = nodes.map((n) => n.x);
  const minX = nodes.length ? Math.min(...xs) : 0;
  const maxX = nodes.length ? Math.max(...xs) : 0;
  const maxY = nodes.length ? Math.max(...nodes.map((n) => n.y)) : 0;
  const offX = RING.pad.x + RING.colW / 2 - minX;
  for (const n of nodes) { n.x += offX; n.y += RING.pad.top; }
  return {
    nodes,
    at: new Map(nodes.map((n) => [n.handle, n])),
    w: maxX - minX + RING.colW + 2 * RING.pad.x,
    h: maxY + RING.leaf + RING.pad.top + RING.pad.bottom,
  };
}

/* Who this agent may message, from the member graph the daemon ships whole. */
function meshReachable(info, handle) {
  const out = new Set();
  for (const e of info.member_links || []) {
    if (!e.enabled) continue;
    if (e.a === handle) out.add(e.b);
    else if (e.b === handle) out.add(e.a);
  }
  return out;
}

/* Where the centre-to-centre line leaves a cluster box, so a peer edge stops
   at the boundary instead of running under the agents inside it. */
function boxExit(box, tx, ty) {
  const dx = tx - box.cx, dy = ty - box.cy;
  if (!dx && !dy) return { x: box.cx, y: box.cy };
  const s = Math.min(
    dx ? (box.w / 2) / Math.abs(dx) : Infinity,
    dy ? (box.h / 2) / Math.abs(dy) : Infinity
  );
  return { x: box.cx + dx * s, y: box.cy + dy * s };
}

function topoHint(info, clusters) {
  const focus = meshFocus
    ? ` · showing what ${meshFocus} can reach — click it again to clear`
    : " · click an agent to see who it can reach";
  if (clusters.length < 2) {
    return "one daemon — clusters appear as others join" + focus;
  }
  // Only the authority can edit the graph, so only it is told how.
  return (info.primary === null
    ? "rank 0 holds the authority · drag a cluster onto another to reorder · "
      + "click a link to cut or restore it"
    : `rank 0 holds the authority — edit the graph on ${info.authority}`
  ) + focus;
}

function renderTopology(info) {
  const peers = info.peers || [];
  // A selection outliving the member it named would leave the panel claiming
  // to show the reach of somebody who has left.
  if (meshFocus && !(info.members || []).some((m) => m.handle === meshFocus)) {
    meshFocus = null;
  }
  const clusters = meshClusters(info).map((c) => ({ ...c, ...measureCluster(c) }));
  const box = el("div", "mesh-topo");
  const head = el("div", "mesh-topo-head");
  head.appendChild(el("h3", null, "Topology"));
  head.appendChild(el("span", "wf-note", topoHint(info, clusters)));
  box.appendChild(head);

  // Cluster centres on the rank ring. The cell is the widest box rather than
  // a label, since that is what has to clear now; two clusters sit on a
  // vertical diameter, where only their heights are ever side by side.
  const cell = RING.gap + Math.max(...clusters.map(
    (c) => (clusters.length === 2 ? c.h : Math.max(c.w, c.h))
  ));
  const radius = ringRadius(clusters.length, cell);
  clusters.forEach((c, i) => {
    const p = ringPoint(i, clusters.length, radius);
    c.cx = p.x; c.cy = p.y;
    c.left = p.x - c.w / 2; c.top = p.y - c.h / 2;
  });

  // Absolute position of every agent, so cuts and reachability can cross a
  // cluster boundary without either side knowing about the other's layout.
  const at = new Map();
  for (const c of clusters) {
    for (const n of c.nodes) {
      at.set(n.handle, { x: c.left + n.x, y: c.top + n.y, cluster: c });
    }
  }

  const vb = {
    x: Math.min(...clusters.map((c) => c.left)) - 4,
    y: Math.min(...clusters.map((c) => c.top)) - 4,
  };
  vb.w = Math.max(...clusters.map((c) => c.left + c.w)) - vb.x + 4;
  vb.h = Math.max(...clusters.map((c) => c.top + c.h)) - vb.y + 4;
  const canvas = svg("svg", {
    viewBox: `${vb.x} ${vb.y} ${vb.w} ${vb.h}`,
    width: Math.round(vb.w), height: Math.round(vb.h),
    class: "mesh-ring" + (meshFocus ? " focusing" : ""),
  });
  // Clicking anywhere that is not an agent or a link clears the selection —
  // the gesture people try first, and the cluster boxes cover most of the
  // panel, so waiting for a click on bare canvas would rarely fire.
  canvas.addEventListener("click", (ev) => {
    if (!meshFocus || ev.target.closest(".mesh-agent, .mesh-edge-group")) return;
    meshFocus = null;
    refreshMeshView(true);
  });

  const byMachine = {};
  for (const p of peers) byMachine[p.machine] = p;
  const byName = Object.fromEntries(clusters.map((c) => [c.machine, c]));

  /* 1. cluster boxes, behind everything they contain */
  clusters.forEach((c, i) => {
    const rank = c.peer ? c.peer.rank : 0;
    const g = svg("g", {
      class: "mesh-cluster"
        + (c.peer && c.peer.self ? " self" : "")
        + (rank === 0 && c.peer ? " authority" : "")
        + (c.peer && c.peer.ok === false ? " down" : "")
        + (meshDrag && meshDrag.from === c.machine ? " dragging" : ""),
    });
    g.appendChild(svg("rect", {
      x: c.left, y: c.top, width: c.w, height: c.h, rx: 10,
      class: "mesh-cluster-box",
    }));
    const label = c.machine || "this daemon";
    g.appendChild(svg(
      "text", { x: c.left + 12, y: c.top + 19, class: "mesh-cluster-name" },
      (c.peer ? (rank === 0 ? "★ " : `${rank} · `) : "")
        + (label.length > 20 ? `${label.slice(0, 19)}…` : label)
    ));
    if (!c.members.length) {
      g.appendChild(svg(
        "text",
        { x: c.cx, y: c.cy + 6, class: "mesh-cluster-empty" },
        "no agents"
      ));
    }
    const marks = [];
    if (c.peer) {
      marks.push(`rank ${rank}`, rank === 0 ? "authority" : "peer");
      if (c.peer.self) marks.push("this daemon");
      if (c.peer.ok === false) marks.push(`unreachable: ${c.peer.error}`);
      if (c.peer.queued) marks.push(`${c.peer.queued} queued`);
    }
    marks.push(`${c.members.length} agent${c.members.length === 1 ? "" : "s"}`);
    g.appendChild(svg("title", {}, `${label} — ${marks.join(" · ")}`));

    // Reordering is the authority's call, so only it offers the gesture. As
    // before, no setPointerCapture: capturing would route the release back to
    // the cluster the drag started on and no drop could ever land.
    if (info.primary === null && clusters.length > 1) {
      g.classList.add("draggable");
      g.addEventListener("pointerdown", () => { meshDrag = { from: c.machine }; });
      g.addEventListener("pointerup", () => {
        const from = meshDrag && meshDrag.from;
        meshDrag = null;
        if (!from || from === c.machine) return refreshMeshView(true);
        const order = peers.map((q) => q.machine).filter((m) => m !== from);
        order.splice(i, 0, from);
        if (order[0] !== info.authority && !confirm(
          `Hand the mesh's authority to '${order[0]}'? It takes over ` +
          "sequencing, the roster and the policy engine."
        )) return refreshMeshView(true);
        meshEdit(
          `/api/mesh/${encodeURIComponent(info.name)}/peers`,
          { method: "PUT", body: JSON.stringify({ order }) }
        );
      });
    }
    canvas.appendChild(g);
  });

  /* 2. peer edges — the transport behind every conversation that crosses a
        machine boundary, drawn once at the boundary rather than smeared over
        each pair of agents that uses it. */
  for (const edge of info.links || []) {
    const a = byName[edge.a], b = byName[edge.b];
    if (!a || !b) continue;
    const cls = edgeClass(edge, byMachine);
    const ea = boxExit(a, b.cx, b.cy), eb = boxExit(b, a.cx, a.cy);
    const ends = { x1: ea.x, y1: ea.y, x2: eb.x, y2: eb.y };
    const group = svg("g", { class: `mesh-edge-group ${cls}` });
    // A 1.6px stroke is far too thin to aim at (and a horizontal one has a
    // zero-height box), so a transparent fat line underneath does the
    // hit-testing while the visible one stays hairline.
    group.appendChild(svg("line", { ...ends, class: "mesh-edge-hit" }));
    group.appendChild(svg("line", { ...ends, class: `mesh-edge ${cls}` }));
    group.appendChild(svg("title", {}, `${edge.a} <-> ${edge.b} — ${cls}`));
    if (edge.editable) {
      group.classList.add("editable");
      group.addEventListener("click", () => toggleEdge(info, edge));
    }
    canvas.appendChild(group);
  }

  /* 3. spawn edges, inside their cluster */
  for (const m of info.members || []) {
    const child = at.get(m.handle), parent = m.parent && at.get(m.parent);
    if (!child || !parent || parent.cluster !== child.cluster) continue;
    // An elbow rather than a diagonal: with several children the fan of
    // straight lines is hard to follow back to one parent.
    const mid = (parent.y + child.y) / 2;
    canvas.appendChild(svg("path", {
      class: "mesh-spawn",
      d: `M ${parent.x} ${parent.y} V ${mid} H ${child.x} V ${child.y}`,
    }));
  }

  /* 4. member-graph exceptions. Connected is the default and says nothing;
        a cut is a decision somebody made, so a cut is what gets a line. */
  for (const e of info.member_links || []) {
    if (e.enabled) continue;
    const a = at.get(e.a), b = at.get(e.b);
    if (!a || !b) continue;
    const g = svg("g", { class: "mesh-mcut" });
    g.appendChild(svg("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y }));
    g.appendChild(svg("title", {}, `${e.a} ✕ ${e.b} — cannot message each other`));
    canvas.appendChild(g);
  }

  /* 5. the answer to "who can this one talk to", on demand */
  const reachable = meshFocus ? meshReachable(info, meshFocus) : new Set();
  if (meshFocus && at.has(meshFocus)) {
    const from = at.get(meshFocus);
    for (const handle of reachable) {
      const to = at.get(handle);
      if (!to) continue;
      canvas.appendChild(svg("line", {
        class: "mesh-reach", x1: from.x, y1: from.y, x2: to.x, y2: to.y,
      }));
    }
  }

  /* 6. the agents themselves, over everything */
  for (const m of info.members || []) {
    const p = at.get(m.handle);
    if (!p) continue;
    const lit = !meshFocus || m.handle === meshFocus || reachable.has(m.handle);
    const g = svg("g", {
      class: "mesh-agent " + meshDotClass(m.reachability)
        + (m.handle === meshFocus ? " focus" : "")
        + (lit ? "" : " dim"),
      transform: `translate(${p.x} ${p.y})`,
    });
    g.appendChild(svg("circle", { r: RING.node, class: "mesh-agent-disc" }));
    g.appendChild(svg(
      "text", { class: "mesh-agent-name", y: RING.node + 14 },
      m.handle.length > 14 ? `${m.handle.slice(0, 13)}…` : m.handle
    ));
    const owed = m.owed ? `${m.owed} unanswered` : null;
    g.appendChild(svg("title", {}, [
      `${m.handle} (${m.role})`, m.session, m.reachability,
      m.parent ? `spawned by ${m.parent}` : "not spawned by a member",
      owed,
    ].filter(Boolean).join(" · ")));
    g.addEventListener("click", () => {
      meshFocus = meshFocus === m.handle ? null : m.handle;
      refreshMeshView(true);
    });
    canvas.appendChild(g);
  }
  box.appendChild(canvas);

  const legend = el("div", "mesh-legend");
  for (const [cls, label] of [
    ["ok", "linked"], ["queued", "queued"],
    ["down", "unreachable"], ["cut", "cut"],
    ["spawn", "spawned"], ["mcut", "cannot message"],
  ]) {
    const item = el("span", "mesh-legend-item");
    item.appendChild(el("i", `mesh-legend-swatch ${cls}`));
    item.appendChild(el("span", null, label));
    legend.appendChild(item);
  }
  box.appendChild(legend);
  return box;
}

/* The send/add forms must survive the 2s poll: rebuild everything except a
   form the user is currently typing in. */
function formInUse(root) {
  return root.contains(document.activeElement) &&
    ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
}

function renderMesh(info, history, force, owed) {
  const view = $("mesh-view");
  if (!force && formInUse(view)) return; // don't wipe in-progress input
  // ...nor yank the node out from under a drag, or race an in-flight edit
  if (!force && (meshDrag || meshBusy)) return;
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

  // the graph leads; the boxes below own the text-level detail and the forms
  view.appendChild(renderTopology(info));
  if ((info.links || []).length) view.appendChild(renderLinkEditor(info));

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
      where.addEventListener("click", () => {
        location.hash = "#/s/" + encodeURIComponent(m.session);
      });
    }
    // 'pending' is mail the daemon has not managed to deliver; 'owed' is mail
    // it delivered that the agent never answered. Different faults, so the
    // row names both rather than one "behind" number.
    const state = el("span", "meta", m.reachability +
      (m.pending ? ` · ${m.pending} pending` : "") +
      (m.owed ? ` · ${m.owed} unanswered` : ""));
    if (m.owed) state.classList.add("mesh-owes");
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

  view.appendChild(renderMeshOwed(info, owed));

  // membership from other machines: who joined us (guests) or who owns us
  const fed = el("div", "mesh-fed");
  fed.appendChild(el("h3", null, "Peer daemons"));
  const peers = (info.peers || []).filter((p) => !p.self);
  if (!peers.length) {
    fed.appendChild(el(
      "p", "wf-note",
      "no other machine has joined yet — invite one below, or let it ask with " +
      `'claunch mesh join ${info.name}@${selfMachine || "<this-machine>"}' ` +
      "and approve it here (both daemons need a relay uplink)"
    ));
  }
  for (const p of peers) {
    // Since phase 7 the peer list is the whole rank list, ourselves
    // included — the diagram wants that, this box does not.
    if (p.self) continue;
    const row = el("div", "mesh-member");
    const ok = p.ok === true;
    const state = p.ok === false ? `unreachable — ${p.error || "?"}` :
      (ok ? "ok" : "linked, no traffic yet");
    row.appendChild(el("span", `dot ${ok ? "idle" : (p.ok === false ? "exited" : "starting")}`));
    row.appendChild(el("span", "mesh-handle mono", p.machine));
    row.appendChild(el("span", "mesh-role", `rank ${p.rank} · ${p.role || ""}`));
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
    renderInviteWizard(info, fed, members);
    const fedRow = el("div", "mesh-add");
    const inviteBtn = el("button", "wf-btn option", "Mint invite ticket…");
    const codeOut = document.createElement("input");
    codeOut.readOnly = true;
    codeOut.placeholder =
      "single-use ticket appears here — pre-approves one unattended join";
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
    const cmdBase =
      `claunch mesh join ${info.name}@${selfMachine || "<this-machine>"} --code`;
    if (meshInviteCodes[info.name]) {
      // the whole redeem command, ticket included — one click to carry over
      const cmdRow = el("div", "mesh-add");
      const cmd = document.createElement("input");
      cmd.readOnly = true;
      cmd.className = "mono";
      cmd.value = `${cmdBase} ${meshInviteCodes[info.name]}`;
      cmd.addEventListener("focus", () => cmd.select());
      const copyBtn = el("button", "wf-btn option", "Copy command");
      copyBtn.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(cmd.value);
        } catch {
          cmd.select();
          document.execCommand("copy");
        }
        copyBtn.textContent = "Copied";
      });
      cmdRow.append(cmd, copyBtn);
      fed.appendChild(cmdRow);
    } else {
      fed.appendChild(el(
        "p", "wf-note", `redeemed there with: ${cmdBase} <ticket>`
      ));
    }
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
  view.appendChild(renderMeshRoles(info));

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
  text.placeholder =
    "message (Ctrl+Enter to send) — delivered by typing into the recipient's terminal";
  text.rows = 3;
  const sendBtn = el("button", "wf-btn approve", "Send");
  const submitMsg = async () => {
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
  };
  sendBtn.addEventListener("click", submitMsg);
  text.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      submitMsg();
    }
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

function fmtAge(secs) {
  if (secs === null || secs === undefined) return "?";
  secs = Math.floor(secs);
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  const h = Math.floor(secs / 3600);
  return `${h}h${String(Math.floor((secs % 3600) / 60)).padStart(2, "0")}m`;
}

/* Unanswered mail: the mesh's silence detector.

   A mesh fails quietly. A member that was asked something and simply said
   nothing looks exactly like a member with nothing to say — the message log
   scrolls on either way, and until now the only thing that noticed was the
   heartbeat nudge, which says nothing to the operator. This box is that state
   made visible: who was asked, what they were asked, and how long ago.

   It reports the SAME debt the nudger chases (Mesh.owed follows the policy
   engine's resolution rule exactly), so "listed here" and "being nudged" can
   never disagree — except when the nudge is switched off, which is called out
   in as many words, because an unattended list reads as a handled one. */
function renderMeshOwed(info, report) {
  const box = el("div", "mesh-owed");
  const rows = (report && report.members) || [];
  const owing = rows.filter((r) => (r.owed || 0) > 0);
  const total = (report && report.owed) || 0;
  box.appendChild(el("h3", null, `Unanswered${total ? ` (${total})` : ""}`));
  if (!report) {
    box.appendChild(el("p", "wf-note", "the daemon did not report unanswered mail"));
    return box;
  }
  if (!owing.length) {
    box.appendChild(el(
      "p", "wf-note",
      rows.length
        ? `every member has answered its mail (${rows.length} member${
            rows.length === 1 ? "" : "s"})`
        : "no members yet"
    ));
    return box;
  }
  box.appendChild(el(
    "p", "wf-note",
    "asked something, said nothing back — a reply of any kind clears a " +
    "member, so what is left is mail nobody has acknowledged at all"
  ));
  for (const r of owing) {
    const head = el("div", "mesh-owed-head");
    head.appendChild(el("span", `dot ${meshDotClass(r.reachability)}`));
    head.appendChild(el("span", "mesh-handle", r.handle));
    head.appendChild(el("span", "mesh-role", r.role));
    head.appendChild(el("span", "mesh-owed-count", `${r.owed} unanswered`));
    if (r.oldest_age !== null && r.oldest_age !== undefined) {
      head.appendChild(el("span", "meta", `oldest ${fmtAge(r.oldest_age)} ago`));
    }
    if (r.pending) {
      head.appendChild(el("span", "meta", `· ${r.pending} still undelivered`));
    }
    if (r.local) {
      // The real diagnostic move is to go look at the session.
      const open = el("span", "mesh-session linkish", "open terminal");
      open.addEventListener("click", () => {
        location.hash = "#/s/" + encodeURIComponent(r.session);
      });
      head.appendChild(open);
    } else {
      const badge = el(
        "span", "mesh-owed-remote",
        r.stale ? `${r.machine} — report is stale` : `counted on ${r.machine}`
      );
      if (r.stale) badge.classList.add("stale");
      head.appendChild(badge);
    }
    box.appendChild(head);
    for (const m of r.messages || []) {
      const line = el("div", "mesh-owed-msg");
      const meta = el("div", "mesh-msg-meta");
      meta.appendChild(el("span", "mesh-msg-from", m.from));
      if (m.type && m.type !== "say") {
        meta.appendChild(el("span", "mesh-msg-type", m.type));
      }
      if (m.batch) meta.appendChild(el("span", "mesh-msg-type", "your slice"));
      meta.appendChild(el("span", "mono", m.id));
      meta.appendChild(el("span", "mesh-msg-at", `${fmtAge(m.age)} ago`));
      line.appendChild(meta);
      line.appendChild(el("div", "mesh-msg-body", m.body || ""));
      box.appendChild(line);
    }
    if (!r.local && !(r.messages || []).length) {
      box.appendChild(el(
        "p", "wf-note",
        `${r.machine} counts this member's mail; open that daemon's mesh page ` +
        "for the messages themselves"
      ));
    }
  }
  const hb = report.heartbeat || {};
  if (!hb.enabled) {
    box.appendChild(el(
      "p", "mesh-owed-warn",
      "the heartbeat nudge is OFF for this mesh — nothing is chasing these; " +
      "switch it on under Nudge policy, or message the member yourself below"
    ));
  } else if (report.engine && info.primary) {
    box.appendChild(el(
      "p", "wf-note",
      `the nudge engine runs on the primary daemon (${info.primary})`
    ));
  }
  return box;
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
  // Empty is not "no message": it means the ROLE's own task_poll text, which
  // is where a custom vocabulary carries its wording.
  tpBody.placeholder = `(blank — use the ${firstRole} role's own text)`;
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

/* The mesh's role set: which roles its handles resolve into, and the YAML
   that says so. The vocabulary is the authority's — a mirror's edit is
   forwarded there — so this panel is live on every daemon in the mesh.

   The editor is opened on demand rather than rendered with the page: the
   YAML is a separate fetch (the 2s poll only carries role NAMES, so stance
   prose never rides it), and a textarea that rebuilt itself every two
   seconds would throw away whatever was being typed. `rolesEditor` holds the
   open editor's mesh so the poll leaves it alone. */
let rolesEditor = "";

function renderMeshRoles(info) {
  const roles = info.roles || {};
  const box = el("div", "mesh-roles");
  box.appendChild(el("h3", null, "Roles"));
  box.appendChild(el(
    "p", "wf-note",
    (roles.custom ? "this mesh's own vocabulary" : "the packaged vocabulary") +
    ` · default role: ${roles.default || "?"} · a handle's leading word ` +
    "picks its role, and changing this is never retroactive"
  ));
  const chips = el("div", "mesh-role-chips");
  const held = {};
  for (const m of info.members || []) held[m.role] = (held[m.role] || 0) + 1;
  for (const name of roles.names || []) {
    const chip = el("span", "mesh-role-chip", `${name} · ${held[name] || 0}`);
    if (held[name]) chip.classList.add("held");
    chips.appendChild(chip);
  }
  // A role a member still holds but the vocabulary no longer defines. Not an
  // error — it is what "not retroactive" looks like from the outside.
  for (const name of Object.keys(held).sort()) {
    if ((roles.names || []).includes(name)) continue;
    const chip = el("span", "mesh-role-chip orphan", `${name} · ${held[name]}`);
    chip.title = "held by a member but no longer defined — it matches no rule";
    chips.appendChild(chip);
  }
  box.appendChild(chips);

  if (rolesEditor !== info.name) {
    const edit = el("button", "wf-btn", "Edit role set");
    edit.addEventListener("click", () => { rolesEditor = info.name; refreshMeshView(true); });
    box.appendChild(edit);
    return box;
  }

  const area = document.createElement("textarea");
  area.className = "mesh-roles-yaml";
  area.value = "loading…";
  area.disabled = true;
  box.appendChild(area);
  api(`/api/mesh/${encodeURIComponent(info.name)}/roles`)
    .then((r) => r.json())
    .then((doc) => {
      area.value = doc.yaml || "";
      area.disabled = false;
    })
    .catch(() => { area.value = "(could not load the role set)"; });

  const actions = el("div", "mesh-roles-actions");
  const url = `/api/mesh/${encodeURIComponent(info.name)}/roles`;
  // Deliberately NOT meshEdit: that refreshes unconditionally in its finally,
  // which would rebuild this textarea and throw away the author's text on the
  // very path where they need it most — a rejected upload, whose error names
  // the line to fix. So the editor closes only after a save that took.
  const put = async (body) => {
    const resp = await api(url, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const doc = await resp.json().catch(() => ({}));
    if (!resp.ok) { alert(doc.error || `HTTP ${resp.status}`); return; }
    rolesEditor = "";
    await refreshMeshView(true);
  };
  const save = el("button", "wf-btn approve", "Save role set");
  save.addEventListener("click", () => put({ yaml: area.value }));
  const reset = el("button", "wf-btn", "Reset to default");
  reset.addEventListener("click", () => {
    if (!confirm(
      "Drop this mesh's role set and go back to the packaged one?\n\n" +
      "Members keep the role they joined with — this is not retroactive."
    )) return;
    return put({ yaml: null });
  });
  const cancel = el("button", "wf-btn", "Cancel");
  cancel.addEventListener("click", () => { rolesEditor = ""; refreshMeshView(true); });
  actions.append(save, reset, cancel);
  box.appendChild(actions);
  if (!roles.is_authority) {
    box.appendChild(el(
      "p", "wf-note",
      `${info.authority} owns the vocabulary — your edit is forwarded there ` +
      "and comes back to every daemon in the mesh"
    ));
  }
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
  refreshHarnesses();
  refreshRoles();
  refreshWorkspaces();
  refreshSessions();
  refreshMeshList();
  refreshCflow();
  // Last, so the page it lands on renders against caches the refreshes
  // above have already filled. A #/s/<name> link attaches here — which is
  // why a reload puts you back in the session instead of at an empty slot.
  route();
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    refreshSessions();
    refreshMeshList();
    refreshCflow();
    // Polled because the registry is edited from the CLI, in another window;
    // it redraws only when the list really changed (see refreshWorkspaces).
    refreshWorkspaces();
  }, 2000);
}

boot();
