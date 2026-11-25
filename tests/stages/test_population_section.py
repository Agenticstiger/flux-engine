# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``ship`` draws the run's population against the profile's in ``EVIDENCE.md`` (ADR 0031, section 1).

The bundle is the one ``fluxsim init`` writes for the profile of ``tests/author/test_given_int``:
an INT column ``rating`` over three levels, a ``verdict`` over two, and a ``levelHistogram``
property over each, one of them grouped by ``rating``. Planned, applied and shipped with the
profile it pins, the Population section holds a chart for each, and its numbers are checked here
against the profile's own counts and the rows the run emitted, worked out apart from the report.
The scorecard is not touched: its bytes are the ones the code before the section gave.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from flux_engine.cli._init import run_init
from flux_engine.concordance.scorecard import SCORECARD_FILE
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.run import run
from flux_engine.stages.ship import ship
from flux_engine.stages.verify import verify
from tests.author.test_given_int import CONTRACT, _profile
from tests.overlays import with_the_profile_named, without_replacements

from .conftest import FIXTURE_BUNDLE

pytestmark = pytest.mark.integration

_FORMAT_1_2_0 = "https://agenticstiger.github.io/flux/engine/spec/profile/1-2-0/FluxProfile.json"

#: The run's id and the sha256 of its ``scorecard.json`` as the code before the Population section
#: wrote them (origin/main at 764237e): the section adds nothing to the scorecard.
RUN_ID = "66ed3b33b1ab75ad"
#: The profile's lines per invoice, in the three bins of its histogram: not the run's.
PROFILE_LINES = [10, 40, 50]
SCORECARD_SHA256 = "fc0db618103d03de0b89cfa1d43ed75721eabcf5d55f638c74a32793cc6cb998"


class Shipped:
    def __init__(
        self, root: Path, withheld_level: bool = False, level: Optional[str] = None
    ) -> None:
        document: Dict[str, Any] = copy.deepcopy(_profile())
        document["_schemaURL"] = _FORMAT_1_2_0
        if withheld_level:
            # The profile withholds the count of rating 2, as a disclosure rule would.
            group = document["tables"]["review"]["columns"]["rating"]["statistics"][0]["groups"][0]
            group["levelCounts"][1] = None
            group["cellStatus"][1] = "C"
        contract, text = CONTRACT, json.dumps(document)
        if level is not None:
            # The level "return" of the verdict column is named by the data, so it may be any word.
            contract = contract.replace("'return'", f"'{level}'")
            text = text.replace('"return"', json.dumps(level))
        (root / "shop.fluid.yaml").write_text(contract, encoding="utf-8")
        self.profile = root / "shop.profile.json"
        self.profile.write_text(text, encoding="utf-8")
        written = run_init(
            str(root / "bundle"),
            contract=str(root / "shop.fluid.yaml"),
            profile=str(self.profile),
        )
        assert written.exit_code == 0, written.stderr
        # The scorecard and the run below are the ones made before init wrote `replacedBy`, when
        # its header still named the profile's file.
        self.bundle = with_the_profile_named(
            without_replacements(root / "bundle"), self.profile.name
        )
        self.state = root / ".flux"
        planned = plan(self.bundle, state_dir=self.state, profile=self.profile)
        assert planned.success, planned.stderr
        self.run_id = str(planned.artifacts["run_id"])
        applied = apply(self.bundle, state_dir=self.state, run_id=self.run_id, auto_plan=False)
        assert applied.success, applied.stderr
        verified = verify(
            self.bundle,
            run_id=self.run_id,
            state_dir=self.state,
            profile=self.profile,
            allow_declared_baseline_drift=True,
        )
        assert verified.success, verified.stderr
        shipped = ship(
            self.bundle,
            run_id=self.run_id,
            out_dir=root / "out",
            state_dir=self.state,
            profile=self.profile,
        )
        assert shipped.success, shipped.stderr
        self.run_dir = self.state / "runs" / self.run_id
        self.document = document

    @property
    def population(self) -> str:
        text = (self.run_dir / "EVIDENCE.md").read_text(encoding="utf-8")
        return text.split("## Population\n", 1)[1].split("\n## ", 1)[0]

    def rows(self) -> List[Dict[str, Any]]:
        attestation = json.loads(
            (self.run_dir / "seam-attestation-review.json").read_text(encoding="utf-8")
        )
        lines = (self.run_dir / "emits" / "review.ndjson").read_text(encoding="utf-8").splitlines()
        return [
            dict(zip(attestation["columns"], json.loads(line), strict=True))
            for line in lines
            if line
        ]


@pytest.fixture(scope="module")
def shipped(tmp_path_factory: pytest.TempPathFactory) -> Shipped:
    return Shipped(tmp_path_factory.mktemp("population"))


def _tenths(count: int, total: int) -> str:
    """A share as a percentage to one decimal, half to even, worked out on fractions."""
    scaled = round(Fraction(count * 1000, total))
    return f"{scaled // 10}.{scaled % 10}"


def _shares(counts: Dict[str, int], order: List[str]) -> List[str]:
    total = sum(counts[name] for name in order)
    return [_tenths(counts[name], total) for name in order]


def _tables(text: str) -> List[List[Tuple[str, str, str]]]:
    """Each chart's table of the section, as ``(cell, profile, run)`` rows."""
    found = []
    for block in text.split("<details><summary>Numbers</summary>")[1:]:
        table = block.split("</details>", 1)[0]
        found.append(
            [
                tuple(part.strip() for part in line.strip("|").split("|"))  # type: ignore[misc]
                for line in table.splitlines()
                if line.startswith("| ") and not line.startswith(("| cell", "| ---"))
            ]
        )
    return found


def test_each_scored_histogram_is_drawn_against_its_profile(shipped: Shipped) -> None:
    rows = shipped.rows()
    rating = Counter(str(row["rating"]) for row in rows)
    verdict = Counter(str(row["verdict"]) for row in rows)
    by_rating = {
        level: Counter(str(r["verdict"]) for r in rows if str(r["rating"]) == level)
        for level in "123"
    }
    levels, outcomes = ["1", "2", "3"], ["keep", "return"]
    profile_rating = dict(zip(levels, [300, 500, 200], strict=True))
    profile_verdict = dict(zip(outcomes, [490, 510], strict=True))
    profile_by_rating = {
        "1": dict(zip(outcomes, [60, 240], strict=True)),
        "2": dict(zip(outcomes, [250, 250], strict=True)),
        "3": dict(zip(outcomes, [180, 20], strict=True)),
    }

    expected: List[List[Tuple[str, str, str]]] = [
        list(zip(levels, _shares(profile_rating, levels), _shares(rating, levels), strict=True)),
        list(
            zip(
                outcomes,
                _shares(profile_verdict, outcomes),
                _shares(verdict, outcomes),
                strict=True,
            )
        ),
        [
            (f"g{ordinal}: {outcome}", p, r)
            for ordinal, level in enumerate(levels)
            for outcome, p, r in zip(
                outcomes,
                _shares(profile_by_rating[level], outcomes),
                _shares(by_rating[level], outcomes),
                strict=True,
            )
        ],
    ]
    assert _tables(shipped.population) == expected

    charts = re.findall(r"```mermaid\n(.*?)```", shipped.population, flags=re.DOTALL)
    assert len(charts) == 3
    for chart, table in zip(charts, expected, strict=True):
        assert chart.startswith("xychart-beta\n")
        assert f"  bar [{', '.join(p for _, p, _ in table)}]\n" in chart
        assert f"  line [{', '.join(r for _, _, r in table)}]\n" in chart
        top = max(float(value) for _, p, r in table for value in (p, r))
        assert f'  y-axis "share (percent)" 0 --> {-(-int(top * 10) // 100) * 10 or 10}\n' in chart
    assert 'title "review.rating: bars profile, line run"' in charts[0]
    assert 'title "review.verdict: bars profile, line run"' in charts[1]
    assert shipped.population.rstrip().endswith("Properties left out of the charts: 0 of 3.")


def test_the_scorecard_keeps_the_bytes_it_had_before_the_section(shipped: Shipped) -> None:
    assert shipped.run_id == RUN_ID
    written = (shipped.run_dir / SCORECARD_FILE).read_bytes()
    assert hashlib.sha256(written).hexdigest() == SCORECARD_SHA256
    assert b"cells" not in written and b"Population" not in written


def test_a_run_shipped_without_a_profile_has_nothing_to_draw(tmp_path: Path) -> None:
    state = tmp_path / "state"
    ran = run(FIXTURE_BUNDLE, state_dir=state)
    assert ran.success, ran.stderr
    run_id = ran.artifacts["plan"]["run_id"]
    # A committed slice of the run's own rows, so the check passes and the report is written.
    layout = RunLayout.for_state_dir(state, run_id)
    columns = json.loads(layout.seam_attestation_path("payment_recovery_moment").read_text())[
        "columns"
    ]
    emitted = layout.emit_path("payment_recovery_moment").read_text(encoding="utf-8").splitlines()
    slice_path = tmp_path / "slice.ndjson"
    slice_path.write_text(
        "".join(
            json.dumps(dict(zip(columns, json.loads(line), strict=True))) + "\n"
            for line in emitted
            if line
        ),
        encoding="utf-8",
    )
    shipped = ship(
        FIXTURE_BUNDLE,
        run_id=run_id,
        out_dir=tmp_path / "out",
        state_dir=state,
        committed_slice={"payment_recovery_moment": slice_path.as_uri()},
    )
    assert shipped.success, shipped.stderr
    text = (state / "runs" / run_id / "EVIDENCE.md").read_text(encoding="utf-8")
    section = text.split("## Population\n", 1)[1].split("\n## ", 1)[0].strip()
    assert section == "There is no target to draw: this run was shipped without a profile."


def test_a_cell_the_profile_withheld_is_left_out_of_the_chart_as_it_is_of_the_score(
    tmp_path: Path,
) -> None:
    withheld = Shipped(tmp_path, withheld_level=True)
    rating = Counter(str(row["rating"]) for row in withheld.rows())
    first = _tables(withheld.population)[0]
    kept = ["1", "3"]
    assert first == list(
        zip(
            kept,
            _shares({"1": 300, "3": 200}, kept),
            _shares(rating, kept),
            strict=True,
        )
    )
    assert 'x-axis ["1", "3"]' in withheld.population


@pytest.mark.parametrize("level", ["uncertified", "unimproves"])
def test_a_level_named_with_a_reserved_word_still_ships(tmp_path: Path, level: str) -> None:
    named = Shipped(tmp_path, level=level)
    text = (named.run_dir / "EVIDENCE.md").read_text(encoding="utf-8")
    assert level not in text
    # The verdict chart is still drawn, the level under a name that is not the data's.
    assert "| cell 2 |" in named.population
    assert "| keep |" in named.population


def _children_bundle(tmp_path: Path) -> Tuple[Path, Path]:
    """The counts-invoices bundle pinning a profile whose lines per invoice differ from the run's."""
    import shutil

    from flux_engine.core.digest import digest_of_document

    from .test_counts_invoice_book_e2e import FIXTURE, NAMESPACE
    from .test_counts_invoice_book_e2e import _profile as invoice_profile

    document = invoice_profile()
    statistic = document["relationships"][1]["statistics"][0]
    statistic["groups"][0]["binCounts"] = PROFILE_LINES
    statistic["sampleSize"] = {"min": sum(PROFILE_LINES), "max": sum(PROFILE_LINES)}
    statistic["groups"][0]["k"] = sum(PROFILE_LINES)
    profile = tmp_path / "invoices.profile.json"
    profile.write_text(json.dumps(document), encoding="utf-8")
    bundle = tmp_path / "invoices"
    shutil.copytree(FIXTURE, bundle)
    digest = digest_of_document(document, subject="p").value
    simulation = bundle / "simulation.flux.yml"
    simulation.write_text(
        simulation.read_text("utf-8").replace(
            NAMESPACE, NAMESPACE + f"    profile:\n      digest: {digest}\n"
        ),
        "utf-8",
    )
    return bundle, profile


def test_children_per_parent_the_scorecard_scores_are_drawn_and_counted(tmp_path: Path) -> None:
    bundle, profile = _children_bundle(tmp_path)
    state = tmp_path / ".flux"
    planned = plan(bundle, state_dir=state, profile=profile)
    assert planned.success, planned.stderr
    run_id = str(planned.artifacts["run_id"])
    applied = apply(bundle, state_dir=state, run_id=run_id, auto_plan=False)
    assert applied.success, applied.stderr
    verified = verify(
        bundle, run_id=run_id, state_dir=state, profile=profile, allow_declared_baseline_drift=True
    )
    assert verified.success, verified.stderr
    shipped = ship(
        bundle, run_id=run_id, out_dir=tmp_path / "out", state_dir=state, profile=profile
    )
    assert shipped.success, shipped.stderr
    run_dir = state / "runs" / run_id
    scorecard = json.loads((run_dir / SCORECARD_FILE).read_text(encoding="utf-8"))
    rows = [row for table in scorecard["tables"] for row in table["properties"]]
    by_id = {row["propertyId"]: row for row in rows if row["property"] == "childrenPerParent"}
    assert sorted(by_id) == ["children:invoice.account", "children:invoice_line.invoice"]
    lines = by_id["children:invoice_line.invoice"]
    assert (lines["outcome"], by_id["children:invoice.account"]["outcome"]) == ("outside", "within")
    text = (run_dir / "EVIDENCE.md").read_text(encoding="utf-8")
    section = text.split("## Population\n", 1)[1].split("\n## ", 1)[0]

    for property_id, row in by_id.items():
        assert f'title "{property_id}: bars profile, line run"' in section
        bins = row["bins"]
        cells = [f"{bins[i]} to {bins[i + 1]}" for i in range(len(bins) - 1)]
        assert [
            (cell, _tenths(p, row["profile"]["parents"]), _tenths(r, row["run"]["parents"]))
            for cell, p, r in zip(
                cells, row["profile"]["binCounts"], row["run"]["binCounts"], strict=True
            )
        ] in _tables(section)
    assert (
        f"children:invoice_line.invoice: score {lines['score']}, outside its tolerance "
        f"(distance {lines['distance']}, tolerance {lines['tolerance']})."
    ) in section
    assert "No property scores children per parent" not in text
    # Every scored row is counted against the scorecard's own total; the two charts are drawn.
    assert section.count("```mermaid") == 2
    assert len(rows) > 2
    assert section.rstrip().endswith(
        f"Properties left out of the charts: {len(rows) - 2} of {len(rows)}."
    )
