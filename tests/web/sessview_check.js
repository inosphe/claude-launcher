/* The session's view, remembered per session (claunch-br7qh). Every link to a
   session says #/s/<name>; whether the reader lands on its terminal (CLI) or
   its conversation page (HTML, #/log/<name>) is the session's own last
   choice, kept in the per-session layout row (sessLayoutFor().view) and
   honoured by route() when a link brings the reader in from another session.
   Slice the real route() and its hash helpers out of the shipped app.js and
   walk it through the navigations a reader makes. Everything route() leans on
   that is not under test is answered by a scope proxy with a no-op. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web",
                         "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
const css = fs.readFileSync(path.join(STATIC, "style.css"), "utf8");

function sliceTo(from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to);
  if (a < 0 || b < 0 || b <= a) throw new Error(`cannot slice ${from} .. ${to}`);
  return src.slice(a, b);
}
function sliceFn(name) {
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

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

const code =
  sliceTo("const SESSLAYOUT_KEY =", "/* Bind the terminal to a session") + "\n" +
  ["hashQuery", "hashDetail", "hashWithDetail", "parseHash", "route"]
    .map(sliceFn).join("\n") + "\n" +
  "exports.route = route; exports.sessLayoutFor = sessLayoutFor;";

/* One world: the reader attached to `attached` (CLI unless `page` says
   otherwise), the localStorage row `stored`, and spies on what route() does
   to the terminal and the transcript. */
function build(o = {}) {
  const store = new Map();
  if (o.stored) store.set("claunch_sesslayout:/", JSON.stringify(o.stored));
  const calls = { attach: [], transcript: [], replaced: [] };
  const location = { hash: "" };
  const history = {
    state: null,
    replaceState(s, _t, url) {
      this.state = s;
      if (url !== undefined) { location.hash = url; calls.replaced.push(url); }
    },
  };
  const state = {
    exports: {},
    BASE: "/",
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
    },
    location, history,
    MOBILE_MQ: { matches: false },
    currentName: o.attached || null,
    term: o.attached ? {} : null,
    sessName: null,
    attach(name) { calls.attach.push(name); state.currentName = name; state.term = {}; },
    openTranscript(name, machine) { calls.transcript.push([name, machine || null]); },
  };
  const noop = () => undefined;
  const scope = new Proxy(state, {
    has: (t, k) => typeof k === "string" && !(k in globalThis) || k in t,
    get: (t, k) => {
      if (k === Symbol.unscopables) return undefined;
      if (k in t) return t[k];
      // globalThis.ObserverPage?.stop() and friends: an object whose every
      // member is a no-op; any bare function is a no-op too.
      if (k === "globalThis") return new Proxy({}, { get: () => ({ stop: noop }) });
      return noop;
    },
    set: (t, k, v) => { t[k] = v; return true; },
  });
  new Function("scope", `with (scope) { ${code} }`)(scope);
  /* Follow a link: a new history entry the router has not stamped. */
  function go(hash) { history.state = null; location.hash = hash; state.exports.route(); }
  /* Back/Forward: an entry the router has seen already. */
  function back(hash) { history.state = { routed: true }; location.hash = hash; state.exports.route(); }
  return { state, calls, location, store, go, back,
           view: (n) => state.exports.sessLayoutFor(n).view };
}

/* --- the remembered field reads whole ------------------------------------ */
{
  const w = build({ stored: { s15: { view: "banana" }, s16: { view: "html" } } });
  check("an unknown view reads as CLI", w.view("s15") === "cli");
  check("a session never seen reads as CLI", w.view("nobody") === "cli");
  check("html is kept", w.view("s16") === "html");
}

/* --- a link from another session lands on the remembered view ------------ */
{
  const w = build({ attached: "s15", stored: { s16: { view: "html" } } });
  w.go("#/s/s16");
  check("the session is attached under the HTML view",
        w.calls.attach.join() === "s16", w.calls.attach);
  check("the address becomes its conversation page, replaced",
        w.location.hash === "#/log/s16" && w.calls.replaced.includes("#/log/s16"),
        w.location.hash);
  check("...and the conversation is opened",
        JSON.stringify(w.calls.transcript) === JSON.stringify([["s16", null]]),
        w.calls.transcript);
  check("the choice is left as it was", w.view("s16") === "html");
}
{
  const w = build({ attached: "s15", stored: { s16: { view: "html" } } });
  w.go("#/s/s16?detail=s16");
  check("the rail's ?detail= rides along",
        w.location.hash === "#/log/s16?detail=s16", w.location.hash);
}
{
  const w = build({ attached: "s15" });
  w.go("#/s/s16");
  check("a session with no choice opens on its terminal",
        w.location.hash === "#/s/s16" && w.calls.transcript.length === 0,
        w.location.hash);
  check("...and nothing is written for it", !w.store.has("claunch_sesslayout:/"));
}
{
  const w = build({ attached: "s15", stored: { s16: { view: "html" } } });
  w.go("#/s/s16?detail=s16");
  check("the encoded name survives the rewrite",
        (w.calls.transcript[0] || [])[0] === "s16");
  const enc = build({ attached: "s15", stored: { "a b": { view: "html" } } });
  enc.go("#/s/a%20b");
  check("a name that needs escaping stays escaped in the address",
        enc.location.hash === "#/log/a%20b" && (enc.calls.transcript[0] || [])[0] === "a b",
        enc.location.hash);
}

/* --- the reader's own moves write the choice down ----------------------- */
{
  const w = build({ attached: "s16" });
  w.go("#/log/s16");               // the HTML radio
  check("the session's own conversation page records html", w.view("s16") === "html");
  w.go("#/s/s16");                 // the CLI radio
  check("its terminal, reached from its HTML view, records cli",
        w.view("s16") === "cli" && w.calls.transcript.length === 1,
        { view: w.view("s16"), t: w.calls.transcript });
  check("...without being turned back to the HTML view",
        w.location.hash === "#/s/s16");
}
{
  const w = build({ attached: "s15" });
  w.go("#/log/s16");
  check("another session's transcript is not that session's HTML view",
        w.view("s16") === "cli" && !w.store.has("claunch_sesslayout:/"));
}
{
  const w = build({ attached: "s15" });
  w.go("#/rlog/m2/s15");
  check("a shadow's transcript of the same name records nothing",
        w.view("s15") === "cli");
}

/* --- Back walks history; it does not re-apply the choice ---------------- */
{
  const w = build({ attached: "s15", stored: { s16: { view: "html" } } });
  w.back("#/s/s16");
  check("an entry the router has seen says what it said",
        w.location.hash === "#/s/s16" && w.calls.transcript.length === 0,
        w.location.hash);
}

{
  // Back/Forward onto an entry that was a session's HTML view, while another
  // session is attached: the page is that session's view again, header and all.
  const w = build({ attached: "s16" });
  w.go("#/log/s16");
  check("an in-session conversation entry is stamped",
        !!(w.state.history.state && w.state.history.state.sessView));
  w.go("#/s/s17");
  w.state.history.state = { routed: true, sessView: true };
  w.location.hash = "#/log/s16";
  w.state.exports.route();
  check("returning to it attaches its session",
        w.calls.attach[w.calls.attach.length - 1] === "s16" && w.state.currentName === "s16",
        w.calls.attach);
}
{
  // An unstamped entry (another session's transcript, opened as a page)
  // stays what it was: nothing is attached for it.
  const w = build({ attached: "s15" });
  w.back("#/log/s16");
  check("an unstamped transcript entry attaches nothing",
        w.calls.attach.length === 0 && w.state.currentName === "s15");
}

/* --- the leave-and-come-back the issue asks for -------------------------- */
{
  const w = build({ attached: "s16" });
  w.go("#/log/s16");               // HTML on s16
  w.go("#/s/s17");                 // look at another session
  check("the other session opens on its own (default) view",
        w.location.hash === "#/s/s17");
  w.go("#/s/s16");                 // a session card back to s16
  check("coming back lands on s16's HTML view",
        w.location.hash === "#/log/s16", w.location.hash);
}

/* --- the split pane under the HTML view: the stylesheet places it -------- */
check("the bar and pane are ordered under the conversation",
      /body\[data-page="log"\] #term-split, body\[data-page="log"\] #term-wf \{ order: 1; \}/.test(css));
check("...above the session line",
      /body\[data-page="log"\] #term-composer \{ order: 2; \}/.test(css));
check("...and the conversation takes the terminal's share",
      /body\.term-split\[data-page="log"\] #log-view \{ flex: var\(--split-term/.test(css));

if (failures) { console.log(`${failures} check(s) failed`); process.exit(1); }
console.log("all session-view checks passed");
