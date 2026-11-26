# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A published schema keeps its file: the profile format 1-0-0 stays beside 1-1-0, as published."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SPEC = Path(__file__).resolve().parents[2] / "docs" / "spec"

#: The sha256 of the profile schema of format 1-0-0 as release 0.6.2 published it. A profile written
#: before 0.7.0 names its ``$id``, so the file is kept and never edited.
PROFILE_1_0_0_SHA256 = "fde1dfedd35a20556643ca9102c6886de3982b8761e8730d70d46fdc7c360674"


def test_the_profile_1_0_0_schema_is_kept_as_published() -> None:
    raw = (SPEC / "profile-1-0.schema.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PROFILE_1_0_0_SHA256
    assert json.loads(raw)["$id"].endswith("/profile/1-0-0/FluxProfile.json")


#: The sha256 of the scorecard schema of formats 1-0-0 and 1-1-0 as release 0.7.0 published it. A
#: scorecard written before the coverage names one of its ``$id`` values, so the file is kept.
SCORECARD_1_1_0_SHA256 = "0100d3e931088d46634c99e84a725e67c7c31f7d43dcfe226fc53bbf4f944881"


def test_the_scorecard_1_1_0_schema_is_kept_beside_1_2_0() -> None:
    raw = (SPEC / "scorecard-1.schema.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SCORECARD_1_1_0_SHA256
    assert json.loads(raw)["$id"].endswith("/scorecard/1-1-0/FluxScorecard.json")
    current = json.loads((SPEC / "scorecard-1-2.schema.json").read_text(encoding="utf-8"))
    assert current["$id"].endswith("/scorecard/1-2-0/FluxScorecard.json")
    assert current["properties"]["_schemaURL"]["const"] == current["$id"]


#: The sha256 of the profile schema of format 1-1-0 as release 0.8.0 published it. Format 1-2-0 has a
#: schema file of its own, so a profile that names 1-1-0 keeps the schema it names.
PROFILE_1_1_0_SHA256 = "61d8f860a36b3e3cf878355fe5872c1145537e301e0e7bf4c89bec5924a8863a"


def test_the_profile_1_1_0_schema_is_kept_beside_1_2_0() -> None:
    raw = (SPEC / "profile-1.schema.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PROFILE_1_1_0_SHA256
    assert json.loads(raw)["$id"].endswith("/profile/1-1-0/FluxProfile.json")
    current = json.loads((SPEC / "profile-1-2.schema.json").read_text(encoding="utf-8"))
    assert current["$id"].endswith("/profile/1-2-0/FluxProfile.json")
    assert current["properties"]["_schemaURL"]["enum"][-1] == current["$id"]


#: The sha256 of the scorecard schema of format 1-2-0 as release 0.8.0 published it. Format 1-3-0 has a
#: schema file of its own, so a scorecard that names 1-2-0 keeps the schema it names.
SCORECARD_1_2_0_SHA256 = "6159bbccd0ec15410e6b5c2cfa193c0b6837e82294416c3e7975ab281b37cce4"


def test_the_scorecard_1_2_0_schema_is_kept_beside_1_3_0() -> None:
    raw = (SPEC / "scorecard-1-2.schema.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SCORECARD_1_2_0_SHA256
    current = json.loads((SPEC / "scorecard-1-3.schema.json").read_text(encoding="utf-8"))
    assert current["$id"].endswith("/scorecard/1-3-0/FluxScorecard.json")
    assert current["properties"]["_schemaURL"]["const"] == current["$id"]


def test_the_1_3_0_example_validates_against_its_schema_and_the_1_2_0_example_against_its() -> None:
    from jsonschema import Draft202012Validator

    schema = json.loads((SPEC / "scorecard-1-3.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    example = json.loads((SPEC / "scorecard-1-3-example.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(example)
    assert example["_schemaURL"] == schema["$id"]
    kinds = {row["property"] for table in example["tables"] for row in table["properties"]}
    assert {"fact", "referentialIntegrity"} <= kinds
    previous = json.loads((SPEC / "scorecard-1-2.schema.json").read_text(encoding="utf-8"))
    old = json.loads((SPEC / "scorecard-example.json").read_text(encoding="utf-8"))
    Draft202012Validator(previous).validate(old)
    assert list(
        Draft202012Validator(previous).iter_errors(example)
    ), "a 1-3-0 scorecard names its own $id and holds rows 1-2-0 does not define"


def test_the_1_4_0_schema_adds_the_children_per_parent_and_calendar_rows_to_1_3_0() -> None:
    from jsonschema import Draft202012Validator

    current = json.loads((SPEC / "scorecard-1-4.schema.json").read_text(encoding="utf-8"))
    previous = json.loads((SPEC / "scorecard-1-3.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(current)
    assert current["$id"].endswith("/scorecard/1-4-0/FluxScorecard.json")
    assert current["properties"]["_schemaURL"]["const"] == current["$id"]
    assert previous["$id"].endswith("/scorecard/1-3-0/FluxScorecard.json")
    # The queries dimension's own block of definitions (SCORECARD-SPEC.md section 3a) sits beside them.
    queries_block = {
        "Queries",
        "QueriesCounted",
        "QueriesNotCounted",
        "QueriesVersions",
        "QueryGroup",
        "QueryRequirement",
        "QueryRow",
    }
    assert current["$defs"]["Calendar"]["properties"]["property"]["const"] == "calendar"
    assert current["$defs"]["Calendar"]["properties"]["dimension"]["const"] == "temporal"
    assert (
        set(current["$defs"]) - set(previous["$defs"])
        == {
            "Calendar",
            "ChildrenPerParent",
            "ParentsByBin",
        }
        | queries_block
    )
    assert current["$defs"]["ChildrenPerParent"]["properties"]["property"]["const"] == (
        "childrenPerParent"
    )


def test_the_1_5_0_schema_adds_the_rank_correlation_and_grouped_rows_to_1_4_0() -> None:
    from jsonschema import Draft202012Validator

    current = json.loads((SPEC / "scorecard-1-5.schema.json").read_text(encoding="utf-8"))
    previous = json.loads((SPEC / "scorecard-1-4.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(current)
    assert current["$id"].endswith("/scorecard/1-5-0/FluxScorecard.json")
    assert current["properties"]["_schemaURL"]["const"] == current["$id"]
    assert set(current["$defs"]) - set(previous["$defs"]) == {
        "ChildrenPerParentGrouped",
        "ChildrenPerParentGroup",
        "GroupByBins",
        "NoiseWidening",
        "RankComparison",
    }
    assert "rankCorrelation" in current["$defs"]["Property"]["properties"]["property"]["enum"]
    example = json.loads((SPEC / "scorecard-1-5-example.json").read_text(encoding="utf-8"))
    Draft202012Validator(current).validate(example)
    assert example["_schemaURL"] == current["$id"]
    kinds = {row["property"] for table in example["tables"] for row in table["properties"]}
    assert {"rankCorrelation", "childrenPerParent"} <= kinds
    assert list(
        Draft202012Validator(previous).iter_errors(example)
    ), "a 1-5-0 scorecard names its own $id and holds rows 1-4-0 does not define"


#: The sha256 of the profile schema of format 1-2-0 as release 0.9.0 published it. Format 1-3-0 has a
#: schema file of its own, so a profile that names 1-2-0 keeps the schema it names.
PROFILE_1_2_0_SHA256 = "9fed77263e2708f6f969fa08e7844d0c9c1406a7d1a66478482141a0ce7cebd7"


def test_the_profile_1_2_0_schema_is_kept_beside_1_3_0() -> None:
    raw = (SPEC / "profile-1-2.schema.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PROFILE_1_2_0_SHA256
    current = json.loads((SPEC / "profile-1-3.schema.json").read_text(encoding="utf-8"))
    assert current["$id"].endswith("/profile/1-3-0/FluxProfile.json")
    assert current["properties"]["_schemaURL"]["enum"][-1] == current["$id"]


def test_the_profile_1_3_0_example_validates_against_its_schema_and_not_the_1_2_0_one() -> None:
    from jsonschema import Draft202012Validator

    schema = json.loads((SPEC / "profile-1-3.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    example = json.loads((SPEC / "profile-1-3-example.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(example)
    assert example["_schemaURL"] == schema["$id"]
    for name in ("profile-example.json", "profile-1-2-example.json"):
        old = json.loads((SPEC / name).read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(old)
    previous = json.loads((SPEC / "profile-1-2.schema.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(previous).iter_errors(example))


def test_the_profile_1_5_0_schema_and_example_validate_and_1_4_0_documents_still_do() -> None:
    from jsonschema import Draft202012Validator

    schema = json.loads((SPEC / "profile-1-5.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert schema["$id"].endswith("/profile/1-5-0/FluxProfile.json")
    assert schema["properties"]["_schemaURL"]["enum"][-1] == schema["$id"]
    example = json.loads((SPEC / "profile-1-5-history-example.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(example)
    assert example["_schemaURL"] == schema["$id"]
    for name in (
        "profile-example.json",
        "profile-1-2-example.json",
        "profile-1-2-private-example.json",
        "profile-1-3-example.json",
        "profile-1-4-example.json",
        "profile-1-4-cliques-example.json",
        "profile-1-4-follows-example.json",
    ):
        old = json.loads((SPEC / name).read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(old)
    previous = json.loads((SPEC / "profile-1-4.schema.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(previous).iter_errors(example))


def test_a_null_count_is_noisy_ungrouped_and_has_two_cells() -> None:
    import copy

    from jsonschema import Draft202012Validator

    schema = json.loads((SPEC / "profile-1-5.schema.json").read_text(encoding="utf-8"))
    example = json.loads((SPEC / "profile-1-5-history-example.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    def stat(doc: dict) -> dict:
        return doc["tables"]["member"]["columns"]["segment"]["statistics"][0]

    assert stat(example)["definition"] == "nullCount/2"
    without_noise = copy.deepcopy(example)
    del stat(without_noise)["noise"]
    assert list(validator.iter_errors(without_noise))
    grouped = copy.deepcopy(example)
    stat(grouped)["groupBy"] = "id"
    assert list(validator.iter_errors(grouped))
    three_cells = copy.deepcopy(example)
    stat(three_cells)["groups"][0]["noisyCounts"].append("1")
    assert list(validator.iter_errors(three_cells))
