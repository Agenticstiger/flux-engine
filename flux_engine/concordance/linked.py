# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A property grouped by a link: ``groupBy: <relationship>.<column>`` (SEAM-FIDELITY-SPEC §2).

The group key of a child row is the value of ``<column>`` at the row of the relationship's
target that the child's link names, found in the target's emitted rows by the key the
relationship pairs: ``fromColumns`` of the child with ``toColumns`` of the target. A row whose
link reaches no row, because a key column is null or no target row holds its key, has no
group, as a null grouping column has none (the group that is never compared). The grouping
column is declared in ``fidelity.columns`` under the same ``<relationship>.<column>`` name,
with its ``support``, and the profile publishes the expected statistic as a ``groupByLink``
(PROFILE-SPEC.md section 3).

Nothing here is random and nothing keeps state: the joined column is a function of the two
emits' rows.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_engine.concordance.decoded import DecodedRows
from flux_engine.concordance.errors import FLX_5031, FLX_8001
from flux_engine.concordance.predicate import FidelityBlock
from flux_engine.core.canonical import nfc
from flux_engine.core.errors import FluxError
from flux_engine.seam.keys import compile_keys
from flux_stats.fidelity import is_link_key
from flux_stats.profile import LinkGrouping

__all__ = ["join_linked_columns", "link_groupings", "linked_parents"]

#: The rows of one expose of the run, or ``(code, reason)`` of a refusal to read them.
ParentRows = Callable[[str], Tuple[Any, Optional[Tuple[str, str]]]]


def link_groupings(
    contract: Mapping[str, Any], expose_id: str, block: FidelityBlock, columns: Sequence[str]
) -> Dict[str, LinkGrouping]:
    """The grouping each ``<relationship>.<column>`` key of *block* stands for, by that key.

    *columns* are the expose's own column names: a key that is one of them is a column, not a
    link. Refuses (``FLX-5031``) a key whose relationship the contract does not declare.
    """
    own = {nfc(name) for name in columns}
    named = sorted(name for name in block.columns if name not in own)
    if not named:
        return {}
    keys = compile_keys(contract)
    relationships = {
        nfc(link.name): link
        for link in (keys.expose(expose_id).relationships if keys is not None else ())
    }
    found: Dict[str, LinkGrouping] = {}
    for name in named:
        relationship, _, column = name.partition(".")
        link = relationships.get(relationship)
        if link is None or not is_link_key(name, tuple(relationships)):
            raise FluxError(
                FLX_5031, f"column {name!r} is not a member of the expose, nor of a linked row"
            )
        found[name] = LinkGrouping(
            parent_table=nfc(link.target),
            parent_columns=tuple(nfc(item) for item in link.to_columns),
            child_columns=tuple(nfc(item) for item in link.from_columns),
            column=column,
        )
    return found


def linked_parents(groupings: Mapping[str, LinkGrouping]) -> Tuple[str, ...]:
    """The exposes whose rows the groupings read, sorted."""
    return tuple(sorted({grouping.parent_table for grouping in groupings.values()}))


def _cells(rows: Any, names: Sequence[str]) -> List[Tuple[Any, ...]]:
    if isinstance(rows, DecodedRows):
        columns = [rows.cells(name) for name in names]
        return list(zip(*columns, strict=True))
    return [tuple(row[name] for name in names) for row in rows]


def join_linked_columns(
    rows: Any,
    groupings: Mapping[str, LinkGrouping],
    parents: ParentRows,
) -> Any:
    """*rows* with one more column per grouping: the linked row's value, or ``None``.

    Refuses (``FLX-8001``) when the rows of a linked expose cannot be read or lack a column the
    link names.
    """
    if not groupings:
        return rows
    names = sorted(groupings)
    added: List[List[Any]] = []
    for name in names:
        grouping = groupings[name]
        found, problem = parents(grouping.parent_table)
        if problem is not None:
            raise FluxError(
                FLX_8001, f"the rows of {grouping.parent_table!r} cannot be read: {problem[1]}"
            )
        try:
            keys = _cells(found, grouping.parent_columns)
            values = _cells(found, (grouping.column,))
            own = _cells(rows, grouping.child_columns)
        except (KeyError, ValueError) as error:
            raise FluxError(
                FLX_8001, f"a column of the link {name!r} is not among the emitted columns"
            ) from error
        index: Dict[Tuple[Any, ...], Any] = {}
        for key, value in zip(keys, values, strict=True):
            index.setdefault(key, value[0])
        added.append([None if None in key else index.get(key) for key in own])
    extras = list(zip(*added, strict=True))
    if isinstance(rows, DecodedRows):
        return DecodedRows(
            [*rows.names, *names],
            [[*row, *extra] for row, extra in zip(rows.rows, extras, strict=True)],
        )
    return [
        {**row, **dict(zip(names, extra, strict=True))}
        for row, extra in zip(rows, extras, strict=True)
    ]
