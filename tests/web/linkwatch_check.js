/* The terminal link's own liveness, and the link log (claunch-xbdy7).

   A terminal froze -- no output, no input -- until the page was reloaded,
   while the control socket stayed healthy or had already been recycled. The
   link now pings itself and takes a silent link for dead, gives an attach
   that never answers a deadline, and moves a terminal on a socket of its own
   onto a channel once the control socket is back. Every step is written to
   a rotating log in localStorage that outlives the reload.

   The real functions are sliced out of the shipped app.js and driven here
   with the clock, the sockets and the storage in the harness's hands, the
   way reconnect_check.js drives the rest of the link. */
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

/* ---- the link, in a stub world ---------------------------------------- */
function build(opts) {
  const o = opts || {};
  const nodes = {};
  // Only the link's own chips exist; every other lookup misses, as it does
  // for the blocks of the page this harness leaves out.
  const $ = (id) => {
    if (!["term-link", "m-link", "term-size", "m-size"].includes(id)) return null;
    if (!nodes[id]) {
      nodes[id] = {
        id, textContent: "", title: "", className: "", on: {},
        addEventListener: (ev, fn) => { nodes[id].on[ev] = fn; },
        click: () => nodes[id].on.click && nodes[id].on.click(),
      };
    }
    return nodes[id];
  };
  const timers = [];
  let ticket = 0;
  const setTimeoutStub = (fn, ms) => { timers.push({ id: ++ticket, fn, ms }); return ticket; };
  const clearTimeoutStub = (id) => {
    const i = timers.findIndex((t) => t.id === id);
    if (i >= 0) timers.splice(i, 1);
  };
  const settle = async () => { for (let i = 0; i < 20; i += 1) await Promise.resolve(); };

  const sockets = [];
  class FakeSocket {
    constructor(u) {
      this.url = u;
      this.readyState = 0;
      this.sent = [];
      this.closed = false;
      sockets.push(this);
    }
    send(data) { this.sent.push(data); }
    close() { this.readyState = 3; this.closed = true; }
    opened() { this.readyState = 1; if (this.onopen) this.onopen(); }
    text(msg) { if (this.onmessage) this.onmessage({ data: JSON.stringify(msg) }); }
  }
  FakeSocket.OPEN = 1;

  // A channel is the same shape; what tells them apart is who made it.
  const control = { up: !!o.controlUp };
  const channels = [];
  const openChannel = (name) => {
    if (!control.up) return null;
    const link = new FakeSocket(`channel:${name}`);
    link.ch = channels.length + 1;
    channels.push(link);
    return link;
  };

  const now = { at: 100000 };
  const term = { cols: 80, rows: 24, write: () => {}, resize: () => {}, dispose: () => {} };
  const logged = [];
  const warned = [];
  const code = "let keptTerms = new Map();\nlet currentName = null;\n"
    + "let snapshotName = null;\nfunction removeSnapshot() {}\n"
    + "function controlUp() { return control.up; }\n"
    + "function controlSay() { return false; }\n"
    + "function linkLog(kind, detail) { logged.push({ kind, ...(detail || {}) }); }\n"
    + slice("/* ---- the link ----", "/* ---- text size ----");
  const api = new Function(
    "$", "url", "api", "fetch", "WebSocket", "window", "document", "location",
    "ws", "term", "fitAddon", "attachedPid", "applyingRemoteResize",
    "setStatusBadge", "refitSoon", "setTimeout", "clearTimeout", "Math", "Date",
    "updateScrollChip", "wheelTimer", "wheelAccum", "altScreen", "scrollOffset",
    "fitView", "resyncTerminal", "terminalOnScreen", "console",
    "control", "openChannel", "logged",
    "let sizeOwner = true, sizeHeld = false;\n" +
    "function renderSizeChip() {}\nfunction stealSize() {}\n" +
    code +
    "\nreturn {openSocket, closeLink, linkWatchTick, linkPreferChannel, sendInput," +
    " get state() { return linkState; }," +
    " get sock() { return ws; }," +
    " get queued() { return linkQueue.slice(); }," +
    " OPEN_MS: LINK_OPEN_DEADLINE_MS, PING_MS: LINK_PING_MS," +
    " PONG_MS: LINK_PONG_DEADLINE_MS, PROBE_MS: LINK_PROBE_MS, GAP_MS: LINK_WAKE_GAP_MS};"
  )(
    $, (p) => `/${String(p).replace(/^\//, "")}`,
    async () => ({ ok: true, json: async () => ({}) }),
    async () => ({ ok: true, json: async () => ({ status: "ok" }) }),
    FakeSocket,
    { addEventListener: () => {} },   // no setInterval: the ticks are run by hand
    { hidden: false, hasFocus: () => false },
    { protocol: "http:", host: "d09:8377" },
    null, term, null, null, false,
    () => {}, () => {}, setTimeoutStub, clearTimeoutStub,
    { random: () => 0, round: Math.round, max: Math.max, min: Math.min },
    { now: () => now.at },
    () => {}, null, 0, false, 0, () => {}, () => {}, () => false,
    { warn: (m, d) => warned.push({ m, ...(d || {}) }), info: () => {} },
    control, openChannel, logged,
  );
  const live = async () => {
    api.openSocket("s7");
    const s = sockets[sockets.length - 1];
    s.opened();
    s.text({ type: "init", cols: 80, rows: 24, status: "idle", pid: 4242, boot_id: "b1" });
    await settle();
    return s;
  };
  // Advance the clock a second at a time, ticking as the page would.
  const run = (ms) => {
    for (let t = 0; t < ms; t += 1000) {
      now.at += 1000;
      api.linkWatchTick();
    }
  };
  const pings = (s) => s.sent.filter((m) => typeof m === "string" && JSON.parse(m).type === "ping").length;
  return { api, sockets, channels, control, now, timers, logged, warned, live, run, pings, settle };
}

async function main() {
  /* 1. A live link pings itself, and an answered ping keeps it live. */
  {
    const w = build();
    const s = await w.live();
    w.run(w.api.PING_MS);
    check("a live link sends a ping after LINK_PING_MS", w.pings(s) === 1, s.sent);
    s.text({ type: "pong" });
    // A daemon that answers every ping a second later, for a minute.
    for (let t = 0; t < 60000; t += 1000) {
      const sent = w.pings(s);
      w.run(1000);
      if (w.pings(s) > sent) s.text({ type: "pong" });
    }
    check("an answering link stays live", w.api.state === "live" && w.api.sock === s, w.api.state);
    check("it kept pinging", w.pings(s) >= 6, w.pings(s));
    check("the pong is not taken for a frame of the program", !s.closed);
  }

  /* 2. A link that answered once and then goes silent is taken for dead and
        goes down the ordinary way: a retry is scheduled, keystrokes held. */
  {
    const w = build();
    const s = await w.live();
    w.run(w.api.PING_MS);
    s.text({ type: "pong" });
    w.run(w.api.PING_MS);       // the next ping goes out, and nothing comes back
    check("still live while the deadline runs", w.api.state === "live");
    w.run(w.api.PONG_MS);
    check("a silent link is taken for dead", w.api.state === "reconnecting", w.api.state);
    check("its socket is closed from this side", s.closed);
    check("its handlers are off", s.onclose === null && s.onmessage === null);
    check("a retry is scheduled", w.timers.length === 1, w.timers.length);
    check("the death is logged with its reason",
          w.logged.some((e) => e.kind === "term.dead" && /ping unanswered/.test(e.why)), w.logged);
    check("and so is the close it caused",
          w.logged.some((e) => e.kind === "term.closed" && e.code === 4000), w.logged);
    w.api.sendInput("ls\r");
    check("keystrokes typed meanwhile are held for the retry",
          w.api.queued.join("") === "ls\r", w.api.queued);
  }

  /* 3. A link that never echoed is not judged by the ping deadline (a daemon
        that does not answer pings must not be recycled forever). */
  {
    const w = build();
    const s = await w.live();
    w.run(w.api.PING_MS + w.api.PONG_MS * 3);
    check("a link with no echo yet is left alone", w.api.state === "live" && !s.closed, w.api.state);
  }

  /* 4. Waking: a gap in the clock sends a ping at once, and gives it
        LINK_PROBE_MS rather than the full deadline. */
  {
    const w = build();
    const s = await w.live();
    w.run(w.api.PING_MS);
    s.text({ type: "pong" });
    w.run(2000);
    const before = w.pings(s);
    w.now.at += w.api.GAP_MS + 60000;   // the machine slept
    w.api.linkWatchTick();
    check("a wake sends a ping at once", w.pings(s) === before + 1, [before, w.pings(s)]);
    w.run(w.api.PROBE_MS);
    check("an unanswered wake probe takes the link for dead within LINK_PROBE_MS",
          w.api.state === "reconnecting", w.api.state);
    check("the reason says it was a wake",
          w.logged.some((e) => e.kind === "term.dead" && /waking/.test(e.why)), w.logged);
  }

  /* 5. An attach that never answers is not left in `opening` forever. */
  {
    const w = build();
    w.api.openSocket("s7");
    const s = w.sockets[0];
    w.run(w.api.OPEN_MS - 1000);
    check("still opening before the deadline", w.api.state === "opening", w.api.state);
    w.run(1000);
    check("an attach unanswered past the deadline goes to a retry",
          w.api.state === "reconnecting" && s.closed, w.api.state);
  }

  /* 6. ...nor a link whose handshake answered but whose init never came. */
  {
    const w = build();
    w.api.openSocket("s7");
    const s = w.sockets[0];
    s.opened();
    w.run(w.api.OPEN_MS);
    check("open without an init past the deadline goes to a retry",
          w.api.state === "reconnecting" && s.closed, w.api.state);
  }

  /* 7. The socket of its own moves to a channel once the control socket is
        up; a channel link stays where it is. */
  {
    const w = build({ controlUp: false });
    const own = await w.live();
    check("with the control socket down the terminal takes its own socket",
          !own.url.startsWith("channel:"), own.url);
    w.control.up = true;
    w.api.linkPreferChannel();
    check("the own socket is closed", own.closed && own.onclose === null);
    check("a channel is opened for the same session",
          w.channels.length === 1 && w.channels[0].url === "channel:s7", w.channels.map((c) => c.url));
    check("the link waits for the channel", w.api.state === "opening" && w.api.sock === w.channels[0]);
    check("the move is logged", w.logged.some((e) => e.kind === "term.to_channel"), w.logged);
    const ch = w.channels[0];
    ch.opened();
    ch.text({ type: "init", cols: 80, rows: 24, status: "idle", pid: 4242, boot_id: "b1" });
    w.api.linkPreferChannel();
    check("a link on a channel is not moved again", w.channels.length === 1 && !ch.closed);
  }

  /* 8. ...and a link that is not live is not moved (a retry owns it). */
  {
    const w = build({ controlUp: false });
    w.api.openSocket("s7");
    w.control.up = true;
    w.api.linkPreferChannel();
    check("an opening own socket is not moved", w.channels.length === 0);
  }

  /* 9. closeLink forgets the evidence: a new link earns its own echo. */
  {
    const w = build();
    const s = await w.live();
    w.run(w.api.PING_MS);
    s.text({ type: "pong" });
    w.api.closeLink();
    const s2 = await w.live();
    w.run(w.api.PING_MS + w.api.PONG_MS * 2);
    check("a fresh link with no echo of its own is not taken for dead",
          w.api.state === "live" && !s2.closed, w.api.state);
  }
}

/* ---- the link log ------------------------------------------------------ */
function buildLog(store) {
  const timers = [];
  const storage = {
    data: store || {},
    quota: Infinity,
    getItem(k) { return Object.prototype.hasOwnProperty.call(this.data, k) ? this.data[k] : null; },
    setItem(k, v) {
      if (String(v).length > this.quota) throw new Error("QuotaExceededError");
      this.data[k] = String(v);
    },
    removeItem(k) { delete this.data[k]; },
  };
  const now = { at: Date.UTC(2026, 9, 22, 5, 0, 0) };
  const code = slice("/* ---- the link log (claunch-xbdy7) ----", "/* ---- the connection the page keeps ----");
  const api = new Function(
    "BASE", "localStorage", "setTimeout", "clearTimeout", "Date", "window", "document", "navigator",
    code + "\nreturn {linkLog, linkLogFlush, linkLogEntries, linkLogLine," +
    " MAX: LINK_LOG_MAX, CHARS: LINK_LOG_CHARS, KEY: LINK_LOG_KEY," +
    " get pending() { return linkLogPending.length; }};"
  )(
    "/t/d09/", storage,
    (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    () => {},
    class extends Date { static now() { return now.at; } },
    undefined, undefined, undefined,
  );
  return { api, storage, timers, now };
}

function logChecks() {
  /* 10. Writes are batched and land in localStorage under a BASE-scoped key. */
  {
    const w = buildLog();
    w.api.linkLog("term.closed", { session: "s7", code: 1006, reason: "", wasClean: false });
    check("the key is scoped by BASE", w.api.KEY === "claunch.linklog:/t/d09/", w.api.KEY);
    check("nothing is written before the flush", w.storage.getItem(w.api.KEY) === null);
    check("one flush is scheduled", w.timers.length === 1, w.timers.length);
    w.api.linkLog("term.open", { session: "s7" });
    check("a second entry does not schedule another flush", w.timers.length === 1);
    w.timers[0].fn();
    const stored = JSON.parse(w.storage.getItem(w.api.KEY));
    check("both entries are stored", stored.length === 2 && stored[0].k === "term.closed", stored);
    check("empty values are left out", !("reason" in stored[0]) && stored[0].wasClean === false, stored[0]);
    const line = w.api.linkLogLine(stored[0]);
    check("a line reads as time, page, kind, fields",
          /^2026-10-22T05:00:00\.000Z p=\w+ term\.closed session="s7" code=1006 wasClean=false$/.test(line), line);
  }

  /* 11. It rotates: the newest LINK_LOG_MAX entries survive, oldest first out. */
  {
    const w = buildLog();
    for (let i = 0; i < w.api.MAX + 50; i += 1) {
      w.api.linkLog("tick", { i });
      if (i % 500 === 0) w.api.linkLogFlush();
    }
    w.api.linkLogFlush();
    const stored = JSON.parse(w.storage.getItem(w.api.KEY));
    check("the stored log holds at most LINK_LOG_MAX entries", stored.length === w.api.MAX, stored.length);
    check("the oldest went first", stored[0].i === 50 && stored[stored.length - 1].i === w.api.MAX + 49,
          [stored[0].i, stored[stored.length - 1].i]);
  }

  /* 12. ...and by size: a log of large entries stays under LINK_LOG_CHARS. */
  {
    const w = buildLog();
    const big = "x".repeat(2000);
    for (let i = 0; i < 600; i += 1) w.api.linkLog("big", { i, big });
    w.api.linkLogFlush();
    const text = w.storage.getItem(w.api.KEY);
    check("the stored text is under LINK_LOG_CHARS", text.length <= w.api.CHARS, text.length);
    const stored = JSON.parse(text);
    check("the newest entry survived the trim", stored[stored.length - 1].i === 599);
  }

  /* 13. Two tabs append; they do not overwrite each other. */
  {
    const shared = {};
    const a = buildLog(shared);
    const b = buildLog(shared);
    a.api.linkLog("from.a");
    b.api.linkLog("from.b");
    a.api.linkLogFlush();
    b.api.linkLogFlush();
    const stored = JSON.parse(shared[a.api.KEY]);
    check("both tabs' entries are kept", stored.map((e) => e.k).join() === "from.a,from.b", stored);
    check("and each says which page load wrote it", stored[0].p !== stored[1].p || stored[0].p.length > 0);
  }

  /* 14. Over the quota, the newer half is kept rather than nothing. */
  {
    const w = buildLog();
    for (let i = 0; i < 100; i += 1) w.api.linkLog("q", { i });
    w.storage.quota = 3000;
    w.api.linkLogFlush();
    const text = w.storage.getItem(w.api.KEY);
    check("a write over the quota still stores something", text !== null);
    const stored = text ? JSON.parse(text) : [];
    check("what it stores is the newest", stored.length && stored[stored.length - 1].i === 99, stored.length);
  }

  /* 15. A corrupt stored value is replaced, not thrown at. */
  {
    const w = buildLog();
    w.storage.data[w.api.KEY] = "{not json";
    w.api.linkLog("after.corrupt");
    w.api.linkLogFlush();
    const stored = JSON.parse(w.storage.getItem(w.api.KEY));
    check("a corrupt log is started over", stored.length === 1 && stored[0].k === "after.corrupt", stored);
  }

  /* 16. Storage that throws on read and write does not take the page down. */
  {
    const w = buildLog();
    w.storage.getItem = () => { throw new Error("SecurityError"); };
    w.storage.setItem = () => { throw new Error("SecurityError"); };
    let threw = false;
    try {
      w.api.linkLog("blocked");
      w.api.linkLogFlush();
      w.api.linkLogEntries();
    } catch { threw = true; }
    check("blocked storage is survived", !threw);
  }
}

/* ---- the hooks in the shipped page ------------------------------------- */
function wiringChecks() {
  check("the page runs the link's clock once a second",
        /window\.setInterval\(linkWatchTick, 1000\)/.test(src));
  check("handleFrame hands a pong to the link",
        /if \(msg\.type === "pong"\) \{\s*if \(typeof linkPong === "function"\) linkPong\(\);/.test(src));
  check("the control socket's open moves a terminal onto a channel",
        /linkLog\("control\.opened"\)[\s\S]{0,400}linkPreferChannel\(\)/.test(src));
  check("the control socket's close is logged with its code",
        /function controlClosed\(sock, ev\)[\s\S]{0,300}linkLog\("control\.closed"/.test(src));
  check("the console can read the log",
        /window\.claunchLinkLog = \{/.test(src));
}

main().then(() => {
  logChecks();
  wiringChecks();
  if (failures) {
    console.log(`${failures} linkwatch check(s) failed`);
    process.exit(1);
  }
  console.log("linkwatch_check ok");
}).catch((err) => {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
