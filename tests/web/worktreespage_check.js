/* Worktrees page: the four age groups and the merge label (worktrees-page.js). */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const src = fs.readFileSync(
  path.join(__dirname, '../../src/claude_launcher/web/static/worktrees-page.js'), 'utf8');
const ctx = vm.createContext({ globalThis: {}, document: {}, Date });
ctx.globalThis = ctx;
vm.runInContext(src, ctx);
const { bucketOf, mergeLabel } = ctx.WorktreesPage;

// Local calendar: 2026-09-29 15:00 local.
const now = new Date(2026, 8, 29, 15, 0, 0);
const at = (d, h = 12) => new Date(2026, 8, d, h, 0, 0).toISOString();
assert.strictEqual(bucketOf(at(29, 0), now), 'today');      // local midnight
assert.strictEqual(bucketOf(at(28, 23), now), 'week');      // yesterday
assert.strictEqual(bucketOf(at(23, 0), now), 'week');       // 6 days back
assert.strictEqual(bucketOf(at(22, 23), now), 'month');     // 7th day back
assert.strictEqual(bucketOf(new Date(2026, 7, 31, 0).toISOString(), now), 'month');
assert.strictEqual(bucketOf(new Date(2026, 7, 30, 23).toISOString(), now), 'older');
assert.strictEqual(bucketOf(null, now), 'older');
assert.strictEqual(bucketOf('garbage', now), 'older');

assert.strictEqual(mergeLabel({ trunk: 'master', merged: true, behind: 3 })[0], 'merged');
assert.strictEqual(mergeLabel({ trunk: 'master', merged: false, ahead: 2 })[1], '미머지 · 2 ahead');
assert.strictEqual(mergeLabel({ trunk: 'master', is_trunk: true, merged: true })[0], 'trunk');
assert.strictEqual(mergeLabel({ trunk: '', merged: null })[0], 'unknown');
assert.strictEqual(mergeLabel({ trunk: 'master', merged: null })[0], 'unknown');
console.log('ok');
