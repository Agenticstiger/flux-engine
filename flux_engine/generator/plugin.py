# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A generator plugin generates the other columns of an expose, and the engine checks them (ADR 0032 section 5).

For an expose the bundle declares under ``generators``, the engine derives the identifier and key
columns and draws the foreign keys exactly as for any expose. The plugin generates every other
column, for rows ``start`` to ``stop`` of ``n``, once per expose (per ``--shard`` range), in the
engine's own process and never split by ``--jobs``.

:func:`plugin_expose` says which columns those are, from the contract, the declared statistics and
the keys alone, so the answer is the same before a plan exists and after it. It also holds what the
bundle declares each column's values must satisfy: its type, its nullability, its range, its length
and its levels. :func:`generate_values` calls the plugin and checks what it returns, refusing
(``FLX-3121``) the first failure by naming the plugin, the column and the row, and repairing nothing.
:meth:`PluginExpose.overlay` then puts the values into the rows the engine drew for the other
columns, before any rule is applied, and :func:`check_rows` holds the final rows to the contract's
checks. The engine renders every line itself, with its own renderer, so the bytes have its
canonical form whatever the plugin returned.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.clock import parse_rfc3339_ms
from flux_engine.core.errors import FluxError
from flux_engine.generator import rows as _rows
from flux_engine.generator.families import is_identifier
from flux_engine.generator.statistics import DATE_EPOCH_ORDINAL, ColumnStatistics
from flux_engine.generators.errors import FLX_3121
from flux_engine.generators.request import ColumnRequest, Request
from flux_engine.seam.conform import render_cell, render_row
from flux_engine.seam.contract import CompiledColumn, CompiledExpose, LogicalKind, LogicalType
from flux_stats.fidelity import split_type_params

__all__ = [
    "PluginColumn",
    "PluginExpose",
    "check_rows",
    "constraints_of",
    "declared_length",
    "declared_levels",
    "generate_values",
    "plugin_column_names",
    "plugin_expose",
    "request_of",
]

#: How many rows the checks of the contract read at a time.
_CHUNK = 4096


def plugin_column_names(
    expose: CompiledExpose,
    statistics: Mapping[str, ColumnStatistics],
    related: FrozenSet[str],
) -> Tuple[str, ...]:
    """The columns of *expose* a plugin generates, in declared order.

    Every column that is not a key or a foreign key (*related*, from
    :func:`flux_engine.generator.relations.related_columns`) and is not an identifier column
    (:func:`flux_engine.generator.families.is_identifier`): the engine derives those.
    """
    return tuple(
        column.name
        for column in expose.columns
        if column.name not in related and not is_identifier(column, statistics[column.name])
    )


def declared_length(column: CompiledColumn, statistics: ColumnStatistics) -> Optional[int]:
    """The most characters a string column's value may have, or ``None`` when it declares none.

    From the length the declared type states (``varchar(20)``) and the ``identifierLength`` the
    fidelity block declares for the column; the smaller of the two when both are declared.
    """
    if column.logical_type.kind is not LogicalKind.STRING:
        return None
    bounds: List[int] = []
    _, parameters = split_type_params(" ".join(column.declared_type.strip().lower().split()))
    if parameters is not None and parameters.strip().isdecimal() and int(parameters) >= 1:
        bounds.append(int(parameters))
    if statistics.identifier_length is not None:
        bounds.append(statistics.identifier_length)
    return min(bounds) if bounds else None


def declared_levels(
    column: CompiledColumn, statistics: ColumnStatistics
) -> Optional[Tuple[str, ...]]:
    """The values a column may take, as text, or ``None`` when its declarations name no set.

    The listed ``support`` (or the declared type's own ``enum(...)`` levels it takes), else the
    keys of declared ``levels``, else the levels the declared type names.
    """
    if statistics.support is not None:
        return tuple(statistics.support)
    if statistics.levels is not None:
        return tuple(sorted(statistics.levels))
    enum = column.enum_values
    return tuple(enum) if enum else None


def constraints_of(column: CompiledColumn, statistics: ColumnStatistics) -> Dict[str, Any]:
    """What the bundle declares a plugin's values for *column* must satisfy, as plain data.

    ``range`` as the bundle wrote it, ``levels`` and ``length``; a key is present only when the
    column declares it.
    """
    constraints: Dict[str, Any] = {}
    if statistics.has_range:
        constraints["range"] = {
            "min": statistics.range_min_text or str(statistics.range_min),
            "max": statistics.range_max_text or str(statistics.range_max),
        }
    levels = declared_levels(column, statistics)
    if levels is not None:
        constraints["levels"] = list(levels)
    length = declared_length(column, statistics)
    if length is not None:
        constraints["length"] = length
    return constraints


@dataclass(frozen=True)
class PluginColumn:
    """One column a plugin generates: what it is asked for, and what its values are held to.

    ``bounds`` is the declared range on the column's grid (days since the epoch for a ``DATE``,
    milliseconds for a ``TIMESTAMP``, the number itself otherwise), ``levels`` the rendered values
    the column may take and ``length`` the most characters of a string; each is ``None`` when the
    column declares none.
    """

    request: ColumnRequest
    position: int
    logical_type: LogicalType
    bounds: Optional[Tuple[Decimal, Decimal]] = None
    levels: Optional[FrozenSet[Any]] = None
    length: Optional[int] = None

    @property
    def name(self) -> str:
        return self.request.name


@dataclass(frozen=True)
class PluginExpose:
    """The columns of one expose a plugin generates, in declared order."""

    columns: Tuple[PluginColumn, ...]

    @property
    def names(self) -> Tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    @property
    def requests(self) -> Tuple[ColumnRequest, ...]:
        return tuple(column.request for column in self.columns)

    def as_document(self) -> Dict[str, Any]:
        """The record the plan's ``generatorDigest`` is taken over: which columns the plugin makes."""
        return {"columns": list(self.names)}

    def overlay(
        self,
        rows: Sequence[Tuple[Any, ...]],
        values: Optional[Mapping[str, Sequence[Any]]],
    ) -> Tuple[Tuple[Any, ...], ...]:
        """*rows* with the plugin's columns set to *values*, the rows the engine drew for the rest.

        The values are the ones :func:`generate_values` returned for the same range of rows.
        """
        if values is None:
            raise FluxError(
                _rows.FLX_3402,
                "this plan's expose is generated by a plugin, whose values are given to the row pass",
            )
        positions = [(column.position, values[column.name]) for column in self.columns]
        out: List[Tuple[Any, ...]] = []
        for index, row in enumerate(rows):
            cells = list(row)
            for position, column_values in positions:
                cells[position] = column_values[index]
            out.append(tuple(cells))
        return tuple(out)


def _grid_of(logical: LogicalType, value: Any) -> Optional[Decimal]:
    """The rendered cell *value* on the grid a declared range is on, or ``None`` for a NULL."""
    if value is None:
        return None
    kind = logical.kind
    if kind is LogicalKind.DATE:
        return Decimal(date.fromisoformat(value).toordinal() - DATE_EPOCH_ORDINAL)
    if kind is LogicalKind.TIMESTAMP:
        return Decimal(parse_rfc3339_ms(value))
    return Decimal(value)


def plugin_expose(
    expose: CompiledExpose,
    statistics: Mapping[str, ColumnStatistics],
    related: FrozenSet[str],
) -> PluginExpose:
    """The columns of *expose* a plugin generates, each with what its values are held to."""
    names = plugin_column_names(expose, statistics, related)
    columns: List[PluginColumn] = []
    for position, column in enumerate(expose.columns):
        if column.name not in names:
            continue
        declared = statistics[column.name]
        levels: Optional[FrozenSet[Any]] = None
        listed = declared_levels(column, declared)
        if listed is not None:
            with localcontext(_rows._KERNEL_CONTEXT):
                levels = frozenset(
                    render_cell(_rows._level_to_value(level, column), column.logical_type)
                    for level in listed
                )
        columns.append(
            PluginColumn(
                request=ColumnRequest(
                    name=column.name,
                    type=column.type_name,
                    nullable=not column.required,
                    constraints=constraints_of(column, declared),
                ),
                position=position,
                logical_type=column.logical_type,
                bounds=(
                    (declared.range_min, declared.range_max)
                    if declared.range_min is not None and declared.range_max is not None
                    else None
                ),
                levels=levels,
                length=declared_length(column, declared),
            )
        )
    return PluginExpose(tuple(columns))


def request_of(
    plugin: PluginExpose,
    expose_id: str,
    *,
    n: int,
    seed: bytes,
    options: Mapping[str, Any],
    start: int = 0,
    stop: Optional[int] = None,
) -> Request:
    """The request for rows *start* to *stop* (the whole expose by default) of *plugin*'s columns."""
    return Request(
        expose_id=expose_id,
        columns=plugin.requests,
        n=n,
        start=start,
        stop=n if stop is None else stop,
        seed=seed,
        options=options,
    )


# ---------------------------------------------------------------------------
# What a plugin returns
# ---------------------------------------------------------------------------


def _is_list(value: Any) -> bool:
    return isinstance(value, (list, tuple))


def _checker(column: PluginColumn) -> Callable[[Any], Optional[str]]:
    """A function that says why a value is refused for *column*, or ``None`` when it is accepted.

    The reasons name a class or a bound and never the value: a plugin's values may be exactly the
    kind of data nobody promised was safe to print.
    """
    spec = column.request
    logical = column.logical_type
    bounds, levels, length = column.bounds, column.levels, column.length
    kind = logical.kind

    def check(value: Any) -> Optional[str]:
        if value is None:
            return None if spec.nullable else "is NULL, and the column is required"
        try:
            rendered = render_cell(value, logical)
        except FluxError as error:
            return f"is not a {spec.type} value ({error.detail or error.code.title})"
        if bounds is not None:
            try:
                position = _grid_of(logical, rendered)
            except (ValueError, InvalidOperation, FluxError):  # pragma: no cover - rendered above
                return f"is not a {spec.type} value"
            if position is not None and not bounds[0] <= position <= bounds[1]:
                return "lies outside the range the bundle declares for the column"
        if levels is not None and rendered not in levels:
            return "is not one of the levels the column may take"
        if length is not None and kind is LogicalKind.STRING and len(rendered) > length:
            return f"is longer than the {length} characters the column allows"
        return None

    return check


def generate_values(
    plugin: Any, label: str, expose: PluginExpose, request: Request
) -> Dict[str, Tuple[Any, ...]]:
    """Ask *plugin* for the values of rows ``request.start`` to ``request.stop``, and check them.

    *label* names the plugin in a refusal. The plugin returns one list per requested column, in the
    order of ``request.columns``, each of length ``stop - start``. The first failure is refused
    (``FLX-3121``) naming the plugin, the column and the row, and nothing is repaired: a value of
    the wrong type, NULL in a required column, a value outside the declared range or levels or
    longer than the declared length, a list of the wrong length, or no list for a column.
    """
    start, stop = request.start, request.stop
    where = f"{label} for {request.expose_id!r}, rows {start} to {stop}"
    try:
        returned = plugin.generate(request, start, stop)
    except FluxError:
        raise
    except Exception as error:  # noqa: BLE001 (a plugin's own failure is its refusal, by name)
        raise FluxError(FLX_3121, f"{where}: the plugin raised {type(error).__name__}") from error
    wanted = len(expose.columns)
    if not _is_list(returned) or len(returned) != wanted:
        shown = f"{len(returned)} lists" if _is_list(returned) else f"a {type(returned).__name__}"
        raise FluxError(
            FLX_3121,
            f"{where}: the plugin returned {shown}; one list per column ({wanted}) is asked for",
        )
    found: Dict[str, Tuple[Any, ...]] = {}
    for column, values in zip(expose.columns, returned, strict=True):
        if not _is_list(values) or len(values) != stop - start:
            size = f"{len(values)} values" if _is_list(values) else f"a {type(values).__name__}"
            raise FluxError(
                FLX_3121,
                f"{where}: column {column.name!r} came back as {size}; {stop - start} are asked for",
            )
        check = _checker(column)
        for offset, value in enumerate(values):
            reason = check(value)
            if reason is not None:
                raise FluxError(
                    FLX_3121,
                    f"{label} for {request.expose_id!r}: column {column.name!r}, row "
                    f"{start + offset} {reason}",
                )
        found[column.name] = tuple(values)
    return found


# ---------------------------------------------------------------------------
# The contract's checks on the final rows
# ---------------------------------------------------------------------------


def check_rows(
    label: str,
    plan: "_rows.GeneratorPlan",
    rows: Sequence[Tuple[Any, ...]],
    first_row: int,
) -> None:
    """Refuse (``FLX-3121``) the first of *rows* that a ``check`` of the contract or the bundle fails.

    The rows are final: the engine's own columns and the plugin's. A check holds on every row its
    ``when`` covers, and a text that is unknown on a row (a NULL in a comparison) does not hold it,
    as everywhere (EXPR-SPEC-2 section 4). The refusal names the plugin, the rule, the columns the
    rule reads and the row; it quotes no value. *first_row* is the index of ``rows[0]`` in the
    expose.
    """
    rules = plan.rules
    checks = () if rules is None else rules.checks()
    if rules is None or not checks:
        return
    from flux_engine.concordance.rule_shares import stored_rows
    from flux_engine.expr2.rows import Rows

    expose_id = plan.expose.expose_id
    compiled = []
    for rule in checks:
        predicate, when = rules.predicate_of(rule)
        read = sorted({item.column for text in (predicate, when) if text for item in text.reads})
        compiled.append((rule, predicate, when, ", ".join(repr(name) for name in read)))
    names = plan.expose.column_names
    for chunk in range(0, len(rows), _CHUNK):
        part = rows[chunk : chunk + _CHUNK]
        rendered = [
            render_row(row, plan.expose, row_index=first_row + chunk + offset)
            for offset, row in enumerate(part)
        ]
        try:
            table = Rows(
                rules.model, {expose_id: stored_rows(rules.model, expose_id, names, rendered)}
            )
        except (TypeError, ValueError) as error:
            raise FluxError(
                FLX_3121,
                f"{label} for {expose_id!r}: rows {first_row + chunk} onward cannot be read by "
                f"the rules ({type(error).__name__})",
            ) from error
        for offset, view in enumerate(table.rows(expose_id)):
            for rule, predicate, when, reads in compiled:
                if when is not None and not when.evaluate(view):
                    continue
                if not predicate.evaluate(view):
                    raise FluxError(
                        FLX_3121,
                        f"{label} for {expose_id!r}: row {first_row + chunk + offset} does not "
                        f"satisfy rule {rule.id!r}, which reads {reads or 'no column'}",
                    )
