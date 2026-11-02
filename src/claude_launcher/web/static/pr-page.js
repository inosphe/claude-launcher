/* The PR page (#/pr): every pull request the sessions on this daemon
   recorded, grouped by project and laid out on a date timeline, with the
   session that opened each one.

   What it reads: /api/prs, which merges the PR wizard's records and each v2
   run's `prs` collection as the daemon already keeps them. No gh call is
   made here or there, so the page works without gh installed or logged in;
   `status` and `title` are the values recorded when the PR was opened, and
   the page says that beside the list. A recorded row with no timestamp (the
   records written before the collection stamped `at`) is shown under
   "날짜 미상" rather than given an invented date.

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

    if (loading && !payload) {
      host.appendChild(el("div", "pr-empty", "불러오는 중…"));
      return;
    }
    if (error) {
      const box = el("div", "pr-error");
      box.appendChild(el("strong", null, "데몬에 닿지 못했습니다. "));
      box.appendChild(el("span", null, error));
      host.appendChild(box);
      return;
    }
    const model = buildModel(payload, { project, status });
    if (!model.total) {
      host.appendChild(el("div", "pr-empty",
        "이 조건에 기록된 PR이 없습니다. 세션의 PR 위저드나 improv-worker-v2의 pr-open이 기록한 PR만 이 목록에 나타나고, claunch 밖에서 만든 PR은 기록이 없어 보이지 않습니다."));
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
  }

  async function load() {
    loading = true;
    render();
    try {
      const res = await fetch("/api/prs", { cache: "no-store" });
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

  return { open, stop, load, render, buildModel, prState, dayOf, VIEW };
})();
