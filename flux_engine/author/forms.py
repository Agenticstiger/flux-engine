# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The statistic forms of profile format 1-3-0 that ``fluxsim init`` reads only in part.

PROFILE-SPEC.md section 3 lets a column statistic be grouped by a number column, grouped by a
column of a linked row (``groupByLink``) or counted once per entity (``per``). Each answers a
different question from an ungrouped or a categorically grouped statistic of the same column, so
reading one as if it were the other would draw the wrong numbers. Until a rule reads them, every
reader of a column's statistics passes them over (:func:`grouped`, :func:`unread_reason`), and the
ledger lists each with its reason (ADR 0014).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

__all__ = [
    "BY_LINK",
    "BY_NUMBER",
    "PER_ENTITY",
    "SUPERSEDED",
    "grouped",
    "number_grouped",
    "unread_reason",
]

BY_LINK = (
    "a statistic grouped by a linked row is read only as the weights of a column of the child "
    "drawn given a column of that row"
)
PER_ENTITY = (
    "a trait stable per entity is a contract dependency from the entity key, to declare in the "
    "contract: no dependency of the contract sets this column from the columns it is counted per"
)
BY_NUMBER = (
    "a statistic grouped by a number column is not read: a number column publishes bins, and "
    "a column is drawn given another only over the few levels the profile lists for it"
)

_NUMBER = "number"

#: Set on a per-row statistic that a per-entity one replaces (:func:`flux_engine.author.columns.per_entity`).
SUPERSEDED = "_superseded"


def grouped(statistic: Mapping[str, Any]) -> bool:
    """Whether a statistic is anything but a count over every row of its column.

    ``groupBy`` and ``groupByLink`` split the rows into groups, and ``per`` counts entities
    instead of rows, so none of the three answers an ungrouped question.
    """
    return (
        "groupBy" in statistic
        or "groupByLink" in statistic
        or "per" in statistic
        or SUPERSEDED in statistic
    )


def number_grouped(statistic: Mapping[str, Any], columns: Optional[Mapping[str, Any]]) -> bool:
    """Whether a ``groupBy`` of a statistic names a number column of its table.

    *columns* are the ``columns`` of the table's entry in the profile document.
    """
    if "groupBy" not in statistic or not isinstance(columns, Mapping):
        return False
    entry = columns.get(str(statistic["groupBy"]))
    return isinstance(entry, Mapping) and entry.get("domain") == _NUMBER


def unread_reason(
    statistic: Mapping[str, Any], columns: Optional[Mapping[str, Any]] = None
) -> Optional[str]:
    """Why no rule reads *statistic*, when it is a form this version does not read."""
    if "groupByLink" in statistic:
        return BY_LINK
    if "per" in statistic:
        return PER_ENTITY
    if number_grouped(statistic, columns):
        return BY_NUMBER
    return None
