import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { apiKeyReference, piVersion } from '../src/claude_launcher/pi_provider.mjs';

function packageDir(t, name, version) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'claunch-pi-pkg-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.writeFileSync(path.join(dir, 'package.json'), JSON.stringify({ name, version }));
  return dir;
}

test('Pi before 0.77.0 gets the bare variable name', () => {
  for (const version of ['0.76.9', '0.70.0', '0.9.99']) {
    assert.equal(apiKeyReference('ANTHROPIC_API_KEY', version), 'ANTHROPIC_API_KEY', version);
  }
});

test('Pi 0.77.0 and later gets the $NAME reference', () => {
  for (const version of ['0.77.0', '0.78.2', '0.79.3', '0.79.4', '0.87.1', '1.0.0', '0.77.0-beta.1']) {
    assert.equal(apiKeyReference('ANTHROPIC_API_KEY', version), '$ANTHROPIC_API_KEY', version);
  }
});

test('an unknown version gets the $NAME reference', () => {
  for (const version of [undefined, '', 'dev']) {
    assert.equal(apiKeyReference('ANTHROPIC_API_KEY', version), '$ANTHROPIC_API_KEY');
  }
});

test('the version comes from PI_PACKAGE_DIR', (t) => {
  const dir = packageDir(t, '@earendil-works/pi-coding-agent', '0.78.1');
  assert.equal(piVersion({ PI_PACKAGE_DIR: dir }, [], ''), '0.78.1');
});

test('the version comes from above the entry script, following symlinks', (t) => {
  const dir = packageDir(t, '@mariozechner/pi-coding-agent', '0.76.0');
  fs.mkdirSync(path.join(dir, 'dist'));
  const cli = path.join(dir, 'dist', 'cli.js');
  fs.writeFileSync(cli, '');
  const binDir = fs.mkdtempSync(path.join(os.tmpdir(), 'claunch-pi-bin-'));
  t.after(() => fs.rmSync(binDir, { recursive: true, force: true }));
  const link = path.join(binDir, 'pi');
  try {
    fs.symlinkSync(cli, link);
  } catch (err) {
    // Windows grants file symlinks only with Developer Mode or elevation.
    if (err.code === 'EPERM') return t.skip('creating a symlink needs Developer Mode or elevation on Windows');
    throw err;
  }
  assert.equal(piVersion({}, ['node', link], ''), '0.76.0');
});

test('an unrelated package.json is not taken for Pi', (t) => {
  const dir = packageDir(t, 'something-else', '0.1.0');
  assert.equal(piVersion({ PI_PACKAGE_DIR: dir }, [], ''), undefined);
});

test('the registered provider carries the reference for the running Pi', async (t) => {
  const dir = packageDir(t, '@earendil-works/pi-coding-agent', '0.76.3');
  const env = {
    CLAUNCH_PI_PROVIDER: 'claunch-profile', CLAUNCH_PI_BASE_URL: 'https://example.invalid/v1',
    CLAUNCH_PI_API: 'openai-completions', CLAUNCH_PI_TOKEN_ENV: 'TEST_API_KEY',
    CLAUNCH_PI_MODELS: '["flash"]', PI_PACKAGE_DIR: dir,
  };
  const before = Object.fromEntries(Object.keys(env).map(key => [key, process.env[key]]));
  t.after(() => {
    for (const [key, value] of Object.entries(before)) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
  });
  Object.assign(process.env, env);
  const { default: register } = await import('../src/claude_launcher/pi_provider.mjs?api-key-test');
  let registered;
  register({ registerProvider: (name, config) => { registered = config; }, on: () => {} });
  assert.equal(registered.apiKey, 'TEST_API_KEY');
  fs.writeFileSync(path.join(dir, 'package.json'), JSON.stringify({ name: '@earendil-works/pi-coding-agent', version: '0.79.4' }));
  register({ registerProvider: (name, config) => { registered = config; }, on: () => {} });
  assert.equal(registered.apiKey, '$TEST_API_KEY');
});
