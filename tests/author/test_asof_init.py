# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` writes an ``asOf`` entry for a relationship into a history's emit (ADR 0017 section 13).

For each relationship into an expose it wrote a ``history`` for, from an expose that declares a
``defaultAggTimeDimension``, ``init`` writes an ``asOf`` entry on that dimension, and the plan's
pre-flight accepts it. An expose with no default time dimension writes none.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import pytest
import yaml

from flux_engine.seam.asof import ASOF_KEY, asof_entries_of
from flux_engine.seam.resolve import load_bundle, resolve_all_emits
from flux_engine.stages.validate import validate

from .test_history_init import CONTRACT, _init, _metrics, _ms, _namespace, _profile

pytestmark = pytest.mark.integration

INVOICE = """
  - exposeId: invoice
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/invoice.parquet }
    contract:
      schema:
        - { name: invoice_id, type: INT, required: true }
        - { name: plan_id, type: INT, required: true }
        - { name: invoiced_on, type: DATE, required: true }
    semantics:
      DEFAULT_TIME
      entities:
        - { name: invoice, type: primary, expr: invoice_id }
      dimensions:
        - { name: invoiced_on, type: time }
      relationships:
        - name: plan
          to: plan_version
          fromColumns: [plan_id]
          toColumns: [plan_id]
          cardinality: many_to_one
"""
ENTRY = {"emit": "invoice", "relationship": "plan", "date": "invoiced_on"}


def contract_with_invoice(*, dated: bool = True) -> str:
    """The history contract with an ``invoice`` expose that reads ``plan_version``."""
    default = "defaultAggTimeDimension: invoiced_on" if dated else "entities_note: none"
    block = INVOICE.replace("      DEFAULT_TIME\n", f"      {default}\n" if dated else "")
    return CONTRACT.read_text(encoding="utf-8") + block


def profile_with_invoice() -> Dict[str, Any]:
    found = _profile()
    found["tables"]["invoice"] = {
        "rowCount": 5000,
        "columns": {
            "invoice_id": {"domain": "number", "metrics": _metrics("1", "5000", 5000)},
            "plan_id": {"domain": "number", "metrics": _metrics("1", "1000", 5000)},
            "invoiced_on": {
                "domain": "timestamp",
                "metrics": _metrics(_ms(2025, 3, 1), _ms(2025, 9, 20), 5000),
            },
        },
    }
    return found


def written(tmp_path: Path, profile: Dict[str, Any], contract: Optional[str] = None) -> Any:
    return _init(tmp_path, profile, contract if contract is not None else contract_with_invoice())


def test_a_relationship_into_a_history_from_an_expose_with_a_default_time_dimension(
    tmp_path: Path,
) -> None:
    written(tmp_path, profile_with_invoice())
    assert _namespace(tmp_path)[ASOF_KEY] == [ENTRY]


def test_the_written_bundle_resolves_and_the_plan_accepts_the_entry(tmp_path: Path) -> None:
    written(tmp_path, profile_with_invoice())
    bundle = tmp_path / "bundle"
    assert validate(bundle).exit_code == 0
    loaded = load_bundle(bundle)
    (simulation_id,) = loaded.simulation_ids()
    emits = {e.expose_id: e for e in resolve_all_emits(loaded, simulation_id=simulation_id)}
    (entry,) = asof_entries_of(emits["invoice"].extension, "invoice")
    assert (entry.relationship, entry.date) == ("plan", "invoiced_on")
    assert asof_entries_of(emits["plan_version"].extension, "plan_version") == ()
    # The history stays: the entry reads it.
    assert "history" in _namespace(tmp_path)


def test_an_expose_with_no_default_time_dimension_writes_none(tmp_path: Path) -> None:
    written(tmp_path, profile_with_invoice(), contract_with_invoice(dated=False))
    assert ASOF_KEY not in _namespace(tmp_path)
    assert "history" in _namespace(tmp_path)


def test_a_bundle_without_such_a_relationship_is_what_init_wrote_before(tmp_path: Path) -> None:
    _init(tmp_path, _profile())
    assert ASOF_KEY not in _namespace(tmp_path)
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    assert "asOf" not in text and yaml.safe_load(text)
