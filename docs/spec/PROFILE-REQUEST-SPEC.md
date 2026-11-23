# Profile request: normative specification (formats 1-0-0, 1-1-0 and 1-2-0)

A profile request lists what a profiler must measure for a bundle. `fluxsim init`, given a
FLUID contract and no profile, writes one (ADR 0018). A profiler reads it, measures exactly
the statistics, column metrics and fact shares it lists where the data lives, and writes a
profile (PROFILE-SPEC.md). `fluxsim init` then runs a second time, with `--contract` and
`--profile`, and writes the bundle. That second run also writes a request of its own, beside the
bundle and named `<name>.assumptions.profile-request.yml`, for exactly the statistics that would
replace the numbers it had to assume (PARAMETER-REGISTER-SPEC.md section 1.3): the same format,
with a table's `rowCount` and the metrics and statistics of the columns it names, and no
relationship and no fact.

A request holds names and registered identifiers and nothing else. It carries no record, no
value of the data and no answer: it is the list of questions. It stays outside the bundle, as
a profile does, so it is not an input of `inputDigest` (ADR 0006).

The reader is `flux_stats.request` in the flux-stats distribution, which a profiler imports
without the engine. Its refusal is `FLX-5038`.

## 1. The document

A request is one YAML document in UTF-8 text. It is read exactly, as a bundle's documents are
(`flux_stats.exact_yaml`): no float and no timestamp is built, an integer is whole, and the
read is bounded. A request holds at most 200 000 values once its aliases are written out,
nests at most 12 levels, writes out at most 8 MiB of characters, and uses no merge key. A
text that holds more than one document, or none, is refused. The reader is given the text of
the document and opens no file: reading a file, and holding it to a size, is the caller's.

| Key | Required | Holds |
|---|---|---|
| `_schemaURL` | yes | `https://agenticstiger.github.io/flux/engine/spec/profile-request/1-0-0/FluxProfileRequest.json`, or, for a request that asks for date pairs, a calendar or an exposure (sections 2 and 3) or uses an addition of section 6, `https://agenticstiger.github.io/flux/engine/spec/profile-request/1-1-0/FluxProfileRequest.json`, or, for a request that uses an addition of section 7, `https://agenticstiger.github.io/flux/engine/spec/profile-request/1-2-0/FluxProfileRequest.json`. Any other string is refused. It names the format and its version, as a profile's names its own; the reader compares the string, and no JSON Schema document is published for the request format. |
| `contract` | no | `{ digest }`: the `jcs-document` digest of the contract the request was made from, so a profiler and `init` can tell that a request and a contract belong together. |
| `encoding` | no | `{ fractionalBits, rounding }`: the grid the profile's encoded quantities are formed on (PROFILE-SPEC.md section 2). `rounding` is `floor`. Absent means 16 fractional bits. |
| `tables` | yes | One entry per table, keyed by the FLUID `exposeId` (section 2). |
| `relationships` | no | The children-per-parent statistics asked of each declared relationship (section 3). |
| `facts` | no | The fact shares asked of the rules of the contract (section 4). |
| `queries` | no | Format 1-1-0: the consumers' queries a profiler answers (section 6). |

A name (a table, a column, a rule, a fact) is a non-empty string of at most 512 characters, in
Unicode Normalization Form C, with no control, format or separator character. The one
separator a name may hold is U+0020, a plain space; any other, a no-break space and the line
and paragraph separators among them, is refused. `fluxsim init` applies these rules before it
writes a request (`flux_stats.request.check_request`) and refuses, as `FLX-5038`, a contract
whose table, column or fact name the reader would refuse, naming it. Every mapping
carries exactly the keys this page names; an unknown key is refused. A request holds at most
1 024 tables, 4 096 columns per table, 4 096 relationships, 4 096 facts, 32 statistics or
metrics per entry, 1 024 date pairs per table and per relationship, 1 024 calendar columns per table and 1 024
exposures per relationship.

## 2. Tables and columns

`tables.<exposeId>` holds `columns` and, optionally, `rowCount: true`, which asks the profile
for the table's row count and, in format 1-1-0, `workload` (section 6) and `datePairs`: a list of `{ start, end }`, two
different `timestamp` columns of the table, for each of which the profile states the days from
`start` to `end` and their order (PROFILE-SPEC.md section 5, `datePairs`). A pair is named once.
Also in format 1-1-0, `calendar` is a list of column names, each a `timestamp` column of the table
and named once: the profile counts its known values by month, day of week and hour of week
(PROFILE-SPEC.md section 5, `calendar`).

`columns.<name>` holds `domain` (`category`, `number` or `timestamp`, as in PROFILE-SPEC.md
section 5) and, optionally:

- `metrics`: names of registered column metrics (`count`, `nullCount`, `distinctCount`, `min`,
  `max`, `sum`, `quantiles`), each of which must apply to the column's domain (PROFILE-SPEC.md
  section 4);
- `statistics`: identifiers of registered statistics that read the domain: `mean/1`,
  `marginalHistogram/1`, `quantile/1` and `quantile/2` for a `number` or `timestamp` column,
  `levelHistogram/1` and `support/1` for a `category` column.

A list names each entry once. A writer lists a table's columns, and the tables, in name order,
which is the order the reader gives them back in, so a request that is read and written again
is the same text; `fluxsim init` does.

## 3. Relationships

`relationships` is a list of `{ parent, child, statistics }`. `parent` and `child` are
`{ table, columns }`: a table of this request and key columns of it, the same number on both
sides. `statistics` are registered statistics over the number of child rows each parent row
has: `mean/1`, `marginalHistogram/1`, `quantile/1` and `quantile/2`. In format 1-1-0 a relationship
may also hold `dates`, a list of `{ parent, child }`: a `timestamp` column of the parent's table
and one of the child's, for each of which the profile states the days from the parent's date to
the child's and their order (PROFILE-SPEC.md section 5, `dates`). A pair is named once. Also in
format 1-1-0 it may hold `exposure`, a list of `{ start, end }`: `start` a `timestamp` column of the
parent's table and `end`, optional, another one. For each, the profile states how many child rows
have a parent whose start is known and the days those parents are active inside a window the
profiler states (PROFILE-SPEC.md section 5, `exposure`). A `(start, end)` pair is named once.

## 4. Facts

`facts` is a list of `{ name, table, rule }`. `rule` is the id of a rule that holds on rows of
`table`. A profiler measures, per fact, the number of rows that satisfy the rule and the
number that do not, and writes them as the profile's `invariants` entry of the same `name`
(PROFILE-SPEC.md section 5, format 1-2-0). A name is used once.

## 5. Compatibility

The format grows as the profile does (PROFILE-SPEC.md section 8): a minor version only adds,
and a document that uses an addition names its minor version in `_schemaURL`. Format 1-1-0 adds
`datePairs` and `calendar` to a table and `dates` and `exposure` to a relationship, and the `queries`
and `workload` additions of section 6, all optional, and changes nothing 1-0-0 says. A request that
asks for none of them is written as 1-0-0, byte for byte as before, so a profiler that reads only
1-0-0 reads every request that asks for none. A document that names 1-0-0 and holds an addition of
1-1-0 is refused with `FLX-5038`, naming the addition, since a reader of 1-0-0 would otherwise
ignore what it does not know. A request that names 1-0-0 or 1-1-0 and holds an addition of 1-2-0 is refused with `FLX-5038`, naming the addition and the format to name. A request that asks for none of the additions of 1-2-0 keeps the format it named before and its bytes. This reader reads formats 1-0-0, 1-1-0 and 1-2-0 and refuses any other URL.

## 6. Format 1-1-0: consumer queries and a column workload

Format 1-1-0 is the one new version of the request in its release. It also carries the date
additions (`datePairs` and `calendar`) that other changes of the release define, and this page
describes them where they land. The additions below belong to the consumers' queries (ADR 0028
section 6): `fluxsim init --consumers FILE`, given a contract and no profile, writes them.

**`queries`** is a list of at most 1 024 entries `{ name, sql, unit, groupBy }`, one per
calibration query that has a unit:

- `name` is a name (section 1), and each is used once.
- `sql` is the query's canonical text over the `exposeId`s of the request, in the DuckDB
  dialect, with its comments dropped. A text is at most 65 536 UTF-8 bytes, in Unicode
  Normalization Form C, with no control character but newline and tab.
- `unit` is `{ table }`, a table of this request, or `{ sql }`, the canonical text of a view
  (written under the same rules as `sql`). Exactly one of the two is given.
- `groupBy` is optional: a name, a column of the unit.

A query's text is the consumers' question: its literals come from the consumers, never from the
data. A query that is held out is never in this list (see below).

**`tables.<exposeId>.workload`** is a list of `{ columns, weight }`, the sets of columns that the
calibration queries read together in their predicates and groupings:

- `columns` names 1 to 8 distinct columns of that table, each listed in the request with the
  domain `category`, in sorted order;
- `weight` is a whole number from 1 to 1 000 000: the number of calibration queries that read
  exactly that set of the table's categorical columns;
- each set is listed once, and the list is sorted by `columns`; at most 4 096 sets per table.

The workload holds no value. A private release may weigh the marginals it selects by it; how a
profiler uses it is the profiler's.

**The held-out request.** When the consumers hold queries back to check a fit against, `init`
also writes `<directory>.held-out-request.yml` beside the request: a request of the same format
that lists only those queries, with the table of each unit under `tables` and no column
(`columns: {}`). A reader accepts an empty `columns`. Its answer is a separate profile document,
which `verify` and `calibrate` read, and which no bundle pins. `init` never overwrites either file.

**What a profiler owes.** For each query of `queries`, and for each query of the held-out
request, the profiler answers with a `levelHistogram/1` over `["false", "true"]` in the
profile's `queries` section under the same `name`, grouped per `groupBy` when the entry has one
(profile format 1-3-0): `k` is the number of records of the unit, and the `true` count is the
number of rows the query returns. Each entry is untrusted text. The profiler checks it again
under ADR 0028 section 2, and runs it under the locks of section 3 or stronger ones. Its
disclosure checks look at the queries as a set and not one at a time, because two queries that
differ by one literal can reveal one record by differencing.

## 7. Format 1-2-0: rank correlations, clique columns and grouped relationships

Format 1-2-0 holds everything 1-1-0 does and adds four optional keys, which ask for the statistics of profile
format 1-4-0 (PROFILE-SPEC.md). A request that uses any of them names
`https://agenticstiger.github.io/flux/engine/spec/profile-request/1-2-0/FluxProfileRequest.json`. The request
still holds names and registered identifiers only, and no budget: how much noise to add, and where, is the data
owner's choice, stated in the profile.

- `tables.<exposeId>.rankCorrelations`: a list of `{ "definition", "columns" }`. `definition` is `kendallTau/1`
  or `spearmanRho/1`, each at most once per table. `columns` names 2 to 64 distinct columns of the table, each
  listed in the request with the domain `number` or `timestamp`. The profile states the statistic over every
  pair of them (PROFILE-SPEC.md section 5).
- `tables.<exposeId>.cliqueColumns`: a list of the columns of the table that a profiler may join into cliques,
  `category`, `number` or `timestamp`, each named once, at most 1 024. The profiler chooses the cliques, their
  method and the bins of a number column, and states them (PROFILE-SPEC.md section 5a).
- `relationships[].groupBy`: a list of columns of the relationship's parent table, each named once, at most 64.
  The profile states the relationship's statistics grouped by each (PROFILE-SPEC.md section 5): by its levels for
  a `category` column, and by bins the profiler chooses for a `number` or `timestamp` column.
- `relationships[].linked`: a list of `{ "column", "parentColumn" }`, a column of the child table and one of the
  parent table, each pair named once, at most 1 024. The profile states the child column's statistics grouped by
  the parent column through this relationship (`groupByLink`, PROFILE-SPEC.md section 3).

A name that no column of its table holds is refused with `FLX-5038`, naming where.

**What `fluxsim init` asks for.** Given a contract and no profile, `fluxsim init` writes a request of format 1-2-0
only when it asks for one of the keys above, and a request with none of them keeps its bytes and the format it
named before. For each table it asks for `kendallTau/1` over the number and timestamp columns it would draw from
histograms (at most 64, and none when the table has fewer than two), and names as `cliqueColumns` the columns a
clique could hold (at most 1 024, and none when the table has fewer than two). For each relationship it could lay
out on the counted layout, one column on each side and to another table, whose foreign key no key, other
relationship or rule of the contract sets, it asks to be grouped by every column its parent draws (at most 64) and
lists as `linked` the pairs of a child column and a `category` column of the parent (at most 1 024). A column that a
key, a relationship, a rule or a hidden or identifier column of the contract sets is asked for none of these. The
request holds candidates: which of them a profile answers, and so which `init` draws that way, is the profile's.

When the profile answers, `init` reads its rank correlations into one copula for each table and its grouped
histograms of children per parent into the `given` of a `counts` entry. A column is left out of the copula when
two independent draws of it are equal with probability at least 1/20, the sum over its bins of the squared share of
a bin divided by the number of grid values in it, since the statistics carry no tie correction. The copula's
measure is Kendall's when the profile publishes it for any pair of the copula's columns, and Spearman's
otherwise. The ledger of `init` says why each statistic was used or left unused (the how-to
[Write a bundle from a profile](../how-to/write-a-bundle-from-a-profile.md)).
