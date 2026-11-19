# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Register format 1-2-0: the statistics of the pinned profile no number rests on.

docs/spec/PARAMETER-REGISTER-SPEC.md section 1.1. Pinned here:

* ``extensions["com.agentics.flux/1"].unused`` is a list of citations of the
  pinned profile, each with its reason, and every malformed spelling is refused
  (``FLX-3105``), as is a list beside no register or in a Simulation that pins
  no profile;
* plan reads each citation in the profile and refuses one that reaches nothing
  or a withheld statistic (``FLX-3113``);
* a record that lists them names format 1-2-0, counts four sources, validates
  against the 1-2-0 schema, and the line and the evidence note name them;
* a register that lists none keeps the format and the bytes it had.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from jsonschema import Draft202012Validator

from flux_engine.core.errors import FluxError
from flux_engine.evidence.register import (
    REGISTER_SCHEMA_URL,
    REGISTER_SCHEMA_URL_1_2,
    build_register_document,
    parse_register,
    render_register,
    render_register_line,
    render_register_note,
)
from flux_engine.runtime import resolve_cast
from flux_engine.seam.parameters import (
    FLX_3113,
    build_parameter_register,
    check_profile_citations,
    read_parameters_block,
    read_unused_block,
)
from flux_engine.seam.resolve import (
    EXTENSION_NAMESPACE,
    FLX_3105,
    load_bundle,
    unused_statistics,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
STARTER = REPO_ROOT / "flux_engine" / "starter" / "bundle"
SCHEMA_1_2 = json.loads(
    (REPO_ROOT / "docs" / "spec" / "parameter-register-1-2.schema.json").read_text(encoding="utf-8")
)
DIGEST = "sha256:" + "a" * 64
UNUSED: List[Dict[str, Any]] = [
    {
        "statistic": {
            "pointer": "/tables/orders/columns/tier/statistics/0",
            "definition": "levelHistogram/1",
        },
        "reason": "tier is drawn given segment, whose mutual information is higher",
    },
    {"statistic": {"pointer": "/tables/orders/rowCount"}, "reason": "no rule reads it"},
]
PROFILE: Dict[str, Any] = {
    "tables": {
        "orders": {
            "rowCount": 10,
            "columns": {
                "tier": {
                    "domain": "category",
                    "statistics": [
                        {
                            "definition": "levelHistogram/1",
                            "levels": ["a", "b"],
                            "groupBy": "segment",
                            "groups": [
                                {"group": "x", "k": 10, "levelCounts": [5, 5], "status": "F"}
                            ],
                        },
                        {
                            "definition": "support/1",
                            "groups": [{"group": "", "k": 10, "status": "C"}],
                        },
                    ],
                }
            },
        }
    }
}


def _register(unused: Optional[List[Dict[str, Any]]]) -> Any:
    loaded = load_bundle(STARTER)
    cast = resolve_cast(loaded.flux_documents)
    return build_parameter_register(
        simulation_id=cast.simulation_id,
        documents=dict(cast.documents),
        namespace=EXTENSION_NAMESPACE,
        entries=read_parameters_block([], pinned=True, where="sim", code=FLX_3105),
        profile_digest=DIGEST,
        code=FLX_3105,
        unused=(
            None
            if unused is None
            else read_unused_block(unused, pinned=True, where="sim.unused", code=FLX_3105)
        ),
    )


@pytest.mark.parametrize(
    "block, message",
    [
        ({"statistic": {}}, "is a list"),
        (["x"], "is a mapping"),
        ([{"statistic": {"pointer": "/tables/t/rowCount"}}], "names its 'reason'"),
        ([{"reason": "r"}], "names its 'statistic'"),
        (
            [{"statistic": {"pointer": "/tables/t/rowCount"}, "reason": "r", "note": "n"}],
            "declares",
        ),
        ([{"statistic": {"pointer": "nope"}, "reason": "r"}], "JSON Pointer"),
        ([{"statistic": {"pointer": "/a", "definition": "nope/1"}, "reason": "r"}], "registered"),
        ([{"statistic": {"pointer": "/a"}, "reason": ""}], "non-empty"),
        (
            [
                {"statistic": {"pointer": "/a"}, "reason": "r"},
                {"statistic": {"pointer": "/a"}, "reason": "s"},
            ],
            "the same statistic",
        ),
    ],
)
def test_a_malformed_list_is_refused(block: Any, message: str) -> None:
    with pytest.raises(FluxError) as caught:
        read_unused_block(block, pinned=True, where="sim.unused", code=FLX_3105)
    assert caught.value.code is FLX_3105
    assert message in str(caught.value)


def test_a_list_needs_a_pinned_profile_and_a_register() -> None:
    with pytest.raises(FluxError, match="pins none"):
        read_unused_block(UNUSED, pinned=False, where="sim.unused", code=FLX_3105)
    simulation = {
        "extensions": {EXTENSION_NAMESPACE: {"profile": {"digest": DIGEST}, "unused": UNUSED}}
    }
    with pytest.raises(FluxError, match="declares none"):
        unused_statistics(simulation, where="sim")
    simulation["extensions"][EXTENSION_NAMESPACE]["parameters"] = []
    listed = unused_statistics(simulation, where="sim")
    assert listed is not None and [item.reason for item in listed] == [
        UNUSED[0]["reason"],
        UNUSED[1]["reason"],
    ]


def test_plan_reads_each_citation_in_the_profile() -> None:
    check_profile_citations(_register(UNUSED), PROFILE, where="sim")
    for pointer, definition, message in (
        ("/tables/orders/columns/nope/statistics/0", "levelHistogram/1", "reaches nothing"),
        ("/tables/orders/columns/tier/statistics/0", "support/1", "holds levelHistogram/1"),
        ("/tables/orders/columns/tier/statistics/1", "support/1", "withheld"),
    ):
        block = [{"statistic": {"pointer": pointer, "definition": definition}, "reason": "r"}]
        with pytest.raises(FluxError) as caught:
            check_profile_citations(_register(block), PROFILE, where="sim")
        assert caught.value.code is FLX_3113
        assert message in str(caught.value)


def test_a_record_that_lists_them_is_format_1_2_0() -> None:
    document = build_register_document(_register(UNUSED))
    assert document["_schemaURL"] == REGISTER_SCHEMA_URL_1_2
    assert sorted(document["counts"]) == ["assumption", "contract", "default", "profile"]
    assert document["unused"] == UNUSED
    Draft202012Validator(SCHEMA_1_2).validate(document)
    summary = parse_register(render_register(_register(UNUSED)))
    assert summary.unused is not None and [note.pointer for note in summary.unused] == [
        "/tables/orders/columns/tier/statistics/0",
        "/tables/orders/rowCount",
    ]
    assert summary.as_document()["unused"] == 2
    assert render_register_line(summary).endswith("; 2 statistics of the profile unused")
    note = render_register_note(summary)
    assert "publishes 2 statistics no number of this run rests on" in note
    assert (
        "- `/tables/orders/columns/tier/statistics/0` (`levelHistogram/1`): `tier is drawn" in note
    )


def test_an_empty_list_says_every_statistic_is_used() -> None:
    summary = parse_register(render_register(_register([])))
    assert summary.unused == ()
    assert render_register_line(summary).endswith("; 0 statistics of the profile unused")
    assert "Every statistic of the pinned profile is used" in render_register_note(summary)


def test_a_register_that_lists_none_keeps_its_format_and_bytes() -> None:
    document = build_register_document(_register(None))
    assert document["_schemaURL"] == REGISTER_SCHEMA_URL
    assert "unused" not in document
    assert sorted(document["counts"]) == ["assumption", "default", "profile"]
    summary = parse_register(render_register(_register(None)))
    assert summary.unused is None
    assert "unused" not in summary.as_document()
    assert "unused" not in render_register_line(summary)


def test_a_record_of_an_earlier_format_cannot_list_them() -> None:
    document = build_register_document(_register(None))
    document["unused"] = UNUSED
    with pytest.raises(ValueError, match="only a record of format 1-2-0"):
        parse_register(json.dumps(document))


def test_a_register_that_counts_no_number_still_names_the_unused_statistics() -> None:
    from flux_engine.evidence.register import RegisterSummary, UnusedNote

    summary = RegisterSummary(
        simulation_id="sim",
        profile_digest=DIGEST,
        declared=0,
        counts={"profile": 0, "contract": 0, "assumption": 0, "default": 0},
        assumptions=(),
        stated_defaults=0,
        unused=(UnusedNote(pointer="/tables/orders/rowCount", definition=None, reason="r"),),
    )
    note = render_register_note(summary)
    assert note.startswith("Parameter register: this run reads no number")
    assert "- `/tables/orders/rowCount`: `r`" in note
