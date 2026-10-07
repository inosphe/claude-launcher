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

// With tracking on, every mouse report is user input and SelectionService
// clears its selection on user input. Under ?1003 (any-motion, which Claude
// Code turns on) the first buttonless move after a forced drag was a report,
// and so was a right-click: the selection was gone before the context menu
// could copy it. The local patch to bindMouse() (vendor/VENDORED) sends
// neither report while there is a selection; a left click still reports.
const moveSrc = vendor.match(/mousemove:(e=>\{e\.buttons\|\|this\._selectionService\.hasSelection\|\|i\(e\)\})/);
assert.ok(moveSrc, 'vendored bindMouse() does not report a buttonless move over a selection (vendor/VENDORED)');
const downSrc = vendor.match(/addDisposableListener\)\(t,"mousedown",(\(e=>\{if\(e\.preventDefault\(\),this\.focus\(\),this\.coreMouseService\.areMouseEventsActive&&!this\._selectionService\.shouldForceSelection\(e\)&&!\(2===e\.button&&this\._selectionService\.hasSelection\)\)return i\(e\).{0,200}?this\.cancel\(e\)\}\))/);
assert.ok(downSrc, 'vendored bindMouse() does not report a right-click over a selection (vendor/VENDORED)');
const bind = vm.runInNewContext(`(function(i,s){return {mousemove:${moveSrc[1]},mousedown:${downSrc[1]}}})`);
function tracking(selected) {
  const state = { selected, reports: 0 };
  const self = {
    _selectionService: {
      get hasSelection() { return state.selected; },
      shouldForceSelection(e) { return !!e.shiftKey; },
    },
    coreMouseService: { areMouseEventsActive: true },
    _document: { addEventListener() {} },
    focus() {},
    cancel() {},
  };
  // A report is user input; user input clears the selection.
  const report = () => { state.reports++; state.selected = false; return true; };
  const handlers = bind.call(self, report, { mouseup: null, mousedrag: null });
  const ev = (o) => Object.assign({ buttons: 0, button: 0, shiftKey: false, preventDefault() {} }, o);
  return { state, handlers, ev };
}
{
  const { state, handlers, ev } = tracking(true);
  handlers.mousemove(ev({}));
  assert.equal(state.reports, 0, 'hovering over a forced selection sends no report');
  handlers.mousedown(ev({ button: 2, buttons: 2 }));
  assert.equal(state.reports, 0, 'a right-click over a selection goes to the browser, not the program');
  assert.equal(state.selected, true, 'the selection is still there for the context menu to copy');
  handlers.mousedown(ev({ button: 0, buttons: 1 }));
  assert.equal(state.reports, 1, 'a left click still reports, and so clears the selection');
  assert.equal(state.selected, false);
}
{
  const { state, handlers, ev } = tracking(false);
  handlers.mousemove(ev({}));
  handlers.mousedown(ev({ button: 2, buttons: 2 }));
  assert.equal(state.reports, 2, 'without a selection, moves and right-clicks reach the program as upstream');
}

console.log('termselect: session and CLI terminals force selection on Option (Mac) / Shift (vendored rule), kept across re-sent mouse modes, hover and right-click passed');
