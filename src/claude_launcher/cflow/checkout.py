"""Whose checkout a run is actually working in.

A run is keyed to a directory, and the step's ``verify`` command executes
there (:func:`..engine._run_verify` hands that directory to ``subprocess``).
Nothing checked that the directory is the driving session's *own* checkout,
and two shapes break that assumption:

* the session stands in another session's checkout — a worker spawned
  without a worktree lands in its parent's — so both edit one tree and
  switch its branches under each other;
* the run is keyed somewhere other than where the session actually is, so
  its verify inspects a tree the session never touched.

Either way the machine gate keeps passing: the suite goes green in whatever
tree it was pointed at. Green from the wrong tree is worse than red, because
it is reported as the machine's word on work it never saw — so the fact is
surfaced rather than left for someone to notice.

The daemon is the only holder of "where each session stands", so this asks
it and degrades quietly when it cannot (no daemon, no answer, an unmanaged
run): a missing answer is not an error, it is simply no warning to give.
The same contract as :mod:`.responders`, and for the same reason — a run
must never fail because a best-effort lookup did.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from .. import daemon_client
from . import state as state_mod

#: Ceiling on the daemon call. The check is advisory and sits in front of
#: ``verify``; it may not add meaningful latency to a run.
CALL_TIMEOUT = 3.0

#: Statuses the daemon reports for a session that is no longer running. A
#: dead session's directory is nobody's, so it never counts as a neighbour.
_DEAD = frozenset({"exited", "dead", "gone"})


@dataclass(frozen=True)
class Occupancy:
    """Who stands where, as far as the daemon knows."""

    #: The directory this run is keyed to, canonicalised.
    run_cwd: str
    #: What the daemon records as the driving session's cwd (None = unknown).
    session_cwd: Optional[str] = None
    #: Other live sessions whose cwd IS this run's directory.
    peers: Tuple[str, ...] = ()
    #: Why the question could not be answered, if it could not be.
    problem: Optional[str] = None

    @property
    def mismatch(self) -> bool:
        """The run works somewhere other than where its session stands."""
        return bool(self.session_cwd) and self.session_cwd != self.run_cwd

    @property
    def shared(self) -> bool:
        return bool(self.peers)


def inspect(*, session: str, cwd: Optional[str] = None) -> Occupancy:
    """Where this run works, and who else is standing there.

    Never raises: every failure becomes ``problem`` on an otherwise empty
    answer.
    """
    run_cwd = state_mod.resolve_cwd(cwd)
    if not session or session == state_mod.DEFAULT_SCOPE:
        # Not a managed session: there is no recorded home to compare against,
        # and a lone run owns whatever directory it was started in.
        return Occupancy(run_cwd, problem="not a managed session")
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        # "did not answer" and "is not running" lead a reader to different
        # places; this line is the only thing they get to tell them apart.
        return Occupancy(
            run_cwd, problem=f"the claunch {daemon_client.unreachable_reason(why)}"
        )
    try:
        doc = client.get("/api/sessions", timeout=CALL_TIMEOUT) or {}
    except daemon_client.DaemonClientError as exc:
        return Occupancy(run_cwd, problem=f"the daemon did not answer: {exc}")
    rows = doc.get("sessions") if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        return Occupancy(run_cwd, problem="the daemon's session list was unreadable")

    session_cwd: Optional[str] = None
    peers: List[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        raw = row.get("cwd")
        if not name or not raw:
            continue
        try:
            where = state_mod.resolve_cwd(str(raw))
        except OSError:
            continue
        if name == session:
            session_cwd = where
        elif where == run_cwd and not _exited(row):
            peers.append(name)
    if session_cwd is None:
        return Occupancy(
            run_cwd,
            peers=tuple(sorted(peers)),
            problem=f"the daemon knows no session named {session!r}",
        )
    return Occupancy(run_cwd, session_cwd=session_cwd, peers=tuple(sorted(peers)))


def occupant(cwd: Optional[str] = None) -> str:
    """The one live managed session whose own directory IS this one, or "".

    The reverse of :func:`inspect`: that asks "where does this named session
    stand", this asks "who stands here". It exists because a run does not
    always carry a name to ask with. A run's scope comes from the ambient
    ``CLAUNCH_SESSION`` of whatever started it (:func:`.state.current_scope`),
    so a run started by a process that did not have it is keyed to
    :data:`.state.DEFAULT_SCOPE` -- a real, drivable run with no session
    identity anywhere in it. Measured (issue ``claunch-d7qp``): three worker
    rounds drove a run out of ``.cflow/runs/default/`` inside their own
    worktree, and their ``wrapup`` gate (``tools/report_check.py``, which
    resolves "whose report" the same way) answered ``exit 2``, ``error: no
    session``, three times in a row. The round could not be closed by any
    flag the agent could pass; a person moved the run by hand.

    The daemon holds the fact the run is missing, and it is not a guess: it
    records each managed session's directory, and a session standing in this
    exact directory is the session whose round a gate here is about.

    **One, or none.** Two sessions standing in the same checkout make the
    question ambiguous, and this returns "" rather than picking -- the same
    refusal ``claunch report``'s own last-resort lookup makes
    (``cli_report._run_scope``: "More than one run here is ambiguity, so it is
    declined rather than guessed"). An exited session is nobody, so it never
    answers.

    Best-effort, like everything else in this module: no daemon, no answer or
    an unreadable one all return "", which leaves the caller exactly where it
    was before this existed.
    """
    here = state_mod.resolve_cwd(cwd)
    client = daemon_client.connect()
    if client is None:
        return ""
    try:
        doc = client.get("/api/sessions", timeout=CALL_TIMEOUT) or {}
    except daemon_client.DaemonClientError:
        return ""
    rows = doc.get("sessions") if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        return ""
    found: List[str] = []
    for row in rows:
        if not isinstance(row, dict) or _exited(row):
            continue
        name, raw = str(row.get("name") or ""), row.get("cwd")
        if not name or not raw:
            continue
        try:
            where = state_mod.resolve_cwd(str(raw))
        except OSError:
            continue
        if where == here:
            found.append(name)
    return found[0] if len(found) == 1 else ""


def _exited(row: dict) -> bool:
    if row.get("exited") or row.get("exited_at"):
        return True
    return str(row.get("status") or "").lower() in _DEAD


def warning(occ: Occupancy) -> Optional[str]:
    """The sentence to put in front of whoever drives the run, or None.

    Only the two states that make a machine gate lie are worth saying out
    loud; ``problem`` is deliberately silent, because "could not check" on
    every daemonless run is noise, not information.
    """
    if occ.mismatch:
        return (
            f"this run works in {occ.run_cwd}, but its session stands in "
            f"{occ.session_cwd} — the step's verify runs in the first, so it "
            f"reports on a tree this session is not editing"
        )
    if occ.shared:
        others = ", ".join(occ.peers)
        return (
            f"this run's directory ({occ.run_cwd}) is also where {others} "
            f"{'is' if len(occ.peers) == 1 else 'are'} standing — the step's "
            f"verify runs in a checkout another session edits and switches "
            f"branches in, so a green suite is not evidence about this run's "
            f"work. Give this session its own worktree"
        )
    return None


def check(*, session: Optional[str] = None, cwd: Optional[str] = None) -> Optional[str]:
    """``warning(inspect(...))`` — the one call the engine needs."""
    who = session if session is not None else state_mod.current_scope()
    return warning(inspect(session=who, cwd=cwd))


#: How :func:`own_checkout` came to its answer. The caller prints this, so a
#: gate's verdict always says which tree it was measured in.
NAMED = "named"
SESSION = "session"
RUN_CWD = "run cwd"


def own_checkout(
    explicit: Optional[str] = None,
    *,
    session: Optional[str] = None,
    cwd: Optional[str] = None,
) -> Tuple[str, str]:
    """The checkout whose branch a gate under ``tools/`` should ask about.

    :func:`check` reports the mismatch; this resolves it. A gate that asks
    "did *my* branch land" has to find the session's own tree, and the run's
    directory is not it whenever the run was keyed somewhere the session does
    not stand -- the shape that made ``landed_check`` ask whether ``master``
    had been merged into something, a question with no true answer.

    The ambient ``CLAUNCH_SESSION`` is the session's own, and it holds for two
    different reasons which have to be kept apart. An earlier version of this
    paragraph gave only the first and was read as covering both, which is how
    the defect below stood in the code with a docstring saying it could not.

    * A verify is GIVEN it too, now. It used to INHERIT it: ``_run_verify``
      passed no ``env=``, so the subprocess took the environment of whoever
      called :func:`..engine.next_step` -- the in-session MCP server, where
      the ambient name is the session's own and the answer was right. It was
      right only while that held, and it did not always: a server started
      without ``CLAUNCH_SESSION`` keys its run to
      :data:`.state.DEFAULT_SCOPE` and hands the gate an environment with no
      session in it at all, which is ``exit 2, no session`` for every gate
      that asks whose round this is (issue ``claunch-d7qp``, measured in
      three worktrees). So the environment is now built from the run's own
      scope, falling back to the session the daemon says stands in this
      checkout (:func:`..engine.verify_scope`, :func:`occupant`).
    * A probe is GIVEN it. The daemon's clock does not run a verify; it runs
      :func:`..engine.run_probe`, a different function in a different process
      -- and that process holds one ``CLAUNCH_SESSION``, the terminal's that
      started the daemon, for every run on the machine. Inheriting that is
      precisely the reading this function exists to refuse, so ``run_probe``
      builds the probe's environment from the run's own scope rather than
      passing the daemon's on (:func:`..engine.probe_env`, issue
      ``claunch-04ru``). Noting that a probe is not a verify and stopping
      there is what this paragraph used to do: the distinction was right and
      the conclusion drawn from it was not.

    Both now go through :func:`..engine.probe_env`, which is the point --
    they were two subprocesses taking their session identity from whatever
    environment happened to be around them, and they failed in the two
    different directions that allows (the probe got somebody else's name, the
    verify got none).

    ``explicit`` is a directory the caller was given outright (a gate's
    ``--repo``). It wins without consulting anything, so a test or a hand-run
    is never at the mercy of a daemon, and it is answered here rather than in
    each caller: the three answers are one decision, and splitting it left
    :data:`NAMED` defined in this module and spelled as a bare string in two
    others.

    Degrades in one direction only. No daemon, no managed session, or a
    session the daemon does not know all fall back to ``cwd`` -- the answer
    every caller gave before this existed, so nothing that worked stops.

    What it does NOT answer, and the distinction is the whole limit of this
    fix: *which branch this run is about*. It answers where the session
    stands, and the two agree only when the session stands in the tree it
    edits. A session started at the repository root that works in a worktree
    it made by hand is invisible here -- the daemon records ``cwd`` at
    creation and only an operator ``migrate`` (stop, relaunch) changes it, so
    the session cannot correct the record itself. Measured on a live mesh:
    17 of 23 live sessions stood in a worktree, and the 6 that did not
    included the one whose round this defect blocked. For those, no machine
    fact links the run to its branch, and the gates say "cannot tell" rather
    than guess.
    """
    if explicit is not None:
        return str(Path(explicit).resolve()), NAMED
    who = session if session is not None else state_mod.current_scope()
    here = state_mod.resolve_cwd(cwd)
    occ = inspect(session=who, cwd=cwd)
    if occ.session_cwd:
        return occ.session_cwd, SESSION
    return here, RUN_CWD
