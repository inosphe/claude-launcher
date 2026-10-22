"""No session's identity in the daemon's environment (claunch-2yiij).

A daemon spawned from s609's shell (``ensure_running`` there) kept
``CLAUNCH_SESSION=s609`` -- and s609's codex and Herdr variables -- through
every successor, acted under that name, and handed the rest on to the sessions
it spawned. The removal is pinned at the three points: the spawn, the daemon's
boot, and the environment the daemon gives a session, whose own identity is
then set from its definition.
"""

from __future__ import annotations

import pytest

from claude_launcher import config, daemon_client, profile as profile_mod, session_env
from claude_launcher.daemon import __main__ as daemon_main
from claude_launcher.daemon import harness as harness_mod, instance_manifest, paths
from claude_launcher.daemon.harness import SessionDef

#: What s609's shell exported (the live daemon's /proc environ, 2026-10-15).
S609 = {
    "CLAUNCH_SESSION": "s609",
    "CLAUNCH_PROJECT": "claunch",
    "CLAUNCH_SCRATCH": "/x/daemon/sessions/s609/scratch",
    "CLAUNCH_PROC_OWNER": "s609",
    "CODEX_SESSION_ID": "01a13b94",
    "CODEX_THREAD_ID": "01a13b94",
    "CODEX_CI": "1",
    "HERDR_ENV": "1",
    "HERDR_PANE_ID": "w3:p1",
    "HERDR_TAB_ID": "w3:t1",
    "HERDR_WORKSPACE_ID": "w3",
    "CLAUDECODE": "1",
    "CLAUDE_CODE_SESSION_ID": "ffa99b01",
}


def _pollute(monkeypatch, extra=None):
    for name, value in {**S609, **(extra or {})}.items():
        monkeypatch.setenv(name, value)


def test_scrub_removes_every_session_variable_and_keeps_the_rest():
    env = {**S609, "PATH": "/bin", "HOME": "/h", "CLAUNCH_LAUNCHER": "/repo",
           "CLAUDE_LAUNCHER_HOME": "/h/.claude-launcher"}
    out = session_env.scrub(env)
    assert out == {"PATH": "/bin", "HOME": "/h", "CLAUNCH_LAUNCHER": "/repo",
                   "CLAUDE_LAUNCHER_HOME": "/h/.claude-launcher"}
    assert env["CLAUNCH_SESSION"] == "s609"  # a copy; the input is untouched


def test_a_profile_home_goes_only_when_it_is_a_sessions(home, tmp_path):
    own = str(tmp_path / "my-claude")
    # An operator's own CLAUDE_CONFIG_DIR, in a shell that is no session: kept.
    assert session_env.scrub({"CLAUDE_CONFIG_DIR": own}) == {"CLAUDE_CONFIG_DIR": own}
    # The same variable in a session's shell is that session's profile.
    assert session_env.scrub({"CLAUNCH_SESSION": "s1", "CLAUDE_CONFIG_DIR": own,
                              "CODEX_HOME": own}) == {}
    # A value inside claunch's profiles directory is only ever the harness's.
    inside = str(config.profiles_dir() / "codex" / "codex")
    assert session_env.scrub({"CODEX_HOME": inside}) == {}


def test_spawn_daemon_hands_the_child_no_session_identity(monkeypatch):
    seen = {}

    def fake_popen(argv, **kw):
        seen.update(kw)
        raise OSError("not really")

    _pollute(monkeypatch)
    monkeypatch.setenv("KEEP_ME", "1")
    monkeypatch.setattr(daemon_client.subprocess, "Popen", fake_popen)
    with pytest.raises(OSError):
        daemon_client.spawn_daemon(reason="test")
    assert not set(S609) & set(seen["env"])
    assert seen["env"]["KEEP_ME"] == "1"


def test_a_successors_env_is_scrubbed_too(monkeypatch):
    """The successor of a polluted daemon is spawned with an explicit env
    (``_successor_env``): the removal applies to it as well."""
    seen = {}

    def fake_popen(argv, **kw):
        seen.update(kw)
        raise OSError("not really")

    monkeypatch.setattr(daemon_client.subprocess, "Popen", fake_popen)
    with pytest.raises(OSError):
        daemon_client.spawn_daemon({**S609, "X": "1"}, reason="successor after restart")
    assert seen["env"] == {"X": "1"}


class _Booted(Exception):
    pass


def test_the_daemon_removes_them_from_its_own_environment_at_boot(monkeypatch):
    """A daemon not started through spawn_daemon (by hand, or by a
    predecessor on older code) still drops them before anything runs."""
    import os

    _pollute(monkeypatch)
    monkeypatch.setattr(instance_manifest, "apply", lambda: None)
    monkeypatch.setattr(instance_manifest, "pinned_port", lambda: None)
    seen = {}

    def booted(_foreground):
        seen["env"] = dict(os.environ)
        raise _Booted

    monkeypatch.setattr(daemon_main, "_setup_logging", booted)
    with pytest.raises(_Booted):
        daemon_main.main([])
    assert not set(S609) & set(seen["env"])


def test_a_session_gets_its_own_identity_and_none_of_the_daemons(home, tmp_path, monkeypatch):
    """The boot removal must not take away what the daemon sets for a
    session: its CLAUNCH_SESSION / PROJECT / SCRATCH come from its definition,
    after the removal; the polluting session's codex and Herdr identity do not
    reach it."""
    _pollute(monkeypatch)
    profile_mod.create("work")
    sdef = harness_mod.normalize(SessionDef(name="s700", profile="work", cwd=str(tmp_path)))
    _, env, _ = harness_mod.build_command(sdef)
    assert env["CLAUNCH_SESSION"] == "s700"
    assert env["CLAUNCH_SCRATCH"] == str(paths.session_scratch_dir("s700"))
    assert env["CLAUNCH_PROJECT"]
    for name in ("CODEX_SESSION_ID", "CODEX_THREAD_ID", "CODEX_CI", "HERDR_ENV",
                 "HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID", "CLAUDECODE",
                 "CLAUDE_CODE_SESSION_ID", "CLAUNCH_PROC_OWNER"):
        assert name not in env, name


def test_a_session_still_gets_a_variable_its_definition_sets(home, tmp_path, monkeypatch):
    """``--env`` is applied after the removal: a session that asks for one
    of these names explicitly gets it."""
    _pollute(monkeypatch)
    profile_mod.create("work")
    sdef = harness_mod.normalize(SessionDef(name="s701", profile="work", cwd=str(tmp_path),
                                            env={"HERDR_PANE_ID": "w9:p9"}))
    _, env, _ = harness_mod.build_command(sdef)
    assert env["HERDR_PANE_ID"] == "w9:p9"


#: What s1388's shell (a session on the routing shim) exported; the daemon it
#: started kept the Claude line through every successor (claunch-19liw.1).
S1388_HEADERS = {
    "ANTHROPIC_CUSTOM_HEADERS": "X-Claunch-Session: s1388",
    "CLAUNCH_PI_HEADERS": '{"X-Claunch-Session":"s1388"}',
}


def test_the_header_names_match_their_owners():
    from claude_launcher import metering, pi_provider

    assert session_env.CLAUDE_HEADERS_ENV == metering.CUSTOM_HEADERS_ENV
    assert session_env.PI_HEADERS_ENV == pi_provider.ENV_HEADERS
    assert session_env.SESSION_HEADER == metering.SESSION_HEADER


def test_scrub_drops_a_header_variable_left_with_only_the_session_line():
    assert session_env.scrub({**S1388_HEADERS, "PATH": "/bin"}) == {"PATH": "/bin"}


def test_scrub_keeps_the_users_own_headers():
    env = {
        "ANTHROPIC_CUSTOM_HEADERS": "X-Team: core\nx-claunch-session: s1388\nX-Trace: 1",
        "CLAUNCH_PI_HEADERS": '{"x-claunch-session":"s1388","X-Team":"core"}',
    }
    out = session_env.scrub(env)
    assert out["ANTHROPIC_CUSTOM_HEADERS"] == "X-Team: core\nX-Trace: 1"
    assert out["CLAUNCH_PI_HEADERS"] == '{"X-Team":"core"}'


def test_scrub_leaves_header_variables_without_a_session_line_untouched():
    env = {"ANTHROPIC_CUSTOM_HEADERS": "X-Team: core", "CLAUNCH_PI_HEADERS": "not json"}
    assert session_env.scrub(env) == env


def test_scrub_in_place_reports_a_rewritten_header_variable():
    env = {"ANTHROPIC_CUSTOM_HEADERS": "X-Team: core\nX-Claunch-Session: s1388"}
    assert session_env.scrub_in_place(env) == ["ANTHROPIC_CUSTOM_HEADERS"]
    assert env == {"ANTHROPIC_CUSTOM_HEADERS": "X-Team: core"}


def test_spawn_daemon_hands_the_child_no_session_header(monkeypatch):
    seen = {}

    def fake_popen(argv, **kw):
        seen.update(kw)
        raise OSError("not really")

    _pollute(monkeypatch, S1388_HEADERS)
    monkeypatch.setattr(daemon_client.subprocess, "Popen", fake_popen)
    with pytest.raises(OSError):
        daemon_client.spawn_daemon(reason="test")
    assert not set(S1388_HEADERS) & set(seen["env"])


def test_the_daemon_drops_the_session_header_at_boot(monkeypatch):
    import os

    _pollute(monkeypatch, S1388_HEADERS)
    monkeypatch.setattr(instance_manifest, "apply", lambda: None)
    monkeypatch.setattr(instance_manifest, "pinned_port", lambda: None)
    seen = {}

    def booted(_foreground):
        seen["env"] = dict(os.environ)
        raise _Booted

    monkeypatch.setattr(daemon_main, "_setup_logging", booted)
    with pytest.raises(_Booted):
        daemon_main.main([])
    assert not set(S1388_HEADERS) & set(seen["env"])


def test_a_session_off_the_shim_gets_no_inherited_session_header(home, tmp_path, monkeypatch):
    """The daemon adds the header only for a session on the shim; one that is
    not must not report under the name left in the daemon's environment."""
    _pollute(monkeypatch, S1388_HEADERS)
    profile_mod.create("work")
    sdef = harness_mod.normalize(SessionDef(name="s1406", profile="work", cwd=str(tmp_path)))
    _, env, _ = harness_mod.build_command(sdef)
    assert "s1388" not in env.get("ANTHROPIC_CUSTOM_HEADERS", "")
    assert "s1388" not in env.get("CLAUNCH_PI_HEADERS", "")
