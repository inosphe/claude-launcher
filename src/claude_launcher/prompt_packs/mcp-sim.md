---
title: MCP — simulations
description: How the sim_* tools work — who may read, change and run a simulation, what a start launches, and what a "[claunch simulation]" line means.
check_default: true
---
## Simulations (sim_* tools)

A session builds a formal world model of a feature (state machines, events,
ECA rules quoting their sources, invariants) and the daemon walks it to find
gaps, conflicts, broken invariants and dead ends.

- Any session may read any simulation; only its owner (or the user) changes,
  runs or stops it.
- A start launches a run session that does the interpreting while a judge
  model checks its quotes. Without a usable interpreter profile the start is
  refused and the simulation is marked fault.
- Call `sim_guide` before writing a model.

A `[claunch simulation]` line in your terminal is the daemon: a simulation
you own finished, or the user answered or asked something about it.
