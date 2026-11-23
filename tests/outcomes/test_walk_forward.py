# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A forecast for the next state is scored against it and shipped, over three states (ADR 0029 section 7).

The three profile states of ``tests/fixtures/outcomes/states`` are releases of one request. The
walk forward runs the real command line twice, for ``k`` of 1 and 2: the bundle pinned to state
``k`` makes its forecasts (``fluxsim forecast make``, registered at ``T_k``), the forecasts are
scored against state ``k + 1`` (``fluxsim forecast score --next``, observed at ``T_(k+1)``, later
than ``T_k``), and the run is shipped (``fluxsim ship``). State 3 drifts from state 2 in the premium
plan and in no other, so the first pass leaves its calibration current and the second flags it
stale.

Pinned: every forecast of both passes has a verdict and a row in ``EVIDENCE.md`` and in
``forecast-accuracy.json``; each row's state digest, forecast and interval, observation, result,
interval score, absolute error and drift, the observation read back from the next state's own
profile and the scores recomputed from it; the stale flag of the drifted pass only; the count of
forecasts on a state; the measured interval coverage beside the Playback's declared one; the grades
and the weakest; no banned word; and the order rule, that scoring with an observation time before
the registration time is refused (``FLX-7001``) and writes nothing.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import pytest
import yaml

from flux_engine.accuracy.store import AccuracyStore
from flux_engine.cli import main
from flux_engine.evidence.report import require_no_banned_words
from flux_engine.seam.profile import load_profile
from tests.outcomes.support import BUNDLE, FIXTURE, NAMESPACE

pytestmark = pytest.mark.integration

STATES = FIXTURE / "states"

#: When each state was read. A forecast on state ``k`` is registered at ``T_k``, and state ``k + 1``
#: is observed at ``T_(k+1)``, after it.
INSTANTS = {
    1: "2026-07-01T00:00:00Z",
    2: "2026-10-01T00:00:00Z",
    3: "2027-01-01T00:00:00Z",
}
#: The last instant before the next state is read, as ``--horizon-end`` states it.
HORIZONS = {1: "2026-09-30T23:59:59Z", 2: "2026-12-31T23:59:59Z"}

#: ``alpha`` of the sweep's interval at the declared confidence of 0.9.
ALPHA = Decimal("0.1")
#: Exact fractions are written to 28 places; the recomputation here differs in the last of them.
TOLERANCE = Decimal("1e-24")


@dataclass
class Pass:
    """What one pass of the walk left behind."""

    k: int
    state: Path
    pinned_digest: str
    next_digest: str
    evidence: str
    filed: Dict[str, Any]
    rows: List[List[str]]


def state_profile(k: int) -> Path:
    return STATES / f"state-{k}.profile.json"


def digest_of(k: int) -> str:
    return str(load_profile(state_profile(k)).digest.value)


def pin(tmp_path: Path, k: int) -> Path:
    """A copy of the fixture bundle pinned to state *k*, as ``fluxsim init`` pins a profile."""
    bundle = tmp_path / f"bundle-state-{k}"
    shutil.copytree(BUNDLE, bundle, dirs_exist_ok=True)
    path = bundle / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["extensions"][NAMESPACE]["profile"]["digest"] = digest_of(k)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return bundle


def cli(*arguments: str) -> Tuple[int, str, str]:
    """The exit status, standard output and standard error of one ``fluxsim`` command."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(arguments))
    return code, out.getvalue(), err.getvalue()


def cli_ok(*arguments: str) -> str:
    code, out, err = cli(*arguments)
    assert code == 0, (arguments[:2], err, out)
    return out


def section_rows(text: str) -> List[List[str]]:
    section = text.split("## Forecast Accuracy")[1].split("\n## ")[0]
    return [
        [cell.strip() for cell in line.strip().strip("|").split(" | ")]
        for line in section.splitlines()
        if line.startswith("| `")
    ]


def walk_pass(tmp_path: Path, state: Path, k: int) -> Pass:
    """``forecast make``, ``forecast score --next`` and ``ship`` of the bundle pinned to state *k*."""
    bundle = pin(tmp_path, k)
    pinned, following = state_profile(k), state_profile(k + 1)
    common = ["--state-dir", str(state)]
    cli_ok(
        "forecast",
        "make",
        str(bundle),
        "--profile",
        str(pinned),
        "--registered-at",
        INSTANTS[k],
        "--horizon-end",
        HORIZONS[k],
        *common,
    )
    cli_ok(
        "forecast",
        "score",
        str(bundle),
        "--profile",
        str(pinned),
        "--next",
        str(following),
        "--observed-at",
        INSTANTS[k + 1],
        *common,
    )
    ran = json.loads(cli_ok("run", str(bundle), "--profile", str(pinned), "--json", *common))
    run_id = ran["artifacts"]["apply"]["run_id"]
    cli_ok(
        "ship",
        str(bundle),
        "--run-id",
        run_id,
        "--profile",
        str(pinned),
        "-o",
        str(tmp_path / f"out-state-{k}"),
        *common,
    )
    run_dir = state / "runs" / run_id
    evidence = (run_dir / "EVIDENCE.md").read_text(encoding="utf-8")
    filed = json.loads((run_dir / "forecast-accuracy.json").read_text(encoding="utf-8"))
    return Pass(k, state, digest_of(k), digest_of(k + 1), evidence, filed, section_rows(evidence))


@dataclass
class Walk:
    """The two passes over one state directory, and the scoring refused between make and score."""

    passes: List[Pass]
    state: Path
    refused: Tuple[int, str, str]
    verdicts_after_refusal: List[str]


@pytest.fixture(scope="module")
def walked(tmp_path_factory: pytest.TempPathFactory) -> Walk:
    """Walk forward over the three states, with the order rule tried on the second pass."""
    tmp_path = tmp_path_factory.mktemp("walk-forward")
    state = tmp_path / "state"
    first = walk_pass(tmp_path, state, 1)
    # The second pass forecasts state 3 from state 2. Before it is scored, an observation dated
    # before the forecasts were registered is refused; the pass then goes on as the first did.
    bundle = pin(tmp_path, 2)
    cli_ok(
        "forecast",
        "make",
        str(bundle),
        "--profile",
        str(state_profile(2)),
        "--registered-at",
        INSTANTS[2],
        "--horizon-end",
        HORIZONS[2],
        "--state-dir",
        str(state),
    )
    refused = cli(
        "forecast",
        "score",
        str(bundle),
        "--profile",
        str(state_profile(2)),
        "--next",
        str(state_profile(3)),
        "--observed-at",
        "2026-09-30T00:00:00Z",
        "--state-dir",
        str(state),
    )
    store = AccuracyStore.for_state_dir(state)
    hexadecimal = digest_of(2).removeprefix("sha256:")[:16]
    written = [
        name
        for name in ("basic-control", "premium-offered")
        if store.verdict_path(f"{name}.{hexadecimal}").exists()
    ]
    second = walk_pass(tmp_path, state, 2)
    return Walk([first, second], state, refused, written)


def next_cell(k: int, metric: str) -> Tuple[int, int]:
    """``(k, true count)`` of the cell *metric* names (``query:<id>/<plan>``) in state *k*."""
    query, plan = metric.removeprefix("query:").split("/")
    document = json.loads(state_profile(k).read_text(encoding="utf-8"))
    entry = next(item for item in document["queries"] if item["name"] == query)
    cell = next(g for g in entry["statistics"][0]["groups"] if g["group"] == plan)
    return int(cell["k"]), int(cell["levelCounts"][1])


# ------------------------------------------------------------------ every forecast has a verdict


def test_every_forecast_of_both_passes_has_a_verdict_and_a_row(
    walked: Walk,
) -> None:
    store = AccuracyStore.for_state_dir(walked.state)
    for walk in walked.passes:
        hexadecimal = walk.pinned_digest.removeprefix("sha256:")[:16]
        expected = [f"basic-control.{hexadecimal}", f"premium-offered.{hexadecimal}"]
        assert [row["predictionId"] for row in walk.filed["rows"]] == expected
        assert [row[0] for row in walk.rows] == [f"`{name}`" for name in expected]
        for row in walk.filed["rows"]:
            assert row["status"] == "scored", row
            verdict = json.loads(store.verdict_path(row["predictionId"]).read_text("utf-8"))
            assert verdict["nextStateDigest"] == walk.next_digest
    assert "## Forecast Accuracy" in walked.passes[0].evidence
    assert walked.passes[0].evidence.index("## Fidelity") < (
        walked.passes[0].evidence.index("## Forecast Accuracy")
    )


def test_the_three_states_are_three_releases(
    walked: Walk,
) -> None:
    digests = [digest_of(k) for k in (1, 2, 3)]
    assert len(set(digests)) == 3
    first, second = walked.passes
    assert (first.pinned_digest, first.next_digest) == (digests[0], digests[1])
    assert (second.pinned_digest, second.next_digest) == (digests[1], digests[2])


# ------------------------------------------------------------------ the rows of each pass


def _assert_filed_row(walk: Any, filed: Dict[str, Any]) -> Tuple[Decimal, int, bool]:
    """The filed row's state, forecast, observation, scores and drift, recomputed from the next cell.

    Returns the observed rate, its record count and whether it is inside the interval.
    """
    records, matches = next_cell(walk.k + 1, filed["metric"])
    observed = Decimal(matches) / Decimal(records)
    low, high = Decimal(filed["intervalLow"]), Decimal(filed["intervalHigh"])
    forecast = Decimal(filed["forecast"])
    assert filed["stateDigest"] == walk.pinned_digest
    assert filed["registeredAt"] == INSTANTS[walk.k]
    assert abs(Decimal(filed["observed"]) - observed) <= TOLERANCE
    assert filed["sampleSize"] == records
    assert low <= forecast <= high
    # The result, the scores and the drift, recomputed from the next state's own cell.
    inside = low <= observed <= high
    assert filed["inside"] is inside
    error = abs(observed - forecast)
    assert abs(Decimal(filed["absoluteError"]) - error) <= TOLERANCE
    score = high - low
    score += (2 / ALPHA) * (low - observed) if observed < low else 0
    score += (2 / ALPHA) * (observed - high) if observed > high else 0
    assert abs(Decimal(filed["intervalScore"]) - score) <= TOLERANCE
    assert filed["drift"]["metric"] == "psi" and filed["drift"]["threshold"] == "0.2"
    return observed, records, inside


def _assert_shown_row(
    walk: Any, shown: Sequence[str], filed: Dict[str, Any], records: int, inside: bool
) -> None:
    """The same row, as the section shows it."""
    assert shown[0] == f"`{filed['predictionId']}`" and shown[1] == f"`{filed['metric']}`"
    assert shown[2] == f"`{walk.pinned_digest}`" and shown[3] == f"`{INSTANTS[walk.k]}`"
    assert shown[4] == f"{filed['forecast']} [{filed['intervalLow']}, {filed['intervalHigh']}]"
    assert shown[5] == f"{filed['observed']} ({records} records)"
    assert shown[6] == ("inside" if inside else "outside")
    assert shown[7] == filed["intervalScore"] and shown[8] == filed["absoluteError"]
    drift = filed["drift"]
    assert shown[9] == (
        f"`psi` {drift['largest']} at `{drift['statistic']}`, "
        f"{'stale' if drift['stale'] else 'not stale'}"
    )


def test_each_row_holds_its_state_forecast_observation_and_scores(
    walked: Walk,
) -> None:
    for walk in walked.passes:
        assert len(walk.rows) == len(walk.filed["rows"]) == 2
        for shown, filed in zip(walk.rows, walk.filed["rows"], strict=True):
            _observed, records, inside = _assert_filed_row(walk, filed)
            _assert_shown_row(walk, shown, filed, records, inside)


def test_the_file_holds_the_rows_of_the_section(
    walked: Walk,
) -> None:
    for walk in walked.passes:
        assert [f"`{row['predictionId']}`" for row in walk.filed["rows"]] == [
            row[0] for row in walk.rows
        ]
        for filed, shown in zip(walk.filed["rows"], walk.rows, strict=True):
            assert shown[7] == filed["intervalScore"] and shown[8] == filed["absoluteError"]
            assert shown[6] == ("inside" if filed["inside"] else "outside")


def test_the_drifted_premium_forecast_of_the_second_pass_is_outside_its_interval(
    walked: Walk,
) -> None:
    first, second = walked.passes
    assert all(row["inside"] for row in first.filed["rows"])
    premium = next(r for r in second.filed["rows"] if r["metric"].endswith("/premium"))
    assert premium["inside"] is False
    assert Decimal(premium["intervalScore"]) > Decimal(premium["intervalHigh"]) - Decimal(
        premium["intervalLow"]
    )


# ------------------------------------------------------------------ drift


def test_the_calibration_is_stale_on_the_drifted_pass_only(
    walked: Walk,
) -> None:
    first, second = walked.passes
    assert [row["drift"]["stale"] for row in first.filed["rows"]] == [False, False]
    assert [row["drift"]["stale"] for row in second.filed["rows"]] == [True, True]
    assert all(row[9].endswith(", not stale") for row in first.rows)
    assert all(
        row[9].endswith(", stale") and not row[9].endswith(", not stale") for row in second.rows
    )
    for row in second.filed["rows"]:
        assert row["drift"]["statistic"].endswith("/premium")
        assert Decimal(row["drift"]["largest"]) > Decimal(row["drift"]["threshold"])
    for row in first.filed["rows"]:
        assert Decimal(row["drift"]["largest"]) < Decimal(row["drift"]["threshold"])


# ------------------------------------------------------------------ totals, coverage, grades


def test_the_same_state_counts_and_the_coverage_beside_the_declared_one(
    walked: Walk,
) -> None:
    for walk in walked.passes:
        inside = sum(1 for row in walk.filed["rows"] if row["inside"])
        measured = str(Decimal(inside) / Decimal(2))
        assert walk.filed["totals"] == {
            "declaredCoverage": "0.92",
            "inside": inside,
            "measuredCoverage": measured,
            "playback": "pb-renewal",
            "registered": 2,
            "scored": 2,
        }
        assert walk.filed["stateCounts"] == {walk.pinned_digest: 2}
        text = walk.evidence
        assert (
            f"State `{walk.pinned_digest}`: 2 forecasts in the store across all bundles, "
            "2 of them for this run." in text
        )
        assert "Registered for this run: 2. Scored: 2. Not scored: 0. Pending: 0." in text
        assert f"Inside their interval: {inside} of 2." in text
        assert f"Measured interval coverage: {measured} (2 scored forecasts" not in text
        assert f"Measured interval coverage: {measured} ({inside} of 2 scored forecasts)." in text
        assert "Declared interval coverage: 0.92, from the Playback `pb-renewal`." in text
    first, second = walked.passes
    assert first.filed["totals"]["measuredCoverage"] == "1"
    assert second.filed["totals"]["measuredCoverage"] == "0.5"


def test_the_grades_and_the_weakest_one(
    walked: Walk,
) -> None:
    for walk in walked.passes:
        assert walk.filed["grades"] == {
            "next": {walk.next_digest: "P1"},
            "pinned": {walk.pinned_digest: "P1"},
            "weakest": "P1",
        }
        assert f"Pinned state `{walk.pinned_digest}`: **P1 (checked)**" in walk.evidence
        assert f"Next state `{walk.next_digest}`: **P1 (checked)**" in walk.evidence
        privacy = walk.evidence.split("## Privacy Grade")[1].split("## Column Provenance")[0]
        assert "Weakest grade of every profile this run read: **P1 (checked)**." in privacy


def test_no_banned_word_reaches_either_report(
    walked: Walk,
) -> None:
    for walk in walked.passes:
        require_no_banned_words(walk.evidence)
        require_no_banned_words(json.dumps(walk.filed))


# ------------------------------------------------------------------ the order rule


def test_scoring_with_an_observation_before_the_registration_is_refused(
    walked: Walk,
) -> None:
    code, out, err = walked.refused
    assert code != 0
    assert "FLX-7001" in err + out
    # The refused scoring wrote nothing: the verdicts on state 2's forecasts are the valid
    # scoring's, observed after the registration.
    store = AccuracyStore.for_state_dir(walked.state)
    hexadecimal = digest_of(2).removeprefix("sha256:")[:16]
    for name in ("basic-control", "premium-offered"):
        verdict = json.loads(store.verdict_path(f"{name}.{hexadecimal}").read_text("utf-8"))
        assert verdict["observedAt"] == INSTANTS[3]
