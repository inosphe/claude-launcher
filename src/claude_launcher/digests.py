"""Content ids: one id space for every block of prompt text claunch hands out.

An agent is given prose from several unrelated subsystems — the cflow step it
is standing on, the opening task it was created with, the stance its mesh role
binds it to — and every one of them can be lost to a ``/compact``. The
recovery move is the same in all three cases: *name* the text with a short id
that rides next to it, and later name only the id. An agent asked "do you
still remember the step?" cannot answer; an agent asked "is ``a3f9c2`` in this
conversation?" can, by looking.

That reflex is worth exactly one thing: being uniform. An id minted by cflow
and an id minted by the mesh must look the same, be the same length, and be
checked the same way, because the agent doing the looking should not have to
know which subsystem coined the one in front of it. So the primitive lives
here — above :mod:`cflow`, above :mod:`daemon` — rather than in whichever of
them happened to need it first.

Nothing is stored. Every id is recomputed from the text it names on each call,
which is what makes it self-validating: an id either still matches what the
source says *now* or it does not, and a mismatch is the true answer rather
than a stale snapshot handed back as a current one. It is also why no recall
door needs a content store — the text is always re-derivable from the state
that produced it.
"""

from __future__ import annotations

import hashlib

#: How much of the sha256 rides in a block header. Twelve hex characters is
#: 48 bits: collision-free across anything one machine will ever hold, and
#: short enough that an agent can compare it by eye against the id it was
#: given with the text.
DIGEST_CHARS = 12


def text_digest(body: str) -> str:
    """The id for one block of content, or ``""`` when there is no content.

    Hashes what is passed, byte for byte, with no normalisation of its own:
    callers know whether their text has meaningful leading whitespace and
    whether their fields need a separator, and a helper that quietly
    rewrote either would make two callers that *look* like they agree mint
    different ids for the same prose.

    The empty answer is load-bearing rather than defensive. A position with
    no instructions, a session with no recorded task and a role with no
    stance all have nothing an agent could have lost, and an id for them
    would invite a recall that returns nothing.
    """
    if not (body or "").strip():
        return ""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:DIGEST_CHARS]
