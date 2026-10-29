"""Which tests the cheaper test tiers leave to the full sweep.

The suite is run at three depths (the policy and the reasons are in
``CLAUDE.md``, section "테스트 계층"):

* **T0** -- the targeted run of ``tools/changed_tests.py`` while working.
* **T1** -- the same targeted run on the merge preview, before landing.
* **T2** -- the full sweep after landing: every collected test, no ``-m``.

T0 and T1 drop the slow tail; T2 never filters. The tail is one marker,
:data:`TAIL_MARKER`, so one expression is the whole difference between the
tiers and a test can only be in the tail or not -- there is no third state in
which a test is excluded from the cheap tiers and also from the sweep.

:data:`TAIL_MARKER` is not written on tests. ``tests/conftest.py`` adds it to
every item that carries one of :data:`SOURCE_MARKERS`, which are the markers a
test author writes for the reason the test is slow. Adding a source marker is
how a test joins the tail; ``tests/test_test_tiers.py`` pins that the two stay
consistent and that tail plus core is the whole collection.
"""

from __future__ import annotations

from typing import Iterable

#: Selects the tail. T2 does not use it.
TAIL_MARKER = "slow_tail"

#: Markers a test author writes; each one makes the test part of the tail.
SOURCE_MARKERS = ("worktree", "slow_shim", "browser")

#: The ``-m`` expression T0 and T1 pass to pytest.
CORE_EXPR = f"not {TAIL_MARKER}"


def is_tail(marker_names: Iterable[str]) -> bool:
    """True when a test with these marker names belongs to the tail."""
    names = set(marker_names)
    return TAIL_MARKER in names or any(m in names for m in SOURCE_MARKERS)
