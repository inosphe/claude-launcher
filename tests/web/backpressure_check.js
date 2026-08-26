/* Backpressure, as the dashboard tells it: the header chip, the sentence
   behind it, the banner's reason line, and the detail panel's own box.

   What makes this worth its own check rather than an addendum to
   queued_check is the shape of the thing being reported. Every other hold
   is visible in the backlog: something is waiting, and the UI explains the
   wait. A refusal is the opposite — the message was never appended and
   never queued, so past the cap the backlog STOPS GROWING, which reads as
   calm on every field the page had before. The only trace is a counter on
   the recipient, and if the UI does not say so, the honest-looking answer
   to "why has nobody messaged this session" is an empty panel.

   So the properties held here are mostly about precedence and about drawing
   with nothing to list:
     - the shut door outranks the timing holds in the chip (a held message
       is still coming; a refused one was never taken) but never outranks
       `exited`, where nothing is being refused, nor `hold`, where the chip
       is also the button that undoes a person's own decision;
     - `paced` is named as its own short wait rather than folded into busy;
     - the panel box draws on refusals ALONE, with an empty queue, and stays
       away entirely when the door is open and nobody was turned away.

   Slice the real functions out of app.js and drive them against a stub DOM. */
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static",
            "app.js"),
  "utf8"
);

function slice(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error("missing " + name);
  const head = src.lastIndexOf("async ", start) === start - 6 ? start - 6 : start;
  const body = src.indexOf(") {", start) + 2;
  let depth = 0;
  for (let j = body; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") { depth--; if (!depth) return src.slice(head, j + 1); }
  }
  throw new Error("unbalanced " + name);
}

/* ---- stub DOM ---------------------------------------------------------- */
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), title: "",
    appendChild(c) { this.kids.push(c); return c; },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) {
      this.classes = new Set(String(v).split(/\s+/).filter(Boolean));
    },
  };
  return n;
}
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) String(cls).split(/\s+/).forEach((c) => c && n.classes.add(c));
  if (text !== undefined) n.text = String(text);
  return n;
}
/* Everything the box put on the page, flattened — the check is about what a
   reader can find in it, not about which nesting level it landed on. */
function textOf(n) {
  return [n.text, ...n.kids.map(textOf)].filter(Boolean).join(" ");
}

const ctx = { el, module: { exports: {} } };
const load = new Function(
  "el", "fmtAge_unused", "assert",
  `${slice("fmtAge")}
   let localTypingResult = false;
   function localTyping() { return localTypingResult; }
   ${slice("queuedReason")}
   ${slice("holdChipText")}
   ${slice("chipTitle")}
   ${slice("sessBackpressure")}
   return { queuedReason, holdChipText, chipTitle, sessBackpressure,
            setTyping: (v) => { localTypingResult = v; } };`
);
const app = load(el, null, assert);

/* Payload shorthand: /queued as the daemon fills it, with the backpressure
   block the API now always carries. */
function q(state, msgs, bp) {
  return {
    state,
    messages: Array.from({ length: msgs }, (_, i) => ({ id: `m${i}` })),
    backpressure: Object.assign(
      { enabled: true, queued: msgs, congested: false, inbox_max: 4,
        refused: 0, refused_from: [], paced_for: null, handles: [] },
      bp || {}
    ),
  };
}

/* ---- the chip ---------------------------------------------------------- */
{
  // Open door, nothing pending: unchanged from before backpressure existed.
  assert.strictEqual(app.holdChipText(q("settling", 0), 0), "delivery: live");

  // Shut door. It has to be unmistakable, and it has to carry the numbers —
  // "4/4" is what tells a reader this is a cap and not a coincidence.
  const shut = q("busy", 4, { congested: true, inbox_max: 4 });
  const text = app.holdChipText(shut, 4);
  assert.ok(/REFUS/i.test(text), text);
  assert.ok(text.includes("4/4"), text);
  // ...and it outranks the timing hold underneath it: a reader told only
  // "busy" would wait for the turn to end, which is not what unblocks this.
  assert.ok(!/mid-turn|busy \(/.test(text), text);

  // `exited` is blunter: with no terminal, nothing is being refused, and
  // sending the reader to look for a flood would be a wrong diagnosis.
  assert.strictEqual(
    app.holdChipText(q("exited", 4, { congested: true }), 4),
    "delivery: exited"
  );

  // `hold` keeps its own word too — this chip is the button that un-pins,
  // and a person must never be shown a sentence that hides their own doing.
  const pinned = app.holdChipText(q("hold", 4, { congested: true }), 4);
  assert.ok(pinned.includes("pinned"), pinned);
  assert.ok(pinned.includes("inbox full"), pinned);  // both facts, in order

  // Pacing is its own state: short, mechanical, and not the session's fault.
  assert.strictEqual(app.holdChipText(q("paced", 0), 0), "delivery: paced");
  assert.strictEqual(app.holdChipText(q("paced", 2), 2), "delivery: paced (2)");
}

/* ---- the sentence behind the chip -------------------------------------- */
{
  const title = app.chipTitle(
    q("busy", 4, {
      congested: true, inbox_max: 4, refused: 7,
      refused_from: [{ from: "w1", count: 5, ago: 12 },
                     { from: "w2", count: 2, ago: 40 }],
    })
  );
  // The count of senders turned away is the fact that explains a backlog
  // which has stopped growing; the chip has no room for it, so it lives here.
  assert.ok(title.includes("7 turned away"), title);
  assert.ok(title.includes("w1×5"), title);
  // And it must say what happens to those senders, or a reader will assume
  // the messages are queued somewhere and will arrive later. They are not.
  assert.ok(/nothing of theirs is queued/i.test(title), title);

  // Open door: not a word about capacity, so the sentence stays short.
  const calm = app.chipTitle(q("settling", 0));
  assert.ok(!/capacity|turned away/i.test(calm), calm);
}

/* ---- the banner's reason line ------------------------------------------ */
{
  const line = app.queuedReason(
    q("busy", 4, { congested: true, inbox_max: 4, refused: 3 }), true
  );
  assert.ok(/REFUSING/.test(line), line);
  assert.ok(line.includes("cap 4"), line);
  assert.ok(line.includes("3 turned away"), line);

  // exited still wins here as well — same reason as the chip.
  assert.ok(
    /exited/.test(app.queuedReason(q("exited", 4, { congested: true }), true))
  );

  // Pacing reads as a short wait that ends by itself, not another stuck
  // delivery: "these go in with the next one" is the whole point of it.
  const paced = app.queuedReason(q("paced", 2), true);
  assert.ok(/paced/.test(paced), paced);
  assert.ok(/next one/.test(paced), paced);
}

/* ---- the panel box ----------------------------------------------------- */
{
  // Quiet: door open, nobody refused, no pacing. Nothing is drawn — an
  // always-present "Refused (0)" would train the eye to skip the one time
  // it matters.
  assert.strictEqual(app.sessBackpressure({ queued: q("settling", 0) }), null);

  // Off for this mesh: nothing to say about a gate that is not applied.
  assert.strictEqual(
    app.sessBackpressure({ queued: q("settling", 0, { enabled: false, congested: true }) }),
    null
  );

  // The case the box exists for: NOTHING queued, and yet senders are being
  // turned away. Every other panel on the page is empty here.
  const onlyRefusals = app.sessBackpressure({
    queued: q("settling", 0, {
      refused: 4,
      refused_from: [{ from: "w9", count: 4, ago: 8 }],
    }),
  });
  assert.ok(onlyRefusals, "a refusal with an empty queue must still draw");
  const t = textOf(onlyRefusals);
  assert.ok(t.includes("4 message(s) turned away"), t);
  assert.ok(t.includes("w9"), t);
  assert.ok(t.includes("8s"), t);           // fmtAge, not raw seconds

  // Shut: the box wears the chip's colour, and says what senders are told.
  const shut = app.sessBackpressure({
    queued: q("busy", 4, {
      congested: true, inbox_max: 4, queued: 4, refused: 2,
      refused_from: [{ from: "w1", count: 2, ago: 3 }],
      handles: [{ mesh: "team", handle: "lead", queued: 4, inbox_max: 4,
                  congested: true, paced_for: null, refused: 2 }],
    }),
  });
  assert.ok(shut.classes.has("shut"), shut.className);
  const st = textOf(shut);
  assert.ok(/at capacity/i.test(st), st);
  assert.ok(/wait and re-send/.test(st), st);
  // Which room and on which cap: a session in two meshes can be shut in one
  // and open in the other, and the caps need not match.
  assert.ok(st.includes("lead@team"), st);
  assert.ok(st.includes("4/4 queued"), st);
  assert.ok(st.includes("REFUSING"), st);

  // Paced but open: a wait with a number on it, and no talk of refusal —
  // nothing is being turned away, so saying so would be a false alarm.
  const paced = app.sessBackpressure({
    queued: q("paced", 1, { paced_for: 7.2, handles: [] }),
  });
  assert.ok(paced, "a pacing wait is worth drawing");
  const pt = textOf(paced);
  assert.ok(/paced/.test(pt), pt);
  assert.ok(pt.includes("8s"), pt);          // ceil, so it never reads "0s"
  assert.ok(!/refus/i.test(pt), pt);
  assert.ok(!paced.classes.has("shut"), paced.className);
}

console.log("backpressure_check ok");
