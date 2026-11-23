# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The conditions a Simulation declares: groups of rows that hold fixed values (ADR 0028 section 10).

A Simulation declares them under the top-level ``conditions`` key of its namespace, in the FLUX
bundle and never in the FLUID contract. The key maps an ``exposeId`` to a list of groups:

.. code-block:: yaml

    extensions:
      com.agentics.flux/1:
        conditions:
          subscriptions:
            - values: { plan: premium, region: EU }
              count: 400
            - values: { plan: basic }
              count: 600

Each group says the values of some of the emit's columns, as the text of a level, and how many rows
hold them. The groups take their rows in the order listed, so the group of a row is a function of
its ordinal, and the emit's rows are exactly the groups' rows. One group may leave ``count`` out
when it is the emit's only group: it then covers every row of the emit.

**A group changes the rows the emit generates**, so the emit's groups are bound into ``H`` before
any row is drawn: :func:`bound_into_h` is the member the emit's entry for ``H`` gains, present
only when the emit has groups, so a bundle that declares none hashes exactly as before. ``G``
leaves the member out, as it leaves out facts, links and copies
(:func:`flux_engine.seam.emit.seam_reusable_digest`): the groups choose the weights a drawn cell
is searched in after the statistics are committed, and no committed state depends on them.

This module reads the declaration and checks what the declaration alone says. Whether a column can
be fixed, and whether a value is one of its levels, is checked where the emit's columns are known
(:func:`check_conditioned_columns`, called at ``plan`` and ``validate``). Kernel 0.1.0 refuses the
key (``FLX-3105``, :func:`flux_engine.stages.pipeline.check_conditions_declared`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import ErrorCode, FluxError, register

__all__ = [
    "CONDITIONS_KEY",
    "FLX_3640",
    "FLX_3641",
    "FLX_3642",
    "FLX_3643",
    "FLX_3644",
    "FLX_3645",
    "MOST_GROUPS",
    "ConditionColumn",
    "ConditionGroup",
    "bound_into_h",
    "check_conditioned_columns",
    "check_counts",
    "conditions_of",
    "read_conditions_block",
]

#: The top-level key of the namespace that declares conditions, and the member an emit's ``H`` gains.
CONDITIONS_KEY = "conditions"

#: The most groups one emit may declare.
MOST_GROUPS = 100_000

_GROUP_KEYS = ("values", "count")

FLX_3640: ErrorCode = register(
    "FLX-3640",
    "a conditions declaration is malformed",
    "A Simulation declares `conditions` as a mapping from an emitted `exposeId` to a list of "
    "groups, and a group is a mapping with the keys `values` and `count` and no other. `values` "
    "maps column names to the text of a level (write a number as text, as '1' or '2.50'), and "
    "is not empty. The key is read as exactly as the others of the namespace: an emit the "
    "Simulation does not emit, a column named twice in one group, a group that is not a mapping, "
    "and more than 100,000 groups for one emit are refused, naming the emit and the group.",
)

FLX_3641: ErrorCode = register(
    "FLX-3641",
    "the counts of a conditions declaration do not give the emit's rows",
    "A group's `count` is a whole number of at least 1 and is the number of rows that hold the "
    "group's values. A group may leave `count` out only when it is the emit's only group, and "
    "it then covers every row of the emit. The groups' counts are the emit's rows, so a declared "
    "row count that is not their sum is refused, and so is a count that is not a whole number.",
)

FLX_3642: ErrorCode = register(
    "FLX-3642",
    "a conditions group names a column or a level the emit does not have",
    "A group fixes columns of its own emit, each to one of the levels the column draws. A name "
    "that is not a column of the expose, and a value that is not a level of its column, are "
    "refused before any row is drawn, naming the emit, the group's position and the column. "
    "The levels a column draws are the ones its declaration gives it (its `levels`, its "
    "`support` or the keys of its weight tables, or the contract's `enum`).",
)

FLX_3643: ErrorCode = register(
    "FLX-3643",
    "a conditions group fixes a column that cannot be fixed",
    "A fixed column is a categorical column that draws on its own, from its own marginal or "
    "given other columns. A key or a foreign key, an identifier, a column a rule sets (a copied "
    "column included), a column a conditional link writes, a column drawn given a column of a "
    "linked row, a column that declares a `nullFraction` and a column that is not categorical "
    "are refused, because the value would be decided by something other than the group. So are "
    "conditions on an emit that declares a history, on a projected emit (a snapshot, events or "
    "periods of a history), and on an emit where a column that declares `allocation: exact` is "
    "fixed by a group, or is drawn given a fixed column, or a fixed column is drawn given it: "
    "the history or the allocation decides the rows, and the group would not hold. The "
    "message names the emit and says which case it is; for a column it names the group's "
    "position and the column, and for `allocation: exact` it says to remove it from the column "
    "or drop the group.",
)

FLX_3644: ErrorCode = register(
    "FLX-3644",
    "an emit with conditions is not drawn from declared statistics",
    "Conditioned rows are drawn from the weights of an emit's declared statistics, so the emit "
    "declares a fidelity block. An emit that takes its rows from the run's own events, and a "
    "reference expose, whose rows are published, have no cell whose weights a group could choose "
    "and are refused when they are given conditions.",
)

FLX_3645: ErrorCode = register(
    "FLX-3645",
    "a conditions group fixes a column whose sensitivity needs a grade a fixed value cannot show",
    "A column labelled with a FLUID `sensitivity` that sets a minimum grade of P3 or stronger "
    "(ADR 0020: `pii`, `phi`, `restricted`, `pseudonymized` and `tokenized` built in, and any "
    "label a Simulation raises to P3 with `privacy.minimumGrade`) holds values the run's grade is "
    "meant to cover. A value a group fixes is written in the bundle, so no grade can see it, and "
    "it could be a record's value copied in. The group is refused, naming the emit, the group's "
    "position, the column and its label. Leave the column to be drawn, or correct the label if it "
    "is wrong.",
)


@dataclass(frozen=True)
class ConditionGroup:
    """One group: the values it fixes, sorted by column, and its number of rows when it says."""

    #: ``(column, level text)`` pairs sorted by column name.
    values: Tuple[Tuple[str, str], ...]
    count: Optional[int] = None

    def as_member(self) -> Dict[str, Any]:
        """The group as the emit's ``conditions`` member holds it: no absent ``count``."""
        member: Dict[str, Any] = {"values": dict(self.values)}
        if self.count is not None:
            member["count"] = self.count
        return member


@dataclass(frozen=True)
class ConditionColumn:
    """What the checks need to know of one column of the emit.

    ``levels`` is ``None`` for a column that is not categorical. ``blocked`` says why the column
    cannot be fixed (a short phrase for the message), or is ``None`` when it can.
    """

    levels: Optional[Tuple[str, ...]]
    blocked: Optional[str] = None
    #: The column's sensitivity label when its minimum grade is P3 or stronger, else ``None``.
    sensitive: Optional[str] = None


def _group_values(raw: Any, at: str) -> Tuple[Tuple[str, str], ...]:
    """A ``values`` mapping as ``(column, level text)`` pairs sorted by column."""
    if not isinstance(raw, Mapping) or not raw:
        raise FluxError(
            FLX_3640, f"{at}.values is a non-empty mapping of column names to level texts"
        )
    found: Dict[str, str] = {}
    for column, text in raw.items():
        if not isinstance(column, str) or not column:
            raise FluxError(FLX_3640, f"{at}.values names a column that is not a non-empty string")
        name = nfc(column)
        if not isinstance(text, str):
            raise FluxError(
                FLX_3640,
                f"{at}.values.{name} is the text of a level: write it as a string, as 'EU' or '1'",
            )
        if name in found:
            raise FluxError(FLX_3640, f"{at}.values declares the column {name!r} twice")
        found[name] = nfc(text)
    return tuple(sorted(found.items()))


def _read_group(entry: Any, at: str) -> ConditionGroup:
    if not isinstance(entry, Mapping):
        raise FluxError(FLX_3640, f"{at} is a mapping with values and an optional count")
    unknown = sorted(str(key) for key in entry if str(key) not in _GROUP_KEYS)
    if unknown:
        raise FluxError(FLX_3640, f"{at} declares {unknown}; a group carries {list(_GROUP_KEYS)}")
    if "values" not in entry:
        raise FluxError(FLX_3640, f"{at} declares no values")
    values = _group_values(entry["values"], at)
    count: Optional[int] = None
    if "count" in entry:
        raw = entry["count"]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            raise FluxError(FLX_3641, f"{at}.count is a whole number of at least 1, not {raw!r}")
        count = raw
    return ConditionGroup(values=values, count=count)


def _read_groups(raw: Any, at: str) -> Tuple[ConditionGroup, ...]:
    """One emit's list of groups, or a refusal (``FLX-3640``, ``FLX-3641``) naming *at*."""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        raise FluxError(FLX_3640, f"{at} is a non-empty list of groups")
    if len(raw) > MOST_GROUPS:
        raise FluxError(
            FLX_3640, f"{at} holds {len(raw)} groups; an emit holds at most {MOST_GROUPS}"
        )
    groups = tuple(_read_group(entry, f"{at}[{index}]") for index, entry in enumerate(raw))
    if len(groups) > 1:
        for index, group in enumerate(groups):
            if group.count is None:
                raise FluxError(
                    FLX_3641,
                    f"{at}[{index}] leaves out its count, which only an emit's one group "
                    "may do: it then covers every row",
                )
    return groups


def read_conditions_block(
    block: Any, *, emits: FrozenSet[str], where: str
) -> Dict[str, Tuple[ConditionGroup, ...]]:
    """Read a ``conditions`` block as the groups of each emit, or refuse it, naming where.

    *emits* is every ``exposeId`` the Simulation emits. Refused with ``FLX-3640``: a block that is
    not a mapping, an emit the Simulation does not emit, a list that is empty or holds more than
    :data:`MOST_GROUPS` groups, a group that is not a mapping or carries a key other than
    ``values`` and ``count``, and a ``values`` that is not a non-empty mapping of column names to
    texts. Refused with ``FLX-3641``: a ``count`` that is not a whole number of at least 1, and a
    group without a ``count`` that is not its emit's only group. Whether the columns exist, can be
    fixed and take the values is checked where the emit's columns are known.
    """
    if not isinstance(block, Mapping):
        raise FluxError(FLX_3640, f"{where} is a mapping from an exposeId to a list of groups")
    found: Dict[str, Tuple[ConditionGroup, ...]] = {}
    for key, raw in block.items():
        if not isinstance(key, str) or not key:
            raise FluxError(FLX_3640, f"{where} names an emit that is not a non-empty string")
        emit = nfc(key)
        at = f"{where}.{emit}"
        if emit not in emits:
            raise FluxError(
                FLX_3640,
                f"{at}: {emit!r} is not an emit of this Simulation (it emits {sorted(emits)})",
            )
        if emit in found:
            raise FluxError(FLX_3640, f"{where} declares the emit {emit!r} twice")
        found[emit] = _read_groups(raw, at)
    return {emit: found[emit] for emit in sorted(found)}


def check_counts(
    groups: Sequence[ConditionGroup], *, emit: str, rows: Optional[int], where: str
) -> None:
    """Refuse (``FLX-3641``) a declared row count that is not the sum of the groups' counts.

    A group with no count covers every row, so an emit whose one group has none holds whatever
    its row count is. *rows* is ``None`` when the emit declares none.
    """
    counts = [group.count for group in groups]
    if rows is None or any(count is None for count in counts):
        return
    total = sum(count for count in counts if count is not None)
    if total != rows:
        raise FluxError(
            FLX_3641,
            f"{where}: the conditions of {emit!r} count {total} rows, and the emit declares "
            f"{rows}; the groups' counts are the emit's rows",
        )


def bound_into_h(
    conditions: Mapping[str, Sequence[ConditionGroup]], emit: str
) -> Optional[List[Dict[str, Any]]]:
    """The member of *emit*'s entry for ``H`` that its groups make, or ``None``.

    One ``{"values": {...}, "count": n}`` mapping per group, in the order the groups are
    declared (the order is the order of the rows); ``None`` when the emit has none, so the entry
    gains no key.
    """
    groups = conditions.get(nfc(emit))
    if not groups:
        return None
    return [group.as_member() for group in groups]


def conditions_of(extension: Optional[Mapping[str, Any]]) -> Tuple[ConditionGroup, ...]:
    """The groups an emit's bound extension holds (the member :func:`bound_into_h` made)."""
    raw = extension.get(CONDITIONS_KEY) if extension is not None else None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    found: List[ConditionGroup] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        values = item.get("values")
        count = item.get("count")
        found.append(
            ConditionGroup(
                values=tuple(sorted(dict(values).items())) if isinstance(values, Mapping) else (),
                count=count if isinstance(count, int) and not isinstance(count, bool) else None,
            )
        )
    return tuple(found)


def check_conditioned_columns(
    groups: Sequence[ConditionGroup],
    *,
    emit: str,
    columns: Mapping[str, ConditionColumn],
    where: str,
) -> None:
    """Refuse a group that names a column the emit lacks, cannot fix, or a value it never draws.

    *columns* says what is known of each column of the emit. ``FLX-3642`` for a name that is not
    a column and for a value that is not one of the column's levels; ``FLX-3643`` for a column
    that cannot be fixed, ``FLX-3645`` for one whose sensitivity needs grade P3. Each refusal names the emit, the group's position and the column.
    """
    for index, group in enumerate(groups):
        at = f"{where}: conditions of {emit!r}, group {index}"
        for column, text in group.values:
            _check_value(columns, column, text, at)


def _check_value(columns: Mapping[str, ConditionColumn], column: str, text: str, at: str) -> None:
    """Refuse a column the emit lacks or cannot fix, or a value it never draws (see the caller)."""
    known = columns.get(column)
    if known is None:
        raise FluxError(
            FLX_3642,
            f"{at}: {column!r} is not a column of the expose (its columns: {sorted(columns)})",
        )
    if known.blocked is not None:
        raise FluxError(
            FLX_3643, f"{at}: the column {column!r} cannot be fixed, it is {known.blocked}"
        )
    if known.sensitive is not None:
        raise FluxError(
            FLX_3645,
            f"{at}: the column {column!r} is labelled {known.sensitive}, which needs "
            "grade P3 or stronger; a value a group fixes is written in the bundle, and "
            "no grade can see it",
        )
    if known.levels is None:
        raise FluxError(
            FLX_3643, f"{at}: the column {column!r} cannot be fixed, it is not categorical"
        )
    if text not in known.levels:
        raise FluxError(
            FLX_3642,
            f"{at}: {text!r} is not a level of {column!r} (its levels: {list(known.levels)})",
        )
