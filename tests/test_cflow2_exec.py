"""cflow v2 headless executor: argv from capabilities, result extraction, the
process run with retry/timeout/readonly (docs/cflow-v2.md 6.4, 7)."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time

import pytest

from claude_launcher.cflow2 import caps as caps_mod
from claude_launcher.cflow2 import executors, frontend, runtime
from claude_launcher.cflow2.store import RunStore
from claude_launcher.daemon import cflow2_exec


def _node(src: str, label: str = "t"):
    chart = frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent(src))
    for node in chart.nodes.values():
        if node.label == label:
            return node
    raise AssertionError(label)


READ_NODE = """
nodes:
  t: {prompt: T, returns: {ok: {hits: int}}, effects: [fs.read], executor: [headless],
      budget: {usd: 2}}
body: t
"""


def _argv(harness, src=READ_NODE, prompt="PROMPT", schema_file="/r/d1.schema.json"):
    caps = caps_mod.resolve(harness, doc={})
    plan = caps_mod.plan_node(_node(src), caps).to_dict()
    schema = executors.json_schema({"ok": {"hits": "int"}})
    argv, stdin = executors.build_argv([harness], caps, plan, prompt, harness=harness,
                                       schema=schema, schema_file=schema_file)
    return argv, stdin, plan, schema


def test_claude_argv_inline_schema_tools_restrict_budget():
    argv, stdin, plan, schema = _argv("claude")
    assert stdin is None
    assert argv[:3] == ["claude", "-p", "PROMPT"]
    i = argv.index("--json-schema")
    assert json.loads(argv[i + 1]) == schema
    assert argv[argv.index("--output-format") + 1] == "json"
    assert plan["readonly"]["route"] == "tools-restrict"
    # --tools is the available set (removes the rest); --allowedTools only
    # auto-approves and is not used (A4)
    assert "--allowedTools" not in argv
    tools = argv[argv.index("--tools") + 1].split(",")
    assert "Read" in tools and not {"Edit", "Write", "Bash", "MultiEdit"} & set(tools)
    deny = argv[argv.index("--disallowedTools") + 1].split(",")
    assert {"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"} <= set(deny)
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--strict-mcp-config" in argv
    # the prompt stays right after -p, ahead of the variadic tool flags
    assert argv.index("PROMPT") < argv.index("--tools")
    assert argv[argv.index("--max-budget-usd") + 1] == "2"


def test_codex_argv_schema_file_and_sandbox():
    argv, _stdin, plan, _schema = _argv("codex")
    assert argv[:3] == ["codex", "exec", "PROMPT"]
    assert argv[argv.index("--output-schema") + 1] == "/r/d1.schema.json"
    assert "--json" in argv
    assert plan["readonly"]["route"] == "sandbox-readonly"
    assert argv[argv.index("-s") + 1] == "read-only"
    assert "--max-budget-usd" not in argv


def test_devin_falls_to_worktree_and_text_route():
    argv, _stdin, plan, _schema = _argv("devin")
    assert argv == ["devin", "-p", "PROMPT"]
    assert plan["readonly"]["route"] == "throwaway-worktree"
    assert plan["returns"]["route"] == "text+fenced-block"


def test_long_prompt_goes_to_stdin():
    long = "x" * (executors.ARGV_PROMPT_MAX + 1)
    argv, stdin, _plan, _schema = _argv("codex", prompt=long)
    assert stdin == long and argv[:3] == ["codex", "exec", "-"]
    argv, stdin, _plan, _schema = _argv("claude", prompt=long)
    assert stdin == long and long not in argv


def test_json_schema_is_strict_and_nullable():
    schema = executors.json_schema({"ok": {"hits": "int", "note": "str?"}, "rework": {}})
    assert schema["properties"]["outcome"]["enum"] == ["ok", "rework", "fail"]
    value = schema["properties"]["value"]
    assert value["required"] == ["hits", "note"] and value["additionalProperties"] is False
    assert value["properties"]["hits"]["type"] == ["integer", "null"]


def test_prompt_always_has_the_fenced_json_instruction():
    brief = {"delegation": "d1", "node": "t", "tasks": [{"node": "t", "prompt": "do it"}],
             "returns": {"ok": {"hits": "int"}}, "rules": [{"id": "r", "text": "be nice"}]}
    text = executors.build_prompt(brief, errors=["missing field 'hits'"], readonly=True)
    assert "```json" in text and "do it" in text and "be nice" in text
    assert "READ-ONLY" in text and "missing field 'hits'" in text


# -- extraction ---------------------------------------------------------------

FENCED = 'done.\n```json\n{"outcome": "ok", "value": {"hits": 3}}\n```\n'


def test_extract_claude_json_result_field():
    out = json.dumps({"type": "result", "result": FENCED, "total_cost_usd": 0.1})
    got, how = executors.extract_result(out, json_output=True)
    assert got == ("ok", {"hits": 3}) and how == "fenced-block"


def test_extract_claude_structured_output():
    out = json.dumps({"type": "result", "result": "",
                      "structured_output": {"outcome": "ok", "value": {"hits": 1}}})
    got, how = executors.extract_result(out, json_output=True)
    assert got == ("ok", {"hits": 1}) and how == "structured_output"


def test_extract_codex_jsonl_last_agent_message():
    lines = [
        {"type": "thread.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "thinking"}},
        {"type": "item.completed", "item": {"type": "agent_message",
                                            "text": '{"outcome": "ok", "value": {"hits": 7}}'}},
        {"type": "turn.completed"},
    ]
    out = "\n".join(json.dumps(x) for x in lines)
    got, how = executors.extract_result(out, json_output=True)
    assert got == ("ok", {"hits": 7}) and how == "json-document"
    old = json.dumps({"msg": {"type": "agent_message", "message": FENCED}})
    assert executors.extract_result(old, json_output=True)[0] == ("ok", {"hits": 3})


def test_extract_text_last_block_and_failures():
    two = '```json\n{"value": 1}\n```\nlater\n' + FENCED
    assert executors.extract_result(two, json_output=False)[0] == ("ok", {"hits": 3})
    got, how = executors.extract_result("no json here", json_output=False)
    assert got is None and "no JSON" in how
    got, how = executors.extract_result("```json\n{bad\n```", json_output=False)
    assert got is None and "not valid JSON" in how
    # a bare value (no outcome key) is the ok value
    assert executors.extract_result('{"hits": 2}', json_output=False)[0] == ("ok", {"hits": 2})


# -- the process ----------------------------------------------------------------

FAKE = r'''
import json, os, sys, time
d = os.environ["FAKE_DIR"]
n_path = os.path.join(d, "n")
n = int(open(n_path).read()) + 1 if os.path.exists(n_path) else 1
open(n_path, "w").write(str(n))
json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(),
           "stdin": None if sys.stdin is None or sys.stdin.isatty() else ""},
          open(os.path.join(d, f"argv-{n}.json"), "w"))
plan = json.load(open(os.path.join(d, "plan.json")))
step = plan[min(n, len(plan)) - 1]
if step.get("write"):
    open(step["write"], "w").write("x")
if step.get("sleep"):
    time.sleep(step["sleep"])
sys.stdout.write(step.get("out", ""))
sys.exit(step.get("code", 0))
'''

TEXT_CAPS = {"headless.run": caps_mod.Capability(True, ["-p"])}


class Fake:
    def __init__(self, tmp_path, plan, *, cwd=None, caps=None, src=None, scratch=None):
        self.dir = tmp_path / "fake"
        self.dir.mkdir(exist_ok=True)
        (self.dir / "plan.json").write_text(json.dumps(plan))
        script = self.dir / "harness.py"
        script.write_text(FAKE)
        self.cwd = str(cwd or tmp_path / "run")
        (tmp_path / "run").mkdir(exist_ok=True)
        self.scope = "own"
        chart = frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent(src or """
            nodes:
              t: {prompt: "count things", returns: {ok: {hits: int}}, executor: [headless],
                  budget: {seconds: 20}}
              after: {prompt: after}
            body: [t, after]
        """))
        executors.register(lambda rec, run: "queued")
        try:
            self.st = runtime.start(chart, cwd=self.cwd, scope=self.scope,
                                    caps=caps if caps is not None else TEXT_CAPS)
        finally:
            executors.register(None)
        self.did = self.st["delegations"][0]["id"]
        import os
        env = dict(os.environ, FAKE_DIR=str(self.dir))
        self.launch = cflow2_exec.Launch(harness="claude", command=[sys.executable, str(script)],
                                         env=env, scratch=scratch)

    def run(self, **kw):
        return cflow2_exec.run_headless(self.cwd, self.scope, self.did, self.launch, **kw)

    def argv(self, n):
        return json.loads((self.dir / f"argv-{n}.json").read_text())

    def status(self):
        return runtime.status(cwd=self.cwd, scope=self.scope, tick=False)

    def events(self, name):
        return [e for e in runtime.journal(cwd=self.cwd, scope=self.scope) if e["event"] == name]

    def rec(self):
        return RunStore(self.cwd, self.scope).load_state()["delegations"][self.did]


def test_headless_success_submits_and_keeps_stdout(tmp_path):
    f = Fake(tmp_path, [{"out": FENCED}])
    res = f.run()
    assert res["result"] == "returned"
    st = f.status()
    assert [t["label"] for t in st["tasks"]] == ["after"]
    assert f.rec()["status"] == "returned"
    out = RunStore(f.cwd, f.scope).receipt_path(f.did, ".out")
    assert "hits" in out.read_text()
    assert f.events("delegate_returned")
    argv = f.argv(1)["argv"]
    assert argv[0] == "-p" and "count things" in argv[1] and "```json" in argv[1]


def test_headless_retry_once_with_errors(tmp_path):
    f = Fake(tmp_path, [{"out": "```json\n{\"outcome\": \"ok\", \"value\": {\"hits\": \"x\"}}\n```"},
                        {"out": FENCED}])
    assert f.run()["result"] == "returned"
    assert "Previous attempt was refused" in f.argv(2)["argv"][1]
    assert "hits" in f.argv(2)["argv"][1]
    retry = [e for e in f.events("delegate_failed") if e.get("retry")]
    assert len(retry) == 1
    assert RunStore(f.cwd, f.scope).receipt_path(f.did, ".2.out").is_file()


def test_headless_second_failure_fails_node(tmp_path):
    f = Fake(tmp_path, [{"out": "nothing", "code": 2}, {"out": "still nothing"}])
    res = f.run()
    assert res["result"] == "failed"
    assert f.rec()["status"] == "failed"
    assert f.status()["status"] == "failed"
    assert any("exited 2" in " ".join(e.get("errors") or [])
               for e in f.events("delegate_failed"))


def test_headless_timeout_kills_the_process(tmp_path):
    f = Fake(tmp_path, [{"sleep": 30, "out": FENCED}], src="""
        nodes:
          t: {prompt: T, returns: {ok: {}, timeout: {}}, executor: [headless], budget: {seconds: 1}}
        body: t
    """)
    t0 = time.monotonic()
    res = f.run()
    assert time.monotonic() - t0 < 15
    assert res["result"] == "timeout"
    assert f.rec()["status"] == "timeout"
    st = f.status()
    assert st["outcome"] == "timeout"


def _git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(path), *cmd], check=True)
    (path / "f.txt").write_text("1")
    (path / ".gitignore").write_text(".cflow2/\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return path


RO_SRC = """
    nodes:
      t: {prompt: T, returns: {ok: {hits: int}}, effects: [fs.read], executor: [headless]}
    body: t
"""


def test_pi_argv_tools_allowlist():
    argv, _stdin, plan, _schema = _argv("pi")
    assert plan["readonly"]["route"] == "tools-restrict"
    assert argv[argv.index("--tools") + 1] == "read,grep,find,ls"


RESTRICT_CAPS = {"headless.run": caps_mod.Capability(True, ["-p"]),
                 "headless.tools.restrict": caps_mod.Capability(True, ["--tools", "{tools}"])}


def test_readonly_tools_route_porcelain_violation_fails_delegation(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    f = Fake(tmp_path, [{"out": FENCED, "write": str(repo / "new.txt")}], cwd=repo,
             caps=RESTRICT_CAPS, src=RO_SRC)
    res = f.run()
    assert res["result"] == "readonly-violation" and res.get("readonly_violation")
    assert "new.txt" in res["changed"]
    assert "--tools" in f.argv(1)["argv"]
    ev = f.events("monitor_violation")
    assert ev and ev[0]["kind"] == "readonly"
    assert f.rec()["status"] == "failed"
    st = f.status()
    assert st["status"] == "failed" and st["outcome"] == "fail"
    assert any("read-only" in (e.get("reason") or "") for e in f.events("delegate_failed"))


def test_readonly_tools_route_clean_run_returns(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    f = Fake(tmp_path, [{"out": FENCED}], cwd=repo, caps=RESTRICT_CAPS, src=RO_SRC)
    assert f.run()["result"] == "returned"


def test_readonly_throwaway_worktree_unavailable_reroutes(tmp_path):
    # not a git repository: no worktree, no sandbox, no restriction -> the
    # delegate must not run in the checkout at all
    f = Fake(tmp_path, [{"out": FENCED, "write": "inside.txt"}], caps=TEXT_CAPS,
             src=RO_SRC.replace("executor: [headless]", "executor: [headless, inline]"))
    assert f.rec()["route"]["readonly"]["route"] == "throwaway-worktree"
    res = f.run()
    assert res["result"] == "rerouted" and res["executor"] == "inline"
    assert not (f.dir / "n").exists()  # the harness never ran
    assert [t["label"] for t in f.status()["tasks"]] == ["t"]


def test_readonly_throwaway_worktree(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    scratch = tmp_path / "scratch"
    f = Fake(tmp_path, [{"out": FENCED, "write": "inside.txt"}], cwd=repo,
             caps=TEXT_CAPS, src=RO_SRC, scratch=str(scratch))
    assert f.rec()["route"]["readonly"]["route"] == "throwaway-worktree"
    res = f.run()
    assert res["result"] == "returned" and not res.get("readonly_violation")
    ran_in = f.argv(1)["cwd"]
    assert ran_in.startswith(str(scratch))
    assert not (repo / "inside.txt").exists()
    assert not list((scratch / "cflow2-work").iterdir())
    wt = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True,
                        text=True).stdout
    assert ran_in not in wt


def test_run_gone_midway_is_reported_not_raised(tmp_path):
    f = Fake(tmp_path, [{"out": FENCED}])
    runtime.abort("stop", cwd=f.cwd, scope=f.scope)
    res = f.run()
    assert res["result"] == "error"


def test_launch_takes_the_owning_sessions_profile_env(home, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from claude_launcher import harnesses, lineage, profile
    from claude_launcher.daemon import harness as daemon_harness
    from claude_launcher.daemon.harness import SessionDef

    selected = profile.create("codex")
    lineage.set_harness(selected, "codex")
    monkeypatch.setattr(harnesses.Harness, "available", lambda self: True)
    sdef = daemon_harness.normalize(SessionDef(name="own", profile="codex:codex",
                                               cwd=str(tmp_path), model="luna"))
    _argv, env, _cwd = daemon_harness.build_command(sdef)
    launch = cflow2_exec.launch_for_session(SimpleNamespace(sdef=sdef))
    assert launch.harness == "codex"
    entry = harnesses.get("codex")
    assert launch.command == list(entry.launch_command())
    assert entry.home_env and launch.env[entry.home_env] == env[entry.home_env]
    assert launch.env["CLAUNCH_SESSION"] == "own"
    assert launch.extra_args == ["-c", "model=gpt-5.6-luna"]
    # the interactive full-access args are not carried into the headless run
    assert not set(entry.args) & set(launch.command + launch.extra_args)


# -- A5: interruption, attempt numbering, runner marker ---------------------------


def _claim(f, runner=None):
    runtime.delegate_update(f.did, cwd=f.cwd, scope=f.scope, status="running",
                            runner=runner or {"boot": "b"})


def test_runner_marker_records_pid_and_attempt(tmp_path):
    f = Fake(tmp_path, [{"out": "nothing"}, {"out": FENCED}])
    _claim(f)
    seen = []
    orig = runtime.delegate_update

    def spy(did, **kw):
        if kw.get("runner"):
            seen.append(dict(kw["runner"]))
        return orig(did, **kw)

    import unittest.mock as um
    with um.patch.object(runtime, "delegate_update", spy):
        assert f.run(runner={"boot": "b"})["result"] == "returned"
    assert [m["attempt"] for m in seen] == [1, 2]
    assert all(m["pid"] > 0 and m["boot"] == "b" and m["argv0"] for m in seen)


def test_shutdown_kill_requeues_without_retry(tmp_path):
    import threading

    f = Fake(tmp_path, [{"sleep": 30, "out": FENCED}, {"out": FENCED}])
    _claim(f)
    procs = cflow2_exec.Procs()
    key = ("k",)
    out = {}
    t = threading.Thread(target=lambda: out.update(f.run(procs=procs, key=key)))
    t.start()
    deadline = time.monotonic() + 10
    while not procs.keys() and time.monotonic() < deadline:
        time.sleep(0.05)
    procs.kill_all()
    t.join(15)
    assert out["result"] == "interrupted" and out["why"] == "shutdown"
    rec = f.rec()
    assert rec["status"] == "queued" and rec["attempts"] == 0 and rec.get("runner") is None
    assert int((f.dir / "n").read_text()) == 1  # no second attempt was started
    assert not [e for e in f.events("delegate_failed")]
    assert f.events("delegate_interrupted")


def test_cancel_kill_writes_nothing(tmp_path):
    import threading

    f = Fake(tmp_path, [{"sleep": 30, "out": FENCED}])
    _claim(f)
    procs = cflow2_exec.Procs()
    out = {}
    t = threading.Thread(target=lambda: out.update(f.run(procs=procs, key="k")))
    t.start()
    deadline = time.monotonic() + 10
    while not procs.keys() and time.monotonic() < deadline:
        time.sleep(0.05)
    runtime.abort("stop", cwd=f.cwd, scope=f.scope)
    assert procs.kill("k", cflow2_exec.KILL_CANCELLED)
    t.join(15)
    assert out["result"] == "cancelled"
    assert f.rec()["status"] == "cancelled"


def test_rerun_after_restart_continues_attempt_count(tmp_path):
    f = Fake(tmp_path, [{"out": "still nothing"}])
    _claim(f)
    runtime.delegate_reject(f.did, ["no JSON"], cwd=f.cwd, scope=f.scope)  # attempt 1 refused
    res = f.run()
    assert res["result"] == "failed" and res["attempts"] == 2
    assert "Previous attempt was refused" in f.argv(1)["argv"][1]
    assert RunStore(f.cwd, f.scope).receipt_path(f.did, ".2.out").is_file()


def test_process_alive_identifies_by_argv0():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            start_new_session=True)
    try:
        assert cflow2_exec.process_alive(proc.pid, sys.executable) in (True, None)
        if sys.platform.startswith("linux"):
            assert cflow2_exec.process_alive(proc.pid, sys.executable) is True
            assert cflow2_exec.process_alive(proc.pid, "/bin/not-this") is False
    finally:
        cflow2_exec.kill_group(proc.pid)
        proc.wait(10)
    assert cflow2_exec.process_alive(proc.pid, sys.executable) is False
    assert cflow2_exec.process_alive(None) is False


def test_a_nested_session_delegation_is_told_to_drive_its_run():
    from claude_launcher.cflow2 import executors as ex

    brief = {"delegation": "d1", "node": "n", "tasks": [], "returns": {},
             "nested": {"cwd": "/w", "scope": "s1~d1"},
             "submit": "drive the nested run under scope 's1~d1' with flow_*"}
    text = ex.session_task(brief, cwd="/w", scope="s1", receipt_path="/w/r.json")
    assert "scope 's1~d1'" in text
    assert "flow_delegate" not in text
