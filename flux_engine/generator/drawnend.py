# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history's end column when its cells are drawn, as a projection's ``when`` reads it (ADR 0027).

The amendment of 2026-11-24 lets a ``when`` name the history's end column. A column that is not
tracked holds the end value on an ending version and a level its statistics draw on any other
(:class:`DrawnEnd`), so the answer of a ``when`` that reads it is known at plan time only where
every level the column can draw gives the same one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Container, Optional, Tuple

import numpy as np

from flux_engine.core.errors import FluxError
from flux_engine.expr2.check import Checked
from flux_engine.generator import rows as _rows
from flux_engine.generator.history import HistoryPlan
from flux_engine.seam.history import HistoryEnd

if TYPE_CHECKING:
    from flux_engine.generator.rows import GeneratorPlan

__all__ = ["DrawnEnd"]


class DrawnEnd:
    """The end column of a history whose cells are drawn: its end value and the levels it can draw.

    ``value`` is what the column holds on an ending version, ``levels`` the levels it can hold on
    any other, in the column's stored form (a level of weight zero is never drawn).
    """

    def __init__(
        self, history: "GeneratorPlan", chain: HistoryPlan, column: str, value: str
    ) -> None:
        held = history.column_plan(column)
        levels = held.statistics.levels
        names = (
            [name for name, weight in levels.items() if weight > 0]
            if levels is not None
            else list(held.column.enum_values or ())
        )
        self.column = column
        self.value = _rows._level_to_value(value, held.column)
        self.levels: Tuple[Any, ...] = tuple(
            _rows._level_to_value(name, held.column) for name in names
        )
        self._chain = chain
        self._ending: Optional["np.ndarray"] = None

    @classmethod
    def of(
        cls,
        history: "GeneratorPlan",
        chain: HistoryPlan,
        end: Optional[HistoryEnd],
        tracked: Container[str],
    ) -> Optional["DrawnEnd"]:
        """The drawn end column of *end*, or ``None`` when it has none or tracks it.

        Refused (``FLX-3403``) when the column is neither tracked nor carried: the history's
        first-version cells are the levels the column draws, and a column something else sets
        holds no such level.
        """
        if end is None or end.column is None or end.value is None or end.column in tracked:
            return None
        from flux_engine.generator.relations import FLX_3403, carried_columns

        if end.column not in carried_columns(history):
            raise FluxError(
                FLX_3403,
                f"history ({history.expose.expose_id}).end.column {end.column!r} is not carried: "
                "a rule, a link or the history sets it, so a when that reads it has no drawn "
                "level to read",
            )
        return cls(history, chain, end.column, end.value)

    def is_read_by(self, checked: Checked) -> bool:
        """Whether the condition *checked* reads the end column."""
        return any(read.column == self.column for read in checked.reads)

    def at(self, row: int, level: Any) -> Any:
        """The end value on an ending version row, else *level* (the one the entity's cells carry)."""
        if self._ending is None:
            self._ending = self._chain.ending_rows(np.arange(self._chain.rows, dtype=np.int64))
        return self.value if self._ending[row] else level
