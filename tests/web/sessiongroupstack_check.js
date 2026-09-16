/* The rail's mesh headings as a stack rather than a relay.

   `sessiongroupsticky_check` covers the nested lane: one heading held by its
   own group and released at that group's bottom, which names the group the
   reader is inside and nothing else. This file covers what replaced it at
   the outermost level — every group heading on screen at every scroll
   position, piled at the top once passed and waiting at the bottom before
   it arrives — plus the two controls that stack carries: the fold each group
   remembers across the rail's two-second rebuild, and the + that spawns from
   the mesh's leader.

   Three things are held here. The offsets, because a stack that reserves the
   wrong height either overlaps its own headings or leaves a gap under them.
   The fold's storage round-trip, because a fold that does not survive the
   rebuild is a fold nobody can use. And the spawn target's refusals, because
   the + is drawn even when it cannot spawn and the reason on it is the only
   answer the reader gets. */
const fs = require("fs");
const path = require("path");

const root = path.join(__dirname, "..", "..");
const src = fs.readFileSync(path.join(
  root, "src", "claude_launcher", "web", "static", "app.js"), "utf8");
const css = fs.readFileSync(path.join(
  root, "src", "claude_launcher", "web", "static", "style.css"), "utf8");

let failures = 0;
function check(what, got, want) {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    console.error(`FAIL ${what}\n  got  ${JSON.stringify(got)}` +
                  `\n  want ${JSON.stringify(want)}`);
    failures++;
  }
}

function slice(from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to, a);
  if (a < 0 || b <= a) throw new Error(`cannot locate ${from}`);
  return src.slice(a, b);
}

/* ---------------------------------------------------------------- */
/* the stack's offsets                                              */
/* ---------------------------------------------------------------- */

const sticky = {};
new Function("exports", slice("function sessionGroupStickyTops(",
                              "function setSessionGroup(") +
  "\nexports.layout = sessionGroupStickyLayout;" +
  "\nexports.sync = syncSessionGroupStickyOffsets;" +
  "\nexports.jumpOffset = sessionGroupJumpOffset;" +
  "\nexports.budget = SESSION_GROUP_STACK_BUDGET;")(sticky);

function heading(level, height) {
  const props = {};
  return {
    props,
    dataset: { groupLevel: String(level) },
    getBoundingClientRect: () => ({ height }),
    style: { setProperty(name, value) { props[name] = value; } },
  };
}

const nested = [heading(0, 27), heading(1, 25), heading(0, 27), heading(1, 25)];
const stacked = sticky.layout(nested, 1000);
check("the outermost headings reserve each other's height at both ends",
      [stacked.stacked, stacked.offsets],
      [true, [
        { top: 0, bottom: 27 },
        { top: 27, bottom: null },
        { top: 27, bottom: 0 },
        { top: 54, bottom: null },
      ]]);

const single = sticky.layout([heading(0, 27)], 1000);
check("one group alone is pinned to both ends of an empty stack",
      [single.stacked, single.offsets], [true, [{ top: 0, bottom: 0 }]]);

const many = sticky.layout(
  [heading(0, 27), heading(0, 27), heading(0, 27)], 100);
check("a stack past its share of the rail hands back the nested offsets",
      [many.stacked, many.offsets],
      [false, [
        { top: 0, bottom: null },
        { top: 0, bottom: null },
        { top: 0, bottom: null },
      ]]);

const unmeasured = sticky.layout([heading(0, 27), heading(0, 27)], 0);
check("a rail with no measurable height does not stack",
      unmeasured.stacked, false);

/* The rail writes both custom properties and flips one class, so the
   stylesheet never has to guess which lane it is in. */
const written = [heading(0, 20), heading(0, 20)];
const flips = [];
sticky.sync({
  clientHeight: 400,
  querySelectorAll: () => written,
  classList: { toggle: (name, on) => flips.push([name, on]) },
});
check("the sync writes the stack class and both offsets",
      [flips, written.map((h) => h.props)],
      [[["session-group-stacked", true]], [
        { "--session-group-sticky-top": "0px", "--session-group-sticky-bottom": "20px" },
        { "--session-group-sticky-top": "20px", "--session-group-sticky-bottom": "0px" },
      ]]);

const overBudget = [heading(0, 200), heading(0, 200)];
const overFlips = [];
sticky.sync({
  clientHeight: 300,
  querySelectorAll: () => overBudget,
  classList: { toggle: (name, on) => overFlips.push([name, on]) },
});
check("the fallback clears the bottom offset instead of pinning to it",
      [overFlips, overBudget.map((h) => h.props["--session-group-sticky-bottom"])],
      [[["session-group-stacked", false]], ["auto", "auto"]]);

/* ---------------------------------------------------------------- */
/* the jump to a group's first session                              */
/* ---------------------------------------------------------------- */

/* The row has to land under the group's own heading rather than at the
   rail's top, which is where the pinned stack already sits: a row scrolled
   to the top would be behind the headings the stack keeps there. */
const rect = (top, height) => ({ getBoundingClientRect: () => ({ top, height }) });
function pinned(top, height, stickyTop) {
  const h = rect(top, height);
  h.style = { getPropertyValue: () => `${stickyTop}px` };
  return h;
}

check("the row is scrolled to the first pixel below its own heading",
      sticky.jumpOffset(rect(100, 500), pinned(127, 27, 27), rect(600, 24)),
      600 - 100 - 27 - 27);

check("a row already in place needs no scroll",
      sticky.jumpOffset(rect(0, 500), pinned(54, 27, 54), rect(81, 24)), 0);

check("a row above the heading scrolls the rail back up",
      sticky.jumpOffset(rect(100, 500), pinned(100, 27, 0), rect(120, 24)) < 0,
      true);

const unset = rect(27, 27);
unset.style = { getPropertyValue: () => "" };
check("a heading with no stack offset yet counts only its own height",
      sticky.jumpOffset(rect(0, 500), unset, rect(200, 24)), 200 - 27);

/* ---------------------------------------------------------------- */
/* the fold each group remembers                                    */
/* ---------------------------------------------------------------- */

function storage(initial = null) {
  const writes = [];
  let value = initial;
  return {
    writes,
    getItem: () => value,
    setItem: (key, next) => { value = next; writes.push([key, next]); },
  };
}

function folds(initial) {
  const ctx = {};
  const store = storage(initial);
  new Function("exports", "BASE", "localStorage",
    slice("const SESSION_GROUP_COLLAPSE_KEY", "/* Convert lineage-ordered rows") +
    "\nexports.key = SESSION_GROUP_COLLAPSE_KEY;" +
    "\nexports.groupKey = sessionGroupKey;" +
    "\nexports.get = isSessionGroupCollapsed;" +
    "\nexports.set = setSessionGroupCollapsed;")(ctx, "/t/local/", store);
  return { ctx, store };
}

const fresh = folds(null);
check("a group nobody has touched starts open",
      fresh.ctx.get("mesh", "mesh-0826"), false);
fresh.ctx.set("mesh", "mesh-0826", true);
check("shutting a group records it and remembers it",
      [fresh.ctx.get("mesh", "mesh-0826"),
       JSON.parse(fresh.store.writes[0][1])],
      [true, [fresh.ctx.groupKey("mesh", "mesh-0826")]]);
check("the stored key is namespaced to the page",
      fresh.ctx.key, "claunch_session_group_collapsed:/t/local/");
fresh.ctx.set("mesh", "mesh-0826", false);
check("opening it again drops it from storage",
      [fresh.ctx.get("mesh", "mesh-0826"),
       JSON.parse(fresh.store.writes[1][1])], [false, []]);

const reopened = folds(JSON.stringify([
  fresh.ctx.groupKey("mesh", "mesh-0826"),
]));
check("a stored fold is in force on the next load",
      [reopened.ctx.get("mesh", "mesh-0826"),
       reopened.ctx.get("mesh", "mesh-other")], [true, false]);

const damaged = folds("not json");
check("unreadable storage leaves every group open rather than throwing",
      damaged.ctx.get("mesh", "mesh-0826"), false);

check("the two groupings do not share a fold",
      fresh.ctx.groupKey("mesh", "same") === fresh.ctx.groupKey("workspace", "same"),
      false);

/* ---------------------------------------------------------------- */
/* the + on a mesh heading                                          */
/* ---------------------------------------------------------------- */

function spawn(meshes, sessions) {
  const ctx = {};
  new Function("exports", "meshCache", "sessionsCache",
    slice("function meshGroupLeader(", "/* What the row actually draws") +
    "\nexports.leader = meshGroupLeader;" +
    "\nexports.target = meshGroupSpawnTarget;")(ctx, meshes, sessions);
  return ctx;
}

const room = (members) => [{ name: "mesh-0826", members }];
const leaderMember = { session: "s469", handle: "s469", role: "leader", local: true };
const workerMember = { session: "s522", handle: "s522", role: "worker", local: true };

check("a live local leader is the parent the + spawns from",
      spawn(room([workerMember, leaderMember]),
            [{ name: "s469", status: "idle" }]).target("mesh-0826").name,
      "s469");

const noLeader = spawn(room([workerMember]), [{ name: "s522", status: "idle" }])
  .target("mesh-0826");
check("a mesh with no leader refuses and says which role is missing",
      [noLeader.name, /leader role/.test(noLeader.reason || "")],
      [undefined, true]);

const exited = spawn(room([leaderMember]), [{ name: "s469", status: "exited" }])
  .target("mesh-0826");
check("an exited leader refuses and names the session",
      [exited.name, /'s469'/.test(exited.reason || ""),
       /exited/.test(exited.reason || "")],
      [undefined, true, true]);

const remote = spawn(room([{ ...leaderMember, local: false }]), [])
  .target("mesh-0826");
check("a leader on another machine refuses and says so",
      [remote.name, /another machine/.test(remote.reason || "")],
      [undefined, true]);

const unknown = spawn(room([leaderMember]), []).target("mesh-0826");
check("a leader the rail does not list refuses rather than spawning blind",
      [unknown.name, /not in the rail/.test(unknown.reason || "")],
      [undefined, true]);

const homeless = spawn(room([leaderMember]), [{ name: "s469", status: "idle" }])
  .target("(no mesh)");
check("the catch-all group has no leader to borrow",
      [homeless.name, /no mesh/.test(homeless.reason || "")],
      [undefined, true]);

check("a local leader wins over one on another machine",
      spawn(room([{ ...leaderMember, session: "s900", local: false },
                  leaderMember]),
            [{ name: "s469", status: "idle" }]).leader("mesh-0826").session,
      "s469");

// The roster never forgets a leader: after a hand-over the exited one still
// sits first in roster order, and the + used to refuse on it while the live
// leader sat one row down (mesh-0826 with s127 exited, s469 idle).
const exitedFirst = { ...leaderMember, session: "s127", handle: "s127" };
check("a live leader is chosen over an exited one that precedes it",
      spawn(room([exitedFirst, leaderMember]),
            [{ name: "s127", status: "exited" }, { name: "s469", status: "idle" }])
        .target("mesh-0826").name,
      "s469");
check("a live leader is chosen over one the rail no longer lists",
      spawn(room([exitedFirst, leaderMember]),
            [{ name: "s469", status: "idle" }]).target("mesh-0826").name,
      "s469");
const bothDead = spawn(room([exitedFirst, leaderMember]),
                       [{ name: "s127", status: "exited" }, { name: "s469", status: "exited" }])
  .target("mesh-0826");
check("with no live leader the refusal still names a leader",
      [bothDead.name, /exited/.test(bothDead.reason || "")], [undefined, true]);

/* ---------------------------------------------------------------- */
/* the create button beside the +                                   */
/* ---------------------------------------------------------------- */

/* The + hangs a child off the leader; this one opens the plain create form
   with the mesh already picked, which is the only way to put a session in a
   room without giving it the leader's lineage. It needs no leader, so the
   only refusal it has is the catch-all group, and the route has to carry the
   mesh for the picker to arrive on it. */

const hash = {};
new Function("exports", slice("function parseHash(", "function route()") +
  "\nexports.parse = parseHash;")(hash);

check("#/new still opens the create form with no mesh asked for",
      hash.parse("#/new"), { page: "new", mesh: "" });
check("#/new/<mesh> names the mesh the form should arrive with",
      hash.parse("#/new/mesh-0826"), { page: "new", mesh: "mesh-0826" });
check("a mesh name with a URL-unsafe character survives the round trip",
      hash.parse(`#/new/${encodeURIComponent("mesh a/b")}`),
      { page: "new", mesh: "mesh a/b" });

check("the heading's create button links to that route rather than spawning",
      /go\(`#\/new\/\$\{encodeURIComponent\(row\.value\)\}`\)/.test(src), true);
check("the catch-all group's create button is refused with a reason",
      /const joinable = row\.value && row\.value !== "\(no mesh\)"/.test(src) &&
      /add\.setAttribute\("aria-disabled", "true"\)/.test(src), true);
check("the refused create button keeps its pointer events, like the +",
      /#session-list \.session-group-new\.disabled\s*\{/.test(css) &&
      !/add\.disabled = true/.test(src), true);
check("the create button is drawn and styled",
      /add\.className = "session-group-new"/.test(src) &&
      /#session-list \.session-group-new\s*\{/.test(css), true);
check("the route opens the form through the mesh-aware opener",
      /case "new": showView\("new"\); openNewSession\(r\.mesh\);/.test(src), true);
check("a preset mesh is only applied once the picker has an option for it",
      /if \(pendingNewMesh && \[\.\.\.mesh\.options\]\.some\(\(o\) => o\.value === pendingNewMesh\)\)/
        .test(src) && /pendingNewMesh = "";/.test(src), true);
check("a cold tab fetches the mesh list before presetting",
      /if \(!\(meshCache \|\| \[\]\)\.some\(\(m\) => m\.name === pendingNewMesh\)\) \{\s*await refreshMeshList\(\);/
        .test(src), true);
check("picking the mesh for the operator also re-authorises the role list",
      /await refreshRoles\(form\.mesh\.value\);/
        .test(slice("async function openNewSession(",
                    "/* The create form's mesh")), true);

/* ---------------------------------------------------------------- */
/* what the page has to be wearing for any of it to show            */
/* ---------------------------------------------------------------- */

check("the outermost groups drop their own box so the stack spans the rail",
      /#session-list\.session-group-stacked > li\.session-group\.session-group-level-0\s*\{[^}]*display:\s*contents/
        .test(css), true);
check("headings carry the bottom half of the stack too",
      /#session-list \.session-group-heading\s*\{[^}]*bottom:\s*var\(--session-group-sticky-bottom/
        .test(css), true);
check("a shut group hides its rows",
      /#session-list \.session-group\.collapsed > \.session-group-body\s*\{[^}]*display:\s*none/
        .test(css), true);
check("the refused + keeps its pointer events so its reason can be read",
      /#session-list \.session-group-plus\.disabled\s*\{/.test(css) &&
      /plus\.setAttribute\("aria-disabled", "true"\)/.test(src) &&
      !/plus\.disabled = true/.test(src.slice(src.indexOf("meshGroupSpawnTarget(row.value)"),
                                              src.indexOf("const collapsed ="))), true);
check("the heading opens the same modal the leader's own row + opens",
      /openSpawnModal\(target\.name\)/.test(src), true);
check("a fold re-measures the stack it just changed",
      /setSessionGroupCollapsed\(row\.group, row\.value, shut\)/.test(src) &&
      /paintFold\(shut\);/.test(src), true);
check("the rail re-measures the stack when the window height changes",
      /syncSessionGroupStickyOffsets\(railList\)/.test(src), true);

check("the jump reads the group's first session out of the group's own body",
      /const first = body\.querySelector\("li\[data-name\]"\);/.test(src), true);
check("the jump opens a shut group rather than scrolling to hidden rows",
      /if \(group\.classList\.contains\("collapsed"\)\) \{[\s\S]{0,240}?paintFold\(false\);/
        .test(src), true);
check("the jump moves the rail by the measured offset",
      /list\.scrollTop \+= sessionGroupJumpOffset\(list, heading, first\);/.test(src),
      true);
check("every grouping gets a jump, not only mesh",
      src.indexOf("heading.appendChild(jump);") <
      src.indexOf("meshGroupSpawnTarget(row.value)"), true);
check("the jump is styled as a heading control",
      /#session-list \.session-group-jump\s*\{/.test(css), true);

// Search updates must reveal matches through every folded ancestor, without
// overwriting the saved folds. Include a state-filtered match and no matches.
const searchState = { q: "s561" };
let measurements = 0;
function searchGroup(value, visible) {
  let collapsed = true;
  const caret = {};
  const heading = { setAttribute: (key, value) => { heading[key] = value; } };
  return {
    dataset: { group: "mesh", value }, caret, heading,
    classList: {
      contains: () => collapsed,
      toggle: (_, value) => { collapsed = value; },
    },
    querySelector: (selector) => selector === ".session-group-caret" ? caret
      : selector === ".session-group-heading" ? heading : visible ? {} : null,
  };
}
const outer = searchGroup("outer", true);
const inner = searchGroup("inner", true);
const absent = searchGroup("absent", false);
const groupList = { querySelectorAll: () => [outer, inner, absent] };
const syncSearch = new Function("sessionSearch", "isSessionGroupCollapsed",
  "syncSessionGroupStickyOffsets", slice("function syncSessionGroupSearch(",
  "function syncSessionFilters(") + "return syncSessionGroupSearch;")(
    searchState, () => true, () => { measurements++; });
syncSearch(groupList);
check("search opens matching nested groups only",
  [outer, inner, absent].map(g => g.classList.contains("collapsed")), [false, false, true]);
check("search updates expanded accessibility state", inner.heading["aria-expanded"], "true");
check("search updates fold marker", inner.caret.textContent, "▾");
check("search remeasures changed group geometry", measurements, 1);
syncSearch(groupList);
check("unchanged search does not remeasure geometry", measurements, 1);
searchState.q = "   ";
syncSearch(groupList);
check("clearing search restores saved nested folds",
  [outer, inner, absent].map(g => g.classList.contains("collapsed")), [true, true, true]);
check("clearing search restores accessibility state", inner.heading["aria-expanded"], "false");

if (failures) process.exit(1);
console.log("sessiongroupstack_check: ok");
