"""Rename a temporary file into place, with Windows' transient refusals absorbed.

Every caller here already had the atomic-write shape right: write the new bytes
to a scratch file beside the target, then ``os.replace`` the scratch over it, so
a concurrent reader sees the whole old document or the whole new one and never a
state between them. That reasoning is sound and unchanged -- what it did not
survive is *Windows*.

On Windows the replace step is ``MoveFileEx(..., MOVEFILE_REPLACE_EXISTING)``,
and deleting the destination needs every open handle to it to have been opened
with ``FILE_SHARE_DELETE``. Python's own ``open()`` does not ask for that, and
neither do the file watchers, indexers and virus scanners that touch a file the
moment it changes. So any of them holding the destination for the few
milliseconds after we wrote it turns the rename into ``PermissionError``
``[WinError 5]`` -- not because the caller lacks permission, but because someone
else has the file open right now and will let go shortly.

The observed cost was a test suite that could not be green: two full-suite runs
over the same tree failed *different* tests with an empty intersection, both at
this call. A gate that a healthy tree cannot pass stops being a gate.

So: retry the replace a few times over a fraction of a second, then give up and
re-raise the original error untouched. Bounded, because the failure this absorbs
is transient by construction -- a holder that has not let go in a third of a
second is not a scanner, it is a real conflict, and hiding that would trade a
loud flake for a silent hang. POSIX never raises this, so nothing there changes:
the first attempt succeeds and the retry loop is never entered.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Union

#: Delays between attempts, in seconds. The sum is the whole budget a caller
#: can lose to a holder (0.31s); the first two cover the common case, where a
#: scanner opened the file we just wrote and is already done with it.
BACKOFF = (0.01, 0.02, 0.04, 0.08, 0.16)

#: Windows error codes worth another attempt. 5 is ``ERROR_ACCESS_DENIED``,
#: which is what ``MoveFileEx`` reports when an open handle lacks
#: ``FILE_SHARE_DELETE``; 32 is ``ERROR_SHARING_VIOLATION``, the same story told
#: by a different API layer. Anything else -- a read-only file, a wrong ACL, a
#: missing directory -- is a real answer and is raised at once.
#:
#: The test below is what decides, not the exception's class: CPython maps
#: winerror to an ``OSError`` subclass through a table this code should not have
#: to know (5, 19, 32 and 33 all arrive as ``PermissionError``; 1314 arrives as
#: a bare ``OSError``), and keying on the class would make the retry depend on
#: which side of that table an error happened to fall.
TRANSIENT = frozenset({5, 32})

_PathLike = Union[str, "os.PathLike[str]", Path]


def replace(src: _PathLike, dest: _PathLike) -> None:
    """``os.replace(src, dest)``, retrying Windows' transient refusals.

    Raises exactly what ``os.replace`` raised, once the budget is spent, so a
    caller's ``except OSError`` and the error's ``winerror`` both still read the
    way they did before.
    """
    for delay in BACKOFF:
        try:
            os.replace(src, dest)
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) not in TRANSIENT:
                raise
            time.sleep(delay)
    # Last attempt, outside the loop: whatever it raises is what the caller sees,
    # and a success here is as good as a success on the first try.
    os.replace(src, dest)
