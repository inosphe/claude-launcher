/* The commit page (#/commit/<sha>?session=<name> or ?cwd=<dir>): one commit
   read whole -- its message, who wrote and committed it, its parents, the
   files it changed with their line counts, and each file's diff against the
   first parent, drawn unified or side by side. Drawn from GET /api/commit
   (claude_launcher/commit_detail.py), which already splits the patch into
   hunks and numbers every line, so this file only lays it out.

   Every surface that shows a commit links here through `href`: the address
   carries the sha and the repository it lives in -- a session's name where
   the list is a session's (the rail card, the Details panel), a directory
   where the surface knows a repository and no session. The daemon refuses a
   directory no workspace or session uses. */
globalThis.CommitPage = (() => {
  const STATUS_MARK = {
    added: ["A", "추가"],
    deleted: ["D", "삭제"],
    modified: ["M", "수정"],
    renamed: ["R", "이름 변경"],
    copied: ["C", "복사"],
    "type-changed": ["T", "형식 변경"],
  };
  // A file this long starts folded: opening the page should show the list
  // of what changed, not one generated file's ten thousand lines.
  const FOLD_LINES = 600;
  const MODE_KEY = "claunch.commitDiffMode";

  let want = null;       // {sha, session, cwd} the address names
  let data = null, error = "", loading = false;
  let sequence = 0;
  let mode = readMode();
  const folded = new Set();

  function readMode() {
    try { return localStorage.getItem(MODE_KEY) === "split" ? "split" : "unified"; }
    catch { return "unified"; }
  }
  function saveMode(m) {
    mode = m;
    try { localStorage.setItem(MODE_KEY, m); } catch { /* private window */ }
  }

  /* The address of a commit's page. `where` names the repository:
     {session} or {cwd}; with neither the page has nothing to read from. */
  function href(sha, where = {}) {
    const params = new URLSearchParams();
    if (where.session) params.set("session", where.session);
    else if (where.cwd) params.set("cwd", where.cwd);
    // Keep the rail's project narrowing across the jump (hashWithProject).
    // app.js's `currentProject` is a top-level `let`: shared by name across
    // classic scripts, never a property of globalThis.
    const project = typeof currentProject === "string" ? currentProject : "";
    if (project) params.set("project", project);
    const q = params.toString();
    return `#/commit/${encodeURIComponent(String(sha || ""))}${q ? "?" + q : ""}`;
  }

  /* Turn `elem` into a link to the commit's page. It sits inside cards
     whose own click attaches a session, so the click stops here; a modified
     click (new tab) is left to the browser. */
  function link(elem, sha, where) {
    if (!elem || !sha) return elem;
    if (elem.tagName === "A") elem.href = href(sha, where);
    else {
      elem.setAttribute("role", "link");
      elem.tabIndex = 0;
      elem.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); location.hash = href(sha, where); }
      });
    }
    elem.classList.add("commit-link");
    elem.addEventListener("click", (e) => {
      e.stopPropagation();
      if (elem.tagName === "A") return;
      e.preventDefault();
      location.hash = href(sha, where);
    });
    return elem;
  }

  function node(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function stamp(iso) {
    const t = new Date(iso || "");
    if (!Number.isFinite(t.getTime())) return iso || "";
    return typeof localStamp === "function" ? localStamp(iso) : t.toLocaleString();
  }

  function view() { return document.getElementById("commit-view"); }

  function fileId(i) { return `commit-file-${i}`; }

  function fileLines(f) {
    let n = 0;
    for (const h of f.hunks || []) n += (h.lines || []).length;
    return n;
  }

  /* A hunk's rows paired for the side-by-side view: context on both sides,
     and each run of removals set against the run of additions after it. */
  function splitRows(lines) {
    const rows = [];
    let i = 0;
    while (i < lines.length) {
      const [kind] = lines[i];
      if (kind === " ") { rows.push([lines[i], lines[i]]); i++; continue; }
      if (kind === "\\") { rows.push([lines[i], null, true]); i++; continue; }
      const dels = [], adds = [];
      while (i < lines.length && lines[i][0] === "-") dels.push(lines[i++]);
      while (i < lines.length && lines[i][0] === "+") adds.push(lines[i++]);
      if (!dels.length && !adds.length) { i++; continue; }
      for (let k = 0; k < Math.max(dels.length, adds.length); k++) {
        rows.push([dels[k] || null, adds[k] || null]);
      }
    }
    return rows;
  }

  function num(cls, v) { return node("td", "cd-num " + cls, v === null || v === undefined ? "" : v); }

  function codeCell(line, side) {
    const kind = line ? line[0] : "";
    const cls = !line ? "cd-empty" : kind === "+" ? "cd-add" : kind === "-" ? "cd-del" : "cd-ctx";
    const td = node("td", "cd-code " + cls);
    if (line) {
      td.appendChild(node("span", "cd-sign", kind === " " ? " " : kind));
      td.appendChild(node("span", "cd-text", line[3]));
    }
    if (side) td.dataset.side = side;
    return td;
  }

  function hunkTable(f) {
    const table = node("table", "cd-table cd-" + mode);
    const cols = mode === "split" ? 4 : 3;
    if (mode === "split") {
      // A fixed layout takes its widths from the first row, which is a hunk
      // header spanning every column; the columns say them instead.
      const group = node("colgroup");
      for (const cls of ["cd-col-num", "cd-col-code", "cd-col-num", "cd-col-code"]) {
        group.appendChild(node("col", cls));
      }
      table.appendChild(group);
    }
    const body = node("tbody");
    for (const h of f.hunks || []) {
      const hr = node("tr", "cd-hunk");
      const td = node("td", "cd-hunk-head", h.header);
      td.colSpan = cols;
      hr.appendChild(td);
      body.appendChild(hr);
      if (mode === "split") {
        for (const [left, right, note] of splitRows(h.lines || [])) {
          const tr = node("tr", note ? "cd-note" : "cd-row");
          if (note) {
            const n = node("td", "cd-note-text", left[3]);
            n.colSpan = cols;
            tr.appendChild(n);
          } else {
            tr.appendChild(num("cd-old", left ? left[1] : null));
            tr.appendChild(codeCell(left, "old"));
            tr.appendChild(num("cd-new", right ? right[2] : null));
            tr.appendChild(codeCell(right, "new"));
          }
          body.appendChild(tr);
        }
      } else {
        for (const line of h.lines || []) {
          if (line[0] === "\\") {
            const tr = node("tr", "cd-note");
            const n = node("td", "cd-note-text", line[3]);
            n.colSpan = cols;
            tr.appendChild(n);
            body.appendChild(tr);
            continue;
          }
          const tr = node("tr", "cd-row");
          tr.appendChild(num("cd-old", line[1]));
          tr.appendChild(num("cd-new", line[2]));
          tr.appendChild(codeCell(line));
          body.appendChild(tr);
        }
      }
    }
    table.appendChild(body);
    return table;
  }

  function statusBadge(f) {
    const [mark, word] = STATUS_MARK[f.status] || ["?", f.status || "?"];
    const b = node("span", `cd-status cd-status-${f.status || "unknown"}`, mark);
    b.title = word + (f.similarity !== null && f.similarity !== undefined ? ` (${f.similarity}%)` : "");
    return b;
  }

  function pathLabel(f) {
    const box = node("span", "cd-path");
    if (f.old_path && f.old_path !== f.path) {
      box.appendChild(node("span", "cd-path-old", f.old_path));
      box.appendChild(node("span", "cd-path-arrow", " → "));
    }
    box.appendChild(node("span", "cd-path-new", f.path));
    return box;
  }

  function counts(f) {
    const box = node("span", "cd-counts");
    if (f.binary) { box.appendChild(node("span", "cd-bin", "binary")); return box; }
    box.appendChild(node("span", "cd-plus", `+${f.additions ?? 0}`));
    box.appendChild(node("span", "cd-minus", `−${f.deletions ?? 0}`));
    return box;
  }

  function fileBlock(f, i) {
    const section = node("section", "cd-file");
    section.id = fileId(i);
    const head = node("div", "cd-file-head");
    const toggle = node("button", "cd-fold", folded.has(i) ? "▸" : "▾");
    toggle.type = "button";
    toggle.title = folded.has(i) ? "펼치기" : "접기";
    toggle.onclick = () => {
      if (folded.has(i)) folded.delete(i); else folded.add(i);
      render();
    };
    head.append(toggle, statusBadge(f), pathLabel(f), counts(f));
    const copy = node("button", "wf-btn cd-copy", "경로 복사");
    copy.type = "button";
    copy.onclick = () => navigator.clipboard?.writeText(f.path).catch(() => {});
    head.appendChild(copy);
    section.appendChild(head);
    if (folded.has(i)) return section;
    if (f.omitted) {
      section.appendChild(node("p", "cd-muted",
        "diff를 싣지 않았다: 커밋 전체 diff가 크기 상한을 넘어 이 파일은 목록에만 있다. " +
        "`git show " + (data.short || "") + " -- " + f.path + "`로 볼 수 있다."));
      return section;
    }
    if (f.binary) {
      section.appendChild(node("p", "cd-muted", "바이너리 파일: 줄 단위 diff가 없다."));
      return section;
    }
    if (!(f.hunks || []).length) {
      section.appendChild(node("p", "cd-muted",
        f.status === "renamed" || f.status === "copied" ? "내용 변경 없음(경로만 바뀜)." : "내용 변경 없음(모드 변경 등)."));
      return section;
    }
    const wrap = node("div", "cd-scroll");
    wrap.appendChild(hunkTable(f));
    section.appendChild(wrap);
    if (f.truncated) {
      section.appendChild(node("p", "cd-warn",
        `잘림: 이 파일의 diff는 ${data.limits?.file_lines ?? "상한"}줄 또는 응답 크기 상한까지만 표시했다.`));
    }
    return section;
  }

  function personLine(label, p) {
    const row = node("div", "cd-meta-row");
    row.appendChild(node("span", "cd-meta-key", label));
    const v = node("span", "cd-meta-val");
    v.appendChild(node("span", "cd-person", p.name || ""));
    if (p.email) v.appendChild(node("span", "cd-muted", ` <${p.email}>`));
    v.appendChild(node("span", "cd-muted", "  ·  " + stamp(p.date)));
    if (p.date) v.title = p.date;
    row.appendChild(v);
    return row;
  }

  function metaRow(label, value) {
    const row = node("div", "cd-meta-row");
    row.appendChild(node("span", "cd-meta-key", label));
    const v = node("span", "cd-meta-val");
    if (value && typeof value === "object") v.appendChild(value); else v.textContent = value;
    row.appendChild(v);
    return row;
  }

  function where() {
    return want && want.session ? { session: want.session } : { cwd: (data && data.cwd) || (want && want.cwd) };
  }

  function header(d) {
    const box = node("header", "cd-head");
    const top = node("div", "cd-top");
    const back = node("button", "wf-btn", "← 뒤로");
    back.type = "button";
    back.onclick = () => { if (history.length > 1) history.back(); else location.hash = "#/"; };
    top.appendChild(back);
    top.appendChild(node("span", "cd-kicker", d.merge ? "merge commit" : "commit"));
    const sha = node("code", "cd-sha", d.sha);
    top.appendChild(sha);
    const copy = node("button", "wf-btn", "sha 복사");
    copy.type = "button";
    copy.onclick = () => navigator.clipboard?.writeText(d.sha).catch(() => {});
    top.appendChild(copy);
    box.appendChild(top);
    box.appendChild(node("h2", "cd-subject", d.subject || "(제목 없음)"));
    if (d.body) {
      // The trailers are listed below as rows; the body shows the prose.
      const keys = new Set((d.trailers || []).map((t) => `${t.key}: ${t.value}`));
      const text = d.body.split("\n").filter((l) => !keys.has(l.trim())).join("\n").trim();
      if (text) box.appendChild(node("pre", "cd-body", text));
    }
    const meta = node("div", "cd-meta");
    meta.appendChild(personLine("작성자", d.author || {}));
    const a = d.author || {}, c = d.committer || {};
    if (a.name !== c.name || a.email !== c.email || a.date !== c.date) {
      meta.appendChild(personLine("커미터", c));
    }
    const parents = node("span", "cd-parents");
    if (!(d.parents || []).length) parents.appendChild(node("span", "cd-muted", "없음 (최초 커밋)"));
    (d.parents || []).forEach((p, i) => {
      if (i) parents.appendChild(document.createTextNode("  "));
      parents.appendChild(link(node("a", "cd-parent", p.slice(0, 10)), p, where()));
    });
    meta.appendChild(metaRow(d.merge ? "부모 (diff는 첫 부모 기준)" : "부모", parents));
    if (d.cwd) meta.appendChild(metaRow("저장소", d.cwd));
    for (const t of d.trailers || []) {
      let value = t.value;
      if (t.key === "Claunch-Session" && t.value) {
        value = node("a", "cd-session", t.value);
        value.href = "#/s/" + encodeURIComponent(t.value);
      }
      meta.appendChild(metaRow(t.key, value));
    }
    box.appendChild(meta);
    return box;
  }

  function fileList(d) {
    const box = node("nav", "cd-files");
    const s = d.stats || {};
    const line = node("div", "cd-stats");
    line.appendChild(node("span", null, `파일 ${s.files ?? 0}개 변경`));
    line.appendChild(node("span", "cd-plus", `+${s.additions ?? 0}`));
    line.appendChild(node("span", "cd-minus", `−${s.deletions ?? 0}`));
    const tools = node("span", "cd-tools");
    for (const [m, label] of [["unified", "통합"], ["split", "분할"]]) {
      const b = node("button", "wf-btn" + (mode === m ? " cd-on" : ""), label);
      b.type = "button";
      b.setAttribute("aria-pressed", String(mode === m));
      b.onclick = () => { saveMode(m); render(); };
      tools.appendChild(b);
    }
    const all = node("button", "wf-btn", "모두 펼치기");
    all.type = "button";
    all.onclick = () => { folded.clear(); render(); };
    const none = node("button", "wf-btn", "모두 접기");
    none.type = "button";
    none.onclick = () => { (d.files || []).forEach((_, i) => folded.add(i)); render(); };
    tools.append(all, none);
    line.appendChild(tools);
    box.appendChild(line);
    if (d.truncated) {
      box.appendChild(node("p", "cd-warn",
        "잘림: 이 커밋의 diff 일부가 크기 상한을 넘어 표시되지 않았다. 해당 파일에 표시가 붙어 있다."));
    }
    const list = node("ol", "cd-file-list");
    (d.files || []).forEach((f, i) => {
      const li = node("li", "cd-file-item");
      li.appendChild(statusBadge(f));
      const a = node("button", "cd-file-jump");
      a.type = "button";
      a.appendChild(pathLabel(f));
      a.onclick = (e) => {
        e.preventDefault();
        if (folded.delete(i)) render();
        document.getElementById(fileId(i))?.scrollIntoView({ block: "start" });
      };
      li.appendChild(a);
      li.appendChild(counts(f));
      list.appendChild(li);
    });
    box.appendChild(list);
    return box;
  }

  function render() {
    const root = view();
    if (!root) return;
    root.replaceChildren();
    if (!want || !want.sha) {
      root.appendChild(node("p", "cd-muted", "커밋이 지정되지 않았다."));
      return;
    }
    if (error) {
      const back = node("button", "wf-btn", "← 뒤로");
      back.type = "button";
      back.onclick = () => history.back();
      root.append(back, node("p", "cd-error", `커밋 ${want.sha}를 읽지 못했다: ${error}`));
      return;
    }
    if (!data) {
      root.appendChild(node("p", "cd-muted", loading ? `커밋 ${want.sha} 읽는 중…` : ""));
      return;
    }
    root.appendChild(header(data));
    root.appendChild(fileList(data));
    const files = node("div", "cd-diffs");
    (data.files || []).forEach((f, i) => files.appendChild(fileBlock(f, i)));
    root.appendChild(files);
  }

  async function load() {
    const seq = ++sequence;
    loading = true; render();
    const q = new URLSearchParams({ sha: want.sha });
    if (want.session) q.set("session", want.session);
    else if (want.cwd) q.set("cwd", want.cwd);
    try {
      const response = await api(`/api/commit?${q.toString()}`);
      const body = await response.json();
      if (seq !== sequence) return;
      if (!response.ok) throw Error(body.error || `HTTP ${response.status}`);
      data = body; error = "";
      folded.clear();
      (data.files || []).forEach((f, i) => { if (fileLines(f) > FOLD_LINES) folded.add(i); });
    } catch (exc) {
      if (seq !== sequence) return;
      error = String(exc.message || exc);
    } finally {
      if (seq === sequence) { loading = false; render(); }
    }
  }

  function open(sha, where = {}) {
    const next = { sha: String(sha || ""), session: where.session || "", cwd: where.cwd || "" };
    const same = want && data && want.sha === next.sha && want.session === next.session &&
      want.cwd === next.cwd;
    want = next;
    if (same) { render(); return; }
    data = null; error = "";
    render();
    if (want.sha) load();
    view()?.scrollTo?.(0, 0);
  }

  function stop() { sequence++; loading = false; }

  return { open, stop, href, link, splitRows, fileLines };
})();
