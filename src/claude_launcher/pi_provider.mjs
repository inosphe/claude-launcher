// Register one process-local Pi provider from non-secret values assembled by
// claunch. The API key remains in the harness's declared environment variable;
// this extension stores only that variable's name.

const provider = process.env.CLAUNCH_PI_PROVIDER;
const baseUrl = process.env.CLAUNCH_PI_BASE_URL;
const api = process.env.CLAUNCH_PI_API;
const tokenEnv = process.env.CLAUNCH_PI_TOKEN_ENV;
const rawModels = process.env.CLAUNCH_PI_MODELS;
const rawWindow = Number.parseInt(process.env.CLAUNCH_PI_CONTEXT_WINDOW ?? "", 10);
const contextWindow = Number.isFinite(rawWindow) && rawWindow > 0 ? rawWindow : 128000;
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

export default function (pi) {
  if (!provider || !baseUrl || !api || !tokenEnv || !rawModels) return;

  const ids = JSON.parse(rawModels);
  if (!Array.isArray(ids) || ids.length === 0) {
    throw new Error("CLAUNCH_PI_MODELS must contain at least one model ID");
  }
  const models = ids.map((id) => ({
    id,
    name: id,
    reasoning: false,
    input: ["text"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow,
    maxTokens: 16384,
    compat: {
      supportsStore: false,
      supportsDeveloperRole: false,
      supportsReasoningEffort: false,
      supportsUsageInStreaming: streamUsage,
      maxTokensField: "max_tokens",
      supportsStrictMode: false,
    },
  }));

  pi.registerProvider(provider, {
    baseUrl,
    apiKey: tokenEnv,
    api,
    authHeader: process.env.CLAUNCH_PI_AUTH_HEADER === "1",
    ...(headers ? { headers } : {}),
    models,
  });
}
