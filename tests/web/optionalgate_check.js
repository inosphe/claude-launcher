/* A gate the run is NOT stopped on, on the dashboard's notification channel
   (claunch-hbmhk).

   `test-skip-gate` in improv-worker-v2 is a `skip` button racing the
   targeted-test loop under `settle: race`: the engine cancels it when the
   loop finishes, so leaving it alone costs nothing. The rail carries it in
   `optional` and not in `owed`, and this harness holds what app.js decides
   from that on its own:

   * the card's title says what it is — "waits on your pick" would be false
     of a run that is not waiting;
   * the age reads as how long the door has been OPEN ("open 5m"), not how
     long the reader has held the run ("5m ago"), and the page's one-second
     tick keeps saying that rather than falling back to "ago";
   * a card that learns the flag later (a poll that carried no `optional`,
     then one that did) is re-aged into the right wording in place;
   * every other gate keeps the words and the age it had.

   The real functions are sliced out of app.js and driven against stubs. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                        "web", "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");

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

const wordsStart = src.indexOf("const RUN_GATE_WORDS = {");
const wordsEnd = src.indexOf("\n};", wordsStart) + 3;
if (wordsStart < 0 || wordsEnd < 3) throw new Error("missing RUN_GATE_WORDS");
const WORDS = src.slice(wordsStart, wordsEnd);

let failures = 0;
function check(name, ok, detail) {
  if (!ok) {
    failures++;
    console.log("FAIL " + name +
      (detail === undefined ? "" : ` — ${JSON.stringify(detail)}`));
  }
}

/* ---- the stub world --------------------------------------------------- */
const ages = new Set();
function el(tag, cls, text) {
  const node = {
    tag, className: cls || "", children: [], parent: null, textContent: "",
    appendChild(c) { this.children.push(c); c.parent = this; return c; },
    querySelector(sel) {
      return this.children.find(
        (c) => c.className.split(" ").includes(sel.slice(1))) || null;
    },
  };
  if (text !== undefined) node.textContent = text;
  return node;
}

const ctx = {
  Math, console, Date,
  el,
  noticeAges: ages,
  // The clock the ages are measured against, so "5m" is a number this
  // harness chose and not whatever the machine's clock says.
  Date: class extends Date {
    static now() { return ctx.NOW; }
  },
  NOW: 0,
  document: { createElement: el },
};
vm.createContext(ctx);
vm.runInContext([
  WORDS,
  slice("noticeStamp"),
  slice("noticeElapsed"),
  slice("noticeAgeText"),
  slice("noticeAgeLine"),
  slice("noticeAgeSetFor"),
  slice("noticeAgesPaint"),
  slice("runGateTitle"),
].join("\n"), ctx);

/* Fixed instants: a local wall clock at a round minute, and five minutes on. */
const OPENED = new Date(2026, 10, 4, 13, 47, 0).getTime();
ctx.NOW = OPENED + 5 * 60 * 1000;

/* ---- the card's title ------------------------------------------------ */
check("an optional gate does not say the run waits on the pick",
      ctx.runGateTitle({ kind: "choose", label: "test-skip-gate", optional: true }),
      "has an optional gate open: test-skip-gate");
check("...and a gate the run IS stopped on keeps its word",
      ctx.runGateTitle({ kind: "choose", label: "landing" }),
      "waits on your pick: landing");
check("...an ask keeps its own",
      ctx.runGateTitle({ kind: "ask", label: "coding-approval" }),
      "waits on your answer: coding-approval");

/* ---- the age ---------------------------------------------------------- */
check("a stopping gate's age counts how long it has waited",
      / · 5m ago$/.test(ctx.noticeAgeText(OPENED)), true,
      ctx.noticeAgeText(OPENED));
check("an optional gate's age counts how long it has been open",
      / · open 5m$/.test(ctx.noticeAgeText(OPENED, true)), true,
      ctx.noticeAgeText(OPENED, true));

/* The line the card draws, and the tick that keeps it true. */
const line = ctx.noticeAgeLine(OPENED, true);
check("the card's age line is marked optional", line.__noticeAge.optional, true);
check("...and says so at once", / · open 5m$/.test(line.__noticeAge.node.textContent),
      true, line.__noticeAge.node.textContent);
ctx.NOW = OPENED + 70 * 60 * 1000;
ctx.noticeAgesPaint();
check("the one-second tick keeps the optional wording",
      / · open 1h10m$/.test(line.__noticeAge.node.textContent), true,
      line.__noticeAge.node.textContent);

/* A card drawn before the answer carried the flag is re-aged, not rebuilt:
   the wording follows the flag as well as the stamp. */
ctx.NOW = OPENED + 5 * 60 * 1000;
const plain = ctx.noticeAgeLine(OPENED);
check("a card drawn without the flag says 'ago'", / · 5m ago$/.test(
      plain.__noticeAge.node.textContent), true, plain.__noticeAge.node.textContent);
const root = { querySelector: () => plain };
ctx.noticeAgeSetFor(root, OPENED, true);
check("...and is re-aged into 'open' when the flag arrives",
      / · open 5m$/.test(plain.__noticeAge.node.textContent), true,
      plain.__noticeAge.node.textContent);
ctx.noticeAgeSetFor(root, OPENED, false);
check("...and back if a later poll says it is a stop after all",
      / · 5m ago$/.test(plain.__noticeAge.node.textContent), true,
      plain.__noticeAge.node.textContent);

/* Two separate cards keep their own wording: the record carries the flag. */
const stop = ctx.noticeAgeLine(OPENED, false);
const door = ctx.noticeAgeLine(OPENED, true);
ctx.NOW = OPENED + 60 * 1000;
ctx.noticeAgesPaint();
check("the tick does not smear one card's wording onto another",
      [/ · 1m ago$/.test(stop.__noticeAge.node.textContent),
       / · open 1m$/.test(door.__noticeAge.node.textContent)], [true, true],
      [stop.__noticeAge.node.textContent, door.__noticeAge.node.textContent]);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("optionalgate_check: ok");
