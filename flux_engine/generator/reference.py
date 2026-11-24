# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A reference expose: the rows a profile publishes whole (ADR 0023 section 2, KERNEL-SPEC section 8.2).

An emit whose block declares ``reference: true`` draws nothing. Its plan has one row per row the
pinned profile publishes for the expose (PROFILE-SPEC.md section 5b), in published order, and each
column's value at row ``i`` is the published cell read as the contract column's type. A cell is
written as EXPR-SPEC-2 section 8 writes a key's value: a number as canonical decimal text, a date
as ``YYYY-MM-DD``, a timestamp as its milliseconds since the epoch, a bool as ``true`` or
``false``, a string as stored, and ``null`` for NULL.

Every column gets a :class:`ReferenceColumn` driver in the plan's relations, so the relation
reader treats the expose as it treats any related one: a child's foreign key picks a row of it,
a rule reads its rows through a link, and the plan's ``generatorDigest`` covers the published
values by their digest. The bytes of the emit do not depend on the seed.

Everything that cannot hold is refused before a row exists, ``FLX-3403``: a contract column that
is not published and a published column the contract does not declare, a cell that is not its
column's type or does not fit it, a key that repeats or is NULL, and a relationship that points
at a row no reference expose holds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.canonical import canonical_bytes, sha256_hex
from flux_engine.core.errors import FluxError
from flux_engine.generator import rows as _rows
from flux_engine.generator.families import ColumnType
from flux_engine.generator.statistics import ColumnStatistics
from flux_engine.seam.contract import CompiledColumn, CompiledExpose, LogicalKind
from flux_engine.seam.emit import frame_rows
from flux_engine.seam.keys import ContractKeys

__all__ = [
    "REFERENCE_LAYOUT",
    "ReferenceColumn",
    "ReferenceRows",
    "build_reference_plan",
    "check_reference_exposes",
    "is_reference_plan",
    "reference_rows",
]

#: The layout a reference column's plan entry names.
REFERENCE_LAYOUT = "reference/1"


def _refuse(message: str) -> FluxError:
    # Imported here: relations imports this module for the driver.
    from flux_engine.generator import relations

    return FluxError(relations.FLX_3403, message)


@dataclass(frozen=True)
class ReferenceRows:
    """The rows one expose's profile publishes, as published: column names and cell texts.

    ``rows`` holds one cell per column, in ``columns`` order, each a string or ``None``. This is
    what a plan records (``plan.json``) and what ``verify`` reads back, so a re-run and a check
    never need the profile.
    """

    columns: Tuple[str, ...]
    rows: Tuple[Tuple[Optional[str], ...], ...]

    def as_document(self) -> Dict[str, Any]:
        return {"columns": list(self.columns), "rows": [list(row) for row in self.rows]}

    @classmethod
    def from_document(cls, document: Any, *, where: str) -> "ReferenceRows":
        """Read what :meth:`as_document` wrote; anything else is ``FLX-3105`` naming *where*."""
        from flux_engine.seam.resolve import FLX_3105

        columns = document.get("columns") if isinstance(document, Mapping) else None
        rows = document.get("rows") if isinstance(document, Mapping) else None
        if (
            not isinstance(columns, list)
            or not isinstance(rows, list)
            or not all(isinstance(name, str) for name in columns)
            or not all(
                isinstance(row, list)
                and len(row) == len(columns)
                and all(cell is None or isinstance(cell, str) for cell in row)
                for row in rows
            )
        ):
            raise FluxError(FLX_3105, f"{where}: the recorded published rows are not readable")
        return cls(tuple(columns), tuple(tuple(row) for row in rows))

    @classmethod
    def of(cls, table: Any) -> "ReferenceRows":
        """The rows of a profile's :class:`flux_stats.profile.ReferenceTable` (or its record)."""
        return cls(tuple(table.columns), tuple(tuple(row) for row in table.rows))


@dataclass(frozen=True)
class ReferenceColumn:
    """One column of a reference expose: the published value at each row.

    ``values`` holds the typed value of each published row (``None`` for NULL), ``digest`` is
    the sha256 of the column's cell texts, which is all the plan document records of them.
    """

    column: str
    values: Tuple[Any, ...]
    digest: str
    _index: Dict[Any, int] = field(init=False, repr=False, compare=False, hash=False)

    def __post_init__(self) -> None:
        index: Dict[Any, int] = {}
        for row, value in enumerate(self.values):
            if value is not None:
                index.setdefault(value, row)
        object.__setattr__(self, "_index", index)

    @property
    def rows(self) -> int:
        return len(self.values)

    def value_at(self, row: int) -> Any:
        return self.values[row]

    def holds(self, value: Any) -> bool:
        """Whether some published row holds *value*."""
        return value in self._index

    def row_near(self, value: Any) -> int:
        """The first published row holding *value*: a ``declared`` pick takes the row whose key it names."""
        found = self._index.get(value)
        if found is None:
            raise _refuse(
                f"reference column {self.column!r} holds no row with the key a declared "
                "foreign key drew; every declared level is a key the reference expose publishes"
            )
        return found

    def as_document(self) -> Dict[str, Any]:
        return {
            "kind": "reference",
            "layout": REFERENCE_LAYOUT,
            "column": self.column,
            "rows": len(self.values),
            "digest": self.digest,
        }


def _digest_of(texts: Sequence[Optional[str]]) -> str:
    return "sha256:" + sha256_hex(canonical_bytes(list(texts)))


def _read_cell(column: CompiledColumn, text: Optional[str], where: str) -> Any:
    """One published cell as the contract column's value, or ``FLX-3403``."""
    if text is None:
        return None
    kind = column.logical_type.kind
    spelled = f"{where}: column {column.name!r} ({column.type_name})"
    if kind is LogicalKind.STRING:
        return text
    if kind is LogicalKind.BOOL:
        if text not in ("true", "false"):
            raise _refuse(f"{spelled} publishes a cell that is not true or false")
        return text == "true"
    if kind is LogicalKind.DATE:
        try:
            return date.fromisoformat(text)
        except ValueError:
            raise _refuse(f"{spelled} publishes a cell that is not a date, YYYY-MM-DD") from None
    if kind in (LogicalKind.INT, LogicalKind.TIMESTAMP, LogicalKind.DECIMAL):
        try:
            number = Decimal(text)
        except InvalidOperation:
            raise _refuse(f"{spelled} publishes a cell that is not a number") from None
        if not number.is_finite():
            raise _refuse(_NOT_CANONICAL.format(spelled=spelled))
        try:
            return _read_number(column, kind, number, text, spelled)
        except ArithmeticError:
            raise _refuse(
                f"{spelled} publishes a number outside the range its type can hold; publish "
                "a value within the column's precision"
            ) from None
    raise _refuse(
        f"{spelled} cannot be published whole; a reference column is STRING, INT, DECIMAL, "
        "BOOL, DATE or TIMESTAMP"
    )


_NOT_CANONICAL = (
    "{spelled} publishes a number that is not in canonical decimal text (no "
    "exponent, no leading or trailing zeros, no plus sign)"
)


def _read_number(
    column: CompiledColumn, kind: LogicalKind, number: Decimal, text: str, spelled: str
) -> Any:
    """A finite published number as the column's value; ``ArithmeticError`` when it is out of range.

    The checks run in a context wide enough for the text itself, so a legal ``DECIMAL(38,2)`` cell
    is neither rounded nor refused by the ambient 28 digits.
    """
    scale = column.logical_type.scale or 0
    precision = column.logical_type.precision
    if (
        kind is LogicalKind.DECIMAL
        and precision
        and number != 0
        and number.adjusted() >= precision - scale
    ):
        raise _refuse(
            f"{spelled} publishes a number with more digits than its precision {precision}"
        )
    with localcontext() as context:
        context.prec = len(text) + scale + 40
        if text != _canonical(number):
            raise _refuse(_NOT_CANONICAL.format(spelled=spelled))
        if kind is LogicalKind.DECIMAL:
            if number != number.quantize(Decimal(1).scaleb(-scale)):
                raise _refuse(f"{spelled} publishes a number finer than its scale {scale}")
            return _rows._quantise(number, column)
        if number != number.to_integral_value():
            raise _refuse(f"{spelled} publishes a number that is not whole")
        return int(number)


def _canonical(number: Decimal) -> str:
    """*number* in canonical decimal text: no exponent, no trailing zeros, ``0`` for zero."""
    if number == 0:
        return "0"
    text = format(number.normalize(), "f")
    return text


def cell_text(column: CompiledColumn, value: Any) -> str:
    """The published text of a non-NULL *value* of *column*: what :func:`_read_cell` read it from."""
    kind = column.logical_type.kind
    if kind is LogicalKind.BOOL:
        return "true" if value else "false"
    if kind is LogicalKind.DATE:
        return str(value.isoformat())
    if kind is LogicalKind.DECIMAL:
        return _canonical(Decimal(value))
    return str(value)


def reference_levels(
    column: CompiledColumn, driver: ReferenceColumn
) -> Optional[Tuple[Tuple[str, Any], ...]]:
    """The distinct published values of a reference column, as ``(level, value)``, in published order.

    A level is the value's published text. ``None`` when a row publishes NULL, which has no level.
    """
    found: Dict[str, Any] = {}
    for value in driver.values:
        if value is None:
            return None
        found.setdefault(cell_text(column, value), value)
    return tuple(found.items())


def reference_rows(
    expose: CompiledExpose, table: ReferenceRows, *, where: str
) -> Tuple[Tuple[Any, ...], ...]:
    """The published rows as typed rows in the contract's column order (``FLX-3403`` when they cannot be).

    Every contract column is published and every published column is a contract column, every
    cell reads as its column's type, and every row fits the contract (what the seam renders).
    """
    published = {name: position for position, name in enumerate(table.columns)}
    contract = expose.column_names
    for name in contract:
        if name not in published:
            raise _refuse(
                f"{where}: contract column {name!r} is not published; a reference expose "
                "publishes every column the contract declares"
            )
    for name in table.columns:
        if name not in contract:
            raise _refuse(f"{where}: published column {name!r} is not a column of the contract")
    positions = [published[name] for name in contract]
    typed = tuple(
        tuple(
            _read_cell(column, row[position], f"{where} row {index}")
            for column, position in zip(expose.columns, positions, strict=True)
        )
        for index, row in enumerate(table.rows)
    )
    try:
        frame_rows(list(typed), expose)
    except FluxError as error:
        raise _refuse(
            f"{where}: a published row does not fit the contract ({error.code.code}); "
            "publish values the columns can hold"
        ) from error
    return typed


def _column_type(column: CompiledColumn) -> ColumnType:
    kind = column.logical_type.kind
    if kind is LogicalKind.STRING:
        return ColumnType.STRING_CATEGORICAL
    if kind is LogicalKind.INT:
        return ColumnType.INT
    if kind is LogicalKind.DECIMAL:
        return ColumnType.DECIMAL
    if kind is LogicalKind.BOOL:
        return ColumnType.BOOL
    return ColumnType.DATE_TIMESTAMP


def build_reference_plan(
    expose: CompiledExpose,
    *,
    sim_id: str,
    seam_digest: str,
    table: ReferenceRows,
    generator_version: str,
    where: str,
) -> "_rows.GeneratorPlan":
    """The plan of a reference expose: one row per published row, every column a published column."""
    from flux_engine.generator.relations import Relations

    typed = reference_rows(expose, table, where=where)
    position = {name: index for index, name in enumerate(table.columns)}
    drivers: List[Tuple[str, ReferenceColumn]] = []
    plans: List[_rows.ColumnPlan] = []
    for index, column in enumerate(expose.columns):
        drivers.append(
            (
                column.name,
                ReferenceColumn(
                    column=column.name,
                    values=tuple(row[index] for row in typed),
                    digest=_digest_of([row[position[column.name]] for row in table.rows]),
                ),
            )
        )
        plans.append(
            _rows.ColumnPlan(
                column=column,
                column_type=_column_type(column),
                family=None,
                statistics=ColumnStatistics(column=column.name),
            )
        )
    return _rows.GeneratorPlan(
        sim_id=sim_id,
        expose=expose,
        seam_digest=seam_digest,
        rows=len(typed),
        columns=tuple(plans),
        relations=Relations(drivers=tuple(drivers)),
        generator_version=generator_version,
    )


def is_reference_plan(plan: "_rows.GeneratorPlan") -> bool:
    """Whether *plan* is a reference expose's: every column is a published column."""
    relations = plan.relations
    return (
        relations is not None
        and bool(relations.drivers)
        and all(isinstance(driver, ReferenceColumn) for _, driver in relations.drivers)
    )


def _key_of(plan: "_rows.GeneratorPlan", columns: Sequence[str], row: int) -> Tuple[Any, ...]:
    assert plan.relations is not None  # a reference plan
    found = []
    for name in columns:
        driver = plan.relations.driver(name)
        assert isinstance(driver, ReferenceColumn)  # every column of a reference plan is one
        found.append(driver.value_at(row))
    return tuple(found)


def check_reference_exposes(plans: Mapping[str, "_rows.GeneratorPlan"], keys: ContractKeys) -> None:
    """Refuse (``FLX-3403``) a reference expose whose keys repeat, are NULL, or point nowhere.

    Each key the contract declares for a reference expose is unique over its rows and never NULL.
    A relationship it declares must name another reference expose, and that expose holds every
    key the rows of this one use (a NULL key references no row).
    """
    reference = {name: plan for name, plan in plans.items() if is_reference_plan(plan)}
    for expose_id in sorted(reference):
        plan = reference[expose_id]
        declared = keys.expose(expose_id)
        for key in declared.keys:
            seen = set()
            for row in range(plan.rows):
                value = _key_of(plan, key, row)
                if any(part is None for part in value):
                    raise _refuse(
                        f"reference expose {expose_id!r} key ({', '.join(key)}) is NULL on row {row}; a "
                        "published key holds a value on every row"
                    )
                if value in seen:
                    raise _refuse(
                        f"reference expose {expose_id!r} key ({', '.join(key)}) repeats on row {row}; a "
                        "published key is unique over the rows"
                    )
                seen.add(value)
        for relationship in declared.relationships:
            parent = reference.get(relationship.target)
            if parent is None:
                raise _refuse(
                    f"reference expose {expose_id!r} relationship {relationship.name!r} targets "
                    f"{relationship.target!r}, which is not a reference expose; a reference "
                    "expose points only at another reference expose"
                )
            held = {_key_of(parent, relationship.to_columns, row) for row in range(parent.rows)}
            for row in range(plan.rows):
                value = _key_of(plan, relationship.from_columns, row)
                if all(part is not None for part in value) and value not in held:
                    raise _refuse(
                        f"reference expose {expose_id!r} relationship {relationship.name!r} "
                        f"names a key on row {row} that {relationship.target!r} does not "
                        "publish; publish every key a reference row uses"
                    )
