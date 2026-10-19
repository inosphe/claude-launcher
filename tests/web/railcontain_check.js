/* Rail rows scrolled out of view skip layout and paint (claunch-oo8au.1),
   read off the real style.css.

   What has to hold: a session row (li[data-name]) carries
   `content-visibility: auto` with a remembered intrinsic size, so an
   off-screen row costs no layout or paint and the scrollbar does not jump
   once the row has been seen; and the rule does not reach the group
   containers, whose sticky heading would stop sticking inside a contained
   box. */
const fs = require("fs");
const path = require("path");
const css = fs.readFileSync(
  process.env.RAILCONTAIN_STYLE_CSS || path.join(
    __dirname, "..", "..", "src", "claude_launcher", "web", "static", "style.css"),
  "utf8"
).replace(/\/\*[\s\S]*?\*\//g, "");

let failures = 0;
function check(what, got, want) {
  if (got !== want) {
    console.error(`FAIL ${what}\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`);
    failures++;
  }
}

/* Every rule whose declarations include content-visibility, by selector. */
const rules = [];
for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
  if (/content-visibility\s*:/.test(m[2])) rules.push({ sel: m[1].trim(), body: m[2] });
}
const row = rules.find((r) => r.sel.split(",").some((s) => s.trim() === "#session-list li[data-name]"));
check("a rule gives session rows content-visibility", !!row, true);
if (row) {
  check("...set to auto", /content-visibility\s*:\s*auto\s*(;|$)/.test(row.body), true);
  check("...with a remembered intrinsic size",
        /contain-intrinsic-size\s*:\s*auto\s+\d+px/.test(row.body), true);
}
check("no rule contains the group containers or the whole list",
      rules.some((r) => /session-group|#session-list\s*(li)?\s*$/.test(r.sel)
        && !r.sel.includes("[data-name]")), false);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("railcontain_check: ok");
