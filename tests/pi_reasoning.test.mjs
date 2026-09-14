import assert from "node:assert/strict";
import test from "node:test";

let serial = 0;

async function extension({ effort, format } = {}) {
  const env = {
    CLAUNCH_PI_PROVIDER: "claunch-profile",
    CLAUNCH_PI_BASE_URL: "https://example.invalid/v1",
    CLAUNCH_PI_API: "openai-completions",
    CLAUNCH_PI_TOKEN_ENV: "TEST_API_KEY",
    CLAUNCH_PI_MODELS: '["deepseek-flash"]',
    CLAUNCH_PI_REASONING_EFFORT: effort,
    CLAUNCH_PI_REASONING_FORMAT: format,
  };
  const before = Object.fromEntries(
    Object.keys(env).map((key) => [key, process.env[key]]),
  );
  try {
    for (const [key, value] of Object.entries(env)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    serial += 1;
    const { default: register } = await import(
      `../src/claude_launcher/pi_provider.mjs?reasoning-test=${serial}`
    );
    const hooks = new Map();
    let registered;
    register({
      registerProvider: (name, config) => {
        registered = { name, ...config };
      },
      on: (event, handler) => hooks.set(event, handler),
    });
    return { registered, hooks };
  } finally {
    for (const [key, value] of Object.entries(before)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

test("an undeclared model remains non-reasoning", async () => {
  const { registered, hooks } = await extension();
  const model = registered.models[0];
  assert.equal(model.reasoning, false);
  assert.equal(model.compat.supportsReasoningEffort, false);
  assert.equal("thinkingFormat" in model.compat, false);
  assert.equal(hooks.has("before_provider_request"), false);
});

test("DeepSeek high is explicit in model metadata and request payload", async () => {
  const { registered, hooks } = await extension({
    effort: "high",
    format: "deepseek",
  });
  const model = registered.models[0];
  assert.equal(model.reasoning, true);
  assert.equal(model.compat.supportsReasoningEffort, true);
  assert.equal(model.compat.thinkingFormat, "deepseek");
  assert.equal(model.compat.requiresReasoningContentOnAssistantMessages, true);
  assert.deepEqual(model.thinkingLevelMap, {
    minimal: null,
    low: "low",
    medium: "medium",
    high: "high",
    xhigh: null,
  });

  const hook = hooks.get("before_provider_request");
  const ctx = {
    model: { provider: "claunch-profile", api: "openai-completions" },
  };
  const event = {
    payload: { model: "deepseek-flash", messages: [], stream: true },
  };
  assert.deepEqual(hook(event, ctx), {
    ...event.payload,
    thinking: { type: "enabled" },
    reasoning_effort: "high",
  });
  assert.equal("thinking" in event.payload, false);
});

test("the request hook preserves explicit session overrides", async () => {
  const { hooks } = await extension({ effort: "high", format: "deepseek" });
  const hook = hooks.get("before_provider_request");
  const ctx = {
    model: { provider: "claunch-profile", api: "openai-completions" },
  };
  assert.equal(
    hook(
      {
        payload: {
          model: "deepseek-flash",
          thinking: { type: "disabled" },
        },
      },
      ctx,
    ),
    undefined,
  );
  assert.equal(
    hook(
      {
        payload: {
          model: "deepseek-flash",
          thinking: { type: "enabled" },
          reasoning_effort: "low",
        },
      },
      ctx,
    ),
    undefined,
  );
});

test("incomplete and unsupported declarations are rejected", async () => {
  await assert.rejects(
    extension({ effort: "high" }),
    /CLAUNCH_PI_REASONING_EFFORT and CLAUNCH_PI_REASONING_FORMAT/,
  );
  await assert.rejects(
    extension({ format: "deepseek" }),
    /CLAUNCH_PI_REASONING_EFFORT and CLAUNCH_PI_REASONING_FORMAT/,
  );
  await assert.rejects(
    extension({ effort: "xhigh", format: "deepseek" }),
    /CLAUNCH_PI_REASONING_EFFORT/,
  );
  await assert.rejects(
    extension({ effort: "high", format: "guessed" }),
    /CLAUNCH_PI_REASONING_FORMAT/,
  );
});
