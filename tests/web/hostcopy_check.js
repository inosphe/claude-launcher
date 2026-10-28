// A harness that copies only onto the daemon machine's clipboard (Codex,
// `copy_transport: host`) writes no OSC 52, so the page relays the daemon's
// clipboard history instead (claunch-tn5ba). Slices the real block out of
// app.js and drives it against stub terminals and a stub /api/clipboard:
// the keys that start a watch (Enter, Ctrl+O; not Shift+Enter or typing),
// the harness gate, the baseline held from before the key (a copy the
// daemon recorded before the first response is still relayed), the age
// guard on entries an old baseline lets through, host-only entries, the
// watch window, the parked-terminal and session-switch guards, and the
// notice when the browser refuses the write.
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');
const source = fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/app.js'), 'utf8');

function fakeElement() {
  const e = { listeners: {} };
  e.addEventListener = (type, fn) => { (e.listeners[type] ||= []).push(fn); };
  e.fire = (type, init = {}) => (e.listeners[type] || []).forEach((fn) => fn({ type, ...init }));
  return e;
}
function fakeTerm() {
  const t = { element: fakeElement() };
  t.parser = { registerOscHandler() {} };
  return t;
}
const sessionLine = fakeElement();

const written = [];
const notices = [];
let gets = 0;
let feed = [];          // successive /api/clipboard item lists; the last repeats
let now = 1_000_000;
let epoch = 'first';    // the history's epoch; a restarted daemon has a new one
const ctx = vm.createContext({
  TextDecoder, Uint8Array,
  Date: { now: () => now, parse: (text) => Number(text) },
  setTimeout: (fn, ms) => { now += ms; setImmediate(fn); },
  navigator: { clipboard: { writeText: async (text) => { written.push(text); } } },
  notify: (title, sub, opts) => { notices.push([title, opts && opts.key]); },
  api: async (url, opts) => {
    assert.equal(url, '/api/clipboard');
    assert.ok(!opts, 'the watch only reads the history');
    gets++;
    const items = feed.length > 1 ? feed.shift() : feed[0];
    // The stub's times are Date.now() numbers, so Date.parse is Number.
    return { ok: true, json: async () => ({ items, epoch, now: String(now) }) };
  },
  document: { body: {}, activeElement: null },
  $: (id) => (id === 'term-input' ? sessionLine : null),
  term: null,
  currentName: 'cx',
  sessionsCache: [{ name: 'cx', harness: 'codex' }, { name: 'cl', harness: 'claude' }],
  harnessDetails: { codex: { copy_transport: 'host' }, claude: { copy_transport: 'terminal' } },
});
vm.runInContext(source.slice(source.indexOf('async function copyToBrowserClipboard('), source.indexOf('async function deleteHostClipboard(')), ctx);

const settle = async () => {
  for (let i = 0; i < 40; i++) await new Promise((r) => setImmediate(r));
};
const host = (id, text, at = now) => ({ id: String(id), text, source: 'host', copied_at: String(at) });
const browser = (id, text) => ({ id: String(id), text, source: 'browser', copied_at: String(now) });
const enter = { key: 'Enter' };

(async () => {
  const term = fakeTerm();
  ctx.term = term;
  ctx.installHostCopyRelay(term);
  const watchMs = vm.runInContext('HOST_COPY_WATCH_MS', ctx);

  // The page's first watch has no id from before the key, so it takes any
  // recent host entry -- here one the daemon recorded before the first
  // response -- and skips one copied long before; it is relayed once.
  const recent = vm.runInContext('HOST_COPY_RECENT_MS', ctx);
  feed = [[host(6, 'copied'), host(5, 'older', now - recent - 1000)]];
  term.element.fire('keydown', enter);
  await settle();
  assert.deepEqual(written, ['copied'], 'a first watch relays a copy already in its first response');
  assert.equal(vm.runInContext('hostCopyWatch', ctx), null, 'the watch ends at the first relayed copy');
  assert.equal(vm.runInContext('hostCopySeen.id', ctx), 6, 'the newest id read is kept for the next key');
  written.length = 0;

  // The daemon recorded the copy before the watch's first response (its own
  // once-a-second sample): the baseline is the id held from before the key,
  // so the very first read relays it.
  feed = [[host(7, 'raced'), host(6, 'copied')]];
  term.element.fire('keydown', enter);
  await settle();
  assert.deepEqual(written, ['raced'], 'a copy already in the first response is relayed');
  written.length = 0;

  // An entry the page posted itself (an OSC 52 copy) is on the browser
  // already; the host entry after it is the one relayed.
  feed = [[browser(8, 'osc52'), host(7, 'raced')],
    [host(9, 'next'), browser(8, 'osc52'), host(7, 'raced')]];
  term.element.fire('keydown', { key: 'o', ctrlKey: true });
  await settle();
  assert.deepEqual(written, ['next'], 'Ctrl+O starts a watch; browser entries are skipped');
  written.length = 0;

  // Typing, Shift+Enter and an IME Enter start nothing.
  gets = 0;
  for (const init of [{ key: 'a' }, { key: 'Enter', shiftKey: true }, { key: 'Enter', isComposing: true }, { key: 'o' }]) {
    term.element.fire('keydown', init);
  }
  await settle();
  assert.equal(gets, 0, 'no history read without a copy key');

  // Nothing new inside the window: nothing written, and the polling stops.
  feed = [[host(9, 'next')]];
  term.element.fire('keydown', enter);
  await settle();
  assert.deepEqual(written, []);
  assert.equal(vm.runInContext('hostCopyWatch', ctx), null, 'the watch stops after the window');
  assert.ok(gets <= watchMs / vm.runInContext('HOST_COPY_POLL_MS', ctx) + 2, `bounded reads (${gets})`);

  // The session line's Enter counts for the session on screen.
  feed = [[host(9, 'next')], [host(10, 'from the line'), host(9, 'next')]];
  sessionLine.fire('keydown', enter);
  await settle();
  assert.deepEqual(written, ['from the line']);
  written.length = 0;

  // An entry newer than the held id but copied long before this response
  // (someone at the daemon's machine, between two Enters) is not relayed;
  // a recent one after it still is.
  feed = [[host(11, 'stale', now - recent - 1000), host(10, 'from the line')],
    [host(12, 'fresh'), host(11, 'stale', now - recent - 1000)]];
  term.element.fire('keydown', enter);
  await settle();
  assert.deepEqual(written, ['fresh'], 'an old entry an old baseline lets through is skipped');
  written.length = 0;

  // The daemon restarted: its history counts from one again under a new
  // epoch, so the id held from the old run (12) does not hide its copies.
  epoch = 'second';
  feed = [[host(1, 'after restart')]];
  term.element.fire('keydown', enter);
  await settle();
  assert.deepEqual(written, ['after restart'], 'a restarted daemon\'s copy is relayed under the new epoch');
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(hostCopySeen)', ctx)), { epoch: 'second', id: 1 },
    'the held id is the new epoch\'s');
  written.length = 0;

  // A harness that copies through OSC 52 (Claude Code) is not watched.
  ctx.currentName = 'cl';
  gets = 0;
  term.element.fire('keydown', enter);
  sessionLine.fire('keydown', enter);
  await settle();
  assert.equal(gets, 0, 'copy_transport terminal reads no history');
  // Nor is an unknown harness detail (an older daemon).
  ctx.harnessDetails = {};
  ctx.currentName = 'cx';
  term.element.fire('keydown', enter);
  await settle();
  assert.equal(gets, 0, 'an undeclared transport reads no history');
  ctx.harnessDetails = { codex: { copy_transport: 'host' } };

  // A parked terminal's keys start nothing: the watch is for the one on screen.
  const parked = fakeTerm();
  ctx.installHostCopyRelay(parked);
  parked.element.fire('keydown', enter);
  await settle();
  assert.equal(gets, 0, 'a parked terminal does not start a watch');

  // Switching sessions mid-watch drops it.
  feed = [[host(12, 'x')], [host(13, 'for cx'), host(12, 'x')]];
  term.element.fire('keydown', enter);
  ctx.currentName = 'cl';
  await settle();
  assert.deepEqual(written, [], 'a copy is not relayed into another session view');
  ctx.currentName = 'cx';

  // The browser refuses the write: a notice points at the Clipboard menu.
  ctx.navigator.clipboard = undefined;
  ctx.document.createElement = () => ({ style: {}, setAttribute() {}, select() {}, setSelectionRange() {}, remove() {} });
  ctx.document.body.appendChild = () => {};
  ctx.document.execCommand = () => false;
  feed = [[host(12, 'x')], [host(14, 'refused'), host(12, 'x')]];
  term.element.fire('keydown', enter);
  await settle();
  assert.deepEqual(notices.at(-1), ["Copy did not reach this computer's clipboard", 'osc52']);

  // The session terminal is wired; the CLI tab is not (it runs no harness).
  assert.match(source, /installOsc52\(term\);\s*\n\s*if \(typeof installHostCopyRelay === "function"\) installHostCopyRelay\(term\);/);
  assert.doesNotMatch(source, /installHostCopyRelay\(cliTerm\)/);
  console.log('hostcopy: copy keys, harness gate, held baseline, raced copy, age guard, restart epoch, host-only entries, window, parked/switch guards, refusal notice passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
