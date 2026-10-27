/* Mega threads (#/megathreads, claunch-v5nhp): one input box over several
   (session, thread) pairs. The daemon asks Jev whether a message is for each
   member (mine / not_mine); a single member over the threshold takes it
   alone, two or more come back for the user to pick, and nobody over the
   threshold still leaves the best-ranked members offered.

   The page is the round table: the user at the centre, one node per member
   around it, and the last routing's log-odds drawn as the spoke to each
   node. It talks only to the API circuit 1 built (/api/megathreads,
   docs/mega-thread.md): it never computes a probability itself.

   The model functions at the top take plain data so
   tests/web/megathread_check.js can run them bare. */
globalThis.MegaThreadPage = (() => {
  const POLL_MS = 5000;
  const ROUTE_LOG = 20;
  const STAGE_W = 560;
  const STAGE_H = 420;
  const RING_RX = 190;
  const RING_RY = 130;

  /* ---- model (no DOM) -------------------------------------------------- */
  function ringLayout(n, cx = STAGE_W / 2, cy = STAGE_H / 2, rx = RING_RX, ry = RING_RY) {
    const out = [];
    for (let i = 0; i < n; i++) {
      const angle = -Math.PI / 2 + (2 * Math.PI * i) / Math.max(1, n);
      out.push({ angle, x: cx + Math.cos(angle) * rx, y: cy + Math.sin(angle) * ry });
    }
    return out;
  }

  /* Spoke weight from the last routing: wider means more likely this member,
     and the sign keeps "not mine" from looking like a weak "mine". */
  function edgeWidth(logit) {
    if (logit === null || logit === undefined || Number.isNaN(Number(logit))) return 1;
    return Math.max(1, Math.min(9, 1 + Math.abs(Number(logit)) * 1.4));
  }

  function nodeTone(logit, threshold) {
    if (logit === null || logit === undefined) return "cold";
    const value = Number(logit);
    if (value >= threshold) return "hit";
    return value >= 0 ? "warm" : "cold";
  }

  function pct(p) {
    const value = Number(p);
    if (Number.isNaN(value)) return "–";
    return `${(value * 100).toFixed(value >= 0.995 || value <= 0.005 ? 0 : 1)}%`;
  }

  function logitText(logit) {
    if (logit === null || logit === undefined) return "–";
    const value = Number(logit);
    return (value >= 0 ? "+" : "") + value.toFixed(2);
  }

  /* What the composer sends: the plain message, or the message plus the
     threads the user ticked after an ambiguous answer. */
  function routeBody(text, picks) {
    const body = { text: String(text || "") };
    const chosen = (picks || []).filter(Boolean);
    if (chosen.length) body.choose = chosen;
    return body;
  }

  /* The rows the selection panel shows. `auto` delivered already, `error`
     has nothing to offer; `choose` and `none` offer the members the daemon
     ranked, with the thread id the second request needs. */
  function optionRows(judgment) {
    if (!judgment || (judgment.verdict !== "choose" && judgment.verdict !== "none")) return [];
    return (judgment.options || []).map((r) => ({
      thread: r.thread,
      session: r.session,
      p: r.p_mine,
      logit: r.logit,
      label: `${r.session} · ${shortTitle(r)}`,
    }));
  }

  function shortTitle(row) {
    const title = String((row && (row.thread_title || row.title)) || "");
    return title || String((row && row.thread) || "");
  }

  function verdictLine(judgment) {
    if (!judgment) return "";
    const threshold = Number(judgment.threshold);
    switch (judgment.verdict) {
      case "auto":
        return `단독 라우팅 — ${(judgment.options || []).map((o) => o.session).join(", ")}`;
      case "choose":
        return `후보 ${(judgment.options || []).length}명 — 보낼 곳을 고르세요`;
      case "none":
        return `임계값(${Number.isNaN(threshold) ? "?" : threshold.toFixed(2)})을 넘은 멤버가 없습니다 — 아래 후보에서 고르거나 취소하세요`;
      case "manual":
        return "선택한 곳으로 보냈습니다";
      case "error":
        return "판정 실패 — Jev 호출이 모두 실패했습니다";
      default:
        return String(judgment.verdict || "");
    }
  }

  function deliveryLine(sent) {
    const list = sent || [];
    if (!list.length) return "";
    return list.map((d) => {
      const who = d.session || d.thread;
      if (d.error) return `${who}: 실패 (${d.error})`;
      if (d.queued) return `${who}: 종료된 세션 — 다시 시작(respawn/restore)하면 전달됩니다`;
      if (!d.delivered) return `${who}: 대기 중 (세션이 응답할 수 없음)`;
      return d.hold ? `${who}: 전달됨 · ${d.hold}` : `${who}: 전달됨`;
    }).join(" · ");
  }

  // The routing log write can fail after the message went out. Saying so
  // keeps the empty log entry from reading as "nothing was sent".
  function ledgerLine(res) {
    const err = (res || {}).ledger_error;
    return err ? ` · 라우팅 기록 실패 (${err})` : "";
  }

  function esc(text) {
    return String(text === undefined || text === null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /* ---- DOM ------------------------------------------------------------- */
  const state = {
    clusters: [],
    mega: null,
    routes: [],
    judgment: null,
    picks: new Set(),
    draft: "",
    notice: "",
    error: "",
    title: "",
    sessions: null,
    filter: "",
    picked: new Set(),
    busy: false,
    /* ---- the create panel (claunch-vz96l) ---------------------------- */
    createOpen: false,
    // session name -> {mesh, role}; a session in no mesh is absent, which is
    // the same answer the mesh filter reads as "메시 없음".
    meshOf: {},
    meshNames: [],
    // session name -> the cflow2 line the rail draws (cflow2_web.rail_entry).
    cflowOf: {},
    project: "",
    mesh: "",
    // Which of the seven categories the reader keeps. Empty means all of
    // them, so a reader who has not filtered sees the whole fleet.
    statuses: new Set(),
    sort: "activity-desc",
    hierarchy: true,
    topic: "",
    // The last preview the daemon answered, and what went wrong if it did
    // not. Kept after a failure so 재시도 re-sends the same topic.
    draftPreview: null,
    draftError: "",
    draftBusy: false,
  };
  let timer = null;
  let wantId = "";
  // The create panel's parts while it is open, so a repaint after a tick or
  // a preview reaches the same nodes the reader is using.
  let createParts = null;

  function view() {
    return typeof document !== "undefined" && document.getElementById("megathreads-view");
  }

  function node(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = String(text);
    return e;
  }

  async function apiGet(path) {
    const resp = await api(path);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
  }

  async function apiSend(path, body) {
    const resp = await api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
  }

  /* True while a person is typing into the page. The poll refreshes the
     data but must not redraw over a half-typed message or a title. */
  function typing() {
    if (typeof document === "undefined") return false;
    const active = document.activeElement;
    return !!(active && active.closest && active.closest(".mt-page"));
  }

  async function load(quiet = false) {
    try {
      const listing = await apiGet("/api/megathreads");
      state.clusters = listing.megathreads || [];
      state.error = "";
      const id = wantId || (state.mega && state.mega.id) || (state.clusters[0] && state.clusters[0].id) || "";
      if (id) await loadOne(id);
      else { state.mega = null; state.routes = []; }
    } catch (err) {
      state.error = String(err.message || err);
    }
    if (!quiet || !typing()) render();
  }

  async function loadOne(id) {
    const [detail, routes] = await Promise.all([
      apiGet(`/api/megathreads/${encodeURIComponent(id)}`),
      apiGet(`/api/megathreads/${encodeURIComponent(id)}/routes?limit=${ROUTE_LOG}`),
    ]);
    state.mega = detail.mega;
    state.routes = routes.routes || [];
    wantId = state.mega.id;
  }

  async function sendText() {
    if (!state.mega || state.busy || !isOpen(state.mega)) return;
    const text = state.draft.trim();
    if (!text) return;
    state.busy = true;
    state.picks.clear();
    state.error = "";
    try {
      state.judgment = await apiSend(`/api/megathreads/${encodeURIComponent(state.mega.id)}/route`, routeBody(text));
      // A member whose call failed is not scored, and a "choose" may be the
      // router holding back for exactly that reason. Its error is shown for
      // every verdict, not only when every call failed.
      const failed = (state.judgment.errors || [])
        .map((e) => `${e.thread}: ${e.error}`).join(" · ");
      if (state.judgment.delivered && state.judgment.delivered.length) {
        state.notice = deliveryLine(state.judgment.delivered) + ledgerLine(state.judgment);
        state.draft = "";
      } else if (state.judgment.verdict === "choose" || state.judgment.verdict === "none") {
        state.notice = failed;
      } else if (state.judgment.verdict === "error") {
        state.notice = failed;
      }
      await loadOne(state.mega.id);
    } catch (err) {
      state.error = String(err.message || err);
    }
    state.busy = false;
    render();
  }

  async function sendPicks() {
    if (!state.mega || state.busy || !isOpen(state.mega) || !state.picks.size) return;
    const text = state.draft.trim();
    if (!text) return;
    state.busy = true;
    state.error = "";
    try {
      const res = await apiSend(`/api/megathreads/${encodeURIComponent(state.mega.id)}/route`,
        routeBody(text, [...state.picks]));
      // Keep the scores so the ring still shows why these were the offers;
      // only the panel closes.
      state.judgment = { ...(state.judgment || {}), verdict: "manual", options: [] };
      state.notice = deliveryLine(res.delivered) + ledgerLine(res);
      state.draft = "";
      state.picks.clear();
      await loadOne(state.mega.id);
    } catch (err) {
      state.error = String(err.message || err);
    }
    state.busy = false;
    render();
  }

  /* Close or reopen the cluster. The status is the only thing that changes;
     members, routes and the budget stay as they are. */
  async function toggleStatus() {
    if (!state.mega || state.busy) return;
    state.busy = true;
    state.error = "";
    const next = isOpen(state.mega) ? "closed" : "open";
    try {
      await apiSend(`/api/megathreads/${encodeURIComponent(state.mega.id)}/status`, { status: next });
      state.notice = next === "closed"
        ? "mega thread를 닫았습니다 — 다시 열 때까지 라우팅하지 않습니다"
        : "mega thread를 다시 열었습니다";
      if (next === "closed") state.picks.clear();
      await loadOne(state.mega.id);
    } catch (err) {
      state.error = String(err.message || err);
    }
    state.busy = false;
    render();
  }

  async function createCluster() {
    const title = state.title.trim();
    if (!title || state.busy) return;
    state.busy = true;
    try {
      // The ticks live in state, so a filter keystroke or the poll cannot
      // drop a selection the DOM would have lost on the last repaint.
      const picked = [...state.picked];
      const res = await apiSend("/api/megathreads",
        { title, members: picked.map((session) => ({ session })) });
      wantId = res.mega.id;
      state.title = "";
      state.filter = "";
      state.topic = "";
      state.picked.clear();
      state.draftPreview = null;
      state.draftError = "";
      state.createOpen = false;
      state.notice = `mega thread를 만들었습니다: ${res.mega.title}`;
      await load();
    } catch (err) {
      state.error = String(err.message || err);
    }
    state.busy = false;
    render();
  }

  /* Ask the daemon which sessions the typed topic belongs to. The candidates
     are the sessions the reader's filters currently keep, in the order they
     are drawn and capped at the daemon's own ceiling: a preview has to score
     what the reader is looking at, not the whole archive of the machine.
     A failure leaves the panel usable — the ticks can still be made by hand
     — and 재시도 re-sends the same body. */
  async function requestDraft() {
    const topic = String(state.topic || "").trim();
    if (!topic || state.draftBusy) return;
    state.draftBusy = true;
    state.draftError = "";
    paintDraft(createParts);
    const scope = pickerRows(state.sessions || [], {
      query: state.filter,
      picked: state.picked,
      project: state.project,
      mesh: state.mesh,
      statuses: state.statuses,
      sort: state.sort,
      hierarchy: false,
    }).map((row) => row.name).slice(0, 40);
    try {
      const res = await apiSend("/api/megathreads/draft", { topic, sessions: scope });
      state.draftPreview = res;
      // The preview replaces the ticks: it is the answer to "what belongs
      // here", and the reader edits it from that starting point.
      state.picked.clear();
      for (const name of res.recommended || []) state.picked.add(name);
    } catch (err) {
      state.draftError = String((err && err.message) || err);
    }
    state.draftBusy = false;
    paintDraft(createParts);
    paintRows(createParts);
  }

  async function addMember() {
    if (!state.mega || state.busy) return;
    const select = view() && view().querySelector(".mt-add-session");
    const session = select ? select.value : "";
    if (!session) return;
    state.busy = true;
    try {
      await apiSend(`/api/megathreads/${encodeURIComponent(state.mega.id)}/members`, { session });
      await loadOne(state.mega.id);
    } catch (err) {
      state.error = String(err.message || err);
    }
    state.busy = false;
    render();
  }

  async function removeMember(thread) {
    if (!state.mega || state.busy) return;
    state.busy = true;
    try {
      const resp = await api(`/api/megathreads/${encodeURIComponent(state.mega.id)}/members/${encodeURIComponent(thread)}`,
        { method: "DELETE" });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
      await loadOne(state.mega.id);
    } catch (err) {
      state.error = String(err.message || err);
    }
    state.busy = false;
    render();
  }

  /* ---- drawing --------------------------------------------------------- */
  function membersOf() {
    return (state.mega && state.mega.members) || [];
  }

  /* The last routing's score for each member, so the ring can draw it. */
  function scores() {
    const out = {};
    for (const row of (state.judgment && state.judgment.candidates) || []) {
      out[row.thread] = row;
    }
    for (const row of (state.routes[0] && state.routes[0].candidates) || []) {
      if (!(row.thread in out)) out[row.thread] = row;
    }
    return out;
  }

  function ring(members, scored, threshold) {
    const parts = [];
    parts.push(`<svg class="mt-ring" viewBox="0 0 ${STAGE_W} ${STAGE_H}" role="img" aria-label="mega thread 원탁">`);
    const cx = STAGE_W / 2;
    const cy = STAGE_H / 2;
    const points = ringLayout(members.length);
    const hits = members.map((m) => {
      const row = scored[m.thread] || {};
      return row.logit;
    });
    points.forEach((pt, i) => {
      const tone = nodeTone(hits[i], threshold);
      const width = edgeWidth(hits[i]);
      parts.push(`<line class="mt-edge ${tone}" x1="${cx}" y1="${cy}" x2="${pt.x.toFixed(1)}" y2="${pt.y.toFixed(1)}" stroke-width="${width.toFixed(1)}"/>`);
    });
    parts.push(`<circle class="mt-me" cx="${cx}" cy="${cy}" r="34"/>`);
    parts.push(`<text class="mt-me-label" x="${cx}" y="${cy + 4}" text-anchor="middle">you</text>`);
    members.forEach((m, i) => {
      const pt = points[i];
      const row = scored[m.thread] || {};
      const tone = nodeTone(row.logit, threshold);
      const live = m.status === "idle" || m.status === "busy";
      const label = m.session;
      const sub = m.thread_title || m.thread;
      const below = pt.y >= cy ? 1 : -1;
      parts.push(`<g class="mt-node ${tone}${live ? "" : " gone"}" data-thread="${esc(m.thread)}">`);
      parts.push(`<circle cx="${pt.x.toFixed(1)}" cy="${pt.y.toFixed(1)}" r="24"/>`);
      parts.push(`<text class="mt-node-name" x="${pt.x.toFixed(1)}" y="${(pt.y + below * (below > 0 ? 44 : -30)).toFixed(1)}" text-anchor="middle">${esc(label)}</text>`);
      parts.push(`<text class="mt-node-sub" x="${pt.x.toFixed(1)}" y="${(pt.y + below * (below > 0 ? 58 : -16)).toFixed(1)}" text-anchor="middle">${esc(sub)}</text>`);
      parts.push(`<text class="mt-node-score" x="${pt.x.toFixed(1)}" y="${(pt.y + 4).toFixed(1)}" text-anchor="middle">${esc(logitText(row.logit))}</text>`);
      parts.push("</g>");
    });
    if (!members.length) {
      parts.push(`<text class="mt-empty" x="${cx}" y="${cy + 70}" text-anchor="middle">멤버가 없습니다 — 왼쪽에서 세션을 더하세요</text>`);
    }
    parts.push("</svg>");
    return parts.join("");
  }

  /* Closed clusters are listed, but dimmed: the page still has to reach them
     to reopen them, and routing refuses them with 409. */
  function isOpen(mega) {
    return !mega || mega.status === "open";
  }

  function statusLabel(mega) {
    return isOpen(mega) ? "닫기" : "다시 열기";
  }

  /* ---- the member picker (no DOM) --------------------------------------- */

  /* A live session reads differently from one that has exited, and the rail
     already carries the status; the picker only has to show it. Every
     status the rail can send is one of these four (daemon/session.py). */
  function statusTone(status) {
    if (status === "starting" || status === "busy" || status === "idle") return "live";
    if (!status) return "";
    return "gone";
  }

  /* The seven lifecycle partitions, in the same order and by the same rule as
     daemon/session.py::session_category. Two copies of this rule would file
     one record two ways on one page, which is exactly what the rail refuses
     to do — but the rail cannot answer for the picker: it sends a record per
     partition only when asked for that partition, and the picker holds every
     record at once. So the rule is repeated here, over the marker fields the
     rail's allow-list (api.py RAIL_FIELDS) really carries. */
  const CATEGORIES = ["running", "hold", "paused", "killed", "delayed", "failed", "archived"];

  const CATEGORY_KO = {
    running: "실행 중",
    hold: "홀드",
    paused: "일시정지",
    killed: "종료",
    delayed: "복귀 대기",
    failed: "실패",
    archived: "보관",
  };

  function sessionCategory(session) {
    const s = session || {};
    const exited = s.status === "exited" || !!s.exited_at;
    if (s.archived_at) return "archived";
    if (exited) {
      if (s.held_at) return s.delayed_at ? "delayed" : "hold";
      if (s.failed_at) return "failed";
      return s.paused_at ? "paused" : "killed";
    }
    return "running";
  }

  /* The second line of a row: what this session is working on. Only fields
     the rail poll actually sends are read here — `task` and `role` are in
     the session record but the rail leaves them out on purpose (api.py
     RAIL_FIELDS), so a line built from them is empty on every live row.
     `branch` is set on every live session and names the work; `issue` names
     the board item when there is one. An empty line is dropped rather than
     drawn as a separator with nothing after it. */
  function sessionLine(session) {
    const s = session || {};
    const progress = s.progress && s.progress.text ? String(s.progress.text) : "";
    return [s.role, progress, s.issue, s.branch, s.note]
      .map((bit) => String(bit || "").trim())
      .filter(Boolean)
      .join(" · ");
  }

  /* What the session is, apart from what it is doing: the role the mesh
     roster gives it (the rail's own `role` is empty on every live row —
     claunch-cn8v6 measured 0/26), the project and mesh it is filed under,
     and the cflow run it is driving. The three come from three requests the
     page joins by session name, so a row is built only from what the daemon
     answered for that session. */
  function metaLine(session, meshOf, cflowOf) {
    const s = session || {};
    const name = String(s.name || "");
    const mesh = (meshOf || {})[name] || null;
    const run = (cflowOf || {})[name] || null;
    const cflow = run
      ? `${run.workflow || "cflow"} · ${run.status || "?"}` +
        (run.current && run.current.length ? ` · ${run.current.join(", ")}` : "") +
        (run.gated ? " · ⚑" : "")
      : "";
    return [
      s.role || (mesh && mesh.role) || "",
      s.project || "",
      mesh ? mesh.mesh : "",
      cflow,
    ].map((bit) => String(bit || "").trim()).filter(Boolean).join(" · ");
  }

  /* The briefing digest the daemon cached for the session: one line, already
     written for a row (briefing.digest). It is the only field that says what
     the session believes it is doing, so it is drawn apart from the branch
     and issue instead of being joined into them. The digest names the
     one-liner `one_line` (the summariser's own key is
     "one-line-job-description"); reading only the latter left `now` on every
     row, a step-by-step sentence that goes stale against the live cflow
     line beside it (claunch-g07ra.1). */
  function briefLine(session) {
    const s = session || {};
    const b = s.briefing;
    if (!b) return "";
    if (typeof b === "string") return String(b).trim();
    return String(b.one_line || b["one-line-job-description"] || b.now || b.goal || "").trim();
  }

  /* The filter matches any field a reader might remember the session by.
     Case is folded because names and branches are typed in mixed case. */
  function matchesFilter(session, query) {
    const needle = String(query || "").trim().toLowerCase();
    if (!needle) return true;
    const s = session || {};
    const progress = s.progress && s.progress.text ? s.progress.text : "";
    const brief = briefLine(s);
    return [s.name, s.role, s.issue, s.branch, s.note, s.kind, progress,
            s.project, brief]
      .filter(Boolean).join(" ").toLowerCase().includes(needle);
  }

  /* The two orderings the session list offers, each in both directions:
     when the record was made, and when it last did anything. An empty
     reading sorts last in the newest-first order rather than first, so a
     record with no activity stamp does not head a list of the busiest. */
  const SORTS = {
    "activity-desc": { key: "last_activity_at", dir: -1, label: "활동 최신" },
    "activity-asc": { key: "last_activity_at", dir: 1, label: "활동 오래된" },
    "created-desc": { key: "created_at", dir: -1, label: "생성 최신" },
    "created-asc": { key: "created_at", dir: 1, label: "생성 오래된" },
  };

  function sortRows(rows, sort) {
    const spec = SORTS[sort] || SORTS["activity-desc"];
    return (rows || []).slice().sort((a, b) => {
      const av = String((a || {})[spec.key] || "");
      const bv = String((b || {})[spec.key] || "");
      if (av === bv) return String(a.name || "").localeCompare(String(b.name || ""));
      if (!av) return 1;      // no reading: last, whichever direction
      if (!bv) return -1;
      return av < bv ? -spec.dir : spec.dir;
    });
  }

  /* Parent-before-child order with a depth per row, the same walk the rail
     uses (byLineage in app.js). A row whose parent is not in the list — a
     filtered-out archived parent, a hand-edited record — is a root rather
     than a row that disappears, and a cycle cannot make a row vanish. */
  function hierarchyOrder(rows) {
    const byName = new Map((rows || []).map((r) => [String(r.name || ""), r]));
    const kids = new Map();
    const roots = [];
    for (const row of rows || []) {
      const parent = row.parent;
      if (parent && parent !== row.name && byName.has(parent)) {
        if (!kids.has(parent)) kids.set(parent, []);
        kids.get(parent).push(row);
      } else {
        roots.push(row);
      }
    }
    const out = [];
    const seen = new Set();
    const walk = (row, depth) => {
      if (seen.has(row.name)) return;
      seen.add(row.name);
      out.push({ row, depth });
      for (const kid of kids.get(row.name) || []) walk(kid, depth + 1);
    };
    for (const root of roots) walk(root, 0);
    for (const row of rows || []) {
      if (!seen.has(row.name)) out.push({ row, depth: 0 });
    }
    return out;
  }

  /* Which sessions a filter set keeps. Each axis is independent and an empty
     one means "everything"; the status set is the one exception, where an
     empty set is also "everything" because a reader who has ticked nothing
     has not asked to see nothing. */
  function keepsRow(session, opts) {
    const o = opts || {};
    const s = session || {};
    const name = String(s.name || "");
    if (!matchesFilter(s, o.query)) return false;
    if (o.project && String(s.project || "") !== o.project) return false;
    if (o.mesh) {
      const entry = (o.meshOf || {})[name];
      const mesh = entry ? entry.mesh : "";
      if (o.mesh === "-" ? !!mesh : mesh !== o.mesh) return false;
    }
    if (o.statuses && o.statuses.size) {
      const wanted = o.statuses instanceof Set ? o.statuses : new Set(o.statuses);
      if (!wanted.has(sessionCategory(s))) return false;
    }
    return true;
  }

  /* The rows the picker draws: filtered, ordered by the reader's sort, then
     (when the hierarchy box is on) arranged parent-before-child with the
     depth the row indents by, and marked with whether it is ticked. */
  function pickerRows(sessions, opts) {
    const o = opts || {};
    const on = o.picked instanceof Set ? o.picked : new Set(o.picked || []);
    const kept = (sessions || []).filter((s) => keepsRow(s, o));
    const sorted = sortRows(kept, o.sort);
    const ordered = o.hierarchy
      ? hierarchyOrder(sorted.map((s) => ({ ...s, name: String(s.name || "") })))
      : sorted.map((s) => ({ row: s, depth: 0 }));
    return ordered.map(({ row, depth }) => ({
      name: String(row.name || ""),
      line: sessionLine(row),
      meta: metaLine(row, o.meshOf, o.cflowOf),
      brief: briefLine(row),
      category: sessionCategory(row),
      categoryLabel: CATEGORY_KO[sessionCategory(row)] || sessionCategory(row),
      tone: statusTone(row.status),
      depth,
      checked: on.has(String(row.name || "")),
    }));
  }

  /* The projects and meshes the picker offers, from the records it holds —
     never from the filtered list, which would shrink the choices to whatever
     the current filter already keeps (the shape of claunch-zny1o). */
  function filterChoices(sessions, meshOf) {
    const projects = [];
    for (const s of sessions || []) {
      const name = String((s && s.project) || "");
      if (name && !projects.includes(name)) projects.push(name);
    }
    const meshes = [];
    for (const entry of Object.values(meshOf || {})) {
      const name = String((entry && entry.mesh) || "");
      if (name && !meshes.includes(name)) meshes.push(name);
    }
    projects.sort((a, b) => a.localeCompare(b));
    meshes.sort((a, b) => a.localeCompare(b));
    return { projects, meshes };
  }

  /* What the line above the button says: why it is off, or what the click
     will make. Silence here was the old behaviour and it read as a dead
     button. */
  function createHint(title, count) {
    if (!String(title || "").trim()) return "제목을 입력하세요";
    if (!count) return "멤버를 고르지 않으면 빈 mega thread가 만들어집니다";
    return `${count}명으로 만듭니다`;
  }

  function sideList(root) {
    const aside = node("aside", "mt-list");
    const head = node("div", "mt-list-head");
    head.appendChild(node("h2", null, "Mega threads"));
    const openCreate = node("button", "wf-btn mt-new-open", "＋ 새 mega thread");
    openCreate.type = "button";
    openCreate.title = "세션을 골라 mega thread를 만듭니다";
    openCreate.addEventListener("click", () => {
      state.createOpen = true;
      render();
      const first = view() && view().querySelector(".mt-create-title");
      if (first) first.focus();
    });
    head.appendChild(openCreate);
    aside.appendChild(head);
    for (const cluster of state.clusters) {
      const on = state.mega && state.mega.id === cluster.id ? " on" : "";
      const shut = cluster.status === "open" ? "" : " mt-closed";
      const row = node("button", `mt-cluster${on}${shut}`);
      row.type = "button";
      row.appendChild(node("span", "mt-cluster-title", cluster.title));
      row.appendChild(node("span", "mt-cluster-sub", `${cluster.members.length}명`));
      row.addEventListener("click", () => {
        wantId = cluster.id;
        loadOne(cluster.id).then(render).catch((err) => { state.error = String(err); render(); });
      });
      aside.appendChild(row);
    }
    root.appendChild(aside);
  }

  /* ---- the create panel ------------------------------------------------
     The form used to be a 240px column holding a 200px scroller: the reader
     could see six sessions and had no way to tell two of them apart
     (claunch-vz96l). It is now a panel over the page with the fleet's own
     controls — the two orderings the session list offers, the parent/child
     tree, project, mesh and lifecycle filters — and a Jev preview that
     proposes members for a typed topic. The selection still lives in
     `state.picked`, so filtering, sorting and the poll repaint cannot drop
     a tick. */
  function createPanel() {
    const overlay = node("div", "mt-create-overlay");
    const card = node("div", "mt-create-card");
    overlay.appendChild(card);

    const head = node("div", "mt-create-head");
    head.appendChild(node("h2", null, "새 mega thread"));
    const close = node("button", "mt-create-close", "×");
    close.type = "button";
    close.title = "닫기 (Esc)";
    close.addEventListener("click", () => { state.createOpen = false; render(); });
    head.appendChild(close);
    card.appendChild(head);

    const top = node("div", "mt-create-top");
    const title = document.createElement("input");
    title.type = "text";
    title.className = "mt-create-title";
    title.placeholder = "새 mega thread 제목";
    title.value = state.title;
    title.addEventListener("input", () => { state.title = title.value; paintCounters(parts); });
    top.appendChild(title);

    /* The topic line: type what the conversation is about, ask Jev which
       sessions it belongs to, then edit the ticks it proposes. The preview
       is a proposal — nothing is created until 만들기 — so a topic the
       reader abandons costs one Jev call and leaves no trace but its ledger
       row. */
    const topicRow = node("div", "mt-create-topic");
    const topic = document.createElement("input");
    topic.type = "text";
    topic.className = "mt-create-topic-input";
    topic.placeholder = "주제를 입력하면 Jev가 넣을 세션을 고릅니다";
    topic.value = state.topic;
    topic.addEventListener("input", () => {
      state.topic = topic.value;
      if (state.draftError) state.draftError = "";
      paintCounters(parts);
    });
    topic.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); requestDraft(); }
    });
    topicRow.appendChild(topic);
    const draftBtn = node("button", "wf-btn mt-create-draft", "Jev로 멤버 추천");
    draftBtn.type = "button";
    draftBtn.addEventListener("click", requestDraft);
    topicRow.appendChild(draftBtn);
    const retry = node("button", "wf-btn mt-create-retry", "재시도");
    retry.type = "button";
    retry.hidden = !state.draftError;
    retry.addEventListener("click", requestDraft);
    topicRow.appendChild(retry);
    top.appendChild(topicRow);

    const draftNote = node("p", "mt-create-draft-note");
    top.appendChild(draftNote);
    card.appendChild(top);

    /* The controls: what the reader narrows by, and how the list is ordered.
       Every one of these repaints the list only — never the panel — so the
       control the reader just used keeps its focus. */
    const controls = node("div", "mt-create-controls");
    const filter = document.createElement("input");
    filter.type = "search";
    filter.className = "mt-new-filter";
    filter.placeholder = "이름·작업·이슈·브리핑으로 거르기";
    filter.value = state.filter;
    filter.addEventListener("input", () => { state.filter = filter.value; paintRows(parts); });
    controls.appendChild(filter);

    const { projects, meshes } = filterChoices(state.sessions || [], state.meshOf);
    const projectSelect = document.createElement("select");
    projectSelect.className = "mt-create-project";
    projectSelect.title = "프로젝트로 거르기";
    projectSelect.appendChild(optionRow("", "모든 프로젝트"));
    for (const name of projects) projectSelect.appendChild(optionRow(name, name));
    projectSelect.value = projects.includes(state.project) ? state.project : "";
    projectSelect.addEventListener("change", () => {
      state.project = projectSelect.value;
      paintRows(parts);
    });
    controls.appendChild(projectSelect);

    const meshSelect = document.createElement("select");
    meshSelect.className = "mt-create-mesh";
    meshSelect.title = "메시로 거르기";
    meshSelect.appendChild(optionRow("", "모든 메시"));
    for (const name of meshes) meshSelect.appendChild(optionRow(name, name));
    meshSelect.appendChild(optionRow("-", "메시 없음"));
    meshSelect.value = meshes.includes(state.mesh) || state.mesh === "-" ? state.mesh : "";
    meshSelect.addEventListener("change", () => {
      state.mesh = meshSelect.value;
      paintRows(parts);
    });
    controls.appendChild(meshSelect);

    const sort = document.createElement("select");
    sort.className = "mt-create-sort";
    sort.title = "정렬";
    for (const [key, spec] of Object.entries(SORTS)) sort.appendChild(optionRow(key, spec.label));
    sort.value = state.sort;
    sort.addEventListener("change", () => { state.sort = sort.value; paintRows(parts); });
    controls.appendChild(sort);

    const tree = node("label", "mt-create-tree");
    const treeBox = document.createElement("input");
    treeBox.type = "checkbox";
    treeBox.className = "mt-create-hierarchy";
    treeBox.checked = state.hierarchy;
    treeBox.addEventListener("change", () => {
      state.hierarchy = treeBox.checked;
      paintRows(parts);
    });
    tree.appendChild(treeBox);
    tree.appendChild(node("span", null, "부모-자식 위계"));
    controls.appendChild(tree);
    card.appendChild(controls);

    const statusBar = node("div", "mt-create-statuses");
    for (const category of CATEGORIES) {
      const chip = node("button", `mt-create-status${state.statuses.has(category) ? " on" : ""}`,
        CATEGORY_KO[category] || category);
      chip.type = "button";
      chip.dataset.category = category;
      chip.addEventListener("click", () => {
        if (state.statuses.has(category)) state.statuses.delete(category);
        else state.statuses.add(category);
        chip.classList.toggle("on", state.statuses.has(category));
        paintRows(parts);
      });
      statusBar.appendChild(chip);
    }
    const all = node("button", "mt-create-status mt-create-status-all", "전체");
    all.type = "button";
    all.addEventListener("click", () => {
      state.statuses.clear();
      for (const chip of statusBar.querySelectorAll(".mt-create-status")) chip.classList.remove("on");
      paintRows(parts);
    });
    statusBar.appendChild(all);
    card.appendChild(statusBar);

    const list = node("div", "mt-new-sessions mt-create-list");
    card.appendChild(list);

    const foot = node("div", "mt-create-foot");
    const count = node("span", "mt-new-count");
    foot.appendChild(count);
    const clear = node("button", "mt-new-clear", "전체 해제");
    clear.type = "button";
    clear.addEventListener("click", () => { state.picked.clear(); paintRows(parts); });
    foot.appendChild(clear);
    const hint = node("p", "mt-new-hint");
    foot.appendChild(hint);
    const create = node("button", "wf-btn mt-new-create", "만들기");
    create.type = "button";
    create.addEventListener("click", createCluster);
    foot.appendChild(create);
    card.appendChild(foot);

    const parts = { list, count, clear, hint, create, draftBtn, retry, draftNote, topic, statusBar };
    createParts = parts;
    paintDraft(parts);
    paintRows(parts);
    card.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { state.createOpen = false; render(); }
    });
    return overlay;
  }

  function optionRow(value, label) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    return option;
  }

  /* Ticking a row repaints the row's own state and the counters, not the
     whole list: a full repaint would take focus off the checkbox the reader
     just used. */
  function paintRows(parts) {
    if (!parts || !parts.list) return;
    parts.list.innerHTML = "";
    if (state.sessions === null) {
      parts.list.appendChild(node("p", "wf-note mt-new-loading", "세션 목록을 읽는 중…"));
      return;
    }
    const rows = pickerRows(state.sessions || [], {
      query: state.filter,
      picked: state.picked,
      project: state.project,
      mesh: state.mesh,
      statuses: state.statuses,
      sort: state.sort,
      hierarchy: state.hierarchy,
      meshOf: state.meshOf,
      cflowOf: state.cflowOf,
    });
    const scoring = state.draftPreview && (state.draftPreview.members || []);
    const scoreOf = {};
    for (const row of scoring || []) scoreOf[row.session] = row;
    for (const row of rows) {
      const label = node("label", `mt-new-session-row mt-cat-${row.category}${row.checked ? " on" : ""}`);
      if (row.depth) label.style.paddingLeft = `${6 + row.depth * 14}px`;
      const box = document.createElement("input");
      box.type = "checkbox";
      box.className = "mt-new-session";
      box.value = row.name;
      box.checked = row.checked;
      box.addEventListener("change", () => {
        if (box.checked) state.picked.add(row.name);
        else state.picked.delete(row.name);
        label.classList.toggle("on", box.checked);
        paintCounters(parts);
      });
      label.appendChild(box);

      const body = node("span", "mt-new-body");
      const line1 = node("span", "mt-new-top");
      line1.appendChild(node("span", "mt-new-name", row.name));
      const score = scoreOf[row.name];
      if (score && score.recommended) {
        line1.appendChild(node("span", "mt-new-flag", `추천 ${pct(score.p_mine)}`));
      } else if (score && score.logit !== null && score.logit !== undefined) {
        line1.appendChild(node("span", "mt-new-flag weak", pct(score.p_mine)));
      }
      if (row.tone) line1.appendChild(node("span", `mt-new-dot ${row.tone}`));
      line1.appendChild(node("span", "mt-new-cat", row.categoryLabel));
      body.appendChild(line1);
      if (row.meta) body.appendChild(node("span", "mt-new-meta", row.meta));
      if (row.line) body.appendChild(node("span", "mt-new-line", row.line));
      if (row.brief) body.appendChild(node("span", "mt-new-brief", row.brief));
      label.appendChild(body);
      parts.list.appendChild(label);
    }
    if (!rows.length) {
      parts.list.appendChild(node("p", "wf-note",
        state.filter || state.project || state.mesh || state.statuses.size
          ? "거른 결과가 없습니다" : "고를 세션이 없습니다"));
    }
    paintCounters(parts);
  }

  /* The preview's own line: what Jev said, or what went wrong and that the
     same topic can be sent again. */
  function paintDraft(parts) {
    if (!parts || !parts.draftNote) return;
    const preview = state.draftPreview;
    if (state.draftBusy) {
      parts.draftNote.className = "mt-create-draft-note";
      parts.draftNote.textContent = "Jev가 주제를 읽는 중…";
    } else if (state.draftError) {
      parts.draftNote.className = "mt-create-draft-note bad";
      parts.draftNote.textContent = `추천 실패 — ${state.draftError}`;
    } else if (preview) {
      const n = (preview.recommended || []).length;
      parts.draftNote.className = "mt-create-draft-note ok";
      parts.draftNote.textContent = n
        ? `${n}명을 추천했습니다 (임계값 logit ${logitText(preview.threshold)}) — 체크를 직접 고친 뒤 만드세요`
        : `임계값(logit ${logitText(preview.threshold)})을 넘은 세션이 없습니다 — 점수는 참고로 두고 직접 고르세요`;
      if (preview.budget_remaining_tokens !== null && preview.budget_remaining_tokens !== undefined) {
        parts.draftNote.textContent += ` · 예산 남음 ${Number(preview.budget_remaining_tokens).toLocaleString()} tokens`;
      }
    } else {
      parts.draftNote.className = "mt-create-draft-note";
      parts.draftNote.textContent = "";
    }
    if (parts.retry) parts.retry.hidden = !state.draftError;
    if (parts.draftBtn) {
      parts.draftBtn.disabled = state.draftBusy || !state.topic.trim();
      parts.draftBtn.textContent = state.draftBusy ? "읽는 중…" : "Jev로 멤버 추천";
    }
  }

  function paintCounters(parts) {
    if (!parts || !parts.count) return;
    const n = state.picked.size;
    parts.count.textContent = `${n}개 선택`;
    parts.clear.disabled = n === 0;
    parts.hint.textContent = createHint(state.title, n);
    const ready = String(state.title || "").trim();
    parts.hint.className = `mt-new-hint${ready ? (n ? " ok" : " warn") : ""}`;
    parts.create.disabled = state.busy || !ready;
    paintDraft(parts);
  }

  function sessionNames() {
    return (state.sessions || []).map((s) => s.name).sort();
  }

  function stage(root) {
    const main = node("section", "mt-stage");
    if (!state.mega) {
      main.appendChild(node("p", "wf-note", "왼쪽에서 mega thread를 만들거나 고르세요."));
      root.appendChild(main);
      return;
    }
    const open = isOpen(state.mega);
    const head = node("div", "mt-head");
    head.appendChild(node("h2", null, state.mega.title));
    head.appendChild(node("span", "wf-note", `${membersOf().length}명 · 상태 ${state.mega.status}`));
    const toggle = node("button", "wf-btn mt-status", statusLabel(state.mega));
    toggle.type = "button";
    toggle.disabled = state.busy;
    toggle.addEventListener("click", toggleStatus);
    head.appendChild(toggle);
    main.appendChild(head);
    main.appendChild(membersStrip());

    const scored = scores();
    const threshold = (state.judgment && state.judgment.threshold)
      || (state.routes[0] && state.routes[0].threshold) || Math.log(9);
    const ringWrap = node("div", "mt-ring-wrap");
    ringWrap.innerHTML = ring(membersOf(), scored, Number(threshold));
    ringWrap.addEventListener("click", (event) => {
      const group = event.target.closest && event.target.closest(".mt-node");
      if (!group) return;
      const tid = group.getAttribute("data-thread");
      const member = membersOf().find((m) => m.thread === tid);
      if (member) goToSession(member);
    });
    main.appendChild(ringWrap);

    if (open && state.judgment
        && (state.judgment.verdict === "choose" || state.judgment.verdict === "none")) {
      main.appendChild(optionsPanel());
    }

    const composer = node("div", open ? "mt-compose" : "mt-compose mt-shut");
    const input = document.createElement("textarea");
    input.rows = 2;
    // The daemon refuses a routing on a closed cluster with 409; the page
    // says so before the reader types into a box that cannot send.
    input.placeholder = open
      ? "여러 세션 중 어디로 갈지 데몬이 판정합니다"
      : "닫힌 mega thread입니다 — 다시 열면 보낼 수 있습니다";
    input.value = state.draft;
    input.disabled = state.busy || !open;
    input.addEventListener("input", () => { state.draft = input.value; syncSend(); });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        state.picks.size ? sendPicks() : sendText();
      }
    });
    composer.appendChild(input);
    const send = node("button", "wf-btn mt-send", state.picks.size ? "선택한 곳으로 보내기" : "보내기");
    send.type = "button";
    send.disabled = state.busy || !open || !state.draft.trim();
    send.addEventListener("click", () => (state.picks.size ? sendPicks() : sendText()));
    composer.appendChild(send);
    main.appendChild(composer);

    if (state.judgment) main.appendChild(node("p", "mt-verdict", verdictLine(state.judgment)));
    if (state.notice) main.appendChild(node("p", "mt-notice", state.notice));
    if (state.error) main.appendChild(node("p", "error", state.error));

    const budget = state.judgment && state.judgment.budget_remaining_tokens;
    if (budget !== null && budget !== undefined) {
      main.appendChild(node("p", "wf-note", `Jev 예산 남음: ${Number(budget).toLocaleString()} tokens (24h)`));
    }
    root.appendChild(main);
  }

  /* Who is at the table, and the two ways it changes: a member leaves, or a
     session joins (with its most recent open thread, or a new one — the
     daemon decides that side). */
  function membersStrip() {
    const strip = node("div", "mt-members");
    for (const m of membersOf()) {
      const chip = node("span", "mt-chip");
      chip.appendChild(node("span", "mt-chip-name", `${m.session} · ${m.thread_title || m.thread}`));
      if (m.status && m.status !== "idle" && m.status !== "busy") {
        chip.appendChild(node("span", "mt-chip-dead", m.status));
      }
      const drop = node("button", "mt-chip-x", "×");
      drop.type = "button";
      drop.title = `${m.session}의 이 쓰레드를 내보냅니다`;
      drop.addEventListener("click", () => removeMember(m.thread));
      chip.appendChild(drop);
      strip.appendChild(chip);
    }
    const add = document.createElement("select");
    add.className = "mt-add-session";
    add.title = "이 mega thread에 세션을 더합니다";
    for (const name of sessionNames()) {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      add.appendChild(option);
    }
    strip.appendChild(add);
    const button = node("button", "wf-btn mt-add", "추가");
    button.type = "button";
    button.disabled = state.busy || !sessionNames().length;
    button.addEventListener("click", addMember);
    strip.appendChild(button);
    return strip;
  }

  const VERDICT_KO = {
    auto: "단독", choose: "선택", manual: "직접 선택", none: "후보 없음", error: "실패",
  };

  /* The composer's button follows the draft and the ticks without a redraw. */
  function syncSend() {
    const root = view();
    const button = root && root.querySelector(".mt-send");
    if (!button) return;
    button.textContent = state.picks.size ? "선택한 곳으로 보내기" : "보내기";
    button.disabled = state.busy || !isOpen(state.mega) || !String(state.draft || "").trim();
  }

  function optionsPanel() {
    const panel = node("div", "mt-options");
    panel.appendChild(node("h3", null, "보낼 곳 선택"));
    for (const row of optionRows(state.judgment)) {
      const label = node("label", "mt-option");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = state.picks.has(row.thread);
      box.addEventListener("change", () => {
        if (box.checked) state.picks.add(row.thread);
        else state.picks.delete(row.thread);
        // Nothing moves in the page, so redraw nothing: a re-render here
        // would rebuild the very checkbox under the pointer.
        syncSend();
      });
      label.append(box);
      label.appendChild(node("span", "mt-option-name", row.label));
      label.appendChild(node("span", "mt-option-score", `p ${pct(row.p)} · logit ${logitText(row.logit)}`));
      panel.appendChild(label);
    }
    return panel;
  }

  function logPanel(root) {
    const aside = node("aside", "mt-log");
    aside.appendChild(node("h2", null, "라우팅 로그"));
    if (!state.routes.length) {
      aside.appendChild(node("p", "wf-note", "아직 라우팅이 없습니다."));
      root.appendChild(aside);
      return;
    }
    for (const row of state.routes) {
      const card = node("div", "mt-log-row");
      const kind = VERDICT_KO[row.verdict] || row.verdict;
      card.appendChild(node("div", "mt-log-at", `${row.at} · ${kind} · ${Number(row.tokens || 0).toLocaleString()} tokens`));
      card.appendChild(node("div", "mt-log-body", row.body));
      const scored = (row.candidates || []).slice(0, 6)
        .map((c) => `${c.session} ${logitText(c.logit)}`).join(" · ");
      if (scored) card.appendChild(node("div", "mt-log-scores", scored));
      if (row.delivered && (row.chosen || []).length) {
        card.appendChild(node("div", "mt-log-sent", `→ ${row.chosen.length}곳`));
      }
      aside.appendChild(card);
    }
    root.appendChild(aside);
  }

  function goToSession(member) {
    if (!member || !member.session) return;
    location.hash = `#/s/${encodeURIComponent(member.session)}`;
  }

  function render() {
    const root = view();
    if (!root) return;
    root.innerHTML = "";
    const page = node("div", "mt-page");
    if (state.error) page.appendChild(node("p", "error", state.error));
    const inner = node("div", "mt-inner");
    sideList(inner);
    stage(inner);
    logPanel(inner);
    page.appendChild(inner);
    if (state.createOpen) page.appendChild(createPanel());
    else createParts = null;
    root.appendChild(page);
  }

  /* The fleet the picker draws, from three answers the dashboard already
     polls for its own rail:
     - `/api/sessions?view=rail` — the records (status, project, parent,
       created_at, last_activity_at, issue, branch, briefing);
     - `/api/mesh?view=rail` — which mesh a session is in and the role it
       holds there. The rail's own `role` is empty on every live row
       (claunch-cn8v6 measured 0/26), so the roster is the only source;
     - `/api/cflow2/rail` — the v2 run each session is driving, keyed by
       session name.
     The two joins are optional: a daemon that cannot answer one of them
     still lists every session, with that column empty. */
  async function loadSessions() {
    const [sessions, meshes, runs] = await Promise.all([
      apiGet("/api/sessions?view=rail").catch(() => ({ sessions: [] })),
      apiGet("/api/mesh?view=rail").catch(() => ({})),
      apiGet("/api/cflow2/rail").catch(() => ({})),
    ]);
    state.sessions = sessions.sessions || [];
    state.meshOf = {};
    state.meshNames = [];
    for (const mesh of meshes.meshes || []) {
      const name = String(mesh.name || "");
      if (name && !state.meshNames.includes(name)) state.meshNames.push(name);
      for (const member of mesh.members || []) {
        const session = String((member && member.session) || "");
        if (session) state.meshOf[session] = { mesh: name, role: member.role || "" };
      }
    }
    state.cflowOf = runs.rows || {};
  }

  async function open(id) {
    wantId = id || wantId;
    const root = view();
    if (root) {
      root.classList.remove("hidden");
      root.innerHTML = "";
      root.appendChild(node("p", "wf-note", "읽는 중…"));
    }
    // The clusters first, the fleet after. Only the create panel and the
    // add-member select read the fleet, and its first read is the slow one:
    // `/api/sessions?view=rail` with no state is every record, archived ones
    // included, which no other poll keeps warm (measured 1.5-2.3 s on a
    // 2303-record daemon, against 3 ms for /api/megathreads, claunch-wbs0i).
    // Awaiting it first held the whole page on "읽는 중…" for that long, and
    // reading both at once put them in one /api/batch, which answers its
    // paths one after another.
    await load();
    if (timer) clearInterval(timer);
    timer = setInterval(() => load(true), POLL_MS);
    loadSessions().then(() => {
      if (!timer) return;  // the reader left the page meanwhile
      if (typing() && createParts) { paintRows(createParts); paintCounters(createParts); }
      else if (!typing()) render();
    });
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
  }

  return {
    open, stop, render, ring,
    ringLayout, edgeWidth, nodeTone, routeBody, optionRows, verdictLine,
    deliveryLine, statusLabel, pct, logitText, esc,
    statusTone, sessionLine, matchesFilter, pickerRows, createHint,
    // the create panel's model (tests/web/megathread_check.js runs these bare)
    sessionCategory, metaLine, briefLine, sortRows, hierarchyOrder,
    filterChoices, CATEGORIES, SORTS,
  };
})();
