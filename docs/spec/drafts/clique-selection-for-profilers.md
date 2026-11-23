# Clique selection: what a profiler does and states (draft for profile format 1-4-0)

**Draft for design review, 2026-11-23. Nothing on this page is implemented.** It is the profiler's half of decision 10
of [ADR 0035](../../adr/0035-number-columns-are-drawn-through-a-gaussian-copula-of-published-rank-correlations.md),
written for the authors of a profiler such as forge-cli's `fluid profile`. The format text it relies on is section 1
of [correlations-0.11.0.md](./correlations-0.11.0.md).

FLUX consumes the cliques a profile publishes as they are. It chooses no clique, reweighs no count and fits no model:
`fluxsim init` arranges the cliques of each table into a junction tree and draws each column given its separator (ADR
0021, ADR 0036). Everything that chooses or estimates happens where the data lives, in the profiler, and the profile
says what was done.

## 1. What FLUX reads

- **Cliques per table**, each a `cliqueMarginal/1` (two or more categorical columns) or a `cliqueMarginal/2` (one or
  more columns, categorical with `levels` or number and timestamp with `bins`), its noisy counts row-major.
- **Agreement.** Cliques that share a column list the same levels or edges for it, and their noisy sums over the shared
  columns are exactly equal, compared as rationals. A profiler that makes its tables consistent writes the values it
  computed exactly.
- **A junction tree.** The cliques of a table have the running intersection property: for every column, the cliques
  that hold it are connected in the maximum spanning forest of the clique graph. FLUX refuses a set that has none
  (`FLX-3402`), naming two cliques that hold a column with nothing between them that does.
- **Columns FLUX draws from cliques.** Not a key, not a column a contract rule sets, not a column with a null fraction,
  and, for a categorical column, the contract's levels when the contract declares them. A clique over another column is
  passed over or refused as ADR 0021 says.
- **Limits.** At most 1,024 cliques and 4,096 measurements in a table; at most 32 columns and 1,000,000 cells in a
  clique; at most 10,000,000 cells over the cliques of a table. AIM's default model size of 80 MB, at eight bytes a
  cell, keeps a model within the last.

## 2. What the profile states: `cliqueSelection`

Per table, as section 1.8 of the draft specifies:

- `method`: `aim`, `mst`, `fixed` (cliques named before the data was read) or `other`;
- `producer`: a URI naming the tool and its version, for instance OpenDP's package at the version that ran;
- `selection`: the share of the budget spent choosing the cliques (absent for `fixed`);
- `domain`: the share spent releasing levels or bin edges from the data, when they were not public;
- `published`: `measured` or `estimated`, and with `estimated` the `measurements`, each its columns and its noise
  block.

ADR 0019 let the loss of choosing cliques go unstated, as slack below the total. A profile of format 1-4-0 states it,
so the evidence can show how the total was spent: on choosing, on the domain, and on each measurement.

## 3. A route with OpenDP

OpenDP 0.16.0 (MIT) has `make_contingency_table` in `opendp.extras.mbi`, with the algorithms `AIM`, `MST`, `Fixed` and
`Sequential`, installed by the `opendp[mbi]` extra, which pins `mbi` 1.1.0. It is the route this note recommends; any
tool that meets sections 1 and 2 will do.

1. **The domain from public structure.** Give every categorical column its `keys` from the contract's levels. For a
   number or timestamp column, map each value to its FLUX bin first and pass the bin's index as a categorical key,
   because OpenDP's `cuts` make bins open on the left, `(c_(i-1), c_i]`, while FLUX's are closed on the left,
   `[e_i, e_(i+1))`. Choose the edges without the data, from the contract's declared range, so that `publicStructure`
   holds. A column given neither keys nor cuts has its levels released from a noisy one-way marginal, which spends
   part of the budget: state that share in `domain`.
2. **The unit.** One row of the unit's expose. When the profile's unit follows child rows (ADR 0037), give every
   followed table the unit's identifier, and truncate each followed relationship to `maxChildren` children per parent
   before any measurement, so that every table's contribution `m_T` is the one the profile states.
3. **The method.** MST measures every one-way marginal and a tree of pairs. AIM chooses marginals adaptively against
   a workload; the consumer queries a request carries, when it carries them, are a natural one.
4. **The budget.** Split the total between choosing and measuring and the profile's other noisy statistics, and state
   each part. OpenDP gives choosing a tenth of the budget by default for both methods; private-pgm's MST script gives it
   a third.
5. **What to publish.**
   - **Measured cliques**, natural for MST: the measured pairs, with the one-way measurements folded in, made
     consistent by least squares (ordinary least squares with equal scales, weighted by the inverse of each variance
     otherwise), which keeps PROFILE-SPEC.md section 5a's condition on the covariance. Each pair is a clique with its
     noise block and share. A tree of pairs always has a junction tree.
   - **Estimated cliques**, needed for AIM, whose measured marginals need not have a junction tree: the model's
     marginals over the maximal cliques of its own junction tree, each a `cliqueMarginal/2` with an `estimate` that
     states the variance per cell the profiler vouches for, and every measurement listed with its noise block and share.
     Such cliques agree by construction. How a profiler bounds an estimate's variance is open for review (ADR 0035);
     one that cannot defend a bound publishes measured cliques.
6. **The numbers.** Noisy counts are neither rounded nor withheld, since the noise is the protection. Write them as
   decimal strings, exactly.
7. **Check before publishing.** Read the profile back with `flux_stats.profile`, the reader the engine uses. It imports
   the standard library only, so a profiler can run it without the engine.

## 4. What FLUX refuses

The whole document, with `FLX-5036` and the place in it: cliques that disagree; a measurement whose columns lie in no
published clique; an estimated table with a clique that has a `noise` block, or a measured one with an `estimate`; a
part in the wrong measure, a share below what its mechanism spends, or parts that add up to more than the total; a
document beyond a limit. At `init`, with `FLX-3402`: cliques with no junction tree, and levels that are not the
contract's.

## 5. Versions checked (2026-11-23)

- OpenDP 0.16.0 (MIT): `opendp.extras.mbi`, its documentation and source; the `opendp[mbi]` extra pins `mbi` 1.1.0.
- private-pgm, github.com/ryan112358/private-pgm, Apache-2.0, head `bad442f` of 2026-11-18: `mechanisms/aim.py` (by
  default `16` rounds per column, a model size limit of 80 MB, about nine tenths of each round's share spent measuring)
  and `mechanisms/mst.py` (thirds: one-way marginals, selection, pairs). Its package `mbi` 2.1.0 on PyPI is
  Apache-2.0 and needs jax 0.9 or later; OpenDP's extra pins the earlier 1.1.0.
- smartnoise-synth 1.0.8 (MIT): MST and AIM synthesizers on `mbi`, and an equal-width `BinTransformer`.
- R. McKenna, B. Mullins, D. Sheldon and G. Miklau, "AIM: An Adaptive and Iterative Mechanism for Differentially
  Private Synthetic Data", PVLDB 15(11):2599-2612, 2022, arXiv 2201.12677.
- R. McKenna, G. Miklau and D. Sheldon, "Winning the NIST Contest: A scalable and general approach to differentially
  private synthetic data", Journal of Privacy and Confidentiality 11(3), 2021, DOI 10.29012/jpc.778 (MST).
