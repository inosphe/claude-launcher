/* Diagnostic for claunch-70py6, not a passing regression test.
 * Run: node tools/diagnose_ime.cjs [path/to/xterm.js]
 * Exit 1 means lost text was reproduced; exit 0 means all cases preserved it.
 * Synthetic helper events do not establish a browser/OS IME's event order.
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const bundle = path.resolve(process.argv[2] || path.join(__dirname,
  '../src/claude_launcher/web/static/vendor/xterm.js'));
const source = fs.readFileSync(bundle, 'utf8');
const head = 'class{get isComposing(){return this._isComposing}constructor(';
const start = source.indexOf(head);
if (start < 0) throw new Error('CompositionHelper not found');
let depth = 0, end = -1;
for (let i = source.indexOf('{', start + 5); i < source.length; i++) {
  if (source[i] === '{') depth++;
  if (source[i] === '}' && --depth === 0) { end = i + 1; break; }
}
if (end < 0) throw new Error('CompositionHelper is incomplete');
const body = source.slice(start, end);
const dependency = body.match(/([A-Za-z$_]+)\.C0\.DEL/);
let losses = 0;
for (const drainBetween of [true, false]) {
  for (const keyCode of [null, 32, 13, 190]) {
    const timers = [], chunks = [];
    const scope = { setTimeout: callback => timers.push(callback) };
    if (dependency) scope[dependency[1]] = { C0: { DEL: '\x7f' } };
    const Helper = vm.runInNewContext(`(${body})`, scope);
    const textarea = { value: '', selectionStart: 0, selectionEnd: 0 };
    const helper = new Helper(textarea,
      { textContent: '', style: {}, classList: { add() {}, remove() {} } },
      { buffer: { isCursorInViewport: false } }, { rawOptions: {} },
      { triggerDataEvent: text => chunks.push(text) }, {});
    const drain = () => { while (timers.length) timers.shift()(); };
    // First syllable's update settles; its commit and the next syllable can
    // then arrive before the deferred send gets its turn.
    helper.compositionstart();
    helper.compositionupdate({ data: '가' });
    textarea.value = '가';
    drain();
    helper.compositionend();
    if (drainBetween) drain();
    helper.compositionstart();
    helper.compositionupdate({ data: '나' });
    textarea.value = '가나';
    if (drainBetween) drain();
    helper.compositionend();
    if (drainBetween) drain();
    if (keyCode !== null) helper.keydown({ keyCode });
    drain();
    const actual = chunks.join('');
    const lost = actual !== '가나';
    losses += Number(lost);
    console.log(JSON.stringify({ drainBetween, keyCode,
      expected: '가나', actual, chunks, lost }));
  }
}
console.log(`Text loss in ${losses}/8 synthetic schedules`);
process.exitCode = losses ? 1 : 0;
