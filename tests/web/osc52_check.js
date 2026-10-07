// OSC 52 from a session program reaches the clipboard of the computer the
// browser runs on (claunch-yr3yu). Slices the real block out of app.js and
// drives it against a stub xterm: the parser handler, the input gate (DOM
// user events only -- never xterm's own onData), the query/clear/oversize
// refusals, UTF-8 decoding, and the notice on refusal. Every copy taken is
// also posted to the daemon's history, which the Clipboard menu lists, and
// nothing refused is (claunch-fqw4k).
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');
const source = fs.readFileSync(path.join(__dirname, '../../src/claude_launcher/web/static/app.js'), 'utf8');

function fakeElement() {
  const e = { listeners: {} };
  e.addEventListener = (type, fn) => { (e.listeners[type] ||= []).push(fn); };
  e.fire = (type) => (e.listeners[type] || []).forEach((fn) => fn({ type }));
  return e;
}
// onData here is what xterm emits on its own -- query answers, focus and
// mouse-motion reports -- as well as keystrokes; the gate must not listen.
function fakeTerm() {
  const t = { dataHandlers: [], osc: {}, element: fakeElement() };
  t.onData = (fn) => { t.dataHandlers.push(fn); };
  t.parser = { registerOscHandler(code, fn) { t.osc[code] = fn; } };
  t.emit = (s) => t.dataHandlers.forEach((fn) => fn(s));
  t.type = (s) => { t.element.fire('keydown'); t.emit(s); };
  return t;
}
const sessionLine = fakeElement();
const b64 = (s) => Buffer.from(s, 'utf8').toString('base64');

const written = [];
const notices = [];
const posted = [];
let now = 1_000_000;
const ctx = vm.createContext({
  atob: (s) => { if (!/^[A-Za-z0-9+/]*={0,2}$/.test(s)) throw new Error('bad'); return Buffer.from(s, 'base64').toString('latin1'); },
  TextDecoder, Uint8Array,
  Date: { now: () => now },
  navigator: { clipboard: { writeText: async (text) => { written.push(text); } } },
  notify: (title, sub, opts) => { notices.push([title, opts && opts.key]); },
  api: async (path, opts) => {
    assert.equal(path, '/api/clipboard');
    assert.equal(opts.method, 'POST');
    posted.push(JSON.parse(opts.body).text);
    return { ok: true };
  },
  document: { body: {}, activeElement: null },
  $: (id) => (id === 'term-input' ? sessionLine : null),
  term: null,
});
vm.runInContext(source.slice(source.indexOf('async function copyToBrowserClipboard('), source.indexOf('async function deleteHostClipboard(')), ctx);

const flush = () => new Promise((r) => setImmediate(r));

(async () => {
  const term = fakeTerm();
  ctx.installOsc52(term);
  const osc = term.osc[52];
  assert.ok(osc, 'an OSC 52 handler is registered');

  // No input yet: bytes replayed on attach, or a second viewer, copy nothing.
  assert.equal(osc('c;' + b64('replayed')), true, 'the sequence is consumed either way');
  await flush();
  assert.deepEqual(written, [], 'no copy without input from this terminal');

  // xterm answering a query (DA1, DSR) or reporting focus/motion right before
  // the OSC 52 -- a replay or a watching viewer -- is not the person's input.
  for (const report of ['\x1b[?1;2c', '\x1b[1;1R', '\x1b[I', '\x1b[<35;1;1M']) term.emit(report);
  osc('c;' + b64('replayed after a query'));
  await flush();
  assert.deepEqual(written, [], 'terminal-generated onData does not open the gate');
  assert.deepEqual(notices, [], 'and raises no notice either');
  assert.deepEqual(posted, [], 'a copy the gate refused is not posted to the history');

  // The session line counts for the terminal on screen, and only for it.
  sessionLine.fire('keydown');
  osc('c;' + b64('from the session line, parked'));
  await flush();
  assert.deepEqual(written, [], 'a parked terminal is not opened by the session line');
  ctx.term = term;
  osc('c;' + b64('/copy via the session line'));
  await flush();
  assert.deepEqual(written, ['/copy via the session line']);
  assert.deepEqual(posted, ['/copy via the session line'], 'the copy taken is posted to the history');
  ctx.term = null;
  now += vm.runInContext('OSC52_INPUT_MS', ctx) + 1;
  written.length = 0;

  // A press on the terminal (mouse selection copy) opens it.
  term.element.fire('pointerdown');
  osc('c;' + b64('selected'));
  await flush();
  assert.deepEqual(written, ['selected']);
  written.length = 0;

  // A keystroke, then the program's copy: UTF-8 text lands as written.
  term.type('\r');
  const text = '한글 <copy>\nline two';
  osc('c;' + b64(text));
  await flush();
  assert.deepEqual(written, [text]);

  // Empty targets field and base64 wrapped over lines are still a write.
  osc(';' + b64('plain').replace(/(.{2})/, '$1\n'));
  await flush();
  assert.equal(written.at(-1), 'plain');

  // A query never reads the clipboard back; a clear does not clear; garbage
  // and invalid UTF-8 are dropped silently.
  const count = written.length;
  for (const data of ['c;?', 'c;', 'nosemicolon', 'c;!!!', 'c;' + Buffer.from([0xff, 0xfe]).toString('base64')]) {
    assert.equal(osc(data), true);
  }
  await flush();
  assert.equal(written.length, count, 'query/clear/invalid payloads write nothing');
  assert.deepEqual(notices, []);
  const postedCount = posted.length;

  // Oversized: refused with a notice, nothing written.
  osc('c;' + 'A'.repeat(vm.runInContext('OSC52_MAX_BASE64', ctx) + 4));
  await flush();
  assert.equal(written.length, count);
  assert.deepEqual(notices.at(-1), ['Copy not taken', 'osc52']);
  assert.equal(posted.length, postedCount, 'query/clear/invalid/oversized payloads are not posted');

  // The gate closes OSC52_INPUT_MS after the last input.
  now += vm.runInContext('OSC52_INPUT_MS', ctx) + 1;
  osc('c;' + b64('late'));
  await flush();
  assert.equal(written.length, count, 'no copy long after the last input');
  assert.equal(posted.length, postedCount, 'nor posted');

  // Browser refuses both routes (plain http, no activation): a notice says so.
  term.type('x');
  ctx.navigator.clipboard = undefined;
  ctx.document.createElement = () => ({ style: {}, setAttribute() {}, select() {}, setSelectionRange() {}, remove() {} });
  ctx.document.body.appendChild = () => {};
  ctx.document.execCommand = () => false;
  osc('c;' + b64('refused'));
  await flush();
  assert.deepEqual(notices.at(-1), ["Copy did not reach this computer's clipboard", 'osc52']);
  assert.equal(posted.at(-1), 'refused', "a copy the browser refused is still posted: the menu's Copy reaches it");

  // The execCommand fallback that succeeds raises no notice.
  const before = notices.length;
  ctx.document.execCommand = () => true;
  osc('c;' + b64('fallback'));
  await flush();
  assert.equal(notices.length, before);

  // Both terminals that take input install it; the read-only shadow does not.
  assert.match(source, /term\.onData\(sendInput\);[\s\S]{0,200}installOsc52\(term\)/);
  assert.match(source, /installOsc52\(cliTerm\)/);
  // A history the daemon cannot take (offline, 413) leaves the copy alone.
  ctx.api = async () => { throw new Error('offline'); };
  ctx.document.execCommand = () => true;
  osc('c;' + b64('offline'));
  await flush();
  assert.equal(notices.length, before, 'a failed post raises no notice');
  console.log('osc52: input gate, UTF-8 write, query/clear/invalid ignored, size cap, refusal notice, history post, both terminals wired passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
