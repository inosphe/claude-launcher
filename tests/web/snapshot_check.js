/* The static snapshot an ended session is drawn as, instead of a live xterm.

   A killed, archived or paused session has no live PTY to talk to and nothing
   new to draw. A live xterm still pays to paint its final screen — it measures
   every distinct glyph of the seeded grid synchronously, a forced layout per
   glyph — and on a paused claude session's frame that measurement freezes the
   whole page for seconds, so a click on another session in the rail does not
   route until it finishes. The cure is to not build an xterm for a session
   that has ended: paint its last screen as plain text (`/api/sessions/<name>/
   capture`) with no socket, no wheel to the daemon, and no per-glyph measure.

   None of that is visible to Python, so the shipped functions are sliced out
   of app.js and their structure and the one piece of real behaviour that can
   run without a browser — removeSnapshot's DOM surgery — are driven here. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web",
                         "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

function slice(from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to, a + 1);
  if (a < 0 || b < 0 || b <= a) throw new Error(`cannot slice ${from} .. ${to}`);
  return src.slice(a, b);
}

/* ---- attach() routes an ended session away from the live path --------- */
const attach = slice("function attach(name) {", "\n/* Not merely");
check("attach reads the record's status",
      /sessionsCache\.find\(\(s\)\s*=>\s*s\.name\s*===\s*name\)/.test(attach));
check("an exited record goes to the snapshot path",
      /status\s*===\s*"exited"\)\s*\{\s*snapshotAttach\(name\);\s*return;\s*\}/
        .test(attach));
check("and returns before freshAttach — a live session is the only fresh attach",
      attach.indexOf("snapshotAttach(name); return;") <
        attach.indexOf("freshAttach(name);"));

/* ---- snapshotAttach builds no terminal and opens no socket ------------ */
const snap = slice("async function snapshotAttach(name) {",
                   "function removeSnapshot() {");
check("snapshotAttach never constructs a Terminal",
      !/new Terminal\b/.test(snap));
check("snapshotAttach never opens a socket",
      !/openSocket\(/.test(snap));
check("it marks the session ended, so the send-keys box closes on it",
      /sessionEnded\s*=\s*true/.test(snap));
check("it records which session the snapshot is for",
      /snapshotName\s*=\s*name/.test(snap));
check("it clears any prior snapshot before drawing this one",
      snap.indexOf("removeSnapshot();") >= 0 &&
        snap.indexOf("removeSnapshot();") < snap.indexOf("appendChild"));
check("it fetches the session's last screen as json",
      /capture\?format=json/.test(snap));
check("a capture that lands after the reader walked away is dropped",
      snap.includes("if (snapshotName !== name || currentName !== name) return;"));

/* ---- every path that mounts an xterm first clears the snapshot -------- */
for (const fn of ["function freshAttach(name) {",
                  "function restoreTerminal(b) {",
                  "function detach() {"]) {
  const body = slice(fn, "\n}");
  check(`${fn.split("(")[0].replace("function ", "")} removes the snapshot first`,
        /removeSnapshot\(\)/.test(body));
}

/* ---- a resumed snapshot upgrades itself to a live terminal ------------ */
check("the poll re-attaches a snapshot whose session came back live",
      /snapshotName\s*===\s*currentName\s*&&\s*cur\s*&&\s*cur\.status\s*!==\s*"exited"/
        .test(src));
check("and only while the terminal is the visible view",
      /snapshotName === currentName[\s\S]{0,700}if \(terminalOnScreen\(\)\) attach\(currentName\);/
        .test(src));

/* ---- a terminal that watched its session die follows the relaunch -----
   The socket's `exit` frame leaves the link idle and the xterm on the dead
   child's last screen. A resume from elsewhere (the CLI, another tab, the
   bulk button) then changes the record's pid and status under it; the poll
   has to reattach from that state too, not only from a live link. */
{
  const follow = slice("} else if (cur && attachedPid && cur.pid !== attachedPid",
                       "if (terminalOnScreen()) attach(currentName);");
  check("the pid follow accepts a terminal whose program ended under the socket",
        /sessionEnded\s*&&\s*cur\.status\s*!==\s*"exited"/.test(follow));
  check("and still accepts a live link", /linkState\s*===\s*"live"/.test(follow));
}

/* ---- removeSnapshot's real DOM surgery, run against a stub ------------ */
{
  function makeEl(cls) {
    return { className: cls, removed: false, remove() { this.removed = true; } };
  }
  const kept = makeEl("xterm");
  const shot1 = makeEl("term-snapshot");
  const shot2 = makeEl("term-snapshot");
  const host = {
    children: [kept, shot1, shot2],
    querySelectorAll(sel) {
      check("removeSnapshot targets only the snapshot <pre>",
            sel === "pre.term-snapshot", sel);
      return this.children.filter((c) => c.className === "term-snapshot");
    },
  };
  // Reconstruct the two globals removeSnapshot reads, then run the shipped
  // body verbatim.
  let snapshotName = "s1";
  const $ = (id) => (id === "terminal" ? host : null);
  const body = slice("function removeSnapshot() {", "\nfunction freshAttach");
  eval(`(${body.replace("function removeSnapshot() {",
                        "function _removeSnapshot() {")})()`);
  check("it clears the snapshot marker", snapshotName === null, snapshotName);
  check("it removes every snapshot node", shot1.removed && shot2.removed);
  check("it leaves the live terminal's own nodes alone", kept.removed === false);
}

/* ---- syncMobileBars survives a paused session -------------------------
   The mobile bar reads the header's word, which is `paused` for a paused
   record, and folds it back to `exited`. That fold reassigns `status`, which
   was declared `const` — so it threw "Assignment to constant variable" the
   moment a paused session was attached. And it runs inside the 2s poll
   (refreshSessions -> setStatusBadge -> syncMobileBars), so the throw killed
   the poll and froze the whole dashboard. This drives the real function with
   a paused header and asserts it neither throws nor mislabels the bar. */
{
  function elem() {
    const n = {
      text: "", cls: "", classes: new Set(),
      get textContent() { return n.text; },
      set textContent(v) { n.text = String(v); },
      get className() { return n.cls; },
      set className(v) { n.cls = String(v); },
      classList: {
        toggle: (c, on) => (on ? n.classes.add(c) : n.classes.delete(c)),
        add: (c) => n.classes.add(c),
        remove: (c) => n.classes.delete(c),
        contains: (c) => n.classes.has(c),
      },
    };
    return n;
  }
  const nodes = {};
  const $ = (id) => (nodes[id] || (nodes[id] = elem()));
  nodes["term-status"] = elem();
  nodes["term-status"].textContent = "paused";   // the header's word for a paused record

  let currentName = "s1";
  const sessionsCache = [{ name: "s1", status: "exited", paused_at: "t", profile: "p", harness: "claude" }];
  function mobileTitle() { return "s1"; }
  function profileHarnessLabel() { return "claude"; }
  function syncSessionKillControls() {}

  const body = slice("function syncMobileBars() {", "\n// ☰ is");
  let threw = null;
  try {
    eval(`(${body})()`);
  } catch (e) {
    threw = String(e);
  }
  check("syncMobileBars does not throw on a paused session", threw === null, threw);
  check("the mobile status badge reads paused",
        nodes["m-status"] && nodes["m-status"].textContent === "paused",
        nodes["m-status"] && nodes["m-status"].textContent);
  check("and its class folds the word onto the exited state",
        nodes["m-status"] && /exited/.test(nodes["m-status"].className) &&
          /paused/.test(nodes["m-status"].className),
        nodes["m-status"] && nodes["m-status"].className);
  check("resume is offered on the mobile bar (the session is exited)",
        nodes["m-resume"] && nodes["m-resume"].classList.contains("hidden") === false);
}

if (failures) {
  console.log(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("snapshot_check: ok");
