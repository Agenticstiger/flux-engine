# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Register format 1-4-0: the numbers of the namespace keys beside ``emits`` and of the copula, and
the definitions of the rank correlations and the clique marginals.

docs/spec/PARAMETER-REGISTER-SPEC.md sections 2 and 7. A record is format 1-4-0 exactly when it
counts a number at a position the format added, or an entry (or an ``unused`` item) names a
definition it added; every other record keeps the format, bytes and digests it had.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from jsonschema import Draft202012Validator

from flux_engine.evidence.register import (
    REGISTER_SCHEMA_URL,
    REGISTER_SCHEMA_URL_1_3,
    REGISTER_SCHEMA_URL_1_4,
    build_register_document,
    parse_register,
    render_register,
)
from flux_engine.runtime import resolve_cast
from flux_engine.seam.parameters import (
    build_parameter_register,
    read_parameters_block,
    read_unused_block,
)
from flux_engine.seam.resolve import EXTENSION_NAMESPACE, FLX_3105, load_bundle

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
STARTER = REPO_ROOT / "flux_engine" / "starter" / "bundle"
SPEC = REPO_ROOT / "docs" / "spec"
SCHEMA_1_4 = json.loads((SPEC / "parameter-register-1-4.schema.json").read_text(encoding="utf-8"))
SCHEMA_1_3 = json.loads((SPEC / "parameter-register-1-3.schema.json").read_text(encoding="utf-8"))

COPULA = {
    "emits": {
        "e": {
            "fidelity": {
                "copula": {
                    "rankCorrelations": {
                        "pairs": [{"columns": ["a", "b"], "measure": "kendallTau", "value": "0.4"}]
                    }
                }
            }
        }
    }
}


def _register(
    entries: Optional[List[Dict[str, Any]]] = None,
    block: Optional[Dict[str, Any]] = None,
    unused: Optional[List[Dict[str, Any]]] = None,
) -> Any:
    """The starter's register, its Simulation declaring *block* beside its own namespace keys."""
    loaded = load_bundle(STARTER)
    cast = resolve_cast(loaded.flux_documents)
    documents = copy.deepcopy(dict(cast.documents))
    if block:
        namespace = documents[cast.simulation_id].setdefault("extensions", {})
        namespace.setdefault(EXTENSION_NAMESPACE, {}).update(copy.deepcopy(block))
    return build_parameter_register(
        simulation_id=cast.simulation_id,
        documents=documents,
        namespace=EXTENSION_NAMESPACE,
        entries=read_parameters_block(
            entries or [], pinned=True, where="sim", code=FLX_3105, overlay=None
        ),
        profile_digest="sha256:" + "0" * 64,
        code=FLX_3105,
        unused=(
            read_unused_block(unused, pinned=True, where="sim", code=FLX_3105)
            if unused is not None
            else None
        ),
    )


def _profile_entry(definition: str) -> Dict[str, Any]:
    return {
        "document": "q3-retention-world",
        "pointer": "/spec/population/size",
        "source": "profile",
        "statistic": {"pointer": "/tables/t/columns/c/statistics/0", "definition": definition},
    }


@pytest.mark.parametrize(
    "block",
    [
        {"facts": [{"tolerance": Decimal("0.05")}]},
        {"conditions": {"a": [{"values": {"plan": "x"}, "count": 400}]}},
        {"rates": [{"month": [1] * 12}]},
        {"lifetimes": [{"target": Decimal("0.5")}]},
        COPULA,
    ],
    ids=["facts", "conditions", "rates", "lifetimes", "copula"],
)
def test_a_record_that_counts_a_new_position_is_format_1_4_0_and_validates(
    block: Dict[str, Any],
) -> None:
    document = build_register_document(_register(block=block))
    assert document["_schemaURL"] == REGISTER_SCHEMA_URL_1_4
    assert sorted(document["counts"]) == [
        "assumption",
        "calibrated",
        "contract",
        "default",
        "overlay",
        "profile",
    ]
    Draft202012Validator(SCHEMA_1_4).validate(document)
    # The record is not one of format 1-3-0, whose schema names its own URL.
    assert list(Draft202012Validator(SCHEMA_1_3).iter_errors(document))
    summary = parse_register(render_register(_register(block=block)))
    assert summary.declared == document["declared"]


@pytest.mark.parametrize("definition", ["kendallTau/1", "spearmanRho/1", "cliqueMarginal/2"])
def test_a_record_that_cites_a_new_definition_is_format_1_4_0(definition: str) -> None:
    document = build_register_document(_register([_profile_entry(definition)]))
    assert document["_schemaURL"] == REGISTER_SCHEMA_URL_1_4
    Draft202012Validator(SCHEMA_1_4).validate(document)
    assert parse_register(render_register(_register([_profile_entry(definition)]))).declared


def test_a_replacement_or_an_unused_item_that_names_a_new_definition_is_format_1_4_0() -> None:
    assumed = {
        "document": "q3-retention-world",
        "pointer": "/spec/population/size",
        "source": "assumption",
        "reason": "Sized for a quick look.",
        "replacedBy": {"pointer": "/tables/t/columns/c/statistics/0", "definition": "kendallTau/1"},
    }
    assert build_register_document(_register([assumed]))["_schemaURL"] == REGISTER_SCHEMA_URL_1_4
    item = {
        "statistic": {"pointer": "/tables/t/columns/c/statistics/1", "definition": "spearmanRho/1"},
        "reason": "A pair no clique draws.",
    }
    document = build_register_document(_register(unused=[item]))
    assert document["_schemaURL"] == REGISTER_SCHEMA_URL_1_4
    Draft202012Validator(SCHEMA_1_4).validate(document)


@pytest.mark.parametrize("definition", ["cliqueMarginal/1", "mean/1"])
def test_a_definition_an_earlier_format_allowed_keeps_the_format(definition: str) -> None:
    document = build_register_document(_register([_profile_entry(definition)]))
    assert document["_schemaURL"] == REGISTER_SCHEMA_URL


def test_a_register_that_uses_none_of_the_additions_keeps_its_format_and_bytes() -> None:
    document = build_register_document(_register())
    assert document["_schemaURL"] == REGISTER_SCHEMA_URL
    assert sorted(document["counts"]) == ["assumption", "default", "profile"]
    # A format 1-3-0 addition alone stays at 1-3-0 when no new position or definition is used.
    assumed = {
        "document": "q3-retention-world",
        "pointer": "/spec/population/size",
        "source": "assumption",
        "reason": "Sized for a quick look.",
        "replacedBy": {"pointer": "/tables/t/rowCount"},
    }
    assert build_register_document(_register([assumed]))["_schemaURL"] == REGISTER_SCHEMA_URL_1_3


def test_the_schema_of_1_4_0_differs_from_1_3_0_in_its_names_only() -> None:
    changed = copy.deepcopy(SCHEMA_1_4)
    changed["$id"] = SCHEMA_1_3["$id"]
    changed["title"] = SCHEMA_1_3["title"]
    changed["description"] = SCHEMA_1_3["description"]
    changed["properties"]["_schemaURL"] = SCHEMA_1_3["properties"]["_schemaURL"]
    assert changed == SCHEMA_1_3
    assert SCHEMA_1_4["$id"].endswith("/parameter-register/1-4-0/FluxParameterRegister.json")
    assert SCHEMA_1_4["properties"]["_schemaURL"]["const"] == REGISTER_SCHEMA_URL_1_4
