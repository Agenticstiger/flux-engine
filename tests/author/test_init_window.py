# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The run's window: the hull of every date column's range, the contract's date ranges, ``--window``."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from flux_engine.author.bundle import AuthoredExpose, _time_bounds
from flux_engine.author.columns import ColumnContext, ColumnPlan, bound_to_window
from flux_engine.author.window import (
    WindowError,
    declared_instants,
    instant_ms,
    parse_window,
)
from flux_engine.cli._init import run_init
from flux_engine.core.clock import format_rfc3339_ms
from flux_engine.seam.contract import LogicalKind

from .test_init_from_profile import _simulation

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = REPO_ROOT / "tests" / "fixtures" / "author" / "orders.fluid.yaml"
PROFILE = REPO_ROOT / "tests" / "fixtures" / "author" / "orders.profile.json"


def _ms(text: str) -> int:
    found = instant_ms(text)
    assert found is not None
    return found


def _rule(constraint: str, kind: str = "range") -> Dict[str, Any]:
    return {"name": "c", "validationRules": [{"type": kind, "constraint": constraint}]}


# -- the date range a contract declares ----------------------------------------------------------


@pytest.mark.parametrize(
    "constraint",
    [
        "between '2020-01-01' and '2020-12-31'",
        'BETWEEN "2020-01-01" AND "2020-12-31"',
        "between 2020-01-01 and 2020-12-31",
        ">= '2020-01-01' and <= '2020-12-31'",
        ">'2020-01-01' AND <'2020-12-31'",
        "<= 2020-12-31 and >= 2020-01-01",
    ],
)
def test_a_date_range_is_read_in_each_spelling(constraint: str) -> None:
    assert declared_instants(_rule(constraint)) == (_ms("2020-01-01"), _ms("2020-12-31"))


def test_a_timestamp_with_no_offset_is_utc() -> None:
    found = declared_instants(_rule("between '2020-01-01T06:00:00' and '2020-01-01T08:30:00Z'"))
    assert found == (_ms("2020-01-01") + 6 * 3_600_000, _ms("2020-01-01") + 8.5 * 3_600_000)


def test_several_rules_tighten_each_side() -> None:
    column = {
        "validationRules": [
            {"type": "range", "constraint": "between '2020-01-01' and '2020-12-31'"},
            {"type": "range", "constraint": ">= '2020-03-01'"},
            {"type": "range", "constraint": "<= '2020-09-01'"},
        ]
    }
    assert declared_instants(column) == (_ms("2020-03-01"), _ms("2020-09-01"))


@pytest.mark.parametrize(
    "constraint",
    [
        ">= '2020-01-01'",
        "<= '2020-12-31'",
        "between '2020-12-31' and '2020-01-01'",
        "between '2020-01-01' and '2020-01-01'",
        "between 'soon' and 'later'",
        "length < 5",
        "",
    ],
)
def test_a_range_that_does_not_declare_both_ends_declares_nothing(constraint: str) -> None:
    assert declared_instants(_rule(constraint)) is None


def test_other_rules_and_shapes_declare_nothing() -> None:
    assert declared_instants(_rule("between '2020-01-01' and '2020-12-31'", "pattern")) is None
    assert declared_instants({"validationRules": "x"}) is None
    assert declared_instants("x") is None
    assert declared_instants({}) is None


# -- --window --------------------------------------------------------------------------------------


def test_a_window_is_written_as_rfc3339_milliseconds() -> None:
    assert parse_window("2025-01-01", "2025-06-01T10:00:00+02:00") == (
        "2025-01-01T00:00:00.000Z",
        "2025-06-01T08:00:00.000Z",
    )


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("soon", "2025-01-01"),
        ("2025-01-01", "2025-13-01"),
        ("2025-01-02", "2025-01-01"),
        ("2025-01-01", "2025-01-01"),
        ("0000-01-01", "2025-01-01"),
        ("2025-01-01", "10000-01-01"),
        ("2025-01-01", ""),
    ],
)
def test_a_window_that_is_not_read_is_refused(start: str, end: str) -> None:
    with pytest.raises(WindowError):
        parse_window(start, end)


# -- the hull ---------------------------------------------------------------------------------------


def _expose(*columns: ColumnPlan, kinds: Optional[Dict[str, LogicalKind]] = None) -> AuthoredExpose:
    expose = AuthoredExpose(expose_id="t")
    expose.columns = list(columns)
    expose.kinds = kinds or {column.name: LogicalKind.DATE for column in columns}
    return expose


def _window_of(expose: AuthoredExpose, contract: Any = None) -> Any:
    return _time_bounds([expose], contract)[0]


def test_the_window_covers_a_columns_own_bins_beside_a_published_span() -> None:
    published = ColumnPlan(name="month", time_span=(_ms("2025-10-01"), _ms("2026-09-01")))
    ended = ColumnPlan(
        name="ended",
        declaration={
            "bins": ["2000-01-01", "2010-01-01", "2020-01-01"],
            "binWeights": ["0.5", "0.5"],
        },
    )
    expose = _expose(published, ended)
    window = _window_of(expose)
    assert window == (format_rfc3339_ms(_ms("2000-01-01")), format_rfc3339_ms(_ms("2026-09-01")))
    # Nothing is cut, since the window holds every bin.
    before = dict(ended.declaration)
    bound_to_window(ended, LogicalKind.DATE, window)
    assert ended.declaration == before


def test_a_column_declared_by_levels_and_a_range_gives_its_values() -> None:
    by_levels = ColumnPlan(name="a", declaration={"levels": ["2001-05-01", "2003-01-01"]})
    by_range = ColumnPlan(
        name="b", declaration={"range": {"min": "2030-01-01", "max": "2031-01-01"}}
    )
    window = _window_of(_expose(by_levels, by_range))
    assert window == (format_rfc3339_ms(_ms("2001-05-01")), format_rfc3339_ms(_ms("2031-01-01")))


def test_a_column_that_is_not_a_date_gives_no_span() -> None:
    word = ColumnPlan(name="w", declaration={"levels": ["2001-05-01"]})
    note = _time_bounds([_expose(word, kinds={"w": LogicalKind.STRING})])[1]
    assert note is not None and note[0] == "time window"


def test_a_declared_contract_date_range_widens_the_window() -> None:
    column = ColumnPlan(name="d", time_span=(_ms("2025-10-01"), _ms("2026-09-01")))
    expose = _expose(column)
    expose.index = 0
    context = ColumnContext.__new__(ColumnContext)
    object.__setattr__(context, "schema_index", 0)
    expose.contexts = {"d": context}
    contract = {
        "exposes": [{"contract": {"schema": [_rule("between '2020-01-01' and '2028-01-01'")]}}]
    }
    assert _window_of(expose, contract) == (
        format_rfc3339_ms(_ms("2020-01-01")),
        format_rfc3339_ms(_ms("2028-01-01")),
    )


def test_no_range_anywhere_keeps_the_assumed_year() -> None:
    window, note = _time_bounds([_expose(ColumnPlan(name="d"))])
    assert window[0].startswith("2026-01-01") and note is not None


# -- init --------------------------------------------------------------------------------------------


def test_init_writes_exactly_the_window_it_is_given(tmp_path: Path) -> None:
    result = run_init(
        str(tmp_path / "b"),
        contract=str(CONTRACT),
        profile=str(PROFILE),
        window=("2024-01-01", "2024-03-31T12:00:00Z"),
    )
    assert result.exit_code == 0, result.stderr
    bounds = _simulation(tmp_path / "b")["spec"]["timeBounds"]
    assert bounds == {"start": "2024-01-01T00:00:00.000Z", "end": "2024-03-31T12:00:00.000Z"}
    assert (
        "The run covers 2024-01-01T00:00:00.000Z to 2024-03-31T12:00:00.000Z, as --window gives."
        in (result.stdout)
    )
    assert "No date or timestamp column publishes its range" not in result.stdout


def test_a_given_window_is_no_gap(tmp_path: Path) -> None:
    result = run_init(
        str(tmp_path / "b"),
        contract=str(CONTRACT),
        profile=str(PROFILE),
        strict=True,
        window=("2024-01-01", "2024-03-31"),
    )
    assert "gap /spec/timeBounds" not in (result.stderr or "")


def test_a_contract_date_range_gives_the_window_of_the_bundle(tmp_path: Path) -> None:
    text = CONTRACT.read_text(encoding="utf-8").replace(
        "      type: timestamp\n      required: false\n",
        "      type: timestamp\n      required: false\n      validationRules:\n"
        "      - type: range\n"
        "        constraint: \"between '2020-02-01' and '2021-02-01'\"\n",
    )
    contract = tmp_path / "orders.fluid.yaml"
    contract.write_text(text, encoding="utf-8")
    result = run_init(str(tmp_path / "b"), contract=str(contract), profile=str(PROFILE))
    assert result.exit_code == 0, result.stderr
    bounds = _simulation(tmp_path / "b")["spec"]["timeBounds"]
    assert bounds["start"].startswith("2020-02-01") and bounds["end"].startswith("2021-02-01")


def test_window_needs_the_contract_and_the_profile(tmp_path: Path) -> None:
    window = ("2024-01-01", "2024-03-31")
    for kwargs in ({}, {"contract": str(CONTRACT)}, {"profile": str(PROFILE)}):
        result = run_init(str(tmp_path / "x"), window=window, **kwargs)
        assert result.exit_code == 2
        if "profile" not in kwargs:
            assert "--window" in (result.stderr or "")
    assert not (tmp_path / "x").exists()


@pytest.mark.parametrize(
    "window", [("soon", "2024-01-01"), ("2024-02-01", "2024-01-01"), ("0000-01-01", "2024-01-01")]
)
def test_a_bad_window_is_exit_2_and_names_the_value(tmp_path: Path, window: Any) -> None:
    result = run_init(
        str(tmp_path / "b"), contract=str(CONTRACT), profile=str(PROFILE), window=window
    )
    assert result.exit_code == 2
    assert "--window" in (result.stderr or "")
    assert not (tmp_path / "b").exists()


def test_the_option_is_on_the_command_line(tmp_path: Path) -> None:
    from flux_engine.cli import main

    code = main(
        [
            "init",
            str(tmp_path / "b"),
            "--contract",
            str(CONTRACT),
            "--profile",
            str(PROFILE),
            "--window",
            "2024-01-01",
            "2024-03-31",
        ]
    )
    assert code == 0
    assert _simulation(tmp_path / "b")["spec"]["timeBounds"]["start"] == "2024-01-01T00:00:00.000Z"


# -- a window that does not hold a date column's range --------------------------------------------

JOURNEY = REPO_ROOT / "tests" / "fixtures" / "init-verifies" / "journey-d"


def test_a_window_that_does_not_hold_a_published_range_is_refused(tmp_path: Path) -> None:
    result = run_init(
        str(tmp_path / "b"),
        contract=str(JOURNEY / "contract.fluid.yaml"),
        profile=str(JOURNEY / "profile.json"),
        window=("2024-03-01", "2025-10-01"),
    )
    assert result.exit_code == 1, result.stdout
    message = result.stderr or ""
    assert message.startswith("init: ")
    assert "account.signed_up" in message
    assert "2024-01-01" in message and "2025-03-01" in message
    assert "2024-03-01" in message and "2025-10-01" in message
    assert "give a window that holds" in message
    assert not (tmp_path / "b").exists()


def test_a_window_that_holds_every_range_is_kept(tmp_path: Path) -> None:
    result = run_init(
        str(tmp_path / "b"),
        contract=str(JOURNEY / "contract.fluid.yaml"),
        profile=str(JOURNEY / "profile.json"),
        window=("2023-01-01", "2026-01-01"),
    )
    assert result.exit_code == 0, result.stderr


def test_a_window_that_does_not_hold_a_declared_contract_range_is_refused(tmp_path: Path) -> None:
    text = CONTRACT.read_text(encoding="utf-8").replace(
        "      type: timestamp\n      required: false\n",
        "      type: timestamp\n      required: false\n      validationRules:\n"
        "      - type: range\n"
        "        constraint: \"between '2020-02-01' and '2021-02-01'\"\n",
    )
    contract = tmp_path / "orders.fluid.yaml"
    contract.write_text(text, encoding="utf-8")
    result = run_init(
        str(tmp_path / "b"),
        contract=str(contract),
        profile=str(PROFILE),
        window=("2020-06-01", "2021-02-01"),
    )
    assert result.exit_code == 1, result.stdout
    message = result.stderr or ""
    assert "2020-02-01" in message and "2021-02-01" in message and "2020-06-01" in message
    assert "give a window that holds" in message
