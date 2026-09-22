/* The session detail panel's Flags box: the three standing marks a reader
   sets ON a session, and the one place all three can be set from while
   reading it.

   Before this box existed each of the three was somewhere else. `keep-alive`
   could only be set from the CLI (`claunch keep-alive <name>`); the panel
   drew it as a fact and offered no lever. `observe` and `pin` were on a rail
   row and on a session tab, neither of which is on screen while this panel
   IS the page (the phone) or while the rail is scrolled somewhere else.

   What has to hold, and what each group below is for:

     three, in order -- keep-alive, observe, pin. keep-alive first because it
                        is the one that decides whether the session is still
                        there to read; the pin last because it is the only
                        one of the three no other reader can see.
     read, not pressed -- every button's lit state, its `aria-pressed` and
                        the word beside its label come from the record. A
                        control that drew its own pressed state would read as
                        applied after a write the daemon refused.
     direction        -- a press asks for the OPPOSITE of the record, again
                        not of the button. keep-alive off -> POST with no
                        query, on -> POST with `?off=1`.
     two kinds of state -- keep-alive and observe are the daemon's, held on
                        the session definition and seen by every reader; the
                        pin is this browser's localStorage list and nobody
                        else has it. The hovers say which is which, because
                        the three look alike.
     exited           -- keep-alive governs what happens when a running
                        session's one-shot run finishes, so on a session that
                        has already exited the value is drawn and the lever
                        is withheld. The other two stay live: a reader still
                        pins and still points the observer at an exited row.
     one source       -- the observe button's sentence comes from
                        `observePinTitle`, the same function the rail row and
                        the session tab call, so one flag cannot end up
                        worded in three ways.

   Where the box sits in the panel is checked on the source, for the reason
   sesstask_check gives: renderSession has one harness (railmodel_check) and
   it stubs every section out, this one included, so source order is the only
   part of that wiring a second harness can honestly pin. */
const fs = require("fs");
const path = require("path");
const root = path.join(__dirname, "..", "..", "src", "claude_launcher", "web",
                       "static");
const src = fs.readFileSync(path.join(root, "app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "style.css"), "utf8");

function slice(name) {
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
function node(tag) {
  const n = {
    tag, kids: [], text: "", classes: new Set(), title: "", attrs: {},
    disabled: false, type: "", listeners: {},
    appendChild(c) { this.kids.push(c); return c; },
    addEventListener(t, fn) { this.listeners[t] = fn; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    click() { return this.listeners.click ? this.listeners.click() : undefined; },
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get className() { return [...this.classes].join(" "); },
    set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    all() {
      const out = [this];
      for (const k of this.kids) out.push(...k.all());
      return out;
    },
    find(cls) { return this.all().filter((k) => k.classes.has(cls)); },
    words() { return this.all().map((k) => k.text).join(" "); },
  };
  return n;
}
const document = { createElement: (t) => node(t) };
function el(tag, cls, text) {
  const n = node(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

/* ---- stub daemon and browser ------------------------------------------- */
const posts = [];            // every keep-alive write, in order
let reply = { ok: true, body: { keep_alive: true } };
let unreachable = false;     // the daemon cannot be reached at all
async function api(url, opts) {
  posts.push({ url, method: (opts || {}).method });
  if (unreachable) throw new Error("network down");
  return {
    ok: reply.ok,
    status: reply.ok ? 200 : 409,
    json: async () => reply.body,
  };
}
const modals = [];
async function modalInfo(title, body) { modals.push({ title, body }); }
let redraws = 0;
function refreshSession() { redraws++; }

const observeWrites = [];
async function setObservePin(name, on) { observeWrites.push([name, on]); }
function observePinTitle(name, on) {
  return `${on ? "OFF" : "ON"} ${name} — observePinTitle`;
}

let pins = new Set();
function isSessionPinned(name) { return pins.has(name); }
const pinPresses = [];
function toggleSessionPin(name) {
  pinPresses.push(name);
  if (pins.has(name)) pins.delete(name); else pins.add(name);
}

const sessionsCache = [];

const ctx = {};
new Function(
  "exports", "document", "el", "api", "modalInfo", "refreshSession",
  "setObservePin", "observePinTitle", "isSessionPinned", "toggleSessionPin",
  "sessionsCache",
  slice("setKeepAlive") + slice("sessFlagButton") + slice("sessFlags") + `
Object.assign(exports, {
  flags: sessFlags, button: sessFlagButton, keepAlive: setKeepAlive,
});`)(ctx, document, el, api, modalInfo, refreshSession, setObservePin,
      observePinTitle, isSessionPinned, toggleSessionPin, sessionsCache);

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}

const buttons = (box) => box.find("sess-flag");
const named = (box, cls) => box.find(cls)[0];
const state = (b) => (b.find("sess-flag-state")[0] || {}).text;
const flush = () => new Promise((r) => setImmediate(r));

(async () => {

/* ---- the box, and the three in it -------------------------------------- */
let box = ctx.flags({ name: "s1", status: "running" });
check("the section is drawn", box.classes.has("sess-flags"), true);
check("under a heading that says what it is",
      box.kids[0].tag === "h3" && box.kids[0].text, "Flags");
/* The three look alike and are not alike: two are the daemon's and one is
   this browser's. A reader who does not know that will expect a colleague to
   see their pin. */
check("...whose hover separates the daemon's flags from the browser's",
      /daemon/.test(box.kids[0].title) && /browser/.test(box.kids[0].title),
      true);
check("three controls, in the panel's order",
      buttons(box).map((b) => b.classes.has("sess-flag-keep") ? "keep"
        : b.classes.has("sess-flag-observe") ? "observe"
        : b.classes.has("sess-flag-pin") ? "pin" : "?"),
      ["keep", "observe", "pin"]);
check("all three are buttons, not links",
      buttons(box).map((b) => [b.tag, b.type]),
      [["button", "button"], ["button", "button"], ["button", "button"]]);

/* ---- a session with nothing set ---------------------------------------- */
check("nothing is lit", buttons(box).map((b) => b.classes.has("on")),
      [false, false, false]);
check("...and a screen reader is told the same",
      buttons(box).map((b) => b.attrs["aria-pressed"]),
      ["false", "false", "false"]);
/* Colour alone answers "which of these is on" only once there is a lit one
   to compare against, and this session has none. */
check("...and each says so in a word", buttons(box).map(state),
      ["off", "off", "off"]);
check("the labels name the flags",
      buttons(box).map((b) => b.text),
      ["keep-alive", "👁 observe", "📌 pin"]);

/* ---- each flag lights from its own record ------------------------------ */
box = ctx.flags({ name: "s1", status: "running", keep_alive: true });
check("keep_alive lights the keep-alive control only",
      buttons(box).map((b) => b.classes.has("on")), [true, false, false]);
check("...and its word turns over", state(named(box, "sess-flag-keep")), "on");
check("...and so does aria-pressed",
      named(box, "sess-flag-keep").attrs["aria-pressed"], "true");

box = ctx.flags({ name: "s1", status: "running", observe_pin: true });
check("observe_pin lights the observe control only",
      buttons(box).map((b) => b.classes.has("on")), [false, true, false]);

pins.add("s1");
box = ctx.flags({ name: "s1", status: "running" });
check("the browser's own pin list lights the pin control only",
      buttons(box).map((b) => b.classes.has("on")), [false, false, true]);
pins.delete("s1");

/* The daemon's record is the only source for the two it holds: a session the
   list poll has not reached yet must not read as pinned-for-observation
   because this browser happens to pin its tab. */
pins.add("s1");
box = ctx.flags({ name: "s1", status: "running" });
check("the browser's pin does not leak into the daemon's flags",
      [named(box, "sess-flag-keep").classes.has("on"),
       named(box, "sess-flag-observe").classes.has("on")], [false, false]);
pins.delete("s1");

/* ---- what the hovers have to say --------------------------------------- */
box = ctx.flags({ name: "s1", status: "running" });
check("keep-alive's hover names the command that is the same lever",
      named(box, "sess-flag-keep").title.includes("claunch keep-alive s1"),
      true);
check("...and says the flag outlives a restart",
      /survives a restart/.test(named(box, "sess-flag-keep").title), true);
check("...and, while off, says what setting it would do",
      /leaves the session running/.test(named(box, "sess-flag-keep").title),
      true);
check("while on, it says what lifting it would do",
      /would then end this session/.test(
        named(ctx.flags({ name: "s1", status: "running", keep_alive: true }),
              "sess-flag-keep").title), true);
/* One flag, one sentence: the rail row and the session tab call the same
   function, so a reader is not taught two vocabularies for one mark. */
check("observe's hover is the rail's own sentence, not a second wording",
      named(box, "sess-flag-observe").title, observePinTitle("s1", false));
check("...and it turns over with the record",
      named(ctx.flags({ name: "s1", status: "running", observe_pin: true }),
            "sess-flag-observe").title, observePinTitle("s1", true));
check("the pin's hover says it is this browser's alone",
      /this browser alone/.test(named(box, "sess-flag-pin").title), true);

/* ---- pressing: the direction comes from the record --------------------- */
posts.length = 0;
box = ctx.flags({ name: "s1", status: "running" });
reply = { ok: true, body: { keep_alive: true } };
box.find("sess-flag-keep")[0].click();
await flush();
check("an off keep-alive posts the plain route",
      posts, [{ url: "/api/sessions/s1/keep-alive", method: "POST" }]);

posts.length = 0;
box = ctx.flags({ name: "s1", status: "running", keep_alive: true });
reply = { ok: true, body: { keep_alive: false } };
box.find("sess-flag-keep")[0].click();
await flush();
check("an on keep-alive posts the clearing route",
      posts, [{ url: "/api/sessions/s1/keep-alive?off=1", method: "POST" }]);

observeWrites.length = 0;
box = ctx.flags({ name: "s1", status: "running", observe_pin: true });
box.find("sess-flag-observe")[0].click();
await flush();
check("observe is written with the opposite of the record",
      observeWrites, [["s1", false]]);

pinPresses.length = 0;
const before = redraws;
box = ctx.flags({ name: "s1", status: "running" });
box.find("sess-flag-pin")[0].click();
check("the pin goes through the same toggle the rail row uses",
      pinPresses, ["s1"]);
/* The pin is not on the record this panel polls, so nothing would repaint
   this box on its own. */
check("...and the panel is redrawn rather than left on the old state",
      redraws - before, 1);
pins.delete("s1");

/* ---- a write the daemon refuses ---------------------------------------- */
/* The button must end up reading what the daemon holds, which is why the
   panel is redrawn from the reply instead of from the press. */
modals.length = 0;
sessionsCache.length = 0;
sessionsCache.push({ name: "s1", keep_alive: false });
reply = { ok: false, body: { error: "session is gone" } };
const beforeRefusal = redraws;
box = ctx.flags({ name: "s1", status: "running" });
box.find("sess-flag-keep")[0].click();
await flush();
check("a refusal is reported to the reader", modals.length, 1);
check("...naming the daemon's reason", modals[0].body, "session is gone");
check("...the cached record is left as the daemon has it",
      sessionsCache[0].keep_alive, false);
check("...and the panel still repaints, so the button reverts",
      redraws - beforeRefusal, 1);

/* An accepted write corrects the list cache too: the rail draws this same
   field off its own poll, and a row disagreeing with the panel beside it
   reads as one of the two being wrong. */
sessionsCache.length = 0;
sessionsCache.push({ name: "other" }, { name: "s1", keep_alive: false });
reply = { ok: true, body: { keep_alive: true } };
box = ctx.flags({ name: "s1", status: "running" });
box.find("sess-flag-keep")[0].click();
await flush();
check("an accepted write corrects the rail's cached row",
      sessionsCache.map((r) => [r.name, !!r.keep_alive]),
      [["other", false], ["s1", true]]);

/* A request that never lands is the same promise as a refusal: it is said
   out loud and the panel is repainted, never left showing the press. */
modals.length = 0;
const beforeThrow = redraws;
unreachable = true;
await ctx.keepAlive("s1", true);
unreachable = false;
check("a request that never lands is reported too", modals.length, 1);
check("...carrying the failure's own words", modals[0].body, "network down");
check("...and the panel repaints anyway", redraws - beforeThrow, 1);

/* ---- an exited session ------------------------------------------------- */
const gone = ctx.flags({ name: "s1", status: "exited", keep_alive: true });
check("keep-alive is not offered on a session that has already exited",
      named(gone, "sess-flag-keep").disabled, true);
check("...but its value is still drawn",
      [named(gone, "sess-flag-keep").classes.has("on"),
       state(named(gone, "sess-flag-keep"))], [true, "on"]);
check("...and the hover says why the lever is missing",
      /already exited/.test(named(gone, "sess-flag-keep").title), true);
posts.length = 0;
named(gone, "sess-flag-keep").click();
await flush();
check("...and pressing it writes nothing", posts, []);
/* The other two are still worth setting on an exited session: its row is
   still on the rail and the observer can still be pointed at it. */
check("the other two stay live",
      [named(gone, "sess-flag-observe").disabled,
       named(gone, "sess-flag-pin").disabled], [false, false]);

/* ---- where the box sits, and what draws it ----------------------------- */
const body = slice("renderSession");
check("renderSession draws it", body.includes("sessFlags(s)"), true);
check("under the facts it changes",
      body.indexOf("appendChild(dl)") < body.indexOf("sessFlags(s)"), true);
check("and above the reader's own note",
      body.indexOf("sessFlags(s)") < body.indexOf("sessNote(s)"), true);
/* The fact moved rather than being copied: the facts list no longer draws a
   keep-alive row (railmodel_check holds that end), so a reader cannot change
   it here and read a stale copy four lines up. */
check("the facts list no longer carries the same fact",
      /metaRow\(\s*\n?\s*dl, "keep-alive"/.test(body), false);

/* ---- the stylesheet carries the controls ------------------------------- */
check("the buttons have a rule", css.includes(".sess-flag {"), true);
check("...a lit state, in the amber the rail's pin already wears",
      /\.sess-flag\.on \{[^}]*#d29922/.test(css), true);
check("...and a disabled one that still shows its value",
      /\.sess-flag:disabled \{[^}]*opacity/.test(css), true);
/* Three controls do not fit one line of a docked rail, and a clipped label
   on a control that changes the session is worse than a second row. */
check("the row wraps rather than shrinking the labels",
      /\.sess-flag-row \{[^}]*flex-wrap: wrap/.test(css), true);
check("the box is one of the panel's cards",
      /\.sess-flags \{/.test(css), true);

if (failures) process.exit(1);
console.log("sessflags_check: ok");
})().catch((e) => { console.error(e); process.exit(1); });
