# The profile request

A profile request lists what a profiler must measure for a bundle. `fluxsim init` writes it from a FLUID contract when no profile is given. The format is in the [profile request specification](https://github.com/Agenticstiger/flux-engine/blob/v0.13.0/docs/spec/PROFILE-REQUEST-SPEC.md) (check the installed engine's own text first: the init help names the same file).

## What it holds

A request holds names and registered identifiers and nothing else. It carries no record, no value of the data and no answer. It is one YAML document. It stays outside the bundle, so it is not an input of the bundle's `inputDigest`.

- `_schemaURL`: names the format and its version.
- `contract`: optionally the digest of the contract it was made from, so a profiler and `init` can tell they belong together.
- `encoding`: optionally the grid for encoded quantities.
- `tables`: one entry per table, keyed by the FLUID expose id. Each lists its columns with a domain (category, number or timestamp), the column metrics asked (counts, null counts, distinct counts, minimum, maximum, sum, quantiles), the statistics asked (such as a mean, a histogram, quantiles, level or support), and optionally a row count.
- `relationships`: the children-per-parent statistics asked of each declared relationship.
- `facts`: the fact shares asked of the contract's rules.

Request format 1-2-0 adds four optional asks, which ask for the statistics of profile format 1-4-0: a table's `rankCorrelations` (`kendallTau/1` or `spearmanRho/1` over 2 to 64 number or timestamp columns, which the profile states for every pair), a table's `cliqueColumns` (the columns a profiler may join into cliques), and a relationship's `groupBy` (parent columns its statistics are grouped by) and `linked` (a child column grouped by a parent column through it). A request that uses one names format 1-2-0; one that names an earlier format and holds one is refused with FLX-5038. It still holds names only: how much noise to add, and where, is the data owner's choice.

`fluxsim init` writes the request in name order and refuses with FLX-5038 a contract whose table, column or fact name the reader would refuse, naming it. Run `fluxsim explain FLX-5038` for the remedy.

## Where it is written

`fluxsim init bundle --contract contract.orders.fluid.yaml` writes `bundle.profile-request.yml` beside the folder, never inside it, and never over a file that exists. `--request` names another place. Hand the request to the data owner with the contract; both hold no data.

## One budget per release

When the owner releases the statistics under differential privacy (grade P3), the profile has one budget for the whole release, stated once: the measure, the total privacy loss and the unit of privacy. Every noisy statistic states its own share. The profiler divides the budget over the statistics the request asks for; the engine does not choose the split.

- Parts add up: under zero-concentrated DP the parts are values of rho and rho adds; under approximate DP epsilon adds and delta adds. Every part is in the measure of the total; the engine does not convert.
- The engine refuses a profile whose parts exceed the total, or whose part is below what its mechanism spends, with FLX-5036, naming the statistic and never a value. A noisy statistic in a table whose rows one unit does not change is refused too: by default the unit is one row of one table, and a profile of format 1-4-0 may state a unit that follows child rows.
- Several releases of one data set are several profiles, each with its own budget, and the engine cannot add them.
- Asking for more statistics divides the same budget thinner, so each is noisier. A smaller request is a choice for the owner.

See [ADR 0019](https://github.com/Agenticstiger/flux-engine/blob/v0.13.0/docs/adr/0019-one-privacy-budget-per-profile-release.md). Choosing the total is the data owner's decision, not the agent's.

## The owner-side step

The owner runs forge-cli's profile command where the records live, with the contract, the request, the source and an output path: `fluid profile contract.orders.fluid.yaml --request bundle.profile-request.yml --source orders.parquet --out orders.profile.json`. The other option you may name is `--table`. Check first that the installed command's help lists `--request`; if not, ask the user to upgrade forge-cli. A source that needs a password is the owner's to run, and no password goes in a command line. You never open the records.

## What comes back

A profile document of aggregates, for example `orders.profile.json`. Only the profile crosses to FLUX. Then:

`fluxsim init bundle --contract contract.orders.fluid.yaml --profile orders.profile.json`

writes the bundle: the contract copied unchanged, and a Simulation declared from the profile's published aggregates, pinning its digest. The profile stays outside the bundle. Every number in the Simulation is labelled in its parameter register as from the profile, from the contract, or assumed.

See [write a bundle from a profile](https://github.com/Agenticstiger/flux-engine/blob/v0.13.0/docs/how-to/write-a-bundle-from-a-profile.md).
