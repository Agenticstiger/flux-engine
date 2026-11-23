# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The profile document: read whole or refused, and its schema agrees.

``docs/spec/profile-example.json`` carries every section a profile has, with
suppressed groups, cells and metrics. It must read, and it must validate
against ``docs/spec/profile-1.schema.json``. Each broken copy below must be
refused by the reader with ``FLX-5036`` naming where; the ones a JSON
Schema can express are refused by the schema too, so the published schema
never accepts a document the reader refuses for its shape.
"""

from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Dict

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from flux_stats import metric, metrics, register_metric
from flux_stats.column_metrics import COUNT
from flux_stats.errors import FluxError
from flux_stats.profile import (
    CONF_STATUS,
    SCHEMA_URL,
    SCHEMA_URL_1_1_0,
    load_profile_text,
    parse_profile,
)

pytestmark = pytest.mark.unit


def _repository_root() -> Path:
    """The flux-engine checkout these tests sit in, found by its specification folder.

    Walked up to rather than counted, because mutmut runs these tests from a
    copy of the package under ``packages/flux-stats/mutants``.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "spec" / "PROFILE-SPEC.md").is_file():
            return parent
    raise RuntimeError("these tests read docs/spec from the flux-engine checkout around them")


SPEC = _repository_root() / "docs" / "spec"
EXAMPLE: Dict[str, Any] = json.loads((SPEC / "profile-example.json").read_text(encoding="utf-8"))
SCHEMA: Dict[str, Any] = json.loads((SPEC / "profile-1.schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())

TENURE = ("tables", "subscriber", "columns", "tenure_months")
PAID_AT = ("tables", "payment", "columns", "paid_at")
RULE = ("disclosureRules", "acro-default")


def _at(document: Dict[str, Any], *path: Any) -> Any:
    for step in path:
        document = document[step]
    return document


def test_the_example_reads_every_section() -> None:
    profile = parse_profile(copy.deepcopy(EXAMPLE))
    assert sorted(profile.tables) == ["payment", "subscriber"]
    subscriber = profile.tables["subscriber"]
    assert subscriber.row_count == 1200
    tenure = subscriber.columns["tenure_months"]
    assert tenure.domain == "number"
    assert tenure.metrics["sum"].value == Fraction(34655)
    assert tenure.metrics["min"].value is None and tenure.metrics["min"].status == "A"
    assert tenure.metrics["quantiles"].value == {
        Fraction(1, 4): Fraction(11),
        Fraction(1, 2): Fraction(26),
        Fraction(3, 4): Fraction(44),
    }
    histogram = next(s for s in tenure.statistics if s.identifier == "marginalHistogram/1")
    assert histogram.bins == tuple(Fraction(edge) for edge in ("0", "12", "24", "48", "120"))
    assert histogram.groups[0].suppressed_cells == (3,)
    assert histogram.groups[0].statistic is not None
    assert histogram.groups[0].statistic.bin_counts == (305, 260, 465, 0)
    grouped = next(s for s in tenure.statistics if s.group_by == "market")
    assert [(g.key, g.published) for g in grouped.groups] == [
        ("DE", True),
        ("FR", False),
        ("GB", True),
        ("IE", False),
    ]
    payment = profile.tables["payment"]
    assert payment.pairs[0].columns == ("amount", "attempts")
    period = payment.periods[0]
    assert (period.column, period.period) == ("paid_at", "P1M")
    assert period.statistics[0].of == "amount"
    assert [g.key for g in period.statistics[0].groups] == ["2025-07", "2025-08", "2025-09"]
    (relationship,) = profile.relationships
    assert (relationship.parent_table, relationship.child_table) == ("subscriber", "payment")
    (query,) = profile.queries
    assert query.statistics[0].levels == ("false", "true")
    assert profile.disclosure_rules["acro-default"].dominance == (2, Fraction(9, 10))


def test_the_example_validates_against_the_published_schema() -> None:
    Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["$id"] == SCHEMA_URL_1_1_0
    assert EXAMPLE["_schemaURL"] == SCHEMA_URL, "the example is written in format 1-0-0"
    assert not list(VALIDATOR.iter_errors(EXAMPLE))


def test_the_schema_names_every_status_code() -> None:
    assert SCHEMA["$defs"]["Status"]["enum"] == sorted(CONF_STATUS)


def _set(*path: Any, value: Any) -> Callable[[Dict[str, Any]], None]:
    def change(document: Dict[str, Any]) -> None:
        _at(document, *path[:-1])[path[-1]] = value

    return change


def _drop(*path: Any) -> Callable[[Dict[str, Any]], None]:
    def change(document: Dict[str, Any]) -> None:
        del _at(document, *path[:-1])[path[-1]]

    return change


def _repeat_first(*path: Any) -> Callable[[Dict[str, Any]], None]:
    def change(document: Dict[str, Any]) -> None:
        found = _at(document, *path)
        found.append(copy.deepcopy(found[0]))

    return change


#: (change, where the reader says the problem is, whether the schema refuses it too)
def _min_above_max(document: Dict[str, Any]) -> None:
    metrics = _at(document, *PAID_AT, "metrics")
    metrics["min"]["value"], metrics["max"]["value"] = (
        metrics["max"]["value"],
        metrics["min"]["value"],
    )


BROKEN = [
    (_set("_schemaURL", value="https://example.com/other.json"), "profile._schemaURL", True),
    (_drop("encoding"), "profile", True),
    (_set("encoding", "fractionalBits", value=129), "profile.encoding.fractionalBits", True),
    (_set("encoding", "rounding", value="nearest"), "profile.encoding.rounding", True),
    (_set("surplus", value=1), "profile", True),
    (_set(*TENURE, "domain", value="text"), "tenure_months.domain", True),
    (
        _set(*TENURE, "statistics", 0, "groups", 0, "k", value=-1),
        "tenure_months.statistics[0].groups[0].k",
        True,
    ),
    (
        _set(*TENURE, "statistics", 0, "groups", 0, "Sx", value="1.5"),
        "tenure_months.statistics[0].groups[0].Sx",
        True,
    ),
    (
        _set(*TENURE, "statistics", 0, "definition", value="median/1"),
        "tenure_months.statistics[0].definition",
        True,
    ),
    (
        _set(*TENURE, "statistics", 0, "definition", value="correlation/1"),
        "tenure_months.statistics[0].definition",
        True,
    ),
    (
        _set(*TENURE, "statistics", 1, "bins", value=["0", "12", "12", "120"]),
        "tenure_months.statistics[1].bins",
        False,
    ),
    (
        _set(*TENURE, "statistics", 1, "groups", 0, "cellStatus", value=["F", "F", "F", "F"]),
        "tenure_months.statistics[1].groups[0].binCounts[3]",
        False,
    ),
    (
        _set(*TENURE, "statistics", 1, "groups", 0, "binCounts", value=[1, 2, 3]),
        "tenure_months.statistics[1].groups[0].binCounts",
        False,
    ),
    (
        _set(*TENURE, "statistics", 0, "groups", 0, "status", value="Z"),
        "tenure_months.statistics[0].groups[0].status",
        True,
    ),
    (
        _set(*TENURE, "statistics", 0, "groups", 0, "group", value="all"),
        "tenure_months.statistics[0].groups",
        False,
    ),
    (
        _set(*TENURE, "statistics", 3, "groups", 1, value={"group": "FR", "status": "A", "k": 3}),
        "tenure_months.statistics[3].groups[1]",
        True,
    ),
    (
        _set(*TENURE, "statistics", 3, "groupBy", value="tenure_months"),
        "tenure_months.statistics[3].groupBy",
        False,
    ),
    (
        _set(*TENURE, "statistics", 0, "disclosure", value="unknown"),
        "tenure_months.statistics[0].disclosure",
        False,
    ),
    (
        _set(*TENURE, "statistics", 0, "sampleSize", value={"min": 10, "max": 9}),
        "tenure_months.statistics[0].sampleSize",
        False,
    ),
    (
        _repeat_first(*TENURE, "statistics"),
        "tenure_months.statistics[4]",
        False,
    ),
    (
        _set(
            "tables",
            "subscriber",
            "columns",
            "market",
            "metrics",
            "sum",
            value={
                "definition": "sum/1",
                "status": "F",
                "value": "1",
                "sampleSize": {"min": 1},
                "disclosure": "acro-default",
            },
        ),
        "market.metrics.sum",
        False,
    ),
    (
        _set(*TENURE, "metrics", "count", "definition", value="count/2"),
        "tenure_months.metrics.count.definition",
        True,
    ),
    (
        _set(
            "tables",
            "subscriber",
            "columns",
            "market",
            "statistics",
            0,
            "levels",
            value=["GB", "DE", "FR", "IE"],
        ),
        "market.statistics[0].levels",
        False,
    ),
    (
        _set(
            "tables", "payment", "periods", 0, "statistics", 0, "groups", 0, "group", value="2025-7"
        ),
        "periods[0].statistics[0].groups",
        False,
    ),
    (
        _set("tables", "payment", "periods", 0, "period", value="P1W"),
        "periods[0].period",
        True,
    ),
    (
        _set("relationships", 0, "child", "table", value="invoice"),
        "relationships[0].child.table",
        False,
    ),
    (
        _set("queries", 0, "statistics", 0, "levels", value=["no", "yes"]),
        "queries[0].statistics",
        True,
    ),
    (_set("tables", "subscriber", "rowCount", value=True), "subscriber.rowCount", True),
    (_set(*TENURE, "domain", value=["number"]), "tenure_months.domain", True),
    (_set(*TENURE, "domain", value={}), "tenure_months.domain", True),
    (_drop(*TENURE, "statistics", 2, "level"), "tenure_months.statistics[2]", True),
    (
        _set(*TENURE, "statistics", 2, "level", value="1.5"),
        "tenure_months.statistics[2].level",
        True,
    ),
    (
        _set(*TENURE, "statistics", 2, "level", value="-0"),
        "tenure_months.statistics[2].level",
        True,
    ),
    (_set(*RULE, "pRatio", value="-0.0"), "acro-default.pRatio", True),
    (
        _set(*TENURE, "metrics", "quantiles", "values", value={"-0": "10"}),
        "tenure_months.metrics.quantiles.values",
        True,
    ),
    (_set(*RULE, "pRatio", value="7"), "acro-default.pRatio", True),
    (_set(*RULE, "dominance", "k", value="2"), "acro-default.dominance.k", True),
    (_set(*RULE, "dominance", "n", value=2**53), "acro-default.dominance.n", True),
    (_set(*TENURE, "metrics", "sum", "value", value="1" * 1001), "tenure_months.metrics.sum", True),
    (_set(*PAID_AT, "metrics", "min", "value", value="1.5"), "paid_at.metrics.min.value", True),
    (_min_above_max, "paid_at.metrics", False),
    (_set("_producer", value="urn:example:cafe\u0301"), "profile._producer", False),
    (
        _set(
            "tables",
            "subscriber",
            "columns",
            "market",
            "statistics",
            0,
            "levels",
            0,
            value="E\u0301",
        ),
        "market.statistics[0].levels[0]",
        False,
    ),
    (
        _set(*TENURE, "metrics", "quantiles", "values", value={"0.5": "10", "0.50": "12"}),
        "tenure_months.metrics.quantiles.values",
        False,
    ),
]


@pytest.mark.parametrize("change, where, schema_refuses", BROKEN)
def test_a_broken_profile_is_refused_where_it_breaks(
    change: Callable[[Dict[str, Any]], None], where: str, schema_refuses: bool
) -> None:
    document = copy.deepcopy(EXAMPLE)
    change(document)
    with pytest.raises(FluxError) as raised:
        parse_profile(document)
    assert raised.value.code.code == "FLX-5036"
    assert where in raised.value.detail, raised.value.detail
    if schema_refuses:
        assert list(VALIDATOR.iter_errors(document)), "the schema accepts what the reader refuses"


@pytest.mark.parametrize(
    "text, problem",
    [
        ('{"a": 1.5}', "JSON float"),
        ('{"a": NaN}', "NaN"),
        ('{"a": 1, "a": 2}', "one key twice"),
        ("{", "not JSON"),
        ('{"a": ' + "9" * 5000 + "}", "more digits than any count"),
        ("[" * 100000 + "]" * 100000, "nests deeper"),
        ('{"a": ' * 200 + "1" + "}" * 200, "nests deeper"),
    ],
)
def test_the_text_reader_refuses_what_has_no_one_exact_meaning(text: str, problem: str) -> None:
    with pytest.raises(FluxError) as raised:
        load_profile_text(text)
    assert raised.value.code.code == "FLX-5036" and problem in raised.value.detail


@pytest.mark.parametrize(
    "text, accepted",
    [
        ("9" * 1000, True),
        ("-" + "9" * 1000, True),
        ("9" * 999 + ".9", True),
        ("9" * 1001, False),
        ("-" + "9" * 1001, False),
        ("9" * 1000 + ".9", False),
    ],
)
def test_a_decimal_carries_at_most_a_thousand_digits(text: str, accepted: bool) -> None:
    document = copy.deepcopy(EXAMPLE)
    _set(*TENURE, "metrics", "sum", "value", value=text)(document)
    schema_accepts = not list(VALIDATOR.iter_errors(document))
    assert schema_accepts is accepted
    if accepted:
        parse_profile(document)
    else:
        with pytest.raises(FluxError, match="more than 1000 digits"):
            parse_profile(document)


def test_a_timestamp_column_counts_in_integers_and_values_in_milliseconds() -> None:
    document = copy.deepcopy(EXAMPLE)
    metrics = _at(document, *PAID_AT, "metrics")
    for name in ("count", "nullCount", "distinctCount"):
        metrics[name] = {
            "definition": f"{name}/1",
            "status": "F",
            "sampleSize": {"min": 3},
            "disclosure": "acro-default",
            "value": 3,
        }
    assert not list(VALIDATOR.iter_errors(document))
    column = parse_profile(document).column("payment", "paid_at")
    assert column is not None
    counted = {name: column.metrics[name].value for name in ("count", "nullCount", "distinctCount")}
    assert counted == {"count": 3, "nullCount": 3, "distinctCount": 3}


def test_a_name_outside_nfc_is_refused() -> None:
    document = copy.deepcopy(EXAMPLE)
    columns = document["tables"]["payment"]["columns"]
    columns["état"] = {"domain": "number"}
    with pytest.raises(FluxError, match="Normalization Form C"):
        parse_profile(document)


def test_the_text_reader_returns_the_document_it_read() -> None:
    profile, document = load_profile_text(json.dumps(EXAMPLE))
    assert document == EXAMPLE and profile == parse_profile(copy.deepcopy(EXAMPLE))


# ---------------------------------------------------------------------------
# The column metrics
# ---------------------------------------------------------------------------


def test_the_column_metrics_are_openlineage_s_seven_names() -> None:
    assert sorted(entry.name for entry in metrics()) == [
        "count",
        "distinctCount",
        "max",
        "min",
        "nullCount",
        "quantiles",
        "sum",
    ]
    assert all(entry.identifier == f"{entry.name}/1" for entry in metrics())


def test_the_column_metrics_measure_as_the_fidelity_check_reads() -> None:
    values = [Fraction(3), None, Fraction(1), Fraction(3), Fraction(5), None]
    levels = (Fraction(0), Fraction(1, 2), Fraction(1))
    measured = {entry.name: entry.measure(values, levels) for entry in metrics()}
    assert measured["count"] == 4 and measured["nullCount"] == 2
    assert measured["distinctCount"] == 3
    assert measured["sum"] == Fraction(12)
    assert (measured["min"], measured["max"]) == (Fraction(1), Fraction(5))
    assert measured["quantiles"] == {Fraction(0): 1, Fraction(1, 2): 3, Fraction(1): 5}
    nothing = {entry.name: entry.measure([None], levels) for entry in metrics()}
    assert (nothing["min"], nothing["max"], nothing["quantiles"]) == (None, None, {})


def test_a_metric_spells_and_reads_back_its_value() -> None:
    for entry, value in (
        (metric("count/1"), 7),
        (metric("sum/1"), Fraction(-251, 4)),
        (metric("quantiles/1"), {Fraction(1, 2): Fraction(3, 2)}),
    ):
        spelled = entry.spell(value)
        assert entry.read(spelled, "m") == value
    assert metric("sum/1").spell(Fraction(-251, 4)) == {"value": "-62.75"}
    with pytest.raises(ValueError, match="terminating"):
        metric("sum/1").spell(Fraction(1, 3))


def test_a_metric_cannot_take_a_statistic_s_identifier() -> None:
    from dataclasses import replace

    with pytest.raises(ValueError, match="already registered"):
        register_metric(replace(COUNT, identifier="mean/1", name="mean"))
    with pytest.raises(ValueError, match="name part"):
        register_metric(replace(COUNT, identifier="median/1"))
