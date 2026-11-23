# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Register format 1-3-0: calibrated values and response estimates.

docs/spec/PARAMETER-REGISTER-SPEC.md section 8. Pinned here:

* an entry with ``source: calibrated`` carries a ``calibration`` member, which the
  record carries through, and every malformed spelling is refused (``FLX-3105``),
  among them an ``after`` that is not the value the document holds at the pointer;
* the response estimates a runtime adds are entries of kind ``responseControl`` and
  ``responseEffect``, listed after the declared ones, and no counted number;
* a record with either addition names format 1-3-0 and validates against its schema;
* a register with neither keeps the bytes it had.
"""

from __future__ import annotations

import copy
import hashlib
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
    render_register_line,
    render_register_note,
)
from flux_engine.runtime import resolve_cast
from flux_engine.seam.parameters import (
    RESPONSE_CONTROL,
    RESPONSE_EFFECT,
    ParameterRegister,
    ResponseEstimate,
    ResponseInterval,
    add_response_estimates,
    build_parameter_register,
    read_parameters_block,
)
from flux_engine.seam.resolve import EXTENSION_NAMESPACE, FLX_3105, load_bundle

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
STARTER = REPO_ROOT / "flux_engine" / "starter" / "bundle"
SCHEMA = json.loads(
    (REPO_ROOT / "docs" / "spec" / "parameter-register-1-3.schema.json").read_text(encoding="utf-8")
)
DIGEST = "sha256:" + "b" * 64
CALIBRATED: Dict[str, Any] = {
    "document": "q3-retention-world",
    "pointer": "/spec/population/size",
    "source": "calibrated",
    "calibration": {
        "queries": ["churn-rate", "upgrade-rate"],
        "before": "400",
        "after": "500",
        "patch": "sha256:" + "1" * 64,
        "targets": "sha256:" + "2" * 64,
        "heldOut": {"profile": "sha256:" + "3" * 64, "largestGap": "0.011", "tolerance": "0.02"},
        "versions": {"cma": "4.0.0", "sqlglot": "26.0.0", "duckdb": "1.2.0"},
    },
}
ASSUMED: Dict[str, Any] = {
    "document": "persona-pragmatic-family",
    "pointer": "/spec/traits",
    "source": "assumption",
    "reason": "The growth team's persona workshop.",
}
#: The sha256 of the text a register of the entries of tests/evidence/test_register.py
#: renders to, computed with the code before format 1-3-0.
PINNED_BYTES = "f69657aacbc86ae36314b45b4eea402f7eb60d686acc103fca70ec0f064cf46a"
EARLIER_ENTRIES: List[Dict[str, Any]] = [
    {
        "document": "q3-retention-world",
        "pointer": "/spec/population/size",
        "source": "assumption",
        "reason": "Sized for a quick look.",
    },
    {
        "document": "persona-pragmatic-family",
        "pointer": "/spec/traits",
        "source": "assumption",
        "reason": "The growth team's persona workshop.",
    },
    {
        "document": "persona-pragmatic-family",
        "pointer": "/spec/traits/monthlySpend/min",
        "source": "default",
    },
]
CONTROL = ResponseEstimate(
    kind=RESPONSE_CONTROL,
    response=0,
    segment="gold",
    value="0.25",
    cells=("queries/renewals/gold",),
)
EFFECT = ResponseEstimate(
    kind=RESPONSE_EFFECT,
    response=0,
    segment="gold",
    value="0.04",
    cells=("queries/renewals/gold", "queries/renewals-treated/gold"),
    interval=ResponseInterval(low="0.01", high="0.07", confidence="0.95"),
)


def _register(entries: List[Dict[str, Any]]) -> ParameterRegister:
    loaded = load_bundle(STARTER)
    cast = resolve_cast(loaded.flux_documents)
    return build_parameter_register(
        simulation_id=cast.simulation_id,
        documents=dict(cast.documents),
        namespace=EXTENSION_NAMESPACE,
        entries=read_parameters_block(entries, pinned=False, where="sim", code=FLX_3105),
        profile_digest=None,
        code=FLX_3105,
    )


def _calibrated(change: Any = None) -> Dict[str, Any]:
    entry = copy.deepcopy(CALIBRATED)
    if change is not None:
        change(entry)
    return entry


def _errors(document: Any) -> List[Any]:
    return list(Draft202012Validator(SCHEMA).iter_errors(document))


# ---------------------------------------------------------------------------
# A register without the additions is unchanged
# ---------------------------------------------------------------------------


def test_a_register_without_the_additions_keeps_its_exact_bytes_and_format() -> None:
    register = _register(EARLIER_ENTRIES)
    text = render_register(register)
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == PINNED_BYTES
    assert json.loads(text)["_schemaURL"] == REGISTER_SCHEMA_URL
    assert sorted(json.loads(text)["counts"]) == ["assumption", "default", "profile"]


# ---------------------------------------------------------------------------
# Addition 1: calibrated values
# ---------------------------------------------------------------------------


def test_a_calibrated_value_is_written_with_its_calibration_and_read_back() -> None:
    register = _register([CALIBRATED, ASSUMED])
    document = build_register_document(register)
    assert document["_schemaURL"] == REGISTER_SCHEMA_URL_1_3
    assert sorted(document["counts"]) == [
        "assumption",
        "calibrated",
        "contract",
        "default",
        "overlay",
        "profile",
    ]
    assert document["counts"]["calibrated"] == 1
    assert sum(document["counts"].values()) == document["declared"]
    assert document["entries"][0]["calibration"] == CALIBRATED["calibration"]
    assert [row["source"] for row in document["parameters"]].count("calibrated") == 1
    assert _errors(document) == []
    summary = parse_register(render_register(register))
    assert summary.counts["calibrated"] == 1
    assert "1 calibrated, " in render_register_line(summary)
    assert "1 (5 %) are calibrated values" in render_register_note(summary)


@pytest.mark.parametrize(
    ("gap", "tolerance"),
    [
        ("0.03", "0.02"),
        ("0.1", "0.02"),  # larger as a number, smaller as text
        ("0.0201", "0.02"),
        ("1", "0.5"),
        ("2", "0"),
    ],
)
def test_a_calibrated_value_whose_held_out_gap_exceeds_its_tolerance_is_refused(
    gap: str, tolerance: str
) -> None:
    entry = _calibrated(
        lambda e: e["calibration"]["heldOut"].update(largestGap=gap, tolerance=tolerance)
    )
    with pytest.raises(FluxError) as caught:
        _register([entry])
    assert caught.value.code.code == "FLX-3716"
    assert "calibration.heldOut.largestGap is larger than" in str(caught.value)
    assert "sim" in str(caught.value)


@pytest.mark.parametrize(
    ("gap", "tolerance"),
    [("0.02", "0.02"), ("0.011", "0.02"), ("0", "0"), ("0.0200", "0.02"), ("0.02", "0.1")],
)
def test_a_gap_within_its_tolerance_or_equal_to_it_is_read(gap: str, tolerance: str) -> None:
    entry = _calibrated(
        lambda e: e["calibration"]["heldOut"].update(largestGap=gap, tolerance=tolerance)
    )
    assert build_register_document(_register([entry]))["counts"]["calibrated"] == 1


#: The sha256 of the note of the run of ``EARLIER_ENTRIES``: a run with no calibrated value keeps its
#: note's bytes when the calibrated sentence is reworded. Recomputed when the note's first sentence
#: came to say what the register counts, and not when the calibrated sentence changes.
PINNED_NOTE = "ba2a3e72ce3530396ccf03094076d7b3d3f8aa65041a11bf7b29aff7fecf76d3"


def test_the_note_of_a_run_with_no_calibrated_value_keeps_its_bytes() -> None:
    note = render_register_note(parse_register(render_register(_register(EARLIER_ENTRIES))))
    assert hashlib.sha256(note.encode("utf-8")).hexdigest() == PINNED_NOTE
    assert "calibrated" not in note


def test_a_format_1_3_0_register_with_no_calibrated_value_keeps_the_words_it_had() -> None:
    register = add_response_estimates(_register(EARLIER_ENTRIES), [CONTROL], code=FLX_3105)
    note = render_register_note(parse_register(render_register(register)))
    assert "0 (0 %) are calibrated values, fitted to targets of the pinned profile, " in note
    assert "declares" not in note and "re-run" not in note


def test_the_note_says_the_bundle_declares_a_calibrated_value_and_the_engine_did_not_refit() -> (
    None
):
    summary = parse_register(render_register(_register([CALIBRATED, ASSUMED])))
    note = render_register_note(summary)
    assert "1 (5 %) are calibrated values, which the bundle declares were fitted" in note
    assert "with the digests it names (the engine did not re-run the fit)" in note


def test_the_example_values_validate_against_the_schema_and_a_broken_one_does_not() -> None:
    Draft202012Validator.check_schema(SCHEMA)
    document = build_register_document(_register([CALIBRATED]))
    assert _errors(document) == []
    del document["entries"][0]["calibration"]
    assert _errors(document)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda e: e.pop("calibration"), "names its 'calibration'"),
        (lambda e: e.update(reason="Fitted."), "carries no 'reason'"),
        (lambda e: e["calibration"].pop("patch"), "names its ['patch']"),
        (lambda e: e["calibration"].update(extra="x"), "declares ['extra']"),
        (lambda e: e["calibration"].update(queries=[]), "a list of distinct query ids"),
        (lambda e: e["calibration"].update(queries=["a", "a"]), "a list of distinct query ids"),
        (lambda e: e["calibration"].update(before="1e3"), "an exact decimal text"),
        (lambda e: e["calibration"].update(before=400), "an exact decimal text"),
        (lambda e: e["calibration"].update(before="-0"), "an exact decimal text"),
        (lambda e: e["calibration"].update(after="0500"), "an exact decimal text"),
        (lambda e: e["calibration"].update(patch="sha256:abc"), "a sha256 digest"),
        (lambda e: e["calibration"].update(targets="md5:" + "1" * 32), "a sha256 digest"),
        (lambda e: e["calibration"]["heldOut"].update(profile="x"), "heldOut.profile"),
        (lambda e: e["calibration"]["heldOut"].update(largestGap=".1"), "heldOut.largestGap"),
        (lambda e: e["calibration"]["heldOut"].pop("tolerance"), "names its ['tolerance']"),
        (lambda e: e["calibration"]["versions"].update(cma=""), "versions.cma"),
        (lambda e: e["calibration"].update(versions={"cma": "1"}), "names its"),
        (lambda e: e.update(statistic={"pointer": "/tables"}), "only a number from the profile"),
    ],
)
def test_a_malformed_calibrated_entry_is_refused(change: Any, message: str) -> None:
    with pytest.raises(FluxError) as caught:
        _register([_calibrated(change)])
    assert caught.value.code.code == "FLX-3105"
    assert message in str(caught.value)


@pytest.mark.parametrize("source", ["assumption", "default", "profile", "contract"])
def test_a_calibration_under_another_source_is_refused(source: str) -> None:
    entry = dict(ASSUMED, source=source, calibration=CALIBRATED["calibration"])
    with pytest.raises(FluxError) as caught:
        _register([entry])
    assert caught.value.code.code == "FLX-3105"
    assert "only a calibrated number names a 'calibration'" in str(caught.value)


def test_an_after_that_is_not_the_value_the_document_holds_is_refused() -> None:
    entry = _calibrated(lambda e: e["calibration"].update(after="501"))
    with pytest.raises(FluxError) as caught:
        _register([entry])
    assert caught.value.code.code == "FLX-3105"
    assert "is not the value 'q3-retention-world' holds at the pointer" in str(caught.value)


def test_a_calibrated_value_is_one_number() -> None:
    entry = _calibrated(lambda e: e.update(pointer="/spec/population"))
    with pytest.raises(FluxError, match="names one number"):
        _register([entry])


def test_a_register_of_another_format_does_not_read_a_calibration() -> None:
    document = build_register_document(_register([CALIBRATED]))
    document["_schemaURL"] = REGISTER_SCHEMA_URL
    document["counts"].pop("contract")
    document["counts"].pop("overlay")
    document["counts"].pop("calibrated")
    document["counts"]["default"] += 1
    for row in document["parameters"]:
        if row["source"] == "calibrated":
            row["source"] = "default"
            row["entry"] = None
    document["entries"][0]["source"] = "default"
    with pytest.raises(ValueError, match="only a calibrated entry has a calibration"):
        parse_register(json.dumps(document))


# ---------------------------------------------------------------------------
# Addition 2: response estimates
# ---------------------------------------------------------------------------


def test_response_estimates_are_entries_after_the_declared_ones_and_no_counted_number() -> None:
    base = _register([ASSUMED])
    register = add_response_estimates(base, [CONTROL, EFFECT], code=FLX_3105)
    document = build_register_document(register)
    assert document["_schemaURL"] == REGISTER_SCHEMA_URL_1_3
    assert document["declared"] == len(base.numbers) == len(document["parameters"])
    assert [entry["index"] for entry in document["entries"]] == [0, 1, 2]
    control, effect = document["entries"][1:]
    simulation = base.simulation_id
    assert control == {
        "index": 1,
        "kind": "responseControl",
        "document": simulation,
        "pointer": "/extensions/com.agentics.flux~11/responses/0/segments/gold",
        "value": "0.25",
        "source": "profile",
        "cells": ["queries/renewals/gold"],
    }
    assert effect["kind"] == "responseEffect"
    assert effect["value"] == "0.04"
    assert effect["cells"] == ["queries/renewals/gold", "queries/renewals-treated/gold"]
    assert effect["interval"] == {"low": "0.01", "high": "0.07", "confidence": "0.95"}
    assert _errors(document) == []
    summary = parse_register(render_register(register))
    assert summary.declared == document["declared"]
    assert summary.counts["profile"] == 0


def test_a_segment_key_is_escaped_in_the_pointer() -> None:
    estimate = ResponseEstimate(
        kind=RESPONSE_CONTROL,
        response=2,
        segment="a/b~c",
        value="0",
        cells=("queries/q/k",),
    )
    register = add_response_estimates(_register([]), [estimate], code=FLX_3105)
    pointer = build_register_document(register)["entries"][0]["pointer"]
    assert pointer == "/extensions/com.agentics.flux~11/responses/2/segments/a~1b~0c"


def test_a_register_with_an_estimate_only_is_format_1_3_0() -> None:
    register = add_response_estimates(_register([]), [CONTROL], code=FLX_3105)
    document = build_register_document(register)
    assert document["_schemaURL"] == REGISTER_SCHEMA_URL_1_3
    assert document["counts"]["calibrated"] == 0
    assert _errors(document) == []


@pytest.mark.parametrize(
    ("estimate", "message"),
    [
        (ResponseEstimate("rate", 0, "gold", "0.1", ("queries/a/b",)), "kind is"),
        (ResponseEstimate(RESPONSE_CONTROL, -1, "gold", "0.1", ("queries/a/b",)), "response is"),
        (ResponseEstimate(RESPONSE_CONTROL, 0, "", "0.1", ("queries/a/b",)), "segment is"),
        (ResponseEstimate(RESPONSE_CONTROL, 0, "gold", "1e-1", ("queries/a/b",)), "exact decimal"),
        (ResponseEstimate(RESPONSE_CONTROL, 0, "gold", "0.1", ()), "names 1 distinct"),
        (
            ResponseEstimate(RESPONSE_CONTROL, 0, "gold", "0.1", ("queries/a/b", "queries/a/c")),
            "names 1 distinct",
        ),
        (ResponseEstimate(RESPONSE_CONTROL, 0, "gold", "0.1", ("a/b",)), "queries/<query id>"),
        (
            ResponseEstimate(
                RESPONSE_CONTROL,
                0,
                "gold",
                "0.1",
                ("queries/a/b",),
                ResponseInterval("0", "1", "0.9"),
            ),
            "no interval",
        ),
        (
            ResponseEstimate(RESPONSE_EFFECT, 0, "gold", "0.1", ("queries/a/b", "queries/a/c")),
            "names its interval",
        ),
        (
            ResponseEstimate(RESPONSE_EFFECT, 0, "gold", "0.1", ("queries/a/b",)),
            "names 2 distinct",
        ),
        (
            ResponseEstimate(
                RESPONSE_EFFECT,
                0,
                "gold",
                "0.1",
                ("queries/a/b", "queries/a/b"),
                ResponseInterval("0", "1", "0.9"),
            ),
            "names 2 distinct",
        ),
        (
            ResponseEstimate(
                RESPONSE_EFFECT,
                0,
                "gold",
                "0.1",
                ("queries/a/b", "queries/a/c"),
                ResponseInterval("0.5", "0.1", "0.9"),
            ),
            "low at most high",
        ),
        (
            ResponseEstimate(
                RESPONSE_EFFECT,
                0,
                "gold",
                "0.1",
                ("queries/a/b", "queries/a/c"),
                ResponseInterval("0", "1", "1"),
            ),
            "confidence in (0, 1)",
        ),
    ],
)
def test_a_malformed_response_estimate_is_refused(estimate: ResponseEstimate, message: str) -> None:
    with pytest.raises(FluxError) as caught:
        add_response_estimates(_register([]), [estimate], code=FLX_3105)
    assert caught.value.code.code == "FLX-3105"
    assert message in str(caught.value)


def test_an_estimate_repeating_a_response_segment_and_kind_is_refused() -> None:
    register = add_response_estimates(_register([]), [CONTROL], code=FLX_3105)
    with pytest.raises(FluxError, match="repeats a response, a segment and a kind"):
        add_response_estimates(register, [CONTROL], code=FLX_3105)
    with pytest.raises(FluxError, match="repeats a response, a segment and a kind"):
        add_response_estimates(_register([]), [CONTROL, CONTROL], code=FLX_3105)


def test_a_record_of_an_earlier_format_does_not_read_an_estimate() -> None:
    register = add_response_estimates(_register([]), [CONTROL], code=FLX_3105)
    document = build_register_document(register)
    document["_schemaURL"] = REGISTER_SCHEMA_URL
    document["counts"].pop("contract")
    document["counts"].pop("overlay")
    document["counts"].pop("calibrated")
    with pytest.raises(ValueError, match="only a record of format 1-3-0"):
        parse_register(json.dumps(document))
