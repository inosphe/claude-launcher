"""Wire requests, and the stance a member would otherwise never read.

Two changes with one subject: what the mesh does at the moment a member
cannot reach something it needs — a peer (part A) or its own stance (part B).

The graph is built by hand here rather than through :meth:`MeshManager.join`.
Both features are decided from the session tree and the roster, neither needs
a terminal, and a real join would start PTYs for every member of every
lineage a test wants to describe. ``_Manager`` is the whole of what the code
under test asks of a session manager.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from claude_launcher.daemon import mesh as mesh_mod
from claude_launcher.daemon import mesh_roles, paths, wire
from claude_launcher.daemon.manager import ManagerError
from claude_launcher.daemon.mesh import MeshError, MeshManager, Member


# --------------------------------------------------------------------------- #
# the stand-ins
# --------------------------------------------------------------------------- #
class _Def:
    def __init__(self, name, role=None, parent=None):
        self.name = name
        self.role = role
        self.parent = parent


class _Session:
    def __init__(self, sdef):
        self.sdef = sdef
        self.exited = False


class _Manager:
    """Just enough SessionManager: a role per session and a parent chain."""

    def __init__(self):
        self._defs = {}

    def add(self, name, *, role=None, parent=None):
        self._defs[name] = _Session(_Def(name, role=role, parent=parent))
        return name

    def get(self, name):
        try:
            return self._defs[name]
        except KeyError:
            raise ManagerError(f"no session named {name!r}") from None

    def ancestors(self, name):
        out, seen = [], {name}
        cur = self._defs[name].sdef.parent if name in self._defs else None
        while cur and cur in self._defs and cur not in seen:
            out.append(cur)
            seen.add(cur)
            cur = self._defs[cur].sdef.parent
        return out

    def commands(self, actor, target):
        return bool(actor) and actor != target and actor in self.ancestors(target)

    def live_children(self, name):
        return [
            n for n, s in self._defs.items()
            if s.sdef.parent == name and not s.exited
        ]

    #: ``build_app`` appends the board's exit hook here. Present so the HTTP
    #: test can mount the real app over this stand-in.
    exit_hooks: list = []

    def take_retired_for_sweep(self):
        """build_app sweeps what restore_all retired — this stand-in retires none."""
        return []


def _mesh(mgr, name="m"):
    mm = MeshManager(mgr, settle=0.01)
    mm.create(name)
    return mm, mm.get(name)


def _member(mesh, handle, session, *, role="", wired=True, machine=""):
    """Enrol a member the way a join leaves one: wired, so an unrecorded pair
    is closed for it — which is the state every refusal here starts from."""
    m = Member(handle, session, role=role, wired=wired, machine=machine)
    mesh.members[handle] = m
    return m


def _connect(mesh, a, b, enabled=True):
    mesh.member_edges[mesh.member_key(a, b)] = enabled


def _sent(mesh, to=None):
    """Messages in the log, optionally the ones addressed to ``to``.

    Through ``addressed_to`` because the log stores the *address*, not the
    recipients it resolved to — delivery re-derives them, and so must a test
    that claims to know who was spoken to.
    """
    return [
        m for m in mesh.messages
        if to is None or mesh.addressed_to(m, to)
    ]


# --------------------------------------------------------------------------- #
# A. wire requests
# --------------------------------------------------------------------------- #
def test_a_refused_send_files_a_request_instead_of_ordering_a_relay(home):
    """The refusal stands, and stops telling the sender to route around it.

    The old sentence ended 'or route through a peer you share' — an
    instruction to relay, which is the traffic this whole path exists to
    remove. What replaces it names the session that is now holding the
    decision, and tells the sender not to send that session the content.
    """
    mgr = _Manager()
    mgr.add("lead")
    mgr.add("s1", parent="lead")
    mgr.add("s2", parent="lead")
    mm, mesh = _mesh(mgr)
    _member(mesh, "lead", "lead", role="leader")
    _member(mesh, "w1", "s1", role="worker")
    _member(mesh, "w2", "s2", role="worker")
    _connect(mesh, "lead", "w1")
    _connect(mesh, "lead", "w2")

    with pytest.raises(MeshError) as exc:
        mm._send_core(mesh, "w1", "w2", "your measurement disagrees with mine")
    text = str(exc.value)

    assert "has no connection to w2" in text          # still refused
    assert "route through a peer" not in text         # and no longer a relay
    assert "wire request for w1 <-> w2 is filed with lead" in text
    assert "must not send lead the content" in text

    req = mesh.wire_requests[wire.pair_key("w1", "w2")]
    assert (req.by, req.other, req.state, req.count) == ("w1", "w2", wire.OPEN, 1)
    assert req.approver == "lead"


def test_the_approver_is_told_once_as_a_decision_not_an_ask(home):
    """One notice, typed ``decide``.

    ``decide`` and not ``ask`` because the answer is a ``connect`` call, not a
    reply: an ``ask`` would sit in the owed ledger until the leader happened
    to say something unrelated, and close as though a decision had been made.
    """
    mgr = _Manager()
    mgr.add("lead")
    mgr.add("s1", parent="lead")
    mgr.add("s2", parent="lead")
    mm, mesh = _mesh(mgr)
    _member(mesh, "lead", "lead", role="leader")
    _member(mesh, "w1", "s1", role="worker")
    _member(mesh, "w2", "s2", role="worker")
    _connect(mesh, "lead", "w1")
    _connect(mesh, "lead", "w2")

    for _ in range(3):
        with pytest.raises(MeshError):
            mm._send_core(mesh, "w1", "w2", "ping")

    notices = [m for m in _sent(mesh, "lead") if m["from"] == "policy"]
    assert len(notices) == 1, "a retry must not spend another of the leader's turns"
    body = notices[0]["body"]
    assert notices[0]["type"] == "decide"
    assert mesh_mod.expects_reply("decide") is False
    assert "wire request: w1 tried to message w2" in body
    assert "claunch mesh connect m w1 w2" in body      # how to say yes
    assert "--decline w1 w2" in body                   # how to say no
    assert "both of them are in your subtree" in body  # why it is theirs

    # the retries were not lost, they were counted
    assert mesh.wire_requests[wire.pair_key("w1", "w2")].count == 3


def test_connecting_grants_the_request_and_tells_the_requester(home):
    """There is no approve verb: the edge IS the answer."""
    async def run():
        mgr = _Manager()
        mgr.add("lead")
        mgr.add("s1", parent="lead")
        mgr.add("s2", parent="lead")
        mm, mesh = _mesh(mgr)
        _member(mesh, "lead", "lead", role="leader")
        _member(mesh, "w1", "s1", role="worker")
        _member(mesh, "w2", "s2", role="worker")
        _connect(mesh, "lead", "w1")
        _connect(mesh, "lead", "w2")
        with pytest.raises(MeshError):
            mm._send_core(mesh, "w1", "w2", "ping")

        out = await mm.set_member_link("m", "w1", "w2", enabled=True, actor="lead")
        assert out["granted"] == {"by": "w1", "other": "w2", "asks": 1}

        req = mesh.wire_requests[wire.pair_key("w1", "w2")]
        assert req.state == wire.GRANTED and req.decided_by == "lead"
        told = [m for m in _sent(mesh, "w1") if m["from"] == "policy"]
        assert "wire request granted" in told[-1]["body"]
        assert "settle what you disagree about against the code" in told[-1]["body"]

        # and the send that was refused now goes through
        mm._send_core(mesh, "w1", "w2", "ping")
        assert _sent(mesh, "w2")[-1]["from"] == "w1"

    asyncio.run(run())


def test_a_decline_is_final_and_answers_the_next_refusal_by_itself(home):
    """Silence is what a refused agent retries against; a decline is not.

    The point of recording it is that the *refusal* can carry the answer — so
    a retry after a no costs no message at all, in either direction.
    """
    mgr = _Manager()
    mgr.add("lead")
    mgr.add("s1", parent="lead")
    mgr.add("s2", parent="lead")
    mm, mesh = _mesh(mgr)
    _member(mesh, "lead", "lead", role="leader")
    _member(mesh, "w1", "s1", role="worker")
    _member(mesh, "w2", "s2", role="worker")
    _connect(mesh, "lead", "w1")
    _connect(mesh, "lead", "w2")
    with pytest.raises(MeshError):
        mm._send_core(mesh, "w1", "w2", "ping")

    mm.decline_wire_request(
        "m", "w1", "w2", actor="lead", reason="I want your two readings independent"
    )
    told = [m for m in _sent(mesh, "w1") if m["from"] == "policy"]
    assert "wire request declined" in told[-1]["body"]
    assert "I want your two readings independent" in told[-1]["body"]

    before = len(mesh.messages)
    with pytest.raises(MeshError) as exc:
        mm._send_core(mesh, "w1", "w2", "ping again")
    assert len(mesh.messages) == before, "a declined pair must not re-notify"
    text = str(exc.value)
    assert "lead declined a channel between w1 and w2" in text
    assert "I want your two readings independent" in text
    assert "asking again does not re-ask it" in text


def test_declining_needs_the_same_authority_as_connecting(home):
    """Saying no to a pair is as much a decision about it as saying yes."""
    mgr = _Manager()
    mgr.add("lead")
    mgr.add("s1", parent="lead")
    mgr.add("s2", parent="lead")
    mgr.add("s3", parent="lead")
    mm, mesh = _mesh(mgr)
    _member(mesh, "lead", "lead", role="leader")
    _member(mesh, "w1", "s1", role="worker")
    _member(mesh, "w2", "s2", role="worker")
    _member(mesh, "w3", "s3", role="worker")
    _connect(mesh, "lead", "w1")
    _connect(mesh, "lead", "w2")
    with pytest.raises(MeshError):
        mm._send_core(mesh, "w1", "w2", "ping")

    with pytest.raises(MeshError) as exc:
        mm.decline_wire_request("m", "w1", "w2", actor="w3")
    assert "may not rewire" in str(exc.value)

    # a human (no actor) is not restricted, exactly as with connect
    assert mm.decline_wire_request("m", "w1", "w2")["state"] == wire.DECLINED


def test_the_nearest_common_ancestor_is_preferred_over_the_requesters_parent():
    """Whose decision it is, decided the way authority already runs.

    A session commanding BOTH ends is choosing how its own subtree talks to
    itself, which needs no further justification. An ancestor of the requester
    alone is the fallback that lets a request cross between two trees at all —
    and it is still inside what ``_require_member_authority`` permits.
    """
    tree = {"a1": None, "mid": "a1", "x": "mid", "y": "mid", "z": "a1"}

    def ancestors(name):
        out, cur = [], tree.get(name)
        while cur:
            out.append(cur)
            cur = tree.get(cur)
        return out

    members = {"a1": "a1", "mid": "mid"}          # x/y/z are not approvers
    handle_of = members.get

    # x and y share 'mid', which is nearer than 'a1'
    assert wire.approver(
        "x", "y", ancestors_of=ancestors, handle_of=lambda n: handle_of(n, "")
    ) == ("mid", "common")
    # x and z share only 'a1'
    assert wire.approver(
        "x", "z", ancestors_of=ancestors, handle_of=lambda n: handle_of(n, "")
    ) == ("a1", "common")
    # nobody in x's line is a member here -> nothing to ask
    assert wire.approver(
        "x", "y", ancestors_of=ancestors, handle_of=lambda n: ""
    ) == ("", "")
    # a root requester has no ancestors at all
    assert wire.approver(
        "a1", "z", ancestors_of=ancestors, handle_of=lambda n: handle_of(n, "")
    ) == ("", "")


def test_a_request_nobody_can_grant_says_so_rather_than_going_quiet(home):
    """Two different problems that look identical in a bare count."""
    mgr = _Manager()
    mgr.add("r1")
    mgr.add("r2")
    mm, mesh = _mesh(mgr)
    _member(mesh, "r1", "r1", role="worker")
    _member(mesh, "r2", "r2", role="worker")

    with pytest.raises(MeshError) as exc:
        mm._send_core(mesh, "r1", "r2", "ping")
    text = str(exc.value)
    assert "no session here commands either end, so nobody was asked" in text
    assert "claunch mesh connect m r1 r2" in text
    req = mesh.wire_requests[wire.pair_key("r1", "r2")]
    assert req.state == wire.OPEN and req.approver == ""
    assert not [m for m in mesh.messages if m["from"] == "policy"]


def test_requests_survive_a_restart(home):
    """An unanswered ask is exactly what a restart must not drop."""
    mgr = _Manager()
    mgr.add("lead")
    mgr.add("s1", parent="lead")
    mgr.add("s2", parent="lead")
    mm, mesh = _mesh(mgr)
    _member(mesh, "lead", "lead", role="leader")
    _member(mesh, "w1", "s1", role="worker")
    _member(mesh, "w2", "s2", role="worker")
    _connect(mesh, "lead", "w1")
    _connect(mesh, "lead", "w2")
    with pytest.raises(MeshError):
        mm._send_core(mesh, "w1", "w2", "ping")

    doc = json.loads(
        (paths.mesh_dir("m") / "mesh.json").read_text(encoding="utf-8")
    )
    assert doc["wire_requests"][wire.pair_key("w1", "w2")]["by"] == "w1"

    again = MeshManager(mgr, settle=0.01)
    again.load_all()
    back = again.get("m").wire_requests[wire.pair_key("w1", "w2")]
    assert (back.by, back.other, back.state, back.approver) == (
        "w1", "w2", wire.OPEN, "lead",
    )


def test_a_mesh_that_never_refuses_writes_the_file_it_always_did(home):
    """The key is absent while the table is empty, like ``member_edges``."""
    mgr = _Manager()
    mgr.add("s1")
    mm, mesh = _mesh(mgr)
    _member(mesh, "w1", "s1", role="worker")
    mm._persist_def(mesh)
    doc = json.loads(
        (paths.mesh_dir("m") / "mesh.json").read_text(encoding="utf-8")
    )
    assert "wire_requests" not in doc


def test_the_table_is_bounded_and_drops_settled_rows_first():
    """Under pressure, keep the ones somebody can still act on."""
    table = {}
    for i in range(wire.MAX_REQUESTS + 10):
        req = wire.WireRequest(a=f"a{i}", b="t", by=f"a{i}", at=float(i))
        if i < 20:                       # the oldest twenty are settled
            req.state = wire.DECLINED
            req.decided_at = float(i)
        table[req.key] = req
    wire.trim(table)
    assert len(table) == wire.MAX_REQUESTS
    # ten had to go, and all ten came off the settled end, oldest first
    gone = [i for i in range(wire.MAX_REQUESTS + 10)
            if wire.pair_key(f"a{i}", "t") not in table]
    assert gone == list(range(10))
    assert sum(r.state == wire.OPEN for r in table.values()) == wire.MAX_REQUESTS - 10


def test_a_broken_row_does_not_sink_the_mesh_it_is_in():
    """mesh.json is written by one version and read by the next."""
    loaded = wire.load(
        {
            "ok": {"a": "x", "b": "y", "by": "x", "at": 1.0, "state": "open"},
            "junk": {"a": "x"},
            "self": {"a": "x", "b": "x", "by": "x"},
            "bad-state": {"a": "p", "b": "q", "by": "p", "state": "sideways"},
            "notadict": 7,
        }
    )
    assert set(loaded) == {wire.pair_key("x", "y"), wire.pair_key("p", "q")}
    assert loaded[wire.pair_key("p", "q")].state == wire.OPEN


def test_the_http_surface_lists_and_declines(home, tmp_path):
    """The routes the CLI and the MCP tool both drive."""
    import time

    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app

    async def run():
        mgr = _Manager()
        mgr.add("lead")
        mgr.add("s1", parent="lead")
        mgr.add("s2", parent="lead")
        mm, mesh = _mesh(mgr)
        _member(mesh, "lead", "lead", role="leader")
        _member(mesh, "w1", "s1", role="worker")
        _member(mesh, "w2", "s2", role="worker")
        _connect(mesh, "lead", "w1")
        _connect(mesh, "lead", "w2")
        with pytest.raises(MeshError):
            mm._send_core(mesh, "w1", "w2", "ping")

        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        auth = {"Authorization": "Bearer sekrit"}
        try:
            resp = await client.get("/api/mesh/m/wire-requests", headers=auth)
            rows = (await resp.json())["requests"]
            assert resp.status == 200 and len(rows) == 1
            assert rows[0]["by"] == "w1" and rows[0]["approver"] == "lead"

            resp = await client.get(
                "/api/mesh/m/wire-requests?state=declined", headers=auth
            )
            assert (await resp.json())["requests"] == []

            resp = await client.post(
                "/api/mesh/m/wire-requests/decline",
                json={"a": "w1", "b": "w2", "actor": "lead", "reason": "stay apart"},
                headers=auth,
            )
            assert resp.status == 200
            assert (await resp.json())["state"] == wire.DECLINED

            resp = await client.get(
                "/api/mesh/m/wire-requests?state=declined", headers=auth
            )
            assert (await resp.json())["requests"][0]["reason"] == "stay apart"
        finally:
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# B. the stance a member would otherwise never read
# --------------------------------------------------------------------------- #
def _brief(mgr, *, session_role, member_role, roles_doc=None):
    mm, mesh = _mesh(mgr)
    if roles_doc is not None:
        mesh.roles_doc = roles_doc
    mgr.add("s1", role=session_role)
    member = _member(mesh, "w1", "s1", role=member_role)
    return mm.briefing_block(mesh, member)


@pytest.mark.parametrize("wired", [False, True])
def test_briefing_excludes_exited_and_missing_peers(home, wired):
    mgr = _Manager()
    mm, mesh = _mesh(mgr)
    me = _member(mesh, "me", mgr.add("self"), role="worker", wired=wired)
    _member(mesh, "live", mgr.add("running"), role="worker", wired=wired)
    _member(mesh, "killed", mgr.add("ended"), role="worker", wired=wired)
    _member(mesh, "missing", "removed", role="worker", wired=wired)
    mgr.get("ended").exited = True
    for handle in ("live", "killed", "missing"):
        _connect(mesh, "me", handle)

    block = mm.briefing_block(mesh, me)
    assert "members: live (worker)\n" in block
    assert "other member(s)" not in block
    assert set(mesh.members) == {"me", "live", "killed", "missing"}
    assert set(mesh.neighbours("me")) == {"live", "killed", "missing"}

    # Rebuilding the briefing after respawn must restore the peer immediately.
    mgr.get("ended").exited = False
    assert "members: killed (worker), live (worker)\n" in mm.briefing_block(mesh, me)


def test_briefing_hidden_count_excludes_ended_and_missing_sessions(home):
    mgr = _Manager()
    mm, mesh = _mesh(mgr)
    me = _member(mesh, "me", mgr.add("self"), role="worker")
    _member(mesh, "live", mgr.add("running"), role="worker")
    _member(mesh, "killed", mgr.add("ended"), role="worker")
    _member(mesh, "missing", "removed", role="worker")
    mgr.get("ended").exited = True

    block = mm.briefing_block(mesh, me)
    assert "members: (nobody else yet)\n" in block
    assert "note: 1 other member(s)" in block
    mgr.get("running").exited = True
    assert "other member(s)" not in mm.briefing_block(mesh, me)


@pytest.mark.parametrize("mirror", [False, True])
def test_briefing_keeps_remote_peers_with_unknown_liveness(home, mirror):
    mgr = _Manager()
    mm, mesh = _mesh(mgr)
    mm._machine = "pcA"
    mesh.me = "pcA"
    mesh.peers = ["pcB", "pcA"] if mirror else ["pcA", "pcB"]
    me = _member(mesh, "me", mgr.add("self"), role="worker", machine="pcA")
    _member(mesh, "far", "remote-session", role="worker", machine="pcB")
    _member(mesh, "far-hidden", "other-remote", machine="pcB")
    _connect(mesh, "me", "far")

    block = mm.briefing_block(mesh, me)
    assert "members: far (worker)\n" in block
    assert "note: 1 other member(s)" in block


def test_a_legacy_session_role_still_gets_the_common_stance_briefing(home):
    """SessionDef.role no longer selects a harness-specific delivery path."""
    block = _brief(_Manager(), session_role="worker", member_role="worker")
    assert "claunch mesh stance m" in block
    assert "stance (worker), binding [text id: " in block
    assert "You are a PRODUCER" in block


def test_a_session_with_no_role_is_given_the_prose(home):
    """Nothing else will ever put it in front of this agent.

    A pointer is one command away on a turn the agent has to decide to spend,
    and a compaction leaves nothing behind but another pointer.
    """
    block = _brief(_Manager(), session_role=None, member_role="worker")
    assert "You are a PRODUCER" in block
    assert "stance (worker), binding [text id: " in block
    assert "claunch mesh stance m" in block, "the pointer still rides along"


def test_a_replaced_vocabulary_is_pasted_because_no_prompt_can_carry_it(home):
    """Custom vocabularies use the same membership-owned briefing path."""
    doc = {
        "version": 1,
        "replace": True,
        "default": "surveyor",
        "roles": {"surveyor": {"stance": "You measure and you do not guess."}},
    }
    block = _brief(
        _Manager(), session_role=None, member_role="surveyor", roles_doc=doc
    )
    assert "You measure and you do not guess." in block
    assert "stance (surveyor), binding [text id: " in block


def test_a_legacy_record_does_not_compete_with_the_mesh_role(home):
    block = _brief(_Manager(), session_role="worker", member_role="leader")
    assert "was spawned as" not in block
    assert "stance (leader), binding [text id: " in block
    assert "You set direction and OWN the decisions" in block


def test_every_local_stance_is_named_beside_its_text(home):
    """Role delivery is identical for sessions with and without legacy data."""
    from claude_launcher import digests
    from claude_launcher.daemon import mesh_roles

    pasted = _brief(_Manager(), session_role=None, member_role="worker")
    legacy = _brief(_Manager(), session_role="worker", member_role="worker")
    ident = digests.text_digest(mesh_roles.resolve().get("worker").stance.strip())
    assert f"[text id: {ident}]" in pasted
    assert f"[text id: {ident}]" in legacy


def test_a_remote_member_is_left_to_its_own_daemon(home):
    """Unanswerable here is treated as carried, on purpose."""
    mgr = _Manager()
    mm, mesh = _mesh(mgr)
    member = _member(mesh, "far", "s9", role="worker", machine="pcB")
    mesh.peers = ["pcA", "pcB"]
    mesh.me = "pcA"
    block = mm.briefing_block(mesh, member)
    assert "claunch mesh stance m" in block
    assert "You are a PRODUCER" not in block


def test_a_long_stance_is_capped_so_the_roster_survives(home):
    """A paste is bounded by what the block it rides in can afford."""
    long = "L" * (mesh_mod._INLINE_STANCE + 500)
    doc = {
        "version": 1,
        "replace": True,
        "default": "surveyor",
        "roles": {"surveyor": {"stance": long}},
    }
    block = _brief(
        _Manager(), session_role=None, member_role="surveyor", roles_doc=doc
    )
    assert "[...]" in block
    assert len(block) < mesh_mod._INLINE_STANCE + 2000
    assert "claunch mesh stance m" in block   # the rest is one command away
    assert "you: w1 (role: surveyor)" in block, "the roster is still there"


def test_every_packaged_stance_fits_the_inline_cap_whole():
    """The cap is a guard against a custom vocabulary, not a trim of ours.

    The leader's is much the longest, and it is also the one a truncation
    would hurt most — a leader that never read its stance is the failure the
    inline paste exists for.
    """
    roles = mesh_roles.resolve().roles
    assert roles, "the packaged vocabulary must not be empty"
    for name, role in roles.items():
        assert len(role.stance.strip()) <= mesh_mod._INLINE_STANCE, name


def test_the_briefing_gives_up_the_paste_when_asked_to(home):
    """The knob rebrief turns when it runs out of budget."""
    mgr = _Manager()
    mgr.add("s1", role=None)
    mm, mesh = _mesh(mgr)
    member = _member(mesh, "w1", "s1", role="worker")
    assert "You are a PRODUCER" in mm.briefing_block(mesh, member)
    lean = mm.briefing_block(mesh, member, inline_stance=False)
    assert "You are a PRODUCER" not in lean
    assert "claunch mesh stance m" in lean
    assert "you: w1 (role: worker)" in lean


def test_rebrief_drops_the_stance_before_it_drops_the_task(home, monkeypatch):
    """Over budget, the section with an alternative is the one that yields.

    ``claunch mesh stance`` can hand the stance back on demand. Nothing can
    hand back the opening task, the owed ledger or the open decisions — and a
    blind tail-cut takes whichever section happened to be last, which is the
    task.
    """
    from claude_launcher.daemon import rebrief

    mgr = _Manager()
    mgr.add("s1", role=None)
    mm, mesh = _mesh(mgr)
    _member(mesh, "w1", "s1", role="worker")

    class _Sdef:
        name, cwd, parent = "s1", "", None
        task = "TASK-MARKER: the thing this session exists for"
        issue = "claunch-x1"

    class _Held:
        sdef = _Sdef()

    # Between the two: the pasted stance puts the block over, the pointer
    # keeps it under, which is the only interval where the choice is visible.
    member = mesh.members["w1"]
    fat = len(mm.briefing_block(mesh, member))
    lean = len(mm.briefing_block(mesh, member, inline_stance=False))
    assert lean < fat, "the paste has to be the bigger of the two to matter"
    monkeypatch.setattr(rebrief, "BLOCK_LIMIT", lean + 600)
    block = rebrief.compose(
        "s1",
        manager=type(
            "M", (), {
                "get": lambda self, n: _Held(),
                "live_children": lambda self, n: [],
            },
        )(),
        mesh_mgr=mm,
    )
    assert "TASK-MARKER" in block, "the task must survive the squeeze"
    assert "You are a PRODUCER" not in block, "the stance is what yields"
    assert "claunch mesh stance m" in block, "and it is still one command away"
