# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A consumer query over a hidden table is refused at validate and at plan (FLX-3624).

A hidden table is generated and read through links but never written, so no stream carries it and
``verify`` could only report the query as an engine error. The query schema holds the exposes the
run writes, so the table is unknown to the check and the refusal names it as hidden.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("sqlglot")

from flux_engine.stages._consumer_check import schema_of_emits  # noqa: E402
from flux_engine.stages.plan import plan  # noqa: E402
from flux_engine.stages.validate import validate  # noqa: E402

from .test_hidden_tables_run import hidden_bundle  # noqa: E402

pytestmark = pytest.mark.integration

_CONSUMERS = (
    "    consumers:\n"
    "      dialect: duckdb\n"
    "      asOf: '2026-09-30'\n"
    "{tables}"
    "      queries:\n"
    "      - id: {query}\n"
    "        sql: {sql}\n"
    "        unit: {unit}\n"
)


def _with_query(tmp_path: Path, *, sql: str, unit: str, mapped: bool) -> Path:
    bundle = hidden_bundle(tmp_path)
    path = bundle / "simulation.flux.yml"
    tables = (
        "      tables:\n        customer: customer\n        account: account\n" if mapped else ""
    )
    path.write_text(
        path.read_text("utf-8")
        + _CONSUMERS.format(tables=tables, query="q-hidden", sql=sql, unit=unit),
        "utf-8",
    )
    return bundle


@pytest.mark.parametrize("mapped", [False, True])
def test_a_query_over_a_hidden_table_is_refused_at_validate_and_plan(
    tmp_path: Path, mapped: bool
) -> None:
    bundle = _with_query(
        tmp_path, sql="SELECT * FROM customer WHERE cu_age < 30", unit="customer", mapped=mapped
    )
    checked = validate(bundle)
    assert checked.exit_code == 1 and checked.error_code == "FLX-3624", checked.stderr
    assert "customer is a hidden table" in checked.stderr
    planned = plan(bundle, state_dir=tmp_path / "state")
    assert planned.exit_code == 1 and planned.error_code == "FLX-3624", planned.stderr
    assert "customer is a hidden table" in planned.stderr
    assert not (tmp_path / "state" / "runs").exists()


def test_a_query_over_a_written_table_of_the_same_bundle_is_accepted(tmp_path: Path) -> None:
    bundle = _with_query(
        tmp_path, sql="SELECT * FROM account WHERE a_score > 50", unit="account", mapped=False
    )
    assert validate(bundle).exit_code == 0
    assert plan(bundle, state_dir=tmp_path / "state").exit_code == 0


def test_the_query_schema_leaves_out_a_hidden_table(tmp_path: Path) -> None:
    from flux_engine.seam.resolve import load_bundle, resolve_all_emits

    bundle = hidden_bundle(tmp_path)
    loaded = load_bundle(bundle)
    simulation_id = next(iter(loaded.simulation_ids()))
    resolved = resolve_all_emits(loaded, simulation_id=simulation_id)
    assert {emit.expose_id for emit in resolved} == {"customer", "account"}
    assert [item.expose_id for item in schema_of_emits(resolved).exposes] == ["account"]
