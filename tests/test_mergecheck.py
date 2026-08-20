"""The semantic-dependency check: what a clean text merge is allowed to hide.

The accident this exists for happened here. One branch deleted ``_wait_idle``
from ``tests/test_session_queued_api.py``; another added three calls to it in
the same file. ``git merge-tree`` reported no conflict, and a reviewer looking
for conflicts would have found nothing to look at.

So the fixture is that accident's *shape*, built in a throwaway repository:
a helper, a side that removes it, a side that starts calling it. Built rather
than pinned to the two real commits, because those live on branches that will
land and could be pruned — a regression test that dissolves when the history
is tidied is not a regression test. (The real pair is checked too, and skipped
when it is no longer reachable.)

The cases below are mostly about **not** crying wolf. A checker that reports a
moved function, or a helper the same branch brought with it, is one people
learn to pass over — and this one only ever runs at the moment nobody wants
another thing to read.
"""

from __future__ import annotations

import subprocess

import pytest

from claude_launcher import mergecheck


# --------------------------------------------------------------------------- #
# one repository, every scenario
# --------------------------------------------------------------------------- #
# Built once for the whole module, not once per test. Each scenario is a pair
# of branches off the same root, so they do not interfere -- and the check is
# a read-only question, so nothing here needs a repository of its own. That
# matters: on Windows every git call is a process, a per-test repo costs ~10
# of them, and this suite runs inside a sweep the whole fleet queues for.
def _git(repo, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _commit(repo, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _write(repo, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _scenario(repo, name: str, base_files: dict, left: dict, right: dict) -> None:
    """Two branches -- ``name``-l and ``name``-r -- over a shared root.

    ``base_files`` is written and committed on a root branch of its own, so
    scenarios never see each other's files; the two sides then rewrite it.
    """
    _git(repo, "checkout", "-q", "--orphan", name + "-base")
    _git(repo, "rm", "-rqf", "--ignore-unmatch", ".")
    for path, text in base_files.items():
        _write(repo, path, text)
    _commit(repo, f"{name}: base")
    _git(repo, "checkout", "-q", "-b", name + "-l")
    for path, text in left.items():
        _write(repo, path, text)
    _commit(repo, f"{name}: left")
    _git(repo, "checkout", "-q", "-b", name + "-r", name + "-base")
    for path, text in right.items():
        _write(repo, path, text)
    _commit(repo, f"{name}: right")


HELPER = "async def _wait_idle(session):\n    return session\n\n\n"
TEST_ONE = "def test_one():\n    assert True\n"
CALLER = "\n\nasync def test_two(session):\n    await _wait_idle(session)\n"
Q = "tests/test_queue.py"


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    """Every scenario, in one repository, built once.

    Each is a pair of branches (``<name>-l`` / ``<name>-r``) over its own
    orphan root, so scenarios cannot see each other's files.
    """
    path = tmp_path_factory.mktemp("mergecheck")
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")

    # 1. The accident: the helper goes on one side, the calls arrive on the other.
    _scenario(
        path, "accident",
        {Q: HELPER + TEST_ONE},
        {Q: TEST_ONE},                       # left drops the helper
        {Q: HELPER + TEST_ONE + CALLER},     # right starts calling it
    )
    # 2. The fix the real branch made: keep the rewrite, put the helper back.
    _scenario(
        path, "restored",
        {Q: HELPER + TEST_ONE},
        {Q: HELPER + TEST_ONE + "\n\ndef test_three():\n    assert True\n"},
        {Q: HELPER + TEST_ONE + CALLER},
    )
    # 3. A helper of the same name in ANOTHER module resolves nothing here.
    _scenario(
        path, "elsewhere",
        {Q: HELPER + TEST_ONE},
        {Q: TEST_ONE, "tests/test_other.py": HELPER + TEST_ONE},
        {Q: HELPER + TEST_ONE + CALLER},
    )
    # 4. ...but a second definition in the SAME file carries the merged file.
    _scenario(
        path, "samefile",
        {Q: HELPER + TEST_ONE},
        {Q: TEST_ONE + "\n\nasync def _wait_idle(session):\n    return None\n"},
        {Q: HELPER + TEST_ONE + CALLER},
    )
    # 5. A move: the definition is deleted and added back in the same file.
    _scenario(
        path, "moved",
        {"app.py": "def helper():\n    return 1\n\n\ndef main():\n    return helper()\n"},
        {"app.py": "def main():\n    return helper()\n\n\ndef helper():\n    return 1\n"},
        {"app.py": "def helper():\n    return 1\n\n\ndef main():\n    return helper()\n"
                   "\n\ndef other():\n    return helper()\n"},
    )
    # 6. A side that brings both the call and the callee owes nobody.
    _scenario(
        path, "selfsufficient",
        {"app.py": "def gone():\n    return 1\n"},
        {"app.py": "x = 1\n"},
        {"app.py": "def gone():\n    return 1\n\n\ndef mine():\n    return 2\n"
                   "\n\ndef use():\n    return mine()\n"},
    )
    # 7. Two branches that never meet.
    _scenario(
        path, "untouched",
        {"a.py": "def one():\n    return 1\n", "b.py": "def two():\n    return 2\n"},
        {"a.py": "def one():\n    return 11\n", "b.py": "def two():\n    return 2\n"},
        {"a.py": "def one():\n    return 1\n",
         "b.py": "def two():\n    return 2\n\n\ndef three():\n    return two()\n"},
    )
    # 8. The dashboard's app.js is one script, so the same rule applies to it.
    _scenario(
        path, "js",
        {"app.js": "function fmt(x) { return x; }\nfunction main() { return fmt(1); }\n"},
        {"app.js": "function main() { return 1; }\n"},
        {"app.js": "function fmt(x) { return x; }\nfunction main() { return fmt(1); }\n"
                   "function other() { return fmt(2); }\n"},
    )
    return path


def _symbols(repo, name: str):
    return [
        f.symbol
        for f in mergecheck.check_pair(f"{name}-l", f"{name}-r", cwd=str(repo))
    ]


# --------------------------------------------------------------------------- #
# the accident itself
# --------------------------------------------------------------------------- #
def test_the_accident_is_caught(repo):
    """Neither branch conflicts textually, and the merge is still broken."""
    # First: git really does consider this clean, or the fixture is not the
    # accident. That is the whole premise, so it is asserted, not assumed.
    merged = subprocess.run(
        ["git", "merge-tree", "--write-tree", "accident-l", "accident-r"],
        cwd=str(repo), capture_output=True, text=True,
    )
    assert merged.returncode == 0, "fixture is not a clean merge: " + merged.stdout

    findings = mergecheck.check_pair("accident-l", "accident-r", cwd=str(repo))
    assert [f.symbol for f in findings] == ["_wait_idle"]
    assert findings[0].deleted_by == "accident-l"
    assert findings[0].called_by == "accident-r"
    assert findings[0].deleted_in == [Q]


def test_direction_does_not_matter(repo):
    """Whoever lands first makes the other one the deleter."""
    back = mergecheck.check_pair("accident-r", "accident-l", cwd=str(repo))
    assert [f.symbol for f in back] == ["_wait_idle"]
    assert back[0].deleted_by == "accident-l"


def test_restoring_the_helper_clears_it(repo):
    assert _symbols(repo, "restored") == []


# --------------------------------------------------------------------------- #
# what must NOT be reported, and one that must
# --------------------------------------------------------------------------- #
def test_a_same_named_helper_elsewhere_does_not_clear_it(repo):
    """The bug this check first had, pinned so it cannot come back.

    A top-level name belongs to its module: a ``_wait_idle`` in another test
    file resolves nothing for this one. Asked tree-wide, the check cleared the
    very accident it exists for -- and the real repository has exactly that
    second definition, so this is not a hypothetical.
    """
    assert _symbols(repo, "elsewhere") == ["_wait_idle"]


def test_a_second_definition_in_the_same_file_clears_it(repo):
    assert _symbols(repo, "samefile") == []


def test_a_moved_definition_is_not_a_loss(repo):
    assert _symbols(repo, "moved") == []


def test_a_side_that_brings_its_own_helper_owes_nobody(repo):
    assert _symbols(repo, "selfsufficient") == []


def test_untouched_symbols_are_nobodys_business(repo):
    assert _symbols(repo, "untouched") == []


def test_javascript_is_read_too(repo):
    """A one-line JS function both defines and calls -- the body is on the line."""
    assert _symbols(repo, "js") == ["fmt"]


# --------------------------------------------------------------------------- #
# the report a reviewer reads
# --------------------------------------------------------------------------- #
def test_report_says_so_either_way_and_gates_on_findings(repo):
    """A checker that prints nothing on success reads as one that did not run."""
    risky = mergecheck.check_batch(["accident-l", "accident-r"], cwd=str(repo))
    text, code = mergecheck.report(risky)
    assert code == 1
    assert "RISK" in text and "_wait_idle" in text

    clear = mergecheck.check_batch(["moved-l", "moved-r"], cwd=str(repo))
    clear_text, clear_code = mergecheck.report(clear)
    assert clear_code == 0
    assert "ok" in clear_text

    # ASCII only: this prints into consoles whose code page cannot carry more,
    # and one character outside it raises rather than garbles. It did, once.
    assert text.isascii() and clear_text.isascii()


def test_a_batch_checks_every_pair(repo):
    results = mergecheck.check_batch(
        ["accident-base", "accident-l", "accident-r"], cwd=str(repo)
    )
    assert len(results) == 3          # 3 choose 2
    assert [(a, b) for a, b, f in results if f] == [("accident-l", "accident-r")]


def test_an_unknown_ref_is_refused_not_guessed(repo):
    with pytest.raises(mergecheck.MergeCheckError):
        mergecheck.check_pair("accident-l", "no-such-branch", cwd=str(repo))


# --------------------------------------------------------------------------- #
# the accident as it actually happened
# --------------------------------------------------------------------------- #
def test_the_real_commits():
    """The two commits from this repository's own near-miss, while they last.

    Skipped rather than failed once they are unreachable: the fixtures above
    carry the regression, and this is here because a check built from a real
    incident should be run against it at least once.
    """
    import pathlib

    here = pathlib.Path(__file__).resolve().parent.parent
    for ref in ("41fcfc8", "744e88d"):
        probe = subprocess.run(
            ["git", "cat-file", "-e", ref + "^{commit}"],
            cwd=str(here), capture_output=True,
        )
        if probe.returncode != 0:
            pytest.skip(f"{ref} is no longer in this repository")

    findings = mergecheck.check_pair("41fcfc8", "744e88d", cwd=str(here))
    assert [f.symbol for f in findings] == ["_wait_idle"]
    assert findings[0].deleted_in == ["tests/test_session_queued_api.py"]
