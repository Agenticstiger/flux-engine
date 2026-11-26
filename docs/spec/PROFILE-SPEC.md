# Profile document: normative specification (format 1-5-0)

A profile is a JSON document of aggregate statistics about the data a bundle
imitates. A profiler computes it where the data lives and lets out only
aggregates (ADR 0003, as amended on 2026-11-16). The engine reads a profile,
never a record. A Simulation pins a profile by its digest (§6), and the
fidelity property kinds can read their expected statistics from one instead
of from a committed slice (§7).

Normative files beside this page:

- [`profile-1-5.schema.json`](profile-1-5.schema.json): the JSON Schema, whose
  `$id` is `https://agenticstiger.github.io/flux/engine/spec/profile/1-5-0/FluxProfile.json`.
  It validates format `1-0-0` to `1-4-0` documents too. The schemas of
  [`1-4-0`](profile-1-4.schema.json), [`1-3-0`](profile-1-3.schema.json), [`1-2-0`](profile-1-2.schema.json),
  [`1-1-0`](profile-1.schema.json) and [`1-0-0`](profile-1-0.schema.json) are kept as published.
  The schema checks the shape. The rules it cannot express are stated here,
  and the reader (`flux_stats.profile`, in the flux-stats distribution the
  engine depends on) applies both.
- [`profile-example.json`](profile-example.json): a profile of format `1-0-0` with every
  section it has, with suppressed groups, cells and metrics.
  [`profile-1-2-example.json`](profile-1-2-example.json) is the same profile in format
  `1-2-0`, with an `invariants` section.
  [`profile-1-2-private-example.json`](profile-1-2-private-example.json) is a profile released
  under differential privacy: a `privacy` block, a noisy statistic and two noisy cliques
  (sections 3a and 5a). [`profile-1-3-example.json`](profile-1-3-example.json) is a profile of
  format `1-3-0`: a reference table, a statistic grouped by a number column, one grouped by a
  linked row, one counted per entity, the days between two dates, a calendar and an exposure (sections 3, 5 and 5b).
  [`profile-1-4-example.json`](profile-1-4-example.json) is a profile of format `1-4-0` released under differential
  privacy, with a Kendall and a Spearman rank correlation of one pair of columns (section 5).
  [`profile-1-4-cliques-example.json`](profile-1-4-cliques-example.json) holds a `cliqueMarginal/1`, a
  `cliqueMarginal/2` over a category and a number column, and a `cliqueSelection` (section 5a).
  [`profile-1-4-follows-example.json`](profile-1-4-follows-example.json) is a profile whose unit of privacy follows
  a relationship (section 3a).
  [`profile-1-5-history-example.json`](profile-1-5-history-example.json) is a profile of format `1-5-0` with a noisy
  history and two noisy null counts (section 3a). The tests read all eight and validate them against the schemas.

Every statistic in a profile is an instance of a registered definition
(`flux_stats`, one definition per statistic). The statistics that the
fidelity property kinds compare use the same spelling as the derived
statistic `S` (SEAM-FIDELITY-SPEC §4), under the profile's own grid. A
profile formed over the same records under the same declarations therefore
holds the same numbers `S` would.

## 1. The document

A profile is one JSON object (RFC 8259) in UTF-8.

| Key | Required | Holds |
|---|---|---|
| `_producer` | yes | A URI naming the tool that wrote the profile. |
| `_schemaURL` | yes | The `$id` of the format the document is written in. This engine reads formats `1-0-0` to `1-5-0` (§8) and refuses any other URL. |
| `encoding` | yes | The grid that encoded quantities were formed on (§2). |
| `disclosureRules` | yes | The disclosure rules, by name (§2). |
| `tables` | yes | One entry per table, keyed by the FLUID `exposeId` it conforms to (§5). |
| `relationships` | no | Children per parent, between two tables (§5). |
| `queries` | no | Rates, one per named query (§5). |
| `invariants` | no | The share of rows on which each declared fact holds (§5). Format `1-2-0`. |
| `references` | no | The rows of a table the owner releases whole, one entry per `exposeId` (§5b). Format `1-3-0`. |
| `privacy` | no | The release-level account of a profile released under differential privacy: the measure, the total privacy loss and the unit of privacy, which in format `1-4-0` may follow child rows (§3a). Format `1-2-0`. |

`_producer` and `_schemaURL` are the OpenLineage facet envelope (`BaseFacet`).
The format version is part of the schema URL, so a later format has a new
URL rather than a new meaning.

Numbers:

- A count is a JSON integer from 0 to 2^53 - 1.
- Every other number is a decimal string: an optional minus sign, a whole
  part with no leading zeros, and an optional fraction. It has no exponent,
  no plus sign and at most 1000 digits. Examples are `"0.25"` and `"-12"`.
- A JSON float, `NaN`, `Infinity` and a key that appears twice in one object
  are refused, because none of them has one exact meaning.

A name (a table, a column, a rule, a group key, a level) is a non-empty
string. Every string in a profile, a key or a value, is in Unicode
Normalization Form C. The digest's canonical form puts each string in that
form, so a string in another form is refused rather than read: the text the
engine reads is then the text the digest covers.

The profile's **digest** is `jcs-document` (KERNEL-SPEC §2): `sha256:`
followed by the hex sha256 of the RFC 8785 canonical bytes of the parsed
document. Re-indenting the file or reordering its keys keeps the digest.
Changing any value moves it.

Any rule on this page that a document breaks is refused whole, with
`FLX-5036`. The refusal names where in the document the problem is, and
never quotes a value.

## 2. The grid and the disclosure rules

`encoding` is `{ "fractionalBits": n, "rounding": "floor" }`, with `n` from 0
to 128. It is the grid that the statistics which depend on a grid were
formed on (KERNEL-SPEC §12): `mean/1`, `correlation/1` and `quantile/1`,
whose quantities are encoded values, and `marginalHistogram/1` and
`quantile/2`, whose values are floored onto the grid before they are binned. In format `1-4-0`
the values of `kendallTau/1` and `spearmanRho/1` are floored onto the grid before they are
ranked, so a tie is a tie on the grid, and so are the values a `groupByBins` places in a bin
(§3).

`disclosureRules` maps a rule name to the rule that was applied. Every
statistic and metric names one rule. The fields are ACRO's output-checking
parameters (ACRO 1.1.0, `acro/default.yaml`). A field that is absent was not
applied, so `{}` is the rule "nothing was applied". A ratio (`dominance.k`,
`pRatio`), like a quantile level, is a decimal string in `[0, 1]` with no sign.

| Field | ACRO parameter | ACRO default |
|---|---|---|
| `minimumCell` | `safe_threshold` | `10` |
| `zerosDisclosive` | `zeros_are_disclosive` | `true` |
| `roundingBase` | `safe_round_base` | `5` |
| `dominance` (`n`, `k`) | `safe_nk_n`, `safe_nk_k` | `2`, `"0.90"` |
| `pRatio` | `safe_pratio_p` | `"0.10"` |

These are output-checking conventions only. A profile that names them makes
no privacy or anonymity claim. A statistic released with noise under §3a is the one
exception, and only for that statistic.

### Facts are suppressed as a set

A profiler that publishes several facts over one table, each a share or a count of
the rules that hold on its rows, MUST protect them as a set and not one at a time. A
fact that passes the minimum cell on its own can still disclose a count through the
others, by differencing.

1. The profiler runs one grouped query over the bitmask of the rules, for example
   `SELECT <bitmask of rules>, COUNT(*) ... GROUP BY 1`. Its rows are the atoms of the
   Venn diagram of the facts, and they serve any number of facts.
2. The profiler withholds every fact that touches a non-empty atom whose count is under
   the minimum cell (10 by default). It repeats this until nothing changes. The result is
   monotone and does not depend on the order of the facts.
3. The profiler checks the exact counts before it rounds to the published grain. Counts
   of 102 and 103 publish as 100 and 105, and their difference reveals 1.
4. Checking pairs is not enough. For facts A, B and A or B, every pairwise atom can be
   large while the count of A and B is 3.
5. An atom with a count of zero is not disclosive only when the text of the rules proves
   the containment it shows. Otherwise it is treated as under the minimum cell.
6. A withheld fact is marked with the withheld code `C` (§3, Suppressed values).
7. The rule does not cover overlap with histogram cells or with relationship counts. A
   profile makes no claim about them under this rule.

## 3. Statistics

A statistic is an object with these keys:

| Key | Holds |
|---|---|
| `definition` | A registered identifier: `mean/1`, `marginalHistogram/1`, `levelHistogram/1`, `support/1`, `correlation/1`, `quantile/1` or `quantile/2`; in a `columnPairs` entry of format `1-4-0`, `kendallTau/1` or `spearmanRho/1` (§5); and, in format `1-5-0`, `nullCount/2` of a column, noisy only (§3a). |
| `sampleSize` | `{ "min": a, "max": b }`, the band the number of records behind the statistic lies in. `max` may be absent, and is never below `min`. |
| `disclosure` | The name of the rule in `disclosureRules` that was applied. |
| `groups` | The groups, sorted by key, each key at most once (below). |
| `bins` | `marginalHistogram/1` and `quantile/2` only: at least two strictly increasing decimal strings, the bin edges. |
| `levels` | `levelHistogram/1` and `nullCount/2` only: the levels counted, distinct and sorted by code point. `nullCount/2` states `["known", "null"]`. |
| `level` | `quantile/1` and `quantile/2` only: the quantile level, a decimal string in `[0, 1]` with no sign. |
| `groupBy` | A column statistic grouped by a categorical column of the same table, or, in format `1-3-0`, by a number column of it (§5). |
| `groupByLink` | A column statistic grouped by a column of the row a relationship of its table names (§5). Format `1-3-0`. |
| `groupByBins` | A column statistic grouped by the bins of another number or timestamp column of its table (below). Format `1-4-0`. |
| `per` | A column statistic counted once per distinct value of named columns of its table (§5). Format `1-3-0`. |
| `of` | A period statistic: the number column it sums (§5). |
| `rowFloor` | A rank correlation only: the count of records its value is formed with (§5). Format `1-4-0`. |
| `noise` | A statistic released under differential privacy: its mechanism, scale and share of the budget (§3a). `levelHistogram/1` and `marginalHistogram/1`, a rank correlation and, in format `1-5-0`, `nullCount/2` and a member of a `history` (§3a); never one with `per`. Format `1-2-0`. |

A group is spelled as `S` spells a group (SEAM-FIDELITY-SPEC §4), with a
`status`:

| Definition | Quantities besides `group`, `k` and `status` |
|---|---|
| `mean/1` | `Sx`, the sum of the `k` encoded values |
| `marginalHistogram/1` | `binCounts`, one count per bin |
| `levelHistogram/1` | `levelCounts`, one count per level |
| `support/1` | `support`, the distinct members, sorted |
| `correlation/1` | `Sx`, `Sy`, `Sxx`, `Sxy`, `Syy` |
| `kendallTau/1` | `S`, the sum over pairs of records of the product of the signs of the two differences (§5) |
| `spearmanRho/1` | `A4`, four times the sum of the products of the centred mid-ranks (§5) |
| `nullCount/2` | `noisyCounts` only (§3a): the known cell, then the NULL cell |
| `quantile/1` | `quantileWitness`, `quantileBelow`, `quantileAtOrBelow` |
| `quantile/2` | `quantileBin`, `binBelow`, `binThrough` |

Sums and the quantile witness are integers on the grid, written as strings.

`quantile/2` states no witness (ADR 0012). Its order statistic, at index
`j = ceil(level * k)`, lies in the bin `[e_i, e_{i+1})` (the last bin closed
at the top, as a histogram's is) whose index is `quantileBin`. `binBelow`
counts the records below `e_i` and `binThrough` the records below
`e_{i+1}`, or all of them for the last bin, so `binBelow < j <= binThrough`.
All three are JSON integers.

A profiler publishes the statistics of a column that count records between
edges, `marginalHistogram/1`, `levelHistogram/1`, `quantile/2` and
`quantile/1`, only under ADR 0012's rule, which
`flux_stats.edge_counts.check_edge_counts` applies to each column's statistics
and to each relationship's. A level histogram's declared levels are read as
bins, one per level in their declared order. A cell is small when it holds 1
to `minimumCell - 1` records, or none when the rule holds zeros disclosive.

- Every such statistic of the column, at every level and in every group, is
  on one declared partition: the column's first histogram's bins or its first
  `quantile/2`'s, which a quantile's bins equal or coarsen, or its declared
  levels. Otherwise the statistic is withheld. A `quantile/1` counts at its
  witness, an edge no declaration fixes, so it is published only as the one
  such statistic of its column, and is withheld beside any other: a
  histogram, a `quantile/2`, another level, or the whole beside a groupBy. A
  quantile checked beside them declares bins.
- On unrounded counts, a `quantile/2` group's records below its bin, in it and
  above it each number at least `minimumCell`, or none at the ends of the
  range, which lie outside the declared range. A `quantile/1` group's records
  below its witness, holding it and above it do the same, except that none
  below or above, which says that no record lies between the witness and the
  end of the range, is a cell of no records: allowed only where zeros are not
  disclosive. A histogram's small cell is withheld.
- A group of 1 to `minimumCell - 1` records, or of none where zeros are
  disclosive, is withheld, and so is every group of every grouped statistic of
  its groupBy (histograms, quantiles, means and supports alike), since the
  column's count less the other groups' sizes gives its size.
- No small cell is one a reader can form by adding or subtracting these
  statistics' counts, for which more is withheld: within a group, the records
  between any two edges its counts tie together, and a count that ties none
  of its group's edges to the first edge is withheld; across groups, what the
  groups' sum to the whole gives; a groupBy that still leaves a group partly
  unknown is withheld whole when the column publishes another count at its
  edges.
- Only then are the counts rounded, and a group whose rounded counts no
  longer satisfy `binBelow < ceil(level * k) <= binThrough`, or no longer prove
  a `quantile/1`'s witness by its ranks, or whose positive count rounds to
  zero, is withheld.

Whatever withholds it, a group or cell of a statistic this rule decides is
withheld with `C` (below). The rule takes the sizes of the column's groups as
known, since its means and supports publish them too, which is why the size
rule reaches those. A profiler passes every such statistic of a column to the
check in one call: a statistic checked in a call of its own is judged alone.

Outside this rule, and to be weighed where zeros are disclosive (ADR 0012,
amendment of 2026-11-18):

- A `support/1`, and the groups a grouped statistic lists, say which declared
  levels hold no record.
- A published `min` or `max` (§4) says that no record lies below or above it,
  so the part of the declared range beyond it holds none. This leaves the rule
  inconsistent at the ends of the range: where zeros are disclosive, a
  `quantile/1` whose witness is the column's smallest value is withheld
  because no record lies below its witness, though a published `min` already
  shows that, and one whose witness is the largest value is withheld because
  no record lies above its witness, though a published `max` already shows
  that.
- A profiler that published a group's count of nulls would give the group's
  size in one column back from its size in another.

An ungrouped statistic has exactly one group, keyed by the empty string.
A grouped statistic keys every group by a name. A group is written in its
one canonical spelling: read back and spelled again, it is the same JSON.

### Suppressed values

A value a disclosure rule withheld carries a status from the SDMX codelist
`CL_CONF_STATUS` (version 1.4) and no value. The codes are SDMX's; the
meanings below are paraphrased, and the SDMX registry holds the official
labels.

| Code | Meaning |
|---|---|
| `F` | Free for publication |
| `A` | Primary suppression: small counts |
| `O`, `T`, `G` | Primary suppression: dominance by one, two, or one or two units |
| `M` | Primary suppression: other measures of concentration |
| `D` | Secondary suppression set by the sender, not for publication |
| `S` | Secondary suppression set and managed by the receiver, not for publication |
| `C` | Withheld statistical information |
| `N`, `E` | Not for publication, restricted to internal use (`E` until an embargo ends) |
| `P` | Withheld under non-statistical secrecy arrangements |
| `X` | Deprecated: withheld for military secrecy |

- A **group** whose status is not `F` holds only `group` and `status`.
- In a published group of `marginalHistogram/1` or `levelHistogram/1`, a
  **cell** can be withheld on its own. `cellStatus` lists one status per
  cell, and a cell's count is `null` exactly when its status is not `F`.
  Without `cellStatus`, every cell is published.
- In a **count table** (`marginalHistogram/1`, `levelHistogram/1` and
  `quantile/2`), and in a `quantile/1`, a profiler marks every group and every
  cell that an output check withholds `C`, whatever the check (ADR 0012). A code that named the
  check would tell a reader a range: `A` says that a cell holds fewer than
  `minimumCell` records, and a total a reader knows can turn that range into
  the count, as cells of 10, 9, 9 and 10 records published `F`, `A`, `A`, `F`
  give both withheld cells as 9. The profiler's own report says why each
  value is withheld. A reader reads every code, so a profile that marks a
  count table's cell, or a `quantile/1`'s group, `A` or `D` still reads.

A rule may round the counts it publishes, ACRO's base 5 among them. A
rounded total and its rounded cells need not add up, so a count table is
compared against the sum of the cells it publishes (§7).

## 3a. Released under differential privacy

Format `1-2-0` lets a profiler release count tables under differential privacy and say so. The
vocabulary is OpenDP's (measure, privacy unit, privacy loss, scale), used as words only. ADR 0003
(the amendment of 2026-11-21) says what a run built from such a release carries, and ADR 0019
how the loss is accounted for.

**The `privacy` block** is at the top level of the document, and holds exactly:

| Key | Holds |
|---|---|
| `measure` | `zeroConcentratedDP` (OpenDP's `zero_concentrated_divergence`) or `approximateDP` (`fixed_smoothed_max_divergence`). Pure DP is approximate DP with `delta` `"0"`. |
| `privacyLoss` | The total privacy loss of the release: `{ "rho": r }` under zero-concentrated DP, `{ "epsilon": e, "delta": d }` under approximate DP. Each is a decimal string; `rho` and `epsilon` are above zero and `delta` lies in `[0, 1)`. |
| `privacyUnit` | `{ "expose": id }`: the unit of privacy is one row of that expose. In format `1-4-0` it may also hold `follows`, and the unit is then one row with the child rows it takes with it (below). Neighbouring data sets differ by adding or removing one unit (OpenDP's symmetric distance at distance one). |
| `publicStructure` | Optional boolean. `true` declares that the structure of the release was chosen without the data, or is part of it. See below. |

**The `noise` block** of a statistic or a clique holds exactly `mechanism`, `scale` and
`privacyLoss`:

| Key | Holds |
|---|---|
| `mechanism` | `gaussian`, `discreteGaussian` or `laplace`. |
| `scale` | The noise scale, a decimal string above zero: the standard deviation of a Gaussian, the `b` of a Laplace. |
| `privacyLoss` | This statistic's share of the budget, in the measure of the `privacyLoss` of the block above: `rho`, or `epsilon` and `delta`. |

A noisy statistic has an L1 sensitivity `D1` and an L2 sensitivity `D2` under adding or removing one unit
of privacy. Every count table over the rows of the unit's expose has both equal to 1: a row lands in one
cell. The other statistics, and the tables a unit that follows child rows reaches, have the sensitivities
of "What a noisy statistic is charged" below. The reader checks each pairing of a mechanism and a measure,
in exact arithmetic, and refuses (`FLX-5036`) a share below what the mechanism spends, which is an
understated loss. A share above it is accepted as the profiler's slack.

| Mechanism | Measure | The share is at least |
|---|---|---|
| `gaussian` or `discreteGaussian`, scale `s` | `zeroConcentratedDP` | `rho = D2^2 / (2 s^2)` |
| `gaussian` or `discreteGaussian`, scale `s` | `approximateDP` | `epsilon = rho + 2 sqrt(rho ln(1 / delta))` with `rho = D2^2 / (2 s^2)`, and `0 < delta < 1` |
| `laplace`, scale `b` | `approximateDP` | `epsilon = D1 / b`, with any `delta` |
| `laplace`, scale `b` | `zeroConcentratedDP` | `rho = (D1 / b)^2 / 2` |

An epsilon-DP release is (epsilon^2 / 2)-zero-concentrated DP (Bun and Steinke 2016, Proposition
1.4), which gives the Laplace's rho. A rho-zero-concentrated release is (epsilon, delta)-DP for every
delta in (0, 1) with the epsilon above (Bun and Steinke 2016, Proposition 1.3; for the discrete
Gaussian, Canonne, Kamath and Steinke 2020), so a Gaussian states a delta above zero, since none
satisfies delta = 0. The reader computes that epsilon in decimal arithmetic at a fixed precision with
every inexact step rounded up, so it is never below the true value and every platform agrees, and
compares it exactly with the stated one. A Laplace under zero-concentrated DP that states a rho below
its bound is refused as well: a tighter figure cannot be checked, so the check fails closed.

The variance of one noisy cell is `scale^2` for `gaussian`, `2 * scale^2` for `laplace`, and, for
`discreteGaussian`, `scale^2`, which the discrete Gaussian's variance does not exceed. The reader
holds it exactly, as a rational.

**A noisy statistic** is a `levelHistogram/1` or `marginalHistogram/1` of a column that carries a
`noise` block, in a table whose rows one unit changes. Without `follows` that table is the expose of
`privacyUnit`: a noisy statistic of a column of another table is refused, since the profile does not state
how many rows of another table one unit contributes, so the loss there is not known. A noisy statistic in an
invariant or a period is refused in every format, and one in a query is refused before format `1-5-0`. Format `1-4-0`
allows the block in more places,
under the rules below: a rank correlation of a pair (§5), a column statistic with `groupByBins` or
`groupByLink` (§3), a relationship's `marginalHistogram/1` (§5), and a statistic of a table the unit follows
into. Its groups are
`{ "group", "status": "F", "noisyCounts": [...] }`: every group is published, `noisyCounts` has
one value per cell, each a decimal string that can be negative or fractional, and neither `k`,
`levelCounts`, `binCounts` nor `cellStatus` is written. `sampleSize` and `disclosure` are written
as for any statistic; the band a profiler states for a noisy statistic is one it derives from the
release or from public information, and the engine treats it as part of the release. A statistic
with no `noise` block is released under its disclosure rule and carries no guarantee, however
many noisy ones the profile also holds.

**Reading a noisy group.** The values `x_1 .. x_m` are point estimates of the counts, each with
the variance above. The reader makes them a valid distribution by one rule, in exact arithmetic:

1. Let `T = x_1 + ... + x_m`, the estimated number of records.
2. If `T > 0`, the weights are `max(x_i - t, 0)`, where `t >= 0` is the one threshold for which
   they add up to `T` (the Euclidean projection of `x` onto the non-negative vectors with sum
   `T`, found by sorting the values and taking the largest `j` for which
   `u_j - (u_1 + ... + u_j - T) / j > 0`, with `t = (u_1 + ... + u_j - T) / j`). A vector that is
   non-negative already has `t = 0` and is unchanged. The shares are the weights over `T`.
3. If `T <= 0` the values say nothing about the shares, which are then equal, and the estimated
   number of records is zero.
4. The group's record count `k` is `T` rounded half up (zero when `T <= 0`), and its whole counts
   are the shares times `k` by the largest remainder, the lowest position first among equals, so
   they add up to `k`. A comparison reads these as it reads a published group.

The result depends on the values alone. It is post-processing of the release.

**The budget.** The parts are the `privacyLoss` of every `noise` block of the document, in the
statistics and in the cliques and, in format `1-4-0`, the `selection` and `domain` shares of every
`cliqueSelection` (§5a). Under `zeroConcentratedDP` their `rho` adds, and under
`approximateDP` their `epsilon` adds and their `delta` adds (basic composition). Each sum is exact
and is compared with its total on its own. The reader refuses the document (`FLX-5036`) when:

- a `noise` block, a clique or a `cliqueSelection` appears and there is no `privacy` block;
- a part is not in the measure of the total (a `rho` under approximate DP, say);
- the parts add up to more than the total.

A sum below the total is accepted, and so is a `privacy` block that no statistic, clique or share
spends any of: a stated loss is an upper bound, and overstating it is the conservative side, while a
total below what the parts spend is refused. The grade comes from the statistics and never from the
block alone, so a block that nothing spends earns no grade (ADR 0020). The reader does not check that a mechanism of the stated kind
and scale made the numbers: it reads numbers, not samplers.

### A unit that follows its child rows (format `1-4-0`)

`privacyUnit` is `{ "expose": id }` or, in this format, `{ "expose": id, "follows": [...] }`. Without
`follows` the unit is one row of `expose`, as in every earlier format, and a profile's charges and bytes are
what they were. A document that holds `follows` names `1-4-0`, and one that names an earlier format and holds
it is refused with `FLX-5036`. Each entry of `follows` is
`{ "parent": { "table", "columns" }, "child": { "table", "columns" }, "maxChildren": c }`:

- it names exactly one entry of the profile's `relationships`, by its parent and child tables and columns, and
  no entry is named twice;
- `c` is a count of at least 1, a fixed public cap chosen without the data.

The listed relationships form a tree rooted at the expose. Each table they reach, other than the expose, is the
child of exactly one listed relationship; the parent of every listed relationship is the expose or a table they
reach; no table is listed as a child twice; they form no cycle; and the expose is never a child. Anything else is
refused (`FLX-5036`), naming the entry. A table reached by two paths, or a child whose parent the unit does not
reach, would make the stated reach understate the sensitivity: two followed keys of cap 1 into one table state
a reach of 2, yet one parent row can bring rows in through both, and the rows under a parent that is no part of
the unit move with that parent.

One unit of privacy is one row of `expose` together with, recursively, the rows of child tables that reference a
row of the unit through a listed relationship. Neighbouring data sets differ by one unit.

**What the profiler does.** Before it measures anything, the profiler keeps at most `c` children of each
parent row on each listed relationship. It drops the others by a rule that reads only that parent's own child
rows, such as a fixed order of their values or a seeded draw, and a dropped row takes with it the rows that
follow it. It also drops the rows of a followed table that follow no row of the unit's expose. Every statistic
of the document is measured on what remains. The engine cannot check that the profiler did so, as it cannot
check that noise was drawn, and the evidence says so.

**Reach.** `m_T` is the number of rows of table `T` that one unit contributes: `m_expose = 1`, and for a
reached table `T`, `m_T = c * m_P` for its one listed relationship (`P` to `T`, cap `c`), that is the product
of the caps along the one path from the expose to `T`. A table that no listed relationship reaches has
`m_T = 0`. The numbers are exact integers.

**What the guarantee covers.** Units. The rows of a table with `m_T = 0` are the same in every neighbouring
data set, so a statistic grouped by such a table's column, or a count of its rows' children, protects the
unit's rows and not that table's. The account names the tables the unit reaches and the tables it treats as
fixed. For the minimum grade of a column (ADR 0020), any labelled column (one that carries a sensitivity
label such as `pii` or `restricted`) of a table with `m_T = 0` counts as `P1`, whether or not the statistic
that touches it is noisy. A table the profile does not hold is not reached either. The floor applies to a
profile that names `1-4-0`; an earlier profile keeps the check it had.

### What a noisy statistic is charged (format `1-4-0`)

A noisy statistic over the rows of table `T` has the sensitivity of the table below times `m_T`.

| Statistic | L1 for one row of `T` | L2 for one row of `T` |
|---|---|---|
| a count table: `levelHistogram/1` or `marginalHistogram/1` of a column, grouped by `groupBy`, `groupByBins` or `groupByLink` or not, a calendar or date pair histogram, a clique of either definition | 1 | 1 |
| `nullCount/2` of a column (format `1-5-0`) | 1 | 1 |
| a noisy query's rate (format `1-5-0`; the charge is that of its `unit`, below) | 1 | 1 |
| a noisy member of a `history` (format `1-5-0`; its table is reached by `follows`) | 1 | 1 |
| `kendallTau/1` with `rowFloor` `f` | `4 / (f + 1)` | `4 / (f + 1)` |
| `spearmanRho/1` with `rowFloor` `f` | `9 / (f + 1)` | `9 / (f + 1)` |

Kendall's bound is reached at `n = f`. Spearman's bound is not tight: the largest change found by exhaustive
search is `6 / (f + 2)`, and the reader charges `9 / (f + 1)` until a tighter argument replaces it. Each pair
of a copula's matrix is a statistic with a share of its own, and the shares add (the budget, above).

Two forms read a row of another table and have their own rules:

- **A statistic with `groupByLink`**, of table `T` grouped by a column of parent table `P`, may carry noise only
  when `m_P = 0`, or when its relationship is listed in `follows`. It is charged `m_T` as a count table.
  Otherwise its noise is refused.
- **A relationship's `marginalHistogram/1`**, from parent table `P` to child table `C`. Let `m_C'` be `m_C` less
  the contribution of this relationship to it (`c * m_P` when it is listed, 0 when it is not).
  - `m_P >= 1` and `m_C' = 0`: L1 and L2 are `m_P`.
  - `m_P = 0` and `m_C >= 1`: L1 is `2 * m_C` and L2 is `sqrt(2) * m_C`, so the squared L2 is `2 * m_C^2`.
  - Otherwise its noise is refused.

  A `groupBy` or `groupByBins` naming a column of `P` leaves the sensitivity as it is.

The arithmetic is that of the table of mechanisms above with these sensitivities, and all four pairings of a
mechanism and a measure are checked. `discreteGaussian` applies to count tables only.

**Refused with `FLX-5036`**, naming where in the document and never a value: a noisy statistic over a table
with `m_T = 0`; a `groupByLink` or relationship statistic that the rules above refuse; noise on a `mean/1`, a
quantile, a `support/1`, a `per` statistic, a `history` member of a document that names an earlier format than `1-5-0`
or one the rules of "A noisy history" below refuse, a `nullCount/2` the rules of "A noisy null count" refuse, a query
statistic
the rules of "A noisy query" below refuse, or a
relationship's `dates` or `exposure`; and every refusal of §5's rank correlations.

**What the guarantee needs of the rest of the profile.** The numbers of a noisy statistic are
the release, but a histogram also has levels, bin edges and a `sampleSize` band, which a profiler
chooses. If it looked at the data to choose them they describe the data without noise. A profile
declares `publicStructure: true` when they were chosen without the data (the contract's enum, edges
fixed in advance, a band stated from public information) or are part of the release. The engine cannot
check the declaration, and the evidence says so.

**The declaration about structure in format `1-4-0`.** In a profile of format `1-4-0`,
`publicStructure: true` declares that these were chosen without the data, or are part of the release: the
levels and bin edges of the profile's histograms and cliques, the bins of every `groupByBins`, the group keys
of every grouped noisy statistic, the `sampleSize` bands, every `rowFloor` and every `maxChildren`. A profile
whose `cliqueSelection` states a `domain` share has released some of the levels or edges under its account,
which the declaration allows. The group keys are the levels of a `groupBy` column, the lower edges of
`groupByBins` and the values of the parent column of a `groupByLink`: a noisy statistic publishes every group
it names, and the names themselves come from the declaration, not from a count. A `sampleSize` band is a
number of records stated beside noisy values, so it too is chosen without the data or is part of the release.
A profile of an earlier format keeps the declaration it had, which covers the levels, the bin edges and the
`sampleSize` bands.

When a later format reads estimated cliques (§5a), an estimate's `variance` must be a function of the
measurement design and the noise scales only, and it joins this list: a variance computed from the data would be
a number released outside the account.

**P3.** A run is graded P3 (ADR 0020) only when the profile has the account and the declaration, and every
statistic and metric it holds that the engine can read is noisy under that account: no `rowCount`, no column
metric, no statistic without a `noise` block, no query statistic without one (format `1-5-0`), and no `periods`,
`invariants` or `references` section, none of which can be released with noise. A `columnPairs` entry is noisy when
every statistic in it
carries a `noise` block, so a `correlation/1` keeps a profile from P3 and a rank correlation need not. A
relationship is noisy when every statistic in it carries `noise` (so it holds no `mean/1` and no quantile) and
it holds no `dates` and no `exposure`. A statistic with `groupByBins` or `groupByLink` is noisy when it carries
`noise`, a clique of either definition is always noisy, `cliqueSelection` is accounting and never keeps a
profile from P3, and the unit's `follows` is part of the account: `EVIDENCE.md` states the unit with what it
follows and names the tables the unit does not reach, which the guarantee treats as fixed. A `history` keeps a profile from P3 unless every member it holds carries noise (format `1-5-0`, "A noisy history" below); a `per`
statistic and a `datePairs` or `calendar` histogram without noise keep a profile from P3, as before. A member
the grade does not know counts as released without noise.

A noisy statistic that still states data is graded P1 too, in every format: a `groupBy` on a column that
is not categorical (a number or timestamp column, or a column the profile does not hold), whose group keys
are data values, and a `sampleSize` band whose `min` equals its `max`, which is an exact size. The check
reads the statistics of columns, of `datePairs` and `columnPairs`, of `relationships` (whose `groupBy`
names a column of the parent table), of every member of a `history` (format `1-5-0`) and of the `queries` (format
`1-5-0`, whose `groupBy` names a column of the query's unit table). In a profile of an earlier format a `groupByLink` is graded P1 as well,
because its keys are values of another table and that declaration covers levels, bin edges and bands only;
in format `1-4-0` the declaration covers the values of a `category` column of the parent table, so a
`groupByLink` whose `column` is any other column (a number or timestamp column, or one the profile does not
describe), whose keys are data values, is graded P1.

### A noisy history (format `1-5-0`)

In format `1-5-0` each member of a table's `history` (§5) may carry its own `noise` block: `versionsPerEntity`,
`firstStart`, `versionLength`, `chain`, `lastVersion`, and for each tracked column its `initial`, its
`transitions`, and the `reasons` of the table. A noisy member's groups are the noisy form of this section,
`{ "group", "status": "F", "noisyCounts": [...] }`, and the reader reads them by the rule above, as it reads any
noisy group. Members may mix noisy and exact: a member with no `noise` block is released under its disclosure
rule, as before. A noisy `transitions` publishes every current-value group, one cell per next level, since the
current values are part of the declared structure. A document that holds a noisy history member names `1-5-0`,
and one that names an earlier format and holds one is refused (`FLX-5036`), as before.

**Where it is allowed.** The history's table `T` must be reached by the unit of privacy through `follows`
(`m_T >= 1`) and must not be the unit's own expose. A history of any other table is refused (`FLX-5036`), naming
the member and the table and never a value. The expose is excluded because a unit there is one row, and an
entity's versions are several rows: two versions of one entity are two units, so one unit does not hold a whole
entity and the bound below would not hold. A table the unit follows into takes its rows with the parent row, so
an entity can lie in one unit.

**What it is charged.** Each noisy member is a count table over `T`, with L1 and L2 both equal to `m_T`. One unit
holds at most `m_T` rows of `T`, so at most `m_T` entities, and at most `m_T - 1` consecutive pairs of versions.
The members that count entities (`versionsPerEntity`, `firstStart`, `lastVersion`, and `initial`) move at most
`m_T` entities by one cell each. The members that count pairs (`versionLength`, `chain`, `transitions` and
`reasons`) move at most `m_T - 1` pairs by one cell each. Removing a unit changes each cell count by an amount
that adds up to at most `m_T` over the cells, so both sensitivities are `m_T`, and the engine charges `m_T` for
the pair members as well, which is the slack of one. Each noisy member states its own `privacyLoss` share, and the
shares add with the rest of the budget (basic composition, above).

**What the profiler owes, and the engine cannot check.** The evidence states these as stated and not checked:

1. Every version of an entity references one row of the parent of `T`'s listed relationship, so an entity lies in
   one unit. The profiler refuses to release a noisy history otherwise. It holds by construction when that
   relationship's child columns are the history's `entity` columns.
2. The `maxChildren` bounding of `T` keeps or drops an entity's versions together, by a rule that reads only that
   parent's rows, so the history describes whole entities.
3. `publicStructure: true` also covers the bins of `versionsPerEntity`, `firstStart` and `versionLength`, the
   levels of `initial` and of `transitions`, and the candidate columns of `columns`. The levels of `chain` and of
   `lastVersion` are fixed by the format, and the levels of `reasons` follow from the candidate columns.

**P3.** A history whose every member carries noise under the account no longer keeps a profile from P3. A history
with any member without noise does, as before.

### A noisy null count (format `1-5-0`)

`nullCount/2` is a statistic of a column of any domain that states, noisily, how many of the table's rows have a
known value in the column and how many have NULL:

```json
{ "definition": "nullCount/2", "disclosure": "noisy", "sampleSize": { "min": 1000, "max": 9999 },
  "levels": ["known", "null"], "noise": { "mechanism": "gaussian", "scale": "2", "privacyLoss": { "rho": "0.125" } },
  "groups": [ { "group": "", "status": "F", "noisyCounts": ["4412.6", "587.9"] } ] }
```

It has two cells, in that order: the rows where the column is known, then the rows where it is NULL. It has the
noisy form only: without a `noise` block it is refused (`FLX-5036`), since an exact profile states the `count` and
`nullCount` metrics (§4). It is ungrouped, and holds none of `groupBy`, `groupByLink`, `groupByBins` and `per`.

**What it is charged.** A count table over the column's table `T`, with L1 and L2 both equal to `m_T`: a row lands
in exactly one cell. The table rules are those of any noisy column statistic: `T` is the unit's expose, or a table
the unit follows into (`m_T >= 1`); a `nullCount/2` of any other table is refused (`FLX-5036`), naming the column.

**Reading it.** The two cells are one noisy group and are read by the rule above. The point estimate gives both
the number of rows and the share of them that are NULL. The second cell costs nothing extra and is the
denominator of that share, with a variance of `2 s^2` for a Gaussian of scale `s` (the sum of two cells).

A document that holds a `nullCount/2` names `1-5-0`, and one that names an earlier format and holds one is refused
(`FLX-5036`). A `nullCount/2` carries `noise` and so does not keep a profile from P3.

### A noisy query (format `1-5-0`)

A query's statistic (§5, `queries`) is a `levelHistogram/1` over `["false", "true"]`: `k` is the number of the unit's
records and `true` the number the query matches. In format `1-5-0` it may carry a `noise` block, and its groups are the
noisy form of this section; with a `groupBy` every group is published, since the levels of the grouping are part of the
declared structure. A noisy query is valid only in a document that has a `privacy` block and names `1-5-0`; one that
names an earlier format and holds one is refused (`FLX-5036`), as before.

**The unit.** A noisy query states its `unit`, a member of the query entry that says what one record is:

- `{ "table": T }`: each row of `T` is a record. The charge is `m_T`.
- `{ "table": T, "by": [columns] }`: the records are groups of the rows of `T`. `by` is either (a) the child columns of
  an entry of the profile's `relationships` from a parent `P` into `T`, and one group is the rows of one row of `P`:
  the charge is `m_P` (1 when `P` is the unit's expose) when the unit follows the relationship, and twice that when `T` is
  not reached (see below); or (b) the `history.entity` columns of `T`, and one
  group is one entity: the charge is `m_T`. Form (b) is refused when `T` is the unit's own expose, as a noisy history
  member is there: one unit is one version, and removing it can move its entity from one cell of the rate to the other.
  Form (a) holds only when that relationship is listed in `follows` (the rows of `T` then follow their row of `P`, and a
  group leaves with its unit whole), or `T` must not be reached (`m_T = 0`, so the rows of `T` are the same in every
  neighbouring data set). When `T` is not reached, a group stays when its row of `P` leaves, and a query that reads that
  row can move the group from one cell of the rate to the other: per row of `P`, L1 is 2 and the squared L2 is 2, so the
  charge is `L1 = 2 m_P` and `L2^2 = 2 m_P^2`, as for a relationship statistic whose rows can move between cells
  (above). Otherwise one unit's rows, reached through another relationship, could move a group between the cells, and the
  query is refused. The reader takes (a) when it matches and holds and `P` is reached, and (b)
  otherwise. When (a) holds for entries of `relationships` from more than one parent the unit reaches, on the same child
  columns in any order, the query is refused: one unit's rows of every such parent leave together, and each can move a
  group from one cell of the rate to the other, so their charges would add. A query whose `unit` has `by` states no
  `groupBy` and no `groupByBins` on its statistics: a record is a group
  of rows and can hold several levels of a grouping column, so it can land in more than one cell. A `{ "table": T }` unit
  may group, since one row lands in one group. What the profiler owes, stated and not checked (the evidence says so): the
  query and its unit pass the checks of ADR 0028; whether a record matches reads only rows of that record's own unit and
  rows of tables the unit does not reach, so the query uses no aggregate, subquery or join over other units' rows, since
  removing one unit may change the cells of its own records only;
  under (b), an entity's rows lie in one unit (obligation 1 of "A noisy history") and the bounding of `T` keeps or drops
  an entity's versions together; and a profiler that cannot run a query withholds every query of that release, rather than
  releasing some.

**What it is charged.** Both cells are a count table, since one record of the unit lands in exactly one cell: L1 and L2 are
both the charge above, written as (L1, squared L2): `(m_T, m_T^2)` for a table's rows and for a history's entities, and
`(m_P, m_P^2)` for groups by a followed relationship. Groups by a relationship into a table the unit does not reach are
charged `(2 m_P, 2 m_P^2)`, since a group can change cell without leaving. Each noisy query statistic states its own `privacyLoss` share, and the shares add to the budget.

**Refused with `FLX-5036`**, naming the query and never a value: a noisy query with no `unit`; a `unit` of another form,
including a `by` that matches neither (a) nor (b), a table the profile does not hold and columns its table does not hold;
a charge of 0, which is a table the unit of privacy does not reach; a `by` for which (a) holds through relationships from
more than one parent the unit reaches, on the same child columns in any order; a `groupBy` or `groupByBins` on a
statistic of a query whose `unit` has `by`; and a noisy statistic in a document that names an
earlier format. An exact query reads as before, and its `unit`, if it has one, is checked for its shape only.

**P3.** A profile whose every query statistic carries noise under the account no longer keeps a run from P3 for its
`queries`. A query statistic without noise does, as before.

## 4. Column metrics

`tables.<exposeId>.columns.<name>.metrics` carries OpenLineage's
`columnMetrics` names, one registered definition each. Counts are integers,
and every other value is a decimal string. OpenLineage's own values are JSON
numbers.

| Metric | Definition | Value | Applies to |
|---|---|---|---|
| `count` | `count/1` | the number of non-null values | every column |
| `nullCount` | `nullCount/1` | the number of nulls | every column |
| `distinctCount` | `distinctCount/1` | the number of distinct non-null values | every column |
| `sum` | `sum/1` | the exact sum of the non-null values | number |
| `min` | `min/1` | the smallest non-null value | number, timestamp |
| `max` | `max/1` | the largest non-null value | number, timestamp |
| `quantiles` | `quantiles/1` | `values`: level to value, for each level | number, timestamp |

A metric is `{ "definition", "status", "sampleSize", "disclosure", "value" }`.
`quantiles` holds `values` instead of `value`. A metric whose status is not
`F` holds no value. Values are read as the fidelity check reads the column:
an exact decimal for a number, and integer milliseconds since the epoch for
a timestamp, so a timestamp's `min`, `max` and quantile values are written
with no fraction. When both are published, `min` is not above `max`. A
quantile is the order statistic the fidelity check takes: index
`ceil(level * n)` among the `n` non-null values. `values` names each level
once.

## 5. Sections

**`tables.<exposeId>`** holds `columns`, an optional `rowCount` (a count), and
optional `columnPairs`, `periods` and, in format `1-3-0`, `datePairs` and `calendar`. A table may also hold
`cliques` (§5a) and, in format `1-4-0`, `cliqueSelection` (§5a).

**`columns.<name>`** holds `domain` (`category`, `number` or `timestamp`) and
optionally `metrics` (§4) and `statistics`. A column's statistics are
`mean/1`, `marginalHistogram/1`, `quantile/1` and `quantile/2` over a number
or timestamp column, and `levelHistogram/1` and `support/1` over a categorical one. A
`groupBy` names a categorical column of the same table, other than the
column itself. No two statistics of one list share a definition and a shape.

Format `1-3-0` adds three optional forms of a column statistic. A document that holds any of them
names `1-3-0`, and one that names an earlier format and holds one is refused with `FLX-5036`.

- **`groupBy` on a number column.** A `groupBy` may name a `number` column of the same table,
  other than the column itself, besides a categorical one. Its group keys are the canonical
  decimal text of each value: no exponent, no leading zeros, no trailing zeros after the point,
  and `0` for zero. The records whose grouping value is NULL are one more group, never written,
  as for a categorical `groupBy`.
- **`groupByLink`.** `{ "parent": { "table", "columns" }, "columns": [...], "column": name }`
  groups a column statistic by `column` of the row that a relationship of its table names. It
  has the shape the `relationships` section uses: `parent.columns` and `columns` hold the same
  number of names, `columns` being the key columns of the statistic's own table (the child) and
  `parent.columns` those of the parent. `parent.table` is a table of the profile or an entry of
  `references` (§5b), and `column` is one of its columns. Group keys are the parent column's
  values, written as a cell is (§5b). The child records whose link reaches no row of the parent,
  or whose parent value is NULL, are one more group, never written. A statistic holds at most
  one of `groupBy` and `groupByLink` (and, in format `1-4-0`, of `groupByBins`). The disclosure rules
  apply group by group, exactly as for `groupBy`. In a profile of a format before `1-4-0` a statistic
  with `groupByLink` carries no `noise` block: removing one row of a table moves every row that links
  to it into another group, so the sensitivity of 1 that §3a charges does not hold. Format `1-4-0`
  allows the block where the profile states what one unit changes (§3a).
- **`per`.** A list of 1 to 8 distinct column names of the statistic's own table, none of them
  the statistic's own column. The statistic is counted once per distinct value of those columns,
  so a trait that is stable per entity is published per entity: `k` and every count are
  entities, and each entity counts at the value its rows most often hold, the first in code
  point order on a tie. A statistic with `per` holds no `groupBy`, no `groupByLink` and no `groupByBins`. It carries no `noise`
  block: one changed row can move an entity between two cells, and the budget of §3a charges a noisy
  statistic the sensitivity of a count table.

Format `1-4-0` adds **`groupByBins`**, and a document that holds it names `1-4-0`; one that names an earlier
format and holds it is refused with `FLX-5036`. `"groupByBins": { "column": name, "bins": [...] }` groups a
column statistic by the bins of `column`, a `number` or `timestamp` column of the same table other than the
statistic's own, with `bins` at least two strictly increasing decimal strings, read as a histogram's (a value is
floored onto the grid, bin `i` is `[e_i, e_(i+1))` and the last bin is closed above). Its groups are keyed by
the canonical decimal text of each bin's lower edge (no exponent, no leading zeros, no trailing zeros after the
point, `0` for zero). The records whose grouping value is NULL are one more group, never written. A statistic
holds at most one of `groupBy`, `groupByLink`, `groupByBins` and `per`. The disclosure rules apply group by
group, as for `groupBy`. A `levelHistogram/1` or `marginalHistogram/1` with `groupByBins` may carry `noise`,
and a noisy one publishes a group for every bin, since the bins are part of the declared structure (§3a).

**`cliques`** (format `1-2-0`) is a list of noisy clique marginals (§5a).

**`history`** (format `1-3-0`) describes the versions of the entities of a table that has a
validity window. It is optional, and a document that holds it names `1-3-0`; one that names an
earlier format and holds it is refused with `FLX-5036`. Its members:

- `entity`: the names of the columns that identify an entity, distinct, at least one. `start` and
  `end`: the names of the timestamp columns of a version's validity window. The three are
  different columns of the table.
- `versionsPerEntity`: an ungrouped `marginalHistogram/1` of the entities by their number of
  versions.
- `firstStart`: an ungrouped `marginalHistogram/1` of the entities' first starts, in whole
  milliseconds.
- `versionLength`: an ungrouped `marginalHistogram/1` of the days from a version's start to the
  next version's start, over the versions that have a next one.
- `chain`: an ungrouped `levelHistogram/1` of consecutive versions of an entity over the levels
  `["dayBefore", "gap", "nextStart", "overlap"]`. A version's end is the next version's start
  (`nextStart`), the day before it (`dayBefore`), earlier than that, leaving a gap (`gap`), or
  later than the next start (`overlap`).
- `lastVersion`: an ungrouped `levelHistogram/1` of the entities over `["closed", "open"]`, by how
  the end of their last version reads: a date, or open.
- `columns` (optional): an object keyed by the name of a candidate tracked column, a categorical
  column of the table that is not an `entity`, `start` or `end` column. Each entry holds `initial`,
  an ungrouped `levelHistogram/1` of the column's value on first versions, and `transitions`, one
  `levelHistogram/1` whose `groupBy` is the column itself. Its groups are the current values: the
  group keyed by a value's text counts the column's next value, one level per next value, over the
  changes of an entity where the column moves, so no cell counts a change from a value to itself.
  The records whose current value is NULL are one more group, never written, as for a `groupBy`.
- `reasons` (optional, and only with `columns`): an ungrouped `levelHistogram/1` of the changes by
  the one tracked column they move. Its levels are names of the entries of `columns`, and
  `several` for a change that moves more than one.

Every member is a statistic of this document: it names a disclosure rule and applies it group by
group and cell by cell. None holds a `groupByLink` or a `per`. In format `1-4-0` and earlier none holds a `noise` block either: one
version row can move an entity between cells, and the budget of §3a charges a noisy statistic a sensitivity of 1,
so such a history is released without noise, and a profile that holds one is graded as one that holds exact
statistics. In format `1-5-0` each member may carry a `noise` block, when the table is one a unit of privacy
follows into, and is then charged as §3a ("A noisy history") says; a member without one is exact. The key is read
from format `1-3-0` only.

**`columnPairs`** is a list of `{ "columns": [a, b], "statistics": [...] }`.
`a` and `b` are two different number or timestamp columns of the table, and
the statistics are `correlation/1` over the pair in that order and, in format `1-4-0`, the rank
correlations below.

**Rank correlations (format `1-4-0`).** A `columnPairs` entry may hold, beside `correlation/1`, the two
definitions below over its pair `[a, b]`. Each value is floored onto the profile's grid before it is ranked, so a
tie is a tie on the grid. A record counts when both of its values are known, and `n` is their number. A
document that holds one, or a `rowFloor`, names `1-4-0`, and one that names an earlier format and holds one is
refused with `FLX-5036`.

| Definition | Quantities besides `group`, `k` and `status` | The statistic |
|---|---|---|
| `kendallTau/1` | `S`: the sum over every pair of counted records of `sign(a_i - a_j) * sign(b_i - b_j)`, a tied pair adding 0 | `tau = S / C(N, 2)` |
| `spearmanRho/1` | `A4`: four times `sum (R_i - (n + 1) / 2) * (Q_i - (n + 1) / 2)`, with `R` and `Q` the mid-ranks of the counted records in `a` and in `b` | `rho = 3 * A4 / (N * (N^2 - 1))` |

- `k = n`, a count. `S` and `A4` are integers written as decimal strings, as a sum is. A published group whose
  `S` or `A4` is beyond what its `k` allows (`|S| <= C(k, 2)`, `3 |A4| <= k (k^2 - 1)`) is refused.
- `N = max(n, f)`, where `f` is the statistic's `rowFloor` when it has one, and `n` when it has none.
- `rowFloor` is optional: a count of at least 2, chosen without the data (§3a). The statistic is formed
  with it. It does not state that `n` is at least `f`: a smaller `n` only shrinks the value towards zero.
- Neither value is the Pearson correlation of the ranks. With ties that form has no bound on what one added row
  can change, and the charge of §3a needs one. Neither depends on the margins of the two columns, and a DATE
  column in days or in milliseconds gives the same value, since ranks do not change.
- A published group with `k` below 2 is refused. A rank correlation is ungrouped: its one group is keyed by the
  empty string.
- **Disclosure.** An exact rank correlation names a disclosure rule, and the group size rule of §3 applies to its
  one group as it does to a `correlation/1`: a group of fewer than `minimumCell` records is withheld. The rule
  never withholds the noisy form. A noisy rank correlation is released whatever the number of its records, since
  its noise is its protection and its `sampleSize` band is part of the declared structure (§3a).

**The noisy form.** A rank correlation may carry a `noise` block (§3a) when it has a `rowFloor`. Its group is
`{ "group": "", "status": "F", "noisyValue": v }`, with `v` a decimal string that may lie outside `[-1, 1]`, and
it holds no `S`, `A4` or `k`. The mechanism is `gaussian` or `laplace`. The reader's point estimate is `v`
clamped to `[-1, 1]`, which is post-processing of the release, with the variance of §3a (`scale^2`, or
`2 * scale^2` for a Laplace). Refused with `FLX-5036`: a noisy rank correlation with no `rowFloor`, one under
`discreteGaussian`, and, as before, a noisy `correlation/1`.

**`datePairs`** (format `1-3-0`) is a list of `{ "start", "end", "statistics", "order" }`, at most
1 024 of them. `start` and `end` are two different `timestamp` columns of the table, and the pair
is stated over the rows where both are known. `statistics` are of the whole days from `start` to
`end`, `DAYS_BETWEEN(start, end)`: `marginalHistogram/1`, `quantile/1` and `quantile/2`, each of
which may take a `groupBy` that names a categorical column of the table or a number column of it
(a statistic of a pair holds no `groupByLink` and no `per`). `order` is exactly one ungrouped
`levelHistogram/1` over the levels `["after", "before", "same"]` (the code point order a level
histogram counts in): the rows whose `end` is after, before and the same as their `start`, the
same on the day for a date and on the millisecond for a timestamp. A pair is named once, in the
order `start`, `end`. Each statistic and `order` follows the rules a column statistic of its form
follows: the disclosure rule is applied group by group and cell by cell, a withheld cell reads
as unknown and never as zero, and a noisy statistic states its `noise` block (§3a) in the table
of the profile's unit of privacy.

**`calendar`** (format `1-3-0`) is a list of `{ "column", "month", "dayOfWeek", "hourOfWeek" }`, at
most 1 024 of them and at most one per column. `column` is a `timestamp` column of the table (a
DATE column is a `timestamp` here too, §7). Each of `month`, `dayOfWeek` and `hourOfWeek` is
optional and at least one is present (see "An entry that holds nothing" below for the one
exception the engine makes). Each is exactly one
ungrouped `levelHistogram/1` of the
column's known values, read in UTC, over exactly these levels, written with leading zeros so that
their code point order is their number's: `month` over `01` to `12`; `dayOfWeek` over `1` to `7`,
the ISO 8601 day of the week with Monday `1`; `hourOfWeek` over `000` to `167`, the hour counted
from Monday 00:00 to 00:59 UTC, which is `000`, to Sunday 23:00 to 23:59, which is `167`. A
histogram over other levels, or missing one, is refused. The profile does not say whether a column
is a date: the hour of a date is the engine's business, and `hourOfWeek` is accepted on any
timestamp column. Each histogram follows the rules a column statistic of its form follows,
including the disclosure rule, a withheld cell reading as unknown and never as zero, and a `noise`
block (§3a).

**An entry a release cut.** A profile cut down after its release, by a step that drops each
statistic an `init --json` inventory marks unused, can leave an entry with some of its members, or
with only the keys that name it. The inventory marks a member unused only where nothing reads it,
and it keeps a withheld member's state, so one member of an entry can leave while another stays.
This holds for a `calendar` entry (`column`), a `datePairs` entry (`start`, `end`), a relationship's
`dates` entry (`parent`, `child`), an `exposure` entry (`start`, `end` and `window`), a `history`
(`entity`, `start`, `end`, and its tracked columns named by their keys) and a tracked column of a
history (its key). The format requires each of them to hold its members, so a reader that follows
the format to the letter refuses such an entry (`FLX-5036`), naming the member it misses. The
engine reads it, where the reader is told to (`allow_emptied_entries`, off by default), with the
members that are left, each checked as it would be in a whole entry: a date pair's `statistics`
and `order`, an exposure's `children` and `activeDays`, a history's five statistics and its
`reasons`, and a tracked column's `initial` and `transitions` are each read only when present. An
entry left with none of them holds nothing: it stays in its place, so the positions and pointers
of the entries after it do not move, `init` lists nothing of it, and a citation at or under it is
refused (`FLX-5036`, naming the entry). A citation of a member that is not there is refused as any
citation of a statistic the profile does not publish is (`FLX-3113`).

**`periods`** is a list of `{ "column", "period", "statistics" }`. `column` is
a timestamp column, and `period` is `P1M`, a calendar month, the one period
of format `1-0-0`. The statistics are `mean/1` with `of` naming a number
column. Their groups are keyed by month, `YYYY-MM`, so `k` is the count per
month and `Sx` the sum per month.

**`relationships`** is a list of `{ "parent", "child", "statistics" }`. `parent`
and `child` are `{ "table", "columns" }`, two tables of the profile and key
columns of each, the same number on both sides. The statistics describe the
number of child rows each parent row has: `mean/1`, `marginalHistogram/1`,
`quantile/1` and `quantile/2`.

In format `1-4-0` a statistic of a relationship may hold `groupBy`, naming a `category` column of the parent
table, or `groupByBins`, naming a `number` or `timestamp` column of it. Its groups follow `groupByBins` above
and the rules of a grouped column statistic: the parents whose value is NULL are one more group, never written.
A relationship's `marginalHistogram/1`, grouped or not, may carry `noise` under §3a; its other statistics may
not. A relationship that the unit follows (§3a) holds no bin whose least count is above the cap `maxChildren`: a
bin of the number of children whose lower edge, taken up to the next whole number, exceeds it is refused,
whatever its count says. A relationship named only so that the unit follows it may hold an empty list of
statistics, and then releases nothing.

A `relationships` entry may carry `structural` (format `1-3-0`), a non-empty list of distinct values
from `childless` and `orphan`. `childless` says the source enforces at least one child per parent,
so the class of parents with no child is empty by a constraint. `orphan` says the source enforces
referential integrity, so no child key that is not NULL misses its parent. A structural class is
empty because of a declared constraint of the source, not a count of its records, so it discloses
nothing about them, and a profiler publishes it even where zeros are disclosive. The key is
optional. A document that holds it names `1-3-0`, and one that names an earlier format and holds it
is refused with `FLX-5036`.

In format `1-3-0` a relationship may also hold **`dates`**, a list of at most 1 024 entries
`{ "parent", "child", "statistics", "order" }`. `parent` is a `timestamp` column of the
relationship's parent table and `child` one of its child table. They are stated over the joined
rows where both are known: `statistics` are of `DAYS_BETWEEN(parent, child)` in whole days
(`marginalHistogram/1`, `quantile/1`, `quantile/2`, ungrouped and exact), and `order` is one
`levelHistogram/1` over `["after", "before", "same"]`, the child's date against the parent's. A
pair is named once. The disclosure rules apply as they do to a date pair of a table.

A relationship may also hold **`exposure`** (format `1-3-0`), a list of at most 1 024 entries
`{ "start", "end", "children", "activeDays", "window" }`, at most one per `(start, end)`. `start` is
a `timestamp` column of the relationship's parent table and `end`, optional, another one, not
`start`. A profile has no as-of date, creation date or time bounds of its own, so the entry states
its `window`: `{ "start", "end" }`, integer milliseconds since the epoch in decimal text, `start`
not after `end`. A parent row whose `start` is known is active from its `start`, to its `end` when
that is known and not before its `start`, and otherwise (an `end` that is unknown, before the
`start` or past the window) to the window's `end`, cut to the window. `children` is the number of
child rows whose parent's `start` is known, and `activeDays` is the sum, over every parent row
whose `start` is known, of its active days inside the window, a whole number of days. Each is a
`count/1` metric as §4 writes one, `{ "definition", "status", "sampleSize", "disclosure", "value" }`,
so each is an exact count of 0 or more under the disclosure rule it names, and one that was withheld
holds no value and reads as unknown, never as zero.
**`queries`** is a list of `{ "name", "statistics" }` (and, in format `1-5-0`, an optional `unit`, below) with
distinct names. A
query's statistics are `levelHistogram/1` over the levels `["false", "true"]`,
so `k` is the number of records counted and `levelCounts` the records the
query does not and does match. In format `1-3-0` that statistic may carry a `groupBy`, the name of
a column of the query's rows, which gives the query's rate per group. Its groups are written and
read as those of a grouped `levelHistogram/1` of a column are: keys distinct and sorted, each
published group with its counts and one status per cell, and a withheld group or cell keeping its
status. The disclosure rules apply group by group and cell by cell. The records whose grouping
value is NULL are one more group, never written. A document that holds a grouped query statistic
names `1-3-0`, and one that names `1-2-0` or earlier and holds one is refused with `FLX-5036`.
A fact's share (`invariants`) is never grouped. In format `1-5-0` a query's statistic may carry a `noise` block, with its
groups in the noisy form, and its query then states its `unit`, the records the rate counts ("A noisy query", §3a). An
exact query reads as before, and its `unit`, which is optional, is checked for its shape only.

**`invariants`** (format `1-2-0`) is a list of `{ "name", "statistics" }` with distinct names,
one entry per declared fact. A fact's `name` is the name its Simulation gives it, which for a
rule of a contract is `<exposeId>.<ruleId>` (ADR 0018). `statistics` holds exactly one
ungrouped `levelHistogram/1` over the levels `["false", "true"]`, so `levelCounts` are the rows
that do not and do satisfy the fact. The share of rows that satisfy a fact is its `true` count
over the sum of its published counts, and when the group, or a cell of it, was withheld, or the
total is zero, the profile states no share, and a run is not scored on that fact. The disclosure
rules apply cell by cell, as to a query. A document that holds the section names format `1-2-0`.
`fluxsim init` writes the list of facts to measure in the profile request
(PROFILE-REQUEST-SPEC.md).

## 5b. Reference tables

`references` (format `1-3-0`) maps an `exposeId` to the rows of a table that the owner releases
whole. It is for non-personal reference data, such as a product catalogue (ADR 0003, amendment of
2026-11-16), and not for records about people. An entry holds exactly:

| Key | Holds |
|---|---|
| `disclosure` | The name of a rule of `disclosureRules` (§2): what was applied, `{}` when nothing was. |
| `columns` | The column names, distinct, at least one. |
| `rows` | The rows, in published order, which is significant. Each row holds exactly one cell per column. |

A cell is a string or `null` (NULL). A cell is written as EXPR-SPEC-2 section 8 writes a key's
value: a number as canonical decimal text (no exponent, no leading zeros, no trailing zeros after
the point, `0` for zero), a date as `YYYY-MM-DD`, a timestamp as its milliseconds since the epoch
as decimal text, a bool as `true` or `false`, and a string as stored. The reader refuses a cell
that is neither a string nor `null`, and a row of another width than `columns`.

The bounds are 100 000 rows, 1 000 columns and 1 000 000 cells per table, 1 024 tables, and 4 096
characters per cell. A document beyond one is refused whole, with `FLX-5036`.

## 5a. Noisy clique marginals

A clique is the joint count table of columns of one table, released under noise (§3a). A
`cliqueMarginal/1` holds two or more categorical columns. Format `1-4-0` adds `cliqueMarginal/2`, which also
holds number and timestamp columns by their bins (below). A clique is an object of
`tables.<exposeId>.cliques`:

| Key | Holds |
|---|---|
| `definition` | `cliqueMarginal/1`. |
| `columns` | Two or more distinct names of categorical columns of the table. |
| `levels` | One list of levels for each column, in the order of `columns`, each distinct and sorted by code point. |
| `noisyCounts` | One noisy value per combination of levels (decimal strings), row-major: the first column varies slowest and the last fastest. |
| `noise` | The mechanism, scale and share of the budget (§3a). |

The noise on the cells of a clique must be independent, or have a covariance at most `v` times the
identity, where `v` is the variance its `noise` block gives (SCORECARD-SPEC.md section 9 relies on
it). Independent noise meets it. So does independent noise of equal variances made consistent by
ordinary least squares, and independent noise of unequal variances made consistent by least squares
weighted by the inverse of each clique's variance: either is a projection in the metric the noise
defines, and only shrinks the covariance. Ordinary least squares with unequal scales can exceed it.
A profiler that makes the cliques consistent another way states a larger `scale`.

Cliques of one table that share columns must **agree**. For every two cliques that share at least
one column, the levels listed for each shared column are the same, and each clique summed onto the
shared columns gives exactly the same noisy value for every combination of their levels. The
comparison is exact, on decimal strings read as rationals: a profiler that makes its noisy tables
consistent (a post-processing step) writes values it computed exactly. Two cliques over the same
set of columns are refused, because one joint is released once. Cliques of both definitions in one table agree
as this paragraph says: a column they share has the same levels, or the same bin edges, in each. A document with cliques that
disagree is refused whole (`FLX-5036`), and the message names both cliques by their place and
columns.

A clique answers the level histograms over its columns. Summed onto one of its columns it is that
column's ungrouped `levelHistogram/1`, and summed onto two it is a `levelHistogram/1` of one
grouped by the other. The reader forms each as a statistic of the column, marked derived, with the
point estimate of §3a and the variance of a sum of independent cells (`cells summed * variance`),
unless the column already carries that statistic in the document. A fidelity check can therefore
compare a run with a clique without a property kind of its own, and only through these one- and
two-way marginals. A column of a `cliqueMarginal/1` is categorical. A number column is released as a noisy
`marginalHistogram/1` of its own or, in format `1-4-0`, in a `cliqueMarginal/2`.

### `cliqueMarginal/2` (format `1-4-0`)

| Key | Holds |
|---|---|
| `definition` | `cliqueMarginal/2`. |
| `columns` | One to 32 distinct names of columns of the table, each `category`, `number` or `timestamp`. |
| `domains` | One object per column, in the order of `columns`: `{ "levels": [...] }` for a `category` column, distinct and sorted by code point, or `{ "bins": [...] }` for a `number` or `timestamp` column, at least two strictly increasing decimal strings. |
| `noisyCounts` | One noisy value per combination of levels and bins, row-major: the first column varies slowest and the last fastest. |
| `noise` | The mechanism, scale and share (§3a). |

- Bins are read as `marginalHistogram/1`'s: a value is floored onto the grid, bin `i` is `[e_i, e_(i+1))`, and
  the last bin is closed above. The profiler chooses edges that cover the column's declared range. The reader
  cannot check that, since the range is the contract's, and `fluxsim init`, which reads the contract, refuses a
  clique whose edges do not cover it (`FLX-3402`). A DATE column's edges are milliseconds, converted to days as
  §7 converts a histogram's.
- The cells of a binned column are named by the canonical text of each bin's lower edge, as `groupByBins` names
  its groups (§3).
- A record counts when every column of the clique is known.
- A clique holds at most 1,000,000 cells. A document of this format holds at most 1,024 cliques in a table and
  10,000,000 cells over a table's cliques, checked before the counts are read. The work a table asks of the reader
  is limited too: the sum over its cliques of the clique's cells times the number of marginals the reader sums it
  onto (each column, each two-way table it forms, and each set of shared columns the agreement check compares),
  plus, for each set of shared columns, its cells for every clique compared on it, is at most 40,000,000. A
  table over it is refused, from its structure alone and before any count is summed. A clique of 32 columns sums
  onto 528 marginals, so one of 75,000 cells is near the limit.
- The reader forms the one-way and two-way marginals as above, now with bins: a number column's one-way marginal
  is a `marginalHistogram/1` over the clique's bins; a number and a category give a `marginalHistogram/1` of the
  number grouped by the category; two numbers give a `marginalHistogram/1` of the first in the clique's order
  grouped by the bins of the second (`groupByBins`). Each is marked derived, with the variance of the cells it
  sums: `v` times their number, with `v` the noise's variance.
- The noise is a count table's: a record lands in one cell, so the sensitivity is that of a count table (§3a).
- A document that holds a `cliqueMarginal/2` names `1-4-0`.

### `cliqueSelection` (format `1-4-0`)

A table may hold `cliqueSelection`, which says how its cliques were chosen and released:

| Key | Holds |
|---|---|
| `method` | `aim`, `mst`, `fixed` (the cliques were named before the data was read) or `other`. |
| `producer` | A URI naming the tool that chose and measured the cliques, with its version. |
| `selection` | Optional: `{ "privacyLoss": ... }`, the share spent choosing the cliques, in the measure of the total. A table whose method is `fixed` states none. |
| `domain` | Optional: `{ "privacyLoss": ... }`, the share spent releasing levels or bin edges that were not public. |
| `published` | `measured`. |

- **Measured.** Every clique of the table carries its own `noise` block and share. The two shares of
  `cliqueSelection` are parts of the account (§3a), each exactly once, with no mechanism and no scale: they are
  statements of what was spent, not noise the reader can check.
- **Estimated cliques are not read in this format.** A clique with an `estimate`, the key `measurements`, and
  `published: estimated` are refused with `FLX-5036`, which says that estimated cliques are not read in this
  format. A later format may read cliques estimated from listed measurements, such as the marginals of a model
  fitted to them. An estimate's `variance` must then be a function of the measurement design and the noise
  scales only, and it joins the declaration about structure (§3a).
- **What the reader does not check.** That the method ran as stated, and that the selection's share is what it
  spent. Each is the profiler's statement, as a noise scale is, and the evidence says so.
- The grade treats `cliqueSelection` as accounting: it holds no statistic and never keeps a profile from P3.
- A document that holds one names `1-4-0`, and the table must be one whose rows the unit changes (§3a).

[clique-selection-for-profilers.md](./clique-selection-for-profilers.md) is a note for the authors of a profiler.

## 6. Pinning a profile

A Simulation pins a profile under the top-level `profile` key of this
engine's extension namespace. The key starts at the top level of the
namespace, beside `emits`, because what it declares belongs to the
Simulation and not to one expose:

```yaml
extensions:
  com.agentics.flux/1:
    profile:
      digest: sha256:af97633046a8c9621d32fec595dd02672bc10e3fabd98c846a2ffcd713e89361
    emits:
      payment_recovery_moment:
        rows: 64
```

- The namespace carries `emits`, `parameters`, `profile`, `privacy`, `scorecard` and
  `strictCoverage` and nothing else. Any other key is refused, `FLX-3105`. `privacy` raises
  the minimum privacy grade a column's sensitivity label sets (ADR 0020); a bundle may raise a
  floor and never lower one. `scorecard`
  declares thresholds on the scores a pinned profile gives the run
  (SCORECARD-SPEC.md §4), and `parameters` says what each number the run reads
  rests on, a statistic of the pinned profile among them
  (PARAMETER-REGISTER-SPEC.md).
- `profile` is a mapping that holds exactly `digest`: `sha256:` and 64
  lowercase hex digits. Anything else is refused, `FLX-3105`, by `validate`
  as by every other command.
- A profile is not a bundle file. A bundle directory holds only the files
  ADR 0006 lists, so the profile is handed to `plan`, and to `run` and
  `apply` when they take a fresh plan:
  `fluxsim plan ./bundle --profile ./source.profile.json`. The pin is
  in the Simulation, so `inputDigest` covers it, and the run is bound to the
  profile's exact content through it. `apply --run-id`, which applies a
  recorded plan, refuses `--profile`: that plan was paired when it was taken.
- The plan refuses a pin with no profile, a profile with no pin, and a
  profile it cannot read, with `FLX-3109`. It refuses a profile whose digest
  is not the pinned one with `FLX-3110`. It refuses a document that is not a
  profile with `FLX-5036`. In each case it refuses before it writes anything.
  A bundle that pins no profile plans exactly as before.
- The plan records the pin as `profileDigest`, the grade of the run as `privacyGrade` (ADR 0020), and any scorecard thresholds
  as `scorecard`, only when the Simulation pins a profile, so `verify` and
  `ship` read them through the plan's digest. Both take `--profile` and pair
  it with the recorded pin the same way, and score the run
  (SCORECARD-SPEC.md §5).

## 7. A property's expected statistic from a profile

Each existing property kind can read its expected statistic from a profile,
as an alternative to a committed slice (SEAM-FIDELITY-SPEC §4). The slice
path is unchanged. The synthetic side is formed as the slice path forms it,
and every group is compared by the same registered definition, in the
integer forms of SEAM-FIDELITY-SPEC §5. A profile formed over a committed
slice under the block's declarations therefore gives the outcomes that the
slice path gives.

The profile answers a property when all of these hold. Otherwise the
property is refused with `FLX-5037`, and the message says which part is
missing:

- the profile has a table for the block's `exposeId`, and an entry for each
  column the property reads, its `groupBy` included;
- each such column has the domain the declaration reads it in: a `support`
  column is `category`, and a `range` column is `number` or `timestamp`;
- the table holds the kind's definition (`quantile/2` for a quantile
  property that declares bins) over the same column (or the same
  pair, in order, for a correlation, and in either order for a `rankCorrelation`), with the same `groupBy`
  or the same `groupByBins`, the same bin
  edges, the column's declared levels (sorted) for a level histogram, and the
  same quantile level;
- for `mean/1`, `marginalHistogram/1`, `correlation/1`, `quantile/1` and
  `quantile/2`, the profile's `fractionalBits` equals the block's. A sum, a moment and an order
  statistic are encoded values. A bin count depends on the grid too: a value
  is floored onto the grid before it is binned, so on a grid that misses an
  edge, a value within one grid step below that edge falls in the bin above
  it. Counts per level and member lists do not depend on the grid.

**A DATE column.** A profile has no date domain: it reads a column whose values
are calendar dates as a `timestamp`, in integer milliseconds, and nothing in
the format changes for it. The declaration reads the column in days
(SEAM-FIDELITY-SPEC §2), so the check converts the profile's numbers to days,
exactly, before it compares anything:

- a sum (`mean/1`'s `Sx`) and an order statistic (`quantile/1`'s
  `quantileWitness`) are divided by 86,400,000, and so are a `marginalHistogram/1`
  or `quantile/2` statistic's bin edges before they are matched with the
  declared ones. Counts, ranks and bin indexes are not values and are read as
  they are;
- a published `min` or `max` is compared with the declared range as the day
  it falls in, exactly, without rounding;
- a sum or an order statistic that is not a whole number of days is refused
  (`FLX-5037`), and is never floored: it can only come from a column that
  holds more than dates, and a floored number would be a different
  statistic from the one the profile publishes. A profile of a date column
  holds whole days, so its statistics always divide;
- a `correlation/1` over a DATE column is refused (`FLX-5037`), and a rank correlation needs no conversion,
  since ranks do not change when milliseconds become days;
- a `groupByBins` of a DATE column is spelled in days by the declaration and in milliseconds by the profile,
  and the check converts the profile's edges and keys before it matches them;
- a profile that reads the column as a `number` holds days, and is read as such.

The fail-closed conditions of SEAM-FIDELITY-SPEC §5 hold on the profile's
side as they do on a committed state's:

- a `rowCount` outside `records` is refused;
- a column whose published `min` or `max` lies outside its declared range is
  refused;
- a published group below `records.min` is refused, and so is an ungrouped
  statistic with no records;
- two sides that group into different sets are refused, and a suppressed
  group counts as a group.

What a profile withheld is unknown, not zero, so it is never scored as zero:

- A suppressed **group** is `censored`: it is neither within nor outside, and
  not a refusal. A property whose groups are all censored is `censored` too.
- A suppressed **cell** of a count table is censored: the table is compared
  over its published cells only, on both sides. Each side counts the records
  it puts in those cells, and the total-variation comparison runs over them
  with that sum as the side's count, so both sides are renormalised to the
  published cells. The result compares the two distributions given that a
  record falls in a published cell. It is no bound on the distance over all
  the cells in either direction, and how many records a side puts in the
  withheld cells is not scored.
- The profile's count for that comparison is the sum of its published cells,
  never the group's `k`: a rule may round each cell and the total on their
  own (ACRO's base 5), so `k` less the published cells does not count the
  withheld ones.
- A group whose every cell was withheld is `censored`. A side with no record
  in the published cells has no distribution over them, so the property is
  refused with `FLX-5005`, naming that side.
- An evaluation is within when every scored property is within and at least
  one property was scored.

The fidelity record (SEAM-FIDELITY-SPEC §6) records committed-state checks
only. The profile path computes its outcomes but does not write a record.
`verify` and `ship` score them instead: every scored group gets a
similarity to its reference, and the run gets a scorecard per property,
table, dimension and overall, which `ship` packs as `run/scorecard.json`
(SCORECARD-SPEC.md).

## 8. Compatibility and later additions

The format grows by minor versions, and a minor version only adds: an
optional key, a section, a registered definition, a period, a value a list
may hold. It never removes a key, never makes an optional key required, and
never changes what a key of format `1-0-0` means. So every `1-0-0` document
stays valid, a later reader reads it exactly as this one does, and its
digest does not move. A document that uses an addition names its minor
version in `_schemaURL` (`.../profile/1-3-0/FluxProfile.json`), so a reader
that predates the addition refuses the document with `FLX-5036` rather than
skip what it cannot read. A reader accepts each minor version up to the
newest it knows. Format `1-3-0` is a new minor format because `1-2-0` is published: it adds the
`references` section (§5b), a `groupBy` on a number column, `groupByLink` and `per` (§5), the
`structural` key of a relationship (§5), a table's `datePairs` and `calendar`, a relationship's `dates` and `exposure` (§5), a `groupBy` on a query's statistic (§5), and the `history` of a
table (§5), all optional, and changes nothing `1-2-0` says. A change
to what an existing key means is a new major format, `2-0-0`, with a new URL. Statistics follow the same rule one level
down: forming or comparing a statistic another way is a new definition
(`mean/2`), never a new meaning of `mean/1`.

Format `1-1-0` adds one registered definition, `quantile/2` (§3,
ADR 0012). A document that holds a `quantile/2` statistic names `1-1-0`,
and one that names `1-0-0` and holds one is refused (`FLX-5036`).

Format `1-2-0` adds one optional section, `invariants` (§5). A document that holds it names
`1-2-0`, and one that names `1-0-0` or `1-1-0` and holds it is refused (`FLX-5036`). A document
of an earlier format reads as it did and keeps its digest: an absent key adds no bytes to the
RFC 8785 form the digest is taken over.

Format `1-2-0` also adds, all optional and all additive, the `privacy` block, a `noise` block on a
count statistic with its `noisyCounts` groups, and `cliques` (§3a, §5a). A document that holds any
of them names `1-2-0`, and one that names `1-0-0` or `1-1-0` and holds one is refused
(`FLX-5036`). A document that holds none of them reads as it did and keeps its digest.

Format `1-4-0` adds, all optional and all additive, the rank correlations `kendallTau/1` and `spearmanRho/1`
of a `columnPairs` entry with the `rowFloor` key (§5), `cliqueMarginal/2` and a table's `cliqueSelection` (§5a),
`groupByBins` on a column statistic (§3), `groupBy` and `groupByBins` on a relationship's statistic (§5),
`noise` on a relationship's statistic, on a statistic with `groupByLink` and on a rank correlation (§3a), and
`follows` in the `privacyUnit` (§3a). A document that holds any of them names `1-4-0`, and one that names an
earlier format and holds one is refused (`FLX-5036`). A document that holds none of them reads as it did and
keeps its digest. The schema `profile-1-4.schema.json` validates documents of formats `1-0-0` to `1-4-0`, and
the earlier schemas are kept as published.

Format `1-5-0` adds, all optional and all additive, the `nullCount/2` statistic of a column, a `noise` block on
each member of a table's `history`, and a `noise` block on a query's statistic with the `unit` of its query (§3a). A
document that holds any of them names `1-5-0`, and one that names an earlier
format and holds one is refused (`FLX-5036`). A document that holds neither reads as it did and keeps its digest.
The schema `profile-1-5.schema.json` validates documents of formats `1-0-0` to `1-5-0`, and the earlier schemas are
kept as published.

Every addition is an aggregate that a profiler computes where the data
lives. The engine still reads no record, and `flux_stats` still imports
nothing but the standard library. The additions already planned fit
that rule as follows. Format `1-0-0` implements none of them.

- **Uncertainty inputs (tolerances from uncertainty).** Every statistic and metric already
  carries the band its sample size lies in (`sampleSize`; an exact size is
  a band whose `min` equals its `max`) and names the disclosure rule applied
  to it (`disclosure`). A noisy statistic states its noise in a `noise` block of its own
  (§3a), and a disclosure rule says nothing about noise, so a rule of format `1-0-0` keeps its
  meaning: no noise was added.
- **Statistics by period, and series (checks across periods).** `periods` already keys a
  statistic's groups by period, and those groups, sorted by key, are the
  series in time order. Another period length is a new value of `period`, an
  ISO 8601 duration beside `P1M`. Other registered definitions may join
  `mean/1` there, and a period statistic may take a `groupBy`, its groups
  then keyed by period and group together.
- **Query-result cells (query-result profiles).** `queries` carries rates, as
  `levelHistogram/1` over `["false", "true"]`, per group in format `1-3-0`. Other
  results arrive as other registered definitions in a query's `statistics`, such as `quantile/1`
  over a numeric result. Each cell keeps its own status, so a withheld cell is censored as
  §7 censors one today.
## 9. Prior art

- **OpenDP** (docs.opendp.org, `opendp.measures`): the words measure, privacy unit, privacy loss
  and scale, and the measures `zero_concentrated_divergence` and
  `fixed_smoothed_max_divergence`. A vocabulary only; nothing of it is used.
- **Private-PGM** (github.com/ryan112358/private-pgm, Apache-2.0): marginals of cliques of
  columns, released noisily and made consistent, as the pattern behind §5a. No code is used.
- **DPCopula** (H. Li, L. Xiong and X. Jiang, "Differentially Private Synthesization of Multi-Dimensional Data
  using Copula Functions", EDBT 2014): the release of Kendall's tau of each pair with noise, and its
  sensitivity of `4 / (n + 1)`, which `rowFloor` makes independent of the number of records (§5). No code is
  used.
- **OpenDP** 0.16.0 (MIT): `make_contingency_table`, the recommended route to measured cliques with bin edges
  fixed without the data (§5a).
- **OpenDP** identifier truncation (docs.opendp.org, tabular data): `truncate_per_group(k)` keeps at most `k` records
  per identifier, and the noise grows with the bound; the cap `maxChildren` and the charge `m_T` of a noisy history
  follow it (§3a). No code is used.
- **Tumult Analytics** ("Understanding sensitivity", docs.tmlt.dev): a count's sensitivity is the bound on rows per
  identifier. The charge `m_T` of a count table is that rule. No code is used.
- **SmartNoise SQL** (docs.smartnoise.org, metadata): `max_ids`, how many rows one user can appear in. No code is used.
- **OpenLineage** (spec 2-0-2, facet `DataQualityMetricsInputDatasetFacet`
  1-0-0): the `_producer` and `_schemaURL` envelope, the version in the
  schema's `$id`, and the `columnMetrics` names. OpenLineage writes the
  values as JSON numbers.
- **mostlyai-qa** (1.10.11): its statistics layout of bin edges plus integer
  counts. This format leaves out its per-record PCA arrays.
- **SDMX** `CL_CONF_STATUS` 1.4: the status of a withheld value.
- **ACRO** 1.1.0: the disclosure-rule parameters and their defaults.
- **Deequ** 2.1.0: one analyzer per statistic, feeding both its profiles and
  its checks. Here this is `flux_stats`.
