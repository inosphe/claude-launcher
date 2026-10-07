"""The CLI's onboarding lines — what a create/spawn answer says happened.

A create or spawn reports each onboarding leg separately, because each can
fail on its own: the session exists even when the mesh join is what went
wrong. These lines are read by a person deciding what to do next, so the
sentence has to name the state that actually occurred.

The mesh leg has three states, not two. A join that pended is a *wait* --
the primary has not answered yet -- and printing it as a failure told the
operator the opposite of what happened (claunch-htffv). The round-1
incident read "mesh join failed" for two sessions that were merely waiting
on an answer, and the diagnosis took a round to find.
"""

from __future__ import annotations

from claude_launcher import cli_sessions


def said(capsys, result) -> str:
    cli_sessions._print_onboarding(result)
    return capsys.readouterr().out


def test_a_pending_join_is_reported_as_a_wait_not_a_failure(capsys):
    """The old line collapsed `error` and `pending` with `or`, so a pend
    printed "mesh join failed" followed by the pending dict's repr."""
    out = said(
        capsys,
        {
            "mesh": {
                "ok": False,
                "pending": {
                    "pending": True,
                    "request_id": "req-1",
                    "mesh": "gds6@shared-daemon-main",
                    "primary": "shared-daemon-main",
                },
            }
        },
    )
    assert "mesh join pending" in out, out
    assert "failed" not in out, out
    assert "shared-daemon-main" in out, out
    assert "{" not in out, f"a dict was printed at a person: {out}"


def test_a_refused_join_still_says_failed_with_its_reason(capsys):
    out = said(
        capsys,
        {
            "mesh": {
                "ok": False,
                "error": "handle 's59' is already taken in mesh 'gds6@shared-daemon-main'",
            }
        },
    )
    assert "mesh join failed" in out, out
    assert "already taken" in out, out


def test_an_error_wins_over_a_pend(capsys):
    """`_join` returns one or the other; if both ever arrive the reader needs
    the failure, not the wait -- the same precedence the web card takes."""
    out = said(
        capsys, {"mesh": {"ok": False, "error": "relay refused", "pending": {"primary": "m"}}}
    )
    assert "mesh join failed: relay refused" in out, out


def test_a_failure_with_nothing_to_say_still_says_something(capsys):
    out = said(capsys, {"mesh": {"ok": False}})
    assert "mesh join failed: no reason given" in out, out


def test_a_pend_without_a_primary_names_the_owner(capsys):
    out = said(capsys, {"mesh": {"ok": False, "pending": {"pending": True}}})
    assert "the mesh owner" in out, out


def test_a_join_that_worked_is_unchanged(capsys):
    out = said(
        capsys,
        {
            "mesh": {
                "ok": True,
                "mesh": "claunch",
                "handle": "s120",
                "connected_to": ["s29"],
            }
        },
    )
    assert "joined as s120" in out, out
    assert "can reach s29" in out, out
