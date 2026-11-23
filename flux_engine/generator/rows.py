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

from bisect import bisect_right
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction
from functools import lru_cache
from itertools import product
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

import numpy as np

from flux_engine.core import kernel2
from flux_engine.core.canonical import nfc, sha256_hex
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
    uniform53,
)
from flux_engine.core.rng import exponential as draw_exponential
from flux_engine.core.rng import poisson as draw_poisson
from flux_engine.core.seedtree import (
    FLX_2202,
    SeedTreeRoot,
    digest_bytes,
    key_label,
    path_label,
)
from flux_engine.generator.allocation import allocated_given, allocated_levels
from flux_engine.generator.copula import (
    CopulaDraw,
    check_copula,
    declares_copula,
    plan_copula_draw,
    read_copula,
)
from flux_engine.generator.families import (
    FLX_3401,
    ColumnType,
    Family,
    choose_family,
    classify_column,
)
from flux_engine.generator.paths import (
    GENERATOR_PATH_SHAPE,
    CellStreams,
    StreamSource,
    generator_column_path,
    resolve_stream_source,
)
from flux_engine.generator.statistics import (
    DATE_EPOCH_ORDINAL,
    FLX_3402,
    ColumnStatistics,
    integer_width_bounds,
    joint_level,
    statistics_from_fidelity,
)
from flux_engine.seam.contract import CompiledColumn, CompiledExpose, LogicalKind

if TYPE_CHECKING:
    from flux_engine.generator.conditions import ConditionPlan
    from flux_engine.generator.dates import DatesTally
    from flux_engine.generator.plugin import PluginExpose
    from flux_engine.generator.rules import RulePlan

__all__ = [
    "GENERATOR_LABEL",
    "GENERATOR_VERSION",
    "ColumnPlan",
    "GeneratorPlan",
    "IDENTIFIER_DEFAULT_LENGTH",
    "build_plan",
    "candidate_calls",
    "clear_memos",
    "declared_range_findings",
    "GENERATOR_VERSION_2",
    "generate_column",
    "generate_row_slice",
    "generate_rows",
    "written_rows",
    "generator_version_for",
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

#: Kernel ``0.2.0``'s generator: the same plan, its identifiers from Philox
#: (KERNEL-SPEC-2 section 6.1) and its streams counter-based.
GENERATOR_VERSION_2 = kernel2.IDENTIFIER_LABEL.decode("ascii")


def generator_version_for(seed_tree_version: str) -> str:
    """The generator a run's seed tree goes with: ``flux-gen/2`` for ``flux-seed/2``."""
    if seed_tree_version == kernel2.SEED_TREE_VERSION:
        return GENERATOR_VERSION_2
    return GENERATOR_VERSION


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
    _WHOLE.clear()
    from flux_engine.generator.projections import clear_sources

    clear_sources()


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
    counter_based: bool = False,
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

    With *counter_based*, kernel ``0.2.0``'s rule (KERNEL-SPEC-2 section 6.1): the
    digits are the four words of a Philox block keyed by ``H`` and the column's key
    label, at the counter ``(row, 0, 0, 0)``. The seed is not in it either.
    """
    _require_identifier_shape(length, as_uuid)
    path = generator_column_path(sim_id, expose_id, row_index, column)
    seam = _seam_bytes(seam_digest)
    if counter_based:
        label, (row, _second) = key_label(path, GENERATOR_PATH_SHAPE)
        return _identifier_of_digits(
            kernel2.identifier_digits(seam, label, row), length=length, as_uuid=as_uuid
        )
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
    return _identifier_of_digits(sha256_hex(material), length=length, as_uuid=as_uuid)


def _identifier_of_digits(digits: str, *, length: int, as_uuid: bool) -> str:
    """The first *length* of 64 hex *digits*, or the RFC 9562 version 8 UUID they spell."""
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
    """How one column will be generated: its type, its family, its parameters.

    ``defaulted`` is ``True`` for a column that declares nothing its value
    could be drawn from and takes its type's own default: today that is a
    DATE or TIMESTAMP column drawn uniformly within the simulation's time
    bounds (§1). It is a fact about where the plan came from, not a
    parameter of the draw, so :meth:`as_document` leaves it out and
    ``generatorDigest`` is the one it was before the field existed; the
    coverage report reads it
    (:func:`flux_engine.generator.coverage.coverage_of_plan`).
    """

    column: CompiledColumn
    column_type: ColumnType
    family: Optional[Family]
    statistics: ColumnStatistics
    defaulted: bool = False
    #: The expose's rows ``N`` (after ``--scale``) when the column declares ``allocation: exact``;
    #: its classes take their counts over these rows (KERNEL-SPEC-2 section 8.3). ``None`` for
    #: every other column. ``N`` is the plan's ``rows``, already in the document, so this adds
    #: nothing to it.
    allocated_rows: Optional[int] = None
    #: The statistics of the exact column this exact column is drawn given (KERNEL-SPEC-2 section
    #: 8.3), ``None`` for any other. Like ``allocated_rows`` it is read from the plan's own
    #: statistics, so it is not part of the plan's document.
    allocated_given: Optional[ColumnStatistics] = None
    #: The emit's copula when this column is one of its columns (ADR 0035 decision 6): its value
    #: is drawn through the copula, and its own cell path draws only its NULL draw. Like
    #: ``allocated_rows`` it is not part of the plan's document, which holds the copula once.
    copula: Optional[CopulaDraw] = None

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

    ``rules`` (:mod:`flux_engine.generator.rules`) is ``None`` for an expose with
    no rule, whose document, and so digest, has no ``rules`` entry.

    ``reads`` holds the plan of every expose this plan's paths reach. The document then
    has a ``reads`` entry, each reached plan's ``generatorDigest``, so a moved parent moves
    its readers; a plan with no read has no entry and keeps the digest it always had.

    ``sums`` holds the plan of every child expose a contract ``derive`` with ``aggregate`` summarises
    (ADR 0016 section 5), with the relationship it counts them by. The document then has a ``sums``
    entry, each summed plan's ``generatorDigest``, so a moved child moves its parent; a plan with
    no summary has no entry. A child reads the parent's plan as it is before its sums are attached,
    so no plan holds itself.

    ``plugin`` names the columns a generator plugin generates (ADR 0032 section 5): such a plan draws
    nothing for them, and the row pass puts the plugin's values in before any rule is applied. It is
    ``None`` for every expose no plugin generates, and the document then has no ``plugin`` entry.

    ``relations`` says how the columns the contract makes keys or foreign
    keys are produced (:mod:`flux_engine.generator.relations`), including
    what each foreign key reads of its parent. It is ``None`` for an expose
    with no key and no relationship, and the document then has no
    ``relations`` entry, so such a plan's digest is the one it always was.
    """

    sim_id: str
    expose: CompiledExpose
    seam_digest: str
    rows: int
    columns: Tuple[ColumnPlan, ...]
    time_bounds: Optional[Tuple[int, int]] = None
    relations: Optional[Any] = None
    rules: Optional["RulePlan"] = None
    #: :data:`GENERATOR_VERSION`, or :data:`GENERATOR_VERSION_2` for a kernel ``0.2.0``
    #: run, whose root must then be a ``flux-seed/2`` one (:func:`_stream_source`).
    generator_version: str = GENERATOR_VERSION
    #: The plans of the exposes this plan's paths reach, by ``exposeId``, sorted (ADR 0022
    #: section 3). Empty for every plan that reads no other expose's row.
    reads: Tuple[Tuple[str, "GeneratorPlan"], ...] = ()
    #: The plan of each child expose this plan's summaries read, with the relationship, sorted by
    #: ``(exposeId, relationship)`` (ADR 0016 section 5). Empty for every plan with no summary.
    sums: Tuple[Tuple[str, str, "GeneratorPlan"], ...] = ()
    #: The columns a generator plugin generates, or ``None`` when no plugin generates this expose.
    plugin: Optional["PluginExpose"] = None
    #: The groups of rows that hold fixed values (ADR 0028 section 10), or ``None`` for an expose
    #: whose Simulation declares none; the document then has no ``conditions`` entry.
    conditions: Optional["ConditionPlan"] = None
    #: A projected emit's source (ADR 0027): the history it reads and the kind of projection. Its
    #: ``rows`` is then the count worked out from that history. ``None`` for every other emit,
    #: whose document has no ``projection`` entry.
    projection: Optional[Any] = None
    #: The emit's copula, planned (ADR 0035 decision 5), or ``None`` for an emit that declares none;
    #: the document then has no ``copula`` entry.
    copula: Optional[CopulaDraw] = None

    @property
    def counter_based(self) -> bool:
        """Whether this plan's rows are kernel ``0.2.0``'s."""
        return self.generator_version == GENERATOR_VERSION_2

    def as_document(self) -> Dict[str, Any]:
        """The plan document ``generatorDigest`` is taken over."""
        document: Dict[str, Any] = {
            "generatorVersion": self.generator_version,
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
        if self.relations is not None:
            document["relations"] = self.relations.as_document()
        if self.rules is not None:
            document["rules"] = self.rules.as_document()
        if self.plugin is not None:
            document["plugin"] = self.plugin.as_document()
        if self.conditions is not None:
            document["conditions"] = self.conditions.as_document()
        if self.reads:
            document["reads"] = {
                expose_id: plan.digest().value
                for expose_id, plan in sorted(self.reads, key=lambda read: read[0])
            }
        if self.projection is not None:
            document["projection"] = self.projection.as_document()
        if self.copula is not None:
            document["copula"] = self.copula.as_document()
        if self.sums:
            document["sums"] = [
                {
                    "expose": expose_id,
                    "relationship": relationship,
                    "generatorDigest": plan.digest().value,
                }
                for expose_id, relationship, plan in self.sums
            ]
        return document

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


def declared_range_findings(
    expose: CompiledExpose,
    fidelity: Optional[Mapping[str, Any]],
    time_bounds: Optional[Mapping[str, Any]],
) -> Tuple[str, ...]:
    """Check an emit's declared ranges and bins; return what only deserves a warning.

    Reading the statistics refuses a bin outside its column's range and a
    range outside its integer width (``FLX-3402``). What comes back is the
    softer finding: a DATE or TIMESTAMP column whose own range or bin edge
    lies outside the run's ``timeBounds``. The bounds only fill a range the
    column leaves undeclared (:func:`_with_time_bounds`), so a declared range
    wider than the run window is legal, and still worth a line.
    """
    statistics = statistics_from_fidelity(expose, fidelity)
    bounds = _time_bounds_ms(time_bounds)
    if bounds is None:
        return ()
    findings: List[str] = []
    for column in expose.columns:
        kind = column.logical_type.kind
        if kind not in (LogicalKind.DATE, LogicalKind.TIMESTAMP):
            continue
        declared = statistics[column.name]
        if kind is LogicalKind.DATE:
            low, high = Decimal(bounds[0] // 86_400_000), Decimal(bounds[1] // 86_400_000)
        else:
            low, high = Decimal(bounds[0]), Decimal(bounds[1])
        edges = [value for value in (declared.range_min, declared.range_max) if value is not None]
        edges += list(declared.bins or ())
        if any(edge < low or edge > high for edge in edges):
            findings.append(
                f"{expose.expose_id}.{column.name}: the declared range or bins leave the run's "
                "timeBounds, so rows can carry dates outside the window the run covers"
            )
    return tuple(findings)


def build_plan(
    expose: CompiledExpose,
    *,
    sim_id: str,
    seam_digest: str,
    rows: int,
    fidelity: Optional[Mapping[str, Any]] = None,
    time_bounds: Optional[Mapping[str, Any]] = None,
    related: FrozenSet[str] = frozenset(),
    rules: Optional["RulePlan"] = None,
    generator_version: str = GENERATOR_VERSION,
    plugin: Optional["PluginExpose"] = None,
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

    ``related`` names the columns the contract makes keys or foreign keys
    (:func:`flux_engine.generator.relations.related_columns`). Their values
    come from :func:`flux_engine.generator.relations.relate_plans`, so one
    that declares no draw of its own gets no family and is not refused for
    it; one that declares a draw is checked like any other column, because
    a foreign key draws with it before taking its parent's nearest key.

    ``rules`` is the expose's contract rules. A column that a ``derive`` without
    ``when`` sets on every row may, like a related one, declare no draw.

    ``plugin`` says which columns a generator plugin generates (ADR 0032 section 5). Such a column
    gets no family and draws nothing: whatever it declares is what its values are held to, and the
    plugin's values are put into the rows before any rule is applied.
    """
    # generator_version is the run's (generator_version_for its seed tree).
    if generator_version not in (GENERATOR_VERSION, GENERATOR_VERSION_2):
        raise FluxError(FLX_2202, f"no generator {generator_version!r}")
    if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
        raise FluxError(FLX_3402, f"row count must be a non-negative integer, got {rows!r}")
    statistics = statistics_from_fidelity(expose, fidelity)
    bounds = _time_bounds_ms(time_bounds)

    exempt = related | (frozenset() if rules is None else rules.covers_every_row)
    plans: List[ColumnPlan] = []
    made_by_plugin = frozenset(plugin.names) if plugin is not None else frozenset()
    for column in expose.columns:
        declared = statistics[column.name]
        if column.name in made_by_plugin:
            plans.append(
                ColumnPlan(
                    column=column,
                    column_type=_plugin_column_type(column),
                    family=None,
                    statistics=declared,
                )
            )
            continue
        column_statistics = _with_time_bounds(column, declared, bounds)
        column_type = classify_column(column, column_statistics)
        family = _planned_family(column, column_statistics, column_type, column.name in exempt)
        plans.append(
            ColumnPlan(
                column=column,
                column_type=column_type,
                family=family,
                statistics=column_statistics,
                # `_with_time_bounds` hands back the very object it was given
                # unless it filled a range, so identity says "it filled one";
                # that is a type default only when the column declared no draw.
                defaulted=column_statistics is not declared and not _declares_a_draw(declared),
                allocated_rows=None if column_statistics.allocation is None else rows,
            )
        )
    if rules is not None:
        rules.refuse_given_on_determined(plans)
    _require_binned_parents(plans, generator_version, related, rules)
    _require_conditionals(plans)
    _require_allocatable(plans, generator_version, related, rules)
    plans = _with_allocation_parents(plans)
    copula = _require_copula(
        fidelity, plans, generator_version, related, rules, sim_id, expose.expose_id
    )
    if copula is not None:
        plans = [
            replace(item, copula=copula) if nfc(item.column.name) in copula.columns else item
            for item in plans
        ]
    if generator_version != GENERATOR_VERSION_2:
        for column_plan in plans:
            if column_plan.statistics.given_link is not None:
                raise FluxError(
                    FLX_3402,
                    f"{column_plan.column.name}: a column drawn given a column of a linked row "
                    "is generated on kernel 0.2.0 only, and this run is on kernel 0.1.0",
                )
    return GeneratorPlan(
        sim_id=sim_id,
        expose=expose,
        seam_digest=seam_digest,
        rows=rows,
        columns=tuple(plans),
        time_bounds=bounds,
        rules=rules,
        generator_version=generator_version,
        plugin=plugin,
        copula=copula,
    )


def _require_copula(
    fidelity: Optional[Mapping[str, Any]],
    plans: Sequence[ColumnPlan],
    generator_version: str,
    related: FrozenSet[str],
    rules: Optional["RulePlan"],
    sim_id: str,
    expose_id: str,
) -> Optional[CopulaDraw]:
    """Refuse a copula the emit cannot draw (ADR 0035 decision 4), or plan it (decision 5).

    Kernel 0.1.0 has no copula path, so the key is ``FLX-3105`` there, naming the kernel. Anything
    else the declaration or the emit's columns get wrong is ``FLX-3402``
    (:func:`flux_engine.generator.copula.check_copula`). ``None`` when the emit declares none.
    """
    if not declares_copula(fidelity):
        return None
    if generator_version != GENERATOR_VERSION_2:
        from flux_engine.seam.resolve import FLX_3105

        raise FluxError(
            FLX_3105,
            "fidelity.copula needs kernel 0.2.0, and this run is on kernel 0.1.0",
        )
    declaration = read_copula(fidelity)
    assert declaration is not None  # declares_copula
    check_copula(declaration, plans, set_elsewhere=_set_elsewhere(plans, related, rules))
    return plan_copula_draw(declaration, plans, sim_id=sim_id, expose_id=expose_id)


def _set_elsewhere(
    plans: Sequence[ColumnPlan], related: FrozenSet[str], rules: Optional["RulePlan"]
) -> Dict[str, str]:
    """Each column something other than its own draw sets, by name, with what sets it.

    A key or a foreign key, a column a rule, a ``lifetimes``, ``within`` or ``rates`` entry or a
    copy sets, and a hidden column.
    """
    found: Dict[str, str] = {}
    for plan in plans:
        if plan.column.hidden:
            found[nfc(plan.column.name)] = "a hidden column"
    if rules is not None:
        for name in rules.determined | {c for rule in rules.rules for c in rule.to_columns}:
            found[nfc(name)] = "set by a rule, a lifetimes, within or copy declaration"
        for rate in rules.rates:
            found[nfc(rate.column)] = "a date with seasonal rates"
    for name in related:
        found[nfc(name)] = "a key or a column of a relationship"
    return found


def _with_allocation_parents(plans: List[ColumnPlan]) -> List[ColumnPlan]:
    """Each exact column drawn given another carries that column's statistics (already checked)."""
    by_name = {plan.column.name: plan for plan in plans}
    return [
        (
            replace(plan, allocated_given=by_name[plan.statistics.parents[0]].statistics)
            if plan.allocated_rows is not None and plan.statistics.parents
            else plan
        )
        for plan in plans
    ]


def _require_allocatable(
    plans: Sequence[ColumnPlan],
    generator_version: str,
    related: FrozenSet[str],
    rules: Optional["RulePlan"],
) -> None:
    """Refuse an ``allocation: exact`` column that kernel and family cannot allot (KERNEL-SPEC-2 section 8.3).

    Kernel 0.1.0 refuses the key (``FLX-3105``). A column allots its levels, or its bins; a
    column drawn given another is allotted when it is given one exact column that is given
    nothing itself. Any other family, a key or foreign key column, a column a rule or dependency
    sets, a column given a linked row's column or given a column that is not exact are
    ``FLX-3402``.
    """
    declared = [plan for plan in plans if plan.statistics.allocation is not None]
    if not declared:
        return
    if generator_version != GENERATOR_VERSION_2:
        from flux_engine.seam.resolve import FLX_3105

        raise FluxError(
            FLX_3105,
            f"{declared[0].column.name}: allocation needs kernel 0.2.0, and this run is on "
            "kernel 0.1.0",
        )
    by_name = {plan.column.name: plan for plan in plans}
    set_by_rules = (
        frozenset()
        if rules is None
        else rules.determined | {name for rule in rules.rules for name in rule.to_columns}
    )
    blocked = related | set_by_rules
    for plan in declared:
        _require_allocatable_column(plan, by_name, blocked)


def _require_allocatable_column(
    plan: ColumnPlan, by_name: Mapping[str, ColumnPlan], blocked: FrozenSet[str]
) -> None:
    """Refuse one ``allocation: exact`` column that its family, rules or givens cannot allot."""
    name, statistics = plan.column.name, plan.statistics
    if name in blocked:
        raise FluxError(
            FLX_3402,
            f"{name}: allocation exact is not allowed on a key, a foreign key or a column a "
            "rule sets",
        )
    is_levels = plan.family is Family.CATEGORICAL
    is_bins = plan.family is Family.UNIFORM_EMPIRICAL and statistics.bins is not None
    if not (is_levels or is_bins):
        raise FluxError(
            FLX_3402,
            f"{name}: allocation exact allots the levels of a categorical column or the bins "
            "of a histogram",
        )
    if is_bins and statistics.bin_weights is None and statistics.bin_weights_given is None:
        raise FluxError(FLX_3402, f"{name}: allocation exact needs binWeights to allot by")
    if statistics.given_link is not None:
        raise FluxError(
            FLX_3402,
            f"{name}: allocation exact is not read for a column given a column of a linked row",
        )
    if len(statistics.parents) > 1:
        raise FluxError(
            FLX_3402,
            f"{name}: allocation exact is read given one column, not {len(statistics.parents)}",
        )
    for given in statistics.parents:
        parent = by_name[given]
        if is_binned(parent):
            raise FluxError(
                FLX_3402,
                f"{name}: allocation exact is read given a categorical column, and {given!r} is "
                "drawn from bins",
            )
        if parent.statistics.allocation is None:
            raise FluxError(
                FLX_3402,
                f"{name}: allocation exact is read given a column that is itself exact, and "
                f"{given!r} is not",
            )
        if parent.statistics.parents:
            raise FluxError(
                FLX_3402,
                f"{name}: allocation exact is not read given {given!r}, a column that is "
                "itself drawn given another",
            )


def _plugin_column_type(column: CompiledColumn) -> ColumnType:
    """The column type a plugin's column is filed under: its type's own, which draws nothing here.

    SEAM-FIDELITY-SPEC section 1's six column types do not name JSON or BYTES, which a plugin may
    generate; such a column is filed as a categorical string, and its ``None`` family keeps any
    draw from reading it.
    """
    try:
        return classify_column(column, ColumnStatistics(column=column.name))
    except FluxError:
        return ColumnType.STRING_CATEGORICAL


def _planned_family(
    column: CompiledColumn,
    statistics: ColumnStatistics,
    column_type: ColumnType,
    related: bool,
) -> Optional[Family]:
    """The family a column draws with, checked; ``None`` for a related column that draws nothing.

    A key or foreign key that declares no draw takes its value from the
    relations (:mod:`flux_engine.generator.relations`), so it has no family to
    check. A required one still may not declare nulls.
    """
    if related and not _declares_a_draw(statistics):
        if column.required and statistics.null_fraction is not None:
            raise FluxError(
                FLX_3402,
                f"{column.name}: the contract marks this column required, so a declared "
                "nullFraction would produce rows the seam refuses",
            )
        return None
    family = choose_family(column, statistics, column_type)
    _require_parameters(column, statistics, column_type, family)
    return family


#: The :class:`ColumnStatistics` fields that say how a column's value is
#: drawn. A column whose declaration sets none of them declares no draw, and
#: whatever fills it is the type's default. Every other field is either
#: bookkeeping the draw does not read (the text a number was written as, the
#: identifier length), a statement about nulls and cardinality, which shapes a
#: draw without being one, or ``family``, which names a family and supplies no
#: parameter: the only family a DATE or TIMESTAMP column can name without one
#: is the uniform draw it already gets, so naming it changes nothing and the
#: column is still a type default. ``tests/generator/test_coverage.py`` pins
#: that every field of the class is in exactly one of the two tuples, so a
#: field added later has to be placed.
DRAW_FIELDS: Tuple[str, ...] = (
    "range_min",
    "range_max",
    "support",
    "support_is_declared",
    "levels",
    "mean",
    "stddev",
    "rate",
    "bins",
    "given",
    "given_columns",
    "given_link",
    "bin_weights_given",
    "level_weights_given",
)

#: The fields of :class:`ColumnStatistics` that are not in :data:`DRAW_FIELDS`.
NON_DRAW_FIELDS: Tuple[str, ...] = (
    "column",
    "range_min_text",
    "range_max_text",
    "max_cardinality",
    "null_policy",
    "null_fraction",
    "unique",
    "bin_texts",
    "bin_weights",
    "family",
    "identifier_length",
    "allocation",
)


def _declares_a_draw(statistics: ColumnStatistics) -> bool:
    """Whether *statistics* say anything a family could be drawn from.

    ``is not None`` and ``is not False``, never truthiness: a declared rate
    or mean of ``0`` is a declaration, and an absent one is silence.
    """
    return any(
        value is not None and value is not False
        for value in (getattr(statistics, name) for name in DRAW_FIELDS)
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
    is a categorical with no declared nulls, or a column drawn from ``bins`` with none
    (ADR 0036 decision 3: its classes are its bins, keyed by the text of each lower edge), so
    each row has exactly one class to look up, and the weight table names exactly the classes
    that column can draw.

    A given column may itself be drawn given another, so the columns form a
    forest: each column has at most one parent, and a cycle is refused. That is
    the shape of a Chow-Liu tree (Chow and Liu, 1968), the structure MST
    (McKenna, Miklau and Sheldon, 2021) selects, without its privacy noise.
    """
    by_name = {plan.column.name: plan for plan in plans}
    for plan in plans:
        parents = plan.statistics.parents
        if not parents and plan.statistics.given_link is None:
            continue
        name = plan.column.name
        statistics = plan.statistics
        for given in parents:
            if given not in by_name:
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
        if statistics.given_link is not None:
            # The given column is a column of another plan: relate_plans checks the table
            # against the levels it draws.
            continue
        drawn_by_parent: List[Tuple[str, ...]] = []
        for given in parents:
            parent = by_name[given]
            if parent.family is not Family.CATEGORICAL and not is_binned(parent):
                raise FluxError(
                    FLX_3402,
                    f"{name}: given column {given!r} is not categorical, so it has no level "
                    "to condition on (nor is it drawn from bins, which would give it a bin)",
                )
            if parent.statistics.null_fraction is not None:
                raise FluxError(
                    FLX_3402,
                    f"{name}: given column {given!r} declares a nullFraction, and a null "
                    "has no level to condition on",
                )
            drawn_by_parent.append(tuple(sorted(class_names(parent))))
        if len(parents) == 1:
            drawn = list(drawn_by_parent[0])
        else:
            # One table row per combination of the given columns' levels, in the declared order.
            drawn = sorted(joint_level(levels) for levels in product(*drawn_by_parent))
        if declared != drawn:
            what = f"{list(parents)} draw" if len(parents) > 1 else f"{parents[0]!r} draws"
            raise FluxError(
                FLX_3402,
                f"{name}: {table} declares levels {declared}; {what} {drawn}",
            )
    _require_acyclic(by_name)


def _require_acyclic(by_name: Mapping[str, ColumnPlan]) -> None:
    """Refuse ``given`` links that loop back, naming the loop.

    A column is drawn given one column or several, so the links form a directed graph, and
    a depth-first walk from every column finds a way back to a column on its own path.
    """
    done: Set[str] = set()
    for start in sorted(by_name):
        stack: List[Tuple[str, Iterator[str]]] = [(start, iter(by_name[start].statistics.parents))]
        path: List[str] = [start]
        while stack:
            current, remaining = stack[-1]
            for parent in remaining:
                if parent in path:
                    loop = path[path.index(parent) :] + [parent]
                    raise FluxError(FLX_3402, f"given forms a cycle: {' -> '.join(loop)}")
                if parent not in done:
                    path.append(parent)
                    stack.append((parent, iter(by_name[parent].statistics.parents)))
                    break
            else:
                done.add(current)
                path.pop()
                stack.pop()


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


def is_binned(plan: ColumnPlan) -> bool:
    """Whether *plan*'s column is drawn from ``bins``: a histogram, whose classes are its bins."""
    return plan.family is Family.UNIFORM_EMPIRICAL and plan.statistics.bins is not None


def class_names(plan: ColumnPlan) -> Tuple[str, ...]:
    """The classes a column drawn given can be keyed by: its levels, or the lower edge of each bin.

    A bin is named by the text of its lower edge exactly as the column's ``bins`` write it
    (ADR 0036 decision 3).
    """
    if not is_binned(plan):
        return _level_names(plan.statistics)
    statistics = plan.statistics
    assert statistics.bins is not None
    texts = statistics.bin_texts or tuple(str(edge) for edge in statistics.bins)
    return tuple(nfc(text) for text in texts[:-1])


def bin_class(plan: ColumnPlan, value: Any) -> str:
    """The class of a drawn *value* of a column drawn from bins: its bin's name.

    The value is floored onto the grid the histogram draws on and placed in the bin that holds
    it, the last bin closed above, which is the bin the draw chose.
    """
    statistics = plan.statistics
    edges = statistics.bins
    assert edges is not None
    if isinstance(value, date):
        grid = Decimal(value.toordinal() - DATE_EPOCH_ORDINAL)
    elif isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        grid = Decimal(value)
    else:
        raise FluxError(FLX_3402, f"{plan.column.name}: a value of this column has no bin")
    index = min(max(bisect_right(edges, grid) - 1, 0), len(edges) - 2)
    return class_names(plan)[index]


def _require_binned_parents(
    plans: Sequence[ColumnPlan],
    generator_version: str,
    related: FrozenSet[str],
    rules: Optional["RulePlan"],
) -> None:
    """Refuse a ``given`` that names a column drawn from bins where this plan cannot draw it.

    Kernel 0.1.0 has no such parent, so it is ``FLX-3105``, naming the kernel. A key, a foreign
    key, or a column a rule, a ``lifetimes``, ``within``, ``rates`` or copy entry sets has no
    draw of its own to read a bin from, and is ``FLX-3402``. What else a parent must be is
    checked with every other ``given`` (:func:`_require_conditionals`).
    """
    by_name = {plan.column.name: plan for plan in plans}
    binned = {
        (parent, plan.column.name)
        for plan in plans
        for parent in plan.statistics.parents
        if parent in by_name and is_binned(by_name[parent])
    }
    if not binned:
        return
    parent, child = min(binned)
    if generator_version != GENERATOR_VERSION_2:
        from flux_engine.seam.resolve import FLX_3105

        raise FluxError(
            FLX_3105,
            f"{child}: given {parent!r}, a column drawn from bins, is generated on kernel 0.2.0 "
            "only, and this run is on kernel 0.1.0",
        )
    elsewhere = _set_elsewhere(plans, related, rules)
    for parent, child in sorted(binned):
        if by_name[parent].column.hidden:
            continue
        reason = elsewhere.get(nfc(parent))
        if reason is not None:
            raise FluxError(
                FLX_3402,
                f"{child}: given {parent!r}, which is {reason}, so it has no bin of its own draw",
            )


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
    forced_bin: Optional[int] = None,
) -> Decimal:
    """Pick a declared bin by its weight, then draw uniformly inside it.

    With *forced_bin* the pick is still drawn, and left unread: the bin is the one given, which
    is how exact allocation chooses it (KERNEL-SPEC-2 section 8.3).

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
    index = positions[chosen] if forced_bin is None else forced_bin
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
            f"declared weights sum to {total}, which is further than {WEIGHT_SUM_TOLERANCE} from 1",
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


def _draw_value(
    plan: ColumnPlan,
    stream: DrawStream,
    given_level: Optional[str] = None,
    forced: Union[str, int, None] = None,
) -> Any:
    """Draw one value for a non-identifier column.

    With *forced*, a level's name or a bin's index, the class draw is made and left unread and
    the value is the one of that class (exact allocation, KERNEL-SPEC-2 section 8.3).
    """
    column, statistics, family = plan.column, plan.statistics, plan.family
    kind = column.logical_type.kind

    if family is Family.CATEGORICAL:
        level = categorical(stream, _categorical_levels(statistics, column, given_level))
        return _level_to_value(level if forced is None else str(forced), column)

    if family is Family.BOUNDED_NORMAL:
        return _draw_bounded_normal(plan, stream)

    if family is Family.RATE:
        return _draw_rate(plan, stream)

    if statistics.bins is not None:
        drawn = _draw_histogram(
            stream, statistics, column, given_level, None if forced is None else int(forced)
        )
        return int(drawn) if kind is LogicalKind.INT else _from_grid(drawn, column)

    return _draw_uniform(plan, stream)


def _draw_bounded_normal(plan: ColumnPlan, stream: DrawStream) -> Any:
    """The bounded normal family: a truncated normal on the column's scale."""
    column, statistics = plan.column, plan.statistics
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
    return int(drawn) if column.logical_type.kind is LogicalKind.INT else drawn


def _draw_uniform(plan: ColumnPlan, stream: DrawStream) -> Any:
    """The uniform family: one value on the column's grid between its bounds."""
    column, statistics = plan.column, plan.statistics
    low = statistics.range_min
    high = statistics.range_max
    assert low is not None and high is not None  # _require_parameters
    step = _grid_step(column) if column.logical_type.kind is LogicalKind.DECIMAL else Decimal(1)
    drawn = _uniform_on_grid(stream, low, high, step)
    return _from_grid(drawn, column)


def candidate_calls(plan: ColumnPlan) -> int:
    """``m``: the sampler calls one violation candidate of a date column takes (EXPR-SPEC-2 section 7).

    A candidate is the column's own draw with no null draw. For a DATE or TIMESTAMP column that is
    one call for a uniform over the declared range (one ``bounded``) and for a rate (one ``uniform53``
    for the exponential), and two for a histogram, which is a ``categorical`` pick of the bin and a
    ``bounded`` draw inside it. The count never depends on a value drawn, so it is fixed per column.
    """
    return 2 if plan.family is Family.UNIFORM_EMPIRICAL and plan.statistics.bins is not None else 1


def _draw_rate(plan: ColumnPlan, stream: DrawStream) -> Any:
    """§1's rate family: Poisson for INT counts, exponential otherwise."""
    column, statistics = plan.column, plan.statistics
    kind = column.logical_type.kind
    if plan.column_type is ColumnType.INT:
        assert statistics.mean is not None  # _require_parameters
        drawn_count = draw_poisson(stream, mean=statistics.mean)
        count = int(_clamp(Decimal(drawn_count), statistics))
        width = integer_width_bounds(column)
        # A count never leaves the width the contract declares for the column.
        return count if width is None else max(width[0], min(width[1], count))
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
    if plan.column_type is ColumnType.IDENTIFIER:
        return _identifier_cell(plan, source, sim_id, expose_id, row_index, h)
    if plan.family is None:
        # A column that a derive without `when` sets on every row, and that declares
        # no draw: the rule sets it after this pass.
        return None
    column = plan.column
    if isinstance(source, CellStreams):
        stream = source.stream_at(row_index, column.name)
    else:
        stream = source.stream(generator_column_path(sim_id, expose_id, row_index, column.name))
    if plan.copula is not None:
        return _copula_cell(plan, stream, source, row_index)
    if plan.allocated_rows is None:
        return _draw_cell(plan, stream, given_level)
    return _allocated_cell(plan, stream, source, sim_id, expose_id, row_index, given_level)


def _copula_cell(plan: ColumnPlan, stream: DrawStream, source: _Source, row_index: int) -> Any:
    """One cell of a copula column: its value from the row's copula draws, or null.

    The cell's own stream draws only the NULL draw, at index 0, when the column declares a
    ``nullFraction``; the copula draws its value whether or not the cell is NULL.
    """
    assert plan.copula is not None
    root = getattr(source, "root", None)
    if not isinstance(root, SeedTreeRoot):
        raise FluxError(FLX_2202, f"{plan.column.name}: a copula is drawn over the run's root")
    is_null = plan.statistics.null_fraction is not None and bernoulli(
        stream, plan.statistics.null_fraction
    )
    value = plan.copula.row_value(root, row_index, nfc(plan.column.name))
    return None if is_null else value


def _allocated_cell(
    plan: ColumnPlan,
    stream: DrawStream,
    source: _Source,
    sim_id: str,
    expose_id: str,
    row_index: int,
    given_level: Optional[str],
) -> Any:
    """One cell of an ``allocation: exact`` column: the value of its slot's class, or null."""
    # The cell's draws are made at their indexes and left unread: its class is its slot's.
    (label,) = _allocated_labels(
        plan, source, sim_id, expose_id, np.array([row_index], dtype=np.int64)
    )
    if plan.statistics.null_fraction is not None:
        bernoulli(stream, plan.statistics.null_fraction)
    value = _draw_value(plan, stream, given_level, label)
    return None if label is None else value


def _identifier_cell(
    plan: ColumnPlan,
    source: _Source,
    sim_id: str,
    expose_id: str,
    row_index: int,
    h: str,
) -> Any:
    """One identifier cell of *plan*, from the source's label where it has one."""
    column = plan.column
    length = plan.statistics.identifier_length or IDENTIFIER_DEFAULT_LENGTH
    as_uuid = column.declared_type.strip().lower() in _UUID_TYPES
    if isinstance(source, CellStreams):
        _require_identifier_shape(length, as_uuid)
        try:
            label = source.label(row_index, column.name)
        except (FluxError, UnicodeError):
            label = None
        if label is not None:
            if source.root.counter_based:
                digits = kernel2.identifier_digits(
                    _seam_bytes(h), source.key_label(column.name), row_index
                )
                return _identifier_of_digits(digits, length=length, as_uuid=as_uuid)
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
        counter_based=bool(getattr(getattr(source, "root", None), "counter_based", False)),
    )


def _allocated_labels(
    plan: ColumnPlan, source: _Source, sim_id: str, expose_id: str, at: Any
) -> List[Any]:
    """The class of each row in *at* under exact allocation: a level, a bin's index or ``None``."""
    assert plan.allocated_rows is not None
    root = getattr(source, "root", None)
    if root is None:
        raise FluxError(FLX_3402, f"{plan.column.name}: allocation needs the run's seed tree root")
    if plan.allocated_given is not None:
        return allocated_given(
            plan.statistics, plan.allocated_given, root, sim_id, expose_id, plan.allocated_rows, at
        )
    return allocated_levels(plan.statistics, root, sim_id, expose_id, plan.allocated_rows, at)


def _draw_cell(plan: ColumnPlan, stream: DrawStream, given_level: Optional[str]) -> Any:
    """A declared null, or a draw, from *stream*: what a cell makes once it has its stream."""
    if plan.statistics.null_fraction is not None:
        # The null draw comes first and always happens, so the value draw
        # lands on the same stream position whether or not the cell is null.
        is_null = bernoulli(stream, plan.statistics.null_fraction)
        value = _draw_value(plan, stream, given_level)
        return None if is_null else value
    return _draw_value(plan, stream, given_level)


def _cell_on_stream(plan: ColumnPlan, stream: DrawStream, *, skip_null: bool = False) -> Any:
    """The value :func:`_cell` draws for *plan*'s column, from a stream the caller chose.

    A contract dependency draws a ``to`` column once per key, on its own stream
    (EXPR-SPEC-2 section 8), through the same family and kernel code as a cell. Not for an
    identifier column, and not for a column drawn ``given`` another. With *skip_null* the
    column's null draw is left out and the value is its non-NULL one.
    """
    with localcontext(_KERNEL_CONTEXT):
        if skip_null:
            return _draw_value(plan, stream, None)
        return _draw_cell(plan, stream, None)


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
    if is_binned(plan):
        # A column drawn from bins is given by the bin its own cell draws, on its own stream at
        # the indices a histogram cell draws at: the cell is what _cell makes, its class its bin.
        # (The seam digest only keys an identifier cell, which a column drawn from bins is not.)
        value = _cell(plan, source, sim_id, expose_id, row_index, "", given_level)
        return value, bin_class(plan, value)
    with localcontext(_KERNEL_CONTEXT):
        if isinstance(source, CellStreams):
            stream = source.stream_at(row_index, column.name)
        else:
            stream = source.stream(generator_column_path(sim_id, expose_id, row_index, column.name))
        level = categorical(stream, _categorical_levels(plan.statistics, column, given_level))
        if plan.allocated_rows is not None:
            # The draw above is left unread: the level is the one of the row's slot.
            (allocated,) = _allocated_labels(
                plan, source, sim_id, expose_id, np.array([row_index], dtype=np.int64)
            )
            level = str(allocated)
        return _level_to_value(level, column), level


def _given_columns(plan: GeneratorPlan) -> FrozenSet[str]:
    """The columns some other column of *plan* is drawn given."""
    names: Set[str] = set()
    for column_plan in plan.columns:
        names.update(column_plan.statistics.parents)
    return frozenset(names)


def _level_given(statistics: ColumnStatistics, levels: Mapping[str, str]) -> Optional[str]:
    """The level a column is drawn given in a row: its parent's, or the combination of its parents'.

    ``None`` for a column drawn given nothing. *levels* holds the level each column drawn so
    far drew in the row. With several parents the key is :func:`joint_level` of one level of
    each, in the order ``given`` lists them, which is how the weight tables are keyed.
    """
    parents = statistics.parents
    if not parents:
        return None
    if len(parents) == 1:
        return levels[parents[0]]
    return joint_level([levels[parent] for parent in parents])


def _ancestors(plan: GeneratorPlan, column_plan: ColumnPlan) -> List[ColumnPlan]:
    """Every column *column_plan* is drawn given, directly or not, parents before their children."""
    order = _draw_order(plan)
    wanted: Set[str] = set()
    pending = list(column_plan.statistics.parents)
    while pending:
        name = pending.pop()
        if name not in wanted:
            wanted.add(name)
            pending.extend(plan.column_plan(name).statistics.parents)
    return [plan.columns[index] for index in order if plan.columns[index].column.name in wanted]


def _row_order(plan: GeneratorPlan, givens: FrozenSet[str]) -> Tuple[int, ...]:
    """The order a row's columns are drawn in: given-first when a column is given or conditioned."""
    if givens or plan.conditions is not None:
        return _draw_order(plan)
    return tuple(range(len(plan.columns)))


def _draw_order(plan: GeneratorPlan) -> Tuple[int, ...]:
    """Column indices in an order that draws every given column before those given it.

    By depth in the ``given`` graph (:func:`_require_conditionals` refused
    every cycle; a column's depth is one more than its deepest parent's), then
    by declared position, so the order is a function of the plan alone.
    """
    by_name = {column_plan.column.name: column_plan for column_plan in plan.columns}
    known: Dict[str, int] = {}

    def depth(column_plan: ColumnPlan) -> int:
        name = column_plan.column.name
        if name not in known:
            parents = column_plan.statistics.parents
            known[name] = 1 + max(depth(by_name[parent]) for parent in parents) if parents else 0
        return known[name]

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
        given_level = _level_given(column_plan.statistics, levels)
        name = column_plan.column.name
        hit = _conditioned_cell(plan, column_plan, source, row_index, levels)
        if hit is not None:
            values[index], levels[name] = hit
        elif name in givens:
            values[index], levels[name] = _given_cell(
                column_plan, source, sim_id, expose_id, row_index, given_level
            )
        else:
            values[index] = _cell(column_plan, source, sim_id, expose_id, row_index, h, given_level)
    return tuple(values)


def _conditioned_cell(
    plan: GeneratorPlan,
    column_plan: ColumnPlan,
    source: _Source,
    row_index: int,
    levels: Mapping[str, str],
) -> Optional[Tuple[Any, str]]:
    """A cell its row's group decides, as ``(value, level)``, or ``None`` when it draws as usual.

    A fixed column is set and draws nothing. A column whose weights the group changes draws on its
    own stream at its first word, as :func:`_given_cell` does, and the group only chooses the
    table that word is searched in (:mod:`flux_engine.generator.conditions`).
    """
    conditions = plan.conditions
    name = column_plan.column.name
    if conditions is None or name not in conditions.touched:
        return None
    decided = conditions.decide(row_index, name, levels)
    if decided is None:
        return None
    if isinstance(decided, str):
        level = decided
    else:
        from flux_engine.generator.conditions import level_at

        if isinstance(source, CellStreams):
            stream = source.stream_at(row_index, name)
        else:
            stream = source.stream(
                generator_column_path(plan.sim_id, plan.expose.expose_id, row_index, name)
            )
        level = level_at(decided, uniform53(stream))
    with localcontext(_KERNEL_CONTEXT):
        return _level_to_value(level, column_plan.column), level


def _stream_source(
    plan: GeneratorPlan, root: Optional[SeedTreeRoot], source: Optional[StreamSource]
) -> _Source:
    """The pass's streams: the caller's *source*, or the generator's own over *root*.

    The generator's own (:class:`~flux_engine.generator.paths.CellStreams`)
    gives each cell the stream :class:`~flux_engine.generator.paths.GeneratorStreams`
    would, without keeping every stream of the pass alive.
    """
    given_root = root if root is not None else getattr(source, "root", None)
    if given_root is not None and given_root.counter_based != plan.counter_based:
        raise FluxError(
            FLX_2202,
            f"a {plan.generator_version} plan is generated under a "
            f"{given_root.seed_tree_version} root; a run's plans and root share its kernel",
        )
    if root is not None and source is None:
        return CellStreams(root, plan.sim_id, plan.expose.expose_id)
    return resolve_stream_source(root=root, source=source)


def written_rows(
    plan: GeneratorPlan, rows: Sequence[Tuple[Any, ...]]
) -> Tuple[Tuple[Any, ...], ...]:
    """*rows* as a stream writes them: the plan's hidden columns, which come last, left off.

    The one place rows leave generation for a writer (ADR 0024 section 2). A plan with no hidden
    column returns its rows as they are, so a run without one is untouched.
    """
    hidden = plan.expose.hidden_count
    if not hidden:
        return rows if isinstance(rows, tuple) else tuple(rows)
    written = len(plan.expose.columns) - hidden
    return tuple(row[:written] for row in rows)


def generate_rows(
    plan: GeneratorPlan,
    *,
    root: Optional[SeedTreeRoot] = None,
    source: Optional[StreamSource] = None,
    tally: Optional["DatesTally"] = None,
) -> Tuple[Tuple[Any, ...], ...]:
    """Generate ``plan.rows`` rows in ``rowIndex`` order (§1).

    Each row is a tuple aligned to the expose's declared column order, in
    the value classes :mod:`flux_engine.seam.conform` renders. Pass either
    the run's seed-tree *root* or an explicit stream *source*. Over a
    *root*, the generator's own
    :class:`~flux_engine.generator.paths.CellStreams` serves every cell; a
    *source* is used as given (see
    :func:`flux_engine.generator.paths.resolve_stream_source`). A *tally* counts what each row of a
    fact of ``lifetimes`` or ``within`` came to (:func:`~flux_engine.generator.rules.new_tally`).
    """
    return generate_row_slice(plan, 0, plan.rows, root=root, source=source, tally=tally)


def generate_row_slice(
    plan: GeneratorPlan,
    start: int,
    stop: int,
    *,
    root: Optional[SeedTreeRoot] = None,
    source: Optional[StreamSource] = None,
    plugin_values: Optional[Mapping[str, Sequence[Any]]] = None,
    tally: Optional["DatesTally"] = None,
) -> Tuple[Tuple[Any, ...], ...]:
    """Rows *start* to *stop* - 1 of :func:`generate_rows`, exactly.

    Every cell's stream is addressed by its row index (§1), never by the
    order the generator reaches it, so a slice holds the same rows wherever
    and whenever it is generated. That is what lets a run cut its rows into
    slices, generate them in several processes and join them in order.

    A plan whose expose a generator plugin generates (``plan.plugin``) is given the values the
    plugin returned for exactly these rows, by column name, as *plugin_values*
    (:func:`flux_engine.generator.plugin.generate_values`): they are put into the rows before any
    rule is applied, so a rule reads them as it reads any cell.
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
    if plan.projection is not None:
        # A projected emit draws nothing: each row is a row of the history it projects.
        from flux_engine.generator.projections import projection_rows

        return projection_rows(plan, start, stop, root)
    streams = _stream_source(plan, root, source)
    if plan.rules is not None and plan.rules.windowed:
        # A window over siblings reads rows of every slice: the expose is made whole, once per
        # process, and a slice is cut from it (ADR 0022 section 7).
        return _whole_rows(plan, streams)[start:stop]
    rows = _cell_rows(plan, start, stop, streams)
    if plan.plugin is not None:
        rows = plan.plugin.overlay(rows, plugin_values)
    if plan.rules is None or not (plan.rules.order or plan.rules.links):
        return rows
    # The rules set their columns after the row's own cells are drawn.
    from flux_engine.generator.rules import apply_rules

    root_of_pass = getattr(streams, "root", None)
    return apply_rules(
        plan,
        rows,
        start,
        root=root_of_pass if isinstance(root_of_pass, SeedTreeRoot) else None,
        tally=tally,
    )


def _cell_rows(
    plan: GeneratorPlan, start: int, stop: int, streams: _Source
) -> Tuple[Tuple[Any, ...], ...]:
    """Rows *start* to *stop* - 1 as their own cells draw them, before any rule sets a column.

    A history's ending versions then take their end value (ADR 0017 section 5).
    """
    return _with_end_values(plan, start, _drawn_rows(plan, start, stop, streams), streams)


def _with_end_values(
    plan: GeneratorPlan, start: int, rows: Tuple[Tuple[Any, ...], ...], streams: _Source
) -> Tuple[Tuple[Any, ...], ...]:
    """*rows*, with the end value written in the end column of each ending version among them."""
    from flux_engine.generator.relations import history_pick_of, owner_of

    pick = history_pick_of(plan)
    end = None if pick is None else pick.entry.end
    if pick is None or end is None or end.column is None or end.value is None or not rows:
        return rows
    reader = _relation_reader(streams, plan)
    ending = reader.ending_rows(
        owner_of(plan), pick, np.arange(start, start + len(rows), dtype=np.int64)
    )
    if not ending.any():
        return rows
    position = [column_plan.column.name for column_plan in plan.columns].index(end.column)
    value = _level_to_value(end.value, plan.columns[position].column)
    return tuple(
        (*row[:position], value, *row[position + 1 :]) if is_ending else row
        for row, is_ending in zip(rows, ending.tolist(), strict=True)
    )


def _drawn_rows(
    plan: GeneratorPlan, start: int, stop: int, streams: _Source
) -> Tuple[Tuple[Any, ...], ...]:
    """Rows *start* to *stop* - 1 as their cells draw them, by the column path or row by row."""
    computed: Optional[Tuple[Tuple[Any, ...], ...]] = None
    if plan.counter_based and isinstance(streams, CellStreams):
        # Kernel 0.2.0: the slice a column at a time (ADR 0010, step 4). It leaves a
        # conditional or a refused slice to the per-row path below, and its rows take the
        # rules, a contract's and a bundle's, as the per-row path's do.
        from flux_engine.generator import columns as _columns

        computed = _columns.column_rows(plan, start, stop, streams)
    expose_id = plan.expose.expose_id
    givens = _given_columns(plan)
    if computed is not None:
        return computed
    if plan.relations is not None:
        return _related_rows(plan, start, stop, streams, givens)
    if givens or plan.conditions is not None:
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


#: The expose a process made whole for a window over siblings, newest last: the plan and the root
#: it was made for, the digests that identify them, and its rows. At most :data:`_WHOLE_LIMIT`
#: are kept, so a process that reads several exposes whole holds a bounded number of them;
#: :func:`clear_memos` drops them. A kept expose is a pure function of the plan and the root.
_WHOLE: List[Dict[str, Any]] = []
_WHOLE_LIMIT = 4


def _whole_rows(plan: GeneratorPlan, streams: _Source) -> Tuple[Tuple[Any, ...], ...]:
    """Every row of *plan*'s expose, rules applied step by step over all of them, kept per process.

    A pass over a caller's own stream *source*, with no run root, is not kept: nothing says what
    it would be a function of.
    """
    from flux_engine.generator.rules import apply_rules_whole

    root_of_pass = getattr(streams, "root", None)
    root = root_of_pass if isinstance(root_of_pass, SeedTreeRoot) else None
    if root is None or not isinstance(streams, CellStreams):
        return apply_rules_whole(plan, _cell_rows(plan, 0, plan.rows, streams), root)
    for held in _WHOLE:
        if held["plan"] is plan and held["root"] is root:
            return held["rows"]
    key = (plan.digest().value, root.digest, root.seed_tree_version)
    for held in _WHOLE:
        if held["key"] == key:
            held["plan"], held["root"] = plan, root
            return held["rows"]
    rows = apply_rules_whole(plan, _cell_rows(plan, 0, plan.rows, streams), root)
    _WHOLE.append({"plan": plan, "root": root, "key": key, "rows": rows})
    del _WHOLE[:-_WHOLE_LIMIT]
    return rows


def _relation_reader(streams: _Source, plan: Optional[GeneratorPlan] = None) -> Any:
    """A :class:`~flux_engine.generator.relations.RelationReader` over the pass's run root.

    A related column reads its parent's cells, and the cells of its own
    relationship's first column, by their addresses. Those reads take a new
    stream at each address, which only a run root can hand out: a caller's
    stream table may continue a stream it handed out before. So a pass over a
    plan with relations needs the root, from
    :class:`~flux_engine.generator.paths.CellStreams` or from a source that
    carries one.
    """
    # Imported here: relations imports this module, for the cell functions.
    from flux_engine.generator.relations import RelationReader

    root = getattr(streams, "root", None)
    if not isinstance(root, SeedTreeRoot):
        raise FluxError(
            FLX_2202,
            "a plan with related tables is generated over the run root: pass root=",
        )
    reader = RelationReader(root)
    if plan is not None:
        reader.know(plan)
    return reader


def _related_rows(
    plan: GeneratorPlan,
    start: int,
    stop: int,
    streams: _Source,
    givens: FrozenSet[str],
) -> Tuple[Tuple[Any, ...], ...]:
    """Rows *start* to *stop* - 1 of a plan whose contract relates its columns.

    The columns without a driver are drawn exactly as :func:`_conditional_row`
    and the plain path draw them, from the same streams, so a column the
    relations do not touch holds the same values it would without them. A
    column with a driver takes its value from the
    :class:`~flux_engine.generator.relations.RelationReader`. A foreign key
    another column is drawn given keeps the level it drew for that column and
    holds its parent's nearest key.
    """
    from flux_engine.generator.relations import carried_columns, history_pick_of, owner_of

    reader = _relation_reader(streams, plan)
    owner = owner_of(plan)
    sim_id, expose_id, h = plan.sim_id, plan.expose.expose_id, plan.seam_digest
    order = _row_order(plan, givens)
    relations = plan.relations
    assert relations is not None  # generate_row_slice routes only a related plan here
    drivers = [relations.driver(column_plan.column.name) for column_plan in plan.columns]
    linked = [relations.given_of(column_plan.column.name) for column_plan in plan.columns]
    # A history's carried columns take the cell of their entity's first version row (ADR 0017
    # section 6): every cell of such a column is the one its first row draws.
    pick = history_pick_of(plan)
    carried = carried_columns(plan)
    carries = [column_plan.column.name in carried for column_plan in plan.columns]

    def row(row_index: int) -> Tuple[Any, ...]:
        first = row_index if pick is None else reader.first_row_of(owner, pick, row_index)
        values: List[Any] = [None] * len(plan.columns)
        levels: Dict[str, str] = {}
        for index in order:
            column_plan, driver = plan.columns[index], drivers[index]
            name = column_plan.column.name
            link = linked[index]
            at = first if carries[index] else row_index
            given_level = (
                _level_given(column_plan.statistics, levels)
                if link is None
                else reader.linked_level(plan, link, at)
            )
            hit = (
                _conditioned_cell(plan, column_plan, streams, row_index, levels)
                if driver is None and link is None
                else None
            )
            if hit is not None:
                values[index], levels[name] = hit
            elif name in givens:
                value, levels[name] = _given_cell(
                    column_plan, streams, sim_id, expose_id, at, given_level
                )
                if driver is not None:
                    value = reader.declared_given(owner, driver, column_plan, value, at)
                values[index] = value
            elif driver is not None:
                values[index] = reader.cell(owner, column_plan, driver, at, given_level)
            else:
                values[index] = _cell(column_plan, streams, sim_id, expose_id, at, h, given_level)
        return tuple(values)

    return tuple(row(row_index) for row_index in range(start, stop))


def _determined_column(
    plan: GeneratorPlan, column: str, root: Optional[SeedTreeRoot], source: Optional[StreamSource]
) -> Tuple[Any, ...]:
    """A determined column's values: a function of the whole row, so from the row pass."""
    position = [column_plan.column.name for column_plan in plan.columns].index(column)
    rows = generate_row_slice(plan, 0, plan.rows, root=root, source=source)
    return tuple(row[position] for row in rows)


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
    if (
        plan.conditions is not None
        or plan.projection is not None
        or (plan.rules is not None and column in plan.rules.determined)
    ):
        # A conditioned cell reads the levels of the columns it is summed over, and a determined
        # one is a function of its whole row: both come from the row pass.
        return _determined_column(plan, column, root, source)
    streams = _stream_source(plan, root, source)
    values = _column_values(plan, column, streams)
    return _with_end_column(plan, column, values, streams)


def _carried_row(plan: GeneratorPlan, column: str, streams: _Source) -> Callable[[int], int]:
    """The row a column's cell at a row is drawn on: the row itself, or a carried column's first version row."""
    from flux_engine.generator.relations import carried_columns, history_pick_of, owner_of

    pick = history_pick_of(plan)
    if pick is None or column not in carried_columns(plan):
        return lambda index: index
    reader = _relation_reader(streams, plan)
    owner = owner_of(plan)
    return lambda index: reader.first_row_of(owner, pick, index)


def _with_end_column(
    plan: GeneratorPlan, column: str, values: Tuple[Any, ...], streams: _Source
) -> Tuple[Any, ...]:
    """*column*'s values, with the end value on each ending version when it is the history's end column."""
    from flux_engine.generator.relations import history_pick_of, owner_of

    pick = history_pick_of(plan)
    end = None if pick is None else pick.entry.end
    if pick is None or end is None or end.column != column or end.value is None:
        return values
    ending = _relation_reader(streams, plan).ending_rows(
        owner_of(plan), pick, np.arange(len(values), dtype=np.int64)
    )
    value = _level_to_value(end.value, plan.column_plan(column).column)
    return tuple(value if held else old for old, held in zip(values, ending.tolist(), strict=True))


def _column_values(plan: GeneratorPlan, column: str, streams: _Source) -> Tuple[Any, ...]:
    """:func:`generate_column`'s values before the end value is written."""
    column_plan = plan.column_plan(column)
    sim_id, expose_id, h = plan.sim_id, plan.expose.expose_id, plan.seam_digest
    chain = _ancestors(plan, column_plan)
    carried = _carried_row(plan, column, streams)
    relations = plan.relations
    linked_reader = None
    if relations is not None and (
        relations.given_of(column) is not None
        or any(relations.given_of(a.column.name) is not None for a in chain)
    ):
        linked_reader = _relation_reader(streams, plan)

    def given_level_of(target: ColumnPlan, levels: Dict[str, str], index: int) -> Optional[str]:
        """*target*'s given level: its linked row's, or the level of its parent drawn above."""
        link = None if relations is None else relations.given_of(target.column.name)
        if link is None:
            return _level_given(target.statistics, levels)
        assert linked_reader is not None  # built above for a plan that has a linked given
        return linked_reader.linked_level(plan, link, index)

    def level_at(index: int) -> Optional[str]:
        levels: Dict[str, str] = {}
        for ancestor in chain:
            _, levels[ancestor.column.name] = _given_cell(
                ancestor,
                streams,
                sim_id,
                expose_id,
                carried(index),
                given_level_of(ancestor, levels, carried(index)),
            )
        return given_level_of(column_plan, levels, carried(index))

    driver = None if plan.relations is None else plan.relations.driver(column)
    if driver is not None:
        from flux_engine.generator.relations import owner_of

        reader = _relation_reader(streams, plan)
        owner = owner_of(plan)
        if column in _given_columns(plan):
            # What the row pass writes for a foreign key another column reads.
            return tuple(
                reader.declared_given(
                    owner,
                    driver,
                    column_plan,
                    _given_cell(
                        column_plan, streams, sim_id, expose_id, carried(index), level_at(index)
                    )[0],
                    carried(index),
                )
                for index in range(plan.rows)
            )
        return tuple(
            reader.cell(owner, column_plan, driver, carried(index), level_at(index))
            for index in range(plan.rows)
        )
    return tuple(
        _cell(column_plan, streams, sim_id, expose_id, carried(index), h, level_at(index))
        for index in range(plan.rows)
    )
