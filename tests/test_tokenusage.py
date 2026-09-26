"""The token usage the dashboard shows for each session.

``daemon/tokenusage.py`` totals every model request of a session's current
conversation, per harness: Claude transcripts (one count per message id,
subagents apart), Codex rollouts (the newest cumulative total) and Pi session
files (summed, with the recorded cost). The tests cover counting each request
once, keeping the four components apart, reading only what was appended,
handing a long first read to the background, and absence staying absence.
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest

from claude_launcher import harnesses, profile as profile_mod, transcripts
from claude_launcher.daemon import ctxsize, tokenusage
from claude_launcher.daemon.harness import SessionDef, pi_session_file

CID = "beef0000-0000-0000-0000-0000000000aa"


@pytest.fixture(autouse=True)
def _fresh():
    ctxsize.forget()
    tokenusage.forget()
    yield
    tokenusage.forget()
    ctxsize.forget()


def claude_line(*, mid="msg_1", fresh=0, read=0, write=0, out=0,
                model="claude-opus-5", at="2026-09-27T00:00:00.000Z",
                side=False) -> str:
    return json.dumps({
        "type": "assistant",
        "isSidechain": side,
        "timestamp": at,
        "requestId": "req_" + mid,
        "message": {
            "id": mid,
            "role": "assistant",
            "model": model,
            "usage": {
                "input_tokens": fresh,
                "cache_read_input_tokens": read,
                "cache_creation_input_tokens": write,
                "output_tokens": out,
            },
        },
    })


def codex_total(*, input_tokens, cached, out, reasoning=0,
                at="2026-09-27T00:00:00Z") -> str:
    usage = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": 0,
        "output_tokens": out,
        "reasoning_output_tokens": reasoning,
        "total_tokens": input_tokens + out,
    }
    return json.dumps({
        "timestamp": at,
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {
            "total_token_usage": usage,
            "last_token_usage": usage,
            "model_context_window": 258_400,
        }},
    })


def codex_context(model="gpt-5.6-sol") -> str:
    return json.dumps({"timestamp": "2026-09-27T00:00:00Z",
                       "type": "turn_context",
                       "payload": {"turn_id": "t1", "model": model}})


def pi_line(*, eid, fresh=0, read=0, write=0, out=0, cost=0.0,
            model="deepseek-flash", role="assistant") -> str:
    msg = {"role": role, "content": [{"type": "text", "text": "OK"}]}
    if role == "assistant":
        msg.update({"model": model, "usage": {
            "input": fresh, "output": out, "cacheRead": read, "cacheWrite": write,
            "totalTokens": fresh + read + write + out,
            "cost": {"total": cost},
        }})
    return json.dumps({"type": "message", "id": eid,
                       "timestamp": "2026-09-27T00:00:00.000Z", "message": msg})


def write(path: pathlib.Path, *lines: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def append(path: pathlib.Path, *lines: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# claude
# --------------------------------------------------------------------------- #
def test_claude_counts_a_response_written_as_several_lines_once(tmp_path):
    """Claude writes one line per content block and repeats the usage on
    each; summing lines would count a response once per block."""
    path = write(tmp_path / "c.jsonl",
                 claude_line(mid="a", fresh=10, read=1000, write=50, out=7),
                 claude_line(mid="a", fresh=10, read=1000, write=50, out=7),
                 claude_line(mid="a", fresh=10, read=1000, write=50, out=7),
                 claude_line(mid="b", fresh=2, read=1100, out=3))

    got = tokenusage.read_file(path, "claude")

    assert got["requests"] == 2
    assert (got["input"], got["cache_read"], got["cache_write"], got["output"]) \
        == (12, 2100, 50, 10)
    assert got["total"] == 12 + 2100 + 50 + 10
    assert got["model"] == "claude-opus-5"
    assert "subagents" not in got


def test_claude_sidechain_turns_are_subagent_spend(tmp_path):
    path = write(tmp_path / "c.jsonl",
                 claude_line(mid="a", read=100, out=1),
                 claude_line(mid="s", read=40, out=2, side=True))

    got = tokenusage.read_file(path, "claude")

    assert got["requests"] == 1 and got["cache_read"] == 100
    assert got["subagents"]["requests"] == 1
    assert got["subagents"]["cache_read"] == 40


def test_claude_subagent_files_are_totalled_beside_the_conversation(tmp_path):
    """Newer Claude Code writes each subagent to
    ``<conversation>/subagents/agent-*.jsonl`` next to the transcript."""
    path = write(tmp_path / f"{CID}.jsonl", claude_line(mid="a", read=100, out=1))
    write(tmp_path / CID / "subagents" / "agent-1.jsonl",
          claude_line(mid="x", read=30, out=4, side=True))
    write(tmp_path / CID / "subagents" / "agent-2.jsonl",
          claude_line(mid="y", fresh=5, out=6))

    got = tokenusage.read_file(path, "claude")

    assert got["requests"] == 1 and got["total"] == 101
    assert got["subagents"]["requests"] == 2
    assert got["subagents"]["total"] == 30 + 4 + 5 + 6


def test_lines_that_are_not_usage_are_skipped(tmp_path):
    path = write(tmp_path / "c.jsonl",
                 json.dumps({"type": "user", "message": {"content": "usage"}}),
                 "not json at all \"usage\"",
                 claude_line(mid="z"),       # a response reporting nothing
                 claude_line(mid="a", out=5))

    got = tokenusage.read_file(path, "claude")

    assert got["requests"] == 1 and got["output"] == 5


def test_a_transcript_with_no_request_reads_as_not_known(tmp_path):
    path = write(tmp_path / "c.jsonl", json.dumps({"type": "user"}))
    assert tokenusage.read_file(path, "claude") is None


def test_only_what_was_appended_is_read_again(tmp_path, monkeypatch):
    path = write(tmp_path / "c.jsonl", claude_line(mid="a", read=100, out=1))
    assert tokenusage.read_file(path, "claude")["requests"] == 1

    fed = []
    orig = tokenusage.ClaudeReader.feed
    monkeypatch.setattr(tokenusage.ClaudeReader, "feed",
                        lambda self, e: (fed.append(e), orig(self, e)))
    assert tokenusage.read_file(path, "claude")["requests"] == 1
    assert fed == []                 # unchanged file: nothing parsed

    append(path, claude_line(mid="b", read=200, out=2))
    got = tokenusage.read_file(path, "claude")
    assert len(fed) == 1             # only the new line
    assert got["requests"] == 2 and got["cache_read"] == 300


def test_a_line_still_being_written_is_counted_once_it_is_whole(tmp_path):
    path = write(tmp_path / "c.jsonl", claude_line(mid="a", out=1))
    whole = claude_line(mid="b", out=2)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(whole[:20])
    assert tokenusage.read_file(path, "claude")["requests"] == 1
    with path.open("a", encoding="utf-8") as fh:
        fh.write(whole[20:] + "\n")
    got = tokenusage.read_file(path, "claude")
    assert got["requests"] == 2 and got["output"] == 3


def test_a_truncated_file_is_counted_again_from_the_start(tmp_path):
    path = write(tmp_path / "c.jsonl",
                 claude_line(mid="a", out=100), claude_line(mid="b", out=100))
    assert tokenusage.read_file(path, "claude")["output"] == 200
    write(path, claude_line(mid="c", out=1))
    got = tokenusage.read_file(path, "claude")
    assert got["requests"] == 1 and got["output"] == 1


def test_a_long_first_read_is_finished_in_the_background(tmp_path, monkeypatch):
    """A poll reads at most INLINE_BUDGET bytes; the rest goes to the
    catch-up thread and the reading says ``partial`` until it is done."""
    lines = [claude_line(mid=f"m{i}", read=100, out=1) for i in range(200)]
    path = write(tmp_path / "c.jsonl", *lines)
    monkeypatch.setattr(tokenusage, "CHUNK", 1024)
    monkeypatch.setattr(tokenusage, "INLINE_BUDGET", 2048)

    first = tokenusage.read_file(path, "claude")
    assert first["partial"] is True
    assert first["requests"] < 200

    tokenusage.drain(timeout=10)
    done = tokenusage.read_file(path, "claude")
    assert "partial" not in done
    assert done["requests"] == 200 and done["output"] == 200


def test_subagent_files_past_the_budget_are_not_read_by_the_poll(tmp_path, monkeypatch):
    path = write(tmp_path / f"{CID}.jsonl", claude_line(mid="a", out=1))
    for i in range(5):
        write(tmp_path / CID / "subagents" / f"agent-{i}.jsonl",
              claude_line(mid=f"s{i}", out=10))
    monkeypatch.setattr(tokenusage, "INLINE_BUDGET", 0)

    first = tokenusage.read_file(path, "claude")
    assert first["partial"] is True

    tokenusage.drain(timeout=10)
    done = tokenusage.read_file(path, "claude")
    assert "partial" not in done
    assert done["requests"] == 1
    assert done["subagents"]["requests"] == 5 and done["subagents"]["output"] == 50


# --------------------------------------------------------------------------- #
# codex
# --------------------------------------------------------------------------- #
def test_codex_takes_the_newest_cumulative_total(tmp_path):
    """Values from a real rollout (2026-08-27): input 8,795,022 of which
    8,686,336 cached, output 15,800 -- total_tokens 8,810,822."""
    path = write(tmp_path / "r.jsonl",
                 codex_context(),
                 codex_total(input_tokens=1000, cached=800, out=10),
                 codex_total(input_tokens=1000, cached=800, out=10),   # repeated
                 codex_total(input_tokens=8_795_022, cached=8_686_336,
                             out=15_800, reasoning=3_198))

    got = tokenusage.read_file(path, "codex")

    assert got["input"] == 8_795_022 - 8_686_336
    assert got["cache_read"] == 8_686_336
    assert got["output"] == 15_800
    assert got["total"] == 8_810_822
    assert got["requests"] == 2
    assert got["reasoning"] == 3_198
    assert got["model"] == "gpt-5.6-sol"


def test_codex_with_no_token_event_reads_as_not_known(tmp_path):
    path = write(tmp_path / "r.jsonl", codex_context())
    assert tokenusage.read_file(path, "codex") is None


# --------------------------------------------------------------------------- #
# pi
# --------------------------------------------------------------------------- #
def test_pi_sums_assistant_turns_and_their_recorded_cost(tmp_path):
    path = write(tmp_path / "p.jsonl",
                 json.dumps({"type": "session", "id": CID}),
                 pi_line(eid="u1", role="user"),
                 pi_line(eid="a1", fresh=1694, read=384, out=417, cost=0.25),
                 pi_line(eid="a2", fresh=10, read=2000, write=5, out=20, cost=0.5))

    got = tokenusage.read_file(path, "pi")

    assert (got["input"], got["cache_read"], got["cache_write"], got["output"]) \
        == (1704, 2384, 5, 437)
    assert got["requests"] == 2
    assert got["cost"] == pytest.approx(0.75)


def test_pi_zero_cost_is_not_published(tmp_path):
    path = write(tmp_path / "p.jsonl", pi_line(eid="a1", fresh=5, out=1))
    assert "cost" not in tokenusage.read_file(path, "pi")


# --------------------------------------------------------------------------- #
# the registry and the session
# --------------------------------------------------------------------------- #
def test_a_harness_without_a_reader_has_no_reading(tmp_path):
    path = write(tmp_path / "k.jsonl", claude_line(mid="a", out=1))
    assert tokenusage.read_file(path, "kimi") is None
    assert tokenusage.for_session(SessionDef(
        name="k", harness="kimi", cwd=str(tmp_path), conversation_id=CID)) is None


def test_a_registered_reader_is_used_for_its_harness(tmp_path, monkeypatch):
    class Lines(tokenusage.UsageReader):
        harness = "lines"

        def feed(self, entry):
            self.tally.add(output=int(entry.get("n", 0)))

    monkeypatch.setitem(tokenusage.READERS, "lines", Lines)
    path = write(tmp_path / "l.jsonl", json.dumps({"n": 3}), json.dumps({"n": 4}))

    got = tokenusage.read_file(path, "lines")

    assert got["harness"] == "lines"
    assert got["output"] == 7 and got["requests"] == 2


def _claude_session(tmp_path, *lines):
    cwd = tmp_path / "work"
    cwd.mkdir(exist_ok=True)
    pdir = transcripts.project_dir(tmp_path / ".claude-config", str(cwd))
    write(pdir / f"{CID}.jsonl", *lines)
    return SessionDef(name="s1", harness="claude", cwd=str(cwd), conversation_id=CID)


def test_a_claude_session_is_read_through_its_transcript(home, tmp_path):
    sdef = _claude_session(tmp_path, claude_line(mid="a", read=42_000, out=300))
    got = tokenusage.for_session(sdef)
    assert got["harness"] == "claude" and got["cache_read"] == 42_000


def test_a_codex_session_is_read_through_its_rollout(home, tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    prof = profile_mod.create("codex-usage")
    entry = harnesses.get("codex")
    rollout = (entry.profile_home(prof.config_dir) / "sessions" / "2026" / "09"
               / "27" / f"rollout-2026-09-27T00-00-00-{CID}.jsonl")
    meta = json.dumps({"timestamp": "2026-09-27T00:00:00Z", "type": "session_meta",
                       "payload": {"id": CID, "cwd": str(cwd)}})
    write(rollout, meta, codex_context(),
          codex_total(input_tokens=500, cached=400, out=9))
    sdef = SessionDef(name="cx", profile="codex-usage:codex", harness="codex",
                      cwd=str(cwd), conversation_id=CID)

    got = tokenusage.for_session(sdef)

    assert got["harness"] == "codex" and got["cache_read"] == 400
    assert got["input"] == 100 and got["output"] == 9


def test_a_pi_session_is_read_through_its_pinned_file(home, tmp_path):
    cwd = pathlib.Path(tmp_path.drive + os.sep)
    prof = profile_mod.create("pi-usage")
    entry = harnesses.get("pi")
    path = pathlib.Path(pi_session_file(
        str(entry.profile_home(prof.config_dir)), str(cwd), CID))
    write(path, json.dumps({"type": "session", "id": CID}),
          pi_line(eid="a1", fresh=7, read=70, out=3))
    sdef = SessionDef(name="pi", profile="pi-usage:pi", harness="pi",
                      cwd=str(cwd), conversation_id=CID)

    got = tokenusage.for_session(sdef)

    assert got["harness"] == "pi" and got["total"] == 80


def test_attach_hangs_the_reading_and_omits_it_when_unknown(home, tmp_path):
    sdef = _claude_session(tmp_path, claude_line(mid="a", read=10, out=1))
    info = tokenusage.attach({"name": "s1"}, sdef)
    assert info["token_usage"]["total"] == 11

    none = tokenusage.attach({"name": "s2"}, SessionDef(
        name="s2", harness="claude", cwd=str(tmp_path)))
    assert "token_usage" not in none


def test_attach_survives_a_reader_that_raises(home, tmp_path, monkeypatch):
    sdef = _claude_session(tmp_path, claude_line(mid="a", out=1))

    def boom(entry):
        raise RuntimeError("bad reader")

    monkeypatch.setattr(tokenusage, "for_session", lambda s: boom(s))
    assert tokenusage.attach({"name": "s1"}, sdef) == {"name": "s1"}
