# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""SEAM-FIDELITY-SPEC §1: the rows, and the properties the addressing gives.

The properties pinned here are the ones §1's one sentence about paths
buys, and each has a matching negative control -- a thing that *should*
move the values, asserted to move them -- so a "stable" result is never
stable because nothing was varied.

* **Determinism.** Same plan, same root, same rows. Twice, and from two
  independently constructed stream tables.
* **Order independence.** Generating column-first and row-first give one
  table, because no cell's stream depends on any other cell.
* **Identifiers are not drawn.** An identifier column is a function of
  ``(H, ordinal)`` and is therefore *unchanged* by a new seed, while every
  drawn column moves. This is the pair that makes the claim mean anything.
* **Declared bounds are honoured.** Every drawn value lies inside the
  declared range, at the declared scale, over generated declarations.
* **Everything the generator produces renders.** The values come back in
  the classes KERNEL-SPEC §9 renders, so the seam accepts every row.
"""

from __future__ import annotations

import unicodedata
from contextlib import contextmanager
from datetime import date
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    ROUND_UP,
    Context,
    Decimal,
    getcontext,
    setcontext,
)
from fractions import Fraction
from typing import Any, Dict, List, Set, Tuple

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from flux_engine.core.digest import digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.generator.families import ColumnType, Family
from flux_engine.generator.paths import GeneratorStreams, generator_column_path
from flux_engine.generator.rows import (
    GENERATOR_VERSION,
    IDENTIFIER_DEFAULT_LENGTH,
    GeneratorPlan,
    _common_exponent,
    _grid_units,
    build_plan,
    generate_column,
    generate_rows,
    identifier_value,
)
from flux_engine.generator.statistics import DATE_EPOCH_ORDINAL
from flux_engine.seam.conform import render_rows
from flux_engine.seam.contract import CompiledExpose

from .conftest import (
    EXPOSE_ID,
    OTHER_H,
    SIM_ID,
    TIME_BOUNDS,
    H,
    expose_from_schema,
    fidelity,
    root_for,
)

pytestmark = pytest.mark.unit


FULL_SCHEMA: List[Dict[str, Any]] = [
    {"name": "widget_id", "type": "STRING", "required": True, "semanticType": "identifier"},
    {"name": "market", "type": "STRING"},
    {"name": "count", "type": "INT"},
    {"name": "amount", "type": "DECIMAL(18,4)"},
    {"name": "active", "type": "BOOL"},
    {"name": "seen_on", "type": "DATE"},
    {"name": "seen_at", "type": "TIMESTAMP"},
]

FULL_FIDELITY = fidelity(
    {
        "market": {"support": {"values": ["IE", "GB", "FR", "DE"]}, "maxCardinality": 8},
        "count": {"mean": "3"},
        "amount": {"range": {"min": "0", "max": "10000"}},
        "active": {"levels": {"true": "0.25", "false": "0.75"}},
        "seen_on": {"range": {"min": "2026-07-01", "max": "2026-09-30"}},
        "seen_at": {"range": {"min": "2026-07-01T00:00:00Z", "max": "2026-09-30T23:59:59Z"}},
    }
)


@pytest.fixture
def expose() -> CompiledExpose:
    return expose_from_schema(FULL_SCHEMA)


def _plan(
    expose: CompiledExpose,
    *,
    rows: int = 12,
    seam_digest: str = H,
    block: Any = FULL_FIDELITY,
) -> GeneratorPlan:
    return build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=seam_digest,
        rows=rows,
        fidelity=block,
        time_bounds=TIME_BOUNDS,
    )


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def test_the_plan_resolves_every_column_before_a_single_draw(
    expose: CompiledExpose,
) -> None:
    plan = _plan(expose)
    assert [item.column.name for item in plan.columns] == list(expose.column_names)
    assert [item.column_type for item in plan.columns] == [
        ColumnType.IDENTIFIER,
        ColumnType.STRING_CATEGORICAL,
        ColumnType.INT,
        ColumnType.DECIMAL,
        ColumnType.BOOL,
        ColumnType.DATE_TIMESTAMP,
        ColumnType.DATE_TIMESTAMP,
    ]
    assert [item.family for item in plan.columns] == [
        None,
        Family.CATEGORICAL,
        Family.RATE,
        Family.BOUNDED_NORMAL,
        Family.CATEGORICAL,
        Family.UNIFORM_EMPIRICAL,
        Family.UNIFORM_EMPIRICAL,
    ]


def test_the_plan_fails_before_any_row_is_produced(expose: CompiledExpose) -> None:
    """§12's "compile fails closed", applied to the generator."""
    with pytest.raises(FluxError) as error:
        _plan(expose, block=fidelity({"market": {"support": {"values": ["IE"]}}}))
    assert error.value.code.code == "FLX-3402"


@pytest.mark.parametrize("rows", [-1, True, "5", 1.5, None])
def test_a_row_count_that_is_not_a_count_is_refused(expose: CompiledExpose, rows: Any) -> None:
    with pytest.raises(FluxError) as error:
        _plan(expose, rows=rows)
    assert error.value.code.code == "FLX-3402"


def test_a_zero_row_plan_generates_nothing(expose: CompiledExpose) -> None:
    assert generate_rows(_plan(expose, rows=0), root=root_for()) == ()


def test_the_generator_digest_covers_the_declaration_and_nothing_volatile(
    expose: CompiledExpose,
) -> None:
    plan = _plan(expose)
    document = plan.as_document()
    assert document["generatorVersion"] == GENERATOR_VERSION
    assert document["H"] == H
    assert document["rows"] == plan.rows
    assert [entry["name"] for entry in document["columns"]] == list(expose.column_names)
    assert plan.digest() == digest_of_document(document, subject=plan.digest().subject)
    assert _plan(expose).digest() == plan.digest()


@pytest.mark.parametrize(
    ("what", "changed"),
    [
        ("rows", {"rows": 13}),
        ("H", {"seam_digest": OTHER_H}),
        (
            "a declared parameter",
            {
                "block": fidelity(
                    {**FULL_FIDELITY["columns"], "amount": {"range": {"min": "0", "max": "9999"}}}
                )
            },
        ),
    ],
)
def test_the_generator_digest_moves_when_the_declaration_moves(
    expose: CompiledExpose, what: str, changed: Dict[str, Any]
) -> None:
    """SEAM-FIDELITY-SPEC §8: a moved generator means re-materialising D'."""
    assert _plan(expose).digest() != _plan(expose, **changed).digest(), what


def test_the_plan_records_the_time_bounds_it_used(expose: CompiledExpose) -> None:
    plan = _plan(expose)
    assert plan.time_bounds is not None
    assert plan.as_document()["timeBounds"] == {
        "startMs": plan.time_bounds[0],
        "endMs": plan.time_bounds[1],
    }


def test_a_temporal_column_falls_back_to_the_runs_time_bounds() -> None:
    """§1's default for DATE/TIMESTAMP: "uniform within ``timeBounds``"."""
    expose = expose_from_schema([{"name": "t", "type": "TIMESTAMP"}])
    plan = build_plan(
        expose, sim_id=SIM_ID, seam_digest=H, rows=4, fidelity=None, time_bounds=TIME_BOUNDS
    )
    statistics = plan.columns[0].statistics
    assert statistics.has_range
    # The substituted bounds appear in the digest, so a reader can see them.
    assert plan.as_document()["columns"][0]["statistics"]["range"] is not None


def test_a_temporal_column_with_no_bounds_at_all_is_refused() -> None:
    expose = expose_from_schema([{"name": "t", "type": "TIMESTAMP"}])
    with pytest.raises(FluxError) as error:
        build_plan(expose, sim_id=SIM_ID, seam_digest=H, rows=1)
    assert error.value.code.code == "FLX-3402"


@pytest.mark.parametrize(
    "bounds",
    [
        {"start": "2026-07-01T00:00:00Z"},
        {"start": 0, "end": 1},
        {"start": "2026-09-30T00:00:00Z", "end": "2026-07-01T00:00:00Z"},
    ],
)
def test_malformed_time_bounds_are_refused(bounds: Dict[str, Any]) -> None:
    expose = expose_from_schema([{"name": "t", "type": "TIMESTAMP"}])
    with pytest.raises(FluxError) as error:
        build_plan(expose, sim_id=SIM_ID, seam_digest=H, rows=1, time_bounds=bounds)
    assert error.value.code.code == "FLX-3402"


def test_the_plan_can_be_asked_for_one_column(expose: CompiledExpose) -> None:
    plan = _plan(expose)
    assert plan.column_plan("amount").column.name == "amount"
    with pytest.raises(FluxError) as error:
        plan.column_plan("no-such-column")
    assert error.value.code.code == "FLX-3401"


# ---------------------------------------------------------------------------
# Determinism and addressing
# ---------------------------------------------------------------------------


def test_the_same_plan_and_root_produce_the_same_rows(expose: CompiledExpose) -> None:
    plan = _plan(expose)
    assert generate_rows(plan, root=root_for()) == generate_rows(plan, root=root_for())


def test_two_stream_tables_over_one_root_agree(expose: CompiledExpose) -> None:
    plan = _plan(expose)
    root = root_for()
    assert generate_rows(plan, source=GeneratorStreams(root=root)) == generate_rows(
        plan, source=GeneratorStreams(root=root)
    )


def test_rows_are_produced_in_row_index_order(expose: CompiledExpose) -> None:
    """A longer run's prefix is the shorter run's whole table."""
    root = root_for()
    short = generate_rows(_plan(expose, rows=3), root=root)
    long = generate_rows(_plan(expose, rows=9), root=root_for())
    assert long[: len(short)] == short


@pytest.mark.parametrize("column", [item["name"] for item in FULL_SCHEMA])
def test_column_first_and_row_first_agree(expose: CompiledExpose, column: str) -> None:
    """No cell's stream depends on any other cell."""
    plan = _plan(expose)
    index = expose.index_of[column]
    row_first = tuple(row[index] for row in generate_rows(plan, root=root_for()))
    column_first = generate_column(plan, column, root=root_for())
    assert row_first == column_first


def test_a_different_root_moves_every_drawn_column(expose: CompiledExpose) -> None:
    """The negative control for every determinism assertion above."""
    plan = _plan(expose, rows=20)
    first = generate_rows(plan, root=root_for(seed=1))
    second = generate_rows(plan, root=root_for(seed=2))
    for index, column in enumerate(expose.columns):
        left = [row[index] for row in first]
        right = [row[index] for row in second]
        if column.name == "widget_id":
            continue
        assert left != right, f"{column.name} did not move when the seed did"


def test_every_cell_is_addressed_by_its_own_path(expose: CompiledExpose) -> None:
    plan = _plan(expose, rows=3)
    streams = GeneratorStreams(root=root_for())
    generate_rows(plan, source=streams)
    drawn = {item.column.name for item in plan.columns if item.family is not None}
    expected = {
        generator_column_path(SIM_ID, EXPOSE_ID, index, name)
        for index in range(3)
        for name in drawn
    }
    assert set(streams.materialised_paths) == expected


# ---------------------------------------------------------------------------
# Identifiers: derived from (H, ordinal), never drawn
# ---------------------------------------------------------------------------


def test_an_identifier_column_is_unchanged_by_the_seed(expose: CompiledExpose) -> None:
    """§1: "derived deterministically from (H, ordinal) -- never random"."""
    plan = _plan(expose, rows=8)
    index = expose.index_of["widget_id"]
    first = [row[index] for row in generate_rows(plan, root=root_for(seed=1))]
    second = [row[index] for row in generate_rows(plan, root=root_for(seed=99))]
    assert first == second


def test_an_identifier_column_moves_when_h_moves(expose: CompiledExpose) -> None:
    """The other half: it is a function of H, so a new contract is new ids."""
    index = expose.index_of["widget_id"]
    first = [row[index] for row in generate_rows(_plan(expose), root=root_for())]
    second = [
        row[index] for row in generate_rows(_plan(expose, seam_digest=OTHER_H), root=root_for())
    ]
    assert first != second


def test_identifiers_are_distinct_within_a_column(expose: CompiledExpose) -> None:
    plan = _plan(expose, rows=200)
    index = expose.index_of["widget_id"]
    values: Set[str] = {row[index] for row in generate_rows(plan, root=root_for())}
    assert len(values) == plan.rows


def test_an_identifier_consumes_no_randomness(expose: CompiledExpose) -> None:
    """An identifier column materialises no stream at all."""
    identifier_only = expose_from_schema(
        [{"name": "widget_id", "type": "STRING", "semanticType": "identifier"}]
    )
    plan = build_plan(identifier_only, sim_id=SIM_ID, seam_digest=H, rows=5)
    streams = GeneratorStreams(root=root_for())
    generate_rows(plan, source=streams)
    assert streams.materialised_paths == ()


def test_a_uuid_typed_identifier_is_shaped_like_an_rfc_9562_uuid() -> None:
    expose = expose_from_schema([{"name": "id", "type": "uuid"}])
    plan = build_plan(expose, sim_id=SIM_ID, seam_digest=H, rows=3)
    for value in generate_column(plan, "id", root=root_for()):
        assert isinstance(value, str)
        assert [len(part) for part in value.split("-")] == [8, 4, 4, 4, 12]
        assert value[14] == "8", "RFC 9562 version 8: a vendor-defined derivation"
        assert value[19] in "89ab", "RFC 9562 variant bits"


def test_the_identifier_length_is_declarable() -> None:
    expose = expose_from_schema([{"name": "id", "type": "STRING", "tags": ["identifier"]}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=2,
        fidelity=fidelity({"id": {"identifierLength": 12}}),
    )
    for value in generate_column(plan, "id", root=root_for()):
        assert len(value) == 12
    assert len(
        identifier_value(H, sim_id=SIM_ID, expose_id=EXPOSE_ID, row_index=0, column="c")
    ) == (IDENTIFIER_DEFAULT_LENGTH)


@pytest.mark.parametrize("length", [0, 65, -1])
def test_an_identifier_length_outside_its_domain_is_refused(length: int) -> None:
    with pytest.raises(FluxError) as error:
        identifier_value(
            H, sim_id=SIM_ID, expose_id=EXPOSE_ID, row_index=0, column="c", length=length
        )
    assert error.value.code.code == "FLX-3402"


def test_a_uuid_needs_the_full_128_bits() -> None:
    with pytest.raises(FluxError) as error:
        identifier_value(
            H, sim_id=SIM_ID, expose_id=EXPOSE_ID, row_index=0, column="c", length=16, as_uuid=True
        )
    assert error.value.code.code == "FLX-3402"


def test_an_identifier_column_may_not_declare_a_null_fraction() -> None:
    expose = expose_from_schema([{"name": "id", "type": "uuid"}])
    with pytest.raises(FluxError) as error:
        build_plan(
            expose,
            sim_id=SIM_ID,
            seam_digest=H,
            rows=1,
            fidelity=fidelity({"id": {"nullFraction": "0.5"}}),
        )
    assert error.value.code.code == "FLX-3402"


# ---------------------------------------------------------------------------
# Declared bounds are honoured
# ---------------------------------------------------------------------------


def test_every_drawn_value_lies_inside_its_declared_domain(
    expose: CompiledExpose,
) -> None:
    plan = _plan(expose, rows=120)
    rows = generate_rows(plan, root=root_for())
    columns = {name: index for index, name in enumerate(expose.column_names)}
    for row in rows:
        assert row[columns["market"]] in ("IE", "GB", "FR", "DE")
        assert isinstance(row[columns["count"]], int) and row[columns["count"]] >= 0
        amount = row[columns["amount"]]
        assert Decimal(0) <= amount <= Decimal(10000)
        assert -amount.as_tuple().exponent == 4
        assert row[columns["active"]] in (True, False)
        assert date(2026, 7, 1) <= row[columns["seen_on"]] <= date(2026, 9, 30)
        low = plan.column_plan("seen_at").statistics.range_min
        high = plan.column_plan("seen_at").statistics.range_max
        assert low is not None and high is not None
        assert int(low) <= row[columns["seen_at"]] <= int(high)


@settings(max_examples=40, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(
    low=st.integers(min_value=-500, max_value=0),
    high=st.integers(min_value=1, max_value=500),
    scale=st.integers(min_value=0, max_value=4),
    family=st.sampled_from(["bounded-normal", "uniform"]),
)
def test_a_bounded_draw_stays_inside_a_generated_range(
    low: int, high: int, scale: int, family: str
) -> None:
    expose = expose_from_schema([{"name": "n", "type": f"DECIMAL(18,{scale})"}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=8,
        fidelity=fidelity({"n": {"range": {"min": str(low), "max": str(high)}, "family": family}}),
    )
    for value in generate_column(plan, "n", root=root_for()):
        assert Decimal(low) <= value <= Decimal(high)
        assert -value.as_tuple().exponent == scale


def test_a_declared_stddev_narrows_the_spread() -> None:
    """The declaration is used, not merely accepted."""
    wide = expose_from_schema([{"name": "n", "type": "DECIMAL(18,4)"}])
    values: Dict[str, List[Decimal]] = {}
    for label, block in (
        ("default", {"range": {"min": "0", "max": "100"}}),
        ("narrow", {"range": {"min": "0", "max": "100"}, "mean": "50", "stddev": "1"}),
    ):
        plan = build_plan(
            wide, sim_id=SIM_ID, seam_digest=H, rows=60, fidelity=fidelity({"n": block})
        )
        values[label] = list(generate_column(plan, "n", root=root_for()))
    spread = {label: max(items) - min(items) for label, items in values.items()}
    assert spread["narrow"] < spread["default"]


def test_a_histogram_puts_mass_where_the_weights_say() -> None:
    expose = expose_from_schema([{"name": "n", "type": "DECIMAL(18,2)"}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=300,
        fidelity=fidelity({"n": {"bins": ["0", "10", "1000"], "binWeights": ["9", "1"]}}),
    )
    values = generate_column(plan, "n", root=root_for())
    low = [value for value in values if value < Decimal(10)]
    assert all(Decimal(0) <= value < Decimal(1000) for value in values)
    assert len(low) > len(values) // 2, "the 9:1 weighting should put most mass in the first bin"


def test_a_declared_null_fraction_produces_some_nulls_and_some_values() -> None:
    expose = expose_from_schema([{"name": "n", "type": "DECIMAL(18,2)"}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=200,
        fidelity=fidelity({"n": {"range": {"min": "0", "max": "10"}, "nullFraction": "0.5"}}),
    )
    values = generate_column(plan, "n", root=root_for())
    assert 0 < sum(1 for value in values if value is None) < len(values)


def test_a_null_fraction_of_zero_produces_no_nulls() -> None:
    expose = expose_from_schema([{"name": "n", "type": "DECIMAL(18,2)"}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=50,
        fidelity=fidelity({"n": {"range": {"min": "0", "max": "10"}, "nullFraction": "0"}}),
    )
    assert all(value is not None for value in generate_column(plan, "n", root=root_for()))


def test_a_required_column_may_not_declare_a_null_fraction() -> None:
    expose = expose_from_schema([{"name": "n", "type": "DECIMAL(18,2)", "required": True}])
    with pytest.raises(FluxError) as error:
        build_plan(
            expose,
            sim_id=SIM_ID,
            seam_digest=H,
            rows=1,
            fidelity=fidelity({"n": {"range": {"min": "0", "max": "1"}, "nullFraction": "0.5"}}),
        )
    assert error.value.code.code == "FLX-3402"


def test_a_declared_support_larger_than_its_max_cardinality_is_refused() -> None:
    expose = expose_from_schema([{"name": "m", "type": "STRING"}])
    with pytest.raises(FluxError) as error:
        build_plan(
            expose,
            sim_id=SIM_ID,
            seam_digest=H,
            rows=1,
            fidelity=fidelity({"m": {"support": {"values": ["a", "b", "c"]}, "maxCardinality": 2}}),
        )
    assert error.value.code.code == "FLX-3402"


def test_support_declared_without_a_readable_support_is_refused_by_name() -> None:
    """§2's ``support: declared`` over a type that names no levels is refused.

    The word resolves against the column's own FLUID type -- FLUID 0.7.5
    carries exactly one structured value set, ``enum(...)`` -- and a
    ``STRING`` names none. Resolving it to an empty support would make
    every value a non-member and fail the check for the wrong reason, so
    the plan refuses before a row exists.
    """
    expose = expose_from_schema([{"name": "m", "type": "STRING"}])
    with pytest.raises(FluxError) as error:
        build_plan(
            expose,
            sim_id=SIM_ID,
            seam_digest=H,
            rows=1,
            fidelity=fidelity({"m": {"support": "declared"}}),
        )
    assert error.value.code.code == "FLX-3402"
    assert "m: support" in str(error.value) and "STRING" in str(error.value)


def test_support_declared_over_an_enum_type_draws_the_types_own_levels() -> None:
    """The paired positive case: the refusal is about the type, not the word."""
    expose = expose_from_schema([{"name": "m", "type": "enum('a','b')"}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=8,
        fidelity=fidelity({"m": {"support": "declared"}}),
    )
    drawn = set(generate_column(plan, "m", root=root_for()))
    assert drawn and drawn <= {"a", "b"}


@pytest.mark.parametrize(
    ("declared", "block", "why"),
    [
        ("DECIMAL(18,4)", {"family": "bounded-normal"}, "a bounded normal needs a declared range"),
        ("DECIMAL(18,4)", {"family": "rate"}, "a rate family needs a declared rate"),
        ("DECIMAL(18,4)", {"family": "rate", "rate": "0"}, "rate must be above zero"),
        ("INT", {"family": "rate"}, "a count rate needs a declared mean"),
        ("INT", {"family": "rate", "mean": "-1"}, "mean is negative"),
        ("DECIMAL(18,4)", {"family": "uniform"}, "a uniform needs a declared range or bins"),
        (
            "DECIMAL(18,4)",
            {"family": "uniform", "bins": ["0", "1"]},
            "declared bins need declared binWeights",
        ),
        (
            "DECIMAL(18,4)",
            {"range": {"min": "0", "max": "1"}, "stddev": "-1"},
            "stddev is negative",
        ),
        (
            "TIMESTAMP",
            {"family": "rate", "rate": "1"},
            "an inter-arrival rate needs a declared range.min",
        ),
    ],
)
def test_a_family_without_its_parameters_is_refused(
    declared: str, block: Dict[str, Any], why: str
) -> None:
    expose = expose_from_schema([{"name": "n", "type": declared}])
    with pytest.raises(FluxError) as error:
        build_plan(
            expose,
            sim_id=SIM_ID,
            seam_digest=H,
            rows=1,
            fidelity=fidelity({"n": block}),
        )
    assert error.value.code.code == "FLX-3402"
    assert why in str(error.value)


def test_a_bool_level_that_is_not_true_or_false_is_refused() -> None:
    expose = expose_from_schema([{"name": "b", "type": "BOOL"}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=1,
        fidelity=fidelity({"b": {"levels": {"yes": "0.5", "no": "0.5"}}}),
    )
    with pytest.raises(FluxError) as error:
        generate_rows(plan, root=root_for())
    assert error.value.code.code == "FLX-3402"


def test_binweights_that_sum_to_zero_are_refused() -> None:
    expose = expose_from_schema([{"name": "n", "type": "DECIMAL(18,2)"}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=1,
        fidelity=fidelity({"n": {"bins": ["0", "1", "2"], "binWeights": ["0", "0"]}}),
    )
    with pytest.raises(FluxError) as error:
        generate_rows(plan, root=root_for())
    assert error.value.code.code == "FLX-3402"


# ---------------------------------------------------------------------------
# The rows render
# ---------------------------------------------------------------------------


def test_every_generated_row_renders_through_the_seam(expose: CompiledExpose) -> None:
    """The generator's value classes are exactly the ones §9 renders."""
    plan = _plan(expose, rows=40)
    rendered = render_rows(generate_rows(plan, root=root_for()), expose)
    assert len(rendered) == plan.rows
    for row in rendered:
        assert len(row) == len(expose.columns)
        assert not any(isinstance(value, float) for value in row)


def test_a_rate_column_of_dates_advances_from_the_declared_start() -> None:
    expose = expose_from_schema([{"name": "t", "type": "TIMESTAMP"}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=20,
        fidelity=fidelity(
            {
                "t": {
                    "family": "rate",
                    "rate": "0.00001",
                    "range": {"min": "2026-07-01T00:00:00Z", "max": "2026-09-30T23:59:59Z"},
                }
            }
        ),
    )
    low = plan.column_plan("t").statistics.range_min
    high = plan.column_plan("t").statistics.range_max
    assert low is not None and high is not None
    for value in generate_column(plan, "t", root=root_for()):
        assert int(low) <= value <= int(high)


def test_an_int_rate_column_is_a_non_negative_count() -> None:
    expose = expose_from_schema([{"name": "n", "type": "INT"}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=60,
        fidelity=fidelity({"n": {"mean": "2.5"}}),
    )
    values = generate_column(plan, "n", root=root_for())
    assert all(isinstance(value, int) and value >= 0 for value in values)
    assert len(set(values)) > 1, "a Poisson draw should not be constant"


def test_an_int_categorical_reads_its_levels_as_numbers() -> None:
    expose = expose_from_schema([{"name": "n", "type": "INT"}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=30,
        fidelity=fidelity({"n": {"levels": {"1": "0.5", "7": "0.5"}}}),
    )
    values = set(generate_column(plan, "n", root=root_for()))
    assert values <= {1, 7}
    assert all(isinstance(value, int) for value in values)


def test_a_date_grid_value_is_a_calendar_date() -> None:
    expose = expose_from_schema([{"name": "d", "type": "DATE"}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=10,
        fidelity=fidelity({"d": {"range": {"min": "2026-07-01", "max": "2026-07-31"}}}),
    )
    for value in generate_column(plan, "d", root=root_for()):
        assert isinstance(value, date)
        assert value.toordinal() - DATE_EPOCH_ORDINAL >= 0


def test_a_degenerate_range_produces_the_single_value_it_declares() -> None:
    expose = expose_from_schema([{"name": "n", "type": "DECIMAL(18,2)"}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=5,
        fidelity=fidelity({"n": {"range": {"min": "5", "max": "5"}, "family": "uniform"}}),
    )
    assert set(generate_column(plan, "n", root=root_for())) == {Decimal("5.00")}


def test_generating_needs_exactly_one_of_root_and_source(expose: CompiledExpose) -> None:
    plan = _plan(expose, rows=1)
    with pytest.raises(FluxError):
        generate_rows(plan)
    with pytest.raises(FluxError):
        generate_rows(plan, root=root_for(), source=GeneratorStreams(root=root_for()))


def _column_values(rows: Tuple[Tuple[Any, ...], ...], index: int) -> List[Any]:
    return [row[index] for row in rows]


# ---------------------------------------------------------------------------
# The value is a function of the declaration, not of interpreter state
#
# KERNEL-SPEC §4 evaluates the samplers "in the ``decimal`` module at 28
# digits". The generator's own arithmetic -- a bounded normal's mean and
# spread derived from a declared range, the equal weights of a declared
# support, every quantisation onto a column's scale -- has to run in that
# same pinned context, or a caller who set ``decimal.getcontext()`` for
# reasons of their own moves every drawn value and the run stops being
# replayable.
#
# Each test below is paired with its negative control: the same assertion
# run against arithmetic that does NOT pin the context, shown to move. A
# "stable under every context" result means nothing unless an unpinned
# computation is shown to be unstable under the same contexts.
# ---------------------------------------------------------------------------

#: Contexts a caller might plausibly have installed. ``prec=5`` is the one
#: that used to change the rows; the rounding modes are the ones that would
#: re-weight a categorical whose weights come out of a ratio.
_AMBIENT_CONTEXTS: Tuple[Tuple[str, int, str], ...] = (
    ("narrow", 5, ROUND_HALF_EVEN),
    ("just-enough", 9, ROUND_HALF_EVEN),
    ("wide", 60, ROUND_HALF_EVEN),
    ("round-up", 28, ROUND_UP),
    ("round-floor", 28, ROUND_FLOOR),
    ("round-ceiling", 28, ROUND_CEILING),
)


@contextmanager
def _ambient(precision: int, rounding: str) -> Any:
    """Install a caller-ish ambient decimal context for the duration."""
    saved = getcontext()
    try:
        setcontext(Context(prec=precision, rounding=rounding))
        yield
    finally:
        setcontext(saved)


def _context_sensitive_reference(precision: int, rounding: str) -> Decimal:
    """The negative control: arithmetic of the generator's own shape, unpinned.

    Derives a bounded normal's spread from a declared range exactly as
    ``_draw_value`` does, but in whatever context is ambient. If this does
    not move across :data:`_AMBIENT_CONTEXTS`, the contexts are too alike
    for the stability assertions above them to mean anything.
    """
    with _ambient(precision, rounding):
        return (Decimal(10000) - Decimal(0)) / Decimal(6)


def test_the_ambient_contexts_are_far_enough_apart_to_move_unpinned_arithmetic() -> None:
    """Negative control for the two tests below."""
    seen = {_context_sensitive_reference(prec, mode) for _, prec, mode in _AMBIENT_CONTEXTS}
    assert len(seen) > 1


@pytest.mark.parametrize("label,precision,rounding", _AMBIENT_CONTEXTS)
def test_the_rows_do_not_depend_on_the_callers_decimal_context(
    expose: CompiledExpose, label: str, precision: int, rounding: str
) -> None:
    """KERNEL-SPEC §4: the draws are evaluated in the kernel's own context."""
    plan = _plan(expose, rows=24)
    expected = generate_rows(plan, root=root_for())
    with _ambient(precision, rounding):
        assert generate_rows(plan, root=root_for()) == expected


@pytest.mark.parametrize("label,precision,rounding", _AMBIENT_CONTEXTS)
def test_an_equal_weight_support_does_not_depend_on_the_callers_context(
    label: str, precision: int, rounding: str
) -> None:
    """A support of three levels divides one by three, which never terminates."""
    expose = expose_from_schema([{"name": "market", "type": "STRING"}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=40,
        fidelity=fidelity({"market": {"support": {"values": ["IE", "GB", "FR"]}}}),
    )
    expected = generate_column(plan, "market", root=root_for())
    with _ambient(precision, rounding):
        assert generate_column(plan, "market", root=root_for()) == expected


@pytest.mark.parametrize("label,precision,rounding", _AMBIENT_CONTEXTS)
def test_a_histogram_does_not_depend_on_the_callers_context(
    label: str, precision: int, rounding: str
) -> None:
    """Bin weights of 1/3 each are rounded once, in the kernel's own context."""
    expose = expose_from_schema([{"name": "amount", "type": "DECIMAL(18,4)"}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=40,
        fidelity=fidelity(
            {
                "amount": {
                    "range": {"min": "0", "max": "300"},
                    "bins": ["0", "100", "200", "300"],
                    "binWeights": ["1", "1", "1"],
                }
            }
        ),
    )
    expected = generate_column(plan, "amount", root=root_for())
    with _ambient(precision, rounding):
        assert generate_column(plan, "amount", root=root_for()) == expected


# ---------------------------------------------------------------------------
# A declared range wider than the context's precision is still a bound
# ---------------------------------------------------------------------------

#: 32 significant digits -- wider than KERNEL-SPEC §4's 28. Computing the
#: grid-point count as ``(high - low) / step`` in that context rounds the
#: span *outwards*, so the draw could land above the declared ``range.max``.
_WIDE_MAX = Decimal("12345678901234567890123456789012")

#: The widest integer range a single bounded draw addresses -- see
#: ``rows._MAX_ADDRESSABLE_GRID_POINTS``. Still 20 significant digits, so
#: still wider than the context's 28-digit... no: wider than what a naive
#: ``(high - low) / step`` keeps exact once a fractional scale is involved,
#: and comfortably past the point where the old spelling drifted.
_ADDRESSABLE_MAX = Decimal((1 << 64) - 1)


def test_the_grid_point_count_of_a_wide_range_is_exact() -> None:
    """The count is integer arithmetic, not a 28-digit decimal division."""
    exponent = _common_exponent(Decimal(0), _WIDE_MAX, Decimal(1))
    points = _grid_units(_WIDE_MAX, exponent) - _grid_units(Decimal(0), exponent) + 1
    assert points == int(_WIDE_MAX) + 1
    # The negative control: the context-bounded spelling this replaced is
    # wrong for this range, and wrong in the direction that admits values
    # above the declared maximum.
    rounded = int((_WIDE_MAX - Decimal(0)) / Decimal(1)) + 1
    assert rounded > points


#: A range far from zero and wider than the context's 28 digits, but whose
#: *point count* a single bounded draw still addresses. The pair that
#: separates the two failure modes: the old ``(high - low) / step``
#: spelling rounds both bounds here (``10**30 + 10**12`` is ``10**30`` at 28
#: digits, so the span collapsed to zero), while the count is only 10**12.
_FAR_LOW = Decimal(10) ** 30
_FAR_HIGH = _FAR_LOW + Decimal(10) ** 12


def _significant_digits(value: Any) -> str:
    """The digits of *value*, with the point and any sign removed."""
    return f"{Decimal(value):f}".replace(".", "").replace("-", "")


@pytest.mark.parametrize("declared_type", ["BIGINT", "DECIMAL(38,0)"])
def test_a_range_wider_than_the_precision_keeps_both_of_its_bounds(
    declared_type: str,
) -> None:
    """§2's ``range`` is a bound at every magnitude the declared type admits."""
    expose = expose_from_schema([{"name": "n", "type": declared_type}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=64,
        fidelity=fidelity(
            {"n": {"range": {"min": str(_FAR_LOW), "max": str(_FAR_HIGH)}, "family": "uniform"}}
        ),
    )
    values = [Decimal(value) for value in generate_column(plan, "n", root=root_for())]
    for value in values:
        assert _FAR_LOW <= value <= _FAR_HIGH
    # Negative controls. Both bounds are load-bearing: the values spread
    # across the span rather than collapsing onto the lower bound (which is
    # what a span rounded to zero would give), and they keep all 31 of their
    # significant digits rather than being rounded to the context's 28.
    assert len(set(values)) > 1
    assert all(len(_significant_digits(value)) == 31 for value in values)
    assert any(value % 1000 != 0 for value in values)


def test_a_grid_no_single_bounded_draw_can_address_is_refused_at_plan_time() -> None:
    """The generator refuses rather than drawing from a narrowed distribution.

    ``bounded_uint`` is Lemire over one 64-bit word: for a bound above
    ``2**64`` every word in its budget is rejected and its documented
    fallback returns the last word, which is uniform over the first
    ``2**64`` grid points and not over the declared range. That is a
    distribution nobody declared, so it is refused by name.
    """
    expose = expose_from_schema([{"name": "n", "type": "DECIMAL(38,0)"}])
    with pytest.raises(FluxError) as error:
        build_plan(
            expose,
            sim_id=SIM_ID,
            seam_digest=H,
            rows=1,
            fidelity=fidelity(
                {"n": {"range": {"min": "0", "max": str(_WIDE_MAX)}, "family": "uniform"}}
            ),
        )
    assert error.value.code.code == "FLX-3402"
    assert "grid points" in (error.value.detail or "")


def test_the_widest_addressable_grid_is_accepted_and_the_next_one_is_not() -> None:
    """The refusal sits exactly at ``bounded_uint``'s own domain boundary."""
    expose = expose_from_schema([{"name": "n", "type": "DECIMAL(38,0)"}])

    def plan_for(maximum: int) -> None:
        build_plan(
            expose,
            sim_id=SIM_ID,
            seam_digest=H,
            rows=1,
            fidelity=fidelity(
                {"n": {"range": {"min": "0", "max": str(maximum)}, "family": "uniform"}}
            ),
        )

    plan_for((1 << 64) - 1)  # exactly 2**64 points: the last drawable grid
    with pytest.raises(FluxError) as error:
        plan_for(1 << 64)  # one point more
    assert error.value.code.code == "FLX-3402"


def test_the_refusal_is_on_the_point_count_not_the_magnitude() -> None:
    """A wide range at a coarse scale is fine; a narrow one at a fine scale is not."""
    coarse = expose_from_schema([{"name": "n", "type": "DECIMAL(38,0)"}])
    build_plan(
        coarse,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=1,
        fidelity=fidelity(
            {"n": {"range": {"min": "0", "max": str((1 << 64) - 1)}, "family": "uniform"}}
        ),
    )
    fine = expose_from_schema([{"name": "n", "type": "DECIMAL(38,30)"}])
    with pytest.raises(FluxError) as error:
        build_plan(
            fine,
            sim_id=SIM_ID,
            seam_digest=H,
            rows=1,
            fidelity=fidelity({"n": {"range": {"min": "0", "max": "1000"}, "family": "uniform"}}),
        )
    assert error.value.code.code == "FLX-3402"


def test_an_unaddressable_histogram_bin_is_refused_at_plan_time() -> None:
    """Each bin is drawn from in turn, so each bin is checked in turn.

    The first bin here is comfortably addressable and the second is not, so
    the refusal naming the second is evidence the check runs per bin rather
    than once over the outer range.
    """
    expose = expose_from_schema([{"name": "n", "type": "DECIMAL(38,20)"}])
    with pytest.raises(FluxError) as error:
        build_plan(
            expose,
            sim_id=SIM_ID,
            seam_digest=H,
            rows=1,
            fidelity=fidelity(
                {
                    "n": {
                        "range": {"min": "0", "max": "100000"},
                        "bins": ["0", "0.00000000000000000100", "100000"],
                        "binWeights": ["1", "1"],
                    }
                }
            ),
        )
    assert error.value.code.code == "FLX-3402"
    assert "bin 1" in (error.value.detail or "")


def test_a_wide_value_keeps_every_declared_digit_through_the_seam() -> None:
    """A value wider than the context's precision is not silently rounded."""
    expose = expose_from_schema([{"name": "amount", "type": "DECIMAL(38,4)"}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=16,
        fidelity=fidelity(
            {
                "amount": {
                    "range": {
                        "min": str(_FAR_LOW),
                        "max": str(_FAR_LOW + Decimal(10) ** 12),
                    },
                    "family": "uniform",
                }
            }
        ),
    )
    rows = generate_rows(plan, root=root_for())
    rendered = render_rows(rows, expose)
    assert any(len(text.replace(".", "").rstrip("0")) > 28 for (text,) in rendered)
    for (value,), (text,) in zip(rows, rendered, strict=True):
        assert Decimal(text) == value
        assert -value.as_tuple().exponent == 4


# ---------------------------------------------------------------------------
# The remaining branches of §1's table, each with its own declaration
# ---------------------------------------------------------------------------


def test_a_date_column_falls_back_to_the_runs_time_bounds() -> None:
    """§1's DATE/TIMESTAMP default is "uniform within ``timeBounds``"."""
    expose = expose_from_schema([{"name": "d", "type": "DATE"}])
    plan = build_plan(expose, sim_id=SIM_ID, seam_digest=H, rows=12, time_bounds=TIME_BOUNDS)
    statistics = plan.column_plan("d").statistics
    assert statistics.has_range
    for value in generate_column(plan, "d", root=root_for()):
        assert isinstance(value, date)
        assert date(2026, 7, 1) <= value <= date(2026, 9, 30)


def test_a_bin_narrower_than_one_grid_point_holds_its_lower_edge() -> None:
    """Two adjacent edges one step apart make a bin of exactly one value."""
    expose = expose_from_schema([{"name": "n", "type": "DECIMAL(18,2)"}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=40,
        fidelity=fidelity(
            {
                "n": {
                    "range": {"min": "0", "max": "10"},
                    # Edges finer than the column's own scale: the first bin
                    # spans less than one grid point, so its top edge lands
                    # below its bottom one and the bin holds one value.
                    "bins": ["0", "0.001", "10"],
                    "binWeights": ["1", "0"],
                }
            }
        ),
    )
    assert set(generate_column(plan, "n", root=root_for())) == {Decimal("0.00")}


def test_a_decimal_rate_column_is_an_exponential_wait_at_the_declared_scale() -> None:
    """§1's rate family on a DECIMAL is the inter-arrival wait itself."""
    expose = expose_from_schema([{"name": "wait", "type": "DECIMAL(18,3)"}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=30,
        fidelity=fidelity({"wait": {"rate": "0.5", "range": {"min": "0", "max": "20"}}}),
    )
    assert plan.column_plan("wait").family is Family.RATE
    values = generate_column(plan, "wait", root=root_for())
    for value in values:
        assert Decimal(0) <= value <= Decimal(20)
        assert -value.as_tuple().exponent == 3
    assert len(set(values)) > 1


def test_a_rate_draw_is_clamped_onto_the_declared_bounds() -> None:
    """A wait past the declared maximum is clamped, not emitted out of range."""
    expose = expose_from_schema([{"name": "wait", "type": "DECIMAL(18,2)"}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=60,
        # A slow rate against a tight ceiling, so the exponential tail lands
        # above range.max often enough to exercise the upper clamp.
        fidelity=fidelity({"wait": {"rate": "0.05", "range": {"min": "0", "max": "1"}}}),
    )
    values = generate_column(plan, "wait", root=root_for())
    assert all(Decimal(0) <= value <= Decimal(1) for value in values)
    assert Decimal(1) in set(values)


def test_a_decimal_categorical_reads_its_levels_as_numbers() -> None:
    """A DECIMAL column may declare levels; they are read at its own scale."""
    expose = expose_from_schema([{"name": "tier", "type": "DECIMAL(18,2)"}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=40,
        fidelity=fidelity({"tier": {"levels": {"1.5": "0.5", "9.25": "0.5"}}}),
    )
    assert plan.column_plan("tier").family is Family.CATEGORICAL
    values = set(generate_column(plan, "tier", root=root_for()))
    assert values == {Decimal("1.50"), Decimal("9.25")}


def test_a_count_below_a_declared_minimum_is_clamped_up_to_it() -> None:
    """``_clamp`` is a two-sided bound, and the lower side is reachable.

    An INT column whose family is a rate draws a Poisson count, which has
    no lower bound of its own; a declared ``range.min`` is what puts one
    there. The pair with the upper clamp above is what makes "clamped onto
    the declared bounds" a statement about both ends.
    """
    expose = expose_from_schema([{"name": "n", "type": "INT"}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=60,
        fidelity=fidelity(
            {"n": {"family": "rate", "mean": "1", "range": {"min": "5", "max": "9"}}}
        ),
    )
    assert plan.column_plan("n").family is Family.RATE
    values = generate_column(plan, "n", root=root_for())
    assert all(5 <= value <= 9 for value in values)
    assert 5 in set(values)


# ---------------------------------------------------------------------------
# Declared level weights are a declared distribution
# ---------------------------------------------------------------------------


def _levels_plan(levels: Dict[str, str], rows: int = 200) -> Any:
    expose = expose_from_schema([{"name": "m", "type": "enum('ie','uk')"}])
    return build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=rows,
        fidelity=fidelity({"m": {"support": {"values": ["ie", "uk"]}, "levels": levels}}),
    )


def test_declared_levels_that_do_not_sum_to_one_are_refused_at_plan_time() -> None:
    """FLX-5020, naming the column, before any row exists.

    Folding the residue away instead would draw ``uk`` at 0.5 where the
    document says 0.2, and would run *before* ``categorical``'s own sum
    check so that check could never fire on this path. The drawn marginal
    would then be hashed into ``outputDigest`` indistinguishably from a
    declared one.
    """
    with pytest.raises(FluxError) as caught:
        _levels_plan({"ie": "0.5", "uk": "0.2"})
    assert caught.value.code.code == "FLX-5020"
    assert "m" in (caught.value.detail or "")


def test_declared_levels_summing_above_one_are_refused_too() -> None:
    with pytest.raises(FluxError) as caught:
        _levels_plan({"ie": "0.8", "uk": "0.4"})
    assert caught.value.code.code == "FLX-5020"


def test_declared_levels_that_sum_to_one_are_drawn_at_the_declared_weights() -> None:
    """The negative control: a declared distribution still draws."""
    plan = _levels_plan({"ie": "0.5", "uk": "0.5"}, rows=400)
    drawn = generate_column(plan, "m", root=root_for())
    assert set(drawn) == {"ie", "uk"}


def test_the_residue_a_decimal_rendering_leaves_is_still_folded_away() -> None:
    """A third rendered at the module's fixed number of places still draws.

    That sub-tolerance residue is the whole reason ``_renormalised``
    exists; refusing it would refuse an equal-weight support.
    """
    from flux_engine.generator.rows import _fraction_as_decimal, _renormalised

    third = _fraction_as_decimal(Fraction(1, 3))
    weights = {"a": third, "b": third, "c": third}
    assert sum(weights.values()) != Decimal(1)
    assert sum(_renormalised(weights).values()) == Decimal(1)


def test_the_residue_lands_on_the_level_categorical_treats_as_last() -> None:
    """``_renormalised`` and ``categorical`` must agree on which level is last.

    ``categorical`` orders levels by NFC key. A raw code-point sort puts a
    decomposed level in a different place, so the level absorbing the
    decimal residue would not be the one absorbing ``categorical``'s own
    threshold remainder -- both corrections would land on the same side and
    one level would be weighted twice over.
    """
    from flux_engine.core.rng import nfc_sorted_items
    from flux_engine.generator.rows import _fraction_as_decimal, _renormalised

    decomposed = unicodedata.normalize("NFD", "é")
    third = _fraction_as_decimal(Fraction(1, 3))
    weights = {decomposed: third, "z": third, "a": third}
    assert sorted(weights)[-1] != nfc_sorted_items(weights)[-1][0], "the two orders differ"

    adjusted = _renormalised(weights)
    moved = [key for key, value in adjusted.items() if value != weights[key]]
    assert moved == [nfc_sorted_items(weights)[-1][0]]


# ---------------------------------------------------------------------------
# The last histogram bin is closed at the top
# ---------------------------------------------------------------------------


def test_the_top_of_the_last_histogram_bin_is_drawable() -> None:
    """The generator reaches the value ``bin_index`` puts in the last bin.

    ``bin_index`` closes the last bin at the top "so a record exactly at
    the declared maximum is binned rather than dropped". Taking a grid step
    off every bin's upper bound would leave that value unreachable while
    the check went on counting it inside the last bin -- a top bin short of
    one grid point of mass that the histogram property itself cannot see.
    """
    from flux_engine.concordance.boundary.statistic import bin_index

    expose = expose_from_schema([{"name": "n", "type": "INT"}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=2000,
        fidelity=fidelity(
            {
                "n": {
                    "range": {"min": "0", "max": "4"},
                    "bins": ["0", "2", "4"],
                    "binWeights": ["1", "1"],
                }
            }
        ),
    )
    drawn = set(generate_column(plan, "n", root=root_for()))
    assert bin_index(4, [0, 2, 4]) == 1, "the checker puts the top edge in the last bin"
    assert drawn == {0, 1, 2, 3, 4}


def test_a_bin_below_the_last_stays_half_open() -> None:
    """The negative control: only the *last* bin is closed at the top."""
    expose = expose_from_schema([{"name": "n", "type": "INT"}])
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=2000,
        fidelity=fidelity(
            {
                "n": {
                    "range": {"min": "0", "max": "4"},
                    "bins": ["0", "2", "4"],
                    "binWeights": ["1", "0"],
                }
            }
        ),
    )
    assert set(generate_column(plan, "n", root=root_for())) == {0, 1}
