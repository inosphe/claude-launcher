import assert from "node:assert/strict";
import test from "node:test";
import install from "../src/claude_launcher/pi_activity.mjs";

test("Pi starts, refreshes, stops and releases its heartbeat", (t) => {
  const previous = process.env.CLAUNCH_SESSION;
  process.env.CLAUNCH_SESSION = "test";
  t.after(() => {
    if (previous === undefined) delete process.env.CLAUNCH_SESSION;
    else process.env.CLAUNCH_SESSION = previous;
  });
  const output = [];
  const timers = new Map();
  t.mock.method(process.stdout, "write", (text) => { output.push(text); return true; });
  t.mock.method(globalThis, "setInterval", (callback, ms) => {
    assert.equal(ms, 5000);
    const timer = { unref() {} };
    timers.set(timer, callback);
    return timer;
  });
  t.mock.method(globalThis, "clearInterval", (timer) => timers.delete(timer));
  const handlers = {};
  install({ on: (event, handler) => { handlers[event] = handler; } });
  handlers.agent_start({}, { hasUI: false });
  handlers.agent_end();
  handlers.session_shutdown();
  assert.equal(output.length, 0);
  handlers.agent_start({}, { hasUI: true });
  assert.equal(output.at(-1), "\x1b]777;claunch;activity;busy\x07");
  for (const tick of timers.values()) tick();
  assert.equal(output.length, 2);
  handlers.agent_start({}, { hasUI: true });
  assert.equal(timers.size, 1);
  handlers.agent_end();
  assert.equal(timers.size, 0);
  assert.equal(output.at(-1), "\x1b]777;claunch;activity;idle\x07");
  handlers.agent_start({}, { hasUI: true });
  handlers.session_shutdown();
  assert.equal(timers.size, 0);
  assert.equal(output.at(-1), "\x1b]777;claunch;activity;idle\x07");
});

test("unmanaged Pi does not register activity hooks", () => {
  const previous = process.env.CLAUNCH_SESSION;
  delete process.env.CLAUNCH_SESSION;
  try {
    install({ on() { assert.fail("unexpected hook"); } });
  } finally {
    if (previous !== undefined) process.env.CLAUNCH_SESSION = previous;
  }
});
