"""A failed tool call leaves a line in the session's ``mcp-server.log``
(claunch-f7osr): a refusal as one line, a crash with its traceback before the
process exits."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from claude_launcher import mcp_rpc
from claude_launcher.daemon import paths


class _Refuse(Exception):
    pass


def _dispatch(name: str, args: dict) -> dict:
    if name == "refuse":
        raise _Refuse("nothing to report here")
    return {"ok": True}


SERVER = mcp_rpc.Server(
    name="test",
    tools=({"name": "refuse"}, {"name": "fine"}),
    dispatch=_dispatch,
    errors=(_Refuse,),
)


def _call(tool: str, args: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": tool, "arguments": args}}


def _lines(path: Path) -> list:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_refused_call_is_logged_in_the_session_directory(monkeypatch):
    monkeypatch.setenv("CLAUNCH_SESSION", "s-refuse")
    response = SERVER.handle(_call("refuse", {"node": "work@3"}))
    assert response["result"]["isError"] is True

    log = paths.session_dir("s-refuse") / mcp_rpc.LOG_NAME
    [entry] = _lines(log)
    assert entry["kind"] == "refused"
    assert entry["tool"] == "refuse"
    assert entry["method"] == "tools/call"
    assert entry["session"] == "s-refuse"
    assert entry["server"] == "test"
    assert entry["message"] == "nothing to report here"
    assert json.loads(entry["args"]) == {"node": "work@3"}
    assert entry["pid"] == os.getpid()
    assert "traceback" not in entry


def test_a_successful_call_writes_nothing(monkeypatch):
    monkeypatch.setenv("CLAUNCH_SESSION", "s-fine")
    SERVER.handle(_call("fine", {}))
    assert not (paths.session_dir("s-fine") / mcp_rpc.LOG_NAME).exists()


def test_no_session_means_no_log_file(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    assert mcp_rpc.log_path() is None
    assert mcp_rpc.record("refused", server="test", method="tools/call") is None


def test_long_arguments_are_cut(monkeypatch):
    monkeypatch.setenv("CLAUNCH_SESSION", "s-long")
    SERVER.handle(_call("refuse", {"body": "x" * 5000}))
    [entry] = _lines(paths.session_dir("s-long") / mcp_rpc.LOG_NAME)
    assert len(entry["args"]) < mcp_rpc.LOG_ARGS_CHARS + 40
    assert entry["args"].endswith("chars cut)")


def test_the_log_rotates_at_its_size_limit(monkeypatch):
    monkeypatch.setenv("CLAUNCH_SESSION", "s-rotate")
    monkeypatch.setattr(mcp_rpc, "LOG_MAX_BYTES", 2000)
    for _ in range(30):
        SERVER.handle(_call("refuse", {"n": 1}))
    log = paths.session_dir("s-rotate") / mcp_rpc.LOG_NAME
    old = log.with_name(log.name + ".1")
    assert old.exists()
    assert log.stat().st_size <= 2000
    assert old.stat().st_size <= 2000
    assert not log.with_name(log.name + ".2").exists()
    assert all(e["kind"] == "refused" for e in _lines(log) + _lines(old))


def test_a_logging_failure_does_not_change_the_call(monkeypatch, tmp_path):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("")
    monkeypatch.setenv("CLAUNCH_SESSION", "s-broken")
    monkeypatch.setattr(mcp_rpc, "log_path", lambda: blocker / "mcp-server.log")
    response = SERVER.handle(_call("refuse", {}))
    assert response["result"]["isError"] is True


_CRASHING_SERVER = textwrap.dedent("""
    from claude_launcher import mcp_rpc

    class Refuse(Exception):
        pass

    def dispatch(name, args):
        raise RuntimeError("kaboom from " + name)

    mcp_rpc.Server(name="crashy", tools=({"name": "explode"},),
                   dispatch=dispatch, errors=(Refuse,)).serve()
""")


def _src_dir() -> str:
    return str(Path(mcp_rpc.__file__).resolve().parents[1])


def _run_child(script: str, stdin: bytes, session: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CLAUNCH_SESSION"] = session
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (_src_dir(), env.get("PYTHONPATH", "")) if p)
    return subprocess.run([sys.executable, "-c", script], input=stdin,
                          capture_output=True, env=env, timeout=60)


def test_a_crash_inside_a_tool_call_is_logged_before_the_process_exits():
    msg = _call("explode", {"workflow": "improv-worker-v2"})
    proc = _run_child(_CRASHING_SERVER, json.dumps(msg).encode() + b"\n", "s-crash")
    assert proc.returncode == 1, proc.stderr.decode()

    log = paths.session_dir("s-crash") / mcp_rpc.LOG_NAME
    [entry] = _lines(log)
    assert entry["kind"] == "crash"
    assert entry["tool"] == "explode"
    assert entry["server"] == "crashy"
    assert entry["session"] == "s-crash"
    assert entry["message"] == "RuntimeError: kaboom from explode"
    assert json.loads(entry["args"]) == {"workflow": "improv-worker-v2"}
    assert "Traceback (most recent call last)" in entry["traceback"]
    assert "kaboom from explode" in entry["traceback"]
    assert entry["pid"] != os.getpid()
    # stderr still carries the traceback, and names the log.
    err = proc.stderr.decode()
    assert "kaboom from explode" in err
    assert f"crash recorded in {log}" in err


_INLINE_CRASH = textwrap.dedent("""
    from claude_launcher import mcp_rpc

    def boom(self, msg):
        raise ValueError("bad " + str(msg.get("method")))

    mcp_rpc.Server.handle = boom
    mcp_rpc.Server(name="inline", tools=(), dispatch=lambda n, a: {},
                   errors=(KeyError,)).serve()
""")


def test_a_crash_in_an_inline_request_is_logged_too():
    msg = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    proc = _run_child(_INLINE_CRASH, json.dumps(msg).encode() + b"\n", "s-inline")
    assert proc.returncode != 0

    [entry] = _lines(paths.session_dir("s-inline") / mcp_rpc.LOG_NAME)
    assert entry["kind"] == "crash"
    assert entry["method"] == "ping"
    assert entry["tool"] is None
    assert entry["message"] == "ValueError: bad ping"
    assert "Traceback" in entry["traceback"]
