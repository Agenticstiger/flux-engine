# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Declared aggregate statistics: what the generator is allowed to know.

SEAM-FIDELITY-SPEC §1: the generator picks a family and its parameters
"from the column's declared aggregate statistics ... or, absent
statistics, from the type default". ADR-0003 makes that a rule rather than
a preference: the simulation is built from aggregate statistics and never
from real records, so the *only* input this module reads is a declaration
-- a range, a support set, a marginal rate, a set of histogram edges. It
never sees a record and has no way to.

The declarations live in the fidelity block of §2, under
``emits[].fidelity.columns[<column>]``, plus the histogram edges §2 puts
on a ``marginalHistogram`` property. Everything read here is enumerated in
:data:`COLUMN_STATISTIC_KEYS`; a key the block carries that is not in that
set is left alone (the block is a contract between an owner and a
verifier, and the generator is not its only reader).

**Nothing is defaulted out of nothing.** Every parameter is either
declared or a documented function of a declaration -- the midpoint of a
declared range, the equal weights of a declared support. A family whose
parameters are neither is refused (``FLX-3402``) rather than run with an
invented distribution. This is the whole difference between a synthetic
dataset a data owner can account for and one that merely looks plausible.

**Numbers are exact.** §2: "Numbers are strings (exact decimals), never
floats." A number arrives here as a ``str``, an ``int`` or a ``Decimal``
and leaves as a ``Decimal``; a ``float`` is refused with ``FLX-3002``, the
same code the seam gives a float in a row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, FrozenSet, Mapping, Optional, Sequence, Tuple

from flux_engine.concordance.predicate import (
    COLUMN_KEYS,
    SUPPORT_DECLARED,
    read_support_levels,
    require_number_length,
)
from flux_engine.core.canonical import nfc
from flux_engine.core.clock import parse_rfc3339_ms
from flux_engine.core.errors import FluxError, register
from flux_engine.core.lrm import FLX_5020, WEIGHT_SUM_TOLERANCE
from flux_engine.seam.conform import DATE_PATTERN, FLX_3002
from flux_engine.seam.contract import CompiledColumn, CompiledExpose, LogicalKind

__all__ = [
    "COLUMN_STATISTIC_KEYS",
    "DATE_EPOCH_ORDINAL",
    "FLX_3402",
    "ColumnStatistics",
    "domain_value",
    "exact_decimal",
    "fidelity_columns",
    "statistics_for",
    "statistics_from_fidelity",
]

#: ``date(1970, 1, 1).toordinal()``. A ``DATE`` column's declared range and
#: bin edges are carried on the grid of days since the Unix epoch, which is
#: what the virtual clock's milliseconds divide down to -- so a ``DATE`` and
#: a ``TIMESTAMP`` bound written in the same calendar order stay in that
#: order after both are read onto their grids.
DATE_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()

FLX_3402 = register(
    "FLX-3402",
    "declared statistics do not parameterise the chosen family",
    "SEAM-FIDELITY-SPEC §1 draws every column from a distribution family "
    "whose parameters come from the column's declared aggregate statistics. "
    "A parameter that is neither declared nor a documented function of a "
    "declaration -- the midpoint of a declared range, the equal weights of "
    "a declared support -- would have to be invented, and an invented "
    "parameter makes a dataset nobody can account for. Declare the range, "
    "support, marginal rate or histogram the family needs.",
)

#: Every key a column's fidelity declaration may carry -- **the same set
#: the fidelity check parses**
#: (:data:`flux_engine.concordance.predicate.COLUMN_KEYS`), imported
#: rather than restated. Four of them (``range``, ``support``,
#: ``maxCardinality``, ``null``) are read by both readers; the rest are
#: the aggregate draw parameters SEAM-FIDELITY-SPEC §1 needs and §2's
#: example does not exercise. All of them aggregate quantities, none of
#: them record-level.
#:
#: The two sets used to differ, and ``fluxsim apply`` narrowed the block
#: before the check saw it -- silently dropping every property whose
#: column carried a generator key. One set means neither reader refuses a
#: key the other owns, and nothing narrows the block any more.
COLUMN_STATISTIC_KEYS: FrozenSet[str] = frozenset(COLUMN_KEYS)


def exact_decimal(value: Any, *, what: str) -> Decimal:
    """Read *value* as an exact :class:`~decimal.Decimal`.

    ``str``, ``int`` and ``Decimal`` are exact and are accepted; ``float``
    is refused with ``FLX-3002``, because a binary float in a declared
    statistic would put an unreproducible number on the generator's own
    seed-addressed path.

    A number too long to write out is refused by the check's own rule
    (:func:`~flux_engine.concordance.predicate.require_number_length`,
    ``FLX-5030``). A draw parameter such as ``mean`` is read by the
    generator alone, so ``mean: "1E+99999999"`` used to pass the check and
    stop ``plan`` on an uncaught ``decimal.Overflow`` while rows were drawn.
    """
    if isinstance(value, bool):
        raise FluxError(FLX_3402, f"{what}: a bool is not a number")
    if isinstance(value, float):
        raise FluxError(FLX_3002, f"{what}: declared statistics are exact decimals")
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, str):
        try:
            number = Decimal(value.strip())
        except InvalidOperation as exc:
            raise FluxError(FLX_3402, f"{what}: {value!r} is not a decimal number") from exc
    else:
        raise FluxError(FLX_3402, f"{what}: value class {type(value).__name__!r}")
    if not number.is_finite():
        raise FluxError(FLX_3402, f"{what}: an infinite or not-a-number value has no domain")
    return require_number_length(number, what)


def domain_value(value: Any, column: CompiledColumn, *, what: str) -> Decimal:
    """Read a declared bound or bin edge onto *column*'s own grid.

    SEAM-FIDELITY-SPEC §2's own example declares a ``TIMESTAMP`` column's
    range in RFC 3339 (``{min: "2026-07-01T00:00:00Z", max:
    "2026-09-30T23:59:59Z"}``), so a bound is read in the units of the
    column it bounds, not as a bare decimal:

    * ``TIMESTAMP`` -- an RFC 3339 timestamp, or an integer already in
      milliseconds since the Unix epoch. Parsed by
      :func:`flux_engine.core.clock.parse_rfc3339_ms`, so the seam, the
      scheduler and the generator all read one grammar.
    * ``DATE`` -- a ``YYYY-MM-DD`` date, or an integer already in days
      since the Unix epoch.
    * everything else -- an exact decimal.

    The declared spelling is kept beside the parsed value (see
    :attr:`ColumnStatistics.range_min_text`) so ``generatorDigest`` covers
    what the author wrote.

    The clock refuses a malformed timestamp with its own numerics-band code
    (``FLX-5022``). A malformed *declaration* is a declaration problem, so
    it is re-raised as ``FLX-3402`` carrying the clock's own explanation --
    a caller reading a plan-time refusal should not have to know which
    kernel module the reader delegates to.
    """
    kind = column.logical_type.kind
    if kind is LogicalKind.TIMESTAMP:
        if isinstance(value, str):
            try:
                return Decimal(parse_rfc3339_ms(value))
            except FluxError as exc:
                raise FluxError(FLX_3402, f"{what}: {exc.detail or exc.code.title}") from exc
        if not isinstance(value, bool) and isinstance(value, int):
            return Decimal(value)
        raise FluxError(
            FLX_3402,
            f"{what}: a TIMESTAMP bound is an RFC 3339 timestamp or integer milliseconds, "
            f"got {type(value).__name__}",
        )
    if kind is LogicalKind.DATE:
        if isinstance(value, str):
            if DATE_PATTERN.match(value) is None:
                raise FluxError(FLX_3402, f"{what}: a DATE bound is spelled YYYY-MM-DD")
            try:
                parsed = date.fromisoformat(value)
            except ValueError as exc:
                raise FluxError(FLX_3402, f"{what}: not a calendar date") from exc
            return Decimal(parsed.toordinal() - DATE_EPOCH_ORDINAL)
        if not isinstance(value, bool) and isinstance(value, int):
            return Decimal(value)
        raise FluxError(
            FLX_3402,
            f"{what}: a DATE bound is a YYYY-MM-DD string or integer days since the epoch, "
            f"got {type(value).__name__}",
        )
    return exact_decimal(value, what=what)


@dataclass(frozen=True)
class ColumnStatistics:
    """One column's declared aggregate statistics.

    Every field is ``None`` when the column declares nothing for it, so
    "absent" and "declared as zero" are different states -- which they
    have to be, because a declared ``nullFraction`` of ``0`` is a promise
    and an absent one is silence.

    ``support`` carries the levels whichever spelling declared them
    (:func:`flux_engine.concordance.predicate.read_support_levels`):
    ``{values: [...]}`` writes them out, ``declared`` takes them from the
    FLUID column type's own ``enum(...)`` list, and a column that spells
    ``declared`` over a type that names no levels is refused there rather
    than arriving here with an empty set.
    :attr:`support_is_declared` records which spelling was used, so the
    generator digest covers what the author wrote and not only what it
    resolved to.
    """

    column: str
    range_min: Optional[Decimal] = None
    range_max: Optional[Decimal] = None
    range_min_text: Optional[str] = None
    range_max_text: Optional[str] = None
    support: Optional[Tuple[str, ...]] = None
    support_is_declared: bool = False
    max_cardinality: Optional[int] = None
    null_policy: Optional[str] = None
    null_fraction: Optional[Decimal] = None
    unique: bool = False
    levels: Optional[Mapping[str, Decimal]] = None
    mean: Optional[Decimal] = None
    stddev: Optional[Decimal] = None
    rate: Optional[Decimal] = None
    bins: Optional[Tuple[Decimal, ...]] = None
    bin_texts: Optional[Tuple[str, ...]] = None
    bin_weights: Optional[Tuple[Decimal, ...]] = None
    family: Optional[str] = None
    identifier_length: Optional[int] = None
    #: The column this one is drawn conditionally on (``given``), and, per
    #: level of that column, the weights to draw with: one ``binWeights``
    #: list for a histogram column (``binWeightsGiven``), or one level ->
    #: weight mapping for a categorical column (``levelWeightsGiven``). They
    #: come from (group, bin) or (level, level) counts, aggregates of S, so
    #: a conditional draw is still aggregate-only.
    given: Optional[str] = None
    bin_weights_given: Optional[Mapping[str, Tuple[Decimal, ...]]] = None
    level_weights_given: Optional[Mapping[str, Mapping[str, Decimal]]] = None

    @property
    def has_range(self) -> bool:
        """Whether both ends of a range were declared."""
        return self.range_min is not None and self.range_max is not None

    def as_document(self) -> Dict[str, Any]:
        """The declaration, as canonical-ready JSON for the generator digest.

        Numbers are rendered as their exact decimal strings -- the same
        spelling §2 requires them to be written in -- so the digest covers
        the declared value rather than a re-formatted one.
        """
        document: Dict[str, Any] = {"column": self.column}
        if self.has_range:
            document["range"] = {
                "min": self.range_min_text or str(self.range_min),
                "max": self.range_max_text or str(self.range_max),
            }
        if self.support is not None:
            document["support"] = list(self.support)
        if self.support_is_declared:
            document["supportIsDeclared"] = True
        if self.max_cardinality is not None:
            document["maxCardinality"] = self.max_cardinality
        if self.null_policy is not None:
            document["null"] = self.null_policy
        if self.null_fraction is not None:
            document["nullFraction"] = str(self.null_fraction)
        if self.unique:
            document["unique"] = True
        if self.levels is not None:
            document["levels"] = {key: str(value) for key, value in sorted(self.levels.items())}
        for name, value in (
            ("mean", self.mean),
            ("stddev", self.stddev),
            ("rate", self.rate),
        ):
            if value is not None:
                document[name] = str(value)
        if self.bins is not None:
            document["bins"] = list(self.bin_texts or tuple(str(edge) for edge in self.bins))
        if self.bin_weights is not None:
            document["binWeights"] = [str(weight) for weight in self.bin_weights]
        if self.family is not None:
            document["family"] = self.family
        if self.identifier_length is not None:
            document["identifierLength"] = self.identifier_length
        if self.given is not None:
            document["given"] = self.given
        if self.bin_weights_given is not None:
            document["binWeightsGiven"] = {
                level: [str(weight) for weight in weights]
                for level, weights in sorted(self.bin_weights_given.items())
            }
        if self.level_weights_given is not None:
            document["levelWeightsGiven"] = {
                level: {key: str(weight) for key, weight in sorted(weights.items())}
                for level, weights in sorted(self.level_weights_given.items())
            }
        return document


def fidelity_columns(fidelity: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Return the ``columns`` mapping of a fidelity block, or an empty one."""
    if not isinstance(fidelity, Mapping):
        return {}
    columns = fidelity.get("columns")
    return columns if isinstance(columns, Mapping) else {}


def _histogram_bins(
    fidelity: Optional[Mapping[str, Any]], column: CompiledColumn
) -> Optional[Tuple[Tuple[Decimal, ...], Tuple[str, ...]]]:
    """The declared ``marginalHistogram`` edges for *column*, if §2 declares any.

    §2 puts the bin edges on the property rather than on the column, and
    they are a genuine declared aggregate: the generator honours them so
    the dataset it produces is shaped by the same edges the check will be
    evaluated on.
    """
    if not isinstance(fidelity, Mapping):
        return None
    properties = fidelity.get("properties")
    if not isinstance(properties, Sequence) or isinstance(properties, (str, bytes)):
        return None
    for entry in properties:
        if not isinstance(entry, Mapping):
            continue
        if nfc(str(entry.get("property", ""))) != "marginalHistogram":
            continue
        if nfc(str(entry.get("column", ""))) != column.name:
            continue
        edges = entry.get("bins")
        if not isinstance(edges, Sequence) or isinstance(edges, (str, bytes)):
            continue
        texts = tuple(str(edge) for edge in edges)
        return (
            tuple(
                domain_value(edge, column, what=f"{column.name}: marginalHistogram bin {index}")
                for index, edge in enumerate(edges)
            ),
            texts,
        )
    return None


def _read_support(value: Any, column: CompiledColumn) -> Tuple[Optional[Tuple[str, ...]], bool]:
    """Read a column's ``support`` through the one shared reader.

    :func:`flux_engine.concordance.predicate.read_support_levels` owns both
    spellings -- ``{values: [...]}`` and the word ``declared`` -- so the
    generator and the check never disagree about what a support *is*. The
    ``declared`` spelling resolves against the column's own FLUID type
    (``enum('IE', 'GB')``); a column that spells it and declares no enum is
    refused there, which is why this generator no longer carries a family
    that quietly has no levels to draw from.

    The refusal is re-banded onto ``FLX-3402`` -- "declared statistics do
    not parameterise the chosen family" -- because at this call site the
    reader's numerics-band code would name a subsystem the caller of
    :func:`statistics_for` never asked about.
    """
    try:
        levels, source = read_support_levels(
            value,
            subject=f"{column.name}: support",
            declared_type=column.declared_type,
        )
    except FluxError as exc:
        raise FluxError(FLX_3402, exc.detail or exc.code.title) from exc
    return levels, source == SUPPORT_DECLARED


def _read_levels(value: Any, column: str) -> Mapping[str, Decimal]:
    if not isinstance(value, Mapping) or not value:
        raise FluxError(
            FLX_3402, f"{column}: levels is a non-empty mapping of level name to weight"
        )
    levels: Dict[str, Decimal] = {}
    for key, weight in value.items():
        name = nfc(str(key))
        if name in levels:
            raise FluxError(FLX_3402, f"{column}: two level names normalise onto {name!r}")
        parsed = exact_decimal(weight, what=f"{column}: levels[{name}]")
        if parsed < 0:
            raise FluxError(FLX_3402, f"{column}: levels[{name}] is negative")
        levels[name] = parsed
    # A declared level set is a declared distribution: it sums to one, or it
    # is refused here, at parse time, where the diagnostic can still name the
    # column. Only the sub-tolerance residue a decimal rendering leaves
    # behind is folded away later (``rows._renormalised``); anything larger
    # would draw a level at a weight nobody wrote down.
    total = sum(levels.values(), Decimal(0))
    if abs(total - 1) > WEIGHT_SUM_TOLERANCE:
        raise FluxError(
            FLX_5020,
            f"{column}: levels sum to {total}, which is further than "
            f"{WEIGHT_SUM_TOLERANCE} from 1",
        )
    return levels


def _read_given(value: Any, column: str) -> str:
    if not isinstance(value, str) or not value:
        raise FluxError(
            FLX_3402, f"{column}: given names the column this one is drawn conditionally on"
        )
    parent = nfc(value)
    if parent == column:
        raise FluxError(FLX_3402, f"{column}: a column is not drawn conditionally on itself")
    return parent


def _read_bin_weights_given(
    value: Any, column: str, bins: Optional[Tuple[Decimal, ...]]
) -> Dict[str, Tuple[Decimal, ...]]:
    """One ``binWeights`` list per level of the ``given`` column.

    Each list is read exactly as ``binWeights`` is, against the same edges,
    and must carry some mass: a level whose row could not be drawn would
    fail on the first row that reaches it, so it is refused here instead.
    """
    if bins is None:
        raise FluxError(FLX_3402, f"{column}: binWeightsGiven declared without bins")
    if not isinstance(value, Mapping) or not value:
        raise FluxError(
            FLX_3402,
            f"{column}: binWeightsGiven maps each level of the given column to a binWeights list",
        )
    table: Dict[str, Tuple[Decimal, ...]] = {}
    for key, raw in value.items():
        level = nfc(str(key))
        if level in table:
            raise FluxError(
                FLX_3402, f"{column}: two binWeightsGiven levels normalise onto {level!r}"
            )
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise FluxError(FLX_3402, f"{column}: binWeightsGiven[{level}] is a list of weights")
        weights = tuple(
            exact_decimal(weight, what=f"{column}: binWeightsGiven[{level}][{index}]")
            for index, weight in enumerate(raw)
        )
        if len(weights) != len(bins) - 1:
            raise FluxError(
                FLX_3402,
                f"{column}: {len(bins)} edges make {len(bins) - 1} bins; "
                f"binWeightsGiven[{level}] carries {len(weights)}",
            )
        if any(weight < 0 for weight in weights):
            raise FluxError(
                FLX_3402, f"{column}: binWeightsGiven[{level}] carries a negative weight"
            )
        if sum(weights, Decimal(0)) <= 0:
            raise FluxError(FLX_3402, f"{column}: binWeightsGiven[{level}] sums to zero")
        table[level] = weights
    return table


def _read_level_weights_given(value: Any, column: str) -> Dict[str, Dict[str, Decimal]]:
    """One level -> weight mapping per level of the ``given`` column.

    The weights are counts or shares, normalised per given level when drawn,
    like ``binWeights``. Every mapping names the same levels (the column's
    own level set) and carries some mass, so every row can be drawn.
    """
    if not isinstance(value, Mapping) or not value:
        raise FluxError(
            FLX_3402,
            f"{column}: levelWeightsGiven maps each level of the given column to a "
            "level -> weight mapping",
        )
    table: Dict[str, Dict[str, Decimal]] = {}
    names: Optional[Tuple[str, ...]] = None
    for key, raw in value.items():
        given_level = nfc(str(key))
        if given_level in table:
            raise FluxError(
                FLX_3402, f"{column}: two levelWeightsGiven levels normalise onto {given_level!r}"
            )
        if not isinstance(raw, Mapping) or not raw:
            raise FluxError(
                FLX_3402,
                f"{column}: levelWeightsGiven[{given_level}] is a non-empty level -> weight mapping",
            )
        weights: Dict[str, Decimal] = {}
        for level_key, weight in raw.items():
            level = nfc(str(level_key))
            if level in weights:
                raise FluxError(
                    FLX_3402,
                    f"{column}: two levels of levelWeightsGiven[{given_level}] normalise "
                    f"onto {level!r}",
                )
            parsed = exact_decimal(
                weight, what=f"{column}: levelWeightsGiven[{given_level}][{level}]"
            )
            if parsed < 0:
                raise FluxError(
                    FLX_3402, f"{column}: levelWeightsGiven[{given_level}][{level}] is negative"
                )
            weights[level] = parsed
        if sum(weights.values(), Decimal(0)) <= 0:
            raise FluxError(FLX_3402, f"{column}: levelWeightsGiven[{given_level}] sums to zero")
        these = tuple(sorted(weights))
        if names is None:
            names = these
        elif these != names:
            raise FluxError(
                FLX_3402,
                f"{column}: levelWeightsGiven[{given_level}] names levels {list(these)}; "
                f"the first given level names {list(names)}",
            )
        table[given_level] = weights
    return table


def _read_conditional(
    block: Mapping[str, Any],
    name: str,
    bins: Optional[Tuple[Decimal, ...]],
    bin_weights: Optional[Tuple[Decimal, ...]],
) -> Tuple[
    Optional[str],
    Optional[Dict[str, Tuple[Decimal, ...]]],
    Optional[Dict[str, Dict[str, Decimal]]],
]:
    """``given`` and its one weight table, read and paired, or three ``None``.

    ``given`` names the column this one is drawn conditionally on, and it comes
    with exactly one table: ``binWeightsGiven`` in place of ``binWeights``, or
    ``levelWeightsGiven`` in place of ``levels``.
    """
    given = _read_given(block["given"], name) if "given" in block else None
    bin_weights_given = (
        _read_bin_weights_given(block["binWeightsGiven"], name, bins)
        if "binWeightsGiven" in block
        else None
    )
    level_weights_given = (
        _read_level_weights_given(block["levelWeightsGiven"], name)
        if "levelWeightsGiven" in block
        else None
    )
    tables = (bin_weights_given is not None) + (level_weights_given is not None)
    if tables > 1:
        raise FluxError(FLX_3402, f"{name}: declare binWeightsGiven or levelWeightsGiven, not both")
    if (given is None) != (tables == 0):
        raise FluxError(
            FLX_3402,
            f"{name}: given is declared together with binWeightsGiven or levelWeightsGiven "
            "(the column conditioned on, and one weight table per level of it)",
        )
    if bin_weights_given is not None and bin_weights is not None:
        raise FluxError(FLX_3402, f"{name}: declare binWeights or binWeightsGiven, not both")
    if level_weights_given is not None and "levels" in block:
        raise FluxError(FLX_3402, f"{name}: declare levels or levelWeightsGiven, not both")
    return given, bin_weights_given, level_weights_given


def _read_positive_int(value: Any, column: str, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FluxError(FLX_3402, f"{column}: {what} must be an integer >= 1")
    return value


def statistics_for(
    column: CompiledColumn,
    fidelity: Optional[Mapping[str, Any]] = None,
) -> ColumnStatistics:
    """Read one column's declared statistics out of a fidelity block.

    Takes the *compiled* column, not just its name, because a declared
    bound is read in the units of the type it bounds -- see
    :func:`domain_value`.
    """
    name = column.name
    declaration = fidelity_columns(fidelity).get(name)
    block: Mapping[str, Any] = declaration if isinstance(declaration, Mapping) else {}

    range_min = range_max = None
    range_min_text = range_max_text = None
    declared_range = block.get("range")
    if isinstance(declared_range, Mapping):
        if "min" not in declared_range or "max" not in declared_range:
            raise FluxError(FLX_3402, f"{name}: a range declares both min and max")
        range_min_text = str(declared_range["min"])
        range_max_text = str(declared_range["max"])
        range_min = domain_value(declared_range["min"], column, what=f"{name}: range.min")
        range_max = domain_value(declared_range["max"], column, what=f"{name}: range.max")
        if range_min > range_max:
            raise FluxError(FLX_3402, f"{name}: range.min is above range.max")
    elif declared_range is not None:
        raise FluxError(FLX_3402, f"{name}: range class {type(declared_range).__name__!r}")

    support: Optional[Tuple[str, ...]] = None
    support_is_declared = False
    if "support" in block:
        support, support_is_declared = _read_support(block["support"], column)

    histogram = _histogram_bins(fidelity, column)
    bins = bin_texts = None
    if "bins" in block:
        raw_bins = block["bins"]
        if not isinstance(raw_bins, Sequence) or isinstance(raw_bins, (str, bytes)):
            raise FluxError(FLX_3402, f"{name}: bins is a list of edges")
        bin_texts = tuple(str(edge) for edge in raw_bins)
        bins = tuple(
            domain_value(edge, column, what=f"{name}: bins[{index}]")
            for index, edge in enumerate(raw_bins)
        )
    elif histogram is not None:
        bins, bin_texts = histogram
    if bins is not None:
        if len(bins) < 2:
            raise FluxError(FLX_3402, f"{name}: bins declares fewer than two edges")
        if any(later <= earlier for earlier, later in zip(bins, bins[1:], strict=False)):
            raise FluxError(FLX_3402, f"{name}: bins are not strictly increasing")

    bin_weights = None
    if "binWeights" in block:
        raw_weights = block["binWeights"]
        if not isinstance(raw_weights, Sequence) or isinstance(raw_weights, (str, bytes)):
            raise FluxError(FLX_3402, f"{name}: binWeights is a list of weights")
        bin_weights = tuple(
            exact_decimal(weight, what=f"{name}: binWeights[{index}]")
            for index, weight in enumerate(raw_weights)
        )
        if bins is None:
            raise FluxError(FLX_3402, f"{name}: binWeights declared without bins")
        if len(bin_weights) != len(bins) - 1:
            raise FluxError(
                FLX_3402,
                f"{name}: {len(bins)} edges make {len(bins) - 1} bins; "
                f"binWeights carries {len(bin_weights)}",
            )
        if any(weight < 0 for weight in bin_weights):
            raise FluxError(FLX_3402, f"{name}: binWeights carries a negative weight")
        # Refused here, where the plan is formed, and not on the first row
        # drawn: with no mass in any bin no row can be drawn at all, and a
        # plan with no rows would otherwise pass.
        if not any(weight > 0 for weight in bin_weights):
            raise FluxError(
                FLX_3402,
                f"{name}: binWeights sums to zero, so no bin can be drawn; "
                "give at least one bin a weight above zero",
            )

    given, bin_weights_given, level_weights_given = _read_conditional(
        block, name, bins, bin_weights
    )

    null_policy = block.get("null")
    if null_policy is not None and not isinstance(null_policy, str):
        raise FluxError(FLX_3402, f"{name}: null policy class {type(null_policy).__name__!r}")

    null_fraction = None
    if "nullFraction" in block:
        null_fraction = exact_decimal(block["nullFraction"], what=f"{name}: nullFraction")
        if not Decimal(0) <= null_fraction <= Decimal(1):
            raise FluxError(FLX_3402, f"{name}: nullFraction lies outside [0, 1]")

    return ColumnStatistics(
        column=name,
        range_min=range_min,
        range_max=range_max,
        range_min_text=range_min_text,
        range_max_text=range_max_text,
        support=support,
        support_is_declared=support_is_declared,
        max_cardinality=(
            _read_positive_int(block["maxCardinality"], name, "maxCardinality")
            if "maxCardinality" in block
            else None
        ),
        null_policy=nfc(null_policy) if isinstance(null_policy, str) else None,
        null_fraction=null_fraction,
        unique=bool(block.get("unique", False)),
        levels=_read_levels(block["levels"], name) if "levels" in block else None,
        mean=exact_decimal(block["mean"], what=f"{name}: mean") if "mean" in block else None,
        stddev=(
            exact_decimal(block["stddev"], what=f"{name}: stddev") if "stddev" in block else None
        ),
        rate=exact_decimal(block["rate"], what=f"{name}: rate") if "rate" in block else None,
        bins=bins,
        bin_texts=bin_texts,
        bin_weights=bin_weights,
        family=nfc(str(block["family"])) if "family" in block else None,
        identifier_length=(
            _read_positive_int(block["identifierLength"], name, "identifierLength")
            if "identifierLength" in block
            else None
        ),
        given=given,
        bin_weights_given=bin_weights_given,
        level_weights_given=level_weights_given,
    )


def statistics_from_fidelity(
    expose: CompiledExpose, fidelity: Optional[Mapping[str, Any]] = None
) -> Mapping[str, ColumnStatistics]:
    """Read the declared statistics of every column of *expose*.

    A column the block does not mention gets an empty
    :class:`ColumnStatistics`, which is a declaration of nothing rather
    than a set of guesses. A ``columns`` entry naming a column the expose
    does not declare is refused (``FLX-3402``) -- §2 requires every
    ``column`` to be "a member of the referenced expose schema", and a
    declaration attached to nothing is a declaration whose author believes
    something untrue about the contract.
    """
    declared = fidelity_columns(fidelity)
    known = set(expose.column_names)
    for name in sorted(declared):
        if nfc(str(name)) not in known:
            raise FluxError(
                FLX_3402,
                f"fidelity declares column {name!r}, which expose "
                f"{expose.expose_id!r} does not: {sorted(known)}",
            )
    return {column.name: statistics_for(column, fidelity) for column in expose.columns}
