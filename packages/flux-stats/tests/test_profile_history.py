# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Profile format 1-3-0: the ``history`` of a table (PROFILE-SPEC.md section 5).

``docs/spec/profile-1-3-example.json`` holds one on the table ``plan_version``. It must validate
against the schema and read, each broken copy must be refused with ``FLX-5036`` naming where, a
document of an earlier format that holds it is refused, and one that does not hold it reads as it
did.
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
from flux_stats.profile import CHAIN_LEVELS, SCHEMA_URL_1_2_0, parse_profile

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
WHERE = "profile.tables.plan_version.history"


def _edit(change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    document = copy.deepcopy(EXAMPLE)
    change(document)
    return document


def _history(change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    return _edit(lambda document: change(document["tables"]["plan_version"]["history"]))


def _refused(document: Dict[str, Any]) -> str:
    with pytest.raises(FluxError) as refused:
        parse_profile(document)
    assert refused.value.code.code == "FLX-5036"
    return str(refused.value)


def test_the_example_validates_and_reads_its_history() -> None:
    assert not list(VALIDATOR.iter_errors(EXAMPLE))
    history = parse_profile(copy.deepcopy(EXAMPLE)).tables["plan_version"].history
    assert history is not None
    assert history.entity == ("plan_id",)
    assert (history.start, history.end) == ("valid_from", "valid_to")
    assert history.chain.levels == CHAIN_LEVELS
    assert history.last_version.levels == ("closed", "open")
    assert history.versions_per_entity.identifier == "marginalHistogram/1"
    tier = history.columns["tier"]
    assert tier.transitions.group_by == "tier"
    assert [group.key for group in tier.transitions.groups] == ["basic", "plus", "pro"]
    assert history.reasons is not None
    assert history.reasons.levels == ("several", "tier")


def test_a_table_without_a_history_reads_none() -> None:
    profile = parse_profile(copy.deepcopy(EXAMPLE))
    assert profile.tables["payment"].history is None


def test_a_history_without_columns_and_reasons_reads() -> None:
    document = _history(lambda found: (found.pop("columns"), found.pop("reasons")))
    assert not list(VALIDATOR.iter_errors(document))
    history = parse_profile(document).tables["plan_version"].history
    assert history is not None and not history.columns and history.reasons is None


def _drop(key: str) -> Callable[[Dict[str, Any]], None]:
    return lambda found: found.pop(key)


def _set(key: str, value: Any) -> Callable[[Dict[str, Any]], None]:
    return lambda found: found.__setitem__(key, value)


def _chain_levels(levels: Any) -> Callable[[Dict[str, Any]], None]:
    return lambda found: found["chain"].__setitem__("levels", levels)


@pytest.mark.parametrize(
    "change, where, schema_refuses",
    [
        (_drop("chain"), "", True),
        (_set("surprise", 1), "", True),
        (_set("entity", []), ".entity", True),
        (_set("entity", ["plan_id", "plan_id"]), ".entity", True),
        (_set("entity", ["nowhere"]), ".entity", False),
        (_set("start", "tier"), ".start", False),
        (_set("end", "valid_from"), "", False),
        (_set("start", "plan_id"), ".start", False),
        (_chain_levels(["gap", "nextStart"]), ".chain", True),
        (_chain_levels(["dayBefore", "gap", "nextStart", "overlaps"]), ".chain.levels", True),
        (
            lambda found: found["lastVersion"].__setitem__("levels", ["closed", "ended"]),
            ".lastVersion.levels",
            True,
        ),
        (
            lambda found: found["firstStart"].__setitem__("definition", "levelHistogram/1"),
            ".firstStart",
            True,
        ),
        (
            lambda found: found["chain"].__setitem__("groupBy", "tier"),
            ".chain",
            False,
        ),
        (
            lambda found: found["chain"].__setitem__("per", ["plan_id"]),
            ".chain",
            False,
        ),
        (
            lambda found: found["columns"].__setitem__("plan_id", found["columns"]["tier"]),
            ".columns.plan_id",
            False,
        ),
        (
            lambda found: found["columns"].__setitem__("valid_to", found["columns"]["tier"]),
            ".columns.valid_to",
            False,
        ),
        (
            lambda found: found["columns"]["tier"]["transitions"].pop("groupBy"),
            ".columns.tier.transitions",
            True,
        ),
        (
            lambda found: found["columns"]["tier"]["initial"].__setitem__("groupBy", "tier"),
            ".columns.tier.initial",
            False,
        ),
        (
            lambda found: found["columns"]["tier"].pop("initial"),
            ".columns.tier",
            True,
        ),
        (
            lambda found: found["reasons"].__setitem__("levels", ["other", "several"]),
            ".reasons.levels",
            False,
        ),
        (
            lambda found: (found.pop("columns"), None)[1],
            ".reasons",
            False,
        ),
    ],
)
def test_a_broken_history_is_refused(
    change: Callable[[Dict[str, Any]], None], where: str, schema_refuses: bool
) -> None:
    document = _history(change)
    if schema_refuses:
        assert list(VALIDATOR.iter_errors(document))
    assert WHERE + where in _refused(document)


def test_an_earlier_format_that_holds_it_is_refused() -> None:
    document = _edit(lambda found: found.__setitem__("_schemaURL", SCHEMA_URL_1_2_0))
    document.pop("references", None)
    document["relationships"][0].pop("structural", None)
    assert "format 1-3-0" in _refused(document)


def test_a_document_without_it_reads_and_keeps_its_digest() -> None:
    document = _edit(lambda found: found["tables"].pop("plan_version"))
    before = json.dumps(canonicalise(document), sort_keys=True)
    assert parse_profile(document).tables.get("plan_version") is None
    assert json.dumps(canonicalise(document), sort_keys=True) == before
    assert "history" not in before


def test_the_example_round_trips() -> None:
    text = json.dumps(EXAMPLE, indent=2, ensure_ascii=False) + "\n"
    assert text == (SPEC / "profile-1-3-example.json").read_text("utf-8")
    assert json.loads(json.dumps(canonicalise(EXAMPLE))) == json.loads(
        json.dumps(canonicalise(json.loads(text)))
    )
