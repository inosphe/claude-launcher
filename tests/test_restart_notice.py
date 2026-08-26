"""The restart notice: what a restart owes, and why it is a debt and not a nudge.

Three contracts, and they fail in three different ways.

*Order.* The record has to be on disk before the daemon is asked to stop, or
it is not a record of anything — the turn that would have written it later is
dead. So the first test does not check that a file exists at the end; it
checks what ``daemon_client.stop`` can see at the moment it is called.

*The absence of a record is a verdict.* A boot that finds nothing waiting, with
a boot before it in the ledger, was not asked for. That reading is only sound
if every door that ends a daemon on purpose leaves a line — which is why a
plain ``daemon stop`` writes one too, and why an operator's stop-then-start is
tested here as explicitly *not* an unsolicited restart.

*A debt is not dropped.* The resume nudge next door gives up on a session that
is working again, and that is right for "carry on". It is wrong here: a
session working again is exactly the one that never learned its restart
succeeded, and it is the one that asks for another. The delivery tests hold
that line against the three ways a message normally goes away — the session is
busy, the delivery is refused, the window ends.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import types

import pytest

from claude_launcher import cli_sessions, daemon_client
from claude_launcher.daemon import restart_notice, resume
from claude_launcher.daemon.harness import SessionDef

LIVE = {"pid": 4242, "started_at": "2026-08-26T02:36:00+00:00"}


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class _Session:
    """A live session as the deliverer reads one."""

    def __init__(self, name: str, *, harness: str = "claude") -> None:
        self.sdef = SessionDef(name=name, cwd="", harness=harness)
        self.exited = False
        self.screen = types.SimpleNamespace(bracketed_paste=True)
        self.delivered: list = []
        #: One answer per poll, last value repeating — so a test writes the
        #: sequence it wants instead of racing a clock.
        self.statuses = ["idle"]
        self.deliver_answers = [True]
        self._seen = 0
        self._offered = 0

    def status(self, threshold=None) -> str:
        i = min(self._seen, len(self.statuses) - 1)
        self._seen += 1
        return self.statuses[i]

    async def deliver(self, text: str) -> bool:
        i = min(self._offered, len(self.deliver_answers) - 1)
        self._offered += 1
        if not self.deliver_answers[i]:
            return False
        self.delivered.append(text)
        return True


class _Dead:
    def __init__(self, name: str) -> None:
        self.sdef = SessionDef(name=name, cwd="")
        self.exited = True


class _Manager:
    def __init__(self, sessions: dict) -> None:
        self._sessions = sessions

    def get(self, name: str):
        if name not in self._sessions:
            raise KeyError(name)
        return self._sessions[name]


async def _drain(notice, *, timeout: float = 5.0) -> None:
    notice.start()
    if notice._task is not None:
        await asyncio.wait_for(notice._task, timeout)


@pytest.fixture
def instant(monkeypatch):
    """No startup settle: these tests own the status sequence, not the clock."""
    monkeypatch.setattr(restart_notice, "INPUT_SETTLE", 0.0)


def _boot(**kw) -> list:
    """Run one boot with sensible defaults; returns the debts it created."""
    args = {"pid": 5151, "started_at": "2026-08-26T02:38:12+00:00", "version": "0.1.0"}
    args.update(kw)
    return restart_notice.note_boot(**args)


# --------------------------------------------------------------------------- #
# the requester's half: written before anything is stopped
# --------------------------------------------------------------------------- #
def _daemon_args(action: str = "restart", **kw) -> argparse.Namespace:
    return argparse.Namespace(action=action, all=False, force=False, **kw)


def test_the_record_is_on_disk_before_the_daemon_is_stopped(home, monkeypatch):
    """The whole fix is this ordering, so the test is about the moment, not
    the outcome: what ``stop`` can see when it is called."""
    monkeypatch.setenv("CLAUNCH_SESSION", "s45")
    monkeypatch.setattr(
        "claude_launcher.daemon.runtime_state.read_daemon_json", lambda: dict(LIVE)
    )
    monkeypatch.setattr(
        daemon_client, "diagnose", lambda **kw: {"state": daemon_client.SERVING}
    )
    seen: dict = {}

    def _stop(*a, **kw):
        seen["requests"] = restart_notice.read_requests()
        return True

    monkeypatch.setattr(daemon_client, "stop", _stop)
    monkeypatch.setattr(
        daemon_client,
        "ensure_running",
        lambda **kw: types.SimpleNamespace(base_url="http://127.0.0.1:8377"),
    )
    monkeypatch.setattr(cli_sessions.time, "sleep", lambda s: None)

    assert cli_sessions._cmd_daemon(_daemon_args()) == 0

    [record] = seen["requests"]
    assert record["kind"] == restart_notice.KIND_RESTART
    assert record["requested_by"] == "s45"
    # The identity of the daemon about to die, captured while it can still be
    # read: after the stop, daemon.json is gone.
    assert record["daemon_pid"] == LIVE["pid"]
    assert record["daemon_started_at"] == LIVE["started_at"]


def test_a_stop_records_itself_too(home, monkeypatch):
    """It owes nobody a notice; it exists so the next boot has an alibi."""
    monkeypatch.setenv("CLAUNCH_SESSION", "s45")
    monkeypatch.setattr(
        "claude_launcher.daemon.runtime_state.read_daemon_json", lambda: dict(LIVE)
    )
    monkeypatch.setattr(daemon_client, "stop", lambda **kw: True)

    assert cli_sessions._cmd_daemon(_daemon_args("stop")) == 0
    [record] = restart_notice.read_requests()
    assert record["kind"] == restart_notice.KIND_STOP


def test_nothing_is_recorded_when_no_daemon_is_announced(home, monkeypatch):
    """A stop of something already gone must not excuse the boot that follows."""
    monkeypatch.setattr(
        "claude_launcher.daemon.runtime_state.read_daemon_json", lambda: None
    )
    assert restart_notice.record_request() is None
    assert restart_notice.read_requests() == []


def test_a_torn_line_costs_one_notice_not_the_boot(home):
    restart_notice.record_request(session="s45", daemon=dict(LIVE))
    path = restart_notice.requests_file()
    path.write_text(path.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")
    assert len(restart_notice.read_requests()) == 1


# --------------------------------------------------------------------------- #
# the boot's half: who is owed what
# --------------------------------------------------------------------------- #
def test_the_boot_answers_the_session_that_asked(home):
    restart_notice.record_request(session="s45", daemon=dict(LIVE))
    debts = _boot()

    [debt] = debts
    assert debt["session"] == "s45"
    assert debt["kind"] == "requested"
    # The two pids are the fact the asker can re-check for itself, so they are
    # in the text, not merely in the record.
    assert "4242" in debt["text"] and "5151" in debt["text"]
    assert "do NOT restart again" in debt["text"]
    assert restart_notice.owed("s45") == debts


def test_a_boot_nobody_asked_for_says_so(home):
    _boot(pid=1, started_at="T1")           # a machine that has run a daemon
    debts = _boot(pid=2, started_at="T2", restored=["s45", "s99"])

    assert [d["session"] for d in debts] == ["s45", "s99"]
    assert {d["kind"] for d in debts} == {"unsolicited"}
    assert "nothing asked it to" in debts[0]["text"]


def test_the_first_boot_on_a_machine_is_not_a_restart(home):
    """No boot before it, so there is no gap to explain — and a cold start
    that announced itself as an unexplained restart would be pure noise."""
    assert _boot(restored=["s45"]) == []


def test_a_deliberate_stop_then_start_is_not_unsolicited(home, monkeypatch):
    """The operator's own ``daemon stop`` ... ``daemon start`` is the false
    positive the alibi exists to prevent: nothing asked for the *restart*,
    because nobody restarted anything."""
    _boot(pid=1, started_at="T1")
    monkeypatch.setattr(
        "claude_launcher.daemon.runtime_state.read_daemon_json", lambda: dict(LIVE)
    )
    restart_notice.record_request(kind=restart_notice.KIND_STOP, session="s45")

    debts = _boot(pid=2, started_at="T2", restored=["s45"])
    assert debts == []           # neither owed an answer nor reported as stray
    assert restart_notice.owed() == []


def test_a_human_shells_restart_is_an_alibi_and_nothing_more(home):
    """No session asked, so no session is owed — the shell already has the
    command's own stdout, which is the one turn that does not die."""
    restart_notice.record_request(session=None, daemon=dict(LIVE))
    _boot(pid=1, started_at="T1")
    assert restart_notice.owed() == []

    # ...and the boot after it is still reported, because that one had no line
    assert [d["kind"] for d in _boot(pid=2, started_at="T2", restored=["s45"])] == [
        "unsolicited"
    ]


def test_one_boot_answers_a_request_once(home):
    restart_notice.record_request(session="s45", daemon=dict(LIVE))
    assert len(_boot(pid=2, started_at="T2")) == 1
    assert restart_notice.read_requests() == []      # consumed
    # The next boot has no request of its own, so it reports itself as stray
    # rather than answering the same one again.
    assert [d["kind"] for d in _boot(pid=3, started_at="T3", restored=["s45"])] == [
        "unsolicited"
    ]


def test_the_ledger_remembers_the_boot_before_this_one(home):
    """``daemon.json`` cannot answer this — shutdown removes it — which is the
    reason the ledger is append-only rather than a field added there."""
    assert restart_notice.previous_boot() is None
    _boot(pid=1, started_at="T1")
    assert restart_notice.previous_boot()["pid"] == 1
    _boot(pid=2, started_at="T2")
    assert restart_notice.previous_boot()["pid"] == 2


def test_the_ledger_stays_bounded(home):
    for i in range(restart_notice.BOOT_HISTORY + 5):
        _boot(pid=i, started_at=f"T{i}")
    boots = restart_notice.read_ledger()["boots"]
    assert len(boots) == restart_notice.BOOT_HISTORY
    assert boots[-1]["pid"] == restart_notice.BOOT_HISTORY + 4


# --------------------------------------------------------------------------- #
# delivery: the three ways a message normally goes away, and none of them apply
# --------------------------------------------------------------------------- #
def test_a_session_working_again_is_still_told(home, instant):
    """The regression this whole module exists for.

    On 2026-08-26 the leader restarted the daemon three times in ninety
    seconds because it never learned the first one worked. The log line at the
    end of that sequence is ``resume nudge for 's45' dropped: it is working
    again`` — the one message that could have stopped it, withheld on the
    grounds that it was busy. Busy is the state the loop lives in.
    """
    restart_notice.record_request(session="s45", daemon=dict(LIVE))
    debts = _boot()
    session = _Session("s45")
    session.statuses = ["idle", "busy", "busy"]
    notice = restart_notice.RestartNotice(
        _Manager({"s45": session}), debts, poll=0.01, window=5.0
    )
    asyncio.run(_drain(notice))

    assert notice.delivered == ["s45"]
    assert len(session.delivered) == 1
    assert restart_notice.owed() == []


def test_the_resume_nudge_drops_the_same_session(home, monkeypatch, instant):
    """The contrast, held in a test so the difference is a contract and not a
    comment: identical session, identical statuses, opposite outcome."""
    monkeypatch.setattr(resume, "INPUT_SETTLE", 0.0)
    monkeypatch.setattr(resume, "gate", lambda cwd, scope: True)
    session = _Session("s45")
    session.statuses = ["idle", "busy", "busy"]
    nudge = resume.ResumeNudge(
        _Manager({"s45": session}), ["s45"], poll=0.01, window=5.0
    )
    asyncio.run(_drain(nudge))

    assert nudge.delivered == []       # dropped: "it is working again"
    assert session.delivered == []


def test_a_session_that_never_goes_idle_is_told_anyway(home, monkeypatch, instant):
    """A quiet-since test is one a working session never passes, so it cannot
    be the only way through."""
    monkeypatch.setattr(restart_notice, "BUSY_GRACE", 0.0)
    restart_notice.record_request(session="s45", daemon=dict(LIVE))
    debts = _boot()
    session = _Session("s45")
    session.statuses = ["busy"]
    notice = restart_notice.RestartNotice(
        _Manager({"s45": session}), debts, poll=0.01, window=5.0
    )
    asyncio.run(_drain(notice))

    assert notice.delivered == ["s45"]


def test_a_held_delivery_is_retried_not_dropped(home, instant):
    """An unsent line in the composer holds a delivery. Held is fine — the
    debt survives it — and this is where the resume nudge's other exit
    (``deliver`` said no, give the session back to the pool) would have been
    wrong: nothing here is allowed to conclude from a refusal."""
    restart_notice.record_request(session="s45", daemon=dict(LIVE))
    debts = _boot()
    session = _Session("s45")
    session.deliver_answers = [False, False, True]
    notice = restart_notice.RestartNotice(
        _Manager({"s45": session}), debts, poll=0.01, window=5.0
    )
    asyncio.run(_drain(notice))

    assert notice.delivered == ["s45"]
    assert restart_notice.owed() == []


def test_a_window_that_ends_leaves_the_debt_on_disk(home, instant):
    """Expiry is not a drop: it stops offering, and the next boot picks it up."""
    restart_notice.record_request(session="s45", daemon=dict(LIVE))
    debts = _boot()
    session = _Session("s45")
    session.deliver_answers = [False]
    notice = restart_notice.RestartNotice(
        _Manager({"s45": session}), debts, poll=0.01, window=0.15
    )
    asyncio.run(_drain(notice))

    assert notice.delivered == []
    assert [d["session"] for d in restart_notice.owed()] == ["s45"]


def test_the_next_boot_picks_up_what_the_last_one_could_not(home, instant):
    """A ``RestartNotice`` built with no list takes everything still owed —
    which is how a debt outlives the boot that created it."""
    restart_notice.record_request(session="s45", daemon=dict(LIVE))
    _boot()
    session = _Session("s45")
    notice = restart_notice.RestartNotice(
        _Manager({"s45": session}), poll=0.01, window=5.0
    )
    asyncio.run(_drain(notice))

    assert notice.delivered == ["s45"]
    assert restart_notice.owed() == []


def test_a_delivered_debt_leaves_the_ledger_and_the_boots_stay(home, instant):
    restart_notice.record_request(session="s45", daemon=dict(LIVE))
    debts = _boot()
    notice = restart_notice.RestartNotice(
        _Manager({"s45": _Session("s45")}), debts, poll=0.01, window=5.0
    )
    asyncio.run(_drain(notice))

    ledger = json.loads(restart_notice.ledger_file().read_text(encoding="utf-8"))
    assert ledger["debts"] == []
    assert len(ledger["boots"]) == 1     # settling a debt must not lose history


def test_a_debt_with_no_terminal_left_is_retired(home, instant):
    """The one retirement there is: an exited record cannot be typed into, so
    holding its notice forever would only grow the file."""
    restart_notice.record_request(session="s45", daemon=dict(LIVE))
    debts = _boot()
    notice = restart_notice.RestartNotice(
        _Manager({"s45": _Dead("s45")}), debts, poll=0.01, window=5.0
    )
    asyncio.run(_drain(notice))

    assert notice.delivered == [] and notice.abandoned == ["s45"]
    assert restart_notice.owed() == []


def test_a_session_the_manager_never_heard_of_is_retired(home, instant):
    restart_notice.record_request(session="ghost", daemon=dict(LIVE))
    debts = _boot()
    notice = restart_notice.RestartNotice(
        _Manager({}), debts, poll=0.01, window=5.0
    )
    asyncio.run(_drain(notice))

    assert notice.abandoned == ["ghost"]
    assert restart_notice.owed() == []


def test_one_unreadable_session_does_not_strand_the_rest(home, instant):
    _boot(pid=1, started_at="T1")
    debts = _boot(pid=2, started_at="T2", restored=["bad", "good"])

    class _Exploding(_Session):
        async def deliver(self, text: str) -> bool:
            raise RuntimeError("pty gone")

    notice = restart_notice.RestartNotice(
        _Manager({"bad": _Exploding("bad"), "good": _Session("good")}),
        debts, poll=0.01, window=5.0,
    )
    asyncio.run(_drain(notice))

    assert notice.delivered == ["good"]
