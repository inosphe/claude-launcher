// The pure half of the simulation run session (runner.mjs): the campaign
// loop, its prompts, answer parsing and the checkout readers behind the file
// tools. No pi or pi-durable imports, so it is tested without the runtime
// (tests/sim_campaign.test.mjs).

import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";

export const LIMITS = {
  // One request's context, one conversation's input sum, generations per question.
  peak: 200000,
  sum: 1000000,
  generations: 12,
  // Gap batches interpreted per pass, rules per judge question, retries of a rejected answer.
  gaps: 4,
  judgeBatch: 8,
  attempts: 2,
  // Passes in a row without an accepted change before the run gives up.
  idle: 2,
};

// -- checkout readers ------------------------------------------------------------------

const SKIP = new Set([".git", "node_modules", ".venv", "__pycache__", ".beads", "dist", "build"]);
const READ_MAX = 400_000;

export function inside(root, rel) {
  const base = path.resolve(root);
  const p = path.resolve(base, String(rel ?? "."));
  if (p !== base && !p.startsWith(base + path.sep)) throw new Error(`${rel} is outside the simulation root`);
  return p;
}

export function readFileText(root, rel, offset = 0) {
  const p = inside(root, rel);
  const text = readFileSync(p, "utf8");
  const start = Math.max(0, offset | 0);
  const part = text.slice(start, start + READ_MAX);
  const more = start + READ_MAX < text.length ? `\n[... ${text.length - start - READ_MAX} more characters; read again with offset ${start + READ_MAX}]` : "";
  return part + more;
}

export function listFiles(root, rel = ".", depth = 2) {
  const out = [];
  const walk = (dir, d) => {
    let names;
    try { names = readdirSync(dir).sort(); } catch { return; }
    for (const name of names) {
      if (SKIP.has(name) || out.length >= 2000) continue;
      const p = path.join(dir, name);
      let st;
      try { st = statSync(p); } catch { continue; }
      const r = path.relative(root, p);
      if (st.isDirectory()) {
        out.push(r + "/");
        if (d > 1) walk(p, d - 1);
      } else out.push(r);
    }
  };
  walk(inside(root, rel), Math.max(1, Math.min(depth | 0 || 2, 6)));
  return out.join("\n") || "(empty)";
}

export function searchText(root, pattern, rel = ".") {
  const re = new RegExp(pattern, "i");
  const out = [];
  const walk = (dir) => {
    let names;
    try { names = readdirSync(dir); } catch { return; }
    for (const name of names) {
      if (SKIP.has(name) || out.length >= 200) continue;
      const p = path.join(dir, name);
      let st;
      try { st = statSync(p); } catch { continue; }
      if (st.isDirectory()) walk(p);
      else if (st.size < 2_000_000) {
        let lines;
        try { lines = readFileSync(p, "utf8").split("\n"); } catch { continue; }
        lines.forEach((line, i) => { if (out.length < 200 && re.test(line)) out.push(`${path.relative(root, p)}:${i + 1}: ${line.slice(0, 300)}`); });
      }
    }
  };
  walk(inside(root, rel));
  return out.join("\n") || "(no match)";
}

// -- prompts and answers ---------------------------------------------------------------

/** The JSON value an answer ends with: the last ```json fence, else the outermost braces or brackets. */
export function answerJson(text) {
  const fences = [...String(text ?? "").matchAll(/```(?:json)?\s*\n([\s\S]*?)```/g)];
  const candidates = fences.length ? [fences[fences.length - 1][1]] : [];
  const t = String(text ?? "");
  // The value that opens first is the outer one: an array of objects, or an object holding arrays.
  const spans = [["{", "}"], ["[", "]"]].map(([a, b]) => [t.indexOf(a), t.lastIndexOf(b)]).filter(([i, j]) => i >= 0 && j > i);
  spans.sort((x, y) => x[0] - y[0]);
  for (const [i, j] of spans) candidates.push(t.slice(i, j + 1));
  for (const c of candidates) {
    try { return JSON.parse(c); } catch { /* next */ }
  }
  throw new Error("the answer has no JSON value");
}

const brief = (sim) => [
  `Simulation ${sim.id} "${sim.title}".`,
  sim.goal ? `Goal: ${sim.goal}` : "",
  sim.sources?.length ? `Sources (paths under the root): ${sim.sources.join(", ")}` : "Sources: none named; find the documents and code that specify the behaviour (list_files, search_text).",
].filter(Boolean).join("\n");

/** The language of what the user reads (settings.language): questions,
 * options and reasons. JSON keys, ids, paths and quotes stay as they are. */
const language = (sim) => sim.settings?.language || "한국어";
const langRule = (sim, what) => `Write ${what} in ${language(sim)}. Keep JSON keys, ids, file paths and text quoted from the sources exactly as they are.`;

const QUOTE_RULE = "Every rule and invariant either quotes its source word for word -- source: [{ref: <path under the root>, quote: <exact text from that file>}] -- or is marked provisional: {why: <what the sources leave open>}. A quote the daemon cannot find in the file rejects the whole patch.";

export function formalizePrompt(sim, guide, rejected) {
  return [
    "You build a formal world model of a system from its sources, for a deterministic simulator that walks it and reports gaps.",
    brief(sim),
    "Read the sources with your tools (read_file, list_files, search_text) before you write anything.",
    QUOTE_RULE,
    "The model format:",
    guide,
    rejected ? `Your previous model was rejected by the daemon:\n- ${rejected.join("\n- ")}\nFix exactly these problems.` : "",
    "Answer with the model as one JSON patch object (machines, vars, events, rules, invariants, checkpoints) in a ```json fence at the end of your answer.",
  ].filter(Boolean).join("\n\n");
}

export function judgePrompt(sim, items) {
  return [
    "You are the judge. Another model wrote the rules below and quoted a source for each. Check every one against its source file (read it with read_file).",
    brief(sim),
    "For each rule: confirmed -- the quoted text is in the file and says what the rule does; fabricated -- the quote is not there, or it does not support the rule's condition or effect; ambiguous -- the text could be read this way but also another way.",
    "Rules:",
    "```json\n" + JSON.stringify(items.map((r) => ({ section: r.section, id: r.id, on: r.on, when: r.when, do: r.do, check: r.check, source: r.source })), null, 1) + "\n```",
    langRule(sim, "each why"),
    'Answer with a JSON array [{"section": "rules"|"invariants", "id": ..., "verdict": "confirmed"|"fabricated"|"ambiguous", "why": ...}] in a ```json fence at the end.',
  ].join("\n\n");
}

export function gapPrompt(sim, model, batch, guide, rejected) {
  return [
    "You interpret gaps a deterministic simulator found in a formal world model. Each finding comes with a witness: the event sequence that reached it.",
    brief(sim),
    "Current model:",
    "```json\n" + JSON.stringify(model) + "\n```",
    `Findings at ${batch.place}:`,
    "```json\n" + JSON.stringify(batch.findings.map((f) => ({ key: f.key, kind: f.kind, detail: f.detail, machine: f.machine, state: f.state, event: f.event, witness: f.witness?.steps, assumed: f.assumed, provisional: f.provisional, ...(f.triage ? { judge: f.triage } : {}) })), null, 1) + "\n```",
    "For each finding decide, reading the sources with your tools: (a) the sources say what happens -- add or change rules that quote them; (b) it cannot happen or is intended -- dismiss it with a reason; (c) only a person can decide -- ask a question (with options when there are a few clear choices).",
    batch.findings.some((f) => f.triage) ? "A judge has already sorted these findings (the judge field): model -- the model misreads the sources, so fix the rules it names; spec -- the sources say nothing about this, so ask a question unless you find text that settles it, and do not add a provisional rule for it; design -- the path ran through provisional rules and the design lacks a rule here, so add one (quote a source if you find text, else provisional with a why); decide -- the path ran through provisional rules and only a person can choose, so ask a question." : "",
    QUOTE_RULE,
    "The model format:",
    guide,
    rejected ? `Your previous patch was rejected by the daemon:\n- ${rejected.join("\n- ")}\nFix exactly these problems.` : "",
    langRule(sim, "every question, option and dismiss reason (the user reads them)"),
    'Answer with {"patch": <model patch or null>, "resolve": [{"key": ..., "action": "dismiss", "reason": ...} | {"key": ..., "action": "ask", "question": ..., "options": [...]}]} in a ```json fence at the end. Findings your patch handles need no resolve entry: the next walk closes them.',
  ].filter(Boolean).join("\n\n");
}

export function editorPrompt(sim, targets, max, rejected) {
  return [
    "You are the editor. A simulation of the system described by the sources raised questions the sources do not settle, and the user answered some of them. Propose edits to the source files so that the documents themselves settle them.",
    brief(sim),
    "Findings:",
    "```json\n" + JSON.stringify(targets.map((f) => ({ key: f.key, kind: f.kind, detail: f.detail, witness: f.witness?.steps, question: f.question?.text, options: f.question?.options, answer: f.answer?.text })), null, 1) + "\n```",
    "Read the files first (read_file, search_text). For an answered finding, write the user's answer into the document where the surrounding behaviour is described (kind complete). For an open question, write the choice as an explicit open decision with its options, without picking one (kind decide). Use clarify only for rewording that does not change the meaning. Never change a decision the document already records.",
    "old must be text copied word for word from the file (it is replaced once); leave old empty to append to the end of the file. Keep each edit small.",
    `Write new in the language of the file it goes into. ${langRule(sim, "each reason")}`,
    rejected ? `Your previous proposals were rejected by the daemon:\n- ${rejected.join("\n- ")}\nFix exactly these problems.` : "",
    `Answer with a JSON array of at most ${max} items [{"file": <path under the root>, "old": ..., "new": ..., "kind": "clarify"|"complete"|"decide", "reason": ..., "findings": [keys]}] in a \`\`\`json fence at the end.`,
  ].filter(Boolean).join("\n\n");
}

/** Triage verdicts (store.SimStore.TRIAGE / TRIAGE_PROVISIONAL): a finding
 * whose path ran through a provisional rule is sorted as a design question. */
export const TRIAGE = ["model", "spec", "intended"];
export const TRIAGE_PROVISIONAL = ["design", "intended", "decide"];
const triageVerdicts = (batch) => {
  const sets = new Set(batch.findings.flatMap((f) => (f.provisional?.length ? TRIAGE_PROVISIONAL : TRIAGE)));
  return [...sets].map((v) => `"${v}"`).join("|");
};

export function triagePrompt(sim, model, batch) {
  return [
    "You are the judge. A deterministic simulator walked a formal world model built from the sources and found the gaps below. Before anyone answers them, sort each one. Read the sources with your tools.",
    brief(sim),
    "Current model:",
    "```json\n" + JSON.stringify(model) + "\n```",
    `Findings at ${batch.place}:`,
    "```json\n" + JSON.stringify(batch.findings.map((f) => ({ key: f.key, kind: f.kind, detail: f.detail, witness: f.witness?.steps, ...(f.provisional?.length ? { provisional: f.provisional } : {}) })), null, 1) + "\n```",
    "For a finding without a provisional field: model -- the sources do say what happens here and the model misreads or omits it (name the text); spec -- the sources say nothing that settles it, so a person must decide; intended -- it is not a defect (the situation cannot arise, or doing nothing is plainly what the sources mean); give the reason.",
    batch.findings.some((f) => f.provisional?.length) ? "A finding with a provisional field reached its place through those provisional rules, which no source text backs, so there is nothing to misread. Sort it with these verdicts instead: design -- the design lacks a rule here, add one; intended -- an intended exclusion (the situation is left out on purpose or cannot arise); decide -- only a person can choose what happens; give the reason." : "",
    langRule(sim, "each why (the user reads it)"),
    `Answer with a JSON array [{"key": ..., "verdict": ${triageVerdicts(batch)}, "why": ...}] in a \`\`\`json fence at the end.`,
  ].filter(Boolean).join("\n\n");
}

export function requotePrompt(sim, stale, guide, rejected) {
  return [
    "The sources of a formal world model changed since these rules were written: each rule's quote is no longer in its file.",
    brief(sim),
    "Rules that lost their quote:",
    "```json\n" + JSON.stringify(stale, null, 1) + "\n```",
    "Read the files again (read_file, search_text). For each rule: if the sources still say it, re-quote the current text (the same id, the new quote); if they now say something else, rewrite the rule from the new text; if they no longer say it, remove it ({\"remove\": {\"rules\": [ids]}}) or mark it provisional with a why.",
    QUOTE_RULE,
    "The model format:",
    guide,
    rejected ? `Your previous patch was rejected by the daemon:\n- ${rejected.join("\n- ")}\nFix exactly these problems.` : "",
    'Answer with {"patch": <model patch>} in a ```json fence at the end.',
  ].filter(Boolean).join("\n\n");
}

export function answersPrompt(sim, model, answered, guide, rejected) {
  return [
    "The user answered questions raised about gaps in a formal world model. Put each answer into the model.",
    brief(sim),
    "Current model:",
    "```json\n" + JSON.stringify(model) + "\n```",
    "Answers:",
    "```json\n" + JSON.stringify(answered.map((f) => ({ key: f.key, kind: f.kind, detail: f.detail, question: f.question?.text, answer: f.answer?.text })), null, 1) + "\n```",
    'Each rule you write for an answer has source [{"ref": "user:<finding key>", "quote": <the answer text>}].',
    "The model format:",
    guide,
    rejected ? `Your previous patch was rejected by the daemon:\n- ${rejected.join("\n- ")}\nFix exactly these problems.` : "",
    'Answer with {"patch": <model patch>} in a ```json fence at the end.',
  ].filter(Boolean).join("\n\n");
}

// -- the campaign -----------------------------------------------------------------------

/**
 * Drive one campaign to its end. ``ask(role, key, prompt)`` asks B or C and
 * returns the answer text; ``api`` is the daemon client; ``progress`` gets a
 * record for the session's progress display. Returns {reason, failed}.
 */
export async function runCampaign(opts) {
  const res = await walkCampaign(opts);
  if (!res.failed) await proposeEdits(opts);
  return res;
}

/**
 * The editor (D): once the campaign has ended, propose edits to the source
 * files that would settle the questions and record the user's answers --
 * at most max_edits of them, never applied (the user accepts or rejects).
 */
export async function proposeEdits({ simId, session, api, ask, progress = () => {}, limits = LIMITS, log = () => {} }) {
  const work = await api("GET", `/api/sims/${simId}/work?gaps=0&session=${encodeURIComponent(session)}`);
  const max = work.sim.settings.max_edits ?? 0;
  const targets = (work.edit_targets ?? []).slice(0, Math.max(0, max) * 3);
  if (!max || !targets.length) return;
  progress({ stage: "edits", text: `${targets.length} findings` });
  const base = `edit:${targets.map((f) => f.key).sort().join(",")}`;
  let rejected;
  for (let a = 1; a <= limits.attempts; a++) {
    const text = await ask("B", `${base}:${a}`, editorPrompt(work.sim, targets, max, rejected), true);
    let items;
    try { items = answerJson(text); } catch (error) { log("edit_unreadable", { error: error.message }); return; }
    if (!Array.isArray(items)) return;
    const res = await api("POST", `/api/sims/${simId}/proposals`, { session, key: `${base}:${a}`, items: items.slice(0, max) });
    log("edits", { attempt: a, done: res.done?.length ?? 0, errors: res.errors?.length ?? 0 });
    if (res.ok || res.done?.length) return;
    rejected = res.errors;
  }
}

export async function walkCampaign({ simId, session, api, ask, guide, progress = () => {}, limits = LIMITS, log = () => {} }) {
  const q = `session=${encodeURIComponent(session)}`;
  const submit = async (key, patch) => api("POST", `/api/sims/${simId}/model`, { session, patch, key, round: false });
  let idle = 0;
  for (let pass = 1; ; pass++) {
    let work = await api("GET", `/api/sims/${simId}/work?gaps=${limits.gaps}&${q}`);
    const sim = work.sim;
    const r = sim.last_round?.round ?? 0;
    const show = (stage, text) => progress({ stage, done: sim.run_rounds ?? 0, total: sim.settings.max_rounds, text });
    if (sim.state !== "running") return { reason: `campaign ${sim.state}: ${sim.state_reason}` };
    let changed = false;

    // 1. formalize an empty model
    if (!(work.model.rules ?? []).length && !(work.model.machines ?? []).length) {
      show("formalize", "reading the sources");
      let rejected;
      for (let a = 1; a <= limits.attempts + 1; a++) {
        const text = await ask("B", `formalize:${a}`, formalizePrompt(sim, guide, rejected), true);
        let res;
        try { res = await submit(`formalize:${a}`, answerJson(text)); }
        catch (error) { res = { ok: false, rejected: [String(error.message)] }; }
        log("formalize", { attempt: a, ok: res.ok, rejected: res.rejected?.length ?? 0 });
        if (res.ok) { changed = true; break; }
        rejected = res.rejected;
      }
      if (!changed) return { reason: "the interpreter could not write a model the daemon accepts", failed: true };
      work = await api("GET", `/api/sims/${simId}/work?gaps=${limits.gaps}&${q}`);
    }

    // 1b. carried rules whose quote is gone from the file (the sources changed)
    if ((work.stale ?? []).length) {
      show("requote", `${work.stale.length} rules lost their quote`);
      const base = `requote:${work.stale.map((x) => `${x.section}:${x.id}`).join(",")}:r${r}`;
      let rejected;
      for (let a = 1; a <= limits.attempts; a++) {
        const text = await ask("B", `${base}:${a}`, requotePrompt(sim, work.stale, guide, rejected), true);
        let res;
        try { res = await submit(`${base}:${a}`, answerJson(text).patch ?? {}); }
        catch (error) { res = { ok: false, rejected: [String(error.message)] }; }
        log("requote", { attempt: a, ok: res.ok });
        if (res.ok) { changed = true; break; }
        rejected = res.rejected;
      }
      work = await api("GET", `/api/sims/${simId}/work?gaps=${limits.gaps}&${q}`);
    }

    // 2. judge quoted rules without a verdict
    for (let i = 0; i < work.unjudged.length; i += limits.judgeBatch) {
      const items = work.unjudged.slice(i, i + limits.judgeBatch);
      const key = `judge:${items.map((x) => `${x.section}:${x.id}`).join(",")}:r${r}`;
      show("judge", `${i + items.length}/${work.unjudged.length} rules`);
      // A failed question ends the run; only an answer without JSON is skipped.
      const text = await ask("C", key, judgePrompt(sim, items), true);
      let verdicts;
      try { verdicts = answerJson(text); }
      catch (error) { log("judge_unreadable", { key, error: error.message }); continue; }
      if (!Array.isArray(verdicts)) continue;
      const res = await api("POST", `/api/sims/${simId}/verdicts`, { session, key, items: verdicts });
      log("judge", { key, done: res.done?.length ?? 0, retracted: res.retracted ?? [] });
      if (res.retracted?.length) changed = true;
    }

    // 3. the user's answers
    if (work.answered.length) {
      show("answers", `${work.answered.length} answers`);
      const base = `answers:${work.answered.map((f) => f.key).sort().join(",")}`;
      let rejected;
      for (let a = 1; a <= limits.attempts; a++) {
        const text = await ask("B", `${base}:${a}`, answersPrompt(sim, work.model, work.answered, guide, rejected), false);
        let res;
        try { res = await submit(`${base}:${a}`, answerJson(text).patch ?? {}); }
        catch (error) { res = { ok: false, rejected: [String(error.message)] }; }
        log("answers", { attempt: a, ok: res.ok });
        if (res.ok) { changed = true; break; }
        rejected = res.rejected;
      }
    }

    // 4. round
    show("round", `round ${r + 1}`);
    const round = await api("POST", `/api/sims/${simId}/round`, { session });
    log("round", { round: round.result?.round, open: round.result?.open, ended: round.result?.ended ?? null });
    if (round.result?.ended) return { reason: `round ${round.result.round}: ${round.result.ended.reason}`, ended: true };
    const n = round.result?.round ?? r + 1;

    // 5. gaps
    work = await api("GET", `/api/sims/${simId}/work?gaps=${limits.gaps}&${q}`);
    let b = 0;
    for (const raw of work.gaps) {
      b++;
      let batch = raw;
      // The judge sorts the batch first: model errors, silent sources, intended behaviour.
      if (sim.settings.triage) {
        show("triage", `round ${n}: ${raw.place} (${b}/${work.gaps.length})`);
        const tkey = `triage:r${n}:${raw.place}`;
        const text = await ask("C", tkey, triagePrompt(sim, work.model, raw), true);
        let verdicts;
        try { verdicts = answerJson(text); } catch (error) { log("triage_unreadable", { tkey, error: error.message }); }
        if (Array.isArray(verdicts)) {
          const res = await api("POST", `/api/sims/${simId}/triage`, { session, key: tkey, items: verdicts });
          log("triage", { tkey, done: res.done?.length ?? 0 });
          const by = new Map(verdicts.filter((v) => res.done?.includes(v.key)).map((v) => [v.key, v]));
          if ([...by.values()].some((v) => v.verdict === "intended")) changed = true;
          const findings = raw.findings.filter((f) => by.get(f.key)?.verdict !== "intended")
            .map((f) => (by.has(f.key) ? { ...f, triage: { verdict: by.get(f.key).verdict, why: by.get(f.key).why } } : f));
          batch = { ...raw, findings };
        }
        if (!batch.findings.length) continue;
      }
      show("gaps", `round ${n}: ${batch.place} (${b}/${work.gaps.length})`);
      const base = `gap:r${n}:${batch.place}`;
      let rejected;
      for (let a = 1; a <= limits.attempts; a++) {
        const text = await ask("B", `${base}:${a}`, gapPrompt(sim, work.model, batch, guide, rejected), true);
        let answer;
        try { answer = answerJson(text); }
        catch (error) { log("gap_unreadable", { base, error: error.message }); break; }
        let ok = true;
        if (answer.patch && Object.keys(answer.patch).length) {
          const res = await submit(`${base}:${a}`, answer.patch).catch((error) => ({ ok: false, rejected: [String(error.message)] }));
          log("gap_patch", { base, attempt: a, ok: res.ok });
          if (res.ok) changed = true;
          else { ok = false; rejected = res.rejected; }
        }
        const items = (answer.resolve ?? []).filter((x) => x && x.key);
        if (items.length) {
          const res = await api("POST", `/api/sims/${simId}/resolve`, { session, key: `${base}:${a}:resolve`, items });
          if (res.done?.length) changed = true;
        }
        if (ok) break;
      }
    }
    if (changed) idle = 0;
    else if (++idle >= limits.idle) return { reason: `${idle} passes in a row changed nothing` };
  }
}

