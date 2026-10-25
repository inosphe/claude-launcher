/* The Hive page (#/hive): every project, its meshes, the session trees in
   them and the board's work on one pan-and-zoom canvas (claunch-yzgqb).

   One shape at every level. A project is a wide hexagonal cell, a mesh a
   smaller cell inside it, and a session or an issue a chip with a hexagonal
   core whose colour and glyph say its state. A session's children nest
   under its chip one size smaller, so grandparent -> parent -> child reads
   as depth; the issues a session holds hang off its chip as issue chips.
   Siblings that do not need a row each are stacked into one deck chip the
   viewer can open: minor roles (reviewers, oneshot jobs) always, ended
   siblings (killed, failed) once there are several, and the board's
   unassigned pool by priority.

   Ended records leave the canvas by themselves: archived ones are hidden
   unless asked for, killed/failed ones stay for a chosen window after they
   ended (6h by default) and then drop off, and the toolbar archives every
   killed record at once through the rail's own bulk action. An ended
   session that still has a live descendant stays as a faded chip so the
   tree above that descendant is not cut.

   Like the Chart page it owns no poll of the fleet: sessions, memberships
   and cflow runs come from the caches app.js keeps (sessionsCache,
   sessMeshes, cflow2RailCache / sessCflowRun). The work comes from
   /api/beads/queues, read here on a slower clock while the page is open:
   one lane per session (its queue) plus each board's unassigned pool. That
   payload carries no parent-child edges, so an issue's parent is read from
   its id: `claunch-q4cqy.5` belongs under `claunch-q4cqy` when both are
   drawn.

   The model (buildModel and the helpers before it) takes plain data so
   tests/web/hivepage_check.js runs it without a page. The canvas is rebuilt
   only when the model changed: a timer redraw that would draw the same
   thing touches nothing (claunch-h7zge). */
globalThis.HivePage = (() => {
  const H = 3600000;
  const ENDED_WINDOWS = [
    ["0", "숨김", 0], ["1h", "1시간", H], ["6h", "6시간", 6 * H],
    ["24h", "24시간", 24 * H], ["all", "전체", Infinity],
  ];
  const REDRAW_MS = 4000;
  const BEADS_MS = 20000;
  const STORE_KEY = "claunch_hive_view";
  // Ended siblings stack into one deck from this many on.
  const ENDED_STACK_MIN = 3;
  // Held, paused and delayed siblings with nothing live under them stack from
  // this many on: parked sessions are most of a fleet's records.
  const RESTING_STACK_MIN = 4;
  const RESTING = new Set(["hold", "paused", "delayed"]);
  // An opened deck draws this many chips before it offers the rest.
  const DECK_OPEN_CAP = 40;
  const ZOOM_MIN = 0.2, ZOOM_MAX = 2.5;
  const STATES = ["busy", "idle", "starting", "hold", "delayed", "paused", "failed", "killed", "archived"];
  const STATE_LABEL = {
    busy: "busy", idle: "idle", starting: "starting", hold: "hold", delayed: "delayed",
    paused: "paused", failed: "failed", killed: "killed", archived: "archived",
  };
  const STATE_GLYPH = {
    busy: "●", idle: "○", starting: "◌", hold: "‖", delayed: "⏲", paused: "Ⅱ",
    failed: "✕", killed: "×", archived: "▫",
  };
  const ISSUE_ACTIVE = ["in_progress", "in_review", "blocked", "in_ready", "open"];

  /* The rail's categories (app.js sessionCategory), repeated so the model
     runs without app.js. */
  function category(s) {
    if (s && s.archived_at) return "archived";
    if (s && s.status === "exited") {
      if (s.held_at) return s.delayed_at ? "delayed" : "hold";
      if (s.failed_at) return "failed";
      return s.paused_at ? "paused" : "killed";
    }
    return "running";
  }

  /* What the chip's core says: the category, with a running session split
     into what its program is doing now. */
  function stateOf(s) {
    const c = category(s);
    if (c !== "running") return c;
    const st = String((s && s.status) || "");
    return st === "busy" || st === "idle" ? st : "starting";
  }

  function ended(state) {
    return state === "killed" || state === "failed" || state === "archived";
  }

  function ms(iso) {
    const t = Date.parse(iso || "");
    return Number.isFinite(t) ? t : null;
  }

  function endedAt(s) {
    return ms(s && s.exited_at) ?? ms(s && s.archived_at) ?? ms(s && s.last_activity_at)
      ?? ms(s && s.created_at);
  }

  function projectOf(s) {
    return String((s && s.project) || "").trim() || "default";
  }

  /* The role a chip names: the record's own field, then what it holds in
     the mesh it is drawn under, then any membership. */
  function roleOf(s, memberships) {
    if (s && s.role) return String(s.role);
    const ms_ = memberships || [];
    const first = ms_.find((m) => m.label);
    return first ? first.label : "";
  }

  /* A minor session: one whose work is a side job of another's -- a
     reviewer, a oneshot job. Read from the role first; a session with no
     role is minor by the naming the fleet gives reviewers (`<parent>-rev`,
     `-rev2`) or a kind that says it runs once. A leader or worker role is
     never minor, whatever else it holds. */
  function isMinor(s, role) {
    const r = String(role || "").toLowerCase();
    if (/\b(leader|worker|merger)\b/.test(r)) return false;
    if (/reviewer|oneshot|one-shot|observer|probe/.test(r)) return true;
    const kind = String((s && s.kind) || "").toLowerCase();
    if (/oneshot|one-shot|quick/.test(kind)) return true;
    return /-rev\d*$/.test(String((s && s.name) || ""));
  }

  /* Whether a record is drawn at all: archived only when asked for, killed
     and failed for `endedMs` after they ended, everything else always. */
  function keep(s, opts, now) {
    const c = category(s);
    if (c === "archived") return !!opts.archived;
    if (c === "killed" || c === "failed") {
      const w = opts.endedMs ?? 6 * H;
      if (!w) return false;
      if (w === Infinity) return true;
      const t = endedAt(s);
      return t === null || now - t <= w;
    }
    return true;
  }

  /* The parent an issue id names by its dotted suffix, among `ids`:
     `a-b.2.1` -> `a-b.2` if present, else `a-b`, else null. */
  function issueParentId(id, ids) {
    let cur = String(id || "");
    while (/\.\d+$/.test(cur)) {
      cur = cur.replace(/\.\d+$/, "");
      if (ids.has(cur)) return cur;
    }
    return null;
  }

  const STATE_RANK = Object.fromEntries(STATES.map((s, i) => [s, i]));
  const ISSUE_RANK = Object.fromEntries(ISSUE_ACTIVE.map((s, i) => [s, i]));
  const byName = (a, b) => String(a.name).localeCompare(String(b.name), undefined, { numeric: true });

  function issueChip(row) {
    return {
      kind: "issue", id: String(row.id), title: String(row.title || ""),
      status: String(row.status || "open"), priority: Number.isFinite(row.priority) ? row.priority : 2,
      type: String(row.issue_type || "task"), assignee: row.assignee || "", children: [],
    };
  }

  function sortIssues(list) {
    list.sort((a, b) => (ISSUE_RANK[a.status] ?? 9) - (ISSUE_RANK[b.status] ?? 9)
      || a.priority - b.priority || a.id.localeCompare(b.id, undefined, { numeric: true }));
    for (const i of list) sortIssues(i.children);
    return list;
  }

  /* Nest a flat list of issue chips by their dotted ids; what has no parent
     in the list stays at the top. */
  function nestIssues(chips) {
    const ids = new Set(chips.map((c) => c.id));
    const at = new Map(chips.map((c) => [c.id, c]));
    const top = [];
    for (const c of chips) {
      const p = issueParentId(c.id, ids);
      if (p && p !== c.id) at.get(p).children.push(c);
      else top.push(c);
    }
    return sortIssues(top);
  }

  function countIssues(list) {
    let n = 0;
    for (const i of list) n += 1 + countIssues(i.children);
    return n;
  }

  /* The deck a set of sibling nodes folds into. `key` names it across
     redraws, so an opened deck stays open. */
  function deck(key, label, items, kind) {
    const states = {};
    for (const it of items) {
      const st = it.kind === "issue" ? it.status : it.state;
      states[st] = (states[st] || 0) + 1;
    }
    const n = kind === "issue" ? countIssues(items) : items.length;
    return { kind: "deck", key, label, of: kind, count: n, states, items };
  }

  /* Split siblings into the ones drawn one by one and the decks the rest
     fold into. */
  function arrangeSiblings(nodes, key, stackMinor, stackIdle = true) {
    const major = [], minor = [], over = [], resting = [];
    for (const n of nodes) {
      // A sibling whose subtree holds a live session or work in hand keeps
      // its own place: folding it would hide what is still moving.
      // A minor session folds whatever its own state: the deck's dots still
      // say how many of them are working. Only one with something of its
      // own under it (work, a live child) keeps its place.
      const quiet = !hasLive(n);
      if (stackMinor && n.minor && !hasLiveBelow(n)) minor.push(n);
      else if (ended(n.state) && quiet) over.push(n);
      else if (stackIdle && RESTING.has(n.state) && quiet) resting.push(n);
      else major.push(n);
    }
    const decks = [];
    if (minor.length >= 2) decks.push(deck(`${key}/minor`, "보조 세션", minor, "session"));
    else major.push(...minor);
    if (resting.length >= RESTING_STACK_MIN) decks.push(deck(`${key}/resting`, "쉬는 세션", resting, "session"));
    else major.push(...resting);
    if (over.length >= ENDED_STACK_MIN) decks.push(deck(`${key}/ended`, "끝난 세션", over, "session"));
    else major.push(...over);
    major.sort((a, b) => (a.minor - b.minor) || (STATE_RANK[a.state] - STATE_RANK[b.state]) || byName(a, b));
    for (const d of decks) d.items.sort((a, b) => (STATE_RANK[a.state] - STATE_RANK[b.state]) || byName(a, b));
    return { nodes: major, decks };
  }

  /* Whether a node or anything under it is running or holds work. */
  function hasLive(n) {
    return n.state === "busy" || n.state === "idle" || n.state === "starting" || hasLiveBelow(n);
  }

  function hasLiveBelow(n) {
    if (n.issues && n.issues.length) return true;
    return (n.children || []).some(hasLive) || (n.decks || []).some((d) => d.items.some(hasLive));
  }

  /* session name -> {issues, created} from a /api/beads/queues payload, and
     each board's pool. Lanes of sessions the daemon does not know (a person
     such as `operator`, a name on another machine) are kept apart: their
     issues are work nobody on this canvas holds. */
  function readQueues(queues) {
    const lanes = new Map();
    const boards = [];
    for (const b of (queues && queues.boards) || []) {
      const foreign = [];
      for (const lane of b.lanes || []) {
        if (!lane.session) continue;
        if (!lane.known) {
          for (const i of lane.issues || []) foreign.push(i);
          continue;
        }
        const cur = lanes.get(lane.session) || { issues: [], created: 0, board: b.root };
        cur.issues = cur.issues.concat(lane.issues || []);
        cur.created += (lane.created || []).length;
        lanes.set(lane.session, cur);
      }
      boards.push({
        root: b.root || "", workspace: b.workspace || "",
        unassigned: b.unassigned || [], foreign, error: b.error || "",
        sessions: (b.lanes || []).filter((l) => l.known).map((l) => l.session),
      });
    }
    return { lanes, boards };
  }

  function boardLabel(b) {
    const ws = b.workspace && typeof b.workspace === "object" ? b.workspace.name : b.workspace;
    if (ws) return String(ws);
    const parts = String(b.root || "").split(/[\\/]/).filter(Boolean);
    return parts[parts.length - 1] || "board";
  }

  /* Everything the canvas draws.
     opts: now, endedMs, archived, issues (draw work), pool (draw the
     unassigned pool), stackMinor, project ("" = all), meshesOf(name),
     cflowOf(name). */
  function buildModel(sessions, queues, opts = {}) {
    const now = opts.now ?? Date.now();
    const meshesOf = opts.meshesOf || (() => []);
    const cflowOf = opts.cflowOf || (() => null);
    const all = new Map();
    for (const s of sessions || []) if (s && s.name) all.set(s.name, s);
    const { lanes, boards } = readQueues(queues);
    const counts = Object.fromEntries(STATES.map((s) => [s, 0]));
    let hiddenEnded = 0, hiddenArchived = 0, killedTotal = 0;
    const shown = new Set();
    for (const s of all.values()) {
      const st = stateOf(s);
      if (st === "killed") killedTotal++;
      if (!keep(s, opts, now)) {
        if (st === "archived") hiddenArchived++; else hiddenEnded++;
        continue;
      }
      shown.add(s.name);
    }
    // The ancestors of a drawn session are drawn too (faded when they would
    // not be), so a tree is never cut above a live child.
    const ghosts = new Set();
    for (const name of [...shown]) {
      let p = all.get(name).parent;
      const seen = new Set([name]);
      while (p && all.has(p) && !seen.has(p)) {
        seen.add(p);
        if (!shown.has(p)) { ghosts.add(p); shown.add(p); }
        p = all.get(p).parent;
      }
    }
    for (const g of ghosts) {
      const st = stateOf(all.get(g));
      if (st === "archived") hiddenArchived--; else hiddenEnded--;
    }
    const wantIssues = opts.issues !== false;
    const heldIssue = new Set();
    const nodes = new Map();
    for (const name of shown) {
      const s = all.get(name);
      const memberships = meshesOf(name) || [];
      const role = roleOf(s, memberships);
      const state = stateOf(s);
      counts[state]++;
      const lane = lanes.get(name);
      let issues = [];
      if (wantIssues && lane && !ended(state)) {
        issues = nestIssues(lane.issues.map(issueChip));
        for (const i of lane.issues) heldIssue.add(String(i.id));
      }
      const cflow = cflowOf(name);
      nodes.set(name, {
        kind: "session", name, state, role, minor: isMinor(s, role),
        ghost: ghosts.has(name), project: projectOf(s), parent: s.parent || "",
        meshes: memberships.map((m) => m.mesh), issue: s.issue || "",
        fault: s.fault && s.fault.active ? String(s.fault.label || s.fault.kind || "fault") : "",
        compacting: !!s.compacting,
        cflow: cflow ? { state: cflow.state, label: cflow.label } : null,
        created: lane ? lane.created : 0, issues, children: [], decks: [],
      });
    }
    const roots = [];
    for (const n of nodes.values()) {
      const p = n.parent && nodes.get(n.parent);
      if (p && p !== n && !isAncestor(nodes, n.name, p.name)) p.children.push(n);
      else roots.push(n);
    }
    const stackMinor = opts.stackMinor !== false;
    const stackIdle = opts.stackIdle !== false;
    const settle = (n) => {
      const { nodes: kids, decks } = arrangeSiblings(n.children, n.name, stackMinor, stackIdle);
      n.children = kids;
      n.decks = decks;
      for (const k of kids) settle(k);
      for (const d of decks) for (const k of d.items) settle(k);
    };
    // A project, then a mesh: a tree goes where its root is filed. A root
    // in several meshes goes under the one it leads, else its first.
    const projects = new Map();
    const projectFor = (name) => {
      if (!projects.has(name)) projects.set(name, { name, meshes: new Map(), pool: [], sessions: 0, issues: 0 });
      return projects.get(name);
    };
    for (const r of roots) {
      if (opts.project && r.project !== opts.project) continue;
      const ms_ = meshesOf(r.name) || [];
      const lead = ms_.find((m) => /leader/.test(m.label || m.role || "")) || ms_[0];
      const meshName = lead ? lead.mesh : "";
      const proj = projectFor(r.project);
      if (!proj.meshes.has(meshName)) proj.meshes.set(meshName, { name: meshName, roots: [] });
      proj.meshes.get(meshName).roots.push(r);
    }
    // The pool: each board's unassigned issues by priority, and the issues
    // held by sessions this canvas does not draw (ended, or unknown to the
    // daemon). A board goes to the project most of its drawn sessions are
    // filed under.
    if (wantIssues && opts.pool !== false) {
      for (const b of boards) {
        const tally = new Map();
        for (const name of b.sessions) {
          const n = nodes.get(name);
          if (n && !n.ghost) tally.set(n.project, (tally.get(n.project) || 0) + 1);
        }
        let pname = "default", best = -1;
        for (const [p, c] of tally) if (c > best) { best = c; pname = p; }
        if (opts.project && pname !== opts.project) continue;
        const orphan = [];
        for (const name of b.sessions) {
          if (nodes.has(name) && !ended(nodes.get(name).state)) continue;
          const lane = lanes.get(name);
          if (lane && lane.board === b.root) for (const i of lane.issues) if (!heldIssue.has(String(i.id))) orphan.push(i);
        }
        const decks = [];
        const label = boardLabel(b);
        if (orphan.length) decks.push(deck(`${b.root}/orphan`, "끝난 세션이 쥔 일감", nestIssues(orphan.map(issueChip)), "issue"));
        if (b.foreign.length) decks.push(deck(`${b.root}/foreign`, "사람·외부 배정", nestIssues(b.foreign.map(issueChip)), "issue"));
        const byPri = new Map();
        for (const i of b.unassigned) {
          const p = Number.isFinite(i.priority) ? i.priority : 2;
          if (!byPri.has(p)) byPri.set(p, []);
          byPri.get(p).push(i);
        }
        for (const p of [...byPri.keys()].sort((a, c) => a - c)) {
          decks.push(deck(`${b.root}/P${p}`, `미배정 P${p}`, nestIssues(byPri.get(p).map(issueChip)), "issue"));
        }
        if (decks.length) projectFor(pname).pool.push({ board: b.root, label, error: b.error, decks });
      }
    }
    const out = [];
    for (const p of projects.values()) {
      const meshes = [...p.meshes.values()];
      for (const m of meshes) {
        const { nodes: rs, decks } = arrangeSiblings(m.roots, `${p.name}/${m.name}`, stackMinor, stackIdle);
        m.roots = rs;
        m.decks = decks;
        for (const r of rs) settle(r);
        for (const d of decks) for (const r of d.items) settle(r);
        m.counts = tally(m.roots.concat(...m.decks.map((d) => d.items)));
      }
      // The busiest mesh first; sessions with no mesh last.
      meshes.sort((a, b) => (!a.name - !b.name) || (b.counts.live - a.counts.live)
        || a.name.localeCompare(b.name));
      const pc = tally(meshes.flatMap((m) => m.roots.concat(...m.decks.map((d) => d.items))));
      out.push({ name: p.name, meshes, pool: p.pool, counts: pc,
                 poolCount: p.pool.reduce((n, b) => n + b.decks.reduce((k, d) => k + d.count, 0), 0) });
    }
    out.sort((a, b) => (b.counts.live - a.counts.live) || a.name.localeCompare(b.name));
    return { projects: out, counts, hiddenEnded, hiddenArchived, killedTotal,
             projectNames: [...new Set([...all.values()].map(projectOf))].sort() };
  }

  function isAncestor(nodes, name, of) {
    // Whether `name` is an ancestor of `of` -- a parent loop guard.
    const seen = new Set();
    let cur = nodes.get(of);
    while (cur && cur.parent && !seen.has(cur.name)) {
      seen.add(cur.name);
      if (cur.parent === name) return true;
      cur = nodes.get(cur.parent);
    }
    return false;
  }

  /* State counts over a set of trees (decks included), with `live` the
     sessions whose program is up. */
  function tally(nodes) {
    const c = Object.fromEntries(STATES.map((s) => [s, 0]));
    c.live = 0; c.total = 0; c.issues = 0;
    const walk = (n) => {
      c[n.state]++; c.total++;
      if (n.state === "busy" || n.state === "idle" || n.state === "starting") c.live++;
      c.issues += countIssues(n.issues);
      for (const k of n.children) walk(k);
      for (const d of n.decks) for (const k of d.items) walk(k);
    };
    for (const n of nodes) walk(n);
    return c;
  }

  /* ---------------------------------------------------------------- */
  /* page                                                             */
  /* ---------------------------------------------------------------- */
  const state = loadState();
  let timer = null;
  let beadsTimer = null;
  let queues = null;
  let queuesError = "";
  let queuesBusy = false;
  let archivedRecords = [];
  let archivedBusy = false;
  let lastKey = "";
  let lastModel = null;
  let picked = null;           // {kind, id}
  const openDecks = new Set();
  let shell = null;            // the elements built once per page life

  function loadState() {
    const base = { ended: "6h", archived: false, issues: true, pool: true, stackMinor: true,
                   stackIdle: true, project: "", view: { x: 24, y: 16, k: 1 } };
    try {
      const raw = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
      const v = raw.view || {};
      return { ...base, ...raw,
               view: { x: Number(v.x) || 24, y: Number(v.y) || 16,
                       k: clampZoom(Number(v.k) || 1) } };
    } catch { return base; }
  }

  function remember() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch {}
  }

  function clampZoom(k) {
    return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Number.isFinite(k) ? k : 1));
  }

  function endedMs() {
    const w = ENDED_WINDOWS.find((x) => x[0] === state.ended) || ENDED_WINDOWS[2];
    return w[2];
  }

  function node(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }

  function view() {
    return typeof document !== "undefined" ? document.getElementById("hive-view") : null;
  }

  /* app.js globals, read at call time; each guarded so a missing one costs
     its feature and not the page. */
  function liveSessions() {
    const live = typeof sessionsCache !== "undefined" ? sessionsCache || [] : [];
    if (!state.archived) return live;
    const seen = new Set(live.map((s) => s.name));
    return live.concat(archivedRecords.filter((s) => !seen.has(s.name)));
  }

  function appMeshesOf(name) {
    return typeof sessMeshes === "function" ? sessMeshes(name) || [] : [];
  }

  function appCflowOf(name) {
    const v2 = typeof cflow2RailCache !== "undefined" ? (cflow2RailCache || {})[name] : null;
    if (v2 && typeof cflow2RailMarkState === "function") {
      return { state: cflow2RailMarkState(v2),
               label: `${v2.workflow || "cflow2"} · ${cflow2RailLabel(v2)}` };
    }
    const v1 = typeof sessCflowRun === "function" ? sessCflowRun(name) : null;
    if (v1 && typeof wfMarkState === "function") {
      return { state: wfMarkState(v1.status, v1),
               label: `${v1.workflow || "cflow"} · ${sessCflowLabel(v1)}` };
    }
    return null;
  }

  function glyphOfFlow(st) {
    return typeof WF_GLYPH !== "undefined" ? WF_GLYPH[st] || "▸" : "▸";
  }

  async function readQueuesNow() {
    if (queuesBusy || typeof api !== "function") return;
    queuesBusy = true;
    try {
      // Out on its own connection: a 3 MB answer has no business in a batch.
      const resp = await api("/api/beads/queues", { noBatch: true });
      const data = await resp.json().catch(() => ({}));
      if (resp.ok) { queues = data; queuesError = data.error || ""; }
      else queuesError = data.error || `HTTP ${resp.status}`;
    } catch (err) {
      queuesError = String((err && err.message) || err);
    } finally {
      queuesBusy = false;
    }
    render();
  }

  async function loadArchived() {
    if (archivedBusy || typeof api !== "function") return;
    archivedBusy = true;
    try {
      const resp = await api("/api/sessions?view=rail&state=archived");
      const data = await resp.json().catch(() => ({}));
      if (resp.ok) archivedRecords = data.sessions || [];
    } catch { /* keep what we had */ }
    finally { archivedBusy = false; }
    render();
  }

  /* -- chips -------------------------------------------------------- */

  function sessionTitle(n) {
    const parts = [`${n.name} — ${STATE_LABEL[n.state]}`];
    if (n.ghost) parts.push("(끝났지만 살아 있는 자식이 있어 남김)");
    if (n.role) parts.push(`role: ${n.role}`);
    if (n.meshes.length) parts.push(`mesh: ${n.meshes.join(", ")}`);
    if (n.fault) parts.push(`fault: ${n.fault}`);
    if (n.compacting) parts.push("compacting");
    if (n.cflow) parts.push(`cflow: ${n.cflow.label}`);
    if (n.issue) parts.push(`issue: ${n.issue}`);
    return parts.join("\n");
  }

  function sessionChip(n) {
    const c = node("div", `hive-chip s-${n.state}${n.minor ? " minor" : ""}${n.ghost ? " ghost" : ""}`);
    c.dataset.kind = "session";
    c.dataset.id = n.name;
    c.tabIndex = 0;
    c.title = sessionTitle(n);
    const core = node("span", "hive-core");
    core.appendChild(node("i", null, STATE_GLYPH[n.state]));
    c.appendChild(core);
    const body = node("span", "hive-body");
    body.appendChild(node("b", "hive-name", n.name));
    if (!n.minor) {
      const sub = [n.role || "", STATE_LABEL[n.state]].filter(Boolean).join(" · ");
      body.appendChild(node("span", "hive-sub", sub));
    }
    c.appendChild(body);
    const badges = [];
    if (n.fault) badges.push(["fault", "!"]);
    if (n.compacting) badges.push(["compact", "⟳"]);
    if (n.cflow) badges.push([`flow f-${n.cflow.state}`, glyphOfFlow(n.cflow.state)]);
    if (n.created) badges.push(["created", `+${n.created}`]);
    if (badges.length) {
      const b = node("span", "hive-badges");
      for (const [cls, t] of badges) b.appendChild(node("span", `hive-badge ${cls}`, t));
      c.appendChild(b);
    }
    if (picked && picked.kind === "session" && picked.id === n.name) c.classList.add("picked");
    return c;
  }

  function issueChipEl(i) {
    const c = node("div", `hive-chip hive-issue st-${i.status} p-${Math.min(4, Math.max(0, i.priority))}`);
    c.dataset.kind = "issue";
    c.dataset.id = i.id;
    c.tabIndex = 0;
    c.title = `${i.id} — ${i.status} · P${i.priority} · ${i.type}\n${i.title}${i.assignee ? `\nassignee: ${i.assignee}` : ""}`;
    const core = node("span", "hive-core");
    core.appendChild(node("i", null, `P${i.priority}`));
    c.appendChild(core);
    const body = node("span", "hive-body");
    body.appendChild(node("b", "hive-name", i.id));
    body.appendChild(node("span", "hive-sub", i.title));
    c.appendChild(body);
    if (picked && picked.kind === "issue" && picked.id === i.id) c.classList.add("picked");
    return c;
  }

  function issueTree(i) {
    const wrap = node("div", "hive-itree");
    wrap.appendChild(issueChipEl(i));
    if (i.children.length) {
      const kids = node("div", "hive-ikids");
      for (const k of i.children) kids.appendChild(issueTree(k));
      wrap.appendChild(kids);
    }
    return wrap;
  }

  function deckEl(d) {
    const open = openDecks.has(d.key);
    const el = node("div", `hive-deck of-${d.of}${open ? " open" : ""}`);
    const face = node("div", "hive-chip hive-deck-face");
    face.dataset.deck = d.key;
    face.tabIndex = 0;
    face.title = `${d.label} ${d.count}개 — 눌러서 ${open ? "접기" : "펼치기"}`;
    const core = node("span", "hive-core");
    core.appendChild(node("i", null, String(d.count)));
    face.appendChild(core);
    const body = node("span", "hive-body");
    body.appendChild(node("b", "hive-name", `${d.label} ×${d.count}`));
    const dots = node("span", "hive-deck-dots");
    for (const [st, n] of Object.entries(d.states)) {
      const dot = node("span", `hive-dot ${d.of === "issue" ? "st-" : "s-"}${st}`, String(n));
      dot.title = `${st} ${n}`;
      dots.appendChild(dot);
    }
    body.appendChild(dots);
    face.appendChild(body);
    face.appendChild(node("span", "hive-deck-toggle", open ? "▾" : "▸"));
    el.appendChild(face);
    if (open) {
      const items = node("div", "hive-deck-items");
      const shownItems = d.items.slice(0, DECK_OPEN_CAP);
      for (const it of shownItems) items.appendChild(it.kind === "issue" ? issueTree(it) : treeEl(it));
      if (d.items.length > shownItems.length) {
        items.appendChild(node("span", "hive-more", `+${d.items.length - shownItems.length}개 더 — Beads 탭에서 전체 보기`));
      }
      el.appendChild(items);
    }
    return el;
  }

  /* One session and everything under it: its issues beside it, its
     children and their decks one level smaller below. */
  function treeEl(n) {
    const t = node("div", `hive-tree${n.minor ? " minor" : ""}`);
    const head = node("div", "hive-head");
    head.appendChild(sessionChip(n));
    if (n.issues.length) {
      const att = node("div", "hive-attach");
      for (const i of n.issues) att.appendChild(issueTree(i));
      head.appendChild(att);
    }
    t.appendChild(head);
    if (n.children.length || n.decks.length) {
      const kids = node("div", "hive-kids");
      for (const k of n.children) kids.appendChild(treeEl(k));
      for (const d of n.decks) kids.appendChild(deckEl(d));
      t.appendChild(kids);
    }
    return t;
  }

  function countPills(c, cls) {
    const row = node("span", cls || "hive-counts");
    for (const st of STATES) {
      if (!c[st]) continue;
      const p = node("span", `hive-dot s-${st}`, `${STATE_GLYPH[st]} ${c[st]}`);
      p.title = `${STATE_LABEL[st]} ${c[st]}`;
      row.appendChild(p);
    }
    return row;
  }

  function projectEl(p) {
    const cell = node("section", "hive-cell hive-project");
    const head = node("header", "hive-cell-head");
    head.appendChild(node("span", "hive-hex", "⬢"));
    head.appendChild(node("h3", null, p.name));
    head.appendChild(countPills(p.counts));
    if (p.counts.issues || p.poolCount) {
      head.appendChild(node("span", "hive-muted",
        `일감 ${p.counts.issues} 진행 · 풀 ${p.poolCount}`));
    }
    cell.appendChild(head);
    const meshes = node("div", "hive-meshes");
    for (const m of p.meshes) {
      const mc = node("section", `hive-cell hive-mesh${m.name ? "" : " nomesh"}`);
      const mh = node("header", "hive-cell-head");
      mh.appendChild(node("span", "hive-hex", m.name ? "⬡" : "·"));
      mh.appendChild(node("h4", null, m.name || "메시 없음"));
      mh.appendChild(countPills(m.counts));
      mc.appendChild(mh);
      const forest = node("div", "hive-forest");
      for (const r of m.roots) forest.appendChild(treeEl(r));
      for (const d of m.decks) forest.appendChild(deckEl(d));
      mc.appendChild(forest);
      meshes.appendChild(mc);
    }
    for (const b of p.pool) {
      const pc = node("section", "hive-cell hive-mesh hive-pool");
      const ph = node("header", "hive-cell-head");
      ph.appendChild(node("span", "hive-hex", "◈"));
      ph.appendChild(node("h4", null, `${b.label} 보드 · 잡히지 않은 일감`));
      pc.appendChild(ph);
      if (b.error) pc.appendChild(node("p", "hive-error", b.error));
      const forest = node("div", "hive-forest");
      for (const d of b.decks) forest.appendChild(deckEl(d));
      pc.appendChild(forest);
      meshes.appendChild(pc);
    }
    cell.appendChild(meshes);
    return cell;
  }

  /* -- detail -------------------------------------------------------- */

  function findPicked(model) {
    if (!picked || !model) return null;
    let hit = null;
    const visitIssue = (i) => { if (!hit && picked.kind === "issue" && i.id === picked.id) hit = i; i.children.forEach(visitIssue); };
    const visit = (n) => {
      if (hit) return;
      if (picked.kind === "session" && n.name === picked.id) { hit = n; return; }
      n.issues.forEach(visitIssue);
      n.children.forEach(visit);
      n.decks.forEach((d) => d.items.forEach(visit));
    };
    for (const p of model.projects) {
      for (const m of p.meshes) { m.roots.forEach(visit); m.decks.forEach((d) => d.items.forEach(visit)); }
      for (const b of p.pool) for (const d of b.decks) d.items.forEach(visitIssue);
    }
    return hit;
  }

  function renderDetail(model) {
    const side = shell.detail;
    side.innerHTML = "";
    const it = findPicked(model);
    side.classList.toggle("hidden", !it);
    if (!it) return;
    const close = node("button", "hive-detail-close", "×");
    close.type = "button";
    close.title = "닫기";
    close.onclick = () => { picked = null; lastKey = ""; render(true); };
    side.appendChild(close);
    const dl = node("dl", "hive-detail-list");
    const row = (k, v) => {
      if (v === "" || v === null || v === undefined) return;
      dl.appendChild(node("dt", null, k));
      dl.appendChild(node("dd", null, String(v)));
    };
    if (it.kind === "session") {
      side.appendChild(node("h3", null, it.name));
      row("상태", STATE_LABEL[it.state] + (it.ghost ? " (자식 때문에 남김)" : ""));
      row("role", it.role || "free-role");
      row("project", it.project);
      row("mesh", it.meshes.join(", "));
      row("parent", it.parent);
      row("자식", it.children.length + it.decks.reduce((n, d) => n + d.items.length, 0) || "");
      row("issue", it.issue);
      row("큐", it.issues.length ? `${countIssues(it.issues)}건` : "");
      row("만든 후속 일감", it.created || "");
      row("cflow", it.cflow ? it.cflow.label : "");
      row("fault", it.fault);
      side.appendChild(dl);
      const actions = node("div", "hive-detail-actions");
      const open = node("a", "hive-btn", "세션 열기 →");
      open.href = "#/s/" + encodeURIComponent(it.name);
      actions.appendChild(open);
      if ((it.state === "killed" || it.state === "failed") && typeof archiveSession === "function") {
        const arch = node("button", "hive-btn", "보관(archive)");
        arch.type = "button";
        arch.onclick = async () => {
          arch.disabled = true;
          if (await archiveSession(it.name)) { picked = null; lastKey = ""; }
          render(true);
        };
        actions.appendChild(arch);
      }
      side.appendChild(actions);
    } else {
      side.appendChild(node("h3", null, it.id));
      side.appendChild(node("p", "hive-detail-title", it.title));
      row("상태", it.status);
      row("우선순위", `P${it.priority}`);
      row("type", it.type);
      row("assignee", it.assignee || "(없음)");
      row("하위 일감", it.children.length ? `${countIssues(it.children)}건` : "");
      side.appendChild(dl);
      const actions = node("div", "hive-detail-actions");
      const open = node("a", "hive-btn", "Beads에서 열기 →");
      open.href = "#/beads/" + encodeURIComponent(it.id);
      actions.appendChild(open);
      side.appendChild(actions);
    }
  }

  /* -- pan & zoom ---------------------------------------------------- */

  function applyView() {
    if (!shell) return;
    const { x, y, k } = state.view;
    shell.stage.style.transform = `translate(${x}px, ${y}px) scale(${k})`;
    shell.port.classList.toggle("z-far", k < 0.45);
    shell.port.classList.toggle("z-mid", k >= 0.45 && k < 0.75);
    shell.zoomLabel.textContent = `${Math.round(k * 100)}%`;
  }

  function zoomAt(factor, cx, cy) {
    const v = state.view;
    const k = clampZoom(v.k * factor);
    const f = k / v.k;
    v.x = cx - (cx - v.x) * f;
    v.y = cy - (cy - v.y) * f;
    v.k = k;
    applyView();
    remember();
  }

  function fit() {
    if (!shell) return;
    const pw = shell.port.clientWidth, ph = shell.port.clientHeight;
    const sw = shell.stage.scrollWidth, sh = shell.stage.scrollHeight;
    if (!pw || !sw) return;
    const k = clampZoom(Math.min(1, (pw - 32) / sw, (ph - 32) / sh));
    state.view = { x: Math.max(16, (pw - sw * k) / 2), y: 16, k };
    applyView();
    remember();
  }

  function wirePort(port) {
    let drag = null;
    port.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || e.target.closest(".hive-chip, button, a, select, input")) return;
      drag = { x: e.clientX, y: e.clientY, vx: state.view.x, vy: state.view.y, id: e.pointerId };
      port.classList.add("panning");
      try { port.setPointerCapture(e.pointerId); } catch {}
    });
    port.addEventListener("pointermove", (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      state.view.x = drag.vx + e.clientX - drag.x;
      state.view.y = drag.vy + e.clientY - drag.y;
      applyView();
    });
    const end = () => {
      if (!drag) return;
      drag = null;
      port.classList.remove("panning");
      remember();
    };
    port.addEventListener("pointerup", end);
    port.addEventListener("pointercancel", end);
    port.addEventListener("wheel", (e) => {
      e.preventDefault();
      const box = port.getBoundingClientRect();
      zoomAt(Math.exp(-e.deltaY * 0.0015), e.clientX - box.left, e.clientY - box.top);
    }, { passive: false });
    // One delegated handler for every chip and deck the stage will ever
    // hold: the stage is rebuilt, the listener is not.
    const activate = (e) => {
      const face = e.target.closest(".hive-deck-face");
      if (face) {
        const key = face.dataset.deck;
        if (openDecks.has(key)) openDecks.delete(key); else openDecks.add(key);
        lastKey = "";
        render(true);
        return;
      }
      const chip = e.target.closest(".hive-chip[data-kind]");
      if (!chip) return;
      const same = picked && picked.kind === chip.dataset.kind && picked.id === chip.dataset.id;
      picked = same ? null : { kind: chip.dataset.kind, id: chip.dataset.id };
      lastKey = "";
      render(true);
    };
    port.addEventListener("click", activate);
    port.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        if (e.target.closest(".hive-chip")) { e.preventDefault(); activate(e); }
        return;
      }
      if (e.target.closest("input, select")) return;
      const box = port.getBoundingClientRect();
      if (e.key === "+" || e.key === "=") zoomAt(1.2, box.width / 2, box.height / 2);
      else if (e.key === "-") zoomAt(1 / 1.2, box.width / 2, box.height / 2);
      else if (e.key === "0") fit();
    });
  }

  /* -- shell --------------------------------------------------------- */

  function select(options, value, onChange, title) {
    const s = node("select", "hive-select");
    if (title) s.title = title;
    for (const [v, label] of options) {
      const o = node("option", null, label);
      o.value = v;
      s.appendChild(o);
    }
    s.value = value;
    s.onchange = () => onChange(s.value);
    return s;
  }

  function toggle(label, key, title, after) {
    const l = node("label", "hive-toggle");
    if (title) l.title = title;
    const cb = node("input");
    cb.type = "checkbox";
    cb.checked = !!state[key];
    cb.onchange = () => { state[key] = cb.checked; remember(); if (after) after(); render(true); };
    l.appendChild(cb);
    l.appendChild(document.createTextNode(" " + label));
    return l;
  }

  function buildShell(root) {
    root.innerHTML = "";
    const head = node("div", "wf-head");
    head.appendChild(node("h2", null, "Hive"));
    head.appendChild(node("span", "hive-muted",
      "프로젝트 ⬢ → 메시 ⬡ → 세션 트리와 일감을 한 캔버스에서 본다. 끌어서 이동, 휠로 확대·축소."));
    root.appendChild(head);
    const bar = node("div", "hive-toolbar");
    const project = node("span", "hive-tool");
    bar.appendChild(project);
    const endedPick = select(ENDED_WINDOWS.map(([v, l]) => [v, l]), state.ended,
      (v) => { state.ended = v; remember(); render(true); },
      "killed·failed 세션을 끝난 뒤 얼마 동안 남길지");
    const endedWrap = node("label", "hive-tool", "끝난 세션 ");
    endedWrap.appendChild(endedPick);
    bar.appendChild(endedWrap);
    bar.appendChild(toggle("archived", "archived", "보관된 세션도 그린다",
      () => { if (state.archived && !archivedRecords.length) loadArchived(); }));
    bar.appendChild(toggle("일감", "issues", "세션이 쥔 일감과 보드 풀을 그린다"));
    bar.appendChild(toggle("풀", "pool", "보드의 미배정 일감을 우선순위별 묶음으로 그린다"));
    bar.appendChild(toggle("보조 role 묶기", "stackMinor", "reviewer·oneshot 세션을 형제끼리 한 묶음으로 접는다"));
    bar.appendChild(toggle("쉬는 세션 묶기", "stackIdle", "살아 있는 자손이 없는 hold·paused 형제를 한 묶음으로 접는다"));
    const search = node("input", "hive-search");
    search.type = "search";
    search.placeholder = "찾기: 세션·일감·메시";
    search.oninput = () => render(true);
    bar.appendChild(search);
    const archiveBtn = node("button", "hive-btn hive-archive", "");
    archiveBtn.type = "button";
    archiveBtn.title = "killed 세션을 전부 보관한다(세션 레일의 'archive exited'와 같은 동작)";
    archiveBtn.onclick = () => {
      const b = typeof document !== "undefined" && document.getElementById("archive-exited");
      if (b) b.click();
      setTimeout(() => render(true), 1500);
    };
    bar.appendChild(archiveBtn);
    root.appendChild(bar);
    const status = node("div", "hive-status");
    root.appendChild(status);
    const frame = node("div", "hive-frame");
    const port = node("div", "hive-port");
    port.tabIndex = 0;
    port.setAttribute("aria-label", "Hive canvas");
    const stage = node("div", "hive-stage");
    port.appendChild(stage);
    const zoom = node("div", "hive-zoom");
    const zb = (t, title, fn) => {
      const b = node("button", "hive-btn", t);
      b.type = "button";
      b.title = title;
      b.onclick = fn;
      zoom.appendChild(b);
      return b;
    };
    zb("−", "축소 (-)", () => zoomAt(1 / 1.2, port.clientWidth / 2, port.clientHeight / 2));
    const zoomLabel = node("span", "hive-zoom-label", "100%");
    zoom.appendChild(zoomLabel);
    zb("+", "확대 (+)", () => zoomAt(1.2, port.clientWidth / 2, port.clientHeight / 2));
    zb("⤢", "전체 맞춤 (0)", fit);
    port.appendChild(zoom);
    port.appendChild(legend());
    frame.appendChild(port);
    const detail = node("aside", "hive-detail hidden");
    frame.appendChild(detail);
    root.appendChild(frame);
    wirePort(port);
    shell = { root, project, status, port, stage, detail, zoomLabel, search, archiveBtn };
    applyView();
  }

  function legend() {
    const box = node("div", "hive-legend");
    for (const st of STATES) {
      const p = node("span", `hive-dot s-${st}`, `${STATE_GLYPH[st]} ${STATE_LABEL[st]}`);
      box.appendChild(p);
    }
    box.appendChild(node("span", "hive-legend-sep", "|"));
    box.appendChild(node("span", "hive-badge fault", "!"));
    box.appendChild(node("span", "hive-legend-note", "fault"));
    box.appendChild(node("span", "hive-badge compact", "⟳"));
    box.appendChild(node("span", "hive-legend-note", "compacting"));
    box.appendChild(node("span", "hive-badge flow f-yours", "◆"));
    box.appendChild(node("span", "hive-legend-note", "응답 대기"));
    return box;
  }

  function currentProjectPick() {
    return state.project;
  }

  function renderProjectPick(model) {
    const wrap = shell.project;
    const want = [["", "모든 프로젝트"], ...model.projectNames.map((p) => [p, p])];
    const sig = JSON.stringify(want);
    if (wrap.dataset.sig === sig) return;
    wrap.dataset.sig = sig;
    wrap.innerHTML = "";
    if (state.project && !model.projectNames.includes(state.project)) state.project = "";
    wrap.appendChild(select(want, state.project, (v) => { state.project = v; remember(); render(true); }));
  }

  function render(force = false) {
    const root = view();
    if (!root || root.classList.contains("hidden")) return;
    if (!shell || shell.root !== root || !root.contains(shell.stage)) buildShell(root);
    const focused = typeof document !== "undefined" ? document.activeElement : null;
    if (!force && focused && focused.tagName === "SELECT" && root.contains(focused)) return;
    const model = buildModel(liveSessions(), queues, {
      now: Date.now(), endedMs: endedMs(), archived: state.archived,
      issues: state.issues, pool: state.pool, stackMinor: state.stackMinor,
      stackIdle: state.stackIdle,
      project: currentProjectPick(), meshesOf: appMeshesOf, cflowOf: appCflowOf,
    });
    lastModel = model;
    renderProjectPick(model);
    const q = shell.search.value.trim().toLowerCase();
    const key = JSON.stringify([model.projects, q, picked, [...openDecks]]);
    const c = model.counts;
    const live = c.busy + c.idle + c.starting;
    const bits = [`살아 있음 ${live} (busy ${c.busy} · idle ${c.idle})`,
                  `hold ${c.hold}`, `paused ${c.paused}`];
    if (c.delayed) bits.push(`delayed ${c.delayed}`);
    if (c.failed) bits.push(`failed ${c.failed}`);
    bits.push(`killed ${c.killed} 표시`);
    if (model.hiddenEnded) bits.push(`끝난 세션 ${model.hiddenEnded} 숨김`);
    if (model.hiddenArchived) bits.push(`archived ${model.hiddenArchived} 숨김`);
    if (queuesError) bits.push(`일감 읽기 실패: ${queuesError}`);
    else if (!queues && state.issues) bits.push("일감 읽는 중…");
    shell.status.textContent = bits.join(" · ");
    shell.archiveBtn.textContent = `killed ${model.killedTotal}개 보관`;
    shell.archiveBtn.disabled = !model.killedTotal;
    if (key === lastKey) return;
    lastKey = key;
    const frag = document.createDocumentFragment();
    if (!model.projects.length) frag.appendChild(node("p", "hive-muted hive-empty", "그릴 세션이 없다."));
    for (const p of model.projects) frag.appendChild(projectEl(p));
    shell.stage.replaceChildren(frag);
    shell.port.classList.toggle("searching", !!q);
    if (q) {
      // A chip's title already names everything worth finding it by: the
      // session's role, meshes and issue, the issue's title and assignee.
      for (const chip of shell.stage.querySelectorAll(".hive-chip[data-kind]")) {
        if (chip.title.toLowerCase().includes(q)) chip.classList.add("hit");
      }
    }
    renderDetail(model);
  }

  function open() {
    const root = view();
    if (root && (!shell || shell.root !== root)) buildShell(root);
    lastKey = "";
    if (state.archived && !archivedRecords.length) loadArchived();
    render(true);
    readQueuesNow();
    if (timer) clearInterval(timer);
    timer = setInterval(() => render(), REDRAW_MS);
    if (beadsTimer) clearInterval(beadsTimer);
    beadsTimer = setInterval(() => { if (state.issues) readQueuesNow(); }, BEADS_MS);
  }

  function stop() {
    if (timer) clearInterval(timer);
    if (beadsTimer) clearInterval(beadsTimer);
    timer = null;
    beadsTimer = null;
  }

  return { open, stop, render, fit, category, stateOf, keep, isMinor, roleOf,
           issueParentId, nestIssues, readQueues, buildModel, tally,
           arrangeSiblings, ENDED_WINDOWS, STATES, get model() { return lastModel; } };
})();
