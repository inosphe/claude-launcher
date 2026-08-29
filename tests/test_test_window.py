from __future__ import annotations

import pytest

from claude_launcher import test_window


@pytest.fixture(autouse=True)
def no_parent_window_grant(monkeypatch):
    monkeypatch.delenv(test_window.WINDOW_GRANT_ENV, raising=False)
    monkeypatch.delenv(test_window.WINDOW_CLASS_ENV, raising=False)
    monkeypatch.delenv(test_window.WINDOW_WORKERS_ENV, raising=False)


class _Client:
    def __init__(self):
        self.calls = []

    def post(self, path, body, **kwargs):
        self.calls.append((path, body, kwargs))
        if path.endswith("/acquire"):
            return {"granted": True, "grant_id": "g-1", "advisory_n": 6}
        return {"released": 1}


def test_daemon_grant_is_passed_to_the_child_and_released(monkeypatch):
    client = _Client()
    monkeypatch.setattr(test_window.daemon_client, "connect", lambda: client)
    monkeypatch.setenv("CLAUNCH_SESSION", "s9")

    grant = test_window.acquire("targeted", label="pytest tests/test_x.py")
    assert grant.advisory_n == 6
    assert grant.receipt()["source"] == "daemon"
    assert grant.child_env()[test_window.WINDOW_GRANT_ENV] == "g-1"
    grant.release()

    assert client.calls[0][0] == "/api/window/acquire"
    assert client.calls[0][1]["session"] == "s9"
    assert client.calls[-1][0] == "/api/window/release"


def test_an_inherited_grant_does_not_contact_the_daemon(monkeypatch):
    monkeypatch.setenv(test_window.WINDOW_GRANT_ENV, "parent")
    monkeypatch.setenv(test_window.WINDOW_CLASS_ENV, "sweep")
    monkeypatch.setenv(test_window.WINDOW_WORKERS_ENV, "7")
    monkeypatch.setattr(
        test_window.daemon_client,
        "connect",
        lambda: (_ for _ in ()).throw(AssertionError("daemon contacted")),
    )

    grant = test_window.acquire("sweep")
    assert grant.source == "inherited"
    assert grant.advisory_n == 7
    grant.release()


def test_a_targeted_parent_grant_cannot_cover_a_sweep(monkeypatch):
    monkeypatch.setenv(test_window.WINDOW_GRANT_ENV, "parent")
    monkeypatch.setenv(test_window.WINDOW_CLASS_ENV, "targeted")
    with pytest.raises(test_window.WindowUnavailable, match="exclusive sweep"):
        test_window.acquire("sweep")


def test_targeted_fallback_is_explicit_and_nonblocking(monkeypatch, capsys):
    monkeypatch.setattr(test_window.daemon_client, "connect", lambda: None)
    grant = test_window.acquire("targeted")
    assert grant.source == "fallback"
    assert "without its capacity limit" in capsys.readouterr().err


def test_sweep_fallback_is_an_os_lock(monkeypatch):
    monkeypatch.setattr(test_window.daemon_client, "connect", lambda: None)
    first = test_window.acquire("sweep", wait=0)
    try:
        with pytest.raises(test_window.WindowUnavailable):
            test_window.acquire("sweep", wait=0)
    finally:
        first.release()
    again = test_window.acquire("sweep", wait=0)
    again.release()


def test_operator_override_is_reported(monkeypatch, capsys):
    monkeypatch.setenv(test_window.WINDOW_MODE_ENV, "off")
    grant = test_window.acquire("sweep")
    assert grant.source == "disabled"
    assert "no concurrency guard" in capsys.readouterr().err
