"""The daemon's one reading of the profile registry and what it implies.

Three handlers ask questions whose answers come from the same sources: the
config document, the profile directories, and the harness registry merged from
the packaged declaration. ``/api/profiles`` reads them while the dashboard
boots, the session detail panel polls ``/api/session/<name>`` every two
seconds, and the Restart-settings picker reads ``/api/borrow-options`` each
time a panel opens. Each of them used to read the sources per request: a YAML
document parsed once per link of every inheritance chain, the registry rebuilt
per lookup, and a borrow verdict -- two harness policies plus a credential
probe -- computed per lender, per request, per session (claunch-89eom).

:class:`ProfileFacts` holds one :class:`Facts` and one row of borrow verdicts
per runtime selector, rebuilt by :mod:`claude_launcher.daemon.snapshots` on the
daemon's clock. A request is a dictionary lookup. The refresh interval bounds
how long a verdict can be stale, which is why it is short: a lender's token can
expire, and the detail rail is expected to say so without a restart. A write to
the config that these answers depend on calls :meth:`ProfileFacts.invalidate`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .. import borrowing, harnesses as harness_registry, lineage
from .. import profile as profile_mod
from .. import store
from . import snapshots

#: Seconds between rebuilds. The detail rail polls every two seconds and a
#: borrow verdict is what turns it red, so this is deliberately short; the
#: whole rebuild is a handful of milliseconds (twelve profiles, twelve
#: lenders) once the config document is read once instead of per verdict.
DEFAULT_INTERVAL = 5.0


@dataclass(frozen=True)
class Facts:
    """One read of everything the profile-derived answers need."""

    doc: dict
    profiles: Tuple[profile_mod.Profile, ...]
    registry: Dict[str, harness_registry.Harness]
    harness_names: Tuple[str, ...]
    available: Dict[str, bool]
    built_at: float

    def harness(self, name: str) -> Optional[harness_registry.Harness]:
        return self.registry.get(str(name or "").strip())


def read_facts() -> Facts:
    """The expensive read: one config load, one listing, one registry merge."""
    doc = store.load()
    profiles = tuple(profile_mod.list_all())
    registry = harness_registry.registry(doc)
    return Facts(
        doc=doc,
        profiles=profiles,
        registry=registry,
        harness_names=tuple(harness_registry.names(doc)),
        available={
            name: bool(entry and entry.available())
            for name, entry in registry.items()
        },
        built_at=time.monotonic(),
    )


class ProfileFacts:
    """Profile facts and borrow verdicts, kept in memory between reads."""

    def __init__(self, *, interval: float = DEFAULT_INTERVAL) -> None:
        self.interval = float(interval)
        self._facts = snapshots.PeriodicSnapshot(
            read_facts, interval=self.interval, name="profile-facts"
        )
        self._rows = snapshots.KeyedSnapshots(
            self._build_row, interval=self.interval, name="profile-borrow"
        )

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._facts.start()
        self._rows.start()

    def stop(self) -> None:
        self._rows.stop()
        self._facts.stop()

    # -- readers ---------------------------------------------------------

    def facts(self) -> Facts:
        """The current facts; builds them if this is the first read."""
        return self._facts.current()

    def harness(self, name: str) -> Optional[harness_registry.Harness]:
        return self.facts().harness(name)

    def borrow_options(self, selector: str, *, refresh: bool = False) -> dict:
        """The ``/api/borrow-options`` answer for one runtime selector.

        ``refresh`` rebuilds the row now instead of waiting for the clock --
        the manual path a person takes when a token was just replaced.
        """
        runtime = profile_mod.resolve_selector(selector)
        key = runtime.selector
        if refresh:
            # A row is built from the facts, so a manual rebuild has to pick
            # up a profile added or removed since they were read: refresh the
            # facts too, or the row would be rebuilt over stale inputs.
            self._facts.refresh()
            return dict(self._rows.refresh(key))
        return dict(self._rows.require(key))

    def borrow_validation(
        self, runtime_selector: str, lender: str, *, harness_name: Optional[str] = None
    ) -> Optional[dict]:
        """One lender's verdict for one runtime, for the session detail rail.

        ``harness_name`` is the harness the *session* runs under, which can
        differ from the profile's own effective harness; a row built for the
        effective harness does not answer for it. The row is used only when the
        two agree, and the fallback validates the pair directly -- rare, and
        correctness outranks the saved read.
        """
        facts = self.facts()
        try:
            runtime = profile_mod.resolve_selector(runtime_selector)
            effective = lineage.effective_harness(runtime, facts.doc)
        except (profile_mod.ProfileError, lineage.LineageError):
            return None
        recorded = str(harness_name or "").strip() or effective
        if recorded == effective:
            try:
                row = self.borrow_options(runtime.selector)
            except (profile_mod.ProfileError, lineage.LineageError):
                row = None
            if row is not None:
                for item in row.get("options") or []:
                    if item.get("name") == lender:
                        return dict(item)
        # Either the session's harness is not the profile's, or the lender is
        # no longer in the profile list (removed, renamed). The question is
        # about this exact pair, so ask it rather than report nothing.
        entry = facts.harness(recorded)
        return borrowing.validate(runtime, lender, entry=entry, doc=facts.doc).to_dict()

    # -- writers ---------------------------------------------------------

    def tick(self) -> None:
        """One turn of the clock: rebuild whatever the interval makes due.

        The background threads call this on their own schedule; tests drive it
        directly so they never have to sleep.
        """
        self._facts.tick()
        self._rows.tick()

    def invalidate(self) -> None:
        """Forget everything: the next read sees the config as it is now.

        Called after the daemon writes ``~/.claunch.yaml`` itself. Writes made
        outside the daemon (``claunch create``, ``set-token``) are picked up by
        the next clock tick instead.
        """
        self._rows.invalidate()
        self._facts.invalidate()

    # -- the one expensive read ------------------------------------------

    def _build_row(self, selector: str) -> dict:
        """Validate every lender against one runtime, once."""
        facts = self.facts()
        runtime = profile_mod.resolve_selector(selector)
        harness_name = lineage.effective_harness(runtime, facts.doc)
        entry = facts.registry.get(harness_name)
        capability = borrowing.capability(entry)
        options: List[dict] = []
        if capability["allowed"]:
            for lender in facts.profiles:
                report = borrowing.validate(
                    runtime, lender.name, entry=entry, doc=facts.doc
                ).to_dict()
                report["name"] = lender.name
                report["selectable"] = bool(report["valid"])
                report["label"] = (
                    lender.name
                    if report["valid"]
                    else f"{lender.name} — {report['message']}"
                )
                options.append(report)
        return {
            "profile": runtime.selector,
            "harness": harness_name,
            "capability": capability,
            "options": options,
        }
