# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Generating columns from a FLUID contract's rules (EXPR-SPEC-2 §6, ADR 0011 step 1).

A contract rule (``exposes[].contract.invariants[]``) that determines a column
is applied to every generated row, so the column holds what the rule says. Steps 1
and 2 generate the two forms whose value is a function of the row alone, and one
whose value is a function of its key:

* a ``derive`` with ``expr`` that reads only the row's own columns;
* a ``check`` of difference form over days, ``k = DAYS_BETWEEN(x, c)`` or
  ``k = DAYS_BETWEEN(c, x)``, whose column ``c`` the Simulation names under
  ``extensions["com.agentics.flux/1"].emits[<exposeId>].invariants.<id>.generates``.

* a contract ``dependency``, one way, whose band is zero violations or an upper bound
  (EXPR-SPEC-2 section 8, ADR 0011 step 2): each key gets its ``to`` values once, on a
  stream addressed by the key, and every row of the key copies them. Under an upper
  bound the ``to`` columns' NULLs fall on NULL rows, drawn per row, so the share of rows
  they make violate stays inside the band.

* on kernel 0.2.0 only, a ``derive`` with ``expr``, and a difference form over days, that read a
  column of the row a link reaches (EXPR-SPEC-2 section 6, ADR 0022): a contract's one hop
  ``<relationship>.<column>`` and a FLUX-side chain of to-one relationships. The value read is what
  the linked expose's own pass writes at the row the link names, so such a plan is applied by the
  rows, through the plans its ``reads`` carry; a column that declares a ``nullFraction`` keeps its
  own NULLs (NULLs first, section 9), which a rule that allows no violating row cannot meet
  (``FLX-6204``).

* on kernel 0.2.0 only, a FLUX-side ``derive`` whose value holds a window over the row's siblings
  (EXPR-SPEC-2 section 5, ADR 0022 section 7): ``ROW_NUMBER``, ``RANK``, ``DENSE_RANK``,
  ``FIRST_VALUE``, ``LAST_VALUE``, ``LAG`` and ``LEAD``. An
  expose with such a rule is made whole in each process that needs any of its rows
  (:func:`apply_rules_whole`): every row's cells, then the rule steps in order, each step over
  every row before the next, so a window sees every sibling's final values of what it reads.

* on kernel 0.2.0 only, a contract ``derive`` with ``aggregate`` over a relationship the Simulation
  lays out with a ``counts`` entry (EXPR-SPEC-2 section 6, ADR 0016 section 5): the value is the
  summary of the parent's children, the run of child rows the counted layout gives it, as the child's
  own pass writes them. The parent's plan carries the child plans (``GeneratorPlan.sums``), and its rows
  are made a row at a time.
* On a history's emit (kernel 0.2.0, ADR 0017 section 7) a FLUX-side ``derive`` that reads ``previous.<column>``
  or ``previous.<relationship>.<column>``. The previous version of row ``i`` is row ``i - 1``, read as the
  expose's own pass writes it, unless ``i`` opens its entity's chain, where every ``previous`` path is NULL.

Everything else that would determine a column is refused at plan time with
``FLX-6210`` until a later step brings it: an aggregate over any other relationship, a FLUX-side
text that holds one, a dependency with ``bothWays`` or a
band with a lower bound above zero, a bounds form, a month or year difference, a rule that reads
``previous`` outside a history's emit or reads children (and one that reads a link or a window over siblings, on kernel 0.1.0), a ``percent``
band that needs violating rows, a key or a foreign key, a relationship's
``toColumns``, a column another is drawn ``given``, and an emit with no
``fidelity`` block. This version never generates a violating row.

Borrowed patterns. PostgreSQL's generated columns: a generation expression
reads only the current row, which is the scope this step keeps (``GENERATED
ALWAYS AS (expr) STORED``). dbldatagen's ``baseColumn`` ordering: a derived
column is computed after the columns it reads, here a topological order over
what each rule reads, ties in declared column order.

A ``derive`` or a difference form draws nothing. Each rule is applied after the row's own
cells are drawn, so the cells it replaces still consume their streams and no other cell
moves; the output is a function of the row's index and the plan alone, and ``--jobs``
cannot change it. A dependency's ``to`` column draws on a stream of its own, keyed by the
dependency's key (:func:`flux_engine.generator.paths.dependency_stream`), so its value is a
function of the key and the run, whatever the row; under an upper bound a row's NULLs
draw on the dependency's stream for that row
(:func:`flux_engine.generator.paths.dependency_row_stream`).

:func:`model_from_contract` reads every expose of a contract into the
:class:`~flux_engine.expr2.scope.Model` the checker resolves names against.
A contract's texts are core texts and name no version; a FLUX-side text can, on an expose
whose entity has a validity window, and is then refused as a read of another row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from fractions import Fraction
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Collection,
    Dict,
    FrozenSet,
    Hashable,
    Iterator,
    List,
    Mapping,
    NoReturn,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

import numpy as np

from flux_engine.core.canonical import canonical_bytes, nfc
from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import FLX_2202
from flux_engine.expr2.ast import (
    Aggregate,
    And,
    Call,
    Children,
    Column,
    Compare,
    Node,
    Path,
    Window,
    quote_name,
    unparse,
)
from flux_engine.expr2.check import PREDICATE, VALUE, Checked, check
from flux_engine.expr2.errors import (
    FLX_6111,
    FLX_6112,
    FLX_6113,
    FLX_6201,
    FLX_6202,
    FLX_6203,
    FLX_6204,
    FLX_6205,
    FLX_6208,
    FLX_6210,
    Expr2Error,
)
from flux_engine.expr2.evaluate import Session, key_value
from flux_engine.expr2.parser import Side, parse
from flux_engine.expr2.rules import (
    aggregate_tree,
    check_flux_side_rule,
    check_language,
    check_rule_ids,
    subtrees,
)
from flux_engine.expr2.scope import Expose, Link, Model, Versions
from flux_engine.expr2.types import ColumnType, Kind, column_type, fit
from flux_engine.generator.dates import (
    CANDIDATE_POSITION,
    CANDIDATES,
    HOLD_POSITION,
    KIND_DATE,
    TIMESTAMP_MAX_MS,
    TIMESTAMP_MIN_MS,
    DatesTally,
    Duration,
    Lifetime,
    Period,
    WithinLevel,
    cut_end,
    cut_table,
    date_of_days,
    days_of_date,
    draw_within,
    fit_in_bounds,
    holding_probability,
    inside,
    intersect,
    interval_of,
    lifetime_in_units,
    lifetimes_meet,
    offset_table,
    offsets_reach_forward,
    positive_target,
    unit_of,
    window_in_units,
)
from flux_engine.generator.rates import Rates, thin
from flux_engine.generator.statistics import DATE_EPOCH_ORDINAL
from flux_engine.seam.asof import AsOfEntry
from flux_engine.seam.contract import declared_enum_values
from flux_engine.seam.copies import declares_copies
from flux_engine.seam.coverage import (
    SOURCE_FLUX_RULE_PREFIX,
    SOURCE_LIFETIME_PREFIX,
    SOURCE_RELATIONSHIP_PREFIX,
    SOURCE_RULE_PREFIX,
    SOURCE_SHARED_ATTRIBUTE_PREFIX,
    SOURCE_WITHIN_PREFIX,
)
from flux_engine.seam.declared import (
    KIND_CHECK,
    KIND_CHECK_OVER_LINK,
    KIND_SHARED_ATTRIBUTE,
    KIND_SHARED_COLUMNS,
    REASON_KEY_COLUMN,
    REASON_SELF_LINK,
    NotGenerated,
)
from flux_engine.seam.history import HistoryEntry
from flux_engine.seam.keys import (
    ContractKeys,
    ExposeKeys,
    compile_keys,
    declared_exposes,
    window_ends,
)
from flux_engine.seam.links import LinkEntry
from flux_engine.seam.resolve import FLX_3105

if TYPE_CHECKING:
    from flux_engine.core.seedtree import SeedTreeRoot
    from flux_engine.generator.rows import ColumnPlan, GeneratorPlan

__all__ = [
    "ORIGIN_BUNDLE",
    "ORIGIN_CONTRACT",
    "ORIGIN_SHARED",
    "Band",
    "ParentCondition",
    "PickCopy",
    "PlannedRule",
    "RulePlan",
    "apply_rules",
    "apply_rules_whole",
    "new_tally",
    "declared_not_generated",
    "flux_rule_entries",
    "model_from_contract",
    "parent_condition",
    "plan_rules",
    "shared_attribute_members",
    "shared_copy_texts",
    "shared_copy_texts_not_generated",
]

#: The settings a contract rule can carry in the Simulation (EXPR-SPEC-2 §1).
_SETTING_KEYS = frozenset({"generates"})

_DIFFERENCES = {"DAYS_BETWEEN": "days", "MONTHS_BETWEEN": "months", "YEARS_BETWEEN": "years"}
_ORDER_OPERATORS = frozenset({"<", "<=", ">", ">="})
_BAND_KEYS = (
    "mustBe",
    "mustBeLessThan",
    "mustBeLessOrEqualTo",
    "mustBeGreaterThan",
    "mustBeGreaterOrEqualTo",
    "mustBeBetween",
)

#: Where a rule was declared: the contract's invariants, or the bundle's FLUX-side rules.
ORIGIN_CONTRACT = "contract"
ORIGIN_BUNDLE = "bundle"
#: A copy read from a relationship's ``sharedColumns`` or a column's ``sharedAttribute``
#: (EXPR-SPEC-2 section 9, ADR 0022 section 4): kernel 0.2.0 only.
ORIGIN_SHARED = "shared"
#: The language a copy through several links is read in.
SHARED_CHAIN_LANGUAGE = "flux-expr/2"

#: The keys a FLUX-side rule entry carries (EXPR-SPEC-2 §1): its texts and its band.
_FLUX_RULE_KEYS = frozenset(
    {"id", "language", "derive", "expr", "check", "when", "unit", *_BAND_KEYS, "generates"}
)

_DEPENDENCY_KEYS = frozenset({"from", "to", "bothWays"})

FORM_DERIVE = "derive"
FORM_DEPENDENCY = "dependency"
FORM_BOUNDS = "check-bounds"
FORM_OTHER = "check-other"
#: A lifetime's end drawn as a duration from its start (ADR 0024 section 4), kernel 0.2.0 only.
FORM_DURATION = "lifetime"
#: A date within the lifetime of the row a link reaches (ADR 0024 section 5), kernel 0.2.0 only.
FORM_WITHIN = "within"

#: The position of a date's direct value on its rule stream (ADR 0024 section 6): position 0 is
#: the hold draw, which only a ``target`` above zero takes.
DATES_VALUE_POSITION = 1


# ---------------------------------------------------------------------------
# The model a contract's texts are checked against
# ---------------------------------------------------------------------------


def _placeholders(entry: Mapping[str, Any]) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    block = entry.get("placeholders")
    if not isinstance(block, Mapping):
        return (), ()

    def texts(key: str) -> Tuple[str, ...]:
        raw = block.get(key)
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return ()
        return tuple(item for item in raw if isinstance(item, str) and item)

    return texts("values"), texts("prefixes")


def _column_of(entry: Mapping[str, Any]) -> ColumnType:
    declared = str(entry.get("type"))
    try:
        levels = declared_enum_values(declared)
    except FluxError:
        # An expose the Simulation does not emit is never compiled by the seam, so a
        # level list it cannot read is not this reader's refusal to make.
        levels = ()
    values, prefixes = _placeholders(entry)
    return column_type(
        declared,
        enum=levels or None,
        placeholder_values=values,
        placeholder_prefixes=prefixes,
    )


def _versions_of(
    entry: Mapping[str, Any], keys: Optional[Any], expose_id: str
) -> Optional[Versions]:
    """The versions of an expose whose entity has a validity window, or ``None``.

    The entity is the expose's ``natural`` entity, and the window is its two time dimensions
    with ``typeParams.validityParams`` ``isStart`` and ``isEnd`` (EXPR-SPEC-2 §3).
    """
    if keys is None:
        return None
    natural = tuple(
        entity.column for entity in keys.expose(expose_id).entities if entity.kind == "natural"
    )
    ends = window_ends(entry.get("semantics"))
    if not natural or "start" not in ends:
        return None
    return Versions(natural, ends["start"], ends.get("end"))


@dataclass(frozen=True)
class OpenExpose(Expose):
    """An expose with a validity window that a path may cross (ADR 0017 section 8).

    It holds the same versions as :class:`~flux_engine.expr2.scope.Expose`, so ``previous`` still
    resolves on it, but it does not declare a window to a path that reaches it: the planner then
    decides which of its columns a path may read, and the generator which version it reads.
    """

    @property
    def declares_validity_window(self) -> bool:
        return False


def _contract_columns(entry: Mapping[str, Any]) -> Dict[str, ColumnType]:
    """The columns of an expose's declared schema, in declared order."""
    contract = entry.get("contract")
    schema = contract.get("schema") if isinstance(contract, Mapping) else None
    columns: Dict[str, ColumnType] = {}
    if isinstance(schema, Sequence) and not isinstance(schema, (str, bytes)):
        for column in schema:
            if isinstance(column, Mapping) and isinstance(column.get("name"), str):
                columns.setdefault(nfc(column["name"]), _column_of(column))
    return columns


def _links_and_key(
    keys: Optional[ContractKeys], expose_id: str
) -> Tuple[Dict[str, Link], Tuple[str, ...]]:
    """The links of an expose and the columns of its first ``primary`` entity."""
    links: Dict[str, Link] = {}
    if keys is None:
        return links, ()
    declared = keys.expose(expose_id)
    for relationship in declared.relationships:
        links[relationship.name] = Link(
            relationship.name,
            relationship.target,
            relationship.from_columns,
            relationship.to_columns,
        )
    primary = [entity.column for entity in declared.entities if entity.kind == "primary"]
    return links, ((primary[0],) if primary else ())


def model_from_contract(
    contract_document: Mapping[str, Any], *, open_to: Collection[str] = ()
) -> Model:
    """Every expose of *contract_document* as the row scope a text is checked against.

    * Columns come in declared order, typed through RFC-data-rules.md §2.3
      (:func:`~flux_engine.expr2.types.column_type`); the levels of an
      ``enum(...)`` declared type and a column's ``placeholders`` are read.
    * Links are ``semantics.relationships`` (``name``, ``to``, ``fromColumns``,
      ``toColumns``), read through :func:`~flux_engine.seam.keys.compile_keys`, so
      a relationship that does not resolve is its ``FLX-3005``. A contract
      whose FLUID version carries no relationships has no links.
    * The key is the columns of the expose's first ``primary`` entity.

    An expose whose natural entity has a validity window carries its versions, so a
    FLUX-side ``previous`` resolves there and an unversioned expose refuses it
    (``FLX-6106``). A contract's texts are core texts, in which ``previous.x`` names a
    relationship's column, so none of them reaches a previous version.

    The exposes named by *open_to* are history emits (ADR 0017 section 8): a path may cross a
    relationship that reaches one (:class:`OpenExpose`), and the planner checks what it reads.
    """
    keys = compile_keys(contract_document)
    exposes: Dict[str, Expose] = {}
    for expose_id, entry in declared_exposes(contract_document).items():
        columns = _contract_columns(entry)
        links, key = _links_and_key(keys, expose_id)
        kind = OpenExpose if expose_id in open_to else Expose
        exposes[expose_id] = kind(
            expose_id, columns, links, key, _versions_of(entry, keys, expose_id)
        )
    return Model(exposes)


# ---------------------------------------------------------------------------
# Bands (RFC-data-rules.md §1.4, EXPR-SPEC-2 §7)
# ---------------------------------------------------------------------------


def _number(value: Any, where: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        raise ValueError(f"{where} is a number")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{where} is a number") from None
    if not number.is_finite():
        raise ValueError(f"{where} is a finite number")
    return number


@dataclass(frozen=True)
class Band:
    """The expected number or share of violating rows: one ODCS operator and its ``unit``."""

    operator: str
    values: Tuple[Decimal, ...]
    unit: str

    @classmethod
    def read(cls, invariant: Mapping[str, Any], where: str) -> Optional["Band"]:
        """The band *invariant* declares, or ``None`` when it declares none."""
        named = [key for key in _BAND_KEYS if key in invariant]
        if len(named) > 1:
            raise ValueError(f"{where} declares more than one operator: {named}")
        unit = invariant.get("unit", "rows")
        if unit not in ("rows", "percent"):
            raise ValueError(f"{where}: unit is 'rows' or 'percent'")
        if not named:
            return None
        operator = named[0]
        raw = invariant[operator]
        values: Tuple[Decimal, ...]
        if operator == "mustBeBetween":
            if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) != 2:
                raise ValueError(f"{where}: mustBeBetween is [low, high]")
            values = (_number(raw[0], where), _number(raw[1], where))
        else:
            values = (_number(raw, where),)
        return cls(operator, values, str(unit))

    def zero_violations_meet(self) -> bool:
        """Whether a run with no violating row is within the band."""
        zero = Decimal(0)
        first = self.values[0]
        if self.operator == "mustBe":
            return first == zero
        if self.operator == "mustBeLessThan":
            return zero < first
        if self.operator == "mustBeLessOrEqualTo":
            return zero <= first
        if self.operator == "mustBeGreaterThan":
            return zero > first
        if self.operator == "mustBeGreaterOrEqualTo":
            return zero >= first
        return first <= zero <= self.values[1]

    def allows_only_zero(self) -> bool:
        """Whether zero violations is the one count the band allows (EXPR-SPEC-2 section 8).

        ``mustBe`` or ``mustBeLessOrEqualTo`` zero, ``mustBeBetween`` ``[0, 0]``, and, in
        ``rows`` only, ``mustBeLessThan`` a value up to one, since a count is a whole number.
        """
        zero = Decimal(0)
        first = self.values[0]
        if self.operator in ("mustBe", "mustBeLessOrEqualTo"):
            return first == zero
        if self.operator == "mustBeLessThan":
            return self.unit == "rows" and zero < first <= 1
        if self.operator == "mustBeBetween":
            return first == zero and self.values[1] == zero
        return False

    def upper(self) -> Optional[Tuple[Decimal, bool]]:
        """The band's upper bound and whether it is strict, or ``None`` when it has none."""
        if self.operator in ("mustBe", "mustBeLessOrEqualTo"):
            return self.values[0], False
        if self.operator == "mustBeLessThan":
            return self.values[0], True
        if self.operator == "mustBeBetween":
            return self.values[1], False
        return None

    def needs_a_target(self) -> bool:
        """Whether a run needs a target strictly inside the band: a ``percent`` band of positive width."""
        if self.unit != "percent":
            return False
        first = self.values[0]
        if self.operator == "mustBe":
            return False
        if self.operator == "mustBeBetween":
            return self.values[1] > first
        if self.operator in ("mustBeLessThan", "mustBeLessOrEqualTo"):
            return first > 0
        return first < 100

    def as_document(self) -> Dict[str, Any]:
        return {
            "operator": self.operator,
            "values": [str(value) for value in self.values],
            "unit": self.unit,
        }


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedRule:
    """One invariant of the emitted expose, as the plan records it.

    ``text`` is the canonical text of the rule: the ``check``, or a ``derive``'s
    ``expr``. ``origin`` says where it was declared, and ``language`` is the one a FLUX-side
    rule (origin ``bundle``) declares, which its texts are read in. ``column`` is the column the rule determines, or ``None`` for a
    rule that only checks. ``k``, ``x`` and ``sign`` are a difference form's two
    sides and its direction (``c = x + sign * k`` days).
    """

    id: str
    form: str
    text: str
    column: Optional[str] = None
    when: Optional[str] = None
    band: Optional[Band] = None
    k: Optional[str] = None
    x: Optional[str] = None
    sign: int = 1
    reads: Tuple[str, ...] = ()
    origin: str = ORIGIN_CONTRACT
    language: Optional[str] = None
    #: A dependency's ``from`` and ``to`` columns, and the ``to`` columns that are foreign keys.
    from_columns: Tuple[str, ...] = ()
    to_columns: Tuple[str, ...] = ()
    foreign_columns: Tuple[str, ...] = ()
    #: The share of covered rows that are NULL rows, when a dependency's band draws its ``to``
    #: columns' NULLs per row (EXPR-SPEC-2 section 8); ``None`` when they are drawn per key.
    null_rows: Optional[Decimal] = None
    #: The relationship paths the rule's texts read (``Read.path`` of each read through a link),
    #: sorted (EXPR-SPEC-2 section 6). Empty for a rule that reads only its own row.
    paths: Tuple[Tuple[str, ...], ...] = ()
    #: Whether a ``derive``'s whole value is a path: its column is then a copy of the linked row's.
    whole_path: bool = False
    #: The coverage source of a copy read from ``sharedColumns`` or ``sharedAttribute``; ``None``
    #: for every other rule, whose source is its id.
    source: Optional[str] = None
    #: What names a copy in a refusal that its path earns: the ``sharedAttribute`` and its members.
    blame: str = ""
    #: Whether a ``derive``'s texts hold a window over the row's siblings (EXPR-SPEC-2 section 5):
    #: the expose is then made whole in each process that needs a row of it (ADR 0022 section 7).
    windowed: bool = False
    #: The text of the sentinel a copy writes on a row whose link reaches no row, in place of NULL
    #: (ADR 0022 section 6); ``None`` for a rule with none.
    sentinel: Optional[str] = None
    #: The child expose and relationship a contract ``derive`` with ``aggregate`` summarises
    #: (EXPR-SPEC-2 section 5, ADR 0016 section 5); ``None`` for every other rule. Its ``text`` is
    #: the summary written as a FLUX-side text.
    summary: Optional[Tuple[str, str]] = None
    #: A lifetime's duration (form ``lifetime``): the end is drawn from the row's own start.
    duration: Optional[Duration] = None
    #: A ``within`` entry (form ``within``): the date is drawn inside the lifetime of the linked row.
    within: Optional[WithinLevel] = None
    #: A lifetime's end that is also a period within a linked row's lifetime (form ``lifetime``).
    period: Optional[Period] = None
    #: Whether a FLUX-side ``derive`` reads the entity's previous version (EXPR-SPEC-2 section 3):
    #: only a history's emit generates it (ADR 0017 section 7), by the rows, one entity at a time.
    previous: bool = False
    #: Whether a dependency holds by construction: a history's parent layout gives every version
    #: of an entity one parent row, so the foreign key is a function of the entity. The layout
    #: sets the columns, the rule draws nothing and sets nothing, and it is still checked.
    by_layout: bool = False

    @property
    def determines(self) -> Tuple[str, ...]:
        """The columns this rule sets: a dependency's ``to`` columns, else its one column."""
        if self.by_layout:
            return ()
        if self.form == FORM_DEPENDENCY:
            return self.to_columns
        return () if self.column is None else (self.column,)


@dataclass(frozen=True)
class PlannedLink:
    """A conditional link of the emitted expose, as the plan records it (ADR 0022 section 5).

    ``when`` is the canonical predicate over the row's own columns, ``columns`` the relationship's
    ``fromColumns`` and ``reads`` the columns the predicate reads. ``after`` is how many of the
    plan's determined columns are set before this step: it runs once the columns it reads are set
    and before any rule that reads the link's columns.
    """

    relationship: str
    when: str
    columns: Tuple[str, ...]
    reads: Tuple[str, ...] = ()
    after: int = 0


@dataclass
class _LinkStep:
    """A compiled conditional link: the positions of its columns, and its predicate."""

    relationship: str
    positions: Tuple[int, ...]
    when: Checked


@dataclass
class _Dependency:
    """A dependency's compiled reads: where its ``from`` values sit in a row, and their kinds."""

    rule: PlannedRule
    positions: Tuple[int, ...]
    kinds: Tuple[str, ...]
    reference: str
    driver: Optional[Any] = None
    #: The rule's NULL-row share, each ``to`` column's chance of a NULL on a NULL row, in
    #: ``to`` order, and where this step's column sits in that order.
    null_rows: Optional[Decimal] = None
    null_given: Tuple[Decimal, ...] = ()
    slot: int = 0


@dataclass
class _Item:
    when: Optional[Checked]
    value: Optional[Checked] = None
    k: Optional[Checked] = None
    x: Optional[Checked] = None
    sign: int = 1
    #: A copy's sentinel as the column holds it, and the path whose first missing hop writes it.
    sentinel: Any = None
    has_sentinel: bool = False
    path: Tuple[str, ...] = ()


@dataclass
class _Step:
    column: str
    position: int
    column_type: ColumnType
    plan: "ColumnPlan"
    items: List[_Item] = field(default_factory=list)
    dependency: Optional[_Dependency] = None
    #: NULLs first (EXPR-SPEC-2 section 9): a column a rule determines through a link, and that
    #: declares a ``nullFraction``, keeps a NULL its own draw made.
    keeps_nulls: bool = False
    #: A lifetime's duration: the end is drawn from the row's own start (ADR 0024 section 4).
    duration: Optional["_DurationStep"] = None
    #: The ``within`` entries of a date column, drawn together (ADR 0024 section 5).
    within: Optional["_WithinStep"] = None
    #: A lifetime's end that is cut at the ends of the parents of its period (ADR 0024 section 5).
    period: Optional["_PeriodStep"] = None
    #: The seasonal rates that thin the column's date (ADR 0025 section 2), when it has an entry.
    rates: Optional[Rates] = None


@dataclass
class _PeriodStep:
    """A compiled period end: where the start sits in a row and the run's window in the end's units."""

    period: Period
    start_position: int
    window: Optional[Tuple[int, int]]


@dataclass
class _WithinStep:
    """A compiled date within linked lifetimes: its entries, nearest first, and the window in its units."""

    levels: Tuple[WithinLevel, ...]
    kind: str
    window: Optional[Tuple[int, int]]
    #: The date column's own declared range ``R`` in its units (ADR 0024 section 5).
    bounds: Optional[Tuple[int, int]] = None


@dataclass
class _DurationStep:
    """A compiled duration: where the start sits in a row and the run's window in the column's units."""

    duration: Duration
    start_position: int
    window: Optional[Tuple[int, int]]
    #: Where the dwell family's ``given`` column sits in a row and its FLUID SQL/1 kind (ADR 0025
    #: section 3), when the duration has one.
    given_position: Optional[int] = None
    given_kind: Optional[str] = None


@dataclass
class _Compiled:
    index: Dict[str, int]
    steps: Tuple[Union[_Step, _LinkStep], ...]


@dataclass(frozen=True)
class RulePlan:
    """The rules of one emitted expose and the order its determined columns are set in.

    The texts are kept as canonical strings and compiled once per process, so
    the plan pickles (a worker process receives it) and the compiled checkers,
    which hold closures, never travel. ``order`` lists the determined columns in
    generation order.
    """

    contract_id: Optional[str]
    expose_id: str
    model: Model
    rules: Tuple[PlannedRule, ...]
    order: Tuple[str, ...]
    #: The conditional links (kernel 0.2.0), each a step ordered with the rules.
    links: Tuple[PlannedLink, ...] = ()
    #: The seasonal rates of the emit's date columns, one entry per column (ADR 0025 section 2).
    rates: Tuple[Rates, ...] = ()
    #: The as-of links of the emit (kernel 0.2.0, ADR 0017 section 8): a path through the relationship
    #: reads the version of the linked entity valid on the row's date.
    asof: Tuple[AsOfEntry, ...] = ()
    #: The copies into a foreign key of another relationship (kernel 0.2.0, ADR 0022 section 4
    #: amendment of 2026-11-24): each sets that relationship's pick instead of a rule's cell.
    pick_copies: Tuple["PickCopy", ...] = ()
    _cache: Dict[str, Any] = field(default_factory=dict, init=False, repr=False, compare=False)

    def __getstate__(self) -> Dict[str, Any]:
        return {key: value for key, value in self.__dict__.items() if key != "_cache"}

    def __setstate__(self, state: Mapping[str, Any]) -> None:
        for key, value in state.items():
            object.__setattr__(self, key, value)
        object.__setattr__(self, "_cache", {})

    @property
    def determined(self) -> FrozenSet[str]:
        """The columns a rule sets."""
        return frozenset(self.order)

    def date_facts(self) -> Tuple[str, ...]:
        """The ids of the facts of ``lifetimes`` and ``within`` this plan generates (ADR 0024 section 7).

        A lifetime's fact is generated when its end has a duration; each ``within`` entry, a period
        included, is one. They are the ids ``verify`` names a fact by, without the expose.
        """
        facts = [_date_fact(rule) for rule in self.rules]
        return tuple(dict.fromkeys(fact.id for fact in facts if fact is not None))

    @property
    def has_rates(self) -> bool:
        """Whether a date column of the emit has seasonal rates, so its dates are drawn by thinning."""
        return bool(self.rates)

    @property
    def has_date_targets(self) -> bool:
        """Whether a fact of ``lifetimes`` or ``within`` carries a target above zero, so rows violate it."""
        return _has_date_targets(self.rules)

    @property
    def windowed(self) -> bool:
        """Whether a rule holds a window over siblings, so the expose is made whole."""
        return any(rule.windowed for rule in self.rules)

    @property
    def summarised(self) -> FrozenSet[str]:
        """The columns a summary over children sets, and every column a rule computes from one.

        A row of the expose holds none of them until its children are known (ADR 0016 section 5).
        """
        found = {rule.column for rule in self.rules if rule.summary is not None and rule.column}
        grew = bool(found)
        while grew:
            grew = False
            for rule in self.rules:
                new = set(rule.determines) - found
                if new and (set(rule.reads) | set(rule.from_columns)) & found:
                    found |= new
                    grew = True
        return frozenset(found)

    def link_reads(self) -> FrozenSet[Tuple[str, str]]:
        """The ``(exposeId, column)`` each rule reads through a link, in its text and its condition."""
        found: Set[Tuple[str, str]] = set()
        for rule in self.rules:
            if not rule.paths:
                continue
            texts = [rule.text] if rule.form == FORM_DERIVE else [rule.k, rule.x]
            checked = [self._checked(text, rule, value=True) for text in texts if text is not None]
            if rule.when is not None:
                checked.append(self._checked(rule.when, rule, value=False))
            found.update(
                (read.expose, read.column)
                for text in checked
                for read in text.reads
                if read.reach == "link" or (read.reach == "previous" and len(read.path) > 1)
            )
        return frozenset(found)

    def link_paths(self) -> FrozenSet[Tuple[Tuple[str, ...], str, str]]:
        """The ``(path, exposeId, column)`` each rule reads through a link, in its text and its condition.

        *path* is the relationships followed from the expose, or from its previous version, to the
        row the column is read on.
        """
        found: Set[Tuple[Tuple[str, ...], str, str]] = set()
        for rule in self.rules:
            if not rule.paths:
                continue
            texts = [rule.text] if rule.form == FORM_DERIVE else [rule.k, rule.x]
            checked = [self._checked(text, rule, value=True) for text in texts if text is not None]
            if rule.when is not None:
                checked.append(self._checked(rule.when, rule, value=False))
            found.update(
                (read.path[1:] if read.reach == "previous" else read.path, read.expose, read.column)
                for text in checked
                for read in text.reads
                if read.reach == "link" or (read.reach == "previous" and len(read.path) > 1)
            )
        return frozenset(found)

    def column_reads(self) -> Dict[str, FrozenSet[Tuple[str, str, Tuple[str, ...]]]]:
        """For each column a rule sets, the ``(exposeId, column, path)`` its rules read.

        *path* is empty for a column of the row itself, else the relationships followed to the row
        the column is read on (:meth:`link_paths`). A rule that sets several columns reads the
        same for each.
        """
        found: Dict[str, Set[Tuple[str, str, Tuple[str, ...]]]] = {}
        for rule in self.rules:
            reads: Set[Tuple[str, str, Tuple[str, ...]]] = {
                (self.expose_id, name, ()) for name in rule.reads
            }
            if rule.paths:
                texts = [rule.text] if rule.form == FORM_DERIVE else [rule.k, rule.x]
                checked = [
                    self._checked(text, rule, value=True) for text in texts if text is not None
                ]
                if rule.when is not None:
                    checked.append(self._checked(rule.when, rule, value=False))
                reads.update(
                    (
                        read.expose,
                        read.column,
                        read.path[1:] if read.reach == "previous" else read.path,
                    )
                    for text in checked
                    for read in text.reads
                    if read.reach == "link" or (read.reach == "previous" and len(read.path) > 1)
                )
            for column in rule.determines:
                found.setdefault(column, set()).update(reads)
        return {column: frozenset(reads) for column, reads in found.items()}

    def asof_date(self, relationship: str) -> Optional[str]:
        """The column that dates a read through *relationship*, or ``None`` when it has no as-of entry."""
        for entry in self.asof:
            if entry.relationship == relationship:
                return entry.date
        return None

    @property
    def has_previous(self) -> bool:
        """Whether a rule reads the entity's previous version."""
        return any(rule.previous for rule in self.rules)

    @property
    def covers_every_row(self) -> FrozenSet[str]:
        """The determined columns a ``derive`` without ``when`` sets on every row.

        Only these may declare no draw of their own. A column whose rules leave a
        row uncovered (every ``derive`` on it has a ``when``), or whose difference
        form can meet an empty interval, takes its own source on those rows
        (EXPR-SPEC-2 section 9), so it is planned like any other column.
        """
        return frozenset(
            rule.column
            for rule in self.rules
            if rule.column is not None
            and (
                (rule.form == FORM_DURATION and rule.duration is not None)
                or (rule.form == FORM_DERIVE and rule.when is None)
            )
        )

    def refuse_given_on_determined(self, plans: Sequence["ColumnPlan"]) -> None:
        """Refuse what a determined column cannot be in this version (``FLX-6210``).

        No column may be drawn ``given`` a determined one, and a column a difference form
        generates may not be drawn ``given`` another: whether a solved date has weight
        would then depend on the level the other column drew in that row.
        """
        determined = self.determined
        solved = {
            rule.column
            for rule in self.rules
            if rule.column and rule.form not in (FORM_DERIVE, FORM_DURATION, FORM_WITHIN)
        }
        for column_plan in plans:
            parents = column_plan.statistics.parents
            if parents and column_plan.column.name in solved:
                raise Expr2Error(
                    FLX_6210,
                    f"column {column_plan.column.name!r} is generated by a difference form and "
                    f"drawn given {', '.join(repr(name) for name in parents)}; this version does "
                    "not weigh a solved date by the level another column drew",
                )
            for given in parents:
                if given in determined:
                    raise Expr2Error(
                        FLX_6210,
                        f"column {column_plan.column.name!r} is drawn given {given!r}, which a "
                        "rule determines; this version does not generate a determined column "
                        "that another is drawn given",
                    )
        self._refuse_undrawable_targets(plans)
        self._refuse_candidateless_targets(plans)

    def _refuse_candidateless_targets(self, plans: Sequence["ColumnPlan"]) -> None:
        """Refuse a target whose violating rows have no statistics to draw candidates from (ADR 0024 section 7).

        A violating row draws its candidates from the column's own statistics: a column that
        declares none (a lifetime's end that only its duration draws) has nothing to draw
        (``FLX-6205``, as EXPR-SPEC-2 section 7 refuses a target with no statistics), and one drawn
        ``given`` another would need that column's level in the row (``FLX-6210``).
        """
        by_name = {column_plan.column.name: column_plan for column_plan in plans}
        for rule in self.rules:
            fact = _date_fact(rule)
            if fact is None or rule.column is None or not positive_target(fact.target):
                continue
            column_plan = by_name[rule.column]
            if column_plan.family is None:
                raise Expr2Error(
                    FLX_6205,
                    f"rule {rule.id!r} declares a target, and column {rule.column!r} declares no "
                    "statistics its violating rows could be drawn from; declare a range or "
                    "bins for it",
                )
            if column_plan.statistics.parents:
                raise Expr2Error(
                    FLX_6210,
                    f"rule {rule.id!r} declares a target, and column {rule.column!r} is drawn "
                    "given another column; this version draws a violating row's candidates "
                    "without a given level",
                )

    def _refuse_undrawable_targets(self, plans: Sequence["ColumnPlan"]) -> None:
        """Refuse a dependency's ``to`` column that has nothing to draw on its own stream (``FLX-6210``).

        An identifier is a function of the row index, a column drawn ``given`` another needs
        that column's level in the row, and a column that is not a foreign key and declares no
        draw has no statistics: none can be drawn once per key.
        """
        from flux_engine.generator.families import ColumnType as _PlannedType

        by_name = {column_plan.column.name: column_plan for column_plan in plans}
        for rule in self.rules:
            if rule.form != FORM_DEPENDENCY or rule.by_layout:
                continue
            for column in rule.to_columns:
                column_plan = by_name[column]
                if column_plan.column_type is _PlannedType.IDENTIFIER:
                    reason = "is an identifier, a function of the row index"
                elif column_plan.statistics.parents:
                    reason = "is drawn given " + ", ".join(
                        repr(name) for name in column_plan.statistics.parents
                    )
                elif column_plan.family is None and column not in rule.foreign_columns:
                    reason = "declares no draw and is not a foreign key"
                else:
                    continue
                raise Expr2Error(
                    FLX_6210,
                    f"dependency {rule.id!r} determines {column!r}, which {reason}; this version "
                    "draws a 'to' column once per key from the column's own declared statistics",
                )

    def rules_of(self, column: str) -> Tuple[PlannedRule, ...]:
        """The rules that determine *column*, in declaration order."""
        return tuple(rule for rule in self.rules if column in rule.determines)

    def checks(self) -> Tuple[PlannedRule, ...]:
        """The rules that only check: they set no column, and are not a dependency.

        An expose whose values a generator plugin makes is held to these on every row it covers
        (:func:`flux_engine.generator.plugin.check_rows`).
        """
        return tuple(
            rule
            for rule in self.rules
            if rule.column is None and rule.form not in (FORM_DERIVE, FORM_DEPENDENCY)
        )

    def predicate_of(self, rule: PlannedRule) -> Tuple[Checked, Optional[Checked]]:
        """A check's predicate, and the predicate of its ``when``, compiled to evaluate on a row."""
        when = None if rule.when is None else self._checked(rule.when, rule, value=False)
        return self._checked(rule.text, rule, value=False), when

    @staticmethod
    def _rule_document(rule: PlannedRule, position: Mapping[str, int]) -> Dict[str, Any]:
        if rule.period is not None or rule.within is not None or rule.duration is not None:
            return RulePlan._dated_document(rule, position)
        if rule.form == FORM_DEPENDENCY:
            return {
                "id": rule.id,
                "form": rule.form,
                "from": list(rule.from_columns),
                "to": list(rule.to_columns),
                "band": None if rule.band is None else rule.band.as_document(),
                "order": [position[column] for column in rule.to_columns if column in position],
                "origin": rule.origin,
                "nullRows": None if rule.null_rows is None else str(rule.null_rows),
                **({"byLayout": True} if rule.by_layout else {}),
            }
        return RulePlan._text_document(rule, position)

    @staticmethod
    def _dated_document(rule: PlannedRule, position: Mapping[str, int]) -> Dict[str, Any]:
        """The document of a rule a period, a within entry or a duration sets.

        Only an emit that declares one lists it, so a plan with none keeps every byte it had.
        """
        assert rule.column is not None
        document: Dict[str, Any] = {
            "id": rule.id,
            "form": rule.form,
            "column": rule.column,
            "order": position[rule.column],
        }
        if rule.period is not None:
            document["period"] = rule.period.as_document()
            if rule.duration is not None:
                document["lifetime"] = rule.duration.as_document()
        elif rule.within is not None:
            document["within"] = rule.within.as_document()
        elif rule.duration is not None:
            document["lifetime"] = rule.duration.as_document()
        return document

    @staticmethod
    def _text_document(rule: PlannedRule, position: Mapping[str, int]) -> Dict[str, Any]:
        """The document of a rule written as a text."""
        document: Dict[str, Any] = {
            "id": rule.id,
            "form": rule.form,
            "text": rule.text,
            "column": rule.column,
            "when": rule.when,
            "band": None if rule.band is None else rule.band.as_document(),
            "order": None if rule.column is None else position[rule.column],
        }
        if rule.paths:
            # Only a rule that reads through a link lists its paths, so a plan with none keeps
            # every byte it had.
            document["paths"] = [list(path) for path in rule.paths]
        if rule.previous:
            document["previous"] = True
        if rule.sentinel is not None:
            document["sentinel"] = rule.sentinel
        if rule.origin == ORIGIN_SHARED:
            # Only a copy says so, and a chain of links says it is read as a FLUX-side text, so a
            # plan with none keeps every byte it had.
            document["origin"] = rule.origin
            if rule.language is not None:
                document["language"] = rule.language
        elif rule.origin != ORIGIN_CONTRACT:
            # Only a FLUX-side rule says where it came from and in which language its text
            # is read, so a plan with none keeps every byte it had.
            document["origin"] = rule.origin
            document["language"] = rule.language
        return document

    def as_document(self) -> Dict[str, Any]:
        """The rules as canonical-ready JSON, for ``generatorDigest``."""
        position = {column: index for index, column in enumerate(self.order)}
        document: Dict[str, Any] = {
            "contractId": self.contract_id,
            "rules": [self._rule_document(rule, position) for rule in self.rules],
        }
        if self.links:
            # Only a plan with a conditional link lists them, so a plan with none keeps every
            # byte it had.
            document["links"] = [
                {"relationship": link.relationship, "when": link.when, "order": link.after}
                for link in self.links
            ]
        if self.rates:
            # Only an emit that declares seasonal rates lists them, so a plan with none keeps
            # every byte it had.
            document["rates"] = [rates.as_document() for rates in self.rates]
        if self.asof:
            # Only a plan with an as-of link lists them, so a plan with none keeps every byte it had.
            document["asOf"] = [entry.as_member() for entry in self.asof]
        return document

    # ------------------------------------------------------------ compiling

    def _compiled(self, plan: "GeneratorPlan") -> _Compiled:
        # The steps read the plan's column statistics, so they are compiled per plan: a
        # RulePlan handed to a second build_plan compiles again rather than reuse the first.
        cached = self._cache.get("compiled")
        if cached is not None and cached[0] is plan:
            compiled_for_plan = cached[1]
            assert isinstance(compiled_for_plan, _Compiled)
            return compiled_for_plan
        index = {column_plan.column.name: i for i, column_plan in enumerate(plan.columns)}
        steps: List[Union[_Step, _LinkStep]] = []
        for slot, column in enumerate(self.order):
            steps.extend(self._link_steps(slot, index))
            steps.append(self._column_step(column, index, plan))
        steps.extend(self._link_steps(len(self.order), index))
        compiled = _Compiled(index, tuple(steps))
        self._cache["compiled"] = (plan, compiled)
        return compiled

    def _column_step(self, column: str, index: Mapping[str, int], plan: "GeneratorPlan") -> _Step:
        """The compiled step of one determined *column*, with its rules' items and dependency."""
        column_plan = plan.column_plan(column)
        step = _Step(
            column=column,
            position=index[column],
            column_type=self.model.exposes[self.expose_id].columns[column],
            plan=column_plan,
        )
        within_levels: List[WithinLevel] = []
        for rule in self.rules_of(column):
            if rule.summary is not None and not plan.sums:
                continue  # the copy of a parent a child reads: no child plan to summarise
            if rule.form == FORM_DEPENDENCY:
                step.dependency = self._dependency_step(rule, column, index, plan)
            elif rule.within is not None:
                within_levels.append(rule.within)
                # NULLs first (EXPR-SPEC-2 section 9): the date's own NULL stands.
                step.keeps_nulls = column_plan.statistics.null_fraction is not None
            elif rule.duration is not None or rule.period is not None:
                self._lifetime_steps(step, rule, index, plan)
                # NULLs first (EXPR-SPEC-2 section 9): the end's own NULL stands.
                step.keeps_nulls = column_plan.statistics.null_fraction is not None
            else:
                step.items.append(self._item(rule, column))
                if rule.paths and column_plan.statistics.null_fraction is not None:
                    step.keeps_nulls = True
        if within_levels:
            ordered = tuple(sorted(within_levels, key=lambda level: len(level.relationship)))
            step.within = _WithinStep(
                ordered,
                ordered[0].kind,
                window_in_units(ordered[0].kind, plan.time_bounds),
                ordered[0].bounds,
            )
        step.rates = next((rates for rates in self.rates if rates.column == column), None)
        return step

    def _lifetime_steps(
        self, step: _Step, rule: PlannedRule, index: Mapping[str, int], plan: "GeneratorPlan"
    ) -> None:
        """Set *step*'s duration and period from *rule*, each as the plan's window and columns read them."""
        if rule.duration is not None:
            given = rule.duration.given
            columns = self.model.exposes[self.expose_id].columns
            given_kind = None if given is None else columns[given].kind
            step.duration = _DurationStep(
                rule.duration,
                index[rule.duration.start],
                window_in_units(rule.duration.kind, plan.time_bounds),
                None if given is None else index[given],
                None if given_kind is None else given_kind.value,
            )
        if rule.period is not None:
            step.period = _PeriodStep(
                rule.period,
                index[rule.period.start],
                window_in_units(rule.period.kind, plan.time_bounds),
            )

    def _link_steps(self, slot: int, index: Mapping[str, int]) -> List[_LinkStep]:
        """The conditional links set once *slot* determined columns are, as compiled steps."""
        steps: List[_LinkStep] = []
        for link in self.links:
            if link.after != slot:
                continue
            checked = check(
                parse(link.when, side=Side.CORE), self.model, self.expose_id, position=PREDICATE
            )
            steps.append(
                _LinkStep(link.relationship, tuple(index[name] for name in link.columns), checked)
            )
        return steps

    def _dependency_step(
        self, rule: PlannedRule, column: str, index: Mapping[str, int], plan: "GeneratorPlan"
    ) -> _Dependency:
        columns = self.model.exposes[self.expose_id].columns
        kinds: List[str] = []
        for name in rule.from_columns:
            kind = columns[name].kind
            assert kind is not None  # _Reader._dependency refuses an untyped column
            kinds.append(kind.value)
        driver = None
        if column in rule.foreign_columns:
            driver = None if plan.relations is None else plan.relations.driver(column)
            from flux_engine.generator.relations import ForeignKey

            if not isinstance(driver, ForeignKey):
                raise Expr2Error(
                    FLX_6210,
                    f"dependency {rule.id!r} determines {column!r}, a foreign key, but the plan "
                    "relates no foreign key to it; relate the plan before generating its rows",
                )
        null_given: Tuple[Decimal, ...] = ()
        if rule.null_rows is not None:
            null_given = tuple(
                _given_a_null_row(plan.column_plan(name).statistics.null_fraction, rule.null_rows)
                for name in rule.to_columns
            )
        return _Dependency(
            rule=rule,
            positions=tuple(index[name] for name in rule.from_columns),
            kinds=tuple(kinds),
            reference=rule_reference(self.contract_id, rule.id),
            driver=driver,
            null_rows=rule.null_rows,
            null_given=null_given,
            slot=rule.to_columns.index(column),
        )

    def _checked(
        self, text: str, rule: PlannedRule, *, value: bool, target: Optional[str] = None
    ) -> Checked:
        if rule.origin == ORIGIN_BUNDLE:
            assert rule.language is not None
            parsed = parse(text, side=Side.FLUX, language=rule.language)
        elif rule.summary is not None:
            parsed = parse(text, side=Side.FLUX)  # a summary is written in the FLUX-side text
        elif rule.origin == ORIGIN_SHARED:
            parsed = parse(text, side=Side.CORE if rule.language is None else Side.FLUX)
        else:
            parsed = parse(text, side=Side.CORE)
        return check(
            parsed,
            self.model,
            self.expose_id,
            position=VALUE if value else PREDICATE,
            target=target,
        )

    def _item(self, rule: PlannedRule, column: str) -> _Item:
        when = None if rule.when is None else self._checked(rule.when, rule, value=False)
        if rule.form == FORM_DERIVE:
            item = _Item(when, value=self._checked(rule.text, rule, value=True, target=column))
            if rule.sentinel is not None:
                item.has_sentinel = True
                item.path = rule.paths[0]
                item.sentinel = sentinel_value(
                    rule.sentinel, self.model.exposes[self.expose_id].columns[column], column
                )
            return item
        assert rule.k is not None and rule.x is not None
        return _Item(
            when,
            k=self._checked(rule.k, rule, value=True),
            x=self._checked(rule.x, rule, value=True),
            sign=rule.sign,
        )


# ---------------------------------------------------------------------------
# Reading the rules of an emit
# ---------------------------------------------------------------------------


def _invariants(entry: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    contract = entry.get("contract")
    raw = contract.get("invariants") if isinstance(contract, Mapping) else None
    if raw is None:
        return []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("contract.invariants is a list")
    found: List[Mapping[str, Any]] = []
    for index, invariant in enumerate(raw):
        if not isinstance(invariant, Mapping):
            raise ValueError(f"contract.invariants[{index}] is an object")
        found.append(invariant)
    return found


def flux_rule_entries(raw: Any, *, where: str) -> List[Any]:
    """The FLUX-side rule entries an emit declares under ``rules``, as declared.

    ``None`` is none declared. Anything but a list is ``FLX-3105``; what each entry holds is
    checked where the entry is read (:meth:`_Reader.flux_rule`).
    """
    if raw is None:
        return []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise FluxError(FLX_3105, f"{where}: rules is {type(raw).__name__}, not a list")
    return list(raw)


def _settings_for(settings: Any, ids: Sequence[str], *, where: str) -> Dict[str, Mapping[str, Any]]:
    """The FLUX settings of the emit's rules, checked against the ids that exist (``FLX-3105``)."""
    if settings is None:
        return {}
    if not isinstance(settings, Mapping):
        raise FluxError(
            FLX_3105, f"{where}: invariants is {type(settings).__name__}, not a mapping"
        )
    known = set(ids)
    found: Dict[str, Mapping[str, Any]] = {}
    for raw_id, setting in settings.items():
        rule_id = str(raw_id)
        if rule_id not in known:
            raise FluxError(
                FLX_3105,
                f"{where}: invariants[{rule_id!r}] names no invariant of this expose; "
                f"it declares {sorted(known)}",
            )
        if not isinstance(setting, Mapping):
            raise FluxError(
                FLX_3105,
                f"{where}: invariants[{rule_id!r}] is {type(setting).__name__}, not a mapping",
            )
        unknown = sorted(str(key) for key in setting if key not in _SETTING_KEYS)
        if unknown:
            raise FluxError(
                FLX_3105,
                f"{where}: invariants[{rule_id!r}] declares {unknown}; a rule's FLUX setting "
                f"carries {sorted(_SETTING_KEYS)}",
            )
        generates = setting.get("generates")
        if generates is not None and (not isinstance(generates, str) or not generates):
            raise FluxError(
                FLX_3105, f"{where}: invariants[{rule_id!r}].generates is a column name"
            )
        found[rule_id] = setting
    return found


def _is_column(node: Node, name: str) -> bool:
    return isinstance(node, Column) and node.relationship is None and nfc(node.name) == name


def _difference(tree: Node) -> Optional[Tuple[str, Call, Node]]:
    """``(unit, the difference call, the other side)`` of ``k = DIFF(a, b)``, either way round."""
    if not isinstance(tree, Compare) or tree.op != "=":
        return None
    sides = (tree.left, tree.right)
    calls = [
        i for i, side in enumerate(sides) if isinstance(side, Call) and side.name in _DIFFERENCES
    ]
    if len(calls) != 1:
        return None
    call = sides[calls[0]]
    assert isinstance(call, Call)
    return _DIFFERENCES[call.name], call, sides[1 - calls[0]]


def _bounds_leaves(tree: Node) -> Optional[List[Compare]]:
    """The comparisons of a bounds-shaped check, or ``None``: one comparison, or two joined by AND."""
    if isinstance(tree, And):
        left, right = _bounds_leaves(tree.left), _bounds_leaves(tree.right)
        if left is None or right is None or len(left) + len(right) != 2:
            return None
        return left + right
    if isinstance(tree, Compare) and tree.op in _ORDER_OPERATORS:
        return [tree]
    return None


def _check_form(tree: Node) -> str:
    difference = _difference(tree)
    if difference is not None:
        return f"check-difference-{difference[0]}"
    return FORM_BOUNDS if _bounds_leaves(tree) is not None else FORM_OTHER


def _mentions(node: Node, name: str) -> bool:
    if _is_column(node, name):
        return True
    return any(_mentions(part, name) for part in subtrees(node))


def _own_columns(checked: Checked) -> Tuple[Tuple[str, ...], bool]:
    """The columns a text reads on its own row, and whether it reads anything beyond the row."""
    own = sorted({read.column for read in checked.reads if read.reach == "row"})
    beyond = any(read.reach != "row" for read in checked.reads)
    return tuple(own), beyond


def _names_a_collection(node: Node) -> bool:
    """Whether *node* holds an aggregate or a children collection, which no link reaches."""
    if isinstance(node, (Aggregate, Children)):
        return True
    return any(_names_a_collection(part) for part in subtrees(node))


def _windows_of(node: Node) -> List[Window]:
    """Every window *node* holds, in the order the text writes them."""
    found: List[Window] = [node] if isinstance(node, Window) else []
    for part in subtrees(node):
        found.extend(_windows_of(part))
    return found


def _sort_read(read: Any, own: Set[str], paths: Set[Tuple[str, ...]], previous: bool) -> bool:
    """File *read* under the row's own columns or the paths; False when it reads more than that."""
    if read.reach == "row":
        own.add(read.column)
    elif read.reach == "link":
        paths.add(read.path)
    elif read.reach == "siblings":
        through = read.path[1:]  # path[0] is the partition's relationship
        if through:
            paths.add(through)
        else:
            own.add(read.column)
    elif read.reach == "previous" and previous:
        if len(read.path) > 1:
            paths.add(read.path[1:])
    else:
        return False
    return True


def _split_reads(
    checked: Optional[Checked], *, previous: bool = False
) -> Tuple[Tuple[str, ...], Tuple[Tuple[str, ...], ...], bool]:
    """What a text reads: its own row's columns, the paths through links, and whether it reads more.

    A read inside a window is a read of a sibling (EXPR-SPEC-2 section 6): the sibling's own
    column is one of the row's own columns, since the siblings are rows of the same expose, and
    its path through a link is a path of the row's. *More* is a read of the previous version or of
    children, or an aggregate that reads no column at all: none of them generates. On a history's
    emit (*previous*) a read of the previous version generates: its column is the previous row's,
    and a path through a link after ``previous`` is a path of the previous row's.
    """
    if checked is None:
        return (), (), False
    own: Set[str] = set()
    paths: Set[Tuple[str, ...]] = set()
    beyond = False
    for read in checked.reads:
        if not _sort_read(read, own, paths, previous):
            beyond = True
    return (
        tuple(sorted(own)),
        tuple(sorted(paths)),
        beyond or _names_a_collection(checked.parsed.tree),
    )


def _reads_previous(*texts: Optional[Checked]) -> bool:
    """Whether any of *texts* reads the entity's previous version."""
    return any(
        read.reach == "previous"
        for checked in texts
        if checked is not None
        for read in checked.reads
    )


def _is_whole_path(tree: Node) -> bool:
    """Whether *tree* is one column read through a link (``account.region``, ``a.b.region``)."""
    if isinstance(tree, Column):
        return tree.relationship is not None
    return isinstance(tree, Path) and tree.root != "previous"


def _canonical_of(node: Node, model: Model, expose: str) -> str:
    return unparse(node, flux_side=False, columns=frozenset(model.exposes[expose].columns))


def _flux_entry_shape(entry: Mapping[str, Any], where: str) -> None:
    """Refuse a FLUX-side entry whose keys or value types are not the shape of §1 (``FLX-3105``)."""
    bad = [key for key in entry if not isinstance(key, str)]
    unknown = sorted(str(key) for key in entry if key not in _FLUX_RULE_KEYS)
    if bad or unknown:
        raise FluxError(
            FLX_3105,
            f"{where} declares {unknown or [str(key) for key in bad]}; a FLUX-side rule carries "
            f"{sorted(_FLUX_RULE_KEYS)}",
        )
    if ("check" in entry) == ("derive" in entry):
        raise FluxError(FLX_3105, f"{where} has exactly one of 'check', and 'derive' with 'expr'")
    if "derive" in entry and "expr" not in entry:
        raise FluxError(FLX_3105, f"{where} has a 'derive' and no 'expr'")
    if "check" in entry and "expr" in entry:
        raise FluxError(FLX_3105, f"{where} has an 'expr' and no 'derive'")
    for key in ("check", "derive", "expr", "when"):
        if key in entry and (not isinstance(entry[key], str) or not entry[key]):
            raise FluxError(FLX_3105, f"{where}: {key!r} is a non-empty text")


def _flux_band(entry: Mapping[str, Any], where: str) -> Optional[Band]:
    try:
        return Band.read(entry, where)
    except ValueError as error:
        raise FluxError(FLX_3105, str(error)) from None


class _Reader:
    """Reads one emit's rules into :class:`PlannedRule` objects, refusing what cannot generate."""

    def __init__(
        self,
        model: Model,
        expose_id: str,
        settings: Mapping[str, Mapping[str, Any]],
        keys: Optional[ExposeKeys] = None,
        related: FrozenSet[str] = frozenset(),
        counter_based: bool = False,
        counted: FrozenSet[Tuple[str, str]] = frozenset(),
        history: bool = False,
    ) -> None:
        self.model = model
        self.expose_id = expose_id
        self.settings = settings
        self.keys = keys
        self.related = related
        self.counter_based = counter_based
        self.counted = counted
        self.history = history
        self.columns = model.exposes[expose_id].columns

    def _links_generate(self, paths: Sequence[Tuple[str, ...]]) -> bool:
        """Whether a rule that reads *paths* generates: only kernel 0.2.0 reads through a link."""
        return not paths or self.counter_based

    def _first_columns(self, paths: Sequence[Tuple[str, ...]]) -> Tuple[str, ...]:
        """The ``fromColumns`` of each path's first relationship, which the rule reads as well."""
        links = self.model.exposes[self.expose_id].links
        return tuple(sorted({name for path in paths for name in links[path[0]].from_columns}))

    def _predicate(self, text: Any, where: str) -> Checked:
        if not isinstance(text, str):
            raise ValueError(f"{where} is a FLUID SQL/1 text")
        return check(parse(text, side=Side.CORE), self.model, self.expose_id, position=PREDICATE)

    def rule(self, invariant: Mapping[str, Any]) -> PlannedRule:
        rule_id = invariant["id"]
        where = f"rule {rule_id!r}"
        forms = [key for key in ("check", "derive", "dependency") if key in invariant]
        if len(forms) != 1:
            raise ValueError(f"{where} has exactly one of check, derive and dependency")
        generates = self.settings.get(rule_id, {}).get("generates")
        band = Band.read(invariant, where)
        if "dependency" in invariant:
            if generates is not None:
                raise Expr2Error(
                    FLX_6202, f"{where} is a dependency, and a FLUX setting generates {generates!r}"
                )
            return self._dependency(invariant, where, band)
        when = None
        if invariant.get("when") is not None:
            when = self._predicate(invariant["when"], f"{where} when")
        if "derive" in invariant:
            return self._derive(invariant, where, generates, when, band)
        return self._check(invariant, where, generates, when, band)

    def shared_rule(self, copy: "_Copy") -> PlannedRule:
        """The copy rule of one ``sharedColumns`` entry or ``sharedAttribute`` member.

        It is ``derive: <column>`` with the expression ``<path>.<column read>``, with no ``when``,
        so it covers every row; a row whose path reaches no row takes NULL (EXPR-SPEC-2 sections 6
        and 9). One hop is a core text, exactly the contract's own ``derive``; a chain of links is
        read as a FLUX-side text (``flux-expr/2``).
        """
        language = copy.language
        checked = check(
            parse(copy.text, side=Side.CORE if language is None else Side.FLUX),
            self.model,
            self.expose_id,
            position=VALUE,
            target=copy.column,
        )
        own, paths, _ = _split_reads(checked)
        return PlannedRule(
            id=copy.rule_id,
            form=FORM_DERIVE,
            text=checked.canonical,
            column=copy.column,
            reads=tuple(sorted(set(own) | set(self._first_columns(paths)))),
            origin=ORIGIN_SHARED,
            language=language,
            paths=paths,
            whole_path=True,
            source=copy.source,
            blame=copy.blame,
        )

    def _column_list(self, raw: Any, where: str, side: str) -> Tuple[str, ...]:
        if (
            not isinstance(raw, Sequence)
            or isinstance(raw, (str, bytes))
            or not raw
            or not all(isinstance(name, str) and name for name in raw)
        ):
            raise ValueError(f"{where}: dependency.{side} is a non-empty list of column names")
        names = tuple(nfc(name) for name in raw)
        if len(set(names)) != len(names):
            raise ValueError(f"{where}: dependency.{side} names a column twice")
        for name in names:
            if name not in self.columns:
                raise Expr2Error(
                    FLX_6113,
                    f"{where}: dependency.{side} names {name!r}, which is not a column of "
                    f"{self.expose_id}",
                )
        return names

    def _dependency(
        self, invariant: Mapping[str, Any], where: str, band: Optional[Band]
    ) -> PlannedRule:
        """One contract ``dependency`` (EXPR-SPEC-2 section 8), or the refusal it earns."""
        if "when" in invariant:
            raise ValueError(f"{where} is a dependency, which has no 'when'")
        raw = invariant["dependency"]
        if not isinstance(raw, Mapping) or sorted(
            str(key) for key in raw if key not in _DEPENDENCY_KEYS
        ):
            raise ValueError(f"{where}: dependency is {{from, to, bothWays}}")
        sources = self._column_list(raw.get("from"), where, "from")
        targets = self._column_list(raw.get("to"), where, "to")
        if set(sources) & set(targets):
            raise ValueError(f"{where}: a column is on both sides of the dependency")
        both_ways = raw.get("bothWays", False)
        if not isinstance(both_ways, bool):
            raise ValueError(f"{where}: dependency.bothWays is true or false")
        for name in sources:
            if self.columns[name].kind is None:
                raise Expr2Error(
                    FLX_6111,
                    f"{where}: dependency.from names {name!r}, whose declared type "
                    f"{self.columns[name].declared!r} has no FLUID SQL/1 type, so it has no key",
                )
        if both_ways:
            raise Expr2Error(
                FLX_6210,
                f"{where} is a dependency with bothWays; this version generates a dependency one way",
            )
        # Whether the NULLs are drawn per key or per row, and on how many rows, plan_rules
        # decides once it has the declared row count and null fractions.
        foreign = tuple(name for name in targets if self._refuse_target(where, name))
        return PlannedRule(
            id=invariant["id"],
            form=FORM_DEPENDENCY,
            text=f"{', '.join(sources)} -> {', '.join(targets)}",
            band=band,
            reads=tuple(sorted(sources)),
            from_columns=sources,
            to_columns=targets,
            foreign_columns=foreign,
        )

    def _refuse_target(self, where: str, column: str) -> bool:
        """Whether *column* is a foreign key a dependency may set; ``FLX-6210`` if it is a key it may not.

        A ``to`` column that is a single-column foreign key draws the foreign key's own
        statistics on the dependency's stream (EXPR-SPEC-2 section 8). A key column, a column
        of a composite foreign key and a one-to-one foreign key are refused.
        """
        keys = self.keys
        if keys is None:
            if column in self.related:
                raise Expr2Error(
                    FLX_6210,
                    f"{where}: 'to' column {column!r} is a key or foreign key; this version "
                    "does not generate one",
                )
            return False
        owners = [r for r in keys.relationships if column in r.from_columns]
        for relationship in owners:
            if len(relationship.from_columns) > 1:
                raise Expr2Error(
                    FLX_6210,
                    f"{where}: 'to' column {column!r} is part of the composite foreign key "
                    f"{relationship.name!r}; this version generates a single-column foreign key",
                )
            if relationship.cardinality == "one_to_one":
                raise Expr2Error(
                    FLX_6210,
                    f"{where}: 'to' column {column!r} is a one_to_one foreign key "
                    f"({relationship.name!r}), which references each parent row once; this "
                    "version does not generate one",
                )
        key_columns = {name for key in (*keys.keys, *keys.instant_keys) for name in key}
        key_columns |= {e.column for e in keys.entities if e.kind in ("primary", "unique")}
        if column in key_columns:
            raise Expr2Error(
                FLX_6210,
                f"{where}: 'to' column {column!r} is a key column of {self.expose_id}; this "
                "version does not generate one",
            )
        return bool(owners)

    def flux_rule(self, entry: Mapping[str, Any]) -> PlannedRule:
        """One FLUX-side rule of the emit (EXPR-SPEC-2 §1), or the refusal it earns.

        ``derive`` generates when every read it makes is the row's own; a ``check``
        generates nothing. A malformed entry is ``FLX-3105``: the ``rules`` block has no
        schema of its own.
        """
        check_language(entry, where=f"FLUX-side rule {entry.get('id')!r}")
        where = f"FLUX-side rule {entry.get('id')!r}"
        _flux_entry_shape(entry, where)
        band = _flux_band(entry, where)
        if "derive" in entry:
            entry = {**entry, "derive": nfc(entry["derive"])}
        flux_rule = check_flux_side_rule(entry, self.model, self.expose_id)
        language = entry["language"]
        when = flux_rule.when
        when_text = None if when is None else when.canonical
        if flux_rule.derive is None:
            if "generates" in entry:
                raise Expr2Error(
                    FLX_6210,
                    f"{where} is a check with 'generates'; this version generates only from a "
                    "FLUX-side derive, and a later step generates from a FLUX-side check",
                )
            return PlannedRule(
                id=flux_rule.id,
                form=_check_form(flux_rule.value.parsed.tree),
                text=flux_rule.value.canonical,
                when=when_text,
                band=band,
                origin=ORIGIN_BUNDLE,
                language=language,
            )
        if "generates" in entry:
            raise Expr2Error(FLX_6202, f"{where} is a derive, and it declares 'generates'")
        column = flux_rule.derive
        own, paths, partitions = self._own_row_reads(flux_rule.value, when, column, where)
        # A path after ``previous`` leaves the previous row's columns, not this row's.
        own_paths = tuple(
            sorted(set(_split_reads(flux_rule.value)[1]) | set(_split_reads(when)[1]))
        )
        return PlannedRule(
            id=flux_rule.id,
            form=FORM_DERIVE,
            text=flux_rule.value.canonical,
            column=column,
            when=when_text,
            band=band,
            reads=tuple(sorted(set(own) | set(self._first_columns(own_paths)) | set(partitions))),
            origin=ORIGIN_BUNDLE,
            language=language,
            paths=paths,
            whole_path=_is_whole_path(flux_rule.value.parsed.tree),
            windowed=bool(partitions),
            previous=_reads_previous(flux_rule.value, when),
        )

    def _own_row_reads(
        self, value: Checked, when: Optional[Checked], column: str, where: str
    ) -> Tuple[Tuple[str, ...], Tuple[Tuple[str, ...], ...], Tuple[str, ...]]:
        """The row's own columns a FLUX-side derive reads, its paths through links, and its partitions.

        A read of the previous version or of children is refused, and so is a path or a window
        under kernel 0.1.0. The third item is the columns the windows' partitions join on, which
        the rule reads as well; it is empty for a rule with no window.
        """
        own, paths, beyond = _split_reads(value, previous=self.history)
        when_own, when_paths, when_beyond = _split_reads(when, previous=self.history)
        if beyond or when_beyond:
            raise Expr2Error(
                FLX_6210,
                f"{where} derives {column!r} from another row (previous, a path, children or "
                "siblings); this version generates a FLUX-side derive that reads the row's own "
                "columns. A summary over children generates as a contract `derive` with "
                "`aggregate` over a relationship laid out with a counts entry, not as a "
                "FLUX-side text",
            )
        windows = _windows_of(value.parsed.tree)
        if when is not None:
            windows += _windows_of(when.parsed.tree)
        found = tuple(sorted(set(paths) | set(when_paths)))
        if windows and _reads_previous(value, when):
            raise Expr2Error(
                FLX_6210,
                f"{where} derives {column!r} from the previous version and over its siblings; "
                "this version generates a read of the previous version on its own",
            )
        if (windows or found) and not self.counter_based:
            raise Expr2Error(
                FLX_6210,
                f"{where} derives {column!r} from another row (previous, a path, children or "
                "siblings); this version generates a FLUX-side derive that reads the row's own "
                "columns. Kernel 0.2.0 generates one that reads through a link"
                + (" or over its siblings" if windows else ""),
            )
        links = self.model.exposes[self.expose_id].links
        partitions: Set[str] = set()
        for window in windows:
            partitions.update(links[window.partition].from_columns)
        return tuple(sorted(set(own) | set(when_own))), found, tuple(sorted(partitions))

    def _derive(
        self,
        invariant: Mapping[str, Any],
        where: str,
        generates: Optional[str],
        when: Optional[Checked],
        band: Optional[Band],
    ) -> PlannedRule:
        if generates is not None:
            raise Expr2Error(
                FLX_6202, f"{where} is a derive, and a FLUX setting generates {generates!r}"
            )
        has_expr, has_aggregate = "expr" in invariant, "aggregate" in invariant
        if has_expr == has_aggregate:
            raise ValueError(f"{where} has exactly one of expr and aggregate with derive")
        column = nfc(str(invariant["derive"]))
        if has_aggregate:
            return self._summary(invariant, where, column, when, band)
        expr = invariant["expr"]
        if not isinstance(expr, str):
            raise ValueError(f"{where} expr is a FLUID SQL/1 text")
        checked = check(
            parse(expr, side=Side.CORE),
            self.model,
            self.expose_id,
            position=VALUE,
            target=column,
        )
        own, paths, beyond = _split_reads(checked)
        when_own, when_paths, when_beyond = _split_reads(when)
        if beyond or when_beyond:
            raise Expr2Error(
                FLX_6210,
                f"{where} derives {column!r} from another row (a link, previous, children or "
                "siblings); this version generates a derive that reads the row's own columns",
            )
        found = tuple(sorted(set(paths) | set(when_paths)))
        if not self._links_generate(found):
            raise Expr2Error(
                FLX_6210,
                f"{where} derives {column!r} from another row (a link, previous, children or "
                "siblings); this version generates a derive that reads the row's own columns. "
                "Kernel 0.2.0 generates one that reads through a link",
            )
        return PlannedRule(
            id=invariant["id"],
            form=FORM_DERIVE,
            text=checked.canonical,
            column=column,
            when=None if when is None else when.canonical,
            band=band,
            reads=tuple(sorted(set(own) | set(when_own) | set(self._first_columns(found)))),
            paths=found,
            whole_path=_is_whole_path(checked.parsed.tree),
        )

    def _summary(
        self,
        invariant: Mapping[str, Any],
        where: str,
        column: str,
        when: Optional[Checked],
        band: Optional[Band],
    ) -> PlannedRule:
        """A contract ``derive`` with ``aggregate``: the summary of the children of a counted layout.

        It generates on kernel 0.2.0 when the relationship ``via`` of ``from`` has a ``counts``
        entry (ADR 0016 section 5); otherwise it is ``FLX-6210``. The summary is checked as the
        FLUX-side text EXPR-SPEC-2 section 5 maps it to, on the parent: the column and the filter
        resolve in the child (``FLX-6113``), a sum takes a number of bounded scale and the value
        fits the column (``FLX-6110``). A filter reads the child's own columns; one that reads
        another row is ``FLX-6210``.
        """
        aggregate = invariant["aggregate"]
        source, via = str(aggregate.get("from")), str(aggregate.get("via"))
        if not self.counter_based:
            raise Expr2Error(
                FLX_6210,
                f"{where} derives {column!r} from an aggregate over {source}.{via}; kernel 0.2.0 "
                "generates a summary over a relationship laid out with a counts entry, and this "
                "run is kernel 0.1.0",
            )
        if (source, via) not in self.counted:
            raise Expr2Error(
                FLX_6210,
                f"{where} derives {column!r} from an aggregate over {source}.{via}, which is not "
                "on the counted layout; this version generates a summary only over a relationship "
                f"the Simulation lays out with a counts entry: lay {source}.{via} out with one",
            )
        checked = check(
            aggregate_tree(aggregate),
            self.model,
            self.expose_id,
            position=VALUE,
            target=column,
        )
        if any(read.reach == "children" and len(read.path) > 2 for read in checked.reads):
            raise Expr2Error(
                FLX_6210,
                f"{where} filters the children of {source}.{via} on another row (a link); this "
                "version generates a filter over the child's own columns",
            )
        when_own, when_paths, when_beyond = _split_reads(when)
        if when_paths or when_beyond:
            raise Expr2Error(
                FLX_6210,
                f"{where} derives {column!r} from an aggregate when a condition that reads another "
                "row holds; this version generates a condition over the row's own columns",
            )
        return PlannedRule(
            id=invariant["id"],
            form=FORM_DERIVE,
            text=checked.canonical,
            column=column,
            when=None if when is None else when.canonical,
            band=band,
            reads=when_own,
            summary=(source, via),
        )

    def _check(
        self,
        invariant: Mapping[str, Any],
        where: str,
        generates: Optional[str],
        when: Optional[Checked],
        band: Optional[Band],
    ) -> PlannedRule:
        checked = self._predicate(invariant["check"], f"{where} check")
        form = _check_form(checked.parsed.tree)
        plain = PlannedRule(
            id=invariant["id"],
            form=form,
            text=checked.canonical,
            when=None if when is None else when.canonical,
            band=band,
        )
        if generates is None:
            return plain
        column = nfc(generates)
        if column not in self.columns:
            raise Expr2Error(
                FLX_6113,
                f"{where}: the FLUX setting generates {column!r}, which is not a column "
                f"of {self.expose_id}",
            )
        if form == FORM_OTHER:
            raise Expr2Error(
                FLX_6202,
                f"{where} is neither a bounds nor a difference form, so no column is generated "
                f"from it ({column!r})",
            )
        if form == FORM_BOUNDS:
            leaves = _bounds_leaves(checked.parsed.tree)
            assert leaves is not None
            if self._bounds_fit(leaves, column, when):
                raise Expr2Error(
                    FLX_6210,
                    f"{where} bounds {column!r}; this version does not generate from a bounds form",
                )
            raise Expr2Error(
                FLX_6202,
                f"{where} does not compare {column!r} alone with expressions that do not read it",
            )
        return self._difference_rule(checked, plain, where, column, when)

    def _bounds_fit(self, leaves: Sequence[Compare], column: str, when: Optional[Checked]) -> bool:
        for leaf in leaves:
            left_is, right_is = _is_column(leaf.left, column), _is_column(leaf.right, column)
            if left_is == right_is:
                return False
            other = leaf.right if left_is else leaf.left
            if _mentions(other, column):
                return False
        return when is None or column not in _own_columns(when)[0]

    def _difference_rule(
        self, checked: Checked, plain: PlannedRule, where: str, column: str, when: Optional[Checked]
    ) -> PlannedRule:
        found = _difference(checked.parsed.tree)
        assert found is not None
        unit, call, k_node = found
        first, second = call.args
        first_is, second_is = _is_column(first, column), _is_column(second, column)
        if first_is == second_is:
            raise Expr2Error(
                FLX_6202,
                f"{where}: {column!r} is not exactly one argument of its date difference",
            )
        x_node = second if first_is else first
        k = check(
            parse(_canonical_of(k_node, self.model, self.expose_id), side=Side.CORE),
            self.model,
            self.expose_id,
            position=VALUE,
        )
        x = check(
            parse(_canonical_of(x_node, self.model, self.expose_id), side=Side.CORE),
            self.model,
            self.expose_id,
            position=VALUE,
        )
        k_own, k_paths, k_beyond = _split_reads(k)
        x_own, x_paths, x_beyond = _split_reads(x)
        when_own, when_paths, when_beyond = _split_reads(when)
        for name, reads in (("value side", k_own + x_own), ("when", when_own)):
            if column in reads:
                raise Expr2Error(
                    FLX_6202, f"{where}: the {name} reads {column!r}, the column it generates"
                )
        if unit != "days":
            raise Expr2Error(
                FLX_6210,
                f"{where} generates {column!r} from a {unit[:-1]} difference; this version "
                "generates a difference over days",
            )
        if k_beyond or x_beyond or when_beyond:
            raise Expr2Error(
                FLX_6210,
                f"{where} generates {column!r} from another row; this version generates a "
                "difference that reads the row's own columns",
            )
        link_paths = tuple(sorted(set(k_paths) | set(x_paths) | set(when_paths)))
        if not self._links_generate(link_paths):
            raise Expr2Error(
                FLX_6210,
                f"{where} generates {column!r} from another row; this version generates a "
                "difference that reads the row's own columns. Kernel 0.2.0 generates one that "
                "reads through a link",
            )
        # DAYS_BETWEEN(x, c) = (c - x).days = k, so c = x + k; DAYS_BETWEEN(c, x) = k gives c = x - k.
        return PlannedRule(
            id=plain.id,
            form="check-difference-days",
            text=plain.text,
            column=column,
            when=plain.when,
            band=plain.band,
            k=k.canonical,
            x=x.canonical,
            sign=1 if second_is else -1,
            reads=tuple(
                sorted(
                    set(k_own) | set(x_own) | set(when_own) | set(self._first_columns(link_paths))
                )
            ),
            paths=link_paths,
        )


def _order(
    rules: Sequence[PlannedRule],
    declared: Sequence[str],
    determined: FrozenSet[str],
    links: Sequence[PlannedLink] = (),
    asof: Sequence[AsOfEntry] = (),
) -> Tuple[Tuple[str, ...], Tuple[PlannedLink, ...]]:
    """The determined columns, each after those its rules read; ties in declared column order.

    A conditional link is a step of its own (ADR 0022 section 5). It comes after the rules that
    determine the columns its predicate reads, or that set the link's own columns, and before
    every rule that reads one of the link's columns, so a copy, a window's partition or a rule
    over the link sees the NULL it clears. The links come back with ``after`` set to the number
    of determined columns set before each.
    """
    nodes = _order_nodes(rules, determined, links, asof)
    rank = {column: i for i, column in enumerate(declared)}
    remaining = {node: set(before) for node, before in nodes.items()}
    ordered: List[str] = []
    placed: Dict[int, int] = {}
    while remaining:
        ready = sorted(
            (n for n, before in remaining.items() if not before),
            key=lambda n: (0, int(n[1])) if n[0] == 0 else (1, rank[n[1]]),
        )
        if not ready:
            raise _order_cycle(rules, links, remaining, rank)
        node = ready[0]
        if node[0] == 0:
            placed[int(node[1])] = len(ordered)
        else:
            ordered.append(node[1])
        del remaining[node]
        for before in remaining.values():
            before.discard(node)
    return tuple(ordered), tuple(
        replace(link, after=placed[number]) for number, link in enumerate(links)
    )


def _order_nodes(
    rules: Sequence[PlannedRule],
    determined: FrozenSet[str],
    links: Sequence[PlannedLink],
    asof: Sequence[AsOfEntry] = (),
) -> Dict[Tuple[int, str], Set[Tuple[int, str]]]:
    """The steps of :func:`_order` and what each waits for: a determined column is ``(1, name)``, a link
    ``(0, number)``.

    A rule that reads through a relationship with an as-of entry waits for the entry's ``date``, so a
    ``date`` such a rule sets is a cycle (ADR 0017 section 8).
    """
    nodes: Dict[Tuple[int, str], Set[Tuple[int, str]]] = {}
    dates = {entry.relationship: entry.date for entry in asof}
    for column in determined:
        nodes[(1, column)] = set()
    for rule in rules:
        for column in rule.determines:
            before = nodes[(1, column)]
            before.update((1, name) for name in rule.reads if name in determined)
            before.update(
                (1, dates[path[0]])
                for path in rule.paths
                if path[0] in dates and dates[path[0]] in determined
            )
            for number, link in enumerate(links):
                if column not in link.columns and set(rule.reads) & set(link.columns):
                    before.add((0, str(number)))
    for number, link in enumerate(links):
        nodes[(0, str(number))] = {
            (1, name) for name in (*link.reads, *link.columns) if name in determined
        }
    return nodes


def _order_cycle(
    rules: Sequence[PlannedRule],
    links: Sequence[PlannedLink],
    remaining: Mapping[Tuple[int, str], Set[Tuple[int, str]]],
    rank: Mapping[str, int],
) -> Expr2Error:
    """The refusal for steps that wait on each other: the rules, and the links when one is in the cycle."""
    stuck = {name for _, name in remaining}
    names = [rule.id for rule in rules if stuck & set(rule.determines)]
    columns = sorted((c for kind, c in remaining if kind == 1), key=rank.__getitem__)
    return Expr2Error(
        FLX_6208,
        (
            f"rules {names} determine columns {columns} in a cycle"
            if not any(kind == 0 for kind, _ in remaining)
            else f"rules {names} and the conditional links of "
            f"{sorted(links[int(c)].relationship for k, c in remaining if k == 0)} "
            f"set columns {columns} in a cycle"
        ),
    )


def sentinel_value(text: str, column: ColumnType, name: str) -> Any:
    """*text*, a link's sentinel for column *name*, as the column holds a value (ADR 0022 section 6).

    Read as the column's type, as a declared level's text is: a string as it is, a bool as
    ``true`` or ``false``, a number at the column's scale and within its range (a count of
    decimal places or a precision it does not hold is refused), a date as ``YYYY-MM-DD``.
    ``ValueError`` for a text the type does not read, and for a type that takes no sentinel.
    """
    kind = column.kind
    if kind is Kind.STRING:
        return text
    if kind is Kind.BOOL:
        if text not in ("true", "false"):
            raise ValueError(f"a BOOL sentinel is 'true' or 'false', not {text!r}")
        return text == "true"
    if kind is Kind.NUMBER:
        try:
            number = Decimal(text)
            exact = Fraction(number)
        except (InvalidOperation, ValueError):
            raise ValueError(f"{text!r} is not a number") from None
        try:
            return fit(exact, column, column_name=name)
        except Expr2Error as error:
            raise ValueError(str(error)) from None
    if kind is Kind.DATE:
        try:
            return date.fromisoformat(text)
        except ValueError:
            raise ValueError(f"{text!r} is not a date, YYYY-MM-DD") from None
    raise ValueError(f"a sentinel of a {kind.value if kind else 'untyped'} column is not read")


def _with_sentinels(
    rules: Sequence[PlannedRule],
    entries: Sequence[LinkEntry],
    model: Model,
    expose_id: str,
    where: str,
) -> Tuple[PlannedRule, ...]:
    """*rules* with each declared sentinel set on the copies it names (ADR 0022 section 6).

    A sentinel's column must be copied through the entry's relationship: a ``derive`` whose
    whole value is a path whose first relationship it is (a ``sharedColumns`` copy, a
    ``sharedAttribute`` copy that starts with it, or a contract's or a bundle's own ``derive``),
    and its text must read as the column's type and fit it (``FLX-3105``).
    """
    wanted = {
        (entry.relationship, column): text for entry in entries for column, text in entry.sentinels
    }
    if not wanted:
        return tuple(rules)
    columns = model.exposes[expose_id].columns
    out = list(rules)
    for (relationship, column), text in sorted(wanted.items()):
        at = f"{where}: the sentinel of {column!r} on link {relationship!r}"
        found = [
            index
            for index, rule in enumerate(out)
            if rule.column == column
            and rule.form == FORM_DERIVE
            and rule.whole_path
            and len(rule.paths) == 1
            and rule.paths[0][0] == relationship
        ]
        if not found:
            raise FluxError(
                FLX_3105,
                f"{at} names a column that relationship does not copy; a sentinel is for a "
                "sharedColumns column of the relationship, a sharedAttribute copy through it, or "
                "a derive whose whole value is a path that starts with it",
            )
        for index in found:
            if out[index].when is not None:
                raise FluxError(
                    FLX_3105,
                    f"{at} on expose {expose_id!r} cannot be written where the derive "
                    f"{out[index].id!r} of column {column!r} has the when "
                    f"{out[index].when!r}: a row that fails the when keeps a drawn value, not "
                    "the sentinel; drop the when or drop the sentinel",
                )
        try:
            sentinel_value(text, columns[column], column)
        except ValueError as error:
            raise FluxError(FLX_3105, f"{at} is not a value of the column: {error}") from None
        for index in found:
            out[index] = replace(out[index], sentinel=text)
    return tuple(out)


def _plan_links(
    model: Model, expose_id: str, entries: Sequence[LinkEntry], where: str
) -> Tuple[PlannedLink, ...]:
    """The conditional links an emit declares, or the refusal each earns.

    A condition is a FLUID SQL/1 predicate over the row's own columns: a read of another row is
    ``FLX-6210``. The relationship must be one the expose declares (``FLX-3105``).
    """
    links = model.exposes[expose_id].links
    planned: List[PlannedLink] = []
    for entry in entries:
        link = links.get(entry.relationship)
        if link is None:
            raise FluxError(
                FLX_3105,
                f"{where}: links configure the relationship {entry.relationship!r}, which "
                f"{expose_id!r} does not declare",
            )
        if entry.when is None:
            continue
        try:
            checked = check(parse(entry.when, side=Side.CORE), model, expose_id, position=PREDICATE)
        except Expr2Error as error:
            # The text's own refusal, with the key it came from in front of it.
            raise Expr2Error(
                error.code,
                f"links[{expose_id}.{entry.relationship}].when {entry.when!r}: "
                + error.detail.rsplit(" (at offset", 1)[0],
                position=error.position,
            ) from None
        own, paths, beyond = _split_reads(checked)
        if paths or beyond or _windows_of(checked.parsed.tree):
            raise Expr2Error(
                FLX_6210,
                f"{where}: the condition of link {entry.relationship!r} reads beyond the row "
                "(a link, previous, children, siblings or a window); a condition reads the "
                "emit's own columns",
            )
        planned.append(
            PlannedLink(
                relationship=entry.relationship,
                when=checked.canonical,
                columns=tuple(link.from_columns),
                reads=own,
            )
        )
    return tuple(planned)


@dataclass(frozen=True)
class ParentCondition:
    """A ``parentWhen`` predicate over one expose's own columns, checked (ADR 0016 section 4).

    ``when`` is the canonical text, ``reads`` the columns it reads. :meth:`holds` evaluates it on
    rows as the expose's own pass writes them, with the evaluator a rule's ``when`` uses. The
    contract model rides along to compile it and is no part of the condition's identity.
    """

    expose_id: str
    when: str
    reads: Tuple[str, ...]
    model: Any = field(compare=False, repr=False)

    def holds(
        self, columns: Sequence[str], rows: Sequence[Tuple[Any, ...]], start: int = 0
    ) -> List[bool]:
        """Whether the predicate is TRUE on each of *rows* (NULL and FALSE are not), by column order."""
        checked = check(
            parse(self.when, side=Side.CORE), self.model, self.expose_id, position=PREDICATE
        )
        index = {name: position for position, name in enumerate(columns)}
        return [
            checked.evaluate(_Values(index, list(row), start + offset), None) is True
            for offset, row in enumerate(rows)
        ]


def parent_condition(
    contract_document: Mapping[str, Any], expose_id: str, text: str
) -> Tuple[ParentCondition, bool]:
    """*text* checked as a predicate over *expose_id*'s own row, and whether it reads beyond it.

    The second value is ``True`` when the text reads a link, a previous version, children,
    siblings or a window, which a ``parentWhen`` may not. A text that does not parse or check is
    refused as any rule's is (``FLX-61nn``), and one that is not a predicate is ``FLX-6206``.
    """
    model = model_from_contract(contract_document)
    target = nfc(expose_id)
    checked = check(parse(text, side=Side.CORE), model, target, position=PREDICATE)
    own, paths, beyond = _split_reads(checked)
    reaches = bool(paths) or beyond or bool(_windows_of(checked.parsed.tree))
    return ParentCondition(target, checked.canonical, own, model), reaches


def plan_rules(
    *,
    contract_document: Mapping[str, Any],
    expose_id: str,
    settings: Any,
    related: FrozenSet[str],
    mapping: Optional[Mapping[str, Any]],
    has_fidelity: bool,
    strict: bool,
    where: str = "",
    flux_rules: Any = None,
    keys: Optional[ExposeKeys] = None,
    rows: Optional[int] = None,
    null_fractions: Optional[Mapping[str, Any]] = None,
    counter_based: bool = False,
    links: Sequence[LinkEntry] = (),
    copies: bool = False,
    counted: FrozenSet[Tuple[str, str]] = frozenset(),
    duration: Optional[Duration] = None,
    within: Sequence[WithinLevel] = (),
    windowed: bool = True,
    period: Optional[Period] = None,
    rates: Sequence[Rates] = (),
    history: Sequence[HistoryEntry] = (),
    asof: Sequence[AsOfEntry] = (),
    history_emits: Collection[str] = (),
) -> Optional[RulePlan]:
    """Read the rules of one emitted expose and decide what they generate, or refuse.

    Returns ``None`` when the expose declares no rule, or when the emit takes
    its rows from the run (no ``fidelity`` block) and no rule determines a
    column. The emit's FLUX-side rules (*flux_rules*, EXPR-SPEC-2 §1) are read with the
    contract's and set in one order. Raises ``FLX-6201`` for ids, the ``FLX-61nn`` codes of a text that
    does not check, ``FLX-3105`` for a FLUX setting that names no invariant,
    ``FLX-6202``, ``FLX-6203``, ``FLX-6204``, ``FLX-6208`` and ``FLX-6210``
    (EXPR-SPEC-2 §12). *related* is the columns the contract makes keys or foreign keys of
    the expose (:func:`flux_engine.generator.relations.related_columns`); *settings* is the emit's ``invariants`` block as
    declared; *flux_rules* is its ``rules`` list as declared; *strict* says that the run refuses what coverage leaves open
    (``FLX-3304``). *keys* is the expose's declared keys and relationships, which say whether a
    dependency's ``to`` column is a key (refused) or a single foreign key (drawn, then held at the
    parent's nearest key); without them every related column is taken to be a key. *rows* is the
    emit's declared row count and *null_fractions* the ``nullFraction`` each column declares
    under the emit's ``fidelity.columns``, as declared; a dependency's band is read against
    them, and decides how its ``to`` columns' NULLs are drawn (EXPR-SPEC-2 section 8).
    *counter_based* says that the run is kernel 0.2.0's, the only kernel on which a rule reads
    the row a link reaches (EXPR-SPEC-2 section 6); on kernel 0.1.0 such a rule is ``FLX-6210``.
    *links* is the emit's conditional links (:mod:`flux_engine.seam.links`, ADR 0022 section 5):
    each becomes a step that clears the relationship's columns where its condition is not TRUE,
    ordered with the rules, and kernel 0.1.0 refuses them (``FLX-3105``). *copies* says that the
    Simulation asks for copies (``copies: true``, ADR 0022 section 4): only then, on kernel 0.2.0,
    is a ``sharedColumns`` or ``sharedAttribute`` copied; otherwise it is read as it always was.
    *counted* is the ``(exposeId, relationship)`` pairs the Simulation lays out with a ``counts``
    entry (ADR 0016): on kernel 0.2.0 a contract ``derive`` with ``aggregate`` over one of them
    generates (:class:`RulePlan` ``summary``), and over any other it is ``FLX-6210``.
    *duration* is the emit's lifetime when it has a duration (ADR 0024 section 4): its end becomes
    a column a rule determines, set per row from the row's own start, on kernel 0.2.0.
    *within* is the emit's ``within`` entries for a date (ADR 0024 section 5), one level each: a
    date column they name becomes a column a rule determines, set per row inside the lifetimes of
    the rows its links reach, which the plan of the emit reads as a path does. *windowed* says
    that the run declares ``spec.timeBounds``; without it an entry that cannot bound its date
    from above is ``FLX-6205``. *period* is the end of the emit's lifetime when the emit has a
    period among its *within* entries (ADR 0024 section 5): the end is set per row, cut at the
    ends of the rows its period entries reach, and it replaces the duration's own rule. *rates*
    is the emit's seasonal rates, one entry per date column its dates steps set (ADR 0025
    section 2): they are listed in the plan and thin those columns' draws.
    *history* is the emit's history (:mod:`flux_engine.seam.history`, ADR 0017 section 2):
    kernel 0.1.0 refuses it (``FLX-3105``), and on kernel 0.2.0 a FLUX-side ``derive`` that reads
    ``previous`` generates (ADR 0017 section 7): an emit with no history keeps ``FLX-6210``.
    *asof* is the emit's as-of links (:mod:`flux_engine.seam.asof`, ADR 0017 section 8): kernel 0.1.0
    refuses them (``FLX-3105``), and a ``date`` that a rule reading through the same link sets is
    ``FLX-6208``. *history_emits* is the exposes the Simulation declares a history for, which a path
    may cross: what it reads there is checked when the plans are related.
    """
    prefix = f"{where}: " if where else ""
    target = nfc(expose_id)
    _refuse_links_on_kernel_1(links, counter_based, prefix, target)
    _refuse_history(history, counter_based, prefix, target)
    _refuse_asof(asof, counter_based, prefix, target)
    by_id = declared_exposes(contract_document)
    _check_contract_ids(by_id, contract_document, prefix)
    entry = by_id.get(target)
    invariants = _invariants(entry) if entry is not None else []
    ids = [str(invariant["id"]) for invariant in invariants]
    declared_settings = _settings_for(settings, ids, where=f"{prefix}emit {target!r}")
    entries = _flux_entries(flux_rules, f"{prefix}emit {target!r}")
    shared_declared = (
        counter_based and copies and has_fidelity and entry is not None and _declares_copies(entry)
    )
    if _no_source(invariants, entries, shared_declared, links, duration, within):
        return None
    model = model_from_contract(
        contract_document,
        open_to=(
            frozenset(nfc(name) for name in history_emits if nfc(name) != target)
            if counter_based
            else frozenset()
        ),
    )
    reader = _Reader(
        model, target, declared_settings, keys, related, counter_based, counted, bool(history)
    )
    rules = tuple(reader.rule(invariant) for invariant in invariants) + tuple(
        reader.flux_rule(flux_entry) for flux_entry in entries
    )
    copy_rules: Tuple[PlannedRule, ...] = ()
    pick_copies: Tuple[PickCopy, ...] = ()
    if shared_declared:
        assert entry is not None
        copy_rules, pick_copies = _shared_rules(
            reader, contract_document, entry, model, target, related
        )
    planned_links = _plan_links(model, target, links, f"{prefix}emit {target!r}")
    if _no_source(invariants, entries, copy_rules or pick_copies, planned_links, duration, within):
        return None  # every declared copy is one a copy cannot set: the emit has no rule
    rules = _with_sentinels(rules + copy_rules, links, model, target, f"{prefix}emit {target!r}")
    if period is not None:
        rules = rules + (_period_rule(period, model, target),)
    elif duration is not None:
        rules = rules + (_duration_rule(duration),)
    if within:
        rules = rules + _within_rules(model, target, within, windowed, f"{prefix}emit {target!r}")
    rules = _held_by_layout(rules, history, keys, null_fractions or {})
    determining = [rule for rule in rules if rule.determines]
    rates, asof = tuple(rates), tuple(asof)
    contract_id = _contract_id(contract_document)
    _refuse_links_without_fidelity(planned_links, has_fidelity, prefix, target)
    if not determining:
        if not has_fidelity:
            return None
        placed = _order(rules, [], frozenset(), planned_links)[1]
        return RulePlan(contract_id, target, model, rules, (), placed, rates, asof, pick_copies)
    _refuse_two_sources(rules, mapping)
    _refuse_targets_on_windows(rules, f"{prefix}emit {target!r}")
    _refuse_undeliverable(determining, related, has_fidelity, f"{prefix}emit {target!r}")
    _check_bands(determining, strict)
    _refuse_nulls_that_violate(determining, null_fractions or {}, f"{prefix}emit {target!r}")
    rules = _plan_dependency_nulls(rules, rows, null_fractions or {}, strict)
    determined = frozenset(column for rule in determining for column in rule.determines)
    order, placed = _order(
        rules, list(model.exposes[target].columns), determined, planned_links, asof
    )
    return RulePlan(contract_id, target, model, rules, order, placed, rates, asof, pick_copies)


def _held_by_layout(
    rules: Tuple[PlannedRule, ...],
    history: Sequence[HistoryEntry],
    keys: Optional[ExposeKeys],
    null_fractions: Mapping[str, Any],
) -> Tuple[PlannedRule, ...]:
    """*rules*, with each dependency a history's parent layout already holds marked ``by_layout``.

    A history with a ``parent`` lays its entities out over the parent's rows, so every version of
    an entity carries one parent: the relationship's foreign key is a function of the natural
    entity. A dependency from columns that include that entity's column to columns that are all
    foreign-key columns of the parent relationship therefore holds by construction (ADR 0017,
    amendment of 2026-11-25). Any other dependency is left to be refused or drawn as before.
    """
    if keys is None or not any(entry.parent is not None for entry in history):
        return rules
    parent = next(entry.parent for entry in history if entry.parent is not None)
    relationship = next((r for r in keys.relationships if r.name == parent.relationship), None)
    natural = {entity.column for entity in keys.entities if entity.kind == "natural"}
    if relationship is None or len(natural) != 1:
        return rules
    foreign = set(relationship.from_columns)
    held: List[PlannedRule] = []
    for rule in rules:
        if (
            rule.form == FORM_DEPENDENCY
            and set(rule.to_columns) <= foreign
            and natural <= set(rule.from_columns)
            and not any(column in null_fractions for column in rule.to_columns)
        ):
            rule = replace(rule, by_layout=True)
        held.append(rule)
    return tuple(held)


def _no_source(
    invariants: Sequence[Any],
    entries: Sequence[Any],
    copies: Any,
    links: Sequence[Any],
    duration: Optional[Duration],
    within: Sequence[WithinLevel],
) -> bool:
    """Whether an emit has nothing that makes a rule: no invariant, FLUX-side rule, copy, link, duration or within entry."""
    return not (invariants or entries or copies or links or duration is not None or within)


def _date_fact(rule: PlannedRule) -> Optional[Union[WithinLevel, Duration]]:
    """The fact of ``lifetimes`` or ``within`` a rule generates, or ``None`` for any other rule."""
    return rule.within if rule.within is not None else rule.duration


def _has_date_targets(rules: Sequence[PlannedRule]) -> bool:
    """Whether a fact among *rules* carries a target above zero."""
    facts = [_date_fact(rule) for rule in rules]
    return any(fact is not None and positive_target(fact.target) for fact in facts)


def new_tally(plan: "GeneratorPlan") -> Optional[DatesTally]:
    """A tally for *plan*'s date facts, or ``None`` unless one carries a target above zero or a column has rates.

    A plan with neither counts nothing, so a run without one keeps the receipt it had. A plan with
    seasonal rates also counts the rows that fell back from their thinning (ADR 0025 section 2).
    """
    rules = plan.rules
    if rules is None or not (rules.has_date_targets or rules.has_rates):
        return None
    return DatesTally(rules.date_facts(), rates=rules.has_rates)


def _refuse_targets_on_windows(rules: Sequence[PlannedRule], where: str) -> None:
    """Refuse (``FLX-3105``) a target on an emit whose rules hold a window over siblings.

    Such an expose is made whole, once per process, and a row of it is read from a kept copy, so
    the rows a run counts are not the rows one pass makes (ADR 0022 section 7).
    """
    if _has_date_targets(rules) and any(rule.windowed for rule in rules):
        raise FluxError(
            FLX_3105,
            f"{where}: a target on a lifetime or a within entry is counted over the rows one "
            "pass makes, and a rule of the emit holds a window over its siblings, which makes "
            "the expose whole; declare the dates without a target",
        )


def _duration_reads(duration: Duration) -> Tuple[str, ...]:
    """The columns a duration reads: the row's start and, for a dwell family, its ``given`` state."""
    return (duration.start,) if duration.given is None else (duration.start, duration.given)


def _duration_rule(duration: Duration) -> PlannedRule:
    """The step that draws a lifetime's end: it reads the row's own start and determines the end."""
    return PlannedRule(
        id=f"lifetime:{duration.start}.{duration.end}",
        form=FORM_DURATION,
        text="",
        column=duration.end,
        reads=_duration_reads(duration),
        origin=ORIGIN_BUNDLE,
        source=f"{SOURCE_LIFETIME_PREFIX}{duration.end}",
        duration=duration,
    )


def _period_rule(period: Period, model: Model, expose_id: str) -> PlannedRule:
    """The step that draws the end of a lifetime that is a period: its own start and its parents' ends.

    It reads the row's own start and the foreign keys of each period entry's first relationship,
    and the rows its entries reach as a rule over a path does. It sets the end column, from the
    lifetime's duration when it has one and otherwise from the column's own cell.
    """
    links = model.exposes[expose_id].links
    reads = [period.start]
    if period.duration is not None and period.duration.given is not None:
        reads.append(period.duration.given)  # the dwell is chosen by the row's final state
    for level in period.levels:
        reads.extend(links[level.relationship[0]].from_columns)
    return PlannedRule(
        id=f"lifetime:{period.start}.{period.end}",
        form=FORM_DURATION,
        text="",
        column=period.end,
        reads=tuple(dict.fromkeys(reads)),
        origin=ORIGIN_BUNDLE,
        paths=tuple(sorted({level.relationship for level in period.levels})),
        source=f"{SOURCE_LIFETIME_PREFIX}{period.end}",
        duration=period.duration,
        period=period,
    )


def _within_rules(
    model: Model, expose_id: str, levels: Sequence[WithinLevel], windowed: bool, where: str
) -> Tuple[PlannedRule, ...]:
    """The rules of an emit's ``within`` entries: one per entry, each reading its path (ADR 0024 section 5).

    An entry reads the first relationship's foreign key columns of its own row, and the lifetime
    of the row its whole path reaches, so the plan reads that row as a rule over a path does and
    refuses a cycle among such reads (``FLX-6210``). A run with no ``spec.timeBounds`` leaves a
    date's interval open above wherever its parent's lifetime is open, so it needs an ``offset``
    for the date that puts weight on a day from 0 up (``FLX-6205``).
    """
    links = model.exposes[expose_id].links
    if not windowed:
        by_column: Dict[str, List[WithinLevel]] = {}
        for level in levels:
            by_column.setdefault(level.column, []).append(level)
        for column, held in by_column.items():
            offsets = [level.offset for level in held if level.offset is not None]
            if not offsets or not offsets_reach_forward(offsets[0]):
                raise Expr2Error(
                    FLX_6205,
                    f"{where}: column {column!r} is within a linked row's lifetime, which may "
                    "have no end, and the run declares no spec.timeBounds to cut it at; declare "
                    "an offset with a positive weight on a day from 0 up, or the time bounds",
                )
    return tuple(
        PlannedRule(
            id=level.id,
            form=FORM_WITHIN,
            text="",
            column=level.column,
            reads=tuple(links[level.relationship[0]].from_columns),
            origin=ORIGIN_BUNDLE,
            paths=(level.relationship,),
            source=f"{SOURCE_WITHIN_PREFIX}{level.column}",
            within=level,
        )
        for level in levels
    )


def _refuse_links_on_kernel_1(
    links: Sequence[LinkEntry], counter_based: bool, prefix: str, target: str
) -> None:
    """Refuse (``FLX-3105``) links on kernel 0.1.0, which has no step that clears a relationship."""
    if links and not counter_based:
        raise FluxError(
            FLX_3105,
            f"{prefix}emit {target!r} configures links, which need kernel 0.2.0; this run is "
            "kernel 0.1.0",
        )


def _refuse_asof(asof: Sequence[AsOfEntry], counter_based: bool, prefix: str, target: str) -> None:
    """Refuse an as-of link on kernel 0.1.0 (``FLX-3105``), which has no chain of versions to read."""
    if asof and not counter_based:
        raise FluxError(
            FLX_3105,
            f"{prefix}emit {target!r} configures asOf, which needs kernel 0.2.0; this run is "
            "kernel 0.1.0",
        )


def _refuse_history(
    history: Sequence[HistoryEntry], counter_based: bool, prefix: str, target: str
) -> None:
    """Refuse a history on kernel 0.1.0 (``FLX-3105``), which has no chain of versions."""
    if history and not counter_based:
        raise FluxError(
            FLX_3105,
            f"{prefix}emit {target!r} configures a history, which needs kernel 0.2.0; this run is "
            "kernel 0.1.0",
        )


def _refuse_links_without_fidelity(
    planned_links: Sequence[Any], has_fidelity: bool, prefix: str, target: str
) -> None:
    """Refuse (``FLX-6210``) a link with a condition on an emit that has no fidelity block."""
    if planned_links and not has_fidelity:
        raise Expr2Error(
            FLX_6210,
            f"{prefix}emit {target!r} declares no fidelity block, and its link "
            f"{planned_links[0].relationship!r} has a condition; this version clears a link only "
            "where the emit generates its rows from a fidelity block",
        )


def _shared_rules(
    reader: "_Reader",
    contract_document: Mapping[str, Any],
    entry: Mapping[str, Any],
    model: Any,
    target: str,
    related: FrozenSet[str],
) -> Tuple[Tuple[PlannedRule, ...], Tuple["PickCopy", ...]]:
    """The rules of the columns the expose's shared columns and attributes copy, and its pick copies.

    A copy into the foreign key of another relationship sets that relationship's pick and makes no
    rule (ADR 0022 section 4).
    """
    copies = _copies_of(contract_document, entry, model, target, related)[0]
    return (
        tuple(reader.shared_rule(copy) for copy in copies if copy.pick is None),
        tuple(copy.pick for copy in copies if copy.pick is not None),
    )


#: The ways a read crosses a relationship; ``previous`` stays on the entity's own rows.
_LINK_REACHES = frozenset({"link", "children", "siblings"})


@dataclass(frozen=True)
class PickCopy:
    """A ``sharedColumns`` copy into the foreign key of another relationship (ADR 0022 section 4).

    ``column`` is the child's foreign key column and ``relationship`` the relationship it belongs
    to (the one whose pick is set). ``through`` is the relationship the column is copied through
    and ``reach`` its target. ``source`` is the relationship of ``reach`` that holds ``column`` as
    its own foreign key to the same keys: the row ``relationship`` reaches is the row ``source``
    reaches from the row ``through`` reaches.
    """

    column: str
    relationship: str
    through: str
    reach: str
    source: str


@dataclass(frozen=True)
class _Copy:
    """One column a ``sharedColumns`` entry or a ``sharedAttribute`` makes a copy.

    ``path`` is the relationships followed from the expose, ``read`` the column read at the end of
    them (``column`` itself for a ``sharedColumns`` entry), and ``source`` the coverage source.
    """

    column: str
    path: Tuple[str, ...]
    read: str
    rule_id: str
    source: str
    blame: str = ""
    #: Set when the copy is into the foreign key of another relationship: it sets that
    #: relationship's pick and makes no rule.
    pick: Optional["PickCopy"] = None

    @property
    def text(self) -> str:
        """The expression the copy is: ``<relationship>.<column>``, or a chain of links."""
        return ".".join(quote_name(part) for part in (*self.path, self.read))

    @property
    def language(self) -> Optional[str]:
        """``None`` for a core text (one hop at most), else the FLUX-side language of the chain."""
        return SHARED_CHAIN_LANGUAGE if len(self.path) > 1 else None


def _list_of(raw: Any) -> List[Any]:
    return list(raw) if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) else []


def _shared_lists(entry: Mapping[str, Any]) -> List[Tuple[str, Tuple[str, ...]]]:
    """Each relationship of the expose with its ``sharedColumns``, in declared order, none empty."""
    semantics = entry.get("semantics")
    found: List[Tuple[str, Tuple[str, ...]]] = []
    for relationship in _list_of(
        semantics.get("relationships") if isinstance(semantics, Mapping) else None
    ):
        if not isinstance(relationship, Mapping):
            continue
        name = relationship.get("name")
        raw = _list_of(relationship.get("sharedColumns"))
        if isinstance(name, str) and raw:
            found.append((name, tuple(nfc(c) for c in raw if isinstance(c, str))))
    return found


def _pick_copy(
    contract_document: Optional[Mapping[str, Any]],
    expose_id: str,
    reached_by: Tuple[str, Any],
    column: str,
    model: Model,
    related: FrozenSet[str],
) -> Optional[PickCopy]:
    """The copy of *column* through *through* into another relationship's pick, or ``None``.

    ``None`` when *column* is no one-column foreign key of a relationship of the expose other than
    *through* (a key, a key part, or the foreign key of *through* itself stays listed as before).
    Otherwise the copy is accepted only when the value is known to be a key of that relationship's
    target: *column* is itself the one-column foreign key, in the expose *through* reaches, of a
    relationship to the same target over the same keys. Anything else is ``FLX-6210``. A
    relationship that is ``inner`` while the copy may be NULL is also ``FLX-6210``.
    """
    through, link = reached_by
    if contract_document is None or column not in related or link is None:
        return None
    if link.target == expose_id or column not in model.exposes[link.target].columns:
        return None
    keys = compile_keys(contract_document)
    if keys is None:
        return None
    mine = keys.expose(expose_id).relationships
    held = [r for r in mine if r.from_columns == (column,) and r.name != through]
    if not held or any(r.name == through for r in mine if column in r.from_columns):
        return None
    relationship = held[0]
    reached = next((r for r in mine if r.name == through), None)
    if reached is None:
        return None
    source = next(
        (
            r
            for r in keys.expose(link.target).relationships
            if r.from_columns == (column,)
            and r.target == relationship.target
            and r.to_columns == relationship.to_columns
        ),
        None,
    )
    where = (
        f"sharedColumns {column!r} of relationship {through!r} of {expose_id!r} copies into "
        f"{column!r}, the foreign key of relationship {relationship.name!r} to "
        f"{relationship.target!r}"
    )
    if source is None:
        _refuse_unknown_source(where, link.target, relationship, column)
    if relationship.join_type == "inner" and (
        reached.join_type != "inner" or source.join_type != "inner"
    ):
        _refuse_nullable_copy(where, (through, source.name), link.target)
    return PickCopy(column, relationship.name, through, link.target, source.name)


def _refuse_unknown_source(where: str, reached: str, relationship: Any, column: str) -> NoReturn:
    """``FLX-6210``: the table a copy goes through does not hold the column as a key of the target."""
    raise Expr2Error(
        FLX_6210,
        f"{where}: {reached!r} does not hold {column!r} as a foreign key to "
        f"{relationship.target!r} on the same keys, so the copied value is not known to be a "
        f"key of {relationship.target!r}; declare that relationship on {reached!r}, or "
        "remove the column from sharedColumns",
    )


def _refuse_nullable_copy(where: str, relationships: Tuple[str, str], reached: str) -> NoReturn:
    """``FLX-6210``: an inner relationship's copy may be NULL through a left one."""
    through, source = relationships
    raise Expr2Error(
        FLX_6210,
        f"{where}, which has joinType inner, but the copy may be NULL: relationship "
        f"{through!r} or relationship {source!r} of {reached!r} is a left join; "
        "make them inner, or make the joinType left",
    )


def _left_out(
    column: str, link: Any, expose_id: str, model: Model, related: FrozenSet[str]
) -> Optional[str]:
    """Why a ``sharedColumns`` column stays declared and not generated, or ``None`` when it is copied."""
    if column in related:
        return REASON_KEY_COLUMN
    if link is not None and link.target == expose_id:
        return REASON_SELF_LINK
    held = model.exposes[expose_id].columns
    if link is None or column not in held or column not in model.exposes[link.target].columns:
        return ""
    return None


def _shared_columns_copies(
    entry: Mapping[str, Any],
    model: Model,
    expose_id: str,
    related: FrozenSet[str],
    contract_document: Optional[Mapping[str, Any]] = None,
) -> Tuple[List[_Copy], List[NotGenerated]]:
    """The copies kernel 0.2.0 makes of an expose's ``sharedColumns``, and what stays listed.

    With the *contract_document*, a column that is the one-column foreign key of another
    relationship of the expose is a copy into that relationship's pick (:func:`_pick_copy`).

    Each listed column is the copy ``derive: <column>``, ``expr: <relationship>.<column>``. A column
    that is a key or a foreign key of the expose stays declared and not generated, with its reason
    (ADR 0022 section 4), and so does one copied through a relationship of the expose to itself,
    which would read a row of its own expose, and one the relationship's two exposes do not both hold.
    """
    links = model.exposes[expose_id].links
    copies: List[_Copy] = []
    left: List[NotGenerated] = []
    for name, listed in _shared_lists(entry):
        link = links.get(name)
        stays: Dict[str, List[str]] = {REASON_KEY_COLUMN: [], REASON_SELF_LINK: [], "": []}
        for column in dict.fromkeys(listed):
            picked = _pick_copy(contract_document, expose_id, (name, link), column, model, related)
            reason = (
                None if picked is not None else _left_out(column, link, expose_id, model, related)
            )
            if reason is not None:
                stays[reason].append(column)
                continue
            copies.append(
                _Copy(
                    column=column,
                    path=(name,),
                    read=column,
                    rule_id=f"{name}.{column}",
                    source=f"{SOURCE_RELATIONSHIP_PREFIX}{name}.sharedColumns.{column}",
                    pick=picked,
                )
            )
        locator = f"semantics.relationships.{name}.sharedColumns"
        for reason, names in stays.items():
            if names:
                left.append(
                    NotGenerated(expose_id, KIND_SHARED_COLUMNS, locator, tuple(names), reason)
                )
    return copies, left


def _shared_attribute_declarations(entry: Mapping[str, Any], expose_id: str) -> List[NotGenerated]:
    """The ``sharedAttribute`` of the expose's columns, as declared."""
    found: List[NotGenerated] = []
    contract = entry.get("contract")
    for column in _list_of(contract.get("schema") if isinstance(contract, Mapping) else None):
        if not isinstance(column, Mapping):
            continue
        name, attribute = column.get("name"), column.get("sharedAttribute")
        if isinstance(name, str) and isinstance(attribute, str) and attribute:
            found.append(
                NotGenerated(
                    expose_id,
                    KIND_SHARED_ATTRIBUTE,
                    f"contract.schema.{nfc(name)}.sharedAttribute",
                    (attribute,),
                )
            )
    return found


def _members_of(contract_document: Mapping[str, Any]) -> Dict[str, List[Tuple[str, str]]]:
    """Each ``sharedAttribute`` of the contract with its members, ``(expose, column)`` in schema order."""
    found: Dict[str, List[Tuple[str, str]]] = {}
    for expose_id, entry in declared_exposes(contract_document).items():
        contract = entry.get("contract")
        for column in _list_of(contract.get("schema") if isinstance(contract, Mapping) else None):
            if not isinstance(column, Mapping):
                continue
            name, attribute = column.get("name"), column.get("sharedAttribute")
            if isinstance(name, str) and isinstance(attribute, str) and attribute:
                found.setdefault(attribute, []).append((expose_id, nfc(name)))
    return found


def _paths_between(model: Model, start: str, goal: str) -> List[Tuple[str, ...]]:
    """Every path of relationships from *start* to *goal* that visits no expose twice."""
    found: List[Tuple[str, ...]] = []

    def walk(here: str, trail: Tuple[str, ...], seen: FrozenSet[str]) -> None:
        for name, link in sorted(model.exposes[here].links.items()):
            if link.target == goal:
                found.append((*trail, name))
            elif link.target not in seen and link.target in model.exposes:
                walk(link.target, (*trail, name), seen | {link.target})

    walk(start, (), frozenset({start}))
    return found


def _attribute_owner(
    attribute: str, members: Sequence[Tuple[str, str]], model: Model
) -> Tuple[str, str, Dict[str, Tuple[str, ...]]]:
    """The owner of *attribute*: its expose, its column, and the one path each other expose follows to it.

    The owner is the member that every other member reaches by following relationships, on exactly
    one path (RFC-data-rules section 5). Several members in the owner's expose copy its first
    member within the row. ``FLX-6210`` when no member is reached by all the others, when two are,
    or when a member reaches the owner on two paths.
    """
    listed = ", ".join(f"{expose}.{column}" for expose, column in members)
    where = f"sharedAttribute {attribute!r} (members {listed})"
    exposes = list(dict.fromkeys(expose for expose, _ in members))
    reach = {
        (start, goal): _paths_between(model, start, goal)
        for start in exposes
        for goal in exposes
        if start != goal
    }
    owners = [
        goal for goal in exposes if all(reach[(start, goal)] for start in exposes if start != goal)
    ]
    if not owners:
        raise Expr2Error(
            FLX_6210,
            f"{where} has no owner: no member is reached through relationships by every other "
            "member, so there is nothing to generate once and copy",
        )
    if len(owners) > 1:
        raise Expr2Error(
            FLX_6210,
            f"{where} has more than one owner, {', '.join(owners)}: each is reached through "
            "relationships by every other member, so the owner is not one expose",
        )
    (owner,) = owners
    paths: Dict[str, Tuple[str, ...]] = {}
    for start in exposes:
        if start == owner:
            continue
        found = reach[(start, owner)]
        if len(found) > 1:
            named = " and ".join(".".join(path) for path in found)
            raise Expr2Error(
                FLX_6210,
                f"{where}: {start!r} reaches the owner {owner!r} on more than one path, {named}; "
                "an owner is reached on exactly one",
            )
        paths[start] = found[0]
    owner_column = next(column for expose, column in members if expose == owner)
    return owner, owner_column, paths


def _shared_attribute_copies(
    contract_document: Mapping[str, Any],
    model: Model,
    expose_id: str,
    related: FrozenSet[str],
) -> Tuple[List[_Copy], List[NotGenerated]]:
    """The copies kernel 0.2.0 makes of the ``sharedAttribute`` members of an expose, and what stays listed.

    Every member but the owner copies the owner's column along the one path to it (a copy in the
    owner's own expose reads the owner column of the row), named by the attribute. A member that is
    a key or a foreign key is not copied and stays declared and not generated, with its reason.
    """
    copies: List[_Copy] = []
    left: List[NotGenerated] = []
    for attribute, members in _members_of(contract_document).items():
        mine = [column for expose, column in members if expose == expose_id]
        if not mine or len(members) < 2:
            continue
        owner, owner_column, paths = _attribute_owner(attribute, members, model)
        listed = ", ".join(f"{expose}.{column}" for expose, column in members)
        copying = [column for column in mine if not (expose_id == owner and column == owner_column)]
        for column in copying:
            if column in related:
                left.append(
                    NotGenerated(
                        expose_id,
                        KIND_SHARED_ATTRIBUTE,
                        f"contract.schema.{column}.sharedAttribute",
                        (attribute,),
                        REASON_KEY_COLUMN,
                    )
                )
                continue
            copies.append(
                _Copy(
                    column=column,
                    path=() if expose_id == owner else paths[expose_id],
                    read=owner_column,
                    rule_id=attribute if len(copying) == 1 else f"{attribute}.{column}",
                    source=f"{SOURCE_SHARED_ATTRIBUTE_PREFIX}{attribute}",
                    blame=f"sharedAttribute {attribute!r} (members {listed})",
                )
            )
    return copies, left


def _declares_copies(entry: Mapping[str, Any]) -> bool:
    """Whether the expose declares a ``sharedColumns`` or a ``sharedAttribute``, which ``copies: true`` copies."""
    return declares_copies(entry)


def _copies_of(
    contract_document: Mapping[str, Any],
    entry: Mapping[str, Any],
    model: Model,
    expose_id: str,
    related: FrozenSet[str],
) -> Tuple[List[_Copy], List[NotGenerated]]:
    """Every copy kernel 0.2.0 makes in *expose_id*, and every declaration of it that stays listed."""
    columns, left = _shared_columns_copies(entry, model, expose_id, related, contract_document)
    attributes, kept = _shared_attribute_copies(contract_document, model, expose_id, related)
    return [*columns, *attributes], [*left, *kept]


def shared_copy_texts(
    contract_document: Mapping[str, Any], expose_id: str, related: FrozenSet[str]
) -> Tuple[Tuple[str, str, str, Optional[str]], ...]:
    """The copies kernel 0.2.0 makes in *expose_id*: ``(id, column, text, language)`` of each.

    ``language`` is ``None`` for a core text. ``fluxsim verify`` scores each as the ``derive`` it is.
    """
    target = nfc(expose_id)
    entry = declared_exposes(contract_document).get(target)
    if entry is None or not _declares_copies(entry):
        return ()
    model = model_from_contract(contract_document)
    return tuple(
        (copy.rule_id, copy.column, copy.text, copy.language)
        for copy in _copies_of(contract_document, entry, model, target, related)[0]
    )


def shared_copy_texts_not_generated(
    contract_document: Mapping[str, Any],
    expose_id: str,
    related: FrozenSet[str],
    *,
    generates: bool,
) -> Tuple[Tuple[str, str, str, Optional[str]], ...]:
    """The copies *expose_id* declares and the run does not make, as :func:`shared_copy_texts` gives them.

    Under kernel 0.1.0, or in an emit with no fidelity block (*generates* false), that is every
    copy; otherwise it is the members that are a key or a foreign key, which a copy does not set,
    and a ``sharedColumns`` entry through a relationship of the expose to itself. ``fluxsim
    verify`` reports each as the ``derive`` it would be and fails nothing for it. A contract
    kernel 0.2.0 would refuse (no owner for an attribute) gives none: the plan is what refuses it.
    """
    target = nfc(expose_id)
    entry = declared_exposes(contract_document).get(target)
    if entry is None or not _declares_copies(entry):
        return ()
    model = model_from_contract(contract_document)
    made: Set[Tuple[str, str]] = set()
    try:
        if generates:
            made = {
                (copy.rule_id, copy.column)
                for copy in _copies_of(contract_document, entry, model, target, related)[0]
            }
        columns, _ = _shared_columns_copies(entry, model, target, frozenset())
        attributes, _ = _shared_attribute_copies(contract_document, model, target, frozenset())
    except (Expr2Error, FluxError):
        return ()
    links = model.exposes[target].links
    own = model.exposes[target].columns
    selves = [
        _Copy(
            column=column,
            path=(name,),
            read=column,
            rule_id=f"{name}.{column}",
            source="",
        )
        for name, listed in _shared_lists(entry)
        if name in links and links[name].target == target
        for column in dict.fromkeys(listed)
        if column in own
    ]
    return tuple(
        (copy.rule_id, copy.column, copy.text, copy.language)
        for copy in (*columns, *selves, *attributes)
        if (copy.rule_id, copy.column) not in made
    )


def shared_attribute_members(
    contract_document: Mapping[str, Any], expose_id: str, related: FrozenSet[str]
) -> Dict[str, str]:
    """The columns of *expose_id* that copy a ``sharedAttribute``: column -> attribute.

    A contract the plan would refuse (no owner, two paths) gives none; the plan raises it.
    """
    target = nfc(expose_id)
    try:
        model = model_from_contract(contract_document)
        copies = _shared_attribute_copies(contract_document, model, target, related)[0]
    except (Expr2Error, FluxError):
        return {}
    return {copy.column: copy.source[len(SOURCE_SHARED_ATTRIBUTE_PREFIX) :] for copy in copies}


def _shared_declarations(
    contract_document: Mapping[str, Any],
    entry: Mapping[str, Any],
    expose_id: str,
    model: Optional[Model] = None,
    related: FrozenSet[str] = frozenset(),
) -> List[NotGenerated]:
    """The ``sharedColumns`` and ``sharedAttribute`` declarations of the expose that are not generated.

    With a *model* (kernel 0.2.0, an emit generated from statistics) only what a copy cannot set
    stays listed; without one every declaration does.
    """
    if model is not None:
        try:
            return _copies_of(contract_document, entry, model, expose_id, related)[1]
        except Expr2Error:
            pass  # `plan` refused it before this was read; list it as declared
    found: List[NotGenerated] = []
    for name, listed in _shared_lists(entry):
        found.append(
            NotGenerated(
                expose_id,
                KIND_SHARED_COLUMNS,
                f"semantics.relationships.{name}.sharedColumns",
                listed,
            )
        )
    found.extend(_shared_attribute_declarations(entry, expose_id))
    return found


def _check_not_generated(
    expose_id: str, locator: str, texts: Sequence[Optional[Checked]]
) -> NotGenerated:
    """One check that determines no column: over a link when a text reads through a relationship."""
    links = sorted(
        {
            ".".join((*read.path, read.column))
            for text in texts
            if text is not None
            for read in text.reads
            if read.reach in _LINK_REACHES
        }
    )
    kind = KIND_CHECK_OVER_LINK if links else KIND_CHECK
    return NotGenerated(expose_id, kind, locator, tuple(links))


def declared_not_generated(
    *,
    contract_document: Mapping[str, Any],
    expose_id: str,
    settings: Any,
    flux_rules: Any = None,
    where: str = "",
    counter_based: bool = False,
    has_fidelity: bool = False,
    related: FrozenSet[str] = frozenset(),
    copies: bool = False,
    enforced_checks: bool = False,
    history_emits: Collection[str] = (),
) -> Tuple[NotGenerated, ...]:
    """What the expose declares that this version accepts and does not generate.

    A relationship's ``sharedColumns`` and a column's ``sharedAttribute`` are read by nothing
    under kernel 0.1.0, and under kernel 0.2.0 unless the Simulation asks for copies (*copies*).
    Under kernel 0.2.0 (*counter_based*) with *copies*, in an emit generated from
    statistics (*has_fidelity*), they are copied, and only what a copy cannot set stays listed:
    a column that is a key or a foreign key (*related*, as :func:`plan_rules` takes it). A
    ``check`` is generated only through a FLUX ``generates`` setting
    (a difference over days), so a contract ``check`` with none, and a FLUX-side
    ``check``, determine no column and are not enforced on the rows. Each is listed
    once, in a fixed order. A declaration this version refuses (``FLX-6210`` and its
    kin) is not listed: ``plan`` raised before this is read. With *enforced_checks* (an expose whose
    values a generator plugin makes) a check is not listed: the engine holds the plugin's values to it.
    *history_emits* is as :func:`plan_emit_rules` takes it: a path may cross those exposes.
    """
    prefix = f"{where}: " if where else ""
    target = nfc(expose_id)
    entry = declared_exposes(contract_document).get(target)
    if entry is None:
        return ()
    copies_made = counter_based and copies and has_fidelity
    found = _shared_declarations(
        contract_document,
        entry,
        target,
        model_from_contract(contract_document) if copies_made else None,
        related,
    )
    invariants = _invariants(entry)
    entries = _flux_entries(flux_rules, f"{prefix}emit {target!r}")
    if (invariants or entries) and not enforced_checks:
        ids = [str(invariant["id"]) for invariant in invariants]
        declared_settings = _settings_for(settings, ids, where=f"{prefix}emit {target!r}")
        model = model_from_contract(
            contract_document,
            open_to=(
                frozenset(nfc(name) for name in history_emits if nfc(name) != target)
                if counter_based
                else frozenset()
            ),
        )
        reader = _Reader(model, target, declared_settings)
        for invariant in invariants:
            rule_id = str(invariant["id"])
            if "check" not in invariant or declared_settings.get(rule_id, {}).get("generates"):
                continue
            when = (
                reader._predicate(invariant["when"], f"rule {rule_id!r} when")
                if invariant.get("when") is not None
                else None
            )
            found.append(
                _check_not_generated(
                    target,
                    f"{SOURCE_RULE_PREFIX}{rule_id}",
                    [reader._predicate(invariant["check"], f"rule {rule_id!r} check"), when],
                )
            )
        for flux_entry in entries:
            flux_rule = check_flux_side_rule(flux_entry, model, target)
            if flux_rule.derive is None:
                found.append(
                    _check_not_generated(
                        target,
                        f"{SOURCE_FLUX_RULE_PREFIX}{flux_rule.id}",
                        [flux_rule.value, flux_rule.when],
                    )
                )
    return tuple(sorted(found, key=NotGenerated.sort_key))


def _check_contract_ids(
    by_id: Mapping[str, Any], contract_document: Mapping[str, Any], prefix: str
) -> None:
    """Every invariant id of the contract is present, an identifier and unique (``FLX-6201``)."""
    all_ids: List[Optional[str]] = []
    for declared_expose in by_id.values():
        for invariant in _invariants(declared_expose):
            rule_id = invariant.get("id")
            all_ids.append(rule_id if isinstance(rule_id, str) else None)
    if all_ids:
        label = str(contract_document.get("id", "the contract"))
        check_rule_ids(all_ids, where=f"{prefix}{label}")


def _flux_entries(flux_rules: Any, where: str) -> List[Mapping[str, Any]]:
    """The emit's FLUX-side entries: each a mapping with its own id, unique within the emit."""
    entries = flux_rule_entries(flux_rules, where=where)
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise Expr2Error(
                FLX_6201, f"{where}: rules[{index}] is {type(entry).__name__}, not a mapping"
            )
    if entries:
        check_rule_ids([entry.get("id") for entry in entries], where=f"{where} rules")
    return entries


def _refuse_undeliverable(
    determining: Sequence[PlannedRule], related: FrozenSet[str], has_fidelity: bool, where: str
) -> None:
    """Refuse a determined column that is a key or foreign key, or an emit with no fidelity block."""
    for rule in determining:
        if rule.column is not None and rule.column in related:
            raise Expr2Error(
                FLX_6210,
                f"rule {rule.id!r} determines {rule.column!r}, a key or foreign key of "
                f"{where} (a primary or unique entity, a relationship's toColumns or "
                "fromColumns); this version does not generate one",
            )
    if not has_fidelity:
        raise Expr2Error(
            FLX_6210,
            f"{where} declares no fidelity block, and rule {determining[0].id!r} determines "
            f"{determining[0].determines[0]!r}; this version generates a determined column only "
            "where the emit generates its rows from a fidelity block",
        )


def _check_bands(determining: Sequence[PlannedRule], strict: bool) -> None:
    for rule in determining:
        band = rule.band
        if band is None or rule.form == FORM_DEPENDENCY:
            continue  # a dependency's band is read when it is read (EXPR-SPEC-2 section 8)
        if band.needs_a_target():
            raise Expr2Error(
                FLX_6210,
                f"rule {rule.id!r} has a percent band of positive width, which needs a target "
                "and violating rows; this version generates no violation",
            )
        if strict and not band.zero_violations_meet():
            raise Expr2Error(
                FLX_6204,
                f"rule {rule.id!r}: its band needs violating rows, and this version generates "
                "none, so a strict run cannot meet it",
            )


def _refuse_nulls_that_violate(
    determining: Sequence[PlannedRule], null_fractions: Mapping[str, Any], where: str
) -> None:
    """Refuse (``FLX-6204``) a column whose own NULLs come first and would violate its rule.

    A column that a rule over a link determines keeps its own NULL on every row whose own draw
    is NULL (EXPR-SPEC-2 section 6, "NULLs come first"), and the check counts such a row as a
    violation of the rule. A rule that allows no violating row (no band, or a band of zero
    violations) on a column that declares a ``nullFraction`` above zero is therefore one the rows
    cannot meet, in every run; a rule with a ``rows`` band that admits violations keeps NULLs
    first. The refusal is the plan's, so ``plan``, ``apply`` and ``verify`` agree.
    """
    for rule in determining:
        column = rule.column
        if not rule.paths or column is None or rule.form in (FORM_DEPENDENCY, FORM_WITHIN):
            continue  # a within date keeps its own NULLs, which satisfy its fact (ADR 0024 section 5)
        if rule.period is not None:
            continue  # a period's end replaces its own NULL by its parent's end
        if rule.band is not None and not rule.band.allows_only_zero():
            continue
        declared = null_fractions.get(column)
        if not _fraction(declared):
            continue
        source = f" (read from {rule.source})" if rule.source else ""
        raise Expr2Error(
            FLX_6204,
            f"{where}: column {column!r} declares a nullFraction of {declared}, and "
            f"rule {rule.id!r}{source} determines it through a link and allows no violating row; the "
            "column's own NULLs come first and would violate the rule, so drop the "
            "nullFraction or give the rule a `rows` band that admits them",
        )


#: The working precision of a dependency's NULL-row share, and the bisection that finds it.
_SHARE_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN)
_SHARE_HALVINGS = 64
#: How many standard deviations of the violating count a band's upper bound is kept away from.
_SHARE_SIGMAS = 4


def _fraction(declared: Any) -> Decimal:
    """A declared ``nullFraction`` as a share, ``0`` when the column declares none.

    A value that is not a share in [0, 1] is left to the plan, which refuses it in its own words.
    """
    if declared is None or isinstance(declared, bool):
        return Decimal(0)
    try:
        fraction = Decimal(str(declared))
    except InvalidOperation:
        return Decimal(0)
    return fraction if fraction.is_finite() and 0 <= fraction <= 1 else Decimal(0)


def _violating_share(null_rows: Decimal, fractions: Sequence[Decimal]) -> Decimal:
    """``A(q) = q * (1 - prod(1 - p / q))``: the expected share of covered rows with a NULL."""
    clean = Decimal(1)
    for fraction in fractions:
        clean *= 1 - fraction / null_rows
    return null_rows * (1 - clean)


def _given_a_null_row(fraction: Optional[Decimal], null_rows: Decimal) -> Decimal:
    """A ``to`` column's chance of a NULL on a NULL row: its null fraction over the share."""
    if fraction is None or not fraction:
        return Decimal(0)
    with localcontext(_SHARE_CONTEXT):
        return min(fraction / null_rows, Decimal(1))


def _plan_dependency_nulls(
    rules: Tuple[PlannedRule, ...],
    rows: Optional[int],
    null_fractions: Mapping[str, Any],
    strict: bool,
) -> Tuple[PlannedRule, ...]:
    """*rules*, with each dependency's NULL-row share set, or the refusal its band earns."""
    planned: List[PlannedRule] = []
    for rule in rules:
        if rule.form == FORM_DEPENDENCY:
            share = _null_rows(rule, rows, null_fractions, strict)
            if share is not None:
                rule = replace(rule, null_rows=share)
        planned.append(rule)
    return tuple(planned)


def _null_rows(
    rule: PlannedRule, rows: Optional[int], null_fractions: Mapping[str, Any], strict: bool
) -> Optional[Decimal]:
    """The share of covered rows that are NULL rows (EXPR-SPEC-2 section 8), ``None`` per key.

    No band, or one whose only count is zero, draws the NULLs per key. A band that allows
    fewer than zero violating rows is ``FLX-6204``, and one with a lower bound above zero is
    ``FLX-6210``: its violations wait for section 7. A band with no upper bound, no ``to``
    column that declares a NULL, or no covered row to violate it, takes ``1``. An emit with
    no declared row count has no ``N``: a ``rows`` band there is ``FLX-6204``, and so is any
    upper bound in a strict run, until the emit declares its ``rows``. Otherwise the upper
    bound ``u``, as a share of the ``N`` covered rows, is ``FLX-6204`` below the share
    ``max p`` the column with the most NULLs can make violate on its own; a ``percent`` band
    of an emit with no declared row count takes ``max p``, the fewest violating rows. With
    ``N``, the target is ``min(S, u - 4 sqrt(u (1 - u) / N))``, never below ``max p``, and a
    strict run refuses a ``u`` that leaves less room than that above ``max p``. The share is
    ``1`` when the target is ``S``, ``max p`` when it is ``max p``, else the ``q`` whose
    ``A(q)`` it is.
    """
    band = rule.band
    if band is None or band.allows_only_zero():
        return None
    where = f"dependency {rule.id!r}"
    upper = band.upper()
    _refuse_a_band_nulls_cannot_meet(band, upper, where)
    if upper is None:
        return Decimal(1)
    with localcontext(_SHARE_CONTEXT):
        fractions = [_fraction(null_fractions.get(column)) for column in rule.to_columns]
        largest = max(fractions, default=Decimal(0))
        if not largest:
            return Decimal(1)  # no 'to' column declares a NULL, so no row violates
        covered = _covered_rows(rule, rows, null_fractions)
        if covered is not None and not covered:
            return Decimal(1)  # no covered row, so nothing to violate
        values = f"{band.operator} {[str(v) for v in band.values]} {band.unit}"
        if covered is None and (band.unit != "percent" or strict):
            reason = (
                "a count of rows the plan cannot read as a share"
                if band.unit != "percent"
                else "a bound whose room a strict run needs the row count to size"
            )
            raise Expr2Error(
                FLX_6204,
                f"{where}: the band {values} is {reason}, and the emit declares no 'rows'; "
                "declare the emit's rows so the plan can hold its NULLs to the band",
            )
        if band.unit == "percent":
            bound = upper[0] / 100
        else:
            assert covered is not None
            bound = upper[0] / covered
        if bound >= 1:
            return Decimal(1)
        heaviest = rule.to_columns[fractions.index(largest)]
        if largest > bound or (upper[1] and largest >= bound):
            raise Expr2Error(
                FLX_6204,
                f"{where}: {heaviest!r} declares a null fraction of {largest}, so its NULLs can "
                f"make that share of rows violate on their own, above the band {values}, "
                "however the NULLs fall",
            )
        if covered is None:
            return largest  # a percent band, no row count: the fewest violating rows
        room = bound - _SHARE_SIGMAS * (bound * (1 - bound) / covered).sqrt()
        if strict and largest > room:
            raise Expr2Error(
                FLX_6204,
                f"{where}: the band {values} leaves less than {_SHARE_SIGMAS} standard deviations "
                f"of the count over {covered} covered rows above the share {heaviest!r}'s NULLs "
                f"make violate on their own ({largest}), so a strict run cannot promise it",
            )
        independent = _violating_share(Decimal(1), fractions)
        target = max(min(independent, room), largest)
        if target >= independent:
            return Decimal(1)
        if target <= largest:
            return largest  # every NULL on the rows of the column with the most
        return _share_for(fractions, largest, target)


def _refuse_a_band_nulls_cannot_meet(
    band: Band, upper: Optional[Tuple[Decimal, bool]], where: str
) -> None:
    """``FLX-6204`` for a band below zero, ``FLX-6210`` for one with a lower bound above zero."""
    values = f"{band.operator} {[str(v) for v in band.values]}"
    if upper is not None and (upper[0] < 0 or (upper[1] and upper[0] <= 0)):
        raise Expr2Error(
            FLX_6204, f"{where}: its band {values} allows fewer violating rows than none"
        )
    if not band.zero_violations_meet():
        raise Expr2Error(
            FLX_6210,
            f"{where}: its band {values} has a lower bound above zero, so it needs violating "
            "rows; this version generates none beyond the 'to' columns' own NULLs, and cannot "
            "hold those to a lower bound",
        )


def _covered_rows(
    rule: PlannedRule, rows: Optional[int], null_fractions: Mapping[str, Any]
) -> Optional[Decimal]:
    """``N``: the declared row count times ``prod(1 - f)`` over the ``from`` columns' fractions."""
    if rows is None:
        return None
    covered = Decimal(rows)
    for column in rule.from_columns:
        covered *= 1 - _fraction(null_fractions.get(column))
    return covered


def _share_for(fractions: Sequence[Decimal], largest: Decimal, target: Decimal) -> Decimal:
    """The ``q`` in ``[max p, 1]`` with ``A(q)`` the target: 64 halvings, keeping the upper end."""
    low, high = largest, Decimal(1)
    for _ in range(_SHARE_HALVINGS):
        middle = (low + high) / 2
        if _violating_share(middle, fractions) < target:
            low = middle
        else:
            high = middle
    return high


def _contract_id(contract_document: Mapping[str, Any]) -> Optional[str]:
    value = contract_document.get("id")
    return value if isinstance(value, str) else None


#: How the message of ``FLX-6203`` calls the rules of each origin.
_ORIGIN_NAMES = {
    ORIGIN_CONTRACT: "contract's rules",
    ORIGIN_BUNDLE: "FLUX-side rules",
    ORIGIN_SHARED: "copies of sharedColumns or sharedAttribute",
}


def _refuse_two_sources(rules: Sequence[PlannedRule], mapping: Optional[Mapping[str, Any]]) -> None:
    by_column: Dict[str, List[PlannedRule]] = {}
    for rule in rules:
        for column in rule.determines:
            by_column.setdefault(column, []).append(rule)
    for column, sources in by_column.items():
        origins = {rule.origin for rule in sources}
        copies = sorted(rule.id for rule in sources if rule.origin == ORIGIN_SHARED)
        if len(origins) > 1 or len(copies) > 1:
            by_origin = {
                origin: sorted(rule.id for rule in sources if rule.origin == origin)
                for origin in _ORIGIN_NAMES
                if origin in origins
            }
            described = " and ".join(
                f"the {_ORIGIN_NAMES[origin]} {ids}" for origin, ids in by_origin.items()
            )
            raise Expr2Error(FLX_6203, f"column {column!r} has more than one source: {described}")
        derived = [rule for rule in sources if rule.form == FORM_DERIVE]
        # The within entries of one date are one source: they draw it together.
        generated = [rule for rule in sources if rule.form not in (FORM_DERIVE, FORM_WITHIN)]
        if any(rule.form == FORM_WITHIN for rule in sources):
            generated.append(next(rule for rule in sources if rule.form == FORM_WITHIN))
        if generated and (derived or len(generated) > 1):
            names = sorted(rule.id for rule in sources)
            raise Expr2Error(
                FLX_6203,
                f"column {column!r} has more than one source among the rules {names}: a derive "
                "and a generates, a dependency or another dependency, or two generates",
            )
    if mapping:
        named = {nfc(str(key)) for key in mapping}
        for column, sources in by_column.items():
            if column in named:
                raise Expr2Error(
                    FLX_6203,
                    f"column {column!r} is set by a mapping override and by rule {sources[0].id!r}",
                )


# ---------------------------------------------------------------------------
# Applying the rules to rows
# ---------------------------------------------------------------------------


class _Values:
    """One row's values as a :class:`~flux_engine.expr2.evaluate.RowView` over its own columns.

    ``follow`` reaches the row a link names, as its expose's own pass writes it (ADR 0022
    section 3), through the pass that holds the plan and the reader. ``children`` is the rows of
    a child expose that name this row, as the child's own pass writes them (ADR 0016 section 5),
    for a plan that carries the child plan. ``siblings`` is the rows of the whole table that share
    the row's partition (ADR 0022 section 7), which only a pass over the whole expose holds.
    ``previous`` is the row before this one in the entity's chain of versions, as the expose's own
    pass writes it (ADR 0017 section 7), and ``None`` on an entity's first version.
    """

    __slots__ = ("_context", "_index", "_values", "row")

    def __init__(
        self,
        index: Mapping[str, int],
        values: List[Any],
        row: int,
        context: Optional["_Pass"] = None,
    ) -> None:
        self._index = index
        self._values = values
        self.row = row
        self._context = context

    @property
    def key(self) -> Hashable:
        return self.row

    def get(self, column: str) -> Any:
        return self._values[self._index[column]]

    def follow(self, relationship: str) -> Optional["_Followed"]:
        assert self._context is not None, "a rule that reads another row is applied by the rows"
        context = self._context
        return context.follow(context.plan, self.row, self._values, relationship)

    def previous(self) -> Optional["_Followed"]:
        assert self._context is not None, "a rule that reads another row is applied by the rows"
        return self._context.previous(self._context.plan, self.row)

    def children(self, expose: str, relationship: str) -> Sequence[Any]:
        assert self._context is not None, "a summary over children is applied by the rows"
        return self._context.children(self.row, expose, relationship)

    def siblings(self, relationship: str) -> Sequence["_Values"]:
        context = self._context
        assert (
            context is not None and context.table is not None
        ), "a rule over siblings is applied over the whole expose"
        return context.table.siblings(self, relationship)


class _Table:
    """Every row of an expose, held as the rule steps set them, for the windows over siblings.

    ``values[i]`` is row *i*'s list, which the steps set in place, and ``views[i]`` is the row
    as a :class:`~flux_engine.expr2.evaluate.RowView`. A partition is the rows whose relationship
    columns are equal, NULL equal to NULL and numbers equal by value, in row order, as
    :meth:`flux_engine.expr2.rows.Rows.partition_index` groups them. The partitions are worked
    out from the values as they stand when a step starts (:meth:`begin`), since an earlier step
    can set a relationship's column.
    """

    def __init__(
        self,
        rules: "RulePlan",
        index: Mapping[str, int],
        values: List[List[Any]],
        context: "_Pass",
    ) -> None:
        self._rules = rules
        self._index = index
        self.values = values
        self.views = [_Values(index, row, i, context) for i, row in enumerate(values)]
        self._partitions: Dict[str, Dict[Tuple[Any, ...], List[_Values]]] = {}
        self.session = Session()

    def begin(self) -> None:
        """Start a step: forget the partitions and the windows the last step worked out."""
        self._partitions = {}
        self.session = Session()

    def siblings(self, view: _Values, relationship: str) -> List[_Values]:
        groups = self._partitions.get(relationship)
        columns = self._rules.model.exposes[self._rules.expose_id].columns
        link = self._rules.model.exposes[self._rules.expose_id].links[relationship]
        if groups is None:
            groups = {}
            for other in self.views:
                groups.setdefault(self._key(columns, link.from_columns, other), []).append(other)
            self._partitions[relationship] = groups
        return groups[self._key(columns, link.from_columns, view)]

    def _key(
        self, columns: Mapping[str, ColumnType], names: Sequence[str], view: _Values
    ) -> Tuple[Any, ...]:
        return tuple(key_value(columns[name], view.get(name)) for name in names)


class _Followed:
    """The row a link names, as its expose's own pass writes it, and the links it has in turn."""

    __slots__ = ("_context", "_index", "_plan", "_values", "row")

    def __init__(
        self,
        context: "_Pass",
        plan: "GeneratorPlan",
        row: int,
        values: Tuple[Any, ...],
        index: Mapping[str, int],
    ) -> None:
        self._context = context
        self._plan = plan
        self.row = row
        self._values = values
        self._index = index

    @property
    def key(self) -> Hashable:
        return (self._plan.expose.expose_id, self.row)

    def get(self, column: str) -> Any:
        return self._values[self._index[column]]

    def follow(self, relationship: str) -> Optional["_Followed"]:
        return self._context.follow(self._plan, self.row, self._values, relationship)

    def previous(self) -> Optional["_Followed"]:
        return self._context.previous(self._plan, self.row)

    def children(self, expose: str, relationship: str) -> Sequence[Any]:  # pragma: no cover
        raise NotImplementedError("a rule that reads another row is refused at plan time")

    def siblings(self, relationship: str) -> Sequence[Any]:  # pragma: no cover
        raise NotImplementedError("a rule that reads another row is refused at plan time")


def _has_weight(plan: "ColumnPlan", day: date) -> bool:
    """Whether *plan*'s declared statistics give *day* any weight.

    A column that declares no draw of its own (its range came from the run's
    time bounds, or it has none) gives every date weight. Otherwise the date
    must lie inside the declared range and fall in a bin that carries weight. A
    DATE column is drawn uniformly, over bins or a range, or by a rate from a
    declared minimum, never from levels (§1's family table), so a level has no
    meaning here.
    """
    from flux_engine.generator.rows import _declares_a_draw

    statistics = plan.statistics
    if plan.defaulted or not _declares_a_draw(statistics):
        return True
    ordinal = Decimal(day.toordinal() - DATE_EPOCH_ORDINAL)
    if statistics.bins is not None:
        edges = statistics.bins
        weights: Sequence[Decimal]
        # A solved column is never drawn given another (refuse_given_on_determined), so its
        # bins carry their own weights or none.
        if statistics.bin_weights is not None:
            weights = statistics.bin_weights
        else:
            weights = [Decimal(1)] * (len(edges) - 1)
        for i, weight in enumerate(weights):
            last = i == len(edges) - 2
            if edges[i] <= ordinal and (
                ordinal <= edges[i + 1] if last else ordinal < edges[i + 1]
            ):
                return bool(weight > 0)
        return False
    if statistics.range_min is not None and ordinal < statistics.range_min:
        return False
    if statistics.range_max is not None and ordinal > statistics.range_max:
        return False
    return True


def _set(step: _Step, value: Any, row: int) -> Any:
    """*value* in the class the generator writes for *step*'s column; ``FLX-6112`` if it does not fit."""
    column_type_ = step.column_type
    if column_type_.kind is Kind.NUMBER and value is not None:
        return fit(value, column_type_, column_name=step.column, row=row)
    return value


def _own_null(step: _Step, view: _Values, values: List[Any], row: int) -> bool:
    """Whether the row's own value of *step*'s column is NULL, for NULLs first.

    A column that declares a draw made its NULL in its own cell. One that declares only a
    ``nullFraction`` draws nothing in its cell, so its NULL is the null draw that cell would
    make first, at position 0 of its own stream (EXPR-SPEC-2 section 6, "NULLs come first").
    """
    if step.plan.family is not None:
        return values[step.position] is None
    context = view._context
    assert context is not None, "a rule that reads another row is applied by the rows"
    return context.own_null(step.plan, row)


def _reaches_no_row(view: Any, path: Tuple[str, ...]) -> bool:
    """Whether *path*, followed from *view*, meets a hop that reaches no row."""
    here = view
    for relationship in path:
        here = here.follow(relationship)
        if here is None:
            return True
    return False


def _apply_link(
    step: _LinkStep, view: _Values, values: List[Any], session: Optional[Session] = None
) -> None:
    """Clear the link's columns on a row where its condition is not TRUE (ADR 0022 section 5).

    The foreign key was drawn on every row, so no draw moves: a row that holds the condition
    keeps the value it drew, and any other row holds no link.
    """
    if step.when.evaluate(view, session) is not True:
        for position in step.positions:
            values[position] = None


def _apply_step(
    step: _Step,
    view: _Values,
    values: List[Any],
    row: int,
    session: Optional[Session] = None,
) -> None:
    if step.period is not None:
        _apply_period(step, step.period, view, values, row)
        return
    if step.keeps_nulls and _own_null(step, view, values, row):
        values[step.position] = None
        return  # NULLs first (EXPR-SPEC-2 section 9): the row's own NULL stands
    if step.duration is not None:
        _apply_duration(step, step.duration, view, values, row)
        return
    if step.within is not None:
        _apply_within(step, step.within, view, values, row)
        return
    _apply_items(step, view, values, row, session)


def _apply_items(
    step: _Step, view: _Values, values: List[Any], row: int, session: Optional[Session]
) -> None:
    """Set the column from the first item whose condition covers the row (declaration order decides)."""
    for item in step.items:
        if item.when is not None and not item.when.evaluate(view, session):
            continue
        # The first rule in declaration order that covers the row decides it.
        if item.value is not None:
            value = item.value.evaluate(view, session)
            if value is None and item.has_sentinel and _reaches_no_row(view, item.path):
                values[step.position] = item.sentinel  # a missing link writes its sentinel
                return
            values[step.position] = _set(step, value, row)
            return
        moved = _moved_date(step, item, view, session)
        if moved is not None:
            values[step.position] = moved
        return


def _dates_stream(context: _Pass, row: int, column: str, position: int) -> Any:
    """A new stream of *column*'s dates rule at *row*, whose next draw is *position* (ADR 0024 section 6)."""
    return (
        context.reader()
        .streams(context.plan.sim_id, context.plan.expose.expose_id)
        .dates_stream_at(row, column, position)
    )


def _holds(context: _Pass, row: int, column: str, target: str) -> bool:
    """Whether the row holds its fact: ``bernoulli(1 - t / 100)`` at position 0 of the dates stream.

    Drawn only for a ``target`` above zero, on a covered row whose own value is not NULL.
    """
    from flux_engine.core.rng import bernoulli, kernel_context

    stream = _dates_stream(context, row, column, HOLD_POSITION)
    with localcontext(kernel_context()):
        return bernoulli(stream, holding_probability(target))


def _violating_value(
    step: _Step, context: _Pass, row: int, kind: str, accepts: Callable[[int], bool]
) -> Optional[int]:
    """The value a violating row takes: the first candidate *accepts*, else an end of the declared range.

    Candidate ``j``, from 0 to 15, is a draw from the column's own statistics (the sampler and
    plan its cell uses, with no null draw) on the dates stream at ``257 + j * m``, ``m`` being the
    sampler calls one such draw makes (:func:`~flux_engine.generator.rows.candidate_calls`). When
    none is accepted, the first of the statistics' minimum and maximum that is. ``None`` when no
    value is. In the column's units, epoch days or milliseconds.
    """
    from flux_engine.generator import rows as _rows

    calls = _rows.candidate_calls(step.plan)
    for j in range(CANDIDATES):
        stream = _dates_stream(context, row, step.column, CANDIDATE_POSITION + j * calls)
        value = _epoch_of(_rows._cell_on_stream(step.plan, stream, skip_null=True), kind)
        if accepts(value):
            return value
    statistics = step.plan.statistics
    for end in (statistics.range_min, statistics.range_max):
        if end is not None and accepts(int(end)):
            return int(end)
    return None


def _stored(
    step: _Step, values: List[Any], value: Optional[int], kind: str, row: int, what: str
) -> None:
    """Write *value*, epoch days or milliseconds, as the column holds it; ``FLX-6112`` for a DATE out of range."""
    if value is None:
        values[step.position] = None
        return
    if kind != KIND_DATE:
        if not TIMESTAMP_MIN_MS <= value <= TIMESTAMP_MAX_MS:
            raise Expr2Error(
                FLX_6112,
                f"column {step.column!r}, row {row}: {what} is outside the milliseconds a "
                "TIMESTAMP column holds",
            )
        values[step.position] = value
        return
    try:
        values[step.position] = date_of_days(value)
    except (ValueError, OverflowError):
        raise Expr2Error(
            FLX_6112,
            f"column {step.column!r}, row {row}: {what} is outside the dates a DATE column holds",
        ) from None


def _lifetime_outcome(
    step: _Step,
    duration: Duration,
    context: _Pass,
    row: int,
    start: int,
    end: Optional[int],
    fell_back: bool = False,
) -> Optional[int]:
    """The end a row of a lifetime with a duration holds, after its hold draw (ADR 0024 section 7).

    *end* is the end the duration drew, ``None`` for a row with no end, which no violation covers.
    With a target above zero, position 0 decides whether the row holds; a row that does not takes
    the first candidate before its *start*, and one with none keeps the end that holds. A row whose
    end is the one thinning fell back to (*fell_back*, ADR 0025 section 2) counts a rates fallback
    when it keeps that end.
    """
    if end is None:
        return None
    tally = context.tally
    if (
        duration.target is not None
        and positive_target(duration.target)
        and not _holds(context, row, step.column, duration.target)
    ):
        found = _violating_value(
            step, context, row, duration.kind, lambda candidate: candidate < start
        )
        _count_lifetime(tally, duration, "notViolated" if found is None else "drawn", fell_back)
        return end if found is None else found
    _count_lifetime(tally, duration, "held", fell_back)
    return end


def _count_lifetime(tally: Any, duration: Duration, outcome: str, fell_back: bool) -> None:
    """Count a row's *outcome* for *duration*, and a rates fallback when it keeps the end thinning fell back to."""
    if tally is None:
        return
    tally.add(duration.id, outcome)
    if fell_back and outcome != "drawn":
        tally.add(duration.id, "ratesFallbacks")


def _state_text(drawn: _DurationStep, values: Sequence[Any]) -> Optional[str]:
    """The text of the row's final value of a dwell family's ``given``, ``None`` for NULL or no family.

    The text is EXPR-SPEC-2 section 8's, as a ``from`` value of a dependency is keyed by it, in NFC.
    """
    if drawn.given_position is None:
        return None
    value = values[drawn.given_position]
    if value is None:
        return None
    assert drawn.given_kind is not None
    return nfc(_value_text(drawn.given_kind, value))


def _apply_duration(
    step: _Step, drawn: _DurationStep, view: _Values, values: List[Any], row: int
) -> None:
    """Set a lifetime's end to the row's start plus a duration drawn on the row's dates stream.

    The start is the row's final value, so a start that a rule sets is read after that rule. A row
    whose start is NULL keeps the value its own cell drew. The value is one ``uniform53`` at
    position 1 of the stream (ADR 0024 section 6); the end is NULL when the draw is the ``open``
    slot, or, under ``endPastWindow: open``, when it falls after the run's window. A lifetime with
    a target above zero draws at position 0 whether a row with an end holds it, and a row that does
    not ends before its start (:func:`_lifetime_outcome`).

    A dwell family (ADR 0025 section 3) picks the dwell by the text of the row's final value of its
    ``given`` column; a row it has no dwell for keeps the end its own cell drew, as a NULL start does.
    """
    start = values[drawn.start_position]
    if start is None:
        return
    context = view._context
    assert context is not None, "a duration is applied by the rows"
    kind = drawn.duration.kind
    start_epoch = _epoch_of(start, kind)
    stream = _dates_stream(context, row, step.column, DATES_VALUE_POSITION)
    state = _state_text(drawn, values)
    applies, drawn_end = drawn.duration.draw(start_epoch, stream.uniform53(), state)
    if not applies:
        return
    end = drawn.duration.past_window(drawn_end, drawn.window)
    fell_back = False
    if step.rates is not None:
        # ADR 0025 section 2: the draw above is proposal 0, and each proposal is the same draw
        # under the window rule, so an end past the window under ``open`` is a NULL proposal.
        end, fell_back = thin(
            step.rates,
            stream,
            end,
            lambda: drawn.duration.end_of(start_epoch, stream.uniform53(), drawn.window, state),
        )
    end = _lifetime_outcome(step, drawn.duration, context, row, start_epoch, end, fell_back)
    _stored(step, values, end, kind, row, "the end a duration draws")


def _apply_period(
    step: _Step, drawn: _PeriodStep, view: _Values, values: List[Any], row: int
) -> None:
    """Set the end of a period: the lifetime's end, cut at its parents' ends (ADR 0024 section 5).

    A row whose start is NULL, and a row no period entry covers (a link that reaches no row, or a
    parent whose start is NULL), keeps the end the lifetime alone gives it: the duration's, or the
    cell's own value (NULLs first). On a covered row the end is drawn as it is without a period
    (the duration's ``uniform53`` at position 1 of the dates stream, the open slot ``None``, or
    the cell's value, which is ``None`` for the row's own NULL), and then cut by
    :func:`~flux_engine.generator.dates.cut_end`. The cut draws nothing. A row whose start the
    entries for the start column violated, so that it lies outside a parent's lifetime, is a
    violating row (ADR 0024 section 7) and is not cut: its end follows from its start alone.
    """
    start = values[drawn.start_position]
    covered = _covered_lifetimes(view, drawn, start)
    own_null = step.keeps_nulls and _own_null(step, view, values, row)
    kind = drawn.period.kind
    if not covered:
        if own_null:
            values[step.position] = None
        elif step.duration is not None:
            _apply_duration(step, step.duration, view, values, row)
        return
    context = view._context
    assert context is not None, "a period is applied by the rows"
    start_epoch = _epoch_of(start, kind)
    end, applies, fell_back = _period_end(step, drawn, context, row, values, start_epoch, own_null)
    lifetimes = [lifetime for _, lifetime in covered]
    leaves = drawn.period.start_targeted and any(
        not inside(lifetime, start_epoch) for lifetime in lifetimes
    )
    end = cut_end(
        end,
        [] if leaves else [lifetime[1] for lifetime in lifetimes],
        drawn.window,
        drawn.period.end_past_window,
    )
    if end is not None and _below_range(end, drawn.period.end_range):
        # The parents' end cut the end below the end column's own declared range: no end holds the
        # period and stays inside the range, so the row keeps the end its own cell drew.
        _count_period_infeasible(context, covered)
        return
    if step.duration is not None and not own_null and applies:
        end = _lifetime_outcome(
            step, step.duration.duration, context, row, start_epoch, end, fell_back
        )
    _stored(step, values, end, kind, row, "the end a period draws")


def _below_range(end: int, bounds: Optional[Tuple[int, int]]) -> bool:
    """Whether *end* lies below the declared range *bounds*' minimum."""
    return bounds is not None and end < bounds[0]


def _count_period_infeasible(context: Any, covered: Sequence[Tuple[WithinLevel, Lifetime]]) -> None:
    """Count the row as ``infeasible`` for the period entry whose parent's end cut the end furthest.

    The entry of the earliest end among the covered parents that have ended.
    """
    tally = context.tally
    if tally is None:
        return
    ended = [(lifetime[1], level) for level, lifetime in covered if lifetime[1] is not None]
    if ended:
        tally.add(min(ended, key=lambda found: found[0])[1].id, "infeasible")


def _covered_lifetimes(
    view: _Values, drawn: _PeriodStep, start: Any
) -> List[Tuple[WithinLevel, Lifetime]]:
    """The entries of a period and the lifetimes of the parents they reach (none for a row whose start is NULL)."""
    covered: List[Tuple[WithinLevel, Lifetime]] = []
    if start is None:
        return covered
    for level in drawn.period.levels:
        found = _parent_lifetime(view, level, drawn.period.kind)
        if found is not None:
            covered.append((level, found[0]))
    return covered


def _period_end(
    step: _Step,
    drawn: _PeriodStep,
    context: _Pass,
    row: int,
    values: Sequence[Any],
    start_epoch: int,
    own_null: bool,
) -> Tuple[Optional[int], bool, bool]:
    """The end a covered row has before it is cut, whether the duration applied, and whether thinning fell back.

    The end is the row's own NULL, the duration's draw (thinned under ``rates``, ADR 0025 section
    2), or the cell's own value: for a lifetime with no duration, and for a dwell family with no
    dwell for the row's state.
    """
    end: Optional[int] = None
    applies = True
    fell_back = False
    period_duration = drawn.period.duration
    if not own_null and period_duration is not None:
        assert step.duration is not None
        stream = _dates_stream(context, row, step.column, DATES_VALUE_POSITION)
        state = _state_text(step.duration, values)
        applies, end = period_duration.draw(start_epoch, stream.uniform53(), state)
        if applies and step.rates is not None:
            end, fell_back = thin(
                step.rates,
                stream,
                end,
                lambda: period_duration.drawn_end(start_epoch, stream.uniform53(), state),
            )
    if not applies or (not own_null and period_duration is None):
        cell = values[step.position]
        end = None if cell is None else _epoch_of(cell, drawn.period.kind)
    return end, applies, fell_back


def _epoch_of(value: Any, kind: str) -> int:
    """A date value of a row as epoch days (``DATE``) or epoch milliseconds (``TIMESTAMP``)."""
    return days_of_date(value) if kind == KIND_DATE else int(value)


def _parent_lifetime(view: Any, level: WithinLevel, kind: str) -> Optional[Tuple[Lifetime, int]]:
    """The lifetime of the row *level*'s links reach from *view*, in *kind*'s units, and its start.

    ``None`` where the row is not covered: a link that reaches no row (its foreign key is NULL),
    or a parent whose start is NULL. The start comes back too, in the column's units, since an
    offset counts the days from it.
    """
    here = view
    for relationship in level.relationship:
        here = here.follow(relationship)
        if here is None:
            return None
    start = here.get(level.start)
    if start is None:
        return None
    end = None if level.end is None else here.get(level.end)
    found = lifetime_in_units(
        _epoch_of(start, level.parent_kind),
        None if end is None else _epoch_of(end, level.parent_kind),
        level.parent_kind,
        kind,
    )
    assert found is not None
    return found, found[0]


def _apply_within(
    step: _Step, drawn: _WithinStep, view: _Values, values: List[Any], row: int
) -> bool:
    """Set a date inside the lifetimes of the rows its entries reach (ADR 0024 section 5).

    The lifetimes are the parents' final values. The lifetimes of the entries that cover the row
    are intersected and cut at the window to ``J``, and the date is one draw on position 1 of the
    dates stream: the cut histogram of the entry that carries an ``offset``, or, with none or
    where no bin keeps a value in ``J``, uniform over ``J``. A row no entry covers keeps the value
    its own cell drew. Returns whether the row fell back from its offset.

    The entry that carries a target above zero (one per column) lets position 0 decide whether the
    row holds. A row that does not takes the first candidate outside that entry's lifetime and
    inside every other covering entry's, and one with none keeps the date that holds (ADR 0024
    section 7). Lifetimes that do not meet (a middle row left its parent) leave no date that holds
    every entry: the date is drawn inside the nearest entry's interval, and the row counts as a
    violation of the outermost covering entry, ``infeasible``.

    The interval is ``J`` cut to the column's own declared range ``R`` (ADR 0024 section 5), so a
    date never leaves its column's range. A row where ``J`` and ``R`` do not meet keeps the date its
    own cell drew, which its own statistics drew inside ``R``, draws nothing more, and counts as a
    violation of the outermost covering entry, ``infeasible``.
    """
    lifetimes, covering, offset = _covering_levels(view, drawn)
    if not lifetimes:
        return False
    context = view._context
    assert context is not None, "a date within a lifetime is applied by the rows"
    tally = context.tally
    outcomes = ["held"] * len(covering)
    # ``J`` cut to the column's own declared range: ``J`` and ``R`` that do not meet leave no date
    # that holds the fact and that the column's own statistics allow.
    interval = fit_in_bounds(interval_of(intersect(lifetimes), drawn.window), drawn.bounds)
    if not lifetimes_meet(lifetimes):
        outcomes[-1] = "infeasible"
    else:
        found_value = _violating_date(step, drawn, context, row, covering, lifetimes, outcomes)
        if found_value is not None:
            _tally_outcomes(tally, covering, outcomes)
            _stored(step, values, found_value, drawn.kind, row, "the date drawn within a lifetime")
            return False
        if interval is None:
            outcomes[-1] = "infeasible"
    _tally_outcomes(tally, covering, outcomes)
    if interval is None:
        return False  # the row keeps the date its own cell drew, inside the declared range
    value, fell_back, thinned = _date_in_lifetimes(step, drawn, context, row, interval, offset)
    if tally is not None:
        if fell_back:
            assert offset is not None
            tally.add(offset[0].id, "offsetFallbacks")
        if thinned:
            tally.add(covering[0].id, "ratesFallbacks")
    _stored(step, values, value, drawn.kind, row, "the date drawn within a lifetime")
    return fell_back


def _covering_levels(
    view: _Values, drawn: _WithinStep
) -> Tuple[List[Lifetime], List[WithinLevel], Optional[Tuple[WithinLevel, int]]]:
    """The lifetimes of the entries that cover the row, those entries, and the one with an ``offset``.

    An entry covers the row when its parent's lifetime is read (:func:`_parent_lifetime`); the
    offset entry comes back with the start of its parent's lifetime, in the column's units.
    """
    lifetimes: List[Lifetime] = []
    covering: List[WithinLevel] = []
    offset: Optional[Tuple[WithinLevel, int]] = None
    for level in drawn.levels:
        found = _parent_lifetime(view, level, drawn.kind)
        if found is None:
            continue
        lifetimes.append(found[0])
        covering.append(level)
        if level.offset is not None:
            offset = (level, found[1])
    return lifetimes, covering, offset


def _tally_outcomes(tally: Any, covering: Sequence[WithinLevel], outcomes: Sequence[str]) -> None:
    """Count each covering entry's outcome for the row, when the run keeps a tally."""
    if tally is not None:
        for level, outcome in zip(covering, outcomes, strict=True):
            tally.add(level.id, outcome)


def _violating_date(
    step: _Step,
    drawn: _WithinStep,
    context: Any,
    row: int,
    covering: Sequence[WithinLevel],
    lifetimes: Sequence[Lifetime],
    outcomes: List[str],
) -> Optional[Any]:
    """The date a targeted entry makes the row violate, or ``None`` when the row keeps the date that holds.

    The entry that carries a target above zero lets position 0 decide whether the row holds. A row
    that does not takes the first candidate outside that entry's lifetime and inside every other
    covering entry's, and *outcomes* then says ``drawn`` for the entry; with no such candidate it
    says ``notViolated``.
    """
    targeted = next((i for i, level in enumerate(covering) if positive_target(level.target)), None)
    if targeted is None:
        return None
    index: int = targeted
    target = covering[index].target
    assert target is not None
    if _holds(context, row, step.column, target):
        return None
    found_value = _violating_value(
        step,
        context,
        row,
        drawn.kind,
        lambda candidate: not inside(lifetimes[index], candidate)
        and all(inside(lifetime, candidate) for i, lifetime in enumerate(lifetimes) if i != index),
    )
    outcomes[index] = "notViolated" if found_value is None else "drawn"
    return found_value


def _date_in_lifetimes(
    step: _Step,
    drawn: _WithinStep,
    context: Any,
    row: int,
    interval: Lifetime,
    offset: Optional[Tuple[WithinLevel, int]],
) -> Tuple[Any, bool, bool]:
    """The date drawn over *interval*, the lifetimes' intersection cut at the window and the range, and whether it fell back or was thinned.

    One draw on position 1 of the dates stream: the cut histogram of the entry that carries an
    ``offset``, or, with none or where no bin keeps a value, uniform over the interval. A column
    with ``rates`` thins it (ADR 0025 section 2). An interval with no upper end that the offset
    keeps no value in is ``FLX-6205``.
    """
    cut = (
        None
        if offset is None or offset[0].offset is None
        else cut_table(offset_table(offset[0].offset), offset[1], interval, unit_of(drawn.kind))
    )
    stream = _dates_stream(context, row, step.column, DATES_VALUE_POSITION)
    has_offset = offset is not None
    thinned = False
    try:
        value, fell_back = draw_within(interval, cut, has_offset, stream)
        if step.rates is not None:
            # ADR 0025 section 2: the draw above is proposal 0; each later proposal is the same
            # draw, which does not move the offset fallback (it depends on the interval alone).
            chosen, thinned = thin(
                step.rates,
                stream,
                value,
                lambda: draw_within(interval, cut, has_offset, stream)[0],
            )
            assert chosen is not None  # a date drawn within a lifetime is a value, never NULL
            value = chosen
    except ValueError:
        raise Expr2Error(
            FLX_6205,
            f"column {step.column!r}, row {row}: its interval has no upper end and its offset "
            "keeps no value inside it, so nothing declares a draw over it",
        ) from None
    return value, fell_back, thinned


def _moved_date(step: _Step, item: Any, view: _Values, session: Optional[Session]) -> Any:
    """The date an interval rule moves the row's date to, or ``None`` to keep the value as it is.

    An empty interval, a date out of range, or one the column gives no weight keeps the value
    the column's own source gave it.
    """
    assert item.k is not None and item.x is not None
    k, x = item.k.evaluate(view, session), item.x.evaluate(view, session)
    if k is None or x is None or not isinstance(k, Fraction) or k.denominator != 1:
        return None
    try:
        moved = x + timedelta(days=item.sign * int(k))
    except OverflowError:
        return None
    return moved if _has_weight(step.plan, moved) else None


def rule_reference(contract_id: Optional[str], rule_id: str) -> str:
    """EXPR-SPEC-2 section 7's ``R``: the canonical JSON of ``["contract", <contract id>, <id>]``."""
    return canonical_bytes(["contract", contract_id, rule_id]).decode("utf-8")


def _number_text(value: Any) -> str:
    """A number as its exact value in canonical decimal text (EXPR-SPEC-2 section 8).

    No exponent, no leading zeros, no trailing zeros after the point, ``0`` for zero, so equal
    values are one key whatever their scale. A float is first the shortest decimal that
    converts back to the same double. Worked on the digits, so no ``decimal`` context rounds it.
    """
    number = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    sign, digit_tuple, exponent = number.as_tuple()
    if not isinstance(exponent, int):
        raise ValueError(f"{value!r} is not a finite number, so it has no key text")
    digits = list(digit_tuple)
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if not digits:
        return "0"
    text = "".join(str(digit) for digit in digits)
    if exponent >= 0:
        text += "0" * exponent
    else:
        point = len(text) + exponent
        text = f"{text[:point]}.{text[point:]}" if point > 0 else "0." + "0" * -point + text
    return f"-{text}" if sign else text


def _value_text(kind: str, value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if kind in ("timestamp", "timestamptz"):
        if isinstance(value, datetime):
            stamp = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
            return str(int(stamp.timestamp() * 1000))
        return str(int(value))
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float, Decimal)):
        return _number_text(value)
    return str(value)


def key_text(kinds: Sequence[str], values: Sequence[Any]) -> str:
    """EXPR-SPEC-2 section 8's ``K``: the canonical JSON of the pairs ``[type, text]`` of a key's values.

    The type is the column's FLUID SQL/1 kind and the text is the value written as that
    section says, so ``1`` and ``'1'`` are two keys and no value can forge another's path.
    """
    pairs = [[kind, _value_text(kind, value)] for kind, value in zip(kinds, values, strict=True)]
    # For strings, RFC 8785's output is the standard encoder's with ensure_ascii off and no
    # whitespace (canonical_array_bytes relies on the same fact), which is far quicker; a
    # string that encoder cannot write as UTF-8 goes through the canonical writer, which refuses it.
    text = "[" + ",".join(f"[{_json(kind)},{_json(nfc(text))}]" for kind, text in pairs) + "]"
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return canonical_bytes(pairs).decode("utf-8")
    return text


def _json(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


#: How many keys' values one pass remembers before it forgets them all. The values are a
#: function of the key, so forgetting one only costs the draw again.
_KEY_CACHE_LIMIT = 16384

#: The child rows a summary reads in one call to the child's pass.
_CHILD_CHUNK = 4096


class _ChildRows(Sequence[Any]):
    """The children of one parent row: a run of the child's rows, each read as its own pass writes it."""

    def __init__(self, context: "_Pass", plan: "GeneratorPlan", run: range) -> None:
        self._context = context
        self._plan = plan
        self._run = run

    def __len__(self) -> int:
        return len(self._run)

    def __getitem__(self, index: Any) -> Any:
        if not isinstance(index, int):
            raise TypeError("the children of a row are read one at a time")
        return self._context.child(self._plan, self._run[index])

    def __iter__(self) -> Iterator[Any]:
        # The same rows as indexing yields, in order, without the per-row index check.
        child = self._context.child
        plan = self._plan
        for row in self._run:
            yield child(plan, row)


class _Pass:
    """What one :func:`apply_rules` call needs for dependencies: the run root and a small memo."""

    def __init__(
        self,
        plan: "GeneratorPlan",
        root: Optional["SeedTreeRoot"],
        tally: Optional[DatesTally] = None,
    ) -> None:
        self.plan = plan
        self.root = root
        #: What each row of a date fact came to, when the caller counts (ADR 0024 section 7).
        self.tally = tally
        self.memo: Dict[Tuple[str, str, str], Any] = {}
        #: The key of each dependency for the row being set, which its columns share, and
        #: which of its ``to`` columns are NULL in the row when it draws them per row.
        self.row_keys: Dict[str, str] = {}
        self.row_nulls: Dict[str, Tuple[bool, ...]] = {}
        self._reader: Any = None
        self._indexes: Dict[Tuple[str, str], Dict[str, int]] = {}
        #: The whole expose, for a pass that applies windows over siblings (:func:`apply_rules_whole`).
        self.table: Optional[_Table] = None
        #: The last chunk of rows read of each summed child expose: its number and its rows.
        self._chunks: Dict[str, Tuple[int, Tuple[Tuple[Any, ...], ...]]] = {}
        #: The rows this pass has finished, by row, for the previous version of the next row.
        self.finished: Dict[int, Tuple[Any, ...]] = {}
        #: The child rows each summary of the row last asked for reads, so that the rules of one
        #: row that summarise the same children lay them out once (the layout is a function of
        #: the row alone).
        self._children_row = -1
        self._children_held: Dict[Tuple[str, str], "_ChildRows"] = {}
        self._child_plans: Optional[Dict[Tuple[str, str], "GeneratorPlan"]] = None

    def reader(self) -> Any:
        if self._reader is None:
            from flux_engine.generator.relations import RelationReader

            self._reader = RelationReader(_run_root(self))
            # The history a rule's ``previous`` walks may keep to its parent's lifetime, whose
            # layout reads the parents through this plan's own foreign key (ADR 0017).
            self._reader.know(self.plan)
        return self._reader

    def own_null(self, column_plan: "ColumnPlan", row: int) -> bool:
        """The null draw *column_plan*'s own cell makes first at *row*, on its own stream."""
        from flux_engine.core.rng import bernoulli, kernel_context

        null_fraction = column_plan.statistics.null_fraction
        assert null_fraction is not None  # only a column that declares one keeps its NULLs
        stream = (
            self.reader()
            .streams(self.plan.sim_id, self.plan.expose.expose_id)
            .stream_at(row, column_plan.column.name)
        )
        with localcontext(kernel_context()):
            return bernoulli(stream, null_fraction)

    def follow(
        self, plan: "GeneratorPlan", row: int, values: Sequence[Any], relationship: str
    ) -> Optional[_Followed]:
        """The row *relationship* names at *row* of *plan*, whose values are *values*, or ``None``.

        The parent row is the one the key picked (ADR 0022 section 2), and the row is made by
        the plan of the expose it belongs to, which the reading plan carries in ``reads``. A link
        with an as-of entry reads the version of the picked row's entity valid on the row's date.
        """
        reader = self.reader()
        parent = reader.parent_row(plan, relationship, row, values)
        if parent is None:
            return None
        target = self._target(plan, relationship)
        index = self._index_of(target)
        date_column = plan.rules.asof_date(relationship) if plan.rules is not None else None
        if date_column is not None:
            # An as-of link reads the version of the picked row's entity valid on this row's date
            # (ADR 0017 section 8); a row that has no date, or an entity no version on it, reaches none.
            moment = values[self._index_of(plan)[date_column]]
            parent = None if moment is None else reader.asof_row(target, parent, moment)
            if parent is None:
                return None
        return _Followed(self, target, parent, reader.final_row(target, parent), index)

    def previous(self, plan: "GeneratorPlan", row: int) -> Optional[_Followed]:
        """The previous version of row *row* of *plan*, or ``None`` on its entity's first version.

        Row ``i - 1`` is the previous version of row ``i`` unless ``i`` opens its entity's chain
        (ADR 0017 section 7), and it is read as its own pass writes it, its rules included. A row
        this pass has finished is held; any other is made from its entity's first row up, one
        row at a time, so that a long chain does not nest.
        """
        from flux_engine.generator.relations import history_pick_of, owner_of

        pick = history_pick_of(plan)
        assert pick is not None, "a rule that reads previous is planned on a history's emit"
        reader = self.reader()
        owner = owner_of(plan)
        first = reader.first_row_of(owner, pick, row)
        if row == first:
            return None
        values = self.finished.get(row - 1) if plan is self.plan else None
        if values is None:
            for earlier in range(first, row - 1):
                reader.final_row(plan, earlier)
            values = reader.final_row(plan, row - 1)
        return _Followed(self, plan, row - 1, values, self._index_of(plan))

    def _index_of(self, plan: "GeneratorPlan") -> Dict[str, int]:
        """The position of each column in *plan*'s rows."""
        found = self._indexes.get((plan.sim_id, plan.expose.expose_id))
        if found is None:
            found = {column_plan.column.name: i for i, column_plan in enumerate(plan.columns)}
            self._indexes[(plan.sim_id, plan.expose.expose_id)] = found
        return found

    def children(self, row: int, expose: str, relationship: str) -> "_ChildRows":
        """The rows of *expose* that name row *row* of this pass's plan through *relationship*.

        The plan carries the plan of each child it summarises (``sums``); the layout is worked out
        through that plan's own foreign key, as the child's pass does (ADR 0016 section 5).
        """
        if row != self._children_row:
            self._children_row = row
            self._children_held = {}
        held = self._children_held.get((expose, relationship))
        if held is not None:
            return held
        plans = self._child_plans
        if plans is None:
            plans = {}
            for source, via, plan in self.plan.sums:
                plans.setdefault((source, via), plan)  # the first plan that matches is the one read
            self._child_plans = plans
        child = plans.get((expose, relationship))
        if child is None:  # pragma: no cover - relate_plans attaches every plan a summary reads
            raise KeyError((expose, relationship))
        held = _ChildRows(self, child, self.reader().children_of(child, relationship, row))
        self._children_held[(expose, relationship)] = held
        return held

    def child(self, plan: "GeneratorPlan", row: int) -> _Followed:
        """Row *row* of the child *plan*, as its own pass writes it, read a chunk of rows at a time."""
        number, offset = divmod(row, _CHILD_CHUNK)
        held = self._chunks.get(plan.expose.expose_id)
        if held is None or held[0] != number:
            from flux_engine.generator import rows as _rows

            start = number * _CHILD_CHUNK
            stop = min(start + _CHILD_CHUNK, plan.rows)
            held = (number, _rows.generate_row_slice(plan, start, stop, root=_run_root(self)))
            self._chunks[plan.expose.expose_id] = held
        return _Followed(self, plan, row, held[1][offset], self._index_of(plan))

    def _target(self, plan: "GeneratorPlan", relationship: str) -> "GeneratorPlan":
        """The plan of the expose *relationship* of *plan*'s expose reaches."""
        rules = self.plan.rules
        assert rules is not None
        link = rules.model.exposes[nfc(plan.expose.expose_id)].links[relationship]
        for source in (plan.reads, self.plan.reads):
            for expose_id, reached in source:
                if expose_id == link.target:
                    return reached
        raise KeyError(link.target)  # pragma: no cover - relate_plans attaches every plan read


def _apply_dependency(step: _Step, values: List[Any], context: _Pass, row: int) -> None:
    """Set *step*'s ``to`` column to its key's value, on the rows the dependency covers.

    A dependency that draws its NULLs per row first draws, once for row *row*, whether the row
    is a NULL row and which of its ``to`` columns are NULL there (EXPR-SPEC-2 section 8).
    """
    dependency = step.dependency
    assert dependency is not None
    key = context.row_keys.get(dependency.reference)
    if key is None:
        key_values = [values[position] for position in dependency.positions]
        if any(value is None for value in key_values):
            return  # a NULL in a 'from' column is not covered: the row keeps its own source
        key = key_text(dependency.kinds, key_values)
        context.row_keys[dependency.reference] = key
    skip_null = False
    if dependency.null_rows is not None:
        # The row's NULLs come from its own draw on the dependency's row stream, so a NULL
        # row can hold a NULL in any 'to' column and every other row copies the key's value.
        nulls = context.row_nulls.get(dependency.reference)
        if nulls is None:
            nulls = _row_nulls(dependency, context, row)
            context.row_nulls[dependency.reference] = nulls
        if nulls[dependency.slot]:
            values[step.position] = None
            return
        skip_null = True
    values[step.position] = _dependency_value(step, context, key, skip_null)


def _dependency_value(step: _Step, context: _Pass, key: str, skip_null: bool) -> Any:
    """The value of *step*'s ``to`` column for the key text *key*: one draw per key, remembered."""
    from flux_engine.generator import rows as _rows
    from flux_engine.generator.paths import dependency_stream

    dependency = step.dependency
    assert dependency is not None
    memo_key = (dependency.reference, key, step.column)
    if memo_key in context.memo:
        return context.memo[memo_key]
    stream = dependency_stream(
        _run_root(context), context.plan.sim_id, dependency.reference, key, step.column
    )
    if dependency.driver is not None:
        value = context.reader().foreign_on_stream(
            step.plan, dependency.driver, stream, skip_null=skip_null
        )
    else:
        value = _rows._cell_on_stream(step.plan, stream, skip_null=skip_null)
    if len(context.memo) >= _KEY_CACHE_LIMIT:
        context.memo.clear()
    context.memo[memo_key] = value
    return value


def _run_root(context: _Pass) -> "SeedTreeRoot":
    """The run root a dependency draws its streams from, which a plan with one needs."""
    if context.root is None:
        raise FluxError(
            FLX_2202,
            "a plan with a dependency, a rule that reads through a link or a summary over children "
            "is generated over the run root: pass root=",
        )
    return context.root


def _row_nulls(dependency: _Dependency, context: _Pass, row: int) -> Tuple[bool, ...]:
    """Which of *dependency*'s ``to`` columns are NULL in row *row*, in ``to`` order.

    One stream per row: its first draw says whether the row is a NULL row, at the rule's
    NULL-row share, then one draw per ``to`` column at the column's chance on a NULL row. Every
    draw is taken, so each lands at the same position whatever the others drew.
    """
    from flux_engine.core.rng import bernoulli
    from flux_engine.generator.paths import dependency_row_stream

    assert dependency.null_rows is not None
    stream = dependency_row_stream(
        _run_root(context), context.plan.sim_id, dependency.reference, row
    )
    null_row = bernoulli(stream, dependency.null_rows)
    draws = [bernoulli(stream, given) for given in dependency.null_given]
    return tuple(null_row and drawn for drawn in draws)


def apply_rules(
    plan: "GeneratorPlan",
    rows: Sequence[Tuple[Any, ...]],
    start: int,
    root: Optional["SeedTreeRoot"] = None,
    tally: Optional[DatesTally] = None,
) -> Tuple[Tuple[Any, ...], ...]:
    """*rows*, which are rows *start* onward of *plan*, with every determined column set.

    *root* is the run root a dependency draws its streams from; a plan with none needs no root.
    A *tally* counts what each row of a date fact came to (:func:`new_tally`); a plan whose rows
    are set a window at a time takes none (``FLX-3105`` at plan).

    Under kernel 0.2.0 a slice is set a rule at a time on arrays where the rule allows it
    (:func:`_apply_on_arrays`); anything that path does not take, or meets an error in, is
    set row by row, so what is raised is what the per-row path raises, at the row and rule it
    raises it for.
    """
    if plan.rules is not None and plan.rules.windowed:
        if start != 0 or len(rows) != plan.rows:
            raise ValueError("a plan with a window over siblings is applied to its whole expose")
        return apply_rules_whole(plan, rows, root)
    if plan.counter_based and rows:
        try:
            done = _apply_on_arrays(plan, rows, start, root)
        except Exception:
            done = None
        if done is not None:
            return done
    return _apply_by_rows(plan, rows, start, root, tally)


def apply_rules_whole(
    plan: "GeneratorPlan",
    rows: Sequence[Tuple[Any, ...]],
    root: Optional["SeedTreeRoot"] = None,
) -> Tuple[Tuple[Any, ...], ...]:
    """*rows*, every row of *plan*, with every determined column set, a step at a time.

    For an expose whose rules hold a window over siblings (ADR 0022 section 7). Each step is
    applied to every row before the next, in the order the plan sets its columns, so a window
    sees every sibling's final value of each column it reads. The windows are evaluated by the
    expr2 evaluator with one session for the step, over views whose siblings are the rows of the
    whole table that share the partition. A step that is not a window gives each row what
    :func:`_apply_by_rows` gives it: the key a dependency works out is kept per row.
    """
    rules = plan.rules
    assert rules is not None
    compiled = rules._compiled(plan)
    context = _Pass(plan, root)
    values = [list(row) for row in rows]
    table = _Table(rules, compiled.index, values, context)
    context.table = table
    keys: List[Dict[str, str]] = [{} for _ in values]
    nulls: List[Dict[str, Tuple[bool, ...]]] = [{} for _ in values]
    for step in compiled.steps:
        table.begin()
        for row, row_values in enumerate(values):
            if isinstance(step, _LinkStep):
                _apply_link(step, table.views[row], row_values, table.session)
            elif step.dependency is not None:
                context.row_keys, context.row_nulls = keys[row], nulls[row]
                _apply_dependency(step, row_values, context, row)
            else:
                _apply_step(step, table.views[row], row_values, row, table.session)
    return tuple(tuple(row) for row in values)


def _apply_by_rows(
    plan: "GeneratorPlan",
    rows: Sequence[Tuple[Any, ...]],
    start: int,
    root: Optional["SeedTreeRoot"],
    tally: Optional[DatesTally] = None,
) -> Tuple[Tuple[Any, ...], ...]:
    """The rules, a row at a time and exactly: the oracle the array path is held to."""
    rules = plan.rules
    assert rules is not None
    compiled = rules._compiled(plan)
    context = _Pass(plan, root, tally)
    out: List[Tuple[Any, ...]] = []
    for offset, row in enumerate(rows):
        values = list(row)
        view = _Values(compiled.index, values, start + offset, context)
        context.row_keys.clear()
        context.row_nulls.clear()
        for step in compiled.steps:
            if isinstance(step, _LinkStep):
                _apply_link(step, view, values)
            elif step.dependency is not None:
                _apply_dependency(step, values, context, start + offset)
            else:
                _apply_step(step, view, values, start + offset)
        out.append(tuple(values))
        if rules.has_previous:
            context.finished[start + offset] = out[-1]
    return tuple(out)


def _apply_on_arrays(
    plan: "GeneratorPlan",
    rows: Sequence[Tuple[Any, ...]],
    start: int,
    root: Optional["SeedTreeRoot"],
) -> Optional[Tuple[Tuple[Any, ...], ...]]:
    """The rules a step at a time over the whole slice, or ``None`` when it is the rows'.

    No rule reads another row (a plan with a rule that reads through a link is the rows'), so a
    step set for every row at once leaves each row what setting it row by row would. A ``derive`` whose texts :mod:`~flux_engine.generator.rulearrays`
    evaluates, and whose results fit, is computed on scaled integers; any other ``derive`` is
    set per row, for its step alone. A dependency draws once per distinct key. A dependency that
    draws NULLs per row, or sets a foreign key, takes the whole slice to the per-row path.
    """
    from flux_engine.generator.rulearrays import Evaluator, Unfit

    rules = plan.rules
    assert rules is not None
    if (
        any(
            rule.paths or rule.summary or rule.duration is not None or rule.previous
            for rule in rules.rules
        )
        or rules.links
    ):
        # a rule that reads a linked row or summarises children, a duration's end (drawn on the
        # row's own stream), a rule reading the previous version or a link a condition clears is
        # the rows'
        return None
    compiled = rules._compiled(plan)
    steps = [step for step in compiled.steps if isinstance(step, _Step)]
    for step in steps:
        dependency = step.dependency
        if dependency is not None and (
            dependency.null_rows is not None or dependency.driver is not None
        ):
            return None
    count = len(rows)
    cols: List[Sequence[Any]] = list(zip(*rows, strict=True))
    columns = rules.model.exposes[rules.expose_id].columns
    names = {index: name for name, index in compiled.index.items()}
    evaluator = Evaluator(
        count, lambda name: columns[name], lambda name: cols[compiled.index[name]]
    )
    context = _Pass(plan, root)
    keys: Dict[str, List[Optional[str]]] = {}
    for step in steps:
        if step.dependency is not None:
            cols[step.position] = _dependency_column(step, cols, context, keys)
        else:
            try:
                column = _derive_column(step, cols, evaluator, start)
            except Unfit:
                column = None
            if column is None:
                column = _derive_by_rows(step, compiled.index, cols, start)
            cols[step.position] = column
        evaluator.forget(names[step.position])
    return tuple(zip(*cols, strict=True))


def _derive_column(
    step: _Step, cols: Sequence[Sequence[Any]], evaluator: Any, start: int
) -> Optional[List[Any]]:
    """*step*'s column on scaled integers, or ``None`` when its rules are not evaluated so.

    Raises :class:`~flux_engine.generator.rulearrays.Unfit` for a rule or a result the array
    evaluator does not take, which leaves the step to the rows.
    """
    from flux_engine.generator.rulearrays import NULL, Num, Unfit, units_to_decimals

    target = step.column_type
    if target.kind is not Kind.NUMBER or target.scale is None or target.is_float:
        raise Unfit("a derive target that is not a decimal or an integer")
    if any(item.value is None for item in step.items):
        raise Unfit("a difference rule is evaluated per row")
    count = evaluator.rows
    out: List[Any] = list(cols[step.position])
    decided = np.zeros(count, dtype=bool)
    for item in step.items:
        if decided.all():
            break
        covered = ~decided
        if item.when is not None:
            covered = covered & evaluator.predicate(item.when.parsed.tree)
        if not covered.any():
            continue
        assert item.value is not None
        result = evaluator.value(item.value.parsed.tree)
        positions = np.flatnonzero(covered)
        if not isinstance(result, Num):
            if result is not NULL:
                raise Unfit("a derive whose value is not a number")
            for position in positions.tolist():
                out[position] = None
            decided |= covered
            continue
        units, scale = result.units[positions], result.scale
        present = np.ones(positions.shape[0], dtype=bool)
        if result.valid is not None:
            present = result.valid[positions]
        units = units[present]
        if scale > target.scale:
            factor = 10 ** (scale - target.scale)
            if factor > 10**18 or bool((units % factor != 0).any()):
                raise Unfit("more decimal places than the column's scale")
            units = units // factor
        elif scale < target.scale:
            factor = 10 ** (target.scale - scale)
            if units.size and max(abs(int(units.min())), abs(int(units.max()))) * factor > (
                (1 << 63) - 1
            ):
                raise Unfit("a value outside int64 at the column's scale")
            units = units * factor
        if units.size:
            low, high = int(units.min()), int(units.max())  # Python ints: no wrap
            if target.precision is not None and max(abs(low), abs(high)) >= 10**target.precision:
                raise Unfit("a value past the column's precision")
            if target.integer_range is not None and not (
                target.integer_range[0] <= low and high <= target.integer_range[1]
            ):
                raise Unfit("a value outside the column's integer range")
        if target.integer_range is not None:
            written: List[Any] = units.tolist()
        else:
            written = units_to_decimals(units.tolist(), target.scale)
        slots = iter(written)
        for position, here in zip(positions.tolist(), present.tolist(), strict=True):
            out[position] = next(slots) if here else None
        decided |= covered
    return out


def _derive_by_rows(
    step: _Step, index: Mapping[str, int], cols: Sequence[Sequence[Any]], start: int
) -> List[Any]:
    """*step*'s column set row by row, exactly: the per-row path's own function, for one step."""
    column = list(cols[step.position])
    for offset, row in enumerate(zip(*cols, strict=True)):
        values = list(row)
        _apply_step(step, _Values(index, values, start + offset), values, start + offset)
        column[offset] = values[step.position]
    return column


def _dependency_column(
    step: _Step,
    cols: Sequence[Sequence[Any]],
    context: _Pass,
    keys: Dict[str, List[Optional[str]]],
) -> List[Any]:
    """A dependency's ``to`` column: each row's key's value, drawn once per distinct key.

    A row whose ``from`` columns hold a NULL is not covered and keeps its own value. The key
    of a row is worked out at the dependency's first step and shared by its other ``to``
    columns, as the per-row path shares it.
    """
    dependency = step.dependency
    assert dependency is not None
    reference = dependency.reference
    column = list(cols[step.position])
    sources = [cols[position] for position in dependency.positions]
    known = keys.get(reference)
    if known is None:
        known = keys[reference] = [None] * len(column)
        pending: Sequence[int] = range(len(column))
    else:
        pending = [i for i, key in enumerate(known) if key is None]
    texts: Dict[Tuple[Any, ...], Optional[str]] = {}
    for i in pending:
        point = tuple(source[i] for source in sources)
        if point not in texts:
            texts[point] = (
                None if any(value is None for value in point) else key_text(dependency.kinds, point)
            )
        known[i] = texts[point]
    drawn: Dict[str, Any] = {}
    for i, key in enumerate(known):
        if key is None:
            continue
        if key not in drawn:
            drawn[key] = _dependency_value(step, context, key, False)
        column[i] = drawn[key]
    return column
