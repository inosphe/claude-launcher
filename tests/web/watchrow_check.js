/* The cflow-watch subscriptions on the two places this round asked for: the
   rail card (one pill with the count, the targets in its hover) and the
   detail panel (one row per subscription).

   The record is the watcher's own loops.json "watch" rows, read by the
   daemon and carried by /api/sessions?view=rail as ``watch`` and by
   /api/sessions/<name>/meta. These helpers only decide how that list is
   drawn, so this holds the drawing: nothing to watch draws nothing, the
   kinds a subscription narrowed to are named, and an unnarrowed one says
   "all events" rather than reading as empty. */
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(name) {
  let start = src.indexOf(`async function ${name}(`);
  if (start < 0) start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}

function mkel(tag, cls, text) {
  const node = {
    tag, className: cls || "", textContent: text === undefined ? "" : text,
    title: "", children: [],
    appendChild(child) { this.children.push(child); child.parent = this; return child; },
  };
  return node;
}

const helpers = new Function(
  "document",
  [slice("el"), slice("localStamp")].join("\n")
  + "\nreturn {el, localStamp};"
)({ createElement: mkel });
const { el, localStamp } = helpers;

const api = new Function(
  "el", "localStamp",
  [slice("railWatchTag"), slice("watchTargetLabel"), slice("sessWatchPanel")].join("\n")
  + "\nreturn {railWatchTag, watchTargetLabel, sessWatchPanel};"
)(el, localStamp);

/* ---- the rail card's pill ---- */
assert.equal(api.railWatchTag({}), null, "no watch key: no pill");
assert.equal(api.railWatchTag({ watch: [] }), null, "watching nothing: no pill");

const one = api.railWatchTag({ watch: [{ target: "kid", kinds: ["reported", "done"] }] });
assert.equal(one.text, "watch 1");
assert.equal(one.title, "watching: kid (reported, done)");

const two = api.railWatchTag({
  watch: [
    { target: "kid", kinds: null },
    { target: "other", kinds: ["done"] },
  ],
});
assert.equal(two.text, "watch 2");
assert.equal(two.title, "watching: kid, other (done)",
  "an unnarrowed subscription is its target alone, not an empty pair");

assert.equal(api.watchTargetLabel({ target: "kid", kinds: [] }), "kid",
  "an empty kinds list means the defaults, not 'hears nothing'");

/* ---- the detail panel's section ---- */
assert.equal(api.sessWatchPanel({}), null, "no watch list: no section");
assert.equal(api.sessWatchPanel({ watch: [] }), null, "an empty list: no section");

const panel = api.sessWatchPanel({
  watch: [
    { target: "kid", kinds: ["reported", "done"], since: "2026-10-21T11:02:39+00:00" },
    { target: "other", kinds: null, since: null },
  ],
});
assert.equal(panel.tag, "section");
assert.equal(panel.className, "sess-watch");
assert.equal(panel.children[0].tag, "h3");
assert.equal(panel.children[0].textContent, "Watching");
assert.equal(panel.children[1].className, "wf-note");

const [first, second] = panel.children.slice(2);
assert.equal(first.className, "sess-watch-row");
assert.equal(first.children[0].className, "sess-watch-target");
assert.equal(first.children[0].textContent, "kid");
assert.equal(first.children[1].className, "sess-watch-kinds");
assert.equal(first.children[1].textContent, "reported, done");
assert.equal(first.children[2].className, "sess-watch-since");
assert(first.children[2].textContent.startsWith("since "), "the age is labelled");

// The same stamp the rest of the panel draws, not the raw ISO string.
const since = "2026-10-21T11:02:39+00:00";
const d = new Date(Date.parse(since));
const p2 = (n) => String(n).padStart(2, "0");
const stamp = `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ` +
  `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
assert.equal(first.children[2].textContent, `since ${stamp}`);

assert.equal(second.children[1].textContent, "all events",
  "a subscription with no kinds says so instead of leaving the column empty");
assert.equal(second.children.length, 2, "no recorded start draws no since cell");

console.log("watchrow_check: ok");
