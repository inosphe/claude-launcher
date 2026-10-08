/* The project header and the search on one line (claunch-p6xb3).

   Under the project tabs, the picked project's header shares its line with
   the rail's search. What has to hold:
   - folded, the line is the header, the filter icon, Search anything and
     the manage icon; the box is not drawn;
   - pressing the filter icon (or the box taking the focus) opens the
     search over the header: the box takes the header's place and gets the
     caret;
   - a box with a query in it stays open when it loses the focus, so the
     narrowing on the list is never hidden behind an icon; an empty one
     folds back;
   - Esc on an empty box folds it, after the clear Esc always did;
   - manage is an icon link to the projects page, on the same line. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web",
                         "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* ---- stub DOM ---- */
function node(id) {
  const n = {
    id, classes: new Set(), attrs: {}, value: "", focused: 0,
    setAttribute(k, v) { n.attrs[k] = v; },
    focus() { n.focused++; document.activeElement = n; },
  };
  n.classList = {
    toggle: (c, on) => { if (on) n.classes.add(c); else n.classes.delete(c); return !!on; },
    contains: (c) => n.classes.has(c),
  };
  return n;
}
const row = node("session-search-row");
const box = node("session-search");
const toggle = node("session-search-toggle");
const byId = { "session-search-row": row, "session-search": box, "session-search-toggle": toggle };
const $ = (id) => byId[id] || null;
const document = { activeElement: null };

const fns = new Function("$", "document",
  slice("syncSessionSearchOpen") + slice("openSessionSearch") +
  "return { syncSessionSearchOpen, openSessionSearch };")($, document);
const open = () => [row.classes.has("searching"), toggle.attrs["aria-expanded"]];

/* ---- folded and opened ---- */
fns.syncSessionSearchOpen();
check("an empty box that is not being typed in starts folded", open(), [false, "false"]);
fns.openSessionSearch();
check("the filter icon opens the search and hands the box the caret",
      [...open(), box.focused], [true, "true", 1]);
document.activeElement = null;
fns.syncSessionSearchOpen(false);
check("an empty box folds back when it loses the focus", open(), [false, "false"]);
box.value = "s88";
fns.syncSessionSearchOpen(false);
check("a box with a query stays open when it loses the focus", open(), [true, "true"]);
box.value = "";
fns.syncSessionSearchOpen(false);
check("...and folds once it is emptied", open(), [false, "false"]);
box.value = "x";
fns.syncSessionSearchOpen();
check("a reload that hands the box back with text starts open", open(), [true, "true"]);
box.value = "";
document.activeElement = box;
fns.syncSessionSearchOpen();
check("the box holding the focus is open", open(), [true, "true"]);

/* ---- the wiring ---- */
const wiring = src.slice(src.indexOf('const sessionSearchBox = $("session-search");'),
                         src.indexOf('$("new-session").mesh.addEventListener'));
check("the icon opens the search",
      wiring.includes('$("session-search-toggle")?.addEventListener("click", openSessionSearch)'),
      true);
check("the box opens on focus and folds on blur",
      [wiring.includes('addEventListener("focus", () => syncSessionSearchOpen(true))'),
       wiring.includes('addEventListener("blur", () => syncSessionSearchOpen(false))')],
      [true, true]);
check("Esc on an empty box folds it, after the clear",
      /if \(e\.key === "Escape"\) clearSessionSearch\(\);\s*\/\/[^\n]*\n\s*if \(e\.key === "Escape" && !sessionSearchBox\.value\) \{\s*sessionSearchBox\.blur\(\);\s*syncSessionSearchOpen\(false\);/
        .test(wiring), true);
check("the first state is read from the box", wiring.includes("syncSessionSearchOpen();"), true);

/* ---- the markup ---- */
const line = html.slice(html.indexOf('<div id="session-search-row"'),
                        html.indexOf('id="session-search-note"'));
const at = (needle) => line.indexOf(needle);
check("one line holds the header, the filter icon, the box, Search anything and manage",
      ['id="project-header"', 'id="session-search-toggle"', 'class="session-search-box"',
       'id="search-anything-open"', 'id="project-manage"'].map((n) => at(n) >= 0),
      [true, true, true, true, true]);
check("...in that order",
      at('id="project-header"') < at('id="session-search-toggle"') &&
      at('id="session-search-toggle"') < at('class="session-search-box"') &&
      at('class="session-search-box"') < at('id="search-anything-open"') &&
      at('id="search-anything-open"') < at('id="project-manage"'), true);
check("manage is an icon link to the projects page, named for a reader",
      /<a id="project-manage" class="project-manage" href="#\/workspaces"\s+aria-label="Manage projects"/.test(line)
        && line.slice(at('id="project-manage"')).includes("<svg"), true);
check("the filter icon says what it controls and starts folded",
      /aria-controls="session-search" aria-expanded="false"/.test(line), true);
check("the tabs stand right above the line",
      html.indexOf('id="project-tabs"') < html.indexOf('<div id="session-search-row"'), true);

/* ---- the stylesheet ---- */
check("folded, the box is not drawn; opened, it is, and the header and the icon go",
      [/\.session-search-row \.session-search-box \{ display: none;/.test(css),
       /\.session-search-row\.searching \.session-search-box \{ display: flex; \}/.test(css),
       /\.session-search-row\.searching \.project-header,\s*\.session-search-row\.searching \.session-search-toggle \{ display: none; \}/.test(css)],
      [true, true, true]);
check("the note takes a line of its own under the row",
      /\.session-search-note \{ flex: 1 0 100%;/.test(css), true);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("projectsearchline_check: ok");
