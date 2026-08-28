/* The rail row's attention line — who has been near this session.

   Three readings on one line: when a person last LOOKED at the session, when
   a person last TYPED into it, and when the session itself last MOVED. The
   defect this guards against is not an arithmetic one. It is one reading
   silently standing in for another — the row keeps drawing three numbers,
   they keep looking plausible, and "seen" is now really "moved", which makes
   the one row an operator is hunting for (handed a task, then forgotten)
   indistinguishable from the twenty that are fine.

   So every check below pins a pair to its OWN field, and several pin what a
   pair must NOT react to. They also avoid naming the row's inner structure
   where they can: the line is found by class because that is the contract
   the stylesheet holds it to, but the pairs inside it are found by the key
   they print, so the line can be reordered or re-nested without this going
   red for the wrong reason.

   Held here rather than in railctx_check for the same reason that harness
   was split off from ctxsize_check: whether a value is right and whether it
   is still ATTACHED to anything are different failures, and the second one
   leaves no trace at all — no error, no wrong string, just a line that
   quietly stopped being appended. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  process.env.RAILSEEN_APP_JS || path.join(
    __dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8"
);
/* The colours are half of what this line says, and a class the stylesheet
   has no rule for is the one failure the DOM checks below cannot see: the
   markup keeps saying "stale" and the value keeps rendering in the plain
   grey it always had. */
const css = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "style.css"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot locate ${name} in app.js`);
  const head = src.lastIndexOf("async ", start) === start - 6 ? start - 6 : start;
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error(`unbalanced ${name}`);
}
function constLine(name) {
  const m = src.match(new RegExp(`^const ${name} = .+$`, "m"));
  if (!m) throw new Error(`cannot locate ${name} in app.js`);
  return m[0] + "\n";
}

/* ---- stub DOM ---- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), dataset: {}, style: {},
    title: "", type: "",
    appendChild(c) { n.kids.push(c); return c; },
    append(...cs) { cs.forEach((c) => n.appendChild(c)); },
    addEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    get textContent() { return n.text; },
    set textContent(v) { n.text = String(v); },
    get className() { return [...n.classes].join(" "); },
    set className(v) { n.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get innerHTML() { return ""; },
    set innerHTML(v) { n.kids = []; },
  };
  n.classList = {
    add: (...cs) => cs.forEach((c) => n.classes.add(c)),
    remove: (...cs) => cs.forEach((c) => n.classes.delete(c)),
    contains: (c) => n.classes.has(c),
    toggle: (c, on) => (on ? n.classes.add(c) : n.classes.delete(c)),
  };
  return n;
}
function descendants(n, out = []) {
  for (const k of n.kids) { out.push(k); descendants(k, out); }
  return out;
}
const document = { createElement: node };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const list = node("ul");
let served = { sessions: [] };
const api = async () => ({ ok: true, json: async () => served });

/* Everything refreshSessions leans on that is not this line. Same standing
   liability railctx_check names: a branch that adds a call to the row
   builder breaks every harness that slices it, with a ReferenceError that
   reads like a defect and is not one. Point RAILSEEN_APP_JS at the merged
   tree before believing a green here. */
const stubs = `
let sessionsCache = [], currentName = null, currentPage = "home";
let attachedPid = null, linkState = "down", sessName = null;
let keptTerms = new Map();
function dropKept() {}
function railHeld() { return false; }
let railRedrawPending = false;
function forgetDeadSessions() {}
function refreshResumeChoices() {}
function refreshParentChoices() {}
function renderHome() {}
function syncBulkActions() {}
function syncMobileBars() {}
/* The terminal header's mesh-handle chip, which the session poll
   repaints (sesshandle_check's subject); here it is only a call that
   has to resolve — this harness draws no header. */
function renderTermHandle() {}
function applyCflowBadges() {}
function applyGotoFlash() {}
function applyRailQuiet() {}
function applyBriefingCards() {}
function decorateBriefingRow(li, s) {}
function terminalOnScreen() { return false; }
function attach() {}
function setStatusBadge() {}
function $(id) { return list; }
`;

const ctx = {};
new Function(
  "exports", "document", "el", "api", "list", "meshCache",
  stubs + constLine("RAIL_MESH_TAGS") + constLine("CTX_DOMAIN")
  + constLine("SEEN_COLD") + constLine("TYPED_STALE")
  + slice("byLineage") + slice("sessMeshes") + slice("railMeshTags") + slice("sessHandles") + slice("handleTag")
  + slice("fmtAge") + slice("ctxShort") + slice("ctxAgeOf")
  + slice("ctxKnowable") + slice("ctxSentence") + slice("ctxBreakdown")
  + slice("ctxTooltip") + slice("ctxNoteOnRow") + slice("modelShort")
  + slice("shortenPath") + slice("cwdSplit") + slice("cwdShort")
  + slice("cwdLine") + slice("railCwdLine") + slice("ctxRailLine")
  + slice("seenAgo") + slice("seenPair") + slice("railSeenLine")
  + slice("profileHarnessLabel") + slice("railMetaText") + slice("refreshSessions")
  + `
Object.assign(exports, {
  refresh: refreshSessions,
  ago: seenAgo,
  line: railSeenLine,
  SEEN_STALE: SEEN_COLD,
  STALE: TYPED_STALE,
});`)(ctx, document, el, api, list, []);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const ago = (secs) => new Date(Date.now() - secs * 1000).toISOString();

/* ------------------------------------------------------------------ */
/* the shortening: days, where the shared fmtAge stops at hours        */
/* ------------------------------------------------------------------ */
check("a reading taken seconds ago reads as now",
      ctx.ago(ago(3)).text, "now");
check("...seconds, once it is worth counting", ctx.ago(ago(42)).text, "42s");
check("...minutes", ctx.ago(ago(185)).text, "3m");
check("...hours", ctx.ago(ago(3 * 3600 + 300)).text, "3h05m");
/* The reason this is not just fmtAge: these stamps run to days routinely —
   that is the point of them — and "51h00m" is a number the reader has to do
   arithmetic on before it means anything. */
check("...and days, which is where fmtAge would have said 51h00m",
      ctx.ago(ago(51 * 3600)).text, "2d");
check("nothing at all where there is no stamp", ctx.ago(null), null);
check("...or where the stamp is not a date", ctx.ago("soon"), null);
/* A daemon clock a little ahead of the browser's must not produce a negative
   age. Pinned on `secs` and not on the text, because the text cannot tell:
   any negative number falls through the "< 10 seconds" branch and reads as
   "now" whether it was clamped or not. `secs` is the half that is actually
   consumed — it is what decides the stale colouring — so it is the half worth
   holding. */
check("a stamp from the future is clamped rather than left negative",
      ctx.ago(new Date(Date.now() + 7_200_000).toISOString()).secs, 0);

/* ------------------------------------------------------------------ */
/* each pair reads its OWN field                                       */
/* ------------------------------------------------------------------ */
const pairs = (s) => {
  const line = ctx.line(s);
  const out = {};
  for (const p of line.kids) {
    const [k, v] = p.kids;
    out[k.textContent] = v.textContent;
  }
  return out;
};
const classOf = (s, key) => {
  for (const p of ctx.line(s).kids) {
    if (p.kids[0].textContent === key) return p.kids[1].className;
  }
  return null;
};

check("the line says all three things, and in the order it reads in",
      Object.keys(pairs({})), ["seen", "typed", "moved"]);

check("each pair takes its value from its own field and no other",
      pairs({
        last_visited_at: ago(120),
        last_input_at: ago(3600 * 5),
        last_activity_at: ago(8),
      }),
      { seen: "2m", typed: "5h00m", moved: "now" });

/* The crossed-wires failure, stated directly: a session nobody has looked at
   for a day while its agent has been working all along. If "seen" ever
   starts reading the activity stamp this flips, and nothing else would
   notice. */
check("a busy session nobody has visited still says nobody has visited",
      pairs({ last_visited_at: ago(86_400 * 2), last_activity_at: ago(2) }),
      { seen: "2d", typed: "–", moved: "now" });

/* ...and its mirror: watched all afternoon, idle since lunch. */
check("a watched session that has not moved says exactly that",
      pairs({ last_visited_at: ago(30), last_activity_at: ago(4 * 3600) }),
      { seen: "30s", typed: "–", moved: "4h00m" });

/* ------------------------------------------------------------------ */
/* absence is drawn as absence — and never as a zero or a gap          */
/* ------------------------------------------------------------------ */
check("a session nobody has been near draws dashes, not zeroes",
      pairs({}), { seen: "–", typed: "–", moved: "–" });
/* Every row draws all three pairs whether or not each has an answer: the
   value of a rail is that the same fact sits at the same place on every
   line, and a pair that vanished when unknown would shift the two beside it
   at exactly the moment the column is worth reading. */
check("...and the pairs are still all there, so the columns stay aligned",
      ctx.line({}).kids.length, 3);
check("a dash is not dressed as a number", classOf({}, "seen"),
      "rail-seen-val unknown");

/* ------------------------------------------------------------------ */
/* "now" while somebody is actually there — a state, not an age        */
/* ------------------------------------------------------------------ */
/* No stamp taken in the past can say "still here": a tab being read this
   second would otherwise show the age of the moment it was opened. */
check("an open terminal reads as now, whatever the stamp says",
      pairs({ viewers: 1, last_visited_at: ago(4000) }).seen, "now");
check("...and is coloured as the state it is, not as a duration",
      classOf({ viewers: 1, last_visited_at: ago(4000) }, "seen"),
      "rail-seen-val live");
check("nobody watching goes back to reading the stamp",
      pairs({ viewers: 0, last_visited_at: ago(4000) }).seen, "1h06m");
/* Only the visit has a "right now"; the other two are stamps by nature and
   must not borrow the viewer count. */
check("viewers do not make the session look busy",
      pairs({ viewers: 3, last_activity_at: ago(4000) }).moved, "1h06m");

/* ------------------------------------------------------------------ */
/* stale thresholds: one hour for seen/moved, half an hour for typed  */
/* ------------------------------------------------------------------ */
check("fresh readings are drawn plainly",
      [classOf({ last_visited_at: ago(60) }, "seen"),
       classOf({ last_activity_at: ago(60) }, "moved")],
      ["rail-seen-val", "rail-seen-val"]);
check("seen and moved stay plain right up to the hour",
      [classOf({ last_visited_at: ago(ctx.SEEN_STALE - 60) }, "seen"),
       classOf({ last_activity_at: ago(ctx.SEEN_STALE - 60) }, "moved")],
      ["rail-seen-val", "rail-seen-val"]);
check("...and are emphasized past the hour",
      [classOf({ last_visited_at: ago(ctx.SEEN_STALE + 60) }, "seen"),
       classOf({ last_activity_at: ago(ctx.SEEN_STALE + 60) }, "moved")],
      ["rail-seen-val stale", "rail-seen-val stale"]);

/* Half an hour since a person typed is drawn as an error state rather than
   as an age: the session somebody handed a task to and then walked away from
   is legible well before the hour at which the other two readings become
   interesting, and it is the row the whole line is scanned for.

   The threshold is checked on both sides, because a step that fires early is
   the same defect as one that never fires: a rail where most rows are red
   says nothing, and the reader stops looking at the colour. */
check("the typed reading is drawn plainly right up to the half hour",
      classOf({ last_input_at: ago(ctx.STALE - 60) }, "typed"),
      "rail-seen-val");
check("...and past it, it is an error state",
      classOf({ last_input_at: ago(ctx.STALE + 60) }, "typed"),
      "rail-seen-val stale");
check("...and remains emphasized past the hour",
      classOf({ last_input_at: ago(ctx.SEEN_STALE * 5) }, "typed"),
      "rail-seen-val stale");
/* The three readings retain their own thresholds. Seen and moved do not turn
   red at typed's earlier half-hour boundary. */
check("seen and moved retain their one-hour threshold",
      [classOf({ last_visited_at: ago(ctx.STALE + 60) }, "seen"),
       classOf({ last_activity_at: ago(ctx.STALE + 60) }, "moved")],
      ["rail-seen-val", "rail-seen-val"]);
/* Absence is not a stale timer. */
check("a missing reading is not an error, it is a dash",
      [classOf({}, "seen"), classOf({}, "typed"), classOf({}, "moved")],
      ["rail-seen-val unknown", "rail-seen-val unknown",
       "rail-seen-val unknown"]);

/* The colour has to exist. This is the failure the DOM cannot see: the class
   keeps being written onto the value and the value keeps rendering grey. */
check("the stylesheet paints the state the markup claims",
      /#session-list \.rail-seen-val\.stale\s*\{[^}]*color:/.test(css), true);
/* ...and not by borrowing the global `.error` rule, which carries a
   font-size and a margin-top that would lift this value off the baseline its
   two neighbours sit on. */
check("...with its own rule, not the global .error box",
      /"rail-seen-val" \+ \([^;]*\berror\b/.test(src), false);

/* The tooltip is where the number explains itself. A red value with a
   hover that still says only "when a person last typed here" leaves the
   reader to guess which threshold tripped. */
const titleOf = (s, key) => {
  for (const p of ctx.line(s).kids) {
    if (p.kids[0].textContent === key) return p.title;
  }
  return null;
};
check("a red value says on hover what tripped it",
      /over 30m since anyone typed here/.test(
        titleOf({ last_input_at: ago(ctx.STALE + 60) }, "typed")),
      true);
check("stale seen and moved values explain their own thresholds",
      [/over 60m since anyone looked here/.test(
         titleOf({ last_visited_at: ago(ctx.SEEN_STALE + 60) }, "seen")),
       /over 60m since the screen moved for real/.test(
         titleOf({ last_activity_at: ago(ctx.SEEN_STALE + 60) }, "moved"))],
      [true, true]);
check("...and a value that has not tripped does not carry the note",
      /since anyone typed here/.test(
        titleOf({ last_input_at: ago(60) }, "typed")),
      false);
/* The dash again, from the only side that can see it. The class expression
   answers "no reading" before it answers "past the step", so a threshold
   that counts absence as tripped still renders a dash and leaves no trace in
   the markup at all. The hover is where that flag shows through. */
check("...and neither does a reading that was never taken",
      /since anyone typed here/.test(titleOf({}, "typed")), false);

/* ------------------------------------------------------------------ */
/* and it is actually ON the row the poll builds                       */
/* ------------------------------------------------------------------ */
const WATCHED = {
  name: "watched", status: "busy", harness: "claude", profile: "nc",
  parent: null, viewers: 1, last_visited_at: ago(9),
  last_input_at: ago(600), last_activity_at: ago(3),
};
const FORGOTTEN = {
  name: "forgotten", status: "idle", harness: "claude", profile: "nc",
  parent: null, last_visited_at: ago(86_400 * 3), last_input_at: ago(86_400 * 3),
  last_activity_at: ago(7200),
};
/* A harness that keeps no transcript gets no context gauge — the line under
   test must not have been riding on that one's presence. */
const PLAIN = { name: "pi", status: "idle", harness: "pi", profile: "nc",
                parent: null };
/* An exited record: its screen reading is gone with the process, but the two
   human stamps came back with the record, and "when did I last look at the
   one that died" is most of what these are opened for. */
const GONE = { name: "gone", status: "exited", exit_code: 0, harness: "claude",
               profile: "nc", parent: null, last_visited_at: ago(1800),
               last_input_at: ago(2400), last_activity_at: null };
served = { sessions: [WATCHED, FORGOTTEN, PLAIN, GONE] };

(async () => {
  await ctx.refresh();

  const rows = list.kids;
  check("every session still gets a row",
        rows.map((r) => r.dataset.name),
        ["watched", "forgotten", "pi", "gone"]);

  const row = (name) => rows.find((r) => r.dataset.name === name);
  const lineOf = (name) =>
    descendants(row(name)).find((k) => k.classes.has("rail-seen"));
  const readOff = (name) => {
    const line = lineOf(name);
    if (!line) return null;
    const out = {};
    for (const p of line.kids) out[p.kids[0].textContent] = p.kids[1].textContent;
    return out;
  };

  /* The containment failure this file exists for: the line stops being
     appended and nothing anywhere says so. */
  check("every row carries the line — including one with no context gauge",
        ["watched", "forgotten", "pi", "gone"].map((n) => !!lineOf(n)),
        [true, true, true, true]);

  check("the watched row reads off the poll's own fields",
        readOff("watched"), { seen: "now", typed: "10m", moved: "now" });
  check("the forgotten one is the row this line exists to surface",
        readOff("forgotten"), { seen: "3d", typed: "3d", moved: "2h00m" });
  check("a session nobody has touched draws three dashes",
        readOff("pi"),
        { seen: "–", typed: "–", moved: "–" });
  check("an exited record keeps the human stamps and drops the screen one",
        readOff("gone"), { seen: "30m", typed: "40m", moved: "–" });

  /* And the colour survives the trip through the row builder, which is a
     separate question from whether seenPair computes it: the row is where
     the class actually reaches a stylesheet. `forgotten` is the shape this
     step was asked for — typed into once, then left. */
  const classOnRow = (name, key) => {
    for (const p of lineOf(name).kids) {
      if (p.kids[0].textContent === key) return p.kids[1].className;
    }
    return null;
  };
  check("the row the poll builds carries the error state on typed",
        classOnRow("forgotten", "typed"), "rail-seen-val stale");
  check("...and on stale seen and moved readings",
        [classOnRow("forgotten", "seen"), classOnRow("forgotten", "moved")],
        ["rail-seen-val stale", "rail-seen-val stale"]);
  check("...and the row typed into ten minutes ago does not",
        classOnRow("watched", "typed"), "rail-seen-val");
  /* The exited record's 40-minute stamp is past the threshold too. Nothing
     special is done for exited rows: the reading is a fact about when a
     person was last at this session's keyboard, and it does not stop being
     true because the process went away. */
  check("an exited record is coloured off the same reading as any other",
        classOnRow("gone", "typed"), "rail-seen-val stale");

  /* The stylesheet's invariant, which this line is now part of: the rail row
     wraps, and only the declared full-width children may break it. A
     line-breaker that landed before the ⓘ would push it onto a line of its
     own — the four-line row this rail has been bitten by before. So the line
     must be a direct child of the row, and it must sit after the name group,
     not inside it. */
  const kids = row("watched").kids;
  const seenIdx = kids.findIndex((k) => k.classes.has("rail-seen"));
  const headIdx = kids.findIndex((k) => k.classes.has("rail-head"));
  const infoIdx = kids.findIndex((k) => k.classes.has("sess-info"));
  check("the line is a direct child of the row, after the name group",
        seenIdx > headIdx && seenIdx >= 0, true);
  /* ⓘ stays on the row: it is ordered back onto the name line by the
     stylesheet, so its position in the DOM after a line-breaker is fine —
     what must not happen is the line-breaker disappearing into the head
     box, where `nowrap` would squeeze it out of existence instead. */
  check("...and not inside it, where nowrap would crush it",
        descendants(kids[headIdx]).some((k) => k.classes.has("rail-seen")),
        false);
  check("the ⓘ is still on the row", infoIdx >= 0, true);

  if (failures) {
    console.error(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("railseen_check ok");
})();
