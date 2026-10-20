"""MCP tools for world-model simulations (claunch-efnbt).

Thin clients of the daemon's ``/api/sims`` routes: the daemon keeps the
records, walks the model and decides who may do what. Every call that
changes something carries ``$CLAUNCH_SESSION`` as the caller, so only a
simulation's owner session can change, run or stop it; any session can read
any simulation.
"""

from __future__ import annotations

import os
import urllib.parse

from . import daemon_client, mcp_rpc

_ID = {"sim": {"type": "string", "description": "simulation id (sim-xxxxxx)"}}

GUIDE = """\
World-model simulations: build a formal model of a feature (state machines,
variables, events, ECA rules, invariants) from its specs, code and
scenarios, then let the daemon walk it to find where the model has no answer
(unhandled state x event), two answers (conflict), a broken invariant, a dead
end (stuck) or a runaway event chain (loop).

Model shape (sim_patch upserts by id/name):
  machines:   {id, states: [..], initial}
  vars:       {name, init, domain?: [values the world may start with]}
  events:     {name, kind: actor|system|fault, available?: cond}
              actor/fault events the walk may fire any time they are
              available; system events only happen via emit/schedule.
  rules:      {id, on: {event}, when?: cond, do: [effects], priority?,
               kind?: effect|available, source: [{ref, quote}] | provisional: {why}}
  invariants: {id, holds: cond, source: [...]}
  checkpoints: {name: {sm: {...}, vars: {...}}}   extra start states
Conditions: true | {all:[..]} | {any:[..]} | {not: c} | {sm, is} |
  {var, eq|ne|lt|le|gt|ge: value} | {prose: "not formalized yet"} (unknown).
Effects: {sm, to} ("$prev" = previous state) | {set|add: var, value} |
  {emit: event} | {schedule: event, in: n} | {unschedule: event} |
  {roll: p, then: [..], else: [..]} | {prose: "..."}.
Values: scalars, {var}, {sm}, {op: + - * / min max, args: [..]}.

Rules:
- A rule quotes its source word for word: ref = a file path relative to the
  simulation root (the owner's checkout), quote = text in that file. The
  daemon checks it. A rule nothing says is a guess: mark it
  provisional: {why}. Answers from the user use ref "user:<finding key>".
- Keep the state Markov: anything the next step depends on is a machine or
  a variable, never history.
- Model the domain, not the scenarios: reuse one set of business states
  and user actions, and keep scenario numbers as metadata on the path (a
  checkpoint name, a source quote), never as their own steps or events.
  A model stitched from per-scenario states leaves most of the map empty.
- The map (Sims tab) keeps two things apart per state x event cell: what the
  model text says (rule yes/maybe/no, event available yes/maybe/no, judged
  with every other machine and variable unknown) and what the last round
  saw there (handled/unhandled counts). A rule whose state test lives only
  in the event's available condition still counts.
- sim_start needs settings.interpreter_profile (a profile that can launch
  pi; the Settings tab lists them) and the runtime (claunch sim runtime
  install). Without either it runs nothing and sets state fault with the
  reason; fix it and start again. The judge may use the same model.
- Work gaps in batches (sim_gaps groups them by place). For big models,
  hand each batch to a subagent so this conversation stays small.
- A gap that is intended or impossible: sim_resolve dismiss with the
  reason, or add an availability rule (kind: available). A gap only a human
  can decide: sim_resolve ask with options -- the user answers in the Sims
  tab and you are told.
- Goal-setting steps of a cflow run (marked sims: deny; the canonical
  workflows mark intake and the issue/goal decision) refuse sim_start and
  sim_round with "목표 승인 후 실행 가능" and the step name. sim_create,
  sim_patch and every read still work there; a patch to a running campaign
  then skips its round (round_skipped). Run once the goal is approved.
- settings.purpose: spec (default; the simulation checks its sources) or
  design (a design review the sources do not hold). spec with no sources
  is allowed but warned about (sim_show: purpose.warnings). A finding whose
  path ran through a provisional rule is triaged design | intended |
  decide instead of model | spec | intended, and gets no edit proposal
  when its path ran on provisional rules alone.
- When a running simulation finishes you get a "[claunch simulation]" line:
  read the result and carry the discussion on with the user.
"""

INSTRUCTIONS = """\
Simulations (sim_* tools): world models the daemon walks for gaps; call
sim_guide before writing one. Full guidance: prompt pack 'mcp-sim' (load it
on demand, see Prompt packs).
"""

TOOLS = [
    {"name": "sim_guide", "description": "The model format and how to work a simulation. Read before writing a model.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "sim_list", "description": "List simulations (any session may read). Filter by project or owner.",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"}, "owner": {"type": "string", "description": "session name; \"\" = unowned"},
         "mine": {"type": "boolean", "description": "only the ones this session owns"}}}},
    {"name": "sim_show", "description": "One simulation: record, counts, settings, recent rounds and journal.",
     "inputSchema": {"type": "object", "properties": _ID, "required": ["sim"]}},
    {"name": "sim_model", "description": "The simulation's world model (JSON).",
     "inputSchema": {"type": "object", "properties": _ID, "required": ["sim"]}},
    {"name": "sim_findings",
     "description": "Findings ledger: open, question, answered, dismissed, resolved. Each has a witness that replays.",
     "inputSchema": {"type": "object", "properties": {
         **_ID, "status": {"type": "string", "description": "comma list, e.g. open,question"},
         "kind": {"type": "string", "enum": ["unhandled", "conflict", "invariant", "stuck", "loop", "eval-error"]},
         "limit": {"type": "integer"}}, "required": ["sim"]}},
    {"name": "sim_gaps", "description": "Open findings grouped by place (machine/state), largest first: the unit to interpret in one go.",
     "inputSchema": {"type": "object", "properties": {**_ID, "limit": {"type": "integer"}}, "required": ["sim"]}},
    {"name": "sim_replay", "description": "Replay a finding's witness on the current model, step by step.",
     "inputSchema": {"type": "object", "properties": {**_ID, "key": {"type": "string"}}, "required": ["sim", "key"]}},
    {"name": "sim_proposals", "description": "Edit proposals to the sources (the editor's output), with their status "
                                             "and the quoted rules each would break.",
     "inputSchema": {"type": "object", "properties": _ID, "required": ["sim"]}},
    {"name": "sim_create",
     "description": "Create a simulation owned by this session, rooted at this session's checkout.",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string"}, "goal": {"type": "string"},
         "sources": {"type": "array", "items": {"type": "string"}, "description": "paths/globs the model is built from"},
         "issue": {"type": "string"},
         "from": {"type": "string", "description": "start from this simulation's model, verdicts and answered questions"},
         "settings": {"type": "object", "description": "interpreter_profile, judge_profile, *_model, *_reasoning, seed, budget, horizon, max_rounds, "
                                                              "purpose (spec|design, default spec)"}},
         "required": ["title"]}},
    {"name": "sim_configure", "description": "Owner: change title, goal, sources or settings.",
     "inputSchema": {"type": "object", "properties": {
         **_ID, "title": {"type": "string"}, "goal": {"type": "string"},
         "sources": {"type": "array", "items": {"type": "string"}},
         "settings": {"type": "object", "description": "as sim_create; purpose: spec|design"}},
         "required": ["sim"]}},
    {"name": "sim_patch",
     "description": "Owner: upsert model items by id/name, remove some ({remove: {rules: [ids]}}). Rejected as a whole "
                    "if the model does not check or a quote is not in its file. Pass key to make a retry harmless. "
                    "While running, a round reruns after every accepted patch.",
     "inputSchema": {"type": "object", "properties": {
         **_ID, "patch": {"type": "object"}, "key": {"type": "string"}}, "required": ["sim", "patch"]}},
    {"name": "sim_start", "description": "Owner: start the campaign. The daemon launches a run session that "
                                         "formalizes, judges and works the gaps itself -- leave the model to it and wait "
                                         "for the finished line. It needs an interpreter_profile that can launch pi and "
                                         "the installed runtime; without them nothing runs, the simulation goes to state "
                                         "fault and the error says what to fix. sim_round walks one round yourself. "
                                         "Refused (409) while this session's cflow step is marked sims: deny "
                                         "(goal-setting): build the model now, run it after the goal is approved.",
     "inputSchema": {"type": "object", "properties": _ID, "required": ["sim"]}},
    {"name": "sim_round", "description": "Owner: run one round now. Refused (409) while this session's cflow "
                                         "step is marked sims: deny (goal-setting): run it after the goal is approved.",
     "inputSchema": {"type": "object", "properties": _ID, "required": ["sim"]}},
    {"name": "sim_stop", "description": "Owner: stop the campaign.",
     "inputSchema": {"type": "object", "properties": {**_ID, "reason": {"type": "string"}}, "required": ["sim"]}},
    {"name": "sim_resolve",
     "description": "Owner: close findings by hand. items: [{key, action: dismiss, reason} | "
                    "{key, action: ask, question, options?} | {key, action: reopen}].",
     "inputSchema": {"type": "object", "properties": {
         **_ID, "items": {"type": "array", "items": {"type": "object"}}, "key": {"type": "string"}},
         "required": ["sim", "items"]}},
    {"name": "sim_claim", "description": "Take ownership of a simulation nobody owns (its owner exited or released it).",
     "inputSchema": {"type": "object", "properties": _ID, "required": ["sim"]}},
    {"name": "sim_release", "description": "Owner: give up ownership (a running campaign stops).",
     "inputSchema": {"type": "object", "properties": _ID, "required": ["sim"]}},
]


class SimMcpError(Exception):
    pass


def _client():
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        raise SimMcpError(f"cannot reach the claunch daemon: {daemon_client.unreachable_reason(why)}")
    return client


def _session() -> str:
    session = os.environ.get("CLAUNCH_SESSION")
    if not session:
        raise SimMcpError("no $CLAUNCH_SESSION -- simulation changes only work inside a managed claunch session")
    return session


def _sim(args: dict) -> str:
    sid = str(args.get("sim") or "")
    if not sid:
        raise SimMcpError("sim is required")
    return urllib.parse.quote(sid, safe="")


def call_tool(name: str, args: dict) -> dict:
    if name == "sim_guide":
        return {"guide": GUIDE}
    c = _client()
    if name == "sim_list":
        q = {}
        if args.get("project"):
            q["project"] = args["project"]
        if args.get("mine"):
            q["owner"] = _session()
        elif "owner" in args:
            q["owner"] = args["owner"]
        return c.get("/api/sims" + ("?" + urllib.parse.urlencode(q) if q else ""))
    if name == "sim_show":
        return c.get(f"/api/sims/{_sim(args)}")
    if name == "sim_model":
        return c.get(f"/api/sims/{_sim(args)}/model")
    if name == "sim_findings":
        q = {k: args[k] for k in ("status", "kind", "limit") if args.get(k)}
        return c.get(f"/api/sims/{_sim(args)}/findings" + ("?" + urllib.parse.urlencode(q) if q else ""))
    if name == "sim_gaps":
        q = {"limit": int(args.get("limit") or 8)}
        return c.get(f"/api/sims/{_sim(args)}/gaps?" + urllib.parse.urlencode(q))
    if name == "sim_replay":
        return c.get(f"/api/sims/{_sim(args)}/replay?" + urllib.parse.urlencode({"key": args.get("key") or ""}))
    if name == "sim_proposals":
        return c.get(f"/api/sims/{_sim(args)}/proposals")
    me = _session()
    if name == "sim_create":
        body = {k: args[k] for k in ("title", "goal", "sources", "issue", "settings", "from") if k in args}
        return c.post("/api/sims", {**body, "session": me})
    if name == "sim_configure":
        body = {k: args[k] for k in ("title", "goal", "sources", "settings") if k in args}
        return c.post(f"/api/sims/{_sim(args)}/settings", {**body, "session": me})
    if name == "sim_patch":
        return c.post(f"/api/sims/{_sim(args)}/model",
                      {"session": me, "patch": args.get("patch") or {}, "key": args.get("key")})
    if name in ("sim_start", "sim_round", "sim_stop", "sim_claim", "sim_release"):
        verb = name[4:]
        body = {"session": me}
        if name == "sim_stop" and args.get("reason"):
            body["reason"] = args["reason"]
        return c.post(f"/api/sims/{_sim(args)}/{verb}", body)
    if name == "sim_resolve":
        return c.post(f"/api/sims/{_sim(args)}/resolve",
                      {"session": me, "items": args.get("items") or [], "key": args.get("key")})
    raise SimMcpError(f"unknown tool {name!r}")


SERVER = mcp_rpc.Server(
    name="claunch-sim",
    tools=tuple(TOOLS),
    dispatch=call_tool,
    errors=(SimMcpError, daemon_client.DaemonClientError),
    instructions=INSTRUCTIONS,
)
