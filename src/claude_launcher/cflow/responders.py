"""Who can answer a delegated decision — the seam between a run and the mesh.

A workflow declares candidates (``{role: leader, up: 1}``); turning those into
*sessions that exist right now* needs the mesh roster and the spawn lineage,
and only the daemon holds either. This module is where that lookup lands, so
the engine can stay what it has always been: a state machine over files, with
no daemon in reach.

Two properties of the lookup matter more than its mechanism:

**It runs once, at the moment the ask is opened, and the answer is frozen into
the run.** Authorization is then a membership test against that recorded list
— never a re-resolution. It is the same rule the mesh already applies to
``auto_link`` (evaluated at join, stored as an edge): a decision about who may
act must not silently change because a session exited, was renamed, or was
spawned after the question went out.

**A candidate is always an ancestor.** ``up`` is required by the schema for
exactly this reason, and it is enforced again here: an agent can spawn a
session with any handle it likes, so a responder found *below* the asking
session would be one the run manufactured for itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from . import model


@dataclass(frozen=True)
class Responder:
    """One resolved member that may answer, as recorded into the run."""

    #: The managed session name — the scope this member drives its own runs
    #: in, and the identity its MCP server reports when it answers.
    session: str
    #: Its mesh handle, for delivery and for anything a human reads.
    handle: str
    role: str
    #: Hops above the asking session in the (enrolled) spawn lineage.
    up: int

    def to_dict(self) -> dict:
        return {
            "kind": "member",
            "session": self.session,
            "handle": self.handle,
            "role": self.role,
            "up": self.up,
        }


#: What an ``asked`` entry looks like for the human token. A human holds no
#: session and no handle, so there is nothing to record but the kind: their
#: authority comes from reaching the CLI or the dashboard at all.
HUMAN_ENTRY = {"kind": "human"}


def resolve(
    candidate: model.Candidate, *, session: str, cwd: str
) -> Tuple[List[Responder], Optional[str]]:
    """Members matching ``candidate`` above ``session``, newest lookup each time.

    Returns ``(responders, reason)`` — ``reason`` explains an EMPTY result in
    words a human can act on, and is carried into the run so a question that
    ended up in front of a person says why it was not answered by an agent.
    Never raises for "nobody matched": an unresolvable group escalates, and a
    resolution failure that read as a crash would take the run down with it.
    """
    if candidate.human:
        raise ValueError("the human token is not resolved through the mesh")
    return [], _unavailable_reason(candidate, session)


def _unavailable_reason(candidate: model.Candidate, session: str) -> str:
    """Why nothing matched, while the daemon lookup is still to be wired.

    Deliberately explicit about the cause rather than a bare "no match": the
    escalation path prints these, and "the mesh was never consulted" and "the
    mesh has no such member" are different problems for whoever reads it.
    """
    if not session:
        return (
            f"{candidate.describe()}: this run is not driven by a managed "
            f"session, so it has no lineage to look up"
        )
    return (
        f"{candidate.describe()}: delegated responders are not resolved yet "
        f"(the daemon lookup is not wired), so this group cannot be asked"
    )
