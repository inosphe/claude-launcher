/* The IME path of the SHIPPED xterm bundle, driven with the event sequence a
   Korean keyboard produces.

   Korean commits a syllable every two or three keystrokes, and each commit
   ends one composition and starts the next. xterm reads the committed text
   out of the textarea by offset, and 5.5.0 recorded the END of that range
   only from the timer `compositionupdate` schedules. A typist fast enough to
   commit the next syllable before that timer runs left the range stale, the
   slice came back empty, and the syllable was never sent — continuously,
   because "fast enough" for Korean is ordinary typing speed. The web
   terminal was the only surface affected: `claunch attach` has no browser in
   it, and the session line's send-keys box is a plain <input> the browser
   composes into itself.

   So this drives the real CompositionHelper out of the vendored bundle — not
   a copy of its logic, which would pin the wrong thing — at several typing
   speeds, and asserts every keystroke arrives. The bundle is the fixture: a
   downgrade past the fix fails here. */
const fs = require("fs");
const path = require("path");

const BUNDLE = path.join(__dirname, "..", "..", "src", "claude_launcher",
                         "web", "static", "vendor", "xterm.js");

/* Lift the class out of the minified bundle. It is a class expression with a
   known head, and its body is balanced, so the braces locate it exactly. */
function compositionHelperClass() {
  const src = fs.readFileSync(BUNDLE, "utf8");
  const head = "class{get isComposing(){return this._isComposing}constructor(";
  const at = src.indexOf(head);
  if (at < 0) throw new Error("no CompositionHelper in " + BUNDLE);
  const open = src.indexOf("{", at + 5);
  let depth = 0, close = -1;
  for (let i = open; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (!depth) { close = i + 1; break; } }
  }
  const body = src.slice(at, close);
  // The one module it reaches out to inside the class body is the escape
  // sequence table, for the backspace it sends on a shrinking textarea.
  const dep = body.match(/([A-Za-z$_]+)\.C0\.DEL/);
  return new Function(dep ? dep[1] : "ESC", "return (" + body + ")")(
    { C0: { DEL: String.fromCharCode(0x7f) } });
}

const Helper = compositionHelperClass();

/* A textarea and the two services the composition path touches. */
function harness() {
  const sent = [];
  const textarea = { value: "", selectionStart: 0, selectionEnd: 0 };
  const view = { textContent: "", style: {}, classList: { add() {}, remove() {} } };
  const helper = new Helper(
    textarea, view,
    { cols: 80, rows: 24, buffer: { x: 0, y: 0, isCursorInViewport: false } },
    { rawOptions: { fontFamily: "monospace", fontSize: 13 } },
    { triggerDataEvent: (d) => sent.push(d) },
    { dimensions: { css: { cell: { width: 8, height: 16 } } } }
  );
  return { helper, textarea, sent };
}

/* The jamo a 2-set Korean keyboard walks through to build one syllable, and
   the intermediate strings the IME shows on the way: ㅎ, 하, 한. */
const LEAD = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ";
function keystrokes(syllable) {
  const code = syllable.charCodeAt(0) - 0xac00;
  if (code < 0 || code > 11171) return [syllable];   // space, ASCII: one key
  const lead = Math.floor(code / 588);
  const vowel = Math.floor((code % 588) / 28);
  const tail = code % 28;
  const out = [LEAD[lead], String.fromCharCode(0xac00 + lead * 588 + vowel * 28)];
  if (tail) out.push(syllable);
  return out;
}

const tick = () => new Promise((r) => setTimeout(r, 5));

/* Type `text`. `perTask` is how many keystrokes share one task before the
   timers get to run — 1 is a slow typist, 4 is an ordinary one. */
async function type(text, perTask) {
  const { helper, textarea, sent } = harness();
  let committed = "", composing = null, pending = 0;
  for (const ch of text) {
    const keys = keystrokes(ch);
    for (let i = 0; i < keys.length; i++) {
      // Every key during composition reaches keydown as 229.
      helper.keydown({ keyCode: 229 });
      if (i === 0 && composing !== null) {
        helper.compositionend();          // the previous syllable commits
        committed += composing;
        composing = null;
      }
      if (i === 0) helper.compositionstart();
      helper.compositionupdate({ data: keys[i] });
      composing = keys[i];
      textarea.value = committed + keys[i];
      textarea.selectionStart = textarea.selectionEnd = textarea.value.length;
      if (++pending >= perTask) { await tick(); pending = 0; }
    }
  }
  helper.keydown({ keyCode: 229 });
  helper.compositionend();                // Enter or space ends the last one
  textarea.value = committed + (composing || "");
  await tick();
  return sent.join("");
}

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

const PHRASES = [
  "가나다라",
  "한글",
  "안녕하세요",
  "이 즉시 머지해줘",
];

async function main() {
  for (const perTask of [1, 2, 3, 4, 8]) {
    for (const phrase of PHRASES) {
      const got = await type(phrase, perTask);
      check(`${perTask} keystroke(s) per task: ${phrase}`, got === phrase,
            { want: phrase, got });
    }
  }

  /* The shape of the fix, asserted directly: a composition that ends while
     the next one has already started must be sliced at the new composition's
     start. 5.5.0 sliced at a stale end and produced "". */
  {
    const { helper, textarea, sent } = harness();
    helper.compositionstart();                       // "가"
    helper.compositionupdate({ data: "ㄱ" });
    textarea.value = "ㄱ";
    helper.compositionupdate({ data: "가" });
    textarea.value = "가";
    helper.compositionend();                         // commits, no tick yet
    helper.compositionstart();                       // "나" begins at once
    helper.compositionupdate({ data: "ㄴ" });
    textarea.value = "가ㄴ";
    await tick();
    check("a commit racing the next composition still sends its syllable",
          sent.join("") === "가", sent);
  }

  console.log(failures ? `\n${failures} failure(s)`
                       : "all composition checks passed");
  process.exit(failures ? 1 : 0);
}

main();
