// VM access requests as dashboard notices (vm-setup-8s6): one sticky card per
// request id, summarised with short paths, taken down when the request is gone,
// and the nav item counting what waits. Slices the block out of app.js.
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const src = fs.readFileSync(path.join(__dirname, "../../src/claude_launcher/web/static/app.js"), "utf8");
const from = src.indexOf("const VM_NOTICED_KEY");
const to = src.indexOf("/* thread replies as notices");
assert(from > 0 && to > from, "the VM notice block is in app.js");
const code = src.slice(from, to);

function build(apiBody, ok = true) {
  const cards = [], gone = [], store = {}, badge = { textContent: "", hidden: true };
  const ctx = {
    noticeLoad: (k, d) => (k in store ? JSON.parse(store[k]) : d),
    noticeSave: (k, v) => { store[k] = JSON.stringify(v); },
    notify: (title, sub, opts) => { cards.push({ title, sub, opts }); },
    dismissNotice: (key) => { gone.push(key); },
    $: (id) => (id === "vm-nav-badge" ? badge : null),
    api: async () => { if (apiBody === "throw") throw new Error("down"); return { ok, json: async () => apiBody }; },
    location: { hash: "" },
    String, Array, Set, JSON,
  };
  vm.createContext(ctx);
  vm.runInContext(code + "\nthis.sync = vmRequestSync; this.refresh = refreshVmRequests; this.summary = vmRequestSummary;", ctx);
  return { ctx, cards, gone, store, badge };
}

const req = { vm: "a", id: "r1", session: "s2", scopes: ["files", "exec"], reason: "x",
  shares: [{ name: "out", mode: "rw", host_path: "/Users/u/works/proj/out" }, { name: "docs", mode: "ro", host_path: "/d" }] };

(async () => {
  const t = build({ requests: [req] });
  await t.ctx.refresh();
  assert.strictEqual(t.cards.length, 1);
  const c = t.cards[0];
  assert.strictEqual(c.title, "s2 asks for a VM");
  assert.strictEqual(c.sub, "VM a: scopes: files+exec; share out (rw, WRITE) …/proj/out; share docs (ro) /d");
  assert(!c.sub.includes("/Users/u/works"), "a long path is cut");
  assert.strictEqual(c.opts.source, "vm");
  assert.strictEqual(c.opts.sticky, true);
  assert.strictEqual(c.opts.key, "vm-a:s2:r1");
  assert.strictEqual(c.opts.sync, "vm:a:s2:r1");
  c.opts.open();
  assert.strictEqual(t.ctx.location.hash, "#/vm");
  assert.deepStrictEqual([t.badge.textContent, t.badge.hidden], ["1", false]);

  // the same request is announced once
  t.ctx.sync([req]);
  assert.strictEqual(t.cards.length, 1);
  // a new request id (the session asked again) is announced again, and the old card goes
  t.ctx.sync([{ ...req, id: "r2" }]);
  assert.strictEqual(t.cards.length, 2);
  assert(t.gone.includes("vm-a:s2:r1"), "the replaced request's card comes down");
  // answered: the card comes down and the badge hides
  t.ctx.sync([]);
  assert(t.gone.includes("vm-a:s2:r2"));
  assert.deepStrictEqual([t.badge.textContent, t.badge.hidden], ["", true]);
  // a browser that kept the list does not announce again
  const kept = build({ requests: [req] });
  kept.store["claunch.vmNoticed"] = JSON.stringify(["a:s2:r1"]);
  await kept.ctx.refresh();
  assert.strictEqual(kept.cards.length, 0);
  assert.strictEqual(kept.badge.textContent, "1");
  // no VM hub (404) or a down daemon: nothing happens
  for (const t2 of [build({}, false), build("throw")]) {
    await t2.ctx.refresh();
    assert.strictEqual(t2.cards.length, 0);
    assert.strictEqual(t2.badge.hidden, true);
  }
  // more than three shares are counted, not listed
  const many = { ...req, scopes: [], shares: [1, 2, 3, 4, 5].map((i) => ({ name: `s${i}`, mode: "ro", host_path: `/p${i}` })) };
  assert(t.ctx.summary(many).endsWith("+2 more"));
  console.log("vmnotice_check ok");
})().catch((e) => { console.error(e); process.exit(1); });
