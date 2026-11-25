# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The privacy grade of a run is read from the profile it pins, and nothing else (ADR 0020)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from flux_engine.core.grades import ORDER, Pattern, at_least, weakest
from flux_engine.evidence.grades import GeneratorGrade, ProfileGrade, RunGrade, grade_profile
from flux_engine.evidence.report import render_privacy_section

pytestmark = pytest.mark.unit

SPEC = Path(__file__).resolve().parents[2] / "docs" / "spec"
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "private-telco" / "telco.profile.json"


def _private() -> Dict[str, Any]:
    document: Dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return document


def _graded(change: Callable[[Dict[str, Any]], None]) -> ProfileGrade:
    document = _private()
    change(document)
    return grade_profile(document, pinned=True)


def test_the_patterns_and_their_order() -> None:
    assert [pattern.value for pattern in Pattern] == [
        "P0",
        "P1",
        "P3",
        "P4",
        "P5",
    ], "P2 is not defined"
    assert ORDER == (Pattern.P5, Pattern.P1, Pattern.P3, Pattern.P4, Pattern.P0)
    assert ORDER[0] is Pattern.P5, "P5 is a learned model with no account, so it is the weakest"
    assert weakest([Pattern.P3, Pattern.P1]) is Pattern.P1
    assert weakest([Pattern.P0, Pattern.P3]) is Pattern.P3
    assert weakest([]) is Pattern.P0
    assert at_least(Pattern.P3, Pattern.P1) and not at_least(Pattern.P1, Pattern.P3)


def test_p4_and_p5_have_their_names() -> None:
    assert Pattern.P4.label == "differentially private model"
    assert Pattern.P5.label == "learned model, no privacy account"


@pytest.mark.parametrize(
    ("found", "weakest_of"),
    [
        ([Pattern.P5, Pattern.P1], Pattern.P5),
        ([Pattern.P5, Pattern.P0], Pattern.P5),
        ([Pattern.P4, Pattern.P5], Pattern.P5),
        ([Pattern.P4, Pattern.P3], Pattern.P3),
        ([Pattern.P4, Pattern.P1], Pattern.P1),
        ([Pattern.P4, Pattern.P0], Pattern.P4),
        ([Pattern.P3, Pattern.P4, Pattern.P0], Pattern.P3),
        ([Pattern.P5], Pattern.P5),
        ([Pattern.P4], Pattern.P4),
    ],
)
def test_the_weakest_of_a_set_follows_the_order(found: List[Pattern], weakest_of: Pattern) -> None:
    assert weakest(found) is weakest_of
    assert weakest(reversed(found)) is weakest_of


def test_at_least_holds_for_every_pair_by_the_order() -> None:
    for position, pattern in enumerate(ORDER):
        for other_position, floor in enumerate(ORDER):
            assert at_least(pattern, floor) is (position >= other_position), (pattern, floor)
    assert at_least(Pattern.P4, Pattern.P3) and not at_least(Pattern.P3, Pattern.P4)
    assert at_least(Pattern.P0, Pattern.P4) and not at_least(Pattern.P4, Pattern.P0)
    assert not at_least(Pattern.P5, Pattern.P1) and at_least(Pattern.P1, Pattern.P5)
    for floor in (Pattern.P1, Pattern.P3, Pattern.P4, Pattern.P0):
        assert not at_least(Pattern.P5, floor), "P5 is below every other pattern"


def test_a_run_that_pins_no_profile_is_p0() -> None:
    graded = grade_profile(None, pinned=False)
    assert graded.pattern is Pattern.P0 and not graded.pinned and graded.reasons == ()


def test_a_profile_of_noisy_statistics_with_an_account_is_p3() -> None:
    graded = grade_profile(_private(), pinned=True)
    assert graded.pattern is Pattern.P3 and graded.reasons == ()
    accounting = graded.accounting
    assert accounting is not None
    assert accounting.definition == "zero-concentrated differential privacy"
    assert accounting.unit == "subscriber" and accounting.parts == 2 and accounting.public_structure
    assert accounting.total == {"rho": "0.3"} and accounting.spent == {"rho": "0.2"}


def test_the_example_profile_of_the_spec_is_p3_too() -> None:
    document = json.loads((SPEC / "profile-1-2-private-example.json").read_text(encoding="utf-8"))
    assert grade_profile(document, pinned=True).pattern is Pattern.P3


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda d: d["tables"]["subscriber"].update(rowCount=9195), "row count"),
        (
            lambda d: d["tables"]["subscriber"]["columns"]["region"].update(
                metrics={"count": {"definition": "count/1", "status": "F", "value": 1}}
            ),
            "column metrics",
        ),
        (
            lambda d: d["tables"]["subscriber"]["columns"]["region"].update(
                statistics=[{"definition": "levelHistogram/1", "groups": []}]
            ),
            "a statistic released without noise",
        ),
        (
            lambda d: d["tables"]["subscriber"].update(
                columnPairs=[{"columns": [], "statistics": []}]
            ),
            "released without noise in tables.`subscriber`.columnPairs[0]",
        ),
        (
            lambda d: d.update(
                relationships=[
                    {"parent": {}, "child": {}, "statistics": [{"definition": "mean/1"}]}
                ]
            ),
            "released without noise in relationships[0]",
        ),
        (lambda d: d.update(queries=[{"name": "q", "statistics": []}]), "query"),
        (lambda d: d.update(invariants=[{"name": "f", "statistics": []}]), "fact"),
        (
            lambda d: d.update(
                references={"plan": {"disclosure": "none", "columns": ["a"], "rows": []}}
            ),
            "reference rows",
        ),
        (lambda d: d["privacy"].pop("publicStructure"), "chosen without the data"),
        (lambda d: d["privacy"].update(publicStructure=False), "chosen without the data"),
        (lambda d: d.pop("privacy"), "states no account"),
    ],
)
def test_anything_the_engine_can_read_that_is_not_noisy_makes_the_run_p1(
    change: Callable[[Dict[str, Any]], None], reason: str
) -> None:
    graded = _graded(change)
    assert graded.pattern is Pattern.P1
    assert any(reason in found for found in graded.reasons), graded.reasons


def test_a_run_that_pins_a_profile_it_cannot_read_is_p1() -> None:
    graded = grade_profile(None, pinned=True)
    assert graded.pattern is Pattern.P1 and graded.pinned and graded.reasons


def test_a_profile_with_no_account_is_p1_whatever_its_statistics() -> None:
    document = _private()
    document.pop("privacy")
    for table in document["tables"].values():
        table.pop("cliques")
    assert grade_profile(document, pinned=True).pattern is Pattern.P1


def _exact_beside_a_block() -> Dict[str, Any]:
    """The private profile with its noisy cliques gone and an exact row count in their place."""
    document = _private()
    for table in document["tables"].values():
        table.pop("cliques")
        table["rowCount"] = 9195
    return document


def test_exact_statistics_beside_a_privacy_block_grade_no_better_than_without_the_block() -> None:
    with_block = _exact_beside_a_block()
    without = copy.deepcopy(with_block)
    without.pop("privacy")
    blocked = grade_profile(with_block, pinned=True)
    plain = grade_profile(without, pinned=True)
    assert blocked.pattern is Pattern.P1 and plain.pattern is Pattern.P1
    assert at_least(plain.pattern, blocked.pattern)
    assert any("row count" in reason for reason in blocked.reasons)
    # The reader accepts the block that nothing spends, and the grade says nothing spends it.
    assert blocked.accounting is not None and blocked.accounting.parts == 0
    assert any("spent by no noisy statistic or clique" in reason for reason in blocked.reasons)


@pytest.mark.parametrize("rho", ["0.0001", "0.3", "1000000"])
def test_no_profile_reaches_p3_because_a_privacy_block_is_present(rho: str) -> None:
    # Whatever loss the block states, a profile with no noisy statistic or clique is not P3: the
    # grade comes from the statistics, and the block alone is a statement nothing backs.
    for statistics in ("none", "exact"):
        document = _private()
        for table in document["tables"].values():
            table.pop("cliques")
            if statistics == "exact":
                table["rowCount"] = 9195
        document["privacy"]["privacyLoss"] = {"rho": rho}
        graded = grade_profile(document, pinned=True)
        assert graded.pattern is Pattern.P1, statistics
        assert graded.accounting is not None and graded.accounting.parts == 0


def test_an_overstated_loss_over_noisy_statistics_does_not_change_the_grade() -> None:
    # The grade follows the statistics: all noisy under an account is P3 whatever slack the total
    # leaves, and one exact statistic is P1 whatever the total.
    stated = _private()
    stated["privacy"]["privacyLoss"] = {"rho": "100"}
    assert grade_profile(stated, pinned=True).pattern is Pattern.P3
    stated["tables"]["subscriber"]["rowCount"] = 9195
    assert grade_profile(stated, pinned=True).pattern is Pattern.P1


def test_the_section_states_the_p3_guarantee_as_the_profiles_and_in_nists_terms() -> None:
    text = render_privacy_section(RunGrade(grade_profile(_private(), pinned=True)))
    assert text.startswith("Run grade: **P3 (differentially private marginals)**.")
    assert (
        "Formal guarantee, as stated by the profile's release. The engine checks that the stated "
        "shares add up, not that noise was applied." in text
    )
    assert "- Definition: zero-concentrated differential privacy." in text
    assert "- Unit of privacy: one row of `subscriber`" in text
    assert "- Privacy loss, in total: rho = 0.3." in text
    assert (
        "the privacy loss rho of the parts adds; 2 noisy parts spend rho 0.2, within the total"
        in text
    )
    assert "cannot check, that the mechanism it names made the numbers" in text
    assert "chosen without the data" in text


def test_the_section_for_p0_does_not_call_it_a_guarantee() -> None:
    text = render_privacy_section(RunGrade(grade_profile(None, pinned=False)))
    assert (
        "Declared hand-written: no profile was pinned, so the engine read no data; it cannot confirm"
        in text
    )
    assert "formal guarantee" not in text.replace("no formal guarantee", "")


def test_the_section_for_p1_lists_why_and_gives_no_guarantee() -> None:
    graded = _graded(lambda d: d["tables"]["subscriber"].update(rowCount=1))
    text = render_privacy_section(RunGrade(graded))
    assert text.startswith("Run grade: **P1 (checked)**.")
    assert "No formal guarantee." in text and "Why not P3:" in text
    assert "- The profile holds a row count released without noise." in text
    assert "gives this run no guarantee at its grade" in text
    assert "Formal guarantee, as stated" not in text


def test_a_run_that_was_not_graded_says_so() -> None:
    assert render_privacy_section(None).startswith("This run was not graded")


def test_a_copy_of_the_profile_grades_the_same() -> None:
    document = _private()
    assert grade_profile(copy.deepcopy(document), pinned=True) == grade_profile(
        document, pinned=True
    )


def test_a_name_with_a_pipe_cannot_forge_the_cells_of_a_row() -> None:
    from flux_engine.evidence.grades import ColumnFloor

    grade = RunGrade(
        grade_profile(None, pinned=False),
        (
            ColumnFloor("shop", "a | pii | P3 | yes", "pii", Pattern.P3, False),
            ColumnFloor("shop", "plain", None, None, True),
            ColumnFloor("shop", "certified", None, None, True),
            ColumnFloor("shop", "line\nbreak", None, None, True),
        ),
    )
    text = render_privacy_section(grade)
    rows = [line for line in text.splitlines() if line.startswith("| `shop`") or "column " in line]
    forged = next(line for line in text.splitlines() if "a \\| pii" in line)
    # five unescaped pipes after the leading one: five cells, whatever the name holds
    assert forged.replace("\\|", "").count("|") == 6
    assert "| `a \\| pii \\| P3 \\| yes` | pii | P3 or stronger | NO, below its minimum |" in forged
    assert "certified" not in text and "line\nbreak" not in text
    assert len(text.splitlines()) == len(text.split("\n"))  # no row was split by a newline
    assert rows


_P4_PLUGIN = GeneratorGrade(
    "subscriber",
    "learned",
    "my-package",
    "1.2.0",
    Pattern.P4,
    unit="one data subject with all of its records",
    loss="epsilon = 3, delta = 0.00001",
    accounting="a Renyi accountant over the training steps",
)
_P5_PLUGIN = GeneratorGrade("subscriber", "wild", "other-package", "0.4", Pattern.P5)


def test_a_run_with_no_profile_and_a_p4_plugin_is_p4() -> None:
    grade = RunGrade(grade_profile(None, pinned=False), (), (_P4_PLUGIN,))
    assert grade.pattern is Pattern.P4 and grade.profile.pattern is Pattern.P0


def test_the_run_is_the_weakest_of_the_profile_and_every_plugin() -> None:
    private = grade_profile(_private(), pinned=True)
    assert RunGrade(private, (), (_P4_PLUGIN,)).pattern is Pattern.P3
    assert RunGrade(private, (), (_P4_PLUGIN, _P5_PLUGIN)).pattern is Pattern.P5
    assert RunGrade(private).pattern is Pattern.P3


def test_the_section_states_a_p4_plugins_account_as_the_bundle_declares_it() -> None:
    text = render_privacy_section(RunGrade(grade_profile(None, pinned=False), (), (_P4_PLUGIN,)))
    assert text.startswith("Run grade: **P4 (differentially private model)**.")
    assert "Formal guarantee, as the bundle declares it for each generator plugin below." in text
    assert (
        "- `subscriber`: plugin `learned` from `my-package` `1.2.0`, declared P4 "
        "(differentially private model). Unit of privacy: `one data subject with all of its "
        "records`. Privacy loss: epsilon = 3, delta = 0.00001. Accounting: `a Renyi accountant "
        "over the training steps`." in text
    )
    assert "the engine checks the account's form, not how the model was trained" in text.replace(
        "The engine", "the engine"
    )
    assert "**P4 differentially private model**" in text and "**P5 learned model" in text


def test_the_section_for_a_p5_plugin_gives_no_guarantee_and_no_account() -> None:
    text = render_privacy_section(RunGrade(grade_profile(None, pinned=False), (), (_P5_PLUGIN,)))
    assert text.startswith("Run grade: **P5 (learned model, no privacy account)**.")
    assert "No privacy account." in text and "Unit of privacy" not in text
    assert "plugin `wild` from `other-package` `0.4`, declared P5" in text
    assert "Formal guarantee" not in text


def test_a_plugin_name_that_would_forge_a_row_or_a_section_is_withheld() -> None:
    forged = GeneratorGrade(
        "shop",
        "learned",
        "pkg",
        "1",
        Pattern.P4,
        unit="a\n\n## Forged section",
        loss="rho = 1",
        accounting="x `y`",
    )
    text = render_privacy_section(RunGrade(grade_profile(None, pinned=False), (), (forged,)))
    assert "## Forged section" not in text
    assert "Unit of privacy: withheld." in text and "Accounting: withheld." in text


def test_a_run_with_no_plugin_reads_as_it_did() -> None:
    text = render_privacy_section(RunGrade(grade_profile(None, pinned=False)))
    assert "Generator plugins" not in text


def test_a_noisy_profile_that_holds_an_exact_history_is_p1_and_names_the_history() -> None:
    history = json.loads((SPEC / "profile-1-3-example.json").read_text(encoding="utf-8"))
    table = history["tables"]["plan_version"]["history"]

    def add(document: Dict[str, Any]) -> None:
        document["_schemaURL"] = history["_schemaURL"]
        document["disclosureRules"].update(history["disclosureRules"])
        document["tables"]["plan_version"] = {"columns": {}, "history": table}
        document["tables"]["plan_version"]["columns"] = history["tables"]["plan_version"]["columns"]

    graded = _graded(add)
    assert graded.pattern is Pattern.P1
    assert any("history" in reason for reason in graded.reasons)
    accounting = graded.accounting
    assert accounting is not None and accounting.parts == 2
    assert accounting.spent == {"rho": "0.2"}


# ---------------------------------------------------------------------------
# Calendar and date-pair statistics, relationship dates, and members the grade does not know
# ---------------------------------------------------------------------------

_MONTHS = [f"{n:02d}" for n in range(1, 13)]
_ORDER_LEVELS = ["after", "before", "same"]
_NOISE = {"mechanism": "gaussian", "scale": "10", "privacyLoss": {"rho": "0.05"}}


def _exact_histogram(levels: List[str], counts: List[int]) -> Dict[str, Any]:
    return {
        "definition": "levelHistogram/1",
        "levels": levels,
        "sampleSize": {"min": 1000, "max": 9999},
        "disclosure": "noisy",
        "groups": [{"group": "", "k": sum(counts), "levelCounts": counts, "status": "F"}],
    }


def _noisy_histogram(levels: List[str]) -> Dict[str, Any]:
    return {
        "definition": "levelHistogram/1",
        "levels": levels,
        "sampleSize": {"min": 1000, "max": 9999},
        "disclosure": "noisy",
        "groups": [
            {"group": "", "status": "F", "noisyCounts": [str(100 + n) for n in range(len(levels))]}
        ],
        "noise": dict(_NOISE),
    }


def _with_timestamps(document: Dict[str, Any]) -> Dict[str, Any]:
    document["_schemaURL"] = (
        "https://agenticstiger.github.io/flux/engine/spec/profile/1-3-0/FluxProfile.json"
    )
    columns = document["tables"]["subscriber"]["columns"]
    columns["joined_at"] = {"domain": "timestamp"}
    columns["left_at"] = {"domain": "timestamp"}
    return document


def _calendar(noisy: bool) -> List[Dict[str, Any]]:
    month = _noisy_histogram(_MONTHS) if noisy else _exact_histogram(_MONTHS, [1] + [0] * 11)
    return [{"column": "joined_at", "month": month}]


def _date_pairs(noisy: bool) -> List[Dict[str, Any]]:
    order = _noisy_histogram(_ORDER_LEVELS) if noisy else _exact_histogram(_ORDER_LEVELS, [1, 0, 0])
    return [{"start": "joined_at", "end": "left_at", "statistics": [], "order": order}]


def _calendar_profile(noisy: bool) -> Dict[str, Any]:
    document = _with_timestamps(_private())
    document["tables"]["subscriber"]["calendar"] = _calendar(noisy)
    return document


def test_an_exact_calendar_makes_the_profile_p1_and_names_the_section() -> None:
    from flux_stats.profile import parse_profile

    document = _calendar_profile(noisy=False)
    parse_profile(document)
    graded = grade_profile(document, pinned=True)
    assert graded.pattern is Pattern.P1
    assert (
        "the profile holds a statistic released without noise in tables.`subscriber`.calendar"
        in graded.reasons
    )


def test_an_exact_date_pair_makes_the_profile_p1_and_names_the_section() -> None:
    document = _with_timestamps(_private())
    document["tables"]["subscriber"]["datePairs"] = _date_pairs(noisy=False)
    graded = grade_profile(document, pinned=True)
    assert graded.pattern is Pattern.P1
    assert (
        "the profile holds a statistic released without noise in tables.`subscriber`.datePairs"
        in graded.reasons
    )


def test_a_date_pair_with_a_noisy_order_and_an_exact_statistic_is_p1() -> None:
    document = _with_timestamps(_private())
    pairs = _date_pairs(noisy=True)
    pairs[0]["statistics"] = [
        _exact_histogram([f"{n}" for n in range(3)], [1, 0, 0]) | {"definition": "levelHistogram/1"}
    ]
    document["tables"]["subscriber"]["datePairs"] = pairs
    assert grade_profile(document, pinned=True).pattern is Pattern.P1


def test_a_fully_noisy_calendar_and_date_pair_section_keeps_p3() -> None:
    document = _calendar_profile(noisy=True)
    document["tables"]["subscriber"]["datePairs"] = _date_pairs(noisy=True)
    graded = grade_profile(document, pinned=True)
    assert graded.pattern is Pattern.P3 and graded.reasons == ()


def test_an_exact_relationship_date_or_exposure_makes_the_profile_p1() -> None:
    for member in ("dates", "exposure"):
        document = _private()
        document["relationships"] = [{"parent": {}, "child": {}, "statistics": [], member: [{}]}]
        graded = grade_profile(document, pinned=True)
        assert graded.pattern is Pattern.P1
        assert (
            f"the profile holds a statistic released without noise in relationships[0].{member}"
            in graded.reasons
        )


def test_a_noisy_calendar_and_date_pair_are_counted_in_the_account_as_the_reader_counts() -> None:
    from flux_stats.errors import FluxError
    from flux_stats.profile import parse_profile

    document = _calendar_profile(noisy=True)
    document["tables"]["subscriber"]["datePairs"] = _date_pairs(noisy=True)
    document["privacy"]["privacyLoss"] = {"rho": "0.3"}
    accounting = grade_profile(document, pinned=True).accounting
    assert accounting is not None
    assert accounting.parts == 4 and accounting.spent == {"rho": "0.3"}
    parse_profile(document)  # the reader's budget check holds at exactly this spend
    document["privacy"]["privacyLoss"] = {"rho": "0.29"}
    with pytest.raises(FluxError):
        parse_profile(document)  # and it refuses one hundredth less, as the account says


def test_an_unknown_member_makes_the_profile_p1_and_is_named() -> None:
    cases = {
        "tables.`subscriber`.`extras`": lambda d: d["tables"]["subscriber"].update(extras=[1]),
        "tables.`subscriber`.columns.`region`.`extras`": lambda d: d["tables"]["subscriber"][
            "columns"
        ]["region"].update(extras=1),
        "relationships[0].`extras`": lambda d: d.update(
            relationships=[{"parent": {}, "child": {}, "statistics": [], "extras": 1}]
        ),
    }
    for path, change in cases.items():
        graded = _graded(change)
        assert graded.pattern is Pattern.P1
        assert any(path in reason for reason in graded.reasons), (path, graded.reasons)


#: Schema members that carry no statistic, each with the reason the grade leaves them be.
_STRUCTURAL_ALLOWLIST = {
    ("Column", "domain"): "the column's kind, a name with no value of the data",
    ("Relationship", "parent"): "the parent's table and key columns, names only",
    ("Relationship", "child"): "the child's table and key columns, names only",
    ("Relationship", "structural"): "classes of parents a constraint leaves empty, no counts",
}


def test_every_schema_member_of_a_table_column_or_relationship_is_graded_or_allowed() -> None:
    for name, later in (("profile-1-3.schema.json", True), ("profile-1-4.schema.json", False)):
        _check_members(json.loads((SPEC / name).read_text(encoding="utf-8")), earlier=later)


#: The members of a table that only format 1-4-0 holds, so the 1-3-0 schema does not list them.
_ADDED_IN_1_4_0 = {"Table": frozenset({"cliqueSelection"})}


def _check_members(schema: Dict[str, Any], *, earlier: bool = False) -> None:
    from flux_engine.evidence import grades

    graded = {
        "Table": grades.GRADED_TABLE_MEMBERS,
        "Column": grades.GRADED_COLUMN_MEMBERS,
        "Relationship": grades.GRADED_RELATIONSHIP_MEMBERS,
    }
    structural = {
        "Table": frozenset(),
        "Column": grades.STRUCTURAL_COLUMN_MEMBERS,
        "Relationship": grades.STRUCTURAL_RELATIONSHIP_MEMBERS,
    }
    top = set(schema["properties"])
    assert top == grades.GRADED_TOP_LEVEL_MEMBERS | grades.STRUCTURAL_TOP_LEVEL_MEMBERS
    privacy = set(schema["$defs"]["Privacy"]["properties"])
    assert privacy == grades.STRUCTURAL_PRIVACY_MEMBERS
    for definition, members in graded.items():
        found = set(schema["$defs"][definition]["properties"])
        for member in sorted(found):
            allowed = (definition, member) in _STRUCTURAL_ALLOWLIST
            assert member in members or allowed, f"{definition}.{member} is not graded"
            if allowed:
                assert member in structural[definition], f"{definition}.{member} is flagged"
        later = _ADDED_IN_1_4_0.get(definition, frozenset()) if earlier else frozenset()
        assert (
            members - later <= found
        ), f"the grade reads {sorted(members - later - found)} the schema lacks"
    assert {key for key, _ in _STRUCTURAL_ALLOWLIST} <= set(graded)


_HOSTILE = "audit\n\n## Privacy Grade (verified)\n\n| a | b |\n\n<!--"


def _assert_nothing_forged(text: str) -> None:
    assert "## Privacy Grade (verified)" not in text
    assert "<!--" not in text
    assert "| a | b |" not in text
    assert "\n\n##" not in text


def test_a_name_from_the_profile_cannot_forge_a_heading_a_row_or_a_comment_in_a_grade_reason() -> (
    None
):
    def table_id(document: Dict[str, Any]) -> None:
        document["tables"][_HOSTILE] = {"extras": 1}

    def column(document: Dict[str, Any]) -> None:
        document["tables"]["subscriber"]["columns"][_HOSTILE] = {"extras": 1}

    def member(document: Dict[str, Any]) -> None:
        document["tables"]["subscriber"][_HOSTILE] = 1
        document["tables"]["subscriber"]["columns"]["region"][_HOSTILE] = 1
        document["relationships"] = [{"parent": {}, "child": {}, "statistics": [], _HOSTILE: 1}]

    for change in (table_id, column, member):
        graded = _graded(change)
        assert graded.pattern is Pattern.P1
        assert any("withheld" in reason for reason in graded.reasons)
        assert "\n".join(graded.reasons).count(_HOSTILE) == 0
        text = render_privacy_section(RunGrade(graded, ()))
        _assert_nothing_forged(text)
        assert text.count("\n## ") == 0


# ---------------------------------------------------------------------------
# Column pairs, and the members at the top of a profile and in its privacy block
# ---------------------------------------------------------------------------


def _pairs_profile() -> Dict[str, Any]:
    """The 1-4-0 example: one pair holding a noisy Kendall's and a noisy Spearman's, nothing exact."""
    document: Dict[str, Any] = json.loads(
        (SPEC / "profile-1-4-example.json").read_text(encoding="utf-8")
    )
    return document


def _exact_correlation() -> Dict[str, Any]:
    return {
        "definition": "correlation/1",
        "sampleSize": {"min": 1000},
        "disclosure": "noisy",
        "groups": [
            {
                "group": "",
                "k": 3,
                "status": "F",
                "Sx": "1",
                "Sy": "1",
                "Sxx": "1",
                "Sxy": "1",
                "Syy": "1",
            }
        ],
    }


def test_a_pair_whose_statistics_are_all_noisy_keeps_the_profile_p3() -> None:
    graded = grade_profile(_pairs_profile(), pinned=True)
    assert graded.pattern is Pattern.P3 and graded.reasons == ()
    assert graded.accounting is not None and graded.accounting.parts == 3


def test_a_pair_with_a_correlation_is_p1_and_the_reason_names_the_pair() -> None:
    document = _pairs_profile()
    document["tables"]["subscriber"]["columns"]["usage"] = {"domain": "number"}
    document["tables"]["subscriber"]["columnPairs"].append(
        {"columns": ["age", "usage"], "statistics": [_exact_correlation()]}
    )
    graded = grade_profile(document, pinned=True)
    assert graded.pattern is Pattern.P1
    assert graded.reasons == (
        "the profile holds a statistic released without noise in "
        "tables.`subscriber`.columnPairs[`age`, `usage`]",
    )


def test_a_pair_that_mixes_a_noisy_and_an_exact_statistic_is_p1_and_named() -> None:
    document = _pairs_profile()
    statistics = document["tables"]["subscriber"]["columnPairs"][0]["statistics"]
    del statistics[1]["noise"]
    statistics[1]["groups"] = [{"group": "", "k": 10, "status": "F", "A4": "100"}]
    graded = grade_profile(document, pinned=True)
    assert graded.pattern is Pattern.P1
    assert any("columnPairs[`age`, `tenure`]" in reason for reason in graded.reasons)
    assert graded.accounting is not None and graded.accounting.parts == 2


def test_a_pair_with_no_statistic_is_not_noisy() -> None:
    document = _pairs_profile()
    document["tables"]["subscriber"]["columnPairs"][0]["statistics"] = []
    graded = grade_profile(document, pinned=True)
    assert graded.pattern is Pattern.P1
    assert any("columnPairs[`age`, `tenure`]" in reason for reason in graded.reasons)


def test_a_pair_name_that_would_forge_a_row_or_a_section_is_withheld() -> None:
    document = _pairs_profile()
    document["tables"]["subscriber"]["columnPairs"][0]["columns"] = [_HOSTILE, "tenure"]
    document["tables"]["subscriber"]["columnPairs"][0]["statistics"].append(_exact_correlation())
    graded = grade_profile(document, pinned=True)
    assert graded.pattern is Pattern.P1 and any("withheld" in r for r in graded.reasons)
    assert "\n".join(graded.reasons).count(_HOSTILE) == 0
    _assert_nothing_forged(render_privacy_section(RunGrade(graded, ())))


def test_an_unknown_top_level_or_privacy_member_counts_as_released_without_noise() -> None:
    top = _graded(lambda d: d.update(extras=[1]))
    assert top.pattern is Pattern.P1
    assert any(
        "`extras`" in reason and "does not know" in reason and "released without noise" in reason
        for reason in top.reasons
    ), top.reasons
    inside = _graded(lambda d: d["privacy"].update(extras=1))
    assert inside.pattern is Pattern.P1
    assert any("privacy.`extras`" in reason for reason in inside.reasons), inside.reasons


def test_the_members_the_grade_knows_at_the_top_and_in_privacy_are_not_flagged() -> None:
    graded = grade_profile(_private(), pinned=True)
    assert graded.pattern is Pattern.P3 and graded.reasons == ()


def test_a_hostile_top_level_or_privacy_name_cannot_forge_a_heading_a_row_or_a_comment() -> None:
    for change in (
        lambda d: d.update({_HOSTILE: 1}),
        lambda d: d["privacy"].update({_HOSTILE: 1}),
    ):
        graded = _graded(change)
        assert graded.pattern is Pattern.P1
        assert any("withheld" in reason for reason in graded.reasons)
        assert "\n".join(graded.reasons).count(_HOSTILE) == 0
        _assert_nothing_forged(render_privacy_section(RunGrade(graded, ())))


# ---------------------------------------------------------------------------
# A statistic grouped by bins, and a table's clique selection (profile format 1-4-0)
# ---------------------------------------------------------------------------


def _binned(*, noisy: bool) -> Dict[str, Any]:
    statistic: Dict[str, Any] = {
        "definition": "marginalHistogram/1",
        "bins": ["0", "10", "20"],
        "sampleSize": {"min": 1000, "max": 9999},
        "disclosure": "noisy",
        "groupByBins": {"column": "age", "bins": ["0", "30", "60"]},
        "groups": [{"group": key, "status": "F", "noisyCounts": ["5", "6"]} for key in ("0", "30")],
    }
    if noisy:
        statistic["noise"] = {
            "mechanism": "gaussian",
            "scale": "5",
            "privacyLoss": {"rho": "0.02"},
        }
    else:
        statistic["groups"] = [
            {"group": key, "status": "F", "k": 11, "binCounts": [5, 6]} for key in ("0", "30")
        ]
    return statistic


def _with_binned(noisy: bool) -> Dict[str, Any]:
    document: Dict[str, Any] = json.loads(
        (SPEC / "profile-1-4-example.json").read_text(encoding="utf-8")
    )
    document["privacy"]["privacyLoss"] = {"rho": "1"}
    document["tables"]["subscriber"]["columns"]["tenure"]["statistics"] = [_binned(noisy=noisy)]
    return document


def test_a_statistic_grouped_by_bins_is_noisy_only_when_it_carries_noise() -> None:
    noisy = grade_profile(_with_binned(True), pinned=True)
    assert noisy.pattern is Pattern.P3 and noisy.reasons == ()
    exact = grade_profile(_with_binned(False), pinned=True)
    assert exact.pattern is Pattern.P1
    assert any("a statistic released without noise" in reason for reason in exact.reasons)


def test_a_clique_selection_is_accounting_and_never_makes_a_profile_not_p3() -> None:
    document: Dict[str, Any] = json.loads(
        (SPEC / "profile-1-4-cliques-example.json").read_text(encoding="utf-8")
    )
    graded = grade_profile(document, pinned=True)
    assert graded.pattern is Pattern.P3 and graded.reasons == ()
    assert graded.accounting is not None and graded.accounting.parts == 4
    # an unknown member beside it is still not graded as noisy
    document["tables"]["subscriber"]["cliqueSelectionX"] = {}
    assert grade_profile(document, pinned=True).pattern is Pattern.P1


def _noisy_stat_on(
    document: Dict[str, Any], column: str, change: Callable[[Dict[str, Any]], None]
) -> None:
    """Add a noisy histogram of ``region`` that *change* then edits."""
    columns = document["tables"]["subscriber"]["columns"]
    columns.setdefault(column, {"domain": "number"})
    statistic = _noisy_histogram(["east", "north", "west"])
    change(statistic)
    columns["region"]["statistics"] = [statistic]


def _fixed_reasons(graded: ProfileGrade) -> str:
    return "\n".join(graded.reasons)


def test_a_noisy_statistic_grouped_by_a_number_column_is_p1() -> None:
    def number_group(document: Dict[str, Any]) -> None:
        _noisy_stat_on(document, "tenure", lambda s: s.update(groupBy="tenure"))

    graded = _graded(number_group)
    assert graded.pattern is Pattern.P1
    assert "grouped by a column whose keys are data values" in _fixed_reasons(graded)
    assert "tenure" not in _fixed_reasons(graded)


def test_a_noisy_statistic_grouped_by_a_timestamp_column_is_p1() -> None:
    def timestamp_group(document: Dict[str, Any]) -> None:
        _noisy_stat_on(document, "joined_at", lambda s: s.update(groupBy="joined_at"))
        document["tables"]["subscriber"]["columns"]["joined_at"] = {"domain": "timestamp"}

    assert _graded(timestamp_group).pattern is Pattern.P1


def test_a_noisy_statistic_grouped_by_a_column_the_profile_does_not_hold_is_p1() -> None:
    graded = _graded(lambda d: _noisy_stat_on(d, "plan", lambda s: s.update(groupBy="nowhere")))
    assert graded.pattern is Pattern.P1


def test_a_noisy_statistic_grouped_through_a_link_is_p1() -> None:
    link = {
        "parent": {"table": "account", "columns": ["id"]},
        "columns": ["account_id"],
        "column": "opened_at",
    }
    graded = _graded(lambda d: _noisy_stat_on(d, "plan", lambda s: s.update(groupByLink=link)))
    assert graded.pattern is Pattern.P1
    assert "grouped through a link" in _fixed_reasons(graded)
    assert "opened_at" not in _fixed_reasons(graded)


def test_a_noisy_statistic_with_an_exact_sample_size_band_is_p1() -> None:
    graded = _graded(
        lambda d: _noisy_stat_on(
            d, "plan", lambda s: s.update(sampleSize={"min": 9195, "max": 9195})
        )
    )
    assert graded.pattern is Pattern.P1
    assert "sampleSize band whose min equals its max" in _fixed_reasons(graded)
    assert "9195" not in _fixed_reasons(graded)


def test_an_enumerated_group_and_a_real_sample_size_band_keep_p3() -> None:
    def keep(document: Dict[str, Any]) -> None:
        def grouped(statistic: Dict[str, Any]) -> None:
            statistic["groupBy"] = "plan"
            statistic["groups"][0]["group"] = "basic"

        _noisy_stat_on(document, "plan", grouped)
        document["tables"]["subscriber"]["columns"]["plan"] = {"domain": "category"}

    graded = _graded(keep)
    assert graded.pattern is Pattern.P3 and graded.reasons == ()
    assert _graded(lambda d: _noisy_stat_on(d, "plan", lambda s: None)).pattern is Pattern.P3


def test_an_exact_sample_size_band_is_p1_in_format_1_4_0_too() -> None:
    """The 1-4-0 declaration covers bands chosen without the data; a band of one size is not one."""
    document = _pairs_profile()
    statistic = document["tables"]["subscriber"]["columns"]["region"]["statistics"][0]
    statistic["sampleSize"] = {"min": 14000, "max": 14000}
    graded = grade_profile(document, pinned=True)
    assert graded.pattern is Pattern.P1
    assert any("min equals its max" in reason for reason in graded.reasons)


def test_a_group_by_on_a_column_the_profile_does_not_describe_is_p1_in_format_1_4_0_too() -> None:
    """Its group keys are data values, which no declaration makes public."""
    document = _pairs_profile()
    statistic = document["tables"]["subscriber"]["columns"]["region"]["statistics"][0]
    statistic["groupBy"] = "undescribed"
    graded = grade_profile(document, pinned=True)
    assert graded.pattern is Pattern.P1
    assert any("keys are data values" in reason for reason in graded.reasons)


# ---------------------------------------------------------------------------
# A relationship's ``structural`` member is structure only from format 1-4-0
# ---------------------------------------------------------------------------


def _with_structural(schema_url: str) -> ProfileGrade:
    def change(document: Dict[str, Any]) -> None:
        document["_schemaURL"] = document["_schemaURL"].replace("1-2-0", schema_url)
        plan = copy.deepcopy(document["tables"]["subscriber"])
        for member in ("cliques", "cliqueSelection", "rowCount", "columnPairs"):
            plan.pop(member, None)
        document["tables"]["plan"] = plan
        document["relationships"] = [
            {
                "parent": {"table": "plan", "columns": ["region"]},
                "child": {"table": "subscriber", "columns": ["region"]},
                "statistics": [],
                "structural": ["childless"],
            }
        ]

    return _graded(change)


def test_a_structural_member_in_a_profile_of_format_1_3_0_is_not_structure_and_is_p1() -> None:
    # 0.10.0 graded this profile P1; the member was read as structure in every format since.
    graded = _with_structural("1-3-0")
    assert graded.pattern is Pattern.P1
    assert any("structural" in reason and "does not know" in reason for reason in graded.reasons)


def test_a_structural_member_in_a_profile_of_format_1_4_0_is_structure_and_keeps_p3() -> None:
    graded = _with_structural("1-4-0")
    assert graded.reasons == ()
    assert graded.pattern is Pattern.P3
