# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``--scale F`` and ``--shard I/N``: the size of a run, and the share of it one process makes.

**Scale.** ``--scale F`` multiplies every emit's declared ``rows``, the ``records``
bounds of its fidelity block, and the span of every integer or decimal key column that
declares a range or bins (a key, or a column of a relationship: :func:`~flux_engine.generator.relations.related_columns`),
so a related set of tables grows together and its keys still fit its rows. A span
grows from its low end: ``min`` stays, and ``max`` becomes ``min + (max - min + 1) * F - 1``
for an integer column; a bin edge moves in proportion to its distance from ``min``, rounded
as that end is and held to it. A table whose rows come from the run's own events (it is not
drawn from statistics) has no declared size to multiply, and is left as it is. A column's
declared levels are not scaled: a foreign key that takes the nearest key its parent holds follows the
parent's cut keys, and a history's tracked foreign key, which writes its level as the key, keeps its drawn parent at the
fewest rows whose keys hold every level (:func:`floored_rows`, noted by ``plan`` and recorded as ``scaleFloors``), and is refused at
plan (``FLX-3403``, naming the parent's keys in the scaled plan) when no row count does.
``F = 1`` changes nothing, and writes nothing to the plan, so every digest of a bundle
planned without it is the one it was. Any other value is recorded in ``plan.json`` as
``scale``, and ``apply`` and ``replay`` read it back from there, as they read the kernel.
The scaled declarations are what the run, its fidelity input and its attestations carry.

**Shard.** ``--shard I/N`` generates the ``I``-th of ``N`` contiguous shares of every emit's
rows. The shares, joined in order, are the bytes of the emit one run writes: the share of
row ``r`` is decided by ``r`` alone, rows are addressed by their index, and each line is
framed as one process frames it. An emit that is not drawn from statistics (its rows come
from the run's own moments) has no row range to cut and belongs whole to the first share.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.errors import FluxError
from flux_engine.core.sizing import (
    FLX_3404,
    FLX_3405,
    MOST_ROWS,
    MOST_SCALE,
    optional_scale,
    parse_scale,
    parse_shard,
    scale_text,
    share_of,
)
from flux_engine.generator.conditions import scaled_counts
from flux_engine.seam.conditions import CONDITIONS_KEY
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.history import HISTORY_KEY
from flux_engine.seam.resolve import ResolvedEmit

__all__ = [
    "FLX_3404",
    "FLX_3405",
    "MOST_SCALE",
    "ScaleFloor",
    "child_rows_note",
    "key_space_note",
    "optional_scale",
    "parse_scale",
    "parse_shard",
    "floor_for_cut",
    "scale_emit",
    "scale_emits",
    "scale_text",
    "scaled_rows",
    "share_of",
    "too_many_child_rows_note",
    "widened_columns",
]


def _scaled_rows(rows: int, scale: Decimal) -> int:
    scaled = int((Decimal(rows) * scale).to_integral_value(ROUND_HALF_UP))
    scaled = max(1, scaled) if rows > 0 else 0
    if scaled > MOST_ROWS:
        raise FluxError(FLX_3405, f"{rows} rows at scale {scale_text(scale)} is {scaled} rows")
    return scaled


#: *rows* at *scale*, as the plan rounds them (half up, and at least one row of a table that has any).
scaled_rows = _scaled_rows


def _scaled_span(low: Decimal, high: Decimal, scale: Decimal, *, whole: bool) -> Decimal:
    """*high* moved so that the span from *low* is *scale* times as long."""
    if whole:
        return low + ((high - low + 1) * scale).to_integral_value(ROUND_HALF_UP) - 1
    return low + (high - low) * scale


def _edge(text: Any, low: Decimal, scale: Decimal, whole: bool) -> Decimal:
    edge = Decimal(str(text))
    moved = low + (edge - low) * scale
    if whole:
        moved = moved.to_integral_value(ROUND_HALF_UP)
    return moved


def _scaled_edges(
    edges: List[Any], low: Decimal, high: Decimal, end: Decimal, scale: Decimal, whole: bool
) -> List[str]:
    """*edges* moved as the span is, none past the span's new end; ``ValueError`` when two meet.

    A bin edge is cut as the range's end is, from the range's low end; a rounded edge can land
    past the end the span's own rounding gives (the last edge, which sits at the end, most often),
    so every edge is held to that end. Edges that then meet leave a bin with no width.
    """
    moved = [
        # A whole-number range counts its last value, so an edge at the declared high is the
        # range's end once scaled; moving it as an interior edge stops it short of that end.
        end if whole and Decimal(str(edge)) == high else min(_edge(edge, low, scale, whole), end)
        for edge in edges
    ]
    if any(later <= earlier for earlier, later in zip(moved, moved[1:], strict=False)):
        raise ValueError("the bins meet")
    return [format(edge, "f") for edge in moved]


def _scaled_bins(
    name: str,
    bins: List[Any],
    bounds: Tuple[Decimal, Decimal, Decimal],
    scale: Decimal,
    whole: bool,
) -> Optional[List[str]]:
    """The key column's bin edges grown from the low end, or ``None`` when an edge is no number."""
    low, high, end = bounds
    try:
        return _scaled_edges(bins, low, high, end, scale, whole)
    except InvalidOperation:
        return None
    except ValueError:
        raise FluxError(
            FLX_3405,
            f"{scale_text(scale)} leaves the key column {name} fewer values than its "
            f"{len(bins) - 1} {'bin' if len(bins) == 2 else 'bins'}; use a larger --scale",
        ) from None


def _range_bounds(declared_range: Mapping[str, Any]) -> Optional[Tuple[Decimal, Decimal]]:
    """The low and high ends of a declared range, or ``None`` when either is no number."""
    try:
        return Decimal(str(declared_range["min"])), Decimal(str(declared_range["max"]))
    except InvalidOperation:
        return None


def _scale_column(
    name: str, declared: Mapping[str, Any], kind: LogicalKind, scale: Decimal
) -> Mapping[str, Any]:
    """One key column's declaration with its range and bins grown from their low end."""
    if kind not in (LogicalKind.INT, LogicalKind.DECIMAL):
        return declared
    whole = kind is LogicalKind.INT
    result: Dict[str, Any] = dict(declared)
    declared_range = declared.get("range")
    if isinstance(declared_range, Mapping) and {"min", "max"} <= set(declared_range):
        ends = _range_bounds(declared_range)
        if ends is None:
            return declared
        low, high = ends
        end = _scaled_span(low, high, scale, whole=whole)
        if end <= low:
            raise FluxError(
                FLX_3405,
                f"{scale_text(scale)} leaves the key column {name} (range {declared_range['min']} "
                f"to {declared_range['max']}) with fewer than two values; use a larger --scale",
            )
        result["range"] = {**declared_range, "max": format(end, "f")}
        bins = declared.get("bins")
        if isinstance(bins, list):
            edges = _scaled_bins(name, bins, (low, high, end), scale, whole)
            if edges is None:
                return declared
            result["bins"] = edges
    return result


def child_rows_note(scale: Decimal) -> str:
    """What a scaled plan adds to the refusal of child rows too few for a ``minChildren`` (``FLX-3403``)."""
    return (
        f"--scale {scale_text(scale)} left too few child rows for the minimum: it scales the child "
        "and the parent rows alike but rounds each on its own, so raise the scale, lower minChildren "
        "or declare more child rows"
    )


def too_many_child_rows_note(scale: Decimal) -> str:
    """What a scaled plan adds to the refusal of more child rows than a ``one_to_one`` parent has (``FLX-3403``)."""
    return (
        f"--scale {scale_text(scale)} left more child rows than parents: it scales the child "
        "and the parent rows alike but rounds each on its own, so raise the scale or declare "
        "fewer child rows"
    )


def key_space_note(scale: Decimal) -> str:
    """What a scaled plan adds to a key's refusal for too few values (``FLX-3403``).

    Below 1 the scale cuts the declared range of every integer or decimal key column as it
    cuts the rows, so a key over two or more of them loses values faster than rows. Above 1
    it multiplies the rows, and only those columns and keys into a parent's rows grow with
    them, so a key over none of them (levels, text or dates) keeps its values. Each case says
    which way to move the scale.
    """
    text = scale_text(scale)
    if scale < 1:
        return (
            f"--scale {text} cuts the declared range of every integer or decimal key column as "
            "it cuts the rows, so a key over two or more of them loses values faster than rows: "
            "plan at a larger --scale"
        )
    return (
        f"--scale {text} multiplies the rows, and only an integer or decimal key column with a "
        "declared range, or a key into a parent's rows, grows with them: widen the key's other "
        "columns, or plan at a smaller --scale"
    )


def _scale_condition_counts(extension: Dict[str, Any], scale: Decimal) -> None:
    """Scale the counts of an emit's condition groups in *extension*, when every group has one.

    The groups' rows are the emit's rows, so each count scales by the largest remainder and the
    counts add up to the scaled total (ADR 0028 section 10).
    """
    groups = extension.get(CONDITIONS_KEY)
    if not (
        isinstance(groups, list)
        and groups
        and all(
            isinstance(group, Mapping) and isinstance(group.get("count"), int) for group in groups
        )
    ):
        return
    counts = scaled_counts(
        [group["count"] for group in groups],
        _scaled_rows(sum(group["count"] for group in groups), scale),
    )
    extension[CONDITIONS_KEY] = [
        {**group, "count": count} for group, count in zip(groups, counts, strict=True)
    ]


def _natural_columns(resolved: ResolvedEmit) -> FrozenSet[str]:
    """The natural entity's columns of an emit that declares a history, which ``entities`` scales."""
    history = (resolved.extension or {}).get(HISTORY_KEY)
    if not isinstance(history, Mapping) or not isinstance(history.get("entities"), int):
        return frozenset()
    if resolved.keys is None:
        return frozenset()
    return frozenset(entity.column for entity in resolved.keys.entities if entity.kind == "natural")


def _scale_fidelity(
    resolved: ResolvedEmit, fidelity: Mapping[str, Any], scale: Decimal
) -> Dict[str, Any]:
    """The ``fidelity`` block with its ``records`` bounds and its key columns' ranges scaled."""
    block: Dict[str, Any] = dict(fidelity)
    records = block.get("records")
    if isinstance(records, Mapping):
        block["records"] = {
            key: (
                _scaled_rows(value, scale)
                if key in ("min", "max") and isinstance(value, int) and not isinstance(value, bool)
                else value
            )
            for key, value in records.items()
        }
    columns = block.get("columns")
    if isinstance(columns, Mapping):
        block["columns"] = _scaled_columns(resolved, columns, scale)
    return block


def _scaled_columns(
    resolved: ResolvedEmit, columns: Mapping[str, Any], scale: Decimal
) -> Dict[str, Any]:
    """The declarations of *columns* with each key column's range and bins grown (:func:`_scale_column`)."""
    from flux_engine.generator.relations import related_columns

    kinds: Dict[str, LogicalKind] = {
        column.name: column.logical_type.kind for column in resolved.compile().columns
    }
    keyed = related_columns(resolved.keys) | _natural_columns(resolved)
    return {
        name: (
            _scale_column(name, declared, kinds[name], scale)
            if name in keyed and name in kinds and isinstance(declared, Mapping)
            else declared
        )
        for name, declared in columns.items()
    }


def widened_columns(resolved: ResolvedEmit, scale: Decimal) -> FrozenSet[str]:
    """The columns of *resolved* that *scale* widens: a key column whose declared range it grows.

    Only above 1 (below it the scale cuts a key's range and the contract's range still holds the
    keys), and only a key column whose fidelity declares a range that :func:`_scale_column` grows,
    so ``plan``, which scales, and ``verify``, which reads the contract against the run, name the
    same columns. *resolved* is the emit as the bundle declares it, before :func:`scale_emit`.
    """
    fidelity = (resolved.extension or {}).get("fidelity")
    columns = fidelity.get("columns") if isinstance(fidelity, Mapping) else None
    if scale <= 1 or not isinstance(columns, Mapping):
        return frozenset()
    scaled = _scaled_columns(resolved, columns, scale)
    return frozenset(name for name, declared in columns.items() if scaled[name] != declared)


def scale_emit(resolved: ResolvedEmit, scale: Decimal) -> ResolvedEmit:
    """*resolved* with its rows, ``records`` bounds and key spans multiplied by *scale*."""
    if scale == 1 or resolved.extension is None:
        return resolved
    extension: Dict[str, Any] = dict(resolved.extension)
    rows = resolved.declared_row_count
    if rows is not None:
        extension["rows"] = _scaled_rows(rows, scale)
    _scale_condition_counts(extension, scale)
    history = extension.get(HISTORY_KEY)
    if isinstance(history, Mapping) and isinstance(history.get("entities"), int):
        extension[HISTORY_KEY] = {
            **history,
            "entities": _scaled_rows(history["entities"], scale),
        }
    fidelity = extension.get("fidelity")
    if isinstance(fidelity, Mapping):
        extension["fidelity"] = _scale_fidelity(resolved, fidelity, scale)
    return replace(resolved, extension=extension)


# ---------------------------------------------------------------------------
# The keys a tracked level names (ADR 0017 section 6, the amendment of 2026-11-25)
# ---------------------------------------------------------------------------

#: The most row counts :func:`floored_rows` tries above the first that fits the widest level.
_FLOOR_SEARCH_LIMIT = 100_000


@dataclass(frozen=True)
class ScaleFloor:
    """A drawn parent kept at *rows* rows, the fewest whose key grid holds every level a history tracks.

    ``tracked_by`` names each ``expose.column`` of the histories whose tracked foreign key writes
    those levels as its cell.
    """

    expose_id: str
    rows: int
    tracked_by: Tuple[str, ...]
    levels: Tuple[str, ...]
    key: str

    def note(self, scale: Decimal) -> str:
        """The line ``plan`` prints for the floor."""
        who = ", ".join(self.tracked_by)
        return (
            f"note: {self.expose_id} keeps {self.rows} rows at --scale {scale_text(scale)}, "
            f"the fewest that hold every level {who} tracks"
        )


def _effective_scale(rows: int, declared: int) -> Decimal:
    """``rows / declared`` as a scale that rounds the exact ties up, as the exact ratio does."""
    with localcontext() as context:
        context.prec = 60
        context.rounding = ROUND_CEILING
        return Decimal(rows) / Decimal(declared)


def _grid_holds(first: int, width: int, rows: int, level: int) -> bool:
    """Whether row ``k`` of *rows* takes ``first + k * width // rows`` for the key *level*."""
    offset = level - first
    if offset < 0 or offset >= width or rows > width:
        return False
    row = -((-offset * rows) // width)
    return row < rows and (row * width) // rows == offset


def _integral(text: str) -> Optional[int]:
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return int(value) if value == value.to_integral_value() else None


def _declared_key_range(resolved: ResolvedEmit, column: str) -> Optional[Tuple[int, int, int]]:
    """The low and high of a whole-number key's declared range and the emit's rows, or ``None``.

    ``None`` for what a floor is not worked out for: a history's emit, an emit with no declared
    rows, a column that is not an integer with a plain range (bins, a date, a decimal).
    """
    extension = resolved.extension or {}
    fidelity = extension.get("fidelity")
    columns = fidelity.get("columns") if isinstance(fidelity, Mapping) else None
    declared = columns.get(column) if isinstance(columns, Mapping) else None
    rows = resolved.declared_row_count
    if HISTORY_KEY in extension or rows is None or rows < 1 or not isinstance(declared, Mapping):
        return None
    kinds = {c.name: c.logical_type.kind for c in resolved.compile().columns}
    plain = declared.get("range")
    if kinds.get(column) is not LogicalKind.INT or "bins" in declared:
        return None
    if not isinstance(plain, Mapping) or not {"min", "max"} <= set(plain):
        return None
    low, high = _integral(str(plain["min"])), _integral(str(plain["max"]))
    return None if low is None or high is None or high < low else (low, high, rows)


def _scaled_width(low: int, high: int, declared: int, rows: int) -> Optional[int]:
    """How many values the key's range holds when the emit is scaled to *rows* rows, or ``None``."""
    scale = _effective_scale(rows, declared)
    if _scaled_rows(declared, scale) != rows:
        return None
    end = _scaled_span(Decimal(low), Decimal(high), scale, whole=True)
    return int(end) - low + 1 if end > low else None


def floored_rows(
    resolved: ResolvedEmit, column: str, levels: Sequence[str], scale: Decimal
) -> Optional[int]:
    """The fewest rows of *resolved*, more than its rows at *scale*, whose key grid holds every level.

    A floor always raises the rows: the layout at the scaled count is the one that missed a level,
    so the search starts one row above it.

    The emit is scaled to those rows (rows, key spans and records in proportion), so the rows
    and the range the grid is laid over move together, as they do under any ``--scale``. A key
    grid of one range over whole numbers: ``None`` for any other layout, for a level that is no
    whole number or no key of the range at any count, and when no count within
    :data:`_FLOOR_SEARCH_LIMIT` of the first that reaches the widest level holds them all.
    """
    found = _declared_key_range(resolved, column)
    wanted = [_integral(level) for level in levels]
    if found is None or not wanted or any(level is None for level in wanted):
        return None
    low, high, declared = found
    held = [level for level in wanted if level is not None]
    widest = max(held) - low + 1
    start = _scaled_rows(declared, scale) + 1

    def width(rows: int) -> int:
        return _scaled_width(low, high, declared, rows) or 0

    if start > declared or min(held) < low or width(declared) < widest:
        return None
    first, last = start, declared  # the first count whose range reaches the widest level
    while first < last:
        middle = (first + last) // 2
        first, last = (first, middle) if width(middle) >= widest else (middle + 1, last)
    for rows in range(first, min(declared, first + _FLOOR_SEARCH_LIMIT) + 1):
        span = width(rows)
        if span >= widest and all(_grid_holds(low, span, rows, level) for level in held):
            return rows
    return None


def floor_for_cut(
    emits: Sequence[ResolvedEmit],
    floors: Mapping[str, ScaleFloor],
    cut: Any,
    scale: Decimal,
) -> Optional[Dict[str, ScaleFloor]]:
    """*floors* with the parent a refused tracked foreign key names kept at the rows its levels need.

    *emits* are the bundle's emits before ``scale``; *cut* is the refusal
    (:class:`~flux_engine.generator.relations.TrackedLevelsCut`). ``None`` when no floor holds:
    the plan is refused as it was, and is also when the refusal repeats one the floor already
    answers.
    """
    asked = cut.tracked
    parent = next((emit for emit in emits if emit.expose_id == asked.parent), None)
    named = f"{asked.child}.{asked.column}"
    held = floors.get(asked.parent)
    if scale >= 1 or parent is None or (held is not None and named in held.tracked_by):
        return None
    levels = tuple(sorted({*(held.levels if held else ()), *asked.levels}))
    rows = floored_rows(parent, asked.key, levels, scale)
    if rows is None:
        return None
    tracked = (*(held.tracked_by if held else ()), named)
    return {**floors, asked.parent: ScaleFloor(asked.parent, rows, tracked, levels, asked.key)}


def scale_emits(
    emits: Sequence[ResolvedEmit], scale: Decimal, floors: Mapping[str, ScaleFloor]
) -> Tuple[ResolvedEmit, ...]:
    """Each of *emits* scaled by *scale*, a floored parent as if its rows were the floor's."""
    return scale_emits_to_rows(emits, scale, {name: floor.rows for name, floor in floors.items()})


def scale_emits_to_rows(
    emits: Sequence[ResolvedEmit], scale: Decimal, kept: Mapping[str, int]
) -> Tuple[ResolvedEmit, ...]:
    """Each of *emits* scaled by *scale*, an emit named in *kept* as if its rows were those.

    The one place a run's scaled emits are made: ``plan`` and ``apply`` give it the parents their
    floors keep, and ``ship`` the rows the recorded plan holds for them.
    """
    return tuple(
        scale_emit(
            emit,
            (
                _effective_scale(kept[emit.expose_id], emit.declared_row_count or 1)
                if emit.expose_id in kept
                else scale
            ),
        )
        for emit in emits
    )


def kept_rows_of_plan(
    emits: Sequence[ResolvedEmit], scale: Decimal, planned: Mapping[str, Any]
) -> Dict[str, int]:
    """The rows a recorded plan kept for each drawn parent a floor raised above its scaled rows.

    *planned* is the plan's ``emits`` member, whose ``generator.rows`` is what each drawn emit made. A floor always raises rows, so an emit whose recorded
    rows exceed its rows at *scale* is a floored one.
    """
    kept: Dict[str, int] = {}
    for emit in emits:
        entry = planned.get(emit.expose_id)
        generator = entry.get("generator") if isinstance(entry, Mapping) else None
        rows = generator.get("rows") if isinstance(generator, Mapping) else None
        declared = emit.declared_row_count
        if isinstance(rows, int) and declared and rows > _scaled_rows(declared, scale):
            kept[emit.expose_id] = rows
    return kept
