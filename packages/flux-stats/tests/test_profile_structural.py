# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Profile format 1-3-0: the ``structural`` key of a relationship (PROFILE-SPEC.md section 5).

``docs/spec/profile-1-3-example.json`` holds it on one relationship. It must validate against the
schema and read, each broken copy must be refused with ``FLX-5036`` naming where, a document of an
earlier format that holds it is refused, and one that does not hold it reads as it did.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable, Dict

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from flux_stats.canonical import canonicalise
from flux_stats.errors import FluxError
from flux_stats.profile import SCHEMA_URL_1_2_0, parse_profile

pytestmark = pytest.mark.unit


def _spec() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "spec" / "PROFILE-SPEC.md").is_file():
            return parent / "docs" / "spec"
    raise RuntimeError("these tests read docs/spec from the flux-engine checkout around them")


SPEC = _spec()
EXAMPLE: Dict[str, Any] = json.loads((SPEC / "profile-1-3-example.json").read_text("utf-8"))
SCHEMA: Dict[str, Any] = json.loads((SPEC / "profile-1-3.schema.json").read_text("utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


def _edit(change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    document = copy.deepcopy(EXAMPLE)
    change(document)
    return document


def _set(value: Any) -> Dict[str, Any]:
    return _edit(lambda document: document["relationships"][0].__setitem__("structural", value))


def _refused(document: Dict[str, Any]) -> str:
    with pytest.raises(FluxError) as refused:
        parse_profile(document)
    assert refused.value.code.code == "FLX-5036"
    return str(refused.value)


def test_the_example_validates_and_reads_its_structural_classes() -> None:
    assert not list(VALIDATOR.iter_errors(EXAMPLE))
    profile = parse_profile(copy.deepcopy(EXAMPLE))
    assert profile.relationships[0].structural == frozenset({"orphan"})


def test_both_classes_are_accepted() -> None:
    document = _set(["childless", "orphan"])
    assert not list(VALIDATOR.iter_errors(document))
    assert parse_profile(document).relationships[0].structural == {"childless", "orphan"}


@pytest.mark.parametrize(
    "value, where",
    [
        ([], "structural"),
        (["orphans"], "structural[0]"),
        (["orphan", "orphan"], "structural"),
        (["childless", 3], "structural[1]"),
        ("orphan", "structural"),
    ],
)
def test_a_broken_structural_is_refused_by_the_reader_and_the_schema(
    value: Any, where: str
) -> None:
    document = _set(value)
    assert list(VALIDATOR.iter_errors(document))
    assert f"profile.relationships[0].{where}" in _refused(document)


def test_an_earlier_format_that_holds_it_is_refused() -> None:
    document = _edit(lambda found: found.__setitem__("_schemaURL", SCHEMA_URL_1_2_0))
    document.pop("references", None)
    assert "format 1-3-0" in _refused(document)


def test_a_document_without_it_reads_empty_and_keeps_its_digest() -> None:
    document = _edit(lambda found: found["relationships"][0].pop("structural"))
    before = json.dumps(canonicalise(document), sort_keys=True)
    profile = parse_profile(document)
    assert profile.relationships[0].structural == frozenset()
    assert json.dumps(canonicalise(document), sort_keys=True) == before
    assert "structural" not in before
