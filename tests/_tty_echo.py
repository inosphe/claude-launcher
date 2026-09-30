"""A prelude for the tests' stand-in harness child: turn the terminal's echo off.

Several test modules register a tiny python program as the ``py`` harness. It
prints ``READY`` and then answers every input line with ``echo:<line>``. On a
POSIX PTY the line discipline's ECHO flag is on by default, so each line the
daemon types reaches the output twice: once as the terminal's echo of the
input, and once as the child's own ``echo:`` copy. The two are produced by
different writers into the same master side and interleave.

A test that waits for one line of a long typed block (an opening, a briefing)
can then find it split: the terminal's copy of line N is broken by the child's
copy of an earlier line, and the child's intact copy sits elsewhere on the
grid. ``test_create_joins_a_mesh_and_starts_a_run_in_one_call`` failed 4 of 20
serial runs on an unchanged tree this way (claunch-fepnp); the same test with
this prelude in front of its child failed 0.

``NO_TTY_ECHO + CHILD`` leaves exactly one copy of every line -- the child's.
``termios`` exists only on POSIX. On Windows the import fails, the prelude
does nothing, and the console's own echo stays as it was; a test that uses
this still has to look for its line where the one remaining copy can be.
"""

NO_TTY_ECHO = (
    "try:\n"
    "    import termios\n"
    "except ImportError:\n"
    "    termios = None\n"
    "if termios is not None:\n"
    "    _attrs = termios.tcgetattr(0)\n"
    "    _attrs[3] &= ~termios.ECHO\n"
    "    termios.tcsetattr(0, termios.TCSANOW, _attrs)\n"
)
