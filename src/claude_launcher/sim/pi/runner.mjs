// The simulation run session: a pi extension the daemon loads into the pi
// session it launches when a campaign starts (daemon/sim_api.py,
// launch_runner). Without CLAUNCH_SIM_ID it does nothing.
//
// The daemon keeps the simulation (model, findings, rounds); this process
// only reads it and submits to it over the daemon API, as the run session,
// which acts with the owner's authority while attached. Every question to a
// model is one durable task (pi-durable) with its own fresh conversation,
// keyed by what it is about, so a restart of this process (a daemon restart
// restores the session) waits for the same task instead of asking again,
// and every submission carries the same key, which the daemon applies once.
//
// The loop (runCampaign):
//   1. formalize  -- an empty model: the interpreter (B, the session's own
//                    model) reads the sources and writes the first model;
//   2. judge      -- every quoted rule without a verdict on its current text
//                    goes to the judge (C, the judge profile's model):
//                    confirmed, fabricated (retracted), ambiguous;
//   3. answers    -- the user's answers become rules (source user:<key>);
//   4. round      -- the daemon walks the model;
//   5. gaps       -- B interprets each gap batch: patch, dismiss or ask;
//   and again until the daemon ends the campaign (nothing open, max_rounds,
//   a stop) or two passes in a row change nothing. Then it reports to the
//   daemon (runner/done), which tells the owner session, and exits.
//
// Ported from gds6 designframe/sim2/pi/extension.mjs (askTask, runKeyed,
// registryModels, cutLoops, the store lock).

import { appendFileSync, closeSync, mkdirSync, openSync, readFileSync, unlinkSync, writeSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import { Type } from "@earendil-works/pi-ai";
import { configure, createRegistry, defineDoc, defineExtension, defineTask, defineTool, Harness } from "@earendil-works/pi-durable";
import { openNodeSqliteStorage } from "@earendil-works/pi-durable/storage/sqlite/node";
import { LIMITS, listFiles, readFileText, runCampaign, searchText } from "./campaign.mjs";

const cx = BACKGROUND_CONTEXT;

// -- configuration ------------------------------------------------------------

export function runnerEnv(env = process.env) {
  const id = (env.CLAUNCH_SIM_ID ?? "").trim();
  if (!id) return null;
  return {
    id,
    store: env.CLAUNCH_SIM_STORE,
    daemonDir: env.CLAUNCH_SIM_DAEMON_DIR,
    providerJs: env.CLAUNCH_SIM_PROVIDER_JS,
    session: env.CLAUNCH_SESSION ?? "",
  };
}

const HARNESS_SETTINGS = {
  stream: { maxTokens: 65536 },
  retry: { enabled: true, maxRetries: 8, baseDelayMs: 2000, maxAgentDelayMs: 60_000 },
};

// -- daemon API -----------------------------------------------------------------

export function daemonClient(daemonDir, fetchImpl = fetch) {
  const doc = JSON.parse(readFileSync(path.join(daemonDir, "daemon.json"), "utf8"));
  const host = ["0.0.0.0", "::", undefined, ""].includes(doc.host) ? "127.0.0.1" : doc.host;
  const base = `http://${host}:${doc.port}`;
  const token = readFileSync(path.join(daemonDir, "token"), "utf8").trim();
  return async function api(method, route, body) {
    const resp = await fetchImpl(base + route, {
      method,
      headers: { Authorization: `Bearer ${token}`, ...(body ? { "Content-Type": "application/json" } : {}) },
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(`${method} ${route}: ${resp.status} ${data.error ?? ""}`.trim());
    return data;
  };
}

// -- store lock (one process per durable store) -----------------------------------

export class StoreLocked extends Error {}

const alive = (pid) => {
  try { process.kill(pid, 0); return true; } catch (error) { return error.code === "EPERM"; }
};

export function acquireStoreLock(storeFile, pid = process.pid) {
  const lockFile = `${storeFile}.lock`;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const fd = openSync(lockFile, "wx");
      writeSync(fd, String(pid));
      closeSync(fd);
      return () => {
        try { if (readFileSync(lockFile, "utf8").trim() === String(pid)) unlinkSync(lockFile); } catch { /* gone */ }
      };
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      let holder;
      try { holder = Number.parseInt(readFileSync(lockFile, "utf8").trim(), 10); } catch { holder = NaN; }
      if (Number.isInteger(holder) && holder > 0 && holder !== pid && alive(holder)) {
        throw new StoreLocked(`durable store ${storeFile} is held by process ${holder}`);
      }
      try { unlinkSync(lockFile); } catch { /* removed meanwhile */ }
    }
  }
  throw new Error(`could not take the store lock ${lockFile}`);
}

const OUTPUT_LIMITS = { maxBytes: 16 * 1024 * 1024, maxLines: 1_000_000, retain: "head" };

async function conversationCost(tx, conversationId) {
  const out = { input: 0, cacheRead: 0, output: 0, totalTokens: 0, peak: 0, generations: 0 };
  let cursor;
  do {
    const page = await tx.scanEntries({ conversationId, order: "ascending" }, 200, cursor);
    for (const e of page.items) for (const m of e.model ?? []) {
      if (m.role !== "assistant" || !m.usage) continue;
      const c = (m.usage.input ?? 0) + (m.usage.cacheRead ?? 0);
      out.input += m.usage.input ?? 0; out.cacheRead += m.usage.cacheRead ?? 0;
      out.output += m.usage.output ?? 0; out.totalTokens += m.usage.totalTokens ?? 0;
      out.peak = Math.max(out.peak, c); out.generations++;
    }
    cursor = page.next;
  } while (cursor !== undefined);
  return out;
}

// -- file tools (durable: the question's conversation reads the checkout) ------------

export function overLimit(cost, limits = LIMITS) {
  if (cost.generations >= limits.generations) return `the question reached ${limits.generations} generations`;
  if (cost.peak > limits.peak) return `one request's context reached ${cost.peak} tokens (limit ${limits.peak})`;
  if (cost.input + cost.cacheRead > limits.sum) return `the conversation's input reached ${cost.input + cost.cacheRead} tokens (limit ${limits.sum})`;
  return undefined;
}

function fileTool(name, description, parameters, run) {
  return defineTool({
    name, description, parameters, replay: "safe", outputLimits: OUTPUT_LIMITS,
    execute: async (args, api, context) => {
      const cost = await api.commit((tx) => conversationCost(tx, api.conversationId), context);
      const over = overLimit(cost);
      if (over) return { content: [{ type: "text", text: `${over}; stop reading and answer now.` }], isError: true, control: { terminate: true } };
      try {
        return { content: [{ type: "text", text: run(args) }] };
      } catch (error) {
        return { content: [{ type: "text", text: String(error.message ?? error) }], isError: true };
      }
    },
  });
}

export function fileTools(root) {
  return [
    fileTool("read_file", "Read a text file under the simulation root, whole (offset continues a very long one).",
      Type.Object({ path: Type.String(), offset: Type.Optional(Type.Integer()) }),
      (a) => readFileText(root, a.path, a.offset ?? 0)),
    fileTool("list_files", "List files and directories under a directory of the simulation root.",
      Type.Object({ dir: Type.Optional(Type.String()), depth: Type.Optional(Type.Integer()) }),
      (a) => listFiles(root, a.dir ?? ".", a.depth ?? 2)),
    fileTool("search_text", "Search the files under the simulation root for a regular expression (case-insensitive); returns path:line: text.",
      Type.Object({ pattern: Type.String(), dir: Type.Optional(Type.String()) }),
      (a) => searchText(root, a.pattern, a.dir ?? ".")),
  ];
}

// -- one question = one durable task with its own conversation ------------------------

const THINKING_OF = Object.freeze({ none: "minimal", low: "low", medium: "medium", high: "high" });
const EFFORT_OF = Object.freeze({ minimal: "none", low: "low", medium: "medium", high: "high" });

let toolsInUse = [];

const AskTask = defineTask({
  name: "sim.ask",
  version: 1,
  initial: () => ({ phase: "open" }),
  phases: {
    open: async (task, runtime, context) => {
      await runtime.commit(async (tx) => {
        const conversation = await tx.createConversation({ ownership: { kind: "task", taskId: runtime.taskId } });
        await configure(tx, conversation.id, {
          model: task.input.model,
          thinkingLevel: THINKING_OF[task.input.reasoning] ?? "off",
          tools: task.input.tools ? toolsInUse : [],
        });
        return { status: "running", checkpoint: { phase: "ask", conversationId: conversation.id } };
      }, context);
    },
    ask: async (task, runtime, context) => {
      const { conversationId } = task.state.checkpoint;
      const conversation = await runtime.conversation(conversationId, context);
      const submission = await conversation.submit({ type: "input", content: task.input.prompt, requestId: task.input.key }, context);
      const settled = await submission.wait(context);
      if (settled.status !== "done") {
        await runtime.commit(() => ({ status: "terminal", outcome: { status: "failed", error: { message: `no answer: ${settled.reason}` } } }), context);
        return;
      }
      await runtime.commit(async (tx) => {
        const message = (await tx.entry(settled.answer))?.model?.[0] ?? {};
        const text = (message.content ?? []).filter((c) => c.type === "text").map((c) => c.text).join("");
        const usage = await conversationCost(tx, conversationId);
        return { status: "terminal", outcome: { status: "completed", result: { text, usage, stop: message.stopReason ?? null } } };
      }, context);
    },
  },
  abort: async (_task, runtime, context) => {
    await runtime.commit(() => ({ status: "terminal", outcome: { status: "aborted", reason: "abort" } }), context);
  },
});

const Asks = defineDoc({ kind: "sim.asks", version: 1, scope: "session", history: "latest", initial: () => ({ byKey: {} }) });

/** Find or create the task for ``key`` and wait for it; the same key after a restart waits for the same task. */
async function runKeyed(harness, key, input) {
  const root = await harness.root(cx, { agent: { model: input.model } });
  const taskId = await root.commit(async (tx) => {
    const asks = await tx.doc(Asks);
    if (asks.byKey[key]) return asks.byKey[key];
    const id = await tx.createTask(AskTask, { ...input, key }, { ownership: { kind: "conversation" }, background: true });
    asks.byKey[key] = id;
    return id;
  }, cx);
  const outcome = (await harness.waitForTask(taskId, cx)).state.outcome;
  if (outcome.status !== "completed") throw new Error(`question ${key} ${outcome.status}: ${outcome.error?.message ?? outcome.reason ?? ""}`);
  return outcome.result;
}

// -- the models pi-durable calls, through pi's registry -------------------------------

export const LOOP = { min: 20000, window: 6000, distinct: 20, every: 2000 };

export function thinkingLoops(text, loop = LOOP) {
  if (text.length < loop.min) return false;
  const words = text.slice(-loop.window).toLowerCase().split(/[^\p{L}\p{N}]+/u).filter(Boolean);
  return new Set(words).size < loop.distinct;
}

/** Cut a generation whose thinking repeats itself; it ends as stopReason length. */
export function cutLoops(open, options, loop = LOOP) {
  const own = new AbortController();
  const signal = options?.signal ? AbortSignal.any([options.signal, own.signal]) : own.signal;
  const inner = open({ ...options, signal });
  let last;
  let cut = false;
  let checked = 0;
  const events = (async function* () {
    for await (const event of inner) {
      if (cut) continue;
      if (event.partial) {
        last = event.partial;
        const parts = (event.partial.content ?? []).filter((c) => c.type === "thinking");
        const size = parts.reduce((n, c) => n + (c.thinking?.length ?? 0), 0);
        if (size - checked >= loop.every) {
          checked = size;
          if (!options?.signal?.aborted && thinkingLoops(parts.map((c) => c.thinking ?? "").join(""), loop)) {
            cut = true;
            own.abort();
            continue;
          }
        }
      }
      yield event;
    }
  })();
  return {
    [Symbol.asyncIterator]: () => events,
    async result() {
      const message = await inner.result();
      if (!cut || options?.signal?.aborted) return message;
      const { errorMessage: _, ...rest } = { ...(last ?? message), usage: message.usage ?? last?.usage };
      return { ...rest, stopReason: "length" };
    },
  };
}

export function reasoningPayload(payload, model, effort) {
  if (!effort || model?.api !== "openai-completions" || !payload || typeof payload !== "object") return undefined;
  if ("reasoning_effort" in payload || "thinking" in payload || "reasoning" in payload) return undefined;
  return { ...payload, reasoning_effort: effort };
}

/** pi's ModelRegistry in the shape pi-durable calls (getModel, streamSimple, completeSimple, deferred). */
export function registryModels(registry) {
  const provider = (model) => registry.getProvider(model.provider);
  const withEffort = (options) => {
    const effort = EFFORT_OF[options?.reasoning];
    if (!effort) return options;
    const inner = options?.onPayload;
    return { ...options, onPayload: async (payload, m) => { const p = (await inner?.(payload, m)) ?? payload; return reasoningPayload(p, m, effort) ?? (p === payload ? undefined : p); } };
  };
  return {
    getModel: (p, id) => registry.find(p, id),
    getProvider: (p) => registry.getProvider(p),
    getProviders: () => registry.getRegisteredProviderIds().map((id) => registry.getProvider(id)).filter(Boolean),
    getModels: () => registry.getAll(),
    streamSimple: (model, context, options) => cutLoops((o) => registry.streamSimple(model, context, withEffort(o)), options),
    completeSimple: async (model, context, options) => registry.streamSimple(model, context, options).result(),
    fetchDeferred: (model, handle, options) => {
      const p = provider(model);
      if (!p?.fetchDeferred) throw new Error(`${model.provider} has no deferred responses`);
      return p.fetchDeferred(model, handle, options);
    },
    cancelDeferred: async (model, handle, options) => provider(model)?.cancelDeferred?.(model, handle, options),
  };
}

// -- the extension ------------------------------------------------------------------------

export default function simRunner(pi) {
  const cfg = runnerEnv();
  if (!cfg) return;
  const logFile = cfg.store.replace(/\.sqlite$/, ".events.jsonl");
  const log = (event, extra = {}) => {
    try { appendFileSync(logFile, `${JSON.stringify({ at: new Date().toISOString(), pid: process.pid, session: cfg.session, event, ...extra })}\n`); }
    catch { /* the log is evidence only */ }
  };
  // closing: the process is going away under the run (a kill, or a daemon
  // restart that will relaunch this session). Then nothing is reported: a
  // restart resumes the campaign, and a kill is reported by the daemon's
  // exit hook.
  let harness, release, started = false, closing = false;

  pi.on("session_start", async (event, ctx) => {
    log("session_start", { reason: event.reason });
    if (started) return;
    started = true;
    const api = daemonClient(cfg.daemonDir);
    const progress = (rec) => api("POST", `/api/sessions/${encodeURIComponent(cfg.session)}/progress`, rec).catch(() => {});
    const finish = async (reason, failed) => {
      log("done", { reason, failed: !!failed });
      await progress({ stage: failed ? "failed" : "done", text: String(reason).slice(0, 200) });
      await api("POST", `/api/sims/${cfg.id}/runner/done`, { session: cfg.session, reason, failed: !!failed }).catch((error) => log("done_failed", { error: error.message }));
      ctx.shutdown();
    };
    // The loop runs beside the TUI; session_start returns at once.
    (async () => {
      try {
        if (!ctx.model) throw new Error("the session has no model selected");
        const conf = await api("GET", `/api/sims/${cfg.id}/runner?session=${encodeURIComponent(cfg.session)}`);
        const root = conf.sim.root;
        const st = conf.sim.settings;
        // B: the session's model, or another model of the same profile. C: the judge
        // profile's model (registered here as a second provider), or another model of
        // B's profile, or B itself.
        const B = { provider: ctx.model.provider, modelId: st.interpreter_model || ctx.model.id };
        let C = st.judge_model ? { provider: B.provider, modelId: st.judge_model } : B;
        if (conf.judge) {
          process.env.CLAUNCH_SIMJ_API_KEY = conf.judge.key;
          const { registerFromEnv } = await import(pathToFileURL(cfg.providerJs).href);
          registerFromEnv(pi, conf.judge.env, "CLAUNCH_SIMJ_");
          C = { provider: "claunch-judge", modelId: st.judge_model || conf.judge.models[0] };
        }
        for (const m of [B, C]) if (!ctx.modelRegistry.find(m.provider, m.modelId)) throw new Error(`model ${m.provider}/${m.modelId} is not registered in this session`);
        mkdirSync(path.dirname(cfg.store), { recursive: true });
        release = acquireStoreLock(cfg.store);
        const registry = createRegistry();
        toolsInUse = fileTools(root);
        registry.install(defineExtension({ name: "sim-runner", tasks: [AskTask], tools: toolsInUse }));
        harness = await Harness.open(await openNodeSqliteStorage(cfg.store), { models: registryModels(ctx.modelRegistry), registry, settings: HARNESS_SETTINGS }, cx);
        harness.resume();
        log("harness_open", { b: B, c: C, root });
        const ask = async (role, key, prompt, tools) => {
          const reasoning = role === "C" ? st.judge_reasoning : st.interpreter_reasoning;
          const got = await runKeyed(harness, `${role}:${key}`, { role, prompt, tools, reasoning, model: role === "C" ? C : B });
          log("ask", { role, key, usage: got.usage, stop: got.stop });
          return got.text;
        };
        const res = await runCampaign({ simId: cfg.id, session: cfg.session, api, ask, guide: conf.guide, progress, log });
        await finish(res.reason, res.failed);
      } catch (error) {
        if (closing) { log("interrupted", { error: String(error.message ?? error) }); return; }
        log("error", { error: String(error.stack ?? error) });
        await finish(String(error.message ?? error), true);
      }
    })();
  });

  pi.on("session_shutdown", async (event) => {
    closing = true;
    log("session_shutdown", { reason: event?.reason });
    const h = harness;
    harness = undefined;
    if (h) await h.close(cx).catch(() => {});
    release?.();
    release = undefined;
  });
}
