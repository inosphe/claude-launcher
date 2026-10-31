/* The classic scripts index.html loads share one global lexical scope
   (claunch-7jzp8.5.1). A top-level const/let/class in one file that reuses a
   name another file already declared makes the later script fail with
   "SyntaxError: Identifier 'X' has already been declared" before any of it
   runs. plan-page.js and plan-pane.js both declared `const PLAN_VIA`, and the
   session's plan pane drew nothing; each file's own harness loads that file
   alone, so none of them saw it.

   This loads every script index.html names, in page order, into one vm
   context, which is what the browser does. The engine raises that error while
   it instantiates a script's global declarations, before the script's first
   statement, so the check needs no DOM: whatever a script throws once it runs
   (no `document` here) is not this error and is ignored. */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const STATIC = path.join(__dirname, '..', '..', 'src', 'claude_launcher', 'web', 'static');
const html = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');

const tags = [...html.matchAll(/<script\b([^>]*)>/g)].map((m) => m[1]);
assert.ok(!tags.some((a) => /type\s*=\s*["']?module/.test(a)),
          'a module script has its own scope; this check reads classic scripts only');
const srcs = tags.map((a) => (a.match(/\bsrc="static\/([^"?#]+)/) || [])[1]).filter(Boolean);
assert.ok(srcs.length > 10, `index.html names ${srcs.length} scripts; the pattern no longer matches it`);

// Code a script starts after its first error (a rejected promise, a timer the
// context cannot schedule) is not what this check measures.
process.on('unhandledRejection', () => {});

const context = vm.createContext({});
const clashes = [];
for (const src of srcs) {
  const file = path.join(STATIC, src);
  assert.ok(fs.existsSync(file), `index.html names static/${src}, which does not exist`);
  try {
    new vm.Script(fs.readFileSync(file, 'utf8'), {filename: src}).runInContext(context, {timeout: 2000});
  } catch (err) {
    const message = String(err && err.message);
    if (err && err.name === 'SyntaxError' && /has already been declared/.test(message)) {
      clashes.push(`${src}: ${message}`);
    } else if (err && err.name === 'SyntaxError') {
      clashes.push(`${src}: does not parse: ${message}`);
    }
  }
}

assert.deepStrictEqual(clashes, [],
  'scripts in index.html redeclare a top-level name another script already declared:\n' + clashes.join('\n'));
console.log(`globalscope_check: ${srcs.length} scripts share the global scope without a clash`);
