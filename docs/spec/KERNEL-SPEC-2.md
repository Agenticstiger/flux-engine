# FLUX Simulation Engine: kernel v2 normative specification (v0.2)

Status: normative for kernel `0.2.0` and seed tree `flux-seed/2`, decided in
[ADR 0010](../adr/0010-faster-generation-through-a-second-kernel.md). The engine implements it beside kernel `0.1.0`,
and from 0.9.0 it is the kernel a new plan takes when `fluxsim plan` is given no `--kernel`.
Kernel `0.1.0` stays specified by [KERNEL-SPEC.md](./KERNEL-SPEC.md) and stays selectable (`fluxsim plan --kernel 0.1.0`):
a plan or an evidence pack that names `0.1.0` runs under it, to the same digests.

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
with `fluxsim plan --kernel` and takes `0.2.0` when it names none, and every stage runs and stamps the kernel of the
plan it reads, never the default (KERNEL-SPEC §1).

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
  call ordinal, a run index, a row, an entity index, a version index (section 8.4). `o1` and `o2` are the path's ordinals in order, `0` where it has fewer than two;
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

### 6.3 A generator plugin's seed

An expose the Simulation declares under `generators` (ADR 0032) has the columns the engine does not derive generated
by a plugin, which is handed a 32-byte seed with its request. The seed is a hash in the form of section 6.2, under its
own tag:

```text
seed = sha256(b"flux-seed/2" + NUL + b"plugin" + NUL + root + NUL + label(("sim", sim_id, "gen", exposeId)))
```

`label(path)` is KERNEL-SPEC §3's full label, so `sim_id` and `exposeId` are NFC-normalised and may hold neither
separator. The whole digest is the seed; nothing is drawn from it by the engine, and a plugin takes its values from it
as it likes. It reads the run's `root`, so a sweep moves a plugin's values as it moves every draw of the engine's. The
path is read only for an expose declared under `generators`, and its tag `plugin` is one no other derivation of
section 6 uses, so it is an additive path in the sense of section 12: a bundle that declares no plugin draws nothing
on it and keeps every digest. Kernel `0.1.0` derives no such seed and refuses the declaration (`FLX-3105`).

## 7. `permute/2` (replaces the round function of KERNEL-SPEC §4.1)

The split, the four alternating Feistel rounds and cycle walking of KERNEL-SPEC §4.1 hold. Only the round function
changes:

```text
roundKey = sha256(b"flux-permute/2" + NUL + key + NUL + domain.to_bytes(33, "big"))[0:16]
F(i, y)  = w0 + w1 * 2^64 of Philox4x64-10((y mod 2^64, y >> 64, i, 0), roundKey)
```

A half of a domain of at most `2^256` holds at most 128 bits, so `y` fits the counter's first two words.

## 8. Related tables under `0.2.0` (KERNEL-SPEC §8.2)

KERNEL-SPEC §8.2 holds, with three substitutions, and a relationship that declares how many children each parent has
is laid out by section 8.1:

- `k = sha256(b"flux-key/2" + NUL + H_bytes + NUL + label(...))`, the same label, in place of `flux-key/1`;
- `permute/2` (section 7) in place of `permute`;
- a `uniform` pick is section 4's `bounded(parentRows)` on the first `fromColumn`'s cell: draw index 1 when that
  column declares a `nullFraction`, whose null draw is index 0, and index 0 when it declares none;
- a `weighted` pick (the `links` entry's `weight`) draws one `uniform53` `u` at that same draw index, and the parent
  row is the first row `i` with `u < T_i`, `T_i = floor(2^53 * (w_0 + ... + w_i) / W)` on exact integers, found by
  bisection over the thresholds of the parent's final weights in row order.

A conditional link (KERNEL-SPEC §8.2, the Simulation's `links` key) and a bridge table's pair layout (the `bridges` key,
with `permute/2` and `flux-key/2` as above) are generated under `0.2.0` only; kernel `0.1.0` refuses either key with
`FLX-3105`, naming the kernel. So is an association table (the `associations` key, section 8.7).

### 8.1 Children per parent: the counted layout

A relationship that a `counts` entry of the Simulation names, or that an expected-count hook returns a member for
(ADR 0016), does not pick a parent per child row. Its child rows take their parents from a layout that gives each
eligible parent a whole number of children. The layout is a pure function of its inputs and draws no value.

A plan that uses a counted layout replays only on engine 0.10.0 and later. Kernel `0.2.0` did not change: the layout is read only for a bundle that declares a `counts` entry or an expected-count hook, in a key the earlier engines did not read, so every other bundle draws what it drew before (section 12).

**Inputs.** `N` is the child's rows and `P` the parent's rows, both after `--scale`. `E` is the eligible parents,
the parent rows on which `parentWhen` is TRUE on the parent's final row, or every parent row when the entry has no
condition, in row order. The rank `r` of an eligible parent is its position in `E`, and `e = |E|`. `m` is
`minChildren`, `0` when it is not declared.

**The key.** `K` is the whole sha256 digest whose first 16 bytes are `key(path)` of section 2,
`K = sha256(b"flux-seed/2" + NUL + root + NUL + key_label(path))`, 32 bytes, for the path

```text
("sim", sim_id, "gen", exposeId, "count", relationship)
```

where `exposeId` is the child's. The path has no ordinal part, so its whole path is its key label and both counter
ordinals are `0`. `K` is the `key` that `permute/2` (section 7) takes, which, like `flux-key/2`'s, is 32 bytes.
Like the cell path, the path stays out of the seed-path ledger. For `e >= 1`,

```text
sigma(r) = permute/2(K, e, r)
```

**The shape** (`children`). Its classes are taken in declared order. Levels must be strictly ascending whole numbers
of at least `0`, and bin edges strictly ascending whole numbers of at least `0` (`FLX-3403` otherwise). A level class
is `[v, v]`. Bin `k` is `[edge_k, edge_{k+1} - 1]`, except the last bin, which holds its upper edge too: from the
second to last edge to the last. The parents
of each class are `n_k = LRM(e, weights)` (KERNEL-SPEC §5), with class `k` keyed by `k` written in decimal and
zero-padded to the width of the largest class index, so that NFC order is class order. The slots `0 .. e - 1` are laid
out class by class, and the `j`-th slot of class `k`, for `j` from `0`, holds

```text
v = lo_k + floor(j * (hi_k - lo_k + 1) / n_k)
```

so slot values never decrease with the slot.

**Expected counts.** Rank `r` has an expected count `x_r`, a non-negative rational:

- a shape and no hook: `x_r = v_sigma(r)`, the value of slot `sigma(r)`;
- a hook and no shape: `x_r = e_r`, the hook's exact rational for that parent;
- a hook and a shape, the rank coupling: the ranks are ordered by `(e_r, sigma(r))` ascending, and the `i`-th rank in
  that order takes the value of slot `i`. When every `e_r` is equal this is exactly `v_sigma(r)`;
- neither: `x_r = 1`.

**Given a class** (a `counts` entry with `given`, ADR 0037 decision 4; kernel `0.2.0` only). The entry names a column
of the parent and a weight list for each of its classes over the shape's classes (SEAM-FIDELITY-SPEC section 3), a class
with no list taking the shape's own weights. The classes of the column are its declared levels in NFC order, or its
bins in order, and a parent's class is that of its value on its final row (the bin the value falls in, for a column
drawn from bins). `E` is split into `E_l`, one for each class `l`, keeping `E`'s order. Class `l` has its own count
classes `n_(l,k) = LRM(|E_l|, weights_l)`, keyed as above by `k`, and the slots `v_(l,i)` of the formula above over
`|E_l|`, from `i = 0`. `sigma` is `permute/2(K, e, r)` as before, taken over the whole of `E`. The ranks of `E_l`
in ascending order of `sigma` take `v_(l,0), v_(l,1), ...` as their expected counts. With a hook, the ranks of
`E_l` are ordered by `(e_r, sigma(r))` ascending instead, and the `i`-th takes `v_(l,i)`: the rank coupling applies
within each class. A class with no eligible parent has no slot, and a value of the column that is no class of it is
`FLX-3403`. The minimum, the allotment and the rows below are unchanged. No seed path is added: `K` is the key of the
`count` path, so the declaration meets the four conditions of section 12 as a declaration that adds no path does,
and a `counts` entry with no `given` lays out exactly as before. The vector file `tests/vectors/kernel2-counted-given-vectors.json`
pins layouts with given classes (section 11), and the `countedLayouts` of the streams vectors keep their bytes.

**The minimum.** `S = N - m * e`, and `S < 0` is `FLX-3403`, as is `e = 0` with `N > 0`. Then `y_r = max(x_r - m, 0)`
and `Y` is the sum of the `y_r`. If `Y = 0` and `S > 0`, every `y_r` is `1`.

**The allotment**, on exact integers, the rationals scaled to a common denominator. Each rank takes
`a_r = floor(S * y_r / Y)`. The `S - sum(a)` ranks with the largest remainders `(S * y_r) mod Y`, compared exactly,
take one more each, a tie going to the lower rank. The count `c` of an eligible parent is `m + a_r`, and of every
other parent `0`.

**The rows.** `C_p` is the sum of `c` over the parent rows before parent row `p`, in row order. Parent `p` owns the
child rows `C_p .. C_p + c_p - 1`, and child row `i` takes the parent whose run holds it, found by bisection over
`C`. The siblings of a parent are consecutive child rows.

**A `one_to_one` relationship** gives no parent more than one child. The allotment above gives each rank
`c_r = m + a_r`; where some `c_r` exceeds `1`, the layout keeps `min(c_r, 1)` for every rank and moves the children
above one: with `D = sum(max(c_r - 1, 0))`, the `D` spare children go one each to the ranks whose kept count is `0`,
those with the largest `y_r` first (the `y_r` of the allotment, after the replacement by `1` when every `y_r` is `0`),
a tie going to the lower rank. When no `c_r` exceeds `1`, `D = 0` and the layout is the allotment. So with `N <= e`
the layout never refuses, and the counts still total `N`. `N > e` is `FLX-3403`, as is `m > 1`. The cap is what lets
the rounding of `N` and of a shape's classes under `--scale` leave one child more than the parents of class `1`
without refusing.

**The hook draws nothing.** An expected-count hook is a pure function of the parent row, its final row and the
plan. It consumes no draw index on any path, and the key `K` is the same with or without it.

**Section 12.** The layout is additive. Its address is new, the literal `count` at the position where no other shape
has one; it is read only for a bundle that declares a `counts` entry or a hook; nothing that exists moves, the
foreign key column's own stream and every other cell's draws included; and the vector file pins its words: the key
label, the key, `sigma`, the slots, the counts and the offsets of chosen layouts (section 11).

### 8.2 Parent aggregates over a counted relationship

A contract `derive` with `aggregate` whose `via` relationship is on the counted layout (section 8.1) takes, for parent
row `p`, the summary of the children `C_p .. C_p + c_p - 1` of the child's rows. The layout is worked out through the
child plan's own foreign key for `via`, so the summary and the child's own pass see one layout. Each child row is read
as the child's own pass writes it: its cells, its rules, dependencies and `given` columns, over the run in one call
(`generate_row_slice`), in chunks for a long run. The semantics are EXPR-SPEC-2 section 5's: `count` is the number of
children for which `filter` is TRUE, every child when there is none; `sum`, `min` and `max` run over the children whose
`column` is not NULL and for which `filter` is TRUE, and are NULL when there are none; and `ifNone` replaces a NULL
summary. The arithmetic is exact, at the column's scale, and a value that does not fit the column is `FLX-6112` when
the row is made.

A summary draws nothing and takes no path, so no stream and no draw index moves. It is a step of the parent's rules,
ordered by EXPR-SPEC-2 section 10, and the parent's rows are made a row at a time, like a rule that reads through a
link. Each process that makes a slice of the parent makes the children of that slice's parents.

The parent's plan document has a `sums` member, a list of `{expose, relationship, generatorDigest}` for each summed
child plan, present only when the plan has a summary, so a plan with none keeps every byte. The child reads the
parent's plan as it is before its `sums` are attached, so no plan or digest holds itself. A child that reads a column
a summary sets, or a column a rule of the parent computes from one, through a path is a cycle, `FLX-6208`, raised at
plan time; the copy of the parent that a child reads has no sums, and the refusal is what guarantees nothing reads
them there. An expose other than a child that reads such a column through a link is `FLX-6210`.

**Section 12.** Nothing new is read for a bundle that declares no summary: the plan has no `sums`, and no path or draw
is added.

### 8.3 Exact allocation

A column whose fidelity declaration holds `allocation: exact` (SEAM-FIDELITY-SPEC section 2) gives each of its classes
exactly its share of the expose's rows, in place of a share each row draws for itself. The layout is a pure function of
the rows `N`, the column's declaration and the key, and it draws no value. `N` is the expose's rows after `--scale`.

A plan that uses an exact allocation replays only on engine 0.10.0 and later. Kernel `0.2.0` did not change: the allocation is read only for a column that declares `allocation: exact`, in a key the earlier engines did not read, so every other bundle draws what it drew before (section 12).

**The column.** It is drawn from `levels`, with `levelWeights` or, where the column declares none (a categorical column declared by a `support` only), the equal weights of
its declared support levels; from `bins` with `binWeights`; or from `levelWeightsGiven` or `binWeightsGiven` given a column
that is itself exact (the last case below). Its classes are its levels in NFC order of their names, the order a
categorical draw keys them in, or its bins in edge order. When the
column declares a `nullFraction` above 0, NULL is one more class, the last. The weights are read as exact rationals:
the declared ones, normalised to sum to 1, and, with a NULL class, scaled by `1 - nullFraction`, the NULL class taking
`nullFraction`. The class weights then sum to 1 exactly.

**The counts.** `n_k = LRM(N, weights)` (KERNEL-SPEC §5), class `k` keyed by `k` in decimal zero-padded to the width
of the largest class index, so that NFC order is class order and a tie goes to the lower class, as in section 8.1. A
class of weight 0 holds no row.

**The key.** `K` is the whole sha256 digest whose first 16 bytes are `key(path)` of section 2,
`K = sha256(b"flux-seed/2" + NUL + root + NUL + key_label(path))`, 32 bytes, for the path

```text
("sim", sim_id, "gen", exposeId, "allocate", column)
```

The path has no ordinal part, so its whole path is its key label and both counter ordinals are `0`. `K` is the `key`
that `permute/2` (section 7) takes. Like the cell path, the path stays out of the seed-path ledger.

**The slots.** The slots `0 .. N - 1` are laid out class by class: class `k` holds the `n_k` slots from
`start_k = n_0 + ... + n_(k-1)`. Row `i` takes the class of the slot `s_i = permute/2(K, N, i)`.

**Given an exact column.** Let `g` be the column the column `c` is given, with the key `K_g` of its own path, and let
`g` be exact with the class counts above. Row `i`'s parent class `l` is the class of its slot
`s_g = permute/2(K_g, N, i)`. Its index in the group of `l` is `j = s_g - start_l`, where `start_l` is the first
slot of class `l` in the layout of `g`, and the group's size is `n_l`. The child classes in the group have the counts
`LRM(n_l, weights given l)`, where `weights given l` is the row of `levelWeightsGiven` (or the list of
`binWeightsGiven`) for the level `l`, normalised, with the keys and tie rule above. Row `i` takes the child class of
the slot `permute/2(K_c, n_l, j)`, laid out class by class within the group, `K_c` being the key of the child column's
own path. A group of one row is `permute/2` over a domain of 1, which is 0, so the row takes the first child class
that holds a row. A given column is a categorical with no `nullFraction` (SEAM-FIDELITY-SPEC section 2), so a parent
class is never NULL: an exact column that declares a `nullFraction` and is named by another column's `given` is
refused as every such given column is (`FLX-3402`). A child that declares its own `nullFraction` has NULL as its last
class in every group, with its weight scaled as above.

**What a cell draws.** Every draw keeps its index (section 2). A cell makes its null draw when the column declares a
`nullFraction`, then its `categorical` draw (for a histogram, the bin), and, for a histogram, the `bounded` draw of
its value inside the bin, exactly as it does without `allocation`. The null draw and the `categorical` draw are made
and not read: the class comes from the layout. A histogram's value inside its bin is the `bounded` draw it always
makes, at its own index, so the value draw is the word it would have been. A row of the NULL class is NULL, and the
value draws of that row are made and not read, as they are for a cell whose null draw fired.

**Refusals.** Kernel `0.1.0` refuses the key, naming the kernel (`FLX-3105`), and so does a value other than `exact`.
A histogram that declares neither `binWeights` nor `binWeightsGiven` has no weights to allot by and is
`FLX-3402`. A column that declares `allocation` with a family other than a categorical or a histogram (a mean and a standard
deviation, a rate, an identifier), a column given a column of a linked row
(`given: <relationship>.<column>`), a column given a column that is not exact, a column given several columns, and
a column given an exact column that is itself given another are `FLX-3402`, the code for declared statistics that do
not parameterise the chosen family: the layout needs the weights it allocates by. A column that is not exact and is
given an exact column reads the class of that column's slot, the level the exact column holds in the row.

**Properties.** Every class holds exactly `n_k` rows. Under `given`, every pair of a parent class and a child class
holds exactly its count `LRM(n_l, weights given l)` within the group of the parent class, so each class of the child
holds the sum of those counts over the groups, and each parent class holds exactly its `n_l` rows.

**Section 12.** The layout is additive. Its address is new, the literal `allocate` at the position where no other
shape has one; it is read only for a column that declares `allocation`; nothing that exists moves, every other cell's
draws and the column's own stream included; and the vector file pins its words: the key label, the key, the counts,
the slots and the class of chosen rows, and for the given case the group sizes and every pair's count (section 11).
### 8.4 Versions per entity and their instants (ADR 0017)

A Simulation whose namespace carries a `history` entry for an emit (SEAM-FIDELITY-SPEC section 3) lays that emit out
as a chain of versions per entity. The layout, the chains, the ended entities and the change reasons are a pure
function of the plan and the seed tree. Every draw is addressed by an entity and a version, and nothing here reads
`spec.timeBounds.end`.

**The two layouts.** The emit's rows are laid out with section 8.1's counted layout twice, one inside the other, by
`counted_layout` as it stands:

1. *Entities per parent.* With a `parent`, the entities are the children and the parent's rows the parents:
   `A = counted_layout(K_A, P, E, shape, minimum, eligible, expected)`. `K_A` is the key, by section 2, of
   `("sim", sim_id, "gen", exposeId, "entities", relationship)`; `P` is the parent's rows and `E` the scaled
   `entities`; the shape, the minimum and the eligible parents are the `parent` entry's, read as a `counts`
   entry's, and an expected-count hook that returns a member for the relationship gives `expected` as it would for
   a `counts` entry. Without a `parent`, entity `e` has no parent and the entities' order is their index.
2. *Versions per entity.* The versions are the children and the entities the parents:
   `V = counted_layout(K_V, E, N, versions, 1, None, expected)`. `K_V` is the key of
   `("sim", sim_id, "gen", exposeId, "versions")`; `N` is the emit's scaled rows; the minimum is `1`. With a shape,
   `expected` is each entity's room, `until` less its first start, as an exact whole number of the chain's units,
   the expected counts of section 8.1, so the slot values go to the entities in the order of their room, the entity
   with the most room taking the most versions, the rank coupling of section 8.1. Without a shape, `expected` is
   none, so every entity counts alike and takes `floor(N / E)` or `ceil(N / E)` versions. The room reads drawn
   first starts and `until`, never the window.

Version row `i` is version `j = i - V.offsets[e]` of entity `e = V.parent_of(i)`, and entity `e` is a child of parent
row `A.parent_of(e)`. An entity's versions are consecutive rows and so are a parent's entities, so the rows run in
parent order, then entity order, then version order. More entities than versions is section 8.1's `S < 0`
(`FLX-3403`). The counts themselves, as a multiset, follow from `N`, `E` and the shape alone, so `c`, the largest
count an entity takes, is worked out at plan time from declared values. `--scale` keeps at least one entity and one parent row, so a history always has an entity. The natural entity's columns are laid out over the
entities as KERNEL-SPEC section 8.2 lays a key out over rows, the entity index standing for the row index, and every
version copies its entity's; so the check that a key's declared range holds its rows counts the entities, `E`, for a
history's emit. The `parent` relationship's foreign key copies parent row `A.parent_of(e)` whole on every version of
`e` and draws nothing. The primary entity is either one column, laid out over the rows as before, or the natural
entity's columns and the start, the natural columns from the entity's key and the start from the chain; any other
primary key is `FLX-3403` in this version.

**The three keys.** A layout key and a permutation key are the full 32-byte key of a path with no ordinal part,
as section 8.1's key is: `K_A` and `K_V` above, `K_end` of `("sim", sim_id, "gen", exposeId, "ended")` and
`K_reason` of `("sim", sim_id, "gen", exposeId, "reason")`.

**The first start and the lengths.** The chain's unit is a whole day for DATE columns and a millisecond for
TIMESTAMP columns.

- The first start `a` of entity `e` is drawn at position 0 of `("sim", sim_id, "gen", exposeId, "history", e)` at the
  chain's unit inside `J = [timeBounds.start, until - c')`, where `c'` is `c` (the largest count, above), plus one
  under `closeLast`, in units: from the part of `start` inside `J` with one `uniform53`, as EXPR-SPEC-2 section 6
  draws a value inside an interval, or uniformly over `J` with one `bounded` at the same position when `start` is
  absent or no bin meets `J`. Which of the two an entity takes depends on `J` and the bins, never on a draw. So
  every entity has at least `c' + 1` units of room. An empty `J` is `FLX-3403` at plan time.
- The length `d_j` of version `j` is one `uniform53` at position 0 of `("sim", sim_id, "gen", exposeId, "change", e,
  j)`, drawn from `length` as EXPR-SPEC-2 section 6 draws a value from a histogram, at the chain's unit and in whole
  units of at least one. Every version draws its length, the last included, and the length of an open version is
  left unread, so no position depends on the count.
- The starts are `s_0 = a` and `s_(j+1) = s_j + d_j`.

**The fit.** Every start and every closed end lies before `until`. Let `k` be the entity's closed versions, `D` the
sum of their lengths and `room` the entity's room in units. When `D > room - 1`, the closed lengths are compressed on
exact integers: each keeps one unit, and the other `room - 1 - k` units are allotted over the closed versions in
proportion to `d_j - 1` by the Largest Remainder Method (KERNEL-SPEC section 5), a tie going to the lower version.
Since `k` is at most `c'` and the room at least `c' + 1`, every chain fits, and no drawn row is ever refused. The fit
reads `until` and drawn values only and draws nothing.

**The ends.** Under `chain: nextStart`, version `j` ends at `s_(j+1)`, a half-open interval. Under `dayBefore` it ends
the day before `s_(j+1)`, a closed interval, for DATE columns only, in which a one-day version, or a compressed one,
starts and ends on the same day. The last version of an entity that has not ended
is open: its end is NULL, or the entry's `openEnd`, and lies at `until`, inside the window.

**A length is a histogram.** Positions 1 to 255 of the `change` path stay reserved, so that a later record can draw a
length another way there, given the version's state, without moving any position.

**The ended allocation.** With an `end`, the eligible entities are, under `closeLast`, every entity, or with a
`column` every entity with at least two versions, and under `finalVersion` the entities with at least two versions.
Let `E'` be their number, `E` the number of all entities and `share` the entry's decimal. The share is a share
of every entity, and the entities that end are chosen among the eligible ones:

```text
n = min(LRM(E, [share, 1 - share])[0], E')       keyed "0" and "1"
entity of eligible rank r ends  <=>  permute/2(K_end, E', r) < n
```

With no eligible entity, none ends, and when fewer than the share's `n` are eligible every eligible entity ends. Under `closeLast` an ended entity's last
version is closed, with its own drawn length inside the fit, at `s_last + d_last` under `nextStart` and at the day
before, `s_last + d_last - 1` day, under `dayBefore`. Under `finalVersion` its last version
is a final version that carries the entry's `value` in `column` and stays open. The ending change is forced and draws
nothing.

**Plans written before the share moved.** Engine 0.11.1 and earlier took `share` of the eligible entities alone,
`n = LRM(E', [share, 1 - share])[0]`, with the same permutation. A plan states which count it was made with: `plan`
writes `historyEndShare: "entities"` into `plan.json` when an emit declares a history with an `end` that leaves an
entity ineligible (a `column`, or `finalVersion`), so the plan digest covers it. Every other plan has no key, since
with every entity eligible `E' = E` and the two counts are one. A recorded plan without the key replays the earlier
count, so a run id made before the change replays to the run digest it recorded; planning the same bundle again
carries the key, and moves the plan digest, the run id and the bytes of the history and of every table that reads
it. Any other value of the key is refused (`FLX-2102`). Kernel `0.2.0` did not change: the key is read only for a
bundle that declares such an `end`, and an engine without this paragraph ignores it, so it replays a plan that
carries the key to the earlier count and so to other bytes (section 12).

**Change reasons.** The ordinary changes are every version after an entity's first except an ending version,
numbered `t` in row order, `T` of them. With one tracked column it is the reason of every change. Otherwise the
`reasons` classes take `LRM(T, weights)` changes each, keyed as section 8.1 keys classes, and change `t` takes the
class of slot `permute/2(K_reason, T, t)`, the slots laid out class by class. The count per reason is known at plan
time without a draw.

**The value draws.** The reason's column moves from its value on the previous version to a level drawn from that
level's row of `transitions`, one `categorical` at position `256 + c` of the version's `change` path, `c` the
column's place in `tracked`. Version 0 draws each tracked column's initial value at its own `256 + c`. The other
tracked columns carry their previous value. Positions 0 to 255 of the `change` path belong to the length: 0 is the
one `uniform53`, and 1 to 255 are reserved.

**Carried columns.** Every other column of the emit that the history does not set (not the entity's key, the
version's key, the parent's key, a validity column, the activation, a tracked column or the end column) and that no
rule sets takes, on every version, the value its own cell draws on the entity's first version row. The later rows'
cells are never drawn, which moves no draw, since every cell has its own counter. A foreign key other than the
parent's is picked on the first version row and carried too. Rules run on each version row after these values.

**The parent bound.** An entry with `parent.lifetime: true` keeps each entity to the lifetime `[ps, pe]` of its parent
row (ADR 0017, amendment of 2026-11-24). `ps` and `pe` are the parent's start and last-day cells in the chain's unit
(a DATE end covers its whole day, so a millisecond chain reads it to the last millisecond of that day); `ps` is absent
for a NULL start, and `pe` when there is no end, the end is NULL or it is before `ps`, and an absent bound cuts
nothing. The parents' lifetimes are final cells read at plan time, so the entities per parent (above) are worked out
first. Every array below is a plan-time array over the entities, which a slice, `--jobs`, `--shard`, the column path and
the per-row path read alike. A bundle that does not declare the key keeps every byte.

- *The limit.* The chain of entity `e` must end at `limit = min(until, pe)`, which is `until` when `pe` is absent.
  With `z = 1` when an ended entity's last version is closed (`closeLast`, or no `end`, where only the forced
  entities below end), and `z = 0` otherwise, the entity's chain holds `room - z - 1` versions at most, where
  `room = limit - a` and `a` is its first start.
- *The interval.* The first start is drawn as above, at position 0 of its `history` path, from the part of `start`
  inside `[max(first, ps), limit - (c + z) - 1]` when that is not empty, with `first` the window's first unit,
  so an entity whose parent cuts nothing draws what it draws without the key. Otherwise it is drawn inside
  `[max(first, ps), limit - z - 2]`, the room for one version. When that too is empty for any entity, the plan is
  `FLX-3403`, naming `history(<emit>).parent.lifetime`, the number of such entities and the ranges of their parents'
  starts and last days. An empty `J` for the whole history is refused as it is without the key.
- *The room.* `V` is worked out as above, with `expected` each entity's `limit - a`, so the rank coupling reads the
  limit. Each count is then capped at `room - z - 1`, and the `L` versions above the caps go one at a time to the
  entities under their cap, in rounds: a round gives one to each entity still under its cap, in order of
  `room` descending and entity index ascending, until the `L` are placed or no entity is under its cap
  (the allotment `counts._capped` makes for a `one_to_one` relationship, with a cap for each entity). The `L'` that
  found none are the shortfall, and the layout holds `N - L'` rows: the emit is planned at them, so its plan, its
  rows and its verification count the rows it has, and `V` is always worked out from the declared `N`.
- *The forced entities.* An entity whose `pe` is at or before `until` is forced to end whatever its count: under
  `finalVersion` its final version starts the day before `pe` at the latest and carries `value`, under `closeLast` or
  with no `end` its last version closes the day before `pe` at the latest. With `f` forced entities and `n` as in
  the ended allocation (`E'` counting the forced entities too), the others end by
  `permute/2(K_end, E'', r) < n - f`, with `E''` the eligible entities that are not forced and `r` the rank among
  them, and when `f` is at least `n` every forced entity ends and no other does. Without a forced entity this is the
  ended allocation unchanged.

**The paths.** The counter of the `history` path is `(entity, 0, draw, attempt)` and of the `change` path
`(entity, version, draw, attempt)`, by section 2's ordinal parts.

| path | kind | positions |
|---|---|---|
| `("sim", sim_id, "gen", exposeId, "entities", relationship)` | layout key | entities per parent |
| `("sim", sim_id, "gen", exposeId, "versions")` | layout key | versions per entity |
| `("sim", sim_id, "gen", exposeId, "history", entity)` | stream, one ordinal | 0: the first start |
| `("sim", sim_id, "gen", exposeId, "change", entity, version)` | stream, two ordinals | 0 to 255: the length, 0 for one `uniform53`, 1 to 255 reserved; `256 + c`: tracked column `c`'s value |
| `("sim", sim_id, "gen", exposeId, "ended")` | permutation key | which entities end |
| `("sim", sim_id, "gen", exposeId, "reason")` | permutation key | which change moves which tracked column |
| `("sim", sim_id, "gen", exposeId, "lag", entity, version)` | stream, two ordinals | 0: an event's lag (section 8.5) |
| `("sim", sim_id, "gen", exposeId, "kind", k, p)` | permutation key | one kind's rows per pair in one period (section 8.5) |

The layout and permutation keys stay out of the seed-path ledger, as section 8.1's does.

**Section 12.** These paths are additive. Each shape has, at the place after `exposeId`, a literal that no other
shape has there (`entities`, `versions`, `history`, `change`, `ended` and `reason`, beside the existing `row`, `key`,
`pair`, `count` and `allocate`), so every address is new. They are read only for a bundle that declares `history`.
Nothing that exists moves: the first version's cells draw on the generator cell path as they always did, the other
versions' cells are never drawn, and the parent's foreign key draws nothing on its own stream. The vector file
`tests/vectors/kernel2-history-vectors.json` pins each path's key, counters and blocks, and the layouts, chains, fits,
ended entities and reasons of chosen entries.

### 8.5 Projected emits: snapshots, events and periodic rows (ADR 0027)

An emit that a `snapshots`, `events` or `periods` entry of the Simulation names (SEAM-FIDELITY-SPEC section 3) is
projected: its rows follow from the history of its entry's `of`, an emit that a `history` entry declares.

**Rows derived at plan time and recorded.** A projected emit declares no `rows`. At plan time its number of rows is
worked out from the plan alone, from the history's layouts, chains, ended entities and change reasons, and for
periods the tracked values and each period's allotments, none of which needs a cell. The plan document records it as
the emit's `rows`, with `rowsFrom: {of, projection}`, and it enters `planDigest` only, never an entry for `H`. `--scale F`
scales the history's `entities` and `rows`, and a projection's count follows from the scaled history and is not scaled
again. `--shard I/N` takes contiguous shares of the derived rows. Another emit may pick its parents in a snapshot
or an events emit, whose row count is known at plan time and does not change with the window; a relationship whose
parent is a periodic emit is `FLX-3403` in this version, since that parent's rows grow with the window and every pick
over them would move.

**A snapshot** has one row per entity of the history, in entity order. Each column holds the entity's last version's
final value, its rules included. With `ended: drop` the entities the history ended are left out, so the rows are `E`
less the ended count. A snapshot draws nothing and adds no path.

**Change events.** In entity order, each entity has a `c` event for its first version, a `u` event for each later
version, and a `d` event when the history ended it: under `finalVersion` the change into its final version is the
`d`, and under `closeLast` the close of its last version is. `on` keeps the listed ones. An event's instant is the
version's start, or for a `closeLast` `d` the last version's end. A `when` predicate over the version the event is
about keeps an event only where it is TRUE and reads nothing a cell draws.

**The lag.** The `date` column holds the instant plus a lag. The lag is one `uniform53` at position 0 of
`("sim", sim_id, "gen", exposeId, "lag", entity, version)`, drawn at the date column's unit from the entry's `lag`
histogram, whose edges are whole days, inside the interval from zero to `until` less the change's instant, as
EXPR-SPEC-2 section 6 draws a value inside an interval, and uniformly over that interval with one `bounded` at the
same position when no bin meets it. The `d` of a `closeLast` entity takes the version
ordinal one past its last version. Without `lag` the event is recorded at its change, and no event is dated after
the history's `until`.

**Before, after and other columns.** `before` holds the previous version's value, NULL on a `c`, and on the `d` of a
`closeLast` entity the last version's own value, the state that was deleted; `after` holds this version's value, NULL
on a `d`. A `before` column the event contract requires is `FLX-3403` when `on` keeps `c`, and an `after` column it
requires is `FLX-3403` when `on` keeps `d`, since those events write NULL there. An event column that names a column of the history's emit and has no mapping holds
the value of the version the event is about: the new version for a `c` or a `u`, the last version for a `d`. Every
other column is drawn on the generator cell path at its row.

**The periods.** Period `p` runs from `timeBounds.start`'s day plus `p` grains to the day `p + 1` grains on, for `p`
from 0 while it starts before `timeBounds.end`, the window's end excluded. Each period's first day is counted from
the window's start, never from the period before, and a month added to a day the month lacks lands on the month's
last day, as `ADD_MONTHS` does (EXPR-SPEC-3).

**The pairs.** `match` says which versions a period holds: `overlap`, the default, every version valid at some instant
of the period; `end`, the version valid at the period's last instant inside the window; `start`, the version valid
at its first instant. An open version is valid up to the window's end, and an entity holds no period before its first
start or after a closed end. A held (version, period) is a pair.

**The per-period allotment.** A kind's `count` is a whole number of rows per pair, or `levels` with `levelWeights`.
Then in each period the `M` pairs the kind holds take `LRM(M, weights)` per level, keyed as section 8.1 keys classes,
and the pair of rank `r` among them, in row order, takes the level of slot `permute/2(K, M, r)`, the slots laid out
level by level. `K` is the key of `("sim", sim_id, "gen", exposeId, "kind", k, p)`, with `k` and `p` the kind's and
the period's positions written in decimal. A period's allotment reads no other period. A kind's `when` predicate over
the pair's version and the period column holds a pair's rows only where it is TRUE and reads nothing a cell draws.

**Order and count.** Rows run period by period, then entity, then version, then kind, then the kind's rows. The count
is the sum over periods, kinds and held pairs of the rows each takes, worked out in one pass over the versions, each
giving its first and last period, and the allotments of each period. A process that writes a slice finds its first
period by bisection over the periods' counts.

**The key.** A periodic emit's primary key, a whole-number column, is not laid out by section 8.1's key grid: row `i`
takes `lo + i`, with `lo` the low end of its declared range or first bin, one step per row and never a point spread
over the row count, so a longer window appends keys and keeps every earlier one. The check that the range holds the
rows stays. A primary key of another type, and a column under `allocation: exact`, which reads the row count, are
`FLX-3403` on a periodic emit. Every other emit's key is laid out as before.

**Section 12.** The `lag` and `kind` paths are additive: each is a literal no other shape has at the place after
`exposeId`, beside section 8.4's, so its address is new. They are read only for a bundle that declares `events` or
`periods`, and nothing that exists moves. A snapshot draws nothing. A projected emit's own cells draw on the generator
cell path at its rows, as any emit's do.

A lifetime, a date within a linked row's lifetime, a hidden column and a hidden table (the `lifetimes` and `within` keys,
an emit's `hiddenColumns` and `hidden`; SEAM-FIDELITY-SPEC §2) are generated under `0.2.0` only; kernel `0.1.0` refuses
each key with `FLX-3105`, naming the kernel.

### 8.6 The copula (ADR 0035)

A bundle that declares `fidelity.copula` (SEAM-FIDELITY-SPEC.md section 2) draws its columns together. The
declaration is read under kernel `0.2.0` only. The plan builds the matrix once, in `decimal`, and every row draws
its `q` columns from one stream.

**The path.**

```text
("sim", sim_id, "gen", exposeId, "copula", row)
```

It is a stream with one ordinal, the row: its key label leaves the row out, and the counter is `(row, 0, d, 0)`
(section 2). Draw `d`, for `d` from `0` to `q - 1`, is the column at position `d` of the declaration's `columns`,
and takes one word. Like the cell path, the path stays out of the seed-path ledger.

**The plan, in `decimal` at 50 digits, rounding half to even.** Each step runs in a local context, so no setting
of the caller moves a digit.

1. The latent correlation of a declared value is `r = sin(pi * tau / 2)` for `kendall` and
   `r = 2 * sin(pi * rho / 6)` for `spearman`. `pi` is `rng._pi` at 60 digits. The sine is its Taylor series
   summed at 60 digits, then rounded to 50: the first term, the argument, is always added; a later term is
   added only when its magnitude is at least `10^-55`, and the series stops at the first term below that, which
   is not added. The argument lies in `[-pi/2, pi/2]`, so no reduction is needed.
2. `R` has a unit diagonal and `r` off it, in the order of `columns`. A pair that is not listed has `r = 0`.
3. `R` passes when every pivot of its LDLᵀ factorisation, without pivoting and in the order of `columns`, is at
   least `10^-6`.
4. `alpha` is `0` when `R` passes. Otherwise `R` is shrunk towards the identity, `R(alpha) = (1 - alpha) R +
   alpha I`, with `alpha` from 40 steps of bisection on `[0, 1]`: `lo = 0`, `hi = 1`; the midpoint becomes `hi`
   when `R(mid)` passes and `lo` when it does not; `alpha = hi`. Each pivot of `R(alpha)` is concave in `alpha`
   and is 1 at `alpha = 1`, so the values of `alpha` that pass form an interval that ends at 1, and the
   bisection finds its start. The identity passes, so the repair never refuses.
5. `L` is the Cholesky factor of `R(alpha)`: each entry is the unit LDLᵀ factor's entry times the square root of
   its column's pivot, at 50 digits, then rounded once to binary64. An entry whose rounded binary64 magnitude is
   below `2^-900` is zero, so that no product of the draw below is subnormal (section 5.1).

The emit's entry of the plan document gains `copula: { "alpha", "factor" }`, only when the emit declares a
copula, so it enters `planDigest` there and nowhere else. `alpha` is canonical decimal text. `factor` is the
sha256 of the binary64 words of the lower triangle of `L`, diagonal included, in row-major order (row `i` holds
entries `0 .. i`), each word as the big-endian IEEE 754 bytes of the entry.

**The draw, in binary64** (section 5.1's arithmetic: one IEEE operation per step, no fused multiply-add, no
reassociation).

```text
u_d = (2 * (w0 >> 12) + 1) * 2^-53         w0 of the block of draw d at attempt 0; exact, strictly inside (0, 1)
z_d = Φ⁻¹(u_d)                             section 5.2
x_i = ((L[i][0] * z_0 + L[i][1] * z_1) + ...) + L[i][i] * z_i     left to right
```

For column `i`, with bins `b = 1 .. B`, shares `p_b` (its `binWeights`, normalised) and cumulative shares
`c_b = p_1 + ... + p_b` (`c_0 = 0`), all exact, and `w_b` grid values in bin `b`, numbered `0 .. w_b - 1` in the
order the histogram's own draw counts them: `F(b, j) = c_(b-1) + p_b * j / w_b`, an exact fraction;
`binary64(F)` rounds the fraction once to the nearest binary64, ties to even;
`t(F) = Φ⁻¹(binary64(F))` for `F <= 1/2` and `t(F) = -Φ⁻¹(binary64(1 - F))` for `F > 1/2`, so an upper tail keeps
its digits; a binary64 argument below `2^-1022` gives `-∞` or `+∞` of the sign of the tail. With `x = x_i`:

```text
lo = 0, hi = Bp                           Bp the last bin of positive weight
while hi - lo > 1:  mid = (lo + hi) // 2;  if t(c_mid) <= x: lo = mid  else: hi = mid
b = lo + 1                                the bin, of 1 .. B
lo = 0, hi = w_b
while hi - lo > 1:  mid = (lo + hi) // 2;  if t(F(b, mid)) <= x: lo = mid  else: hi = mid
the value is grid value lo of bin b       of 0 .. w_b - 1
```

Starting `hi` at the last bin of positive weight, and not at `B`, makes a bin of weight zero never chosen: for
every finite `x` the thresholds beyond it are `+∞` and give the same result as a bisection from `B`, and
`x = +∞` cannot select a trailing bin of weight zero. The value is uniform over its bin's grid values given its
bin, its margin is its histogram's, and the copula holds across bins and inside them. The path draws nothing
else. A copula column's own cell path draws only its NULL draw, at index 0, when it declares a `nullFraction`.
An implementation may tabulate thresholds; the bisections define the result.

**The column path.** Kernel `0.2.0`'s column path makes the same operations one array at a time: one
multiplication and one addition per term, looping over the terms, with no matrix product and no reduction that
could reorder a sum, and the same probes per row, so it equals the per-row path bit for bit, thresholds that tie
or are infinite included. A row's values depend on its ordinal alone, so `--jobs 1` and `--jobs 4` give the same
digests, the shards of `--shard I/N` concatenate to the whole run, and `--scale` changes only the number of rows.

### 8.7 Association tables (ADR 0039)

An emit that an `associations` entry names, `{emit, relationships: [a, b], distinct}`, puts both relationships on
the counted layout and pairs them. All arithmetic is on exact integers; `E` is the emit's rows, `P_a` and `P_b` the
two parents' rows.

1. **Degrees.** `d_a` is section 8.1's layout of `E` child rows over `P_a` parents under the key of the `count` path of
   `a` and the shape of `a`'s `counts` entry (none: every rank weighs 1), with its `minChildren`; `d_b` likewise. Both
   sum to `E`.
2. **Caps** (`distinct` only; `E > P_a * P_b` is `FLX-3403`). Each `d_a[p]` above `P_b` is cut to `P_b`; the excess
   `X` goes, in rounds, to the parents under the cap: weights `w_p = d_a[p]` for a parent under it and `0` otherwise
   (every parent under it weighs 1 when they all weigh 0), `floor(X * w_p / W)` each and the `X - sum` parents with the
   largest remainders one more, a tie to the lower row, each gain cut to the parent's room, until none is left. Then
   `d_b` against `P_a` the same way.
3. **Gale-Ryser** (`distinct` only). With `b` the second side sorted in decreasing order, the gap is
   `max(0, max_k (b_1 + ... + b_k - sum_p min(d_a[p], k)))` over `k = 1 .. P_b`. While the gap `g` is above 0, `g` moves
   are made, each taking a row from the first parent of most rows of `d_b` (the lowest row among equals) to the first
   parent of fewest, and the gap is taken again.
4. **Pairs.** `o_a`, `o_b` are the offsets (the sum of the counts before each parent). Row `r`'s first-side parent is
   the `p` with `o_a[p] <= r < o_a[p] + d_a[p]`, and its second-side parent the last `q` with `o_b[q] <= s`,
   `s = permute/2(K, E, r)`, `K` the key of the path `("sim", sim_id, "gen", exposeId, "associate", a, b, "stubs")`:
   `sha256("flux-seed/2" NUL root NUL key_label(path))`, as section 8.1 derives a count key.
5. **Repair** (`distinct` only). A row is *repeated* when an earlier row holds its pair; the pairs held so at the start
   are the *repeated pairs*. The switches run in rounds `t = 0 .. 63`: round `t` takes every repeated row `r` still
   waiting, in row order, and its partner `s = permute/2(K', E, (64 r + t) mod E)` with `K'` the key of the path
   ending `"switches"`. With `(a, b)` and `(a', b')` the two rows' pairs, the switch is made when `s` is not a
   repeated row still waiting, `a != a'`, `b != b'`, `(a', b')` is no repeated pair, and neither `(a, b')` nor
   `(a', b)` is held: the rows become `(a, b')` and `(a', b)`, and `r` stops waiting. The rows still waiting are then
   placed: their second-side parents form a multiset of stubs; their first-side parents, most waiting rows first (the
   lower row on a tie), each take, for their rows in row order, the parents of the stubs left that they do not pair
   with, most stubs first (the lower row on a tie). When a parent finds fewer such stubs than rows, every row's
   second-side parent is laid out again and no switch or placement is kept: first-side parents in decreasing order of
   `d_a` (the lower row on a tie) each take the `d_a[p]` second-side parents with the most stubs left, a tie going to
   the lower rank `(sigma(q) + tau(p)) mod P_b` with `sigma(q) = permute/2(K, P_b, q)` and
   `tau(p) = permute/2(K, P_a, p)`, held by its rows in ascending order of that rank. The keyed ties give parents of
   as many rows different second-side parents, so a dense table holds no block pattern.

With `distinct: false` steps 2, 3 and 5 are skipped and a row's parents are a function of its index alone. The repair
reads every row: the process that starts a run's worker pool works it out once and hands it to every worker, and a
process that writes the emit without a pool works it out itself, so `--jobs`, chunking and `--shard` keep the bytes.
The `associate` path is a key only, with no draw.

**The distance.** A side's distance from its shape is the total variation distance between its parents counted into
the shape's classes and the classes' weights. A count that no class holds (below the first, above the last, between
two levels) is a mass of its own whose weight is 0, so it adds its whole share to the distance.

**Section 12.** The `associate` path is additive: the literal `associate` stands after `exposeId` where no other shape
has it, only a bundle that declares `associations` reads it, nothing that exists moves, and
`tests/vectors/kernel2-association-vectors.json` holds its keys and five layouts.

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
| `tests/vectors/kernel2-streams-vectors.json` | Philox4x64-10 blocks, Random123's three among them; the run root; the key label, ordinals, key, counters and blocks of a path of every canonical shape and of a generator cell; `bounded` with every attempt's word, rejections included; `bernoulli`, `categorical` and `permutation`; `poisson` at thresholds spread over each table; `permute/2` images with the `flux-key/2` label parts of their key; generated and seed-tree identifiers; a generator plugin's seed (section 6.3); `uniform` foreign-key picks at draw index 0 and 1. |
| `tests/vectors/kernel2-streams-vectors.json`, `countedLayouts` | Section 8.1's counted layouts: the path's key label and key, `e`, `sigma` for every rank, the slot values, the expected counts as exact fractions and the counts per parent row with their offsets, and the parent of the first, the last and a run boundary child row; with no shape, with bins and a minimum, with levels whose slot values total the child rows, with a parent condition, with a hook alone, with a hook and tied expected counts under a shape, with a constant hook, whose counts equal the shape's alone, and with three `one_to_one` layouts, one whose allotment the cap moves from a parent of class `1` to one of class `0`, one the cap leaves as the allotment, and one under a hook that moves two spare children by `y`. |
| `tests/vectors/kernel2-streams-vectors.json`, `exactAllocations` | Section 8.3's exact allocations: the path's key label and key, the class weights as exact fractions, the counts and first slots per class, and the slot and class of rows; four levels over 26 rows, the same with a `nullFraction` (a NULL class), four bins over 1000 rows (chosen rows only), and a column given the first one, with the parent class and child class of every row, the group sizes and the count of every pair of a parent class and a child class. |
| `tests/vectors/kernel2-history-vectors.json` | Section 8.4's and 8.5's paths: each path's key label, key, counters and blocks; the layouts, chains, fits, ended entities and reasons of chosen entries, and the derived counts and allotments of chosen projections. |
| `tests/vectors/kernel2-transforms-vectors.json` | The logarithm's and AS241's constants by bit pattern; `log` and Φ⁻¹ at their region edges; per declaration, the binary64 constants of section 5.3 and the `z` bits and units of each probed `uniform53`, a declaration whose tails are taken as zero among them; the exponential's units. |
| `tests/vectors/kernel2-copula-vectors.json` | Section 8.6's copula path: the key label, key, counters and blocks of chosen rows, and for each `u`, `z`, `x`, the bin thresholds, the bin and grid index and the value, and the plan's `alpha`, factor and its digest, for three small copulas: one whose matrix passes, one that is shrunk, and one with a bin of weight zero. |
| `tests/vectors/kernel2-counted-given-vectors.json` | Section 8.1's counted layouts given a class of a parent column: the path's key label and key, `e`, the classes of the column with each one's weights, the class of each eligible parent, `sigma`, the slot values, the expected counts as exact fractions, the counts per parent row and their offsets, with and without a hook. |
| `tests/vectors/kernel2-binned-given-vectors.json` | A column drawn given a column drawn from bins (SEAM-FIDELITY-SPEC.md section 2): for chosen rows, the key, counters and blocks of each column's own cell path, the bin and class the parent drew, the place inside the bin and the value, the class named by the text of its lower edge. |
| `tests/vectors/kernel2-dates-vectors.json` | The dates stream of EXPR-SPEC-2 section 7: its key label, key, counters and blocks at positions 0, 1 and 257 for a DATE column and for a TIMESTAMP column, and the cell path of a hidden column; the thinning positions 2, 3 and 256 and a thinning run over a fixed intensity. |
| `tests/vectors/kernel2-association-vectors.json` | Section 8.7's pairing: for five layouts (sparse, repeats allowed, capped, moved by the Gale-Ryser step, and dense, laid out again whole), the `associate` path, its stubs and switches keys, each side's parent rows, shape and degrees, the rows, the moves, what the repair did and the second side's parent of every row. |

Words, keys and binary64 values are written as hex digits. Integers that can exceed `2^53` (uniforms, bounds,
domains, images, thresholds) are decimal strings; small ones (ordinals inside a path, draw indices, attempts, lengths,
scales, permutation items, a table's length) are JSON numbers. The vectors pin each rule at chosen points, edges
first. A change to any rule here moves a vector, and a moved vector is a `kernelVersion` change with a
`GOLDEN-CHANGELOG.md` row, as KERNEL-SPEC says of its own rules.

## 12. A path that only an opt-in declaration reads is additive

A feature that adds a seed-tree path (a stream for a value the generator did not draw before) does not change
`kernelVersion` when every one of these holds:

1. **A new declaration reads it, and only that declaration.** The path is drawn only for a bundle that
   declares the feature, in a key of the Simulation or the contract that the engine did not read before. A bundle
   without the key draws nothing on the path.
2. **Its address is new.** The path has a label no existing path has, built by section 2's derivation, so its key
   is not the key of any path a bundle already draws on.
3. **Nothing that exists moves.** No existing path changes its address, its derivation, the number or order of its
   draws, or what a draw's word is read as. The streams of every other cell, column and table of the run, and so
   every digest of a bundle that does not declare the feature, are the bytes they were.
4. **Its words are pinned.** The vector files hold the new path's key, counters and blocks, so an implementation is
   held to them as it is held to section 11's.

Such a feature is additive: a kernel `0.2.0` plan written before it replays to the digests it had, and a plan
written after it that does not declare it has the same digests as one written before. The declaration is in the
bundle, so it is part of `inputDigest` and the run seeds from `H` as before.

A change that breaks any of the four is not additive and is a `kernelVersion` change with a `GOLDEN-CHANGELOG.md`
row: a draw that an existing declaration starts to make, a path whose address is another path's, a rule that
changes what a recorded draw means, or a default that a bundle which declares nothing would now take. The
same holds for kernel `0.1.0` and KERNEL-SPEC.md, whose seed-tree paths are in its section 3.

The dates stream (EXPR-SPEC-2 section 7, `R` the canonical JSON of `["dates", <column>]`) and the cell path of a hidden
column are additive under conditions 1 to 4. (1) Only `lifetimes`, `within`, `rates` and `hiddenColumns` read them: a bundle that
declares none of the four draws nothing on either path, and a column with no `rates` entry draws position 1 of its dates
stream alone, while one with an entry takes proposal `k` at position `1 + 2k` and its test at `2 + 2k` for `k` below 128. (2) The label's first `R` element, `"dates"`, is no element any
contract or bundle rule's `R` holds, and a hidden name is one no contract column has (a hidden name that is a contract
column's is refused), so each address is new. (3) Nothing that exists moves: no existing path changes its address,
derivation, draws or reading, and every other cell, column and table keeps the bytes it had. (4) `tests/vectors/kernel2-dates-vectors.json`
holds the key, counters and blocks of the dates stream and of a hidden column's cell path.

The paths of section 8.4 meet all four. Each has at the place after `exposeId` a literal no other shape has there, a
bundle without a `history` entry never reads them, nothing that exists moves, and
`tests/vectors/kernel2-history-vectors.json` pins their keys, counters and blocks. No plan document enters an entry
for `H`: a plan document holds the window, and the seed tree's root folds in every `H`, so the root and every stream
are the same under a window that grows, and a history's emit is the same byte for byte under two windows with the
same `until`. The `lag` and `kind` paths of section 8.5 meet them the same way. So a history and its projections are additive: a kernel `0.2.0` plan written before them replays to the digests it had.

The copula path of section 8.6 meets all four. (1) Only a bundle that declares `fidelity.copula` reads it, in a key
of the fidelity block that the engine did not read before. (2) The literal `copula` stands at the place after
`exposeId` where no other shape has it, which the shape table of `tests/kernel2` checks for every shape. (3) Nothing that exists moves, and a
copula column's own cell path keeps its null draw at the index it always had. (4)
`tests/vectors/kernel2-copula-vectors.json` holds the key label, the key, the counters and the blocks.

An example of a declaration that adds no path at all: a column drawn given several columns (`given` a list,
SEAM-FIDELITY-SPEC section 2). It draws on its own column's path at the index every categorical column draws at,
and the combination of its parents' levels only chooses the table of weights the uniform is searched in. It
satisfies 1 to 4 trivially, so a kernel `0.2.0` plan that declares it replays on any engine that reads the list. So does a column drawn given
a column of a linked row (`given: <relationship>.<column>`): the level of the parent's final value picks the same
kind of table, the cell is drawn on its own path at the same index, and the draws of every other cell are the bytes
they were. So does a column drawn given the bin of another number column (SEAM-FIDELITY-SPEC.md section 2): the
parent's own draw chose the bin, the bin picks the table the child's uniform is searched in, the child's cell is
drawn on its own path at the index every histogram or categorical column draws at, and
`tests/vectors/kernel2-binned-given-vectors.json` pins the cells. The same holds for a `conditions` group that
fixes a categorical column drawn given a bin (CONSUMER-QUERIES-SPEC.md section 10).
