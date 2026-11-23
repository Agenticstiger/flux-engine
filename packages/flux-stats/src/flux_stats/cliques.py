# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Noisy clique marginals: a tuple of columns of one expose and a table of noisy counts.

``docs/spec/PROFILE-SPEC.md`` section 5a is the normative description. A clique is the joint
count table of two or more categorical columns (one column is a marginal the column's own
statistics already carry), released under noise by the profiler (:class:`flux_stats.noise.Noise`).
Cliques that share columns must agree on the marginal of the columns they share, exactly, so that
the numbers the engine draws from describe one distribution and not several that disagree. This
module reads a clique, checks that agreement, and sums a clique onto a subset of its columns.

The pattern is the measurement step of the Private-PGM family (McKenna, Sheldon and Miklau,
https://github.com/ryan112358/private-pgm, Apache-2.0): marginals over cliques of columns,
released noisily, then made consistent. Nothing of that code is used.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from itertools import product
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple

from flux_stats.definitions import level_order
from flux_stats.noise import Accounting, Noise, read_noise
from flux_stats.spelling import read_decimal, read_texts, refuse

__all__ = ["CLIQUE_DEFINITION", "CliqueEntry", "check_consistency", "read_clique"]

#: The identifier of a clique marginal, as a statistic's ``definition`` names one. A parameter
#: register cites a clique with it.
CLIQUE_DEFINITION = "cliqueMarginal/1"

#: The most cells one clique may hold, so a malformed document cannot ask for a table the reader
#: cannot hold. A clique over ten columns of ten levels would be 10^10 cells.
MOST_CELLS = 1_000_000


@dataclass(frozen=True)
class CliqueEntry:
    """A clique: its columns, their levels, and one noisy count per combination of levels.

    ``noisy`` is in row-major order: the first column varies slowest and the last fastest, each
    column's levels in :func:`flux_stats.definitions.level_order`.
    """

    expose_id: str
    index: int
    columns: Tuple[str, ...]
    levels: Tuple[Tuple[str, ...], ...]
    noisy: Tuple[Fraction, ...]
    noise: Noise

    @property
    def label(self) -> str:
        """Where the clique sits and which columns it holds, for a refusal."""
        names = ", ".join(self.columns)
        return f"profile.tables.{self.expose_id}.cliques[{self.index}] (columns {names})"

    def cells(self) -> List[Tuple[Tuple[str, ...], Fraction]]:
        """Every cell: the levels of the columns, in order, and the noisy count."""
        return list(zip(product(*self.levels), self.noisy, strict=True))

    def marginal(self, keep: Sequence[str]) -> Dict[Tuple[str, ...], Fraction]:
        """The noisy counts summed onto the columns *keep*, keyed by their levels in that order."""
        positions = [self.columns.index(name) for name in keep]
        out: Dict[Tuple[str, ...], Fraction] = {}
        for combination, value in self.cells():
            key = tuple(combination[position] for position in positions)
            out[key] = out.get(key, Fraction(0)) + value
        return out

    def levels_of(self, column: str) -> Tuple[str, ...]:
        return self.levels[self.columns.index(column)]


def read_clique(
    value: Any,
    where: str,
    *,
    expose_id: str,
    index: int,
    categorical: Set[str],
    accounting: Accounting,
) -> CliqueEntry:
    """Read one clique of a table, or refuse (``FLX-5036``)."""
    if not isinstance(value, Mapping):
        raise refuse(where, f"expected an object, got {type(value).__name__}")
    allowed = {"definition", "columns", "levels", "noisyCounts", "noise"}
    unknown = sorted({str(key) for key in value} - allowed)
    if unknown:
        raise refuse(where, f"unknown key {unknown[0]!r}")
    for key in ("definition", "columns", "levels", "noisyCounts", "noise"):
        if key not in value:
            raise refuse(where, f"missing key {key!r}")
    if value["definition"] != CLIQUE_DEFINITION:
        raise refuse(f"{where}.definition", f"a clique is {CLIQUE_DEFINITION}")
    columns = tuple(read_texts(value["columns"], f"{where}.columns"))
    if len(columns) < 2 or len(set(columns)) != len(columns):
        raise refuse(f"{where}.columns", "a clique names at least two distinct columns")
    if not set(columns) <= categorical:
        raise refuse(
            f"{where}.columns", "names a column that is not a categorical column of its table"
        )
    declared = value["levels"]
    if not isinstance(declared, list) or len(declared) != len(columns):
        raise refuse(f"{where}.levels", "one list of levels for each column of the clique")
    levels = []
    for position, one in enumerate(declared):
        names = tuple(read_texts(one, f"{where}.levels[{position}]"))
        if not names or names != level_order(set(names)):
            raise refuse(
                f"{where}.levels[{position}]", "levels are distinct and sorted, and at least one"
            )
        levels.append(names)
    cells = 1
    for names in levels:
        cells *= len(names)
        if cells > MOST_CELLS:
            raise refuse(f"{where}.levels", f"a clique holds at most {MOST_CELLS} cells")
    counts = value["noisyCounts"]
    if not isinstance(counts, list) or len(counts) != cells:
        raise refuse(f"{where}.noisyCounts", f"one noisy count for each of the {cells} cells")
    return CliqueEntry(
        expose_id=expose_id,
        index=index,
        columns=columns,
        levels=tuple(levels),
        noisy=tuple(
            read_decimal(count, f"{where}.noisyCounts[{position}]")
            for position, count in enumerate(counts)
        ),
        noise=read_noise(value["noise"], f"{where}.noise", accounting),
    )


def check_consistency(cliques: Sequence[CliqueEntry]) -> None:
    """Refuse (``FLX-5036``) two cliques that disagree on the columns they share, naming both.

    Two cliques of one table that share columns must list the same levels for them and give the
    same noisy count to every combination of those levels once each is summed onto them. The
    comparison is exact (decimal strings read as rationals), and a pair that shares no column is
    not compared. Two cliques over the same set of columns are refused too: one table of a joint
    is released once.
    """
    for first, second in _pairs(cliques):
        if set(first.columns) == set(second.columns):
            raise refuse(
                "profile.tables",
                f"{first.label} and {second.label} are over the same columns",
            )
        shared = sorted(set(first.columns) & set(second.columns))
        if not shared:
            continue
        for column in shared:
            if first.levels_of(column) != second.levels_of(column):
                raise refuse(
                    "profile.tables",
                    f"{first.label} and {second.label} list different levels of column {column!r}",
                )
        if first.marginal(shared) != second.marginal(shared):
            raise refuse(
                "profile.tables",
                f"{first.label} and {second.label} disagree on the marginal of "
                f"{', '.join(shared)}",
            )


def _pairs(cliques: Sequence[CliqueEntry]) -> List[Tuple[CliqueEntry, CliqueEntry]]:
    return [
        (cliques[i], cliques[j]) for i in range(len(cliques)) for j in range(i + 1, len(cliques))
    ]
