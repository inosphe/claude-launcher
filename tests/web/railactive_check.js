/* The rail's "you are here" mark — the card of the session whose terminal is
   on screen.

   `currentName` in app.js puts `.active` on exactly one row, and the three
   navigation paths (attach, the boot landing, the hash route) move it. Until
   this harness existed, that row was marked with a 2px edge in a muted blue
   on the card's transparent face: the weight of the hover tint and one hue
   away from it, so on a rail of hundreds of rows the mark was something the
   reader had to hunt for. The request behind the change is exactly that —
   "highlight the card of the session being rendered more clearly" — so what
   is pinned here is the strength of the mark, not one particular colour.

   Three things have to hold, and they fail in different ways:

   * the rule DECLARES a face (a fill the plain rows do not have, an accent
     bar, a rim, a brighter name) — a rule that keeps `background:
     transparent` is the weakness the change was asked to remove;
   * the rule WINS the cascade against every tint it shares a row with
     (`li:hover`, `li.active`, `li.child.active`, `li.kin-outside.active`,
     `li.sess-card`'s own `background: transparent`). A mark that exists but
     loses the cascade paints nothing, which is how the child flash was lost
     once already (see railcardmode_check.js); specificity is computed here
     rather than eyeballed;
   * the rule does not MOVE the row. The inset ring is deliberate — a border
     or padding would change an active row's measured height, and the rail's
     height model (railRowKey/railEstimatePx, app.js) counts those heights.

   railcards_browser.cjs measures the painted face on the real page, because
   a declaration and a painting are different claims. This harness needs
   neither node modules nor a browser. */
const fs = require("fs");
const path = require("path");

const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");
/* Comments out: this file explains itself with the very selectors it
   declares, and a comment must not be able to satisfy a check. */
const plain = css.replace(/\/\*[\s\S]*?\*\//g, "");

let failures = 0;
function ok(what, cond, detail) {
  if (!cond) { console.error(`FAIL ${what}${detail ? "\n  " + detail : ""}`); failures++; }
}
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

function rule(sel) {
  const escaped = sel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const m = plain.match(new RegExp(escaped + "\\s*\\{([^}]*)\\}"));
  return m ? m[1] : "";
}

const ACTIVE = "#session-list .sess-card.active:not(.goto-flash)";

/* ---- the face ---------------------------------------------------------- */
const face = rule(ACTIVE);
ok("the active card has a rule of its own", !!face, "no rule for " + ACTIVE);

const fill = (face.match(/background:\s*([^;]+);/) || [])[1] || "";
ok("it fills the card instead of leaving it transparent",
   !!fill && !/transparent|none/.test(fill), `background: ${fill.trim() || "(none)"}`);

const shadow = (face.match(/box-shadow:\s*([\s\S]*?);/) || [])[1] || "";
const bar = (shadow.match(/inset\s+(\d+)px\s+0\s+0\s+([^,;]+)/) || []);
ok("...with an accent bar on the leading edge",
   !!bar[1] && Number(bar[1]) >= 2 && /#58a6ff/i.test(bar[2] || ""),
   `box-shadow: ${shadow.trim() || "(none)"}`);
ok("...at least as thick as the muted edge it replaced (2px)",
   Number(bar[1]) >= 2, `bar ${bar[1]}px`);
ok("...and a hairline rim that closes the card",
   /inset\s+0\s+0\s+0\s+1px/.test(shadow),
   `box-shadow: ${shadow.trim() || "(none)"}`);

const name = rule(`${ACTIVE} .rail-name`);
ok("the name is lifted, not left at its neighbours' contrast",
   /color:\s*#f0f6fc/i.test(name), name.trim() || "no rule");

/* An inset shadow paints inside the box: no border, padding or margin may
   ride along, or the marked row measures taller than the same row unmarked
   and the rail's height model drifts by one row's delta. */
ok("the mark does not change the row's box",
   !/(^|[;{\s])(padding|margin|border)\b/.test(face), face.trim().slice(0, 120));

/* ---- the cascade -------------------------------------------------------
   Specificity as (ids, classes, elements); `:not(x)` contributes its
   argument's, and the pseudo-classes this rail uses (:hover) count as
   classes. Enough for the flat selectors below — anything with a comma or a
   combinator this function cannot read would be visible here as a wrong
   number rather than silently passing. */
function specificity(sel) {
  let s = sel.trim().replace(/:not\(([^)]*)\)/g, (_, inner) => " " + inner + " ");
  const ids = (s.match(/#[\w-]+/g) || []).length;
  const classes = (s.match(/\.[\w-]+|:[\w-]+/g) || []).length;
  const elements = (s.split(/[\s>+~]+/).filter(Boolean)
    .map((part) => part.replace(/[#.:][\w-]+/g, ""))
    .filter(Boolean)).length;
  return [ids, classes, elements];
}
function beats(a, b) {
  const [ai, ac, ae] = specificity(a), [bi, bc, be] = specificity(b);
  if (ai !== bi) return ai > bi;
  if (ac !== bc) return ac > bc;
  return ae > be;
}

check("the active selector is read as one id, three classes",
      specificity(ACTIVE), [1, 3, 0]);
for (const tint of ["#session-list li:hover",
                    "#session-list li.active",
                    "#session-list li.child.active",
                    "#session-list li.kin-outside.active",
                    "#session-list li.sess-card",
                    "#session-list li.sess-card[data-rail-mode=\"min\"]"]) {
  ok(`the active face outranks ${tint}`,
     beats(ACTIVE, tint),
     `${JSON.stringify(specificity(ACTIVE))} vs ${JSON.stringify(specificity(tint))}`);
}

/* ---- the colours say different things ----------------------------------
   The fill has to be separable from the two faces it competes with on the
   same strip of screen: the plain card (transparent, so: the rail itself)
   and the hover tint. Equal colours would satisfy every assertion above and
   still leave the reader unable to find the row. */
const railBg = (rule("#sidebar").match(/background:\s*([^;]+);/) || [])[1] || "";
const hover = (rule("#session-list li:hover").match(/background:\s*([^;]+);/) || [])[1] || "";
ok("the rail and the hover tint are both declared",
   !!railBg && !!hover, `rail ${railBg.trim()}, hover ${hover.trim()}`);
ok("the active fill is not the rail's own background",
   !!fill && fill.trim() !== railBg.trim(), `${fill.trim()} vs ${railBg.trim()}`);
ok("...nor the hover tint",
   !!fill && fill.trim() !== hover.trim(), `${fill.trim()} vs ${hover.trim()}`);

/* ---- the flash still wins -----------------------------------------------
   `.goto-flash` (⇱ card) is a momentary, stronger claim: "the row you just
   asked for". The active rule must keep excluding it, or the two faces fight
   on exactly the rows the flash exists for. */
ok("the active rule still excludes the flash",
   /:not\(\.goto-flash\)/.test(ACTIVE) && !!face,
   "the selector no longer excludes .goto-flash");
ok("...and the flash still declares a face of its own",
   /background:/.test(rule("#session-list li.goto-flash")),
   rule("#session-list li.goto-flash").trim() || "no rule");

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("railactive_check: ok");
