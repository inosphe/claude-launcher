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


def test_the_requested_width_travels_to_the_daemon(monkeypatch):
    client = _Client()
    monkeypatch.setattr(test_window.daemon_client, "connect", lambda: client)
    test_window.acquire("targeted", workers=2).release()
    assert client.calls[0][1]["workers"] == 2
    client.calls.clear()
    test_window.acquire("targeted").release()
    assert "workers" not in client.calls[0][1]  # 0 = the class ceiling


def test_a_refusal_reason_reaches_the_caller(monkeypatch):
    class _Refusing(_Client):
        def post(self, path, body, **kwargs):
            return {"granted": False, "position": 1, "reason": "the targeted cap (3) is reached"}

    monkeypatch.setattr(test_window.daemon_client, "connect", lambda: _Refusing())
    with pytest.raises(test_window.WindowUnavailable, match=r"targeted cap \(3\)"):
        test_window.acquire("targeted", wait=0)


def test_targeted_fallback_holds_one_of_a_fixed_number_of_slots(monkeypatch, capsys):
    """Daemon down: targeted runs are still capped, by slot lock files."""
    monkeypatch.setattr(test_window.daemon_client, "connect", lambda: None)
    grants = [
        test_window.acquire("targeted", wait=0)
        for _ in range(test_window.FALLBACK_TARGETED_SLOTS)
    ]
    try:
        assert all(g.source == "fallback" for g in grants)
        assert "slot lock" in capsys.readouterr().err
        with pytest.raises(test_window.WindowUnavailable, match="slots are held"):
            test_window.acquire("targeted", wait=0)
    finally:
        for grant in grants:
            grant.release()
    test_window.acquire("targeted", wait=0).release()


def test_sweep_fallback_excludes_fallback_targeted_runs(monkeypatch):
    monkeypatch.setattr(test_window.daemon_client, "connect", lambda: None)
    targeted = test_window.acquire("targeted", wait=0)
    try:
        # A slot is taken, so the sweep cannot collect every slot lock.
        with pytest.raises(test_window.WindowUnavailable):
            test_window.acquire("sweep", wait=0)
    finally:
        targeted.release()
    sweep = test_window.acquire("sweep", wait=0)
    try:
        with pytest.raises(test_window.WindowUnavailable):
            test_window.acquire("targeted", wait=0)
    finally:
        sweep.release()
    # A failed sweep attempt kept nothing: the slots are all free again.
    test_window.acquire("sweep", wait=0).release()


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
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.setenv(test_window.WINDOW_MODE_ENV, "off")
    grant = test_window.acquire("sweep")
    assert grant.source == "disabled"
    assert "no concurrency guard" in capsys.readouterr().err


def test_a_managed_session_cannot_switch_the_window_off(monkeypatch, capsys):
    client = _Client()
    monkeypatch.setattr(test_window.daemon_client, "connect", lambda: client)
    monkeypatch.setenv("CLAUNCH_SESSION", "s9")
    monkeypatch.setenv(test_window.WINDOW_MODE_ENV, "off")
    grant = test_window.acquire("targeted")
    assert grant.source == "daemon"
    assert "ignored inside managed session 's9'" in capsys.readouterr().err
    grant.release()


def test_the_direct_child_is_told_to_report_into_the_grant():
    grant = test_window.WindowGrant("targeted", "g-9", 2, "daemon", 0.0, "s1")
    env = grant.child_env({})
    assert env[test_window.WINDOW_REPORT_ENV] == "1"
    assert env[test_window.WINDOW_GRANT_ENV] == "g-9"


def test_a_result_is_reported_only_for_a_daemon_grant(monkeypatch):
    client = _Client()
    client_calls = client.calls

    def post(path, body, **kwargs):
        client_calls.append((path, body, kwargs))
        return {"reported": True}

    client.post = post
    monkeypatch.setattr(test_window.daemon_client, "connect", lambda: client)
    for skipped in ("disabled", "sweep-fallback", "targeted-fallback", ""):
        assert not test_window.report_result(skipped, {"outcome": "passed"})
    assert client_calls == []
    assert test_window.report_result("g-1", {"outcome": "passed"})
    assert client_calls[0][:2] == (
        "/api/window/report", {"grant_id": "g-1", "result": {"outcome": "passed"}}
    )


def test_a_report_that_cannot_reach_the_daemon_costs_nothing(monkeypatch):
    monkeypatch.setattr(test_window.daemon_client, "connect", lambda: None)
    assert not test_window.report_result("g-1", {"outcome": "passed"})

    class _Old(_Client):
        def post(self, path, body, **kwargs):
            raise test_window.daemon_client.DaemonClientError("404")

    monkeypatch.setattr(test_window.daemon_client, "connect", lambda: _Old())
    assert not test_window.report_result("g-1", {"outcome": "passed"})


def test_a_pytest_session_maps_to_the_result_fields():
    stats = {"passed": [1, 2, 3], "failed": [1], "error": [], "skipped": [1, 2]}
    result = test_window.pytest_result(1, stats, 6, 12.34)
    assert result == {
        "outcome": "failed", "exit_code": 1, "passed": 3, "failed": 1,
        "errors": 0, "skipped": 2, "collected": 6, "duration": 12.3,
    }
    assert test_window.pytest_result(0, {}, 0, 0)["outcome"] == "passed"
    assert test_window.pytest_result(2, {}, 0, 0)["outcome"] == "interrupted"
    assert test_window.pytest_result(5, {}, 0, 0)["outcome"] == "no_tests"
    assert test_window.pytest_result(4, {}, 0, 0)["outcome"] == "error"


def test_xdist_width_is_read_and_cut_to_the_grant():
    from types import SimpleNamespace

    option = SimpleNamespace(tx=["popen"] * 16, numprocesses=16)
    assert test_window.requested_xdist_width(option) == 16
    assert test_window.clamp_xdist_width(option, 4) == (16, 4)
    assert option.tx == ["popen"] * 4 and option.numprocesses == 4
    assert test_window.clamp_xdist_width(option, 4) is None  # already within

    starred = SimpleNamespace(tx=["6*popen"])
    assert test_window.requested_xdist_width(starred) == 6
    assert test_window.clamp_xdist_width(starred, 2) == (6, 2)

    serial = SimpleNamespace(tx=[])
    assert test_window.requested_xdist_width(serial) == 1
    assert test_window.clamp_xdist_width(serial, 1) is None

    remote = SimpleNamespace(tx=["ssh=host//python=python3"] * 3)
    assert test_window.clamp_xdist_width(remote, 1) is None  # not ours to edit
