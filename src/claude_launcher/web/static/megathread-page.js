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
      if (!d.delivered) return `${who}: 대기 중 (세션이 응답할 수 없음)`;
      return d.hold ? `${who}: 전달됨 · ${d.hold}` : `${who}: 전달됨`;
    }).join(" · ");
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
    busy: false,
  };
  let timer = null;
  let wantId = "";

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
    if (!state.mega || state.busy) return;
    const text = state.draft.trim();
    if (!text) return;
    state.busy = true;
    state.picks.clear();
    state.error = "";
    try {
      state.judgment = await apiSend(`/api/megathreads/${encodeURIComponent(state.mega.id)}/route`, routeBody(text));
      if (state.judgment.delivered && state.judgment.delivered.length) {
        state.notice = deliveryLine(state.judgment.delivered);
        state.draft = "";
      } else if (state.judgment.verdict === "choose" || state.judgment.verdict === "none") {
        state.notice = "";
      } else if (state.judgment.verdict === "error") {
        state.notice = (state.judgment.errors || []).map((e) => `${e.thread}: ${e.error}`).join(" · ");
      }
      await loadOne(state.mega.id);
    } catch (err) {
      state.error = String(err.message || err);
    }
    state.busy = false;
    render();
  }

  async function sendPicks() {
    if (!state.mega || state.busy || !state.picks.size) return;
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
      state.notice = deliveryLine(res.delivered);
      state.draft = "";
      state.picks.clear();
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
      const picked = [...(view() || document).querySelectorAll(".mt-new-session:checked")]
        .map((box) => box.value);
      const res = await apiSend("/api/megathreads",
        { title, members: picked.map((session) => ({ session })) });
      wantId = res.mega.id;
      state.title = "";
      state.notice = `mega thread를 만들었습니다: ${res.mega.title}`;
      await load();
    } catch (err) {
      state.error = String(err.message || err);
    }
    state.busy = false;
    render();
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

  function sideList(root) {
    const aside = node("aside", "mt-list");
    aside.appendChild(node("h2", null, "Mega threads"));
    for (const cluster of state.clusters) {
      const row = node("button", `mt-cluster${state.mega && state.mega.id === cluster.id ? " on" : ""}`);
      row.type = "button";
      row.appendChild(node("span", "mt-cluster-title", cluster.title));
      row.appendChild(node("span", "mt-cluster-sub", `${cluster.members.length}명`));
      row.addEventListener("click", () => {
        wantId = cluster.id;
        loadOne(cluster.id).then(render).catch((err) => { state.error = String(err); render(); });
      });
      aside.appendChild(row);
    }
    const form = node("div", "mt-new");
    const title = document.createElement("input");
    title.type = "text";
    title.placeholder = "새 mega thread 제목";
    title.value = state.title;
    title.addEventListener("input", () => { state.title = title.value; });
    form.appendChild(title);
    const sessions = node("div", "mt-new-sessions");
    for (const s of sessionNames()) {
      const label = node("label", "mt-new-session-row");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.className = "mt-new-session";
      box.value = s;
      label.append(box, ` ${s}`);
      sessions.appendChild(label);
    }
    form.appendChild(sessions);
    const create = node("button", "wf-btn", "만들기");
    create.type = "button";
    create.disabled = state.busy || !state.title.trim();
    create.addEventListener("click", createCluster);
    form.appendChild(create);
    aside.appendChild(form);
    root.appendChild(aside);
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
    const head = node("div", "mt-head");
    head.appendChild(node("h2", null, state.mega.title));
    head.appendChild(node("span", "wf-note", `${membersOf().length}명 · 상태 ${state.mega.status}`));
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

    if (state.judgment && (state.judgment.verdict === "choose" || state.judgment.verdict === "none")) {
      main.appendChild(optionsPanel());
    }

    const composer = node("div", "mt-compose");
    const input = document.createElement("textarea");
    input.rows = 2;
    input.placeholder = "여러 세션 중 어디로 갈지 데몬이 판정합니다";
    input.value = state.draft;
    input.disabled = state.busy;
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
    send.disabled = state.busy || !state.draft.trim();
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
    button.disabled = state.busy || !String(state.draft || "").trim();
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
    root.appendChild(page);
  }

  async function loadSessions() {
    if (state.sessions) return;
    try {
      const data = await apiGet("/api/sessions?view=rail");
      state.sessions = data.sessions || [];
    } catch (_err) {
      state.sessions = [];
    }
  }

  async function open(id) {
    wantId = id || wantId;
    const root = view();
    if (root) {
      root.classList.remove("hidden");
      root.innerHTML = "";
      root.appendChild(node("p", "wf-note", "읽는 중…"));
    }
    await loadSessions();
    await load();
    if (timer) clearInterval(timer);
    timer = setInterval(() => load(true), POLL_MS);
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
  }

  return {
    open, stop, render, ring,
    ringLayout, edgeWidth, nodeTone, routeBody, optionRows, verdictLine,
    deliveryLine, pct, logitText, esc,
  };
})();
