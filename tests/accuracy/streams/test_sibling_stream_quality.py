# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Family 5: cheap proxies for stream quality on the first words of sibling streams.

The sequence under test is the first raw 64-bit word of the generator-row stream at row
``0 .. n-1`` under one root, with ``n = 2**16`` in the pull-request tier and ``2**20``
nightly -- what a column of one-draw cells sees. PractRand (``test_practrand.py``) is the
sharp instrument; these are the quick ones that run on every pull request.

Pass criterion: three statistics, all in one Benjamini-Yekutieli battery at ``q = 0.01``
(66 tests), reject nothing:

* byte frequency: chi-square over the 256 byte values of the ``8n`` bytes of the words;
* per-bit balance: 64 exact binomial tests, bit ``j`` set in ``Binomial(n, 1/2)`` words;
* lag-1 serial correlation of the words read as uniforms in ``[0, 1)``:
  ``z = r * sqrt(n)``, two-sided normal p-value.

All 66 tests read the same words (the bit counts are linear in the byte counts), so they
share one sample and the battery is adjusted with BY, which holds under any dependence.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy")

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from scipy import stats  # noqa: E402

from tests.accuracy.harness.record import ResultLog  # noqa: E402
from tests.accuracy.harness.stats import Battery, Q  # noqa: E402
from tests.accuracy.harness.tier import Tier  # noqa: E402
from tests.accuracy.streams.practrand import first_words_bytes  # noqa: E402

FAMILY = "streams"
#: Every test reads the same words (bit counts are linear in byte counts), so one sample.
SAMPLE = "sibling-first-words"
CRITERION = (
    "no statistic rejected by Benjamini-Yekutieli at q=0.01 across byte frequency, "
    "per-bit balance and lag-1 serial correlation of sibling first words"
)


def _uniforms(words: np.ndarray) -> np.ndarray:
    return (words >> np.uint64(11)).astype(np.float64) * 2.0**-53


def serial_correlation_p(uniforms: np.ndarray) -> "tuple[float, float]":
    """Lag-1 correlation ``r`` and its two-sided p-value from ``z = r * sqrt(n)``."""
    r = float(np.corrcoef(uniforms[:-1], uniforms[1:])[0, 1])
    z = r * np.sqrt(uniforms.size)
    return r, float(2.0 * stats.norm.sf(abs(z)))


def build_battery(words: np.ndarray) -> Battery:
    """The 66-test battery for a vector of 64-bit words."""
    n = words.size
    battery = Battery(name="sibling first words", q=Q)
    byte_counts = np.bincount(words.astype("<u8").view(np.uint8), minlength=256)
    chi = stats.chisquare(byte_counts)
    battery.add(
        case="byte-frequency",
        test="chi-square-256",
        statistic=chi.statistic,
        pvalue=chi.pvalue,
        sample=SAMPLE,
    )
    for bit in range(64):
        ones = int(((words >> np.uint64(bit)) & np.uint64(1)).sum())
        battery.add(
            case="bit-balance",
            test=f"bit{bit}",
            statistic=ones / n,
            pvalue=stats.binomtest(ones, n, 0.5).pvalue,
            sample=SAMPLE,
        )
    r, p = serial_correlation_p(_uniforms(words))
    battery.add(case="serial-correlation", test="lag1", statistic=r, pvalue=p, sample=SAMPLE)
    return battery


@pytest.fixture(scope="module")
def words(tier: Tier) -> np.ndarray:
    n = tier.pick(pr=1 << 16, nightly=1 << 20)
    seed = tier.seed("streams", "sibling-quality")
    return np.frombuffer(first_words_bytes(seed, n), dtype="<u8").astype(np.uint64)


@pytest.fixture(scope="module")
def battery(words: np.ndarray) -> Battery:
    return build_battery(words)


@pytest.mark.parametrize("case", ["byte-frequency", "bit-balance", "serial-correlation"])
def test_sibling_first_words_pass_the_cheap_battery(
    case: str, battery: Battery, words: np.ndarray, tier: Tier, accuracy_log: ResultLog
) -> None:
    decisions = [d for d in battery.decisions() if d.entry.case == case]
    rejected = [d for d in decisions if d.rejected]
    accuracy_log.record(
        family=FAMILY,
        case=f"sibling-quality/{case}",
        criterion=CRITERION,
        passed=not rejected,
        seed=tier.seed("streams", "sibling-quality"),
        n=int(words.size),
        metrics={
            "method": battery.method,
            "tests": len(decisions),
            "minPvalue": min(d.entry.pvalue for d in decisions),
            "minAdjusted": min(d.adjusted for d in decisions),
            "rejected": [d.entry.test for d in rejected],
            "statistics": {d.entry.test: d.entry.statistic for d in decisions[:3]},
        },
    )
    assert battery.method == "by"
    assert not rejected, battery.summary()


def test_the_battery_has_sixty_six_tests(battery: Battery) -> None:
    assert len(battery.entries) == 1 + 64 + 1


def test_the_battery_catches_a_stuck_bit(words: np.ndarray) -> None:
    """Power check: forcing one bit on is rejected."""
    broken = build_battery(words | np.uint64(1 << 9))
    assert any(d.entry.test == "bit9" for d in broken.rejected())


def test_the_battery_catches_serial_correlation(words: np.ndarray) -> None:
    """Power check: mixing each word with its predecessor (r about 0.1) is rejected."""
    u = _uniforms(words)
    mixed = np.clip(u + 0.15 * np.roll(u, 1), 0.0, 1.9) / 1.9
    r, p = serial_correlation_p(mixed)
    assert r > 0.05
    assert p < 1e-6


def test_the_battery_catches_a_skewed_byte_distribution(words: np.ndarray) -> None:
    """Power check: zeroing the top byte of 2% of the words is rejected."""
    skewed = words.copy()
    skewed[: words.size // 50] &= np.uint64(0x00FFFFFFFFFFFFFF)
    broken = build_battery(skewed)
    assert any(d.entry.case == "byte-frequency" for d in broken.rejected())
