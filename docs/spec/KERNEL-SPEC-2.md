# FLUX Simulation Engine: kernel v2 normative specification (v0.2)

Status: normative for kernel `0.2.0` and seed tree `flux-seed/2`, decided in
[ADR 0010](../adr/0010-faster-generation-through-a-second-kernel.md). The engine implements it beside kernel `0.1.0`:
`fluxsim plan --kernel 0.2.0` plans a run under it.
Kernel `0.1.0` stays specified by [KERNEL-SPEC.md](./KERNEL-SPEC.md) and stays selectable: a plan or an evidence
pack that names `0.1.0` runs under it, to the same digests.

This document states what kernel `0.2.0` changes. Every rule of KERNEL-SPEC.md that it does not replace holds for
`0.2.0` as written; "KERNEL-SPEC §n" below means that document's section. Two vector files pin every rule here:
`tests/vectors/kernel2-streams-vectors.json` (the integer side) and `tests/vectors/kernel2-transforms-vectors.json`
(the binary64 side). `tests/kernel2/reference.py` computes them, and `python -m tests.kernel2.test_kernel2_vectors`
regenerates them. An implementation is held to the vector files, not to the reference.

What v2 changes, in one paragraph: a stream is no longer a numpy generator built per path. It is Philox4x64-10, a
pure function of a key and a counter: the key comes from the path with its ordinals left out, and the counter holds
the ordinals, the draw's index and an attempt. So the words of every row of a column come from one call, and any row
from any process. Continuous draws are binary64 arithmetic in a fixed order instead of `decimal`, and keys and
generated identifiers come from Philox instead of a sha256 per value. Integer rules that v1 already states exactly
(`bernoulli`, `categorical`, the LRM) are unchanged, applied to v2's words, and `poisson` keeps v1's cumulative sums.

---

## 1. Versions

| Name | Kernel `0.1.0` | Kernel `0.2.0` |
|---|---|---|
| `kernelVersion` | `0.1.0` | `0.2.0` |
| `seedTreeVersion` | `flux-seed/1` | `flux-seed/2` |
| `traceVersion`, `runFormatVersion` | unchanged | unchanged |

`kernelVersion` is in `inputDigest` (KERNEL-SPEC §11), so a plan is bound to its kernel. A new plan picks its kernel
with `fluxsim plan --kernel`, and every stage runs and stamps the kernel of the plan it reads (KERNEL-SPEC §1).

## 2. The root, a path's key and a draw's counter (replaces the derivation in KERNEL-SPEC §3)

The canonical path table of KERNEL-SPEC §3, the generator's cell path (SEAM-FIDELITY-SPEC section 1), the rule that
a stream is addressed by its path and never by spawn order, `seed-paths.ndjson` and `seedTreeDigest` all hold. A path
is materialised when any block of it is read.

```text
root_label    = b"flux-seed/2" + NUL + H_bytes + NUL + effective_seed.to_bytes(8, "big", signed=True)
root          = sha256(root_label)
key_label     = label(path) of KERNEL-SPEC §3, with every ordinal part replaced by the empty string
key(path)     = sha256(b"flux-seed/2" + NUL + root + NUL + key_label)[0:16]
              = (k0, k1), bytes 0..7 and 8..15, each big-endian
counter(path, draw, attempt) = (o1, o2, draw, attempt)
block         = Philox4x64-10(counter, key) = (w0, w1, w2, w3)
```

- **Ordinal parts** are the parts the path's shape marks as ordinals: the subject ordinal, an occurrence, a step, a
  call ordinal, a run index, a row. `o1` and `o2` are the path's ordinals in order, `0` where it has fewer than two;
  no canonical shape has more. An ordinal, a draw index or an attempt that does not fit in 64 bits is refused
  (`FLX-2202`).
- **One key per shape and names.** No name part is empty, and an empty part marks an ordinal, so a key label tells
  where its ordinals were. The canonical shapes differ from each other in their length or in a literal part, so two
  paths share a key label only when they have the same shape and the same names, and then their counters differ in
  their ordinals. A shape added to the table must keep that property; `tests/kernel2` checks it for every shape.
- **Draw indices.** Each call a consumer makes to a sampler of section 4 or 5 (`uniform53`, `bernoulli`,
  `categorical`, `bounded`, `truncated_normal`, `exponential`, `poisson`) takes the next index of its path,
  `0, 1, 2, ...`, in the order kernel `0.1.0`'s consumer makes those calls; never one index per word. A `permutation`
  of `n` takes `n - 1` indices, from `first`, the number of calls made on the path before it. The index a draw takes
  never depends on a value drawn before it: a generator cell's value draw keeps its index whether or not the cell is
  null. For example, a generator cell makes its null draw first when its column declares a `nullFraction`, then its
  value, and a histogram value is a `categorical` draw then a `bounded` one (`generator/rows.py`). A signal's first
  occurrence draws its count, then its start offset when the window is longer than zero, then its payload fields in
  NFC order; each later occurrence draws its gap, then its payload (`runtime/signals.py`). The consumers' code is
  where that order is written; kernel `0.2.0` keeps it.
- **Dependency paths.** The path of a contract dependency's draw (EXPR-SPEC-2 section 8), `("sim", sim_id,
  "dependency", R, "key", K, column)`, has no ordinal part, so its key is the whole path and its counter's two
  ordinals are zero; it is derived with this section's rules and, like the generator's cell path, stays out of the
  seed-path ledger. The path of one row's NULL draws for a dependency that draws them per row, `("sim", sim_id,
  "dependency", R, "row", rowIndex)`, has one ordinal, the row index: its key leaves the index out and the
  counter carries it, as for the cell path. It stays out of the ledger too.
- `Experiment.spec.assignment.seed` roots the experiment's path as KERNEL-SPEC §3 says, with `flux-seed/2` in place
  of `flux-seed/1`.

## 3. Philox4x64-10

Random123's function (Salmon, Moraes, Dror and Shaw, "Parallel random numbers: as easy as 1, 2, 3", SC11).

```text
M0 = 0xD2E7470EE14C6C93   M1 = 0xCA5A826395121157      multipliers
W0 = 0x9E3779B97F4A7C15   W1 = 0xBB67AE8584CAA73B      key increments
mulhilo(a, b)  = the high and the low 64-bit word of the 128-bit product a * b
round(c, k):   (hi0, lo0) = mulhilo(M0, c0)
               (hi1, lo1) = mulhilo(M1, c2)
               c = (hi1 XOR c1 XOR k0, lo1, hi0 XOR c3 XOR k1, lo0)
Philox4x64-10(c, k): ten rounds; before each round but the first, k0 = k0 + W0, k1 = k1 + W1 (mod 2^64)
```

Counter and key word 0 is the one Random123 and numpy call `v[0]`. The streams vector file holds Random123's three
published known-answer vectors. `numpy.random.Philox` computes the same function, with one difference of position: it
steps its counter before it makes a block, so `Philox(counter=c)` first returns the block of `c + 1`. Its counter
carries from word 0 into word 1, so blocks of consecutive rows are consecutive, and a column's draw `d` at attempt 0
for a slice of rows is one call.

## 4. Integer draws (replaces the draws of KERNEL-SPEC §4)

- `uniform53 = w0 >> 11`, from the draw's block at attempt 0.
- `bernoulli(p)` and `categorical(levels)`: KERNEL-SPEC §4's rules, unchanged, on that `uniform53`.
- `bounded(n)`, for `1 <= n <= 2^64`: for `attempt = 0, 1, ..., 63`, take `w0` of the draw's block at that attempt and
  `m = w0 * n` (128 bits); the first attempt with `m mod 2^64 >= 2^64 mod n` gives `m >> 64` (Lemire's test,
  "Fast random integer generation in an interval", ACM TOMACS 29(1), 2019). If all 64 attempts are rejected, the
  result is the last `w0 mod n`, KERNEL-SPEC §4's documented fallback. This replaces the fixed 64-word budget: each
  attempt has a counter of its own, so no other draw's words depend on how many attempts were needed.
- `permutation(n)`: KERNEL-SPEC §4's Fisher-Yates (`i` from `n - 1` down to `1`, `j = bounded(i + 1)`), its `k`-th
  swap drawn at index `first + k`.
- `poisson(mean)`, for `mean >= 0` (`FLX-5021` otherwise): `0` for a mean of zero. Otherwise, once per
  declaration, in `decimal` at 50 digits rounding half-even, kernel `0.1.0`'s sums:
  `term_0 = c_0 = exp(-mean)`, then for `k = 1 .. cap - 1`, `term_k = (term_{k-1} * mean) / k` and
  `c_k = c_{k-1} + term_k`, where `cap = floor(mean + 10 * sqrt(mean) + 10)` (KERNEL-SPEC §4's cap). The thresholds
  are `t_k = ceil(c_k * 2^53)`, the product taken exactly (70 digits hold it). The draw is the smallest `k` with
  `uniform53 < t_k`, or `cap` when there is none: an integer search, so it vectorizes. Kernel `0.1.0`'s search
  compares the same sums with its uniform rounded to 50 digits, so the two could differ only for a uniform within
  about `10^-50` of a threshold; the vectors find none at any threshold of the means they test.

KERNEL-SPEC §4's "never" list holds, and grows: no libm or numpy transcendental function (`exp`, `log`, `erf`,
`np.exp`, `np.log`, ...) on a digest path, binary64 ones included. Two of §4's rules give way. Rejection is allowed
in the one form above, on the draw's own attempts. A branch on a binary64 value is allowed inside section 5's
transforms, whose values are reproducible bit for bit.

## 5. Continuous draws in binary64 (replaces `truncated_normal` and `exponential` in KERNEL-SPEC §4)

### 5.1 The arithmetic

IEEE 754 binary64 with rounding to nearest, ties to even. Every operation is one correctly rounded IEEE operation
(addition, subtraction, multiplication, division, square root, comparison, an exact `frexp`, an exact conversion),
taken in the order written. Two operations are never fused into a multiply-add, operations are never reassociated,
and no intermediate is held at another precision. A `decimal` value becomes binary64 by rounding to nearest, ties to
even. These rules are what make the bits the same on every operating system, CPU and numpy release; a compiler that
fuses multiplies and adds changes them, and CPython's own C copy of AS241 is compiled that way (ADR 0010).

No intermediate may be subnormal. The bounds of 5.3 and 5.4 make sure none is, and a process whose floating-point
environment flushes subnormal results to zero is refused before it computes a draw (`FLX-5050`, registered with the
implementation): half of `2^-1022`, computed while the process runs (never a constant a compiler can fold), must not
be zero, both in plain binary64 arithmetic and on the array path that computes draws (numpy's, or a SIMD unit's).
Every process checks, workers included. The check catches flush-to-zero; a mode that reads subnormal inputs as zero
changes nothing once no intermediate is subnormal.

### 5.2 The logarithm and Φ⁻¹

The logarithm of a positive normal binary64 `x`, by the method of fdlibm's `e_log.c` (Sun Microsystems, 1993):

```text
(m, e) = frexp(x)                          x = m * 2^e, 0.5 <= m < 1
if m < SQRT_HALF: m = m * 2, e = e - 1     SQRT_HALF = 0x3fe6a09e667f3bcd, the binary64 nearest sqrt(1/2)
k = e as binary64
f = m - 1
s = f / (2 + f)
z = s * s
w = z * z
t1 = w * (Lg2 + w * (Lg4 + w * Lg6))
t2 = z * (Lg1 + w * (Lg3 + w * (Lg5 + w * Lg7)))
r = t2 + t1
hfsq = (0.5 * f) * f
log(x) = k * ln2_hi - ((hfsq - (s * (hfsq + r) + k * ln2_lo)) - f)
```

`ln2_hi`, `ln2_lo` and `Lg1` to `Lg7` are fdlibm's constants, by bit pattern in the transforms vector file.

Φ⁻¹ of `0 < p < 1` is Wichura's AS241, PPND16 (Applied Statistics 37(3), 477 to 484, 1988), in the order of
operations of CPython's pure-Python `statistics.NormalDist.inv_cdf`:

```text
q = p - 0.5
if |q| <= 0.425:   r = 0.180625 - q * q;   return (A(r) * q) / B(r)
t = sqrt(-log(p if q <= 0 else 1 - p))
if t <= 5:         t = t - 1.6;   x = C(t) / D(t)
else:              t = t - 5;     x = E(t) / F(t)
return -x if q < 0 else x
```

Each of `A` to `F` is a polynomial of degree 7, evaluated as `((c7 * x + c6) * x + ...) * x + c0`, its coefficients
highest power first by bit pattern in the transforms vector file (`B`, `D` and `F` end in `1.0`).

### 5.3 The truncated normal

KERNEL-SPEC §4's refusals come first and hold (`FLX-5021`): a negative `stddev`, `lo` above `hi`, an interval that
holds no value at its scale. A `stddev` of zero keeps KERNEL-SPEC §4's rule, the mean quantised and clamped, and
none of what follows applies to it. For `stddev > 0`, once per declaration, in `decimal` at 50 digits rounding
half-even:

```text
a = (lo - mean) / stddev,  b = (hi - mean) / stddev
mirror = a > 0;  if mirror: (a, b) = (-b, -a)
cdfA = Φ(a),  sfB = Φ(-b),  mass = Φ(b) - Φ(a)
loUnits = ceil(lo * 10^scale),  hiUnits = floor(hi * 10^scale)
```

Φ is kernel `0.1.0`'s `normal_cdf` (`flux_engine/core/rng.py`) at 50 digits: through the complementary error
function of a non-negative argument, by the all-positive series of `erf` up to 4 and the continued fraction of `erfc`,
200 levels deep, above it. That function is normative for Φ here, and the transforms vector file pins its binary64
rounding for every declaration there.

Kernel `0.2.0` refuses (`FLX-5021`) what binary64 cannot hold, which is stricter than kernel `0.1.0`:

- an interval with `0 < mass < 2^-969`, which holds too little of its distribution;
- `|loUnits|` or `|hiUnits|` above `2^52` (at six places, a bound beyond about `4.5e9`);
- a `scale` above 22, beyond which `10^scale` is not exact in binary64;
- a `stddev` whose binary64 value lies outside `[2^-900, 2^900]`, or a `mean` that is not zero and whose magnitude
  lies outside it.

A `cdfA` or `sfB` below `2^-1022` is taken as zero. Then `cdfA`, `sfB`, `mass`, `mean`, `stddev` and `10^scale` are
rounded to binary64 once. With these bounds every intermediate below is zero or a normal number.

Each draw, from its `uniform53`:

```text
u = uniform53 * 2^-53                                    exact
if mirror: u = 1 - u                                     exact
if mass <= 0:                       the draw is the LOWER END
lower = cdfA + u * mass
if lower <= 0.5:
    if lower <= 0:                  the draw is the LOWER END
    z = Φ⁻¹(lower)
else:
    upper = sfB + (1 - u) * mass    never zero, see below
    z = -Φ⁻¹(upper)
if mirror: z = -z
value = mean + stddev * z
units = clamp(roundTiesToEven(value * 10^scale), loUnits, hiUnits)
```

The lower end is `loUnits` and the upper end `hiUnits`, the other way round when `mirror`: an end is taken whole,
with no binary64 arithmetic, as kernel `0.1.0` takes a bound. `lower` is zero only when `cdfA` is taken as zero,
`uniform53` is 0 and the plan is not mirrored. `upper` is never zero: unmirrored, `(1 - u) * mass` is at least
`2^-53 * mass`, and mirrored, `sfB = Φ(-b)` is above one half. The vectors record a `z` for every draw; at an end it
is that end's standardised bound in binary64, negated when mirrored, for information only. The draw is `units * 10^-scale` as a decimal at `scale`;
an INT column takes `units`. A draw in the upper half of the
interval goes through that tail's own probability, never through one minus a number close to one, which in binary64
would lose most of a far tail's digits. An implementation may compute `upper` on every draw and choose afterwards;
the result is the same.

What ADR 0010 measured, informative: against KERNEL-SPEC §4's decimal value before rounding, the largest error is
3.5e-8 of a step of the 2^-16 statistics grid (KERNEL-SPEC §12), and every sampled draw rounds to §4's value at the
declared scale. The order promise, which the property test states: at the declared scale, a larger `uniform53` never
gives a smaller value when the two inputs are more than three apart.

### 5.4 The exponential

```text
x = -log(1 - u) / rate           u = uniform53 * 2^-53, 1 - u exact, log as 5.2
units = roundTiesToEven(x * 10^scale)
```

`rate` is rounded to binary64 once per declaration. A `rate <= 0` is refused (KERNEL-SPEC §4), and, stricter than
kernel `0.1.0`, so are (`FLX-5021`): a `scale` above 22; a rate whose binary64 value lies outside `[2^-900, 2^900]`;
and a rate for which `(37 / rate) * 10^scale`, computed in binary64, exceeds `2^52`, since `-log(2^-53)` is below 37
and so no draw's units then exceed it.

## 6. Identifiers (replaces kernel `0.1.0`'s derivations of them)

### 6.1 Generated identifiers

SEAM-FIDELITY-SPEC section 1 asks that an identifier column be derived from `(H, ordinal)`. Kernel `0.1.0` derives it
as a sha256 per value (`flux-gen/1`, in `generator/rows.py`); kernel `0.2.0` derives it from Philox:

```text
key    = sha256(b"flux-gen/2" + NUL + H_bytes + NUL + key_label(cell path))[0:16]
digits = the four words of Philox4x64-10((row, 0, 0, 0), key), each as 16 lowercase hex digits, w0 first
```

The identifier is `digits[0:length]`, `1 <= length <= 64`. A UUID takes `d = digits[0:32]` laid out as kernel `0.1.0`
lays it out, an RFC 9562 version 8 UUID: `d[12]` becomes `8`, `d[16]` becomes the hex digit of `(d[16] AND 3) OR 8`,
and the 32 digits are written `8-4-4-4-12` with hyphens. The key
reads `H` and the column's path, never the seed, so an identifier is derived from `(H, ordinal)` as the fidelity
specification requires. For a fixed key, Philox is a bijection of the counter, so the rows of one column get distinct
64-digit identifiers.

### 6.2 Subject and event identifiers

A subject's id and an event's id come from the seed tree (`derive_identifier` in `core/seedtree.py`), not from a
column. Kernel `0.2.0` keeps kernel `0.1.0`'s rule for them, on the `0.2.0` root:

```text
id = hex(sha256(b"flux-seed/2" + NUL + b"id" + NUL + root + NUL + label(path)))[0:length]
```

`label(path)` is KERNEL-SPEC §3's full label, ordinals included, and the path need not be canonical (a subject's id
path, `("world", world_id, "subject", ordinal, "id")`, is not). One sha256 per subject or per event is not a cost that
batching would change.

## 7. `permute/2` (replaces the round function of KERNEL-SPEC §4.1)

The split, the four alternating Feistel rounds and cycle walking of KERNEL-SPEC §4.1 hold. Only the round function
changes:

```text
roundKey = sha256(b"flux-permute/2" + NUL + key + NUL + domain.to_bytes(33, "big"))[0:16]
F(i, y)  = w0 + w1 * 2^64 of Philox4x64-10((y mod 2^64, y >> 64, i, 0), roundKey)
```

A half of a domain of at most `2^256` holds at most 128 bits, so `y` fits the counter's first two words.

## 8. Related tables under `0.2.0` (KERNEL-SPEC §8.2)

KERNEL-SPEC §8.2 holds, with three substitutions:

- `k = sha256(b"flux-key/2" + NUL + H_bytes + NUL + label(...))`, the same label, in place of `flux-key/1`;
- `permute/2` (section 7) in place of `permute`;
- a `uniform` pick is section 4's `bounded(parentRows)` on the first `fromColumn`'s cell: draw index 1 when that
  column declares a `nullFraction`, whose null draw is index 0, and index 0 when it declares none.

## 9. What stays as KERNEL-SPEC states it

Canonicalisation and digests (§2), the path table and `seedTreeDigest` (§3), the Largest Remainder Method (§5),
temperature dials (§6, their `bernoulli` as section 4 above), the clock and scheduler (§7), the emit stream and
`outputDigest` (§8), keys from the contract (§8.1), value rendering (§9: every draw reaches a row as a decimal or an
integer, never as binary64), the trace and the run digest (§10), `inputDigest` and `planDigest` (§11), fixed-point
statistics (§12) and the error-code ranges (§13).

## 10. The broken kernels (KERNEL-SPEC §14 under `0.2.0`)

The six of §14 hold, and three more must fail the property suite: a kernel that gives two draws of a path one
counter; a transform that fuses a multiply and an add; a key label that keeps a path's ordinals, which gives every row
of a column a key of its own.

## 11. The vector files

| File | What it pins |
|---|---|
| `tests/vectors/kernel2-streams-vectors.json` | Philox4x64-10 blocks, Random123's three among them; the run root; the key label, ordinals, key, counters and blocks of a path of every canonical shape and of a generator cell; `bounded` with every attempt's word, rejections included; `bernoulli`, `categorical` and `permutation`; `poisson` at thresholds spread over each table; `permute/2` images with the `flux-key/2` label parts of their key; generated and seed-tree identifiers; `uniform` foreign-key picks at draw index 0 and 1. |
| `tests/vectors/kernel2-transforms-vectors.json` | The logarithm's and AS241's constants by bit pattern; `log` and Φ⁻¹ at their region edges; per declaration, the binary64 constants of section 5.3 and the `z` bits and units of each probed `uniform53`, a declaration whose tails are taken as zero among them; the exponential's units. |

Words, keys and binary64 values are written as hex digits. Integers that can exceed `2^53` (uniforms, bounds,
domains, images, thresholds) are decimal strings; small ones (ordinals inside a path, draw indices, attempts, lengths,
scales, permutation items, a table's length) are JSON numbers. The vectors pin each rule at chosen points, edges
first. A change to any rule here moves a vector, and a moved vector is a `kernelVersion` change with a
`GOLDEN-CHANGELOG.md` row, as KERNEL-SPEC says of its own rules.
