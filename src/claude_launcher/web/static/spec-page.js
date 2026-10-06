/* The Work spec page (#/spec): paste or load a work spec YAML, check it,
   choose the leader, preview the plan and run the import. The three verbs
   are POST /api/spec/check · plan · import (daemon/spec_api.py) — the same
   code `claunch spec` runs. The format is docs/work-spec.md. */
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
  // errors: the validator's lines; plan/log: the last answers.
  let text = "", spec = null, errors = [], plan = null, log = null;
  let leaderKind = "", leaderSession = "", profile = "";
  let notice = "", error = "", leaderError = "", busy = false;
  let sequence = 0;

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
      body: JSON.stringify({ yaml: text, ...extra }),
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
      spec = ok ? body.spec : null;
      if (!ok && !errors.length) error = body.error || "검증 실패";
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
    const what = `${spec.name} → 메시 ${spec.mesh}, 리더 ${choice || "(메시의 현 리더)"}`;
    if (!confirm(`import 를 실행한다: ${what}\n이슈를 만들고 세션을 띄운다.`)) return;
    return run("import", { leader: choice, profile: profile.trim() }, ({ ok, status, body }) => {
      log = body.log || [];
      if (ok) { notice = `import 완료 — ${log.length}개 action`; return; }
      if (body.leader_error) { leaderError = body.error; return; }
      error = body.error || `HTTP ${status}`;
      if (body.resumable) notice = "그 전에 실행된 action은 남아 있다. 원인을 고친 뒤 같은 버튼으로 다시 실행하면 이어서 진행된다.";
    });
  }

  function onText(value) {
    text = value;
    // A changed spec is an unchecked spec: nothing below it describes it any more.
    spec = null; errors = []; plan = null; log = null; notice = ""; error = "";
    render();
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

  function render() {
    const root = view();
    if (!root) return;
    const parts = [];
    const head = node("div", "wf-head");
    head.appendChild(node("h2", null, "Work spec"));
    parts.push(head);
    parts.push(node("p", "wt-muted",
      "명세 YAML을 붙여넣거나 파일로 읽고, 검증 → 리더 선택 → plan 미리보기 → import 순서로 진행한다. 형식은 docs/work-spec.md."));

    const source = node("div", "spec-row");
    const file = node("input");
    file.type = "file"; file.accept = ".yaml,.yml,text/yaml";
    file.setAttribute("aria-label", "명세 파일");
    file.onchange = async () => {
      const f = file.files && file.files[0];
      if (f) onText(await f.text());
    };
    source.appendChild(file);
    parts.push(source);
    const area = node("textarea", "spec-text");
    area.setAttribute("aria-label", "명세 YAML");
    area.rows = 18; area.spellcheck = false;
    area.value = text;
    area.oninput = () => onText(area.value);
    parts.push(area);

    const actions = node("div", "spec-row");
    actions.appendChild(button(busy ? "처리 중…" : "검증", check, busy || !text.trim()));
    parts.push(actions);

    if (error) parts.push(node("p", "wt-error", error));
    if (errors.length) {
      parts.push(node("h3", null, `오류 ${errors.length}건`));
      const list = node("ul", "spec-errors");
      for (const e of errors) list.appendChild(node("li", null, e));
      parts.push(list);
    }
    if (spec) {
      parts.push(node("p", "spec-summary",
        `${spec.name}${spec.title ? ` — ${spec.title}` : ""} · 메시 ${spec.mesh} (${spec.mesh_mode})` +
        ` · 에이전트 ${spec.agents} · 이슈 ${spec.issues}` +
        (spec.workspace ? ` · workspace ${spec.workspace}` : "")));
      parts.push(leaderBlock());
      const next = node("div", "spec-row");
      next.appendChild(button("plan 미리보기", preview, busy));
      next.appendChild(button("import 실행", runImport, busy || !plan));
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
    root.replaceChildren(...parts);
  }

  function open() { render(); }

  /* Leaving the route: an answer still in flight no longer draws. */
  function stop() { sequence++; busy = false; }

  return { open, stop, leaderChoice };
})();
