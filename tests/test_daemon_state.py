"""Daemon runtime state: address file, token, singleton lock, config schema."""

from __future__ import annotations

from claude_launcher import store
from claude_launcher.daemon import paths, runtime_state


def test_daemon_json_roundtrip(home):
    assert runtime_state.read_daemon_json() is None
    runtime_state.write_daemon_json("127.0.0.1", 1234)
    doc = runtime_state.read_daemon_json()
    assert doc["host"] == "127.0.0.1"
    assert doc["port"] == 1234
    assert doc["pid"]
    runtime_state.remove_daemon_json()
    assert runtime_state.read_daemon_json() is None


def test_token_created_once(home):
    t1 = runtime_state.load_or_create_token()
    t2 = runtime_state.load_or_create_token()
    assert t1 == t2
    assert len(t1) > 30
    assert paths.token_file().is_file()


def test_token_rotate_changes_value(home):
    t1 = runtime_state.load_or_create_token()
    t2 = runtime_state.rotate_token()
    assert t1 != t2
    assert runtime_state.load_or_create_token() == t2


def test_singleton_lock_excludes_second_holder(home):
    lock1 = runtime_state.SingletonLock()
    lock2 = runtime_state.SingletonLock()
    assert lock1.acquire() is True
    assert lock2.acquire() is False
    lock1.release()
    assert lock2.acquire() is True
    lock2.release()


def test_daemon_config_defaults(home):
    cfg = store.daemon_config()
    assert cfg["host"] == "127.0.0.1"
    assert cfg["port"] == 8377
    assert cfg["restore"] is True


def test_daemon_config_overrides(home):
    store.set_daemon_field("host", "0.0.0.0")
    store.set_daemon_field("port", 9000)
    cfg = store.daemon_config()
    assert cfg["host"] == "0.0.0.0"
    assert cfg["port"] == 9000
    # untouched keys keep defaults
    assert cfg["idle_threshold"] == 2.0
    store.set_daemon_field("host", None)
    assert store.daemon_config()["host"] == "127.0.0.1"
