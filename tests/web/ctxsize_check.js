/* The context reading, as the dashboard writes it, run against the real
   functions from app.js.

   The daemon hands each session a `context` block or nothing at all, and
   everything here is the wording of that. Two rules are what these checks
   exist for, because both are ways of lying with a true number:

   - No percentage is printed. Claude has no recorded hard limit. Codex
     supplies a model context window, which is named directly in the
     breakdown and marked on the gauge.
   - "Not known" is not a number. A session that has said nothing yet, and a
     harness that keeps no transcript, must not come out as 0 or as a blank
     that looks like one.

   The rail row's own gauge line (bar + short count) belongs to
   railctx_check; here it is the wording — the sentence, the breakdown, the
   tooltip they join into, and the briefing card's chip. */
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

const ctx = {};
new Function(
  "exports", "el", "sessionsCache",
  ["fmtAge", "ctxShort", "ctxAgeOf", "ctxKnowable", "ctxSentence",
   "ctxBreakdown", "ctxTooltip", "ctxNoteOnRow", "ctxChip"].map(slice).join("\n") + `
exports.onRow = ctxNoteOnRow;
exports.short = ctxShort;
exports.sentence = ctxSentence;
exports.tooltip = ctxTooltip;
exports.chip = ctxChip;
exports.setSessions = (s) => { sessionsCache = s; };
`)(ctx,
   (tag, cls, text) => ({ tag, className: cls || "", textContent: text ?? "",
                          title: "" }),
   []);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

/* ---- the number itself ---- */
check("under a thousand is written out", ctx.short(742), "742");
check("a small count keeps a decimal so 1.2k and 1.9k differ",
      [ctx.short(1200), ctx.short(9400)], ["1.2k", "9.4k"]);
check("a large one is rounded — nobody compares the last 500 tokens",
      [ctx.short(59088), ctx.short(154706)], ["59k", "155k"]);
check("a missing count is not a small one", ctx.short(undefined), "?");

/* ---- the sentence ---- */
const AT = new Date(Date.now() - 185_000).toISOString();   // 3m ago
const READING = {
  tokens: 154706, input: 2, cache_read: 154073, cache_write: 631,
  output: 210, model: "claude-opus-5", at: AT,
};
const LIVE = { name: "lead", harness: "claude", context: READING };
const QUIET = { name: "quiet", harness: "claude" };        // nothing said yet
const CODEX_READING = {
  tokens: 187281, input: 1169, cache_read: 186112, cache_write: 0,
  output: 59, model: "gpt-5.6-sol", at: AT,
  model_context_window: 258400,
};
const CODEX = { name: "codex", harness: "codex", context: CODEX_READING };
const CODEX_QUIET = { name: "codex-quiet", harness: "codex" };
const OTHER = { name: "pi", harness: "pi" };

check("the count, the model, and how old the count is",
      ctx.sentence(LIVE),
      `context ${(154706).toLocaleString()} tokens · claude-opus-5 · as of 3m ago`);
check("a session that has not answered yet says so, in words",
      ctx.sentence(QUIET), "context not known yet — no reading recorded");
check("a Codex rollout reading uses the same context sentence",
      ctx.sentence(CODEX),
      `context ${(187281).toLocaleString()} tokens · gpt-5.6-sol · as of 3m ago`);
check("a Codex session with no request recorded says so",
      ctx.sentence(CODEX_QUIET), "context not known yet — no reading recorded");
check("a harness that keeps no transcript says nothing at all",
      ctx.sentence(OTHER), "");
check("a reading with no timestamp still dates itself honestly",
      ctx.sentence({ harness: "claude", context: { ...READING, at: null } })
        .endsWith("as of its last turn"), true);

/* ---- the tooltip: the sentence, plus how the number is made up ---- */
const tip = ctx.tooltip(LIVE);
check("the tooltip leads with the sentence", tip.split("\n")[0], ctx.sentence(LIVE));
check("and breaks the input side down, since it is one number billed three ways",
      ["fresh input", "replayed from cache", "written to cache", "answer"]
        .every((k) => tip.includes(k)), true);
check("...saying out loud why there is no percentage",
      tip.includes("the context limit is not recorded anywhere"), true);
const codexTip = ctx.tooltip(CODEX);
check("Codex names its reported model context window",
      codexTip.includes("model context window 258,400 tokens"), true);
check("Codex does not claim that its context limit is unavailable",
      codexTip.includes("context limit is not recorded"), false);
check("no percentage is offered anywhere — there is no denominator to make one",
      /%/.test(tip + codexTip), false);
check("an unknown session's tooltip is the sentence and nothing more",
      ctx.tooltip(QUIET), "context not known yet — no reading recorded");
check("a harness with no transcript adds nothing to its row's tooltip",
      ctx.tooltip(OTHER), "");

/* ---- the chip on the briefing card's head ---- */
ctx.setSessions([LIVE, QUIET, CODEX, CODEX_QUIET, OTHER]);
const lead = ctx.chip("lead");
check("the chip is the count, short, and carries the full story on hover",
      [lead.textContent, lead.className, lead.title === tip],
      ["155k ctx", "sess-brief-ctx", true]);
const quiet = ctx.chip("quiet");
check("a session with no reading gets a marked absence, not a zero",
      [quiet.textContent, quiet.className],
      ["ctx ?", "sess-brief-ctx unknown"]);
check("and a harness that never has one gets no chip",
      ctx.chip("pi"), null);
check("Codex gets the same compact context chip",
      [ctx.chip("codex").textContent, ctx.chip("codex").title === codexTip],
      ["187k ctx", true]);
check("Codex without a reading gets a marked absence",
      ctx.chip("codex-quiet").className, "sess-brief-ctx unknown");
check("nor does a session the rail no longer knows", ctx.chip("gone"), null);

/* ---- what the rail row ends up carrying ---- */
/* The row, and the name inside it. A child's title wins wherever the pointer
   lands, and the name is where it lands — so the note has to go on both, and
   it has to join what the name already said rather than take its place: the
   rail clips a long name and that tooltip is where the rest of it went. */
function row(title = "") { return { title }; }

let li = row("spawned by lead"), nm = row("a-very-long-session-name");
ctx.onRow(li, nm, LIVE);
check("the row keeps what it already said and adds the reading",
      li.title, `spawned by lead\n${ctx.tooltip(LIVE)}`);
check("and the name does too — a clipped name must still be readable",
      nm.title, `a-very-long-session-name\n${ctx.tooltip(LIVE)}`);

li = row(); nm = row();
ctx.onRow(li, nm, QUIET);
check("a session with no reading says so on both",
      [li.title, nm.title], [ctx.tooltip(QUIET), ctx.tooltip(QUIET)]);

li = row("exited — open it to resume"); nm = row("pi");
ctx.onRow(li, nm, OTHER);
check("and a harness that never has one leaves both exactly as they were",
      [li.title, nm.title], ["exited — open it to resume", "pi"]);

li = row("solo");
ctx.onRow(li, null, LIVE);
check("a row with no name element beside it is not a crash",
      li.title.startsWith("solo"), true);

if (failures) {
  console.error(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("ctxsize_check: ok");
