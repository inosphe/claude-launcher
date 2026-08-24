/* The "claunch" wordmark at the top of the rail is the root-page link: a
   nav item's worth of behavior (route back to "#/") without a nav item's
   worth of chrome. Checked as markup, not as a JS function, because there
   is no logic here — the existing hash router (already exercised for
   #rail-nav a) does the routing; the only new claims are the anchor's
   presence/target and that CSS strips the link's default look. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web",
                         "static");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

const railTitleStart = html.indexOf('id="rail-title"');
check("rail-title exists", railTitleStart >= 0);
const railTitleEnd = html.indexOf("</div>", railTitleStart);
const railTitle = html.slice(railTitleStart, railTitleEnd);

check("wordmark links to the root route", /<a\s+href="#\/"[^>]*>\s*claunch\s*<\/a>/.test(railTitle),
  railTitle);

check("style.css leaves it looking like the title, not a browser link",
  /#sidebar h1 a\s*\{[^}]*text-decoration:\s*none/.test(css));

if (failures) {
  console.log(`${failures} failure(s)`);
  process.exit(1);
}
console.log("ok");
