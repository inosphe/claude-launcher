import assert from 'node:assert/strict';
import test from 'node:test';

async function extension(maxTokens) {
  const env = {
    CLAUNCH_PI_PROVIDER: 'claunch-profile', CLAUNCH_PI_BASE_URL: 'https://example.invalid/v1',
    CLAUNCH_PI_API: 'openai-completions', CLAUNCH_PI_TOKEN_ENV: 'TEST_API_KEY',
    CLAUNCH_PI_MODELS: '["flash"]', CLAUNCH_PI_CONTEXT_WINDOW: '1000000',
    CLAUNCH_PI_MAX_TOKENS: maxTokens,
  };
  const before = Object.fromEntries(Object.keys(env).map(key => [key, process.env[key]]));
  try {
    for (const [key, value] of Object.entries(env)) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
    const { default: register } = await import(`../src/claude_launcher/pi_provider.mjs?output-test=${encodeURIComponent(String(maxTokens))}`);
    const hooks = new Map(); let registered;
    register({ registerProvider: (name, config) => { registered = { name, ...config }; }, on: (event, handler) => hooks.set(event, handler) });
    return { registered, hooks };
  } finally {
    for (const [key, value] of Object.entries(before)) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
  }
}

test('unconfigured profiles preserve the 16384 output default', async () => {
  const { registered, hooks } = await extension(undefined);
  assert.equal(registered.models[0].maxTokens, 16384);
  assert.equal(hooks.has('before_provider_request'), false);
});

test('384K output reaches the request, with unrelated models and payload fields preserved', async () => {
  const { registered, hooks } = await extension('384000');
  assert.equal(registered.models[0].maxTokens, 384000);
  assert.equal(registered.models[0].contextWindow, 1000000);
  const hook = hooks.get('before_provider_request');
  const event = { payload: { model: 'flash', max_tokens: 32000, messages: [], stream: true } };
  const ctx = { model: { provider: 'claunch-profile', api: 'openai-completions' } };
  assert.deepEqual(hook(event, ctx), { ...event.payload, max_tokens: 384000 });
  assert.equal(event.payload.max_tokens, 32000);
  assert.equal(hook(event, { model: { ...ctx.model, provider: 'other' } }), undefined);
  assert.equal(hook(event, { model: { ...ctx.model, api: 'anthropic-messages' } }), undefined);
  assert.equal(hook({ payload: { model: 'other' } }, ctx), undefined);
});

test('invalid or excessive budgets are rejected at Pi extension load', async () => {
  for (const value of ['', '0', '-1', '1.5', '384000garbage', '1000001']) {
    await assert.rejects(extension(value), /CLAUNCH_PI_MAX_TOKENS/);
  }
});
