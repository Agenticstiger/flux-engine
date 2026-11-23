# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The two tiers every accuracy family runs at, and the seeds each one uses.

* **pr** -- the pull-request tier, and the default. Fixed seeds and sizes small
  enough to finish in minutes, so a red result reproduces exactly on any
  machine and is a change in the code, not a roll of the dice.
* **nightly** -- fresh seeds and large sizes. Every seed is derived from one
  logged base seed, so a nightly failure reproduces from the artefact alone:
  ``FLUX_ACCURACY_TIER=nightly FLUX_ACCURACY_SEED=<baseSeed> pytest tests/accuracy``.

A test never draws a seed from the clock or from process entropy itself. It
asks :meth:`Tier.seed` for one, naming what the seed is for, and gets the same
answer in every pytest-xdist worker, which a ``secrets.randbits`` call per
process would not give.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Callable, Tuple, TypeVar

import pytest

__all__ = [
    "NIGHTLY",
    "PR",
    "PR_BASE_SEED",
    "SEED_ENV",
    "TIER_ENV",
    "Tier",
    "current_tier",
]

T = TypeVar("T")

#: ``pr`` (the default) or ``nightly``.
TIER_ENV = "FLUX_ACCURACY_TIER"
#: The nightly base seed. The workflow sets it to a fresh value and logs it.
SEED_ENV = "FLUX_ACCURACY_SEED"

PR = "pr"
NIGHTLY = "nightly"

#: The pull-request tier's base seed. A constant, so that the pull-request
#: tier is a deterministic regression gate rather than a sample.
PR_BASE_SEED = 20261111

_SEED_MODULUS = 1 << 63


def _derive(base_seed: int, labels: Tuple[object, ...]) -> int:
    material = "\x1f".join([str(base_seed), *(str(label) for label in labels)])
    digest = hashlib.sha256(b"flux-accuracy/seed\x00" + material.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % _SEED_MODULUS


@dataclass(frozen=True)
class Tier:
    """One tier's identity and base seed."""

    name: str
    base_seed: int

    @property
    def nightly(self) -> bool:
        """Whether this is the nightly tier."""
        return self.name == NIGHTLY

    def pick(self, *, pr: T, nightly: T) -> T:
        """The value for this tier: a size, a repetition count, a budget."""
        return nightly if self.nightly else pr

    def seed(self, *labels: object) -> int:
        """A non-negative 63-bit seed for whatever *labels* name.

        A pure function of the base seed and the labels: the same labels give
        the same seed in every process of one run, and different labels give
        unrelated ones.
        """
        return _derive(self.base_seed, labels)

    def seeds(self, count: int, *labels: object) -> Tuple[int, ...]:
        """*count* seeds for repetitions of one experiment."""
        return tuple(self.seed(*labels, index) for index in range(count))

    def only_nightly(self, why: str) -> None:
        """Skip the calling test in the pull-request tier, saying why."""
        if not self.nightly:
            pytest.skip(f"nightly tier only: {why} (set {TIER_ENV}=nightly)")

    def property_settings(self, *, pr: int, nightly: int, label: str) -> Callable[[Any], Any]:
        """Hypothesis settings for one property test, as a decorator.

        The pull-request tier is derandomised, so its examples are the same
        on every run. The nightly tier is seeded from the base seed and
        *label*, so a nightly counterexample reproduces from the logged base
        seed.
        """
        from hypothesis import seed as hypothesis_seed
        from hypothesis import settings

        def decorate(test: Any) -> Any:
            if self.nightly:
                configured = settings(max_examples=nightly, deadline=None)(test)
                return hypothesis_seed(self.seed("hypothesis", label))(configured)
            return settings(max_examples=pr, deadline=None, derandomize=True)(test)

        return decorate


def _nightly_base_seed() -> int:
    explicit = os.environ.get(SEED_ENV, "").strip()
    if explicit:
        return int(explicit) % _SEED_MODULUS
    # No seed was handed in. Every worker must still agree on one, so it is
    # derived from the UTC date rather than drawn: fresh each day, the same in
    # every process of one run, and recoverable from the date alone.
    today = datetime.now(timezone.utc).date().isoformat()
    return _derive(0, ("nightly", today))


@lru_cache(maxsize=1)
def current_tier() -> Tier:
    """The tier selected by ``FLUX_ACCURACY_TIER``; ``pr`` when it is unset."""
    name = os.environ.get(TIER_ENV, PR).strip().lower() or PR
    if name not in (PR, NIGHTLY):
        raise ValueError(f"{TIER_ENV} must be {PR!r} or {NIGHTLY!r}, got {name!r}")
    if name == NIGHTLY:
        return Tier(name=NIGHTLY, base_seed=_nightly_base_seed())
    return Tier(name=PR, base_seed=PR_BASE_SEED)
