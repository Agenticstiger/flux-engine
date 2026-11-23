# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Profile format 1-2-0: the ``invariants`` section, read beside 1-0-0 and 1-1-0.

``docs/spec/profile-1-2-example.json`` holds the section, one fact published and one
withheld. It must read, validate against ``docs/spec/profile-1-2.schema.json``, and each
broken copy must be refused with ``FLX-5036`` naming where. A document of an earlier format
reads exactly as it did, and names no fact.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable, Dict

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from flux_stats.errors import FluxError
from flux_stats.profile import (
    SCHEMA_URL,
    SCHEMA_URL_1_1_0,
    SCHEMA_URL_1_2_0,
    load_profile_text,
    parse_profile,
)

pytestmark = pytest.mark.unit


def _spec() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "spec" / "PROFILE-SPEC.md").is_file():
            return parent / "docs" / "spec"
    raise RuntimeError("these tests read docs/spec from the flux-engine checkout around them")


SPEC = _spec()
EXAMPLE: Dict[str, Any] = json.loads((SPEC / "profile-1-2-example.json").read_text("utf-8"))
OLD_EXAMPLE: Dict[str, Any] = json.loads((SPEC / "profile-example.json").read_text("utf-8"))
SCHEMA: Dict[str, Any] = json.loads((SPEC / "profile-1-2.schema.json").read_text("utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


def _refused(document: Dict[str, Any]) -> str:
    with pytest.raises(FluxError) as refused:
        parse_profile(document)
    assert refused.value.code.code == "FLX-5036"
    return str(refused.value)


def _edit(change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    document = copy.deepcopy(EXAMPLE)
    change(document)
    return document


def test_the_example_reads_its_facts() -> None:
    profile = parse_profile(copy.deepcopy(EXAMPLE))
    assert [entry.name for entry in profile.invariants] == [
        "payment.paid_after_due",
        "subscriber.tenure_within_term",
    ]
    published, withheld = profile.invariants
    assert published.counts() == (7960, 8000)
    assert withheld.counts() is None
    assert profile.invariant("payment.paid_after_due") is published
    assert profile.invariant("nothing") is None


def test_the_example_and_the_schema_agree() -> None:
    Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["$id"] == SCHEMA_URL_1_2_0 == EXAMPLE["_schemaURL"]
    assert not list(VALIDATOR.iter_errors(EXAMPLE))


def test_the_schema_of_1_2_0_validates_the_earlier_formats_too() -> None:
    assert not list(VALIDATOR.iter_errors(OLD_EXAMPLE))
    assert OLD_EXAMPLE["_schemaURL"] == SCHEMA_URL


def test_a_document_of_an_earlier_format_names_no_fact() -> None:
    profile = parse_profile(copy.deepcopy(OLD_EXAMPLE))
    assert profile.invariants == () and profile.invariant("a") is None


@pytest.mark.parametrize("url", [SCHEMA_URL, SCHEMA_URL_1_1_0])
def test_an_earlier_format_that_holds_the_section_is_refused(url: str) -> None:
    document = _edit(lambda d: d.update(_schemaURL=url))
    message = _refused(document)
    assert "profile.invariants" in message and "1-2-0" in message


def test_a_1_2_0_document_may_hold_no_section() -> None:
    document = copy.deepcopy(OLD_EXAMPLE)
    document["_schemaURL"] = SCHEMA_URL_1_2_0
    assert parse_profile(document).invariants == ()


def test_an_unknown_url_is_still_refused() -> None:
    document = _edit(lambda d: d.update(_schemaURL=SCHEMA_URL_1_2_0.replace("1-2-0", "1-4-0")))
    assert "profile._schemaURL" in _refused(document)


@pytest.mark.parametrize(
    ("change", "where"),
    [
        (lambda d: d["invariants"][0].update(extra=1), "profile.invariants[0]"),
        (lambda d: d["invariants"][0].pop("name"), "profile.invariants[0]"),
        (lambda d: d["invariants"][0].update(name=""), "profile.invariants[0].name"),
        (lambda d: d["invariants"][1].update(name="payment.paid_after_due"), "profile.invariants"),
        (lambda d: d["invariants"][0].update(statistics=[]), "profile.invariants[0].statistics"),
        (
            lambda d: d["invariants"][0].update(statistics=d["invariants"][0]["statistics"] * 2),
            "profile.invariants[0].statistics",
        ),
        (
            lambda d: d["invariants"][0]["statistics"][0].update(levels=["no", "yes"]),
            "profile.invariants[0].statistics",
        ),
        (
            lambda d: d["invariants"][0]["statistics"][0].update(definition="mean/1"),
            "profile.invariants[0].statistics[0].definition",
        ),
        (lambda d: d.update(invariants={}), "profile.invariants"),
    ],
)
def test_a_broken_section_is_refused_where_it_breaks(
    change: Callable[[Dict[str, Any]], None], where: str
) -> None:
    assert where in _refused(_edit(change))


def test_a_float_count_in_the_section_is_refused() -> None:
    text = json.dumps(EXAMPLE).replace("7960", "7960.0")
    with pytest.raises(FluxError) as refused:
        load_profile_text(text)
    assert refused.value.code.code == "FLX-5036"


def test_a_withheld_cell_leaves_no_share() -> None:
    document = _edit(
        lambda d: d["invariants"][0]["statistics"][0]["groups"][0].update(
            levelCounts=[None, 7960], cellStatus=["A", "F"]
        )
    )
    assert parse_profile(document).invariants[0].counts() is None


def test_a_group_of_no_rows_leaves_no_share() -> None:
    document = _edit(
        lambda d: d["invariants"][0]["statistics"][0]["groups"][0].update(k=0, levelCounts=[0, 0])
    )
    assert parse_profile(document).invariants[0].counts() is None
