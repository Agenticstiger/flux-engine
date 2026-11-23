# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The boundary where a schema-declared JSON number becomes a kernel ``Decimal``.

KERNEL-SPEC §2.1: "rejection (``FLX-1001``) of any ``float`` anywhere in the
object -- values that carry numbers are ``int`` or ``Decimal``". FLUX documents
are YAML, and the FLUX 0.5.0 schema declares ``seed``, ``weight``,
``successProbability``, ``mean``, ``stddev``, ``min``, ``max`` and every
``unitInterval`` as JSON numbers, so PyYAML hands this engine Python
``float``\\s. Exactly one place is allowed to convert them, and this is it:
everything downstream of :func:`decimal_of` is ``int`` or ``Decimal``, so no
float can reach a traced op or a digest.

**The conversion rule.** ``Decimal(repr(x))`` -- not ``Decimal(x)``. CPython's
``float.__repr__`` returns the shortest decimal string that round-trips to the
same double (``sys.float_repr_style == 'short'``, David Gay / Grisu style
shortest-repr, identical on every CPython build), so ``0.6`` becomes
``Decimal('0.6')`` and not the exact binary expansion
``Decimal('0.59999999999999997779553950749686919152736663818359375')``. The
result is therefore the decimal the document author wrote, which is the number
the digest should carry; and it is a pure function of the double, so two hosts
that parsed the same YAML byte-for-byte produce the same ``Decimal``.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Tuple, TypeVar

from flux_engine.core.errors import FluxError
from flux_engine.expr.types import nfc
from flux_engine.world.errors import FLX_5040, FLX_5041

__all__ = [
    "ONE",
    "ZERO",
    "decimal_of",
    "int_of",
    "nfc_sorted_items",
    "probability_of",
    "weight_map",
]

_T = TypeVar("_T")

ZERO = Decimal(0)
ONE = Decimal(1)


def decimal_of(value: Any, *, what: str) -> Decimal:
    """Return *value* as an exact ``Decimal``; ``FLX-5041`` on anything else.

    ``bool`` is refused ahead of ``int`` because ``True`` is an ``int`` in
    Python and a probability of ``True`` is a declaration mistake, not the
    number 1. The error names *what* and the value's **class**, never the
    value -- the same discipline KERNEL-SPEC §9 puts on the seam.
    """
    if isinstance(value, bool):
        raise FluxError(FLX_5041, f"{what}: bool is not a number")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, str):
        try:
            return Decimal(nfc(value))
        except InvalidOperation:
            raise FluxError(FLX_5041, f"{what}: string is not a decimal literal") from None
    raise FluxError(FLX_5041, f"{what}: {type(value).__name__} is not a number")


def int_of(value: Any, *, what: str) -> int:
    """Return *value* as an ``int``; ``FLX-5041`` on a bool, a float or a non-number."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise FluxError(FLX_5041, f"{what}: {type(value).__name__} is not an integer")
    return value


def probability_of(value: Any, *, what: str) -> Decimal:
    """Return *value* as a ``Decimal`` in ``[0, 1]``; ``FLX-5041`` outside it.

    Every rate this engine draws with -- a lifecycle weight, a temperature
    dial, a ``successProbability``, a categorical level -- passes through here,
    so an out-of-range declaration fails at the document boundary rather than
    inside :func:`flux_engine.core.rng.bernoulli`, where the diagnostic could
    not name the document field it came from.
    """
    number = decimal_of(value, what=what)
    if not ZERO <= number <= ONE:
        raise FluxError(FLX_5041, f"{what}: probability outside [0, 1]")
    return number


def weight_map(declared: Mapping[str, Any], *, what: str) -> Mapping[str, Decimal]:
    """Convert a declared weight mapping to NFC-keyed exact ``Decimal`` weights.

    Keys are NFC-normalised and the mapping is rebuilt in sorted-key order
    (KERNEL-SPEC §7: "iteration over any mapping is by sorted key"), so the
    largest-remainder allocation and the categorical sampler see one order
    whatever order PyYAML happened to build the dict in. Two declared keys that
    normalise to the same string are a collision and fail (``FLX-5040``) rather
    than one silently winning.
    """
    built: dict[str, Decimal] = {}
    for key in sorted(declared, key=nfc):
        name = nfc(str(key))
        if name in built:
            raise FluxError(FLX_5040, f"{what}: two keys normalise to {name!r}")
        built[name] = probability_of(declared[key], what=f"{what}[{name}]")
    if not built:
        raise FluxError(FLX_5040, f"{what}: no weights declared")
    return built


def nfc_sorted_items(mapping: Mapping[str, _T]) -> Tuple[Tuple[str, _T], ...]:
    """``mapping``'s items in NFC-sorted key order (KERNEL-SPEC §7)."""
    return tuple((nfc(key), mapping[key]) for key in sorted(mapping, key=nfc))
