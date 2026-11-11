# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Family 5: the seed tree meets the strict avalanche criterion.

Changing one input -- an ordinal by one, an occurrence by one, one bit of one character of a
name, the lowest bit of the root seed -- must flip every output bit with probability 1/2.
For each of those four mutations, over ``K`` base paths (2,000 in the pull-request tier,
20,000 nightly), the flips are counted per output bit of the 256-bit entropy and of the first
64-bit raw word.

Pass criterion:

* each bit position's flip count is Binomial(K, 1/2) under the null: the exact binomial test
  of every one of the ``4 * (256 + 64)`` positions, in one Benjamini-Yekutieli battery at
  ``q = 0.01``, rejects none. All tests of one mutation read the same K pairs (and each first
  word is a function of its entropy), so they share a sample and the battery uses BY, which
  holds under any dependence -- the case a defective hash would produce.
* the overall flip fraction of each mutation/output lies inside the Clopper-Pearson interval
  around 1/2. The eight intervals are one family, so each is taken at level ``1 - 0.01/8``
  (Bonferroni), which keeps the family-wise false alarm at 1% instead of the roughly 8% that
  eight separate 99% intervals would give on a correct engine.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy")

import hashlib  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from typing import Dict, List, Tuple  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from numpy.random import PCG64, SeedSequence  # noqa: E402
from scipy import stats  # noqa: E402

from flux_engine.core.seedtree import (  # noqa: E402
    SeedPath,
    SeedTreeRoot,
    derive_root,
    signal_path,
    stream_entropy,
    subject_trait_path,
)
from flux_engine.generator.paths import generator_column_path  # noqa: E402
from tests.accuracy.harness.record import ResultLog  # noqa: E402
from tests.accuracy.harness.stats import Battery, Q, clopper_pearson  # noqa: E402
from tests.accuracy.harness.tier import Tier  # noqa: E402
from tests.accuracy.streams.practrand import SEAM  # noqa: E402

FAMILY = "streams"
OUTPUTS = {"entropy": 256, "first-word": 64}
KINDS = ("ordinal+-1", "occurrence+-1", "name-bit", "root-seed-lsb")
CRITERION = (
    "every output bit flips with probability 1/2: no exact binomial test rejected by BY at "
    "q=0.01; overall flip fraction inside its Clopper-Pearson interval"
)
_LETTERS = "abcdefghijklmnopqrstuvwxyz"


@dataclass(frozen=True)
class Flips:
    """Per-bit flip counts of one mutation kind over *k* base paths."""

    k: int
    entropy: np.ndarray  # (256,)
    first_word: np.ndarray  # (64,)


def _first_word(entropy: int) -> int:
    return int(PCG64(SeedSequence(entropy)).random_raw())


def _flip_counts(pairs: List[Tuple[int, int]]) -> Flips:
    """Count, per output bit, the pairs whose two outputs differ in that bit."""
    k = len(pairs)
    entropy_xor = np.empty((k, 32), dtype=np.uint8)
    word_xor = np.empty((k, 8), dtype=np.uint8)
    for index, (a, b) in enumerate(pairs):
        entropy_xor[index] = np.frombuffer((a ^ b).to_bytes(32, "big"), dtype=np.uint8)
        word_xor[index] = np.frombuffer(
            (_first_word(a) ^ _first_word(b)).to_bytes(8, "big"), dtype=np.uint8
        )
    return Flips(
        k=k,
        entropy=np.unpackbits(entropy_xor, axis=1).sum(axis=0),
        first_word=np.unpackbits(word_xor, axis=1).sum(axis=0),
    )


class _HashRng:
    """A tiny SHA-256 counter generator, so the PR tier's inputs are the same on any NumPy."""

    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._counter = 0

    def integers(self, low: int, high: int) -> int:
        """A value in ``[low, high)`` (the modulo bias of 128 bits is negligible)."""
        digest = hashlib.sha256(f"flux-accuracy/avalanche/{self._seed}/{self._counter}".encode())
        self._counter += 1
        return low + int.from_bytes(digest.digest()[:16], "big") % (high - low)


def _random_name(rng: _HashRng) -> str:
    length = rng.integers(8, 17)
    return "".join(_LETTERS[rng.integers(0, len(_LETTERS))] for _ in range(length))


def _flip_one_bit(name: str, rng: _HashRng) -> str:
    # Lower-case ASCII with bits 0-6 flipped stays a printable, non-separator ASCII character
    # (0x21..0x7F), so the mutated name is always a valid path part.
    position = int(rng.integers(0, len(name)))
    bit = rng.integers(0, 7)
    return name[:position] + chr(ord(name[position]) ^ (1 << bit)) + name[position + 1 :]


def _pairs(kind: str, k: int, seed: int) -> List[Tuple[int, int]]:
    rng = _HashRng(seed)
    root = derive_root(SEAM, rng.integers(0, 1 << 62))
    pairs: List[Tuple[int, int]] = []
    for _ in range(k):
        if kind == "ordinal+-1":
            ordinal = rng.integers(1, 1 << 24)
            step = 1 if rng.integers(0, 2) else -1
            a = subject_trait_path("w-avalanche", ordinal, "age")
            b = subject_trait_path("w-avalanche", ordinal + step, "age")
            pairs.append((stream_entropy(root, a), stream_entropy(root, b)))
        elif kind == "occurrence+-1":
            occurrence = rng.integers(1, 1 << 16)
            step = 1 if rng.integers(0, 2) else -1
            a = signal_path("s-avalanche", "arrival", 5, occurrence)
            b = signal_path("s-avalanche", "arrival", 5, occurrence + step)
            pairs.append((stream_entropy(root, a), stream_entropy(root, b)))
        elif kind == "name-bit":
            name = _random_name(rng)
            a = subject_trait_path("w-avalanche", 3, name)
            b = subject_trait_path("w-avalanche", 3, _flip_one_bit(name, rng))
            pairs.append((stream_entropy(root, a), stream_entropy(root, b)))
        elif kind == "root-seed-lsb":
            seed_value = rng.integers(0, 1 << 62)
            path: SeedPath = generator_column_path(
                "accuracy", "avalanche", rng.integers(0, 1 << 24), "value"
            )
            roots: Tuple[SeedTreeRoot, SeedTreeRoot] = (
                derive_root(SEAM, seed_value),
                derive_root(SEAM, seed_value ^ 1),
            )
            pairs.append((stream_entropy(roots[0], path), stream_entropy(roots[1], path)))
        else:  # pragma: no cover - the KINDS tuple is closed
            raise ValueError(kind)
    return pairs


@pytest.fixture(scope="module")
def flips(tier: Tier) -> Dict[str, Flips]:
    k = tier.pick(pr=2_000, nightly=20_000)
    return {
        kind: _flip_counts(_pairs(kind, k, tier.seed("streams", "avalanche", kind)))
        for kind in KINDS
    }


def _add_positions(battery: Battery, case: str, counts: np.ndarray, k: int) -> None:
    for position, count in enumerate(counts.tolist()):
        result = stats.binomtest(int(count), k, 0.5)
        battery.add(
            case=case,
            test=f"bit{position}",
            statistic=count / k,
            pvalue=result.pvalue,
            sample=case.split("/")[0],
        )


@pytest.fixture(scope="module")
def battery(flips: Dict[str, Flips]) -> Battery:
    battery = Battery(name="seed-tree avalanche", q=Q)
    for kind, result in flips.items():
        _add_positions(battery, f"{kind}/entropy", result.entropy, result.k)
        _add_positions(battery, f"{kind}/first-word", result.first_word, result.k)
    return battery


CASES = [(kind, output) for kind in KINDS for output in OUTPUTS]


@pytest.mark.parametrize(("kind", "output"), CASES)
def test_each_output_bit_flips_with_probability_one_half(
    kind: str,
    output: str,
    battery: Battery,
    flips: Dict[str, Flips],
    tier: Tier,
    accuracy_log: ResultLog,
) -> None:
    case = f"{kind}/{output}"
    counts = flips[kind].entropy if output == "entropy" else flips[kind].first_word
    k = flips[kind].k
    decisions = [d for d in battery.decisions() if d.entry.case == case]
    rejected = [d for d in decisions if d.rejected]
    bits = OUTPUTS[output]
    level = 1.0 - Q / len(CASES)
    low, high = clopper_pearson(int(counts.sum()), k * bits, level)
    fraction = float(counts.sum()) / (k * bits)
    inside = low <= 0.5 <= high
    accuracy_log.record(
        family=FAMILY,
        case=f"avalanche/{case}",
        criterion=CRITERION,
        passed=not rejected and inside,
        seed=tier.seed("streams", "avalanche", kind),
        n=k,
        metrics={
            "method": battery.method,
            "bits": bits,
            "minPvalue": min(d.entry.pvalue for d in decisions),
            "minAdjusted": min(d.adjusted for d in decisions),
            "rejectedBits": [d.entry.test for d in rejected],
            "flipFraction": fraction,
            "intervalLevel": level,
            "interval": [low, high],
            "minBitFraction": float(counts.min()) / k,
            "maxBitFraction": float(counts.max()) / k,
        },
    )
    assert battery.method == "by"
    assert len(decisions) == bits
    assert not rejected, battery.summary()
    assert inside, f"{case}: flip fraction {fraction:.6f} outside [{low:.6f}, {high:.6f}]"


def test_the_whole_battery_rejects_nothing(battery: Battery) -> None:
    assert len(battery.entries) == len(KINDS) * (256 + 64)
    assert not battery.rejected(), battery.summary()


def _inject(battery: Battery, case: str, position: int, count: int, k: int) -> Battery:
    """A copy of the real battery with one bit's flip count replaced by *count*."""
    test = f"bit{position}"
    copy = Battery(name=f"{battery.name} (injected)", q=battery.q)
    for entry in battery.entries:
        if entry.case == case and entry.test == test:
            copy.add(
                case=case,
                test=test,
                statistic=count / k,
                pvalue=stats.binomtest(count, k, 0.5).pvalue,
                sample=entry.sample,
            )
        else:
            copy.add(
                case=entry.case,
                test=entry.test,
                statistic=entry.statistic,
                pvalue=entry.pvalue,
                sample=entry.sample,
            )
    return copy


def test_the_real_battery_catches_a_stuck_bit(battery: Battery, flips: Dict[str, Flips]) -> None:
    """Power check, inside the full 1,280-test battery: one entropy bit that never flips."""
    case = "name-bit/entropy"
    injected = _inject(battery, case, 17, 0, flips["name-bit"].k)
    assert len(injected.entries) == len(battery.entries)
    assert [(d.entry.case, d.entry.test) for d in injected.rejected()] == [(case, "bit17")]


def test_the_real_battery_catches_a_biased_bit(
    battery: Battery, flips: Dict[str, Flips], tier: Tier
) -> None:
    """Power check, inside the full battery: a first-word bit biased to 60% (PR) / 55% (nightly).

    A 55% bias at the PR size (K=2,000) is not reliably caught after the 1,280-test BY
    adjustment, which is the battery's resolution at that size, so the PR control is
    the stronger 60% bias.
    """
    case = "name-bit/first-word"
    k = flips["name-bit"].k
    bias = tier.pick(pr=0.60, nightly=0.55)
    injected = _inject(battery, case, 3, int(round(bias * k)), k)
    assert (case, "bit3") in [(d.entry.case, d.entry.test) for d in injected.rejected()]
