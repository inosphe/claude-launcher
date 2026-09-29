// Register one process-local Pi provider from non-secret values assembled by
// claunch. The API key remains in the harness's declared environment variable;
// this extension stores only a reference to that variable.

import fs from "node:fs";
import path from "node:path";

const provider = process.env.CLAUNCH_PI_PROVIDER;
const baseUrl = process.env.CLAUNCH_PI_BASE_URL;
const api = process.env.CLAUNCH_PI_API;
const tokenEnv = process.env.CLAUNCH_PI_TOKEN_ENV;
const rawModels = process.env.CLAUNCH_PI_MODELS;
const rawWindow = Number.parseInt(process.env.CLAUNCH_PI_CONTEXT_WINDOW ?? "", 10);
const contextWindow = Number.isFinite(rawWindow) && rawWindow > 0 ? rawWindow : 128000;
const rawMaxTokens = process.env.CLAUNCH_PI_MAX_TOKENS;
const maxTokens = rawMaxTokens === undefined ? 16384 : Number(rawMaxTokens);
const reasoningEffort = process.env.CLAUNCH_PI_REASONING_EFFORT;
const reasoningFormat = process.env.CLAUNCH_PI_REASONING_FORMAT;
const reasoningEnabled = reasoningEffort !== undefined && reasoningFormat !== undefined;
const reasoningEfforts = new Set(["low", "medium", "high"]);
const reasoningFormats = new Set(["deepseek"]);
// Set by claunch when the base URL is its metering shim: the shim reads the
// usage object out of the stream, and OpenAI-style backends only send one
// when asked (stream_options.include_usage) -- which Pi does for a model whose
// compat does not say supportsUsageInStreaming: false.
const streamUsage = process.env.CLAUNCH_PI_STREAM_USAGE === "1";
// Extra request headers claunch wants on every call (the session name for the
// shim's records); a JSON object, absent on a direct launch.
const headers = parseHeaders(process.env.CLAUNCH_PI_HEADERS);

function parseHeaders(raw) {
  if (!raw) return undefined;
  const parsed = JSON.parse(raw);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("CLAUNCH_PI_HEADERS must be a JSON object");
  }
  return Object.keys(parsed).length > 0 ? parsed : undefined;
}

// Pi 0.77.0 changed how a provider's apiKey string is read: a plain string is
// now the key itself, and an environment variable is referenced as $NAME
// (0.77.0-0.79.3 still accepted a bare uppercase name for custom providers;
// 0.79.4 dropped that). Before 0.77.0 only the bare name was resolved, and
// "$NAME" would be sent literally. Sending the bare name to 0.79.4+ makes the
// backend receive the variable's name as the key (401).
const ENV_REFERENCE_SINCE = [0, 77, 0];

// The running Pi's version, from its package.json: PI_PACKAGE_DIR (Pi's own
// override), next to the executable (Bun binary), or above the entry script
// (npm and managed installs). undefined when none of them says.
export function piVersion(env = process.env, argv = process.argv, execPath = process.execPath) {
  const candidates = [];
  if (env.PI_PACKAGE_DIR) candidates.push(env.PI_PACKAGE_DIR);
  if (execPath) candidates.push(path.dirname(execPath));
  if (argv[1]) {
    let dir;
    try {
      dir = path.dirname(fs.realpathSync(argv[1]));
    } catch {
      dir = undefined;
    }
    while (dir) {
      candidates.push(dir);
      const parent = path.dirname(dir);
      if (parent === dir) break;
      dir = parent;
    }
  }
  for (const dir of candidates) {
    let pkg;
    try {
      pkg = JSON.parse(fs.readFileSync(path.join(dir, "package.json"), "utf8"));
    } catch {
      continue;
    }
    if (typeof pkg?.name === "string" && pkg.name.endsWith("/pi-coding-agent") && typeof pkg.version === "string") {
      return pkg.version;
    }
  }
  return undefined;
}

// The apiKey string that makes `version` read `name` from the environment. An
// unknown version gets the $NAME form, which every current Pi requires.
export function apiKeyReference(name, version) {
  const parts = /^(\d+)\.(\d+)\.(\d+)/.exec(version ?? "");
  if (!parts) return `$${name}`;
  for (let i = 0; i < 3; i++) {
    const diff = Number(parts[i + 1]) - ENV_REFERENCE_SINCE[i];
    if (diff !== 0) return diff > 0 ? `$${name}` : name;
  }
  return `$${name}`;
}

export default function (pi) {
  if (!provider || !baseUrl || !api || !tokenEnv || !rawModels) return;
  if (rawMaxTokens !== undefined && (!/^[0-9]+$/.test(rawMaxTokens) || !Number.isSafeInteger(maxTokens) || maxTokens <= 0 || maxTokens > contextWindow)) {
    throw new Error("CLAUNCH_PI_MAX_TOKENS must be a positive integer no greater than contextWindow");
  }
  if ((reasoningEffort === undefined) !== (reasoningFormat === undefined)) {
    throw new Error("CLAUNCH_PI_REASONING_EFFORT and CLAUNCH_PI_REASONING_FORMAT must be set together");
  }
  if (reasoningEffort !== undefined && !reasoningEfforts.has(reasoningEffort)) {
    throw new Error("CLAUNCH_PI_REASONING_EFFORT must be low, medium, or high");
  }
  if (reasoningFormat !== undefined && !reasoningFormats.has(reasoningFormat)) {
    throw new Error("CLAUNCH_PI_REASONING_FORMAT must be deepseek");
  }

  const ids = JSON.parse(rawModels);
  if (!Array.isArray(ids) || ids.length === 0) {
    throw new Error("CLAUNCH_PI_MODELS must contain at least one model ID");
  }
  const models = ids.map((id) => ({
    id,
    name: id,
    reasoning: reasoningEnabled,
    ...(reasoningFormat === "deepseek" ? {
      thinkingLevelMap: { minimal: null, low: "low", medium: "medium", high: "high", xhigh: null },
    } : {}),
    input: ["text"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow,
    maxTokens,
    compat: {
      supportsStore: false,
      supportsDeveloperRole: false,
      supportsReasoningEffort: reasoningEnabled,
      supportsUsageInStreaming: streamUsage,
      maxTokensField: "max_tokens",
      supportsStrictMode: false,
      ...(reasoningEnabled ? { thinkingFormat: reasoningFormat } : {}),
      ...(reasoningFormat === "deepseek" ? {
        requiresReasoningContentOnAssistantMessages: true,
      } : {}),
    },
  }));

  pi.registerProvider(provider, {
    baseUrl,
    apiKey: apiKeyReference(tokenEnv, piVersion()),
    api,
    authHeader: process.env.CLAUNCH_PI_AUTH_HEADER === "1",
    ...(headers ? { headers } : {}),
    models,
  });

  if (rawMaxTokens !== undefined || reasoningEnabled) {
    // Set declared request controls at the final provider boundary. Pi's
    // native DeepSeek encoder normally supplies both reasoning fields; the
    // fallback below covers versions that only consume the model metadata.
    // A present thinking field is an explicit session choice and is kept.
    pi.on("before_provider_request", (event, ctx) => {
      if (ctx.model?.provider !== provider || ctx.model?.api !== api) return;
      const payload = event.payload;
      if (!payload || typeof payload !== "object" || Array.isArray(payload) || !ids.includes(payload.model)) return;
      let next = payload;
      let changed = false;
      if (rawMaxTokens !== undefined) {
        next = { ...next, max_tokens: maxTokens };
        changed = true;
      }
      if (reasoningFormat === "deepseek") {
        if (!Object.hasOwn(next, "thinking")) {
          next = {
            ...next,
            thinking: { type: "enabled" },
            reasoning_effort: reasoningEffort,
          };
          changed = true;
        } else if (
          next.thinking?.type === "enabled" &&
          !Object.hasOwn(next, "reasoning_effort")
        ) {
          next = { ...next, reasoning_effort: reasoningEffort };
          changed = true;
        }
      }
      return changed ? next : undefined;
    });
  }
}
