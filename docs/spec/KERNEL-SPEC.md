# FLUX Simulation Engine: kernel normative specification (v0.1)

Status: normative for `flux_engine.core`, `flux_engine.world`, `flux_engine.runtime`, `flux_engine.expr`,
`flux_engine.seam`. Every rule here is either pinned by a published vector file, a golden, or a property test.
A change to any rule is a `kernelVersion` bump with a `GOLDEN-CHANGELOG.md` row.
Kernel `0.2.0` is specified in [KERNEL-SPEC-2.md](./KERNEL-SPEC-2.md): it replaces the rules it names and keeps the rest
of this document, which stays normative for kernel `0.1.0`.

Vocabulary used in code and tests: "subject" = one synthetic entity of the population; "run" = one execution of a
`Simulation` under one effective seed; "tick" = one virtual time step.

---

## 1. Versions carried in every artifact

| Name | Meaning | Where |
|---|---|---|
| `kernelVersion` | semver of *normative behaviour* (this document). Bump = goldens move on purpose | `plan.json`, inside `inputDigest`, receipt, run digest document |
| `seedTreeVersion` | `"flux-seed/1"` | receipt, `plan.json` |
| `traceVersion` | `"flux-trace/1"` | receipt, run digest document |
| `runFormatVersion` | shape of `run-digest.json` | run digest document |
| engine package version | `flux_engine.__version__`: informational only, never hashed | receipt (masked) |

A plan names its kernel. `apply`, `replay`, `verify` and `ship` run and stamp the kernel the plan names, never the
one a new plan would take, so a plan recomputes the same `inputDigest`, and every digest after it, on any engine that
implements its kernel. An engine that does not implement it refuses the plan (`FLX-2119`), and a `run-digest.json`
that names a different kernel from its plan's is refused (`FLX-2113`).

## 2. Canonicalisation and digests (`core/canonical.py`): the only module that imports `rfc8785` and `hashlib`

1. **Canonical JSON** = RFC 8785 (JCS) via `rfc8785.dumps`, applied after:
   - recursive NFC normalisation of every `str` value **and every key**;
   - coercion of non-`str` mapping keys to `str` (`True`→`"true"`, `1`→`"1"`), because PyYAML yields bool/int keys;
   - rejection (`FLX-1001`) of any `float` anywhere in the object: values that carry numbers are `int` or
     `Decimal`; `Decimal` is serialised as a **string** with its declared scale (`"42.0000"`), never as a JSON number.
2. **Digest** = `"sha256:" + hex(sha256(canonical_bytes))`.
3. **Digest domains**: a digest is always a `DigestRef(value, domain, subject)`; comparing two refs of different
   domains raises `FLX-1002`:
   - `raw-bytes`: file bytes exactly as on disk (manifest `files`, the shipped `.fluid.yml`);
   - `jcs-document`: canonical JSON of a parsed document (`contractDigest`, `planDigest`, `inputDigest`, run digest);
   - `jcs-ndjson-stream`: the emit stream (§8).
4. **`flux_engine.core.canonical.NON_DIGEST_FIELDS`** = `{"generated_at", "generated_by", "engineVersion", "hostname", "durationMs"}` plus the
   self-referential fields of the document being digested (`planDigest` inside plan.json, `baselineDigest`,
   `outputDigest`). A totality test enumerates every receipt field and asserts it is either digested or listed.
   The name is qualified because `flux_engine.core.artifact` defines a constant spelled the same way holding a
   different, smaller set: the two envelope-only fields (`generated_at`, `generated_by`) an artifact envelope
   masks. The digest mask is the `canonical` one; `artifact`'s is a subset of it, and nothing outside that module
   reads it.

## 3. Seed tree (`core/seedtree.py`, `flux-seed/1`)

```
effective_seed = Simulation.spec.seed if present else World.spec.seed          # both int64
root_label     = b"flux-seed/1" + b"\x00" + H_bytes + b"\x00" + effective_seed.to_bytes(8, "big", signed=True)
root           = int.from_bytes(sha256(root_label).digest(), "big")
```
`H` is the contract digest of the seam (the `jcs-document` digest over the FLUID document + the `emits[]` entry +
the declared column set, §8). When a Simulation has several `emits[]`, `H_bytes` is the sha256 of the concatenation
of the per-emit digests in `emits[]` order. **Changing the contract changes every stream.**

Stream derivation is by **path**, never by spawn order:
```
label(path) = b"\x1f".join(NFC(str(part)).encode("utf-8") for part in path)
stream(path) = Generator(PCG64(SeedSequence(int.from_bytes(sha256(b"flux-seed/1\x00" + root_bytes + b"\x00" + label).digest(), "big"))))
```
`PCG64` is named explicitly (never `default_rng`). numpy is used **only** as the bit source
(`bit_generator.random_raw()`); no numpy distribution function is called on any digest path.

Canonical paths (closed list; adding one is a `kernelVersion` bump):

| Path | Consumer |
|---|---|
| `("world", world_id, "cohorts")` | LRM tie-free, so this stream is reserved and currently unused |
| `("world", world_id, "market", ordinal)` | market assignment of subject `ordinal` |
| `("world", world_id, "subject", ordinal, "trait", trait_name)` | trait draw |
| `("world", world_id, "subject", ordinal, "products")` | vertical-pack derived attributes |
| `("sim", sim_id, "signal", signal_id, "subject", ordinal, occurrence)` | event arrival and payload |
| `("sim", sim_id, "journey", journey_id, "subject", ordinal, step)` | reaction draw |
| `("sim", sim_id, "campaign", campaign_id, "subject", ordinal)` | delivery-side randomness |
| `("sim", sim_id, "temperature", dial, "subject", ordinal, occurrence)` | stochastic share of a dial |
| `("experiment", experiment_id, "assignment")` | rooted at `Experiment.spec.assignment.seed`, **not** at `root` |
| `("sweep", run_index)` | stochastic sweeps: run `k` uses `effective_seed = baseSeed + k` |
| `("sim", sim_id, "agentic", skill_name, "subject", ordinal, call_ordinal)` | v0.3 |

Every path materialised during a run is recorded (sorted) in `seed-paths.ndjson` beside the plan (one canonical path per line; raw-bytes digest = `seedTreeDigest`) and only `seedTreeDigest` plus a per-shape count table are written into `plan.json`, which stays small and is the artifact that is golden-pinned; a run that touches a path the plan did not anticipate fails with `FLX-2201`.

**Insertion stability** (property test): adding an unreferenced document to the bundle, or a new consumer that uses
a new path, changes no existing stream.

## 4. Draws (`core/rng.py`): fixed draw count, no rejection, no float branch decisions

- `uniform53(stream) -> int` in `[0, 2**53)`: `random_raw() >> 11`.
- `bernoulli(stream, p: Decimal) -> bool`: `uniform53 < floor(p * 2**53)`; the comparison is integer.
- `categorical(stream, levels: Mapping[str, Decimal]) -> str`: cumulative integer thresholds over levels
  **sorted by NFC key**; one draw.
- `bounded_uint(stream, n) -> int`: Lemire's nearly-divisionless method, **debiased by a fixed 64-draw budget**:
  draw up to 64 raw words; if all are rejected (probability < 2^-64·64), take the last word modulo `n` (documented
  bias, effectively unreachable). Draw count is bounded, never data-dependent beyond the bound; the bound is
  consumed in full (all 64 words are always drawn) so the stream position is data-independent.
- `permutation(stream, n)`: Fisher-Yates over `bounded_uint`.
- `truncated_normal(stream, mean, stddev, lo, hi) -> Decimal`: **one** `uniform53`, mapped through the inverse CDF of
  the normal truncated to `[lo, hi]`: `u' = Φ(lo) + u·(Φ(hi) − Φ(lo))`, `x = Φ⁻¹(u')`, where `Φ` and `Φ⁻¹` are
  evaluated in the `decimal` module at 28 digits (Acklam's rational approximation with one Halley refinement step,
  `Decimal.ln()`/`.sqrt()`/`.exp()`, correctly rounded by the General Decimal Arithmetic spec, therefore
  bit-identical across libm and architectures). Result quantised to the trait's declared scale (default 6 dp).
- `exponential`, `poisson` (for rates): inverse CDF in `decimal`; poisson by sequential search with a hard cap at
  `mean + 10·sqrt(mean) + 10` (fixed, documented); one uniform per draw.

Never: `random`, `numpy.random.Generator.<distribution>`, `math.exp/log/erf` on a digest path, rejection loops.

### 4.1 Random-access permutation (`core/permute.py`)

`permute(key, domain, index)` is a bijection of `[0, domain)` for every 32-byte `key`, read at one index without
building the rest, so the value a key takes at row `i` is computed in whichever process holds row `i` (§8.2):

```
b = bit_length(domain - 1); u = b // 2; v = b - u        # left part u bits, right part v bits (FF1's split)
x = index
repeat:                                                   # cycle walking (Black and Rogaway, 2002)
    L, R = x >> v, x mod 2**v
    for i in 0, 1, 2, 3:
        even i: L = (L + F(i, R)) mod 2**u
        odd i:  R = (R + F(i, L)) mod 2**v
    x = L * 2**v + R
until x < domain
F(i, y) = int(sha256(b"flux-permute/1" + b"\x00" + key + b"\x00" + domain.to_bytes(33, "big") + bytes([i]) + y.to_bytes(32, "big")))
```

A Feistel network is invertible whatever its round function, so the four rounds permute `[0, 2**b)`, and the walk
stays on the cycle of `index`, which holds `index`, so it ends; `2**b < 2 * domain`, so it takes fewer than two
passes on average. Four rounds is Luby and Rackoff's count for a strong pseudorandom permutation from a pseudorandom
round function. No stream is read, so the number of passes moves no stream position. `domain = 1` gives `0`; a key
of another length, a domain outside `[1, 2**256]` or an index outside `[0, domain)` is `FLX-5021`.
`tests/vectors/permute-vectors.json` pins images for several domains.

## 5. Largest Remainder Method (`core/lrm.py`)

Inputs: `n: int ≥ 1`, `weights: Mapping[str, Decimal]` summing to 1 within 1e-9 (validator already checks).
Algorithm on exact rationals (`fractions.Fraction(str(weight))`): `floor(n·w_i)` each, residual units go to the
largest fractional parts; **ties broken by NFC-sorted key** (never by seed). Output preserves the input key order.
Property: sum equals `n` exactly; invariant to input order; changing the seed never changes cohort sizes.

## 6. Temperature dials (`runtime/temperature.py`)

A dial `t ∈ [0,1]` for an outcome with authored rate `p` over an eligible set of subjects is realised as:
1. partition the eligible subjects (in canonical ordinal order) by LRM into a **frozen share** `1−t` and a
   **stochastic share** `t`;
2. in the frozen share, exactly `LRM(count, p)` subjects (the first ones in ordinal order) get the positive outcome,
   with no draw;
3. in the stochastic share, each subject draws `bernoulli(stream(temperature path), p)`.
`E[outcome rate] = p` for every `t`; `t = 0` consumes no draw; variance is monotone in `t`. Dial names are open;
`engagement` scales event-arrival rates, `campaignResponse` scales `Journey.personaReactions[].successProbability`.

## 7. Virtual clock and scheduler (`core/clock.py`)

- Time is integer milliseconds from `Simulation.spec.timeBounds.start` (RFC 3339, normalised to UTC). No wall clock.
- Tick = `tickMs` (engine-owned, declared in the receipt; default 3 600 000).
- Every scheduled item has the key `(t_ms, phase, entity_ordinal, stream_ordinal, seq)`: `phase` is the fixed enum
  `SIGNAL=0, DETECT=1, JOURNEY=2, CAMPAIGN=3, EXPERIMENT=4, EMIT=5`; `entity_ordinal` is the subject ordinal
  (`-1` for world-level items); `stream_ordinal` is the canonical ordinal of the producing document in the bundle
  (documents sorted by NFC id); `seq` is a per-run monotonic counter. The key is unique by construction.
- The queue is a binary heap over that key; the scheduler runs in one thread; iteration over any mapping is by sorted key.

## 8. Emit stream and `outputDigest` (`seam/emit.py`)

Rows are emitted in scheduler order as **arrays aligned to an ordered header** taken from the expose's declared
column order (JCS sorts object keys, so objects would erase the order):
```
header       = {"exposeId", "columns": [name...], "types": [logical type...], "contractDigest"}
line(row)    = JCS([v1, v2, ...]) + b"\n"           # values rendered per §9
outputDigest = "sha256:" + hex(sha256(b"flux-emit/1\x00" + JCS(header) + b"\x00" + concat(line(row) for row in rows)))
```
Domain `jcs-ndjson-stream`. Computed **above** the sink; the sink cannot influence it. `contractDigest` is recomputed
from the in-bundle `.fluid.yml` (`jcs-document`) and compared with the declared `provenance.contractDigest`
(`FLX-3101` on mismatch). The seam digest `H` used in §3 is the `jcs-document` digest of
`{"contract": <parsed .fluid.yml>, "emit": <the emits[] entry with concordance block if present>, "columns": [...]}`.

### 8.1 Keys from the contract (`seam/keys.py`)

Primary and foreign keys come from the FLUID contract, and FLUX does not restate them. They are read from a
contract that declares `fluidVersion: "0.7.7"`, the first FLUID version with relationships (flux-spec 0.5.2
vendors it as a preview, which only an exact pin selects). A contract on an earlier version is read exactly as
before, entities and all.

- A relationship (`exposes[].semantics.relationships[]`) is one foreign key: the `fromColumns` of the declaring
  expose equal the `toColumns` of the expose `to` names, paired by position, so a composite key is one
  relationship. It reaches at most one target row, so its `toColumns` are a key of the target. A `one_to_one`
  relationship makes its `fromColumns` a key of the declaring expose too. On a target with a validity window
  (a dimension carrying `typeParams.validityParams`) the key holds at each point in time, not across versions.
- A `primary` or `unique` entity is a one-column key of its expose, and its `expr` (the entity's name when
  absent) names the column. A `natural` entity names the key a validity window versions. A `foreign` entity is
  a join name and keys nothing.
- A key that does not resolve is refused where the contract declares it (`FLX-3005`): `to` names no expose of
  the contract, the two column lists differ in length or name a column twice, a column is not one its expose
  declares, or a key entity's `expr` names no column.
- An emit of a contract that declares keys pins the contract digest in `provenance.contractDigest`
  (`FLX-3112`; the message carries the digest of the contract in the bundle). A moved key then stops the seam:
  the reference validator reports the stale pin (`FLX-3105`), and the engine's own check is `FLX-3101`. The
  reference validator and section 2 canonicalise a YAML float, and text not in Unicode NFC, differently, so a
  contract that declares keys cannot carry either until the two agree; the `FLX-3112` message says so when it
  does.
- The Simulation does not restate a key (`FLX-3111`), whatever FLUID version its contracts declare. Its
  extensions namespace and an emit's block carry no `keys`, `primaryKey`, `primaryKeys`, `foreignKey`,
  `foreignKeys`, `uniqueKeys`, `relationships` or `entities` (refused as `FLX-3105` before keys came from the contract). A fidelity `unique: false` on a column the contract makes a key is refused, and so is
  `unique: true` on a key the contract's validity window repeats across versions. `unique: true` on a
  one-column key agrees with the contract and passes.
- In a generator-driven emit (one with a fidelity block, SEAM-FIDELITY-SPEC section 1), the keys and foreign
  keys are generated together (§8.2): every key is unique and every foreign key value is a key its parent's
  rows hold. A scenario-driven emit fills its columns from its mapping, as before.

### 8.2 Related tables (`generator/relations.py`)

The generator-driven emits of one contract are related after every emit is planned and before any row is drawn.
Nothing is materialised: a value is still a function of the run, the expose, the row index and the column, so a
slice of rows is the same in any process, and `--jobs` gives the bytes one job gives.

**Keys are derived, never drawn, and none reads the seed.**

- A one-column INT, DECIMAL, DATE or TIMESTAMP key is laid out in ascending order over the grid points of its
  declared range, or of its declared bins, each bin taking the rows §5 allots it by `binWeights`: row
  `start_j + k` of bin `j` takes grid point `first_j + floor(k * width_j / count_j)`, and `count_j <= width_j`,
  so no two rows meet. An INT key that declares nothing counts `1..rows`; a DATE or TIMESTAMP key that declares
  nothing is laid out over the Simulation's `timeBounds`, the range such a column takes by default
  (SEAM-FIDELITY-SPEC section 1); any other key needs a declared range.
- A one-column STRING key is `permute(k, 16**L, rowIndex)` (§4.1) written as `L` lowercase hex digits, `L` its
  `identifierLength` (default 32). A `uuid` key permutes over `2**122` and is written as an RFC 9562 version 8
  UUID, every permuted bit kept.
- A key of several columns takes the point `permute(k, size, rowIndex)` of the product of its parts, decoded in
  mixed radix, parts in key-column order. A relationship whose columns lie inside the key is one part: its domain
  is the parent's rows, and its columns take the parent's key at the decoded row. Any other column is a part over
  its declared levels or the grid points of its declared range. A key that a smaller unique key lies inside (a
  one-column key, or the columns of a distinct relationship below) needs no layout of its own.
- `k = sha256(b"flux-key/1" + b"\x00" + H_bytes + b"\x00" + label(("sim", sim_id, "gen", exposeId, "key", column...)))`,
  from the emit's seam digest `H`, like an identifier.
- A key on an expose with a validity window is laid out the same way: unique on every row is unique at each
  point in time, with one version per key.

**A foreign key takes the parent's key at a parent row `p`.** The relationship's `fromColumns` take the values its
`toColumns` hold at `p`, so a composite foreign key copies one parent row whole.

- `distinct`: a `one_to_one` relationship, or one whose `fromColumns` are a key of the child, references each parent
  row at most once: `p = permute(k_rel, parentRows, rowIndex)`, `k_rel` as `k` with `("rel", name)` in place of the
  key columns.
- `declared`: a one-column relationship whose target key is an ordered layout, when its column declares a draw.
  The column draws on its own path as any column does, and `p` is the row whose key is nearest the drawn value,
  the lower key when two are as near; a value outside the parent's keys takes the first or the last. The declared
  shape of children per parent is kept.
- `uniform`: any other, `p = bounded_uint(stream, parentRows)` on the stream of the relationship's first
  `fromColumn`, after that column's null draw.
- A foreign key into a **reference expose** (SEAM-FIDELITY-SPEC section 2, `reference: true`) picks a row as any
  foreign key does: `distinct` and `uniform` as above. A `declared` pick is taken when the relationship has one
  column, the column declares levels or a support, and the parent column is a reference column: `p` is the row whose
  key equals the drawn level, and a declared level that is no key is `FLX-3403` at plan time. A reference expose's
  value at row `i` is its published cell, and it draws nothing.
- A foreign key's columns are null together, by the first column's declared `nullFraction` on its own path; another
  column of it declares none. The `nullFraction` is kept when the relationship's `joinType` is `left` (FLUID's
  default): a null references no row, and a `distinct` relationship's other rows still reach distinct parents.
  `inner` joins every row, so a `nullFraction` there is refused. Every other column of a key holds a value on every
  row: only a `distinct` relationship's own columns may be null, and not when they are a `primary` or `unique` entity
  or a key another relationship targets, whose children would copy the null.
- When two relationships set one column, the one with more columns sets it. The other is kept when its columns lie
  inside the first and the first's target declares the same relationship on the paired columns, so copying the
  parent row keeps both (a return's item inside its sale key, the sale's own item key pointing at the item). An
  `inner` one is kept only when the target's is `inner` too and the first cannot be null, so the value it copies is
  never null.

**A conditional link (kernel 0.2.0 only).** The Simulation's top-level `links` key (ADR 0022 section 5) may give a
relationship a `when`, a FLUID SQL/1 predicate over the emit's own columns. The foreign key is drawn on every row as
above, so no draw moves. A step ordered with the expression rules then sets every `fromColumn` of the relationship to
NULL on a row where the predicate is not TRUE: the link reaches no row there, and every copy or path through it reads
NULL. The step comes after the rules that set the columns its predicate reads, and before every rule that reads one of
the relationship's columns. A bundle that declares no `links` entry has no such step, and its plan document and
digests are those it had. Kernel 0.1.0 refuses the key (`FLX-3105`).

**A weighted pick (kernel 0.2.0 only).** A `links` entry's `weight` names a number column (INT or DECIMAL) of the
relationship's target. The pick is `weighted` in place of `uniform`: after the first column's null draw, when it
declares a `nullFraction`, one `uniform53` `u` is drawn at the draw index the `uniform` pick takes, and the parent row is
the first row `i` with `u < T_i`, `T_i = floor(2^53 * (w_0 + ... + w_i) / W)` on exact rationals over the parent's
final weights in row order (a NULL weighs zero; `W` is their total). The thresholds are worked out once per process for
the parent plan and the column over every parent row, through the row the parent's own pass writes, or, for a reference
parent, from the published values. A weight on a `distinct` relationship, on a `declared` pick, on a side of a bridge,
on a relationship another one implies or that a dependency sets, or on a reference expose's relationship, a weight
column that is no number column of the target, a negative weight and weights that total zero are `FLX-3403` (the last
two when the thresholds are first worked out, or at plan time for a reference parent). Kernel 0.1.0 refuses the entry
(`FLX-3105`). A plan with such a pick is generated on the per-row path.

**A sentinel (kernel 0.2.0 only).** A `links` entry's `sentinels` maps a column to the text of its sentinel. Each
column must be copied through the entry's relationship: a `sharedColumns` column of it, a copy of a `sharedAttribute`
whose path starts with it, or a `derive` whose whole value is a path whose first relationship it is. The text is read as
the column's type (a string as written, a bool as `true` or `false`, a number at the column's scale and within its range,
a date as `YYYY-MM-DD`), and a text that does not read or fit is `FLX-3105`. On a row whose path reaches no row, at its
first missing hop (a NULL key, a key that matches nothing, a condition that cleared the link), the copy writes the
sentinel in place of NULL. A row whose path reaches a row whose value is NULL keeps NULL, and so does a row whose own
value is NULL, which NULLs first keeps (EXPR-SPEC-2 section 6). A rule's document records its sentinel only when it
declares one.

**A bridge table's pairs (kernel 0.2.0 only).** The Simulation's top-level `bridges` key (ADR 0022 section 8) lists
`{emit, relationships: [a, b]}`: two distinct many-to-one relationships of one emitted expose whose pairs never
repeat. The two are laid out together as one key of two parts, `a` first, through the several-column key above with two
parts that each take the rows of their parent: the point `permute/2(k, rows_a * rows_b, rowIndex)` decoded in mixed
radix, the last part varying fastest, so row `i` takes the parent row `point div rows_b` through `a` and `point mod
rows_b` through `b`, and every `fromColumn` of both relationships is a member of the key. `k` is the layout key of the
label `("sim", sim_id, "gen", exposeId, "pair", a, b)`, as the key above is of its label with `"key"` and the key
columns. So no pair repeats and each side names a row its parent holds. A bundle that declares no bridge has no such
key, and its plan document and digests are those it had. Kernel 0.1.0 refuses the key (`FLX-3105`).

**A counted relationship (kernel 0.2.0 only).** The Simulation's top-level `counts` key (ADR 0016 section 1) puts a
many-to-one relationship of an emitted expose on the counted layout: its parent rows are not picked one child at a time but laid
out so that each parent has a whole number of children, in a run of consecutive child rows, and the relationship's first
column draws nothing. `minChildren` gives every parent at least that many children: the child rows beyond the minimum are allotted over the parents by their expected counts above it. `parentWhen` leaves every parent whose final row does not make the predicate TRUE with no children. The layout is a pure function of the child's rows, the parent's rows and the declared shape, worked out
once per process; KERNEL-SPEC-2 section 8.1 states it. A bundle that declares no `counts` entry has no such layout, and its
plan document and digests are those it had. Kernel 0.1.0 refuses the key (`FLX-3105`).

**Refusals** (`FLX-3403`, before any row): a relationship whose target the Simulation does not generate from
statistics, or that declares no rows; a foreign key whose type differs from its target's (for a DECIMAL, its precision
or scale); a key whose range, bins, identifier length or parts hold fewer values than its rows, or that declares a
draw that repeats values (levels, support, a mean, a rate, `given`), or levels that spell one value twice; a column
of a key that a smaller key keeps unique and that declares no draw of its own; a `one_to_one` child with more rows
than its parent; a `nullFraction` where the rules above allow none; two relationships that set one column otherwise
than above; a key that holds part of a relationship; keys that take their values from one another round a cycle; a
column drawn `given` a column whose value is not drawn; a `links` condition on an `inner` relationship, on one whose
columns are a key of the expose or part of one, and on one another relationship implies; a bridge with more rows than pairs, a `nullFraction` on either side's first
column, a side that is `one_to_one`, whose columns are a key, that is already part of a key or that another relationship
implies, a side a `links` condition configures, and one relationship in two bridges.

The document `generatorDigest` covers carries a `relations` entry for each related column, with what it reads of
its parent (the parent's `H`, rows, column plans and layouts), so a moved parent moves its children's digests. An
expose with no key and no relationship has no entry, and its digest does not move.

## 9. Value rendering (`seam/conform.py`)

Closed `LogicalType` set: `STRING, INT, DECIMAL(p,s), BOOL, DATE, TIMESTAMP, JSON, BYTES`. FLUID types are
normalised case-insensitively; anything outside the set fails by name (`FLX-3001`). Rendering: STRING → NFC string;
INT → JSON integer; DECIMAL → string at scale `s`; BOOL → JSON bool; DATE → `YYYY-MM-DD`; TIMESTAMP → RFC 3339 UTC
with millisecond precision and `Z`; JSON → canonical JSON string; BYTES → base64 string. `float` → `FLX-3002`.

INT renders from an `int` only: any other value class, `bool` and `Decimal` included, → `FLX-3003`. A
scenario-driven INT column (`seam/mapping.py`) is decided at plan time, never per value: its mapped source must
be **integral**, every value it can hold a whole number. The integral sources are the integer scalars
(`subject.ordinal`, `event.time`, `moment.firstTime`, `moment.lastTime`, `moment.eventCount`) and each derived
attribute, trait or payload field whose every declared family draws only whole numbers (`RateCount`;
`BoundedNormal` at scale 0; `Constant` holding a whole number), across every lifecycle override, Persona and Signal
that declares it. Any other source → `FLX-3303` before a row exists. A `Decimal` from an integral source is
converted exactly (`int(v)`, after checking `v` is finite and whole, else `FLX-3003`) before it is rendered, so the
class rule holds for every caller.

A row missing a `required` column → `FLX-3202`; an undeclared column → `FLX-3201`; errors name column, row index
and value **class**, never the value.

## 10. flux-trace/1 and the run digest (`core/trace.py`)

The trace is the closed list of observable decisions, each a canonical JSON object with `op` and fields:

| op | fields |
|---|---|
| `cohort` | worldId, lifecycle, count |
| `market` | ordinal, market |
| `trait` | ordinal, name, value |
| `signal` | t, signalId, ordinal, occurrence, payloadDigest |
| `detect` | t, detectorId, ordinal, firstT, lastT, eventCount |
| `transition` | t, journeyId, ordinal, fromState, toState, outcome, success |
| `deliver` | t, campaignId, ordinal, channelId |
| `suppress` | t, campaignId, ordinal, reasonCode |
| `assign` | experimentId, ordinal, variant |
| `emit` | exposeId, rowIndex |
| `decision` | seq, policyDigest, reasonCode, allow |

Kernel internals are never traced. Component digests: `populationDigest` (cohort+market+trait ops),
`eventDigest` (signal+detect), `journeyDigest` (transition), `deliveryDigest` (deliver+suppress), `assignmentDigest`,
per-emit `outputDigest` (§8), `decisionsDigest` (raw bytes of `decisions.ndjson`). The run digest document:
```
{"runFormatVersion": 1, "traceVersion": "flux-trace/1", "kernelVersion": ..., "inputDigest": ..., "planDigest": ...,
 "components": {"population": ..., "events": ..., "journeys": ..., "delivery": ..., "assignment": ..., "emits": {exposeId: ...}, "decisions": ...}}
runDigest = jcs-document digest of that document
golden.baselineDigest ≡ runDigest
```
A golden failure names the component that moved. `--update-golden` requires `--reason` and a
`GOLDEN-CHANGELOG.md` row (CI-enforced).

## 11. `inputDigest` and `planDigest`

`inputDigest` = `jcs-document` digest of `{"kernelVersion", "seedTreeVersion", "traceVersion", "documents": {path: raw-bytes digest, sorted}, "fluidDocuments": {...}, "lock": raw-bytes digest of flux.lock if present, "attributeModelDigest"}`.

A bundle directory holds two classes of file and no third.

**Inputs** are the documents the loader reads. `*.flux.yml` and `*.flux.yaml` fill `documents`,
`*.fluid.yml` and `*.fluid.yaml` fill `fluidDocuments`, and `flux.lock` fills `lock` as its own
field rather than as a member of either map. Every input is hashed, and the set that is hashed is
the set the offline reference validator read, taken from that read rather than from a second match
over the directory, so the files a run was checked against and the files its digest covers cannot
come apart.

**Products** are the files a pack writes beside the documents: `flux-manifest.json` and its detached
signature `flux-manifest.json.sig`. A product is never hashed. Its content is a function of the
inputs, so hashing it would fold a bundle's digest into itself, and the FLUX bundle format already
excludes the manifest from its own file map and Merkle root for that reason. The two names are read
from the bundler this engine links against, not copied, so a rename upstream carries here without an
edit. No suffix is excluded as a class: a file called `evil.sig` is not a product.

Everything else is refused. A regular file in the bundle directory that is neither an input nor a
product ends the command with `FLX-3108`, naming the file and naming the set this engine recognises,
before any digest is taken. There is no flag that hashes it anyway. This selection reads regular files in the bundle
directory and does not descend, so a run state directory such as `.flux/` and the `run/` directory
inside an extracted archive are outside `inputDigest` by construction. That is a statement about
this digest and not about the archive: the FLUX bundle format's own file map and Merkle root walk
the directory recursively, so a pack run over a bundle that carries a state directory ships those
files as archive members.

Two consequences follow, and both are the point. A bundle's digest does not move when a pack runs in
its directory, and a shipped archive extracts to the digest it was packed from, because the manifest
is a product in both cases. Nothing the engine reads is left out of the digest and nothing it does
not read is quietly folded in, because a file that is neither ends the command instead of being
guessed at. One function computes this selection and both `apply` and `verify` call it, so the two
stages cannot hold a second opinion about which files are inputs. The stage that computes
`inputDigest` reports the product files it found and did not hash; that report is not part of any
digested document, because a digest that folded in the list of files it skipped would depend on
those files again.

`planDigest` = digest of `plan.json` with its own `planDigest` and `generated_at` masked (forge-cli `plan_digest` rule). `apply`/`run` refuse to proceed if `inputDigest` or `planDigest` moved (`FLX-2101`, `FLX-2102`).

## 12. Fixed-point statistics (`flux_stats.fixedpoint`)

All statistics over emitted columns and over aggregate inputs are computed on a declared grid: `fractionalBits`
(default 16), quantiser rounds toward −∞, values are `int` on the grid; means and correlations are compared in
integer cross-multiplied form (no division, no roots); histogram bins have declared edges on the grid; a declared
tolerance is rounded in the direction that tightens it; an encoding-error bound `ε` is propagated by interval
arithmetic from declared value ranges and minimum counts; the compared margin is `τ′ = τ − ε` and compile fails
closed when `ε ≥ τ`. Intermediate magnitudes are bounded at compile time from the declared ranges and maximum
record count (`FLX-5001` when out of range).

## 13. Error code ranges (`FLX-nnnn`)

1000 canonicalisation · 2000 plan/replay binding · 3000 seam/conformance · 4000 governance/sovereignty ·
5000 numerics · 6000 expression language · 7000 calibration · 8000 evidence/pack. `fluxsim explain FLX-nnnn` prints
the catalog entry.

## 14. The six broken kernels (negative determinism suite)

Each is a test-only subclass or monkeypatch carrying one defect, and each MUST fail the property suite:
spawn-ordered seed tree · float-accumulating LRM · set-iterating scheduler · wall-clock timestamp in a traced op ·
uuid4 event id · sorted-key (object) row serialiser.
