# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Parse and validate the fidelity block -- SEAM-FIDELITY-SPEC §2.

The block lives in RFC-02's document-level extension namespace, at
``Simulation.extensions["com.agentics.flux/1"].emits[<exposeId>].fidelity``
-- FLUX 0.5.0 closes ``spec.emits[]``, and the extension map is the one
sanctioned place a dialect may add a declaration without forking the
schema. This module turns the parsed YAML mapping into frozen
dataclasses, and refuses -- offline, before any statistic is formed --
everything §2 says ``fluxsim validate`` refuses:

    Rules enforced by ``fluxsim validate`` (offline): every ``column`` and
    ``groupBy`` is a member of the referenced expose schema; bins are
    strictly increasing and partition the declared range; a
    ``correlation`` property declares two columns and a ``minVariance``;
    every property's parameters are present (a contract missing any of
    them is refused before any statistic is formed). Numbers are strings
    (exact decimals), never floats.

Nothing here computes a digest, a margin or a statistic: those are
:mod:`flux_engine.concordance.compiler` and
:mod:`flux_engine.concordance.boundary.statistic`. This module answers one
question -- is the declaration well-formed and does it name things that
exist -- and answers it totally, with a refusal code per failure class.

Four readings of §2 this module fixes, each documented at its code site:

* a range bound is an exact decimal when it spells one and an RFC 3339
  timestamp otherwise (:func:`_read_bound`);
* a property may only name a column the block itself declares, because
  ``epsilon`` is computed "from ``encoding``, ``columns[].range``"
  (SEAM-FIDELITY-SPEC §3) and an undeclared column has no range;
* ``groupBy`` names a column declared with ``support``, because the group
  count of a continuous column is unbounded and ``records.min`` per group
  could not be checked against a declaration;
* ``support`` has exactly two spellings -- ``{values: [...]}`` and the
  word ``declared``, which takes the levels from the FLUID column type's
  own ``enum(...)`` list (:func:`read_support_levels`).

:data:`COLUMN_KEYS` is the **one** ``fidelity.columns.<name>`` key set,
shared with :mod:`flux_engine.generator.statistics`: the check reads four
of its keys and the generator reads the rest, and neither refuses a key
the other owns.

Every mapping is iterated by sorted key and every collection this module
returns is immutable, so two runs over one block build one object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from fractions import Fraction
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

from flux_engine.concordance.errors import (
    FLX_5030,
    FLX_5031,
    FLX_5032,
    FLX_5033,
    FLX_5034,
)
from flux_engine.core.canonical import nfc
from flux_engine.core.clock import parse_rfc3339_ms
from flux_engine.core.errors import FluxError
from flux_engine.core.fixedpoint import DEFAULT_FRACTIONAL_BITS, Grid, Interval
from flux_engine.seam.contract import declared_enum_values

__all__ = [
    "COLUMN_KEYS",
    "SUPPORT_DECLARED",
    "SUPPORT_VALUES_KEY",
    "Budget",
    "ColumnDeclaration",
    "ColumnKind",
    "DeclaredExpose",
    "Encoding",
    "ExposeSchema",
    "FidelityBlock",
    "FailureAction",
    "NullPolicy",
    "PropertyDeclaration",
    "PropertyKind",
    "RecordsRange",
    "Tolerance",
    "ToleranceKind",
    "ValueDomain",
    "expose_from_contract",
    "parse_duration_ms",
    "parse_fidelity_block",
    "read_support_levels",
]


# ---------------------------------------------------------------------------
# The expose the block is checked against
# ---------------------------------------------------------------------------


@runtime_checkable
class ExposeSchema(Protocol):
    """The expose a fidelity block's columns are checked for membership in.

    Structural, not nominal: the resolved expose is
    ``seam/contract.py: compile_expose``'s output (SEAM-FIDELITY-SPEC §1,
    "ordered columns with ``LogicalType``"), which does not exist yet.
    Naming the two members this module actually reads means the seam can
    supply its own compiled expose later without a change here, and means
    a test can supply a two-line stand-in without importing the seam.
    """

    @property
    def expose_id(self) -> str:
        """The id of the expose the emit entry references."""

    @property
    def column_names(self) -> Tuple[str, ...]:
        """The expose's declared column names, in declared order."""

    @property
    def column_types(self) -> Mapping[str, str]:
        """Column name -> the contract's own type spelling.

        Needed by ``support: declared``, which takes a column's levels from
        the declared type's ``enum(...)`` list
        (:func:`~flux_engine.seam.contract.declared_enum_values`). Both
        :class:`DeclaredExpose` and
        :class:`flux_engine.seam.contract.CompiledExpose` carry it.
        """


@dataclass(frozen=True)
class DeclaredExpose:
    """An expose read straight from a parsed FLUID contract document.

    Carries the declared column order, which KERNEL-SPEC §8 makes
    load-bearing (the emit stream is arrays aligned to that order, and the
    seam digest ``H`` quotes it) -- so this type keeps a tuple, never a
    set or a mapping whose iteration order would be someone else's choice.
    """

    expose_id: str
    column_names: Tuple[str, ...]
    # default_factory, not a bare MappingProxyType: Python 3.11 dataclasses reject an
    # unhashable default even for a frozen field (3.12 relaxed the check).
    column_types: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


def expose_from_contract(document: Mapping[str, Any], expose_id: str) -> DeclaredExpose:
    """Read one expose out of a parsed ``.fluid.yml`` document.

    A convenience for callers that have the contract and not a compiled
    expose. It reads only what §2's membership rule needs -- the ordered
    column names and their declared type spellings -- and refuses
    (``FLX-5030``) a document that does not carry the named expose or
    whose schema is not a list of named columns.
    """
    exposes = document.get("exposes")
    if not isinstance(exposes, Sequence) or isinstance(exposes, (str, bytes)):
        raise FluxError(FLX_5030, "contract has no exposes list")
    wanted = nfc(expose_id)
    for entry in exposes:
        if not isinstance(entry, Mapping):
            continue
        if nfc(str(entry.get("exposeId", ""))) != wanted:
            continue
        schema = entry.get("contract", {})
        columns = schema.get("schema") if isinstance(schema, Mapping) else None
        if not isinstance(columns, Sequence) or isinstance(columns, (str, bytes)):
            raise FluxError(FLX_5030, f"expose {expose_id!r} declares no schema")
        names: List[str] = []
        types: Dict[str, str] = {}
        for column in columns:
            if not isinstance(column, Mapping) or "name" not in column:
                raise FluxError(FLX_5030, f"expose {expose_id!r} has an unnamed column")
            name = nfc(str(column["name"]))
            names.append(name)
            types[name] = str(column.get("type", ""))
        return DeclaredExpose(
            expose_id=wanted,
            column_names=tuple(names),
            column_types=MappingProxyType(dict(sorted(types.items()))),
        )
    raise FluxError(FLX_5030, f"contract declares no expose {expose_id!r}")


# ---------------------------------------------------------------------------
# Closed sets
# ---------------------------------------------------------------------------


class NullPolicy(str, Enum):
    """What a column's declaration says about nulls (SEAM-FIDELITY-SPEC §2).

    ``EXCLUDE`` drops the record from every property over that column;
    ``FAIL`` refuses the check. There is no third option that would let a
    null be counted as a value.
    """

    EXCLUDE = "exclude"
    FAIL = "fail"


class ColumnKind(str, Enum):
    """The two column declarations §2's example shows.

    ``RANGE`` declares a closed numeric (or timestamp) interval and is
    what every encoded statistic needs. ``SUPPORT`` declares a categorical
    column whose committed member list is the thing compared, and carries
    a ``maxCardinality`` instead of an interval.
    """

    RANGE = "range"
    SUPPORT = "support"


class ValueDomain(str, Enum):
    """How a ``RANGE`` column's values are read before they are encoded.

    ``NUMBER`` values are exact decimals. ``TIMESTAMP`` values are RFC
    3339 strings converted to integer milliseconds by KERNEL-SPEC §7's
    parser, which is the only spelling of an instant this engine has.
    """

    NUMBER = "number"
    TIMESTAMP = "timestamp"


class PropertyKind(str, Enum):
    """The five properties SEAM-FIDELITY-SPEC §5 gives a comparison for."""

    MEAN = "mean"
    MARGINAL_HISTOGRAM = "marginalHistogram"
    SUPPORT = "support"
    CORRELATION = "correlation"
    QUANTILE = "quantile"


class ToleranceKind(str, Enum):
    """The tolerance spellings §2's example uses, plus ``absolute``.

    ``RELATIVE`` and ``ABSOLUTE`` carry ``value``; ``DISTANCE`` carries a
    ``measure`` and a ``max``; ``COUNT`` carries an integer ``max``.
    """

    ABSOLUTE = "absolute"
    RELATIVE = "relative"
    DISTANCE = "distance"
    COUNT = "count"


class FailureAction(str, Enum):
    """What the ship gate does when a property is outside its margin.

    One member. SEAM-FIDELITY-SPEC §7 gives the gate one behaviour --
    "write no bundle" -- so the field is a closed set with the one value
    the specification names rather than an open string that would let a
    bundle ship on a failed check by declaring a second one.
    """

    WITHHOLD_BUNDLE = "withholdBundle"


#: The measure a ``DISTANCE`` tolerance may name. §5 gives one distance
#: comparison, over total variation.
DISTANCE_MEASURES = ("totalVariation",)

#: Which tolerance kinds each property's comparison can consume. A mean
#: compares in the value domain (absolute) or against its own reference
#: (relative); a histogram compares a distance; a support check compares a
#: count of non-members; a correlation and a quantile compare an absolute
#: margin -- a correlation because it is already dimensionless, a quantile
#: because it is compared in the column's units.
_TOLERANCE_KINDS: Mapping[PropertyKind, Tuple[ToleranceKind, ...]] = MappingProxyType(
    {
        PropertyKind.MEAN: (ToleranceKind.ABSOLUTE, ToleranceKind.RELATIVE),
        PropertyKind.MARGINAL_HISTOGRAM: (ToleranceKind.DISTANCE,),
        PropertyKind.SUPPORT: (ToleranceKind.COUNT,),
        PropertyKind.CORRELATION: (ToleranceKind.ABSOLUTE,),
        PropertyKind.QUANTILE: (ToleranceKind.ABSOLUTE,),
    }
)

#: The column kind each property's comparison needs. A support check
#: compares member lists and needs a ``SUPPORT`` column; everything else
#: encodes values and needs a ``RANGE``.
_COLUMN_KINDS: Mapping[PropertyKind, ColumnKind] = MappingProxyType(
    {
        PropertyKind.MEAN: ColumnKind.RANGE,
        PropertyKind.MARGINAL_HISTOGRAM: ColumnKind.RANGE,
        PropertyKind.SUPPORT: ColumnKind.SUPPORT,
        PropertyKind.CORRELATION: ColumnKind.RANGE,
        PropertyKind.QUANTILE: ColumnKind.RANGE,
    }
)


# ---------------------------------------------------------------------------
# Reading exact numbers out of a declaration
# ---------------------------------------------------------------------------


def _require_mapping(value: Any, subject: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FluxError(FLX_5030, f"{subject} must be a mapping, got {type(value).__name__}")
    return value


def _known_keys(value: Mapping[str, Any], allowed: Tuple[str, ...], subject: str) -> None:
    """Refuse a key the closed shape of §2 does not define.

    An unknown key is a typo or a field from a later revision; either way,
    accepting it silently would mean a declaration that reads as though it
    said something the check never applied.
    """
    for key in sorted(str(k) for k in value):
        if key not in allowed:
            raise FluxError(FLX_5030, f"{subject}: unknown key {key!r}")


def _require_int(value: Any, subject: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FluxError(FLX_5030, f"{subject} must be an integer, got {type(value).__name__}")
    if value < minimum:
        raise FluxError(FLX_5030, f"{subject} must be at least {minimum}")
    return value


def _decimal_of(value: Any, subject: str) -> Optional[Decimal]:
    """Read *value* as an exact decimal, or return ``None`` if it is not one.

    §2: "Numbers are strings (exact decimals), never floats." A ``float``
    is refused outright rather than converted, because the conversion is
    exactly the platform-dependent step the grid exists to remove. An
    ``int`` is accepted -- YAML spells a whole number without quotes and
    an integer has one exact decimal reading.
    """
    if isinstance(value, bool):
        raise FluxError(FLX_5030, f"{subject}: a bool is not a number")
    if isinstance(value, float):
        raise FluxError(FLX_5030, f"{subject}: numbers are exact decimal strings, never floats")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            return None
        if not parsed.is_finite():
            raise FluxError(FLX_5030, f"{subject}: a non-finite decimal has no value")
        return parsed
    raise FluxError(FLX_5030, f"{subject}: value class {type(value).__name__!r}")


def _require_decimal(value: Any, subject: str) -> Decimal:
    parsed = _decimal_of(value, subject)
    if parsed is None:
        raise FluxError(FLX_5030, f"{subject} is not an exact decimal")
    return parsed


def _read_bound(value: Any, subject: str) -> Tuple[Fraction, ValueDomain]:
    """Read a range bound as an exact decimal, else as an RFC 3339 instant.

    §2's example declares ``amount`` with a decimal range and
    ``aborted_at`` with a range of RFC 3339 timestamps, and gives the
    block no field to say which is which. The two spellings are disjoint
    -- an RFC 3339 date-time never parses as a decimal -- so the read is
    unambiguous: try the decimal, and fall back to KERNEL-SPEC §7's
    timestamp parser, whose result is integer milliseconds. A string that
    is neither is ``FLX-5030``.
    """
    parsed = _decimal_of(value, subject)
    if parsed is not None:
        return Fraction(parsed), ValueDomain.NUMBER
    return Fraction(parse_rfc3339_ms(str(value))), ValueDomain.TIMESTAMP


#: Which duration designators a ``maxStateAge`` may use. Years and months
#: are excluded: neither has a fixed length in milliseconds, so a state
#: age expressed in them would depend on when it was read, and the gate's
#: comparison is meant to be a property of two instants.
_DURATION_UNITS_MS: Mapping[str, int] = MappingProxyType(
    {
        "W": 7 * 24 * 60 * 60 * 1000,
        "D": 24 * 60 * 60 * 1000,
        "H": 60 * 60 * 1000,
        "M": 60 * 1000,
        "S": 1000,
    }
)


def parse_duration_ms(text: str) -> int:
    """Parse an ISO 8601 duration into integer milliseconds.

    SEAM-FIDELITY-SPEC §2 declares ``budget.maxStateAge`` as ``P30D``.
    Accepts weeks, days, hours, minutes and seconds (with an optional
    fractional second of at most three digits); refuses years and months
    for the reason given at :data:`_DURATION_UNITS_MS`, and refuses an
    empty or zero duration, which would make every committed state
    instantly stale.
    """
    raw = nfc(str(text)).strip()
    if not raw.startswith("P"):
        raise FluxError(FLX_5030, "maxStateAge is not an ISO 8601 duration")
    body = raw[1:]
    if not body:
        raise FluxError(FLX_5030, "maxStateAge names no quantity")
    total = 0
    number = ""
    in_time = False
    seen: List[str] = []
    for character in body:
        if character == "T":
            if in_time or number:
                raise FluxError(FLX_5030, "maxStateAge has a misplaced time designator")
            in_time = True
            continue
        if character.isdigit() or character == ".":
            number += character
            continue
        if character in ("Y", "M") and not in_time:
            raise FluxError(
                FLX_5030,
                "maxStateAge in years or months has no fixed length in milliseconds",
            )
        if character not in _DURATION_UNITS_MS or not number:
            raise FluxError(FLX_5030, f"maxStateAge has no unit {character!r}")
        if character in ("W", "D") and in_time:
            raise FluxError(FLX_5030, f"maxStateAge: {character!r} is not a time-part unit")
        if character in ("H", "S") and not in_time:
            raise FluxError(FLX_5030, f"maxStateAge: {character!r} needs the T designator")
        if character in seen:
            raise FluxError(FLX_5030, f"maxStateAge repeats the unit {character!r}")
        seen.append(character)
        # Scaled as an exact rational, not as a Decimal product: a Decimal
        # multiplication goes through the arithmetic context and rounds at
        # 28 significant digits, and a rounded quantity can land on a whole
        # number the exact one is not -- which would let a declaration be
        # read as a different maximum age from the one it spells.
        scaled = Fraction(_require_decimal(number, "maxStateAge")) * _DURATION_UNITS_MS[character]
        if scaled.denominator != 1:
            raise FluxError(FLX_5030, "maxStateAge is not a whole number of milliseconds")
        total += int(scaled)
        number = ""
    if number:
        raise FluxError(FLX_5030, "maxStateAge ends in a quantity with no unit")
    if total <= 0:
        raise FluxError(FLX_5030, "maxStateAge must be a positive duration")
    return total


# ---------------------------------------------------------------------------
# The parsed block
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Encoding:
    """``encoding: { fractionalBits: 16, rounding: floor }`` (§2)."""

    fractional_bits: int = DEFAULT_FRACTIONAL_BITS
    rounding: str = "floor"

    @property
    def grid(self) -> Grid:
        """The KERNEL-SPEC §12 grid this encoding describes."""
        return Grid(fractional_bits=self.fractional_bits)


@dataclass(frozen=True)
class RecordsRange:
    """``records: { min: 1000, max: 100000 }`` (§2).

    ``min`` is the floor a check refuses below (§5: "count outside
    ``records``", "group below ``records.min``") and ``max`` is both the
    ceiling and the width the boundary pads its per-column commitment to
    (§4), so a state's record count never leaks through the shape of the
    commitment.
    """

    minimum: int
    maximum: int


@dataclass(frozen=True)
class ColumnDeclaration:
    """One entry of the block's ``columns`` mapping (§2)."""

    name: str
    kind: ColumnKind
    null_policy: NullPolicy
    domain: ValueDomain = ValueDomain.NUMBER
    lower: Optional[Fraction] = None
    upper: Optional[Fraction] = None
    max_cardinality: Optional[int] = None
    support_source: Optional[str] = None
    #: The levels the declaration names, for a ``SUPPORT`` column. The
    #: *check* does not read them -- SEAM-FIDELITY-SPEC §5 compares the
    #: synthetic support against the **committed** state's support, not
    #: against a declaration -- but the generator draws from them, and
    #: carrying them here means one parse serves both readers.
    support_values: Tuple[str, ...] = ()

    @property
    def interval(self) -> Interval:
        """The declared range as the interval epsilon propagation reads.

        Only a ``RANGE`` column has one; asking a ``SUPPORT`` column for
        an interval is a caller error, not an empty answer.
        """
        if self.kind is not ColumnKind.RANGE or self.lower is None or self.upper is None:
            raise FluxError(FLX_5034, f"column {self.name!r} declares no range")
        return Interval(self.lower, self.upper)


@dataclass(frozen=True)
class Tolerance:
    """A property's declared tolerance, and the raw spelling of it.

    :attr:`magnitude` is the exact number the comparison consumes --
    ``value`` for an absolute or relative tolerance, ``max`` for a
    distance or a count. :attr:`declared` keeps the mapping as written so
    the fidelity record can quote the declaration rather than a
    re-rendering of it (SEAM-FIDELITY-SPEC §6's ``properties[].tolerance``).
    """

    kind: ToleranceKind
    magnitude: Fraction
    measure: Optional[str] = None
    declared: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class PropertyDeclaration:
    """One entry of the block's ``properties`` list (§2)."""

    index: int
    kind: PropertyKind
    columns: Tuple[str, ...]
    tolerance: Tolerance
    group_by: Optional[str] = None
    bins: Tuple[Fraction, ...] = ()
    level: Optional[Fraction] = None
    min_variance: Optional[Fraction] = None

    @property
    def property_id(self) -> str:
        """A stable, unique key for this property inside its block.

        The declaration's list position leads, so two properties that
        differ only in a tolerance -- which is exactly the change §8 says
        leaves ``G`` unchanged -- still key apart, and a record's property
        rows line up with the declaration order a reader is looking at.
        """
        tail = f"@{self.group_by}" if self.group_by is not None else ""
        return f"{self.index:03d}:{self.kind.value}:{'+'.join(self.columns)}{tail}"


@dataclass(frozen=True)
class Budget:
    """``budget: { maxChecksPerState: 20, maxStateAge: P30D }`` (§2)."""

    max_checks_per_state: int
    max_state_age_ms: Optional[int] = None
    max_state_age_declared: Optional[str] = None


@dataclass(frozen=True)
class FidelityBlock:
    """The whole validated block.

    ``columns`` and ``properties`` are immutable and in a fixed order --
    columns by NFC-sorted name, properties by declaration index -- so
    every derived artefact (the compiled margins, the derived statistic,
    the record's property rows) is built in one order whatever order the
    YAML loader happened to yield.
    """

    expose_id: str
    encoding: Encoding
    records: RecordsRange
    columns: Mapping[str, ColumnDeclaration]
    properties: Tuple[PropertyDeclaration, ...]
    budget: Budget
    failure_action: FailureAction

    @property
    def grid(self) -> Grid:
        """The declared fixed-point grid."""
        return self.encoding.grid

    def column(self, name: str) -> ColumnDeclaration:
        """Return the declaration for *name*, or refuse (``FLX-5034``)."""
        try:
            return self.columns[nfc(name)]
        except KeyError:
            raise FluxError(FLX_5034, f"column {name!r} has no declaration") from None

    @property
    def columns_covered(self) -> Tuple[str, ...]:
        """Every column a property reads, sorted -- §6's ``columnsCovered``."""
        covered: set[str] = set()
        for prop in self.properties:
            covered.update(prop.columns)
            if prop.group_by is not None:
                covered.add(prop.group_by)
        return tuple(sorted(covered))


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------

_BLOCK_KEYS = ("encoding", "records", "columns", "properties", "budget", "failureAction")
_ENCODING_KEYS = ("fractionalBits", "rounding")
_RECORDS_KEYS = ("min", "max")

#: **The one shared ``fidelity.columns.<name>`` key set.** Two modules read
#: a column declaration and they now read the same closed set:
#:
#: * this module, for the check -- ``range``, ``support``,
#:   ``maxCardinality``, ``null``;
#: * :mod:`flux_engine.generator.statistics`, for the draw -- the rest.
#:
#: They used to disagree, and the disagreement was papered over by a
#: narrowing pass in ``fluxsim apply`` that silently dropped every property
#: whose column carried a generator key. One set, imported by both, means
#: an unknown key is refused by both and no property is dropped behind the
#: author's back. A key the check does not consume is not ignored
#: *silently*: it parameterises the draw, it sits inside ``H`` and ``G``,
#: and §8's change table therefore covers it.
COLUMN_KEYS: Tuple[str, ...] = (
    # read by the check (this module)
    "range",
    "support",
    "maxCardinality",
    "null",
    # read by the generator (flux_engine.generator.statistics)
    "bins",
    "binWeights",
    "family",
    "identifierLength",
    "levels",
    "mean",
    "nullFraction",
    "rate",
    "stddev",
    "unique",
)

#: The one spelling of a support column's declared level list.
SUPPORT_VALUES_KEY = "values"

#: ``support: declared`` -- "take the levels from the contract".
SUPPORT_DECLARED = "declared"

_COLUMN_KEYS = COLUMN_KEYS
_SUPPORT_KEYS = (SUPPORT_VALUES_KEY,)
_RANGE_KEYS = ("min", "max")
_BUDGET_KEYS = ("maxChecksPerState", "maxStateAge")
_PROPERTY_KEYS = (
    "property",
    "column",
    "columns",
    "groupBy",
    "tolerance",
    "bins",
    "level",
    "minVariance",
)
_TOLERANCE_KEYS = ("type", "value", "max", "measure")


def _parse_encoding(raw: Any) -> Encoding:
    mapping = _require_mapping(raw, "fidelity.encoding")
    _known_keys(mapping, _ENCODING_KEYS, "fidelity.encoding")
    bits = _require_int(
        mapping.get("fractionalBits", DEFAULT_FRACTIONAL_BITS),
        "fidelity.encoding.fractionalBits",
    )
    rounding = nfc(str(mapping.get("rounding", "floor")))
    if rounding != "floor":
        # KERNEL-SPEC §12 defines one quantiser, rounding toward -inf. A
        # block declaring another rounding would describe a grid this
        # engine cannot evaluate on, and the epsilon bounds are one-sided
        # precisely because the rounding is.
        raise FluxError(
            FLX_5030,
            f"fidelity.encoding.rounding {rounding!r}: the grid rounds toward -inf only",
        )
    encoding = Encoding(fractional_bits=bits, rounding=rounding)
    encoding.grid  # noqa: B018 - constructing the Grid validates fractionalBits (FLX-5004)
    return encoding


def _parse_records(raw: Any) -> RecordsRange:
    mapping = _require_mapping(raw, "fidelity.records")
    _known_keys(mapping, _RECORDS_KEYS, "fidelity.records")
    if "min" not in mapping or "max" not in mapping:
        raise FluxError(FLX_5034, "fidelity.records needs both min and max")
    minimum = _require_int(mapping["min"], "fidelity.records.min", minimum=1)
    maximum = _require_int(mapping["max"], "fidelity.records.max", minimum=1)
    if maximum < minimum:
        raise FluxError(FLX_5030, "fidelity.records.max is below fidelity.records.min")
    return RecordsRange(minimum=minimum, maximum=maximum)


def read_support_levels(
    raw: Any, *, subject: str, declared_type: str = ""
) -> Tuple[Tuple[str, ...], str]:
    """Read a column's ``support`` declaration; return ``(levels, source)``.

    **One spelling per source, shared by the check and the generator.**

    * ``support: { values: ["IE", "GB"] }`` -- the levels, written out.
      ``source`` is ``"values"``.
    * ``support: declared`` -- "the categorical enum the FLUID column type
      declares". ``source`` is ``"declared"``.

    §2's example uses the second spelling, and the rule it needs is the
    one this function fixes: *declared* means the level list on the
    column's own type, ``enum('IE', 'GB')``
    (:func:`flux_engine.seam.contract.declared_enum_values`). FLUID 0.7.5
    carries no other structured value set -- ``validationRules[].constraint``
    is free text with no grammar -- so a column whose type is not a
    parameterised ``enum`` has no support to take, and the declaration is
    **refused** (``FLX-5034``) rather than resolved to an empty set. An
    empty support would make every value a non-member and the support
    check trivially fail, which is a wrong answer dressed as a strict one.

    A bare list (``support: ["IE", "GB"]``) is refused by name: it was the
    generator's old spelling, and two spellings of one declaration are two
    digests of one contract.
    """
    if isinstance(raw, str):
        source = nfc(raw)
        if source != SUPPORT_DECLARED:
            raise FluxError(
                FLX_5030,
                f"{subject} {source!r}: a support is "
                f"{{{SUPPORT_VALUES_KEY}: [...]}} or the word {SUPPORT_DECLARED!r}",
            )
        levels = declared_enum_values(declared_type)
        if not levels:
            raise FluxError(
                FLX_5034,
                f"{subject}: {SUPPORT_DECLARED!r} takes the levels from the FLUID column "
                f"type's own enum, and the column declares {declared_type!r}, which names "
                f"none. Write the levels as {{{SUPPORT_VALUES_KEY}: [...]}}, or declare the "
                "column as enum(...) in the contract",
            )
        return levels, SUPPORT_DECLARED
    if isinstance(raw, Mapping):
        _known_keys(raw, _SUPPORT_KEYS, subject)
        if SUPPORT_VALUES_KEY not in raw:
            raise FluxError(FLX_5034, f"{subject} needs {SUPPORT_VALUES_KEY}")
        values = raw[SUPPORT_VALUES_KEY]
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise FluxError(
                FLX_5030,
                f"{subject}.{SUPPORT_VALUES_KEY} is a list of level names, got "
                f"{type(values).__name__}",
            )
        if not values:
            raise FluxError(FLX_5034, f"{subject}.{SUPPORT_VALUES_KEY} lists no level")
        levels = ()
        seen: List[str] = []
        for index, item in enumerate(values):
            if not isinstance(item, str):
                raise FluxError(
                    FLX_5030,
                    f"{subject}.{SUPPORT_VALUES_KEY}[{index}] is "
                    f"{type(item).__name__}, not a level name",
                )
            level = nfc(item)
            if level in seen:
                raise FluxError(
                    FLX_5030,
                    f"{subject}.{SUPPORT_VALUES_KEY} repeats a level after NFC normalisation",
                )
            seen.append(level)
        return tuple(seen), SUPPORT_VALUES_KEY
    raise FluxError(
        FLX_5030,
        f"{subject} is {type(raw).__name__}; a support is "
        f"{{{SUPPORT_VALUES_KEY}: [...]}} or the word {SUPPORT_DECLARED!r}",
    )


def _parse_column(
    name: str,
    raw: Any,
    expose_columns: Tuple[str, ...],
    declared_type: str = "",
) -> Optional[ColumnDeclaration]:
    """Parse one ``fidelity.columns`` entry, or return ``None``.

    ``None`` means "this entry declares nothing the *check* consumes" --
    the shared key set (:data:`COLUMN_KEYS`) also carries the generator's
    draw parameters, and a column declared only with those has no range
    and no support to compare against. It is not an error: the block is a
    contract between an owner, a generator and a verifier, and a column
    only the generator reads is a legal thing to declare. A *property*
    that names such a column still fails, in :func:`_parse_property`,
    with ``FLX-5034`` -- which is where the failure belongs, because that
    is where a comparison was actually asked for.
    """
    if name not in expose_columns:
        raise FluxError(FLX_5031, f"column {name!r} is not a member of the expose")
    mapping = _require_mapping(raw, f"fidelity.columns.{name}")
    _known_keys(mapping, _COLUMN_KEYS, f"fidelity.columns.{name}")
    policy_text = nfc(str(mapping.get("null", NullPolicy.EXCLUDE.value)))
    try:
        policy = NullPolicy(policy_text)
    except ValueError:
        raise FluxError(
            FLX_5030, f"fidelity.columns.{name}.null {policy_text!r} is not a null policy"
        ) from None

    has_range = "range" in mapping
    has_support = "support" in mapping
    if has_range and has_support:
        raise FluxError(
            FLX_5030,
            f"fidelity.columns.{name} declares both range and support; a column is "
            "compared as an interval or as a member list, never as both",
        )
    if not has_range and not has_support:
        if "maxCardinality" in mapping:
            raise FluxError(
                FLX_5030,
                f"fidelity.columns.{name}: maxCardinality belongs to a support column",
            )
        return None

    if has_range:
        bounds = _require_mapping(mapping["range"], f"fidelity.columns.{name}.range")
        _known_keys(bounds, _RANGE_KEYS, f"fidelity.columns.{name}.range")
        if "min" not in bounds or "max" not in bounds:
            raise FluxError(FLX_5034, f"fidelity.columns.{name}.range needs both min and max")
        lower, lower_domain = _read_bound(bounds["min"], f"fidelity.columns.{name}.range.min")
        upper, upper_domain = _read_bound(bounds["max"], f"fidelity.columns.{name}.range.max")
        if lower_domain is not upper_domain:
            raise FluxError(
                FLX_5030,
                f"fidelity.columns.{name}.range mixes a decimal bound with a timestamp bound",
            )
        if upper <= lower:
            raise FluxError(FLX_5030, f"fidelity.columns.{name}.range is empty or inverted")
        if "maxCardinality" in mapping:
            raise FluxError(
                FLX_5030, f"fidelity.columns.{name}: maxCardinality belongs to a support column"
            )
        return ColumnDeclaration(
            name=name,
            kind=ColumnKind.RANGE,
            null_policy=policy,
            domain=lower_domain,
            lower=lower,
            upper=upper,
        )

    levels, source = read_support_levels(
        mapping["support"],
        subject=f"fidelity.columns.{name}.support",
        declared_type=declared_type,
    )
    if "maxCardinality" not in mapping:
        raise FluxError(FLX_5034, f"fidelity.columns.{name} needs a maxCardinality")
    cardinality = _require_int(
        mapping["maxCardinality"], f"fidelity.columns.{name}.maxCardinality", minimum=1
    )
    if len(levels) > cardinality:
        raise FluxError(
            FLX_5030,
            f"fidelity.columns.{name}: the declared support has {len(levels)} levels and "
            f"maxCardinality is {cardinality}; a committed state at the declared support "
            "would be refused before it was formed",
        )
    return ColumnDeclaration(
        name=name,
        kind=ColumnKind.SUPPORT,
        null_policy=policy,
        max_cardinality=cardinality,
        support_source=source,
        support_values=levels,
    )


def _parse_tolerance(raw: Any, kind: PropertyKind, subject: str) -> Tolerance:
    mapping = _require_mapping(raw, subject)
    _known_keys(mapping, _TOLERANCE_KEYS, subject)
    if "type" not in mapping:
        raise FluxError(FLX_5034, f"{subject} needs a type")
    text = nfc(str(mapping["type"]))
    try:
        tolerance_kind = ToleranceKind(text)
    except ValueError:
        raise FluxError(FLX_5030, f"{subject}.type {text!r} is not a tolerance type") from None
    allowed = _TOLERANCE_KINDS[kind]
    if tolerance_kind not in allowed:
        raise FluxError(
            FLX_5030,
            f"{subject}.type {text!r} is not a tolerance a {kind.value} comparison consumes "
            f"(one of {', '.join(k.value for k in allowed)})",
        )

    measure: Optional[str] = None
    if tolerance_kind is ToleranceKind.DISTANCE:
        if "measure" not in mapping:
            raise FluxError(FLX_5034, f"{subject} needs a measure")
        measure = nfc(str(mapping["measure"]))
        if measure not in DISTANCE_MEASURES:
            raise FluxError(FLX_5030, f"{subject}.measure {measure!r} has no comparison")
    elif "measure" in mapping:
        raise FluxError(FLX_5030, f"{subject}: only a distance tolerance carries a measure")

    if tolerance_kind in (ToleranceKind.ABSOLUTE, ToleranceKind.RELATIVE):
        if "value" not in mapping:
            raise FluxError(FLX_5034, f"{subject} needs a value")
        if "max" in mapping:
            raise FluxError(FLX_5030, f"{subject}: a {tolerance_kind.value} tolerance has no max")
        magnitude = Fraction(_require_decimal(mapping["value"], f"{subject}.value"))
        if magnitude <= 0:
            raise FluxError(FLX_5030, f"{subject}.value must be positive")
    else:
        if "max" not in mapping:
            raise FluxError(FLX_5034, f"{subject} needs a max")
        if "value" in mapping:
            raise FluxError(FLX_5030, f"{subject}: a {tolerance_kind.value} tolerance has no value")
        if tolerance_kind is ToleranceKind.COUNT:
            magnitude = Fraction(_require_int(mapping["max"], f"{subject}.max"))
        else:
            magnitude = Fraction(_require_decimal(mapping["max"], f"{subject}.max"))
            if magnitude <= 0:
                raise FluxError(FLX_5030, f"{subject}.max must be positive")

    declared = {str(key): str(mapping[key]) for key in sorted(str(k) for k in mapping)}
    return Tolerance(
        kind=tolerance_kind,
        magnitude=magnitude,
        measure=measure,
        declared=MappingProxyType(declared),
    )


def _property_columns(raw: Mapping[str, Any], kind: PropertyKind, subject: str) -> Tuple[str, ...]:
    """Read a property's columns from ``column`` or ``columns``.

    §2 spells a single-column property with ``column`` and a correlation
    with two columns; ``columns`` is the plural spelling. Declaring both
    is refused rather than resolved, and a correlation that does not end
    up with exactly two distinct columns is ``FLX-5033``.
    """
    if "column" in raw and "columns" in raw:
        raise FluxError(FLX_5030, f"{subject} declares both column and columns")
    if "columns" in raw:
        listed = raw["columns"]
        if not isinstance(listed, Sequence) or isinstance(listed, (str, bytes)):
            raise FluxError(FLX_5030, f"{subject}.columns must be a list")
        names = tuple(nfc(str(entry)) for entry in listed)
    elif "column" in raw:
        names = (nfc(str(raw["column"])),)
    else:
        raise FluxError(FLX_5034, f"{subject} names no column")

    if kind is PropertyKind.CORRELATION:
        if len(names) != 2:
            raise FluxError(
                FLX_5033,
                f"{subject}: a correlation declares two columns, got {len(names)}",
            )
        if names[0] == names[1]:
            raise FluxError(FLX_5033, f"{subject}: a correlation needs two distinct columns")
    elif len(names) != 1:
        raise FluxError(FLX_5030, f"{subject}: a {kind.value} property names one column")
    return names


def _parse_bins(
    raw: Any, column: ColumnDeclaration, grid: Grid, subject: str
) -> Tuple[Fraction, ...]:
    """Read the histogram edges and check they partition the declared range.

    §2: "bins are strictly increasing and partition the declared range".
    Partitioning is read literally: the first edge is the column's
    declared minimum and the last is its declared maximum, so every record
    inside the declared range falls in exactly one bin and neither side's
    bin counts can silently omit a record. The edges must also land on the
    grid -- ``epsilon_marginal_histogram`` is zero only because they do
    (KERNEL-SPEC §12) -- which is checked by the compiler when it computes
    that epsilon.
    """
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise FluxError(FLX_5030, f"{subject}.bins must be a list")
    if len(raw) < 2:
        raise FluxError(FLX_5032, f"{subject}.bins needs at least two edges")
    edges: List[Fraction] = []
    for index, value in enumerate(raw):
        edge, domain = _read_bound(value, f"{subject}.bins[{index}]")
        if domain is not column.domain:
            raise FluxError(
                FLX_5032,
                f"{subject}.bins[{index}] is not in the same domain as the column's range",
            )
        if edges and edge <= edges[-1]:
            raise FluxError(FLX_5032, f"{subject}.bins[{index}] does not increase")
        edges.append(edge)
    interval = column.interval
    if edges[0] != interval.lo or edges[-1] != interval.hi:
        raise FluxError(
            FLX_5032,
            f"{subject}.bins run from the first edge to the last, which are not the "
            f"column's declared minimum and maximum",
        )
    if not grid.is_on_grid(edges[0]) or not grid.is_on_grid(edges[-1]):  # pragma: no cover
        raise FluxError(FLX_5032, f"{subject}.bins do not land on the declared grid")
    return tuple(edges)


def _parse_property(
    index: int,
    raw: Any,
    columns: Mapping[str, ColumnDeclaration],
    grid: Grid,
) -> PropertyDeclaration:
    subject = f"fidelity.properties[{index}]"
    mapping = _require_mapping(raw, subject)
    _known_keys(mapping, _PROPERTY_KEYS, subject)
    if "property" not in mapping:
        raise FluxError(FLX_5034, f"{subject} names no property")
    text = nfc(str(mapping["property"]))
    try:
        kind = PropertyKind(text)
    except ValueError:
        raise FluxError(FLX_5030, f"{subject}.property {text!r} has no comparison") from None

    names = _property_columns(mapping, kind, subject)
    wanted_kind = _COLUMN_KINDS[kind]
    for name in names:
        if name not in columns:
            # §3 computes epsilon "from encoding, columns[].range", so a
            # property over a column the block does not declare has no
            # range, no epsilon and no margin -- it is missing a
            # parameter, not merely referring elsewhere.
            raise FluxError(
                FLX_5034,
                f"{subject}: column {name!r} has no declaration in fidelity.columns",
            )
        if columns[name].kind is not wanted_kind:
            raise FluxError(
                FLX_5030,
                f"{subject}: a {kind.value} comparison needs a {wanted_kind.value} column, "
                f"and {name!r} declares a {columns[name].kind.value}",
            )

    group_by: Optional[str] = None
    if "groupBy" in mapping:
        group_by = nfc(str(mapping["groupBy"]))
        if group_by not in columns:
            raise FluxError(
                FLX_5034,
                f"{subject}: groupBy column {group_by!r} has no declaration in fidelity.columns",
            )
        if columns[group_by].kind is not ColumnKind.SUPPORT:
            # A group per distinct value of a continuous column has no
            # bound on the number of groups, so records.min per group
            # (§5's fail-closed rule) could not be checked against
            # anything the declaration fixes.
            raise FluxError(
                FLX_5030,
                f"{subject}: groupBy needs a support column, and {group_by!r} declares a range",
            )
        if group_by in names:
            raise FluxError(FLX_5030, f"{subject}: groupBy repeats the property's own column")

    if "tolerance" not in mapping:
        raise FluxError(FLX_5034, f"{subject} needs a tolerance")
    tolerance = _parse_tolerance(mapping["tolerance"], kind, f"{subject}.tolerance")

    bins: Tuple[Fraction, ...] = ()
    if kind is PropertyKind.MARGINAL_HISTOGRAM:
        if "bins" not in mapping:
            raise FluxError(FLX_5034, f"{subject} needs bins")
        bins = _parse_bins(mapping["bins"], columns[names[0]], grid, subject)
    elif "bins" in mapping:
        raise FluxError(FLX_5030, f"{subject}: only a marginalHistogram carries bins")

    level: Optional[Fraction] = None
    if kind is PropertyKind.QUANTILE:
        if "level" not in mapping:
            raise FluxError(FLX_5034, f"{subject} needs a level")
        level = Fraction(_require_decimal(mapping["level"], f"{subject}.level"))
        if not 0 < level < 1:
            raise FluxError(FLX_5030, f"{subject}.level must lie strictly inside (0, 1)")
    elif "level" in mapping:
        raise FluxError(FLX_5030, f"{subject}: only a quantile carries a level")

    min_variance: Optional[Fraction] = None
    if kind is PropertyKind.CORRELATION:
        if "minVariance" not in mapping:
            raise FluxError(FLX_5033, f"{subject}: a correlation needs a minVariance")
        min_variance = Fraction(_require_decimal(mapping["minVariance"], f"{subject}.minVariance"))
        if min_variance <= 0:
            raise FluxError(FLX_5033, f"{subject}.minVariance must be positive")
    elif "minVariance" in mapping:
        raise FluxError(FLX_5030, f"{subject}: only a correlation carries a minVariance")

    return PropertyDeclaration(
        index=index,
        kind=kind,
        columns=names,
        tolerance=tolerance,
        group_by=group_by,
        bins=bins,
        level=level,
        min_variance=min_variance,
    )


def _parse_budget(raw: Any) -> Budget:
    mapping = _require_mapping(raw, "fidelity.budget")
    _known_keys(mapping, _BUDGET_KEYS, "fidelity.budget")
    if "maxChecksPerState" not in mapping:
        raise FluxError(FLX_5034, "fidelity.budget needs a maxChecksPerState")
    checks = _require_int(
        mapping["maxChecksPerState"], "fidelity.budget.maxChecksPerState", minimum=1
    )
    age_declared: Optional[str] = None
    age_ms: Optional[int] = None
    if "maxStateAge" in mapping:
        age_declared = nfc(str(mapping["maxStateAge"]))
        age_ms = parse_duration_ms(age_declared)
    return Budget(
        max_checks_per_state=checks,
        max_state_age_ms=age_ms,
        max_state_age_declared=age_declared,
    )


def parse_fidelity_block(raw: Any, *, expose: ExposeSchema) -> FidelityBlock:
    """Parse and validate one ``emits[].fidelity`` block against *expose*.

    Implements every offline rule of SEAM-FIDELITY-SPEC §2, in the order
    that makes each refusal name the smallest thing that is wrong: shape
    first, then column membership, then per-property parameters. The
    result is fully immutable and ordered, and no statistic, digest or
    margin has been computed -- that is the compiler's work.
    """
    mapping = _require_mapping(raw, "fidelity")
    _known_keys(mapping, _BLOCK_KEYS, "fidelity")
    for required in ("records", "columns", "properties", "budget"):
        if required not in mapping:
            raise FluxError(FLX_5034, f"fidelity needs a {required} section")

    encoding = _parse_encoding(mapping.get("encoding", {}))
    records = _parse_records(mapping["records"])

    raw_columns = _require_mapping(mapping["columns"], "fidelity.columns")
    if not raw_columns:
        raise FluxError(FLX_5034, "fidelity.columns declares no column")
    expose_columns = tuple(nfc(name) for name in expose.column_names)
    declared_types = {
        nfc(str(name)): str(spelling)
        for name, spelling in getattr(expose, "column_types", {}).items()
    }
    parsed_columns: Dict[str, ColumnDeclaration] = {}
    for key in sorted(nfc(str(name)) for name in raw_columns):
        original = next(name for name in raw_columns if nfc(str(name)) == key)
        declaration = _parse_column(
            key, raw_columns[original], expose_columns, declared_types.get(key, "")
        )
        # A column declared only with the generator's draw parameters is
        # legal and has nothing for the check to compare; it is simply
        # absent from the compiled block. `_parse_property` refuses a
        # property that names one, so the omission can never be silent.
        if declaration is not None:
            parsed_columns[key] = declaration

    raw_properties = mapping["properties"]
    if not isinstance(raw_properties, Sequence) or isinstance(raw_properties, (str, bytes)):
        raise FluxError(FLX_5030, "fidelity.properties must be a list")
    if not raw_properties:
        raise FluxError(FLX_5034, "fidelity.properties declares no property")
    properties = tuple(
        _parse_property(index, entry, parsed_columns, encoding.grid)
        for index, entry in enumerate(raw_properties)
    )

    budget = _parse_budget(mapping["budget"])

    action_text = nfc(str(mapping.get("failureAction", FailureAction.WITHHOLD_BUNDLE.value)))
    try:
        action = FailureAction(action_text)
    except ValueError:
        raise FluxError(
            FLX_5030, f"fidelity.failureAction {action_text!r} is not a failure action"
        ) from None

    return FidelityBlock(
        expose_id=nfc(expose.expose_id),
        encoding=encoding,
        records=records,
        columns=MappingProxyType(parsed_columns),
        properties=properties,
        budget=budget,
        failure_action=action,
    )
