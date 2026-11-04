"""Registry PATH merged into the environment handed to a daemon (claunch-labxh).

A Windows process copies its parent's environment, so a daemon that restarts by
spawning its successor keeps the PATH of the shell that first started it. These
tests cover the merge that puts back what the registry has and the environment
lacks, on any host: the pieces are pure functions and take the registry value
as an argument.
"""

import os

from claude_launcher import win_path


def test_directories_the_registry_added_are_appended():
    merged = win_path.merge_path(
        r"C:\Windows\system32",
        r"C:\Windows\system32;C:\Program Files\GitHub CLI",
        {},
    )
    assert merged == r"C:\Windows\system32;C:\Program Files\GitHub CLI"


def test_the_environments_own_order_and_extra_entries_survive():
    venv = r"C:\repo\.venv\Scripts"
    merged = win_path.merge_path(
        venv + r";C:\Windows\system32",
        r"C:\Windows\system32;C:\Program Files\GitHub CLI",
        {},
    )
    assert merged.split(";") == [venv, r"C:\Windows\system32",
                                 r"C:\Program Files\GitHub CLI"]


def test_nothing_is_removed_when_the_registry_no_longer_has_it():
    merged = win_path.merge_path(
        r"C:\gone\bin;C:\Windows\system32",
        r"C:\Windows\system32",
        {},
    )
    assert merged == r"C:\gone\bin;C:\Windows\system32"


def test_case_and_a_trailing_separator_do_not_make_a_second_entry():
    merged = win_path.merge_path(
        r"c:\program files\github cli\;C:\Windows\system32",
        r"C:\Program Files\GitHub CLI;C:\WINDOWS\System32",
        {},
    )
    assert merged == r"c:\program files\github cli\;C:\Windows\system32"


def test_registry_variables_are_expanded():
    merged = win_path.merge_path(
        r"C:\Windows\system32",
        r"%SystemRoot%\system32;%ProgramFiles%\GitHub CLI",
        {"SystemRoot": r"C:\Windows", "ProgramFiles": r"C:\Program Files"},
    )
    # The expanded form of the first entry is the one already there, so only
    # the second is appended -- as the expanded path, not the raw one.
    assert merged == r"C:\Windows\system32;C:\Program Files\GitHub CLI"


def test_a_variable_that_is_not_in_the_environment_is_left_alone():
    merged = win_path.merge_path(
        r"C:\Windows\system32",
        r"%Nowhere%\bin",
        {"SystemRoot": r"C:\Windows"},
    )
    assert merged == r"C:\Windows\system32;%Nowhere%\bin"


def test_empty_entries_are_not_carried_over():
    merged = win_path.merge_path(";;", r"C:\bin;;C:\other", {})
    assert merged == r"C:\bin;C:\other"


def test_an_empty_current_path_takes_the_registry_whole():
    merged = win_path.merge_path("", r"C:\Windows\system32;C:\bin", {})
    assert merged == r"C:\Windows\system32;C:\bin"


def test_refresh_environment_without_a_registry_returns_an_unchanged_copy():
    env = {"PATH": r"C:\Windows\system32", "OTHER": "1"}
    out = win_path.refresh_environment(env, read_registry=lambda: None)
    assert out == env
    assert out is not env


def test_refresh_environment_merges_the_registry_path_only():
    env = {"PATH": r"C:\Windows\system32", "CLAUNCH_X": "keep"}
    out = win_path.refresh_environment(
        env, read_registry=lambda: r"C:\Windows\system32;C:\Program Files\GitHub CLI"
    )
    assert out["PATH"] == r"C:\Windows\system32;C:\Program Files\GitHub CLI"
    assert out["CLAUNCH_X"] == "keep"


def test_refresh_environment_writes_under_the_key_the_mapping_uses():
    env = {"Path": r"C:\Windows\system32"}
    out = win_path.refresh_environment(
        env, read_registry=lambda: r"C:\Program Files\GitHub CLI"
    )
    assert out == {"Path": r"C:\Windows\system32;C:\Program Files\GitHub CLI"}


def test_an_environment_without_a_path_is_left_alone():
    """A caller hands over a bare env to prove what was scrubbed away
    (``test_session_env.test_a_successors_env_is_scrubbed_too``); filling in a
    PATH would answer a question it did not ask."""
    env = {"OTHER": "1"}
    out = win_path.refresh_environment(
        env, read_registry=lambda: r"C:\Program Files\GitHub CLI"
    )
    assert out == {"OTHER": "1"}


def test_spawn_daemon_hands_the_child_what_the_registry_has(monkeypatch, tmp_path):
    """The call site itself: a daemon start carries the registry PATH, not the
    one this process inherited (claunch-labxh)."""
    from claude_launcher import daemon_client

    monkeypatch.setattr(
        win_path, "registry_path", lambda: r"C:\Program Files\GitHub CLI"
    )
    monkeypatch.setenv("PATH", r"C:\Windows\system32")
    monkeypatch.setattr(daemon_client, "daemon_cwd", lambda: tmp_path)
    log_fd = os.open(os.devnull, os.O_WRONLY)
    monkeypatch.setattr(daemon_client, "_open_log_for_child", lambda: log_fd)
    seen = {}

    def fake_popen(argv, **kwargs):
        seen.update(kwargs)
        raise OSError("not really")

    monkeypatch.setattr(daemon_client.subprocess, "Popen", fake_popen)
    try:
        daemon_client.spawn_daemon(reason="test")
    except OSError:
        pass
    assert seen["env"]["PATH"].split(";") == [
        r"C:\Windows\system32",
        r"C:\Program Files\GitHub CLI",
    ]


def test_registry_path_is_none_where_there_is_no_registry(monkeypatch):
    if os.name == "nt":  # pragma: no cover - this host is the one it reads
        return
    assert win_path.registry_path() is None
