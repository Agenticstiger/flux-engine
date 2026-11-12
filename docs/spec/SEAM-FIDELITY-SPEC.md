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
increasing and partition the declared range; a `correlation` property declares two columns and a `minVariance`;
every property's parameters are present (a contract missing any of them is refused before any statistic is
formed). Numbers are strings (exact decimals), never floats.

**`columns.<name>` is one schema, read by two consumers.** `range`, `support`, `maxCardinality` and `null` are
read by the check (§5); `bins`, `binWeights`, `family`, `identifierLength`, `levels`, `mean`, `nullFraction`,
`rate`, `stddev` and `unique` are read by the §1 generator. The set is closed and shared
(`concordance.predicate.COLUMN_KEYS`): a key outside it is refused by both, and neither refuses a key the other
owns. A column entry that declares neither `range` nor `support` declares nothing the check can compare — it is
legal (the generator still reads it), and a *property* naming such a column is `FLX-5034`.

**`support` has exactly two spellings.** `{ values: [...] }` writes the levels out. `declared` takes them from
the FLUID column type's own enum list — `type: "enum('IE','GB')"`, the only structured value set FLUID 0.7.5
carries (`validationRules[].constraint` is free text with no grammar). A column that spells `declared` over a
type naming no levels is refused (`FLX-5034`) rather than resolved to an empty support, because an empty support
makes every value a non-member and fails the check for the wrong reason. A bare list is refused: two spellings of
one declaration would be two digests of one contract.

## 3. Digests

- `H` = `jcs-document` digest of `{"contract": <parsed .fluid.yml>, "emit": <emits[] entry>, "extension": <the emit's entry in `extensions["com.agentics.flux/1"].emits`, when it has one>, "columns": [declared column names in order]}`. The `extension` key is **present only when the emit has such an entry**, so a bundle that declares none keeps the `H`, the seed root and the whole stream it had before the block existed. Computed by `fluxsim plan`, printed, and stored in `plan.json`; the run seed derives from it (KERNEL-SPEC §3).
- `G` = the same with the **excluded set removed** from the emit entry and from the extension block: every `tolerance`, every `minVariance`, `budget.maxStateAge`. `G ⊂ H`. A change that leaves `G` unchanged keeps the committed statistic reusable.
- `ε` per property, computed by the compiler from `encoding`, `columns[].range`, `records.min` (and `minVariance` for correlations) by interval propagation; `τ′ = τ − ε`; `ε ≥ τ` → `FLX-5002` at plan time; `τ′` rounded onto the grid in the tightening direction.
- Both `H` and `G` and every `ε` are written to `plan.json`.

## 4. Boundary side (`concordance/boundary/`, invoked by `fluxsim apply --boundary` from `Playback.sourceStream`)

- **Committed state**: the owner fixes the record set (`file://` slice in v0.1; partition/sequence boundary recorded); `stateOrdinal` increments per state.
- **C1**: per-column Merkle root (sha256 in v0.1; the leaf function is a port so it can become circuit-friendly later) over fixed-point-encoded values padded to `records.max` with the reserved padding encoding; nulls carry the null encoding; a validity flag per position.
- **S** (derived statistic): per property the aggregate quantities only — bucket counts on the declared edges, `Sx`, `Sy`, `Sxy`, `Sxx`, `Syy`, the sorted committed support, the quantile witness, `n`, `k` per property and per group.
- **C2**: sha256 over canonical `S` plus a per-state blinding value that never leaves the boundary.
- What leaves the boundary in v0.1: `S`, `C1`, `C2`, `stateOrdinal`, record count (or declared range), commitment time. **Never a record.** The check counter (`budget.maxChecksPerState`) lives here and refuses beyond budget (`FLX-5010`).

## 5. Verifier (`concordance/verifier.py`, `ClearStatistic` backend)

Inputs: the contract set (recomputes `H`, `G`), `S`, `C1`, `C2`, and D′ (recomputes D′'s statistics on the same grid and D′'s digest = `outputDigest`). Evaluates each property in the integer cross-multiplied form:
- mean: `|Sx·n′ − Sx′·k| ≤ τ′·k·n′` (absolute) or the relative homogenised form;
- total variation: `Σ_bins |n′·c − k·c′| ≤ 2·τ′·k·n′`;
- correlation: `L·R ≤ A ≤ U·R` split by signs, with `Qx ≥ k²·v`, `Qy ≥ k²·v`;
- quantile: order-statistic rule on both sides;
- support: count of non-members ≤ `max`.
Fail-closed conditions: `k = 0`, count outside `records`, group below `records.min`, a value outside its range, a column absent, `ε ≥ τ`. **No proof is generated in v0.1**; the `ProofSystem` port has one implementation, `none`, and the record says so.

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
