/* The Settings card for daemon.pty_host (claunch-xnnct.5), run against stub
   nodes.

   GET /api/pty-host-setting answers the saved setting, whether the platform
   has a PTY host, and host_code.info() as this daemon runs it; PUT
   {enabled} saves the setting and answers the same shape. What is checked:
   the card reads "reading…" before the first answer; the checkbox follows
   the saved value and is disabled on a platform without a PTY host; the
   "Now:" line follows host.state; a saved value that differs from what the
   daemon started with (host.configured) adds the restart line for that
   direction, and none when they agree; a change sends PUT {enabled} and
   redraws from the answer; a failed PUT shows the error and keeps the
   saved value. */
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8");

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

function slice(from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to, a + 1);
  if (a < 0 || b < 0 || b <= a) throw new Error(`cannot slice ${from} .. ${to}`);
  return src.slice(a, b);
}

function node(tag, cls, text) {
  return {
    tag, cls: cls || "", text: text === undefined ? "" : String(text),
    children: [], on: {}, checked: false, disabled: false, id: "", type: "",
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { for (const c of cs) this.children.push(c); },
    addEventListener(ev, fn) { (this.on[ev] = this.on[ev] || []).push(fn); },
  };
}

function texts(card) {
  return card.children.filter((c) => c.tag === "p").map((c) => c.text);
}

function checkbox(card) {
  const label = card.children.find((c) => c.tag === "label");
  return label && label.children.find((c) => c.tag === "input");
}

const HOST_OFF = { configured: false, state: "off", pid: null, sessions: 0 };

function build(answers) {
  const calls = [];
  let renders = 0;
  const api = async (url, opts) => {
    calls.push({ url, method: (opts && opts.method) || "GET",
                 body: opts && opts.body ? JSON.parse(opts.body) : null });
    const next = answers.shift();
    return { ok: next.status === 200, status: next.status, json: async () => next.body };
  };
  const code = slice("/* Keep sessions across a daemon restart (claunch-xnnct.5)",
                     "function promptPresetCard(");
  const w = new Function(
    "el", "document", "api", "renderWorkspaces", "wsOpen",
    code + "\nreturn { ptyHostSetting, ptyHostSettingCard, refreshPtyHostSetting, savePtyHostSetting };")(
    node, { createElement: (tag) => node(tag) }, api, () => { renders += 1; }, true);
  return { w, calls, renders: () => renders };
}

(async () => {
  {
    const { w } = build([]);
    const card = w.ptyHostSettingCard();
    check("before the first answer the card is reading", texts(card).includes("reading…"), texts(card));
    check("no checkbox before the first answer", !checkbox(card));
  }

  {
    const { w, calls } = build([{ status: 200, body: { enabled: false, supported: true, host: HOST_OFF } }]);
    await w.refreshPtyHostSetting();
    check("refresh is a GET of the setting", calls[0].url === "/api/pty-host-setting" && calls[0].method === "GET", calls);
    const card = w.ptyHostSettingCard();
    const box = checkbox(card);
    check("off: the box is unchecked and enabled", box && !box.checked && !box.disabled, box);
    check("off: the Now line says off",
          texts(card).some((t) => t.startsWith("Now: off.")), texts(card));
    check("off and started off: no restart line",
          !texts(card).some((t) => t.startsWith("Saved.")), texts(card));
  }

  {
    const on = { configured: false, state: "off", pid: null, sessions: 0 };
    const { w, calls } = build([
      { status: 200, body: { enabled: false, supported: true, host: HOST_OFF } },
      { status: 200, body: { enabled: true, supported: true, host: on } },
    ]);
    await w.refreshPtyHostSetting();
    const box = checkbox(w.ptyHostSettingCard());
    box.checked = true;
    box.on.change.forEach((fn) => fn());
    await new Promise((r) => setTimeout(r, 0));
    check("a change sends PUT {enabled: true}",
          calls[1] && calls[1].method === "PUT" && calls[1].body.enabled === true, calls);
    const card = w.ptyHostSettingCard();
    check("after the PUT the box shows the saved value", checkbox(card).checked === true);
    check("saved on, started off: the restart line for turning it on",
          texts(card).some((t) => t.startsWith("Saved. Applies at the next daemon restart (either")), texts(card));
    check("the Now line still says what runs now",
          texts(card).some((t) => t.startsWith("Now: off.")), texts(card));
  }

  {
    const host = { configured: true, state: "connected", pid: 42, sessions: 3 };
    const { w } = build([{ status: 200, body: { enabled: false, supported: true, host } }]);
    await w.refreshPtyHostSetting();
    const card = w.ptyHostSettingCard();
    check("connected: the Now line names the pid and the sessions",
          texts(card).includes("Now: on. PTY host pid 42 holds 3 sessions."), texts(card));
    check("saved off, started on: the restart line for turning it off",
          texts(card).some((t) => t.startsWith("Saved. Applies at the next daemon restart: the PTY host ends")),
          texts(card));
  }

  {
    const host = { configured: true, state: "stale", pid: 7, sessions: 1 };
    const { w } = build([{ status: 200, body: { enabled: true, supported: true, host } }]);
    await w.refreshPtyHostSetting();
    const card = w.ptyHostSettingCard();
    check("stale: the Now line says older code",
          texts(card).includes("Now: on. PTY host pid 7 holds 1 session; it runs older code than this daemon."),
          texts(card));
    check("on and started on: no restart line",
          !texts(card).some((t) => t.startsWith("Saved.")), texts(card));
  }

  {
    const host = { configured: true, state: "unreachable", pid: null, sessions: 0 };
    const { w } = build([{ status: 200, body: { enabled: true, supported: true, host } }]);
    await w.refreshPtyHostSetting();
    check("unreachable: the Now line says no host is connected",
          texts(w.ptyHostSettingCard()).some((t) => t.startsWith("Now: on, but no PTY host is connected")));
  }

  {
    const { w } = build([{ status: 200, body: { enabled: false, supported: false, host: HOST_OFF } }]);
    await w.refreshPtyHostSetting();
    const card = w.ptyHostSettingCard();
    check("unsupported: the box is disabled", checkbox(card).disabled === true);
    check("unsupported: the card says POSIX only",
          texts(card).some((t) => t.includes("POSIX only")), texts(card));
  }

  {
    const { w } = build([
      { status: 200, body: { enabled: false, supported: true, host: HOST_OFF } },
      { status: 400, body: { error: "'enabled' must be true or false" } },
    ]);
    await w.refreshPtyHostSetting();
    await w.savePtyHostSetting(true);
    const card = w.ptyHostSettingCard();
    check("a refused PUT shows the error",
          card.children.some((c) => c.cls === "error" && c.text.includes("must be true or false")),
          card.children.map((c) => c.text));
    check("a refused PUT keeps the saved value", checkbox(card).checked === false);
    check("a refused PUT leaves the box usable", checkbox(card).disabled === false);
  }

  if (failures) {
    console.log(`ptyhostsetting_check: ${failures} failure(s)`);
    process.exit(1);
  }
  console.log("ptyhostsetting_check ok");
})().catch((err) => { console.error(err); process.exit(1); });
