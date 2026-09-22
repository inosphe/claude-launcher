/* The observe-pin sentence, and the one place all four surfaces read it from.

   One flag is drawn in four places: the session tab, the rail row, the
   observer card's action chip, and the detail panel's Flags box. Each draws
   its own control, but the words have to come from one function, because the
   sentence names a DIRECTION -- what the press will do -- and a fixed one is
   wrong in exactly half the states it is shown in.

   The observer card used to write its own:

     pin.title = `「고정만 관찰」 모드에서 ${s.name}을 관찰 대상에 넣습니다`

   which says "넣습니다" while the flag is already on, telling the reader the
   opposite of what the press would do. The comment over `observePinTitle`
   said the surfaces shared one sentence; the observer card was not calling
   it, so the comment described a state that was not standing.

   What has to hold, and what each group below is for:

     direction      -- the card's title differs between the on state and the
                       off state, and each one is the sentence
                       `observePinTitle` makes for that state. Equality with
                       that function is the check, not the Korean words: the
                       words may be rewritten, and when they are, one edit has
                       to move all four surfaces.
     read, not pressed -- the title, `aria-pressed` and the lit class all come
                       off the session record. A card that drew its own
                       pressed state would read as applied after a write the
                       daemon refused.
     no second copy -- observer.js holds no literal of that sentence any more.
                       A fallback string kept "for safety" is a fourth wording
                       that nothing updates.
     all four call it -- the tab, the rail row and the Flags box are checked on
                       app.js source. They already called it; this pins that
                       they keep calling it, because the defect this round
                       fixed is what happens when one surface stops.
     reachable      -- observer.js is wrapped in one IIFE and reaches app.js
                       globals through the scope chain, the way `request()`
                       already reaches `api()`. Both are classic scripts in
                       index.html, and cardActions runs at render time. The
                       load order is checked on index.html so a later edit
                       cannot quietly put observer.js in a document without
                       app.js.

   The click round trip is not checked here. `observer_browser.cjs` covers it
   and skips without Playwright, which is recorded as a gap rather than
   papered over: this harness is about which sentence is drawn, not about what
   the press does to the daemon. */
const fs = require("fs");
const path = require("path");
const root = path.join(__dirname, "..", "..", "src", "claude_launcher", "web",
                       "static");
const app = fs.readFileSync(path.join(root, "app.js"), "utf8");
const observer = fs.readFileSync(path.join(root, "observer.js"), "utf8");
const index = fs.readFileSync(path.join(root, "index.html"), "utf8");

function slice(src, name) {
  let start = src.indexOf(`async function ${name}(`);
  if (start < 0) start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

/* ---- stub DOM ---------------------------------------------------------- */
function makeNode(tag, text, cls) {
  const n = {
    tag, kids: [], text: text === undefined ? "" : String(text),
    classes: new Set(String(cls || "").split(/\s+/).filter(Boolean)),
    title: "", attrs: {}, disabled: false, type: "", onclick: null,
    append(...cs) { this.kids.push(...cs); },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    querySelector() { return this; },
  };
  n.classList = {
    toggle(name, on) { if (on) n.classes.add(name); else n.classes.delete(name); },
    contains(name) { return n.classes.has(name); },
  };
  return n;
}

/* ---- the real functions, cut out of the two files ---------------------- */
const ctx = {};
new Function("exports", "observePinTitle", slice(app, "observePinTitle") +
             "\nexports.observePinTitle = observePinTitle;")(ctx);
const observePinTitle = ctx.observePinTitle;

const pinWrites = [];
const cardCtx = {};
new Function(
  "exports", "node", "observePinTitle", "setObservePin", "refreshing",
  "refreshNotes", "chooseTarget", "composerFolded", "composerState", "$",
  "oneShot",
  slice(observer, "cardActions") + "\nexports.cardActions = cardActions;"
)(
  cardCtx, makeNode, observePinTitle,
  (name, on) => { pinWrites.push([name, on]); },
  new Set(), new Map(), () => {}, true, () => {}, () => makeNode("div"),
  () => {}
);
const cardActions = cardCtx.cardActions;

function drawCard(session) {
  const links = makeNode("div", "", "observer-links");
  const card = { querySelector: () => links };
  cardActions(card, session);
  const pin = links.kids.find((k) => k.classes.has("observer-pin"));
  if (!pin) throw new Error("the card drew no observe-pin control");
  return pin;
}

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* ---- direction: the two states say different things -------------------- */
const off = drawCard({ name: "s1", observe_pin: false, running: true });
const on = drawCard({ name: "s1", observe_pin: true, running: true });

check("the card says something different when the flag is on",
      off.title === on.title, false);
check("...and off, it is the sentence observePinTitle makes for off",
      off.title, observePinTitle("s1", false));
check("...and on, the one it makes for on",
      on.title, observePinTitle("s1", true));
/* The point of the round, said as the reader would hit it: with the flag on,
   the press removes the session, and the words have to say so. */
check("with the flag on it no longer offers to add what is already added",
      on.title.includes("넣습니다"), false);
check("the session's own name is in the sentence",
      on.title.includes("s1") && off.title.includes("s1"), true);

/* A second session, so nothing above passed on a name baked into the file. */
const other = drawCard({ name: "builder-7", observe_pin: true, running: false });
check("the sentence follows the session it is drawn for",
      other.title, observePinTitle("builder-7", true));

/* ---- read, not pressed ------------------------------------------------- */
check("aria-pressed on the on record", on.attrs["aria-pressed"], "true");
check("aria-pressed on the off record", off.attrs["aria-pressed"], "false");
check("the lit class comes off the record too", on.classList.contains("on"), true);
check("...and is absent when the record is off",
      off.classList.contains("on"), false);
check("an exited session still gets the control",
      drawCard({ name: "s2", observe_pin: false, running: false }).title,
      observePinTitle("s2", false));

/* The press asks for the opposite of the record. Drawing is this harness's
   subject, but a control that asked for what it already has would make every
   sentence above true and the button useless. */
pinWrites.length = 0;
on.onclick();
check("a press on a lit control asks for off", pinWrites, [["s1", false]]);
pinWrites.length = 0;
off.onclick();
check("a press on an unlit one asks for on", pinWrites, [["s1", true]]);

/* ---- no second copy of the sentence ------------------------------------ */
check("observer.js keeps no literal of the old fixed title",
      observer.includes("관찰 대상에 넣습니다"), false);
check("...and none of the wording observePinTitle owns",
      observer.includes("관찰 대상에서 빼기") ||
        observer.includes("관찰 대상으로 고정") ||
        observer.includes("Observer의 「고정만」 모드가 이 표시를 읽습니다"),
      false);
check("it calls the shared function instead",
      /pin\.title\s*=\s*observePinTitle\(/.test(observer), true);
/* No `typeof ... === "function"` guard with a string on the other side: a
   fallback sentence is the thing this round removed. */
check("and does not keep a fallback string beside the call",
      /observePinTitle[\s\S]{0,80}\?\s*["'`]/.test(observer), false);

/* ---- all four surfaces call it ----------------------------------------- */
const calls = (app.match(/observePinTitle\(/g) || []).length;
check("app.js calls it from more than one surface", calls >= 4, true);
check("the session tab calls it",
      /observe\.title = observePinTitle\(/.test(app), true);
check("the rail row calls it",
      /observePin\.title = observePinTitle\(/.test(app), true);
check("the detail panel's Flags box calls it",
      /"sess-flag-observe"[\s\S]{0,120}observePinTitle\(/.test(app), true);
/* The comment over the function is what a later reader checks their edit
   against, so it has to count the surfaces that exist. */
check("its comment names four surfaces, not three",
      /the tab, the rail row, the observer card and[\s\S]{0,120}Flags box/.test(app),
      true);

/* ---- reachable: one document, app.js parsed too ------------------------ */
const atObserver = index.indexOf("static/observer.js");
const atApp = index.indexOf("static/app.js");
check("index.html loads observer.js", atObserver >= 0, true);
check("...and app.js in the same document", atApp >= 0, true);
/* Order is not what makes the call work -- cardActions runs long after both
   scripts parse -- but a document with only one of them would break it, and
   that is what this pins. */
check("neither is a module, so both share one global scope",
      /<script src="static\/(observer|app)\.js" type="module"/.test(index), false);
check("observer.js is one IIFE, so its own helpers stay private",
      /globalThis\.ObserverPage\s*=\s*\(\(\)\s*=>\s*\{/.test(observer), true);
/* The precedent for reaching across: observer.js's request() already calls
   api(), which only app.js defines. */
check("it already reaches an app.js global elsewhere",
      /await api\(/.test(observer) && /async function api\(/.test(app), true);

if (failures) process.exit(1);
console.log("observerpin_check: ok");
