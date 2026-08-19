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
  const overlay = $("auth-overlay");
  // Focus only as it opens. The poll can raise this repeatedly (every tick
  // while a rotated token goes unfixed), and a caret yanked back to the start
  // of the box every two seconds makes the token unpasteable.
  const opening = overlay.classList.contains("hidden");
  overlay.classList.remove("hidden");
  if (opening) $("auth-token").focus();
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

/* The rail's bulk bar. Four verbs that act on every session at once, each
   labelled with the number it would touch and hidden when that number is
   zero — so the bar is a reading of the rail rather than a fixed row of
   controls, half of which would do nothing on any given rail.

   They are four and not two because the pairs differ in what survives. Stop
   ends the programs and keeps the records, so the rail comes back with
   `resume`; clear and delete are the ones that make a session unresumable,
   which is why they are the ones that ask, and why they are coloured like it.
   Exited sessions are kept indefinitely for exactly that reason: dropping
   them is the user's call, in bulk, from here. */
function syncBulkActions(sessions) {
  const live = sessions.filter((s) => s.status !== "exited").length;
  const dead = sessions.length - live;
  const set = (id, n, label, title) => {
    const btn = $(id);
    if (!btn) return;  // an older index.html served by a newer daemon
    btn.classList.toggle("hidden", n === 0);
    btn.textContent = label;
    btn.title = title;
  };
  set("stop-all", live, `■ stop ${live}`,
      "kill the program in every running session — the records stay, so each "
      + "one can be resumed afterwards");
  set("resume-all", dead, `▶ resume ${dead}`,
      "relaunch every exited session under its own name and conversation");
  set("clear-exited", dead, `clear ${dead} exited`,
      "forget the records of the exited sessions — they can no longer be "
      + "resumed here. Running sessions are untouched");
  set("delete-all", sessions.length, `✕ delete all ${sessions.length}`,
      "stop every running session and forget every record");
  // The bar's own border would otherwise sit above the nav as a stray rule on
  // a rail with nothing on it.
  const bar = $("bulk-actions");
  if (bar) bar.classList.toggle("hidden", sessions.length === 0);
}

/* A bulk call answers with what it did *and* with what it did not: a record
   held back because a mesh row still names it, a session that would not come
   back. Either one is why the rail a second later does not match the count on
   the button, and an omission the button never mentions reads as the button
   not having worked — the next click is someone trying harder. So both are
   said out loud, once, here. */
function reportBulk(result, verb) {
  const kept = (result && result.kept) || [];
  const failed = (result && result.failed) || [];
  const parts = [];
  if (kept.length) {
    parts.push(
      `Kept ${kept.length} record(s) still named by a mesh:\n` +
      kept.map((k) => `  ${k.name} — ${k.meshes.map((m) => m.mesh).join(", ")}`)
        .join("\n") +
      `\n\nRemove them from the mesh first (the roster's ×), then ${verb} again.`
    );
  }
  if (failed.length) {
    parts.push(
      `${failed.length} session(s) could not ${verb}:\n` +
      failed.map((f) => `  ${f.name} — ${f.error}`).join("\n")
    );
  }
  if (parts.length) alert(parts.join("\n\n"));
}

/* Send one, keeping its button pressed-out for the duration: these are slow
   (delete waits every running child out) and they are not idempotent, so a
   second click while the first is in flight is the one thing to prevent. */
async function bulkAction(btn, path, opts, verb) {
  btn.disabled = true;
  try {
    const resp = await api(path, opts);
    const result = await resp.json().catch(() => null);
    if (!resp.ok) {
      alert((result && result.error) || `HTTP ${resp.status}`);
      return null;
    }
    reportBulk(result, verb);
    return result;
  } catch {
    return null;  // the auth overlay is up; api() has already raised it
  } finally {
    btn.disabled = false;
  }
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
    // The row attaches — that is what the session is doing. This opens what
    // it *is* (definition, meshes, its cflow run) beside it, so the two are
    // not two places you have to travel between.
    const info = document.createElement("button");
    info.className = "sess-info";
    info.type = "button";
    info.dataset.name = s.name;
    if (s.name === sessName) info.classList.add("on");
    info.textContent = "ⓘ";
    info.title = "session details: harness, directory, meshes, workflow";
    info.addEventListener("click", (e) => {
      e.stopPropagation();   // the row itself attaches; this button does not
      openDetail(s.name);
    });
    li.append(dot, label, meta, info);
    li.addEventListener("click", () => {
      location.hash = "#/s/" + encodeURIComponent(s.name);
    });
    list.appendChild(li);
  }
  refreshResumeChoices();  // the spawn form offers these same conversations
  if (currentPage === "home") renderHome();
  syncBulkActions(sessionsCache);

  const cur = currentName && sessionsCache.find((s) => s.name === currentName);
  if (cur && attachedPid && cur.pid !== attachedPid && linkState === "live") {
    // Someone else (a `claunch respawn`, another tab) resumed this session:
    // our socket is bound to the replaced, now-dead child, so follow the new
    // one instead of showing its frozen last screen. Only while the terminal
    // is the visible view — reattaching must not yank the user off a
    // workflow page, or out of the mobile menu (it stays pending until they
    // come back, since the stale pid keeps failing this test).
    //
    // Only from a live link, too. This used to be the *only* way a dropped
    // socket ever came back, which it was bad at; now the link repairs itself
    // and this is once more about the child being replaced under a working
    // socket — a question a broken one has no opinion on.
    if (terminalOnScreen()) attach(currentName);
  } else if (cur && linkState !== "live") {
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
  // Delegated: stopped, but not on anything the operator has to do. Its own
  // colour, because painting it the same amber as a gate would grow a queue
  // of things that look like work and are not.
  if (status === "waiting_answer") return "wf-delegated";
  if (status === "waiting_approval" || status === "waiting_selection" || status === "report_required") return "wf-waiting";
  if (status === "done") return "wf-done";
  if (status === "error" || status === "aborted") return "wf-error";
  return "wf-running";
}

/* Who an open ask is with, in a few words. */
function askWho(ask) {
  const asked = (ask && ask.asked) || [];
  if (!asked.length) return "nobody — it fell to you";
  return asked.map((e) => e.handle || e.kind).join(", ");
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
    // A run is keyed by (directory, session), so a team working one workflow
    // in one tree makes cards that differ ONLY by the session. That makes the
    // session part of the run's name here, not a decoration beside it — the
    // whole point of this list is picking the right one of them.
    const scoped = r.scope && r.scope !== "default";
    name.textContent = scoped
      ? `${r.workflow || "(workflow)"} · ${r.scope}`
      : (r.workflow || "(workflow)");
    const st = document.createElement("span");
    st.className = "meta";
    st.textContent =
      r.status === "waiting_approval" && r.reason === "loop_limit"
        ? "loop limit"
        : r.status === "waiting_approval" && r.reason === "declined"
        ? "declined"
        : r.status === "waiting_answer"
        ? `with ${askWho(r.ask)}`
        : r.status;
    head.append(dot, name, st);
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
    // Always, not only while the session is alive: a run whose session has
    // exited is exactly the one a human mistakes for someone else's, and the
    // exited session is still there to attach (it resumes).
    if (scoped) {
      const live = (r.sessions || []).includes(r.scope);
      const sess = cflowLine(
        `session: ${r.scope}${live ? "" : " (not running)"}`, "dim"
      );
      sess.classList.add("linkish");
      sess.title = live
        ? "attach the session's terminal"
        : "this run's session is not running — open it to resume";
      sess.addEventListener("click", (e) => {
        e.stopPropagation();
        location.hash = "#/s/" + encodeURIComponent(r.scope);
      });
      li.appendChild(sess);
    }

    if (r.status === "waiting_answer") {
      li.appendChild(cflowLine(`waiting on ${askWho(r.ask)} to decide`));
      if (r.ask && r.ask.deadline) {
        li.appendChild(cflowLine(`moves on after ${r.ask.deadline}`));
      }
    } else if (r.status === "waiting_approval") {
      if (r.reason === "declined" && r.declined) {
        li.appendChild(cflowLine(
          `${r.declined.by} declined — ${r.declined.reason || "no reason given"}`
        ));
      }
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

$("term-details").addEventListener("click", () => openDetail(currentName));

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

/* Stop everything. The records stay and every one of them is resumable after,
   which is what makes this the one bulk verb that needs no mesh guard: a
   member row is *meant* to outlive its terminal, reading `exited`. */
$("stop-all").addEventListener("click", async () => {
  const live = sessionsCache
    .filter((s) => s.status !== "exited").map((s) => s.name);
  if (!live.length) return;
  if (!confirm(
    `Stop ${live.length} running session(s)?\n\n${live.join(", ")}\n\n` +
    `The program in each one is terminated. Their records stay, so all of ` +
    `them can be resumed from here afterwards.`
  )) return;
  await bulkAction($("stop-all"), "/api/sessions/kill", { method: "POST" }, "stop");
  // The open terminal's own socket sees its child go before the next poll
  // does, so there is nothing to reattach here — only the rail to redraw.
  refreshSessions();
});

/* Bring everything back. Each respawn replaces its session's child, so the one
   this tab is watching has to be picked up again by hand: the poll only
   notices a swapped pid from a socket that is still live, and ours died with
   the child it was bound to. */
$("resume-all").addEventListener("click", async () => {
  const dead = sessionsCache
    .filter((s) => s.status === "exited").map((s) => s.name);
  if (!dead.length) return;
  if (!confirm(
    `Resume ${dead.length} exited session(s)?\n\n${dead.join(", ")}\n\n` +
    `Each comes back under its own name — the claude harness with --resume of ` +
    `the conversation it was pinned to.`
  )) return;
  const result = await bulkAction(
    $("resume-all"), "/api/sessions/respawn", { method: "POST" }, "resume"
  );
  const back = (result && result.respawned) || [];
  detach();
  await refreshSessions();
  if (currentName && back.includes(currentName)) attach(currentName);
});

$("clear-exited").addEventListener("click", async () => {
  const dead = sessionsCache.filter((s) => s.status === "exited").map((s) => s.name);
  if (!dead.length) return;
  if (!confirm(
    `Drop the records of ${dead.length} exited session(s)?\n\n${dead.join(", ")}\n\n` +
    `They can no longer be resumed. Running sessions are untouched.`
  )) return;
  // A record a mesh row still names is kept, not dropped — the two are one
  // fact, and half of it left behind is a member nobody can respawn or reach.
  // reportBulk() is what says so, for this button and for delete alike.
  const result = await bulkAction(
    $("clear-exited"), "/api/sessions", { method: "DELETE" }, "clear"
  );
  dropIfGone(result, dead);
  refreshSessions();
});

/* The whole rail, gone: running sessions stopped and waited out, then every
   record forgotten. One call rather than stop-all followed by clear, because
   a session that has just been signalled is not yet `exited` — a clear sent
   straight after it would skip exactly the sessions it was meant to remove. */
$("delete-all").addEventListener("click", async () => {
  const all = sessionsCache.map((s) => s.name);
  if (!all.length) return;
  const live = sessionsCache.filter((s) => s.status !== "exited").length;
  if (!confirm(
    `Delete all ${all.length} session(s)?\n\n${all.join(", ")}\n\n` +
    (live ? `${live} of them are still running and are stopped first. ` : "") +
    `The daemon then forgets every record, so none of them can be resumed.`
  )) return;
  const result = await bulkAction(
    $("delete-all"), "/api/sessions?running=1", { method: "DELETE" }, "delete"
  );
  dropIfGone(result, all);
  refreshSessions();
});

/* Was the session this tab is attached to among the records just dropped? Then
   there is nothing left to watch — not even an exited screen — so let the
   terminal go and fall back to home. A record the mesh guard kept is still
   there, and stays open. */
function dropIfGone(result, candidates) {
  if (!result || !currentName || !candidates.includes(currentName)) return;
  const kept = (result.kept || []).map((k) => k.name);
  if (kept.includes(currentName)) return;
  detach();
  currentName = null;
  location.hash = "#/";
}

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

/* ---- the link ----

   The terminal is the one thing on this page that is not a poll. Everything
   else asks again every two seconds and so repairs itself by accident; the
   terminal holds a socket, and a socket that closes stays closed. A daemon
   restart, a laptop waking up, a relay dropping its tunnel — each of those
   used to end as `[disconnected]` painted into the buffer with nothing behind
   it. What recovery there was came from the session poll noticing the child's
   pid had changed, which is a different question: a restart that relaunches a
   session gives it a new pid, but one that retires it hands back the pid it
   died with, and either way a viewer that never got an init frame has no pid
   to compare against and is stuck for good. Hence a reload being the cure.

   So the link is its own small state machine, and the only thing that opens
   sockets:

     idle          nothing is attached, or the session itself has ended
     opening       a socket is being established
     live          frames are flowing
     reconnecting  it closed under us, and a retry is scheduled
     lost          the retries ran out; it waits for a person or an event

   Retries only ever run from `reconnecting`. A live socket is never re-opened
   underneath the user, and a session that sent `exit` is not a broken link
   but a finished program — `resume` is the answer to that one, not a retry.

   And they are bounded. An unbounded backoff against a daemon that is not
   coming back is a tab that quietly wakes a phone every ten seconds until the
   battery is gone; when LINK_BACKOFF is spent the chip in the header says so
   and offers the retry. Nothing is lost by stopping: the health poll below
   kicks the link once more if a daemon actually turns up, so giving up costs
   a person nothing except in the case where nobody is watching anyway. */
const LINK_BACKOFF = [500, 1000, 2000, 4000, 6000, 8000, 10000, 10000];
// Two viewers of the same session — a phone and a laptop, or every tunnel
// behind one relay — come back from the same outage at the same moment.
const LINK_JITTER = 0.25;
// Alt-tabbing must not turn "retry when the user looks at it" into a retry
// loop wearing a different hat. A press of the chip is exempt: that is a
// person asking, and asking twice is their business.
const LINK_KICK_MS = 3000;
// Held keystrokes. Small on purpose — this covers a restart, not a walk.
const LINK_QUEUE_MAX = 4096;

let linkState = "idle";
let linkName = null;      // the session this link is for
let linkTry = 0;          // retries spent in the current outage
let linkTimer = null;     // the scheduled retry
let linkTicket = 0;       // bumped whenever a socket stops being the current one
let linkKickAt = 0;       // last event-driven retry, for the throttle above
let linkQueue = [];       // keystrokes typed while it was down
let sessionEnded = false; // an `exit` frame arrived: the program, not the link
let attachedBoot = null;  // daemon incarnation the socket's pid belongs to
const linkEncoder = new TextEncoder();

function setLink(state) {
  linkState = state;
  syncLinkChip();
}

/* The header (and its mirror on a phone) say what the socket is doing, because
   the status badge beside them cannot: that one reports the *session*, which
   goes on running perfectly well while this browser cannot see it. A tab that
   says `idle` next to a terminal that has been frozen for a minute is the
   worst of the states this page can be in. */
function syncLinkChip() {
  const down = linkState === "reconnecting" || linkState === "lost";
  const text = linkState === "lost"
    ? "disconnected ⟳"
    : `reconnecting… ${linkTry}/${LINK_BACKOFF.length}`;
  const title = linkState === "lost"
    ? "the daemon never answered — press to try again now"
    : "this terminal's socket dropped; press to retry without waiting";
  for (const [id, cls] of [["term-link", "term-btn"], ["m-link", "m-act"]]) {
    const chip = $(id);
    chip.textContent = text;
    chip.title = title;
    chip.className = `${cls} link-chip ${linkState}${down ? "" : " hidden"}`;
  }
}

/* Is the daemon answering? Unauthenticated (api.py keeps /api/health open),
   which is the whole reason to ask it rather than /api/daemon: the login
   cookie lives in the daemon's memory and therefore died in the restart we
   are recovering from, so an authenticated probe cannot tell "not back yet"
   from "back, and it does not know me any more". A refused WebSocket upgrade
   cannot tell them apart either — the browser reports a 401 upgrade as a
   plain close with no status at all. */
async function daemonHealth() {
  try {
    const resp = await fetch(url("/api/health"), { cache: "no-store" });
    if (!resp.ok) return null;
    return await resp.json();
  } catch {
    return null;
  }
}

/* Open (or re-open) the socket for `name`. The terminal object is deliberately
   not touched: a reconnect is a new pipe to the same screen, and the daemon's
   first act on a fresh socket is to repaint it (ws.py), so the grid comes back
   as it now is and the scrollback above it survives. */
function openSocket(name) {
  const ticket = ++linkTicket;   // any older socket's events are noise from here
  linkName = name;
  setLink("opening");

  const proto = location.protocol === "https:" ? "wss" : "ws";
  const sock = new WebSocket(
    `${proto}://${location.host}${url(`/api/sessions/${encodeURIComponent(name)}/ws`)}`
  );
  sock.binaryType = "arraybuffer";
  ws = sock;

  sock.onopen = () => {
    if (ticket !== linkTicket) return;
    linkTry = 0;   // this outage is over; the next one starts with a full budget
    setLink("live");
  };

  sock.onmessage = (ev) => {
    if (ticket !== linkTicket) return;
    if (typeof ev.data === "string") {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      handleFrame(msg);
    } else if (term) {
      term.write(new Uint8Array(ev.data));
    }
  };

  sock.onclose = () => {
    // A socket we have already replaced closing late is not an outage — it is
    // the tail of one we have dealt with. Without this, the losing half of a
    // race schedules retries against a link that is already live.
    if (ticket !== linkTicket) return;
    ws = null;
    if (linkState === "idle" || sessionEnded) { setLink("idle"); return; }
    linkDown();
  };
}

function handleFrame(msg) {
  if (msg.type === "init") {
    // Seed the grid with the session's current size without echoing it back;
    // the fit below sends this viewer's own size as the single resize.
    applyingRemoteResize = true;
    try { term.resize(msg.cols, msg.rows); }
    finally { applyingRemoteResize = false; }
    // Is this the same program we were talking to before the link dropped? A
    // respawn — or a daemon restart that relaunched the session — keeps the
    // name and replaces the child, and the pid alone cannot say so across a
    // restart, since a retired record keeps the pid it last had.
    const same = attachedPid !== null
      && msg.pid === attachedPid
      && (!msg.boot_id || !attachedBoot || msg.boot_id === attachedBoot);
    attachedPid = msg.pid || null;
    attachedBoot = msg.boot_id || null;
    setStatusBadge(msg.status);
    flushInput(same);
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
    // Not a broken link: the program finished. There is nothing to reconnect
    // to, so the machine goes idle and stays there until `resume` builds a
    // new session under the name.
    sessionEnded = true;
    linkQueue = [];
    setLink("idle");
    setStatusBadge("exited");
    term.write(
      `\r\n\x1b[90m[session exited (code ${msg.code})] ` +
      `- press "resume" above to relaunch it\x1b[0m\r\n`
    );
  }
}

/* Everything typed into the terminal goes through here. While the link is down
   the keystrokes are held rather than dropped on the floor: dropping them
   silently is how a phone user — who has no local echo to tell them otherwise
   — comes to believe the keyboard missed the line they just wrote. Held only
   while a retry is actually coming, though; once the link is `lost` there is
   nothing to hold them for, and a buffer that fills over a lunch break and
   then fires into a live shell is far worse than a lost keystroke. */
function sendInput(data) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(linkEncoder.encode(data));
    return;
  }
  if (linkState !== "opening" && linkState !== "reconnecting") return;
  const held = linkQueue.reduce((n, s) => n + s.length, 0);
  if (held + data.length > LINK_QUEUE_MAX) return;
  linkQueue.push(data);
}

/* And they are only replayed into the child they were meant for. Typing into
   a session that has since been replaced is how a half-written command ends
   up executed by whatever came next. */
function flushInput(sameChild) {
  const held = linkQueue;
  linkQueue = [];
  if (!held.length) return;
  if (!sameChild) {
    term.write(
      "\r\n\x1b[90m[reconnected to a new process — what you typed while it " +
      "was down was discarded]\x1b[0m\r\n"
    );
    return;
  }
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(linkEncoder.encode(held.join("")));
  }
}

function linkDown() {
  if (term) term.write("\r\n\x1b[90m[disconnected — reconnecting…]\x1b[0m\r\n");
  scheduleReconnect();
}

function scheduleReconnect() {
  clearTimeout(linkTimer);
  linkTimer = null;
  if (linkTry >= LINK_BACKOFF.length) {
    setLink("lost");
    if (term) {
      term.write(
        "\r\n\x1b[90m[still nothing — press reconnect in the header, or " +
        "reload]\x1b[0m\r\n"
      );
    }
    return;
  }
  const wait = Math.round(LINK_BACKOFF[linkTry++] * (1 + LINK_JITTER * Math.random()));
  setLink("reconnecting");
  linkTimer = setTimeout(tryReconnect, wait);
}

async function tryReconnect() {
  if (linkState !== "reconnecting" || !linkName) return;  // the only state it runs from
  const ticket = linkTicket;
  if (!(await daemonHealth())) { scheduleReconnect(); return; }
  if (ticket !== linkTicket || linkState !== "reconnecting") return;
  // The daemon is up, so the remaining reason an upgrade would be refused is
  // the cookie the old one minted. api() renews it from the remembered token
  // on 401, which is why this goes through api() and not fetch().
  try { await api("/api/daemon"); }
  catch { scheduleReconnect(); return; }   // still unauthorised: the overlay is up
  if (ticket !== linkTicket || linkState !== "reconnecting") return;
  openSocket(linkName);
}

/* A retry that does not wait out the backoff: the chip was pressed, the tab
   came back to the foreground, the network came back, or the poll saw a
   different daemon answering. Only ever from a link that is down — a live
   socket is never disturbed, which is what keeps this whole machine out of
   the way of ordinary use. */
function reconnectNow(byUser) {
  if (linkState !== "reconnecting" && linkState !== "lost") return;
  if (!linkName || sessionEnded) return;
  const now = Date.now();
  if (!byUser && now - linkKickAt < LINK_KICK_MS) return;
  linkKickAt = now;
  clearTimeout(linkTimer);
  linkTimer = null;
  linkTry = 0;   // a new circumstance, not another blind retry
  setLink("reconnecting");
  tryReconnect();
}

/* Take the link down deliberately. Nothing here is an outage, so no retry is
   scheduled, and the ticket bump means neither the socket being dropped nor a
   retry already in flight can speak for the link again. */
function closeLink() {
  linkTicket += 1;
  clearTimeout(linkTimer);
  linkTimer = null;
  linkTry = 0;
  linkName = null;
  linkQueue = [];
  sessionEnded = false;
  setLink("idle");
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
  attachedPid = null;   // re-learned from the next socket's init frame
  attachedBoot = null;
}

function detach() {
  closeLink();
  if (term) { term.dispose(); term = null; fitAddon = null; }
}

$("term-link").addEventListener("click", () => reconnectNow(true));
$("m-link").addEventListener("click", () => $("term-link").click());
// The network coming back is the one event that says "try now" without a
// person having to be there. Guarded like the rest: it does nothing unless
// the link is down.
window.addEventListener("online", () => reconnectNow());

/* ---- text size ----
   Scaling the glyphs scales the session: the grid is however many cells fit
   the box, so a step here ends in a fit(), which tells the daemon the program
   now has fewer (or more) columns and rows to draw into. It is therefore not
   a private zoom — the size travels to the child and to every other viewer
   that is currently in the background.

   Remembered per browser rather than per session, because it answers a
   question about the reader and not about the session, and scoped by BASE for
   the same reason the token is: several daemons can reach one origin through
   the relay and share its localStorage. */
const FONT_KEY = `claunch_fontsize:${BASE}`;
const FONT_DEFAULT = 13;
const FONT_MIN = 8;    // below this xterm's cell metrics stop being legible
const FONT_MAX = 28;   // above it a laptop is down to a shell 40 columns wide
const FONT_STEP = 1;

function clampFont(px) {
  if (!Number.isFinite(px)) return FONT_DEFAULT;
  return Math.min(FONT_MAX, Math.max(FONT_MIN, Math.round(px)));
}

let fontSize = clampFont(Number(localStorage.getItem(FONT_KEY)) || FONT_DEFAULT);

function setFontSize(px) {
  fontSize = clampFont(px);
  localStorage.setItem(FONT_KEY, String(fontSize));
  if (term) {
    term.options.fontSize = fontSize;
    // The cell got bigger or smaller, so the grid the box holds did too —
    // refit rather than leave the session sized for the old glyph. Straight
    // away, not debounced: this one came from a deliberate press.
    if (canFit()) fitAddon.fit();
  }
  syncZoomControls();
}

function syncZoomControls() {
  $("term-zoom-level").textContent = `${fontSize}px`;
  const atMin = fontSize <= FONT_MIN;
  const atMax = fontSize >= FONT_MAX;
  $("term-zoom-out").disabled = atMin;
  $("term-zoom-in").disabled = atMax;
  // The phone's bar carries the same two buttons — see the mirrors below.
  $("m-zoom-out").disabled = atMin;
  $("m-zoom-in").disabled = atMax;
}

$("term-zoom-out").addEventListener("click", () => setFontSize(fontSize - FONT_STEP));
$("term-zoom-in").addEventListener("click", () => setFontSize(fontSize + FONT_STEP));
// The readout is the way back: a size you stepped into and can't remember
// leaving shouldn't need a click-count to undo.
$("term-zoom-level").addEventListener("click", () => setFontSize(FONT_DEFAULT));
// Mirrors on the phone's bar, which stands in for the header it hides. They
// reach the header's buttons rather than call setFontSize themselves, so a
// step means one thing wherever it is pressed.
$("m-zoom-out").addEventListener("click", () => $("term-zoom-out").click());
$("m-zoom-in").addEventListener("click", () => $("term-zoom-in").click());
syncZoomControls();

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
  // The panel may already have been pointing here (opened from the rail's ⓘ
  // while another terminal was up); walking into that terminal is what makes
  // the header's `details` its close button, so it has to light now.
  markDetailRow();

  term = new Terminal({
    fontFamily: "Cascadia Mono, Consolas, Menlo, monospace",
    fontSize: fontSize,
    theme: { background: "#14161a" },
    scrollback: 5000,
  });
  fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open($("terminal"));
  fitAddon.fit();

  // Wired once, for the life of this terminal object: the link swaps sockets
  // underneath these, and a reconnect must not leave a second pair behind.
  term.onData(sendInput);
  term.onResize(({ cols, rows }) => {
    // A resize we applied from a server broadcast must not be echoed back, or
    // two viewers (or a stale echo over a high-latency relay) ping-pong forever.
    if (applyingRemoteResize) return;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "resize", cols, rows }));
    }
  });

  openSocket(name);
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
  // A tab returning to the foreground is the cheapest moment to notice that
  // the link died while nobody was looking — and the likeliest, since a phone
  // suspends its sockets the moment the screen goes off.
  if (linkState === "reconnecting" || linkState === "lost") { reconnectNow(); return; }
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
  syncDetailPanel();
  syncMobileBars();
  // Coming back from the rail the terminal was display:none, so its grid is
  // whatever it was before the viewport last changed. Re-fit it.
  if (wasOpen && !railOpen) refitSoon(60);
}

/* Where the session detail lives, and whether it is up.

   Wide: a rail of its own down the right-hand side, opposite the one that
   lists the sessions — the left rail is what exists, this is what the one
   you picked *is*, and the terminal keeps the middle. Not folded into the
   left rail: the session list is a monitor you watch while working, and a
   panel growing under it would push the thing being watched off screen.
   Narrow: two rails do not fit next to anything, so it takes the page slot
   instead — which is why "session" is a page on a phone and nothing at all
   on a desktop.

   The same node is *moved*, never duplicated: one render path, and the poll,
   the half-typed context line and the scroll position all survive a rotation
   across the breakpoint. */
let detailWasUp = false;

function syncDetailPanel() {
  const view = $("sess-view");
  const narrow = MOBILE_MQ.matches;
  const host = narrow ? $("main") : $("layout");
  if (view.parentNode !== host) {
    // Last in #layout is the mobile bottom bar (display:none up here), so the
    // rail goes before it: #main keeps the middle, this takes the right edge.
    if (narrow) host.appendChild(view);
    else host.insertBefore(view, $("mobile-bottom"));
  }
  view.classList.toggle("docked", !narrow);
  const up = !!sessName && (!narrow || currentPage === "session");
  view.classList.toggle("hidden", !up);
  // Docking and undocking take width off #main and give it back, and no
  // resize event announces that — a sibling changing width is not a viewport
  // change. Without this the terminal keeps the columns it had and the
  // session wraps its output against a width that is no longer there.
  if (!narrow && up !== detailWasUp) refitSoon(60);
  detailWasUp = up;
}

/* Which row's ⓘ is lit. Rebuilt rows get this from refreshSessions; this is
   for the rows already on screen when the panel opens or closes.
   The header's `details` is the same switch and gets the same treatment —
   pressed only while the open panel is describing the terminal under it,
   which is exactly when pressing it again would close rather than repoint.
   Both names null (no terminal, no panel) is not a match. In that state the
   panel has handed this button its × (see sessHead), so it says so. */
function markDetailRow() {
  document.querySelectorAll("#session-list .sess-info").forEach((b) =>
    b.classList.toggle("on", b.dataset.name === sessName)
  );
  const closes = !!sessName && sessName === currentName;
  const chip = $("term-details");
  chip.setAttribute("aria-pressed", String(closes));
  chip.title = closes
    ? "close this session's details"
    : "this session's metadata and workflow";
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
    case "flow": return `flows · ${flowMesh}`;
    case "wf": return `workflow · ${shortenPath(wfCwd || "")}`;
    case "msg": return `messages · ${traceSession}`;
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
  // Nothing to size without a terminal under the bar.
  $("m-zoom").classList.toggle("hidden", !has);
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
  // Except the detail, which is the one thing that is a page on one side of
  // the breakpoint and not on the other. Narrowing: the right rail has
  // nowhere to go on a phone that isn't the page the user is already on, so
  // it closes (ⓘ brings it back). Widening: it stops being a page and
  // becomes a rail, so the slot it was borrowing has to be handed back —
  // otherwise #main is left showing nothing at all.
  if (MOBILE_MQ.matches) {
    if (sessName && currentPage !== "session") dropDetail();
  } else if (currentPage === "session") {
    go(currentName ? "#/s/" + encodeURIComponent(currentName) : "#/");
  }
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

/* Every page's container, by page name. Two are deliberately not in here:
   the terminal, which is not swapped in and out but *covered* (see
   showView), and the session detail, which is not a page on a wide screen
   at all — it is the right-hand rail, and syncDetailPanel owns it. */
const VIEWS = {
  home: "home-view",
  new: "new-view",
  meshes: "meshes-view",
  flows: "flows-view",
  wf: "wf-view",
  msg: "msg-view",
  mesh: "mesh-view",
  flow: "flow-view",
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
  // On a phone the detail occupies the page slot, so leaving that page is
  // closing it. As a rail it is not a page and survives every navigation.
  if (name !== "session" && MOBILE_MQ.matches) dropDetail();
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
 *   #/new               the create form
 *   #/mesh              the mesh list, and create/join
 *   #/mesh/<name>       one mesh
 *   #/mesh/<name>/flows ...and where each of its agents is in its workflow
 *   #/flows             cflow runs
 *   #/wf/<scope|cwd>    one run
 *   #/msg/<name>        what that session has said and been told
 *   #/msg/<name>/<mesh> ...in the mesh named, rather than its first
 *   #/workspaces        the workspace registry
 */
function parseHash(h) {
  const raw = (h || "").replace(/^#\/?/, "");
  if (!raw) return { page: "home" };
  const parts = raw.split("/").map(decodeURIComponent);
  // A session has one destination: its terminal. What it *is* is a panel
  // beside that (see openDetail), not a place you can be — so an old
  // /info link lands on the session rather than on nothing.
  if (parts[0] === "s" && parts[1]) return { page: "terminal", name: parts[1] };
  if (parts[0] === "wf" && parts[1]) {
    // The scope is glued to the cwd with '|' because a Windows path is full
    // of the separators a path segment would otherwise be split on.
    const token = parts.slice(1).join("/");
    const sep = token.indexOf("|");
    return sep >= 0
      ? { page: "wf", cwd: token.slice(sep + 1), scope: token.slice(0, sep) }
      : { page: "wf", cwd: token, scope: "default" };  // pre-scope links
  }
  // The session's traffic, not the session — a third reading of it, beside
  // the terminal (what it is doing) and the run page (where it has got to).
  // The mesh is in the URL because a session is a different handle in each
  // one, so which room this is says which name it is being called by.
  if (parts[0] === "msg" && parts[1]) {
    return { page: "msg", name: parts[1], mesh: parts[2] || "" };
  }
  if (parts[0] === "mesh") {
    if (!parts[1]) return { page: "meshes" };
    // Same shape as #/s/<name>/info: one more segment is a second reading of
    // the same thing, not a different thing.
    return parts[2] === "flows"
      ? { page: "flow", name: parts[1] }
      : { page: "mesh", name: parts[1] };
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
  if (r.page !== "msg") stopMsgPoll();
  if (r.page !== "mesh") stopMeshPoll();
  if (r.page !== "flow") stopFlowPoll();
  if (r.page !== "ws") closeWorkspaces();

  switch (r.page) {
    case "terminal":
      // Re-entering the route we are already attached to must not tear the
      // socket down and build it again — coming back from another page is
      // the common case, and it would cost the scrollback every time.
      if (currentName === r.name && term) {
        showView("terminal");
        // Walking back into a terminal is someone coming to look at it, which
        // is as good a moment as any to try the socket again. It does nothing
        // unless the link is down (and not twice within the throttle), so
        // this is not the reattach-on-navigation the old code accidentally
        // relied on — that is the link's job now.
        reconnectNow();
      } else attach(r.name);
      // An open rail follows the terminal. Never *opened* here — a panel
      // moving to the session the user just went to is one thing, one
      // springing up because they changed terminals is another. On a phone
      // this does nothing: showView above has already closed the detail,
      // whose home there is the page slot the terminal just took.
      if (sessName && sessName !== r.name) repointDetail(r.name);
      break;
    case "wf": openWorkflow(r.cwd, r.scope); break;
    case "msg": openTrace(r.name, r.mesh); break;
    case "mesh": openMesh(r.name); break;
    case "flow": openFlowTopology(r.name); break;
    case "meshes": showView("meshes"); refreshMeshList(); break;
    case "new": showView("new"); refreshWorkflowChoices(); break;
    case "flows": showView("flows"); refreshCflow(); break;
    case "ws": openWorkspaces(); break;
    default: openHome();
  }
}
window.addEventListener("hashchange", route);

/* Navigate, even when the URL already says where we are. The detail panel
   can be up over the very route the URL names (a phone shows it in the page
   slot), so "go to the terminal" has to mean re-entering the route rather
   than assigning a hash the browser will discard as a no-op. */
function go(hash) {
  if ((location.hash || "#/") === hash) route();
  else location.hash = hash;
}

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

/* `after` is for the callers that are not the run page: refreshWf is a no-op
   unless that page is the one open, and a control pressed somewhere else
   still has to see its own view catch up. */
async function cflowAction(path, body, after) {
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
  if (after) after();
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
  // Which session owns this run is its identity, not a detail: several
  // sessions run the same workflow in the same tree, and the pages are then
  // identical but for this. A link either way — an exited session is still
  // openable (it resumes), and that is the first thing wanted here.
  if (data.scope && data.scope !== "default") {
    const owner = el("a", "cflow-scope", `session ${data.scope}`);
    owner.href = "#/s/" + encodeURIComponent(data.scope);
    owner.title = (data.sessions || []).includes(data.scope)
      ? "attach this run's session"
      : "this run's session is not running — open it to resume";
    meta.appendChild(owner);
  }
  meta.appendChild(el("span", null, `run ${run.run || "?"}`));
  meta.appendChild(el("span", null, `started ${(run.started_at || "?").replace("T", " ")}`));
  meta.appendChild(el("span", null, `${run.steps_completed ?? 0} steps done`));
  meta.appendChild(el("span", "mono", data.cwd));
  for (const s of data.sessions || []) {
    const link = el("a", "wf-session", `attach: ${s}`);
    link.href = "#/s/" + encodeURIComponent(s);
    meta.appendChild(link);
  }
  view.appendChild(meta);
  /* The run reads a snapshot, so this file is where it CAME from — which is
     the only way to tell two same-named workflows apart after the fact, and
     the thing to open when the run is doing something surprising. */
  if (run.source) {
    const src = el("p", "wf-source mono");
    src.appendChild(el("span", "wf-source-path", run.source));
    if (run.origin) {
      src.appendChild(el("span", "wf-source-origin", ` — ${run.origin}`));
    }
    view.appendChild(src);
  }
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

/* The human controls for a run. Shared with the session panel's fold, which
   asks for two things the page does not: `after`, because refreshWf only
   refreshes the run page, and no archive — aborting a run is not something to
   offer in a corner of a terminal, and the page it points at has it. */
function wfActions(data, opts = {}) {
  const run = data.run || {};
  const after = opts.after;
  const box = el("div", "wf-actions");
  // Leads, because it changes how everything below it reads: a run whose
  // session is not running is not being worked on, whatever position it
  // recorded before it stopped. Said after "agent is working on 'survey'",
  // it reads as a footnote to the opposite claim.
  const homeless = !(data.sessions || []).length;
  const scoped = data.scope && data.scope !== "default";
  if (homeless && run.status !== "done" && run.status !== "aborted") {
    box.appendChild(el(
      "p", scoped ? "wf-warning" : "wf-note",
      scoped
        ? `session '${data.scope}' is not running — nothing is driving this run`
        : "this run belongs to no managed session — nudge the agent wherever it runs"
    ));
  }
  // Who a delegated decision went to, and why it did not go further. Shown
  // for every ask, answered by an agent or fallen to us: "no leader above
  // this run" is the whole explanation for why a question is on this screen.
  if (run.ask) {
    box.appendChild(el("p", "wf-note", `asked: ${askWho(run.ask)}`));
    for (const s of run.ask.skipped || []) {
      box.appendChild(el("p", "wf-note", `skipped ${s.candidate} — ${s.reason}`));
    }
    if (run.ask.deadline) {
      box.appendChild(el("p", "wf-note", `moves on after ${run.ask.deadline}`));
    }
    if (run.ask.undelivered) {
      box.appendChild(el("p", "wf-warning",
        `recorded, but not announced: ${run.ask.undelivered}`));
    }
  }
  if (run.status === "waiting_answer") {
    box.appendChild(el("p", "wf-gate", run.ask ? run.ask.prompt : "waiting for a decision"));
    box.appendChild(el("p", "wf-note",
      "this is with another agent; you do not have to do anything. Take it " +
      "over only if it is stuck."));
    const btn = el("button", "wf-btn", "Decide it myself");
    btn.addEventListener("click", () => {
      if (!confirm(
        `Take '${run.step_id}' away from ${askWho(run.ask)} and decide it yourself?`
      )) return;
      if (run.ask && run.ask.kind === "branch") {
        // A branch needs an option, so send them back to the buttons the
        // human-facing path already draws rather than inventing a second one.
        alert("Use 'claunch cflow select <option>' to pick the branch.");
        return;
      }
      cflowAction("/api/cflow/approve", { cwd: data.cwd, scope: data.scope }, after);
    });
    box.appendChild(btn);
  } else if (run.status === "waiting_approval") {
    const isLoop = run.reason === "loop_limit";
    if (run.reason === "declined" && run.declined) {
      box.appendChild(el("p", "wf-warning",
        `${run.declined.by} declined: ${run.declined.reason || "no reason given"}`));
    }
    box.appendChild(el("p", "wf-gate", run.gate || "waiting for approval"));
    const btn = el("button", "wf-btn approve",
      isLoop ? "Extend loop limit"
        : run.reason === "declined" ? "Override the refusal" : "Approve gate");
    btn.addEventListener("click", () => {
      const q = isLoop
        ? `Extend the loop limit at step '${run.step_id}'?`
        : `Approve the gate at step '${run.step_id}'?`;
      if (confirm(q)) {
        cflowAction("/api/cflow/approve", { cwd: data.cwd, scope: data.scope }, after);
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
            }, after);
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
      homeless
        ? `recorded position: step '${run.step_id}' — stopped here`
        : `agent is working on '${run.step_id}' — nothing needs a human right now`
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
          nudgeRun(data.cwd, data.scope).then(() => { if (after) after(); });
        }
      });
    } else {
      // Why it is dead is already stated at the top of this box.
      btn.disabled = true;
      btn.title = "nothing to nudge: this run has no live session of its own";
    }
    box.appendChild(btn);
  }

  if (opts.archive === false) return box;

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
  // Which slot is empty, not just which directory: the same directory holds
  // one slot per session, and all but this one may well be busy.
  view.appendChild(el(
    "p", "wf-note",
    data.scope && data.scope !== "default"
      ? `no active cflow run for session '${data.scope}' in ${data.cwd}`
      : `no active cflow run in ${data.cwd}`
  ));
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
    opt.title = w.path;
    sel.appendChild(opt);
  }
  /* A name is not a file: the same name can be declared in the project and in
     the global layer, and picking from a list of names hides which one runs.
     The path goes under the select rather than into the option text — an
     option cannot wrap, and these are absolute paths. */
  const source = el("p", "wf-source mono");
  const showSource = () => {
    const w = flows.find((f) => f.name === sel.value);
    source.replaceChildren();
    if (!w) return;
    source.appendChild(el("span", "wf-source-path", w.path));
    if (w.shadowed && w.shadowed.length) {
      source.appendChild(el(
        "span", "wf-source-shadow",
        ` — ${w.origin} copy, overriding ${w.shadowed.join(", ")}`
      ));
    } else if (w.origin) {
      source.appendChild(el("span", "wf-source-origin", ` — ${w.origin}`));
    }
  };
  sel.addEventListener("change", showSource);
  showSource();

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
  box.appendChild(source);
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
/* session detail — what a session IS, beside what it is doing        */
/* ------------------------------------------------------------------ */
/* The session list answers "which sessions exist"; the terminal answers
   "what is it doing right now". Neither answers "what is this session" —
   which harness and profile, whose directory, which role, which meshes, and
   above all which cflow run it drives. Four registries hold those answers and
   they only ever met in the operator's head; /api/sessions/<name>/meta
   gathers them, keyed by the one thing they share: the session name.

   It used to be a route, #/s/<name>/info, and that was the wrong shape: you
   read a session's definition *while* watching it work, and a route made
   that a trip away from the terminal and back. It is a rail now — open down
   the right-hand side, beside the thing it describes, closed by the same
   button that opened it, and not in the URL at all. Where it goes is the
   layout's business (syncDetailPanel), the one place that knows how wide the
   screen is. */
let sessName = null;      // the session whose detail is open (null = closed)
let sessPollTimer = null;
let sessStartBox = null;  // reused across polls: it holds the user's typing
let sessSendBox = null;   // and so does the message box — same reason
let sessRunFold = null;   // reused across polls too: it holds open/shut
let sessRunTimer = null;  // the fold's own poll, alive only while it is open

/* Forget the open detail. State only — the caller syncs the layout, which is
   what actually takes the panel off the screen. */
function dropDetail() {
  if (sessPollTimer) { clearInterval(sessPollTimer); sessPollTimer = null; }
  stopSessRun();
  sessName = null;
  sessStartBox = null;
  sessSendBox = null;
  sessRunFold = null;
  $("sess-view").innerHTML = "";
  markDetailRow();
}

/* ⓘ, and the terminal header's `details`. Same button both ways: pressing it
   on the session already showing closes the panel. */
function openDetail(name) {
  if (!name) return;
  if (name === sessName) { closeDetail(); return; }
  repointDetail(name);
  // On a phone the detail is the page; on a desktop the page does not change
  // at all — the right rail opens beside it.
  if (MOBILE_MQ.matches) showView("session");
  else syncLayout();
}

/* Aim the panel at a session, without deciding where it goes. Opening does
   that (above); so does entering another terminal — the rail describes the
   session on screen, and one left pointing at the session we came *from*
   quietly mislabels everything in it, its cflow run most of all: the run page
   it then offers is another session's. */
function repointDetail(name) {
  if (sessPollTimer) clearInterval(sessPollTimer);
  stopSessRun();
  sessName = name;
  sessStartBox = null;
  sessSendBox = null;
  sessRunFold = null;
  $("sess-view").innerHTML = "<p class='wf-note'>loading…</p>";
  markDetailRow();
  refreshSession();
  sessPollTimer = setInterval(refreshSession, 2000);
}

function closeDetail() {
  const wasPage = currentPage === "session";
  dropDetail();
  // On a phone the detail *was* the page, so closing it has to leave
  // something behind: the terminal it describes, or home if none is open.
  if (wasPage) go(currentName ? "#/s/" + encodeURIComponent(currentName) : "#/");
  else syncLayout();
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

/* Who this panel is about, and the two ways out of it — both of which drop
   away in the one arrangement where they are noise.

   Docked beside the terminal it describes, the header's `details` chip is
   anchored over this head's top-right corner and a second press of it closes
   the panel. There is no sense in a × under a close button, nor in an "open
   the terminal" that opens the terminal already on screen. Every other
   arrangement keeps both and needs them: the panel aimed at another row's
   session (that button travels), a page covering the terminal (it brings it
   back), and the phone, where this panel IS the page and there is no header
   anywhere on screen to close it from. */
function sessHead(s) {
  const head = el("div", "wf-head sess-head");
  head.appendChild(el("h2", null, s.name || "session"));
  head.appendChild(el("span", `badge ${s.status || ""}`, s.status || "?"));
  const mine = !!s.name && s.name === currentName;
  // Opening another row's ⓘ is a legitimate thing to do — read one session
  // while watching another — but then every line under this head, the
  // workflow run included, belongs to a session that is not the one on
  // screen. Unsaid, the panel simply reads as the terminal's own. Only worth
  // saying while a terminal is actually up beside it to be mistaken for.
  if (currentName && s.name && !mine && terminalOnScreen()) {
    const other = el("span", "sess-elsewhere", `not ${currentName}`);
    other.title =
      `these details are session '${s.name}'; the terminal on screen is ` +
      `'${currentName}'`;
    head.appendChild(other);
  }
  if (mine && terminalOnScreen() && !MOBILE_MQ.matches) return head;

  // Through the router, so the terminal it opens is the one the URL names —
  // and via go(), because on a phone this panel is laid over the very route
  // that terminal lives at, where assigning the same hash would do nothing.
  const open = el("button", "wf-btn", "Open terminal");
  open.addEventListener("click", () => go("#/s/" + encodeURIComponent(s.name)));
  head.appendChild(open);
  const close = el("button", "sess-close", "×");
  close.title = "close details";
  close.addEventListener("click", closeDetail);
  head.appendChild(close);
  return head;
}

function renderSession(data) {
  const view = $("sess-view");
  const s = data.session || {};
  // The 2s poll rebuilds this panel from scratch, and a rebuild detaches
  // whatever the user is typing in — which takes the caret with it. Keeping
  // the live node across polls (sessSendBox, sessStartBox) saves the text but
  // not the focus, so while a field in here has it, the poll waits. Same rule
  // the mesh page keeps, and for the same reason: a message being written is
  // worth more than a two-second-fresher 'last output'.
  if (formInUse(view)) return;
  view.innerHTML = "";

  view.appendChild(sessHead(s));

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
  // A `--worktree` session sits inside its workspace rather than at its root,
  // so say which of the two it is: "workspace X" and "in X / wt-name" are
  // different facts, and reading the second as the first would have the
  // operator looking for their branch in the wrong checkout.
  metaRow(
    dl, "directory",
    data.workspace
      ? data.workspace_subpath
        ? `${s.cwd}  (in workspace ${data.workspace.name} / ${data.workspace_subpath})`
        : `${s.cwd}  (workspace ${data.workspace.name})`
      : s.cwd,
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

  // Above the memberships, because it is what they are FOR: the list says
  // which rooms this session can be spoken to in, this says something in one.
  view.appendChild(sessSend(data));

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
  // The chips above say which rooms this session is in; this reads what was
  // actually said in them, as a sequence. Only offered when there is a room:
  // a session in no mesh has no traffic to draw, and the note above already
  // says why. Not a chip, because it is not another membership — it is the
  // way out of this panel into a page, like the run's button below.
  if (meshes.length) {
    const trace = el("button", "wf-btn option", "Message trace");
    trace.title =
      "who this session has spoken to, and been asked by, in order — " +
      "with what has not been answered";
    trace.addEventListener(
      "click", () => go("#/msg/" + encodeURIComponent(s.name))
    );
    meshBox.appendChild(trace);
  }
  view.appendChild(meshBox);

  view.appendChild(sessWorkflow(data));
}

/* ---- say something to this session, from the panel that names it ----
   The mesh page's "Send message" box can already reach any member, but it is
   a page away and it asks you to pick the recipient out of a roster — while
   the panel you are already reading knows exactly which session it is about.
   So the same send, with the recipient answered: from you, the operator, to
   this session's handle in the mesh you pick.

   Delivery is the mesh's, not the terminal's: the message is sequenced into
   the log, counts against the sender's reply ledger when it asks for one, and
   is typed in by the daemon when the agent is between turns. Typing the same
   words into the terminal beside this does none of that.

   A message is carried BY a mesh, so a session in none has nothing to send
   through — hence the note rather than a dead form. */
function sessSend(data) {
  const box = el("div", "sess-send");
  box.appendChild(el("h3", null, "Send message"));
  const s = data.session || {};
  const meshes = data.meshes || [];
  if (!meshes.length) {
    sessSendBox = null;
    box.appendChild(el(
      "p", "wf-note",
      "messages travel through a mesh and this session is in none — " +
      "join it to one to write to it"
    ));
    return box;
  }

  // Rebuilt only when the memberships it can speak through change: the 2s
  // poll must not wipe a half-typed message. Member counts are deliberately
  // out of the key — someone else joining is no reason to lose your sentence.
  const key = `${s.name}|` + meshes.map((m) => `${m.mesh}>${m.handle}`).join(",");
  if (sessSendBox && sessSendBox.dataset.slot === key) {
    box.appendChild(sessSendBox);   // appending moves the live node here
    return box;
  }
  sessSendBox = el("div", "sess-send-form");
  sessSendBox.dataset.slot = key;
  box.appendChild(sessSendBox);

  const row = el("div", "sess-send-row");
  // One option is still a select: it says which mesh carries this, which is
  // not obvious from a rail that lists the memberships underneath.
  const mesh = document.createElement("select");
  for (const m of meshes) {
    const opt = document.createElement("option");
    opt.value = m.mesh;
    opt.textContent = `via ${m.mesh}`;
    opt.title = `delivered to ${m.handle} (${m.role})`;
    mesh.appendChild(opt);
  }
  const intent = document.createElement("select");
  for (const [v, label] of [
    ["say", "say"], ["ask", "ask (expects reply)"],
    ["fyi", "fyi (no reply)"], ["ack", "ack (no reply)"],
  ]) {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = label;
    intent.appendChild(opt);
  }
  row.append(mesh, intent);

  const text = document.createElement("textarea");
  text.rows = 3;
  const status = el("p", "wf-note hidden");
  const sendBtn = el("button", "wf-btn approve", "Send");

  // Which handle this lands on is the mesh's answer, not the session name's:
  // the same session is 'reviewer' in one mesh and 'coder4' in another.
  const target = () => meshes.find((m) => m.mesh === mesh.value) || meshes[0];
  const retarget = () => {
    text.placeholder =
      `message to ${target().handle} (Ctrl+Enter to send) — typed into ` +
      "this session's terminal by the daemon";
  };
  retarget();
  mesh.addEventListener("change", retarget);

  // One class, not both: .wf-note is declared after .wf-warning and would
  // take the amber back off a line that is there to warn.
  const say = (msg, cls) => {
    status.className = cls || "wf-note";
    status.textContent = msg;
  };

  const submitMsg = async () => {
    const body = text.value.trim();
    if (!body || sendBtn.disabled) return;
    const to = target();
    sendBtn.disabled = true;
    say("sending…");
    let doc = {};
    let resp;
    try {
      resp = await api(`/api/mesh/${encodeURIComponent(to.mesh)}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          // The operator is nobody's member, which is exactly what 'external'
          // admits — the same way the mesh page speaks as you.
          from: "operator",
          to: to.handle,
          body,
          external: true,
          type: intent.value,
        }),
      });
      doc = await resp.json().catch(() => ({}));
    } catch {
      sendBtn.disabled = false;
      say("could not reach the daemon — nothing was sent", "wf-warning");
      return;
    }
    sendBtn.disabled = false;
    if (!resp.ok) {
      say(doc.error || `HTTP ${resp.status}`, "wf-warning");
      return;
    }
    text.value = "";
    if (doc.queued) {
      // a mirror whose primary is unreachable: durable, but not delivered yet
      say(`queued ${doc.id} — the mesh's primary daemon is unreachable; it ` +
          "will be forwarded, in order, on reconnect", "wf-warning");
    } else {
      say(`sent to ${to.handle}` + (doc.notice ? ` · ${doc.notice}` : ""));
    }
    // The panel does not show the log, but the mesh page and the owed ledger
    // do — and an 'ask' from here is a debt from now on.
    refreshSession();
  };

  sendBtn.addEventListener("click", submitMsg);
  text.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      submitMsg();
    }
  });

  sessSendBox.append(row, text, sendBtn, status);
  return box;
}

/* The session's cflow slot: a run is keyed by (directory, scope) and the
   scope IS this session's name, so there is exactly one to show. */
function sessWorkflow(data) {
  const box = el("div", "sess-wf");
  box.appendChild(el("h3", null, "Workflow"));
  const flow = data.cflow;
  // Only the branch below with a run in it hangs the fold; every other one
  // has to let go of it, or its poll outlives the run it was reading and
  // keeps asking about a slot that has nothing in it.
  if (!flow || flow.status === "error" || !flow.status || flow.status === "idle") {
    stopSessRun();
    sessRunFold = null;
  }
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
    link.title = "the full diagram, every report, and force-set state";
    link.addEventListener("click", () => {
      location.hash = "#/wf/" + encodeURIComponent(`${flow.scope}|${flow.cwd}`);
    });
    box.appendChild(link);
    if (pending) box.appendChild(pending);
    box.appendChild(sessRunFoldFor(flow));
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

/* ---- the run, opened where you already are ----
   The panel's Workflow block says which run and which step; everything else
   about it — where that step sits in the graph, the gate wording, the buttons
   that clear it, what each step reported — was a page away. That page replaces
   the terminal you were reading the run *for*, which is the wrong trade for
   "approve this and carry on". So the same material folds out here, at rail
   width, and the page keeps what genuinely needs room: the full diagram, every
   report, force-set-state and archive.

   Shut by default, and it costs nothing shut: the fetch and the poll start on
   the first open and stop on close. The node survives the panel's 2s rebuild
   the way the start box does, so neither the fold's state nor the reports the
   user just expanded blink away underneath them. */
function sessRunFoldFor(flow) {
  const key = `${flow.scope}|${flow.cwd}`;
  if (sessRunFold && sessRunFold.dataset.slot === key) return sessRunFold;
  stopSessRun();
  const fold = document.createElement("details");
  fold.className = "sess-run";
  fold.dataset.slot = key;
  fold.dataset.cwd = flow.cwd;
  fold.dataset.scope = flow.scope;
  const sum = el("summary", null, "the run, here");
  sum.title = "where it is, what it is waiting for, and what each step reported";
  fold.appendChild(sum);
  fold.appendChild(el("div", "sess-run-body", ""));
  fold.addEventListener("toggle", () => {
    stopSessRun();
    if (!fold.open) return;
    refreshSessRun();
    sessRunTimer = setInterval(refreshSessRun, 2000);
  });
  sessRunFold = fold;
  return fold;
}

/* The fold's poll only. The node itself is left alone — a shut fold is inert,
   and it is still the one the next render should re-use. */
function stopSessRun() {
  if (sessRunTimer) { clearInterval(sessRunTimer); sessRunTimer = null; }
}

async function refreshSessRun() {
  const fold = sessRunFold;
  if (!fold || !fold.open) return;
  const body = fold.querySelector(".sess-run-body");
  let data;
  try {
    const resp = await api(
      `/api/cflow/run?cwd=${encodeURIComponent(fold.dataset.cwd)}` +
      `&scope=${encodeURIComponent(fold.dataset.scope)}`
    );
    data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      body.innerHTML = "";
      body.appendChild(el("p", "wf-warning", data.error || "cannot load this run"));
      return;
    }
  } catch {
    return;  // auth overlay is up; the poll will come back round
  }
  // The panel repoints, and the fold is rebuilt with it: a reply that lands
  // after that belongs to a run this fold no longer shows.
  if (sessRunFold !== fold || !fold.open) return;
  renderSessRun(body, data);
}

const SESS_RUN_REPORTS = 8;   // newest kept; the rest are one click away
const SESS_RUN_JOURNAL = 40;

function renderSessRun(body, data) {
  body.innerHTML = "";
  const run = data.run || {};
  const wf = data.workflow || null;
  if (data.status === "idle" || !run.status) {
    body.appendChild(el("p", "wf-note", "this slot has no run any more"));
    return;
  }

  if (wf && (wf.steps || []).length) body.appendChild(sessRunTrack(wf, run));
  const meta = el("div", "wf-meta");
  meta.appendChild(el("span", null, `run ${run.run || "?"}`));
  meta.appendChild(el("span", null, `${run.steps_completed ?? 0} steps done`));
  meta.appendChild(el("span", null,
    `started ${(run.started_at || "?").replace("T", " ")}`));
  body.appendChild(meta);
  for (const w of (wf || {}).warnings || []) {
    body.appendChild(el("p", "wf-warning", `⚠ ${w}`));
  }

  // The point of the fold: the gate, and the button that clears it.
  body.appendChild(wfActions(data, { archive: false, after: refreshSessRun }));

  const reports = (data.reports || []).slice().reverse();  // newest first
  body.appendChild(el("h4", null, `Reports (${reports.length})`));
  if (!reports.length) body.appendChild(el("p", "wf-note", "no reports yet"));
  for (const r of reports.slice(0, SESS_RUN_REPORTS)) {
    const card = el("div", "sess-run-report");
    const head = el("div", "wf-report-head");
    head.appendChild(el("span", "wf-report-step",
      r.visit > 1 ? `${r.step} ×${r.visit}` : r.step));
    head.appendChild(el("span", "wf-report-at", (r.at || "").replace("T", " ")));
    card.appendChild(head);
    card.appendChild(el("p", "wf-report-summary", r.summary || ""));
    if (r.details) {
      const more = document.createElement("details");
      more.className = "sess-run-more";
      more.appendChild(el("summary", null, "details"));
      more.appendChild(el("pre", "wf-report-details", r.details));
      card.appendChild(more);
    }
    body.appendChild(card);
  }
  // Said, not silently dropped: a rail showing the newest few must not read
  // as the whole history of the run.
  if (reports.length > SESS_RUN_REPORTS) {
    body.appendChild(el("p", "wf-note",
      `newest ${SESS_RUN_REPORTS} of ${reports.length} — the run page has them all`));
  }

  const events = (data.journal || []).slice().reverse();
  const journal = document.createElement("details");
  journal.className = "wf-journal";
  journal.appendChild(el("summary", null, `journal (${events.length} events)`));
  for (const e of events.slice(0, SESS_RUN_JOURNAL)) {
    journal.appendChild(el("div", "wf-journal-line mono",
      `${(e.at || "").replace("T", " ")}  ${e.event || ""}` +
      `${e.step ? "  " + e.step : ""}${e.option ? "  -> " + e.option : ""}`));
  }
  if (events.length > SESS_RUN_JOURNAL) {
    journal.appendChild(el("p", "wf-note",
      `newest ${SESS_RUN_JOURNAL} of ${events.length}`));
  }
  body.appendChild(journal);
}

/* Where the run is, as the flow view draws it: the whole state machine on one
   line, which is the only rendering of it that fits a rail. The run page's
   diagram is the same walk laid out in two dimensions — this is an index into
   it, not a replacement, and the button above goes to the real thing. */
function sessRunTrack(wf, run) {
  const track = flowTrack(wf, run);
  const m = flowMetrics(track.pips.length);
  const here = track.offGraph
    ? `step '${run.step_id}' is not in this workflow's graph`
    : track.current >= 0
      ? `here: ${track.pips[track.current].id}`
      : run.status === "done" ? "finished" : run.status;
  const wrap = el("div", "sess-run-here");
  const node = svg("svg", {
    class: `sess-run-track ${flowState(run)}`,
    viewBox: `${-m.cardW / 2} -18 ${m.cardW} 40`,
    // The line below is the picture's own caption, so it is also its name —
    // a role="img" with nothing to read out is worse than an unlabelled one.
    role: "img", "aria-label": `${wf.name || "workflow"}: ${here}`,
  });
  node.appendChild(flowTrackSvg(track, m));
  wrap.appendChild(node);
  const line = el("p", "sess-run-where", here);
  line.title = "◆ a branch · | a gate to enter or a verify to leave · ×n revisits";
  wrap.appendChild(line);
  return wrap;
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

   What is NOT drawn is the point of the design. The member graph used to be
   complete by default and was drawn as its exceptions — the cuts — because
   every pair would have been n² hairlines saying nothing. A join now wires a
   member to its parent and to whatever the mesh's rules match, and leaves the
   rest closed, so the sparse side has swapped: the pairs that CAN message are
   the few, and the ones that cannot are most of n² and carry no decision.
   So the open pairs get the line and nothing else does. Everything further
   answers on demand: click an agent and its reachable set lights up. And the
   transport behind a cross-machine conversation is a property of the two
   DAEMONS, not of the pair of agents — so it belongs on the cluster boundary,
   drawn once, rather than smeared over every member pair that crosses it.

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
/* The last edit's outcome, shown as a line under the diagram rather than as
   an alert(): a modal for "connected a <-> b" interrupts the very reading
   the edit was made to change, and a modal for a refusal takes the words
   away while the graph they are about is still on screen. */
let meshNotice = null;   // {text, bad} | null

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

/* `what` is the sentence to show when it works — the edits here are small
   and their effect is a line or a dot moving somewhere in a diagram, which
   is easy to miss, so each one says what it just did. */
async function meshEdit(path, options, what) {
  meshBusy = true;
  try {
    const resp = await api(path, {
      headers: { "Content-Type": "application/json" }, ...options,
    });
    if (!resp.ok) {
      const doc = await resp.json().catch(() => ({}));
      meshNotice = { text: doc.error || `HTTP ${resp.status}`, bad: true };
      return false;
    }
    if (what) meshNotice = { text: what, bad: false };
    return true;
  } catch (err) {
    // Includes the 401 api() throws after putting the login overlay up: the
    // panel behind it should not also claim the edit went through.
    meshNotice = { text: String((err && err.message) || err), bad: true };
    return false;
  } finally {
    meshBusy = false;
    await refreshMeshView(true);
    refreshMeshList();
  }
}

/* Putting a cut peer edge back.

   Cutting one is no longer offered here, and that is the point of this being
   half a toggle: the peer graph is meant to be a full interconnect — every
   daemon linked to every other, with the authority's fanout as the fallback
   rather than the plan — so there is no routine edit to make on it, and a
   clickable hairline that could take a link away by accident was a hazard
   with nothing on the other side of it. A cut edge is an anomaly against
   that shape (somebody used `claunch mesh cut`, or an older dashboard), so
   the one action left is the repair. What this page edits instead is the
   member graph one layer up, where a cut IS a decision somebody makes. */
function restoreEdge(info, edge, btn) {
  const { a, b } = edge;
  if (btn) { btn.disabled = true; btn.textContent = "restoring…"; }
  return meshEdit(
    `/api/mesh/${encodeURIComponent(info.name)}/links/` +
    `${encodeURIComponent(a)}/${encodeURIComponent(b)}`,
    { method: "PATCH", body: JSON.stringify({ enabled: true }) },
    `restored the direct link ${a} ↔ ${b}`
  );
}

/* One place decides what connecting or disconnecting two members means, so
   the diagram's switches and the list's buttons cannot drift apart.

   Connecting applies on the click: it grants, and a grant made in error is
   one click back. Disconnecting asks first, because it is not the peer
   graph's "take the slow road" — members are never routed around a cut, so
   the pair simply stops being able to speak, and mail already owed between
   them stops being chased on the spot. */
function setMemberLink(info, a, b, enabled, btn) {
  if (!enabled && !confirm(
    `Disconnect ${a} <-> ${b}?\n\n` +
    "They can no longer message each other: sends between them are refused, " +
    "and a '*' from either one skips the other."
  )) return Promise.resolve(false);
  if (btn) { btn.disabled = true; btn.textContent = "…"; }
  return meshEdit(
    `/api/mesh/${encodeURIComponent(info.name)}/members/` +
    `${encodeURIComponent(a)}/links/${encodeURIComponent(b)}`,
    { method: "PATCH", body: JSON.stringify({ enabled: !!enabled }) },
    `${enabled ? "connected" : "disconnected"} ${a} ↔ ${b}`
  );
}

/* The bulk edits behind 'connect to all' and 'isolate'. One PATCH per pair,
   because the daemon has no bulk route and inventing one in the browser
   would be a second way for the graph to change — but one confirmation and
   one redraw for the run, because n dialogs is a dialog nobody reads by the
   third and n redraws is a panel that flickers under the reader's cursor. */
async function wireEvery(info, from, handles, enabled, btn) {
  if (!handles.length) return;
  if (!enabled && !confirm(
    `Disconnect ${from} from ${handles.length} member` +
    `${handles.length === 1 ? "" : "s"}?\n\n` +
    `${from} can then message nobody, and nobody it, until something is ` +
    "connected again."
  )) return;
  if (btn) { btn.disabled = true; btn.textContent = "…"; }
  meshBusy = true;
  let done = 0, failed = "";
  try {
    for (const to of handles) {
      const resp = await api(
        `/api/mesh/${encodeURIComponent(info.name)}/members/` +
        `${encodeURIComponent(from)}/links/${encodeURIComponent(to)}`,
        {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: !!enabled }),
        }
      );
      if (!resp.ok) {
        const doc = await resp.json().catch(() => ({}));
        failed = doc.error || `HTTP ${resp.status}`;
        break;
      }
      done += 1;
    }
  } catch (err) {
    failed = String((err && err.message) || err);
  } finally {
    meshBusy = false;
    // How far it got, either way: a run that stopped halfway left a graph
    // that neither the request nor the refusal describes on its own.
    const verb = enabled ? "connected" : "disconnected";
    meshNotice = failed
      ? { text: `${verb} ${done} of ${handles.length}, then: ${failed}`, bad: true }
      : {
        text: `${verb} ${from} ${enabled ? "to" : "from"} ${done} member` +
              `${done === 1 ? "" : "s"}`,
        bad: false,
      };
    await refreshMeshView(true);
    refreshMeshList();
  }
}

/* The peer edges as text: what each daemon-to-daemon link is doing.

   Read-only, and that is a change of mind rather than an omission. This list
   used to be the reliable half of an editing surface whose other half was a
   clickable hairline — but the peer graph is not a shape an operator
   draws: every peer is linked to every other, and a link that is down or
   slow is covered by the authority's fanout rather than by somebody
   rewiring it. So what is left is a status board, plus one button for the
   one state that should not persist: an edge somebody cut. */
function renderPeerLinks(info) {
  const edges = info.links || [];
  const box = el("div", "mesh-links");
  box.appendChild(el("h3", null, "Peer links"));
  if (!edges.length) return box;
  const cutCount = edges.filter((e) => !e.enabled).length;
  box.appendChild(el(
    "p", cutCount ? "wf-warning" : "wf-note",
    cutCount
      ? `${cutCount} of ${edges.length} links ${cutCount === 1 ? "is" : "are"} `
        + `cut — that traffic goes through ${info.authority}, which still `
        + "delivers it, only slower. Restore them for a full interconnect."
      : "every daemon is linked to every other — nothing to edit here; the "
        + "authority's fanout carries whatever a link cannot"
  ));
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
    if (edge.enabled) {
      box.appendChild(row);
      continue;   // a healthy link needs no button pretending otherwise
    }
    if (edge.editable) {
      const btn = el("button", "wf-btn option", "Restore");
      btn.title = `put the direct link ${edge.a} ↔ ${edge.b} back`;
      btn.addEventListener("click", () => restoreEdge(info, edge, btn));
      row.appendChild(btn);
    } else {
      // Where the operator can act instead: a row that reads "cut" and
      // offers nothing is a dead end for whoever came to fix it.
      row.appendChild(el(
        "span", "wf-note",
        `not this daemon's edge — restore it on ${info.authority}, `
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
   `machine` is blank on the AUTHORITY's own members (federation v2), so the
   authority — rank 0, i.e. order[0] — fills in, matching how the daemon
   buckets them itself. Reading it as `info.self` is right only while we hold
   authority; on a mirror it draws the authority's agents inside our own
   cluster and leaves the authority's empty. */
function meshClusters(info) {
  const peers = info.peers || [];
  const order = peers.length
    ? peers.map((p) => p.machine)
    : [info.self || ""];
  const buckets = new Map(order.map((m) => [m, []]));
  for (const m of info.members || []) {
    const home = m.machine || order[0];
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
   different picture each time for a graph whose shape we already know.

   `m` is the metric block — RING for the dot-per-agent picture, a wider one
   for the flow view, whose agents are cards. Same engine either way: the two
   views must place the same mesh the same way, or they stop being two zoom
   levels of one thing. */
function layoutForest(forest, m = RING) {
  const pos = new Map();
  let col = 0;
  const place = (handle, depth) => {
    if (pos.has(handle)) return null;  // already placed: a cycle led back here
    pos.set(handle, null);             // reserve before recursing
    const xs = (forest.kids.get(handle) || [])
      .map((c) => place(c, depth + 1))
      .filter((x) => x !== null);
    const x = xs.length ? (xs[0] + xs[xs.length - 1]) / 2 : col++;
    pos.set(handle, { x: x * m.colW, y: depth * m.rowH });
    return x;
  };
  for (const root of forest.roots) {
    place(root, 0);
    col += 0.55;  // a gap between sibling trees, so two teams read as two
  }
  return pos;
}

/* Lay a cluster out in its own coordinates and measure the box it needs. */
function measureCluster(cluster, m = RING) {
  const pos = layoutForest(meshForest(cluster.members), m);
  const nodes = [...pos].map(([handle, p]) => ({ handle, ...p }));
  const xs = nodes.map((n) => n.x);
  const minX = nodes.length ? Math.min(...xs) : 0;
  const maxX = nodes.length ? Math.max(...xs) : 0;
  const maxY = nodes.length ? Math.max(...nodes.map((n) => n.y)) : 0;
  const offX = m.pad.x + m.colW / 2 - minX;
  for (const n of nodes) { n.x += offX; n.y += m.pad.top; }
  return {
    nodes,
    at: new Map(nodes.map((n) => [n.handle, n])),
    w: maxX - minX + m.colW + 2 * m.pad.x,
    h: maxY + m.leaf + m.pad.top + m.pad.bottom,
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
    ? ` · wiring ${meshFocus}: lit agents are the ones it can message, and `
      + "the ⊕ / ⊗ on each other agent connects or disconnects the pair "
      + `· click ${meshFocus} again to stop`
    : " · click an agent to see who it can message, and to wire it up";
  if (clusters.length < 2) {
    return "one daemon — clusters appear as others join" + focus;
  }
  // Reordering is the authority's, so only it is told about the drag. The
  // peer links themselves are no longer edited from here at all: they are a
  // full interconnect, and the mesh's own wiring is the member graph.
  return (info.primary === null
    ? "rank 0 holds the authority · drag a cluster onto another to reorder"
    : `rank 0 holds the authority — reorder on ${info.authority}`
  ) + focus;
}

/* The connect/disconnect switch that rides on an agent's disc while another
   agent is selected. A group of its own so it can carry its own hit area,
   its own tooltip and its own keyboard stop: the glyph is a few pixels and
   the ring around it is the target. Beside the disc rather than on it, so
   the disc keeps meaning what it always meant — select this one instead. */
function wireBadge(info, from, to, on) {
  const g = svg("g", {
    class: "mesh-wire " + (on ? "on" : "off"),
    transform: `translate(${RING.node - 2} ${-RING.node + 2})`,
    tabindex: "0", role: "button",
    "aria-label": `${on ? "disconnect" : "connect"} ${from} and ${to}`,
  });
  g.appendChild(svg("circle", { r: 9, class: "mesh-wire-disc" }));
  g.appendChild(svg(
    "text", { class: "mesh-wire-glyph", y: 4 }, on ? "×" : "+"
  ));
  g.appendChild(svg("title", {}, on
    ? `disconnect ${from} ↔ ${to} — they stop being able to message `
      + "each other"
    : `connect ${from} ↔ ${to} — let them message each other`));
  const act = (ev) => {
    // The badge sits inside the agent group, whose own click moves the
    // selection. Wiring a pair is not a change of selection.
    if (ev && ev.stopPropagation) ev.stopPropagation();
    if (ev && ev.preventDefault) ev.preventDefault();
    setMemberLink(info, from, to, !on);
  };
  g.addEventListener("click", act);
  g.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") act(ev);
  });
  return g;
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
  // What the last edit did, where the reader's eyes already are. Click to
  // dismiss; otherwise it stands until the next edit replaces it, because a
  // refusal that vanished on a timer would be a refusal nobody read.
  if (meshNotice) {
    const line = el(
      "p", "mesh-notice" + (meshNotice.bad ? " bad" : ""), meshNotice.text
    );
    line.title = "dismiss";
    line.addEventListener("click", () => {
      meshNotice = null;
      refreshMeshView(true);
    });
    box.appendChild(line);
  }

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
    if (!meshFocus || ev.target.closest(".mesh-agent")) return;
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
    // A 1.6px stroke is far too thin to hover (and a horizontal one has a
    // zero-height box), so a transparent fat line underneath carries the
    // tooltip while the visible one stays hairline. It is no longer a click
    // target: these edges are not edited from the diagram, and a hairline
    // that could sever a link on a stray click was the wrong thing to aim at.
    group.appendChild(svg("line", { ...ends, class: "mesh-edge-hit" }));
    group.appendChild(svg("line", { ...ends, class: `mesh-edge ${cls}` }));
    group.appendChild(svg("title", {}, `${edge.a} <-> ${edge.b} — ${cls}`));
    canvas.appendChild(group);
  }

  /* 3. spawn edges, inside their cluster. The pairs drawn here are recorded
        so step 4 does not draw the same relationship a second time as a
        straight line across the forest. */
  const spawnPair = new Set();
  for (const m of info.members || []) {
    const child = at.get(m.handle), parent = m.parent && at.get(m.parent);
    if (!child || !parent || parent.cluster !== child.cluster) continue;
    spawnPair.add([m.handle, m.parent].sort().join("|"));
    // An elbow rather than a diagonal: with several children the fan of
    // straight lines is hard to follow back to one parent.
    const mid = (parent.y + child.y) / 2;
    canvas.appendChild(svg("path", {
      class: "mesh-spawn",
      d: `M ${parent.x} ${parent.y} V ${mid} H ${child.x} V ${child.y}`,
    }));
  }

  /* 4. the member graph: who may message whom. Drawn as the pairs that CAN,
        which is the inversion the wiring bought. A join now connects a member
        to its parent and to whatever the mesh's rules match, and leaves the
        rest closed — so the open set is the sparse one and the informative
        one, while the closed set is most of n² and says only "nobody asked
        for this". A pair that cannot speak gets no line at all.

        The parent edge is skipped: it is already on the canvas as the spawn
        elbow, and drawing it twice would put a straight line across the tidy
        forest the elbows exist to keep. */
  for (const e of info.member_links || []) {
    if (!e.enabled) continue;
    const a = at.get(e.a), b = at.get(e.b);
    if (!a || !b) continue;
    // Keyed sorted at both ends: the daemon happens to emit sorted pairs, but
    // an edge is unordered and a suppression that only worked one way round
    // would put a stray line across one arbitrary half of the forest.
    if (spawnPair.has([e.a, e.b].sort().join("|"))) continue;
    const g = svg("g", { class: "mesh-mlink" });
    g.appendChild(svg("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y }));
    g.appendChild(svg("title", {}, `${e.a} ↔ ${e.b} — may message each other`));
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
      // What the Connections list points at when a row is hovered: the two
      // panels show one graph, and a handle in a list is a poor way to find
      // a dot in a forest.
      "data-handle": m.handle,
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
    // While an agent is selected, every OTHER agent wears the switch for the
    // pair. On the agent, because that is where the reader already is: the
    // alternative is finding one pair in a list of n² of them.
    if (meshFocus && m.handle !== meshFocus) {
      g.appendChild(wireBadge(info, meshFocus, m.handle, reachable.has(m.handle)));
    }
    canvas.appendChild(g);
  }
  box.appendChild(canvas);

  const legend = el("div", "mesh-legend");
  for (const [cls, label] of [
    ["ok", "linked"], ["queued", "queued"],
    ["down", "unreachable"], ["cut", "cut"],
    ["spawn", "spawned"], ["mlink", "may message"],
  ]) {
    const item = el("span", "mesh-legend-item");
    item.appendChild(el("i", `mesh-legend-swatch ${cls}`));
    item.appendChild(el("span", null, label));
    legend.appendChild(item);
  }
  box.appendChild(legend);
  return box;
}

/* ---- the member graph, as something you can edit ------------------------ */
/* Who may message whom, one agent at a time.

   Per agent and not per pair, because the pairs are n² and nobody arrives
   holding a question about the set of them: they arrive with "who can this
   worker reach?" or "why is the reviewer hearing nothing?". So the panel
   borrows the diagram's selection — the same click, the same highlight — and
   spends it on a row per other member with the switch on the row.

   Two surfaces for one edit, which is the arrangement the peer graph always
   had and the half of it worth keeping: the diagram is the quick one, and a
   badge inside a dense forest is a small target, so the list is the one that
   is always usable. Both call setMemberLink, so they cannot drift apart. */
function renderWiring(info) {
  const members = (info.members || []).slice()
    .sort((x, y) => (x.handle < y.handle ? -1 : x.handle > y.handle ? 1 : 0));
  const box = el("div", "mesh-wiring");
  const head = el("div", "mesh-wiring-head");
  head.appendChild(el("h3", null, "Connections"));
  box.appendChild(head);
  if (members.length < 2) {
    box.appendChild(el(
      "p", "wf-note",
      "a mesh needs two members before there is anything to wire — enrol "
      + "one below"
    ));
    return box;
  }

  // The diagram's selection, reachable without the diagram: a long roster is
  // easier to pick from a list than to find in a ring, and somebody who came
  // here to fix one agent should not have to hunt for its dot first.
  const pick = el("select", "mesh-wire-pick");
  const blank = el("option", null, "pick an agent…");
  blank.value = "";
  pick.appendChild(blank);
  for (const m of members) {
    const opt = el("option", null, `${m.handle} (${m.role})`);
    opt.value = m.handle;
    if (m.handle === meshFocus) opt.selected = true;
    pick.appendChild(opt);
  }
  pick.addEventListener("change", () => {
    meshFocus = pick.value || null;
    refreshMeshView(true);
  });
  head.appendChild(pick);

  if (!meshFocus) {
    // Nothing selected: show the wiring as it stands. The open pairs, not the
    // closed ones — a join wires a member to its parent and to whatever the
    // rules match and leaves the rest shut, so the open set is the short one
    // and the one somebody chose. (The diagram and the CLI agree on this.)
    const open = (info.member_links || []).filter((e) => e.enabled);
    box.appendChild(el(
      "p", "wf-note",
      open.length
        ? `${open.length} connected pair${open.length === 1 ? "" : "s"}. Pick an `
          + "agent above, or click one in the diagram, to change what it reaches"
        : "no pair is connected — nobody here can message anybody. Pick an "
          + "agent above to wire it up"
    ));
    for (const e of open) {
      const row = el("div", "mesh-member");
      row.appendChild(el("span", "mesh-link-swatch mlink"));
      row.appendChild(el("span", "mesh-handle mono", `${e.a} ↔ ${e.b}`));
      const btn = el("button", "wf-btn clear", "Disconnect");
      btn.title = "they stop being able to message each other";
      btn.addEventListener("click", () => setMemberLink(info, e.a, e.b, false, btn));
      row.appendChild(btn);
      box.appendChild(row);
    }
    return box;
  }

  const me = members.find((m) => m.handle === meshFocus);
  const others = members.filter((m) => m.handle !== meshFocus);
  const reach = meshReachable(info, meshFocus);
  const on = others.filter((m) => reach.has(m.handle));
  box.appendChild(el(
    "p", "wf-note",
    `${meshFocus} can message ${on.length} of ${others.length}. A disconnected `
    + "pair is not sent the long way round like a cut peer link — members are "
    + "not routed at all, so the send is simply refused"
  ));

  // The two edits worth having as one gesture: wire this agent to everybody,
  // or take it out of the conversation entirely.
  const bulk = el("div", "mesh-wire-bulk");
  const off = others.filter((m) => !reach.has(m.handle));
  if (off.length) {
    const all = el("button", "wf-btn option", "connect to all");
    all.title = `let ${meshFocus} message every other member (${off.length} to add)`;
    all.addEventListener("click", () => wireEvery(
      info, meshFocus, off.map((m) => m.handle), true, all
    ));
    bulk.appendChild(all);
  }
  if (on.length) {
    const iso = el("button", "wf-btn archive", "isolate");
    iso.title = `disconnect ${meshFocus} from every other member`;
    iso.addEventListener("click", () => wireEvery(
      info, meshFocus, on.map((m) => m.handle), false, iso
    ));
    bulk.appendChild(iso);
  }
  const done = el("button", "wf-btn clear", "done");
  done.title = "clear the selection";
  done.addEventListener("click", () => { meshFocus = null; refreshMeshView(true); });
  bulk.appendChild(done);
  box.appendChild(bulk);

  for (const m of others) {
    const linked = reach.has(m.handle);
    const row = el("div", "mesh-member" + (linked ? " linked" : ""));
    row.appendChild(el("span", `dot ${meshDotClass(m.reachability)}`));
    row.appendChild(el("span", "mesh-handle", m.handle));
    row.appendChild(el("span", "mesh-role", m.role));
    // Lineage, where there is any. Cutting the edge along a spawn is the one
    // disconnect with a second consequence: the briefing a child was given
    // tells it to report to its parent, and a report it cannot send is a run
    // that stalls with nobody told why.
    const kin = m.parent === meshFocus ? "child"
      : (me && me.parent === m.handle ? "parent" : "");
    if (kin) row.appendChild(el("span", "mesh-kin", kin));
    row.appendChild(el("span", "meta", linked ? "connected" : "not connected"));
    const btn = el(
      "button", linked ? "wf-btn clear" : "wf-btn option",
      linked ? "Disconnect" : "Connect"
    );
    btn.title = linked
      ? `${meshFocus} and ${m.handle} stop being able to message each other`
        + (kin === "child" ? ` — and ${m.handle} reports to ${meshFocus}` : "")
        + (kin === "parent" ? ` — and ${meshFocus} reports to ${m.handle}` : "")
      : `let ${meshFocus} and ${m.handle} message each other`;
    btn.addEventListener(
      "click", () => setMemberLink(info, meshFocus, m.handle, !linked, btn)
    );
    row.appendChild(btn);
    hoverLink(row, m.handle);
    box.appendChild(row);
  }
  return box;
}

/* Hovering a row lights the agent it names in the diagram above.

   Looked up at hover time rather than held as a reference, because the 2s
   poll rebuilds both panels and a node captured at render time would soon be
   lighting something that is no longer on the page. Guarded, because the
   test harness's stub DOM has no query engine and this is decoration. */
function hoverLink(row, handle) {
  if (!document.querySelectorAll) return;
  const mark = (lit) => {
    for (const n of document.querySelectorAll(".mesh-agent")) {
      if (n.getAttribute("data-handle") === handle) n.classList.toggle("hot", lit);
    }
  };
  row.addEventListener("mouseenter", () => mark(true));
  row.addEventListener("mouseleave", () => mark(false));
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
  // The same mesh with the runs drawn in. A link rather than a section: the
  // roster answers "who is here", and adding "how far along is each of them"
  // to the same page would make the answer to neither easy to find.
  const flowLink = el("a", "wf-btn option", "flow view");
  flowLink.href = "#/mesh/" + encodeURIComponent(info.name) + "/flows";
  flowLink.title = "the same topology, with every agent's workflow inside it";
  head.appendChild(flowLink);
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
  // Who may message whom is the mesh's own shape and the thing an operator
  // actually rewires, so it sits directly under the picture of it. The peer
  // links are transport, and follow as a status board.
  view.appendChild(renderWiring(info));
  if ((info.links || []).length) view.appendChild(renderPeerLinks(info));

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

/* One button press against the ledger below: disable while it is in flight
   (the 2s poll would otherwise repaint a live button under a pointer that
   has already clicked), report the daemon's refusal verbatim, and redraw
   from the daemon rather than from what we hoped happened. */
async function owedAct(btn, call) {
  btn.disabled = true;
  try {
    const resp = await call();
    if (!resp.ok) {
      const doc = await resp.json().catch(() => ({}));
      alert(doc.error || `HTTP ${resp.status}`);
      return;
    }
  } catch {
    return;               // api() has already dealt with a lost session
  } finally {
    btn.disabled = false;
  }
  refreshMeshView(true);
  refreshMeshList();
  // The same ledger is drawn into the trace's margin, and these buttons are
  // reachable from there too (that page shows this very box).
  if (traceSession) refreshTrace();
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
   in as many words, because an unattended list reads as a handled one.

   Each row also carries the two answers to what it shows: nudge (ask again
   now) and dismiss (stop asking). Both are the daemon's to perform and the
   daemon's to allow — the buttons only appear where it says they can. */
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
    "member, so what is left is mail nobody has acknowledged at all. " +
    "Nudge asks again now; dismiss writes the debt off without an answer."
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
    // The two things an operator can do about a row, next to the row itself:
    // ask again, or stop asking. Which of them this daemon can actually
    // perform is the report's call (`can_nudge` / `can_dismiss`) — a remote
    // member's mail is counted on its own daemon, and only the authority can
    // reach that daemon at all.
    if (r.can_nudge) {
      const nudge = el("button", "mesh-owed-btn", "nudge");
      nudge.title =
        `inject the heartbeat reminder into ${r.handle} now` +
        (r.local ? "" : ` — queued for ${r.machine} to deliver`);
      nudge.addEventListener("click", () => owedAct(nudge, () => api(
        `/api/mesh/${encodeURIComponent(info.name)}/members/` +
        `${encodeURIComponent(r.handle)}/nudge`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }
      )));
      head.appendChild(nudge);
    }
    if (r.can_dismiss) {
      const drop = el("button", "mesh-owed-btn", "dismiss all");
      drop.title = `write off all ${r.owed} unanswered message(s) for ${r.handle}`;
      drop.addEventListener("click", () => {
        if (r.owed > 1 && !confirm(
          `Dismiss all ${r.owed} unanswered messages for '${r.handle}'?\n\n` +
          "They stay in the message log; they just stop counting as a debt."
        )) return;
        owedAct(drop, () => api(
          `/api/mesh/${encodeURIComponent(info.name)}/members/` +
          `${encodeURIComponent(r.handle)}/owed`,
          { method: "DELETE" }
        ));
      });
      head.appendChild(drop);
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
      if (r.can_dismiss && m.id) {
        const x = el("button", "mesh-owed-x", "×");
        x.title = "dismiss just this message";
        x.addEventListener("click", () => owedAct(x, () => api(
          `/api/mesh/${encodeURIComponent(info.name)}/members/` +
          `${encodeURIComponent(r.handle)}/owed/${encodeURIComponent(m.id)}`,
          { method: "DELETE" }
        )));
        meta.appendChild(x);
      }
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
      "the heartbeat nudge is OFF for this mesh — nothing is chasing these " +
      "on its own; nudge a member by hand above, switch the heartbeat on " +
      "under Nudge policy, or message the member yourself below"
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
    "picks its role. The same document carries auto_link — which pairs a " +
    "join connects — so a rule can name a role and be checked against it. " +
    "Editing either is never retroactive."
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
/* flow topology (#/mesh/<name>/flows) — the mesh, and how far along   */
/* every agent in it is                                                */
/* ------------------------------------------------------------------ */
/* The mesh page answers "who is here, and who may speak to whom". The flows
   page answers "which runs exist, and where do they stand". Neither answers
   the question a person actually arrives with, which is the join of the two:
   *which of my agents is stuck, and who do I ask about it*. Reading it today
   means holding a roster in your head while scrolling a list of runs.

   So this view puts the run inside the node. Same mesh, same clusters, same
   spawn forest, same cuts — the layout engine is literally the one the other
   diagram uses, called with wider metrics — but every agent is a card, and
   the card carries its whole state machine as a track:

       ●───●───◆───◉───○───▷        visited · select · HERE · ahead · end
           │       └ gate/verify bars flank the pip they belong to
           └────────────────┘        a back arc: this workflow loops

   The track is the workflow breadth-first from `start` — the same order the
   run page stacks its rows in, so the strip is that diagram turned on its
   side, and the two are readable as one picture at two zoom levels. Steps
   that terminate grow an `end` pip at the tail. Edges that a straight rail
   already implies (i -> i+1) are not drawn; only the ones that say something
   are: a skip forward arcs over the rail, a loop back arcs under it.

   What the picture is FOR, in one rule: the loudest thing on it is an agent
   waiting on a human. Those cards get a halo, and they are repeated above
   the canvas with the actual Approve / option buttons, because a monitor you
   cannot act from just sends you somewhere else to act. Everything else —
   the full state machine, the reports, the journal — is one click away, on
   the card, rather than in the picture. A dense diagram that answers the
   wrong question is worse than a sparse one that answers the right one. */
const FLOW = {
  pip: 6,          // pip radius; the gate/verify bars flank it
  gap: 22,         // pip centre to pip centre along the rail
  trackPad: 18,    // rail inset from the card's edge
  cardH: 78,
  minCard: 158, maxCard: 320,
};
let flowMesh = null;      // mesh whose flow view is open
let flowPollTimer = null;
let flowPick = null;      // handle whose full state machine is expanded
let flowLast = null;      // last {info, data}, for instant re-render on a pick

/* Card size, and how tight the pips have to sit to fit the longest track in
   this mesh. One geometry for every card: a workflow's progress is only
   comparable across agents if the tracks line up. */
function flowMetrics(maxPips) {
  const span = FLOW.gap * Math.max(0, maxPips - 1);
  const wanted = span + 2 * FLOW.trackPad;
  const cardW = Math.min(FLOW.maxCard, Math.max(FLOW.minCard, wanted));
  const gap = wanted > FLOW.maxCard && maxPips > 1
    ? (FLOW.maxCard - 2 * FLOW.trackPad) / (maxPips - 1)
    : FLOW.gap;
  return {
    cardW, cardH: FLOW.cardH, gap,
    // The RING contract, in card units — see layoutForest/measureCluster.
    // pad.top must clear the machine-name header AND the half-card that
    // hangs above the first row's centre line.
    node: FLOW.cardH / 2, colW: cardW + 26, rowH: FLOW.cardH + 36,
    leaf: FLOW.cardH / 2 + 12, gapRing: 46,
    pad: { x: 26, top: 34 + FLOW.cardH / 2, bottom: 16 },
  };
}

/* The steps of a workflow in the order the run page lays them out: breadth
   first from `start`, then whatever the walk could not reach (an orphaned
   step is a mistake worth seeing, not worth hiding). Deliberately the same
   walk as wfDiagramSvg's — not shared with it, because that one builds a
   string and this one builds data, but a test pins the two orders together.
   If they ever drift, the strip stops being the run page's diagram. */
function flowOrder(wf) {
  const steps = wf.steps || [];
  const byId = {};
  for (const s of steps) byId[s.id] = s;
  const outs = (s) => (s.select ? s.select.options.map((o) => o.next) : [s.next]);
  const order = [];
  const seen = new Set();
  const queue = [wf.start];
  while (queue.length) {
    const id = queue.shift();
    if (!id || seen.has(id) || !byId[id]) continue;
    seen.add(id);
    order.push(id);
    for (const t of outs(byId[id])) if (t && !seen.has(t)) queue.push(t);
  }
  for (const s of steps) if (!seen.has(s.id)) order.push(s.id);
  return order;
}

/* One agent's whole state machine, squeezed onto a line. Pure: everything
   the drawing needs, and nothing about pixels. */
function flowTrack(wf, run) {
  const order = flowOrder(wf);
  const byId = {};
  for (const s of wf.steps || []) byId[s.id] = s;
  const outs = (s) => (s.select ? s.select.options.map((o) => o.next) : [s.next]);
  const index = new Map(order.map((id, i) => [id, i]));
  const visits = (run && run.visits) || {};
  const status = (run && run.status) || "";
  // A finished run is nowhere: leaving step_id lit would claim it is still
  // working on the step it stopped at.
  const here = status === "done" || status === "aborted"
    ? "" : (run && run.step_id) || "";

  const pips = order.map((id) => {
    const s = byId[id];
    return {
      id,
      kind: s.select ? "select" : "step",
      gate: !!s.gate,
      verify: !!s.verify,
      visits: visits[id] || 0,
      state: id === here ? "current" : (visits[id] ? "visited" : "ahead"),
    };
  });
  if (order.some((id) => outs(byId[id]).some((t) => !t))) {
    pips.push({
      id: "end", kind: "end", gate: false, verify: false, visits: 0,
      state: status === "done" ? "current" : "ahead",
    });
    index.set("end", pips.length - 1);
  }

  const arcs = [];
  const drawn = new Set();
  for (const id of order) {
    for (const t of outs(byId[id])) {
      const to = t ? t : "end";
      if (!index.has(to)) continue;   // a `next` naming nothing: no edge to draw
      const i = index.get(id), j = index.get(to);
      const key = `${i}>${j}`;
      if (j === i + 1 || drawn.has(key)) continue;  // the rail already says it
      drawn.add(key);
      arcs.push({ from: i, to: j, back: j <= i });
    }
  }
  return {
    pips, arcs,
    current: here && index.has(here) ? index.get(here) : -1,
    // The graphs are shared per workflow@cwd, so a re-run over an edited YAML
    // can put the run on a step this snapshot has never heard of. Say so
    // rather than drawing a track with nothing lit on it.
    offGraph: !!here && !index.has(here),
  };
}

/* Blocked on a HUMAN — the one state in this picture a person can clear. An
   agent-chooser select is the agent's own call and must not read as a queue
   for the operator; neither is a gate in front of a session that has exited,
   since approving it would unblock a run nobody is driving. */
function flowNeedsHuman(f) {
  if (!f || f.remote || f.stopped) return false;
  if (f.status === "waiting_approval" || f.status === "waiting_selection") return true;
  return f.status === "select" && f.chooser === "user";
}

/* One word for the whole card, and the class that colours it. */
function flowState(f) {
  if (!f || f.remote) return "unknown";
  if (f.status === "error") return "error";
  if (f.status === "done") return "done";
  if (f.status === "aborted") return "aborted";
  // A finished run is finished whoever is (or is not) in front of it. An
  // UNfinished one whose session has exited is where the agent left it —
  // reporting that position as "running" is the reading this page exists to
  // prevent, so it outranks everything below.
  if (f.stopped) return "stopped";
  if (flowNeedsHuman(f)) return "blocked";
  // Waiting, but on a peer rather than on us — a distinct word, or the card
  // reads as "running" while nothing is happening.
  if (f.status === "waiting_answer") return "delegated";
  if (f.status === "select") return "deciding";
  if (!f.status || f.status === "idle" ||
      f.status === "no_session" || f.status === "no_cwd") return "none";
  return "running";
}

const FLOW_WORDS = {
  blocked: "waiting on you", running: "running", deciding: "agent deciding",
  delegated: "waiting on a peer",
  done: "done", aborted: "aborted", error: "error",
  stopped: "session stopped", none: "no run",
  unknown: "run lives on its own daemon",
};

/* ---- drawing ---------------------------------------------------------- */
function flowPipShape(pip, x, y) {
  const r = FLOW.pip;
  if (pip.kind === "select") {
    return svg("path", {
      class: "flow-pip-mark",
      d: `M ${x} ${y - r - 1} L ${x + r + 1} ${y} L ${x} ${y + r + 1} ` +
         `L ${x - r - 1} ${y} Z`,
    });
  }
  return svg("circle", { class: "flow-pip-mark", cx: x, cy: y, r });
}

/* The track, in card coordinates (0,0 = the card's centre). */
function flowTrackSvg(track, m) {
  const g = svg("g", { class: "flow-track" });
  const y = 4;
  // Centred, not left-aligned: the pip spacing is one number for the whole
  // mesh, so two agents on the same workflow still line up pip for pip, and
  // a short workflow beside a long one does not sit in a lopsided card.
  const x0 = -(m.gap * (track.pips.length - 1)) / 2;
  const xOf = (i) => x0 + i * m.gap;
  const last = track.pips.length - 1;
  if (last > 0) {
    g.appendChild(svg("line", {
      class: "flow-rail", x1: xOf(0), y1: y, x2: xOf(last), y2: y,
    }));
  }
  for (const a of track.arcs) {
    const xa = xOf(a.from), xb = xOf(a.to);
    // Kept shallow deliberately: the card's two text lines sit ~17px either
    // side of the rail, and an arc that reached them would read as a strike
    // through the words rather than as an edge.
    const lift = a.back ? 11 : -11;
    g.appendChild(svg("path", {
      class: `flow-arc ${a.back ? "back" : "skip"}`,
      d: `M ${xa} ${y + (a.back ? FLOW.pip : -FLOW.pip)} ` +
         `Q ${(xa + xb) / 2} ${y + lift * 2} ` +
         `${xb} ${y + (a.back ? FLOW.pip : -FLOW.pip)}`,
    }));
  }
  track.pips.forEach((pip, i) => {
    const x = xOf(i);
    const node = svg("g", { class: `flow-pip ${pip.state} ${pip.kind}` });
    if (pip.state === "current") {
      node.appendChild(svg("circle", {
        class: "flow-halo", cx: x, cy: y, r: FLOW.pip + 5,
      }));
    }
    // A gate is a door you must be let through to ENTER; a verify is one you
    // must pass to LEAVE. Same mark, the side says which.
    if (pip.gate) {
      g.appendChild(svg("line", {
        class: "flow-bar gate", x1: x - FLOW.pip - 4, y1: y - 6,
        x2: x - FLOW.pip - 4, y2: y + 6,
      }));
    }
    if (pip.verify) {
      g.appendChild(svg("line", {
        class: "flow-bar verify", x1: x + FLOW.pip + 4, y1: y - 6,
        x2: x + FLOW.pip + 4, y2: y + 6,
      }));
    }
    node.appendChild(flowPipShape(pip, x, y));
    if (pip.kind === "end") {
      node.appendChild(svg("circle", { class: "flow-pip-core", cx: x, cy: y, r: 2.5 }));
    }
    if (pip.visits > 1) {
      node.appendChild(svg(
        "text", { class: "flow-visits", x, y: y - FLOW.pip - 6, "text-anchor": "middle" },
        `×${pip.visits}`
      ));
    }
    node.appendChild(svg("title", {}, [
      pip.id,
      pip.kind === "select" ? "branch" : null,
      pip.gate ? "gate to enter" : null,
      pip.verify ? "verify to leave" : null,
      pip.state === "current" ? "here now"
        : pip.visits ? `visited ${pip.visits}×` : "not reached",
    ].filter(Boolean).join(" · ")));
    g.appendChild(node);
  });
  return g;
}

function flowCardSvg(member, f, wf, m) {
  const state = flowState(f);
  const g = svg("g", {
    class: `flow-card ${state}` + (flowPick === member.handle ? " picked" : ""),
  });
  g.appendChild(svg("rect", {
    class: "flow-card-box", x: -m.cardW / 2, y: -m.cardH / 2,
    width: m.cardW, height: m.cardH, rx: 9,
  }));
  const left = -m.cardW / 2 + 12, right = m.cardW / 2 - 12, top = -m.cardH / 2;
  g.appendChild(svg("circle", {
    class: `flow-dot ${meshDotClass(member.reachability)}`,
    cx: left + 4, cy: top + 16, r: 4,
  }));
  g.appendChild(svg(
    "text", { class: "flow-handle", x: left + 14, y: top + 20 },
    member.handle.length > 16 ? `${member.handle.slice(0, 15)}…` : member.handle
  ));
  const wfName = (f && f.workflow) || "";
  g.appendChild(svg(
    "text", { class: "flow-wf", x: right, y: top + 20, "text-anchor": "end" },
    wfName.length > 18 ? `${wfName.slice(0, 17)}…` : (wfName || "—")
  ));

  if (wf && f) {
    const track = flowTrack(wf, f);
    g.appendChild(flowTrackSvg(track, m));
    g.appendChild(svg(
      "text", { class: "flow-foot", x: left, y: m.cardH / 2 - 10 },
      track.offGraph
        ? `${f.step_id} — not in this workflow snapshot`
        : (f.step_id || FLOW_WORDS[state])
    ));
    g.appendChild(svg(
      "text", { class: `flow-state ${state}`, x: right, y: m.cardH / 2 - 10,
                "text-anchor": "end" },
      FLOW_WORDS[state]
    ));
  } else {
    // Nothing to track. The card says why in the space the track would have
    // taken rather than in the corner, because on this page a card with no
    // line through it is the thing a reader stops at.
    g.appendChild(svg(
      "text", { class: `flow-foot none ${state}`, x: left, y: 10 },
      f && f.graph_error ? "workflow snapshot unreadable" : FLOW_WORDS[state]
    ));
  }
  g.appendChild(svg("title", {}, [
    `${member.handle} (${member.role})`,
    member.session,
    wfName ? `workflow ${wfName}` : "no workflow run",
    FLOW_WORDS[state],
    member.parent ? `spawned by ${member.parent}` : null,
  ].filter(Boolean).join(" · ")));
  g.addEventListener("click", () => {
    flowPick = flowPick === member.handle ? null : member.handle;
    if (flowLast) renderFlowTopo(flowLast.info, flowLast.data);
  });
  return g;
}

/* ---- the page --------------------------------------------------------- */
function renderFlowTopo(info, data) {
  const view = $("flow-view");
  view.innerHTML = "";
  const flows = data.flows || {};
  const graphs = data.workflows || {};
  const members = info.members || [];
  const wfFor = (handle) => {
    const f = flows[handle];
    return f && f.key ? graphs[f.key] || null : null;
  };

  const head = el("div", "wf-head");
  head.appendChild(el("h2", null, `flows: ${info.name}`));
  const back = el("a", "wf-btn option", "mesh view");
  back.href = "#/mesh/" + encodeURIComponent(info.name);
  back.title = "the same mesh without the workflows: links, roster, history";
  head.appendChild(back);
  view.appendChild(head);
  view.appendChild(el(
    "p", "wf-desc",
    "every agent in the mesh, with its workflow run drawn into it — " +
    "click a card for the full state machine"
  ));

  if (!members.length) {
    view.appendChild(el("p", "wf-note", "no members yet — nothing to draw"));
    return;
  }

  // Blocked runs first, in text, with the buttons that clear them: the
  // picture is where you notice, this is where you act.
  const waiting = members.filter((m) => flowNeedsHuman(flows[m.handle]));
  const strip = el("div", "flow-waiting");
  strip.appendChild(el("h3", null, waiting.length
    ? `Waiting on you (${waiting.length})`
    : "Waiting on you"));
  if (!waiting.length) {
    strip.appendChild(el("p", "wf-note", "nothing in this mesh is blocked on a human"));
  }
  for (const m of waiting) strip.appendChild(flowWaitingRow(m, flows[m.handle]));
  view.appendChild(strip);

  const maxPips = Math.max(1, ...members.map((m) => {
    const wf = wfFor(m.handle);
    return wf ? flowTrack(wf, flows[m.handle]).pips.length : 1;
  }));
  const m = flowMetrics(maxPips);
  const clusters = meshClusters(info).map((c) => ({ ...c, ...measureCluster(c, m) }));
  const cell = m.gapRing + Math.max(...clusters.map(
    (c) => (clusters.length === 2 ? c.h : Math.max(c.w, c.h))
  ));
  const radius = ringRadius(clusters.length, cell);
  clusters.forEach((c, i) => {
    const p = ringPoint(i, clusters.length, radius);
    c.cx = p.x; c.cy = p.y;
    c.left = p.x - c.w / 2; c.top = p.y - c.h / 2;
  });
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
    width: Math.round(vb.w), height: Math.round(vb.h), class: "flow-ring",
  });

  /* 1. the clusters, behind what they hold */
  for (const c of clusters) {
    const rank = c.peer ? c.peer.rank : 0;
    const g = svg("g", {
      class: "mesh-cluster"
        + (c.peer && c.peer.self ? " self" : "")
        + (rank === 0 && c.peer ? " authority" : "")
        + (c.peer && c.peer.ok === false ? " down" : ""),
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
    canvas.appendChild(g);
  }

  /* 2. peer edges at the cluster boundary — unchanged from the mesh view,
        because the transport is a property of the daemons either side */
  const byMachine = {};
  for (const p of info.peers || []) byMachine[p.machine] = p;
  const byName = Object.fromEntries(clusters.map((c) => [c.machine, c]));
  for (const edge of info.links || []) {
    const a = byName[edge.a], b = byName[edge.b];
    if (!a || !b) continue;
    const cls = edgeClass(edge, byMachine);
    const ea = boxExit(a, b.cx, b.cy), eb = boxExit(b, a.cx, a.cy);
    const g = svg("g", { class: `mesh-edge-group ${cls}` });
    g.appendChild(svg("line", {
      x1: ea.x, y1: ea.y, x2: eb.x, y2: eb.y, class: `mesh-edge ${cls}`,
    }));
    g.appendChild(svg("title", {}, `${edge.a} <-> ${edge.b} — ${cls}`));
    canvas.appendChild(g);
  }

  /* 3. spawn edges, card edge to card edge rather than centre to centre.
        Recorded, so step 4 does not draw the same relationship again as a
        straight line across the forest. */
  const spawnPair = new Set();
  for (const mem of members) {
    const child = at.get(mem.handle), parent = mem.parent && at.get(mem.parent);
    if (!child || !parent || parent.cluster !== child.cluster) continue;
    spawnPair.add([mem.handle, mem.parent].sort().join("|"));
    const mid = (parent.y + m.cardH / 2 + child.y - m.cardH / 2) / 2;
    canvas.appendChild(svg("path", {
      class: "mesh-spawn",
      d: `M ${parent.x} ${parent.y + m.cardH / 2} V ${mid} ` +
         `H ${child.x} V ${child.y - m.cardH / 2}`,
    }));
  }

  /* 4. the member graph, the same way the mesh view draws it: the pairs that
        CAN message. A join wires a member to its parent and to whatever the
        mesh's rules match and leaves the rest closed, so the open set is the
        sparse and informative one. The parent edge is already on the canvas
        as the spawn elbow and is not drawn twice. */
  for (const e of info.member_links || []) {
    if (!e.enabled) continue;
    const a = at.get(e.a), b = at.get(e.b);
    if (!a || !b) continue;
    if (spawnPair.has([e.a, e.b].sort().join("|"))) continue;
    const g = svg("g", { class: "mesh-mlink" });
    g.appendChild(svg("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y }));
    g.appendChild(svg("title", {}, `${e.a} ↔ ${e.b} — may message each other`));
    canvas.appendChild(g);
  }

  /* 5. the agents, each carrying its own run */
  for (const mem of members) {
    const p = at.get(mem.handle);
    if (!p) continue;
    const card = flowCardSvg(mem, flows[mem.handle], wfFor(mem.handle), m);
    card.setAttribute("transform", `translate(${p.x} ${p.y})`);
    canvas.appendChild(card);
  }
  view.appendChild(canvas);

  const legend = el("div", "flow-legend");
  for (const [cls, label] of [
    ["visited", "visited"], ["current", "here now"], ["ahead", "not reached"],
    ["select", "branch"], ["gate", "gate in"], ["verify", "verify out"],
    ["back", "loops back"], ["end", "terminates"],
  ]) {
    const item = el("span", "mesh-legend-item");
    item.appendChild(el("i", `flow-legend-swatch ${cls}`));
    item.appendChild(el("span", null, label));
    legend.appendChild(item);
  }
  view.appendChild(legend);

  if (flowPick) {
    const mem = members.find((x) => x.handle === flowPick);
    if (!mem) flowPick = null;      // it left while its card was open
    else view.appendChild(flowDetail(info, mem, flows[flowPick], wfFor(flowPick)));
  }
}

/* One blocked run, and the button that unblocks it. */
function flowWaitingRow(member, f) {
  const row = el("div", "flow-waiting-row");
  const who = el("span", "mesh-handle linkish", member.handle);
  who.title = "attach this session's terminal";
  who.addEventListener("click", () => {
    location.hash = "#/s/" + encodeURIComponent(f.session || member.session);
  });
  row.append(who, el("span", "mesh-role", member.role));
  row.appendChild(el("span", "flow-waiting-what",
    f.gate || f.prompt || `step ${f.step_id || "?"}`));
  const act = el("span", "flow-waiting-acts");
  if (f.status === "waiting_approval") {
    const btn = el("button", "wf-btn approve",
      f.reason === "loop_limit" ? "Extend loop limit" : "Approve gate");
    btn.addEventListener("click", async () => {
      if (!confirm(`Approve '${f.step_id}' for ${member.handle}?`)) return;
      await cflowAction("/api/cflow/approve", { cwd: f.cwd, scope: f.scope });
      refreshFlowView();
    });
    act.appendChild(btn);
  } else {
    for (const o of f.options || []) {
      const btn = el("button", "wf-btn option", o.name);
      btn.title = o.description || "";
      btn.addEventListener("click", async () => {
        if (!confirm(`Select '${o.name}' for ${member.handle}?`)) return;
        await cflowAction("/api/cflow/select",
          { cwd: f.cwd, scope: f.scope, option: o.name });
        refreshFlowView();
      });
      act.appendChild(btn);
    }
  }
  row.appendChild(act);
  return row;
}

/* The card's own truth, expanded: the run page's full diagram, unchanged, so
   the compact track is an index into something and not a replacement for it. */
function flowDetail(info, member, f, wf) {
  const box = el("div", "flow-detail");
  const head = el("div", "flow-detail-head");
  head.appendChild(el("h3", null, `${member.handle} · ${member.role}`));
  const close = el("button", "wf-btn clear", "Close");
  close.addEventListener("click", () => {
    flowPick = null;
    if (flowLast) renderFlowTopo(flowLast.info, flowLast.data);
  });
  head.appendChild(close);
  box.appendChild(head);

  const meta = el("div", "wf-meta");
  const attach = el("a", "wf-session", `attach: ${member.session}`);
  attach.href = "#/s/" + encodeURIComponent(member.session);
  meta.appendChild(attach);
  if (member.parent) meta.appendChild(el("span", null, `spawned by ${member.parent}`));
  const reach = [...meshReachable(info, member.handle)];
  meta.appendChild(el("span", null,
    reach.length ? `can message ${reach.join(", ")}` : "can message nobody"));
  if (f && f.cwd) {
    const run = el("a", "wf-session", "open the run page");
    run.href = "#/wf/" + encodeURIComponent(`${f.scope}|${f.cwd}`);
    meta.appendChild(run);
  }
  box.appendChild(meta);

  if (!f || f.remote) {
    box.appendChild(el("p", "wf-note",
      "this member runs on another daemon — open the flow view there"));
    return box;
  }
  if (f.graph_error) box.appendChild(el("p", "wf-warning", f.graph_error));
  // Above the graph, because it changes what the graph means: this is where
  // the run got to, not where it is going. Its session is resumable, so the
  // attach link above is the way to pick it back up.
  if (f.stopped) {
    box.appendChild(el("p", "wf-warning",
      `session '${member.session}' has exited — its run is where it stopped`));
  }
  if (!wf) {
    box.appendChild(el("p", "wf-note",
      f.status === "no_session"
        ? "this session is not a record here any more — nothing to show"
        : f.status === "no_cwd"
          ? "its session has no working directory, and a run is keyed by one"
          : "no cflow run in this session's directory"));
    return box;
  }
  const dia = el("div", "wf-diagram");
  dia.innerHTML = wfDiagramSvg(wf, f, null);
  box.appendChild(dia);
  return box;
}

function stopFlowPoll() {
  if (flowPollTimer) { clearInterval(flowPollTimer); flowPollTimer = null; }
  flowMesh = null;
  flowLast = null;
}

async function openFlowTopology(name) {
  if (flowPollTimer) clearInterval(flowPollTimer);
  flowMesh = name;
  flowPick = null;
  showView("flow");
  $("flow-view").innerHTML = "<p class='wf-note'>loading…</p>";
  await refreshFlowView();
  flowPollTimer = setInterval(refreshFlowView, 2000);
}

async function refreshFlowView() {
  if (!flowMesh) return;
  let info, data;
  try {
    const [r1, r2] = await Promise.all([
      api(`/api/mesh/${encodeURIComponent(flowMesh)}`),
      api(`/api/mesh/${encodeURIComponent(flowMesh)}/flows`),
    ]);
    info = await r1.json();
    if (!r1.ok) {
      $("flow-view").innerHTML = "";
      $("flow-view").appendChild(el("p", "wf-warning", info.error || "cannot load mesh"));
      return;
    }
    // A daemon too old to know the route still draws the topology, with
    // every card reading "no run" — degraded, not broken.
    data = r2.ok ? await r2.json() : { flows: {}, workflows: {} };
  } catch {
    return;
  }
  flowLast = { info, data };
  renderFlowTopo(info, data);
}

/* ------------------------------------------------------------------ */
/* message trace (#/msg/<name>) — what a session said, and was told    */
/* ------------------------------------------------------------------ */
/* The third reading of a session. The terminal says what it is doing now;
   the run page says how far through its workflow it is; this says who it has
   been working WITH — read as a sequence, top to bottom, so an afternoon's
   collaboration is a story rather than a scroll of chat lines.

   The mesh page already lists the same messages. What it cannot show is the
   shape: which of them crossed which pair, what was asked and never answered,
   and what the session was doing between them. That shape is the whole point
   of drawing it as lanes.

   Deliberately not the focus session's mailbox alone. A message from lead to
   reviewer is the reason the next one arrived here, and reading this session's
   half of it explains nothing — so the whole room is drawn, and everything the
   focus session is not part of is faded rather than dropped. */
let traceSession = null;   // the session the trace is about (null = closed)
let traceMesh = "";        // the mesh tab on screen ("" = not chosen yet)
let tracePollTimer = null;
let traceLast = null;      // last payload, for an instant redraw on expand
let traceOpen = new Set(); // message ids expanded to their full body

/* Slower than the terminal's rail and the mesh page (2s): this is a page you
   read, not a monitor you watch, and every tick costs four calls. */
const TRACE_POLL_MS = 5000;

/* A run of silence longer than this is folded into one marker. Long enough
   that a working exchange never breaks up, short enough that "they went
   quiet" is visible as itself rather than as a scrollbar. */
const TRACE_GAP_MS = 5 * 60 * 1000;

function stopMsgPoll() {
  if (tracePollTimer) { clearInterval(tracePollTimer); tracePollTimer = null; }
  traceSession = null;
  traceLast = null;
}

function openTrace(name, mesh) {
  if (tracePollTimer) clearInterval(tracePollTimer);
  // Re-entering the same session keeps what is expanded; arriving at another
  // one starts clean, because those ids belong to a different conversation.
  if (traceSession !== name) traceOpen = new Set();
  traceSession = name;
  traceMesh = mesh || "";
  traceLast = null;
  showView("msg");
  $("msg-view").innerHTML = "<p class='wf-note'>loading…</p>";
  refreshTrace();
  tracePollTimer = setInterval(refreshTrace, TRACE_POLL_MS);
}

/* Move to another mesh's tab. Through the URL, so the tab you are reading is
   the one a link or a reload lands on. */
function traceGoMesh(mesh) {
  location.hash =
    `#/msg/${encodeURIComponent(traceSession)}/${encodeURIComponent(mesh)}`;
}

function traceFail(msg) {
  $("msg-view").innerHTML = "";
  $("msg-view").appendChild(traceHead(null));
  $("msg-view").appendChild(el("p", "wf-warning", msg));
}

async function refreshTrace() {
  if (!traceSession) return;
  const want = traceSession;
  let meta;
  try {
    const resp = await api(`/api/sessions/${encodeURIComponent(want)}/meta`);
    meta = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      traceFail(meta.error || `cannot load this session (HTTP ${resp.status})`);
      return;
    }
  } catch {
    return;
  }
  if (traceSession !== want) return;      // navigated away mid-flight

  const meshes = meta.meshes || [];
  if (!meshes.length) {
    traceLast = null;
    renderTrace({ meta, meshes, mesh: null });
    return;
  }
  // The tab: the one the URL names while it is still a membership, else the
  // first. A session leaving a mesh must not leave the page on a dead tab.
  const seat = meshes.find((m) => m.mesh === traceMesh) || meshes[0];

  const cwd = (meta.session || {}).cwd || "";
  let info, history, owed, flow;
  try {
    const calls = [
      api(`/api/mesh/${encodeURIComponent(seat.mesh)}`),
      api(`/api/mesh/${encodeURIComponent(seat.mesh)}/messages?limit=200`),
      api(`/api/mesh/${encodeURIComponent(seat.mesh)}/owed`),
    ];
    // The focus lane's workflow, and only its: a run is keyed by (directory,
    // scope) and the scope IS the session name, so this is one call. Every
    // other member's run would be one call each, per poll, to annotate a lane
    // nobody came here to read.
    if (cwd) {
      calls.push(api(
        `/api/cflow/run?cwd=${encodeURIComponent(cwd)}` +
        `&scope=${encodeURIComponent(want)}`
      ));
    }
    const [r1, r2, r3, r4] = await Promise.all(calls);
    if (!r1.ok) {
      const doc = await r1.json().catch(() => ({}));
      traceFail(doc.error || `cannot load mesh '${seat.mesh}'`);
      return;
    }
    info = await r1.json();
    history = r2.ok ? (await r2.json()).messages || [] : [];
    owed = r3.ok ? await r3.json() : null;
    flow = r4 && r4.ok ? await r4.json() : null;
  } catch {
    return;
  }
  if (traceSession !== want) return;
  traceLast = { meta, meshes, mesh: seat, info, history, owed, flow };
  renderTrace(traceLast);
}

/* ---- the event list ------------------------------------------------
   Pure, and the one piece worth testing on its own (tests/web/seq_check.js):
   everything the page shows is a drawing of what this returns. */
function traceMs(at) {
  const t = Date.parse(at || "");
  return Number.isNaN(t) ? 0 : t;
}

/* (epoch, seq) is the mesh's own order and the only authoritative one — a
   clock is a machine's opinion, and a federated mesh has several. Epoch moves
   only on an authority handover, so a forced takeover cannot interleave with
   the old authority's late traffic. Timestamps decide only where a message
   has no sequence at all (one parked by the fast path, or a log written
   before sequencing). */
function traceCmp(a, b) {
  const ae = a.epoch || 0, be = b.epoch || 0;
  if (ae !== be) return ae - be;
  const as = a.seq, bs = b.seq;
  if (as !== undefined && as !== null && bs !== undefined && bs !== null) {
    if (as !== bs) return as - bs;
  }
  return traceMs(a.ts) - traceMs(b.ts);
}

/* Which journal entries are worth a mark on the lane. The engine writes a
   great deal that is bookkeeping (locks, cursors, superseded requests); what
   belongs beside a conversation is where the run GOT to, and what it said
   about it. Anything unlisted is left out rather than drawn as a mystery. */
function traceFlowLabel(e) {
  const step = e.step || e.current || e.id || "";
  switch (e.event) {
    case "started": return `run started · ${e.workflow || ""}`.trim();
    case "step_completed": return `done: ${step}`;
    case "step_report": return `report: ${e.summary || step}`;
    case "select_presented": return `choosing: ${step}`;
    case "select_confirmed": return `chose: ${e.option || step}`;
    case "gate_wait": return `gate: ${step}`;
    case "approved": return `approved: ${step}`;
    case "ask_opened": return `asked ${(e.asked || []).join(", ") || "nobody"}: ${step}`;
    case "ask_unresolved": return `nobody to ask: ${step}`;
    case "ask_escalated": return `escalated: ${step}`;
    case "ask_abstained": return `${e.by || "a peer"} abstained: ${step}`;
    case "ask_answered": return `${e.by || "a peer"} decided ${e.decision || ""}: ${step}`.trim();
    case "ask_declined": return `${e.by || "a peer"} declined: ${step}`;
    case "ask_discarded": return `question dropped: ${step}`;
    case "loop_limit": return `loop limit: ${step}`;
    case "loop_extended": return `loop limit raised: ${step}`;
    case "state_forced": return `forced to: ${step}`;
    case "done": return "run finished";
    case "aborted": return "run aborted";
    case "archived": return "run archived";
    default: return "";
  }
}

function msgEvents(input) {
  const focus = input.handle || "";
  const gapMs = input.gapMs === undefined ? TRACE_GAP_MS : input.gapMs;
  const members = input.members || [];
  const known = new Set(members.map((m) => m.handle));

  // Who is owed what, by message. One message can be owed by several
  // recipients — a batch asks each of them separately.
  const debts = {};
  for (const r of (input.owed || {}).members || []) {
    for (const m of r.messages || []) {
      if (!m.id) continue;
      (debts[m.id] = debts[m.id] || []).push({ handle: r.handle, age: m.age });
    }
  }

  // Everything that is not a message carries a wall clock and nothing else,
  // so it is merged by time against the sequenced messages. That is an
  // approximation and the only one available: the run's journal and the
  // roster are written by other hands than the mesh's sequencer.
  const side = [];
  for (const m of members) {
    if (!m.joined_at) continue;
    side.push({
      kind: "join", at: m.joined_at, handle: m.handle,
      role: m.role, parent: m.parent, machine: m.machine,
    });
  }
  for (const e of (input.journal || [])) {
    const label = traceFlowLabel(e);
    if (label) side.push({ kind: "flow", at: e.at, handle: focus, label, entry: e });
  }
  side.sort((a, b) => traceMs(a.at) - traceMs(b.at));

  const msgs = [...(input.messages || [])].sort(traceCmp);
  const merged = [];
  let i = 0;
  for (const msg of msgs) {
    const ts = traceMs(msg.ts);
    while (i < side.length && traceMs(side[i].at) <= ts) merged.push(side[i++]);
    // A daemon older than these assets serves the files from disk but runs
    // the Python it started with, so the annotation can be missing. Then the
    // address is all there is: a handle list still names its recipients, but
    // a '*' names nobody we may invent — that resolution is the member
    // graph's, and guessing it here would draw arrows to people it never
    // reached. The row says so rather than pretending either way.
    const resolved = Array.isArray(msg.recipients);
    const to = resolved
      ? msg.recipients
      : (msg.to === "*" ? [] : Array.isArray(msg.to) ? msg.to : [msg.to]);
    merged.push({
      kind: "msg",
      at: msg.ts,
      msg,
      from: msg.from,
      to,
      resolved,
      // The focus session is a party to this if it sent it or is being sent
      // it. Everything else is the room's business, drawn faded.
      mine: msg.from === focus || to.includes(focus),
      external: !known.has(msg.from),
      delivered: msg.delivered || [],
      remote: msg.remote || [],
      debts: debts[msg.id] || [],
    });
  }
  while (i < side.length) merged.push(side[i++]);

  if (!gapMs) return merged;
  const out = [];
  let prev = null;
  for (const ev of merged) {
    const at = traceMs(ev.at);
    if (prev && at && at - prev >= gapMs) {
      out.push({ kind: "gap", ms: at - prev });
    }
    if (at) prev = at;
    out.push(ev);
  }
  return out;
}

/* The columns, left to right. Outsiders first — the operator is not a member
   and speaks from beside the mesh, not inside it — then members in the order
   the trace first mentions them, which for a room that grew by spawning is
   the order it grew in. */
function msgLanes(events, focus) {
  const lanes = [];
  const seen = new Map();
  const add = (key, extra) => {
    if (!key || seen.has(key)) return seen.get(key);
    const lane = { key, label: key, self: key === focus, ...extra };
    seen.set(key, lane);
    lanes.push(lane);
    return lane;
  };
  for (const ev of events) {
    if (ev.kind === "msg" && ev.external) add(ev.from, { outside: true });
  }
  for (const ev of events) {
    if (ev.kind === "join") add(ev.handle, { role: ev.role, machine: ev.machine });
    else if (ev.kind === "msg") {
      add(ev.from, ev.external ? { outside: true } : {});
      for (const h of ev.to) add(h, {});
    }
  }
  add(focus, {});   // silent, but it is what the page is about
  return lanes;
}

/* ---- geometry ----------------------------------------------------- */
const SEQ = {
  gutter: 74,   // the clock down the left, inside the same SVG as the lanes
  lane: 132,    // lane pitch — the minimum; widened to fit the page (seqFit)
  right: 124,   // room past the last lane for an "unanswered" chip
  row: 34,      // a collapsed row
  head: 52,     // the sticky lane heads
  line: 15,     // one wrapped line of an expanded body
};
const SEQ_LANE_MIN = 132;   // a handle and a role, side by side
const SEQ_LANE_MAX = 240;   // past this the arrows are more travel than picture

/* Spread the lanes across the page when there are few of them: the label a
   message gets to show is the width between its endpoints, and three agents
   on a wide screen would otherwise be read through a keyhole with the rest of
   the row left blank. Narrower than the minimum is never worth it — that way
   the diagram scrolls sideways instead of becoming unreadable. */
function seqFit(count, available) {
  if (!available || count < 1) return SEQ_LANE_MIN;
  const room = Math.floor((available - SEQ.gutter - SEQ.right) / count);
  return Math.max(SEQ_LANE_MIN, Math.min(SEQ_LANE_MAX, room));
}

const seqX = (i) => SEQ.gutter + SEQ.lane / 2 + i * SEQ.lane;
const seqW = (n) => SEQ.gutter + SEQ.lane * Math.max(n, 1) + SEQ.right;

function seqSvg(width, height, cls) {
  return svg("svg", {
    width, height, viewBox: `0 0 ${width} ${height}`, class: cls,
  });
}

/* The lane lines, drawn per row rather than once behind everything: a row
   knows its own height, so nothing has to be measured, and a body folding
   open cannot leave the lanes short. */
function seqLanes(node, lanes, height, top = 0) {
  lanes.forEach((lane, i) => {
    node.appendChild(svg("line", {
      x1: seqX(i), y1: top, x2: seqX(i), y2: height,
      class: "seq-lane" + (lane.self ? " self" : "") + (lane.outside ? " outside" : ""),
    }));
  });
}

function seqClock(at) {
  const t = String(at || "");
  const time = t.includes("T") ? t.split("T")[1] : t;
  return (time || "").replace("Z", "").split(".")[0].split("+")[0];
}

/* Fit a label to the width it has. Characters, not pixels: the stylesheet
   owns the font, and this is the same approximation the topology's cluster
   names use. */
function seqClip(text, width) {
  const room = Math.max(8, Math.floor(width / 6.3));
  const flat = String(text || "").replace(/\s+/g, " ").trim();
  return flat.length > room ? `${flat.slice(0, room - 1)}…` : flat;
}

function seqWrap(text, width) {
  const room = Math.max(20, Math.floor(width / 6.3));
  const out = [];
  for (const para of String(text || "").split("\n")) {
    let line = "";
    for (let word of para.split(/\s+/)) {
      // A path, a URL or a hash has nowhere to break and is exactly what gets
      // pasted into these messages — cut it rather than let it run off the
      // side of a row whose width is the lanes', not the text's.
      while (word.length > room) {
        if (line) { out.push(line); line = ""; }
        out.push(word.slice(0, room));
        word = word.slice(room);
        if (out.length >= 24) return [...out, "…"];
      }
      if (!line) line = word;
      else if ((line + " " + word).length <= room) line += " " + word;
      else { out.push(line); line = word; }
      if (out.length >= 24) return [...out, "…"];   // a long report is not the page
    }
    out.push(line);
  }
  return out;
}

/* ---- the rows ----------------------------------------------------- */
/* The lane heads, and the only part of the drawing that stays put: pinned to
   the top of the scroller, because a name you have scrolled past is a lane
   you can no longer read. */
function seqHeadSvg(lanes) {
  const width = seqW(lanes.length);
  const node = seqSvg(width, SEQ.head, "seq-head-svg");
  lanes.forEach((lane, i) => {
    const g = svg("g", {
      class: "seq-lane-head"
        + (lane.self ? " self" : "") + (lane.outside ? " outside" : ""),
    });
    g.appendChild(svg(
      "text", { x: seqX(i), y: 20, class: "seq-lane-name" },
      seqClip(lane.key, SEQ.lane - 10)
    ));
    const under = lane.outside
      ? "not a member"
      : (lane.machine ? `${lane.role || "member"} · ${lane.machine}` : (lane.role || ""));
    if (under) {
      g.appendChild(svg(
        "text", { x: seqX(i), y: 34, class: "seq-lane-role" },
        seqClip(under, SEQ.lane - 10)
      ));
    }
    g.appendChild(svg("title", {}, lane.outside
      ? `${lane.key} — speaking from outside the mesh`
      : `${lane.key}${lane.role ? ` (${lane.role})` : ""}` +
        (lane.machine ? ` on ${lane.machine}` : "")));
    node.appendChild(g);
  });
  // The lanes start under the names rather than through them: this block is
  // where each line comes FROM.
  seqLanes(node, lanes, SEQ.head, 40);
  return node;
}

/* An arrowhead at (x, y) pointing along `dir` (+1 right, -1 left). Hollow
   when the message has left but has not been typed in anywhere yet — the
   difference between "they have not answered" and "they have not been asked
   yet", which is the whole diagnosis. */
function seqArrowHead(x, y, dir, open) {
  return svg("path", {
    d: `M ${x} ${y} L ${x - dir * 8} ${y - 4.5} L ${x - dir * 8} ${y + 4.5} Z`,
    class: "seq-arrowhead" + (open ? " open" : ""),
  });
}

function seqMsgRow(ev, lanes, width) {
  const m = ev.msg;
  const body = m.body || "";
  const open = traceOpen.has(m.id);
  const lines = open ? seqWrap(body, width - SEQ.gutter - 40) : [];
  const height = SEQ.row + (lines.length ? lines.length * SEQ.line + 6 : 0);
  const cy = 20;
  const node = seqSvg(width, height, "seq-row-svg");
  seqLanes(node, lanes, height);

  const at = lanes.findIndex((l) => l.key === ev.from);
  const to = ev.to.map((h) => lanes.findIndex((l) => l.key === h)).filter((i) => i >= 0);
  const g = svg("g", {
    class: "seq-msg" + (ev.mine ? "" : " faint")
      + (traceExpandable(m) ? " openable" : ""),
  });

  if (at < 0 || !to.length) {
    // Two different silences, and they must not be drawn as one. Either the
    // daemon resolved this and the answer was nobody — an edge cut since it
    // was sent — or it is too old to have been asked, and we know nothing.
    const stale = !ev.resolved;
    g.appendChild(svg(
      "text", { x: seqX(Math.max(at, 0)), y: cy + 4, class: "seq-nowhere" },
      stale ? "→ recipients not reported" : "⊘ reaches nobody"
    ));
    g.appendChild(svg("title", {}, stale
      ? `${m.from} → ${fmtTo(m.to)}: this daemon does not say who a message ` +
        "reached — 'claunch daemon restart' to pick up this version"
      : `${m.from} → ${fmtTo(m.to)}: nobody this message is addressed to is ` +
        "still connected to the sender"));
  } else {
    let far = to[0];
    for (const i of to) if (Math.abs(i - at) > Math.abs(far - at)) far = i;
    const dir = far >= at ? 1 : -1;
    const x0 = seqX(at), x1 = seqX(far);
    const localTo = ev.to.filter((h) => !ev.remote.includes(h));
    const waiting = localTo.some((h) => !ev.delivered.includes(h));
    g.appendChild(svg("line", {
      x1: x0, y1: cy, x2: x1 - dir * 7, y2: cy,
      class: "seq-arrow" + (waiting ? " waiting" : ""),
    }));
    g.appendChild(seqArrowHead(x1, cy, dir, waiting));
    // One mark per recipient, so a broadcast says who it actually reached.
    for (const i of to) {
      const handle = lanes[i].key;
      const remote = ev.remote.includes(handle);
      const got = ev.delivered.includes(handle);
      const mark = svg("circle", {
        cx: seqX(i), cy, r: 3.4,
        class: "seq-drop" + (remote ? " remote" : got ? " in" : " out"),
      });
      mark.appendChild(svg("title", {}, remote
        ? `${handle} is on another daemon — whether it has been typed in is ` +
          "that daemon's to know"
        : got
          ? `typed into ${handle}'s terminal`
          : `queued for ${handle} — not typed in yet`));
      g.appendChild(mark);
    }
    const span = Math.abs(x1 - x0);
    const label = (m.type && m.type !== "say" ? `${m.type} · ` : "")
      + (m.reply_to ? "re · " : "") + body;
    g.appendChild(svg(
      "text",
      { x: (x0 + x1) / 2, y: cy - 8, class: "seq-label" },
      seqClip(label, Math.max(span, SEQ.lane) - 8)
    ));
  }

  g.appendChild(svg("title", {}, [
    `${m.from} → ${fmtTo(m.to)}`,
    m.type && m.type !== "say" ? `(${m.type})` : "",
    m.reply_to ? `in reply to ${m.reply_to}` : "",
    "", body,
  ].filter((s) => s !== "").join("\n")));
  if (traceExpandable(m)) {
    g.addEventListener("click", () => {
      if (traceOpen.has(m.id)) traceOpen.delete(m.id);
      else traceOpen.add(m.id);
      if (traceLast) renderTrace(traceLast);
    });
  }
  node.appendChild(g);

  // Unanswered, in the right-hand margin: always the same column, so a
  // reader's eye finds the silences without following each arrow to its end.
  if (ev.debts.length) {
    const oldest = ev.debts.reduce(
      (a, d) => (d.age !== null && d.age !== undefined && d.age > a ? d.age : a), 0
    );
    const chip = svg("g", { class: "seq-owed" });
    chip.appendChild(svg(
      "text", { x: width - SEQ.right + 10, y: cy + 4 },
      `⚠ ${fmtAge(oldest)} unanswered`
    ));
    chip.appendChild(svg("title", {}, ev.debts
      .map((d) => `${d.handle} has not answered this (${fmtAge(d.age)} ago)`)
      .join("\n") + "\n\nnudge or dismiss it in Unanswered, above"));
    node.appendChild(chip);
  }

  lines.forEach((line, i) => {
    node.appendChild(svg(
      "text",
      { x: SEQ.gutter + 8, y: SEQ.row + i * SEQ.line, class: "seq-body" },
      line
    ));
  });
  node.appendChild(svg(
    "text", { x: 8, y: cy + 4, class: "seq-time" }, seqClock(ev.at)
  ));
  return node;
}

/* Worth a fold: a body the one-line label cannot hold. */
function traceExpandable(m) {
  const body = m.body || "";
  return body.length > 40 || body.includes("\n");
}

function fmtTo(to) {
  if (to === "*") return "everyone";
  return Array.isArray(to) ? to.join(", ") : String(to || "");
}

function seqFlowRow(ev, lanes, width) {
  const node = seqSvg(width, SEQ.row, "seq-row-svg");
  seqLanes(node, lanes, SEQ.row);
  const i = lanes.findIndex((l) => l.key === ev.handle);
  const cy = 18;
  if (i >= 0) {
    const g = svg("g", { class: "seq-flow" });
    const x = seqX(i);
    g.appendChild(svg("path", {
      d: `M ${x} ${cy - 5} L ${x + 5} ${cy} L ${x} ${cy + 5} L ${x - 5} ${cy} Z`,
      class: "seq-flow-mark",
    }));
    // Free to run into the right-hand margin: that column is the unanswered
    // chips', and a message row is the only kind that has one.
    g.appendChild(svg(
      "text", { x: x + 11, y: cy + 4, class: "seq-flow-label" },
      seqClip(ev.label, width - x - 24)
    ));
    const e = ev.entry || {};
    g.appendChild(svg("title", {}, [
      ev.label, e.details || "", e.by ? `by ${e.by}` : "",
    ].filter(Boolean).join("\n\n")));
    node.appendChild(g);
  }
  node.appendChild(svg(
    "text", { x: 8, y: cy + 4, class: "seq-time" }, seqClock(ev.at)
  ));
  return node;
}

function seqJoinRow(ev, lanes, width) {
  const node = seqSvg(width, SEQ.row, "seq-row-svg");
  seqLanes(node, lanes, SEQ.row);
  const i = lanes.findIndex((l) => l.key === ev.handle);
  const cy = 18;
  if (i >= 0) {
    const g = svg("g", { class: "seq-join" });
    const x = seqX(i);
    g.appendChild(svg("line", { x1: x - 11, y1: cy, x2: x + 11, y2: cy }));
    g.appendChild(svg(
      "text", { x: x + 16, y: cy + 4, class: "seq-join-label" },
      seqClip(
        `joined${ev.role ? ` as ${ev.role}` : ""}` +
        (ev.parent ? ` · spawned by ${ev.parent}` : ""),
        width - x - 24
      )
    ));
    g.appendChild(svg("title", {}, `${ev.handle} joined this mesh` +
      (ev.role ? ` as ${ev.role}` : "") +
      (ev.parent ? `, spawned by ${ev.parent}` : "") +
      (ev.machine ? `, on ${ev.machine}` : "")));
    node.appendChild(g);
  }
  node.appendChild(svg(
    "text", { x: 8, y: cy + 4, class: "seq-time" }, seqClock(ev.at)
  ));
  return node;
}

function seqGapRow(ev, lanes, width) {
  const h = 26;
  const node = seqSvg(width, h, "seq-row-svg");
  seqLanes(node, lanes, h);
  const g = svg("g", { class: "seq-gap" });
  g.appendChild(svg("line", {
    x1: SEQ.gutter, y1: h / 2, x2: width - SEQ.right, y2: h / 2,
  }));
  g.appendChild(svg(
    "text", { x: (SEQ.gutter + width - SEQ.right) / 2, y: h / 2 + 4 },
    `⋯ ${fmtAge(ev.ms / 1000)} quiet ⋯`
  ));
  node.appendChild(g);
  return node;
}

function seqRow(ev, lanes, width) {
  if (ev.kind === "msg") return seqMsgRow(ev, lanes, width);
  if (ev.kind === "flow") return seqFlowRow(ev, lanes, width);
  if (ev.kind === "join") return seqJoinRow(ev, lanes, width);
  return seqGapRow(ev, lanes, width);
}

/* ---- the page ----------------------------------------------------- */
function traceHead(data) {
  const head = el("div", "wf-head");
  head.appendChild(el("h2", null, `messages: ${traceSession}`));
  const seat = data && data.mesh;
  if (seat) {
    const who = el("span", "badge", `${seat.handle} in ${seat.mesh}`);
    who.title =
      "the name this session answers to in this mesh — it is a different " +
      "handle in each one";
    head.appendChild(who);
  }
  const term = el("button", "wf-btn option", "terminal");
  term.title = "watch this session work";
  term.addEventListener("click", () => go("#/s/" + encodeURIComponent(traceSession)));
  head.appendChild(term);
  if (seat) {
    const room = el("a", "wf-btn option", "mesh view");
    room.href = "#/mesh/" + encodeURIComponent(seat.mesh);
    room.title = "the same room as a roster and a topology";
    head.appendChild(room);
  }
  return head;
}

function traceTabs(data) {
  const bar = el("div", "seq-tabs");
  for (const m of data.meshes) {
    const on = data.mesh && m.mesh === data.mesh.mesh;
    const tab = el("button", "seq-tab" + (on ? " on" : ""), `${m.mesh} · ${m.handle}`);
    tab.title = `${m.members} member(s) — this session is '${m.handle}' here`;
    if (!on) tab.addEventListener("click", () => traceGoMesh(m.mesh));
    bar.appendChild(tab);
  }
  return bar;
}

function traceLegend() {
  const box = el("div", "seq-legend");
  for (const [cls, label] of [
    ["mine", "this session is a party to it"],
    ["faint", "between others, for context"],
    ["waiting", "sent, not typed in yet"],
    ["owed", "asked, never answered"],
    ["flow", "its workflow moved"],
  ]) {
    const item = el("span", "mesh-legend-item");
    item.appendChild(el("i", `seq-legend-swatch ${cls}`));
    item.appendChild(el("span", null, label));
    box.appendChild(item);
  }
  return box;
}

function renderTrace(data) {
  const view = $("msg-view");
  const old = view.querySelector(".seq-scroll");
  const keep = old && {
    top: old.scrollTop,
    left: old.scrollLeft,
    // Following the story down as it happens is the one reason to move the
    // scroll under the reader; anywhere else, a poll must leave it alone.
    end: old.scrollHeight - old.scrollTop - old.clientHeight < 8,
  };
  view.innerHTML = "";
  view.appendChild(traceHead(data));

  if (!data.meshes.length) {
    view.appendChild(el(
      "p", "wf-note",
      "messages travel through a mesh and this session is in none — there is " +
      "nothing to trace. Join it to one from its details panel."
    ));
    return;
  }
  view.appendChild(traceTabs(data));

  const focus = data.mesh.handle;
  const events = msgEvents({
    handle: focus,
    members: (data.info || {}).members || [],
    messages: data.history || [],
    owed: data.owed,
    journal: (data.flow || {}).journal || [],
  });
  const lanes = msgLanes(events, focus);
  // Settled once per draw, before anything is measured against it: every row
  // is laid out from SEQ.lane, so they all have to agree on it.
  // clientWidth carries this page's own padding; the diagram gets what is
  // left of it.
  SEQ.lane = seqFit(lanes.length, Math.max(0, (view.clientWidth || 0) - 56));
  const width = seqW(lanes.length);

  view.appendChild(traceLegend());
  view.appendChild(el(
    "p", "wf-desc",
    "the last " + (data.history || []).length + " message(s) in this mesh, in " +
    "the order the mesh sequenced them. Only what travelled THROUGH the mesh " +
    "is here — words typed straight into a terminal leave no record. Click a " +
    "message to read all of it."
  ));

  // Above the scroller, not inside it: the diagram scrolls sideways, and a
  // block of prose and buttons dragged along by that is unreadable on a
  // narrow screen — its buttons end up past the right-hand edge. It keeps its
  // own height instead, and its own scrollbar when the list is long.
  // Only when there is a debt. On the mesh page an empty ledger saying so is
  // worth its line; here it would take a third of the screen off the picture
  // it annotates, to report the ordinary case — which the picture already
  // reports, by carrying no chips.
  if (data.owed && (data.owed.owed || 0) > 0) {
    const strip = el("div", "seq-owed-strip");
    strip.appendChild(renderMeshOwed(data.info, data.owed));
    view.appendChild(strip);
  }

  const scroll = el("div", "seq-scroll");
  const headBox = el("div", "seq-head");
  headBox.appendChild(seqHeadSvg(lanes));
  scroll.appendChild(headBox);

  const rows = el("div", "seq-rows");
  if (!events.length) {
    rows.appendChild(el("p", "wf-note", "nothing has been said in this mesh yet"));
  }
  for (const ev of events) rows.appendChild(seqRow(ev, lanes, width));
  scroll.appendChild(rows);
  view.appendChild(scroll);

  if (keep) {
    scroll.scrollLeft = keep.left;
    scroll.scrollTop = keep.end ? scroll.scrollHeight : keep.top;
  } else {
    scroll.scrollTop = scroll.scrollHeight;   // the latest, like a chat
  }
}

/* ------------------------------------------------------------------ */
/* boot                                                               */
/* ------------------------------------------------------------------ */
/* The poll is installed here rather than at the end of boot(), and is never
   taken down: a page opened while the daemon was down and a page whose daemon
   went down under it are the same predicament, and in both something has to
   keep asking. It used to be boot()'s last act, so the first predicament left
   a permanently dead page — the one thing a user cannot tell apart from a
   broken app.

   Each tick leads with /api/health, which needs no cookie. That keeps "the
   daemon is gone" and "the daemon is back and my login died with the old one"
   separable, and it means a restart is *noticed* — by its boot id — rather
   than inferred from things going quiet. */
let pollTimer = null;
let booted = false;        // boot() has seeded the page and routed once
let daemonOnline = true;   // last verdict; only the transitions do any work
let daemonBoot = null;     // which daemon that verdict was about

function authOpen() {
  return !$("auth-overlay").classList.contains("hidden");
}

function setDaemonOnline(up) {
  if (daemonOnline === up) return;
  daemonOnline = up;
  const info = $("daemon-info");
  info.classList.toggle("off", !up);
  if (!up) {
    info.textContent = "daemon offline";
    // The lists stay on screen, so they have to be labelled: a rail full of
    // sessions is otherwise indistinguishable from a rail full of *current*
    // sessions, and this one is a photograph.
    info.title = "nothing is answering — the lists below are the last thing it said";
  }
  // Coming back is boot()'s job: it re-reads the version and the relay state.
}

async function boot() {
  let info;
  try {
    const resp = await api("/api/daemon");
    info = await resp.json();
  } catch {
    return;   // down, or the auth overlay is up — the poll comes back to this
  }
  const badge = $("daemon-info");
  badge.textContent = `v${info.version}`;
  badge.title = "";
  badge.classList.remove("off");
  if (info.boot_id) daemonBoot = info.boot_id;
  renderRelayBadge(info.relay);
  refreshProfiles();
  refreshHarnesses();
  refreshRoles();
  refreshWorkspaces();
  refreshSessions();
  refreshMeshList();
  refreshCflow();
  // Last, and once. A #/s/<name> link attaches here — which is why a reload
  // puts you back in the session instead of at an empty slot — and it renders
  // against caches the refreshes above have already filled. Re-running it on
  // every recovery would re-enter whatever page the user has since walked to.
  if (!booted) { booted = true; route(); }
}

let polling = false;

async function pollTick() {
  // While the token prompt is up there is nobody to poll for, and every
  // request would only raise it again under the fingers typing into it.
  if (authOpen()) return;
  // A tick that is still waiting on a dead host must not have another stacked
  // on top of it every two seconds: connect attempts to a machine that has
  // gone away hang for a good while, and that is exactly when this runs.
  if (polling) return;
  polling = true;
  try { await pollOnce(); } finally { polling = false; }
}

async function pollOnce() {
  const health = await daemonHealth();
  if (!health) { setDaemonOnline(false); return; }
  // A boot id we have not seen means the daemon we were talking to is gone:
  // new cookies, new pids, and every socket we hold bound to nothing.
  const restarted = !!(health.boot_id && daemonBoot && health.boot_id !== daemonBoot);
  const returned = !daemonOnline || !booted;
  setDaemonOnline(true);
  if (restarted || returned) {
    daemonBoot = health.boot_id || null;
    await boot();     // re-read everything this daemon publishes, from scratch
    reconnectNow();   // and give the attached terminal its socket back
    return;
  }
  if (health.boot_id) daemonBoot = health.boot_id;
  refreshSessions();
  refreshMeshList();
  refreshCflow();
  // Polled because the registry is edited from the CLI, in another window;
  // it redraws only when the list really changed (see refreshWorkspaces).
  refreshWorkspaces();
}

pollTimer = setInterval(pollTick, 2000);
boot();
