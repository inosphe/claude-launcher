// A modifier+drag selects text in the browser even while the session program
// has mouse tracking on (claunch-ekrwr): Shift on Windows/Linux is xterm's
// default, Option on a Mac needs macOptionClickForcesSelection. Checks that
// both terminals that take input set the flag, and that the vendored xterm
// still decides forced selection the way that flag assumes.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const vm = require('vm');
const root = path.join(__dirname, '../../src/claude_launcher/web/static');
const source = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const vendor = fs.readFileSync(path.join(root, 'vendor/xterm.js'), 'utf8');

// The options object of the `new Terminal({...})` assigned to `name`.
function terminalOptions(name) {
  const start = source.indexOf(`${name} = new Terminal({`);
  assert.ok(start >= 0, `${name} is built with new Terminal`);
  const end = source.indexOf('});', start);
  return source.slice(start, end);
}

for (const name of ['term', 'cliTerm']) {
  assert.match(terminalOptions(name), /macOptionClickForcesSelection:\s*true/,
    `${name} lets Option+drag select on a Mac`);
}

// The vendored rule: on a Mac, Alt(Option) forces selection only with the
// flag; elsewhere Shift does. If an xterm upgrade changes this, the flag above
// may no longer mean what the comment in app.js says.
assert.match(vendor,
  /shouldForceSelection\(\w\)\{return \w+\.isMac\?\w\.altKey&&this\._optionsService\.rawOptions\.macOptionClickForcesSelection:\w\.shiftKey\}/,
  'vendored xterm forces selection with Shift, or Option+flag on a Mac');

// A forced selection survives the program re-sending its mouse modes
// (claunch-xa0j7). Every DECSET 9/1000/1002/1003 fires onProtocolChange, even
// for the protocol already active, and with tracking on that handler calls
// the selection service's disable() -- upstream, disable() clears the
// selection, so a Shift/Option+drag vanished mid-drag whenever Claude Code
// redrew. The vendored file carries a local patch (see vendor/VENDORED):
// disabling an already disabled service keeps the selection.
assert.ok(/this\.coreMouseService\.onProtocolChange\(\(e=>\{e\?\(.{0,200}?this\.element\.classList\.add\("enable-mouse-events"\),this\._selectionService\.disable\(\)\)/.test(vendor),
  'turning mouse tracking on (again) disables the selection service');
assert.ok(/set activeProtocol\(e\)\{.{0,120}?this\._activeProtocol=e,this\._onProtocolChange\.fire\(/.test(vendor),
  'the activeProtocol setter fires onProtocolChange without comparing to the old value');
const disableSrc = vendor.match(/disable\(\)\{this\._enabled&&this\.clearSelection\(\),this\._enabled=!1\}/);
assert.ok(disableSrc, 'vendored SelectionService.disable() carries the claunch-xa0j7 patch (vendor/VENDORED)');
const service = vm.runInNewContext(`({${disableSrc[0]}})`);
function run(enabled) {
  const state = { _enabled: enabled, cleared: 0, clearSelection() { this.cleared++; } };
  service.disable.call(state);
  return state;
}
const again = run(false);
assert.equal(again.cleared, 0, 'a mode re-sent while tracking is on keeps the forced selection');
assert.equal(again._enabled, false);
const first = run(true);
assert.equal(first.cleared, 1, 'tracking turning on still clears an ordinary selection, as upstream');
assert.equal(first._enabled, false);

console.log('termselect: session and CLI terminals force selection on Option (Mac) / Shift (vendored rule), kept across re-sent mouse modes passed');
