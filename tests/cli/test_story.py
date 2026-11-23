# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim story``: one entity's history, read from a committed run (ADR 0031 section 3).

The bundle is ``tests/fixtures/related-star`` with a timestamp column added to its sales, so
the order of a story has a date to follow. It is planned and applied once; every test then
reads that run directory.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from flux_engine.cli import main
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.story import escape_control
from tests.bundles import repin_contract

from .conftest import REPO_ROOT

pytestmark = pytest.mark.integration

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "related-star"
_SALES_COLUMN = '        - { name: ss_net_paid, type: "DECIMAL(7,2)", required: true }\n'
_SALES_FIDELITY = '            ss_net_paid: { range: { min: "1", max: "500" } }\n'


def _dated_bundle(target: Path) -> Path:
    shutil.copytree(FIXTURE, target)
    contract = target / "store-star.fluid.yml"
    text = contract.read_text("utf-8")
    assert text.count(_SALES_COLUMN) == 1
    contract.write_text(
        text.replace(
            _SALES_COLUMN,
            _SALES_COLUMN + "        - { name: ss_sold_at, type: TIMESTAMP, required: true }\n",
        ),
        "utf-8",
    )
    simulation = target / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    assert text.count(_SALES_FIDELITY) == 1
    simulation.write_text(
        text.replace(
            _SALES_FIDELITY,
            _SALES_FIDELITY
            + '            ss_sold_at: { range: { min: "2026-07-01T00:00:00Z", max: "2026-09-30T00:00:00Z" } }\n',
        ),
        "utf-8",
    )
    repin_contract(contract, simulation, "retail.store_star")
    return target


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Tuple[Path, str]:
    root = tmp_path_factory.mktemp("story")
    state_dir = root / ".flux"
    applied = apply(_dated_bundle(root / "bundle"), state_dir=state_dir)
    assert applied.success, applied.stderr
    return state_dir, str(applied.artifacts["run_id"])


def _rows(layout: RunLayout, expose_id: str) -> List[List[Any]]:
    text = layout.emit_path(expose_id).read_text("utf-8")
    return [json.loads(line) for line in text.splitlines()]


def _story(
    run: Tuple[Path, str], capsys: pytest.CaptureFixture[str], *flags: str
) -> Tuple[int, str, str]:
    state_dir, run_id = run
    status = main(["story", "--state-dir", str(state_dir), "--run-id", run_id, *flags])
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def _customer_with_sales(run: Tuple[Path, str]) -> Tuple[str, List[int]]:
    """A customer id and the row indexes of the sales that name it."""
    layout = RunLayout.for_state_dir(*run)
    customers = _rows(layout, "customer")
    sales = _rows(layout, "store_sales")
    for customer in customers:
        owned = [number for number, sale in enumerate(sales) if sale[2] == customer[0]]
        if len(owned) >= 3:
            return str(customer[0]), owned
    raise AssertionError("no customer with three sales")


def _documents(out: str) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in out.splitlines()]


def test_a_parent_key_tells_the_child_rows_in_date_order(
    run: Tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    customer, owned = _customer_with_sales(run)
    status, out, _ = _story(
        run, capsys, "--emit", "customer", "--key", customer, "--depth", "1", "--json"
    )
    assert status == 0
    rows = _documents(out)
    assert {row["row"] for row in rows if row["expose"] == "store_sales"} == set(owned)
    assert [row["expose"] for row in rows].count("customer") == 1
    # The entity comes first; the rest follow in date order.
    assert rows[0]["expose"] == "customer" and rows[0]["date"] is None
    dated = [row["date"] for row in rows[1:]]
    assert dated == sorted(dated) and len(dated) == len(owned)


def test_the_human_form_is_one_tab_separated_line_per_row(
    run: Tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    customer, owned = _customer_with_sales(run)
    status, out, _ = _story(run, capsys, "--emit", "customer", "--key", customer, "--depth", "1")
    assert status == 0
    header, *lines = out.splitlines()
    assert header.startswith("1 row of customer matched ")
    assert len(lines) == len(owned) + 1
    assert lines[0].split("\t")[:5] == ["-", "customer", customer, "0", "-"]
    date, expose, key, hop, relationship, *pairs = lines[1].split("\t")
    assert expose == "store_sales" and date.startswith("2026-")
    assert hop == "1" and relationship != "-"
    assert any(pair == f"ss_customer_id={customer}" for pair in pairs)


def test_depth_one_stays_with_the_direct_links_and_depth_two_goes_on(
    run: Tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    customer, _ = _customer_with_sales(run)
    _, one, _ = _story(
        run, capsys, "--emit", "customer", "--key", customer, "--depth", "1", "--json"
    )
    _, two, _ = _story(run, capsys, "--emit", "customer", "--key", customer, "--json")
    _, none, _ = _story(
        run, capsys, "--emit", "customer", "--key", customer, "--depth", "0", "--json"
    )
    assert {row["expose"] for row in _documents(one)} == {"customer", "store_sales"}
    assert {"date_dim", "item"} <= {row["expose"] for row in _documents(two)}
    assert [row["expose"] for row in _documents(none)] == ["customer"]


def test_a_composite_key_names_each_column(
    run: Tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    layout = RunLayout.for_state_dir(*run)
    sale = _rows(layout, "store_sales")[0]
    status, out, _ = _story(
        run,
        capsys,
        "--emit",
        "store_sales",
        "--key",
        f"ss_item_sk={sale[1]}",
        "--key",
        f"ss_ticket_number={sale[3]}",
        "--depth",
        "1",
        "--json",
    )
    assert status == 0
    rows = _documents(out)
    assert any(row["expose"] == "store_sales" and row["row"] == 0 for row in rows)
    assert {"item", "customer"} <= {row["expose"] for row in rows}


def test_the_limit_cuts_the_story_and_says_so(
    run: Tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    customer, _ = _customer_with_sales(run)
    status, out, err = _story(
        run, capsys, "--emit", "customer", "--key", customer, "--limit", "2", "--json"
    )
    assert status == 0
    assert len(_documents(out)) == 2
    assert "--limit" in err
    status, out, err = _story(
        run, capsys, "--emit", "customer", "--key", customer, "--limit", "500", "--json"
    )
    assert status == 0 and len(_documents(out)) > 2 and err == ""


def test_a_key_the_expose_does_not_hold_is_refused_by_name(
    run: Tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    status, out, err = _story(run, capsys, "--emit", "customer", "--key", "no-such-customer")
    assert status == 1 and out == ""
    assert "customer" in err and "no-such-customer" in err
    status, _, err = _story(run, capsys, "--emit", "no_such_expose", "--key", "1")
    assert status == 1 and "no_such_expose" in err


def test_the_entity_comes_first_and_every_row_says_how_far_it_is(
    run: Tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    customer, owned = _customer_with_sales(run)
    status, out, _ = _story(run, capsys, "--emit", "customer", "--key", customer, "--json")
    assert status == 0
    rows = _documents(out)
    assert all("hop" in row and "relationship" in row for row in rows)
    own = [row for row in rows if row["hop"] == 0]
    assert [row["expose"] for row in own] == ["customer"] and own[0]["relationship"] is None
    assert rows[0]["hop"] == 0
    assert all(row["hop"] != 0 for row in rows[1:])
    sales = [row for row in rows if row["expose"] == "store_sales"]
    assert {row["hop"] for row in sales} == {1} and len(sales) == len(owned)
    assert all(isinstance(row["relationship"], str) and row["relationship"] for row in sales)
    assert max(row["hop"] for row in rows) == 2
    farther = [row for row in rows if row["hop"] == 2]
    assert farther and all(row["relationship"] for row in farther)


def test_every_row_that_matches_the_key_is_an_entity(
    run: Tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    customer, owned = _customer_with_sales(run)
    flags = ("--emit", "store_sales", "--key", f"ss_customer_id={customer}", "--depth", "0")
    status, out, _ = _story(run, capsys, *flags, "--json")
    assert status == 0
    rows = _documents(out)
    assert sorted(row["row"] for row in rows) == sorted(owned)
    assert {row["hop"] for row in rows} == {0}
    status, out, _ = _story(run, capsys, *flags)
    header = out.splitlines()[0]
    assert header.startswith(f"{len(owned)} rows of store_sales matched ")
    assert f"ss_customer_id={customer}" in header
    assert len(out.splitlines()) == len(owned) + 1


def test_a_refusal_under_json_is_an_envelope_with_its_code(
    run: Tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir, run_id = run
    base = ["-j", "story", "--state-dir", str(state_dir), "--run-id", run_id]
    cases = [
        (["--emit", "no_such_expose", "--key", "1"], "FLX-2111", "no_such_expose"),
        (["--emit", "customer", "--key", "no-such-customer"], "FLX-2121", "no-such-customer"),
        (["--emit", "customer", "--key", "a", "--key", "b"], "FLX-2121", "COLUMN=VALUE"),
    ]
    for tail, code, text in cases:
        status = main([*base, *tail])
        captured = capsys.readouterr()
        envelope = json.loads(captured.out)
        assert status == 1 and envelope["exitCode"] == 1 and envelope["ok"] is False
        assert envelope["stage"] == "story" and envelope["artifacts"] == {}
        assert envelope["error"]["code"] == code and text in envelope["error"]["message"]
    status = main(
        ["-j", "story", "--state-dir", str(state_dir), "--run-id", "0" * 16]
        + ["--emit", "customer", "--key", "1"]
    )
    envelope = json.loads(capsys.readouterr().out)
    assert status == 1 and envelope["error"]["code"] == "FLX-2109"


def test_a_mistyped_key_column_is_named_with_the_columns_the_expose_has(
    run: Tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir, run_id = run
    status = main(
        ["-j", "story", "--state-dir", str(state_dir), "--run-id", run_id]
        + ["--emit", "store_sales", "--key", "ss_itm_sk=1"]
    )
    error = json.loads(capsys.readouterr().out)["error"]
    assert status == 1 and error["code"] == "FLX-2121"
    assert "ss_itm_sk is not one of them" in error["message"]
    assert "ss_item_sk" in error["message"]
    status = main(
        ["-j", "story", "--state-dir", str(state_dir), "--run-id", run_id]
        + ["--emit", "customer", "--key", "idd=1"]
    )
    error = json.loads(capsys.readouterr().out)["error"]
    assert status == 1 and error["code"] == "FLX-2121"
    assert "idd is not a column of customer" in error["message"]
    assert "its key columns:" in error["message"] or "its columns:" in error["message"]


def test_a_stream_that_no_longer_matches_the_receipt_is_refused(
    run: Tuple[Path, str], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    state_dir, run_id = run
    copied = tmp_path / ".flux"
    shutil.copytree(state_dir, copied)
    layout = RunLayout.for_state_dir(copied, run_id)
    customer, _ = _customer_with_sales(run)
    path = layout.emit_path("store_sales")
    path.write_text(path.read_text("utf-8").replace("2026", "2027", 1), "utf-8")
    status = main(
        [
            "story",
            "--state-dir",
            str(copied),
            "--run-id",
            run_id,
            "--emit",
            "customer",
            "--key",
            customer,
        ]
    )
    captured = capsys.readouterr()
    assert status == 1 and captured.out == ""
    assert "FLX-2110" in captured.err and "emits.store_sales" in captured.err


def test_the_story_writes_nothing(
    run: Tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir, _ = run
    before = sorted((p, p.stat().st_mtime_ns) for p in state_dir.rglob("*"))
    customer, _ = _customer_with_sales(run)
    _story(run, capsys, "--emit", "customer", "--key", customer)
    assert sorted((p, p.stat().st_mtime_ns) for p in state_dir.rglob("*")) == before


def test_control_and_bidirectional_characters_are_written_out() -> None:
    assert escape_control("\x1b[31mred") == "\\x1b[31mred"
    assert escape_control("a‮b") == "a\\u202eb"
    assert escape_control("a b c") == "a\\u2028b\\u2029c"
    assert escape_control("\x00\x7f\x85؜‎‏⁦⁩") == (
        "\\x00\\x7f\\x85\\u061c\\u200e\\u200f\\u2066\\u2069"
    )
    assert escape_control("plain \\ text, café 中") == "plain \\ text, café 中"


_HOSTILE = "x\x1b[2J\nFLX-0000: forged‮"


def _inert(text: str) -> None:
    """*text* holds no escape, no bidirectional control and one line, apart from its ending.

    The ``more:`` pointer a coded failure adds is a second line of the engine's own.
    """
    assert "\x1b" not in text and "‮" not in text
    lines = [line for line in text.strip().splitlines() if not line.startswith("more: ")]
    assert len(lines) == 1


def test_a_refusal_never_prints_the_key_or_the_expose_it_was_given_raw(
    run: Tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    status, _, err = _story(run, capsys, "--emit", "customer", "--key", _HOSTILE)
    assert status == 1
    _inert(err)
    assert "\\x1b[2J\\x0aFLX-0000: forged\\u202e" in err
    status, _, err = _story(run, capsys, "--emit", "store_sales", "--key", f"ss_item_sk={_HOSTILE}")
    assert status == 1
    _inert(err)
    status, _, err = _story(run, capsys, "--emit", _HOSTILE, "--key", "1")
    assert status == 1
    _inert(err)


def test_a_refusal_never_prints_a_column_name_of_the_run_raw() -> None:
    from flux_engine.stages.story import _entity_columns, _Refused, _Stream

    stream = _Stream(("id", _HOSTILE), ("INT", "STRING"), [], {})
    with pytest.raises(_Refused) as one_column:
        _entity_columns(["1"], stream, "orders", ())
    _inert(str(one_column.value))
    assert "\\x1b[2J\\x0aFLX-0000: forged\\u202e" in str(one_column.value)
    with pytest.raises(_Refused) as twice:
        _entity_columns([f"{_HOSTILE}=1", f"{_HOSTILE}=2"], stream, "orders", ())
    _inert(str(twice.value))
    with pytest.raises(_Refused) as composite:
        _entity_columns(["1", "2"], _Stream(("id",), ("INT",), [], {}), _HOSTILE, ())
    _inert(str(composite.value))
