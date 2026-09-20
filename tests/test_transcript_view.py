"""The conversation pane's pages, and the index that makes them cheap.

The terminal cannot serve this. A claude session repaints the alternate screen
instead of scrolling it, so its history never reaches a scrollback — measured
on this project's own sessions, 424 KiB of PTY output leaves two lines behind —
and the readable record lives only in claude's own jsonl. These check that the
jsonl is turned into pages a scroller can walk, and that walking it does not
re-read the whole file every time.
"""

from __future__ import annotations

import json

import pytest

from claude_launcher.daemon import transcript_view as tv


class FakeDef:
    """The surface ``locate_transcript`` reads; the tests point it directly."""

    def __init__(self, name="s1"):
        self.name = name


@pytest.fixture
def transcript(tmp_path, monkeypatch):
    """A jsonl in claude's shape, plus a home for the index beside it."""
    src = tmp_path / "conv.jsonl"
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(tv, "locate_transcript", lambda sdef, **kw: src)
    monkeypatch.setattr(tv.paths, "session_dir", lambda name: sessions / name)
    return src


def _rec(kind, content, **extra):
    doc = {"type": kind, "timestamp": "2026-08-25T00:00:00Z",
           "message": {"role": kind, "content": content}}
    doc.update(extra)
    return json.dumps(doc, ensure_ascii=False) + "\n"


def _noise(kind="mode"):
    return json.dumps({"type": kind, "sessionId": "x"}) + "\n"


def _codex(kind, **payload):
    return (
        json.dumps(
            {
                "type": "response_item",
                "timestamp": "2026-08-29T00:00:00Z",
                "payload": {"type": kind, **payload},
            },
            ensure_ascii=False,
        )
        + "\n"
    )


def _write(src, records):
    src.write_text("".join(records), encoding="utf-8")


def test_a_page_carries_conversation_and_drops_the_bookkeeping(transcript):
    """Nine records in ten are mode flips and titles; a reader wants neither."""
    _write(transcript, [
        _noise("mode"),
        _rec("user", "hello"),
        _noise("ai-title"),
        _rec("assistant", [{"type": "text", "text": "hi back"}]),
        _noise("queue-operation"),
    ])
    page = tv.page("s1", FakeDef())
    assert [r["role"] for r in page["records"]] == ["user", "assistant"]
    assert page["records"][0]["blocks"] == [{"type": "text", "text": "hello"}]
    assert page["records"][1]["blocks"][0]["text"] == "hi back"
    assert page["has_more"] is False
    assert page["total"] == 2


def test_paging_walks_backwards_to_the_top(transcript):
    """`before` is the reader's cursor as they scroll up, and `has_more` is
    how the scroller learns to stop asking."""
    _write(transcript, [_rec("user", f"m{i}") for i in range(25)])

    first = tv.page("s1", FakeDef(), limit=10)
    assert [r["blocks"][0]["text"] for r in first["records"]][-1] == "m24"
    assert len(first["records"]) == 10
    assert first["has_more"] is True

    second = tv.page("s1", FakeDef(), before=first["cursor"], limit=10)
    assert [r["blocks"][0]["text"] for r in second["records"]][0] == "m5"
    assert second["has_more"] is True

    third = tv.page("s1", FakeDef(), before=second["cursor"], limit=10)
    assert [r["blocks"][0]["text"] for r in third["records"]][0] == "m0"
    assert third["has_more"] is False, "the top is reached, not looped"


def test_tool_traffic_is_clipped_but_prose_is_not(transcript):
    """A tool_result can be a megabyte of file content; the reader came for
    the prose, and wants only to see that a tool ran."""
    long_prose = "p" * (tv.TOOL_CLIP + 500)
    _write(transcript, [
        _rec("assistant", [
            {"type": "text", "text": long_prose},
            {"type": "tool_use", "name": "Bash", "id": "t1",
             "input": {"command": "x" * (tv.TOOL_CLIP + 500)}},
        ]),
        _rec("user", [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": "o" * (tv.TOOL_CLIP + 500)},
        ]),
    ])
    recs = tv.page("s1", FakeDef())["records"]
    text, use = recs[0]["blocks"]
    assert text["text"] == long_prose, "prose arrives whole"
    assert use["name"] == "Bash"
    assert use["clipped"] is True
    assert len(use["text"]) == tv.TOOL_CLIP
    assert use["full"] > tv.TOOL_CLIP, "it says how much it is not showing"

    result = recs[1]["blocks"][0]
    assert result["type"] == "tool_result" and result["clipped"] is True
    assert result["id"] == "t1"


def test_a_structured_tool_result_keeps_its_text_parts(transcript):
    _write(transcript, [
        _rec("user", [{"type": "tool_result", "tool_use_id": "t9",
                       "content": [{"type": "text", "text": "seen"},
                                   {"type": "image", "source": {}}]}]),
    ])
    block = tv.page("s1", FakeDef())["records"][0]["blocks"][0]
    assert block["text"] == "seen"


def test_thinking_is_carried_but_an_empty_one_is_not(transcript):
    """Redacted thinking arrives as an empty string with a signature; a record
    left with nothing readable is not a record."""
    _write(transcript, [
        _rec("assistant", [{"type": "thinking", "thinking": "", "signature": "x"}]),
        _rec("assistant", [{"type": "thinking", "thinking": "weighing it"}]),
    ])
    recs = tv.page("s1", FakeDef())["records"]
    assert len(recs) == 1
    assert recs[0]["blocks"] == [{"type": "thinking", "text": "weighing it"}]


def test_codex_messages_and_tool_traffic_use_the_shared_page_shape(transcript):
    _write(
        transcript,
        [
            _noise("session_meta"),
            _codex(
                "message",
                role="developer",
                content=[
                    {"type": "input_text", "text": "internal instructions"},
                ],
            ),
            _codex(
                "message",
                role="user",
                content=[
                    {"type": "input_text", "text": "inspect the session"},
                ],
            ),
            _codex("custom_tool_call", name="exec", call_id="c1", input="status"),
            _codex("custom_tool_call_output", call_id="c1", output="working"),
            _codex(
                "message",
                role="assistant",
                content=[
                    {"type": "output_text", "text": "the rollout is readable"},
                ],
            ),
        ],
    )

    page = tv.page("s1", FakeDef())
    assert page["total"] == 4
    assert [r["role"] for r in page["records"]] == [
        "user",
        "assistant",
        "assistant",
        "assistant",
    ]
    assert page["records"][0]["blocks"] == [
        {"type": "text", "text": "inspect the session"},
    ]
    assert page["records"][1]["blocks"][0]["type"] == "tool_use"
    assert page["records"][2]["blocks"][0]["type"] == "tool_result"
    assert page["records"][3]["blocks"][0]["text"] == "the rollout is readable"


# --------------------------------------------------------------------------- #
# the index
# --------------------------------------------------------------------------- #
def test_the_index_extends_rather_than_rescans(transcript):
    """The transcript is append-only and reaches tens of megabytes, so a page
    request must not re-read what it already indexed."""
    _write(transcript, [_rec("user", f"m{i}") for i in range(5)])
    tv.page("s1", FakeDef())
    doc = json.loads(tv.index_path("s1").read_text(encoding="utf-8"))
    assert len(doc["offsets"]) == 5
    first_scan = doc["scanned"]

    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(_rec("user", "m5"))
    page = tv.page("s1", FakeDef())
    doc2 = json.loads(tv.index_path("s1").read_text(encoding="utf-8"))
    assert len(doc2["offsets"]) == 6
    assert doc2["scanned"] > first_scan
    assert doc2["offsets"][:5] == doc["offsets"], "the old offsets stood"
    assert page["records"][-1]["blocks"][0]["text"] == "m5"


def test_a_half_written_record_is_left_for_the_next_pass(transcript):
    """claude appends while the daemon reads. A line without its newline is a
    record still being written; indexing it would pin an offset to a fragment
    that never parses."""
    _write(transcript, [_rec("user", "done")])
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write('{"type":"user","message":{"role":"user","content":"half')
    page = tv.page("s1", FakeDef())
    assert [r["blocks"][0]["text"] for r in page["records"]] == ["done"]

    # Completed, it is picked up — the scan resumes at the byte it stopped on.
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write('"}}\n')
    page2 = tv.page("s1", FakeDef())
    assert [r["blocks"][0]["text"] for r in page2["records"]] == ["done", "half"]


def test_a_replaced_transcript_rebuilds_the_index(transcript):
    """A file shorter than what was already scanned is not the file that was
    indexed; carrying those offsets over would seek into the middle of lines."""
    _write(transcript, [_rec("user", f"m{i}") for i in range(20)])
    tv.page("s1", FakeDef())
    assert len(json.loads(tv.index_path("s1").read_text())["offsets"]) == 20

    _write(transcript, [_rec("user", "fresh")])
    page = tv.page("s1", FakeDef())
    assert page["total"] == 1
    assert page["records"][0]["blocks"][0]["text"] == "fresh"


def test_no_conversation_is_an_empty_page_not_an_error(transcript, monkeypatch):
    """A session with no transcript — a plain shell, a harness that keeps
    none — has nothing to show and must not fail the pane."""
    monkeypatch.setattr(tv, "locate_transcript", lambda sdef, **kw: None)
    page = tv.page("s1", FakeDef())
    assert page == {"records": [], "has_more": False, "total": 0, "source": None}


def test_the_index_fast_path_misses_nothing_it_should_keep(transcript):
    """``_peek_type`` skips ``json.loads`` for lines without a literal
    ``"type":"user"``, because nine records in ten are bookkeeping and the scan
    walks a file that reaches tens of megabytes. A record it wrongly skipped
    would vanish from the pane silently, so the substring test has to agree
    with a real parse — including on the spacing json.dumps produces, and on a
    tool result whose *body* happens to contain the needle.
    """
    spaced = json.dumps(
        {"type": "assistant",
         "message": {"role": "assistant", "content": "spaced out"}},
        indent=None, separators=(", ", ": "),
    ) + "\n"
    nested = _rec("user", [{
        "type": "tool_result", "tool_use_id": "t1",
        # The needle, inside content that belongs to a record whose own type
        # is `user` — the fast path must defer to the parsed top-level type.
        "content": 'a file containing {"type":"assistant"} verbatim',
    }])
    _write(transcript, [
        _noise("file-history-snapshot"),
        spaced,
        nested,
        _noise("attachment"),
    ])
    page = tv.page("s1", FakeDef())
    assert [r["role"] for r in page["records"]] == ["assistant", "user"]
    assert page["total"] == 2, "the bookkeeping stayed out"


def test_a_corrupt_line_costs_only_itself(transcript):
    _write(transcript, [
        _rec("user", "before"),
        '{"type":"user","message":{"role":"user","content":"unclosed\n',
        _rec("user", "after"),
    ])
    texts = [r["blocks"][0]["text"] for r in tv.page("s1", FakeDef())["records"]]
    assert texts == ["before", "after"]


# --------------------------------------------------------------------------- #
# pi
# --------------------------------------------------------------------------- #
#: 2026-09-11T12:46:55.993Z, as pi stamps it: epoch milliseconds.
PI_MS = 1789130815993


def _pi(role, content, **msg):
    """One pi session record — ``type: message`` around an API-shaped body."""
    doc = {
        "type": "message",
        "id": "r1",
        "parentId": None,
        "timestamp": PI_MS,
        "message": {"role": role, "content": content, "timestamp": PI_MS, **msg},
    }
    return json.dumps(doc, ensure_ascii=False) + "\n"


def _pi_noise(kind):
    return json.dumps({"type": kind, "id": "x", "timestamp": PI_MS}) + "\n"


class PiDef(FakeDef):
    harness = "pi"


@pytest.fixture
def pi_transcript(tmp_path, monkeypatch):
    """A jsonl in pi's shape, reached the way a pi session's is: through the
    context gauge's locator, never the briefing's scan."""
    src = tmp_path / "pi.jsonl"
    sessions = tmp_path / "sessions"

    def no_scan(sdef, **kw):  # pragma: no cover - a hit here is the failure
        raise AssertionError("a pi session is not located by scanning")

    monkeypatch.setattr(tv, "locate_transcript", no_scan)
    monkeypatch.setattr(tv.ctxsize, "transcript_of", lambda sdef: src)
    monkeypatch.setattr(tv.paths, "session_dir", lambda name: sessions / name)
    return src


def test_pi_conversation_pages_in_order_and_drops_its_bookkeeping(pi_transcript):
    """The probe's file (claunch-e7ow): session/model/thinking-level records
    first, then user, assistant (text + toolCall), toolResult, assistant."""
    _write(pi_transcript, [
        _pi_noise("session"),
        _pi_noise("model_change"),
        _pi_noise("thinking_level_change"),
        _pi("user", [{"type": "text", "text": "list the files"}]),
        _pi("assistant", [
            {"type": "text", "text": "OK"},
            {"type": "toolCall", "id": "call_00", "name": "bash",
             "arguments": {"command": "ls -la"}},
        ], model="deepseek-flash"),
        _pi("toolResult", [{"type": "text", "text": "total 232"}],
            toolCallId="call_00", toolName="bash", isError=False),
        _pi("assistant", [
            {"type": "thinking", "thinking": "done", "thinkingSignature": "s"},
            {"type": "text", "text": "two files"},
        ]),
    ])
    page = tv.page("p1", PiDef())
    assert page["total"] == 4, "the three bookkeeping records stayed out"
    assert [r["role"] for r in page["records"]] == [
        "user", "assistant", "user", "assistant",
    ]
    user, asst, result, last = page["records"]
    assert user["blocks"] == [{"type": "text", "text": "list the files"}]
    assert [b["type"] for b in asst["blocks"]] == ["text", "tool_use"]
    assert asst["blocks"][1]["name"] == "bash"
    assert asst["blocks"][1]["id"] == "call_00"
    assert json.loads(asst["blocks"][1]["text"]) == {"command": "ls -la"}
    # The tool's output is a record of its own in pi's file; the page hands it
    # over in the shape claude writes for the same event — a user-role record
    # of tool_result blocks — which the pane reads with the assistant.
    assert result["blocks"] == [{
        "type": "tool_result", "id": "call_00", "error": False,
        "text": "total 232", "clipped": False,
    }]
    assert [b["type"] for b in last["blocks"]] == ["thinking", "text"]
    assert last["sidechain"] is False


def test_pi_timestamps_arrive_as_iso_like_every_other_harness(pi_transcript):
    """Pi stamps epoch milliseconds; the pane's clock parses ISO-8601."""
    _write(pi_transcript, [_pi("user", [{"type": "text", "text": "hi"}])])
    rec = tv.page("p1", PiDef())["records"][0]
    assert rec["ts"] == "2026-09-11T12:46:55.993Z"
    assert tv._pi_timestamp("not-a-number") == "not-a-number"
    assert tv._pi_timestamp(None) == ""


def test_pi_tool_output_is_clipped_and_keeps_its_error_flag(pi_transcript):
    big = "o" * (tv.TOOL_CLIP + 500)
    _write(pi_transcript, [
        _pi("toolResult", [{"type": "text", "text": big},
                           {"type": "image", "data": "..."}],
            toolCallId="call_07", toolName="read", isError=True),
    ])
    block = tv.page("p1", PiDef())["records"][0]["blocks"][0]
    assert block["type"] == "tool_result"
    assert block["error"] is True
    assert block["clipped"] is True and block["full"] == len(big)
    assert len(block["text"]) == tv.TOOL_CLIP


def test_a_pi_record_with_nothing_readable_is_not_a_record(pi_transcript):
    """A role the reader is not shown, or a body that is all whitespace."""
    _write(pi_transcript, [
        _pi("system", [{"type": "text", "text": "hidden"}]),
        _pi("assistant", [{"type": "thinking", "thinking": "   "}]),
        _pi("user", "   "),
        _pi("user", "kept"),
    ])
    page = tv.page("p1", PiDef())
    assert page["total"] == 3, "the index keeps the roles it shows"
    assert [r["blocks"][0]["text"] for r in page["records"]] == ["kept"]


def test_a_pi_session_without_a_file_yet_is_an_empty_page(pi_transcript, monkeypatch):
    """The path is a function of (home, cwd, id); before pi's first write it
    simply is not there, and the pane must not fail."""
    monkeypatch.setattr(tv.ctxsize, "transcript_of", lambda sdef: None)
    assert tv.page("p1", PiDef()) == {
        "records": [], "has_more": False, "total": 0, "source": None,
    }


def test_a_claude_record_mentioning_a_pi_message_type_is_still_claude(transcript):
    """``"type":"message"`` inside a tool result's body must not turn a claude
    user record into a pi projection — the parsed top-level type decides."""
    _write(transcript, [
        _rec("user", [{
            "type": "tool_result", "tool_use_id": "t1",
            "content": 'a pi file line: {"type":"message","message":{"role":"user"}}',
        }]),
        _rec("assistant", [{"type": "text", "text": "seen"}]),
    ])
    page = tv.page("s1", FakeDef())
    assert [r["role"] for r in page["records"]] == ["user", "assistant"]
    assert page["records"][0]["blocks"][0]["type"] == "tool_result"
    assert page["records"][0]["blocks"][0]["id"] == "t1"
