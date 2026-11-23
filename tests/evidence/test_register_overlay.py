# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Register format 1-3-0: ``source: overlay`` (ADR 0030 section 8).

docs/spec/PARAMETER-REGISTER-SPEC.md section 1.2. Pinned here:

* an ``overlay`` entry names the Simulation's overlay and cites a statistic of the overlaid
  profile, and every malformed spelling is refused (``FLX-3105``);
* ``plan`` reads a ``profile`` citation in the pinned profile and an ``overlay`` citation in the
  overlaid one, and refuses a ``profile`` citation of a number the patch changed (``FLX-3113``);
* a record that cites an overlay names format 1-3-0, counts five sources, validates against the
  1-3-0 schema, and the line and the evidence note name the overlay as the maintainers' scenario;
* a register that cites none stays at the format it had, with its bytes.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from jsonschema import Draft202012Validator

from flux_engine.core.errors import FluxError
from flux_engine.evidence.register import (
    REGISTER_SCHEMA_URL,
    REGISTER_SCHEMA_URL_1_2,
    REGISTER_SCHEMA_URL_1_3,
    build_register_document,
    parse_register,
    render_register,
    render_register_line,
    render_register_note,
)
from flux_engine.runtime import resolve_cast
from flux_engine.seam.overlay import overlaid_document, read_overlay_block
from flux_engine.seam.parameters import (
    FLX_3113,
    build_parameter_register,
    check_profile_citations,
    read_parameters_block,
    read_unused_block,
)
from flux_engine.seam.resolve import EXTENSION_NAMESPACE, FLX_3105, load_bundle

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
STARTER = REPO_ROOT / "flux_engine" / "starter" / "bundle"
SCHEMA_1_3 = json.loads(
    (REPO_ROOT / "docs" / "spec" / "parameter-register-1-3.schema.json").read_text(encoding="utf-8")
)
DIGEST = "sha256:" + "a" * 64
PROFILE: Dict[str, Any] = json.loads(
    (REPO_ROOT / "tests" / "fixtures" / "author" / "orders.profile.json").read_text("utf-8")
)
OVERLAY = read_overlay_block(
    {
        "name": "double-orders",
        "patch": {
            "tables": {
                "orders": {
                    "rowCount": 1200,
                    "columns": {"amount": {"metrics": {"min": {"value": "20"}}}},
                }
            }
        },
    },
    where="sim.overlay",
    code=FLX_3105,
)
#: The overlaid profile: as the reader needs it, the metric restated whole.
PATCHED = copy.deepcopy(PROFILE)
PATCHED["tables"]["orders"]["rowCount"] = 1200
PATCHED["tables"]["orders"]["columns"]["amount"]["metrics"]["min"]["value"] = "20"
ROW_COUNT = {"pointer": "/tables/orders/rowCount"}
MIN = {"pointer": "/tables/orders/columns/amount/metrics/min", "definition": "min/1"}
MAX = {"pointer": "/tables/orders/columns/amount/metrics/max", "definition": "max/1"}


def _entry(source: str, statistic: Dict[str, str], **more: Any) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "document": "q3-retention-world",
        "pointer": "/spec/population/size",
        "source": source,
        "statistic": statistic,
    }
    entry.update(more)
    return entry


def _other(source: str, statistic: Dict[str, str]) -> Dict[str, Any]:
    """An entry for a different number than :func:`_entry`'s, so two entries never name one."""
    return _entry(source, statistic, document="persona-pragmatic-family", pointer="/spec/traits")


def _register(
    entries: List[Dict[str, Any]],
    *,
    overlay: Optional[str] = "double-orders",
    unused: Optional[List[Dict[str, Any]]] = None,
) -> Any:
    loaded = load_bundle(STARTER)
    cast = resolve_cast(loaded.flux_documents)
    return build_parameter_register(
        simulation_id=cast.simulation_id,
        documents=dict(cast.documents),
        namespace=EXTENSION_NAMESPACE,
        entries=read_parameters_block(
            entries, pinned=True, where="sim", code=FLX_3105, overlay=overlay
        ),
        profile_digest=DIGEST,
        code=FLX_3105,
        unused=(
            None
            if unused is None
            else read_unused_block(unused, pinned=True, where="sim.unused", code=FLX_3105)
        ),
    )


def _overlay_entry(**change: Any) -> Dict[str, Any]:
    entry = _entry("overlay", ROW_COUNT, overlay="double-orders")
    for key, value in change.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    return entry


# ---------------------------------------------------------------------------
# Reading the entry
# ---------------------------------------------------------------------------


def test_an_overlay_entry_names_its_overlay_and_cites_the_overlaid_profile() -> None:
    (entry,) = read_parameters_block(
        [_overlay_entry()], pinned=True, where="sim", code=FLX_3105, overlay="double-orders"
    )
    assert entry.source.value == "overlay" and entry.overlay == "double-orders"
    assert entry.citation is not None and entry.citation.pointer == "/tables/orders/rowCount"


@pytest.mark.parametrize(
    ("entry", "declared", "message"),
    [
        (_overlay_entry(overlay=None), "double-orders", "names its 'overlay'"),
        (_overlay_entry(overlay=""), "double-orders", "names its 'overlay'"),
        (_overlay_entry(overlay=3), "double-orders", "names its 'overlay'"),
        (_overlay_entry(statistic=None), "double-orders", "names its 'statistic'"),
        (_overlay_entry(reason="r"), "double-orders", "carries no 'reason'"),
        (
            _overlay_entry(contract={"productRef": "p", "pointer": "/a"}),
            "double-orders",
            "'contract'",
        ),
        (_overlay_entry(overlay="other"), "double-orders", "declares the overlay 'double-orders'"),
        (_overlay_entry(), None, "the Simulation declares none"),
        (
            _overlay_entry(statistic={"pointer": "/a", "definition": "nope/1"}),
            "double-orders",
            "registered",
        ),
        (
            _entry("profile", ROW_COUNT, overlay="double-orders"),
            "double-orders",
            "only a number from an overlay names an 'overlay'",
        ),
        (
            {
                "document": "q3-retention-world",
                "pointer": "/spec/population/size",
                "source": "assumption",
                "reason": "r",
                "overlay": "double-orders",
            },
            "double-orders",
            "only a number from an overlay names an 'overlay'",
        ),
        (
            {
                "document": "q3-retention-world",
                "pointer": "/spec/population/size",
                "source": "assumption",
                "reason": "r",
                "statistic": ROW_COUNT,
            },
            "double-orders",
            "only a number from the profile or an overlay names a 'statistic'",
        ),
    ],
)
def test_a_malformed_overlay_entry_is_refused(
    entry: Dict[str, Any], declared: Optional[str], message: str
) -> None:
    with pytest.raises(FluxError) as caught:
        read_parameters_block([entry], pinned=True, where="sim", code=FLX_3105, overlay=declared)
    assert caught.value.code is FLX_3105 and message in str(caught.value)


def test_an_overlay_entry_needs_a_pinned_profile() -> None:
    with pytest.raises(FluxError, match="the Simulation pins none"):
        read_parameters_block(
            [_overlay_entry()], pinned=False, where="sim", code=FLX_3105, overlay="double-orders"
        )


# ---------------------------------------------------------------------------
# Plan reads the citations
# ---------------------------------------------------------------------------


def _check(entries: List[Dict[str, Any]], **options: Any) -> None:
    check_profile_citations(
        _register(entries, **options),
        PROFILE,
        where="sim",
        overlay=("double-orders", overlaid_document(PROFILE, OVERLAY, code=FLX_3105)),
    )


def test_an_overlay_citation_is_read_in_the_overlaid_profile() -> None:
    _check([_overlay_entry()])
    _check([_overlay_entry(statistic=MIN)])
    _check([_overlay_entry(statistic=MAX)])  # a number the patch left alone may be cited either way


def test_an_overlay_citation_of_a_statistic_the_overlaid_profile_lacks_is_refused() -> None:
    gone = {"pointer": "/tables/orders/columns/amount/metrics/nope", "definition": "min/1"}
    with pytest.raises(FluxError) as caught:
        _check([_overlay_entry(statistic=gone)])
    assert caught.value.code is FLX_3113 and "reaches nothing in the pinned profile" in str(
        caught.value
    )


def test_a_profile_citation_of_a_number_the_patch_did_not_change_is_read_as_before() -> None:
    _check([_other("profile", MAX), _overlay_entry()])


@pytest.mark.parametrize("statistic", [ROW_COUNT, MIN])
def test_a_profile_citation_of_a_number_the_patch_changed_is_refused(
    statistic: Dict[str, str],
) -> None:
    with pytest.raises(FluxError) as caught:
        _check([_entry("profile", statistic)])
    assert caught.value.code is FLX_3113
    text = str(caught.value)
    assert "changed" in text and "'double-orders'" in text and "source: overlay" in text


def test_a_profile_citation_of_a_metric_the_patch_removed_is_refused() -> None:
    gone = read_overlay_block(
        {
            "name": "double-orders",
            "patch": {"tables": {"orders": {"columns": {"amount": {"metrics": {"max": None}}}}}},
        },
        where="o",
        code=FLX_3105,
    )
    with pytest.raises(FluxError) as caught:
        check_profile_citations(
            _register([_entry("profile", MAX)]),
            PROFILE,
            where="sim",
            overlay=("double-orders", overlaid_document(PROFILE, gone, code=FLX_3105)),
        )
    assert caught.value.code is FLX_3113 and "changed" in str(caught.value)


def test_a_citation_of_a_part_of_a_changed_statistic_is_refused_too() -> None:
    inside = {"pointer": "/tables/orders/columns/amount/metrics/min/value", "definition": "min/1"}
    with pytest.raises(FluxError, match="changed"):
        _check([_entry("profile", inside)])


def test_an_overlay_citation_needs_the_overlaid_profile() -> None:
    with pytest.raises(FluxError, match="no overlaid profile was handed"):
        check_profile_citations(_register([_overlay_entry()]), PROFILE, where="sim")


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def test_a_record_that_cites_an_overlay_is_format_1_3_0() -> None:
    register = _register([_overlay_entry(), _other("profile", MAX)])
    document = build_register_document(register)
    assert document["_schemaURL"] == REGISTER_SCHEMA_URL_1_3
    assert list(document["counts"]) == [
        "profile",
        "contract",
        "overlay",
        "assumption",
        "default",
        "calibrated",
    ]
    assert document["counts"]["overlay"] == 1
    assert document["entries"][0]["overlay"] == "double-orders"
    assert "overlay" not in document["entries"][1]
    assert "unused" not in document
    Draft202012Validator(SCHEMA_1_3).validate(document)


def test_a_record_that_cites_an_overlay_and_lists_unused_statistics_is_valid() -> None:
    unused = [{"statistic": MAX, "reason": "no rule reads it"}]
    document = build_register_document(_register([_overlay_entry()], unused=unused))
    assert document["_schemaURL"] == REGISTER_SCHEMA_URL_1_3 and document["unused"] == unused
    Draft202012Validator(SCHEMA_1_3).validate(document)
    summary = parse_register(render_register(_register([_overlay_entry()], unused=unused)))
    assert summary.unused is not None and summary.overlays == ("double-orders",)


def test_the_schema_refuses_an_overlay_entry_without_its_members() -> None:
    document = build_register_document(_register([_overlay_entry()]))
    for member in ("overlay", "statistic"):
        broken = copy.deepcopy(document)
        del broken["entries"][0][member]
        assert list(Draft202012Validator(SCHEMA_1_3).iter_errors(broken)), member
    broken = copy.deepcopy(document)
    broken["entries"][0]["reason"] = "r"
    assert list(Draft202012Validator(SCHEMA_1_3).iter_errors(broken))


def test_the_text_names_the_overlay_and_says_its_numbers_were_not_measured() -> None:
    entries = [
        {
            "document": "q3-retention-world",
            "pointer": "/spec/population/size",
            "source": "overlay",
            "overlay": "double-orders",
            "statistic": ROW_COUNT,
        }
    ]
    summary = parse_register(render_register(_register(entries)))
    assert summary.overlays == ("double-orders",)
    assert summary.counts["overlay"] == 1
    line = render_register_line(summary)
    assert "1 from the overlay," in line
    note = render_register_note(summary)
    assert (
        "from the overlay `double-orders`, a scenario the maintainers wrote and not a measurement"
        in note
    )


def test_a_register_that_cites_no_overlay_keeps_the_format_and_the_bytes_it_had() -> None:
    plain = [_entry("profile", MAX)]
    document = build_register_document(_register(plain, overlay=None))
    assert document["_schemaURL"] == REGISTER_SCHEMA_URL
    assert sorted(document["counts"]) == ["assumption", "default", "profile"]
    listed = build_register_document(_register(plain, overlay=None, unused=[]))
    assert listed["_schemaURL"] == REGISTER_SCHEMA_URL_1_2
    assert sorted(listed["counts"]) == ["assumption", "contract", "default", "profile"]
    # A Simulation that declares an overlay and cites it in no entry is recorded as it was.
    declared = build_register_document(_register(plain, overlay="double-orders", unused=[]))
    assert declared == listed
    summary = parse_register(render_register(_register(plain, overlay=None)))
    assert summary.overlays == () and "overlay" not in render_register_line(summary)
    assert "overlay" not in render_register_note(summary)


def test_a_record_of_an_earlier_format_cannot_name_an_overlay() -> None:
    document = build_register_document(_register([_overlay_entry()]))
    document["_schemaURL"] = REGISTER_SCHEMA_URL_1_2
    with pytest.raises(ValueError):
        parse_register(json.dumps(document))
