# End-to-end accuracy on AWS

This harness checks FLUX the way a data owner would use it. A relational source
lives on AWS (Parquet on S3, catalogued in Glue, queried with Athena). FLUX makes a
synthetic copy from the FLUID contract and a profile of the data, without ever
reading the data. The copy is published the same way, and four judges compare the
two.

The source is a TPC-DS-shaped dataset generated with DuckDB's `tpcds` extension.
Nothing this harness reports is a TPC-DS benchmark result, and its numbers should
never be presented as one.

It is a separate uv project. It is not part of the repository's workspace, it ships
in no wheel, and no CI job runs it. Every step that touches AWS refuses to run
unless `FLUX_E2E_AWS_LIVE=1` is set.

## The two sides

**The data owner** has the data and the AWS credentials. It:

1. runs `CALL dsdgen(sf=..., keys=true)` in DuckDB and copies each table in scope
   to Parquet;
2. writes the FLUID 0.7.7 contract, one expose per table, with each table's
   primary key, every foreign key between the tables, and the calendar's and the
   clock's rules (below);
3. profiles each table with `fluid profile` (forge-cli's `profile` extra) and joins
   the per-table documents into the one profile a Simulation pins;
4. publishes both copies to S3 and Glue, and runs the judges.

**The generator** gets three files: the contract, the profile and
`flux_e2e/bundle.py`, which writes the Simulation from the first two. It runs
`fluxsim init`, that script, `fluxsim validate`, `fluxsim run` (with the Parquet
sink) and `fluxsim verify`:

- under `env -i`, with a fresh HOME and TMPDIR inside its own directory and
  `PATH=/usr/bin:/bin`, so no AWS variable or configuration reaches it;
- from a virtual environment built from this checkout's lock file with the
  `arrow` extra only, which the harness checks holds no AWS SDK and no DuckDB;
- on macOS, under `sandbox-exec` with a profile that refuses all network access
  and refuses to read anything under the home directory, the temporary
  directories or the work root, which holds every run's directory, except its own
  directory, its virtual environment and the Python installation. Ordinary reads
  of the source files, the owner's files, other runs' files and `~/.aws`
  therefore fail. The profile guards against the engine reaching the data by
  accident; it is not a jail for code that is trying to escape.

The run records every command, its environment, its exit code and the sandbox
profile in `run.json`.

## How the Simulation is written

FLUX draws each column from statistics the Simulation declares. `bundle.py` takes
them from the profile's published metrics and says, per column, whether a
declaration came from the profile, from the contract or from an assumption:

- `rows` is the table's `rowCount`;
- `nullFraction` is `nullCount / rowCount`;
- a number or date column is a histogram over the published min, quartiles and
  max; a withheld extreme clips that tail to the nearest published value;
- a number column with too few order statistics for a histogram keeps its
  published mean (`sum / count`): a uniform range with the published min (or
  max) at one end and that mean, or else centred on the mean and as many values
  wide as the published `distinctCount`, or else the mean on every row. The
  spread is an assumption, and is counted as one;
- one published value and nothing else gives a one-level column (a one-day range
  for a date);
- a string column with an `enum(...)` type in the contract draws those levels with
  equal weights. The owner gives a column an enum only when it has at most 64
  levels, every level is held by at least 10 rows, and every level can be written
  as a quoted FLUID 0.7.7 level: not empty, with no control character, and
  already in Unicode NFC;
- any other string column draws as many opaque levels (`<column>-<n>`) as its
  published `distinctCount`, at most 1,000;
- a one-column integer primary key has no published order statistic (each of
  its values is one row), so it spans every value the foreign keys that reference
  it are declared over, and at least `rowCount` values from the lowest of them, or
  is `1..rowCount` when no key references it. A referencing key is declared by the
  rules above, so a small table whose min and max are withheld contributes the
  range around its mean. Both are counted as assumptions;
- whatever else the profile withheld becomes a placeholder, counted as assumed.

## The calendar and the clock follow their key

`date_dim` is a calendar keyed by the Julian day and `time_dim` a clock keyed by the
second of the day, so most of their columns are a function of the key. The owner writes
those functions into the contract as rules (`exposes[].contract.invariants[]`), and FLUX
generates the columns from them instead of from their own statistics (ADR 0011, step 1).
`flux_e2e/rules.py` holds the 22 rules: a `derive` for the year, the month of the year,
the month, quarter and week sequences, the day of the week, its name, the weekend flag,
the first day of the month, the quarter and the three fiscal columns, and for every column
of the clock but its key and its id; and one difference-form `check` that ties `d_date` to
`d_date_sk`. `bundle.py` gives that check the FLUX setting
`invariants.date_of_key.generates: d_date`, so FLUX generates the date from the key.

The rules follow dsdgen's calendar, quirks included. The owner counts each rule's
violating rows on the source with forge-cli's own reading of FLUID SQL/1, so a rule that
holds on part of the rows carries a band (`mustBeLessOrEqualTo`, in `rows`) and the source
still meets its own contract; FLUX then generates the rule on every row. Only the day of
the week keeps a band, since dsdgen's own value differs from the key's on a few days at the
two ends of the calendar. A band is a count over a calendar or a clock, which hold no
person, and it passes the same checks as the published statistics: a rule that fewer than
10 rows violate is left out, and a band is rounded up to a multiple of 5.

Three columns turn on a date part, date arithmetic or a quarter's name, which FLUID SQL/1
leaves out, so they follow the date through FLUX-side rules in flux-expr/3 (EXPR-SPEC-2,
section 1), which `bundle.py` writes under the emit's `rules`. They are the day of the
month (`d_dom`, `EXTRACT(DAY FROM d_date)`), the same day a year back (`d_same_day_ly`,
the key of that day, with the day clamped where the earlier year has no such day) and the
quarter's name (`d_quarter_name`, `d_year || 'Q' || d_qoy`). The first day of the month
and dsdgen's quarter are contract rules written from the day of the month, so FLUX sets
`d_dom` first. A FLUX-side rule is written only when the expose has its columns and no
contract rule already decides its column. The last day of the month and the same day a
quarter back keep their own statistics, because dsdgen's own values for them are not a
function of the date.

## A ticket's lines follow the ticket

Every line of a store sale's ticket carries the ticket's date, time, customer, demographics,
address and store, so `ticket` is a functional dependency from `ss_ticket_number` to those
seven columns (ADR 0011, step 2). `flux_e2e/rules.py` holds it beside the rules, and FLUX
generates it as EXPR-SPEC-2, section 8 says: each ticket gets its seven values once, and every
line of the ticket copies them, whatever the slicing or `--jobs`. A foreign key among them takes
the foreign key's own draw and then the parent's nearest key, so it still names a row.

The owner counts the dependency's violating rows on the source with the g3 measure of section 8
(a key's covered rows minus those that carry its most common tuple, a NULL being a value). dsdgen's
store sales hold one non-NULL value per column for each ticket, but dsdgen picks some lines and NULLs a
random subset of each picked line's columns, so roughly one line in twelve counts as violating. The
owner writes the dependency with the count the source needs, `mustBeLessOrEqualTo` in `rows`: the
violating rows rounded up to a multiple of 5, as for every other rule. The band is an integer because
the contract declares keys, and a contract that does cannot carry a YAML float (KERNEL-SPEC); the
owner refuses one before the run profiles anything. One to nine violating rows would describe single
rows, so the dependency is then left out, and a dependency no row violates carries no band. With that
band FLUX puts the columns' NULLs on NULL rows (EXPR-SPEC-2, section 8): each column keeps its declared
null fraction, the NULLs fall on the same lines as far as the band needs, and every non-NULL value of a
ticket agrees, so the generated tickets carry the source's structure. A band below what the column with
the most NULLs gives on its own is refused (`FLX-6204`) in every run.

## The judges

1. **FLUX verify.** `fluxsim verify --profile` against the pinned profile: the
   scorecard, the fidelity outcomes and the shape flags.
2. **Consumer queries.** The vendored Athena-dialect queries
   (`queries/athena-tpcds`) that read only the tables in scope run on both
   databases. A query's outermost SELECT says which output columns are measures
   (aggregates) and which are keys (its GROUP BY). Each answer is compared as
   rates: the relative error of each measure's total, the total variation
   distance between the two sides' shares of each measure by key, and the overlap
   of the keys. A query whose keys share no value between the two sides is
   counted as such rather than averaged in. A query that fails on the synthetic
   side is a result; one that fails on the source side stops the run.
3. **Keys.** On both copies: the share of duplicate primary-key values and the
   orphan rate of every foreign key. The source side is the control.
4. **SDMetrics.** Its multi-table `QualityReport` and `DiagnosticReport`, run on
   the owner's side because they read records.

## The privacy judge

A synthetic copy can still say something about the people it was made from. The
privacy judge measures how much, with the three attacks that the EDPB's
anonymisation guidance names as criteria: isolation (singling out), linkage
(linkability) and inference. They are the criteria of the Article 29 Working
Party's Opinion 05/2014, and the ones Anonymeter's evaluators implement. It runs
on the data owner's side and needs no AWS.

1. **A holdout of people.** `--holdout 0.1` moves about a tenth of the customers,
   with every sale and return that refers to them, out of the source before anything
   is profiled. A keyed hash of the customer key picks them, and `--holdout-seed`
   fixes the hash. FLUX is profiled and generated from the rest. The held-out
   people are Anonymeter's control set: an attack that does as well on people FLUX
   never saw has learned the population, not the individuals.
   Addresses and demographics stay in the source, because other customers and
   their sales refer to them; what leaves is each held-out person's link to them.
2. **One row per person.** The part FLUX was built from, the synthetic copy and the
   holdout each become one row per customer: the customer with its address and
   demographics, its number of store sales and its total spend.
   - Auxiliary columns (what an attacker knows): birth year and month, gender, city,
     state and zip.
   - Secret columns (what they try to learn): credit rating, purchase estimate,
     education and total spend.
   - Both lists, and the column sets the linkage attacks hold, are in
     `flux_e2e/privacy.py` and recorded with every run.
3. **The attacks.** Singling out (univariate and multivariate), linkability, and
   inference of each secret column. Linkability runs twice. The narrow sets (birth
   year, month and gender; city, state and zip) leave many people tied; they stay so
   that earlier runs remain comparable. The wide sets add marital status and
   education to the first and the preferred-customer flag to the second. Each attack
   reports:
   - its risk with a 95% confidence interval;
   - the success rates of the main attack, the baseline (random guessing) and the
     control;
   - whether the attack beat its baseline at all. An attack that did not cannot be
     trusted.

   Every attack also runs against a verbatim copy of the rows FLUX was built from,
   standing in for the synthetic copy. Its risk is what a generator that memorised
   its input would score here, the positive control a low risk is read against.
   Each criterion's headline is its highest risk among the attacks that beat their
   baseline. The history line keeps the headlines and every attack's own risk, so
   runs with different attacks can still be compared attack by attack. Anonymeter
   draws some randomness of its own, so a rerun with the same seed varies a little.

   The rows FLUX was built from are sampled down to the holdout's size first, so that
   every attack compares sides of equal size: singling out succeeds more often in a
   larger table, and Anonymeter's correction for a smaller control set extrapolates.

With `--sdmetrics-python`, SDMetrics' distance-to-closest-record protection metrics
run on the same rows too. For the overfitting metric, the training side is sampled
down to the holdout's size. The overfitting metric then runs once more, with a second,
disjoint sample of built-from people in place of the held-out ones. Nobody in that
sample was held out, so its score is the metric's own lean: SDMetrics scales each
column by the range of each reference set, and two samples of the same people need
not split 50/50. The overfitting score says something about memorisation only where
it falls below this null split. The report and the history line keep both.

The source is TPC-DS-shaped and holds no real people, so a run measures the method,
not a customer base.

Anonymeter is not a dependency of this harness and never becomes one. Its licence,
the Clear BSD License, grants no patent rights, and its code is patent-pending. It
also pins `numpy<1.27`. Install it into an environment of your own and pass that
interpreter:

```console
uv venv ~/.local/share/flux-e2e-aws/anonymeter-venv --python 3.11
uv pip install --python ~/.local/share/flux-e2e-aws/anonymeter-venv/bin/python anonymeter "pandas<3" pyarrow
uv run python -m flux_e2e run --holdout 0.1 --no-aws \
    --anonymeter-python ~/.local/share/flux-e2e-aws/anonymeter-venv/bin/python
```

A run without `--holdout` keeps every customer and runs no privacy judge.

## SDMetrics stays private

SDMetrics is MIT licensed, but it requires `copulas`, whose licence is the
Business Source License 1.1. It is therefore in no dependency list of this
repository. To use the fourth judge, install `sdmetrics` and `pyarrow` into an
environment of your own, outside this repository, and pass its interpreter:

```console
uv venv ~/.local/share/flux-e2e-aws/sdmetrics-venv
uv pip install --python ~/.local/share/flux-e2e-aws/sdmetrics-venv/bin/python sdmetrics pyarrow
```

`--sdmetrics-python ~/.local/share/flux-e2e-aws/sdmetrics-venv/bin/python` then runs
`flux_e2e/sdmetrics_judge.py` with it. Without the option the judge is skipped.

## Running it

You need uv and, for the Athena judges, AWS credentials for a sandbox account that
may create S3 buckets, Glue databases and Athena workgroups, and the region to use.
`--no-aws` skips publishing and the Athena judges. At scale factor 1 a run peaks at
about 11 GB of memory, or about 119 GB with `--sdmetrics-python` (see
[Memory](#memory)). From this directory:

```console
uv sync --frozen
FLUX_E2E_AWS_LIVE=1 AWS_PROFILE=<profile> uv run python -m flux_e2e run \
    --scale-factor 1 --region us-east-1 --label "<what changed since the last run>"
```

Use `--until <step>` to stop early (`source`, `contract`, `profile` and `generate`
need no AWS access), and `--run-id <id>` to resume a run: finished steps are
skipped, and the scope, scale factor, seed and region must match the ones it
started with. Once the run has been judged, a resume needs no AWS access; before
that, a resume publishes again, because the sweep has deleted what was there.
`--keep` leaves the AWS resources in place. Where `sandbox-exec` is missing (any
host but macOS), the run stops before the generator starts unless
`--allow-unconfined` says that `env -i` alone is acceptable.

Every resource is named with the prefix `fluid-lane-flux-e2e-` (Glue databases with
`fluid_lane_flux_e2e_`). A run deletes everything under that prefix in its region
when the AWS steps end, and after a failure too, so run one at a time per region.
It exits non-zero when the sweep leaves anything behind. To sweep by hand:

```console
FLUX_E2E_AWS_LIVE=1 AWS_PROFILE=<profile> uv run python -m flux_e2e sweep --region us-east-1
```

The workgroup refuses any query that would scan more than 2 GiB.

### Memory

These peaks were measured at scale factor 1 with `--holdout 0.1`, `--jobs 8` and the
default `--sdmetrics-max-rows`, on a 64 GB Mac. Each one is the largest summed
physical footprint of the harness and its child processes, sampled once a second,
or one process's own recorded peak where that is higher.

| step | peak | what holds the memory |
| --- | --- | --- |
| `source` | 2 GB | the harness, which runs DuckDB's generator in process |
| `profile` | 8 GB | one `fluid profile` per table, eight at a time; `store_sales` takes 6.5 GB |
| `generate`, `fluxsim run` | 11 GB | a main process of up to 8.7 GB and eight workers of up to 0.6 GB each, peaking at different times |
| `generate`, `fluxsim verify` | 7 GB | one process on one core: it holds the largest table as Python objects |
| `privacy` | 2 GB | Anonymeter, 0.8 GB, then SDMetrics' distance metrics, 1.5 GB, with `--sdmetrics-python` |
| `contract`, `publish`, `judge` | under 1 GB | the harness |
| `sdmetrics` | 119 GB | SDMetrics' quality report, only with `--sdmetrics-python` |

Before the quality report scores a pair of categorical columns, it measures their
association in the source on a dense contingency table. For one pair, that table has
about 2.4 billion cells. The step's physical footprint, which on macOS also counts
compressed and swapped-out pages, reached 119 GB, while its resident memory peaked at
about 31 GB. Plan for the footprint, or leave out `--sdmetrics-python` to keep a run
at about 11 GB.

## A prototype for ADR 0011

`python -m flux_e2e.follow` measures what ADR 0011's rules would buy before FLUX
generates from them; since step 1, FLUX generates the calendar's and the clock's
rules itself, the FLUX-side ones in flux-expr/3 included, so it generates everything
the `reference` layer did and that layer is superseded. It clones a finished run, rewrites the clone's synthetic copy
and clears its publish and judge steps. `python -m flux_e2e run --run-id <clone>`,
with the run's own settings, then publishes and judges the rewritten copy:

```bash
uv run python -m flux_e2e.follow --from-run <run id> --run-id <clone id> \
  --layers reference,ticket,parent
```

- `reference` recomputes `date_dim`'s and `time_dim`'s attributes from the key, as
  the calendar and the clock they are;
- `ticket` gives every line of a store ticket the ticket's date, time, customer,
  demographics, address and store;
- `parent` makes each store return follow its sale: the sale's customer on the
  published share of rows, a date a published lag after the sale, a published share
  of the sold quantity, and the sale's price.

The owner side publishes the share and the two histograms that `parent` reads, as
frequency tables over the join of each return to its sale. They are published only
when every cell holds at least 10 rows and 10 distinct customers, and so does every
pattern of rows the tables leave out (a null in what one reads, or a return with no
sale), with the counts rounded to a multiple of 5; withheld tables leave their
columns as FLUX drew them. The clone gets its own output digests and drops the
original's privacy, publish, judge and SDMetrics results. FLUX's own verify step does not run
again on the clone, so the clone's report carries the original run's. A clone that
fails half way is removed.

## Where the results go

Results are the data owner's and stay out of this repository. Each run writes its
own directory under the work root (`$XDG_DATA_HOME/flux-e2e-aws`, by default
`~/.local/share/flux-e2e-aws`), readable by you alone: the source, the contract,
the profile, the generator's sandbox, `run.json`, `report.md` and `report.json`.
Every run also writes its headline numbers as one line of `history.jsonl` in the
work root, so a run after a change to FLUX reads against the runs before it. The
run records the sha256 of the bundle FLUX ran and of every file it wrote: the same
scale factor, seed and engine give the same digests.

## Tests

`uv run python -m pytest` runs the harness's own offline tests: no AWS, no source,
no FLUX run.
