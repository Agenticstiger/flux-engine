# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The children-per-parent row of a relationship laid out given a parent column (ADR 0037 decision 7).

The bundle is ``tests/fixtures/exact-counts`` whose invoices per account are given the account's
``segment`` (``test_counts_given_run``). A profile that holds a ``marginalHistogram/1`` of invoices per
account grouped by ``segment``, or by the bins of a column, makes the row a grouped one: the run's
parents of each class are counted into the profile's bins by their number of invoices and each class
is compared with the profile's group by total variation distance. What is pinned, end to end through
``apply`` and ``verify``:

* **Within and outside.** A profile built from the run is within in every class and names scorecard
  format 1-5-0, valid against its schema and not against 1-4-0; a class whose counts the profile has
  swapped is outside, by ordinal, and the others are not. The ``counts`` entry's tolerance decides.
* **Withheld.** A class the profile withheld is censored and the others are still scored.
* **Without it.** A profile with only the ungrouped histogram, or one grouped by another column,
  leaves the ungrouped row and the format it named before.
* **Noise.** A noisy reference widens each class's margin by its own ``w``, so a class a small noisy
  profile would leave outside is within, and a class that is far off is still outside.
* **Bins.** A relationship given a column drawn from bins is scored per bin of the profile's own
  edges, named by ordinal in the order of the bins.
"""

from __future__ import annotations

import copy
import json
import shutil
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest
from jsonschema import Draft202012Validator

from flux_engine.core.digest import digest_of_document
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages import pipeline
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.verify import verify as run_verify
from flux_stats.profile import SCHEMA_URL_1_4_0
from flux_stats.reading import MS_PER_DAY, parse_date_days

from .test_counts_given_refusals import _binned_bundle
from .test_counts_given_run import CLASSES, _per_account, _table, given_bundle
from .test_counts_run import bundle_with
from .test_counts_scorecard import NAMESPACE, SCHEMA_1_3, SCHEMA_1_4

pytestmark = pytest.mark.integration

SPEC = Path(__file__).resolve().parents[2] / "docs" / "spec"
SCHEMA_1_5 = json.loads((SPEC / "scorecard-1-5.schema.json").read_text(encoding="utf-8"))
EXAMPLE = json.loads((SPEC / "profile-1-4-follows-example.json").read_text(encoding="utf-8"))

#: Invoices per account fall in three bins of the profile: up to two, three or four, five or more.
BINS = ["0", "3", "5", "8"]
ROW = "children:invoice.account"


def _binned(spread: Sequence[int]) -> List[int]:
    counts = [0, 0, 0]
    for children in spread:
        counts[0 if children < 3 else 1 if children < 5 else 2] += 1
    return counts


def _counts_of(bundle: Path) -> Dict[str, List[int]]:
    """The run's accounts of each segment counted into the profile's bins by their invoices."""
    outcome = pipeline.execute(bundle, kernel=KERNEL_V2)
    held: Dict[str, List[int]] = {name: [] for name in CLASSES}
    for row, number in zip(_table(outcome, "account"), _per_account(outcome), strict=True):
        held[row["segment"]].append(number)
    return {name: _binned(spread) for name, spread in held.items()}


@pytest.fixture(scope="module")
def counts(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, List[int]]:
    return _counts_of(given_bundle(tmp_path_factory.mktemp("counts")))


def _toleranced(tmp_path: Path, name: str, tolerance: str) -> Path:
    """The bundle whose entry declares a tolerance (an edit of the entry, so another run)."""
    return given_bundle(tmp_path, name, extra=f', tolerance: "{tolerance}"')


def _exact_group(key: str, binned: Sequence[int]) -> Dict[str, Any]:
    return {"group": key, "k": sum(binned), "binCounts": list(binned), "status": "F"}


def _exact_profile(
    counts: Dict[str, List[int]],
    *,
    swap: Sequence[str] = (),
    withhold: Sequence[str] = (),
    group_by: str = "segment",
    ungrouped: bool = False,
) -> Dict[str, Any]:
    groups = []
    for key in sorted(counts):
        held = counts[key]
        if key in swap:
            held = held[::-1]
        group = _exact_group(key, held)
        if key in withhold:
            group = {"group": key, "status": "S"}
        groups.append(group)
    statistic: Dict[str, Any] = {
        "definition": "marginalHistogram/1",
        "bins": BINS,
        "sampleSize": {"min": 300, "max": 300},
        "disclosure": "none",
        "groups": groups,
    }
    if ungrouped:
        total = [sum(column) for column in zip(*counts.values(), strict=True)]
        statistic["groups"] = [_exact_group("", total)]
    else:
        statistic["groupBy"] = group_by
    column = {"domain": "category"}
    return {
        "_producer": "urn:example:test-profiler",
        "_schemaURL": SCHEMA_URL_1_4_0,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}},
        "tables": {
            "account": {"columns": {"account_id": column, "segment": column, "status": column}},
            "invoice": {"columns": {"account_id": column}},
        },
        "relationships": [
            {
                "parent": {"table": "account", "columns": ["account_id"]},
                "child": {"table": "invoice", "columns": ["account_id"]},
                "statistics": [statistic],
            }
        ],
    }


def _scorecard(
    tmp_path: Path,
    profile: Dict[str, Any],
    *,
    bundle: Optional[Path] = None,
) -> Dict[str, Any]:
    target = bundle if bundle is not None else given_bundle(tmp_path, "bundle")
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile), encoding="utf-8")
    simulation = target / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    digest = digest_of_document(profile, subject="p").value
    text = text.replace(NAMESPACE, NAMESPACE + f"    profile:\n      digest: {digest}\n", 1)
    simulation.write_text(text, "utf-8")
    state = tmp_path / ".flux"
    applied = apply(target, state_dir=state, profile=path)
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(state, str(applied.artifacts["run_id"]))
    result = run_verify(
        target,
        run_id=layout.run_id,
        state_dir=state,
        profile=path,
        allow_declared_baseline_drift=True,
    )
    document = result.artifacts["scorecard"]["document"]
    assert isinstance(document, dict)
    return document


def _row(document: Dict[str, Any], property_id: str = ROW) -> Dict[str, Any]:
    (found,) = (
        row
        for table in document["tables"]
        for row in table["properties"]
        if row["propertyId"] == property_id
    )
    return found


def _valid_1_5(document: Dict[str, Any]) -> None:
    assert document["_schemaURL"] == SCHEMA_1_5["properties"]["_schemaURL"]["const"]
    Draft202012Validator(SCHEMA_1_5).validate(document)
    assert list(Draft202012Validator(SCHEMA_1_4).iter_errors(document))


def test_a_run_given_a_column_is_scored_per_class_and_names_format_1_5_0(
    tmp_path: Path, counts: Dict[str, List[int]]
) -> None:
    document = _scorecard(tmp_path, _exact_profile(counts))
    _valid_1_5(document)
    row = _row(document)
    assert row["outcome"] == "within" and row["groupBy"] == "segment"
    assert row["distance"] == "0" and row["score"] == "1" and "noise" not in row
    assert [group["ordinal"] for group in row["groups"]] == [0, 1, 2]
    for group, key in zip(row["groups"], sorted(counts), strict=True):
        assert group["run"] == {"parents": sum(counts[key]), "binCounts": counts[key]}
        assert group["profile"] == group["run"] and group["outcome"] == "within"
        assert "widening" not in group
    assert "retail" not in json.dumps(row) and "trade" not in json.dumps(row)


def test_a_class_the_profile_holds_otherwise_is_outside_and_the_others_are_not(
    tmp_path: Path, counts: Dict[str, List[int]]
) -> None:
    row = _row(_scorecard(tmp_path, _exact_profile(counts, swap=["retail"])))
    assert row["outcome"] == "outside"
    names = sorted(counts)
    outcomes = {names[group["ordinal"]]: group["outcome"] for group in row["groups"]}
    assert outcomes == {"public": "within", "retail": "outside", "trade": "within"}
    reversed_ = counts["retail"][::-1]
    expected = Fraction(
        sum(abs(a - b) for a, b in zip(counts["retail"], reversed_, strict=True)),
        2 * sum(counts["retail"]),
    )
    assert Fraction(row["distance"]) == expected and expected > Fraction(1, 10)


def test_the_entrys_tolerance_decides_within_or_outside(tmp_path: Path) -> None:
    rows: Dict[str, Dict[str, Any]] = {}
    for name, tolerance in (("tight", "0.01"), ("wide", "0.99")):
        bundle = _toleranced(tmp_path, f"{name}-bundle", tolerance)
        profile = _exact_profile(_counts_of(bundle), swap=["retail"])
        rows[name] = _row(_scorecard(tmp_path / name, profile, bundle=bundle))
    assert rows["tight"]["outcome"] == "outside" and rows["tight"]["tolerance"] == "0.01"
    assert rows["wide"]["outcome"] == "within" and rows["wide"]["tolerance"] == "0.99"
    assert rows["wide"]["distance"] != "0"


def test_a_class_the_profile_withheld_is_censored_and_the_others_are_scored(
    tmp_path: Path, counts: Dict[str, List[int]]
) -> None:
    row = _row(_scorecard(tmp_path, _exact_profile(counts, withhold=["public"])))
    first, *rest = row["groups"]
    assert first["outcome"] == "censored" and first["profile"] is None
    assert first["distance"] is None and first["run"]["parents"] == sum(counts["public"])
    assert row["outcome"] == "within" and all(group["outcome"] == "within" for group in rest)


def test_a_profile_with_only_the_ungrouped_histogram_leaves_the_row_and_the_format(
    tmp_path: Path, counts: Dict[str, List[int]]
) -> None:
    document = _scorecard(tmp_path, _exact_profile(counts, ungrouped=True))
    assert document["_schemaURL"] == SCHEMA_1_4["properties"]["_schemaURL"]["const"]
    row = _row(document)
    assert "groups" not in row and row["run"]["parents"] == 300
    assert not list(Draft202012Validator(SCHEMA_1_4).iter_errors(document))
    assert list(Draft202012Validator(SCHEMA_1_3).iter_errors(document))


def test_a_profile_grouped_by_another_column_is_not_the_given_columns_row(
    tmp_path: Path, counts: Dict[str, List[int]]
) -> None:
    document = _scorecard(tmp_path, _exact_profile(counts, group_by="status"))
    assert not any(
        row["propertyId"] == ROW for table in document["tables"] for row in table["properties"]
    )


def test_a_bundle_without_given_scores_a_grouped_profile_as_it_always_did(
    tmp_path: Path, counts: Dict[str, List[int]]
) -> None:
    document = _scorecard(tmp_path, _exact_profile(counts), bundle=bundle_with(tmp_path, "plain"))
    assert not any(
        row["propertyId"] == ROW for table in document["tables"] for row in table["properties"]
    )


# -- a noisy reference ---------------------------------------------------------------------


def _noisy_profile(counts: Dict[str, List[int]], shift: Dict[str, List[int]]) -> Dict[str, Any]:
    """The profile of ``counts`` released under Gaussian noise of scale 1, each cell moved by ``shift``."""
    document = copy.deepcopy(EXAMPLE)
    document["privacy"]["privacyLoss"] = {"rho": "1000"}
    document["privacy"]["privacyUnit"] = {"expose": "account"}
    document["tables"]["account"]["columns"]["account_id"] = {"domain": "category"}
    document["tables"]["account"]["columns"]["segment"]["statistics"][0]["levels"] = sorted(counts)
    document["tables"]["account"]["columns"]["segment"]["statistics"][0]["groups"] = [
        {"group": "", "status": "F", "noisyCounts": ["100", "100", "100"]}
    ]
    document["tables"]["invoice"]["columns"] = {"account_id": {"domain": "category"}}
    groups = []
    for key in sorted(counts):
        cells = [str(value + move) for value, move in zip(counts[key], shift[key], strict=True)]
        groups.append({"group": key, "status": "F", "noisyCounts": cells})
    document["relationships"] = [
        {
            "parent": {"table": "account", "columns": ["account_id"]},
            "child": {"table": "invoice", "columns": ["account_id"]},
            "statistics": [
                {
                    "definition": "marginalHistogram/1",
                    "bins": BINS,
                    "groupBy": "segment",
                    "sampleSize": {"min": 100, "max": 999},
                    "disclosure": "noisy",
                    "noise": {
                        "mechanism": "gaussian",
                        "scale": "1",
                        "privacyLoss": {"rho": "0.5"},
                    },
                    "groups": groups,
                }
            ],
        }
    ]
    return document


def _shifted(counts: Dict[str, List[int]], moved: Dict[str, List[int]]) -> Dict[str, List[int]]:
    return {
        key: [value + move for value, move in zip(held, moved[key], strict=True)]
        for key, held in counts.items()
    }


def test_a_noisy_reference_widens_each_class_by_its_own_reach(tmp_path: Path) -> None:
    bundle = _toleranced(tmp_path, "zero-bundle", "0")
    counts = _counts_of(bundle)
    moved = {name: [2, -2, 0] for name in counts}
    # The same cells as an exact reference are outside a tolerance of 0 ...
    exact = _scorecard(
        tmp_path / "exact", _exact_profile(_shifted(counts, moved)), bundle=copy_of(bundle)
    )
    assert _row(exact)["outcome"] == "outside"
    # ... and within it when the profile was released under noise, whose reach widens the margin.
    document = _scorecard(tmp_path / "noisy", _noisy_profile(counts, moved), bundle=bundle)
    _valid_1_5(document)
    noisy = _row(document)
    assert noisy["outcome"] == "within"
    assert noisy["noise"]["mechanism"] == "gaussian" and noisy["noise"]["cellVariance"] == "1"
    names = sorted(counts)
    widenings = {names[g["ordinal"]]: Fraction(g["widening"]) for g in noisy["groups"]}
    assert all(widening > 0 for widening in widenings.values())
    # the class with fewer accounts has the wider margin: the reach is each class's own
    assert widenings["public"] > widenings["retail"]
    assert Fraction(noisy["noise"]["widening"]) == max(widenings.values())
    assert all(
        0 < Fraction(g["distance"]) <= widenings[names[g["ordinal"]]] for g in noisy["groups"]
    )


def copy_of(bundle: Path) -> Path:
    """A second copy of *bundle*, for a run that pins another profile."""
    target = bundle.parent / f"{bundle.name}-copy"
    shutil.copytree(bundle, target)
    return target


def test_a_class_that_is_far_from_a_noisy_reference_is_outside_whatever_the_noise(
    tmp_path: Path,
) -> None:
    bundle = _toleranced(tmp_path, "zero-bundle", "0")
    counts = _counts_of(bundle)
    far = {name: [0, 0, 0] for name in counts}
    far["retail"] = [60, -20, -40]
    row = _row(_scorecard(tmp_path / "far", _noisy_profile(counts, far), bundle=bundle))
    outcomes = {sorted(counts)[g["ordinal"]]: g["outcome"] for g in row["groups"]}
    assert outcomes["retail"] == "outside" and outcomes["public"] == "within"
    assert row["outcome"] == "outside"


# -- bins of the parent column ----------------------------------------------------------------

EDGES = ("2026-07-01", "2026-08-01", "2026-09-01", "2026-09-30")


def _edge_ms(text: str) -> int:
    return parse_date_days(text) * MS_PER_DAY


def test_a_relationship_given_a_binned_column_is_scored_per_bin_of_the_profiles_edges(
    tmp_path: Path,
) -> None:
    lists = '{ "2026-07-01": ["1", "0", "0"], "2026-09-01": ["0", "0", "1"] }'
    target = _binned_bundle(tmp_path, lists)
    outcome = pipeline.execute(target, kernel=KERNEL_V2)
    invoice, lines = outcome.emit("invoice"), outcome.emit("invoice_line")
    names, line_names = list(invoice.expose.column_names), list(lines.expose.column_names)
    held: Dict[Any, int] = {}
    for row in lines.rows:
        key = row[line_names.index("invoice_id")]
        held[key] = held.get(key, 0) + 1
    spreads: Dict[str, Dict[int, int]] = {edge: {} for edge in EDGES[:-1]}
    for row in invoice.rows:
        issued = str(row[names.index("issued_on")])
        edge = EDGES[0] if issued < EDGES[1] else EDGES[1] if issued < EDGES[2] else EDGES[2]
        number = held.get(row[names.index("invoice_id")], 0)
        spreads[edge][number] = spreads[edge].get(number, 0) + 1
    line_bins = ["1", "2", "4", "7"]

    def binned(spread: Dict[int, int]) -> List[int]:
        found = [0, 0, 0]
        for number, parents in spread.items():
            found[0 if number < 2 else 1 if number < 4 else 2] += parents
        return found

    keys = [str(_edge_ms(edge)) for edge in EDGES]
    groups = []
    for index, edge in enumerate(EDGES[:-1]):
        cells = binned(spreads[edge])
        groups.append(
            {"group": keys[index], "k": sum(cells), "binCounts": cells, "status": "F"}
            if sum(cells)
            else {"group": keys[index], "status": "S"}
        )
    column = {"domain": "category"}
    profile = {
        "_producer": "urn:example:test-profiler",
        "_schemaURL": SCHEMA_URL_1_4_0,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}},
        "tables": {
            "invoice": {"columns": {"invoice_id": column, "issued_on": {"domain": "timestamp"}}},
            "invoice_line": {"columns": {"invoice_id": column}},
        },
        "relationships": [
            {
                "parent": {"table": "invoice", "columns": ["invoice_id"]},
                "child": {"table": "invoice_line", "columns": ["invoice_id"]},
                "statistics": [
                    {
                        "definition": "marginalHistogram/1",
                        "bins": line_bins,
                        "groupByBins": {
                            "column": "issued_on",
                            "bins": [str(_edge_ms(e)) for e in EDGES],
                        },
                        "sampleSize": {"min": 1200, "max": 1200},
                        "disclosure": "none",
                        "groups": groups,
                    }
                ],
            }
        ],
    }
    document = _scorecard(tmp_path, profile, bundle=target)
    _valid_1_5(document)
    row = _row(document, "children:invoice_line.invoice")
    assert row["groupByBins"]["column"] == "issued_on"
    assert [group["ordinal"] for group in row["groups"]] == list(range(len(row["groups"])))
    assert row["outcome"] == "within" and row["distance"] == "0"
    assert sum(group["run"]["parents"] for group in row["groups"]) == len(invoice.rows)
