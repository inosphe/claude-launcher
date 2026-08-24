/* The web terminal's typing marks, run against a stub socket.

   A keystroke the daemon receives as bytes marks its keyboard busy; a key an
   IME is still composing (Hangul, a phone keyboard mid-word) sends no bytes,
   and a delivery landing in that window types itself into the half-written
   line. The terminal's textarea therefore reports keys as `typing` control
   frames — throttled, never while the link is down, and never as bytes. The
   real functions are sliced out of the shipped app.js and driven here. */
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "..", "src", "claude_launcher", "web",
                         "static");
const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");

function slice(from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to, a + 1);
  if (a < 0 || b < 0 || b <= a) throw new Error(`cannot slice ${from} .. ${to}`);
  return src.slice(a, b);
}

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

function build() {
  const now = { at: 100000 };
  const sock = { readyState: 1, sent: [] };
  sock.send = (d) => sock.sent.push(d);
  const WebSocket = { OPEN: 1 };
  const code = slice("/* ---- typing marks ----", "/* Everything typed into the terminal");
  const api = new Function(
    "ws", "WebSocket", "Date", "lastLocalKey",
    code +
    "\nreturn {noteTyping, watchComposer, markMs: TYPING_MARK_MS," +
    " get lastKey() { return lastLocalKey; }};"
  )(sock, WebSocket, { now: () => now.at }, 0);
  return { api, now, sock };
}

const frames = (sock) => sock.sent.map((d) => JSON.parse(d).type);

/* ---- a key marks the keyboard, once per window ------------------------ */
{
  const { api, now, sock } = build();
  api.noteTyping();
  check("first key sends a typing mark", frames(sock).join() === "typing", sock.sent);
  check("and is this tab's typing for the banner", api.lastKey === now.at);
  now.at += 300;
  api.noteTyping();
  api.noteTyping();
  check("keys inside the window send nothing more", sock.sent.length === 1, sock.sent);
  check("but still count as local typing", api.lastKey === now.at);
  now.at += api.markMs;
  api.noteTyping();
  check("the next window sends the next mark", sock.sent.length === 2, sock.sent);
  check("marks are control frames, never bytes",
        sock.sent.every((d) => typeof d === "string"), sock.sent);
}

/* ---- a down link gets no mark, and no crash ---------------------------- */
{
  const { api, sock } = build();
  sock.readyState = 3;
  api.noteTyping();
  check("closed socket: nothing sent", sock.sent.length === 0, sock.sent);
  check("closed socket: still local typing", api.lastKey > 0);
}
{
  const code = slice("/* ---- typing marks ----", "/* Everything typed into the terminal");
  const api = new Function("ws", "WebSocket", "Date", "lastLocalKey",
    code + "\nreturn {noteTyping};")(null, { OPEN: 1 }, { now: () => 1 }, 0);
  let threw = false;
  try { api.noteTyping(); } catch (e) { threw = true; }
  check("no socket at all: does not throw", !threw);
}

/* ---- the textarea is where composing shows up -------------------------- */
{
  const { api } = build();
  const on = {};
  const term = { textarea: { addEventListener: (ev, fn) => { on[ev] = fn; } } };
  api.watchComposer(term);
  for (const ev of ["keydown", "compositionstart", "compositionupdate", "input"]) {
    check(`watches ${ev}`, on[ev] === api.noteTyping, Object.keys(on));
  }
  let threw = false;
  try { api.watchComposer({}); api.watchComposer(null); } catch (e) { threw = true; }
  check("a terminal without a textarea is left alone", !threw);
}

if (failures) { console.log(`${failures} failure(s)`); process.exit(1); }
console.log("typing_check: ok");
