/* Category trees (claunch-p2v4m): the workspace keyword index of sessions and
   issues, at #/category, and a session's place in it in the terminal's split
   pane (app.js openSplit "category"). All user text uses textContent.

   One factory, two mounts -- the page (CategoryPage) edits the tree:
   select items (and categories) and extract them into a new child, merge
   them into a new category under their lowest common ancestor, move,
   rename, delete, and review the agents' proposals. The pane (CategoryPane)
   shows where its session sits and starts "assign to tree". */

function categoryNode(tag, text, cls) {
  const n = document.createElement(tag);
  if (text != null) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}

async function categoryRequest(path, method = "GET", body) {
  const response = await api(path, {method, ...(body ? {headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)} : {})});
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw Object.assign(Error(result.error || `HTTP ${response.status}`), {status: response.status, body: result});
  return result;
}

const categoryKey = (item) => item.kind + ":" + item.ref;
const categoryAliases = (text) => String(text || "").split(/[\n,]/).map(s => s.trim()).filter(Boolean);

/* Ancestors of a node id, nearest first, ending with null (the root). */
function categoryAncestors(byId, id) {
  const out = [];
  const seen = new Set();
  while (id != null && byId.has(id) && !seen.has(id)) { seen.add(id); out.push(id); id = byId.get(id).parent_id; }
  out.push(null);
  return out;
}

/* The deepest node that is an ancestor-or-self of every anchor (null = root),
   the same rule category.merge applies on the server. */
function categoryLca(byId, anchors) {
  if (!anchors.length) return null;
  const chains = anchors.map(a => categoryAncestors(byId, a));
  const common = new Set(chains[0]);
  for (const chain of chains.slice(1)) for (const n of [...common]) if (!chain.includes(n)) common.delete(n);
  return chains[0].find(n => common.has(n)) ?? null;
}

function makeCategoryView({root, workspace: fixed = ""}) {
  const node = categoryNode;
  let workspace = fixed, workspaces = [], data = null, events = [];
  let error = "", notice = "", busy = false, tab = "tree", seq = 0;
  let picked = new Set(), pickedNodes = new Set(), collapsed = new Set();
  // Finished sessions are most of a long-lived tree and are not what the
  // index is read for, so the page hides them until asked: one box per
  // partition (daemon/session.py session_category).
  let showKilled = false, showArchived = false;
  let form = null;   // {kind, ...}: the one inline form open, if any
  let proposalDrafts = {};
  const url = (path, params = {}) => path + (path.includes("?") ? "&" : "?") + new URLSearchParams({workspace, ...params});

  function byId() { return new Map((data?.nodes || []).map(n => [n.id, n])); }
  function itemNode() { return new Map((data?.items || []).map(i => [categoryKey(i), i.node_id])); }
  function pathOf(id) { const n = byId().get(id); return n ? n.path.join(" / ") : "(top level)"; }

  async function load() {
    const mine = ++seq;
    try {
      const [tree, history] = await Promise.all([
        categoryRequest(url("/api/category")),
        tab === "history" ? categoryRequest(url("/api/category/events")) : Promise.resolve(null),
      ]);
      if (mine !== seq) return;
      data = tree; if (history) events = history.events; error = "";
      const keys = new Set([...data.items, ...(data.unassigned || [])].map(categoryKey));
      picked = new Set([...picked].filter(k => keys.has(k)));
      const ids = new Set(data.nodes.map(n => n.id));
      pickedNodes = new Set([...pickedNodes].filter(id => ids.has(id)));
    } catch (exc) { if (mine === seq) error = exc.message; }
    if (mine === seq) render();
  }

  async function act(action, done = "") {
    if (busy) return;
    busy = true; error = ""; notice = ""; render();
    try { await action(); form = null; notice = done; picked.clear(); pickedNodes.clear(); await load(); }
    catch (exc) { error = exc.message; }
    finally { busy = false; render(); }
  }

  function button(text, action, cls = "", disabled = false) {
    const b = node("button", text, "wf-btn " + cls); b.type = "button";
    b.disabled = busy || disabled; b.onclick = action; return b;
  }

  /* Where a new category for the current selection goes: extract wants every
     picked item inside one category (its lowest common one); merge also takes
     whole categories and loose sessions, and may land at the top level. */
  function selection() {
    const nodes = byId(), at = itemNode();
    const items = [...picked].map(k => { const i = k.indexOf(":"); return {kind: k.slice(0, i), ref: k.slice(i + 1)}; });
    const anchors = items.map(i => at.get(categoryKey(i)) ?? null);
    const extractAt = items.length && !pickedNodes.size && anchors.every(a => a != null)
      ? categoryLca(nodes, anchors) : null;
    const mergeAnchors = anchors.concat([...pickedNodes].map(id => nodes.get(id)?.parent_id ?? null));
    return {items, nodes: [...pickedNodes], extractAt, mergeAt: categoryLca(nodes, mergeAnchors),
            canMerge: items.length + pickedNodes.size >= 2};
  }

  function newCategoryForm(title, where, submit) {
    const f = node("form", null, "category-form");
    f.append(node("h4", title), node("p", "새 카테고리 위치: " + where, "category-muted"));
    const label = node("input"); label.placeholder = "키워드 (예: restart gate)"; label.required = true;
    label.maxLength = 120; label.setAttribute("aria-label", "카테고리 이름"); label.value = form.label || "";
    label.oninput = () => { form.label = label.value; };
    const aliases = node("input"); aliases.placeholder = "별칭 · 쉼표로 구분 (선택)"; aliases.value = form.aliases || "";
    aliases.setAttribute("aria-label", "별칭"); aliases.oninput = () => { form.aliases = aliases.value; };
    const ok = node("button", "만들기", "wf-btn category-primary"); ok.disabled = busy;
    f.append(label, aliases, ok, button("취소", () => { form = null; render(); }));
    f.onsubmit = (e) => { e.preventDefault(); submit({label: label.value, aliases: categoryAliases(aliases.value)}); };
    setTimeout(() => label.focus?.(), 0);
    return f;
  }

  function nodePicker(current, exclude, onPick) {
    const select = node("select"); select.setAttribute("aria-label", "카테고리 선택");
    const top = node("option", "(top level)"); top.value = ""; select.append(top);
    for (const n of [...(data?.nodes || [])].sort((a, b) => a.path.join("/").localeCompare(b.path.join("/")))) {
      if (exclude && categoryAncestors(byId(), n.id).includes(exclude)) continue;
      const o = node("option", n.path.join(" / ")); o.value = n.id; select.append(o);
    }
    select.value = current || "";
    select.onchange = () => onPick(select.value || null);
    return select;
  }

  function toolbar() {
    const bar = node("div", null, "category-toolbar");
    const sel = selection();
    const count = sel.items.length + sel.nodes.length;
    bar.append(node("span", count ? `선택: 항목 ${sel.items.length}개 · 카테고리 ${sel.nodes.length}개` : "항목이나 카테고리를 선택하세요", "category-muted"));
    bar.append(
      button("Extract…", () => { form = {kind: "extract", at: sel.extractAt}; render(); }, "", !sel.extractAt),
      button("Split…", () => { form = {kind: "split", at: sel.extractAt, lines: ""}; render(); }, "", !sel.extractAt || sel.items.length < 2),
      button("Merge…", () => { form = {kind: "merge", at: sel.mergeAt}; render(); }, "", !sel.canMerge),
      button("Move to…", () => { form = {kind: "move", target: ""}; render(); }, "", !sel.items.length || sel.nodes.length > 0),
      button("Remove from tree", () => act(() => categoryRequest(url("/api/category/unassign"), "POST", {items: sel.items}), "트리에서 뺐습니다"), "", !sel.items.length || sel.nodes.length > 0),
      button("New category…", () => { form = {kind: "child", parent: null}; render(); }),
      button("선택 해제", () => { picked.clear(); pickedNodes.clear(); render(); }, "", !count),
    );
    const wrap = node("div", null, "category-toolbar-wrap"); wrap.append(bar);
    if (form?.kind === "extract") {
      wrap.append(newCategoryForm("Extract: 선택한 항목을 새 하위 카테고리로", pathOf(form.at), (v) => act(() =>
        categoryRequest(url("/api/category/extract"), "POST", {node_id: form.at, items: sel.items, ...v}), "새 하위 카테고리를 만들었습니다")));
    } else if (form?.kind === "split") {
      wrap.append(splitForm(sel));
    } else if (form?.kind === "merge") {
      wrap.append(newCategoryForm("Merge: 선택한 항목·카테고리를 공통 부모 아래 하나로", pathOf(form.at), (v) => act(() =>
        categoryRequest(url("/api/category/merge"), "POST", {items: sel.items, nodes: sel.nodes, ...v}), "합쳤습니다")));
    } else if (form?.kind === "child") {
      wrap.append(newCategoryForm("새 카테고리", pathOf(form.parent), (v) => act(() =>
        categoryRequest(url("/api/category/nodes"), "POST", {parent_id: form.parent, ...v}), "카테고리를 만들었습니다")));
    } else if (form?.kind === "move") {
      const f = node("div", null, "category-form");
      f.append(node("h4", "선택한 항목을 옮길 카테고리"));
      const picker = nodePicker(form.target, null, (id) => { form.target = id || ""; });
      picker.querySelector("option").remove();
      if (!form.target) form.target = picker.value;
      picker.value = form.target;
      f.append(picker, button("옮기기", () => act(() => categoryRequest(url("/api/category/assign"), "POST",
        {node_id: form.target, items: sel.items}), "옮겼습니다"), "category-primary", !data?.nodes?.length),
        button("취소", () => { form = null; render(); }));
      wrap.append(f);
    }
    return wrap;
  }

  /* Split: several new children in one write, and each picked item goes to the
     part whose name is on its line. A line without a ref list takes whatever
     the earlier lines left -- the server applies the same rule
     (category_assign._split). */
  function splitForm(sel) {
    const f = node("form", null, "category-form");
    f.append(node("h4", "Split: 선택한 항목을 여러 하위 카테고리로 한 번에"),
      node("p", "새 카테고리 위치: " + pathOf(form.at), "category-muted"),
      node("p", "한 줄에 하나씩 `이름: 항목, 항목`. 항목을 안 적은 줄이 나머지를 가져갑니다. 항목은 " +
        sel.items.map(i => i.ref).join(", ") + " 가운데에서 고릅니다.", "category-muted"));
    const text = node("textarea"); text.rows = Math.max(3, sel.items.length);
    // Left empty on purpose: the person writes the lines, and each line's label
    // is theirs to choose. The example shows the shape -- a labelled line, then
    // a line that takes what is left -- with the refs in the `kind:ref` spelling
    // the route accepts. Prefilling the refs alone would read as one part
    // labelled "session", which the server refuses.
    text.value = form.lines || "";
    text.setAttribute("placeholder", "새 이름: " + sel.items.map(categoryKey).join(", ")
      + "\n나머지");
    text.setAttribute("aria-label", "분할 줄");
    text.oninput = () => { form.lines = text.value; };
    const err = node("p", "", "category-error");
    const ok = node("button", "분할", "wf-btn category-primary"); ok.disabled = busy;
    f.append(text, err, ok, button("취소", () => { form = null; render(); }));
    f.onsubmit = (e) => {
      e.preventDefault();
      const parts = splitParts(text.value, sel.items);
      if (!parts.length) { err.textContent = "`이름: 항목` 형태로 한 줄 이상 적으세요."; return; }
      // `items` is the selection the form told the person to choose from, sent
      // as {kind, ref} (the route's `_items` shape, not the part refs' strings).
      // Without it the route falls back to the whole parent subtree, which
      // would move items nobody picked out to the catch-all line.
      act(() => categoryRequest(url("/api/category/split"), "POST", {
        parent_id: form.at, items: sel.items.map(i => ({kind: i.kind, ref: i.ref})), parts}),
        "하위 카테고리를 만들었습니다");
    };
    setTimeout(() => text.focus?.(), 0);
    return f;
  }

  /* "이름: session:s1, issue:i-1" per line -> [{label, items: [{kind, ref}]}].
     A line with no refs sends the label alone: the server treats a part with
     no items as the catch-all. A ref that is not in this selection is passed
     through and the server refuses the whole call -- better than placing it
     somewhere the person did not mean. */
  function splitParts(text, items = []) {
    const known = new Map(items.map(i => [categoryKey(i), i]));
    const out = [];
    for (const line of String(text || "").split("\n")) {
      if (!line.trim()) continue;
      const at = line.indexOf(":");
      // No colon: the whole line is a label with no refs -- the catch-all.
      if (at < 0) { out.push({label: line.trim()}); continue; }
      const label = line.slice(0, at).trim();
      if (!label) continue;
      const refs = line.slice(at + 1).split(/[,\n]/).map(s => s.trim()).filter(Boolean);
      if (!refs.length) { out.push({label}); continue; }
      const placed = refs.map(ref => {
        const hit = known.get(ref) || known.get("session:" + ref) || known.get("issue:" + ref);
        return hit ? categoryKey(hit) : ref;
      });
      out.push({label, items: placed});
    }
    return out;
  }

  /* Sessions the page is hiding until their box is ticked. `life` is the
     daemon's partition; a session the daemon no longer has a record of has
     none and stays visible. */
  function hiddenSession(item) {
    if (item.kind !== "session") return false;
    const life = ((data?.details || {})[categoryKey(item)] || {}).life;
    return (life === "killed" && !showKilled) || (life === "archived" && !showArchived);
  }

  function itemRow(item) {
    const key = categoryKey(item);
    const row = node("li", null, "category-item");
    const box = node("input"); box.type = "checkbox"; box.checked = picked.has(key);
    box.setAttribute("aria-label", `${item.kind} ${item.ref} 선택`);
    box.onchange = () => { box.checked ? picked.add(key) : picked.delete(key); render(); };
    const info = (data?.details || {})[key] || {};
    const link = node("a", item.ref, "category-ref category-" + item.kind);
    link.href = item.kind === "session" ? "#/s/" + encodeURIComponent(item.ref) : "#/beads/" + encodeURIComponent(item.ref);
    row.append(box, node("span", item.kind === "session" ? "▶" : "◈", "category-kind"), link);
    if (item.kind === "session") {
      // running carries what the screen reports (busy/idle/starting); the
      // other partitions are the state itself.
      if (info.life) {
        const label = info.life === "running" && info.status && info.status !== "exited"
          ? "running · " + info.status : info.life;
        row.append(node("span", label, "category-chip category-status-" + info.life));
      } else if (info.status) {
        row.append(node("span", info.status, "category-chip category-status-" + info.status));
      }
      if (info.note) row.append(node("span", info.note, "category-muted"));
    } else {
      if (info.status) row.append(node("span", info.status, "category-chip"));
      if (info.title) row.append(node("span", info.title, "category-muted"));
    }
    return row;
  }

  function nodeBlock(n, children, itemsByNode) {
    const li = node("li", null, "category-node");
    const head = node("div", null, "category-node-head");
    const kids = children.get(n.id) || [], items = itemsByNode.get(n.id) || [];
    const open = !collapsed.has(n.id);
    const toggle = button(kids.length || items.length ? (open ? "▾" : "▸") : "·", () => {
      open ? collapsed.add(n.id) : collapsed.delete(n.id); render(); }, "category-toggle");
    toggle.setAttribute("aria-expanded", String(open));
    const box = node("input"); box.type = "checkbox"; box.checked = pickedNodes.has(n.id);
    box.setAttribute("aria-label", `카테고리 ${n.label} 선택`);
    box.onchange = () => { box.checked ? pickedNodes.add(n.id) : pickedNodes.delete(n.id); render(); };
    const total = countBelow(n.id, children, itemsByNode);
    head.append(toggle, box, node("span", n.label, "category-label"));
    if (n.aliases.length) head.append(node("span", n.aliases.join(", "), "category-aliases"));
    head.append(node("span", String(total), "category-count"));
    const tools = node("span", null, "category-node-tools");
    tools.append(
      button("+", () => { form = {kind: "child", parent: n.id}; render(); }, "category-mini"),
      button("✎", () => { form = {kind: "rename", id: n.id, label: n.label, aliases: n.aliases.join(", ")}; render(); }, "category-mini"),
      button("⇄", () => { form = {kind: "reparent", id: n.id, parent: n.parent_id}; render(); }, "category-mini"),
      button("✕", () => {
        if (globalThis.confirm(`“${n.label}” 카테고리를 삭제할까요? 안의 항목은 상위 카테고리로 옮겨집니다.`))
          act(() => categoryRequest(url("/api/category/nodes/" + n.id), "DELETE"), "삭제했습니다");
      }, "category-mini category-danger"),
    );
    tools.children[0].title = "하위 카테고리 추가"; tools.children[1].title = "이름·별칭 수정";
    tools.children[2].title = "다른 카테고리 아래로 이동"; tools.children[3].title = "삭제";
    head.append(tools);
    li.append(head);
    if (form?.kind === "rename" && form.id === n.id) li.append(renameForm(n));
    if (form?.kind === "reparent" && form.id === n.id) li.append(reparentForm(n));
    if (open) {
      const ul = node("ul", null, "category-children");
      for (const item of items) ul.append(itemRow(item));
      for (const child of kids) ul.append(nodeBlock(child, children, itemsByNode));
      if (ul.childElementCount) li.append(ul);
    }
    return li;
  }

  function countBelow(id, children, itemsByNode) {
    let total = (itemsByNode.get(id) || []).length;
    for (const c of children.get(id) || []) total += countBelow(c.id, children, itemsByNode);
    return total;
  }

  function renameForm(n) {
    const f = node("form", null, "category-form");
    const label = node("input"); label.value = form.label; label.required = true; label.setAttribute("aria-label", "카테고리 이름");
    label.oninput = () => { form.label = label.value; };
    const aliases = node("input"); aliases.value = form.aliases; aliases.setAttribute("aria-label", "별칭");
    aliases.oninput = () => { form.aliases = aliases.value; };
    const ok = node("button", "저장", "wf-btn category-primary"); ok.disabled = busy;
    f.append(label, aliases, ok, button("취소", () => { form = null; render(); }));
    f.onsubmit = (e) => { e.preventDefault(); act(() => categoryRequest(url("/api/category/nodes/" + n.id), "PUT",
      {label: label.value, aliases: categoryAliases(aliases.value)}), "수정했습니다"); };
    return f;
  }

  function reparentForm(n) {
    const f = node("div", null, "category-form");
    f.append(node("span", "새 상위 카테고리", "category-muted"),
      nodePicker(n.parent_id, n.id, (id) => { form.parent = id; }),
      button("이동", () => act(() => categoryRequest(url("/api/category/nodes/" + n.id), "PUT",
        {parent_id: form.parent}), "이동했습니다"), "category-primary"),
      button("취소", () => { form = null; render(); }));
    return f;
  }

  /* "show killed" / "show archived": one box each, both off by default. */
  function filters() {
    const bar = node("div", null, "category-filters");
    const one = (key, label, on, set) => {
      const box = node("input"); box.type = "checkbox"; box.checked = on;
      box.id = "category-show-" + key;
      box.onchange = () => { set(box.checked); render(); };
      const wrap = node("label", null, "category-filter");
      wrap.setAttribute("for", box.id);
      wrap.append(box, node("span", label));
      bar.append(wrap);
    };
    one("killed", "show killed", showKilled, (v) => { showKilled = v; });
    one("archived", "show archived", showArchived, (v) => { showArchived = v; });
    return bar;
  }

  function treeView() {
    const box = node("section", null, "category-tree");
    const children = new Map(), itemsByNode = new Map();
    const nodes = data?.nodes || [];
    const ids = new Set(nodes.map(n => n.id));
    for (const n of nodes) {
      const parent = ids.has(n.parent_id) ? n.parent_id : null;
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(n);
    }
    for (const i of data?.items || []) {
      if (hiddenSession(i)) continue;
      if (!itemsByNode.has(i.node_id)) itemsByNode.set(i.node_id, []);
      itemsByNode.get(i.node_id).push(i);
    }
    box.append(toolbar(), filters());
    const top = children.get(null) || [];
    if (!top.length) box.append(node("p", "아직 카테고리가 없습니다. ‘New category…’로 만들거나, 세션 패널의 ‘Assign to tree’로 에이전트에게 배정을 요청하세요.", "category-empty"));
    const ul = node("ul", null, "category-roots");
    for (const n of top) ul.append(nodeBlock(n, children, itemsByNode));
    box.append(ul);
    const loose = (data?.unassigned || []).filter(i => !hiddenSession(i));
    if (loose.length) {
      const group = node("details", null, "category-loose");
      group.open = loose.some(i => picked.has(categoryKey(i)));
      group.append(node("summary", `트리에 없는 세션 ${loose.length}개`));
      const list = node("ul", null, "category-children");
      for (const item of loose) list.append(itemRow(item));
      group.append(list); box.append(group);
    }
    return box;
  }

  function proposalsView() {
    const box = node("section", null, "category-proposals");
    const list = data?.proposals || [];
    if (!list.length) { box.append(node("p", "검토할 제안이 없습니다. 에이전트가 새 카테고리를 제안하면 여기에 표시됩니다.", "category-empty")); return box; }
    for (const p of list) {
      const card = node("article", null, "category-card");
      const draft = proposalDrafts[p.id] ||= {label: p.label, aliases: p.aliases.join(", ")};
      card.append(node("h4", p.label), node("p", "위치: " + pathOf(p.parent_id) + " 아래", "category-muted"));
      if (p.aliases.length) card.append(node("p", "별칭: " + p.aliases.join(", "), "category-muted"));
      if (p.description) card.append(node("p", p.description));
      card.append(node("p", "이유: " + p.reason), node("p", `제안: ${p.proposed_by || "agent"} · ${String(p.created_at).slice(0, 16).replace("T", " ")}`, "category-muted"));
      const items = node("ul", null, "category-children");
      for (const i of p.items) {
        const li = node("li", null, "category-item");
        li.append(node("span", i.kind === "session" ? "▶" : "◈", "category-kind"), node("span", i.ref, "category-ref"));
        items.append(li);
      }
      card.append(items);
      const label = node("input"); label.value = draft.label; label.setAttribute("aria-label", "수락할 이름");
      label.oninput = () => { draft.label = label.value; };
      const aliases = node("input"); aliases.value = draft.aliases; aliases.setAttribute("aria-label", "수락할 별칭");
      aliases.oninput = () => { draft.aliases = aliases.value; };
      const actions = node("div", null, "category-actions");
      actions.append(label, aliases,
        button("수락", () => act(() => categoryRequest(url(`/api/category/proposals/${p.id}/review`), "POST",
          {decision: "accept", label: draft.label, aliases: categoryAliases(draft.aliases)}), "카테고리를 만들었습니다"), "category-primary"),
        button("거절", () => act(() => categoryRequest(url(`/api/category/proposals/${p.id}/review`), "POST",
          {decision: "reject"}), "거절했습니다")));
      card.append(actions); box.append(card);
    }
    return box;
  }

  function historyView() {
    const box = node("section", null, "category-history");
    if (!events.length) { box.append(node("p", "기록이 없습니다.", "category-empty")); return box; }
    const ul = node("ul", null, "category-events");
    for (const e of events) {
      const li = node("li");
      const d = e.detail || {};
      const what = d.label || (d.items || []).map(i => Array.isArray(i) ? i[1] : i.ref).join(", ");
      li.append(node("span", String(e.at).slice(0, 19).replace("T", " "), "category-muted"),
        node("span", e.op, "category-chip"), node("span", e.actor || "", "category-muted"), node("span", what || ""));
      ul.append(li);
    }
    box.append(ul);
    return box;
  }

  function render() {
    const el = root(); if (!el) return;
    el.replaceChildren();
    const shell = node("div", null, "category-shell");
    const head = node("header", null, "category-head");
    const title = node("div");
    title.append(node("span", "CATEGORY", "category-eyebrow"), node("h2", "주제별 색인"),
      node("p", "세션과 이슈를 주제 키워드 트리로 정리합니다. 용어집(Glossary)과는 별개입니다.", "category-muted"));
    const scope = node("label", null, "category-scope"); scope.append(node("span", "Workspace"));
    const picker = node("select"); picker.setAttribute("aria-label", "Workspace"); picker.disabled = busy;
    for (const w of workspaces) { const o = node("option", w.name); o.value = w.name; picker.append(o); }
    picker.value = workspace;
    picker.onchange = () => { workspace = picker.value; data = null; picked.clear(); pickedNodes.clear(); form = null; render(); load(); };
    scope.append(picker); head.append(title, scope); shell.append(head);
    if (error) { const a = node("p", error, "category-error"); a.setAttribute("role", "alert"); shell.append(a); }
    if (notice) shell.append(node("p", notice, "category-notice"));
    if (!workspaces.length) { shell.append(node("p", "Workspace를 먼저 등록하세요 (Workspaces 페이지).", "category-empty")); el.append(shell); return; }
    const nav = node("nav", null, "category-tabs");
    const pending = (data?.proposals || []).length;
    for (const [key, label] of [["tree", "트리"], ["proposals", pending ? `제안 검토 (${pending})` : "제안 검토"], ["history", "기록"]]) {
      const b = button(label, () => { tab = key; form = null; render(); if (key === "history") load(); }, tab === key ? "is-active" : "");
      b.setAttribute("aria-pressed", String(tab === key)); nav.append(b);
    }
    nav.append(button("새로고침", load));
    shell.append(nav);
    if (!data) shell.append(node("p", "loading…", "wf-note"));
    else shell.append(tab === "proposals" ? proposalsView() : tab === "history" ? historyView() : treeView());
    el.append(shell);
  }

  async function open(ws = "") {
    if (ws) workspace = ws;
    const mine = ++seq;
    try {
      const result = await categoryRequest("/api/workspaces");
      if (mine !== seq) return;
      workspaces = result.workspaces || [];
      if (!workspaces.some(w => w.name === workspace)) workspace = workspaces[0]?.name || "";
      if (workspace) await load(); else render();
    } catch (exc) { if (mine === seq) { error = exc.message; render(); } }
  }

  return {open, stop: () => { ++seq; }, _state: () => ({workspace, tab, picked, pickedNodes, form, data})};
}

globalThis.CategoryPage = makeCategoryView({root: () => document.getElementById("category-view")});

/* The terminal's split pane: where this session (and its issue) sits, and the
   "Assign to tree" press. */
function makeCategoryPane({root, session}) {
  const node = categoryNode;
  let view = null, error = "", busy = false, seq = 0, timer = null;
  // "제안으로 등록": unchecked (default) creates a new category at once,
  // checked files it for a person to review on the Category page.
  let proposal = false;
  const base = "/api/sessions/" + encodeURIComponent(session) + "/category";

  async function load() {
    const mine = ++seq;
    try {
      const out = await categoryRequest(base);
      if (mine !== seq) return;
      view = out; error = "";
    } catch (exc) { if (mine === seq) error = exc.message; }
    if (mine === seq) { render(); schedule(); }
  }
  function running() { return ["running", "assigned"].includes(view?.job?.state); }
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(load, running() ? 3000 : 15000);
  }
  // A held session is woken by the daemon on its own; any other ended one
  // (killed, paused, ...) comes back only after the person says yes here.
  // Ending it again afterwards is the person's call.
  async function assign() {
    if (busy) return;
    busy = true; error = ""; render();
    try {
      try { await categoryRequest(base + "/assign", "POST", {proposal}); }
      catch (exc) {
        if (!exc.body?.needs_wake) throw exc;
        const life = exc.body.life || "killed";
        if (!globalThis.confirm(`${session}은(는) 종료된(${life}) 세션입니다. 깨워서 트리에 배정할까요?\n배정이 끝난 뒤 세션을 다시 종료하는 것은 직접 해야 합니다.`)) return;
        await categoryRequest(base + "/assign", "POST", {wake: true, proposal});
      }
    }
    catch (exc) { error = exc.message; }
    finally { busy = false; await load(); }
  }

  function render() {
    const el = root(); if (!el) return;
    el.replaceChildren();
    const shell = node("div", null, "category-pane");
    const head = node("div", null, "category-pane-head");
    head.append(node("strong", "category"), node("span", session, "category-muted"));
    if (view?.workspace) {
      const link = node("a", "트리 열기 ↗", "category-link");
      link.href = "#/category/" + encodeURIComponent(view.workspace); head.append(link);
    }
    shell.append(head);
    if (error) { const a = node("p", error, "category-error"); a.setAttribute("role", "alert"); shell.append(a); }
    if (!view) { shell.append(node("p", error ? "" : "loading…", "wf-note")); el.append(shell); return; }
    const list = node("ul", null, "category-places");
    for (const place of view.places || []) {
      const li = node("li");
      li.append(node("span", place.kind === "session" ? "▶ " + place.ref : "◈ " + place.ref, "category-ref"));
      li.append(node("span", place.node ? place.node.path.join(" / ") : "트리에 없음", place.node ? "category-path" : "category-muted"));
      for (const p of place.proposals || []) li.append(node("span", `제안 대기: ${p.label}`, "category-chip"));
      list.append(li);
    }
    shell.append(list);
    const job = view.job;
    const ended = view.life && view.life !== "running";
    if (ended) shell.append(node("p", view.life === "hold"
      ? "hold 상태의 세션입니다. 배정을 누르면 세션을 깨워서 진행합니다."
      : `종료된(${view.life}) 세션입니다. 배정을 누르면 세션을 다시 깨울지 먼저 묻습니다.`, "category-muted"));
    const how = view.mode === "fork"
      ? "이 세션을 quick-fork해서 배정하고, 결과를 이 세션에 merge합니다."
      : "이 하네스는 quick-fork를 지원하지 않아 이 세션에서 직접 배정합니다.";
    // An in-place request the session has not acted on can be sent again:
    // the daemon replaces that job and types the block once more.
    const resend = job?.mode === "inplace" && job.state === "running";
    const press = node("button", resend ? "다시 요청" : running() ? "배정 진행 중…" : "Assign to tree", "wf-btn category-primary");
    press.type = "button"; press.disabled = busy || (running() && !resend); press.onclick = assign;
    const opt = node("input"); opt.type = "checkbox"; opt.id = "category-proposal-opt";
    opt.checked = proposal; opt.onchange = () => { proposal = opt.checked; };
    const optLabel = node("label", null, "category-proposal-opt");
    optLabel.setAttribute("for", opt.id);
    optLabel.append(opt, node("span", "제안으로 등록 (새 카테고리를 사람이 검토)"));
    shell.append(press, optLabel, node("p", how, "category-muted"));
    if (resend) shell.append(node("p", "이 세션이 아직 배정하지 않았습니다. 요청을 무시했다면 다시 요청할 수 있습니다.", "category-muted"));
    if (job) {
      const labels = {running: "진행 중", assigned: "배정됨 · merge 대기", done: "완료", failed: "실패"};
      const line = node("p", null, "category-job category-job-" + job.state);
      line.append(node("span", labels[job.state] || job.state, "category-chip"),
        node("span", (job.fork ? job.fork + " · " : "") + String(job.started_at).slice(0, 16).replace("T", " "), "category-muted"));
      if (job.note) line.append(node("span", job.note));
      shell.append(line);
    }
    el.append(shell);
  }

  return {open: load, stop: () => { ++seq; clearTimeout(timer); timer = null; }};
}

globalThis.CategoryPane = (() => {
  let view = null, el = null, host = null, name = "";
  function mount(target, session) {
    if (view && host === target && name === session && target.contains(el)) return;
    unmount();
    el = document.createElement("div"); el.className = "category-host";
    target.replaceChildren(el); name = session; host = target;
    view = makeCategoryPane({root: () => el, session}); view.open();
  }
  function unmount() { if (view) view.stop(); view = null; if (el) el.remove(); el = host = null; name = ""; }
  return {mount, unmount};
})();
