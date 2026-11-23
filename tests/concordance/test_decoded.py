# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""An emit decoded once into integer columns forms the statistics the rows form.

:class:`flux_engine.concordance.decoded.DecodedRows` holds an emit's columns as exact integers
(units at a scale, days, milliseconds, level codes), and the statistic and the shape counts are
formed from them with numpy. The row-by-row reader of :mod:`flux_stats.reading` is the oracle:
over generated columns, with nulls, edge values and values outside their declared range, the
two give the same group statistics and the same shape summary, field for field; and a cell the
fast reading does not take is read by the rows' reader, which refuses it as it always did.
"""

from __future__ import annotations

import copy
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Sequence, Tuple

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flux_engine.concordance.boundary.statistic import compute_statistic
from flux_engine.concordance.decoded import DecodedRows, safe_sum
from flux_engine.concordance.shape import summarise_emit
from flux_engine.core.errors import FluxError
from flux_stats.profile import SCHEMA_URL, parse_profile
from tests.concordance.conftest import build_compiled

from .test_level_histogram import _document as _levels_document
from .test_shape import TABLE, _column, _distinct, _histogram, _levels

pytestmark = pytest.mark.unit

_NAMES = ["channel", "plan", "amount", "qty", "day", "at"]
_TOLERANCE = {"type": "distance", "measure": "totalVariation", "max": "0.25"}
_ABSOLUTE = {"type": "absolute", "value": "9"}


def _block() -> Dict[str, Any]:
    document = _levels_document()
    document["contract"]["exposes"][0]["contract"]["schema"] = [
        {"name": "channel", "type": "STRING", "required": True},
        {"name": "plan", "type": "STRING"},
        {"name": "amount", "type": "DECIMAL(18,4)"},
        {"name": "qty", "type": "INT"},
        {"name": "day", "type": "DATE"},
        {"name": "at", "type": "TIMESTAMP"},
    ]
    document["expose"]["columns"] = list(_NAMES)
    fidelity = document["extension"]["fidelity"]
    fidelity["encoding"] = {"fractionalBits": 16, "rounding": "floor"}
    fidelity["records"] = {"min": 1, "max": 1000000}
    columns = fidelity["columns"]
    columns["amount"] = {"range": {"min": "-5", "max": "100"}, "null": "exclude"}
    columns["qty"] = {"range": {"min": "0", "max": "1000"}, "null": "exclude"}
    columns["day"] = {"range": {"min": "2026-01-01", "max": "2026-12-31"}, "null": "exclude"}
    columns["at"] = {
        "range": {"min": "2026-01-01T00:00:00.000Z", "max": "2026-12-31T23:59:59.999Z"},
        "null": "exclude",
    }
    columns.pop("tier", None)
    fidelity["properties"] = [
        {"property": "mean", "column": "amount", "tolerance": dict(_ABSOLUTE)},
        {"property": "mean", "column": "qty", "groupBy": "channel", "tolerance": dict(_ABSOLUTE)},
        {"property": "mean", "column": "day", "tolerance": dict(_ABSOLUTE)},
        {"property": "mean", "column": "at", "tolerance": dict(_ABSOLUTE)},
        {
            "property": "marginalHistogram",
            "column": "amount",
            "bins": ["-5", "10", "50", "100"],
            "tolerance": dict(_TOLERANCE),
        },
        {
            "property": "marginalHistogram",
            "column": "qty",
            "groupBy": "plan",
            "bins": ["0", "500", "1000"],
            "tolerance": dict(_TOLERANCE),
        },
        {"property": "quantile", "column": "amount", "level": "0.5", "tolerance": dict(_ABSOLUTE)},
        {
            "property": "quantile",
            "column": "amount",
            "level": "0.9",
            "bins": ["-5", "10", "50", "100"],
            "tolerance": dict(_ABSOLUTE),
        },
        {"property": "quantile", "column": "qty", "level": "0.25", "tolerance": dict(_ABSOLUTE)},
        {
            "property": "levelHistogram",
            "column": "plan",
            "groupBy": "channel",
            "tolerance": dict(_TOLERANCE),
        },
        {"property": "levelHistogram", "column": "channel", "tolerance": dict(_TOLERANCE)},
        {"property": "support", "column": "plan", "tolerance": {"type": "count", "max": 0}},
    ]
    return document


_COMPILED = build_compiled(_block())

_CHANNELS = st.sampled_from(["web", "store"])
_PLANS = st.one_of(st.none(), st.sampled_from(["pro", "basic", "plus"]))
_AMOUNTS = st.one_of(
    st.none(),
    st.sampled_from(["-5.0000", "0.0000", "99.9999", "100.0000", "10.0000", "50.0000", "0.0001"]),
    st.integers(-50000, 1000000).map(
        lambda u: str(Decimal(u).scaleb(-4).quantize(Decimal("0.0001")))
    ),
)
_QTY = st.one_of(st.none(), st.integers(0, 1000))
_DAYS = st.one_of(st.none(), st.dates(date(2026, 1, 1), date(2026, 12, 31)).map(str))


def _instant(ms: int) -> str:
    moment = datetime.fromtimestamp(1767225600 + ms // 1000, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


_INSTANTS = st.one_of(st.none(), st.integers(0, 364 * 86400 * 1000 + 86399999).map(_instant))
_ROWS = st.lists(
    st.tuples(_CHANNELS, _PLANS, _AMOUNTS, _QTY, _DAYS, _INSTANTS), min_size=2, max_size=60
)


def _both(rows: Sequence[Tuple[Any, ...]]) -> Tuple[Any, Any]:
    mappings = [dict(zip(_NAMES, row, strict=True)) for row in rows]
    return mappings, DecodedRows(_NAMES, [list(row) for row in rows])


def _outcome(make: Any) -> str:
    try:
        return repr(make())
    except FluxError as error:
        return f"{error.code.code}: {error}"


@pytest.mark.property
@settings(max_examples=120, deadline=None)
@given(rows=_ROWS)
def test_every_statistic_is_the_one_the_rows_form(rows: List[Tuple[Any, ...]]) -> None:
    mappings, decoded = _both(rows)
    assert _outcome(lambda: compute_statistic(decoded, _COMPILED)) == _outcome(
        lambda: compute_statistic(mappings, _COMPILED)
    )


def test_a_value_the_fast_reading_does_not_take_is_read_by_the_rows() -> None:
    rows = [
        ("web", "pro", "1e1", 1, "2026-02-01", None),  # a number spelled with an exponent
        ("web", "pro", "5.0000", 2, "2026-02-01", None),
    ]
    mappings, decoded = _both(rows)
    assert _outcome(lambda: compute_statistic(decoded, _COMPILED)) == _outcome(
        lambda: compute_statistic(mappings, _COMPILED)
    )
    outside = [("web", "pro", "500.0000", 1, "2026-02-01", None)] * 3
    mappings, decoded = _both(outside)
    refusal = _outcome(lambda: compute_statistic(decoded, _COMPILED))
    assert refusal == _outcome(lambda: compute_statistic(mappings, _COMPILED))
    assert "FLX-5005" in refusal


def test_a_sum_larger_than_an_int64_is_exact() -> None:
    import numpy as np

    values = np.full(1000, (1 << 62) - 1, dtype=np.int64)
    assert safe_sum(values) == 1000 * ((1 << 62) - 1)
    assert safe_sum(-values) == -1000 * ((1 << 62) - 1)
    assert safe_sum(np.zeros(0, dtype=np.int64)) == 0


# ---------------------------------------------------------------------------
# The shape counts
# ---------------------------------------------------------------------------


def _profile() -> Any:
    columns: Dict[str, Any] = {
        "amount": _column(
            statistics=[_histogram([30, 40, 30], edges=[-5, 10, 50, 100])],
            metrics=_distinct(50, count=100),
        ),
        "plan": _column(
            "category", [_levels(["basic", "plus", "pro"], [30, 30, 40])], _distinct(3)
        ),
        "channel": _column("category", [_levels(["store", "web"], [50, 50])]),
        "qty": _column(metrics=_distinct(20, count=100)),
        "at": _column(
            "timestamp",
            [_histogram([1, 1], edges=[1767225600000, 1782864000000, 1798761599999])],
        ),
    }
    document = {
        "_producer": "urn:example:flux-test-profiler",
        "_schemaURL": SCHEMA_URL,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}},
        "tables": {TABLE: {"rowCount": 100, "columns": copy.deepcopy(columns), "columnPairs": []}},
    }
    return parse_profile(document)


@pytest.mark.property
@settings(max_examples=120, deadline=None)
@given(rows=_ROWS)
def test_the_shape_counts_are_the_ones_the_rows_give(rows: List[Tuple[Any, ...]]) -> None:
    profile = _profile()
    mappings, decoded = _both(rows)
    assert _outcome(lambda: summarise_emit(profile, TABLE, decoded)) == _outcome(
        lambda: summarise_emit(profile, TABLE, mappings)
    )


def test_a_skewed_column_has_the_same_point_masses_either_way() -> None:
    profile = _profile()
    rows = [("web", "pro", "20.0000", 5, "2026-03-03", "2026-03-03T00:00:00.000Z")] * 50 + [
        ("store", "plus", "-6.0000", 7, "2026-03-04", "2027-03-03T00:00:00.000Z")
    ] * 5
    mappings, decoded = _both(rows)
    summary = summarise_emit(profile, TABLE, decoded)
    assert summary.columns
    assert repr(summary) == repr(summarise_emit(profile, TABLE, mappings))


def test_the_properties_are_formed_from_the_columns_not_the_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The comparison above would pass if every call fell back to the rows; this one does not."""
    from flux_engine.concordance import decoded as module
    from flux_engine.concordance.boundary import statistic

    taken: List[bool] = []
    real = module.measure_decoded

    def spy(*args: Any) -> Any:
        found = real(*args)
        taken.append(found is not None)
        return found

    monkeypatch.setattr(statistic, "measure_decoded", spy)
    rows = [
        (
            "web" if i % 3 else "store",
            ("pro", "basic", None)[i % 3],
            f"{i % 90}.5000",
            i % 900,
            "2026-03-03",
            "2026-03-03T00:00:00.000Z",
        )
        for i in range(120)
    ]
    _, decoded = _both(rows)
    compute_statistic(decoded, _COMPILED)
    assert taken and all(taken)


# ---------------------------------------------------------------------------
# A boolean is the category "false" or "true"
# ---------------------------------------------------------------------------


def _boolean_block() -> Dict[str, Any]:
    document = _levels_document()
    document["contract"]["exposes"][0]["contract"]["schema"] = [
        {"name": "channel", "type": "STRING", "required": True},
        {"name": "active", "type": "BOOL"},
    ]
    document["expose"]["columns"] = ["channel", "active"]
    fidelity = document["extension"]["fidelity"]
    fidelity["encoding"] = {"fractionalBits": 16, "rounding": "floor"}
    fidelity["records"] = {"min": 1, "max": 1000000}
    fidelity["columns"] = {
        "channel": {"support": {"values": ["web", "store"]}, "maxCardinality": 4, "null": "fail"},
        "active": {
            "support": {"values": ["false", "true"]},
            "maxCardinality": 2,
            "null": "exclude",
        },
    }
    fidelity["properties"] = [
        {"property": "levelHistogram", "column": "active", "tolerance": dict(_TOLERANCE)},
        {
            "property": "levelHistogram",
            "column": "active",
            "groupBy": "channel",
            "tolerance": dict(_TOLERANCE),
        },
        {"property": "support", "column": "active", "tolerance": {"type": "count", "max": 0}},
    ]
    return document


_BOOLEAN_ROWS = st.lists(
    st.tuples(_CHANNELS, st.one_of(st.none(), st.booleans())), min_size=2, max_size=40
)


@pytest.mark.property
@settings(max_examples=60, deadline=None)
@given(rows=_BOOLEAN_ROWS)
def test_a_boolean_column_is_read_as_the_category_by_both_readers(
    rows: List[Tuple[Any, ...]],
) -> None:
    compiled = build_compiled(_boolean_block())
    names = ["channel", "active"]
    mappings = [dict(zip(names, row, strict=True)) for row in rows]
    decoded = DecodedRows(names, [list(row) for row in rows])
    assert _outcome(lambda: compute_statistic(decoded, compiled)) == _outcome(
        lambda: compute_statistic(mappings, compiled)
    )


def test_the_counts_of_a_boolean_column_are_its_false_and_true_records() -> None:
    compiled = build_compiled(_boolean_block())
    rows = [("web", True)] * 3 + [("web", False)] * 2 + [("store", None), ("store", True)]
    names = ["channel", "active"]
    statistic = compute_statistic(DecodedRows(names, [list(row) for row in rows]), compiled)
    counts = statistic.properties["000:levelHistogram:active"].groups[0].level_counts
    assert counts == (2, 4)  # "false", "true": the null is left out


def test_a_shape_check_reads_a_boolean_column() -> None:
    columns = {
        "active": _column("category", [_levels(["false", "true"], [50, 50])], _distinct(2)),
    }
    document = {
        "_producer": "urn:example:flux-test-profiler",
        "_schemaURL": SCHEMA_URL,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}},
        "tables": {TABLE: {"rowCount": 100, "columns": columns, "columnPairs": []}},
    }
    profile = parse_profile(document)
    rows = [("web", True)] * 40 + [("web", False)] * 40
    names = ["channel", "active"]
    mappings = [dict(zip(names, row, strict=True)) for row in rows]
    by_rows = summarise_emit(profile, TABLE, mappings)
    by_columns = summarise_emit(profile, TABLE, DecodedRows(names, [list(row) for row in rows]))
    (column,) = by_rows.columns
    assert column.status == "checked" and column.records == 80 and column.distinct == 2
    assert column.cell_counts == (40, 40)
    assert repr(by_rows) == repr(by_columns)
