# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The property checks: each one a pure function of a JSON-able input.

A check takes the *input* a counterexample file stores and either returns or raises
``AssertionError``. Returning ``False`` means the input was vacuous (the engine
refused one side of a comparison, so there is nothing to compare); returning
``True`` or ``"ok"``/``"refused"`` means the property was actually checked.

Hypothesis tests build the input, hand it to the check inside
``saving_counterexample``, and so every failing example is already in the form the
regression replay reads. Nothing here imports pytest or hypothesis.
"""

from __future__ import annotations

import decimal
import re
from datetime import date
from decimal import ROUND_UP, Context, Decimal, localcontext
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from flux_engine.core.errors import FluxError
from flux_engine.core.rng import DrawStream, exponential, truncated_normal
from flux_engine.generator.rows import GeneratorPlan, build_plan, generate_column, generate_rows
from flux_engine.generator.statistics import DATE_EPOCH_ORDINAL
from flux_engine.runtime.signals import ArrivalModel
from flux_engine.seam.conform import render_rows
from flux_engine.seam.contract import LogicalKind, compile_expose
from tests.accuracy.properties import _support as support

__all__ = [
    "CHECKS",
    "check_ambient_decimal_context",
    "check_arrival_gap_scaling",
    "check_column_order",
    "check_exponential_scaling",
    "check_fluid_columns",
    "check_generator_added_column",
    "check_generator_same_bytes",
    "check_persona_spec",
    "check_prefix_stable",
    "check_truncated_normal_equivariance",
    "check_world_added_trait",
]

_HEX = re.compile(r"^[0-9a-f]+$")


def hostile_context() -> Context:
    """A decimal context chosen to break any arithmetic that leaks out of its own."""
    return Context(prec=5, rounding=ROUND_UP)


def _exact(prec: int = 80) -> Context:
    return Context(prec=prec, rounding=decimal.ROUND_HALF_EVEN)


def _by_name(plan: GeneratorPlan, rows: Sequence[Sequence[Any]]) -> Dict[str, Tuple[str, ...]]:
    """Each column's value sequence, as ``repr`` so ``Decimal('1.0')`` and ``'1.00'`` differ."""
    names = [item.column.name for item in plan.columns]
    return {name: tuple(repr(row[index]) for row in rows) for index, name in enumerate(names)}


# ---------------------------------------------------------------------------
# MR1: the same seed gives the same bytes (generator level)
# ---------------------------------------------------------------------------


def check_generator_same_bytes(inp: Mapping[str, Any]) -> bool:
    """Two generations under one root, and one under a hostile ambient context, agree."""
    seed, rows = int(inp["seed"]), int(inp["rows"])
    first = generate_rows(support.base_plan(rows), root=support.root_of(seed))
    second = generate_rows(support.base_plan(rows), root=support.root_of(seed))
    assert repr(first) == repr(second), "two generations under one root differ"
    with localcontext(hostile_context()):
        plan = support.base_plan(rows)
        hostile = generate_rows(plan, root=support.root_of(seed))
        hostile_digest = plan.digest().value
    assert repr(first) == repr(hostile), "an ambient decimal context moved a generated value"
    assert (
        hostile_digest == support.base_plan(rows).digest().value
    ), "an ambient decimal context moved generatorDigest"
    return True


def check_ambient_decimal_context(inp: Mapping[str, Any]) -> bool:
    """The population and the signal stage do not depend on the ambient ``decimal`` context.

    Materialises a small population (Persona traits included) and emits its signals
    once in the default context and once with ``prec``/``rounding`` from *inp*
    installed as the ambient context, and compares traits, event times and event
    payload digests. A crash inside the second run is a failure too: an ambient
    context that makes the engine raise is as much a dependence on it as one that
    moves a value.
    """
    seed, size = int(inp["seed"]), int(inp["size"])
    prec, rounding = int(inp["prec"]), str(inp["rounding"])

    def observe() -> Tuple[Any, ...]:
        small = support.small_world()
        store = support.population_of(small.persona, seed=seed, size=size)
        events = support.emit_small(seed=seed, size=size, arrival=support.default_arrival())
        traits = tuple((name, repr(store.column(f"traits.{name}"))) for name in store.trait_names)
        return traits, tuple(
            (e.signal_id, e.ordinal, e.occurrence, e.t_ms, e.payload_digest, e.id) for e in events
        )

    plain = observe()
    with localcontext(Context(prec=prec, rounding=getattr(decimal, rounding))):
        hostile = observe()
    assert plain[0] == hostile[0], f"a trait moved under ambient prec={prec} {rounding}"
    assert plain[1] == hostile[1], f"a signal moved under ambient prec={prec} {rounding}"
    return True


# ---------------------------------------------------------------------------
# MR2: column order does not change any column's values (fixed root)
# ---------------------------------------------------------------------------


def check_column_order(inp: Mapping[str, Any]) -> bool:
    """Permuting the declared column order leaves each column's values unchanged.

    Level: generator, under a fixed seam digest ``H`` and a fixed root. End to end the
    root moves by design, because ``H`` binds the column order (KERNEL-SPEC §3), and
    ``generatorDigest`` moves with it, which is asserted too.
    """
    seed, rows = int(inp["seed"]), int(inp["rows"])
    order: List[str] = list(inp["order"])
    base, permuted = support.base_plan(rows), support.base_plan(rows, order=order)
    root = support.root_of(seed)
    base_rows, perm_rows = generate_rows(base, root=root), generate_rows(permuted, root=root)
    base_cols, perm_cols = _by_name(base, base_rows), _by_name(permuted, perm_rows)
    assert set(base_cols) == set(perm_cols)
    for name, values in base_cols.items():
        assert values == perm_cols[name], f"column {name!r} changed when the order changed"
        column_first = tuple(repr(v) for v in generate_column(permuted, name, root=root))
        assert column_first == values, f"generate_column({name!r}) disagrees under the new order"
    if order != list(support.base_names()):
        assert (
            base.digest().value != permuted.digest().value
        ), "generatorDigest did not move with the column order"
    return True


# ---------------------------------------------------------------------------
# MR3: dwell scaling
# ---------------------------------------------------------------------------


def check_exponential_scaling(inp: Mapping[str, Any]) -> bool:
    """``exponential(rate=r/c)`` equals ``c * exponential(rate=r)`` on one stream.

    Both draws read the same raw word, so before quantisation the two values differ
    by exactly the factor ``c`` (up to the working precision). Each is rounded to
    ``10**-scale``, a half-unit error of at most ``0.5 * 10**-scale`` on the left
    and ``c * 0.5 * 10**-scale`` on the right: the bound is their sum.
    """
    entropy, scale = int(inp["entropy"]), int(inp["scale"])
    rate, factor = Decimal(str(inp["rate"])), Decimal(str(inp["c"]))
    with localcontext(_exact()):
        slow = rate / factor
    wide = exponential(DrawStream.from_entropy(entropy), rate=slow, scale=scale)
    narrow = exponential(DrawStream.from_entropy(entropy), rate=rate, scale=scale)
    with localcontext(_exact()):
        half_ulp = Decimal(1).scaleb(-scale) / 2
        bound = (1 + factor) * half_ulp + Decimal("1e-30") * (abs(wide) + 1)
        difference = abs(wide - factor * narrow)
    assert difference <= bound, (
        f"exponential(rate/c)={wide} but c*exponential(rate)={factor * narrow}; "
        f"difference {difference} exceeds the rounding bound {bound}"
    )
    return True


def check_truncated_normal_equivariance(inp: Mapping[str, Any]) -> bool:
    """Location-scale equivariance of ``truncated_normal`` on one stream.

    With ``a`` a shift and ``b > 0`` a scale, the draw for ``(mean * b + a, sd * b,
    lo * b + a, hi * b + a)`` equals ``b * x + a`` where ``x`` is the draw for
    ``(mean, sd, lo, hi)``: the standardised bounds, and so the underlying uniform
    mapping, are identical. The two results are each rounded to ``10**-scale`` and
    clamped to bounds rounded *inward* to that grid, which can move a clamped value
    by up to one unit, so the bound is ``(1 + b) * 10**-scale``.

    Vacuous when either side refuses (the inward-rounded interval is empty on one
    side only, which the rounding makes legitimate).
    """
    entropy, scale = int(inp["entropy"]), int(inp["scale"])
    with localcontext(_exact()):
        mean, sd = Decimal(inp["mean"]), Decimal(inp["stddev"])
        lo, hi = Decimal(inp["lo"]), Decimal(inp["hi"])
        shift, spread = Decimal(inp["a"]), Decimal(inp["b"])
        mapped = (mean * spread + shift, sd * spread, lo * spread + shift, hi * spread + shift)
    try:
        plain = truncated_normal(
            DrawStream.from_entropy(entropy), mean=mean, stddev=sd, lo=lo, hi=hi, scale=scale
        )
        moved = truncated_normal(
            DrawStream.from_entropy(entropy),
            mean=mapped[0],
            stddev=mapped[1],
            lo=mapped[2],
            hi=mapped[3],
            scale=scale,
        )
    except FluxError:
        return False
    with localcontext(_exact()):
        bound = (1 + spread) * Decimal(1).scaleb(-scale) + Decimal("1e-20") * (abs(moved) + 1)
        difference = abs(moved - (spread * plain + shift))
    assert difference <= bound, (
        f"truncated_normal is not location-scale equivariant: {moved} vs "
        f"{spread * plain + shift} (difference {difference}, bound {bound})"
    )
    return True


def _chains(events: Sequence[Any]) -> Dict[Tuple[str, int], List[Any]]:
    chains: Dict[Tuple[str, int], List[Any]] = {}
    for event in events:
        chains.setdefault((event.signal_id, event.ordinal), []).append(event)
    for chain in chains.values():
        chain.sort(key=lambda event: event.occurrence)
    return chains


def check_arrival_gap_scaling(inp: Mapping[str, Any]) -> bool:
    """Doubling ``gap_mean_ms`` doubles each within-burst gap, up to rounding.

    A gap is ``max(int(exponential(rate=1/gap_mean_ms, scale=0)), 1)``: the same raw
    word, so ``round(2x)`` against ``2 * round(x)``, which never differ by more than
    1 (the floor at 1 keeps that: ``x < 0.5`` gives gaps ``1`` and ``1``, and
    ``|1 - 2| = 1``). The emitters, the burst starts and the payloads do not depend
    on the gap mean and must be identical; a burst may only get shorter, because
    longer gaps reach the end of the window sooner.
    """
    seed, size = int(inp["seed"]), int(inp["size"])
    factor = int(inp.get("factor", 2))
    assert factor == 2, "the rounding bound proved in the docstring is for a factor of 2"
    gap = Decimal(str(inp["gap_mean_ms"]))
    base = ArrivalModel(
        emitter_share=Decimal("0.6"), extra_occurrences_mean=Decimal("2.5"), gap_mean_ms=gap
    )
    slow = ArrivalModel(
        emitter_share=base.emitter_share,
        extra_occurrences_mean=base.extra_occurrences_mean,
        gap_mean_ms=gap * factor,
    )
    first = _chains(support.emit_small(seed=seed, size=size, arrival=base))
    second = _chains(support.emit_small(seed=seed, size=size, arrival=slow))
    assert set(first) == set(second), "doubling the gap mean changed who emits"
    compared = 0
    for key, chain in first.items():
        other = second[key]
        assert 1 <= len(other) <= len(chain), f"{key}: a slower burst is longer, not shorter"
        assert other[0].t_ms == chain[0].t_ms, f"{key}: the burst start moved"
        for index in range(len(other)):
            assert (
                other[index].payload_digest == chain[index].payload_digest
            ), f"{key}: payload {index} moved with the gap mean"
        for index in range(1, len(other)):
            gap_first = chain[index].t_ms - chain[index - 1].t_ms
            gap_second = other[index].t_ms - other[index - 1].t_ms
            assert (
                abs(gap_second - factor * gap_first) <= 1
            ), f"{key}: gap {index} is {gap_second}, expected 2 * {gap_first} within 1"
            compared += 1
    return compared > 0


# ---------------------------------------------------------------------------
# MR4: more rows keep the marginals
# ---------------------------------------------------------------------------


def check_prefix_stable(inp: Mapping[str, Any]) -> bool:
    """Under a fixed root the first ``n`` rows of a ``2n``-row generation are the ``n`` rows."""
    seed, rows = int(inp["seed"]), int(inp["n"])
    root = support.root_of(seed)
    small, large = support.base_plan(rows), support.base_plan(2 * rows)
    small_rows, large_rows = generate_rows(small, root=root), generate_rows(large, root=root)
    assert repr(small_rows) == repr(large_rows[:rows]), "the first n rows changed with n"
    for column in support.base_names():
        assert repr(generate_column(small, column, root=root)) == repr(
            generate_column(large, column, root=root)[:rows]
        ), f"column {column!r}: the first n values changed with n"
    return True


# ---------------------------------------------------------------------------
# MR5: adding a column / a trait leaves its siblings untouched
# ---------------------------------------------------------------------------


def check_generator_added_column(inp: Mapping[str, Any]) -> bool:
    """Adding a column, anywhere in the order, leaves every other column's values unchanged.

    Level: generator, under a fixed ``H`` and root. End to end, adding a column to the
    contract changes ``H`` and so every stream, by design.
    """
    seed, rows = int(inp["seed"]), int(inp["rows"])
    name, position = str(inp["name"]), int(inp["position"])
    column_spec, statistics = support.EXTRA_COLUMNS[str(inp["spec"])]
    schema = [dict(column) for column in support.BASE_SCHEMA]
    schema.insert(min(position, len(schema)), {"name": name, **column_spec})
    root = support.root_of(seed)
    base = support.base_plan(rows)
    grown = support.plan_of(schema, {**support.BASE_FIDELITY_COLUMNS, name: statistics}, rows=rows)
    base_cols = _by_name(base, generate_rows(base, root=root))
    grown_cols = _by_name(grown, generate_rows(grown, root=root))
    assert name in grown_cols and name not in base_cols
    for column, values in base_cols.items():
        assert grown_cols[column] == values, f"column {column!r} changed when {name!r} was added"
    return True


def check_world_added_trait(inp: Mapping[str, Any]) -> bool:
    """Adding a Persona trait leaves every other trait of every subject unchanged.

    Level: ``materialise_population`` under a fixed root. Personas are not in ``H``
    (KERNEL-SPEC §3), so the same holds end to end; that is checked separately
    through the small bundle.
    """
    seed, size = int(inp["seed"]), int(inp["size"])
    name, declaration = str(inp["name"]), inp["declaration"]
    small = support.small_world()
    assert name not in small.persona["spec"]["traits"]
    grown = {**small.persona, "spec": {**small.persona["spec"]}}
    grown["spec"]["traits"] = {**small.persona["spec"]["traits"], name: declaration}
    before = support.population_of(small.persona, seed=seed, size=size)
    after = support.population_of(grown, seed=seed, size=size)
    assert name in after.trait_names and name not in before.trait_names
    for column in ("id", "lifecycle", "persona", "market"):
        assert before.column(column) == after.column(column), f"{column} changed"
    for trait in before.trait_names:
        assert repr(before.column(f"traits.{trait}")) == repr(
            after.column(f"traits.{trait}")
        ), f"trait {trait!r} changed when {name!r} was added"
    for derived in before.derived_names:
        assert repr(before.column(derived)) == repr(
            after.column(derived)
        ), f"derived column {derived!r} changed"
    return True


# ---------------------------------------------------------------------------
# Schema-driven properties: refuse in type, or conform
# ---------------------------------------------------------------------------


def _conforms(plan: GeneratorPlan, rows: Sequence[Sequence[Any]]) -> None:
    for index, item in enumerate(plan.columns):
        column, stats, kind = item.column, item.statistics, item.column.logical_type.kind
        for row_index, row in enumerate(rows):
            value = row[index]
            where = f"column {column.name!r} row {row_index}: {value!r}"
            if value is None:
                assert stats.null_fraction is not None, f"{where}: a null nobody declared"
                continue
            if item.column_type.name == "IDENTIFIER":
                assert isinstance(value, str) and _HEX.match(value.replace("-", "")), where
                continue
            if kind is LogicalKind.INT:
                assert type(value) is int, where
            elif kind is LogicalKind.DECIMAL:
                assert isinstance(value, Decimal), where
                scale = column.logical_type.scale or 0
                assert value.as_tuple().exponent == -scale, f"{where}: not at scale {scale}"
                precision = column.logical_type.precision or 0
                assert len(value.as_tuple().digits) <= precision, f"{where}: over precision"
            elif kind is LogicalKind.BOOL:
                assert type(value) is bool, where
            elif kind is LogicalKind.STRING:
                assert isinstance(value, str), where
            elif kind is LogicalKind.TIMESTAMP:
                assert type(value) is int, where
            elif kind is LogicalKind.DATE:
                assert isinstance(value, date), where
            number = _as_number(value, kind)
            if stats.has_range and stats.levels is None and not stats.support:
                assert stats.range_min <= number <= stats.range_max, f"{where}: outside range"
            if stats.levels is not None and kind not in (LogicalKind.STRING, LogicalKind.BOOL):
                assert number in {
                    Decimal(key) for key in stats.levels
                }, f"{where}: undeclared level"
            if stats.levels is not None and kind is LogicalKind.STRING:
                assert value in stats.levels, f"{where}: undeclared level"
            if stats.support:
                assert value in stats.support, f"{where}: outside the declared support"
            if kind is LogicalKind.INT and stats.rate is None and stats.mean is not None:
                assert value >= 0, f"{where}: a negative count"


def _as_number(value: Any, kind: LogicalKind) -> Any:
    if kind is LogicalKind.DATE:
        return Decimal(value.toordinal() - DATE_EPOCH_ORDINAL)
    if isinstance(value, (bool, str)):
        return value
    return Decimal(value)


def check_fluid_columns(inp: Mapping[str, Any]) -> str:
    """A schema-valid FLUID column list: a typed refusal, or rows that conform.

    Returns ``"refused"`` or ``"ok"``. Any exception that is not a
    :class:`FluxError` propagates unchanged, with a note naming the stage.
    """
    expose = {
        "exposeId": support.EXPOSE_ID,
        "kind": "stream",
        "contract": {"schema": inp["columns"]},
        "binding": {"platform": "kafka", "format": "kafka_topic", "location": {"topic": "t"}},
    }
    stage = "compile_expose"
    try:
        compiled = compile_expose(expose)
        stage = "build_plan"
        plan = build_plan(
            compiled,
            sim_id=support.SIM_ID,
            seam_digest=support.H,
            rows=int(inp.get("rows", 4)),
            fidelity={"columns": inp["fidelity"]},
            time_bounds=support.TIME_BOUNDS,
        )
        stage = "generate_rows"
        rows = generate_rows(plan, root=support.root_of(int(inp["seed"])))
    except FluxError:
        return "refused"
    except Exception as exc:
        # Not a FluxError: the property fails with the engine's own exception type, so a
        # known defect can be pinned by the exact type it raises.
        exc.add_note(f"{stage} raised {type(exc).__name__}, not a FluxError")
        raise
    _conforms(plan, rows)
    try:
        render_rows(rows, compiled)
    except FluxError as exc:
        raise AssertionError(f"the seam renderer refused a generated row: {exc}") from exc
    return "ok"


def check_persona_spec(inp: Mapping[str, Any]) -> str:
    """A schema-valid Persona ``spec``: a typed refusal, or a population that conforms."""
    from flux_engine.world.families import BoundedNormal, Categorical, Constant

    small = support.small_world()
    persona = {**small.persona, "spec": inp["spec"]}
    stage = "materialise_population"
    try:
        store = support.population_of(persona, seed=int(inp["seed"]), size=int(inp.get("size", 6)))
    except FluxError:
        return "refused"
    except Exception as exc:
        # Not a FluxError: the property fails with the engine's own exception type, so a
        # known defect can be pinned by the exact type it raises.
        exc.add_note(f"{stage} raised {type(exc).__name__}, not a FluxError")
        raise
    from flux_engine.world.population import PersonaSpec

    families = PersonaSpec.from_document(persona).traits
    for name, family in families.items():
        for value in store.column(f"traits.{name}"):
            where = f"trait {name!r}: {value!r}"
            if isinstance(family, Constant):
                assert value == family.value, where
                assert Decimal(0) <= value <= Decimal(1), where
            elif isinstance(family, Categorical):
                assert value in family.levels, where
            elif isinstance(family, BoundedNormal):
                assert isinstance(value, Decimal) and family.lo <= value <= family.hi, where
                assert value.as_tuple().exponent == -family.scale, where
    for name in persona["spec"].get("worldview") or {}:
        column = store.column(f"worldview.{name}")
        assert all(item is not None for item in column), f"worldview {name!r} is missing"
    return "ok"


#: Property name -> check, for the regression replay.
CHECKS = {
    "mr1_generator_same_bytes": check_generator_same_bytes,
    "mr1_ambient_decimal_context": check_ambient_decimal_context,
    "mr2_column_order": check_column_order,
    "mr3_exponential_scaling": check_exponential_scaling,
    "mr3_truncated_normal_equivariance": check_truncated_normal_equivariance,
    "mr3_arrival_gap_scaling": check_arrival_gap_scaling,
    "mr4_prefix_stable": check_prefix_stable,
    "mr5_generator_added_column": check_generator_added_column,
    "mr5_world_added_trait": check_world_added_trait,
    "schema_fluid_columns": check_fluid_columns,
    "schema_flux_persona": check_persona_spec,
}
