/* The Operator is v2 only (claunch-iw5i8.6): the floating Operator
   (operator_fab.js) and the operators section of the Observer page
   (observer_operator.js). Operator v1 — its page, script, stylesheet, nav
   entry and HTTP routes — is gone.

   What has to hold:
     v1 gone       -- no operator.js / operator.css file, no script or
                      stylesheet tag, no #/operator nav entry or view, no
                      OperatorPanel route, no api/operator/ call left in the
                      dashboard; an old #/operator bookmark lands on Observer.
     floating      -- the dock segment and Alt+O open the v2 dialog, a
                      session link in it closes it, and it never assigns
                      HTML. */
const fs = require("fs");
const path = require("path");
const root = path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static");
const app = fs.readFileSync(path.join(root, "app.js"), "utf8");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const fab = fs.readFileSync(path.join(root, "operator_fab.js"), "utf8");

let failures = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  if (a !== e) { failures++; console.error(`FAIL ${label}\n  got      ${a}\n  expected ${e}`); }
}

check("the v1 script and stylesheet files are gone",
      [fs.existsSync(path.join(root, "operator.js")), fs.existsSync(path.join(root, "operator.css"))], [false, false]);
check("index.html loads neither",
      [html.includes('static/operator.js"'), html.includes('static/operator.css"')], [false, false]);
check("no v1 nav entry, badge or view",
      [html.includes('href="#/operator"'), html.includes("operator-nav-badge"), html.includes('id="operator-view"')],
      [false, false, false]);
check("no v1 page route in app.js",
      [/OperatorPanel/.test(app), app.includes('operator: "operator-view"'), app.includes('showView("operator")')],
      [false, false, false]);
check("an old #/operator bookmark opens Observer",
      app.includes('if (parts[0] === "operator") return { page: "observer", scope: "global", name: "" };'), true);
const v1Calls = fs.readdirSync(root).filter(f => /\.(js|html)$/.test(f))
  .filter(f => /api\/operator\/|api\/operator\?|"api\/operator"/.test(fs.readFileSync(path.join(root, f), "utf8")));
check("no dashboard file calls the v1 HTTP routes", v1Calls, []);

check("the floating Operator opens from anywhere: a segment of the shared dock and Alt+O, both v2 (operator_fab.js)",
      [fab.includes('fabDock().append(fab)'), fab.includes('dock.id = "fab-dock"'), /event\.altKey[^\n]*event\.key === "o"/.test(fab),
       fab.includes("OperatorView.create("), html.includes('<script src="static/operator_fab.js"></script>'), html.includes('href="static/operator_fab.css"')],
      [true, true, true, true, true, true]);
check("a session link in the floating Operator closes it",
      /root\.addEventListener\("click"[^\n]*\n[^\n]*a\[href\^="#\/"\][^\n]*modal\.close\(\)/.test(fab), true);
check("operator_fab.js never assigns HTML", /innerHTML|outerHTML|insertAdjacentHTML/.test(fab), false);

if (failures) { console.error(`${failures} check(s) failed`); process.exit(1); }
console.log("operator_check: ok");
