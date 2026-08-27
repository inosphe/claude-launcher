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
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Union

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


@contextmanager
def scratch(path: Path) -> Iterator[Path]:
    """A private temporary file beside ``path``, removed if the block raises.

    The other half of an atomic write, and the half that was missing. Writing
    the new bytes beside the target and renaming them over it is only atomic
    while **this** writer owns the bytes it is renaming. Four of the five
    callers named their scratch file after the target alone -- ``x.tmp`` for
    ``x`` -- so two writers of the same document shared one scratch path, and
    the failures that come out of that are not the one :func:`replace` absorbs:

    * one writer renames a file the other is still filling, which is another
      ``WinError 5`` and reads exactly like a holder on the destination;
    * or the second write completes first and the first writer renames the
      *other* document into place. Nothing raises. The lost update is silent,
      which is why no test and no sweep has ever reported it.

    ``store.py`` alone got this right, and its docstring said why: the name
    carries the pid "so two writers cannot land on the same scratch name". A
    pid is not enough here, and the measurement is specific rather than
    theoretical -- ``daemon/api.py`` serves a transcript page with
    ``asyncio.to_thread(transcript_view.page, ...)``, which reaches
    ``_save_index`` on a worker thread, so two requests for the same session
    are two threads in **one** process writing one scratch path. ``mkstemp``
    separates them: it creates the file ``O_EXCL`` under a name nobody else
    will draw, in the target's own directory so the rename stays within one
    filesystem and stays atomic.

    The name it draws keeps the target's name as a prefix and ``.tmp`` as a
    suffix, so a leftover is still recognisable as this code's litter rather
    than a document -- which matters because a unique name is one nobody
    reuses, and a scratch file that survives its writer is never overwritten
    the way a shared name was. That is what the cleanup here is for, and why
    it belongs with the naming instead of at each call site: the two are one
    decision, and four of the five sites had neither.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    os.close(fd)
    tmp = Path(name)
    try:
        yield tmp
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
