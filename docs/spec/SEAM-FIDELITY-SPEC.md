# Seam generator and fidelity check: normative specification (v0.1)

## 1. The contract-schema generator (`generator/`)

Input: a resolved expose (`seam/contract.py: compile_expose`) → ordered columns with `LogicalType`. For each column
the generator picks a **family** from the column's declared aggregate statistics (§3) or, absent statistics, from
the type default:

| Column type (6) | Default family | Families allowed (4) |
|---|---|---|
| STRING categorical (declared support or ≤ 64 distinct) | categorical | categorical, empirical-histogram |
| INT | poisson/exponential-rate (counts) or uniform (ids) | categorical, bounded-normal, rate, uniform/empirical |
| DECIMAL(p,s) | bounded-normal | bounded-normal, rate, uniform/empirical, categorical |
| BOOL | categorical (Bernoulli) | categorical |
| DATE / TIMESTAMP | uniform within `timeBounds` or rate (inter-arrival) | uniform/empirical, rate |
| identifier (STRING/uuid with `unique`) | derived deterministically from `(H, ordinal)` — never random | — |

Every draw uses the §4 kernel samplers on the path `("sim", sim_id, "gen", exposeId, "row", rowIndex, column)`.
A column declared `given` another (§2) keeps that path: what it reads from the other column is the level that
column drew in the same row, which picks the weights it draws with.
Rows are produced in `rowIndex` order; the number of rows is declared (`extensions["com.agentics.flux/1"].emits[<exposeId>].rows`,
§2; default = population size when the emit is scenario-driven).

**Two row sources, and the declaration picks.** The generator above runs for an emit whose extension block
declares a `fidelity` block — declared statistics decide every value, and the fidelity check is specified over
them. An emit that declares no block is **scenario-driven**: its rows are the run's own moments, and one detector
firing for one subject is one row.

The scenario path (`seam/mapping.py`, `stages/pipeline.py`):

* **the moment is the detector fire**, not the journey transition. A transition exists only where a Journey's
  trigger held and a persona reaction was drawn, so journey-driven rows would silently drop every burst the
  detector found outside a journey's reach; the detection also carries its matched events, so every mapped column
  has a source.
* **the mapping is resolved at plan time**, once per emit, by three steps in fixed order, first match wins:
  (1) the extension block's `mapping` override; (2) the vertical's alias table (`verticals/<pack>.py`, e.g.
  `subscriber_id → subject.id`, `aborted_at → event.time`, `amount → event.data.amount`, `market →
  subject.market`); (3) the column's own name, searched under `event.data.`, `subject.`, `subject.traits.` in
  that order. A **required** column no step resolves is `FLX-3302`, naming the column, before any row exists; an
  optional one renders JSON `null`.
* **an INT column's source is checked in the same step**: it must be integral (KERNEL-SPEC §9), read off the
  declared families and never off the values a run drew, or the emit is `FLX-3303`, naming the column and the
  source, before any row exists. A `Decimal` from an integral source reaches the renderer as the exact `int` it
  equals.
* the moment's event is the **last** matched event of the detection — the one that completed the pattern.
* rows are scheduled through KERNEL-SPEC §7's scheduler at `phase = EMIT`, keyed on that event's instant, and
  written through the same `EmitWriter`; `outputDigest` is unchanged (§8 of KERNEL-SPEC, computed above the sink).
* a declared `rows` **caps** a scenario-driven stream; a run with fewer moments emits what it has and never pads.

## 2. The fidelity block (lives in `Simulation.extensions["com.agentics.flux/1"].emits[<exposeId>].fidelity`)

`fidelity` is the block's **only** spelling in a document. The
subsystem keeps its own module name (`flux_engine.concordance`), and that name
stays inside the package: a second accepted key would be two digests of one
contract — the same reason §2 refuses a bare support list below — and it would
put a reserved word into a file an author writes.

**Why it does not sit in `spec.emits[]`.** FLUX 0.5.0 declares `Simulation.spec.emits[]` items with
`additionalProperties: false`, so a bundle carrying `fidelity` or `rows` there is refused by the published
validator and by the packer — the bundle cannot ship. RFC-02's document-level `extensions` map is the one
sanctioned extension point (`propertyNames` = `reverse-dns/major`, contents owned by the namespace), so this
engine's declarations live there and every bundle stays valid against the **unmodified** 0.5.0 schema.

```yaml
extensions:
  com.agentics.flux/1:
    emits:
      payment_recovery_moment:              # keyed by the emit's exposeId
        rows: 1000                          # §1's declared row count
        mapping:                            # optional per-column source overrides (§1)
          aborted_at: event.time
        fidelity:
          encoding: { fractionalBits: 16, rounding: floor }
          records: { min: 1000, max: 100000 }
          columns:
            amount:      { range: { min: "0", max: "10000" }, null: exclude }
            market:      { support: { values: ["IE","GB","FR","DE"] }, maxCardinality: 8, null: fail }
            aborted_at:  { range: { min: "2026-07-01T00:00:00Z", max: "2026-09-30T23:59:59Z" }, null: fail }
          properties:
            - { property: mean, column: amount, tolerance: { type: relative, value: "0.02" } }
            - { property: marginalHistogram, column: amount, bins: ["0","50","100","250","1000","10000"], tolerance: { type: distance, measure: totalVariation, max: "0.05" } }
            - { property: support, column: market, tolerance: { type: count, max: 0 } }
            - { property: mean, column: amount, groupBy: market, tolerance: { type: relative, value: "0.03" } }
          budget: { maxChecksPerState: 20, maxStateAge: P30D }
          failureAction: withholdBundle
spec:
  emits:
    - productRef: telco.gold.payment_recovery_moment
      exposeId: payment_recovery_moment
      fluidVersion: "^0.7.3"
```
Rules enforced by `fluxsim validate` (offline): every `column` and `groupBy` is a member of the referenced expose
schema; every key of `extensions[...].emits` is an `exposeId` the Simulation actually emits; bins are strictly
increasing and partition the declared range; a `levelHistogram` names a `support` column and carries no `bins`;
a `correlation` property declares two columns and a `minVariance`;
every property's parameters are present (a contract missing any of them is refused before any statistic is
formed). Numbers are strings (exact decimals), never floats. Written out without an exponent, a number has at
most 1000 digits, not counting a leading `0.`: `"1E+99999999"` names a number of a hundred million digits and
is refused (`FLX-5030`) by its length, before its value is formed.

**`columns.<name>` is one schema, read by two consumers.** `range`, `support`, `maxCardinality` and `null` are
read by the check (§5); `bins`, `binWeights`, `binWeightsGiven`, `family`, `given`, `identifierLength`,
`levelWeightsGiven`, `levels`, `mean`, `nullFraction`, `rate`, `stddev` and `unique` are read by the §1 generator. The set is closed and shared
(`concordance.predicate.COLUMN_KEYS`): a key outside it is refused by both, and neither refuses a key the other
owns. A column entry that declares neither `range` nor `support` declares nothing the check can compare — it is
legal (the generator still reads it), and a *property* naming such a column is `FLX-5034`.

**`support` has exactly two spellings.** `{ values: [...] }` writes the levels out. `declared` takes them from
the FLUID column type's own enum list — `type: "enum('IE','GB')"`, the only structured value set FLUID 0.7.5
carries (`validationRules[].constraint` is free text with no grammar). A column that spells `declared` over a
type naming no levels is refused (`FLX-5034`) rather than resolved to an empty support, because an empty support
makes every value a non-member and fails the check for the wrong reason. A bare list is refused: two spellings of
one declaration would be two digests of one contract.

**`levelHistogram` counts a categorical column.** It names a `support` column, whose declared levels, NFC-normalised
and sorted, are its cells, as a `marginalHistogram`'s declared bins are its cells. It carries no `bins`, and its
tolerance is a total-variation distance. With `groupBy` (a second `support` column) it is a two-way count table of
the two columns: one count per (group, level) cell, compared group by group as a grouped `marginalHistogram` is.
A value of either column outside its declared support has no cell, so it is refused (`FLX-5005`) rather than
counted, as a value outside a declared range is. Every group needs `records.min` records, as every group does. A
declared level the state never holds is a cell of count zero, so the table's shape comes from the declaration and
never from the data.

**`given` draws a column conditionally on another.** `given` names one other column of the same expose, and the
column declaring it carries one weight table per level of that column, in place of its unconditional weights:

- a histogram column (declared `bins`) declares `binWeightsGiven` in place of `binWeights`: for each level of the
  given column, one `binWeights` list over the same edges, P(bin | level). A grouped `marginalHistogram`
  property (`groupBy` the given column, over the same bins) states exactly these lists: its (group, bin) counts;
- a categorical column declares `levelWeightsGiven` in place of `levels`: for each level of the given column, one
  mapping of level to weight, P(level | level), from a two-way count table. Every mapping names the same levels.

The weights are counts or shares, normalised per level of the given column as `binWeights` is, and every list or
mapping carries some weight. The given column is a categorical with no `nullFraction`, so each row has exactly
one level to look up, and the table names exactly the levels it can draw. The links form a forest: a column has
at most one `given`, a cycle is refused, and a histogram column is always a leaf, because a bin is not a level.
A column is drawn after the column it is given, whatever the declared column order; every cell keeps its own
§1 path, so column-first generation and slices are unchanged, and a root of the forest (a column drawn given
nothing) draws exactly the values it drew without the links. A declaration the
generator cannot draw is refused at plan time with `FLX-3402`. A plan that declares no `given` documents none of
the three keys, so its `generatorDigest` and its rows are the ones it had before they existed.

`flux_engine.calibrate.structure` suggests the forest offline, from two-way count tables alone: a Chow-Liu tree
(Chow and Liu, 1968), the maximum spanning tree of pairwise mutual information, which is the selection step of
the MST synthesizer (McKenna, Miklau and Sheldon, 2021) without its privacy noise, with every numeric column a
leaf. It reads counts and writes the `columns.<name>` entries; it never reads a record. Counts can still single
out a rare combination: a cell of one or two counts describes a handful of real records, so the owner applies
their own disclosure rule, such as a minimum cell count, before declaring a table.

**What the check covers of a `given` link.** Both kinds of link are checked end to end, by a grouped property
over the drawn column with `groupBy` the given column. A histogram drawn given a categorical is checked by a
grouped `marginalHistogram` over the same bins, and a categorical drawn given a categorical by a grouped
`levelHistogram`. Each compares D′'s (group, cell) counts with S's, group by group. They are the counts the link is
declared from: a grouped `marginalHistogram`'s (group, bin) counts are `binWeightsGiven`'s lists, and a grouped
`levelHistogram`'s (group, level) counts are `levelWeightsGiven`'s mappings. `flux_engine.calibrate.structure`
reads them back as two-way tables (`pair_table_from_groups`, `pair_table_from_level_groups`), so the forest can be
suggested and declared from a committed S. A root's own `levels` are checked by an ungrouped `levelHistogram` over
it. A pair of columns that no declared property counts together is not checked.

## 3. Digests

- `H` = `jcs-document` digest of `{"contract": <parsed .fluid.yml>, "emit": <emits[] entry>, "extension": <the emit's entry in `extensions["com.agentics.flux/1"].emits`, when it has one>, "columns": [declared column names in order]}`. The `extension` key is **present only when the emit has such an entry**, so a bundle that declares none keeps the `H`, the seed root and the whole stream it had before the block existed. Computed by `fluxsim plan`, printed, and stored in `plan.json`; the run seed derives from it (KERNEL-SPEC §3).
- `G` = the same with the **excluded set removed** from the emit entry and from the extension block: every `tolerance`, every `minVariance`, `budget.maxStateAge`. `G ⊂ H`. A change that leaves `G` unchanged keeps the committed statistic reusable.
- `ε` per property, computed by the compiler from `encoding`, `columns[].range`, `records.min` (and `minVariance` for correlations) by interval propagation; `τ′ = τ − ε`; `ε ≥ τ` → `FLX-5002` at plan time; `τ′` rounded onto the grid in the tightening direction.
- Both `H` and `G` and every `ε` are written to `plan.json`.

## 4. Boundary side (`concordance/boundary/`, invoked by `fluxsim apply --boundary` from `Playback.sourceStream`)

- **Committed state**: the owner fixes the record set (`file://` slice in v0.1; partition/sequence boundary recorded); `stateOrdinal` increments per state.
- **C1**: per-column Merkle root (sha256 in v0.1; the leaf function is a port so it can become circuit-friendly later) over fixed-point-encoded values padded to `records.max` with the reserved padding encoding; nulls carry the null encoding; a validity flag per position.
- **S** (derived statistic): per property the aggregate quantities only: bucket counts on the declared edges, level counts on the declared support, `Sx`, `Sy`, `Sxy`, `Sxx`, `Syy`, the sorted committed support, the quantile witness, `n`, `k` per property and per group.
- **C2**: sha256 over canonical `S` plus a per-state blinding value that never leaves the boundary.
- What leaves the boundary in v0.1: `S`, `C1`, `C2`, `stateOrdinal`, record count (or declared range), commitment time. **Never a record.** The check counter (`budget.maxChecksPerState`) lives here and refuses beyond budget (`FLX-5010`).

## 5. Verifier (`concordance/verifier.py`, `ClearStatistic` backend)

Inputs: the contract set (recomputes `H`, `G`), `S`, `C1`, `C2`, and D′ (recomputes D′'s statistics on the same grid and D′'s digest = `outputDigest`). Evaluates each property in the integer cross-multiplied form:
- mean: `|Sx·n′ − Sx′·k| ≤ τ′·k·n′` (absolute) or the relative homogenised form;
- total variation: `Σ_bins |n′·c − k·c′| ≤ 2·τ′·k·n′`;
- level histogram: the same form over the declared levels, `Σ_levels |n′·c − k·c′| ≤ 2·τ′·k·n′`, with `ε = 0`;
- correlation: `L·R ≤ A ≤ U·R` split by signs, with `Qx ≥ k²·v`, `Qy ≥ k²·v`;
- quantile: order-statistic rule on both sides;
- support: count of non-members ≤ `max`.
Fail-closed conditions: `k = 0`, count outside `records`, group below `records.min`, a value outside its range or its declared support, a column absent, `ε ≥ τ`. **No proof is generated in v0.1**; the `ProofSystem` port has one implementation, `none`, and the record says so.

## 6. The fidelity record (`concordance/record.py`) — one per verified emit, in the evidence pack

```json
{
  "recordVersion": 1,
  "reference": { "productRef", "exposeId", "fluidVersion", "contractDigest" },
  "H": "sha256:…", "G": "sha256:…",
  "committedState": { "ordinal", "C1", "C2", "recordCount" | "recordRange", "committedAt", "checkOrdinal" },
  "synthetic": { "outputDigest", "rows", "generatorDigest" },
  "properties": [ { "property", "column(s)", "groupBy?", "tolerance", "margin", "epsilon", "outcome": "within|outside" } ],
  "proofSystem": "none",
  "scope": {
    "certifies": "statistical relation between the synthetic dataset and the committed state under the registered properties",
    "doesNotCertify": ["privacy or re-identification resistance of the synthetic dataset", "fairness or any property of the source dataset itself", "any property of a system trained on or tested with the synthetic dataset", "that the synthetic dataset was generated independently of the source", "that the committed state is the whole source dataset", "predictive validity of any simulation outcome"]
  },
  "columnsCovered": [...],
  "signature": { "alg": "ed25519", "keyId", "sig" }
}
```
The scope statement is generated from a fixed template, never hand-edited; its `doesNotCertify` list is the
register-discipline guard. The record is signed by the verifier key (ed25519 via `cryptography`), and travels in the
evidence pack under `run/fidelity-record-<exposeId>.json`.

## 7. The gate (`concordance/gate.py`, invoked by `fluxsim ship`)

Recompute `H` over the contract set; verify the record's binding to `H` and its signature; recompute D′'s digest and
statistics from the rows actually placed in the pack; re-evaluate every property; check `committedAt` against
`budget.maxStateAge`. On any failure: write no bundle, exit 1 with `FLX-8001`, emit a CloudEvent
`com.agentics.flux.ship.withheld` to the configured sink (stdout NDJSON by default). On success: pack (staged copy;
the `.fluid.yml` copied byte-for-byte) and print the public re-verification commands.

## 8. What a change does

| Change | Effect |
|---|---|
| tolerance / `minVariance` / `maxStateAge` only | `H` moves, `G` unchanged → re-run the check against the cached `S`, new record |
| any other declaration in the block or the contract | `G` moves → new committed state required (`FLX-5011`) |
| generator (engine `kernelVersion`) | `generatorDigest` moves → re-materialise D′, new record against the same state |
| appended source records | new committed state (ordinal+1); additive quantities folded, quantiles recomputed |
