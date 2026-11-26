# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The request ``init`` writes for a private release asks for the noisy history and null counts.

A profile released under differential privacy cannot state a table's history or a column's null
count exactly, so the request written beside its bundle names request format 1-3-0 and asks for
``history`` where the contract declares a validity window and ``nullCount/2`` for each column the
contract allows to be NULL. A release that is not private keeps the format and the bytes it had.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from flux_engine.author.replace import ask_for_private_release
from flux_engine.cli._init import run_init
from flux_engine.seam.profile_request import column_domains
from flux_stats.profile import SCHEMA_URL_1_5_0
from flux_stats.request import (
    NULL_COUNT,
    REQUEST_SCHEMA_URL,
    REQUEST_SCHEMA_URL_1_3_0,
    ProfileRequest,
    RequestColumn,
    RequestHistory,
    RequestTable,
    load_request_text,
    render_request,
)
from tests.overlays import ORDERS_CONTRACT, ORDERS_PROFILE

pytestmark = pytest.mark.integration

REQUEST = "requests/assumptions.profile-request.yml"
VERSIONED = (
    Path(__file__).resolve().parents[1] / "fixtures" / "versioned" / "subscriptions.fluid.yml"
)


def _private_profile(root: Path) -> Path:
    document = json.loads(ORDERS_PROFILE.read_text(encoding="utf-8"))
    document["_schemaURL"] = SCHEMA_URL_1_5_0
    document["privacy"] = {
        "measure": "zeroConcentratedDP",
        "privacyLoss": {"rho": "1"},
        "privacyUnit": {"expose": "orders"},
    }
    path = root / "private.profile.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _asked(root: Path, profile: Path, name: str) -> ProfileRequest:
    result = run_init(str(root / name), contract=str(ORDERS_CONTRACT), profile=str(profile))
    assert result.exit_code == 0, result.stderr
    return load_request_text((root / name / REQUEST).read_text(encoding="utf-8"))


def test_a_request_for_a_release_that_is_not_private_keeps_its_format_and_bytes(
    tmp_path: Path,
) -> None:
    run_init(str(tmp_path / "bundle"), contract=str(ORDERS_CONTRACT), profile=str(ORDERS_PROFILE))
    text = (tmp_path / "bundle" / REQUEST).read_text(encoding="utf-8")
    assert REQUEST_SCHEMA_URL in text
    assert "nullCount/2" not in text and "history" not in text


def test_a_private_release_asks_for_a_null_count_of_each_nullable_column(tmp_path: Path) -> None:
    public = _asked(tmp_path, ORDERS_PROFILE, "public")
    private = _asked(tmp_path, _private_profile(tmp_path), "private")
    text = (tmp_path / "private" / REQUEST).read_text(encoding="utf-8")
    assert REQUEST_SCHEMA_URL_1_3_0 in text
    contract = yaml.safe_load(ORDERS_CONTRACT.read_text(encoding="utf-8"))
    schema = {
        column["name"]: column.get("required", False)
        for column in contract["exposes"][0]["contract"]["schema"]
    }
    for table in private.tables:
        for column in table.columns:
            assert (NULL_COUNT in column.statistics) == (not schema[column.name]), column.name
    # what the public request asks for keeps its place, so no replacedBy pointer moves
    for before in public.tables[0].columns:
        after = next(c for c in private.tables[0].columns if c.name == before.name)
        assert after.statistics[: len(before.statistics)] == before.statistics
        assert after.metrics == before.metrics


def _versioned() -> Dict[str, Any]:
    return yaml.safe_load(VERSIONED.read_text(encoding="utf-8"))


def test_a_table_with_a_validity_window_asks_for_its_history() -> None:
    contract = _versioned()
    domains = column_domains(contract)
    request = ProfileRequest(
        contract_digest="sha256:" + "0" * 64,
        fractional_bits=16,
        tables=(
            RequestTable("customer", False, (RequestColumn("region", "category", (), ()),)),
            RequestTable(
                "subscription_version",
                False,
                (RequestColumn("plan", "category", (), ("levelHistogram/1",)),),
            ),
        ),
    )
    asked = ask_for_private_release(request, contract, domains)
    by_name = {table.expose_id: table for table in asked.tables}
    assert by_name["customer"].history is None
    assert by_name["subscription_version"].history == RequestHistory()
    ended = {column.name: column for column in by_name["subscription_version"].columns}
    assert ended["valid_to"].statistics == (NULL_COUNT,)  # the one column that may be NULL
    assert ended["plan"].statistics == ("levelHistogram/1",)
    assert REQUEST_SCHEMA_URL_1_3_0 in render_request(asked)
    assert load_request_text(render_request(asked)) == asked
