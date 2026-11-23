# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Sampling families: a declared shape plus the §4 kernel sampler that draws it.

SEAM-FIDELITY-SPEC §1 names four families a generator may pick from
("categorical, bounded-normal, rate, uniform/empirical") and requires that
"every draw uses the §4 kernel samplers". This module is that vocabulary,
factored out of the two places that need it: the vertical-derived subject
attributes of :mod:`flux_engine.world.derived` and the signal payload fields of
:mod:`flux_engine.runtime.payloads`.

Four rules hold of every family here:

* **Every parameter is an exact ``Decimal`` built from a string.** No family
  ever holds a ``float``, so a drawn value is safe to put in a traced op
  (KERNEL-SPEC §2.1).
* **Every draw has a fixed word cost** that depends on the family and not on
  the value drawn (:attr:`Family.word_cost`), so a stream's position after a
  draw is data-independent -- the property KERNEL-SPEC §4 buys with its fixed
  draw budgets, and the reason a family may not branch on a drawn value to
  decide whether to draw again.
* **Draw order inside a composite family is NFC-sorted key order**
  (KERNEL-SPEC §7), so a family declared in a different mapping order draws the
  same values.
* **Whether a family draws only whole numbers is a property of its
  declaration** (:attr:`Family.integral`), so the seam decides at plan time
  whether a drawn attribute may fill an ``INT`` column (KERNEL-SPEC §9), and
  never from the values one run happened to draw.

:meth:`Family.draw` takes a *factory* rather than a stream, and a zero-cost
family never calls it. That is the type-level statement of KERNEL-SPEC §3's
insertion-stability property: a constant trait materialises no seed path, so it
cannot appear in ``seedTreeDigest`` and cannot move any other stream.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, FrozenSet, Mapping, Tuple

from flux_engine.core.rng import (
    RAW_WORDS_PER_DRAW,
    DrawStream,
    bernoulli,
    categorical,
    poisson,
    truncated_normal,
)
from flux_engine.expr.types import BOOL, NUMBER, SET_STRING, Type, TypeKind, enum_of
from flux_engine.world.values import nfc_sorted_items

__all__ = [
    "Bernoulli",
    "BoundedNormal",
    "Categorical",
    "Constant",
    "Family",
    "RateCount",
    "SetOf",
    "StreamFactory",
    "drawn_type_satisfies",
]

#: How a family asks for the stream it draws on. Called at most once per draw,
#: and not at all by a zero-cost family.
StreamFactory = Callable[[], DrawStream]

_UNIFORM_WORDS = RAW_WORDS_PER_DRAW["uniform53"]


class Family(ABC):
    """One declared sampling family. Frozen, ``Decimal``-parameterised, total."""

    @abstractmethod
    def draw(self, open_stream: "StreamFactory") -> Any:
        """Draw one value, consuming exactly :attr:`word_cost` words of the stream.

        *open_stream* is called once, and only when :attr:`word_cost` is
        non-zero -- see the module docstring.
        """

    @property
    @abstractmethod
    def word_cost(self) -> int:
        """Raw words this family consumes per draw. Independent of the value drawn."""

    @property
    @abstractmethod
    def expr_type(self) -> Type:
        """The ``flux-expr/1`` type the drawn value inhabits (EXPR-SPEC §2)."""

    @property
    def integral(self) -> bool:
        """Whether every value this family can draw is a whole number.

        Read off the declaration, never off a drawn value, so the seam can
        decide at plan time whether a source may fill an ``INT`` column
        (KERNEL-SPEC §9). ``False`` unless a family says otherwise: a family
        that does not state it draws only whole numbers is refused for an
        ``INT`` column, at plan time and by name, rather than trusted.
        """
        return False


def _is_whole_number(value: Any) -> bool:
    """Whether *value* is an ``int`` (never a ``bool``) or a finite whole ``Decimal``."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value()


@dataclass(frozen=True)
class Constant(Family):
    """A declared value, drawn without touching the stream.

    The 0.3.0-compatible scalar trait (``digitalFluency: 0.6``) is this family:
    every subject carries the declared number, and the stream for that trait is
    never materialised -- which is what makes adding a scalar trait leave every
    other stream untouched (KERNEL-SPEC §3, insertion stability).
    """

    value: Any
    declared_type: Type = NUMBER

    def draw(self, open_stream: StreamFactory) -> Any:
        return self.value

    @property
    def word_cost(self) -> int:
        return 0

    @property
    def expr_type(self) -> Type:
        return self.declared_type

    @property
    def integral(self) -> bool:
        """The declared value is the only value, so it decides: ``1.000000`` is whole."""
        return _is_whole_number(self.value)


@dataclass(frozen=True)
class Categorical(Family):
    """One level from a closed weighted set -- KERNEL-SPEC §4's ``categorical``."""

    levels: Mapping[str, Decimal]

    def draw(self, open_stream: StreamFactory) -> str:
        return categorical(open_stream(), self.levels)

    @property
    def word_cost(self) -> int:
        return _UNIFORM_WORDS

    @property
    def expr_type(self) -> Type:
        return enum_of(self.levels)


@dataclass(frozen=True)
class BoundedNormal(Family):
    """KERNEL-SPEC §4's ``truncated_normal``: one uniform through the inverse CDF."""

    mean: Decimal
    stddev: Decimal
    lo: Decimal
    hi: Decimal
    scale: int = 6

    def draw(self, open_stream: StreamFactory) -> Decimal:
        return truncated_normal(
            open_stream(),
            mean=self.mean,
            stddev=self.stddev,
            lo=self.lo,
            hi=self.hi,
            scale=self.scale,
        )

    @property
    def word_cost(self) -> int:
        return _UNIFORM_WORDS

    @property
    def expr_type(self) -> Type:
        return NUMBER

    @property
    def integral(self) -> bool:
        """Whole at scale 0, and only there.

        ``truncated_normal`` quantises its result to the declared scale and
        clamps it to the bounds rounded *inward* onto that scale, so at scale 0
        the value and both bounds it can be clamped to are whole numbers.
        """
        return self.scale == 0


@dataclass(frozen=True)
class RateCount(Family):
    """A count: KERNEL-SPEC §4's ``poisson`` shifted by a declared *offset*.

    *offset* exists so a field whose smallest legal value is 1 (a payment
    *attempts* counter) is declared as ``RateCount(mean, offset=1)`` rather than
    drawn and then clamped -- clamping would put a spike of probability mass on
    the bound and would make the drawn value's distribution depend on a
    correction the declaration does not state.
    """

    mean: Decimal
    offset: int = 0

    def draw(self, open_stream: StreamFactory) -> Decimal:
        return Decimal(poisson(open_stream(), mean=self.mean) + self.offset)

    @property
    def word_cost(self) -> int:
        return _UNIFORM_WORDS

    @property
    def expr_type(self) -> Type:
        return NUMBER

    @property
    def integral(self) -> bool:
        """A Poisson count is an ``int``, so the draw is whole when the offset is."""
        return _is_whole_number(self.offset)


@dataclass(frozen=True)
class Bernoulli(Family):
    """A boolean at declared rate *p* -- KERNEL-SPEC §4's integer comparison."""

    p: Decimal

    def draw(self, open_stream: StreamFactory) -> bool:
        return bernoulli(open_stream(), self.p)

    @property
    def word_cost(self) -> int:
        return _UNIFORM_WORDS

    @property
    def expr_type(self) -> Type:
        return BOOL


@dataclass(frozen=True)
class SetOf(Family):
    """A ``set<string>``: one independent Bernoulli per declared member.

    Members are drawn in NFC-sorted order and **every** member is drawn, so the
    word cost is the member count and not the size of the set produced. A
    subject that draws false on every member carries the empty set, which is a
    legal ``set<string>``; ``products.has('tv')`` is simply false for it.
    """

    members: Mapping[str, Decimal]

    def draw(self, open_stream: StreamFactory) -> FrozenSet[str]:
        stream = open_stream()
        chosen = [name for name, rate in nfc_sorted_items(self.members) if bernoulli(stream, rate)]
        return frozenset(chosen)

    @property
    def word_cost(self) -> int:
        return _UNIFORM_WORDS * len(self.members)

    @property
    def expr_type(self) -> Type:
        return SET_STRING

    @property
    def member_names(self) -> Tuple[str, ...]:
        """The declared members, NFC-sorted -- the order :meth:`draw` uses."""
        return tuple(name for name, _ in nfc_sorted_items(self.members))


def drawn_type_satisfies(declared: Type, drawn: Type) -> bool:
    """Whether a family drawing *drawn* may fill a path declared as *declared*.

    Equal kinds satisfy each other, with one refinement and one widening, both
    read straight off EXPR-SPEC §2's "``enum<...>`` (a string drawn from a
    declared closed set; compares as string)":

    * an ``enum`` may fill a ``string`` -- every enum value *is* a string, and a
      predicate written against the path compares it as one;
    * an ``enum`` may fill an ``enum`` only when its levels are a subset of the
      declared ones, because a level outside the declared set would make a
      literal that is ``FLX-6003`` at plan time match at run time.

    A ``string`` may **not** fill an ``enum``: the drawn value could be outside
    the closed set the checker validated literals against.
    """
    if declared.kind is drawn.kind:
        if declared.kind is TypeKind.ENUM:
            return set(drawn.levels) <= set(declared.levels)
        return True
    return declared.kind is TypeKind.STRING and drawn.kind is TypeKind.ENUM
