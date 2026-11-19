# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Register format 1-1-0: a number the FLUID contract fixes, cited by a pointer into it.

docs/spec/PARAMETER-REGISTER-SPEC.md, the ``contract`` source. Pinned here:

* an entry ``source: contract`` names its place in the contract with
  ``contract: {productRef, pointer}`` and nothing else, and every malformed
  spelling of one is refused (``FLX-3105``);
* the citation is read against the contracts the run emits from, by
  ``validate`` as by every other command, and one that names another contract
  or reaches nothing is refused (``FLX-3105``);
* a record with a contract entry names format 1-1-0, counts four sources and
  validates against the 1-1-0 schema; the reports give the contract's share;
* a register with no contract entry is recorded and reported as format 1-0-0.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List

import pytest
from jsonschema import Draft202012Validator

from flux_engine.core.errors import FluxError
from flux_engine.evidence.register import (
    REGISTER_SCHEMA_URL,
    REGISTER_SCHEMA_URL_1_1,
    build_register_document,
    parse_register,
    render_register,
    render_register_line,
    render_register_note,
)
from flux_engine.seam.parameters import (
    Source,
    check_contract_citations,
    read_parameters_block,
)
from flux_engine.seam.resolve import FLX_3105, load_bundle
from flux_engine.stages.validate import validate

from .test_register import _register

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
STARTER = REPO_ROOT / "flux_engine" / "starter" / "bundle"
SPEC = REPO_ROOT / "docs" / "spec"
SCHEMA_1_1 = json.loads((SPEC / "parameter-register-1-1.schema.json").read_text(encoding="utf-8"))
PRODUCT = "telco.gold.payment_recovery_moment"
CITED: Dict[str, Any] = {
    "document": "q3-retention-world",
    "pointer": "/spec/population/size",
    "source": "contract",
    "contract": {"productRef": PRODUCT, "pointer": "/exposes/0/contract/schema/0/name"},
}
ASSUMED: Dict[str, Any] = {
    "document": "persona-pragmatic-family",
    "pointer": "/spec/traits",
    "source": "assumption",
    "reason": "The growth team's persona workshop.",
}


def _read(entries: List[Dict[str, Any]]) -> Any:
    return read_parameters_block(entries, pinned=False, where="sim", code=FLX_3105)


def _contracts() -> Dict[str, Any]:
    loaded = load_bundle(STARTER)
    return {PRODUCT: loaded.fluid_documents[PRODUCT][1]}


# ---------------------------------------------------------------------------
# Reading an entry
# ---------------------------------------------------------------------------


def test_a_contract_entry_names_the_contract_and_a_pointer_into_it() -> None:
    (entry,) = _read([CITED])
    assert entry.source is Source.CONTRACT
    assert entry.contract is not None
    assert entry.contract.as_document() == CITED["contract"]
    assert entry.contract.tokens == ("exposes", "0", "contract", "schema", "0", "name")
    assert entry.citation is None and entry.reason is None


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda e: e.pop("contract"), "names its place there, 'contract'"),
        (lambda e: e.update(reason="Because."), "carries no 'reason'"),
        (lambda e: e.update(statistic={"pointer": "/tables"}), "only a number from the profile"),
        (lambda e: e.update(contract="the contract"), "a mapping holding 'productRef'"),
        (lambda e: e["contract"].update(column="name"), "declares ['column']"),
        (lambda e: e["contract"].update(productRef=""), "productRef is the id of a contract"),
        (lambda e: e["contract"].pop("pointer"), "names the position in the contract"),
        (lambda e: e["contract"].update(pointer=""), "names a part of the contract"),
        (lambda e: e["contract"].update(pointer="/exposes/~2"), "is not a JSON Pointer"),
    ],
)
def test_a_malformed_contract_entry_is_refused(change: Any, message: str) -> None:
    entry = json.loads(json.dumps(CITED))
    change(entry)
    with pytest.raises(FluxError) as caught:
        _read([entry])
    assert caught.value.code.code == "FLX-3105"
    assert message in str(caught.value)


def test_only_a_contract_entry_carries_a_contract_citation() -> None:
    entry = dict(ASSUMED, contract=CITED["contract"])
    with pytest.raises(FluxError, match="only a number from the contract names a 'contract'"):
        _read([entry])


# ---------------------------------------------------------------------------
# Reading the citation against the contract
# ---------------------------------------------------------------------------


def test_a_citation_that_reaches_a_value_of_an_emitted_contract_holds() -> None:
    check_contract_citations(_register([CITED]), _contracts(), where="sim", code=FLX_3105)


def test_a_citation_of_a_contract_the_run_does_not_emit_from_is_refused() -> None:
    entry = json.loads(json.dumps(CITED))
    entry["contract"]["productRef"] = "telco.gold.other"
    with pytest.raises(FluxError) as caught:
        check_contract_citations(_register([entry]), _contracts(), where="sim", code=FLX_3105)
    assert caught.value.code.code == "FLX-3105"
    assert f"its emits name: {PRODUCT}" in str(caught.value)


def test_a_citation_that_reaches_nothing_says_where_it_stops() -> None:
    entry = json.loads(json.dumps(CITED))
    entry["contract"]["pointer"] = "/exposes/0/contract/schema/9/type"
    with pytest.raises(FluxError) as caught:
        check_contract_citations(_register([entry]), _contracts(), where="sim", code=FLX_3105)
    assert "reaches nothing in contract" in str(caught.value)
    assert "has no item '9'" in str(caught.value)


def test_validate_refuses_a_citation_that_reaches_nothing(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    shutil.copytree(STARTER, bundle)
    simulation = bundle / "simulation.flux.yml"
    simulation.write_text(
        simulation.read_text(encoding="utf-8")
        + "extensions:\n"
        + "  com.agentics.flux/1:\n"
        + "    parameters:\n"
        + "      - document: q3-retention-world\n"
        + "        pointer: /spec/population/size\n"
        + "        source: contract\n"
        + f"        contract: {{productRef: {PRODUCT}, pointer: /exposes/0/nothing}}\n",
        encoding="utf-8",
    )
    result = validate(bundle)
    assert result.exit_code == 1
    assert "FLX-3105" in result.stderr or any(
        "FLX-3105" in str(problem) for problem in result.artifacts.get("problems", [])
    )


# ---------------------------------------------------------------------------
# The record and the reports
# ---------------------------------------------------------------------------


def test_a_record_with_a_contract_entry_is_format_1_1_0() -> None:
    register = _register([CITED, ASSUMED])
    document = build_register_document(register)
    assert document["_schemaURL"] == REGISTER_SCHEMA_URL_1_1
    assert document["counts"] == {"profile": 0, "contract": 1, "assumption": 8, "default": 11}
    assert document["entries"][0]["contract"] == CITED["contract"]
    Draft202012Validator.check_schema(SCHEMA_1_1)
    assert not list(Draft202012Validator(SCHEMA_1_1).iter_errors(document))
    summary = parse_register(render_register(register))
    assert dict(summary.counts) == document["counts"]
    assert render_register_line(summary) == (
        "parameters: 20 declared -- 0 from a profile, 1 from the contract, 8 assumed, 11 default"
    )
    assert "1 (5 %) come from the contract the run emits from" in render_register_note(summary)


def test_a_register_with_no_contract_entry_stays_format_1_0_0() -> None:
    register = _register([ASSUMED])
    document = build_register_document(register)
    assert document["_schemaURL"] == REGISTER_SCHEMA_URL
    assert sorted(document["counts"]) == ["assumption", "default", "profile"]
    line = render_register_line(parse_register(render_register(register)))
    assert "contract" not in line


def test_a_1_1_0_record_is_refused_when_its_counts_leave_the_contract_out() -> None:
    document = build_register_document(_register([CITED]))
    del document["counts"]["contract"]
    with pytest.raises(ValueError, match="sources of its format"):
        parse_register(json.dumps(document))


def test_a_1_0_0_record_cannot_carry_a_contract_entry() -> None:
    document = build_register_document(_register([CITED]))
    document["_schemaURL"] = REGISTER_SCHEMA_URL
    del document["counts"]["contract"]
    with pytest.raises(ValueError):
        parse_register(json.dumps(document))
