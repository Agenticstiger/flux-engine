# ADR 0039: an association table pairs two counted sides stub by stub

## Status

Accepted 2026-11-25. Builds on [ADR 0016](./0016-children-per-parent-are-counted.md) (the counted layout) and
[ADR 0022](./0022-rows-read-their-linked-rows.md) section 8 (bridge pairs). The format text is in
[KERNEL-SPEC-2.md](../spec/KERNEL-SPEC-2.md) section 8.7 and [SEAM-FIDELITY-SPEC.md](../spec/SEAM-FIDELITY-SPEC.md)
section 2; kernel `0.2.0` only.

## Context

**What an association table is.** A table of two foreign keys and no other column, each the one column of a
`many_to_one` relationship and neither part of a key: an enrolment of a student and a course, a membership of a person
and a club. Its rows are the edges of a bipartite graph between the two parents. A profile describes it by each
relationship's children per parent (PROFILE-SPEC.md section 5), so both sides' degree distributions are known and
their joint is not.

**What FLUX did with one.** `fluxsim init` wrote a `counts` entry for each side and then left the table out: the
fidelity block of an emit whose every column the layout sets would declare no column (`FLX-5034`), and the bundle
refused to validate because a `counts` entry named an emit it did not write. A hand-written bundle could declare the
two `counts` entries beside a third column; each side was then laid out on its own, as consecutive runs, so the two
sides followed the same row order and the same pair repeated along each run. A bridge (ADR 0022 section 8) holds every
pair once but draws its pairs uniformly from the product of the parents, so neither side keeps its degrees.

**Borrow-before-build (2026-11-25).** Inside the repository: `generator/counts.counted_layout` (largest remainder,
exact sums), `pass_excess`, `core/permute.permute2` and `kernel2.permute2_array` (a keyed Feistel permutation that
can be read at any index), the bridge pair layout, and the scorecard's children-per-parent row. No graph library is a dependency. Searches: the bipartite configuration model and
Havel-Hakimi in networkx; Gale-Ryser realisation; removing repeated edges of a configuration model by
degree-preserving switches; synthetic data generators for many-to-many tables. What was read:

- networkx 3.7, `networkx/algorithms/bipartite/generators.py` (BSD-3-Clause): `configuration_model` repeats each node
  by its degree on both sides, shuffles both stub lists from one random state and pairs them in order, into a
  multigraph; `havel_hakimi_graph` joins the first side's node with the most stubs left to the second side's nodes
  with the most; neither checks more than the equal sums. `networkx/algorithms/swap.py`: `double_edge_swap` with
  `nswap` and a `max_tries` bound. Read as a reference, not a dependency: a shuffled stub list cannot give one row
  without the others, and there is no keyed, bounded repair.
- D. Gale, "A theorem on flows in networks", and H. J. Ryser, "Combinatorial properties of matrices of zeros and
  ones", both 1957: two degree sequences with equal sums belong to a simple bipartite graph exactly when the second,
  in decreasing order, is dominated by the conjugate of the first. M. Krause (1996) gives a constructive proof.
- J. Sjostrand, "Making multigraphs simple by a sequence of double edge swaps" (arXiv 1904.06999): any multigraph whose
  degree sequence is graphical can be made simple by finitely many double edge swaps, each touching a repeated edge;
  no bound on their number.
- The bipartite configuration model of maximum entropy (`bicm`, MIT) and Chung-Lu generators: degrees in expectation
  only, not exact.
- Learned relational synthesizers (RelDiff, ClavaDDPM): fitted models, not a declared layout. SDV's multi-table
  synthesizer fits poorly here and is under the Business Source License (ADR 0003).

## Decision

### 1. The declaration

The namespace key `associations` lists `{emit, relationships: [a, b], distinct}`, at most one entry per emit, in the
FLUX bundle (FLUID has no spelling for it). Both relationships are on the counted layout: each side's number of rows
per parent is its `counts` entry's shape, or an even spread without one. `distinct` (default `true`) says that no pair
of parent rows repeats. The entry is bound into `H` (`G` leaves it out), so a bundle that declares none keeps every
byte and every digest. Its emit may declare an empty `fidelity.columns`, as a snapshot's may.

Refused before any row (`FLX-3403`): a side that is a key, a bridge side, `one_to_one`, implied by another relationship
or carries a `nullFraction` or a draw (every refusal of a `counts` entry), a side whose `counts` entry has a
`parentWhen`, a `given` or an expected-count hook, an emit that declares a history, and, with `distinct`, more rows than
the two parents make pairs. A summary over the second side's children is `FLX-6210`: its rows are no run.

### 2. The layout

The textbook bipartite configuration model, on exact integers (KERNEL-SPEC-2 section 8.7):

1. **Edges.** `E` is the emit's rows. Both sides are counted layouts over `E` rows, so both sum to exactly `E`.
2. **Caps** (`distinct`). A parent of one side has at most as many rows as the other side has parents; the rows above
   a cap go to the parents still under theirs, in proportion to their rows, by the largest remainders, round after
   round. The plan records how many moved.
3. **Gale-Ryser** (`distinct`). Where the condition fails, one row at a time moves from the second side's parent with
   the most rows to the one with the fewest. Every move lowers the sum of squares and an even spread meets the
   condition, so the moves end. The plan records them.
4. **Pairs.** Row `r` takes the first side's stub `r` (that side's parents' runs in row order) and the second side's
   stub `permute/2(K, E, r)`, whose parent is found by bisection, with `K` the key of
   `("sim", sim_id, "gen", exposeId, "associate", a, b, "stubs")`.
5. **Repair** (`distinct`). A row whose pair an earlier row holds is switched with a keyed partner
   `permute/2(K', E, (64 r + t) mod E)` in round `t < 64` (each round takes the rows still waiting, in row order),
   when `(a, b), (a', b')` can become `(a, b'), (a', b)` with neither new pair held, which keeps both sides'
   degrees. The rows left go through a Havel-Hakimi pass over their own stubs; a first-side parent that finds too few
   stubs it does not already pair with makes the whole table laid out again by Havel-Hakimi, which the Gale-Ryser
   condition guarantees. So the repair always ends, with no pair repeated. That layout breaks its ties by a keyed
   order of each first-side parent's own (KERNEL-SPEC-2 section 8.7), so a dense table holds many neighbour sets and
   no block pattern; the plan notes when it was taken.

With repeats allowed, steps 2, 3 and 5 are not taken, and a row's parents are a function of its index alone.

### 3. What the repair costs

The repair needs a global pass: it reads every row's pair. The process that starts a run's worker pool works it out
once and hands it to every worker, and a process without a pool works it out itself, from the same keys, so a slice's
rows are the same in any process and `--jobs` and `--shard` change no byte. The pass is not slice-independent and
holds `O(E)` integers in memory. Each round of switches draws its partners in one array call, and the membership of a
pair is one bisection into the sorted pairs: 400,000 rows at a density of 0.8 take about 5 s and 3,000,000 rows at
0.5 about 14 s on one core. A table that allows repeats has no such pass.

### 4. What the plan and verify say

`plan.json` gains, for the emit only, an `association` member: the relationships, `distinct`, the rows, the moves of
steps 2 and 3, what the repair did (`switches`, `placed`, `rebuilt`) and each side's realised total variation distance
from its shape (`null` for a side with none). `fluxsim verify` adds, for a table that holds each pair once
(`distinct`), a line `association:<a>,<b>.pairs` (the share of rows whose pair occurs once, as a bridge's line) and, for each side with a shape, `association:<a>,<b>.rows:<side>`,
whose share is `1 - d` for that distance and which is met when `d` is at most the side's `counts` tolerance (`0.10`
when it declares none). A count no class of the shape holds is a mass of its own, so it adds its whole share to `d`.
`fluxsim plan` prints a note when the caps or the Gale-Ryser step moved rows, and when the whole table was laid out
again. The children-per-parent rows of the scorecard score each side against the profile as before.

### 5. `fluxsim init`

An expose of exactly two `many_to_one` relationships over one column each, the two columns all of its columns and no
entity key, whose relationships are both laid out by counts, is written with an `associations` entry. Rows above
the pairs of its two parents prove that a pair repeats, and the entry declares `distinct: false`; at or below them it
declares `distinct: true`, which no statistic states, so a note says so. Its rows are the published `rowCount`;
without one, the inverse-variance mean of the two sides' totals of children (a `mean/1`'s total, exact to the unit, or
the sum a histogram gives with each class's parents at its middle and the variance of an even spread over the class),
rounded half up and kept within one row and the pairs of the two parents. Neither side's `counts` entry takes a
given column: a side's rows pair with the other side's stubs one by one, so a profile statistic that groups its
children by a column of its parent is listed unused, with that reason.

## Consequences

- A bundle that declares no association keeps its rows and digests: the key is new, and no existing path moves. The
  `associate` path is read by nothing else (KERNEL-SPEC-2 section 12), and `tests/vectors/kernel2-association-vectors.json`
  pins its keys and the pairs of five layouts, computed by a reference written from the specification.
- An association's first side follows its parents in row order, as any counted relationship does; the second side's
  parents are spread through the permutation.
- A dense distinct table (most of its possible pairs held) can leave the switches rows the Havel-Hakimi pass cannot
  serve; the whole-table layout then holds the pairs, deterministic and less random. The plan says so.

## What would change this decision

- A profile statistic of the joint degrees (how many rows join a parent of degree `i` to one of degree `j`), which
  would ask for a joint-degree layout.
- A table too large for one process to hold its pairs, which would need a repair that works slice by slice.
- FLUID gaining a spelling for a pair key, which would move `distinct` into the contract.


## Amendment of 2026-11-26: a parent whose row count is an assumption does not set the pairs

The rows of an association without a published `rowCount` were the inverse-variance mean of the two sides' totals. A
side's total rests on its parents, so a parent whose row count is the engine's own assumption (no row count and no
column count published) gives no total, and the mean put the pairs far from the side that does.

- **Derived rows.** Both sides count the same pairs, so the parents of a side times its mean children per parent equal the
  pairs, the degree-sum identity of a bipartite graph that `networkx.bipartite.configuration_model` asks of the two degree
  sequences and PopulationSim keeps between a household and a person table. When exactly one parent's rows are assumed,
  the pairs are the other side's parents times its mean children per parent (the mean over the parents the statistic counts
  times the parents of this side; the statistic's total of children only where it counts none), and the assumed parent takes `round(pairs / mean children per parent)` rows, as an assumption in the register
  that says what was derived.
- **The mean of the assumed side.** A noisy histogram of children per parent is read for this mean over the cells that
  stand clear of the noise (`noise_floor` over all its cells, ADR 0019), as `init` reads a noisy date or number histogram:
  a cell inside it carries no weight, and a histogram with no cell clear of it gives no mean. The `counts` entries read
  the histogram as before.
- **No association.** Both parents assumed, a side with no mean, a parent another table refers to or that refers to a
  parent of its own, rows far (ten times) from the parents a side's own statistic counts, a class of either side holding
  more children than the other side has parents, or rows that cannot hold the pairs (fewer than one parent) keep the
  behaviour before associations: no `associations` entry, and a note that says why. A table of two keys and no other
  column is then not written (`FLX-5034`), as before.
- **What does not move.** Parents with published or estimated row counts keep the mean of the two totals.
