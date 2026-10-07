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

const worktrees = [
  { name: 'mixed', state: 'active', sessions: [{ name: 'old', category: 'archived' }, { name: 'live', category: 'running' }] },
  { name: 'killed', state: 'active', sessions: [{ name: 'dead', category: 'killed' }] },
  { name: 'archived', state: 'orphaned', sessions: [{ name: 'old2', category: 'archived' }] },
  { name: 'paused', state: 'active', sessions: [{ name: 'paused', category: 'paused' }] },
  { name: 'empty', state: 'unlinked' },
];
const names = (filter, search) => Array.from(ctx.WorktreesPage.filterItems(worktrees, filter, search), w => w.name);
assert.deepStrictEqual(names('archived'), ['mixed', 'archived']);
assert.deepStrictEqual(names('killed'), ['killed']);
assert.deepStrictEqual(names('active'), ['mixed', 'killed', 'paused']);
assert.deepStrictEqual(names('orphaned'), ['archived']);
assert.deepStrictEqual(names('unlinked'), ['empty']);
assert.deepStrictEqual(names('archived', ' OLD2 '), ['archived']);
assert.deepStrictEqual(names('killed', 'old'), []);
assert.strictEqual(names('all').length, 5);

// Exercise expansion, status rendering, refreshing and retry without HTML
// interpretation of repository-controlled filenames.
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.textContent = ''; }
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
}
ctx.document.createElement = tag => new Element(tag);
const textOf = el => el.textContent + el.children.map(textOf).join('');
(async () => {
  let calls = 0;
  let reply = { files: [{ status: 'RM', original_path: 'old.txt', path: '<img>.txt' },
    { status: '??', path: '한 글.txt' }] };
  let ok = true;
  ctx.api = async url => {
    calls++;
    assert.strictEqual(url, '/api/worktrees/changes?path=F%3A%2Fwork%20tree');
    return { ok, json: async () => reply };
  };
  const panel = ctx.WorktreesPage.changesPanel({ path: 'F:/work tree' });
  assert.strictEqual(calls, 0);
  panel.open = true;
  await panel.ontoggle();
  assert.strictEqual(calls, 1);
  assert(textOf(panel).includes('2개'));
  assert(textOf(panel).includes('old.txt → <img>.txt'));
  assert(textOf(panel).includes('한 글.txt'));
  const list = panel.children[1].children[1];
  assert.strictEqual(list.children[0].children[0].textContent, 'RM');
  assert.strictEqual(list.children[0].children[1].children.length, 0);
  await panel.ontoggle();
  assert.strictEqual(calls, 1); // already loaded
  reply = { files: [] };
  await panel.children[2].onclick();
  assert(textOf(panel).includes('커밋되지 않은 파일이 없습니다.'));
  ok = false; reply = { error: 'git failed' };
  await panel.children[2].onclick();
  assert(textOf(panel).includes('조회 실패'));
  assert(textOf(panel).includes('git failed'));
  assert(!textOf(panel).includes('0개'));
  ok = true; reply = { files: [] };
  await panel.ontoggle();
  assert(textOf(panel).includes('0개'));
  console.log('changes panel ok');
})().catch(error => { console.error(error); process.exitCode = 1; });
