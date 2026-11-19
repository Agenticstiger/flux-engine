# A local round trip against a generated truth

This harness checks the data owner's path end to end on one machine, with no
cloud. It draws a truth it knows exactly, profiles it the way a data owner would,
asks `fluxsim init` for a bundle from the contract and the profile alone, runs
that bundle, and measures how far the synthetic copy is from the truth.

It is a separate uv project, like [`e2e/aws`](../aws/README.md). It is not part
of the repository's workspace, it ships in no wheel, and no CI job runs it.

## What a run does

1. **Truth.** `fluxsim run` on the bundle in `truth/`: a FLUID 0.7.7 contract and
   a Simulation for three related business-to-business tables.
   - `organisation` (1,000 rows): `org_id`, `segment`, `region`, `tier` (drawn
     given `segment`, with `levelWeightsGiven`) and `seats`.
   - `account` (5,000 rows): `account_id`, `org_id` (a skewed spread over the
     organisations: the first ten hold three accounts in ten and the last hundred
     none), `plan`, `opened_on` and `monthly_fee`. A one-way contract
     `dependency` says a plan has one monthly fee. The fee each plan gets is a
     draw of the truth's seed, not a price list.
   - `invoice` (20,000 rows): `invoice_id`, `account_id` (a skewed spread over the
     accounts), `issued_on`, `net`, and two contract `derive` rules,
     `vat = ROUND(net * 0.081, 2)` and `gross = net + vat`.
2. **Profile.** The truth's rows go to Parquet, and `fluid profile` runs on each
   table at its default settings. The per-table documents are joined into the
   one profile a Simulation pins (as `e2e/aws/flux_e2e/owner.py` does).
3. **Round trip.** `fluxsim init <dir> --contract C --profile P --json`, then
   `fluxsim run <dir> --profile P --verify --json`. Both JSON envelopes are kept
   in the work directory.
4. **Judge.** Pure Python, on both sides' rows and on what the engine said. This
   is not the engine: the judges read records, which the engine never does.

A run takes about fifteen seconds.

## Run it

```console
cd e2e/roundtrip
uv run python -m flux_roundtrip run --record
```

The run's files go to `$XDG_DATA_HOME/flux-roundtrip` (`~/.local/share/flux-roundtrip`
by default), replaced on every run. Options:

- `--record` appends one JSON line to `history.jsonl`: the UTC date, the engine's
  git commit (`+dirty` when its source differs from it), the forge-cli commit the
  harness pins, every figure below, and which targets passed. The file is
  committed, so it keeps the history of scores. It holds no path.
- `--report-only` prints the targets but exits 0 when one fails. Without it the
  exit code is 1 when any target fails.
- `--work-root DIR` puts the run's files somewhere else.

`uv run python -m flux_roundtrip repin` writes the digest of `truth/contract.fluid.yaml`
into `truth/simulation.flux.yml` again, which is needed after editing the contract.
`uv run pytest` runs the offline tests of the judge math; they need no profiler
and no run.

## What each target means

Each is printed as `PASS` or `FAIL` with its numbers.

| Target | Meaning |
|---|---|
| No `CONSTANT` shape flag | `verify` raised no `CONSTANT` flag (a column the run drew as one value) on a column whose profile shows two or more levels or bins. A profile with no histogram for a column shows its published `distinctCount` instead. |
| Childless share within 0.10 | Per relationship, the share of parents with no child, truth against synthetic. |
| Top 1 % share within 0.10 | Per relationship, the share of children held by the 1 % of parents with the most children (at least one parent), truth against synthetic. |
| Grouped shares within 0.10 | For `tier` given `segment`, the largest absolute difference between a truth share and the same synthetic share of a tier within a segment. |
| Cramér's V within 0.05 | Cramér's V of `segment` and `tier` on each side, the absolute difference. |
| Contract derives on 100 % of rows | The share of synthetic invoices on which `vat == ROUND(net * 0.081, 2)` and `gross == net + vat` both hold. |
| 0 unused statistics | `fluxsim init`'s ledger (`ledger.counts.unused` of its JSON): statistics the profile publishes that no number of the bundle rests on. |
| The scorecard states its coverage | The scorecard's `coverage` holds the column and relationship shares. |

Reported, with no target:

- **The scorecard's overall score and its coverage** (columns and relationships
  covered, of those the contract declares). The score covers only what the profile
  gave the run a property for, so read it with the coverage beside it.
- **The k-marginal score**, per table and overall. For k = 2, it is
  `1 - mean TV`, the mean total-variation distance over every pair of categorical
  or binned columns of a table (primary keys left out). A number or a date is
  binned on the truth's quantile edges, ten bins. This is the k-marginal idea of
  [NIST SDNist](https://github.com/usnistgov/SDNist), reimplemented here with no
  dependency. 1 is a copy and 0 shares no cell; two draws of one truth score below
  1 from sampling noise alone.

## Reading the numbers

The children-per-parent spread is an approximation. `fluxsim init` lays the
profile's count classes over the parent's keys and weights each class, and the
generator then draws each child's parent independently, so a parent in a class of
mean `m` receives a Poisson-like number of children and about `e^-m` of them
receive none (ADR 0014, section 5). The childless share therefore comes out
larger than the profile says by about that much, and the exact counts need the
parents drawn first. Targets of 0.10 leave room for it.

`fluid profile` is pinned by commit in `pyproject.toml`, and the harness profiles the
whole product in one call. That profiler does not yet publish children per parent, so
the two relationship targets (childless share, top 1 % share) measure what `init` does
with a profile's `relationships`, which the engine's tests exercise with hand-written
profiles. They stay red on the profiler's defaults until it publishes them.

The baseline line in `history.jsonl` was recorded against a forge-cli that
publishes no histogram and no children per parent by default. Its profile holds
counts, sums, order statistics and the mean of each column, so `init` assumes
the levels, the grouped shares and the key spreads, and the targets on those fail
there. That is expected: it is the measurement the next change is judged by.
