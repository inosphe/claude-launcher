/* Hive page: the model behind #/hive (hive-page.js) -- which sessions it
   keeps, how they nest into projects, meshes and trees, which siblings fold
   into decks, and where the board's work lands. */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const src = fs.readFileSync(
  path.join(__dirname, '../../src/claude_launcher/web/static/hive-page.js'), 'utf8');
const ctx = vm.createContext({
  globalThis: {}, Date,
  localStorage: { getItem: () => null, setItem: () => {} },
  setInterval: () => 0, clearInterval: () => {},
});
ctx.globalThis = ctx;
vm.runInContext(src, ctx);
const P = ctx.HivePage;

const H = 3600000;
const now = Date.UTC(2026, 9, 26, 12, 0, 0);
const iso = (t) => new Date(t).toISOString();
const plain = (v) => JSON.parse(JSON.stringify(v));

// States: the rail's categories, a running session split by what it does.
assert.strictEqual(P.stateOf({ status: 'busy' }), 'busy');
assert.strictEqual(P.stateOf({ status: 'idle' }), 'idle');
assert.strictEqual(P.stateOf({ status: 'running' }), 'starting');
assert.strictEqual(P.stateOf({ status: 'exited', held_at: 'x' }), 'hold');
assert.strictEqual(P.stateOf({ status: 'exited', held_at: 'x', delayed_at: 'x' }), 'delayed');
assert.strictEqual(P.stateOf({ status: 'exited', paused_at: 'x' }), 'paused');
assert.strictEqual(P.stateOf({ status: 'exited', failed_at: 'x' }), 'failed');
assert.strictEqual(P.stateOf({ status: 'exited' }), 'killed');
assert.strictEqual(P.stateOf({ status: 'exited', archived_at: 'x' }), 'archived');

const killedAgo = (h) => ({ name: 'k', status: 'exited', exited_at: iso(now - h * H) });
const killedAgo0 = () => killedAgo(0);
// Ended records stay off unless asked for: archived behind their switch,
// killed and failed behind a window (none by default), held and paused always.
assert.strictEqual(P.keep(killedAgo0(), {}, now), false, 'killed is hidden by default');
assert.strictEqual(P.keep({ status: 'exited', failed_at: iso(now) }, {}, now), false, 'failed too');
assert.strictEqual(P.keep(killedAgo(1), { endedMs: 6 * H }, now), true);
assert.strictEqual(P.keep(killedAgo(7), { endedMs: 6 * H }, now), false);
assert.strictEqual(P.keep(killedAgo(7), { endedMs: Infinity }, now), true);
assert.strictEqual(P.keep(killedAgo(0), { endedMs: 0 }, now), false);
assert.strictEqual(P.keep({ status: 'exited', archived_at: 'x' }, {}, now), false);
assert.strictEqual(P.keep({ status: 'exited', archived_at: 'x' }, { archived: true }, now), true);
assert.strictEqual(P.keep({ status: 'exited', held_at: iso(now - 900 * H) }, { endedMs: 0 }, now), true);
assert.strictEqual(P.keep({ status: 'exited', paused_at: 'x' }, { endedMs: 0 }, now), true);

// Minor roles: reviewers and oneshot jobs, by role or by the -rev naming;
// a leader or worker role never is.
assert.strictEqual(P.isMinor({ name: 'a' }, 'reviewer'), true);
assert.strictEqual(P.isMinor({ name: 'a', kind: 'oneshot' }, ''), true);
assert.strictEqual(P.isMinor({ name: 's12-rev2' }, ''), true);
assert.strictEqual(P.isMinor({ name: 's12-rev' }, 'leader+reviewer'), false);
assert.strictEqual(P.isMinor({ name: 's12' }, 'worker'), false);
assert.strictEqual(P.isMinor({ name: 's12' }, ''), false);

// An issue's parent is read from its dotted id, nearest present ancestor.
const ids = new Set(['a-x', 'a-x.2']);
assert.strictEqual(P.issueParentId('a-x.2.1', ids), 'a-x.2');
assert.strictEqual(P.issueParentId('a-x.3.1', ids), 'a-x');
assert.strictEqual(P.issueParentId('a-y.1', ids), null);
assert.strictEqual(P.issueParentId('a-x', ids), null);
const nested = plain(P.nestIssues([
  { kind: 'issue', id: 'a-x.1', status: 'open', priority: 2, children: [] },
  { kind: 'issue', id: 'a-x', status: 'open', priority: 2, children: [] },
  { kind: 'issue', id: 'a-z', status: 'in_progress', priority: 3, children: [] },
]));
// in_progress sorts ahead of open; the child hangs under its parent.
assert.deepStrictEqual(nested.map((i) => i.id), ['a-z', 'a-x']);
assert.deepStrictEqual(nested[1].children.map((i) => i.id), ['a-x.1']);

// A fleet: two projects, one mesh, a three-level tree with reviewers, an
// ended parent kept for its live child, a killed session past the window.
const s = (name, extra = {}) => ({ name, status: 'idle', project: 'claunch', created_at: iso(now - 10 * H), ...extra });
const fleet = [
  s('lead', { status: 'busy' }),
  s('w1', { parent: 'lead', status: 'busy' }),
  s('w1-kid', { parent: 'w1' }),
  s('w1-rev', { parent: 'w1' }),
  s('w1-rev2', { parent: 'w1' }),
  s('w2', { parent: 'lead', status: 'exited', exited_at: iso(now - 30 * H) }),
  s('w2-kid', { parent: 'w2' }),
  s('gone', { parent: 'lead', status: 'exited', exited_at: iso(now - 30 * H) }),
  s('h1', { status: 'exited', held_at: 'x' }),
  s('h2', { status: 'exited', held_at: 'x' }),
  s('h3', { status: 'exited', held_at: 'x' }),
  s('h4', { status: 'exited', held_at: 'x' }),
  s('h5', { status: 'exited', held_at: 'x', project: 'gds' }),
  s('old', { status: 'exited', archived_at: 'x' }),
  s('loop-a', { parent: 'loop-b' }),
  s('loop-b', { parent: 'loop-a' }),
];
const MESH = { lead: [{ mesh: 'claunch', label: 'leader' }], w1: [{ mesh: 'claunch', label: 'worker' }] };
const queues = {
  boards: [{
    root: '/w/claunch', workspace: 'claunch',
    lanes: [
      { session: 'w1', known: true, issues: [
        { id: 'c-1', title: 'one', status: 'in_progress', priority: 2 },
        { id: 'c-1.1', title: 'one.one', status: 'open', priority: 2 },
      ], created: [{ id: 'c-9' }] },
      { session: 'gone', known: true, issues: [{ id: 'c-2', title: 'orphaned', status: 'in_progress', priority: 1 }], created: [] },
      { session: 'operator', known: false, issues: [{ id: 'c-3', title: 'a person', status: 'open', priority: 2 }], created: [] },
    ],
    unassigned: [
      { id: 'c-4', title: 'p1', status: 'open', priority: 1 },
      { id: 'c-5', title: 'p3', status: 'open', priority: 3 },
      { id: 'c-5.1', title: 'p3 child', status: 'open', priority: 3 },
    ],
  }],
};
const model = plain(P.buildModel(fleet, queues, {
  now, endedMs: 6 * H, meshesOf: (n) => MESH[n] || [], cflowOf: (n) => n === 'lead' ? { state: 'yours', label: 'v2 · ⚑ select' } : null,
}));

// Projects: the busiest first; every drawn session counted once.
assert.deepStrictEqual(model.projects.map((p) => p.name), ['claunch', 'gds']);
assert.strictEqual(model.hiddenArchived, 1);
assert.strictEqual(model.hiddenEnded, 1, 'gone is past the window; w2 is kept for its child');
assert.strictEqual(model.killedTotal, 2);
assert.strictEqual(model.archivable, 2, 'the archive button counts what archive-exited takes');
const claunch = model.projects[0];
// The tree goes under the mesh its root leads; roots with no mesh last.
assert.deepStrictEqual(claunch.meshes.map((m) => m.name), ['claunch', '']);
const lead = claunch.meshes[0].roots[0];
assert.strictEqual(lead.name, 'lead');
assert.strictEqual(lead.cflow.state, 'yours');
// w1 is drawn; w2 stays as a faded chip because w2-kid is alive.
assert.deepStrictEqual(lead.children.map((n) => n.name), ['w1', 'w2']);
const w2 = lead.children[1];
assert.strictEqual(w2.ghost, true);
assert.strictEqual(w2.state, 'killed');
assert.deepStrictEqual(w2.children.map((n) => n.name), ['w2-kid']);
// w1: its reviewers fold into one deck; its work hangs off its chip.
const w1 = lead.children[0];
assert.deepStrictEqual(w1.children.map((n) => n.name), ['w1-kid']);
assert.strictEqual(w1.decks.length, 1);
assert.strictEqual(w1.decks[0].label, '보조 세션');
assert.deepStrictEqual(w1.decks[0].items.map((n) => n.name), ['w1-rev', 'w1-rev2']);
assert.deepStrictEqual(w1.issues.map((i) => i.id), ['c-1']);
assert.deepStrictEqual(w1.issues[0].children.map((i) => i.id), ['c-1.1']);
assert.strictEqual(w1.created, 1);
// Four held roots with nothing live under them fold into "쉬는 세션"; the
// parent loop still draws both of its sessions.
const nomesh = claunch.meshes[1];
assert.deepStrictEqual(nomesh.decks.map((d) => [d.label, d.count]), [['쉬는 세션', 4]]);
const drawn = [];
const walk = (n) => { drawn.push(n.name); n.children.forEach(walk); n.decks.forEach((d) => d.items.forEach(walk)); };
for (const p of model.projects) for (const m of p.meshes) { m.roots.forEach(walk); m.decks.forEach((d) => d.items.forEach(walk)); }
assert.deepStrictEqual(drawn.filter((n) => n.startsWith('loop')).sort(), ['loop-a', 'loop-b']);
assert.strictEqual(new Set(drawn).size, drawn.length, 'every session drawn once');
assert.ok(!drawn.includes('gone') && !drawn.includes('old'));
// The pool: work held by a session not drawn, work a person holds, and the
// unassigned issues by priority, nested by id.
assert.strictEqual(claunch.pool.length, 1);
const decks = claunch.pool[0].decks.map((d) => [d.label, d.count]);
assert.deepStrictEqual(decks, [['끝난 세션이 쥔 일감', 1], ['사람·외부 배정', 1], ['미배정 P1', 1], ['미배정 P3', 2]]);
const p3 = claunch.pool[0].decks[3];
assert.deepStrictEqual(p3.items.map((i) => i.id), ['c-5']);
assert.deepStrictEqual(p3.items[0].children.map((i) => i.id), ['c-5.1']);
assert.strictEqual(claunch.poolCount, 5);

// The default window hides every killed record; failed ones count toward
// the archive button with the killed ones.
const dflt = plain(P.buildModel([...fleet, s('f1', { status: 'exited', failed_at: iso(now) })], queues,
                                { now, meshesOf: (n) => MESH[n] || [] }));
assert.strictEqual(dflt.hiddenEnded, 2, 'gone and f1; w2 stays for its live child');
assert.strictEqual(dflt.archivable, 3);

// Narrowed to one project, the others are gone, the pool with them.
const gds = plain(P.buildModel(fleet, queues, { now, project: 'gds', meshesOf: (n) => MESH[n] || [] }));
assert.deepStrictEqual(gds.projects.map((p) => p.name), ['gds']);
assert.strictEqual(gds.projects[0].pool.length, 0);

// Switches: no work drawn at all; minor roles unstacked; parked unstacked.
const bare = plain(P.buildModel(fleet, queues, { now, issues: false, stackMinor: false, stackIdle: false,
                                                meshesOf: (n) => MESH[n] || [] }));
const bw1 = bare.projects[0].meshes[0].roots[0].children[0];
assert.deepStrictEqual(bw1.issues, []);
assert.deepStrictEqual(bw1.children.map((n) => n.name), ['w1-kid', 'w1-rev', 'w1-rev2']);
assert.strictEqual(bare.projects[0].pool.length, 0);
assert.strictEqual(bare.projects[0].meshes[1].decks.length, 0);

// Categories: an item's rows in /api/category become its paths; a second
// row for the same item (an item in two nodes) adds a second path; rows on
// unknown nodes are dropped. The model tags sessions and issues with them.
const trees = [{
  nodes: [
    { id: 'n1', parent_id: null, label: 'web', path: ['web'] },
    { id: 'n2', parent_id: 'n1', label: 'hive', path: ['web', 'hive'] },
    { id: 'n3', parent_id: null, label: 'beads', path: ['beads'] },
  ],
  items: [
    { kind: 'session', ref: 'w1', node_id: 'n2' },
    { kind: 'issue', ref: 'c-1', node_id: 'n2' },
    { kind: 'issue', ref: 'c-1', node_id: 'n3' },
    { kind: 'issue', ref: 'c-4', node_id: 'n1' },
    { kind: 'issue', ref: 'c-5', node_id: 'gone' },
  ],
}];
const cats = P.readCategories(trees);
assert.deepStrictEqual(plain([...cats]), [
  ['session:w1', ['web / hive']], ['issue:c-1', ['web / hive', 'beads']], ['issue:c-4', ['web']],
]);
assert.deepStrictEqual(plain(P.boardWorkspaces(queues)), ['claunch']);
const tagged = plain(P.buildModel(fleet, queues, { now, endedMs: 6 * H, categories: cats,
                                                  meshesOf: (n) => MESH[n] || [] }));
const tw1 = tagged.projects[0].meshes[0].roots[0].children[0];
assert.deepStrictEqual(tw1.categories, ['web / hive']);
assert.deepStrictEqual(tw1.issues[0].categories, ['web / hive', 'beads']);
assert.deepStrictEqual(tw1.issues[0].children[0].categories, [], 'c-1.1 is filed nowhere');
assert.deepStrictEqual(tagged.projects[0].meshes[0].roots[0].categories, [], 'lead is filed nowhere');
const tp1 = tagged.projects[0].pool[0].decks.find((d) => d.label === '미배정 P1');
assert.deepStrictEqual(tp1.items[0].categories, ['web']);
assert.deepStrictEqual(w1.categories, [], 'no categories given: none drawn');

// Relations (claunch-yzgqb.2): how a session is tied to an issue -- it
// holds it, was started on it (and holds it or nobody does), was started
// on it while another session holds it (joined), or filed it.
assert.deepStrictEqual(plain(P.relationKinds('w', 'c-1', { id: 'c-1', assignee: 'w' })), ['assignee', 'link']);
assert.deepStrictEqual(plain(P.relationKinds('r', 'c-1', { id: 'c-1', assignee: 'w' })), ['join']);
assert.deepStrictEqual(plain(P.relationKinds('r', 'c-1', { id: 'c-1' })), ['link']);
assert.deepStrictEqual(plain(P.relationKinds('l', '', { id: 'c-2', assignee: 'w', created_by: 'l' })), ['created']);
assert.deepStrictEqual(plain(P.relationKinds('l', '', { id: 'c-9' }, true)), ['created'], "a lane's created row");
assert.deepStrictEqual(plain(P.relationKinds('x', 'c-3', { id: 'c-1', assignee: 'w' })), []);
assert.deepStrictEqual(plain([...P.issueRows(queues).keys()]), ['c-1', 'c-1.1', 'c-9', 'c-2', 'c-3', 'c-4', 'c-5', 'c-5.1']);

// The fleet again, with ties: w1 was started on c-1 and holds it, w1-kid
// joined it, w1 filed c-9 (held by nobody drawn) and lead filed c-1.1,
// lead was started on c-4 (in the pool), h1 on an issue the board no
// longer lists.
const tied = fleet.map((x) => {
  if (x.name === 'w1') return { ...x, issue: 'c-1' };
  if (x.name === 'w1-kid') return { ...x, issue: 'c-1' };
  if (x.name === 'lead') return { ...x, issue: 'c-4' };
  if (x.name === 'h1') return { ...x, issue: 'c-gone' };
  return x;
});
const tq = plain(queues);
tq.boards[0].lanes[0].issues[1].created_by = 'lead';
const rel = plain(P.buildModel(tied, tq, { now, endedMs: 6 * H, meshesOf: (n) => MESH[n] || [] }));
const rlead = rel.projects[0].meshes[0].roots[0];
const rw1 = rlead.children[0];
const rkid = rw1.children[0];
assert.strictEqual(rkid.name, 'w1-kid');
// Every chip of an issue lists the drawn sessions tied to it.
assert.deepStrictEqual(rw1.issues[0].links, [
  { session: 'w1', kinds: ['assignee', 'link'] }, { session: 'w1-kid', kinds: ['join'] },
]);
assert.deepStrictEqual(rw1.issues[0].children[0].links, [
  { session: 'lead', kinds: ['created'] }, { session: 'w1', kinds: ['assignee'] },
]);
// What a session is tied to without holding it is a reference chip beside
// it, saying how; what it holds is not repeated there.
assert.deepStrictEqual(rkid.refs.map((r) => [r.id, r.own, r.assignee]), [['c-1', ['join'], 'w1']]);
assert.deepStrictEqual(rw1.refs.map((r) => [r.id, r.own]), [['c-9', ['created']]]);
assert.deepStrictEqual(rlead.refs.map((r) => [r.id, r.own]).sort(), [['c-1.1', ['created']], ['c-4', ['link']]]);
// The pool chip of c-4 says lead was started on it.
const rp1 = rel.projects[0].pool[0].decks.find((d) => d.label === '미배정 P1');
assert.deepStrictEqual(rp1.items[0].links, [{ session: 'lead', kinds: ['link'] }]);
// The detail pane's lists, by kind; a start issue the board does not list
// is kept as missing and draws no chip.
assert.deepStrictEqual(rw1.rels, { assignee: [{ id: 'c-1', missing: false }, { id: 'c-1.1', missing: false }],
                                   link: [{ id: 'c-1', missing: false }], created: [{ id: 'c-9', missing: false }] });
const rh1 = rel.projects[0].meshes[1].decks[0].items.find((n) => n.name === 'h1');
assert.deepStrictEqual(rh1.rels, { link: [{ id: 'c-gone', missing: true }] });
assert.deepStrictEqual(rh1.refs, []);
assert.deepStrictEqual(Object.keys(rel.relations).sort(), ['c-1', 'c-1.1', 'c-4', 'c-9']);
// Held work still counts once: references are not the session's queue.
assert.strictEqual(rel.projects[0].counts.issues, model.projects[0].counts.issues);
// With work switched off there are no ties either.
const rbare = plain(P.buildModel(tied, tq, { now, endedMs: 6 * H, issues: false, meshesOf: (n) => MESH[n] || [] }));
assert.deepStrictEqual(rbare.projects[0].meshes[0].roots[0].refs, []);
assert.deepStrictEqual(rbare.relations, {});

// An ended sibling group folds from three on, but one with work in hand
// keeps its own place.
const ended3 = P.arrangeSiblings([
  { name: 'a', state: 'killed', issues: [], children: [], decks: [] },
  { name: 'b', state: 'killed', issues: [], children: [], decks: [] },
  { name: 'c', state: 'failed', issues: [], children: [], decks: [] },
  { name: 'd', state: 'hold', issues: [{ id: 'x', children: [] }], children: [], decks: [] },
], 'k', true, true);
assert.deepStrictEqual(plain(ended3.decks).map((d) => [d.label, d.count]), [['끝난 세션', 3]]);
assert.deepStrictEqual(plain(ended3.nodes).map((n) => n.name), ['d']);

console.log('hivepage ok');
