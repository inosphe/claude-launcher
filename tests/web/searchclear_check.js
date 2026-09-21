/* The clear button on the rail's filter box (index.html #session-search-clear).
   Four things must hold. The button is shown exactly while the box holds
   characters, including characters Enter would trim away, since it is the
   typed text the button erases. Pressing it empties the box, drops the search
   state through setSessionSearch("") so a ranking by meaning goes with it, and
   returns the caret to the box. A page shipping older markup -- the box
   without the button, or neither -- still boots instead of throwing. And the
   markup and stylesheet ship the button inside the input's own cell, hidden to
   begin with, with the browser's native clear affordance suppressed so the
   field never shows two crosses.
   Slice the real functions out of app.js and drive them against a stub DOM. */
const assert = require("assert/strict");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const root = path.join(__dirname, "..", "..");
const src = fs.readFileSync(
  path.join(root, "src", "claude_launcher", "web", "static", "app.js"), "utf8");
const html = fs.readFileSync(
  path.join(root, "src", "claude_launcher", "web", "static", "index.html"), "utf8");
const css = fs.readFileSync(
  path.join(root, "src", "claude_launcher", "web", "static", "style.css"), "utf8");

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(value = "") {
  return {
    value, focused: 0, classes: new Set(),
    focus() { this.focused++; },
    get classList() {
      const self = this;
      return {
        contains: (c) => self.classes.has(c),
        toggle: (c, on) => { on ? self.classes.add(c) : self.classes.delete(c); },
      };
    },
  };
}

/* `nodes` is the page: leave an id out of it and $ answers null, which is the
   older-markup case the guards in both functions exist for. */
function load(nodes) {
  const cleared = [];
  const context = vm.createContext({
    $: (id) => nodes[id] || null,
    setSessionSearch: (q) => cleared.push(q),
  });
  vm.runInContext(slice("syncSessionSearchClear") + "\n" + slice("clearSessionSearch"), context);
  return { context, cleared };
}

/* ---- the button follows the box's text --------------------------------- */
const box = node("");
const button = node();
const page = load({ "session-search": box, "session-search-clear": button });

page.context.syncSessionSearchClear();
assert.equal(button.classList.contains("hidden"), true, "an empty box hides the button");

box.value = "mesh";
page.context.syncSessionSearchClear();
assert.equal(button.classList.contains("hidden"), false, "text in the box shows it");

/* Enter trims what it sends to the daemon, so sessionSearch.q would be "" here
   while the box still holds a character the operator can see and would want
   gone. The button reads the box for exactly that reason. */
box.value = "  ";
page.context.syncSessionSearchClear();
assert.equal(button.classList.contains("hidden"), false, "spaces are still text to clear");

/* ---- pressing it ------------------------------------------------------- */
box.value = "railsearch";
page.context.syncSessionSearchClear();
page.context.clearSessionSearch();
assert.equal(box.value, "", "the box is emptied");
assert.deepEqual(page.cleared, [""], "the search state is dropped through setSessionSearch");
assert.equal(button.classList.contains("hidden"), true, "the button hides itself again");
assert.equal(box.focused, 1, "the caret goes back to the box");

/* ---- older markup ------------------------------------------------------ */
const boxOnly = node("mesh");
const older = load({ "session-search": boxOnly });
older.context.syncSessionSearchClear();
older.context.clearSessionSearch();
assert.equal(boxOnly.value, "", "a page without the button still clears its box");
assert.deepEqual(older.cleared, [""], "...and still drops the search state");

const empty = load({});
empty.context.syncSessionSearchClear();
empty.context.clearSessionSearch();
assert.deepEqual(empty.cleared, [], "a page without the box asks for nothing");

/* ---- the wiring -------------------------------------------------------- */
const wiring = src.slice(src.indexOf('const sessionSearchBox = $("session-search");'));
assert(wiring.indexOf('addEventListener("click", clearSessionSearch)') > 0
       && wiring.indexOf('addEventListener("click", clearSessionSearch)') < wiring.indexOf("$(\"new-session\")"),
       "the button's click runs the same function Esc does");
assert(wiring.slice(0, wiring.indexOf("$(\"new-session\")"))
             .includes('if (e.key === "Escape") clearSessionSearch();'),
       "Esc goes down that one path rather than keeping a copy of it");

/* ---- the markup -------------------------------------------------------- */
const cell = html.slice(html.indexOf('<div class="session-search-box">'),
                        html.indexOf('id="search-anything-open"'));
assert(cell.includes('id="session-search"') && cell.includes('id="session-search-clear"'),
       "the box and its button ship in one cell, ahead of the Search-anything button");
assert(/class="session-search-clear hidden"/.test(cell), "it ships hidden");
assert(cell.includes('type="button"'), "it is a button, not a submit");
assert(/aria-label="Clear the session filter"/.test(cell), "it is named for a screen reader");

/* ---- the stylesheet ---------------------------------------------------- */
assert(/\.session-search-box \{[^}]*position: relative/.test(css),
       "the cell is the positioning context the button sits in");
assert(/#session-search \{[^}]*padding: 5px 22px 5px 8px/.test(css),
       "the input reserves the room the button covers");
assert(css.includes("#session-search::-webkit-search-cancel-button"),
       "WebKit's own cross is suppressed so the field shows one");
const mobile = css.slice(css.indexOf("iOS zooms the page"), css.indexOf("iOS zooms the page") + 700);
assert(/\.session-search-clear \{ width: 22px/.test(mobile),
       "the mobile block gives it a thumb-sized target");

console.log("searchclear ok: visibility, clearing, older markup, wiring, markup, stylesheet");
