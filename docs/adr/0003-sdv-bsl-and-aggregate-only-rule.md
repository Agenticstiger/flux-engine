# ADR 0003: aggregate-only calibration; no SDV, no record-level fitting in the v0.1/v0.2 build

## Status

Accepted. **Amended 2026-11-11:** the opt-in `[synth]` extra named under
Decision was removed, because nothing imported it. The rule itself is unchanged.
**Amended 2026-11-16:** the rule is extended. The engine never reads a
record of the data a bundle imitates, and a separate profiler package reads
records only where they live and emits only aggregates, under the
disclosure defaults set out in [the amendment](#amendment-of-2026-11-16) at
the end. One engine path, `--committed-slice`, still reads records today:
the amendment names it, and it moves into the profiler package. The
Context, Decision and Consequences above that section are unchanged.

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

## Amendment of 2026-11-16

### Context

A synthetic copy of a whole multi-table source has to be calibrated to
statistics of that source: per column, per pair of columns, per
relationship (children per parent), per period and per consumer query.
Someone has to compute those numbers from records. The decision above says
what the generator and the calibration path take as input. It does not say
who reads the records, where, or what may leave with the numbers.

### Decision

1. The engine never reads a record of the data a bundle imitates. Every
   input that describes that data is an aggregate statistic or
   non-personal reference data, such as a product catalogue. This holds
   for every code path and every extra, not only for the generator and the
   calibration path. The rows the engine writes, and reads back in
   `verify` and `ship`, are synthetic and are not such records. One path
   does not hold yet; it is named below.
2. A separate profiler package computes the aggregates. It reads records
   only where they live, inside the data owner's environment, and emits
   only aggregates: no record, sample of records or per-record value
   leaves it. It is a package of its own, so once the path below has moved
   into it, installing the engine installs no record reader.
3. The profiler applies disclosure rules before an aggregate leaves, and
   records which rule it applied to each statistic. Its defaults take
   their values from ACRO (https://github.com/AI-SDC/ACRO, MIT,
   `acro/default.yaml` at v1.1.0):
   - a cell with fewer than 10 records is suppressed and flagged
     (`safe_threshold: 10`);
   - a zero cell is disclosive (`zeros_are_disclosive: True`);
   - the counts it publishes are rounded to a multiple of 5, the base of
     ACRO's rounding mitigation (`safe_round_base: 5`);
   - no noise is added by default.

   These are output-checking conventions only; no privacy or anonymity
   guarantee is made or implied. ACRO comes from the SACRO project, whose
   tools are built to assist human output checkers, not to replace them.
4. A fidelity check's result, and any score computed from it, states a
   statistical relation between the synthetic rows and the statistics
   they are checked against (a committed state today, a profile once
   profiles exist), under the properties registered for the check, and
   nothing more. It says nothing about privacy, fairness or the validity
   of a simulated outcome.

The path that reads records today is the fidelity check's boundary side
(SEAM-FIDELITY-SPEC §4), which `--committed-slice` reaches from
`fluxsim run`, `ship` and `verify`. It reads a committed record set where
it lives. What it lets out is the list in §4 (`S`, `C1`, `C2`, the state
ordinal, the record count or declared range, and the commitment time),
plus `G` and the slice's URI and line range. It applies none of the
defaults in point 3. Its counts are exact, its support list names every
category it saw however few records hold it, and a quantile witness is a
value read from one record; the contract's `records.min` is its only
floor. It moves into the profiler package when that package ships, and
how point 3 then applies to its statistics is part of that package's
design. Until then it is the one exception to point 1.

### Consequences

- Calibrating a multi-table source keeps the rule: what crosses into the
  engine is aggregates and non-personal reference data, never a record.
- A fidelity check against a profile must treat a cell the profiler
  suppressed as unknown, not zero, and leave it out of the result:
  scored as zero, it would count against every run whose synthetic rows
  fall in that cell.
- This amendment changes no code. The profiler package, and the move of
  the boundary side into it, are later work.
