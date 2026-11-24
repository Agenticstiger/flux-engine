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
and sums a clique onto a subset of its columns. It also reads a table's ``cliqueSelection``, which
says how the cliques were chosen and what choosing them spent.

The pattern is the measurement step of the Private-PGM family (McKenna, Sheldon and Miklau,
https://github.com/ryan112358/private-pgm, Apache-2.0): marginals over cliques of columns,
released noisily, then made consistent. Nothing of that code is used.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import product
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

from flux_stats.column_metrics import CATEGORY
from flux_stats.definitions import level_order
from flux_stats.noise import Accounting, Noise, PrivacyShare, read_noise, read_share
from flux_stats.spelling import normal_text, read_decimal, read_texts, refuse, render_exact

__all__ = [
    "CLIQUE_DEFINITION",
    "CLIQUE_DEFINITION_2",
    "CLIQUE_METHODS",
    "CliqueEntry",
    "CliqueSelection",
    "MOST_CLIQUES",
    "MOST_CLIQUE_COLUMNS",
    "MOST_TABLE_CELLS",
    "MOST_TABLE_WORK",
    "check_consistency",
    "check_table_limits",
    "edge_text",
    "read_clique",
    "read_clique_2",
    "read_clique_selection",
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

#: The widest common denominator, in bits, that a clique's counts are summed over as integers.
_MOST_SCALE_BITS = 256

#: The most work one table of format 1-4-0 may ask of the reader: the sum over its cliques of the
#: cells of the clique times the number of marginals the reader sums it onto (the one-way and
#: two-way ones it forms, and the ones the agreement check compares), plus, for each marginal two
#: or more cliques are compared on, the cells of that marginal for each of them. It follows the
#: document's structure alone, never its counts.
MOST_TABLE_WORK = 40_000_000

#: How a table's cliques were chosen (PROFILE-SPEC.md section 5a): by AIM or MST as private-pgm
#: implements them, named before the data was read, or by another method.
CLIQUE_METHODS: Tuple[str, ...] = ("aim", "mst", "fixed", "other")

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
    #: The marginals :meth:`prime` kept, by the sorted positions of the columns they sum onto.
    _sums: Dict[Tuple[int, ...], Dict[Tuple[str, ...], Fraction]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    @property
    def label(self) -> str:
        """Where the clique sits and which columns it holds, for a refusal."""
        names = ", ".join(self.columns)
        return f"profile.tables.{self.expose_id}.cliques[{self.index}] (columns {names})"

    def cells(self) -> List[Tuple[Tuple[str, ...], Fraction]]:
        """Every cell: the levels of the columns, in order, and the noisy count."""
        return list(zip(product(*self.levels), self.noisy, strict=True))

    def marginal(self, keep: Sequence[str]) -> Dict[Tuple[str, ...], Fraction]:
        """The noisy counts summed onto the columns *keep*, keyed by their levels in that order.

        A marginal the clique was primed for (:meth:`prime`) is read from what the one pass kept;
        any other is summed on the spot and not kept.
        """
        positions = [self.columns.index(name) for name in keep]
        wanted = tuple(sorted(positions))
        sums = self._sums.get(wanted) if len(set(positions)) == len(positions) else None
        if sums is None:
            return self._summed([tuple(positions)])[0]
        if positions == list(wanted):
            return sums
        order = [wanted.index(position) for position in positions]
        return {tuple(key[at] for at in order): value for key, value in sums.items()}

    def prime(self, sets: Sequence[Tuple[int, ...]]) -> None:
        """Sum the clique onto every set of column positions (each sorted) in one pass over its cells."""
        todo = [item for item in dict.fromkeys(sets) if item not in self._sums]
        if todo:
            self._sums.update(zip(todo, self._summed(todo), strict=True))

    def clear_sums(self) -> None:
        """Forget the marginals :meth:`prime` kept."""
        self._sums.clear()

    def _summed(self, sets: Sequence[Tuple[int, ...]]) -> List[Dict[Tuple[str, ...], Fraction]]:
        """One pass over the cells, summing onto each tuple of positions, keyed in that order.

        The counts are summed as integers over their common denominator, and each sum is made a
        fraction once; a common denominator that would be large is not used, and the fractions are
        added instead. Either way every sum is exact.
        """
        scale = 1
        for denominator in {value.denominator for value in self.noisy}:
            scale = scale * denominator // math.gcd(scale, denominator)
            if scale.bit_length() > _MOST_SCALE_BITS:
                scale = 0
                break
        if scale:
            values: Sequence[Any] = [
                value.numerator * (scale // value.denominator) for value in self.noisy
            ]
            zero: Any = 0
        else:
            values, zero = self.noisy, Fraction(0)
        getters = [_picker(item) if len(item) > 1 else None for item in sets]
        tables: List[Dict[Any, Any]] = [{} for _ in sets]
        for combination, value in zip(product(*self.levels), values, strict=True):
            for item, getter, table in zip(sets, getters, tables, strict=True):
                key: Any
                if getter is not None:
                    key = getter(combination)
                elif item:
                    key = combination[item[0]]
                else:
                    key = ()
                table[key] = table.get(key, zero) + value
        return [
            {
                (key,) if len(item) == 1 else key: Fraction(total, scale) if scale else total
                for key, total in table.items()
            }
            for item, table in zip(sets, tables, strict=True)
        ]

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


@dataclass(frozen=True)
class CliqueSelection:
    """How a table's cliques were chosen and released (format 1-4-0), and what that spent.

    ``selection`` is the share spent choosing the cliques and ``domain`` the share spent releasing
    levels or bin edges that were not public; each is ``None`` when the document states none. Both
    are parts of the account. The reader checks the arithmetic and the structure, and cannot check
    that the method ran or that a share is what it spent.
    """

    method: str
    producer: str
    selection: Optional[PrivacyShare]
    domain: Optional[PrivacyShare]
    published: str = "measured"


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
            l1=Fraction(accounting.reach(expose_id)),
            l2_squared=Fraction(accounting.reach(expose_id) ** 2),
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
    value = _clique_object(value, where)
    columns = _clique_columns(value, where, domains)
    declared = value["domains"]
    if not isinstance(declared, list) or len(declared) != len(columns):
        raise refuse(f"{where}.domains", "one domain for each column of the clique")
    levels: List[Tuple[str, ...]] = []
    edges: List[Tuple[Fraction, ...]] = []
    cells = 1
    for position, (name, one) in enumerate(zip(columns, declared, strict=True)):
        names, cut = _read_domain(one, domains[name] == CATEGORY, f"{where}.domains[{position}]")
        levels.append(names)
        edges.append(cut)
        cells *= len(names)
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
            l1=Fraction(accounting.reach(expose_id)),
            l2_squared=Fraction(accounting.reach(expose_id) ** 2),
        ),
        definition=CLIQUE_DEFINITION_2,
        edges=tuple(edges),
    )


def _clique_object(value: Any, where: str) -> Mapping[str, Any]:
    """The object of a ``cliqueMarginal/2``, with exactly its five keys and its definition."""
    if not isinstance(value, Mapping):
        raise refuse(where, f"expected an object, got {type(value).__name__}")
    for key in ("estimate", "measurements"):
        if key in value:
            raise refuse(f"{where}.{key}", _NOT_READ)
    if value.get("published") == "estimated":
        raise refuse(f"{where}.published", _NOT_READ)
    allowed = {"definition", "columns", "domains", "noisyCounts", "noise"}
    unknown = sorted({str(key) for key in value} - allowed)
    if unknown:
        raise refuse(where, f"unknown key {unknown[0]!r}")
    for key in ("definition", "columns", "domains", "noisyCounts", "noise"):
        if key not in value:
            raise refuse(where, f"missing key {key!r}")
    if value["definition"] != CLIQUE_DEFINITION_2:
        raise refuse(f"{where}.definition", f"a clique is {CLIQUE_DEFINITION_2}")
    return value


def _clique_columns(
    value: Mapping[str, Any], where: str, domains: Mapping[str, str]
) -> Tuple[str, ...]:
    """The distinct columns a clique names, each a column of its table."""
    columns = tuple(read_texts(value["columns"], f"{where}.columns"))
    if not 1 <= len(columns) <= MOST_CLIQUE_COLUMNS or len(set(columns)) != len(columns):
        raise refuse(
            f"{where}.columns",
            f"a clique names 1 to {MOST_CLIQUE_COLUMNS} distinct columns",
        )
    if any(name not in domains for name in columns):
        raise refuse(f"{where}.columns", "names a column that is no column of its table")
    return columns


def _read_domain(one: Any, category: bool, at: str) -> Tuple[Tuple[str, ...], Tuple[Fraction, ...]]:
    """The levels a column of a clique counts over, and its edges (none for a category column)."""
    if not isinstance(one, Mapping) or len(one) != 1:
        raise refuse(at, "a domain is an object with 'levels' or 'bins'")
    key = next(iter(one))
    if category:
        if key != "levels":
            raise refuse(at, "a category column lists its levels")
        names = tuple(read_texts(one["levels"], f"{at}.levels"))
        if not names or names != level_order(set(names)):
            raise refuse(f"{at}.levels", "levels are distinct and sorted, and at least one")
        return names, ()
    if key != "bins":
        raise refuse(at, "a number or timestamp column lists its bins")
    cut = _decimal_edges(one["bins"], f"{at}.bins")
    return tuple(edge_text(edge) for edge in cut[:-1]), cut


def check_table_limits(where: str, count: int, cells: int) -> None:
    """Refuse (``FLX-5036``) a table of format 1-4-0 that holds too many cliques or cells."""
    if count > MOST_CLIQUES:
        raise refuse(f"{where}.cliques", f"a table holds at most {MOST_CLIQUES} cliques")
    if cells > MOST_TABLE_CELLS:
        raise refuse(
            f"{where}.cliques", f"a table's cliques hold at most {MOST_TABLE_CELLS} cells together"
        )


def read_clique_selection(value: Any, where: str, *, accounting: Accounting) -> CliqueSelection:
    """Read a table's ``cliqueSelection`` (format 1-4-0), measured, or refuse (``FLX-5036``).

    The keys are ``method``, ``producer``, ``published`` and, optionally, ``selection`` and
    ``domain``, each of the last two ``{"privacyLoss": ...}`` in the account's measure. ``fixed``
    states no ``selection``: nothing was spent choosing cliques that were named before the data
    was read. ``published`` is ``measured``; ``estimated`` and ``measurements`` are refused.
    """
    if not isinstance(value, Mapping):
        raise refuse(where, f"expected an object, got {type(value).__name__}")
    if "measurements" in value:
        raise refuse(f"{where}.measurements", _NOT_READ)
    allowed = {"method", "producer", "selection", "domain", "published"}
    unknown = sorted({str(key) for key in value} - allowed)
    if unknown:
        raise refuse(where, f"unknown key {unknown[0]!r}")
    for key in ("method", "producer", "published"):
        if key not in value:
            raise refuse(where, f"missing key {key!r}")
    method = value["method"]
    if not isinstance(method, str) or method not in CLIQUE_METHODS:
        raise refuse(f"{where}.method", f"a method is one of {list(CLIQUE_METHODS)}")
    producer = normal_text(value["producer"], f"{where}.producer")
    if ":" not in producer:
        raise refuse(f"{where}.producer", "the producer is a URI")
    published = value["published"]
    if published == "estimated":
        raise refuse(f"{where}.published", _NOT_READ)
    if published != "measured":
        raise refuse(f"{where}.published", "is 'measured'")
    if method == "fixed" and "selection" in value:
        raise refuse(
            f"{where}.selection",
            "cliques named before the data was read spent nothing choosing them",
        )
    shares = {
        key: read_share(value[key], f"{where}.{key}", accounting)
        for key in ("selection", "domain")
        if key in value
    }
    return CliqueSelection(
        method=method,
        producer=producer,
        selection=shares.get("selection"),
        domain=shares.get("domain"),
    )


def _picker(item: Tuple[int, ...]) -> Callable[[Tuple[str, ...]], Tuple[str, ...]]:
    """A function taking the levels of a cell at the positions *item*, as a tuple."""
    if len(item) == 2:
        first, second = item
        return lambda cell: (cell[first], cell[second])
    return lambda cell: tuple([cell[position] for position in item])


def derived_sets(clique: CliqueEntry) -> List[Tuple[int, ...]]:
    """The sets of column positions (each sorted) the reader sums *clique* onto to form its statistics.

    Every column on its own, and every pair of columns that gives a two-way table: two categories,
    a category and a binned column, and two binned columns whose first (in the clique's order) is
    the one grouped.
    """
    found: Dict[Tuple[int, ...], None] = {}
    for position, name in enumerate(clique.columns):
        found[(position,)] = None
        edges = clique.edges_of(name)
        for other, neighbour in enumerate(clique.columns):
            if other == position:
                continue
            if not clique.binned(neighbour) or (edges and other > position):
                found[(min(position, other), max(position, other))] = None
    return list(found)


def _agreement(cliques: Sequence[CliqueEntry]) -> Dict[FrozenSet[str], List[CliqueEntry]]:
    """Refuse two cliques over one set of columns or listing different levels or edges of one column.

    Returns, for each set of columns that two cliques share, the cliques that are one end of such a
    pair. Equal marginals are equal to each other, so each set needs every one of its cliques
    compared with the first, not each pair.
    """
    whole: Dict[FrozenSet[str], CliqueEntry] = {}
    held: List[FrozenSet[str]] = []
    for clique in cliques:
        names = frozenset(clique.columns)
        first = whole.setdefault(names, clique)
        if first is not clique:
            raise refuse(
                "profile.tables", f"{first.label} and {clique.label} are over the same columns"
            )
        held.append(names)
    firsts: Dict[str, CliqueEntry] = {}
    for clique in cliques:
        for column in clique.columns:
            first = firsts.setdefault(column, clique)
            if first is clique:
                continue
            if first.levels_of(column) != clique.levels_of(column):
                raise refuse(
                    "profile.tables",
                    f"{first.label} and {clique.label} list different levels of column {column!r}",
                )
            if first.edges_of(column) != clique.edges_of(column):
                raise refuse(
                    "profile.tables",
                    f"{first.label} and {clique.label} list different bin edges of column "
                    f"{column!r}",
                )
    ends: Dict[FrozenSet[str], Dict[int, CliqueEntry]] = {}
    for i, names in enumerate(held):
        for j in range(i + 1, len(held)):
            shared = names & held[j]
            if shared:
                members = ends.setdefault(shared, {})
                members[i] = cliques[i]
                members[j] = cliques[j]
    return {shared: list(members.values()) for shared, members in ends.items()}


def _sums_wanted(
    cliques: Sequence[CliqueEntry], agreement: Mapping[FrozenSet[str], List[CliqueEntry]]
) -> Dict[int, Dict[Tuple[int, ...], None]]:
    """For each clique (by identity) the sets of positions it is summed onto, in order, once each."""
    wanted: Dict[int, Dict[Tuple[int, ...], None]] = {
        id(clique): dict.fromkeys(derived_sets(clique)) for clique in cliques
    }
    for shared, members in agreement.items():
        for clique in members:
            wanted[id(clique)][tuple(sorted(clique.columns.index(name) for name in shared))] = None
    return wanted


def table_work(
    cliques: Sequence[CliqueEntry], agreement: Mapping[FrozenSet[str], List[CliqueEntry]]
) -> int:
    """The work :data:`MOST_TABLE_WORK` limits, from the structure of the cliques alone."""
    wanted = _sums_wanted(cliques, agreement)
    work = sum(clique.cell_count * len(wanted[id(clique)]) for clique in cliques)
    for shared, members in agreement.items():
        size = 1
        for name in shared:
            size *= len(members[0].levels_of(name))
        work += size * len(members)
    return work


def check_consistency(cliques: Sequence[CliqueEntry], *, work_where: Optional[str] = None) -> None:
    """Refuse (``FLX-5036``) two cliques that disagree on the columns they share, naming both.

    Two cliques of one table that share columns must list the same levels (or the same bin edges)
    for them and give the same noisy count to every combination of those levels once each is summed onto them. The
    comparison is exact (decimal strings read as rationals), and a pair that shares no column is
    not compared. Two cliques over the same set of columns are refused too: one table of a joint
    is released once.

    Each clique is summed onto everything the table asks of it (the marginals compared and the
    ones the reader forms its statistics from) in one pass, and the marginals are kept for the
    caller to read through :meth:`CliqueEntry.marginal` and let go with
    :meth:`CliqueEntry.clear_sums`. With *work_where* (a document of format 1-4-0) a table whose
    structure asks for more than :data:`MOST_TABLE_WORK` is refused first, before any count is
    summed.
    """
    agreement = _agreement(cliques)
    wanted = _sums_wanted(cliques, agreement)
    if work_where is not None:
        check_table_work(work_where, table_work(cliques, agreement))
    for clique in cliques:
        clique.prime(list(wanted[id(clique)]))
    for shared, members in agreement.items():
        names = sorted(shared)
        first = members[0]
        base = first.marginal(names)
        for clique in members[1:]:
            if clique.marginal(names) != base:
                raise refuse(
                    "profile.tables",
                    f"{first.label} and {clique.label} disagree on the marginal of "
                    f"{', '.join(names)}",
                )


def check_table_work(where: str, work: int) -> None:
    """Refuse (``FLX-5036``) a table of format 1-4-0 whose cliques ask for too much work."""
    if work > MOST_TABLE_WORK:
        raise refuse(
            f"{where}.cliques",
            f"a table's cliques ask for more than {MOST_TABLE_WORK} cell visits to sum and compare",
        )
