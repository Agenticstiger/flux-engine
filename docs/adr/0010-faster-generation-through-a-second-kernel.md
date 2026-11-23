# ADR 0010: faster generation comes from a second kernel, not from a port of the first

## Status

Accepted 2026-11-18. **Amended 2026-11-18:** step 1 is specified in
[KERNEL-SPEC-2](../spec/KERNEL-SPEC-2.md), and `poisson` keeps kernel v1's
decimal search rather than a binary64 one (see the amendment below). Two
prototypes under `tests/bench` measure the parts the choice rests on.
**Amended again 2026-11-18:** step 4 is built and measured, about ten times
faster end to end on one job, step 5 is decided on its profile, no compiled
accelerator, and kernel `0.1.0` stays the default until a minor release of its
own switches it (see the last amendment). **Amended 2026-11-21:** that release
is 0.9.0, and kernel `0.2.0` is the default for new plans from it (see the
amendment at the end). **Amended again 2026-11-21:** step 5 is judged on the
profile taken after column-at-a-time rendering, and the engine does not adopt a
Rust line renderer (see the last amendment).

## Context

On main at `f6926de`, the related-star fixture scaled to 235,565 rows runs in
37.4 s on one job: 6,300 rows a second, 227 MB. With `--jobs 8` it takes 9.2 s,
4.1 times faster, and user time rises from 37 s to 55 s, which is process
overhead. The load average was about 4. Under cProfile the one-job run splits
like this:

- about 25% builds one stream per cell. `rng.from_entropy` is 11.5%, numpy's
  `errstate` inside the seeding is 5.9% and the label's sha256 about 5%.
- about 20% is decimal arithmetic. `Decimal.exp` alone is 9%, then the
  error-function series, Acklam's estimate and `truncated_normal` itself.
- key permutation is 3.7% and relation lookups about 6%.
- the rest is per-row Python: dispatch, null draws, rendering, framing.

One cell's stream, `Generator(PCG64(SeedSequence(e)))`, costs 6.28 µs on numpy
2.4.6. `PCG64(SeedSequence(e))` alone costs 6.06 µs and `SeedSequence(e)`
3.16 µs, so dropping the `Generator` wrapper saves 3.5%.

That cost is a consequence of what kernel v1 promises, and the promises are
the right ones. KERNEL-SPEC section 3 derives every stream from its path, never
from spawn order, so a value depends only on the run, the expose, the row and
the column. Section 4 evaluates every continuous draw in `decimal`, whose
`exp`, `ln` and `sqrt` are correctly rounded, so the bits cannot depend on a
platform's libm. Between them they give the same bytes for any `--jobs`, any
operating system and any numpy release in range. But they also make every cell
its own piece of scalar work: a sha256, a `SeedSequence`, a `PCG64`, then
decimal arithmetic. No two cells share any of it, so nothing can be computed
for a whole column at once, in numpy or anywhere else.

The goal is an order of magnitude on one core, before any parallelism, without
giving up any of those promises.

## The two paths

**A. Keep kernel v1 byte-identical and port the hot loop to Rust**, with PyO3,
maturin, rust-numpy and `rand_pcg`. Nothing moves: no digest, golden or evidence
pack changes, and there is one kernel. The cost is a port that must match the
Python kernel bit for bit, forever:

- numpy's `SeedSequence` has no Rust port. It is about 100 lines of 32-bit
  mixing in `numpy/random/bit_generator.pyx` (BSD-3-Clause), so the port is
  ours to write. The only port we found is the OCaml `bitgenerators` package.
- `rand_pcg`'s `Lcg128Xsl64` is numpy's PCG64. Its `new(state, stream)`, as its
  source reads, with numpy's four seed words as `state = w0·2⁶⁴ + w1` and
  `stream = w2·2⁶⁴ + w3`, gives numpy's state and first word. This was checked
  in Python against numpy for 10,000 seeds, without running `rand_pcg`.
- Every decimal operation on a digest path must give libmpdec's result.
  Correctly rounded functions agree with each other, but `rust_decimal` keeps a
  96-bit mantissa and does not reproduce Python's `Decimal` (rust-decimal issue
  471). The one libmpdec binding, the `mpdec` crate, had 496 downloads. The
  port would rest on it or on a decimal library of our own.
- The gain is bounded by what moves. Streams and decimal arithmetic are about
  45% of a run, so porting only them leaves the gain below 2 times. The 3 to 5
  times estimated for this path needs most of the row loop in Rust as well, and
  10 times needs nearly all of it: `generator/rows.py`, `generator/relations.py`,
  the families, the flux-expr/2 evaluator for derived columns and the
  rendering, each matching its Python twin byte for byte.
- The repository's proof-material guard (`make proof-guard`, a required check)
  refuses any `.rs` file, `Cargo.toml` or `rust-toolchain` file. It is a policy
  about proof-system backends, and a maintainer could scope it to them; as it
  stands, Rust code would live in a repository and distribution of its own.

**A without Rust** would keep v1's bytes and batch v1 itself in numpy. After the
hash, v1's seeding is integer arithmetic: `SeedSequence` is 32-bit mixing and
PCG64's seeding is two 128-bit steps, so both could run over a column of cells
at once. A transform could take a binary64 fast path and fall back to `decimal`
only when its result lies too close to a rounding boundary of the declared
scale to be sure, which is Ziv's strategy and how CRlibm rounds correctly. For a
coarse declared scale there is an exact shortcut: v1's map is monotone, so
bisecting it gives a table of the uniforms at which the rounded value changes,
and a draw becomes an integer search that returns v1's own bytes. The table
grows with the number of values a declaration holds, so it suits an INT or a
two-place column, not six places over a wide range. Path B could use such a
table too, for a draw that is integer-only and exactly monotone at a coarse
scale.

The per-cell sha256 does not rule this route out. With the front that every
label of a column shares hashed once and copied, a cell's hash costs
386 ns in measurement (a), about 1.7 µs for a row of the
related-star fixture, which reads 4.4 streams a row; ten times faster than
today leaves 15.9 µs a row. What weighs against it is the rest. Each Feistel
round of every key value is another sha256 (section 4.1), and so is every
identifier value, and every bounded draw is defined over a 64-word budget that a
batched twin has to honour, by stepping PCG64 or by its jump-ahead. Every v1 rule
gets a batched twin that must agree with the scalar one on every input, and
each fast path needs an error bound proven for every declared scale, not
measured. Path B gets array arithmetic everywhere from a smaller
specification.

**B. A second kernel, v2: counter-based streams and basic-operation
transforms.** Kernel v1 stays, and an old plan or evidence pack replays on it to
the same digests. Every v2 digest is new: a new KERNEL-SPEC version, new goldens
and GOLDEN-CHANGELOG rows.

- A stream is a pure function of a key and a counter. Each column's key is a
  sha256 of its path without the row, taken once per run. The counter holds the
  row, the draw's index within the cell and an attempt number. The function is
  Philox4x64-10 from Random123, which numpy ships as `numpy.random.Philox`, so
  there is no new dependency.
- Continuous draws use binary64 and only the operations IEEE 754 requires to be
  correctly rounded, in one fixed order: AS241 for Φ⁻¹, and fdlibm's method for
  the logarithm its tails need. Φ at a declaration's bounds is still worked out
  in `decimal`, once per declaration.

## Measurements

Both prototypes are in `tests/bench`, outside the engine, and run from the
repository root. Each times its variants in turn within a round and prints the
load average beside every timing, because other work shares this machine.
`tests/bench/test_kernel_prototypes.py` checks them and pins their output. It
is fast on purpose, so the pull-request matrix runs it on Linux (x86_64) under
Python 3.11, 3.12 and 3.13. The numbers below are from macOS on arm64, Python
3.13.14, numpy 2.4.6, 16 CPUs, on 2026-11-18.

### (a) Words for a million cells

```text
python -m tests.bench.kernel_streams --cells 1000000 --rounds 3
```

Two words for each of 1,000,000 cells of one column, three rounds, one-minute
load average between 3.8 and 4.6:

| Variant | Best ns per cell | Median | v1 / variant |
|---|---|---|---|
| v1 per cell: the engine's `CellStreams.stream_at` | 11,044.9 | 11,119.5 | 1.0 |
| v1 hash floor: the label's sha256, its shared front copied | 386.0 | 386.7 | 28.6 |
| v2 per cell: one numpy `Philox` object per cell | 8,193.5 | 8,206.2 | 1.3 |
| v2 column: one numpy `Philox` for the column | 14.4 | 14.8 | 768 |
| v2 any rows: Philox4x64-10 in numpy arrays, rows shuffled | 134.2 | 134.6 | 82 |

A `Philox` object per cell is barely faster than v1: the gain comes from asking
for a column at once, not from Philox itself. A column's words for a slice of
rows are one call, about 770 times cheaper than v1's per-cell streams.
The array version takes any rows in any order, which is what reading a parent's
columns at the rows its children picked would need, once a child's columns can
follow its parent row; it is about 80 times cheaper.

Before timing, the script checks that the array version gives the words numpy's
`Philox` gives for 200 random keys and counters, and the same words as the
column call for 10,000 rows, and that the hash floor gives v1's own stream
entropy. numpy steps its counter before making a block (`philox_next` in
`philox.h`), so `Philox(counter=c)` first returns the block of `c + 1`. The
specification would name the block's own counter, and the numpy route would
start one below it.

### (b) A truncated normal from basic operations

```text
python -m tests.bench.kernel_normal --draws 200000 --rounds 3
```

The prototype maps the same 53-bit uniform v1 draws through AS241 (PPND16)
with the coefficients and the order of CPython's pure-Python
`statistics.NormalDist.inv_cdf`. Its only operations are addition,
subtraction, multiplication, division and square root, plus the exact `frexp`.
The logarithm for the tails is fdlibm's `e_log.c` method on the same
operations. Φ at the two bounds comes from v1's own `normal_cdf` in `decimal`,
once per declaration, rounded to binary64, as are the mean and the standard
deviation.

Speed, best of three rounds, with the one-minute load average between
4.6 and 10.7:

| Interval, declared scale | v1 decimal | v2 plain Python | v2 numpy | v1 / numpy |
|---|---|---|---|---|
| [−8, 8], mean 0, sd 1, 6 places | 58,386 | 970 | 44.0 | 1,326 |
| [20, 80], mean 50, sd 10, 2 places | 57,719 | 976 | 43.7 | 1,320 |
| [3, 10], mean 0, sd 1, 6 places (mirrored) | 124,973 | 1,297 | 44.4 | 2,815 |
| [−12, −6], mean 0, sd 1, 6 places | 212,750 | 1,277 | 44.3 | 4,798 |
| [0, 5000], mean 1000, sd 300, INT | 58,911 | 978 | 43.6 | 1,352 |

Times are nanoseconds per draw. v1's tails cost more because `Decimal.ln` and a
longer error-function evaluation join in; the prototype's cost is the same
everywhere. Even one draw at a time in plain Python floats, with no numpy, is
59 to 167 times faster than v1.

Error against the value v1 computes before it rounds, over 200,113 inputs
per interval (200,000 random uniforms, the two ends and every power of two from
each end):

| Interval | Largest error, in grid steps | Another value at the declared scale | Another grid step |
|---|---|---|---|
| [−8, 8] | 1.7e-10 | 0 | 0 |
| [20, 80] | 1.1e-9 | 0 | 0 |
| [3, 10] | 2.0e-10 | 0 | 0 |
| [−12, −6] | 3.5e-10 | 0 | 0 |
| [0, 5000], INT | 3.5e-8 | 0 | 0 |

One step of the statistics grid is 2⁻¹⁶ (KERNEL-SPEC section 12), and the
largest error is at most 3.5e-8 of a step. Each of the sampled draws rounds to
the value v1 gives at the declared scale. That is a sample, not a guarantee: on
`[−8, 8]` at six places the largest error is about 2.6e-15, so a draw rounds
otherwise only when v1's value lies that close to a rounding boundary, about 5
draws in a billion. Matching v1 is not what v2 needs, since its digests are new
anyway; it shows the transform is as accurate as v1's wherever the declared
scale can tell.

The first version of the prototype mapped the upper half of an interval through
`1 − p`, as AS241 does inside. Near `u = 1` on `[−8, 8]` that subtraction kept
only a few digits of the far tail, and 21 of 2,113 draws rounded to another
value than v1's, the worst by 0.0118. The prototype now maps a draw in the
upper half through that tail's own probability, `Φ(−b) + (1 − u)·mass`, in
which `1 − u` is exact for every 53-bit uniform. A specification would have
to say the same.

**Order.** v1's draws are monotone in `u`, and a property test holds them to it
with eight draws spread across the unit interval (`tests/core/test_rng.py`).
Binary64 arithmetic is monotone only to within its rounding. Over 40,000
neighbouring uniform53 values around each of AS241's region edges, the switch
between the two tails and one plain central point, z steps backward between 0
and 3.3% of the time, by at most 3 units in the last place of z: 142 of 199,995
steps on `[−8, 8]`, 165 of 159,996 on `[20, 80]`, none of 39,999 on `[3, 10]`,
1,307 of 39,999 on `[−12, −6]` near the tail's `r = 5` edge, and 145 of
159,996 on the INT interval. Two values come out in the wrong order only when
their uniform53 inputs are at most one step apart, or three on `[−12, −6]`,
where neighbouring inputs move z by less than one unit in its last place. In 15
more windows, each centred on the rounding boundary of the declared scale
nearest one of those points, no step goes backward after rounding, and v1's
property test, with its draws far apart, would pass. These are measurements,
not a proof. v2 would promise order at the declared scale between uniform53
inputs more than three steps apart, the property test would state that, and the
specification would either prove the bound or test it over its vector set. The
threshold table above is exactly monotone where a declaration is coarse enough
for it.

**Why the bits are the same everywhere.** IEEE 754 requires addition,
subtraction, multiplication, division and square root to be correctly rounded,
so each has exactly one answer in binary64, whatever the CPU or the SIMD loop
numpy picks. `frexp` and multiplying by a power of two are exact. A numpy ufunc
does one operation, so no two are fused into a multiply-add, and the code fixes
the order. No libm function is called, so nothing depends on a platform's `exp`
or `log`; numpy 1.22 added AVX-512 loops from Intel's SVML for many
transcendental ufuncs on Linux, so which loop runs there depends on the CPU. On
this machine the numpy route and a plain-Python route through the same
operations agree on all 1,000,565 draws, bit for bit, and the logarithm is within
one unit in the last place of `math.log`.

Fusion is the trap. CPython's own C copy of AS241, behind
`statistics.NormalDist.inv_cdf`, was compiled here with each of its 42 Horner
steps fused into one multiply-add instruction, and it returns other last bits
than CPython's pure-Python copy of the same formula for 10,249 of 20,000
inputs. The prototype's order matches the pure-Python copy on all of them. The
specification has to forbid fusion, and a port in C or Rust has to keep it off.

No intermediate is ever subnormal: the prototype takes a tail mass below
binary64's smallest normal number as zero, and refuses an interval that holds
too little of its distribution, such as 40 to 41 standard deviations out, as the
specification would. So a process that a library built with `-ffast-math` has
switched to flush-to-zero still computes the same bits. Brendan Dolan-Gavitt
described PyPI packages that do that to every process importing them (2022).
numpy takes its subnormal constants from compiled tables, so it would not notice
such a switch made while running; kernel v2 would check in each worker, with
arithmetic, that half the smallest normal number is not zero.

The test pins a digest of the bits, and the Linux cells of the pull-request
matrix check it under the numpy each installs. numpy 1.26, the oldest release
the project supports, runs only in the nightly determinism matrix, which does
not run `tests/bench`; the specification's vector files would put the
transform there. CORE-MATH's correctly rounded `exp`, `log` and `erf` would also
give one answer everywhere, by definition, and are the route if a transform
ever needs them; numpy does not expose them, so they would be a compiled
dependency.

## Prior art

- **Random123.** Salmon, Moraes, Dror and Shaw, "Parallel random numbers: as
  easy as 1, 2, 3", SC11, doi:10.1145/2063384.2063405. Counter-based generators,
  Philox among them, that pass TestU01's BigCrush; known-answer vectors ship in
  its `tests/kat_vectors`. BSD-3-Clause.
- **numpy.** `numpy.random.Philox` is Philox4x64-10 with a 256-bit counter
  and a 128-bit key, and its counter steps once per four words. NEP 19 asks bit
  generators to keep their streams for a set of methods, and the compatibility
  page gives them "stronger guarantees" than `Generator`, whose streams may
  change in a feature release. Neither mentions `SeedSequence`, which kernel v1
  relies on to stay the same.
- **JAX.** Its PRNG design note (JEP 263) splits keys by hashing with
  Threefry, another counter-based function. JAX 0.5.0 (January 2025) turned
  `jax_threefry_partitionable` on by default, listed under breaking changes,
  with the flag left to select the old behaviour: a precedent for new values
  in a release with the old ones still reachable.
- **PyTorch.** Its CUDA generator hands each kernel launch a seed and an offset
  into a Philox stream (`PhiloxCudaState`, in
  `aten/src/ATen/cuda/detail/PhiloxCudaStateRaw.cuh`); under CUDA graph
  capture the offset is read through a pointer, so a replayed graph draws new
  numbers.
- **OpenRAND.** Khan, Palmer, Edelmaier and Aktulga, arXiv:2310.19925 (2023),
  MIT. Counter-based generators, Philox and Tyche among them, built from a seed
  and a counter per work item, such as a particle, for reproducible parallel
  results on CPUs and GPUs. **Squares** (Widynski, arXiv:2004.06278) claims
  more speed than Philox and has seen less use.
- **Normal quantiles.** Wichura, "Algorithm AS 241: the percentage points of
  the normal distribution", Applied Statistics 37(3), 477 to 484, 1988,
  doi:10.2307/2347330. CPython's `statistics.NormalDist.inv_cdf` implements it
  with `math.log` and `math.sqrt`. Acklam's approximation, which v1 refines
  with one Halley step, has a relative error below 1.15e-9.
- **Exact sampling.** Karney, "Sampling exactly from the normal
  distribution", ACM Transactions on Mathematical Software 42(1), article 3,
  2016, doi:10.1145/2710016, arXiv:1303.6257. It uses integer operations only
  and produces a deviate digit by digit, at a cost that grows with the
  precision. It is not chosen: it does not map one uniform to one value, so a
  truncation needs rejection, and its cost varies from draw to draw, which does
  not vectorize.
- **Correctly rounded functions.** CORE-MATH (Sibidanov, Zimmermann and
  Glondu, ARITH 2022; MIT) has correctly rounded binary64 `exp`, `log`, `erf`,
  `erfc` and more, and glibc 2.43 takes some of them. CRlibm (Daramy-Loirat and
  others, 2006; LGPL) came before it. fdlibm's `e_log.c` (Sun Microsystems,
  1993) states an error below one unit in the last place; its method is what
  the prototype's logarithm follows.
- **Byte-for-byte ports.** tpchgen-rs (Apache-2.0), a Rust port of TPC-H's
  `dbgen`, produces the same bytes as `dbgen`, checks that in CI, and reports
  being more than ten times faster than the next fastest generator. That is the
  gate any port in either path would need.
- **Decimal in Rust.** Python documents `Decimal.exp` and `Decimal.ln` as
  correctly rounded. rust-decimal issue 471 shows `rust_decimal` disagreeing
  with Python's `Decimal`; `mpdec` binds libmpdec but had 496 downloads; `dec`
  binds decNumber, a different implementation.

## Decision

Take path B, in this order:

1. **Specify kernel v2** as KERNEL-SPEC 0.2, `kernelVersion` 0.2.0 and
   `seedTreeVersion` `flux-seed/2`:
   - a stream key is the first 16 bytes of a sha256 over the root and the path
     without its ordinals. The 256-bit counter holds the path's ordinals (the
     row, or the subject and its occurrence), the draw's index within the cell
     and an attempt number, and each block gives four 64-bit words;
   - `uniform53`, `bernoulli` and `categorical` keep their integer rules. A
     bounded draw applies Lemire's test and, when a word is rejected, takes the
     next attempt of the same draw, so no other draw's words move and no fixed
     64-word budget is needed. That drops section 4's rule against rejection,
     which kept stream positions independent of the data, a property counters
     give by construction;
   - the continuous transforms are binary64 with basic operations only, in an
     order the specification writes out, never fused, with both tails through
     their own probability. That drops section 4's rule against float branch
     decisions too: a branch on a binary64 value is as reproducible as the
     value. A tail mass too small for binary64 becomes zero in `decimal` before
     it is converted, and an interval holding too little of its distribution
     for binary64 is refused (`FLX-5021`), so no intermediate is subnormal;
   - `exponential` is `−log(1 − u)/rate` with the same logarithm. `poisson`
     keeps v1's capped search, with its probabilities in binary64 from an
     exponential function built the same way (fdlibm's `e_exp.c` method), and
     needs a rule of its own for a mean above about 700, where `exp(−mean)`
     underflows. Neither is prototyped yet;
   - identifier values (`flux-gen/1`) come from Philox words keyed by `H` and
     the column's path, without the seed, so they stay seed-free and derived
     from `(H, ordinal)`, as the fidelity specification asks, without a sha256
     per row;
   - `permute` (section 4.1) takes a Philox block as its round function instead
     of a sha256, so a key's values come from array arithmetic too;
   - vector files pin it all: Random123's Philox known-answer vectors and the
     transforms' outputs on the prototype's inputs.
2. **Thread the kernel version through the stages.** Today `KERNEL_VERSION` is
   one constant in `stages/pipeline.py`, and the input digest, `apply`,
   `verify` and `ship` all stamp it. Each must take the version its plan or
   pack names instead, so a v1 plan recomputes the same `inputDigest` whatever
   the default is. This lands before anything else changes.
3. **Build kernel v2 beside v1.** v1's code, vectors and goldens stay as they
   are, and `fluxsim plan --kernel` picks the kernel of a new plan.
4. **Generate a column at a time under v2.** First measure it: one table of
   the related-star fixture, a column at a time through rendering, tests the
   projection below before the generator is rebuilt. Then draw a slice's words
   for a column in one call, compute nulls, values, keys and foreign-key picks
   as arrays, and keep per-row Python for what has to be per row: derived
   columns and the rendering of each line. Rendering a slice a column at a time
   comes next if the profile asks for it.
5. **No Rust now.** If profiling the column-at-a-time generator shows per-row
   rendering or derived columns dominating, a compiled accelerator can follow,
   in its own repository and distribution, checked byte for byte against the
   Python kernel on every change, as tpchgen-rs checks itself against `dbgen`.

## Consequences

- **Determinism contract.** The promises stay: same bytes for any `--jobs`,
  operating system, CPU and numpy release in range. What carries them changes.
  v1 relies on numpy keeping `SeedSequence` and PCG64's seeding exactly as they
  are; v2 relies on IEEE 754's basic operations and a Philox function the
  specification defines and its vectors pin, which the engine can compute
  without numpy's object at all (the array prototype does, at about
  135 ns a cell). The cross-operating-system and numpy matrix runs both
  kernels' digest sets.
- **Replay.** Once step 2 has threaded the version through the stages, an old
  bundle, plan or evidence pack replays on the kernel it names, to the same
  digests. An evidence pack's runtime statement already records the kernel
  version; offline verification of a pack re-runs nothing and does not change.
- **The accuracy program.** The goodness-of-fit battery
  (`tests/accuracy/gof`) and the PractRand lane test distributions and streams,
  so they run on both kernels; PractRand's first-words feeder, which pays one
  v1 stream derivation per word, becomes one call per column. The numerics
  tests that hold v1's decimal Φ and Φ⁻¹ to 1e-12 stay v1's; v2 gets its own,
  for the bound measured above, and its monotone test states the promise above.
  The determinism manifest gains v2 digests.
- **Related tables (KERNEL-SPEC section 8.2).** The design carries over: a
  value was already a function of the run, the expose, the row and the column,
  which is exactly what a counter-based stream is. Key layouts stay derived and
  seed-free, and `row_near` becomes `numpy.searchsorted`. A uniform foreign-key
  pick reads its null draw and its bounded draw from two draw indices of one
  counter, four words each, instead of 65 words of one stream.
- **Parallelism.** Every value is a pure function of a key and a counter, so
  any split of rows gives the same bytes without coordination. Column-at-a-time
  numpy releases the interpreter lock inside its loops, so threads become worth
  measuring against the process pool, which spends user time on start-up and
  on pickling the plan (37 s to 55 s at eight jobs). numpy supports
  free-threaded CPython from 2.1, which removes the lock altogether.
- **Where Rust fits.** Under B, Rust is an accelerator for what stays per row,
  not a second home for the kernel's arithmetic. v2's specification is
  integers and IEEE operations, so a port is small to check, and it must keep
  fusion off (no `mul_add`, no fast-math) so its arithmetic matches Python's
  operation for operation.
- **Migration.** After step 2, v2 ships behind `--kernel 0.2.0`, with its
  goldens and GOLDEN-CHANGELOG rows; then a minor release makes it the default,
  and its release notes say that every new plan's digests change. v1 stays for
  replay and gets no new behaviour. If the FLUX specification pins either
  version string, it changes there first.
- **Projection.** In a plain one-job run of the related-star fixture with its
  fact tables scaled to 216,065 rows (load average 13), rendering, framing and
  the digest take 9.3 µs a row, 3.5 to 3.8% of the run, and generation nearly
  all the rest. (cProfile puts rendering at 8.5%, because it inflates code that
  makes many small calls.) Streams and decimal arithmetic, about half of
  generation, are what the prototypes measured, and they drop by 750 times and
  more. The other half is per-row Python: dispatch, null draws, relation
  lookups and permutation. That it too can be done a column at a time is an
  assumption, not a measurement, and the same restructuring would serve A
  without Rust as well. Taking rendering at 3.7%, and the 2.1% that cProfile
  puts outside both generation and the writer as unchanged: if that half only
  halves, the run gets about 3.4 times faster; if it drops ten times, about 9.5
  times; twenty times, about 12; and with nothing left of it, about 17. The
  fixture has no derived columns. Step 4's first measurement tests this before
  anything is rebuilt. Past it, rendering a slice's lines a column at a time,
  with section 9's bytes, is the next step and the first place a compiled
  accelerator would pay.

## What would change this decision

- **A need for v1's bytes from new runs that must also be fast.** v1 stays
  available, so needing its bytes alone does not change the route; needing them
  at v2's speed would point back to A, or to A without Rust.
- **A without Rust coming close.** A measured prototype of it within about 1.5
  times of B, with a fast-path error bound that can be proven, would point to
  it: it keeps every digest, and no user has to move.
- **A platform that computes a different bit.** If the pinned transform digest
  fails on an operating system, CPU or numpy release the engine supports, the
  claim above is wrong. The fallback is fixed-point integer arithmetic for the
  transforms, still array arithmetic, or CORE-MATH as a compiled dependency.
- **Per-row work that stays above a tenth of today's.** If step 4's measurement
  finds the rest of generation, rendering and derived columns together above a
  tenth of today's cost per row, ten times is out of reach without compiled
  code, and the comparison with A without Rust has to be made again on that
  measurement.
- **A maintained Rust `SeedSequence` and a decimal crate that matches
  libmpdec.** Those would make A cheaper, though it would still need the row
  loop in Rust.
- **numpy changing `Philox`.** numpy promises its bit generators more stability
  than `Generator`, but it is numpy's promise. If `Philox`'s stream ever moved,
  v2 would compute the words itself, as the array prototype does, since the
  specification defines the function rather than numpy's object.

## Amendment of 2026-11-18: the specification

Step 1 is [KERNEL-SPEC-2](../spec/KERNEL-SPEC-2.md), with two vector files that
`tests/kernel2/reference.py` generates. Writing it settled one point
differently from the Decision above.

`poisson` does not move to binary64. Its draw depends on its declaration's mean
only through the search's cumulative sums, so those sums are worked out once per
declaration in `decimal`, exactly as kernel v1 works them out, and become integer
thresholds `ceil(c_k * 2^53)`. A draw is then an integer search of its uniform
over them. That needs no binary64 exponential function and no rule of its own
for a mean above about 700: a large mean costs a longer table, built once, where
v1's search spends that time on every draw. Kernel v1 rounds its uniform to 50
digits before it compares, so the two could differ only for a uniform within
about `10^-50` of a threshold, and none of the vectors' thresholds has one. The
exponential and the truncated normal stay in binary64 as decided.

Three more points the specification settles. Its labels are `flux-seed/2`,
`flux-gen/2`, `flux-key/2` and `flux-permute/2`, so no v2 hash input can equal a
v1 one; the Decision's `flux-gen/1` named the identifiers' family as v1 calls it.
Subject and event ids keep v1's single sha256, on the v2 root: they are one per
subject or event, so batching them would change nothing. And kernel v2 refuses
what binary64 cannot hold, which is stricter than v1: a bound beyond `2^52`
units, a scale above 22, or a standard deviation, a rate or a mean other than
zero whose magnitude lies outside `[2^-900, 2^900]`.

## Amendment of 2026-11-18: steps 4 and 5, measured

Step 4 is built. Under kernel `0.2.0` the generator computes a slice of rows a
column at a time (`flux_engine/generator/columns.py`):

- A column's draws for every row of a slice are one Philox call over the rows'
  counters. `flux_engine.core.kernel2` has an array twin of each scalar function
  the generator calls: `uniform53`, the bounded draw with its 64 attempts as
  masks, `bernoulli`, `categorical`, the Poisson table's search, the binary64
  truncated normal and exponential, generated identifiers and `permute/2`.
  `tests/kernel2/test_columns.py` holds each twin to its scalar function, bit
  for bit.
- A plan the contract relates first works out, for the whole slice, every
  identifier and composite key, every foreign key's null and pick, and the
  parent rows those name, through every view below them
  (`RelationReader.prefetch`). Its columns are then read from what it worked
  out. `row_near` stays a scalar search, because its arithmetic is exact on
  integers that can pass `2^63`.
- What stays per row: a plan with a column drawn `given` another, a key too
  wide for an int64 array or an array permutation, the rules, a contract's and
  a bundle's FLUX-side ones, which set their columns on each row after it is
  drawn (ADR 0011), and rendering. A column no array function covers takes the
  per-row code cell by cell. A slice in which anything is refused or raised
  goes to the per-row path whole, so what it raises is what it always raised,
  at the row and column it always raised it for.

The per-row path stays the oracle. The `related-star-v2` golden and the
manifest's `run2` digests were made by it in step 3, and they do not move.
`tests/generator/test_columns.py` generates every family, the related star,
a foreign key to its own table, composite and identifier keys too wide for the
arrays, slices across chunk boundaries and refused declarations both ways, and
compares the rows by their `repr`, so another value class or another decimal
exponent fails as a changed byte would. Of the mutants of the new code that the
author and a review tried, every one that changes an output fails a test.

### What it measured

`fluxsim plan` and then `fluxsim apply --run-id` on one job, on the
related-star fixture with its fact tables scaled to 216,065 rows, macOS on
arm64, Python 3.13.14, numpy 2.4.6, in two rounds that alternate the kernels.
Other work kept the load average between 130 and 190 on 16 CPUs, so the measure
is CPU time:

| CPU seconds | kernel `0.1.0` | kernel `0.2.0` | faster |
| --- | --- | --- | --- |
| round 1, plan and apply | 104.9 | 9.0 | 11.7 times |
| round 2, plan and apply | 97.8 | 9.5 | 10.3 times |

Planning and applying each take about the same share: 48.2 to 53.7 s under
kernel `0.1.0` against 4.5 to 4.8 s under kernel `0.2.0`. Wall time gave 13.2
and 10.2 times.

Timing `generate_rows` and the line rendering alone, table by table, gave
246.6 µs a row of CPU time under kernel `0.1.0`, 151.8
under kernel `0.2.0` without the column path, and 17.9 with it (load average
110 to 147). On `store_sales`, the 200,000-row fact table, generation is now
9.6 µs a row and rendering 8.5. Rendering is about as large as generation,
where under kernel `0.1.0` it was about a thirtieth of it. What is left of
generation is Python around the array calls: reading composite key members and
parent values, building decimals from grid units, and the nearest-key search of
a declared foreign key.

### Step 5, decided

No compiled accelerator. Step 5 named the condition for one: per-row rendering
or derived columns dominating the column-at-a-time generator. Rendering has
come close, but it is decimal formatting and canonical JSON in Python, and the
step this ADR names first for it is rendering a slice a column at a time with
section 9's bytes, in Python. What is left of generation is Python around array
calls, not arithmetic a compiled kernel would speed up. Ten times kernel
`0.1.0` is reached without compiled code, so "What would change this decision"
above has not been met on either side.

A compiled accelerator is reconsidered only if a profile taken after
column-at-a-time rendering still shows rendering or derived columns above half
of a run.

### The default kernel, decided by the maintainers, 2026-11-18

Kernel `0.1.0` stays the default for the coming release. Kernel `0.2.0` becomes
the default in a minor release of its own after it, as Consequences above lays
out under Migration.

The alternatives were to switch the default in the same release, or to leave
kernel `0.2.0` opt-in with no date. Switching in the same release would move
every new plan's digests in the release that first ships kernel `0.2.0`, so no
user could try `--kernel 0.2.0` against the old default before the default
moved, and one set of release notes would have to announce both. Leaving it
opt-in with no date keeps its speed from everyone who does not read this ADR. A
release of its own for the switch keeps one change of default per release,
gives `--kernel 0.2.0` a release to be tried first, and lets its notes say
plainly that every new plan's digests change. Either way an existing plan, pack
or bundle replays on the kernel it names, to the same digests.

## Amendment of 2026-11-21: kernel 0.2.0 is the default from 0.9.0

The maintainers decided that kernel `0.2.0` is the default for new plans in 0.9.0, the minor release after
the one that shipped it (0.8.0), as "The default kernel, decided by the maintainers, 2026-11-18" above
asked for. A plan made with no `--kernel` now names `0.2.0`, and `fluxsim plan --kernel 0.1.0` plans on the
first kernel as before.

### What moves

Every new plan's digests. `kernelVersion` and `seedTreeVersion` are inside `inputDigest`
(KERNEL-SPEC §11), so a new plan's `inputDigest`, `planDigest`, run id and `runDigest` differ from what the
same bundle gave under `0.1.0`, and so does every stream, because the seed tree is different. The goldens
of the sets that plan with the default move to kernel `0.2.0`, each with its GOLDEN-CHANGELOG row. A set
that exists to hold kernel `0.1.0`'s digests (the related-tables set, the determinism manifest's first run,
and the sets of the public examples) plans with `--kernel 0.1.0` explicitly, so it holds the digests it held
and keeps proving that an old plan replays to them.

### What does not move

- **A recorded plan** keeps its kernel. `apply`, `replay`, `verify` and `ship` read the kernel from the
  plan they are given (`kernel_named`), never from the default, so a plan, a run directory or an evidence
  pack that names `0.1.0` replays on kernel `0.1.0` to the same digests. A plan written by 0.8.0 replays
  to the digests 0.8.0 gave it.
- **Kernel `0.1.0`** stays implemented, specified by KERNEL-SPEC.md, and selectable. It gets no new
  behaviour, as Migration above says.
- **A bundle that declares no module** keeps its bytes under a given kernel. The default is the only thing
  that changed, and the change is in the plan, not in the bundle.

### Why in this release, and the one change of default

The release that shipped kernel `0.2.0` kept `0.1.0` as the default, so `--kernel 0.2.0` had a release to be
tried first. 0.9.0 is the release of its own for the switch: it holds no other change of default, and its
notes say that every new plan's digests change. A seed path that only a new opt-in declaration reads is
additive and needs no kernel bump (KERNEL-SPEC-2 section 12), so features that arrive after this one do not
move the default again.

## Amendment of 2026-11-21: step 5, judged on the 1,000,000-row profile

The amendment of 2026-11-18 left a compiled accelerator open on one condition:
a profile taken after column-at-a-time rendering that still showed rendering or
derived columns above half of a run. Column-at-a-time rendering is in the engine
(`flux_engine.seam.emit.frame_rows`), a spike has built the line renderer an
accelerator would have started with, and the profile has been taken. The
maintainers judged it: the engine does not adopt the Rust line renderer.

### What was measured

The related-star fixture scaled 200 times (1,000,365 rows), on kernel `0.2.0`,
in one process, on macOS on arm64 with Python 3.13.14 and numpy 2.4.6, at a load
average of 7 to 30.

- **Rendering is 12.4 % of a run.** It takes 1.17 µs of the 9.42 µs a row of a
  whole run, and between 11.6 and 13.7 % from run to run. The condition was half.
- **The Rust renderer is slower than the engine's once it is handed the
  engine's rows.** The spike is an extension module of its own (PyO3, built with
  maturin, in a repository of its own) that renders a slice's lines from
  columns. On the five emits of this world it took 0.118 µs a row. The engine
  holds a slice as row tuples, so the spike needs an adapter that turns them
  into the columns it reads. With the adapter it took 1.788 µs a row, against
  1.218 µs a row for the engine's renderer, which includes the sha256 of each
  line: 1.5 times slower.
- **Its bytes are the engine's.** Its lines and their digests were identical to
  the engine's on all five emits, so the judgement is about cost and not about
  correctness.

### Judgement

1. **The condition is not met.** Rendering is 12.4 % of a run, about a quarter
   of the half that the amendment of 2026-11-18 asked for. A renderer that cost
   nothing would take 12.4 % off a run.
2. **Fed the engine's rows, the Rust renderer costs more than the one it would
   replace.** The renderer itself is fast. What costs is the hand-over from row
   tuples to columns, and the engine has no columns to hand over: it holds rows.
3. **It is revisited only if the engine comes to hold columns as arrays.** Then
   nothing has to turn rows into columns, and the question is put again against
   the profile of that engine.

What this does not change: the engine renders as it did, its bytes and every
digest are as they were, and the rest of step 5 stands. An accelerator, if one
is ever adopted, lives in a repository and a distribution of its own and is
checked byte for byte against the Python kernel on every change.
