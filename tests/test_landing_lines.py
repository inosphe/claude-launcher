"""The landing report's (a)~(e) lines, extracted from a sweep's own output.

A landing report used to be typed. Typing it is where the values drifted: a
count copied from the wrong run, a "none" written for a block the tool never
prints, a tool version named from memory. ``tools/landing_lines.py`` reads the
two streams instead and writes the five lines from what is in them.

What these cases pin is not the wording. It is the one distinction the script
exists for -- *why* a line is missing:

* the rule ran and had nothing to say  -> the tool's own clean line, quoted
* the version has no such rule         -> "측정 안 됨" **plus that version's blob**
* there is no output to read at all    -> no report, exit 1, and the reason

The last one is why the script is in ``tools/`` rather than a scratchpad. An
earlier version answered an empty file with five cheerful "없음" lines and exit
0, which reads downstream as coverage: nobody can tell "we looked and found
nothing" from "we never looked". Two cases below hold that door shut, and one
holds the other half -- a "측정 안 됨" that does not name the version it read is
refused, because nobody can disprove it.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "landing_lines.py"


def _load():
    """``tools/`` is not a package -- load the script the way a script is."""
    spec = importlib.util.spec_from_file_location("landing_lines", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


landing_lines = _load()

TREE = "ced58295b32bf747e88cb0c221200e8c8604585a"
TOOLREF = "37f7dd0:tools/changed_tests.py"
CAND = "3d41ad1abc9defabc9defabc9defabc9defabcde"
TIP = "4652b98abc9defabc9defabc9defabc9defabcde"

#: the shape 37f7dd0 prints -- both clean lines are unconditional there.
CLEAN_OUT = """\
$ uv run --no-sync python tools/changed_tests.py --list
15 test module(s) selected
  tests/test_cli.py
  tests/test_store.py
all 9 changed path(s) map to at least one test module.
no test module sits one import hop outside this selection.
"""

#: the shape a version prints when it does have something to say.
NOISY_OUT = """\
12 test module(s) selected
WARNING: 2 changed path(s) map to no test module:
  uv.lock
  docs/notes.md
  Check by hand
NOTE: 1 test module sits one import hop outside this selection:
  tests/test_daemon_wedge.py
"""

#: 4652b98 prints neither clean line and has no hop rule at all.
BARE_OUT = "22 test module(s) selected\n  tests/test_sweep.py\n"

#: enough of a tool source for the script to read rules out of.
SRC_WITH_HOP = (
    "def select(...):\n    ...\n"
    "def importers(...):\n    ...\n"
    "def mentioning(...):\n    ...\n"
    "EXPLICIT_GUARDS = {}\n"
    "def relations_tried(...):\n    ...\n"
    "reached_indirectly = 1\n"
)
SRC_WITHOUT_HOP = (
    "def select(...):\n    ...\n"
    "def importers(...):\n    ...\n"
    "def mentioning(...):\n    ...\n"
    "EXPLICIT_GUARDS = {}\n"
)


def _files(tmp_path, out, err="", src=None):
    (tmp_path / "out.txt").write_text(out, encoding="utf-8")
    (tmp_path / "err.txt").write_text(err, encoding="utf-8")
    argv = [str(tmp_path / "out.txt"), str(tmp_path / "err.txt"), TREE, TOOLREF]
    if src is not None:
        (tmp_path / "tool.py").write_text(src, encoding="utf-8")
        argv.append(str(tmp_path / "tool.py"))
    return argv


def test_a_clean_run_quotes_the_tools_own_two_lines(tmp_path, capsys):
    """(c) and (d) are the tool's sentences, not a summary of them."""
    rc = landing_lines.main(_files(tmp_path, CLEAN_OUT, src=SRC_WITH_HOP))
    out = capsys.readouterr().out
    assert rc == 0
    assert "(b) 선택       : 15 test module(s) selected" in out
    assert "(c) 못 맞춘 경로: [stdout] all 9 changed path(s) map to at least one test module." in out
    assert "(d) 한 홉 밖    : [stdout] no test module sits one import hop outside this selection." in out
    assert "한 홉 규칙(2b 한 칸 밖 보고): 있다" in out


def test_warning_and_note_blocks_carry_their_bodies(tmp_path, capsys):
    """The paths under a WARNING are the evidence -- the head line alone is not."""
    rc = landing_lines.main(_files(tmp_path, NOISY_OUT, src=SRC_WITH_HOP))
    out = capsys.readouterr().out
    assert rc == 0
    assert "(c) 못 맞춘 경로: [stdout] WARNING: 2 changed path(s) map to no test module:" in out
    assert "uv.lock" in out and "docs/notes.md" in out
    assert "(d) 한 홉 밖    : [stdout] NOTE: 1 test module sits one import hop outside" in out
    assert "tests/test_daemon_wedge.py" in out
    # "Check by hand" ends the block: it is the tool's advice, not a path.
    assert "                  Check by hand" not in out


def test_a_warning_on_stderr_is_labelled_as_stderr(tmp_path, capsys):
    """Which stream a verdict arrived on is itself a value in this repository."""
    rc = landing_lines.main(
        _files(
            tmp_path,
            BARE_OUT,
            err="WARNING: 1 changed path(s) map to no test module:\n  uv.lock\n",
            src=SRC_WITH_HOP,
        )
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "(c) 못 맞춘 경로: [stderr] WARNING:" in out


def test_a_version_without_the_hop_rule_says_so_and_names_its_blob(tmp_path, capsys):
    """A version with no such rule did not measure the axis, and must name itself."""
    argv = _files(tmp_path, BARE_OUT, src=SRC_WITHOUT_HOP) + [CAND, TIP]
    rc = landing_lines.main(argv)
    out = capsys.readouterr().out
    assert rc == 0
    assert "(d) 한 홉 밖    : **측정 안 됨**" in out
    assert CAND[:12] in out
    assert "사슬 tip 과 다르다" in out
    assert "한 홉 규칙(2b 한 칸 밖 보고): **이 판의 도구에 없다**" in out


def test_measured_nothing_without_a_blob_is_refused(tmp_path, capsys):
    """A "측정 안 됨" nobody can check is worth less than no line at all."""
    rc = landing_lines.main(_files(tmp_path, BARE_OUT, src=SRC_WITHOUT_HOP))
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "근거 없음" in captured.err and "6번째 인자" in captured.err


def test_an_empty_stdout_produces_no_report(tmp_path, capsys):
    """The failure this script was written for: five "없음" lines over nothing."""
    rc = landing_lines.main(_files(tmp_path, "", src=SRC_WITH_HOP))
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "test module(s) selected" in captured.err
    assert "0바이트" in captured.err


def test_a_missing_file_is_not_an_empty_file(tmp_path, capsys):
    """Absent and empty are different claims -- and neither one is a traceback."""
    rc = landing_lines.main([str(tmp_path / "gone.txt"), str(tmp_path / "err.txt"), TREE, TOOLREF])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "stdout 파일이 없다" in captured.err
    assert "Traceback" not in captured.err


def test_without_a_tool_copy_the_relations_line_says_it_was_not_measured(tmp_path, capsys):
    """(a) names the version it read, or admits it read none."""
    rc = landing_lines.main(_files(tmp_path, CLEAN_OUT))
    out = capsys.readouterr().out
    assert rc == 0
    assert "**도구 사본을 안 줘서 안 쟀다**" in out
    assert "한 홉 규칙" not in out


def test_the_script_runs_as_a_script(tmp_path):
    """``tools/`` scripts are invoked by path, so the entry point is part of the contract."""
    argv = _files(tmp_path, CLEAN_OUT, src=SRC_WITH_HOP)
    proc = subprocess.run([sys.executable, str(TOOL), *argv], capture_output=True, text=True)
    assert proc.returncode == 0
    assert "(e) 읽은 곳     : stdout " in proc.stdout
    missing = subprocess.run(
        [sys.executable, str(TOOL), str(tmp_path / "gone.txt"), str(tmp_path / "err.txt"), TREE, TOOLREF],
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 1
    assert missing.stdout == ""
