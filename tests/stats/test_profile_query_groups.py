# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Profile format 1-3-0: a query's rate per group.

A document of format 1-3-0 whose queries hold one grouped statistic must validate against
``docs/spec/profile-1-3.schema.json`` and read with its cells, a withheld cell and a withheld
group keep their status, and a document of format 1-2-0 or earlier that holds one is refused with
``FLX-5036``. ``docs/spec/profile-1-3-example.json`` reads and digests as before, and
``fluxsim init`` writes a bundle from a profile that holds a grouped query.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from flux_engine.core.digest import digest_of_document
from flux_engine.seam.profile import load_profile
from flux_stats.errors import FluxError
from flux_stats.profile import (
    SCHEMA_URL_1_2_0,
    SCHEMA_URL_1_3_0,
    load_profile_text,
    parse_profile,
)

pytestmark = pytest.mark.unit

SPEC = Path(__file__).resolve().parents[2] / "docs" / "spec"
SCHEMA: Dict[str, Any] = json.loads((SPEC / "profile-1-3.schema.json").read_text("utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())

#: The queries of the document below: an ungrouped rate, and one grouped by a column of its rows
#: with a withheld cell (``premium``) and a withheld group (``trial``).
QUERIES: List[Dict[str, Any]] = json.loads("""[
    {
        "name": "late_payment_rate",
        "statistics": [
            {
                "definition": "levelHistogram/1",
                "levels": [
                    "false",
                    "true"
                ],
                "sampleSize": {
                    "min": 1000,
                    "max": 9999
                },
                "disclosure": "acro-default",
                "groups": [
                    {
                        "group": "",
                        "k": 5400,
                        "levelCounts": [
                            4995,
                            405
                        ],
                        "status": "F"
                    }
                ]
            }
        ]
    },
    {
        "name": "late_payment_rate_by_segment",
        "statistics": [
            {
                "definition": "levelHistogram/1",
                "levels": [
                    "false",
                    "true"
                ],
                "sampleSize": {
                    "min": 1000,
                    "max": 9999
                },
                "disclosure": "acro-default",
                "groupBy": "segment",
                "groups": [
                    {
                        "group": "gold",
                        "k": 1500,
                        "levelCounts": [
                            1380,
                            120
                        ],
                        "status": "F"
                    },
                    {
                        "group": "premium",
                        "k": 900,
                        "levelCounts": [
                            null,
                            45
                        ],
                        "cellStatus": [
                            "A",
                            "F"
                        ],
                        "status": "F"
                    },
                    {
                        "group": "standard",
                        "k": 3000,
                        "levelCounts": [
                            2700,
                            300
                        ],
                        "status": "F"
                    },
                    {
                        "group": "trial",
                        "status": "A"
                    }
                ]
            }
        ]
    }
]
    """)


def _document() -> Dict[str, Any]:
    """The 1-3-0 example with its queries replaced by ``QUERIES``."""
    document: Dict[str, Any] = json.loads((SPEC / "profile-1-3-example.json").read_text("utf-8"))
    document["queries"] = copy.deepcopy(QUERIES)
    return document


EXAMPLE: Dict[str, Any] = _document()
TEXT = json.dumps(EXAMPLE)

#: The digest of ``docs/spec/profile-1-3-example.json``, pinned so that the grouped query moves
#: nothing. The example also holds the date pairs, calendars, histories and exposures of format 1-3-0.
EXAMPLE_1_3_0_DIGEST = "sha256:f160c7bf95008a498c4a8762ab32ceda82e50f846316d615932e0704577e0e05"


def _grouped(document: Dict[str, Any]) -> Dict[str, Any]:
    (query,) = [q for q in document["queries"] if q["name"] == "late_payment_rate_by_segment"]
    return query["statistics"][0]


def _without_1_3_0_forms(document: Dict[str, Any]) -> Dict[str, Any]:
    """*document* without the other forms of format 1-3-0, so only a grouped query is left."""
    document.pop("references", None)
    for relationship in document.get("relationships", []):
        for form in ("structural", "dates", "exposure"):
            relationship.pop(form, None)
    for table in document["tables"].values():
        for form in ("datePairs", "calendar", "history"):
            table.pop(form, None)
        for column in table["columns"].values():
            if "statistics" in column:
                column["statistics"] = [
                    s
                    for s in column["statistics"]
                    if not {"groupBy", "groupByLink", "per"} & s.keys()
                ]
    return document


def _refused(document: Dict[str, Any]) -> str:
    with pytest.raises(FluxError) as refused:
        parse_profile(document)
    assert refused.value.code.code == "FLX-5036"
    return str(refused.value)


def test_the_example_validates_and_a_grouped_query_reads_back_with_its_cells() -> None:
    assert not list(VALIDATOR.iter_errors(EXAMPLE))
    assert EXAMPLE["_schemaURL"] == SCHEMA["$id"] == SCHEMA_URL_1_3_0
    profile, document = load_profile_text(TEXT)
    assert document == EXAMPLE
    by_name = {query.name: query for query in profile.queries}
    assert by_name["late_payment_rate"].statistics[0].group_by is None
    grouped = by_name["late_payment_rate_by_segment"].statistics[0]
    assert grouped.group_by == "segment"
    assert grouped.levels == ("false", "true")
    gold = grouped.groups[0]
    assert (gold.key, gold.status, gold.cell_status) == ("gold", "F", ("F", "F"))
    assert gold.statistic is not None and gold.statistic.level_counts == (1380, 120)


def test_withheld_cells_and_groups_keep_their_status() -> None:
    profile, _ = load_profile_text(TEXT)
    grouped = {q.name: q for q in profile.queries}["late_payment_rate_by_segment"].statistics[0]
    by_key = {group.key: group for group in grouped.groups}
    assert [group.key for group in grouped.groups] == ["gold", "premium", "standard", "trial"]
    assert by_key["premium"].cell_status == ("A", "F")
    assert by_key["premium"].suppressed_cells == (0,)
    assert by_key["premium"].statistic is not None
    assert by_key["premium"].statistic.level_counts == (0, 45)
    assert by_key["trial"].status == "A" and not by_key["trial"].published
    assert by_key["trial"].statistic is None


def test_the_engine_loads_the_grouped_query_through_the_seam(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    path.write_text(TEXT, encoding="utf-8")
    pinned = load_profile(path)
    assert pinned.document == EXAMPLE
    assert [query.name for query in pinned.profile.queries] == [
        "late_payment_rate",
        "late_payment_rate_by_segment",
    ]


def test_a_grouped_query_under_an_earlier_format_is_refused() -> None:
    document = copy.deepcopy(EXAMPLE)
    document["_schemaURL"] = SCHEMA_URL_1_2_0
    document = _without_1_3_0_forms(document)
    message = _refused(document)
    assert "queries[1].statistics[0].groupBy" in message and "1-3-0" in message


def test_the_schema_validates_a_grouped_query_and_refuses_a_malformed_one() -> None:
    assert not list(VALIDATOR.iter_errors(EXAMPLE))
    document = copy.deepcopy(EXAMPLE)
    _grouped(document)["groupBy"] = 7
    assert list(VALIDATOR.iter_errors(document))
    document = copy.deepcopy(EXAMPLE)
    _grouped(document)["levels"] = ["no", "yes"]
    assert list(VALIDATOR.iter_errors(document))


def test_a_fact_of_the_invariants_section_is_never_grouped() -> None:
    document = copy.deepcopy(EXAMPLE)
    document["invariants"][0]["statistics"][0]["groupBy"] = "segment"
    assert list(VALIDATOR.iter_errors(document))
    _refused(document)


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s.update(groupBy=""),
        lambda s: s.update(groupBy=7),
        lambda s: s["groups"].reverse(),
        lambda s: s["groups"].append(s["groups"][0]),
        lambda s: s["groups"][0].update(group=""),
        lambda s: s["groups"][0].update(levelCounts=[1380]),
        lambda s: s.update(groupByLink={"column": "x"}),
    ],
)
def test_a_malformed_grouped_query_is_refused(change: Any) -> None:
    document = copy.deepcopy(EXAMPLE)
    change(_grouped(document))
    _refused(document)


def test_a_grouped_query_keeps_one_group_when_it_names_no_column() -> None:
    document = copy.deepcopy(EXAMPLE)
    del _grouped(document)["groupBy"]
    assert "keyed by the empty string" in _refused(document)


def test_a_profile_of_format_1_3_0_reads_and_digests_as_before() -> None:
    path = SPEC / "profile-1-3-example.json"
    document = json.loads(path.read_text("utf-8"))
    assert document["_schemaURL"] == SCHEMA_URL_1_3_0
    profile = parse_profile(document)
    assert all(
        statistic.group_by is None for query in profile.queries for statistic in query.statistics
    )
    assert digest_of_document(document, subject="p").value == EXAMPLE_1_3_0_DIGEST
    assert load_profile(path).digest.value == EXAMPLE_1_3_0_DIGEST


def test_fluxsim_init_writes_a_bundle_from_a_1_3_0_profile_with_a_grouped_query(
    tmp_path: Path,
) -> None:
    from flux_engine.cli._init import run_init
    from tests.author.test_catalogue_init import contract_text, profile_document

    document = profile_document()
    document["queries"] = copy.deepcopy(QUERIES)
    document["disclosureRules"] = {"acro-default": {}, "none": {}}
    assert document["_schemaURL"] == SCHEMA_URL_1_3_0
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(document), encoding="utf-8")
    contract = tmp_path / "catalogue.fluid.yml"
    contract.write_text(contract_text(), encoding="utf-8")
    result = run_init(str(tmp_path / "bundle"), contract=str(contract), profile=str(profile))
    assert result.exit_code == 0, result.stderr
    assert (tmp_path / "bundle" / "simulation.flux.yml").is_file()
