/* The browser tab's title (claunch-9thjl), run against the real functions
   from app.js on a stub DOM.

   - By default the title names what the rail is looking at: the open group
     tab (a mesh, or a workspace by its short label) and the picked project.
     The `(no mesh)` bucket is not a name, and with nothing picked the title
     is the old "claunch sessions".
   - A person can name the tab. The name is this tab's alone, so it is kept
     in the address (`?title=`) and in sessionStorage, which the browser
     keeps per tab -- never in localStorage, which every tab of the origin
     shares and which would rename the other windows on their next load.
   - An address that names a title is obeyed; one that names none (every
     link inside the page is written without it) is given the name back.
     Clearing the name drops it from both and the automatic title returns.
   - route() calls the sync, setCurrentProject and the group tab strip both
     refresh the title, and the ✎ button sits in the rail's title row. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

/* ---- the wiring ---------------------------------------------------------- */
const titleRow = html.slice(html.indexOf('id="rail-title"'), html.indexOf('id="refresh-all"'));
check("the ✎ button sits in the rail's title row", titleRow.includes('id="tab-title-edit"'), true);
check("route() syncs the title from the address",
      /syncRouteTitle\(\);/.test(slice("route")), true);
check("a project pick refreshes the title",
      /syncTabTitle\(\)/.test(slice("setCurrentProject")), true);
check("the group tab strip reports its open tab",
      /noteRailOpenGroup\(/.test(slice("renderSessionGroupTabs")), true);
const stateBlock = src.slice(src.indexOf("const TAB_TITLE_KEY = "),
                             src.indexOf("function syncRouteTitle("));
check("the custom title never touches localStorage",
      /localStorage/.test(stateBlock + slice("syncRouteTitle")), false);

/* ---- the stub browser ---------------------------------------------------- */
function storage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => { m.set(k, String(v)); },
    removeItem: (k) => { m.delete(k); },
    dump: () => Object.fromEntries(m),
  };
}

function load(hash, session) {
  const location = { hash };
  const history = {
    state: null,
    replaceState(state, _t, url) { this.state = state; if (url !== undefined) location.hash = url; },
  };
  const document = { title: "claunch sessions" };
  const btn = {
    title: "", classes: new Set(),
    classList: { toggle(c, on) { if (on) btn.classes.add(c); else btn.classes.delete(c); } },
  };
  const $ = (id) => (id === "tab-title-edit" ? btn : null);
  const ctx = { location, history, document, $, sessionStorage: session, BASE: "/t" };
  const code = [
    "let currentProject = '';",
    slice("sessionWorkspaceLabel"), slice("hashQuery"),
    stateBlock, slice("syncRouteTitle"),
    "return { autoTabTitle, hashWithTitle, setCustomTabTitle, syncRouteTitle, " +
    "noteRailOpenGroup, syncTabTitle, " +
    "setProject: (p) => { currentProject = p; syncTabTitle(); }, " +
    "custom: () => customTabTitle };",
  ].join("\n");
  const fns = new Function(...Object.keys(ctx), code)(...Object.values(ctx));
  return { fns, location, document, btn };
}

/* ---- the automatic title ------------------------------------------------- */
let t = load("#/", storage());
const { autoTabTitle, hashWithTitle } = t.fns;
check("nothing picked keeps the old title", autoTabTitle("", "", null), "claunch sessions");
check("a mesh tab names the window", autoTabTitle("", "mesh", "mesh-0826"), "mesh-0826 · claunch");
check("...with the project after it", autoTabTitle("gds6", "mesh", "gds6-main"),
      "gds6-main · gds6 · claunch");
check("the (no mesh) bucket is not a name", autoTabTitle("default", "mesh", "(no mesh)"),
      "default · claunch");
check("a workspace tab uses its short label",
      autoTabTitle("", "workspace", "F:\\works\\claude-launcher\\sub"), "…/claude-launcher/sub · claunch");
check("the title param is set beside the others",
      hashWithTitle("#/s/a?project=hq", "build box"), "#/s/a?project=hq&title=build+box");
check("...and taken out", hashWithTitle("#/s/a?project=hq&title=x", ""), "#/s/a?project=hq");

t.fns.syncRouteTitle();
t.fns.noteRailOpenGroup("mesh", "mesh-0826");
check("the open mesh tab becomes the tab title", t.document.title, "mesh-0826 · claunch");
t.fns.setProject("default");
check("a project pick joins it", t.document.title, "mesh-0826 · default · claunch");
check("the button says the title is automatic", t.btn.classes.has("custom"), false);

/* ---- the custom title ---------------------------------------------------- */
const session = storage();
t = load("#/?project=hq", session);
t.fns.noteRailOpenGroup("mesh", "mesh-0826");
t.fns.setCustomTabTitle("  review box ");
check("a custom name wins, trimmed", t.document.title, "review box");
check("...goes into the address", t.location.hash, "#/?project=hq&title=review+box");
check("...and this tab's sessionStorage", session.dump(), { "claunch_tab_title:/t": "review box" });
check("...and the button shows it is set", t.btn.classes.has("custom"), true);
t.location.hash = "#/s/s1131";   // an in-page link, written without the title
t.fns.syncRouteTitle();
check("an address with no title is given the name back", t.location.hash, "#/s/s1131?title=review+box");
t.location.hash = "#/s/s1131?title=other";
t.fns.syncRouteTitle();
check("an address that names one is obeyed", [t.fns.custom(), t.document.title], ["other", "other"]);
t.fns.setCustomTabTitle("");
check("clearing returns to the automatic title", t.document.title, "mesh-0826 · claunch");
check("...and drops the name from both", [t.location.hash, session.dump()], ["#/s/s1131", {}]);

/* a reload: the address first, then this tab's sessionStorage */
t = load("#/?title=from+address", storage());
check("a reload reads the name off the address", t.fns.custom(), "from address");
const kept = storage();
kept.setItem("claunch_tab_title:/t", "kept");
t = load("#/", kept);
check("...or from this tab's sessionStorage when the address names none", t.fns.custom(), "kept");
t.fns.syncRouteTitle();
check("...and writes it back into the address", t.location.hash, "#/?title=kept");

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("tabtitle_check: all checks passed");
