"""The lists a cflow v2 input's ``options:`` names, read from this launcher.

``cflow2/inputs.py`` declares ``options: {from, of, empty}`` and checks a
value against a *catalog*; the engine has no launcher config of its own, so
the catalog lives here, beside the code that later acts on the value
(``spawn``). One :class:`Catalog` reads ``~/.claunch.yaml`` once and answers
every question of one check (a form edit, a start, a run-page write).

What each list holds -- what the value will be accepted as further on:

- ``profiles``: the profile names (``claunch ls``). A selector
  (``NAME:HARNESS``) is not a profile; the harness has its own input.
- ``harnesses``: the harnesses ``harness_policy`` admits for the profile --
  the same rule spawn's ``PROFILE:HARNESS`` selector meets.
- ``models``: what spawn's ``model`` takes for a child of the profile, on
  the profile's own harness (:func:`spawn._resolve_model_preset`). Claude
  and Codex are read differently, and each has its own function:

  * claude (:func:`claude_models`): every preset. ``default`` is the
    harness default, ``small``/``large``/``xlarge`` are Claude Code's
    aliases resolved through the profile's environment, and ``subagent``
    falls back to ``small`` -- no preset is ever refused. Plus the harness's
    model choices (``haiku``, ``sonnet``...).
  * codex (:func:`codex_models`): only the presets the profile's provider
    declares a model for (role fallbacks applied). Codex never reads the
    provider's models itself (``translators.codex``), so spawn refuses an
    undeclared preset instead of launching ``-c model=small``. Plus the
    harness's model choices (``luna``, ``sol``... and ``model_choices:``
    overrides).
  * any other harness (:func:`declared_models`): spawn's non-claude path,
    the rule Codex follows.

  A model name outside these lists used to pass through to the harness;
  the input refuses it now (claunch-iq1xc, the user's decision).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional

from . import harness_policy, harnesses, lineage, provider_spec, providers, store
from . import profile as profile_mod

#: What a profile cannot be read as, for every list below: the list is
#: "cannot say" (None) rather than empty, and the profile's own input reports
#: the profile.
_READ_ERRORS = (
    profile_mod.ProfileError,
    lineage.LineageError,
    providers.ProviderError,
    harness_policy.HarnessPolicyError,
    harnesses.HarnessConfigError,
)

#: The environment variables a session's harness is pointed at its profile
#: with; :func:`profile_from_env` reads them back.
_PROFILE_ENV = ("CLAUDE_CONFIG_DIR", "CODEX_HOME")


def claude_models(spec: providers.ProviderSpec, entry: Optional[harnesses.Harness]) -> List[str]:
    """Every preset, then the claude harness's model choices."""
    del spec  # every preset resolves on claude, declared or not
    return _join(provider_spec.MODEL_ROLES, entry)


def codex_models(spec: providers.ProviderSpec, entry: Optional[harnesses.Harness]) -> List[str]:
    """The presets the provider declares a model for, then codex's model
    choices."""
    return declared_models(spec, entry)


def declared_models(spec: providers.ProviderSpec, entry: Optional[harnesses.Harness]) -> List[str]:
    """Spawn's non-claude rule: a preset counts only with a declared model."""
    return _join([role for role in provider_spec.MODEL_ROLES if spec.model(role)], entry)


def _join(presets, entry: Optional[harnesses.Harness]) -> List[str]:
    out: List[str] = []
    for name in list(presets) + list(getattr(entry, "models", None) or ()):
        if name not in out:
            out.append(name)
    return out


_MODEL_RULES = {
    harnesses.CLAUDE_HARNESS: claude_models,
    "codex": codex_models,
}


class Catalog:
    """The ``options`` lists for one check (``cflow2/inputs.py``'s catalog).

    ``session_profile`` is the profile of the session the run belongs to --
    what an empty ``options.empty: session`` input stands for. Unknown
    (``None``), the lists that depend on it are not checked."""

    def __init__(self, session_profile: Optional[str] = None, doc: Optional[dict] = None):
        # read on first use: a definition without ``options`` costs nothing
        self._loaded = doc
        name = profile_mod.split_selector(session_profile)[0] if session_profile else None
        self.session_profile = name or None
        self._profiles: Optional[Dict[str, profile_mod.Profile]] = None
        self._registry: Optional[Dict[str, harnesses.Harness]] = None

    @property
    def _doc(self) -> dict:
        if self._loaded is None:
            self._loaded = store.load()
        return self._loaded

    def _by_name(self) -> Dict[str, profile_mod.Profile]:
        if self._profiles is None:
            self._profiles = {p.name: p for p in profile_mod.list_all()}
        return self._profiles

    def _harnesses(self) -> Dict[str, harnesses.Harness]:
        if self._registry is None:
            self._registry = harnesses.registry(self._doc)
        return self._registry

    def profiles(self) -> List[str]:
        return list(self._by_name())

    def harnesses(self, profile: Optional[str]) -> Optional[List[str]]:
        p = self._by_name().get(str(profile or ""))
        if p is None:
            return None
        try:
            return harness_policy.allowed_names(p, self._doc)
        except _READ_ERRORS:
            return None

    def models(self, profile: Optional[str]) -> Optional[List[str]]:
        p = self._by_name().get(str(profile or ""))
        if p is None:
            return None
        try:
            harness = lineage.effective_harness(p, self._doc)
            spec = providers.spec_for(p, doc=self._doc)
        except _READ_ERRORS:
            return None
        rule = _MODEL_RULES.get(harness, declared_models)
        return rule(spec, self._harnesses().get(harness))

    def empty_profile(self, meaning: str) -> Optional[str]:
        if meaning == "delegate":
            cfg = store.daemon_config(self._doc)
            return str(cfg.get("delegate_profile") or "") or \
                store.DAEMON_DEFAULTS["delegate_profile"]
        return self.session_profile


def profile_from_env(env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """The profile this process's session runs on, read from the directory
    its harness was pointed at (:data:`_PROFILE_ENV`); ``None`` outside a
    profile's directory."""
    env = os.environ if env is None else env
    dirs = []
    for var in _PROFILE_ENV:
        value = str(env.get(var) or "").strip()
        if value:
            try:
                dirs.append(Path(value).resolve())
            except OSError:
                continue
    if not dirs:
        return None
    try:
        profiles = profile_mod.list_all()
    except profile_mod.ProfileError:
        return None
    for p in profiles:
        try:
            root = p.config_dir.resolve()
        except OSError:
            continue
        for d in dirs:
            if d == root or root in d.parents:
                return p.name
    return None
