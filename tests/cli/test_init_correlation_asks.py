# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init --contract C`` asks for correlations in request format 1-2-0 (ADR 0035 to 0037).

Pinned here: a table is asked for ``kendallTau/1`` over the number and timestamp columns that
``init`` would draw from histograms and for the columns a clique could hold; a relationship that
``init`` would lay out on the counted layout is asked to be grouped by its parent's drawn columns
and for the link groupings a child column could read; a column that a key, a relationship or a
rule of the contract sets is asked for none of them; and a request with none of these keeps the
format and the bytes it had.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from flux_engine.cli._init import run_init
from flux_stats.request import (
    MOST_CLIQUE_CANDIDATES,
    MOST_RANK_COLUMNS,
    REQUEST_SCHEMA_URL,
    REQUEST_SCHEMA_URL_1_2_0,
    ProfileRequest,
    load_request_text,
    render_request,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
BOOK = FIXTURES / "invoice-book-init" / "invoice-book.fluid.yml"
ORDERS = FIXTURES / "author" / "orders.fluid.yaml"


def _text(tmp_path: Path, contract: Path) -> str:
    result = run_init(str(tmp_path / "b"), contract=str(contract))
    assert result.exit_code == 0, result.stderr
    return (tmp_path / "b.profile-request.yml").read_text(encoding="utf-8")


def _request(tmp_path: Path, contract: Path) -> ProfileRequest:
    return load_request_text(_text(tmp_path, contract))


def _contract(tmp_path: Path, columns: List[str], name: str = "t") -> Path:
    """One table whose schema is the lines in *columns*, in the contract's own flow style."""
    body = "\n".join(f"        - {line}" for line in columns)
    path = tmp_path / f"{name}.fluid.yaml"
    path.write_text(
        f"""\
fluidVersion: "0.7.7"
kind: DataProduct
id: shop.{name}
name: {name}
metadata:
  owner:
    team: shop
exposes:
  - exposeId: {name}
    kind: table
    binding:
      platform: local
      format: parquet
      location: {{ path: ./out/{name}.parquet }}
    contract:
      schema:
{body}
""",
        encoding="utf-8",
    )
    return path


def test_number_and_timestamp_columns_are_asked_their_kendall_tau(tmp_path: Path) -> None:
    contract = _contract(
        tmp_path,
        [
            '{ name: amount, type: "DECIMAL(12,2)" }',
            "{ name: region, type: STRING }",
            "{ name: balance, type: INT }",
            "{ name: opened_at, type: TIMESTAMP }",
            "{ name: closed_on, type: DATE }",
        ],
    )
    text = _text(tmp_path, contract)
    assert REQUEST_SCHEMA_URL_1_2_0 in text
    (table,) = load_request_text(text).tables
    assert [(each.definition, each.columns) for each in table.rank_correlations] == [
        ("kendallTau/1", ("amount", "balance", "opened_at", "closed_on"))
    ]
    assert table.clique_columns == ("amount", "region", "balance", "opened_at", "closed_on")


def test_a_key_a_rule_and_an_identifier_are_asked_for_no_correlation_and_no_clique(
    tmp_path: Path,
) -> None:
    path = _contract(
        tmp_path,
        [
            "{ name: order_id, type: INT, required: true }",
            "{ name: ref, type: STRING, tags: [identifier] }",
            '{ name: price, type: "DECIMAL(12,2)" }',
            "{ name: quantity, type: INT }",
            '{ name: total, type: "DECIMAL(12,2)" }',
            "{ name: tier, type: STRING }",
        ],
        "orders",
    )
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    expose = document["exposes"][0]
    expose["contract"]["invariants"] = [
        {"id": "total_is_price_times_quantity", "derive": "total", "expr": "price * quantity"}
    ]
    expose["semantics"] = {"entities": [{"name": "order", "type": "primary", "expr": "order_id"}]}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    (table,) = _request(tmp_path, path).tables
    assert [each.columns for each in table.rank_correlations] == [("price", "quantity")]
    assert table.clique_columns == ("price", "quantity", "tier")


def test_a_table_with_one_candidate_asks_for_nothing_and_keeps_its_format(tmp_path: Path) -> None:
    path = _contract(tmp_path, ['{ name: amount, type: "DECIMAL(12,2)" }'])
    text = _text(tmp_path, path)
    assert REQUEST_SCHEMA_URL in text
    for key in ("rankCorrelations", "cliqueColumns", "groupBy", "linked"):
        assert key not in text
    request = load_request_text(text)
    assert render_request(request) == text


def test_one_number_column_is_asked_no_rank_correlation_but_a_clique_with_a_category(
    tmp_path: Path,
) -> None:
    path = _contract(
        tmp_path, ['{ name: amount, type: "DECIMAL(12,2)" }', "{ name: region, type: STRING }"]
    )
    (table,) = _request(tmp_path, path).tables
    assert table.rank_correlations == ()
    assert table.clique_columns == ("amount", "region")


def test_the_most_columns_a_request_may_name_are_the_first_in_the_contracts_order(
    tmp_path: Path,
) -> None:
    count = MOST_RANK_COLUMNS + 6
    path = _contract(tmp_path, [f"{{ name: n{n:03d}, type: INT }}" for n in range(count)], "wide")
    (table,) = _request(tmp_path, path).tables
    (ranked,) = table.rank_correlations
    assert ranked.columns == tuple(f"n{n:03d}" for n in range(MOST_RANK_COLUMNS))
    assert len(table.clique_columns) == count <= MOST_CLIQUE_CANDIDATES


def test_a_counted_relationship_is_asked_to_group_by_its_parents_drawn_columns(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, BOOK)
    tables = {table.expose_id: table for table in request.tables}
    # The invoice's total, line_count and max_qty are aggregates, its keys are keys.
    assert tables["invoice"].clique_columns == ("status", "issued_on")
    assert tables["account"].clique_columns == ("segment", "status")
    assert tables["account"].rank_correlations == ()
    assert tables["invoice"].rank_correlations == ()
    by_child = {relationship.child.table: relationship for relationship in request.relationships}
    invoices = by_child["invoice"]
    assert invoices.group_by == ("segment", "status")
    assert [(each.column, each.parent_column) for each in invoices.linked] == [
        ("status", "segment"),
        ("status", "status"),
        ("issued_on", "segment"),
        ("issued_on", "status"),
    ]
    lines = by_child["invoice_line"]
    assert lines.group_by == ("status", "issued_on")
    assert [(each.column, each.parent_column) for each in lines.linked] == [
        ("quantity", "status"),
        ("amount", "status"),
    ]


def test_a_relationship_whose_foreign_key_is_not_laid_out_is_asked_nothing(
    tmp_path: Path,
) -> None:
    document: Dict[str, Any] = yaml.safe_load(BOOK.read_text(encoding="utf-8"))
    invoice = document["exposes"][1]
    invoice["contract"]["invariants"].append(
        {"id": "account_is_one", "derive": "account_id", "expr": "1"}
    )
    path = tmp_path / "rule.fluid.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    by_child = {
        relationship.child.table: relationship
        for relationship in _request(tmp_path, path).relationships
    }
    assert by_child["invoice"].group_by == () and by_child["invoice"].linked == ()
    assert by_child["invoice_line"].group_by != ()


def test_the_request_asked_is_the_same_bytes_twice_and_reads_back_equal(tmp_path: Path) -> None:
    first = _text(tmp_path, BOOK)
    (tmp_path / "b.profile-request.yml").unlink()
    assert _text(tmp_path, BOOK) == first
    request = load_request_text(first)
    assert load_request_text(render_request(request)) == request


def test_orders_ask_for_correlations_and_say_the_format(tmp_path: Path) -> None:
    text = _text(tmp_path, ORDERS)
    assert REQUEST_SCHEMA_URL_1_2_0 in text
    (table,) = load_request_text(text).tables
    assert [each.columns for each in table.rank_correlations] == [("amount", "created_at")]
