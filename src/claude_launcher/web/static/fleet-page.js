/* The Fleet page (#/fleet): start a whole agent tree from a preset, and
   write the presets. Two tabs.

   Launch: pick a preset, the project (which fills its default workspace and
   mesh), the mesh, the root's opening task and the counts; each level -- and
   each member, if wanted -- gets a profile. The tree on the right is the
   daemon's expansion (POST /api/fleet/expand): the very work spec the
   import runs, so what is drawn is what starts. "Spec에서 열기" hands that
   spec to the Spec page (#/spec) to edit by hand. Launch is
   POST /api/fleet/launch -- the spec's import, run as the person.

   Presets: the list, and a structured editor of the selected preset -- its
   groups as a tree (kind, count, handle, workflow, relation, board issue,
   profile, tier, opening text), its count params and its meta -- with a
   live check (POST /api/fleet/check) that answers the members it would
   start, and the YAML the daemon saves. The YAML can be edited directly
   too; it is parsed by the daemon, never in the page.

   The format is docs/fleet.md; the model is claude_launcher/fleet.py. */
globalThis.FleetPage = (() => {
  const node = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const button = (label, onclick, cls = "", disabled = false) => {
    const b = node("button", `wf-btn${cls ? " " + cls : ""}`, label);
    b.type = "button"; b.disabled = disabled; b.onclick = onclick;
    return b;
  };
  const field = (label, control, hint) => {
    const box = node("label", "fl-field");
    box.appendChild(node("span", "fl-label", label));
    box.appendChild(control);
    if (hint) box.appendChild(node("span", "fl-hint", hint));
    return box;
  };
  const select = (aria, options, value, onchange) => {
    const s = node("select");
    s.setAttribute("aria-label", aria);
    for (const opt of options) {
      const [v, label] = Array.isArray(opt) ? opt : [opt, opt];
      const o = node("option", null, label); o.value = v;
      s.appendChild(o);
    }
    s.value = value == null ? "" : value;
    s.onchange = () => onchange(s.value);
    return s;
  };
  const input = (aria, value, oninput, placeholder = "", type = "text") => {
    const i = node("input");
    i.type = type;
    i.setAttribute("aria-label", aria);
    i.value = value == null ? "" : String(value);
    if (placeholder) i.placeholder = placeholder;
    i.oninput = () => oninput(i.value);
    return i;
  };
  const clone = v => JSON.parse(JSON.stringify(v));
  /* A <details> whose open state survives the redraws (every input
     redraws the page). */
  const opened = new Set();
  const fold = (key, summary) => {
    const d = node("details", "fl-adv");
    d.open = opened.has(key);
    d.ontoggle = () => { if (d.open) opened.add(key); else opened.delete(key); };
    d.appendChild(node("summary", null, summary));
    return d;
  };

  async function call(path, method = "GET", body) {
    const opts = { method };
    if (body !== undefined) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    const response = await api(path, opts);
    let out = {};
    try { out = await response.json(); } catch { out = {}; }
    return { ok: response.ok, status: response.status, body: out };
  }

  const KIND_LABEL = { coordinator: "coordinator", "sub-lead": "sub-lead", worker: "worker", reviewer: "reviewer" };
  const ISSUE_LABEL = { lane: "lane 이슈", pending: "시작 이슈 대기", none: "이슈 없음" };
  const RELATIONS = [["", "relation 없음"], ["independent", "independent → 리더 큐"], ["stacked", "stacked → 부모 스택"]];

  // ----------------------------------------------------------------- state
  let meta = null;             // /api/fleet/presets
  let catalog = { profiles: [], projects: [], workspaces: [], meshes: [] };
  let tab = "launch";
  let loadError = "";
  let sequence = 0, timer = null;
  const DELAY = 450;

  // Launch tab.
  const L = {
    preset: "", project: "", workspace: "", mesh: "", task: "", params: {},
    profiles: {}, defaultProfile: "", name: "", prefix: "",
    shape: null, expanded: null, error: "", notice: "", log: null, busy: false,
    launching: false, showYaml: false, perMember: false,
  };
  // Presets tab.
  const E = {
    name: "", doc: null, original: "", selected: "", errors: [], shape: null, yaml: "",
    yamlMode: false, yamlText: "", dirty: false, error: "", notice: "", busy: false,
  };

  function view() { return document.getElementById("fleet-view"); }
  function presetRow(name) { return (meta?.presets || []).find(p => p.name === name) || null; }

  // ------------------------------------------------------------- loading
  async function load() {
    loadError = "";
    try {
      const [presets, profiles, projects, workspaces, meshes] = await Promise.all([
        call("/api/fleet/presets"), call("/api/profiles"), call("/api/projects"),
        call("/api/workspaces"), call("/api/mesh?view=rail"),
      ]);
      if (!presets.ok) throw new Error(presets.body.error || `HTTP ${presets.status}`);
      meta = presets.body;
      catalog = {
        profiles: profiles.body.profiles || [],
        projects: (projects.body.projects || []).filter(p => !p.is_default || p.default_workspace),
        workspaces: (workspaces.body.workspaces || []).map(w => w.name),
        meshes: (meshes.body.meshes || []).map(m => m.name),
      };
      if (!presetRow(L.preset)) pickPreset(presetRow(meta.default) ? meta.default : (meta.presets[0] || {}).name || "");
      if (!E.doc && meta.presets.length) editPreset(L.preset || meta.presets[0].name);
    } catch (exc) {
      loadError = String(exc.message || exc);
    }
    render();
  }

  // ------------------------------------------------------------- launch
  function launchBody() {
    const profiles = {};
    for (const [k, v] of Object.entries(L.profiles)) if (v) profiles[k] = v;
    return {
      preset: L.preset,
      launch: {
        task: L.task, project: L.project, workspace: L.workspace, mesh: L.mesh.trim(),
        params: L.params, profiles, default_profile: L.defaultProfile,
        ...(L.name.trim() ? { name: L.name.trim() } : {}),
        ...(L.prefix.trim() ? { prefix: L.prefix.trim() } : {}),
      },
    };
  }

  function pickPreset(name) {
    L.preset = name; L.params = {}; L.profiles = {}; L.expanded = null; L.log = null;
    const row = presetRow(name);
    for (const [k, p] of Object.entries(row?.params || {})) L.params[k] = p.default;
    L.shape = row?.shape || null;
    changed();
  }

  function pickProject(name) {
    L.project = name;
    const p = catalog.projects.find(x => x.name === name);
    if (p) {
      if (p.default_workspace) L.workspace = p.default_workspace;
      if (p.default_mesh) L.mesh = p.default_mesh;
    }
    changed();
  }

  /* Any launch input moved: the old expansion no longer describes it. */
  function changed() {
    sequence++;
    L.expanded = null; L.error = ""; L.notice = "";
    if (timer !== null && typeof clearTimeout === "function") clearTimeout(timer);
    timer = null;
    if (typeof setTimeout === "function") timer = setTimeout(() => { timer = null; refresh(); }, DELAY);
    render();
  }

  /* The tree: the daemon's expansion once there is a task, else the
     preset's shape for these counts. */
  async function refresh() {
    if (!L.preset) return;
    const seq = ++sequence;
    L.busy = true; render();
    try {
      if (L.task.trim()) {
        const { ok, body } = await call("/api/fleet/expand", "POST", launchBody());
        if (seq !== sequence) return;
        if (ok) { L.expanded = body; L.shape = null; L.name = L.name || ""; L.error = ""; }
        else { L.expanded = null; L.error = body.error || "전개 실패"; }
      }
      if (!L.expanded) {
        const { ok, body } = await call("/api/fleet/check", "POST", { preset: L.preset, params: L.params });
        if (seq !== sequence) return;
        L.shape = ok && !(body.errors || []).length ? body.shape : null;
        if (ok && (body.errors || []).length) L.error = body.errors.join("\n");
      }
    } catch (exc) {
      if (seq === sequence) L.error = String(exc.message || exc);
    } finally {
      if (seq === sequence) { L.busy = false; render(); }
    }
  }

  async function launch() {
    const x = L.expanded;
    if (!x || L.launching) return;
    const what = `${x.agents.length}개 세션 · 메시 ${x.mesh} (${x.mesh_mode === "attach" ? "기존 메시에 합류" : "새 메시"}) · 보드 ${x.board_dir}`;
    if (!confirm(`fleet을 띄운다: ${what}\n이슈를 만들고 세션을 시작한다.`)) return;
    // Pin the clock-derived name and prefix to the ones previewed.
    const body = launchBody();
    body.launch.name = x.name;
    if (x.prefix) body.launch.prefix = x.prefix;
    L.launching = true; L.error = ""; L.notice = ""; render();
    try {
      const { ok, status, body: out } = await call("/api/fleet/launch", "POST", body);
      L.log = out.log || [];
      if (ok) L.notice = `fleet 시작됨 — action ${L.log.length}개. 메시 ${out.mesh}`;
      else {
        L.error = out.error || `HTTP ${status}`;
        if (out.resumable) {
          L.notice = "그 전에 실행된 action은 남아 있다. 원인을 고친 뒤 같은 버튼을 누르면 이어서 진행된다(같은 이름·접두사).";
          L.name = out.name || L.name; L.prefix = out.prefix || L.prefix;
        }
      }
    } catch (exc) {
      L.error = String(exc.message || exc);
    } finally {
      L.launching = false; render();
    }
  }

  function openInSpec() {
    const x = L.expanded;
    if (!x || !globalThis.SpecPage?.load) return;
    globalThis.SpecPage.load(x.yaml, { workspace: L.workspace, profile: L.defaultProfile });
    if (typeof location !== "undefined") location.hash = "#/spec";
  }

  // ----------------------------------------------------------- the tree
  /* Rows (handle, parent, ...) -> nested {row, kids}. */
  function nest(rows) {
    const by = new Map(), roots = [];
    for (const r of rows) by.set(r.handle, { row: r, kids: [] });
    for (const r of rows) {
      const parent = r.parent && by.get(rows.find(x => (x.id || x.handle) === r.parent)?.handle || r.parent);
      if (parent) parent.kids.push(by.get(r.handle)); else roots.push(by.get(r.handle));
    }
    return roots;
  }

  function chip(text, cls) { return node("span", `fl-chip ${cls || ""}`, text); }

  function card(r, editable) {
    const c = node("div", `fl-card fl-kind-${r.kind}`);
    const head = node("div", "fl-card-head");
    head.appendChild(node("span", "fl-dot"));
    head.appendChild(node("strong", "fl-handle", r.handle));
    head.appendChild(node("span", "fl-kind", KIND_LABEL[r.kind] || r.kind));
    c.appendChild(head);
    const chips = node("div", "fl-chips");
    chips.appendChild(chip(r.workflow || "워크플로 없음", "fl-wf"));
    if (r.relation) chips.appendChild(chip(r.relation, `fl-rel-${r.relation}`));
    chips.appendChild(chip(ISSUE_LABEL[r.issue] || r.issue, `fl-issue-${r.issue}`));
    c.appendChild(chips);
    if (editable) {
      const opts = [["", r.depth ? "레벨 값·상속" : "레벨 값·기본"], ...catalog.profiles.map(p => [p, p])];
      const pick = select(`${r.handle} 프로필`, opts, L.profiles[r.id || r.handle] || "", v => {
        L.profiles[r.id || r.handle] = v; changed();
      });
      pick.className = "fl-card-profile";
      const line = node("div", "fl-card-foot");
      line.appendChild(node("span", "fl-muted", r.profile ? `→ ${r.profile}${r.model ? " · " + r.model : ""}` : "부모 프로필 상속"));
      if (L.perMember) line.appendChild(pick);
      c.appendChild(line);
    } else if (r.profile !== undefined && r.profile) {
      c.appendChild(node("div", "fl-muted", `→ ${r.profile}`));
    }
    return c;
  }

  function treeOf(items, editable) {
    const box = node("div", "fl-kids");
    for (const it of items) box.appendChild(branch(it, editable));
    return box;
  }

  function branch(it, editable) {
    const b = node("div", "fl-node");
    b.appendChild(card(it.row, editable));
    if (it.kids.length) b.appendChild(treeOf(it.kids, editable));
    return b;
  }

  function tree(rows, editable) {
    const box = node("div", "fl-tree");
    for (const it of nest(rows)) box.appendChild(branch(it, editable));
    return box;
  }

  function counts(rows) {
    const by = {};
    for (const r of rows) by[r.kind] = (by[r.kind] || 0) + 1;
    return Object.entries(by).map(([k, n]) => `${KIND_LABEL[k] || k} ${n}`).join(" · ");
  }

  // ------------------------------------------------------ launch render
  function launchTab() {
    const grid = node("div", "fl-grid");
    const form = node("section", "fl-panel fl-form");
    const row = presetRow(L.preset);

    // Preset cards: the choice, with what each starts.
    form.appendChild(node("h3", "fl-step", "1 · 프리셋"));
    const cards = node("div", "fl-presets");
    for (const p of meta.presets) {
      const c = node("button", `fl-preset${p.name === L.preset ? " fl-on" : ""}${p.errors.length ? " fl-bad" : ""}`);
      c.type = "button";
      c.setAttribute("aria-label", `프리셋 ${p.name}`);
      c.appendChild(node("strong", null, p.title || p.name));
      c.appendChild(node("span", "fl-muted", `${p.name} · ${p.source}${p.errors.length ? " · 오류" : ""}`));
      c.disabled = !!p.errors.length;
      c.onclick = () => pickPreset(p.name);
      cards.appendChild(c);
    }
    form.appendChild(cards);
    if (row?.description) form.appendChild(node("p", "fl-desc", row.description));

    form.appendChild(node("h3", "fl-step", "2 · 위치"));
    const where = node("div", "fl-row");
    where.appendChild(field("Project", select("project", [["", "선택…"], ...catalog.projects.map(p => [p.name, p.name])], L.project, pickProject)));
    where.appendChild(field("Workspace", select("workspace", [["", "선택…"], ...catalog.workspaces.map(w => [w, w])], L.workspace, v => { L.workspace = v; changed(); })));
    const meshInput = input("mesh", L.mesh, v => { L.mesh = v; changed(); }, "비우면 spec-<이름> 새 메시");
    const exists = L.mesh.trim() && catalog.meshes.includes(L.mesh.trim());
    where.appendChild(field("Mesh", meshInput, L.mesh.trim() ? (exists ? "기존 메시에 합류 — independent 리드는 그 메시의 리더 큐로 착지" : "새 메시를 만든다") : ""));
    form.appendChild(where);
    if (catalog.meshes.length) {
      const pills = node("div", "fl-pills");
      for (const m of catalog.meshes.slice(0, 12)) {
        const p = button(m, () => { L.mesh = m; changed(); }, `fl-pill${m === L.mesh ? " fl-on" : ""}`);
        pills.appendChild(p);
      }
      form.appendChild(pills);
    }

    form.appendChild(node("h3", "fl-step", "3 · 루트의 오프닝 태스크"));
    const task = node("textarea", "fl-task");
    task.setAttribute("aria-label", "오프닝 태스크");
    task.rows = 6; task.value = L.task;
    task.placeholder = "코디네이터가 사용자와 조율할 목표. 첫 줄이 상위 epic의 제목이 된다.";
    task.oninput = () => { L.task = task.value; changed(); };
    form.appendChild(task);

    form.appendChild(node("h3", "fl-step", "4 · 규모와 프로필"));
    const params = Object.entries(row?.params || {});
    if (params.length) {
      const pr = node("div", "fl-row");
      for (const [k, p] of params) {
        const i = input(p.label || k, L.params[k], v => { L.params[k] = v === "" ? p.default : Number(v); changed(); }, "", "number");
        i.min = String(p.min ?? 0); i.max = String(p.max ?? meta.max_count);
        pr.appendChild(field(p.label || k, i, `${p.min ?? 0}–${p.max ?? meta.max_count}`));
      }
      form.appendChild(pr);
    }
    const levels = node("div", "fl-levels");
    levels.appendChild(field("기본 프로필(루트)", select("기본 프로필", [["", "선택…"], ...catalog.profiles.map(p => [p, p])], L.defaultProfile, v => { L.defaultProfile = v; changed(); })));
    for (const g of row?.groups || []) {
      const label = `${"  ".repeat(g.depth)}${g.label} (${g.kind}${g.depth ? ` ×${g.count}` : ""})`;
      const hint = g.tier ? `tier ${g.tier}` : g.profile ? `프리셋 기본 ${g.profile}` : g.depth ? "비우면 부모 상속" : "비우면 기본 프로필";
      levels.appendChild(field(label, select(`${g.id} 레벨 프로필`, [["", g.depth ? "상속" : "기본"], ...catalog.profiles.map(p => [p, p])], L.profiles[g.id] || "", v => { L.profiles[g.id] = v; changed(); }), hint));
    }
    form.appendChild(levels);
    const per = node("label", "fl-check");
    const box = node("input"); box.type = "checkbox"; box.checked = L.perMember;
    box.setAttribute("aria-label", "멤버별 프로필");
    box.onchange = () => { L.perMember = !!box.checked; render(); };
    per.appendChild(box); per.appendChild(node("span", null, "멤버별로 프로필을 따로 고른다(트리 카드)"));
    form.appendChild(per);

    const adv = fold("adv", "고급: 이름 · 핸들 접두사");
    const ar = node("div", "fl-row");
    ar.appendChild(field("spec 이름", input("spec 이름", L.name, v => { L.name = v; changed(); }, L.expanded?.name || "fleet-<프리셋>-<시각>")));
    ar.appendChild(field("핸들 접두사", input("핸들 접두사", L.prefix, v => { L.prefix = v; changed(); }, L.expanded?.prefix || "기존 메시면 f<시각>"),
      "기존 메시에서는 남의 핸들과 겹치지 않도록 접두사가 붙는다"));
    adv.appendChild(ar);
    form.appendChild(adv);
    grid.appendChild(form);

    // The tree and what the launch does.
    const side = node("section", "fl-panel fl-side");
    const x = L.expanded;
    const rows = x ? x.agents : (L.shape || []);
    const headline = node("div", "fl-side-head");
    headline.appendChild(node("h3", null, x ? "시작할 fleet" : "구성 미리보기"));
    headline.appendChild(node("span", "fl-muted", rows.length ? `${rows.length}개 세션 · ${counts(rows)}` : ""));
    side.appendChild(headline);
    side.appendChild(legend());
    if (L.busy) side.appendChild(node("p", "fl-muted", "계산 중…"));
    if (rows.length) side.appendChild(tree(rows, !!x));
    if (!x && rows.length) side.appendChild(node("p", "fl-muted", "오프닝 태스크를 적으면 daemon이 work spec으로 전개하고 각 멤버의 프로필·이슈를 채운다."));
    if (x) {
      const facts = node("div", "fl-facts");
      facts.appendChild(node("div", null, `spec ${x.name}`));
      facts.appendChild(node("div", null, `메시 ${x.mesh} (${x.mesh_mode === "attach" ? "합류" : "새로 만듦"})${x.prefix ? ` · 핸들 접두사 ${x.prefix}` : ""}`));
      facts.appendChild(node("div", x.board_error ? "wt-error" : null, x.board_error || `보드 ${x.board_dir}`));
      const tasks = (x.preview?.tasks || []);
      facts.appendChild(node("div", null, `보드 이슈 ${tasks.length}개: ${tasks.map(t => `${t.key}(${t.type})`).join(", ")}`));
      side.appendChild(facts);
      const plan = fold("plan", `plan — action ${x.lines.length}개`);
      const ol = node("ol", "fl-plan");
      for (const ln of x.lines) ol.appendChild(node("li", null, ln));
      plan.appendChild(ol);
      side.appendChild(plan);
      const yamlBox = fold("spec-yaml", "work spec YAML");
      yamlBox.appendChild(node("pre", "fl-yaml", x.yaml));
      side.appendChild(yamlBox);
    }
    if (L.error) side.appendChild(node("pre", "wt-error fl-err", L.error));
    if (L.notice) side.appendChild(node("p", "wt-notice", L.notice));
    const act = node("div", "fl-actions");
    act.appendChild(button("Spec에서 열기", openInSpec, "", !x));
    act.appendChild(button(L.launching ? "시작 중…" : "Fleet 시작", launch, "fl-primary",
      !x || L.launching || !!x.board_error));
    side.appendChild(act);
    if (L.log && L.log.length) {
      const t = node("table", "spec-table");
      const h = node("tr");
      for (const c of ["action", "대상", "결과", "내용"]) h.appendChild(node("th", null, c));
      t.appendChild(h);
      for (const r of L.log) {
        const tr = node("tr");
        for (const c of [r.do, r.what, r.result, r.detail || ""]) tr.appendChild(node("td", null, c));
        t.appendChild(tr);
      }
      side.appendChild(t);
    }
    grid.appendChild(side);
    return grid;
  }

  function legend() {
    const l = node("div", "fl-legend");
    for (const k of ["coordinator", "sub-lead", "worker", "reviewer"]) {
      const s = node("span", `fl-key fl-kind-${k}`);
      s.appendChild(node("span", "fl-dot"));
      s.appendChild(node("span", null, k));
      l.appendChild(s);
    }
    l.appendChild(chip("independent", "fl-rel-independent"));
    l.appendChild(chip("stacked", "fl-rel-stacked"));
    return l;
  }

  // ------------------------------------------------------------ editor
  function editPreset(name) {
    const row = presetRow(name);
    if (!row) return;
    E.name = name; E.original = name; E.dirty = false; E.error = ""; E.notice = "";
    E.yamlMode = false; E.selected = "";
    check({ yaml: row.yaml }, true);
  }

  function newPreset(from) {
    const base = from ? clone(from) : { fleet: 1, name: "my-fleet", title: "", description: "", params: {},
      root: { id: "coord", kind: "coordinator", label: "코디네이터", handle: "coord", workflow: "improv-coordinator-v2", children: [] } };
    const taken = new Set(meta.presets.map(p => p.name));
    let name = from ? `${base.name}-copy` : base.name, k = 2;
    while (taken.has(name)) name = `${from ? base.name + "-copy" : base.name}-${k++}`;
    base.name = name.slice(0, 32);
    E.original = ""; E.name = base.name; E.doc = base; E.selected = base.root.id;
    E.dirty = true; E.error = ""; E.notice = ""; E.yamlMode = false;
    check({ doc: E.doc });
  }

  /* POST /api/fleet/check: the parsed doc, its YAML, errors, shape. */
  async function check(body, adopt = false) {
    const seq = ++sequence;
    E.busy = true; render();
    try {
      const { ok, body: out } = await call("/api/fleet/check", "POST", body);
      if (seq !== sequence) return;
      if (!ok) { E.errors = out.errors || [out.error || "읽을 수 없음"]; E.shape = null; return; }
      if (adopt || E.yamlMode || !E.doc) E.doc = out.doc;
      if (!E.selected || !findNode(E.selected)) E.selected = E.doc?.root?.id || "";
      E.errors = out.errors || [];
      E.shape = out.shape || null;
      E.yaml = out.yaml || "";
      if (!E.yamlMode) E.yamlText = E.yaml;
    } catch (exc) {
      if (seq === sequence) E.error = String(exc.message || exc);
    } finally {
      if (seq === sequence) { E.busy = false; render(); }
    }
  }

  function edited() {
    E.dirty = true; E.notice = "";
    if (timer !== null && typeof clearTimeout === "function") clearTimeout(timer);
    timer = null;
    const go = () => { timer = null; check(E.yamlMode ? { yaml: E.yamlText } : { doc: E.doc }); };
    if (typeof setTimeout === "function") timer = setTimeout(go, DELAY);
    render();
  }

  function walk(fn, n = E.doc?.root, parent = null) {
    if (!n) return null;
    const hit = fn(n, parent);
    if (hit) return hit;
    for (const c of n.children || []) {
      const got = walk(fn, c, n);
      if (got) return got;
    }
    return null;
  }
  const findNode = id => walk(n => (n.id === id ? n : null));
  const parentOf = id => walk((n, p) => (n.id === id ? (p || "root") : null));

  function depthOf(id) {
    let d = 0, p = parentOf(id);
    while (p && p !== "root") { d++; p = parentOf(p.id); }
    return d;
  }

  function freshId(base) {
    let i = 1;
    while (findNode(`${base}${i}`)) i++;
    return `${base}${i}`;
  }

  function addChild(id) {
    const n = findNode(id);
    if (!n) return;
    const depth = depthOf(id) + 1;
    const child = depth === 1
      ? { id: freshId("lead"), kind: "sub-lead", label: "리드", count: 1, workflow: "improv-worker-v2", inputs: { relation: "independent" }, issue: "lane", children: [] }
      : { id: freshId("w"), kind: "worker", label: "워커", count: 1, workflow: "improv-worker-v2", inputs: { relation: "stacked" }, issue: "pending" };
    n.children = [...(n.children || []), child];
    E.selected = child.id;
    edited();
  }

  function removeNode(id) {
    const p = parentOf(id);
    if (!p || p === "root") return;
    p.children = p.children.filter(c => c.id !== id);
    E.selected = p.id;
    edited();
  }

  function moveNode(id, delta) {
    const p = parentOf(id);
    if (!p || p === "root") return;
    const i = p.children.findIndex(c => c.id === id), j = i + delta;
    if (j < 0 || j >= p.children.length) return;
    const list = [...p.children];
    [list[i], list[j]] = [list[j], list[i]];
    p.children = list;
    edited();
  }

  async function save() {
    if (!E.doc) return;
    E.busy = true; E.error = ""; render();
    try {
      const body = E.yamlMode ? { yaml: E.yamlText } : { doc: E.doc };
      const name = E.yamlMode ? (E.doc?.name || E.name) : E.doc.name;
      const { ok, status, body: out } = await call(`/api/fleet/presets/${encodeURIComponent(name)}`, "PUT", body);
      if (!ok) { E.error = out.error || `HTTP ${status}`; E.errors = out.errors || E.errors; return; }
      E.dirty = false; E.original = name; E.name = name;
      E.notice = `저장됨 — ${name}${out.preset?.overrides ? " (내장 프리셋을 대신한다)" : ""}`;
      await load();
    } catch (exc) {
      E.error = String(exc.message || exc);
    } finally {
      E.busy = false; render();
    }
  }

  async function remove() {
    const row = presetRow(E.original);
    if (!row || row.source !== "user") return;
    if (!confirm(`프리셋 ${row.name}을 지운다.${row.overrides ? " 같은 이름의 내장 프리셋이 다시 보인다." : ""}`)) return;
    const { ok, body } = await call(`/api/fleet/presets/${encodeURIComponent(row.name)}`, "DELETE");
    if (!ok) { E.error = body.error || "삭제 실패"; render(); return; }
    E.doc = null; E.notice = `지움 — ${row.name}`;
    await load();
  }

  function groupForm(n) {
    const box = node("div", "fl-group-form");
    const depth = depthOf(n.id);
    const set = (key, v) => { if (v === "" || v == null) delete n[key]; else n[key] = v; edited(); };
    const head = node("div", "fl-row");
    head.appendChild(field("그룹 id", input("그룹 id", n.id, v => { n.id = v.trim(); E.selected = n.id; edited(); })));
    head.appendChild(field("이름(표시)", input("그룹 이름", n.label, v => set("label", v))));
    head.appendChild(field("kind", select("kind", meta.kinds, n.kind || "worker", v => set("kind", v))));
    box.appendChild(head);
    const shape = node("div", "fl-row");
    if (depth) {
      const params = Object.keys(E.doc.params || {});
      const isParam = typeof n.count === "string";
      shape.appendChild(field("개수", select("개수 방식", [["fixed", "고정"], ...params.map(p => [`param:${p}`, `param ${p}`])],
        isParam ? `param:${n.count}` : "fixed", v => set("count", v === "fixed" ? 1 : v.slice(6)))));
      if (!isParam) shape.appendChild(field("고정 개수", input("고정 개수", n.count ?? 1, v => set("count", Number(v)), "", "number")));
    }
    shape.appendChild(field("핸들", input("핸들 템플릿", n.handle, v => set("handle", v),
      depth === 0 ? "{id}" : depth === 1 ? "{id}{n}" : "{parent}-{id}{n}"), "{n} 순번 · {parent} 부모 핸들 · {id} 그룹 id"));
    box.appendChild(shape);
    const run = node("div", "fl-row");
    run.appendChild(field("workflow", input("workflow", n.workflow, v => set("workflow", v), "improv-worker-v2")));
    run.appendChild(field("relation", select("relation", RELATIONS, n.inputs?.relation || "", v => {
      const inputs = { ...(n.inputs || {}) };
      if (v) inputs.relation = v; else delete inputs.relation;
      set("inputs", Object.keys(inputs).length ? inputs : "");
    }), "independent: master 기준, 리더 큐 착지 · stacked: 부모 스택 위"));
    if (depth) run.appendChild(field("보드 이슈", select("보드 이슈", meta.issue_modes.map(m => [m, ISSUE_LABEL[m]]), n.issue || "pending", v => set("issue", v)),
      "lane: 부모 이슈 아래 자기 이슈 · 대기: 부모가 일감을 준다"));
    box.appendChild(run);
    const who = node("div", "fl-row");
    who.appendChild(field("기본 프로필", select("그룹 프로필", [["", "없음(실행 시 선택·상속)"], ...catalog.profiles.map(p => [p, p])], n.profile || "", v => set("profile", v))));
    who.appendChild(field("tier", select("tier", [["", "없음"], ...(meta.tiers || []).map(t => [t, t])], n.tier || "", v => set("tier", v)),
      "Settings의 model tier가 정한 프로필·모델"));
    who.appendChild(field("model", input("model", n.model, v => set("model", v), "비우면 프로필 기본")));
    box.appendChild(who);
    const t = node("textarea", "fl-task");
    t.setAttribute("aria-label", "그룹 오프닝 추가 문구");
    t.rows = 3; t.value = n.task || "";
    t.placeholder = depth ? "이 그룹 멤버의 오프닝 태스크에 덧붙일 문구" : "루트 오프닝 태스크 뒤에 덧붙일 문구";
    t.oninput = () => set("task", t.value);
    box.appendChild(field("오프닝 추가 문구", t));
    const ops = node("div", "fl-actions");
    const holds = (meta.holders || []).includes(n.kind || "worker");
    ops.appendChild(button("+ 하위 그룹", () => addChild(n.id), "", !holds || depth >= 3));
    if (depth) {
      ops.appendChild(button("↑", () => moveNode(n.id, -1)));
      ops.appendChild(button("↓", () => moveNode(n.id, 1)));
      ops.appendChild(button("그룹 삭제", () => removeNode(n.id), "fl-danger"));
    }
    box.appendChild(ops);
    return box;
  }

  function groupTree() {
    const box = node("div", "fl-gtree");
    const rec = (n, depth) => {
      const b = button("", () => { E.selected = n.id; render(); },
        `fl-gnode fl-kind-${n.kind || "worker"}${n.id === E.selected ? " fl-on" : ""}`);
      b.setAttribute("aria-label", `그룹 ${n.id}`);
      b.style && (b.style.marginLeft = `${depth * 18}px`);
      b.appendChild(node("span", "fl-dot"));
      b.appendChild(node("strong", null, n.label || n.id));
      const count = depth ? (typeof n.count === "string" ? `×${n.count}` : `×${n.count ?? 1}`) : "";
      b.appendChild(node("span", "fl-muted", `${n.kind || "worker"} ${count}`));
      box.appendChild(b);
      for (const c of n.children || []) rec(c, depth + 1);
    };
    if (E.doc?.root) rec(E.doc.root, 0);
    return box;
  }

  function paramsForm() {
    const box = node("div", "fl-params");
    const params = E.doc.params || (E.doc.params = {});
    Object.entries(params).forEach(([k, p], i) => {
      const r = node("div", "fl-row");
      // Labelled by position: the name itself is what is being typed.
      r.appendChild(field("param", input(`param ${i + 1} 이름`, k, v => {
        const next = {};
        for (const [kk, vv] of Object.entries(params)) next[kk === k ? v : kk] = vv;
        E.doc.params = next;
        walk(n => { if (n.count === k) n.count = v; return null; });
        edited();
      })));
      r.appendChild(field("표시", input(`param ${i + 1} 표시`, p.label, v => { p.label = v; edited(); })));
      for (const key of ["default", "min", "max"]) {
        r.appendChild(field(key, input(`param ${i + 1} ${key}`, p[key], v => { p[key] = Number(v); edited(); }, "", "number")));
      }
      r.appendChild(button("삭제", () => { delete params[k]; edited(); }, "fl-danger"));
      box.appendChild(r);
    });
    box.appendChild(button("+ 개수 param", () => {
      let i = 1; while (params[`n${i}`]) i++;
      params[`n${i}`] = { label: `개수 ${i}`, default: 1, min: 0, max: 4 };
      edited();
    }));
    return box;
  }

  function editTab() {
    const grid = node("div", "fl-grid fl-grid-edit");
    const list = node("section", "fl-panel fl-list");
    list.appendChild(node("h3", null, "프리셋"));
    for (const p of meta.presets) {
      const b = button("", () => {
        if (E.dirty && !confirm("저장하지 않은 편집을 버린다.")) return;
        editPreset(p.name);
      }, `fl-preset${p.name === E.original ? " fl-on" : ""}${p.errors.length ? " fl-bad" : ""}`);
      b.appendChild(node("strong", null, p.title || p.name));
      b.appendChild(node("span", "fl-muted", `${p.name} · ${p.source}${p.overrides ? " (내장 대체)" : ""}`));
      list.appendChild(b);
    }
    const ops = node("div", "fl-actions");
    ops.appendChild(button("+ 새 프리셋", () => newPreset(null)));
    ops.appendChild(button("복제", () => E.doc && newPreset(E.doc), "", !E.doc));
    const row = presetRow(E.original);
    ops.appendChild(button("삭제", remove, "fl-danger", !row || row.source !== "user"));
    list.appendChild(ops);
    if (meta.dir) list.appendChild(node("p", "fl-muted", `저장 위치: ${meta.dir}`));
    grid.appendChild(list);

    const main = node("section", "fl-panel fl-editor");
    if (!E.doc) {
      main.appendChild(node("p", "fl-muted", E.busy ? "읽는 중…" : "프리셋을 고른다."));
    } else {
      const head = node("div", "fl-side-head");
      head.appendChild(node("h3", null, `${E.doc.name || "(이름 없음)"}${E.dirty ? " · 저장 안 됨" : ""}`));
      const modes = node("div", "fl-seg");
      modes.appendChild(button("구조", () => { E.yamlMode = false; render(); }, E.yamlMode ? "" : "fl-on"));
      modes.appendChild(button("YAML", () => { E.yamlMode = true; E.yamlText = E.yaml; render(); }, E.yamlMode ? "fl-on" : ""));
      head.appendChild(modes);
      main.appendChild(head);
      if (E.yamlMode) {
        const t = node("textarea", "fl-yaml-edit");
        t.setAttribute("aria-label", "프리셋 YAML");
        t.rows = 24; t.spellcheck = false; t.value = E.yamlText;
        t.oninput = () => { E.yamlText = t.value; edited(); };
        main.appendChild(t);
      } else {
        const m = node("div", "fl-row");
        m.appendChild(field("이름", input("프리셋 이름", E.doc.name, v => { E.doc.name = v.trim(); edited(); }), "파일 이름이 된다"));
        m.appendChild(field("제목", input("프리셋 제목", E.doc.title, v => { E.doc.title = v; edited(); })));
        main.appendChild(m);
        const d = node("textarea", "fl-task");
        d.setAttribute("aria-label", "프리셋 설명"); d.rows = 3; d.value = E.doc.description || "";
        d.oninput = () => { E.doc.description = d.value; edited(); };
        main.appendChild(field("설명", d));
        main.appendChild(node("h4", "fl-step", "개수 param"));
        main.appendChild(paramsForm());
        main.appendChild(node("h4", "fl-step", "그룹 트리"));
        const split = node("div", "fl-split");
        split.appendChild(groupTree());
        const sel = findNode(E.selected);
        split.appendChild(sel ? groupForm(sel) : node("p", "fl-muted", "그룹을 고른다."));
        main.appendChild(split);
      }
      if (E.errors.length) {
        const ul = node("ul", "spec-errors");
        for (const e of E.errors) ul.appendChild(node("li", null, e));
        main.appendChild(ul);
      }
      if (E.error) main.appendChild(node("p", "wt-error", E.error));
      if (E.notice) main.appendChild(node("p", "wt-notice", E.notice));
      const act = node("div", "fl-actions");
      act.appendChild(button(E.busy ? "확인 중…" : "저장", save, "fl-primary", E.busy || !E.dirty || !!E.errors.length));
      main.appendChild(act);
    }
    grid.appendChild(main);

    const side = node("section", "fl-panel fl-side");
    const rows = E.shape || [];
    const sh = node("div", "fl-side-head");
    sh.appendChild(node("h3", null, "기본 개수로 띄우면"));
    sh.appendChild(node("span", "fl-muted", rows.length ? `${rows.length}개 세션 · ${counts(rows)}` : ""));
    side.appendChild(sh);
    side.appendChild(legend());
    if (rows.length) side.appendChild(tree(rows, false));
    else side.appendChild(node("p", "fl-muted", E.errors.length ? "오류를 고치면 구성이 보인다." : ""));
    if (!E.yamlMode && E.yaml) {
      const y = fold("preset-yaml", "저장될 YAML");
      y.appendChild(node("pre", "fl-yaml", E.yaml));
      side.appendChild(y);
    }
    grid.appendChild(side);
    return grid;
  }

  // ------------------------------------------------------------ render
  /* The control being typed in, so a redraw can give it back its focus and
     caret: controls are found again by their aria-label. */
  function focused(root) {
    const a = typeof document.activeElement !== "undefined" ? document.activeElement : null;
    if (!a || !a.getAttribute || !root.contains || !root.contains(a)) return null;
    const key = a.getAttribute("aria-label");
    if (!key) return null;
    let start = null, end = null;
    try { start = a.selectionStart; end = a.selectionEnd; } catch { /* number inputs */ }
    return { key, start, end };
  }

  function refocus(root, f) {
    if (!f || !root.querySelectorAll) return;
    const hit = [...root.querySelectorAll("[aria-label]")].find(n => n.getAttribute("aria-label") === f.key);
    if (!hit) return;
    hit.focus();
    try { if (f.start != null) hit.setSelectionRange(f.start, f.end); } catch { /* not a text control */ }
  }

  function render() {
    const root = view();
    if (!root) return;
    const f = focused(root);
    const shell = node("div", "fl-shell");
    const head = node("header", "fl-head");
    const titles = node("div");
    titles.appendChild(node("span", "fl-eyebrow", "AGENT FLEET"));
    titles.appendChild(node("h2", null, "Fleet"));
    titles.appendChild(node("p", "fl-muted", "프리셋 하나로 코디네이터 · 리드 · 워커 트리를 한 번에 띄운다. 프리셋은 work spec으로 전개되어 같은 import로 실행된다."));
    head.appendChild(titles);
    const tabs = node("div", "fl-seg fl-tabs");
    tabs.appendChild(button("Launch", () => { tab = "launch"; render(); }, tab === "launch" ? "fl-on" : ""));
    tabs.appendChild(button("Presets", () => { tab = "edit"; render(); }, tab === "edit" ? "fl-on" : ""));
    head.appendChild(tabs);
    shell.appendChild(head);
    if (loadError) shell.appendChild(node("p", "wt-error", loadError));
    else if (!meta) shell.appendChild(node("p", "fl-muted", "읽는 중…"));
    else shell.appendChild(tab === "launch" ? launchTab() : editTab());
    root.replaceChildren(shell);
    refocus(root, f);
  }

  function open(which) {
    if (which === "presets") tab = "edit";
    else if (which === "launch") tab = "launch";
    render();
    load();
  }

  function stop() {
    sequence++;
    L.busy = false; E.busy = false;
    if (timer !== null && typeof clearTimeout === "function") clearTimeout(timer);
    timer = null;
  }

  return { open, stop, _state: () => ({ L, E, tab }), _refresh: refresh };
})();
