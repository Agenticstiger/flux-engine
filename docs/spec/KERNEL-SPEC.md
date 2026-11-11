# FLUX Simulation Engine — kernel normative specification (internal, v0.1)

Status: normative for `flux_engine.core`, `flux_engine.world`, `flux_engine.runtime`, `flux_engine.expr`,
`flux_engine.seam`. Every rule here is either pinned by a published vector file, a golden, or a property test.
A change to any rule is a `kernelVersion` bump with a `GOLDEN-CHANGELOG.md` row.

Vocabulary used in code and tests: "subject" = one synthetic entity of the population; "run" = one execution of a
`Simulation` under one effective seed; "tick" = one virtual time step.

---

## 1. Versions carried in every artifact

| Name | Meaning | Where |
|---|---|---|
| `kernelVersion` | semver of *normative behaviour* (this document). Bump = goldens move on purpose | inside `inputDigest`, receipt |
| `seedTreeVersion` | `"flux-seed/1"` | receipt, `plan.json` |
| `traceVersion` | `"flux-trace/1"` | receipt, run digest document |
| `runFormatVersion` | shape of `run-digest.json` | run digest document |
| engine package version | `flux_engine.__version__` — informational only, never hashed | receipt (masked) |

## 2. Canonicalisation and digests (`core/canonical.py`) — the only module that imports `rfc8785` and `hashlib`

1. **Canonical JSON** = RFC 8785 (JCS) via `rfc8785.dumps`, applied after:
   - recursive NFC normalisation of every `str` value **and every key**;
   - coercion of non-`str` mapping keys to `str` (`True`→`"true"`, `1`→`"1"`), because PyYAML yields bool/int keys;
   - rejection (`FLX-1001`) of any `float` anywhere in the object — values that carry numbers are `int` or
     `Decimal`; `Decimal` is serialised as a **string** with its declared scale (`"42.0000"`), never as a JSON number.
2. **Digest** = `"sha256:" + hex(sha256(canonical_bytes))`.
3. **Digest domains** — a digest is always a `DigestRef(value, domain, subject)`; comparing two refs of different
   domains raises `FLX-1002`:
   - `raw-bytes` — file bytes exactly as on disk (manifest `files`, the shipped `.fluid.yml`);
   - `jcs-document` — canonical JSON of a parsed document (`contractDigest`, `planDigest`, `inputDigest`, run digest);
   - `jcs-ndjson-stream` — the emit stream (§8).
4. **`flux_engine.core.canonical.NON_DIGEST_FIELDS`** = `{"generated_at", "generated_by", "engineVersion", "hostname", "durationMs"}` plus the
   self-referential fields of the document being digested (`planDigest` inside plan.json, `baselineDigest`,
   `outputDigest`). A totality test enumerates every receipt field and asserts it is either digested or listed.
   The name is qualified because `flux_engine.core.artifact` defines a constant spelled the same way holding a
   different, smaller set — the two envelope-only fields (`generated_at`, `generated_by`) an artifact envelope
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

Every path materialised during a run is recorded (sorted) in `seed-paths.ndjson` beside the plan (one canonical path per line; raw-bytes digest = `seedTreeDigest`) and only `seedTreeDigest` plus a per-shape count table are written into `plan.json` — the plan stays small and is the artifact that is golden-pinned; a run that touches a path the plan did not anticipate fails with `FLX-2201`.

**Insertion stability** (property test): adding an unreferenced document to the bundle, or a new consumer that uses
a new path, changes no existing stream.

## 4. Draws (`core/rng.py`) — fixed draw count, no rejection, no float branch decisions

- `uniform53(stream) -> int` in `[0, 2**53)`: `random_raw() >> 11`.
- `bernoulli(stream, p: Decimal) -> bool`: `uniform53 < floor(p * 2**53)` — the comparison is integer.
- `categorical(stream, levels: Mapping[str, Decimal]) -> str`: cumulative integer thresholds over levels
  **sorted by NFC key**; one draw.
- `bounded_uint(stream, n) -> int`: Lemire's nearly-divisionless method, **debiased by a fixed 64-draw budget**:
  draw up to 64 raw words; if all are rejected (probability < 2^-64·64), take the last word modulo `n` (documented
  bias, effectively unreachable). Draw count is bounded, never data-dependent beyond the bound; the bound is
  consumed in full (all 64 words are always drawn) so the stream position is data-independent.
- `permutation(stream, n)`: Fisher–Yates over `bounded_uint`.
- `truncated_normal(stream, mean, stddev, lo, hi) -> Decimal`: **one** `uniform53`, mapped through the inverse CDF of
  the normal truncated to `[lo, hi]` — `u' = Φ(lo) + u·(Φ(hi) − Φ(lo))`, `x = Φ⁻¹(u')` — where `Φ` and `Φ⁻¹` are
  evaluated in the `decimal` module at 28 digits (Acklam's rational approximation with one Halley refinement step,
  `Decimal.ln()`/`.sqrt()`/`.exp()` — correctly rounded by the General Decimal Arithmetic spec, therefore
  bit-identical across libm and architectures). Result quantised to the trait's declared scale (default 6 dp).
- `exponential`, `poisson` (for rates): inverse CDF in `decimal`; poisson by sequential search with a hard cap at
  `mean + 10·sqrt(mean) + 10` (fixed, documented) — one uniform per draw.

Never: `random`, `numpy.random.Generator.<distribution>`, `math.exp/log/erf` on a digest path, rejection loops.

## 5. Largest Remainder Method (`core/lrm.py`)

Inputs: `n: int ≥ 1`, `weights: Mapping[str, Decimal]` summing to 1 within 1e-9 (validator already checks).
Algorithm on exact rationals (`fractions.Fraction(str(weight))`): `floor(n·w_i)` each, residual units go to the
largest fractional parts; **ties broken by NFC-sorted key** (never by seed). Output preserves the input key order.
Property: sum equals `n` exactly; invariant to input order; changing the seed never changes cohort sizes.

## 6. Temperature dials (`runtime/temperature.py`)

A dial `t ∈ [0,1]` for an outcome with authored rate `p` over an eligible set of subjects is realised as:
1. partition the eligible subjects (in canonical ordinal order) by LRM into a **frozen share** `1−t` and a
   **stochastic share** `t`;
2. in the frozen share, exactly `LRM(count, p)` subjects (the first ones in ordinal order) get the positive outcome —
   no draw;
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
- The queue is a binary heap over that key; the run is single-threaded; iteration over any mapping is by sorted key.

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

## 9. Value rendering (`seam/conform.py`)

Closed `LogicalType` set: `STRING, INT, DECIMAL(p,s), BOOL, DATE, TIMESTAMP, JSON, BYTES`. FLUID types are
normalised case-insensitively; anything outside the set fails by name (`FLX-3001`). Rendering: STRING → NFC string;
INT → JSON integer; DECIMAL → string at scale `s`; BOOL → JSON bool; DATE → `YYYY-MM-DD`; TIMESTAMP → RFC 3339 UTC
with millisecond precision and `Z`; JSON → canonical JSON string; BYTES → base64 string. `float` → `FLX-3002`.
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

## 12. Fixed-point statistics (`core/fixedpoint.py`)

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
