/* The packs pane (claunch-6esft.1): the ↻ rebrief button opens it in the
   terminal's split pane (app.js openSplit "packs") instead of typing the
   briefing straight away. It shows the briefing the button used to send and
   this session's prompt packs -- the ones its opening carried, the others it
   could be sent, and what a person has sent it since -- and sends any of
   them into the terminal once (daemon/session_packs.py). A sent pack is not
   written to the session's definition; an mcp- or other pack the opening did
   not carry is held instead, so the session reminder names its id and
   rebrief gives it back after a compaction -- until "drop" here.
   All text from the daemon goes in through textContent. */

function packsNode(tag, text, cls) {
  const n = document.createElement(tag);
  if (text != null) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}

async function packsRequest(path, method = "GET", body) {
  const response = await api(path, {method, ...(body ? {headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)} : {})});
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(result.error || `HTTP ${response.status}`);
  return result;
}

/* Why a given pack is in the opening, in the words its header uses. */
function packsWhy(row) {
  if (row.required_by?.length) return "required by " + row.required_by.join(", ");
  if (row.always) return "workspace prompt";
  if (row.source === "daemon") return "daemon prompt";
  if (row.ticked) return "ticked at creation";
  return "chosen at creation";
}

const PACKS_KIND = {
  mcp: "MCP tool guide", "on-demand": "on demand", opening: "opening pack", always: "workspace prompt",
};

function makePacksPane({root, session}) {
  const node = packsNode;
  const base = "/api/sessions/" + encodeURIComponent(session);
  let view = null, briefing = null, error = "", notice = "", busy = "", seq = 0;
  // The previews open right now, by key ("briefing" or a pack id), with the
  // block the daemon would type; a re-render keeps them open.
  const open = new Map();

  async function load() {
    const mine = ++seq;
    try {
      const [packs, brief] = await Promise.all([
        packsRequest(base + "/packs"),
        packsRequest(base + "/rebrief"),
      ]);
      if (mine !== seq) return;
      view = packs; briefing = brief.block || ""; error = "";
    } catch (exc) { if (mine === seq) error = exc.message; }
    if (mine === seq) render();
  }

  async function toggle(key) {
    if (open.has(key)) { open.delete(key); render(); return; }
    if (key === "briefing") { open.set(key, briefing); render(); return; }
    open.set(key, null); render();
    try {
      const out = await packsRequest(base + "/packs?pack=" + encodeURIComponent(key));
      if (open.has(key)) open.set(key, out.block);
    } catch (exc) { open.delete(key); error = exc.message; }
    render();
  }

  async function send(key) {
    if (busy) return;
    const what = key === "briefing" ? "the briefing" : `prompt pack ${key}`;
    if (!globalThis.confirm(`Send ${what} to ${session}? It is typed into the terminal as a new message.`)) return;
    busy = key; error = ""; notice = ""; render();
    try {
      const out = key === "briefing"
        ? await packsRequest(base + "/rebrief", "POST")
        : await packsRequest(base + "/packs", "POST", {pack: key});
      notice = out.empty ? "nothing to re-brief: the briefing is empty"
        : out.delivered === false ? "the session did not take it: it is not running or not reading input"
        : `${what} sent`;
    } catch (exc) { error = exc.message; }
    finally { busy = ""; await load(); }
  }

  async function drop(key) {
    if (busy) return;
    if (!globalThis.confirm(`Stop holding prompt pack ${key} for ${session}? ` +
                            "The reminder stops naming it and rebrief stops giving it back; " +
                            "what the session already read stays in its context.")) return;
    busy = key; error = ""; notice = ""; render();
    try {
      await packsRequest(base + "/packs?pack=" + encodeURIComponent(key), "DELETE");
      notice = `prompt pack ${key} dropped`;
    } catch (exc) { error = exc.message; }
    finally { busy = ""; await load(); }
  }

  function actions(key, label, held) {
    const box = node("span", null, "packs-actions");
    const peek = node("button", open.has(key) ? "hide" : "view", "wf-btn");
    peek.type = "button"; peek.onclick = () => toggle(key);
    const go = node("button", busy === key ? "sending…" : label, "wf-btn packs-send");
    go.type = "button"; go.disabled = !!busy; go.onclick = () => send(key);
    box.append(peek, go);
    if (held) {
      const off = node("button", "drop", "wf-btn packs-drop");
      off.type = "button"; off.disabled = !!busy; off.onclick = () => drop(key);
      box.append(off);
    }
    return box;
  }

  function preview(key) {
    if (!open.has(key)) return null;
    const text = open.get(key);
    return node("pre", text == null ? "loading…" : text || "(empty)", "packs-preview");
  }

  function row(key, title, meta, label, held) {
    const li = node("li", null, "packs-row");
    li.dataset.pack = key;
    const line = node("div", null, "packs-line");
    const name = node("span", null, "packs-name");
    name.append(node("strong", key === "briefing" ? "briefing" : key));
    if (title) name.append(node("span", title, "packs-title"));
    line.append(name, node("span", meta, "packs-meta"), actions(key, label, held));
    li.append(line);
    const pre = preview(key);
    if (pre) li.append(pre);
    return li;
  }

  function section(heading, hint, rows) {
    const box = node("section", null, "packs-section");
    box.append(node("h4", heading));
    if (hint) box.append(node("p", hint, "packs-muted"));
    const list = node("ul", null, "packs-list");
    for (const r of rows) list.append(r);
    if (!rows.length) list.append(node("li", "none", "packs-muted"));
    box.append(list);
    return box;
  }

  function render() {
    const el = root(); if (!el) return;
    el.replaceChildren();
    const shell = node("div", null, "packs-pane");
    if (error) { const a = node("p", error, "packs-error"); a.setAttribute("role", "alert"); shell.append(a); }
    if (notice) shell.append(node("p", notice, "packs-notice"));
    if (!view) { shell.append(node("p", error ? "" : "loading…", "wf-note")); el.append(shell); return; }
    shell.append(section("Briefing",
      "mesh, run, parent and children, opening task -- composed now from the daemon's state",
      [row("briefing", "", briefing ? `${briefing.split("\n").length} lines` : "empty", "send")]));
    shell.append(section("In the opening",
      "given at creation; sending one again types the recorded text, for a session that lost it to a compaction",
      (view.given || []).map((p) => row(p.id, p.title || "",
        [packsWhy(p), p.source, `${p.lines} lines`, `id ${p.text_id}`, p.changed ? "file edited since" : ""]
          .filter(Boolean).join(" · "), "send again"))));
    shell.append(section("Not in the opening",
      "typed into the terminal once; an MCP guide or other pack is then held -- the reminder names its id and rebrief gives it back after a compaction -- until dropped (on-demand packs are not held)",
      (view.available || []).map((p) => row(p.id, p.title || "",
        [PACKS_KIND[p.kind] || p.kind, p.source, `${p.lines} lines`,
         p.held ? `held · id ${p.held.text_id}` : ""].filter(Boolean).join(" · "),
        p.held ? "send again" : "send", !!p.held))));
    const history = node("section", null, "packs-section");
    history.append(node("h4", "Sent"));
    const log = node("ul", null, "packs-list packs-sent");
    for (const s of view.sent || []) {
      log.append(node("li", `${String(s.at || "").replace("T", " ").replace("Z", "")} · ${s.pack} · ${s.how}` +
        (s.delivered ? "" : " · not delivered"), s.delivered ? "" : "packs-muted"));
    }
    if (!(view.sent || []).length) log.append(node("li", "nothing sent from here yet", "packs-muted"));
    history.append(log);
    shell.append(history);
    el.append(shell);
  }

  return {open: load, stop: () => { ++seq; }};
}

globalThis.PacksPane = (() => {
  let view = null, el = null, host = null, name = "";
  function mount(target, session) {
    if (view && host === target && name === session && target.contains(el)) return;
    unmount();
    el = document.createElement("div"); el.className = "packs-host";
    target.replaceChildren(el); name = session; host = target;
    view = makePacksPane({root: () => el, session}); view.open();
  }
  function unmount() { if (view) view.stop(); view = null; if (el) el.remove(); el = host = null; name = ""; }
  return {mount, unmount};
})();
