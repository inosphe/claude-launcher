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
let altScreen = false;      // the program is drawing the alternate screen
let mouseTracking = false;  // the program asked for the mouse — the wheel is its own
let scrollOffset = 0;       // daemon history offset this viewer is reading (0 = live)
let wheelAccum = 0;         // unflushed wheel delta, accumulated in lines
let wheelTimer = null;      // debounce timer for wheel -> scroll control
// Rough pixels per terminal line, for converting pixel-mode wheel deltas;
// a scalar, coarse enough that the daemon's clamped offset absorbs the error.
const WHEEL_LINE_PX = 20;

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

/* The rail's bulk bar. Three verbs that act on the working fleet, each
   labelled with the number it would touch and hidden when that number is
   zero — so the bar is a reading of the rail rather than a fixed row of
   controls, half of which would do nothing on any given rail.

   Stop ends programs, resume brings exited records back, and archive moves
   exited records out of the working fleet while keeping them inspectable and
   resumable. Permanent removal remains on the explicit API and CLI paths. */
function syncBulkActions(sessions, filter = "current") {
  const live = filter === "current" || filter === "running"
    ? sessions.filter((s) => s.status !== "exited").length : 0;
  const dead = filter === "current" || filter === "killed"
    ? sessions.filter((s) => s.status === "exited" && !s.archived_at).length : 0;
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
      "relaunch every unarchived exited session under its own name and conversation");
  set("archive-exited", dead, `archive ${dead} exited`,
      "move exited sessions into the archive while retaining their records, "
      + "conversations and resume capability");
  // The bar's own border would otherwise sit above the nav as a stray rule on
  // a rail with nothing on it.
  const bar = $("bulk-actions");
  if (bar) bar.classList.toggle("hidden", live + dead === 0);
}

/* The stand-in for confirm()/alert() on the flows that end a session or its
   record. Not restyling for its own sake: a native dialog can only answer
   yes or no, and the question these flows actually end on — a record a mesh
   row still names, force it off the roster or keep it — needs a third
   button. Resolves to the pressed action's `value`; Escape, the backdrop
   and Cancel are all null, so every caller's "did not answer" is one shape.

   `choices` adds a radio group under the body for the questions that are not
   yes/no either: what to do with the sessions under the one being removed is
   two different actions, and putting each on its own button would leave the
   safe answer and the destructive one side by side, one click apart. The
   picked option's `value` is merged into the pressed action's value, so the
   caller reads one object. */
function showModal({ title, body, actions, checkbox = null, choices = null }) {
  return new Promise((resolve) => {
    const overlay = $("modal-overlay");
    $("modal-title").textContent = title;
    const bodyEl = $("modal-body");
    bodyEl.textContent = body;
    const row = $("modal-actions");
    row.innerHTML = "";
    const done = (value) => {
      overlay.classList.add("hidden");
      document.removeEventListener("keydown", onKey);
      resolve(value);
    };
    const onKey = (e) => { if (e.key === "Escape") done(null); };
    let check = null;
    if (checkbox) {
      const label = document.createElement("label");
      label.classList.add("modal-check");
      check = document.createElement("input");
      check.type = "checkbox";
      label.append(check, document.createTextNode(checkbox.label));
      bodyEl.appendChild(label);
    }
    // The first option is the one selected on open, so it is the safe answer
    // in every group: a modal dismissed with Enter must not take the
    // destructive branch of a question the operator never read.
    let picked = choices ? choices.options[0] : null;
    if (choices) {
      const group = document.createElement("div");
      group.classList.add("modal-choices");
      for (const opt of choices.options) {
        const label = document.createElement("label");
        label.classList.add("modal-choice");
        const radio = document.createElement("input");
        radio.type = "radio";
        radio.name = "modal-choice";
        radio.checked = opt === picked;
        radio.addEventListener("change", () => {
          picked = opt;
          for (const { action, button } of buttons) {
            if (action.dangerWhen) {
              button.classList.toggle("danger", !!opt[action.dangerWhen]);
            }
          }
        });
        const text = document.createElement("span");
        text.textContent = opt.label;
        if (opt.hint) {
          const hint = document.createElement("small");
          hint.textContent = opt.hint;
          text.appendChild(hint);
        }
        label.append(radio, text);
        group.appendChild(label);
      }
      bodyEl.appendChild(group);
    }
    const buttons = [];
    for (const a of actions) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = a.label;
      if (a.danger) btn.classList.add("danger");
      if (a.requiresCheck) btn.disabled = !check || !check.checked;
      if (a.dangerWhen && picked && picked[a.dangerWhen]) btn.classList.add("danger");
      btn.addEventListener("click", () => done(
        a.value && picked ? { ...a.value, ...picked.value } : a.value
      ));
      row.appendChild(btn);
      buttons.push({ action: a, button: btn });
    }
    if (check) check.addEventListener("change", () => {
      for (const { action, button } of buttons) {
        if (action.requiresCheck) button.disabled = !check.checked;
      }
    });
    // Assigned, not addEventListener'd: each asking replaces the last one's
    // backdrop handler instead of stacking a resolved promise's behind it.
    overlay.onclick = (e) => { if (e.target === overlay) done(null); };
    document.addEventListener("keydown", onKey);
    overlay.classList.remove("hidden");
    // Focus the first (safe) answer: a stray Enter must not confirm a
    // delete the way it would with the destructive button focused.
    const first = row.querySelector("button");
    if (first) first.focus();
  });
}

const modalInfo = (title, body) =>
  showModal({ title, body, actions: [{ label: "OK", value: true }] });

const modalConfirm = (title, body, label, danger = true) =>
  showModal({
    title, body,
    actions: [
      { label: "Cancel", value: null },
      { label, value: true, danger },
    ],
  });

/* A bulk call answers with what it did *and* with what it did not: a session
   that would not stop, one that would not come back. That omission is why
   the rail a second later does not match the count on the button, and left
   unmentioned it reads as the button not having worked. */
async function reportBulk(result, verb) {
  const failed = (result && result.failed) || [];
  if (!failed.length) return;
  await modalInfo(
    `${failed.length} session(s) could not ${verb}`,
    failed.map((f) => `${f.name} — ${f.error}`).join("\n")
  );
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
      await modalInfo(`Could not ${verb}`,
                      (result && result.error) || `HTTP ${resp.status}`);
      return null;
    }
    await reportBulk(result, verb);
    return result;
  } catch {
    return null;  // the auth overlay is up; api() has already raised it
  } finally {
    btn.disabled = false;
  }
}

/* Everything this page remembers about a session, dropped when the session
   stops existing.

   The page keys several things by session NAME — the parked terminal, the
   briefing card's text, whether that card is folded open — and a name is not
   a durable identity here. Sessions are spawned and killed all day, so a tab
   left open since morning has watched hundreds of names appear and go away
   for good; a store that is only ever added to is then a per-session-EVER
   cache wearing a per-session one's clothes. It grows for as long as the tab
   is open (which is the leak), and when a respawn reuses a name it answers
   for the wrong session (which is worse — a briefing card describing work
   the session on screen never did).

   So the sweep is one function rather than a line next to each store: the
   next thing keyed by name belongs HERE, and the rule it has to obey is
   visible from where it would be written. Called once per /api/sessions
   poll, off the list that poll just returned — the daemon's own answer to
   "which sessions exist", which is the only authority on the question. */
function forgetDeadSessions() {
  const alive = new Set(sessionsCache.map((s) => s.name));
  // A parked terminal must not be re-shown, frozen, next time its old name
  // is clicked — and its socket and xterm object die with it (dropKept).
  for (const parked of [...keptTerms.keys()]) {
    if (!alive.has(parked)) dropKept(parked);
  }
  // The briefing is a summary of a conversation that has ended. Keeping the
  // text costs memory for nothing; showing it again under a reused name is
  // a lie the reader has no way to spot.
  for (const name of [...briefingCache.keys()]) {
    if (!alive.has(name)) briefingCache.delete(name);
  }
  for (const name of [...briefingOpen]) {
    if (!alive.has(name)) briefingOpen.delete(name);
  }
}

/* The rail holds still while a pointer is down on it.

   Every poll rebuilds the whole list from scratch (`list.innerHTML` below,
   driven by setInterval(pollTick, 2000)). A press is not an instant, though:
   pointerdown, then pointerup, and only then the click the handler is
   waiting for. A rebuild landing between the first two takes the node the
   press started on out of the document, and the browser is then left with no
   common ancestor to dispatch the click to -- so the press produces nothing
   at all, with no sign it was ever taken. It reads as "the button does
   nothing", intermittently, and it lands hardest on the row's small glyphs:
   the briefing toggle and its refresh, the details button, the spawn +.

   Only the teardown waits. Everything else the poll does still happens on
   time -- the caches are refreshed, and the cflow badges and briefing cards
   are applied in place onto the rows that already exist (both are written
   idempotently for exactly that reason). The redraw runs the moment the
   press ends.

   The hold carries its own deadline instead of trusting a pointerup to
   arrive. One can be lost -- the pointer leaves the window, another element
   captures it, the tab is hidden mid-press -- and a rail frozen for the rest
   of the session would be a far worse bug than the one this fixes. */
const RAIL_HOLD_MS = 1200;
let railHeldUntil = 0;
let railRedrawPending = false;

function railHeld() { return Date.now() < railHeldUntil; }

function holdRail() { railHeldUntil = Date.now() + RAIL_HOLD_MS; }

/* Released on pointerup, but the redraw it owes is deferred by a task: the
   click is dispatched after this handler returns, and a redraw run first
   would remove the very node that click is still on its way to. */
function releaseRail() {
  if (!railHeldUntil) return;
  railHeldUntil = 0;
  if (railRedrawPending) setTimeout(refreshSessions, 0);
}

/* What a rail row's meta line says: the identity, then the state.

   The line used to draw either — an exited row said "exit 1" and kept the
   profile that had run there to itself, and a borrowed token was never
   named on the rail at all. State is a sentence about the same session, so
   it joins the identity instead of replacing it, in the order the rest of
   the UI reads one (status · identity, the pair the header uses at
   app.js:6022): first who it is, then what became of it. The stylesheet
   caps the line and the full text rides the element's title. */
function railMetaText(s) {
  const identity = s.borrow
    ? `${profileHarnessLabel(s.profile, s.harness)} → ${s.borrow}`
    : profileHarnessLabel(s.profile, s.harness);
  const state = s.archived_at ? "archived" : s.status === "exited"
    ? `exit ${s.exit_code ?? "?"}`
    : s.winddown ? "winding down" : "";
  return [identity, state].filter(Boolean).join(" · ");
}

/* One mutually exclusive state filter for the rail. "Current" is the normal
   working set (running plus killed, excluding archived records); the other
   three modes answer the state-specific questions directly. The browser
   remembers the choice so repeated monitoring does not require resetting it
   after every two-second poll. */
const SESSION_FILTER_KEY = `claunch_session_filter:${BASE}`;
const SESSION_FILTERS = ["current", "running", "killed", "archived"];
let sessionFilter = localStorage.getItem(SESSION_FILTER_KEY) || "current";
if (!SESSION_FILTERS.includes(sessionFilter)) sessionFilter = "current";

function sessionCategory(s) {
  if (s && s.archived_at) return "archived";
  return s && s.status === "exited" ? "killed" : "running";
}

function sessionMatchesFilter(s, filter = sessionFilter) {
  const category = sessionCategory(s);
  return filter === "current" ? category !== "archived" : category === filter;
}

function sessionFilterCounts(sessions) {
  const counts = { current: 0, running: 0, killed: 0, archived: 0 };
  for (const session of sessions || []) {
    const category = sessionCategory(session);
    counts[category]++;
    if (category !== "archived") counts.current++;
  }
  return counts;
}

function setSessionFilter(filter, remember = true) {
  if (!SESSION_FILTERS.includes(filter)) return;
  sessionFilter = filter;
  if (remember) localStorage.setItem(SESSION_FILTER_KEY, filter);
  syncSessionFilters(sessionsCache);
}

function syncSessionFilters(sessions) {
  const list = $("session-list");
  if (!list) return;
  const counts = sessionFilterCounts(sessions);
  const labels = {
    current: "Current", running: "Running", killed: "Killed", archived: "Archived",
  };
  for (const filter of SESSION_FILTERS) {
    const button = $(`session-filter-${filter}`);
    if (!button) continue;
    button.textContent = "";
    button.append(
      document.createTextNode(labels[filter]),
      Object.assign(document.createElement("span"), {
        className: "session-filter-count", textContent: String(counts[filter]),
      })
    );
    button.setAttribute("aria-pressed", filter === sessionFilter ? "true" : "false");
  }
  for (const row of list.querySelectorAll("li[data-name]")) {
    const session = (sessions || []).find((s) => s.name === row.dataset.name);
    row.classList.toggle("session-filtered", !session || !sessionMatchesFilter(session));
  }
  if (typeof syncBulkActions === "function") syncBulkActions(sessions || [], sessionFilter);
}

async function refreshSessions() {
  let data;
  try {
    const resp = await api("/api/sessions");
    // An error response carries a JSON body of its own, so `resp.json()`
    // succeeds and `data.sessions` is simply absent -- which used to read as
    // "this daemon has no sessions" and empty the rail, drop every parked
    // terminal and forget every briefing (forgetDeadSessions works off this
    // very list). A failed poll must leave the page as it was.
    if (!resp.ok) return;
    data = await resp.json();
  } catch {
    return;
  }
  sessionsCache = data.sessions || [];
  briefingLLM = data.llm_configured !== false;
  forgetDeadSessions();
  const list = $("session-list");
  // See the hold above: a press in flight keeps the rows it started on, and
  // the redraw it postpones is owed back the moment the press ends.
  const rebuild = !railHeld();
  railRedrawPending = !rebuild;
  if (rebuild) list.innerHTML = "";
  for (const [s, depth] of rebuild ? byLineage(sessionsCache) : []) {
    const li = document.createElement("li");
    li.dataset.name = s.name;
    if (s.name === currentName) li.classList.add("active");
    // The indent goes on the row, not on a spacer element, so the whole row
    // stays one click target and the hover/active background still spans it.
    // The step is deliberately small: on a 260px rail every pixel of indent
    // is taken from the name and its tags, and depth is already spelt out by
    // the └ tick — the indent only has to make the nesting scannable, not
    // measure it. It stops growing past four levels for the same reason; a
    // deep child that indented itself off the rail would be unreadable in
    // exchange for a fact the tick and the tooltip already carry.
    if (depth) {
      li.style.paddingLeft = `${12 + Math.min(depth, 4) * 10}px`;
      li.classList.add("child");
      li.title = `spawned by ${s.parent}`;
    }
    const dot = document.createElement("span");
    dot.className = `dot ${s.status}`;
    const label = document.createElement("span");
    label.className = "rail-name";
    label.textContent = s.name;
    // A name too long for the rail is cut with an ellipsis rather than
    // wrapping the row; this is where the rest of it went.
    label.title = s.name;
    // The session's role, next to the name it is part of — same tag the mesh
    // roster draws, so "worker" reads as the same fact in both places. Only
    // rendered when the session has one; most ad-hoc sessions do not, and a
    // blank pill on every row would just be noise.
    const role = s.role ? document.createElement("span") : null;
    if (role) {
      role.className = "mesh-role";
      role.textContent = s.role;
    }
    // And the name the mesh calls it by, when that is not the name above.
    // First of the qualifiers, before the role, because it is another way of
    // saying WHO this row is — the role and the rooms are both properties of
    // that identity, and a reader who came here from a mesh log looking for
    // `merger-r13` has to find it beside the name, not after two other pills.
    const hTag = handleTag(s.name);
    let handleBox = null;
    if (hTag) {
      handleBox = document.createElement("span");
      handleBox.className = "rail-handle";
      handleBox.textContent = hTag.text;
      handleBox.title = hTag.title;
    }
    // Then which rooms it is in. Role first and in colour, membership after
    // it in neutral grey: the pair reads as "what this session is, and where
    // it belongs", and only the first of those is a property of the session
    // itself. Drawn on the same terms as the role tag — a session in no mesh
    // gets nothing rather than an empty pill.
    const tags = railMeshTags(s.name);
    let meshBox = null;
    if (tags.length) {
      meshBox = document.createElement("span");
      meshBox.className = "rail-meshes";
      for (const t of tags) {
        const tag = document.createElement("span");
        tag.className = t.more ? "rail-mesh rail-mesh-more" : "rail-mesh";
        tag.textContent = t.text;
        tag.title = t.title;
        meshBox.appendChild(tag);
      }
    }
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = railMetaText(s);
    // The cap clips the line when the identity runs long against the rail;
    // the whole of it stays one hover away, the same recovery the name has.
    meta.title = meta.textContent;
    if (s.winddown) {
      li.title = [li.title, "being ended — settling its board issues first; " +
        "kill again to stop now"].filter(Boolean).join(" · ");
    }
    if (s.status === "exited") {
      li.title = [li.title, s.archived_at
        ? "archived — open it to inspect or resume"
        : "exited — open it to resume or archive"].filter(Boolean).join(" · ");
    }
    // How full this session's context is, on the rail row itself. The story
    // lives in the tooltip (a note on both the row and its name — that is
    // still the only place the model and the breakdown fit), but each row
    // now also spends one deliberate full-width line on it: a thin gauge
    // bar beside the short count, so twenty rows can be compared at a
    // glance. The line is a flex line-breaker like the cflow badge below
    // it, and nothing at all where a harness keeps no transcript.
    ctxNoteOnRow(li, label, s);
    const railCtx = ctxRailLine(s);
    // And where it runs — the checkout, which on this rail is usually a
    // worktree, and is the one fact that tells two sessions doing the same
    // job apart. A full-width line like the gauge below it; always drawn,
    // because every session runs somewhere.
    const railCwd = railCwdLine(s);
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
    // Spawn beside it, same row-action pattern, but for creating. An exited
    // session has nothing to spawn from ("an exited session cannot spawn
    // children"), so the + is the one action that row's state denies.
    let plus = null;
    if (s.status !== "exited") {
      plus = document.createElement("button");
      plus.className = "sess-plus";
      plus.type = "button";
      plus.textContent = "+";
      plus.title = "spawn a child of this session — same wizard the Spawn " +
        "button and quick job open";
      plus.addEventListener("click", (e) => {
        e.stopPropagation();
        openSpawnModal(s.name);
      });
    }
    // The name and the two things that qualify it travel together, in a box
    // that shrinks instead of wrapping. That is the whole trick: the row
    // itself must wrap (the cflow line and the briefing card are full-width
    // lines below it), and a wrapping flex line breaks before it shrinks — so
    // a pill sitting loose on the row pushed the ⓘ, and then the ▸, onto
    // lines of their own. Inside a nowrap box the pills have nowhere to break
    // to and give up width instead, which is what ellipsis is for.
    const head = document.createElement("span");
    head.className = "rail-head";
    head.append(label, ...(handleBox ? [handleBox] : []),
                ...(role ? [role] : []), ...(meshBox ? [meshBox] : []));
    // Where it runs, then how full it is, then who has been near it: the two
    // identity lines first and the state line under them, so a reader
    // scanning for "which of these has nobody touched" finds it in one
    // column rather than hunting a different offset on every row.
    const railSeen = railSeenLine(s);
    li.append(dot, head, meta, railCwd, ...(railCtx ? [railCtx] : []),
              railSeen, ...(plus ? [plus] : []), info);
    li.addEventListener("click", () => {
      location.hash = "#/s/" + encodeURIComponent(s.name);
    });
    // The briefing's one-line and the collapsed ⟳, always on the row.
    decorateBriefingRow(li, s);
    list.appendChild(li);
  }
  refreshResumeChoices();  // the spawn form offers these same conversations
  refreshParentChoices();  // ...and the same sessions, as parents to spawn from
  if (currentPage === "home") renderHome();
  syncBulkActions(sessionsCache);
  // Some embedded consumers reuse refreshSessions with a reduced rail DOM;
  // the shipped page has the control, while those consumers keep the list
  // behaviour they had before this optional view was added.
  if (typeof syncSessionFilters === "function") {
    syncSessionFilters(sessionsCache);
  }

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
  // The rail rows above were rebuilt with the handles the mesh poll last
  // knew; the header beside them is repainted from the same value here.
  renderTermHandle();
  // The rows and the runs arrive on separate polls; whichever lands last
  // paints the cflow badges over the rows that exist now.
  applyCflowBadges();
  applyRailQuiet();
  applyBriefingCards();
  // A rebuild throws away the class the goto press wrote onto its row; this
  // puts it back, so the mark outlives the poll that lands mid-scroll.
  applyGotoFlash();
}

/* The meshes a rail row speaks for — the rooms that session is in.

   Derived from the mesh poll rather than the session poll: /api/sessions
   knows nothing about meshes, and the rooms are already on the client for
   the sidebar's own list. Only LOCAL members are considered: a remote
   member's session name lives on another daemon and may well collide with
   one of ours, and tagging our row with somebody else's room would be a
   lie the reader cannot check. */
function sessMeshes(name) {
  const out = [];
  for (const m of meshCache || []) {
    for (const mem of m.members || []) {
      if (mem.local && mem.session === name) {
        out.push({ mesh: m.name, handle: mem.handle, role: mem.role || "" });
      }
    }
  }
  return out.sort((a, b) => a.mesh.localeCompare(b.mesh));
}

/* What the row actually draws: the first few rooms, then a count for the
   rest. A session is normally in one mesh, but nothing stops it joining
   several, and five pills would push the name it belongs to off the row. */
const RAIL_MESH_TAGS = 2;

function railMeshTags(name) {
  const meshes = sessMeshes(name);
  const shown = meshes.slice(0, RAIL_MESH_TAGS).map((m) => ({
    text: m.mesh,
    title: `mesh ${m.mesh} — joined as ${m.handle}` +
           (m.role ? ` (${m.role})` : ""),
  }));
  const rest = meshes.slice(RAIL_MESH_TAGS);
  if (rest.length) {
    // Flagged, not merely last: the row draws this one outside the shrinking
    // box (see below), so it has to be told apart from a room's name.
    shown.push({
      text: `+${rest.length}`,
      more: true,
      title: "also in " + rest.map((m) => `${m.mesh} (${m.handle})`).join(", "),
    });
  }
  return shown;
}

/* The names a session answers to in its rooms, when they are not the name
   this page calls it by.

   A session's mesh handle is chosen at join time and is free to differ from
   its session name: `s236` answers to `merger-r13`, and every message about
   it on the mesh uses that word. Until now the page said the handle in two
   tooltips and one chip at the bottom of the details panel, so a reader
   watching the rail had no way to connect the two — the mesh log named a
   session the rail did not list. Hence a value the three places that carry a
   session's identity can each draw.

   Only DIFFERING handles are collected. The common case is handle == name,
   and a pill repeating the name it sits beside is noise on a 260px rail; the
   fact worth surfacing is precisely the mismatch. Duplicates across rooms
   collapse for the same reason — joining four meshes as `merger-r13` is one
   name, not four. */
function sessHandles(name) {
  const out = [];
  for (const m of sessMeshes(name)) {
    if (!m.handle || m.handle === name) continue;
    if (!out.some((h) => h.handle === m.handle)) out.push(m);
  }
  return out;
}

/* That value as something drawable: the first differing handle, plus a count
   when a session answers to more than one, and the whole of it as hover.
   Null when there is nothing to say, which is what every caller tests. */
function handleTag(name) {
  const hs = sessHandles(name);
  if (!hs.length) return null;
  const rest = hs.length - 1;
  return {
    handle: hs[0].handle,
    text: rest ? `${hs[0].handle} +${rest}` : hs[0].handle,
    title:
      `session '${name}' answers to ` +
      hs.map((h) => `'${h.handle}' in ${h.mesh}` +
                    (h.role ? ` (${h.role})` : "")).join(", ") +
      " — address it by that name on the mesh",
  };
}

/* The terminal header's copy of that, painted from whatever the mesh poll
   last knew. Called at attach AND on both polls, because the two arrive
   independently: an attach that lands before the first /api/mesh answer has
   nothing to draw, and without a repaint the chip would stay empty until the
   reader switched terminals and came back. Down to nothing when the session
   answers to its own name, which is the ordinary case. */
function renderTermHandle() {
  const box = $("term-handle");
  if (!box) return;
  const tag = currentName ? handleTag(currentName) : null;
  box.classList.toggle("hidden", !tag);
  box.textContent = tag ? tag.text : "";
  box.title = tag ? tag.title : "";
}

/* The cflow run a rail row speaks for. Runs are keyed (cwd, session); after a
   migrate-session a stale run under the old cwd can share the scope, so the
   one whose canonical cwd still holds the live session wins. */
function sessCflowRun(name) {
  const runs = (cflowCache || []).filter(
    (r) => r.scope === name && r.status !== "idle"
  );
  return runs.find((r) => (r.sessions || []).includes(name)) || runs[0] || null;
}

/* Whether the run has stopped on something only a HUMAN resolves — a gate
   approval or a branch choice. waiting_answer is excluded only while it is
   genuinely with another agent: one that reached nobody is the reader's to
   clear (see answerFellToUs), and leaving it out is what let a stranded run
   sit in the rail looking like somebody else's problem. */
function sessCflowGated(r) {
  return r.status === "waiting_approval" || r.status === "waiting_selection" ||
         r.status === "waiting_goto" || answerFellToUs(r);
}

function sessCflowLabel(r) {
  if (r.status === "waiting_selection") return "choose an option";
  // The agent is asking to leave the route its workflow declares. Named by
  // where it wants to go: that is the whole of what is being decided.
  if (r.status === "waiting_goto")
    return `wants to move to '${(r.goto_request || {}).step || "?"}'`;
  if (r.status === "waiting_approval")
    return r.reason === "loop_limit" ? "loop limit — approve to continue"
         : r.reason === "declined" ? "declined — decide"
         : "approval needed";
  if (r.status === "waiting_answer")
    return answerFellToUs(r) ? "asked of nobody — approve to continue"
                             : `with ${askWho(r.ask)}`;
  if (r.status === "report_required") return "report required";
  // "held → 19:52", the short form s107 draws on the diagram and the flow
  // card. One state, one wording, wherever a reader meets it.
  if (r.status === "waiting_window")
    return `'${r.option}' held → ${fmtOpensAt(r.opens_at)}`;
  if (r.status === "done" || r.status === "error" || r.status === "aborted")
    return r.status;
  return r.title || r.step_id || "running";
}

/* A paced option's opening moment, as a local clock time; the raw ISO
   string when it does not parse. */
function fmtOpensAt(iso) {
  const t = Date.parse(iso || "");
  if (Number.isNaN(t)) return iso || "?";
  return new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/* One line under each rail row: which workflow the session is on and where it
   stands, amber-flagged when it is the reader's move. Applied idempotently
   from both refreshSessions (rows rebuilt) and refreshCflow (runs updated),
   because the two caches fill on independent requests. */
function applyCflowBadges() {
  const list = $("session-list");
  if (!list) return;
  for (const li of list.querySelectorAll("li[data-name]")) {
    const old = li.querySelector(".sess-cflow");
    const r = sessCflowRun(li.dataset.name);
    if (!r) { if (old) old.remove(); continue; }
    const line = old || document.createElement("span");
    if (!old) {
      // The badge walks to the run page; the row it sits on attaches. One
      // listener for the element's lifetime — it reads the run key from the
      // dataset, which every poll below rewrites (a migrate-session moves
      // the run's cwd under the same scope).
      line.addEventListener("click", (e) => {
        e.stopPropagation();
        location.hash = "#/wf/" + encodeURIComponent(line.dataset.wf || "");
      });
      li.appendChild(line);
    }
    line.dataset.wf = `${r.scope || "default"}|${r.cwd}`;
    const gated = sessCflowGated(r);
    line.className = `sess-cflow${gated ? " gated" : ""}`;
    line.textContent = "";
    const [markCls, markGlyph] = wfMark(r.status, r);
    const mark = document.createElement("span");
    mark.className = markCls;
    mark.textContent = markGlyph;
    const txt = document.createElement("span");
    txt.className = "sess-cflow-text";
    txt.textContent =
      `${r.workflow || "cflow"} · ${gated ? "⚑ " : ""}${sessCflowLabel(r)}`;
    line.title = gated
      ? (r.gate || r.prompt || "") +
        (r.options ? ` — options: ${r.options.join(", ")}` : "")
      : (r.title || r.step_id || "");
    line.append(mark, txt);
  }
}

/* The two settings that stop the daemon typing into a session, drawn on that
   session's own row.

   They are separate mechanisms — one is the mesh delivery gate
   (Session.delivery_held), the other is the step reminder clock
   (cflow_clock.ReminderClock) — and they are shown together because from the
   rail they are one question: which of these terminals is the daemon not
   going to speak into. Both are silences somebody chose, and a silence
   nobody remembers choosing is indistinguishable from a broken daemon; that
   is the whole reason these have a row at all rather than living only in the
   pages that own them.

   Only drawn when a flag is actually set. An "everything is normal" pill on
   twenty rows is a row of noise, and the state worth finding is the odd one.

   Two polls feed it — /api/sessions carries the hold, /api/cflow carries the
   reminder — so it is repainted from both, idempotently, the same shape as
   the cflow badge above. */
function railQuietFlags(name) {
  const out = [];
  const s = (sessionsCache || []).find((x) => x.name === name);
  // Exited rows are left out: a record with no terminal holds nothing back
  // (DeadSession.delivery_held), so a pill there would name a hold that is
  // not being applied.
  if (s && s.delivery_hold && s.status !== "exited") {
    out.push({
      cls: "quiet-hold",
      text: "held",
      title:
        "delivery held: somebody pinned this session shut, so no mesh " +
        "message is typed in here until it is released.\n" +
        "Nothing is dropped — the backlog goes in on release, or on " +
        "'deliver now'.\n" +
        "Release: `claunch delivery-hold " + name + " --off`, or the " +
        "terminal header's own control.",
    });
  }
  const r = sessCflowRun(name);
  const rem = r && r.timers && r.timers.reminder;
  if (rem && rem.enabled === false) {
    // Whose decision it was. The run's own override and the machine default
    // read identically on the row — the clock is silent either way — but
    // they are undone in different places, so the tooltip has to separate
    // them or it sends the reader to the wrong switch.
    const own = r.reminder && r.reminder.enabled === false;
    out.push({
      cls: "quiet-remind",
      text: "reminder off",
      title:
        "step reminder off: the daemon will not re-type this run's current " +
        "step into this session, however long it sits.\n" +
        (own
          ? "Set for this run (its own override, kept in the run's state " +
            "and so still off after a daemon restart). Turn it back on " +
            "from the run page, or the terminal header's chip."
          : "Not this run's doing — step reminders are off machine-wide " +
            "(daemon config `cflow_reminder`). Every run reads this way " +
            "until that is changed."),
    });
  }
  return out;
}

function applyRailQuiet() {
  const list = $("session-list");
  if (!list) return;
  for (const li of list.querySelectorAll("li[data-name]")) {
    const old = li.querySelector(".rail-quiet");
    const flags = railQuietFlags(li.dataset.name);
    if (!flags.length) { if (old) old.remove(); continue; }
    const line = old || el("span", "rail-quiet");
    line.textContent = "";
    for (const f of flags) {
      const pill = el("span", `rail-quiet-pill ${f.cls}`);
      // The glyph is the pause the header chip uses for the same fact, so
      // "stopped on purpose" reads the same in both places.
      pill.append(el("span", "quiet-glyph", "⏸"), el("span", null, f.text));
      pill.title = f.title;
      line.appendChild(pill);
    }
    if (!old) li.appendChild(line);
  }
}

/* ------------------------------------------------------------------ */
/* the nudge clocks                                                   */
/* ------------------------------------------------------------------ */
/* The strip above the rail's nav: what the daemon is about to type into a
   session by itself, and how long is left of it.

   Two clocks can do that (daemon/cflow_clock.py). The REMINDER re-types the
   current step into a session that is working and has stopped moving; the
   STALL PING wakes one that stopped altogether at a step nothing is holding.
   They are complements — at any moment at most one of them applies to a given
   run — so this shows the one that would actually speak next, and names it.
   Showing only the reminder would report "off" at exactly the moments the
   ping is the live clock, which is the wrong answer told confidently.

   Everything the daemon knows rides in `run.timers` on the 2s /api/cflow
   poll. The countdown between polls is local arithmetic on the last reading,
   which is why railTimerLine takes the elapsed seconds rather than reading a
   clock itself: it is the one function here worth testing, and a function
   that calls Date.now() cannot be. */

/* Seconds as a countdown a person reads at a glance: 7 -> "0:07",
   252 -> "4:12", 3852 -> "1:04:12". Deliberately not fmtAge's "4m" — the
   whole point of this strip is watching the last minute run out. */
function fmtCountdown(sec) {
  const n = Math.max(0, Math.round(Number(sec) || 0));
  const s = n % 60, m = Math.floor(n / 60) % 60, h = Math.floor(n / 3600);
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return (h ? `${h}:` : "") + `${mm}:${String(s).padStart(2, "0")}`;
}

/* Which clock's story the strip tells, when both have one. Ordered by how
   much a reader needs it: something about to happen beats something counting,
   which beats a clock that is armed but deliberately quiet, which beats every
   flavour of silence.

   Among the silences the order is the one that keeps a reader from being sent
   the wrong way. A tick that is not RUNNING is a defect and outranks the two
   silences that are working as designed: BLOCKED (the run is on a gate this
   clock is not allowed to touch) and OFF (somebody turned it off). Reporting
   "off" over a dead tick is the trap — it sends a person to switch on a thing
   that is already on. And blocked outranks off because it is the answer to
   the question actually being asked: not "is this configured" but "why is
   nothing happening". */
const RAIL_TIMER_RANK = {
  due: 0, counting: 1, held: 2, waiting: 3, watching: 4,
  arming: 5, stopped: 6, blocked: 7, off: 8,
};

/* The clock a run's strip speaks for, or null when the daemon published no
   timers for it (an older daemon, or a slot that is idle or errored). */
function railTimerPick(run) {
  const timers = (run && run.timers) || null;
  if (!timers) return null;
  const cands = [];
  for (const clock of ["reminder", "ping"]) {
    const c = timers[clock];
    if (c && c.state) cands.push(Object.assign({}, c, { clock }));
  }
  if (!cands.length) return null;
  const rank = (c) => {
    const r = RAIL_TIMER_RANK[c.state];
    return r === undefined ? 99 : r;
  };
  const soonest = (c) =>
    c.due_in === null || c.due_in === undefined ? Infinity : c.due_in;
  cands.sort((a, b) => (rank(a) - rank(b)) || (soonest(a) - soonest(b)));
  return Object.assign({}, cands[0], {
    scope: run.scope, cwd: run.cwd, workflow: run.workflow,
    others: cands.slice(1),
  });
}

const RAIL_TIMER_GLYPH = {
  due: "!", counting: "⏱", held: "⏸", waiting: "⏸",
  watching: "◎", arming: "⏱", blocked: "⏸", off: "○",
  stopped: "⚠",
};

/* One clock's line, `elapsed` seconds after its numbers were read.

   The state can change under that elapsed time and is recomputed here rather
   than trusted: a reading that said "counting, 3s left" is, four seconds
   later, a clock that is due. Letting the strip keep counting down past zero
   into negative numbers would be the one thing worse than saying nothing. */
function railTimerLine(pick, elapsed = 0) {
  if (!pick) return null;
  const name = pick.clock === "ping" ? "stall ping" : "step reminder";
  let state = pick.state;
  const due = pick.due_in === null || pick.due_in === undefined
    ? null : pick.due_in - (Number(elapsed) || 0);
  if (state === "counting" && due !== null && due <= 0) state = "due";
  let text;
  if (state === "counting") text = `${name} in ${fmtCountdown(due)}`;
  else if (state === "due") text = `${name} due now`;
  else if (state === "held") text = `${name} held — session stopped`;
  else if (state === "waiting") text = `${name} armed — session working`;
  else if (state === "watching") text = `watching: ${pick.awaits || "a signal"}`;
  else if (state === "arming") text = `${name} arming`;
  else if (state === "blocked") text = `${name} paused — not this run's move`;
  else if (state === "off") text = `${name} off`;
  else if (state === "stopped") text = "clock not running";
  else text = `${name} — ${state}`;
  return {
    state,
    glyph: RAIL_TIMER_GLYPH[state] || "⏱",
    text,
    title: railTimerTitle(pick, state),
  };
}

/* The hover text: both clocks, so the one this line is NOT about is still
   answerable without a trip to the run page — "why is it saying stall ping"
   has its answer in the reminder's own line. */
function railTimerTitle(pick, state) {
  const say = (c, st) => {
    const bits = [c.clock === "ping" ? "stall ping" : "step reminder", st];
    if (!c.running) bits.push("clock not running");
    else if (!c.enabled) bits.push("switched off");
    else if (c.interval) bits.push(`every ${fmtCountdown(c.interval)}`);
    if (c.fired_ago !== null && c.fired_ago !== undefined) {
      bits.push(`last fired ${fmtCountdown(c.fired_ago)} ago`);
    }
    /* The reminder pastes the whole step the first time it speaks at a
       position and a short pointer after that, so "next fire" is two very
       different sizes and the reader deciding whether to let it speak wants
       to know which. The ping has one form and says nothing here. */
    if (c.form === "short") bits.push("next: short form");
    else if (c.form === "full") bits.push("next: full restatement");
    return bits.join(" · ");
  };
  const lines = [`${pick.workflow || "cflow"} · ${pick.scope || "default"}`];
  lines.push(say(pick, state));
  for (const other of pick.others || []) lines.push(say(other, other.state));
  if (state === "counting" || state === "due") {
    lines.push(
      "the daemon types this into the session by itself; the clock is " +
      "re-armed whenever the run moves"
    );
  }
  return lines.join("\n");
}

/* ------------------------------------------------------------------ */
/* the clock, on the session's own header                             */
/* ------------------------------------------------------------------ */
/* The countdown's one home is the chip in the session's header: it answers
   "is the daemon about to type into THIS session, and when". The attached
   session's run, or nothing at all — a countdown drawn beside a session's
   name is read as that session's, so any other run's clock would be a
   confident lie. That is what the two differences from the rail's old strip
   were for, and both stay because the reasoning survives the strip's
   removal:

   * no fallback. The attached session's run or nothing;
   * no scope label. The chip needs no "whose clock" tag — the name is
     already at the other end of the same row, and repeating it would be
     noise.

   Reusing railTimerLine rather than writing a second one is the point: one
   vocabulary and one wording for one clock, tested through the chip. */

/* The run the header's chip speaks for: the attached session's, or null. */
function termTimerRun() {
  if (typeof currentName !== "string" || !currentName) return null;
  const run = sessCflowRun(currentName);
  return run && run.timers ? run : null;
}

/* The last reading, stamped with the session it was taken FOR. A terminal
   switch repaints long before the 2s poll comes round, and the previous
   session's countdown left on the new session's header would be the one
   mistake this chip exists to avoid.

   `remind` rides along beside the pick because the chip is a SWITCH as well
   as a readout, and the two do not always speak for the same clock: the line
   reports whichever clock is loudest (the strip's ranking, unchanged), while
   the switch is always this run's step reminder — the only one of the two a
   person can turn off here at all (the stall ping is machine-wide, see
   cflow_clock.ping_policy). So the reminder's own standing has to be kept,
   not re-derived from a pick that may be about the ping. */
let termTimerRead = null;
let termTimerTicker = null;
let termTimerBusy = false;   // one flip at a time; a double-click is one flip
let termTimerSkipBusy = false;  // same, for the skip beside it

function renderTermTimer() {
  const run = termTimerRun();
  termTimerRead = {
    name: currentName, pick: railTimerPick(run), at: Date.now(),
    remind: (run && run.timers && run.timers.reminder) || null,
  };
  paintTermTimer();
}

/* What the chip's leading glyph means, and it is not what the strip's means.

   The strip's glyph states the clock's condition, and it has to: a strip with
   no subject can offer nothing to press. This chip has a subject — one
   session, one run — so the same few pixels are worth more as the control
   than as a second copy of a state the line spells out in words beside it and
   the colour carries anyway.

   Which is also the bug it fixes. The strip's vocabulary hangs "⏸" on held,
   waiting and blocked — every state EXCEPT the one a person actually caused —
   and "○" on `off`, the switched-off one. On a strip that is only slightly
   odd; on a button it is a lie, and the lie a reader acts on: a pause icon on
   something pressable promises a pause, and pressing it used to navigate away
   to the run page instead.

   So: filled pause = the reminder is on, press to stop it; play = it is off,
   press to start it again. The condition of the clock stays in the words. */
const TERM_TIMER_HOLD_GLYPH = { on: "⏸", off: "▶" };

/* Whether the header's chip can offer the switch at all: a run to address
   (the pick carries its cwd and scope) and a daemon that published the
   reminder's standing. An older daemon that publishes timers without it
   still gets the strip's readout and the strip's click — a chip that
   silently did nothing would be worse than one that navigates. */
function termTimerHold() {
  const read = termTimerRead;
  if (!read || !read.pick || !read.remind) return null;
  if (!read.pick.cwd) return null;
  return { on: !!read.remind.enabled, cwd: read.pick.cwd,
           scope: read.pick.scope || "default" };
}

/* The reminder states in which a skip has something to skip.

   All three are the same fact seen at three moments: this clock is holding a
   live timer for this run and the next thing it does is type. `counting` is
   before, `due` is at, and `held` is after — due, and waiting only for the
   session to be working again, which makes it the state where a skip is
   worth the most: a held reminder is retried EVERY poll, so it lands the
   instant the agent starts its next turn.

   The rest are not "not yet", they are nothing to skip, and offering the
   button on them would be a promise the daemon cannot keep. `arming` has no
   timer in the clock's table at all (the run just moved); `watching` has an
   interval of zero and speaks only when its probe changes; `off`, `blocked`
   and `stopped` are the three ways the clock is already silent — and on
   those the honest control is the switch beside it, or nothing. */
const TERM_TIMER_SKIPPABLE = { counting: 1, due: 1, held: 1 };

/* The run whose next reminder this press would skip, or null when there is
   none to skip.

   Read off the REMINDER's own standing, never off the line's — the chip
   reports whichever of the two clocks is loudest, so a run counting down to
   a reminder can be showing a stall ping that is due, and a skip decided
   from the line would then be offered for a clock this button cannot touch
   (the ping is machine-wide) or hidden for one it can. Same split, and the
   same reason, as the switch's `remind` above. */
function termTimerSkip() {
  const read = termTimerRead;
  if (!read || !read.pick || !read.remind) return null;
  if (!read.pick.cwd) return null;
  const state = read.remind.state;
  if (!TERM_TIMER_SKIPPABLE[state]) return null;
  return { state, cwd: read.pick.cwd, scope: read.pick.scope || "default",
           interval: read.remind.interval };
}

function paintTermTimer() {
  const box = $("term-timer");
  if (!box) return;
  const read = termTimerRead;
  const line = read && read.name === currentName
    ? railTimerLine(read.pick, (Date.now() - read.at) / 1000) : null;
  // Gated on `line`, not on the reading alone: a reading taken for the
  // session we just walked away from must not leave a live skip button
  // sitting on the new session's name any more than it may leave the
  // countdown there.
  paintTermTimerSkip(line ? termTimerSkip() : null);
  if (!line) {
    box.className = "term-btn timer-chip hidden";
    box.textContent = "";
    box.removeAttribute("title");
    box.disabled = false;
    return;
  }
  const hold = termTimerHold();
  box.className = `term-btn timer-chip ${line.state}`;
  if (hold) box.classList.toggle("timer-paused", !hold.on);
  box.title = termTimerTitle(read.pick, line, hold);
  box.textContent = "";
  box.append(
    el("span", "tt-glyph",
       hold ? TERM_TIMER_HOLD_GLYPH[hold.on ? "on" : "off"] : line.glyph),
    el("span", "tt-text", line.text)
  );
  box.disabled = termTimerBusy;
  // Wired once: textContent above wipes the children, not the box, so a
  // listener added per repaint would stack.
  if (!box.dataset.wired) {
    box.dataset.wired = "1";
    box.addEventListener("click", termTimerClick);
  }
}

/* The strip's hover text plus the two things a control owes a reader that a
   readout does not: what pressing it does, and — since pressing it no longer
   goes there — where the run page still is. */
function termTimerTitle(pick, line, hold) {
  const lines = [line.title];
  if (hold) {
    lines.push(
      "Click to " + (hold.on
        ? "PAUSE this run's step reminder: the daemon stops re-typing the "
        + "step into this session until you say."
        : "RESUME this run's step reminder: the daemon may re-type the step "
        + "into this session again."),
      // Named by the route that survives: the badge is on the session's own
      // row and goes to that session's run, where the strip above the nav is
      // one element on a page other people are rearranging.
      "Set for this run only; the machine defaults stay untouched. The "
      + "interval lives on the run page — the run's badge in the rail "
      + "opens it."
    );
  } else {
    lines.push("Click to open the run page, where the interval is editable.");
  }
  return lines.join("\n");
}

async function termTimerClick() {
  const hold = termTimerHold();
  if (!hold) {
    // No switch to offer (an older daemon): the strip's destination, on this
    // chip's own reading — what this chip did before it had a switch.
    const p = termTimerRead && termTimerRead.pick;
    if (!p) return;
    location.hash =
      "#/wf/" + encodeURIComponent(`${p.scope || "default"}|${p.cwd}`);
    return;
  }
  if (termTimerBusy) return;
  termTimerBusy = true;
  paintTermTimer();
  // `enabled` alone: the override merges, so a run that had an interval set
  // keeps it, and nothing here has to know the floor (cflow_engine.set_reminder).
  await cflowAction("/api/cflow/reminder", {
    cwd: hold.cwd, scope: hold.scope, enabled: !hold.on,
  });
  termTimerBusy = false;
  // cflowAction re-polls, but the poll is up to 2s away and a switch that
  // takes two seconds to look flipped reads as a switch that did nothing.
  if (termTimerRead && termTimerRead.remind) {
    termTimerRead.remind = Object.assign(
      {}, termTimerRead.remind, { enabled: !hold.on }
    );
  }
  paintTermTimer();
}

/* The skip beside the switch: one press, one reminder let go by.

   Icon only, and that is not a space saving. The chip next to it already
   spells the clock out in words, and the one thing this button adds to that
   sentence is a verb — a second copy of "step reminder" on the same row
   would push the countdown into its ellipsis to say nothing new. */
function paintTermTimerSkip(skip) {
  const box = $("term-timer-skip");
  if (!box) return;
  if (!skip) {
    box.className = "term-btn timer-skip hidden";
    box.textContent = "";
    box.removeAttribute("title");
    box.disabled = false;
    return;
  }
  // Wearing the clock's state, like the chip: on `due` and `held` this is
  // the button somebody is reaching for, and it should be visible from the
  // same glance that made them reach.
  box.className = `term-btn timer-skip ${skip.state}`;
  box.textContent = "⏭";
  box.title = termTimerSkipTitle(skip);
  box.disabled = termTimerSkipBusy;
  if (!box.dataset.wired) {
    box.dataset.wired = "1";
    box.addEventListener("click", termTimerSkipClick);
  }
}

/* What the press does, and — the part a reader has to be told, because the
   button beside it does the other thing — what it does NOT do. */
function termTimerSkipTitle(skip) {
  const lines = [
    skip.state === "held"
      ? "SKIP the reminder now waiting: it is due and is retried every "
        + "poll, so it lands the moment this session is working again. "
        + "Press to let it go instead."
      : "SKIP this one step reminder: the daemon does not re-type the step "
        + "into this session now.",
    "The clock stays on" + (skip.interval
      ? `, and the next one is due in ${fmtCountdown(skip.interval)}.`
      : ".")
      + " Nothing is stored: neither this run's setting nor the machine "
      + "defaults change — that is the ⏸ beside it, and it is a pause you "
      + "have to remember to undo.",
  ];
  return lines.join("\n");
}

async function termTimerSkipClick() {
  const skip = termTimerSkip();
  if (!skip || termTimerSkipBusy) return;
  termTimerSkipBusy = true;
  paintTermTimer();
  // No body but the run: this door sets nothing, so there is nothing to
  // send it. `skipped: false` comes back when the daemon was keeping no
  // timer for the run — not an error, and the next poll says so in the
  // clock's own words, so it is left to the poll rather than alerted.
  await cflowAction("/api/cflow/reminder/skip", {
    cwd: skip.cwd, scope: skip.scope,
  });
  termTimerSkipBusy = false;
  // Same reason the switch does this: the poll is up to 2s away, and a
  // countdown that goes on counting down for two seconds after a skip reads
  // as a skip that did nothing. Re-armed locally exactly as the daemon just
  // re-armed it — a full interval from now.
  const read = termTimerRead;
  if (read && read.remind && skip.interval) {
    read.remind = Object.assign({}, read.remind, {
      state: "counting", due_in: skip.interval,
    });
    // Only when the line is speaking for the reminder. When the ping is the
    // loudest clock the chip is telling the ping's story, and restamping the
    // reading would rewind the ping's countdown along with it.
    if (read.pick && read.pick.clock === "reminder") {
      read.pick = Object.assign({}, read.pick, {
        state: "counting", due_in: skip.interval,
      });
      read.at = Date.now();
    }
  }
  paintTermTimer();
}

/* ------------------------------------------------------------------ */
/* context size                                                       */
/* ------------------------------------------------------------------ */
/* How full each session's conversation is. The daemon reads it out of the
   transcript the harness itself writes (see daemon/ctxsize.py) and hangs it
   on the session as `context`, so everything here is formatting — no fetch
   of its own, no second opinion about what the number means.

   Two facts shape every string below:

   * No percentage is printed. On claude there is no denominator to make
     one: nothing records the context limit and it differs by model — a
     claude-opus-5 session in this fleet was measured at 286,674 tokens, so a
     hardcoded 200k would already be a lie. Codex is a different case, since
     it is told the model context window on every request; that number is
     named directly in the breakdown and marked on the gauge, and the
     arithmetic is left to the reader. The model's name is shown either way:
     it is what someone who wants to judge "is that a lot" actually needs.
   * The number is the last *completed* turn's, never this instant's, so its
     age travels with it. An idle session's hour-old reading is exactly
     right; a busy one's is a floor.

   And where there is no number there is no number: "not known", never 0. */

function ctxShort(n) {
  if (!Number.isFinite(n) || n < 0) return "?";
  if (n < 1000) return String(n);
  const k = n / 1000;
  return (k < 10 ? k.toFixed(1) : String(Math.round(k))) + "k";
}

function ctxAgeOf(iso) {
  const t = Date.parse(iso || "");
  return Number.isFinite(t)
    ? fmtAge(Math.max(0, Math.floor((Date.now() - t) / 1000)))
    : "";
}

/* Whether this session is one that *could* have a reading. Two harnesses
   keep a record the daemon reads this out of: claude's transcript and
   codex's rollout. On any other harness the absence is not news to report —
   the row says nothing rather than "unknown", which would read as something
   having gone wrong. */
function ctxKnowable(s) {
  const harness = (s && s.harness) || "claude";
  return !!s && (harness === "claude" || harness === "codex");
}

/* ---- which model this session is actually answering on ---- */
/* The same reading carries the model id the harness sent that turn to, and
   that is the one fact here nobody can get from anywhere else: a session's
   model is chosen inside the terminal (/model), not at spawn, so the
   launcher's own definition cannot know it and neither can the operator
   without attaching. It was already travelling in `context.model` and being
   spent entirely on tooltips; these two functions are what put it on screen.

   It is the LAST COMPLETED TURN's model, exactly like the count beside it.
   A session switched with /model mid-answer still reads as the old one until
   it finishes a turn — which is why the age of the reading is what the
   detail row says beside the name, rather than a bare present tense. */

/* The rail's version of the id: short enough for a row, still recognisable.

   Three things are dropped, each because it is the same model either way —
   the host path a gateway prefixes (`accounts/fireworks/models/glm-5p2`),
   the dated release (`claude-haiku-4-5-20251001`), and the vendor word every
   row in a claude fleet would otherwise repeat. What is left is the name and
   its version, and the version is why the hyphens are not simply spaced out:
   `4-5` is one number, and "haiku 4 5" would read as two. The full id is
   never thrown away — it is what the tooltip and the detail row say. */
function modelShort(id) {
  const raw = String(id || "").trim();
  if (!raw) return "";
  const tail = raw.split("/").pop() || raw;
  return tail
    .replace(/-\d{8}$/, "")
    .replace(/^claude-/, "")
    .replace(/(\d)-(?=\d)/g, "$1.")
    .replace(/-/g, " ");
}

/* The detail panel's version: the full id and how old the reading is, or the
   honest absence. Empty — no row at all — where the session is not one that
   could have a model to report, on the same terms as `ctxKnowable`: a
   harness that keeps neither transcript nor rollout has nothing to read, and
   "unknown" there would read as a fault. */
function modelSentence(s) {
  if (!ctxKnowable(s)) return "";
  const c = s && s.context;
  if (!c || !c.model) return "not known yet — no context reading recorded";
  const age = ctxAgeOf(c.at);
  return `${c.model}${age ? ` (as of its turn ${age} ago)` : ""}`;
}

/* The whole sentence, for a tooltip or a details row. Empty where there is
   nothing to say at all. */
function ctxSentence(s) {
  const c = s && s.context;
  if (!c) {
    return ctxKnowable(s)
      ? "context not known yet — no reading recorded"
      : "";
  }
  const parts = [`context ${c.tokens.toLocaleString()} tokens`];
  if (c.model) parts.push(c.model);
  const age = ctxAgeOf(c.at);
  parts.push(age ? `as of ${age} ago` : "as of its last turn");
  return parts.join(" · ");
}

/* The breakdown, for the tooltip under the sentence. The three input numbers
   are one number split by how it was billed, not three different things —
   said here so nobody reads "cache read 154k" as an aside to a small
   "input 2". */
function ctxBreakdown(c) {
  if (!c) return "";
  const modelWindow = Number.isFinite(c.model_context_window)
    && c.model_context_window > 0 ? c.model_context_window : 0;
  return [
    `fresh input ${c.input.toLocaleString()}`,
    `replayed from cache ${c.cache_read.toLocaleString()}`,
    `written to cache ${c.cache_write.toLocaleString()}`,
    `answer ${c.output.toLocaleString()}`,
    /* Whichever of the two is true of this reading. Codex is told the
       model's context window on every request, so naming it is the answer to
       "is that a lot"; claude has no such number recorded anywhere, and
       saying that out loud is what keeps the missing percentage from reading
       as an oversight. Still no percentage either way — the count and the
       window are both here and the arithmetic is the reader's. */
    modelWindow
      ? `model context window ${modelWindow.toLocaleString()} tokens`
      : "no percentage: the context limit is not recorded anywhere and " +
        "differs by model",
  ].join("\n");
}

/* What the rail row adds to its tooltip — the full story behind the glance
   the gauge line below gives it: the model, the age of the reading, and the
   breakdown, none of which fit on the row itself. */
function ctxTooltip(s) {
  const sentence = ctxSentence(s);
  if (!sentence) return "";
  const c = s && s.context;
  return c ? `${sentence}\n${ctxBreakdown(c)}` : sentence;
}

/* Hang that note on a rail row, without giving the row a single new pixel.

   On both the row and its name, because a child's `title` wins over its
   parent's wherever the pointer actually lands — and the name is where it
   lands. The name carries one of its own (the rail clips a long name with an
   ellipsis and the tooltip is where the rest of it went), so the note joins
   that one instead of replacing it: a row whose name is cut still has to be
   able to say what its name was.

   This is a function rather than three lines inside the row builder so it
   can be tested, and so the row builder — which is a busy piece of code that
   more than one pair of hands edits — carries one line about context and no
   more. */
function ctxNoteOnRow(row, name, s) {
  const note = ctxTooltip(s);
  if (!note) return;
  const join = (had) => [had, note].filter(Boolean).join("\n");
  if (row) row.title = join(row.title);
  if (name) name.title = join(name.title);
}

/* The chip on an open briefing card's head — the same fact, in the one place
   that already has room for it. */
function ctxChip(name) {
  const s = sessionsCache.find((x) => x.name === name);
  if (!ctxKnowable(s)) return null;
  const c = s && s.context;
  const chip = el(
    "span", "sess-brief-ctx" + (c ? "" : " unknown"),
    c ? `${ctxShort(c.tokens)} ctx` : "ctx ?"
  );
  chip.title = ctxTooltip(s);
  return chip;
}

/* The gauge's fixed domain: every bar spans 0–1M tokens, whatever the model.
   A shared domain is what makes twenty bars comparable — the same fill on
   two rows means the same count — and 1M is the widest window any current
   model offers, so nothing overruns it. This is a drawing domain, not a
   claim about any model's limit; the fact worth marking on it is the
   session's own auto-compact threshold, which the daemon resolves from the
   child's env and hands over as `context.compact_window`. */
const CTX_DOMAIN = 1_000_000;

/* The dedicated line a rail row spends on context: a thin gauge bar with the
   short count beside it ("155k"), right under the name line. The bar spans
   the fixed 0–1M domain, with a tick where this session's
   CLAUDE_CODE_AUTO_COMPACT_WINDOW sits — the point claude will compact at —
   so "how close to compaction" is read as fill-against-tick, not
   fill-against-the-end. The warm/hot colouring is judged against that same
   window where one is configured (against the domain otherwise), because
   compaction fires at the tick, not at 1M. Where a claude session has not
   answered yet the track stays empty beside a greyed "?", and a harness
   that keeps no transcript gets no line at all. The model's short name
   leads the line, because it is what the fill has to be read against; the
   rest of the story (the full id, the age, the breakdown) stays in the
   tooltip — this is the glance, not the reading. */
function ctxRailLine(s) {
  if (!ctxKnowable(s)) return null;
  const c = s && s.context;
  /* What the tick marks is whichever threshold this harness actually
     reports: claude's configured auto-compact point, or the model context
     window codex is told on every request. They are different facts, so the
     tick and the tooltip name the one they are drawing. */
  const compact = c && Number.isFinite(c.compact_window) && c.compact_window > 0
    ? c.compact_window : 0;
  const reported = c && Number.isFinite(c.model_context_window)
    && c.model_context_window > 0 ? c.model_context_window : 0;
  const winKind = compact ? "auto-compact window" : "model context window";
  const win = Math.min(compact || reported, CTX_DOMAIN);
  const line = el("span", "rail-ctx-line" + (c ? "" : " unknown"));
  const bar = el("span", "rail-ctx-bar");
  if (c) {
    const level = c.tokens / (win || CTX_DOMAIN);
    const fill = el("span", "rail-ctx-fill" +
                    (level >= 0.9 ? " hot" : level >= 0.7 ? " warm" : ""));
    fill.style.width = (Math.min(1, c.tokens / CTX_DOMAIN) * 100).toFixed(1) + "%";
    bar.appendChild(fill);
  }
  if (win) {
    const tick = el("span", "rail-ctx-tick");
    tick.style.left = ((win / CTX_DOMAIN) * 100).toFixed(1) + "%";
    tick.title = `${winKind}: ${ctxShort(win)} tokens` +
                 (compact ? " (CLAUDE_CODE_AUTO_COMPACT_WINDOW)" : "");
    bar.appendChild(tick);
  }
  const num = el("span", "rail-ctx" + (c ? "" : " unknown"),
                 c ? ctxShort(c.tokens) : "?");
  // The model, first on the line, because it is what the count is read
  // against: the bar's fill is only "a lot" relative to the window the model
  // has, and the operator scanning twenty rows for the expensive one is
  // looking for this word, not for a number. Short form here (the row has no
  // width for a dated id) with the full id in the line's tooltip, and
  // nothing at all rather than a placeholder where no turn has been answered
  // yet — the "?" beside the empty track already says that once.
  const short = c ? modelShort(c.model) : "";
  const model = short ? el("span", "rail-model", short) : null;
  if (model) model.title = c.model;
  line.append(...(model ? [model] : []), bar, num);
  const note = ctxTooltip(s);
  const scale = c
    ? "bar spans 0–1M tokens" +
      (win ? `; the tick is the ${winKind} at ${ctxShort(win)}` : "")
    : "";
  line.title = [note, scale].filter(Boolean).join("\n");
  return line;
}

/* Where a session runs: the checkout its harness was started in.

   A worktree is the interesting case. This launcher spawns most of its
   agents into `<repo>/.claude/worktrees/<name>`, so a rail full of sessions
   from one repository differs only in that last segment — and the segment
   before it, "worktrees", is the same on every row and says nothing. The
   plain tail-of-path shortening ("…/worktrees/s84-scroll-restore") keeps the
   noise and drops the repository, which is the one word that tells a
   worktree of THIS repo from a worktree of another. So a worktree path is
   read as the two facts it is — which repository, which checkout — and any
   other path keeps the ordinary "…/last/two" form. `null` when the path is
   not a worktree, so a caller can tell the two shapes apart. */
function cwdSplit(p) {
  const parts = (p || "").split(/[\\/]+/).filter(Boolean);
  const i = parts.lastIndexOf("worktrees");
  if (i < 2 || parts[i - 1] !== ".claude" || i + 1 >= parts.length) return null;
  return { repo: parts[i - 2], worktree: parts.slice(i + 1).join("/") };
}

function cwdShort(p) {
  const wt = cwdSplit(p);
  return wt ? `${wt.repo} › ${wt.worktree}` : (p ? shortenPath(p) : "");
}

/* One line saying WHERE the session is, drawn wherever its name is: under
   the name on its rail row (`rail-cwd`) and under the name in the detail
   panel's head (`sess-cwd`). Short form on the line (a 260px rail has no
   width for `F:\works\claude-launcher\.claude\worktrees\…`) and the full
   path on its title, so nothing is lost, only folded. A session created
   without a directory runs in the daemon's own, and the line says so in
   words rather than printing an empty string that reads as "no directory" —
   the form that created it offers the same choice under the same name,
   "(daemon cwd)". `cls` is the place-specific class; the `worktree` and
   `unknown` markers ride along so each place can dress them.

   The branch follows the path as a sibling (`cwd-branch`) that never
   shrinks, with the ellipsis falling on the path element instead. A
   worktree's "repository › checkout" already brushes the edge of a 260px
   rail, and the one fact that tells two sessions in the same repository
   apart is the one that must survive where a path's tail can give way. */
function cwdLine(s, cls) {
  const cwd = (s && s.cwd) || "";
  const wt = cwdSplit(cwd);
  const line = el(
    "span",
    cls + (wt ? " worktree" : "") + (cwd ? "" : " unknown")
  );
  line.appendChild(el("span", "cwd-path", cwd ? cwdShort(cwd) : "(daemon cwd)"));
  const branch = (s && s.branch) || "";
  if (branch) {
    const tag = el("span", "cwd-branch", `⎇ ${branch}`);
    tag.title = `git branch ${branch}`;
    line.appendChild(tag);
  }
  line.title = cwd
    ? (wt ? `worktree ${wt.worktree} of ${wt.repo}\n` : "directory\n") + cwd
    : "directory: the daemon's own — none was given when the session was created";
  return line;
}

/* The rail row's copy: a full-width line right under the name, above the
   context gauge. */
function railCwdLine(s) {
  return cwdLine(s, "rail-cwd");
}

/* ------------------------------------------------------------------ */
/* has anyone been here: the rail row's attention line                 */
/* ------------------------------------------------------------------ */
/* Three readings that a rail of twenty sessions otherwise hides completely:
   when a person last LOOKED at this session, when a person last TYPED into
   it, and when the session itself last DID something visible.

   They are three facts, not three views of one, and collapsing them is
   exactly the mistake. A session can be grinding away with nobody watching;
   another can have been open in a tab all afternoon while its agent has not
   moved since lunch; a third was last typed into an hour before it was last
   read. "Active" is a word that answers none of those, and the row the
   operator is hunting for — the one they handed a task to and then forgot —
   is only findable by the gap between the three.

   The dash is not padding. Every row draws all three pairs whether or not
   each has an answer, because the value of a rail is that the same fact sits
   at the same place on every line; a pair that vanished when unknown would
   shift the two beside it and make the column unreadable at the moment it is
   most worth reading.

   No timer runs for this. The session poll rebuilds these rows every couple
   of seconds and the labels are recomputed from the stamps then — which is
   also the whole of what "does not need to be real time" buys: nothing in
   the browser and nothing in the daemon ticks on this line's behalf. */

/* Days, where fmtAge stops at hours. `fmtAge` is shared with the mesh log and
   the owed ledger, where a value is minutes old and an "h" is already the
   long tail; these stamps run to days routinely (that is the point of them),
   and "51h00m" is a number the reader has to do arithmetic on. */
function seenAgo(iso) {
  const t = Date.parse(iso || "");
  if (!Number.isFinite(t)) return null;
  const secs = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (secs < 10) return { secs, text: "now" };
  if (secs >= 86400) return { secs, text: `${Math.floor(secs / 86400)}d` };
  return { secs, text: fmtAge(secs) };
}

/* How stale a reading has to be before the row says so in colour. One step
   for the two pairs that only age: this line is a glance, and a colour
   gradient on three pairs would be states to learn for a row that is trying
   to say one thing. */
const SEEN_COLD = 3600;  // an hour without the reader, or without the agent

/* A second step, which one pair asks for and the other two do not. Half an
   hour since a person last typed here is drawn red rather than amber: the
   row an operator scans this rail for is the session they handed something
   to and then walked away from, and that one is legible at half an hour —
   well before the hour at which "nobody has looked" and "nothing has moved"
   become worth a colour. A pair gets this step only if it is asked for
   (`staleAfter`), so `seen` and `moved` keep the single amber one.

   Both steps read the same field, so they are ordered rather than combined:
   past 30 minutes the typed value is red and stays red, and the hour mark
   passes without changing anything. */
const TYPED_STALE = 1800;

function seenPair(label, iso, title, opts) {
  const pair = el("span", "rail-seen-pair");
  pair.appendChild(el("span", "rail-seen-key", label));
  const live = opts && opts.live;
  const ago = seenAgo(iso);
  // Only where a threshold was asked for, and only against a real reading: a
  // dash means nobody has EVER typed here, which is the ordinary state of
  // every session an agent spawned. Colouring absence red would paint most
  // of the rail and bury the rows this step exists to pick out.
  const after = (opts && opts.staleAfter) || 0;
  const stale = !live && !!ago && !!after && ago.secs >= after;
  const val = el(
    "span",
    "rail-seen-val" + (
      live ? " live"
      : !ago ? " unknown"
      : stale ? " stale"
      : ago.secs >= SEEN_COLD ? " cold"
      : ""),
    live ? "now" : ago ? ago.text : "\u2013"
  );
  pair.appendChild(val);
  pair.title = (ago || live
    ? `${title}\n${live ? "right now" : new Date(Date.parse(iso)).toLocaleString()}`
    : `${title}\nnot recorded — see the line's own note`)
    // Why it is red, on the pair carrying the colour. The line's own note
    // says what the three readings are, which is the wrong place to explain
    // one row's colour: a reader hovering a red number is asking about that
    // number.
    + (stale ? `\nover ${Math.round(after / 60)}m since anyone typed here` : "");
  return pair;
}

/* The line itself. Always drawn, on every row, live or exited: an exited
   session is one of the ones this is most often asked about ("when did I
   last look at the one that died"), and its record keeps both human stamps
   across the restart that retired it. */
function railSeenLine(s) {
  const line = el("span", "rail-seen");
  const watching = Number(s && s.viewers) > 0;
  line.append(
    // Looked at. "now" while a socket is actually open, because a stamp that
    // says "4m" about a terminal being read this second is simply wrong, and
    // the daemon cannot fix that by stamping more often — only by saying
    // that somebody is still there.
    seenPair("seen", s && s.last_visited_at,
             "when a person last had this session open — the web terminal " +
             "or `claunch attach`", { live: watching }),
    // Typed into. A human at a keyboard only: `claunch send-keys` and mesh
    // deliveries type into this session too, and counting those would answer
    // "when was this session last written to", which is a different question
    // and one the row's own busy/idle dot already gestures at.
    seenPair("typed", s && s.last_input_at,
             "when a person last typed here — deliveries and `send-keys` " +
             "do not count",
             { staleAfter: TYPED_STALE }),
    // Moved. Not raw output: claude animates a spinner and a clock while it
    // waits for you, so bytes never stop arriving; this is the last time a
    // row that is NOT an animation changed.
    seenPair("moved", s && s.last_activity_at,
             "when the screen last changed for real — spinners and the " +
             "elapsed-time counter do not count")
  );
  line.title =
    "who has been here: last looked at / last typed into / last moved on " +
    "its own.\n" +
    "A dash means no reading: nobody has visited or typed since this " +
    "session started, and 'moved' is read off the running screen, so a " +
    "daemon restart leaves it blank until the session paints again.\n" +
    "Amber is an hour without the reader or without the agent; red is " +
    "half an hour since anyone typed, and only 'typed' is drawn that way.";
  return line;
}

/* ------------------------------------------------------------------ */
/* session briefing card                                              */
/* ------------------------------------------------------------------ */
/* A row can fold open a card summarising what its session is up to: the
   daemon reads the session's own record and has the configured LLM compress
   it to goal / now / state / progress. Rows are rebuilt on every poll, so
   which cards are open and what each one knows live here, and
   applyBriefingCards() repaints them onto whatever rows exist now — the
   same idempotent shape as the cflow badge above. */
const briefingOpen = new Set();     // session names whose card is folded open
const briefingCache = new Map();    // name -> {phase, data?, error?}
/* Whether the daemon has an llm: block to summarise with — said by the
   session-list response the rail already polls. Optimistic until the first
   poll answers; a daemon too old to say is treated as configured, so the
   worst case is the old behaviour (the card explains), never a feature
   locked away by a missing key. */
let briefingLLM = true;

/* The states the summariser is allowed to claim. blocked and waiting are
   the two where the reader may BE the unblock, so they carry the loud
   colours; anything unrecognised stays neutral rather than borrowing a
   meaning it was not given. */
function briefingStateClass(state) {
  return ["working", "blocked", "waiting", "idle", "done"].includes(state)
    ? state : "other";
}

async function fetchBriefing(name, refresh) {
  // A refresh keeps the old text on screen, dimmed, instead of blanking the
  // card for however long the summariser takes.
  const prev = briefingCache.get(name);
  briefingCache.set(name, { phase: "loading", data: prev && prev.data });
  applyBriefingCards();
  let entry;
  try {
    const resp = await api(
      `/api/sessions/${encodeURIComponent(name)}/briefing${refresh ? "?refresh=1" : ""}`
    );
    const body = await resp.json().catch(() => null);
    if (resp.ok) {
      entry = { phase: "ok", data: body };
    } else if (resp.status === 400) {
      entry = { phase: "unconfigured" };
    } else if (resp.status === 404) {
      entry = { phase: "norecord" };
    } else {
      entry = { phase: "error", error: (body && body.error) || `HTTP ${resp.status}` };
    }
  } catch {
    entry = { phase: "error", error: "request failed" };
  }
  briefingCache.set(name, entry);
  applyBriefingCards();
}

function toggleBriefing(name) {
  if (briefingOpen.has(name)) {
    briefingOpen.delete(name);
  } else {
    briefingOpen.add(name);
    // Reopening shows what we already have (with its age on it); the person
    // who wants a fresh read has the ⟳ for exactly that.
    if (!briefingCache.has(name)) fetchBriefing(name, false);
  }
  applyBriefingCards();
}

function renderBriefingCard(name, entry) {
  const card = el("div", "sess-brief");
  // The row underneath navigates; the card is for reading.
  card.addEventListener("click", (e) => e.stopPropagation());
  const data = entry && entry.data;
  const brief = data && data.briefing;
  const loading = !entry || entry.phase === "loading";
  if (loading && data) card.className += " refreshing";

  const head = el("div", "sess-brief-head");
  if (brief && brief.state) {
    head.appendChild(el(
      "span", `sess-brief-state st-${briefingStateClass(brief.state)}`, brief.state
    ));
  }
  if (data && data.generated_at) {
    const secs = Math.max(0, Math.floor((Date.now() - Date.parse(data.generated_at)) / 1000));
    head.appendChild(el(
      "span", "sess-brief-age",
      `${fmtAge(secs)} ago${data.cached ? " · cached" : ""}`
    ));
  }
  // The card is the row's "what is this session doing"; how much room it has
  // left to keep doing it belongs on the same line.
  const chip = ctxChip(name);
  if (chip) head.appendChild(chip);
  const refresh = el("button", `sess-brief-refresh${loading ? " spinning" : ""}`, "⟳");
  refresh.type = "button";
  refresh.title = "re-summarise now";
  refresh.disabled = loading;
  refresh.addEventListener("click", (e) => {
    e.stopPropagation();
    fetchBriefing(name, true);
  });
  head.appendChild(refresh);
  card.appendChild(head);

  if (loading && !data) {
    card.appendChild(el("div", "sess-brief-note", "summarising…"));
  } else if (!entry || entry.phase === "unconfigured") {
    card.appendChild(el(
      "div", "sess-brief-note",
      "no LLM configured — set the llm section (endpoint, model, api_key) in ~/.claunch.yaml"
    ));
  } else if (entry.phase === "norecord") {
    card.appendChild(el("div", "sess-brief-note", "no session record to summarise"));
  } else if (entry.phase === "error") {
    card.appendChild(el("div", "sess-brief-note error", entry.error || "briefing failed"));
  } else if (brief) {
    // The one line that says what this session is FOR sits at the top of the
    // card too — the row's under-name line shows it always, this is the same
    // fact where the rest of the summary is read.
    if (brief["one-line-job-description"]) {
      card.appendChild(el("div", "sess-brief-one", brief["one-line-job-description"]));
    }
    for (const [key, val] of [
      ["goal", brief.goal], ["now", brief.now], ["progress", brief.progress],
    ]) {
      if (val === undefined || val === null || val === "") continue;
      const row = el("div", "sess-brief-row");
      row.append(el("span", "sess-brief-k", key), el("span", "sess-brief-v", String(val)));
      card.appendChild(row);
    }
  } else if (data && data.raw) {
    // The summariser answered but not in the agreed shape — its words are
    // still the best available summary, so show them as they came.
    card.appendChild(el("pre", "sess-brief-raw", data.raw));
  } else {
    card.appendChild(el("div", "sess-brief-note", "empty briefing"));
  }
  return card;
}

/* Repaint every row's toggle and card from the state above. Safe to call
   any time; refreshSessions calls it after each rebuild. The card is
   rebuilt in place (its listeners live and die with it) — only the toggle
   is reused, and it carries no state beyond its glyph. */
function applyBriefingCards() {
  const list = $("session-list");
  if (!list) return;
  for (const li of list.querySelectorAll("li[data-name]")) {
    const name = li.dataset.name;
    let btn = li.querySelector(".sess-brief-toggle");
    if (!btn) {
      btn = el("button", "sess-brief-toggle");
      btn.type = "button";
      btn.addEventListener("click", (e) => {
        e.stopPropagation();   // the row itself attaches; this button does not
        toggleBriefing(name);
      });
      li.appendChild(btn);
    }
    // The toggle is the feature's one always-visible handle, so it is also
    // where "this exists but is off" is said: without an llm: block the
    // button stays put but inert, and its tooltip points at the config to
    // write — better than a live-looking button opening onto that sentence.
    btn.disabled = !briefingLLM;
    btn.title = briefingLLM
      ? "briefing: goal, current work, state — summarised"
      : "briefing off — set the llm section (endpoint, model, api_key)"
        + " in ~/.claunch.yaml to enable";
    const open = briefingLLM && briefingOpen.has(name);
    btn.textContent = open ? "▾" : "▸";
    syncRowRefresh(li, name);
    // A refresh that just landed paints its one-line onto the row now, so
    // the ⟳ stopping and the text changing are one event — otherwise the
    // line waits for the next /api/sessions poll and the click looks to
    // have done nothing. The poll repaints the same words from the
    // daemon's cache, so this is early, not different.
    const fresh = briefingCache.get(name);
    const one = fresh && fresh.phase === "ok" && fresh.data && fresh.data.briefing
      && fresh.data.briefing["one-line-job-description"];
    const oline = li.querySelector(".rail-brief");
    if (one && oline) oline.textContent = one;
    const old = li.querySelector(".sess-brief");
    if (old) old.remove();
    if (open) li.appendChild(renderBriefingCard(name, briefingCache.get(name)));
  }
  // And the current session's top-bar button + pane, now the rail exists.
  applyBriefingTop();
}

/* The top header's briefing control: the same feature the row ▸ carries,
   sized up so it is discoverable from the strip below the header instead of
   a glyph in a 260px rail. The button owns the current session's card —
   opening it here opens the row's too, and vice versa — and, without an
   llm: block, goes inert with a tooltip that says how to turn it on, a
   louder echo of the disabled row toggle. Safe to call any time; the pane
   and button are hidden by the view system when no terminal is up. */
function applyBriefingTop() {
  const btn = $("term-brief");
  if (!btn) return;
  const open = briefingLLM && currentName && briefingOpen.has(currentName);
  btn.disabled = !briefingLLM || !currentName;
  btn.setAttribute("aria-pressed", open ? "true" : "false");
  btn.title = briefingLLM
    ? "briefing: goal, current work, state — summarised"
    : "briefing off — set the llm section (endpoint, model, api_key)"
      + " in ~/.claunch.yaml to enable";
  btn.textContent = (open ? "▾ " : "▸ ") + "briefing";
  const pane = $("term-brief-pane");
  if (!pane) return;
  pane.innerHTML = "";
  if (open) pane.appendChild(renderBriefingCard(currentName, briefingCache.get(currentName)));
  pane.classList.toggle("hidden", !open);
  // The card and the terminal share one flex column, so opening it, closing
  // it, or its text arriving from the summariser all change the height the
  // grid has to draw into. Without a refit the session keeps the rows it had
  // and the ones the card pushed past the bottom edge are simply gone: the
  // stylesheet clips #terminal (overflow: hidden) and the wheel browses the
  // daemon's history rather than that overflow, so there is nothing to scroll
  // to reach them — the foot of the screen stays missing until some unrelated
  // event happens to refit. Guarded by a signature for the same reason
  // renderTermQueued's strip is: this runs on every 2s poll, and refitting on
  // an unchanged card would resize the session twice a second.
  const sig = open ? `open:${currentName}:${pane.textContent.length}` : "shut";
  if (pane.dataset.sig !== sig) {
    pane.dataset.sig = sig;
    refitSoon(60);
  }
}

/* What a rail row says whether folded or open: the briefing's one-line job
   description, or the recorded opening task until a briefing exists. The
   digest rides the /api/sessions poll (see briefing.digest), so a browser
   refresh repaints it from the daemon's session state instead of asking the
   LLM again. The row's ⟳ refresh sits beside it as the collapsed-state
   handle — same fetch as the card's, but it never opens the card. Built here
   on every row rebuild; the ▸ toggle and the card are applyBriefingCards'. */
function decorateBriefingRow(li, s) {
  const one = (s.briefing && s.briefing.one_line) || s.task || "";
  let oline = li.querySelector(".rail-brief");
  if (one) {
    if (!oline) {
      oline = el("div", "rail-brief");
      oline.title = "one-line job description — ▸ opens the full briefing";
      li.appendChild(oline);
    }
    oline.textContent = one;
  } else if (oline) {
    oline.remove();
  }
  let refresh = li.querySelector(".sess-brief-rowref");
  if (!refresh) {
    refresh = el("button", "sess-brief-rowref");
    refresh.type = "button";
    refresh.addEventListener("click", (e) => {
      e.stopPropagation();   // the row navigates; this button only refreshes
      refreshBriefingRow(s.name);
    });
    li.appendChild(refresh);
  }
  refresh.textContent = "⟳";
  syncRowRefresh(li, s.name);
}

/* The row ⟳'s face, read off the briefing cache: spinning and inert while a
   fetch is in flight (the only sign the row gives that the click landed —
   the card is closed, so nothing else moves), red with the reason on its
   tooltip after a failed one, plain otherwise. Called from every rebuild
   and every cache change, since the row is torn down by the poll and the
   phase is set by the fetch, and both must repaint the same button. */
function syncRowRefresh(li, name) {
  const refresh = li.querySelector(".sess-brief-rowref");
  if (!refresh) return;
  const entry = briefingCache.get(name);
  const loading = !!entry && entry.phase === "loading";
  const failed = briefingLLM && !!entry && entry.phase === "error";
  refresh.classList.toggle("spinning", loading);
  refresh.classList.toggle("failed", failed);
  refresh.disabled = !briefingLLM || loading;
  refresh.title = !briefingLLM
    ? "briefing off — set the llm section (endpoint, model, api_key)"
      + " in ~/.claunch.yaml to enable"
    : loading ? "summarising…"
    : failed ? `briefing failed: ${entry.error || "unknown error"} — click to retry`
    : "refresh the summary without opening it";
}

/* The collapsed row's refresh: re-ask the daemon (bypassing its cache) and
   leave the card folded — the one-line and state repaint on the next poll,
   which is what "without opening" means. */
function refreshBriefingRow(name) {
  fetchBriefing(name, true);
}

/* The detail panel's copy of the summary: the same card the ▸ toggles open,
   drawn under the session's facts so the panel says what the session is
   DOING instead of only what it is. Fetches on first open — the 2s poll
   repaints from the cache, and the card's own ⟳ still refreshes it. Without
   an llm: block the drawer is a static pointer at the config to write, so
   someone who just set the section sees it acknowledged. */
function sessBriefSection(name) {
  const box = el("div", "sess-brief-section");
  box.appendChild(el("h3", null, "Briefing"));
  if (!briefingLLM) {
    box.appendChild(el(
      "p", "sess-brief-note",
      "briefing off — set the llm section (endpoint, model, api_key)"
        + " in ~/.claunch.yaml to enable"
    ));
    return box;
  }
  if (!briefingCache.has(name)) fetchBriefing(name, false);
  box.appendChild(renderBriefingCard(name, briefingCache.get(name)));
  return box;
}

/* ------------------------------------------------------------------ */
/* cflow workflow monitoring                                          */
/* ------------------------------------------------------------------ */
function wfDotClass(status, run) {
  if (status === "step" || status === "select" || status === "reported") return "wf-running";
  // Delegated: stopped, but not on anything the operator has to do. Its own
  // colour, because painting it the same amber as a gate would grow a queue
  // of things that look like work and are not. An ask that reached nobody is
  // not one of those — it IS the operator's — so it takes the gate's amber.
  if (status === "waiting_answer") {
    return answerFellToUs(run) ? "wf-waiting" : "wf-delegated";
  }
  if (status === "waiting_approval" || status === "waiting_selection" ||
      status === "waiting_checklist" || status === "waiting_goto" ||
      status === "report_required") return "wf-waiting";
  // A held choice: the agent decided, the workflow paces it — nobody's move.
  if (status === "waiting_window") return "wf-delegated";
  if (status === "done") return "wf-done";
  if (status === "error" || status === "aborted") return "wf-error";
  return "wf-running";
}

/* The mark that stands in front of a workflow run's name — a glyph, and
   deliberately COLOURLESS.

   It used to be a coloured dot, and that is the bug: the rail draws a run's
   mark one line under the session's own liveness dot, and the two palettes
   are the same palette — a finished run and an idle session are both
   #3fb950, a running step and a starting session are both #58a6ff. A reader
   scanning the rail saw two green circles and read the second as another
   session. Colour cannot say WHOSE state it is showing, only which.

   So the run's mark gives colour up entirely: it inherits the line's colour
   (grey, or the amber the whole line takes when the run is the reader's
   move), which leaves the saturated dots on this rail meaning exactly one
   thing — a session. Its own state it says by SHAPE, which nothing else in
   the rail speaks in:

     ▸  running     — moving
     ‖  held        — stopped by the clock: a paced option waiting for its
                      window, which the daemon opens. Nobody can hurry it,
                      so it must not look like either kind of "waiting"
     ◆  your move   — stopped, filled: it is on the person reading this
     ◇  with a peer — stopped, hollow: it is on somebody else
     ✓  done
     ✕  error/aborted

   ▸/‖ is the play/pause pair, and ◆/◇ is the filled/hollow one; the latter
   matters because those two are the same status word ("waiting") and
   opposite meanings for the reader. Badges are untouched: `.badge.wf-*`
   keeps its colour, being a labelled pill that no dot sits near. */
const WF_GLYPH = {
  running: "▸",
  held: "‖",
  yours: "◆",
  peer: "◇",
  done: "✓",
  error: "✕",
};

/* Which SHAPE a run wears — deliberately its own vocabulary, not a re-use of
   the colour classes above.

   The two axes stopped agreeing the moment colour left. `wfDotClass` answers
   "which colour does a badge paint" and hands `waiting_window` the same
   `wf-delegated` as an ask sitting with a peer, because neither is the
   operator's move and one colour said that much. Shape can afford the
   distinction the colour could not: a peer can be chased and a clock cannot.
   Keeping them separate also means a new shape costs nothing — the mark has
   no per-state rule to add, having no colour to declare. (Agreed with s107,
   who draws the same state on the workflow diagram: the word for it is
   "held", the thing that releases it is the daemon.) */
function wfMarkState(status, run) {
  if (status === "waiting_window") return "held";
  if (status === "waiting_answer") return answerFellToUs(run) ? "yours" : "peer";
  if (status === "waiting_approval" || status === "waiting_selection" ||
      status === "waiting_goto" || status === "report_required") return "yours";
  if (status === "done") return "done";
  if (status === "error" || status === "aborted") return "error";
  return "running";
}

/* [class, glyph] for a run's mark. Two values rather than a built element:
   the three places that draw one build their nodes in their own idiom, and
   one of them goes through `el`. */
function wfMark(status, run) {
  const state = wfMarkState(status, run);
  return [`wf-mark wf-mark-${state}`, WF_GLYPH[state]];
}

/* Who an open ask is with, in a few words. */
function askWho(ask) {
  const asked = (ask && ask.asked) || [];
  if (!asked.length) return "nobody — it fell to you";
  return asked.map((e) => e.handle || e.kind).join(", ");
}

/* A `waiting_answer` whose question reached NOBODY.

   The status word says the run is with another agent; the payload says
   otherwise — `asked` is empty, or there is no ask at all, which is
   exactly what forcing the run onto an asking step with `goto` leaves
   behind (goto moves the step deliberately without delivering it). A
   consumer that reads the status alone then tells the reader some peer
   has it, and the run stands for ever: no reminder is due, no gate event
   fires, and the panel offers nothing that would clear it.

   So the discriminator lives here, once, and the consumers ask it rather
   than the status. The daemon already treats this case as the human's —
   engine.approve() handles an ask that reached nobody — so the ordinary
   gate press is what clears it. */
function answerFellToUs(r) {
  if (!r || r.status !== "waiting_answer") return false;
  return !r.ask || !((r.ask.asked || []).length);
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

/* A checklist item's three states, as a glyph. `?` is the one that has to be
   distinguishable: an item nobody could measure is a different fact from one
   that measured false, and the two send a reader to different places. */
function checklistMark(ok) {
  if (ok === true) return "\u2713";
  if (ok === false) return "\u00d7";
  return "?";
}

function checklistClass(ok) {
  if (ok === true) return "ok";
  if (ok === false) return "no";
  return "unknown";
}

/* The gate as a list of conditions, which is the whole point of the
   `checklist:` step type reaching a screen. These decisions -- did the parent
   merge this branch, did the live daemon pick the merge up -- used to be
   carried by the step's prose, so the only account of which parts were true
   was the driving agent's, and a person watching had no way to check it. */
function checklistLines(checklist) {
  const out = [];
  if (!checklist) return out;
  const head = cflowLine(
    `checklist ${checklist.passed}/${checklist.total} true` +
    (checklist.all_true && !checklist.report_filed
      ? " \u2014 waiting on this step's report"
      : checklist.all_true ? " \u2014 the daemon is moving the run" : "")
  );
  out.push(head);
  for (const item of checklist.items || []) {
    const line = cflowLine(
      `${checklistMark(item.ok)} ${item.id}: ${item.describe}` +
      (item.exit_code === null || item.exit_code === undefined
        ? (item.measured_at ? " (could not measure)" : " (not measured yet)")
        : ` (exit ${item.exit_code})`),
      `checklist-item ${checklistClass(item.ok)}`
    );
    out.push(line);
  }
  if (!checklist.all_true) {
    out.push(cflowLine(
      `moves to '${checklist.then}' once every item is true \u2014 nobody ` +
      `advances this step by hand`,
      "dim"
    ));
  }
  return out;
}

function cflowHint(cmd) {
  const el = document.createElement("code");
  el.className = "cflow-hint";
  el.textContent = cmd;
  return el;
}

/* The reminder clock's machine defaults, editable in place. Rendered once
   (guarded by dataset.ready) so the flows list's rebuild never wipes a
   half-typed interval; the daemon re-reads these on every clock tick, so a
   save applies without a restart. */
async function renderReminderDefaults() {
  const box = $("cflow-reminder-defaults");
  if (!box || box.dataset.ready) return;
  box.dataset.ready = "1";
  let defs = null;
  try {
    const resp = await api("/api/cflow/reminder");
    if (resp.ok) defs = ((await resp.json()) || {}).defaults;
  } catch { /* auth overlay is up */ }
  if (!defs) {
    delete box.dataset.ready; // try again on the next visit
    return;
  }
  box.innerHTML = "";
  const head = el("label", "pol-head");
  const on = document.createElement("input");
  on.type = "checkbox";
  on.checked = !!defs.enabled;
  head.appendChild(on);
  head.appendChild(el("span", null, "step reminders — machine default"));
  head.title = "while a run sits on the same step, the daemon re-types that " +
    "step's instructions into its session at this interval";
  box.appendChild(head);
  const row = el("div", "pol-row");
  row.appendChild(el("span", "pol-label", "every"));
  const iv = document.createElement("input");
  iv.type = "number";
  iv.min = "30";
  iv.step = "10";
  iv.className = "pol-num";
  iv.value = String(Math.round(defs.interval || 600));
  row.appendChild(iv);
  row.appendChild(el("span", "pol-label", "s without progress"));
  const save = el("button", "wf-btn", "Save defaults");
  save.addEventListener("click", async () => {
    try {
      const resp = await api("/api/cflow/reminder", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: on.checked, interval: +iv.value }),
      });
      const doc = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        alert(doc.error || `HTTP ${resp.status}`);
        return;
      }
      document.activeElement?.blur?.();
    } catch { /* auth overlay is up */ }
  });
  row.appendChild(save);
  box.appendChild(row);
  box.appendChild(el(
    "p", "wf-note",
    "applies to every run without its own setting, within one daemon tick " +
    "(~15s); each run page can override it"
  ));
}

/* The stall ping's machine settings, editable in place — the reminder box's
   complement. That clock re-aims a session that is WORKING; this one wakes a
   session that has STOPPED at a step no gate is holding, which is the one
   position nothing else watches. Off by default (a run can sit at an
   actionable step legitimately, waiting for a person to hand it a goal), so
   this box is where it gets turned on. Rendered once, same guard as above:
   the flows list rebuilds on every poll and would otherwise wipe a
   half-typed message. */
async function renderStallPingDefaults() {
  const box = $("cflow-ping-defaults");
  if (!box || box.dataset.ready) return;
  box.dataset.ready = "1";
  let defs = null;
  try {
    const resp = await api("/api/cflow/ping");
    if (resp.ok) defs = ((await resp.json()) || {}).defaults;
  } catch { /* auth overlay is up */ }
  if (!defs) {
    delete box.dataset.ready; // try again on the next visit
    return;
  }
  box.innerHTML = "";
  const head = el("label", "pol-head");
  const on = document.createElement("input");
  on.type = "checkbox";
  on.checked = !!defs.enabled;
  head.appendChild(on);
  head.appendChild(el("span", null, "stall pings — stopped sessions"));
  head.title = "when a run sits at a step that is its agent's own to move " +
    "— no approval, no selection, no delegated answer holding it — and the " +
    "session has stopped working, the daemon pings it with the message below";
  box.appendChild(head);
  const row = el("div", "pol-row");
  row.appendChild(el("span", "pol-label", "after"));
  const iv = document.createElement("input");
  iv.type = "number";
  iv.min = String(Math.round(defs.min_interval || 60));
  iv.step = "30";
  iv.className = "pol-num";
  iv.value = String(Math.round(defs.interval || 900));
  row.appendChild(iv);
  row.appendChild(el("span", "pol-label", "s stopped"));
  box.appendChild(row);
  const msgRow = el("div", "pol-row");
  const msg = document.createElement("textarea");
  msg.className = "pol-msg";
  msg.rows = 3;
  msg.value = defs.message || "";
  msg.placeholder = "ping message (blank restores the default)";
  msgRow.appendChild(msg);
  box.appendChild(msgRow);
  const saveRow = el("div", "pol-row");
  const save = el("button", "wf-btn", "Save ping settings");
  save.addEventListener("click", async () => {
    try {
      const resp = await api("/api/cflow/ping", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          enabled: on.checked, interval: +iv.value, message: msg.value,
        }),
      });
      const doc = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        alert(doc.error || `HTTP ${resp.status}`);
        return;
      }
      // A blank message clears back to the packaged default, so show what
      // the daemon actually kept rather than the empty box that was sent.
      if (doc.defaults) msg.value = doc.defaults.message || "";
      document.activeElement?.blur?.();
    } catch { /* auth overlay is up */ }
  });
  saveRow.appendChild(save);
  box.appendChild(saveRow);
  box.appendChild(el(
    "p", "wf-note",
    "the message is typed in as a fresh turn, so it wakes an agent that had " +
    "stopped; a run parked on an approval or a delegated answer is never " +
    "pinged. Applies within one daemon tick (~15s)"
  ));
}

async function refreshCflow() {
  renderReminderDefaults(); // once; guarded inside
  renderStallPingDefaults(); // once; guarded inside
  let data;
  try {
    const resp = await api("/api/cflow");
    data = await resp.json();
  } catch {
    return;
  }
  const runs = data.runs || [];
  cflowCache = runs;
  applyCflowBadges();  // the rail rows may have painted before this cache filled
  applyRailQuiet();    // one of its two flags is read off this very cache
  renderTermTimer();   // the attached session's own header chip
  if (currentPage === "home") renderHome();
  // Everything above is what feeds the rail and the header: badges on rows,
  // the attached session's countdown, the home card's count. What follows
  // rebuilds the Flows page's
  // whole list from scratch — one card per run, a hundred of them on a
  // working machine — and this runs on the two-second tick from whatever page
  // you are on. Off the flows page there is nobody to see it, and the tick
  // was spending its main-thread budget building a list behind a terminal.
  // route() calls refreshCflow on the way in, so arriving still finds it
  // drawn from the poll that just landed.
  if (currentPage !== "flows") return;
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
    const [markCls, markGlyph] = wfMark(r.status, r);
    const mark = document.createElement("span");
    mark.className = markCls;
    mark.textContent = markGlyph;
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
        ? (answerFellToUs(r) ? "asked of nobody" : `with ${askWho(r.ask)}`)
        : r.status === "waiting_goto"
        ? "step change asked"
        : r.status;
    head.append(mark, name, st);
    li.appendChild(head);

    if (r.step_id) {
      const visit = r.visit > 1 ? ` · visit ${r.visit}` : "";
      const round = r.round ? ` · round ${r.round}` : r.recur ? " · recurs" : "";
      li.appendChild(cflowLine(
        `step: ${r.title || r.step_id}${visit}${round} · ${r.steps_completed ?? 0} done`
      ));
    }
    // A slot with no run but a request filed against it is listed too — that
    // waiting period is exactly when a human wants to see something.
    if (r.pending_start) {
      li.appendChild(cflowLine(
        r.pending_start.by === "recur"
          ? `recurs — next round requested` +
            (r.pending_start.round ? ` (round ${r.pending_start.round})` : "")
          : `start requested: ${r.pending_start.name || r.pending_start.workflow}`,
        "report"
      ));
    }

    // Latest step reports: the agent's own account of each finished step
    // (plus the current step's filed-but-not-advanced report, if any).
    const reports = (r.reports || []).slice(-3);
    for (const rep of reports) {
      const line = cflowLine(`${rep.step}: ${mdPlain(rep.summary)}`, "report");
      if (rep.details) line.title = mdText(rep.details);
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
      if (answerFellToUs(r)) {
        // Nobody holds this one, so the rail names the press that clears
        // it — the same hint a gate gets, because that is what it is.
        li.appendChild(cflowLine("put to nobody — it is yours to approve"));
        li.appendChild(cflowHint("claunch cflow approve"));
      } else {
        li.appendChild(cflowLine(`waiting on ${askWho(r.ask)} to decide`));
        if (r.ask && r.ask.deadline) {
          li.appendChild(cflowLine(`moves on after ${r.ask.deadline}`));
        }
      }
    } else if (r.status === "waiting_approval") {
      if (r.reason === "declined" && r.declined) {
        li.appendChild(cflowLine(
          `${r.declined.by} declined — ` +
          (mdPlain(r.declined.reason) || "no reason given")
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
    } else if (r.status === "waiting_checklist") {
      for (const line of checklistLines(r.checklist)) li.appendChild(line);
    } else if (r.status === "waiting_goto") {
      const gr = r.goto_request || {};
      li.appendChild(cflowLine(
        `asked to move '${gr.from || r.step_id}' → '${gr.step}' — ` +
        (mdPlain(gr.reason) || "no reason given")
      ));
      li.appendChild(cflowHint("claunch cflow goto --approve | --deny"));
    } else if (r.status === "waiting_window") {
      li.appendChild(cflowLine(
        `chose '${r.option}' — held until ${fmtOpensAt(r.opens_at)} ` +
        `(at most every ${r.interval}s); the daemon releases it`
      ));
      li.appendChild(cflowHint(`claunch cflow select ${r.option}`));
    } else if (r.status === "error") {
      li.appendChild(cflowLine(r.error || "error", "error"));
    }

    list.appendChild(li);
  }
}

let profileDetails = {};
let newProfileOptions = [];
let newHarnessFor = null;
let newBorrowFor = null;
let newBorrowSeq = 0;

/* A managed session stores PROFILE:HARNESS so restore does not reinterpret a
   later default change. The UI presents the same pair once, as PROFILE/HARNESS.
   A mismatch is kept visible instead of silently discarding either value. */
function profileHarnessLabel(profile, harness) {
  const selector = String(profile || "").trim();
  const running = String(harness || "").trim();
  if (!selector) return running;
  const parts = selector.split(":");
  if (parts.length === 2 && (!running || parts[1] === running)) {
    return `${parts[0]}/${parts[1]}`;
  }
  return running ? `${selector}/${running}` : selector;
}

function baseProfileName(selector) {
  return String(selector || "").split(":", 1)[0];
}
let harnessDetails = {};

/* Borrow is a property of the selected harness's auth contract, not of its
   name. Old daemons did not publish borrow_allowed, so Claude remains the
   compatibility fallback while a new daemon also opens API-key harnesses. */
function profileBorrowCapability(detail, harnessName) {
  if (detail && typeof detail.borrow_allowed === "boolean") {
    return {
      allowed: detail.borrow_allowed,
      mode: detail.borrow_mode || (detail.borrow_allowed ? "token" : "none"),
    };
  }
  return {
    allowed: harnessName === "claude",
    mode: harnessName === "claude" ? "provider-token" : "none",
  };
}

/* The disabled Borrow row still needs to say which authentication will be
   used. OAuth harnesses do not accept a `borrow` value: selecting the profile
   selects its namespaced login, so the empty value is that profile's auth
   rather than the parent's. Keep the qualified PROFILE/HARNESS in the label
   because two selectors with one base profile can run different programs. */
function profileOwnAuthLabel(selector, harnessName) {
  const base = baseProfileName(selector);
  const identity = base && harnessName
    ? `${base}/${harnessName}` : (base || harnessName || "selected profile");
  const detail = (typeof harnessDetails !== "undefined"
    ? harnessDetails[harnessName] : null) || {};
  if (detail.auth === "oauth") {
    return `(${identity} profile's own OAuth login)`;
  }
  if (detail.auth === "none") return `(${identity} uses no authentication)`;
  return `(${identity} profile authentication)`;
}

function profileHarnessName(selector, parent) {
  const detail = selector
    ? profileDetails[selector] || profileDetails[baseProfileName(selector)]
    : null;
  return detail ? (detail.harness || "") : ((parent || {}).harness || "");
}

/* New Session and Spawn share the daemon's qualified selector contract. The
   page form keeps its controls as native form fields, so this adapter gives
   the split-profile helpers the same small ui bag the Spawn modal uses. */
function newProfileUi(f) {
  return {
    profile: f.profile,
    harness: f.harness,
    parentSess: spawnParent() || {},
    _profileOptions: newProfileOptions,
  };
}

function newProfileSelector(f) {
  return spawnProfileSelector(newProfileUi(f));
}

function newProfileOverride(f) {
  return spawnProfileOverride(newProfileUi(f));
}

function newProfileDetail(f, selector = "") {
  selector = selector || newProfileSelector(f);
  return profileDetails[selector] ||
    profileDetails[baseProfileName(selector)] || null;
}

function newProfileHarnessName(f, selector = "") {
  selector = selector || newProfileSelector(f);
  const named = String(selector).split(":", 2)[1] || "";
  const detail = newProfileDetail(f, selector);
  return named || (detail && detail.harness) ||
    (spawnParent() || {}).harness || "";
}

/* ---- Codex's harness-specific runtime panel ----------------------------
   Approval and file isolation are independent Codex settings.  They are not
   rendered from generic capability flags: a different harness that happens
   to declare a permission toggle still needs its own vocabulary and layout.
   The argv remains declaration-driven so a configured Codex command and the
   daemon continue to agree on the exact native flags. */
function argvHasGroup(argv, group) {
  argv = (argv || []).map(String);
  group = (group || []).map(String);
  if (!group.length) return false;
  return argv.some((_, i) =>
    group.every((arg, j) => argv[i + j] === arg));
}

function codexModeGroups(capabilities) {
  capabilities = capabilities || {};
  const declared = (key, fallback) => {
    const group = (capabilities[key] || []).map(String);
    return group.length ? group : fallback.slice();
  };
  const groups = {
    bypass: declared("mode_conflict_args",
      ["--dangerously-bypass-approvals-and-sandbox"]),
    skip: declared("skip_permissions_args", ["--approval-mode", "full-auto"]),
    sandboxOn: declared("full_access_off_args", ["--sandbox", "workspace-write"]),
    sandboxOff: declared("full_access_args", ["--sandbox", "danger-full-access"]),
  };
  groups.all = [];
  for (const group of [groups.bypass, groups.skip,
                       groups.sandboxOn, groups.sandboxOff]) {
    if (!groups.all.some((seen) => JSON.stringify(seen) === JSON.stringify(group))) {
      groups.all.push(group);
    }
  }
  return groups;
}

function withoutArgGroups(argv, groups) {
  const out = (argv || []).filter((arg) => arg !== "--").map(String);
  const usable = (groups || []).filter((group) => group && group.length);
  let i = 0;
  while (i < out.length) {
    const found = usable.find((group) =>
      group.every((arg, j) => out[i + j] === arg));
    if (found) out.splice(i, found.length);
    else i++;
  }
  return out;
}

function codexRuntimeState(argv, capabilities) {
  const own = (argv || []).filter((arg) => arg !== "--").map(String);
  const groups = codexModeGroups(capabilities);
  const managed = groups.all.some((group) => argvHasGroup(own, group));
  const declared = ((capabilities && capabilities.args) || []).map(String);
  const effective = managed
    ? own
    : [...(declared.length ? declared : groups.bypass), ...own];
  const skipping = argvHasGroup(effective, groups.skip);
  return {
    yolo: argvHasGroup(effective, groups.bypass) || skipping,
    sandbox: argvHasGroup(effective, groups.sandboxOn) ||
      (skipping && !argvHasGroup(effective, groups.sandboxOff)),
  };
}

function codexRuntimeArgs(argv, capabilities, yolo, sandbox) {
  const groups = codexModeGroups(capabilities);
  const out = withoutArgGroups(argv, groups.all);
  if (yolo && !sandbox) return [...groups.bypass, ...out];
  const mode = [];
  if (yolo) mode.push(...groups.skip);
  mode.push(...(sandbox ? groups.sandboxOn : groups.sandboxOff));
  return [...mode, ...out];
}

function codexRuntimeText(yolo, sandbox) {
  if (yolo && !sandbox) {
    return "YOLO enabled · sandbox disabled · approvals and sandbox are bypassed";
  }
  return `${yolo ? "YOLO enabled · approval prompts disabled" :
    "YOLO disabled · approval prompts enabled"} · ` +
    `${sandbox ? "workspace-write sandbox enabled" : "sandbox disabled"}`;
}

function refillNewHarnessOptions(f, want, force = false) {
  const parent = spawnParent() || {};
  const signature = newProfileOptions.map((item) =>
    `${item.value}:${item.harness_available === false ? 0 : 1}`).join("|");
  const key = `${f.profile.value}|${parent.profile || ""}|` +
    `${parent.harness || ""}|${signature}`;
  if (!force && key === newHarnessFor) return;
  newHarnessFor = key;
  refillSpawnHarnesses(newProfileUi(f), want);
}

async function readBorrowOptions(selector) {
  if (!selector) return { options: [], capability: { allowed: false } };
  const resp = await api(
    `/api/borrow-options?profile=${encodeURIComponent(selector)}`
  );
  const doc = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(doc.error || `HTTP ${resp.status}`);
  return doc;
}

function fillValidatedBorrow(select, doc, ownLabel, current, omitName = "", ownName = "") {
  select.innerHTML = "";
  const own = document.createElement("option");
  own.textContent = ownLabel;
  own.value = "";
  select.appendChild(own);
  const lenders = (doc && doc.options) || [];
  if (ownName) {
    // "The selected profile's own token" is an answer of its own on a form
    // that names a parent, where the empty head option means "authenticate
    // as the parent does" -- including a parent's own borrow. Its payload
    // is the profile's own name, an explicit lender replacing inherited
    // auth, which is why the API docstring lists the base profile among
    // the lenders; the lender row that used to ask for that same name is
    // folded into this head option, with the verdict and reason intact.
    const base = lenders.find((item) => item.name === ownName);
    const opt = document.createElement("option");
    opt.textContent = `${ownName}'s own token`;
    opt.value = ownName;
    opt.disabled = !!(base && !base.selectable);
    opt.title = (base && base.message) || "";
    select.appendChild(opt);
  }
  for (const item of lenders) {
    if (item.name === omitName || (ownName && item.name === ownName)) continue;
    const opt = document.createElement("option");
    opt.textContent = item.label || item.name;
    opt.value = item.name;
    opt.disabled = !item.selectable;
    opt.title = item.message || "";
    select.appendChild(opt);
  }
  const kept = [...select.options].find((o) => o.value === current);
  select.value = kept && !kept.disabled ? current : "";
  select.title = "";
}

async function syncNewBorrowOptions(force = false) {
  const f = $("new-session");
  if (!f || !f.borrow || !f.profile || !f.harness) return;
  const parent = spawnParent();
  const selector = newProfileSelector(f);
  const harnessName = newProfileHarnessName(f, selector);
  const detail = newProfileDetail(f, selector);
  const borrowCap = profileBorrowCapability(
    detail, harnessName
  );
  const ownLabel = !borrowCap.allowed
    ? profileOwnAuthLabel(selector, harnessName)
    : parent
      ? `(as ${parent.name} authenticates)`
      : "(this profile's own token)";
  // On a borrow-capable parented form the empty answer keeps the parent's
  // auth arrangement, so the selected profile's OWN token needs a head option
  // of its own. OAuth harnesses have no lender value: their one empty answer
  // already names the selected profile login above.
  const ownName = parent && borrowCap.allowed ? baseProfileName(selector) : "";
  const omitName = parent ? "" : baseProfileName(selector);
  const key = `${selector}|${ownLabel}`;
  if (!force && key === newBorrowFor) return;
  newBorrowFor = key;
  const seq = ++newBorrowSeq;
  const current = f.borrow.value;
  f.borrow._validationPending = true;
  f.borrow._validationError = "";
  // Clear the previous harness's lenders before waiting for the new policy
  // answer. A quick submit during the request can then only mean own auth.
  fillValidatedBorrow(f.borrow, { options: [] }, ownLabel, "", omitName, ownName);
  f.borrow.disabled = true;
  try {
    const doc = await readBorrowOptions(selector);
    if (seq !== newBorrowSeq || key !== newBorrowFor) return;
    fillValidatedBorrow(f.borrow, doc, ownLabel, current, omitName, ownName);
  } catch (e) {
    if (seq !== newBorrowSeq || key !== newBorrowFor) return;
    fillValidatedBorrow(f.borrow, { options: [] }, ownLabel, "", omitName, ownName);
    f.borrow._validationError =
      `borrow validation unavailable: ${e.message || e}`;
    f.borrow.title = f.borrow._validationError;
  }
  f.borrow._validationPending = false;
  if (spawnParent()) syncSpawnMode();
  else syncForkAvailability();
}

async function refreshProfiles() {
  try {
    const resp = await api("/api/profiles");
    const data = await resp.json();
    const f = $("new-session");
    const select = f.profile;
    const previousProfile = select.value || "";
    const previousSelector = newProfileSelector(f);
    const previousHarness = f.harness.value ||
      (String(previousSelector).split(":", 2)[1] || "");
    profileDetails = {};
    for (const item of data.profile_details || []) {
      if (item && item.name) profileDetails[item.name] = item;
    }
    const optionDefs = data.profile_options ||
      (data.profile_selectors || data.profiles || []).map(
        (name) => ({ value: name, label: name })
      );
    newProfileOptions = normalizeSpawnProfileOptions(optionDefs);
    const grouped = new Map();
    for (const item of newProfileOptions) {
      const options = grouped.get(item.profile) || [];
      options.push(item);
      grouped.set(item.profile, options);
    }
    const pairs = [...grouped].map(([profile, options]) => [
      profile, profile,
      options.every((item) => item.harness_available === false),
    ]);
    const child = !!spawnParent();
    let wantedProfile = child && !previousProfile
      ? "" : baseProfileName(previousSelector);
    if (!wantedProfile && !child) {
      const first = pairs.find((item) => !item[2]) || pairs[0];
      wantedProfile = first ? first[0] : "";
    }
    fillSpawnSelect(
      select, pairs,
      child ? "(inherit the parent's profile)" : null,
      wantedProfile
    );
    newHarnessFor = null;
    refillNewHarnessOptions(
      f, child && !select.value ? "" : previousHarness, true
    );
    await syncNewBorrowOptions(true);
    syncForkAvailability();
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

/* The same guard for the two create-form pickers built out of the session
   list, which the rail's own two-second poll rebuilds. Same symptom exactly:
   a user who opens either dropdown and reads it for two seconds has it shut
   in their face, and the longer the list the more certain they are still
   reading when the tick lands.

   These cannot be signed the way the workspace list is, by stringifying what
   the poll returned. A session record carries a pid, a context size and a
   last-activity clock that move on their own, so the whole record differs on
   nearly every tick and a signature over it would never hold — the guard
   would be there and do nothing. So each of these signs exactly the fields
   its own rebuild reads, and the rebuild is fed from the same list it
   signed, which is what keeps the two from drifting apart. */
let resumeRendered = null;
let parentsRendered = null;

/* Last workspace list the poll saw, for the manage page (#/workspaces). */
let workspacesCache = [];

/* Last cflow run list the poll saw, for the home dashboard. */
let cflowCache = [];

/* The declared harnesses. Fetched once: the set is declared in YAML and
   changes when someone edits config or installs a program, neither of which
   happens mid-session — a reload is the honest way to pick those up. */
async function refreshHarnesses() {
  try {
    const resp = await api("/api/harnesses");
    const data = await resp.json();
    harnessDetails = {};
    for (const item of data.harnesses || []) {
      if (item && item.name) harnessDetails[item.name] = item;
    }
  } catch { /* an older daemon leaves the capability rows hidden */ }
  syncForkAvailability();  // role/resume/fork only apply to the claude harness
  syncSpawnMode();
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
  // The registry is polled, so this row is rebuilt behind the form's back:
  // whatever the spawn mode had done to it (the "inherit" wording) goes with
  // the old options and has to be said again.
  syncSpawnMode();
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
   Fed by the session poll, so it is rebuilt only when the offered
   conversations actually differ (see resumeRendered) and the current choice
   is preserved by hand across that rebuild. */
function refreshResumeChoices() {
  const select = document.querySelector("#new-session select[name=resume]");
  // Name and status ARE the option — its value and its label — so they are
  // the whole of the signature. A session whose pid or context size moved is
  // the same row and must not cost the user their open dropdown.
  const offered = sessionsCache
    .filter((s) => s.conversation_id)  // nothing pinned to resume
    .map((s) => [s.name, s.status]);
  const signature = JSON.stringify(offered);
  if (signature === resumeRendered) return;
  resumeRendered = signature;

  const previous = select.value;
  select.innerHTML = "";
  select.appendChild(new Option("(new conversation)", ""));
  select.appendChild(new Option("pick in claude's picker (--resume)", PICKER));
  for (const [name, status] of offered) {
    select.appendChild(new Option(`${name} — ${status}`, name));
  }
  // A session that vanished (cleared, renamed) takes its option with it;
  // falling back to "(new conversation)" beats silently resuming a stranger.
  select.value = [...select.options].some((o) => o.value === previous)
    ? previous
    : "";
  syncForkAvailability();
}

function seedNewCodexRuntime(f, capabilities, argv, key) {
  if (!f.codex_yolo || !f.codex_sandbox || f._codexRuntimeFor === key) return;
  const state = codexRuntimeState(argv, capabilities);
  f.codex_yolo.checked = state.yolo;
  f.codex_sandbox.checked = state.sandbox;
  f._codexRuntimeFor = key;
  f._codexRuntimeOriginal = state;
  f._codexRuntimeBaseArgs = (argv || []).slice();
}

function renderNewCodexRuntime(f, harnessName, capabilities, parent = null) {
  const panel = $("new-codex-runtime");
  if (!panel || !f.codex_yolo || !f.codex_sandbox) return;
  const codex = harnessName === "codex";
  panel.classList.toggle("hidden", !codex);
  if (!codex) return;
  if (!parent) {
    seedNewCodexRuntime(
      f, capabilities, [], `new:${newProfileSelector(f)}:${harnessName}`
    );
    f.codex_yolo.disabled = false;
    f.codex_sandbox.disabled = false;
  }
  const hint = $("new-codex-runtime-hint");
  if (hint) {
    const inherited = parent && f.codex_yolo.disabled
      ? (harnessName === parent.harness
        ? ` · inherited from ${parent.name} (spawn.allow_args)`
        : " · Codex default (spawn.allow_args to override)")
      : "";
    hint.textContent = codexRuntimeText(
      f.codex_yolo.checked, f.codex_sandbox.checked
    ) + inherited;
  }
}

function renderNewClaudeRuntime(f, harnessName, capabilities, parent = null) {
  const panel = $("new-claude-runtime");
  if (!panel || !f.skip_permissions) return;
  const claude = harnessName === "claude";
  panel.classList.toggle("hidden", !claude);
  if (!claude) return;

  const permissionArgs = (capabilities.skip_permissions_args || []).map(String);
  if (parent) {
    const base = harnessName === parent.harness ? (parent.args || []) : [];
    const key = `child:${parent.name}:${newProfileSelector(f)}:${harnessName}`;
    if (f._claudeRuntimeFor !== key) {
      f.skip_permissions.checked = argvHasGroup(base, permissionArgs);
      f._claudeRuntimeFor = key;
      f._claudeRuntimeOriginal = f.skip_permissions.checked;
      f._claudeRuntimeBaseArgs = base.slice();
    }
  } else {
    f.skip_permissions.disabled = false;
  }
  const hint = $("new-claude-runtime-hint");
  if (hint) {
    const mode = f.skip_permissions.checked
      ? `enabled${permissionArgs.length ? ` (${permissionArgs.join(" ")})` : ""}`
      : "disabled";
    const source = parent && f.skip_permissions.disabled
      ? (harnessName === parent.harness
        ? ` · inherited from ${parent.name} (spawn.allow_args)`
        : " · Claude default (spawn.allow_args to override)")
      : "";
    hint.textContent = `permission skipping ${mode}${source}`;
  }
}

/* --fork-session is claude's own "use with --resume or --continue": with
   nothing to fork it is not a weaker choice, it is a rejected one. */
function syncForkAvailability() {
  const f = $("new-session");
  const resuming = f.resume.value !== "";
  const parent = spawnParent();
  const selector = newProfileSelector(f);
  const harnessName = newProfileHarnessName(f, selector) || "claude";
  const claude = harnessName === "claude";
  const capabilities = (typeof harnessDetails !== "undefined"
    ? harnessDetails[harnessName] : null) || {};
  const borrowCap = profileBorrowCapability(
    newProfileDetail(f, selector), harnessName
  );
  f.fork.disabled = !resuming || !claude;
  if (f.fork.disabled) f.fork.checked = false;
  f.role.disabled = !claude;
  f.resume.disabled = !claude;
  renderNewClaudeRuntime(f, harnessName, capabilities, parent);
  renderNewCodexRuntime(f, harnessName, capabilities, parent);
  // Claude and declared API-key harnesses consume the shared profile token.
  // OAuth harnesses keep auth in their own profile home. --null remains a
  // Claude-only answer and cannot coexist with a borrow.
  f.null_token.disabled = !claude;
  f.borrow.disabled = !!f.borrow._validationPending ||
    !!f.borrow._validationError ||
    !borrowCap.allowed || (claude && f.null_token.checked);
  f.borrow.title = f.borrow._validationError ||
    (f.borrow._validationPending ? "validating borrow candidates" : "");
  if (f.borrow.disabled) f.borrow.value = "";
  if (!claude) {
    f.role.value = "";
    f.resume.value = "";
    f.null_token.checked = false;
    renderRoleStance();
  }
  // This function speaks for the create form; on a child the spawn policy
  // has the last word, and it has just been overruled row by row above.
  if (spawnParent()) syncSpawnMode();
  // The fold below hides these rows; the summary line has to keep saying
  // what they hold, so every path that changes one of them lands here.
  // After syncSpawnMode, never before: on a child the policy decides which
  // of them speak for themselves, and the summary reads that answer. The
  // profile hint reads the same disables, for the same reason.
  renderRuntimeSummary();
  renderProfileHint();
}

/* What the folded "How it runs" rows currently say, written onto the fold's
   own summary line.

   A fold that hides the directory would be a trap: an agent started in the
   wrong checkout does not complain, it quietly works on the wrong tree, and
   the user finds out from a commit. So the values ride on the face of the
   fold — the directory always, the rest only when they are set to something
   other than their default, because a summary that lists every row is the
   fold nobody opens AND the line nobody reads.

   It does NOT name the promoted row (RUNTIME_PROMOTED — the qualified profile
   selector, including its harness). It is on the face of the form now, so
   a copy here would be noise at best and, since the two are written by
   different code paths, a contradiction at worst. The rule is the same one
   the fold's face has always followed: say what the reader cannot see.

   On a child only the rows the spawn policy left OPEN may speak for
   themselves. A greyed row still holds whatever the form was last showing,
   and that value is not what gets created — the parent's is. Reporting it
   would be the fold's face telling a lie about the very thing the fold is
   hiding, so a locked row is left out and the parent is named instead.
   Which rows are shut is the parent hint's job, above the fold and always
   visible; this line says what would be used, not what is forbidden. */
function renderRuntimeSummary() {
  const out = $("new-runtime-sum");
  if (!out) return;  // the fold is markup; a page that predates it still runs
  const f = $("new-session");
  const parent = spawnParent();
  const speaks = (key) => !parent || !!(f[key] && !f[key].disabled);
  const bits = [];
  if (speaks("cwd")) {
    // The option's label is "name — path"; the name is what the user
    // registered the directory as, and the path is what the fold shows.
    // In child mode the first entry says "(inherit the parent's directory)",
    // which is the honest answer until a workspace is picked.
    // With no options yet — the workspace list has not arrived, or its
    // fetch failed — the row itself does not know where it would go, so the
    // line says nothing rather than naming a directory it made up.
    const dir = f.cwd.options[f.cwd.selectedIndex];
    if (dir) bits.push(dir.text.split(" — ")[0]);
  }
  if (speaks("borrow") && f.borrow.value) bits.push(`borrow ${f.borrow.value}`);
  if (speaks("null_token") && f.null_token.checked) bits.push("--null");
  if (speaks("resume") && f.resume.value) {
    bits.push(f.resume.value === PICKER ? "resume (picker)" : `resume ${f.resume.value}`);
  }
  if (speaks("args") && f.args.value.trim()) bits.push("+args");
  const claudePanel = $("new-claude-runtime");
  if (claudePanel && !claudePanel.classList.contains("hidden") &&
      speaks("skip_permissions") && f.skip_permissions.checked) {
    bits.push("Claude permissions skipped");
  }
  const codexPanel = $("new-codex-runtime");
  if (codexPanel && !codexPanel.classList.contains("hidden") &&
      speaks("codex_yolo")) {
    if (f.codex_yolo && f.codex_yolo.checked) bits.push("Codex YOLO");
    if (f.codex_sandbox && f.codex_sandbox.checked) bits.push("Codex sandbox");
  }
  if (parent) {
    out.textContent = bits.length
      ? `— ${parent.name}'s setup · ${bits.join(" · ")}`
      : `— inherited from ${parent.name}`;
    return;
  }
  // Nothing left to hide is nothing to say: with the profile promoted out,
  // a form whose workspace list has not arrived yet has no folded value at
  // all, and a bare "—" is a label pointing at nothing.
  out.textContent = bits.length ? `— ${bits.join(" · ")}` : "";
}

/* The one line the promoted Profile row cannot say for itself.

   Promoting the row answers "which profile", and that is the question people
   get wrong — but three rows still inside the fold change what the answer
   MEANS, and each of them is the same class of silent mistake:

   - --borrow runs the session on ANOTHER profile's token. The Profile row
     goes on naming the profile whose config and skills are used, which is
     true and, on its own, misleading about the credential.
   - --null runs it on no token at all; the profile is still real, but
     nothing is logged in until someone types /login inside.
   - a profile whose harness this machine has not installed cannot boot at
     all (profile_details[].harness_available); the selector names the program
     but cannot by itself say that the executable is missing.

   Silent when none of them applies: a hint that is always up is a hint
   nobody reads, and this one has to be legible on the one launch in fifty
   where it matters. On a child, a row the spawn policy locked is the
   parent's and says nothing here — same rule as the summary line's. */
function renderProfileHint() {
  const box = $("profile-hint");
  if (!box) return;  // a page that predates the row still runs
  const f = $("new-session");
  if (!f || !f.profile) return;
  const parent = spawnParent();
  const speaks = (key) => !parent || !!(f[key] && !f[key].disabled);
  const picked = newProfileSelector(f);
  const detail = picked ? newProfileDetail(f, picked) : null;
  const shown = detail && !detail.error
    ? profileHarnessLabel(detail.profile || picked, detail.harness)
    : profileHarnessLabel(picked, "");
  const choseOverride = !!f.profile.value || !!(f.harness && f.harness.value);
  const whose = (shown && (!parent || choseOverride))
    ? shown : (parent ? `${parent.name}'s profile` : "this profile");
  let text = "";
  if (speaks("null_token") && f.null_token && f.null_token.checked) {
    text = `--null: it boots with no token at all — ${whose}'s config and ` +
           `skills, but somebody has to run /login inside before it works.`;
  } else if (speaks("borrow") && f.borrow && f.borrow.value) {
    text = `--borrow ${f.borrow.value}: it runs on ${f.borrow.value}'s token ` +
           `and provider — only the credential is theirs, the config and ` +
           `skills stay ${whose}'s.`;
  } else if (detail && detail.error) {
    text = `${shown}: ${detail.error}`;
  } else if (detail && detail.harness_available === false) {
    text = `${detail.harness || "its harness"} is not installed on this ` +
           `machine — a session on ${shown} will not start until it is.`;
  }
  box.textContent = text;
  box.classList.toggle("hidden", !text);
}

/* The fold is shut on arrival, which is right for as long as everything
   inside it is the parent's — and wrong the moment the spawn policy hands a
   row back, because an unlocked row is a decision the operator cannot see is
   theirs while it is folded away. So entering a state where something inside
   is editable opens the fold.

   Opening is the only move it makes. It never shuts the fold, and it does
   not re-open on the next poll: the stamp remembers the parent and the exact
   set of rows that were open, so a fold the operator shut stays shut until
   the parent — or what the policy opens for it — actually changes. Without
   that latch the two-second poll would fight whoever tried to close it. */
let runtimeFoldOpenedFor = null;
function syncRuntimeFold(f, parent) {
  const fold = $("new-runtime");
  // Only the rows the fold actually hides count. A promoted row the policy
  // hands back is already visible and already labelled, so springing the
  // fold open for it would open it on rows that stayed the parent's — the
  // operator reads them as theirs and they are not.
  const openRows = parent
    ? SPAWN_INHERITS.filter(
        (k) => f[k] && !f[k].disabled && !RUNTIME_PROMOTED.includes(k))
    : [];
  const stamp = parent ? `${parent.name}:${openRows.join(",")}` : "";
  if (fold && openRows.length && stamp !== runtimeFoldOpenedFor) fold.open = true;
  runtimeFoldOpenedFor = stamp;
}

/* One listener for the whole form rather than one per folded row: `input`
   bubbles from every control in it, and both lines are cheap to rebuild. */
$("new-session").addEventListener("input", () => {
  const f = $("new-session");
  const harnessName = newProfileHarnessName(f) || "claude";
  const capabilities = (typeof harnessDetails !== "undefined"
    ? harnessDetails[harnessName] : null) || {};
  renderNewClaudeRuntime(f, harnessName, capabilities, spawnParent());
  renderNewCodexRuntime(
    f, harnessName, capabilities, spawnParent()
  );
  renderRuntimeSummary();
  renderProfileHint();
});

/* ------------------------------------------------------------------ */
/* the create form as `claunch spawn`: a CHILD of a session            */
/* ------------------------------------------------------------------ */
/* Naming a parent turns Create into a spawn. A child inherits everything
   that decides what runs — harness, profile, auth, directory, args — so
   those rows start greyed. WHICH of them it may still be asked is not this
   form's opinion though: it is the spawn policy's, field by field (the
   per-field unlocks in ~/.claunch.yaml), and the daemon publishes exactly
   that, per parent. So the form asks, and hands back the rows the policy
   opens — the same rows, from the same report, as the CLI wizard and the
   spawn modal. A form that offers what it cannot send teaches the policy
   wrong; one that withholds what the policy opened teaches it just as
   wrong, and lies to the person who set 'allow_profile: true'. */
const SPAWN_INHERITS = ["profile", "harness", "borrow", "null_token", "cwd",
                        "args", "resume", "fork", "skip_permissions", "codex_yolo",
                        "codex_sandbox"];

/* Of those, the two that no longer live in the fold. They are still
   inherited — the spawn policy governs them exactly as before, and
   spawnChildFields still reads them through their disables — but they are
   asked on the face of the form, because what they decide is WHOSE
   credentials the session runs on rather than merely how it runs. A pair
   picked wrong is not caught by anything downstream: the session boots, on
   the wrong token, and reports nothing.

   Everything that reasons about "what the fold hides" subtracts this list —
   the summary line does not repeat a visible row (renderRuntimeSummary) and
   the fold does not spring open for one (syncRuntimeFold). The markup is
   held to the same partition by tests/web/newform_check.js: the fold's rows
   plus these must be exactly SPAWN_INHERITS, so promoting a row means moving
   it, never copying it. */
const RUNTIME_PROMOTED = ["profile", "harness"];

/* The picked parent's spawn capabilities, and which parent they are about:
   one report per parent, kept until the pick moves. */
let newSpawnReport = null;
let newSpawnReportFor = null;
/* The parent the inherited rows were last seeded for, so entering child mode
   can default them (to the parent's own harness, to "inherit") without a
   poll stamping on what the operator picked afterwards. */
let newSpawnDefaultsFor = null;

/* Which inherited rows a report hands back, keyed like SPAWN_INHERITS. No
   report — none fetched yet, or the fetch failed — opens nothing, so the
   form behaves exactly as it did before it asked: the reading that cannot
   invent a permission. */
function spawnUnlocked(report) {
  const may = (report && report.may_choose) || [];
  return {
    harness: may.includes("profile"),
    profile: may.includes("profile"),
    borrow: may.includes("borrow"),
    // Ungated by the policy — it takes a credential away rather than
    // granting one — but still claude-only machinery (see syncSpawnMode).
    null_token: may.includes("null_token"),
    // The directory travels as a workspace NAME: 'allow_cwd' is the
    // free-text path this form never sends, while the registry the picker
    // is built from is exactly what 'allow_workspace' opens. The report
    // omits the list rather than emptying it when that is shut.
    cwd: !!(report && report.workspaces),
    args: may.includes("args"),
    // The child API treats an empty args list as inheritance, so a checkbox
    // cannot faithfully remove a parent's sole Claude permission flag. Keep
    // the Claude panel visible as inherited; the free Args override remains
    // the policy-controlled escape hatch. Codex always emits an explicit
    // mode group and therefore has no empty-override ambiguity.
    skip_permissions: false,
    codex_yolo: may.includes("args"),
    codex_sandbox: may.includes("args"),
    // Not the policy's: a spawn has no --resume of its own, and the one
    // conversation a child can start from is its parent's — the fork row,
    // which is where that question is actually asked.
    resume: false,
    fork: false,
  };
}

/* The spawn policy for the parent now named, fetched once per parent. The
   answer arrives after the form is on screen, so the rows stay inherited
   until it does, and a failed fetch simply leaves them that way. */
async function refreshSpawnPolicy() {
  const f = $("new-session");
  const name = f.parent ? f.parent.value : "";
  if (name === newSpawnReportFor) return;
  newSpawnReportFor = name;
  newSpawnReport = null;
  syncSpawnMode();
  // A child's workflows are the ones declared where the child will stand,
  // which is its parent's directory unless the policy lets it be moved.
  if (!name) { refreshWorkflowChoices(); return; }
  const report = await spawnReport(name);
  if (newSpawnReportFor !== name) return;   // the pick moved on while we asked
  newSpawnReport = report;
  syncSpawnMode();
  refreshWorkflowChoices();
}

/* The workspace a picked directory IS, by name. The picker's values are
   paths, because that is what the create form sends; a child's directory
   travels as the registry name the policy vouched for instead. A path with
   no entry — a registry edited between the fill and the submit — answers
   "", and the caller sends the path so the daemon's own refusal explains it
   rather than the form quietly dropping the pick. */
function spawnWorkspaceName(path) {
  const hit = (workspacesCache || []).find((w) => w.path === path);
  return hit ? hit.name : "";
}

/* The sessions a child can be a child of: the live ones. An exited session
   is refused by the daemon ("an exited session cannot spawn children"), so
   it is not offered. Fed by the session poll and so guarded the same way as
   the resume picker (see parentsRendered). */
function refreshParentChoices() {
  const select = document.querySelector("#new-session select[name=parent]");
  if (!select) return;
  // Wider than the option it draws, on purpose: the rebuild's tail is
  // syncSpawnMode, which reads more of the PICKED parent than the label
  // shows — its harness decides which rows a child may differ on, and
  // whether it has a conversation decides whether the fork is offered.
  // Leave those out of the signature and a parent that changed one keeps
  // the greying it had before, which is the create form lying about what
  // Create would send.
  const offered = sessionsCache
    .filter((s) => s.status !== "exited")
    .map((s) => [s.name, s.status, s.harness || "", !!s.conversation_id]);
  const signature = JSON.stringify(offered);
  if (signature === parentsRendered) return;
  parentsRendered = signature;

  const previous = select.value;
  select.innerHTML = "";
  select.appendChild(new Option("(none — a session of its own)", ""));
  for (const [name, status] of offered) {
    select.appendChild(new Option(`${name} — ${status}`, name));
  }
  select.value = [...select.options].some((o) => o.value === previous)
    ? previous
    : "";
  syncSpawnMode();
}

function spawnParent() {
  const f = $("new-session");
  const name = f.parent ? f.parent.value : "";
  return name ? sessionsCache.find((s) => s.name === name) || null : null;
}

/* Grey what a child inherits AND the policy keeps shut, and offer the fork
   only where there is a conversation to copy. Claude keeps transcripts per
   working directory and a child stays in its parent's, so the fork here is
   always the parent's own — there is no directory question to contradict
   it. */
function syncSpawnMode() {
  const f = $("new-session");
  const parent = spawnParent();
  const hint = $("parent-hint");
  // A report is only about the parent it was fetched for; a pick that moved
  // on since is no report at all.
  const report =
    parent && newSpawnReportFor === parent.name ? newSpawnReport : null;
  const open = spawnUnlocked(report);
  for (const key of SPAWN_INHERITS) {
    if (f[key]) f[key].disabled = !!parent && !open[key];
  }
  // Seeding happens once per parent, not on every poll: the second call
  // would be the one that throws away the operator's own pick.
  newSpawnDefaultsFor = parent ? parent.name : null;
  syncSpawnProfileRow(f, !!parent);
  refillNewHarnessOptions(
    f, parent && !f.profile.value ? "" : undefined
  );
  syncSpawnCwdRow(f, !!parent);
  syncNewBorrowOptions();
  if (parent) {
    // Auth is claude's token machinery: on a child running anything else
    // both rows are moot however the policy is set, and a yes on --null greys
    // the borrow row rather than provoking the daemon's refusal of the pair.
    // The same two rules the spawn modal applies.
    const selector = newProfileSelector(f);
    const childHarness = newProfileHarnessName(f, selector);
    const borrowCap = profileBorrowCapability(
      newProfileDetail(f, selector), childHarness
    );
    const claude = !childHarness || childHarness === "claude";
    const childCapabilities = (typeof harnessDetails !== "undefined"
      ? harnessDetails[childHarness] : null) || {};
    const modeBase = childHarness === parent.harness ? (parent.args || []) : [];
    seedNewCodexRuntime(
      f, childCapabilities, modeBase,
      `child:${parent.name}:${selector}:${childHarness}`
    );
    renderNewClaudeRuntime(f, childHarness, childCapabilities, parent);
    renderNewCodexRuntime(f, childHarness, childCapabilities, parent);
    f.role.disabled = !claude;
    if (!claude) {
      f.null_token.checked = false;
      f.null_token.disabled = true;
      f.role.value = "";
      renderRoleStance();
    }
    if (f.borrow._validationPending || f.borrow._validationError) {
      f.borrow.value = "";
      f.borrow.disabled = true;
      f.borrow.title = f.borrow._validationError ||
        "validating borrow candidates";
    } else if (!borrowCap.allowed) {
      f.borrow.value = "";
      f.borrow.disabled = true;
    } else if (f.null_token.checked && !f.null_token.disabled) {
      f.borrow.value = "";
      f.borrow.disabled = true;
    }
    // Named, not merely greyed: "inherits everything" is true of a locked
    // form and of an open one alike, and the operator who unlocked profile
    // in ~/.claunch.yaml needs to see which rows are still shut to know the
    // daemon read the file.
    const panelVisible = (id) => {
      const panel = $(id);
      return panel && !panel.classList.contains("hidden");
    };
    const runtimeVisible = (key) =>
      key === "skip_permissions" ? panelVisible("new-claude-runtime") :
      (key === "codex_yolo" || key === "codex_sandbox")
        ? panelVisible("new-codex-runtime") : true;
    const shut = SPAWN_INHERITS.filter(
      (k) => f[k] && runtimeVisible(k) && f[k].disabled &&
        k !== "resume" && k !== "fork");
    hint.textContent =
      `a child of ${parent.name}: it inherits that session's setup, and the ` +
      `rows left open below are what may differ` +
      (shut.length
        ? ` — ${shut.join(", ")} stay its parent's (the spawn.* unlocks in ~/.claunch.yaml)`
        : "");
    hint.classList.remove("hidden");
    // The policy has just decided which folded rows are the operator's, and
    // the summary line reports exactly that — so it is re-rendered HERE, not
    // left to syncForkAvailability. The other branch reaches it through that
    // call instead, which is why this one is inside the child arm.
    renderRuntimeSummary();
    renderProfileHint();
  } else {
    hint.classList.add("hidden");
    // The rows go back to the form that owns them, which has its own
    // reasons to grey some of them (a non-claude harness, --null).
    syncForkAvailability();
  }
  // The policy has had its say on every folded row by now, so this is
  // where the fold can tell whether anything inside is the operator's.
  syncRuntimeFold(f, parent);
  syncSpawnOverRow(f, report);
  const row = $("new-fork-row");
  row.classList.toggle("hidden", !parent);
  const forkable =
    !!parent && parent.harness === "claude" && !!parent.conversation_id;
  f.fork_parent.disabled = !forkable;
  if (!forkable) f.fork_parent.checked = false;
  row.title = forkable
    ? "the child opens a copy of the parent's conversation and diverges from there"
    : "the parent has no claude conversation to copy";
}

/* The profile row means different things in the two modes: a session of its
   own always runs SOME profile, while a child runs its parent's unless it is
   told otherwise. So the inherit entry exists only while a parent is named,
   and is what the row starts on — without it every unlocked child would be
   spawned onto whichever profile happens to sort first. */
function syncSpawnProfileRow(f, child) {
  const sel = f.profile;
  if (!sel || !sel.options) return;
  const at = [...sel.options].findIndex((o) => o.value === "");
  if (child && at < 0) {
    sel.insertBefore(new Option("(inherit the parent's profile)", ""),
                     sel.options[0] || null);
    sel.value = "";
  } else if (!child && at >= 0) {
    sel.remove(at);
    if (!sel.value) {
      const first = [...sel.options].find((o) => !o.disabled);
      sel.value = first ? first.value : "";
    }
  }
}

/* Same shape for the directory row: "" is the daemon's own directory when
   the session is its own, and the parent's when it is a child. One option,
   two truths — said in words rather than left for the operator to guess. */
function syncSpawnCwdRow(f, child) {
  const sel = f.cwd;
  const first = sel && sel.options && sel.options[0];
  if (!first || first.value !== "") return;
  first.textContent = child ? "(inherit the parent's directory)" : "(daemon cwd)";
}

/* The child cap is SOFT (spawn.py: it warns and lets the spawn through), so
   a parent standing at its limit gets the crossing PRE-TICKED rather than a
   dead end — the row is there to say the cap was reached and to let anyone
   who wants the strict reading untick it, not to collect permission the
   daemon no longer asks for. Shown only while the daemon actually reports
   the cap reached, the same condition the CLI wizard's Over limit row is
   shown under. */
function syncSpawnOverRow(f, report) {
  const soft = (report && report.soft_blocked_by) || [];
  const row = $("new-over-row");
  if (!row) return;
  const wasHidden = row.classList.contains("hidden");
  row.classList.toggle("hidden", !soft.length);
  const text = $("new-over-text");
  if (text) {
    text.textContent = soft.length
      ? `${soft.join("; ")} — untick to be refused at the cap instead`
      : "";
  }
  // Pre-answered on the way UP only. Re-ticking it on every sync would undo
  // an untick the operator had just made, the row being synced by more than
  // the parent changing under it.
  if (soft.length && wasHidden && f.over_limit) f.over_limit.checked = true;
  // An answer given while the row was up, on a parent that then freed a
  // slot, must not survive as a silent override either way.
  if (!soft.length && f.over_limit) f.over_limit.checked = false;
}

/* What a child carries beyond its name, read through the disables: every
   row the policy left open and the operator actually filled in. The keys are
   the spawn API's, which is not always the picker's — the directory travels
   as the workspace NAME the registry vouched for, never as a path. */
function spawnChildFields(f, body) {
  const put = (k, v) => { if (v) body[k] = v; };
  if (!f.profile.disabled && !f.harness.disabled) {
    put("profile", newProfileOverride(f));
  }
  if (!f.borrow.disabled) put("borrow", f.borrow.value);
  if (!f.null_token.disabled && f.null_token.checked) body.null_token = true;
  const selector = newProfileSelector(f);
  const harnessName = newProfileHarnessName(f, selector);
  const capabilities = (typeof harnessDetails !== "undefined"
    ? harnessDetails[harnessName] : null) || {};
  const typed = !f.args.disabled && f.args.value.trim()
    ? f.args.value.trim().split(/\s+/) : [];
  const codexPanel = $("new-codex-runtime");
  const claudePanel = $("new-claude-runtime");
  const codexOpen = harnessName === "codex" && codexPanel &&
    !codexPanel.classList.contains("hidden") &&
    f.codex_yolo && !f.codex_yolo.disabled;
  const claudeOpen = harnessName === "claude" && claudePanel &&
    !claudePanel.classList.contains("hidden") &&
    f.skip_permissions && !f.skip_permissions.disabled;
  if (codexOpen) {
    const original = f._codexRuntimeOriginal || { yolo: true, sandbox: false };
    const changed = f.codex_yolo.checked !== original.yolo ||
      f.codex_sandbox.checked !== original.sandbox;
    if (typed.length || changed) {
      body.args = codexRuntimeArgs(
        typed.length ? typed : (f._codexRuntimeBaseArgs || []),
        capabilities,
        !!f.codex_yolo.checked,
        !!f.codex_sandbox.checked
      );
    }
  } else if (claudeOpen) {
    const changed = f.skip_permissions.checked !== !!f._claudeRuntimeOriginal;
    if (typed.length || changed) {
      const permissionArgs = (capabilities.skip_permissions_args || []).map(String);
      body.args = withoutArgGroups(
        typed.length ? typed : (f._claudeRuntimeBaseArgs || []),
        [permissionArgs]
      );
      if (f.skip_permissions.checked) body.args.push(...permissionArgs);
    }
  } else if (typed.length) {
    body.args = typed;
  }
  if (!f.cwd.disabled && f.cwd.value) {
    const name = spawnWorkspaceName(f.cwd.value);
    // No entry for the path (a registry edited under the form): send it as
    // the path so the daemon says why, instead of dropping the pick here.
    if (name) body.workspace = name;
    else body.cwd = f.cwd.value;
  }
  // Both answers travel, and only from a VISIBLE row. `false` is the one
  // that changes anything now — it asks the daemon for the refusal it no
  // longer gives by default — so it cannot be dropped as falsy the way the
  // `put` helper above drops empty strings.
  const over = $("new-over-row");
  if (over && !over.classList.contains("hidden") && f.over_limit) {
    body.over_limit = !!f.over_limit.checked;
  }
  return body;
}

document
  .querySelector("#new-session select[name=parent]")
  .addEventListener("change", () => {
    syncSpawnMode();
    refreshSpawnPolicy();
    // A child is created on the board of ITS directory, which the parent
    // decides — so the memo is dropped here as well as on the Directory row.
    issuesFor = null;
    issuesRead = false;
    if (beadsMode() === "existing") refreshIssueChoices();
  });

document
  .querySelector("#new-session select[name=role]")
  .addEventListener("change", () => {
    renderRoleStance();
    // Picking a role picks its workflow, the way the CLI wizard's Role row
    // does — see syncOnboardPickers for what survives the change.
    syncOnboardPickers();
  });
$("new-session").resume.addEventListener("change", syncForkAvailability);
$("new-session").profile.addEventListener("change", () => {
  const f = $("new-session");
  refillNewHarnessOptions(f, f.profile.value ? undefined : "", true);
  syncNewBorrowOptions(true);
  syncForkAvailability();
  syncSpawnMode();
  renderProfileHint();
});
$("new-session").harness.addEventListener("change", () => {
  syncNewBorrowOptions(true);
  syncForkAvailability();
  syncSpawnMode();
  renderProfileHint();
});
$("new-session").null_token.addEventListener("change", syncForkAvailability);

$("new-session").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const parent = spawnParent();
  // A child is built from its parent's definition, so only the fields that
  // make it a different worker are sent — the rest would be refused by the
  // spawn policy, field by field.
  const body = parent ? { name: f.name.value.trim() } : {
    name: f.name.value.trim(),
    profile: newProfileSelector(f) || null,
    cwd: f.cwd.value,  // a registered workspace path, or "" = the daemon's cwd
    args: f.args.value.trim() ? f.args.value.trim().split(/\s+/) : [],
  };
  if (!parent) {
    const selector = newProfileSelector(f);
    const harnessName = newProfileHarnessName(f, selector) || "claude";
    const capabilities = harnessDetails[harnessName] || {};
    if (harnessName === "codex") {
      body.args = codexRuntimeArgs(
        body.args,
        capabilities,
        !!f.codex_yolo.checked,
        !!f.codex_sandbox.checked
      );
    } else if (harnessName === "claude") {
      const permissionArgs = (capabilities.skip_permissions_args || []).map(String);
      body.args = withoutArgGroups(body.args, [permissionArgs]);
      if (f.skip_permissions.checked) body.args.push(...permissionArgs);
    }
  }
  // A child sends what the spawn policy left open, and nothing else: a value
  // standing on a greyed row is not an answer anybody gave, and sending it
  // provokes a 403 naming a field nobody in this form could still choose.
  if (parent) spawnChildFields(f, body);
  if (parent && f.fork_parent.checked) body.fork = true;
  if (f.role.value) body.role = f.role.value;
  if (!parent && f.borrow.value) body.borrow = f.borrow.value;
  if (!parent && f.null_token.checked) body.null_token = true;
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
  // The board answer. "new" is the absence of both keys — a request that says
  // nothing gets an issue minted from the task, which is what every client
  // that has never heard of this field still wants.
  const beads = f.beads.value;
  if (beads === "none") body.beads = false;
  else if (beads === "existing" && f.issue.value) body.issue = f.issue.value;
  // Only under the answer it belongs to: the box keeps its text while the
  // radios are being tried out, and sending it alongside "existing" or
  // "none" is a contradiction the daemon refuses (beads.check_request).
  else if (beads === "new" && f.issue_text.value.trim()) {
    body.issue_text = f.issue_text.value.trim();
  }
  if (!parent && f.resume.value) {
    // "" (no resume) is left off entirely: the API reads a missing key as
    // "a new conversation" and an empty string as "open the picker".
    body.resume = f.resume.value === PICKER ? "" : f.resume.value;
    body.fork_session = f.fork.checked;
  }
  const resp = await api(
    parent
      ? `/api/sessions/${encodeURIComponent(parent.name)}/children`
      : "/api/sessions",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }
  );
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
  // The spawn endpoint wraps the child (it also reports the parent and what
  // the onboarding did); the create one answers with the session itself.
  const made = info.session || info;
  await refreshSessions();
  location.hash = "#/s/" + encodeURIComponent(made.name);
});

$("term-details").addEventListener("click", () => openDetail(currentName));

/* Navigation, not an action on the session: it scrolls the rail to the card
   for the terminal you are in and marks it for a moment (see
   gotoSessionCard). */
$("term-goto").addEventListener("click", () => { gotoSessionCard(); });

/* Kill ends, remove forgets, and the two buttons never share a meaning:
   kill posts to the kill route (which leaves an exited session alone), and
   remove is the only thing on the page that makes a session unresumable.
   The header shows exactly one of them at a time (see setStatusBadge). */
$("term-kill").addEventListener("click", async () => {
  if (!currentName) return;
  const name = currentName;
  // A live session holding board issues is wound down first (the daemon
  // types a settle-the-board block in and waits for that turn); the same
  // button pressed again while that runs means "stop now" — the daemon
  // reads the second kill that way, this only says so in the URL.
  const winding = !!(sessionsCache.find((s) => s.name === name) || {}).winddown;
  const resp = await api(
    `/api/sessions/${encodeURIComponent(name)}/kill${winding ? "?winddown=0" : ""}`,
    { method: "POST" }
  );
  const info = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    await modalInfo(`Could not kill '${name}'`,
                    info.error || `HTTP ${resp.status}`);
  } else if (info.status === "exited") {
    const at = sessionsCache.findIndex((s) => s.name === name);
    if (at >= 0) sessionsCache[at] = { ...sessionsCache[at], ...info };
    setSessionFilter("killed");
  }
  refreshSessions();
});

async function archiveExitedSession(name) {
  const resp = await api(
    `/api/sessions/${encodeURIComponent(name)}/archive`, { method: "POST" }
  );
  const info = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    await modalInfo(`Could not archive '${name}'`,
                    info.error || `HTTP ${resp.status}`);
    return false;
  }
  const at = sessionsCache.findIndex((s) => s.name === name);
  if (at >= 0) sessionsCache[at] = { ...sessionsCache[at], ...info };
  // Archive changes the rail classification while the final screen, detail
  // pane and URL remain on this session. The state-specific filter follows
  // the transition so the selected row stays visible.
  setSessionFilter("archived");
  await refreshSessions();
  setStatusBadge("exited");
  return true;
}

$("term-archive").addEventListener("click", async () => {
  if (!currentName) return;
  await archiveExitedSession(currentName);
});

/* Stop everything. The records stay and every one of them is resumable after,
   which is what makes this the one bulk verb that needs no mesh guard: a
   member row is *meant* to outlive its terminal, reading `exited`. */
$("stop-all").addEventListener("click", async () => {
  const live = sessionsCache
    .filter((s) => s.status !== "exited").map((s) => s.name);
  if (!live.length) return;
  if (!(await modalConfirm(
    `Stop ${live.length} running session(s)?`,
    `${live.join(", ")}\n\n` +
    `The program in each one is terminated. Their records stay, so all of ` +
    `them can be resumed from here afterwards.`,
    "Stop", false
  ))) return;
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
    .filter((s) => s.status === "exited" && !s.archived_at).map((s) => s.name);
  if (!dead.length) return;
  if (!(await modalConfirm(
    `Resume ${dead.length} exited session(s)?`,
    `${dead.join(", ")}\n\n` +
    `Each comes back under its own name — the claude harness with --resume of ` +
    `the conversation it was pinned to.`,
    "Resume", false
  ))) return;
  const result = await bulkAction(
    $("resume-all"), "/api/sessions/respawn?archived=0",
    { method: "POST" }, "resume"
  );
  const back = (result && result.respawned) || [];
  detach();
  await refreshSessions();
  if (currentName && back.includes(currentName)) attach(currentName);
});

$("archive-exited").addEventListener("click", async () => {
  const dead = sessionsCache
    .filter((s) => s.status === "exited" && !s.archived_at)
    .map((s) => s.name);
  if (!dead.length) return;
  await bulkAction(
    $("archive-exited"), "/api/sessions/archive",
    { method: "POST" }, "archive"
  );
  setSessionFilter("archived");
  await refreshSessions();
});

for (const filter of SESSION_FILTERS) {
  const button = $(`session-filter-${filter}`);
  if (button) button.addEventListener("click", () => setSessionFilter(filter));
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

/* The top briefing button: fold open (or shut) the current session's summary
   card in the pane under the header. Same ownership as the row ▸, so the two
   stay in step — opening from the header also opens it on the row. */
$("term-brief").addEventListener("click", () => {
  if (currentName) toggleBriefing(currentName);
});

/* The transcript button: walk to this session's conversation page. A route,
   not a toggle — see the transcript page's own block for why the fourth
   reading of a session deserves the same standing as the other three. The
   terminal is not torn down by the trip; it is parked like any navigation,
   socket and all, and the back button brings it straight back. */
$("term-log").addEventListener("click", () => {
  if (currentName) location.hash = `#/log/${encodeURIComponent(currentName)}`;
});

/* Rebrief: have the daemon re-derive this session's briefing (mesh roster,
   owed replies, cflow position, parent/children, opening task) and type it
   into the terminal — the operator's push for an agent whose context was
   cleared or compacted and does not know what it lost. The automatic path is
   the SessionStart hook every managed claude session carries; this button is
   for the cases the hook cannot cover (another harness, a hook that was
   stripped, or "I can see it flailing right now"). */
$("term-rebrief").addEventListener("click", async () => {
  if (!currentName) return;
  const btn = $("term-rebrief");
  btn.disabled = true;
  try {
    const resp = await api(
      `/api/sessions/${encodeURIComponent(currentName)}/rebrief`,
      { method: "POST" }
    );
    const info = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      alert(info.error || `HTTP ${resp.status}`);
      return;
    }
    if (info.empty) {
      alert("nothing to re-brief: this session has no mesh membership, " +
            "no cflow run, no parent or children, and no recorded task");
    }
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
  // An exited session is revivable and archivable. An archived record keeps
  // resume while archive itself disappears because the transition is done.
  const exited = status === "exited";
  const archived = !!(sessionsCache.find((s) =>
    s.name === currentName) || {}).archived_at;
  $("term-resume").classList.toggle("hidden", !exited);
  // Rebrief types into a live terminal; on an exited one there is nobody to
  // read it, so the button yields its spot to resume.
  $("term-rebrief").classList.toggle("hidden", exited);
  $("term-kill").classList.toggle("hidden", exited);
  $("term-archive").classList.toggle("hidden", !exited || archived);
  // Every attach path passes through here (freshAttach and restoreTerminal
  // both seed the header with it), so this is where the countdown is told
  // which session it is now about — a whole second of the last session's
  // clock would otherwise sit on this one's name.
  renderTermTimer();
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
  // `scrollback=1` asks the daemon to seed this socket with its scrollback
  // before the grid repaint. This page is the client that wants it: its xterm
  // is built with a real scrollback (buildSessionTerm), so the seeded lines
  // land somewhere and the wheel over them is the browser's own. The daemon
  // sends nothing without the flag — `claunch attach` never asked for five
  // thousand lines, and a client that says nothing must keep what it had.
  const sock = new WebSocket(
    `${proto}://${location.host}`
    + url(`/api/sessions/${encodeURIComponent(name)}/ws?scrollback=1`)
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
    // The seeded grid may be another viewer's (an attach in a maximized
    // terminal): render it whole right now, before the refit below claims
    // the size — a 50ms flash of a grid overflowing its box is still a
    // scrollbar someone can see.
    fitView();
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
    refreshTermInput();
    flushInput(same);
    // Adopt the viewer's size once attached.
    refitSoon(50);
    // A fresh socket always starts live, in whatever buffer the program is
    // drawing — and with whoever owns the mouse still owning it.
    altScreen = !!msg.alt;
    mouseTracking = !!msg.mouse;
    scrollOffset = 0;
    updateScrollChip();
    // ...unless there is no program there at all. A viewer landing on a
    // session that had ALREADY finished (#/s/<name> for a killed one) never
    // receives an `exit` frame — that one is published by a child ending, and
    // this child ended before anybody subscribed — so the init flag is the
    // only telling there is. Reading it is what keeps this terminal out of a
    // loop: the repaint above hands xterm the program's own mouse modes back
    // (?1003h — any-event tracking is in a claude session's final screen), so
    // without it a mouse MOVEMENT over the terminal writes to a child that is
    // gone, the daemon ends the socket on it, and the link answers a close it
    // takes for an outage by reconnecting, repainting, and being closed again
    // a moment later. That loop is what a reader sees as a terminal that will
    // not sit still.
    if (msg.exited) endSession();
  } else if (msg.type === "buffer") {
    altScreen = !!msg.alt;
    if (!altScreen) {
      // The TUI left the alternate screen: xterm's own scrollback takes over
      // for the wheel, and the daemon has already unfrozen us (ws.py).
      scrollOffset = 0;
    }
    updateScrollChip();
  } else if (msg.type === "mouse") {
    // The program took the mouse, or gave it back. Either way the wheel
    // changes hands; the daemon has already unfrozen us if it had to.
    mouseTracking = !!msg.tracking;
    if (mouseTracking) scrollOffset = 0;
    updateScrollChip();
  } else if (msg.type === "scrolled") {
    // The daemon's clamped answer to a scroll control — the truth for the
    // chip and for sendInput's snap-to-live.
    scrollOffset = msg.offset || 0;
    updateScrollChip();
  } else if (msg.type === "state") {
    setStatusBadge(msg.status);
    refreshTermInput();
  } else if (msg.type === "resize") {
    if (term.cols === msg.cols && term.rows === msg.rows) {
      // This viewer's own claim echoing back, or no news. Filtering on the
      // dims (rather than on visibility, as this used to) is also what keeps
      // a stale echo over the relay from churning the grid.
    } else if (document.hasFocus() && terminalOnScreen()) {
      // Another viewer claimed the size out from under the one actually
      // being looked at (an attach entering, a daemon restart): take it
      // back. No ping-pong hides here — the other side re-asserts only on
      // its own focus events, and it cannot be focused while this is.
      resyncTerminal();
    } else {
      // Not the viewer in use: mirror the claimed grid — it is the daemon's
      // truth, and a grid that disagrees with the PTY wraps every long line
      // into garble — then shrink the glyphs until the whole of it fits
      // this box (fitView), and ask for a repaint to fill the new shape.
      applyingRemoteResize = true;
      try { term.resize(msg.cols, msg.rows); }
      finally { applyingRemoteResize = false; }
      fitView();
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "repaint" }));
      }
    }
  } else if (msg.type === "exit") {
    // Not a broken link: the program finished under this socket. The notice
    // belongs here rather than in endSession(), because this is the case
    // where it lands after the program's own last output.
    endSession();
    term.write(
      `\r\n\x1b[90m[session exited (code ${msg.code})] ` +
      `- press "resume" above to relaunch it\x1b[0m\r\n`
    );
  }
}

/* The program this terminal is bound to is over — whether it ended under the
   socket (an `exit` frame) or had already ended before the socket existed (an
   `init` that says so). Everything here follows from there being no child:
   nothing to reconnect TO, so a close is not an outage (openSocket's onclose,
   reconnectNow); nothing to type at, so the send-keys strip closes and held
   keystrokes are dropped rather than replayed into whatever `resume` builds
   next; and nobody owning the mouse or the alternate screen, so the wheel
   comes back to the reader.

   Idempotent on purpose: the daemon can say it twice — the init flag, and the
   exit frame the daemon's write path answers with if this terminal got a
   report out before that flag was read — and the second telling must not
   re-run the notice above or move a scroll position. */
function endSession() {
  if (sessionEnded) return;
  sessionEnded = true;
  linkQueue = [];
  setLink("idle");
  setStatusBadge("exited");
  altScreen = false;
  mouseTracking = false;
  scrollOffset = 0;
  updateScrollChip();
  refreshTermInput();
}

/* ---- the one-line send-keys input --------------------------------------
   The native input under the terminal. The terminal's textarea is the xterm
   composer — a thing the browser treats as a terminal, not as a place to
   type — and an IME, a phone's soft keyboard or a pasted block all
   misbehave there in exactly the ways they behave in a real <input>. So this
   strip is where the reader types a prompt, and Enter hands the line to the
   session through the same send-keys passthrough `claunch send-keys` uses:
   POST /api/sessions/name/keys with keys=[text, "Enter"], in ONE call.

   The text and its Enter are never two transmissions: splitting them on the
   client is what re-spreads the submit/enter split across call sites, and
   the split belongs to Session.send_keys — the one place that may fold and
   un-fold it (split_submit, under the bracketed-paste marker). A test pins
   this single-call shape (tests/web/sendinput_check.js). */

function termInputBlock(ended) {
  if (ended) return "this session has ended — nothing to send keys to";
  return "";
}

function termInputNote(note, message, warn = false) {
  note.classList.add(warn ? "wf-warning" : "wf-note");
  note.classList.remove(warn ? "wf-note" : "wf-warning");
  note.textContent = message;
  note.title = message;
  note.classList.remove("hidden");
}

async function sendKeyLine(field, btn, note) {
  const text = field.value.trim();
  if (!text || !currentName) return false;
  const blocked = termInputBlock(sessionEnded);
  if (blocked) {
    termInputNote(note, blocked);
    return false;
  }
  note.classList.add("hidden");
  note.classList.remove("wf-warning");
  const wasField = field.disabled, wasBtn = btn.disabled;
  field.disabled = true;
  btn.disabled = true;
  try {
    const resp = await api(
      `/api/sessions/${encodeURIComponent(currentName)}/keys`,
      { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keys: [text, "Enter"] }) }
    );
    const doc = await resp.json().catch(() => ({}));
    if (resp.ok) {
      field.value = "";
      return true;
    }
    termInputNote(note, doc.error || "the session refused these keys", true);
    return false;
  } catch {
    termInputNote(note, "nothing was sent — the daemon is unreachable", true);
    return false;
  } finally {
    field.disabled = wasField;
    btn.disabled = wasBtn;
  }
}

/* The field's live-ness follows the session it types to. An ended session has
   no PTY any send-keys could reach, so the box is closed with the reason
   shown; a live one is open, and whatever this tab's badge says the daemon
   answers for. */
function refreshTermInput() {
  const field = $("term-input-field");
  const note = $("term-input-note");
  const btn = $("term-input-send");
  if (!field || !btn) return;
  const blocked = termInputBlock(sessionEnded);
  field.disabled = !!blocked;
  btn.disabled = !!blocked;
  if (blocked) {
    termInputNote(note, blocked);
  } else {
    note.classList.add("hidden");
    note.textContent = "";
    note.title = "";
  }
}

function onTermInputSubmit(ev) {
  ev.preventDefault();
  sendKeyLine($("term-input-field"), $("term-input-send"), $("term-input-note"));
}

// Wired at load, like every other listener this page mounts — guarded like
// every OTHER element access the whole-block harnesses (reconnect/wheel) boot
// without: those eval this block against a stub DOM that only carries what the
// block under test touches, and #term-input is not one of them.
if ($("term-input"))
  $("term-input").addEventListener("submit", onTermInputSubmit);

/* ---- typing marks ----
   Keystrokes that reach the daemon as bytes mark its keyboard busy on
   arrival (the BINARY frame sendInput sends), and that mark is what parks a
   mesh delivery or a send-keys from another agent until the human here stops
   typing. But not every key becomes bytes right away: an IME composing Hangul
   keeps the syllable in xterm's textarea until it commits, a phone keyboard
   keeps the whole word, and while that goes on the daemon sees a keyboard
   that has been quiet for seconds — its guard is 5s (TYPING_GUARD) — and
   types the delivery straight into the half-written line. So the textarea
   reports the keys themselves, as a `typing` control frame: at most one per
   TYPING_MARK_MS, which against a 5s guard is plenty, and never a byte into
   the PTY. Bare keydowns (a modifier, an arrow) mark too; a spurious mark
   only delays a delivery by the guard, a missed one corrupts a line. */
const TYPING_MARK_MS = 1000;
let lastTypingMark = 0;
let lastMarkWasDraft = false;

/* Did this event put *text* into the composer? The daemon holds deliveries
   for as long as an unsent line exists (Session.draft_open), and only a key
   that writes one may open that hold: a held Shift, an arrow, a Ctrl chord
   would open a draft that nothing the person types next can close. Enter is
   excluded for the same reason from the other side — the byte it sends is
   what CLOSES the draft, and a mark claiming otherwise would fight it. */
function isDraftEvent(ev) {
  if (!ev) return false;
  if (ev.type !== "keydown") return true;  // composition/input: text, always
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return false;
  const k = ev.key;
  // "Process"/"Unidentified" is a keydown an IME has already swallowed —
  // a syllable being composed, which is a draft in progress by definition.
  if (k === "Process" || k === "Unidentified") return true;
  return typeof k === "string" && k.length === 1;  // a character key
}

function noteTyping(ev) {
  // Same bookkeeping as a keystroke: the queued-deliveries banner says "your
  // typing" for a hold this tab is causing, composing included.
  lastLocalKey = Date.now();
  const draft = isDraftEvent(ev);
  // Throttled to one mark per window, except the first mark that carries a
  // draft: a bare keydown arriving a few milliseconds earlier must not
  // swallow the composition event behind it, or the syllable being typed
  // never opens the hold it exists to open.
  const fresh = draft && !lastMarkWasDraft;
  if (Date.now() - lastTypingMark < TYPING_MARK_MS && !fresh) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  lastTypingMark = Date.now();
  lastMarkWasDraft = draft;
  ws.send(JSON.stringify({ type: "typing", draft }));
}
/* Wired once per Terminal object, like onData: xterm owns one hidden textarea
   for the life of the terminal and every key — composed or not — passes
   through it. `input` covers virtual keyboards that fire no keydown, the
   composition events cover an IME that swallows both. */
function watchComposer(t) {
  const ta = t && t.textarea;
  if (!ta) return;
  for (const ev of ["keydown", "compositionstart", "compositionupdate", "input"]) {
    ta.addEventListener(ev, noteTyping);
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
  // Every keystroke aimed at this terminal, whether it reaches the daemon or
  // waits in linkQueue: the queued-deliveries banner uses this to tell "YOUR
  // typing is holding delivery" apart from some other viewer's keyboard.
  lastLocalKey = Date.now();
  // Nothing is aimed at a session that has ended. Not merely pointless: this
  // path carries what xterm generates on its OWN account as well as what a
  // person types — mouse reports under the tracking modes the repaint
  // re-asserted, focus reports, an answer to a device query — and the daemon
  // ends a socket whose write finds no child, which the link would then have
  // to read as an outage.
  if (sessionEnded) return;
  // Typing while scrolled back into history goes to a session the viewer is
  // not watching — and the response would be frozen with it. Snap to live
  // first, as every terminal does when the wheel returns to the bottom.
  if (scrollOffset > 0) {
    scrollOffset = 0;
    sendScroll(-999999);   // server clamps to 0
    updateScrollChip();
  }
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
  // A full tear-down supersedes whatever parked copy of this session exists —
  // the stale child the respawn-follow path leaves behind is exactly that.
  if (currentName) keptTerms.delete(currentName);
  closeLink();
  // The wheel handler and its debounce belong to the terminal being torn down.
  if (wheelTimer) { clearTimeout(wheelTimer); wheelTimer = null; }
  wheelAccum = 0;
  scrollOffset = 0;
  altScreen = false;
  mouseTracking = false;
  if (term) { term.dispose(); term = null; fitAddon = null; }
  updateScrollChip();
}

$("term-link").addEventListener("click", () => reconnectNow(true));
$("m-link").addEventListener("click", () => $("term-link").click());

/* ---- queued deliveries ----
   Mesh messages are not typed into a terminal the moment they are sent: the
   daemon holds them while the agent is mid-turn — and, less obviously, while
   a KEYBOARD is active on the session, because a paste landing in a human's
   thinking pause submits their half-typed line with the message folded in.
   Which means the operator most often blocks their own message: they send it
   from the panel, stare at the terminal, touch a key — and the touching is
   the hold. Nothing on screen said so; this banner is that missing sentence.

   It sits between the header and the terminal, amber like the busy badge,
   and folds open to show the actual backlog (from/via/how long, and the
   text) so "did it take my message?" never needs the CLI. Fed by its own
   lightweight endpoint on the same 2s poll as everything else; a daemon too
   old to have the route just never shows it. */
let lastLocalKey = 0;   // when THIS tab last typed into the attached terminal
let tqOpen = false;     // the banner's fold; survives every repaint
/* Why the last "deliver now" changed nothing, shown in place of the standard
   reason line. Cleared the moment the backlog does, so a stale complaint
   never outlives the thing it was about. */
let tqNote = "";

/* Could the keyboard the daemon is waiting out be ours? The daemon's guard
   is 5s of quiet (TYPING_GUARD); claim the hold a little longer than that so
   the wording never flips to "another viewer" while our own last keystroke
   is still the one being waited out. */
function localTyping() {
  return Date.now() - lastLocalKey < 8000;
}

/* One sentence for WHY the backlog is a backlog — shared by the banner and
   the session panel. `mine` says whether this tab's typing can be the
   keyboard in question (false when the panel describes another session). */
function queuedReason(q, mine) {
  // The DOOR before any of the holds: a hold is "this is still coming", and
  // a shut door is "the next one is not". Someone reading a banner to find
  // out why the terminal is quiet needs the second answer more, because it
  // is the one that also explains a backlog which has stopped growing —
  // every other field here looks identical to calm. `exited` still wins:
  // nothing is being refused there, the terminal is simply gone.
  const bp = q.backpressure || {};
  if (bp.congested && q.state !== "exited") {
    return "at capacity: " + bp.queued + " waiting (cap " + bp.inbox_max +
      ") — the mesh is REFUSING new messages for this session" +
      (bp.refused ? ", " + bp.refused + " turned away in the last 10 min" : "") +
      ". Senders are told to wait and re-send; nothing of theirs is queued";
  }
  // `state`, not `reason`: the daemon fills it unconditionally (it is the
  // gate's one-word answer even before anything has queued), so a payload
  // carrying it says the same thing here, in the banner, and in the header
  // chip. `reason` is kept for the old clients that asked about a backlog.
  switch (q.state !== undefined ? q.state : q.reason) {
    case "keyboard":
      // A draft is a different wait from a recent keystroke, and saying so
      // matters: "wait a few seconds" is true of the second and never of the
      // first — an unsent line holds the message until it is sent or cleared,
      // which is exactly what someone staring at the banner needs to know.
      if (q.draft_open) {
        return mine && localTyping()
          ? "held: YOUR unsent line is in the composer — send it (Enter) or clear it (Ctrl-C) and this goes in right after"
          : "held: an unsent line is in this session's composer (another viewer, or claunch attach)";
      }
      return mine && localTyping()
        ? "held by YOUR typing — leave the keyboard alone a few seconds and it will be typed in"
        : "held: a keyboard is active on this session (another viewer, or claunch attach)";
    case "busy":
      return q.status === "starting"
        ? "held: the session is still starting"
        : "held: the agent is mid-turn — typed in when it goes idle";
    case "exited":
      return "held: the session has exited — delivered if it is respawned";
    case "hold":
      return "held: delivery is pinned shut here (the hold chip) — press it " +
        "again to resume";
    case "paced":
      // Not a fault and not the session's doing: the daemon typed a block
      // in less than min_gap ago and is letting the next arrivals gather
      // into one block instead of three interruptions. Say the wait is
      // short, or this reads as another stuck delivery.
      return "held: delivery into this terminal is being paced — a block " +
        "went in moments ago and these go in with the next one";
    default:
      return "delivering…";
  }
}

/* Ask the daemon to type the backlog in NOW. The delivery worker holds a
   message while the agent is mid-turn; this is the operator overruling that
   wait — the one judgement the daemon cannot make for itself, because only a
   person knows whether interrupting this particular turn is fine.

   `flushed: 0` is a normal answer, not a failure: the session may have
   exited, or its terminal may not be able to take a paste yet. The caller
   gets the whole response so it can say which happened instead of leaving a
   button that looks like it did nothing. */
async function flushQueued(name, btn) {
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "delivering…"; }
  let doc = null, resp = null;
  try {
    resp = await api(
      `/api/sessions/${encodeURIComponent(name)}/queued/flush`,
      { method: "POST" }
    );
    doc = await resp.json().catch(() => ({}));
  } catch {
    if (btn) { btn.disabled = false; btn.textContent = label; }
    return { note: "could not reach the daemon — nothing was delivered" };
  }
  if (btn) { btn.disabled = false; btn.textContent = label; }
  if (!resp.ok) {
    return { note: (doc && doc.error) || `HTTP ${resp.status}` };
  }
  if (doc.flushed > 0) return { doc, note: "" };
  // Nothing moved: the reason the backlog still has is the reason why.
  const q = doc.queued || {};
  let note;
  if (q.reason === "exited") {
    note = "nothing delivered — the session has exited";
  } else if (q.draft_open) {
    // The one hold this button deliberately does not overrule: typing the
    // backlog in now would splice it into the line still sitting in the
    // composer, wrecking both. Say whose keypress ends the wait instead of
    // leaving a button that looks broken.
    note = "nothing delivered — an unsent line is in the composer; send it " +
      "(Enter) or clear it (Ctrl-C) and this goes in right after";
  } else {
    note = "nothing delivered — the terminal cannot take a paste yet; it " +
      "will land on its own";
  }
  return { doc, note };
}

function queuedMsgRow(m) {
  const row = el("div", "tq-msg");
  const meta = el(
    "div", "tq-msg-meta",
    `${m.from} → ${m.handle} · via ${m.mesh}` +
    (m.type && m.type !== "say" ? ` · ${m.type}` : "") +
    (m.held_for !== null && m.held_for !== undefined
      ? ` · waiting ${fmtAge(m.held_for)}` : "")
  );
  if (m.id) meta.title = m.id;
  row.appendChild(meta);
  row.appendChild(el("div", "tq-msg-body", m.body || ""));
  return row;
}

function renderTermQueued(q) {
  const box = $("term-queued");
  const msgs = (q && q.messages) || [];
  // `reason` names a backlog's hold, so it is null while there is none; the
  // banner only ever draws with a backlog, so either field would do here —
  // but `state` is the one-word truth the daemon now always fills in, and
  // reading it is what keeps this strip and the header chip in agreement.
  const show = msgs.length > 0 && currentPage === "terminal" && !!currentName;
  // The banner and the terminal share a column, so appearing, disappearing
  // and folding all change the grid the session draws into — refit on any of
  // them, and only on them (a 2s repaint with the same shape must not).
  const sig = show ? (tqOpen ? `open:${msgs.length}` : "shut") : "hidden";
  const changed = box.dataset.sig !== sig;
  box.dataset.sig = sig;
  if (!show) {
    tqNote = "";   // the backlog is gone; so is anything said about it
    box.classList.add("hidden");
    box.innerHTML = "";
    if (changed) refitSoon(60);
    return;
  }
  box.innerHTML = "";
  // Opening the strip is still a full-width target (it works on a phone),
  // but it can no longer BE the whole strip: "deliver now" is a second,
  // differently-consequenced action and must not be reachable by the tap
  // that only meant "let me look". Siblings in a row, never nested.
  const row = el("div", "tq-head-row");
  const head = el("button", "tq-head");
  head.type = "button";
  head.title =
    "messages the mesh has accepted for this session but not yet typed " +
    "into this terminal";
  head.appendChild(el(
    "span", "tq-count",
    `⏸ ${msgs.length} queued message${msgs.length === 1 ? "" : "s"}`
  ));
  head.appendChild(el("span", "tq-reason", tqNote || queuedReason(q, true)));
  head.appendChild(el("span", "tq-toggle", tqOpen ? "hide ▴" : "show ▾"));
  head.addEventListener("click", () => {
    tqOpen = !tqOpen;
    renderTermQueued(q);
  });
  row.appendChild(head);
  const flush = el("button", "tq-flush", "deliver now");
  flush.type = "button";
  flush.title =
    "type these in immediately instead of waiting for the agent's turn to " +
    "end, a keyboard to fall quiet, or a hold to be lifted — an unsent line " +
    "in the composer is submitted first, never typed over";
  flush.addEventListener("click", async () => {
    const { note } = await flushQueued(currentName, flush);
    tqNote = note;
    refreshTermQueued();
  });
  row.appendChild(flush);
  box.appendChild(row);
  if (tqOpen) {
    const list = el("div", "tq-list");
    for (const m of msgs) list.appendChild(queuedMsgRow(m));
    box.appendChild(list);
  }
  // Loudest exactly when the reader is the reason: their own keystrokes are
  // what delivery is waiting out.
  box.classList.toggle("focus-hold", q.reason === "keyboard" && localTyping());
  box.classList.remove("hidden");
  if (changed) refitSoon(60);
}

async function refreshTermQueued() {
  const name = currentName;
  if (!name || currentPage !== "terminal") { renderTermQueued(null); return; }
  let q = null;
  try {
    const resp = await api(`/api/sessions/${encodeURIComponent(name)}/queued`);
    if (!resp.ok) { renderTermQueued(null); return; } // older daemon: no route
    q = await resp.json();
  } catch {
    return; // offline — the health poll owns saying so; keep the last truth
  }
  if (name !== currentName || currentPage !== "terminal") return;
  renderTermQueued(q);
  renderHoldChip(q);
}

/* ---- delivery chip (#term-hold) ----
   The answer to the question this page could not ask before: if a message
   arrived at this session right now, would it be typed in or queued — and,
   when it is queued, by what. The status badge beside it says what the
   session is DOING; the chip says what that means for anything trying to
   reach it, which is a different question and the one the operator staring
   at a quiet terminal is actually asking. It is a chip, not a banner,
   because the answer must be readable while the backlog is still empty —
   "nothing queued yet, because I have this session pinned shut" and
   "nothing queued, and the next arrival goes straight in" are the same
   empty backlog and opposite situations.

   Clicking toggles the manual hold: "type nothing in here until I say" —
   the case every automatic signal is blind to, since they all watch the
   keyboard and the turn and nobody is at the keyboard. A hold you cannot
   get out of from the panel that set it would be a trap, so the same click
   that set it unsets it. "deliver now" still goes through (see the banner):
   two instructions from the same person, and the later one is the live one.

   Fed by the same poll as the queued banner (refreshTermQueued), and kept
   to one sentence: the WHY, when a click would not fit it, is the banner's
   job — this only ever has to answer "is it coming in or not". */
let holdBusy = false;   // one toggle at a time; a double-click is one flip

/* One sentence for the chip, from the daemon's state word. The ladder here
   mirrors the delivery gate's order in _deliver_to, so the chip never names
   a hold the daemon is not applying. `draft` sharpens the keyboard hold the
   way the banner does — the fix for an unsent line (send it or clear it)
   is different from the fix for a recent keystroke (just wait). */
function holdChipText(q, msgs) {
  // The chip asks "would a message arriving right now get in", and a shut
  // door answers it more finally than any hold does: a held message is
  // still coming, a refused one was never taken. So congestion is read
  // first — except under the two states that are blunter still. `exited`
  // because nothing is being refused when there is no terminal, and `hold`
  // because this chip is also the button that un-pins, and a person who
  // pinned it must not be shown a sentence that hides their own doing.
  const bp = q.backpressure || {};
  const shut = !!bp.congested && q.state !== "exited";
  if (shut && q.state !== "hold") {
    return `delivery: REFUSING — inbox full (${msgs}/${bp.inbox_max})`;
  }
  switch (q.state) {
    case "exited":
      return "delivery: exited";
    case "hold":
      return shut
        ? `delivery: held — pinned, inbox full (${msgs}/${bp.inbox_max})`
        : msgs > 0 ? `delivery: held — pinned (${msgs} queued)` : "delivery: held — pinned";
    case "paced":
      return msgs > 0 ? `delivery: paced (${msgs})` : "delivery: paced";
    case "busy":
      return msgs > 0 ? `delivery: queued — busy (${msgs})` : "delivery: would queue — busy";
    case "keyboard":
      return q.draft_open
        ? (msgs > 0 ? `delivery: queued — unsent line (${msgs})` : "delivery: would queue — unsent line")
        : (msgs > 0 ? `delivery: queued — typing (${msgs})` : "delivery: would queue — typing");
    default:
      return msgs > 0 ? `delivery: live (${msgs} queued)` : "delivery: live";
  }
}

function renderHoldChip(q) {
  const chip = $("term-hold");
  if (!chip) return;   // markup from before the chip existed
  if (!chip.dataset.holdBound) {
    // Bound here, not at load: the chip lives in the terminal page's markup,
    // and a load-time `$("term-hold")` throws the moment app.js is evaluated
    // with that page absent (the stub DOMs in tests/web, which carry only the
    // elements they exercise). First render is also first real use.
    chip.dataset.holdBound = "1";
    chip.addEventListener("click", toggleHold);
  }
  const show = !!q && currentPage === "terminal" && !!currentName;
  if (!show) {
    chip.classList.add("hidden");
    holdBusy = false;
    return;
  }
  const msgs = ((q && q.messages) || []).length;
  chip.textContent = holdChipText(q, msgs);
  chip.className = "term-btn hold-chip";
  chip.classList.toggle("hold-pinned", q.state === "hold");
  chip.classList.toggle("hold-blocked", q.state !== "settling" && q.state !== "hold");
  // Its own colour, not the amber every other hold shares: those all end by
  // themselves, and this one is turning other sessions' messages away until
  // this terminal reads what it has.
  chip.classList.toggle(
    "hold-shut", !!(q.backpressure || {}).congested && q.state !== "exited"
  );
  chip.disabled = holdBusy;
  chip.title = chipTitle(q);
}

function chipTitle(q) {
  const pinned = q.state === "hold";
  const lines = [
    "would a message arriving right now be typed in, or queued — and by what.",
    "Click to " + (pinned
      ? "RESUME: let deliveries type in again (the backlog goes first)."
      : "HOLD: type nothing in here until you say — even while you are reading, hands off the keys."),
  ];
  if (pinned && (q.messages || []).length) {
    lines.push("The queued strip below is what is waiting; \"deliver now\" there still goes through.");
  }
  // Said here rather than in the chip's one line: the count of senders
  // turned away is the fact that explains a backlog which has stopped
  // growing, and it is worth more room than the chip has.
  const bp = q.backpressure || {};
  if (bp.congested) {
    lines.push(
      `At capacity: ${bp.queued} waiting, cap ${bp.inbox_max}. New messages ` +
      "for this session are REFUSED — their senders are told to wait and " +
      "re-send, and nothing of theirs is queued here."
    );
  }
  if (bp.refused) {
    const who = (bp.refused_from || [])
      .map((r) => `${r.from}×${r.count}`).join(", ");
    lines.push(
      `${bp.refused} turned away in the last 10 min` + (who ? `: ${who}.` : ".")
    );
  }
  return lines.join("\n");
}

async function toggleHold() {
  const chip = $("term-hold");
  if (holdBusy) return;
  holdBusy = true;
  if (chip) chip.disabled = true;
  let q = null;
  try {
    const resp = await api(
      `/api/sessions/${encodeURIComponent(currentName)}/queued/hold`,
      { method: "POST", headers: { "content-type": "application/json" }, body: "{}" }
    );
    if (resp.ok) {
      const doc = await resp.json().catch(() => ({}));
      q = doc.queued || null;
    }
    // not ok: older daemon without the route — fall through and re-poll,
    // so the chip tells the truth it can rather than the one it wished for
  } catch {
    // offline — the health poll owns saying so; re-poll to keep the last truth
  }
  holdBusy = false;
  if (q) { renderTermQueued(q); renderHoldChip(q); }
  else refreshTermQueued();
}
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
    setRenderFont(fontSize);
    // The cell got bigger or smaller, so the grid the box holds did too —
    // refit rather than leave the session sized for the old glyph. Straight
    // away, not debounced: this one came from a deliberate press.
    if (canFit()) localFit();
  }
  syncZoomControls();
}

/* The floor for fitView's shrinking, well below FONT_MIN: FONT_MIN is where
   reading stops being comfortable, this is where a 384-column attach grid
   still fits a browser panel whole. Unreadably small beats a scrollbar —
   the grid this size is another viewer's, and this box only surveys it. */
const VIEW_FONT_MIN = 4;

/* What xterm renders with, as distinct from `fontSize`, the size the reader
   chose: fitView may take the rendering below the choice to fit a foreign
   grid, and localFit restores the choice before measuring the box. */
function setRenderFont(px) {
  if (term && term.options.fontSize !== px) term.options.fontSize = px;
}

/* Make the grid as it IS fit the box as it is — by shrinking glyphs, never
   by resizing the session. This is the viewer's move when the grid belongs
   to someone else (another viewer claimed the size); the promise it keeps is
   that the terminal never draws outside its box, so the web page never
   grows a scrollbar. When the grid already fits at the reader's chosen font
   size, this is a no-op at that size. */
function fitView() {
  if (!term || !fitAddon || !terminalOnScreen()) return;
  let size = fontSize;
  setRenderFont(size);
  let p = fitAddon.proposeDimensions();
  while (p && size > VIEW_FONT_MIN && (p.cols < term.cols || p.rows < term.rows)) {
    size -= 1;
    setRenderFont(size);
    p = fitAddon.proposeDimensions();
  }
}

/* The opposite move: size the SESSION to this box, at the reader's chosen
   font size. Every fit() must pass through here — a fit measured while
   fitView has the glyphs shrunk would claim a grid far wider than the
   reader can read. */
function localFit() {
  if (!canFit()) return;
  setRenderFont(fontSize);
  fitAddon.fit();
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

/* ---- per-session layout ----
   Two choices a reader makes about a session's screen and expects to find
   again: which panel the right rail shows (the details, or the workflow run),
   and whether the run page is halved into the bottom of the terminal's
   column — with where they left the bar between the two. Remembered per
   SESSION, unlike the font size: "watch s15's run under its terminal" is a
   fact about s15, not about the reader, and walking to another session must
   not drag it along. One key with a map inside rather than a key per
   session: sessions come and go too fast to each own a localStorage row, and
   an entry left behind by a deleted session is a few bytes of nothing. */
const SESSLAYOUT_KEY = `claunch_sesslayout:${BASE}`;
const SPLIT_DEFAULT = 0.6;   // the terminal's share of the column
const SPLIT_MIN = 0.2;       // past either end the loser is too short to read
const SPLIT_MAX = 0.8;

function clampSplitRatio(x) {
  if (!Number.isFinite(x)) return SPLIT_DEFAULT;
  return Math.min(SPLIT_MAX, Math.max(SPLIT_MIN, Math.round(x * 1000) / 1000));
}

function loadSessLayouts() {
  // Junk in the key (hand-edited, or another tool's) must not brick the page.
  try {
    const doc = JSON.parse(localStorage.getItem(SESSLAYOUT_KEY) || "{}");
    return doc && typeof doc === "object" && !Array.isArray(doc) ? doc : {};
  } catch {
    return {};
  }
}

/* Always whole and always sane, whatever the key holds: junk falls back
   field by field rather than as a lump, so a broken ratio does not cost the
   radio its choice. */
function sessLayoutFor(name) {
  const raw = (name && loadSessLayouts()[name]) || {};
  return {
    rail: raw.rail === "wf" ? "wf" : "detail",
    split: !!raw.split,
    ratio: clampSplitRatio(Number(raw.ratio)),
  };
}

function setSessLayout(name, patch) {
  if (!name) return;
  const all = loadSessLayouts();
  // Through the reader on the way in, so what is stored is already whole —
  // the next read never depends on this patch having been complete.
  all[name] = { ...sessLayoutFor(name), ...patch };
  localStorage.setItem(SESSLAYOUT_KEY, JSON.stringify(all));
  syncSplitPane();
}

/* ---- who owns the wheel ----

   Three regimes, and the whole trick is telling them apart before spending a
   tick.

   1. The program took the mouse. claude does — it asserts `?1000h ?1002h
      ?1003h ?1006h` behind the alternate screen and never lets go — and that
      is a program saying "send me the wheel, I scroll myself". It does, from
      its own model, to a depth no terminal keeps. This is the common case and
      it wants exactly one thing from us: to get out of the way. xterm.js
      already encodes the SGR report; returning true lets it.

   2. The main buffer, no mouse. A plain shell, a build log. Here xterm's OWN
      scrollback is the right answer, and the terminal is built with one (see
      buildSessionTerm) seeded from the daemon at attach — so, again, hands
      off: native scrolling, a real scrollbar, momentum, find-in-page.

   3. The alternate screen with the mouse left alone. A pager that reads arrow
      keys. Nothing scrolls off the alt buffer, so neither xterm's scrollback
      nor a native wheel has anything to move; the daemon's history window is
      all there is, and the `scroll` control below serves it.

   Only (3) is ours. Until now every tick went through (3)'s path, including
   claude's — which is why the wheel felt dead: the daemon spends ~18 ms and
   22 KB repainting a 233x77 grid per tick to move a history that, measured on
   real sessions, is one or two lines deep. */
function wheelBelongsToProgram() {
  // xterm's own view of the modes, learned from the byte stream — including
  // the re-assertion the daemon puts in every repaint, so a terminal that
  // attached mid-session knows as much as one that watched it start.
  const m = term && term.modes;
  if (m && m.mouseTrackingMode && m.mouseTrackingMode !== "none") return true;
  // The daemon's `init.mouse` / `mouse` frames, as the fallback for an xterm
  // that has not parsed the assertion yet (the flag arrives with `init`,
  // ahead of the repaint that carries the escapes).
  return mouseTracking;
}

/* True when the wheel is xterm's to spend on its own scrollback: the main
   buffer, where a seeded scrollback actually holds something. */
function wheelIsNative() {
  return !altScreen;
}

function handleWheel(e) {
  if (wheelBelongsToProgram() || wheelIsNative()) {
    // Not ours. No preventDefault, no accumulator, no control frame — xterm
    // forwards the mouse report, or scrolls its own buffer, natively.
    return true;
  }
  e.preventDefault();
  let delta = e.deltaY;
  if (e.deltaMode === 2) {                 // DOM_DELTA_PAGE
    delta = e.deltaY * (term ? term.rows : 24);
  } else if (e.deltaMode !== 1) {          // DOM_DELTA_PIXEL (mouse/touchpad)
    delta = e.deltaY / WHEEL_LINE_PX;
  }
  // Coalesce bursts (touchpads emit many small deltas) into one control.
  wheelAccum += delta;
  clearTimeout(wheelTimer);
  wheelTimer = setTimeout(flushWheel, 50);
  return false;                            // xterm's arrow-key fallback: skipped
}

function flushWheel() {
  const lines = Math.round(wheelAccum);
  wheelAccum = 0;
  wheelTimer = null;
  if (!lines) return;
  // Wheel down (deltaY > 0) moves toward the live bottom, which is a
  // negative offset motion; the daemon clamps at 0 either way.
  sendScroll(-lines);
}

function sendScroll(lines) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "scroll", lines }));
  }
}

/* The header says why the terminal is not advancing while the session keeps
   running: the viewer scrolled into history, and the wheel below it (or a
   keystroke) is the way back to live.

   Only the daemon-served regime gets a chip. When the program owns the wheel
   it is scrolling its own view and the terminal is not frozen at all — there
   is nothing to explain and nothing to come back from — and when xterm owns
   it the scrollbar is the affordance, which is the whole point of giving the
   wheel back. */
function updateScrollChip() {
  const chip = $("term-scroll");
  if (!chip) return;
  if (scrollOffset > 0) {
    chip.textContent = "history";
    chip.title = "scrolled back — wheel down, or type, to return to live";
    chip.classList.remove("hidden");
  } else {
    chip.classList.add("hidden");
  }
}

/* ---- kept-alive terminals ----

   Binding the terminal to a session used to mean tearing the old one down
   and building the new one up — a new xterm object, a new WebSocket, and the
   daemon repainting its whole screen over the line — and the reader toggling
   between two sessions paid that cost on every hop. Now the terminals you
   have visited are kept alive and simply parked: the xterm object, its
   element (hidden in place, never moved) and the socket underneath all stay
   up, and the socket's handler is swapped for a passive shim that keeps the
   hidden buffer current without letting any of its frames speak for the
   viewer actually looking. Coming back to a parked session is a swap of
   state and a re-fit — no socket, no repaint — and costs this browser
   nothing it was not already spending, since the daemon broadcasts every
   terminal's output to all of its viewers anyway. The one thing that still
   pays the old cost is a socket that died while parked: it reconnects (and
   repaints) exactly once, on the way back in.

   One copy per session, and the least recently visited is dropped when the
   cache fills — a parked terminal is a pair of eyes the user is not wearing,
   so it gets a budget. A session that stops existing (killed, cleared) drops
   its parked terminal on the next poll, and a session respawned under the
   same name is re-followed by the same pid test the live link already uses. */
const TERM_CACHE_MAX = 3;      // the on-screen terminal plus this many parked
const keptTerms = new Map();   // session name -> parked {term, fitAddon, ws, ...}

function park(b) {
  if (b.term && b.term.element) b.term.element.style.display = "none";
}
function unpark(b) {
  if (b.term && b.term.element) b.term.element.style.display = "";
}

/* Park the active terminal. The socket's handlers are swapped for the shim
   so nothing it says — a status change, an exit, a late `close` — can touch
   the live machine, the element hides, and the live globals reset to nil.
   The terminal and socket stay up, which is what makes the return cheap. */
function suspendActive() {
  if (!term) return;
  const b = {
    name: currentName, term, fitAddon, ws,
    pid: attachedPid, boot: attachedBoot,
    alt: altScreen, mouse: mouseTracking,
    scroll: scrollOffset, exited: sessionEnded,
  };
  if (ws) {
    ws.onopen = null;
    ws.onclose = null;
    ws.onmessage = (ev) => shimFrame(b, ev);
  }
  if (keptTerms.has(b.name)) dropKept(b.name);   // one parked copy per session
  keptTerms.set(b.name, b);
  while (keptTerms.size > TERM_CACHE_MAX - 1) {
    dropKept(keptTerms.keys().next().value);     // the least recently parked
  }
  park(b);
  resetLive();
}

/* The live machine, emptied. Everything the non-terminal code reads is
   still a global, so it needs to point at nothing at all between terminals
   rather than at the one being parked. */
function resetLive() {
  if (wheelTimer) { clearTimeout(wheelTimer); wheelTimer = null; }
  wheelAccum = 0;
  linkTimer = null;
  linkTry = 0;
  linkTicket += 1;               // sockets opened before are no one's link now
  linkName = null;
  linkQueue = [];
  sessionEnded = false;
  lastLocalKey = 0;
  attachedPid = null;
  attachedBoot = null;
  scrollOffset = 0;
  altScreen = false;
  mouseTracking = false;
  applyingRemoteResize = false;
  currentName = null;
  term = null;
  fitAddon = null;
  ws = null;
  setLink("idle");
  updateScrollChip();
}

/* Dispose a parked terminal for good. The party it belonged to has gone
   away, so neither its socket nor its xterm object may speak again. */
function dropKept(name) {
  const b = keptTerms.get(name);
  if (!b) return;
  keptTerms.delete(name);
  if (b.ws) {
    b.ws.onopen = null;
    b.ws.onmessage = null;
    b.ws.onclose = null;
    try { b.ws.close(); } catch { /* already closed */ }
  }
  if (b.term) {
    const el = b.term.element;
    if (el && el.parentNode) el.parentNode.removeChild(el);
    b.term.dispose();
  }
}

/* The passive handler a parked terminal's socket runs. Binary output keeps
   refreshing the hidden buffer — that is the whole point of leaving the
   socket up — and the control frames update only the parked state so the
   terminal is correct when it comes back. Nothing here touches the live
   machine: the badge, the chip, the scroll affordance and the retry belong
   to the viewer that is actually looking. */
function shimFrame(b, ev) {
  if (typeof ev.data === "string") {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "init") {
      b.pid = msg.pid || null;
      b.boot = msg.boot_id || null;
      b.alt = !!msg.alt;
      b.mouse = !!msg.mouse;
      // The same flag the live machine reads, for the same reason:
      // restoreTerminal opens a fresh socket for a parked terminal whose
      // session has not ended, and doing that to one that has is where the
      // loop above starts.
      if (msg.exited) b.exited = true;
    } else if (msg.type === "buffer") {
      b.alt = !!msg.alt;
    } else if (msg.type === "mouse") {
      b.mouse = !!msg.tracking;
      if (b.mouse) b.scroll = 0;
    } else if (msg.type === "scrolled") {
      b.scroll = msg.offset || 0;
    } else if (msg.type === "resize") {
      if (b.term) b.term.resize(msg.cols, msg.rows);
    } else if (msg.type === "exit") {
      b.exited = true;
      b.alt = false;
      b.mouse = false;
      b.scroll = 0;
      if (b.term) {
        b.term.write(
          `\r\n\x1b[90m[session exited (code ${msg.code})] ` +
          `- press "resume" above to relaunch it\x1b[0m\r\n`
        );
      }
    }
    // state and pong: the badge is the viewer's, and there is nothing to
    // acknowledge for a terminal nobody is watching.
  } else if (b.term) {
    b.term.write(new Uint8Array(ev.data));
  }
}

/* The handlers a live socket runs, bound by identity rather than the ticket
   the fresh-connect path uses: a socket that was parked and has come back
   keeps its old ticket number, so each event instead asks "am I still the
   socket being looked at?". Same shape as openSocket's wiring, so a restored
   link heals exactly the way a fresh one does. */
function wireActive(b) {
  const sock = b.ws;
  sock.onopen = () => {
    if (ws !== sock) return;
    linkTry = 0;
    setLink("live");
  };
  sock.onmessage = (ev) => {
    if (ws !== sock) return;
    if (typeof ev.data === "string") {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      handleFrame(msg);
    } else if (term) {
      term.write(new Uint8Array(ev.data));
    }
  };
  sock.onclose = () => {
    if (ws !== sock) return;
    ws = null;
    if (linkState === "idle" || sessionEnded) { setLink("idle"); return; }
    linkDown();
  };
}

/* Bring a parked terminal back. The swap happens in the globals, which every
   other part of the page already reads, so the header, the badge, the wheel
   and the link all see this terminal without knowing a swap happened. A
   socket still open is re-wired to the live machine; one that died while
   parked connects fresh, paying the old reconnect cost exactly once. */
function restoreTerminal(b) {
  keptTerms.delete(b.name);
  currentName = b.name;
  term = b.term;
  fitAddon = b.fitAddon;
  ws = b.ws;
  attachedPid = b.pid;
  attachedBoot = b.boot;
  scrollOffset = b.scroll;
  altScreen = b.alt;
  mouseTracking = !!b.mouse;
  sessionEnded = b.exited;
  // The same header seeding a fresh attach does, so the previous session's
  // controls never linger on this one.
  showView("terminal");
  $("term-title").textContent = b.name;
  renderTermHandle();
  setStatusBadge((sessionsCache.find((s) => s.name === b.name) || {}).status || "starting");
  document.querySelectorAll("#session-list li").forEach((li) =>
    li.classList.toggle("active", li.dataset.name === b.name)
  );
  markDetailRow();
  unpark(b);
  if (ws && ws.readyState === WebSocket.OPEN && !sessionEnded) {
    wireActive(b);
    linkName = b.name;          // resetLive cleared it; a live socket's retries need it
    setLink("live");
  } else if (!sessionEnded) {
    openSocket(b.name);
  } else {
    setLink("idle");
  }
  refitSoon(50);
}

/* ---- the CLI tab: one raw shell, one terminal ----

   The daemon keeps exactly one unmanaged shell for its lifetime (see
   daemon/clipty.py), so this page's xterm and socket are also built exactly
   once and kept: navigating away hides the page but leaves the socket up,
   so the shell's output keeps flowing into this terminal's buffer and
   nothing is lost for the viewer coming back. The frame dialect is the
   session terminal's, thinned: init (which carries `exited`, because a
   viewer can land on a shell that is already dead), exit, resize, and the
   raw bytes themselves. No scrollback negotiation — the wheel browses
   xterm's own scrollback, so this terminal gets one.

   STATE: cliStatus says what this page is looking at — "connecting", "live",
   "exited" (the shell stopped; the header offers restart) or "down" (the
   daemon itself stopped answering; the header offers retry). */
let cliTerm = null;
let cliFit = null;
let cliWs = null;
let cliStatus = "off";
let cliRetry = 0;
let cliRemoteResize = false;
let cliFitTimer = null;

/* The header's one control; what it does is named by its label — restart an
   exited shell, retry a dead link. */
function cliAct() {
  if (cliStatus === "exited" && cliWs && cliWs.readyState === WebSocket.OPEN) {
    cliWs.send(JSON.stringify({ type: "restart" }));
  } else if (cliStatus === "down") {
    ensureCliSocket();
  }
}

function cliSetStatus(state) {
  cliStatus = state;
  $("cli-status").className = "badge " + (state === "live" ? "idle" : "exited");
  $("cli-status").textContent =
    state === "live" ? "live"
    : state === "exited" ? "exited"
    : state === "down" ? "offline"
    : state === "connecting" ? "…"
    : "off";
  const act = $("cli-act");
  act.classList.toggle("hidden", state !== "exited" && state !== "down");
  act.textContent = state === "exited" ? "restart" : state === "down" ? "retry" : "";
  act.title = state === "exited"
    ? "the shell exited — start a fresh one"
    : "the daemon stopped answering — try the socket again";
}

/* Enter the CLI page. The terminal object IS the page's content and is
   never rebuilt — like the session terminals, it is hidden in place on the
   way out and simply shown again here. */
function openCli() {
  showView("cli");
  if (!cliTerm) buildCliTerm();
  ensureCliSocket();
  cliRefit(0);
}

function buildCliTerm() {
  cliTerm = new Terminal({
    fontFamily: "Cascadia Mono, Consolas, Menlo, monospace",
    fontSize: fontSize,
    theme: { background: "#14161a" },
    // The session terminal runs scrollback: 0 because the daemon serves the
    // wheel from its own history. A raw shell has no daemon-side screen —
    // the ring the daemon keeps is only catch-up on attach — so this
    // terminal keeps xterm's own scrollback and the wheel browses it.
    scrollback: 5000,
  });
  cliFit = new FitAddon.FitAddon();
  cliTerm.loadAddon(cliFit);
  cliTerm.open($("cli-term"));
  cliTerm.onData((data) => {
    if (cliWs && cliWs.readyState === WebSocket.OPEN && cliStatus !== "exited") {
      // A binary frame, on purpose: the daemon reads TEXT frames as JSON
      // control messages, and a keystroke must never be mistaken for one.
      cliWs.send(new TextEncoder().encode(data));
    }
  });
  // A resize we applied from a server broadcast must not be echoed back, or
  // two viewers ping-pong forever (the session terminal's rule, verbatim).
  cliTerm.onResize(({ cols, rows }) => {
    if (cliRemoteResize) return;
    if (cliWs && cliWs.readyState === WebSocket.OPEN) {
      cliWs.send(JSON.stringify({ type: "resize", cols, rows }));
    }
  });
  $("cli-act").addEventListener("click", cliAct);
  cliRefit(0);
}

/* Open (or re-open) the socket. One per page lifetime: a close only ever
   means "try again", never "point at a different child" — the daemon names
   the shell, the tab just looks at it. */
function ensureCliSocket() {
  if (cliWs) {
    if (cliWs.readyState === WebSocket.OPEN || cliWs.readyState === WebSocket.CONNECTING) return;
    cliWs = null;   // closed: fall through to a fresh socket
  }
  cliSetStatus("connecting");
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const sock = new WebSocket(`${proto}://${location.host}${url("/api/cli/ws")}`);
  sock.binaryType = "arraybuffer";
  cliWs = sock;
  sock.onopen = () => {
    if (cliWs !== sock) return;
    cliRetry = 0;
  };
  sock.onmessage = (ev) => {
    if (cliWs !== sock) return;
    if (typeof ev.data === "string") {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "init") {
        cliRetry = 0;
        // The daemon's truth on arrival: a fresh shell (first viewer of
        // this daemon incarnation) or an already-dead one (only the restart
        // control brings it back).
        if (cliTerm) {
          cliRemoteResize = true;
          try { cliTerm.resize(msg.cols, msg.rows); }
          finally { cliRemoteResize = false; }
        }
        if (msg.exited) {
          if (cliTerm) cliTerm.write(
            "\r\n\x1b[90m[shell exited — restart it from the header]\x1b[0m\r\n");
          cliSetStatus("exited");
        } else {
          cliSetStatus("live");
          cliRefit(60);   // then claim this viewer's own size
        }
      } else if (msg.type === "exit") {
        if (cliTerm) cliTerm.write(
          `\r\n\x1b[90m[shell exited (code ${msg.code}) — restart it from the header]\x1b[0m\r\n`);
        cliSetStatus("exited");
      } else if (msg.type === "resize") {
        // Another viewer claimed the size; take it unless it is the echo of
        // this viewer's own claim.
        if (cliTerm && (cliTerm.cols !== msg.cols || cliTerm.rows !== msg.rows)) {
          cliRemoteResize = true;
          try { cliTerm.resize(msg.cols, msg.rows); }
          finally { cliRemoteResize = false; }
        }
      }
      // shutdown, pong and friends: nothing this page needs to hold.
    } else if (cliTerm) {
      cliTerm.write(new Uint8Array(ev.data));
    }
  };
  sock.onclose = () => {
    if (cliWs !== sock) return;
    cliWs = null;
    if (cliStatus === "exited") return;   // the shell stopped, not the link
    if (cliRetry < 8) {
      cliRetry += 1;
      cliSetStatus("connecting");
      setTimeout(ensureCliSocket, 1000 * cliRetry);
    } else {
      cliSetStatus("down");   // gave up: from here the header's retry does it
    }
  };
}

/* Fit the CLI terminal to its box at the reader's font size — the session
   terminal's localFit, minus the zoom pill this page has no room for.
   Delayed so a page swap has landed before the measurement. */
function cliRefit(delay = 60) {
  if (!cliFit || !cliTerm) return;
  clearTimeout(cliFitTimer);
  cliFitTimer = setTimeout(() => {
    if (currentPage !== "cli") return;   // hidden boxes measure to nothing
    if (cliTerm.options.fontSize !== fontSize) cliTerm.options.fontSize = fontSize;
    cliFit.fit();
  }, delay);
}

/* Build a new terminal for a session that has not been up before (or whose
   parked copy was evicted). This is the pre-cache attach() body — the cost a
   session switch used to always pay. */
function freshAttach(name) {
  currentName = name;
  // showView first, and before the terminal is opened: on mobile #main is
  // display:none while the rail is up, and a terminal opened into a
  // zero-height box fits to nothing.
  showView("terminal");
  $("term-title").textContent = name;
  renderTermHandle();
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
    // A real scrollback, like the CLI tab's. It used to be 0, on the reading
    // that the daemon's history served the wheel instead — but that history
    // is one or two lines deep for a session running claude (it repaints the
    // grid rather than scrolling it), so the trade bought nothing and cost
    // the browser's own scrolling: the scrollbar, the momentum, the touch
    // drag, PgUp/Home, find-in-page, selection across more than one screen.
    // On the alternate screen xterm keeps this empty by construction, which
    // is right — there the program owns the wheel (see handleWheel). This is
    // for the main buffer: a plain shell, a build log, a session after its
    // TUI has exited.
    scrollback: 5000,
  });
  fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open($("terminal"));
  localFit();

  // Wired once, for the life of this terminal object: the link swaps sockets
  // underneath these, and a reconnect must not leave a second pair behind.
  term.onData(sendInput);
  watchComposer(term);
  term.onResize(({ cols, rows }) => {
    // A resize we applied from a server broadcast must not be echoed back, or
    // two viewers (or a stale echo over a high-latency relay) ping-pong forever.
    if (applyingRemoteResize) return;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "resize", cols, rows }));
    }
  });

  // The wheel handler belongs to this Terminal instance — freshAttach()
  // builds a new one every time, and the parked copy it replaced keeps its
  // own, hidden with it.
  term.attachCustomWheelEventHandler(handleWheel);

  openSocket(name);
}

/* Bind the terminal to a session. Called only by the #/s/<name> route, so
   the attached session is in the URL: a reload, a bookmark or a shared link
   lands back on the same terminal instead of on an empty slot. A session we
   have parked comes back by swap; anything else is a fresh attach. */
function attach(name) {
  stopWfPoll();
  stopMeshPoll();
  // The send-keys input belongs to the session it is attached to: switching
  // sessions must not hand one session's half-typed line to the next. Guarded
  // (like #term-input below) for the whole-block harnesses that boot this
  // function without the input's element in their stub DOM.
  const termInputField = $("term-input-field");
  if (termInputField) termInputField.value = "";
  // The common hop: this session has been up before, so bring its parked
  // terminal back instead of building a new one — no socket, no repaint.
  if (name !== currentName) {
    const kept = keptTerms.get(name);
    if (kept) {
      // Out of the cache first: parking the session we are leaving may have to
      // evict the least recently parked to stay in budget, and tonight that
      // oldest one is exactly the terminal about to come back. Take it out of
      // reach before the park, bring it back after.
      keptTerms.delete(name);
      suspendActive();
      restoreTerminal(kept);
      return;
    }
  }
  suspendActive();
  // A fresh attach supersedes any parked copy of the same name — a respawn-
  // follow reattaching the very session being watched does this, so the old
  // child's parked terminal does not come back in its place.
  dropKept(name);
  freshAttach(name);
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
  fitTimer = setTimeout(localFit, delay);
}
window.addEventListener("resize", () => {
  refitSoon();
  if (currentPage === "cli") cliRefit(150);
});

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
  localFit(); // fires term.onResize -> server resize when dims changed
  ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
  ws.send(JSON.stringify({ type: "repaint" }));
}
window.addEventListener("focus", resyncTerminal);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) resyncTerminal();
});

/* The CLI tab's half of the same two wake-ups: the daemon is not watching
   its grid and a backgrounded tab's socket may have died quietly, so coming
   back to the CLI page re-fits the terminal (which sends the daemon this
   viewer's size) and re-opens a dead socket. */
function resyncCli() {
  if (currentPage !== "cli") return;
  if (cliStatus === "down") { ensureCliSocket(); return; }
  if (!cliTerm) return;
  if (cliWs && cliWs.readyState !== WebSocket.OPEN) ensureCliSocket();
  cliRefit(0);
}
window.addEventListener("focus", resyncCli);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) resyncCli();
});

/* ---- the transcript page (#/log/<name>) ----

   What the terminal cannot answer: "what did this session say an hour ago".
   claude repaints the alternate screen every frame instead of scrolling it,
   so nothing scrolls off into any scrollback — measured on this fleet's own
   sessions, four hundred kilobytes of output leaves two lines behind in the
   daemon's history — and no amount of wheel plumbing over that history was
   ever going to find the rest. It is not in the pipe. It is on disk, in the
   conversation jsonl claude keeps, and the daemon serves it in pages
   (/api/sessions/<name>/transcript).

   A page of its own, not a pane folded over the terminal. A session already
   has more than one reading: the terminal is what it is DOING, the run page
   (#/wf) is where it has GOT TO, the trace (#/msg) is who it has TALKED to.
   This is the fourth: what it SAID. Making it a route rather than a toggle is
   what puts it in that company — it becomes a link you can send, a place the
   back button returns to, and a page the view system parks and unparks like
   any other (route() stops its poll on the way out, syncLayout gives it the
   phone's page slot).

   And it reaches further than the toggle could. The material is claude's
   jsonl on disk, not the live PTY, and the daemon hands a record out for any
   session it still knows — so an EXITED session's conversation reads exactly
   like a running one's. The old pane could not show that at all: it had to be
   opened over an attached terminal, and a dead session has none.

   The scroller itself is deliberately an ordinary div with `overflow-y: auto`.
   That is the whole feature: the browser owns the wheel, so a scrollbar,
   momentum, a touch drag, PgUp/Home/End, find-in-page and selection across
   the whole conversation all come for free and none of them cost a frame of
   daemon time. Older pages are fetched as the reader nears the top; while
   they sit at the bottom the poll follows the session forward. */
const TRANSCRIPT_PAGE = 40;
const TRANSCRIPT_NEAR_TOP = 400;   // px from the top that triggers an older page
const TRANSCRIPT_NEAR_END = 40;    // px from the bottom that still counts as "live"
let transcriptName = null;         // the session whose conversation this page is
let transcriptCursor = null;       // oldest seq loaded; the next page ends here
let transcriptSeen = -1;           // newest seq loaded, for the follow-forward
let transcriptMore = false;        // is there anything above what is loaded
let transcriptBusy = false;        // one fetch at a time, or a flick sends ten

/* Open the page for a session — the route's entry point. Re-entering the one
   already on screen is a no-op rather than a reload: coming back from the
   terminal is the common case, and re-reading would throw away where the
   reader had scrolled to. */
function openTranscript(name) {
  showView("log");
  $("log-title").textContent = name;
  const back = $("log-back");
  if (back) back.href = `#/s/${encodeURIComponent(name)}`;
  if (transcriptName === name) return;
  transcriptName = name;
  transcriptCursor = null;
  transcriptSeen = -1;
  transcriptMore = false;
  const pane = $("term-log-pane");
  if (pane) {
    pane.innerHTML = "";
    pane.scrollTop = 0;
    if (!pane.dataset.wired) {
      pane.dataset.wired = "1";
      pane.addEventListener("scroll", onTranscriptScroll);
    }
  }
  loadTranscriptPage({ older: false });
  startTranscriptPoll();
}

/* Leaving the page. Called centrally by route(), like every other page's
   stop*, so the poll never outlives the view that reads it. The name is
   forgotten too: a conversation moves on while you are away, and coming back
   should land at the bottom rather than at a cursor into a stale page. */
function closeTranscript() {
  stopTranscriptPoll();
  transcriptName = null;
  transcriptBusy = false;
}

function transcriptIsOpen() {
  return !!transcriptName;
}

/* Near the top, reach for the page above. Nothing else: this fires on every
   frame of a scroll, and the browser is doing the scrolling. */
function onTranscriptScroll() {
  const pane = $("term-log-pane");
  if (!pane || !transcriptIsOpen()) return;
  if (pane.scrollTop < TRANSCRIPT_NEAR_TOP && transcriptMore && !transcriptBusy) {
    loadTranscriptPage({ older: true });
  }
}

function transcriptAtEnd(pane) {
  return pane.scrollHeight - pane.scrollTop - pane.clientHeight < TRANSCRIPT_NEAR_END;
}

/* One page, prepended (older) or appended (the tail, and the follow-forward).

   Prepending has to hold the reader still: the browser measures scrollTop
   from the top of the content, so inserting above them would slide the text
   they are reading down by exactly the height of what arrived. Taking the
   height before and after and restoring the difference is what makes an
   infinite scroller feel like a long page instead of a trapdoor. */
async function loadTranscriptPage(opts) {
  const older = !!(opts && opts.older);
  const name = transcriptName;
  if (!name || transcriptBusy) return;
  transcriptBusy = true;
  const pane = $("term-log-pane");
  if (pane && !pane.children.length) {
    pane.appendChild(el("div", "log-note", "reading the conversation…"));
  }
  try {
    const q = `limit=${TRANSCRIPT_PAGE}`
      + (older && transcriptCursor !== null ? `&before=${transcriptCursor}` : "");
    const res = await api(`/api/sessions/${encodeURIComponent(name)}/transcript?${q}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    // The reader may have closed it, or walked to another session, while this
    // was in flight; its answer is not theirs any more.
    if (transcriptName !== name) return;
    renderTranscriptPage(data, older);
  } catch (err) {
    if (transcriptName !== name) return;
    const box = $("term-log-pane");
    if (box && !box.querySelector(".log-rec")) {
      box.innerHTML = "";
      box.appendChild(el("div", "log-note",
                         `could not read the conversation — ${err.message}`));
    }
  } finally {
    transcriptBusy = false;
  }
}

function renderTranscriptPage(data, older) {
  const pane = $("term-log-pane");
  if (!pane) return;
  const note = pane.querySelector(".log-note");
  if (note) note.remove();

  const records = (data.records || []).filter(
    (r) => older || r.seq > transcriptSeen
  );
  if (older) {
    transcriptMore = !!data.has_more;
    transcriptCursor = data.cursor === undefined ? transcriptCursor : data.cursor;
  } else {
    // The first page also establishes the top cursor; a follow-forward must
    // not move it, or scrolling up would re-fetch from the wrong place.
    if (transcriptCursor === null) {
      transcriptCursor = data.cursor === undefined ? null : data.cursor;
      transcriptMore = !!data.has_more;
    }
  }
  for (const r of records) {
    if (r.seq > transcriptSeen) transcriptSeen = r.seq;
  }

  if (!records.length) {
    if (!pane.querySelector(".log-rec")) {
      pane.appendChild(el("div", "log-note", data.source
        ? "this conversation has nothing to show yet"
        : "no conversation on file for this session"));
    }
    return;
  }

  const frag = document.createDocumentFragment();
  for (const r of records) frag.appendChild(renderTranscriptRecord(r));

  if (older) {
    const before = pane.scrollHeight;
    const top = pane.scrollTop;
    pane.insertBefore(frag, pane.firstChild);
    pane.scrollTop = top + (pane.scrollHeight - before);
  } else {
    const follow = !pane.querySelector(".log-rec") || transcriptAtEnd(pane);
    pane.appendChild(frag);
    // Only if they were already at the bottom. A reader who has scrolled up
    // to read something is not asking to be dragged back down every time the
    // session says another word.
    if (follow) pane.scrollTop = pane.scrollHeight;
  }
}

function renderTranscriptRecord(r) {
  // The role comes off the jsonl, and claude writes every tool_result as a
  // user-role record — the API's shape, not the reader's. The output belongs
  // to the assistant's tool exchange, so a user record whose blocks are all
  // tool results reads with the assistant; only a real user turn (always
  // text) stays in the user column. Otherwise a tool-heavy session reads as
  // the user talking through every tool — see claunch-jm2.
  const asst = r.role === "assistant"
    || (r.role === "user" && (r.blocks || []).every((b) => b.type === "tool_result"));
  const box = el("div", `log-rec log-${asst ? "asst" : "user"}`);
  box.dataset.seq = String(r.seq);
  const head = el("div", "log-head");
  head.appendChild(el("span", "log-role", asst ? "assistant" : r.role));
  if (r.ts) {
    const when = el("span", "log-ts", fmtLogTime(r.ts));
    when.title = r.ts;
    head.appendChild(when);
  }
  box.appendChild(head);
  for (const b of r.blocks || []) box.appendChild(renderTranscriptBlock(b));
  return box;
}

function renderTranscriptBlock(b) {
  if (b.type === "text") return el("div", "log-text", b.text);
  if (b.type === "thinking") {
    const d = el("div", "log-think");
    d.appendChild(el("div", "log-kind", "thinking"));
    d.appendChild(el("div", "log-text", b.text));
    return d;
  }
  if (b.type === "tool_use") {
    const d = el("div", "log-tool");
    d.appendChild(el("div", "log-kind", `▸ ${b.name}`));
    d.appendChild(el("pre", "log-pre", transcriptClipped(b)));
    return d;
  }
  if (b.type === "tool_result") {
    const d = el("div", `log-tool${b.error ? " log-err" : ""}`);
    d.appendChild(el("div", "log-kind", b.error ? "◂ error" : "◂ result"));
    d.appendChild(el("pre", "log-pre", transcriptClipped(b)));
    return d;
  }
  return el("div", "log-text", "");
}

/* A clipped block says so, and says how much it is holding back — the page
   carries two thousand characters of a tool result, not the megabyte of file
   content some of them are. */
function transcriptClipped(b) {
  if (!b.clipped) return b.text || "";
  const rest = (b.full || 0) - (b.text || "").length;
  return `${b.text}\n… ${ctxShort(rest)} more characters`;
}

/* The clock time a turn landed at, in the reader's own zone. The date is
   dropped — a conversation is read as a sequence, not a calendar — and the
   full ISO stamp rides the title for the one time somebody needs the day. */
function fmtLogTime(iso) {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  return new Date(t).toLocaleTimeString();
}

/* The follow-forward: while the pane is open and the reader is sitting at the
   bottom of it, new turns arrive under them the way the terminal's own output
   does. Scrolled up, nothing moves — a reader who went looking for something
   is not asking to be dragged back to the present every few seconds.

   Its own timer rather than a ride on the session poll: the pane is open for
   one session at a time and closed most of the time, and a conversation turn
   is a slower thing than a rail row. Started when it opens, stopped when it
   closes, so a closed pane costs nothing. */
const TRANSCRIPT_POLL_MS = 4000;
let transcriptTimer = null;

function startTranscriptPoll() {
  if (transcriptTimer) return;
  transcriptTimer = setInterval(pollTranscript, TRANSCRIPT_POLL_MS);
}

function stopTranscriptPoll() {
  if (!transcriptTimer) return;
  clearInterval(transcriptTimer);
  transcriptTimer = null;
}

function pollTranscript() {
  // `transcriptIsOpen` is the whole guard now: route() clears the name (and
  // stops this timer) the moment the reader leaves the page, so a live name
  // means the page is the one on screen.
  if (!transcriptIsOpen() || transcriptBusy) return;
  const pane = $("term-log-pane");
  if (!pane || !transcriptAtEnd(pane)) return;
  loadTranscriptPage({ older: false });
}

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
  syncSplitPane();
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
  const split = $("detail-split");
  const narrow = MOBILE_MQ.matches;
  const host = narrow ? $("main") : $("layout");
  if (view.parentNode !== host) {
    // Last in #layout is the mobile bottom bar (display:none up here), so the
    // rail goes before it: #main keeps the middle, this takes the right edge.
    if (narrow) host.appendChild(view);
    else host.insertBefore(view, $("mobile-bottom"));
    // The resize handle goes with it, as the pair's left half — the same
    // reason it is declared beside the rail in the markup: the two nodes
    // must never be split across homes, or one docks while the other page.
    host.insertBefore(split, view);
  }
  view.classList.toggle("docked", !narrow);
  const up = !!sessName && (!narrow || currentPage === "session");
  view.classList.toggle("hidden", !up);
  // The handle is the docked rail's, so it is only up while the rail is the
  // right column: a phone never shows it even when the detail page does.
  split.classList.toggle("hidden", !(up && !narrow));
  // Docking and undocking take width off #main and give it back, and no
  // resize event announces that — a sibling changing width is not a viewport
  // change. Without this the terminal keeps the columns it had and the
  // session wraps its output against a width that is no longer there.
  if (!narrow && up !== detailWasUp) refitSoon(60);
  detailWasUp = up;
}

/* Whether the run pane is halved into the terminal's column, and at what
   ratio. Derived like the rest of the chrome — from (breakpoint, page,
   session) plus the session's remembered choice — and never toggled from the
   outside: the header's ⬒ button writes the choice down (setSessLayout) and
   this reads it back, so a reload, a session switch and the button all take
   the same path. Wide screens only: below the breakpoint the terminal is
   already fighting a keyboard for rows, and half of that is no use to
   anybody — the choice is kept, not honoured, until the screen comes back. */
let splitWasUp = false;

function syncSplitPane() {
  const name = currentPage === "terminal" ? currentName : null;
  const lay = name ? sessLayoutFor(name) : null;
  const up = !!lay && lay.split && !MOBILE_MQ.matches;
  $("term-split").classList.toggle("hidden", !up);
  $("term-wf").classList.toggle("hidden", !up);
  document.body.classList.toggle("term-split", up);
  // Pressed is the session's remembered choice, not what fits this screen:
  // the button must read "on" on a narrowed window too, or pressing it there
  // would silently write the opposite of what it appears to do.
  const btn = $("term-splitbtn");
  btn.setAttribute("aria-pressed", String(!!lay && lay.split));
  btn.title = lay && lay.split
    ? "close the run pane — the terminal takes the column back"
    : "this session's workflow run under the terminal — " +
      "drag the bar between them to resize";
  if (up) {
    applySplitRatio(lay.ratio);
    if (splitFor !== name) openSplit(name);
  } else if (splitFor) {
    closeSplit();
  }
  // The pane takes height off the terminal and gives it back, and no resize
  // event announces that — same deal as the detail rail docking beside it.
  if (up !== splitWasUp) refitSoon(60);
  splitWasUp = up;
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

/* ---- back to the rail's card for the session you are in ----

   The rail is a monitor of every session at once, in a 260px column that
   scrolls: the row for the one you are actually typing in is as likely to be
   out of sight as not, and once it is, the page had no way to say where it
   went. The header's `⇱ card` is that way — the only control here that moves
   the reader rather than the session.

   A wide-screen control by construction: on a phone the whole header is
   display:none (the mobile top bar stands in for it) and the rail is a mode
   instead of a column, so there is no case where this button is on screen
   and the rail is not a scrollable box beside it. */

/* How long the row it lands on stays marked. Long enough to be seen after a
   smooth scroll, short enough that a rail left alone is not still shouting
   about a press from a minute ago. */
const GOTO_FLASH_MS = 1600;

/* Which row is marked, if any. Held here and not on the node because the rail
   is rebuilt whole on every 2s poll — a class written straight onto the row
   would be thrown away by the next tick, which is well inside the time the
   scroll itself takes. refreshSessions repaints it from this instead. */
let gotoFlashName = null;
let gotoFlashTimer = null;

/* Paint the mark onto the rows that exist now. Idempotent, and safe on a rail
   that has since lost the row (the session exited and was cleared). */
function applyGotoFlash() {
  document.querySelectorAll("#session-list li").forEach((li) =>
    li.classList.toggle(
      "goto-flash", !!gotoFlashName && li.dataset.name === gotoFlashName
    )
  );
}

/* Scroll the rail to `name`'s row and mark it. Answers whether there was a
   row at all: the rail is a poll behind the terminal, so a session attached a
   second ago can legitimately have none yet, and that is a no-op rather than
   an error — the next poll builds it and the reader can press again. */
function revealSessionCard(name) {
  let row = null;
  document.querySelectorAll("#session-list li").forEach((li) => {
    if (li.dataset.name === name) row = li;
  });
  if (!row) return false;
  // Centred, not merely "into view": a row brought to the very edge of the
  // rail is on screen and still reads as not found.
  if (row.scrollIntoView) row.scrollIntoView({ block: "center", behavior: "smooth" });
  gotoFlashName = name;
  if (gotoFlashTimer) clearTimeout(gotoFlashTimer);
  gotoFlashTimer = setTimeout(() => {
    gotoFlashTimer = null;
    gotoFlashName = null;
    applyGotoFlash();
  }, GOTO_FLASH_MS);
  applyGotoFlash();
  return true;
}

/* The header button's whole job: the attached session's card, on screen. */
function gotoSessionCard() {
  if (!currentName) return false;
  return revealSessionCard(currentName);
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
    case "reports": return "reports";
    case "mesh": return `mesh · ${meshName}`;
    case "flow": return `flows · ${flowMesh}`;
    // The session first, for the reason the head carries it (wfOwnerChip):
    // one directory holds one run per session, so the path alone names a
    // group of pages rather than the one on screen.
    case "wf": return `workflow · ${
      wfScope && wfScope !== "default" ? wfScope + " · " : ""
    }${shortenPath(wfCwd || "")}`;
    case "log": return `transcript · ${transcriptName || ""}`;
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
  $("m-kill").classList.toggle("hidden", !has || status === "exited");
  $("m-archive").classList.toggle(
    "hidden", !has || status !== "exited" || !!(sess && sess.archived_at));

  const bDot = $("mb-dot");
  bDot.className = `dot ${status}`;
  bDot.classList.toggle("hidden", !has);
  $("mb-name").textContent = has ? currentName : "no session open";
  $("mb-meta").textContent = has
    ? [status, sess && profileHarnessLabel(sess.profile, sess.harness)]
        .filter(Boolean).join(" · ")
    : "pick one from the list";
  $("mobile-bottom").classList.toggle("empty", !has);
}

// ☰ is "show me the rail" — which is the home route, the one page that IS
// the rail on a phone. Going through the router rather than flipping the
// flag keeps the URL honest about what is on screen.
$("m-menu").addEventListener("click", () => { location.hash = "#/"; });
// The header's controls are the real ones; these mirrors keep archive and
// resume behaviour in one place.
$("m-kill").addEventListener("click", () => $("term-kill").click());
$("m-archive").addEventListener("click", () => $("term-archive").click());
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
  cli: "cli-view",
  beads: "beads-view",
  reports: "reports-view",
  wf: "wf-view",
  // The session's conversation — the fourth reading of it, beside the
  // terminal (what it is doing), the run page (where it has got to) and the
  // trace (who it has talked to). Its own page for the same reason those
  // are: it is a place you can be, with a link and a back button.
  log: "log-view",
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
  // The send-keys input belongs to an attached session: hidden with the
  // terminal, and only up when one is attached. Guarded like the terminal
  // itself is not — the element always exists in the shipped page, but a
  // harness that boots this function against a partial DOM has no reason to
  // know about it.
  if ($("term-input"))
    $("term-input").classList.toggle("hidden", !(showTerm && currentName));
  // The transcript is its own page now (VIEWS below hides and shows it like
  // any other), so nothing here has to reach for it. The terminal button that
  // walks to it lives in the header, which the line above already handles.
  // The queued-deliveries banner belongs to the terminal under it: gone with
  // the terminal, re-asked-for on the way back in (the 2s poll would repaint
  // it anyway, but a page swap should not flash a stale backlog first).
  if (!showTerm) renderTermQueued(null);
  else refreshTermQueued();
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

/* The page's identity, as renderWfInto sees it: where the step selection
   lives, how to redraw and re-fetch, and whose reminder box is whose. The
   split pane below hands the same renderer a different one of these
   (splitUi), which is the whole of how one run page draws in two places. */
const wfPageUi = {
  host: "page",
  getStep: () => wfSelectedStep,
  putStep: (s) => { wfSelectedStep = s; },
  select(step) { this.putStep(step); if (wfLastData) renderWf(wfLastData); },
  refresh: () => refreshWf(),
  stillHere: (cwd) => wfCwd === cwd,
  fullLink: false,
  // The page keeps wfActions' defaults — archive offered, no `after` needed
  // because cflowAction already re-fetches this very page.
  actions: {},
};

/* ------------------------------------------------------------------ */
/* split mode: the run page halved into the terminal's column          */
/* ------------------------------------------------------------------ */
/* The same material as #/wf/<scope|cwd>, rendered under the terminal that
   drives it, so the run and the agent working it are read together. The
   pane owns its own slot, poll, selection and last payload — the page's
   globals above belong to the page, and the two must be able to differ. */
let splitFor = null;           // the session the pane is showing (null = shut)
let splitCwd = null;           // its slot, resolved once from the meta endpoint
let splitScope = null;
let splitPollTimer = null;
let splitSelectedStep = null;
let splitLastData = null;

const splitUi = {
  host: "split",
  getStep: () => splitSelectedStep,
  putStep: (s) => { splitSelectedStep = s; },
  select(step) { this.putStep(step); if (splitLastData) renderSplit(splitLastData); },
  refresh: () => refreshSplit(),
  stillHere: (cwd) => splitCwd === cwd,
  fullLink: true,   // the way from the half to the whole page
  // cflowAction re-fetches the PAGE after an action; this pane it does not
  // know about, so the pane asks for itself.
  actions: { after: () => refreshSplit() },
};

function renderSplit(data) {
  renderWfInto($("term-wf"), data, splitUi);
}

/* Point the pane at a session. Everything else is the poll's: resolving the
   slot is in there too, so an ask that fails on the first paint (the auth
   overlay is up, the daemon blinked) is simply asked again two seconds on,
   instead of leaving the pane on "loading…" for good. */
function openSplit(name) {
  closeSplit();
  splitFor = name;
  $("term-wf").innerHTML = "<p class='wf-note'>loading…</p>";
  refreshSplit();
  splitPollTimer = setInterval(refreshSplit, 2000);
}

function closeSplit() {
  if (splitPollTimer) { clearInterval(splitPollTimer); splitPollTimer = null; }
  splitFor = null;
  splitCwd = null;
  splitScope = null;
  splitSelectedStep = null;
  splitLastData = null;
  $("term-wf").innerHTML = "";
}

async function refreshSplit() {
  const want = splitFor;
  if (!want) return;
  // A run is keyed by (cwd, scope) and the meta endpoint already answers
  // that resolution for a session — ask it rather than re-deriving the slot
  // rules here. null is "not asked yet"; "" is "asked, and there is none",
  // which unlike a failed ask is an answer, so the poll stops asking.
  if (splitCwd === null) {
    let flow;
    try {
      const resp = await api(`/api/sessions/${encodeURIComponent(want)}/meta`);
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) return;   // the poll will come back round
      flow = data.cflow || null;
    } catch {
      return;
    }
    if (splitFor !== want) return;   // repointed while the reply was in flight
    if (!flow || !flow.cwd) {
      splitCwd = "";
      $("term-wf").innerHTML = "";
      $("term-wf").appendChild(el(
        "p", "wf-note",
        "this session has no working directory — there is no run slot to show"
      ));
      return;
    }
    splitCwd = flow.cwd;
    splitScope = flow.scope || want;
  }
  if (!splitCwd) return;   // resolved: this session has no slot
  let data;
  try {
    const resp = await api(
      `/api/cflow/run?cwd=${encodeURIComponent(splitCwd)}` +
      `&scope=${encodeURIComponent(splitScope)}`
    );
    data = await resp.json();
    if (!resp.ok) {
      $("term-wf").innerHTML = "";
      $("term-wf").appendChild(
        el("p", "wf-warning", data.error || "cannot load run"));
      return;
    }
  } catch {
    return;
  }
  if (splitFor !== want) return;   // repointed while the reply was in flight
  splitLastData = data;
  renderSplit(data);
}

/* The ⬒ button: writes the choice down; syncSplitPane (via the setter) is
   what actually opens and closes the pane, so a press and a reload agree. */
$("term-splitbtn").addEventListener("click", () => {
  if (!currentName) return;
  setSessLayout(currentName, { split: !sessLayoutFor(currentName).split });
});

/* The bar between the two. The ratio is applied live while the hand moves
   and written down once at release — localStorage is not a place to stream
   pointer events into. */
function applySplitRatio(r) {
  $("main").style.setProperty("--split-term", String(r));
  $("main").style.setProperty("--split-wf", String(1 - r));
}

let splitDragRatio = null;   // non-null only mid-drag

$("term-split").addEventListener("pointerdown", (e) => {
  if (!currentName) return;
  e.preventDefault();   // a drag must not start selecting terminal text
  $("term-split").setPointerCapture(e.pointerId);
  $("term-split").classList.add("dragging");
  splitDragRatio = sessLayoutFor(currentName).ratio;
});
$("term-split").addEventListener("pointermove", (e) => {
  if (splitDragRatio === null) return;
  const bar = $("term-split");
  const top = $("terminal").getBoundingClientRect().top;
  const span = $("term-wf").getBoundingClientRect().bottom - top - bar.offsetHeight;
  if (span <= 0) return;
  splitDragRatio = clampSplitRatio((e.clientY - top - bar.offsetHeight / 2) / span);
  applySplitRatio(splitDragRatio);
  refitSoon();   // debounced: the real refit lands when the hand pauses
});
const endSplitDrag = () => {
  if (splitDragRatio === null) return;
  $("term-split").classList.remove("dragging");
  if (currentName) setSessLayout(currentName, { ratio: splitDragRatio });
  splitDragRatio = null;
  refitSoon(60);
};
$("term-split").addEventListener("pointerup", endSplitDrag);
$("term-split").addEventListener("pointercancel", endSplitDrag);

/* ------------------------------------------------------------------ */
/* the rail's width: the bar between the sidebar and #main             */
/* ------------------------------------------------------------------ */
/* The split bar's shape, turned upright: applied live while the hand moves,
   written down once at release. Scoped by BASE for the same reason the font
   size is — daemons behind one relay share this localStorage. The phone
   breakpoint hides the bar entirely (the rail is a mode there, not a
   column), so none of this runs on a phone. */
const RAIL_W_KEY = `claunch_railw:${BASE}`;
const RAIL_W_DEFAULT = 260;   // what the stylesheet ships
const RAIL_W_MIN = 180;       // narrower and every session row is ellipsis
// the ceiling moves with the window: half the screen is the most a list
// should ever take from a terminal
const railWMax = () => Math.max(RAIL_W_MIN, Math.round(window.innerWidth / 2));

function clampRailW(px) {
  if (!Number.isFinite(px)) return RAIL_W_DEFAULT;
  return Math.min(railWMax(), Math.max(RAIL_W_MIN, Math.round(px)));
}

function applyRailW(px) {
  $("layout").style.setProperty("--rail-w", `${px}px`);
}
applyRailW(clampRailW(Number(localStorage.getItem(RAIL_W_KEY)) || RAIL_W_DEFAULT));

let railDragW = null;   // non-null only mid-drag

$("rail-split").addEventListener("pointerdown", (e) => {
  e.preventDefault();   // a drag must not start selecting list text
  $("rail-split").setPointerCapture(e.pointerId);
  $("rail-split").classList.add("dragging");
  railDragW = $("sidebar").getBoundingClientRect().width;
});
$("rail-split").addEventListener("pointermove", (e) => {
  if (railDragW === null) return;
  railDragW = clampRailW(e.clientX - $("sidebar").getBoundingClientRect().left);
  applyRailW(railDragW);
  refitSoon();   // debounced: the real refit lands when the hand pauses
});
const endRailDrag = () => {
  if (railDragW === null) return;
  $("rail-split").classList.remove("dragging");
  localStorage.setItem(RAIL_W_KEY, String(railDragW));
  railDragW = null;
  refitSoon(60);
};
$("rail-split").addEventListener("pointerup", endRailDrag);
$("rail-split").addEventListener("pointercancel", endRailDrag);
/* back to the stylesheet's width, the way the zoom readout resets the text */
$("rail-split").addEventListener("dblclick", () => {
  applyRailW(RAIL_W_DEFAULT);
  localStorage.setItem(RAIL_W_KEY, String(RAIL_W_DEFAULT));
  refitSoon(60);
});

/* ------------------------------------------------------------------ */
/* the docked detail's width: the bar beside the right-hand rail       */
/* ------------------------------------------------------------------ */
/* #rail-split's mirror, sitting between #main and the session detail
   once it docks. Same contract: applied live while the hand moves, written
   down once at release, reset by a double click, remembered per browser by
   its own key (scoped by BASE like the rail's) — and the phone hides the
   bar entirely, because there the detail is a page, not a column. */
const DETAIL_W_KEY = `claunch_detailw:${BASE}`;
const DETAIL_W_DEFAULT = 300;   // what the stylesheet ships
const DETAIL_W_MIN = 220;       // narrower and the run page chokes two columns
// the ceiling moves with the window: half the screen is the most a second
// panel should ever take from a terminal
const detailWMax = () => Math.max(DETAIL_W_MIN, Math.round(window.innerWidth / 2));

function clampDetailW(px) {
  if (!Number.isFinite(px)) return DETAIL_W_DEFAULT;
  return Math.min(detailWMax(), Math.max(DETAIL_W_MIN, Math.round(px)));
}

function applyDetailW(px) {
  $("layout").style.setProperty("--detail-w", `${px}px`);
}
applyDetailW(clampDetailW(Number(localStorage.getItem(DETAIL_W_KEY)) || DETAIL_W_DEFAULT));

let detailDragW = null;   // non-null only mid-drag

$("detail-split").addEventListener("pointerdown", (e) => {
  e.preventDefault();   // a drag must not start selecting detail text
  $("detail-split").setPointerCapture(e.pointerId);
  $("detail-split").classList.add("dragging");
  detailDragW = $("sess-view").getBoundingClientRect().width;
});
$("detail-split").addEventListener("pointermove", (e) => {
  if (detailDragW === null) return;
  // The bar is the rail's left edge: dragging it away from the rail's right
  // edge narrows the rail, towards it widens it.
  detailDragW = clampDetailW($("sess-view").getBoundingClientRect().right - e.clientX);
  applyDetailW(detailDragW);
  refitSoon();   // debounced: the real refit lands when the hand pauses
});
const endDetailDrag = () => {
  if (detailDragW === null) return;
  $("detail-split").classList.remove("dragging");
  localStorage.setItem(DETAIL_W_KEY, String(detailDragW));
  detailDragW = null;
  refitSoon(60);
};
$("detail-split").addEventListener("pointerup", endDetailDrag);
$("detail-split").addEventListener("pointercancel", endDetailDrag);
/* back to the stylesheet's width, the way the rail's bar resets its own */
$("detail-split").addEventListener("dblclick", () => {
  applyDetailW(DETAIL_W_DEFAULT);
  localStorage.setItem(DETAIL_W_KEY, String(DETAIL_W_DEFAULT));
  refitSoon(60);
});

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
  // What the session SAID — the fourth reading, and the only one that does
  // not need the session to be alive: it comes off claude's jsonl on disk,
  // so an exited record reads exactly like a running one.
  if (parts[0] === "log" && parts[1]) return { page: "log", name: parts[1] };
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
  // One page, one shell: nothing else about the CLI tab is addressable, so
  // anything past "#/cli" is still the same terminal.
  if (parts[0] === "cli") return { page: "cli" };
  // #/beads is the board; #/beads/<id> the board with one issue opened.
  if (parts[0] === "beads") return { page: "beads", id: parts[1] || "" };
  if (parts[0] === "reports") return { page: "reports" };
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
  if (r.page !== "beads") stopBeadsPoll();
  if (r.page !== "reports") stopReportsPoll();
  if (r.page !== "log") closeTranscript();

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
    case "log": openTranscript(r.name); break;
    case "msg": openTrace(r.name, r.mesh); break;
    case "mesh": openMesh(r.name); break;
    case "flow": openFlowTopology(r.name); break;
    case "meshes": showView("meshes"); refreshMeshList(); break;
    case "new": showView("new"); refreshWorkflowChoices(); break;
    case "flows": showView("flows"); refreshCflow(); break;
    case "cli": openCli(); break;
    case "ws": openWorkspaces(); break;
    case "beads": openBeads(r.id); break;
    case "reports": openReports(); break;
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

  // Ranked by the picked role, exactly as the CLI wizard's Workflow row and
  // the spawn modal's rank it: the role's own defaults first, then the rest,
  // the ones its filter_roles refuses last.
  const wf = form.workflow;
  const { options, auto } = spawnRankWorkflows(workflowsCache, form.role.value);
  // Choosing a role chooses its workflow — but only over a row nobody has
  // touched. A workflow the operator picked survives every later role change,
  // and so does "(none)": an auto-pick that came back after somebody chose to
  // start no run at all would be the form overruling them, which is the CLI
  // wizard's rule as well.
  const keptWf = newWfPicked ? wf.value : auto;
  wf.innerHTML = "";
  wf.appendChild(new Option("(none)", ""));
  for (const o of options) {
    wf.appendChild(new Option(o.detail ? `${o.name} — ${o.detail}` : o.name,
                              o.name));
  }
  wf.value = [...wf.options].some((o) => o.value === keptWf) ? keptWf : "";
  $("new-context-row").classList.toggle("hidden", !wf.value);
}

/* Workflows are declared per directory, so the list follows the Directory
   picker rather than being fetched once. Kept as the daemon serves them —
   name, default_role, priority, filter_roles — because the picker ranks them
   by the chosen role, and a list of bare names cannot be ranked at all. */
let workflowsCache = [];
let workflowsFor = null;
/* Whether the Workflow row has been touched by hand. Until it has, the
   picked role decides it (see syncOnboardPickers). */
let newWfPicked = false;

/* Where the session being created will actually stand: its own directory, or
   its parent's when it is a child that may not be moved. Asking for the
   form's cwd alone would list the daemon directory's workflows for a child
   that will boot somewhere else entirely. */
function newSessionCwd() {
  const f = $("new-session");
  const parent = spawnParent();
  if (!parent) return f.cwd.value;
  if (!f.cwd.disabled && f.cwd.value) return f.cwd.value;
  return parent.cwd || "";
}

async function refreshWorkflowChoices() {
  const cwd = newSessionCwd();
  if (cwd === workflowsFor) return;
  // Claimed before the await so two changes in flight do not both fetch, and
  // GIVEN BACK below if the fetch failed: `workflowsFor` is a memo of an
  // ANSWER, and a daemon that was busy for one tick did not give one. Left
  // claimed, an empty list would be remembered as "this directory declares no
  // workflows" and every later call would early-return on it -- the picker
  // staying blank for the life of the tab, which is exactly how this comes
  // back under load.
  workflowsFor = cwd;
  let answered = false;
  try {
    const resp = await api(`/api/cflow/workflows?cwd=${encodeURIComponent(cwd)}`);
    workflowsCache = resp.ok ? ((await resp.json()).workflows || []) : [];
    answered = resp.ok;
  } catch {
    workflowsCache = [];
  }
  // Only this call's own claim is released: a later cwd change that already
  // re-claimed it owns the memo now, and its fetch is the one that answers.
  if (!answered && workflowsFor === cwd) workflowsFor = null;
  syncOnboardPickers();
}

/* The board rows. The mode is a closed question with three answers, so it is
   radios rather than a fourth entry in a picker; the issue list is only a
   question at all under one of them, and is hidden under the other two.

   Every row carries the daemon's own verdict on it (daemon/beads.adoption):
   an issue nobody holds would be ASSIGNED to the new session, one a running
   session holds would be JOINED and the assignment left where it is. Saying
   so here rather than in the created session's opening block is the whole
   point of the row — by then the choice has been made. */
function beadsMode() {
  const f = $("new-session");
  return f.beads ? f.beads.value : "new";
}

function syncBeadsRow() {
  const f = $("new-session");
  const picking = beadsMode() === "existing";
  // Hidden, not cleared: somebody who types a specification, tries the other
  // two answers and comes back should find their words where they left them.
  $("new-issue-text-row").classList.toggle("hidden", beadsMode() !== "new");
  $("new-issue-row").classList.toggle("hidden", !picking);
  const hint = $("new-issue-hint");
  const row = picking
    ? issuesCache.find((i) => i.id === f.issue.value)
    : null;
  // Only the consequence a reader cannot see from the row is written out:
  // an issue that would simply be assigned needs no warning.
  if (row && row.held_by) {
    hint.textContent =
      `${row.held_by} is assigned to ${row.id} and still running — this ` +
      "session JOINS it: the assignment stays put and the two settle " +
      "ownership between them.";
    hint.classList.remove("hidden");
  } else if (picking && issuesRead && !issuesCache.length) {
    // Only once the board has actually answered: an empty list held while
    // the fetch is still in flight would read as "this board has nothing",
    // which is a different and wrong thing to tell somebody.
    hint.textContent = issuesError ||
      "no open issue on this directory's board.";
    hint.classList.remove("hidden");
  } else {
    hint.classList.add("hidden");
  }
}

/* The board follows the Directory picker exactly as the workflow list does,
   and for the same reason: a session created somewhere else is created on
   another repository's board. Kept as the daemon serves them, verdict and
   all — a list of bare ids could not say which ones are held. */
let issuesCache = [];
let issuesFor = null;
let issuesError = "";
/* Whether the board has answered at all yet — see syncBeadsRow. */
let issuesRead = false;

async function refreshIssueChoices() {
  const cwd = newSessionCwd();
  const parent = spawnParent();
  const key = `${cwd}\u0000${parent ? parent.name : ""}`;
  if (key === issuesFor) return;
  // Claimed before the await and given back on failure, like
  // refreshWorkflowChoices: an empty list remembered as an answer would
  // leave the picker blank for the life of the tab.
  issuesFor = key;
  let answered = false;
  try {
    const q = parent && !cwd
      ? `parent=${encodeURIComponent(parent.name)}`
      : `cwd=${encodeURIComponent(cwd)}`;
    const resp = await api(`/api/beads/candidates?${q}`);
    const doc = resp.ok ? await resp.json() : {};
    issuesCache = doc.issues || [];
    issuesError = doc.error || "";
    answered = resp.ok;
  } catch {
    issuesCache = [];
    issuesError = "";
  }
  if (!answered && issuesFor === key) issuesFor = null;
  issuesRead = true;
  renderIssueOptions();
}

function renderIssueOptions() {
  const sel = $("new-session").issue;
  const kept = sel.value;
  sel.innerHTML = "";
  sel.appendChild(new Option("(pick an issue)", ""));
  for (const i of issuesCache) {
    const held = i.held_by ? ` — held by ${i.held_by}, would JOIN` : "";
    sel.appendChild(
      new Option(`${i.id}  ${i.title || ""}`.trim() + ` [${i.status}]${held}`,
                 i.id)
    );
  }
  sel.value = [...sel.options].some((o) => o.value === kept) ? kept : "";
  syncBeadsRow();
}

for (const radio of document.querySelectorAll(
  '#new-beads input[name="beads"]'
)) {
  radio.addEventListener("change", () => {
    // The list is only fetched once somebody asks for it: a board read costs
    // a `br` fork on the daemon, and two of the three answers never look.
    if (beadsMode() === "existing") refreshIssueChoices();
    syncBeadsRow();
  });
}
$("new-session").issue.addEventListener("change", syncBeadsRow);

$("new-session").mesh.addEventListener("change", syncOnboardPickers);
$("new-session").workflow.addEventListener("change", () => {
  // From here on this row is the operator's, not the role's.
  newWfPicked = true;
  syncOnboardPickers();
});
document
  .querySelector("#new-session select[name=cwd]")
  .addEventListener("change", () => {
    refreshWorkflowChoices();
    // The board moves with the directory too — but only while it is being
    // looked at; the memo below makes the next open re-read it regardless.
    issuesFor = null;
    issuesRead = false;
    if (beadsMode() === "existing") refreshIssueChoices();
  });


function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* ------------------------------------------------------------------ */
/* markdown — the little of it that cflow reports are written in        */
/* ------------------------------------------------------------------ */
/* A step report is the one thing on this dashboard a human reads end to
   end, and the `report` tool now asks the agent for markdown. Rendering it
   needs no library: the subset that gets written — headings, lists, fenced
   code, tables, emphasis — is a hundred lines, and building NODES instead
   of an HTML string means there is no sanitiser here to get wrong. Text the
   grammar does not recognise stays as its own literal text, so a report
   that was never markdown still reads exactly as it was typed.

   Two deliberate departures from CommonMark, both for the same reason —
   these are reports, not prose:
   - a single newline inside a paragraph is a line break, not a space. An
     agent that lays evidence out one fact per line meant those lines.
   - a paragraph keeps its leading whitespace (CSS pre-wrap), so pasted
     command output stays aligned even when nobody fenced it. */

const MD_BULLET = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const MD_RULE = /^\s{0,3}([-*_])[ \t]*(\1[ \t]*){2,}$/;
const MD_FENCE = /^\s*(```|~~~)/;
const MD_BLOCK_START = /^\s{0,3}(#{1,6}\s|>|```|~~~)/;
// Only what a dashboard link may point at. Anything else (javascript:, data:)
// renders as the link's own text — visible, inert, and not silently dropped.
const MD_SAFE_HREF = /^(https?:\/\/|mailto:|#|\/)/i;

/* Inline markers, in the order they must be tried: a code span swallows
   what is inside it, so it matches before emphasis can see the backticks.
   `_` comes after `*` on purpose and is guarded below — snake_case_names
   are far commoner in these reports than underscore emphasis is. */
const MD_INLINE = [
  [/^(`+)([\s\S]*?)\1(?!`)/, (m) => el("code", "md-code", m[2].replace(/^ (.*) $/, "$1"))],
  [/^\*\*([\s\S]+?)\*\*/, (m) => mdWrap("strong", "md-strong", m[1])],
  [/^__([\s\S]+?)__(?!\w)/, (m) => mdWrap("strong", "md-strong", m[1])],
  [/^~~([\s\S]+?)~~/, (m) => mdWrap("s", "md-strike", m[1])],
  [/^\*([^*\n]+)\*/, (m) => mdWrap("em", "md-em", m[1])],
  [/^_([^_\n]+)_(?!\w)/, (m) => mdWrap("em", "md-em", m[1])],
  [/^\[([^\]\n]*)\]\(\s*([^)\s]+)(?:\s+"[^"]*")?\s*\)/, (m) => mdLink(m[1], m[2])],
];

function mdWrap(tag, cls, text) {
  const node = el(tag, cls);
  for (const n of mdInline(text)) node.appendChild(n);
  return node;
}

function mdLink(text, href) {
  const label = text || href;
  if (!MD_SAFE_HREF.test(href)) return mdWrap("span", "md-link-inert", label);
  const a = mdWrap("a", "md-link", label);
  a.setAttribute("href", href);
  if (/^https?:/i.test(href)) {
    a.setAttribute("target", "_blank");
    a.setAttribute("rel", "noreferrer noopener");
  }
  return a;
}

/* One line of inline markdown -> a list of nodes. */
function mdInline(src) {
  const text = String(src == null ? "" : src);
  const out = [];
  let plain = "";
  const flush = () => {
    if (plain) { out.push(document.createTextNode(plain)); plain = ""; }
  };
  let i = 0;
  while (i < text.length) {
    const c = text[i];
    if (c === "\\" && i + 1 < text.length && "\\`*_~[]()#+-.!>|".includes(text[i + 1])) {
      plain += text[i + 1];
      i += 2;
      continue;
    }
    // A mid-word underscore is part of the word: test_web_topology.py
    if (c === "_" && i > 0 && /\w/.test(text[i - 1])) { plain += c; i++; continue; }
    let hit = null;
    if ("`*_~[".includes(c)) {
      for (const [re, make] of MD_INLINE) {
        const m = re.exec(text.slice(i));
        if (m) { hit = [m, make]; break; }
      }
    }
    if (!hit) { plain += c; i++; continue; }
    flush();
    out.push(hit[1](hit[0]));
    i += hit[0][0].length;
  }
  flush();
  return out;
}

/* A run of bullets at one indent -> [list node, index of the first line
   after it]. Deeper bullets nest under the item above them; an indented
   line that is not a bullet continues that item's own text. */
function mdList(lines, start) {
  const first = MD_BULLET.exec(lines[start]);
  const indent = first[1].length;
  const ordered = /\d/.test(first[2]);
  const list = el(ordered ? "ol" : "ul", "md-list");
  if (ordered) {
    const n = parseInt(first[2], 10);
    if (n > 1) list.setAttribute("start", String(n));
  }
  let i = start;
  let item = null;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      // A blank line only ends the list if what follows is not more of it.
      let j = i + 1;
      while (j < lines.length && !lines[j].trim()) j++;
      const nxt = j < lines.length ? MD_BULLET.exec(lines[j]) : null;
      if (!nxt || nxt[1].length < indent) break;
      i = j;
      continue;
    }
    const m = MD_BULLET.exec(line);
    if (m && m[1].length < indent) break;
    if (m && m[1].length <= indent + 1) {
      item = el("li", "md-item");
      for (const n of mdInline(m[3])) item.appendChild(n);
      list.appendChild(item);
      i++;
      continue;
    }
    if (m && item) {
      const [sub, next] = mdList(lines, i);
      item.appendChild(sub);
      i = next;
      continue;
    }
    if (!m && item && /^\s/.test(line)) {
      item.appendChild(el("br", null));
      for (const n of mdInline(line.trim())) item.appendChild(n);
      i++;
      continue;
    }
    break;
  }
  return [list, i];
}

const mdCells = (row) =>
  row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());

/* Reports carry evidence as (axis, tree, value) rows often enough that a
   pipe table earns its twenty lines. A header row plus a `---|---` rule is
   the whole signature; anything else falls through to a paragraph. */
function mdTable(lines, start) {
  const align = mdCells(lines[start + 1]).map((c) =>
    /^:-+:$/.test(c) ? "center" : /-+:$/.test(c) ? "right" : /^:-+/.test(c) ? "left" : "");
  const table = el("table", "md-table");
  const thead = el("thead", null);
  const hrow = el("tr", null);
  mdCells(lines[start]).forEach((c, k) => {
    const th = mdWrap("th", null, c);
    if (align[k]) th.setAttribute("style", `text-align:${align[k]}`);
    hrow.appendChild(th);
  });
  thead.appendChild(hrow);
  table.appendChild(thead);
  const body = el("tbody", null);
  let i = start + 2;
  for (; i < lines.length && lines[i].trim() && lines[i].includes("|"); i++) {
    const row = el("tr", null);
    mdCells(lines[i]).forEach((c, k) => {
      const td = mdWrap("td", null, c);
      if (align[k]) td.setAttribute("style", `text-align:${align[k]}`);
      row.appendChild(td);
    });
    body.appendChild(row);
  }
  table.appendChild(body);
  return [table, i];
}

/* Markdown text -> a list of block nodes. */
function mdBlocks(src) {
  const lines = String(src == null ? "" : src).replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }

    const fence = MD_FENCE.exec(line);
    if (fence) {
      const close = new RegExp("^\\s*" + (fence[1][0] === "`" ? "```" : "~~~") + "\\s*$");
      const lang = line.trim().slice(3).trim();
      const buf = [];
      i++;
      while (i < lines.length && !close.test(lines[i])) { buf.push(lines[i]); i++; }
      i++;  // the closing fence, or past the end when there never was one
      const pre = el("pre", "md-pre");
      const code = el("code", null, buf.join("\n"));
      if (lang) code.setAttribute("data-lang", lang);
      pre.appendChild(code);
      out.push(pre);
      continue;
    }

    const head = /^\s{0,3}(#{1,6})\s+(.*)$/.exec(line);
    if (head) {
      out.push(mdWrap("div", "md-h md-h" + head[1].length,
                      head[2].replace(/\s+#+\s*$/, "")));
      i++;
      continue;
    }

    if (MD_RULE.test(line)) { out.push(el("hr", "md-hr")); i++; continue; }

    if (/^\s{0,3}>/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s{0,3}>/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s{0,3}>\s?/, ""));
        i++;
      }
      const quote = el("blockquote", "md-quote");
      for (const n of mdBlocks(buf.join("\n"))) quote.appendChild(n);
      out.push(quote);
      continue;
    }

    if (line.includes("|") && i + 1 < lines.length &&
        lines[i + 1].includes("-") && /^\s*\|?[\s:|-]+$/.test(lines[i + 1]) &&
        lines[i + 1].includes("|")) {
      const [table, next] = mdTable(lines, i);
      out.push(table);
      i = next;
      continue;
    }

    if (MD_BULLET.test(line)) {
      const [list, next] = mdList(lines, i);
      out.push(list);
      i = next;
      continue;
    }

    const buf = [];
    while (i < lines.length && lines[i].trim() &&
           !MD_BLOCK_START.test(lines[i]) && !MD_BULLET.test(lines[i]) &&
           !MD_RULE.test(lines[i])) {
      buf.push(lines[i].replace(/\s+$/, ""));
      i++;
    }
    const p = el("p", "md-p");
    buf.forEach((ln, k) => {
      if (k) p.appendChild(el("br", null));
      for (const n of mdInline(ln)) p.appendChild(n);
    });
    out.push(p);
  }
  return out;
}

/* Render `text` into `node` as markdown and hand `node` back, so it drops
   into the one expression where an `el(...)` used to stand. */
function mdInto(node, text) {
  for (const b of mdBlocks(text)) node.appendChild(b);
  return node;
}

/* The same markdown with its markers taken off, line structure kept. For a
   `title` tooltip, where the browser renders text and nothing else: a
   reader hovering a rail line should not be shown the asterisks. */
function mdText(src) {
  return String(src == null ? "" : src)
    .replace(/\r\n?/g, "\n")
    .replace(/^[ \t]*(```|~~~).*$/gm, "")
    .replace(/^[ \t]{0,3}#{1,6}[ \t]+/gm, "")
    .replace(/^[ \t]{0,3}>[ \t]?/gm, "")
    .replace(/^[ \t]{0,3}([-*_])[ \t]*(\1[ \t]*){2,}$/gm, "")
    .replace(/^([ \t]*)([-*+]|\d+[.)])[ \t]+/gm, "$1• ")
    .replace(/`+([^`]*)`+/g, "$1")
    .replace(/\*\*([\s\S]+?)\*\*/g, "$1")
    .replace(/__([\s\S]+?)__(?!\w)/g, "$1")
    .replace(/~~([\s\S]+?)~~/g, "$1")
    .replace(/\*([^*\n]+)\*/g, "$1")
    .replace(/\[([^\]\n]*)\]\([^)\n]*\)/g, "$1")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/* And the same again folded onto one line, for the rail rows that are a
   single ellipsised line by design. */
function mdPlain(src) {
  return mdText(src).replace(/\s*\n\s*/g, " ").replace(/[ \t]{2,}/g, " ").trim();
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
    row.appendChild(el(
      "span", "meta", profileHarnessLabel(s.profile, s.harness)
    ));
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

  grid.appendChild(homeCard(
    "Beads", "#/beads",
    "the repository board, by session — who is on what"
  ));

  const missing = workspacesCache.filter((w) => !w.exists).length;
  grid.appendChild(homeCard(
    "Workspaces", "#/workspaces",
    workspacesCache.length
      ? plural(workspacesCache.length, "directory", "directories") +
        (missing ? ` · ${missing} missing` : "")
      : "none registered"
  ));

  // Last: the one card that is not a doorway — the machinery every other
  // card lives in, with the one control that acts on it.
  grid.appendChild(daemonCard());

  view.appendChild(grid);
}

/* The daemon's own card: what is serving (from the last /api/daemon read;
   boot() refills it on every recovery) and the restart control. The POST
   only asks — the daemon finishes the reply, drains its sessions, and
   spawns its own successor; the page then notices the new boot_id in
   pollOnce() and re-boots itself, so recovery is the ordinary reconnect
   path rather than anything this card does. Not a shortcut around any
   approval gate either: what a restart *serves* (say, a new build going
   live) is still decided wherever it is decided — this button is only the
   mechanics of `claunch daemon restart`, brought to the page.

   What it deliberately is NOT: a rescue for a daemon that has stopped
   answering. The POST is served by the daemon's own event loop, so a loop
   that has stopped turning never accepts it - the button would sit there
   looking like it should work, which is worse than not offering it. That
   case belongs to the CLI, which can reach the process itself
   (`claunch daemon restart --force`); the wording says so. */
function daemonCard() {
  const card = el("div", "home-card static");
  const head = el("div", "home-card-head");
  head.appendChild(el("h3", null, "Daemon"));
  card.appendChild(head);
  const live = sessionsCache.filter((s) => s.status !== "exited").length;
  // The start time earns its place here: the card in the corner is a moment
  // and is dismissed, and this is what is left to answer "is this still the
  // daemon from this morning?" long after it. A clock, not a duration —
  // this line is redrawn by a poll and a duration would sit here frozen.
  card.appendChild(el(
    "p", "home-sub",
    (daemonCache ? `v${daemonCache.version}` : "version unknown") +
      ` · ${plural(live, "live session")}` +
      (daemonStartedAt
        ? ` · started ${new Date(daemonStartedAt).toLocaleTimeString()}`
        : "")
  ));
  const btn = el("button", "wf-btn force", "Restart daemon");
  btn.title = "planned restart of a daemon that is answering - a daemon " +
    "that has stopped answering cannot be restarted from here " +
    "(claunch daemon restart --force)";
  btn.addEventListener("click", async () => {
    if (!confirm(
      "Restart the daemon?\n\n" +
      "Running sessions are stopped and relaunched into their own " +
      "conversations (per their restore flag). Attached terminals and this " +
      "page reconnect on their own — and the page will ask for the token " +
      "again, because login cookies die with the process.\n\n" +
      "This is a planned restart, and it goes through the daemon " +
      "itself: if one has stopped answering, this button cannot " +
      "reach it either - that case is 'claunch daemon restart " +
      "--force' from a terminal."
    )) return;
    btn.disabled = true;
    btn.textContent = "Restarting…";
    try {
      await api("/api/daemon/restart", { method: "POST" });
    } catch {
      btn.disabled = false;   // down already, or the auth overlay is up
      btn.textContent = "Restart daemon";
      return;
    }
    // From here the daemon goes quiet on purpose; label the gap so the rail
    // does not read as a mystery outage. pollOnce()'s boot_id check does the
    // actual recovery the moment the successor answers.
    setDaemonOnline(false);
    $("daemon-info").textContent = "restarting…";
    // setDaemonOnline just posted "daemon offline", which is true and, here,
    // alarming for no reason: this outage was asked for. Same key, so the
    // card is replaced rather than joined by a second one.
    notify(
      "restarting the daemon",
      `asked at ${noticeClock()} — it stops answering while it drains, and ` +
        "this page reconnects on its own once the successor is up",
      { key: NOTICE_LINK, kind: "warn", sticky: true }
    );
  });
  card.appendChild(btn);
  return card;
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

/* ------------------------------------------------------------------ */
/* the Beads page: the board, drawn against the fleet                 */
/* ------------------------------------------------------------------ */
/* One page, every board the fleet's sessions live in (one per repository,
   found through git's common dir so worktrees share it). The daemon tags
   each issue with the sessions it belongs to — the same match the rail
   draws for one session (`beads.match`: the recorded link, assignee,
   created_by, an `issue: <id>` in the task) — so the page answers "who is
   on what" without a shell. Read-only by design: the writes are the
   agents' (`claunch beads ...`) and the daemon's (creation, wind-down,
   the exit sweep). Polled at 5 s, not 2: `br` forks per board per read. */
let beadsOpen = false;
let beadsTimer = null;
let beadsCache = null;     // the last /api/beads payload
let beadsError = "";
let beadsFocus = "";       // the issue opened in the detail pane, by id
let beadsDetail = null;    // its /api/beads/<id> payload
let beadsFilter = "active";  // status filter: active | <status> | all
let beadsSession = "";     // session filter: "" = everybody
let beadsLayout = "board"; // "board" = status lanes, "tree" = the forest

const BEADS_STATUSES = ["open", "in_progress", "in_review", "blocked", "closed"];
const BEADS_ACTIVE = new Set(["open", "in_progress", "in_review", "blocked"]);

function openBeads(id) {
  beadsOpen = true;
  const focus = id || "";
  if (focus !== beadsFocus) beadsDetail = null;
  beadsFocus = focus;
  showView("beads");
  renderBeads();
  refreshBeads();
  if (!beadsTimer) beadsTimer = setInterval(refreshBeads, 5000);
}

function stopBeadsPoll() {
  if (beadsTimer) { clearInterval(beadsTimer); beadsTimer = null; }
  beadsOpen = false;
}

async function refreshBeads() {
  if (!beadsOpen) return;
  // The focused issue's own fetch goes out *with* the listing, not after it.
  // The two answer different questions and the detail is the small one, but
  // it was queued behind three quarters of a megabyte of board it does not
  // read — so the page a reader opened to see one issue waited for every
  // issue first. The root comes from the previous listing, which is where it
  // came from anyway; on the very first draw there is none yet and the
  // request goes without it, exactly as the sequential version's would have
  // on its first pass.
  const detailWanted = beadsFocus;
  let detailPromise = null;
  if (detailWanted) {
    const root = beadsRootOf(detailWanted);
    const q = root ? `?cwd=${encodeURIComponent(root)}` : "";
    detailPromise = api(`/api/beads/${encodeURIComponent(detailWanted)}${q}`)
      .then(async (resp) => {
        const data = await resp.json().catch(() => ({}));
        return resp.ok ? data : { error: data.error || `HTTP ${resp.status}` };
      })
      .catch(() => null);   // keep the last detail
  }
  try {
    const resp = await api("/api/beads");
    if (resp.status === 404) {
      beadsError = "this daemon predates the Beads page — 'claunch daemon " +
        "restart' to pick up this version";
    } else {
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) beadsError = data.error || `HTTP ${resp.status}`;
      else { beadsCache = data; beadsError = data.error || ""; }
    }
  } catch { return; }   // auth overlay is up, or the daemon is away
  if (detailPromise) {
    const data = await detailPromise;
    // Only if the reader has not walked to another issue meanwhile: this
    // request was fired against the focus of the poll that started it.
    if (data && beadsFocus === detailWanted) beadsDetail = data;
  }
  if (beadsOpen) renderBeads();
}

/* The board an issue id belongs to, from the last listing. */
function beadsRootOf(id) {
  for (const b of (beadsCache && beadsCache.boards) || []) {
    if ((b.issues || []).some((i) => i.id === id)) return b.root;
  }
  return "";
}

/* The rows a board shows under the current filters. `active` is the default
   because a board is read for what is still to do; `all` is the audit view.
   The session filter matches the daemon's tags, so "s12" shows every issue
   the daemon says is s12's — by whichever of the four links. */
function beadsFilterIssues(issues, filter, session) {
  return (issues || []).filter((i) => {
    if (filter === "active" ? !BEADS_ACTIVE.has(i.status)
        : filter !== "all" && i.status !== filter) return false;
    if (session && !(i.sessions || []).some((s) => s.name === session)) return false;
    return true;
  });
}

/* Sort for reading: what is being worked first, then by priority, then the
   most recently touched. */
function beadsSortIssues(issues) {
  const rank = { in_progress: 0, in_review: 1, blocked: 2, open: 3, closed: 9 };
  return [...issues].sort((a, b) =>
    (rank[a.status] ?? 8) - (rank[b.status] ?? 8) ||
    (a.priority ?? 9) - (b.priority ?? 9) ||
    String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
}

function beadsStatusBadge(status) {
  const cls = { in_progress: "busy", in_review: "review", blocked: "blocked",
                open: "open", closed: "exited" }[status] || "";
  return el("span", `badge beads-status ${cls}`, status || "?");
}

/* One issue, one row. `compact` is the rail's shape (no session column —
   the rail already IS one session). A session tag is a link to that
   session's terminal, carrying why it matched in its title. */
function beadsIssueRow(issue, opts = {}) {
  const row = el("div", "beads-row");
  if (issue.id === beadsFocus && !opts.compact) row.classList.add("on");
  const id = el("a", "beads-id", issue.id || "?");
  id.href = "#/beads/" + encodeURIComponent(issue.id || "");
  id.title = "open this issue";
  row.appendChild(id);
  row.appendChild(beadsStatusBadge(issue.status));
  if (issue.priority !== undefined && issue.priority !== null) {
    row.appendChild(el("span", "beads-pri", `P${issue.priority}`));
  }
  const text = el("div", "beads-text");
  text.appendChild(el("span", "beads-title", issue.title || "(untitled)"));
  const bits = [];
  if (issue.issue_type && issue.issue_type !== "task") bits.push(issue.issue_type);
  for (const l of issue.labels || []) bits.push("#" + l);
  if (issue.assignee) bits.push("→ " + issue.assignee);
  if (opts.compact && issue.via) bits.push("via " + issue.via.join(", "));
  if (bits.length) text.appendChild(el("span", "beads-bits", bits.join("  ")));
  row.appendChild(text);
  if (!opts.compact) {
    const who = el("span", "beads-sessions");
    for (const s of issue.sessions || []) {
      const tag = el("a", `beads-sess ${s.status || ""}`, s.name);
      tag.href = "#/s/" + encodeURIComponent(s.name);
      tag.title = `${s.name} (${s.status || "?"}) — ${(s.via || []).join(", ")}`;
      who.appendChild(tag);
    }
    row.appendChild(who);
  }
  return row;
}

function beadsFilterBar() {
  const bar = el("div", "seq-tabs beads-filters");
  for (const f of ["active", ...BEADS_STATUSES, "all"]) {
    const b = el("button", "seq-tab" + (beadsFilter === f ? " on" : ""), f);
    b.type = "button";
    b.addEventListener("click", () => { beadsFilter = f; renderBeads(); });
    bar.appendChild(b);
  }
  const sel = document.createElement("select");
  sel.className = "beads-session-pick";
  sel.title = "only the issues the daemon ties to this session";
  const any = document.createElement("option");
  any.value = ""; any.textContent = "every session";
  sel.appendChild(any);
  const names = new Set();
  for (const b of (beadsCache && beadsCache.boards) || []) {
    for (const s of b.sessions || []) names.add(s.name);
  }
  for (const s of sessionsCache) names.add(s.name);
  for (const n of [...names].sort()) {
    const o = document.createElement("option");
    o.value = n; o.textContent = n;
    if (n === beadsSession) o.selected = true;
    sel.appendChild(o);
  }
  sel.addEventListener("change", () => { beadsSession = sel.value; renderBeads(); });
  bar.appendChild(sel);
  /* Two readings of the same board, because a family does not fit in a
     column: the lanes say what state everything is in, and the tree says
     what hangs off what across every state at once. */
  const lay = el("div", "seq-tabs beads-layout");
  for (const [key, label] of [["board", "lanes"], ["tree", "tree"]]) {
    const b = el("button", "seq-tab" + (beadsLayout === key ? " on" : ""), label);
    b.type = "button";
    b.title = key === "board"
      ? "one lane per status"
      : "the parent-child forest, every status together";
    b.addEventListener("click", () => { beadsLayout = key; renderBeads(); });
    lay.appendChild(b);
  }
  bar.appendChild(lay);
  return bar;
}

/* ---- the hierarchy ---------------------------------------------------- */
/* The board's parent-child edges, resolved into a forest.

   `br` stores an edge on the DEPENDING side, and `br dep add <child> <parent>
   --type parent-child` makes the child the depending one — so `from` is the
   child and `to` is the parent (the daemon's `edges` carries that direction
   through untouched). Only `parent-child` builds the tree: `blocks` is a
   different relation between peers, and nesting by it would say something the
   board does not mean.

   Three things this must survive, because a board is written by agents and
   nothing stops them: an edge pointing at an issue that is not on this board
   (dropped — there is nothing to nest under), an issue given two parents (the
   lowest id wins, so the drawing does not shuffle between polls), and a cycle
   (every edge that would close one is dropped, leaving those issues as roots
   rather than hanging the walk). */
function beadsHierarchy(issues, deps) {
  const byId = new Map();
  for (const i of issues || []) if (i && i.id) byId.set(i.id, i);
  const cand = new Map();
  for (const d of deps || []) {
    if (!d || d.type !== "parent-child") continue;
    const child = d.from, up = d.to;
    if (child === up || !byId.has(child) || !byId.has(up)) continue;
    const cur = cand.get(child);
    if (cur === undefined || String(up) < String(cur)) cand.set(child, up);
  }
  const parent = new Map();
  for (const [child, up] of cand) {
    const seen = new Set([child]);
    let at = up, ok = true;
    while (at !== undefined) {
      if (seen.has(at)) { ok = false; break; }
      seen.add(at);
      at = cand.get(at);
    }
    if (ok) parent.set(child, up);
  }
  const kids = new Map();
  for (const [child, up] of parent) {
    if (!kids.has(up)) kids.set(up, []);
    kids.get(up).push(child);
  }
  const rank = (ids) =>
    beadsSortIssues(ids.map((x) => byId.get(x))).map((i) => i.id);
  const order = [];
  const walk = (id) => {
    order.push(id);
    for (const k of rank(kids.get(id) || [])) walk(k);
  };
  for (const r of rank([...byId.keys()].filter((id) => !parent.has(id)))) walk(r);
  return { parent, kids, order };
}

/* The rows of one lane, in forest order and indented by the ancestors that
   are IN THIS LANE.

   A kanban splits a family across columns — a child `in_progress` under a
   parent still `open` — so indenting by true depth would push a card in on
   account of a parent the reader cannot see beside it. Indenting by the
   visible ancestors instead means the nesting a lane draws is nesting a
   reader can follow, and the parent that is elsewhere is said on the card
   instead: `parentHere` is false and the card links up to it. */
function beadsLaneRows(issues, tree) {
  const here = new Map();
  for (const i of issues || []) here.set(i.id, i);
  const rank = new Map(tree.order.map((id, n) => [id, n]));
  const rows = [...(issues || [])].sort(
    (a, b) => (rank.get(a.id) ?? 1e9) - (rank.get(b.id) ?? 1e9));
  return rows.map((issue) => {
    let indent = 0, at = tree.parent.get(issue.id);
    const seen = new Set([issue.id]);
    while (at !== undefined && !seen.has(at)) {
      if (here.has(at)) indent++;
      seen.add(at);
      at = tree.parent.get(at);
    }
    const up = tree.parent.get(issue.id);
    return {
      issue,
      indent,
      parent: up === undefined ? "" : up,
      parentHere: up !== undefined && here.has(up),
      kids: (tree.kids.get(issue.id) || []).length,
    };
  });
}

/* One card. `beadsIssueRow` is still the row shape and still the rail's — a
   rail is one column wide, where a card would only be a row that wrapped. */
function beadsCard(row) {
  const issue = row.issue || {};
  const href = "#/beads/" + encodeURIComponent(issue.id || "");
  const card = el("div", "beads-card");
  if (issue.id === beadsFocus) card.classList.add("on");
  card.classList.add("pri" + (issue.priority ?? 9));
  // Indent as a class, not an inline style: the step is a CSS decision and
  // the depth is capped there too -- a chain deeper than four would otherwise
  // walk a card off the right edge of a lane that is 210px wide.
  if (row.indent) card.classList.add("nested", "ind" + Math.min(row.indent, 4));

  const top = el("div", "beads-card-top");
  const id = el("a", "beads-id", issue.id || "?");
  id.href = href;
  id.title = "open this issue";
  top.appendChild(id);
  if (issue.priority !== undefined && issue.priority !== null) {
    top.appendChild(el("span", "beads-pri", `P${issue.priority}`));
  }
  card.appendChild(top);

  const title = el("a", "beads-card-title", issue.title || "(untitled)");
  title.href = href;
  card.appendChild(title);

  /* Where this card sits in the family, said only when the nesting cannot say
     it: a parent that is not in this lane, and how many children hang off
     this one (they may be in any lane, so the count is the only place a
     reader learns the card is a parent at all). */
  const rel = el("div", "beads-card-rel");
  if (row.parent && !row.parentHere) {
    const up = el("a", "beads-rel-up", "↰ " + row.parent);
    up.href = "#/beads/" + encodeURIComponent(row.parent);
    up.title = "its parent, which is not in this lane";
    rel.appendChild(up);
  }
  if (row.kids) {
    rel.appendChild(el("span", "beads-rel-kids",
      row.kids === 1 ? "1 child" : `${row.kids} children`));
  }
  if (rel.children.length) card.appendChild(rel);

  /* The badge row. Anything a later round wants to flag on a card without
     re-cutting the layout goes here (claunch-3dgs wants "has reports"), which
     is why it is a row of its own rather than more text on the title. */
  const badges = el("div", "beads-card-badges");
  if (issue.issue_type && issue.issue_type !== "task") {
    badges.appendChild(el("span", "beads-badge type", issue.issue_type));
  }
  for (const l of issue.labels || []) {
    badges.appendChild(el("span", "beads-badge label", "#" + l));
  }
  if (issue.assignee) {
    badges.appendChild(el("span", "beads-badge who", "→ " + issue.assignee));
  }
  if (badges.children.length) card.appendChild(badges);

  const who = el("div", "beads-sessions");
  for (const s of issue.sessions || []) {
    const tag = el("a", `beads-sess ${s.status || ""}`, s.name);
    tag.href = "#/s/" + encodeURIComponent(s.name);
    tag.title = `${s.name} (${s.status || "?"}) — ${(s.via || []).join(", ")}`;
    who.appendChild(tag);
  }
  if (who.children.length) card.appendChild(who);
  return card;
}

/* Which lanes a board draws, from the status filter. `active` is the four
   that are still work and `all` adds closed; picking one status is a board of
   one lane, which is the honest drawing of that filter rather than four lanes
   with three of them empty. */
function beadsLanes(filter) {
  if (filter === "all") return BEADS_STATUSES;
  if (filter === "active") return BEADS_STATUSES.filter((s) => BEADS_ACTIVE.has(s));
  return BEADS_STATUSES.includes(filter) ? [filter] : BEADS_STATUSES;
}

function beadsLane(status, rows) {
  const lane = el("div", `beads-lane ${status}`);
  const head = el("div", "beads-lane-head");
  head.appendChild(el("span", "beads-lane-name", status));
  head.appendChild(el("span", "beads-lane-count", String(rows.length)));
  lane.appendChild(head);
  const body = el("div", "beads-lane-body");
  if (!rows.length) body.appendChild(el("p", "beads-lane-empty", "—"));
  for (const r of rows) body.appendChild(beadsCard(r));
  lane.appendChild(body);
  return lane;
}

function beadsBoardSection(board) {
  const sec = el("div", "beads-board");
  const head = el("div", "beads-board-head");
  head.appendChild(el("h3", null, board.root || "?"));
  const live = (board.sessions || []).filter((s) => s.status !== "exited");
  head.appendChild(el("span", "wf-note",
    live.length ? live.map((s) => s.name).join(" · ") : "no live session here"));
  sec.appendChild(head);
  if (board.error) {
    sec.appendChild(el("p", "wf-warning", board.error));
    return sec;
  }
  // The forest is built from the WHOLE board, not from what the filter left:
  // a parent is a fact about the board, and one filtered out of view must
  // still be named on its child's card rather than quietly making that child
  // a root.
  const tree = beadsHierarchy(board.issues, board.deps);
  const shown = beadsFilterIssues(board.issues, beadsFilter, beadsSession);
  if (!shown.length) {
    sec.appendChild(el("p", "wf-note",
      `nothing ${beadsFilter === "all" ? "" : beadsFilter + " "}here` +
      (beadsSession ? ` for ${beadsSession}` : "")));
    return sec;
  }
  if (beadsLayout === "tree") {
    // One lane's worth of rows over the whole visible board: every ancestor
    // that survived the filter is beside its children, which is the reading
    // the columns give up in exchange for showing state at a glance.
    const list = el("div", "beads-tree");
    for (const r of beadsLaneRows(shown, tree)) list.appendChild(beadsCard(r));
    sec.appendChild(list);
    return sec;
  }
  const lanes = beadsLanes(beadsFilter);
  const grid = el("div", "beads-lanes");
  for (const status of lanes) {
    grid.appendChild(beadsLane(
      status, beadsLaneRows(shown.filter((i) => i.status === status), tree)));
  }
  sec.appendChild(grid);
  return sec;
}

/* The opened issue's place in the family, built from the board listing's own
   edges rather than from the issue payload. `br show` resolves what an issue
   depends on, so the parent is in there — but nothing on that side names the
   children, and the children are half of what a reader opens a parent to see.
   The listing has both directions, so both come from there or neither does.

   Returns null when there is no family to draw, so the pane spends no room
   saying an issue is unrelated to everything — which is most of them. */
function beadsRelationBlock(id) {
  const board = ((beadsCache && beadsCache.boards) || []).find(
    (b) => (b.issues || []).some((i) => i.id === id));
  if (!board) return null;
  const byId = new Map((board.issues || []).map((i) => [i.id, i]));
  const tree = beadsHierarchy(board.issues, board.deps);
  const up = tree.parent.get(id);
  const kids = tree.kids.get(id) || [];
  if (up === undefined && !kids.length) return null;
  const box = el("div", "beads-detail-rel");
  const line = (label, issue) => {
    const row = el("div", "beads-rel-row");
    row.appendChild(el("span", "beads-rel-label", label));
    row.appendChild(beadsStatusBadge(issue.status));
    const a = el("a", "beads-rel-link", issue.id);
    a.href = "#/beads/" + encodeURIComponent(issue.id);
    row.appendChild(a);
    row.appendChild(el("span", "beads-rel-title", issue.title || "(untitled)"));
    return row;
  };
  if (up !== undefined && byId.has(up)) {
    box.appendChild(line("parent", byId.get(up)));
  }
  const rows = beadsSortIssues(kids.map((k) => byId.get(k)).filter(Boolean));
  rows.forEach((k, n) => box.appendChild(
    line(n ? "" : `children (${rows.length})`, k)));
  return box;
}

/* The opened issue: what the row cannot show — description and comments. */
function beadsDetailPane() {
  const pane = el("div", "beads-detail");
  const head = el("div", "beads-detail-head");
  head.appendChild(el("h3", null, beadsFocus));
  const close = el("button", "sess-close", "×");
  close.title = "close";
  close.addEventListener("click", () => { location.hash = "#/beads"; });
  head.appendChild(close);
  pane.appendChild(head);
  if (!beadsDetail) {
    pane.appendChild(el("p", "wf-note", "loading…"));
    return pane;
  }
  if (beadsDetail.error) {
    pane.appendChild(el("p", "wf-warning", beadsDetail.error));
    return pane;
  }
  const i = beadsDetail.issue || {};
  pane.appendChild(el("h2", "beads-detail-title", i.title || "(untitled)"));
  const meta = el("div", "beads-detail-meta");
  meta.appendChild(beadsStatusBadge(i.status));
  const facts = [];
  if (i.priority !== undefined) facts.push(`P${i.priority}`);
  if (i.issue_type) facts.push(i.issue_type);
  if (i.assignee) facts.push("assignee " + i.assignee);
  if (i.created_by) facts.push("by " + i.created_by);
  for (const l of i.labels || []) facts.push("#" + l);
  if (i.updated_at) facts.push("updated " + String(i.updated_at).replace("T", " ").slice(0, 19));
  meta.appendChild(el("span", "beads-bits", facts.join("  ·  ")));
  pane.appendChild(meta);
  const rel = beadsRelationBlock(i.id || beadsFocus);
  if (rel) pane.appendChild(rel);
  // The third section of this pane, and the last one still drawn without a
  // heading. Reports and Comments both announce themselves; the issue's own
  // text just began, so a reader scrolling in landed in the middle of prose
  // with nothing saying what it was. One rule draws all three now.
  if (i.description) {
    pane.appendChild(el("h4", null, "Description"));
    pane.appendChild(el("pre", "beads-desc", i.description));
  }
  if (i.close_reason) pane.appendChild(el("p", "wf-note", "closed: " + i.close_reason));
  // The rounds that were written up for this issue. Keyed by issue across
  // every session, so a closed issue whose session ended long ago still hands
  // its write-up back — that reader is the whole point of keeping the pages
  // outside sessions/<name>/ in the first place.
  const reports = beadsDetail.reports || [];
  if (reports.length) pane.appendChild(sessReports(reports, { by: "session" }));
  const comments = i.comments || [];
  pane.appendChild(el("h4", null, `Comments (${comments.length})`));
  for (const c of comments) {
    const box = el("div", "beads-comment");
    box.appendChild(el("span", "beads-bits",
      `${c.author || c.actor || "?"} · ${String(c.created_at || "").replace("T", " ").slice(0, 19)}`));
    box.appendChild(el("pre", "beads-desc", c.text || c.body || c.content || ""));
    pane.appendChild(box);
  }
  return pane;
}

function renderBeads() {
  const view = $("beads-view");
  if (formInUse(view)) return;
  view.innerHTML = "";
  const head = el("div", "wf-head");
  head.appendChild(el("h2", null, "Beads"));
  const back = el("button", "wf-btn clear", "Back");
  back.addEventListener("click", () => { location.hash = "#"; });
  head.appendChild(back);
  view.appendChild(head);
  view.appendChild(el("p", "wf-note",
    "The repository board (beads), by session: each issue carries the " +
    "sessions the daemon ties it to — the recorded link, assignee, " +
    "creator, or an `issue: <id>` in the session's task. Writes are the " +
    "agents' (`claunch beads …`); the daemon registers an issue at " +
    "creation, winds a session down before a kill, and returns what it " +
    "was working on to open when it exits."));
  if (beadsError) view.appendChild(el("p", "wf-warning", beadsError));
  if (!beadsCache) {
    if (!beadsError) view.appendChild(el("p", "wf-note", "loading…"));
    return;
  }
  view.appendChild(beadsFilterBar());
  const body = el("div", "beads-body" + (beadsFocus ? " split" : ""));
  const list = el("div", "beads-list");
  const boards = beadsCache.boards || [];
  if (!boards.length) {
    list.appendChild(el("p", "wf-note",
      "no board: none of the sessions' directories is a repository with a " +
      ".beads/ — 'claunch beads init --prefix <name>' at its root starts one"));
  }
  for (const b of boards) list.appendChild(beadsBoardSection(b));
  body.appendChild(list);
  if (beadsFocus) body.appendChild(beadsDetailPane());
  view.appendChild(body);
}

/* ---- the rail's block: one session's slice of its board ---- */
let sessBeadsBox = null;   // the create form, which holds a typed title

function sessBeads(data) {
  const s = data.session || {};
  const b = data.beads || {};
  const issues = b.issues || [];
  const box = el("div", "sess-beads");
  box.appendChild(el("h3", null, `Beads (${issues.length})`));
  if (b.winddown) {
    const w = el("p", "wf-warning",
      `winding down since ${String(b.winddown.since || "").replace("T", " ").slice(0, 19)} — ` +
      `asked to settle ${(b.winddown.issues || []).join(", ")}; terminated once ` +
      `idle or after ${Math.round(b.winddown.grace || 0)}s. Kill again to stop now.`);
    box.appendChild(w);
  }
  // Reports come before the board's error return on purpose, mirroring the
  // daemon filling them before its own early returns: a machine without `br`
  // still has the pages its sessions wrote, and the one thing this panel can
  // still show must not be hidden behind the board being unreachable.
  const reports = b.reports || [];
  if (reports.length) box.appendChild(sessReports(reports));
  if (b.error) {
    box.appendChild(el("p", "wf-note", b.error));
    return box;
  }
  if (!issues.length) {
    box.appendChild(el("p", "wf-note", "no issue on the board names this session"));
  }
  for (const i of issues) box.appendChild(beadsIssueRow(i, { compact: true }));
  if (!b.issue && s.name && s.status !== "exited") {
    box.appendChild(sessBeadsCreate(s.name));
  }
  const open = el("button", "wf-btn option", "Open board");
  open.title = "the Beads page, filtered to this session";
  open.addEventListener("click", () => {
    beadsSession = s.name || "";
    go("#/beads");
  });
  box.appendChild(open);
  return box;
}

/* The HTML pages a round left behind, newest first. The daemon indexes them
   by reading its reports directory (the filenames carry the time and the
   issue), and serves each one sandboxed, so these are ordinary links — the
   dashboard's cookie authenticates them and a new tab is the right place for
   a page that was written to be read on its own.

   A card, and that is the whole of what was wrong here. This block sits in a
   pane that goes on to stack tens of pre-formatted comment blocks under it,
   and as one line of 13px blue text it disappeared into them: the most
   valuable thing in the pane was the weakest thing drawn in it. The weight is
   carried by the card, which is what lets the heading drop to an h4 and agree
   with the section beside it instead of competing with it.

   The card is drawn on this element itself, never on a wrapper around it —
   the pane's order is checked elsewhere by reading its children's first class
   (beads_check), so an extra div here would silently break that. */
function sessReports(reports, opts = {}) {
  // Same rows in both places; only the label differs. On a session's page the
  // reports all share a session, so the issue is what tells them apart — on an
  // issue's page they all share the issue, so the session does. The label is
  // whichever half is not already the heading of the page you are on.
  const bySession = opts.by === "session";
  const box = el("div", "sess-reports");
  // An h4, deliberately: this was an h3 while Comments beside it was an h4,
  // so two sections at the same level of the same pane were drawn at two
  // different levels. They share one rule now (.beads-detail h4).
  const head = el("h4", "sess-reports-head");
  head.appendChild(el("span", "sess-reports-mark", "▤"));
  head.appendChild(el("span", "sess-reports-name", "Round reports"));
  head.appendChild(el("span", "sess-reports-count", String(reports.length)));
  box.appendChild(head);
  // What a row IS, said once for the block instead of not at all. Neither
  // half of a row ("s121", "19 KB") says it, and a reader who has not met one
  // of these pages cannot tell this link from any other link on the page.
  box.appendChild(el("p", "sess-reports-what", bySession
    ? "The write-up each session left when its round on this issue ended — " +
      "one HTML page, opening in its own tab."
    : "The write-up this session left at the end of each round — one HTML " +
      "page, opening in its own tab."));
  for (const r of reports) box.appendChild(sessReportRow(r, bySession));
  return box;
}

/* One round. The whole row is the link, because the report is the only thing
   in this block worth clicking — the target used to be the four characters of
   a session name, which is a hard thing to hit and an easy thing to miss.

   Through url(), like every other request the page makes. The daemon hands
   back its own absolute path ("/api/sessions/<s>/reports/<f>") because it
   cannot know how it was reached; resolving that against BASE is the
   browser's half, and this link skipped it. Reached through a relay tunnel
   the dashboard sits under "/t/<backend>/", so the unresolved href walked out
   of the tunnel and hit the relay's own 404 instead of the daemon — which is
   exactly what a reader saw when they copied a report link out of a relayed
   dashboard and got "Not Found".

   The sweep behind that is "every `.href =` assignment in this file" — 23 of
   them, and these two were the only ones handed a daemon path rather than a
   hash route. Say the shape, because it is not the same claim as "the only
   such link on the page": `setAttribute("href", ...)` is outside that
   regex's shape, and mdLink() is exactly that — MD_SAFE_HREF admits a
   root-relative path, so a report body can still carry an unresolved one.
   That second site is claunch-xntk, not this change. */
function sessReportRow(r, bySession) {
  const row = el("a", "sess-report sess-report-link");
  row.href = url(r.url);
  row.target = "_blank";
  row.rel = "noopener";
  row.title = bySession
    ? `the round ${r.session || "a session"} wrote up for this issue`
    : `this session's write-up of the round it spent on ${r.issue || "no issue"}`;
  row.appendChild(el("span", "sess-report-name",
    (bySession ? r.session : r.issue) || "round report"));
  // Never the raw byte count: 19591 is what the filesystem knows, and 19 KB
  // is what tells a reader whether this is a write-up or a stub.
  row.appendChild(el("span", "sess-report-bits",
    `${reportWhen(r.at)} · ${fmtReportSize(r.size)}`));
  return row;
}

/* An issue for a session that has none — the one write this panel makes.
   Hoisted across renders (the poll rebuilds the panel every 2 s) and rebuilt
   only when the rail points at another session. */
function sessBeadsCreate(name) {
  if (sessBeadsBox && sessBeadsBox.dataset.session === name) return sessBeadsBox;
  const form = el("form", "sess-beads-create");
  form.dataset.session = name;
  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = "register an issue for this session — title";
  input.maxLength = 140;
  form.appendChild(input);
  const btn = el("button", "wf-btn", "Create");
  btn.type = "submit";
  form.appendChild(btn);
  const note = el("span", "wf-note", "");
  form.appendChild(note);
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const title = input.value.trim();
    if (!title) return;
    btn.disabled = true;
    note.textContent = "";
    try {
      const resp = await api(`/api/sessions/${encodeURIComponent(name)}/beads`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title }),
      });
      const doc = await resp.json().catch(() => ({}));
      if (!resp.ok) note.textContent = doc.error || `HTTP ${resp.status}`;
      else { input.value = ""; sessBeadsBox = null; refreshSession(); }
    } catch { /* auth overlay is up */ }
    finally { btn.disabled = false; }
  });
  sessBeadsBox = form;
  return form;
}

/* ---- the rail's block: what this session committed ---- */
/* The commits carrying this session's `Claunch-Session` trailer, newest
   first, read straight out of the repository under the session's directory
   (see session_commits.py). Nothing is recorded for this block to read, so
   there is no state to go stale: a session killed mid-round still shows every
   commit it managed to make, and a commit someone rewrites away stops being
   listed the moment it stops existing.

   Empty is a real answer and is drawn, not hidden — a block that appears only
   on success reads as one that failed to load the rest of the time. But it is
   said as a fact about the READING, not about the session: "no stamped commit
   found" rather than "this session committed nothing". The difference is not
   pedantry. `for_session` returns the same empty list for a repository it
   could not read as for one it read and found nothing in — a pruned worktree,
   no git on PATH, a walk that timed out — so the strong sentence would be a
   claim this page has no way to check. Telling those two apart is
   claunch-j5kp; until it lands, the weaker sentence is the true one.

   Nothing at all is drawn when the daemon serves no `commits` — an older
   daemon, or a session with no directory to read, which api.py reports as
   null for exactly the reason above. */
function sessCommits(data) {
  const c = data.commits;
  const box = el("div", "sess-commits");
  // No answer is not an answer of "none": an old daemon, or a session whose
  // directory the daemon has none of, and neither is evidence about commits.
  if (!c) return box;
  const rows = c.commits || [];
  box.appendChild(el("h3", null, `Commits (${c.count || rows.length || 0})`));
  box.appendChild(el("p", "wf-note",
    "what this session committed in its directory, read from the " +
    "Claunch-Session trailer on each commit — `claunch commits` prints the " +
    "same list."));
  if (!rows.length) {
    box.appendChild(el("p", "wf-note", "no stamped commit found"));
    return box;
  }
  for (const r of rows) {
    const row = el("div", "sess-commit");
    row.appendChild(el("span", "sess-commit-sha", r.short || ""));
    row.appendChild(el("span", "sess-commit-subject", r.subject || ""));
    const bits = [String(r.committed_at || "").replace("T", " ").slice(0, 19)];
    if (r.worktree) bits.push(r.worktree);
    row.appendChild(el("span", "sess-commit-bits", bits.join("  ·  ")));
    row.title = r.sha || "";
    box.appendChild(row);
  }
  return box;
}

/* ------------------------------------------------------------------ */
/* the Reports page: every round report on this machine               */
/* ------------------------------------------------------------------ */
/* The third reading of these pages, beside the two that already exist: a
   session's rail block ("what did this one leave?") and an issue's detail
   pane ("what was written up for this?"). Both of those need something in
   hand. This page is for the reader who has neither — and it is the reading
   the files were kept outside sessions/<name>/ for, because most of what it
   lists was written by sessions the daemon has long since forgotten.

   One fetch (/api/reports), whose rows come off the daemon's disk rather
   than its registry, so a cleared session's round is still here. What the
   registry does know rides along per row (session_status) and is DRAWN
   rather than filtered on: the sessions that are gone are not an edge case
   of this page, they are most of it. */
let reportsCache = null;      // the rows, newest first; null until the first answer
let reportsError = "";
let reportsTimer = null;
let reportsOpen = false;
/* The narrowing, held across renders the way the Beads page holds its own —
   a poll that redrew the table must not throw away what the reader picked. */
let reportsState = "all";     // all | live | ended | gone
let reportsSession = "";
let reportsIssue = "";
let reportsOldest = false;

function openReports() {
  reportsOpen = true;
  showView("reports");
  renderReports();
  refreshReports();
  // 30 s, not the rail's 2. A report is written once per round — tens of
  // minutes apart at the very best — and each tick costs the daemon a
  // listdir per session plus a read per file to tell a page from a stub.
  if (!reportsTimer) reportsTimer = setInterval(refreshReports, 30000);
}

function stopReportsPoll() {
  if (reportsTimer) { clearInterval(reportsTimer); reportsTimer = null; }
  reportsOpen = false;
}

async function refreshReports() {
  if (!reportsOpen) return;
  try {
    const resp = await api("/api/reports");
    if (resp.status === 404) {
      // The daemon on the other end is older than this page. Say so rather
      // than drawing an empty table, which would read as "nothing written".
      reportsError = "this daemon predates the Reports page — 'claunch " +
        "daemon restart' to pick up this version";
      reportsCache = reportsCache || [];
    } else {
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) reportsError = data.error || `HTTP ${resp.status}`;
      else { reportsCache = data.reports || []; reportsError = ""; }
    }
  } catch { return; }   // auth overlay is up, or the daemon is away
  if (reportsOpen) renderReports();
}

/* What the daemon still knows about the session that wrote a row. Three
   states, and the page draws all three: a report outlives its session by
   design, so "no record" is this page's ordinary case rather than its broken
   one — it is what a cleared session looks like from the one side that still
   holds the evidence. */
function reportSessionState(r) {
  const st = (r && r.session_status) || "";
  if (!st) return "gone";
  return st === "exited" ? "ended" : "live";
}

/* The rows on screen: the narrowing, then the order. Never a re-sort — the
   daemon already answered newest-first, and oldest-first is that same list
   read the other way, so the two orders cannot disagree about a tie. */
function reportsShown(rows) {
  const out = (rows || []).filter((r) =>
    (reportsState === "all" || reportSessionState(r) === reportsState) &&
    (!reportsSession || r.session === reportsSession) &&
    (!reportsIssue || (r.issue || "") === reportsIssue));
  return reportsOldest ? out.slice().reverse() : out;
}

/* A report's size, said the way a person reads it. The bytes are what the
   filesystem knows; "19 KB" is what tells a reader at a glance whether this
   is a write-up or a stub. */
function fmtReportSize(n) {
  const b = Number(n);
  if (!Number.isFinite(b) || b < 0) return "";
  if (b < 1024) return `${Math.round(b)} B`;
  const k = b / 1024;
  if (k < 1024) return `${k < 10 ? k.toFixed(1) : Math.round(k)} KB`;
  const m = k / 1024;
  return `${m < 10 ? m.toFixed(1) : Math.round(m)} MB`;
}

/* The stamp, which came off the filename. It is UTC because the daemon names
   the file that way (the name has to sort), and it is shown as written
   rather than moved into the reader's zone — the same string is in the URL
   of the page it opens, and the two must be readable as one thing. */
function reportWhen(iso) {
  return String(iso || "").replace("T", " ").replace("Z", " UTC");
}

const REPORT_STATES = [
  ["all", "all", "every round on this machine"],
  ["live", "live session", "rounds whose session is still running"],
  ["ended", "ended", "rounds whose session finished, record still here"],
  ["gone", "no record", "rounds whose session the daemon no longer knows — " +
                        "cleared, or from an install that is gone"],
];

function reportsFilterBar() {
  const bar = el("div", "seq-tabs reports-filters");
  for (const [key, label, why] of REPORT_STATES) {
    const b = el("button", "seq-tab" + (reportsState === key ? " on" : ""), label);
    b.type = "button";
    b.title = why;
    b.addEventListener("click", () => { reportsState = key; renderReports(); });
    bar.appendChild(b);
  }
  const rows = reportsCache || [];
  bar.appendChild(reportsPick(
    "session", [...new Set(rows.map((r) => r.session))].sort(), reportsSession,
    (v) => { reportsSession = v; renderReports(); }));
  bar.appendChild(reportsPick(
    "issue", [...new Set(rows.map((r) => r.issue).filter(Boolean))].sort(),
    reportsIssue, (v) => { reportsIssue = v; renderReports(); }));
  const order = el("button", "wf-btn clear reports-order",
    reportsOldest ? "oldest first" : "newest first");
  order.type = "button";
  order.title = "flip the order";
  order.addEventListener("click", () => { reportsOldest = !reportsOldest; renderReports(); });
  bar.appendChild(order);
  return bar;
}

/* One of the two narrowing pickers. Its options are whatever the rows
   actually carry, not the fleet: a filter offering a session with no report
   would be offering an empty page. */
function reportsPick(kind, values, current, onPick) {
  const sel = document.createElement("select");
  sel.className = "reports-pick";
  sel.title = kind === "session"
    ? "only the rounds this session wrote up"
    : "only the rounds written up for this issue";
  const any = document.createElement("option");
  any.value = "";
  any.textContent = kind === "session" ? "every session" : "every issue";
  sel.appendChild(any);
  for (const v of values) {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = v;
    if (v === current) o.selected = true;
    sel.appendChild(o);
  }
  sel.addEventListener("change", () => onPick(sel.value));
  return sel;
}

/* One row. The link that matters is the report itself, so it leads and it is
   the largest thing in the line; the rest says which round this was. The
   session is a second link where there is still something to walk to, and
   plain text where there is not — a link to a session the daemon has never
   heard of is a promise the page cannot keep. */
function reportsRow(r) {
  const row = el("div", "reports-row");
  const open = el("a", "reports-open");
  // Resolved against BASE, for the same reason sessReportRow is: served
  // through a relay tunnel this page lives under "/t/<backend>/", and the
  // daemon's own absolute path leaves the tunnel.
  open.href = url(r.url);
  open.target = "_blank";
  open.rel = "noopener";
  open.title = `open the round ${r.session} wrote up` +
    (r.issue ? ` for ${r.issue}` : ", which named no issue");
  open.appendChild(el("span", "reports-mark", "▤"));
  open.appendChild(el("span", "reports-issue", r.issue || "no issue"));
  row.appendChild(open);

  const state = reportSessionState(r);
  const sess = el(state === "gone" ? "span" : "a", "reports-sess " + state);
  if (state !== "gone") sess.href = `#/s/${encodeURIComponent(r.session)}`;
  sess.title = state === "gone"
    ? `${r.session} — no record of this session any more; its round is here ` +
      "because reports are kept outside the session directory"
    : `${r.session} — ${r.session_status}`;
  sess.appendChild(el("span", "reports-sess-name", r.session));
  sess.appendChild(el("span", "reports-sess-state",
    { live: "running", ended: "ended", gone: "no record" }[state]));
  row.appendChild(sess);

  row.appendChild(el("span", "reports-when", reportWhen(r.at)));
  row.appendChild(el("span", "reports-size", fmtReportSize(r.size)));
  if (r.issue) {
    const board = el("a", "reports-board", "issue ↗");
    board.href = `#/beads/${encodeURIComponent(r.issue)}`;
    board.title = `${r.issue} on the board`;
    row.appendChild(board);
  }
  return row;
}

function renderReports() {
  const view = $("reports-view");
  view.innerHTML = "";
  const head = el("div", "wf-head");
  head.appendChild(el("h2", null, "Reports"));
  const back = el("button", "wf-btn clear", "Back");
  back.addEventListener("click", () => { location.hash = "#"; });
  head.appendChild(back);
  view.appendChild(head);
  view.appendChild(el("p", "wf-note",
    "Every round report on this machine, newest first. One HTML page per " +
    "round, written by the session that ran it and kept outside that " +
    "session's own directory — so the write-up stays readable long after " +
    "the session itself was cleared. Most of the rows below are exactly " +
    "that, which is why a session that is gone is marked here rather than " +
    "dropped."));
  if (reportsError) view.appendChild(el("p", "wf-warning", reportsError));
  if (!reportsCache) {
    if (!reportsError) view.appendChild(el("p", "wf-note", "loading…"));
    return;
  }
  if (!reportsCache.length) {
    view.appendChild(el("p", "wf-note",
      "No round has been written up yet. A session files one with " +
      "'claunch report save <file>'; the workflows' wrapup step is what " +
      "asks for it."));
    return;
  }
  view.appendChild(reportsFilterBar());
  const rows = reportsShown(reportsCache);
  const list = el("div", "reports-list");
  list.appendChild(el("p", "reports-count",
    rows.length === reportsCache.length
      ? `${rows.length} report${rows.length === 1 ? "" : "s"}`
      : `${rows.length} of ${reportsCache.length} reports`));
  if (!rows.length) {
    list.appendChild(el("p", "wf-note",
      "nothing matches — the filters above are narrower than the machine"));
  }
  for (const r of rows) list.appendChild(reportsRow(r));
  view.appendChild(list);
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
  renderWfInto($("wf-view"), data, wfPageUi);
}

/* The workflow view rebuilds itself on every 2s poll (renderWfInto wipes the
   container), and that rebuild was costing the reader's place: the diagram,
   the reports column and a long gate's prose all came back scrolled to the
   top two seconds after they moved it. Capture the run view's scrollers
   before the wipe and put them back after, so a poll lets the run progress
   without dragging the reader with it. The near-end rule is the seq-scroll
   one: whoever is riding the tail keeps riding it as new content lands,
   anywhere else a poll leaves the position alone. .wf-cols carries only the
   sideways travel (the columns scroll beneath it), so it is restored exactly
   and never follows width growth. */
function captureWfScrolls(view) {
  const at = (sel) => {
    const n = view.querySelector(sel);
    return n && {
      top: n.scrollTop,
      left: n.scrollLeft,
      nearEnd: n.scrollHeight - n.scrollTop - n.clientHeight < 8,
    };
  };
  return {
    cols: at(".wf-cols"),
    side: at(".wf-side"),
    diagram: at(".wf-diagram"),
    bar: at(".wf-bar"),
  };
}

function restoreWfScrolls(view, keep) {
  if (!keep) return;
  const cols = view.querySelector(".wf-cols");
  if (cols && keep.cols) cols.scrollLeft = keep.cols.left;
  for (const [sel, key] of [[".wf-side", "side"], [".wf-diagram", "diagram"],
                            [".wf-bar", "bar"]]) {
    const el = view.querySelector(sel);
    const s = keep[key];
    if (el && s) el.scrollTop = s.nearEnd ? el.scrollHeight : s.top;
  }
}

/* Which session owns this run, drawn as the head's chip. It is identity and
   not a detail: several sessions run the same workflow in the same tree, and
   the pages are then identical but for this — so it belongs beside the
   workflow's name, in the strip that holds still, rather than down in the
   meta line that scrolls away under it. A link either way — an exited
   session is still openable (it resumes), and that is the first thing
   wanted here. */
function wfOwnerChip(data) {
  if (!data.scope || data.scope === "default") return null;
  const owner = el("a", "cflow-scope", `session ${data.scope}`);
  owner.href = "#/s/" + encodeURIComponent(data.scope);
  owner.title = (data.sessions || []).includes(data.scope)
    ? "attach this run's session"
    : "this run's session is not running — open it to resume";
  return owner;
}

/* One renderer, two homes: the #/wf page, and the split pane halved into the
   terminal's column. `ui` says which home this is — where its step selection
   lives, how it redraws, how it re-fetches after an action — so the pane is
   the page's material and not a lookalike that drifts. */
function renderWfInto(view, data, ui) {
  // With a run on screen the view stops scrolling as a whole: the head and
  // the button bar hold still and each column below owns its own scroll
  // (.wf-scroll-split). Idle is an ordinary page, so the class comes off.
  view.classList.toggle("wf-scroll-split", data.status !== "idle");
  if (data.status === "idle") {
    // Built once and left alone: the 2s poll must not wipe the user's
    // in-progress picker/context input.
    if (!view.querySelector(".wf-start")) renderWfIdle(view, data, ui);
    return;
  }
  // The rebuild below wipes every scroller's place in the DOM; take theirs
  // now and give it back once the fresh tree is in (the idle path above is
  // exempt — the picker is built once and then left alone).
  const keep = captureWfScrolls(view);
  view.innerHTML = "";
  const run = data.run || {};
  const wf = data.workflow || { steps: [] };

  const head = el("div", "wf-head");
  head.appendChild(el("h2", null, wf.name || run.workflow || "workflow"));
  const owner = wfOwnerChip(data);
  if (owner) head.appendChild(owner);
  head.appendChild(el(
    "span",
    `badge ${wfDotClass(run.status, run)}`,
    run.status === "waiting_approval" && run.reason === "loop_limit"
      ? "loop limit" : run.status
  ));
  // recur: true is a fact about every round, so it reads as part of the
  // run's identity, not as a detail buried in the YAML.
  if (run.recur || wf.recur) {
    const loop = el("span", "badge wf-recur",
      run.round ? `recurring · round ${run.round}` : "recurring");
    loop.title = "recur: true — each finished round requests the next; " +
      "only a human ends the loop (withdraw the request, or archive)";
    head.appendChild(loop);
  }
  if (ui.fullLink) head.appendChild(wfFullLink(data));
  view.appendChild(head);

  // The buttons at the panel's own top, not the screen's: the gate and the
  // presses that clear it hold still under the head while everything below
  // scrolls. The reminder form stays out of it (reminder: false) — it is a
  // setting, not a press, and it reads better down among the text.
  const bar = el("div", "wf-bar");
  bar.appendChild(wfActions(data, { ...ui.actions, reminder: false }));
  view.appendChild(bar);

  // Under the bar the body is two columns: the picture on the left, and
  // everything to read — description, meta, context, the reports, the
  // journal — down the right. One structure for both homes: the full page
  // and the split pane (at whatever ratio its bar sits at) both pin the
  // head and the buttons and give each column its own scroll; only a phone
  // falls back to one flow (see the 820px block).
  const side = el("div", "wf-side");
  if (wf.description) side.appendChild(el("p", "wf-desc", wf.description));

  const meta = el("div", "wf-meta");
  // The owning session is not in this line any more: it moved up to the head
  // (wfOwnerChip), which holds still while everything here scrolls away.
  meta.appendChild(el("span", null, `run ${run.run || "?"}`));
  meta.appendChild(el("span", null, `started ${(run.started_at || "?").replace("T", " ")}`));
  meta.appendChild(el("span", null, `${run.steps_completed ?? 0} steps done`));
  meta.appendChild(el("span", "mono", data.cwd));
  for (const s of data.sessions || []) {
    const link = el("a", "wf-session", `attach: ${s}`);
    link.href = "#/s/" + encodeURIComponent(s);
    meta.appendChild(link);
  }
  side.appendChild(meta);
  /* The run reads a snapshot, so this file is where it CAME from — which is
     the only way to tell two same-named workflows apart after the fact, and
     the thing to open when the run is doing something surprising. */
  if (run.source) {
    const src = el("p", "wf-source mono");
    src.appendChild(el("span", "wf-source-path", run.source));
    if (run.origin) {
      src.appendChild(el("span", "wf-source-origin", ` — ${run.origin}`));
    }
    side.appendChild(src);
  }
  if (run.context) side.appendChild(el("p", "wf-context", `context: ${run.context}`));
  for (const w of wf.warnings || []) {
    side.appendChild(el("p", "wf-warning", `⚠ ${w}`));
  }

  const pending = pendingBanner(data, ui.refresh);
  if (pending) side.appendChild(pending);

  // The reminder, out of the bar (see above). Offered exactly when the bar's
  // nudge is: while the run is still somebody's to remind.
  if (run.status !== "done" && run.status !== "aborted") {
    side.appendChild(reminderControl(data, (ui.actions || {}).after, ui.host));
  }

  // drop a stale selection if the workflow changed under us
  if (
    ui.getStep() && ui.getStep() !== "end" &&
    !(wf.steps || []).some((s) => s.id === ui.getStep())
  ) {
    ui.putStep(null);
  }
  const sel = ui.getStep();

  const cols = el("div", "wf-cols");
  const dia = el("div", "wf-diagram");
  dia.innerHTML = wfDiagramSvg(wf, run, sel);
  const finished = run.status === "done" || run.status === "aborted";
  dia.querySelectorAll("g.wfd-node[data-step]").forEach((g) => {
    g.addEventListener("click", () => {
      const step = g.dataset.step;
      ui.select(ui.getStep() === step ? null : step); // click again = clear
    });
  });
  cols.appendChild(dia);
  dia.appendChild(el(
    "p", "wf-note",
    "click a step to inspect its reports (click again to clear)"
  ));
  const pacedNote = wfPacedNote(wf, run);
  if (pacedNote) dia.appendChild(pacedNote);

  // The same steps, on a clock. Under the graph rather than beside it because
  // the two are one reading: what the run may do, then what it did and when.
  const timeline = wfTimelinePanel(data, ui);
  if (timeline) dia.appendChild(timeline);

  const forceBtn = el(
    "button", "wf-btn force",
    !sel
      ? "Force set state (select a step first)"
      : sel === "end"
        ? "Force-finish this run"
        : `Force run to '${sel}'`
  );
  forceBtn.disabled = !sel;
  if (sel) {
    const step = sel;
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

  side.appendChild(wfReports(data, ui));
  cols.appendChild(side);
  view.appendChild(cols);

  // Into the text column, not under the columns: outside them it would sit
  // below two scrollers that never yield the height to reach it.
  const journal = document.createElement("details");
  journal.className = "wf-journal";
  journal.appendChild(el("summary", null, `journal (${(data.journal || []).length} events)`));
  for (const e of (data.journal || []).slice().reverse()) {
    const line =
      `${(e.at || "").replace("T", " ")}  ${e.event || ""}` +
      `${e.step ? "  " + e.step : ""}${e.option ? "  -> " + e.option : ""}`;
    journal.appendChild(el("div", "wf-journal-line mono", line));
  }
  side.appendChild(journal);

  restoreWfScrolls(view, keep);
}

/* The way from the half to the whole: the split pane renders the run page's
   material, and this is the button to the page itself — same run, full
   height, and nothing else on screen to share it with. */
function wfFullLink(data) {
  const btn = el("button", "wf-btn wf-full", "Open the run page");
  btn.title = "this run as its own page";
  btn.addEventListener("click", () => {
    location.hash =
      "#/wf/" + encodeURIComponent(`${data.scope || "default"}|${data.cwd}`);
  });
  return btn;
}

/* The human controls for a run. Shared with the session panel's fold, which
   asks for two things the page does not: `after`, because refreshWf only
   refreshes the run page, and no archive — aborting a run is not something to
   offer in a corner of a terminal, and the page it points at has it. */
function wfActions(data, opts = {}) {
  const run = data.run || {};
  const after = opts.after;
  const box = el("div", "wf-actions");
  // Three homes inside the one box, so prose and presses stop interleaving:
  // everything to read (msgs), the presses that answer the gate (main), and
  // the run-management presses (tools). The bar lays main and tools out on
  // one line, decisions left and management right; the fold stacks them.
  // An empty home takes no row (CSS :empty).
  const msgs = el("div", "wf-act-msgs");
  const main = el("div", "wf-act-main");
  const tools = el("div", "wf-act-tools");
  box.appendChild(msgs);
  box.appendChild(main);
  box.appendChild(tools);
  // Leads, because it changes how everything below it reads: a run whose
  // session is not running is not being worked on, whatever position it
  // recorded before it stopped. Said after "agent is working on 'survey'",
  // it reads as a footnote to the opposite claim.
  const homeless = !(data.sessions || []).length;
  const scoped = data.scope && data.scope !== "default";
  if (homeless && run.status !== "done" && run.status !== "aborted") {
    msgs.appendChild(el(
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
    msgs.appendChild(el("p", "wf-note", `asked: ${askWho(run.ask)}`));
    for (const s of run.ask.skipped || []) {
      msgs.appendChild(el("p", "wf-note", `skipped ${s.candidate} — ${s.reason}`));
    }
    if (run.ask.deadline) {
      msgs.appendChild(el("p", "wf-note", `moves on after ${run.ask.deadline}`));
    }
    if (run.ask.undelivered) {
      msgs.appendChild(el("p", "wf-warning",
        `recorded, but not announced: ${run.ask.undelivered}`));
    }
  }
  if (answerFellToUs(run)) {
    // The question reached nobody, so there is no peer to wait for and
    // "you do not have to do anything" would be a lie that stops the run
    // for good. Read it as the gate it actually is: say who it is with
    // (nobody), and offer the press that clears it. The daemon accepts
    // that press — engine.approve() handles an ask that reached nobody.
    msgs.appendChild(el("p", "wf-gate", run.ask ? run.ask.prompt : "waiting for a decision"));
    msgs.appendChild(el("p", "wf-warning",
      "this was put to nobody — no agent is going to answer it. Only you " +
      "can let the run continue."));
    const btn = el("button", "wf-btn approve", "Approve gate");
    btn.addEventListener("click", () => {
      if (run.ask && run.ask.kind === "branch") {
        // Same reason as below: a branch needs an option, not an approval.
        alert("Use 'claunch cflow select <option>' to pick the branch.");
        return;
      }
      if (confirm(
        `Answer '${run.step_id}' yourself?\n\nIt was put to nobody, so ` +
        "nothing else will."
      )) {
        cflowAction("/api/cflow/approve", { cwd: data.cwd, scope: data.scope }, after);
      }
    });
    main.appendChild(btn);
  } else if (run.status === "waiting_answer") {
    msgs.appendChild(el("p", "wf-gate", run.ask ? run.ask.prompt : "waiting for a decision"));
    msgs.appendChild(el("p", "wf-note",
      "this is with another agent; you do not have to do anything. Take it " +
      "over only if it is stuck — your answer lands over theirs, and they " +
      "are told the question is closed."));
    const branch = run.ask && run.ask.kind === "branch";
    // A branch needs an option, not an approval. This used to say so in an
    // alert and send the reader to the CLI — a dead end on the one screen
    // that had every part of the question already in hand. The takeover is
    // the same press as any other selection; only the confirm differs,
    // because this one is taken away from somebody.
    const opts = branch ? (run.ask.options || []) : [null];
    for (const o of opts) {
      const btn = el("button", "wf-btn" + (o ? " option" : ""),
        o ? o.name : "Decide it myself");
      if (o) btn.title = o.description || "";
      btn.addEventListener("click", () => {
        if (!confirm(
          `Take '${run.step_id}' away from ${askWho(run.ask)} and ` +
          (o ? `answer '${o.name}'` : "decide it") + " yourself?"
        )) return;
        if (o) {
          cflowAction("/api/cflow/select", {
            cwd: data.cwd, scope: data.scope, option: o.name,
          }, after);
        } else {
          cflowAction("/api/cflow/approve", { cwd: data.cwd, scope: data.scope }, after);
        }
      });
      main.appendChild(btn);
    }
  } else if (run.status === "waiting_approval") {
    const isLoop = run.reason === "loop_limit";
    if (run.reason === "declined" && run.declined) {
      const dec = el("div", "wf-warning md");
      dec.appendChild(el("div", null, `${run.declined.by} declined:`));
      mdInto(dec, run.declined.reason || "no reason given");
      msgs.appendChild(dec);
    }
    msgs.appendChild(el("p", "wf-gate", run.gate || "waiting for approval"));
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
    main.appendChild(btn);
  } else if (run.status === "waiting_checklist") {
    const cl = run.checklist || {};
    msgs.appendChild(el(
      "p", "wf-gate",
      cl.prompt || `every condition must be true to leave '${run.step_id}'`
    ));
    const box = el("div", "wf-checklist");
    for (const item of cl.items || []) {
      const row = el("div", `wf-check ${checklistClass(item.ok)}`);
      row.appendChild(el("span", "wf-check-mark", checklistMark(item.ok)));
      const body = el("div", "wf-check-body");
      body.appendChild(el("div", "wf-check-what", item.describe || item.id));
      const detail =
        item.exit_code === null || item.exit_code === undefined
          ? (item.measured_at ? "could not measure" : "not measured yet")
          : `exit ${item.exit_code}`;
      body.appendChild(el(
        "div", "wf-check-meta",
        `${item.id} \u2014 ${detail}` +
        (item.measured_at ? ` \u2014 ${item.measured_at}` : "")
      ));
      if (item.check) body.title = item.check;
      row.appendChild(body);
      box.appendChild(row);
    }
    msgs.appendChild(box);
    // No button. This gate is not a person's to grant -- that is what makes
    // it different from every other stop on this page -- so the page says who
    // does move it and what is still holding it, and offers the override that
    // does exist (a goto, which is journalled as one).
    msgs.appendChild(el(
      "p", "wf-note",
      cl.all_true && !cl.report_filed
        ? `every item is true; the move to '${cl.then}' is waiting on this ` +
          `step's report`
        : cl.all_true
          ? `every item is true and the report is filed \u2014 the daemon ` +
            `moves this run to '${cl.then}'`
          : `the daemon re-measures every ${Math.round(cl.poll || 60)}s and ` +
            `moves the run to '${cl.then}' when all ${cl.total} are true`
    ));
  } else if (run.status === "waiting_selection" || run.status === "select") {
    msgs.appendChild(el("p", "wf-gate", run.prompt || "decision point"));
    if (run.proposal) {
      const prop = el("div", "wf-proposal md");
      prop.appendChild(el(
        "div", "wf-proposal-head", `agent proposes: ${run.proposal.option}`));
      mdInto(prop, run.proposal.reason || "");
      msgs.appendChild(prop);
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
        main.appendChild(btn);
      }
      // Beside the presses it explains, not among the prose above them.
      main.appendChild(el("p", "wf-note",
        "confirming unblocks the agent; its managed session is nudged automatically"));
    } else {
      msgs.appendChild(el("p", "wf-note", "the agent decides this branch on its own"));
    }
  } else if (run.status === "waiting_goto") {
    /* The agent has hit something the graph declares no route for and is
       asking to be moved. Two presses, because the two answers are not
       symmetric: granting moves the run and reopens work, refusing costs
       nothing but leaves the agent on a route it says cannot carry this. The
       diagram above is the third answer — force the run to some THIRD step —
       and it stays where it already is rather than being duplicated here. */
    const gr = run.goto_request || {};
    const gate = el("div", "wf-gate md");
    gate.appendChild(el(
      "div", null,
      `the agent asks to move from '${gr.from || run.step_id}' to '${gr.step}'`
    ));
    mdInto(gate, gr.reason || "no reason given");
    msgs.appendChild(gate);
    msgs.appendChild(el("p", "wf-note",
      "the run does not advance until this is answered. Refusing leaves the " +
      "position alone and tells the agent to continue on the declared route; " +
      "to send it to a third step instead, pick that step on the diagram and " +
      "force it — that answers the request too."));
    const grant = el("button", "wf-btn approve", `Move to '${gr.step}'`);
    grant.addEventListener("click", () => {
      if (confirm(
        `Move the run to '${gr.step}', as the agent asked?` +
        (gr.step === "end" ? " (this finishes the run)" : "")
      )) {
        cflowAction("/api/cflow/goto/resolve", {
          cwd: data.cwd, scope: data.scope, decision: "approve",
        }, after);
      }
    });
    main.appendChild(grant);
    const refuse = el("button", "wf-btn", "Refuse");
    refuse.title = "the run stays where it is; the agent is told and continues";
    refuse.addEventListener("click", () => {
      const why = prompt(
        "Refuse the move? A reason is optional but is what the agent reads:",
        ""
      );
      if (why === null) return; // cancelled the dialog, not the refusal
      cflowAction("/api/cflow/goto/resolve", {
        cwd: data.cwd, scope: data.scope, decision: "deny", reason: why,
      }, after);
    });
    main.appendChild(refuse);
  } else if (run.status === "waiting_window") {
    msgs.appendChild(el("p", "wf-gate", run.prompt || "decision point"));
    msgs.appendChild(el(
      "p", "wf-note",
      `agent chose '${run.option}' — held until ${fmtOpensAt(run.opens_at)} ` +
      `(this option runs at most every ${run.interval}s); the daemon releases ` +
      `it then and nudges the session`
    ));
    const btn = el("button", "wf-btn option", `take '${run.option}' now`);
    btn.title = "confirm the held choice without waiting for its window (journaled as an override)";
    btn.addEventListener("click", () => {
      if (confirm(`Take '${run.option}' now, without waiting for its window?`)) {
        cflowAction("/api/cflow/select", {
          cwd: data.cwd, scope: data.scope, option: run.option,
        }, after);
      }
    });
    main.appendChild(btn);
  } else if (run.status === "done" || run.status === "aborted") {
    msgs.appendChild(el("p", "wf-note", `workflow ${run.status}`));
  } else {
    msgs.appendChild(el(
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
    tools.appendChild(btn);
    // The run page and the pane pull the reminder out (reminder: false) and
    // seat it in their text column; the fold keeps it here, in the one box.
    if (opts.reminder !== false) {
      box.appendChild(reminderControl(data, after, opts.host));
    }
  }

  // Skipping a round. A recurring run's rounds only count upward — a normal
  // finish files the request for round N+1 by itself; this is the forced
  // version mid-round: archive the run as it stands, request the same
  // workflow (same context) at round N+1, nudge the session. Offered in the
  // session fold too — unlike archive it does not end the loop, it moves it
  // along. Only while the round is live: a finished one has already filed
  // its own next-round request, and there is nothing left to cut short.
  const live = run.status !== "done" && run.status !== "aborted";
  if (live && (run.recur || (data.workflow || {}).recur)) {
    const r = Number(run.round) || 1;
    const skp = el("button", "wf-btn skip", `Skip round (${r} → ${r + 1})`);
    skp.title = "archive this round as it stands and request the same " +
      `workflow again as round ${r + 1} — the loop moves on early`;
    skp.addEventListener("click", () => {
      const q =
        `Skip round ${r} of the '${run.workflow || "workflow"}' loop?\n\n` +
        "The round is cut short: the current run is archived and the same " +
        `workflow (same context) is requested again as round ${r + 1}. ` +
        "The session is nudged to start it.";
      if (confirm(q)) {
        cflowAction("/api/cflow/skip", { cwd: data.cwd, scope: data.scope }, after);
      }
    });
    tools.appendChild(skp);
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
  tools.appendChild(arch);
  return box;
}

/* This run's reminder: whether the daemon re-types the current step's
   instructions into the driving session while the run does not move, and how
   often. What is stored here is the RUN's override; runs without one follow
   the machine defaults edited on the Workflows page. Rebuilt only when the
   slot changes (the sess-send-box pattern): the 2s poll must not wipe a
   half-typed interval — a save clears the cache so the server's answer is
   what the next poll draws.

   Cached per HOST, not one node for everybody: the session panel's fold and
   the split pane can show the same slot at the same time, and a single cached
   node would be *moved* between the two by whichever poll ran last —
   appendChild takes the live node with it. */
let wfReminderBoxes = {};
function reminderControl(data, after, host = "page") {
  const run = data.run || {};
  const key = `${data.scope}|${data.cwd}`;
  const kept = wfReminderBoxes[host];
  if (kept && kept.dataset.slot === key) return kept;
  const box = el("div", "wf-reminder");
  box.dataset.slot = key;

  const override = run.reminder || null;
  const defs = data.reminder_defaults || {};
  const effOn = override && "enabled" in override
    ? !!override.enabled : !!defs.enabled;
  const effIv = override && "interval" in override
    ? override.interval : (defs.interval || 600);

  const head = el("label", "wf-reminder-head");
  const on = document.createElement("input");
  on.type = "checkbox";
  on.checked = effOn;
  head.appendChild(on);
  head.appendChild(el("span", null, "remind the session of its step"));
  head.title = "while the run sits on the same step, the daemon re-types " +
    "that step's instructions into the session at this interval";
  box.appendChild(head);

  const row = el("div", "wf-reminder-row");
  row.appendChild(el("span", "wf-note", "every"));
  const iv = document.createElement("input");
  iv.type = "number";
  iv.min = "30";
  iv.step = "10";
  iv.className = "pol-num";
  iv.value = String(Math.round(effIv));
  row.appendChild(iv);
  row.appendChild(el("span", "wf-note", "s without progress"));
  const save = el("button", "wf-btn", "Set for this run");
  save.title = "stored with this run only; the machine defaults stay untouched";
  save.addEventListener("click", () => {
    delete wfReminderBoxes[host];
    cflowAction("/api/cflow/reminder", {
      cwd: data.cwd, scope: data.scope,
      enabled: on.checked, interval: +iv.value,
    }, after);
  });
  row.appendChild(save);
  const clear = el("button", "wf-btn clear", "Use defaults");
  clear.disabled = !override;
  clear.title = "drop this run's override and follow the machine defaults again";
  clear.addEventListener("click", () => {
    delete wfReminderBoxes[host];
    cflowAction("/api/cflow/reminder", {
      cwd: data.cwd, scope: data.scope, clear: true,
    }, after);
  });
  row.appendChild(clear);
  box.appendChild(row);

  box.appendChild(el(
    "p", "wf-note",
    override
      ? "this run overrides the machine defaults"
      : `following the machine defaults (${defs.enabled ? "on" : "off"}, ` +
        `${Math.round(defs.interval || 0)}s) — set on the Workflows page`
  ));
  wfReminderBoxes[host] = box;
  return box;
}

/* Idle (cwd, scope): offer to start a new run. */
async function renderWfIdle(view, data, ui) {
  view.innerHTML = "";
  // Which slot is empty, not just which directory: the same directory holds
  // one slot per session, and all but this one may well be busy.
  view.appendChild(el(
    "p", "wf-note",
    data.scope && data.scope !== "default"
      ? `no active cflow run for session '${data.scope}' in ${data.cwd}`
      : `no active cflow run in ${data.cwd}`
  ));
  if (ui.fullLink) view.appendChild(wfFullLink(data));
  const pending = pendingBanner(data, ui.refresh);
  if (pending) view.appendChild(pending);
  const box = el("div", "wf-start");
  view.appendChild(box); // present immediately so the poll doesn't rebuild
  await buildStartPanel(box, {
    cwd: data.cwd,
    scope: data.scope || "default",
    sessions: data.sessions || [],
    stillHere: () => ui.stillHere(data.cwd),
    after: () => { ui.refresh(); refreshCflow(); },
  });
}

/* A human's pending start request, with the way to take it back.
   Rendered wherever a slot is shown, because between the request and the
   agent acting on it this is the only sign that anything is coming. */
function pendingBanner(data, after) {
  const req = data.pending_start || (data.run || {}).pending_start;
  if (!req) return null;
  // A recurring run files its own next round through this same channel, and
  // that is a different thing to read: nobody "asked", the loop is looping —
  // and withdrawing the request is the loop's off switch.
  const recur = req.by === "recur";
  const box = el("div", "wf-pending");
  box.appendChild(el(
    "p", "wf-pending-head",
    recur
      ? `next round requested: ${req.name || req.workflow}` +
        (req.round ? ` (round ${req.round})` : "")
      : `start requested: ${req.name || req.workflow}`
  ));
  if (req.context) box.appendChild(el("p", "wf-pending-ctx", req.context));
  box.appendChild(el(
    "p", "wf-note",
    recur
      ? `this workflow recurs: the round that finished at ` +
        `${(req.at || "?").replace("T", " ")} filed this itself. The agent ` +
        `starts the next round on its next cflow 'status' call; withdrawing ` +
        `the request is how the loop is stopped.`
      : `asked by ${req.by || "?"} at ${(req.at || "").replace("T", " ")} — the ` +
        `session's agent starts it itself, so it knows what it is running. It ` +
        `picks the request up on its next cflow 'status' call.`
  ));
  const cancel = el("button", "wf-btn clear", recur ? "Stop the loop" : "Withdraw request");
  cancel.addEventListener("click", async () => {
    const q = recur
      ? `Stop the '${req.name || req.workflow}' loop?\n\nThe next round's ` +
        `request is withdrawn; the finished run stays as it is.`
      : `Withdraw the pending start of '${req.name || req.workflow}'?`;
    if (!confirm(q)) return;
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
/* One line per workflow in the picker. An option cannot wrap, and the native
   popup sizes itself to the widest option: a paragraph-length description
   drags the whole dropdown past the viewport edge, where the browser pins and
   clips it. So the list gets one clipped line per workflow; the full
   description renders under the select for whichever is picked. */
function wfOptionLabel(w) {
  if (!w.description) return w.name;
  const d = w.description.replace(/\s+/g, " ").trim();
  const clipped = d.length > 48 ? d.slice(0, 48).trimEnd() + "…" : d;
  return `${w.name} — ${clipped}`;
}

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
    opt.textContent = wfOptionLabel(w);
    opt.title = w.path;
    sel.appendChild(opt);
  }
  const desc = el("p", "wf-start-desc");
  /* A name is not a file: the same name can be declared in the project and in
     the global layer, and picking from a list of names hides which one runs.
     The path goes under the select rather than into the option text — an
     option cannot wrap, and these are absolute paths. */
  const source = el("p", "wf-source mono");
  const showSource = () => {
    const w = flows.find((f) => f.name === sel.value);
    source.replaceChildren();
    desc.textContent = w && w.description ? w.description : "";
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
    if (w.recur) {
      source.appendChild(el(
        "span", "wf-source-origin",
        " · recurs: each finished round requests the next"
      ));
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
  box.appendChild(desc);
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

function wfReports(data, ui) {
  const sel = ui.getStep();
  const box = el("div", "wf-reports");
  const head = el("div", "wf-reports-head");
  head.appendChild(el("h3", null,
    sel ? `Step reports — ${sel}` : "Step reports"));
  if (sel) {
    const clear = el("button", "wf-btn clear", "Show all");
    clear.title = "clear the step selection and expand every report";
    clear.addEventListener("click", () => ui.select(null));
    head.appendChild(clear);
  }
  box.appendChild(head);

  if (sel && sel !== "end") {
    const step = ((data.workflow || {}).steps || [])
      .find((s) => s.id === sel);
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
  if (sel && reports.length &&
      !reports.some((r) => r.step === sel)) {
    box.appendChild(el("p", "wf-note", `no reports for '${sel}' yet`));
  }
  for (const r of reports) {
    const expanded = !sel || r.step === sel;
    const card = el("div", expanded ? "wf-report" : "wf-report folded");
    const rhead = el("div", "wf-report-head");
    rhead.appendChild(el("span", "wf-report-step", r.visit > 1 ? `${r.step} ×${r.visit}` : r.step));
    rhead.appendChild(el("span", "wf-report-at", (r.at || "").replace("T", " ")));
    card.appendChild(rhead);
    if (expanded) {
      card.appendChild(mdInto(el("div", "wf-report-summary md"), r.summary || ""));
      if (r.details) {
        card.appendChild(mdInto(el("div", "wf-report-details md"), r.details));
      }
    } else {
      card.title = `show reports for '${r.step}'`;
      card.addEventListener("click", () => ui.select(r.step));
    }
    box.appendChild(card);
  }
  return box;
}

/* SVG graph: layered rows from start (wfStepOrder), forward edges on the
   right rail, back edges (the cycle arcs) on the left rail, select options
   as edge labels. */
function escXml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/* The steps top to bottom, one row per layer: a step sits on the row below
   its DEEPEST predecessor, so every edge runs downward and the picture
   reads as a flow. A breadth-first walk cannot promise that — BFS rows a
   step by its SHORTEST path from `start`, and a join reachable only by a
   long chain lands above the branch that waits for it (improv-worker's
   wrapup sat above its own landed, and the final chain ran backward up the
   rail). The loops are the exception, and they are chosen, not accidental:
   a DFS over the graph marks the edge that closes each cycle (its target is
   still on the walk's stack); those edges alone may point up, and they
   become the left-rail arcs. Steps `start` cannot reach are walked as
   islands of their own and laid below the reachable ones, not interleaved.
   The two pictures in this column share it deliberately — a lane that is
   not on the same row as its box is a lane the reader has to hunt for. */
function wfStepOrder(wf) {
  const steps = (wf && wf.steps) || [];
  const byId = {};
  for (const s of steps) byId[s.id] = s;
  const outs = (s) => (s.select ? s.select.options.map((o) => o.next) : [s.next]);

  /* Walk every step (one DFS per island, siblings in option order) and mark
     the cycle-closing edge of each loop: u->v with v still on the stack.
     Remove those edges and the graph is a DAG, which is what makes the
     longest-path pass below terminate. */
  const pre = [], pos = new Map(), color = new Map(), back = new Set();
  const comp = new Map();
  let comps = 0;
  const stack = [];
  for (const root of [wf && wf.start, ...steps.map((s) => s.id)]) {
    if (!root || !byId[root] || color.has(root)) continue;
    const island = comps++;
    stack.push([root, 0]);
    while (stack.length) {
      const top = stack[stack.length - 1];
      const u = top[0];
      if (!color.has(u)) {
        color.set(u, 1);
        comp.set(u, island);
        pos.set(u, pre.length);
        pre.push(u);
      }
      const ways = outs(byId[u]);
      if (top[1] < ways.length) {
        const t = ways[top[1]++];
        if (!t || !byId[t]) continue;   // a dangling `next` costs an edge, not the picture
        if (!color.has(t)) stack.push([t, 0]);
        else if (color.get(t) === 1) back.add(`${u}>${t}`);
      } else {
        color.set(u, 2);
        stack.pop();
      }
    }
  }

  /* Longest path across the DAG: v is one row below the deepest step that
     points at it. Each pass relaxes paths one edge longer, so the pass
     count is bounded by the number of steps. */
  const level = new Map(pre.map((id) => [id, 0]));
  for (let pass = 0; pass <= pre.length; pass++) {
    let moved = false;
    for (const u of pre) {
      for (const t of outs(byId[u])) {
        if (!t || !byId[t] || back.has(`${u}>${t}`)) continue;
        if (level.get(t) < level.get(u) + 1) {
          level.set(t, level.get(u) + 1);
          moved = true;
        }
      }
    }
    if (!moved) break;
  }

  /* Each island below the last: an orphaned step is a mistake worth seeing
     at the tail, not worth hiding among the flow. */
  const offsets = new Array(comps).fill(0);
  let floor = -1;
  for (let c = 0; c < comps; c++) {
    offsets[c] = floor + 1;
    for (let i = 0; i < pre.length; i++) {
      if (comp.get(pre[i]) !== c) continue;
      floor = Math.max(floor, offsets[c] + level.get(pre[i]));
    }
  }
  const final = (id) => offsets[comp.get(id)] + level.get(id);
  return pre.slice().sort((a, b) =>
    (final(a) - final(b)) || (pos.get(a) - pos.get(b)));
}

/* A cadence in the largest unit that still reads whole: 300 -> "5m",
   90 -> "90s", 5400 -> "1.5h". The diagram has about ten pixels of type to
   say it in, so "300 seconds" is not an option and neither is "00:05:00". */
function fmtPace(sec) {
  const n = Number(sec);
  if (!Number.isFinite(n) || n <= 0) return "";
  if (n < 60) return `${+n.toFixed(2)}s`;
  if (n < 3600) return `${+(n / 60).toFixed(2)}m`;
  return `${+(n / 3600).toFixed(2)}h`;
}

/* How wide a string draws at a given font size. SVG has no layout to ask, so
   this estimates: a Hangul or CJK glyph takes a full em, the rest of what
   step titles are written in takes about 0.55. The estimate only has to be
   safe in one direction — it must not claim a string fits when it does not —
   and 0.55em is the wide end of this face's Latin advances. */
function wfdTextW(s, px) {
  let w = 0;
  for (const ch of String(s)) {
    const c = ch.codePointAt(0);
    const wide = (c >= 0x1100 && c <= 0x115f)
      || (c >= 0x2e80 && c <= 0x303e) || (c >= 0x3041 && c <= 0x33ff)
      || (c >= 0x3400 && c <= 0x4dbf) || (c >= 0x4e00 && c <= 0x9fff)
      || (c >= 0xa000 && c <= 0xa4cf) || (c >= 0xac00 && c <= 0xd7a3)
      || (c >= 0xf900 && c <= 0xfaff) || (c >= 0xfe30 && c <= 0xfe6f)
      || (c >= 0xff00 && c <= 0xff60) || (c >= 0xffe0 && c <= 0xffe6);
    w += wide ? px : px * 0.55;
  }
  return w;
}

/* Cut a string to a pixel budget, ellipsis included in the budget. A title
   that overruns its box does not merely look wrong — it runs out over the
   right rail and lands on the edge labels drawn there (improv-worker's
   `landing` title crossed its own `request` label). The full text stays
   reachable: the caller hangs it on the node as a <title>. */
function wfdFit(s, px, max) {
  const str = String(s);
  if (wfdTextW(str, px) <= max) return str;
  const budget = max - wfdTextW("…", px);
  let out = "", w = 0;
  for (const ch of str) {
    const cw = wfdTextW(ch, px);
    if (w + cw > budget) break;
    out += ch; w += cw;
  }
  return `${out.replace(/\s+$/, "")}…`;
}

function wfDiagramSvg(wf, run, selected) {
  const steps = wf.steps || [];
  const byId = {};
  for (const s of steps) byId[s.id] = s;
  // Each way out carries the option it came from, not just its name: an
  // option may be PACED (`interval`), and pacing is a property of the branch
  // — this edge is passable at most once per interval — so the drawing has
  // to reach the option itself to draw it.
  const outsOf = (s) =>
    s.select
      ? s.select.options.map((o) => [o.next, o.name, o])
      : [[s.next, null, null]];

  // Shared with the timing diagram under this graph: the two pictures have
  // to put a step on the same row, or the lane is one the reader must hunt
  // for rather than glance down to.
  const order = wfStepOrder(wf);

  const rows = {};
  order.forEach((id, i) => { rows[id] = i; });

  const edges = [];
  let hasEnd = false;
  for (const s of steps) {
    outsOf(s).forEach(([t, label, opt], i) => {
      const pace = (opt && opt.interval) || null;
      if (t) edges.push({ from: s.id, to: t, label, i, pace });
      else { hasEnd = true; edges.push({ from: s.id, to: "end", label, i, pace }); }
    });
  }

  /* The one branch the run is parked on, if there is one. A paced option
     chosen inside its interval is HELD rather than taken: the engine records
     the choice, reports `waiting_window`, and the daemon's clock releases it.
     That is a state of one branch of one step — not of the step, which is why
     it is drawn on the edge — and `run.option` names which. */
  const held = run && run.status === "waiting_window"
    ? { step: run.step_id, option: run.option }
    : null;
  const isHeld = (e) => !!held && held.step === e.from && held.option === e.label;

  const NW = 210, NH = 44, ROWH = 82, W = 480;
  const NX = (W - NW) / 2;
  const endRow = order.length;
  const H = (endRow + (hasEnd ? 1 : 0)) * ROWH + 10;
  const rowOf = (id) => (id === "end" ? endRow : rows[id]);
  const yTop = (id) => 8 + rowOf(id) * ROWH;

  /* Two options that leave the same step for the same step are ONE route on
     the page. Drawing one arc per option drew that route twice, and both
     copies put their label on the same coordinate — improv-worker's `rebase`
     and `remeasure` (both await-landing -> rebase) came out at x=127,y=649
     on top of each other, and the loop cost two upward arcs where the reader
     only ever had one way back. A route carries its options instead, so the
     count of upward arcs is the count of loops: two for improv-worker, which
     is the floor (each directed cycle must send one arc up, and the rest of
     this graph flows down). */
  const routes = [];
  const byPair = new Map();
  for (const e of edges) {
    const key = `${e.from}>${e.to}`;
    let r = byPair.get(key);
    if (!r) {
      r = { from: e.from, to: e.to, i: e.i, opts: [] };
      byPair.set(key, r);
      routes.push(r);
    }
    r.opts.push(e);
  }

  /* Gutter columns by row span, not by option index. The index is a number
     about one step's menu; whether two arcs collide is a fact about the rows
     they cross. improv-worker had both of its left-rail arcs on index 1 —
     `changes` (rows 1..7) and the loop back to rebase (rows 6..9) — so they
     ran down the same column and crossed. Shortest span first, so a long arc
     nests outside a short one instead of cutting through it; arcs that share
     no row reuse a column and the drawing stays as narrow as it was. */
  const laneOf = new Map();
  const lanes = (rs) => {
    const taken = [];
    for (const r of rs.slice().sort((a, b) => {
      const sa = Math.abs(rowOf(a.to) - rowOf(a.from));
      const sb = Math.abs(rowOf(b.to) - rowOf(b.from));
      return (sa - sb) || (rowOf(a.from) - rowOf(b.from));
    })) {
      const lo = Math.min(rowOf(r.from), rowOf(r.to));
      const hi = Math.max(rowOf(r.from), rowOf(r.to));
      let lane = 0;
      while (taken[lane] && taken[lane].some(([a, b]) => lo <= b && a <= hi)) lane++;
      (taken[lane] = taken[lane] || []).push([lo, hi]);
      laneOf.set(r, lane);
    }
  };
  lanes(routes.filter((r) => rowOf(r.to) > rowOf(r.from) + 1));   // right rail
  lanes(routes.filter((r) => rowOf(r.to) <= rowOf(r.from)));      // left rail

  /* The centre column fans the same way, and for the same reason: a step's
     one straight way out belongs on the centre line. Counting the option's
     place in the MENU pushed it off — improv-worker's `landing` sends its
     first option down the right rail and its second straight down, so the
     only straight arrow it draws was offset as though it had a twin. Count
     the straight ways out instead. */
  const fanOf = new Map();
  const straight = new Map();
  for (const r of routes) {
    if (rowOf(r.to) !== rowOf(r.from) + 1) continue;
    const n = straight.get(r.from) || 0;
    fanOf.set(r, n);
    straight.set(r.from, n + 1);
  }

  /* Where paths come back together, and where they split. A fork is already
     visible — the arcs leave the box in front of the reader — but a MERGE is
     not: an arc arriving from four rows up looks exactly the same whether it
     is the only way in or the second of two, so finding them meant tracing
     every arc to its far end. improv-worker has two (`rebase`, from landing
     and landing-review; `wrapup`, from landing-review and landed) and neither
     was drawn as anything.

     Only arrivals from ABOVE count. A loop back is a retry, not a
     convergence: it is one path returning to itself, it already reads as one
     on the left rail, and counting it would call every retry target a merge.

     Forks count DESTINATIONS, not options — which is the fact a reader cannot
     get from `select:agent`. improv-worker's await-landing offers three
     options that lead to two places, because `rebase` and `remeasure` both go
     back to rebase. Three choices, two outcomes. */
  const mergeIn = new Map();
  const forkOut = new Map();
  for (const r of routes) {
    if (rowOf(r.to) > rowOf(r.from)) mergeIn.set(r.to, (mergeIn.get(r.to) || 0) + 1);
    forkOut.set(r.from, (forkOut.get(r.from) || 0) + 1);
  }
  const isMerge = (id) => (mergeIn.get(id) || 0) > 1;

  const parts = [];
  // width/height attrs pin the drawing at its natural size (one SVG unit =
  // one CSS pixel): the column growing must not blow the graph up with it.
  // The stylesheet only ever shrinks it (max-width) on columns narrower
  // than the drawing.
  parts.push(
    `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" ` +
    `xmlns="http://www.w3.org/2000/svg" class="wfd">`
  );
  // The merge head is the same shape and the same ink, just bigger. Size,
  // not colour: every colour in this picture already means a run state
  // (amber a second visit, blue the current step, green a visited one), and
  // a merge is a fact about the workflow that is true before any run exists.
  parts.push(
    '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" ' +
    'markerWidth="7" markerHeight="7" orient="auto-start-reverse">' +
    '<path d="M 0 0 L 10 5 L 0 10 z" fill="#4d5566"/></marker>' +
    '<marker id="arrow-merge" viewBox="0 0 10 10" refX="9" refY="5" ' +
    'markerWidth="11" markerHeight="11" orient="auto-start-reverse">' +
    '<path d="M 0 0 L 10 5 L 0 10 z" fill="#4d5566"/></marker></defs>'
  );

  /* Two labels on one coordinate are one unreadable label. Every line the
     loop below places goes through here, which pushes a line down until it
     clears whatever already stands in that column. */
  const placed = [];
  const freeY = (x, y, anchor) => {
    let out = y;
    for (let n = 0; n < placed.length + 1; n++) {
      const hit = placed.some((p) =>
        p.anchor === anchor && Math.abs(p.x - x) < 40 && Math.abs(p.y - out) < 10);
      if (!hit) break;
      out += 11;
    }
    placed.push({ x, y: out, anchor });
    return out;
  };

  for (const e of routes) {
    const r1 = rowOf(e.from), r2 = rowOf(e.to);
    const lane = laneOf.get(e) || 0;
    let d, lx, ly, anchor = "start";
    if (r2 === r1 + 1) {
      const f = fanOf.get(e) || 0;
      const x = W / 2 + (f ? (f % 2 ? -1 : 1) * 18 * Math.ceil(f / 2) : 0);
      const y1 = yTop(e.from) + NH, y2 = yTop(e.to) - 2;
      d = `M ${x} ${y1} L ${x} ${y2}`;
      lx = x + 7; ly = (y1 + y2) / 2 + 4;
    } else if (r2 > r1) {
      const y1 = yTop(e.from) + NH / 2, y2 = yTop(e.to) + 8;
      const b = NX + NW + 30 + 16 * lane;
      d = `M ${NX + NW} ${y1} C ${b} ${y1}, ${b} ${y2}, ${NX + NW + 2} ${y2}`;
      lx = NX + NW + 8; ly = y1 - 8;
    } else {
      const y1 = yTop(e.from) + NH / 2, y2 = yTop(e.to) + NH / 2;
      const b = NX - 30 - 16 * lane;
      d = `M ${NX} ${y1} C ${b} ${y1}, ${b} ${y2}, ${NX - 2} ${y2}`;
      // At the end the arc LEAVES from, not at its middle: the middle of a
      // long arc is beside rows that have nothing to do with the branch, and
      // two arcs of different length can share a middle. The step a branch
      // departs from is unique to it.
      lx = NX - 8; ly = y1 - 8; anchor = "end";
    }
    const hold = e.opts.some(isHeld);
    const pace = e.opts.some((o) => o.pace);
    // Dashed for as long as the pacing exists, not only while it bites: that
    // this branch runs at most once per interval is a fact about the
    // workflow, and a reader planning a run needs it before anything is held.
    const ecls = `wfd-edge${pace ? " paced" : ""}${hold ? " held" : ""}`;
    const head = isMerge(e.to) && rowOf(e.to) > rowOf(e.from) ? "arrow-merge" : "arrow";
    parts.push(`<path class="${ecls}" d="${d}" marker-end="url(#${head})"/>`);
    for (const o of e.opts) {
      if (o.label) {
        parts.push(
          `<text class="wfd-elabel" x="${lx}" y="${freeY(lx, ly, anchor)}" ` +
          `text-anchor="${anchor}">${escXml(o.label)}</text>`
        );
      }
      // Under the option's own name, in its column: what the pacing costs this
      // branch — and, while it is actually held, when the window opens.
      if (o.pace) {
        const word = isHeld(o)
          ? `held → ${fmtOpensAt(run.opens_at)}`
          : `every ${fmtPace(o.pace)}`;
        parts.push(
          `<text class="wfd-epace${isHeld(o) ? " held" : ""}" x="${lx}" ` +
          `y="${freeY(lx, ly + 11, anchor)}" text-anchor="${anchor}">` +
          `${escXml(word)}</text>`
        );
      }
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
    // Standing here, but on the clock rather than on the work. Distinct from
    // `current` because the two want opposite readings out of a reader: one
    // says "this is being worked", the other "this is not, and no press of
    // yours is owed".
    if (active && held) cls.push("holding");
    if (id === selected) cls.push("selected");
    const flags = [];
    if (s.gate) flags.push("gate");
    if (s.verify) flags.push("verify");
    if (s.select) flags.push(`select:${s.select.chooser}`);
    // A step is paced if any way out of it is. The box says the property
    // exists; the edges say which branch carries it, and at what cadence.
    if (s.select && (s.select.options || []).some((o) => o.interval)) {
      flags.push("paced");
    }
    // Shape last, after the properties: `fork:2` counts where this step can
    // send the run, `merge:2` counts how many places send the run here. The
    // arrowheads say the second one too — this says it in a number, and says
    // it on the box the reader is already looking at.
    if ((forkOut.get(id) || 0) > 1) flags.push(`fork:${forkOut.get(id)}`);
    if (isMerge(id)) flags.push(`merge:${mergeIn.get(id)}`);
    const title = s.title && s.title !== s.id ? `${s.id} — ${s.title}` : s.id;
    // 12px of padding each side, and the visit counter takes the right end of
    // the line when a step has been stood on twice.
    const room = NW - 24 - (visits[id] > 1 ? 26 : 0);
    const shown = wfdFit(title, 13, room);
    parts.push(`<g class="${cls.join(" ")}" data-step="${escXml(s.id)}">`);
    // First child, where SVG says a <title> belongs: it is the group's
    // tooltip, and what the cut title dropped is only reachable here.
    if (shown !== title) parts.push(`<title>${escXml(title)}</title>`);
    parts.push(`<rect x="${NX}" y="${y}" width="${NW}" height="${NH}" rx="8"/>`);
    parts.push(
      `<text class="wfd-title" x="${NX + 12}" y="${y + (flags.length ? 19 : 27)}">` +
      `${escXml(shown)}</text>`
    );
    if (flags.length) {
      /* This line was never cut to the box, and adding `fork:`/`merge:` to it
         is what makes that matter: `gate · verify · select:agent · paced ·
         fork:3 · merge:2` is 54 characters in a box that holds about 34 at
         10px. No workflow shipped today reaches that, which is the same
         "green by accident" the titles were in before they were cut. The full
         line stays reachable as this text's own tooltip, so nothing a cut
         drops is lost — and the node's <title> keeps carrying the title
         alone, which is what a reader hovering the box asks for. */
      const flagStr = flags.join(" · ");
      const shownFlags = wfdFit(flagStr, 10, NW - 24);
      parts.push(
        `<text class="wfd-flags" x="${NX + 12}" y="${y + 35}">` +
        (shownFlags === flagStr ? "" : `<title>${escXml(flagStr)}</title>`) +
        `${escXml(shownFlags)}</text>`
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

/* The key to the dashes, offered only by the drawings that have any. A dashed
   branch means nothing on its own, and pacing is the one thing in this picture
   a reader has no other way to learn: the run page's prose says it while a
   choice is held, and says nothing at all the rest of the time. */
function wfPacedNote(wf, run) {
  const paced = (wf.steps || []).some(
    (s) => s.select && (s.select.options || []).some((o) => o.interval)
  );
  if (!paced) return null;
  const held = run && run.status === "waiting_window"
    ? ` — '${run.option}' is held now, and opens ${fmtOpensAt(run.opens_at)}`
    : "";
  return el(
    "p", "wf-note",
    "a dashed branch is paced: the run may take it at most once per the " +
    "interval on it, and a choice made inside that interval is held until " +
    "the window opens — the daemon releases it, not you" + held
  );
}

/* ------------------------------------------------------------------ */
/* the run in the time domain                                          */
/* ------------------------------------------------------------------ */
/* The graph above says what this workflow CAN do. The journal says what this
   run DID — and it said it as two hundred lines of prose in a fold nobody
   opens twice. The same record laid on a time axis answers the one question
   the prose cannot: where the wall clock actually went. One lane per step, a
   bar per visit, and inside the bar the stretches where nothing was being
   worked — a choice presented and not yet confirmed, a gate open, a question
   sitting with a peer — drawn apart from the stretches where it was. A step
   that took twenty minutes because a leader took nineteen of them to answer
   looks nothing like one that took twenty minutes of work, and on this
   picture they no longer read alike. */

/* Which journal events open a stretch of WAITING inside a step, and what
   closes each. The engine writes no single "waiting" event: every door has
   its own pair, and a step can be behind two at once (a paced option chosen
   inside its interval is held while the selection it belongs to is still
   open), so they are tracked independently rather than collapsed to a flag.
   `state_forced` closes every door because it is the one press that takes
   the run somewhere else without answering any of them. */
const WFT_WAIT = {
  select_presented: {
    kind: "select", label: "choice open",
    ends: ["select_confirmed", "select_hold_cancelled", "state_forced"],
  },
  select_held: {
    kind: "window", label: "held for its window",
    ends: ["select_confirmed", "select_hold_cancelled", "window_discarded",
           "state_forced"],
  },
  gate_wait: {
    kind: "gate", label: "gate",
    ends: ["approved", "state_forced"],
  },
  ask_opened: {
    kind: "ask", label: "asked a peer",
    ends: ["ask_answered", "ask_declined", "ask_abstained", "ask_escalated",
           "ask_unresolved", "ask_discarded", "ask_unanswered_proceeded",
           "ask_withdraw_failed", "state_forced"],
  },
};

/* The events that happened AT a moment rather than over one — a glyph on the
   bar, not a stretch of it. Anything unlisted is bookkeeping and is left out
   rather than drawn as a mystery (the rule traceFlowLabel already follows). */
function wftMark(e) {
  const clip = (s, n) => wftClip(String(s == null ? "" : s)
    .replace(/\s+/g, " ").trim(), n);
  switch (e.event) {
    case "step_report":
      return { kind: "report", glyph: "◆", label: `report — ${clip(e.summary, 90)}` };
    case "verify_passed":
      return { kind: "pass", glyph: "✓", label: "verify passed" };
    case "verify_failed":
      return { kind: "fail", glyph: "✗", label: `verify FAILED — ${clip(e.output, 90)}` };
    case "verify_discarded":
      return { kind: "fail", glyph: "✗", label: "verify discarded" };
    case "select_confirmed":
      return { kind: "chose", glyph: "●", label: `chose ${e.option || "?"}` };
    case "approved":
      return { kind: "chose", glyph: "●", label: `approved by ${e.by || "?"}` };
    // The three ways a delegated question ends without an answer. They are
    // the reason a step's bar can be long with no door left open on it, so
    // leaving them off would make the picture look like time spent working.
    case "ask_unanswered_proceeded":
      return { kind: "forced", glyph: "⋯", label: "nobody answered — proceeded" };
    case "ask_unresolved":
      return { kind: "forced", glyph: "⋯", label: "nobody to ask" };
    case "ask_escalated":
      return { kind: "forced", glyph: "↑", label: `escalated to ${e.to || "?"}` };
    case "state_forced":
      return { kind: "forced", glyph: "⤳", label: `forced to ${e.step || "?"}` };
    case "loop_limit":
      return { kind: "fail", glyph: "!", label: "loop limit reached" };
    case "loop_extended":
      return { kind: "chose", glyph: "↻", label: "loop limit raised" };
    default:
      return null;
  }
}

/* journal -> the picture's model. Kept apart from the drawing because the
   interesting half is here: which bar an event belongs to, which door it
   opened or shut, and where a run that is still going has its right edge.
   `nowMs` is passed in rather than read so the model is a pure function of
   the record — a drawing that moves on its own cannot be pinned by a test. */
function wfTimeline(wf, run, journal, nowMs) {
  const entries = [];
  for (const e of journal || []) {
    const ms = Date.parse(e.at || "");
    if (Number.isFinite(ms)) entries.push({ e, ms });
  }
  // Stable (ES2019 on), which matters here: a step completing and the next
  // being delivered share a timestamp to the second, and the file's order is
  // the only thing that says which of them came first.
  entries.sort((a, b) => a.ms - b.ms);
  const runStart = Date.parse((run || {}).started_at || "");
  if (!entries.length && !Number.isFinite(runStart)) return null;
  const t0 = entries.length ? entries[0].ms : runStart;
  const last = entries.length ? entries[entries.length - 1].ms : t0;
  const status = (run || {}).status;
  const live = status !== "done" && status !== "aborted";
  const now = Number.isFinite(nowMs) ? nowMs : Date.now();
  // A run still going owns the axis out to now, so its open bar grows with
  // every poll instead of stopping at whatever it last wrote. The floor keeps
  // a run that has only just started from dividing by zero.
  const t1 = Math.max(last, live ? now : last, t0 + 1000);

  const bars = [];
  const runMarks = [];
  // Events that named a step the run had not been SEEN to reach yet. The
  // engine writes a door's outcome an instant before the step it belongs to
  // is delivered (`ask_unanswered_proceeded integrate` lands on the same
  // second as `step_delivered integrate`, just ahead of it), so an event with
  // nowhere to go is held rather than dropped, and laid on that step's bar
  // the moment it opens.
  const pending = {};
  let bar = null;
  const closeBar = (ms) => {
    if (!bar) return;
    bar.to = Math.max(bar.from, ms);
    for (const w of bar.waits) if (w.to === null) w.to = bar.to;
    bar = null;
  };
  // The doors and the point events, onto whatever bar is standing.
  const attach = (e, ms) => {
    if (!bar) return;
    const opens = WFT_WAIT[e.event];
    if (opens) {
      bar.waits.push({ kind: opens.kind, label: opens.label, from: ms,
                       to: null, ends: opens.ends });
    } else {
      for (const w of bar.waits) {
        if (w.to === null && w.ends.indexOf(e.event) >= 0) w.to = ms;
      }
    }
    const m = wftMark(e);
    if (m) bar.marks.push(Object.assign({ at: ms }, m));
  };
  const openBar = (step, visit, ms) => {
    // Closing on the way in, not only on completion: `state_forced` and a
    // resumed run both put the run somewhere else without the standing step
    // ever completing, and a bar left open runs across every lane below it.
    closeBar(ms);
    bar = {
      step, visit: visit || 0, from: ms, to: null, live: false,
      waits: [], marks: [],
    };
    if (!bar.visit) bar.visit = bars.filter((b) => b.step === step).length + 1;
    bars.push(bar);
    for (const q of pending[step] || []) attach(q.e, Math.max(ms, q.ms));
    pending[step] = [];
  };

  for (const rec of entries) {
    const e = rec.e, ms = rec.ms;
    if (e.event === "started") {
      runMarks.push({ at: ms, kind: "started",
                      label: `run started · ${e.workflow || ""}`.trim() });
      continue;
    }
    if (e.event === "done" || e.event === "aborted" || e.event === "archived") {
      closeBar(ms);
      runMarks.push({
        at: ms, kind: e.event,
        label: e.event === "done" ? "run finished" : `run ${e.event}`,
      });
      continue;
    }
    if (e.event === "step_delivered") { openBar(e.step || "?", e.visit, ms); continue; }
    if (e.event === "step_completed") { closeBar(ms); continue; }
    const step = e.step;
    if (step && (!bar || bar.step !== step)) {
      // A door is opened AT the step being entered, BEFORE its instructions
      // are handed over — and a step that is nothing but a choice (a leader's
      // `standby`) is never delivered at all, so a run can sit in one for
      // twenty minutes with no `step_delivered` anywhere in the record. The
      // door itself is therefore what puts the run in the step; without this
      // the picture calls that step never entered.
      if (WFT_WAIT[e.event]) openBar(step, e.visit, ms);
      else { (pending[step] = pending[step] || []).push({ e, ms }); continue; }
    }
    attach(e, ms);
  }
  if (bar) {
    bar.live = live;
    bar.to = t1;
    for (const w of bar.waits) if (w.to === null) w.to = t1;
  }

  const lanes = [];
  const byLane = new Map();
  const byId = {};
  for (const s of (wf && wf.steps) || []) byId[s.id] = s;
  const lane = (id, declared) => {
    let l = byLane.get(id);
    if (!l) {
      const s = byId[id];
      l = {
        id, declared,
        title: s && s.title && s.title !== id ? s.title : "",
        bars: [], busy: 0, waited: 0,
      };
      byLane.set(id, l);
      lanes.push(l);
    }
    return l;
  };
  for (const id of wfStepOrder(wf)) lane(id, true);
  for (const b of bars) {
    // A run outlives the file it was cut from: a step the workflow no longer
    // declares still happened, and dropping it would leave a hole in the
    // clock with nothing on the page saying why.
    const l = lane(b.step, false);
    l.bars.push(b);
    l.busy += b.to - b.from;
    for (const w of b.waits) l.waited += Math.max(0, w.to - w.from);
  }
  return { t0, t1, span: t1 - t0, live, lanes, bars, runMarks };
}

/* ---- drawing ------------------------------------------------------ */
/* Same width as the state graph above it, and pinned at its natural size for
   the same reason: one SVG unit is one CSS pixel, so a wide column must not
   blow the type up with it. */
const WFT = {
  w: 480,      // total, matching wfDiagramSvg's W
  name: 112,   // the step names down the left
  right: 54,   // the per-step total down the right
  head: 26,    // the elapsed axis across the top
  row: 20,     // lane pitch
  bar: 11,     // the occupancy bar inside a lane
};

/* Tick cadences that read whole. The axis is elapsed time, so these are the
   units a person says out loud: seconds, minutes, hours, then days. */
const WFT_TICKS = [1, 5, 15, 30, 60, 300, 900, 1800, 3600, 7200, 21600,
                   43200, 86400, 172800, 604800];

function wftTickStep(spanMs, want) {
  const target = spanMs / (want || 5) / 1000;
  for (const s of WFT_TICKS) if (s >= target) return s * 1000;
  return WFT_TICKS[WFT_TICKS.length - 1] * 1000;
}

function wftClip(s, n) {
  const t = String(s == null ? "" : s);
  return t.length > n ? t.slice(0, n - 1) + "…" : t;
}

/* A duration as this picture says it: at most two units and never a decimal
   — "8m40s", not "8.67m". The axis ticks land on whole units by construction,
   the per-step totals down the right do not, and a column of totals nobody
   can read at a glance is a column of noise. Truncating rather than rounding
   the smaller unit keeps "1h59m" from ever printing as "1h60m". */
function wftDur(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) {
    const m = Math.floor(s / 60), r = s % 60;
    return r ? `${m}m${r}s` : `${m}m`;
  }
  if (s < 86400) {
    const h = Math.floor(s / 3600), r = Math.floor((s % 3600) / 60);
    return r ? `${h}h${r}m` : `${h}h`;
  }
  const d = Math.floor(s / 86400), r = Math.floor((s % 86400) / 3600);
  return r ? `${d}d${r}h` : `${d}d`;
}

function wftStamp(ms) {
  return new Date(ms).toISOString().replace("T", " ").slice(0, 19) + "Z";
}

function wfTimelineSvg(model, selected, run) {
  const lanes = model.lanes;
  const H = WFT.head + lanes.length * WFT.row + 8;
  const plotX = WFT.name;
  const plotW = WFT.w - WFT.name - WFT.right;
  // Two decimals is a twentieth of a pixel — past that the coordinates are
  // noise, and a run with two hundred journal entries pays for every digit
  // of it on every two-second poll.
  const n = (v) => Math.round(v * 100) / 100;
  const x = (ms) => n(plotX + ((ms - model.t0) / model.span) * plotW);
  const current = run && run.step_id &&
    run.status !== "done" && run.status !== "aborted" ? run.step_id : null;

  const parts = [];
  parts.push(
    `<svg viewBox="0 0 ${WFT.w} ${H}" width="${WFT.w}" height="${H}" ` +
    `xmlns="http://www.w3.org/2000/svg" class="wft">`
  );

  // the elapsed axis: gridlines the full height, labelled once across the top
  const step = wftTickStep(model.span, 5);
  parts.push(
    `<text class="wft-axis" x="${plotX - 8}" y="${WFT.head - 8}" ` +
    `text-anchor="end">elapsed →</text>`
  );
  for (let t = model.t0; t <= model.t1 + 1; t += step) {
    const gx = x(t);
    parts.push(
      `<line class="wft-grid" x1="${gx}" y1="${WFT.head - 4}" ` +
      `x2="${gx}" y2="${H - 6}"/>`
    );
    parts.push(
      `<text class="wft-tick" x="${gx + 3}" y="${WFT.head - 8}">` +
      `${escXml(t === model.t0 ? "0" : wftDur(t - model.t0))}</text>`
    );
  }
  // The right edge is a moment too, and on a live run it is *now* — said once
  // at the end of the axis rather than drawn as a line that creeps.
  parts.push(
    `<text class="wft-tick end" x="${WFT.w - 6}" y="${WFT.head - 8}" ` +
    `text-anchor="end">` +
    `${escXml((model.live ? "now · " : "") + wftDur(model.span))}</text>`
  );

  lanes.forEach((lane, i) => {
    const top = WFT.head + i * WFT.row;
    const mid = top + WFT.row / 2;
    const cls = ["wft-lane"];
    if (!lane.bars.length) cls.push("idle");
    if (lane.id === current) cls.push("current");
    if (lane.id === selected) cls.push("selected");
    if (!lane.declared) cls.push("gone");
    parts.push(`<g class="${cls.join(" ")}" data-step="${escXml(lane.id)}">`);
    const visits = lane.bars.length;
    parts.push(
      `<title>${escXml(
        lane.id + (lane.title ? ` — ${lane.title}` : "") + "\n" +
        (visits
          ? `${visits} visit${visits > 1 ? "s" : ""}, ${wftDur(lane.busy)} in all` +
            (lane.waited ? `, ${wftDur(lane.waited)} of it waiting` : "")
          : "never entered") +
        (lane.declared ? "" : "\nno longer declared by this workflow")
      )}</title>`
    );
    // The whole row is the click target, so a lane with no bar on it is still
    // the same press as its box in the graph above.
    parts.push(
      `<rect class="wft-hit" x="0" y="${top}" width="${WFT.w}" ` +
      `height="${WFT.row}"/>`
    );
    parts.push(
      `<line class="wft-base" x1="${plotX}" y1="${mid}" ` +
      `x2="${plotX + plotW}" y2="${mid}"/>`
    );
    parts.push(
      `<text class="wft-name" x="${WFT.name - 8}" y="${mid + 4}" ` +
      `text-anchor="end">${escXml(wftClip(lane.id, 17))}</text>`
    );
    if (visits) {
      parts.push(
        `<text class="wft-total" x="${WFT.w - 6}" y="${mid + 4}" ` +
        `text-anchor="end">${escXml(
          wftDur(lane.busy) + (visits > 1 ? ` ×${visits}` : "")
        )}</text>`
      );
    }

    for (const b of lane.bars) {
      const bx = x(b.from);
      const bw = n(Math.max(2, x(b.to) - bx));
      const by = mid - WFT.bar / 2;
      parts.push(`<g class="wft-bar${b.live ? " live" : ""}">`);
      parts.push(
        `<title>${escXml(
          `${lane.id} ×${b.visit}\n${wftStamp(b.from)} → ` +
          `${b.live ? "still standing here" : wftStamp(b.to)}\n` +
          `${wftDur(b.to - b.from)}` +
          b.waits.map((w) => `\n· ${w.label}: ${wftDur(w.to - w.from)}`).join("") +
          b.marks.map((m) => `\n· ${m.label}`).join("")
        )}</title>`
      );
      parts.push(
        `<rect class="wft-run" x="${bx}" y="${by}" width="${bw}" ` +
        `height="${WFT.bar}" rx="2"/>`
      );
      for (const w of b.waits) {
        const wx = x(w.from);
        const ww = n(Math.max(1, x(w.to) - wx));
        parts.push(
          `<rect class="wft-wait ${w.kind}" x="${wx}" y="${by}" ` +
          `width="${ww}" height="${WFT.bar}"/>`
        );
      }
      for (const m of b.marks) {
        parts.push(
          `<text class="wft-mark ${m.kind}" x="${x(m.at)}" ` +
          `y="${by + WFT.bar - 2}" text-anchor="middle">` +
          `${escXml(m.glyph)}</text>`
        );
      }
      parts.push("</g>");
    }
    parts.push("</g>");
  });

  parts.push("</svg>");
  return parts.join("");
}

/* The timing diagram as the run page's element: the model, the drawing, and
   the lane clicks bound to the same selection the graph's boxes drive. Null
   when there is nothing to lay on an axis — a run whose first step has not
   been delivered has a journal but no duration anywhere in it. */
function wfTimelinePanel(data, ui) {
  const model = wfTimeline(
    data.workflow || {}, data.run || {}, data.journal, Date.now()
  );
  if (!model || !model.bars.length) return null;
  const box = el("div", "wf-time");
  box.appendChild(el("h4", "wf-time-head", "time domain"));
  const svg = el("div", "wf-time-svg");
  svg.innerHTML = wfTimelineSvg(model, ui.getStep(), data.run || {});
  svg.querySelectorAll("g.wft-lane[data-step]").forEach((g) => {
    g.addEventListener("click", () => {
      const step = g.dataset.step;
      ui.select(ui.getStep() === step ? null : step);   // click again = clear
    });
  });
  box.appendChild(svg);
  box.appendChild(el(
    "p", "wf-time-key",
    "one lane per step, one bar per visit. The pale stretch inside a bar is " +
    "time the run spent at a door — a choice open, a gate, a question with a " +
    "peer — rather than working; ◆ a report, ✓/✗ a verify, ● the option taken."
  ));
  return box;
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
let sessMigrateBox = null; // and the migrate picker — same reason again
let sessReborrowBox = null; // and the borrow picker — same reason again
let sessPermsBox = null;    // and the permissions toggle — same reason again
let sessRunFold = null;   // reused across polls too: it holds open/shut
let sessRunTimer = null;  // the fold's own poll, alive only while it is open
let sessQuickJobBox = null; // the quick-job form — it holds a typed task
let sessKidsBox = null;   // the children panel, which polls on its own
let sessKidsTimer = null;

/* Forget the open detail. State only — the caller syncs the layout, which is
   what actually takes the panel off the screen. */
function dropDetail() {
  if (sessPollTimer) { clearInterval(sessPollTimer); sessPollTimer = null; }
  stopSessRun();
  stopSessKids();
  sessName = null;
  sessStartBox = null;
  sessSendBox = null;
  sessMigrateBox = null;
  sessReborrowBox = null;
  sessPermsBox = null;
  sessQuickJobBox = null;
  sessKidsBox = null;
  sessBeadsBox = null;
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
  stopSessKids();
  sessName = name;
  sessStartBox = null;
  sessSendBox = null;
  sessMigrateBox = null;
  sessReborrowBox = null;
  sessPermsBox = null;
  sessQuickJobBox = null;
  sessKidsBox = null;
  sessBeadsBox = null;
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
  // Directly after the name, the other name: what the mesh calls this
  // session when that differs. The panel already carried it, at the bottom,
  // inside the Meshes chips — which is the wrong altitude for an identity.
  // A reader arrives here from a mesh log holding a handle and needs the
  // head to confirm they opened the right session, before any of the
  // metadata below is worth reading.
  const hTag = handleTag(s.name || "");
  if (hTag) {
    const chip = el("span", "sess-handle", hTag.text);
    chip.title = hTag.title;
    head.appendChild(chip);
  }
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
  // Where it runs, under the name — the rail row's line again, here so the
  // panel says it whichever tab is lit (the Details list's `directory` row
  // is the full path, and it is not on the Workflow tab at all). Appended
  // before the early return below because it belongs to every arrangement
  // of this head, not only the ones that keep the buttons; the stylesheet
  // orders it last so it is the head's own bottom line.
  head.appendChild(cwdLine(s, "sess-cwd"));
  if (mine && terminalOnScreen() && !MOBILE_MQ.matches) return head;

  // The spawn wizard, aimed at this session: the panel's verb for growing a
  // fleet under whatever this session is. An exited session has nothing to
  // spawn from, so its button is the one head action that state denies.
  const spawn = el("button", "wf-btn approve", "Spawn");
  spawn.title = s.status === "exited"
    ? "an exited session cannot spawn children"
    : "the spawn wizard, with this session as the parent";
  if (s.status === "exited") spawn.disabled = true;
  spawn.addEventListener("click", () => openSpawnModal(s.name));
  head.appendChild(spawn);

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

/* The radio under the head: which panel this column is. `Details` is the
   session's facts and the ways to speak to it; `Workflow` is its run, given
   the whole column instead of a section at the bottom of one. A pair of
   buttons where one is always dead, like the trace page's mesh tabs — the
   lit one not being wired is what makes the pair read as a radio. The choice
   is the session's, remembered with its layout (sessLayoutFor). */
function sessRailTabs(name) {
  const bar = el("div", "seq-tabs sess-tabs");
  const cur = sessLayoutFor(name).rail;
  for (const [id, label] of [["detail", "Details"], ["wf", "Workflow"]]) {
    const on = cur === id;
    const tab = el("button", "seq-tab" + (on ? " on" : ""), label);
    tab.title = id === "wf"
      ? "this session's workflow run, at full height"
      : "what this session is: metadata, messages, meshes";
    if (!on) {
      tab.addEventListener("click", () => {
        setSessLayout(name, { rail: id });
        refreshSession();   // redraw now, not at the poll's leisure
      });
    }
    bar.appendChild(tab);
  }
  return bar;
}

/* What this session was asked to do, in the words it was asked in.

   The record was always there — `SessionDef.task`, kept so a re-briefing can
   restate the job after a compaction (daemon/harness.py) — and it rides every
   session payload the daemon serves. One place read it: the rail row's
   one-liner, and only as the fallback for when no LLM briefing exists, cut to
   a single line. The whole of it was drawn nowhere, and "what was this
   session started for" is the question this panel gets opened with more often
   than any single fact in the list above it.

   Drawn as typed, not as markdown: an opening task is instructions, usually a
   list, and the renderer would eat the characters that carry them.

   A long one is clipped rather than dropped — the briefing, the send box and
   the board all sit under this, and a twenty-line brief would push them off
   the rail — with a toggle for the whole of it. That toggle's state is held
   in a set outside the panel, like the rail's briefing cards, because the 2s
   poll rebuilds every node in here: state kept on the node itself would fold
   shut under the reader's hand two seconds after they opened it.

   Empty is drawn rather than hidden. A session a person opened by hand has no
   task and that is the ordinary case, so a section that appeared only
   sometimes would read as one that failed to load. */
const sessTaskOpen = new Set();   // session names whose task is unfolded

/* Long enough to be worth folding. Lines first, characters as the backstop
   for a task typed as one unbroken paragraph — either way the question is how
   much of the rail the section takes, not what the task says. */
function taskIsLong(text) {
  return text.split("\n").length > 8 || text.length > 480;
}

function sessTask(s) {
  const box = el("div", "sess-task");
  const head = el("h3", null, "Opening task");
  head.title =
    "what this session was opened with, recorded at creation — the live " +
    "copy was typed in once, on its first spawn, and is never replayed";
  box.appendChild(head);
  const text = String(s.task || "");
  if (!text.trim()) {
    box.appendChild(el(
      "p", "wf-note",
      "no opening task recorded — a session opened by hand often has none"
    ));
    return box;
  }
  const name = s.name || "";
  const open = sessTaskOpen.has(name);
  const long = taskIsLong(text);
  box.appendChild(el(
    "pre", "sess-task-text" + (long && !open ? " clipped" : ""), text
  ));
  if (long) {
    const more = el("button", "wf-btn option sess-task-more",
                    open ? "Show less" : "Show all");
    more.title = open
      ? "fold it back to its first lines"
      : `the whole task — ${text.split("\n").length} lines`;
    more.addEventListener("click", () => {
      if (open) sessTaskOpen.delete(name);
      else sessTaskOpen.add(name);
      refreshSession();   // redraw now, not at the poll's leisure
    });
    box.appendChild(more);
  }
  return box;
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

  // The workflow used to be the last section of this panel; now it is the
  // radio's other panel, with the column to itself. Everything below the
  // branch is the Details panel only.
  const name = s.name || sessName;
  view.appendChild(sessRailTabs(name));
  if (sessLayoutFor(name).rail === "wf") {
    view.appendChild(sessWorkflow(data));
    return;
  }
  // The fold belongs to the other panel; showing this one must take its poll
  // down with it, or it keeps asking about a run nobody is reading.
  stopSessRun();
  sessRunFold = null;

  const dl = el("dl", "sess-meta");
  metaRow(
    dl, "profile / harness", profileHarnessLabel(s.profile, s.harness),
    (data.harness || {}).description
  );
  // Whose token it actually runs on, when that is not the profile's own —
  // invisible from the terminal, and reapplied on every restore.
  metaRow(dl, "borrow", s.borrow, "another profile's token; the config stays this profile's");
  if (s.null_token) metaRow(dl, "auth", "--null (no OAuth token injected)");
  // Whose this session is. Near the top because it changes how everything
  // below it reads: an inherited mesh and a scoped run are the parent's
  // arrangement, not choices this session made.
  metaRow(dl, "spawned by", s.parent, "the session that created this one");
  metaRow(
    dl, "role", data.role ? data.role.name : s.role,
    data.role ? data.role.stance : ""
  );
  // Beside the role, because the two are one fact between them: what this
  // session is on a mesh, and what it is called there. Spelt out as a row
  // rather than left to the head's chip so it can say WHICH room each name
  // belongs to — the chip has room for one word and a count.
  const handles = sessHandles(s.name || "");
  if (handles.length) {
    metaRow(
      dl, "mesh handle",
      handles.map((h) => `${h.handle} (in ${h.mesh})`).join(", "),
      "the name this session joined its mesh under — messages to it are " +
      "addressed to this, not to the session name"
    );
  }
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
  // The checkout's branch, beside the directory — the one thing the
  // directory's tail cannot say: two sessions from one worktree share the
  // same path, and only the branch tells them apart.
  metaRow(
    dl, "branch", s.branch,
    "the git branch checked out in the session's directory"
  );
  metaRow(dl, "conversation", s.conversation_id, "claude --session-id");
  // Which model is answering in it — above the size for the same reason the
  // gauge line leads with it: the count means different things on different
  // models, and this is the only place outside the terminal that says. Its
  // own row rather than a clause inside the context sentence, because it is
  // the fact people open this panel to check, and a full dated id has no
  // business being read out of the middle of a sentence about tokens.
  metaRow(
    dl, "model", modelSentence(s),
    "the model of the latest context reading — claude's transcript or " +
    "codex's rollout; a /model switch shows here once the next turn finishes"
  );
  // Under the conversation, because it is a fact about the conversation and
  // not about the process: how much of it the harness last carried.
  metaRow(dl, "context", ctxSentence(s), ctxBreakdown(s.context));
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

  // What it was asked to do, in the words it was asked in. Above the
  // briefing because the briefing is a reading OF this — the summary says
  // what the session has made of the job, and comparing the two is only
  // possible with the original in front of you.
  view.appendChild(sessTask(s));

  // What this session is DOING, next to the facts above: the llm summary,
  // fetched on first open and repainted by the 2s poll, with the card's own
  // ⟳ for a fresh read. Here, high up, because it is the reason the panel
  // gets opened more often than the metadata is.
  if (s.name) view.appendChild(sessBriefSection(s.name));

  // Above the memberships, because it is what they are FOR: the list says
  // which rooms this session can be spoken to in, this says something in one.
  view.appendChild(sessSend(data));

  // And directly under the send box, what became of messages like it: the
  // backlog the daemon has accepted but not yet typed in, with the reason.
  // "Send" answering with a quiet terminal is exactly when this is read.
  const queued = sessQueued(data);
  if (queued) view.appendChild(queued);

  // And under THAT, what never became a backlog at all: the senders the
  // mesh turned away because this one had stopped reading. Drawn even with
  // an empty queue above it — a refusal leaves no message to list, so an
  // empty panel is exactly the wrong answer to "why has nobody written".
  const bpBox = sessBackpressure(data);
  if (bpBox) view.appendChild(bpBox);

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

  // Its work, as the board records it: the issue it was created for and
  // every issue that names it. Right after the memberships because the two
  // answer the same question from two registries — where it belongs, and
  // what it is on.
  view.appendChild(sessBeads(data));
  // And what it actually left in the repository. Beside the board and the
  // round reports because the three are one answer read from three places:
  // what it was asked to do, what it wrote up, what it committed.
  view.appendChild(sessCommits(data));

  // What this session is FOR, by role: a leader gets its dispatch and reaping
  // panels here, other roles whatever ROLE_PANELS declares for them. Between
  // the memberships (identity) and the migrate form (plumbing), because these
  // are the panel's verbs.
  for (const build of rolePanels(data)) view.appendChild(build(data));

  view.appendChild(sessReborrow(data));
  view.appendChild(sessPerms(data));
  view.appendChild(sessMigrate(data));
}

/* ---- restart this session on another answer to "whose token" ----
   `claunch reborrow`, from the panel that names the session. A managed
   session's auth is part of its definition — reapplied on every restore —
   so changing it is a restart, not an edit: the daemon stops the session and
   relaunches it with the auth swapped. Same name, same conversation, same
   directory — unlike a migrate there is nothing to carry. The picker asks
   the same question creation asks: its own token or a borrowed one; Claude
   additionally offers none (--null). Picking any clears the others, so a
   borrow chosen on a --null Claude session turns the token back on. */
function sessReborrow(data) {
  const s = data.session || {};
  const harness = data.harness || {};
  const box = el("div", "sess-reborrow");
  box.appendChild(el("h3", null, "Borrowed auth"));

  // Borrowability comes from the packaged/custom harness auth contract. Keep
  // Claude as the old-daemon fallback, but do not infer every other harness is
  // OAuth: API-key harnesses consume the same base-profile token.
  const borrowable = typeof harness.borrowable === "boolean"
    ? harness.borrowable : s.harness === "claude";
  if (!borrowable) {
    sessReborrowBox = null;
    box.appendChild(el(
      "p", "wf-note",
      `harness ${s.harness || "?"} keeps ${harness.auth || "its"} auth in ` +
      "the selected profile's own storage — there is no shared token to borrow"
    ));
    return box;
  }

  // Rebuilt only when the auth state changes — the 2s poll must not wipe a
  // picked answer, and a successful restart changes the key, which is what
  // re-aims the picker at the new current.
  const validation = data.borrowed_auth || null;
  const validationKey = validation
    ? `${validation.status || ""}:${validation.ready ? 1 : 0}:${validation.message || ""}`
    : "own";
  const key = `${s.name}|${s.borrow || ""}|${s.null_token ? 1 : 0}|${validationKey}`;
  if (sessReborrowBox && sessReborrowBox.dataset.slot === key) {
    box.appendChild(sessReborrowBox);   // appending moves the live node here
    return box;
  }
  const form = el("div", "sess-reborrow-form");
  form.dataset.slot = key;
  sessReborrowBox = form;
  box.appendChild(form);

  // Whose token it runs on now — one accurate sentence for each of the
  // three modes — plus the warning that changing it costs a restart.
  const providerBorrow = (harness.borrow_mode || "provider-token") === "provider-token";
  const baseProfile = baseProfileName(s.profile);
  const current = s.borrow
    ? `borrowing ${s.borrow}'s token${providerBorrow ? " and provider/backend" : ""}` +
      ` — the config and skills stay ${baseProfile}'s`
    : s.null_token
      ? "started --null — no token is injected at all"
      : `running on ${baseProfile}'s own token`;
  form.appendChild(el(
    "p", "wf-note",
    `${current}. Changing it stops the session and relaunches it — same name, same conversation`
  ));
  if (s.borrow) {
    const good = !!(validation && validation.valid);
    form.appendChild(el(
      "p", good ? "wf-note" : "wf-warning",
      `${good ? "✓" : "⚠"} validation: ` +
      (validation ? validation.message : "the daemon did not report a borrow check")
    ));
  }

  const row = el("div", "sess-send-row");
  const dest = document.createElement("select");
  dest.disabled = true;
  dest.appendChild(el("option", null, "reading the profiles…"));
  row.appendChild(dest);
  const goBtn = el("button", "wf-btn option", "Restart");
  goBtn.disabled = true;
  goBtn.title = "stop the session and relaunch it on the picked auth";
  const status = el("p", "wf-note hidden");
  form.append(row, goBtn, status);

  const say = (msg, cls) => {
    status.className = cls || "wf-note";
    status.textContent = msg;
  };

  // One question, three answers: own token, none, or a borrowed one. The
  // values are prefixed like the migrate picker's (wt:), so a profile named
  // 'own' or 'null' cannot collide with a mode. The current one is
  // preselected and picking it again enables nothing — a restart that
  // changes nothing is the daemon's refusal, mirrored here as a dead button.
  const currentChoice = s.borrow ? `b:${s.borrow}` : s.null_token ? "null" : "own";
  (async () => {
    let doc = { options: [] };
    let readError = "";
    try {
      // A bare saved profile may have acquired a different YAML default
      // since this process started. Validate against the harness the live
      // session will actually restart from; the reborrow boundary does the
      // same check against old.harness before stopping anything.
      const runtimeSelector = s.profile && !s.profile.includes(":") && s.harness
        ? `${s.profile}:${s.harness}` : (s.profile || "");
      doc = await readBorrowOptions(runtimeSelector);
    } catch (e) {
      readError = `cannot validate borrow candidates: ${e.message || e}`;
      say(readError, "wf-warning");
    }
    if (sessReborrowBox !== form) return; // the panel moved on mid-flight
    dest.innerHTML = "";
    const choices = [
      { value: "own", label: `its own token (${baseProfile})`, selectable: true },
      ...(s.harness === "claude"
        ? [{ value: "null", label: "no token (--null)", selectable: true }]
        : []),
      ...(doc.options || [])
        .filter((item) => item.name !== baseProfile)
        .map((item) => ({
          value: `b:${item.name}`,
          label: `borrow: ${item.label || item.name}`,
          selectable: !!item.selectable,
          message: item.message || "",
        })),
    ];
    if (s.borrow && !choices.some((item) => item.value === currentChoice)) {
      choices.push({
        value: currentChoice,
        label: `borrow: ${s.borrow} — ${readError || "not available for this harness"}`,
        selectable: false,
        message: readError || "the current lender is not available for this harness",
      });
    }
    for (const item of choices) {
      const opt = document.createElement("option");
      opt.value = item.value;
      opt.textContent = item.label;
      opt.disabled = !item.selectable;
      opt.title = item.message || "";
      dest.appendChild(opt);
    }
    dest.value = currentChoice;
    const sync = () => {
      const selected = [...dest.options].find((o) => o.value === dest.value);
      goBtn.disabled = dest.value === currentChoice || !!(selected && selected.disabled);
    };
    dest.addEventListener("change", sync);
    dest.disabled = false;
    sync();
  })();

  goBtn.addEventListener("click", async () => {
    if (goBtn.disabled) return;
    const name = s.name;
    const choice = dest.value;
    const body =
      choice === "own" ? { borrow: null }
      : choice === "null" ? { borrow: null, null_token: true }
      : { borrow: choice.slice(2) };
    goBtn.disabled = true;
    say("restarting… (stopping it, relaunching on the picked auth)");
    let doc = {};
    let resp;
    try {
      resp = await api(`/api/sessions/${encodeURIComponent(name)}/reborrow`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      doc = await resp.json().catch(() => ({}));
    } catch {
      say("could not reach the daemon — nothing was changed", "wf-warning");
      goBtn.disabled = false;
      return;
    }
    goBtn.disabled = false;
    if (!resp.ok) {
      say(doc.error || `HTTP ${resp.status}`, "wf-warning");
      return;
    }
    say(
      doc.borrow
        ? `restarted — now borrowing ${doc.borrow}`
        : doc.null_token
          ? "restarted — now running with no token (--null)"
          : `restarted — back on ${baseProfileName(doc.profile)}'s own token`
    );
    // The restart relaunched a fresh PTY under the same name; a terminal
    // attached to the old one is watching a socket that just died.
    if (currentName === name) {
      detach();
      await refreshSessions();
      attach(name);
    } else {
      refreshSessions();
    }
  });

  return box;
}

/* ---- restart this session with permission prompts off — or back on ----
   `claunch skip-permissions`, from the panel that names it. The flag lives
   in the definition's args, so toggling it is the same kind of restart as
   the auth picker's: stop, relaunch with the flag added or removed — same
   name, same conversation, same directory. Binary, so one button rather
   than a picker: it always offers the opposite of the current state. */
function sessPerms(data) {
  const s = data.session || {};
  const box = el("div", "sess-perms");
  box.appendChild(el("h3", null, "Permissions"));

  const capabilities = harnessDetails[s.harness] || {};
  const permissionArgs = capabilities.skip_permissions_args || [];
  if (!permissionArgs.length) {
    sessPermsBox = null;
    box.appendChild(el(
      "p", "wf-note",
      `the ${s.harness || "?"} harness declares no approval-mode toggle`
    ));
    return box;
  }

  // Rebuilt only when the toggle's state changes — same keep-the-node rule
  // as the other restart pickers; a successful restart flips the key.
  const currentArgs = s.args || [];
  const skipping = currentArgs.some((_, i) =>
    permissionArgs.every((arg, j) => currentArgs[i + j] === arg));
  const flag = permissionArgs.join(" ");
  const key = `${s.name}|${skipping ? 1 : 0}`;
  if (sessPermsBox && sessPermsBox.dataset.slot === key) {
    box.appendChild(sessPermsBox);   // appending moves the live node here
    return box;
  }
  const form = el("div", "sess-perms-form");
  form.dataset.slot = key;
  sessPermsBox = form;
  box.appendChild(form);

  form.appendChild(el(
    "p", "wf-note",
    skipping
      ? `never asks before it acts (${flag}). ` +
        "Turning the asks back on stops the session and relaunches it — " +
        "same name, same conversation"
      : "asks before it acts. Skipping the asks " +
        `(${flag}) stops the session and relaunches ` +
        "it — same name, same conversation"
  ));
  const btn = el(
    "button", "wf-btn option", skipping ? "Ask again" : "Skip permissions"
  );
  btn.title = skipping
    ? `restart with ${flag} removed — the harness asks before it acts`
    : `restart with ${flag} — the harness stops asking`;
  const status = el("p", "wf-note hidden");
  form.append(btn, status);

  const say = (msg, cls) => {
    status.className = cls || "wf-note";
    status.textContent = msg;
  };

  btn.addEventListener("click", async () => {
    if (btn.disabled) return;
    const name = s.name;
    btn.disabled = true;
    say("restarting… (stopping it, relaunching with the flag toggled)");
    let doc = {};
    let resp;
    try {
      resp = await api(
        `/api/sessions/${encodeURIComponent(name)}/skip-permissions`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ skip: !skipping }),
        }
      );
      doc = await resp.json().catch(() => ({}));
    } catch {
      say("could not reach the daemon — nothing was changed", "wf-warning");
      btn.disabled = false;
      return;
    }
    btn.disabled = false;
    if (!resp.ok) {
      say(doc.error || `HTTP ${resp.status}`, "wf-warning");
      return;
    }
    say(
      (doc.args || []).some((_, i, args) =>
        permissionArgs.every((arg, j) => args[i + j] === arg))
        ? "restarted — now acting without asking"
        : "restarted — asking before it acts again"
    );
    // The restart relaunched a fresh PTY under the same name; a terminal
    // attached to the old one is watching a socket that just died.
    if (currentName === name) {
      detach();
      await refreshSessions();
      attach(name);
    } else {
      refreshSessions();
    }
  });

  return box;
}

/* ---- move this session to another checkout ----
   `claunch migrate-session`, from the panel that names the session. Claude
   keeps transcripts per working directory, so this is more than a cwd edit:
   the daemon stops the session, re-files its conversation under the target
   directory's slug, and relaunches it there — same name, same conversation,
   same mesh memberships. The picker offers the checkouts that make sense
   from where the session stands: its repository's existing worktrees, or a
   new one cut on the spot. Directories outside the repository stay a CLI
   affair (`--to DIR`), the same way free-text paths are kept out of the
   create form. */
function sessMigrate(data) {
  const s = data.session || {};
  const box = el("div", "sess-migrate");
  box.appendChild(el("h3", null, "Move to worktree"));

  // Rebuilt only when the session or its directory changes — the 2s poll
  // must not wipe a picked destination, and after a successful move the cwd
  // itself changes the key, which is what re-aims the picker.
  const key = `${s.name}|${s.cwd}`;
  if (sessMigrateBox && sessMigrateBox.dataset.slot === key) {
    box.appendChild(sessMigrateBox);   // appending moves the live node here
    return box;
  }
  const form = el("div", "sess-migrate-form");
  form.dataset.slot = key;
  sessMigrateBox = form;
  box.appendChild(form);

  form.appendChild(el(
    "p", "wf-note",
    "stops the session, carries its conversation to the checkout you pick, " +
    "and relaunches it there — same name, same conversation"
  ));

  const row = el("div", "sess-send-row");
  const dest = document.createElement("select");
  dest.disabled = true;
  dest.appendChild(el("option", null, "reading the repository…"));
  row.appendChild(dest);
  const nameIn = document.createElement("input");
  nameIn.placeholder = "new worktree name (blank = generated)";
  nameIn.className = "hidden";
  const kids = el("label", "check");
  const kidsBox = document.createElement("input");
  kidsBox.type = "checkbox";
  kids.append(kidsBox, el(
    "span", null, "also move children standing in this directory"
  ));
  const moveBtn = el("button", "wf-btn option", "Migrate");
  moveBtn.disabled = true;
  const status = el("p", "wf-note hidden");
  form.append(row, nameIn, kids, moveBtn, status);

  const say = (msg, cls) => {
    status.className = cls || "wf-note";
    status.textContent = msg;
  };

  // The choices are the daemon's answer, not a guess: which worktrees
  // already stand beside this session's checkout, and whether there is a
  // repository to cut a new one of at all.
  (async () => {
    let info = {};
    try {
      const resp = await api(`/api/git?cwd=${encodeURIComponent(s.cwd || "")}`);
      info = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(info.error || `HTTP ${resp.status}`);
    } catch (e) {
      say(`cannot read the repository: ${e.message || e}`, "wf-warning");
      return;
    }
    if (sessMigrateBox !== form) return; // the panel moved on mid-flight
    if (!info.repo) {
      say(
        "not inside a git repository — nothing to make a worktree of " +
        "(claunch migrate-session --to DIR moves it anywhere)",
        "wf-warning"
      );
      return;
    }
    dest.innerHTML = "";
    for (const w of info.worktrees || []) {
      const opt = document.createElement("option");
      opt.value = `wt:${w}`;
      opt.textContent = `worktree: ${w}`;
      dest.appendChild(opt);
    }
    const fresh = document.createElement("option");
    fresh.value = "new";
    fresh.textContent = "new worktree…";
    dest.appendChild(fresh);
    if (!(info.worktrees || []).length) dest.value = "new";
    const sync = () =>
      nameIn.classList.toggle("hidden", dest.value !== "new");
    dest.addEventListener("change", sync);
    sync();
    dest.disabled = false;
    moveBtn.disabled = false;
  })();

  moveBtn.addEventListener("click", async () => {
    if (moveBtn.disabled) return;
    const name = s.name;
    const wt = dest.value === "new" ? nameIn.value.trim() : dest.value.slice(3);
    moveBtn.disabled = true;
    say("migrating… (stopping it, moving its transcript, relaunching)");
    let doc = {};
    let resp;
    try {
      resp = await api(`/api/sessions/${encodeURIComponent(name)}/migrate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ worktree: wt, children: kidsBox.checked }),
      });
      doc = await resp.json().catch(() => ({}));
    } catch {
      say("could not reach the daemon — nothing was moved", "wf-warning");
      moveBtn.disabled = false;
      return;
    }
    moveBtn.disabled = false;
    if (!resp.ok) {
      say(doc.error || `HTTP ${resp.status}`, "wf-warning");
      return;
    }
    const kidsOk = (doc.children || []).filter((c) => c.ok);
    const kidsFailed = (doc.children || []).filter((c) => !c.ok);
    say(
      `migrated to ${doc.cwd}` +
      (kidsOk.length ? ` — ${kidsOk.map((c) => c.name).join(", ")} too` : "") +
      (kidsFailed.length
        ? `; NOT moved: ${kidsFailed
            .map((c) => `${c.name} (${c.error})`)
            .join(", ")}`
        : ""),
      kidsFailed.length ? "wf-warning" : "wf-note"
    );
    // The migrate relaunched a fresh PTY under the same name; a terminal
    // attached to the old one is watching a socket that just died.
    if (currentName === name) {
      detach();
      await refreshSessions();
      attach(name);
    } else {
      refreshSessions();
    }
  });

  return box;
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

/* ---- the backlog, in the panel ----
   The terminal page's banner (renderTermQueued) says the same thing to the
   person watching the terminal; this says it to the person reading the
   panel — which may be about ANOTHER session than the one on screen, so the
   "your typing" attribution only holds when the two names agree. Nothing
   rendered when the backlog is empty: an always-present "Queued (0)" box
   would train the eye to skip the one time it matters. */
function sessQueued(data) {
  const q = data.queued;
  if (!q || !(q.messages || []).length) return null;
  const s = data.session || {};
  const box = el("div", "sess-queued");
  if (q.state === "keyboard") box.className += " held";
  box.appendChild(el("h3", null, `Queued deliveries (${q.messages.length})`));
  box.appendChild(el(
    "p", q.state === "keyboard" ? "wf-warning" : "wf-note",
    "accepted by the mesh, not yet typed into the terminal — " +
    queuedReason(q, s.name === currentName)
  ));
  // Named, because this panel is often about a session the reader is not
  // looking at: "deliver now" alone would not say where the paste lands.
  if (s.name) {
    const status = el("p", "wf-note", "");
    const flush = el("button", "wf-btn", `deliver to ${s.name} now`);
    flush.type = "button";
    flush.title =
      "stop waiting for the agent's turn to end, for a keyboard to fall " +
      "quiet or for a hold to be lifted, and type these in — an unsent " +
      "line in the composer is submitted first, never typed over";
    flush.addEventListener("click", async () => {
      const { note } = await flushQueued(s.name, flush);
      status.textContent = note;
      status.className = note ? "wf-warning" : "wf-note";
      refreshSession();
    });
    box.appendChild(flush);
    box.appendChild(status);
  }
  for (const m of q.messages) box.appendChild(queuedMsgRow(m));
  return box;
}

/* ---- backpressure, in the panel ----
   The queued box above says what IS waiting. This says what is not, and
   never will be: the messages the mesh turned away at the door because this
   session's backlog had reached its cap.

   It is a separate box for one reason — it has to be able to draw when the
   backlog is EMPTY. A refusal leaves no message anywhere: not in the log
   (it was never appended), not in the queue (that is the whole point). The
   only trace is the recipient's record, and if nothing rendered it, the
   answer to "why has nobody messaged this session in ten minutes" would be
   an empty panel. Nothing is drawn while the door is open and nobody has
   been refused, though: an always-present "Refused (0)" would train the eye
   to skip the one time it matters. */
function sessBackpressure(data) {
  const bp = (data.queued || {}).backpressure;
  if (!bp || !bp.enabled) return null;
  const paced = bp.paced_for || 0;
  if (!bp.congested && !bp.refused && !paced) return null;
  const box = el("div", "sess-bp");
  if (bp.congested) box.className += " shut";
  box.appendChild(el("h3", null, "Mesh backpressure"));
  if (bp.congested) {
    box.appendChild(el(
      "p", "wf-warning",
      `at capacity — ${bp.queued} waiting, cap ${bp.inbox_max}. New messages ` +
      "for this session are refused at the door: their senders are told to " +
      "wait and re-send, and nothing of theirs is queued here."
    ));
  } else if (paced) {
    box.appendChild(el(
      "p", "wf-note",
      `delivery paced — the next block waits about ${Math.ceil(paced)}s, so ` +
      "arrivals in the meantime go in together rather than one at a time."
    ));
  }
  if (bp.refused) {
    box.appendChild(el(
      "p", "wf-note",
      `${bp.refused} message(s) turned away in the last 10 minutes.`
    ));
    const list = el("div", "bp-list");
    for (const r of bp.refused_from || []) {
      const row = el("div", "bp-row");
      row.appendChild(el("span", "bp-who", r.from));
      row.appendChild(el(
        "span", "bp-meta",
        `${r.count} refused` +
        (r.ago === null || r.ago === undefined ? "" : ` · last ${fmtAge(r.ago)} ago`)
      ));
      list.appendChild(row);
    }
    box.appendChild(list);
  }
  // Which room, and on which cap — a session in two meshes can be shut in
  // one and open in the other, and the two may be configured differently.
  for (const h of bp.handles || []) {
    if (!h.congested && !h.refused && !h.paced_for) continue;
    const row = el("div", "bp-row");
    row.appendChild(el("span", "bp-who", `${h.handle}@${h.mesh}`));
    row.appendChild(el(
      "span", "bp-meta",
      `${h.queued}/${h.inbox_max} queued` +
      (h.congested ? " · REFUSING" : "") +
      (h.paced_for ? ` · paced ${Math.ceil(h.paced_for)}s` : "")
    ));
    box.appendChild(row);
  }
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
    line.appendChild(el("span", ...wfMark(flow.status, flow)));
    line.appendChild(el("span", "sess-wf-name", flow.workflow || "(workflow)"));
    line.appendChild(el("span", "meta",
      flow.status +
      (flow.round ? ` · round ${flow.round}` : flow.recur ? " · recurs" : "")));
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
      const line2 = cflowLine(`${rep.step}: ${mdPlain(rep.summary)}`, "report");
      if (rep.details) line2.title = mdText(rep.details);
      box.appendChild(line2);
    }
    const link = el("button", "wf-btn approve", "Open the run page");
    link.title = "the full diagram, every report, and force-set state";
    link.addEventListener("click", () => {
      location.hash = "#/wf/" + encodeURIComponent(`${flow.scope}|${flow.cwd}`);
    });
    box.appendChild(link);
    if (pending) box.appendChild(pending);
    // Open from the start: this block is the Workflow panel now, chosen by
    // the radio — a reader who asked for the run should not find it folded.
    box.appendChild(sessRunFoldFor(flow, true));
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
function sessRunFoldFor(flow, unfold) {
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
  // On creation only, so shutting it stays shut across the panel's rebuilds:
  // the browser answers this assignment with the same toggle event a click
  // fires, and the poll starts through the one path above.
  if (unfold) fold.open = true;
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
  body.appendChild(wfActions(data, {
    archive: false, after: refreshSessRun, host: "fold",
  }));

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
    card.appendChild(mdInto(el("div", "wf-report-summary md"), r.summary || ""));
    if (r.details) {
      const more = document.createElement("details");
      more.className = "sess-run-more";
      more.appendChild(el("summary", null, "details"));
      more.appendChild(mdInto(el("div", "wf-report-details md"), r.details));
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

/* ---- role-specific panels ----------------------------------------------
   The rail reads the same for every session, but what a session is FOR
   differs by role: a leader dispatches work and reaps the workers it
   spawned, a worker just works. This registry is the seam between the two —
   a role name maps to the panel builders that role's sessions get, and
   renderSession asks it and nothing else. Growing a role's UI is one entry
   here, not another branch in the renderer.

   A session's roles are read from both places one is declared: the
   definition's own role (the stance injected at every spawn) and the role
   each mesh membership carries — the same session is 'leader' in its own
   definition and 'leader' again in the mesh roster, but either alone must
   be enough, because either alone is how operators actually set them. */
const ROLE_PANELS = {
  leader: [sessQuickJob, sessChildren],
};

function sessRoleNames(data) {
  const s = data.session || {};
  const names = new Set();
  const add = (r) => { if (r) names.add(String(r).toLowerCase()); };
  add(data.role ? data.role.name : s.role);
  for (const m of data.meshes || []) add(m.role);
  return names;
}

/* The panels `data`'s session gets, in ROLE_PANELS order, each at most once —
   a session that is 'leader' twice over must not get two dispatch forms. */
function rolePanels(data) {
  const roles = sessRoleNames(data);
  const out = [];
  for (const [role, builders] of Object.entries(ROLE_PANELS)) {
    if (!roles.has(role)) continue;
    for (const b of builders) if (!out.includes(b)) out.push(b);
  }
  return out;
}

/* ---- shared spawn plumbing ----
   The one report, the one verdict and the one POST every spawn surface uses —
   quick job and the spawn modal must not each grow their own reading of the
   policy's answer, or they will drift apart in what they refuse. */
async function spawnReport(parent) {
  return api(`/api/sessions/${encodeURIComponent(parent)}/children`)
    .then((r) => (r.ok ? r.json() : null)).catch(() => null);
}

/* The policy's verdict as one line for a status element: blocked with the
   daemon's own reasons, or the slots still open. `kids` may be null (an old
   daemon, a failed fetch) — then there is nothing to say and nothing to
   forbid; the POST is the backstop. */
function spawnPreflightNote(kids) {
  if (kids && kids.can_spawn === false) {
    return {
      ok: false,
      msg: (kids.blocked_by || []).join("; ") || "this session may not spawn",
      cls: "wf-warning",
    };
  }
  if (kids && typeof kids.children_remaining === "number") {
    // Zero left is not a refusal any more — the cap warns and lets it
    // through — so it gets the daemon's own sentence about the crossing
    // rather than "0 child slot(s) left", which reads as a dead end.
    const soft = (kids.soft_blocked_by || []).join("; ");
    if (!kids.children_remaining && soft) {
      return { ok: true, cls: "wf-warning", msg: soft };
    }
    return {
      ok: true, cls: "wf-note",
      msg: `${kids.children_remaining} child slot(s) left`,
    };
  }
  return { ok: true, msg: "", cls: "wf-note" };
}

/* The blocks that are actually final: spawning switched off, the depth
   ceiling. Those two are all `blocked_by` carries now — the SOFT child cap
   left it when the cap stopped refusing (spawn.py, capabilities()) and lives
   in `soft_blocked_by` alone, where it is a thing to SAY rather than a thing
   to stop a form load for. The subtraction stays anyway, because it costs
   nothing and it is what keeps this form usable against a daemon old enough
   to still fold the cap into both lists — that fold is exactly how a parent
   standing at its child cap once came up with every picker unfilled. */
function spawnHardBlocks(report) {
  const soft = new Set((report && report.soft_blocked_by) || []);
  return ((report && report.blocked_by) || []).filter((b) => !soft.has(b));
}

/* ---- an empty picker is two different facts ----
   Every list in the spawn surfaces is filled from its own fetch, and every
   one of those fetches degrades the same way: `.catch(() => null)`, which
   fills the picker with nothing. But "this daemon offers no workflows" and
   "the workflow list never arrived" look identical in a <select> that has
   only its placeholder — and they are not the same thing at all. The first
   is an answer; the second is a form quietly lying about what you may pick,
   with the Spawn button armed over it. These two name the difference so the
   note can say it out loud. */
function spawnMissingSources(docs) {
  return Object.keys(docs).filter((k) => !docs[k]);
}

function spawnSourceNote(missing) {
  if (!missing.length) return "";
  const many = missing.length > 1;
  return `could not load ${missing.join(", ")} — ` +
    `${many ? "those pickers are" : "that picker is"} empty because the ` +
    `${many ? "lists" : "list"} never arrived, not because there is nothing ` +
    "to offer; reload the page, or check the daemon is still up";
}

async function postSpawn(parent, body) {
  let resp, doc = {};
  try {
    resp = await api(`/api/sessions/${encodeURIComponent(parent)}/children`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    doc = await resp.json().catch(() => ({}));
  } catch {
    return { ok: false, status: 0, doc: {},
             error: "could not reach the daemon — nothing was spawned" };
  }
  if (!resp.ok) {
    return { ok: false, status: resp.status, doc,
             error: doc.error || `spawn refused (HTTP ${resp.status})` };
  }
  return { ok: true, status: resp.status, doc, error: "" };
}

/* ---- the spawn modal's brain ----
   The CLI wizard (SpawnWizard) rebuilt in the browser: the same fields, the
   same gating, the same payload. Split from the DOM so a test can drive the
   rules with stub nodes — everything below reads a `ui` bag of controls
   (each with .value/.checked/.disabled/.hidden) plus the fetched data
   (`report`, `parentSess`, `parentMesh`, `git`, `stamp`) and never touches
   the document. */

/* The daemon publishes qualified PROFILE:HARNESS execution selectors. The
   modal presents their two axes separately, while retaining those selectors
   as the policy-filtered source of truth and recombining the picked pair for
   the unchanged spawn API. */
function normalizeSpawnProfileOptions(raws) {
  const out = [];
  for (const raw of raws || []) {
    const item = raw && typeof raw === "object" ? raw : { value: raw };
    const value = String(item.value || item.name || "").trim();
    const parts = value.split(":", 2);
    const profile = String(item.profile || parts[0] || "").trim();
    const harness = String(item.harness || parts[1] || "").trim();
    if (!profile) continue;
    out.push({
      value: value || (harness ? `${profile}:${harness}` : profile),
      profile,
      harness,
      // A daemon old enough to publish bare profile names only still has a
      // default harness; the empty harness option below preserves that bare
      // request and lets the daemon resolve it as before.
      default: item.default === undefined ? !harness : !!item.default,
      harness_available: item.harness_available,
    });
  }
  return out;
}

function spawnProfileSelector(ui) {
  const picked = String((ui.profile && ui.profile.value) || "").trim();
  // Compatibility for rule tests and clients built before the split: their
  // one profile control still holds the already-qualified selector.
  if (!ui.harness) {
    return picked || String((ui.parentSess || {}).profile || "").trim();
  }
  const profile = picked || baseProfileName((ui.parentSess || {}).profile);
  const harness = String(ui.harness.value || "").trim() ||
    (picked ? "" : String((ui.parentSess || {}).harness || "").trim());
  if (!profile) return "";
  return harness ? `${profile}:${harness}` : profile;
}

function spawnProfileOverride(ui) {
  if (!ui.harness) return (ui.profile && ui.profile.value) || "";
  if (!ui.profile.value && !ui.harness.value) return "";
  return spawnProfileSelector(ui);
}

function refillSpawnHarnesses(ui, want) {
  if (!ui.harness) return;
  const inherited = !ui.profile.value;
  const profile = ui.profile.value || baseProfileName(ui.parentSess.profile);
  const options = (ui._profileOptions || []).filter(
    (item) => item.profile === profile
  );
  const seen = new Set();
  const pairs = [];
  for (const item of options) {
    if (inherited && !item.harness) continue;
    if (seen.has(item.harness)) continue;
    seen.add(item.harness);
    pairs.push([
      item.harness, item.harness || "(profile default)",
      item.harness_available === false,
    ]);
  }

  let chosen = "";
  if (want) {
    // A remembered qualified selector is preserved even when it is no longer
    // offered, so fillSpawnSelect can expose the stale value and the daemon
    // can refuse it explicitly instead of silently changing the request.
    chosen = want;
  } else if (!inherited) {
    const current = want === undefined ? ui.harness.value : "";
    const currentAllowed = options.some((item) => item.harness === current);
    const fallback = options.find((item) => item.default) || options[0];
    chosen = currentAllowed ? current : (fallback ? fallback.harness : "");
  }
  fillSpawnSelect(
    ui.harness,
    pairs,
    inherited ? "(inherit the parent's harness)" :
      (pairs.length ? null : "(no allowed harness)"),
    chosen
  );
}

/* What the modal remembers between spawns — the CLI wizard's recall_fields,
   minus attach (the web has no terminal to take over). BASE-scoped like the
   auth token: daemons behind one relay share this localStorage. */
const SPAWN_RECALL_FIELDS = ["parent", "profile", "borrow", "null_token", "role"];
const SPAWN_RECALL_KEY = `claunch_spawn_recall:${BASE}`;

function spawnRecall() {
  try {
    return JSON.parse(localStorage.getItem(SPAWN_RECALL_KEY) || "{}") || {};
  } catch { return {}; }
}

function saveSpawnRecall(picks) {
  const keep = {};
  for (const k of SPAWN_RECALL_FIELDS) {
    if (picks[k] !== undefined) keep[k] = picks[k];
  }
  try { localStorage.setItem(SPAWN_RECALL_KEY, JSON.stringify(keep)); } catch {}
}

/* The mesh the child would land in: the one picked, or the parent's own.
   "-" is the API's own spelling for "none at all". */
function spawnMeshNow(ui) {
  const picked = ui.mesh.value || "";
  if (picked === "-") return "";
  return picked || ui.parentMesh || "";
}

/* worktree._fragment in the browser: what survives as part of a name. */
function spawnWtFragment(text) {
  return String(text || "").replace(/[^\w.]+/g, "-").replace(/^[-.]+|[-.]+$/g, "");
}

/* worktree.child_name in the browser: `<parent>-<child>-<stamp>`, the two
   sessions the checkout is between plus a stamp fixed once at build — a name
   that ticked over between being shown and being sent would cut a worktree
   nobody read.

   `""` when the child has no name yet, which is the QUICK JOB's normal case:
   the daemon picks the child's `sN`, so nothing here can finish the name, and
   naming the checkout after the parent alone (which is what this used to do)
   is how a repository fills up with `s45-<stamp>` directories that no longer
   say which worker each one belongs to. `spawnPayload` asks the daemon to
   name it instead of guessing. */
function spawnAutoWorktree(ui) {
  const kid = spawnWtFragment((ui.name.value || "").trim());
  if (!kid) return "";
  return `${spawnWtFragment(ui.parent.value) || "child"}-${kid}-${ui.stamp}`;
}

/* What the blank-name placeholder reads: the generated name when this side
   can compute it, and its SHAPE when it cannot. Spelling out an exact string
   the daemon is going to pick differently is worse than naming the hole. */
function spawnAutoWorktreeHint(ui) {
  return spawnAutoWorktree(ui) ||
    `${spawnWtFragment(ui.parent.value) || "child"}-<the child's name>-<time cut>`;
}

/* One workflow the daemon offered, normalized — a bare name (an older
   daemon) reads as a workflow that volunteers for nobody. */
function spawnWorkflowEntry(raw) {
  if (raw && typeof raw === "object") {
    let priority = parseInt(raw.priority, 10);
    if (!Number.isFinite(priority)) priority = 0;
    return {
      name: String(raw.name || ""),
      default_role: String(raw.default_role || "").trim().toLowerCase(),
      priority,
      filter_roles: raw.filter_roles || null,
    };
  }
  return {
    name: String(raw || ""), default_role: "", priority: 0, filter_roles: null,
  };
}

/* Would this workflow's filter_roles let `role` drive it? True with no
   filter or no role. The filter is enforced at start by the daemon; this
   only decides what the form volunteers, exactly like the CLI wizard —
   including refusing to volunteer on a `type` outside the vocabulary. */
function spawnWorkflowAdmits(entry, role) {
  const f = entry.filter_roles;
  if (!f || typeof f !== "object" || !role) return true;
  const kind = String(f.type || "").trim().toLowerCase();
  if (kind !== "whitelist" && kind !== "blacklist") return false;
  const roles = (f.roles || []).map((r) => String(r).trim().toLowerCase());
  const held = roles.includes(String(role).trim().toLowerCase());
  return kind === "whitelist" ? held : !held;
}

/* The wizard's workflow ranking: the picked role's own candidates first,
   then the rest, the ones its filter refuses last — each band by descending
   priority. Returns the ordered options and the auto-pick (the role's
   highest-priority default), for the caller to apply over a value only the
   auto-pick itself set last time. */
function spawnRankWorkflows(raws, role) {
  role = String(role || "").trim().toLowerCase();
  const entries = (raws || []).map(spawnWorkflowEntry).filter((e) => e.name);
  const band = (e) => {
    if (role && e.default_role === role && spawnWorkflowAdmits(e, role)) return 0;
    return spawnWorkflowAdmits(e, role) ? 1 : 2;
  };
  entries.sort((a, b) =>
    band(a) - band(b) || b.priority - a.priority ||
    (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  const options = entries.map((e) => {
    const d = [];
    if (e.default_role) d.push(`default for ${e.default_role}`);
    if (e.priority) d.push(`priority ${e.priority}`);
    if (role && !spawnWorkflowAdmits(e, role)) {
      d.push(`filter_roles turns '${role}' away`);
    }
    return { name: e.name, detail: d.join(", ") };
  });
  const auto = entries.find((e) => band(e) === 0);
  return { options, auto: auto ? auto.name : "" };
}

function syncSpawnCodexRuntime(ui, childHarness, capabilities, may) {
  if (!ui.codexPanel || !ui.codexYolo || !ui.codexSandbox) return;
  const codex = childHarness === "codex";
  ui.codexPanel.hidden = !codex;
  if (!codex) return;

  const parentArgs = childHarness === (ui.parentSess || {}).harness
    ? ((ui.parentSess || {}).args || []) : [];
  const key = `${spawnProfileSelector(ui)}:${childHarness}:` +
    JSON.stringify(parentArgs);
  if (ui._codexRuntimeFor !== key) {
    const state = codexRuntimeState(parentArgs, capabilities);
    ui.codexYolo.checked = state.yolo;
    ui.codexSandbox.checked = state.sandbox;
    ui._codexRuntimeFor = key;
    ui._codexRuntimeOriginal = state;
    ui._codexRuntimeBaseArgs = parentArgs.slice();
  }

  const inherited = !may.includes("args");
  const inheritsParent = childHarness === (ui.parentSess || {}).harness;
  ui.codexYolo.disabled = inherited;
  ui.codexSandbox.disabled = inherited;
  for (const [field, note] of [
    [ui.codexYolo, ui.codexYoloNote],
    [ui.codexSandbox, ui.codexSandboxNote],
  ]) {
    if (!note) continue;
    note.hidden = !inherited;
    note.textContent = inherited
      ? (inheritsParent
        ? `inherited from the parent: ${field.checked ? "enabled" : "disabled"} ` +
          "(spawn.allow_args)"
        : `Codex default: ${field.checked ? "enabled" : "disabled"} ` +
          "(spawn.allow_args to override)")
      : "";
  }
  if (ui.codexState) {
    ui.codexState.textContent = codexRuntimeText(
      ui.codexYolo.checked, ui.codexSandbox.checked
    );
  }
}

/* The wizard's _sync: every dependency between rows, re-derived on every
   change. Locks carry the wizard's own wording — a greyed row says which
   policy key opens it, not just that it is shut. */
function syncSpawnGates(ui) {
  const report = ui.report || {};
  const may = report.may_choose || [];
  const sess = ui.parentSess || {};
  /* A locked row must not still be carrying a yes. spawnPayload reads every
     answer THROUGH its disable, so a tick left standing on a greyed box is
     not a weaker answer -- it is a dropped one, and dropped without a word:
     the operator ticks "start from a copy of the parent's conversation",
     picks a worktree two rows down, and the child boots empty with nothing
     on screen having said the fork went away. That is the failure this
     clears (claunch-409i, measured on session s245 -- its recorded argv
     carries --session-id and no --resume).

     Checkboxes only. A select or a text box keeps what was typed in it,
     which is the same line the borrow and beads rows already draw: words
     somebody typed are theirs to find again when the row comes back, while
     a tick IS the whole answer and has nowhere else to be read from. */
  const lock = (field, note, why) => {
    field.disabled = !!why;
    if (why && field.type === "checkbox") field.checked = false;
    if (note) { note.hidden = !why; note.textContent = why || ""; }
  };

  // Two different folds, because they answer two different questions. The
  // CROSSING is offered only where the daemon named something soft to cross;
  // the GATE around it opens wherever the button is dead for a reason this
  // form is allowed to state (`ui.capped`, set by the load) — including the
  // policy's bare "this session may not spawn", which names no crossing and
  // so opens the gate on its reason alone rather than on an empty box.
  const overCap = !!(report.soft_blocked_by || []).length;
  // `!== false`, not truthiness: the row is built without a `hidden` at all,
  // and an undefined one is a row nobody has shown yet -- which is exactly
  // the way-up this pre-tick is for.
  const capRowWasHidden = ui.overRow.hidden !== false;
  ui.overRow.hidden = !overCap;
  // Pre-answered on the way UP only, like the new-session form's row: the
  // cap warns rather than refusing, so the crossing is the default and the
  // tick is there to be TAKEN AWAY by anyone who wants the strict reading.
  // Re-ticking on every sync would undo that untick.
  if (overCap && capRowWasHidden) ui.over.checked = true;
  if (!overCap) ui.over.checked = false;
  if (ui.capGate) ui.capGate.hidden = !(overCap || ui.capped);

  const pickedProfile = spawnProfileSelector(ui);
  const details = ui.profileDetails || {};
  const pickedDetail = pickedProfile
    ? details[pickedProfile] || details[baseProfileName(pickedProfile)]
    : null;
  const childHarness = String((ui.harness && ui.harness.value) || "") ||
    (pickedDetail ? pickedDetail.harness : (ui.parentSess || {}).harness || "");
  const childCapabilities = (typeof harnessDetails !== "undefined"
    ? harnessDetails[childHarness] : null) || {};
  syncSpawnCodexRuntime(ui, childHarness, childCapabilities, may);
  lock(ui.profile, ui.profileNote, may.includes("profile") ? "" :
    "the child runs under its parent's profile (spawn.allow_profile)");
  if (ui.harness) {
    lock(ui.harness, ui.harnessNote, may.includes("profile") ? "" :
      "the child runs under its parent's harness (spawn.allow_profile)");
  }

  // Null is Claude-only. Borrow follows the selected harness's declared auth
  // capability: Claude borrows token+provider, API-key harnesses borrow only
  // the shared token, OAuth harnesses borrow neither.
  const nonClaude = !!childHarness && childHarness !== "claude";
  const parentProfile = (ui.parentSess || {}).profile || "";
  const effectiveDetail = pickedDetail || details[parentProfile] ||
    details[baseProfileName(parentProfile)];
  const borrowCap = profileBorrowCapability(effectiveDetail, childHarness);
  if (nonClaude) {
    lock(ui.nullTok, ui.nullNote, "the claude harness only");
  } else {
    lock(ui.nullTok, ui.nullNote, "");
  }
  if (ui._borrowValidationError) {
    ui.borrow.value = "";
    lock(ui.borrow, ui.borrowNote, ui._borrowValidationError);
  } else if (!borrowCap.allowed) {
    ui.borrow.value = "";
    lock(ui.borrow, ui.borrowNote,
      `harness ${childHarness || "?"} keeps auth in its own profile storage`);
  } else if (!nonClaude && ui.nullTok.checked) {
    ui.borrow.value = "";
    lock(ui.borrow, ui.borrowNote, "--null launches without any token");
  } else {
    lock(ui.borrow, ui.borrowNote, may.includes("borrow") ? "" :
      "the child authenticates as its parent does (spawn.allow_profile)");
  }

  // Absent, not empty, when the policy has it locked: the report only
  // lists workspaces when a child may be sent to one.
  lock(ui.workspace, ui.workspaceNote, report.workspaces != null ? "" :
    "the child inherits its parent's directory (spawn.allow_workspace)");
  lock(ui.args, ui.argsNote, may.includes("args") ? "" :
    "the child runs its parent's args (spawn.allow_args)");

  // The worktree rows: a locked row greys every mode with the key that opens
  // it, while a directory that is no repository takes the rows away entirely.
  const git = ui.git || {};
  if (!may.includes("worktree")) {
    ui.wtRow.hidden = false;
    lock(ui.wtMode, ui.worktreeNote,
      "a child inherits its parent's directory (spawn.allow_worktree)");
  } else {
    ui.wtRow.hidden = !git.repo;
    lock(ui.wtMode, ui.worktreeNote, "");
  }
  const wtDead = ui.wtRow.hidden || ui.wtMode.disabled;
  // Reuse is an answer only where there is something to reuse. Greying the
  // one mode — rather than offering it over an empty picker — is the whole
  // reason the row is three radios and not a <select>.
  const reusable = (git.worktrees || []).length > 0;
  if (typeof ui.wtMode.enable === "function") {
    ui.wtMode.enable("existing", reusable);
  }
  const mode = ui.wtMode.value || "";
  ui.wtNameRow.hidden = wtDead || mode !== "new";
  // The name a blank field would cut, spelt out: the operator reads the
  // generated name instead of pressing Spawn to discover it -- or, when the
  // child is not named on this form either, its shape, because the session
  // name in the middle of it is the daemon's to pick.
  ui.wtName.placeholder = `blank = ${spawnAutoWorktreeHint(ui)}`;
  ui.wtPickRow.hidden = wtDead || mode !== "existing";
  // Only a REUSED checkout can be behind: a new one is cut from the
  // repository as it stands, so there is nothing to catch up on.
  ui.updateRow.hidden = ui.wtPickRow.hidden;
  ui.rebaseRow.hidden = ui.updateRow.hidden || !ui.update.checked;

  // Forking needs a parent holding a claude conversation AND a child that
  // stays in the directory it was held in — claude keeps transcripts per
  // directory, so a workspace or a worktree of its own would leave the
  // child opening a conversation that is not there.
  const elsewhere =
    (!ui.workspace.disabled && ui.workspace.value) ? "a workspace"
      : (!wtDead && mode) ? "a worktree of its own" : "";
  if (nonClaude) {
    lock(ui.fork, ui.forkNote, "the claude harness only");
  } else if (!may.includes("fork")) {
    lock(ui.fork, ui.forkNote, "the parent has no claude conversation to copy");
  } else if (elsewhere) {
    lock(ui.fork, ui.forkNote,
      `the child runs in ${elsewhere}, and claude keeps transcripts per directory`);
  } else {
    lock(ui.fork, ui.forkNote, "");
  }

  // Inheriting is only ambiguous upward: several meshes and the daemon will
  // refuse the spawn by name. Saying it here costs the operator one read
  // instead of one failed spawn — and the picker stays live, because naming
  // one is exactly the fix.
  if (ui.meshNote) {
    const several = (ui.parentMeshes || []).length > 1;
    const ambiguous = several && !ui.mesh.value;
    ui.meshNote.hidden = !ambiguous;
    ui.meshNote.textContent = ambiguous
      ? `the parent is in ${ui.parentMeshes.length} meshes `
        + `(${ui.parentMeshes.join(", ")}) — name the one this child belongs `
        + `in, or pick (none)`
      : "";
  }

  const noMesh = ui.mesh.value === "-";
  ui.handleRow.hidden = noMesh;
  ui.connectRow.hidden = noMesh || !(ui.connectHandles || []).length;
  ui.contextRow.hidden = !ui.workflow.value;
  syncSpawnBeads(ui);
}

/* The board row: which of its three answers is picked decides which of the
   two detail rows exists, and only "existing" is allowed to ask what the
   board holds — the fetch costs the daemon a `br` fork, so it happens once
   somebody picks the one answer that looks at it (see refreshSpawnBeads).

   Every row carries the daemon's own verdict on it (daemon/beads.adoption):
   an issue nobody holds would be ASSIGNED to the child, one a running
   session holds would be JOINED and the assignment left where it is. Saying
   so here rather than in the child's opening block is the whole point of
   the row — by then the choice has been made.

   Optional like meshNote: a ui bag that predates the row (a test driving
   only the older fields) must still pass through the gates. */
function syncSpawnBeads(ui) {
  if (!ui.beads) return;
  const picking = ui.beads.value === "existing";
  // Hidden, not cleared: somebody who types a specification, tries the other
  // two answers and comes back should find their words where they left them.
  ui.issueTextRow.hidden = ui.beads.value !== "new";
  ui.issueRow.hidden = !picking;
  const hint = ui.issueHint;
  // Only the consequence a reader cannot see from the row is written out:
  // an issue that would simply be assigned needs no warning.
  const row = picking
    ? (ui._issues || []).find((i) => i.id === ui.issuePick.value)
    : null;
  if (row && row.held_by) {
    hint.textContent =
      `${row.held_by} is assigned to ${row.id} and still running — this ` +
      "session JOINS it: the assignment stays put and the two settle " +
      "ownership between them.";
    hint.hidden = false;
  } else if (picking && ui._issuesRead && !(ui._issues || []).length) {
    // Only once the board has actually answered: an empty list held while
    // the fetch is still in flight would read as "this board has nothing",
    // which is a different and wrong thing to tell somebody.
    hint.textContent = ui._issuesError ||
      "no open issue on this directory's board.";
    hint.hidden = false;
  } else {
    hint.hidden = true;
  }
}

/* The POST body, in the CLI's spelling: non-falsy keys only, and read
   THROUGH the disables like SpawnWizard.apply — a value standing on a
   greyed row is not an answer the user gave, and sending it provokes a 403
   naming a field nobody in this form could still choose. */
function spawnPayload(ui) {
  const body = {};
  const put = (k, v) => { if (v) body[k] = v; };
  put("name", (ui.name.value || "").trim());
  // Read through the hidden flag: a yes given while the row was shown, on a
  // parent that then changed to one with slots free, must not travel.
  // Both answers travel, from a VISIBLE row only. The `false` is the one
  // that does something now — it asks for the refusal the cap no longer
  // gives by default — so it cannot ride the falsy-dropping `put` below.
  if (!ui.overRow.hidden) body.over_limit = !!ui.over.checked;
  if (!ui.profile.disabled && (!ui.harness || !ui.harness.disabled)) {
    put("profile", spawnProfileOverride(ui));
  }
  if (!ui.borrow.disabled) put("borrow", ui.borrow.value);
  if (!ui.nullTok.disabled && ui.nullTok.checked) body.null_token = true;
  if (!ui.fork.disabled && ui.fork.checked) body.fork = true;
  const typedArgs = !ui.args.disabled && (ui.args.value || "").trim()
    ? ui.args.value.trim().split(/\s+/) : [];
  const codexOpen = ui.codexPanel && !ui.codexPanel.hidden &&
    ui.codexYolo && !ui.codexYolo.disabled;
  if (codexOpen) {
    const original = ui._codexRuntimeOriginal || { yolo: true, sandbox: false };
    const changed = ui.codexYolo.checked !== original.yolo ||
      ui.codexSandbox.checked !== original.sandbox;
    if (typedArgs.length || changed) {
      const selector = spawnProfileSelector(ui);
      const detail = (ui.profileDetails || {})[selector] ||
        (ui.profileDetails || {})[baseProfileName(selector)] || {};
      const harnessName = String((ui.harness && ui.harness.value) || "") ||
        detail.harness || (ui.parentSess || {}).harness || "";
      const capabilities = (typeof harnessDetails !== "undefined"
        ? harnessDetails[harnessName] : null) || {};
      body.args = codexRuntimeArgs(
        typedArgs.length ? typedArgs : (ui._codexRuntimeBaseArgs || []),
        capabilities,
        !!ui.codexYolo.checked,
        !!ui.codexSandbox.checked
      );
    }
  } else if (typedArgs.length) {
    body.args = typedArgs;
  }
  // Both travel as NAMES, never paths: the workspace is what the API
  // resolves, and the child's worktree is cut by the daemon from the
  // parent's own repository.
  if (!ui.workspace.disabled) put("workspace", ui.workspace.value);
  // The three modes collapse back into the ONE key the API has: a name.
  // "new" with a blank field is the generated one; "existing" travels as the
  // checkout it names, and only that mode may carry a rebase.
  const mode = ui.wtMode.value || "";
  if (!ui.wtRow.hidden && !ui.wtMode.disabled && mode) {
    if (mode === "new") {
      // A typed name is the whole name. Blank is the generated one — spelled
      // out here when the child is named on this form (so what the
      // placeholder read is what gets cut), and otherwise handed to the
      // daemon as `true`, the only side that knows the child's session name.
      body.worktree =
        (ui.wtName.value || "").trim() || spawnAutoWorktree(ui) || true;
    } else if (mode === "existing" && ui.wtPick.value) {
      body.worktree = ui.wtPick.value;
      if (!ui.rebaseRow.hidden && ui.rebase.value) {
        body.rebase_onto = ui.rebase.value;
      }
    }
  }
  const mesh = ui.mesh.value || "";
  put("mesh", mesh);   // "" = inherit the parent's; "-" travels, meaning none
  if (mesh !== "-") {
    put("handle", (ui.handle.value || "").trim());
    const conn = (ui.connect ? ui.connect() : []).filter(Boolean);
    if (conn.length) body.connect = conn;
  }
  put("role", ui.role.value);
  // "" is not silence here: the daemon reads an absent workflow as "give the
  // child the pair my run declares", so a row the operator cleared has to say
  // no out loud — otherwise the form hands back the very run it was used to
  // take away. Only worth saying when there is a pair to refuse.
  const paired = ((ui.report || {}).child_cflow) || "";
  if (ui.workflow.value) {
    put("workflow", ui.workflow.value);
    put("context", (ui.context.value || "").trim());
  } else if (paired) {
    body.workflow = "-";
  }
  // The board answer, the same contract as the create form's: "new" with
  // an empty box sends nothing — a request that says nothing gets an issue
  // minted from the task, which is what every client that has never heard
  // of this field still wants. Only the key of the answer PICKED travels:
  // issue_text beside "existing" or "none" is a contradiction the daemon
  // refuses (beads.check_request).
  if (ui.beads) {
    const mode = ui.beads.value || "new";
    if (mode === "none") body.beads = false;
    else if (mode === "existing" && ui.issuePick.value) {
      body.issue = ui.issuePick.value;
    } else if (mode === "new" && (ui.issueText.value || "").trim()) {
      body.issue_text = ui.issueText.value.trim();
    }
  }
  put("task", (ui.task.value || "").trim());
  return body;
}

/* ---- the spawn modal itself ----
   The wizard as a dialog. Any session may be a parent, and the three ways a
   spawn starts — the rail's +, the detail panel's Spawn button, and the
   leader's quick job — all land here. The modal owns the fetch and the POST;
   the brain above owns the rules. Built as DOM over the shared #modal-overlay
   (showModal's body is text; a form is not), and closed the same way it is
   opened: the backdrop, Escape, or the Cancel button. */

function spawnRow(label, control, note) {
  const wrap = el("div", "sess-spawn-row");
  wrap.appendChild(el("label", "sess-spawn-label", label));
  wrap.appendChild(control);
  if (note) {
    // THE note element, not a fresh one: syncSpawnGates writes the policy's
    // "which key unlocks this" line straight onto it, so the caller hands the
    // element in and keeps its reference for exactly that purpose. A plain
    // string is tolerated — it becomes a span, read but not referenced.
    const n = typeof note === "string" ? el("span", "sess-spawn-note", note) : note;
    n.hidden = true;
    wrap.appendChild(n);
  }
  return wrap;
}

/* A row that belongs to the answer above it rather than to the form. Same
   grid, one class more: the stylesheet indents it and hangs it off the
   answer it details, so a fold-out is read as part of its mode and not as
   the next question. */
function spawnSubRow(label, control, note) {
  const wrap = spawnRow(label, control, note);
  wrap.classList.add("sess-spawn-sub");
  return wrap;
}

function spawnCheckRow(label, note) {
  const inp = document.createElement("input");
  inp.type = "checkbox";
  const lab = el("label", "check sess-spawn-check");
  lab.append(inp, el("span", null, label));
  const wrap = el("div", "sess-spawn-row");
  wrap.appendChild(lab);
  if (note) {
    const n = el("span", "sess-spawn-note");
    n.hidden = true;
    wrap.appendChild(n);
  }
  return wrap;
}

/* ---- a radio group the gates can drive like a <select> ------------------
   Three answers that are not one list. The worktree row used to be a single
   picker holding "(no worktree)", "@auto", "@named" and every checkout the
   repository already has — four different KINDS of answer racked as if they
   were four values of one, so the reader had to open the popup to find out
   that two of them were modes and the rest were names. Radios say the three
   modes on the face of the form and leave the detail of each to its own
   sub-rows.

   The rest of the wizard must not learn a second widget for that, so the
   group answers to `.value` and `.disabled` exactly as the <select> did:
   syncSpawnGates' `lock()` writes `.disabled`, spawnPayload reads `.value`,
   and neither knows the difference. `enable(v, on)` is the one thing a
   <select> could not do — greying ONE answer (there is nothing to reuse in a
   repository with no worktrees) while the others stay live. */
function spawnRadioGroup(name, items) {
  const wrap = el("div", "sess-spawn-radios");
  const inputs = {};
  for (const [value, label, hint] of items) {
    const inp = document.createElement("input");
    inp.type = "radio";
    inp.name = name;
    inp.value = value;
    const lab = el("label", "check sess-spawn-radio");
    lab.append(inp, el("span", null, label));
    if (hint) lab.appendChild(el("span", "sess-spawn-hint", hint));
    wrap.appendChild(lab);
    inputs[value] = inp;
  }
  const keys = () => Object.keys(inputs);
  const off = {};          // per-answer greying, on top of the row's own
  const group = {
    el: wrap, inputs,
    get value() {
      for (const k of keys()) if (inputs[k].checked) return k;
      return "";
    },
    set value(v) {
      // An unknown answer falls back to the FIRST, which is why "no worktree"
      // is declared first: clearing the group must land on the harmless one.
      const want = inputs[v] ? v : keys()[0];
      for (const k of keys()) inputs[k].checked = (k === want);
    },
    get disabled() { return !!group._off; },
    set disabled(v) {
      group._off = !!v;
      for (const k of keys()) inputs[k].disabled = !!v || !!off[k];
    },
    /* Grey one answer. A greyed answer that was the current one is dropped
       rather than left standing: a checked radio nobody can uncheck would
       send a value the form is telling the operator they may not have. */
    enable(v, on) {
      off[v] = !on;
      if (!inputs[v]) return;
      inputs[v].disabled = !on || !!group._off;
      if (!on && group.value === v) group.value = "";
    },
    /* One handler over the three buttons. The browser unchecks the siblings
       itself (they share `name`); the assignment repeats that so a stub DOM —
       and any node that drifted out of the group — reads the same. */
    listen(fn) {
      for (const k of keys()) {
        inputs[k].addEventListener("change", () => {
          if (inputs[k].checked) group.value = k;
          fn(group.value);
        });
      }
    },
  };
  group.value = "";
  return group;
}

function fillSpawnSelect(sel, pairs, noneLabel, want) {
  sel.innerHTML = "";
  if (noneLabel !== null) {
    const o = document.createElement("option");
    o.value = "";
    o.textContent = noneLabel;
    sel.appendChild(o);
  }
  for (const [v, t, disabled] of pairs) {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = t;
    if (disabled) o.disabled = true;
    sel.appendChild(o);
  }
  if (want === undefined || want === null) want = "";
  const present = [...sel.options].some((o) => o.value === want);
  if (present) sel.value = want;
  else if (want) {
    const o = document.createElement("option");
    o.value = want;
    o.textContent = `${want} (not found here)`;
    sel.appendChild(o);
    sel.value = want;
  }
  return sel;
}

/* The workflow picker, ranked by the picked role the way the CLI wizard
   ranks it: the role's own defaults first, then the rest, the ones its filter
   refuses last. The current selection is carried over UNLESS it was the
   auto-pick — then it follows the pair, so a role switch re-ranks the list
   without trampling a pick the operator made. `last` is the auto value the
   caller last applied; it is returned so the caller can remember it.

   What is auto-picked is NOT the role's default, though: this form makes a
   CHILD, and a child's run comes from the pair its parent's own run declares
   (`child_cflow`, the daemon's reading of `default_child_cflow`). The role
   only ranks the list. A parent that pairs with nothing preselects nothing —
   that "" is an answer, not a missing one, which is why it is not fallen
   back from: reading the child's run off its role is exactly what handed a
   worker-role child the worker flow under a parent driving something else. */
function refillSpawnWorkflows(ui, role, last) {
  const { options } = spawnRankWorkflows(
    ui._wfs || [], role
  );
  const auto = ((ui.report || {}).child_cflow) || "";
  const want = ui.workflow.value === last ? auto
    : (ui.workflow.value || auto);
  fillSpawnSelect(ui.workflow,
    options.map((o) => [o.name, o.detail ? `${o.name} — ${o.detail}` : o.name]),
    "(no workflow)", want);
  return { auto };
}

/* The field the child would be its own name in the picked mesh — excluded
   from the connect list along with the parent's own handle, both of which are
   wired by the mesh join itself. */
function spawnConnectNow(ui, handles) {
  const mine = (ui.handle.value || "").trim();
  return handles.filter(
    (h) => h !== mine && h !== ui.parentSess._meshHandle
  );
}

function buildSpawnForm(parentName, seed) {
  seed = seed || {};
  const rec = spawnRecall();
  const box = el("div", "sess-spawn");
  const st = {
    parent: parentName,
    parentSess: {}, parentMesh: "", parentMeshes: [], report: {}, git: {},
    _meshHandle: null, _wfs: [], stamp: qjStamp(),
    connectHandles: [], lastWfAuto: "",
  };
  const ui = st;

  // The parent is not a control: every entry point pins it, and relocating
  // the child means reopening from another row. Its facts open the form.
  ui.parent = { value: parentName };
  const parentLine = el("p", "sess-spawn-parent",
    `child of ${parentName} — inherits its harness, profile, directory and args`);
  box.appendChild(parentLine);

  ui.note = el("p", "wf-note hidden");
  box.appendChild(ui.note);
  const noteShow = (msg, cls) => {
    ui.note.className = cls || "wf-note";
    ui.note.textContent = msg;
  };
  ui.noteShow = noteShow;

  ui.name = document.createElement("input");
  ui.name.placeholder = "child name (blank = auto)";
  box.appendChild(spawnRow("Name", ui.name, null));

  ui.role = document.createElement("select");
  box.appendChild(spawnRow("Role", ui.role, null));

  /* start it working, in the order the child experiences them */
  ui.workflow = document.createElement("select");
  box.appendChild(spawnRow("Workflow", ui.workflow, null));
  ui.contextRow = spawnRow("Context", (ui.context = document.createElement("input")), null);
  ui.contextRow.hidden = true;
  box.appendChild(ui.contextRow);

  ui.mesh = document.createElement("select");
  box.appendChild(spawnRow("Mesh", ui.mesh, (ui.meshNote = el("span", "sess-spawn-note"))));
  ui.handleRow = spawnRow("Handle", (ui.handle = document.createElement("input")), null);
  ui.handleRow.hidden = true;
  box.appendChild(ui.handleRow);
  ui.connectRow = el("div", "sess-spawn-row sess-spawn-connect");
  box.appendChild(ui.connectRow);
  ui.connect = () => spawnConnectNow(ui, ui._connectChecked || []);

  ui.task = document.createElement("textarea");
  ui.task.rows = 3;
  ui.task.placeholder = "opened with this once it has booted — what it is for";
  box.appendChild(spawnRow("Opening task", ui.task, null));

  /* The board row, after the opening task because it is still the fallback
     two of its three shapes are read off: "new" mints from the box below
     when that is filled and from the task when it is not, so an empty pair
     has nothing to mint from. The third — an issue that already exists —
     is a picker rather than a text box for the same reason every other row
     here is: the daemon publishes the list, and an id it does not have
     would be a refusal nobody needed to provoke. The child's issue is its
     own answer — not something the spawn policy decides — so the row
     stands ungated, exactly like the create form's #new-beads. */
  ui.beads = spawnRadioGroup("spawn-beads", [
    ["new", "new issue", "minted from the task or the box below"],
    ["existing", "an existing issue", "assigned, or joined while its holder is running"],
    ["none", "no issue", "the child starts without a board record"],
  ]);
  ui.beads.value = "new";
  box.appendChild(spawnRow("Board issue", ui.beads.el, null));
  ui.issueText = document.createElement("textarea");
  ui.issueText.rows = 3;
  ui.issueText.placeholder =
    "what the issue says — first line is its title; empty uses the opening task";
  ui.issueTextRow = spawnSubRow("Issue text", ui.issueText, null);
  ui.issueTextRow.hidden = true;
  box.appendChild(ui.issueTextRow);
  ui.issuePick = document.createElement("select");
  ui.issueHint = el("span", "sess-spawn-note");
  ui.issueRow = spawnSubRow("Issue", ui.issuePick, ui.issueHint);
  ui.issueRow.hidden = true;
  box.appendChild(ui.issueRow);
  ui._issues = [];
  ui._issuesFor = null;
  ui._issuesError = "";
  ui._issuesRead = false;

  /* the inherited rows: what a child may be told to differ on */
  ui.profile = document.createElement("select");
  box.appendChild(spawnRow("Profile", ui.profile, (ui.profileNote = el("span", "sess-spawn-note"))));
  ui.harness = document.createElement("select");
  box.appendChild(spawnRow("Harness", ui.harness, (ui.harnessNote = el("span", "sess-spawn-note"))));
  ui.borrow = document.createElement("select");
  box.appendChild(spawnRow("Borrow", ui.borrow, (ui.borrowNote = el("span", "sess-spawn-note"))));
  ui.nullTok = null; ui.nullNote = null;
  const nullRow = spawnCheckRow("run with no token (--null — log in inside)", null);
  ui.nullTok = nullRow.querySelector("input");
  ui.nullNote = nullRow.querySelector(".sess-spawn-note");
  box.appendChild(nullRow);
  ui.args = document.createElement("input");
  ui.args.placeholder = "extra harness flags";
  box.appendChild(spawnRow("Args", ui.args, (ui.argsNote = el("span", "sess-spawn-note"))));

  /* Codex has a named runtime panel rather than generic permission rows.
     Other harnesses do not acquire Codex labels merely because their
     declaration exposes a similar argv capability. */
  ui.codexPanel = document.createElement("fieldset");
  ui.codexPanel.className = "sess-spawn-harness sess-spawn-codex";
  ui.codexPanel.hidden = true;
  ui.codexPanel.appendChild(el("legend", null, "Codex runtime"));
  const yoloRow = spawnCheckRow("YOLO mode — skip approval prompts", true);
  ui.codexYolo = yoloRow.querySelector("input");
  ui.codexYolo.checked = true;
  ui.codexYoloNote = yoloRow.querySelector(".sess-spawn-note");
  const sandboxRow = spawnCheckRow(
    "Sandbox — limit writes to the workspace", true
  );
  ui.codexSandbox = sandboxRow.querySelector("input");
  ui.codexSandbox.checked = false;
  ui.codexSandboxNote = sandboxRow.querySelector(".sess-spawn-note");
  ui.codexState = el("p", "sess-spawn-harness-state");
  ui.codexPanel.append(yoloRow, sandboxRow, ui.codexState);
  box.appendChild(ui.codexPanel);

  ui.workspace = document.createElement("select");
  box.appendChild(spawnRow("Directory", ui.workspace, (ui.workspaceNote = el("span", "sess-spawn-note"))));

  /* worktree: the daemon cuts it from the parent's repository.
     Three modes on the face of the form, each with its own sub-rows folded
     under it — the name for a fresh checkout, the picker and its catch-up
     for a reused one. Only the picked mode's sub-rows are on screen, so what
     is asked is never a question about a mode nobody chose. */
  ui.worktreeNote = el("span", "sess-spawn-note");
  ui.wtMode = spawnRadioGroup("spawn-wt", [
    ["", "no worktree", "the child runs in the parent's directory"],
    ["new", "new worktree", "cut fresh from this repository, on a branch of its own"],
    ["existing", "existing worktree", "reuse a checkout that is already here"],
  ]);
  ui.wtRow = spawnRow("Worktree", ui.wtMode.el, ui.worktreeNote);
  ui.wtName = document.createElement("input");
  ui.wtNameRow = spawnSubRow("Name", ui.wtName, null);
  ui.wtNameRow.hidden = true;
  ui.wtPick = document.createElement("select");
  ui.wtPickRow = spawnSubRow("Reuse", ui.wtPick, null);
  ui.wtPickRow.hidden = true;
  ui.update = null; ui.updateRow = null;
  const updRow = spawnCheckRow("bring the reused checkout up to date", null);
  updRow.classList.add("sess-spawn-sub");
  ui.update = updRow.querySelector("input");
  ui.updateRow = updRow;
  ui.updateRow.hidden = true;
  ui.rebase = document.createElement("input");
  ui.rebase.placeholder = "branch to fold this reused checkout onto";
  ui.rebaseRow = spawnSubRow("Rebase onto", ui.rebase, null);
  ui.rebaseRow.hidden = true;
  box.append(ui.wtRow, ui.wtNameRow, ui.wtPickRow, updRow, ui.rebaseRow);

  ui.fork = null; ui.forkNote = null;
  // A note is asked for here because this box is greyed more often than it is
  // offered: a non-claude child, a parent holding no conversation, and — the
  // one an operator meets first — the "new worktree" this form opens on, which
  // puts the child in a directory claude keeps no transcript of. syncSpawnGates
  // has the wording for all three; without the element it wrote them nowhere,
  // and the row went grey saying nothing at all.
  const forkRow = spawnCheckRow("start from a copy of the parent's conversation", true);
  ui.fork = forkRow.querySelector("input");
  ui.forkNote = forkRow.querySelector(".sess-spawn-note");
  box.appendChild(forkRow);

  /* ---- the child cap: laid out at the press, not inside the form -------
     The cap is not a property of the child being described — it is a gate on
     the button — and it used to be laid out as if it were neither. Its three
     parts stood in three places: the reason ("child limit reached (4/4)") in
     the note at the TOP of a 21-row form, the crossing that revives the
     button as the LAST row of that form, and the dead button itself out in
     #modal-actions, which is not even inside the form's scroller. So the
     operator read a warning, found the button under it dead, and had twenty
     rows to scroll before meeting the box the warning had named — which is
     the mismatch this row is being moved to end. Reason and crossing are
     built here as ONE block, and openSpawnModal hangs it in the action bar
     on the line above Spawn, where the button they explain actually is. */
  ui.capNote = el("p", "sess-spawn-cap-msg");
  // spawnCheckRow's second argument is a request for a note element, not its
  // text; the cost of the crossing hangs there, under the action it costs,
  // instead of riding inside the label as parentheses nobody reads.
  ui.overRow = spawnCheckRow("spawn over the child limit", true);
  ui.over = ui.overRow.querySelector("input");
  ui.overNote = ui.overRow.querySelector(".sess-spawn-note");
  ui.overNote.textContent = "the daemon counts it against you";
  ui.overNote.hidden = false;
  ui.capGate = el("div", "sess-spawn-cap");
  ui.capGate.hidden = true;
  ui.capGate.append(ui.capNote, ui.overRow);

  // Seed: a quick-job launch fingers role/workflow/worktree/task; everything
  // else falls back to what the browser used last, then the wizard's defaults.
  if (seed.stamp) ui.stamp = seed.stamp;
  ui.name.value = seed.name || "";
  ui.task.value = seed.task || "";
  ui.args.value = (seed.args || []).join(" ");
  ui.nullTok.checked = !!(seed.null_token ?? rec.null_token);
  return { box, ui, noteShow };
}

/* ---- the box's remembered size --------------------------------------- */
/* The spawn form is a 21-row form in a box sized for a paragraph, so how much
   of it is on screen at once is the operator's call. The grip itself is the
   stylesheet's (`resize: both`); what belongs here is REMEMBERING where they
   left it, on the same contract the rail and detail bars keep: clamped on the
   way in, written down once, scoped by BASE because daemons behind one relay
   share this localStorage.

   Native resize writes INLINE width/height, and #modal-overlay's .modal-box is
   one element shared with the confirm dialogs — 460px of prose that must not
   inherit a 900px form's drag. So the inline pair is applied when the spawn
   modal takes the box and stripped when it gives it back; the sheet's own
   width rules the confirm dialogs again the moment it closes. */
const SPAWN_SIZE_KEY = `claunch_spawnsize:${BASE}`;
const SPAWN_W_MIN = 420;   // the stylesheet's floor, mirrored so JS clamps alike
const SPAWN_H_MIN = 240;
const spawnWMax = () => Math.max(SPAWN_W_MIN, window.innerWidth - 32);
const spawnHMax = () => Math.max(SPAWN_H_MIN, Math.round(window.innerHeight * 0.88));

/* A remembered size is only as good as the window it is restored into: the
   operator may have dragged it wide on a monitor they are no longer at. */
function clampSpawnSize(size) {
  if (!size || !Number.isFinite(size.w) || !Number.isFinite(size.h)) return null;
  return {
    w: Math.min(spawnWMax(), Math.max(SPAWN_W_MIN, Math.round(size.w))),
    h: Math.min(spawnHMax(), Math.max(SPAWN_H_MIN, Math.round(size.h))),
  };
}

function spawnSizeRecall() {
  try {
    return clampSpawnSize(JSON.parse(localStorage.getItem(SPAWN_SIZE_KEY) || "null"));
  } catch { return null; }   // a hand-edited or half-written row is not a size
}

function spawnSizeApply(box) {
  const size = spawnSizeRecall();
  if (!size) return;
  box.style.width = `${size.w}px`;
  box.style.height = `${size.h}px`;
}

/* Read at close rather than watched while dragging: there is no resize EVENT
   on an element, and the alternative — a ResizeObserver — would be a listener
   to own and unhook for a value nobody needs until the box shuts. */
function spawnSizeRemember(box) {
  const r = box.getBoundingClientRect();
  // A box that is not laid out measures 0x0, and the stylesheet's floors mean
  // a VISIBLE spawn box can never measure under them. So a measurement below
  // the floor is not a small size the operator chose — it is no size at all,
  // and clamping it up to the floor would silently shrink the modal they had.
  // Nothing is written down; the size they last chose stands.
  if (r.width >= SPAWN_W_MIN && r.height >= SPAWN_H_MIN) {
    const size = clampSpawnSize({ w: r.width, h: r.height });
    if (size) {
      try { localStorage.setItem(SPAWN_SIZE_KEY, JSON.stringify(size)); } catch { /* full or blocked */ }
    }
  }
  box.style.width = "";
  box.style.height = "";
}

/* ---- open / load / go / close ---------------------------------------- */
let spawnModal = null;

function spawnModalKey(e) { if (e.key === "Escape") spawnModalClose(); }

function spawnModalClose() {
  if (!spawnModal) return;
  spawnModal = null;
  const overlay = $("modal-overlay");
  // Before the class goes: the size is read off the box while the spawn rules
  // still apply to it, and the inline pair is stripped so the next confirm
  // dialog opens at the sheet's 460px rather than at this form's drag.
  spawnSizeRemember(overlay.querySelector(".modal-box"));
  overlay.classList.add("hidden");
  overlay.classList.remove("spawn-open");
  const body = $("modal-body");
  body.innerText = "";
  $("modal-actions").innerHTML = "";
  document.removeEventListener("keydown", spawnModalKey);
}

/* Borrow candidates depend on the effective Profile : Harness selection.
   The daemon validates each base profile against both sides' allow-lists and
   current credential state. Rebuild the list whenever that execution
   selector changes; a remembered lender is retained only when the fresh
   verdict still marks it selectable. */
async function refreshSpawnBorrowOptions(st, force = false) {
  const ui = st.ui;
  const selector = spawnProfileSelector(ui);
  const details = ui.profileDetails || {};
  const detail = details[selector] || details[baseProfileName(selector)];
  const childHarness = String((ui.harness && ui.harness.value) || "") ||
    (detail && detail.harness) ||
    (ui.parentSess || {}).harness || "";
  const borrowCap = profileBorrowCapability(detail, childHarness);
  const ownLabel = borrowCap.allowed
    ? `(as ${st.parent} authenticates)`
    : profileOwnAuthLabel(selector, childHarness);
  // A borrow-capable child keeps the parent's auth on the empty answer, so the
  // selected profile's OWN token rides as a separate head option. An OAuth
  // child has one empty answer, labelled as its selected profile login above.
  const ownName = borrowCap.allowed ? baseProfileName(selector) : "";
  const key = `${selector}|${ownLabel}`;
  if (!force && ui._borrowFor === key) return;
  ui._borrowFor = key;
  const seq = (ui._borrowSeq || 0) + 1;
  ui._borrowSeq = seq;
  const current = ui._borrowPreset || ui.borrow.value || "";

  // Do not leave a lender from the previous harness selectable while the
  // matching policy verdict is in flight.
  fillValidatedBorrow(ui.borrow, { options: [] }, ownLabel, "", "", ownName);
  ui.borrow.disabled = true;
  ui._borrowValidationError = "";
  try {
    const doc = await readBorrowOptions(selector);
    if (spawnModal !== st || ui._borrowSeq !== seq || ui._borrowFor !== key) return;
    fillValidatedBorrow(ui.borrow, doc, ownLabel, current, "", ownName);
  } catch (e) {
    if (spawnModal !== st || ui._borrowSeq !== seq || ui._borrowFor !== key) return;
    ui._borrowValidationError =
      `borrow validation unavailable: ${e.message || e}`;
    fillValidatedBorrow(ui.borrow, { options: [] }, ownLabel, "", "", ownName);
    ui.borrow.title = ui._borrowValidationError;
  }
  ui._borrowPreset = "";
  syncSpawnGates(ui);
}

async function openSpawnModal(parentName, opts = {}) {
  if (!parentName) return;
  const { box, ui, noteShow } = buildSpawnForm(parentName, opts.seed || null);
  const overlay = $("modal-overlay");
  $("modal-title").textContent = `Spawn a child of ${parentName}`;
  const body = $("modal-body");
  body.innerText = "";
  body.appendChild(box);
  const actions = $("modal-actions");
  actions.innerHTML = "";
  const spawnBtn = el("button", "wf-btn approve",
                      (opts.seed && opts.seed.quick) ? "Spawn worker" : "Spawn child");
  const cancel = el("button", "wf-btn option", "Cancel");
  spawnBtn.disabled = true;
  // The cap gate first, so DOM order is reading order: the stylesheet gives
  // it the whole first line of the bar and the buttons keep the second. It
  // stays folded until syncSpawnGates finds a soft block to open it for.
  actions.append(ui.capGate, cancel, spawnBtn);
  const st = { ui, parent: parentName, seed: opts.seed || null,
               spawnBtn, noteShow, busy: false };
  cancel.addEventListener("click", spawnModalClose);
  spawnBtn.addEventListener("click", () => spawnModalGo(st));
  overlay.onclick = (e) => { if (e.target === overlay) spawnModalClose(); };
  document.addEventListener("keydown", spawnModalKey);
  overlay.classList.remove("hidden");
  overlay.classList.add("spawn-open");
  spawnSizeApply(overlay.querySelector(".modal-box"));
  spawnModal = st;
  await spawnModalLoad(st);
}

/* Everything the form can be, fetched in two rounds: the parent's own facts
   first (its cwd is what the workflow and git questions are about), then the
   daemon-wide option sets in parallel. Each failure degrades its own field,
   exactly like the quick job's. */
async function spawnModalLoad(st) {
  const ui = st.ui, parent = st.parent;
  const meta = await api(`/api/sessions/${encodeURIComponent(parent)}/meta`)
    .then((r) => (r.ok ? r.json() : null)).catch(() => null);
  if (spawnModal !== st) return;
  const found = (meta && meta.session) ||
    sessionsCache.find((s) => s.name === parent) || null;
  const sess = found || {};
  ui.parentSess = sess;
  const ms = (meta && meta.meshes) || [];
  // The parent's own handle in its mesh, for the connect list to leave out.
  ui.parentSess._meshHandle = ms.length ? ms[0].handle : null;
  ui.parentMeshes = ms.map((m) => (m && m.mesh) || "").filter(Boolean);
  // The mesh an INHERITING child lands in — defined only when the parent is
  // in exactly one. The daemon refuses to guess between several and says why
  // (daemon/onboard.py inherit_mesh), so neither does this: a guess here does
  // not fail, it broadcasts the child into a room of strangers.
  ui.parentMesh = ui.parentMeshes.length === 1 ? ui.parentMeshes[0] : "";
  // Where the child will stand — the question the git and workflow fetches
  // below are BOTH about. A blank cwd is a real answer for a parent that runs
  // in no directory of its own: the child inherits that and runs where the
  // daemon does, which is what an absent cwd resolves to (daemon/api.py
  // h_cflow_workflows). It is not an answer when the parent's own record
  // never arrived — its meta fetch failed and the rail's cache has no row for
  // it. Asking anyway would succeed about the DAEMON's directory and fill the
  // pickers with workflows and worktrees the child will never see, with
  // nothing on screen to say so. `null` means "not known", and the two
  // fetches that need it are skipped so they report as missing sources.
  const cwd = found ? (sess.cwd || "") : null;
  const askCwd = (path) => (cwd === null ? Promise.resolve(null)
    : api(`${path}${encodeURIComponent(cwd)}`)
        .then((r) => (r.ok ? r.json() : null)).catch(() => null));
  const [report, roles, profDoc, meshDoc, gitDoc, wfDoc] = await Promise.all([
    spawnReport(parent),
    api("/api/roles").then((r) => (r.ok ? r.json() : null)).catch(() => null),
    api("/api/profiles").then((r) => (r.ok ? r.json() : null)).catch(() => null),
    api("/api/mesh").then((r) => (r.ok ? r.json() : null)).catch(() => null),
    askCwd("/api/git?cwd="),
    askCwd("/api/cflow/workflows?cwd="),
  ]);
  if (spawnModal !== st) return;
  ui.report = report || {};
  ui.git = gitDoc || { repo: false, worktrees: [] };
  ui._wfs = (wfDoc && wfDoc.workflows) || [];
  const roleNames = ((roles && roles.roles) || []).map((r) => r.name).filter(Boolean);
  const rawProfileOptions = ui.report.profile_options ||
    (profDoc && profDoc.profile_options) ||
    ((ui.report.profile_selectors) ||
      (profDoc && (profDoc.profile_selectors || profDoc.profiles)) || [])
      .map((value) => ({ value, label: value }));
  const profileOptions = normalizeSpawnProfileOptions(rawProfileOptions);
  const meshNames = (meshDoc && meshDoc.meshes || []).map((m) => (m && m.name) || "");
  const seed = st.seed || {};
  const re = spawnRecall();

  ui.profileDetails = {};
  for (const item of (profDoc && profDoc.profile_details) || []) {
    if (item && item.name) ui.profileDetails[item.name] = item;
  }
  ui._profileOptions = profileOptions;
  const desiredSelector =
    (seed.profile !== undefined && seed.profile !== null) ? seed.profile :
      (re.profile || "");
  const desiredParts = String(desiredSelector || "").split(":", 2);
  const desiredProfile = desiredParts[0] || "";
  const desiredHarness = desiredParts[1] || "";
  const groupedProfiles = new Map();
  for (const item of profileOptions) {
    const group = groupedProfiles.get(item.profile) || [];
    group.push(item);
    groupedProfiles.set(item.profile, group);
  }
  fillSpawnSelect(ui.profile, [...groupedProfiles].map(([profile, options]) => [
    profile, profile,
    options.every((item) => item.harness_available === false),
  ]), "(inherit the parent's profile)", desiredProfile);
  refillSpawnHarnesses(ui, desiredHarness);
  ui._borrowPreset =
    (seed.borrow !== undefined && seed.borrow !== null) ? seed.borrow :
      (re.borrow || "");
  const initialSelector = spawnProfileSelector(ui);
  const initialDetail = ui.profileDetails[initialSelector] ||
    ui.profileDetails[baseProfileName(initialSelector)];
  const initialHarness = (initialDetail && initialDetail.harness) ||
    sess.harness || "";
  const initialBorrowCap = profileBorrowCapability(
    initialDetail, initialHarness
  );
  fillValidatedBorrow(
    ui.borrow, { options: [] },
    initialBorrowCap.allowed
      ? `(as ${parent} authenticates)`
      : profileOwnAuthLabel(initialSelector, initialHarness),
    "", "",
    initialBorrowCap.allowed ? baseProfileName(initialSelector) : ""
  );
  fillSpawnSelect(ui.role, roleNames.map((r) => [r, r]), "(no role)",
    (seed.role !== undefined && seed.role !== null) ? seed.role :
      (re.role || ""));
  fillSpawnSelect(ui.mesh,
    [].concat(meshNames.map((n) => [n, n]), [["-", "(none) — no mesh"]]),
    "(inherit the parent's mesh)",
    // Inherit is the DEFAULT, not merely an option: the row now opens the way
    // Profile and Directory do. Naming the parent's mesh outright is
    // the same answer only while the parent is in one mesh, and it spells that
    // answer into the payload — which takes the choice away from
    // daemon/onboard.py inherit_mesh, the one place that knows the rule.
    seed.mesh !== undefined ? seed.mesh : "");
  const wsp = ui.report.workspaces;   // absent when the policy locks the row
  fillSpawnSelect(ui.workspace,
    (wsp || []).map((w) => [w.name, w.exists ? `${w.name} — ${w.path}` : `${w.name} (missing)`, !w.exists]),
    "(inherit the parent's directory)", seed.workspace || "");
  /* The seed still speaks the API's language — `true` for "one of its own",
     a name for a particular checkout — and this is where that becomes a mode.
     A name the repository already has is a REUSE; a name it does not have is
     a new checkout carrying its name, which is what the quick job's saved
     default means when it names one. */
  const wts = ui.git.worktrees || [];
  const wt = seed.worktree;
  const named = (typeof wt === "string" && wt) ? wt : "";
  const reuse = named && wts.includes(named);
  fillSpawnSelect(ui.wtPick, wts.map((n) => [n, n]),
    wts.length ? null : "(no worktree here yet)", reuse ? named : "");
  /* Silence in the seed opens on "new". A checkout of its own is what a
     child usually needs -- two sessions in one checkout tread on each
     other's edits and branch switches -- so the form opens on the mode that
     keeps them apart, and sharing the parent's directory becomes an answer
     the operator gives rather than one they forget to take away. Only
     SILENCE, though: `worktree: false` is what the quick-job panel sends for
     an unticked box, and that is an answer already.

     Where the row cannot be used the default stays the harmless one. A
     directory that is no repository takes the row away and a locked
     `spawn.allow_worktree` greys every radio (syncSpawnGates), and
     spawnPayload reads through both -- so a "new worktree" left checked
     there would send nothing while telling the operator, over a note reading
     "a child inherits its parent's directory", that a checkout is coming. */
  const wtSaid = wt !== undefined && wt !== null;
  const wtOpen = !!ui.git.repo &&
    ((ui.report.may_choose || []).includes("worktree"));
  ui.wtMode.value = wt === true || (named && !reuse) || (!wtSaid && wtOpen)
    ? "new"
    : reuse ? "existing" : "";
  if (named && !reuse) ui.wtName.value = named;
  if (seed.wtName) ui.wtName.value = seed.wtName;
  if (seed.context) ui.context.value = seed.context;
  if (seed.rebase) ui.rebase.value = seed.rebase;

  // Only a HARD block stops the load here. The child cap is soft and is
  // crossed by the Over-limit row below, so stopping on it would hide that
  // row (syncSpawnGates is what un-hides it) AND leave the Workflow picker
  // unfilled -- a modal that reports "child limit reached" over an empty
  // dropdown, which is what a full parent used to open as.
  const hard = spawnHardBlocks(ui.report);
  if (hard.length) {
    st.noteShow(hard.join("; ") || "this session may not spawn", "wf-warning");
    return;   // the form stands readable; the button stays dead
  }
  const verdict = spawnPreflightNote(ui.report);
  // Which of the six sources did not arrive. Said before the slot count,
  // and in the warning colour: a picker that is empty because its fetch
  // failed is the one thing this form cannot let the operator discover by
  // pressing Spawn — the payload simply omits the field, and the child comes
  // up without the role or workflow it was meant to have.
  const srcNote = spawnSourceNote(spawnMissingSources({
    "the spawn policy": report, roles, profiles: profDoc,
    meshes: meshDoc, "the git state": gitDoc, workflows: wfDoc,
  }));
  // Where the verdict is SAID depends on which verdict it is. Below the cap
  // it is a standing fact about the parent ("2 child slot(s) left") and reads
  // with the form's other standing facts, at the top. At the cap it is the
  // reason a particular button is dead, so it goes to that button — as the
  // gate's heading, one line above the crossing that revives it. Carrying it
  // in both places would only teach the operator to read neither.
  const capped = !verdict.ok;
  // The cap has its own home now -- the gate down in the action bar, beside
  // the button it is about. Saying it up here as well is the three-places
  // layout that gate was built to end: in both places would only teach the
  // operator to read neither.
  const softCap = !!(report.soft_blocked_by || []).length;
  const lines = [];
  if (srcNote) lines.push(srcNote);
  if (verdict.msg && !capped && !softCap) lines.push(verdict.msg);
  if (lines.length) {
    st.noteShow(lines.join(" · "), srcNote ? "wf-warning" : "wf-note");
  }
  ui.capped = capped;
  // Two sentences for two situations, and they are no longer the same one.
  // A hard block (spawning off, depth) leaves Spawn dead and no box can
  // change that. The soft cap leaves Spawn alive and says so — the box under
  // it is how someone asks to be refused, which is the opposite errand from
  // the one the old "until this box is ticked" sent them on.
  ui.capNote.textContent = capped
    ? `${verdict.msg || "this session may not spawn"} — Spawn stays dead`
    : (report.soft_blocked_by || []).join("; ");

  // The workflow picker's first fill: a seed names the workflow outright (the
  // quick-job default), otherwise the parent's own pair is offered.
  // Reading THROUGH the seed lets refill keep a value the auto had set, which
  // is how a role switch re-homes it without trampling an explicit pick.
  ui.workflow.value = seed.workflow || "";
  syncSpawnGates(ui);
  st.lastWfAuto = refillSpawnWorkflows(ui, seed.role || re.role || ui.role.value, "").auto;
  syncSpawnGates(ui);
  await refreshSpawnBorrowOptions(st, true);
  if (spawnModal !== st) return;
  // The child cap no longer shuts the button, so the tick no longer opens
  // it: what is left in `capped` is spawning switched off and the depth
  // ceiling, and neither of those is a thing a checkbox waives. The button
  // follows the hard verdict alone, and the title says which one it is
  // instead of pointing at a box that would not help.
  st.spawnBtn.disabled = capped;
  st.spawnBtn.title = capped
    ? (verdict.msg || "this session may not spawn")
    : "";
  // The default mesh's own members, fetched once so the connect offers are
  // standing before anyone touches the mesh picker.
  refreshSpawnConnect(st).then(() => {
    if (spawnModal === st) syncSpawnGates(ui);
  });

  // The choices that re-gate their neighbours:
  ui.profile.addEventListener("change", () => {
    refillSpawnHarnesses(ui, ui.profile.value ? undefined : "");
    return refreshSpawnBorrowOptions(st, true);
  });
  ui.harness.addEventListener("change", () =>
    refreshSpawnBorrowOptions(st, true));
  ui.nullTok.addEventListener("change", () => syncSpawnGates(ui));
  ui.codexYolo.addEventListener("change", () => syncSpawnGates(ui));
  ui.codexSandbox.addEventListener("change", () => syncSpawnGates(ui));
  ui.wtMode.listen(() => syncSpawnGates(ui));
  ui.wtPick.addEventListener("change", () => syncSpawnGates(ui));
  ui.update.addEventListener("change", () => syncSpawnGates(ui));
  ui.workspace.addEventListener("change", () => {
    syncSpawnGates(ui);
    // The board follows the Directory row in the create form, and a child
    // aimed at another workspace stands on that board — the issue memo is
    // given back so the next "existing" reads there regardless (the same
    // drop the create form does on its Directory row).
    ui._issuesFor = null;
    ui._issuesRead = false;
    if (ui.beads.value === "existing") refreshSpawnBeads(st);
  });
  ui.workflow.addEventListener("change", () => syncSpawnGates(ui));
  ui.mesh.addEventListener("change", () => refreshSpawnConnect(st).then(() => syncSpawnGates(ui)));
  ui.handle.addEventListener("input", () => refreshSpawnConnect(st).then(() => syncSpawnGates(ui)));
  ui.role.addEventListener("change", () => {
    const last = st.lastWfAuto;
    st.lastWfAuto = refillSpawnWorkflows(ui, ui.role.value, last).auto;
    syncSpawnGates(ui);
  });

  /* The board row's own wiring. The radio listener exists for the FETCH —
     the gates already re-sync the row on any change — because the list is
     only worth a board read once somebody picks the one answer that looks
     at it; the pick listener re-verdicts the hint under the picked row.
     syncSpawnGates re-syncs the row on any other change. */
  ui.beads.listen(() => {
    if (ui.beads.value === "existing") refreshSpawnBeads(st);
    syncSpawnBeads(ui);
  });
  ui.issuePick.addEventListener("change", () => syncSpawnBeads(ui));
}

/* The issues the "existing" answer offers, fetched from the daemon's own
   verdicts (daemon/beads.py adoption) so the row promises exactly what the
   spawn is about to do. The child stands in the parent's directory unless
   the workspace row moves it, and the board follows the child — which is
   why the candidates endpoint takes `?parent=` and resolves it to the
   parent's cwd (api.py h_beads_candidates); a pick of a particular
   workspace asks that board directly.

   Claimed before the await and given back on failure, like the create
   form's refreshIssueChoices: an empty list remembered as an answer would
   leave the picker blank for the life of the open. */
async function refreshSpawnBeads(st) {
  const ui = st.ui;
  const wsp = (!ui.workspace.disabled && ui.workspace.value)
    ? ((ui.report && ui.report.workspaces) || [])
        .find((w) => w.name === ui.workspace.value)
    : null;
  const key = wsp ? `cwd:${wsp.path || wsp.name}` : `parent:${st.parent}`;
  if (key === ui._issuesFor) return;
  ui._issuesFor = key;
  let answered = false;
  try {
    const q = wsp && wsp.path
      ? `cwd=${encodeURIComponent(wsp.path)}`
      : `parent=${encodeURIComponent(st.parent)}`;
    const resp = await api(`/api/beads/candidates?${q}`);
    const doc = resp.ok ? await resp.json() : {};
    ui._issues = doc.issues || [];
    ui._issuesError = doc.error || "";
    answered = resp.ok;
  } catch {
    ui._issues = [];
    ui._issuesError = "";
  }
  if (!answered && ui._issuesFor === key) ui._issuesFor = null;
  ui._issuesRead = true;
  fillSpawnIssueOptions(ui);
  if (spawnModal === st) syncSpawnBeads(ui);
}

/* The picker, filled from the last board answer and kept on the row the
   operator already chose — the same "leave a value standing where it was"
   rule the other pickers refill under. */
function fillSpawnIssueOptions(ui) {
  const kept = ui.issuePick.value;
  fillSpawnSelect(
    ui.issuePick,
    (ui._issues || []).map((i) => {
      const held = i.held_by ? ` — held by ${i.held_by}, would JOIN` : "";
      return [
        i.id,
        `${i.id}  ${i.title || ""}`.trim() + ` [${i.status}]${held}`,
      ];
    }),
    "(pick an issue)",
    kept
  );
}

/* The connect row: the members of the picked mesh the child may also message.
   The parent's own handle and the child's pick are excluded — the mesh join
   wires both — and the list is opt-in, so nothing is connected it was not
   asked to be. */
async function refreshSpawnConnect(st) {
  const ui = st.ui;
  // The effective mesh, not the literal pick: sitting on "(inherit)" still
  // lands the child in the parent's mesh, so its members are still the peers
  // on offer. spawnMeshNow answers "" when there is nothing to inherit — a
  // parent in no mesh (the daemon opens a fresh one holding only the pair) or
  // in several (the daemon refuses rather than guess) — and an empty answer
  // is the right one to offer no peers for.
  const mesh = spawnMeshNow(ui);
  ui._connectChecked = [];
  const row = ui.connectRow;
  row.innerHTML = "";
  if (!mesh || mesh === "-") { ui.connectHandles = []; return; }
  let info = null;
  try {
    info = await api(`/api/mesh/${encodeURIComponent(mesh)}`)
      .then((r) => (r.ok ? r.json() : null));
  } catch { info = null; }
  if (spawnModal !== st) return;
  const handles = spawnConnectNow(ui,
    (info && info.members || []).map((m) => m.handle).filter(Boolean));
  ui.connectHandles = handles;
  if (!handles.length) return;   // the row stays hidden; the join is enough
  row.appendChild(el("span", "sess-spawn-label", "Connect"));
  const boxes = [];
  for (const h of handles) {
    const lab = el("label", "check sess-spawn-check");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    boxes.push({ h, cb });
    cb.addEventListener("change", () => {
      ui._connectChecked =
        boxes.filter((b) => b.cb.checked).map((b) => b.h);
    });
    lab.append(cb, el("span", null, h));
    row.appendChild(lab);
  }
}

async function spawnModalGo(st) {
  if (st.busy) return;
  const ui = st.ui;
  const body = spawnPayload(ui);
  st.busy = true;
  st.spawnBtn.disabled = true;
  st.noteShow("spawning…");
  const res = await postSpawn(st.parent, body);
  st.busy = false;
  if (spawnModal !== st) return;
  if (!res.ok) {
    st.noteShow(res.error, "wf-warning");
    st.spawnBtn.disabled = false;
    return;
  }
  saveSpawnRecall({
    parent: st.parent,
    profile: body.profile, borrow: body.borrow,
    null_token: !!body.null_token, role: body.role,
  });
  spawnModalClose();
  // The spawned session is what the press was for, so the page goes there —
  // the same landing the create form gives (`#/s/<name>` right after its
  // POST). Without it the wizard closed onto whatever was behind it and the
  // new terminal had to be found in the rail by hand, which on a busy rail
  // is a scroll and a guess at which `sN` is the new one.
  //
  // The rail is refreshed FIRST and awaited: the terminal route repoints the
  // detail panel and paints the header from `sessionsCache`, and a route
  // entered before the child is in that cache paints an empty one.
  await refreshSessions();
  if (sessName === st.parent) refreshSessKids();
  // The spawn endpoint wraps the child ("session"), and the same shape is
  // read in the create form. A daemon that answered without a name is not a
  // reason to navigate nowhere — the spawn still happened, so the rail
  // refresh above stands and only the hop is skipped.
  const made = (res.doc && res.doc.session) || res.doc || {};
  if (made.name) go("#/s/" + encodeURIComponent(made.name));
}

/* ---- quick job: one form, one worker ----
   The wizard's spawn form shrunk to the child a leader actually dispatches:
   a worker-role session driving the worker workflow, in a checkout of its
   own, briefed with one task. Everything but the task is prefilled from the
   `quick_job:` block of ~/.claunch.yaml (GET /api/quickjob), and the form
   can write edited defaults back (PUT) — the YAML stays the single source,
   the form is just a hand on it. What a child MAY be is still the spawn
   policy's call: a default the policy refuses is refused at spawn time,
   with the daemon's own message shown here. */
const QUICKJOB_FALLBACK = {
  // No workflow: which run a child drives is the parent's pair to declare
  // (default_child_cflow), and a name hard-coded here would hand it to the
  // children of a session driving something else. Mirrors quickjob.DEFAULTS.
  role: "worker", workflow: "", worktree: true,
  name_prefix: "job", task: "",
};

function qjStamp() {
  const d = new Date();
  const p2 = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${p2(d.getMonth() + 1)}${p2(d.getDate())}` +
    `-${p2(d.getHours())}${p2(d.getMinutes())}${p2(d.getSeconds())}`;
}

/* The nudge's whole text, in one place so a test can hold it against what
   the button sends. It reports idleness and asks for judgement; the daemon
   kills nothing on its own. */
function idleNudgeText(idle) {
  return (
    `[dashboard nudge] idle child session(s): ${idle.join(", ")} — for each ` +
    "one, if its work is done and you have its report, kill it ('kill' via " +
    "MCP, or 'claunch kill NAME') so the slot comes back; if it should " +
    "still be working, ask it what it is waiting on. Your call — this " +
    "button only reports idleness."
  );
}

function sessQuickJob(data) {
  const s = data.session || {};
  const box = el("div", "sess-quickjob");
  box.appendChild(el("h3", null, "Quick job"));

  // Rebuilt only when the panel repoints: the 2s poll must not wipe a
  // half-typed task, and the pickers were fetched for THIS session's cwd.
  const key = `${s.name}|${s.cwd}`;
  if (sessQuickJobBox && sessQuickJobBox.dataset.slot === key) {
    box.appendChild(sessQuickJobBox);   // appending moves the live node here
    return box;
  }
  const form = el("div", "sess-quickjob-form");
  form.dataset.slot = key;
  sessQuickJobBox = form;
  box.appendChild(form);

  // What this panel IS, now that the spawn wizard does the spawning: the
  // hand on the quick_job block of ~/.claunch.yaml, plus the one button that
  // hands those defaults to the wizard. It is deliberately NOT a second
  // spawn form — every field a child can differ on lives in the wizard, and
  // a panel that offered them too would be a second reading of the policy.
  form.appendChild(el(
    "p", "wf-note",
    "the quick_job defaults from ~/.claunch.yaml — role, workflow and " +
    "worktree for the workers this leader dispatches. Save as defaults " +
    "writes them back; Spawn worker hands them to the spawn wizard, where " +
    "the task is typed and the spawn actually happens"
  ));

  const row = el("div", "sess-send-row");
  const roleSel = document.createElement("select");
  const wfSel = document.createElement("select");
  for (const sel of [roleSel, wfSel]) {
    sel.disabled = true;
    sel.appendChild(el("option", null, "loading…"));
  }
  roleSel.title = "the child's role — a stance injected at every spawn";
  wfSel.title = "the cflow workflow started for the child, from those " +
    "declared in this directory — leave it at (no workflow) to take the " +
    "pair this session's own run declares (default_child_cflow)";
  row.append(roleSel, wfSel);

  const wtLabel = el("label", "check");
  const wtBox = document.createElement("input");
  wtBox.type = "checkbox";
  wtLabel.append(wtBox, el(
    "span", null, "cut it a worktree of its own (no collisions with siblings)"
  ));

  const spawnBtn = el("button", "wf-btn approve", "Spawn worker…");
  spawnBtn.title = "open the spawn wizard prefilled with these defaults — " +
    "type the task there, then press Spawn worker";
  spawnBtn.disabled = true;
  const saveBtn = el("button", "wf-btn option", "Save as defaults");
  saveBtn.title = "write role/workflow/worktree back to the quick_job block " +
    "of ~/.claunch.yaml";
  saveBtn.disabled = true;
  const btns = el("div", "sess-quickjob-btns");
  btns.append(spawnBtn, saveBtn);
  const status = el("p", "wf-note hidden");
  form.append(row, wtLabel, btns, status);

  const say = (msg, cls) => {
    status.className = cls || "wf-note";
    status.textContent = msg;
  };

  let defaults = { ...QUICKJOB_FALLBACK };
  let canSave = false;

  const fill = (sel, names, none, want) => {
    sel.innerHTML = "";
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = none;
    sel.appendChild(empty);
    for (const n of names) {
      const opt = document.createElement("option");
      opt.value = n;
      opt.textContent = n;
      sel.appendChild(opt);
    }
    // The default may name a role/workflow this daemon does not know; offer
    // it anyway, marked, rather than silently spawning without it.
    if (want && !names.includes(want)) {
      const opt = document.createElement("option");
      opt.value = want;
      opt.textContent = `${want} (not found here)`;
      sel.appendChild(opt);
    }
    sel.value = want || "";
    sel.disabled = false;
  };

  (async () => {
    // Four closed sets, fetched once per repoint like the wizard's sources:
    // the defaults, the role and workflow lists, and what this parent may
    // still spawn. Each failure degrades its own field, not the form.
    const [qj, roles, wfs, kids] = await Promise.all([
      api("/api/quickjob").then((r) => (r.ok ? r.json() : null)).catch(() => null),
      api("/api/roles").then((r) => (r.ok ? r.json() : null)).catch(() => null),
      api(`/api/cflow/workflows?cwd=${encodeURIComponent(s.cwd || "")}`)
        .then((r) => (r.ok ? r.json() : null)).catch(() => null),
      spawnReport(s.name),
    ]);
    if (sessQuickJobBox !== form) return; // the panel moved on mid-flight

    if (qj && qj.quick_job) {
      defaults = { ...QUICKJOB_FALLBACK, ...qj.quick_job };
      canSave = true;
    } else {
      say(
        "this daemon has no /api/quickjob — using built-in defaults; " +
        "'claunch daemon restart' to pick up this version", "wf-warning"
      );
    }
    fill(
      roleSel,
      ((roles && roles.roles) || []).map((r) => r.name).filter(Boolean),
      "(no role)", defaults.role
    );
    fill(
      wfSel,
      ((wfs && wfs.workflows) || [])
        .map((w) => (typeof w === "string" ? w : w && !w.error && w.name))
        .filter(Boolean),
      "(no workflow)", defaults.workflow
    );
    wtBox.checked = !!defaults.worktree;
    saveBtn.disabled = !canSave;

    // Same rule as the wizard's: a picker emptied by a failed fetch says so.
    // Here it matters twice over, because these two values are what gets
    // WRITTEN BACK to ~/.claunch.yaml — saving an empty role over a good one
    // because /api/roles was down is a silent edit of the user's config.
    const srcNote = spawnSourceNote(spawnMissingSources({ roles, workflows: wfs }));
    if (srcNote) {
      say(srcNote, "wf-warning");
      saveBtn.disabled = true;   // do not write a list we could not read
    }

    // The policy's own verdict, before the button is pressed: a form that
    // lets you type a task and then refuses the press taught you nothing.
    // Only a HARD block takes the button away, though -- the same subtraction
    // the wizard now makes. The child cap does not refuse at all any more; it
    // warns, and a leader standing at 4/4 that lost the button here never
    // reached the wizard that would have spawned anyway.
    const hard = spawnHardBlocks(kids);
    if (hard.length) {
      say(hard.join("; ") || "this session may not spawn", "wf-warning");
      return; // spawnBtn stays disabled
    }
    const verdict = spawnPreflightNote(kids);
    const capped = !!(kids && (kids.soft_blocked_by || []).length);
    // A source warning outranks the SLOT COUNT: the slots are the happy news,
    // and overwriting the warning with it would hide the only line that
    // explains why a picker is blank. The cap is not happy news, so it stands
    // beside that note instead of being dropped behind it.
    const lines = [];
    if (srcNote) lines.push(srcNote);
    if (verdict.msg && (capped || !srcNote)) lines.push(verdict.msg);
    if (lines.length) {
      say(lines.join(" · "), (srcNote || capped) ? "wf-warning" : "wf-note");
    }
    spawnBtn.disabled = false;
  })();

  spawnBtn.addEventListener("click", () => {
    if (spawnBtn.disabled) return;
    // The leader's batch dispatch, through the same wizard every spawn uses:
    // the panel's three pickers are the seed, the task is typed where the
    // wizard is. The panel stays the hand on the quick_job YAML (save below);
    // the spawn itself moves to the modal, which refreshes kids and rail on
    // success.
    openSpawnModal(s.name, { seed: {
      quick: true,
      role: roleSel.value,
      workflow: wfSel.value,
      worktree: wtBox.checked,
      task: defaults.task || "",
    } });
  });

  saveBtn.addEventListener("click", async () => {
    if (saveBtn.disabled) return;
    saveBtn.disabled = true;
    let resp, doc = {};
    try {
      resp = await api("/api/quickjob", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          role: roleSel.value,
          workflow: wfSel.value,
          worktree: wtBox.checked,
        }),
      });
      doc = await resp.json().catch(() => ({}));
    } catch {
      say("could not reach the daemon — defaults unchanged", "wf-warning");
      saveBtn.disabled = false;
      return;
    }
    saveBtn.disabled = false;
    if (!resp.ok) {
      say(doc.error || `save refused (HTTP ${resp.status})`, "wf-warning");
      return;
    }
    defaults = { ...defaults, ...(doc.quick_job || {}) };
    say("saved to ~/.claunch.yaml (quick_job)");
  });

  return box;
}

/* ---- the leader's children, and the reaping nudge ----
   The roster GET /children already carries — each child, its status, the
   run it drives — put where the leader's operator is looking, with the one
   button the fleet keeps needing: point the leader at its idle children.
   The button KILLS NOTHING. It types a request into the leader's own
   terminal (send-keys via /deliver), because whether an idle child is
   'finished, holding a report' or 'stuck mid-task' is the leader's judgement
   to make, and it is the one who must collect the report before the kill. */
function sessChildren(data) {
  const s = data.session || {};
  const box = el("div", "sess-kids");
  box.appendChild(el("h3", null, "Children"));

  const key = s.name || sessName || "";
  if (sessKidsBox && sessKidsBox.dataset.slot === key) {
    box.appendChild(sessKidsBox);   // appending moves the live node here
    return box;
  }
  stopSessKids();
  const bodyEl = el("div", "sess-kids-body");
  bodyEl.dataset.slot = key;
  sessKidsBox = bodyEl;
  box.appendChild(bodyEl);
  bodyEl.appendChild(el("p", "wf-note", "loading…"));
  refreshSessKids();
  // Its own poll, slower than the panel's: statuses drift in seconds, but a
  // roster is glanced at, not watched.
  sessKidsTimer = setInterval(refreshSessKids, 5000);
  return box;
}

function stopSessKids() {
  if (sessKidsTimer) { clearInterval(sessKidsTimer); sessKidsTimer = null; }
}

async function refreshSessKids() {
  const bodyEl = sessKidsBox;
  // Detached while the Workflow tab has the column: skip the fetch, keep the
  // timer — the Details tab re-adopts the same node when it comes back.
  if (!bodyEl || !bodyEl.isConnected) return;
  const name = bodyEl.dataset.slot;
  let doc;
  try {
    const resp = await api(`/api/sessions/${encodeURIComponent(name)}/children`);
    doc = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      bodyEl.innerHTML = "";
      bodyEl.appendChild(el(
        "p", "wf-warning", doc.error || "cannot read this session's children"
      ));
      return;
    }
  } catch {
    return; // the poll comes back round
  }
  if (sessKidsBox !== bodyEl) return; // repointed mid-flight
  renderSessKids(bodyEl, doc);
}

function renderSessKids(bodyEl, doc) {
  bodyEl.innerHTML = "";
  const kids = doc.children || [];
  if (!kids.length) {
    bodyEl.appendChild(el("p", "wf-note",
      "no children — the quick job form above spawns one"));
    return;
  }
  for (const k of kids) {
    const row = el("div", "sess-kid");
    row.appendChild(el("span", `dot ${k.status || ""}`));
    const link = el("a", "sess-kid-name", k.name);
    link.href = "#/s/" + encodeURIComponent(k.name);
    row.appendChild(link);
    row.appendChild(el("span", "meta", k.status || "?"));
    if (k.cflow) {
      row.appendChild(el("span", "meta sess-kid-flow",
        `${k.cflow.workflow || "run"}${k.cflow.step ? " · " + k.cflow.step : ""}`));
    }
    bodyEl.appendChild(row);
  }

  const idle = kids.filter((k) => k.status === "idle").map((k) => k.name);
  const nudge = el(
    "button", "wf-btn option",
    idle.length ? `Nudge: reap idle children (${idle.length})`
      : "Nudge: reap idle children"
  );
  nudge.disabled = !idle.length;
  nudge.title = idle.length
    ? "types a request into this leader's terminal to review " +
      idle.join(", ") + " and kill the finished ones — nothing is killed " +
      "by the dashboard itself"
    : "no idle children right now";
  const status = el("p", "wf-note hidden");
  nudge.addEventListener("click", async () => {
    if (nudge.disabled) return;
    nudge.disabled = true;
    let resp, doc2 = {};
    try {
      resp = await api(
        `/api/sessions/${encodeURIComponent(bodyEl.dataset.slot)}/deliver`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: idleNudgeText(idle) }),
        });
      doc2 = await resp.json().catch(() => ({}));
    } catch {
      status.className = "wf-warning";
      status.textContent = "could not reach the daemon — nothing was sent";
      nudge.disabled = false;
      return;
    }
    nudge.disabled = false;
    status.className = resp.ok ? "wf-note" : "wf-warning";
    status.textContent = resp.ok
      ? (doc2.delivered
        ? "typed into the leader's terminal"
        : "accepted — queued until the leader's terminal is free")
      : (doc2.error || `nudge refused (HTTP ${resp.status})`);
  });
  bodyEl.append(nudge, status);
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
  // A room may have been joined (or left) since the last session poll, so
  // the header's handle chip is repainted on this poll too — it is the mesh
  // that owns the fact, and this is where the fact arrives.
  renderTermHandle();
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
      "under Delivery policy, or message the member yourself below"
    ));
  } else if (report.engine && info.primary) {
    box.appendChild(el(
      "p", "wf-note",
      `the nudge engine runs on the primary daemon (${info.primary})`
    ));
  }
  return box;
}

/* Delivery-policy editor: heartbeat / task-poll / stall warnings, and the
   backpressure gate, per mesh.

   The first three are nudges — terminal injections that consume the agent's
   turn — so each ships disabled until deliberately switched on here. The
   fourth is the opposite and ships ON: it is what stops a fan-in of a dozen
   children from spending a leader's turns for it. Editable in the same
   place all the same, because a gate a person can see refusing messages
   (the header chip says so) and cannot adjust is worse than no gate. */
function renderMeshPolicy(info) {
  const pol = info.policy || {};
  const box = el("div", "mesh-policy");
  box.appendChild(el("h3", null, "Delivery policy"));
  box.appendChild(el(
    "p", "wf-note",
    "nudges are typed into the member's terminal, so each one costs the " +
    "agent a turn — enable deliberately. Backpressure, at the bottom, is " +
    "the one that ships on: it bounds what a terminal can be handed."
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

  // Not a nudge: the door and the pacing gate. `num`'s min is 1, and both
  // of these take 0 as a real setting ("no cap" / "no pacing"), so they get
  // their own inputs rather than borrowing that one.
  const bp = pol.backpressure || {};
  const zeroable = (val) => {
    const inp = document.createElement("input");
    inp.type = "number"; inp.min = "0"; inp.value = val;
    inp.className = "pol-num";
    return inp;
  };
  section(
    "backpressure",
    "backpressure — stop accepting mail a member has not read (ON by default)",
    [
      ["inbox_max", "refuse past this many queued (0 = no cap)",
       zeroable(bp.inbox_max ?? 4)],
      ["min_gap", "least gap between deliveries (s, 0 = none)",
       zeroable(bp.min_gap ?? 15)],
      ["retry_after", "tell refused senders to wait (s)",
       zeroable(bp.retry_after ?? 90)],
    ]
  );

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
      backpressure: {
        enabled: fields.backpressure.enabled.checked,
        inbox_max: +fields.backpressure.inbox_max.value,
        min_gap: +fields.backpressure.min_gap.value,
        retry_after: +fields.backpressure.retry_after.value,
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

/* The steps of a workflow in the order the run page lays them out. The
   strip's dial is the diagram's, so this borrows wfStepOrder rather than
   walking the graph a second way. It was once its own breadth-first walk —
   deliberately the same as the diagram's, but not shared because one built
   a string and the other data. The test that pinned the two orders
   together still sits in flowtrack_check: if they ever drift, the strip
   stops being the run page's diagram, loudly. */
function flowOrder(wf) {
  return wfStepOrder(wf);
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
  if (f.status === "waiting_approval" || f.status === "waiting_selection" ||
      f.status === "waiting_goto") return true;
  // ...and the ask that reached nobody, which is a gate wearing another
  // status word. flowState asks this before it says "delegated", so the
  // card reads "waiting on you" rather than "waiting on a peer".
  if (answerFellToUs(f)) return true;
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
  // Held for a paced option's window. Its own word for the same reason
  // "delegated" is: nothing is happening. But not delegated either — no peer
  // holds this, the daemon's clock does, and a reader who cannot tell the two
  // apart will go looking for an agent to chase. Below flowNeedsHuman on
  // purpose: a person cannot clear this, so it must not join the queue of
  // things that are theirs to clear.
  if (f.status === "waiting_window") return "held";
  if (f.status === "select") return "deciding";
  if (!f.status || f.status === "idle" ||
      f.status === "no_session" || f.status === "no_cwd") return "none";
  return "running";
}

const FLOW_WORDS = {
  blocked: "waiting on you", running: "running", deciding: "agent deciding",
  delegated: "waiting on a peer",
  held: "held for its window",
  done: "done", aborted: "aborted", error: "error",
  stopped: "session stopped", none: "no run",
  unknown: "run lives on its own daemon",
};

/* The state word, sharpened by the run when the run has more to say. Held is
   the one state whose word is incomplete on its own: "held for its window"
   invites exactly one question, and the payload already answers it. */
function flowStateWord(state, f) {
  if (state === "held" && f && f.opens_at) {
    return `held → ${fmtOpensAt(f.opens_at)}`;
  }
  return FLOW_WORDS[state];
}

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
        : (f.step_id || flowStateWord(state, f))
    ));
    g.appendChild(svg(
      "text", { class: `flow-state ${state}`, x: right, y: m.cardH / 2 - 10,
                "text-anchor": "end" },
      flowStateWord(state, f)
    ));
  } else {
    // Nothing to track. The card says why in the space the track would have
    // taken rather than in the corner, because on this page a card with no
    // line through it is the thing a reader stops at.
    g.appendChild(svg(
      "text", { class: `flow-foot none ${state}`, x: left, y: 10 },
      f && f.graph_error ? "workflow snapshot unreadable" : flowStateWord(state, f)
    ));
  }
  g.appendChild(svg("title", {}, [
    `${member.handle} (${member.role})`,
    member.session,
    wfName ? `workflow ${wfName}` : "no workflow run",
    flowStateWord(state, f),
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
  const pacedNote = wfPacedNote(wf, f);
  if (pacedNote) box.appendChild(pacedNote);
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
    case "state_forced":
      return e.granted ? `move granted: ${step}` : `forced to: ${step}`;
    case "goto_requested": return `asked to move to: ${step}`;
    case "goto_denied": return `move refused: ${step}`;
    case "goto_withdrawn": return `move request withdrawn: ${step}`;
    case "goto_superseded": return `move request overtaken: ${step}`;
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
/* notices — the page's own voice                                     */
/* ------------------------------------------------------------------ */
/* Every other surface here is a poll: it repaints, and whatever changed is
   simply *there*. That is right for a number that moved and wrong for an
   event that happened — a daemon restart repaints into a page that looks
   exactly like the one before it, so the one thing a person wants to know
   ("is this the daemon I was talking to?") was the one thing nothing said.

   A notice is that sentence, and it is deliberately cheap: no permissions,
   no service worker, no sound — a card in the corner, dismissed by clicking
   it. The only real decision in here is `sticky`: an event that happened
   while the tab sat unwatched must NOT time out before it is read, which is
   exactly the restart case, while a blip that already healed should not
   need clearing by hand. `key` is the other half of that — a flapping
   daemon replaces its own card instead of stacking twenty of them. */
const NOTICE_MS = 8000;          // how long a non-sticky card stays up
const NOTICE_LINK = "daemon-link";  // the connection's card: offline / back
const NOTICE_BOOT = "daemon-boot";  // "this is a different daemon"
const notices = new Map();       // key -> {node, timer}
let noticeSeq = 0;

/* Local wall-clock, which is what "when did it happen" means to the person
   reading it: the daemon's own uptime is a duration, and a duration read off
   a card that has been sitting there for ten minutes is a lie. */
function noticeClock() {
  return new Date().toLocaleTimeString();
}

function notify(title, sub, opts) {
  const o = opts || {};
  const host = $("notices");
  if (!host) return null;
  const key = o.key || `n${(noticeSeq += 1)}`;
  dismissNotice(key);            // one card per key, always the newest
  const node = el("div", "notice" + (o.kind ? ` ${o.kind}` : ""));
  node.appendChild(el("div", "notice-title", title));
  if (sub) node.appendChild(el("div", "notice-sub", sub));
  node.title = "click to dismiss";
  node.addEventListener("click", () => dismissNotice(key));
  host.appendChild(node);
  const timer = o.sticky
    ? null
    : setTimeout(() => dismissNotice(key), o.ms || NOTICE_MS);
  notices.set(key, { node, timer });
  return node;
}

function dismissNotice(key) {
  const rec = notices.get(key);
  if (!rec) return;
  notices.delete(key);
  if (rec.timer) clearTimeout(rec.timer);
  if (rec.node.parentNode) rec.node.parentNode.removeChild(rec.node);
}

/* ------------------------------------------------------------------ */
/* the restart gate: an agent session's restart, waiting on a person   */
/* ------------------------------------------------------------------ */
/* A managed session's `claunch daemon restart` opens this gate instead
   of restarting on the spot (daemon/restart_gate.py): the request sits
   here until the operator approves, rejects, or lets its timeout count
   it as approved. Polled with the page's 2s heartbeat — pending keeps
   the card drawn with a live countdown, a settled or gone request takes
   it down. Approving is the daemon's own restart door, so the existing
   restarting announcement (the daemon card's NOTICE_LINK) labels the
   outage that follows; rejecting just closes the card. */
const NOTICE_GATE = "restart-gate";

function gateCountdown(deadlineIso) {
  const dead = new Date(deadlineIso).getTime();
  if (isNaN(dead)) return "";
  const ms = Math.max(0, dead - Date.now());
  const m = Math.floor(ms / 60000);
  const s = Math.floor((ms % 60000) / 1000);
  return `${m}:${String(s).padStart(2, "0")}`;
}

async function gatePost(path, btn) {
  btn.disabled = true;
  try {
    await api(path, { method: "POST" });
  } catch {
    btn.disabled = false;
    return false;
  }
  return true;
}

async function refreshRestartGate() {
  const host = $("notices");
  if (!host) return;
  let body;
  try {
    body = await (await api("/api/daemon/restart-request")).json();
  } catch {
    return; // daemon down or auth up — pollOnce's own channels own both
  }
  const rec = body && body.request ? body.request : null;
  if (!rec || rec.status !== "pending") {
    dismissNotice(NOTICE_GATE);
    return;
  }
  let card = notices.get(NOTICE_GATE);
  if (!card || !card.node.isConnected) {
    dismissNotice(NOTICE_GATE);
    const node = el("div", "notice warn gate");
    const title = el("div", "notice-title", "daemon restart requested");
    const sub = el("div", "notice-sub");
    const actions = el("div", "gate-actions");
    const approve = el("button", "wf-btn approve", "Approve");
    const reject = el("button", "wf-btn clear", "Reject");
    approve.title =
      "the daemon restarts now — or the timeout counts the request as " +
      "approved on its own — either way every attached terminal goes " +
      "down with it";
    reject.title = "nothing restarts; the asking session keeps working";
    approve.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      if (await gatePost("/api/daemon/restart-request/approve", approve)) {
        // The daemon's own door: it finishes the reply, drains, and spawns
        // the successor; label the gap the way the daemon card's button
        // does, so the outage is not read as a mystery.
        setDaemonOnline(false);
        $("daemon-info").textContent = "restarting…";
        notify(
          "restarting the daemon",
          `approved at ${noticeClock()} — the page reconnects on its own ` +
            "once the successor is up",
          { key: NOTICE_LINK, kind: "warn", sticky: true }
        );
        dismissNotice(NOTICE_GATE);
      }
    });
    reject.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      if (await gatePost("/api/daemon/restart-request/reject", reject)) {
        dismissNotice(NOTICE_GATE);
      }
    });
    actions.appendChild(approve);
    actions.appendChild(reject);
    node.appendChild(title);
    node.appendChild(sub);
    node.appendChild(actions);
    host.appendChild(node);
    const entry = { node, timer: null };
    notices.set(NOTICE_GATE, entry);
    card = entry;   // the freshly built card carries this poll's countdown too
  }
  const sub = card.node.querySelector(".notice-sub");
  if (sub) {
    sub.textContent =
      `session ${rec.session || "?"}` +
      (rec.requested_at
        ? ` asked at ${new Date(rec.requested_at).toLocaleTimeString()}`
        : "") +
      ` — auto-approves in ${gateCountdown(rec.deadline)}`;
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
let daemonCache = null;    // last /api/daemon payload; the home card reads it
let daemonStartedAt = null; // ms epoch, derived from that payload's uptime

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
    // The badge is eight pixels in a corner and is missed by everyone whose
    // eyes are on a terminal; the card says the same thing where it cannot
    // be. Sticky, because the outage outlasts any timeout worth setting.
    // Guarded on `booted`: a page opened while the daemon is already down
    // reports that through the auth/empty state, not as an event.
    if (booted) {
      notify(
        "daemon offline",
        `nothing has answered since ${noticeClock()} — what is on screen is ` +
          "the last thing it said, not what is happening now",
        { key: NOTICE_LINK, kind: "bad", sticky: true }
      );
    }
  } else {
    // Whatever the card said about the link is over the moment one answers;
    // WHICH daemon answered is pollOnce's sentence, not this one's.
    dismissNotice(NOTICE_LINK);
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
  daemonCache = info;
  // Uptime is a duration measured at the instant it was read, so it goes
  // stale the moment it lands; the wall-clock start it implies does not.
  // That is what the home card shows, and it is what makes "did it restart
  // while I was away" answerable after the card in the corner is gone.
  if (typeof info.uptime === "number") {
    daemonStartedAt = Date.now() - info.uptime * 1000;
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
  const wasDown = !daemonOnline;   // setDaemonOnline is about to forget this
  setDaemonOnline(true);
  if (restarted || returned) {
    daemonBoot = health.boot_id || null;
    // ...and it is announced. Everything below this line already worked —
    // the page rebuilt itself and the terminal got its socket back — which
    // is precisely why the restart was invisible: recovery that succeeds
    // silently is indistinguishable from nothing having happened. `booted`
    // keeps the first load quiet: arriving is not an event.
    if (booted && restarted) {
      // Two times, because they answer two questions: when the daemon itself
      // came up (what "did it restart while I was away" asks — health
      // publishes it) and when this page noticed (what the card otherwise
      // is). An older daemon without the field still gets the old sentence.
      const bootAt = health.started_at ? new Date(health.started_at) : null;
      const bootText = bootAt && !isNaN(bootAt) ? bootAt.toLocaleTimeString() : null;
      notify(
        "daemon restarted",
        (bootText ? `restarted at ${bootText}, ` : "") +
          `a different daemon answered at ${noticeClock()}` +
          (health.version ? ` (v${health.version})` : "") +
          " — sessions were relaunched, this page re-read everything, and " +
          "your login cookie died with the old process",
        { key: NOTICE_BOOT, kind: "warn", sticky: true }
      );
    } else if (booted && wasDown) {
      notify(
        "daemon back",
        `the same daemon answered again at ${noticeClock()} — it never ` +
          "restarted, so nothing was relaunched",
        { key: NOTICE_LINK }
      );
    }
    await boot();     // re-read everything this daemon publishes, from scratch
    reconnectNow();   // and give the attached terminal its socket back
    return;
  }
  if (health.boot_id) daemonBoot = health.boot_id;
  refreshSessions();
  refreshMeshList();
  refreshCflow();
  refreshTermQueued();
  // Polled because the registry is edited from the CLI, in another window;
  // it redraws only when the list really changed (see refreshWorkspaces).
  refreshWorkspaces();
  // The restart gate can be opened from any session's terminal, so the card
  // is fed by the same heartbeat as the registry.
  refreshRestartGate();
}

pollTimer = setInterval(pollTick, 2000);

/* What the rail hold listens to (its state and functions live beside
   refreshSessions, which consults them). pointerdown covers mouse, pen and
   touch; the release is caught on the document in the capture phase because
   the pointer may well come up somewhere else entirely — a drag off the row,
   a press that ends over the terminal. */
$("session-list").addEventListener("pointerdown", holdRail);
document.addEventListener("pointerup", releaseRail, true);
document.addEventListener("pointercancel", releaseRail, true);

// The countdown, between polls. It fetches nothing — it ages the last
// /api/cflow reading — so it is a second's worth of arithmetic, and it is
// separate from the 2s poll because a clock that only moves every other
// second reads as a clock that has stopped, which is the exact thing the
// header chip exists to tell apart.
termTimerTicker = setInterval(() => { paintTermTimer(); }, 1000);

boot();
