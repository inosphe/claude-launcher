"""Profile sync: the merge engine, the server's storage/auth, and both together.

The end-to-end tests run the real aiohttp app on a real socket and drive it with
the real (urllib-based) client, so the wire format is exercised rather than
mocked. Two "machines" are simulated by pointing the launcher home at two
different directories.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
import yaml

from claude_launcher import config, store, sync
from claude_launcher.sync import SyncError
from claude_launcher.syncserver.api import build_app
from claude_launcher.syncserver.docs import (
    DocStore,
    RevisionMismatch,
    SyncServerError,
)
from claude_launcher.syncserver.users import UserStore


# --------------------------------------------------------------------------- #
# three-way merge
# --------------------------------------------------------------------------- #
def test_merge_takes_each_side_s_own_change():
    base = {"profiles": {"work": {"env": {"A": "1"}}}}
    local = {"profiles": {"work": {"env": {"A": "1", "L": "local"}}}}
    remote = {"profiles": {"work": {"env": {"A": "1", "R": "remote"}}}}
    merged, conflicts = sync.three_way_merge(base, local, remote)
    assert merged == {"profiles": {"work": {"env": {"A": "1", "L": "local", "R": "remote"}}}}
    assert conflicts == []


def test_merge_propagates_a_deletion_instead_of_resurrecting_it():
    base = {"profiles": {"work": {}, "old": {}}}
    local = {"profiles": {"work": {}}}  # this machine deleted 'old'
    remote = {"profiles": {"work": {}, "old": {}}}  # the server still has it
    merged, conflicts = sync.three_way_merge(base, local, remote)
    assert merged == {"profiles": {"work": {}}}
    assert conflicts == []


def test_merge_without_a_base_unions_both_sides():
    merged, conflicts = sync.three_way_merge(
        {}, {"profiles": {"a": {}}}, {"profiles": {"b": {}}}
    )
    assert merged == {"profiles": {"a": {}, "b": {}}}
    assert conflicts == []


def test_merge_conflict_prefers_local_by_default_and_reports_it():
    base = {"provider": "one"}
    merged, conflicts = sync.three_way_merge(
        base, {"provider": "two"}, {"provider": "three"}
    )
    assert merged == {"provider": "two"}
    assert [(c.path, c.local, c.remote) for c in conflicts] == [
        ("provider", "two", "three")
    ]


def test_merge_conflict_can_prefer_remote():
    merged, conflicts = sync.three_way_merge(
        {"provider": "one"}, {"provider": "two"}, {"provider": "three"},
        prefer="remote",
    )
    assert merged == {"provider": "three"}
    assert conflicts[0].winner == "remote"


def test_merge_conflict_path_names_the_leaf():
    base = {"profiles": {"w": {"env": {"K": "0"}}}}
    _, conflicts = sync.three_way_merge(
        base,
        {"profiles": {"w": {"env": {"K": "1"}}}},
        {"profiles": {"w": {"env": {"K": "2"}}}},
    )
    assert [c.path for c in conflicts] == ["profiles.w.env.K"]


def test_merge_delete_versus_edit_is_a_conflict():
    base = {"profiles": {"w": {"env": {"K": "0"}}}}
    local = {"profiles": {"w": {"env": {}}}}  # deleted the key
    remote = {"profiles": {"w": {"env": {"K": "9"}}}}  # edited it
    merged, conflicts = sync.three_way_merge(base, local, remote)
    assert [c.path for c in conflicts] == ["profiles.w.env.K"]
    assert merged == {"profiles": {"w": {"env": {}}}}  # local (the deletion) won


def test_merge_rejects_an_unknown_preference():
    with pytest.raises(SyncError):
        sync.three_way_merge({}, {}, {}, prefer="whoever")


def test_diff_lines_describe_adds_removes_and_edits():
    lines = sync.diff_lines(
        {"profiles": {"a": {"env": {"K": "1"}}, "gone": {}}},
        {"profiles": {"a": {"env": {"K": "2"}}, "new": {}}},
    )
    assert lines == ["~ profiles.a.env.K", "- profiles.gone", "+ profiles.new"]


# --------------------------------------------------------------------------- #
# client configuration
# --------------------------------------------------------------------------- #
def test_config_needs_a_server(home):
    with pytest.raises(SyncError, match="no sync server configured"):
        sync.load_config()


def test_config_reads_the_sync_block(home):
    store.save(
        {
            "sync": {
                "url": "https://sync.example.com/",
                "namespace": "alice",
                "token": "t0ken",
            }
        }
    )
    cfg = sync.load_config()
    assert cfg.url == "https://sync.example.com"  # trailing slash trimmed
    assert cfg.namespace == "alice"
    assert cfg.sections == sync.DEFAULT_SECTIONS


def test_env_overrides_beat_the_file(home, monkeypatch):
    store.save({"sync": {"url": "https://a.example", "namespace": "a", "token": "x"}})
    monkeypatch.setenv("CLAUNCH_SYNC_URL", "https://b.example")
    monkeypatch.setenv("CLAUNCH_SYNC_TOKEN", "env-token")
    monkeypatch.setenv("CLAUNCH_SYNC_NAMESPACE", "b")
    cfg = sync.load_config()
    assert (cfg.url, cfg.namespace, cfg.token) == ("https://b.example", "b", "env-token")


def test_plain_http_to_a_remote_host_is_refused(home):
    store.save({"sync": {"url": "http://sync.example.com", "namespace": "a", "token": "x"}})
    with pytest.raises(SyncError, match="plain http"):
        sync.load_config()


def test_plain_http_is_fine_on_loopback(home):
    store.save({"sync": {"url": "http://127.0.0.1:8378", "namespace": "a", "token": "x"}})
    assert sync.load_config().url == "http://127.0.0.1:8378"


def test_plain_http_can_be_opted_into(home):
    store.save(
        {
            "sync": {
                "url": "http://sync.internal",
                "namespace": "a",
                "token": "x",
                "allow_insecure": True,
            }
        }
    )
    assert sync.load_config().url == "http://sync.internal"


def test_the_sync_block_itself_can_never_be_synced(home):
    store.save(
        {
            "sync": {
                "url": "https://s.example",
                "namespace": "a",
                "token": "x",
                "sections": ["profiles", "sync"],
            }
        }
    )
    with pytest.raises(SyncError, match="sync.sections may not include sync"):
        sync.load_config()


def test_daemon_block_is_not_synced_by_default(home):
    store.save(
        {
            "sync": {"url": "https://s.example", "namespace": "a", "token": "x"},
            "daemon": {"port": 9999},
            "profiles": {"work": {}},
        }
    )
    cfg = sync.load_config()
    subset = sync.local_subset(cfg.sections)
    assert "daemon" not in subset and "sync" not in subset
    assert subset == {"profiles": {"work": {}}}


def test_apply_subset_leaves_local_only_sections_alone(home):
    store.save(
        {
            "sync": {"url": "https://s.example", "namespace": "a", "token": "x"},
            "daemon": {"port": 9999},
            "profiles": {"old": {}},
        }
    )
    sync.apply_subset({"profiles": {"new": {}}}, ["profiles", "providers"])
    doc = store.load()
    assert doc["profiles"] == {"new": {}}
    assert doc["daemon"] == {"port": 9999}
    assert doc["sync"]["namespace"] == "a"


# --------------------------------------------------------------------------- #
# server: document storage
# --------------------------------------------------------------------------- #
def test_missing_document_reads_as_revision_zero(tmp_path):
    docs = DocStore(tmp_path)
    stored = docs.read("alice")
    assert (stored.revision, stored.doc) == (0, {})


def test_write_bumps_the_revision_and_round_trips(tmp_path):
    docs = DocStore(tmp_path)
    saved = docs.write("alice", {"profiles": {"w": {}}}, 0, by="alice")
    assert saved.revision == 1
    again = docs.read("alice")
    assert again.doc == {"profiles": {"w": {}}}
    assert again.revision == 1
    assert again.updated_by == "alice"


def test_a_stale_write_is_rejected(tmp_path):
    docs = DocStore(tmp_path)
    docs.write("alice", {"a": 1}, 0)
    docs.write("alice", {"a": 2}, 1)
    with pytest.raises(RevisionMismatch) as exc:
        docs.write("alice", {"a": 3}, 1)
    assert (exc.value.expected, exc.value.actual) == (1, 2)
    assert docs.read("alice").doc == {"a": 2}  # the loser changed nothing


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", ".", "with space", "-lead"])
def test_namespaces_cannot_escape_the_data_dir(tmp_path, bad):
    with pytest.raises(SyncServerError):
        DocStore(tmp_path).read(bad)


def test_an_unreadable_document_is_an_error_not_an_empty_one(tmp_path):
    docs = DocStore(tmp_path)
    docs.write("alice", {"a": 1}, 0)
    (tmp_path / "docs" / "alice.yaml").write_text("{[not yaml", encoding="utf-8")
    with pytest.raises(SyncServerError):
        docs.read("alice")


# --------------------------------------------------------------------------- #
# server: accounts
# --------------------------------------------------------------------------- #
def test_tokens_are_stored_hashed_and_authenticate(tmp_path):
    users = UserStore(tmp_path)
    token = users.add("alice")
    raw = (tmp_path / "users.yaml").read_text(encoding="utf-8")
    assert token not in raw
    assert users.authenticate(token).name == "alice"
    assert users.authenticate("nope") is None


def test_an_account_defaults_to_its_own_namespace(tmp_path):
    users = UserStore(tmp_path)
    users.add("alice")
    user = users.get("alice")
    assert user.namespaces == ["alice"]
    assert user.may_access("alice")
    assert not user.may_access("bob")


def test_wildcard_grants_every_namespace(tmp_path):
    users = UserStore(tmp_path)
    users.add("admin", ["*"])
    assert users.get("admin").may_access("anything")


def test_rotating_a_token_invalidates_the_old_one(tmp_path):
    users = UserStore(tmp_path)
    old = users.add("alice")
    new = users.rotate("alice")
    assert users.authenticate(old) is None
    assert users.authenticate(new).name == "alice"


def test_duplicate_users_are_refused(tmp_path):
    users = UserStore(tmp_path)
    users.add("alice")
    with pytest.raises(SyncServerError):
        users.add("alice")


# --------------------------------------------------------------------------- #
# end to end, over a real socket
# --------------------------------------------------------------------------- #
class Server:
    """The real aiohttp app on a real port, running in a background thread."""

    def __init__(self, root: Path) -> None:
        self.docs = DocStore(root)
        self.users = UserStore(root)
        self.url = ""
        self._loop = None
        self._thread = None
        self._runner = None

    def start(self) -> None:
        from aiohttp import web

        ready = threading.Event()

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop

            async def _boot():
                self._runner = web.AppRunner(build_app(self.docs, self.users))
                await self._runner.setup()
                site = web.TCPSite(self._runner, "127.0.0.1", 0, shutdown_timeout=1.0)
                await site.start()
                port = site._server.sockets[0].getsockname()[1]
                self.url = f"http://127.0.0.1:{port}"
                ready.set()

            loop.run_until_complete(_boot())
            loop.run_forever()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        assert ready.wait(10), "sync server did not start"

    def stop(self) -> None:
        if self._loop is None:
            return
        fut = asyncio.run_coroutine_threadsafe(self._runner.cleanup(), self._loop)
        try:
            fut.result(timeout=5)
        except Exception:  # noqa: BLE001 - teardown must not fail the test
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


@pytest.fixture
def server(tmp_path):
    s = Server(tmp_path / "server")
    s.start()
    try:
        yield s
    finally:
        s.stop()


def _configure(url: str, token: str, namespace: str = "alice") -> None:
    """Point the current launcher home at ``url`` (loopback, so http is allowed)."""
    store.update(
        lambda doc: doc.update(
            {"sync": {"url": url, "namespace": namespace, "token": token}}
        )
    )


def test_round_trip_push_then_pull_on_a_second_machine(server, tmp_path, monkeypatch):
    token = server.users.add("alice")

    # machine A
    a = tmp_path / "machine-a"
    a.mkdir()
    monkeypatch.setenv("CLAUDE_LAUNCHER_HOME", str(a))
    monkeypatch.setenv("CLAUDE_LAUNCHER_SYNC_FILE", str(a / ".claunch.yaml"))
    monkeypatch.setenv("CLAUDE_LAUNCHER_SEED", str(tmp_path))
    store.save({"profiles": {"work": {"env": {"K": "1"}}}})
    _configure(server.url, token)
    up = sync.run("up")
    assert up.pushed and up.revision == 1

    # machine B, same namespace
    b = tmp_path / "machine-b"
    b.mkdir()
    monkeypatch.setenv("CLAUDE_LAUNCHER_HOME", str(b))
    monkeypatch.setenv("CLAUDE_LAUNCHER_SYNC_FILE", str(b / ".claunch.yaml"))
    store.save({})
    _configure(server.url, token)
    down = sync.run("down")
    assert store.load()["profiles"] == {"work": {"env": {"K": "1"}}}
    assert down.local_changes == ["+ profiles"]
    assert not down.pushed
    # a pulled profile is materialized so it is immediately usable
    assert (b / "profiles" / "work").is_dir()


def test_merge_carries_each_machine_s_change_to_the_other(server, tmp_path, monkeypatch):
    token = server.users.add("alice")
    a, b = tmp_path / "ma", tmp_path / "mb"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("CLAUDE_LAUNCHER_SEED", str(tmp_path / "seed"))
    (tmp_path / "seed").mkdir()

    def machine(root: Path) -> None:
        monkeypatch.setenv("CLAUDE_LAUNCHER_HOME", str(root))
        monkeypatch.setenv("CLAUDE_LAUNCHER_SYNC_FILE", str(root / ".claunch.yaml"))

    # A publishes 'work'
    machine(a)
    store.save({"profiles": {"work": {}}})
    _configure(server.url, token)
    sync.run("up")

    # B pulls it, then adds 'home'
    machine(b)
    store.save({})
    _configure(server.url, token)
    sync.run("merge")
    assert set(store.load()["profiles"]) == {"work"}
    store.update(lambda doc: doc["profiles"].update({"home": {}}))
    sync.run("merge")

    # A adds 'lab' and merges: it keeps its own, gains B's, keeps 'work'
    machine(a)
    store.update(lambda doc: doc["profiles"].update({"lab": {}}))
    result = sync.run("merge")
    assert set(store.load()["profiles"]) == {"work", "home", "lab"}
    assert result.pushed

    # ...and B sees 'lab' on its next merge
    machine(b)
    sync.run("merge")
    assert set(store.load()["profiles"]) == {"work", "home", "lab"}


def test_merge_propagates_a_deletion_across_machines(server, tmp_path, monkeypatch):
    token = server.users.add("alice")
    a, b = tmp_path / "da", tmp_path / "db"
    a.mkdir()
    b.mkdir()
    (tmp_path / "seed").mkdir()
    monkeypatch.setenv("CLAUDE_LAUNCHER_SEED", str(tmp_path / "seed"))

    def machine(root: Path) -> None:
        monkeypatch.setenv("CLAUDE_LAUNCHER_HOME", str(root))
        monkeypatch.setenv("CLAUDE_LAUNCHER_SYNC_FILE", str(root / ".claunch.yaml"))

    machine(a)
    store.save({"profiles": {"work": {}, "scratch": {}}})
    _configure(server.url, token)
    sync.run("up")

    machine(b)
    store.save({})
    _configure(server.url, token)
    sync.run("merge")
    assert set(store.load()["profiles"]) == {"work", "scratch"}

    # A drops 'scratch' and pushes
    machine(a)
    store.update(lambda doc: doc["profiles"].pop("scratch"))
    sync.run("merge")

    # B, which never touched it, loses it too rather than pushing it back
    machine(b)
    sync.run("merge")
    assert set(store.load()["profiles"]) == {"work"}
    assert set(server.docs.read("alice").doc["profiles"]) == {"work"}


def test_dry_run_writes_nothing_on_either_side(server, home):
    token = server.users.add("alice")
    store.save({"profiles": {"work": {}}})
    _configure(server.url, token)
    result = sync.run("merge", dry_run=True)
    assert result.remote_changes == ["+ profiles"]
    assert server.docs.read("alice").revision == 0
    assert not sync.base_path().exists()


def test_a_stale_push_is_retried_on_top_of_the_winner(server, home, monkeypatch):
    token = server.users.add("alice")
    store.save({"profiles": {"work": {}}})
    _configure(server.url, token)
    sync.run("up")  # revision 1, base recorded

    # Someone else pushes revision 2 between our fetch and our push.
    cfg = sync.load_config()
    real_fetch = sync.SyncClient.fetch
    state = {"done": False}

    def fetch_then_meddle(self):
        remote = real_fetch(self)
        if not state["done"]:
            state["done"] = True
            server.docs.write(
                "alice", {"profiles": {"work": {}, "other": {}}}, remote.revision
            )
        return remote

    monkeypatch.setattr(sync.SyncClient, "fetch", fetch_then_meddle)
    store.update(lambda doc: doc["profiles"].update({"mine": {}}))
    result = sync.run("merge")

    assert result.retried
    assert set(server.docs.read("alice").doc["profiles"]) == {"work", "other", "mine"}
    assert set(store.load()["profiles"]) == {"work", "other", "mine"}


def test_a_bad_token_is_rejected(server, home):
    server.users.add("alice")
    _configure(server.url, "not-the-token")
    with pytest.raises(SyncError, match="check the sync token"):
        sync.run("merge")


def test_a_foreign_namespace_is_refused(server, home):
    token = server.users.add("alice")  # may only touch 'alice'
    _configure(server.url, token, namespace="bob")
    with pytest.raises(SyncError, match="no access to namespace"):
        sync.run("merge")


def test_provider_definitions_travel_with_the_profiles(server, home):
    token = server.users.add("alice")
    store.save(
        {
            "provider": "glm",
            "providers": {"glm": {"env": {"ANTHROPIC_BASE_URL": "https://x.example"}}},
            "template": {"env": {"T": "1"}},
            "profiles": {"work": {"provider": "glm"}},
        }
    )
    _configure(server.url, token)
    sync.run("up")
    stored = server.docs.read("alice").doc
    assert stored["providers"]["glm"]["env"]["ANTHROPIC_BASE_URL"] == "https://x.example"
    assert stored["provider"] == "glm"
    assert stored["template"] == {"env": {"T": "1"}}
    assert "sync" not in stored  # the link to the server never leaves the machine


def test_the_base_is_ignored_when_the_server_changes(server, home):
    token = server.users.add("alice")
    store.save({"profiles": {"work": {}}})
    _configure(server.url, token)
    sync.run("up")
    assert sync.load_base(sync.load_config()).revision == 1

    # Same file, but now pointed at a different namespace: the old base does not
    # describe it, so it must not be used as one.
    _configure(server.url, token, namespace="alice")
    store.update(lambda doc: doc["sync"].update({"namespace": "alice"}))
    cfg = sync.load_config()
    base = sync.load_base(cfg)
    assert base.revision == 1  # still the same document
    raw = yaml.safe_load(sync.base_path().read_text(encoding="utf-8"))
    assert raw["namespace"] == "alice" and raw["url"] == server.url


def test_status_output_needs_no_server(server, home, capsys):
    from claude_launcher import cli

    token = server.users.add("alice")
    store.save({"profiles": {"work": {}}})
    _configure(server.url, token)
    assert cli.main(["sync", "--status"]) == 0
    out = capsys.readouterr().out
    assert server.url in out
    assert "namespace:    alice" in out


def test_cli_points_at_prune_when_a_pull_drops_a_profile(server, home, capsys):
    """A deletion pulled from the server clears the declaration, not the directory.

    That is the launcher's standing rule (reconciliation only ever creates), so
    the CLI has to say where the leftover directory went.
    """
    from claude_launcher import cli

    token = server.users.add("alice")
    store.save({"profiles": {"work": {}, "scratch": {}}})
    _configure(server.url, token)
    assert cli.main(["sync", "--mode", "up"]) == 0
    capsys.readouterr()

    # another machine removed 'scratch'
    server.docs.write("alice", {"profiles": {"work": {}}}, 1)
    assert cli.main(["sync"]) == 0
    captured = capsys.readouterr()
    assert "- profiles.scratch" in captured.out
    assert "'scratch' is no longer declared" in captured.err
    assert "claunch prune" in captured.err
    assert (home / "profiles" / "scratch").is_dir()  # untouched, as promised


def test_a_dropped_profile_is_named_even_with_dots_in_it(server, home):
    """The hint comes from the documents, not from parsing the diff text.

    ``- profiles.a.b`` is ambiguous as a string: 'a.b' is a legal profile name.
    """
    token = server.users.add("alice")
    store.save({"profiles": {"team.work": {"env": {"K": "1"}}, "keep": {}}})
    _configure(server.url, token)
    sync.run("up")

    server.docs.write("alice", {"profiles": {"keep": {}}}, 1)
    result = sync.run("merge")
    assert result.dropped_profiles == ["team.work"]


def test_an_edited_profile_is_not_reported_as_dropped(server, home):
    token = server.users.add("alice")
    store.save({"profiles": {"work": {"env": {"K": "1"}}}})
    _configure(server.url, token)
    sync.run("up")

    server.docs.write("alice", {"profiles": {"work": {"env": {}}}}, 1)
    result = sync.run("merge")
    assert result.dropped_profiles == []
    assert result.local_changes == ["- profiles.work.env.K"]


def test_cli_flags_a_conflict_and_the_other_resolution(server, home, capsys):
    from claude_launcher import cli

    token = server.users.add("alice")
    store.save({"profiles": {"work": {"env": {"R": "eu"}}}})
    _configure(server.url, token)
    assert cli.main(["sync", "--mode", "up"]) == 0
    server.docs.write("alice", {"profiles": {"work": {"env": {"R": "us"}}}}, 1)
    store.update(lambda doc: doc["profiles"]["work"]["env"].update({"R": "apac"}))

    assert cli.main(["sync"]) == 0
    captured = capsys.readouterr()
    assert "! profiles.work.env.R" in captured.out
    assert "--prefer remote" in captured.err
    assert store.load()["profiles"]["work"]["env"]["R"] == "apac"


def test_cli_prefer_remote_lets_the_server_win(server, home, capsys):
    from claude_launcher import cli

    token = server.users.add("alice")
    store.save({"profiles": {"work": {"env": {"R": "eu"}}}})
    _configure(server.url, token)
    assert cli.main(["sync", "--mode", "up"]) == 0
    server.docs.write("alice", {"profiles": {"work": {"env": {"R": "us"}}}}, 1)
    store.update(lambda doc: doc["profiles"]["work"]["env"].update({"R": "apac"}))

    assert cli.main(["sync", "--prefer", "remote"]) == 0
    assert store.load()["profiles"]["work"]["env"]["R"] == "us"
    assert "--prefer remote" not in capsys.readouterr().err  # already did


def test_down_discards_a_local_only_edit(server, home):
    token = server.users.add("alice")
    store.save({"profiles": {"work": {"env": {"K": "1"}}}})
    _configure(server.url, token)
    sync.run("up")
    store.update(lambda doc: doc["profiles"]["work"]["env"].update({"LOCAL": "x"}))

    result = sync.run("down")
    assert store.load()["profiles"]["work"]["env"] == {"K": "1"}
    assert not result.pushed
    assert server.docs.read("alice").revision == 1  # server untouched


def test_down_from_an_empty_namespace_refuses_to_wipe_local_config(server, home):
    server.users.add("alice")
    token = server.users.rotate("alice")
    store.save({"profiles": {"work": {}, "home": {}}})
    _configure(server.url, token)

    with pytest.raises(SyncError, match="nothing to pull"):
        sync.run("down")
    assert set(store.load()["profiles"]) == {"work", "home"}


def test_up_overwrites_the_server_wholesale(server, home):
    token = server.users.add("alice")
    server.docs.write("alice", {"profiles": {"theirs": {}}}, 0)
    store.save({"profiles": {"mine": {}}})
    _configure(server.url, token)

    sync.run("up")
    assert server.docs.read("alice").doc["profiles"] == {"mine": {}}


def test_cli_sync_reports_what_it_pushed(server, home, capsys):
    from claude_launcher import cli

    token = server.users.add("alice")
    store.save({"profiles": {"work": {}}})
    _configure(server.url, token)
    assert cli.main(["sync", "--mode", "up"]) == 0
    out = capsys.readouterr().out
    assert "pushed to server" in out
    assert "+ profiles" in out
    assert cli.main(["sync"]) == 0
    assert "already in sync" in capsys.readouterr().out
