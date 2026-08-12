"""Per-mesh role sets: scenario-derived tests.

Scenario matrix, derived from docs/mesh-design.md "Roles":

A. The vocabulary
   A1 the packaged set carries interconnect's roles AND their aliases, so a
      handle like `coder1` resolves to worker instead of a dead label
   A2 an unlabelled handle defaults to reviewer (audit, never rubber-stamp)
   A3 an explicit --role is normalised through the aliases; an unknown one
      is refused rather than silently stored

B. Overriding
   B1 a role named in an upload replaces that role whole; roles left
      unmentioned keep their packaged definition
   B2 `<name>: null` deletes a packaged role
   B3 `replace: true` makes the upload the entire vocabulary
   B4 a name or alias may only ever mean one role
   B5 a bad upload is refused whole — nothing half-applies
   B6 uploading resets: the override can be cleared back to the default

C. Not retroactive
   C1 an upload never rewrites a member that already joined
   C2 members who join AFTER it resolve through the new vocabulary
   C3 a member holding a role the vocabulary dropped keeps it, is reported
      as an orphan, and simply matches no rule

D. Federation
   D1 the authority owns the vocabulary; a peer's upload is forwarded to it
      and converges back onto every mirror
   D2 a joining guest gets the vocabulary with its grant, not one sync later
   D3 the role set rides a sync only when the peer is behind on it — never
      on the back of an ordinary message flush

E. Behaviour keyed off the role
   E1 stall warnings go to the roles the vocabulary marks stall_watch, not
      to a hardcoded "leader"
   E2 task-poll text comes from the role set, and the mesh's own policy
      body still wins over it

F. Persistence
   F1 an override survives a reload; a mesh without one writes no roles key

G. The wiring rules (`auto_link`), which ride the same document
   G1 the packaged set connects roots to roots and nothing else
   G2 a rule is an unordered pair predicate — either orientation matches
   G3 `within: tree` confines a rule to one spawn tree
   G4 a rule naming a role the vocabulary does not define is refused — the
      payoff for keeping the wiring in the document that defines the roles
   G5 a malformed rule is refused with the offending rule named
   G6 auto_link replaces wholesale, and survives `replace: true` (which is
      about the vocabulary, not the wiring)
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from claude_launcher import store
from claude_launcher.daemon import mesh_policy, mesh_roles
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshError, MeshManager, PeerUnreachable

from test_mesh_graph import _dispatch_peer, _register_py_harness, _wire


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


def _yaml(text: str) -> dict:
    return mesh_roles.parse(text)


# --------------------------------------------------------------------------- #
# A. the vocabulary
# --------------------------------------------------------------------------- #
def test_packaged_vocabulary_resolves_the_handles_people_actually_use():
    rs = mesh_roles.resolve()

    # A1: aliases. The old table had none, so a fleet named coder1..coder5 —
    # exactly what interconnect channels are named — landed every member on
    # "member", a role no policy targeted.
    assert rs.infer("coder1") == "worker"
    assert rs.infer("dev_2") == "worker"
    assert rs.infer("engineer-b") == "worker"
    assert rs.infer("mod") == "leader"
    assert rs.infer("moderator") == "leader"
    assert rs.infer("qa3") == "reviewer"
    assert rs.infer("gatekeeper") == "specialist"
    assert rs.infer("op1") == "operator"

    # A2: the default is an auditing stance, not an inert label.
    assert rs.default == "reviewer"
    assert rs.infer("alice") == "reviewer"

    # A3: an explicit role is normalised, and a wrong one is refused.
    assert rs.resolve("w1", "mod") == "leader"
    assert rs.resolve("w1", "WORKER") == "worker"
    with pytest.raises(mesh_roles.RoleError):
        rs.resolve("w1", "boss")

    # Every packaged role says what it is; that text is the whole point.
    assert all(r.stance for r in rs.roles.values())


# --------------------------------------------------------------------------- #
# B. overriding
# --------------------------------------------------------------------------- #
def test_an_upload_replaces_roles_one_at_a_time():
    # B1: worker is replaced whole (its packaged aliases go with it); every
    # other packaged role is untouched.
    rs = mesh_roles.resolve(_yaml("""
        roles:
          worker:
            aliases: [hacker]
            stance: build the thing
    """))
    assert sorted(rs.roles) == [
        "leader", "operator", "reviewer", "specialist", "worker"
    ]
    assert rs.infer("hacker2") == "worker"
    assert rs.get("worker").stance == "build the thing"
    assert rs.infer("coder1") == "reviewer"     # the packaged alias went away
    assert rs.get("leader").stall_watch is True  # untouched role kept whole
    assert rs.get("leader").stance

    # B2: a tombstone deletes.
    rs = mesh_roles.resolve(_yaml("roles: {specialist: null, operator: null}"))
    assert sorted(rs.roles) == ["leader", "reviewer", "worker"]

    # B3: replace:true is the whole vocabulary.
    rs = mesh_roles.resolve(_yaml("""
        replace: true
        default: hand
        roles:
          hand: {aliases: [h], stance: do it}
          boss: {stall_watch: true, stance: decide it}
    """))
    assert sorted(rs.roles) == ["boss", "hand"]
    assert rs.default == "hand" and rs.stall_watchers() == ["boss"]
    assert rs.infer("h7") == "hand" and rs.infer("nobody") == "hand"


def test_a_bad_upload_is_refused_whole():
    # B4: one word, one role — otherwise a handle's meaning depends on dict
    # order, which is exactly the kind of bug nobody finds. The message names
    # the usual cause: a document meant as a whole vocabulary that merged.
    with pytest.raises(mesh_roles.RoleError, match="resolves to both") as exc:
        mesh_roles.resolve(_yaml("roles: {mycoder: {aliases: [coder]}}"))
    assert "replace: true" in str(exc.value)
    # ...and does NOT suggest it when the author already said so.
    with pytest.raises(mesh_roles.RoleError, match="resolves to both") as exc:
        mesh_roles.resolve(_yaml(
            "replace: true\ndefault: a\nroles: {a: {aliases: [x]}, c: {aliases: [x]}}"
        ))
    assert "replace: true" not in str(exc.value)

    # A replacing set with no default used to fail as "default role '' is not
    # defined", which describes the symptom rather than the mistake.
    with pytest.raises(mesh_roles.RoleError, match="must name its 'default:'"):
        mesh_roles.resolve(_yaml("replace: true\nroles: {a: {}}"))
    # Tombstoning the role that WAS the default is caught too.
    with pytest.raises(mesh_roles.RoleError, match="not defined"):
        mesh_roles.resolve(_yaml("roles: {reviewer: null}"))

    # B5: every other way to get it wrong, refused at parse/resolve rather
    # than half-applied.
    for bad, match in [
        ("roles: {worker: {stanze: x}}", "unknown key"),
        ("version: 99", "unsupported"),
        ("default: nope", "not defined"),
        ("stances: {}", "unknown top-level"),
        ("roles: {'bad name!': {}}", "invalid role name"),
        ("", "empty"),
        ("- a\n- b", "must be a mapping"),
    ]:
        with pytest.raises(mesh_roles.RoleError, match=match):
            mesh_roles.resolve(mesh_roles.parse(bad))

    # Caps are enforced, so a role set can never grow past what a sync should
    # carry.
    with pytest.raises(mesh_roles.RoleError, match="over the"):
        mesh_roles.parse({"roles": {"w": {"stance": "x" * (mesh_roles.MAX_STANCE + 1)}}})

    # A document that arrives unreadable (older/newer daemon, hand-edited
    # mesh.json) is dropped with a warning, not raised — the mesh keeps
    # working on the packaged vocabulary.
    assert mesh_roles.load_override({"roles": {"w": {"stanze": 1}}}) is None
    assert mesh_roles.load_override(None) is None


def test_the_yaml_view_round_trips_back_into_an_upload():
    doc = _yaml("roles: {worker: {aliases: [hacker], stance: build it}}")
    again = mesh_roles.parse(mesh_roles.to_yaml(doc))
    assert again == doc
    # And the packaged set is expressible as a document too, which is what
    # `mesh roles --yaml` prints for a mesh that has no override yet.
    rendered = mesh_roles.to_yaml(mesh_roles.resolve().to_doc())
    assert mesh_roles.resolve(mesh_roles.parse(rendered)).roles.keys() == \
        mesh_roles.resolve().roles.keys()


# --------------------------------------------------------------------------- #
# C. not retroactive
# --------------------------------------------------------------------------- #
def test_uploading_a_role_set_does_not_rewrite_the_members_already_there(
    home, tmp_path
):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        for s in ("s1", "s2"):
            mgr.create(SessionDef(name=s, harness="py", cwd=str(tmp_path)))
        mm.create("m")
        await mm.join("m", "s1", handle="coder1")
        mesh = mm.get("m")
        assert mesh.members["coder1"].role == "worker"

        # Swap the vocabulary out from under a running mesh.
        await mm.set_roles("m", """
            replace: true
            default: crew
            roles:
              crew: {aliases: [coder], stance: crew stance}
              boss: {stall_watch: true, stance: boss stance}
        """)

        # C1: the member that was already there is untouched.
        assert mesh.members["coder1"].role == "worker"

        # C2: a member joining now resolves through the NEW vocabulary.
        await mm.join("m", "s2", handle="coder2")
        assert mesh.members["coder2"].role == "crew"

        # C3: the old role is reported as an orphan and matches no rule —
        # which needs no code at all, and is why this stays simple.
        view = mm.roles_view("m")
        assert view["orphans"] == ["worker"]
        assert [r["name"] for r in view["roles"]] == ["boss", "crew"]
        assert mesh.roleset.get("worker") is None
        assert mesh.roleset.stall_watchers() == ["boss"]

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# D. federation
# --------------------------------------------------------------------------- #
def test_the_authority_owns_the_vocabulary_and_a_peer_upload_is_forwarded(
    home, tmp_path
):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mms = {
            name: MeshManager(mgr, settle=0.05, root=tmp_path / ("mesh" + name))
            for name in ("pcA", "pcB")
        }
        _wire(mms)
        for s in ("sa", "sb"):
            mgr.create(SessionDef(name=s, harness="py", cwd=str(tmp_path)))
        mms["pcA"].create("m")
        await mms["pcA"].join("m", "sa", handle="alice")
        await mms["pcB"].join(
            "m@pcA", "sb", handle="bob", code=mms["pcA"].invite("m")["code"]
        )
        a, b = mms["pcA"].get("m"), mms["pcB"].get("m")

        # D2: the grant carried the vocabulary, so the guest reads the roster
        # correctly from its first render rather than one sync later.
        assert b.roleset.default == "reviewer"
        assert b.roles_version == a.roles_version

        # D1: the MIRROR uploads. It is forwarded to the authority, adopted
        # there, and pushed back out — the dashboard a user happens to have
        # open should not have to be the authority's. So a mirror reports
        # is_authority=False and is still perfectly able to edit.
        assert mms["pcB"].roles_view("m")["is_authority"] is False
        assert mms["pcA"].roles_view("m")["is_authority"] is True
        await mms["pcB"].set_roles("m", "roles: {worker: {aliases: [smith]}}")
        assert a.roles_doc is not None
        assert a.roleset.infer("smith1") == "worker"
        await mms["pcA"]._flush_guest(a, "pcB")
        assert b.roles_doc == a.roles_doc
        assert b.roles_version == a.roles_version
        assert b.roleset.infer("smith1") == "worker"

        # B6: clearing it converges too — "back to the packaged set" is a
        # change like any other, not an absent field.
        await mms["pcB"].set_roles("m", None)
        await mms["pcA"]._flush_guest(a, "pcB")
        assert a.roles_doc is None and b.roles_doc is None
        assert b.roleset.infer("coder1") == "worker"

        await mgr.shutdown_all()

    asyncio.run(run())


def test_the_role_set_rides_a_sync_only_when_the_peer_is_behind(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mms = {
            name: MeshManager(mgr, settle=0.05, root=tmp_path / ("mesh" + name))
            for name in ("pcA", "pcB")
        }
        sent: list = []

        async def call(machine, path, body):
            if path == "/peer/mesh/sync":
                sent.append(body)
            return _dispatch_peer(mms[machine], path, body)

        for name, mm in mms.items():
            mm.machine = name
            mm.peer_transport = call
            mm.relay_connected = lambda: True
        for s in ("sa", "sb"):
            mgr.create(SessionDef(name=s, harness="py", cwd=str(tmp_path)))
        mms["pcA"].create("m")
        await mms["pcA"].join("m", "sa", handle="alice")
        await mms["pcB"].join(
            "m@pcA", "sb", handle="bob", code=mms["pcA"].invite("m")["code"]
        )
        a = mms["pcA"].get("m")

        # D3: ordinary traffic must not drag the stance prose along with it.
        sent.clear()
        for i in range(3):
            await mms["pcA"].send("m", "alice", ["bob"], f"hello {i}")
            await mms["pcA"]._flush_guest(a, "pcB")
        assert sent, "no sync happened, so the assertion below proves nothing"
        assert not any("roles" in body for body in sent)

        # Only a real change carries it, and only once.
        sent.clear()
        await mms["pcA"].set_roles("m", "roles: {worker: {aliases: [smith]}}")
        await mms["pcA"]._flush_guest(a, "pcB")
        assert sum("roles" in body for body in sent) == 1

        sent.clear()
        await mms["pcA"].send("m", "alice", ["bob"], "after")
        await mms["pcA"]._flush_guest(a, "pcB")
        assert not any("roles" in body for body in sent)

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# E. behaviour keyed off the role
# --------------------------------------------------------------------------- #
def test_stall_warnings_follow_the_vocabulary_not_a_hardcoded_leader(
    home, tmp_path
):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        for s in ("s1", "s2"):
            mgr.create(SessionDef(name=s, harness="py", cwd=str(tmp_path)))
        mm.create("m")
        mesh = mm.get("m")

        # A mesh that names its lead role something else still gets warned.
        await mm.set_roles("m", """
            replace: true
            default: crew
            roles:
              crew: {stance: do the work}
              captain: {stall_watch: true, stance: steer}
        """)
        await mm.join("m", "s1", handle="captain1")
        await mm.join("m", "s2", handle="crew1")
        assert mesh.members["captain1"].role == "captain"
        assert mesh.roleset.stall_watchers() == ["captain"]

        # E1: the tick's recipient list is derived, not literal.
        watching = set(mesh.roleset.stall_watchers())
        assert sorted(
            h for h, m in mesh.members.items() if m.role in watching
        ) == ["captain1"]

        await mgr.shutdown_all()

    asyncio.run(run())


def test_task_poll_text_comes_from_the_role_then_the_policy_override(
    home, tmp_path
):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        mm.create("m")
        mesh = mm.get("m")
        tp = mesh.policy["task_poll"]

        # E2: with nothing set here, the text is the ROLE's — so a mesh that
        # defines its own roles gets matching bodies without a second edit.
        assert tp["bodies"] == {}
        body = mesh_policy.task_poll_body(mesh, tp, "worker")
        assert body == mesh.roleset.get("worker").task_poll
        assert "ask the leader to assign one" in body

        # A role with no task_poll of its own falls back to the generic line.
        assert "work for a reviewer" in mesh_policy.task_poll_body(
            mesh, tp, "reviewer"
        )
        # As does a role the vocabulary no longer has.
        assert "work for a ghost" in mesh_policy.task_poll_body(mesh, tp, "ghost")

        # The mesh's own policy body still wins — it is the more specific edit.
        mm.set_policy("m", {"task_poll": {"bodies": {"worker": "pull a task"}}})
        assert mesh_policy.task_poll_body(
            mesh, mesh.policy["task_poll"], "worker"
        ) == "pull a task"

        await mgr.shutdown_all()

    asyncio.run(run())


def test_the_join_briefing_points_at_the_stance_rather_than_pasting_it(
    home, tmp_path
):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
        mm.create("m")
        mesh = mm.get("m")
        await mm.join("m", "s1", handle="coder1")
        member = mesh.members["coder1"]

        # One line naming the command, never the prose: the block is typed
        # into a live terminal, and inlining would freeze the stance into the
        # agent's context so later uploads never reached it.
        lines = mm._stance_lines(mesh, member)
        assert lines.splitlines() == [
            "stance: run 'claunch mesh stance m' now — it prints what a "
            "worker is on this mesh, and it is binding"
        ]
        assert "You are a PRODUCER" not in lines

        # It stays one line however long the stance gets...
        await mm.set_roles("m", {"roles": {"worker": {"stance": "x" * 5000}}})
        assert len(mm._stance_lines(mesh, member).splitlines()) == 1
        assert "xxx" not in mm._stance_lines(mesh, member)

        # ...and a role with no stance adds nothing at all, rather than
        # pointing at a command that would print nothing.
        await mm.set_roles("m", {"roles": {"worker": {}}})
        assert mm._stance_lines(mesh, member) == ""

        await mgr.shutdown_all()

    asyncio.run(run())


def test_the_http_surface_uploads_reads_and_resets_the_role_set(home, tmp_path):
    """The surface the CLI and the web panel both drive."""
    _register_py_harness()
    import time

    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        auth = {"Authorization": "Bearer sekrit"}
        try:
            await client.post("/api/mesh", json={"name": "web"}, headers=auth)
            mgr.create(SessionDef(name="w1", harness="py", cwd=str(tmp_path)))
            resp = await client.post(
                "/api/mesh/web/members",
                json={"session": "w1", "handle": "coder1"}, headers=auth,
            )
            assert (await resp.json())["role"] == "worker"

            resp = await client.get("/api/mesh/web/roles", headers=auth)
            doc = await resp.json()
            assert resp.status == 200 and doc["custom"] is False
            assert doc["default"] == "reviewer"
            assert [r["name"] for r in doc["roles"]] == [
                "leader", "operator", "reviewer", "specialist", "worker"
            ]
            # The roster is where the vocabulary meets reality.
            assert next(
                r for r in doc["roles"] if r["name"] == "worker"
            )["members"] == ["coder1"]
            # What --yaml prints is what --file takes back.
            assert "aliases" in doc["yaml"]

            resp = await client.put(
                "/api/mesh/web/roles",
                json={"yaml": "roles: {worker: {aliases: [smith]}}"},
                headers=auth,
            )
            doc = await resp.json()
            assert resp.status == 200 and doc["custom"] is True
            assert mm.get("web").roleset.infer("smith2") == "worker"

            # A bad upload is a 400 that names the problem, and changes nothing.
            before = mm.get("web").roles_version
            resp = await client.put(
                "/api/mesh/web/roles", json={"yaml": "roles: {w: {stanze: 1}}"},
                headers=auth,
            )
            assert resp.status == 400
            assert "unknown key" in (await resp.json())["error"]
            assert mm.get("web").roles_version == before

            # An emptied editor means reset, not "a set with no roles".
            resp = await client.put(
                "/api/mesh/web/roles", json={"yaml": "   "}, headers=auth
            )
            assert resp.status == 200 and (await resp.json())["custom"] is False
            assert mm.get("web").roles_doc is None

            # The mesh view carries the summary, never the stance prose — the
            # dashboard polls it every 2 seconds.
            resp = await client.get("/api/mesh/web", headers=auth)
            summary = (await resp.json())["roles"]
            assert summary["names"] and "stance" not in str(summary)
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# F. persistence
# --------------------------------------------------------------------------- #
def test_an_override_survives_a_reload_and_costs_nothing_when_unused(
    home, tmp_path
):
    _register_py_harness()

    async def run():
        mgr = _manager()
        root = tmp_path / "mesh"
        mm = MeshManager(mgr, settle=0.05, root=root)
        mm.create("plain")
        mm.create("custom")
        await mm.set_roles("custom", "roles: {worker: {aliases: [smith]}}")

        # F1: a mesh that never touches this writes no roles key at all, so
        # mesh.json stays exactly as small as it always was.
        plain_doc = json.loads((root / "plain" / "mesh.json").read_text("utf-8"))
        assert "roles" not in plain_doc and "roles_version" not in plain_doc

        fresh = MeshManager(mgr, settle=0.05, root=root)
        fresh.load_all()
        assert fresh.get("plain").roles_doc is None
        assert fresh.get("plain").roleset.infer("coder1") == "worker"
        reloaded = fresh.get("custom")
        assert reloaded.roles_doc is not None
        assert reloaded.roles_version == mm.get("custom").roles_version
        assert reloaded.roleset.infer("smith1") == "worker"

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# G. the wiring rules
#
# Pure document tests: these ask what the rules MEAN, not what a join does
# with them (that is tests/test_member_graph.py). No sessions, no mesh —
# `resolve` and `decide` are the whole surface.
# --------------------------------------------------------------------------- #
def _facts(role, tier, root):
    return mesh_roles.LinkFacts(role=role, tier=tier, root=root)


def test_the_packaged_rules_connect_roots_and_nothing_else():
    auto = mesh_roles.resolve().auto_link
    lead, other = _facts("leader", 0, "lead"), _facts("reviewer", 0, "other")
    child = _facts("worker", 1, "lead")
    # G1
    assert auto.decide(lead, other) is True
    assert auto.decide(lead, child) is False       # the JOIN adds this one
    assert auto.decide(child, _facts("worker", 1, "other")) is False


def test_a_rule_matches_a_pair_in_either_orientation():
    """G2: a rule is a predicate over a pair, not an instruction to whoever
    joined second — otherwise the wiring would depend on the order a fleet
    happens to come up in."""
    auto = mesh_roles.resolve(mesh_roles.parse(
        "auto_link: {rules: [{between: [{role: worker}, {role: reviewer}]}]}"
    )).auto_link
    w, r = _facts("worker", 1, "lead"), _facts("reviewer", 2, "lead")
    assert auto.decide(w, r) is True
    assert auto.decide(r, w) is True
    assert auto.decide(w, _facts("worker", 1, "lead")) is False


def test_within_tree_confines_a_rule_to_one_fleet():
    auto = mesh_roles.resolve(mesh_roles.parse(
        "auto_link: {rules: [{between: [{role: worker}, {role: reviewer}], "
        "within: tree}]}"
    )).auto_link
    w = _facts("worker", 1, "leadA")
    # G3
    assert auto.decide(w, _facts("reviewer", 1, "leadA")) is True
    assert auto.decide(w, _facts("reviewer", 1, "leadB")) is False


def test_a_rule_naming_an_undefined_role_is_refused():
    """G4: the reason the wiring lives in the role-set document. Deleting a
    role while a rule still names it is caught here rather than becoming a
    rule that quietly matches nothing for the rest of the mesh's life."""
    with pytest.raises(mesh_roles.RoleError) as exc:
        mesh_roles.resolve(mesh_roles.parse(
            "auto_link: {rules: [{between: [{role: worker}, {}]}]}\n"
            "roles: {worker: null}\n"
        ))
    assert "worker" in str(exc.value)
    # ...and an alias resolves like a handle does, so `coder` is `worker`
    auto = mesh_roles.resolve(mesh_roles.parse(
        "auto_link: {rules: [{between: [{role: coder}, {}]}]}"
    )).auto_link
    assert auto.decide(_facts("worker", 3, "x"), _facts("leader", 0, "y")) is True


@pytest.mark.parametrize("doc, needle", [
    ("auto_link: {rules: [{between: [{}]}]}", "exactly two"),
    ("auto_link: {rules: [{between: [{tier: leaf}, {}]}]}", "'root'"),
    ("auto_link: {rules: [{between: [{}, {}], within: sometimes}]}", "within"),
    ("auto_link: {rules: [{beteen: []}]}", "unknown key"),
    ("auto_link: {rules: [{between: [{}, {}]}, {between: [{rank: 1}, {}]}]}",
     "rule 2"),
])
def test_a_malformed_rule_is_refused_and_named(doc, needle):
    # G5: the rule's ORDINAL is in the message — a document with a dozen of
    # them is unusable if the error only says one of them is wrong.
    with pytest.raises(mesh_roles.RoleError) as exc:
        mesh_roles.parse(doc)
    assert needle in str(exc.value)


def test_auto_link_replaces_wholesale_and_outlives_replace():
    """G6: a rule list has no key to merge on, so an upload's rules are the
    rules. But `replace:` is about the VOCABULARY — a mesh bringing its own
    role names does not also, silently, stop connecting its roots."""
    empty = mesh_roles.resolve(mesh_roles.parse("auto_link: {rules: []}")).auto_link
    assert empty.decide(_facts("leader", 0, "a"), _facts("leader", 0, "b")) is False

    kept = mesh_roles.resolve(mesh_roles.parse(
        "replace: true\ndefault: hand\nroles: {hand: {}}\n"
    )).auto_link
    assert kept.decide(_facts("hand", 0, "a"), _facts("hand", 0, "b")) is True
