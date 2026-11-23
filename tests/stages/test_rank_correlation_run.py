# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``plan``, ``apply`` and ``verify`` of rank correlations on a copula's rows (ADR 0035 decision 7).

``tests/fixtures/copula`` is planned with a copula of one Kendall pair and scored against a profile
that holds a Kendall tau and a Spearman rho of the same pair. The profile's values are formed here
from the rows the first run wrote, by brute force over every pair of them, then pinned in a second
copy of the bundle; the same seed draws the same rows, which the test checks before it relies on it.
Exact and noisy references are both scored, and a profile that states another correlation than the
copula draws is outside.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from fractions import Fraction
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import pytest
from jsonschema import Draft202012Validator

from flux_engine.core.digest import digest_of_document
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.verify import verify as run_verify
from flux_stats.profile import SCHEMA_URL_1_4_0
from tests.seam.test_copula_block import bundle_with

pytestmark = pytest.mark.integration

SPEC = Path(__file__).resolve().parents[2] / "docs" / "spec"
ROWS = 1200
GRID = 1 << 16
NAMESPACE = "  com.agentics.flux/1:\n"
MEAN = (
    "            - property: mean\n"
    "              column: amount\n"
    '              tolerance: { type: absolute, value: "1000" }\n'
)
COPULA = (
    "copula:\n  columns: [amount, balance]\n  rankCorrelations:\n    measure: kendall\n"
    "    pairs: [ { columns: [amount, balance], value: '0.5' } ]\n"
)
PROPERTIES = (
    "            - { property: rankCorrelation, columns: [amount, balance], measure: kendall }\n"
    "            - { property: rankCorrelation, columns: [balance, amount], measure: spearman,"
    " tolerance: { type: absolute, max: '0.08' } }\n"
)


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


def _units(text: Any) -> int:
    return math.floor(Fraction(Decimal(str(text))) * GRID)


def _mid_ranks(values: Sequence[int]) -> List[Fraction]:
    ordered = sorted(values)
    first: Dict[int, int] = {}
    count: Dict[int, int] = {}
    for position, value in enumerate(ordered):
        first.setdefault(value, position)
        count[value] = count.get(value, 0) + 1
    return [Fraction(2 * first[v] + count[v] + 1, 2) for v in values]


def kendall_s(pairs: Sequence[Tuple[int, int]]) -> int:
    return sum(_sign(a[0] - b[0]) * _sign(a[1] - b[1]) for a, b in combinations(pairs, 2))


def spearman_a4(pairs: Sequence[Tuple[int, int]]) -> int:
    n = len(pairs)
    left, right = _mid_ranks([p[0] for p in pairs]), _mid_ranks([p[1] for p in pairs])
    centre = Fraction(n + 1, 2)
    total = 4 * sum((r - centre) * (q - centre) for r, q in zip(left, right, strict=True))
    assert total.denominator == 1
    return int(total)


def _written(layout: RunLayout, expose_id: str, names: Sequence[str]) -> List[List[Any]]:
    header = json.loads(layout.seam_attestation_path(expose_id).read_text("utf-8"))["columns"]
    at = [header.index(name) for name in names]
    return [
        [json.loads(line)[i] for i in at]
        for line in layout.emit_path(expose_id).read_text("utf-8").splitlines()
    ]


def _bundle(tmp_path: Path, name: str, copula: str = COPULA) -> Path:
    bundle = bundle_with(tmp_path, copula, name)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    assert text.count("        rows: 400\n") == 1 and text.count(MEAN) == 1
    simulation.write_text(
        text.replace("        rows: 400\n", f"        rows: {ROWS}\n").replace(MEAN, PROPERTIES),
        "utf-8",
    )
    return bundle


def _statistic(definition: str, **fields: Any) -> Dict[str, Any]:
    return {"definition": definition, "sampleSize": {"min": 1}, "disclosure": "none", **fields}


def _noisy(definition: str, value: str, scale: str) -> Dict[str, Any]:
    return _statistic(
        definition,
        rowFloor=ROWS,
        disclosure="noisy",
        noise={"mechanism": "gaussian", "scale": scale, "privacyLoss": {"rho": "1"}},
        groups=[{"group": "", "status": "F", "noisyValue": value}],
    )


def _profile(statistics: List[Dict[str, Any]]) -> Dict[str, Any]:
    document: Dict[str, Any] = {
        "_producer": "urn:example:test-profiler",
        "_schemaURL": SCHEMA_URL_1_4_0,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}, "noisy": {}},
        "tables": {
            "accounts": {
                "columns": {"amount": {"domain": "number"}, "balance": {"domain": "number"}},
                "columnPairs": [{"columns": ["balance", "amount"], "statistics": statistics}],
            }
        },
    }
    if any("noise" in each for each in statistics):
        document["privacy"] = {
            "measure": "zeroConcentratedDP",
            "privacyLoss": {"rho": "100"},
            "privacyUnit": {"expose": "accounts"},
        }
    return document


class Pinned:
    """The fixture applied plain, a profile built by *shape* from its rows, and applied again under it."""

    def __init__(self, tmp_path: Path, shape: Any, copula: str = COPULA) -> None:
        plain = _bundle(tmp_path, "plain", copula)
        planned = apply(plain, state_dir=tmp_path / "state-plain")
        assert planned.success, planned.stderr
        layout = RunLayout.for_state_dir(tmp_path / "state-plain", str(planned.artifacts["run_id"]))
        written = _written(layout, "accounts", ["amount", "balance"])
        self.pairs = [(_units(a), _units(b)) for a, b in written]
        self.kendall = Fraction(kendall_s(self.pairs), len(self.pairs) * (len(self.pairs) - 1) // 2)
        n = len(self.pairs)
        self.spearman = Fraction(3 * spearman_a4(self.pairs), n * (n * n - 1))
        document = _profile(shape(self))
        self.profile = tmp_path / "profile.json"
        self.profile.write_text(json.dumps(document), "utf-8")
        self.bundle = _bundle(tmp_path, "pinned", copula)
        simulation = self.bundle / "simulation.flux.yml"
        pin = digest_of_document(document, subject="profile").value
        simulation.write_text(
            simulation.read_text("utf-8").replace(
                NAMESPACE, f"{NAMESPACE}    profile:\n      digest: {pin}\n", 1
            ),
            "utf-8",
        )
        self.state = tmp_path / "state"
        applied = apply(self.bundle, state_dir=self.state, profile=self.profile)
        assert applied.success, applied.stderr
        self.layout = RunLayout.for_state_dir(self.state, str(applied.artifacts["run_id"]))
        assert _written(self.layout, "accounts", ["amount", "balance"]) == written

    def verify(self) -> Any:
        return run_verify(
            self.bundle,
            run_id=self.layout.run_id,
            state_dir=self.state,
            profile=self.profile,
            allow_declared_baseline_drift=True,
        )

    def rows(self, result: Any) -> Dict[str, Any]:
        (table,) = result.artifacts["scorecard"]["document"]["tables"]
        return {row["propertyId"]: row for row in table["properties"]}


def _exact(case: Pinned) -> List[Dict[str, Any]]:
    # The profile names the pair the other way round, as a profiler may: both are symmetric.
    flipped = [(b, a) for a, b in case.pairs]
    return [
        _statistic(
            "kendallTau/1",
            groups=[
                {"group": "", "k": ROWS, "S": str(kendall_s(flipped)), "status": "F"},
            ],
        ),
        _statistic(
            "spearmanRho/1",
            groups=[{"group": "", "k": ROWS, "A4": str(spearman_a4(flipped)), "status": "F"}],
        ),
    ]


def test_a_run_that_matches_an_exact_profile_is_within_and_scores_one(tmp_path: Path) -> None:
    case = Pinned(tmp_path, _exact)
    result = case.verify()
    assert result.success, result.stderr
    kendall, spearman = case.rows(result).values()
    assert kendall["property"] == "rankCorrelation" and kendall["statistic"] == "kendallTau/1"
    assert spearman["statistic"] == "spearmanRho/1"
    for row in (kendall, spearman):
        assert row["outcome"] == "within" and row["score"] == "1" and row["dimension"] == "joint"
        assert row["rank"]["distance"] == "0" and row["rank"]["records"] == ROWS
        assert row["rank"]["tooNoisyToTell"] is False and "noise" not in row
    # The two numbers of the rows are the ones formed here by hand, over every pair of rows.
    assert Fraction(kendall["rank"]["run"]) == case.kendall
    assert Fraction(spearman["rank"]["run"]) == case.spearman
    assert Fraction(kendall["rank"]["samplingWidening"]) > 0
    assert kendall["tolerance"] == {"max": "0.05", "type": "absolute"}
    assert spearman["tolerance"] == {"max": "0.08", "type": "absolute"}


def test_the_copulas_shrinkage_is_on_the_row_and_the_scorecard_names_1_5_0(tmp_path: Path) -> None:
    case = Pinned(tmp_path, _exact)
    result = case.verify()
    document = result.artifacts["scorecard"]["document"]
    assert document["_schemaURL"].endswith("/scorecard/1-5-0/FluxScorecard.json")
    plan_alpha = json.loads(case.layout.plan_path.read_text("utf-8"))["emits"]["accounts"][
        "copula"
    ]["alpha"]
    for row in case.rows(result).values():
        assert row["rank"]["alpha"] == plan_alpha
    schema = json.loads((SPEC / "scorecard-1-5.schema.json").read_text("utf-8"))
    Draft202012Validator(schema).validate(document)


def test_the_tau_a_copula_draws_is_near_the_declared_one(tmp_path: Path) -> None:
    case = Pinned(tmp_path, _exact)
    # The copula declares 0.5 and the fixture's histograms put many values in few bins, so the tau
    # of the grid values lies below it: the run is read, not tuned to the declaration.
    assert 0.2 < case.kendall < 0.55


def test_a_noisy_reference_near_the_run_is_within_and_carries_its_noise(tmp_path: Path) -> None:
    def shape(case: Pinned) -> List[Dict[str, Any]]:
        return [
            _noisy("kendallTau/1", str(float(case.kendall) + 0.02), "0.01"),
            _noisy("spearmanRho/1", str(float(case.spearman) - 0.03), "0.02"),
        ]

    case = Pinned(tmp_path, shape)
    result = case.verify()
    assert result.success, result.stderr
    kendall, spearman = case.rows(result).values()
    assert kendall["noise"] == {
        "mechanism": "gaussian",
        "cellVariance": "0.0001",
        "widening": "0.031622776602",
    }
    assert spearman["noise"]["cellVariance"] == "0.0004"
    assert Fraction(kendall["rank"]["distance"]) == pytest.approx(0.02, abs=1e-3)
    assert kendall["outcome"] == "within" and spearman["outcome"] == "within"
    assert 0 < Fraction(kendall["score"]) < 1


def test_a_profile_that_states_another_correlation_than_the_run_draws_is_outside(
    tmp_path: Path,
) -> None:
    def shape(case: Pinned) -> List[Dict[str, Any]]:
        return [_noisy("kendallTau/1", "-0.4", "0.01"), _noisy("spearmanRho/1", "0.95", "0.01")]

    case = Pinned(tmp_path, shape)
    result = case.verify()
    assert not result.success
    kendall, spearman = case.rows(result).values()
    assert kendall["outcome"] == "outside" and spearman["outcome"] == "outside"
    assert kendall["groups"] == {"scored": 1, "censored": 0, "outside": [0]}
    assert Fraction(kendall["score"]) < Fraction(3, 4)
    assert "rankCorrelation of amount, balance" in result.stderr


def test_a_reference_too_noisy_to_tell_says_so_and_cannot_fail_the_run(tmp_path: Path) -> None:
    def shape(case: Pinned) -> List[Dict[str, Any]]:
        return [_noisy("kendallTau/1", "-0.4", "1"), _noisy("spearmanRho/1", "0.95", "1")]

    case = Pinned(tmp_path, shape)
    result = case.verify()
    assert result.success, result.stderr
    for row in case.rows(result).values():
        assert row["outcome"] == "within" and row["rank"]["tooNoisyToTell"] is True
        assert Fraction(row["rank"]["reach"]) >= 2


def test_a_committed_slice_names_the_property_and_compares_nothing(tmp_path: Path) -> None:
    case = Pinned(tmp_path, _exact)
    columns = json.loads(case.layout.seam_attestation_path("accounts").read_text("utf-8"))[
        "columns"
    ]
    rows = [
        dict(zip(columns, json.loads(line), strict=True))
        for line in case.layout.emit_path("accounts").read_text("utf-8").splitlines()
    ]
    target = tmp_path / "slice.ndjson"
    target.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    result = run_verify(
        case.bundle,
        run_id=case.layout.run_id,
        state_dir=case.state,
        profile=case.profile,
        committed_slice={"accounts": target.as_uri()},
        allow_declared_baseline_drift=True,
    )
    assert result.success, result.stderr
    leg = result.artifacts["fidelity"]["accounts"]
    assert leg["notCompared"] == [
        "000:rankCorrelation:amount+balance",
        "001:rankCorrelation:balance+amount",
    ]
    assert leg["status"] == "not scored"
    assert "not compared with a committed slice" in leg["detail"]
    # The comparison with the pinned profile is still made, and still scores both rows.
    assert {row["outcome"] for row in case.rows(result).values()} == {"within"}
