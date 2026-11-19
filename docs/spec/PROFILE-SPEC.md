# Profile document: normative specification (format 1-1-0)

A profile is a JSON document of aggregate statistics about the data a bundle
imitates. A profiler computes it where the data lives and lets out only
aggregates (ADR 0003, as amended on 2026-11-16). The engine reads a profile,
never a record. A Simulation pins a profile by its digest (§6), and the
fidelity property kinds can read their expected statistics from one instead
of from a committed slice (§7).

Normative files beside this page:

- [`profile-1.schema.json`](profile-1.schema.json): the JSON Schema, whose
  `$id` is `https://agenticstiger.github.io/flux/engine/spec/profile/1-1-0/FluxProfile.json`.
  It validates format `1-0-0` documents too.
  The schema checks the shape. The rules it cannot express are stated here,
  and the reader (`flux_stats.profile`, in the flux-stats distribution the
  engine depends on) applies both.
- [`profile-example.json`](profile-example.json): a profile with every
  section, with suppressed groups, cells and metrics. The tests read it and
  validate it against the schema.

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
| `_schemaURL` | yes | The `$id` of the format the document is written in. This engine reads formats `1-0-0` and `1-1-0` (§8) and refuses any other URL. |
| `encoding` | yes | The grid that encoded quantities were formed on (§2). |
| `disclosureRules` | yes | The disclosure rules, by name (§2). |
| `tables` | yes | One entry per table, keyed by the FLUID `exposeId` it conforms to (§5). |
| `relationships` | no | Children per parent, between two tables (§5). |
| `queries` | no | Rates, one per named query (§5). |

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
`quantile/2`, whose values are floored onto the grid before they are binned.

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
no privacy or anonymity claim.

## 3. Statistics

A statistic is an object with these keys:

| Key | Holds |
|---|---|
| `definition` | A registered identifier: `mean/1`, `marginalHistogram/1`, `levelHistogram/1`, `support/1`, `correlation/1`, `quantile/1` or `quantile/2`. |
| `sampleSize` | `{ "min": a, "max": b }`, the band the number of records behind the statistic lies in. `max` may be absent, and is never below `min`. |
| `disclosure` | The name of the rule in `disclosureRules` that was applied. |
| `groups` | The groups, sorted by key, each key at most once (below). |
| `bins` | `marginalHistogram/1` and `quantile/2` only: at least two strictly increasing decimal strings, the bin edges. |
| `levels` | `levelHistogram/1` only: the levels counted, distinct and sorted by code point. |
| `level` | `quantile/1` and `quantile/2` only: the quantile level, a decimal string in `[0, 1]` with no sign. |
| `groupBy` | A column statistic grouped by a categorical column of the same table (§5). |
| `of` | A period statistic: the number column it sums (§5). |

A group is spelled as `S` spells a group (SEAM-FIDELITY-SPEC §4), with a
`status`:

| Definition | Quantities besides `group`, `k` and `status` |
|---|---|
| `mean/1` | `Sx`, the sum of the `k` encoded values |
| `marginalHistogram/1` | `binCounts`, one count per bin |
| `levelHistogram/1` | `levelCounts`, one count per level |
| `support/1` | `support`, the distinct members, sorted |
| `correlation/1` | `Sx`, `Sy`, `Sxx`, `Sxy`, `Syy` |
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
optional `columnPairs` and `periods`.

**`columns.<name>`** holds `domain` (`category`, `number` or `timestamp`) and
optionally `metrics` (§4) and `statistics`. A column's statistics are
`mean/1`, `marginalHistogram/1`, `quantile/1` and `quantile/2` over a number
or timestamp column, and `levelHistogram/1` and `support/1` over a categorical one. A
`groupBy` names a categorical column of the same table, other than the
column itself. No two statistics of one list share a definition and a shape.

**`columnPairs`** is a list of `{ "columns": [a, b], "statistics": [...] }`.
`a` and `b` are two different number or timestamp columns of the table, and
the statistics are `correlation/1` over the pair in that order.

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

**`queries`** is a list of `{ "name", "statistics" }` with distinct names. A
query's statistics are `levelHistogram/1` over the levels `["false", "true"]`,
so `k` is the number of records counted and `levelCounts` the records the
query does not and does match.

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

- The namespace carries `emits`, `parameters`, `profile`, `scorecard` and
  `strictCoverage` and nothing else. Any other key is refused, `FLX-3105`. `scorecard`
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
- The plan records the pin as `profileDigest`, and any scorecard thresholds
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
  pair, in order, for a correlation), with the same `groupBy`, the same bin
  edges, the column's declared levels (sorted) for a level histogram, and the
  same quantile level;
- for `mean/1`, `marginalHistogram/1`, `correlation/1`, `quantile/1` and
  `quantile/2`, the profile's `fractionalBits` equals the block's. A sum, a moment and an order
  statistic are encoded values. A bin count depends on the grid too: a value
  is floored onto the grid before it is binned, so on a grid that misses an
  edge, a value within one grid step below that edge falls in the bin above
  it. Counts per level and member lists do not depend on the grid.

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
version in `_schemaURL` (`.../profile/1-1-0/FluxProfile.json`), so a reader
that predates the addition refuses the document with `FLX-5036` rather than
skip what it cannot read. A reader accepts each minor version up to the
newest it knows. A change to what an existing key means is a new major
format, `2-0-0`, with a new URL. Statistics follow the same rule one level
down: forming or comparing a statistic another way is a new definition
(`mean/2`), never a new meaning of `mean/1`.

Format `1-1-0` adds one registered definition, `quantile/2` (§3,
ADR 0012). A document that holds a `quantile/2` statistic names `1-1-0`,
and one that names `1-0-0` and holds one is refused (`FLX-5036`).

Every addition is an aggregate that a profiler computes where the data
lives. The engine still reads no record, and `flux_stats` still imports
nothing but the standard library. The additions already planned fit
that rule as follows. Format `1-0-0` implements none of them.

- **Uncertainty inputs (tolerances from uncertainty).** Every statistic and metric already
  carries the band its sample size lies in (`sampleSize`; an exact size is
  a band whose `min` equals its `max`) and names the disclosure rule applied
  to it (`disclosure`). Noise parameters arrive as optional fields of a
  disclosure rule. A field that is absent was not applied (§2), so a rule of
  format `1-0-0` keeps its meaning: no noise was added.
- **Statistics by period, and series (checks across periods).** `periods` already keys a
  statistic's groups by period, and those groups, sorted by key, are the
  series in time order. Another period length is a new value of `period`, an
  ISO 8601 duration beside `P1M`. Other registered definitions may join
  `mean/1` there, and a period statistic may take a `groupBy`, its groups
  then keyed by period and group together.
- **Query-result cells (query-result profiles).** `queries` carries rates, as
  `levelHistogram/1` over `["false", "true"]`. Shares per group arrive as a
  `groupBy` on a query statistic, and other results as other registered
  definitions in a query's `statistics`, such as `quantile/1` over a numeric
  result. Each cell keeps its own status, so a withheld cell is censored as
  §7 censors one today.
- **Invariant satisfaction (declared invariants).** A new optional section, `invariants`,
  is a list of `{ "name", "statistics" }` shaped like `queries`: one
  `levelHistogram/1` over `["false", "true"]` per declared invariant. The
  share of rows that satisfy an invariant is its `true` count over the sum
  of its published counts. No new definition is needed, and the disclosure
  rules apply cell by cell.

## 9. Prior art

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
