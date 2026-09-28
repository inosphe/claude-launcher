"""One session's statistics: usage over time, and where its input came from.

``daemon/sessionstats.py`` reads the same transcripts as ``tokenusage`` for
the dashboard's Stats page. The tests cover sorting every input by origin
(a person, a claunch notice, a mesh message per sender, the opening task,
the harness), the three harnesses' formats, hour/day/week buckets in a fixed
zone, the two estimates built on the inputs (the requests a turn triggered,
and the text carried as context until a compaction), and the endpoint.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from claude_launcher import transcripts
from claude_launcher.daemon import ctxsize, sessionstats, tokenusage
from claude_launcher.daemon.harness import SessionDef

CID = "beef0000-0000-0000-0000-0000000000bb"
KST = timezone(timedelta(hours=9))


@pytest.fixture(autouse=True)
def _fresh():
    ctxsize.forget()
    tokenusage.forget()
    yield
    tokenusage.forget()
    ctxsize.forget()


def at(hour, minute=0, day=27):
    return f"2026-09-{day:02d}T{hour:02d}:{minute:02d}:00.000Z"


def stamp(body: str) -> str:
    return "[claunch delivered 2026-09-27 09:00:00 +0900]\n" + body


REMINDER = stamp("---\n# claunch session: reminder -- machine-generated\nsession: s1\n---")
NUDGE = stamp("cflow: step 'work' is still open -- call next when done")
WINDOW = stamp("---\n# claunch window: release reminder -- machine-generated\ngrant: g1\n---")
OPENING = stamp("---\n# claunch: the session that created you -- machine-generated\n"
                "parent: s0\n---\nfix the bug")


def mesh_block(*senders: str) -> str:
    items = "".join(
        f"- id: msg-{i}\n  from: {who}\n  machine: local\n  type: fyi\n"
        f"  body: hello from {who}\n"
        for i, who in enumerate(senders))
    return stamp("---\n# claunch mesh: automated message delivery — machine-generated\n"
                 f"mesh: m1\nto: s1\nmessages: {len(senders)}\nbatch:\n{items}"
                 "note: fyi/ack only — no reply expected\n...")


def user(text, when, **extra) -> str:
    return json.dumps({"type": "user", "timestamp": when,
                       "message": {"role": "user", "content": text}, **extra})


def queued(text, when) -> str:
    return json.dumps({"type": "attachment", "timestamp": when, "attachment": {
        "type": "queued_command",
        "prompt": f'<pasted_content id="ab12">\n{text}\n</pasted_content id="ab12">'}})


def tool_result(when) -> str:
    return json.dumps({"type": "user", "timestamp": when, "message": {
        "role": "user", "content": [{"type": "tool_result", "tool_use_id": "t",
                                     "content": "[claunch delivered x] not an input"}]}},
        separators=(",", ":"))


def reply(mid, when, *, fresh=0, read=0, write=0, out=0, side=False) -> str:
    return json.dumps({"type": "assistant", "timestamp": when, "isSidechain": side,
                       "requestId": "req_" + mid, "message": {
                           "id": mid, "role": "assistant", "usage": {
                               "input_tokens": fresh,
                               "cache_read_input_tokens": read,
                               "cache_creation_input_tokens": write,
                               "output_tokens": out}}})


def compaction(when) -> str:
    return json.dumps({"type": "system", "subtype": "compact_boundary", "timestamp": when})


def write(path: pathlib.Path, *lines: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def read(path, harness="claude", unit="day"):
    got = sessionstats.read_file(path, harness, unit, KST)
    assert got is not None
    return got


def source(reading, category):
    return next(s for s in reading["sources"] if s["category"] == category)


def kinds(reading, category):
    return {k["kind"]: k for k in source(reading, category)["kinds"]}


# --------------------------------------------------------------------------- #
# classifying one input
# --------------------------------------------------------------------------- #

def test_text_without_a_stamp_was_typed_by_a_person():
    [got] = sessionstats.classify("please add a stats tab", 0.0, True)
    assert (got.category, got.kind, got.messages) == ("human", "typed", 1)


def test_the_first_delivery_is_the_opening_and_later_ones_are_notices():
    [opening] = sessionstats.classify(OPENING, 0.0, True)
    [later] = sessionstats.classify(OPENING, 0.0, False)
    assert opening.category == "opening"
    assert (later.category, later.kind) == ("daemon", "the session that created you")


@pytest.mark.parametrize("text, kind", [
    (REMINDER, "session: reminder"),
    (NUDGE, "cflow nudge"),
    (WINDOW, "window: release reminder"),
    (stamp("[claunch status-check refresh] answer C/M/T"), "status-check refresh"),
    (stamp("[Operator] please look at s2"), "operator"),
    (stamp("---\n# claunch cflow: forced step 'intake' (goto gr-1)\n---"),
     "cflow: forced step 'intake'"),
    (stamp("something new"), "other"),
])
def test_a_notice_is_named_by_its_header(text, kind):
    [got] = sessionstats.classify(text, 0.0, False)
    assert (got.category, got.kind) == ("daemon", kind)


def test_a_mesh_delivery_counts_one_message_per_sender_plus_its_envelope():
    got = sessionstats.classify(mesh_block("s2", "s3", "s2"), 0.0, False)
    messages = [g for g in got if g.kind == "message"]
    [envelope] = [g for g in got if g.kind == "envelope"]
    assert [m.sender for m in messages] == ["s2", "s3", "s2"]
    assert all(m.category == "mesh" and m.messages == 1 for m in messages)
    assert envelope.messages == 0 and envelope.tokens > 0
    whole = sessionstats.estimate_tokens(mesh_block("s2", "s3", "s2").strip())
    assert abs(sum(g.tokens for g in got) - whole) <= len(got)


def test_a_turn_holding_several_deliveries_counts_each():
    text = f'<pasted_content id="x">\n{REMINDER}\n</pasted_content id="x">\n{NUDGE}'
    got = sessionstats.classify(text, 0.0, False)
    assert [g.kind for g in got] == ["session: reminder", "cflow nudge"]


def test_hangul_is_estimated_denser_than_ascii():
    assert sessionstats.estimate_tokens("abcd" * 10) == 10
    assert sessionstats.estimate_tokens("세션별통계") == 5


# --------------------------------------------------------------------------- #
# claude
# --------------------------------------------------------------------------- #

def test_claude_inputs_are_sorted_by_origin(tmp_path):
    path = write(
        tmp_path / "t.jsonl",
        user(OPENING, at(0)),
        user("skill body", at(0, 1), isMeta=True),
        reply("a", at(0, 2), out=10),
        tool_result(at(0, 3)),
        queued(REMINDER, at(0, 4)),
        user("<task-notification>done</task-notification>", at(0, 5)),
        user("now the next thing", at(0, 6)),
        user(mesh_block("s2"), at(0, 7)),
        user("summary of the conversation", at(0, 8), isCompactSummary=True),
    )
    got = read(path)
    counts = {s["category"]: s["messages"] for s in got["sources"]}
    assert counts == {"human": 1, "daemon": 1, "mesh": 1, "opening": 1, "harness": 3}
    assert set(kinds(got, "harness")) == {"skill or meta", "task notification",
                                          "compaction summary"}
    assert got["senders"][0]["sender"] == "s2"


def test_claude_counts_each_response_once_and_subagents_in_the_totals(tmp_path):
    path = write(tmp_path / "t.jsonl",
                 user(OPENING, at(0)),
                 reply("a", at(0, 1), read=100, out=5),
                 reply("a", at(0, 1), read=100, out=5),
                 reply("b", at(0, 2), fresh=7, side=True))
    got = read(path)
    assert got["totals"]["requests"] == 2
    assert got["totals"]["subagent_requests"] == 1
    assert got["totals"]["total"] == 112


def test_a_delivery_queued_into_a_turn_does_not_take_it_over(tmp_path):
    path = write(tmp_path / "t.jsonl",
                 user(OPENING, at(0, day=26)),
                 user("build it", at(0)),
                 reply("a", at(0, 1), out=10),
                 queued(REMINDER, at(0, 2)),
                 reply("b", at(0, 3), out=20),
                 user(NUDGE, at(1)),
                 reply("c", at(1, 1), out=40))
    got = read(path)
    assert source(got, "human")["triggered"]["total"] == 30
    assert source(got, "human")["requests"] == 2
    assert kinds(got, "daemon")["cflow nudge"]["triggered"]["total"] == 40
    assert kinds(got, "daemon")["session: reminder"]["requests"] == 0
    assert source(got, "human")["share"]["triggered"] == pytest.approx(30 / 70)


def test_carry_counts_the_main_requests_until_the_next_compaction(tmp_path):
    text = "x" * 400      # 100 estimated tokens
    path = write(tmp_path / "t.jsonl",
                 user(text, at(0)),
                 reply("a", at(0, 1), out=1),
                 reply("b", at(0, 2), out=1),
                 reply("s", at(0, 2), out=1, side=True),
                 compaction(at(0, 3)),
                 reply("c", at(0, 4), out=1))
    got = read(path)
    assert source(got, "human")["tokens"] == 100
    assert source(got, "human")["carry"] == 200
    assert got["compactions"] == 1


def test_a_tool_result_quoting_a_stamp_is_not_an_input(tmp_path):
    path = write(tmp_path / "t.jsonl", tool_result(at(0)), reply("a", at(0, 1), out=3))
    got = read(path)
    assert sum(s["messages"] for s in got["sources"]) == 0
    assert got["unattributed"]["requests"] == 1


# --------------------------------------------------------------------------- #
# buckets
# --------------------------------------------------------------------------- #

def test_day_buckets_follow_the_local_zone_and_leave_no_gaps(tmp_path):
    # 14:00Z on the 26th is the 26th's 23:00 in KST; 15:00Z is the 27th.
    path = write(tmp_path / "t.jsonl",
                 reply("a", at(14, day=26), out=1),
                 reply("b", at(15, day=26), out=2),
                 reply("c", at(15, day=28), out=4))
    got = read(path, unit="day")
    assert [b["start"][:10] for b in got["buckets"]] == [
        "2026-09-26", "2026-09-27", "2026-09-28", "2026-09-29"]
    assert [b["output"] for b in got["buckets"]] == [1, 2, 0, 4]
    assert got["utc_offset"] == "+0900"


def test_week_buckets_start_on_monday(tmp_path):
    # 2026-09-27 is a Sunday.
    path = write(tmp_path / "t.jsonl",
                 reply("a", at(2, day=27), out=1),
                 reply("b", at(2, day=28), out=2))
    got = read(path, unit="week")
    assert [b["start"][:10] for b in got["buckets"]] == ["2026-09-21", "2026-09-28"]


def test_hour_buckets_are_capped_to_the_newest(tmp_path, monkeypatch):
    monkeypatch.setitem(sessionstats.UNITS, "hour", 3)
    path = write(tmp_path / "t.jsonl",
                 *(reply(f"m{h}", at(h), out=1) for h in range(6)))
    got = read(path, unit="hour")
    assert len(got["buckets"]) == 3
    assert got["buckets"][-1]["start"].startswith("2026-09-27T14:00")
    assert got["totals"]["requests"] == 6


def test_inputs_are_counted_in_their_bucket(tmp_path):
    path = write(tmp_path / "t.jsonl",
                 user("hi", at(0)), user(mesh_block("s2", "s3"), at(1)))
    got = read(path, unit="hour")
    assert [b["inputs"]["mesh"] for b in got["buckets"]] == [0, 2]
    assert [b["inputs"]["human"] for b in got["buckets"]] == [1, 0]


def test_an_unknown_unit_is_refused(tmp_path):
    path = write(tmp_path / "t.jsonl", reply("a", at(0), out=1))
    with pytest.raises(ValueError):
        sessionstats.read_file(path, "claude", "month")


# --------------------------------------------------------------------------- #
# codex and pi
# --------------------------------------------------------------------------- #

def codex_user(text, when):
    return json.dumps({"timestamp": when, "type": "response_item", "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": text}]}})


def codex_count(total_in, cached, out, last_in, last_cached, last_out, when):
    def usage(i, c, o):
        return {"input_tokens": i, "cached_input_tokens": c, "output_tokens": o,
                "total_tokens": i + o}
    return json.dumps({"timestamp": when, "type": "event_msg", "payload": {
        "type": "token_count", "info": {
            "total_token_usage": usage(total_in, cached, out),
            "last_token_usage": usage(last_in, last_cached, last_out)}}})


def test_codex_reads_user_messages_and_each_new_count(tmp_path):
    path = write(tmp_path / "r.jsonl",
                 codex_user("<environment_context>cwd</environment_context>", at(0)),
                 codex_user(OPENING, at(0, 1)),
                 codex_count(100, 60, 5, 100, 60, 5, at(0, 2)),
                 codex_count(100, 60, 5, 100, 60, 5, at(0, 2)),
                 codex_user(REMINDER, at(0, 3)),
                 codex_count(250, 160, 9, 150, 100, 4, at(0, 4)))
    got = read(path, "codex")
    assert got["totals"]["requests"] == 2
    assert got["totals"]["cache_read"] == 160 and got["totals"]["input"] == 90
    assert source(got, "harness")["messages"] == 1
    assert source(got, "opening")["triggered"]["total"] == 105
    assert kinds(got, "daemon")["session: reminder"]["triggered"]["total"] == 154


def test_pi_reads_user_messages_and_assistant_usage(tmp_path):
    def pi(eid, role, when, text="", **usage):
        msg = {"role": role, "content": [{"type": "text", "text": text}]}
        if usage:
            msg["usage"] = usage
        return json.dumps({"type": "message", "id": eid, "timestamp": when, "message": msg})
    path = write(tmp_path / "p.jsonl",
                 pi("u1", "user", at(0), OPENING),
                 pi("a1", "assistant", at(0, 1), input=10, cacheRead=90, output=3),
                 pi("u2", "user", at(0, 2), mesh_block("s9")),
                 pi("a2", "assistant", at(0, 3), input=1, cacheRead=100, output=2))
    got = read(path, "pi")
    assert got["totals"]["total"] == 206
    assert source(got, "mesh")["triggered"]["total"] == 103
    assert got["senders"][0]["sender"] == "s9"


# --------------------------------------------------------------------------- #
# a session, and the endpoint
# --------------------------------------------------------------------------- #

def _claude_session(tmp_path, *lines):
    cwd = tmp_path / "work"
    cwd.mkdir(exist_ok=True)
    pdir = transcripts.project_dir(tmp_path / ".claude-config", str(cwd))
    write(pdir / f"{CID}.jsonl", *lines)
    return SessionDef(name="s1", harness="claude", cwd=str(cwd), conversation_id=CID)


def test_a_session_without_a_transcript_says_so(home, tmp_path):
    sdef = SessionDef(name="s1", harness="claude", cwd=str(tmp_path))
    got = sessionstats.for_session(sdef)
    assert got["available"] is False and "transcript" in got["reason"]
    other = SessionDef(name="s2", harness="nothing", cwd=str(tmp_path))
    assert sessionstats.for_session(other)["available"] is False


def test_the_stats_endpoint_reads_the_session(home, tmp_path):
    import asyncio
    import time

    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager
    from claude_launcher.daemon.mesh import MeshManager
    from claude_launcher.daemon.session import DeadSession

    sdef = _claude_session(tmp_path, user(OPENING, at(0)),
                           reply("a", at(0, 1), read=50, out=5))
    bearer = {"Authorization": "Bearer sekrit"}

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mgr._sessions["s1"] = DeadSession(sdef, exit_code=0)
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=MeshManager(mgr))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/api/sessions/s1/stats?unit=hour", headers=bearer)
            assert resp.status == 200
            body = await resp.json()
            assert body["available"] and body["unit"] == "hour"
            assert body["totals"]["total"] == 55
            assert source(body, "opening")["messages"] == 1
            resp = await client.get("/api/sessions/s1/stats?unit=month", headers=bearer)
            assert resp.status == 400
            # An unknown name is the manager's 400, as for every session route.
            resp = await client.get("/api/sessions/nobody/stats", headers=bearer)
            assert resp.status == 400 and "error" in await resp.json()
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())
