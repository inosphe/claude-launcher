"""``claunch cflow2`` CLI smoke tests."""

from __future__ import annotations

import argparse
import json
import textwrap

import pytest

from claude_launcher import cli_cflow2

DEF = textwrap.dedent("""
    cflow: 2
    name: demo-cli
    description: cli demo
    nodes:
      scan: {prompt: scan, returns: {ok: {hits: int}}, effects: [fs.read],
             executor: [session, subagent, inline]}
      work: {prompt: "work ${params.area}"}
    params:
      area: {default: src}
    body: [scan, work]
""")


@pytest.fixture
def project(tmp_path, monkeypatch):
    d = tmp_path / "proj"
    (d / ".claunch" / "workflows" / "v2").mkdir(parents=True)
    (d / ".claunch" / "workflows" / "v2" / "demo-cli.yaml").write_text(DEF, encoding="utf-8")
    (d / ".claunch" / "workflows" / "v2" / "broken.yaml").write_text("cflow: 2\nname: broken\n",
                                                                     encoding="utf-8")
    monkeypatch.chdir(d)
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    return d


def run(*argv):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    cli_cflow2.register(sub)
    args = parser.parse_args(["cflow2", *argv])
    return args.func(args)


def test_ls_marks_layer_and_errors(project, capsys):
    assert run("ls") == 0
    out = capsys.readouterr().out
    assert "demo-cli" in out and "[project]" in out
    assert "broken" in out and "invalid" in out


def test_compile_and_show(project, capsys):
    assert run("compile", "demo-cli", "--json") == 0
    ir_doc = json.loads(capsys.readouterr().out)
    assert ir_doc["ir_version"] == 1 and "scan" in ir_doc["nodes"]
    assert run("show", "demo-cli") == 0
    assert "task scan" in capsys.readouterr().out


def test_validate_prints_plans(project, capsys):
    rc = run("validate", "demo-cli", "--harness", "codex")
    out = capsys.readouterr().out
    assert "compiled" in out and "delegation points" in out
    assert "scan: executor" in out and "dispatcher session -> " in out
    assert rc in (0, 1)


def test_validate_reports_compile_error(project, capsys):
    assert run("validate", "broken") == 1
    assert "compile error" in capsys.readouterr().out


def test_caps(project, capsys):
    assert run("caps", "--harness", "claude") == 0
    out = capsys.readouterr().out
    assert "subagent.spawn" in out and "session.spawn" in out
    assert "headless" not in out


def test_start_status_journal_abort(project, capsys):
    assert run("start", "demo-cli", "--param", "area=lib", "--scope", "s1", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "running"
    assert run("status", "--scope", "s1", "--json") == 0
    st = json.loads(capsys.readouterr().out)
    assert st["run"] == payload["run"]
    assert run("start", "demo-cli", "--scope", "s1") == 1
    assert "still" in capsys.readouterr().err
    assert run("journal", "--scope", "s1", "-n", "3") == 0
    assert capsys.readouterr().out.strip()
    assert run("abort", "--scope", "s1", "--reason", "test") == 0
    assert "aborted" in capsys.readouterr().out


def test_recover_inspect_apply_and_agent_guard(project, capsys, monkeypatch):
    from claude_launcher.cflow2.store import RunStore

    assert run("start", "demo-cli", "--scope", "s1") == 0
    capsys.readouterr()
    st = RunStore(str(project), "s1")
    st.state_path.write_bytes(b"\0" * 100)
    assert run("recover", "--scope", "s1") == 0
    preview = json.loads(capsys.readouterr().out)
    assert "state.json" in preview["differences"]
    revision = str(preview["revision"])
    monkeypatch.setenv("CLAUNCH_SESSION", "agent")
    assert run("recover", "--scope", "s1", "--apply", "--revision", revision) == 1
    assert "operator action" in capsys.readouterr().err
    monkeypatch.delenv("CLAUNCH_SESSION")
    assert run("recover", "--scope", "s1", "--apply", "--revision", revision) == 0
    assert json.loads(capsys.readouterr().out)["halted"] == "storage-recovery"


COLDEF = textwrap.dedent("""
    cflow: 2
    name: demo-col
    collections:
      todo:
        kind: queue
        key: id
        item: {id: int, what: str}
        status: {values: [open, done], initial: open, settled: [done]}
        access: {push: [person, driver], set: [person], remove: [person]}
    nodes:
      work: {prompt: "todo ${col.todo}"}
    body: [work]
""")


def test_collection_commands_as_person(project, capsys, monkeypatch):
    (project / ".claunch" / "workflows" / "v2" / "demo-col.yaml").write_text(
        COLDEF, encoding="utf-8")
    assert run("start", "demo-col", "--scope", "c1") == 0
    capsys.readouterr()
    assert run("collection", "push", "todo", '{"id": 7, "what": "x"}', "--scope", "c1") == 0
    assert run("collection", "set", "todo", "7", "status=done", "--scope", "c1") == 0
    assert run("collection", "ls", "todo", "--scope", "c1", "--json") == 0
    out = capsys.readouterr().out
    view = json.loads(out[out.index("{\n"):])
    assert view["collections"]["todo"]["items"][0]["status"] == "done"
    assert run("collection", "remove", "todo", "7", "--scope", "c1") == 0
    assert run("collection", "push", "todo", '{"id": "no"}', "--scope", "c1") == 1
    assert "todo" in capsys.readouterr().err
    # the run's own session (an unmanaged run is driven by its scope name
    # here: started without $CLAUNCH_SESSION, the driver is '')
    monkeypatch.setenv("CLAUNCH_SESSION", "s9")
    assert run("start", "demo-col", "--scope", "c2") == 0
    assert run("collection", "push", "todo", '{"id": 1, "what": "y"}', "--scope", "c2") == 1
    assert "person's answer" in capsys.readouterr().err
    assert run("collection", "ls", "--scope", "c2") == 0


def test_collection_writes_are_denied_to_the_agent():
    from claude_launcher import install

    rules = set(install.GATE_DENY_RULES)
    for cmd in ("push", "set", "remove"):
        assert f"Bash(claunch cflow2 collection {cmd}:*)" in rules
        assert f"Bash(*claunch* cflow2 collection {cmd}*)" in rules
    assert not any("collection ls" in r for r in rules)


def test_pr_preflight_prints_the_facts_and_exits_by_readiness(tmp_path, capsys, monkeypatch):
    # claunch-yq80p.5: the remote-setup task reads this to choose PR or local
    from claude_launcher import prflow, local_sync
    monkeypatch.setattr(local_sync, "protected", lambda *args: False)

    ready = {"repo": True, "remote": "origin", "base": "master", "branch": "w1-x",
             "head": "a" * 40, "gh": {"installed": True, "version": "2.45.0"},
             "auth": {"ghe.example": {"authenticated": True, "account": "me"}},
             "remotes": [{"remote": "origin", "host": "ghe.example", "slug": "o/r",
                          "configured": False}],
             "blockers": []}
    monkeypatch.setattr(prflow, "preview", lambda cwd, session="": json.loads(json.dumps(ready)))
    assert run("pr-preflight", "--cwd", str(tmp_path), "--compact") == cli_cflow2.PR_READY
    doc = json.loads(capsys.readouterr().out)
    assert doc["ready"] is True and doc["repo"] == "ghe.example/o/r"
    assert doc["account"] == "me" and doc["base"] == "master" and doc["blockers"] == []

    blocked = dict(ready, blockers=["gh is not signed in to ghe.example"])
    monkeypatch.setattr(prflow, "preview", lambda cwd, session="": json.loads(json.dumps(blocked)))
    assert run("pr-preflight", "--cwd", str(tmp_path)) == cli_cflow2.PR_BLOCKED
    assert json.loads(capsys.readouterr().out)["ready"] is False

    # a remote with no GitHub host is a blocker even when preview found none
    local = dict(ready, remotes=[{"remote": "origin", "host": None, "slug": None,
                                  "configured": False}])
    monkeypatch.setattr(prflow, "preview", lambda cwd, session="": json.loads(json.dumps(local)))
    assert run("pr-preflight", "--cwd", str(tmp_path)) == cli_cflow2.PR_BLOCKED
    assert "not on a GitHub host" in json.loads(capsys.readouterr().out)["blockers"][0]


def test_pr_preflight_base_must_be_on_the_remote(tmp_path, capsys, monkeypatch):
    from claude_launcher import local_sync
    monkeypatch.setattr(local_sync, "protected", lambda *args: False)
    # claunch-yq80p.2 stage B: a stack child's PR base is its parent's
    # <parent>-stack, which the parent pushes; until then it is a blocker
    import subprocess

    from claude_launcher import prflow

    def git(*a, cwd=tmp_path / "w"):
        subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True)

    subprocess.run(["git", "init", "-q", "--bare", str(tmp_path / "r.git")], check=True)
    (tmp_path / "w").mkdir()
    git("init", "-q")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "c")
    git("remote", "add", "origin", str(tmp_path / "r.git"))
    ready = {"repo": True, "remote": "origin", "base": "master", "branch": "w1-x",
             "head": "a" * 40, "gh": {"installed": True, "version": "2.45.0"},
             "auth": {"ghe.example": {"authenticated": True, "account": "me"}},
             "remotes": [{"remote": "origin", "host": "ghe.example", "slug": "o/r",
                          "configured": False}],
             "blockers": []}
    monkeypatch.setattr(prflow, "preview", lambda cwd, session="": json.loads(json.dumps(ready)))
    wd = str(tmp_path / "w")

    assert run("pr-preflight", "--cwd", wd, "--base", "s1-stack") == cli_cflow2.PR_BLOCKED
    doc = json.loads(capsys.readouterr().out)
    assert doc["base"] == "s1-stack" and doc["base_sha"] is None
    assert "origin/s1-stack is not on the remote" in doc["blockers"][0]

    git("push", "-q", "origin", "HEAD:refs/heads/s1-stack")
    head = subprocess.run(["git", "-C", wd, "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()
    assert run("pr-preflight", "--cwd", wd, "--base", "s1-stack") == cli_cflow2.PR_READY
    doc = json.loads(capsys.readouterr().out)
    assert doc["base"] == "s1-stack" and doc["base_sha"] == head and doc["blockers"] == []

    # an unreachable remote is said as such, not as a missing branch (review Y5)
    git("remote", "set-url", "origin", str(tmp_path / "gone.git"))
    assert run("pr-preflight", "--cwd", wd, "--base", "s1-stack") == cli_cflow2.PR_BLOCKED
    doc = json.loads(capsys.readouterr().out)
    assert doc["blockers"][0].startswith("cannot read the remote 'origin'")
    git("remote", "set-url", "origin", str(tmp_path / "r.git"))
    # no remote at all: --base cannot be checked, which is a blocker too
    monkeypatch.setattr(prflow, "preview", lambda cwd, session="": json.loads(json.dumps(
        dict(ready, remote=""))))
    assert run("pr-preflight", "--cwd", wd, "--base", "s1-stack") == cli_cflow2.PR_BLOCKED
    assert "no remote to look for the base" in json.loads(capsys.readouterr().out)["blockers"][0]
    monkeypatch.setattr(prflow, "preview", lambda cwd, session="": json.loads(json.dumps(ready)))

    # without --base nothing is asked of the remote
    assert run("pr-preflight", "--cwd", wd) == cli_cflow2.PR_READY
    doc = json.loads(capsys.readouterr().out)
    assert doc["base"] == "master" and doc["base_sha"] is None


MOVE_DEF = textwrap.dedent("""
    cflow: 2
    name: mv-cli
    nodes:
      intake: {prompt: I, force_target: true}
      work: {prompt: W}
    body: [intake, work]
""")


def test_move_answers_the_drivers_request(project, capsys):
    """claunch-kgfr5: 'move --approve|--deny' answers flow_request_move;
    a bare 'move' without a node or a decision says what it needs."""
    from claude_launcher.cflow2 import runtime

    (project / ".claunch" / "workflows" / "v2" / "mv-cli.yaml").write_text(
        MOVE_DEF, encoding="utf-8")
    assert run("start", "mv-cli") == 0
    st = runtime.status(cwd=str(project))
    runtime.report(st["tasks"][0]["instance"], cwd=str(project))  # intake -> work
    capsys.readouterr()
    assert run("move", "--approve") == 1
    assert "no pending move request" in capsys.readouterr().err
    assert run("move") == 1
    assert "--approve / --deny" in capsys.readouterr().err
    runtime.request_move("intake", "redo", by="s1", cwd=str(project))
    assert run("move", "intake", "--deny") == 1
    assert "names its node already" in capsys.readouterr().err
    assert run("move", "--deny", "--reason", "not now") == 0
    assert "refused the move to intake" in capsys.readouterr().out
    runtime.request_move("intake", "redo, really", by="s1", cwd=str(project))
    assert run("move", "--approve") == 0
    assert "moved to intake" in capsys.readouterr().out
    assert [t["label"] for t in runtime.status(cwd=str(project))["tasks"]] == ["intake"]


VERIFY_DEF = textwrap.dedent("""
    cflow: 2
    name: verify-cli
    nodes:
      chk: {prompt: chk, verify: [sh, -c, "exit 0"]}
      next: {prompt: next}
    body: [chk, next]
""")


def test_a_cli_report_waits_for_the_verify_it_started(project, capsys, monkeypatch):
    """s214 (claunch-vwmoj): 'claunch cflow2 report' started the task's verify
    in a thread of the CLI process and exited, so the verify died with it and
    the task sat in 'verifying'. The command now waits for it."""
    from claude_launcher.cflow2 import runtime

    (project / ".claunch" / "workflows" / "v2" / "verify-cli.yaml").write_text(
        VERIFY_DEF, encoding="utf-8")
    assert run("start", "verify-cli", "--scope", "other") == 0
    assert run("report", "chk", "ok", "--scope", "other", "--reason", "by hand") == 0
    out = capsys.readouterr().out
    assert "verifying chk" in out and "task next@" in out
    st = runtime.status(cwd=str(project), scope="other", tick=False)
    assert [t["label"] for t in st["tasks"]] == ["next"] and not st.get("verifying")


def test_cli_approve_grants_a_report_request(project, capsys):
    from claude_launcher.cflow2 import runtime

    (project / ".claunch" / "workflows" / "v2" / "verify-cli.yaml").write_text(
        VERIFY_DEF, encoding="utf-8")
    assert run("start", "verify-cli", "--scope", "other") == 0
    runtime.request_move("chk", "only a person may report this", by="other", outcome="ok",
                         cwd=str(project), scope="other")
    assert run("move", "--approve", "--scope", "other") == 0
    out = capsys.readouterr().out
    assert "reported chk as ok" in out
    st = runtime.status(cwd=str(project), scope="other", tick=False)
    assert [t["label"] for t in st["tasks"]] == ["next"]
