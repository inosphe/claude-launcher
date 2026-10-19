/* The Work spec page (#/spec): paste or load a work spec YAML, check it,
   choose the leader, preview the plan and run the import. The three verbs
   are POST /api/spec/check · plan · import (daemon/spec_api.py) — the same
   code `claunch spec` runs. The format is docs/work-spec.md.
   The board an import writes to is the spec's registered workspace; a spec
   without one gets it from the choice here (the daemon's own directory is
   not the user's checkout, so the API refuses an import with neither).
   While the text is edited the page checks it by itself (a pause in typing
   sends /check), points each error at its YAML line (/check's `lines`) and,
   once the spec is valid, previews the sessions it starts and the work it
   files (/check's `preview`) before any leader is chosen. The editor is
   built once and kept: redrawing it on every keystroke would drop the
   caret. */
globalThis.SpecPage = (() => {
  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const button = (label, onclick, disabled = false) => {
    const b = node("button", "wf-btn", label);
    b.type = "button"; b.disabled = disabled; b.onclick = onclick;
    return b;
  };

  // text: the spec; spec: /check's summary of it (null until it passes);
  // errors: the validator's lines, lines: the YAML line of each (or null);
  // shown: /check's preview of a valid spec; plan/log: the last answers.
  let text = "", spec = null, errors = [], lines = [], shown = null, plan = null, log = null;
  let leaderKind = "", leaderSession = "", profile = "", workspace = "";
  let notice = "", error = "", leaderError = "", busy = false;
  let sequence = 0;
  // auto: check after a pause in typing; timer: the pending one.
  let auto = true, timer = null;
  const AUTO_DELAY = 600;
  // The parts drawn once per root: the editor and its buttons, and the
  // box everything else is redrawn into.
  let frame = null;

  function view() { return document.getElementById("spec-view"); }

  /* The --leader value the choice spells, as `claunch spec` takes it. */
  function leaderChoice() {
    const s = leaderSession.trim();
    if (spec && spec.mesh_mode === "attach") return s ? `shared:${s}` : "";
    if (leaderKind === "own") return "own";
    if (leaderKind === "shared") return s ? `shared:${s}` : "shared";
    return "";
  }

  async function post(verb, extra = {}) {
    const response = await api(`/api/spec/${verb}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ yaml: text, ...(workspace ? { workspace } : {}), ...extra }),
    });
    let body = {};
    try { body = await response.json(); } catch { body = {}; }
    return { ok: response.ok, status: response.status, body };
  }

  async function run(verb, extra, apply) {
    if (busy) return;
    const seq = ++sequence;
    busy = true; error = ""; leaderError = ""; notice = ""; render();
    try {
      const answer = await post(verb, extra);
      if (seq !== sequence) return;
      apply(answer);
    } catch (exc) {
      if (seq === sequence) error = String(exc.message || exc);
    } finally {
      if (seq === sequence) { busy = false; render(); }
    }
  }

  function check() {
    return run("check", {}, ({ ok, body }) => {
      plan = null; log = null;
      errors = body.errors || [];
      lines = body.lines || [];
      spec = ok ? body.spec : null;
      shown = ok ? body.preview || null : null;
      if (!ok && !errors.length) error = body.error || "검증 실패";
      if (!ok && body.workspace_error) workspace = "";
      if (spec) {
        // The spec's own leader.mode is the default choice; ask leaves it open.
        if (!leaderKind && ["own", "shared"].includes(spec.leader_mode)) leaderKind = spec.leader_mode;
        if (!leaderSession && spec.candidates.length === 1) leaderSession = spec.candidates[0];
      }
    });
  }

  function preview() {
    return run("plan", { leader: leaderChoice() }, ({ ok, body }) => {
      if (ok) { plan = body.actions.map((a, i) => ({ action: a, line: body.lines[i] })); return; }
      plan = null;
      if (body.leader_error) leaderError = body.error;
      else { errors = body.errors || []; error = body.error || "plan 실패"; }
    });
  }

  function runImport() {
    const choice = leaderChoice();
    const what = `${spec.name} → 메시 ${spec.mesh}, 리더 ${choice || "(메시의 현 리더)"}, 보드 ${spec.board_dir}`;
    if (!confirm(`import 를 실행한다: ${what}\n이슈를 만들고 세션을 띄운다.`)) return;
    return run("import", { leader: choice, profile: profile.trim() }, ({ ok, status, body }) => {
      log = body.log || [];
      if (ok) { notice = `import 완료 — ${log.length}개 action`; return; }
      if (body.leader_error) { leaderError = body.error; return; }
      if (body.workspace_error) { error = body.error; return; }
      error = body.error || `HTTP ${status}`;
      if (body.resumable) notice = "그 전에 실행된 action은 남아 있다. 원인을 고친 뒤 같은 버튼으로 다시 실행하면 이어서 진행된다.";
    });
  }

  function onText(value) {
    text = value;
    // A changed spec is an unchecked spec: nothing below it describes it any
    // more, the workspace chosen for the old text included -- nor does an
    // answer still on its way about the old text.
    sequence++; busy = false;
    workspace = ""; spec = null; errors = []; lines = []; shown = null;
    plan = null; log = null; notice = ""; error = "";
    schedule();
    render();
  }

  /* A pause in typing checks the text. The page module also runs where
     there are no timers (the node harness), and there it waits for the
     button. */
  function schedule() {
    if (timer !== null && typeof clearTimeout === "function") clearTimeout(timer);
    timer = null;
    if (!auto || !text.trim() || typeof setTimeout !== "function") return;
    timer = setTimeout(() => { timer = null; check(); }, AUTO_DELAY);
  }

  async function insertTemplate() {
    if (text.trim() && !confirm("지금 입력한 명세를 예시 템플릿으로 바꾼다.")) return;
    try {
      const response = await api("/api/spec/template");
      const body = await response.json();
      if (!response.ok || typeof body.yaml !== "string") throw new Error(body.error || `HTTP ${response.status}`);
      onText(body.yaml);
    } catch (exc) {
      error = String(exc.message || exc); render();
    }
  }

  /* Select line `n` (1-based) of the editor and bring it into view. */
  function jump(n) {
    const editor = frame && frame.editor;
    if (!editor) return;
    const rows = text.split("\n");
    let start = 0;
    for (let i = 0; i < n - 1 && i < rows.length; i++) start += rows[i].length + 1;
    const end = start + (rows[n - 1] || "").length;
    if (typeof editor.focus === "function") editor.focus();
    if (typeof editor.setSelectionRange === "function") editor.setSelectionRange(start, end);
    if (editor.scrollHeight && rows.length) {
      editor.scrollTop = Math.max(0, (n - 3) * (editor.scrollHeight / rows.length));
    }
  }

  function leaderBlock() {
    const box = node("div", "spec-leader");
    box.appendChild(node("h3", null, "리더"));
    const row = node("div", "spec-row");
    if (spec.mesh_mode === "attach") {
      row.appendChild(node("span", "wt-muted",
        `attach — 메시 ${spec.mesh}의 현 리더를 쓴다. 세션을 적으면 그 세션이 현 리더인지 확인한다.`));
    } else {
      const kind = node("select");
      kind.setAttribute("aria-label", "리더");
      for (const [value, label] of [["", "선택…"], ["own", "own — 이 명세 전용 리더를 새로 띄운다"],
        ["shared", "shared — 기존 리더 세션을 합류시킨다"]]) {
        const o = node("option", null, label); o.value = value;
        kind.appendChild(o);
      }
      kind.value = leaderKind;
      kind.onchange = () => { leaderKind = kind.value; plan = null; render(); };
      row.appendChild(kind);
    }
    if (spec.mesh_mode === "attach" || leaderKind === "shared") {
      const session = node("input");
      session.setAttribute("aria-label", "리더 세션");
      session.placeholder = spec.candidates.length ? spec.candidates.join(", ") : "세션 이름";
      session.value = leaderSession;
      session.oninput = () => { leaderSession = session.value; plan = null; };
      row.appendChild(session);
    }
    if (leaderKind === "own" && spec.mesh_mode !== "attach") {
      const p = node("input");
      p.setAttribute("aria-label", "프로필");
      p.placeholder = "프로필(새 최상위 세션에 필요)";
      p.value = profile;
      p.oninput = () => { profile = p.value; };
      row.appendChild(p);
    }
    box.appendChild(row);
    if (leaderError) box.appendChild(node("p", "wt-error", leaderError));
    return box;
  }

  /* Where the import writes: the board directory, and the choice of
     workspace when the spec names none. Choosing re-checks, so the
     directory shown is the one the daemon resolved. */
  function boardBlock() {
    const box = node("div", "spec-leader");
    box.appendChild(node("h3", null, "보드"));
    if (!spec.workspace || workspace) {
      const row = node("div", "spec-row");
      const pick = node("select");
      pick.setAttribute("aria-label", "워크스페이스");
      for (const name of ["", ...spec.workspaces]) {
        const o = node("option", null, name || "워크스페이스 선택…"); o.value = name;
        pick.appendChild(o);
      }
      pick.value = workspace;
      pick.onchange = () => { workspace = pick.value; plan = null; check(); };
      row.appendChild(pick);
      box.appendChild(row);
    }
    if (spec.board_dir) box.appendChild(node("p", "spec-summary", `import가 쓰는 보드: ${spec.board_dir}`));
    if (spec.board_error) box.appendChild(node("p", "wt-error", spec.board_error));
    return box;
  }

  function table(head, rows) {
    const t = node("table", "spec-table");
    const tr = node("tr");
    for (const h of head) tr.appendChild(node("th", null, h));
    t.appendChild(tr);
    for (const cells of rows) {
      const r = node("tr");
      for (const c of cells) r.appendChild(node("td", null, c));
      t.appendChild(r);
    }
    return t;
  }

  function errorList() {
    const list = node("ul", "spec-errors");
    errors.forEach((e, i) => {
      const at = lines[i];
      if (!at) { list.appendChild(node("li", null, e)); return; }
      // An error with a known line leads with a button that selects it.
      const item = node("li");
      const go = node("button", "spec-line", `줄 ${at}`);
      go.type = "button";
      go.setAttribute("aria-label", `줄 ${at}로 이동`);
      go.onclick = () => jump(at);
      item.appendChild(go);
      item.appendChild(node("span", null, e));
      list.appendChild(item);
    });
    return list;
  }

  /* The sessions an import starts, as a tree under the leader, and the work
     it files in the order it can begin. Shown as soon as the spec is valid. */
  function previewBlock() {
    const box = node("div", "spec-preview");
    const sessions = shown.sessions || [];
    box.appendChild(node("h3", null, `세션 미리보기 — ${sessions.length}개`));
    box.appendChild(table(["에이전트", "kind", "역할", "워크플로", "큐(첫 이슈로 spawn)", "리뷰", "연결"],
      sessions.map(s => [
        "  ".repeat(Math.max(0, s.depth - 1)) + (s.depth ? "└ " : "") + s.id +
          (s.kind === "leader" ? ` (${s.mode})` : ""),
        s.kind,
        [s.role, ...(s.subroles || [])].join(" + "),
        s.workflow || "—",
        (s.queue || []).join(", "),
        (s.reviews || []).join(", "),
        (s.connect || []).join(", "),
      ])));
    const tasks = [...(shown.tasks || [])].sort((a, b) => a.wave - b.wave || a.priority - b.priority);
    box.appendChild(node("h3", null, `작업 미리보기 — 이슈 ${tasks.length}개`));
    box.appendChild(node("p", "wt-muted",
      "단계: 선행 이슈(blocked_by)로 계산한 착수 순서(0부터). 결정 대기가 있는 이슈는 그 결정에 답이 날 때까지 착수하지 않는다."));
    box.appendChild(table(["단계", "key", "type", "P", "제목", "부모", "선행", "담당", "리뷰", "결정 대기"],
      tasks.map(t => [String(t.wave), t.key, t.type, String(t.priority), t.title, t.parent || "",
        t.blocked_by.join(", "), t.assignee, t.review, t.gated_by.join(", ")])));
    const contracts = shown.contracts || [];
    if (contracts.length) {
      box.appendChild(node("h3", null, `계약 — doc 이슈 ${contracts.length}개`));
      box.appendChild(table(["id", "kind", "제목", "당사자"],
        contracts.map(c => [c.id, c.kind || "", c.title, c.parties.join(", ")])));
    }
    const decisions = shown.decisions || [];
    if (decisions.length) {
      box.appendChild(node("h3", null, `미결 결정 ${decisions.length}건`));
      box.appendChild(table(["key", "질문", "권장", "막는 이슈"],
        decisions.map(d => [d.key, d.question, d.recommended, d.gates.join(", ")])));
    }
    return box;
  }

  /* The editor and its controls, built once per root and kept across
     redraws so typing keeps its caret. */
  function build(root) {
    const head = node("div", "wf-head");
    head.appendChild(node("h2", null, "Work spec"));
    const intro = node("p", "wt-muted",
      "명세 YAML을 붙여넣거나 파일로 읽고, 검증 → 미리보기 → 리더 선택 → plan → import 순서로 진행한다. 형식은 docs/work-spec.md, 작성 안내는 work-spec skill.");

    const source = node("div", "spec-row");
    const file = node("input");
    file.type = "file"; file.accept = ".yaml,.yml,text/yaml";
    file.setAttribute("aria-label", "명세 파일");
    file.onchange = async () => {
      const f = file.files && file.files[0];
      if (f) onText(await f.text());
    };
    source.appendChild(file);
    source.appendChild(button("예시 템플릿", insertTemplate));
    const toggle = node("label", "spec-auto");
    const box = node("input");
    box.type = "checkbox"; box.checked = auto;
    box.setAttribute("aria-label", "입력 중 자동 검증");
    box.onchange = () => { auto = !!box.checked; schedule(); };
    toggle.appendChild(box);
    toggle.appendChild(node("span", null, "입력 중 자동 검증"));
    source.appendChild(toggle);

    const editor = node("textarea", "spec-text");
    editor.setAttribute("aria-label", "명세 YAML");
    editor.rows = 18; editor.spellcheck = false;
    editor.value = text;
    editor.oninput = () => onText(editor.value);

    const actions = node("div", "spec-row");
    const checkButton = button("검증", check);
    actions.appendChild(checkButton);
    const status = node("span", "wt-muted");
    actions.appendChild(status);

    const body = node("div", "spec-body");
    root.replaceChildren(head, intro, source, editor, actions, body);
    return { root, editor, checkButton, status, body };
  }

  function render() {
    const root = view();
    if (!root) return;
    if (!frame || frame.root !== root) frame = build(root);
    if (frame.editor.value !== text) frame.editor.value = text;
    frame.checkButton.textContent = busy ? "처리 중…" : "검증";
    frame.checkButton.disabled = busy || !text.trim();
    frame.status.textContent = !text.trim() ? "" : busy ? "검증 중…"
      : spec ? "형식 오류 없음" : errors.length ? `오류 ${errors.length}건` : "";

    const parts = [];
    if (error) parts.push(node("p", "wt-error", error));
    if (errors.length) {
      parts.push(node("h3", null, `오류 ${errors.length}건`));
      parts.push(errorList());
    }
    if (spec) {
      parts.push(node("p", "spec-summary",
        `${spec.name}${spec.title ? ` — ${spec.title}` : ""} · 메시 ${spec.mesh} (${spec.mesh_mode})` +
        ` · 에이전트 ${spec.agents} · 이슈 ${spec.issues}` +
        (spec.workspace ? ` · workspace ${spec.workspace}` : "")));
      if (shown) parts.push(previewBlock());
      parts.push(boardBlock());
      parts.push(leaderBlock());
      const next = node("div", "spec-row");
      next.appendChild(button("plan 미리보기", preview, busy));
      next.appendChild(button("import 실행", runImport, busy || !plan || !!spec.board_error));
      parts.push(next);
    }
    if (plan) {
      parts.push(node("h3", null, `plan — action ${plan.length}개`));
      parts.push(table(["#", "action"], plan.map((p, i) => [String(i + 1), p.line])));
    }
    if (notice) parts.push(node("p", "wt-notice", notice));
    if (log && log.length) {
      parts.push(node("h3", null, "import 결과"));
      parts.push(table(["action", "대상", "결과", "내용"],
        log.map(r => [r.do, r.what, r.result, r.detail || ""])));
    }
    frame.body.replaceChildren(...parts);
  }

  function open() { render(); }

  /* Leaving the route: an answer still in flight no longer draws. */
  function stop() {
    sequence++; busy = false;
    if (timer !== null && typeof clearTimeout === "function") clearTimeout(timer);
    timer = null;
  }

  return { open, stop, leaderChoice };
})();
