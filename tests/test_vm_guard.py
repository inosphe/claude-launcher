"""VM-only mode (vm-setup-8s6, s2-vmprompt): the decision, the per-session switch, the
Claude Code hook that carries it, and the user's routes."""
from __future__ import annotations

import argparse
import io
import json
import os
import stat
import subprocess
import sys

import pytest

from claude_launcher import cli_vm, profile
from claude_launcher.daemon import actors, harness, paths, vm_guard

from test_vm_share import AGENT, Host, api_for, run  # noqa: F401


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


# ---- the decision ----------------------------------------------------------------------- #
@pytest.mark.parametrize("command, kind, rule", [
    ("make -j8 all", "build", "make"), ("xcodebuild -scheme X build", "build", "xcodebuild"),
    ("swift build -c release", "build", "swift build"), ("cargo test --all", "build", "cargo test"),
    ("npm run build", "build", "npm run"), ("pytest -q tests", "build", "pytest"),
    ("python -m pytest tests", "build", "python -m pytest"), ("uv run pytest -x", "build", "uv run pytest"),
    ("./gradlew assemble", "build", "gradlew"), ("tsc --noEmit", "build", "tsc"),
    ("npm install left-pad", "install", "npm install"), ("pip3 install requests", "install", "pip3 install"),
    ("brew install jq", "install", "brew install"), ("uv sync", "install", "uv sync"),
    ("python tool.py", "run", "python"), ("python3.12 x.py", "run", "python3"), ("node app.js", "run", "node"),
    ("./run.sh --fast", "run", "./*"), ("open -a Simulator", "run", "open"), ("uv run python x.py", "run", "uv run"),
    # wrapped, chained, nested
    ("FOO=1 make", "build", "make"), ("sudo -E npm install", "install", "npm install"),
    ("env CC=gcc make", "build", "make"), ("cd src && make test", "build", "make"),
    ("git status; npm run build", "build", "npm run"), ("ls | python x.py", "run", "python"),
    ("bash -c 'cd x && make'", "build", "make"), ("sh -c \"python3 x.py\"", "run", "python3"),
    ("echo $(npm install x)", "install", "npm install"), ("nohup ./serve &", "run", "./*"),
])
def test_the_approved_deny_list_refuses(command, kind, rule):
    hit = vm_guard.check(command)
    assert hit is not None and hit[0] == kind and rule.split()[0] in hit[1], (command, hit)


@pytest.mark.parametrize("command", [
    "git status", "git commit -m 'make build'", "git log --oneline | head", "claunch beads list",
    "claunch vm only ls", "ls -la", "cat Makefile", "grep -rn make src", "rg 'npm install' docs",
    "sed -n 1,20p x.py", "echo make", "echo 'python x.py'", "find . -name '*.py'", "head -5 package.json",
    "cd src && git diff", "tail -f log.txt", "which python", "pwd", "", "   ",
])
def test_git_claunch_and_read_commands_pass(command):
    assert vm_guard.check(command) is None, command


def test_the_head_kept_for_the_history_is_three_words_at_most():
    assert vm_guard.check("API_KEY=sekret make deploy --token abc123 now") == ("build", "make", "make deploy --token")
    assert vm_guard.check("FOO=bar python x.py y z")[2] == "python x.py y"
    assert vm_guard.check(None) is None and vm_guard.check(5) is None
    assert vm_guard.check("echo " + "$(" * 20 + "make" + ")" * 20) in (None, ("build", "make", "make"))   # bounded, no crash


def test_the_message_says_to_use_vm_exec():
    text = vm_guard.message("build", "make")
    assert "vm_exec" in text and "VM 전용 모드" in text and "git" in text


# ---- the switch --------------------------------------------------------------------------- #
def test_the_switch_is_a_file_in_the_session_dir_and_needs_the_hook(home):
    assert not vm_guard.is_on("s1") and not vm_guard.hooked("s1")
    ok, why = vm_guard.can_enable("s1", "claude")
    assert not ok and "hook" in why
    ok, why = vm_guard.can_enable("s1", "codex")
    assert not ok and "codex" in why
    vm_guard.mark_hooked("s1", True)
    assert vm_guard.can_enable("s1", "claude") == (True, "")
    assert vm_guard.can_enable("s1", "codex")[0] is False
    vm_guard.set_on("s1", True)
    path = vm_guard.flag_path("s1")
    assert path.parent == paths.session_dir("s1") and path.name == "vm-only.json"
    assert vm_guard.is_on("s1") and vm_guard.since("s1") > 0 and stat.S_IMODE(path.stat().st_mode) == 0o600
    st = vm_guard.status("s1", "claude")
    assert st["on"] and st["can_enable"] and st["why"] == "" and st["blocks"] == []
    vm_guard.set_on("s1", False)
    vm_guard.set_on("s1", False)                                       # off twice is fine
    assert not vm_guard.is_on("s1") and vm_guard.since("s1") == 0.0
    vm_guard.mark_hooked("s1", False)
    assert vm_guard.status("s1", "claude")["why"]


def test_a_session_already_on_stays_switchable_off_without_the_hook(home):
    vm_guard.set_on("s1", True)
    st = vm_guard.status("s1", "claude")
    assert st["on"] and st["can_enable"] is True


# ---- the hook ---------------------------------------------------------------------------- #
def payload(command, tool="Bash"):
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"command": command}}


def test_the_hook_denies_with_the_reason_only_when_the_switch_is_on(home):
    assert vm_guard.hook_response(payload("make"), "s1") is None              # off
    vm_guard.set_on("s1", True)
    out = vm_guard.hook_response(payload("make test"), "s1")
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse" and spec["permissionDecision"] == "deny"
    assert "vm_exec" in spec["permissionDecisionReason"]
    assert vm_guard.hook_response(payload("git status"), "s1") is None
    assert vm_guard.hook_response(payload("make", tool="Read"), "s1") is None
    assert vm_guard.hook_response({"tool_name": "Bash", "tool_input": "x"}, "s1") is None
    assert vm_guard.hook_response({}, "s1") is None and vm_guard.hook_response(payload("make"), "other") is None


def test_blocks_are_recorded_newest_first_without_the_arguments(home):
    vm_guard.set_on("s1", True)
    for command in ("make a", "API_KEY=sekret npm install --token abc", "python x.py"):
        vm_guard.hook_response(payload(command), "s1")
    rows = vm_guard.blocks("s1")
    assert [r["rule"] for r in rows] == ["python", "npm install", "make"]
    text = json.dumps(rows)
    assert "sekret" not in text and "abc" not in text
    for i in range(30):
        vm_guard.record_block("s1", "build", "make", "make")
    assert len(vm_guard.blocks("s1")) == vm_guard.HISTORY_KEEP


def test_a_failure_in_the_hook_allows_the_call(home, monkeypatch):
    vm_guard.set_on("s1", True)
    monkeypatch.setattr(vm_guard, "check", lambda c: (_ for _ in ()).throw(RuntimeError("boom")))
    assert vm_guard.hook_response(payload("make"), "s1") is None
    monkeypatch.undo()
    monkeypatch.setattr(vm_guard, "record_block", lambda *a: (_ for _ in ()).throw(RuntimeError("disk")))
    assert vm_guard.hook_response(payload("make"), "s1") is None              # fail-open also on a history failure


def test_the_cli_adapter_prints_a_decision_or_nothing_and_always_exits_0(home, monkeypatch, capsys):
    monkeypatch.setenv("CLAUNCH_SESSION", "s1")
    vm_guard.set_on("s1", True)
    for stdin, expect in ((json.dumps(payload("make")), True), (json.dumps(payload("ls")), False),
                          ("", False), ("not json", False), ("[]", False)):
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        assert cli_vm._cmd_guard(argparse.Namespace()) == 0
        out = capsys.readouterr().out
        assert bool(out) is expect
        if expect:
            assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    monkeypatch.delenv("CLAUNCH_SESSION")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload("make"))))
    assert cli_vm._cmd_guard(argparse.Namespace()) == 0 and capsys.readouterr().out == ""


# ---- the Claude Code adapter in the spawn ------------------------------------------------------ #
def test_every_claude_spawn_carries_the_pre_tool_use_hook(home, tmp_path):
    profile.create("work")
    sdef = harness.normalize(harness.SessionDef(name="x", profile="work", cwd=str(tmp_path)))
    for restoring in (False, True):
        argv, _, _ = harness.build_command(sdef, restoring=restoring)
        assert harness.REBRIEF_HOOK_SETTINGS_JSON in argv
        settings = json.loads(argv[argv.index("--settings") + 1])
        (entry,) = settings["hooks"]["PreToolUse"]
        assert entry["matcher"] == "Bash"
        assert entry["hooks"] == [{"type": "command", "command": vm_guard.HOOK_COMMAND}]
        assert settings["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "claunch rebrief"
    steered = harness.normalize(harness.SessionDef(name="y", profile="work", cwd=str(tmp_path), args=["--settings", "{}"]))
    argv, _, _ = harness.build_command(steered, restoring=False)
    assert harness.REBRIEF_HOOK_SETTINGS_JSON not in argv


def test_the_hook_command_starts_claunch_only_when_the_switch_exists(tmp_path):
    """Run the literal hook command with a fake claunch on PATH."""
    if sys.platform == "win32":
        pytest.skip("the hook runs under sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claunch"
    fake.write_text("#!/bin/sh\necho \"guard ran: $*\"\n")
    fake.chmod(0o755)
    session = tmp_path / "sessions" / "s1"
    (session / "scratch").mkdir(parents=True)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "CLAUNCH_SCRATCH": str(session / "scratch")}

    def hook():
        return subprocess.run(["sh", "-c", vm_guard.HOOK_COMMAND], env=env, capture_output=True, text=True)
    off = hook()
    assert off.returncode == 0 and off.stdout == ""
    (session / "vm-only.json").write_text("{}")
    on = hook()
    assert on.returncode == 0 and on.stdout.strip() == "guard ran: vm guard"
    env.pop("CLAUNCH_SCRATCH")
    unset = hook()
    assert unset.returncode == 0 and unset.stdout == ""


def test_the_literal_in_harness_is_the_one_in_vm_guard():
    assert harness.VM_GUARD_COMMAND == vm_guard.HOOK_COMMAND


# ---- the routes -------------------------------------------------------------------------------------- #
class Live:
    def __init__(self, harness_name="claude"):
        self.sdef = type("S", (), {"harness": harness_name, "prompt_packs": [], "name": "x"})()

    def queue_delivery(self, *a, **k):
        return True


class Manager:
    def __init__(self, **live):
        self.live = live

    def get(self, name):
        if name not in self.live:
            raise KeyError(name)
        return self.live[name]


def test_the_user_switches_a_session_and_sees_why_it_cannot(tmp_path, host, home):
    async def go():
        async with api_for(tmp_path, host) as api:
            api.app["manager"] = Manager(s1=Live(), s2=Live("codex"), s3=Live())
            for who in ("s1", "s2", "s3"):
                api.reg.grant("a", who, ["view"])
            vm_guard.mark_hooked("s1", True)
            rows = {r["session"]: r for r in (await (await api.get("/api/vm-only")).json())["sessions"]}
            assert set(rows) == {"s1", "s2", "s3"}
            assert rows["s1"]["can_enable"] and not rows["s1"]["on"]
            assert "codex" in rows["s2"]["why"] and "hook" in rows["s3"]["why"]
            assert vm_guard.LIMITS in rows["s1"]["limits"]
            # a session cannot switch itself or anyone
            denied = await api.post("/api/vm-only", json={"session": "s1", "on": True}, headers=AGENT)
            assert denied.status == 403 and not vm_guard.is_on("s1")
            assert (await api.get("/api/vm-only", headers=AGENT)).status == 403
            ok = await api.post("/api/vm-only", json={"session": "s1", "on": True})
            got = {r["session"]: r for r in (await ok.json())["sessions"]}
            assert ok.status == 200 and got["s1"]["on"] and vm_guard.is_on("s1")
            assert any(r.get("what") == "vm_only_on" and r["target"] == "s1" for r in api.reg.audit_log)
            for who, code in (("s2", 409), ("s3", 409), ("nobody", 400)):
                resp = await api.post("/api/vm-only", json={"session": who, "on": True})
                assert resp.status == code, who
                assert not vm_guard.is_on(who)
            assert (await api.post("/api/vm-only", json={"session": "s1", "on": "yes"})).status == 400
            assert (await api.post("/api/vm-only", json={"on": True})).status == 400
            vm_guard.hook_response(payload("make"), "s1")
            shown = {r["session"]: r for r in (await (await api.get("/api/vm-only")).json())["sessions"]}
            assert shown["s1"]["blocks"][0]["rule"] == "make"
            off = await api.post("/api/vm-only", json={"session": "s1", "on": False})
            assert off.status == 200 and not vm_guard.is_on("s1")
            # off is allowed whatever the session is
            await api.post("/api/vm-only", json={"session": "ghost", "on": False})
    run(go())
