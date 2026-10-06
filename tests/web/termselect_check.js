// A modifier+drag selects text in the browser even while the session program
// has mouse tracking on (claunch-ekrwr): Shift on Windows/Linux is xterm's
// default, Option on a Mac needs macOptionClickForcesSelection. Checks that
// both terminals that take input set the flag, and that the vendored xterm
// still decides forced selection the way that flag assumes.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
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

console.log('termselect: session and CLI terminals force selection on Option (Mac) / Shift (vendored rule) passed');
