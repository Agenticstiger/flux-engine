# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Drawing rows: SEAM-FIDELITY-SPEC §1's generator, one column at a time.

§1: "Every draw uses the §4 kernel samplers on the path ``("sim", sim_id,
"gen", exposeId, "row", rowIndex, column)``. Rows are produced in
``rowIndex`` order."

Three properties follow from that sentence and are pinned by tests.

**Every value is independently addressed.** One stream per (row, column),
so the value in a cell depends on the run root, the expose, the row index
and the column name -- and on nothing else. Generating column by column
instead of row by row produces the same table; adding a column to the
contract changes ``H`` and therefore every stream, but generating with a
different *column order* under the same contract does not. A column
declared ``given`` another keeps its own stream; what it reads from the
other column is the level that column drew in the same row, which picks
the bin or level weights it draws with (:func:`_require_conditionals`).

**Identifiers are derived, never drawn.** §1's identifier row says
"derived deterministically from ``(H, ordinal)`` -- never random", and
:func:`identifier_value` does exactly that: it hashes ``H`` and the
column's own path, with no reference to the run's seed. An identifier
column is therefore stable across a sweep, where the seed changes and
every drawn column moves.

**No sampler here is new.** Every draw goes through
:mod:`flux_engine.core.rng`: ``categorical``, ``bounded_uint``,
``truncated_normal``, ``exponential``, ``poisson``, ``bernoulli``. The
uniform draws are integer draws on an exact grid -- the number of grid
points between the declared bounds -- so a uniform ``DECIMAL(18,4)`` is a
uniform over the values that type can actually hold, not a rounded real.

Values leave this module in the classes
:mod:`flux_engine.seam.conform` renders: ``str``, ``int``, ``bool``,
``Decimal``, ``datetime.date``, and ``None``. Rendering, framing and
digesting stay where KERNEL-SPEC §8 and §9 put them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple, Union

from flux_engine.core.canonical import sha256_hex
from flux_engine.core.clock import parse_rfc3339_ms
from flux_engine.core.digest import DigestRef, digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.core.lrm import FLX_5020, WEIGHT_SUM_TOLERANCE
from flux_engine.core.rng import (
    DrawStream,
    bernoulli,
    bounded_uint,
    categorical,
    equal_weights,
    kernel_context,
    nfc_sorted_items,
    truncated_normal,
)
from flux_engine.core.rng import exponential as draw_exponential
from flux_engine.core.rng import poisson as draw_poisson
from flux_engine.core.seedtree import SeedTreeRoot, digest_bytes, path_label
from flux_engine.generator.families import (
    FLX_3401,
    ColumnType,
    Family,
    choose_family,
    classify_column,
)
from flux_engine.generator.paths import (
    CellStreams,
    StreamSource,
    generator_column_path,
    resolve_stream_source,
)
from flux_engine.generator.statistics import (
    DATE_EPOCH_ORDINAL,
    FLX_3402,
    ColumnStatistics,
    statistics_from_fidelity,
)
from flux_engine.seam.contract import CompiledColumn, CompiledExpose, LogicalKind

__all__ = [
    "GENERATOR_LABEL",
    "GENERATOR_VERSION",
    "ColumnPlan",
    "GeneratorPlan",
    "IDENTIFIER_DEFAULT_LENGTH",
    "build_plan",
    "clear_memos",
    "generate_column",
    "generate_row_slice",
    "generate_rows",
    "identifier_value",
]

#: Domain-separation label for the identifier derivation. Part of the
#: pre-image, so changing it moves every identifier column.
GENERATOR_LABEL = b"flux-gen/1"

#: The generator's own version, hashed into ``generatorDigest``.
#: SEAM-FIDELITY-SPEC §8: "generator (engine ``kernelVersion``) ->
#: ``generatorDigest`` moves -> re-materialise D', new record against the
#: same state."
GENERATOR_VERSION = "flux-gen/1"

#: Hex characters in a derived identifier when the declaration names no
#: length. 32 hex characters is 128 bits, the collision floor a UUID has.
IDENTIFIER_DEFAULT_LENGTH = 32

_NUL = b"\x00"

#: How many distinct declarations each memo below keeps. A plan declares one
#: per column; the bound only stops a long-lived process from growing
#: without limit.
_MEMO_SIZE = 1024

#: A finite ``Decimal`` exactly, as ``(sign, digits, exponent)``: what a memo
#: below is keyed on when its result reads the value's exponent, so that
#: ``Decimal("1.0")`` and ``Decimal("1.00")`` never share an entry.
_Exact = Tuple[int, Tuple[int, ...], int]


def clear_memos() -> None:
    """Forget every declaration the generator's memos remembered.

    Safe at any time, since no row depends on what is remembered;
    :func:`flux_engine.stages.pipeline.execute` clears at the end of every
    run, together with :func:`flux_engine.core.rng.clear_memos`.
    """
    for memo in (
        _seam_bytes,
        _grid_of,
        _inner_bin_top,
        _declared_levels,
        _declared_support,
        _bin_levels,
        _weighted_levels,
    ):
        memo.cache_clear()


def _exact(value: Decimal) -> _Exact:
    sign, digits, exponent = value.as_tuple()
    return sign, digits, int(exponent)


#: Declared types whose identifier is formatted as a UUID.
_UUID_TYPES = ("uuid", "uniqueidentifier", "guid")

#: The one ``decimal`` context every value in this module is computed in.
#:
#: KERNEL-SPEC §4 evaluates the continuous samplers "in the ``decimal``
#: module at 28 digits", and :func:`~flux_engine.core.rng.kernel_context`
#: is that context with every field pinned -- precision, rounding, exponent
#: range and traps. The generator's *own* arithmetic (a bounded normal's
#: mean and spread derived from a declared range, the equal weights of a
#: declared support, the quantisation onto a column's scale) has to run in
#: it too: a caller who has set ``decimal.getcontext().prec`` for reasons
#: of their own would otherwise move every drawn value, and a generated
#: dataset whose contents depend on ambient interpreter state cannot be
#: replayed. :func:`_kernel_arithmetic` is applied at :func:`_cell`, the
#: single funnel every generated value passes through, so no public entry
#: point can bypass it.
#:
#: ``localcontext`` installs a *copy*, so one module-level context is safe
#: to share across threads and calls.
_KERNEL_CONTEXT = kernel_context()

#: §1's default stddev for a bounded normal whose declaration carries a
#: range but no spread: the declared range spans six standard deviations,
#: so the declared bounds sit at +/-3 sigma. A function of the declaration,
#: not a number invented beside it.
_RANGE_SIGMAS = 6

#: The bin index is used as a categorical level key, and
#: :func:`~flux_engine.core.rng.categorical` orders levels by NFC-sorted
#: key. Zero-padding to a fixed width makes that sort the numeric order, so
#: bin 2 sits between bin 1 and bin 10 rather than after them.
_BIN_KEY_WIDTH = 6

#: The largest bound :func:`~flux_engine.core.rng.bounded_uint` addresses.
#: Its Lemire threshold is ``2**64 % n``; for ``n > 2**64`` that is ``2**64``
#: itself, which no 64-bit word can reach, so every word in the draw budget
#: is rejected and the documented fallback returns the last word -- uniform
#: over the first ``2**64`` points of the grid rather than over all of them.
#: :func:`_require_drawable_grid` refuses such a grid at plan time.
_MAX_ADDRESSABLE_GRID_POINTS = 1 << 64

#: Decimal places a categorical weight derived from a ratio is rounded to.
#: Fixed and documented: a share such as 1/3 has no finite decimal spelling,
#: and rounding it at a place the run chose would make the run's own
#: arithmetic part of the distribution.
_WEIGHT_DIGITS = 28


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


def identifier_value(
    seam_digest: str,
    *,
    sim_id: str,
    expose_id: str,
    row_index: int,
    column: str,
    length: int = IDENTIFIER_DEFAULT_LENGTH,
    as_uuid: bool = False,
) -> str:
    """Derive an identifier from ``(H, ordinal)`` -- SEAM-FIDELITY-SPEC §1.

    ``sha256(b"flux-gen/1" + NUL + H_bytes + NUL + label(path))``, truncated
    to *length* hex characters. The run's seed is **not** in the pre-image:
    §1 says the derivation is from ``(H, ordinal)``, and that is what makes
    an identifier column stable across a sweep whose every other column
    moves.

    With *as_uuid*, the first 32 hex characters are formatted as an RFC 9562
    UUID with version 8 -- the version that standard reserves for a
    vendor-defined derivation, which is exactly what this is. Claiming
    version 4 (random) or 5 (name-based SHA-1) would be claiming a
    derivation this is not.
    """
    _require_identifier_shape(length, as_uuid)
    path = generator_column_path(sim_id, expose_id, row_index, column)
    seam = _seam_bytes(seam_digest)
    return _identifier_of_label(seam, path_label(path), length=length, as_uuid=as_uuid)


def _require_identifier_shape(length: int, as_uuid: bool) -> None:
    if not 1 <= length <= 64:
        raise FluxError(FLX_3402, f"identifier length must lie in [1, 64], got {length}")
    if as_uuid and length < 32:
        raise FluxError(FLX_3402, "a UUID-shaped identifier needs at least 32 hex characters")


@lru_cache(maxsize=64)
def _seam_bytes(seam_digest: str) -> bytes:
    """The 32 bytes of ``H``, parsed once per digest rather than once per row."""
    return digest_bytes(seam_digest)


def _identifier_of_label(seam: bytes, label: bytes, *, length: int, as_uuid: bool) -> str:
    """:func:`identifier_value`'s derivation, from ``H``'s bytes and the cell's path label.

    The caller has checked the shape (:func:`_require_identifier_shape`).
    """
    material = GENERATOR_LABEL + _NUL + seam + _NUL + label
    digits = sha256_hex(material)
    if not as_uuid:
        return digits[:length]
    # RFC 9562: version nibble at position 12, variant bits at position 16.
    body = digits[:32]
    versioned = body[:12] + "8" + body[13:16] + _variant_nibble(body[16]) + body[17:]
    return (
        f"{versioned[:8]}-{versioned[8:12]}-{versioned[12:16]}-{versioned[16:20]}-{versioned[20:]}"
    )


def _variant_nibble(nibble: str) -> str:
    """Force the RFC 9562 variant bits (``10xx``) while keeping the low bits."""
    return format((int(nibble, 16) & 0x3) | 0x8, "x")


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnPlan:
    """How one column will be generated: its type, its family, its parameters."""

    column: CompiledColumn
    column_type: ColumnType
    family: Optional[Family]
    statistics: ColumnStatistics

    def as_document(self) -> Dict[str, Any]:
        """The plan for this column, as canonical-ready JSON."""
        return {
            "name": self.column.name,
            "type": self.column.type_name,
            "required": self.column.required,
            "columnType": self.column_type.value,
            "family": None if self.family is None else self.family.value,
            "statistics": self.statistics.as_document(),
        }


@dataclass(frozen=True)
class GeneratorPlan:
    """Everything that decides the rows, and nothing that does not.

    :meth:`digest` is SEAM-FIDELITY-SPEC §6's ``generatorDigest``: the
    ``jcs-document`` digest of the plan below. It covers the generator's
    version, the seam digest ``H``, the row count and every column's family
    and declared parameters -- so §8's rule holds mechanically ("generator
    ... -> ``generatorDigest`` moves -> re-materialise D'"), and so does its
    converse: a change that leaves this document alone leaves the rows
    alone.
    """

    sim_id: str
    expose: CompiledExpose
    seam_digest: str
    rows: int
    columns: Tuple[ColumnPlan, ...]
    time_bounds: Optional[Tuple[int, int]] = None

    def as_document(self) -> Dict[str, Any]:
        """The plan document ``generatorDigest`` is taken over."""
        return {
            "generatorVersion": GENERATOR_VERSION,
            "simId": self.sim_id,
            "exposeId": self.expose.expose_id,
            "H": self.seam_digest,
            "rows": self.rows,
            "timeBounds": (
                None
                if self.time_bounds is None
                else {"startMs": self.time_bounds[0], "endMs": self.time_bounds[1]}
            ),
            "columns": [plan.as_document() for plan in self.columns],
        }

    def digest(self) -> DigestRef:
        """``generatorDigest`` (SEAM-FIDELITY-SPEC §6)."""
        return digest_of_document(
            self.as_document(), subject=f"generatorDigest:{self.expose.expose_id}"
        )

    def column_plan(self, name: str) -> ColumnPlan:
        """Return the plan for the column called *name*."""
        for plan in self.columns:
            if plan.column.name == name:
                return plan
        raise FluxError(FLX_3401, f"no column plan for {name!r}")


def _time_bounds_ms(time_bounds: Optional[Mapping[str, Any]]) -> Optional[Tuple[int, int]]:
    """Parse a Simulation's ``timeBounds`` into the clock's integer milliseconds."""
    if time_bounds is None:
        return None
    start = time_bounds.get("start")
    end = time_bounds.get("end")
    if not isinstance(start, str) or not isinstance(end, str):
        raise FluxError(FLX_3402, "timeBounds declares a start and an end as RFC 3339 strings")
    start_ms = parse_rfc3339_ms(start)
    end_ms = parse_rfc3339_ms(end)
    if start_ms >= end_ms:
        raise FluxError(FLX_3402, "timeBounds.start must precede timeBounds.end")
    return start_ms, end_ms


def build_plan(
    expose: CompiledExpose,
    *,
    sim_id: str,
    seam_digest: str,
    rows: int,
    fidelity: Optional[Mapping[str, Any]] = None,
    time_bounds: Optional[Mapping[str, Any]] = None,
) -> GeneratorPlan:
    """Resolve every column to a family and its parameters, before any draw.

    Fails closed, and fails **early**: the whole table is planned before
    the first row is generated, so a column the declaration cannot
    parameterise is a plan-time refusal rather than a half-written stream.
    That is KERNEL-SPEC §12's "compile fails closed" applied to the
    generator.

    ``rows`` is SEAM-FIDELITY-SPEC §1's declared row count. §1's fallback
    -- "the population size of the referenced Segment when the emit is
    journey-driven" -- is a property of the world, so the caller supplies
    the number and this function does not guess one.
    """
    if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
        raise FluxError(FLX_3402, f"row count must be a non-negative integer, got {rows!r}")
    statistics = statistics_from_fidelity(expose, fidelity)
    bounds = _time_bounds_ms(time_bounds)

    plans: List[ColumnPlan] = []
    for column in expose.columns:
        column_statistics = _with_time_bounds(column, statistics[column.name], bounds)
        column_type = classify_column(column, column_statistics)
        family = choose_family(column, column_statistics, column_type)
        _require_parameters(column, column_statistics, column_type, family)
        plans.append(
            ColumnPlan(
                column=column,
                column_type=column_type,
                family=family,
                statistics=column_statistics,
            )
        )
    _require_conditionals(plans)
    return GeneratorPlan(
        sim_id=sim_id,
        expose=expose,
        seam_digest=seam_digest,
        rows=rows,
        columns=tuple(plans),
        time_bounds=bounds,
    )


def _with_time_bounds(
    column: CompiledColumn,
    statistics: ColumnStatistics,
    bounds: Optional[Tuple[int, int]],
) -> ColumnStatistics:
    """Fill a DATE/TIMESTAMP column's absent range from the run's time bounds.

    §1's default for that row is "uniform within ``timeBounds``", so the
    simulation's own declared bounds are the declaration the range comes
    from when the column declares none of its own. The substituted range is
    recorded in the statistics, so it appears in ``generatorDigest`` and a
    reader can see which bounds were used.
    """
    kind = column.logical_type.kind
    if kind not in (LogicalKind.DATE, LogicalKind.TIMESTAMP):
        return statistics
    if statistics.has_range or bounds is None:
        return statistics
    start_ms, end_ms = bounds
    if kind is LogicalKind.DATE:
        low, high = Decimal(start_ms // 86_400_000), Decimal(end_ms // 86_400_000)
    else:
        low, high = Decimal(start_ms), Decimal(end_ms)
    return ColumnStatistics(
        **{
            **statistics.__dict__,
            "range_min": low,
            "range_max": high,
            "range_min_text": str(low),
            "range_max_text": str(high),
        }
    )


def _require_parameters(
    column: CompiledColumn,
    statistics: ColumnStatistics,
    column_type: ColumnType,
    family: Optional[Family],
) -> None:
    """Refuse a column whose family has no parameters it can be built from."""
    name = column.name
    if column_type is ColumnType.IDENTIFIER:
        if statistics.null_fraction is not None:
            raise FluxError(FLX_3402, f"{name}: an identifier column has no nullFraction")
        return
    if column.required and statistics.null_fraction is not None:
        raise FluxError(
            FLX_3402,
            f"{name}: the contract marks this column required, so a declared "
            "nullFraction would produce rows the seam refuses",
        )
    if family is Family.CATEGORICAL:
        if (
            statistics.levels is None
            and not statistics.support
            and statistics.level_weights_given is None
        ):
            # Both support spellings resolve to a non-empty level list or
            # are refused where they are read
            # (:func:`flux_engine.concordance.predicate.read_support_levels`),
            # so reaching here means the column declared neither.
            raise FluxError(
                FLX_3402,
                f"{name}: a categorical needs declared levels or a declared support",
            )
        if statistics.max_cardinality is not None:
            size = len(_level_names(statistics))
            if size > statistics.max_cardinality:
                raise FluxError(
                    FLX_3402,
                    f"{name}: {size} declared levels exceed the declared "
                    f"maxCardinality of {statistics.max_cardinality}",
                )
        _require_renderable_levels(column, statistics)
    elif family is Family.BOUNDED_NORMAL:
        if not statistics.has_range:
            raise FluxError(FLX_3402, f"{name}: a bounded normal needs a declared range")
        if statistics.stddev is not None and statistics.stddev < 0:
            raise FluxError(FLX_3402, f"{name}: stddev is negative")
    elif family is Family.RATE:
        if column_type is ColumnType.INT:
            if statistics.mean is None:
                raise FluxError(
                    FLX_3402, f"{name}: a count rate needs a declared mean (the Poisson mean)"
                )
            if statistics.mean < 0:
                raise FluxError(FLX_3402, f"{name}: mean is negative")
        else:
            if statistics.rate is None:
                raise FluxError(FLX_3402, f"{name}: a rate family needs a declared rate")
            if statistics.rate <= 0:
                raise FluxError(FLX_3402, f"{name}: rate must be above zero")
            if column_type is ColumnType.DATE_TIMESTAMP and statistics.range_min is None:
                raise FluxError(
                    FLX_3402,
                    f"{name}: an inter-arrival rate needs a declared range.min to count from",
                )
    elif family is Family.UNIFORM_EMPIRICAL:
        if statistics.bins is None and not statistics.has_range:
            raise FluxError(FLX_3402, f"{name}: a uniform needs a declared range or bins")
        if (
            statistics.bins is not None
            and statistics.bin_weights is None
            and statistics.bin_weights_given is None
        ):
            raise FluxError(
                FLX_3402,
                f"{name}: declared bins need declared binWeights; the edges alone say "
                "where the bins are, not how much mass each carries",
            )
        _require_drawable_uniform(column, statistics)


def _require_renderable_levels(column: CompiledColumn, statistics: ColumnStatistics) -> None:
    """Refuse a level the column's own value class cannot hold, before any row is drawn.

    A categorical cell draws a level and reads it with :func:`_level_to_value`.
    A level such as ``"p"`` on a ``DECIMAL`` column used to pass the plan and
    fail on the first row with a bare ``decimal.InvalidOperation``. Each level
    is read here once, in the kernel's own context, exactly as a cell reads it.
    The levels are the ones the column can draw (:func:`_level_names`): the
    declared ``levels``, the levels of ``levelWeightsGiven``, or else the
    declared support.
    """
    names = _level_names(statistics)
    with localcontext(_KERNEL_CONTEXT):
        for level in names:
            try:
                _level_to_value(level, column)
            except (ArithmeticError, ValueError) as error:
                raise FluxError(
                    FLX_3402,
                    f"{column.name}: level {level!r} is not a valid "
                    f"{column.logical_type.kind.value} value; declare levels the "
                    "column's type can hold",
                ) from error


def _require_conditionals(plans: Sequence[ColumnPlan]) -> None:
    """Refuse a ``given`` this generator cannot draw, before any row is drawn.

    A conditional column draws with the weights declared for the level its
    ``given`` column drew in the same row: a histogram column with
    ``binWeightsGiven`` (P(bin | level), from a grouped histogram's
    (group, bin) counts), or a categorical column with ``levelWeightsGiven``
    (P(level | level), from a two-way count table). The column it is given
    is a categorical with no declared nulls, so each row has exactly one
    level to look up, and the weight table names exactly the levels that
    column can draw.

    A given column may itself be drawn given another, so the columns form a
    forest: each column has at most one parent, a cycle is refused, and a
    histogram column is always a leaf, because a bin is not a level. That is
    the shape of a Chow-Liu tree (Chow and Liu, 1968), the structure MST
    (McKenna, Miklau and Sheldon, 2021) selects, without its privacy noise.
    """
    by_name = {plan.column.name: plan for plan in plans}
    for plan in plans:
        given = plan.statistics.given
        if given is None:
            continue
        name = plan.column.name
        statistics = plan.statistics
        parent = by_name.get(given)
        if parent is None:
            raise FluxError(
                FLX_3402, f"{name}: given names {given!r}, which is not a column of this expose"
            )
        if statistics.bin_weights_given is not None:
            if plan.family is not Family.UNIFORM_EMPIRICAL or statistics.bins is None:
                raise FluxError(
                    FLX_3402,
                    f"{name}: binWeightsGiven is drawn by a histogram column (declared bins)",
                )
            table, declared = "binWeightsGiven", sorted(statistics.bin_weights_given)
        else:
            if plan.family is not Family.CATEGORICAL:
                raise FluxError(
                    FLX_3402, f"{name}: levelWeightsGiven is drawn by a categorical column"
                )
            assert statistics.level_weights_given is not None  # statistics_for pairs them
            table, declared = "levelWeightsGiven", sorted(statistics.level_weights_given)
            # A declared support may list levels the committed state never
            # holds; drawing a subset of it is legal, drawing outside it is not.
            outside = sorted(set(_level_names(statistics)) - set(statistics.support or ()))
            if statistics.support and outside:
                raise FluxError(
                    FLX_3402,
                    f"{name}: levelWeightsGiven draws {outside}, outside the declared "
                    f"support {sorted(statistics.support)}",
                )
        if parent.family is not Family.CATEGORICAL:
            raise FluxError(
                FLX_3402,
                f"{name}: given column {given!r} is not categorical, so it has no level "
                "to condition on",
            )
        if parent.statistics.null_fraction is not None:
            raise FluxError(
                FLX_3402,
                f"{name}: given column {given!r} declares a nullFraction, and a null "
                "has no level to condition on",
            )
        drawn = sorted(_level_names(parent.statistics))
        if declared != drawn:
            raise FluxError(
                FLX_3402,
                f"{name}: {table} declares levels {declared}; {given!r} draws {drawn}",
            )
    _require_acyclic(by_name)


def _require_acyclic(by_name: Mapping[str, ColumnPlan]) -> None:
    """Refuse ``given`` links that loop back, naming the loop.

    Each column has at most one ``given``, so following the links from any
    column either ends at a column with none or returns to one already seen.
    """
    for start in sorted(by_name):
        seen: List[str] = []
        current: Optional[str] = start
        while current is not None:
            if current in seen:
                loop = seen[seen.index(current) :] + [current]
                raise FluxError(FLX_3402, f"given forms a cycle: {' -> '.join(loop)}")
            seen.append(current)
            current = by_name[current].statistics.given


def _level_names(statistics: ColumnStatistics) -> Tuple[str, ...]:
    """The levels a categorical column can draw, however they are declared.

    ``levelWeightsGiven`` names the same levels under every given level
    (:func:`flux_engine.generator.statistics.statistics_for` refuses it
    otherwise), so its first mapping names them all.
    """
    if statistics.level_weights_given is not None:
        return tuple(next(iter(statistics.level_weights_given.values())))
    if statistics.levels is not None:
        return tuple(statistics.levels)
    return tuple(statistics.support or ())


def _bin_top(edges: Sequence[Decimal], index: int, step: Decimal) -> Decimal:
    """The highest grid point :func:`_draw_histogram` may draw inside bin *index*.

    §2's bins "partition the declared range", so a bin is half-open and its
    top grid point is one step below the next edge -- **except the last**,
    which is closed at the top. That is the rule
    :func:`flux_engine.concordance.boundary.statistic.bin_index` already
    applies when it decides which bin a record falls in ("a record exactly
    at the declared maximum is binned rather than dropped"), so taking a
    step off the last edge here too would leave the generator unable to
    emit the declared maximum while the check went on placing it inside the
    last bin -- a top bin systematically short of one grid point of mass.

    Computed on integer units for the same reason :func:`_uniform_on_grid`
    is: an edge wider than the context's precision must not move when a
    step is taken off it.
    """
    top_edge = edges[index + 1]
    if index == len(edges) - 2:
        return top_edge
    return _inner_bin_top(_exact(edges[index]), _exact(top_edge), _exact(step))


@lru_cache(maxsize=_MEMO_SIZE)
def _inner_bin_top(low: _Exact, top_edge: _Exact, step: _Exact) -> Decimal:
    """:func:`_bin_top` for a bin below the last, once per bin.

    Keyed on the exact representations, because the exponent the units are
    counted in is read from them.
    """
    low_edge, top, unit = Decimal(low), Decimal(top_edge), Decimal(step)
    exponent = _common_exponent(low_edge, top, unit)
    below = _decimal_from_units(_grid_units(top, exponent) - _grid_units(unit, exponent), exponent)
    # A bin narrower than one grid point holds exactly its lower edge.
    return below if below >= low_edge else low_edge


def _require_drawable_uniform(column: CompiledColumn, statistics: ColumnStatistics) -> None:
    """Check every grid a uniform draw will address, before any row is drawn.

    The declared range, and -- when the column declares a histogram -- each
    bin in turn, since :func:`_draw_histogram` draws uniformly inside the
    bin it picks. See :func:`_require_drawable_grid`.
    """
    step = _grid_step(column) if column.logical_type.kind is LogicalKind.DECIMAL else Decimal(1)
    if statistics.bins is not None:
        edges = statistics.bins
        for index in range(len(edges) - 1):
            top = _bin_top(edges, index, step)
            if top < edges[index]:
                continue  # a bin narrower than one grid point holds its lower edge
            _require_drawable_grid(
                column.name, edges[index], top, step, f"bin {index} of the declared histogram"
            )
        return
    if statistics.has_range:
        low, high = statistics.range_min, statistics.range_max
        assert low is not None and high is not None  # has_range
        _require_drawable_grid(column.name, low, high, step, "the declared range")


# ---------------------------------------------------------------------------
# The draws
# ---------------------------------------------------------------------------


def _grid_step(column: CompiledColumn) -> Decimal:
    scale = column.logical_type.scale
    return Decimal(1).scaleb(-(scale or 0))


def _common_exponent(*values: Decimal) -> int:
    """The most negative decimal exponent among *values*, capped at zero.

    Scaling every operand by ``10 ** -exponent`` turns the whole grid into
    integers, which is what :func:`_grid_units` and :func:`_grid_value` use
    to do their arithmetic without a ``decimal`` context in the loop.
    """
    exponents = [int(value.as_tuple().exponent) for value in values]
    return min([*exponents, 0])


def _grid_units(value: Decimal, exponent: int) -> int:
    """*value* as an exact integer number of ``10 ** exponent`` units."""
    scaled = Fraction(value) * Fraction(10) ** (-exponent)
    if scaled.denominator != 1:  # pragma: no cover - exponent is the minimum of the operands
        raise FluxError(FLX_3402, "a declared bound does not lie on its own decimal grid")
    return int(scaled)


def _decimal_from_units(units: int, exponent: int) -> Decimal:
    """Rebuild ``units * 10 ** exponent`` exactly, with no context rounding.

    Built through :class:`~decimal.Decimal`'s tuple constructor rather than
    ``scaleb`` or a multiplication, because those consult the ambient
    context's precision and would round a value wider than it.
    """
    digits = tuple(int(character) for character in str(abs(units)))
    return Decimal((0 if units >= 0 else 1, digits, exponent))


def _grid_point_count(low: Decimal, high: Decimal, step: Decimal) -> int:
    """How many grid points ``[low, high]`` holds, exactly.

    Integer arithmetic on the common decimal exponent, so the count is
    right at every magnitude the declared type admits -- see
    :func:`_uniform_on_grid` for why the obvious decimal spelling is not.
    """
    exponent = _common_exponent(low, high, step)
    step_units = _grid_units(step, exponent)
    if step_units < 1:  # pragma: no cover - the step is 10**-scale, never zero
        raise FluxError(FLX_3402, "a uniform needs a positive grid step")
    return (_grid_units(high, exponent) - _grid_units(low, exponent)) // step_units + 1


def _require_drawable_grid(
    name: str, low: Decimal, high: Decimal, step: Decimal, what: str
) -> None:
    """Refuse a grid with no points, or more than one bounded draw can address.

    :func:`~flux_engine.core.rng.bounded_uint` is Lemire's method over a
    single 64-bit word, so its bound must fit in one: its rejection
    threshold is ``2**64 % n``, which for ``n > 2**64`` equals ``2**64`` and
    is therefore above every 64-bit value the test compares against. Every
    word in the budget is rejected and the documented fallback returns the
    last word unchanged -- a value that is still inside ``[0, n)`` but drawn
    uniformly over only the first ``2**64`` of its points.

    That is a distribution nobody declared, so a grid too fine or too wide
    to address is refused **at plan time** rather than drawn from. The
    remedy is a coarser scale or a narrower declared range; a generator
    that quietly narrowed the range instead would produce a dataset whose
    marginal is not the one the fidelity check is specified over.
    """
    points = _grid_point_count(low, high, step)
    if points < 1:  # pragma: no cover - statistics_for refuses an inverted range
        raise FluxError(FLX_3402, f"{name}: {what} spans no grid point")
    if points > _MAX_ADDRESSABLE_GRID_POINTS:
        raise FluxError(
            FLX_3402,
            f"{name}: {what} spans {points} grid points at scale {-int(step.as_tuple().exponent)}, "
            f"more than the {_MAX_ADDRESSABLE_GRID_POINTS} a single bounded draw addresses. "
            "Declare a narrower range or a coarser scale.",
        )


def _uniform_on_grid(stream: DrawStream, low: Decimal, high: Decimal, step: Decimal) -> Decimal:
    """Uniform over the grid points of ``[low, high]``, by one bounded draw.

    Integer arithmetic throughout, and exactly so. The grid point count is
    computed on :class:`~fractions.Fraction`\\ s and the chosen point is
    rebuilt from integer units, so neither the count nor the value is
    rounded to the ``decimal`` context's precision. That matters at the top
    of the declared type's domain: a ``DECIMAL(38,4)`` range spans more than
    the 28 significant digits KERNEL-SPEC §4 evaluates the samplers at, and
    computing ``(high - low) / step`` in that context would round the span
    *outwards* -- admitting grid points above the declared ``range.max``,
    which is the one thing a declared range is for.

    A uniform over a ``DECIMAL(18,4)`` range is therefore a uniform over the
    values that type can actually hold, not a rounded real.
    """
    low_units, step_units, points, exponent = _grid_of(_exact(low), _exact(high), _exact(step))
    return _decimal_from_units(low_units + bounded_uint(stream, points) * step_units, exponent)


@lru_cache(maxsize=_MEMO_SIZE)
def _grid_of(low: _Exact, high: _Exact, step: _Exact) -> Tuple[int, int, int, int]:
    """The integer grid :func:`_uniform_on_grid` draws on: low point, step, size, exponent.

    A function of the three declared values alone, so it is worked out once
    per column rather than once per draw. Keyed on their exact
    representations, because the exponent the grid is counted in is read
    from them. A refusal is raised rather than remembered.
    """
    low_value, high_value, step_value = Decimal(low), Decimal(high), Decimal(step)
    exponent = _common_exponent(low_value, high_value, step_value)
    low_units = _grid_units(low_value, exponent)
    step_units = _grid_units(step_value, exponent)
    points = _grid_point_count(low_value, high_value, step_value)
    if points < 1 or points > _MAX_ADDRESSABLE_GRID_POINTS:  # pragma: no cover
        # build_plan already refused this grid (_require_drawable_uniform).
        # The guard is kept so that no future caller of this function can
        # reach bounded_uint's silent fallback by skipping the plan.
        raise FluxError(FLX_3402, f"a grid of {points} points is not drawable in one draw")
    return low_units, step_units, points, exponent


def _column_levels(statistics: ColumnStatistics) -> Mapping[str, Decimal]:
    """What a categorical column draws from: its levels, renormalised, once per declaration.

    The declared levels, or equal weights over a declared support, with any
    rendering residue folded in (:func:`_renormalised`), memoised on the
    declaration itself. The memo works in the kernel's own context, so the
    context of whichever caller asks first cannot be the one it remembers.
    """
    if statistics.levels is not None:
        return _declared_levels(tuple(statistics.levels.items()))
    return _declared_support(tuple(statistics.support or ()))


@lru_cache(maxsize=_MEMO_SIZE)
def _declared_levels(items: Tuple[Tuple[str, Decimal], ...]) -> Mapping[str, Decimal]:
    with localcontext(_KERNEL_CONTEXT):
        return MappingProxyType(_renormalised(dict(items)))


@lru_cache(maxsize=_MEMO_SIZE)
def _declared_support(support: Tuple[str, ...]) -> Mapping[str, Decimal]:
    with localcontext(_KERNEL_CONTEXT):
        return MappingProxyType(_renormalised(equal_weights(support)))


def _bin_keys(count: int) -> Tuple[str, ...]:
    return tuple(str(index).zfill(_BIN_KEY_WIDTH) for index in range(count))


def _draw_histogram(
    stream: DrawStream,
    statistics: ColumnStatistics,
    column: CompiledColumn,
    given_level: Optional[str] = None,
) -> Decimal:
    """Pick a declared bin by its weight, then draw uniformly inside it.

    A conditional column picks with the weights declared for *given_level*,
    the level its ``given`` column drew in the same row.
    """
    bins = statistics.bins or ()
    if statistics.bin_weights_given is not None:
        if given_level is None:
            raise FluxError(
                FLX_3402, f"{column.name}: a conditional column is drawn with a given level"
            )
        weights = statistics.bin_weights_given[given_level]
    else:
        weights = statistics.bin_weights or ()
    table = _bin_levels(tuple(weights))
    if table is None:
        raise FluxError(FLX_3402, f"{column.name}: binWeights sum to zero")
    levels, positions = table
    chosen = categorical(stream, levels)
    index = positions[chosen]
    step = _grid_step(column)
    low = bins[index]
    high = _bin_top(bins, index, step)
    return _uniform_on_grid(stream, low, high, step)


@lru_cache(maxsize=_MEMO_SIZE)
def _bin_levels(
    weights: Tuple[Decimal, ...],
) -> Optional[Tuple[Mapping[str, Decimal], Mapping[str, int]]]:
    """A histogram's bin weights as categorical levels, and each level's bin, once per set.

    ``None`` when the weights sum to zero, which the caller refuses with the
    column's name. Evaluated in the kernel's own context, like
    :func:`_column_levels`.
    """
    total = sum((Fraction(weight) for weight in weights), Fraction(0))
    if total <= 0:
        return None
    keys = _bin_keys(len(weights))
    normalised = {
        key: _fraction_as_decimal(Fraction(weight) / total)
        for key, weight in zip(keys, weights, strict=True)
    }
    with localcontext(_KERNEL_CONTEXT):
        levels = _renormalised(normalised)
    return MappingProxyType(levels), MappingProxyType({key: i for i, key in enumerate(keys)})


@lru_cache(maxsize=_MEMO_SIZE)
def _weighted_levels(items: Tuple[Tuple[str, Decimal], ...]) -> Mapping[str, Decimal]:
    """One given level's ``levelWeightsGiven`` mapping as drawable levels, once per set.

    Counts or shares, normalised exactly as :func:`_bin_levels` normalises a
    histogram's bin weights: exact shares, rendered once at a fixed number of
    places, the residue folded by :func:`_renormalised`.
    """
    total = sum((Fraction(weight) for _, weight in items), Fraction(0))
    normalised = {key: _fraction_as_decimal(Fraction(weight) / total) for key, weight in items}
    with localcontext(_KERNEL_CONTEXT):
        return MappingProxyType(_renormalised(normalised))


def _categorical_levels(
    statistics: ColumnStatistics, column: CompiledColumn, given_level: Optional[str]
) -> Mapping[str, Decimal]:
    """What a categorical cell draws from: its declared levels, or its row of ``levelWeightsGiven``."""
    if statistics.level_weights_given is None:
        return _column_levels(statistics)
    if given_level is None:
        raise FluxError(
            FLX_3402, f"{column.name}: a conditional column is drawn with a given level"
        )
    return _weighted_levels(tuple(sorted(statistics.level_weights_given[given_level].items())))


def _fraction_as_decimal(value: Fraction) -> Decimal:
    """Render an exact rational as a decimal at :data:`_WEIGHT_DIGITS` places.

    A share such as ``1/3`` has no finite decimal spelling, so a categorical
    weight derived from a ratio of declared counts has to be rounded
    somewhere. It is rounded here, once, at a fixed and documented number
    of places, and :func:`_renormalised` then folds the whole residue into
    the last level -- which is where
    :func:`~flux_engine.core.rng.categorical` absorbs its own rounding
    anyway. Both steps are exact decimal arithmetic, so two runs round
    identically.

    The context is derived from the kernel's own (:data:`_KERNEL_CONTEXT`)
    rather than from the caller's, so the rounding mode as well as the
    precision is the engine's: ``localcontext()`` with no argument would
    inherit an ambient ``ROUND_UP`` and quietly re-weight the levels.
    """
    with localcontext(_KERNEL_CONTEXT) as context:
        context.prec = _WEIGHT_DIGITS + 10
        return (Decimal(value.numerator) / Decimal(value.denominator)).quantize(
            Decimal(1).scaleb(-_WEIGHT_DIGITS)
        )


def _renormalised(weights: Mapping[str, Decimal]) -> Mapping[str, Decimal]:
    """Fold a *rendering* residue into the last level -- and refuse anything larger.

    ``categorical`` refuses weights further than
    :data:`~flux_engine.core.lrm.WEIGHT_SUM_TOLERANCE` from one, and a ratio
    of exact rationals rendered as decimals (:func:`_fraction_as_decimal`)
    can land just short of one. That sub-tolerance residue is what this
    function exists to absorb.

    A residue larger than the tolerance is a different thing entirely: a
    declared ``levels`` block that does not sum to one. Absorbing it would
    silently draw one level at a weight nobody declared -- and would defeat
    ``categorical``'s own FLX-5020 check, which runs *after* this function
    on the generator path and could therefore never fire. So it is refused
    here with the same code, rather than renormalised away.

    The residue goes to the last key in **NFC-sorted** order, using
    ``categorical``'s own ordering key, so the level that absorbs the
    decimal residue is exactly the level ``categorical``'s last-level rule
    absorbs its threshold remainder into. A raw code-point sort would
    disagree with it whenever a level is written in a decomposed form.

    One exception: a negative residue that the last level cannot absorb. A
    level declared with no mass (a ``binWeights`` entry of ``0``, say) would
    be left with a negative weight, which ``categorical`` refuses with
    FLX-5021 on every draw. The residue goes instead to the NFC-last level
    that can take it and stay non-negative. Only weights that every draw
    used to refuse take this branch, so no row a run could already produce
    moves.
    """
    ordered = [key for key, _ in nfc_sorted_items(weights)]
    total = sum((Decimal(weights[key]) for key in ordered), Decimal(0))
    residue = Decimal(1) - total
    if abs(residue) > WEIGHT_SUM_TOLERANCE:
        raise FluxError(
            FLX_5020,
            f"declared weights sum to {total}, which is further than "
            f"{WEIGHT_SUM_TOLERANCE} from 1",
        )
    absorber = ordered[-1]
    if weights[absorber] + residue < 0:
        # Some level always qualifies: the residue is at most the tolerance,
        # and the weights sum to about one. The default keeps the old refusal
        # should that ever stop holding.
        absorber = next((key for key in reversed(ordered) if weights[key] + residue >= 0), absorber)
    adjusted = dict(weights)
    adjusted[absorber] = weights[absorber] + residue
    return adjusted


def _quantise(value: Decimal, column: CompiledColumn) -> Decimal:
    """Round *value* onto the column's declared scale.

    The context is widened to the column's own declared domain first.
    ``quantize`` signals ``InvalidOperation`` when the result would carry
    more digits than the context's precision, and KERNEL-SPEC §4's 28 digits
    are fewer than a ``DECIMAL(38,4)`` column is entitled to hold -- so
    quantising in the kernel's own context would raise on a value the
    contract declares to be legal. The same widening
    :func:`flux_engine.seam.conform._render_decimal` applies at the seam,
    for the same reason and to the same width, so a value this module
    produces is one the renderer accepts.

    Widening the precision does not loosen the rounding: the value is
    already on the grid in every path that reaches here, and the rounding
    mode stays the kernel's.
    """
    scale = column.logical_type.scale or 0
    precision = column.logical_type.precision or 0
    with localcontext(_KERNEL_CONTEXT) as context:
        context.prec = max(_KERNEL_CONTEXT.prec, precision + scale + 40)
        return value.quantize(Decimal(1).scaleb(-scale))


def _clamp(value: Decimal, statistics: ColumnStatistics) -> Decimal:
    if statistics.range_min is not None and value < statistics.range_min:
        return statistics.range_min
    if statistics.range_max is not None and value > statistics.range_max:
        return statistics.range_max
    return value


def _draw_value(plan: ColumnPlan, stream: DrawStream, given_level: Optional[str] = None) -> Any:
    """Draw one value for a non-identifier column."""
    column, statistics, family = plan.column, plan.statistics, plan.family
    kind = column.logical_type.kind

    if family is Family.CATEGORICAL:
        level = categorical(stream, _categorical_levels(statistics, column, given_level))
        return _level_to_value(level, column)

    if family is Family.BOUNDED_NORMAL:
        low = statistics.range_min
        high = statistics.range_max
        assert low is not None and high is not None  # _require_parameters
        mean = statistics.mean if statistics.mean is not None else (low + high) / 2
        stddev = (
            statistics.stddev
            if statistics.stddev is not None
            else (high - low) / Decimal(_RANGE_SIGMAS)
        )
        scale = column.logical_type.scale or 0
        drawn = truncated_normal(stream, mean=mean, stddev=stddev, lo=low, hi=high, scale=scale)
        return int(drawn) if kind is LogicalKind.INT else drawn

    if family is Family.RATE:
        return _draw_rate(plan, stream)

    if statistics.bins is not None:
        drawn = _draw_histogram(stream, statistics, column, given_level)
        return int(drawn) if kind is LogicalKind.INT else _from_grid(drawn, column)

    low = statistics.range_min
    high = statistics.range_max
    assert low is not None and high is not None  # _require_parameters
    step = _grid_step(column) if kind is LogicalKind.DECIMAL else Decimal(1)
    drawn = _uniform_on_grid(stream, low, high, step)
    return _from_grid(drawn, column)


def _draw_rate(plan: ColumnPlan, stream: DrawStream) -> Any:
    """§1's rate family: Poisson for INT counts, exponential otherwise."""
    column, statistics = plan.column, plan.statistics
    kind = column.logical_type.kind
    if plan.column_type is ColumnType.INT:
        assert statistics.mean is not None  # _require_parameters
        drawn_count = draw_poisson(stream, mean=statistics.mean)
        return int(_clamp(Decimal(drawn_count), statistics))
    assert statistics.rate is not None  # _require_parameters
    scale = column.logical_type.scale or 0
    waited = draw_exponential(stream, rate=statistics.rate, scale=max(scale, 0))
    if kind is LogicalKind.DECIMAL:
        return _quantise(_clamp(waited, statistics), column)
    assert statistics.range_min is not None  # _require_parameters
    return _from_grid(_clamp(statistics.range_min + waited, statistics), column)


def _from_grid(value: Decimal, column: CompiledColumn) -> Any:
    """Turn a grid value into the class KERNEL-SPEC §9 renders for the column."""
    kind = column.logical_type.kind
    if kind is LogicalKind.INT:
        return int(value.to_integral_value())
    if kind is LogicalKind.TIMESTAMP:
        return int(value.to_integral_value())
    if kind is LogicalKind.DATE:
        return date.fromordinal(int(value.to_integral_value()) + DATE_EPOCH_ORDINAL)
    return _quantise(value, column)


def _level_to_value(level: str, column: CompiledColumn) -> Any:
    """Read a declared categorical level as the column's own value class."""
    kind = column.logical_type.kind
    if kind is LogicalKind.STRING:
        return level
    if kind is LogicalKind.BOOL:
        if level not in ("true", "false"):
            raise FluxError(
                FLX_3402,
                f"{column.name}: a BOOL level is 'true' or 'false', not {level!r}",
            )
        return level == "true"
    number = Decimal(level)
    if kind is LogicalKind.INT:
        return int(number)
    if kind is LogicalKind.DECIMAL:
        return _quantise(number, column)
    return _from_grid(number, column)  # pragma: no cover - families table forbids the rest


_Source = Union[CellStreams, StreamSource]


def _cell(
    plan: ColumnPlan,
    source: _Source,
    sim_id: str,
    expose_id: str,
    row_index: int,
    h: str,
    given_level: Optional[str] = None,
) -> Any:
    """Produce one cell: an identifier, a declared null, or a draw.

    The single funnel every generated value passes through, and therefore
    the one place the ``decimal`` context is pinned. See
    :data:`_KERNEL_CONTEXT`: without this, a caller's ambient precision or
    rounding mode would reach the derived parameters of a bounded normal,
    the equal weights of a declared support and every quantisation onto a
    column's scale, and the same plan under the same root would produce
    different rows in two processes.
    """
    with localcontext(_KERNEL_CONTEXT):
        return _cell_in_context(plan, source, sim_id, expose_id, row_index, h, given_level)


def _cell_in_context(
    plan: ColumnPlan,
    source: _Source,
    sim_id: str,
    expose_id: str,
    row_index: int,
    h: str,
    given_level: Optional[str] = None,
) -> Any:
    """:func:`_cell`'s body, with the kernel's ``decimal`` context installed.

    A :class:`~flux_engine.generator.paths.CellStreams` *source* is the
    generator's own, built for this plan's ``sim_id`` and expose: the cell's
    label comes from it whole. Any other source is asked for the stream at
    the cell's path.
    """
    column = plan.column
    if plan.column_type is ColumnType.IDENTIFIER:
        length = plan.statistics.identifier_length or IDENTIFIER_DEFAULT_LENGTH
        as_uuid = column.declared_type.strip().lower() in _UUID_TYPES
        if isinstance(source, CellStreams):
            _require_identifier_shape(length, as_uuid)
            try:
                label = source.label(row_index, column.name)
            except (FluxError, UnicodeError):
                label = None
            if label is not None:
                return _identifier_of_label(_seam_bytes(h), label, length=length, as_uuid=as_uuid)
            # A part of the path is refused. identifier_value checks the parts
            # and the seam digest in its own order, so the refusal raised is
            # the one it raises.
        return identifier_value(
            h,
            sim_id=sim_id,
            expose_id=expose_id,
            row_index=row_index,
            column=column.name,
            length=length,
            as_uuid=as_uuid,
        )
    if isinstance(source, CellStreams):
        stream = source.stream_at(row_index, column.name)
    else:
        stream = source.stream(generator_column_path(sim_id, expose_id, row_index, column.name))
    if plan.statistics.null_fraction is not None:
        # The null draw comes first and always happens, so the value draw
        # lands on the same stream position whether or not the cell is null.
        is_null = bernoulli(stream, plan.statistics.null_fraction)
        value = _draw_value(plan, stream, given_level)
        return None if is_null else value
    return _draw_value(plan, stream, given_level)


def _given_cell(
    plan: ColumnPlan,
    source: _Source,
    sim_id: str,
    expose_id: str,
    row_index: int,
    given_level: Optional[str] = None,
) -> Tuple[Any, str]:
    """A given column's cell, and the level it drew, for the columns drawn given it.

    Exactly the cell :func:`_cell` produces for this column (the same stream
    at its first word, the same categorical draw, with *given_level* when the
    column is itself drawn given another), because
    :func:`_require_conditionals` lets only a categorical with no declared
    nulls be given. The level is returned as well as the value: a level such
    as ``"1.50"`` and the value it renders to are different keys.
    """
    column = plan.column
    with localcontext(_KERNEL_CONTEXT):
        if isinstance(source, CellStreams):
            stream = source.stream_at(row_index, column.name)
        else:
            stream = source.stream(generator_column_path(sim_id, expose_id, row_index, column.name))
        level = categorical(stream, _categorical_levels(plan.statistics, column, given_level))
        return _level_to_value(level, column), level


def _given_columns(plan: GeneratorPlan) -> FrozenSet[str]:
    """The columns some other column of *plan* is drawn given."""
    names = set()
    for column_plan in plan.columns:
        if column_plan.statistics.given is not None:
            names.add(column_plan.statistics.given)
    return frozenset(names)


def _draw_order(plan: GeneratorPlan) -> Tuple[int, ...]:
    """Column indices in an order that draws every given column before those given it.

    By depth in the ``given`` forest (:func:`_require_conditionals` refused
    every cycle), then by declared position, so the order is a function of
    the plan alone.
    """
    by_name = {column_plan.column.name: column_plan for column_plan in plan.columns}

    def depth(column_plan: ColumnPlan) -> int:
        count, given = 0, column_plan.statistics.given
        while given is not None:
            count, given = count + 1, by_name[given].statistics.given
        return count

    depths = [depth(column_plan) for column_plan in plan.columns]
    return tuple(sorted(range(len(plan.columns)), key=lambda index: (depths[index], index)))


def _conditional_row(
    plan: GeneratorPlan,
    order: Sequence[int],
    givens: FrozenSet[str],
    source: _Source,
    row_index: int,
) -> Tuple[Any, ...]:
    """One row of a plan with conditional columns, drawn in *order*.

    Every cell is still drawn once, from its own stream, so the order the
    cells are drawn in changes no value. Drawing parents before children only
    puts each given level in hand before the columns that read it. The row is
    returned in the expose's declared column order.
    """
    sim_id, expose_id, h = plan.sim_id, plan.expose.expose_id, plan.seam_digest
    values: List[Any] = [None] * len(plan.columns)
    levels: Dict[str, str] = {}
    for index in order:
        column_plan = plan.columns[index]
        given = column_plan.statistics.given
        given_level = None if given is None else levels[given]
        name = column_plan.column.name
        if name in givens:
            values[index], levels[name] = _given_cell(
                column_plan, source, sim_id, expose_id, row_index, given_level
            )
        else:
            values[index] = _cell(column_plan, source, sim_id, expose_id, row_index, h, given_level)
    return tuple(values)


def _stream_source(
    plan: GeneratorPlan, root: Optional[SeedTreeRoot], source: Optional[StreamSource]
) -> _Source:
    """The pass's streams: the caller's *source*, or the generator's own over *root*.

    The generator's own (:class:`~flux_engine.generator.paths.CellStreams`)
    gives each cell the stream :class:`~flux_engine.generator.paths.GeneratorStreams`
    would, without keeping every stream of the pass alive.
    """
    if root is not None and source is None:
        return CellStreams(root, plan.sim_id, plan.expose.expose_id)
    return resolve_stream_source(root=root, source=source)


def generate_rows(
    plan: GeneratorPlan,
    *,
    root: Optional[SeedTreeRoot] = None,
    source: Optional[StreamSource] = None,
) -> Tuple[Tuple[Any, ...], ...]:
    """Generate ``plan.rows`` rows in ``rowIndex`` order (§1).

    Each row is a tuple aligned to the expose's declared column order, in
    the value classes :mod:`flux_engine.seam.conform` renders. Pass either
    the run's seed-tree *root* or an explicit stream *source*. Over a
    *root*, the generator's own
    :class:`~flux_engine.generator.paths.CellStreams` serves every cell; a
    *source* is used as given (see
    :func:`flux_engine.generator.paths.resolve_stream_source`).
    """
    return generate_row_slice(plan, 0, plan.rows, root=root, source=source)


def generate_row_slice(
    plan: GeneratorPlan,
    start: int,
    stop: int,
    *,
    root: Optional[SeedTreeRoot] = None,
    source: Optional[StreamSource] = None,
) -> Tuple[Tuple[Any, ...], ...]:
    """Rows *start* to *stop* - 1 of :func:`generate_rows`, exactly.

    Every cell's stream is addressed by its row index (§1), never by the
    order the generator reaches it, so a slice holds the same rows wherever
    and whenever it is generated. That is what lets a run cut its rows into
    slices, generate them in several processes and join them in order.
    """
    if (
        isinstance(start, bool)
        or isinstance(stop, bool)
        or not isinstance(start, int)
        or not isinstance(stop, int)
        or not 0 <= start <= stop <= plan.rows
    ):
        raise FluxError(
            FLX_3402,
            f"a row slice lies within [0, {plan.rows}]; got start={start!r}, stop={stop!r}",
        )
    streams = _stream_source(plan, root, source)
    expose_id = plan.expose.expose_id
    givens = _given_columns(plan)
    if givens:
        order = _draw_order(plan)
        return tuple(
            _conditional_row(plan, order, givens, streams, row_index)
            for row_index in range(start, stop)
        )
    return tuple(
        tuple(
            _cell(column_plan, streams, plan.sim_id, expose_id, row_index, plan.seam_digest)
            for column_plan in plan.columns
        )
        for row_index in range(start, stop)
    )


def generate_column(
    plan: GeneratorPlan,
    column: str,
    *,
    root: Optional[SeedTreeRoot] = None,
    source: Optional[StreamSource] = None,
) -> Tuple[Any, ...]:
    """Generate one column's values, in row order.

    Exists so a test can assert the property §1's per-cell addressing
    promises: the values produced column-first are the values produced
    row-first, because no cell's stream depends on any other cell. A column
    drawn ``given`` another reads that column's level in the same row, so
    every column above it in the ``given`` chain is drawn here too, root
    first, as :func:`generate_row_slice` does.
    """
    streams = _stream_source(plan, root, source)
    column_plan = plan.column_plan(column)
    sim_id, expose_id, h = plan.sim_id, plan.expose.expose_id, plan.seam_digest
    chain: List[ColumnPlan] = []
    given = column_plan.statistics.given
    while given is not None:
        ancestor = plan.column_plan(given)
        chain.append(ancestor)
        given = ancestor.statistics.given
    chain.reverse()

    def level_at(index: int) -> Optional[str]:
        level: Optional[str] = None
        for ancestor in chain:
            _, level = _given_cell(ancestor, streams, sim_id, expose_id, index, level)
        return level

    return tuple(
        _cell(column_plan, streams, sim_id, expose_id, index, h, level_at(index))
        for index in range(plan.rows)
    )
