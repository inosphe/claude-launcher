"""Connect requests and the wait for their answer, from an MCP session.

A session that has no edge to a peer had no way to ask for one except to
send and be refused. Three tools close that: ``join`` (become a member),
``connect_request`` (file the ask on purpose) and ``await_connect`` (hold the
call until the approver answers, with no deadline). The approver is told by
the same ``decide`` notice a refused send files, and answers with the verbs
it already had: ``connect`` grants, ``wire_requests --decline`` refuses.

The daemon side is tested on a real :class:`MeshManager` with a stand-in
session manager, the way :mod:`tests.test_mesh_wire` does it.
"""

from __future__ import annotations

import pytest

from claude_launcher import mesh_mcp
from claude_launcher.daemon import wire
from claude_launcher.daemon.manager import ManagerError
from claude_launcher.daemon.mesh import MeshError, MeshManager, Member


class _Def:
    def __init__(self, name, parent=None):
        self.name = name
        self.parent = parent
        self.role = None


class _Session:
    def __init__(self, sdef):
        self.sdef = sdef
        self.exited = False


class _Manager:
    def __init__(self):
        self._defs = {}

    def add(self, name, *, parent=None):
        self._defs[name] = _Session(_Def(name, parent=parent))

    def get(self, name):
        try:
            return self._defs[name]
        except KeyError:
            raise ManagerError(f"no session named {name!r}") from None

    def ancestors(self, name):
        out, cur = [], self._defs[name].sdef.parent if name in self._defs else None
        while cur and cur in self._defs and cur not in out:
            out.append(cur)
            cur = self._defs[cur].sdef.parent
        return out

    def commands(self, actor, target):
        return bool(actor) and actor != target and actor in self.ancestors(target)

    def live_children(self, name):
        return [n for n, s in self._defs.items() if s.sdef.parent == name and not s.exited]

    exit_hooks: list = []

    def take_retired_for_sweep(self):
        return []


def _mesh_with_pair():
    """A lead with two workers, both members, neither connected to the other."""
    mgr = _Manager()
    mgr.add("lead")
    mgr.add("s1", parent="lead")
    mgr.add("s2", parent="lead")
    mm = MeshManager(mgr, settle=0.01)
    mm.create("m")
    mesh = mm.get("m")
    for handle, session, role in (("lead", "lead", "leader"),
                                  ("w1", "s1", "worker"),
                                  ("w2", "s2", "worker")):
        mesh.members[handle] = Member(handle, session, role=role, wired=True)
    mesh.member_edges[mesh.member_key("lead", "w1")] = True
    mesh.member_edges[mesh.member_key("lead", "w2")] = True
    return mm, mesh


# --------------------------------------------------------------------------- #
# daemon: the ask, its standing, and the answers
# --------------------------------------------------------------------------- #
def test_a_connect_request_files_an_open_ask_the_approver_is_told_of(home):
    mm, mesh = _mesh_with_pair()

    status = mm.request_connect("m", "w1", "w2")

    assert status["connected"] is False
    assert status["request"]["state"] == wire.OPEN
    assert status["request"]["by"] == "w1"
    notices = [m for m in mesh.messages if m["from"] == "policy"]
    assert len(notices) == 1 and notices[0]["type"] == "decide"


def test_a_pair_already_connected_needs_no_request(home):
    mm, mesh = _mesh_with_pair()
    mesh.member_edges[mesh.member_key("w1", "w2")] = True

    status = mm.request_connect("m", "w1", "w2")

    assert status == {"a": "w1", "b": "w2", "connected": True, "request": None}


def test_connecting_the_pair_is_what_grants_the_request(home):
    mm, mesh = _mesh_with_pair()
    mm.request_connect("m", "w1", "w2")

    mesh.member_edges[mesh.member_key("w1", "w2")] = True
    status = mm.connect_status("m", "w1", "w2")

    assert status["connected"] is True
    assert status["request"] is not None


def test_a_decline_is_the_answer_a_waiting_asker_sees_with_its_reason(home):
    mm, mesh = _mesh_with_pair()
    mm.request_connect("m", "w1", "w2")

    mm.decline_wire_request("m", "w1", "w2", actor="lead", reason="use the file lock")
    status = mm.connect_status("m", "w1", "w2")

    assert status["connected"] is False
    assert status["request"]["state"] == wire.DECLINED
    assert status["request"]["reason"] == "use the file lock"


def test_a_non_member_cannot_file_a_connect_request(home):
    mm, mesh = _mesh_with_pair()
    with pytest.raises(MeshError):
        mm.request_connect("m", "ghost", "w2")


def test_a_connect_request_to_oneself_is_refused(home):
    mm, mesh = _mesh_with_pair()
    with pytest.raises(MeshError):
        mm.request_connect("m", "w1", "w1")


# --------------------------------------------------------------------------- #
# MCP: the tools and the wait
# --------------------------------------------------------------------------- #
class _FakeClient:
    """Routes GETs by path; a path whose value is a list is answered in order,
    the last answer repeating once the list is spent."""

    def __init__(self, gets):
        self.gets = gets
        self.posts = []

    def get(self, path):
        answer = self.gets[path]
        if isinstance(answer, list):
            return answer.pop(0) if len(answer) > 1 else answer[0]
        return answer

    def post(self, path, body):
        self.posts.append((path, body))
        return {"posted": True}


ROSTER = {"members": [{"handle": "w1", "session": "s1"}]}
STATUS = "/api/mesh/m/connect-status?a=w1&b=w2"


@pytest.fixture
def as_s1(monkeypatch):
    monkeypatch.setenv("CLAUNCH_SESSION", "s1")
    monkeypatch.setattr(mesh_mcp.time, "sleep", lambda s: None)


def test_the_join_connect_and_wait_tools_are_offered():
    names = {t["name"]: t for t in mesh_mcp.TOOLS}
    assert {"join", "connect_request", "await_connect"} <= names.keys()
    assert names["join"]["inputSchema"]["required"] == ["mesh"]
    for tool in ("connect_request", "await_connect"):
        assert names[tool]["inputSchema"]["required"] == ["mesh", "peer"]


def test_connect_request_files_the_ask_as_this_session_s_handle(as_s1, monkeypatch):
    fake = _FakeClient({"/api/mesh/m": ROSTER})
    monkeypatch.setattr(mesh_mcp, "_client", lambda: fake)

    mesh_mcp.call_tool("connect_request", {"mesh": "m", "peer": "w2"})

    assert fake.posts == [("/api/mesh/m/wire-requests", {"requester": "w1", "target": "w2"})]


def test_join_sends_this_session_as_the_member(as_s1, monkeypatch):
    fake = _FakeClient({})
    monkeypatch.setattr(mesh_mcp, "_client", lambda: fake)

    mesh_mcp.call_tool("join", {"mesh": "m", "role": "worker"})

    path, body = fake.posts[0]
    assert path == "/api/mesh/m/join"
    assert body["session"] == "s1" and body["role"] == "worker"


def test_await_connect_holds_through_any_number_of_waits_then_returns_the_grant(as_s1, monkeypatch):
    """No deadline: the wait survives far more polls than a timeout would allow."""
    open_row = {"state": wire.OPEN}
    pending = {"a": "w1", "b": "w2", "connected": False, "request": open_row}
    granted = {"a": "w1", "b": "w2", "connected": True, "request": {"state": wire.GRANTED}}
    fake = _FakeClient({
        "/api/mesh/m": ROSTER,
        STATUS: [pending] * 500 + [granted],
    })
    monkeypatch.setattr(mesh_mcp, "_client", lambda: fake)
    sleeps = []
    monkeypatch.setattr(mesh_mcp.time, "sleep", lambda s: sleeps.append(s))

    result = mesh_mcp.call_tool("await_connect", {"mesh": "m", "peer": "w2"})

    assert result["outcome"] == "connected"
    assert len(sleeps) == 500
    assert all(s == mesh_mcp.CONNECT_POLL_SECONDS for s in sleeps)


def test_await_connect_returns_the_decline_with_its_reason(as_s1, monkeypatch):
    declined = {
        "a": "w1", "b": "w2", "connected": False,
        "request": {"state": wire.DECLINED, "reason": "use the file lock"},
    }
    fake = _FakeClient({"/api/mesh/m": ROSTER, STATUS: declined})
    monkeypatch.setattr(mesh_mcp, "_client", lambda: fake)

    result = mesh_mcp.call_tool("await_connect", {"mesh": "m", "peer": "w2"})

    assert result["outcome"] == "declined"
    assert result["request"]["reason"] == "use the file lock"


def test_await_connect_reports_a_granted_edge_that_was_cut_since(as_s1, monkeypatch):
    cut = {"a": "w1", "b": "w2", "connected": False, "request": {"state": wire.GRANTED}}
    fake = _FakeClient({"/api/mesh/m": ROSTER, STATUS: cut})
    monkeypatch.setattr(mesh_mcp, "_client", lambda: fake)

    assert mesh_mcp.call_tool("await_connect", {"mesh": "m", "peer": "w2"})["outcome"] == "cut"


def test_await_connect_refuses_to_wait_on_a_request_nobody_filed(as_s1, monkeypatch):
    nothing = {"a": "w1", "b": "w2", "connected": False, "request": None}
    fake = _FakeClient({"/api/mesh/m": ROSTER, STATUS: nothing})
    monkeypatch.setattr(mesh_mcp, "_client", lambda: fake)

    with pytest.raises(mesh_mcp.MeshMcpError) as exc:
        mesh_mcp.call_tool("await_connect", {"mesh": "m", "peer": "w2"})
    assert "connect_request" in str(exc.value)


def test_a_session_outside_the_mesh_is_told_to_join_first(monkeypatch):
    monkeypatch.setenv("CLAUNCH_SESSION", "s238")
    fake = _FakeClient({"/api/mesh/m": ROSTER})
    monkeypatch.setattr(mesh_mcp, "_client", lambda: fake)

    with pytest.raises(mesh_mcp.MeshMcpError) as exc:
        mesh_mcp.call_tool("connect_request", {"mesh": "m", "peer": "w2"})
    assert "join it first" in str(exc.value)
    assert fake.posts == []
