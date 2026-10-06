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
             executor: [headless, subagent, inline]}
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
    assert "scan: executor" in out and "dispatcher headless -> " in out
    assert rc in (0, 1)


def test_validate_reports_compile_error(project, capsys):
    assert run("validate", "broken") == 1
    assert "compile error" in capsys.readouterr().out


def test_caps(project, capsys):
    assert run("caps", "--harness", "claude") == 0
    out = capsys.readouterr().out
    assert "subagent.spawn" in out and "headless.run" in out


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
