# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A whole run whose accounts and invoices carry a ``sharedAttribute`` their customers own.

The bundle of ``test_shared_columns_run`` with its ``sharedColumns`` replaced by two attributes
(ADR 0022 section 4): ``region`` is carried by the customer, the account and the invoice, so the
customer owns it and the account and the invoice copy it along their one path; ``score`` is carried by
the account and the invoice, so the account owns it. The tests run the bundle through the pipeline
every command uses, and then through ``plan``, ``apply`` and ``verify``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.stages import pipeline
from flux_engine.stages.validate import validate

from ._jobs import plan_apply_verify
from .test_shared_columns_run import _CONTRACT_TEXT, INVOICES, _bundle

_ATTRIBUTES = re.sub(r"\n *sharedColumns: \[[^\]]*\]", "", _CONTRACT_TEXT)
for _name in ("region", "a_score"):
    _ATTRIBUTES = re.sub(
        rf"(\{{ name: {_name}, type: \w+, required: false) \}}",
        r"\1, sharedAttribute: " + ("region" if _name == "region" else "score") + " }",
        _ATTRIBUTES,
    )


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _bundle(tmp_path_factory.mktemp("attribute-run"), contract=_ATTRIBUTES)


@pytest.fixture(scope="module")
def result(bundle: Path) -> pipeline.PipelineResult:
    return pipeline.execute(bundle, kernel=KERNEL_V2)


def _tables(outcome: pipeline.PipelineResult) -> Dict[str, List[Dict[str, Any]]]:
    return {
        e.resolved.expose_id: [
            dict(zip(list(e.expose.column_names), row, strict=True)) for row in e.rows
        ]
        for e in outcome.emits
    }


def test_the_contract_declares_the_attributes() -> None:
    assert _ATTRIBUTES.count("sharedAttribute: region") == 3
    assert _ATTRIBUTES.count("sharedAttribute: score") == 2
    assert "sharedColumns" not in _ATTRIBUTES


@pytest.mark.integration
def test_every_member_holds_the_owners_value_on_every_row_whose_path_reaches_it(
    result: pipeline.PipelineResult,
) -> None:
    tables = _tables(result)
    customers = {row["cu_id"]: row for row in tables["customer"]}
    accounts = {row["a_id"]: row for row in tables["account"]}
    reached = 0
    for row in tables["account"]:
        if row["a_customer"] is None:
            assert row["region"] is None
        else:
            assert row["region"] == customers[row["a_customer"]]["region"] is not None
    for row in tables["invoice"]:
        account = accounts.get(row["i_account"])
        assert row["a_score"] == (None if account is None else account["a_score"])
        customer = None if account is None else customers.get(account["a_customer"])
        assert row["region"] == (None if customer is None else customer["region"])
        reached += customer is not None
    assert reached > 0.7 * INVOICES


@pytest.mark.integration
def test_plan_lists_nothing_and_names_each_member_by_its_attribute(
    result: pipeline.PipelineResult,
) -> None:
    assert not any(emit.not_generated for emit in result.emits)
    invoice = next(e for e in result.emits if e.resolved.expose_id == "invoice")
    coverage = {
        c.column: (c.origin.value, c.decided_by, c.source) for c in invoice.coverage.columns
    }
    assert coverage["region"] == ("copied", "rule", "contract.sharedAttribute.region")
    assert coverage["a_score"] == ("copied", "rule", "contract.sharedAttribute.score")
    assert invoice.plan is not None and sorted(invoice.plan.as_document()["reads"]) == [
        "account",
        "customer",
    ]


@pytest.mark.integration
def test_kernel_0_1_0_lists_every_attribute_and_copies_nothing(tmp_path: Path) -> None:
    old = pipeline.execute(_bundle(tmp_path, contract=_ATTRIBUTES, copies=False), kernel=KERNEL_V1)
    listed = {e.resolved.expose_id: [row.kind for row in e.not_generated] for e in old.emits}
    assert listed["customer"] == ["shared-attribute"]
    assert listed["account"] == ["shared-attribute", "shared-attribute"]
    assert listed["invoice"] == ["shared-attribute", "shared-attribute"]
    assert all(emit.plan is not None and emit.plan.rules is None for emit in old.emits)


@pytest.mark.integration
def test_validate_accepts_the_bundle_as_plan_reads_it(bundle: Path) -> None:
    assert validate(bundle).exit_code == 0


@pytest.mark.slow
@pytest.mark.integration
def test_plan_apply_and_verify_score_each_copy_for_any_number_of_jobs(
    bundle: Path, tmp_path: Path
) -> None:
    digests, rules = plan_apply_verify(bundle, tmp_path)
    assert digests[0] == digests[1]
    assert rules[0] == rules[1]
    by_rule = {entry["rule"]: entry for entry in rules[0]["invoice"]}
    assert {"region", "score"} <= set(by_rule)
    for rule_id in ("region", "score"):
        entry = by_rule[rule_id]
        assert entry["origin"] == "shared" and entry["enforced"] is True
        assert entry["satisfied"] == entry["total"] and entry["total"] > 0, rule_id
