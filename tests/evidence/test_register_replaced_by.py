# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Register format 1-3-0: ``replacedBy`` on an assumption or a default (ADR 0030 section 8).

docs/spec/PARAMETER-REGISTER-SPEC.md section 1.3. Pinned here:

* an ``assumption`` or ``default`` entry may name, in ``replacedBy``, the statistic a profile of
  the contract would publish for the number (a pointer and a definition), and every other source
  and every malformed spelling is refused (``FLX-3105``);
* the key is read for its form and ``plan`` does not read it in the pinned profile;
* a record that uses it names format 1-3-0, validates against the schema, and the evidence note
  names the statistic beside the assumption;
* a register that uses it in no entry stays at the format and the bytes it had.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from jsonschema import Draft202012Validator

from flux_engine.core.errors import FluxError
from flux_engine.evidence.register import (
    REGISTER_SCHEMA_URL,
    REGISTER_SCHEMA_URL_1_3,
    build_register_document,
    parse_register,
    render_register,
    render_register_note,
)
from flux_engine.runtime import resolve_cast
from flux_engine.seam.parameters import build_parameter_register, read_parameters_block
from flux_engine.seam.resolve import EXTENSION_NAMESPACE, FLX_3105, load_bundle

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
STARTER = REPO_ROOT / "flux_engine" / "starter" / "bundle"
SCHEMA_1_3 = json.loads(
    (REPO_ROOT / "docs" / "spec" / "parameter-register-1-3.schema.json").read_text(encoding="utf-8")
)
HISTOGRAM = {
    "pointer": "/tables/orders/columns/amount/statistics/0",
    "definition": "marginalHistogram/1",
}
ROW_COUNT = {"pointer": "/tables/orders/rowCount"}


def _assumption(**more: Any) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "document": "q3-retention-world",
        "pointer": "/spec/population/size",
        "source": "assumption",
        "reason": "Sized for a quick look.",
        "replacedBy": HISTOGRAM,
    }
    entry.update(more)
    return {key: value for key, value in entry.items() if value is not None}


def _register(entries: List[Dict[str, Any]]) -> Any:
    loaded = load_bundle(STARTER)
    cast = resolve_cast(loaded.flux_documents)
    return build_parameter_register(
        simulation_id=cast.simulation_id,
        documents=dict(cast.documents),
        namespace=EXTENSION_NAMESPACE,
        entries=read_parameters_block(
            entries, pinned=False, where="sim", code=FLX_3105, overlay=None
        ),
        profile_digest=None,
        code=FLX_3105,
    )


def test_an_assumption_and_a_default_may_name_what_would_replace_them() -> None:
    (entry,) = read_parameters_block([_assumption()], pinned=False, where="sim", code=FLX_3105)
    assert entry.replaced_by is not None
    assert entry.replaced_by.pointer == HISTOGRAM["pointer"]
    assert entry.replaced_by.definition == "marginalHistogram/1"
    (default,) = read_parameters_block(
        [_assumption(source="default", reason=None, replacedBy=ROW_COUNT)],
        pinned=False,
        where="sim",
        code=FLX_3105,
    )
    assert default.replaced_by is not None and default.replaced_by.definition is None


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (
            {
                "document": "d",
                "pointer": "/a",
                "source": "profile",
                "statistic": ROW_COUNT,
                "replacedBy": HISTOGRAM,
            },
            "only an assumption or a default names what would replace it",
        ),
        (
            {
                "document": "d",
                "pointer": "/a",
                "source": "contract",
                "contract": {"productRef": "p", "pointer": "/x"},
                "replacedBy": HISTOGRAM,
            },
            "only an assumption or a default names what would replace it",
        ),
        (_assumption(replacedBy="/tables/a"), "mapping holding 'pointer'"),
        (_assumption(replacedBy={}), "names the statistic with 'pointer'"),
        (_assumption(replacedBy={"pointer": ""}), "not all of it"),
        (_assumption(replacedBy={"pointer": "tables"}), "JSON Pointer"),
        (_assumption(replacedBy={"pointer": "/a", "definition": "nope/1"}), "registered"),
        (_assumption(replacedBy={"pointer": "/a", "note": "n"}), "declares"),
    ],
)
def test_a_malformed_replacement_is_refused(entry: Dict[str, Any], message: str) -> None:
    with pytest.raises(FluxError) as caught:
        read_parameters_block([entry], pinned=True, where="sim", code=FLX_3105)
    assert caught.value.code is FLX_3105 and message in str(caught.value)


def test_a_record_that_uses_it_is_format_1_3_0_and_validates() -> None:
    document = build_register_document(_register([_assumption()]))
    assert document["_schemaURL"] == REGISTER_SCHEMA_URL_1_3
    assert sorted(document["counts"]) == [
        "assumption",
        "calibrated",
        "contract",
        "default",
        "overlay",
        "profile",
    ]
    assert document["counts"]["overlay"] == 0 and document["counts"]["assumption"] == 1
    assert document["entries"][0]["replacedBy"] == HISTOGRAM
    Draft202012Validator(SCHEMA_1_3).validate(document)


def test_the_schema_refuses_it_on_a_number_that_is_not_assumed() -> None:
    document = build_register_document(_register([_assumption()]))
    broken = copy.deepcopy(document)
    broken["entries"][0].update(source="profile", statistic=ROW_COUNT)
    del broken["entries"][0]["reason"]
    assert list(Draft202012Validator(SCHEMA_1_3).iter_errors(broken))


def test_the_evidence_names_the_statistic_beside_the_assumption() -> None:
    summary = parse_register(render_register(_register([_assumption()])))
    (assumption,) = summary.assumptions
    assert assumption.replaced_by == (HISTOGRAM["pointer"], "marginalHistogram/1")
    note = render_register_note(summary)
    assert (
        "A profile statistic, `/tables/orders/columns/amount/statistics/0` "
        "(`marginalHistogram/1`), would replace it."
    ) in note
    # No overlay is cited, so the report says nothing of one.
    assert "overlay" not in note and "overlay" not in render_register_note(summary).lower()


def test_a_register_that_uses_it_in_no_entry_keeps_its_format_and_bytes() -> None:
    plain = _assumption(replacedBy=None)
    document = build_register_document(_register([plain]))
    assert document["_schemaURL"] == REGISTER_SCHEMA_URL
    assert "replacedBy" not in json.dumps(document)
    assert parse_register(render_register(_register([plain]))).assumptions[0].replaced_by is None


def test_a_record_of_an_earlier_format_cannot_hold_it() -> None:
    document = build_register_document(_register([_assumption(replacedBy=None)]))
    document["entries"][0]["replacedBy"] = HISTOGRAM
    with pytest.raises(ValueError, match="format 1-3-0"):
        parse_register(json.dumps(document))
    document = build_register_document(_register([_assumption()]))
    document["entries"][0]["replacedBy"] = {"pointer": 3}
    with pytest.raises(ValueError, match="replacedBy"):
        parse_register(json.dumps(document))
