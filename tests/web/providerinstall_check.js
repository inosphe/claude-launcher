/* Settings ▸ Providers (#/settings/providers) — the install form.

   The server half (claude_launcher.provider_install) is tested in Python; what
   is pinned here is the page's side of the contract:

   - the form draws a control for every schema key the issue lists, and the
     preset picker comes from the server's preset files (a preset that failed
     to parse is disabled rather than silently offered);
   - picking a preset fills the form and keeps the provider id the person had
     already typed — a preset is a starting value, not a rename;
   - Save posts {name, entry, profile} where the entry carries only the fields
     that have a value (a blank input leaves the key out rather than writing an
     empty string), and the numbers go as numbers;
   - a 400 shows the server's reason and leaves the form up;
   - Check posts the same entry to /api/providers/check and renders the answer
     without saving anything. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "src", "claude_launcher", "web", "static", "app.js"),
  "utf8"
);

function slice(from, to) {
  const start = src.indexOf(from);
  if (start < 0) throw new Error("missing " + from);
  const end = src.indexOf(to, start);
  if (end < 0) throw new Error("missing " + to);
  return src.slice(start, end);
}

let failures = 0;
function check(what, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    console.error(`FAIL ${what}\n  got  ${g}\n  want ${w}`);
    failures++;
  }
}
function ok(what, cond) {
  if (!cond) { console.error(`FAIL ${what}`); failures++; }
}

function node(tag) {
  return {
    tag, kids: [], textContent: "", className: "", handlers: {},
    value: "", checked: false, disabled: false, title: "", type: "",
    appendChild(child) { this.kids.push(child); return child; },
    append(...children) { children.forEach((c) => this.appendChild(c)); },
    addEventListener(kind, fn) { (this.handlers[kind] ||= []).push(fn); },
    fire(kind) { (this.handlers[kind] || []).forEach((fn) => fn({})); },
  };
}
const all = (n, out = []) => { out.push(n); (n.kids || []).forEach((k) => all(k, out)); return out; };
const text = (n) => all(n).map((k) => k.textContent || "").join(" ");
const byTag = (n, tag) => all(n).filter((k) => k.tag === tag);

const calls = [];
let responder = () => ({ ok: true, status: 200, body: {} });

const context = {
  console,
  el(tag, cls, content) {
    const n = node(tag);
    if (cls) n.className = cls;
    if (content !== undefined) n.textContent = content;
    return n;
  },
  document: { createElement: (tag) => node(tag) },
  wsOpen: true,
  renderWorkspaces() {},
  async api(path, opts = {}) {
    calls.push({ path, opts });
    const answer = responder(path, opts);
    return {
      ok: answer.ok, status: answer.status,
      json: async () => answer.body,
    };
  },
};
/* ``let`` at the top level of a vm script is a lexical binding, not a property
   of the context -- the page's own state is therefore *installed* here rather
   than declared by the slice, which is also how profilesettings_check.js
   supplies wsSection. The slice starts at the constants for the same reason. */
context.providersState = {
  data: null, error: "", draft: null, busy: "", notice: "", check: null,
};
vm.createContext(context);
vm.runInContext(
  slice("const PROVIDER_FORM_FIELDS = [", "function promptPresetCard()"),
  context
);

const LISTING = {
  providers: [
    { name: "opengateway",
      entry: { endpoints: { anthropic: "https://apis.opengateway.ai",
                            openai: "https://apis.opengateway.ai/v1" },
               models: { default: "deepseek/deepseek-v4.1-flash-ultrafast" },
               context_window: 1000000,
               routing: { order: ["CoreWeave"] },
               harness_options: { claude: { env: { A: "1" } } } },
      api_key_set: true,
      problems: [], kept: ["routing"] },
    { name: "broken", entry: {}, problems: ["endpoints is required"] },
  ],
  presets: [
    { name: "gateway", label: "Example gateway", note: "", error: "",
      path: "/p/gateway.yaml",
      entry: { endpoints: { openai: "https://gateway.example.com/v1" },
               models: { default: "some/model" } } },
    { name: "typo", label: "typo", note: "", error: "unknown field(s) endpionts",
      path: "/p/typo.yaml", entry: {} },
  ],
  profiles: [{ name: "work", provider: "opengateway", effective: "opengateway" }],
  global_provider: "",
  preset_dir: "/opt/py/lib/claude_launcher/provider-presets",
  fields: { protocols: ["anthropic", "openai"], roles: [], reasoning_efforts: [],
            openai_reasoning_formats: [], anthropic_auth_headers: [], fields: [],
            reserved: ["default", "claude"] },
};

(async () => {
  context.providersState.data = LISTING;
  context.providersState.draft = context.providerDraftFromEntry({});
  const card = context.providersPanel();
  const page = text(card);

  // --- the form covers the schema -----------------------------------------
  for (const label of [
    "endpoints.anthropic", "endpoints.openai", "api_key", "models.default",
    "models.small", "models.large", "models.xlarge", "models.subagent",
    "context_window", "auto_compact_at", "claude model_tag",
    "anthropic_auth_header", "session_header", "session_affinity",
    "allowed_harnesses", "service", "harness", "reasoning_effort",
    "openai_reasoning_format",
  ]) {
    ok(`the form has a row for ${label}`, page.includes(label));
  }
  ok("the preset directory is shown", page.includes("/opt/py/lib/claude_launcher/provider-presets"));
  ok("an installed provider with a problem shows the reason", page.includes("endpoints is required"));
  ok("an installed provider's facts are drawn",
     page.includes("anthropic: https://apis.opengateway.ai"));

  // --- loading a provider and saving it back is lossless -------------------
  // The reviewed defect: the form drew no control for `routing`, `dynamic`,
  // `capabilities` or the harness options other than claude.model_tag, and a
  // save replaced the stored entry, deleting them while reporting success.
  // The fixture carries `claude.env` although the server withholds it today
  // (it can hold a token): the page's own rule is "never drop what I do not
  // draw", and it has to hold for any entry a listing puts in front of it.
  {
    const loaded = context.providerDraftFromEntry(
      LISTING.providers[0].entry,
      { kept: ["routing"], api_key_set: LISTING.providers[0].api_key_set }
    );
    const back = context.providerEntryFromDraft(loaded);
    check("routing survives a load and a save", back.routing, { order: ["CoreWeave"] });
    check("an un-drawn harness option survives", back.harness_options.claude.env, { A: "1" });
    ok("the key is never part of the entry the form sends",
       loaded.residual.api_key === undefined && !("api_key" in loaded.residual));
    // The listing's own display flag is not a provider field; posting it back
    // is refused as `unknown field(s): api_key_set`. The server keeps it on the
    // row, and the page must not forward it even if one arrives in the entry.
    ok("the page never posts the listing's api_key_set back",
       !("api_key_set" in back) && !("api_key_set" in loaded.residual));
    ok("a stored key is reported from the row, not the entry", loaded.api_key_set === true);
    const was = context.providersState.draft;
    context.providersState.draft = loaded;
    const panel = text(context.providersPanel());
    context.providersState.draft = was;
    ok("the page says which keys it is not drawing",
       panel.includes("saving cannot remove them") && panel.includes("routing"));
    ok("the page says a key is stored without showing it",
       panel.includes("a key is stored for this provider"));
  }

  // --- presets -------------------------------------------------------------
  const selects = byTag(card, "select");
  const presetSelect = selects[0];
  check("the preset picker offers scratch plus one option per file",
        presetSelect.kids.map((o) => o.value), ["", "gateway", "typo"]);
  ok("a broken preset is disabled rather than offered",
     presetSelect.kids[2].disabled === true && presetSelect.kids[1].disabled === false);

  context.providersState.draft.name = "mygateway";
  presetSelect.value = "gateway";
  presetSelect.fire("change");
  check("picking a preset fills the form",
        context.providersState.draft["endpoints.openai"], "https://gateway.example.com/v1");
  check("picking a preset keeps the id already typed",
        context.providersState.draft.name, "mygateway");

  // --- save ---------------------------------------------------------------
  const draft = context.providersState.draft;
  draft["endpoints.anthropic"] = "https://gateway.example.com";
  draft["models.small"] = "";
  draft.context_window = "200000";
  draft.auto_compact_at = "";
  draft.session_affinity = true;
  draft.allowed_harnesses = "claude, codex";
  draft.profile = "work";

  responder = () => ({ ok: true, status: 200, body: LISTING });
  await context.providersSave();
  const save = calls[calls.length - 1];
  check("save posts to /api/providers", save.path, "/api/providers");
  check("save is a POST", save.opts.method, "POST");
  const body = JSON.parse(save.opts.body);
  check("the id goes as typed", body.name, "mygateway");
  check("the profile pin goes with it", body.profile, "work");
  check("blank inputs leave their key out", "small" in body.entry.models, false);
  check("numbers go as numbers", body.entry.context_window, 200000);
  check("an empty number leaves its key out", "auto_compact_at" in body.entry, false);
  check("a ticked box goes as true", body.entry.session_affinity, true);
  check("a list is split on commas", body.entry.allowed_harnesses, ["claude", "codex"]);
  check("both endpoints survive", body.entry.endpoints,
        { anthropic: "https://gateway.example.com",
          openai: "https://gateway.example.com/v1" });
  ok("a save reports itself", (context.providersState.notice || "").includes("saved"));

  // --- a refusal leaves the form up, showing the reason --------------------
  responder = () => ({ ok: false, status: 400,
                       body: { error: "unknown field(s): endpionts" } });
  await context.providersSave();
  ok("a 400 is shown verbatim",
     (context.providersState.error || "").includes("unknown field(s): endpionts"));
  ok("a refused save does not claim success", !(context.providersState.notice || "").includes("saved"));

  // --- the connection check ------------------------------------------------
  responder = () => ({ ok: true, status: 200,
                       body: { ok: false, status: 401, url: "https://gateway.example.com/v1/models",
                               detail: "HTTP 401: bad key", elapsed_ms: 4,
                               authenticated: true, token_source: "api_key" } });
  calls.length = 0;
  context.providersState.notice = "";
  await context.providersCheck();
  const probeCall = calls[calls.length - 1];
  check("check posts to /api/providers/check", probeCall.path, "/api/providers/check");
  check("check sends the same entry the form holds",
        JSON.parse(probeCall.opts.body).entry.endpoints.anthropic,
        "https://gateway.example.com");
  const after = context.providersPanel();
  const shown = text(after);
  ok("a failed check says so instead of failing the save",
     shown.includes("not connected") && shown.includes("HTTP 401"));
  ok("a check says whether a credential was used", shown.includes("checked with a credential"));

  if (failures) {
    console.error(`providerinstall_check: ${failures} failure(s)`);
    process.exit(1);
  }
  console.log("providerinstall_check: ok");
})();
