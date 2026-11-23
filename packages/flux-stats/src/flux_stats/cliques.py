# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Noisy clique marginals: a tuple of columns of one expose and a table of noisy counts.

``docs/spec/PROFILE-SPEC.md`` section 5a is the normative description. A ``cliqueMarginal/1`` is
the joint count table of two or more categorical columns (one column is a marginal the column's own
statistics already carry), released under noise by the profiler (:class:`flux_stats.noise.Noise`).
A ``cliqueMarginal/2`` (profile format 1-4-0) holds one to 32 columns, each a category, a number
or a timestamp, a number or a timestamp cut into bins. Cliques that share columns must agree on the
marginal of the columns they share, exactly, so that the numbers the engine draws from describe
one distribution and not several that disagree. This module reads a clique, checks that agreement,
and sums a clique onto a subset of its columns.

The pattern is the measurement step of the Private-PGM family (McKenna, Sheldon and Miklau,
https://github.com/ryan112358/private-pgm, Apache-2.0): marginals over cliques of columns,
released noisily, then made consistent. Nothing of that code is used.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from itertools import product
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple

from flux_stats.column_metrics import CATEGORY
from flux_stats.definitions import level_order
from flux_stats.noise import Accounting, Noise, read_noise
from flux_stats.spelling import read_decimal, read_texts, refuse, render_exact

__all__ = [
    "CLIQUE_DEFINITION",
    "CLIQUE_DEFINITION_2",
    "CliqueEntry",
    "MOST_CLIQUES",
    "MOST_CLIQUE_COLUMNS",
    "MOST_TABLE_CELLS",
    "check_consistency",
    "check_table_limits",
    "edge_text",
    "read_clique",
    "read_clique_2",
]

#: The identifier of a clique marginal, as a statistic's ``definition`` names one. A parameter
#: register cites a clique with it.
CLIQUE_DEFINITION = "cliqueMarginal/1"

#: The identifier of the clique that holds number and timestamp columns in bins (format 1-4-0).
CLIQUE_DEFINITION_2 = "cliqueMarginal/2"

#: The most cells one clique may hold, so a malformed document cannot ask for a table the reader
#: cannot hold. A clique over ten columns of ten levels would be 10^10 cells.
MOST_CELLS = 1_000_000

#: The limits a document of format 1-4-0 is held to, beyond :data:`MOST_CELLS`: the cliques of one
#: table, the columns of one clique and the cells over all of a table's cliques. A document that
#: goes beyond one is refused whole.
MOST_CLIQUES = 1_024
MOST_CLIQUE_COLUMNS = 32
MOST_TABLE_CELLS = 10_000_000

#: What the reader says of an estimated release, which this format does not read.
_NOT_READ = "estimated cliques are not read in this format; a table's cliques are measured"


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
    #: The definition the clique was read as: ``cliqueMarginal/1`` or ``cliqueMarginal/2``.
    definition: str = CLIQUE_DEFINITION
    #: One entry per column (format 1-4-0): the bin edges of a number or timestamp column, strictly
    #: increasing, and ``()`` for a category column. Empty for a ``cliqueMarginal/1``. For a binned
    #: column :attr:`levels` holds the canonical text of each bin's lower edge, so a cell is named
    #: by the same keys a ``groupByBins`` statistic writes.
    edges: Tuple[Tuple[Fraction, ...], ...] = ()

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

    def edges_of(self, column: str) -> Tuple[Fraction, ...]:
        """The bin edges of *column*, or ``()`` for a column that is not binned."""
        return self.edges[self.columns.index(column)] if self.edges else ()

    def binned(self, column: str) -> bool:
        """Whether *column* is a number or timestamp column cut into bins."""
        return bool(self.edges_of(column))

    @property
    def cell_count(self) -> int:
        return len(self.noisy)


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
        noise=read_noise(
            value["noise"],
            f"{where}.noise",
            accounting,
            l1=Fraction(1),
            l2_squared=Fraction(1),
        ),
    )


def edge_text(edge: Fraction) -> str:
    """The canonical decimal text of a bin edge: no exponent, no leading zeros, no trailing zeros
    after the point, ``0`` for zero."""
    return render_exact(edge)


def _decimal_edges(value: Any, where: str) -> Tuple[Fraction, ...]:
    if not isinstance(value, list):
        raise refuse(where, f"expected a list of bin edges, got {type(value).__name__}")
    edges = tuple(read_decimal(edge, f"{where}[{index}]") for index, edge in enumerate(value))
    if len(edges) < 2 or any(b <= a for a, b in zip(edges, edges[1:], strict=False)):
        raise refuse(where, "bin edges are at least two, strictly increasing")
    return edges


def read_clique_2(
    value: Any,
    where: str,
    *,
    expose_id: str,
    index: int,
    domains: Mapping[str, str],
    accounting: Accounting,
) -> CliqueEntry:
    """Read one ``cliqueMarginal/2`` of a table, measured, or refuse (``FLX-5036``).

    *domains* maps each column of the table to its domain. A clique holds one to
    :data:`MOST_CLIQUE_COLUMNS` distinct columns, each a category, a number or a timestamp, and at
    most :data:`MOST_CELLS` cells. A clique with an ``estimate`` is refused: this format reads
    measured cliques only.
    """
    if not isinstance(value, Mapping):
        raise refuse(where, f"expected an object, got {type(value).__name__}")
    if "estimate" in value:
        raise refuse(f"{where}.estimate", _NOT_READ)
    allowed = {"definition", "columns", "domains", "noisyCounts", "noise"}
    unknown = sorted({str(key) for key in value} - allowed)
    if unknown:
        raise refuse(where, f"unknown key {unknown[0]!r}")
    for key in ("definition", "columns", "domains", "noisyCounts", "noise"):
        if key not in value:
            raise refuse(where, f"missing key {key!r}")
    if value["definition"] != CLIQUE_DEFINITION_2:
        raise refuse(f"{where}.definition", f"a clique is {CLIQUE_DEFINITION_2}")
    columns = tuple(read_texts(value["columns"], f"{where}.columns"))
    if not 1 <= len(columns) <= MOST_CLIQUE_COLUMNS or len(set(columns)) != len(columns):
        raise refuse(
            f"{where}.columns",
            f"a clique names 1 to {MOST_CLIQUE_COLUMNS} distinct columns",
        )
    if any(name not in domains for name in columns):
        raise refuse(f"{where}.columns", "names a column that is no column of its table")
    declared = value["domains"]
    if not isinstance(declared, list) or len(declared) != len(columns):
        raise refuse(f"{where}.domains", "one domain for each column of the clique")
    levels: List[Tuple[str, ...]] = []
    edges: List[Tuple[Fraction, ...]] = []
    cells = 1
    for position, (name, one) in enumerate(zip(columns, declared, strict=True)):
        at = f"{where}.domains[{position}]"
        if not isinstance(one, Mapping) or len(one) != 1:
            raise refuse(at, "a domain is an object with 'levels' or 'bins'")
        key = next(iter(one))
        if domains[name] == CATEGORY:
            if key != "levels":
                raise refuse(at, "a category column lists its levels")
            names = tuple(read_texts(one["levels"], f"{at}.levels"))
            if not names or names != level_order(set(names)):
                raise refuse(f"{at}.levels", "levels are distinct and sorted, and at least one")
            levels.append(names)
            edges.append(())
            cells *= len(names)
        else:
            if key != "bins":
                raise refuse(at, "a number or timestamp column lists its bins")
            cut = _decimal_edges(one["bins"], f"{at}.bins")
            levels.append(tuple(edge_text(edge) for edge in cut[:-1]))
            edges.append(cut)
            cells *= len(cut) - 1
        if cells > MOST_CELLS:
            raise refuse(f"{where}.domains", f"a clique holds at most {MOST_CELLS} cells")
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
        noise=read_noise(
            value["noise"],
            f"{where}.noise",
            accounting,
            l1=Fraction(1),
            l2_squared=Fraction(1),
        ),
        definition=CLIQUE_DEFINITION_2,
        edges=tuple(edges),
    )


def check_table_limits(where: str, count: int, cells: int) -> None:
    """Refuse (``FLX-5036``) a table of format 1-4-0 that holds too many cliques or cells."""
    if count > MOST_CLIQUES:
        raise refuse(f"{where}.cliques", f"a table holds at most {MOST_CLIQUES} cliques")
    if cells > MOST_TABLE_CELLS:
        raise refuse(
            f"{where}.cliques", f"a table's cliques hold at most {MOST_TABLE_CELLS} cells together"
        )


def check_consistency(cliques: Sequence[CliqueEntry]) -> None:
    """Refuse (``FLX-5036``) two cliques that disagree on the columns they share, naming both.

    Two cliques of one table that share columns must list the same levels (or the same bin edges)
    for them and give the same noisy count to every combination of those levels once each is summed onto them. The
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
            if first.edges_of(column) != second.edges_of(column):
                raise refuse(
                    "profile.tables",
                    f"{first.label} and {second.label} list different bin edges of column "
                    f"{column!r}",
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
