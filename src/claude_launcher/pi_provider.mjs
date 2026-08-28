// Register one process-local Pi provider from non-secret values assembled by
// claunch. The API key remains in the harness's declared environment variable;
// this extension stores only that variable's name.

const provider = process.env.CLAUNCH_PI_PROVIDER;
const baseUrl = process.env.CLAUNCH_PI_BASE_URL;
const tokenEnv = process.env.CLAUNCH_PI_TOKEN_ENV;
const rawModels = process.env.CLAUNCH_PI_MODELS;

export default function (pi) {
  if (!provider || !baseUrl || !tokenEnv || !rawModels) return;

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
    contextWindow: 128000,
    maxTokens: 16384,
  }));

  pi.registerProvider(provider, {
    baseUrl,
    apiKey: tokenEnv,
    api: "anthropic-messages",
    authHeader: process.env.CLAUNCH_PI_AUTH_HEADER === "1",
    models,
  });
}
