"""The price table behind cost estimates, keyed by provider and then model.

Settings ▸ Model prices writes it to the config file and every cost estimate
reads it back (the session card and detail panel in the dashboard, and any
daemon-side figure priced per request)::

    daemon:
      model_prices:
        deepseek:                       # a provider name (providers.<name>)
          deepseek-v4-pro: {input: 0.3, cache_read: 0.006, output: 1.2}
        "*":                            # any provider without its own row
          claude-opus-5: {input: 15, cache_read: 1.5, cache_write: 18.75, output: 75}

The provider is the one the session's profile resolves to
(:func:`providers.resolve_name` — own setting, then an ancestor's, then the
global default; a borrowing session runs on its lender's provider), so the
same model id served by two backends can carry two prices. A lookup tries the
provider's own row first and then the ``"*"`` row (:func:`rate_for`).

The table used to be keyed by model id alone. Such a row (a mapping of rate
names to numbers at the top level) still reads, as a ``"*"`` row
(:func:`normalize`), so a table written before the provider tier keeps
pricing until it is saved again in the new shape.
"""

from __future__ import annotations

import math
from typing import Dict, Optional

RATE_KEYS = ("input", "cache_read", "cache_write", "output")
ANY = "*"

Rates = Dict[str, float]
Table = Dict[str, Dict[str, Rates]]


def _is_rate_row(value: object) -> bool:
    """Whether ``value`` is one model's rates (the pre-provider shape)."""
    return isinstance(value, dict) and bool(value) and all(
        k in RATE_KEYS for k in value)


def normalize(raw: object) -> Table:
    """The stored table as ``{provider: {model: rates}}``.

    Reads both shapes: a provider block (model → rates) and a legacy row
    (rates at the top level, keyed by model id), which lands under ``"*"``.
    A provider block's row for the same model wins over a legacy row. Entries
    that are neither are skipped — this is the read path, and a hand-edited
    config should not take every estimate down with it.
    """
    out: Table = {}
    if not isinstance(raw, dict):
        return out
    legacy: Dict[str, Rates] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        if _is_rate_row(value):
            legacy[key] = dict(value)
            continue
        block = {m: dict(r) for m, r in value.items()
                 if isinstance(m, str) and isinstance(r, dict)}
        if block:
            out[key] = block
    if legacy:
        out[ANY] = {**legacy, **out.get(ANY, {})}
    return out


def clean(raw: object) -> Table:
    """Validate a table sent by Settings and return it in stored form.

    Accepts the provider-keyed shape and, for an older client, legacy rows at
    the top level (stored under ``"*"``). Raises :class:`ValueError` with the
    reason a 400 should carry. A rate left out (or ``None``) is not stored —
    an unset cache-write rate must stay unset, since the estimate prices it
    at the read rate and 0 would turn that off. Providers and models with no
    rates left are dropped.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("'prices' must be an object keyed by provider")
    out: Table = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("price rows are keyed by a non-empty provider name")
        if not isinstance(value, dict):
            raise ValueError(f"prices for {key!r} must be an object")
        if _is_rate_row(value):
            provider, rows = ANY, {key: value}
        else:
            provider, rows = key.strip(), value
        for model, rates in rows.items():
            if not isinstance(model, str) or not model.strip():
                raise ValueError(f"prices under {provider!r} are keyed by a non-empty model id")
            if not isinstance(rates, dict):
                raise ValueError(f"prices for {provider}/{model} must be an object")
            row: Rates = {}
            for rk in RATE_KEYS:
                v = rates.get(rk)
                if v is None:
                    continue
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    raise ValueError(f"price {rk!r} for {provider}/{model} must be a number") from None
                if not math.isfinite(v) or v < 0:
                    raise ValueError(f"price {rk!r} for {provider}/{model} must be a "
                                     "non-negative finite number")
                row[rk] = v
            if row:
                out.setdefault(provider, {})[model.strip()] = row
    return {p: rows for p, rows in out.items() if rows}


def rate_for(prices: object, provider: str, model: str) -> Optional[Rates]:
    """The rates for ``model`` served by ``provider``, or None if unpriced.

    The provider's own row first, then the ``"*"`` row. ``prices`` may be the
    stored table in either shape. A row with no rates (only a hand-edited
    config holds one — :func:`clean` drops it) counts as absent, so it does
    not hide the ``"*"`` row or price a model at 0. None means "not priced"
    — callers show no estimate rather than a zero one.
    """
    if not model:
        return None
    table = normalize(prices)
    for p in ((provider, ANY) if provider and provider != ANY else (ANY,)):
        rates = table.get(p, {}).get(model)
        if isinstance(rates, dict) and rates:
            return rates
    return None


def profile_providers(doc: Optional[dict] = None) -> Dict[str, str]:
    """Each profile's effective provider, by profile name.

    What the dashboard needs to price a session: it knows the session's
    profile (and lender), not where that profile's provider comes from.
    """
    from . import profile as profile_mod
    from . import providers, store

    doc = store.load() if doc is None else doc
    out: Dict[str, str] = {}
    for item in profile_mod.list_all():
        try:
            out[item.name] = providers.resolve_name(item, doc)
        except Exception:  # noqa: BLE001 - one broken profile must not unprice the rest
            continue
    return out
