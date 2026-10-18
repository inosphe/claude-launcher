/* A run view asks for the v1 and the v2 run in one batch (claunch-74t39).

   The run page (refreshWf), the split pane under the terminal (refreshSplit)
   and the detail panel's run fold (refreshSessRun) each read
   /api/cflow/run, and draw /api/cflow2/run when the v1 slot is idle and
   points at v2 -- which is the usual case now. The v2 read used to go out
   only after the v1 answer had arrived, so every open and every poll paid two
   round trips on the daemon's loop. Both reads now leave in the same turn,
   which puts them in the same batch window (batchQueue).

   Pinned here, against the three functions sliced out of the shipped app.js
   with `api` stubbed:
     - both paths are asked before either answer is in;
     - an idle v1 slot that names v2 draws the v2 answer, and only it;
     - a live v1 run draws the v1 answer and leaves the v2 one unused. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8"
);

let failures = 0;
function check(name, cond, extra) {
  if (cond) return;
  failures += 1;
  console.log(`FAIL ${name}${extra === undefined ? "" : ` — ${JSON.stringify(extra)}`}`);
}

function slice(head) {
  const start = src.indexOf(head);
  if (start < 0) throw new Error(`cannot locate ${head}`);
  let depth = 0;
  for (let j = src.indexOf("{", start); j < src.length; j++) {
    if (src[j] === "{") depth += 1;
    else if (src[j] === "}") { depth -= 1; if (depth === 0) return src.slice(start, j + 1); }
  }
  throw new Error(`unbalanced ${head}`);
}

const code = [
  slice("async function fetchCflow2Run("),
  slice("async function refreshWf("),
  slice("async function refreshSplit("),
  slice("async function refreshSessRun("),
].join("\n");

const V1_IDLE = { status: "idle", v2: true };
const V1_LIVE = { status: "running", step: "s1" };
const V2_DOC = { run: { status: "running" }, chart: {} };

function world() {
  const asked = [];
  const drawn = [];
  const body = { innerHTML: "", appendChild() {} };
  const fold = {
    open: true,
    dataset: { cwd: "/repo", scope: "s1" },
    querySelector: () => body,
  };
  const ctx = {
    asked, drawn, fold,
    $: () => ({ innerHTML: "", appendChild() {} }),
    el: () => ({}),
    api(p) {
      return new Promise((resolve) => {
        asked.push({ path: p, answer: (doc) => resolve({ ok: true, json: async () => doc }) });
      });
    },
    renderCflow2Into: (_into, doc) => drawn.push(["v2", doc]),
    renderWf: (doc) => drawn.push(["v1", doc]),
    renderSplit: (doc) => drawn.push(["v1", doc]),
    renderSessRun: (_body, doc) => drawn.push(["v1", doc]),
    refreshCflow() {},
  };
  vm.createContext(ctx);
  vm.runInContext(`
    let wfCwd = "/repo", wfScope = "s1", wfRefreshRequest = 0, wfLastData = null;
    let splitFor = "s1", splitKind = "run", splitCwd = "/repo", splitScope = "s1",
        splitLastData = null, splitGen = 0, splitBody = { innerHTML: "", appendChild() {} };
    let sessRunFold = fold;
    ${code}
    this.refreshWf = refreshWf;
    this.refreshSplit = refreshSplit;
    this.refreshSessRun = refreshSessRun;
  `, ctx);
  return ctx;
}

const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };

async function scenario(fn, v1doc) {
  const w = world();
  const done = w[fn]();
  await flush();
  const paths = w.asked.map((a) => a.path.split("?")[0]);
  check(`${fn}: both reads leave before either answer`,
        paths.includes("/api/cflow/run") && paths.includes("/api/cflow2/run"), paths);
  for (const a of w.asked) {
    a.answer(a.path.startsWith("/api/cflow2/") ? V2_DOC : v1doc);
  }
  await done;
  await flush();
  return w.drawn;
}

(async () => {
  for (const fn of ["refreshWf", "refreshSplit", "refreshSessRun"]) {
    const idle = await scenario(fn, V1_IDLE);
    check(`${fn}: idle v1 naming v2 draws the v2 run`,
          idle.length === 1 && idle[0][0] === "v2" && idle[0][1] === V2_DOC, idle);
    const live = await scenario(fn, V1_LIVE);
    check(`${fn}: a live v1 run draws v1`,
          live.length === 1 && live[0][0] === "v1" && live[0][1] === V1_LIVE, live);
  }
  if (failures) {
    console.log(`runbatch_check: ${failures} failure(s)`);
    process.exit(1);
  }
  console.log("runbatch_check: run page, split pane and detail run fold batch their v1 and v2 reads");
})();
