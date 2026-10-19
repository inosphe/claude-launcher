/* The execution history of a pi-durable session as a flow diagram
   (claunch-neeya, claunch-neeya.1).

   Two producers log a run as JSONL events, both read from
   ``GET /api/sessions/<name>/durable`` (daemon/sim_api.py):
   - a simulation's run session (sim/pi/runner.mjs) logs every step of its
     campaign loop to ``durable.events.jsonl``: the start, each question it
     asks B or C (one durable task each), what the daemon did with the
     answer, every round, the end;
   - gds6's sim2 runner (env ``SIM_RUN_DIR``) logs its start, every question
     (``ask`` with ``askRole``) and ``run_end`` to ``<run>.events.jsonl``.
   The terminal's ◇ chart pane shows it; ⬒ run stays the workflow run.

   Layout: one column per phase in the order the loop entered it (start,
   formalize, judge, round N, ..., edits, end); a phase entered again later
   gets a new column, so the picture reads left to right as the run went.
   sim2 questions are grouped per stage into one counted node, and a stage
   keeps the column it opened (its questions interleave). Within a column
   the nodes stack in time order. The log is append-only and a later event
   only ever completes or counts into a node already drawn, so a node's
   place never changes once drawn: an update adds the new nodes and edges to
   the SVG that is already on screen and rewrites the text and class of the
   nodes whose state changed. Nothing is redrawn wholesale -- a hover, the
   scroll position and the pane's size survive every poll.

   Parallel work (claunch-qk2nc). A producer that asks many questions at
   once says so with two optional events; a log without them draws as above.
   - ``{"event":"ask_start","at","key","askRole"?,"unit"?}`` -- the moment a
     question really went out (past the producer's concurrency semaphore).
     It ends at the ``ask`` with the same key, which may also carry
     ``"unit"?``: the id of the unit the question belongs to.
   - A question that throws (cost cap, stop, harness error) still logs its
     ``ask`` -- without usage, with ``"error":"<one line>"`` -- so the pair
     closes. Its node turns warn and shows the error in its tip.
   - ``{"event":"unit","at","unit":"<id>","state":"start"|"done"|"failed"|
     "skipped"|"interrupted","label"?,"phase"?,"after"?:["<unit id>"...],
     "note"?}`` -- a unit of work. Its start carries the label, a phase (a
     hint for the column's name) and ``after``: the units it actually waited
     for. A later event may carry the state alone. Units not linked through
     ``after`` may have run at the same time.
   Drawing rules:
   - A unit is one node (its questions, tokens, questions in flight, state)
     in the column of its depth: the longest ``after`` chain behind it, fixed
     at its start. Its only edges are its ``after`` edges -- no chain edge
     runs into or out of a unit, and a node drawn after a unit is not chained
     to the node drawn before that unit.
   - A question without a unit draws as before. A grouped (sim2) stage node
     is not chained to the stage above it in its column -- they ran side by
     side -- but hangs off the node its column was entered from.
   - Ungrouped questions (a claunch simulation, which asks one at a time)
     keep their chain.
   - The headline counts the questions in flight and the most that were in
     flight at once, from ask_start/ask pairs. An ask_start left without its
     ask counts as closed at its unit's end event, and at run_end, done,
     error, interrupted and session_shutdown (the process died before it
     could log the ask).

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
    model: "모델 구성", triage: "분류",
  };

  function phaseTitle(phase) {
    if (phase.startsWith("round:")) return "라운드 " + phase.slice(6);
    if (phase.startsWith("unit:")) return "단계 " + (Number(phase.slice(5)) + 1);
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

  /* gds6's sim2 runner (a session with env SIM_RUN_DIR) asks one role per
     question and keys it `<role>:<stage>[...]` -- `B:effects|<hash>:0`,
     `C:r1:j`, `A:1:p2:7:0`, `D:cr-000003:0:0`. A run asks a thousand of
     them, mostly in parallel, so they are drawn one node per stage group
     (counted) rather than one per question. */
  const B_STAGE = {
    formalize: "형식화", effects: "효과", available: "가용성", world: "세계",
    interp: "해석", clash: "충돌", invariants: "불변식", start: "시작 상태", screen: "화면",
  };

  function sim2Group(key) {
    const k = String(key || "").split("|", 1)[0];
    let m;
    if ((m = /^B:([^:]+)/.exec(k))) {
      return { phase: "model", group: "B:" + m[1], label: B_STAGE[m[1]] || "기능 " + m[1] };
    }
    if (k === "C:triage" || k.startsWith("C:triage:")) return { phase: "triage", group: "C:triage", label: "분류" };
    if ((m = /^C:r(\d+):j/.exec(k))) return { phase: "round:" + m[1], group: "C:r" + m[1] + ":j", label: "판정" };
    if ((m = /^C:r(\d+):f/.exec(k))) return { phase: "round:" + m[1], group: "C:r" + m[1] + ":f", label: "발견 검토" };
    if ((m = /^A:(\d+):(p\d+)/.exec(k))) {
      return { phase: "round:" + m[1], group: "A:" + m[1] + ":" + m[2], label: "플레이 " + m[2] };
    }
    if ((m = /^D:([^:]+)/.exec(k))) return { phase: "edits", group: "D:" + m[1], label: "변경 " + m[1] };
    return { phase: "other", group: k.split(":", 1)[0] || "?", label: k || "ask" };
  }

  function tokens(n) {
    const v = Number(n) || 0;
    if (v >= 1e6) return (v / 1e6).toFixed(2) + "M";
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
    let tokensTotal = 0, asks = 0, ended = null, first = null, lastAt = null;
    const units = new Map(); // unit id -> its node
    const open = new Map();  // ask key -> [unit id | null] of starts not yet answered
    const keyUnit = new Map(); // ask key -> the unit its last ask_start named
    let running = 0, peak = 0, starts = 0;

    // A phase entered again gets a new column -- except a grouped (sim2)
    // stage, whose questions interleave: it keeps the column it opened.
    function column(phase, reuse) {
      if (reuse) {
        const at = cols.findIndex((c) => c.phase === phase);
        if (at >= 0) return at;
      }
      const last = cols[cols.length - 1];
      if (last && last.phase === phase) return cols.length - 1;
      cols.push({ phase, title: phaseTitle(phase), nodes: [] });
      return cols.length - 1;
    }
    function add(phase, base, fields, reuse) {
      const c = column(phase, reuse);
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
    function tail() {
      return nodes[nodes.length - 1] || null;
    }
    function settle(node, ok, note, phase, label) {
      if (!node) {
        node = add(phase, "result:" + label, { label });
      }
      node.status = ok === true ? "ok" : ok === false ? "fail" : ok;
      if (note) node.sub = node.sub ? node.sub + " · " + note : note;
      return node;
    }
    // A unit's node: placed once, at the depth its start's ``after`` gives.
    function unitNode(id, ev) {
      let node = units.get(id);
      if (node) return node;
      const after = (Array.isArray(ev && ev.after) ? ev.after : []).map(String).filter((a) => units.has(a));
      const depth = after.reduce((d, a) => Math.max(d, units.get(a).depth + 1), 0);
      node = add("unit:" + depth, "unit:" + id, { unit: id, depth, after, label: id, count: 0, tok: 0,
                                                  running: 0, state: "start", status: "run", note: "" }, true);
      units.set(id, node);
      return node;
    }
    // Close the starts left without their ask (all, or one unit's).
    function closeOpen(unit) {
      for (const [key, pend] of open) {
        const keep = [];
        for (const u of pend) {
          if (unit !== undefined && u !== unit) { keep.push(u); continue; }
          running -= 1;
          if (u !== null) { units.get(u).running -= 1; unitSub(units.get(u)); }
        }
        if (keep.length) open.set(key, keep); else open.delete(key);
      }
    }
    const UNIT_STATUS = { start: "run", done: "ok", failed: "fail", skipped: "skip", interrupted: "warn" };
    const UNIT_STATE = { skipped: "건너뜀", interrupted: "중단", failed: "실패" };
    function unitSub(node) {
      node.sub = ["물음 " + node.count, tokens(node.tok) + " tok",
                  node.running ? "진행 " + node.running : "", UNIT_STATE[node.state] || ""]
        .filter(Boolean).join(" · ");
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
        case "harness_open":
        case "run_start": {
          const host = [...nodes].reverse().find((x) => x.id.startsWith("start"));
          const sub = ev.b ? "B " + modelName(ev.b) + " · C " + modelName(ev.c) : "";
          const tip = ev.event === "run_start"
            ? when + " run_start " + [ev.preset, ev.rounds != null ? ev.rounds + " rounds" : ""].filter(Boolean).join(" · ")
            : "root " + (ev.root || ev.file || "");
          if (host) { if (sub) host.sub = sub; host.tip.push(tip); }
          else add("start", "start", { label: "시작", sub, tip: [when, tip] });
          break;
        }
        case "unit": {
          if (ev.unit == null) break;
          const id = String(ev.unit);
          const node = unitNode(id, ev);
          const state = UNIT_STATUS[ev.state] ? ev.state : "start";
          node.state = state;
          node.status = node.cut && state === "done" ? "warn" : UNIT_STATUS[state];
          if (state !== "start") closeOpen(id);
          if (ev.label) node.label = String(ev.label);
          if (ev.note) node.note = String(ev.note);
          if (state === "start" && ev.phase) {
            const c = cols[node.col];
            c.hints = c.hints || [];
            if (!c.hints.includes(String(ev.phase))) {
              c.hints.push(String(ev.phase));
              c.title = c.hints.join(" · ");
            }
          }
          if (state === "start") {
            node.tip = [when + " " + id + (node.after.length ? " ← " + node.after.join(", ") : "")];
          } else {
            node.tip.push(when + " " + state);
          }
          if (ev.note) node.tip.push(String(ev.note));
          unitSub(node);
          break;
        }
        case "ask_start": {
          const key = String(ev.key || "");
          const unit = ev.unit != null ? String(ev.unit) : null;
          if (!open.has(key)) open.set(key, []);
          open.get(key).push(unit);
          if (unit !== null) keyUnit.set(key, unit);
          starts += 1;
          running += 1;
          peak = Math.max(peak, running);
          if (unit !== null) {
            const node = unitNode(unit, null);
            node.running += 1;
            unitSub(node);
          }
          break;
        }
        case "ask": {
          const pend = open.get(String(ev.key || ""));
          let unit = ev.unit != null ? String(ev.unit) : null;
          const failed = ev.error ? String(ev.error).split("\n")[0] : "";
          if (unit === null && keyUnit.has(String(ev.key || ""))) unit = keyUnit.get(String(ev.key || ""));
          if (pend && pend.length) {
            const from = pend.shift();
            if (!pend.length) open.delete(String(ev.key || ""));
            running -= 1;
            if (from !== null) {
              const node = units.get(from);
              node.running -= 1;
              unitSub(node);
            }
            if (unit === null) unit = from;
          }
          if (unit !== null) {
            const node = unitNode(unit, null);
            const u = ev.usage || {};
            tokensTotal += Number(u.totalTokens) || 0;
            asks += 1;
            node.count += 1;
            node.tok += Number(u.totalTokens) || 0;
            if ((ev.stop && ev.stop !== "stop") || failed) {
              node.cut = true;
              if (node.status === "ok") node.status = "warn";
            }
            if (failed) node.tip.push(when + " " + String(ev.key || "") + ": " + failed);
            unitSub(node);
            break;
          }
          if (ev.askRole) {
            // sim2: one node per stage group, counted.
            const g = sim2Group(ev.key);
            const u = ev.usage || {};
            tokensTotal += Number(u.totalTokens) || 0;
            asks += 1;
            let node = byId.get("grp:" + g.group);
            if (!node) {
              node = add(g.phase, "grp:" + g.group, { key: g.group, count: 0, tok: 0, status: "done", grouped: true }, true);
            }
            node.count += 1;
            node.tok += Number(u.totalTokens) || 0;
            node.label = g.label + " ×" + node.count;
            node.sub = ev.askRole + " · " + tokens(node.tok) + " tok";
            if ((ev.stop && ev.stop !== "stop") || failed) node.status = "warn";
            if (failed) node.errors = [...(node.errors || []), when + " " + String(ev.key || "") + ": " + failed];
            node.tip = [when + " 마지막 " + String(ev.key || ""), "stop " + (ev.stop || "?"), ...(node.errors || [])];
            break;
          }
          asks += 1;
          const key = String(ev.key || "");
          const u = ev.usage || {};
          tokensTotal += Number(u.totalTokens) || 0;
          const times = (seen.get(key) || 0) + 1;
          seen.set(key, times);
          add(keyPhase(key), "ask:" + key, {
            key, label: askLabel(key),
            sub: (ev.role || "?") + " · " + tokens(u.totalTokens) + " tok",
            status: (ev.stop && ev.stop !== "stop") || failed ? "warn" : "done",
            tip: [when, key, "role " + (ev.role || "?"),
                  `in ${u.input || 0} · cache ${u.cacheRead || 0} · out ${u.output || 0} · gen ${u.generations || 0}`,
                  "stop " + (ev.stop || "?"), failed].filter(Boolean),
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
          closeOpen();
          ended = { failed: !!ev.failed, reason: ev.reason || "" };
          add("end", "done", { label: ev.failed ? "실패" : "끝", sub: String(ev.reason || "").slice(0, 40),
                               status: ev.failed ? "fail" : "ok", tip: [when, String(ev.reason || "")] });
          break;
        case "done_failed":
          settle(byId.get("done") || null, "warn", "데몬 보고 실패", "end", "끝");
          break;
        case "error":
          closeOpen();
          ended = ended || { failed: true, reason: String(ev.error || "").split("\n")[0] };
          add("end", "error", { label: "오류", sub: String(ev.error || "").split("\n")[0].slice(0, 40),
                                status: "fail", tip: [when, String(ev.error || "").slice(0, 400)] });
          break;
        case "interrupted":
          closeOpen();
          add("end", "interrupted", { label: "중단", sub: String(ev.error || "").slice(0, 40), status: "warn",
                                      tip: [when, String(ev.error || "")] });
          break;
        case "run_end": {
          closeOpen();
          // done, stopped (asked to stop), or anything else (it broke).
          const stopped = ev.state === "stopped" && !ev.error;
          const failed = !stopped && (ev.state !== "done" || !!ev.error);
          ended = { failed, reason: String(ev.error || ev.state || "") };
          let sum = 0;
          for (const r of Object.values(ev.tally || {})) sum += Number(r && r.total) || 0;
          add("end", "run_end", { label: failed ? "실패" : stopped ? "중단" : "끝",
                                  sub: String(ev.state || "") + (sum ? " · " + tokens(sum) + " tok" : ""),
                                  status: failed ? "fail" : stopped ? "warn" : "ok",
                                  tip: [when, ev.error ? String(ev.error).slice(0, 400) : ""] });
          break;
        }
        case "agent_end":
        case "harness_closed": {
          const t = tail();
          if (t) t.tip.push(when + " " + ev.event);
          break;
        }
        case "session_shutdown": {
          closeOpen();
          const tail = nodes[nodes.length - 1];
          if (tail && cols[tail.col].phase === "end") {
            tail.tip.push(when + " session_shutdown " + (ev.reason || ""));
          } else {
            add("end", "shutdown", { label: "종료", sub: ev.reason || "", status: "done", tip: [when] });
          }
          break;
        }
        default: {
          const last = cols.length ? cols[cols.length - 1].phase : "other";
          add(last.startsWith("unit:") ? "other" : last, "ev:" + ev.event,
              { label: String(ev.event || "?"), tip: [when, JSON.stringify(ev).slice(0, 300)] });
        }
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
    // Each node hangs off the one drawn before it -- or, when a grouped stage
    // landed back in an earlier column, off the node above it there. A
    // grouped stage below another in its column hangs off the node the column
    // was entered from instead: the stages ran side by side. A unit hangs off
    // its ``after`` units only, and the chain does not run across units.
    const edges = [];
    const entry = new Map(); // column -> the node its first node hangs off
    let prev = null, crossed = false;
    for (const to of nodes) {
      if (to.unit !== undefined) {
        for (const a of to.after) {
          const from = units.get(a);
          edges.push({ id: from.id + ">" + to.id, from, to });
        }
        crossed = true;
        continue;
      }
      let from = crossed ? null : prev;
      if (from && to.grouped && (to.row || to.col <= from.col)) from = entry.get(to.col) || null;
      else if (from && to.col < from.col) from = to.row ? byId.get(cols[to.col].nodes[to.row - 1]) : null;
      if (from) edges.push({ id: from.id + ">" + to.id, from, to });
      if (!to.row) entry.set(to.col, from);
      prev = to;
      crossed = false;
    }
    if (pending && nodes.length && nodes[nodes.length - 1].unit === undefined) {
      edges.push({ id: "live-edge", from: nodes[nodes.length - 1], to: pending });
    }
    return { cols, nodes, edges, pending, ended, tokens: tokensTotal, asks, first, lastAt,
             count: (events || []).length, running, peak, starts, units: units.size };
  }

  function pos(n) {
    return { x: PAD + n.col * (W + GAP_X), y: PAD + HEAD + n.row * (H + GAP_Y) };
  }

  // Same column: straight down. Next column: out of the right side, in at
  // the left side of the target, with an elbow halfway. Edges into a unit
  // turn at an x set by the source's row, so the buses of two sources that
  // feed the same column do not meet and read as one.
  function edgePath(a, b) {
    const p = pos(a), q = pos(b);
    if (a.col === b.col) {
      const x = p.x + W / 2;
      return `M${x},${p.y + H} L${x},${q.y}`;
    }
    const lane = b.unit !== undefined ? ((a.row % 5) - 2) * 5 : 0;
    const x1 = p.x + W, y1 = p.y + H / 2, x2 = q.x, y2 = q.y + H / 2, mx = x1 + GAP_X / 2 + lane;
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
      if (!m.edges.some((e) => e.id === "live-edge")) {
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
    parts.push("사건 " + m.count, "질문 " + m.asks, "토큰 " + tokens(m.tokens));
    if (m.starts) {
      if (data.live && !m.ended) parts.push("진행 중 " + m.running);
      parts.push("최대 동시 " + m.peak);
    }
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

  globalThis.DurableFlow = { model, mount, makeView, keyPhase, askLabel, sim2Group };
})();
