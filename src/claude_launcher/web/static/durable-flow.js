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
   - ``{"event":"plan","at","units":[{"unit","label"?,"phase"?,"after"?:[...]}...]}``
     (claunch-ry446) -- the units the run means to do, logged before it
     starts them. It may come more than once; the latest one wins.
   Drawing rules:
   - A unit is one node (its questions, tokens, questions in flight, state)
     in the column of its depth: the longest ``after`` chain behind it, fixed
     at its start. Its only edges are its ``after`` edges -- no chain edge
     runs into or out of a unit, and a node drawn after a unit is not chained
     to the node drawn before that unit. Of the ``after`` edges only the
     transitive reduction is drawn (claunch-nn4fx): a unit it also waits for
     through another drawn unit on the list (the root a card's feature
     already waited for) gets a hidden edge, and the tip keeps the whole list. An edge
     that skips columns runs through the gap between rows, not across boxes.
   - A planned unit is drawn before it starts, as a faint dashed node in
     the column of its depth along the plan's ``after`` chain, with its
     ``after`` edges. Its start (or its first question) turns the same node
     into the unit -- same place, same edges. A unit a later plan leaves out
     and that never started is hidden (``status: "gone"``); its row stays
     empty, because nothing that was drawn ever moves. A log without a plan
     draws as before, and the headline counts the plan's units done.
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

   The pane has two views of the same log (claunch-qmwog). The columns above
   place structure: a node's column is the phase and depth it reached. The
   sequence view places time: one lane per unit -- or per question asked
   outside any unit -- with the interval it ran in, and the asks it asked
   drawn inside that interval. Two lanes that overlap are work that ran at
   the same time, which no column can show. A live run's open intervals run
   out to now, and the axis labels clock time.

   Both views export: the pane's SVG and PNG buttons write the view on
   screen as a standalone file. ``svgSource`` inlines the rules that paint
   it, because a downloaded SVG carries no page CSS, and PNG is that same
   file drawn onto a canvas.

   ``model(events, live, progress)`` and ``sequence(events, live, now)`` are
   the pure halves (tests/web/durableflow_check.js); ``mount(host, session)``
   owns the poll, the view switch and the SVG. */
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
    let plan = null;         // unit ids of the latest plan event

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
    // A planned unit that has not started yet becomes the unit itself.
    function wake(node) {
      if (node.state !== "plan" && node.state !== "gone") return;
      node.state = "start";
      node.status = "run";
    }
    // Place the plan's units not drawn yet, each after the units it waits
    // for (the list may name a unit before its ``after``), and hide the
    // planned units the newest plan drops.
    function addPlan(list, when) {
      const want = new Map();
      for (const p of list) {
        if (p && typeof p === "object" && p.unit != null && !want.has(String(p.unit))) want.set(String(p.unit), p);
      }
      plan = [...want.keys()];
      for (const node of units.values()) {
        if (node.state === "plan" && !want.has(node.unit)) node.state = node.status = "gone";
        else if (node.state === "gone" && want.has(node.unit)) { node.state = "plan"; node.status = "plan"; }
      }
      let todo = [...want.values()];
      while (todo.length) {
        const ready = todo.filter((p) => !(Array.isArray(p.after) ? p.after : [])
          .some((a) => want.has(String(a)) && !units.has(String(a)) && String(a) !== String(p.unit)));
        const take = ready.length ? ready : [todo[0]];   // a cycle: place the first, drop what is not there
        for (const p of take) {
          const id = String(p.unit);
          if (!units.has(id)) {
            const node = unitNode(id, p);
            node.state = node.status = "plan";
            node.tip = [when + " 계획 " + id + (node.after.length ? " ← " + node.after.join(", ") : "")];
          }
          const node = units.get(id);
          if (node.state === "plan") {
            if (p.label) node.label = String(p.label);
            if (p.phase) hint(node, p.phase);
            node.sub = "대기";
          }
        }
        todo = todo.filter((p) => !take.includes(p));
      }
    }
    function hint(node, phase) {
      const c = cols[node.col];
      c.hints = c.hints || [];
      if (!c.hints.includes(String(phase))) {
        c.hints.push(String(phase));
        c.title = c.hints.join(" · ");
      }
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
          wake(node);
          const state = UNIT_STATUS[ev.state] ? ev.state : "start";
          node.state = state;
          node.status = node.cut && state === "done" ? "warn" : UNIT_STATUS[state];
          if (state !== "start") closeOpen(id);
          if (ev.label) node.label = String(ev.label);
          if (ev.note) node.note = String(ev.note);
          if (state === "start" && ev.phase) hint(node, ev.phase);
          if (state === "start") {
            node.tip = [...node.tip.filter((t) => / 계획 /.test(t)),when + " " + id + (node.after.length ? " ← " + node.after.join(", ") : "")];
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
            wake(node);
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
            wake(node);
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
        case "plan":
          if (Array.isArray(ev.units)) addPlan(ev.units, when);
          break;
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
    // A unit draws the transitive reduction of its ``after``: a unit it also
    // waits for through another unit it waits for gets no visible edge of its
    // own (its tip keeps the whole list). Only a path through units still
    // drawn reduces an edge -- a hidden (gone) unit can come back, so the
    // reduced edge stays in the model, flagged, and keeps its id.
    const reach = new Map(); // unit node -> the drawn unit ids behind it
    function behind(node) {
      let set = reach.get(node);
      if (set) return set;
      set = new Set();
      reach.set(node, set);
      for (const a of node.after) {
        const u = units.get(a);
        if (u.state === "gone") continue;
        set.add(a);
        for (const x of behind(u)) set.add(x);
      }
      return set;
    }
    let prev = null, crossed = false;
    for (const to of nodes) {
      if (to.unit !== undefined) {
        for (const a of to.after) {
          const from = units.get(a);
          const plan = from.state === "plan" || to.state === "plan";
          const gone = from.state === "gone" || to.state === "gone";
          const reduced = to.after.some((b) => b !== a && units.get(b).state !== "gone" && behind(units.get(b)).has(a));
          edges.push({ id: from.id + ">" + to.id, from, to, plan, gone, reduced });
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
    // done/total over the latest plan's units.
    const planned = plan && { total: plan.length, done: plan.filter((id) => units.get(id).state === "done").length };
    return { cols, nodes, edges, pending, ended, tokens: tokensTotal, asks, first, lastAt,
             count: (events || []).length, running, peak, starts, units: units.size, plan: planned };
  }

  /* The state a unit's last event names, as a class the view paints. */
  const SEQ_STATUS = { start: "run", done: "ok", failed: "fail",
                       skipped: "skip", interrupted: "warn" };
  /* Lanes drawn at most. A sim asks thousands of questions and a lane each
     would be a wall; the pane is read from its newest end, so the oldest
     lanes are counted (`hidden`) and not drawn. */
  const SEQ_MAX = 240;

  /* The run on a time axis (claunch-qmwog). A lane is a unit's interval --
     start at its first `unit` or `ask_start`, end at its done/failed/... --
     or, for a question asked outside any unit, that question's own interval.
     The asks a unit asked hang under it with their `ask_start`..`ask`
     interval, so a question still in flight is visible inside its unit.
     A lane with no end yet is open: the view draws it out to `now`.
     Missing timestamps leave a null edge; the view clamps those to the
     axis's own ends rather than dropping the lane. */
  function sequence(events, live, now) {
    now = Number.isFinite(now) ? now : Date.now();
    const atOf = (ev) => {
      const t = Date.parse((ev && ev.at) || "");
      return Number.isFinite(t) ? t : null;
    };
    const lanes = [], byUnit = new Map(), byGroup = new Map(), open = new Map();
    let t0 = null, t1 = null, no = 0, asks = 0, endedAt = null;

    const touch = (t) => {
      if (t === null) return;
      if (t0 === null || t < t0) t0 = t;
      if (t1 === null || t > t1) t1 = t;
    };
    function addLane(id, start, fields) {
      const lane = { id, start, end: null, state: "run", asks: [], order: no++, ...fields };
      lanes.push(lane);
      touch(start);
      return lane;
    }
    function unitLane(id, ev) {
      let lane = byUnit.get(id);
      if (!lane) {
        lane = addLane("u:" + id, ev ? atOf(ev) : null, {
          unit: id, label: String((ev && ev.label) || id),
          phase: String((ev && ev.phase) || ""),
        });
        byUnit.set(id, lane);
      }
      return lane;
    }
    function closeAsk(key, unit, start, end, status) {
      const lane = unit !== null ? unitLane(unit, null)
        : addLane("a:" + key + "#" + (++asks), start, { label: askLabel(key), key });
      // A unit first named by its question starts no earlier than that question.
      if (lane.unit !== undefined && lane.start === null && start !== null) lane.start = start;
      lane.asks.push({ key, start, end, status });
      if (lane.unit === undefined) {          // a question outside any unit
        lane.start = start; lane.end = end; lane.state = status;
      }
      touch(start); touch(end);
      return lane;
    }

    for (const ev of events || []) {
      if (!ev || typeof ev !== "object") continue;
      const when = atOf(ev);
      touch(when);
      switch (ev.event) {
        case "unit": {
          if (ev.unit == null) break;
          const lane = unitLane(String(ev.unit), ev);
          if (ev.label) lane.label = String(ev.label);
          if (ev.phase) lane.phase = String(ev.phase);
          const state = ev.state || "start";
          lane.state = SEQ_STATUS[state] || "run";
          if (state === "start") { if (lane.start === null) lane.start = when; }
          else lane.end = when;
          break;
        }
        case "ask_start": {
          const key = String(ev.key || "");
          const unit = ev.unit != null ? String(ev.unit) : null;
          if (!open.has(key)) open.set(key, []);
          open.get(key).push({ unit, start: when });
          if (unit !== null) unitLane(unit, ev);
          break;
        }
        case "ask": {
          const key = String(ev.key || "");
          const unit = ev.unit != null ? String(ev.unit) : null;
          const status = ev.error ? "fail"
            : (ev.stop && ev.stop !== "stop") ? "warn" : "ok";
          const pend = open.get(key);
          const from = pend && pend.length ? pend.shift() : null;
          if (pend && !pend.length) open.delete(key);
          const start = from ? from.start : when;
          if (unit === null && ev.askRole) {
            // sim2 asks a thousand questions, one role each: the tree draws
            // one counted node per stage, and the timeline draws one lane per
            // stage with every question as a mark inside it.
            const g = sim2Group(key);
            let lane = byGroup.get(g.group);
            if (!lane) {
              lane = addLane("g:" + g.group, start,
                             { label: g.label, group: g.group, state: "ok" });
              byGroup.set(g.group, lane);
            }
            if (lane.start === null || (start !== null && start < lane.start)) lane.start = start;
            lane.end = when;
            lane.asks.push({ key, start, end: when, status });
            if (status !== "ok") lane.state = status;
            break;
          }
          closeAsk(key, unit !== null ? unit : (from ? from.unit : null),
                   start, when, status);
          break;
        }
        case "done":
        case "error":
        case "interrupted":
        case "run_end":
        case "session_shutdown":
          if (when !== null) endedAt = when;
          break;
        default: break;
      }
    }
    // A run that ended closes what was still open at its end; a live one
    // leaves it open, and the view draws it out to now.
    if (endedAt !== null) for (const lane of lanes) if (lane.end === null) lane.end = endedAt;
    for (const lane of lanes) {
      for (const a of lane.asks) if (a.end === null) a.end = lane.end;
    }
    lanes.sort((a, b) => (a.start === null ? Infinity : a.start) -
                         (b.start === null ? Infinity : b.start) || a.order - b.order);
    const hidden = Math.max(0, lanes.length - SEQ_MAX);
    const shown = hidden ? lanes.slice(-SEQ_MAX) : lanes;
    // How many questions ran at once, from their own intervals.
    const marks = [];
    for (const lane of shown) {
      for (const a of lane.asks) {
        if (a.start === null) continue;
        marks.push([a.start, 1]);
        marks.push([a.end === null ? (live ? now : t1 ?? a.start) : a.end, -1]);
      }
    }
    marks.sort((x, y) => x[0] - y[0] || x[1] - y[1]);
    let running = 0, peak = 0;
    for (const [, d] of marks) { running += d; if (running > peak) peak = running; }
    const end = live ? now : (t1 === null ? now : t1);
    return {
      lanes: shown, t0, t1: Math.max(end, t0 === null ? end : t0 + 1),
      now: end, live: !!live, peak, hidden, total: lanes.length,
    };
  }

  function pos(n) {
    return { x: PAD + n.col * (W + GAP_X), y: PAD + HEAD + n.row * (H + GAP_Y) };
  }

  // Same column: straight down. Next column: out of the right side, in at
  // the left side of the target, with an elbow halfway. Edges into a unit
  // turn at an x set by the source's row, so the buses of two sources that
  // feed the same column do not meet and read as one. An edge that skips
  // columns does not run across the boxes between: it turns into the gap
  // between the rows beside its target (above it, or below a first-row
  // target), runs along that gap, and turns in through the gap before the
  // target's column.
  function edgePath(a, b) {
    const p = pos(a), q = pos(b);
    if (a.col === b.col) {
      const x = p.x + W / 2;
      return `M${x},${p.y + H} L${x},${q.y}`;
    }
    const lane = b.unit !== undefined ? ((a.row % 5) - 2) * 5 : 0;
    const x1 = p.x + W, y1 = p.y + H / 2, x2 = q.x, y2 = q.y + H / 2, mx = x1 + GAP_X / 2 + lane;
    if (b.col > a.col + 1) {
      const gy = b.row ? q.y - GAP_Y / 2 : q.y + H + GAP_Y / 2, nx = x2 - GAP_X / 2 + lane;
      return `M${x1},${y1} L${mx},${y1} L${mx},${gy} L${nx},${gy} L${nx},${y2} L${x2},${y2}`;
    }
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
          path = mk("path", gEdges, {});
          edges.set(e.id, path);
        }
        set(path, "class", "dfl-edge" + (e.id === "live-edge" ? " live" : "") +
                           (e.gone || e.reduced ? " gone" : e.plan ? " plan" : ""));
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

  /* The time axis's proportions, in the viewBox's own units: a label column
     on the left, a clock axis across the top, one row a lane. */
  const SEQ_W = 960, SEQ_PAD = 12, SEQ_LABEL = 210, SEQ_ROW = 20,
        SEQ_BAR = 9, SEQ_AXIS = 22;

  /* The sequence view: one SVG kept across polls, like the tree's. A lane's
     row is written every draw (its bar grows as the axis does), and a lane
     whose unit is not in the model any more is dropped -- the only lanes
     that ever go away are the ones the cap hides. */
  function makeSequenceView(doc) {
    const svg = doc.createElementNS(NS, "svg");
    svg.setAttribute("class", "dfl dfl-seq");
    svg.setAttribute("xmlns", NS);
    const gAxis = doc.createElementNS(NS, "g");
    const gRows = doc.createElementNS(NS, "g");
    svg.appendChild(gAxis); svg.appendChild(gRows);
    const rows = new Map();

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

    function draw(m) {
      const t0 = m.t0 === null || m.t0 === undefined ? m.t1 - 1 : m.t0;
      const t1 = Math.max(m.t1, t0 + 1);
      const lanes = m.lanes || [];
      const rowsH = Math.max(1, lanes.length) * SEQ_ROW;
      const h = SEQ_PAD * 2 + SEQ_AXIS + rowsH + (m.hidden ? 12 : 0);
      set(svg, "viewBox", `0 0 ${SEQ_W} ${h}`);
      set(svg, "width", SEQ_W);
      set(svg, "height", h);
      const x = (t) => SEQ_LABEL + (SEQ_W - SEQ_LABEL - SEQ_PAD) *
        Math.min(1, Math.max(0, (t - t0) / (t1 - t0)));

      // The axis: the run's span in clock time.
      while (gAxis.firstChild) gAxis.removeChild(gAxis.firstChild);
      for (let i = 0; i <= 5; i++) {
        const at = t0 + ((t1 - t0) * i) / 5;
        const px = x(at);
        mk("line", gAxis, { class: "dfl-seq-tick", x1: px, x2: px,
                            y1: SEQ_AXIS - 6, y2: SEQ_AXIS + rowsH });
        const label = mk("text", gAxis, { class: "dfl-seq-axis",
                                          x: Math.min(px, SEQ_W - 44), y: SEQ_AXIS - 9 });
        label.textContent = clock(at);
      }
      if (m.hidden) {
        const more = mk("text", gAxis, {
          class: "dfl-seq-axis", x: SEQ_PAD, y: h - 4,
        });
        more.textContent = `이전 ${m.hidden}개 단위는 그리지 않는다`;
      }

      const keep = new Set();
      lanes.forEach((lane, i) => {
        keep.add(lane.id);
        let v = rows.get(lane.id);
        if (!v) {
          const g = mk("g", gRows, { class: "dfl-seq-row" });
          g.setAttribute("data-id", lane.id);
          const line = mk("line", g, { class: "dfl-seq-rule", x1: SEQ_LABEL,
                                       x2: SEQ_W - SEQ_PAD, y1: SEQ_ROW - 3, y2: SEQ_ROW - 3 });
          const label = mk("text", g, { class: "dfl-seq-label", x: SEQ_PAD, y: 13 });
          const tip = mk("title", g, {});
          const bar = mk("rect", g, { class: "dfl-seq-bar", rx: 3,
                                      y: (SEQ_ROW - SEQ_BAR) / 2, height: SEQ_BAR });
          v = { g, line, label, tip, bar, asks: [] };
          rows.set(lane.id, v);
        }
        set(v.g, "transform", `translate(0,${SEQ_AXIS + SEQ_PAD + i * SEQ_ROW})`);
        text(v.label, clip(lane.label, 30));
        const start = lane.start === null || lane.start === undefined ? t0 : lane.start;
        const end = lane.end === null || lane.end === undefined ? t1 : lane.end;
        const bx = x(start), bw = Math.max(2, x(end) - bx);
        set(v.bar, "x", bx);
        set(v.bar, "width", bw);
        set(v.bar, "class", `dfl-seq-bar st-${lane.state}` +
                            (lane.end === null || lane.end === undefined ? " live" : ""));
        text(v.tip, [
          lane.label, lane.phase ? `phase ${lane.phase}` : "",
          `시작 ${clock(start)}` + (lane.end === null || lane.end === undefined
            ? " · 진행 중" : ` · 끝 ${clock(end)}`),
          lane.asks.length ? `물음 ${lane.asks.length}` : "",
        ].filter(Boolean).join("\n"));
        lane.asks.forEach((a, j) => {
          let r = v.asks[j];
          if (!r) {
            r = mk("rect", v.g, { class: "dfl-seq-ask", y: SEQ_ROW - 5,
                                  height: 3, rx: 1 });
            v.asks[j] = r;
          }
          const as = a.start === null || a.start === undefined ? start : a.start;
          const ae = a.end === null || a.end === undefined ? end : a.end;
          const ax = x(as);
          set(r, "x", ax);
          set(r, "width", Math.max(1, x(ae) - ax));
          set(r, "class", "dfl-seq-ask" + (a.status === "ok" ? "" : " " + a.status));
        });
        while (v.asks.length > lane.asks.length) v.asks.pop().remove();
      });
      for (const [id, v] of rows) {
        if (keep.has(id)) continue;
        v.g.remove();
        rows.delete(id);
      }
    }

    return { svg, draw, rows };
  }

  /* ---- taking the picture out (claunch-qmwog) -------------------------
     A downloaded SVG carries no page CSS, so the rules that paint both
     views travel inside the file (`svgSource`). PNG is that same file
     drawn onto a canvas, at a size that keeps the 10-12px text legible
     (pngSize). Neither touches the page's own elements. */

  const EXPORT_CSS = [
    ".dfl{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}",
    ".dfl-paper{fill:#0d1117}",
    ".dfl-head{fill:#7d8590;font-size:11px;text-transform:uppercase;letter-spacing:.04em}",
    ".dfl-node rect{fill:#1b1e24;stroke:#30363d;stroke-width:1.5}",
    ".dfl-node.st-ok rect{stroke:#2f6f43}",
    ".dfl-node.st-fail rect{stroke:#b0413e;fill:#2a1617}",
    ".dfl-node.st-warn rect{stroke:#d29922}",
    ".dfl-node.st-run rect{stroke:#58a6ff}",
    ".dfl-node.st-skip{opacity:.55}",
    ".dfl-node.st-plan{opacity:.45}",
    ".dfl-node.st-plan rect{fill:none;stroke:#4d5566;stroke-dasharray:5 4}",
    ".dfl-node.st-gone,.dfl-edge.gone{display:none}",
    ".dfl-node.round rect{fill:#14233a;stroke:#3b6ea8}",
    ".dfl-node.st-live rect{fill:none;stroke:#58a6ff;stroke-dasharray:6 4}",
    ".dfl-title{fill:#d8dbe0;font-size:12px}",
    ".dfl-sub{fill:#8b949e;font-size:10px}",
    ".dfl-edge{fill:none;stroke:#4d5566;stroke-width:1.4}",
    ".dfl-edge.live{stroke:#58a6ff;stroke-dasharray:4 4}",
    ".dfl-edge.plan{stroke-dasharray:4 4;opacity:.45}",
    ".dfl-seq-label{fill:#d8dbe0;font-size:11px}",
    ".dfl-seq-rule{stroke:#21262d;stroke-width:1}",
    ".dfl-seq-tick{stroke:#30363d;stroke-width:1}",
    ".dfl-seq-axis{fill:#7d8590;font-size:10px}",
    ".dfl-seq-bar{fill:#1b1e24;stroke:#30363d}",
    ".dfl-seq-bar.st-run{fill:#12304f;stroke:#58a6ff}",
    ".dfl-seq-bar.st-ok{fill:#12301f;stroke:#2f6f43}",
    ".dfl-seq-bar.st-fail{fill:#2a1617;stroke:#b0413e}",
    ".dfl-seq-bar.st-warn{fill:#2b2413;stroke:#d29922}",
    ".dfl-seq-bar.st-skip{opacity:.55}",
    ".dfl-seq-bar.st-plan{fill:none;stroke:#4d5566;stroke-dasharray:4 4}",
    ".dfl-seq-bar.live{stroke-dasharray:6 4}",
    ".dfl-seq-ask{fill:#58a6ff;opacity:.75}",
    ".dfl-seq-ask.warn{fill:#d29922}",
    ".dfl-seq-ask.fail{fill:#b0413e}",
  ].join("");

  function escXml(s) {
    return String(s).replace(/[&<>"]/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  /* One node as XML. The browser's serializer knows namespaces; when it is
     there it is used, and this walker stands in for it (tests, and any host
     that kept only the element API). Either way the text a `<text>` carries
     is one node's `textContent` -- this view never nests elements in one. */
  function serializeNode(n) {
    if (n === null || n === undefined) return "";
    if (n.nodeType === 3) return escXml(n.nodeValue || "");
    const tag = n.tagName || n.tag || "";
    if (!tag) return escXml(n.textContent || "");
    const attrs = [];
    if (n.attributes && typeof n.attributes.length === "number") {
      for (const a of Array.from(n.attributes)) attrs.push([a.name, a.value]);
    } else if (n.attrs) {
      for (const k in n.attrs) attrs.push([k, n.attrs[k]]);
    }
    let out = "<" + tag;
    for (const [k, v] of attrs) out += ` ${k}="${escXml(v)}"`;
    out += ">";
    if (n.childNodes && typeof n.childNodes.length === "number") {
      for (const c of Array.from(n.childNodes)) out += serializeNode(c);
    } else if (n.children && n.children.length) {
      for (const c of n.children) out += serializeNode(c);
      if (n._text) out += escXml(n._text);
    } else if (n._text) {
      out += escXml(n._text);
    } else if (!n.children && typeof n.textContent === "string" && n.textContent) {
      out += escXml(n.textContent);
    }
    return out + `</${tag}>`;
  }

  /* The view on screen as a file: the same markup, with the styles and a
     paper rect inside it. `width`/`height` come from the view so the paper
     covers the drawing. */
  function svgSource(svg) {
    let raw = "";
    const Serializer = globalThis.XMLSerializer;
    if (typeof Serializer === "function") {
      try { raw = new Serializer().serializeToString(svg); } catch { raw = ""; }
    }
    if (!raw) raw = serializeNode(svg);
    const open = raw.indexOf(">");
    const w = svg.getAttribute("width") || 0;
    const h = svg.getAttribute("height") || 0;
    const inside = `<style>${EXPORT_CSS}</style>` +
      `<rect class="dfl-paper" x="0" y="0" width="${escXml(w)}" height="${escXml(h)}"/>`;
    return open < 0 ? raw : raw.slice(0, open + 1) + inside + raw.slice(open + 1);
  }

  /* The pixel size a PNG is drawn at: the view's own units times `scale`
     (2 by default -- these labels are 10-12px). */
  function pngSize(svg, scale = 2) {
    const vb = /^0 0 ([\d.]+) ([\d.]+)$/.exec(String(svg.getAttribute("viewBox") || ""));
    const w = Number(svg.getAttribute("width")) || (vb ? Number(vb[1]) : 0);
    const h = Number(svg.getAttribute("height")) || (vb ? Number(vb[2]) : 0);
    const k = Number(scale) > 0 ? Number(scale) : 2;
    return { width: Math.max(1, Math.round(w * k)),
             height: Math.max(1, Math.round(h * k)) };
  }

  /* The SVG drawn onto a canvas, handed back as a PNG blob. */
  function pngBlob(svg, opts = {}) {
    const doc = opts.doc || globalThis.document;
    const size = pngSize(svg, opts.scale || 2);
    const text = svgSource(svg);
    const url = URL.createObjectURL(new Blob([text], { type: "image/svg+xml;charset=utf-8" }));
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => {
        try {
          const canvas = doc.createElement("canvas");
          canvas.width = size.width;
          canvas.height = size.height;
          const ctx = canvas.getContext("2d");
          ctx.fillStyle = "#0d1117";
          ctx.fillRect(0, 0, size.width, size.height);
          ctx.drawImage(img, 0, 0, size.width, size.height);
          URL.revokeObjectURL(url);
          canvas.toBlob((b) => (b ? resolve(b)
            : reject(new Error("the canvas produced no PNG"))), "image/png");
        } catch (err) {
          URL.revokeObjectURL(url);
          reject(err);
        }
      };
      img.onerror = () => {
        URL.revokeObjectURL(url);
        reject(new Error("the SVG image did not load"));
      };
      img.src = url;
    });
  }

  /* Save a blob under `name` through the browser's own download path. */
  function saveBlob(blob, name) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }

  function elapsed(a, b) {
    const s = Math.max(0, Math.round((new Date(b) - new Date(a)) / 1000));
    if (!isFinite(s)) return "";
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }

  function summary(m, data) {
    const parts = [data.sim || "", data.live ? (m.ended ? "끝남" : "실행 중") : "세션 종료"];
    if (m.plan) parts.push("단위 " + m.plan.done + "/" + m.plan.total);
    parts.push("사건 " + m.count, "질문 " + m.asks, "토큰 " + tokens(m.tokens));
    if (m.starts) {
      if (data.live && !m.ended) parts.push("진행 중 " + m.running);
      parts.push("최대 동시 " + m.peak);
    }
    if (m.first && m.lastAt) parts.push("경과 " + elapsed(m.first, m.lastAt));
    return parts.filter(Boolean).join(" · ");
  }

  /* Which view the pane opens with. Kept across openings: a reader working
     on the timeline wants the timeline the next time, too. */
  const VIEW_KEY = "claunch_dfl_view";
  function savedMode() {
    try {
      const v = globalThis.localStorage && globalThis.localStorage.getItem(VIEW_KEY);
      return v === "sequence" ? "sequence" : "tree";
    } catch { return "tree"; }
  }
  function rememberMode(mode) {
    try {
      if (globalThis.localStorage) globalThis.localStorage.setItem(VIEW_KEY, mode);
    } catch { /* no storage here: the switch still works, it just forgets */ }
  }

  /* Mount on ``host`` for run session ``session``: polls the tail of the
     log and redraws in place. ``opts.api`` is the page's authenticated
     fetch; ``opts.doc`` stands in for ``document`` in tests.

     The head carries the view switch and the two exports (claunch-qmwog):
     the tree the pane has always drawn, and the sequence view of the same
     log. Only the view on screen sits in the scroller -- the other keeps
     its element (and the tree's append-stable nodes) in a detached holder,
     so switching back is the same SVG, not a rebuilt one. */
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

    // The view switch and the way out with either view.
    const tools = doc.createElement("div");
    tools.className = "dfl-tools";
    const press = (label, title) => {
      const b = doc.createElement("button");
      b.className = "wf-btn sm";
      b.textContent = label;
      b.title = title;
      b.type = "button";
      tools.appendChild(b);
      return b;
    };
    const treeBtn = press("트리", "구조: 위상 열과 unit 간선");
    const seqBtn = press("시퀀스", "시간: x축은 시각, 레인은 unit, 그 아래 물음 구간");
    const svgBtn = press("SVG", "지금 보이는 차트를 SVG 파일로 내려받는다");
    const pngBtn = press("PNG", "지금 보이는 차트를 PNG 파일로 내려받는다");
    head.appendChild(tools);

    const note = doc.createElement("p");
    note.className = "wf-note";
    note.textContent = "loading…";
    const scroller = doc.createElement("div");
    scroller.className = "dfl-scroll";
    host.appendChild(head); host.appendChild(note); host.appendChild(scroller);

    let view = null, seqView = null, events = [], gone = false, busy = false, timer = null;
    let lastTree = null, lastSeq = null, mode = savedMode();
    // Both views are made here and attach to the scroller when it is their
    // turn; a view whose turn it is not keeps its element in this holder.
    const holder = doc.createElement("div");

    const parentOf = (n) => n.parentNode || n.parent || null;
    const shownSvg = () => (mode === "sequence"
      ? (seqView && seqView.svg) : (view && view.svg));

    function paintMode() {
      treeBtn.setAttribute("aria-pressed", String(mode === "tree"));
      seqBtn.setAttribute("aria-pressed", String(mode === "sequence"));
      const want = shownSvg();
      if (!want) return;
      if (parentOf(want) !== scroller) {
        while (scroller.firstChild) scroller.removeChild(scroller.firstChild);
        scroller.appendChild(want);      // moves the node out of the holder
      }
      scroller.className = "dfl-scroll" + (mode === "sequence" ? " dfl-seq" : "");
    }
    function drawShown() {
      if (mode === "sequence") { if (seqView && lastSeq) seqView.draw(lastSeq); }
      else if (view && lastTree) view.draw(lastTree);
    }
    function setMode(next) {
      mode = next === "sequence" ? "sequence" : "tree";
      rememberMode(mode);
      paintMode();
      drawShown();
      scroller.scrollLeft = scroller.scrollWidth;
    }
    treeBtn.addEventListener("click", () => setMode("tree"));
    seqBtn.addEventListener("click", () => setMode("sequence"));
    const fileBase = () => `durable-${session}-${mode}`;
    svgBtn.addEventListener("click", () => {
      const svg = shownSvg();
      if (!svg || !svg.getAttribute("width")) return;   // nothing drawn yet
      saveBlob(new Blob([svgSource(svg)], { type: "image/svg+xml;charset=utf-8" }),
               `${fileBase()}.svg`);
    });
    pngBtn.addEventListener("click", () => {
      const svg = shownSvg();
      if (!svg || !svg.getAttribute("width")) return;   // nothing drawn yet
      pngBlob(svg).then((b) => saveBlob(b, `${fileBase()}.png`))
        .catch((e) => { note.textContent = `PNG를 만들지 못했다: ${(e && e.message) || e}`; });
    });

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
          if (seqView) { seqView.svg.remove(); seqView = null; }
          busy = false;
          return refresh();
        }
        if (data.since === events.length) events = events.concat(data.events || []);
        const m = model(events, !!data.live, data.progress);
        // A run that ended does not grow with its session: only a run still
        // going is drawn out to now.
        const s = sequence(events, !!data.live && !m.ended, Date.now());
        lastTree = m; lastSeq = s;
        if (!events.length) {
          note.textContent = data.live ? "아직 기록된 사건이 없다 — 실행 세션이 시작하는 중" : "기록된 사건이 없다";
        } else {
          note.textContent = data.live && data.progress && !m.ended
            ? "진행: " + [data.progress.stage, data.progress.text].filter(Boolean).join(" — ")
            : "";
        }
        if (!view) view = makeView(doc, holder);
        if (!seqView) seqView = makeSequenceView(doc);
        paintMode();
        const before = shownSvg() ? String(shownSvg().getAttribute("width")) : "";
        const atEnd = scroller.scrollLeft + scroller.clientWidth >= scroller.scrollWidth - 8;
        if (mode === "sequence") seqView.draw(s); else view.draw(m);
        // Follow the run while the reader is at its end.
        const after = shownSvg() ? String(shownSvg().getAttribute("width")) : "";
        if (atEnd && before !== after && (mode !== "sequence" || before !== "")) {
          scroller.scrollLeft = scroller.scrollWidth;
        }
        meta.textContent = summary(m, data) +
          (mode === "sequence" && s.lanes.length
            ? ` · 레인 ${s.lanes.length}` + (s.hidden ? ` (+${s.hidden} 이전)` : "")
            : "");
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
      get sequence() { return seqView; },
      get mode() { return mode; },
      setMode,
      get events() { return events; },
    };
  }

  globalThis.DurableFlow = { model, mount, makeView, keyPhase, askLabel, sim2Group,
                             sequence, makeSequenceView, svgSource, pngSize,
                             serializeNode, EXPORT_CSS, SEQ_MAX };
})();
