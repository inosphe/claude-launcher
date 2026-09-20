"""Finding a conversation from a shell, and searching inside it.

The dashboard's transcript page was the only door to the record of what a
session said. These cover the second door — ``claunch transcript ls | path |
show | search``, the two daemon routes it reads, and the search itself: what
it matches, what it stops at, and what it reports having left unread.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from claude_launcher import cli_transcript, daemon_client
from claude_launcher.daemon import paths, transcript_view as tv


class FakeDef:
    """The surface the locator reads; the tests point it directly."""

    def __init__(self, name="s1", harness="claude"):
        self.name = name
        self.harness = harness
        self.conversation_id = "c-1"
        self.cwd = "F:/works/x"


@pytest.fixture
def transcript(tmp_path, monkeypatch):
    src = tmp_path / "conv.jsonl"
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(tv, "locate_transcript", lambda sdef, **kw: src)
    monkeypatch.setattr(tv.paths, "session_dir", lambda name: sessions / name)
    return src


def _rec(role, content, ts="2026-09-21T00:00:00Z"):
    return json.dumps(
        {"type": role, "timestamp": ts, "message": {"role": role, "content": content}},
        ensure_ascii=False,
    ) + "\n"


def _write(path, records):
    path.write_text("".join(records), encoding="utf-8")


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #
def test_search_reports_matches_newest_first_with_the_seq_to_read_them_at(transcript):
    """The seq is the whole point of a match: it is what ``show --before``
    takes, so a hit found here is readable in context there."""
    _write(transcript, [
        _rec("user", "첫 질문: 트랜스크립트를 어디에 두는가"),
        _rec("assistant", "관계 없는 답"),
        _rec("user", "두 번째 질문: 트랜스크립트 검색"),
    ])
    view = tv.search("s1", FakeDef(), query="트랜스크립트")
    assert [m["seq"] for m in view["matches"]] == [2, 0]
    assert view["total"] == 3
    assert view["truncated"] is False
    assert "트랜스크립트" in view["matches"][0]["excerpt"]
    assert view["matches"][0]["role"] == "user"


def test_search_ignores_case_until_it_is_asked_not_to(transcript):
    _write(transcript, [_rec("user", "Transcript View")])
    assert tv.search("s1", FakeDef(), query="transcript")["matches"]
    assert not tv.search("s1", FakeDef(), query="transcript",
                         ignore_case=False)["matches"]


def test_a_literal_query_is_not_read_as_a_pattern(transcript):
    """``a.b`` finds ``a.b`` and not ``axb`` — a search box that quietly
    took regular expressions would answer a question nobody asked."""
    _write(transcript, [_rec("user", "axb"), _rec("assistant", "a.b")])
    literal = tv.search("s1", FakeDef(), query="a.b")
    assert [m["seq"] for m in literal["matches"]] == [1]
    loose = tv.search("s1", FakeDef(), query="a.b", regex=True)
    assert [m["seq"] for m in loose["matches"]] == [1, 0]


def test_a_bad_pattern_is_an_answer_not_a_traceback(transcript):
    _write(transcript, [_rec("user", "x")])
    with pytest.raises(ValueError):
        tv.search("s1", FakeDef(), query="(unclosed", regex=True)


def test_search_counts_every_hit_in_a_record_but_reports_it_once(transcript):
    _write(transcript, [_rec("user", "rebase, then rebase again, rebase")])
    [match] = tv.search("s1", FakeDef(), query="rebase")["matches"]
    assert match["matches"] == 3


def test_the_walk_stops_at_the_limit_and_says_records_are_behind_it(transcript):
    """A conversation is read backwards until the caller has its fill. The
    records not reached are the ones it must not claim to have searched."""
    _write(transcript, [_rec("user", f"hit {i}") for i in range(10)])
    view = tv.search("s1", FakeDef(), query="hit", limit=3)
    assert [m["seq"] for m in view["matches"]] == [9, 8, 7]
    assert view["scanned"] == 3
    assert view["truncated"] is True
    whole = tv.search("s1", FakeDef(), query="hit", limit=50)
    assert whole["scanned"] == 10
    assert whole["truncated"] is False


def test_roles_and_prose_narrow_what_is_searched(transcript):
    _write(transcript, [
        _rec("user", "deploy it"),
        _rec("assistant", [
            {"type": "text", "text": "running the deploy"},
            {"type": "tool_use", "name": "Bash", "id": "t1",
             "input": {"command": "deploy --now"}},
        ]),
    ])
    users = tv.search("s1", FakeDef(), query="deploy", roles=["user"])
    assert [m["seq"] for m in users["matches"]] == [0]
    prose = tv.search("s1", FakeDef(), query="--now", prose_only=True)
    assert prose["matches"] == []
    tools = tv.search("s1", FakeDef(), query="--now")
    assert [m["block"] for m in tools["matches"]] == ["tool_use"]


def test_a_tool_is_findable_by_its_name(transcript):
    """Somebody looking for what a session ran searches for ``Bash``, which
    appears nowhere in the arguments."""
    _write(transcript, [_rec("assistant", [
        {"type": "tool_use", "name": "Bash", "id": "t1", "input": {"command": "ls"}},
    ])])
    assert tv.search("s1", FakeDef(), query="Bash")["matches"]


def test_a_match_past_the_pages_clip_is_still_a_match(transcript):
    """The page clips a tool result at 2000 characters because a reader
    scrolling prose does not want a megabyte in the way. A search that
    inherited the clip would report a conversation as not holding something
    it holds."""
    buried = "x" * (tv.TOOL_CLIP + 500) + " NEEDLE"
    _write(transcript, [_rec("user", [
        {"type": "tool_result", "tool_use_id": "t1", "content": buried},
    ])])
    assert tv.search("s1", FakeDef(), query="NEEDLE")["matches"]
    # The page still clips, so the two readings stay different on purpose.
    [record] = tv.page("s1", FakeDef())["records"]
    assert record["blocks"][0]["clipped"] is True


def test_a_session_with_no_transcript_searches_to_an_empty_answer(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(tv, "locate_transcript", lambda sdef, **kw: None)
    monkeypatch.setattr(tv.paths, "session_dir", lambda name: tmp_path / name)
    view = tv.search("s1", FakeDef(), query="anything")
    assert view == {"matches": [], "total": 0, "scanned": 0,
                    "truncated": False, "source": None}


# --------------------------------------------------------------------------- #
# info
# --------------------------------------------------------------------------- #
def test_info_reports_the_file_and_counts_records_only_from_a_current_index(
    transcript,
):
    """``records`` is null until something has indexed the file: a listing of
    the fleet must not scan every transcript on it to fill a column."""
    _write(transcript, [_rec("user", "one"), _rec("assistant", "two")])
    cold = tv.info("s1", FakeDef())
    assert cold["source"] == str(transcript)
    assert cold["size"] == transcript.stat().st_size
    assert cold["records"] is None
    assert cold["modified_at"]

    tv.page("s1", FakeDef())  # builds the index
    warm = tv.info("s1", FakeDef())
    assert warm["records"] == 2

    _write(transcript, [_rec("user", "one"), _rec("assistant", "two"),
                        _rec("user", "three")])
    grown = tv.info("s1", FakeDef())
    assert grown["records"] is None  # the index no longer describes this file


def test_a_shallow_lookup_does_not_walk_the_config_dir(tmp_path, monkeypatch):
    """What a fleet listing turns off. The deep search reads every project
    directory, and a listing resolves hundreds of sessions in one request."""
    from claude_launcher.daemon import briefing

    walked = []
    monkeypatch.setattr(briefing.transcripts, "find",
                        lambda cdir, cid: walked.append(cid))
    monkeypatch.setattr(briefing, "_config_dir", lambda sdef: tmp_path)

    assert briefing.locate_transcript(FakeDef(), deep=False) is None
    assert walked == []
    briefing.locate_transcript(FakeDef())
    assert walked == ["c-1"]


# --------------------------------------------------------------------------- #
# the routes
# --------------------------------------------------------------------------- #
BEARER = {"Authorization": "Bearer sekrit"}


class _Known:
    """A session the manager knows, with nothing running behind it."""

    def __init__(self, name, status="idle", sdef=None):
        self.name = name
        self.sdef = sdef or FakeDef(name)
        self.sdef.name = name
        self.exited = status == "exited"
        self.archived_at = None
        self.paused_at = None
        self._status = status

    def status(self):
        return self._status


async def _client():
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager
    from claude_launcher.daemon.mesh import MeshManager

    mgr = SessionManager(idle_threshold=0.5, scrollback=50, restore_default=False)
    app = build_app(mgr, "sekrit", started_at=time.monotonic(),
                    mesh=MeshManager(mgr, root=paths.mesh_root()))
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def test_the_fleet_listing_carries_the_sessions_that_have_something_to_read(
    home, tmp_path, monkeypatch
):
    """One row per session that has a file, newest written first; the ones
    with nothing to read appear only when they are asked for."""
    src = tmp_path / "conv.jsonl"
    _write(src, [_rec("user", "hello")])
    monkeypatch.setattr(
        tv, "locate_transcript",
        lambda sdef, **kw: None if sdef.name == "s-blank" else src,
    )
    monkeypatch.setattr(tv.paths, "session_dir", lambda name: tmp_path / name)
    live, gone, blank = _Known("s-live"), _Known("s-gone", "exited"), _Known("s-blank")

    async def run():
        client = await _client()
        try:
            client.app["manager"].list = lambda: [live, gone, blank]
            resp = await client.get("/api/transcripts", headers=BEARER)
            rows = (await resp.json())["sessions"]
            assert [r["session"] for r in rows] == ["s-live", "s-gone"]
            assert rows[0]["status"] == "idle"
            assert rows[0]["source"] == str(src)
            assert rows[1]["category"] == "killed"

            resp = await client.get("/api/transcripts?all=1&state=active",
                                    headers=BEARER)
            rows = (await resp.json())["sessions"]
            assert [r["session"] for r in rows] == ["s-live", "s-blank"]
            assert rows[1]["source"] is None

            resp = await client.get("/api/transcripts?state=nonsense",
                                    headers=BEARER)
            assert resp.status == 400
        finally:
            await client.close()

    asyncio.run(run())


def test_the_search_route_answers_matches_and_refuses_a_bad_pattern(
    home, transcript
):
    _write(transcript, [_rec("user", "find me"), _rec("assistant", "not this")])

    async def run():
        client = await _client()
        try:
            known = _Known("s1")
            client.app["manager"].get = lambda name: known
            resp = await client.get(
                "/api/sessions/s1/transcript/search?q=find", headers=BEARER)
            view = await resp.json()
            assert [m["seq"] for m in view["matches"]] == [0]

            resp = await client.get(
                "/api/sessions/s1/transcript/search", headers=BEARER)
            assert resp.status == 400

            resp = await client.get(
                "/api/sessions/s1/transcript/search?q=(a&regex=1", headers=BEARER)
            assert resp.status == 400

            resp = await client.get(
                "/api/sessions/s1/transcript/info", headers=BEARER)
            assert (await resp.json())["source"] == str(transcript)
        finally:
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the commands
# --------------------------------------------------------------------------- #
class _FakeClient:
    """Stands in for the daemon: records what was asked, answers canned rows."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def get(self, path, **kw):
        self.asked.append(path)
        for prefix, answer in self.answers.items():
            if path.startswith(prefix):
                return answer
        raise AssertionError(f"unexpected request: {path}")


def _run(monkeypatch, answers, argv):
    from claude_launcher import cli

    client = _FakeClient(answers)
    monkeypatch.setattr(cli_transcript, "_client", lambda: client)
    parser = cli.build_parser()
    args = parser.parse_args(argv)
    return client, args.func(args)


def test_ls_prints_a_row_per_session_and_says_what_was_not_counted(
    monkeypatch, capsys
):
    rows = {"sessions": [
        {"session": "s1", "status": "idle", "harness": "claude", "size": 2048,
         "modified_at": "2026-09-21T00:00:00Z", "records": 12,
         "source": "C:/x/c-1.jsonl"},
        {"session": "s2", "status": "exited", "harness": "codex", "size": 10,
         "modified_at": "2026-09-20T00:00:00Z", "records": None,
         "source": "C:/x/c-2.jsonl"},
    ]}
    _, code = _run(monkeypatch, {"/api/transcripts": rows}, ["transcript", "ls"])
    out = capsys.readouterr().out
    assert code == 0
    assert "2 session(s)" in out
    assert "12 records" in out and "not counted" in out
    assert "C:/x/c-1.jsonl" in out


def test_path_prints_the_file_alone_and_fails_when_there_is_none(
    monkeypatch, capsys
):
    """One line on stdout is what a shell can use: ``less $(claunch
    transcript path s1)``."""
    found = {"session": "s1", "source": "C:/x/c-1.jsonl", "conversation_id": "c-1"}
    _, code = _run(monkeypatch, {"/api/sessions/s1/transcript/info": found},
                   ["transcript", "path", "s1"])
    assert code == 0
    assert capsys.readouterr().out == "C:/x/c-1.jsonl\n"

    missing = {"session": "s1", "source": None, "conversation_id": ""}
    _, code = _run(monkeypatch, {"/api/sessions/s1/transcript/info": missing},
                   ["transcript", "path", "s1"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "no transcript" in captured.err


def test_show_prints_records_with_their_seq_and_trims_tool_traffic(
    monkeypatch, capsys
):
    page = {
        "source": "C:/x/c-1.jsonl", "total": 90, "cursor": 88, "has_more": True,
        "records": [
            {"seq": 88, "role": "user", "ts": "2026-09-21T00:00:00Z",
             "blocks": [{"type": "text", "text": "두 줄\n짜리 질문"}]},
            {"seq": 89, "role": "assistant", "ts": "2026-09-21T00:00:01Z",
             "blocks": [{"type": "tool_use", "name": "Bash", "id": "t",
                         "text": "y" * 400, "clipped": True, "full": 5000}]},
        ],
    }
    _, code = _run(monkeypatch, {"/api/sessions/s1/transcript": page},
                   ["transcript", "show", "s1"])
    out = capsys.readouterr().out
    assert code == 0
    assert "[88] user" in out and "짜리 질문" in out
    assert "→ Bash" in out
    assert "--before 88" in out          # the cursor for the page above
    assert "more chars" in out           # what the daemon did not send
    assert "y" * 300 not in out          # and what this command did not print


def test_show_can_drop_tool_traffic_entirely(monkeypatch, capsys):
    page = {"source": "C:/x/c-1.jsonl", "total": 2, "cursor": 0, "has_more": False,
            "records": [{"seq": 0, "role": "assistant", "ts": "",
                         "blocks": [{"type": "text", "text": "said"},
                                    {"type": "tool_use", "name": "Bash",
                                     "id": "t", "text": "ran"}]}]}
    _, code = _run(monkeypatch, {"/api/sessions/s1/transcript": page},
                   ["transcript", "show", "s1", "--prose"])
    out = capsys.readouterr().out
    assert "said" in out and "Bash" not in out


def test_search_reads_the_fleet_newest_first_and_stops_where_it_says(
    monkeypatch, capsys
):
    """The cap is the honest part: a query with no hits reads every record of
    every transcript it is given, so the footer names what it did not read."""
    listing = {"sessions": [{"session": f"s{i}"} for i in range(4)]}
    hit = {"matches": [{"seq": 7, "role": "user", "ts": "2026-09-21T00:00:00Z",
                        "block": "text", "matches": 2, "excerpt": "...needle..."}],
           "truncated": False, "total": 9, "scanned": 9, "source": "x"}
    answers = {"/api/transcripts": listing,
               "/api/sessions/s0/transcript/search": hit,
               "/api/sessions/s1/transcript/search": {"matches": []},
               "/api/sessions/s2/transcript/search": {"matches": []}}
    client, code = _run(monkeypatch, answers,
                        ["transcript", "search", "needle", "--max-sessions", "3"])
    out = capsys.readouterr().out
    assert code == 0
    assert "s0:7 user" in out and "...needle..." in out
    assert "1 match(es) in 3 session(s) read" in out
    assert "1 older transcript(s) not read" in out
    assert not any("s3" in path for path in client.asked)


def test_search_reports_nothing_found_as_a_failing_exit(monkeypatch, capsys):
    """So a shell can branch on it: ``claunch transcript search X || echo
    'never said'``."""
    answers = {"/api/transcripts": {"sessions": [{"session": "s1"}]},
               "/api/sessions/s1/transcript/search": {"matches": []}}
    _, code = _run(monkeypatch, answers, ["transcript", "search", "nope"])
    assert code == 1
    assert "0 match(es)" in capsys.readouterr().out


def test_a_named_session_is_searched_without_listing_the_fleet(
    monkeypatch, capsys
):
    answers = {"/api/sessions/s9/transcript/search": {"matches": [], }}
    client, _ = _run(monkeypatch, answers,
                     ["transcript", "search", "x", "--session", "s9"])
    assert client.asked == ["/api/sessions/s9/transcript/search?q=x&limit=20"]


def test_the_command_and_the_route_agree_on_the_wire(
    home, tmp_path, monkeypatch, capsys
):
    """The tests above pin what each command prints, against answers this
    file writes. This one pins that the request a command sends is the one
    the route reads: a renamed query parameter passes all of them and
    answers nothing in a real shell."""
    from claude_launcher import cli

    src = tmp_path / "conv.jsonl"
    _write(src, [_rec("user", "the needle"), _rec("assistant", "hay")])
    monkeypatch.setattr(tv, "locate_transcript", lambda sdef, **kw: src)
    monkeypatch.setattr(tv.paths, "session_dir", lambda name: tmp_path / name)
    known = _Known("s1")

    async def run():
        client = await _client()
        try:
            client.app["manager"].list = lambda: [known]
            client.app["manager"].get = lambda name: known
            monkeypatch.setattr(
                cli_transcript, "_client",
                lambda: daemon_client.DaemonClient(
                    f"http://127.0.0.1:{client.server.port}", "sekrit"),
            )
            parser = cli.build_parser()
            for argv in (
                ["transcript", "ls"],
                ["transcript", "path", "s1"],
                ["transcript", "show", "s1", "--limit", "2"],
                ["transcript", "search", "needle"],
            ):
                args = parser.parse_args(argv)
                # The command is synchronous urllib and the server is on this
                # loop: calling it here would block the server it is calling.
                assert await asyncio.to_thread(args.func, args) == 0, argv
        finally:
            await client.close()

    asyncio.run(run())
    out = capsys.readouterr().out
    assert str(src) in out        # ls and path found the file
    assert "[0] user" in out      # show read a record
    assert "s1:0 user" in out     # search found it and named the seq to read
