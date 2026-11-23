# Correlations: the parts not yet in the specifications (draft)

**Draft, 2026-11-23. This page holds only what [ADR 0037](../../adr/0037-child-columns-and-child-counts-follow-their-parent-and-a-unit-can-follow-its-children.md)
decides for a number of children given a parent column and is not yet in the specifications.** The rest of the
format changes of ADRs 0035 to 0037 is in [PROFILE-SPEC.md](../PROFILE-SPEC.md) (profile format `1-4-0`),
[PROFILE-REQUEST-SPEC.md](../PROFILE-REQUEST-SPEC.md) (request format `1-2-0`),
[SEAM-FIDELITY-SPEC.md](../SEAM-FIDELITY-SPEC.md), [KERNEL-SPEC-2.md](../KERNEL-SPEC-2.md) section 8.6 and
[SCORECARD-SPEC.md](../SCORECARD-SPEC.md) (scorecard format `1-5-0`). Each part below moves into the specification
its heading names when it is built, and this page is removed.

## 1. What `fluxsim init` writes for them

Request format `1-2-0` is specified (PROFILE-REQUEST-SPEC.md section 7). `fluxsim init` writes these asks for the
columns it would draw from them: number columns it draws from histograms, the columns a clique could hold, and the
parent columns of relationships it lays out on the counted layout.

## 2. The bundle: children given a parent column (SEAM-FIDELITY-SPEC.md section 3 and the `counts` key)

**`counts[].given` and `counts[].childrenGiven`** (ADR 0037 decision 4), read under kernel `0.2.0` only; under
kernel `0.1.0` they are refused with `FLX-3105`, naming the kernel. `given` names a `category` column of the parent
expose with declared levels and no `nullFraction`, or a binned column; `childrenGiven` maps classes of it to
weight lists over the classes `children` declares, and a class with no list takes `children`'s weights, which the
plan states. `children` is required with them. Refused with `FLX-3403`: a key that is not a class of the given
column, a list of the wrong length or with no weight, and, as for `parentWhen`, a given column that reads a link, a
previous version, children, siblings or a column a summary over the parent's children sets. The emit's `extension`
member carries the entry for `H`, and `G` leaves it out, as it leaves `counts` out. An end-to-end `ship` test
covers it.

## 3. Kernel `0.2.0`: the counted layout given a class (KERNEL-SPEC-2.md section 8.1)

With `given`, `E` is split into classes `E_l` by the given column's value on each parent's final row (its bin, for a
binned column), keeping `E`'s order. Class `l` has `n_(l,k) = LRM(|E_l|, weights_l)` parents in count class `k` and
the slots `v_(l,i)` of section 8.1. `sigma = permute/2(K, |E|, r)` as before; the parents of `E_l` in ascending
order of `sigma` take `v_(l,0), v_(l,1), ...` as their expected counts. With a hook, the rank coupling applies
within each class. The minimum, the allotment and the rows are unchanged. No seed path is added: `K` is the key of
the `count` path and `sigma` is today's permutation, so the declaration meets section 12's four conditions as a
declaration that adds no path does. The vector file of the counted layouts gains layouts with given classes.

## 4. Scorecard format `1-5-0`: children per parent, grouped (SCORECARD-SPEC.md section 3)

- **Children per parent, grouped** (`crossTable`): the run's parents of each group of the parent column are counted
  into the profile's bins by their number of children, as section 3's ungrouped row counts them, and each group is
  compared with the profile's group by total variation distance; groups are named by ordinal, the tolerance is the
  `counts` entry's, and a noisy reference is widened by section 9's `w` group by group. A scorecard that holds such
  a row names `1-5-0`.
- **A noisy reference grouped by a link** is widened by section 9's `w` group by group, as a noisy grouped histogram
  is.

## 5. Error codes

No code is added. A `counts` entry is refused with `FLX-3403`, a key under kernel `0.1.0` with `FLX-3105`. Each
message names where the problem is and never quotes a value.
