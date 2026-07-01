"""Cover `run --add-prompt`: flag extraction, editor collection, injection."""

from __future__ import annotations

from claude_launcher import cli, prompt_input, runner


def test_extract_add_prompt_pulls_flag():
    found, rest = cli._extract_add_prompt(["--add-prompt", "--resume"])
    assert found is True
    assert rest == ["--resume"]


def test_extract_add_prompt_absent():
    found, rest = cli._extract_add_prompt(["--resume", "-p", "hi"])
    assert found is False
    assert rest == ["--resume", "-p", "hi"]


def test_extract_add_prompt_stops_at_separator():
    # A literal --add-prompt after `--` is forwarded to claude untouched.
    found, rest = cli._extract_add_prompt(["--", "--add-prompt"])
    assert found is False
    assert rest == ["--", "--add-prompt"]


def test_strip_instructions_keeps_markdown_headings():
    text = (
        "# My heading\n"
        "be concise\n"
        "\n"
        f"{prompt_input._SCISSORS}\n"
        "# these instruction lines are dropped\n"
    )
    assert prompt_input._strip_instructions(text) == "# My heading\nbe concise"


def test_collect_returns_body(monkeypatch):
    # Simulate the editor by writing a body into the temp file it is handed.
    def fake_run(cmd, *a, **k):
        path = cmd[-1]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("focus on tests\n" + prompt_input._INSTRUCTIONS)
        return type("C", (), {"returncode": 0})()

    monkeypatch.setattr(prompt_input.subprocess, "run", fake_run)
    assert prompt_input.collect() == "focus on tests"


def test_run_injects_append_system_prompt(home, monkeypatch, capsys):
    run = lambda *a: cli.main(list(a))
    run("create", "work", "--no-seed")
    run("set-token", "work", "sk-ant-oat01-X")
    capsys.readouterr()

    monkeypatch.setattr(prompt_input, "collect", lambda *a, **k: "review carefully")
    captured = {}

    def fake_spawn(profile, args, *, with_token, borrow=None):
        captured["args"] = list(args)
        return 0

    monkeypatch.setattr(runner, "_spawn", fake_spawn)
    assert run("run", "work", "--add-prompt", "--resume") == 0
    assert captured["args"] == [
        "--append-system-prompt",
        "review carefully",
        "--resume",
    ]


def test_run_empty_prompt_skips_injection(home, monkeypatch, capsys):
    run = lambda *a: cli.main(list(a))
    run("create", "work", "--no-seed")
    run("set-token", "work", "sk-ant-oat01-X")
    capsys.readouterr()

    monkeypatch.setattr(prompt_input, "collect", lambda *a, **k: "")
    captured = {}

    def fake_spawn(profile, args, *, with_token, borrow=None):
        captured["args"] = list(args)
        return 0

    monkeypatch.setattr(runner, "_spawn", fake_spawn)
    assert run("run", "work", "--add-prompt") == 0
    assert captured["args"] == []
    assert "no prompt entered" in capsys.readouterr().err
