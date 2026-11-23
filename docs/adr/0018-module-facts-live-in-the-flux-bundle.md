# ADR 0018: module facts are declared in the FLUX bundle, never in the FLUID contract

## Status

Accepted 2026-11-21. The maintainers chose the FLUX bundle over the FLUID contract as the
home of module facts. A module is a unit of generation that adds rules to a bundle (copied
columns, children per parent, versions, periods, and so on). This record is the base every
module builds on, and it holds nothing module-specific.

## Context

A module states facts about the data it generates: this share of rows satisfies that rule,
these children belong to those parents. Three places could hold a fact:

- **The FLUID contract's `extensions`.** The contract is the data owner's promise to its
  consumers. A fact such as "the synthetic copy should satisfy this rule in about 97 % of
  rows, within 2 points" is a statement about a simulation of the data, not about the data,
  and a consumer that never heard of FLUX would read a foreign key it cannot interpret.
  Putting it there also makes the contract's digest depend on how one run of one tool is
  configured: the contract is an input of `H`, so a tolerance edit would read as a changed
  contract.
- **A new file beside the bundle.** `inputDigest` covers a closed set of files (ADR 0006),
  so a file the loader does not name is refused, and one it names becomes a second place to
  keep in step with the Simulation.
- **The FLUX bundle's Simulation, under `extensions['com.agentics.flux/1']`.** This is where
  the fidelity block, the parameter register, the scorecard thresholds and the profile pin
  already live (SEAM-FIDELITY-SPEC section 2). The Simulation belongs to the run, not to the
  data, and the namespace's keys are closed, so a fact nothing reads is refused rather than
  ignored.

The profiler is a different tool, run where the data lives, by people who may not hold the
bundle. It has to learn what to measure. Today `fluxsim init` reads a profile and cannot say
beforehand which statistics it will read, so a profile is made, found short, and made again.

## Decision

### 1. Module facts live in the FLUX bundle

A module declares its facts under the Simulation's `extensions['com.agentics.flux/1']`, in a
top-level key of its own, and under no other key and in no other document. The FLUID contract
gains no FLUX key. The one fact kind the base defines is `facts`: a list of
`{emit, rule, tolerance}` entries, each naming a rule of one expose's contract (or a rule the
bundle declares for it) and optionally a `tolerance`, the largest gap, as an exact decimal,
between the share of rows that satisfy the rule in the run and the share the profile measured
on the data. A module adds its own kinds the same way, each one a key of the namespace that
nothing else reads.

### 2. A profile request carries what the profiler must measure

`fluxsim init` given a contract and no profile writes a profile request: a document of a
flux-stats format of its own (`docs/spec/PROFILE-REQUEST-SPEC.md`) that lists every statistic,
column metric and fact share the bundle would read. It is a plain YAML file that stays outside
the bundle, as the profile does. A profiler reads it, measures exactly those aggregates where
the data lives, and writes a profile; `fluxsim init` then runs again with `--contract` and
`--profile`. The command runs in two steps because the profile can only be made once the
request exists.

The request is parsed with flux-stats' exact reader and its bounds, never with a plain YAML
load: no float, no timestamp, a bounded number of values, characters and levels, and unknown
keys refused. It names no record and no value of the data.

### 3. A fact with a tolerance is part of the run's fidelity predicate

A fact that carries a tolerance says when the run is faithful, so it is bound into `H` before
any row is drawn, not only into `inputDigest`. The emit's entry for `H` gains a `facts` member
when, and only when, the Simulation declares a fact for that emit. A bundle that declares none
hashes exactly as before. `G`, the digest of what a committed state depends on, leaves the
member out: a tolerance edit moves `H` and not `G`, the rule SEAM-FIDELITY-SPEC section 8
already states for every other tolerance.

### 4. What is not decided here

How a module's facts are measured, scored and generated is the module's own record. This
record fixes only where facts live, how a profiler is told what to measure, and what a
tolerance does to `H`.

## Consequences

- A contract stays what its owner published. The same contract serves any number of
  Simulations, each with its own tolerances.
- A bundle that declares no fact keeps every byte and every digest.
- `fluxsim init` has two steps when there is no profile yet. A person who already holds a
  profile still runs one.
- A fact's name is `<exposeId>.<ruleId>`. The profile's `invariants` section (profile format
  1-2-0) and the scorecard's `invariants` dimension use the same name, so a fact is measured
  and scored under one name.

## Alternatives considered

- **A FLUX key in the contract.** Rejected for the reasons in the context: it ties a contract
  to one tool's run settings and moves the contract's digest on a tolerance edit.
- **One more file in the bundle.** Rejected: ADR 0006 keeps the set of input files closed, and
  a second document for facts would be a second place that can disagree with the Simulation.
- **Facts inside each emit's block (`emits[<exposeId>]`).** Rejected: a fact can read rows of
  more than one expose, and the block is per expose. The key is top level for the profile
  pin's reason, and the emit's `H` entry takes the facts that concern it.
