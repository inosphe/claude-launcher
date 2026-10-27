/* Scratch storage on the two places this round asked for: one size pill on
   the session card, and the totals + per-path table in the detail panel.

   The number is the daemon's cached measurement (daemon/storage.py, carried
   by /api/sessions?view=rail as ``storage``); the panel's breakdown comes
   from /api/sessions/<name>/storage. These helpers only decide how that is
   drawn, so this holds the drawing: 1024-based units, nothing at zero, the
   hover carrying files and when it was measured, the table sorted as the
   daemon sent it with no click handler (cleanup is artifact_cleanup's, not
   this panel's), and the endpoint asked at most once per minute per session
   however often the panel is rebuilt. */
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

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

function mkel(tag, cls, text) {
  const node = {
    tag, className: cls || "", textContent: text === undefined ? "" : text,
    title: "", children: [], isConnected: true,
    set innerHTML(_v) { this.children = []; },
    get innerHTML() { return ""; },
    appendChild(child) { this.children.push(child); child.parent = this; return child; },
    append(...kids) { for (const k of kids) this.appendChild(k); },
    addEventListener() {},
    setAttribute() {},
  };
  return node;
}

const apiCalls = [];
let apiDoc = { entries: [] };
function api(url) {
  apiCalls.push(url);
  return Promise.resolve({
    ok: true,
    json: async () => apiDoc,
    text: async () => "",
  });
}

const seenMap = new Map();
const helpers = new Function(
  "document", "api", "sessStorageSeen", "Date",
  [slice("el"), slice("localStamp"), slice("storageSize"), slice("railStorageTag"),
   slice("sessStoragePanel")].join("\n")
  + "\nreturn {el, localStamp, storageSize, railStorageTag, sessStoragePanel};"
)({ createElement: mkel }, api, seenMap, Date);
const { storageSize, railStorageTag, sessStoragePanel } = helpers;

/* ---- the units ---- */
assert.equal(storageSize(0), "", "zero draws nothing, so callers can hide on it");
assert.equal(storageSize(1), "1 B");
assert.equal(storageSize(1023), "1023 B");
assert.equal(storageSize(1024), "1.0 KB");
assert.equal(storageSize(1536), "1.5 KB");
assert.equal(storageSize(82 * 1024 * 1024), "82 MB");
assert.equal(storageSize(12 * 1024 * 1024 * 1024), "12 GB");
assert.equal(storageSize(null), "");
assert.equal(storageSize("nonsense"), "");

/* ---- the rail's pill ---- */
assert.equal(railStorageTag({}), null, "no measurement yet: no pill");
assert.equal(railStorageTag({ storage: { bytes: 0, files: 0 } }), null,
  "an empty scratch and an unmeasured one both draw nothing");

const pill = railStorageTag({
  storage: { bytes: 82 * 1024 * 1024, files: 12345, scanned_at: "2026-10-22T06:57:09+00:00" },
});
assert.equal(pill.text, "82 MB");
assert(pill.title.startsWith("scratch 82 MB · 12345 files · measured "),
  "the hover carries size, file count and when it was measured: " + pill.title);

/* ---- the detail panel ---- */
const doc = {
  session: "s1", root: "/scratch", bytes: 2048, files: 3, dirs: 1,
  scanned_at: "2026-10-22T06:57:09+00:00", stale: false, truncated: false,
  error: null,
  entries: [
    { path: "big", bytes: 2000, files: 2 },
    { path: "a.txt", bytes: 48, files: 1 },
  ],
};
apiDoc = doc;
// Seeded as if this panel had asked once already: the redraw path is the one
// the 2s poll takes on every tick after the first, and it must draw without
// another request.
seenMap.set("s1", { at: Date.now(), doc, error: null });

const panel = sessStoragePanel("s1");
assert.equal(panel.tag, "section");
assert.equal(panel.className, "sess-storage");
assert.equal(panel.children[0].tag, "h3");
assert.equal(panel.children[0].textContent, "Storage");

const [head, tableBox] = panel.children.slice(1);
assert.equal(head.children[0].className, "sess-storage-total");
assert.equal(head.children[0].textContent, "2.0 KB · 3 files · 1 directories");
assert.equal(head.children[1].className, "sess-storage-when");
assert(!head.children[1].textContent.includes("stale"));
assert.equal(head.children[2].className, "sess-storage-refresh",
  "the button that pays for a new walk");
assert.equal(apiCalls.length, 0, "a rebuild inside the minute asks nothing");

const table = tableBox.children[0];
assert.equal(table.className, "sess-storage-table");
const rows = table.children[1].children;     // tbody
assert.equal(rows.length, 2);
assert.equal(rows[0].children[0].textContent, "big");
assert.equal(rows[0].children[1].textContent, "2.0 KB");
assert.equal(rows[0].children[2].textContent, "2");
assert.equal(rows[1].children[0].textContent, "a.txt",
  "the daemon's order is the drawn order (largest first)");

// The section is read-only: nothing in the table took a click handler, and
// the only button is the refresh. A delete control here would be a second,
// unreviewed way into cleanup.
for (const row of rows) {
  assert.equal(row.children.length, 3, "path, size, files -- no action cell");
}

// Rebuilt again: still no request.
const again = sessStoragePanel("s1");
assert.equal(apiCalls.length, 0, "a redraw inside the minute replays the cache");
assert.equal(again.children[1].children[0].textContent,
  "2.0 KB · 3 files · 1 directories");

/* ---- a truncated or stale answer says so ---- */
(async () => {
  // A first open (nothing cached) asks once, for this session only.
  const first = new Function(
    "document", "api", "sessStorageSeen", "Date",
    [slice("el"), slice("localStamp"), slice("storageSize"), slice("railStorageTag"),
     slice("sessStoragePanel")].join("\n")
    + "\nreturn {sessStoragePanel};"
  )({ createElement: mkel },
    (url) => { apiCalls.push(url);
      return Promise.resolve({ ok: true, json: async () => doc, text: async () => "" }); },
    new Map(), Date);
  first.sessStoragePanel("s4");
  assert.equal(apiCalls.length, 1);
  assert.equal(apiCalls[0], "/api/sessions/s4/storage");

  const fresh = new Map();
  const helpers2 = new Function(
    "document", "api", "sessStorageSeen", "Date",
    [slice("el"), slice("localStamp"), slice("storageSize"), slice("railStorageTag"),
     slice("sessStoragePanel")].join("\n")
    + "\nreturn {sessStoragePanel};"
  )({ createElement: mkel },
    (url) => { apiCalls.push(url);
      return Promise.resolve({ ok: true, json: async () => ({
        session: "s2", root: "/r", bytes: 5, files: 1, dirs: 0,
        scanned_at: "2026-10-22T06:57:09+00:00", stale: true, truncated: true,
        error: "PermissionError: denied",
        entries: [{ path: "x", bytes: 5, files: 1 }],
      }), text: async () => "" }); },
    fresh, Date);

  const box = helpers2.sessStoragePanel("s2");
  await new Promise((r) => setImmediate(r));
  const when = box.children[1].children[1];
  assert(when.textContent.includes("stale"), "a stale number says so: " + when.textContent);
  assert(when.textContent.includes("partial walk"),
    "a walk that hit its budget says so: " + when.textContent);

  // A dead endpoint: the section says why and offers a retry, and the rest of
  // the panel is not the section's to lose.
  const failing = new Function(
    "document", "api", "sessStorageSeen", "Date",
    [slice("el"), slice("localStamp"), slice("storageSize"), slice("railStorageTag"),
     slice("sessStoragePanel")].join("\n")
    + "\nreturn {sessStoragePanel};"
  )({ createElement: mkel },
    () => Promise.reject(new Error("HTTP 500")),
    new Map(), Date);
  const dead = failing.sessStoragePanel("s3");
  await new Promise((r) => setImmediate(r));
  const note = dead.children[1].children[0];
  assert.equal(note.className, "wf-note");
  assert(note.textContent.includes("measurement unavailable"),
    "an unmeasurable session says so: " + note.textContent);
  assert(note.textContent.includes("HTTP 500"), "and gives the reason");

  console.log("railstorage_check: ok");
})();
