/* A rail row's kind and progress (claunch-siz7t), run against the real
   functions from app.js on a stub DOM.

   - railMetaText puts the declared kind right after the identity it
     qualifies ("ds4/pi · pi-durable"), and a row without one reads as before.
   - progressBrief draws the same line `claunch sessions` prints
     (cli_sessions.progress_brief): each part only when published.
   - decorateProgressRow adds the line ahead of the path, updates it in
     place, and removes it when the session clears its progress; the hover
     names the status file and the time without the page ever opening it. */

const fs = require("fs");
const path = require("path");
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

function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), title: "",
    appendChild(c) { c.parent = n; n.kids.push(c); return c; },
    insertBefore(c, ref) {
      c.parent = n;
      const i = n.kids.indexOf(ref);
      n.kids.splice(i < 0 ? n.kids.length : i, 0, c);
      return c;
    },
    remove() { if (n.parent) n.parent.kids = n.parent.kids.filter((k) => k !== n); },
    querySelector(sel) {
      const cls = sel.replace(/^\./, "");
      return n.kids.find((k) => k.classes.has(cls)) || null;
    },
    get textContent() { return n.text; },
    set textContent(v) { n.text = String(v); },
    set className(v) { n.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
  };
  return n;
}
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const api = new Function("el", [
  slice("profileHarnessLabel"), slice("railMetaText"), slice("progressBrief"),
  slice("decorateProgressRow"), slice("railNotePlace"),
  "return { railMetaText, progressBrief, decorateProgressRow };",
].join("\n"))(el);

function eq(got, want, what) {
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    throw new Error(`${what}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
  }
}

eq(api.railMetaText({ profile: "ds4:pi", harness: "pi", kind: "pi-durable", status: "busy" }),
   "ds4/pi · pi-durable", "kind follows the identity");
eq(api.railMetaText({ profile: "ds4:pi", harness: "pi", status: "busy" }),
   "ds4/pi", "no kind, line unchanged");
eq(api.railMetaText({ profile: "ds4:pi", harness: "pi", kind: "pi-durable",
                      status: "exited", exit_code: 0 }),
   "ds4/pi · pi-durable · exit 0", "state still last");

eq(api.progressBrief({ stage: "ask", done: 3, total: 10, active: 2, text: "B" }),
   "ask 3/10 (2 active) B", "full brief");
eq(api.progressBrief({ total: 4 }), "0/4", "total alone");
eq(api.progressBrief({ file: "/x" }), "", "file alone draws nothing");
eq(api.progressBrief(undefined), "", "absent");

const li = node("li");
const cwd = el("div", "rail-cwd", "/w");
li.appendChild(el("span", "name", "sim-1"));
li.appendChild(cwd);

api.decorateProgressRow(li, { progress: { stage: "ask", done: 1, total: 3,
  file: "/runs/1/status.json", at: "2026-10-11T08:00:00+00:00" } });
let line = li.querySelector(".rail-progress");
eq(line && line.textContent, "ask 1/3", "line drawn");
eq(li.kids.indexOf(line) < li.kids.indexOf(cwd), true, "ahead of the path");
eq(line.title.includes("file: /runs/1/status.json") && line.title.includes("at: "), true,
   "hover names file and time");

api.decorateProgressRow(li, { progress: { stage: "judge", done: 2, total: 3 } });
eq(li.kids.filter((k) => k.classes.has("rail-progress")).length, 1, "updated in place");
eq(li.querySelector(".rail-progress").textContent, "judge 2/3", "new text");

api.decorateProgressRow(li, {});
eq(li.querySelector(".rail-progress"), null, "cleared progress removes the line");

console.log("railprogress_check: ok");
