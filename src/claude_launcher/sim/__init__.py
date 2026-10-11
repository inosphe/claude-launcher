"""World-model simulations: a deterministic walk over a formal model that a
session builds from specs, code and scenarios (claunch-efnbt).

The model (state machines, variables, events, ECA rules, invariants) is what
a session writes; the engine walks it and reports where the model has no
answer (``unhandled``), two answers (``conflict``), a broken invariant, a dead
end or a runaway event chain. Every finding carries a witness that replays.
The design came from gds6's simulation v2 (its ``src/sim2`` engine), with the
game-design specifics removed; see ``docs/simulation.md``.

* :mod:`.expr` -- the JSON condition/effect language, three-valued.
* :mod:`.model` -- declarations, shape checks, initial state.
* :mod:`.engine` -- step, explore, replay.
* :mod:`.store` -- the daemon's registry: records, ownership, rounds, findings.
"""
