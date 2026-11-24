# Clique selection: what a profiler does and states

This note is for the authors of a profiler, such as forge-cli's `fluid profile`. It is guidance, not a
specification: the rules a profile must meet are in [PROFILE-SPEC.md](./PROFILE-SPEC.md) section 5a and section 3a,
and it is the profile reader, `flux_stats.profile`, that enforces them. The decision behind it is
[ADR 0035](../adr/0035-number-columns-are-drawn-through-a-gaussian-copula-of-published-rank-correlations.md)
decision 10.

FLUX consumes the cliques a profile publishes as they are. It chooses no clique, reweighs no count and fits no
model. Everything that chooses or estimates happens where the data lives, in the profiler, and the profile says
what was done.

## 1. What FLUX reads

- **Cliques per table**, each a `cliqueMarginal/1` (two or more categorical columns) or a `cliqueMarginal/2` (one
  to 32 columns, categorical with `levels` or number and timestamp with `bins`), its noisy counts row-major.
- **Agreement.** Cliques that share a column list the same levels or edges for it, and their noisy sums over the
  shared columns are exactly equal, compared as rationals. A profiler that makes its tables consistent writes the
  values it computed exactly. Two cliques over one set of columns are refused.
- **A junction tree.** The cliques of a table should have the running intersection property: for every column,
  the cliques that hold it are connected in the maximum spanning forest of the clique graph. `fluxsim init`
  refuses a set that has none (`FLX-3402`), naming two cliques that hold a column with nothing between them that
  does.
- **Limits.** At most 1,024 cliques in a table; at most 32 columns and 1,000,000 cells in a clique; at most
  10,000,000 cells over the cliques of a table. AIM's default model size of 80 MB, at eight bytes a cell, keeps a
  model within the last. The reader also limits the work a table asks of it, from the structure alone: the cells of
  each clique times the marginals it sums them onto, at most 40,000,000 over the table (PROFILE-SPEC.md section 5a).

## 2. What the profile states: `cliqueSelection`

Per table, as PROFILE-SPEC.md section 5a specifies:

- `method`: `aim`, `mst`, `fixed` (cliques named before the data was read) or `other`;
- `producer`: a URI naming the tool and its version, for instance OpenDP's package at the version that ran;
- `selection`: the share of the budget spent choosing the cliques (absent for `fixed`);
- `domain`: the share spent releasing levels or bin edges from the data, when they were not public;
- `published`: `measured`.

ADR 0019 let the loss of choosing cliques go unstated, as slack below the total. A profile of format 1-4-0
states it, so the evidence can show how the total was spent: on choosing, on the domain, and on each clique.

## 3. A route with OpenDP

OpenDP 0.16.0 (MIT) has `make_contingency_table` in `opendp.extras.mbi`, with the algorithms `AIM`, `MST`, `Fixed`
and `Sequential`, installed by the `opendp[mbi]` extra, which pins `mbi` 1.1.0. It is the route this note
recommends; any tool that meets sections 1 and 2 will do.

1. **The domain from public structure.** Give every categorical column its `keys` from the contract's levels. For a
   number or timestamp column, map each value to its FLUX bin first and pass the bin's index as a categorical key,
   because OpenDP's `cuts` make bins open on the left, `(c_(i-1), c_i]`, while FLUX's are closed on the left,
   `[e_i, e_(i+1))`. Choose the edges without the data, from the contract's declared range, so that
   `publicStructure` holds. Any public edges will do. For a date column, calendar months are the natural choice,
   since a profiler can fix them without looking at the data. A column given neither keys nor cuts has its levels
   released from a noisy one-way marginal, which spends part of the budget: state that share in `domain`.
2. **The unit.** One row of the unit's expose. When the profile's unit follows child rows (ADR 0037), give every
   followed table the unit's identifier, and truncate each followed relationship to `maxChildren` children per
   parent before any measurement, so that every table's contribution `m_T` is the one the profile states.
3. **The method.** MST measures every one-way marginal and a tree of pairs. AIM chooses marginals adaptively
   against a workload; the consumer queries a request carries, when it carries them, are a natural one.
4. **The budget.** Split the total between choosing and measuring and the profile's other noisy statistics, and
   state each part. OpenDP gives choosing a tenth of the budget by default for both methods; private-pgm's MST
   script gives it a third.
5. **What to publish.** Measured cliques: the marginals that were measured, with the one-way measurements folded in
   and made consistent by least squares (ordinary least squares with equal scales, weighted by the inverse of each
   variance otherwise), which keeps PROFILE-SPEC.md section 5a's condition on the covariance. Each is a clique
   with its noise block and share. A tree of pairs, which is what MST measures, always has a junction tree.
   Estimated cliques, the marginals of a model such as AIM's fitted to the measurements, are not read in profile
   format 1-4-0: a clique with an `estimate`, a `measurements` key and `published: estimated` are refused. A
   profiler that runs AIM publishes the marginals it measured, and checks that they have a junction tree. When a
   later format reads estimated cliques, an estimate's variance will have to be a function of the measurement
   design and the noise scales only, and it will be part of the structure a profile declares public: a variance
   computed from the data would be a number released outside the account.
6. **The noise sampler.** The reader checks the arithmetic of a share against a scale, and cannot check how the
   noise was drawn. A sampler that draws a Laplace or a Gaussian variable with ordinary floating-point arithmetic
   leaks through the least significant bits of its output, and breaks the guarantee its scale states (I. Mironov,
   "On significance of the least significant bits for differential privacy", ACM CCS 2012). A profiler must use a
   float-safe sampler: the exact discrete samplers of Canonne, Kamath and Steinke (the discrete Laplace and the
   discrete Gaussian, which a profile names as the `discreteGaussian` mechanism for integer counts), the snapping
   mechanism of Mironov, or a library that documents one of them for the version in use. OpenDP's measurements are
   built for this; check its documentation for the version that ran.
7. **The numbers.** Noisy counts are neither rounded nor withheld, since the noise is the protection. Write them as
   decimal strings, exactly.
8. **Check before publishing.** Read the profile back with `flux_stats.profile`, the reader the engine uses. It
   imports the standard library only, so a profiler can run it without the engine.

## 4. What FLUX refuses

The whole document, with `FLX-5036` and the place in it: cliques that disagree; a part in the wrong measure, a
share below what its mechanism spends, or parts that add up to more than the total; an estimated clique or table
(`estimate`, `measurements`, `published: estimated`); a `selection` share under the method `fixed`; a document
beyond a limit. At `init`, with `FLX-3402`: cliques with no junction tree, and levels that are not the contract's.

## 5. Versions checked (2026-11-23)

- OpenDP 0.16.0 (MIT): `opendp.extras.mbi`, its documentation and source; the `opendp[mbi]` extra pins `mbi` 1.1.0.
- private-pgm, github.com/ryan112358/private-pgm, Apache-2.0, head `bad442f` of 2026-11-18: `mechanisms/aim.py` (by
  default `16` rounds per column, a model size limit of 80 MB, about nine tenths of each round's share spent
  measuring) and `mechanisms/mst.py` (thirds: one-way marginals, selection, pairs). Its package `mbi` 2.1.0 on PyPI
  is Apache-2.0 and needs jax 0.9 or later; OpenDP's extra pins the earlier 1.1.0.
- smartnoise-synth 1.0.8 (MIT): MST and AIM synthesizers on `mbi`, and an equal-width `BinTransformer`.
- R. McKenna, B. Mullins, D. Sheldon and G. Miklau, "AIM: An Adaptive and Iterative Mechanism for Differentially
  Private Synthetic Data", PVLDB 15(11):2599-2612, 2022, arXiv 2201.12677.
- R. McKenna, G. Miklau and D. Sheldon, "Winning the NIST Contest: A scalable and general approach to differentially
  private synthetic data", Journal of Privacy and Confidentiality 11(3), 2021, DOI 10.29012/jpc.778 (MST).
- I. Mironov, "On significance of the least significant bits for differential privacy", ACM CCS 2012, DOI
  10.1145/2382196.2382264.
- C. L. Canonne, G. Kamath and T. Steinke, "The discrete Gaussian for differential privacy", NeurIPS 2020.
