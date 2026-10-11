"""Seeded PRNG (mulberry32). The same seed gives the same sequence -- the walk
and witness replay depend on it, so nothing here uses :mod:`random`."""

from __future__ import annotations

from typing import Optional, Sequence, TypeVar

T = TypeVar("T")
_M = 0xFFFFFFFF


def _imul(a: int, b: int) -> int:
    return (a * b) & _M


class Rng:
    def __init__(self, seed: int, state: Optional[int] = None):
        self._s = (seed if state is None else state) & _M

    def next(self) -> float:
        """A float in [0, 1)."""
        self._s = (self._s + 0x6D2B79F5) & _M
        t = self._s
        t = _imul(t ^ (t >> 15), t | 1)
        t ^= (t + _imul(t ^ (t >> 7), t | 61)) & _M
        return ((t ^ (t >> 14)) & _M) / 4294967296

    def pick(self, items: Sequence[T]) -> T:
        if not items:
            raise ValueError("rng.pick: empty list")
        return items[int(self.next() * len(items))]

    def state(self) -> int:
        return self._s
