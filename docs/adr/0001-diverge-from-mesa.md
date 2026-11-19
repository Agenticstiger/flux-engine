# ADR 0001: diverge from Mesa for the simulation substrate

## Status

Accepted

## Context

Mesa 3 (Apache-2.0, JOSS 2025, roughly 3.8k GitHub stars) is the standard
Python agent-based-modeling framework: `Model(rng=...)`, a `batch_run`
helper with seed control, grids, and a visualization layer.
Borrow-before-build search: "agent-based modeling python 3 deterministic
seed batch_run" (2026-09-06); fetched
https://github.com/projectmesa/mesa.

FLUX's kernel needs a contract-bound, byte-stable, seed-tree scheduler:
every decision (cohort allocation, trait draw, signal, detection,
transition, delivery/suppression, assignment, emit) must be reproducible
byte-for-byte from a seed, hashed into a run digest, and free of any
floating-point accumulation path. That is a narrow, closed vocabulary of a
few hundred lines -- not a general research harness.

## Decision

Do not depend on Mesa. Write the scheduler, clock, and seed tree directly
in `flux_engine.core` / `flux_engine.world` / `flux_engine.runtime`.

## Consequences

- No grid/visualization/general-ABM surface to fight or route around.
- The scheduler's total-order key and the seed tree's derivation function
  are flux_engine-specific and fully within this project's own test and
  digest-stability guarantees, rather than inherited from a library whose
  primary users are researchers plotting agent trajectories.
- The cost is writing (and testing) a scheduler ourselves. Given the
  target is a closed vocabulary of a few hundred lines, this is judged
  cheaper than adapting Mesa's `Model`/`Agent`/`Scheduler` classes to a
  determinism contract they were never designed to hold.
