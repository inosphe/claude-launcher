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

if (failures) process.exit(1);
console.log("sessiongroupstack_check: ok");
