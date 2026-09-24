"""``pending`` and ``owed_all`` read the log through its address index.

Both used to walk the log entry by entry, per member, and on mesh-0826
(382 members, 29697 messages) a full roster read visited 4.3 million entries
to find the few that concerned anyone -- 4.1s of event loop per
``GET /api/mesh``, which every cflow delegation check calls (claunch-y9ax9,
measured 2026-09-24). ``mesh._LogIndex`` keeps the positions that can be
addressed to each handle, so the walks visit only those.

What these pin: the answers are the ones the plain walk gives (the member
graph, the cursor, delivered ids, the join floor and the unsequenced tail
all still count), the index follows a log that is replaced or shortened,
and the walk no longer touches mail sent to somebody else.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from claude_launcher.daemon.mesh import (
    Member,
    Mesh,
    _age_secs,
    expects_reply,
    msg_type_for,
)

HANDLES = ["lead", "w1", "w2", "w3", "w4"]
T0 = datetime(2026, 9, 24, tzinfo=timezone.utc)


def _ts(i: int) -> str:
    return (T0 + timedelta(seconds=i)).isoformat()


def _plain_pending(mesh: Mesh, handle: str) -> list:
    """The walk as it was before the index."""
    start = mesh.cursors.get(handle, 0)
    done = mesh.delivered_ids.get(handle) or frozenset()
    out = [
        m for m in mesh.messages[start:]
        if mesh.addressed_to(m, handle) and m.get("id") not in done
    ]
    out.extend(
        m for m in mesh.provisional
        if mesh.addressed_to(m, handle) and m.get("id") not in done
    )
    return out


def _plain_owed_all(mesh: Mesh, handle: str) -> list:
    """The walk as it was before the index."""
    start = mesh.cursors.get(handle, 0)
    done = mesh.delivered_ids.get(handle) or frozenset()
    n = len(mesh.messages)
    member = mesh.members.get(handle)
    now = datetime.now(timezone.utc)
    joined_ago = (
        _age_secs(member.joined_at, now)
        if member is not None and member.joined_at
        else None
    )
    out = []
    for k in range(n + len(mesh.provisional) - 1, -1, -1):
        if k < n:
            m = mesh.messages[k]
            delivered = k < start or m.get("id") in done
        else:
            m = mesh.provisional[k - n]
            delivered = m.get("id") in done
        if m.get("from") == handle:
            break
        if joined_ago is not None:
            age = _age_secs(m.get("ts"), now)
            if age is not None and age > joined_ago:
                break
        if not delivered or not mesh.addressed_to(m, handle):
            continue
        if expects_reply(msg_type_for(m, handle)):
            out.append(m)
    out.reverse()
    return out


def _random_mesh(seed: int) -> Mesh:
    rnd = random.Random(seed)
    mesh = Mesh("m")
    for i, h in enumerate(HANDLES):
        # Joins spread over the log, so the floor bites for some members.
        mesh.members[h] = Member(
            h, f"s{i}", joined_at=_ts(rnd.choice([0, 0, 40, 120])),
            wired=rnd.random() < 0.5,
        )
    for a in HANDLES:
        for b in HANDLES:
            if a < b and rnd.random() < 0.3:
                mesh.member_edges[Mesh.member_key(a, b)] = rnd.random() < 0.6
    for i in range(300):
        sender = rnd.choice(HANDLES + ["operator"])
        shape = rnd.random()
        if shape < 0.15:
            to = "*"
        elif shape < 0.6:
            to = rnd.choice(HANDLES)
        else:
            to = rnd.sample(HANDLES, rnd.randint(1, 3))
        msg = {
            "id": f"m{i}", "from": sender, "to": to, "ts": _ts(i),
            "type": rnd.choice(["say", "ask", "fyi", "ack"]),
        }
        (mesh.provisional if i >= 290 else mesh.messages).append(msg)
    for h in HANDLES:
        mesh.cursors[h] = rnd.randint(0, len(mesh.messages))
        mesh.delivered_ids[h] = {
            f"m{i}" for i in rnd.sample(range(300), 40)
        }
    return mesh


def test_the_answers_are_the_plain_walks_answers():
    for seed in range(40):
        mesh = _random_mesh(seed)
        for h in HANDLES:
            assert [m["id"] for m in mesh.pending(h)] == [
                m["id"] for m in _plain_pending(mesh, h)
            ], (seed, h, "pending")
            assert [m["id"] for m in mesh.owed_all(h)] == [
                m["id"] for m in _plain_owed_all(mesh, h)
            ], (seed, h, "owed_all")


def test_an_edge_cut_after_the_index_was_built_still_counts():
    """The index holds addresses; the graph is asked on every read."""
    mesh = _random_mesh(1)
    mesh.member_edges.clear()
    for m in mesh.members.values():
        m.wired = False
    mesh.messages.append({"id": "x", "from": "lead", "to": ["w1"], "ts": _ts(500)})
    assert "x" in [m["id"] for m in mesh.pending("w1")]
    mesh.member_edges[Mesh.member_key("lead", "w1")] = False
    assert "x" not in [m["id"] for m in mesh.pending("w1")]


def test_the_index_follows_a_log_that_is_appended_replaced_or_shortened():
    # Sent by a non-member, which the member graph always lets through.
    mesh = _random_mesh(2)
    for h in HANDLES:
        mesh.pending(h)  # builds the index
    mesh.messages.append({"id": "tail", "from": "operator", "to": "*", "ts": _ts(999)})
    mesh.cursors["w1"] = 0
    assert "tail" in [m["id"] for m in mesh.pending("w1")]

    # Shortened: the entries the index remembers are gone.
    del mesh.messages[10:]
    for h in HANDLES:
        assert mesh.pending(h) == _plain_pending(mesh, h)

    # Same length, different last entry.
    mesh.messages[-1] = {"id": "swap", "from": "operator", "to": ["w1"], "ts": _ts(9)}
    assert "swap" in [m["id"] for m in mesh.pending("w1")]

    # A different list object altogether (a reload).
    mesh.messages = [{"id": "fresh", "from": "operator", "to": "w1", "ts": _ts(1)}]
    mesh.provisional = []
    mesh.cursors["w1"] = 0
    assert [m["id"] for m in mesh.pending("w1")] == ["fresh"]


def test_mail_to_somebody_else_is_not_walked():
    mesh = Mesh("m")
    for h in ("lead", "w1", "w2"):
        mesh.members[h] = Member(h, h, joined_at=_ts(0))
    for i in range(1, 2001):
        mesh.messages.append(
            {"id": f"o{i}", "from": "lead", "to": ["w2"], "ts": _ts(i), "type": "ask"}
        )
    mesh.messages.append(
        {"id": "mine", "from": "lead", "to": ["w1"], "ts": _ts(3000), "type": "ask"}
    )
    mesh.cursors["w1"] = len(mesh.messages)

    asked = []
    real = Mesh.addressed_to

    def watched(self, msg, handle):
        asked.append(msg["id"])
        return real(self, msg, handle)

    Mesh.addressed_to = watched
    try:
        assert [m["id"] for m in mesh.owed_all("w1")] == ["mine"]
        mesh.cursors["w1"] = 0
        assert [m["id"] for m in mesh.pending("w1")] == ["mine"]
    finally:
        Mesh.addressed_to = real
    assert asked == ["mine", "mine"]
