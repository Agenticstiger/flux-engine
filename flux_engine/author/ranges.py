# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The range a contract declares for a column, read from its ``validationRules``.

A schema column may carry ``validationRules: [{type: range, constraint: "<text>"}]``.
The text is ``between A and B``, or one or more of ``>= A``, ``> A``, ``<= B`` and
``< B`` joined by ``and`` (any case); ``A`` and ``B`` are decimal numbers of at most
:data:`LONGEST_NUMBER` characters. Anything else declares nothing. Several range rules
hold together, so the tightest bound on each side stands. Bounds are exact fractions: a
strict bound is not reached, and the first grid value inside it is the nearest value a
column can draw.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, List, Mapping, Optional, Tuple

__all__ = ["LONGEST_NUMBER", "RangeRule", "column_range", "grid_bounds", "parse_range"]

#: The longest number, in characters, a range's constraint may carry.
LONGEST_NUMBER = 1000

_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")
_BETWEEN = re.compile(r"between\s+(.+)", re.IGNORECASE | re.DOTALL)
_CLAUSE = re.compile(r"(>=|<=|>|<)\s*(.+)", re.DOTALL)


@dataclass(frozen=True)
class RangeRule:
    """The tightest bound on each side a column's range rules give; ``None`` is no bound."""

    low: Optional[Fraction] = None
    low_strict: bool = False
    high: Optional[Fraction] = None
    high_strict: bool = False

    def tighten(self, other: "RangeRule") -> "RangeRule":
        low, low_strict = self.low, self.low_strict
        if other.low is not None and (
            low is None or other.low > low or (other.low == low and other.low_strict)
        ):
            low, low_strict = other.low, other.low_strict
        high, high_strict = self.high, self.high_strict
        if other.high is not None and (
            high is None or other.high < high or (other.high == high and other.high_strict)
        ):
            high, high_strict = other.high, other.high_strict
        return RangeRule(low, low_strict, high, high_strict)


def _number(text: str) -> Optional[Fraction]:
    text = text.strip()
    if len(text) > LONGEST_NUMBER or _NUMBER.fullmatch(text) is None:
        return None
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None


def _and_parts(text: str) -> List[str]:
    """*text* cut at each word ``and``, in any case, in one pass over its words.

    The same parts as splitting at whitespace, ``and``, whitespace; a regular
    expression for that backtracks over a long run of spaces, and a contract
    can hold one.
    """
    parts: List[str] = []
    words: List[str] = []
    for word in text.split():
        if word.lower() == "and":
            parts.append(" ".join(words))
            words = []
        else:
            words.append(word)
    parts.append(" ".join(words))
    return parts


def parse_range(constraint: Any) -> Optional[RangeRule]:
    """The bounds a range rule's text declares, or ``None`` when it declares nothing."""
    if not isinstance(constraint, str):
        return None
    text = constraint.strip()
    between = _BETWEEN.fullmatch(text)
    if between is not None:
        parts = _and_parts(between.group(1))
        if len(parts) != 2:
            return None
        low, high = _number(parts[0]), _number(parts[1])
        if low is None or high is None or low > high:
            return None
        return RangeRule(low, False, high, False)
    rule = RangeRule()
    for clause in _and_parts(text):
        found = _CLAUSE.fullmatch(clause.strip())
        value = _number(found.group(2)) if found is not None else None
        if found is None or value is None:
            return None
        sign = found.group(1)
        if sign in (">=", ">"):
            rule = rule.tighten(RangeRule(low=value, low_strict=sign == ">"))
        else:
            rule = rule.tighten(RangeRule(high=value, high_strict=sign == "<"))
    return rule


def column_range(column: Any) -> Optional[RangeRule]:
    """The range a schema column's ``validationRules`` give together, or ``None``."""
    rules = column.get("validationRules") if isinstance(column, Mapping) else None
    found: Optional[RangeRule] = None
    for rule in rules if isinstance(rules, list) else ():
        if not isinstance(rule, Mapping) or rule.get("type") != "range":
            continue
        parsed = parse_range(rule.get("constraint"))
        if parsed is not None:
            found = parsed if found is None else found.tighten(parsed)
    return found


def grid_bounds(
    rule: RangeRule, step: Fraction
) -> Optional[Tuple[Optional[Fraction], Optional[Fraction]]]:
    """The first grid value inside each bound, or ``None`` when none lies inside both."""
    low: Optional[Fraction] = None
    high: Optional[Fraction] = None
    if rule.low is not None:
        units = math.ceil(rule.low / step)
        if rule.low_strict and units * step == rule.low:
            units += 1
        low = units * step
    if rule.high is not None:
        units = math.floor(rule.high / step)
        if rule.high_strict and units * step == rule.high:
            units -= 1
        high = units * step
    if low is not None and high is not None and low > high:
        return None
    return low, high
