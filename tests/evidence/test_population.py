# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Population section of ``EVIDENCE.md`` (ADR 0031, section 1), without a run."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Sequence, Tuple

import pytest

from flux_engine.api.verdict import Reproducibility
from flux_engine.core.digest import DigestDomain, DigestRef
from flux_engine.evidence.population import (
    MAX_CELLS,
    MAX_CHARTS,
    PopulationCell,
    PopulationGroup,
    PopulationProperty,
    render_population_section,
    safe_text,
    share_percent,
)
from flux_engine.evidence.report import EVIDENCE_SECTIONS, render_evidence_report

pytestmark = pytest.mark.unit

ALLOWED = re.compile(r"[A-Za-z0-9 _.,:;/()+=\-]*")


def _property(
    cells: Sequence[Tuple[str, int, int]],
    *,
    expose_id: str = "subscriber",
    column: str = "market",
    score: str = "0.9",
) -> PopulationProperty:
    return PopulationProperty(
        expose_id=expose_id,
        column=column,
        score=score,
        groups=(PopulationGroup("", tuple(PopulationCell(*cell) for cell in cells)),),
    )


def _fence_count(text: str) -> int:
    return text.count("```")


def test_the_section_follows_fidelity_in_a_report() -> None:
    report = render_evidence_report(
        bundle_name="b",
        kernel_version="0.1.0",
        run_digest=DigestRef(
            value="sha256:" + "aa" * 32, domain=DigestDomain.JCS_DOCUMENT, subject="run-digest"
        ),
        generated_at="2026-01-01T00:00:00Z",
        conformance_summary="ok",
        reproducibility=Reproducibility.NOT_CHECKED,
        fidelity_summary="fidelity text",
        population=[_property([("DE", 1, 1)])],
    )
    headings = re.findall(r"^## (.+)$", report, flags=re.MULTILINE)
    assert tuple(headings) == EVIDENCE_SECTIONS
    assert headings[headings.index("Fidelity") + 1] == "Population"


def test_a_small_property_draws_exactly_this_chart() -> None:
    text = render_population_section(
        [_property([("DE", 50, 487), ("FR", 30, 312), ("NL", 20, 201)])]
    )
    assert text.split("\n\n", 1)[1] == (
        "subscriber.market: score 0.9.\n\n"
        "```mermaid\n"
        "xychart-beta\n"
        '  title "subscriber.market: bars profile, line run"\n'
        '  x-axis ["DE", "FR", "NL"]\n'
        '  y-axis "share (percent)" 0 --> 50\n'
        "  bar [50.0, 30.0, 20.0]\n"
        "  line [48.7, 31.2, 20.1]\n"
        "```\n\n"
        "<details><summary>Numbers</summary>\n\n"
        "| cell | profile | run |\n"
        "| --- | --- | --- |\n"
        "| DE | 50.0 | 48.7 |\n"
        "| FR | 30.0 | 31.2 |\n"
        "| NL | 20.0 | 20.1 |\n\n"
        "</details>\n\n"
        "Properties left out of the charts: 0 of 1."
    )


def test_a_grouped_property_takes_each_share_over_its_own_group() -> None:
    grouped = PopulationProperty(
        "s",
        "c",
        "1",
        (
            PopulationGroup("g0", (PopulationCell("a", 1, 3), PopulationCell("b", 3, 1))),
            PopulationGroup("g1", (PopulationCell("a", 1, 1), PopulationCell("b", 1, 1))),
        ),
    )
    text = render_population_section([grouped])
    assert "| g0: a | 25.0 | 75.0 |" in text
    assert "| g1: b | 50.0 | 50.0 |" in text


@pytest.mark.parametrize(
    ("count", "total", "shown"),
    [
        (49, 400, "12.2"),  # 12.25 is a tie: the even tenth is 12.2
        (497, 4000, "12.4"),  # 12.425 rounds up
        (1235, 10000, "12.4"),  # 12.35 is a tie: the even tenth is 12.4
        (1, 8, "12.5"),
        (1, 3, "33.3"),
        (0, 5, "0.0"),
        (5, 5, "100.0"),
        (1, 0, "0.0"),
    ],
)
def test_a_share_is_rounded_half_to_even_to_one_decimal(count: int, total: int, shown: str) -> None:
    assert share_percent(count, total) == Decimal(shown)
    assert str(share_percent(count, total)) == shown


@pytest.mark.parametrize(
    ("counts", "ymax"),
    [
        ((0,), 10),  # no records: every share is 0.0, and the axis is at least 10
        ((1,) * 20, 10),  # 5.0
        ((1,) * 10, 10),  # exactly 10.0
        ((101, 899), 90),  # 89.9
        ((500, 500), 50),
        ((3, 1), 80),  # 75.0
        ((1, 1, 1), 40),  # 33.3
        ((7,), 100),  # one cell holds everything
    ],
)
def test_ymax_is_the_first_multiple_of_ten_at_or_above_the_largest_share(
    counts: Tuple[int, ...], ymax: int
) -> None:
    text = render_population_section(
        [_property([(f"c{index}", count, count) for index, count in enumerate(counts)])]
    )
    assert f'y-axis "share (percent)" 0 --> {ymax}\n' in text


def test_hostile_text_is_flattened_and_cannot_break_out() -> None:
    hostile = [
        "``` x",
        'say "hi"',
        "%%{init: {}}%%",
        "line\nbreak",
        "</details>",
        "a|b",
        "café ‮ ",
        "<script>",
    ]
    text = render_population_section(
        [
            _property(
                [(label, 1, 1) for label in hostile],
                expose_id="e`x`%%",
                column="c\n</details>",
            )
        ]
    )
    block = text.split("```mermaid\n", 1)[1].split("```\n", 1)[0]
    assert _fence_count(text) == 2, "only the one fence the section writes"
    assert text.count("<details>") == 1 and text.count("</details>") == 1
    assert "%%" not in text and "<script" not in text
    for line in block.splitlines():
        assert line == line.strip() or line.startswith("  ")
    assert block.count("\n") == 6, "the chart is the seven lines it was written as"
    labels = re.search(r"x-axis \[(.*)\]", block)
    assert labels is not None
    names = re.findall(r'"([^"]*)"', labels.group(1))
    assert len(names) == len(hostile)
    assert all(ALLOWED.fullmatch(name) for name in names)
    rows = [line for line in text.splitlines() if line.startswith("| ") and "---" not in line]
    assert all(row.count("|") == 4 for row in rows), "no cell adds a column"
    assert any("caf_ __" in row for row in rows), "a non-ASCII character becomes an underscore"


def test_text_keeps_only_the_allowed_characters_and_is_cut_at_forty() -> None:
    assert safe_text("aZ09 _.,:;/()+=-") == "aZ09 _.,:;/()+=-"
    assert safe_text("\"'`%<>|\\[]{}#*&!?@$^~\n\t") == "_" * len("\"'`%<>|\\[]{}#*&!?@$^~\n\t")
    assert safe_text("x" * 100) == "x" * 40
    assert safe_text("é" * 100) == "_" * 40


def test_more_cells_than_a_chart_holds_is_listed_with_its_score_and_not_drawn() -> None:
    big = _property(
        [(f"c{index}", 1, 1) for index in range(MAX_CELLS + 1)], column="wide", score="0.5"
    )
    exact = _property([(f"c{index}", 1, 1) for index in range(MAX_CELLS)], column="narrow")
    text = render_population_section([big, exact])
    assert (
        f"subscriber.wide: score 0.5, not drawn ({MAX_CELLS + 1} cells, more than {MAX_CELLS})."
        in text
    )
    assert "subscriber.narrow: score 0.9." in text
    assert text.count("```mermaid") == 1
    assert text.endswith("Properties left out of the charts: 1 of 2.")


def test_no_more_than_thirty_two_charts_are_drawn_in_property_order() -> None:
    many = [_property([("a", 1, 1)], column=f"col{index}") for index in range(MAX_CHARTS + 3)]
    text = render_population_section(many)
    assert text.count("```mermaid") == MAX_CHARTS
    assert f'title "subscriber.col{MAX_CHARTS - 1}:' in text
    assert f"subscriber.col{MAX_CHARTS}:" not in text
    assert text.endswith(f"Properties left out of the charts: 3 of {MAX_CHARTS + 3}.")


def test_a_run_without_a_profile_says_there_is_no_target() -> None:
    assert render_population_section(None) == (
        "There is no target to draw: this run was shipped without a profile."
    )


def test_a_profiled_run_with_no_scored_histogram_says_so() -> None:
    assert "nothing to draw" in render_population_section([])


def _gated(text: str) -> None:
    """The report's own refusal of a reserved word, over a rendered section."""
    from flux_engine.evidence.report import require_no_banned_words

    require_no_banned_words(text)


@pytest.mark.parametrize(
    "word", ["uncertified", "Certified", "approves", "improves", "Concordance"]
)
def test_a_level_holding_a_reserved_word_does_not_stop_the_report(word: str) -> None:
    section = render_population_section([_property([(word, 3, 4), ("plain", 7, 6)])])
    _gated(section)
    assert word not in section
    # The cell is still drawn, under a name that holds no data, beside the plain one.
    assert "| plain | 70.0 | 60.0 |" in section
    assert "| cell 1 | 30.0 | 40.0 |" in section


def test_a_column_holding_a_reserved_word_does_not_stop_the_report() -> None:
    section = render_population_section(
        [_property([("a", 1, 1), ("b", 1, 1)], column="certified_flag", expose_id="improves")]
    )
    _gated(section)
    assert "certified_flag" not in section and "improves" not in section
    assert "column" in section


def test_a_group_label_and_a_prefix_cannot_carry_a_reserved_word_either() -> None:
    prop = PopulationProperty(
        expose_id="t",
        column="c",
        score="1",
        groups=(PopulationGroup("proves", (PopulationCell("x", 1, 1),)),),
    )
    _gated(render_population_section([prop]))


def test_labels_without_a_reserved_word_are_written_as_before() -> None:
    section = render_population_section([_property([("DE", 1, 1), ("née", 1, 3)])])
    assert 'x-axis ["DE", "n_e"]' in section
    assert "| n_e | 50.0 | 75.0 |" in section


def _children(*, outside: bool, charted: bool = True) -> PopulationProperty:
    return PopulationProperty(
        expose_id="invoice",
        column="account",
        score="0.76" if outside else "1",
        label="children:invoice.account",
        outside="distance 0.24, tolerance 0.1" if outside else "",
        groups=(
            (
                PopulationGroup(
                    "", (PopulationCell("0 to 1", 60, 35), PopulationCell("1 to 3", 10, 76))
                ),
            )
            if charted
            else ()
        ),
    )


def test_a_children_per_parent_property_is_drawn_under_its_property_id() -> None:
    section = render_population_section([_children(outside=True)])
    assert 'title "children:invoice.account: bars profile, line run"' in section
    assert (
        "children:invoice.account: score 0.76, outside its tolerance (distance 0.24, tolerance 0.1)."
        in section
    )
    assert section.rstrip().endswith("Properties left out of the charts: 0 of 1.")


def test_a_property_left_out_outside_its_tolerance_is_named_with_its_distance() -> None:
    left_out = PopulationProperty(
        expose_id="invoice",
        column="",
        score="0.5",
        label="children:invoice.account",
        outside="distance 0.24, tolerance 0.1",
        groups=(),
    )
    within = PopulationProperty("invoice", "", "1", (), label="relationship:invoice.account")
    section = render_population_section([_property([("a", 1, 1)]), left_out, within])
    assert (
        "children:invoice.account: outside its tolerance (distance 0.24, tolerance 0.1), not drawn."
        in section
    )
    assert "relationship:invoice.account" not in section
    assert section.rstrip().endswith("Properties left out of the charts: 2 of 3.")


def test_no_sentence_claims_that_nothing_scores_children_per_parent() -> None:
    assert "No property scores children per parent" not in render_population_section(
        [_property([("a", 1, 1)])]
    )
