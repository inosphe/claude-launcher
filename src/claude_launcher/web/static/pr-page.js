/* The PR page (#/pr): every pull request the sessions on this daemon
   recorded, grouped by project and laid out on a date timeline, with the
   session that opened each one.

   What it reads: /api/prs, which merges the PR wizard's records and each v2
   run's `prs` collection as the daemon already keeps them. No gh call is
   made by the list, so it works without gh installed or logged in;
   `status` and `title` are the values recorded when the PR was opened, and
   the page says that beside the list. A recorded row with no timestamp (the
   records written before the collection stamped `at`) is shown under
   "날짜 미상" rather than given an invented date.

   A row opens a panel with the live state for that PR
   (/api/prs/<host>/<owner>/<name>/<number>) and, when the daemon says the
   merge would succeed, the merge itself. The daemon runs gh -- the browser
   never reaches the forge, which a relay tunnel cannot do anyway -- and the
   merge is refused unless the request came from the dashboard's own cookie
   (daemon/api.py, h_pr_merge).

   Every request goes through `url()`, the app's base-path helper
   (app.js:31): "/" when this page is served directly and "/t/<name>/" when
   it is reached through a relay tunnel. A bare "/api/..." leaves the tunnel
   and the relay answers 404 -- that was claunch-ijkge.3.

   The model below is plain data so tests/web/prpage_check.js can run it
   without a page. The canvas is a straight rebuild: the list is short and
   only changes when a session opens a PR, so a redraw that would draw the
   same thing costs nothing worth avoiding. */
globalThis.PrPage = (() => {
  const POLL_MS = 60000;
  const VIEW = "pr-view";

  let timer = null;
  let payload = null;
  let project = "";
  let status = "";
  let loading = false;
  let error = "";

  /* The panel one row opens. `detail` is the daemon's answer for that PR
     (live state from gh, the merge verdict, the conflicting paths when it
     could compute them); `row` keeps the recorded values so the panel still
     has a title when the read fails. */
  let detailFor = null;
  let detail = null;
  let detailError = "";
  let detailLoading = false;
  let mergeArmed = false;
  let mergeMethod = "auto";
  let mergeError = "";
  let mergeResult = null;
  let rebaseNote = "";
  let busy = false;

  /* One status word for the filter and the chip: a draft is its own state,
     and a record with no status is "recorded" (the value the wizard leaves
     behind) rather than an invented "open". */
  function prState(pr) {
    if (pr && pr.draft) return "draft";
    const s = String((pr && pr.status) || "").toLowerCase();
    if (s === "open" || s === "opened") return "open";
    if (s === "merged") return "merged";
    if (s === "closed" || s === "declined") return "closed";
    return "recorded";
  }

  /* The date a row is filed under: the recorded stamp's day, or "" for a
     record that predates the stamp. */
  function dayOf(pr) {
    const at = pr && typeof pr.at === "string" ? pr.at : "";
    return at.length >= 10 ? at.slice(0, 10) : "";
  }

  function projectName(pr) {
    const name = pr && typeof pr.project === "string" ? pr.project.trim() : "";
    return name || "default";
  }

  function matches(pr, want) {
    if (!want || want === "all") return true;
    return prState(pr) === want;
  }

  /* {projects: [{project, count, dates: [{date, prs: [...]}]}], total}
     The rows arrive newest first (the daemon sorts by the recorded stamp);
     the model keeps that order inside each day. */
  function buildModel(data, opts) {
    const rows = Array.isArray(data && data.prs) ? data.prs : [];
    const wantProject = (opts && opts.project) || "";
    const wantStatus = (opts && opts.status) || "";
    const byProject = new Map();
    let total = 0;
    for (const pr of rows) {
      if (wantProject && projectName(pr) !== wantProject) continue;
      if (!matches(pr, wantStatus)) continue;
      const name = projectName(pr);
      const day = dayOf(pr);
      let group = byProject.get(name);
      if (!group) {
        group = new Map();
        byProject.set(name, group);
      }
      let list = group.get(day);
      if (!list) {
        list = [];
        group.set(day, list);
      }
      list.push(pr);
      total += 1;
    }
    const projects = [];
    for (const name of [...byProject.keys()].sort((a, b) => a.localeCompare(b))) {
      const dates = [];
      const days = byProject.get(name);
      // A real day first, newest day first; the undated bucket last.
      const keys = [...days.keys()].sort((a, b) => {
        if (!a) return 1;
        if (!b) return -1;
        return b.localeCompare(a);
      });
      for (const day of keys) {
        dates.push({ date: day, label: day || "날짜 미상", prs: days.get(day) });
      }
      projects.push({ project: name, count: dates.reduce((n, d) => n + d.prs.length, 0), dates });
    }
    return { projects, total };
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function external(url, className, text) {
    const a = el("a", className, text);
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    return a;
  }

  /* ── the daemon's address for one PR ────────────────────────────────
     A recorded row carries `repo` as `<host>/<owner>/<name>`, which is what
     `gh -R` takes. The daemon's route spells those as four path segments
     instead of a slug, because `%2F` is a separator by the time aiohttp has
     decoded the path. `endpoint` returns the path WITHOUT the page's own
     prefix -- every caller wraps it in `url()`, which is what makes the page
     work under a relay tunnel (/t/<name>/) as well as at the root. */
  function splitRepo(repo) {
    const parts = String(repo || "").split("/");
    if (parts.length !== 3) return null;
    for (const part of parts) {
      if (!/^[A-Za-z0-9._-]+$/.test(part)) return null;
    }
    return { host: parts[0], owner: parts[1], name: parts[2] };
  }

  function endpoint(repo, number, suffix) {
    const parts = splitRepo(repo);
    const n = Number(number);
    if (!parts || !(n > 0)) return "";
    const path = `/api/prs/${parts.host}/${parts.owner}/${parts.name}/${n}`;
    return suffix ? `${path}/${suffix}` : path;
  }

  function canMerge(view) {
    return !!(view && view.verdict && view.verdict.can_merge === true);
  }

  function codeBlock(text) {
    const pre = el("pre", "pr-detail-raw", String(text || ""));
    return pre;
  }

  /* An error arrives in two shapes: a route refusal sends a sentence
     ({"error": "..."}), a classified gh failure sends {kind, message, raw}. */
  function errorText(body) {
    if (!body || !body.error) return "";
    if (typeof body.error === "string") return body.error;
    return body.error.message || "";
  }

  function openDetail(pr) {
    detailFor = { repo: pr.repo || "", number: pr.number || 0, row: pr };
    detail = null;
    detailError = "";
    mergeArmed = false;
    mergeError = "";
    mergeResult = null;
    rebaseNote = "";
    if (!endpoint(pr.repo, pr.number)) {
      detailError = "이 기록의 저장소 주소(repo)가 <host>/<owner>/<name> 형식이 아니어서 "
        + "데몬에 상세를 요청할 수 없습니다.";
      render();
      return;
    }
    /* Set before the first paint so the panel says "reading" rather than
       drawing an empty verdict for a frame. */
    detailLoading = true;
    render();
    loadDetail();
  }

  function closeDetail() {
    detailFor = null;
    detail = null;
    detailError = "";
    mergeArmed = false;
    mergeError = "";
    mergeResult = null;
    rebaseNote = "";
    render();
  }

  async function loadDetail() {
    if (!detailFor) return;
    const path = endpoint(detailFor.repo, detailFor.number);
    if (!path) return;
    detailLoading = true;
    detailError = "";
    render();
    try {
      const res = await fetch(url(path), { cache: "no-store" });
      const body = await res.json().catch(() => null);
      if (!res.ok) {
        /* A route-level refusal (400/403/404/503) sends {"error": "<문장>"};
           a read that ran and failed sends 200 with error.kind/raw instead.
           Only the second is a view the panel can draw. */
        detail = null;
        detailError = errorText(body) || `HTTP ${res.status}`;
      } else {
        if (!body) throw new Error(`HTTP ${res.status}`);
        detail = body;
      }
    } catch (err) {
      detail = null;
      detailError = err && err.message ? err.message : String(err);
    } finally {
      detailLoading = false;
      render();
    }
  }

  async function postDetail(suffix, payload) {
    const path = endpoint(detailFor && detailFor.repo, detailFor && detailFor.number, suffix);
    if (!path) throw new Error("요청 주소를 만들 수 없습니다.");
    const res = await fetch(url(path), {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await res.json().catch(() => null);
    return { res, body };
  }

  async function doMerge() {
    if (busy || !detailFor) return;
    busy = true;
    mergeError = "";
    mergeResult = null;
    render();
    try {
      const { res, body } = await postDetail("merge", { method: mergeMethod, confirm: true });
      if (body && body.ok) {
        mergeResult = body;
      } else {
        const err = body && typeof body.error === "object" && body.error ? body.error : {};
        mergeError = err.message || errorText(body) || `HTTP ${res.status}`;
        mergeResult = { error: err };
      }
    } catch (err) {
      mergeError = err && err.message ? err.message : String(err);
    } finally {
      busy = false;
      mergeArmed = false;
      render();
      if (mergeResult && mergeResult.ok) loadDetail();
    }
  }

  async function askRebase() {
    if (busy || !detailFor) return;
    busy = true;
    mergeError = "";
    render();
    try {
      const { res, body } = await postDetail("rebase-request", { note: rebaseNote });
      if (body && body.delivered) {
        mergeResult = { rebase: body };
      } else if (body) {
        mergeError = `세션에 전달하지 못했습니다 — ${body.why || "사유 미상"}`;
        mergeResult = { rebase: body };
      } else {
        mergeError = `HTTP ${res.status}`;
      }
    } catch (err) {
      mergeError = err && err.message ? err.message : String(err);
    } finally {
      busy = false;
      render();
    }
  }

  function checkLine(label, value) {
    const line = el("div", "pr-detail-row");
    line.appendChild(el("span", "pr-detail-key", label));
    line.appendChild(el("span", "pr-detail-val", value));
    return line;
  }

  function renderDetail(host) {
    if (!detailFor) return;
    const view = detail || {};
    const pr = view.pr || {};
    const recorded = view.recorded || detailFor.row || {};
    const verdict = view.verdict || {};
    const box = el("div", "pr-detail-box");

    const head = el("div", "pr-detail-head");
    head.appendChild(el("h3", "pr-detail-heading",
      `#${detailFor.number} ${pr.title || recorded.title || ""}`.trim()));
    const close = el("button", "pr-detail-close", "닫기");
    close.addEventListener("click", closeDetail);
    head.appendChild(close);
    box.appendChild(head);

    const externalUrl = pr.url || recorded.url;
    if (recorded.session || externalUrl) {
      const line = el("div", "pr-detail-links");
      if (recorded.session) {
        const link = el("a", "pr-session", `세션 ${recorded.session}`);
        link.href = `#/s/${encodeURIComponent(recorded.session)}`;
        line.appendChild(link);
      }
      if (externalUrl) {
        line.appendChild(external(externalUrl, "pr-detail-external", "github에서 열기"));
        line.appendChild(el("span", "pr-detail-hint",
          "이 링크는 브라우저가 내부망에 닿아야 열립니다."));
      }
      box.appendChild(line);
    }

    if (detailLoading) {
      box.appendChild(el("div", "pr-empty", "데몬에서 PR 상태를 읽는 중…"));
      host.appendChild(box);
      return;
    }

    if (detailError || (detail && detail.ok === false)) {
      const err = (view && view.error) || {};
      const fail = el("div", "pr-error");
      fail.appendChild(el("strong", null, "PR 상태를 읽지 못했습니다. "));
      fail.appendChild(el("span", null, detailError || err.message || ""));
      box.appendChild(fail);
      if (err.raw) box.appendChild(codeBlock(err.raw));
      box.appendChild(el("p", "pr-detail-hint",
        "목록의 기록값은 그대로입니다 — 데몬이 github에 닿지 못해도 페이지는 살아 있습니다."));
      host.appendChild(box);
      return;
    }

    const facts = el("div", "pr-detail-facts");
    facts.appendChild(checkLine("상태", pr.state || "—"));
    facts.appendChild(checkLine("브랜치", `${pr.head || "?"} → ${pr.base || "?"}`));
    const checks = pr.checks || {};
    facts.appendChild(checkLine("체크",
      `${checks.total || 0}건 · 통과 ${checks.passing || 0} · 실패 ${checks.failing || 0} · 진행 ${checks.pending || 0}`));
    facts.appendChild(checkLine("리뷰", pr.review || "—"));
    box.appendChild(facts);

    const verdictBox = el("div", verdict.kind === "clean" ? "pr-verdict pr-verdict-ok"
                                                         : "pr-verdict pr-verdict-stop");
    verdictBox.appendChild(el("strong", null, verdict.kind === "clean" ? "머지 가능 " : "머지 불가 "));
    verdictBox.appendChild(el("span", null, verdict.reason || ""));
    box.appendChild(verdictBox);

    const conflicts = view.conflicts;
    if (verdict.kind === "conflict" && conflicts) {
      const section = el("section", "pr-detail-section");
      section.appendChild(el("h4", null, "충돌 파일"));
      if (conflicts.computed && conflicts.files && conflicts.files.length) {
        const list = el("ul", "pr-conflicts");
        for (const path of conflicts.files) list.appendChild(el("li", null, path));
        section.appendChild(list);
      } else if (conflicts.computed) {
        section.appendChild(el("p", "pr-detail-hint",
          "충돌 파일이 없습니다 — 기준선과 같은 내용으로 이미 반영되었을 수 있습니다."));
      } else {
        section.appendChild(el("p", "pr-detail-hint",
          `충돌 파일을 계산하지 못했습니다. ${conflicts.why || ""}`.trim()));
      }
      box.appendChild(section);
    }

    /* The merge itself: two steps, because one click on a list is too
       little for an action that cannot be undone from here. */
    if (verdict.kind === "clean") {
      const actions = el("div", "pr-detail-actions");
      if (!mergeArmed) {
        const start = el("button", "pr-merge", "머지…");
        start.addEventListener("click", () => { mergeArmed = true; mergeError = ""; render(); });
        actions.appendChild(start);
        actions.appendChild(el("span", "pr-detail-hint", "방식과 확인을 한 번 더 고릅니다."));
      } else {
        const select = el("select", "pr-merge-method");
        for (const [value, label] of [
          ["auto", "저장소 설정 따름"], ["merge", "머지 커밋"], ["squash", "스쿼시"], ["rebase", "리베이스"],
        ]) {
          const opt = el("option", null, label);
          opt.value = value;
          select.appendChild(opt);
        }
        select.value = mergeMethod;
        select.addEventListener("change", () => { mergeMethod = select.value; });
        actions.appendChild(select);
        const go = el("button", "pr-merge pr-merge-go", busy ? "머지 중…" : "정말 머지");
        go.addEventListener("click", () => { if (!busy) doMerge(); });
        actions.appendChild(go);
        const cancel = el("button", "pr-merge-cancel", "취소");
        cancel.addEventListener("click", () => { mergeArmed = false; render(); });
        actions.appendChild(cancel);
      }
      box.appendChild(actions);
    }

    if (verdict.kind === "conflict" || verdict.kind === "behind") {
      const actions = el("div", "pr-detail-actions");
      const input = el("input", "pr-rebase-note");
      input.type = "text";
      input.placeholder = "세션에 함께 보낼 메모(선택)";
      input.value = rebaseNote;
      input.addEventListener("input", () => { rebaseNote = input.value; });
      actions.appendChild(input);
      const ask = el("button", "pr-rebase", busy ? "보내는 중…" : "세션에 rebase 요청");
      ask.addEventListener("click", () => { if (!busy) askRebase(); });
      actions.appendChild(ask);
      box.appendChild(actions);
    }

    if (mergeResult && mergeResult.ok) {
      const done = el("div", "pr-verdict pr-verdict-ok");
      done.appendChild(el("strong", null, "머지했습니다. "));
      done.appendChild(el("span", null,
        `${mergeResult.method || ""} 방식 · ${mergeResult.output || ""}`.trim()));
      box.appendChild(done);
    }
    if (mergeResult && mergeResult.rebase) {
      const sent = el("div", mergeResult.rebase.delivered ? "pr-verdict pr-verdict-ok"
                                                          : "pr-verdict pr-verdict-stop");
      sent.appendChild(el("strong", null,
        mergeResult.rebase.delivered ? "요청을 보냈습니다. " : "요청을 전달하지 못했습니다. "));
      sent.appendChild(el("span", null, mergeResult.rebase.why || ""));
      box.appendChild(sent);
      if (!mergeResult.rebase.delivered && mergeResult.rebase.text) {
        box.appendChild(codeBlock(mergeResult.rebase.text));
      }
    }
    if (mergeError) {
      const fail = el("div", "pr-error");
      fail.appendChild(el("strong", null, "머지하지 못했습니다. "));
      fail.appendChild(el("span", null, mergeError));
      box.appendChild(fail);
      const err = mergeResult && mergeResult.error;
      if (err && err.raw) box.appendChild(codeBlock(err.raw));
    }

    host.appendChild(box);
  }

  function controls(host, projects) {
    const bar = el("div", "pr-controls");
    const projectLabel = el("label", "pr-filter", "프로젝트");
    const projectSel = el("select", "pr-project-select");
    const allProject = el("option", null, "전체 프로젝트");
    allProject.value = "";
    projectSel.appendChild(allProject);
    for (const name of projects || []) {
      const opt = el("option", null, name);
      opt.value = name;
      projectSel.appendChild(opt);
    }
    projectSel.value = project;
    projectSel.addEventListener("change", () => {
      project = projectSel.value;
      render();
    });
    projectLabel.appendChild(projectSel);
    bar.appendChild(projectLabel);

    const statusLabel = el("label", "pr-filter", "상태");
    const statusSel = el("select", "pr-status-select");
    for (const [value, label] of [
      ["", "전체 상태"], ["open", "Open"], ["merged", "Merged"],
      ["closed", "Closed"], ["draft", "Draft"], ["recorded", "기록됨"],
    ]) {
      const opt = el("option", null, label);
      opt.value = value;
      statusSel.appendChild(opt);
    }
    statusSel.value = status;
    statusSel.addEventListener("change", () => {
      status = statusSel.value;
      render();
    });
    statusLabel.appendChild(statusSel);
    bar.appendChild(statusLabel);
    host.appendChild(bar);
    return { project: projectSel, status: statusSel };
  }

  function row(pr) {
    const line = el("div", "pr-row");
    line.appendChild(external(pr.url, "pr-num", `#${pr.number}`));
    const main = el("div", "pr-main");
    const title = typeof pr.title === "string" && pr.title ? pr.title : (pr.branch || "제목 미기록");
    main.appendChild(external(pr.url, "pr-title", title));
    const meta = el("div", "pr-meta");
    if (pr.repo) meta.appendChild(el("span", "pr-repo", pr.repo));
    const state = prState(pr);
    meta.appendChild(el("span", `pr-state pr-state-${state}`, state.toUpperCase()));
    const branch = pr.branch && pr.base ? `${pr.branch} → ${pr.base}` : (pr.branch || pr.base || "");
    if (branch) meta.appendChild(el("span", "pr-branch", branch));
    if (typeof pr.session === "string" && pr.session) {
      const link = el("a", "pr-session", `세션 ${pr.session}`);
      link.href = `#/s/${encodeURIComponent(pr.session)}`;
      meta.appendChild(link);
    }
    for (const issue of Array.isArray(pr.issues) ? pr.issues : []) {
      if (typeof issue === "string" && issue) meta.appendChild(el("span", "pr-issue", issue));
    }
    if (pr.source) meta.appendChild(el("span", "pr-source", pr.source));
    main.appendChild(meta);
    line.appendChild(main);
    /* A row opens the panel; the two anchors inside it still open github,
       which is the one thing a reader on the internal network may want. */
    line.classList.add("pr-clickable");
    line.addEventListener("click", (event) => {
      const target = event && event.target;
      if (target && target.closest && target.closest("a")) return;
      openDetail(pr);
    });
    return line;
  }

  function render() {
    const host = document.getElementById(VIEW);
    if (!host) return;
    host.textContent = "";
    const head = el("div", "pr-head");
    head.appendChild(el("h2", null, "Pull requests"));
    const count = payload && typeof payload.count === "number" ? payload.count : 0;
    head.appendChild(el("span", "pr-count", String(count)));
    head.appendChild(el("p", "wf-note",
      "세션이 기록한 PR입니다(PR 위저드 · improv-worker-v2의 pr-open). 상태·제목은 기록 시점 값이고 gh를 다시 묻지 않으므로, 최신 상태·머지 가능 여부는 PR 링크에서 확인하세요."));
    host.appendChild(head);

    controls(host, (payload && payload.projects) || []);

    /* The panel is drawn on every path below: a merge that just failed is
       exactly when the list must not be able to take it away. */
    if (loading && !payload) {
      host.appendChild(el("div", "pr-empty", "불러오는 중…"));
      renderDetail(host);
      return;
    }
    if (error) {
      const box = el("div", "pr-error");
      box.appendChild(el("strong", null, "데몬에 닿지 못했습니다. "));
      box.appendChild(el("span", null, error));
      host.appendChild(box);
      renderDetail(host);
      return;
    }
    const model = buildModel(payload, { project, status });
    if (!model.total) {
      host.appendChild(el("div", "pr-empty",
        "이 조건에 기록된 PR이 없습니다. 세션의 PR 위저드나 improv-worker-v2의 pr-open이 기록한 PR만 이 목록에 나타나고, claunch 밖에서 만든 PR은 기록이 없어 보이지 않습니다."));
      renderDetail(host);
      return;
    }
    const list = el("div", "pr-list");
    for (const group of model.projects) {
      const section = el("section", "pr-project");
      const heading = el("h3", "pr-project-head");
      heading.appendChild(el("span", "pr-project-name", group.project));
      heading.appendChild(el("span", "pr-project-count", `${group.count} PR`));
      section.appendChild(heading);
      for (const day of group.dates) {
        const dayBox = el("section", "pr-day");
        dayBox.appendChild(el("h4", "pr-day-head", day.label));
        const rows = el("div", "pr-rows");
        for (const pr of day.prs) rows.appendChild(row(pr));
        dayBox.appendChild(rows);
        section.appendChild(dayBox);
      }
      list.appendChild(section);
    }
    host.appendChild(list);
    renderDetail(host);
  }

  async function load() {
    loading = true;
    render();
    try {
      /* url() carries the page's own base path: "/" served directly,
         "/t/<name>/" under a relay tunnel. A bare "/api/prs" leaves the
         tunnel and the relay answers 404 -- that was claunch-ijkge.3. */
      const res = await fetch(url("/api/prs"), { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      payload = await res.json();
      error = "";
    } catch (err) {
      error = err && err.message ? err.message : String(err);
    } finally {
      loading = false;
      render();
    }
  }

  function open() {
    render();
    load();
    if (!timer) timer = setInterval(load, POLL_MS);
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
  }

  return {
    open, stop, load, render, buildModel, prState, dayOf, VIEW,
    splitRepo, endpoint, canMerge, openDetail, closeDetail, loadDetail,
  };
})();
