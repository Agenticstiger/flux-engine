# ADR 0003: aggregate-only calibration; no SDV, no record-level fitting in the v0.1/v0.2 build

## Status

Accepted (W0). **Amended 2026-11-11:** the opt-in `[synth]` extra named under
Decision was removed, because nothing imported it. The rule itself is unchanged.

## Context

Borrow-before-build search: "SDV synthetic data vault license Business
Source License" (2026-09-06); fetched
https://datacebo.com/blog/sdv-bsl-license/. SDV's core package is
BSL 1.1: non-production use is always allowed, but production use is
restricted, and each release converts to MIT only four years after that
release ships (v1.0.0 converts 2027-03-28). Depending on SDV in a
production FLUX Simulation Engine would put a restricted license under
every downstream user.

Separately, and independent of licensing: the project's scope is to build
synthetic rows from a contract schema, calibrated on aggregate statistics
only and never on real records, so that no synthetic data is derived from
personal data. Record-level model fitting (SDV, synthcity's CTGAN/TVAE, or
any generative model trained on real rows) is a different technique from
schema-driven generation calibrated on aggregate statistics, and is out of
scope for the v0.1/v0.2 build regardless of which library would supply it.

## Decision

- The v0.1 generator (`flux_engine.generator`) produces rows from a FLUID
  expose schema's declared column types and four distribution families,
  parameterized by aggregate statistics -- never by fitting a model to
  real records.
- No record-level library is a dependency, in any extra. synthcity
  (Apache-2.0) and SDMetrics (MIT) were first listed as an opt-in `[synth]`
  extra for a customer's own record-level workflow. Nothing imported it, so
  it was removed.
- SDV is not a dependency, in any extra, under any circumstance, while its
  core package remains BSL 1.1.

## Consequences

- No production-license exposure from a restricted-license dependency.
- The aggregate-only rule is enforced by what the v0.1/v0.2 code paths
  call, not only by a license choice -- `flux_engine.generator` and the
  fitted calibration path (v0.2) both take aggregate statistics as their
  only cross-boundary input, never a stream of individual records.
