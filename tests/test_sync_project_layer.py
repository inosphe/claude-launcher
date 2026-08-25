"""The project layer is the package plus its grafted fields — regenerated, not typed.

``tools/sync_project_layer.py`` is the procedure AGENTS.md names for
propagating a packaged workflow edit into this repository's overrides. The
graft is pinned on small synthetic texts, and then the real overrides are
checked against their own regeneration — which is the drift test for every
override at once, the worker pair included.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "sync_project_layer.py"


@pytest.fixture(scope="module")
def sync():
    spec = importlib.util.spec_from_file_location("sync_project_layer", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BUNDLED = """name: t
steps:
  one:
    title: first
    instructions: |
      do the thing
    done_when: |
      it is done
    # verify lives in the project layer
    next: two
  two:
    title: second
    instructions: |
      # not a comment: six-space text
    next: end
"""

PROJECT = """name: t
steps:
  one:
    title: first
    instructions: |
      stale wording nobody should keep
    # verify lives in the project layer
    # why: this suite, this machine
    verify: 'run the suite'
    next: two
  two:
    title: second
    instructions: |
      # not a comment: six-space text
    verify: 'run the other check'
    next: end
"""


def test_field_blocks_take_the_comment_run_above_each_field(sync):
    blocks = sync.field_blocks(PROJECT)
    assert list(blocks) == ["one", "two"]
    assert blocks["one"] == [
        "    # verify lives in the project layer\n",
        "    # why: this suite, this machine\n",
        "    verify: 'run the suite'\n",
    ]
    assert blocks["two"] == ["    verify: 'run the other check'\n"]


def test_graft_keeps_the_package_and_only_adds_verify(sync):
    out = sync.graft(BUNDLED, sync.field_blocks(PROJECT))
    # The project's stale wording is gone; the packaged wording stands.
    assert "stale wording" not in out and "do the thing" in out
    # The packaged pointer comment is replaced by the block, not duplicated.
    assert out.count("# verify lives in the project layer") == 1
    assert "    # why: this suite, this machine\n    verify: 'run the suite'\n    next: two\n" in out
    # A step with no pointer comment just gains its verify before next:.
    assert "      # not a comment: six-space text\n    verify: 'run the other check'\n    next: end\n" in out


def test_a_verify_for_a_step_the_package_lost_is_an_error_not_a_silent_drop(sync):
    orphan = PROJECT.replace("  two:", "  gone:")
    with pytest.raises(ValueError, match="gone"):
        sync.graft(BUNDLED, sync.field_blocks(orphan))


def test_regeneration_is_idempotent(sync):
    once = sync.graft(BUNDLED, sync.field_blocks(PROJECT))
    assert sync.graft(BUNDLED, sync.field_blocks(once)) == once


def test_this_repositorys_overrides_are_their_own_regeneration(sync):
    """Every override here equals packaged + its verify blocks — no drift.

    This is the check the leader's identity test used to make for one file;
    it now covers the worker too, which had been left out because nobody
    wanted to resync it by hand.
    """
    names = sync.overrides()
    assert names, "this repository carries project overrides"
    for name in names:
        current = (sync.PROJECT / f"{name}.yaml").read_text(encoding="utf-8")
        assert current == sync.regenerate(name), (
            f"{name}: .claunch/workflows/{name}.yaml differs from the packaged "
            f"copy by more than its grafted field blocks — regenerate it with "
            f"tools/sync_project_layer.py instead of editing it"
        )


def test_a_multi_line_field_survives_the_graft_whole(sync):
    """``verify`` fits on one line; ``awaits`` need not. The graft keys on the
    field NAME and takes everything indented under it, so a block written over
    several lines is carried across instead of being cut after its first."""
    project = PROJECT.replace(
        "    verify: 'run the suite'\n",
        "    verify: 'run the suite'\n"
        "    # why we watch it\n"
        "    awaits:\n"
        "      probe: verify\n"
        "      poll: 30\n",
    )
    blocks = sync.field_blocks(project)
    assert blocks["one"] == [
        "    # verify lives in the project layer\n",
        "    # why: this suite, this machine\n",
        "    verify: 'run the suite'\n",
        "    # why we watch it\n",
        "    awaits:\n",
        "      probe: verify\n",
        "      poll: 30\n",
    ]
    out = sync.graft(BUNDLED, blocks)
    assert "      poll: 30\n    next: two\n" in out
    # the block stopped where it should: the next step is still intact
    assert "  two:\n" in out
    assert sync.graft(BUNDLED, sync.field_blocks(out)) == out


def test_a_field_block_does_not_swallow_the_blank_line_between_steps(sync):
    """A blank line inside a block scalar belongs to the field; the one that
    separates two steps does not, and telling them apart takes a lookahead."""
    project = (
        "name: t\nsteps:\n"
        "  one:\n"
        "    verify: |\n"
        "      first line\n"
        "\n"
        "      after a blank\n"
        "\n"
        "    next: two\n"
    )
    assert sync.field_blocks(project)["one"] == [
        "    verify: |\n",
        "      first line\n",
        "\n",
        "      after a blank\n",
    ]
