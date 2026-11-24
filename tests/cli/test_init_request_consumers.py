# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init --contract C --consumers F`` with no profile: the request asks for query rates.

Pinned here: the request and the held-out request written for ``tests/fixtures/consumers`` (as
files), the workload weights, that a held-out query adds nothing to the workload, that a request
without consumers keeps its format and text, and that neither request is ever overwritten
(ADR 0028 section 6).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

pytest.importorskip("sqlglot")

from flux_engine.cli._init import run_init  # noqa: E402
from flux_engine.seam.profile_request import workload_of  # noqa: E402
from flux_stats.request import (  # noqa: E402
    REQUEST_SCHEMA_URL,
    REQUEST_SCHEMA_URL_1_1_0,
    REQUEST_SCHEMA_URL_1_2_0,
    RequestWorkload,
    load_request_text,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "consumers"
CONTRACT = FIXTURE / "subscriptions.fluid.yaml"
CONSUMERS = FIXTURE / "consumers.yml"


def _write(tmp_path: Path, queries: List[Dict[str, Any]], **block: Any) -> Path:
    path = tmp_path / "consumers.yml"
    path.write_text(
        yaml.safe_dump({"queries": queries, **block}, sort_keys=False), encoding="utf-8"
    )
    return path


def _q(name: str, sql: str, **more: Any) -> Dict[str, Any]:
    return {"id": name, "sql": f"SELECT * FROM subscriptions WHERE {sql}", **more}


def _ask(tmp_path: Path, consumers: Path, name: str = "b") -> Any:
    result = run_init(str(tmp_path / name), contract=str(CONTRACT), consumers=str(consumers))
    assert result.exit_code == 0, result.stderr
    return result


def _request(tmp_path: Path, name: str = "b") -> Any:
    return load_request_text((tmp_path / f"{name}.profile-request.yml").read_text(encoding="utf-8"))


def _workload(request: Any) -> Dict[Any, int]:
    return {entry.columns: entry.weight for entry in request.tables[0].workload}


# ---------------------------------------------------------------------------
# The fixture, pinned as files
# ---------------------------------------------------------------------------


def test_the_request_for_the_fixture_is_the_pinned_file(tmp_path: Path) -> None:
    _ask(tmp_path, CONSUMERS)
    written = (tmp_path / "b.profile-request.yml").read_bytes()
    assert written == (FIXTURE / "subscriptions.profile-request.yml").read_bytes()


def test_the_held_out_request_for_the_fixture_is_the_pinned_file(tmp_path: Path) -> None:
    _ask(tmp_path, CONSUMERS)
    written = (tmp_path / "b.held-out-request.yml").read_bytes()
    assert written == (FIXTURE / "subscriptions.held-out-request.yml").read_bytes()


def test_the_pinned_requests_read_and_say_what_the_spec_says() -> None:
    request = load_request_text(
        (FIXTURE / "subscriptions.profile-request.yml").read_text(encoding="utf-8")
    )
    assert [query.name for query in request.queries] == ["premium-renewed", "renewed-offered"]
    first, second = request.queries
    assert first.unit.table == "subscriptions" and first.unit.sql is None and first.group_by is None
    assert second.unit.table is None and second.group_by == "plan"
    assert second.unit.sql is not None and second.unit.sql.startswith("SELECT ")
    assert "'premium'" in first.sql and "sub_plan" not in first.sql  # canonical: contract names
    assert _workload(request) == {("offered", "plan", "renewed"): 1, ("plan", "renewed"): 1}

    held_out = load_request_text(
        (FIXTURE / "subscriptions.held-out-request.yml").read_text(encoding="utf-8")
    )
    assert [query.name for query in held_out.queries] == ["renewed-since"]
    assert [(table.expose_id, table.columns, table.workload) for table in held_out.tables] == [
        ("subscriptions", (), ())
    ]
    assert held_out.contract_digest == request.contract_digest


def test_the_held_out_request_names_format_1_1_0_and_the_profile_request_1_2_0(
    tmp_path: Path,
) -> None:
    """The profile request also asks for correlations (format 1-2-0, which holds every 1-1-0 key)."""
    _ask(tmp_path, CONSUMERS)
    held_out = (tmp_path / "b.held-out-request.yml").read_text(encoding="utf-8")
    assert REQUEST_SCHEMA_URL_1_1_0 in held_out
    asked = (tmp_path / "b.profile-request.yml").read_text(encoding="utf-8")
    assert REQUEST_SCHEMA_URL_1_2_0 in asked and '"queries":' in asked


def test_the_report_names_both_requests_and_the_command_that_follows(tmp_path: Path) -> None:
    result = _ask(tmp_path, CONSUMERS)
    assert "the request asks for the rate of 2 queries and lists 2 sets of columns" in result.stdout
    assert "wrote the held-out request" in result.stdout and "1 query, no column" in result.stdout
    assert "its profile is read by `verify` and `calibrate` only" in result.stdout
    assert result.artifacts["queries"] == 2 and result.artifacts["workload"] == 2
    assert result.artifacts["heldOutQueries"] == 1
    assert result.artifacts["heldOutRequest"].endswith("b.held-out-request.yml")
    assert result.artifacts["next"][-1].endswith(f"--consumers {CONSUMERS}")


def test_the_same_inputs_write_the_same_bytes(tmp_path: Path) -> None:
    _ask(tmp_path, CONSUMERS, "one")
    _ask(tmp_path, CONSUMERS, "two")
    for kind in ("profile-request", "held-out-request"):
        assert (tmp_path / f"one.{kind}.yml").read_bytes() == (
            tmp_path / f"two.{kind}.yml"
        ).read_bytes()


# ---------------------------------------------------------------------------
# The workload
# ---------------------------------------------------------------------------


def test_the_weight_is_the_number_of_calibration_queries_that_read_exactly_the_set(
    tmp_path: Path,
) -> None:
    consumers = _write(
        tmp_path,
        [
            _q("a", "plan = 'basic'", unit="subscriptions"),
            _q("b", "plan = 'premium' AND amount > 5"),
            _q("c", "plan = 'basic' AND region = 'north'", unit="subscriptions"),
            _q("d", "region IN ('north', 'south') AND plan = 'x'"),
            _q("e", "region = 'east'", unit="subscriptions"),
        ],
    )
    _ask(tmp_path, consumers)
    assert _workload(_request(tmp_path)) == {
        ("plan",): 2,
        ("plan", "region"): 2,
        ("region",): 1,
    }


def test_a_held_out_query_adds_nothing_to_the_workload(tmp_path: Path) -> None:
    consumers = _write(
        tmp_path,
        [
            _q("a", "plan = 'basic'", unit="subscriptions"),
            _q("later", "plan = 'basic' AND region = 'north'", unit="subscriptions", heldOut=True),
        ],
    )
    _ask(tmp_path, consumers)
    assert _workload(_request(tmp_path)) == {("plan",): 1}
    held_out = load_request_text((tmp_path / "b.held-out-request.yml").read_text(encoding="utf-8"))
    assert all(not table.workload for table in held_out.tables)


def test_only_categorical_columns_count_and_a_set_with_none_is_no_entry(tmp_path: Path) -> None:
    consumers = _write(
        tmp_path,
        [
            _q("a", "amount > 5 AND renewed_at >= DATE '2025-02-01'", unit="subscriptions"),
            _q("b", "amount > 5 AND plan = 'basic'", unit="subscriptions"),
        ],
    )
    _ask(tmp_path, consumers)
    assert _workload(_request(tmp_path)) == {("plan",): 1}


def test_a_grouping_and_the_group_by_key_are_read(tmp_path: Path) -> None:
    consumers = _write(
        tmp_path,
        [
            {
                "id": "g",
                "sql": "SELECT region, count(*) AS n FROM subscriptions WHERE plan = 'basic' GROUP BY region",
                "unit": "subscriptions",
                "groupBy": "region",
            },
            {
                "id": "h",
                "sql": "SELECT plan, region FROM subscriptions",
                "unit": "subscriptions",
                "groupBy": "plan",
            },
        ],
    )
    _ask(tmp_path, consumers)
    assert _workload(_request(tmp_path)) == {("plan", "region"): 1, ("plan",): 1}


def test_a_query_without_a_unit_adds_to_the_workload_but_asks_for_no_rate(tmp_path: Path) -> None:
    consumers = _write(tmp_path, [_q("a", "plan = 'basic'")])
    result = _ask(tmp_path, consumers)
    request = _request(tmp_path)
    assert request.queries == ()
    assert _workload(request) == {("plan",): 1}
    assert result.artifacts["queries"] == 0
    assert not (tmp_path / "b.held-out-request.yml").exists()
    assert REQUEST_SCHEMA_URL_1_2_0 in (tmp_path / "b.profile-request.yml").read_text(
        encoding="utf-8"
    )


def test_a_subquery_over_the_same_table_adds_its_columns_to_the_querys_one_set(
    tmp_path: Path,
) -> None:
    consumers = _write(
        tmp_path,
        [
            {
                "id": "s",
                "sql": "SELECT * FROM subscriptions WHERE plan IN (SELECT plan FROM subscriptions WHERE region = 'north')",
                "unit": "subscriptions",
            }
        ],
    )
    _ask(tmp_path, consumers)
    assert _workload(_request(tmp_path)) == {("plan", "region"): 1}


def test_workload_of_drops_sets_past_eight_columns_and_counts_the_rest() -> None:
    names = frozenset(f"c{n}" for n in range(10))
    found = workload_of(
        [
            ("t", frozenset({"c1", "c2"})),
            ("t", frozenset({"c2", "c1"})),
            ("t", frozenset({"c1", "x"})),
            ("t", names),
            ("t", frozenset({"x"})),
            ("u", frozenset({"c1"})),
        ],
        {"t": names},
    )
    assert found == {
        "t": (RequestWorkload(("c1",), 1), RequestWorkload(("c1", "c2"), 2)),
    }


# ---------------------------------------------------------------------------
# What stays as it was, and what is never overwritten
# ---------------------------------------------------------------------------


def _contract_without_dates(tmp_path: Path) -> Path:
    """The fixture contract cut to its one key column: nothing asks a calendar, a correlation or a clique."""
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    schema = document["exposes"][0]["contract"]["schema"]
    schema[:] = schema[:1]
    path = tmp_path / "no-dates.fluid.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def test_a_request_with_a_date_column_asks_its_calendar(tmp_path: Path) -> None:
    result = run_init(str(tmp_path / "b"), contract=str(CONTRACT))
    assert result.exit_code == 0, result.stderr
    text = (tmp_path / "b.profile-request.yml").read_text(encoding="utf-8")
    assert REQUEST_SCHEMA_URL_1_2_0 in text and REQUEST_SCHEMA_URL not in text
    assert '"calendar":' in text and '"renewed_at"' in text
    assert "queries" not in text and "workload" not in text


def test_a_request_without_consumers_keeps_its_format(tmp_path: Path) -> None:
    result = run_init(str(tmp_path / "b"), contract=str(_contract_without_dates(tmp_path)))
    assert result.exit_code == 0, result.stderr
    text = (tmp_path / "b.profile-request.yml").read_text(encoding="utf-8")
    assert REQUEST_SCHEMA_URL in text and "1-1-0" not in text
    assert "queries" not in text and "workload" not in text
    assert not (tmp_path / "b.held-out-request.yml").exists()
    assert "queries" not in result.artifacts


def test_consumers_with_no_queries_keep_format_1_0_0(tmp_path: Path) -> None:
    path = tmp_path / "consumers.yml"
    path.write_text("dialect: duckdb\n", encoding="utf-8")
    result = run_init(
        str(tmp_path / "b"),
        contract=str(_contract_without_dates(tmp_path)),
        consumers=str(path),
    )
    assert result.exit_code == 0, result.stderr
    text = (tmp_path / "b.profile-request.yml").read_text(encoding="utf-8")
    assert REQUEST_SCHEMA_URL in text and "queries" not in text and "workload" not in text


def test_an_existing_held_out_request_is_never_overwritten_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    taken = tmp_path / "b.held-out-request.yml"
    taken.write_text("mine\n", encoding="utf-8")
    result = run_init(str(tmp_path / "b"), contract=str(CONTRACT), consumers=str(CONSUMERS))
    assert result.exit_code == 1
    assert "never overwrites" in result.stderr and "b.held-out-request.yml" in result.stderr
    assert taken.read_text(encoding="utf-8") == "mine\n"
    assert not (tmp_path / "b.profile-request.yml").exists()


def test_an_existing_request_is_never_overwritten(tmp_path: Path) -> None:
    taken = tmp_path / "b.profile-request.yml"
    taken.write_text("mine\n", encoding="utf-8")
    result = run_init(str(tmp_path / "b"), contract=str(CONTRACT), consumers=str(CONSUMERS))
    assert result.exit_code == 1 and "never overwrites" in result.stderr
    assert taken.read_text(encoding="utf-8") == "mine\n"
    assert not (tmp_path / "b.held-out-request.yml").exists()


def test_a_named_request_has_its_held_out_request_beside_it(tmp_path: Path) -> None:
    target = tmp_path / "asked.yml"
    result = run_init(
        str(tmp_path / "b"),
        contract=str(CONTRACT),
        consumers=str(CONSUMERS),
        request=str(target),
    )
    assert result.exit_code == 0, result.stderr
    assert target.is_file() and (tmp_path / "asked.held-out-request.yml").is_file()


def test_a_hostile_query_writes_neither_request(tmp_path: Path) -> None:
    consumers = _write(
        tmp_path, [{"id": "q", "sql": "DROP TABLE subscriptions", "unit": "subscriptions"}]
    )
    result = run_init(str(tmp_path / "b"), contract=str(CONTRACT), consumers=str(consumers))
    assert result.exit_code == 1 and "FLX-3620" in result.stderr
    assert list(tmp_path.glob("b.*")) == []
