/* Session wrap-up drafts (claunch-ar8jn): the terminal's split pane (app.js
   openSplit "wrapup") over /api/wrapup. A session files glossary terms,
   category placements and knowledge records as drafts; here the user edits,
   deletes or confirms them, one at a time or a tab at once. Only a confirm
   writes an entry into its workspace store (daemon/wrapup_api.py). All user
   text uses textContent.

   Every write names the revision the pane last read; a 409 carries the
   current draft, which replaces the one on screen, and the user decides
   again. A draft's unsaved edits are kept per draft id across polls, and a
   confirm with unsaved edits saves them first. */

const WRAPUP_KINDS = [
  {id: "glossary", label: "Glossary"},
  {id: "category", label: "Category"},
  {id: "knowledge", label: "Knowledge"},
];
const WRAPUP_KNOWLEDGE_KINDS = ["vision", "need", "decision", "principle", "lesson"];
const WRAPUP_STATUS = {draft: "임시본", applying: "반영 중", confirmed: "확정됨", failed: "반영 실패", discarded: "삭제됨"};
const WRAPUP_CATEGORY_OPS = ["node_id", "new", "fold", "split", "group"];

function wrapupNode(tag, text, cls) {
  const n = document.createElement(tag);
  if (text != null) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}

const wrapupList = (text) => String(text || "").split(/[\n,]/).map(s => s.trim()).filter(Boolean);

function wrapupCategoryOp(payload) {
  return WRAPUP_CATEGORY_OPS.find(op => payload && payload[op] != null && payload[op] !== "") || "new";
}

/* The form's fields for a payload, as strings the inputs hold. */
function wrapupFormOf(kind, p) {
  p = p || {};
  if (kind === "glossary") {
    return {term: p.term || "", definition: p.definition || "", aliases: (p.aliases || []).join(", "), source: p.source || ""};
  }
  if (kind === "knowledge") {
    return {kind: p.kind || "decision", title: p.title || "", body: p.body || "",
            tags: (p.tags || []).join(", "), refs: (p.refs || []).join(", "), source: p.source || ""};
  }
  const op = wrapupCategoryOp(p);
  const form = {op, node_id: "", label: "", aliases: "", description: "", parent_id: "", json: ""};
  if (op === "node_id") form.node_id = p.node_id || "";
  else if (op === "new") {
    const n = p.new || {};
    Object.assign(form, {label: n.label || "", aliases: (n.aliases || []).join(", "),
                         description: n.description || "", parent_id: n.parent_id || ""});
  } else form.json = JSON.stringify(p[op] || {}, null, 2);
  return form;
}

/* The payload a form becomes. A category draft keeps its items and reason
   from the draft; an unparseable JSON operation throws for the pane to show. */
function wrapupPayloadOf(kind, form, before) {
  if (kind === "glossary") {
    return {term: form.term.trim(), definition: form.definition.trim(), aliases: wrapupList(form.aliases), source: form.source.trim()};
  }
  if (kind === "knowledge") {
    const out = {kind: form.kind, title: form.title.trim(), body: form.body.trim(),
                 tags: wrapupList(form.tags), refs: wrapupList(form.refs), source: form.source.trim()};
    if (before && before.supersedes) out.supersedes = before.supersedes;
    return out;
  }
  const out = {};
  if (form.op === "node_id") out.node_id = form.node_id.trim();
  else if (form.op === "new") {
    out.new = {label: form.label.trim(), aliases: wrapupList(form.aliases), description: form.description.trim()};
    if (form.parent_id.trim()) out.new.parent_id = form.parent_id.trim();
  } else {
    let parsed;
    try { parsed = JSON.parse(form.json || "{}"); } catch (exc) { throw Error(`${form.op}: JSON이 올바르지 않습니다 (${exc.message})`); }
    out[form.op] = parsed;
  }
  if (before && before.items) out.items = before.items;
  if (before && before.reason) out.reason = before.reason;
  return out;
}

/* One line naming a draft: its term, title or category operation. */
function wrapupSummary(d) {
  const p = d.payload || {};
  if (d.kind === "glossary") return p.term || "";
  if (d.kind === "knowledge") return `[${p.kind || ""}] ${p.title || ""}`;
  const op = wrapupCategoryOp(p);
  if (op === "node_id") return `기존 노드 ${p.node_id}에 배치`;
  return `${op} ${(p[op] && p[op].label) || ""}`.trim();
}

function makeWrapupPane({root, session, request}) {
  const node = wrapupNode;
  const base = "/api/wrapup";
  let data = null, error = "", busy = false, seq = 0, timer = null, tab = null;
  const forms = {};      // draft id -> {revision, form, dirty}
  const notes = {};      // draft id -> the last write's message

  async function load() {
    const mine = ++seq;
    try {
      const out = await request(`${base}?session=${encodeURIComponent(session)}`);
      if (mine !== seq) return;
      data = out; error = "";
      if (!tab) tab = (WRAPUP_KINDS.find(k => (out.counts || {})[k.id] > 0) || WRAPUP_KINDS[0]).id;
    } catch (exc) { if (mine === seq) error = exc.message; }
    if (mine === seq) { render(); schedule(); }
  }
  function schedule() { clearTimeout(timer); timer = setTimeout(load, 5000); }

  function formFor(d) {
    const kept = forms[d.id];
    // A draft that moved under unsaved edits keeps the edits; one that moved
    // with none is shown as it now stands.
    if (!kept || (!kept.dirty && kept.revision !== d.revision)) forms[d.id] = {revision: d.revision, form: wrapupFormOf(d.kind, d.payload), dirty: false};
    return forms[d.id];
  }

  /* Run a write; a 409 hands back the current draft, which replaces the
     form (the user sees what moved and decides again). */
  async function act(d, fn, done) {
    if (busy) return;
    busy = true; notes[d.id] = ""; render();
    try { await fn(); notes[d.id] = done || ""; }
    catch (exc) {
      notes[d.id] = exc.message;
      const current = exc.body && (exc.body.current || exc.body.draft);
      if (current && current.id === d.id) delete forms[d.id];
    }
    finally { busy = false; await load(); }
  }

  async function save(d) {
    const f = forms[d.id];
    const out = await request(`${base}/${encodeURIComponent(d.id)}`, "POST",
      {revision: f.revision, payload: wrapupPayloadOf(d.kind, f.form, d.payload)});
    delete forms[d.id];
    return out.draft;
  }

  async function confirm(d) {
    let revision = d.revision;
    if (forms[d.id] && forms[d.id].dirty) revision = (await save(d)).revision;
    await request(`${base}/${encodeURIComponent(d.id)}/confirm`, "POST", {revision});
  }

  async function confirmAll() {
    if (busy) return;
    const dirty = (data?.drafts || []).filter(d => d.kind === tab && forms[d.id]?.dirty);
    if (dirty.length) { error = "저장하지 않은 편집이 있습니다. 먼저 저장하거나 항목별로 확정하세요."; render(); return; }
    busy = true; error = ""; render();
    try {
      const out = await request(`${base}/confirm-all`, "POST", {target: session, kind: tab});
      if (out.failed) error = `${out.failed}건은 반영되지 않았습니다. 항목의 오류를 확인하세요.`;
    } catch (exc) { error = exc.message; }
    finally { busy = false; await load(); }
  }

  function field(f, key, label, multiline) {
    const input = node(multiline ? "textarea" : "input");
    input.value = f.form[key]; input.setAttribute("aria-label", label);
    input.disabled = busy;
    input.oninput = () => { f.form[key] = input.value; f.dirty = true; };
    const row = node("label", null, "wrapup-field");
    row.append(node("span", label, "wrapup-field-label"), input);
    return row;
  }

  function editor(d, f) {
    const box = node("div", null, "wrapup-editor");
    if (d.kind === "glossary") {
      box.append(field(f, "term", "용어"), field(f, "definition", "정의", true),
                 field(f, "aliases", "별칭 (쉼표로 구분)"), field(f, "source", "출처"));
    } else if (d.kind === "knowledge") {
      const sel = node("select"); sel.setAttribute("aria-label", "종류"); sel.disabled = busy;
      for (const k of WRAPUP_KNOWLEDGE_KINDS) { const o = node("option", k); o.value = k; sel.append(o); }
      sel.value = f.form.kind;
      sel.onchange = () => { f.form.kind = sel.value; f.dirty = true; };
      const row = node("label", null, "wrapup-field"); row.append(node("span", "종류", "wrapup-field-label"), sel);
      box.append(row, field(f, "title", "제목"), field(f, "body", "본문", true),
                 field(f, "tags", "태그 (쉼표로 구분)"), field(f, "refs", "참조 (쉼표로 구분)"), field(f, "source", "출처"));
    } else {
      if (f.form.op === "node_id") box.append(field(f, "node_id", "배치할 노드 id"));
      else if (f.form.op === "new") {
        box.append(field(f, "label", "새 카테고리 이름"), field(f, "aliases", "별칭 (쉼표로 구분)"),
                   field(f, "description", "설명"), field(f, "parent_id", "상위 노드 id (비우면 최상위)"));
      } else box.append(field(f, "json", `${f.form.op} (category_assign 인자, JSON)`, true));
      const items = d.payload.items || [];
      if (items.length) box.append(node("p", "대상: " + items.map(i => `${i.kind} ${i.ref}`).join(", "), "wrapup-muted"));
    }
    return box;
  }

  function card(d) {
    const f = formFor(d);
    const el = node("article", null, "wrapup-card wrapup-" + d.status);
    const head = node("div", null, "wrapup-card-head");
    head.append(node("span", WRAPUP_STATUS[d.status] || d.status, "wrapup-chip"), node("strong", wrapupSummary(d)),
                node("span", d.id + " · rev " + d.revision, "wrapup-muted"));
    if (d.edited_by_user) head.append(node("span", "사용자 편집", "wrapup-chip"));
    el.append(head);
    if (d.reason) el.append(node("p", "이유: " + d.reason, "wrapup-muted"));
    if (d.status === "failed" && d.error) { const e = node("p", d.error, "wrapup-error"); e.setAttribute("role", "alert"); el.append(e); }
    el.append(editor(d, f));
    const actions = node("div", null, "wrapup-actions");
    const b = (label, fn, cls) => { const x = node("button", label, "wf-btn" + (cls ? " " + cls : "")); x.type = "button"; x.disabled = busy; x.onclick = fn; return x; };
    actions.append(
      b("확정", () => act(d, () => confirm(d), "확정했습니다"), "wrapup-primary"),
      b("저장", () => act(d, () => save(d), "저장했습니다")),
      b("삭제", () => act(d, () => request(`${base}/${encodeURIComponent(d.id)}/discard`, "POST", {revision: d.revision}), "삭제했습니다")),
    );
    el.append(actions);
    if (notes[d.id]) el.append(node("p", notes[d.id], "wrapup-note"));
    return el;
  }

  function settledRow(d) {
    const li = node("li", null, "wrapup-settled-row");
    li.append(node("span", WRAPUP_STATUS[d.status] || d.status, "wrapup-chip"), node("span", wrapupSummary(d)));
    if (d.applied) li.append(node("span", "→ " + (d.applied.path ? d.applied.path.join(" / ") : (d.applied.id || "")), "wrapup-muted"));
    return li;
  }

  function render() {
    const el = root(); if (!el) return;
    el.replaceChildren();
    const shell = node("div", null, "wrapup-pane");
    const head = node("div", null, "wrapup-pane-head");
    head.append(node("strong", "wrap-up"), node("span", session, "wrapup-muted"));
    shell.append(head);
    if (error) { const a = node("p", error, "wrapup-error"); a.setAttribute("role", "alert"); shell.append(a); }
    if (!data) { shell.append(node("p", error ? "" : "loading…", "wf-note")); el.append(shell); return; }
    if (data.alive === false && data.unresolved) {
      shell.append(node("p", `이 세션은 종료되었습니다. 남은 임시본 ${data.unresolved}건을 확정하거나 삭제하세요.`, "wrapup-warn"));
    }
    const tabs = node("div", null, "wrapup-tabs");
    tabs.setAttribute("role", "tablist");
    for (const k of WRAPUP_KINDS) {
      const n = (data.counts || {})[k.id] || 0;
      const t = node("button", n ? `${k.label} (${n})` : k.label, "wrapup-tab");
      t.type = "button"; t.setAttribute("role", "tab"); t.setAttribute("aria-selected", String(tab === k.id));
      t.onclick = () => { tab = k.id; render(); };
      tabs.append(t);
    }
    shell.append(tabs);
    const mine = (data.drafts || []).filter(d => d.kind === tab);
    const open = mine.filter(d => ["draft", "failed", "applying"].includes(d.status));
    const done = mine.filter(d => !open.includes(d));
    if (!open.length) shell.append(node("p", "확정을 기다리는 임시본이 없습니다.", "wrapup-empty"));
    else {
      const all = node("button", `이 탭 모두 확정 (${open.length})`, "wf-btn wrapup-primary");
      all.type = "button"; all.disabled = busy; all.onclick = confirmAll;
      shell.append(all);
      for (const d of open) shell.append(card(d));
    }
    if (done.length) {
      const det = node("details", null, "wrapup-settled");
      det.append(node("summary", `처리된 항목 ${done.length}개`));
      const ul = node("ul");
      for (const d of done) ul.append(settledRow(d));
      det.append(ul);
      shell.append(det);
    }
    el.append(shell);
  }

  return {open: load, stop: () => { ++seq; clearTimeout(timer); timer = null; }, _state: () => ({data, tab, forms})};
}

async function wrapupRequest(path, method = "GET", body) {
  const response = await api(path, {method, ...(body ? {headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)} : {})});
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw Object.assign(Error(result.error || `HTTP ${response.status}`), {status: response.status, body: result});
  return result;
}

globalThis.WrapupPane = (() => {
  let view = null, el = null, host = null, name = "";
  function mount(target, session, opts = {}) {
    if (view && host === target && name === session && target.contains(el)) return;
    unmount();
    el = document.createElement("div"); el.className = "wrapup-host";
    target.replaceChildren(el); name = session; host = target;
    view = makeWrapupPane({root: () => el, session, request: opts.request || wrapupRequest});
    view.open();
  }
  function unmount() { if (view) view.stop(); view = null; if (el) el.remove(); el = host = null; name = ""; }
  return {mount, unmount, _view: () => view};
})();
