/* Sub procedures of a cflow v2 run (docs/cflow-v2.md 2.12.1-2.12.3,
   claunch-8t5r0).

   Three surfaces read the same data:

   - SubprocUi.card(li, row): a line under the session card's v2 line while
     the session's run is inside a sub procedure call -- the procedure, fork
     or inline, and a fork that fell back to inline (claunch-8t5r0.3).
   - SubprocUi.header(): the same fact as a chip in #term-header for the
     attached session. A fallback is amber and carries its cause.
     Both read `subprocs` of the session's /api/cflow2/rail row
     (daemon/cflow2_web.rail_subprocs); the notification of a fallback is
     the daemon's own (spawn_events, kind fallback).
   - SubprocPane: the split pane behind the header's ⤷ sub button. It lists
     every procedure the run could call (GET /api/cflow2/procedures), the
     ones that cannot run now disabled with the reason as their tooltip,
     an "available only" filter, a call as the user (POST /api/cflow2/call),
     and the procedure packs with their role filter (GET/POST
     /api/cflow2/packs*, claunch-8t5r0.4/.5).

   All text from the daemon goes in through textContent. */

function subprocNode(tag, text, cls) {
  const n = document.createElement(tag);
  if (text != null) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}

function subprocRailRow(name) {
  const cache = typeof cflow2RailCache === "object" && cflow2RailCache ? cflow2RailCache : {};
  return name ? cache[name] || null : null;
}

/* One open call as a short phrase and a tooltip. `fallback` is the fork that
   could not happen: the call runs inline and the user is told why. */
function subprocDescribe(s) {
  const fell = !!s.fallback;
  const mode = fell ? "fork→inline" : s.mode === "fork"
    ? ((s.fork || {}).status === "queued" ? "fork (starting)" : "fork") : "inline";
  const lines = [
    `sub procedure ${s.procedure} (call ${s.id}${s.by === "user" ? ", called by you" : ""})`,
    `from task ${s.label || "?"}`,
    `mode: ${mode}`,
  ];
  if ((s.fork || {}).session) lines.push(`fork session: ${s.fork.session}`);
  if (fell) lines.push(`fork failed, running inline: ${s.fallback.cause || "cause unknown"}`);
  return { mode, fell, title: lines.join("\n") };
}

function subprocSummary(subs) {
  const first = subs[0];
  const d = subprocDescribe(first);
  const more = subs.length > 1 ? ` (+${subs.length - 1})` : "";
  return { text: `⤷ sub ${first.procedure} · ${d.mode}${more}`, fell: subs.some((s) => !!s.fallback),
    title: subs.map((s) => subprocDescribe(s).title).join("\n\n") };
}

globalThis.SubprocUi = (() => {
  function card(li, row) {
    const subs = (row && row.subprocs) || [];
    const old = li.querySelector(".sess-subproc");
    if (!subs.length) { if (old) old.remove(); return; }
    const sum = subprocSummary(subs);
    const line = old || subprocNode("div", null, "sess-subproc");
    const sig = JSON.stringify([sum.text, sum.fell, sum.title]);
    if (line.dataset.sig !== sig) {
      line.dataset.sig = sig;
      line.textContent = sum.text;
      line.title = sum.title;
      line.classList.toggle("fallback", sum.fell);
    }
    // Below the run's own line, which the caller may create after this.
    const place = () => {
      const v2 = li.querySelector(".sess-cflow2");
      if (v2 && v2.nextSibling !== line) v2.after(line);
      else if (!v2 && line.parentNode !== li) li.appendChild(line);
    };
    place();
    if (typeof queueMicrotask === "function") queueMicrotask(place);
  }

  let bound = false;
  function header() {
    const chip = typeof document !== "undefined" ? document.getElementById("term-subproc") : null;
    if (!chip) return;
    if (!bound) {
      bound = true;
      const open = () => { if (typeof toggleSplitPane === "function") toggleSplitPane("subprocs"); };
      chip.addEventListener("click", open);
      chip.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
      });
    }
    const name = typeof currentName !== "undefined" ? currentName : null;
    const row = subprocRailRow(name);
    const subs = (row && row.subprocs) || [];
    const sum = subs.length ? subprocSummary(subs) : null;
    const sig = sum ? JSON.stringify([name, sum.text, sum.fell, sum.title]) : "hidden";
    if ((chip.dataset.sig || "hidden") === sig) return;
    chip.dataset.sig = sig;
    chip.classList.toggle("hidden", !sum);
    chip.classList.toggle("fallback", !!(sum && sum.fell));
    chip.textContent = sum ? sum.text.replace(/^⤷ sub /, "⤷ ") + (sum.fell ? " ⚠" : "") : "";
    chip.title = sum ? `${sum.title}\n\nclick: the sub procedure pane` : "";
  }

  return { card, header };
})();

async function subprocRequest(path, method = "GET", body) {
  const response = await api(path, {
    method,
    ...(body ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(result.error || `HTTP ${response.status}`);
  return result;
}

const SUBPROC_FILTER_KEY = "claunch.subprocs.availableOnly";

function subprocFilterGet() {
  try { return localStorage.getItem(SUBPROC_FILTER_KEY) === "1"; } catch { return false; }
}

function subprocFilterSet(on) {
  try { localStorage.setItem(SUBPROC_FILTER_KEY, on ? "1" : "0"); } catch { /* private window */ }
}

/* A starting `with` object for a call: every parameter with its default,
   a required one empty for the person to fill. */
function subprocDefaults(params) {
  const out = {};
  for (const [k, d] of Object.entries(params || {})) {
    const spec = d || {};
    if (spec.type === "block") continue;
    out[k] = "default" in spec ? spec.default : (spec.required ? "" : null);
  }
  return out;
}

function makeSubprocPane({ root, session }) {
  const node = subprocNode;
  let seq = 0;
  let availableOnly = subprocFilterGet();
  let open = null;    // the procedure whose call form is unfolded
  let note = "";

  async function load() {
    const my = ++seq;
    const el = root();
    if (!el) return;
    const row = subprocRailRow(session);
    if (!row) {
      el.replaceChildren(node("p", "This session has no cflow v2 run. A sub procedure is " +
        "called from a task of a v2 run.", "wf-note"));
      return;
    }
    let procs, packs;
    try {
      const q = `cwd=${encodeURIComponent(row.cwd)}&scope=${encodeURIComponent(row.scope)}`;
      [procs, packs] = await Promise.all([
        subprocRequest(`/api/cflow2/procedures?${q}`),
        subprocRequest(`/api/cflow2/packs?cwd=${encodeURIComponent(row.cwd)}`),
      ]);
    } catch (err) {
      if (my !== seq) return;
      el.replaceChildren(node("p", `could not read the sub procedures: ${err.message}`, "wf-note"));
      return;
    }
    if (my !== seq || !root()) return;
    draw(row, procs, packs);
  }

  function draw(row, procs, packs) {
    const el = root();
    const shell = node("div", null, "subproc-pane");
    const head = node("div", null, "subproc-head");
    head.append(node("span", procs.task_label ? `task ${procs.task_label}` : "no active task",
      "subproc-task"));
    head.append(node("span", `roles: ${(procs.roles || []).join(", ") || "unknown"}`,
      "subproc-muted"));
    if (procs.fork_calls) head.append(node("span", "this task forks forkable callees", "subproc-muted"));
    const filter = node("label", null, "subproc-filter");
    const box = node("input");
    box.type = "checkbox";
    box.checked = availableOnly;
    box.addEventListener("change", () => {
      availableOnly = box.checked;
      subprocFilterSet(availableOnly);
      draw(row, procs, packs);
    });
    filter.append(box, document.createTextNode(" available only"));
    head.append(filter);
    const reload = node("button", "↻", "subproc-btn");
    reload.type = "button";
    reload.title = "read the list again";
    reload.addEventListener("click", () => { note = ""; load(); });
    head.append(reload);
    shell.append(head);
    if (procs.refusal) shell.append(node("p", `No call can start now: ${procs.refusal}`, "subproc-refusal"));
    if (note) shell.append(node("p", note, "subproc-note"));

    const list = node("ul", null, "subproc-list");
    const all = procs.procedures || [];
    const shown = availableOnly ? all.filter((p) => p.available) : all;
    for (const p of shown) list.append(item(row, procs, p));
    if (!shown.length) {
      list.append(node("li", availableOnly && all.length
        ? `none of the ${all.length} sub procedures can run now (clear the filter to see why)`
        : "no sub procedure is declared or installed", "subproc-muted"));
    }
    shell.append(node("h4", `Sub procedures (${all.filter((p) => p.available).length}/${all.length} available)`),
      list);
    shell.append(packSection(row, packs));
    el.replaceChildren(shell);
  }

  function item(row, procs, p) {
    const li = node("li", null, "subproc-item" + (p.available ? "" : " disabled"));
    li.title = p.available ? (p.description || p.name) : p.why;
    const top = node("div", null, "subproc-row");
    top.append(node("span", p.name, "subproc-name"));
    const tags = [p.source === "calls" ? "declared" : "pack", p.mode];
    if (p.forkable) tags.push("forkable");
    if (p.pack_roles) tags.push(`${p.pack_roles.type} ${p.pack_roles.roles.join(",")}`);
    if (p.filter_roles) tags.push(`own filter ${p.filter_roles.type}`);
    for (const t of tags) top.append(node("span", t, `subproc-tag subproc-tag-${String(t).split(" ")[0]}`));
    const run = node("button", open === p.name ? "cancel" : "call…", "subproc-btn");
    run.type = "button";
    run.disabled = !p.available;
    run.title = p.available ? `call ${p.name} on task ${procs.task_label}` : p.why;
    run.addEventListener("click", () => {
      open = open === p.name ? null : p.name;
      draw(row, procs, lastPacks);
    });
    top.append(run);
    li.append(top);
    if (p.description) li.append(node("div", p.description, "subproc-desc"));
    if (!p.available) li.append(node("div", `why not: ${p.why}`, "subproc-why"));
    if (open === p.name && p.available) li.append(callForm(row, procs, p));
    return li;
  }

  function callForm(row, procs, p) {
    const form = node("div", null, "subproc-form");
    const params = node("textarea", null, "subproc-params");
    params.rows = Math.min(10, 2 + Object.keys(p.params || {}).length);
    params.value = JSON.stringify(subprocDefaults(p.params), null, 2);
    params.title = Object.entries(p.params || {}).map(([k, d]) =>
      `${k}: ${(d || {}).type || "any"}${(d || {}).required ? " (required)" : ""}` +
      `${(d || {}).description ? " -- " + d.description : ""}`).join("\n") || "no parameters";
    const mode = node("select", null, "subproc-mode");
    for (const [v, label] of [["auto", p.mode === "fork" ? "auto (fork)" : "auto (inline)"],
      ["inline", "inline"]]) {
      const o = node("option", label);
      o.value = v;
      mode.append(o);
    }
    mode.value = "auto";
    const go = node("button", "call", "subproc-btn approve");
    go.type = "button";
    const err = node("span", "", "subproc-error");
    go.addEventListener("click", async () => {
      let w;
      try {
        w = JSON.parse(params.value || "{}");
        if (w === null || typeof w !== "object" || Array.isArray(w)) throw Error("an object");
      } catch (e) {
        err.textContent = `parameters must be a JSON object (${e.message})`;
        return;
      }
      for (const k of Object.keys(w)) if (w[k] === null) delete w[k];
      go.disabled = true;
      try {
        const out = await subprocRequest("/api/cflow2/call", "POST", {
          cwd: row.cwd, scope: row.scope, procedure: p.name, node: procs.task, with: w,
          mode: mode.value,
        });
        const c = out.called || {};
        note = `called ${p.name} as ${c.call} (${c.mode}); the session was told.`;
        open = null;
        if (typeof refreshCflow2Rail === "function") refreshCflow2Rail();
        load();
      } catch (e) {
        err.textContent = e.message;
        go.disabled = false;
      }
    });
    form.append(node("label", "parameters (JSON)", "subproc-muted"), params, mode, go, err);
    return form;
  }

  let lastPacks = null;
  function packSection(row, packs) {
    lastPacks = packs;
    const sec = node("section", null, "subproc-packs");
    sec.append(node("h4", "Procedure packs"));
    const list = node("ul", null, "subproc-list");
    for (const pk of packs.packs || []) list.append(packItem(row, pk));
    if (!(packs.packs || []).length) list.append(node("li", "no pack in the catalog or this workspace", "subproc-muted"));
    sec.append(list);
    return sec;
  }

  async function packOp(path, body) {
    try {
      await subprocRequest(path, "POST", body);
      note = "";
    } catch (e) {
      note = e.message;
    }
    load();
  }

  function packItem(row, pk) {
    const li = node("li", null, "subproc-item");
    const top = node("div", null, "subproc-row");
    top.append(node("span", pk.id, "subproc-name"));
    top.append(node("span", pk.installed ? `installed (${pk.layer})` : "catalog", "subproc-tag"));
    if (pk.error) top.append(node("span", pk.error, "subproc-why"));
    if (!pk.installed) {
      const b = node("button", "install", "subproc-btn");
      b.type = "button";
      b.title = "copy this pack into the workspace's .claunch/workflows/v2/packs";
      b.addEventListener("click", () => packOp("/api/cflow2/packs/install", { cwd: row.cwd, pack: pk.id }));
      top.append(b);
    } else if (pk.layer === "project") {
      const b = node("button", "remove", "subproc-btn");
      b.type = "button";
      b.title = "remove this pack from the workspace (the catalog copy stays)";
      b.addEventListener("click", () => {
        if (confirm(`Remove pack ${pk.id} from ${row.cwd}?`)) {
          packOp("/api/cflow2/packs/remove", { cwd: row.cwd, pack: pk.id });
        }
      });
      top.append(b);
    }
    li.append(top);
    if (pk.title || pk.description) li.append(node("div", [pk.title, pk.description].filter(Boolean).join(" — "), "subproc-desc"));
    li.append(node("div", `procedures: ${(pk.procedures || []).join(", ") || "none"}`, "subproc-muted"));
    li.append(rolesEditor(row, pk));
    return li;
  }

  function rolesEditor(row, pk) {
    const wrap = node("div", null, "subproc-roles");
    const kind = node("select");
    for (const [v, label] of [["", "every role"], ["whitelist", "only these roles"],
      ["blacklist", "every role but these"]]) {
      const o = node("option", label);
      o.value = v;
      kind.append(o);
    }
    kind.value = (pk.roles && pk.roles.type) || "";
    const roles = node("input");
    roles.type = "text";
    roles.placeholder = "roles, comma separated (e.g. unreal)";
    roles.value = ((pk.roles && pk.roles.roles) || []).join(", ");
    const save = node("button", "save roles", "subproc-btn");
    save.type = "button";
    const editable = pk.installed && pk.layer === "project";
    kind.disabled = roles.disabled = save.disabled = !editable;
    save.title = editable ? "write the role filter into this workspace's copy of the pack"
      : pk.installed ? `this pack is installed in the ${pk.layer} layer; install it into the workspace to edit its filter here`
        : "install the pack into the workspace first";
    wrap.title = save.title;
    save.addEventListener("click", () => {
      const names = roles.value.split(",").map((r) => r.trim()).filter(Boolean);
      const spec = kind.value ? { type: kind.value, roles: names } : null;
      if (spec && !names.length) { note = "name at least one role, or choose every role"; load(); return; }
      packOp("/api/cflow2/packs/roles", { cwd: row.cwd, pack: pk.id, roles: spec });
    });
    wrap.append(node("span", "role filter:", "subproc-muted"), kind, roles, save);
    return wrap;
  }

  return { open: load, stop: () => { ++seq; } };
}

globalThis.SubprocPane = (() => {
  let view = null, el = null, host = null, name = "";
  function mount(target, session) {
    if (view && host === target && name === session && target.contains(el)) return;
    unmount();
    el = document.createElement("div");
    el.className = "subproc-host";
    target.replaceChildren(el);
    name = session;
    host = target;
    view = makeSubprocPane({ root: () => el, session });
    view.open();
  }
  function unmount() {
    if (view) view.stop();
    view = null;
    if (el) el.remove();
    el = host = null;
    name = "";
  }
  return { mount, unmount };
})();
