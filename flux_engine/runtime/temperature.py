# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Temperature dials -- KERNEL-SPEC §6, the largest-remainder partition.

§6 in full::

    A dial t in [0,1] for an outcome with authored rate p over an eligible set
    of subjects is realised as:
    1. partition the eligible subjects (in canonical ordinal order) by LRM into
       a frozen share 1-t and a stochastic share t;
    2. in the frozen share, exactly LRM(count, p) subjects (the first ones in
       ordinal order) get the positive outcome -- no draw;
    3. in the stochastic share, each subject draws
       bernoulli(stream(temperature path), p).
    E[outcome rate] = p for every t; t = 0 consumes no draw; variance is
    monotone in t.

Three consequences this module is built around:

* **The partition is deterministic.** It is a largest-remainder allocation over
  ``{"frozen": 1-t, "stochastic": t}`` -- no draw, ties broken by NFC-sorted
  key, and therefore invariant to the seed. Two runs of the same plan freeze the
  same subjects.
* **``t = 0`` opens no stream.** The stochastic share is empty, so
  :meth:`Dial.realise` never calls its stream factory; the temperature path is
  never materialised and never enters ``seedTreeDigest``.
* **The expectation is exact, not approximate.** The frozen share's positives
  are ``largest_remainder(count, {"positive": p, "negative": 1-p})``, which is
  ``floor(count*p)`` plus the residual unit -- so the frozen share's realised
  rate is the closest integer rate to ``p`` that a set of that size admits.

§6 also names what the two dials of FLUX 0.5.0 act on: "``engagement`` scales
event-arrival rates, ``campaignResponse`` scales
``Journey.personaReactions[].successProbability``". "Scales" is read here as
"is the dial of", the only reading consistent with ``E[outcome rate] = p for
every t``: the dial moves the *variance* of the realised rate between zero and
the binomial variance, and leaves its expectation at the authored ``p``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Dict, FrozenSet, Mapping, Sequence, Tuple

from flux_engine.core.lrm import largest_remainder
from flux_engine.core.rng import DrawStream, bernoulli
from flux_engine.world.values import ONE, ZERO, probability_of

__all__ = ["Dial", "Partition", "Realisation", "partition"]

#: The two share names the §6 partition allocates over. NFC-sorted, so
#: ``"frozen"`` takes the residual unit on a tie -- which is what makes a dial
#: of exactly 0.5 over an odd eligible set freeze the extra subject rather than
#: leave it to a draw.
FROZEN = "frozen"
STOCHASTIC = "stochastic"

_POSITIVE = "positive"
_NEGATIVE = "negative"


@dataclass(frozen=True)
class Partition:
    """§6 step 1: the eligible ordinals split into the two shares."""

    frozen: Tuple[int, ...]
    stochastic: Tuple[int, ...]

    @property
    def size(self) -> int:
        return len(self.frozen) + len(self.stochastic)


def partition(eligible: Sequence[int], t: Decimal) -> Partition:
    """Split *eligible* (already in canonical ordinal order) by the dial *t*.

    The frozen share takes the **first** ``LRM(n, 1-t)`` ordinals, so growing the
    eligible set at the end leaves every already-frozen subject frozen.
    """
    dial = probability_of(t, what="temperature dial")
    ordered = tuple(eligible)
    if not ordered:
        return Partition(frozen=(), stochastic=())
    shares = largest_remainder(len(ordered), {FROZEN: ONE - dial, STOCHASTIC: dial})
    cut = shares[FROZEN]
    return Partition(frozen=ordered[:cut], stochastic=ordered[cut:])


@dataclass(frozen=True)
class Realisation:
    """The outcome of one dialled decision over an eligible set."""

    positive: FrozenSet[int]
    partition: Partition
    rate: Decimal

    def __contains__(self, ordinal: object) -> bool:
        return ordinal in self.positive

    @property
    def frozen_positive(self) -> Tuple[int, ...]:
        """The frozen ordinals that got the positive outcome, in ordinal order."""
        return tuple(o for o in self.partition.frozen if o in self.positive)

    @property
    def stochastic_positive(self) -> Tuple[int, ...]:
        """The drawn ordinals that got the positive outcome, in ordinal order."""
        return tuple(o for o in self.partition.stochastic if o in self.positive)


@dataclass(frozen=True)
class Dial:
    """One named temperature dial at its declared value."""

    name: str
    value: Decimal

    @classmethod
    def declared(
        cls, temperature: Mapping[str, Decimal], name: str, *, default: Decimal = ZERO
    ) -> "Dial":
        """The dial *name* as ``World.spec.temperature`` declares it.

        An undeclared dial defaults to ``0`` -- fully frozen. Defaulting the
        other way would make an omitted field introduce randomness, and §6 is
        explicit that ``t = 0`` is the no-draw end of the range.
        """
        return cls(name=name, value=temperature.get(name, default))

    def realise(
        self,
        eligible: Sequence[int],
        rate: Decimal,
        open_stream: Callable[[int], DrawStream],
    ) -> Realisation:
        """§6 steps 1-3 for an outcome of authored rate *rate*.

        *open_stream* is called once per **stochastic** subject with that
        subject's ordinal, and must return the stream for its temperature path
        ``("sim", sim_id, "temperature", <dial>, "subject", ordinal, occurrence)``.
        It is not called at all when the dial is 0.
        """
        p = probability_of(rate, what=f"rate under dial {self.name!r}")
        split = partition(eligible, self.value)
        positive: Dict[int, None] = {}
        if split.frozen:
            allocation = largest_remainder(len(split.frozen), {_POSITIVE: p, _NEGATIVE: ONE - p})
            for ordinal in split.frozen[: allocation[_POSITIVE]]:
                positive[ordinal] = None
        for ordinal in split.stochastic:
            if bernoulli(open_stream(ordinal), p):
                positive[ordinal] = None
        return Realisation(positive=frozenset(positive), partition=split, rate=p)
