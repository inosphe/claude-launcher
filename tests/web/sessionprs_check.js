const fs = require("fs");
const path = require("path");
const assert = require("assert");
const src = fs.readFileSync(path.join(__dirname, "../../src/claude_launcher/web/static/app.js"), "utf8");
const start = src.indexOf("function sessionPrRows(");
const end = src.indexOf("function sessDetailTabs(", start);
function el(tag, className, textContent) {
  return {tag, className, textContent, children: [],
    appendChild(child) { this.children.push(child); }};
}
const api = new Function("el", src.slice(start, end)
  + "return {sessionPrBadge, sessionPrPanel};")(el);
assert.equal(api.sessionPrBadge({}), null);
assert.equal(api.sessionPrPanel({prs: []}), null);
const pr = {url: "https://ghe.example.com/o/r/pull/7", number: 7,
  repo: "ghe.example.com/o/r", title: "<script>literal title</script>",
  branch: "s1-feature", base: "master", status: "merged", issues: ["issue-1"]};
assert.equal(api.sessionPrBadge({prs: [pr]}).textContent, "PR #7");
assert.equal(api.sessionPrBadge({prs: [pr, {...pr, number: 8}]}).textContent, "PR ×2");
const panel = api.sessionPrPanel({prs: [pr]});
const row = panel.children[2];
const link = row.children[0];
assert.equal(link.href, pr.url);
assert.equal(link.target, "_blank");
assert.equal(link.rel, "noopener noreferrer");
assert(link.textContent.includes(pr.title));
assert(row.children[2].textContent.includes("s1-feature → master"));
assert(row.children[2].textContent.includes("merged"));
for (const url of ["javascript:alert(1)", "https://u:p@ghe.example.com/o/r/pull/7", "https://ghe.example.com/o/r/issues/7"]) {
  assert.equal(api.sessionPrBadge({prs: [{...pr, url}]}), null);
  assert.equal(api.sessionPrPanel({prs: [{...pr, url}]}), null);
}
assert(src.includes("const prBadge = sessionPrBadge(s)"));
assert(src.includes("const prs = sessionPrPanel(s)"));
console.log("session PR badge and details: passed");
