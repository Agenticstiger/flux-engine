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

Everything else that would determine a column is refused at plan time with
``FLX-6210`` until a later step brings it: an aggregate, a dependency with ``bothWays`` or a
band with a lower bound above zero, a bounds form, a month or year difference, a rule that reads another row, a ``percent``
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
    Dict,
    FrozenSet,
    Hashable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from flux_engine.core.canonical import canonical_bytes, nfc
from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import FLX_2202
from flux_engine.expr2.ast import And, Call, Column, Compare, Node, unparse
from flux_engine.expr2.check import PREDICATE, VALUE, Checked, check
from flux_engine.expr2.errors import (
    FLX_6111,
    FLX_6113,
    FLX_6201,
    FLX_6202,
    FLX_6203,
    FLX_6204,
    FLX_6208,
    FLX_6210,
    Expr2Error,
)
from flux_engine.expr2.parser import Side, parse
from flux_engine.expr2.rules import (
    check_flux_side_rule,
    check_language,
    check_rule_ids,
    names_another_row,
    subtrees,
)
from flux_engine.expr2.scope import Expose, Link, Model, Versions
from flux_engine.expr2.types import ColumnType, Kind, column_type, fit
from flux_engine.generator.statistics import DATE_EPOCH_ORDINAL
from flux_engine.seam.contract import declared_enum_values
from flux_engine.seam.coverage import SOURCE_FLUX_RULE_PREFIX, SOURCE_RULE_PREFIX
from flux_engine.seam.declared import (
    KIND_CHECK,
    KIND_CHECK_OVER_LINK,
    KIND_SHARED_ATTRIBUTE,
    KIND_SHARED_COLUMNS,
    NotGenerated,
)
from flux_engine.seam.keys import ExposeKeys, compile_keys, declared_exposes
from flux_engine.seam.resolve import FLX_3105

if TYPE_CHECKING:
    from flux_engine.core.seedtree import SeedTreeRoot
    from flux_engine.generator.rows import ColumnPlan, GeneratorPlan

__all__ = [
    "ORIGIN_BUNDLE",
    "ORIGIN_CONTRACT",
    "Band",
    "PlannedRule",
    "RulePlan",
    "apply_rules",
    "declared_not_generated",
    "flux_rule_entries",
    "model_from_contract",
    "plan_rules",
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

#: The keys a FLUX-side rule entry carries (EXPR-SPEC-2 §1): its texts and its band.
_FLUX_RULE_KEYS = frozenset(
    {"id", "language", "derive", "expr", "check", "when", "unit", *_BAND_KEYS, "generates"}
)

_DEPENDENCY_KEYS = frozenset({"from", "to", "bothWays"})

FORM_DERIVE = "derive"
FORM_DEPENDENCY = "dependency"
FORM_BOUNDS = "check-bounds"
FORM_OTHER = "check-other"


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


def _window_ends(semantics: Any) -> Dict[str, str]:
    """The columns of an expose's validity window: ``start`` and ``end`` (EXPR-SPEC-2 §3)."""
    dimensions = semantics.get("dimensions") if isinstance(semantics, Mapping) else None
    if not isinstance(dimensions, Sequence) or isinstance(dimensions, (str, bytes)):
        return {}
    ends: Dict[str, str] = {}
    for dimension in dimensions:
        if not isinstance(dimension, Mapping) or not isinstance(dimension.get("name"), str):
            continue
        params = dimension.get("typeParams")
        window = params.get("validityParams") if isinstance(params, Mapping) else None
        for flag, side in (("isStart", "start"), ("isEnd", "end")):
            if isinstance(window, Mapping) and window.get(flag):
                ends.setdefault(side, nfc(dimension["name"]))
    return ends


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
    ends = _window_ends(entry.get("semantics"))
    if not natural or "start" not in ends:
        return None
    return Versions(natural, ends["start"], ends.get("end"))


def model_from_contract(contract_document: Mapping[str, Any]) -> Model:
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
    """
    keys = compile_keys(contract_document)
    exposes: Dict[str, Expose] = {}
    for expose_id, entry in declared_exposes(contract_document).items():
        contract = entry.get("contract")
        schema = contract.get("schema") if isinstance(contract, Mapping) else None
        columns: Dict[str, ColumnType] = {}
        if isinstance(schema, Sequence) and not isinstance(schema, (str, bytes)):
            for column in schema:
                if isinstance(column, Mapping) and isinstance(column.get("name"), str):
                    columns.setdefault(nfc(column["name"]), _column_of(column))
        links: Dict[str, Link] = {}
        key: Tuple[str, ...] = ()
        if keys is not None:
            declared = keys.expose(expose_id)
            for relationship in declared.relationships:
                links[relationship.name] = Link(
                    relationship.name,
                    relationship.target,
                    relationship.from_columns,
                    relationship.to_columns,
                )
            primary = [entity.column for entity in declared.entities if entity.kind == "primary"]
            key = (primary[0],) if primary else ()
        exposes[expose_id] = Expose(
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

    @property
    def determines(self) -> Tuple[str, ...]:
        """The columns this rule sets: a dependency's ``to`` columns, else its one column."""
        if self.form == FORM_DEPENDENCY:
            return self.to_columns
        return () if self.column is None else (self.column,)


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


@dataclass
class _Step:
    column: str
    position: int
    column_type: ColumnType
    plan: "ColumnPlan"
    items: List[_Item] = field(default_factory=list)
    dependency: Optional[_Dependency] = None


@dataclass
class _Compiled:
    index: Dict[str, int]
    steps: Tuple[_Step, ...]


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
            if rule.column is not None and rule.form == FORM_DERIVE and rule.when is None
        )

    def refuse_given_on_determined(self, plans: Sequence["ColumnPlan"]) -> None:
        """Refuse what a determined column cannot be in this version (``FLX-6210``).

        No column may be drawn ``given`` a determined one, and a column a difference form
        generates may not be drawn ``given`` another: whether a solved date has weight
        would then depend on the level the other column drew in that row.
        """
        determined = self.determined
        solved = {rule.column for rule in self.rules if rule.column and rule.form != FORM_DERIVE}
        for column_plan in plans:
            given = column_plan.statistics.given
            if given is not None and column_plan.column.name in solved:
                raise Expr2Error(
                    FLX_6210,
                    f"column {column_plan.column.name!r} is generated by a difference form and "
                    f"drawn given {given!r}; this version does not weigh a solved date by the "
                    "level another column drew",
                )
            if given is not None and given in determined:
                raise Expr2Error(
                    FLX_6210,
                    f"column {column_plan.column.name!r} is drawn given {given!r}, which a rule "
                    "determines; this version does not generate a determined column that another "
                    "is drawn given",
                )
        self._refuse_undrawable_targets(plans)

    def _refuse_undrawable_targets(self, plans: Sequence["ColumnPlan"]) -> None:
        """Refuse a dependency's ``to`` column that has nothing to draw on its own stream (``FLX-6210``).

        An identifier is a function of the row index, a column drawn ``given`` another needs
        that column's level in the row, and a column that is not a foreign key and declares no
        draw has no statistics: none can be drawn once per key.
        """
        from flux_engine.generator.families import ColumnType as _PlannedType

        by_name = {column_plan.column.name: column_plan for column_plan in plans}
        for rule in self.rules:
            if rule.form != FORM_DEPENDENCY:
                continue
            for column in rule.to_columns:
                column_plan = by_name[column]
                if column_plan.column_type is _PlannedType.IDENTIFIER:
                    reason = "is an identifier, a function of the row index"
                elif column_plan.statistics.given is not None:
                    reason = f"is drawn given {column_plan.statistics.given!r}"
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

    @staticmethod
    def _rule_document(rule: PlannedRule, position: Mapping[str, int]) -> Dict[str, Any]:
        if rule.form == FORM_DEPENDENCY:
            return {
                "id": rule.id,
                "form": rule.form,
                "from": list(rule.from_columns),
                "to": list(rule.to_columns),
                "band": None if rule.band is None else rule.band.as_document(),
                "order": [position[column] for column in rule.to_columns],
                "origin": rule.origin,
                "nullRows": None if rule.null_rows is None else str(rule.null_rows),
            }
        document: Dict[str, Any] = {
            "id": rule.id,
            "form": rule.form,
            "text": rule.text,
            "column": rule.column,
            "when": rule.when,
            "band": None if rule.band is None else rule.band.as_document(),
            "order": None if rule.column is None else position[rule.column],
        }
        if rule.origin != ORIGIN_CONTRACT:
            # Only a FLUX-side rule says where it came from and in which language its text
            # is read, so a plan with none keeps every byte it had.
            document["origin"] = rule.origin
            document["language"] = rule.language
        return document

    def as_document(self) -> Dict[str, Any]:
        """The rules as canonical-ready JSON, for ``generatorDigest``."""
        position = {column: index for index, column in enumerate(self.order)}
        return {
            "contractId": self.contract_id,
            "rules": [self._rule_document(rule, position) for rule in self.rules],
        }

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
        steps: List[_Step] = []
        for column in self.order:
            column_plan = plan.column_plan(column)
            step = _Step(
                column=column,
                position=index[column],
                column_type=self.model.exposes[self.expose_id].columns[column],
                plan=column_plan,
            )
            for rule in self.rules_of(column):
                if rule.form == FORM_DEPENDENCY:
                    step.dependency = self._dependency_step(rule, column, index, plan)
                else:
                    step.items.append(self._item(rule, column))
            steps.append(step)
        compiled = _Compiled(index, tuple(steps))
        self._cache["compiled"] = (plan, compiled)
        return compiled

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
            return _Item(when, value=self._checked(rule.text, rule, value=True, target=column))
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
    ) -> None:
        self.model = model
        self.expose_id = expose_id
        self.settings = settings
        self.keys = keys
        self.related = related
        self.columns = model.exposes[expose_id].columns

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
        own = self._own_row_reads(flux_rule.value, when, column, where)
        return PlannedRule(
            id=flux_rule.id,
            form=FORM_DERIVE,
            text=flux_rule.value.canonical,
            column=column,
            when=when_text,
            band=band,
            reads=own,
            origin=ORIGIN_BUNDLE,
            language=language,
        )

    @staticmethod
    def _own_row_reads(
        value: Checked, when: Optional[Checked], column: str, where: str
    ) -> Tuple[str, ...]:
        """The row's own columns a FLUX-side derive reads, refusing a read of another row."""
        own, beyond = _own_columns(value)
        beyond = beyond or names_another_row(value.parsed.tree)
        when_own: Tuple[str, ...] = ()
        if when is not None:
            when_own, when_beyond = _own_columns(when)
            beyond = beyond or when_beyond or names_another_row(when.parsed.tree)
        if beyond:
            raise Expr2Error(
                FLX_6210,
                f"{where} derives {column!r} from another row (previous, a path, children or "
                "siblings); this version generates a FLUX-side derive that reads the row's own "
                "columns",
            )
        return tuple(sorted(set(own) | set(when_own)))

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
            raise Expr2Error(
                FLX_6210,
                f"{where} derives {column!r} from an aggregate over child rows; this version "
                "does not generate summaries",
            )
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
        own, beyond = _own_columns(checked)
        when_own: Tuple[str, ...] = ()
        if when is not None:
            when_own, when_beyond = _own_columns(when)
            beyond = beyond or when_beyond
        if beyond:
            raise Expr2Error(
                FLX_6210,
                f"{where} derives {column!r} from another row (a link, previous, children or "
                "siblings); this version generates a derive that reads the row's own columns",
            )
        return PlannedRule(
            id=invariant["id"],
            form=FORM_DERIVE,
            text=checked.canonical,
            column=column,
            when=None if when is None else when.canonical,
            band=band,
            reads=tuple(sorted(set(own) | set(when_own))),
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
        k_own, k_beyond = _own_columns(k)
        x_own, x_beyond = _own_columns(x)
        when_own: Tuple[str, ...] = ()
        when_beyond = False
        if when is not None:
            when_own, when_beyond = _own_columns(when)
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
            reads=tuple(sorted(set(k_own) | set(x_own) | set(when_own))),
        )


def _order(
    rules: Sequence[PlannedRule], declared: Sequence[str], determined: FrozenSet[str]
) -> Tuple[str, ...]:
    """The determined columns, each after those its rules read; ties in declared column order."""
    reads: Dict[str, Set[str]] = {column: set() for column in determined}
    for rule in rules:
        for column in rule.determines:
            reads[column].update(name for name in rule.reads if name in determined)
    rank = {column: i for i, column in enumerate(declared)}
    remaining = {column: set(before) for column, before in reads.items()}
    ordered: List[str] = []
    while remaining:
        ready = sorted((c for c, before in remaining.items() if not before), key=rank.__getitem__)
        if not ready:
            stuck = {c for c in remaining}
            names = [rule.id for rule in rules if stuck & set(rule.determines)]
            raise Expr2Error(
                FLX_6208,
                f"rules {names} determine columns {sorted(stuck, key=rank.__getitem__)} in a cycle",
            )
        column = ready[0]
        ordered.append(column)
        del remaining[column]
        for before in remaining.values():
            before.discard(column)
    return tuple(ordered)


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
    """
    prefix = f"{where}: " if where else ""
    target = nfc(expose_id)
    by_id = declared_exposes(contract_document)
    _check_contract_ids(by_id, contract_document, prefix)
    entry = by_id.get(target)
    invariants = _invariants(entry) if entry is not None else []
    ids = [str(invariant["id"]) for invariant in invariants]
    declared_settings = _settings_for(settings, ids, where=f"{prefix}emit {target!r}")
    entries = _flux_entries(flux_rules, f"{prefix}emit {target!r}")
    if not invariants and not entries:
        return None
    model = model_from_contract(contract_document)
    reader = _Reader(model, target, declared_settings, keys, related)
    rules = tuple(reader.rule(invariant) for invariant in invariants) + tuple(
        reader.flux_rule(flux_entry) for flux_entry in entries
    )
    determining = [rule for rule in rules if rule.determines]
    contract_id = _contract_id(contract_document)
    if not determining:
        return RulePlan(contract_id, target, model, rules, ()) if has_fidelity else None
    _refuse_two_sources(rules, mapping)
    _refuse_undeliverable(determining, related, has_fidelity, f"{prefix}emit {target!r}")
    _check_bands(determining, strict)
    rules = _plan_dependency_nulls(rules, rows, null_fractions or {}, strict)
    determined = frozenset(column for rule in determining for column in rule.determines)
    order = _order(rules, list(model.exposes[target].columns), determined)
    return RulePlan(contract_id, target, model, rules, order)


#: The ways a read crosses a relationship; ``previous`` stays on the entity's own rows.
_LINK_REACHES = frozenset({"link", "children", "siblings"})


def _shared_declarations(entry: Mapping[str, Any], expose_id: str) -> List[NotGenerated]:
    """The ``sharedColumns`` of the expose's relationships and the ``sharedAttribute`` of its columns."""
    found: List[NotGenerated] = []
    semantics = entry.get("semantics")
    relationships = semantics.get("relationships") if isinstance(semantics, Mapping) else None
    if isinstance(relationships, Sequence) and not isinstance(relationships, (str, bytes)):
        for relationship in relationships:
            if not isinstance(relationship, Mapping):
                continue
            name, shared = relationship.get("name"), relationship.get("sharedColumns")
            if (
                isinstance(name, str)
                and isinstance(shared, Sequence)
                and not isinstance(shared, (str, bytes))
                and shared
            ):
                found.append(
                    NotGenerated(
                        expose_id,
                        KIND_SHARED_COLUMNS,
                        f"semantics.relationships.{name}.sharedColumns",
                        tuple(nfc(column) for column in shared if isinstance(column, str)),
                    )
                )
    contract = entry.get("contract")
    schema = contract.get("schema") if isinstance(contract, Mapping) else None
    if isinstance(schema, Sequence) and not isinstance(schema, (str, bytes)):
        for column in schema:
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
) -> Tuple[NotGenerated, ...]:
    """What the expose declares that this version accepts and does not generate.

    A relationship's ``sharedColumns`` and a column's ``sharedAttribute`` are read
    by nothing. A ``check`` is generated only through a FLUX ``generates`` setting
    (a difference over days), so a contract ``check`` with none, and a FLUX-side
    ``check``, determine no column and are not enforced on the rows. Each is listed
    once, in a fixed order. A declaration this version refuses (``FLX-6210`` and its
    kin) is not listed: ``plan`` raised before this is read.
    """
    prefix = f"{where}: " if where else ""
    target = nfc(expose_id)
    entry = declared_exposes(contract_document).get(target)
    if entry is None:
        return ()
    found = _shared_declarations(entry, target)
    invariants = _invariants(entry)
    entries = _flux_entries(flux_rules, f"{prefix}emit {target!r}")
    if invariants or entries:
        ids = [str(invariant["id"]) for invariant in invariants]
        declared_settings = _settings_for(settings, ids, where=f"{prefix}emit {target!r}")
        model = model_from_contract(contract_document)
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


def _refuse_two_sources(rules: Sequence[PlannedRule], mapping: Optional[Mapping[str, Any]]) -> None:
    by_column: Dict[str, List[PlannedRule]] = {}
    for rule in rules:
        for column in rule.determines:
            by_column.setdefault(column, []).append(rule)
    for column, sources in by_column.items():
        origins = {rule.origin for rule in sources}
        if len(origins) > 1:
            by_origin = {
                origin: sorted(rule.id for rule in sources if rule.origin == origin)
                for origin in origins
            }
            raise Expr2Error(
                FLX_6203,
                f"column {column!r} has more than one source: the contract's rules "
                f"{by_origin[ORIGIN_CONTRACT]} and the FLUX-side rules {by_origin[ORIGIN_BUNDLE]}",
            )
        derived = [rule for rule in sources if rule.form == FORM_DERIVE]
        generated = [rule for rule in sources if rule.form != FORM_DERIVE]
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

    Every rule this version generates reads only the row, so ``follow``,
    ``previous``, ``children`` and ``siblings`` are never reached.
    """

    __slots__ = ("_index", "_values", "row")

    def __init__(self, index: Mapping[str, int], values: List[Any], row: int) -> None:
        self._index = index
        self._values = values
        self.row = row

    @property
    def key(self) -> Hashable:
        return self.row

    def get(self, column: str) -> Any:
        return self._values[self._index[column]]

    def follow(self, relationship: str) -> None:  # pragma: no cover - never reached
        raise NotImplementedError("a rule that reads another row is refused at plan time")

    def previous(self) -> None:  # pragma: no cover - never reached
        raise NotImplementedError("a rule that reads another row is refused at plan time")

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


def _apply_step(step: _Step, view: _Values, values: List[Any], row: int) -> None:
    for item in step.items:
        if item.when is not None and not item.when.evaluate(view):
            continue
        # The first rule in declaration order that covers the row decides it.
        if item.value is not None:
            values[step.position] = _set(step, item.value.evaluate(view), row)
            return
        assert item.k is not None and item.x is not None
        k, x = item.k.evaluate(view), item.x.evaluate(view)
        if k is None or x is None or not isinstance(k, Fraction) or k.denominator != 1:
            return  # an empty interval keeps the value the column's own source gave it
        try:
            moved = x + timedelta(days=item.sign * int(k))
        except OverflowError:
            return
        if not _has_weight(step.plan, moved):
            return
        values[step.position] = moved
        return


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


class _Pass:
    """What one :func:`apply_rules` call needs for dependencies: the run root and a small memo."""

    def __init__(self, plan: "GeneratorPlan", root: Optional["SeedTreeRoot"]) -> None:
        self.plan = plan
        self.root = root
        self.memo: Dict[Tuple[str, str, str], Any] = {}
        #: The key of each dependency for the row being set, which its columns share, and
        #: which of its ``to`` columns are NULL in the row when it draws them per row.
        self.row_keys: Dict[str, str] = {}
        self.row_nulls: Dict[str, Tuple[bool, ...]] = {}
        self._reader: Any = None

    def reader(self) -> Any:
        if self._reader is None:
            from flux_engine.generator.relations import RelationReader

            assert self.root is not None
            self._reader = RelationReader(self.root)
        return self._reader


def _apply_dependency(step: _Step, values: List[Any], context: _Pass, row: int) -> None:
    """Set *step*'s ``to`` column to its key's value, on the rows the dependency covers.

    A dependency that draws its NULLs per row first draws, once for row *row*, whether the row
    is a NULL row and which of its ``to`` columns are NULL there (EXPR-SPEC-2 section 8).
    """
    from flux_engine.generator import rows as _rows
    from flux_engine.generator.paths import dependency_stream

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
    memo_key = (dependency.reference, key, step.column)
    if memo_key in context.memo:
        values[step.position] = context.memo[memo_key]
        return
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
    values[step.position] = value


def _run_root(context: _Pass) -> "SeedTreeRoot":
    """The run root a dependency draws its streams from, which a plan with one needs."""
    if context.root is None:
        raise FluxError(
            FLX_2202, "a plan with a dependency is generated over the run root: pass root="
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
) -> Tuple[Tuple[Any, ...], ...]:
    """*rows*, which are rows *start* onward of *plan*, with every determined column set.

    *root* is the run root a dependency draws its streams from; a plan with none needs no root.
    """
    rules = plan.rules
    assert rules is not None
    compiled = rules._compiled(plan)
    context = _Pass(plan, root)
    out: List[Tuple[Any, ...]] = []
    for offset, row in enumerate(rows):
        values = list(row)
        view = _Values(compiled.index, values, start + offset)
        context.row_keys.clear()
        context.row_nulls.clear()
        for step in compiled.steps:
            if step.dependency is not None:
                _apply_dependency(step, values, context, start + offset)
            else:
                _apply_step(step, view, values, start + offset)
        out.append(tuple(values))
    return tuple(out)
