// A harness that copies only onto the daemon machine's clipboard (Codex,
// `copy_transport: host`) writes no OSC 52, so the page relays the daemon's
// clipboard history instead (claunch-tn5ba). Slices the real block out of
// app.js and drives it against stub terminals and a stub /api/clipboard:
// the keys that start a watch (Enter, Ctrl+O; not Shift+Enter or typing),
// the harness gate, the baseline taken at the key, host-only entries, the
// watch window, the parked-terminal and session-switch guards, and the notice
// when the browser refuses the write.
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
const ctx = vm.createContext({
  TextDecoder, Uint8Array,
  Date: { now: () => now },
  setTimeout: (fn, ms) => { now += ms; setImmediate(fn); },
  navigator: { clipboard: { writeText: async (text) => { written.push(text); } } },
  notify: (title, sub, opts) => { notices.push([title, opts && opts.key]); },
  api: async (url, opts) => {
    assert.equal(url, '/api/clipboard');
    assert.ok(!opts, 'the watch only reads the history');
    gets++;
    const items = feed.length > 1 ? feed.shift() : feed[0];
    return { ok: true, json: async () => ({ items }) };
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
const host = (id, text) => ({ id: String(id), text, source: 'host' });
const browser = (id, text) => ({ id: String(id), text, source: 'browser' });
const enter = { key: 'Enter' };

(async () => {
  const term = fakeTerm();
  ctx.term = term;
  ctx.installHostCopyRelay(term);
  const watchMs = vm.runInContext('HOST_COPY_WATCH_MS', ctx);

  // Enter in a Codex session: the first read is the baseline, the host entry
  // that appears after it goes onto this computer's clipboard, once.
  feed = [[host(5, 'older')], [host(5, 'older')], [host(6, 'copied'), host(5, 'older')]];
  term.element.fire('keydown', enter);
  await settle();
  assert.deepEqual(written, ['copied']);
  assert.equal(vm.runInContext('hostCopyWatch', ctx), null, 'the watch ends at the first relayed copy');
  written.length = 0;

  // An entry the page posted itself (an OSC 52 copy) is on the browser
  // already; the host entry after it is the one relayed.
  feed = [[host(6, 'copied')], [browser(7, 'osc52'), host(6, 'copied')],
    [host(8, 'next'), browser(7, 'osc52'), host(6, 'copied')]];
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
  feed = [[host(8, 'next')]];
  term.element.fire('keydown', enter);
  await settle();
  assert.deepEqual(written, []);
  assert.equal(vm.runInContext('hostCopyWatch', ctx), null, 'the watch stops after the window');
  assert.ok(gets <= watchMs / vm.runInContext('HOST_COPY_POLL_MS', ctx) + 2, `bounded reads (${gets})`);

  // The session line's Enter counts for the session on screen.
  feed = [[host(8, 'next')], [host(9, 'from the line'), host(8, 'next')]];
  sessionLine.fire('keydown', enter);
  await settle();
  assert.deepEqual(written, ['from the line']);
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
  feed = [[host(9, 'x')], [host(10, 'for cx'), host(9, 'x')]];
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
  feed = [[host(10, 'for cx')], [host(11, 'refused'), host(10, 'for cx')]];
  term.element.fire('keydown', enter);
  await settle();
  assert.deepEqual(notices.at(-1), ["Copy did not reach this computer's clipboard", 'osc52']);

  // The session terminal is wired; the CLI tab is not (it runs no harness).
  assert.match(source, /installOsc52\(term\);\s*\n\s*if \(typeof installHostCopyRelay === "function"\) installHostCopyRelay\(term\);/);
  assert.doesNotMatch(source, /installHostCopyRelay\(cliTerm\)/);
  console.log('hostcopy: copy keys, harness gate, baseline, host-only entries, window, parked/switch guards, refusal notice passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
