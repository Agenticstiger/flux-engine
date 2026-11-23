# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Parse and validate the fidelity block -- SEAM-FIDELITY-SPEC §2.

This is flux-stats' module (ADR 0009): the grammar lives here so a profiler
that reads a Simulation's fidelity block implements no parser of its own, and
flux-sim-engine re-exports every name from ``flux_engine.concordance.predicate``
as an import of the same object.

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
    ``levelHistogram`` names a ``support`` column and carries no ``bins``;
    a ``correlation`` property declares two columns and a ``minVariance``;
    every property's parameters are present (a contract missing any of
    them is refused before any statistic is formed). Numbers are strings
    (exact decimals), never floats.

A ``quantile`` property may also carry ``bins``, under the rules a
histogram's follow (ADR 0012): it is then formed and compared as the bin that
holds its order statistic, by ``quantile/2``, and
:func:`definition_for_property` is where a property's definition is picked.

Nothing here computes a digest, a margin or a statistic: those are
the engine's fidelity compiler and its boundary's statistic module. This
module answers one
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
shared with the engine's generator statistics: the check reads four
of its keys and the generator reads the rest, and neither refuses a key
the other owns.

Every mapping is iterated by sorted key and every collection this module
returns is immutable, so two runs over one block build one object.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from fractions import Fraction
from functools import partial
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

from flux_stats.definitions import bin_index
from flux_stats.errors import ErrorCode, FluxError, register
from flux_stats.fixedpoint import DEFAULT_FRACTIONAL_BITS, FLX_5005, Grid, Interval
from flux_stats.rank_correlation import KENDALL_TAU, SPEARMAN_RHO
from flux_stats.reading import (
    FLX_5031,
    NullPolicy,
    Row,
    ValueDomain,
    category,
    cell,
    digits_written_out,
    encode_value,
    level_cells,
    nfc,
    parse_date_days,
    parse_rfc3339_ms,
    read_column,
    read_pairs,
)
from flux_stats.registry import (
    GroupStatistic,
    Parameters,
    StatisticDefinition,
    definition_for_kind,
)
from flux_stats.spelling import MOST_DIGITS, render_exact

__all__ = [
    "COLUMN_KEYS",
    "FLX_3001",
    "FLX_5030",
    "FLX_5031",
    "FLX_5032",
    "FLX_5033",
    "FLX_5034",
    "SUPPORT_DECLARED",
    "SUPPORT_VALUES_KEY",
    "BinsGrouping",
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
    "RANK_MEASURES",
    "RecordsRange",
    "Tolerance",
    "ToleranceKind",
    "ValueDomain",
    "declared_enum_values",
    "definition_for_property",
    "expose_from_contract",
    "is_date_type",
    "bin_group_keys",
    "is_link_key",
    "measure_group",
    "parse_duration_ms",
    "parse_fidelity_block",
    "read_support_levels",
    "require_number_length",
    "split_type_params",
]

# ---------------------------------------------------------------------------
# The codes this module raises
# ---------------------------------------------------------------------------

FLX_3001: ErrorCode = register(
    "FLX-3001",
    "declared column type is outside the closed logical type set",
    "KERNEL-SPEC §9 fixes eight logical types -- STRING, INT, DECIMAL(p,s), "
    "BOOL, DATE, TIMESTAMP, JSON, BYTES -- and every FLUID type name is "
    "normalised case-insensitively onto one of them. A name that maps onto "
    "none of them is refused where it is declared, not where a row happens "
    "to use it. Binary floating-point types (float, double, real) are "
    "refused deliberately: no float may appear in a hashed value, so a "
    "column of them has no rendering. A decimal must carry its precision "
    "and scale, as DECIMAL(18,4) does.",
)

FLX_5030: ErrorCode = register(
    "FLX-5030",
    "the fidelity block is malformed",
    "The block's shape does not match SEAM-FIDELITY-SPEC §2: a required "
    "section is missing, a section is not a mapping, a value has the wrong "
    "class, a number needs more than 1000 digits written out, or a closed "
    "set (a property name, a tolerance type, a null policy, a failure "
    "action) was given a member it does not have. A tolerance that carries "
    "sampling must be a distance tolerance and give a boolean; a column's "
    "range must have a maximum above its minimum and bounds of one kind, "
    "must not sit beside a support, and must not carry maxCardinality, "
    "which belongs to a support column. The block is refused before any "
    "statistic is formed.",
)

FLX_5032: ErrorCode = register(
    "FLX-5032",
    "histogram bins do not partition the declared range",
    "SEAM-FIDELITY-SPEC §2: bins are strictly increasing and partition the "
    "declared range. The first edge is the column's declared minimum and "
    "the last its declared maximum; otherwise a record could fall outside "
    "every bin and the two sides' bin counts would not account for the "
    "same records.",
)

FLX_5033: ErrorCode = register(
    "FLX-5033",
    "a correlation property is incompletely declared",
    "SEAM-FIDELITY-SPEC §2: a correlation property declares two columns "
    "and a minVariance. Without the second column there is nothing to "
    "correlate; without the variance floor the encoding error on a "
    "correlation has no bound (KERNEL-SPEC §12), so no margin can be "
    "compiled.",
)

FLX_5034: ErrorCode = register(
    "FLX-5034",
    "a parameter the declared property needs is absent",
    "SEAM-FIDELITY-SPEC §2: every property's parameters are present, and a "
    "contract missing any of them is refused before any statistic is "
    "formed. A histogram needs bins, a quantile needs a level, every "
    "property needs a tolerance of the type its comparison uses, and every "
    "column a property names needs its own declaration in columns. The list "
    "itself may be empty: a block that lists no property declares a table with "
    "nothing to score, which `verify` reports as not scored and never as "
    "verified. A missing `properties` section, or one that is not a list, is "
    "still refused.",
)


# ---------------------------------------------------------------------------
# A declared type's levels
#
# The type reader mirrors forge-cli's fluid_build/util/column_type.py (the FLUID
# reader; Apache-2.0), which reads a quoted string by the SQL string-literal rule
# (PostgreSQL's lexical structure; sqlglot's tokenizer scans a doubled delimiter
# the same way). flux-stats does not depend on forge-cli, so the scanner is
# ported here.
# ---------------------------------------------------------------------------

#: Quote characters that open a quoted string inside a column type's parameter
#: list: a level, or any other parameter.
_QUOTES = ("'", '"')

#: A level's three spellings, from the FLUID 0.7.7 schema's LEVEL pattern: a
#: single-quoted string, a double-quoted string, or a word list. A quoted string
#: holds any character but a control character (``\x00-\x1f``, ``\x7f-\x9f``),
#: its own quote doubled; the pattern captures the inside. A word list is words
#: of ASCII letters, digits, ``_`` and ``-`` separated by whitespace, kept as written.
#: Each is used with ``fullmatch`` on one stripped part of the list.
_SINGLE_QUOTED_LEVEL = re.compile(r"'((?:[^'\x00-\x1f\x7f-\x9f]|'')*)'")
_DOUBLE_QUOTED_LEVEL = re.compile(r'"((?:[^"\x00-\x1f\x7f-\x9f]|"")*)"')
_WORD_LIST_LEVEL = re.compile(r"[0-9A-Za-z_-]+(?:\s+[0-9A-Za-z_-]+)*")

_QUOTED_LEVELS = {"'": _SINGLE_QUOTED_LEVEL, '"': _DOUBLE_QUOTED_LEVEL}


def _scan_quoted(text: str, start: int) -> int:
    """The index just past the quoted string that opens at ``text[start]``.

    A doubled quote inside the string is one literal quote, the SQL
    string-literal rule (PostgreSQL's lexical structure; sqlglot's tokenizer
    scans a doubled delimiter the same way). Returns ``-1`` when the string
    never closes.
    """
    quote = text[start]
    i = start + 1
    while i < len(text):
        if text[i] == quote:
            if i + 1 < len(text) and text[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return -1


def split_type_params(declared: str) -> Tuple[str, Optional[str]]:
    """Split ``"decimal(18,4)"`` into ``("decimal", "18,4")``.

    Reads a column type the way forge-cli's ``fluid_build/util/column_type.py``
    does, so both tools cut the same type at the same place.

    ``"enum('a)b', 'c')"`` gives ``("enum", "'a)b', 'c'")``: the ``)`` inside the
    quoted level does not end the list. A quote that never closes is an ordinary
    character, so ``"varchar(a'b)"`` gives ``("varchar", "a'b")``.

    A type with no parameter list gives ``(type, None)``. So does a type whose
    parentheses do not close it: an unclosed list, a nested ``(`` outside
    quotes, or text after the closing ``)``.

    Both parts are stripped of surrounding whitespace; the inside of the list
    is returned as written.
    """
    text = declared.strip()
    open_at = text.find("(")
    if open_at < 0:
        return text, None
    i = open_at + 1
    while i < len(text):
        char = text[i]
        if char in _QUOTES:
            end = _scan_quoted(text, i)
            i = i + 1 if end < 0 else end
            continue
        if char == "(":
            return text, None
        if char == ")":
            if text[i + 1 :].strip():
                return text, None
            return text[:open_at].strip(), text[open_at + 1 : i].strip()
        i += 1
    return text, None


def _split_param_list(params: str) -> List[str]:
    """Split a parameter list at the commas outside quotes, each part stripped.

    ``"'a, b', 'c'"`` gives ``["'a, b'", "'c'"]``. Empty parts are kept, so the
    caller decides what one means. A quote that never closes is an ordinary
    character.
    """
    parts: List[str] = []
    start = 0
    i = 0
    while i < len(params):
        char = params[i]
        if char in _QUOTES:
            end = _scan_quoted(params, i)
            i = i + 1 if end < 0 else end
            continue
        if char == ",":
            parts.append(params[start:i].strip())
            start = i + 1
        i += 1
    parts.append(params[start:].strip())
    return parts


def _read_level(declared: str, part: str) -> str:
    """One level of an ``enum(...)`` list, read by FLUID 0.7.7's level grammar.

    *part* is one stripped part of the list. A quoted level (``'...'`` or
    ``"..."``) may hold any character but a control character, a comma, a
    parenthesis, a dot and ``>`` among them; its own quote is doubled inside it
    and reads as one quote, so ``'O''Brien'`` is ``O'Brien``. An unquoted level
    is a word list: words of ASCII letters, digits, ``_`` and ``-`` separated by
    whitespace, kept as written. Anything else is refused (``FLX-3001``).
    """
    if not part:
        raise FluxError(FLX_3001, f"declared type {declared!r} carries an empty enum level")
    quote = part[0]
    quoted = _QUOTED_LEVELS.get(quote)
    match = (quoted or _WORD_LIST_LEVEL).fullmatch(part)
    if match is None:
        raise FluxError(
            FLX_3001,
            f"declared type {declared!r} carries {part!r}, which is not an enum level: "
            "a level is a word list (ASCII letters, digits, '_' and '-', separated by spaces) "
            "or a quoted string with no control character, its own quote doubled inside it",
        )
    if quoted is None:
        return part
    return match.group(1).replace(quote * 2, quote)


def declared_enum_values(declared: Any) -> Tuple[str, ...]:
    """The categorical levels a FLUID column's declared *type* spells out.

    FLUID 0.7.5's column object carries no structured value set: the only
    place a contract can name the levels of a categorical column is the
    type's own parameter list, ``enum('IE', 'GB')`` -- which the FLUID
    schema's type pattern admits alongside ``decimal(18,4)`` and
    ``varchar(255)``. This function reads exactly that, and nothing else:
    ``validationRules[].constraint`` is free text with no declared
    grammar, and parsing prose into a value set would be an inference
    about a contract rather than a reading of one.

    The levels are separated by commas and read by FLUID 0.7.7's grammar: a
    quoted string, in which a doubled quote reads as one quote
    (``'O''Brien'`` is ``O'Brien``), or a word list. They are read from the
    declared text as the author wrote it, never in a lowercased form, because a
    level name is a value and ``'IE'`` is not ``'ie'``.

    Returns ``()`` for a type that is not a parameterised ``enum``. That
    includes a type whose parentheses do not close it (an unclosed list, a
    nested ``(`` outside quotes, or text after the ``)``): the engine's seam
    refuses such a type as no type name. It also returns ``()`` for an empty
    list, ``enum()`` -- "this column declares no levels", which is the state
    :func:`read_support_levels` refuses ``support: declared`` over.

    A list the grammar refuses, such as ``enum('a' 'b')``, raises ``FLX-3001``
    and never returns ``()``: ``()`` would read a list the author did write as
    "declares no levels".

    Levels keep the contract's own order (it is the declared order, and
    the fidelity block's digest quotes what the author wrote); a repeated
    level, counted after unescaping and NFC normalisation, is refused
    (``FLX-3001``) because two spellings of one level are one level and a
    contract that carries both means something it cannot say.
    """
    if not isinstance(declared, str):
        return ()
    name, params = split_type_params(declared)
    if params is None or name.lower() != "enum" or not params:
        return ()
    levels: List[str] = []
    for part in _split_param_list(params):
        level = nfc(_read_level(declared, part))
        if not level:
            raise FluxError(FLX_3001, f"declared type {declared!r} carries an empty enum level")
        if level in levels:
            raise FluxError(
                FLX_3001,
                f"declared type {declared!r} repeats a level after NFC normalisation",
            )
        levels.append(level)
    return tuple(levels)


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
        (:func:`declared_enum_values`). Both
        :class:`DeclaredExpose` and
        the engine's compiled expose carry it.
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
    #: The names of the relationships the expose declares as the child
    #: (``semantics.relationships``), which a ``groupBy`` may reach a linked row's column through.
    relationship_names: Tuple[str, ...] = ()


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
        semantics = entry.get("semantics")
        listed = semantics.get("relationships") if isinstance(semantics, Mapping) else None
        links = (
            tuple(
                nfc(str(link["name"]))
                for link in listed
                if isinstance(link, Mapping) and "name" in link
            )
            if isinstance(listed, Sequence) and not isinstance(listed, (str, bytes))
            else ()
        )
        return DeclaredExpose(
            expose_id=wanted,
            column_names=tuple(names),
            column_types=MappingProxyType(dict(sorted(types.items()))),
            relationship_names=links,
        )
    raise FluxError(FLX_5030, f"contract declares no expose {expose_id!r}")


# ---------------------------------------------------------------------------
# Closed sets
# ---------------------------------------------------------------------------


# NullPolicy and ValueDomain, the closed sets the reader reads, are
# flux_stats.reading's, so a profiler reads a null and a value the way the
# check does (ADR 0009).


class ColumnKind(str, Enum):
    """The two column declarations §2's example shows.

    ``RANGE`` declares a closed numeric (or timestamp) interval and is
    what every encoded statistic needs. ``SUPPORT`` declares a categorical
    column whose committed member list is the thing compared, and carries
    a ``maxCardinality`` instead of an interval.
    """

    RANGE = "range"
    SUPPORT = "support"


class PropertyKind(str, Enum):
    """The six properties SEAM-FIDELITY-SPEC §5 gives a comparison for.

    ``LEVEL_HISTOGRAM`` is the categorical twin of ``MARGINAL_HISTOGRAM``:
    its cells are a support column's declared levels where a histogram's
    are its declared bins, and grouped by another support column it is a
    two-way count table of two categorical columns.
    """

    MEAN = "mean"
    MARGINAL_HISTOGRAM = "marginalHistogram"
    SUPPORT = "support"
    CORRELATION = "correlation"
    QUANTILE = "quantile"
    LEVEL_HISTOGRAM = "levelHistogram"
    RANK_CORRELATION = "rankCorrelation"


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
#: (relative); a histogram, over bins or over levels, compares a distance;
#: a support check compares a count of non-members; a correlation and a
#: quantile compare an absolute margin -- a correlation because it is
#: already dimensionless, a quantile because it is compared in the
#: column's units.
_TOLERANCE_KINDS: Mapping[PropertyKind, Tuple[ToleranceKind, ...]] = MappingProxyType(
    {
        PropertyKind.MEAN: (ToleranceKind.ABSOLUTE, ToleranceKind.RELATIVE),
        PropertyKind.MARGINAL_HISTOGRAM: (ToleranceKind.DISTANCE,),
        PropertyKind.SUPPORT: (ToleranceKind.COUNT,),
        PropertyKind.CORRELATION: (ToleranceKind.ABSOLUTE,),
        PropertyKind.QUANTILE: (ToleranceKind.ABSOLUTE,),
        PropertyKind.LEVEL_HISTOGRAM: (ToleranceKind.DISTANCE,),
        PropertyKind.RANK_CORRELATION: (ToleranceKind.ABSOLUTE,),
    }
)

#: The column kind each property's comparison needs. A support check
#: compares member lists and a level histogram counts declared levels, so
#: both need a ``SUPPORT`` column; everything else encodes values and
#: needs a ``RANGE``.
_COLUMN_KINDS: Mapping[PropertyKind, ColumnKind] = MappingProxyType(
    {
        PropertyKind.MEAN: ColumnKind.RANGE,
        PropertyKind.MARGINAL_HISTOGRAM: ColumnKind.RANGE,
        PropertyKind.SUPPORT: ColumnKind.SUPPORT,
        PropertyKind.CORRELATION: ColumnKind.RANGE,
        PropertyKind.QUANTILE: ColumnKind.RANGE,
        PropertyKind.LEVEL_HISTOGRAM: ColumnKind.SUPPORT,
        PropertyKind.RANK_CORRELATION: ColumnKind.RANGE,
    }
)

#: The two measures of a ``rankCorrelation`` property (ADR 0035 decision 7), and the default
#: tolerance a property that states none is held to.
RANK_MEASURES = ("kendall", "spearman")
DEFAULT_RANK_TOLERANCE = {"type": "absolute", "max": "0.05"}
RANK_DEFINITIONS_BY_MEASURE: Mapping[str, StatisticDefinition] = MappingProxyType(
    {"kendall": KENDALL_TAU, "spearman": SPEARMAN_RHO}
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


#: The most digits a number in a fidelity block may have, written out
#: without an exponent and not counting a leading ``0.``
#: (:func:`flux_stats.reading.digits_written_out` counts them, and a record's
#: number is held to the same bound when it is read). Its exact value
#: becomes a ``Fraction``, which spells ``10**exponent`` out in full, so the
#: twelve characters ``"1E+99999999"`` took minutes to read before any rule
#: could refuse them. 1000 is jackson-core's default longest number
#: (``StreamReadConstraints``), its bound for the same reason. It also keeps
#: what an accepted number becomes, such as a ``maxStateAge`` in
#: milliseconds or a tolerance's denominator, inside CPython's 4300-digit
#: limit on printing an ``int`` (``sys.int_info.default_max_str_digits``,
#: CVE-2020-10735), which a refusal message would otherwise trip over. A
#: block whose numbers all have at most 1000 digits reads exactly as it did
#: without the bound.
_MOST_NUMBER_DIGITS = MOST_DIGITS


def _decimal_of(value: Any, subject: str) -> Optional[Decimal]:
    """Read *value* as an exact decimal, or return ``None`` if it is not one.

    §2: "Numbers are strings (exact decimals), never floats." A ``float``
    is refused outright rather than converted, because the conversion is
    exactly the platform-dependent step the grid exists to remove. An
    ``int`` is accepted -- YAML spells a whole number without quotes and
    an integer has one exact decimal reading. A decimal that needs more
    than :data:`_MOST_NUMBER_DIGITS` digits written out is refused by its
    length, before its value is formed.
    """
    if isinstance(value, bool):
        raise FluxError(FLX_5030, f"{subject}: a bool is not a number")
    if isinstance(value, float):
        raise FluxError(FLX_5030, f"{subject}: numbers are exact decimal strings, never floats")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            return None
    else:
        raise FluxError(FLX_5030, f"{subject}: value class {type(value).__name__!r}")
    if not parsed.is_finite():
        raise FluxError(FLX_5030, f"{subject}: a non-finite decimal has no value")
    return require_number_length(parsed, subject)


def require_number_length(number: Decimal, subject: str) -> Decimal:
    """Return the finite *number*, or refuse it (``FLX-5030``) by its length.

    Written out without an exponent, a number in a fidelity block needs at
    most :data:`_MOST_NUMBER_DIGITS` digits. One rule for both readers of
    the block's numbers: the check's (:func:`_decimal_of`) and the
    generator's (``flux_engine.generator.statistics.exact_decimal``),
    which reads the draw parameters the check never sees. The digits are
    counted from the decimal's own tuple, so no value is formed to count
    them (:func:`flux_stats.reading.digits_written_out`, the count a
    record's number is read by too).
    """
    needed = digits_written_out(number)
    if needed > _MOST_NUMBER_DIGITS:
        raise FluxError(
            FLX_5030,
            f"{subject}: written out, this number needs {needed} digits, and a number "
            f"in a fidelity block may need at most {_MOST_NUMBER_DIGITS}",
        )
    return number


def _require_decimal(value: Any, subject: str) -> Decimal:
    parsed = _decimal_of(value, subject)
    if parsed is None:
        raise FluxError(FLX_5030, f"{subject} is not an exact decimal")
    return parsed


def is_date_type(declared: str) -> bool:
    """Whether a column's declared type spelling is FLUID's DATE (case-insensitive).

    The same reading ``flux_engine.seam.contract.parse_logical_type`` gives the
    spelling: lower-cased, and with any parameter list set aside.
    """
    name, _ = split_type_params(" ".join(declared.lower().split()))
    return name == "date"


def _read_bound(value: Any, subject: str, *, date: bool = False) -> Tuple[Fraction, ValueDomain]:
    """Read a range bound as an exact decimal, else as an RFC 3339 instant.

    §2's example declares ``amount`` with a decimal range and
    ``aborted_at`` with a range of RFC 3339 timestamps, and gives the
    block no field to say which is which. The two spellings are disjoint
    -- an RFC 3339 date-time never parses as a decimal -- so the read is
    unambiguous: try the decimal, and fall back to KERNEL-SPEC §7's
    timestamp parser, whose result is integer milliseconds. A string that
    is neither is ``FLX-5030``.

    A bound of a column whose declared type is DATE (*date*) may be a day: a
    ``YYYY-MM-DD`` string, or a number of days since 1970-01-01. Both read as
    days in the ``DATE`` domain. An RFC 3339 instant stays what it was before
    the ``DATE`` domain existed, a ``TIMESTAMP`` bound in milliseconds, so a
    block a profiler declared with instants reads, and profiles, as it did.
    Any other string is ``FLX-5030``.
    """
    parsed = _decimal_of(value, subject)
    if date:
        if parsed is not None:
            return Fraction(parsed), ValueDomain.DATE
        try:
            return Fraction(parse_date_days(str(value))), ValueDomain.DATE
        except FluxError:
            pass
        try:
            return Fraction(parse_rfc3339_ms(str(value))), ValueDomain.TIMESTAMP
        except FluxError:
            raise FluxError(
                FLX_5030,
                f"{subject}: a DATE bound is a YYYY-MM-DD date, a number of days since "
                "1970-01-01, or an RFC 3339 instant",
            ) from None
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
    #: The levels the declaration names, for a ``SUPPORT`` column. A
    #: support *check* does not read them -- SEAM-FIDELITY-SPEC §5 compares
    #: the synthetic support against the **committed** state's support, not
    #: against a declaration. A level histogram does: the declared levels
    #: are its cells, as a histogram's declared bins are its cells. The
    #: generator draws from them too, and carrying them here means one
    #: parse serves every reader.
    support_values: Tuple[str, ...] = ()
    #: ``"exact"`` when the column declares ``allocation: exact`` (ADR 0016 section 6), else
    #: ``None``. The check never reads it; the engine's sampling margin does.
    allocation: Optional[str] = None
    #: The columns the column is declared ``given``, in the order the declaration lists them.
    given: Tuple[str, ...] = ()

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
    #: A distance tolerance that follows the row count (``sampling: true``): the verifier widens
    #: its margin, group by group, by the widening of sampling
    #: (:func:`flux_stats.noise.sampling_widening`).
    sampling: bool = False


@dataclass(frozen=True)
class BinsGrouping:
    """``groupByBins``: a property grouped by the bins of another range column (format 1-4-0).

    The groups are the bins, keyed by the canonical decimal text of each lower edge as the declaration
    writes it (a DATE column's edges are days), and a record whose value of ``column`` is null is in
    none (``null: exclude``) or refused (``null: fail``), as a ``groupBy`` column's null is.
    """

    column: str
    bins: Tuple[Fraction, ...]

    @property
    def keys(self) -> Tuple[str, ...]:
        """The group key of each bin, in the order of the bins."""
        return tuple(render_exact(edge) for edge in self.bins[:-1])


@dataclass(frozen=True)
class PropertyDeclaration:
    """One entry of the block's ``properties`` list (§2)."""

    index: int
    kind: PropertyKind
    columns: Tuple[str, ...]
    tolerance: Tolerance
    group_by: Optional[str] = None
    #: A histogram's edges, or the edges of a quantile that declares them
    #: (ADR 0012), which a quantile without bins leaves empty.
    bins: Tuple[Fraction, ...] = ()
    level: Optional[Fraction] = None
    min_variance: Optional[Fraction] = None
    #: A ``rankCorrelation``'s measure, ``kendall`` or ``spearman``; ``None`` for any other kind.
    measure: Optional[str] = None
    #: The bins the property is grouped by, in place of ``group_by``; at most one of the two is set.
    group_by_bins: Optional[BinsGrouping] = None

    @property
    def grouped(self) -> bool:
        """Whether the property is grouped, by a column or by bins."""
        return self.group_by is not None or self.group_by_bins is not None

    @property
    def property_id(self) -> str:
        """A stable, unique key for this property inside its block.

        The declaration's list position leads, so two properties that
        differ only in a tolerance -- which is exactly the change §8 says
        leaves ``G`` unchanged -- still key apart, and a record's property
        rows line up with the declaration order a reader is looking at.
        """
        tail = f"@{self.group_by}" if self.group_by is not None else ""
        if self.group_by_bins is not None:
            tail = f"@{self.group_by_bins.column}#bins"
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
            if prop.group_by_bins is not None:
                covered.add(prop.group_by_bins.column)
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
#: * ``flux_engine.generator.statistics``, for the draw -- the rest.
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
    "allocation",
    "bins",
    "binWeights",
    "binWeightsGiven",
    "family",
    "given",
    "identifierLength",
    "levelWeightsGiven",
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

#: The one value of ``fidelity.columns.<name>.allocation`` (SEAM-FIDELITY-SPEC section 2).
ALLOCATION_EXACT = "exact"
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
    "measure",
    "groupByBins",
)
_TOLERANCE_KEYS = ("type", "value", "max", "measure", "sampling")


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
    (:func:`declared_enum_values`). FLUID 0.7.5
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


def is_link_key(name: str, relationships: Sequence[str]) -> bool:
    """Whether *name* is ``<relationship>.<column>``, a column of the row a relationship reaches."""
    relationship, dot, column = name.partition(".")
    return bool(dot) and bool(column) and relationship in relationships


def _parse_column(
    name: str,
    raw: Any,
    expose_columns: Tuple[str, ...],
    declared_type: str = "",
    relationships: Sequence[str] = (),
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
    linked = name not in expose_columns and is_link_key(name, relationships)
    if name not in expose_columns and not linked:
        raise FluxError(FLX_5031, f"column {name!r} is not a member of the expose")
    mapping = _require_mapping(raw, f"fidelity.columns.{name}")
    _known_keys(mapping, _COLUMN_KEYS, f"fidelity.columns.{name}")
    if "allocation" in mapping and mapping["allocation"] != ALLOCATION_EXACT:
        raise FluxError(
            FLX_5030,
            f"fidelity.columns.{name}.allocation {mapping['allocation']!r} is not "
            f"{ALLOCATION_EXACT!r}, the one allocation there is",
        )
    if linked:
        # A column of a linked row is only ever a group key: its levels are declared here, and
        # its draw is the linked expose's own (SEAM-FIDELITY-SPEC section 2).
        extra = sorted(set(map(str, mapping)) - {"support", "maxCardinality", "null"})
        if extra or "support" not in mapping:
            raise FluxError(
                FLX_5030,
                f"fidelity.columns.{name} names a column of a linked row, which declares its "
                "support and maxCardinality and nothing else",
            )
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
        dated = is_date_type(declared_type)
        lower, lower_domain = _read_bound(
            bounds["min"], f"fidelity.columns.{name}.range.min", date=dated
        )
        upper, upper_domain = _read_bound(
            bounds["max"], f"fidelity.columns.{name}.range.max", date=dated
        )
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
            allocation=_allocation_of(mapping),
            given=_given_of(mapping),
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
        allocation=_allocation_of(mapping),
        given=_given_of(mapping),
    )


def _allocation_of(mapping: Mapping[str, Any]) -> Optional[str]:
    """``exact`` when the column declares it (any other value was refused above), else ``None``."""
    return ALLOCATION_EXACT if "allocation" in mapping else None


def _given_of(mapping: Mapping[str, Any]) -> Tuple[str, ...]:
    """The names in a column's ``given``, a name or a list of names; what the generator checks stays its own."""
    raw = mapping.get("given")
    if isinstance(raw, str):
        return (nfc(raw),)
    if isinstance(raw, (list, tuple)):
        return tuple(nfc(name) for name in raw if isinstance(name, str))
    return ()


def _tolerance_magnitude(
    mapping: Mapping[str, Any],
    tolerance_kind: ToleranceKind,
    subject: str,
    *,
    ranked: bool = False,
) -> Fraction:
    """The ``value`` or ``max`` a tolerance of *tolerance_kind* carries, as an exact fraction.

    A ``rankCorrelation``'s absolute tolerance (*ranked*) is written with ``max``, as a bound on
    a distance is; every other absolute tolerance is written with ``value``.
    """
    if ranked:
        if "max" not in mapping:
            raise FluxError(FLX_5034, f"{subject} needs a max")
        if "value" in mapping:
            raise FluxError(FLX_5030, f"{subject}: a rank correlation's tolerance has no value")
        magnitude = Fraction(_require_decimal(mapping["max"], f"{subject}.max"))
        if magnitude <= 0:
            raise FluxError(FLX_5030, f"{subject}.max must be positive")
        return magnitude
    if tolerance_kind in (ToleranceKind.ABSOLUTE, ToleranceKind.RELATIVE):
        if "value" not in mapping:
            raise FluxError(FLX_5034, f"{subject} needs a value")
        if "max" in mapping:
            raise FluxError(FLX_5030, f"{subject}: a {tolerance_kind.value} tolerance has no max")
        magnitude = Fraction(_require_decimal(mapping["value"], f"{subject}.value"))
        if magnitude <= 0:
            raise FluxError(FLX_5030, f"{subject}.value must be positive")
        return magnitude
    if "max" not in mapping:
        raise FluxError(FLX_5034, f"{subject} needs a max")
    if "value" in mapping:
        raise FluxError(FLX_5030, f"{subject}: a {tolerance_kind.value} tolerance has no value")
    if tolerance_kind is ToleranceKind.COUNT:
        return Fraction(_require_int(mapping["max"], f"{subject}.max"))
    magnitude = Fraction(_require_decimal(mapping["max"], f"{subject}.max"))
    if magnitude <= 0:
        raise FluxError(FLX_5030, f"{subject}.max must be positive")
    return magnitude


def _tolerance_sampling(
    mapping: Mapping[str, Any], tolerance_kind: ToleranceKind, subject: str
) -> bool:
    """Whether a distance tolerance widens by the sampling variance; false when undeclared."""
    if "sampling" not in mapping:
        return False
    if tolerance_kind is not ToleranceKind.DISTANCE:
        raise FluxError(FLX_5030, f"{subject}: only a distance tolerance carries sampling")
    if not isinstance(mapping["sampling"], bool):
        raise FluxError(
            FLX_5030,
            f"{subject}.sampling must be a boolean, got {type(mapping['sampling']).__name__}",
        )
    return mapping["sampling"]


def _tolerance_declared(mapping: Mapping[str, Any]) -> Dict[str, str]:
    """Each declared key of a tolerance with its value as text, in key order."""
    return {
        str(key): (
            ("true" if mapping[key] is True else "false")
            if key == "sampling"
            else str(mapping[key])
        )
        for key in sorted(str(k) for k in mapping)
    }


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

    magnitude = _tolerance_magnitude(
        mapping, tolerance_kind, subject, ranked=kind is PropertyKind.RANK_CORRELATION
    )
    sampling = _tolerance_sampling(mapping, tolerance_kind, subject)
    declared = _tolerance_declared(mapping)
    return Tolerance(
        kind=tolerance_kind,
        magnitude=magnitude,
        measure=measure,
        declared=MappingProxyType(declared),
        sampling=sampling,
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

    if kind is PropertyKind.CORRELATION or kind is PropertyKind.RANK_CORRELATION:
        noun = "a correlation" if kind is PropertyKind.CORRELATION else "a rank correlation"
        if len(names) != 2:
            raise FluxError(
                FLX_5033,
                f"{subject}: {noun} declares two columns, got {len(names)}",
            )
        if names[0] == names[1]:
            raise FluxError(FLX_5033, f"{subject}: {noun} needs two distinct columns")
    elif len(names) != 1:
        raise FluxError(FLX_5030, f"{subject}: a {kind.value} property names one column")
    return names


def _parse_bins(
    raw: Any, column: ColumnDeclaration, grid: Grid, subject: str
) -> Tuple[Fraction, ...]:
    """Read the edges of a histogram, or of a quantile, and check they partition the range.

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
        edge, domain = _read_bound(
            value, f"{subject}.bins[{index}]", date=column.domain is ValueDomain.DATE
        )
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


def _parse_group_by_bins(
    mapping: Mapping[str, Any],
    names: Tuple[str, ...],
    columns: Mapping[str, ColumnDeclaration],
    grid: Grid,
    subject: str,
    group_by: Optional[str],
) -> Optional[BinsGrouping]:
    """Read ``groupByBins``: ``{column, bins}``, the profile's spelling (PROFILE-SPEC.md section 5).

    ``column`` is a range column of the block other than the property's own, and ``bins`` are at least
    two strictly increasing edges on the grid, in the column's domain, that cover its declared range,
    so that every value a record may hold lies in a bin (a bin is half open and the last is closed).
    """
    if "groupByBins" not in mapping:
        return None
    here = f"{subject}.groupByBins"
    if group_by is not None:
        raise FluxError(
            FLX_5030, f"{subject}: a property holds at most one of groupBy and groupByBins"
        )
    raw = _require_mapping(mapping["groupByBins"], here)
    _known_keys(raw, ("column", "bins"), here)
    if "column" not in raw or "bins" not in raw:
        raise FluxError(FLX_5034, f"{here} needs a column and bins")
    name = nfc(str(raw["column"]))
    if name not in columns:
        raise FluxError(FLX_5034, f"{here}: column {name!r} has no declaration in fidelity.columns")
    column = columns[name]
    if column.kind is not ColumnKind.RANGE:
        raise FluxError(
            FLX_5030, f"{here}: groupByBins needs a range column, and {name!r} is a support"
        )
    if name in names:
        raise FluxError(FLX_5030, f"{here}: groupByBins repeats the property's own column")
    listed = raw["bins"]
    if not isinstance(listed, Sequence) or isinstance(listed, (str, bytes)):
        raise FluxError(FLX_5030, f"{here}.bins must be a list")
    if len(listed) < 2:
        raise FluxError(FLX_5032, f"{here}.bins needs at least two edges")
    edges: List[Fraction] = []
    for position, value in enumerate(listed):
        edge, domain = _read_bound(
            value, f"{here}.bins[{position}]", date=column.domain is ValueDomain.DATE
        )
        if domain is not column.domain:
            raise FluxError(
                FLX_5032, f"{here}.bins[{position}] is not in the same domain as the column's range"
            )
        if edges and edge <= edges[-1]:
            raise FluxError(FLX_5032, f"{here}.bins[{position}] does not increase")
        if not grid.is_on_grid(edge):
            raise FluxError(FLX_5032, f"{here}.bins[{position}] does not land on the declared grid")
        edges.append(edge)
    interval = column.interval
    if edges[0] > interval.lo or edges[-1] < interval.hi:
        raise FluxError(
            FLX_5032, f"{here}.bins do not cover the column's declared minimum and maximum"
        )
    return BinsGrouping(column=name, bins=tuple(edges))


def bin_group_keys(
    rows: Sequence[Row],
    grouping: BinsGrouping,
    column: ColumnDeclaration,
    grid: Grid,
    *,
    first_row: int = 0,
) -> List[Tuple[str, List[int]]]:
    """Partition row indices by the bin each row's value of the grouping column falls in.

    A group's key is the text of its bin's lower edge; groups with no record are left out, and come
    back in the order of the bins, never in the order the rows arrived. A null is left out under
    ``exclude`` and refused under ``fail``, as :func:`flux_stats.reading.group_keys` does.
    """
    edges = [grid.quantise(edge, subject="bin edge") for edge in grouping.bins]
    keys = grouping.keys
    buckets: Dict[int, List[int]] = {}
    for index, row in enumerate(rows):
        named = first_row + index
        value = cell(row, column, row_index=named)
        if value is None:
            if column.null_policy is NullPolicy.FAIL:
                raise FluxError(FLX_5005, f"column {column.name!r} row {named}: null is refused")
            continue
        units = encode_value(value, column, grid, row_index=named)
        buckets.setdefault(bin_index(units, edges), []).append(index)
    return [(keys[position], buckets[position]) for position in sorted(buckets)]


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

    if kind is PropertyKind.RANK_CORRELATION and ("groupBy" in mapping or "groupByBins" in mapping):
        raise FluxError(FLX_5030, f"{subject}: a rank correlation is ungrouped")
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

    group_by_bins = _parse_group_by_bins(mapping, names, columns, grid, subject, group_by)
    if "tolerance" not in mapping and kind is PropertyKind.RANK_CORRELATION:
        mapping = {**mapping, "tolerance": dict(DEFAULT_RANK_TOLERANCE)}
    if "tolerance" not in mapping:
        raise FluxError(FLX_5034, f"{subject} needs a tolerance")
    tolerance = _parse_tolerance(mapping["tolerance"], kind, f"{subject}.tolerance")

    bins: Tuple[Fraction, ...] = ()
    if kind is PropertyKind.MARGINAL_HISTOGRAM:
        if "bins" not in mapping:
            raise FluxError(FLX_5034, f"{subject} needs bins")
        bins = _parse_bins(mapping["bins"], columns[names[0]], grid, subject)
    elif kind is PropertyKind.QUANTILE and "bins" in mapping:
        # A quantile may carry bins, under a histogram's rules (ADR 0012): it
        # is then formed and compared as the bin that holds it, ``quantile/2``.
        bins = _parse_bins(mapping["bins"], columns[names[0]], grid, subject)
    elif "bins" in mapping:
        if kind is PropertyKind.LEVEL_HISTOGRAM:
            # The column's declared support is the cell set; a second
            # spelling of it here would be two digests of one contract.
            raise FluxError(
                FLX_5030,
                f"{subject}: a levelHistogram counts its column's declared support "
                "levels and carries no bins",
            )
        raise FluxError(FLX_5030, f"{subject}: only a marginalHistogram or a quantile carries bins")

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

    measure: Optional[str] = None
    if kind is PropertyKind.RANK_CORRELATION:
        if "measure" not in mapping:
            raise FluxError(FLX_5034, f"{subject} needs a measure")
        measure = nfc(str(mapping["measure"]))
        if measure not in RANK_MEASURES:
            raise FluxError(
                FLX_5030, f"{subject}.measure is {' or '.join(RANK_MEASURES)}, not {measure!r}"
            )
    elif "measure" in mapping:
        raise FluxError(FLX_5030, f"{subject}: only a rankCorrelation carries a measure")

    return PropertyDeclaration(
        index=index,
        kind=kind,
        columns=names,
        tolerance=tolerance,
        group_by=group_by,
        bins=bins,
        level=level,
        min_variance=min_variance,
        measure=measure,
        group_by_bins=group_by_bins,
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
    relationships = tuple(nfc(name) for name in getattr(expose, "relationship_names", ()))
    declared_types = {
        nfc(str(name)): str(spelling)
        for name, spelling in getattr(expose, "column_types", {}).items()
    }
    parsed_columns: Dict[str, ColumnDeclaration] = {}
    for key in sorted(nfc(str(name)) for name in raw_columns):
        original = next(name for name in raw_columns if nfc(str(name)) == key)
        declaration = _parse_column(
            key, raw_columns[original], expose_columns, declared_types.get(key, ""), relationships
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
    # An empty list is a table with nothing to score (a table whose columns the
    # profile gives only order statistics): it is legal, and `verify` reports it
    # as not scored rather than as verified.
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


# ---------------------------------------------------------------------------
# Measuring one group of a property
# ---------------------------------------------------------------------------


def _level_histogram_group(
    rows: Sequence[Row],
    indices: Sequence[int],
    key: str,
    column: ColumnDeclaration,
) -> GroupStatistic:
    """One group of a level histogram: records per declared level of *column*."""
    slots = {level: slot for slot, level in enumerate(level_cells(column))}

    def slot_of(value: Any, *, row_index: int) -> int:
        slot = slots.get(category(value, column, row_index=row_index))
        if slot is None:
            # A level the declaration does not name has no cell, as a value
            # outside the declared range has no bin: refused, never dropped
            # and never counted into a neighbour.
            raise FluxError(
                FLX_5005,
                f"column {column.name!r} row {row_index}: value outside the declared support",
            )
        return slot

    found = read_column(rows, indices, column, slot_of)
    return definition_for_kind(PropertyKind.LEVEL_HISTOGRAM).measure(
        key, found, Parameters(level_count=len(slots))
    )


def definition_for_property(declaration: PropertyDeclaration) -> StatisticDefinition:
    """The one registered definition that forms and compares *declaration*'s property.

    The kind names it, except for a ``quantile`` that declares ``bins``, which
    is ``quantile/2`` where one without them is ``quantile/1`` (ADR 0012).
    Everything that picks a property's statistic, to measure it, spell it,
    compare it, match a profile's or score it, picks it here, so no two of
    them can disagree about which one a property is.
    """
    if declaration.kind is PropertyKind.RANK_CORRELATION:
        return RANK_DEFINITIONS_BY_MEASURE[declaration.measure or ""]
    binned = declaration.kind is PropertyKind.QUANTILE and bool(declaration.bins)
    return definition_for_kind(declaration.kind, binned=binned)


def measure_group(
    rows: Sequence[Row],
    indices: Sequence[int],
    key: str,
    declaration: PropertyDeclaration,
    columns: Mapping[str, ColumnDeclaration],
    grid: Grid,
) -> GroupStatistic:
    """Read one group's values for one property, then measure them.

    The reading is :mod:`flux_stats.reading`'s, on the columns this
    function picks: the null policy, and every refusal a record can meet.
    The measuring is the property's one registered definition
    (:func:`definition_for_property`). The fidelity check forms each
    group of a property with this function, and a profiler forms the same
    group the same way, from the same parsed declaration.
    """
    kind = declaration.kind
    statistic = definition_for_property(declaration)

    if kind is PropertyKind.SUPPORT:
        column = columns[declaration.columns[0]]
        categories = read_column(rows, indices, column, partial(category, column=column))
        group = statistic.measure(key, categories, Parameters())
        maximum = column.max_cardinality
        if maximum is not None and len(group.support) > maximum:
            raise FluxError(
                FLX_5005,
                f"column {column.name!r}: {len(group.support)} distinct members exceed the "
                f"declared maxCardinality {maximum}",
            )
        return group

    if kind is PropertyKind.LEVEL_HISTOGRAM:
        return _level_histogram_group(rows, indices, key, columns[declaration.columns[0]])

    if kind is PropertyKind.CORRELATION or kind is PropertyKind.RANK_CORRELATION:
        pairs = read_pairs(
            rows,
            indices,
            columns[declaration.columns[0]],
            columns[declaration.columns[1]],
            grid,
        )
        return statistic.measure(key, pairs, Parameters())

    column = columns[declaration.columns[0]]
    values = read_column(rows, indices, column, partial(encode_value, column=column, grid=grid))

    if kind is PropertyKind.MEAN:
        return statistic.measure(key, values, Parameters())

    if kind is PropertyKind.MARGINAL_HISTOGRAM:
        edges = tuple(grid.quantise(edge, subject="bin edge") for edge in declaration.bins)
        return statistic.measure(key, values, Parameters(edges=edges))

    # QUANTILE: with bins it is quantile/2, which needs the encoded edges too.
    edges = tuple(grid.quantise(edge, subject="bin edge") for edge in declaration.bins)
    return statistic.measure(key, values, Parameters(edges=edges, level=declaration.level))
