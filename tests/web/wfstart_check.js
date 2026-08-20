/* The workflow picker's option line, run against the real function from
   app.js.

   A native <option> cannot wrap, and the select's popup sizes itself to the
   widest option — so a paragraph-length workflow description used to drag
   the whole dropdown past the viewport edge, where the browser pins and
   clips it (measured: the widest real label rendered at 3438px against a
   1496px viewport). What has to hold: the label never carries more than a
   clipped first line of the description; folded-scalar newlines collapse to
   spaces so the clip window isn't spent on whitespace; the ellipsis appears
   exactly when something was cut; and a workflow without a description is
   just its name. */
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

const wfOptionLabel = new Function(`${slice("wfOptionLabel")}; return wfOptionLabel;`)();

function assert(cond, msg) {
  if (!cond) { console.error(`FAIL: ${msg}`); process.exit(1); }
}

// no description: the name stands alone, no dash and no ellipsis
assert(wfOptionLabel({ name: "feature-dev" }) === "feature-dev",
       "name-only workflow gets a bare name");
assert(wfOptionLabel({ name: "feature-dev", description: "" }) === "feature-dev",
       "empty description is the same as none");

// short description: shown whole, no ellipsis
assert(
  wfOptionLabel({ name: "fd", description: "design -> ship" }) ===
    "fd — design -> ship",
  "short description survives uncut"
);

// a boundary-length description (exactly 48 chars) is not cut
const exact = "x".repeat(48);
assert(wfOptionLabel({ name: "n", description: exact }) === `n — ${exact}`,
       "48-char description is uncut");

// a paragraph is clipped: the label's description part never exceeds
// 48 chars + ellipsis, however long the source is
const para = "자유 진행 워커 — 터미널 사용자(또는 리더 브리핑)가 주는 목표 하나가 한 회차다: "
  .repeat(8);
const label = wfOptionLabel({ name: "improv-worker", description: para });
const tail = label.slice("improv-worker — ".length);
assert(tail.endsWith("…"), "overlong description ends in an ellipsis");
assert(tail.length <= 49, `clipped tail stays within 48+ellipsis (got ${tail.length})`);

// folded-scalar newlines and runs of spaces collapse before clipping, so
// the 48-char window is spent on text rather than whitespace
const folded = "one\ntwo\n  three   four\nfive";
assert(
  wfOptionLabel({ name: "n", description: folded }) === "n — one two three four five",
  "newlines and space runs collapse to single spaces"
);

// the clip never strands a trailing space before the ellipsis
const spaceAtCut = `${"a".repeat(47)} b`;
assert(
  wfOptionLabel({ name: "n", description: spaceAtCut }) ===
    `n — ${"a".repeat(47)}…`,
  "a cut landing on a space trims it before the ellipsis"
);

console.log("wfstart_check: all assertions passed");
