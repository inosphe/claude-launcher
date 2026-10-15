"""pytest plugin: hold the machine test window in any repository (claunch-ersj2).

claunch's own suite takes the window from ``tests/conftest.py``. Another
repository's pytest never did, so a session testing there ran next to whatever
held the machine and never showed on the Window page. Installed with claunch,
this plugin (entry point ``pytest11: claunch_window``) takes the same grant for
a pytest run inside a managed session.

It stands aside when the run is already covered: outside a managed session
(no ``$CLAUNCH_SESSION``), in an xdist worker, under an inherited grant (a
parent run, ``claunch window run``, or a conftest that already took one), or
when switched off with ``CLAUNCH_WINDOW_PLUGIN=0`` or ``-p no:claunch_window``.

Hook order: a conftest is registered after the entry-point plugins, so its
plain ``pytest_sessionstart`` runs before this one (pluggy calls the latest
registration first) and claunch's own grant is already in the environment.
This hook is deliberately not ``trylast``: xdist starts its workers from a
``trylast`` sessionstart, and the grant must be installed and the width
clamped before that.
"""

from __future__ import annotations

import os
import time

import pytest

from . import test_window

PLUGIN_ENV = "CLAUNCH_WINDOW_PLUGIN"

#: Per-config state. A pytester run inside a test loads this same module for
#: a second config; module globals would let that inner run release and
#: report the outer run's grant (claunch-ersj2, found by s737-rev).
_STATE = pytest.StashKey[dict]()


def _switched_off() -> bool:
    return os.environ.get(PLUGIN_ENV, "").strip().lower() in ("0", "off", "false", "no")


def window_class(args) -> str:
    """No path, or the repository root, is the whole suite: a sweep."""
    selected = [str(a).replace("\\", "/").rstrip("/") for a in args]
    if not selected or all(a in ("", ".") for a in selected):
        return test_window.SWEEP
    return test_window.TARGETED


def pytest_sessionstart(session):
    if _switched_off() or os.environ.get("PYTEST_XDIST_WORKER"):
        return
    config = session.config
    state = config.stash[_STATE] = {"grant": None, "report": None,
                                    "started": time.monotonic()}
    # Only the direct child of a grant owner (``claunch window run``) reports
    # into that grant; a conftest that took the window popped this already.
    reporter = os.environ.pop(test_window.WINDOW_REPORT_ENV, None) == "1"
    inherited = test_window.inherited_grant()
    if inherited is not None:
        test_window.clamp_xdist_width(config.option, inherited.advisory_n)
        state["report"] = inherited.grant_id if reporter else None
        return
    if not os.environ.get("CLAUNCH_SESSION"):
        return
    try:
        grant = test_window.acquire(
            window_class(config.args),
            label="pytest " + " ".join(str(a) for a in config.args),
            workers=test_window.requested_xdist_width(config.option),
        )
    except test_window.WindowUnavailable as exc:
        raise pytest.UsageError(str(exc)) from exc
    grant.install_environment()
    test_window.clamp_xdist_width(config.option, grant.advisory_n)
    state["grant"] = grant
    state["report"] = grant.grant_id


def _release(config) -> None:
    state = config.stash.get(_STATE, None)
    grant = state and state.pop("grant", None)
    if grant is None:
        return
    grant.release()
    if os.environ.get(test_window.WINDOW_GRANT_ENV) == grant.grant_id:
        for name in (test_window.WINDOW_GRANT_ENV, test_window.WINDOW_CLASS_ENV,
                     test_window.WINDOW_WORKERS_ENV):
            os.environ.pop(name, None)


def pytest_sessionfinish(session, exitstatus):
    state = session.config.stash.get(_STATE, None)
    grant_id = state and state.pop("report", None)
    if test_window.reportable(grant_id):
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        test_window.report_result(grant_id, test_window.pytest_result(
            exitstatus,
            getattr(reporter, "stats", {}) or {},
            getattr(session, "testscollected", 0),
            time.monotonic() - state["started"],
        ))
    _release(session.config)


def pytest_unconfigure(config):
    _release(config)
