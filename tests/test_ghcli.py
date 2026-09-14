"""The GitHub CLI card: what the daemon says about ``gh`` before a worker asks.

``improv-worker-remote``'s remote-setup step stops on three missing facts --
no ``gh`` on the daemon's PATH, no login on the host, no ``claunch.pr.remote``
in the repository -- and a stopped worker can only ask the user. ``ghcli``
answers the same three questions for the Settings page, so the user acts on
them once. These tests pin:

* the URL reader, since the host is what ``gh auth status`` is asked about;
* the repository reader on real (temporary) repositories: the configured
  remote wins, an unset key lists every GitHub remote unmarked, a local
  path remote is not a host, and a key naming a missing remote is reported
  rather than dropped;
* the composed payload with the probes injected: no gh means one install
  step and no login steps, a host not signed in means a login step, an
  unconfigured repository means a ``git config`` step, and a machine with
  nothing missing is ``ready`` with an empty guide;
* the route, which is the payload behind a bearer token and nothing else --
  in particular no token value, whatever the environment holds.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claude_launcher import ghcli


# --------------------------------------------------------------------------- #
# remote URLs
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.ncsoft.com/RnIAgentDev/claunch.git", ("github.ncsoft.com", "RnIAgentDev/claunch")),
        ("https://github.com/inosphe/claude-launcher", ("github.com", "inosphe/claude-launcher")),
        ("https://user@ghe.example.com:8443/o/r.git", ("ghe.example.com", "o/r")),
        ("git@github.ncsoft.com:RnIAgentDev/claunch.git", ("github.ncsoft.com", "RnIAgentDev/claunch")),
        ("ssh://git@GitHub.com/o/r.git", ("github.com", "o/r")),
        ("file:///F:/works/claude-launcher", (None, None)),
        ("F:\\works\\claude-launcher", (None, None)),
        ("C:/t/mp", (None, None)),
        ("/home/me/repo.git", (None, None)),
        ("../sibling", (None, None)),
        ("", (None, None)),
    ],
)
def test_parse_remote_url(url, expected):
    assert ghcli.parse_remote_url(url) == expected


# --------------------------------------------------------------------------- #
# repositories
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "remote", "add", "origin", "https://github.com/o/mirror.git")
    _git(r, "remote", "add", "ghe", "git@ghe.example.com:team/repo.git")
    _git(r, "remote", "add", "local", str(tmp_path / "elsewhere"))
    return r


def test_unset_key_lists_every_github_remote_unmarked(repo):
    rows = ghcli.repository_hosts(str(repo))
    assert [(r["remote"], r["host"], r["slug"], r["configured"]) for r in rows] == [
        ("ghe", "ghe.example.com", "team/repo", False),
        ("origin", "github.com", "o/mirror", False),
    ]
    assert all(r["base"] == "master" for r in rows)


def test_the_configured_remote_is_the_only_row(repo):
    _git(repo, "config", ghcli.REMOTE_KEY, "ghe")
    _git(repo, "config", ghcli.BASE_KEY, "main")
    rows = ghcli.repository_hosts(str(repo))
    assert len(rows) == 1
    (row,) = rows
    assert row["remote"] == "ghe" and row["host"] == "ghe.example.com"
    assert row["configured"] is True and row["base"] == "main"


def test_a_key_naming_a_missing_remote_is_reported_not_dropped(repo):
    _git(repo, "config", ghcli.REMOTE_KEY, "gone")
    (row,) = ghcli.repository_hosts(str(repo))
    assert row["configured"] is True and row["host"] is None
    assert "gone" in row["error"] and "origin" in row["error"]


def test_a_local_path_remote_is_not_a_host(repo):
    _git(repo, "config", ghcli.REMOTE_KEY, "local")
    (row,) = ghcli.repository_hosts(str(repo))
    assert row["host"] is None and row["error"]


def test_a_plain_directory_contributes_nothing(tmp_path):
    assert ghcli.repository_hosts(str(tmp_path)) == []


# --------------------------------------------------------------------------- #
# the composed answer
# --------------------------------------------------------------------------- #
def _hosts_of(table):
    return lambda path: list(table.get(path, []))


def _row(host, remote="ghe", configured=True, **over):
    d = {"host": host, "remote": remote, "slug": "t/r", "configured": configured, "base": "master"}
    d.update(over)
    return d


def test_no_gh_means_one_install_step_and_no_login_steps():
    table = {"/a": [_row("ghe.example.com")]}
    st = ghcli.status(
        [("a", "/a")], which=lambda _: None,
        auth=lambda *_: pytest.fail("auth must not run without gh"),
        hosts_of=_hosts_of(table), platform="win32",
    )
    assert st["client"] == {"installed": False, "path": None, "version": None}
    assert st["ready"] is False
    assert [h["host"] for h in st["hosts"]] == ["ghe.example.com"]
    assert st["hosts"][0]["authenticated"] is False
    assert [g["run"] for g in st["guide"]] == ["winget install --id GitHub.cli"]
    assert "Restart the daemon" in st["guide"][0]["note"]


def test_install_command_per_platform():
    assert ghcli.install_command("darwin") == "brew install gh"
    assert "install_linux" in ghcli.install_command("linux")


def _fake_gh(tmp_path: Path) -> Path:
    """A ``gh`` that answers ``--version`` the way the real one does."""
    if sys.platform == "win32":
        gh = tmp_path / "gh.cmd"
        gh.write_text("@echo gh version 2.55.0 (2024-08-20)\n@echo https://github.com/cli/cli/releases/tag/v2.55.0\n", "utf-8")
    else:
        gh = tmp_path / "gh"
        gh.write_text("#!/bin/sh\necho 'gh version 2.55.0 (2024-08-20)'\necho https://github.com/cli/cli/releases/tag/v2.55.0\n", "utf-8")
        gh.chmod(0o755)
    return gh


def test_gh_present_reads_the_version_and_asks_each_host_once(tmp_path):
    gh = _fake_gh(tmp_path)
    asked = []

    def auth(path, host):
        asked.append((path, host))
        return {"authenticated": host == "ghe.example.com", "account": "me" if host == "ghe.example.com" else None,
                "detail": "ok" if host == "ghe.example.com" else "You are not logged into any GitHub hosts"}

    table = {
        "/a": [_row("ghe.example.com")],
        "/b": [_row("ghe.example.com", remote="up"), _row("github.com", remote="origin", configured=False)],
        "/c": [],
    }
    st = ghcli.status(
        [("a", "/a"), ("b", "/b"), ("c", "/c")], which=lambda _: str(gh), auth=auth,
        hosts_of=_hosts_of(table), platform="linux",
    )
    assert st["client"]["installed"] is True and st["client"]["version"] == "2.55.0"
    assert sorted(h for _, h in asked) == ["ghe.example.com", "github.com"]
    assert [r["name"] for r in st["repositories"]] == ["a", "b"]  # c has no remote
    by = {h["host"]: h for h in st["hosts"]}
    assert by["ghe.example.com"]["authenticated"] is True and by["ghe.example.com"]["account"] == "me"
    assert by["ghe.example.com"]["repositories"] == ["a", "b"]
    assert by["github.com"]["authenticated"] is False
    runs = [g["run"] for g in st["guide"]]
    assert runs == ["gh auth login --hostname github.com"]  # b is configured (up), so no git config step
    assert st["ready"] is False


def test_an_unconfigured_repository_gets_a_git_config_step(tmp_path):
    gh = _fake_gh(tmp_path)
    table = {"/a": [_row("ghe.example.com", configured=False), _row("github.com", remote="origin", configured=False)]}
    st = ghcli.status(
        [("a", "/a")], which=lambda _: str(gh),
        auth=lambda *_: {"authenticated": True, "account": "me", "detail": "ok"},
        hosts_of=_hosts_of(table), platform="linux",
    )
    assert [g["run"] for g in st["guide"]] == [f"git config {ghcli.REMOTE_KEY} <remote name>"]
    assert "a:" in st["guide"][0]["why"]
    assert st["ready"] is False


def test_a_broken_key_is_its_own_step(tmp_path):
    gh = _fake_gh(tmp_path)
    table = {"/a": [_row(None, remote="gone", error="claunch.pr.remote = 'gone', but ...")]}
    st = ghcli.status(
        [("a", "/a")], which=lambda _: str(gh),
        auth=lambda *_: pytest.fail("no host to ask"),
        hosts_of=_hosts_of(table), platform="linux",
    )
    assert st["hosts"] == []
    assert len(st["guide"]) == 1 and st["guide"][0]["why"].startswith("a: ")
    assert st["ready"] is False


def test_nothing_missing_is_ready_with_an_empty_guide(tmp_path):
    gh = _fake_gh(tmp_path)
    table = {"/a": [_row("ghe.example.com")]}
    st = ghcli.status(
        [("a", "/a")], which=lambda _: str(gh),
        auth=lambda *_: {"authenticated": True, "account": "me", "detail": "Logged in to ghe.example.com account me"},
        hosts_of=_hosts_of(table), platform="linux",
    )
    assert st["ready"] is True and st["guide"] == []


def test_no_repository_with_a_host_is_not_ready(tmp_path):
    gh = _fake_gh(tmp_path)
    st = ghcli.status([("a", "/a")], which=lambda _: str(gh), hosts_of=_hosts_of({}), platform="linux")
    assert st["hosts"] == [] and st["repositories"] == [] and st["guide"] == []
    assert st["ready"] is False


def test_auth_status_reads_the_verdict_and_the_account(tmp_path):
    """The real command's output shape, from a script standing in for gh."""
    if sys.platform == "win32":
        gh = tmp_path / "gh.cmd"
        gh.write_text(
            "@echo off\r\nif \"%4\"==\"ghe.example.com\" (\r\n"
            "  echo ghe.example.com\r\n  echo   ^) Logged in to ghe.example.com account me ^(keyring^)\r\n  exit /b 0\r\n"
            ") else (\r\n  echo You are not logged into any GitHub hosts. To log in, run: gh auth login 1>&2\r\n  exit /b 1\r\n)\r\n",
            "utf-8",
        )
    else:
        gh = tmp_path / "gh"
        gh.write_text(
            "#!/bin/sh\nif [ \"$4\" = ghe.example.com ]; then\n"
            "  echo ghe.example.com\n  echo '  ✓ Logged in to ghe.example.com account me (keyring)'\n  exit 0\n"
            "else\n  echo 'You are not logged into any GitHub hosts. To log in, run: gh auth login' >&2\n  exit 1\nfi\n",
            "utf-8",
        )
        gh.chmod(0o755)
    ok = ghcli.auth_status(str(gh), "ghe.example.com")
    assert ok["authenticated"] is True and ok["account"] == "me"
    assert "ghe.example.com" in ok["detail"]
    no = ghcli.auth_status(str(gh), "github.com")
    assert no["authenticated"] is False and no["account"] is None
    assert "not logged in" in no["detail"]


def test_token_env_is_a_fact_not_a_value(monkeypatch):
    monkeypatch.delenv("GH_ENTERPRISE_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert ghcli.token_env_set() is False
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "ghp_secret")
    assert ghcli.token_env_set() is True


def test_daemon_repositories_lists_the_cwd_and_existing_workspaces(tmp_path, monkeypatch):
    from claude_launcher import workspaces

    here = tmp_path / "here"
    here.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(here)
    monkeypatch.setattr(
        workspaces, "list_all",
        lambda doc=None: [workspaces.Workspace("ws", str(ws)),
                          workspaces.Workspace("gone", str(tmp_path / "gone")),
                          workspaces.Workspace("dup", str(here))],
    )
    rows = ghcli.daemon_repositories()
    assert rows[0] == ("(daemon directory)", str(here.resolve()))
    assert [label for label, _ in rows] == ["(daemon directory)", "ws"]


# --------------------------------------------------------------------------- #
# the route
# --------------------------------------------------------------------------- #
def test_the_route_is_the_payload_behind_the_token(monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager
    from claude_launcher.daemon.mesh import MeshManager

    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "ghp_never_shown")
    seen = {}

    def fake_status(repositories):
        seen["repositories"] = list(repositories)
        return {"client": {"installed": False, "path": None, "version": None}, "platform": "test",
                "token_env": True, "hosts": [], "repositories": [], "guide": [], "ready": False}

    monkeypatch.setattr(ghcli, "status", fake_status)
    monkeypatch.setattr(ghcli, "daemon_repositories", lambda: [("x", "/x")])

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=False)
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=MeshManager(mgr))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/api/tools/gh")
            assert resp.status == 401
            resp = await client.get("/api/tools/gh", headers={"Authorization": "Bearer sekrit"})
            assert resp.status == 200
            body = await resp.json()
            assert body["token_env"] is True and body["ready"] is False
            assert "ghp_never_shown" not in json.dumps(body)
            assert seen["repositories"] == [("x", "/x")]
        finally:
            await client.close()

    asyncio.run(run())
