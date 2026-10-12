/* The execution history of a pi-durable run session as a flow diagram
   (claunch-neeya).

   A simulation's run session (kind ``pi-durable``, sim/pi/runner.mjs) logs
   every step of its campaign loop to ``durable.events.jsonl``: the start,
   each question it asks B or C (one durable task each), what the daemon did
   with the answer, every round, the end. The session's ⬒ run pane has no
   workflow run to show, so it shows this instead -- read from
   ``GET /api/sessions/<name>/durable`` (daemon/sim_api.py).

   Layout: one column per phase in the order the loop entered it (start,
   formalize, judge, round N, ..., edits, end); a phase entered again later
   gets a new column, so the picture reads left to right as the run went.
   Within a column the nodes stack in time order. The log is append-only and
   a result event only ever completes a node already drawn, so a node's
   place never changes once drawn: an update adds the new nodes and edges to
   the SVG that is already on screen and rewrites the text and class of the
   nodes whose state changed. Nothing is redrawn wholesale -- a hover, the
   scroll position and the pane's size survive every poll.

   ``model(events, live, progress)`` is the pure half (tests/web/durableflow_check.js);
   ``mount(host, session)`` owns the poll and the SVG. */
(function () {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const W = 168, H = 46, GAP_X = 40, GAP_Y = 12, PAD = 14, HEAD = 22;
  const POLL_MS = 2000;

  const PHASE_TITLE = {
    start: "시작", formalize: "형식화", requote: "재인용", judge: "판정",
    answers: "답 반영", edits: "편집 제안", end: "끝", other: "기타",
  };

  function phaseTitle(phase) {
    if (phase.startsWith("round:")) return "라운드 " + phase.slice(6);
    return PHASE_TITLE[phase] || phase;
  }

  /* Which phase an ask's key belongs to: the keys are built in
     sim/pi/campaign.mjs (`formalize:<a>`, `judge:<items>:r<n>`,
     `triage:r<n>:<place>`, `gap:r<n>:<place>:<a>`, `edit:<keys>:<a>`, ...). */
  function keyPhase(key) {
    const k = String(key || "");
    const head = k.split(":", 1)[0];
    if (head === "triage" || head === "gap") {
      const m = /^(?:triage|gap):r(\d+):/.exec(k);
      return m ? "round:" + m[1] : "other";
    }
    if (head === "edit") return "edits";
    if (["formalize", "requote", "judge", "answers"].includes(head)) return head;
    return "other";
  }

  function lastPart(key) {
    const parts = String(key).split(":");
    return parts[parts.length - 1];
  }

  function askLabel(key) {
    const k = String(key || "");
    const head = k.split(":", 1)[0];
    let m;
    if (head === "formalize") return "형식화 #" + lastPart(k);
    if (head === "requote") return "재인용 #" + lastPart(k);
    if (head === "answers") return "답 반영 #" + lastPart(k);
    if (head === "edit") return "편집 제안 #" + lastPart(k);
    if (head === "judge") {
      const items = k.slice(6).replace(/:r\d+$/, "");
      return "판정 · 규칙 " + (items ? items.split(",").length : 0) + "개";
    }
    if ((m = /^triage:r\d+:(.*)$/.exec(k))) return "분류 " + m[1];
    if ((m = /^gap:r\d+:(.*):(\d+)$/.exec(k))) return "빈칸 " + m[1] + " #" + m[2];
    return k || "ask";
  }

  function tokens(n) {
    const v = Number(n) || 0;
    return v >= 1000 ? (v / 1000).toFixed(1) + "k" : String(v);
  }

  function clock(at) {
    const d = new Date(at);
    return isNaN(d) ? "" : d.toTimeString().slice(0, 8);
  }

  function modelName(m) {
    const id = String((m && m.modelId) || "");
    return id.split("/").pop() || "?";
  }

  /* The diagram for an event list. Deterministic and append-stable: the
     nodes for a prefix of the log are a prefix of the nodes for the whole
     log, at the same (col, row), with the same id. */
  function model(events, live, progress) {
    const cols = [];        // {phase, title, nodes: [id]}
    const nodes = [];       // {id, col, row, label, sub, status, tip, key}
    const byId = new Map();
    const seen = new Map(); // ask key -> times drawn (a resumed run asks again)
    let tokensTotal = 0, ended = null, first = null, lastAt = null;

    function column(phase) {
      const last = cols[cols.length - 1];
      if (last && last.phase === phase) return cols.length - 1;
      cols.push({ phase, title: phaseTitle(phase), nodes: [] });
      return cols.length - 1;
    }
    function add(phase, base, fields) {
      const c = column(phase);
      let id = base, n = 1;
      while (byId.has(id)) id = base + "#" + (++n);
      const node = { id, col: c, row: cols[c].nodes.length, label: "", sub: "",
                     status: "done", tip: [], key: null, ...fields };
      cols[c].nodes.push(id);
      nodes.push(node);
      byId.set(id, node);
      return node;
    }
    // The newest node drawn for an ask key (or the newest whose key matches).
    function find(test) {
      for (let i = nodes.length - 1; i >= 0; i--) {
        if (nodes[i].key !== null && test(nodes[i].key)) return nodes[i];
      }
      return null;
    }
    function settle(node, ok, note, phase, label) {
      if (!node) {
        node = add(phase, "result:" + label, { label });
      }
      node.status = ok === true ? "ok" : ok === false ? "fail" : ok;
      if (note) node.sub = node.sub ? node.sub + " · " + note : note;
      return node;
    }

    for (const ev of events || []) {
      if (!ev || typeof ev !== "object") continue;
      const at = ev.at || null;
      if (at && !first) first = at;
      if (at) lastAt = at;
      const when = at ? clock(at) : "";
      switch (ev.event) {
        case "session_start":
          add("start", "start", {
            label: nodes.some((x) => x.id.startsWith("start")) ? "재개" : "시작",
            sub: ev.reason || "", tip: [when, "session_start " + (ev.reason || "")],
          });
          break;
        case "harness_open": {
          const host = [...nodes].reverse().find((x) => x.id.startsWith("start"));
          const sub = "B " + modelName(ev.b) + " · C " + modelName(ev.c);
          if (host) { host.sub = sub; host.tip.push("root " + (ev.root || "")); }
          else add("start", "start", { label: "시작", sub, tip: [when] });
          break;
        }
        case "ask": {
          const key = String(ev.key || "");
          const u = ev.usage || {};
          tokensTotal += Number(u.totalTokens) || 0;
          const times = (seen.get(key) || 0) + 1;
          seen.set(key, times);
          add(keyPhase(key), "ask:" + key, {
            key, label: askLabel(key),
            sub: (ev.role || "?") + " · " + tokens(u.totalTokens) + " tok",
            status: ev.stop && ev.stop !== "stop" ? "warn" : "done",
            tip: [when, key, "role " + (ev.role || "?"),
                  `in ${u.input || 0} · cache ${u.cacheRead || 0} · out ${u.output || 0} · gen ${u.generations || 0}`,
                  "stop " + (ev.stop || "?")],
          });
          break;
        }
        case "formalize":
          settle(find((k) => k === "formalize:" + ev.attempt), !!ev.ok,
                 ev.rejected ? "거절 " + ev.rejected : "", "formalize", "형식화 #" + ev.attempt);
          break;
        case "requote":
        case "answers": {
          const head = ev.event + ":";
          settle(find((k) => k.startsWith(head) && lastPart(k) === String(ev.attempt)), !!ev.ok, "",
                 ev.event, askLabel(head + ev.attempt));
          break;
        }
        case "judge":
          settle(find((k) => k === ev.key), "ok",
                 "확인 " + (ev.done || 0) + ((ev.retracted || []).length ? " · 철회 " + ev.retracted.length : ""),
                 keyPhase(ev.key), askLabel(ev.key));
          break;
        case "triage":
          settle(find((k) => k === ev.tkey), "ok", "분류 " + (ev.done || 0), keyPhase(ev.tkey), askLabel(ev.tkey));
          break;
        case "gap_patch": {
          const key = ev.base + ":" + ev.attempt;
          settle(find((k) => k === key), !!ev.ok, ev.ok ? "패치" : "패치 거절", keyPhase(key), askLabel(key));
          break;
        }
        case "edits":
          settle(find((k) => k.startsWith("edit:") && lastPart(k) === String(ev.attempt)),
                 ev.errors ? "warn" : "ok", "제안 " + (ev.done || 0) + (ev.errors ? " · 오류 " + ev.errors : ""),
                 "edits", "편집 제안 #" + ev.attempt);
          break;
        case "judge_unreadable":
        case "triage_unreadable":
        case "gap_unreadable":
        case "edit_unreadable": {
          const key = ev.key || ev.tkey || ev.base || "";
          const node = find((k) => k === key || (key && k.startsWith(key + ":")) ||
                                   (!key && k.startsWith("edit:")));
          settle(node, false, "답 해석 실패", keyPhase(key || "edit:"), ev.event);
          if (node) node.tip.push(String(ev.error || ""));
          break;
        }
        case "round":
          add("round:" + ev.round, "round:" + ev.round, {
            label: "라운드 " + ev.round, cls: "round",
            sub: "열린 발견 " + (ev.open ?? "?") + (ev.ended ? " · " + ev.ended.state : ""),
            status: ev.ended ? "ok" : "done",
            tip: [when, ev.ended ? "ended: " + (ev.ended.reason || ev.ended.state) : ""],
          });
          break;
        case "done":
          ended = { failed: !!ev.failed, reason: ev.reason || "" };
          add("end", "done", { label: ev.failed ? "실패" : "끝", sub: String(ev.reason || "").slice(0, 40),
                               status: ev.failed ? "fail" : "ok", tip: [when, String(ev.reason || "")] });
          break;
        case "done_failed":
          settle(byId.get("done") || null, "warn", "데몬 보고 실패", "end", "끝");
          break;
        case "error":
          ended = ended || { failed: true, reason: String(ev.error || "").split("\n")[0] };
          add("end", "error", { label: "오류", sub: String(ev.error || "").split("\n")[0].slice(0, 40),
                                status: "fail", tip: [when, String(ev.error || "").slice(0, 400)] });
          break;
        case "interrupted":
          add("end", "interrupted", { label: "중단", sub: String(ev.error || "").slice(0, 40), status: "warn",
                                      tip: [when, String(ev.error || "")] });
          break;
        case "session_shutdown": {
          const tail = nodes[nodes.length - 1];
          if (tail && cols[tail.col].phase === "end") {
            tail.tip.push(when + " session_shutdown " + (ev.reason || ""));
          } else {
            add("end", "shutdown", { label: "종료", sub: ev.reason || "", status: "done", tip: [when] });
          }
          break;
        }
        default:
          add(cols.length ? cols[cols.length - 1].phase : "other", "ev:" + ev.event,
              { label: String(ev.event || "?"), tip: [when, JSON.stringify(ev).slice(0, 300)] });
      }
    }
    // Still running: a dashed node after the last one, where the next
    // question will land. Its place moves; its id does not.
    let pending = null;
    if (live && !ended) {
      const c = Math.max(0, cols.length - 1);
      pending = { id: "live", col: c, row: cols.length ? cols[c].nodes.length : 0,
                  label: "진행 중" + (progress && progress.stage ? " · " + progress.stage : ""),
                  sub: (progress && progress.text) || "", status: "live", tip: [] };
    }
    const edges = [];
    for (let i = 1; i < nodes.length; i++) {
      edges.push({ id: nodes[i - 1].id + ">" + nodes[i].id, from: nodes[i - 1], to: nodes[i] });
    }
    if (pending && nodes.length) {
      edges.push({ id: "live-edge", from: nodes[nodes.length - 1], to: pending });
    }
    return { cols, nodes, edges, pending, ended, tokens: tokensTotal, first, lastAt, count: (events || []).length };
  }

  function pos(n) {
    return { x: PAD + n.col * (W + GAP_X), y: PAD + HEAD + n.row * (H + GAP_Y) };
  }

  // Same column: straight down. Next column: out of the right side, in at
  // the left side of the target, with an elbow halfway.
  function edgePath(a, b) {
    const p = pos(a), q = pos(b);
    if (a.col === b.col) {
      const x = p.x + W / 2;
      return `M${x},${p.y + H} L${x},${q.y}`;
    }
    const x1 = p.x + W, y1 = p.y + H / 2, x2 = q.x, y2 = q.y + H / 2, mx = x1 + GAP_X / 2;
    return `M${x1},${y1} L${mx},${y1} L${mx},${y2} L${x2},${y2}`;
  }

  function clip(s, n) {
    s = String(s || "");
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  /* The view: one SVG, kept. ``draw(m)`` brings it to model ``m`` by adding
     what is new and rewriting what changed; it never empties the SVG. */
  function makeView(doc, host) {
    const svg = doc.createElementNS(NS, "svg");
    svg.setAttribute("class", "dfl");
    svg.setAttribute("xmlns", NS);
    const gHeads = doc.createElementNS(NS, "g");
    const gEdges = doc.createElementNS(NS, "g");
    const gNodes = doc.createElementNS(NS, "g");
    svg.appendChild(gHeads); svg.appendChild(gEdges); svg.appendChild(gNodes);
    host.appendChild(svg);
    const heads = new Map(), edges = new Map(), nodes = new Map();

    function mk(tag, parent, attrs) {
      const e = doc.createElementNS(NS, tag);
      for (const k in attrs) e.setAttribute(k, attrs[k]);
      parent.appendChild(e);
      return e;
    }
    function set(e, k, v) {
      v = String(v);
      if (e.getAttribute(k) !== v) e.setAttribute(k, v);
    }
    function text(e, v) {
      v = String(v);
      if (e.textContent !== v) e.textContent = v;
    }

    function drawNode(n) {
      let v = nodes.get(n.id);
      const p = pos(n);
      if (!v) {
        const g = mk("g", gNodes, {});
        const tip = mk("title", g, {});
        const rect = mk("rect", g, { width: W, height: H, rx: 6 });
        const title = mk("text", g, { x: 10, y: 18, class: "dfl-title" });
        const sub = mk("text", g, { x: 10, y: 35, class: "dfl-sub" });
        v = { g, tip, rect, title, sub };
        nodes.set(n.id, v);
        g.setAttribute("data-id", n.id);
      }
      set(v.g, "class", "dfl-node st-" + n.status + (n.cls ? " " + n.cls : ""));
      set(v.g, "transform", `translate(${p.x},${p.y})`);
      text(v.title, clip(n.label, 24));
      text(v.sub, clip(n.sub, 30));
      text(v.tip, [n.label, n.sub, ...(n.tip || [])].filter(Boolean).join("\n"));
    }

    function draw(m) {
      m.cols.forEach((c, i) => {
        let t = heads.get(i);
        if (!t) {
          t = mk("text", gHeads, { class: "dfl-head", x: PAD + i * (W + GAP_X), y: PAD + 12 });
          heads.set(i, t);
        }
        text(t, c.title);
      });
      for (const e of m.edges) {
        let path = edges.get(e.id);
        if (!path) {
          path = mk("path", gEdges, { class: "dfl-edge" + (e.id === "live-edge" ? " live" : "") });
          edges.set(e.id, path);
        }
        set(path, "d", edgePath(e.from, e.to));
      }
      for (const n of m.nodes) drawNode(n);
      if (m.pending) drawNode(m.pending);
      // The pending node and its edge are the only things that ever go away.
      if (!m.pending || !m.nodes.length) {
        const pe = edges.get("live-edge");
        if (pe) { pe.remove(); edges.delete("live-edge"); }
      }
      if (!m.pending) {
        const pn = nodes.get("live");
        if (pn) { pn.g.remove(); nodes.delete("live"); }
      }
      let rows = 1;
      for (const c of m.cols) rows = Math.max(rows, c.nodes.length);
      if (m.pending) rows = Math.max(rows, m.pending.row + 1);
      const w = PAD * 2 + Math.max(1, m.cols.length) * (W + GAP_X) - GAP_X;
      const h = PAD * 2 + HEAD + rows * (H + GAP_Y) - GAP_Y;
      set(svg, "viewBox", `0 0 ${w} ${h}`);
      set(svg, "width", w);
      set(svg, "height", h);
    }

    return { svg, draw, nodes, edges };
  }

  function elapsed(a, b) {
    const s = Math.max(0, Math.round((new Date(b) - new Date(a)) / 1000));
    if (!isFinite(s)) return "";
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }

  function summary(m, data) {
    const parts = [data.sim || "", data.live ? (m.ended ? "끝남" : "실행 중") : "세션 종료"];
    parts.push("사건 " + m.count, "질문 " + m.nodes.filter((n) => n.key !== null).length,
               "토큰 " + tokens(m.tokens));
    if (m.first && m.lastAt) parts.push("경과 " + elapsed(m.first, m.lastAt));
    return parts.filter(Boolean).join(" · ");
  }

  /* Mount on ``host`` for run session ``session``: polls the tail of the
     log and redraws in place. ``opts.api`` is the page's authenticated
     fetch; ``opts.doc`` stands in for ``document`` in tests. */
  function mount(host, session, opts = {}) {
    const doc = opts.doc || globalThis.document;
    const api = opts.api || globalThis.api;
    const interval = opts.interval ?? POLL_MS;
    const timers = opts.timers || globalThis;
    while (host.firstChild) host.removeChild(host.firstChild);
    const head = doc.createElement("div");
    head.className = "wf-head dfl-headline";
    const h2 = doc.createElement("h2");
    h2.textContent = "실행 기록";
    const meta = doc.createElement("span");
    meta.className = "dfl-meta";
    head.appendChild(h2); head.appendChild(meta);
    const note = doc.createElement("p");
    note.className = "wf-note";
    note.textContent = "loading…";
    const scroller = doc.createElement("div");
    scroller.className = "dfl-scroll";
    host.appendChild(head); host.appendChild(note); host.appendChild(scroller);

    let view = null, events = [], gone = false, busy = false, timer = null;

    async function refresh() {
      if (gone || busy) return;
      if (doc.hidden) return;
      busy = true;
      try {
        const resp = await api(`/api/sessions/${encodeURIComponent(session)}/durable?since=${events.length}`);
        const data = await resp.json().catch(() => ({}));
        if (gone) return;
        if (!resp.ok) { note.textContent = data.error || "cannot load the run's events"; return; }
        if (data.total < events.length) {
          // The log shrank (a new simulation under the same name): start over.
          events = [];
          if (view) { view.svg.remove(); view = null; }
          busy = false;
          return refresh();
        }
        if (data.since === events.length) events = events.concat(data.events || []);
        const m = model(events, !!data.live, data.progress);
        if (!events.length) {
          note.textContent = data.live ? "아직 기록된 사건이 없다 — 실행 세션이 시작하는 중" : "기록된 사건이 없다";
        } else {
          note.textContent = data.live && data.progress && !m.ended
            ? "진행: " + [data.progress.stage, data.progress.text].filter(Boolean).join(" — ")
            : "";
        }
        if (!view) view = makeView(doc, scroller);
        const atEnd = scroller.scrollLeft + scroller.clientWidth >= scroller.scrollWidth - 8;
        const cols = view.svg.getAttribute("width");
        view.draw(m);
        // Follow the run while the reader is at its end.
        if (atEnd && cols !== view.svg.getAttribute("width")) scroller.scrollLeft = scroller.scrollWidth;
        meta.textContent = summary(m, data);
        if (!data.live && timer) { timers.clearInterval(timer); timer = null; }
      } catch {
        /* the poll comes back round */
      } finally {
        busy = false;
      }
    }

    if (interval) timer = timers.setInterval(refresh, interval);
    refresh();
    return {
      refresh,
      unmount() { gone = true; if (timer) timers.clearInterval(timer); timer = null; },
      get view() { return view; },
      get events() { return events; },
    };
  }

  globalThis.DurableFlow = { model, mount, makeView, keyPhase, askLabel };
})();
