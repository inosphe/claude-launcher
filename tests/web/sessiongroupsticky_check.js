/* Nested mesh/workspace headers share the rail's sticky lane.  The offset is
   the measured height of each outer level, so differing type scales do not
   make a workspace heading cover its mesh heading. */
const fs = require("fs");
const path = require("path");

const root = path.join(__dirname, "..", "..");
const src = fs.readFileSync(path.join(
  root, "src", "claude_launcher", "web", "static", "app.js"), "utf8");
const css = fs.readFileSync(path.join(
  root, "src", "claude_launcher", "web", "static", "style.css"), "utf8");

const start = src.indexOf("function sessionGroupStickyTops(");
const end = src.indexOf("function setSessionGroup(", start);
if (start < 0 || end <= start) throw new Error("cannot locate sticky group helpers");

const ctx = {};
new Function("exports", src.slice(start, end) +
  "\nexports.tops = sessionGroupStickyTops;")(ctx);

function heading(level, height) {
  return {
    dataset: { groupLevel: String(level) },
    getBoundingClientRect: () => ({ height }),
    style: { setProperty() {} },
  };
}

let failures = 0;
function check(what, got, want) {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    console.error(`FAIL ${what}\n  got  ${JSON.stringify(got)}` +
                  `\n  want ${JSON.stringify(want)}`);
    failures++;
  }
}

check("each nested level starts below all outer sticky headers",
      ctx.tops([heading(0, 27), heading(1, 25), heading(2, 21)]),
      [0, 27, 52]);
check("a later outer group restarts the sticky stack",
      ctx.tops([heading(0, 27), heading(1, 25), heading(0, 27), heading(1, 25)]),
      [0, 27, 0, 27]);
check("group headings are contained by nested group elements",
      /#session-list \.session-group\s*\{[^}]*display:\s*block/.test(css) &&
      /#session-list \.session-group-body\s*\{[^}]*list-style:\s*none/.test(css), true);
check("nested groups append to the previous level body",
      /const parent = groupBodies\[row\.level - 1\] \|\| list;/.test(src), true);
check("group headings use the computed sticky offset",
      /#session-list \.session-group-heading\s*\{[^}]*position:\s*sticky[^}]*top:\s*var\(--session-group-sticky-top/.test(css), true);

if (failures) process.exit(1);
console.log("sessiongroupsticky_check: ok");
