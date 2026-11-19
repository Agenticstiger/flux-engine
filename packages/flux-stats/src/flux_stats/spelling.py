# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""How a statistic's numbers are spelled in a document, and read back.

Counts are JSON integers; every other number is a decimal string, never a
JSON number with a fraction or an exponent, so no reader goes through binary
floating point (KERNEL-SPEC §2.1). This module is the one reader and writer
of those spellings for the profile document (docs/spec/PROFILE-SPEC.md) and
for the definitions that read their quantities back out of one. A value it
cannot read is refused with ``FLX-5036``, naming where in the document it
sits and the class of what it found, never the value: a profile carries
aggregates of data an owner chose not to share, and a refusal that quoted
one would carry it further.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from fractions import Fraction
from typing import Any, List, Optional, Sequence

from flux_stats.errors import FluxError, register

__all__ = [
    "FLX_5036",
    "MAX_SAFE_COUNT",
    "MOST_DIGITS",
    "normal_text",
    "read_count",
    "read_decimal",
    "read_units",
    "parse_exact",
    "read_counts",
    "read_texts",
    "refuse",
    "render_exact",
]

FLX_5036 = register(
    "FLX-5036",
    "profile document refused",
    "A profile is read whole or not at all, and this one does not have the "
    "shape docs/spec/PROFILE-SPEC.md gives a profile: a key the format does "
    "not define, a count that is not a non-negative JSON integer, a number "
    "written as a JSON float or in a spelling other than a plain decimal "
    "string, a statistic whose definition is not registered, or cells and "
    "flags that do not line up. The message names where in the document the "
    "problem is. Regenerate the profile with a profiler that writes this "
    "format version, or correct the named entry.",
)

#: The largest count a document may carry: the top of the range RFC 8785
#: serialises exactly (2^53 - 1). KERNEL-SPEC §2.1 refuses a larger
#: integer rather than letting it round, and a count is an integer.
MAX_SAFE_COUNT = 2**53 - 1

#: The most digits a decimal string may have. The fidelity block's own
#: bound (``concordance.predicate.require_number_length``) for the same
#: reason: a value is formed only after its length is known to be sane.
#: A plain decimal string here counts every digit, the leading ``0`` of a
#: number below one included; a declared number and a record's number are
#: counted without it (:func:`flux_stats.reading.digits_written_out`).
MOST_DIGITS = 1000

#: A plain decimal string: an optional minus, a whole part without leading
#: zeros, an optional fraction. No exponent, no plus sign, no spaces and no
#: digits outside ASCII, so one number has exactly one spelling up to
#: trailing fraction zeros.
_DECIMAL = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?")

#: An integer written as a decimal string, as ``S`` writes an encoded sum.
_UNITS = re.compile(r"-?(0|[1-9][0-9]*)")


def refuse(where: str, problem: str) -> FluxError:
    """The ``FLX-5036`` refusal for the entry at *where*."""
    return FluxError(FLX_5036, f"{where}: {problem}")


def _class_of(value: Any) -> str:
    return type(value).__name__


def normal_text(value: Any, where: str) -> str:
    """A non-empty string, already in Unicode Normalization Form C.

    The form the engine's canonical form puts every string in (KERNEL-SPEC
    §2.1), so a name read here compares equal to the name a contract
    spells. A string in another form is refused, never normalised: the
    digest covers the normalised text, so reading another spelling would
    read text the digest does not tell apart.
    """
    if not isinstance(value, str):
        raise refuse(where, f"expected a string, got {_class_of(value)}")
    if not value:
        raise refuse(where, "a name must not be empty")
    if not unicodedata.is_normalized("NFC", value):
        raise refuse(where, "a name is written in Unicode Normalization Form C")
    return value


def read_count(value: Any, where: str) -> int:
    """A count: a JSON integer from 0 to :data:`MAX_SAFE_COUNT`."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise refuse(where, f"a count is a JSON integer, got {_class_of(value)}")
    if not 0 <= value <= MAX_SAFE_COUNT:
        raise refuse(where, "a count lies from 0 to 2^53 - 1")
    return value


def _checked(value: Any, where: str, pattern: "re.Pattern[str]", what: str) -> str:
    if not isinstance(value, str):
        raise refuse(where, f"{what} is a string, got {_class_of(value)}")
    if len(value) > MOST_DIGITS + 2 or not pattern.fullmatch(value):
        raise refuse(where, f"{what} is not spelled as one ({len(value)} characters)")
    if len(value) - value.count("-") - value.count(".") > MOST_DIGITS:
        raise refuse(where, f"{what} has more than {MOST_DIGITS} digits")
    return value


def read_decimal(value: Any, where: str) -> Fraction:
    """A decimal string, read exactly."""
    text = _checked(value, where, _DECIMAL, "a plain decimal string")
    return Fraction(Decimal(text))


def read_units(value: Any, where: str) -> int:
    """An integer written as a decimal string, as ``S`` writes an encoded sum."""
    return int(_checked(value, where, _UNITS, "an integer string"))


def read_counts(value: Any, where: str, *, length: Optional[int] = None) -> List[Optional[int]]:
    """A list of counts, ``null`` where a cell is not published."""
    if not isinstance(value, list):
        raise refuse(where, f"expected a list of counts, got {_class_of(value)}")
    if length is not None and len(value) != length:
        raise refuse(where, f"{len(value)} counts for {length} cells")
    return [
        None if entry is None else read_count(entry, f"{where}[{index}]")
        for index, entry in enumerate(value)
    ]


def read_texts(value: Any, where: str) -> Sequence[str]:
    """A list of names, each non-empty and in Unicode Normalization Form C."""
    if not isinstance(value, list):
        raise refuse(where, f"expected a list of strings, got {_class_of(value)}")
    return [normal_text(entry, f"{where}[{index}]") for index, entry in enumerate(value)]


# ---------------------------------------------------------------------------
# Exact rationals, as the engine's documents spell them
# ---------------------------------------------------------------------------


def render_exact(value: Fraction) -> str:
    """Spell an exact rational as a string, without rounding it.

    Every number this module writes into ``plan.json`` or a fidelity
    record is an exact rational, and KERNEL-SPEC §2.1 keeps ``float`` out
    of any document -- so the spelling has to be exact and has to be a
    string. Most of these numbers terminate in decimal (a grid resolution,
    a decimal tolerance) and are written that way. A correlation's
    epsilon does not: it carries the rational lower bound of a square
    root, whose denominator is an arbitrary integer. Rather than round it
    -- rounding an epsilon down would loosen the margin it is subtracted
    from -- it is written as ``"numerator/denominator"``.

    The two spellings are disjoint (only the second contains ``/``), so
    :func:`parse_exact` reads either one back exactly.

    The terminating spelling is built by integer arithmetic rather than by
    dividing two ``Decimal`` s. A division goes through the arithmetic
    context, which rounds at 28 significant digits: ``689/2**36``
    terminates in decimal at 28 digits and a divided spelling of it is
    already the wrong number. Scaling the numerator onto a power of ten
    instead is exact for every terminating rational, whatever its scale.
    """
    reduced = Fraction(value)
    denominator = reduced.denominator
    residue = denominator
    twos = 0
    fives = 0
    while residue % 2 == 0:
        residue //= 2
        twos += 1
    while residue % 5 == 0:
        residue //= 5
        fives += 1
    if residue != 1:
        return f"{reduced.numerator}/{denominator}"
    # value == scaled / 10**scale, exactly: multiplying the numerator by
    # whichever of 2 and 5 the denominator is short of takes the
    # denominator to 10**max(twos, fives) without touching the value.
    scale = max(twos, fives)
    scaled = reduced.numerator * 2 ** (scale - twos) * 5 ** (scale - fives)
    sign = "-" if scaled < 0 else ""
    digits = str(abs(scaled)).rjust(scale + 1, "0")
    if scale == 0:
        return f"{sign}{digits}"
    return f"{sign}{digits[:-scale]}.{digits[-scale:]}"


def parse_exact(text: str) -> Fraction:
    """Read back a number written by :func:`render_exact`, exactly."""
    raw = unicodedata.normalize("NFC", str(text)).strip()
    if "/" in raw:
        numerator, _, denominator = raw.partition("/")
        return Fraction(int(numerator), int(denominator))
    return Fraction(Decimal(raw))
